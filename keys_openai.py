"""API keys de OpenAI: la principal y una de respaldo.

La principal es `OPENAI_API_KEY`, la de siempre (en el server de CSI, la de
CSI). La de respaldo es `OPENAI_API_KEY_RESPALDO`, opcional: otra cuenta que
entra cuando la principal no está disponible. Sin respaldo configurada, todo se
comporta exactamente igual que antes de que existiera este módulo.

El modo lo elige el admin (tab "API keys") y vive en `agentes.db`:
- 'auto'      → la principal; si OpenAI la rechaza por algo propio de esa key o
                cuenta (inválida/revocada, sin saldo, límite de uso, sin acceso
                al modelo), la MISMA llamada se repite con la de respaldo.
- 'principal' → solo la principal. A la de respaldo nunca se le cobra nada.
- 'respaldo'  → solo la de respaldo, para forzarla durante una caída conocida.

Pausas: en modo auto, una key que acaba de fallar se manda al final de la fila
durante ENFRIAMIENTO_S. Sin esto cada consulta pagaría primero el intento
fallido — y un 429 por saldo agotado tarda varios segundos, porque el cliente
de OpenAI lo reintenta 2 veces antes de rendirse. Si el error es de la cuenta
entera (key inválida, sin saldo) la pausa aplica a todos los modelos; si es de
un modelo (sin acceso a él, límite de tokens por minuto), solo a ese modelo.
Las pausas viven en memoria del proceso: se borran al reiniciar el backend o al
cambiar el modo desde el admin.

El valor de las keys nunca sale de este módulo: `estado()` solo dice si cada
una está configurada.

Este módulo NO importa `app.py` — al revés. Lo usan `proveedores.py` (chat) y
`herramientas.py` (embeddings).
"""

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

AGENTES_DB_PATH = os.getenv('AGENTES_DB_PATH', 'agentes.db')

# (etiqueta, variable de entorno), en el orden en que se intentan en modo auto.
KEYS = (
    ('principal', 'OPENAI_API_KEY'),
    ('respaldo', 'OPENAI_API_KEY_RESPALDO'),
)
MODOS = ('auto', 'principal', 'respaldo')
MODO_DEFAULT = 'auto'
ENFRIAMIENTO_S = 300

_CLAVE_MODO = 'openai_modo'
_TODOS = '*'  # "modelo" de una pausa que aplica a la cuenta entera

_lock = threading.Lock()
_pausas: dict = {}         # (etiqueta, modelo | _TODOS) → epoch hasta el que está pausada
_ultimo_exito: dict = {}   # etiqueta → ISO de la última respuesta buena
_ultimo_error: dict = {}   # etiqueta → {cuando, modelo, detalle}

T = TypeVar('T')


class SinKeyError(Exception):
    """El modo actual no deja ninguna key utilizable."""


class TodasFallaronError(Exception):
    """Se intentaron todas las keys y todas fallaron."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_configuracion_db():
    """Tabla clave/valor para ajustes globales que el admin cambia en caliente.
    Hoy solo guarda el modo de las keys de OpenAI."""
    conn = sqlite3.connect(AGENTES_DB_PATH)
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS configuracion (
            clave          TEXT PRIMARY KEY,
            valor          TEXT,
            actualizado_en TEXT
        )''')
        conn.commit()
    finally:
        conn.close()


def obtener_modo() -> str:
    # Se lee de SQLite en cada llamada (~0.5ms), igual que el registro de
    # modelos: un cambio desde el admin aplica desde la siguiente consulta.
    conn = sqlite3.connect(AGENTES_DB_PATH)
    try:
        row = conn.execute(
            "SELECT valor FROM configuracion WHERE clave=?", (_CLAVE_MODO,)
        ).fetchone()
    finally:
        conn.close()
    modo = row[0] if row else None
    return modo if modo in MODOS else MODO_DEFAULT


def guardar_modo(modo: str):
    if modo not in MODOS:
        raise ValueError(f"modo inválido: {modo!r}")
    conn = sqlite3.connect(AGENTES_DB_PATH)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO configuracion (clave, valor, actualizado_en) VALUES (?, ?, ?)",
            (_CLAVE_MODO, modo, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    # Cambiar el modo es una intervención explícita del admin (p.ej. ya le
    # cargó saldo a la principal): se arranca sin pausas heredadas.
    with _lock:
        _pausas.clear()


def _valor(variable: str) -> Optional[str]:
    # Se lee en cada llamada y no al importar, porque app.py corre
    # load_dotenv() después de importar este módulo.
    v = (os.getenv(variable) or '').strip()
    return v or None


def configuradas() -> dict:
    return {etiqueta: _valor(variable) is not None for etiqueta, variable in KEYS}


def variable_de(etiqueta: str) -> str:
    return dict(KEYS)[etiqueta]


def candidatas(modelo: str) -> list:
    """[(etiqueta, api_key)] en el orden en que hay que intentarlas para `modelo`.
    Vacía si el modo pide una key que no está configurada, o si no hay ninguna."""
    modo = obtener_modo()
    disponibles = [(etiqueta, _valor(variable)) for etiqueta, variable in KEYS]
    disponibles = [(etiqueta, key) for etiqueta, key in disponibles if key]
    if modo != 'auto':
        return [c for c in disponibles if c[0] == modo]

    ahora = time.time()
    with _lock:
        pausadas = {
            etiqueta for etiqueta, _ in disponibles
            if _pausas.get((etiqueta, _TODOS), 0) > ahora or _pausas.get((etiqueta, modelo), 0) > ahora
        }
    # Las pausadas van al final, no se descartan: si la otra también falla,
    # vale la pena volver a intentar la primera.
    return [c for c in disponibles if c[0] not in pausadas] + [c for c in disponibles if c[0] in pausadas]


def mensaje_sin_keys() -> str:
    modo = obtener_modo()
    if modo == 'auto':
        return (
            "No hay ninguna API key de OpenAI configurada. Agrega OPENAI_API_KEY "
            "(y, si quieres respaldo, OPENAI_API_KEY_RESPALDO) al .env del backend."
        )
    return (
        f"En Administración → API keys está forzada la key '{modo}', pero "
        f"{variable_de(modo)} no está configurada en el .env del backend. "
        "Configúrala o cambia el modo a Automático."
    )


def _error_openai(exc: Optional[BaseException]):
    """El primer error HTTP de OpenAI en la cadena de causas, o None. LangChain
    normalmente deja pasar el original, pero no cuesta nada revisar la cadena."""
    import openai
    for _ in range(5):
        if exc is None:
            return None
        if isinstance(exc, openai.APIStatusError):
            return exc
        exc = exc.__cause__
    return None


def amerita_respaldo(exc: BaseException) -> bool:
    """¿El error es de la key o de la cuenta, de modo que otra key podría no tenerlo?

    Sí: 401 (key inválida o revocada), 403 (sin permiso), 404 (el modelo no
    existe *para esa cuenta*) y 429 (sin saldo o límite de uso). No: 400 (el
    request está mal para cualquier key), 5xx y errores de red, que fallan igual
    con las dos — reintentarlos con otra cuenta solo duplicaría el intento.
    """
    import openai
    err = _error_openai(exc)
    return isinstance(err, (
        openai.AuthenticationError,
        openai.PermissionDeniedError,
        openai.NotFoundError,
        openai.RateLimitError,
    ))


def _es_de_cuenta(exc: BaseException) -> bool:
    """401 y el 429 por saldo agotado afectan a la cuenta entera, no a un modelo.
    El 429 por límite de uso es por modelo (OpenAI mide tokens/min por modelo)."""
    import openai
    err = _error_openai(exc)
    if isinstance(err, openai.AuthenticationError):
        return True
    return isinstance(err, openai.RateLimitError) and getattr(err, 'code', None) == 'insufficient_quota'


def _registrar_falla(etiqueta: str, modelo: str, exc: BaseException):
    alcance = _TODOS if _es_de_cuenta(exc) else modelo
    with _lock:
        _pausas[(etiqueta, alcance)] = time.time() + ENFRIAMIENTO_S
        _ultimo_error[etiqueta] = {
            "cuando": _now(),
            "modelo": modelo,
            # OpenAI ya enmascara la key en sus mensajes (sk-...abcd).
            "detalle": str(exc)[:300],
        }


def ejecutar(modelo: str, llamada: Callable[[str, str], T]) -> tuple:
    """Corre `llamada(etiqueta, api_key)` con cada key candidata hasta que una
    responda. Devuelve (resultado, etiqueta de la key que respondió).

    Un error que no es de la key (ver amerita_respaldo) se propaga de inmediato.
    """
    lista = candidatas(modelo)
    if not lista:
        raise SinKeyError(mensaje_sin_keys())

    fallas = []
    for i, (etiqueta, api_key) in enumerate(lista):
        try:
            resultado = llamada(etiqueta, api_key)
        except Exception as e:
            if not amerita_respaldo(e):
                raise
            _registrar_falla(etiqueta, modelo, e)
            fallas.append((etiqueta, e))
            if i + 1 < len(lista):
                logger.warning(
                    "[OPENAI] La key '%s' falló con %s (%s); reintentando con '%s'.",
                    etiqueta, modelo, type(e).__name__, lista[i + 1][0],
                )
            continue
        with _lock:
            _ultimo_exito[etiqueta] = _now()
        return resultado, etiqueta

    # Con una sola key se propaga el error original, igual que antes de que
    # existiera el respaldo.
    if len(fallas) == 1:
        raise fallas[0][1]
    detalle = ' | '.join(f"{etiqueta}: {e}" for etiqueta, e in fallas)
    raise TodasFallaronError(f"Ninguna API key de OpenAI pudo responder — {detalle}") from fallas[-1][1]


def estado() -> dict:
    """Para el tab "API keys" del admin. Nunca incluye el valor de las keys."""
    ahora = time.time()
    conf = configuradas()
    keys = []
    with _lock:
        for etiqueta, variable in KEYS:
            pausas = [
                {
                    # None = toda la cuenta, no un modelo en particular.
                    "modelo": None if modelo == _TODOS else modelo,
                    "hasta": datetime.fromtimestamp(hasta, timezone.utc).isoformat(),
                }
                for (etq, modelo), hasta in _pausas.items()
                if etq == etiqueta and hasta > ahora
            ]
            keys.append({
                "key": etiqueta,
                "variable": variable,
                "configurada": conf[etiqueta],
                "ultimo_exito": _ultimo_exito.get(etiqueta),
                "ultimo_error": _ultimo_error.get(etiqueta),
                "pausas": pausas,
            })
    return {
        "modo": obtener_modo(),
        "modos": list(MODOS),
        "enfriamiento_s": ENFRIAMIENTO_S,
        "keys": keys,
    }
