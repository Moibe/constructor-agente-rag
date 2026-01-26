import os
from datetime import datetime
from werkzeug.utils import secure_filename
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import UnstructuredPDFLoader
import herramientas
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
    Guarda el nombre del modelo de embedding en las metadatas de cada chunk.
    """
    
    try:
        chunks = load_and_split_data(file_path)
        if not chunks:
            # Manejar el caso de un archivo vacío o no procesable
            print("No hubo división en chunks...")
            return False
        
        # Obtener el nombre del modelo de embedding asociado al contexto
        modelo_embedding = obtener_modelo_embedding_de_contexto(nombre_contexto)
        
        for chunk in chunks:
            chunk.metadata['file_hash'] = current_hash
            if modelo_embedding:
                chunk.metadata['embedding_model_name'] = modelo_embedding

        db = obtenContexto(nombre_contexto)
        db.add_documents(chunks)

        return True

    except Exception as e:
        print(f"Error durante el proceso de embebido: {e}")
        return False


def obtener_modelo_embedding_de_contexto(nombre_contexto: str) -> str | None:
    """
    Intenta obtener el nombre del modelo de embedding asociado a un contexto.
    Primero intenta usar collection.get_settings(), si no funciona, lee del primer documento.
    """
    try:
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        collection = client.get_collection(name=nombre_contexto)
        
        # Intenta get_settings()
        if hasattr(collection, "get_settings"):
            try:
                settings = collection.get_settings()
                if isinstance(settings, dict):
                    modelo = settings.get('metadata', {}).get('embedding_model_name')
                    if modelo:
                        print(f"Modelo obtenido de get_settings(): {modelo}")
                        return modelo
            except Exception:
                pass
        
        # Fallback: leer del primer documento
        try:
            results = collection.get(include=['metadatas'], limit=1)
            if results and results.get('metadatas'):
                first_meta = results['metadatas'][0] or {}
                modelo = first_meta.get('embedding_model_name')
                if modelo:
                    print(f"Modelo obtenido del primer documento: {modelo}")
                    return modelo
        except Exception:
            pass
        
        print(f"No se pudo obtener el modelo de embedding para '{nombre_contexto}'")
        return None
        
    except Exception as e:
        print(f"Error en obtener_modelo_embedding_de_contexto: {e}")
        return None
    
def obtenContexto(nombre_contexto): 

    client = chromadb.PersistentClient(path=CHROMA_PATH)

    print("Esto es el client de contexto: ", client)
    
    modelo_embedding_nombre = herramientas.obtener_modelo_de_embedding_de_coleccion(nombre_contexto, client)
    print("El modelo de embedding recuperado es: ", modelo_embedding_nombre)
    
    # Si no se encuentra el modelo, usar el modelo por defecto (TEXT_EMBEDDING_MODEL de env vars)
    if not modelo_embedding_nombre:
        modelo_embedding_nombre = TEXT_EMBEDDING_MODEL
        if not modelo_embedding_nombre:
            print(f"Error: No se pudo obtener el nombre del modelo de embedding para la colección '{nombre_contexto}' y tampoco existe TEXT_EMBEDDING_MODEL en env vars.")
            return None
        print(f"Usando modelo por defecto: {modelo_embedding_nombre}")

    embedding = OllamaEmbeddings(model=modelo_embedding_nombre)
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