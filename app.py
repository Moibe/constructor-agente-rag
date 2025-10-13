import os
import shutil
from pathlib import Path
from dotenv import load_dotenv

from pydantic import BaseModel
from fastapi import FastAPI, UploadFile, File, HTTPException

import contextos
from query import query
from delete import delete
from generacion_aumentada import embed

import herramientas

# Cargar variables de entorno
load_dotenv()

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

@app.get("/listarContextos",
         tags=["Contextos"])
def listar_contextos():
    """
    Endpoint para listar todos los contextos del Chatbot.
    """
    try:
        resultado = contextos.listar_contextos_con_conteo()
        return {"Contextos existentes para este chatbot": resultado} 
    except Exception as e:
        return {"error": f"Error al listar las colecciones: {e}"}
    
@app.post("/crearContexto",
          tags=["Contextos"])
def crear_contexto(nombre_contexto: str):
    """
    Endpoint para crear un nueva contexto vacío para el Chatbot.
    """
    try:
        return contextos.crear_contexto(nombre_contexto)
    except Exception as e:
        return {"error": f"Error al crear contexto: {e}"}

@app.get("/listarDocumentos",
         tags=["Documentos"],
         description="Lista los documentos que se han integrado a un contexto.", 
         summary="Listar Documentos")
def listar_documentos(contexto: str):
    """
    Endpoint para listar los nombres únicos de los documentos (archivos) 
    agregados a una colección (contexto).
    """
    file_names = contextos.listar_documentos(contexto)

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

    
    if contextos.existe_contexto(contexto):

        #REVISIÓN DE EXISTENCIA PREVIA DE ESOS VECTORES PARA EVITAR DUPLICIDAD
        # 1. Calcular el hash del archivo subido
        current_hash = herramientas.calculate_file_hash(file_path)
        print("El current hash obtenido es: ", current_hash)

        # 2. Verificar si el contenido ya fue subido
        if herramientas.is_content_duplicate(contexto, current_hash):
            print(f"El archivo {file_path} ya existe en la colección (Hash: {current_hash}). Saltando el embebido.")
            return {"mensaje": "Éste documento ya había sido integrado previamente."} # Ya está embebido, lo tratamos como éxito.

        try:
            embedded = embed(file_path, contexto, current_hash)
            if embedded:
                print("Documento integrado exitosamente..")
                return {"mensaje": "Integración correcta."}
            else:
                print("Error al embeber archivo...")
                raise HTTPException(status_code=500, detail="Error al integrar el documento.")
        finally:
            # Eliminar el archivo temporal
            os.remove(file_path)
    else: 
        return {"mensaje": f"No existe el contexto {contexto} al que quieres integrar el documento."}


@app.delete("/desacoplarDocumento",
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
        deleted_count = contextos.borrar_documento(
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

    response = query(data.pregunta, data.historial, data.contexto, data.modelo_llm)
    if response:
        return {"Mensaje": response}
    else:
        raise HTTPException(status_code=500, detail="Algo salió mal con la consulta.")

@app.delete("/borrarContexto",
            tags=["Contextos"])
def borrar_contexto(contexto: str):
    """
    Endpoint para borrar una colección de ChromaDB por su nombre.
    """
    try:
        delete(contexto)
            
        return {"Mensaje": f"Contexto '{contexto}' borrada exitosamente."}
    except Exception as e:
        return {"error": f"Error al borrar contexto: {e}"}    

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)