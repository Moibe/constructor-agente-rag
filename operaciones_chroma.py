import os
from langchain_ollama import OllamaEmbeddings
from langchain_chroma import Chroma
import chromadb

CHROMA_PATH = os.getenv('CHROMA_PATH', 'chroma')
#COLLECTION_NAME = os.getenv('COLLECTION_NAME', 'local-rag')
TEXT_EMBEDDING_MODEL = os.getenv('TEXT_EMBEDDING_MODEL', 'nomic-embed-text')

def obtenBase(nombre_base): 

    client = chromadb.PersistentClient(path=CHROMA_PATH)
    # Si la base existe la carga la colección con LangChain usando el cliente
    embedding = OllamaEmbeddings(model=TEXT_EMBEDDING_MODEL)
    db = Chroma(
        client=client,
        collection_name=nombre_base,
        embedding_function=embedding
    )

    if db._collection.count() > 0:
        print(f"La colección '{nombre_base}' existe y tiene {db._collection.count()} documentos.")
    else:
        print(f"La colección '{nombre_base}' no existe o está vacía.")

    return db

def crea_base(client, nombre_base): 
    #Básicamente crear una nueva base es lo mismo que cargarla.

    embedding = OllamaEmbeddings(model=TEXT_EMBEDDING_MODEL)
    
    # Si no existe, LangChain la creará la primera vez que se agregue un documento
    db = Chroma(
        client=client,
        collection_name=nombre_base,
        embedding_function=embedding
    )

    return {f"Bases de conocimiento {nombre_base} creada."}
       

def base_existe(client, base_conocimiento):
          
    # Obtiene una lista de los nombres de las colecciones existentes
    bases_existentes = client.list_collections()
    print("Éstas son las bases existentes...")
    print(bases_existentes)
    
    # Verifica si el nombre de la base a crear está en la lista de nombres existentes
    if base_conocimiento in [c.name for c in bases_existentes]:
        print(f"La colección '{base_conocimiento}' ya existe.")
        return True
    else:
        False