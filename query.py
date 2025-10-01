# query.py
from langchain_community.llms import Ollama
from langchain_ollama import OllamaLLM
from langchain.prompts import PromptTemplate
from operaciones_chroma import obtenBase

# ... (código de setup de LangChain, embeddings, etc.) ...

# Define el prompt con un espacio para el historial
# El historial se concatena en un solo string
prompt = PromptTemplate(
    template="""Eres un chatbot. Responde a la pregunta basándote en el siguiente historial y contexto.

    Historial de la conversación:
    {history}

    Contexto de la FAQ:
    {faq_text}

    Pregunta del usuario:
    {user_question}

    Respuesta:""",
    
    input_variables=["history", "faq_text", "user_question"],
)

def query(user_question: str, history: list = [], base_conocimiento: str = 'local-rag', modelo_llm: str = 'phi3'):

    llm = OllamaLLM(model=modelo_llm)
    # La parte RAG (recuperación de la FAQ) sigue siendo la misma
    # Aquí iría el código para buscar en la DB vectorial
    vector_db = obtenBase(base_conocimiento)
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

    return response