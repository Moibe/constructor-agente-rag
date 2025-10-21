import os
from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings

CHROMA_PATH = os.getenv('CHROMA_PATH', 'chroma')
TEXT_EMBEDDING_MODEL = os.getenv('TEXT_EMBEDDING_MODEL') #Antes de pasarlo como parámetro se obtenia de env vars.

def crea_contexto(client, nombre_contexto, embedding_model): 
    #Básicamente crear una nueva base es lo mismo que cargarla.

    embedding = OllamaEmbeddings(model=embedding_model)
    
    # Si no existe, LangChain la creará la primera vez que se agregue un documento
    db = Chroma(
        client=client,
        collection_name=nombre_contexto,
        embedding_function=embedding
    )

    return {f"Contexto: {nombre_contexto} creado."}
       

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