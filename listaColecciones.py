import os
import chromadb

CHROMA_PATH = os.getenv('CHROMA_PATH', 'chroma')

def listaColecciones(): 

    # 1. Conecta al cliente de ChromaDB.
    client = chromadb.PersistentClient(path=CHROMA_PATH)

    # 2. Obtén la lista de colecciones.
    collections = client.list_collections()

    # 3. Extrae los nombres de las colecciones.
    collection_names = [c.name for c in collections]

    return {"collections": collection_names}