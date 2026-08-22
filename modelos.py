"""Registro único de modelos de lenguaje: nombre → proveedor + tarifas.

Es la única fuente de verdad sobre qué modelos existen, quién los sirve y cuánto
cuestan. Antes esa información vivía repartida en cinco lugares que no coincidían
(`globales.modelos`, `herramientas.OPENAI_LLM_MODELS`, el if/else de `chatbot.py`,
el dict `_OPENAI_PRICING` de `app.py` y las constantes del front), con la
consecuencia visible de que había modelos instalados en Ollama que no se podían
asignar a ningún asistente, y modelos ofrecidos en el dropdown que se tarifaban
con el precio de otro.

Vive en `agentes.db` (junto a agentes/proyectos), no en `logs.db`: es
configuración, no telemetría.

Este módulo NO importa `app.py` — al revés. `app.py` y `proveedores.py` lo
consultan.
"""

import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

AGENTES_DB_PATH = os.getenv('AGENTES_DB_PATH', 'agentes.db')

# Default true = comportamiento histórico (el server de CSI tiene Ollama local).
OLLAMA_HABILITADO = os.getenv('OLLAMA_HABILITADO', 'true').strip().lower() not in ('false', '0', 'no')

COLS = "nombre, proveedor, precio_input_usd_1m, precio_output_usd_1m, activo, notas, actualizado_en"

PROVEEDORES_VALIDOS = ('openai', 'ollama', 'anthropic', 'google')

# Tarifas en USD por 1M de tokens, verificadas contra el pricing oficial de cada
# proveedor el 2026-08-07. Este seed corre una sola vez por modelo (INSERT OR
# IGNORE): a partir de ahí las tarifas se editan desde el admin y el seed nunca
# vuelve a pisarlas.
#
# `activo` controla si el modelo aparece en el dropdown del asistente.
# Anthropic y Google entran con activo=0 a propósito: el registro los soporta
# desde el día uno, pero asignarlos sin haber configurado la API key y el
# paquete de LangChain correspondiente sólo produciría errores en runtime.
# Actívalos desde el tab de Tarifas cuando ambos estén listos.
#
# Los de Ollama nacen desactivados cuando OLLAMA_HABILITADO=false, que es el
# caso de cualquier despliegue sin un Ollama alcanzable (p.ej. un droplet
# compartido, donde correr un LLM local no es viable). Sólo afecta al seed de
# una BD nueva: nunca pisa lo que ya hayas editado desde el admin.
_SEED = [
    # nombre, proveedor, input, output, activo, notas
    ('gpt-5.5',       'openai', 5.00,  30.00,  1, 'Verificado 2026-08-07'),
    ('gpt-5.5-pro',   'openai', 30.00, 180.00, 1, 'Verificado 2026-08-07'),
    ('gpt-5',         'openai', 1.25,  10.00,  1, 'Verificado 2026-08-07'),
    ('gpt-5-mini',    'openai', 0.25,  2.00,   1, 'Verificado 2026-08-07'),
    ('gpt-5-nano',    'openai', 0.05,  0.40,   1, 'Verificado 2026-08-07'),
    ('gpt-4o',        'openai', 2.50,  10.00,  1, 'Verificado 2026-08-07'),
    ('gpt-4o-mini',   'openai', 0.15,  0.60,   1, 'Verificado 2026-08-07'),

    # Hardware propio: 0.00 explícito, distinto de NULL/desconocido. Los tokens
    # se siguen registrando porque sirven como medida de carga.
    ('mistral',       'ollama', 0.0, 0.0, 1, 'Local (hardware propio)'),
    ('llama3.1',      'ollama', 0.0, 0.0, 1, 'Local (hardware propio)'),
    ('phi3',          'ollama', 0.0, 0.0, 1, 'Local (hardware propio)'),
    ('gemma:2b',      'ollama', 0.0, 0.0, 1, 'Local (hardware propio)'),

    ('claude-opus-5',   'anthropic', 5.00,  25.00, 0, 'Verificado 2026-08-07. Requiere ANTHROPIC_API_KEY + langchain-anthropic'),
    ('claude-sonnet-5', 'anthropic', 3.00,  15.00, 0, 'Verificado 2026-08-07. Precio introductorio $2/$10 hasta 2026-08-31'),
    ('claude-haiku-4-5','anthropic', 1.00,  5.00,  0, 'Verificado 2026-08-07. Requiere ANTHROPIC_API_KEY + langchain-anthropic'),
    ('claude-fable-5',  'anthropic', 10.00, 50.00, 0, 'Verificado 2026-08-07. Requiere ANTHROPIC_API_KEY + langchain-anthropic'),

    ('gemini-3.6-flash',      'google', 1.50, 7.50,  0, 'Verificado 2026-08-07. Requiere GOOGLE_API_KEY + langchain-google-genai'),
    ('gemini-3.5-flash',      'google', 1.50, 9.00,  0, 'Verificado 2026-08-07. Requiere GOOGLE_API_KEY + langchain-google-genai'),
    ('gemini-3.5-flash-lite', 'google', 0.30, 2.50,  0, 'Verificado 2026-08-07. Requiere GOOGLE_API_KEY + langchain-google-genai'),
    ('gemini-2.5-pro',        'google', 1.25, 10.00, 0, 'Verificado 2026-08-07. Tarifa para prompts <=200k tokens; arriba de eso Google cobra $2/$15'),
    ('gemini-2.5-flash',      'google', 0.30, 2.50,  0, 'Verificado 2026-08-07. Requiere GOOGLE_API_KEY + langchain-google-genai'),
    ('gemini-2.5-flash-lite', 'google', 0.10, 0.40,  0, 'Verificado 2026-08-07. Requiere GOOGLE_API_KEY + langchain-google-genai'),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connection():
    conn = sqlite3.connect(AGENTES_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_modelos_db():
    """Crea la tabla `modelos` y siembra el catálogo inicial.

    El seed usa INSERT OR IGNORE: sólo agrega los que falten. Un precio editado
    desde el admin, o un modelo desactivado a mano, sobrevive a los reinicios.
    """
    conn = sqlite3.connect(AGENTES_DB_PATH)
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS modelos (
            nombre               TEXT PRIMARY KEY,
            proveedor            TEXT NOT NULL,
            precio_input_usd_1m  REAL,
            precio_output_usd_1m REAL,
            activo               INTEGER NOT NULL DEFAULT 1,
            notas                TEXT,
            actualizado_en       TEXT
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_modelos_proveedor ON modelos(proveedor)')
        now = _now()
        for nombre, proveedor, p_in, p_out, activo, notas in _SEED:
            if proveedor == 'ollama' and not OLLAMA_HABILITADO:
                activo = 0
                notas = f'{notas} — desactivado por OLLAMA_HABILITADO=false'
            conn.execute(
                f"INSERT OR IGNORE INTO modelos ({COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (nombre, proveedor, p_in, p_out, activo, notas, now),
            )
        conn.commit()
    finally:
        conn.close()


def obtener(nombre: Optional[str]) -> Optional[dict]:
    """Devuelve la fila del modelo por nombre EXACTO, o None.

    Match exacto a propósito. El esquema anterior hacía longest-prefix match y
    eso no falla ruidosamente, miente: 'gpt-5.5'.startswith('gpt-5') es True, así
    que un modelo de $30/$180 por 1M se reportaba a $1.25/$10 sin ninguna señal
    de que algo estuviera mal.
    """
    if not nombre:
        return None
    conn = _connection()
    try:
        row = conn.execute(
            f"SELECT {COLS} FROM modelos WHERE nombre=?", (nombre,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def listar(solo_activos: bool = False) -> list[dict]:
    conn = _connection()
    try:
        sql = f"SELECT {COLS} FROM modelos"
        if solo_activos:
            sql += " WHERE activo=1"
        sql += " ORDER BY proveedor ASC, nombre ASC"
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def existe(nombre: str, solo_activos: bool = True) -> bool:
    fila = obtener(nombre)
    if fila is None:
        return False
    return bool(fila['activo']) if solo_activos else True


def calcular_costo(nombre: Optional[str], tokens_input, tokens_output):
    """Devuelve (proveedor, costo_usd) para congelar en el log de la consulta.

    `costo_usd` es None ("no se sabe") cuando el modelo no está en el registro,
    no tiene tarifa cargada, o el proveedor no reportó tokens. Nunca se inventa
    un número: la UI muestra "—" y eso es información honesta, mientras que un
    0.00 falso se leería como "gratis".

    Ollama devuelve 0.0, no None: es hardware propio, sí se sabe que no cuesta.
    """
    fila = obtener(nombre)
    if fila is None:
        return None, None

    proveedor = fila['proveedor']
    p_in = fila['precio_input_usd_1m']
    p_out = fila['precio_output_usd_1m']
    if p_in is None or p_out is None:
        return proveedor, None
    if tokens_input is None and tokens_output is None:
        return proveedor, None

    ti = int(tokens_input or 0)
    to = int(tokens_output or 0)
    costo = (ti / 1_000_000) * p_in + (to / 1_000_000) * p_out
    # 6 decimales: una consulta barata con gpt-5-nano cuesta del orden de 1e-5 USD
    # y redondear a 4 la colapsaría a 0.0000.
    return proveedor, round(costo, 6)
