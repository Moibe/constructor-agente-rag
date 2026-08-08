"""Factory de clientes LangChain por proveedor.

Reemplaza el if/else por nombre de modelo que vivía en `chatbot.py`. El proveedor
sale del registro (`modelos.py`), así que agregar un modelo nuevo de un proveedor
ya soportado es insertar una fila, sin tocar código.

Los imports son diferidos por proveedor: el paquete de LangChain sólo se importa
cuando alguien realmente usa ese proveedor, y si falta se levanta un error que
dice qué instalar en vez de un ImportError críptico al arrancar el server.
"""

from typing import Optional

import modelos


class ProveedorError(Exception):
    """Error accionable al construir el cliente del modelo (paquete faltante,
    proveedor desconocido, modelo no registrado)."""


def _paquete_faltante(proveedor: str, paquete: str, err: Exception) -> ProveedorError:
    return ProveedorError(
        f"El proveedor '{proveedor}' requiere el paquete '{paquete}', que no está "
        f"instalado. Instálalo con: pip install {paquete}  (detalle: {err})"
    )


def crear_llm(nombre_modelo: str, temperatura: Optional[float] = None):
    """Devuelve el cliente LangChain que corresponde al modelo.

    Levanta ProveedorError si el modelo no está en el registro, está desactivado,
    el proveedor es desconocido, o falta el paquete del proveedor.
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
    try:
        return _construir(proveedor, nombre_modelo)
    except ProveedorError:
        raise
    except Exception as e:
        # Típicamente la API key faltante. El cliente de LangChain la valida al
        # construirse y su error no menciona ni el modelo ni el proveedor, así
        # que lo envolvemos con ese contexto para que el mensaje que llega al
        # admin diga qué configurar.
        raise ProveedorError(
            f"No se pudo inicializar el modelo '{nombre_modelo}' (proveedor "
            f"'{proveedor}'). Revisa que la API key del proveedor esté "
            f"configurada en el .env del backend. Detalle: {e}"
        )


def _construir(proveedor: str, nombre_modelo: str):
    if proveedor == 'openai':
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as e:
            raise _paquete_faltante(proveedor, 'langchain-openai', e)
        return ChatOpenAI(model=nombre_modelo)

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
