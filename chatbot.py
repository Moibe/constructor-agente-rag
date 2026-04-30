from langchain_ollama import OllamaLLM
from langchain_core.prompts import PromptTemplate
from typing import Optional
from herramientas import obtenContexto
import funciones
import herramientas

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

def chat(user_question: str, history: list = [], contexto: str = 'local-rag', modelo_llm: str = 'phi3', instrucciones: Optional[str] = None):

    #Traducir el texto a inglés?

    #No debe de crear la colección si no existe!
    if funciones.existe_contexto(contexto):

        try: 
            print("Inicializando modelo de lenguaje: ", modelo_llm)
            if funciones.existe_modelo(modelo_llm):
                if herramientas.es_modelo_openai_llm(modelo_llm):
                    from langchain_openai import ChatOpenAI
                    llm = ChatOpenAI(model=modelo_llm)
                else:
                    llm = OllamaLLM(model=modelo_llm)
            else: 
                return {"Mensaje": "No existe ese modelo de lenguaje."}
        except Exception as e:
            print(f"Error al listar las colecciones: {e}")
            return {"error": f"Error al listar las colecciones: {e}"}        

        vector_db = obtenContexto(contexto)
        retriever = vector_db.as_retriever()
        retrieved_docs = retriever.invoke(user_question)    #get_relevant_documents ahora debería usar invoke o batch.
        faq_text = retrieved_docs[0].page_content if retrieved_docs else "No hay información relevante."

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
        response = llm.invoke(full_prompt)

        # Uniformar respuesta a string plano:
        # - ChatOpenAI (Chat Completions, ej. gpt-4o): response.content es str.
        # - ChatOpenAI (Responses API, ej. gpt-5): response.content es lista de dicts.
        # - OllamaLLM: response es str directo.
        raw = response.content if hasattr(response, 'content') else response
        return _extraer_texto_de_respuesta(raw)
    
    else:
        return {"Mensaje": "No existe ese contexto."}