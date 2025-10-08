import os
import chromadb
from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings

CHROMA_PATH = os.getenv('CHROMA_PATH', 'chroma')
TEXT_EMBEDDING_MODEL = os.getenv('TEXT_EMBEDDING_MODEL', 'nomic-embed-text')

def obtenContexto(nombre_contexto): 

    client = chromadb.PersistentClient(path=CHROMA_PATH)
    # Si el contexto (base) existe la carga la colección con LangChain usando el cliente
    embedding = OllamaEmbeddings(model=TEXT_EMBEDDING_MODEL)
    db = Chroma(
        client=client,
        collection_name=nombre_contexto,
        embedding_function=embedding
    )

    if db._collection.count() > 0:
        print(f"La colección '{nombre_contexto}' existe y tiene {db._collection.count()} documentos.")
    else:
        print(f"La colección '{nombre_contexto}' no existe o está vacía.")

    return db

def crea_contexto(client, nombre_contexto): 
    #Básicamente crear una nueva base es lo mismo que cargarla.

    embedding = OllamaEmbeddings(model=TEXT_EMBEDDING_MODEL)
    
    # Si no existe, LangChain la creará la primera vez que se agregue un documento
    db = Chroma(
        client=client,
        collection_name=nombre_contexto,
        embedding_function=embedding
    )

    return {f"Contexto: {nombre_contexto} creada."}
       

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