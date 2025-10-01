import os
import chromadb
from typing import Dict
from pathlib import Path
import operaciones_chroma
import time

CHROMA_PATH = os.getenv('CHROMA_PATH', 'chroma')

def listar_bases_conocimiento(): 

    # 1. Conecta al cliente de ChromaDB.
    client = chromadb.PersistentClient(path=CHROMA_PATH)

    # 2. Obtén la lista de colecciones.
    bases_existentes = client.list_collections()

    # 3. Extrae los nombres de las colecciones.
    resultado = [c.name for c in bases_existentes]

    return {"Bases de conocimiento existentes para éste chatbot": resultado}

def crear_base(nombre_base): 
    
    client = chromadb.PersistentClient(path=CHROMA_PATH)

    if operaciones_chroma.base_existe(client, nombre_base):
        #Si la base existe, solo avisa que la base existe: 
        return {"Message": f"La base {nombre_base} ya existe."}
    else:
        #O sea si no existe
        db = operaciones_chroma.crea_base(client, nombre_base)

    return db

def listar_documentos(base_conocimiento: str) -> list[str]:
    """
    Lista todos los nombres únicos de archivos (basados en el metadato 'source') 
    en una colección dada.
    """

    print("Estoy en list document names...")
    try:
        db = operaciones_chroma.obtenBase(base_conocimiento)
        print("Se obtuvo la base: ", db)
        collection = db._collection
        print("Se obtuvo la colección: ", collection)

        # Obtener todos los documentos, pero solo necesitamos los metadatos.
        # El include=['metadatas'] lo hace eficiente.
        results = collection.get(
            include=['metadatas']
        )

        print("Esto es results de listar los documentos: ")
        print(results)
        
        # 1. Extraer los metadatos
        all_metadatas = results.get('metadatas', [])        
        
        # 2. Obtener todas las rutas de archivo guardadas en la clave 'source'
        source_paths = [
            m['source'] for m in all_metadatas if 'source' in m
        ]
        
        # 3. Extraer solo el nombre del archivo (basename) y asegurarse de que sean únicos
        unique_filenames = set()
        
        for path in source_paths:
            # path.split('/')[-1] extrae el nombre del archivo de la ruta
            # os.path.basename también sirve, pero debemos manejar las barras
            
            # Usaremos Pathlib para un manejo robusto de rutas en diferentes OS
            file_name = Path(path).name
            unique_filenames.add(file_name)
            
        return sorted(list(unique_filenames))

    except Exception as e:
        print(f"Error al listar documentos: {e}")
        return []
    

def borrar_documento(base_conocimiento: str, filename: str) -> int:
    """
    Elimina todos los fragmentos (chunks) asociados a un nombre de archivo (filename) 
    de una colección específica en ChromaDB, utilizando el metadato 'source'.

    Args:
        base_conocimiento: El nombre de la colección de donde eliminar.
        filename: El nombre del archivo a eliminar (ej. 'mis_faqs.pdf').

    Returns:
        El número de documentos (chunks) eliminados.
    """

    TEMP_FOLDER = os.getenv('TEMP_FOLDER', './_temp')

    try:
        db = operaciones_chroma.obtenBase(base_conocimiento)
        collection = db._collection

        # 1. Reconstruir la RUTA EXACTA que LangChain guardó en el metadato 'source'.
        # Es vital que coincida. Usamos .replace('\\', '/') para asegurar que 
        # las barras sean consistentes (LangChain/Linux-style).
        #exact_file_path = os.path.join(TEMP_FOLDER, filename).replace('\\', '/')
        #exact_file_path = TEMP_FOLDER + "\\" + filename
        exact_file_path = os.path.join(TEMP_FOLDER, filename)

        print("Ruta reconstruida: ", exact_file_path)

        # 2. Definir el filtro de metadatos (asumimos que la clave es 'source')
        where_filter: Dict[str, str] = {
            "source": exact_file_path
        }


         # --- PASO DE VISTA PREVIA (SELECT) ---
        documents_to_delete = collection.get(
            where=where_filter,
            # Solo necesitamos los IDs y metadatos para la confirmación, no los embeddings
            include=['metadatas', 'documents'] 
        )
        
        preview_count = len(documents_to_delete.get('ids', []))

        print("\n--- INFORME DE ELIMINACIÓN ---")
        print(f"Colección: {base_conocimiento}")
        print(f"Filtro de Metadato (Source): {exact_file_path}")
        print(f"Documentos (chunks) ENCONTRADOS para eliminar: {preview_count}")
        
        if preview_count > 0:
            # Imprimir el contenido del primer documento para doble verificación
            print(f"   >>> PREVIEW (Primer chunk): {documents_to_delete['documents'][0][:100]}...")
            print(f"   >>> ID: {documents_to_delete['ids'][0]}")

        
        # 3. Obtener el conteo antes de la eliminación
        initial_count = collection.count()

        # 4. Realizar la eliminación usando el filtro de metadatos 'where'
        collection.delete(
            where=where_filter
        )
        
        # 5. Calcular los eliminados
        final_count = collection.count()
        print("El final countdown es: ", final_count)
        deleted_count = initial_count - final_count

        return deleted_count

    except Exception as e:
        print(f"Error en borrar_documento: {e}")
        # En caso de error, retornamos 0 o levantamos una excepción según la gestión de errores deseada
        return 0