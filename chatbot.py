from langchain_ollama import OllamaLLM
from langchain_core.prompts import PromptTemplate
from generacion_aumentada import obtenContexto
import funciones
import herramientas

# Definir el prompt con un espacio para el historial
# El historial se concatena en un solo string
prompt = PromptTemplate(
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

def chat(user_question: str, history: list = [], contexto: str = 'local-rag', modelo_llm: str = 'phi3'):

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

        # El prompt ahora recibe el historial
        full_prompt = prompt.invoke({
            "history": formatted_history,
            "faq_text": faq_text,
            "user_question": user_question
        })

        # Genera la respuesta
        response = llm.invoke(full_prompt)

        # Uniformar respuesta: ChatOpenAI devuelve AIMessage, OllamaLLM devuelve str
        if hasattr(response, 'content'):
            return response.content
        return response
    
    else:
        return {"Mensaje": "No existe ese contexto."}