import os
import operaciones_chroma
from datetime import datetime
from werkzeug.utils import secure_filename
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import UnstructuredPDFLoader

import chromadb
from langchain_ollama import OllamaEmbeddings
from langchain_chroma import Chroma

TEMP_FOLDER = os.getenv('TEMP_FOLDER', './_temp')
CHROMA_PATH = os.getenv('CHROMA_PATH', 'chroma')
TEXT_EMBEDDING_MODEL = os.getenv('TEXT_EMBEDDING_MODEL')

# Function to check if the uploaded file is allowed (only PDF files)
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'pdf'}

# Function to save the uploaded file to the temporary folder
def save_file(file):
    # Save the uploaded file with a secure filename and return the file path
    ct = datetime.now()
    ts = ct.timestamp()
    filename = str(ts) + "_" + secure_filename(file.filename)
    file_path = os.path.join(TEMP_FOLDER, filename)    

    return file_path

# Function to load and split the data from the PDF file
def load_and_split_data(file_path):
    
    print("Cargando documento...")
    print("Subdividiendo documento...")
    
    # Load the PDF file and split the data into chunks
    loader = UnstructuredPDFLoader(file_path=file_path, language=["eng"])
    print("UnstructuredPDFLoader listo.")
    data = loader.load() #Aquí es donde me pide especificar idioma.
    print("Carga de documento completada.")
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=7500, chunk_overlap=100)
    print("Divisor (splitter) cargado y listo para dividir.")
    chunks = text_splitter.split_documents(data)
    print("Subdivisiones (chunks) listas.")

    return chunks

def embed(file_path, nombre_contexto, current_hash):
    """
    Toma un path de archivo, carga, divide, y embebe el contenido en el contexto elegido.
    """
    
    try:
        chunks = load_and_split_data(file_path)
        if not chunks:
            # Manejar el caso de un archivo vacío o no procesable
            print("No hubo división en chunks...")
            return False
        
        for chunk in chunks:
            chunk.metadata['file_hash'] = current_hash

        db = obtenContexto(nombre_contexto)
        db.add_documents(chunks)

        return True

    except Exception as e:
        print(f"Error durante el proceso de embebido: {e}")
        return False
    
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
        print(f"La colección '{nombre_contexto}' está vacía.")

    return db