"""Fuentes de datos externas: APIs REST que un asistente puede consultar en caliente.

El admin pega el spec OpenAPI (JSON o YAML) de la API, elige qué operaciones GET
exponer y carga la credencial. Cada operación elegida se vuelve una herramienta
LangChain con su descripción y parámetros tal cual vienen del spec; el LLM decide
cuándo llamarla y con qué valores, el backend hace la petición HTTP y le devuelve
el resultado al modelo para que conteste con datos reales.

Alcance deliberado (2026-11-04): sólo GET (lectura), sólo admins dan de alta, y un
asistente con fuentes no puede usar modelos de Ollama (los locales chicos no llaman
herramientas de forma confiable; ver app.py donde se valida).

Credenciales: se guardan cifradas con Fernet usando `FUENTES_CLAVE` del .env. Sin
esa variable el alta de fuentes con credencial se rechaza — nunca se guarda en
claro. Para generar una clave: `python -c "from cryptography.fernet import Fernet;
print(Fernet.generate_key().decode())"`.

Este módulo NO importa `app.py` — al revés.
"""
import json
import math
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

AGENTES_DB_PATH = os.getenv('AGENTES_DB_PATH', 'agentes.db')

AUTH_TIPOS = ('ninguna', 'header', 'query', 'basic')

# Lo que se le regresa al LLM por llamada. Las respuestas grandes se truncan y se
# le dice al modelo que están truncadas, en vez de reventar el contexto.
MAX_CARACTERES_RESPUESTA = 6000
TIMEOUT_SEGUNDOS = 15
# Vueltas máximas del ciclo "el modelo pide una herramienta → se ejecuta → vuelve
# a pensar". Protege contra un modelo que se queda llamando APIs en bucle.
MAX_RONDAS_HERRAMIENTAS = 5
# Ejemplos y defaults del spec más largos que esto (en JSON) no se guardan: no
# sirven para prellenar un campo y sólo gastarían tokens en cada consulta.
MAX_LARGO_EJEMPLO = 300
# Valores de un enum que se le listan al modelo en la descripción del parámetro
# (Open-Meteo tiene uno de 355). El formulario de prueba sí los muestra todos.
MAX_VALORES_ENUM_MODELO = 60


class FuenteError(Exception):
    """Error de validación o de configuración, redactado para el admin."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connection():
    conn = sqlite3.connect(AGENTES_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_fuentes_db():
    conn = sqlite3.connect(AGENTES_DB_PATH)
    try:
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('''CREATE TABLE IF NOT EXISTS fuentes_externas (
            id                  TEXT PRIMARY KEY,
            proyecto_id         TEXT NOT NULL,
            nombre              TEXT NOT NULL,
            descripcion         TEXT,
            url_base            TEXT NOT NULL,
            spec                TEXT NOT NULL,
            operaciones         TEXT NOT NULL,
            auth_tipo           TEXT NOT NULL DEFAULT 'ninguna',
            auth_nombre         TEXT,
            auth_secreto_cifrado TEXT,
            creado_en           TEXT NOT NULL,
            actualizado_en      TEXT NOT NULL,
            FOREIGN KEY (proyecto_id) REFERENCES proyectos(id) ON DELETE RESTRICT
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_fuentes_proyecto ON fuentes_externas(proyecto_id)')
        # Relación asistente ↔ fuentes. Borrar el asistente o la fuente limpia la fila.
        conn.execute('''CREATE TABLE IF NOT EXISTS agentes_fuentes (
            agente_id  TEXT NOT NULL,
            fuente_id  TEXT NOT NULL,
            PRIMARY KEY (agente_id, fuente_id),
            FOREIGN KEY (agente_id) REFERENCES agentes(id) ON DELETE CASCADE,
            FOREIGN KEY (fuente_id) REFERENCES fuentes_externas(id) ON DELETE CASCADE
        )''')
        conn.commit()
    finally:
        conn.close()


# ─── Cifrado de credenciales ───────────────────────────────────────────────

def clave_configurada() -> bool:
    return bool((os.getenv('FUENTES_CLAVE') or '').strip())


def _fernet():
    clave = (os.getenv('FUENTES_CLAVE') or '').strip()
    if not clave:
        raise FuenteError(
            "El servidor no tiene FUENTES_CLAVE en el .env, así que no puede guardar "
            "credenciales de APIs externas. Genera una con "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
            "y agrégala al .env."
        )
    from cryptography.fernet import Fernet
    try:
        return Fernet(clave.encode())
    except Exception:
        raise FuenteError("FUENTES_CLAVE no es una clave Fernet válida (debe ser la que genera Fernet.generate_key()).")


def cifrar(texto: str) -> str:
    return _fernet().encrypt(texto.encode('utf-8')).decode('ascii')


def descifrar(cifrado: str) -> str:
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(cifrado.encode('ascii')).decode('utf-8')
    except InvalidToken:
        raise FuenteError(
            "No se pudo descifrar la credencial de la fuente: FUENTES_CLAVE cambió desde "
            "que se guardó. Vuelve a capturar la credencial."
        )


# ─── Parseo del spec OpenAPI ───────────────────────────────────────────────

_TIPOS = {'string': str, 'integer': int, 'number': float, 'boolean': bool}


def _cargar_spec(texto: str) -> dict:
    texto = (texto or '').strip()
    if not texto:
        raise FuenteError("El spec está vacío.")
    try:
        doc = json.loads(texto)
    except ValueError:
        try:
            import yaml
            doc = yaml.safe_load(texto)
        except Exception as e:
            raise FuenteError(f"El spec no es JSON ni YAML válido: {e}")
    if not isinstance(doc, dict) or 'paths' not in doc:
        raise FuenteError("El spec no parece OpenAPI: no tiene la sección `paths`.")
    return doc


MAX_BYTES_SPEC = 3 * 1024 * 1024


def descargar_spec(url: str) -> str:
    """Trae el spec de una URL (p. ej. el /openapi.json de un FastAPI o un .yml en
    GitHub). Sólo http(s), 15 s y 3 MB: el admin ya puede pegar cualquier texto,
    así que esto no abre nada nuevo, pero no hay que convertir al server en proxy."""
    url = (url or '').strip()
    if not re.match(r'^https?://', url, re.I):
        raise FuenteError("La URL del spec debe empezar con http:// o https://.")
    try:
        with httpx.Client(timeout=TIMEOUT_SEGUNDOS, follow_redirects=True) as cliente:
            with cliente.stream('GET', url, headers={'Accept': 'application/json, application/yaml, text/yaml, text/plain;q=0.8, */*;q=0.5'}) as r:
                if r.status_code != 200:
                    raise FuenteError(f"La URL del spec respondió {r.status_code}.")
                partes, total = [], 0
                for trozo in r.iter_bytes():
                    total += len(trozo)
                    if total > MAX_BYTES_SPEC:
                        raise FuenteError("El spec pesa más de 3 MB; pégalo recortado o pásalo por archivo.")
                    partes.append(trozo)
    except httpx.TimeoutException:
        raise FuenteError(f"La URL del spec no respondió en {TIMEOUT_SEGUNDOS}s.")
    except httpx.HTTPError as e:
        raise FuenteError(f"No se pudo descargar el spec: {e}")
    texto = b''.join(partes).decode('utf-8', errors='replace').strip()
    if texto.startswith('<'):
        raise FuenteError("Esa URL devuelve HTML, no un spec: suele ser la página de docs. En FastAPI usa /openapi.json.")
    return texto


def _resolver_ref(doc: dict, obj: Any) -> Any:
    """Sigue `$ref` locales (#/components/... o #/parameters/...), un nivel a la vez."""
    vistos = 0
    while isinstance(obj, dict) and '$ref' in obj and vistos < 10:
        ref = obj['$ref']
        if not isinstance(ref, str) or not ref.startswith('#/'):
            break
        nodo: Any = doc
        for parte in ref[2:].split('/'):
            parte = parte.replace('~1', '/').replace('~0', '~')
            if not isinstance(nodo, dict) or parte not in nodo:
                return obj
            nodo = nodo[parte]
        obj = nodo
        vistos += 1
    return obj


def _url_de_servers(servers: Any) -> str:
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        return str(servers[0].get('url') or '').rstrip('/')
    return ''


def _url_base_sugerida(doc: dict) -> str:
    url = _url_de_servers(doc.get('servers'))
    if url:
        return url
    # Swagger 2
    host = doc.get('host')
    if host:
        esquema = (doc.get('schemes') or ['https'])[0]
        return f"{esquema}://{host}{doc.get('basePath') or ''}".rstrip('/')
    # OpenAPI también deja declarar `servers` por ruta o por operación (Open-Meteo
    # lo hace así): se sugiere el de la primera operación GET que lo traiga.
    for item in (doc.get('paths') or {}).values():
        item = _resolver_ref(doc, item)
        if isinstance(item, dict) and isinstance(item.get('get'), dict):
            url = _url_de_servers(item['get'].get('servers')) or _url_de_servers(item.get('servers'))
            if url:
                return url
    return ''


def _nombre_herramienta(operation_id: Optional[str], ruta: str) -> str:
    base = operation_id or ('get_' + re.sub(r'[{}]', '', ruta))
    base = re.sub(r'[^a-zA-Z0-9_-]+', '_', base).strip('_')
    return (base or 'operacion')[:64]


def _esquema_efectivo(doc: dict, esquema: Any) -> dict:
    """Quita la envoltura de nulabilidad para llegar al tipo real: `anyOf: [{X},
    {type: null}]` (así declara FastAPI un parámetro Optional), `allOf: [{$ref}]`
    (enums de Pydantic v1) o `type: [X, 'null']` (OpenAPI 3.1). Lo declarado afuera
    (default, example, description) gana sobre lo de la variante."""
    esquema = _resolver_ref(doc, esquema)
    for _ in range(3):
        if not isinstance(esquema, dict):
            return {}
        variantes = esquema.get('anyOf') or esquema.get('oneOf') or esquema.get('allOf')
        if 'type' in esquema or not isinstance(variantes, list):
            break
        no_nulas = [v for v in (_resolver_ref(doc, x) for x in variantes)
                    if isinstance(v, dict) and v.get('type') != 'null']
        if len(no_nulas) != 1:
            break
        afuera = {k: v for k, v in esquema.items() if k not in ('anyOf', 'oneOf', 'allOf')}
        esquema = {**no_nulas[0], **afuera}
    if not isinstance(esquema, dict):
        return {}
    tipo = esquema.get('type')
    if isinstance(tipo, list):
        no_nulos = [t for t in tipo if t != 'null']
        esquema = {**esquema, 'type': no_nulos[0] if no_nulos else 'string'}
    return esquema


def _valor_json(v: Any) -> Any:
    """Lo que trae el spec, en algo que json.dumps acepte: el YAML vuelve un
    `2024-01-01` sin comillas un `date`, y `.nan` un float que el JSON rechaza."""
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_valor_json(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _valor_json(x) for k, x in v.items()}
    if hasattr(v, 'isoformat'):
        return v.isoformat()
    return str(v)


def _valor_corto(v: Any) -> Any:
    """Ejemplo/default listo para guardar, o None si viene vacío o es enorme."""
    if v is None:
        return None
    v = _valor_json(v)
    return v if len(json.dumps(v, ensure_ascii=False)) <= MAX_LARGO_EJEMPLO else None


def _ejemplo(doc: dict, p: dict, esquema: dict, items: dict) -> Any:
    """El primer ejemplo que declare el spec. Cada generador lo pone en otro lado:
    `example` o `examples` (mapa de Example Objects) del parámetro, `example` o
    `examples` (lista, OpenAPI 3.1; así lo emite FastAPI con Query(examples=[...]))
    del schema, o `x-example` (Swagger 2)."""
    if p.get('example') is not None:
        return p['example']
    ejemplos = p.get('examples')
    if isinstance(ejemplos, dict):
        for e in ejemplos.values():
            e = _resolver_ref(doc, e)
            if isinstance(e, dict) and e.get('value') is not None:
                return e['value']
    elif isinstance(ejemplos, list) and ejemplos:
        return ejemplos[0]
    if esquema.get('example') is not None:
        return esquema['example']
    if isinstance(esquema.get('examples'), list) and esquema['examples']:
        return esquema['examples'][0]
    if p.get('x-example') is not None:
        return p['x-example']
    if items.get('example') is not None:
        return [items['example']]
    return None


def _separador_lista(p: dict, donde: str) -> Optional[str]:
    """Cómo viaja un arreglo: None = repitiendo el parámetro (?a=1&a=2); si no, el
    separador con el que se juntan los valores (?a=1,2)."""
    if 'schema' not in p:
        # Swagger 2: collectionFormat, y su valor por omisión es csv.
        formato = p.get('collectionFormat') or 'csv'
        return {'ssv': ' ', 'tsv': '\t', 'pipes': '|', 'multi': None}.get(formato, ',')
    if donde != 'query':
        return ','  # path y header usan style=simple: siempre con comas
    estilo = p.get('style') or 'form'
    if p.get('explode', estilo == 'form'):
        return None
    return {'spaceDelimited': ' ', 'pipeDelimited': '|'}.get(estilo, ',')


def _parametro(doc: dict, p: Any) -> Optional[dict]:
    p = _resolver_ref(doc, p)
    if not isinstance(p, dict) or 'name' not in p:
        return None
    donde = p.get('in')
    if donde not in ('path', 'query', 'header'):
        return None
    esquema = _esquema_efectivo(doc, p.get('schema') or {})
    tipo = esquema.get('type') or p.get('type') or 'string'
    if not isinstance(tipo, str):
        tipo = 'string'
    lista = tipo == 'array'
    items = _esquema_efectivo(doc, esquema.get('items') or p.get('items') or {}) if lista else {}
    if tipo not in _TIPOS:
        # Un arreglo se pide (al modelo y en Probar) como texto separado por comas;
        # `ejecutar` lo parte y lo manda como diga el spec (ver `separador`).
        tipo = 'string'
    if lista:
        enum = items.get('enum')
    else:
        enum = esquema.get('enum') if 'enum' in esquema else p.get('enum')
    d = {
        'nombre': p['name'],
        'en': donde,
        'requerido': bool(p.get('required')) or donde == 'path',
        'tipo': tipo,
        'descripcion': (p.get('description') or '').strip(),
        'enum': _valor_json(enum) if isinstance(enum, list) and enum else None,
        'ejemplo': _valor_corto(_ejemplo(doc, p, esquema, items)),
        'default': _valor_corto(esquema['default'] if 'default' in esquema else p.get('default')),
    }
    if lista:
        d['lista'] = True
        d['separador'] = _separador_lista(p, donde)
    return d


def analizar_spec(texto: str) -> dict:
    """Devuelve {url_base_sugerida, titulo, operaciones:[...]} con SOLO las operaciones GET."""
    doc = _cargar_spec(texto)
    operaciones = []
    for ruta, item in (doc.get('paths') or {}).items():
        item = _resolver_ref(doc, item)
        if not isinstance(item, dict):
            continue
        op = item.get('get')
        if not isinstance(op, dict):
            continue
        params = []
        vistos = set()
        for p in list(item.get('parameters') or []) + list(op.get('parameters') or []):
            d = _parametro(doc, p)
            if d and (d['nombre'], d['en']) not in vistos:
                vistos.add((d['nombre'], d['en']))
                params.append(d)
        nombre = _nombre_herramienta(op.get('operationId'), ruta)
        operaciones.append({
            'id': nombre,
            'ruta': ruta,
            'resumen': (op.get('summary') or '').strip(),
            'descripcion': (op.get('description') or '').strip(),
            'parametros': params,
        })
    if not operaciones:
        raise FuenteError("El spec no tiene ninguna operación GET; por ahora sólo se exponen lecturas.")
    # Nombres de herramienta únicos aunque el spec repita operationId.
    vistos: dict = {}
    for o in operaciones:
        n = vistos.get(o['id'], 0)
        vistos[o['id']] = n + 1
        if n:
            o['id'] = f"{o['id']}_{n + 1}"[:64]
    info = doc.get('info') if isinstance(doc.get('info'), dict) else {}
    return {
        'url_base_sugerida': _url_base_sugerida(doc),
        'titulo': (info.get('title') or '').strip(),
        'operaciones': operaciones,
    }


# ─── CRUD ──────────────────────────────────────────────────────────────────

_COLS = ("id, proyecto_id, nombre, descripcion, url_base, spec, operaciones, auth_tipo, "
         "auth_nombre, auth_secreto_cifrado, creado_en, actualizado_en")


def _publica(fila: sqlite3.Row | dict, con_spec: bool = False) -> dict:
    """Versión sin secretos para el API."""
    d = dict(fila)
    ops = json.loads(d.pop('operaciones') or '[]')
    d['operaciones'] = ops
    d['auth_configurada'] = bool(d.pop('auth_secreto_cifrado'))
    if not con_spec:
        d.pop('spec', None)
    return d


def listar(proyecto_id: Optional[str] = None) -> list[dict]:
    conn = _connection()
    try:
        if proyecto_id:
            rows = conn.execute(f"SELECT {_COLS} FROM fuentes_externas WHERE proyecto_id=? ORDER BY creado_en DESC",
                                (proyecto_id,)).fetchall()
        else:
            rows = conn.execute(f"SELECT {_COLS} FROM fuentes_externas ORDER BY creado_en DESC").fetchall()
        return [_publica(r) for r in rows]
    finally:
        conn.close()


def obtener(fid: str, con_spec: bool = False) -> Optional[dict]:
    conn = _connection()
    try:
        row = conn.execute(f"SELECT {_COLS} FROM fuentes_externas WHERE id=?", (fid,)).fetchone()
        return _publica(row, con_spec) if row else None
    finally:
        conn.close()


def _fila_completa(conn, fid: str) -> Optional[dict]:
    row = conn.execute(f"SELECT {_COLS} FROM fuentes_externas WHERE id=?", (fid,)).fetchone()
    return dict(row) if row else None


def _validar_auth(auth_tipo: Optional[str], auth_nombre: Optional[str], auth_secreto: Optional[str],
                  secreto_previo: Optional[str]) -> tuple[str, Optional[str], Optional[str]]:
    """Devuelve (auth_tipo, auth_nombre, auth_secreto_cifrado)."""
    tipo = (auth_tipo or 'ninguna').strip().lower()
    if tipo not in AUTH_TIPOS:
        raise FuenteError(f"auth_tipo inválido: {auth_tipo!r}. Opciones: {', '.join(AUTH_TIPOS)}.")
    if tipo == 'ninguna':
        return tipo, None, None
    nombre = (auth_nombre or '').strip()
    if not nombre:
        etiqueta = {'header': 'el nombre de la cabecera (ej. X-API-Key o Authorization)',
                    'query': 'el nombre del parámetro (ej. api_key)',
                    'basic': 'el usuario'}[tipo]
        raise FuenteError(f"Para auth_tipo={tipo} falta {etiqueta}.")
    secreto = auth_secreto if auth_secreto is not None else None
    if secreto is not None and secreto.strip():
        return tipo, nombre, cifrar(secreto.strip())
    if secreto_previo:
        return tipo, nombre, secreto_previo
    raise FuenteError("Falta la credencial (auth_secreto) para esta fuente.")


def _validar_operaciones(spec: str, elegidas: list) -> tuple[list[dict], str]:
    analisis = analizar_spec(spec)
    por_id = {o['id']: o for o in analisis['operaciones']}
    ids = [str(x).strip() for x in (elegidas or []) if str(x).strip()]
    if not ids:
        raise FuenteError("Elige al menos una operación del spec para exponerla al asistente.")
    desconocidas = [i for i in ids if i not in por_id]
    if desconocidas:
        raise FuenteError(f"Estas operaciones no existen en el spec: {', '.join(desconocidas)}.")
    return [por_id[i] for i in ids], analisis['url_base_sugerida']


def crear(proyecto_id: str, nombre: str, descripcion: Optional[str], url_base: Optional[str], spec: str,
          operaciones: list, auth_tipo: Optional[str], auth_nombre: Optional[str], auth_secreto: Optional[str]) -> dict:
    nombre = (nombre or '').strip()
    if not nombre:
        raise FuenteError("El nombre es obligatorio.")
    ops, sugerida = _validar_operaciones(spec, operaciones)
    url = (url_base or sugerida or '').strip().rstrip('/')
    if not re.match(r'^https?://', url):
        raise FuenteError("La URL base debe empezar con http:// o https:// (si el spec no la trae completa, escríbela a mano).")
    tipo, anombre, cifrado = _validar_auth(auth_tipo, auth_nombre, auth_secreto, None)
    conn = _connection()
    try:
        if not conn.execute("SELECT 1 FROM proyectos WHERE id=?", (proyecto_id,)).fetchone():
            raise FuenteError(f"El proyecto {proyecto_id!r} no existe.")
        fid = uuid.uuid4().hex
        ahora = _now()
        conn.execute(
            f"INSERT INTO fuentes_externas ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, proyecto_id, nombre, (descripcion or '').strip() or None, url, spec,
             json.dumps(ops, ensure_ascii=False), tipo, anombre, cifrado, ahora, ahora),
        )
        conn.commit()
    finally:
        conn.close()
    return obtener(fid)


def actualizar(fid: str, cambios: dict) -> dict:
    """`cambios` trae sólo las llaves enviadas. auth_secreto ausente/None = conservar la credencial."""
    conn = _connection()
    try:
        actual = _fila_completa(conn, fid)
        if not actual:
            raise FuenteError(f"La fuente {fid!r} no existe.")
        nombre = actual['nombre'] if cambios.get('nombre') is None else (cambios['nombre'] or '').strip()
        if not nombre:
            raise FuenteError("El nombre es obligatorio.")
        descripcion = actual['descripcion'] if 'descripcion' not in cambios else ((cambios['descripcion'] or '').strip() or None)
        spec = actual['spec'] if cambios.get('spec') is None else cambios['spec']
        elegidas = (cambios['operaciones'] if cambios.get('operaciones') is not None
                    else [o['id'] for o in json.loads(actual['operaciones'])])
        ops, sugerida = _validar_operaciones(spec, elegidas)
        url = actual['url_base'] if cambios.get('url_base') is None else (cambios['url_base'] or sugerida or '')
        url = url.strip().rstrip('/')
        if not re.match(r'^https?://', url):
            raise FuenteError("La URL base debe empezar con http:// o https://.")
        tipo, anombre, cifrado = _validar_auth(
            cambios.get('auth_tipo', actual['auth_tipo']),
            cambios.get('auth_nombre', actual['auth_nombre']),
            cambios.get('auth_secreto'),
            actual['auth_secreto_cifrado'],
        )
        conn.execute(
            """UPDATE fuentes_externas SET nombre=?, descripcion=?, url_base=?, spec=?, operaciones=?,
               auth_tipo=?, auth_nombre=?, auth_secreto_cifrado=?, actualizado_en=? WHERE id=?""",
            (nombre, descripcion, url, spec, json.dumps(ops, ensure_ascii=False), tipo, anombre, cifrado, _now(), fid),
        )
        conn.commit()
    finally:
        conn.close()
    return obtener(fid)


def asistentes_que_usan(fid: str) -> list[dict]:
    conn = _connection()
    try:
        rows = conn.execute(
            """SELECT a.id, a.slug, a.nombre FROM agentes_fuentes af JOIN agentes a ON a.id = af.agente_id
               WHERE af.fuente_id=? ORDER BY a.nombre""", (fid,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def borrar(fid: str) -> dict:
    conn = _connection()
    try:
        previo = _fila_completa(conn, fid)
        if not previo:
            raise FuenteError(f"La fuente {fid!r} no existe.")
        conn.execute("DELETE FROM fuentes_externas WHERE id=?", (fid,))
        conn.commit()
        return {'id': previo['id'], 'nombre': previo['nombre']}
    finally:
        conn.close()


# ─── Relación con asistentes ───────────────────────────────────────────────

def ids_de_agente(conn, agente_id: str) -> list[str]:
    rows = conn.execute("SELECT fuente_id FROM agentes_fuentes WHERE agente_id=? ORDER BY fuente_id", (agente_id,)).fetchall()
    return [r[0] for r in rows]


def anexar_a_agentes(conn, agentes: list[dict]) -> list[dict]:
    """Agrega `fuentes: [ids]` a cada dict de agente (una sola consulta)."""
    if not agentes:
        return agentes
    ids = [a['id'] for a in agentes]
    marcas = ','.join('?' * len(ids))
    rows = conn.execute(f"SELECT agente_id, fuente_id FROM agentes_fuentes WHERE agente_id IN ({marcas}) ORDER BY fuente_id", ids).fetchall()
    mapa: dict = {}
    for r in rows:
        mapa.setdefault(r[0], []).append(r[1])
    for a in agentes:
        a['fuentes'] = mapa.get(a['id'], [])
    return agentes


def validar_asignacion(conn, proyecto_id: str, fuente_ids: list) -> list[str]:
    """Las fuentes deben existir y ser del mismo proyecto que el asistente."""
    ids = list(dict.fromkeys(str(x).strip() for x in (fuente_ids or []) if str(x).strip()))
    if not ids:
        return []
    marcas = ','.join('?' * len(ids))
    rows = conn.execute(f"SELECT id, proyecto_id, nombre FROM fuentes_externas WHERE id IN ({marcas})", ids).fetchall()
    por_id = {r[0]: r for r in rows}
    faltan = [i for i in ids if i not in por_id]
    if faltan:
        raise FuenteError(f"Estas fuentes no existen: {', '.join(faltan)}.")
    ajenas = [por_id[i][2] for i in ids if por_id[i][1] != proyecto_id]
    if ajenas:
        raise FuenteError(f"Estas fuentes son de otro proyecto: {', '.join(ajenas)}.")
    return ids


def guardar_asignacion(conn, agente_id: str, fuente_ids: list[str]):
    conn.execute("DELETE FROM agentes_fuentes WHERE agente_id=?", (agente_id,))
    conn.executemany("INSERT INTO agentes_fuentes (agente_id, fuente_id) VALUES (?, ?)",
                     [(agente_id, f) for f in fuente_ids])


def modelo_permitido(proveedor: Optional[str]) -> bool:
    """Los modelos locales de Ollama no llaman herramientas de forma confiable."""
    return proveedor in ('openai', 'anthropic', 'google')


# ─── Ejecución de una operación ────────────────────────────────────────────

def _credencial(fuente: dict) -> Optional[str]:
    cifrado = fuente.get('auth_secreto_cifrado')
    return descifrar(cifrado) if cifrado else None


def _texto(v: Any) -> str:
    """Un valor del spec o del modelo como lo escribiría una URL (true, no True)."""
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if v is None:
        return ''
    if isinstance(v, (list, tuple)):
        return ','.join(_texto(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _elementos_lista(valor: Any) -> list[str]:
    """Un arreglo llega como lista o como texto separado por comas (así lo mandan
    el modelo y el formulario de prueba)."""
    crudos = list(valor) if isinstance(valor, (list, tuple)) else str(valor).split(',')
    return [t for t in (_texto(x).strip() for x in crudos) if t]


def ejecutar(fuente: dict, operacion: dict, argumentos: dict) -> dict:
    """Hace el GET y devuelve {ok, status, url, ms, cuerpo, truncado, error}.
    `fuente` es la fila completa (con el secreto cifrado)."""
    ruta = operacion['ruta']
    params: dict = {}
    headers: dict = {'Accept': 'application/json, text/plain;q=0.8, */*;q=0.5'}
    auth = None
    faltan = []
    for p in operacion.get('parametros', []):
        valor = argumentos.get(p['nombre'])
        if p.get('lista') and valor is not None:
            valor = _elementos_lista(valor) or None
        if valor is None or valor == '':
            if p['requerido']:
                faltan.append(p['nombre'])
            continue
        if p.get('lista'):
            if p['en'] == 'query' and p.get('separador') is None:
                params[p['nombre']] = valor  # httpx repite el parámetro por cada valor
                continue
            valor = (p.get('separador') or ',').join(valor)
        if p['en'] == 'path':
            ruta = ruta.replace('{' + p['nombre'] + '}', httpx.URL(path='/' + str(valor)).path[1:])
        elif p['en'] == 'query':
            params[p['nombre']] = valor
        elif p['en'] == 'header':
            headers[p['nombre']] = str(valor)
    if faltan:
        return {'ok': False, 'status': None, 'url': None, 'ms': 0, 'cuerpo': None, 'truncado': False,
                'error': f"Faltan parámetros obligatorios: {', '.join(faltan)}."}
    if '{' in ruta:
        return {'ok': False, 'status': None, 'url': None, 'ms': 0, 'cuerpo': None, 'truncado': False,
                'error': f"La ruta {ruta} tiene placeholders sin valor."}

    tipo = fuente.get('auth_tipo') or 'ninguna'
    secreto = _credencial(fuente) if tipo != 'ninguna' else None
    if tipo == 'header' and secreto:
        headers[fuente['auth_nombre']] = secreto
    elif tipo == 'query' and secreto:
        params[fuente['auth_nombre']] = secreto
    elif tipo == 'basic' and secreto:
        auth = (fuente['auth_nombre'], secreto)

    url = fuente['url_base'].rstrip('/') + '/' + ruta.lstrip('/')
    inicio = time.perf_counter()
    try:
        with httpx.Client(timeout=TIMEOUT_SEGUNDOS, follow_redirects=True) as cliente:
            r = cliente.get(url, params=params, headers=headers, auth=auth)
    except httpx.TimeoutException:
        return {'ok': False, 'status': None, 'url': url, 'ms': int((time.perf_counter() - inicio) * 1000),
                'cuerpo': None, 'truncado': False, 'error': f"La API no respondió en {TIMEOUT_SEGUNDOS}s."}
    except httpx.HTTPError as e:
        return {'ok': False, 'status': None, 'url': url, 'ms': int((time.perf_counter() - inicio) * 1000),
                'cuerpo': None, 'truncado': False, 'error': f"No se pudo conectar con la API: {e}"}
    ms = int((time.perf_counter() - inicio) * 1000)
    texto = r.text or ''
    # Si es JSON, se compacta para que quepa más contenido útil en el límite.
    try:
        texto = json.dumps(r.json(), ensure_ascii=False, separators=(',', ':'))
    except ValueError:
        pass
    truncado = len(texto) > MAX_CARACTERES_RESPUESTA
    if truncado:
        texto = texto[:MAX_CARACTERES_RESPUESTA]
    return {'ok': 200 <= r.status_code < 300, 'status': r.status_code, 'url': str(r.url), 'ms': ms,
            'cuerpo': texto, 'truncado': truncado,
            'error': None if 200 <= r.status_code < 300 else f"La API respondió {r.status_code}."}


def _texto_para_modelo(resultado: dict) -> str:
    if resultado['error'] and resultado['cuerpo'] is None:
        return f"ERROR: {resultado['error']}"
    partes = []
    if resultado['error']:
        partes.append(f"ERROR: {resultado['error']}")
    cuerpo = resultado['cuerpo'] or '(respuesta vacía)'
    if resultado['truncado']:
        cuerpo += f"\n[RESPUESTA TRUNCADA a {MAX_CARACTERES_RESPUESTA} caracteres; pide datos más específicos si falta algo]"
    partes.append(cuerpo)
    return '\n'.join(partes)


# ─── Herramientas LangChain para un asistente ──────────────────────────────

def _descripcion_parametro(p: dict) -> str:
    """Lo que lee el modelo de cada argumento: la descripción del spec más los
    valores válidos, un ejemplo y el default, para que mande el formato correcto."""
    desc = p['descripcion'] or f"Parámetro {p['nombre']} ({p['en']})"
    if p.get('lista'):
        desc += " Acepta varios valores separados por comas."
    if p.get('enum'):
        valores = [_texto(v) for v in p['enum']]
        desc += " Valores posibles: " + ', '.join(valores[:MAX_VALORES_ENUM_MODELO])
        if len(valores) > MAX_VALORES_ENUM_MODELO:
            desc += f" (y {len(valores) - MAX_VALORES_ENUM_MODELO} más)"
        desc += "."
    if p.get('ejemplo') is not None:
        desc += f" Ejemplo: {_texto(p['ejemplo'])}."
    if p.get('default') is not None:
        desc += f" Por defecto: {_texto(p['default'])}."
    return desc


def _esquema_argumentos(nombre: str, parametros: list[dict]):
    from pydantic import Field, create_model
    campos: dict = {}
    for p in parametros:
        tipo = _TIPOS.get(p['tipo'], str)
        desc = _descripcion_parametro(p)
        if p['requerido']:
            campos[p['nombre']] = (tipo, Field(..., description=desc))
        else:
            campos[p['nombre']] = (Optional[tipo], Field(None, description=desc))
    return create_model(f"Args_{re.sub(r'[^a-zA-Z0-9_]', '_', nombre)}", **campos)


def herramientas_para_agente(agente_id: str) -> tuple[list, list]:
    """Devuelve (herramientas LangChain, registro). `registro` es una lista que las
    herramientas van llenando con cada llamada hecha durante la consulta, para
    guardarla en el log."""
    from langchain_core.tools import StructuredTool
    conn = _connection()
    try:
        rows = conn.execute(
            f"""SELECT {_COLS.replace('id,', 'f.id,', 1)} FROM fuentes_externas f
                JOIN agentes_fuentes af ON af.fuente_id = f.id WHERE af.agente_id=? ORDER BY f.nombre""",
            (agente_id,)).fetchall()
        fuentes = [dict(r) for r in rows]
    finally:
        conn.close()

    registro: list = []
    herramientas = []
    nombres_usados: set = set()
    for fuente in fuentes:
        # La cabecera/parámetro de autenticación la pone el backend con la
        # credencial guardada: no se le expone al modelo como argumento.
        auth_nombre = (fuente.get('auth_nombre') or '').lower()
        for op in json.loads(fuente['operaciones'] or '[]'):
            op = dict(op)
            op['parametros'] = [p for p in op.get('parametros', [])
                                if not (p['en'] in ('header', 'query') and p['nombre'].lower() == auth_nombre)]
            nombre = op['id']
            if nombre in nombres_usados:
                nombre = f"{nombre}_{len(nombres_usados)}"[:64]
            nombres_usados.add(nombre)
            descripcion = ' '.join(x for x in (op.get('resumen'), op.get('descripcion')) if x).strip()
            descripcion = f"[{fuente['nombre']}] " + (descripcion or f"GET {op['ruta']}")
            if fuente.get('descripcion'):
                descripcion += f" ({fuente['descripcion']})"

            def _correr(_fuente=fuente, _op=op, _nombre=nombre, **kwargs):
                args = {k: v for k, v in kwargs.items() if v is not None}
                res = ejecutar(_fuente, _op, args)
                registro.append({
                    'fuente': _fuente['nombre'], 'herramienta': _nombre, 'parametros': args,
                    'status': res['status'], 'ms': res['ms'], 'ok': res['ok'], 'error': res['error'],
                })
                return _texto_para_modelo(res)

            herramientas.append(StructuredTool.from_function(
                func=_correr, name=nombre, description=descripcion[:1024],
                args_schema=_esquema_argumentos(nombre, op.get('parametros', [])),
            ))
    return herramientas, registro
