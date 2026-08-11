"""Registro de usuarios finales identificados en los widgets embebidos.

NO es autenticación: los chatbots se embeben en páginas externas sin login.
Esto es una etiqueta que el admin precrea (ej. "Cristian QA", "Bryan PO") para
poder atribuirles consultas en Registros/Consumo, distinguiéndolos de tráfico
anónimo. El widget la recibe una vez por `?usuario=<slug>` en la URL y la
recuerda en localStorage del navegador del usuario final — ver host-asistentes.

Vive en `agentes.db` (junto a proyectos/agentes/modelos), no en `logs.db`: es
configuración, no telemetría.

Scope: por proyecto, no global ni por asistente. Una persona (ej. el QA de un
cliente) suele probar varios asistentes del mismo proyecto; no tiene sentido
recrearla por cada uno. El slug es único dentro de su proyecto, no globalmente
— dos proyectos distintos pueden tener cada uno un "cristian-qa" propio.

Este módulo NO importa `app.py` — al revés. `app.py` lo consulta.
"""

import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

AGENTES_DB_PATH = os.getenv('AGENTES_DB_PATH', 'agentes.db')

COLS = "id, proyecto_id, slug, nombre, activo, notas, creado_en, actualizado_en"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connection():
    conn = sqlite3.connect(AGENTES_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_usuarios_db():
    conn = sqlite3.connect(AGENTES_DB_PATH)
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS usuarios (
            id             TEXT PRIMARY KEY,
            proyecto_id    TEXT NOT NULL,
            slug           TEXT NOT NULL,
            nombre         TEXT NOT NULL,
            activo         INTEGER NOT NULL DEFAULT 1,
            notas          TEXT,
            creado_en      TEXT,
            actualizado_en TEXT,
            UNIQUE(proyecto_id, slug)
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_usuarios_proyecto ON usuarios(proyecto_id)')
        conn.commit()
    finally:
        conn.close()


def listar(proyecto_id: Optional[str] = None, solo_activos: bool = False) -> list[dict]:
    conn = _connection()
    try:
        sql = f"SELECT {COLS} FROM usuarios"
        clauses = []
        params = []
        if proyecto_id:
            clauses.append("proyecto_id=?")
            params.append(proyecto_id)
        if solo_activos:
            clauses.append("activo=1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY nombre ASC"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def obtener(usuario_id: Optional[str]) -> Optional[dict]:
    if not usuario_id:
        return None
    conn = _connection()
    try:
        row = conn.execute(f"SELECT {COLS} FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def resolver(proyecto_id: Optional[str], slug: Optional[str], solo_activos: bool = True) -> Optional[dict]:
    """Busca un usuario por proyecto_id+slug exactos. None si no existe, no
    coincide el proyecto, o está inactivo (con solo_activos=True).

    Nunca inventa: un slug que llega en `?usuario=` y no matchea nada acá
    (typo, proyecto equivocado, usuario borrado) se trata como anónimo, no
    como error — ver el llamador en app.py /chatbot.
    """
    if not proyecto_id or not slug:
        return None
    conn = _connection()
    try:
        row = conn.execute(
            f"SELECT {COLS} FROM usuarios WHERE proyecto_id=? AND slug=?",
            (proyecto_id, slug),
        ).fetchone()
        if not row:
            return None
        fila = dict(row)
        if solo_activos and not fila['activo']:
            return None
        return fila
    finally:
        conn.close()
