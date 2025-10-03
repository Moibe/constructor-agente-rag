#!/bin/bash

# Define la ruta absoluta de la carpeta del proyecto
PROJECT_ROOT="/home/mbriseno/code/mide-chatbot/"

# Navega al directorio del proyecto
cd "$PROJECT_ROOT"

# --- 1. ACTUALIZAR CÓDIGO ---
echo "Iniciando git pull..."
git pull origin main

# --- 2. ACTUALIZAR DEPENDENCIAS (Opcional, si hay cambios) ---
# Si actualizas dependencias en requirements.txt:
source venv/bin/activate
pip install -r requirements.txt
deactivate

# --- 3. REINICIAR PM2 ---
echo "Reiniciando proceso de PM2..."
pm2 restart mide-chatbot-api

echo "✅ Despliegue completado."