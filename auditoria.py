"""Bitácora de administración: quién hizo qué y cuándo.

Responde la pregunta que el `ADMIN_PASSWORD` compartido nunca pudo responder:
*¿quién borró este proyecto?*. Cada acción de escritura que pasa por una
credencial de admin deja un renglón aquí.

Vive en `logs.db` (telemetría, junto a `chat_logs`), no en `agentes.db`: es
historia de lo que pasó, no configuración. Por eso el email y el nombre del
operador se guardan **denormalizados**, igual que `proyecto_slug` o
`usuario_nombre` en `chat_logs`: si mañana esa persona se renombra o se borra,
el renglón histórico debe seguir diciendo cómo se llamaba cuando actuó.

Es **append-only**: no hay update ni delete. Una bitácora que se puede editar no
sirve para lo que existe.

Límites conocidos, para no leerla como si fuera completa:
- Sólo registra lo que pasa por un endpoint protegido. Crear o borrar asistentes
  y bases de conocimiento hoy es público (sin credencial), así que esas acciones
  NO aparecen aquí — no porque nadie las haya hecho, sino porque el servidor no
  sabe quién fue.
- Con el `ADMIN_PASSWORD` legacy queda `credencial='legacy'` y sin email: se sabe
  que alguien con el token compartido lo hizo, no quién.

Este módulo NO importa `app.py` — al revés.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

LOG_DB_PATH = os.getenv('LOG_DB_PATH', 'logs.db')

COLS = ("id, fecha, operador_id, operador_email, operador_nombre, credencial, "
        "accion, entidad, entidad_id, resumen, detalle")

# Verbos de las acciones. No es una restricción de la BD (una bitácora no debe
# rechazar un renglón por un verbo nuevo), sino la lista que el front usa para
# armar su filtro.
ACCIONES = ('crear', 'actualizar', 'borrar', 'password', 'sincronizar')
ENTIDADES = ('proyecto', 'agente', 'operador', 'usuario', 'modelo', 'hito', 'api_keys')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_auditoria_db():
    conn = sqlite3.connect(LOG_DB_PATH)
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS auditoria (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha           TEXT,
            operador_id     TEXT,
            operador_email  TEXT,
            operador_nombre TEXT,
            credencial      TEXT,
            accion          TEXT,
            entidad         TEXT,
            entidad_id      TEXT,
            resumen         TEXT,
            detalle         TEXT
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_auditoria_fecha ON auditoria(fecha)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_auditoria_entidad ON auditoria(entidad)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_auditoria_operador ON auditoria(operador_email)')
        conn.commit()
    finally:
        conn.close()


def registrar(identidad: Optional[dict], accion: str, entidad: str,
              entidad_id: Optional[str] = None, resumen: str = "",
              detalle: Optional[dict] = None) -> None:
    """Anota una acción. Nunca levanta: que falle la bitácora no debe tumbar la
    operación que el usuario pidió (misma política que el log de `/chatbot`).

    `identidad` es lo que devuelve `require_admin` en app.py.
    """
    try:
        operador = (identidad or {}).get("operador") or {}
        conn = sqlite3.connect(LOG_DB_PATH)
        try:
            conn.execute(
                """INSERT INTO auditoria
                   (fecha, operador_id, operador_email, operador_nombre, credencial,
                    accion, entidad, entidad_id, resumen, detalle)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (_now(), operador.get("id"), operador.get("email"), operador.get("nombre"),
                 (identidad or {}).get("tipo"), accion, entidad, entidad_id, resumen,
                 json.dumps(detalle, ensure_ascii=False) if detalle else None),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[AUDITORIA] No se pudo registrar '%s %s': %s", accion, entidad, e)


def listar(desde: Optional[str] = None, hasta_exclusivo: Optional[str] = None,
           operador: Optional[str] = None, entidad: Optional[str] = None,
           accion: Optional[str] = None, limit: int = 50, offset: int = 0) -> dict:
    """Renglones más recientes primero, con el total para paginar."""
    where, params = [], []
    if desde:
        where.append("fecha >= ?"); params.append(desde)
    if hasta_exclusivo:
        where.append("fecha < ?"); params.append(hasta_exclusivo)
    if operador:
        where.append("operador_email = ?"); params.append(operador)
    if entidad:
        where.append("entidad = ?"); params.append(entidad)
    if accion:
        where.append("accion = ?"); params.append(accion)
    sql_where = " AND ".join(where) if where else "1=1"

    conn = sqlite3.connect(LOG_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM auditoria WHERE {sql_where}", params
        ).fetchone()["c"]
        filas = conn.execute(
            f"SELECT {COLS} FROM auditoria WHERE {sql_where} ORDER BY fecha DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        # Para poblar el filtro "quién" sólo con gente que de verdad aparece.
        operadores = [
            r["operador_email"] for r in
            conn.execute("SELECT DISTINCT operador_email FROM auditoria WHERE operador_email IS NOT NULL ORDER BY operador_email").fetchall()
        ]
    finally:
        conn.close()

    items = []
    for r in filas:
        d = dict(r)
        if d.get("detalle"):
            try:
                d["detalle"] = json.loads(d["detalle"])
            except (ValueError, TypeError):
                pass  # se queda como texto; un detalle ilegible no invalida el renglón
        items.append(d)

    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "operadores": operadores,
        "acciones": list(ACCIONES),
        "entidades": list(ENTIDADES),
    }
