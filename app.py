import os
import shutil
from pathlib import Path
from dotenv import load_dotenv

from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel

from integrar_conocimiento import embed
from query import query
from delete import delete
import bases_conocimiento

# Cargar variables de entorno
load_dotenv()

# Inicializar la aplicación
app = FastAPI()

# Definir la carpeta temporal para los archivos y la carpeta de la base de datos vectorial
TEMP_FOLDER = os.getenv('TEMP_FOLDER', './_temp')
DB_FOLDER = os.getenv('VECTOR_DB_FOLDER', './vector_db')
Path(TEMP_FOLDER).mkdir(parents=True, exist_ok=True)
Path(DB_FOLDER).mkdir(parents=True, exist_ok=True)

# Modifica el modelo de datos para incluir un historial
class QueryRequest(BaseModel):
    query: str
    history: list = [] # Nuevo campo para el historial de la conversación

class DeleteRequest(BaseModel):
    """
    Modelo Pydantic para el cuerpo de la solicitud DELETE, 
    asegurando que se envíe el 'filename'.
    """
    filename: str

@app.post("/intergrarConocimiento",
          tags=["Documentos"],
          description="Agrega el documento a la base de conocimiento elegida.",
          summary="Integrar Conocimiento"
          )
async def integrar_conocimiento(base_conocimiento: str, documento: UploadFile = File(...)):
    """
    Endpoint para procesar, dividir, vectorizar e integrar documento a la base de conocimiento.
    """
    if documento.filename == '':
        raise HTTPException(status_code=400, detail="No se ha seleccionado un archivo")

    # Guardar el archivo temporalmente para procesarlo
    file_path = os.path.join(TEMP_FOLDER, documento.filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(documento.file, buffer)

    try:
        embedded = embed(file_path, base_conocimiento)
        if embedded:
            print("Documento integrado exitosamente..")
            return {"message": "Integración correcta."}
        else:
            print("Error al embeber archivo...")
            raise HTTPException(status_code=500, detail="Error al integrar el documento.")
    finally:
        # Eliminar el archivo temporal
        os.remove(file_path)

@app.get("/listarDocumentos",
         tags=["Documentos"],
         description="Lista los documentos de una Base de Conocimiento.", 
         summary="Listar Documentos")
def route_list_documents(base_conocimiento: str):
    """
    Endpoint para listar los nombres únicos de los documentos (archivos) 
    agregados a una colección.
    """
    file_names = bases_conocimiento.list_document_names(base_conocimiento)
    
    if not file_names:
        # Esto sucede si la colección está vacía o si hubo un error.
        return {"message": "La colección está vacía o no se encontraron documentos.", "files": []}
        
    return {
        "cbase_conocimiento": base_conocimiento,
        "documentos": file_names,
        "conteo": len(file_names)
    }


@app.delete("/borrarConocimiento",
            tags=["Documentos"],
            description="Excluye un conocimiento determinado de una Base de Conocimiento.",
            summary="Borrar Documento")
def route_delete_document(base_conocimiento: str, request_data: DeleteRequest):
    """
    Endpoint para eliminar todos los fragmentos (chunks) asociados a 
    un nombre de archivo (filename) de una colección específica.
    """
    try:
        # FastAPI automáticamente valida el JSON y lo convierte a un objeto DeleteRequest
        deleted_count = bases_conocimiento.delete_documents_by_filename(
            base_conocimiento=base_conocimiento, 
            filename=request_data.filename  # Acceso a los datos con .filename
        )
        
        print("Archivo borrado...")
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno al eliminar documentos: {e}")

@app.post("/crearBaseConocimiento",
          tags=["Bases Conocimiento"])
def crear_base_conocimientos(nombre_base: str):
    """
    Endpoint para crear una nueva base de conocimiento vacía para el Chatbot.
    """
    try:
        return bases_conocimiento.crear_base(nombre_base)
    except Exception as e:
        return {"error": f"Error al listar las colecciones: {e}"}

@app.get("/listarBasesConocimiento",
         tags=["Bases Conocimiento"])
def listar_bases_conocimiento():
    """
    Endpoint para listar todas las colecciones de éste Chatbot.
    """
    try:
        return bases_conocimiento.listar_bases_conocimiento()
    except Exception as e:
        return {"error": f"Error al listar las colecciones: {e}"}

@app.post("/chatbot",
          tags=["Chatbot"])
def chatbot(base_conocimiento: str, request_data: QueryRequest):
    response = query(request_data.query, request_data.history, base_conocimiento)
    if response:
        return {"message": response}
    else:
        raise HTTPException(status_code=500, detail="Algo salió mal con la consulta")

@app.delete("/borrarBaseConocimiento",
            tags=["Bases Conocimiento"])
def borrar_base_conocimiento(base_conocimiento: str):
    """
    Endpoint para borrar una colección de ChromaDB por su nombre.
    """
    try:
        delete(base_conocimiento)
            
        return {"message": f"Colección '{base_conocimiento}' borrada exitosamente."}
    except Exception as e:
        return {"error": f"Error al borrar la colección: {e}"}
    

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)