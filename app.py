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
from fastapi import FastAPI, UploadFile, File, HTTPException
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
        error TEXT
    )''')
    conn.commit()
    conn.close()

def init_agentes_db():
    conn = sqlite3.connect(AGENTES_DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS agentes (
        id             TEXT PRIMARY KEY,
        slug           TEXT NOT NULL UNIQUE,
        nombre         TEXT NOT NULL,
        instrucciones  TEXT NOT NULL,
        contexto       TEXT NOT NULL,
        modelo_llm     TEXT NOT NULL,
        historial_max  INTEGER NOT NULL DEFAULT 5,
        creado_en      TEXT NOT NULL,
        actualizado_en TEXT NOT NULL
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_agentes_slug ON agentes(slug)')
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
init_agentes_db()
cleanup_legacy_agentes_in_logs_db()

app = FastAPI(
    title="Constructor RAG",
    description="Operaciones generales de chatbot incluídas la creación de contextos, carga de documentos e interacción con chatbot.",
    version="0.0.0"
)

# Configurar CORS en base a variable de entorno (lista separada por comas)
allowed_origins_env = os.getenv('CORS_ALLOWED_ORIGINS', '*')
allowed_origins = [origin.strip() for origin in allowed_origins_env.split(',') if origin.strip()]
if not allowed_origins:
    allowed_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)

# Definir la carpeta temporal para los archivos y la carpeta de la base de datos vectorial
TEMP_FOLDER = os.getenv('TEMP_FOLDER', './_temp')
DB_FOLDER = os.getenv('VECTOR_DB_FOLDER', './vector_db')
Path(TEMP_FOLDER).mkdir(parents=True, exist_ok=True)
Path(DB_FOLDER).mkdir(parents=True, exist_ok=True)

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

class AgenteCreate(BaseModel):
    slug: str
    nombre: str
    instrucciones: str
    contexto: str
    modelo_llm: str
    historial_max: int = 5

class AgenteUpdate(BaseModel):
    nombre: Optional[str] = None
    instrucciones: Optional[str] = None
    contexto: Optional[str] = None
    modelo_llm: Optional[str] = None
    historial_max: Optional[int] = None
    # Sentinels para detectar intentos de modificar campos inmutables
    id: Optional[str] = None
    slug: Optional[str] = None

class Agente(BaseModel):
    id: str
    slug: str
    nombre: str
    instrucciones: str
    contexto: str
    modelo_llm: str
    historial_max: int
    creado_en: str
    actualizado_en: str

@app.get("/listarContextos",
         tags=["Contextos"])
def listar_contextos():
    """
    Endpoint para listar todos los contextos del Chatbot.
    """
    try:
        resultado = funciones.listar_contextos_con_conteo()
        return {"Contextos existentes para este chatbot": resultado} 
    except Exception as e:
        return {"error": f"Error al listar las colecciones: {e}"}
    
@app.post("/crearContexto",
          tags=["Contextos"])
async def crear_contexto(nombre_contexto: str, embedding_model: str, chunk_size: Optional[int] = None):
    """
    Endpoint para crear un nueva contexto vacío para el Chatbot.
    Si no se especifica chunk_size, se calcula automáticamente como el 80% del máximo
    permitido por el modelo (context_window_tokens × 3 × 0.8).
    """
    try:
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

        return funciones.crear_contexto(nombre_contexto, embedding_model, chunk_size)
    except HTTPException:
        raise
    except Exception as e:
        return {"error": f"Error al crear contexto: {e}"}
    

@app.delete("/borrarContexto",
            tags=["Contextos"])
def borrar_contexto(contexto: str):
    """
    Endpoint para borrar una colección de ChromaDB por su nombre.
    """
    try:
        funciones.delete_contexto(contexto)
            
        return {"Mensaje": f"Contexto '{contexto}' borrada exitosamente."}
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

_AGENTE_COLS = "id, slug, nombre, instrucciones, contexto, modelo_llm, historial_max, creado_en, actualizado_en"

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

    if not contexto_efectivo:
        raise HTTPException(status_code=400, detail="contexto es requerido (envíalo en el body o asocia un agente con contexto).")
    if not modelo_efectivo:
        raise HTTPException(status_code=400, detail="modelo_llm es requerido (envíalo en el body o asocia un agente con modelo_llm).")

    print(f"Modelo LLM: {modelo_efectivo}")
    print(f"Contexto: {contexto_efectivo}")
    print(f"Query: {data.pregunta}")
    print(f"Historial: {data.historial}")
    print(f"Agente: {data.agente_id}")

    response = asistente.chat(
        data.pregunta,
        data.historial,
        contexto_efectivo,
        modelo_efectivo,
        instrucciones=instrucciones_efectivas,
    )
    print("Respuesta: ", response)
    if response:
        return {"Mensaje": response}
    else:
        raise HTTPException(status_code=500, detail="Algo salió mal con la consulta.")

@app.get("/agentes",
         tags=["Agentes"],
         description="Lista todos los agentes configurados, ordenados por fecha de creación descendente.",
         summary="Listar Agentes")
def listar_agentes():
    conn = _agentes_connection()
    try:
        rows = conn.execute(
            f"SELECT {_AGENTE_COLS} FROM agentes ORDER BY creado_en DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@app.post("/agentes",
          tags=["Agentes"],
          status_code=201,
          description="Crea un agente con bundle completo (slug, nombre, instrucciones, contexto, modelo_llm, historial_max).",
          summary="Crear Agente")
def crear_agente(body: AgenteCreate):
    slug = _validate_slug(body.slug)
    nombre = _validate_nombre(body.nombre)
    instrucciones = _validate_no_empty(body.instrucciones, "instrucciones")
    contexto = _validate_no_empty(body.contexto, "contexto")
    modelo_llm = _validate_no_empty(body.modelo_llm, "modelo_llm")
    historial_max = _validate_historial_max(body.historial_max)

    aid = uuid.uuid4().hex
    now = _now()
    conn = _agentes_connection()
    try:
        existing = conn.execute("SELECT id FROM agentes WHERE slug=?", (slug,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"Ya existe un agente con slug '{slug}'.")

        conn.execute(
            f"INSERT INTO agentes ({_AGENTE_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, slug, nombre, instrucciones, contexto, modelo_llm, historial_max, now, now),
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

@app.put("/agentes/{aid}",
         tags=["Agentes"],
         description="Actualiza campos del bundle. id y slug son inmutables. Campos no enviados se mantienen.",
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
        contexto = (
            actual["contexto"] if body.contexto is None
            else _validate_no_empty(body.contexto, "contexto")
        )
        modelo_llm = (
            actual["modelo_llm"] if body.modelo_llm is None
            else _validate_no_empty(body.modelo_llm, "modelo_llm")
        )
        historial_max = (
            actual["historial_max"] if body.historial_max is None
            else _validate_historial_max(body.historial_max)
        )

        conn.execute(
            "UPDATE agentes SET nombre=?, instrucciones=?, contexto=?, modelo_llm=?, historial_max=?, actualizado_en=? WHERE id=?",
            (nombre, instrucciones, contexto, modelo_llm, historial_max, _now(), aid),
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
            '''INSERT INTO chat_logs (fecha, sesion, ambiente, modelo, contexto, pregunta, historial, respuesta, ms, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (data.fecha, data.sesion, data.ambiente, data.modelo, data.contexto,
             data.pregunta, data.historial, data.respuesta, data.ms, data.error)
        )
        conn.commit()
        conn.close()
        print(f"[OK] Log registrado para sesion: {data.sesion}")
        return {"Mensaje": "Log registrado correctamente."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al registrar log: {e}")

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

@app.get("/health",
         tags=["Utilidad"],
         description="Verifica que el servidor esté en línea.",
         summary="Health Check")
def health():
    return {"status": "ok", "mensaje": "Servidor en línea"}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8077)