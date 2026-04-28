import hashlib
import chromadb
import os
import time
import logging
from typing import Optional
from langchain_chroma import Chroma

# Modelos de OpenAI para embeddings
OPENAI_EMBEDDING_MODELS = [
    "text-embedding-3-small",
    "text-embedding-3-large", 
    "text-embedding-ada-002"
]

OPENAI_LLM_MODELS = [
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-3.5-turbo"
]


def es_modelo_openai_llm(nombre_modelo: str) -> bool:
    """Determina si un modelo LLM es de OpenAI."""
    return nombre_modelo in OPENAI_LLM_MODELS

CHROMA_PATH = os.getenv('CHROMA_PATH', 'chroma')
TEXT_EMBEDDING_MODEL = os.getenv('TEXT_EMBEDDING_MODEL')


def es_modelo_openai(nombre_modelo: str) -> bool:
    """
    Determina si un modelo de embedding es de OpenAI.
    """
    return nombre_modelo in OPENAI_EMBEDDING_MODELS


def obtener_embedding_function(nombre_modelo: str):
    """
    Factory que devuelve la función de embedding correcta según el proveedor.
    - Si el modelo es de OpenAI → OpenAIEmbeddings
    - Si no → OllamaEmbeddings
    """
    if es_modelo_openai(nombre_modelo):
        from langchain_openai import OpenAIEmbeddings
        logging.info(f"Usando OpenAIEmbeddings para modelo: {nombre_modelo}")
        return OpenAIEmbeddings(model=nombre_modelo)
    else:
        from langchain_ollama import OllamaEmbeddings
        logging.info(f"Usando OllamaEmbeddings para modelo: {nombre_modelo}")
        return OllamaEmbeddings(model=nombre_modelo)


def obtener_chunk_size_de_coleccion(nombre_contexto: str) -> int:
    """
    Obtiene el chunk_size asociado a un contexto/colección desde collection_metadata.
    Si no encuentra, retorna 7500 como default.
    """
    try:
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        collection = client.get_collection(name=nombre_contexto)
        
        # Obtener desde collection metadata
        try:
            # Intentar obtener metadata de la colección
            metadata = collection.metadata
            if metadata and 'chunk_size' in metadata:
                chunk_size = int(metadata['chunk_size'])
                logging.info(f"Chunk size obtenido desde metadata de colección: {chunk_size}")
                return chunk_size
        except Exception as e:
            logging.warning(f"No se pudo obtener metadata de colección para {nombre_contexto}: {e}")
        
        # Fallback final
        logging.warning(f"No se pudo obtener chunk_size para {nombre_contexto}, usando default 7500")
        return 7500
        
    except Exception as e:
        logging.error(f"Error completo al obtener chunk_size de {nombre_contexto}: {e}")
        return 7500

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
    print("="*50, flush=True)
    print(f"[...] is_content_duplicate() - Verificando duplicado...", flush=True)
    print(f"[*] Contexto: {nombre_contexto}", flush=True)
    print(f"[*] Hash: {file_hash}", flush=True)
    print("="*50, flush=True)
    
    print("[...] Llamando a obtenContexto()...", flush=True)
    db = obtenContexto(nombre_contexto)
    print(f"[OK] obtenContexto() retorno: {db}", flush=True)
    
    collection = db._collection
    print(f"[*] Coleccion obtenida, buscando hash...", flush=True)
    
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
    db = obtenContexto(nombre_base)
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

    # Obtener desde collection metadata
    try:
        metadata = collection.metadata
        if metadata and 'embedding_model_name' in metadata:
            modelo_nombre = metadata['embedding_model_name']
            logging.debug("Modelo obtenido de metadata de colección: %s", modelo_nombre)
            return modelo_nombre
        else:
            logging.debug("No se encontró 'embedding_model_name' en metadata de colección")
    except Exception as e:
        logging.exception("Error obteniendo metadata de colección '%s': %s", nombre_contexto, e)

    logging.warning("No se pudo obtener el modelo de embedding para la colección '%s'", nombre_contexto)
    return None


def obtenContexto(nombre_contexto):
    print("="*50, flush=True)
    print(f"[INICIO] obtenContexto('{nombre_contexto}')", flush=True)
    print("="*50, flush=True)

    client = chromadb.PersistentClient(path=CHROMA_PATH)

    print(f"[OK] Cliente ChromaDB creado", flush=True)

    modelo_embedding_nombre = obtener_modelo_de_embedding_de_coleccion(nombre_contexto, client)
    print(f"[*] Modelo de embedding recuperado: {modelo_embedding_nombre}", flush=True)

    # Si no se encuentra el modelo, usar el modelo por defecto (TEXT_EMBEDDING_MODEL de env vars)
    if not modelo_embedding_nombre:
        modelo_embedding_nombre = TEXT_EMBEDDING_MODEL
        if not modelo_embedding_nombre:
            print(f"[ERROR] No se pudo obtener el nombre del modelo de embedding para '{nombre_contexto}' y tampoco existe TEXT_EMBEDDING_MODEL en env vars.", flush=True)
            return None
        print(f"[AVISO] Usando modelo por defecto: {modelo_embedding_nombre}", flush=True)

    print(f"[...] Creando embedding con modelo: {modelo_embedding_nombre}...", flush=True)
    embedding = obtener_embedding_function(modelo_embedding_nombre)
    print(f"[OK] Embedding creado", flush=True)

    print(f"[...] Creando objeto Chroma para coleccion '{nombre_contexto}'...", flush=True)
    db = Chroma(
        client=client,
        collection_name=nombre_contexto,
        embedding_function=embedding
    )
    print(f"[OK] Objeto Chroma creado", flush=True)

    if db._collection.count() > 0:
        print(f"La colección '{nombre_contexto}' existe y tiene {db._collection.count()} documentos.")
    else:
        print(f"La colección '{nombre_contexto}' está vacía.")

    return db