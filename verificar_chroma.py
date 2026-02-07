#!/usr/bin/env python3
"""Script para verificar el estado de ChromaDB local"""

import chromadb
import os

CHROMA_PATH = os.getenv('CHROMA_PATH', 'chroma')

print("=== VERIFICACIÓN DE CHROMA ===")
print(f"Ruta: {CHROMA_PATH}")
print()

try:
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    
    # Listar todas las colecciones
    contextos = client.list_collections()
    
    print(f"Total de contextos (colecciones): {len(contextos)}")
    print()
    
    if len(contextos) == 0:
        print("✅ SÍ, TODO ESTÁ VACÍO - No hay colecciones")
    else:
        print("⚠️  AÚN HAY COLECCIONES:")
        print("-" * 60)
        
        total_documentos = 0
        for col in contextos:
            count = col.count()
            total_documentos += count
            print(f"  • {col.name}")
            print(f"    └─ Documentos: {count}")
        
        print("-" * 60)
        print(f"\n📊 TOTALES:")
        print(f"   Colecciones: {len(contextos)}")
        print(f"   Documentos totales: {total_documentos}")
        
except Exception as e:
    print(f"❌ Error al acceder a ChromaDB: {e}")
    import traceback
    traceback.print_exc()
