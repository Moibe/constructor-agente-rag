import time
from langchain_core.prompts import PromptTemplate
from typing import Optional
from herramientas import obtenContexto
import funciones
import proveedores

# Prompt hardcoded del MIDE — fallback legacy cuando no se proveen instrucciones explícitas.
_DEFAULT_MIDE_PROMPT = PromptTemplate(
    template="""Eres un chatbot asistente del museo. Responde a la pregunta basándote en el siguiente historial y contexto. Si te preguntaran algo no relacionado al museo, solo contesta que tu eres un asistente especializado en el museo.

    *** INSTRUCCIÓN CLAVE: La respuesta debe ser concisa, directa y no debe exceder las dos (2) oraciones. ***

    *** El Museo es el MIDE: Museo de Economía ***

    Historial de la conversación:
    {history}

    Contexto de la FAQ:
    {faq_text}

    Pregunta del usuario:
    {user_question}

    Respuesta:""",
    input_variables=["history", "faq_text", "user_question"],
)

def _extraer_texto_de_respuesta(content) -> str:
    """Aplana la respuesta de LangChain a un string.

    Maneja dos casos:
    - Chat Completions (gpt-4o, mistral, llama3.1, etc.): content ya es str.
    - Responses API (gpt-5, gpt-5-mini, gpt-5-nano, gpt-5.5, gpt-5.5-pro): content
      es una lista de dicts donde cada item tiene un 'type' ('reasoning' | 'text').
      Solo nos quedamos con los items de tipo 'text' y los concatenamos.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        partes = [
            item.get('text', '')
            for item in content
            if isinstance(item, dict) and item.get('type') == 'text'
        ]
        return ' '.join(p for p in partes if p).strip()
    return str(content)


def _build_prompt(instrucciones: Optional[str]) -> PromptTemplate:
    if instrucciones is None:
        return _DEFAULT_MIDE_PROMPT
    template = (
        f"{instrucciones}\n\n"
        "Historial de la conversación:\n"
        "{history}\n\n"
        "Contexto de la FAQ:\n"
        "{faq_text}\n\n"
        "Pregunta del usuario:\n"
        "{user_question}\n\n"
        "Respuesta:"
    )
    return PromptTemplate(
        template=template,
        input_variables=["history", "faq_text", "user_question"],
    )

def _extract_tokens(response):
    """Extrae (input_tokens, output_tokens) del response de LangChain.
    `usage_metadata` es el formato unificado de LangChain: ChatOpenAI, ChatOllama,
    ChatAnthropic y ChatGoogleGenerativeAI lo exponen igual, así que esta función
    es multi-proveedor sin cambios.
    - Fallback: response_metadata.token_usage (prompt_tokens/completion_tokens).
    - Si no hay ninguno de los dos → (None, None), que significa "no se sabe",
      no "cero".
    """
    if hasattr(response, 'usage_metadata') and response.usage_metadata:
        um = response.usage_metadata
        return um.get('input_tokens'), um.get('output_tokens')
    if hasattr(response, 'response_metadata'):
        usage = (response.response_metadata or {}).get('token_usage', {}) if response.response_metadata else {}
        return (usage.get('prompt_tokens') or usage.get('input_tokens'),
                usage.get('completion_tokens') or usage.get('output_tokens'))
    return None, None


def chat(user_question: str, history: list = [], contexto: Optional[str] = None, modelo_llm: str = 'phi3', instrucciones: Optional[str] = None, top_k: int = 1):
    """Devuelve dict:
    - éxito: {"text": str, "tokens_input": int|None, "tokens_output": int|None,
              "ms_rag": int|None, "ms_llm": int|None}
    - error: {"error_message": str}

    `ms_rag`/`ms_llm` desglosan la latencia total (que ya mide app.py alrededor
    de esta función completa) en sus dos partes caras: la búsqueda en Chroma y
    la llamada al LLM. `ms_rag` es None cuando el agente no tiene BC asignada
    (no hubo RAG que medir) — nunca 0, para no leerse como "instantáneo".

    `top_k`: cuántos chunks recuperar de Chroma y pasar al LLM. Default 1
    (comportamiento histórico para FAQs autocontenidos). Para PDFs informativos
    o BCs con respuestas distribuidas en varios chunks, conviene subirlo a 3-5.
    """
    # Si llega contexto string-no-vacío, debe existir en Chroma. Si llega None/empty,
    # el agente está "sin BC" → modo chat puro (sin RAG).
    sin_rag = not contexto
    if not sin_rag and not funciones.existe_contexto(contexto):
        return {"error_message": "No existe ese contexto."}

    try:
        print("Inicializando modelo de lenguaje: ", modelo_llm)
        # El factory consulta el registro de modelos para saber el proveedor y
        # devolver el cliente correcto. Sus errores (modelo no registrado,
        # desactivado, paquete del proveedor faltante) ya vienen redactados para
        # que el admin pueda accionar sobre ellos.
        llm = proveedores.crear_llm(modelo_llm)
    except proveedores.ProveedorError as e:
        print(f"Error al inicializar modelo: {e}")
        return {"error_message": str(e)}
    except Exception as e:
        print(f"Error al inicializar modelo: {e}")
        return {"error_message": f"Error al inicializar modelo: {e}"}

    ms_rag = None
    if sin_rag:
        # Sin BC: respondemos sin retrieval. Le decimos al modelo explícitamente que
        # no hay base de conocimiento para que no alucine que sí la tiene.
        faq_text = "(Este asistente no tiene base de conocimiento asignada. Responde con conocimiento general.)"
    else:
        # `k` controla cuántos chunks pide langchain a Chroma. Hasta antes de este
        # cambio no se pasaba explícito → langchain usaba su default (k=4) pero
        # acá solo se usaba retrieved_docs[0], desperdiciando 3 búsquedas. Ahora
        # pedimos exactamente `top_k` y concatenamos todo lo que vuelva.
        # El cronómetro arranca incluyendo obtenContexto() (abre/conecta la
        # colección de Chroma) porque para quien lee el reporte, eso también
        # es "tiempo del RAG", no un paso aparte.
        rag_start = time.perf_counter()
        vector_db = obtenContexto(contexto)
        retriever = vector_db.as_retriever(search_kwargs={"k": max(1, top_k)})
        retrieved_docs = retriever.invoke(user_question)
        ms_rag = int((time.perf_counter() - rag_start) * 1000)
        if retrieved_docs:
            # Separador explícito entre chunks para que el LLM los pueda distinguir
            # cuando hay varios (importante para PDFs informativos con respuestas
            # repartidas; irrelevante con top_k=1).
            faq_text = "\n\n---\n\n".join(d.page_content for d in retrieved_docs)
        else:
            faq_text = "No hay información relevante."

    # Formatea el historial para pasárselo al prompt
    formatted_history = "\n".join([f"{item['role']}: {item['content']}" for item in history])

    # Construir prompt dinámico: si llegaron instrucciones, se envuelven; si no, fallback MIDE.
    prompt = _build_prompt(instrucciones)
    full_prompt = prompt.invoke({
        "history": formatted_history,
        "faq_text": faq_text,
        "user_question": user_question
    })

    # Genera la respuesta
    llm_start = time.perf_counter()
    response = llm.invoke(full_prompt)
    ms_llm = int((time.perf_counter() - llm_start) * 1000)

    # Uniformar respuesta a string plano:
    # - ChatOpenAI (Chat Completions, ej. gpt-4o): response.content es str.
    # - ChatOpenAI (Responses API, ej. gpt-5): response.content es lista de dicts.
    # - ChatOllama / ChatAnthropic / ChatGoogleGenerativeAI: content es str
    #   (Anthropic puede devolver lista de bloques, que el helper también aplana).
    raw = response.content if hasattr(response, 'content') else response
    text = _extraer_texto_de_respuesta(raw)
    tokens_input, tokens_output = _extract_tokens(response)
    return {
        "text": text,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "ms_rag": ms_rag,
        "ms_llm": ms_llm,
    }