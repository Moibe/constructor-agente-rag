"""Factory de clientes LangChain por proveedor.

Reemplaza el if/else por nombre de modelo que vivía en `chatbot.py`. El proveedor
sale del registro (`modelos.py`), así que agregar un modelo nuevo de un proveedor
ya soportado es insertar una fila, sin tocar código.

Los imports son diferidos por proveedor: el paquete de LangChain sólo se importa
cuando alguien realmente usa ese proveedor, y si falta se levanta un error que
dice qué instalar en vez de un ImportError críptico al arrancar el server.
"""

import os
from typing import Optional

import keys_openai
import modelos


class ProveedorError(Exception):
    """Error accionable al construir el cliente del modelo (paquete faltante,
    proveedor desconocido, modelo no registrado)."""


def _paquete_faltante(proveedor: str, paquete: str, err: Exception) -> ProveedorError:
    return ProveedorError(
        f"El proveedor '{proveedor}' requiere el paquete '{paquete}', que no está "
        f"instalado. Instálalo con: pip install {paquete}  (detalle: {err})"
    )


# Verificado experimentalmente (2026-08-08): ChatOpenAI y ChatGoogleGenerativeAI
# validan la API key al construirse (truenan antes de llegar aquí, y el except
# genérico de crear_llm() los envuelve bien). ChatAnthropic NO — se construye sin
# error incluso sin key, y sólo fallaría después, al invocar, con un error crudo
# que no pasa por crear_llm(). Por eso Anthropic necesita este chequeo explícito.
def _requerir_env(proveedor: str, env_var: str):
    if not os.getenv(env_var):
        raise ProveedorError(
            f"El proveedor '{proveedor}' requiere la variable de entorno "
            f"'{env_var}', que no está configurada en el .env del backend."
        )


class _ClienteLLM:
    """Lo que devuelve crear_llm(): el modelo listo para invocarse.

    En OpenAI la key se elige en cada invocación (principal o respaldo, ver
    keys_openai.py), y hay un cliente LangChain por key porque la key se fija al
    construirlo. En los demás proveedores es un passthrough a un solo cliente.

    No guarda nada de la consulta en curso, así que es seguro compartirlo entre
    requests concurrentes — vive en `_llm_cache`.
    """

    def __init__(self, proveedor: str, nombre_modelo: str):
        self.proveedor = proveedor
        self.nombre_modelo = nombre_modelo
        self._clientes: dict = {}  # etiqueta de la key (None fuera de OpenAI) → cliente LangChain

    def _cliente(self, etiqueta: Optional[str] = None, api_key: Optional[str] = None):
        cliente = self._clientes.get(etiqueta)
        if cliente is None:
            cliente = _construir(self.proveedor, self.nombre_modelo, api_key)
            self._clientes[etiqueta] = cliente
        return cliente

    def preparar(self):
        """Construye el cliente que se va a usar primero, para que un problema de
        configuración truene aquí — antes del RAG — con un error accionable."""
        if self.proveedor != 'openai':
            self._cliente()
            return
        candidatas = keys_openai.candidatas(self.nombre_modelo)
        if not candidatas:
            raise ProveedorError(keys_openai.mensaje_sin_keys())
        self._cliente(*candidatas[0])

    def invocar(self, prompt):
        """Devuelve (respuesta, key): key es 'principal' o 'respaldo' en OpenAI,
        None en los demás proveedores."""
        if self.proveedor != 'openai':
            return self._cliente().invoke(prompt), None
        return keys_openai.ejecutar(
            self.nombre_modelo,
            lambda etiqueta, api_key: self._cliente(etiqueta, api_key).invoke(prompt),
        )


# Caché de clientes ya construidos, keyed por (proveedor, nombre_modelo); en
# OpenAI cada entrada guarda adentro un cliente por key (ver _ClienteLLM).
# Medido experimentalmente (2026-08-19): construir un ChatOllama/ChatOpenAI/etc.
# cuesta ~300-350ms SIEMPRE, en cada llamada — no es un costo de arranque en
# frío que se diluye, es el validador de Pydantic de esas clases corriendo cada
# vez (sin red de por medio). El cliente en sí es stateless y reutilizable
# entre consultas del mismo modelo, así que cachearlo ahorra ese costo en cada
# /chatbot excepto el primero.
#
# La validación de existencia/activo en el registro (abajo) NUNCA se cachea —
# corre siempre desde SQLite (~0.5ms, no vale la pena cachearla) — para que
# desactivar un modelo desde el admin tenga efecto inmediato en la siguiente
# consulta sin importar qué haya en este caché. Si `proveedor` cambia para un
# `nombre_modelo` existente, la cache key cambia con él, así que nunca se
# devuelve un cliente construido para el proveedor viejo.
_llm_cache: dict = {}


def crear_llm(nombre_modelo: str, temperatura: Optional[float] = None):
    """Devuelve el modelo listo para invocarse con `.invocar(prompt)` (cacheado).

    Levanta ProveedorError si el modelo no está en el registro, está desactivado,
    el proveedor es desconocido, falta el paquete del proveedor, o (en OpenAI)
    no hay ninguna key utilizable con el modo elegido en el admin.
    """
    fila = modelos.obtener(nombre_modelo)
    if fila is None:
        raise ProveedorError(
            f"El modelo '{nombre_modelo}' no está en el registro de modelos. "
            "Agrégalo desde el admin (tab Tarifas) o vía POST /modelos."
        )
    if not fila['activo']:
        raise ProveedorError(
            f"El modelo '{nombre_modelo}' está desactivado en el registro. "
            "Actívalo desde el admin para poder asignarlo."
        )

    proveedor = fila['proveedor']
    cache_key = (proveedor, nombre_modelo)
    cliente = _llm_cache.get(cache_key)
    if cliente is None:
        cliente = _ClienteLLM(proveedor, nombre_modelo)
        _llm_cache[cache_key] = cliente

    try:
        cliente.preparar()
        return cliente
    except ProveedorError:
        raise
    except Exception as e:
        # Típicamente la API key faltante (Google la valida al construirse;
        # Anthropic la valida abajo en _construir() vía _requerir_env; la de
        # OpenAI la resuelve keys_openai). El error de LangChain no menciona ni
        # el modelo ni el proveedor, así que lo envolvemos con ese contexto para
        # que el mensaje que llega al admin diga qué configurar.
        raise ProveedorError(
            f"No se pudo inicializar el modelo '{nombre_modelo}' (proveedor "
            f"'{proveedor}'). Revisa que la API key del proveedor esté "
            f"configurada en el .env del backend. Detalle: {e}"
        )


def _construir(proveedor: str, nombre_modelo: str, api_key: Optional[str] = None):
    if proveedor == 'openai':
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as e:
            raise _paquete_faltante(proveedor, 'langchain-openai', e)
        # La key llega explícita desde keys_openai (principal o respaldo). Sin
        # ella, ChatOpenAI caería solo a OPENAI_API_KEY.
        kwargs = {'api_key': api_key} if api_key else {}
        return ChatOpenAI(model=nombre_modelo, **kwargs)

    if proveedor == 'ollama':
        # ChatOllama, no OllamaLLM. OllamaLLM es la clase de *completions*: devuelve
        # un string plano sin metadatos, así que _extract_tokens() no encontraba
        # nada y los tokens se guardaban NULL — que en la UI se leía como "0 tokens"
        # cuando en realidad era "no se sabe". ChatOllama expone `usage_metadata`
        # igual que ChatOpenAI y los tokens se capturan sin tocar nada más.
        try:
            from langchain_ollama import ChatOllama
        except ImportError as e:
            raise _paquete_faltante(proveedor, 'langchain-ollama', e)
        return ChatOllama(model=nombre_modelo)

    if proveedor == 'anthropic':
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as e:
            raise _paquete_faltante(proveedor, 'langchain-anthropic', e)
        _requerir_env(proveedor, 'ANTHROPIC_API_KEY')
        # max_tokens es obligatorio en la API de Anthropic; LangChain tiene un
        # default bajo, así que lo subimos a algo razonable para respuestas de chat.
        return ChatAnthropic(model=nombre_modelo, max_tokens=4096)

    if proveedor == 'google':
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as e:
            raise _paquete_faltante(proveedor, 'langchain-google-genai', e)
        return ChatGoogleGenerativeAI(model=nombre_modelo)

    raise ProveedorError(
        f"Proveedor '{proveedor}' desconocido para el modelo '{nombre_modelo}'. "
        f"Soportados: {', '.join(modelos.PROVEEDORES_VALIDOS)}."
    )
