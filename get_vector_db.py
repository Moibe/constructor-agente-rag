import os
# from langchain_community.embeddings import OllamaEmbeddings
from langchain_ollama import OllamaEmbeddings
from langchain_chroma import Chroma
# from langchain_community.vectorstores.chroma import Chroma
import chromadb

CHROMA_PATH = os.getenv('CHROMA_PATH', 'chroma')
#COLLECTION_NAME = os.getenv('COLLECTION_NAME', 'local-rag')
TEXT_EMBEDDING_MODEL = os.getenv('TEXT_EMBEDDING_MODEL', 'nomic-embed-text')

def get_vector_db(collection_name):
    embedding = OllamaEmbeddings(model=TEXT_EMBEDDING_MODEL, 
                                 #show_progress=True
                                 )
    
    # Crea una instancia del cliente de ChromaDB directamente
    # con el directorio de persistencia.
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    
    # Obtiene una lista de los nombres de las colecciones existentes
    existing_collections = client.list_collections()

    print("Éstas son las existing collections...")
    print(existing_collections)
    
    # Verifica si el nombre de la colección está en la lista de nombres existentes
    if collection_name in [c.name for c in existing_collections]:
        print(f"La colección '{collection_name}' ya existe. Cargándola...")
        # Ahora, carga la colección con LangChain usando el cliente
        db = Chroma(
            client=client,
            collection_name=collection_name,
            embedding_function=embedding
        )

        if db._collection.count() > 0:
            print(f"La colección '{collection_name}' existe y tiene {db._collection.count()} documentos.")
        else:
            print(f"La colección '{collection_name}' no existe o está vacía. Creando una nueva...")

    else:
        print(f"La colección '{collection_name}' no existe. Creando una nueva...")
        # Si no existe, LangChain la creará la primera vez que se agregue un documento
        db = Chroma(
            client=client,
            collection_name=collection_name,
            embedding_function=embedding
        )
        
    # Regreso la base de datos para que pueda contar los documentos
    # o hacer otras operaciones con ella.
    return db