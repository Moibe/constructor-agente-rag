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
from fastapi.responses import FileResponse
import mimetypes
import funciones
import modelos as registro_modelos
import usuarios as registro_usuarios
import hitos as registro_hitos
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

def _validate_top_k(value: int) -> int:
    """Cuántos chunks recuperar de Chroma por consulta. 1 = comportamiento histórico
    (suficiente para FAQs autocontenidos). 3-5 mejor para PDFs informativos. Cap a
    20 para no abusar del contexto del LLM."""
    if not isinstance(value, int) or value < 1 or value > 20:
        raise HTTPException(status_code=400, detail="top_k debe ser entero en rango 1-20.")
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

def _validate_doc_path_part(value: str, field: str) -> str:
    """Sanitiza una parte de path (contexto o filename) que se va a concatenar al
    DOCS_FOLDER. Rechaza separadores de path, '..' y null bytes para evitar
    path traversal. NO sanitiza silenciosamente — devuelve 400 si detecta algo."""
    if not value:
        raise HTTPException(status_code=400, detail=f"{field} es requerido.")
    if '/' in value or '\\' in value or '..' in value or '\x00' in value:
        raise HTTPException(status_code=400, detail=f"{field} contiene caracteres no permitidos.")
    return value


def _validate_proyecto_password(value: Optional[str]) -> Optional[str]:
    """Password de proyecto: string trimmed, len <= 120. Empty/whitespace → None
    (semánticamente "sin password")."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="password debe ser string.")
    s = value.strip()
    if not s:
        return None
    if len(s) > 120:
        raise HTTPException(status_code=400, detail="password excede 120 caracteres.")
    return s


def _proyecto_to_response(row: dict) -> dict:
    """Convierte una fila de `proyectos` a la respuesta pública: elimina el
    campo `password` (NUNCA debe exponerse) y agrega `requires_password: bool`."""
    out = {k: v for k, v in row.items() if k != "password"}
    out["requires_password"] = bool(row.get("password"))
    return out


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
        tokens_output INTEGER,
        proyecto_id TEXT,
        proyecto_slug TEXT,
        asistente_slug TEXT,
        proveedor TEXT,
        costo_usd REAL
    )''')
    # Migración idempotente para deploys con schema previo.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(chat_logs)").fetchall()}
    for col, typ in (
        ('agente_id', 'TEXT'),
        ('tokens_input', 'INTEGER'),
        ('tokens_output', 'INTEGER'),
        ('proyecto_id', 'TEXT'),
        ('proyecto_slug', 'TEXT'),
        ('asistente_slug', 'TEXT'),
        # `costo_usd` se calcula y congela al momento de loguear, con la tarifa
        # vigente en ese instante. Si mañana un proveedor cambia precios, los
        # reportes históricos siguen mostrando lo que realmente costó — antes se
        # multiplicaba al vuelo en /consumo/resumen y un mes del año pasado se
        # reescribía solo con los precios de hoy.
        # NULL significa "no se sabe" (modelo sin tarifa o sin tokens reportados),
        # nunca 0.
        ('proveedor', 'TEXT'),
        ('costo_usd', 'REAL'),
        # Usuario final identificado (no autenticado) vía `?usuario=<slug>` en el
        # widget. Denormalizado igual que proyecto_slug/asistente_slug: si el
        # usuario se renombra o se borra del registro después, el log histórico
        # sigue mostrando cómo se llamaba en el momento de la consulta.
        # NULL = anónimo (no venía identificado, o el slug no matcheó ninguno).
        ('usuario_id', 'TEXT'),
        ('usuario_slug', 'TEXT'),
        ('usuario_nombre', 'TEXT'),
        # Desglose de `ms` (latencia total): cuánto de ese total fue búsqueda
        # en Chroma vs. la llamada al LLM. NULL, no 0 — ms_rag no aplica si el
        # agente no tiene BC, y ambos quedan NULL si truena antes de invocar
        # el LLM (ver chatbot.chat()).
        ('ms_rag', 'INTEGER'),
        ('ms_llm', 'INTEGER'),
    ):
        if col not in existing:
            conn.execute(f'ALTER TABLE chat_logs ADD COLUMN {col} {typ}')
    # Índices para queries del dashboard de Consumo y de Registros
    # (rango por fecha, group by agente, filtros por proyecto/asistente slug).
    conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_logs_fecha ON chat_logs(fecha)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_logs_agente_id ON chat_logs(agente_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_logs_proyecto_slug ON chat_logs(proyecto_slug)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_logs_asistente_slug ON chat_logs(asistente_slug)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_logs_usuario_slug ON chat_logs(usuario_slug)')
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
        mensaje_inicial   TEXT,
        top_k             INTEGER NOT NULL DEFAULT 1
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_agentes_slug ON agentes(slug)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_agentes_proyecto ON agentes(proyecto_id)')

    # Migración 1: agregar columnas opcionales si faltan (deploys con schema previo).
    # (col, type_sql) — type_sql incluye DEFAULT cuando aplica para back-fillear
    # filas existentes con un valor seguro.
    cols_info = {row[1]: row for row in conn.execute("PRAGMA table_info(agentes)").fetchall()}
    for col, type_sql in (
        ('color_primario', 'TEXT'),
        ('color_burbuja_bot', 'TEXT'),
        ('color_fondo_chat', 'TEXT'),
        ('color_header', 'TEXT'),
        ('mensaje_inicial', 'TEXT'),
        ('top_k', 'INTEGER NOT NULL DEFAULT 1'),
    ):
        if col not in cols_info:
            conn.execute(f'ALTER TABLE agentes ADD COLUMN {col} {type_sql}')

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
            mensaje_inicial   TEXT,
            top_k             INTEGER NOT NULL DEFAULT 1
        )''')
        conn.execute('''INSERT INTO agentes__new
            SELECT id, slug, nombre, instrucciones, NULLIF(contexto, ''),
                   modelo_llm, historial_max, proyecto_id, creado_en, actualizado_en,
                   color_primario, color_burbuja_bot, color_fondo_chat, color_header,
                   mensaje_inicial, top_k
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
        actualizado_en TEXT NOT NULL,
        password       TEXT
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_proyectos_slug ON proyectos(slug)')
    # Migración idempotente: agregar columna password si falta (deploys previos).
    existing = {row[1] for row in conn.execute("PRAGMA table_info(proyectos)").fetchall()}
    if 'password' not in existing:
        conn.execute('ALTER TABLE proyectos ADD COLUMN password TEXT')
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
registro_modelos.init_modelos_db()
registro_usuarios.init_usuarios_db()
registro_hitos.init_hitos_db()
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

# Carpeta donde se persisten los binarios originales subidos vía /integrarDocumento,
# para servirlos de vuelta desde /obtenerDocumento. Estructura:
#   <DOCS_FOLDER>/<contexto>/<filename>
DOCS_FOLDER = os.getenv('DOCS_FOLDER', './data/documentos')
Path(DOCS_FOLDER).mkdir(parents=True, exist_ok=True)

class ChatRequest(BaseModel):
    pregunta: str
    historial: list[dict] = []
    agente_id: Optional[str] = None
    contexto: Optional[str] = None
    modelo_llm: Optional[str] = None
    instrucciones: Optional[str] = None
    historial_max: Optional[int] = None
    top_k: Optional[int] = None
    # Slug del usuario final identificado (no autenticado) por el widget vía
    # `?usuario=<slug>` + localStorage. Si no matchea ningún usuario del
    # proyecto del agente, la consulta se loguea como anónima — nunca se
    # rechaza el chat por esto.
    usuario_slug: Optional[str] = None

class SnippetRequest(BaseModel):
    filename: str
    contenido: str


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
    top_k: int = 1

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
    top_k: Optional[int] = None
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
    top_k: int = 1

class ProyectoCreate(BaseModel):
    slug: str
    nombre: str
    descripcion: Optional[str] = None
    password: Optional[str] = None

class ProyectoUpdate(BaseModel):
    nombre: Optional[str] = None
    descripcion: Optional[str] = None
    password: Optional[str] = None
    # Sentinels para detectar intentos de modificar campos inmutables
    id: Optional[str] = None
    slug: Optional[str] = None

class Proyecto(BaseModel):
    # NOTA: `password` NUNCA va en la respuesta; el endpoint público devuelve
    # `requires_password: bool` calculado server-side.
    id: str
    slug: str
    nombre: str
    descripcion: Optional[str]
    creado_en: str
    actualizado_en: str
    requires_password: bool

class VerificarPasswordRequest(BaseModel):
    password: Optional[str] = None

class ModeloCreate(BaseModel):
    nombre: str
    proveedor: str
    # None = "sin tarifa conocida". Distinto de 0.0, que significa "es gratis"
    # (el caso de Ollama, que corre en hardware propio).
    precio_input_usd_1m: Optional[float] = None
    precio_output_usd_1m: Optional[float] = None
    activo: bool = True
    notas: Optional[str] = None

class ModeloUpdate(BaseModel):
    # `nombre` es la PK y es inmutable: cambiarlo dejaría huérfanos los
    # chat_logs históricos y los agentes que ya lo referencian. Para renombrar,
    # crear el nuevo y desactivar el viejo.
    proveedor: Optional[str] = None
    precio_input_usd_1m: Optional[float] = None
    precio_output_usd_1m: Optional[float] = None
    activo: Optional[bool] = None
    notas: Optional[str] = None

class UsuarioCreate(BaseModel):
    proyecto_id: str
    slug: str
    nombre: str
    activo: bool = True
    notas: Optional[str] = None

class UsuarioUpdate(BaseModel):
    # `slug` es inmutable una vez creado: es lo que viaja en la URL que ya
    # mandaste al usuario final y lo que quedó denormalizado en chat_logs
    # históricos. Para renombrar el slug, crear uno nuevo y desactivar este.
    nombre: Optional[str] = None
    activo: Optional[bool] = None
    notas: Optional[str] = None

class HitoCreate(BaseModel):
    nombre: str
    # ISO 8601. Si no se manda, se usa el momento de creación — pero normalmente
    # conviene mandar la fecha real en que el cambio entró en vigor (ej. la
    # fecha del commit/deploy), no la fecha en que se llenó el formulario.
    fecha: Optional[str] = None
    notas: Optional[str] = None

class HitoUpdate(BaseModel):
    nombre: Optional[str] = None
    fecha: Optional[str] = None
    notas: Optional[str] = None

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
                # Persistir el binario original en DOCS_FOLDER/<contexto>/<filename>
                # para servirlo después vía /obtenerDocumento. Si falla, no rompe
                # la respuesta exitosa del embed — solo queda como "histórico sin
                # binario" (404 al servir). El RAG funciona igual.
                try:
                    docs_ctx_dir = Path(DOCS_FOLDER) / contexto
                    docs_ctx_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file_path, docs_ctx_dir / documento.filename)
                except Exception as persist_err:
                    logger.warning(f"[INGESTA] No se pudo persistir binario '{documento.filename}' en {DOCS_FOLDER}/{contexto}: {persist_err}")
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


@app.post("/agregarSnippet",
          tags=["Documentos"],
          description="Añade un snippet de TEXTO PLANO a una BC sin necesidad de subir un PDF. Útil para incorporar una Q&A puntual o una nota corta. El `filename` actúa como identidad del snippet (igual que el filename de un PDF en /integrarDocumento); /listarDocumentos, /quitarDocumento, /obtenerDocumento y /historialDocumentos lo tratan idéntico. Si ya existe un documento con ese filename en el contexto, lo reemplaza.",
          summary="Agregar Snippet de Texto")
def agregar_snippet(contexto: str, body: SnippetRequest):
    _validate_doc_path_part(contexto, "contexto")
    _validate_doc_path_part(body.filename, "filename")

    contenido = body.contenido.strip() if body.contenido else ""
    if not contenido:
        raise HTTPException(status_code=400, detail="contenido no puede estar vacío.")

    if not funciones.existe_contexto(contexto):
        raise HTTPException(status_code=404, detail=f"Contexto '{contexto}' no existe.")

    import hashlib
    current_hash = hashlib.sha256(contenido.encode('utf-8')).hexdigest()

    # Dup detector global por hash de contenido (mismo que /integrarDocumento).
    # Si exactamente este contenido ya existe en la colección con cualquier
    # filename, salimos temprano sin duplicar vectores.
    if herramientas.is_content_duplicate(contexto, current_hash):
        return {"mensaje": "Este snippet ya había sido integrado previamente."}

    # Si ya existe un doc con ESTE filename en el contexto (pero con otro
    # contenido — distinto hash), borramos sus chunks primero para que el
    # filename quede asociado solo al contenido nuevo.
    docs_actuales = funciones.listar_documentos(contexto)
    if isinstance(docs_actuales, list) and body.filename in docs_actuales:
        logger.info(f"[SNIPPET] Reemplazando contenido existente para '{body.filename}' en '{contexto}'.")
        funciones.borrar_documento(contexto=contexto, filename=body.filename)

    resultado = generacion_aumentada.embed_text(contenido, body.filename, contexto, current_hash)
    if not resultado.get('success'):
        detail = resultado.get('message', 'Error al embeber snippet')
        if 'error_details' in resultado:
            detail += f" | Detalles: {resultado['error_details']}"
        raise HTTPException(status_code=500, detail=detail)

    # Persistir el snippet en disco para que /obtenerDocumento y
    # /historialDocumentos lo vean igual que un PDF. Si falla, log warning
    # pero no rompe — el embed ya está hecho.
    try:
        docs_ctx_dir = Path(DOCS_FOLDER) / contexto
        docs_ctx_dir.mkdir(parents=True, exist_ok=True)
        (docs_ctx_dir / body.filename).write_text(contenido, encoding='utf-8')
    except Exception as persist_err:
        logger.warning(f"[SNIPPET] No se pudo persistir snippet en disco: {persist_err}")

    return {"mensaje": resultado['message']}


@app.get("/obtenerDocumento",
         tags=["Documentos"],
         description="Sirve el binario original de un documento ingresado previamente a una BC. Devuelve Content-Disposition inline para que el navegador previsualice PDFs en vez de forzar descarga. Sin auth: el contexto es un nombre interno conocido solo desde el admin.",
         summary="Obtener Documento")
def obtener_documento(contexto: str, filename: str):
    _validate_doc_path_part(contexto, "contexto")
    _validate_doc_path_part(filename, "filename")

    if not funciones.existe_contexto(contexto):
        raise HTTPException(status_code=404, detail=f"Contexto '{contexto}' no existe.")

    file_path = Path(DOCS_FOLDER) / contexto / filename
    # Defense-in-depth: resolver y verificar que el path resuelto siga dentro de DOCS_FOLDER.
    # `_validate_doc_path_part` ya rechaza '..' y separadores, pero un resolve() final
    # protege contra cosas como symlinks que escapan.
    try:
        resolved = file_path.resolve(strict=False)
        base = Path(DOCS_FOLDER).resolve(strict=False)
        if base not in resolved.parents:
            raise HTTPException(status_code=400, detail="filename fuera del directorio permitido.")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="filename inválido.")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"Documento '{filename}' no encontrado en el contexto '{contexto}'.")

    mime, _ = mimetypes.guess_type(filename)
    return FileResponse(
        path=str(file_path),
        media_type=mime or "application/octet-stream",
        headers={
            # `inline` => el navegador previsualiza PDFs en lugar de descargarlos.
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, max-age=3600",
        },
    )


@app.get("/historialDocumentos",
         tags=["Documentos"],
         description="Auditoría: lista TODOS los binarios persistidos en disco bajo DOCS_FOLDER, marcando cada uno como `activo` o `borrado` según si Chroma aún los referencia. También marca `contexto_estado` para distinguir colecciones vivas vs eliminadas. Sin `?contexto` lista todos los contextos. Admin-only. Nota: solo lista archivos que están en disco; docs históricos cargados antes del feature de persistencia no aparecen acá.",
         summary="Historial de Documentos")
def historial_documentos(
    contexto: Optional[str] = None,
    _: bool = Depends(require_admin),
):
    from datetime import datetime, timezone

    if contexto is not None:
        _validate_doc_path_part(contexto, "contexto")

    base = Path(DOCS_FOLDER)
    if not base.is_dir():
        return {"items": [], "total": 0}

    if contexto:
        ctx_path = base / contexto
        ctx_dirs = [ctx_path] if ctx_path.is_dir() else []
    else:
        ctx_dirs = sorted([d for d in base.iterdir() if d.is_dir()], key=lambda p: p.name)

    items = []
    for ctx_dir in ctx_dirs:
        ctx_name = ctx_dir.name
        ctx_exists = funciones.existe_contexto(ctx_name)
        ctx_estado = "activo" if ctx_exists else "borrado"

        # Filenames que Chroma aún reconoce como activos en este contexto.
        # Si el contexto está borrado, este set queda vacío y todos los binarios
        # caen en `borrado`.
        activos_set = set()
        if ctx_exists:
            try:
                docs = funciones.listar_documentos(ctx_name)
                if isinstance(docs, list):
                    activos_set = set(docs)
            except Exception as e:
                logger.warning(f"[HISTORIAL] listar_documentos falló para '{ctx_name}': {e}")

        for f in sorted(ctx_dir.iterdir(), key=lambda p: p.name):
            if not f.is_file():
                continue
            stat = f.stat()
            estado = "activo" if (ctx_exists and f.name in activos_set) else "borrado"
            items.append({
                "contexto": ctx_name,
                "contexto_estado": ctx_estado,
                "filename": f.name,
                "estado": estado,
                "tamano_bytes": stat.st_size,
                # `fecha_modificacion` es el mtime del binario en disco — refleja
                # cuándo se subió/persistió, NO cuándo se borró de Chroma. No hay
                # tracking de "fecha de borrado" hoy; si se necesita, sería otro
                # PR (tabla de eventos o columna en chat_logs).
                "fecha_modificacion": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            })

    return {"items": items, "total": len(items)}


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

_AGENTE_COLS = "id, slug, nombre, instrucciones, contexto, modelo_llm, historial_max, proyecto_id, creado_en, actualizado_en, color_primario, color_burbuja_bot, color_fondo_chat, color_header, mensaje_inicial, top_k"
_PROYECTO_COLS = "id, slug, nombre, descripcion, creado_en, actualizado_en, password"

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
        return [_proyecto_to_response(dict(r)) for r in rows]
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
    password = _validate_proyecto_password(body.password)

    pid = uuid.uuid4().hex
    now = _now()
    conn = _agentes_connection()
    try:
        existing = conn.execute("SELECT id FROM proyectos WHERE slug=?", (slug,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"Ya existe un proyecto con slug '{slug}'.")

        conn.execute(
            f"INSERT INTO proyectos ({_PROYECTO_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, slug, nombre, descripcion, now, now, password),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT {_PROYECTO_COLS} FROM proyectos WHERE id=?",
            (pid,),
        ).fetchone()
        return _proyecto_to_response(dict(row))
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
        return _proyecto_to_response(dict(row))
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
        # `password`: igual que `contexto`/`mensaje_inicial`, distinguir
        # "no enviado" (preservar) de "enviado como null/empty" (quitar password).
        if 'password' in body.model_fields_set:
            password = _validate_proyecto_password(body.password)
        else:
            password = actual["password"]

        conn.execute(
            "UPDATE proyectos SET nombre=?, descripcion=?, password=?, actualizado_en=? WHERE id=?",
            (nombre, descripcion, password, _now(), pid),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT {_PROYECTO_COLS} FROM proyectos WHERE id=?",
            (pid,),
        ).fetchone()
        return _proyecto_to_response(dict(row))
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

@app.post("/proyectos/{pid}/verificar-password",
          tags=["Proyectos"],
          description="Verifica el password de un proyecto. NO requiere token admin: este endpoint lo usan los usuarios normales para entrar a su proyecto. Si el proyecto no tiene password, cualquier valor (o vacío) lo acepta.",
          summary="Verificar Password de Proyecto")
def verificar_password_proyecto(pid: str, body: VerificarPasswordRequest):
    conn = _agentes_connection()
    try:
        row = conn.execute(
            "SELECT password FROM proyectos WHERE id=?", (pid,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Proyecto con id '{pid}' no encontrado.")
        stored = row["password"]
    finally:
        conn.close()

    # Sin password configurado → acceso libre. Equivale al comportamiento previo
    # a este feature, así que clientes viejos no se rompen.
    if not stored:
        return {"ok": True}

    if body.password is not None and body.password == stored:
        return {"ok": True}

    raise HTTPException(status_code=401, detail="Password incorrecto")

@app.post("/chatbot",
          tags=["Chatbot"])
def chatbot(data: ChatRequest):
    # Resolver bundle del agente si viene agente_id; si no, modo legacy.
    # LEFT JOIN con proyectos para tener `proyecto_slug` disponible al loguear
    # (denormalizado en chat_logs para que /registros pueda filtrar sin JOIN).
    base = {}
    if data.agente_id is not None:
        conn = _agentes_connection()
        try:
            row = conn.execute(
                """SELECT a.*, p.slug AS proyecto_slug
                   FROM agentes a LEFT JOIN proyectos p ON a.proyecto_id = p.id
                   WHERE a.id = ?""",
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

    # `top_k`: precedencia body > agente > default 1. Lo validamos solo cuando llega
    # del body (el del agente ya pasó por _validate_top_k al crear/editar).
    if data.top_k is not None:
        top_k_efectivo = _validate_top_k(data.top_k)
    else:
        top_k_efectivo = base.get("top_k") or 1

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
    # Desglose de la latencia total. None = "no aplica/no llegó a esa etapa"
    # (ej. ms_rag en un agente sin BC, o ambos si truena antes de invocar el
    # LLM) — nunca 0, para no leerse como "instantáneo".
    ms_rag = None
    ms_llm = None
    try:
        result = asistente.chat(
            data.pregunta,
            data.historial,
            contexto_efectivo,
            modelo_efectivo,
            instrucciones=instrucciones_efectivas,
            top_k=top_k_efectivo,
        )
        # chat() ahora devuelve dict: éxito con `text`+tokens, o error con `error_message`.
        if isinstance(result, dict):
            if "error_message" in result:
                error_str = result["error_message"]
            else:
                text = result.get("text", "") or ""
                tokens_input = result.get("tokens_input")
                tokens_output = result.get("tokens_output")
                ms_rag = result.get("ms_rag")
                ms_llm = result.get("ms_llm")
        else:
            # Compat: si algún path devolviera string puro, tratarlo como texto.
            text = str(result)
    except Exception as e:
        error_str = str(e)

    elapsed_ms = int((time.perf_counter() - start_ts) * 1000)
    print("Respuesta:", text or f"<error: {error_str}>", f"({elapsed_ms} ms, tokens={tokens_input}/{tokens_output})")

    # Persistir el log con tokens y los slugs denormalizados (para que /registros
    # pueda filtrar por proyecto/asistente sin JOIN). No bloqueamos la respuesta
    # si esto falla.
    # Costo congelado con la tarifa vigente ahora. Si el modelo no está en el
    # registro o no tiene tarifa cargada, costo_usd queda NULL y la UI muestra
    # "sin tarifa" — nunca un número inventado.
    proveedor_efectivo, costo_usd = registro_modelos.calcular_costo(
        modelo_efectivo, tokens_input, tokens_output
    )

    # Usuario final identificado (no autenticado) por el widget. Se resuelve
    # contra el proyecto del agente — un slug de otro proyecto, mal escrito, o
    # de un usuario borrado/desactivado, simplemente no matchea y la consulta
    # se loguea anónima (usuario_id NULL). Nunca se rechaza el chat por esto.
    usuario_fila = registro_usuarios.resolver(base.get("proyecto_id"), data.usuario_slug)
    usuario_id_efectivo = usuario_fila["id"] if usuario_fila else None
    usuario_slug_efectivo = usuario_fila["slug"] if usuario_fila else None
    usuario_nombre_efectivo = usuario_fila["nombre"] if usuario_fila else None

    try:
        conn = sqlite3.connect(LOG_DB_PATH)
        conn.execute(
            '''INSERT INTO chat_logs (fecha, sesion, ambiente, modelo, contexto, pregunta, historial, respuesta, ms, error, agente_id, tokens_input, tokens_output, proyecto_id, proyecto_slug, asistente_slug, proveedor, costo_usd, usuario_id, usuario_slug, usuario_nombre, ms_rag, ms_llm)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (_now(), None, None, modelo_efectivo, contexto_efectivo, data.pregunta,
             json.dumps(data.historial, ensure_ascii=False) if data.historial else None,
             text, elapsed_ms, error_str, data.agente_id, tokens_input, tokens_output,
             base.get("proyecto_id"), base.get("proyecto_slug"), base.get("slug"),
             proveedor_efectivo, costo_usd,
             usuario_id_efectivo, usuario_slug_efectivo, usuario_nombre_efectivo,
             ms_rag, ms_llm)
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
    top_k = _validate_top_k(body.top_k)
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
            f"INSERT INTO agentes ({_AGENTE_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, slug, nombre, instrucciones, contexto, modelo_llm, historial_max, body.proyecto_id, now, now,
             color_primario, color_burbuja_bot, color_fondo_chat, color_header, mensaje_inicial, top_k),
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
        top_k = (
            actual["top_k"] if body.top_k is None
            else _validate_top_k(body.top_k)
        )

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
            "UPDATE agentes SET nombre=?, instrucciones=?, contexto=?, modelo_llm=?, historial_max=?, proyecto_id=?, color_primario=?, color_burbuja_bot=?, color_fondo_chat=?, color_header=?, mensaje_inicial=?, top_k=?, actualizado_en=? WHERE id=?",
            (nombre, instrucciones, contexto, modelo_llm, historial_max, proyecto_id_efectivo,
             color_primario, color_burbuja_bot, color_fondo_chat, color_header, mensaje_inicial, top_k, _now(), aid),
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

# Las tarifas ya no viven acá. El dict `_OPENAI_PRICING` y su `_pricing_for()`
# por longest-prefix se eliminaron: el prefix-match no fallaba ruidosamente,
# mentía ('gpt-5.5'.startswith('gpt-5') → True, así que un modelo de $30/$180
# por 1M se tarifaba a $1.25/$10). Ahora la fuente de verdad es la tabla
# `modelos` (ver modelos.py), con match exacto, y el costo se persiste en
# chat_logs.costo_usd al momento de cada consulta en vez de recalcularse acá.


@app.get("/consumo/resumen",
         tags=["Consumo"],
         description="Métricas agregadas del periodo (por defecto últimos 30 días). Fuente: logs.db + agentes.db + Chroma. Pensado para el dashboard de Consumo del admin.",
         summary="Resumen de Consumo")
def consumo_resumen(desde: Optional[str] = None, hasta: Optional[str] = None, usuario: Optional[str] = None):
    from datetime import date, timedelta, datetime, timezone

    # UTC, igual que /registros. Con date.today() local el rango terminaba antes
    # que el día UTC en curso, así que las consultas de "hoy" hechas después de
    # las 18:00 hora local (=00:00 UTC del día siguiente) quedaban fuera del
    # agregado: la pestaña Consumo mostraba $0.00 con consumo real registrado.
    today = datetime.now(timezone.utc).date()
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

    # Filtro opcional por usuario final (slug). Se anexa al final del WHERE de
    # cada query de abajo — el orden de los AND no importa para el resultado.
    usuario_sql = " AND usuario_slug = ?" if usuario else ""
    usuario_params = [usuario] if usuario else []

    conn = sqlite3.connect(LOG_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        total_row = conn.execute(
            f"SELECT COUNT(*) AS c FROM chat_logs WHERE fecha >= ? AND fecha < ?{usuario_sql}",
            (desde_str, upper_exclusive, *usuario_params),
        ).fetchone()
        llamadas_total = total_row["c"]

        errores_row = conn.execute(
            f"SELECT COUNT(*) AS c FROM chat_logs WHERE fecha >= ? AND fecha < ? AND error IS NOT NULL AND error != ''{usuario_sql}",
            (desde_str, upper_exclusive, *usuario_params),
        ).fetchone()
        errores_total = errores_row["c"]

        lat_row = conn.execute(
            f"SELECT AVG(ms) AS a FROM chat_logs WHERE fecha >= ? AND fecha < ? AND (error IS NULL OR error = '') AND ms IS NOT NULL{usuario_sql}",
            (desde_str, upper_exclusive, *usuario_params),
        ).fetchone()
        latencia_promedio_ms = int(lat_row["a"]) if lat_row["a"] is not None else None

        dia_rows = conn.execute(
            f"SELECT substr(fecha, 1, 10) AS dia, COUNT(*) AS c FROM chat_logs WHERE fecha >= ? AND fecha < ?{usuario_sql} GROUP BY dia ORDER BY dia ASC",
            (desde_str, upper_exclusive, *usuario_params),
        ).fetchall()
        llamadas_por_dia = [{"fecha": r["dia"], "count": r["c"]} for r in dia_rows]

        agente_rows = conn.execute(
            f"""SELECT agente_id,
                      COUNT(*) AS total,
                      SUM(CASE WHEN error IS NOT NULL AND error != '' THEN 1 ELSE 0 END) AS errores,
                      AVG(CASE WHEN (error IS NULL OR error = '') THEN ms END) AS lat
               FROM chat_logs
               WHERE fecha >= ? AND fecha < ?{usuario_sql}
               GROUP BY agente_id
               ORDER BY total DESC""",
            (desde_str, upper_exclusive, *usuario_params),
        ).fetchall()

        # Sumamos el costo YA PERSISTIDO, no lo recalculamos. `costo_usd` puede
        # ser NULL (modelo sin tarifa) mientras los tokens no lo son: por eso
        # `sin_tarifa` cuenta esas filas aparte, para que el total no se lea como
        # completo cuando en realidad hay consumo sin precio conocido.
        token_rows = conn.execute(
            f"""SELECT modelo,
                      proveedor,
                      COALESCE(SUM(tokens_input), 0) AS ti,
                      COALESCE(SUM(tokens_output), 0) AS toc,
                      SUM(costo_usd) AS costo,
                      SUM(CASE WHEN costo_usd IS NULL THEN 1 ELSE 0 END) AS sin_tarifa
               FROM chat_logs
               WHERE fecha >= ? AND fecha < ?
                 AND (tokens_input IS NOT NULL OR tokens_output IS NOT NULL){usuario_sql}
               GROUP BY modelo, proveedor""",
            (desde_str, upper_exclusive, *usuario_params),
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

    # Consumo de IA agregado por modelo. El costo sale de la columna persistida,
    # no de una multiplicación al vuelo.
    #
    # NOTA sobre el nombre `tokens_openai`: se conserva porque el front lo lee en
    # varios lugares, pero ahora agrega TODOS los proveedores (incluido Ollama a
    # $0.00, que antes se excluía del breakdown por no tener entrada de pricing).
    # Cada fila de `por_modelo` trae su `proveedor` para que la UI pueda separarlos.
    por_modelo = []
    tokens_input_total = 0
    tokens_output_total = 0
    costo_total = 0.0
    filas_sin_tarifa_total = 0
    for r in token_rows:
        modelo = r["modelo"] or ""
        ti = int(r["ti"] or 0)
        to = int(r["toc"] or 0)
        if ti == 0 and to == 0:
            continue
        costo = r["costo"]  # None si TODAS las filas del modelo quedaron sin tarifa
        sin_tarifa = int(r["sin_tarifa"] or 0)
        por_modelo.append({
            "modelo": modelo,
            "proveedor": r["proveedor"],
            "input": ti,
            "output": to,
            # None = "sin tarifa conocida". La UI debe mostrar "—", no 0.
            "costo_usd_estimado": round(costo, 6) if costo is not None else None,
            "consultas_sin_tarifa": sin_tarifa,
        })
        tokens_input_total += ti
        tokens_output_total += to
        costo_total += costo or 0.0
        filas_sin_tarifa_total += sin_tarifa

    tokens_openai = {
        "input": tokens_input_total,
        "output": tokens_output_total,
        "costo_usd_estimado": round(costo_total, 6),
        # Cuántas consultas del periodo no pudieron tarifarse. Si es > 0, el total
        # de arriba es un piso, no el gasto completo — la UI debería advertirlo.
        "consultas_sin_tarifa": filas_sin_tarifa_total,
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


@app.get("/registros",
         tags=["Consumo"],
         description="Auditoría detallada de interacciones con /chatbot. Filtros opcionales por rango de fechas, proyecto, asistente, usuario, errores, texto de la pregunta, y mínimos de latencia (rag/llm/total) y tokens. Ordenable con orden_por/orden_dir. Paginación con limit (max 200) y offset. Requiere token admin.",
         summary="Listar Registros")
def listar_registros(
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    proyecto: Optional[str] = None,
    asistente: Optional[str] = None,
    usuario: Optional[str] = None,
    solo_errores: bool = False,
    pregunta_contiene: Optional[str] = None,
    latencia_rag_min: Optional[int] = None,
    latencia_llm_min: Optional[int] = None,
    latencia_min: Optional[int] = None,
    tokens_min: Optional[int] = None,
    orden_por: Optional[str] = None,
    orden_dir: str = 'desc',
    limit: int = 50,
    offset: int = 0,
    _: bool = Depends(require_admin),
):
    from datetime import date, timedelta, datetime, timezone

    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit debe estar entre 1 y 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset debe ser >= 0.")

    # Whitelist de columnas ordenables: el nombre de columna no se puede
    # parametrizar con `?` en SQL, así que se valida contra este mapa fijo en
    # vez de interpolar lo que mande el cliente (evita SQL injection vía
    # nombre de columna). `tokens` no es una columna real: se ordena por la
    # suma de input+output, que es lo que la UI muestra.
    ORDEN_COLUMNAS = {
        'timestamp': 'fecha',
        'proyecto': 'proyecto_slug',
        'asistente': 'asistente_slug',
        'usuario': 'usuario_nombre',
        'pregunta': 'pregunta',
        'latencia': 'ms',
        'latencia_rag': 'ms_rag',
        'latencia_llm': 'ms_llm',
        'tokens': '(COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0))',
    }
    if orden_dir not in ('asc', 'desc'):
        raise HTTPException(status_code=400, detail="orden_dir debe ser 'asc' o 'desc'.")
    if orden_por is not None and orden_por not in ORDEN_COLUMNAS:
        raise HTTPException(
            status_code=400,
            detail=f"orden_por inválido: '{orden_por}'. Válidos: {', '.join(ORDEN_COLUMNAS)}.",
        )
    # Default: más reciente primero, igual que antes de que existiera este parámetro.
    columna_orden = ORDEN_COLUMNAS.get(orden_por, 'fecha')
    orden_sql = f"{columna_orden} {orden_dir.upper()}, id DESC"

    # UTC, no local: los timestamps en chat_logs son UTC, así que el rango
    # default ("últimos 7 días") tiene que estar en la misma zona para no
    # excluir filas de "hoy" cerca de medianoche UTC.
    today = datetime.now(timezone.utc).date()
    try:
        hasta_date = date.fromisoformat(hasta) if hasta else today
        desde_date = date.fromisoformat(desde) if desde else (hasta_date - timedelta(days=7))
    except ValueError:
        raise HTTPException(status_code=400, detail="desde/hasta deben tener formato YYYY-MM-DD.")

    if desde_date > hasta_date:
        raise HTTPException(status_code=400, detail="desde no puede ser posterior a hasta.")

    desde_str = desde_date.isoformat()
    upper_exclusive = (hasta_date + timedelta(days=1)).isoformat()

    # WHERE dinámico: arrancamos con el rango de fechas y agregamos los filtros
    # opcionales. Usar parametrización SQLite (no concatenar strings) para evitar
    # injection.
    where_clauses = ["fecha >= ?", "fecha < ?"]
    where_params = [desde_str, upper_exclusive]
    if proyecto:
        where_clauses.append("proyecto_slug = ?")
        where_params.append(proyecto)
    if asistente:
        where_clauses.append("asistente_slug = ?")
        where_params.append(asistente)
    if usuario:
        where_clauses.append("usuario_slug = ?")
        where_params.append(usuario)
    if solo_errores:
        where_clauses.append("error IS NOT NULL AND error != ''")
    if pregunta_contiene:
        where_clauses.append("pregunta LIKE ?")
        where_params.append(f"%{pregunta_contiene}%")
    if latencia_rag_min is not None:
        where_clauses.append("ms_rag >= ?")
        where_params.append(latencia_rag_min)
    if latencia_llm_min is not None:
        where_clauses.append("ms_llm >= ?")
        where_params.append(latencia_llm_min)
    if latencia_min is not None:
        where_clauses.append("ms >= ?")
        where_params.append(latencia_min)
    if tokens_min is not None:
        where_clauses.append("(COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0)) >= ?")
        where_params.append(tokens_min)

    where_sql = " AND ".join(where_clauses)

    conn = sqlite3.connect(LOG_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM chat_logs WHERE {where_sql}",
            where_params,
        ).fetchone()["c"]

        rows = conn.execute(
            f"""SELECT id, fecha, proyecto_id, proyecto_slug, asistente_slug,
                       pregunta, respuesta, ms, ms_rag, ms_llm, tokens_input, tokens_output,
                       modelo, proveedor, costo_usd, usuario_slug, usuario_nombre, error
                FROM chat_logs
                WHERE {where_sql}
                ORDER BY {orden_sql}
                LIMIT ? OFFSET ?""",
            where_params + [limit, offset],
        ).fetchall()
    finally:
        conn.close()

    items = [
        {
            "id": r["id"],
            "timestamp": r["fecha"],
            "proyecto_id": r["proyecto_id"],
            "proyecto_slug": r["proyecto_slug"],
            "asistente_slug": r["asistente_slug"],
            "pregunta": r["pregunta"],
            "respuesta": r["respuesta"],
            "latencia_ms": r["ms"],
            # Desglose de latencia_ms. null = no aplica (ms_rag en un agente
            # sin BC) o no se alcanzó esa etapa (truena antes del LLM) —
            # nunca 0, la UI debe mostrar "—".
            "latencia_rag_ms": r["ms_rag"],
            "latencia_llm_ms": r["ms_llm"],
            "tokens_in": r["tokens_input"],
            "tokens_out": r["tokens_output"],
            "modelo": r["modelo"],
            "proveedor": r["proveedor"],
            # Leído de la columna, no recalculado: es la tarifa que estaba
            # vigente cuando ocurrió la consulta. null = sin tarifa conocida,
            # la UI debe mostrar "—" y no 0.
            "costo_usd": r["costo_usd"],
            # Denormalizado al momento de la consulta (ver /chatbot). null =
            # anónimo: no venía identificado o el slug no matcheó ningún
            # usuario del proyecto.
            "usuario_slug": r["usuario_slug"],
            "usuario_nombre": r["usuario_nombre"],
            "error": r["error"],
        }
        for r in rows
    ]

    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "rango": {"desde": desde_date.isoformat(), "hasta": hasta_date.isoformat()},
    }


def _modelo_to_response(row: dict) -> dict:
    return {
        "nombre": row["nombre"],
        "proveedor": row["proveedor"],
        "precio_input_usd_1m": row["precio_input_usd_1m"],
        "precio_output_usd_1m": row["precio_output_usd_1m"],
        "activo": bool(row["activo"]),
        "notas": row["notas"],
        "actualizado_en": row["actualizado_en"],
    }

def _validate_proveedor(proveedor: str) -> str:
    p = (proveedor or "").strip().lower()
    if p not in registro_modelos.PROVEEDORES_VALIDOS:
        raise HTTPException(
            status_code=400,
            detail=f"proveedor inválido: '{proveedor}'. Válidos: {', '.join(registro_modelos.PROVEEDORES_VALIDOS)}.",
        )
    return p

def _validate_precio(valor: Optional[float], campo: str) -> Optional[float]:
    if valor is None:
        return None
    if not isinstance(valor, (int, float)) or isinstance(valor, bool):
        raise HTTPException(status_code=400, detail=f"{campo} debe ser numérico o null.")
    if valor < 0:
        raise HTTPException(status_code=400, detail=f"{campo} no puede ser negativo.")
    return float(valor)

def _validate_nombre_modelo(nombre: str) -> str:
    s = (nombre or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail="nombre no puede estar vacío.")
    if len(s) > 120:
        raise HTTPException(status_code=400, detail="nombre excede 120 caracteres.")
    return s


@app.get("/modelos",
         tags=["Modelos"],
         description="Registro de modelos: nombre, proveedor y tarifas USD por 1M de tokens. Es la fuente de verdad que puebla el dropdown de modelo del asistente (?solo_activos=true) y el tab de Tarifas del admin. Lectura pública porque el host de widgets no tiene token.",
         summary="Listar Registro de Modelos")
def listar_registro_modelos(solo_activos: bool = False):
    return {"modelos": [_modelo_to_response(m) for m in registro_modelos.listar(solo_activos=solo_activos)]}


@app.post("/modelos",
          tags=["Modelos"],
          status_code=201,
          description="Registra un modelo nuevo con su proveedor y tarifas. Requiere token admin.",
          summary="Crear Modelo en el Registro")
def crear_modelo(body: ModeloCreate, _: bool = Depends(require_admin)):
    nombre = _validate_nombre_modelo(body.nombre)
    proveedor = _validate_proveedor(body.proveedor)
    p_in = _validate_precio(body.precio_input_usd_1m, "precio_input_usd_1m")
    p_out = _validate_precio(body.precio_output_usd_1m, "precio_output_usd_1m")

    conn = _agentes_connection()
    try:
        if conn.execute("SELECT nombre FROM modelos WHERE nombre=?", (nombre,)).fetchone():
            raise HTTPException(status_code=409, detail=f"Ya existe un modelo con nombre '{nombre}'.")
        conn.execute(
            f"INSERT INTO modelos ({registro_modelos.COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (nombre, proveedor, p_in, p_out, 1 if body.activo else 0, body.notas, _now()),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT {registro_modelos.COLS} FROM modelos WHERE nombre=?", (nombre,)
        ).fetchone()
        return _modelo_to_response(dict(row))
    finally:
        conn.close()


@app.put("/modelos/{nombre:path}",
         tags=["Modelos"],
         description="Actualiza proveedor, tarifas, estado o notas de un modelo. El nombre es inmutable (es la clave con la que se tarifaron los logs históricos). Requiere token admin.",
         summary="Actualizar Modelo del Registro")
def actualizar_modelo(nombre: str, body: ModeloUpdate, _: bool = Depends(require_admin)):
    conn = _agentes_connection()
    try:
        row = conn.execute(
            f"SELECT {registro_modelos.COLS} FROM modelos WHERE nombre=?", (nombre,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Modelo '{nombre}' no encontrado en el registro.")
        actual = dict(row)

        proveedor = actual["proveedor"] if body.proveedor is None else _validate_proveedor(body.proveedor)
        activo = actual["activo"] if body.activo is None else (1 if body.activo else 0)
        notas = actual["notas"] if body.notas is None else body.notas

        # Los precios usan model_fields_set en vez de `is None` para poder
        # distinguir "no lo mandé" (preservar) de "mandé null" (borrar la tarifa
        # y que el modelo pase a contarse como sin tarifa conocida).
        if 'precio_input_usd_1m' in body.model_fields_set:
            p_in = _validate_precio(body.precio_input_usd_1m, "precio_input_usd_1m")
        else:
            p_in = actual["precio_input_usd_1m"]
        if 'precio_output_usd_1m' in body.model_fields_set:
            p_out = _validate_precio(body.precio_output_usd_1m, "precio_output_usd_1m")
        else:
            p_out = actual["precio_output_usd_1m"]

        conn.execute(
            """UPDATE modelos
               SET proveedor=?, precio_input_usd_1m=?, precio_output_usd_1m=?,
                   activo=?, notas=?, actualizado_en=?
               WHERE nombre=?""",
            (proveedor, p_in, p_out, activo, notas, _now(), nombre),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT {registro_modelos.COLS} FROM modelos WHERE nombre=?", (nombre,)
        ).fetchone()
        return _modelo_to_response(dict(row))
    finally:
        conn.close()


@app.delete("/modelos/{nombre:path}",
            tags=["Modelos"],
            status_code=204,
            description="Elimina un modelo del registro. Bloquea con 409 si algún agente lo tiene asignado — en ese caso desactívalo (PUT activo=false) en vez de borrarlo. Requiere token admin.",
            summary="Borrar Modelo del Registro")
def borrar_modelo_registro(nombre: str, _: bool = Depends(require_admin)):
    conn = _agentes_connection()
    try:
        if not conn.execute("SELECT nombre FROM modelos WHERE nombre=?", (nombre,)).fetchone():
            raise HTTPException(status_code=404, detail=f"Modelo '{nombre}' no encontrado en el registro.")

        en_uso = conn.execute(
            "SELECT COUNT(*) FROM agentes WHERE modelo_llm=?", (nombre,)
        ).fetchone()[0]
        if en_uso:
            raise HTTPException(
                status_code=409,
                detail=f"'{nombre}' está asignado a {en_uso} agente(s). Desactívalo (PUT /modelos/{nombre} con activo=false) en vez de borrarlo.",
            )

        conn.execute("DELETE FROM modelos WHERE nombre=?", (nombre,))
        conn.commit()
        return None
    finally:
        conn.close()


@app.post("/modelos/sincronizar-ollama",
          tags=["Modelos"],
          description="Da de alta en el registro los modelos de Ollama instalados que aún no estén registrados, con tarifa 0.00 (hardware propio). No pisa filas existentes: los precios y el estado que hayas editado se respetan. Requiere token admin.",
          summary="Sincronizar Registro con Ollama")
async def sincronizar_modelos_ollama(_: bool = Depends(require_admin)):
    OLLAMA_URL = os.getenv('OLLAMA_URL', 'http://localhost:11434')
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{OLLAMA_URL}/api/tags", timeout=10)
            response.raise_for_status()
            instalados = [m['name'] for m in response.json().get('models', [])]
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"No se pudo conectar a Ollama: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al consultar Ollama: {e}")

    # Ollama no distingue LLMs de modelos de embedding en /api/tags, así que
    # filtramos por nombre. Es una heurística, no una garantía: si un LLM se
    # llamara "embed-algo" quedaría fuera, y por eso los omitidos se devuelven
    # explícitamente en la respuesta en vez de descartarse en silencio.
    omitidos = [n for n in instalados if 'embed' in n.lower()]
    candidatos = [n for n in instalados if n not in omitidos]

    conn = _agentes_connection()
    try:
        existentes = {r[0] for r in conn.execute("SELECT nombre FROM modelos").fetchall()}
        nuevos = [n for n in candidatos if n not in existentes]
        now = _now()
        for nombre in nuevos:
            conn.execute(
                f"INSERT INTO modelos ({registro_modelos.COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (nombre, 'ollama', 0.0, 0.0, 1, f'Alta automática desde Ollama ({now[:10]})', now),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "instalados_en_ollama": instalados,
        "agregados": nuevos,
        "ya_registrados": [n for n in candidatos if n in existentes],
        "omitidos_por_ser_embedding": omitidos,
    }


def _usuario_to_response(row: dict) -> dict:
    return {
        "id": row["id"],
        "proyecto_id": row["proyecto_id"],
        "slug": row["slug"],
        "nombre": row["nombre"],
        "activo": bool(row["activo"]),
        "notas": row["notas"],
        "creado_en": row["creado_en"],
        "actualizado_en": row["actualizado_en"],
    }


@app.get("/usuarios",
         tags=["Usuarios"],
         description="Registro de usuarios finales identificados (no autenticados) por proyecto — ej. 'Cristian QA', 'Bryan PO'. Es la fuente de verdad que resuelve el `?usuario=<slug>` del widget y puebla el tab Usuarios del admin. Lectura pública porque el host de widgets no tiene token.",
         summary="Listar Usuarios")
def listar_registro_usuarios(proyecto_id: Optional[str] = None, solo_activos: bool = False):
    return {"usuarios": [_usuario_to_response(u) for u in registro_usuarios.listar(proyecto_id=proyecto_id, solo_activos=solo_activos)]}


@app.post("/usuarios",
          tags=["Usuarios"],
          status_code=201,
          description="Registra un usuario final nuevo dentro de un proyecto. El slug es único por proyecto (no globalmente) y es lo que va en la URL `?usuario=<slug>` del widget. Requiere token admin.",
          summary="Crear Usuario")
def crear_usuario(body: UsuarioCreate, _: bool = Depends(require_admin)):
    _validate_proyecto_existe(body.proyecto_id)
    slug = _validate_slug(body.slug)
    nombre = _validate_nombre(body.nombre)

    conn = _agentes_connection()
    try:
        if conn.execute(
            "SELECT id FROM usuarios WHERE proyecto_id=? AND slug=?", (body.proyecto_id, slug)
        ).fetchone():
            raise HTTPException(status_code=409, detail=f"Ya existe un usuario con slug '{slug}' en este proyecto.")
        uid = uuid.uuid4().hex
        now = _now()
        conn.execute(
            f"INSERT INTO usuarios ({registro_usuarios.COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, body.proyecto_id, slug, nombre, 1 if body.activo else 0, body.notas, now, now),
        )
        conn.commit()
        row = conn.execute(f"SELECT {registro_usuarios.COLS} FROM usuarios WHERE id=?", (uid,)).fetchone()
        return _usuario_to_response(dict(row))
    finally:
        conn.close()


@app.put("/usuarios/{usuario_id}",
         tags=["Usuarios"],
         description="Actualiza nombre, estado o notas de un usuario. El proyecto y el slug son inmutables (el slug ya viaja en URLs entregadas y en chat_logs históricos); para renombrar el slug, crear uno nuevo y desactivar este. Requiere token admin.",
         summary="Actualizar Usuario")
def actualizar_usuario(usuario_id: str, body: UsuarioUpdate, _: bool = Depends(require_admin)):
    conn = _agentes_connection()
    try:
        row = conn.execute(f"SELECT {registro_usuarios.COLS} FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Usuario '{usuario_id}' no encontrado.")
        actual = dict(row)

        nombre = actual["nombre"] if body.nombre is None else _validate_nombre(body.nombre)
        activo = actual["activo"] if body.activo is None else (1 if body.activo else 0)
        notas = actual["notas"] if body.notas is None else body.notas

        conn.execute(
            "UPDATE usuarios SET nombre=?, activo=?, notas=?, actualizado_en=? WHERE id=?",
            (nombre, activo, notas, _now(), usuario_id),
        )
        conn.commit()
        row = conn.execute(f"SELECT {registro_usuarios.COLS} FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
        return _usuario_to_response(dict(row))
    finally:
        conn.close()


@app.delete("/usuarios/{usuario_id}",
            tags=["Usuarios"],
            status_code=204,
            description="Elimina un usuario del registro. A diferencia de /modelos, esto NUNCA se bloquea por uso: el nombre ya quedó denormalizado en cada chat_log al momento de la consulta, así que borrar el usuario no corrompe el histórico. Requiere token admin.",
            summary="Borrar Usuario")
def borrar_usuario(usuario_id: str, _: bool = Depends(require_admin)):
    conn = _agentes_connection()
    try:
        if not conn.execute("SELECT id FROM usuarios WHERE id=?", (usuario_id,)).fetchone():
            raise HTTPException(status_code=404, detail=f"Usuario '{usuario_id}' no encontrado.")
        conn.execute("DELETE FROM usuarios WHERE id=?", (usuario_id,))
        conn.commit()
        return None
    finally:
        conn.close()


def _validate_fecha_iso(fecha: Optional[str], campo: str = "fecha") -> Optional[str]:
    """Valida que sea un ISO 8601 parseable y lo devuelve tal cual (no lo
    normaliza) — el front ya manda UTC con offset."""
    if fecha is None:
        return None
    try:
        datetime.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{campo} debe ser una fecha ISO 8601 válida.")
    return fecha


@app.get("/hitos",
         tags=["Hitos"],
         description="Marcadores de línea de tiempo para el reporte de Registros (ej. 'aquí entró el cacheo de LLMs'). Globales, no por proyecto. Solo los usa el admin — requiere token.",
         summary="Listar Hitos")
def listar_hitos(_: bool = Depends(require_admin)):
    return {"hitos": registro_hitos.listar()}


@app.post("/hitos",
          tags=["Hitos"],
          status_code=201,
          description="Crea un hito. Si no se manda `fecha`, se usa el momento de creación — pero normalmente conviene mandar la fecha real en que el cambio entró en vigor (ej. la fecha del commit/deploy). Requiere token admin.",
          summary="Crear Hito")
def crear_hito(body: HitoCreate, _: bool = Depends(require_admin)):
    nombre = _validate_nombre(body.nombre)
    fecha = _validate_fecha_iso(body.fecha) or _now()

    hid = uuid.uuid4().hex
    now = _now()
    conn = _agentes_connection()
    try:
        conn.execute(
            f"INSERT INTO hitos ({registro_hitos.COLS}) VALUES (?, ?, ?, ?, ?, ?)",
            (hid, nombre, fecha, body.notas, now, now),
        )
        conn.commit()
        row = conn.execute(f"SELECT {registro_hitos.COLS} FROM hitos WHERE id=?", (hid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


@app.put("/hitos/{hito_id}",
         tags=["Hitos"],
         description="Actualiza nombre, fecha o notas de un hito. Requiere token admin.",
         summary="Actualizar Hito")
def actualizar_hito(hito_id: str, body: HitoUpdate, _: bool = Depends(require_admin)):
    conn = _agentes_connection()
    try:
        row = conn.execute(f"SELECT {registro_hitos.COLS} FROM hitos WHERE id=?", (hito_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Hito '{hito_id}' no encontrado.")
        actual = dict(row)

        nombre = actual["nombre"] if body.nombre is None else _validate_nombre(body.nombre)
        fecha = actual["fecha"] if body.fecha is None else _validate_fecha_iso(body.fecha)
        notas = actual["notas"] if body.notas is None else body.notas

        conn.execute(
            "UPDATE hitos SET nombre=?, fecha=?, notas=?, actualizado_en=? WHERE id=?",
            (nombre, fecha, notas, _now(), hito_id),
        )
        conn.commit()
        row = conn.execute(f"SELECT {registro_hitos.COLS} FROM hitos WHERE id=?", (hito_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


@app.delete("/hitos/{hito_id}",
            tags=["Hitos"],
            status_code=204,
            description="Elimina un hito. Requiere token admin.",
            summary="Borrar Hito")
def borrar_hito(hito_id: str, _: bool = Depends(require_admin)):
    conn = _agentes_connection()
    try:
        if not conn.execute("SELECT id FROM hitos WHERE id=?", (hito_id,)).fetchone():
            raise HTTPException(status_code=404, detail=f"Hito '{hito_id}' no encontrado.")
        conn.execute("DELETE FROM hitos WHERE id=?", (hito_id,))
        conn.commit()
        return None
    finally:
        conn.close()


@app.get("/listarModelos",
         tags=["Modelos"],
         description="Consulta Ollama y retorna la lista de modelos descargados localmente. Es la vista de 'qué hay instalado en la máquina', distinta de GET /modelos (el registro con proveedores y tarifas).",
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