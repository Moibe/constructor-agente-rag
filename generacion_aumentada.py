import os
from datetime import datetime
from werkzeug.utils import secure_filename
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import UnstructuredPDFLoader
import herramientas
import chromadb

TEMP_FOLDER = os.getenv('TEMP_FOLDER', './_temp')
CHROMA_PATH = os.getenv('CHROMA_PATH', 'chroma')

# Formatos que la ingesta sabe leer. Cada uno tiene su lector en
# _cargar_documentos(); agregar uno nuevo es agregarlo aquí y allá.
EXTENSIONES_SOPORTADAS = {'.pdf', '.docx', '.txt', '.md'}


def extension_de(filename: str) -> str:
    """Extensión en minúsculas, con punto ('.pdf'); '' si no tiene."""
    return os.path.splitext(filename or '')[1].lower()


def allowed_file(filename):
    """True si la ingesta sabe leer ese formato (ver EXTENSIONES_SOPORTADAS)."""
    return extension_de(filename) in EXTENSIONES_SOPORTADAS

# Function to save the uploaded file to the temporary folder
def save_file(file):
    # Save the uploaded file with a secure filename and return the file path
    ct = datetime.now()
    ts = ct.timestamp()
    filename = str(ts) + "_" + secure_filename(file.filename)
    file_path = os.path.join(TEMP_FOLDER, filename)    

    return file_path

def _leer_texto_plano(file_path):
    """Lee un .txt/.md probando codificaciones; latin-1 nunca falla, así que
    cierra la lista y evita que un acento mal guardado tumbe la ingesta."""
    for encoding in ('utf-8', 'utf-8-sig', 'latin-1'):
        try:
            with open(file_path, encoding=encoding) as archivo:
                return archivo.read()
        except UnicodeDecodeError:
            continue
    return ''


def _leer_docx(file_path):
    """Saca el texto de un .docx: párrafos y tablas, en el orden en que están.

    No lee encabezados, pies de página ni cuadros de texto flotantes; si algún
    día hacen falta, hay que recorrer también section.header/footer.
    """
    import docx

    documento = docx.Document(file_path)
    partes = [parrafo.text.strip() for parrafo in documento.paragraphs if parrafo.text.strip()]

    for tabla in documento.tables:
        for fila in tabla.rows:
            # Una celda combinada aparece repetida en la fila: se filtra por
            # identidad del elemento XML para no duplicar su texto.
            vistas, celdas = set(), []
            for celda in fila.cells:
                if id(celda._tc) in vistas:
                    continue
                vistas.add(id(celda._tc))
                if celda.text.strip():
                    celdas.append(celda.text.strip())
            if celdas:
                partes.append(' | '.join(celdas))

    return '\n'.join(partes)


def _cargar_documentos(file_path):
    """Devuelve los Document del archivo, eligiendo lector por extensión.

    Todos los lectores dejan el mismo metadato 'source' (ver load_and_split_data),
    así que listar_documentos y borrar_documento tratan igual a cualquier formato.
    """
    extension = extension_de(file_path)

    if extension == '.pdf':
        # languages=["spa","eng"] activa OCR: necesita tesseract y poppler en el
        # sistema (ver DEPLOY del server).
        print("[*] Lector: UnstructuredPDFLoader (PDF, con OCR)", flush=True)
        return UnstructuredPDFLoader(file_path=file_path, languages=["spa", "eng"]).load()

    if extension == '.docx':
        # Se lee con python-docx directo, NO con UnstructuredWordDocumentLoader:
        # ese lector revienta con segfault (exit 139) al partir un .docx en este
        # entorno. python-docx es puro Python y saca lo mismo que necesitamos.
        # Extrae párrafos y tablas; lo que esté dentro de imágenes incrustadas
        # no, porque aquí no corre OCR.
        from langchain_core.documents import Document
        print("[*] Lector: python-docx (Word .docx)", flush=True)
        return [Document(page_content=_leer_docx(file_path), metadata={'source': file_path})]

    if extension in ('.txt', '.md'):
        # Texto directo, sin lector externo: mismo camino que embed_text().
        from langchain_core.documents import Document
        print(f"[*] Lector: texto plano ({extension})", flush=True)
        contenido = _leer_texto_plano(file_path)
        return [Document(page_content=contenido, metadata={'source': file_path})]

    # No debería llegar aquí: app.py valida la extensión antes de guardar el
    # archivo. Queda como red por si alguien llama embed() directo.
    raise ValueError(
        f"Formato no soportado: '{extension or 'sin extensión'}'. "
        f"Se aceptan: {', '.join(sorted(EXTENSIONES_SOPORTADAS))}."
    )


def load_and_split_data(file_path, chunk_size=7500, chunk_overlap=100):
    """Carga un documento (PDF, Word, txt o md) y lo parte en chunks."""

    print("Cargando documento...")
    data = _cargar_documentos(file_path)
    print("Carga de documento completada.")

    # El borrado busca por coincidencia EXACTA de 'source' (ver
    # funciones.borrar_documento), así que se fija aquí para los cuatro
    # formatos en vez de confiar en lo que ponga cada lector.
    for documento in data:
        documento.metadata['source'] = file_path

    print("Subdividiendo documento...")
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    print(f"Divisor (splitter) cargado con chunk_size={chunk_size}, overlap={chunk_overlap}.")
    chunks = text_splitter.split_documents(data)
    print("Subdivisiones (chunks) listas.")

    return chunks

def embed_text(content: str, filename: str, nombre_contexto: str, current_hash: str):
    """
    Embebe un snippet de TEXTO PLANO (no PDF) en el contexto. Mismo flujo que
    embed() pero sin pasar por UnstructuredPDFLoader: splitter directo sobre el
    string.

    El `source` que se guarda en metadata es os.path.join(TEMP_FOLDER, filename)
    — mismo formato que usa UnstructuredPDFLoader al cargar un PDF desde
    TEMP_FOLDER. Eso garantiza que funciones.listar_documentos y
    funciones.borrar_documento traten al snippet idéntico que a un PDF.

    Returns:
        dict: {'success': bool, 'message': str, 'error_details': str (opcional)}
    """
    try:
        print("="*50, flush=True)
        print(f"[INICIO] embed_text() para: {nombre_contexto}", flush=True)
        print(f"[*] Filename virtual: {filename}", flush=True)
        print(f"[*] Tamaño contenido: {len(content)} chars", flush=True)
        print("="*50, flush=True)

        chunk_size = herramientas.obtener_chunk_size_de_coleccion(nombre_contexto)
        print(f"[OK] Usando chunk_size={chunk_size}", flush=True)

        source_path = os.path.join(TEMP_FOLDER, filename)
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=100)
        chunks = text_splitter.create_documents(
            [content],
            metadatas=[{"source": source_path}],
        )
        if not chunks:
            return {'success': False, 'message': 'No se pudieron generar chunks del snippet', 'error_details': 'Contenido vacío después del split'}

        print(f"[OK] Se generaron {len(chunks)} chunks", flush=True)

        modelo_embedding = obtener_modelo_embedding_de_contexto(nombre_contexto)
        if not modelo_embedding:
            return {'success': False, 'message': 'No se pudo obtener el modelo de embedding del contexto', 'error_details': f'Contexto: {nombre_contexto}'}

        for chunk in chunks:
            chunk.metadata['file_hash'] = current_hash
            chunk.metadata['embedding_model_name'] = modelo_embedding

        db = herramientas.obtenContexto(nombre_contexto)
        if not db:
            return {'success': False, 'message': 'No se pudo obtener la base de datos del contexto', 'error_details': f'Contexto: {nombre_contexto}'}

        db.add_documents(chunks)
        print(f"[OK] Snippet añadido al contexto {nombre_contexto}", flush=True)

        return {'success': True, 'message': f'Snippet procesado correctamente. {len(chunks)} chunks añadidos.'}

    except Exception as e:
        error_message = f"Error durante el embed del snippet: {str(e)}"
        error_details = f"Tipo: {type(e).__name__}, Contexto: {nombre_contexto}, Filename: {filename}"
        print(f"[ERROR] {error_message}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'message': error_message, 'error_details': error_details}


def embed(file_path, nombre_contexto, current_hash):
    """
    Toma un path de archivo, carga, divide, y embebe el contenido en el contexto elegido.
    Guarda el nombre del modelo de embedding en las metadatas de cada chunk.
    
    Returns:
        dict: {'success': bool, 'message': str, 'error_details': str (opcional)}
    """
    import sys
    
    try:
        print("="*50, flush=True)
        print(f"[INICIO] embed() para: {nombre_contexto}", flush=True)
        print(f"[*] Archivo: {file_path}", flush=True)
        print("="*50, flush=True)
        
        # Obtener el chunk_size asociado al contexto
        print("[...] Obteniendo chunk_size...", flush=True)
        chunk_size = herramientas.obtener_chunk_size_de_coleccion(nombre_contexto)
        print(f"[OK] Usando chunk_size={chunk_size} para el contexto '{nombre_contexto}'", flush=True)
        
        print("[...] Cargando y dividiendo documento...", flush=True)
        chunks = load_and_split_data(file_path, chunk_size)
        if not chunks:
            # Manejar el caso de un archivo vacío o no procesable
            return {'success': False, 'message': 'No se pudieron generar chunks del documento', 'error_details': 'El archivo puede estar vacío o no ser procesable'}
        
        print(f"[OK] Se generaron {len(chunks)} chunks", flush=True)
        
        # DEBUG: Verificar tamaño real de cada chunk
        for i, chunk in enumerate(chunks):
            chunk_length = len(chunk.page_content)
            print(f"[chunk] {i+1}: {chunk_length} caracteres", flush=True)
            if chunk_length > chunk_size + 200:  # Margen de tolerancia
                print(f"[AVISO] Chunk {i+1} EXCEDE el tamano esperado ({chunk_length} > {chunk_size})", flush=True)
        
        max_chunk_size = max(len(chunk.page_content) for chunk in chunks)
        print(f"[*] Chunk mas largo: {max_chunk_size} caracteres", flush=True)
        
        # Obtener el nombre del modelo de embedding asociado al contexto
        print("[...] Obteniendo modelo de embedding...", flush=True)
        modelo_embedding = obtener_modelo_embedding_de_contexto(nombre_contexto)
        if not modelo_embedding:
            return {'success': False, 'message': 'No se pudo obtener el modelo de embedding del contexto', 'error_details': f'Contexto: {nombre_contexto}'}
        
        print(f"[OK] Modelo de embedding obtenido: {modelo_embedding}", flush=True)
        
        print("[...] Agregando metadatos a chunks...", flush=True)
        for chunk in chunks:
            chunk.metadata['file_hash'] = current_hash
            chunk.metadata['embedding_model_name'] = modelo_embedding

        print("[...] Obteniendo contexto de ChromaDB...", flush=True)
        db = herramientas.obtenContexto(nombre_contexto)
        if not db:
            return {'success': False, 'message': 'No se pudo obtener la base de datos del contexto', 'error_details': f'Contexto: {nombre_contexto}'}
        
        print(f"[OK] Contexto obtenido, aniadiendo {len(chunks)} documentos...", flush=True)
        print("[...] Iniciando db.add_documents()...", flush=True)
        db.add_documents(chunks)
        print(f"[OK] Documentos aniadidos exitosamente al contexto {nombre_contexto}", flush=True)

        return {'success': True, 'message': f'Documento procesado correctamente. {len(chunks)} chunks añadidos.'}

    except Exception as e:
        error_message = f"Error durante el proceso de embebido: {str(e)}"
        error_details = f"Tipo: {type(e).__name__}, Contexto: {nombre_contexto}, Archivo: {file_path}"
        print(f"[ERROR] {error_message}")
        print(f"[ERROR] {error_details}")
        import traceback
        traceback.print_exc()
        
        return {
            'success': False, 
            'message': error_message,
            'error_details': error_details
        }


def obtener_modelo_embedding_de_contexto(nombre_contexto: str) -> str | None:
    """
    Obtiene el nombre del modelo de embedding asociado a un contexto desde collection.metadata.
    """
    try:
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        collection = client.get_collection(name=nombre_contexto)
        
        # Obtener desde collection metadata
        try:
            metadata = collection.metadata
            if metadata and 'embedding_model_name' in metadata:
                modelo = metadata['embedding_model_name']
                print(f"[OK] Modelo obtenido de metadatos de coleccion: {modelo}")
                return modelo
            else:
                print(f"[ERROR] No se encontro 'embedding_model_name' en metadatos de coleccion '{nombre_contexto}'")
                print(f"[ERROR] Metadatos disponibles: {metadata}")
        except Exception as e:
            print(f"[ERROR] Error obteniendo metadata de coleccion '{nombre_contexto}': {e}")
        
        print(f"[ERROR] No se pudo obtener el modelo de embedding para '{nombre_contexto}'")
        return None
        
    except Exception as e:
        print(f"[ERROR] Error en obtener_modelo_embedding_de_contexto: {e}")
        return None