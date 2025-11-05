import os
from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings

CHROMA_PATH = os.getenv('CHROMA_PATH', 'chroma')
TEXT_EMBEDDING_MODEL = os.getenv('TEXT_EMBEDDING_MODEL') #Antes de pasarlo como parámetro se obtenia de env vars.

def crea_contexto(client, nombre_contexto, embedding_model):
    
    # 1. Inicializar el Embedding
    # Es importante inicializarlo para que LangChain lo use
    embedding = OllamaEmbeddings(validate_model_on_init=True, model=embedding_model)
    print(f"Embedding creado con modelo: {embedding_model}")

    # 2. Definir la metadata a guardar
    # Almacenamos el nombre del modelo bajo una clave que elegimos, por ejemplo:
    metadata_contexto = {
        "embedding_model_name": embedding_model, 
        "descripcion": f"Colección para {nombre_contexto} usando Ollama."
    }
    
    # 3. Crear o Cargar la Colección, guardando la Metadata
    # LangChain la creará la primera vez si no existe.
    db = Chroma(
        client=client,
        collection_name=nombre_contexto,
        embedding_function=embedding,
        collection_metadata=metadata_contexto  # <--- ESTA ES LA CLAVE
    )

    # 4. (Opcional) Forzar la creación si no existe
    # Si quieres asegurar que la metadata se escriba inmediatamente,
    # puedes añadir un documento vacío (dummy) la primera vez que se crea.
    # db.add_texts(["Inicio de colección"], metadatas=[{"source": "initial_creation"}])
    
    return {f"Contexto: {nombre_contexto} creado con modelo {embedding_model}."}
       

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