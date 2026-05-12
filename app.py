import os
import re
import shutil
import sqlite3
import json
import asyncio
import uuid
import httpx
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import Optional
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
import funciones
import chatbot as asistente
import generacion_aumentada

import herramientas

# Cargar variables de entorno
load_dotenv()

print("="*60, flush=True)
print("[INICIO] APP.PY CARGADO - Si ves esto, el codigo esta actualizado", flush=True)
print("[TEST] Cambio de prueba realizado por Claude - verificar sync con VS Code", flush=True)
print("="*60, flush=True)

# Crear archivo de log para debugging
import logging
from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler('debug_mide.log', maxBytes=10*1024*1024, backupCount=3),
        logging.StreamHandler()
    ]
)

# Evitar ruido de recarga automática en desarrollo
logging.getLogger("watchfiles").setLevel(logging.WARNING)
logging.getLogger("watchfiles.main").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
logger.info("[OK] Logger inicializado correctamente")

# Inicializar bases de datos SQLite
LOG_DB_PATH = os.getenv('LOG_DB_PATH', 'logs.db')
AGENTES_DB_PATH = os.getenv('AGENTES_DB_PATH', 'agentes.db')

_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

def _agentes_connection():
    conn = sqlite3.connect(AGENTES_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _validate_slug(slug: str) -> str:
    s = slug.strip() if slug else ""
    if not _SLUG_PATTERN.match(s):
        raise HTTPException(
            status_code=400,
            detail="slug inválido. Debe matchear ^[a-z][a-z0-9-]{1,63}$ (empieza con letra minúscula, lowercase + dígitos + guiones, 2-64 chars).",
        )
    return s

def _validate_nombre(nombre: str) -> str:
    s = nombre.strip() if nombre else ""
    if not s:
        raise HTTPException(status_code=400, detail="nombre no puede estar vacío.")
    if len(s) > 80:
        raise HTTPException(status_code=400, detail="nombre excede 80 caracteres.")
    return s

def _validate_no_empty(value: str, field_name: str) -> str:
    s = value.strip() if value else ""
    if not s:
        raise HTTPException(status_code=400, detail=f"{field_name} no puede estar vacío.")
    return s

def _validate_historial_max(value: int) -> int:
    if not isinstance(value, int) or value < 0 or value > 50:
        raise HTTPException(status_code=400, detail="historial_max debe ser entero en rango 0-50.")
    return value

def _validate_mensaje_inicial(value: Optional[str]) -> Optional[str]:
    """Saludo personalizado del embed. None/empty/whitespace → None (frontend usa default)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="mensaje_inicial debe ser string.")
    s = value.strip()
    if not s:
        return None
    if len(s) > 500:
        raise HTTPException(status_code=400, detail="mensaje_inicial excede 500 caracteres.")
    return s

def _validate_color(value: Optional[str], field_name: str) -> Optional[str]:
    """Validación mínima: string + longitud <= 9 (acepta #rrggbb y futuro #rrggbbaa).
    Empty/whitespace → None para que el frontend pueda resetear al default."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{field_name} debe ser string.")
    s = value.strip()
    if not s:
        return None
    if len(s) > 9:
        raise HTTPException(status_code=400, detail=f"{field_name} excede 9 caracteres.")
    return s

def _validate_descripcion(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = value.strip()
    if len(s) > 500:
        raise HTTPException(status_code=400, detail="descripcion excede 500 caracteres.")
    return s if s else None

def _validate_proyecto_existe(proyecto_id: str) -> dict:
    """Devuelve el row del proyecto si existe; lanza 400 si no."""
    if not proyecto_id or not proyecto_id.strip():
        raise HTTPException(status_code=400, detail="proyecto_id no puede estar vacío.")
    conn = _agentes_connection()
    try:
        row = conn.execute(
            "SELECT id, slug, nombre, descripcion, creado_en, actualizado_en FROM proyectos WHERE id=?",
            (proyecto_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail=f"proyecto_id '{proyecto_id}' no existe.")
        return dict(row)
    finally:
        conn.close()

def _validate_bc_pertenece_a_proyecto(nombre_chroma: str, proyecto_id: str):
    """Lanza 400 si la BC no existe o pertenece a otro proyecto."""
    conn = _agentes_connection()
    try:
        row = conn.execute(
            "SELECT proyecto_id FROM bases_conocimiento WHERE nombre_chroma=?",
            (nombre_chroma,),
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=400,
                detail=f"La base de conocimiento '{nombre_chroma}' no está registrada (no existe o no fue creada con un proyecto asociado).",
            )
        if row["proyecto_id"] != proyecto_id:
            raise HTTPException(
                status_code=400,
                detail=f"La base de conocimiento '{nombre_chroma}' no pertenece al proyecto '{proyecto_id}'.",
            )
    finally:
        conn.close()

def require_admin(authorization: Optional[str] = Header(default=None)) -> bool:
    """Dependencia FastAPI: exige `Authorization: Bearer <ADMIN_PASSWORD>`.
    El password se compara contra `ADMIN_PASSWORD` del .env del server.
    Levanta 401 si falta header, formato malo o token incorrecto.
    Levanta 500 si el server no tiene `ADMIN_PASSWORD` configurado (mejor
    fallar ruidosamente que abrirse a todo el mundo)."""
    expected = os.getenv("ADMIN_PASSWORD")
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_PASSWORD no configurado en server")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token admin requerido")
    token = authorization[len("Bearer "):].strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="Token admin inválido")
    return True


def init_log_db():
    conn = sqlite3.connect(LOG_DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha TEXT,
        sesion TEXT,
        ambiente TEXT,
        modelo TEXT,
        contexto TEXT,
        pregunta TEXT,
        historial TEXT,
        respuesta TEXT,
        ms INTEGER,
        error TEXT,
        agente_id TEXT,
        tokens_input INTEGER,
        tokens_output INTEGER
    )''')
    # Migración idempotente para deploys con schema previo.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(chat_logs)").fetchall()}
    for col, typ in (('agente_id', 'TEXT'), ('tokens_input', 'INTEGER'), ('tokens_output', 'INTEGER')):
        if col not in existing:
            conn.execute(f'ALTER TABLE chat_logs ADD COLUMN {col} {typ}')
    # Índices para queries del dashboard de Consumo (rango de fechas + group by agente).
    conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_logs_fecha ON chat_logs(fecha)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_logs_agente_id ON chat_logs(agente_id)')
    conn.commit()
    conn.close()

def _init_agentes_meta():
    """Tabla _meta en agentes.db para flags de migración one-shot."""
    conn = sqlite3.connect(AGENTES_DB_PATH)
    conn.execute('CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT)')
    conn.commit()
    conn.close()

def migrate_agentes_v2():
    """One-shot: dropear la tabla agentes vieja (sin proyecto_id) y dejar que
    init_agentes_db la recree con el nuevo schema. Tracked vía _meta para que
    no corra en cada arranque y no se evapore data nueva en restores.

    El usuario decidió explícitamente no migrar datos viejos al introducir el
    concepto de Proyectos — cualquier fila previa se borra acá."""
    conn = sqlite3.connect(AGENTES_DB_PATH)
    try:
        flag = conn.execute(
            "SELECT value FROM _meta WHERE key='agentes_v2_proyectos_migration_done'"
        ).fetchone()
        if flag and flag[0] == 'true':
            return

        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agentes'"
        ).fetchone()

        if existing is not None:
            count = conn.execute("SELECT COUNT(*) FROM agentes").fetchone()[0]
            if count > 0:
                logger.warning(
                    f"[MIGRATION agentes_v2] La tabla agentes tiene {count} fila(s) "
                    "que se van a eliminar. El usuario decidió no migrar al "
                    "introducir Proyectos. Si esto es inesperado (restore de backup), "
                    "detén el servidor y respalda agentes.db antes de reiniciar."
                )
            else:
                logger.info("[MIGRATION agentes_v2] Dropeando tabla agentes vacía para recrear con nuevo schema.")
            conn.execute('DROP TABLE IF EXISTS agentes')

        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
            ('agentes_v2_proyectos_migration_done', 'true'),
        )
        conn.commit()
    finally:
        conn.close()

def init_agentes_db():
    conn = sqlite3.connect(AGENTES_DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS agentes (
        id                TEXT PRIMARY KEY,
        slug              TEXT NOT NULL UNIQUE,
        nombre            TEXT NOT NULL,
        instrucciones     TEXT NOT NULL,
        contexto          TEXT,
        modelo_llm        TEXT NOT NULL,
        historial_max     INTEGER NOT NULL DEFAULT 5,
        proyecto_id       TEXT NOT NULL,
        creado_en         TEXT NOT NULL,
        actualizado_en    TEXT NOT NULL,
        color_primario    TEXT,
        color_burbuja_bot TEXT,
        color_fondo_chat  TEXT,
        color_header      TEXT,
        mensaje_inicial   TEXT
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_agentes_slug ON agentes(slug)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_agentes_proyecto ON agentes(proyecto_id)')

    # Migración 1: agregar columnas opcionales si faltan (deploys con schema previo).
    cols_info = {row[1]: row for row in conn.execute("PRAGMA table_info(agentes)").fetchall()}
    for col in ('color_primario', 'color_burbuja_bot', 'color_fondo_chat', 'color_header', 'mensaje_inicial'):
        if col not in cols_info:
            conn.execute(f'ALTER TABLE agentes ADD COLUMN {col} TEXT')

    # Migración 2: relajar `contexto` a NULLABLE si en deploys previos quedó NOT NULL.
    # SQLite no soporta ALTER COLUMN para quitar NOT NULL → rebuild de tabla.
    # NULLIF(contexto,'') limpia los '' que dejaba el endpoint borrarContexto?force=true
    # antes de existir esta columna nullable.
    contexto_col = cols_info.get('contexto')
    if contexto_col and contexto_col[3] == 1:  # notnull flag
        logger.info("[MIGRATION] Relajando agentes.contexto a NULLABLE (rebuild de tabla, '' -> NULL).")
        conn.execute('''CREATE TABLE agentes__new (
            id                TEXT PRIMARY KEY,
            slug              TEXT NOT NULL UNIQUE,
            nombre            TEXT NOT NULL,
            instrucciones     TEXT NOT NULL,
            contexto          TEXT,
            modelo_llm        TEXT NOT NULL,
            historial_max     INTEGER NOT NULL DEFAULT 5,
            proyecto_id       TEXT NOT NULL,
            creado_en         TEXT NOT NULL,
            actualizado_en    TEXT NOT NULL,
            color_primario    TEXT,
            color_burbuja_bot TEXT,
            color_fondo_chat  TEXT,
            color_header      TEXT,
            mensaje_inicial   TEXT
        )''')
        conn.execute('''INSERT INTO agentes__new
            SELECT id, slug, nombre, instrucciones, NULLIF(contexto, ''),
                   modelo_llm, historial_max, proyecto_id, creado_en, actualizado_en,
                   color_primario, color_burbuja_bot, color_fondo_chat, color_header,
                   mensaje_inicial
            FROM agentes''')
        conn.execute('DROP TABLE agentes')
        conn.execute('ALTER TABLE agentes__new RENAME TO agentes')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agentes_slug ON agentes(slug)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agentes_proyecto ON agentes(proyecto_id)')

    conn.commit()
    conn.close()

def init_proyectos_db():
    conn = sqlite3.connect(AGENTES_DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS proyectos (
        id             TEXT PRIMARY KEY,
        slug           TEXT NOT NULL UNIQUE,
        nombre         TEXT NOT NULL,
        descripcion    TEXT,
        creado_en      TEXT NOT NULL,
        actualizado_en TEXT NOT NULL
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_proyectos_slug ON proyectos(slug)')
    conn.commit()
    conn.close()

def init_bases_conocimiento_db():
    conn = sqlite3.connect(AGENTES_DB_PATH)
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('''CREATE TABLE IF NOT EXISTS bases_conocimiento (
        nombre_chroma  TEXT PRIMARY KEY,
        proyecto_id    TEXT NOT NULL,
        creado_en      TEXT NOT NULL,
        FOREIGN KEY (proyecto_id) REFERENCES proyectos(id) ON DELETE RESTRICT
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_bc_proyecto ON bases_conocimiento(proyecto_id)')
    conn.commit()
    conn.close()

def cleanup_legacy_agentes_in_logs_db():
    """One-shot cleanup: drops the vestigial 'agentes' table from logs.db left
    over from the previous PR. Tracked via _meta flag so it runs only once
    across server restarts — protege contra restore/rollback que reintroduzca
    la tabla sin que se evapore silenciosamente al siguiente arranque.

    Si la tabla aparece con filas (caso restore desde backup), se loguea un
    warning con el conteo antes de borrar — para que no pase desapercibido.
    chat_logs nunca se toca."""
    conn = sqlite3.connect(LOG_DB_PATH)
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT)')

        flag = conn.execute(
            "SELECT value FROM _meta WHERE key='agentes_legacy_cleanup_done'"
        ).fetchone()
        if flag and flag[0] == 'true':
            return

        legacy_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agentes'"
        ).fetchone()

        if legacy_table is not None:
            count = conn.execute("SELECT COUNT(*) FROM agentes").fetchone()[0]
            if count > 0:
                logger.warning(
                    f"[MIGRATION] logs.db.agentes contiene {count} fila(s). "
                    "Se borrarán por ser vestigios de una PR anterior — la fuente de "
                    "verdad ahora es agentes.db. Si esto es inesperado (por ejemplo, "
                    "venías de un restore), detén el servidor y respalda logs.db antes "
                    "de reiniciar."
                )
            else:
                logger.info("[MIGRATION] Borrando tabla vestigial logs.db.agentes (vacía).")
            conn.execute('DROP TABLE IF EXISTS agentes')

        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
            ('agentes_legacy_cleanup_done', 'true'),
        )
        conn.commit()
    finally:
        conn.close()

init_log_db()
_init_agentes_meta()
migrate_agentes_v2()
init_proyectos_db()
init_bases_conocimiento_db()
init_agentes_db()
cleanup_legacy_agentes_in_logs_db()

app = FastAPI(
    title="Constructor RAG",
    description="Operaciones generales de chatbot incluídas la creación de contextos, carga de documentos e interacción con chatbot.",
    version="0.0.0"
)

# Configurar CORS en base a variable de entorno (lista separada por comas).
# Si llega "*" en la lista, colapsamos a ["*"] (FastAPI/Starlette no acepta "*" mezclado con otros).
allowed_origins_env = os.getenv('CORS_ALLOWED_ORIGINS', '*')
allowed_origins = [origin.strip() for origin in allowed_origins_env.split(',') if origin.strip()]
if not allowed_origins or '*' in allowed_origins:
    allowed_origins = ["*"]

logger.info(f"[CORS] allow_origins={allowed_origins} (source: env CORS_ALLOWED_ORIGINS={allowed_origins_env!r})")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)

# Carpeta temporal para uploads de documentos antes de embeber.
TEMP_FOLDER = os.getenv('TEMP_FOLDER', './_temp')
Path(TEMP_FOLDER).mkdir(parents=True, exist_ok=True)

class ChatRequest(BaseModel):
    pregunta: str
    historial: list[dict] = []
    agente_id: Optional[str] = None
    contexto: Optional[str] = None
    modelo_llm: Optional[str] = None
    instrucciones: Optional[str] = None
    historial_max: Optional[int] = None

class DeleteRequest(BaseModel):
    """
    Modelo Pydantic para el cuerpo de la solicitud DELETE, 
    asegurando que se envíe el 'filename'.
    """
    contexto: str = None 
    filename: str

class LogRequest(BaseModel):
    fecha: str
    sesion: str
    ambiente: str
    modelo: str
    contexto: str
    pregunta: str
    historial: str
    respuesta: str
    ms: int
    error: Optional[str] = None
    agente_id: Optional[str] = None
    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None

class AgenteCreate(BaseModel):
    slug: str
    nombre: str
    instrucciones: str
    contexto: Optional[str] = None
    modelo_llm: str
    historial_max: int = 5
    proyecto_id: str
    color_primario: Optional[str] = None
    color_burbuja_bot: Optional[str] = None
    color_fondo_chat: Optional[str] = None
    color_header: Optional[str] = None
    mensaje_inicial: Optional[str] = None

class AgenteUpdate(BaseModel):
    nombre: Optional[str] = None
    instrucciones: Optional[str] = None
    contexto: Optional[str] = None
    modelo_llm: Optional[str] = None
    historial_max: Optional[int] = None
    proyecto_id: Optional[str] = None
    color_primario: Optional[str] = None
    color_burbuja_bot: Optional[str] = None
    color_fondo_chat: Optional[str] = None
    color_header: Optional[str] = None
    mensaje_inicial: Optional[str] = None
    # Sentinels para detectar intentos de modificar campos inmutables
    id: Optional[str] = None
    slug: Optional[str] = None

class Agente(BaseModel):
    id: str
    slug: str
    nombre: str
    instrucciones: str
    contexto: Optional[str] = None
    modelo_llm: str
    historial_max: int
    proyecto_id: str
    creado_en: str
    actualizado_en: str
    color_primario: Optional[str] = None
    color_burbuja_bot: Optional[str] = None
    color_fondo_chat: Optional[str] = None
    color_header: Optional[str] = None
    mensaje_inicial: Optional[str] = None

class ProyectoCreate(BaseModel):
    slug: str
    nombre: str
    descripcion: Optional[str] = None

class ProyectoUpdate(BaseModel):
    nombre: Optional[str] = None
    descripcion: Optional[str] = None
    # Sentinels para detectar intentos de modificar campos inmutables
    id: Optional[str] = None
    slug: Optional[str] = None

class Proyecto(BaseModel):
    id: str
    slug: str
    nombre: str
    descripcion: Optional[str]
    creado_en: str
    actualizado_en: str

@app.get("/listarContextos",
         tags=["Contextos"])
def listar_contextos(proyecto_id: Optional[str] = None):
    """
    Lista todos los contextos del chatbot. Cada entrada incluye `proyecto_id`
    si la BC está registrada en `bases_conocimiento`. Si se pasa
    `?proyecto_id=...`, filtra a las BCs de ese proyecto (excluye huérfanas).
    """
    try:
        resultado = funciones.listar_contextos_con_conteo()

        # Enriquecer con proyecto_id desde bases_conocimiento
        conn = _agentes_connection()
        try:
            bc_rows = conn.execute(
                "SELECT nombre_chroma, proyecto_id FROM bases_conocimiento"
            ).fetchall()
            mapping = {r["nombre_chroma"]: r["proyecto_id"] for r in bc_rows}
        finally:
            conn.close()

        for nombre, datos in resultado.items():
            datos["proyecto_id"] = mapping.get(nombre)

        if proyecto_id:
            resultado = {n: d for n, d in resultado.items() if d.get("proyecto_id") == proyecto_id}

        return {"Contextos existentes para este chatbot": resultado}
    except Exception as e:
        return {"error": f"Error al listar las colecciones: {e}"}
    
@app.post("/crearContexto",
          tags=["Contextos"])
async def crear_contexto(nombre_contexto: str, embedding_model: str, proyecto_id: str, chunk_size: Optional[int] = None):
    """
    Crea una BC vacía y la registra en `bases_conocimiento` asociada al
    `proyecto_id` indicado. Si `proyecto_id` no existe → 400.
    Si no se especifica chunk_size, se calcula automáticamente como el 80% del
    máximo permitido por el modelo (context_window_tokens × 3 × 0.8).
    """
    try:
        # Validar que el proyecto exista antes de crear nada en Chroma
        _validate_proyecto_existe(proyecto_id)
        # 1. Obtener información del modelo desde Ollama (o definir defaults para OpenAI)
        context_window = 4096  # Default conservador
        
        if not herramientas.es_modelo_openai(embedding_model):
            OLLAMA_URL = os.getenv('OLLAMA_URL', 'http://localhost:11434')
            async with httpx.AsyncClient() as client:
                try:
                    response = await client.post(f"{OLLAMA_URL}/api/show", json={"name": embedding_model}, timeout=5)
                    if response.status_code == 404:
                        raise HTTPException(
                            status_code=400,
                            detail=f"El modelo de embedding '{embedding_model}' no está disponible en Ollama. Descárgalo con: ollama pull {embedding_model}"
                        )
                    if response.status_code != 200:
                        raise HTTPException(
                            status_code=502,
                            detail=f"Ollama respondió con error {response.status_code} al verificar el modelo '{embedding_model}'."
                        )
                    data = response.json()
                    info = data.get("model_info", {})
                    # Intentar obtener el context_length de la arquitectura del modelo
                    arch = info.get("general.architecture", "")
                    context_window = info.get(f"{arch}.context_length") or info.get("adapter.context_length") or 4096
                except HTTPException:
                    raise
                except httpx.RequestError:
                    raise HTTPException(
                        status_code=503,
                        detail=f"No se pudo conectar a Ollama para verificar el modelo '{embedding_model}'. ¿Está corriendo Ollama?"
                    )
        else:
            # Modelos de OpenAI suelen tener límites conocidos
            if "text-embedding-3" in embedding_model:
                context_window = 8191
            else:
                context_window = 8191

        # 2. Calcular límites en caracteres (1 token ≈ 3 caracteres, factor conservador)
        CHUNK_SIZE_MIN = 100
        CHUNK_SIZE_MAX = int(context_window * 3)
        CHUNK_SIZE_SUGERIDO = int(CHUNK_SIZE_MAX * 0.8)

        # Si no se especificó chunk_size, usar el sugerido automáticamente
        if chunk_size is None:
            chunk_size = CHUNK_SIZE_SUGERIDO

        if chunk_size < CHUNK_SIZE_MIN:
            raise HTTPException(
                status_code=400,
                detail=f"El tamaño del chunk ({chunk_size}) es demasiado pequeño. Mínimo permitido: {CHUNK_SIZE_MIN} caracteres."
            )

        if chunk_size > CHUNK_SIZE_MAX:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"El tamaño del chunk ({chunk_size}) excede el máximo recomendado para el modelo '{embedding_model}': "
                    f"{CHUNK_SIZE_MAX} caracteres (ventana de contexto {context_window} tokens × 3)."
                )
            )

        resultado = funciones.crear_contexto(nombre_contexto, embedding_model, chunk_size)

        # Registrar la BC en bases_conocimiento (idempotente: si ya existe, actualizar proyecto_id no — solo insertar nuevas).
        conn = _agentes_connection()
        try:
            existing = conn.execute(
                "SELECT proyecto_id FROM bases_conocimiento WHERE nombre_chroma=?",
                (nombre_contexto,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO bases_conocimiento (nombre_chroma, proyecto_id, creado_en) VALUES (?, ?, ?)",
                    (nombre_contexto, proyecto_id, _now()),
                )
                conn.commit()
            elif existing["proyecto_id"] != proyecto_id:
                logger.warning(
                    f"[BC REGISTER] '{nombre_contexto}' ya estaba registrada con proyecto_id="
                    f"'{existing['proyecto_id']}'. Se ignora el nuevo proyecto_id='{proyecto_id}'. "
                    "Las BCs no cambian de proyecto post-creación."
                )
        finally:
            conn.close()

        return resultado
    except HTTPException:
        raise
    except Exception as e:
        return {"error": f"Error al crear contexto: {e}"}
    

@app.delete("/borrarContexto",
            tags=["Contextos"])
def borrar_contexto(contexto: str, force: bool = False):
    """
    Borra una colección de ChromaDB por su nombre.

    - Por default (force=False): bloquea con 409 si la BC está en uso por
      algún agente.
    - Con `?force=true`: borra de todas formas, y limpia el campo `contexto`
      de los agentes afectados a `""` (la columna es NOT NULL en el schema
      actual; el agente queda válido pero sin RAG hasta que se le asigne otra
      BC).

    También limpia el registro en `bases_conocimiento`. Atomicidad SQL: el
    UPDATE de agentes y el DELETE de bases_conocimiento ocurren en la misma
    transacción (commit único). El delete_collection de Chroma se hace DESPUÉS
    del commit; si Chroma falla post-commit, los agentes ya quedaron
    consistentes y un retry del endpoint completa el cleanup.
    """
    try:
        conn = _agentes_connection()
        try:
            n_agentes = conn.execute(
                "SELECT COUNT(*) FROM agentes WHERE contexto=?", (contexto,)
            ).fetchone()[0]

            if n_agentes > 0 and not force:
                raise HTTPException(
                    status_code=409,
                    detail=f"No se puede borrar la BC '{contexto}': la usan {n_agentes} agente(s). Bórralos primero o muévelos a otra BC.",
                )

            if n_agentes > 0 and force:
                logger.warning(
                    f"[FORCE DELETE BC] Borrando BC '{contexto}' en uso por {n_agentes} "
                    "agente(s). Sus contextos quedarán en '' (sin RAG)."
                )

            # Misma conexión = misma transacción SQLite. commit() al final, o rollback
            # automático al cerrar si algo lanza excepción antes.
            conn.execute(
                "UPDATE agentes SET contexto='', actualizado_en=? WHERE contexto=?",
                (_now(), contexto),
            )
            conn.execute("DELETE FROM bases_conocimiento WHERE nombre_chroma=?", (contexto,))
            conn.commit()
        finally:
            conn.close()

        funciones.delete_contexto(contexto)

        return {"Mensaje": f"Contexto '{contexto}' borrada exitosamente."}
    except HTTPException:
        raise
    except Exception as e:
        return {"error": f"Error al borrar contexto: {e}"}

@app.get("/listarDocumentos",
         tags=["Documentos"],
         description="Lista los documentos que se han integrado a un contexto.", 
         summary="Listar Documentos")
def listar_documentos(contexto: str):
    """
    Endpoint para listar los nombres únicos de los documentos (archivos) 
    agregados a una colección (contexto).
    """
    file_names = funciones.listar_documentos(contexto)

    #Momentaneamente voy a debuguear lo que hay aquí: 
    print("Inicializando degub...")
    herramientas.debug_check_file_hash_storage(contexto)
    
    if isinstance(file_names, str):
        return {f"El contexto {contexto} no existe en base."}
    
    if not file_names:
        # Esto sucede si la colección está vacía o si hubo un error.
        return {"Mensaje": f"El contexto {contexto} está vacío.", "files": []}
        
    return {
        "contexto": contexto,
        "documentos": file_names,
        "conteo": len(file_names)
    }

@app.post("/integrarDocumento",
          tags=["Documentos"],
          description="Carga, divide, vectoriza e integra el documento al contexto elegido.",
          summary="Integrar Documento"
          )
async def integrar_documento(contexto: str, documento: UploadFile = File(...)):
    """
    Endpoint para procesar, dividir, vectorizar e integrar documento al contexto elegido.
    """
    if documento.filename == '':
        raise HTTPException(status_code=400, detail="No se ha seleccionado un archivo")

    # Guardar el archivo temporalmente para procesarlo
    file_path = os.path.join(TEMP_FOLDER, documento.filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(documento.file, buffer)
    
    logger.info("="*60)
    logger.info(f"[IN] ENDPOINT /integrarDocumento recibido")
    logger.info(f"[*] Contexto: {contexto}")
    logger.info(f"[*] Archivo: {documento.filename}")
    logger.info("="*60)
    
    print("="*60, flush=True)
    print(f"[IN] ENDPOINT /integrarDocumento recibido", flush=True)
    print(f"[*] Contexto: {contexto}", flush=True)
    print(f"[*] Archivo: {documento.filename}", flush=True)
    print("="*60, flush=True)
    
    if funciones.existe_contexto(contexto):
        logger.info(f"[OK] Contexto '{contexto}' existe")
        print(f"[OK] Contexto '{contexto}' existe", flush=True)

        #REVISIÓN DE EXISTENCIA PREVIA DE ESOS VECTORES PARA EVITAR DUPLICIDAD
        # 1. Calcular el hash del archivo subido
        current_hash = herramientas.calculate_file_hash(file_path)
        print(f"[*] Hash calculado: {current_hash}", flush=True)

        # 2. Verificar si el contenido ya fue subido
        if herramientas.is_content_duplicate(contexto, current_hash):
            print(f"[AVISO] El archivo {file_path} ya existe en la coleccion. Saltando.", flush=True)
            return {"mensaje": "Éste documento ya había sido integrado previamente."}

        try:
            print(f"[...] Llamando a generacion_aumentada.embed()...", flush=True)
            resultado = await asyncio.to_thread(generacion_aumentada.embed, file_path, contexto, current_hash)
            print(f"[*] Resultado de embed(): {resultado}", flush=True)
            
            if resultado['success']:
                print("Documento integrado exitosamente..")
                return {"mensaje": resultado['message']}
            else:
                print(f"Error al embeber archivo: {resultado['message']}")
                error_detail = f"{resultado['message']}"
                if 'error_details' in resultado:
                    error_detail += f" | Detalles: {resultado['error_details']}"
                raise HTTPException(status_code=500, detail=error_detail)
        except HTTPException:
            # Re-lanzar HTTPException sin modificar
            raise
        except Exception as e:
            print(f"Excepción no manejada en integrar_documento: {e}")
            raise HTTPException(status_code=500, detail=f"Error inesperado: {str(e)}")
        finally:
            # Eliminar el archivo temporal
            os.remove(file_path)
    else: 
        return {"mensaje": f"No existe el contexto {contexto} al que quieres integrar el documento."}


@app.delete("/quitarDocumento",
            tags=["Documentos"],
            description="Retira un documento determinado, borrando ese aprendizaje de ese contexto.",
            summary="Desacoplar Documento")
def borrar_documento(data: DeleteRequest):
    """
    Endpoint para eliminar todos los fragmentos (chunks) asociados a 
    un nombre de archivo (filename) de una colección específica.
    """
    try:
        # FastAPI automáticamente valida el JSON y lo convierte a un objeto DeleteRequest
        deleted_count = funciones.borrar_documento(
            contexto=data.contexto, 
            filename=data.filename  # Acceso a los datos con .filename
        )
        
        print("Archivo borrado...")
        return {"Mensaje": f"Archivo {data.filename} borrado correctamente del contexto: {data.contexto}."}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno al eliminar documentos: {e}")

_AGENTE_COLS = "id, slug, nombre, instrucciones, contexto, modelo_llm, historial_max, proyecto_id, creado_en, actualizado_en, color_primario, color_burbuja_bot, color_fondo_chat, color_header, mensaje_inicial"
_PROYECTO_COLS = "id, slug, nombre, descripcion, creado_en, actualizado_en"

@app.get("/proyectos",
         tags=["Proyectos"],
         description="Lista todos los proyectos, ordenados por fecha de creación descendente.",
         summary="Listar Proyectos")
def listar_proyectos():
    conn = _agentes_connection()
    try:
        rows = conn.execute(
            f"SELECT {_PROYECTO_COLS} FROM proyectos ORDER BY creado_en DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@app.post("/proyectos",
          tags=["Proyectos"],
          status_code=201,
          description="Crea un nuevo proyecto. El proyecto agrupa bases de conocimiento y agentes. Requiere token admin.",
          summary="Crear Proyecto")
def crear_proyecto(body: ProyectoCreate, _: bool = Depends(require_admin)):
    slug = _validate_slug(body.slug)
    nombre = _validate_nombre(body.nombre)
    descripcion = _validate_descripcion(body.descripcion)

    pid = uuid.uuid4().hex
    now = _now()
    conn = _agentes_connection()
    try:
        existing = conn.execute("SELECT id FROM proyectos WHERE slug=?", (slug,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"Ya existe un proyecto con slug '{slug}'.")

        conn.execute(
            f"INSERT INTO proyectos ({_PROYECTO_COLS}) VALUES (?, ?, ?, ?, ?, ?)",
            (pid, slug, nombre, descripcion, now, now),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT {_PROYECTO_COLS} FROM proyectos WHERE id=?",
            (pid,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()

@app.get("/proyectos/{pid}",
         tags=["Proyectos"],
         description="Obtiene un proyecto por su ID.",
         summary="Obtener Proyecto")
def obtener_proyecto(pid: str):
    conn = _agentes_connection()
    try:
        row = conn.execute(
            f"SELECT {_PROYECTO_COLS} FROM proyectos WHERE id=?",
            (pid,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Proyecto con id '{pid}' no encontrado.")
        return dict(row)
    finally:
        conn.close()

@app.put("/proyectos/{pid}",
         tags=["Proyectos"],
         description="Actualiza nombre y/o descripción de un proyecto. id y slug son inmutables. Requiere token admin.",
         summary="Actualizar Proyecto")
def actualizar_proyecto(pid: str, body: ProyectoUpdate, _: bool = Depends(require_admin)):
    if body.id is not None:
        raise HTTPException(status_code=400, detail="id no es modificable.")
    if body.slug is not None:
        raise HTTPException(status_code=400, detail="slug no es modificable.")

    conn = _agentes_connection()
    try:
        row = conn.execute(
            f"SELECT {_PROYECTO_COLS} FROM proyectos WHERE id=?",
            (pid,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Proyecto con id '{pid}' no encontrado.")
        actual = dict(row)

        nombre = actual["nombre"] if body.nombre is None else _validate_nombre(body.nombre)
        descripcion = (
            actual["descripcion"] if body.descripcion is None
            else _validate_descripcion(body.descripcion)
        )

        conn.execute(
            "UPDATE proyectos SET nombre=?, descripcion=?, actualizado_en=? WHERE id=?",
            (nombre, descripcion, _now(), pid),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT {_PROYECTO_COLS} FROM proyectos WHERE id=?",
            (pid,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()

@app.delete("/proyectos/{pid}",
            tags=["Proyectos"],
            status_code=204,
            description="Elimina un proyecto. Bloquea con 409 si tiene agentes o BCs asociados. Requiere token admin.",
            summary="Borrar Proyecto")
def borrar_proyecto(pid: str, _: bool = Depends(require_admin)):
    conn = _agentes_connection()
    try:
        row = conn.execute("SELECT id FROM proyectos WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Proyecto con id '{pid}' no encontrado.")

        n_agentes = conn.execute(
            "SELECT COUNT(*) FROM agentes WHERE proyecto_id=?", (pid,)
        ).fetchone()[0]
        n_bcs = conn.execute(
            "SELECT COUNT(*) FROM bases_conocimiento WHERE proyecto_id=?", (pid,)
        ).fetchone()[0]

        if n_agentes > 0 or n_bcs > 0:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Proyecto tiene {n_agentes} agente(s) y {n_bcs} base(s) de conocimiento, "
                    "bórralos primero o muévelos a otro proyecto."
                ),
            )

        conn.execute("DELETE FROM proyectos WHERE id=?", (pid,))
        conn.commit()
    finally:
        conn.close()

@app.post("/chatbot",
          tags=["Chatbot"])
def chatbot(data: ChatRequest):
    # Resolver bundle del agente si viene agente_id; si no, modo legacy
    base = {}
    if data.agente_id is not None:
        conn = _agentes_connection()
        try:
            row = conn.execute(
                f"SELECT {_AGENTE_COLS} FROM agentes WHERE id=?",
                (data.agente_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Agente {data.agente_id} no encontrado")
            base = dict(row)
        finally:
            conn.close()

    # Precedencia: override del body gana sobre bundle del agente.
    # NOTA: el operador `or` trata "" (string vacío) como ausente, así que un override "" se
    # pisa con el valor del bundle. Si algún día se necesita "anular" un campo del agente
    # desde el request, distinguir None de "" explícitamente (ej. `data.contexto if data.contexto is not None else base.get("contexto")`).
    contexto_efectivo = data.contexto or base.get("contexto")
    modelo_efectivo = data.modelo_llm or base.get("modelo_llm")
    instrucciones_efectivas = data.instrucciones or base.get("instrucciones")
    # Si instrucciones_efectivas queda None, chatbot.py usa el fallback hardcoded del MIDE.
    # TODO(historial_max): la próxima PR que consuma data.historial_max para truncar el
    # historial DEBE validar aquí el rango (entero 0-50). Hoy se recibe sin validar
    # porque no se usa, pero un valor fuera de rango (-5, 9999, str) pasa callando.

    # contexto es opcional ahora: si no hay BC, chatbot.chat() responde en modo "chat puro" (sin RAG).
    if not modelo_efectivo:
        raise HTTPException(status_code=400, detail="modelo_llm es requerido (envíalo en el body o asocia un agente con modelo_llm).")

    print(f"Modelo LLM: {modelo_efectivo}")
    print(f"Contexto: {contexto_efectivo}")
    print(f"Query: {data.pregunta}")
    print(f"Historial: {data.historial}")
    print(f"Agente: {data.agente_id}")

    import time
    start_ts = time.perf_counter()
    text = ""
    tokens_input = None
    tokens_output = None
    error_str = None
    try:
        result = asistente.chat(
            data.pregunta,
            data.historial,
            contexto_efectivo,
            modelo_efectivo,
            instrucciones=instrucciones_efectivas,
        )
        # chat() ahora devuelve dict: éxito con `text`+tokens, o error con `error_message`.
        if isinstance(result, dict):
            if "error_message" in result:
                error_str = result["error_message"]
            else:
                text = result.get("text", "") or ""
                tokens_input = result.get("tokens_input")
                tokens_output = result.get("tokens_output")
        else:
            # Compat: si algún path devolviera string puro, tratarlo como texto.
            text = str(result)
    except Exception as e:
        error_str = str(e)

    elapsed_ms = int((time.perf_counter() - start_ts) * 1000)
    print("Respuesta:", text or f"<error: {error_str}>", f"({elapsed_ms} ms, tokens={tokens_input}/{tokens_output})")

    # Persistir el log con tokens. No bloqueamos la respuesta si esto falla.
    try:
        conn = sqlite3.connect(LOG_DB_PATH)
        conn.execute(
            '''INSERT INTO chat_logs (fecha, sesion, ambiente, modelo, contexto, pregunta, historial, respuesta, ms, error, agente_id, tokens_input, tokens_output)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (_now(), None, None, modelo_efectivo, contexto_efectivo, data.pregunta,
             json.dumps(data.historial, ensure_ascii=False) if data.historial else None,
             text, elapsed_ms, error_str, data.agente_id, tokens_input, tokens_output)
        )
        conn.commit()
        conn.close()
    except Exception as log_err:
        logger.warning(f"[CHATBOT LOG] No se pudo escribir en chat_logs: {log_err}")

    if error_str:
        raise HTTPException(status_code=500, detail=error_str)
    if text:
        return {"Mensaje": text}
    raise HTTPException(status_code=500, detail="Algo salió mal con la consulta.")

@app.get("/agentes",
         tags=["Agentes"],
         description="Lista todos los agentes. Acepta ?proyecto_id=... para filtrar.",
         summary="Listar Agentes")
def listar_agentes(proyecto_id: Optional[str] = None):
    conn = _agentes_connection()
    try:
        if proyecto_id:
            rows = conn.execute(
                f"SELECT {_AGENTE_COLS} FROM agentes WHERE proyecto_id=? ORDER BY creado_en DESC",
                (proyecto_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_AGENTE_COLS} FROM agentes ORDER BY creado_en DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@app.post("/agentes",
          tags=["Agentes"],
          status_code=201,
          description="Crea un agente. Requiere proyecto_id; el contexto referenciado debe ser una BC del mismo proyecto.",
          summary="Crear Agente")
def crear_agente(body: AgenteCreate):
    slug = _validate_slug(body.slug)
    nombre = _validate_nombre(body.nombre)
    instrucciones = _validate_no_empty(body.instrucciones, "instrucciones")
    # contexto opcional: None o "" o "   " significan "sin BC" → guardar NULL.
    contexto_raw = body.contexto.strip() if isinstance(body.contexto, str) else body.contexto
    contexto = contexto_raw if contexto_raw else None
    modelo_llm = _validate_no_empty(body.modelo_llm, "modelo_llm")
    historial_max = _validate_historial_max(body.historial_max)
    color_primario = _validate_color(body.color_primario, "color_primario")
    color_burbuja_bot = _validate_color(body.color_burbuja_bot, "color_burbuja_bot")
    color_fondo_chat = _validate_color(body.color_fondo_chat, "color_fondo_chat")
    color_header = _validate_color(body.color_header, "color_header")
    mensaje_inicial = _validate_mensaje_inicial(body.mensaje_inicial)
    _validate_proyecto_existe(body.proyecto_id)
    if contexto is not None:
        _validate_bc_pertenece_a_proyecto(contexto, body.proyecto_id)

    aid = uuid.uuid4().hex
    now = _now()
    conn = _agentes_connection()
    try:
        existing = conn.execute("SELECT id FROM agentes WHERE slug=?", (slug,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"Ya existe un agente con slug '{slug}'.")

        conn.execute(
            f"INSERT INTO agentes ({_AGENTE_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, slug, nombre, instrucciones, contexto, modelo_llm, historial_max, body.proyecto_id, now, now,
             color_primario, color_burbuja_bot, color_fondo_chat, color_header, mensaje_inicial),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT {_AGENTE_COLS} FROM agentes WHERE id=?",
            (aid,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()

@app.get("/agentes/{aid}",
         tags=["Agentes"],
         description="Obtiene un agente por su ID.",
         summary="Obtener Agente")
def obtener_agente(aid: str):
    conn = _agentes_connection()
    try:
        row = conn.execute(
            f"SELECT {_AGENTE_COLS} FROM agentes WHERE id=?",
            (aid,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Agente con id '{aid}' no encontrado.")
        return dict(row)
    finally:
        conn.close()

@app.api_route("/agentes/{aid}",
               methods=["PUT", "PATCH"],
               tags=["Agentes"],
               description="Actualiza campos del bundle. id y slug son inmutables. Campos no enviados se mantienen (partial update, semántica PATCH/PUT). Si cambias proyecto_id o contexto, se valida la consistencia BC↔proyecto.",
               summary="Actualizar Agente")
def actualizar_agente(aid: str, body: AgenteUpdate):
    if body.id is not None:
        raise HTTPException(status_code=400, detail="id no es modificable.")
    if body.slug is not None:
        raise HTTPException(status_code=400, detail="slug no es modificable.")

    conn = _agentes_connection()
    try:
        row = conn.execute(
            f"SELECT {_AGENTE_COLS} FROM agentes WHERE id=?",
            (aid,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Agente con id '{aid}' no encontrado.")
        actual = dict(row)

        nombre = actual["nombre"] if body.nombre is None else _validate_nombre(body.nombre)
        instrucciones = (
            actual["instrucciones"] if body.instrucciones is None
            else _validate_no_empty(body.instrucciones, "instrucciones")
        )
        # `contexto` necesita distinción explícita "no enviado" vs "enviado como null/empty":
        # null/empty es una solicitud explícita de DEJAR AL ASISTENTE SIN BC.
        # Para el resto de campos NOT NULL, body.X is None ya significa "no enviado" sin
        # ambigüedad porque el cliente nunca debería enviar null para ellos.
        contexto_enviado = 'contexto' in body.model_fields_set
        if contexto_enviado:
            raw = body.contexto.strip() if isinstance(body.contexto, str) else body.contexto
            contexto = raw if raw else None  # None | "" | "   " → None
        else:
            contexto = actual["contexto"]
        modelo_llm = (
            actual["modelo_llm"] if body.modelo_llm is None
            else _validate_no_empty(body.modelo_llm, "modelo_llm")
        )
        historial_max = (
            actual["historial_max"] if body.historial_max is None
            else _validate_historial_max(body.historial_max)
        )
        proyecto_id_efectivo = (
            actual["proyecto_id"] if body.proyecto_id is None else body.proyecto_id
        )
        color_primario = (
            actual["color_primario"] if body.color_primario is None
            else _validate_color(body.color_primario, "color_primario")
        )
        color_burbuja_bot = (
            actual["color_burbuja_bot"] if body.color_burbuja_bot is None
            else _validate_color(body.color_burbuja_bot, "color_burbuja_bot")
        )
        color_fondo_chat = (
            actual["color_fondo_chat"] if body.color_fondo_chat is None
            else _validate_color(body.color_fondo_chat, "color_fondo_chat")
        )
        color_header = (
            actual["color_header"] if body.color_header is None
            else _validate_color(body.color_header, "color_header")
        )
        # `mensaje_inicial`: igual que `contexto`, distinguir "no enviado" (no tocar)
        # de "enviado como null/empty" (resetear a NULL para que el frontend use su default).
        if 'mensaje_inicial' in body.model_fields_set:
            mensaje_inicial = _validate_mensaje_inicial(body.mensaje_inicial)
        else:
            mensaje_inicial = actual["mensaje_inicial"]

        # Si proyecto_id cambió, validar que el nuevo exista
        if body.proyecto_id is not None and body.proyecto_id != actual["proyecto_id"]:
            _validate_proyecto_existe(proyecto_id_efectivo)

        # Si cambió contexto o proyecto_id, validar la consistencia BC↔proyecto.
        # Solo si contexto != None: limpiar BC (null) no requiere validación de pertenencia.
        contexto_cambio = contexto_enviado and contexto != actual["contexto"]
        proyecto_cambio = body.proyecto_id is not None and proyecto_id_efectivo != actual["proyecto_id"]
        if (contexto_cambio or proyecto_cambio) and contexto is not None:
            _validate_bc_pertenece_a_proyecto(contexto, proyecto_id_efectivo)

        conn.execute(
            "UPDATE agentes SET nombre=?, instrucciones=?, contexto=?, modelo_llm=?, historial_max=?, proyecto_id=?, color_primario=?, color_burbuja_bot=?, color_fondo_chat=?, color_header=?, mensaje_inicial=?, actualizado_en=? WHERE id=?",
            (nombre, instrucciones, contexto, modelo_llm, historial_max, proyecto_id_efectivo,
             color_primario, color_burbuja_bot, color_fondo_chat, color_header, mensaje_inicial, _now(), aid),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT {_AGENTE_COLS} FROM agentes WHERE id=?",
            (aid,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()

@app.delete("/agentes/{aid}",
            tags=["Agentes"],
            status_code=204,
            description="Elimina un agente por su ID.",
            summary="Borrar Agente")
def borrar_agente(aid: str):
    conn = _agentes_connection()
    try:
        cur = conn.execute("DELETE FROM agentes WHERE id=?", (aid,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Agente con id '{aid}' no encontrado.")
    finally:
        conn.close()

@app.post("/registrarLog",
          tags=["Logs"],
          description="Registra un log de conversación en la base de datos SQLite.",
          summary="Registrar Log")
def registrar_log(data: LogRequest):
    try:
        conn = sqlite3.connect(LOG_DB_PATH)
        conn.execute(
            '''INSERT INTO chat_logs (fecha, sesion, ambiente, modelo, contexto, pregunta, historial, respuesta, ms, error, agente_id, tokens_input, tokens_output)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (data.fecha, data.sesion, data.ambiente, data.modelo, data.contexto,
             data.pregunta, data.historial, data.respuesta, data.ms, data.error,
             data.agente_id, data.tokens_input, data.tokens_output)
        )
        conn.commit()
        conn.close()
        print(f"[OK] Log registrado para sesion: {data.sesion}")
        return {"Mensaje": "Log registrado correctamente."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al registrar log: {e}")

# Precios OpenAI por 1M tokens (input/output, USD).
# Valores placeholder según el handoff — VERIFICAR contra pricing oficial al desplegar.
_OPENAI_PRICING = {
    'gpt-5':       {'input': 1.25, 'output': 10.00},
    'gpt-5-mini':  {'input': 0.25, 'output':  2.00},
    'gpt-5-nano':  {'input': 0.05, 'output':  0.40},
    'gpt-4o':      {'input': 2.50, 'output': 10.00},
    'gpt-4o-mini': {'input': 0.15, 'output':  0.60},
}


def _pricing_for(modelo: Optional[str]):
    """Devuelve el entry de _OPENAI_PRICING que matchea el modelo (longest-prefix).
    Soporta nombres con sufijo (ej. 'gpt-4o-2024-08-06' → 'gpt-4o', 'gpt-4o-mini' → 'gpt-4o-mini')."""
    if not modelo:
        return None
    m = modelo.lower()
    # Longest-prefix match para que 'gpt-4o-mini' no caiga en 'gpt-4o'.
    best = None
    for key, pricing in _OPENAI_PRICING.items():
        if m.startswith(key) and (best is None or len(key) > len(best[0])):
            best = (key, pricing)
    return best[1] if best else None


@app.get("/consumo/resumen",
         tags=["Consumo"],
         description="Métricas agregadas del periodo (por defecto últimos 30 días). Fuente: logs.db + agentes.db + Chroma. Pensado para el dashboard de Consumo del admin.",
         summary="Resumen de Consumo")
def consumo_resumen(desde: Optional[str] = None, hasta: Optional[str] = None):
    from datetime import date, timedelta

    today = date.today()
    try:
        hasta_date = date.fromisoformat(hasta) if hasta else today
        desde_date = date.fromisoformat(desde) if desde else (hasta_date - timedelta(days=30))
    except ValueError:
        raise HTTPException(status_code=400, detail="desde/hasta deben tener formato YYYY-MM-DD.")

    desde_str = desde_date.isoformat()
    hasta_str = hasta_date.isoformat()
    # Rango inclusivo: [desde 00:00, hasta+1día 00:00). Funciona con timestamps ISO
    # gracias al orden lexicográfico.
    upper_exclusive = (hasta_date + timedelta(days=1)).isoformat()

    conn = sqlite3.connect(LOG_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        total_row = conn.execute(
            "SELECT COUNT(*) AS c FROM chat_logs WHERE fecha >= ? AND fecha < ?",
            (desde_str, upper_exclusive),
        ).fetchone()
        llamadas_total = total_row["c"]

        errores_row = conn.execute(
            "SELECT COUNT(*) AS c FROM chat_logs WHERE fecha >= ? AND fecha < ? AND error IS NOT NULL AND error != ''",
            (desde_str, upper_exclusive),
        ).fetchone()
        errores_total = errores_row["c"]

        lat_row = conn.execute(
            "SELECT AVG(ms) AS a FROM chat_logs WHERE fecha >= ? AND fecha < ? AND (error IS NULL OR error = '') AND ms IS NOT NULL",
            (desde_str, upper_exclusive),
        ).fetchone()
        latencia_promedio_ms = int(lat_row["a"]) if lat_row["a"] is not None else None

        dia_rows = conn.execute(
            "SELECT substr(fecha, 1, 10) AS dia, COUNT(*) AS c FROM chat_logs WHERE fecha >= ? AND fecha < ? GROUP BY dia ORDER BY dia ASC",
            (desde_str, upper_exclusive),
        ).fetchall()
        llamadas_por_dia = [{"fecha": r["dia"], "count": r["c"]} for r in dia_rows]

        agente_rows = conn.execute(
            """SELECT agente_id,
                      COUNT(*) AS total,
                      SUM(CASE WHEN error IS NOT NULL AND error != '' THEN 1 ELSE 0 END) AS errores,
                      AVG(CASE WHEN (error IS NULL OR error = '') THEN ms END) AS lat
               FROM chat_logs
               WHERE fecha >= ? AND fecha < ?
               GROUP BY agente_id
               ORDER BY total DESC""",
            (desde_str, upper_exclusive),
        ).fetchall()

        token_rows = conn.execute(
            """SELECT modelo,
                      COALESCE(SUM(tokens_input), 0) AS ti,
                      COALESCE(SUM(tokens_output), 0) AS toc
               FROM chat_logs
               WHERE fecha >= ? AND fecha < ?
                 AND (tokens_input IS NOT NULL OR tokens_output IS NOT NULL)
               GROUP BY modelo""",
            (desde_str, upper_exclusive),
        ).fetchall()
    finally:
        conn.close()

    # JOIN manual con agentes (BD distinta) para slug/nombre.
    conn_ag = _agentes_connection()
    try:
        agentes_map = {
            r["id"]: {"slug": r["slug"], "nombre": r["nombre"]}
            for r in conn_ag.execute("SELECT id, slug, nombre FROM agentes").fetchall()
        }
    finally:
        conn_ag.close()

    llamadas_por_asistente = []
    for r in agente_rows:
        info = agentes_map.get(r["agente_id"], {"slug": "<borrado>", "nombre": "<borrado>"})
        llamadas_por_asistente.append({
            "slug": info["slug"],
            "nombre": info["nombre"],
            "count": r["total"],
            "errores": r["errores"] or 0,
            "latencia_promedio_ms": int(r["lat"]) if r["lat"] is not None else None,
        })

    # Tokens OpenAI: agregamos por modelo y calculamos costo con tabla local.
    por_modelo = []
    tokens_input_total = 0
    tokens_output_total = 0
    costo_total = 0.0
    for r in token_rows:
        modelo = r["modelo"] or ""
        ti = int(r["ti"] or 0)
        to = int(r["toc"] or 0)
        if ti == 0 and to == 0:
            continue
        pricing = _pricing_for(modelo)
        if pricing is None:
            # Modelos no-OpenAI (Ollama) o no reconocidos: no van al breakdown ni al total.
            continue
        costo = (ti / 1_000_000) * pricing['input'] + (to / 1_000_000) * pricing['output']
        por_modelo.append({
            "modelo": modelo,
            "input": ti,
            "output": to,
            "costo_usd_estimado": round(costo, 4),
        })
        tokens_input_total += ti
        tokens_output_total += to
        costo_total += costo

    tokens_openai = {
        "input": tokens_input_total,
        "output": tokens_output_total,
        "costo_usd_estimado": round(costo_total, 4),
        "por_modelo": por_modelo,
    }

    # Documentos: contar BCs de Chroma y sumar archivos vía funciones.listar_documentos.
    total_bcs = 0
    total_documentos = 0
    try:
        import chromadb
        chroma_client = chromadb.PersistentClient(path=os.getenv('CHROMA_PATH', 'chroma'))
        bcs = chroma_client.list_collections()
        total_bcs = len(bcs)
        for col in bcs:
            try:
                docs = funciones.listar_documentos(col.name)
                if isinstance(docs, list):
                    total_documentos += len(docs)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"[CONSUMO] No se pudo contar BCs/documentos en Chroma: {e}")

    return {
        "rango": {"desde": desde_str, "hasta": hasta_str},
        "llamadas_chatbot_total": llamadas_total,
        "llamadas_chatbot_por_dia": llamadas_por_dia,
        "errores_total": errores_total,
        "latencia_promedio_ms": latencia_promedio_ms,
        "llamadas_por_asistente": llamadas_por_asistente,
        "tokens_openai": tokens_openai,
        "documentos": {
            "total_bcs": total_bcs,
            "total_documentos": total_documentos,
            # Tamaño en disco no es trivial (Chroma no expone tamaño de archivo
            # original); el handoff permite devolver null.
            "tamano_total_kb": None,
        },
    }


@app.get("/listarModelos",
         tags=["Modelos"],
         description="Consulta Ollama y retorna la lista de modelos descargados localmente.",
         summary="Listar Modelos")
async def listar_modelos():
    OLLAMA_URL = os.getenv('OLLAMA_URL', 'http://localhost:11434')
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{OLLAMA_URL}/api/tags", timeout=10)
            response.raise_for_status()
            modelos = [m['name'] for m in response.json().get('models', [])]
            return {"modelos": modelos}
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"No se pudo conectar a Ollama: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al consultar Ollama: {e}")

@app.get("/infoModelo/{modelo:path}",
         tags=["Modelos"],
         description="Retorna los detalles de un modelo de Ollama (arquitectura, parámetros, etc.).",
         summary="Info de Modelo")
async def info_modelo(modelo: str):
    OLLAMA_URL = os.getenv('OLLAMA_URL', 'http://localhost:11434')
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{OLLAMA_URL}/api/show", json={"name": modelo}, timeout=10)
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Modelo '{modelo}' no encontrado en Ollama.")
            response.raise_for_status()
            data = response.json()
            info = data.get("model_info", {})

            arquitectura = info.get("general.architecture", "desconocida")
            raw_context = info.get(f"{arquitectura}.context_length") or info.get("adapter.context_length")

            context_window_tokens = None
            if isinstance(raw_context, (int, float)):
                context_window_tokens = int(raw_context)
            elif isinstance(raw_context, str):
                try:
                    context_window_tokens = int(raw_context)
                except ValueError:
                    context_window_tokens = None

            chunk_size_max = int(context_window_tokens * 3) if context_window_tokens else None
            chunk_size_sugerido = int(chunk_size_max * 0.8) if chunk_size_max else None

            return {
                "modelo": modelo,
                "arquitectura":  arquitectura,
                "parametros":    info.get("general.parameter_count", "desconocido"),
                "contexto_max":  raw_context or "desconocido",
                "chunk_size_max": chunk_size_max or "desconocido",
                "chunk_size_sugerido": chunk_size_sugerido or "desconocido",
                "tipo":          "embedding" if "embed" in modelo.lower() else "llm",
            }
    except HTTPException:
        raise
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"No se pudo conectar a Ollama: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al consultar info del modelo: {e}")

@app.delete("/borrarModelo",
            tags=["Modelos"],
            description="Borra un modelo de Ollama vía su API nativa (DELETE /api/delete). No filtra nombres ni bloquea si hay asistentes usándolo: el admin confirma en el frontend. Requiere token admin.",
            summary="Borrar Modelo")
async def borrar_modelo(nombre: str, _: bool = Depends(require_admin)):
    OLLAMA_URL = os.getenv('OLLAMA_URL', 'http://localhost:11434')
    try:
        # Timeout más holgado que listar/info: el delete remueve archivos de disco
        # y puede tardar varios segundos en modelos grandes.
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.request(
                "DELETE", f"{OLLAMA_URL}/api/delete", json={"name": nombre}
            )
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Modelo '{nombre}' no encontrado en Ollama.")
            response.raise_for_status()
            return {"Mensaje": f"Modelo '{nombre}' borrado."}
    except HTTPException:
        raise
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"No se pudo conectar a Ollama: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al borrar modelo: {e}")

@app.get("/health",
         tags=["Utilidad"],
         description="Verifica que el servidor esté en línea.",
         summary="Health Check")
def health():
    return {"status": "ok", "mensaje": "Servidor en línea"}


@app.post("/admin/verify",
          tags=["Admin"],
          description="Valida el token admin (Authorization: Bearer <token>). El frontend lo llama al pegar la URL con el param para confirmar antes de persistirlo en localStorage.",
          summary="Verificar token admin")
def admin_verify(_: bool = Depends(require_admin)):
    return {"ok": True}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8077)