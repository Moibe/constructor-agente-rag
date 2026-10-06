"""Operadores: las personas que entran al panel de administración, con login propio.

**No confundir con `usuarios.py`.** Un *usuario* es una etiqueta de usuario final
(ej. "Cristian QA") que el widget manda en la URL para atribuir consultas: no
tiene contraseña y no entra a ningún lado. Un *operador* sí: tiene contraseña,
sesión y un rol que decide qué puede hacer.

Convive con el `ADMIN_PASSWORD` de siempre, que sigue valiendo como superadmin
sin identidad (ver `identidad_actual` en app.py). La idea es poder apagarlo
cuando todos los que entran tengan su cuenta, sin que nadie se quede fuera
mientras tanto.

Roles previstos (hoy solo se usa 'superadmin'):
- superadmin → todo, incluido administrar operadores.
- admin      → opera el sistema, pero no crea ni borra operadores.
- cliente    → pensado para que vea sólo sus proyectos. El filtrado por proyecto
               NO está implementado todavía: un 'cliente' hoy pasaría los mismos
               chequeos que un admin, así que no des de alta cuentas con ese rol
               hasta que exista. A propósito no hay columna `proyecto_id` acá:
               cuando toque, la relación va en su propia tabla operador↔proyectos
               (N:M), porque una persona puede llevar varios proyectos.

Sesiones: token opaco guardado **hasheado**. El token en claro sólo existe en la
respuesta del login y en el navegador — si alguien se roba `agentes.db` no se
lleva sesiones vivas. Nada de JWT: así una sesión se puede revocar de verdad
(logout, cambio de contraseña, cuenta desactivada) sin dependencias nuevas.

Vive en `agentes.db` (configuración), no en `logs.db` (telemetría).

Este módulo NO importa `app.py` — al revés. `app.py` lo consulta.
"""

import hashlib
import logging
import os
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt

logger = logging.getLogger(__name__)

AGENTES_DB_PATH = os.getenv('AGENTES_DB_PATH', 'agentes.db')

COLS = "id, email, nombre, password_hash, rol, activo, creado_en, actualizado_en"
# Lo que sí puede salir en una respuesta HTTP: todo menos el hash.
COLS_PUBLICAS = ("id", "email", "nombre", "rol", "activo", "creado_en", "actualizado_en")

ROLES = ('superadmin', 'admin', 'cliente')

# Lo que hoy se puede asignar desde el panel. 'cliente' queda fuera a propósito:
# el filtrado por proyecto NO existe todavía, así que una cuenta con ese rol
# tendría de hecho los mismos permisos que un admin — justo lo contrario de lo
# que su nombre promete. Se abre cuando el scoping por proyecto esté hecho.
ROLES_ASIGNABLES = ('superadmin', 'admin')

# bcrypt 5 levanta ValueError arriba de 72 bytes en vez de truncar en silencio,
# así que el límite se valida antes y el usuario recibe un mensaje claro.
PASSWORD_MAX_BYTES = 72
PASSWORD_MIN_LEN = 8

# Sesión larga y deslizante: esto es un panel interno que se usa a diario, y
# cada login cuesta un bcrypt. Se renueva sola mientras se use.
SESION_DIAS = 30

# Freno al tanteo de contraseñas. En memoria del proceso a propósito: se borra
# al reiniciar y no ensucia la BD. No pretende parar un ataque distribuido —
# para eso haría falta algo por IP en el reverse proxy — pero sí hace inútil
# probar miles de contraseñas contra una cuenta desde una pestaña.
INTENTOS_MAX = 5
BLOQUEO_S = 60
_intentos: dict = {}  # email → [fallos consecutivos, epoch del último fallo]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connection():
    conn = sqlite3.connect(AGENTES_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_operadores_db():
    conn = sqlite3.connect(AGENTES_DB_PATH)
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS operadores (
            id             TEXT PRIMARY KEY,
            email          TEXT NOT NULL,
            nombre         TEXT NOT NULL,
            password_hash  TEXT NOT NULL,
            rol            TEXT NOT NULL,
            activo         INTEGER NOT NULL DEFAULT 1,
            creado_en      TEXT,
            actualizado_en TEXT
        )''')
        # El email es la credencial: único sin importar mayúsculas (se guarda
        # normalizado a minúsculas) para que no existan dos cuentas que se
        # escriben igual y entran distinto.
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_operadores_email ON operadores(email)')

        conn.execute('''CREATE TABLE IF NOT EXISTS sesiones (
            token_hash  TEXT PRIMARY KEY,
            operador_id TEXT NOT NULL,
            creado_en   TEXT,
            expira_en   TEXT,
            ultimo_uso  TEXT
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_sesiones_operador ON sesiones(operador_id)')
        conn.commit()
    finally:
        conn.close()


# ─── Contraseñas ────────────────────────────────────────────────

class PasswordInvalida(ValueError):
    """La contraseña no cumple el formato (vacía, corta o demasiado larga)."""


def validar_password(password: str) -> str:
    p = password or ""
    if len(p) < PASSWORD_MIN_LEN:
        raise PasswordInvalida(f"La contraseña debe tener al menos {PASSWORD_MIN_LEN} caracteres.")
    if len(p.encode('utf-8')) > PASSWORD_MAX_BYTES:
        raise PasswordInvalida(
            f"La contraseña no puede pasar de {PASSWORD_MAX_BYTES} bytes "
            "(bcrypt no admite más; los acentos y emojis cuentan doble o más)."
        )
    return p


def hashear(password: str) -> str:
    return bcrypt.hashpw(validar_password(password).encode('utf-8'), bcrypt.gensalt()).decode('ascii')


def _verificar_hash(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw((password or "").encode('utf-8'), password_hash.encode('ascii'))
    except (ValueError, TypeError):
        # Hash corrupto o contraseña arriba de 72 bytes: no es válida, pero no
        # es razón para tumbar el request con un 500.
        return False


def normalizar_email(email: str) -> str:
    return (email or "").strip().lower()


# ─── Operadores ─────────────────────────────────────────────────

def _publico(fila: Optional[dict]) -> Optional[dict]:
    """La fila sin el hash de la contraseña. Todo lo que sale a HTTP pasa por aquí."""
    if fila is None:
        return None
    d = {k: fila[k] for k in COLS_PUBLICAS}
    d["activo"] = bool(d["activo"])
    return d


def contar(rol: Optional[str] = None) -> int:
    conn = _connection()
    try:
        if rol:
            return conn.execute("SELECT COUNT(*) FROM operadores WHERE rol=?", (rol,)).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM operadores").fetchone()[0]
    finally:
        conn.close()


def obtener(operador_id: str) -> Optional[dict]:
    conn = _connection()
    try:
        row = conn.execute(f"SELECT {COLS} FROM operadores WHERE id=?", (operador_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def obtener_por_email(email: str) -> Optional[dict]:
    conn = _connection()
    try:
        row = conn.execute(
            f"SELECT {COLS} FROM operadores WHERE email=?", (normalizar_email(email),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def listar() -> list:
    conn = _connection()
    try:
        rows = conn.execute(f"SELECT {COLS} FROM operadores ORDER BY creado_en ASC").fetchall()
        return [_publico(dict(r)) for r in rows]
    finally:
        conn.close()


class EmailDuplicado(ValueError):
    """Ya existe un operador con ese email."""


def crear(email: str, nombre: str, password: str, rol: str = 'admin') -> dict:
    if rol not in ROLES:
        raise ValueError(f"rol inválido: {rol!r}. Válidos: {', '.join(ROLES)}.")
    correo = normalizar_email(email)
    if not correo or '@' not in correo:
        raise ValueError("email inválido.")
    if not (nombre or "").strip():
        raise ValueError("nombre no puede estar vacío.")

    password_hash = hashear(password)  # valida el formato antes de tocar la BD
    oid = uuid.uuid4().hex
    now = _now()
    conn = _connection()
    try:
        try:
            conn.execute(
                f"INSERT INTO operadores ({COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (oid, correo, nombre.strip(), password_hash, rol, 1, now, now),
            )
        except sqlite3.IntegrityError:
            raise EmailDuplicado(f"Ya existe un operador con el email '{correo}'.")
        conn.commit()
        row = conn.execute(f"SELECT {COLS} FROM operadores WHERE id=?", (oid,)).fetchone()
        return _publico(dict(row))
    finally:
        conn.close()


class UltimoSuperadmin(Exception):
    """La operación dejaría al sistema sin ningún superadmin activo."""


def superadmins_activos() -> int:
    conn = _connection()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM operadores WHERE rol='superadmin' AND activo=1"
        ).fetchone()[0]
    finally:
        conn.close()


def _proteger_ultimo_superadmin(fila: dict, rol_nuevo: str, activo_nuevo: bool):
    """Impide quedarse sin superadmin activo.

    Sin esto, bajarse el rol a uno mismo o desactivar la única cuenta deja el
    panel sin quién administre operadores — y con el ADMIN_PASSWORD apagado,
    sin forma de entrar a arreglarlo salvo metiéndose a la BD por SSH.
    """
    era_super_activo = fila['rol'] == 'superadmin' and bool(fila['activo'])
    sigue_super_activo = rol_nuevo == 'superadmin' and activo_nuevo
    if era_super_activo and not sigue_super_activo and superadmins_activos() <= 1:
        raise UltimoSuperadmin(
            "Es el único superadmin activo. Crea o activa otro antes de cambiarle el rol, "
            "desactivarlo o borrarlo."
        )


def actualizar(operador_id: str, nombre: Optional[str] = None,
               rol: Optional[str] = None, activo: Optional[bool] = None) -> dict:
    """Cambia nombre, rol y/o estado. El email y la contraseña van por otro lado:
    el email es la credencial (se cambia creando otra cuenta) y la contraseña
    tiene su propia función."""
    fila = obtener(operador_id)
    if fila is None:
        raise LookupError(f"No existe el operador '{operador_id}'.")

    rol_nuevo = fila['rol'] if rol is None else rol
    if rol_nuevo not in ROLES:
        raise ValueError(f"rol inválido: {rol!r}. Válidos: {', '.join(ROLES)}.")
    activo_nuevo = bool(fila['activo']) if activo is None else bool(activo)
    nombre_nuevo = fila['nombre'] if nombre is None else (nombre or "").strip()
    if not nombre_nuevo:
        raise ValueError("nombre no puede estar vacío.")

    _proteger_ultimo_superadmin(fila, rol_nuevo, activo_nuevo)

    conn = _connection()
    try:
        conn.execute(
            "UPDATE operadores SET nombre=?, rol=?, activo=?, actualizado_en=? WHERE id=?",
            (nombre_nuevo, rol_nuevo, 1 if activo_nuevo else 0, _now(), operador_id),
        )
        conn.commit()
        row = conn.execute(f"SELECT {COLS} FROM operadores WHERE id=?", (operador_id,)).fetchone()
    finally:
        conn.close()

    # Desactivar debe echar fuera ya, no cuando venza la sesión. El cambio de rol
    # no necesita nada: cada request relee el operador, así que aplica solo.
    if not activo_nuevo:
        cerrar_sesiones_de(operador_id)
    return _publico(dict(row))


def borrar(operador_id: str) -> None:
    fila = obtener(operador_id)
    if fila is None:
        raise LookupError(f"No existe el operador '{operador_id}'.")
    _proteger_ultimo_superadmin(fila, rol_nuevo='(borrado)', activo_nuevo=False)

    cerrar_sesiones_de(operador_id)
    conn = _connection()
    try:
        conn.execute("DELETE FROM operadores WHERE id=?", (operador_id,))
        conn.commit()
    finally:
        conn.close()


def sembrar_superadmin():
    """Da de alta el primer superadmin desde el .env, una sola vez.

    Sólo corre si NO existe ningún superadmin, así que cambiar
    SUPERADMIN_PASSWORD en el .env después no reescribe nada — la contraseña se
    cambia desde el panel. Sin las variables, simplemente no hace nada y el
    sistema sigue funcionando con el ADMIN_PASSWORD de siempre.
    """
    email = normalizar_email(os.getenv('SUPERADMIN_EMAIL', ''))
    password = os.getenv('SUPERADMIN_PASSWORD', '')
    if contar(rol='superadmin') > 0:
        return
    if not email or not password:
        logger.info(
            "[OPERADORES] No hay superadmin y no se configuró SUPERADMIN_EMAIL/SUPERADMIN_PASSWORD "
            "en el .env: el panel sigue entrando con ADMIN_PASSWORD (?admin=<token>)."
        )
        return
    try:
        creado = crear(email, os.getenv('SUPERADMIN_NOMBRE', '').strip() or email, password, rol='superadmin')
    except (PasswordInvalida, ValueError, EmailDuplicado) as e:
        logger.error("[OPERADORES] No se pudo sembrar el superadmin desde el .env: %s", e)
        return
    logger.info("[OPERADORES] Superadmin '%s' creado desde el .env.", creado["email"])


def cambiar_password(operador_id: str, password_nueva: str, cerrar_otras: bool = True) -> None:
    password_hash = hashear(password_nueva)
    conn = _connection()
    try:
        conn.execute(
            "UPDATE operadores SET password_hash=?, actualizado_en=? WHERE id=?",
            (password_hash, _now(), operador_id),
        )
        conn.commit()
    finally:
        conn.close()
    if cerrar_otras:
        # Cambiar la contraseña invalida lo que ya estaba abierto: es lo que uno
        # espera cuando la cambia porque cree que alguien se la supo.
        cerrar_sesiones_de(operador_id)


# ─── Autenticación ──────────────────────────────────────────────

class CuentaBloqueada(Exception):
    """Demasiados intentos fallidos seguidos; hay que esperar."""

    def __init__(self, segundos: int):
        self.segundos = segundos
        super().__init__(f"Demasiados intentos fallidos. Espera {segundos} segundos.")


def _revisar_bloqueo(email: str):
    fallos, ultimo = _intentos.get(email, (0, 0.0))
    if fallos < INTENTOS_MAX:
        return
    restante = BLOQUEO_S - (time.time() - ultimo)
    if restante > 0:
        raise CuentaBloqueada(int(restante) + 1)
    _intentos.pop(email, None)  # pasó el castigo, borrón y cuenta nueva


def autenticar(email: str, password: str) -> Optional[dict]:
    """Devuelve el operador (sin hash) si las credenciales son correctas, o None.

    Levanta CuentaBloqueada tras varios fallos seguidos. No distingue entre
    "no existe", "contraseña mala" y "cuenta desactivada": quien prueba
    credenciales no tiene por qué enterarse de cuáles emails existen.
    """
    correo = normalizar_email(email)
    _revisar_bloqueo(correo)

    fila = obtener_por_email(correo)
    ok = bool(fila) and bool(fila["activo"]) and _verificar_hash(password, fila["password_hash"])
    if not ok:
        fallos, _ = _intentos.get(correo, (0, 0.0))
        _intentos[correo] = (fallos + 1, time.time())
        return None

    _intentos.pop(correo, None)
    return _publico(fila)


def autenticar_id(operador_id: str, password: str) -> bool:
    """Confirma la contraseña de un operador ya identificado (por su sesión).

    Para reconfirmar antes de algo delicado, como cambiar la contraseña. Sin
    contador de intentos: quien llega aquí ya tiene una sesión válida, así que
    no es una puerta de entrada que se pueda tantear desde fuera.
    """
    fila = obtener(operador_id)
    return bool(fila) and bool(fila["activo"]) and _verificar_hash(password, fila["password_hash"])


# ─── Sesiones ───────────────────────────────────────────────────

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def crear_sesion(operador_id: str) -> dict:
    """Devuelve {token, expira_en}. El token en claro no se guarda en ningún lado."""
    token = secrets.token_urlsafe(32)
    ahora = datetime.now(timezone.utc)
    expira = ahora + timedelta(days=SESION_DIAS)
    conn = _connection()
    try:
        conn.execute(
            "INSERT INTO sesiones (token_hash, operador_id, creado_en, expira_en, ultimo_uso) VALUES (?, ?, ?, ?, ?)",
            (_hash_token(token), operador_id, ahora.isoformat(), expira.isoformat(), ahora.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"token": token, "expira_en": expira.isoformat()}


def resolver_sesion(token: str) -> Optional[dict]:
    """Operador dueño de la sesión, o None si el token no vale, venció, o la
    cuenta quedó desactivada. Renueva el vencimiento mientras se use."""
    if not token:
        return None
    th = _hash_token(token)
    ahora = datetime.now(timezone.utc)
    conn = _connection()
    try:
        row = conn.execute(
            "SELECT operador_id, expira_en FROM sesiones WHERE token_hash=?", (th,)
        ).fetchone()
        if not row:
            return None
        if row["expira_en"] <= ahora.isoformat():
            conn.execute("DELETE FROM sesiones WHERE token_hash=?", (th,))
            conn.commit()
            return None

        fila = conn.execute(f"SELECT {COLS} FROM operadores WHERE id=?", (row["operador_id"],)).fetchone()
        if not fila or not fila["activo"]:
            # Cuenta borrada o desactivada: la sesión muere con ella.
            conn.execute("DELETE FROM sesiones WHERE token_hash=?", (th,))
            conn.commit()
            return None

        conn.execute(
            "UPDATE sesiones SET ultimo_uso=?, expira_en=? WHERE token_hash=?",
            (ahora.isoformat(), (ahora + timedelta(days=SESION_DIAS)).isoformat(), th),
        )
        conn.commit()
        return _publico(dict(fila))
    finally:
        conn.close()


def cerrar_sesion(token: str) -> bool:
    if not token:
        return False
    conn = _connection()
    try:
        cur = conn.execute("DELETE FROM sesiones WHERE token_hash=?", (_hash_token(token),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def cerrar_sesiones_de(operador_id: str) -> int:
    conn = _connection()
    try:
        cur = conn.execute("DELETE FROM sesiones WHERE operador_id=?", (operador_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def limpiar_sesiones_vencidas() -> int:
    """Borra las sesiones que ya vencieron. Se llama al arrancar; sin esto la
    tabla sólo crece con tokens que ya no sirven."""
    conn = _connection()
    try:
        cur = conn.execute("DELETE FROM sesiones WHERE expira_en <= ?", (_now(),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
