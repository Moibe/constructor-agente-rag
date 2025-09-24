import os
from datetime import datetime
from werkzeug.utils import secure_filename
from langchain_community.document_loaders import UnstructuredPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from get_vector_db import get_vector_db
from langchain_ollama import OllamaEmbeddings
import time 

TEMP_FOLDER = os.getenv('TEMP_FOLDER', './_temp')

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
    file.save(file_path)

    return file_path

# Function to load and split the data from the PDF file
def load_and_split_data(file_path):
    print("Estoy en load and split...")
    
    # Load the PDF file and split the data into chunks
    loader = UnstructuredPDFLoader(file_path=file_path, language="spanish")
    print("Loader listo...")
    data = loader.load()
    print("Data listo...")
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=7500, chunk_overlap=100)
    print("Text splitter listo...")
    chunks = text_splitter.split_documents(data)
    print("Chunks listos...")

    return chunks

def embed(file_path, collection_name):
    """
    Toma un path de archivo, carga, divide, y embebe el contenido en la DB.
    """
    print("Estoy en función embed...")
    
    try:
        print("Estoy en try de embed...")
        chunks = load_and_split_data(file_path)
        if not chunks:
            # Manejar el caso de un archivo vacío o no procesable
            print("No hubo chunks...")
            return False

        db = get_vector_db(collection_name)
        db.add_documents(chunks)
        #db.persist()

        return True

    except Exception as e:
        print(f"Error during embedding process: {e}")
        return False