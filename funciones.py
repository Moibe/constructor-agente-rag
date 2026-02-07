import os
import chromadb
from typing import Dict
from pathlib import Path
import operaciones_chroma
from typing import Dict, List
import globales
import generacion_aumentada
import herramientas

CHROMA_PATH = os.getenv('CHROMA_PATH', 'chroma')

def listar_contextos(): 

    client = chromadb.PersistentClient(path=CHROMA_PATH)

    #Obtener la lista de contextos.
    contextos_existentes = client.list_collections()

    #Extrae los nombres de las colecciones.
    resultado = [c.name for c in contextos_existentes]

    return resultado

def listar_contextos_con_conteo() -> Dict[str, dict]:
    """
    Lista todos los contextos existentes con la cantidad de documentos 
    únicos cargados y el modelo de embedding de cada uno.
    
    Returns:
        Un diccionario en formato {nombre_coleccion: {"documentos": int, "embedding_model": str}}.
    """
    try:
        # 1. Conecta al cliente persistente de ChromaDB.
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        contextos_existentes = client.list_collections()
        print(f"Contextos encontrados: {[c.name for c in contextos_existentes]}")
        
        conteo_por_contexto: Dict[str, dict] = {}

        # 2. Iterar sobre las colecciones
        for collection_object in contextos_existentes:
            nombre_contexto = collection_object.name
            
            try:
                # 3. Obtener el modelo de embedding para este contexto
                modelo_embedding = herramientas.obtener_modelo_de_embedding_de_coleccion(nombre_contexto, client)
                if not modelo_embedding:
                    modelo_embedding = os.getenv('TEXT_EMBEDDING_MODEL', 'desconocido')
                
                # 3b. Obtener el chunk_size para este contexto
                chunk_size = herramientas.obtener_chunk_size_de_coleccion(nombre_contexto)
                
                # 4. Llamar a la función existente: listar_documentos
                # Esta función devuelve una lista[str] de nombres únicos o un mensaje de error (str).
                resultado_listado = listar_documentos(nombre_contexto)
                
                # 5. Manejar el resultado
                if isinstance(resultado_listado, List):
                    # Si es una lista (de nombres únicos), contamos su longitud
                    cantidad_documentos_unicos = len(resultado_listado)
                else:
                    # Si es un mensaje de error (como "La base está vacía."), el conteo es 0.
                    cantidad_documentos_unicos = 0
                
                # 6. Guardar conteo y modelo en el resultado
                conteo_por_contexto[nombre_contexto] = {
                    "documentos": cantidad_documentos_unicos,
                    "embedding_model": modelo_embedding,
                    "chunk_size": chunk_size
                }
                print(f"Contexto '{nombre_contexto}': {cantidad_documentos_unicos} docs, modelo: {modelo_embedding}, chunk_size: {chunk_size}")
            except Exception as e:
                print(f"Error procesando contexto '{nombre_contexto}': {e}")
                # Incluir el contexto con valores por defecto si hay error
                conteo_por_contexto[nombre_contexto] = {
                    "documentos": 0,
                    "embedding_model": "error",
                    "chunk_size": 0
                }

        return conteo_por_contexto

    except Exception as e:
        print(f"Error al listar contextos con conteo: {e}")
        import traceback
        traceback.print_exc()
        return {}

def crear_contexto(nombre_contexto, embedding_model, chunk_size=7500): 
    
    client = chromadb.PersistentClient(path=CHROMA_PATH)

    if operaciones_chroma.contexto_existe(client, nombre_contexto):
        #Si el contexto existe solo avísa: 
        return {"Mensaje": f"El contexto que quieres crear: {nombre_contexto} ya existe."}
    else:
        #No existe
        db = operaciones_chroma.crea_contexto(client, nombre_contexto, embedding_model, chunk_size)

    return db

def listar_documentos(contexto: str) -> list[str]:
    """
    Lista todos los nombres únicos de archivos (basados en el metadato 'source') 
    en una colección dada.
    """

    try:

        if existe_contexto(contexto): 
            #print("La base si existe, continuar...")
            db = generacion_aumentada.obtenContexto(contexto) #Indicar si no hubo contexto porque no existe.
            #print("Se obtuvo el contexto: ", db)
            collection = db._collection
            print("Se obtuvo el contexto: ", collection)

            # Obtener todos los documentos, pero solo necesitamos los metadatos.
            # El include=['metadatas'] lo hace eficiente.
            results = collection.get(
                include=['metadatas']
            )

            
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
        
        else: 
            return "La base está vacía."       

    except Exception as e:
        print(f"Error al listar documentos: {e}")
        return []
    
def delete_contexto(collection_name):
    
    CHROMA_PATH = os.getenv('CHROMA_PATH', 'chroma')

    # 1. Conecta al cliente de ChromaDB.
    client = chromadb.PersistentClient(path=CHROMA_PATH)

    # 2. Usa el método delete_collection para eliminar la colección.
    client.delete_collection(name=collection_name)
    

def borrar_documento(contexto: str, filename: str) -> int:
    """
    Elimina todos los fragmentos (chunks) asociados a un nombre de archivo (filename) 
    de una colección específica en ChromaDB, utilizando el metadato 'source'.

    Args:
        contexto: El nombre de la colección de donde eliminar.
        filename: El nombre del archivo a eliminar (ej. 'mis_faqs.pdf').

    Returns:
        El número de documentos (chunks) eliminados.
    """

    TEMP_FOLDER = os.getenv('TEMP_FOLDER', './_temp')

    try:
        db = generacion_aumentada.obtenContexto(contexto)
        collection = db._collection

        # 1. Reconstruir la RUTA EXACTA que LangChain guardó en el metadato 'source'.
        exact_file_path = os.path.join(TEMP_FOLDER, filename)

        print("\n=== DEBUG BORRAR DOCUMENTO ===")
        print(f"Buscando para eliminar: {exact_file_path}")

        # --- PASO DE VISTA PREVIA (SELECT) ---
        documents_to_delete = collection.get(
            where={"source": exact_file_path},
            include=['metadatas', 'documents'] 
        )
        
        preview_count = len(documents_to_delete.get('ids', []))

        print(f"\n--- INFORME DE ELIMINACIÓN ---")
        print(f"Colección: {contexto}")
        print(f"Filtro de Metadato (Source): {exact_file_path}")
        print(f"Documentos (chunks) ENCONTRADOS: {preview_count}")
        
        # DEBUG: Ver todos los 'source' que existen en la colección
        if preview_count == 0:
            print("\n⚠️ NO ENCONTRÓ DOCUMENTOS CON ESE SOURCE")
            print("Mostrando todos los sources en la colección:")
            all_docs = collection.get(
                include=['metadatas'],
                limit=1000
            )
            sources_encontradas = set()
            for meta in all_docs.get('metadatas', []):
                if 'source' in meta:
                    sources_encontradas.add(meta['source'])
            for source in sources_encontradas:
                print(f"  - {source}")
        
        if preview_count > 0:
            # Imprimir el contenido del primer documento para doble verificación
            print(f"   >>> PREVIEW (Primer chunk): {documents_to_delete['documents'][0][:100]}...")
            print(f"   >>> ID: {documents_to_delete['ids'][0]}")

        # 3. Obtener el conteo antes de la eliminación
        initial_count = collection.count()

        # 4. Realizar la eliminación usando el filtro de metadatos 'where'
        collection.delete(
            where={"source": exact_file_path}
        )
        
        # 5. Calcular los eliminados
        final_count = collection.count()
        deleted_count = initial_count - final_count

        print(f"\nConteo inicial: {initial_count}")
        print(f"Conteo final: {final_count}")
        print(f"Eliminados REALMENTE: {deleted_count}")
        print("=== FIN DEBUG ===\n")

        return deleted_count

    except Exception as e:
        print(f"Error en borrar_documento: {e}")
        import traceback
        traceback.print_exc()
        return 0
    
def existe_contexto(contexto: str): 

    resultados = listar_contextos()

    if contexto in resultados:
        return True
    else:
        return False 
    
def existe_modelo(modelo: str): 

    if modelo in globales.modelos:
        return True
    else:
        return False 