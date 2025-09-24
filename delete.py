import os
import chromadb

def delete(collection_name):
    
    CHROMA_PATH = os.getenv('CHROMA_PATH', 'chroma')

    # 1. Conecta al cliente de ChromaDB.
    client = chromadb.PersistentClient(path=CHROMA_PATH)

    # 2. Usa el método delete_collection para eliminar la colección.
    client.delete_collection(name=collection_name)