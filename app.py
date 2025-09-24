import os
import shutil
from pathlib import Path
from dotenv import load_dotenv

from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel

from embed import embed
from query import query
from get_vector_db import get_vector_db
from delete import delete
from listaColecciones import listaColecciones

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

@app.post("/cargarConocimiento")
async def cargarConocimiento(collection_name: str, file: UploadFile = File(...)):
    """
    Endpoint para procesar y embeber un archivo.
    """
    if file.filename == '':
        raise HTTPException(status_code=400, detail="No se ha seleccionado un archivo")

    # Guardar el archivo temporalmente para procesarlo
    file_path = os.path.join(TEMP_FOLDER, file.filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        embedded = embed(file_path, collection_name)
        if embedded:
            print("Archivo embebido exitosamente.")
            return {"message": "Archivo embebido exitosamente"}
        else:
            print("Error al embeber archivo...")
            raise HTTPException(status_code=500, detail="Error al embeber el archivo")
    finally:
        # Eliminar el archivo temporal
        os.remove(file_path)

@app.get("/collections")
def list_collections():
    """
    Endpoint para listar todas las colecciones de ChromaDB.
    """
    try:
        return listaColecciones()
    except Exception as e:
        return {"error": f"Error al listar las colecciones: {e}"}

@app.post("/query/{collection_name}")
def route_query(collection_name: str, request_data: QueryRequest):
    response = query(request_data.query, request_data.history, collection_name)
    if response:
        return {"message": response}
    else:
        raise HTTPException(status_code=500, detail="Algo salió mal con la consulta")

@app.delete("/reset")
def route_reset():
    """
    Endpoint para resetear la base de datos vectorial.
    """
    if os.path.exists(DB_FOLDER):
        shutil.rmtree(DB_FOLDER)
        Path(DB_FOLDER).mkdir(parents=True, exist_ok=True)
        return {"message": "Base de datos vectorial reseteada exitosamente."}
    else:
        raise HTTPException(status_code=404, detail="La base de datos vectorial no existe.")
    
@app.delete("/borrarColeccion/{collection_name}")
def delete_collection(collection_name: str):
    """
    Endpoint para borrar una colección de ChromaDB por su nombre.
    """
    try:
        delete(collection_name)
            
        return {"message": f"Colección '{collection_name}' borrada exitosamente."}
    except Exception as e:
        return {"error": f"Error al borrar la colección: {e}"}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)