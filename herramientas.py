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

def obtener_modelo_de_embedding_de_coleccion(nombre_contexto: str, client: chromadb.PersistentClient) -> str | None:
    """
    Recupera el nombre del modelo de embedding que guardamos en la metadata de la colección.
    """
    try:
        # 1. Obtener la colección
        collection = client.get_collection(name=nombre_contexto)
        
        # 2. Acceder al diccionario 'metadata' de la colección
        metadata = collection.metadata
        
        # 3. Recuperar el nombre del modelo usando la clave que definimos al crearla
        modelo_nombre = metadata.get("embedding_model_name")
        
        return modelo_nombre
            
    except ValueError as e:
        # ChromaDB lanza ValueError si la colección no existe
        print(f"Error: Colección '{nombre_contexto}' no encontrada. {e}")
        return None
    except Exception as e:
        print(f"Error inesperado al recuperar metadata: {e}")
        return None