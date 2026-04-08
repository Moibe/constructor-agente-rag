import os
from langchain_chroma import Chroma
import herramientas

CHROMA_PATH = os.getenv('CHROMA_PATH', 'chroma')
TEXT_EMBEDDING_MODEL = os.getenv('TEXT_EMBEDDING_MODEL') #Antes de pasarlo como parámetro se obtenia de env vars.

def crea_contexto(client, nombre_contexto, embedding_model, chunk_size=7500):
    
    # 1. Inicializar el Embedding (detecta automáticamente si es OpenAI u Ollama)
    embedding = herramientas.obtener_embedding_function(embedding_model)
    print(f"Embedding creado con modelo: {embedding_model}")

    # 2. Definir la metadata a guardar
    # Almacenamos el nombre del modelo bajo una clave que elegimos, por ejemplo:
    metadata_contexto = {
        "embedding_model_name": embedding_model,
        "chunk_size": chunk_size,
        "descripcion": f"Colección para {nombre_contexto}."
    }
    
    # 3. Crear o Cargar la Colección, guardando la Metadata
    # LangChain la creará la primera vez si no existe.
    db = Chroma(
        client=client,
        collection_name=nombre_contexto,
        embedding_function=embedding,
        collection_metadata=metadata_contexto  # <--- ESTA ES LA CLAVE
    )

    return {"Mensaje": f"Contexto: {nombre_contexto} creado con modelo {embedding_model} y chunk_size de {chunk_size} caracteres."}
       

def contexto_existe(client, contexto):
          
    # Obtiene una lista de los nombres de las colecciones existentes
    contextos_existentes = client.list_collections()
    print("Éstos son los contextos existentes...")
    print(contextos_existentes)
    
    # Verifica si el nombre del contexto (base) a crear está en la lista de nombres existentes.
    if contexto in [c.name for c in contextos_existentes]:
        print(f"La colección '{contexto}' ya existe.")
        return True
    else:
        False