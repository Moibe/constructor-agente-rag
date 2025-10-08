import os
import shutil
from pathlib import Path
from dotenv import load_dotenv

from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel
from typing import Optional

from documentos import embed
from query import query
from delete import delete
import contextos

# Cargar variables de entorno
load_dotenv()

# Inicializar la aplicación
app = FastAPI()

# Definir la carpeta temporal para los archivos y la carpeta de la base de datos vectorial
TEMP_FOLDER = os.getenv('TEMP_FOLDER', './_temp')
DB_FOLDER = os.getenv('VECTOR_DB_FOLDER', './vector_db')
Path(TEMP_FOLDER).mkdir(parents=True, exist_ok=True)
Path(DB_FOLDER).mkdir(parents=True, exist_ok=True)

class ChatRequest(BaseModel):
    # Datos que antes iban separados
    contexto: str = None 
    modelo_llm: str
    pregunta: str
    historial: list = []

# class QueryRequest(BaseModel):
#     query: str
#     history: list = []

class DeleteRequest(BaseModel):
    """
    Modelo Pydantic para el cuerpo de la solicitud DELETE, 
    asegurando que se envíe el 'filename'.
    """
    filename: str

@app.get("/listarContextos",
         tags=["Contextos"])
def listar_contextos():
    """
    Endpoint para listar todas las colecciones de éste Chatbot.
    """
    try:
        resultado = contextos.listar_contextos()
        return {"Contextos existentes para éste chatbot": resultado} 
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

    if isinstance(file_names, str):
        return {"Ese contexto no existe en base."}
    
    if not file_names:
        # Esto sucede si la colección está vacía o si hubo un error.
        return {"message": "El contexto está vacío.", "files": []}
        
    return {
        "contexto": contexto,
        "documentos": file_names,
        "conteo": len(file_names)
    }

@app.post("/vectorizarDocumento",
          tags=["Documentos"],
          description="Carga, divide, vectoriza e integra el documento al contexto elegido.",
          summary="Vectorizar Documento"
          )
async def vectorizar_documento(contexto: str, documento: UploadFile = File(...)):
    """
    Endpoint para procesar, dividir, vectorizar e integrar documento al contexto elegido.
    """
    if documento.filename == '':
        raise HTTPException(status_code=400, detail="No se ha seleccionado un archivo")

    # Guardar el archivo temporalmente para procesarlo
    file_path = os.path.join(TEMP_FOLDER, documento.filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(documento.file, buffer)

    #Quizá aquí antes checar primero si existe el contexto.
    

    try:
        embedded = embed(file_path, contexto)
        if embedded:
            print("Documento integrado exitosamente..")
            return {"message": "Integración correcta."}
        else:
            print("Error al embeber archivo...")
            raise HTTPException(status_code=500, detail="Error al integrar el documento.")
    finally:
        # Eliminar el archivo temporal
        os.remove(file_path)


@app.delete("/desacoplarDocumento",
            tags=["Documentos"],
            description="Retira un documento determinado, borrando ese aprendizaje de ese contexto.",
            summary="Desacoplar Documento")
def borrar_documento(contexto: str, request_data: DeleteRequest):
    """
    Endpoint para eliminar todos los fragmentos (chunks) asociados a 
    un nombre de archivo (filename) de una colección específica.
    """
    try:
        # FastAPI automáticamente valida el JSON y lo convierte a un objeto DeleteRequest
        deleted_count = contextos.delete_documents_by_filename(
            contexto=contexto, 
            filename=request_data.filename  # Acceso a los datos con .filename
        )
        
        print("Archivo borrado...")
        
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
        return {"message": response}
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
            
        return {"message": f"Contexto '{contexto}' borrada exitosamente."}
    except Exception as e:
        return {"error": f"Error al borrar contexto: {e}"}
    

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)