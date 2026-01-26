#!/bin/bash

# Define la ruta absoluta de la carpeta del proyecto
PROJECT_ROOT="/home/mbriseno/code/mide-chatbot/"

# Navega al directorio del proyecto
cd "$PROJECT_ROOT" || exit 1

echo "📍 Directorio actual: $(pwd)"
echo "⏰ Inicio del despliegue: $(date)"

# --- 1. ACTUALIZAR CÓDIGO ---
echo "🔄 Iniciando git pull..."
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "📌 Rama actual: $CURRENT_BRANCH"
git pull origin "$CURRENT_BRANCH" || { echo "❌ Error en git pull"; exit 1; }

# --- 2. ACTUALIZAR DEPENDENCIAS ---
echo "📦 Actualizando dependencias..."
source venv/bin/activate
pip install -r requirements.txt --quiet
deactivate

# --- 3. REINICIAR PM2 ---
echo "🔄 Reiniciando proceso de PM2..."
pm2 restart mide-chatbot-api || { echo "❌ Error restarting PM2"; exit 1; }

echo "✅ Despliegue completado exitosamente."
echo "⏰ Fin del despliegue: $(date)"