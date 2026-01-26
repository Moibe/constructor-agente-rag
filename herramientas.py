import hashlib
import generacion_aumentada
import chromadb
import os
import time
import logging
from typing import Optional

CHROMA_PATH = os.getenv('CHROMA_PATH', 'chroma')

def calculate_file_hash(file_path, hash_algorithm='sha256'):
    """Calcula el hash del contenido del archivo."""
    hasher = hashlib.new(hash_algorithm)
    block_size = 65536  # 64KB
    
    with open(file_path, 'rb') as file:
        buffer = file.read(block_size)
        while len(buffer) > 0:
            hasher.update(buffer)
            buffer = file.read(block_size)
            
    return hasher.hexdigest()

def is_content_duplicate(nombre_contexto: str, file_hash: str) -> bool:
    """Verifica si un hash de contenido ya existe en la colección."""

    print("Estoy en is content duplicate...")
    print("Hashtag:", file_hash)
    print("nombre contexto: ", nombre_contexto)
    db = generacion_aumentada.obtenContexto(nombre_contexto)
    collection = db._collection
    
    # Busca un documento que tenga este hash en su metadato 'file_hash'
    results = collection.get(
        where={"file_hash": file_hash},
        include=[] # Solo necesitamos los IDs para saber si existe
    )

    print("Esto es results obtenido: ", results)
    
    # Si la lista de IDs no está vacía, el documento existe.
    return len(results.get('ids', [])) > 0

def debug_check_file_hash_storage(nombre_base: str):
    """
    Función de DEPURACIÓN: Imprime los metadatos del primer documento 
    para verificar si la clave 'file_hash' se guardó correctamente.
    """
    db = generacion_aumentada.obtenContexto(nombre_base)
    collection = db._collection
    
    # Obtener el primer documento de la colección (limit=1)
    results = collection.get(
        include=['metadatas'],
        limit=1 
    )
    
    if results and results.get('metadatas'):
        print("\n--- METADATOS ALMACENADOS (Primer Chunk) ---")
        stored_metadata = results['metadatas'][0]
        print(stored_metadata)
        
        if 'file_hash' in stored_metadata:
            print("\n¡ÉXITO! La clave 'file_hash' SÍ está presente.")
            print(f"El hash almacenado es: {stored_metadata['file_hash']}")
        else:
            print("\n¡ATENCIÓN! La clave 'file_hash' NO está presente en el metadato.")
            print("Verifique que el código que añade 'chunk.metadata[\"file_hash\"] = ...' se esté ejecutando.")
        print("-------------------------------------------\n")
    else:
        print("La colección está vacía o hubo un error al obtener el documento.")

def obtener_modelo_de_embedding_de_coleccion(nombre_contexto: str, client: chromadb.PersistentClient) -> Optional[str]:
    """Obtiene el nombre del modelo de embedding desde la colección.
    
    Intenta primero con get_settings(), luego fallback a leer del primer documento.
    Finalmente, devuelve None si no se encuentra.
    """
    logging.debug("obtener_modelo_de_embedding_de_coleccion: %s", nombre_contexto)
    try:
        collection = client.get_collection(name=nombre_contexto)
    except Exception as e:
        logging.warning("No se pudo obtener la colección '%s': %s", nombre_contexto, e)
        return None

    # Intenta usar get_settings()
    if hasattr(collection, "get_settings"):
        try:
            settings = collection.get_settings()
            logging.debug("Settings de la colección: %s", settings)
            if isinstance(settings, dict):
                modelo_nombre = settings.get('metadata', {}).get('embedding_model_name')
                if modelo_nombre:
                    logging.debug("Modelo obtenido de settings: %s", modelo_nombre)
                    return modelo_nombre
        except Exception as e:
            logging.debug("No se pudo usar get_settings(): %s", e)

    # Fallback: leer del primer documento
    try:
        results = collection.get(include=['metadatas'], limit=1)
        if results and results.get('metadatas'):
            first_meta = results['metadatas'][0] or {}
            modelo_nombre = first_meta.get('embedding_model_name')
            if modelo_nombre:
                logging.debug("Modelo obtenido del primer documento: %s", modelo_nombre)
                return modelo_nombre
            else:
                logging.debug("No se encontró 'embedding_model_name' en el primer documento: %s", first_meta)
    except Exception as e:
        logging.exception("Fallback leyendo metadatas falló para la colección '%s': %s", nombre_contexto, e)

    logging.warning("No se pudo obtener el modelo de embedding para la colección '%s'", nombre_contexto)
    return None