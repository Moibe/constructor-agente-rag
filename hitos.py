"""Hitos: marcadores de línea de tiempo para el reporte de Registros.

Un hito es una anotación manual del admin — "aquí entró el cacheo de LLMs",
"aquí bajamos a gpt-5-mini", etc. — con una fecha. El front la usa para
dibujar una línea horizontal a través de la tabla de Registros en el punto
cronológico correcto, para que sea visualmente obvio qué consultas son de
antes/después de un cambio que buscaba ahorrar tiempo o tokens.

Global, no por proyecto: un cacheo de LLM o un cambio de modelo default
aplica a todo el sistema, no a un cliente en particular.

Vive en `agentes.db` (configuración/anotación), no en `logs.db` (telemetría).

Este módulo NO importa `app.py` — al revés. `app.py` lo consulta. No lo usa
ningún endpoint público — solo el admin (`/registros`), a diferencia de
usuarios/modelos que sí necesita leer el host de widgets.
"""

import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

AGENTES_DB_PATH = os.getenv('AGENTES_DB_PATH', 'agentes.db')

COLS = "id, nombre, fecha, notas, creado_en, actualizado_en"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connection():
    conn = sqlite3.connect(AGENTES_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_hitos_db():
    conn = sqlite3.connect(AGENTES_DB_PATH)
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS hitos (
            id             TEXT PRIMARY KEY,
            nombre         TEXT NOT NULL,
            fecha          TEXT NOT NULL,
            notas          TEXT,
            creado_en      TEXT,
            actualizado_en TEXT
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_hitos_fecha ON hitos(fecha)')
        conn.commit()
    finally:
        conn.close()


def listar() -> list[dict]:
    conn = _connection()
    try:
        return [dict(r) for r in conn.execute(f"SELECT {COLS} FROM hitos ORDER BY fecha ASC").fetchall()]
    finally:
        conn.close()


def obtener(hito_id: Optional[str]) -> Optional[dict]:
    if not hito_id:
        return None
    conn = _connection()
    try:
        row = conn.execute(f"SELECT {COLS} FROM hitos WHERE id=?", (hito_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
