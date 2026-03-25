import os
import shutil
import sqlite3
import json
import asyncio
import httpx
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import Optional
from fastapi import FastAPI, UploadFile, File, HTTPException
import funciones
import chatbot as asistente
import generacion_aumentada

import herramientas

# Cargar variables de entorno
load_dotenv()

print("="*60, flush=True)
print("[INICIO] APP.PY CARGADO - Si ves esto, el codigo esta actualizado", flush=True)
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

# Inicializar base de datos SQLite para logs
LOG_DB_PATH = os.getenv('LOG_DB_PATH', 'logs.db')

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

init_log_db()

app = FastAPI(
    title="Chatbot - Mide",
    description="Operaciones generales de chatbot incluídas la creación de contextos, carga de documentos e interacción con chatbot.",
    version="0.0.0"
)

# Definir la carpeta temporal para los archivos y la carpeta de la base de datos vectorial
TEMP_FOLDER = os.getenv('TEMP_FOLDER', './_temp')
DB_FOLDER = os.getenv('VECTOR_DB_FOLDER', './vector_db')
Path(TEMP_FOLDER).mkdir(parents=True, exist_ok=True)
Path(DB_FOLDER).mkdir(parents=True, exist_ok=True)

class ChatRequest(BaseModel):
    contexto: str = None 
    modelo_llm: str
    pregunta: str
    historial: list = []

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
async def crear_contexto(nombre_contexto: str, embedding_model: str, chunk_size: int = 7500):
    """
    Endpoint para crear un nueva contexto vacío para el Chatbot.
    """
    try:
        # 1. Obtener información del modelo desde Ollama (o definir defaults para OpenAI)
        context_window = 4096  # Default conservador
        
        if not herramientas.es_modelo_openai(embedding_model):
            OLLAMA_URL = os.getenv('OLLAMA_URL', 'http://localhost:11434')
            async with httpx.AsyncClient() as client:
                try:
                    response = await client.post(f"{OLLAMA_URL}/api/show", json={"name": embedding_model}, timeout=5)
                    if response.status_code == 200:
                        data = response.json()
                        info = data.get("model_info", {})
                        # Intentar obtener el context_length de la arquitectura del modelo
                        arch = info.get("general.architecture", "")
                        context_window = info.get(f"{arch}.context_length") or info.get("adapter.context_length") or 4096
                except Exception as e:
                    logger.warning(f"No se pudo obtener info de Ollama para {embedding_model}, usando default: {e}")
        else:
            # Modelos de OpenAI suelen tener límites conocidos
            if "text-embedding-3" in embedding_model:
                context_window = 8191
            else:
                context_window = 8191

        # 2. Validar chunk_size basado en la ventana de contexto del modelo
        # El chunk_size está en CARACTERES. Convertimos la ventana (en tokens)
        # a un límite aproximado en caracteres usando un factor conservador (3x).
        CHUNK_SIZE_MIN = 100
        CHUNK_SIZE_MAX = int(context_window * 3)

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

@app.post("/chatbot",
          tags=["Chatbot"])
def chatbot(data: ChatRequest):

    print(f"Modelo LLM: {data.modelo_llm}")
    print(f"Contexto: {data.contexto}")
    print(f"Query: {data.pregunta}")
    print(f"Historial: {data.historial}")

    response = asistente.chat(data.pregunta, data.historial, data.contexto, data.modelo_llm)
    print("Respuesta: ", response)
    if response:
        return {"Mensaje": response}
    else:
        raise HTTPException(status_code=500, detail="Algo salió mal con la consulta.")

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
            return {
                "modelo": modelo,
                "arquitectura":  info.get("general.architecture", "desconocida"),
                "parametros":    info.get("general.parameter_count", "desconocido"),
                "contexto_max":  info.get(f"{info.get('general.architecture', '')}.context_length", "desconocido"),
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
    uvicorn.run(app, host="0.0.0.0", port=8080)