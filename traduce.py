"""traduce.py

Script interactivo para traducir texto del español al inglés usando
Helsinki-NLP/opus-mt-es-en (transformers).

El script pide los argumentos por `input()` en la terminal y carga los
modelos solo cuando se va a ejecutar.
"""

from typing import Optional

def traducir_texto(
    texto: str,
    pipe=None,
    tokenizer=None,
    model=None,
    max_length: int = 512,
    num_beams: int = 4,
) -> dict:
    """Traduce `texto` del español al inglés.

    Parámetros:
      - texto: cadena en español.
      - pipe: objeto pipeline (opcional). Si se pasa, se usa para la traducción rápida.
      - tokenizer, model: si se pasan, se usan para la generación directa.
      - max_length, num_beams: parámetros para la generación directa.

    Retorna un diccionario con las claves 'pipeline' y 'modelo' (valores None si no se generó).
    """
    resultados = {"pipeline": None, "modelo": None}

    # Usar pipeline si se proporcionó
    if pipe is not None:
        try:
            traduccion_pipeline = pipe(texto)
            resultados["pipeline"] = traduccion_pipeline[0].get("translation_text")
        except Exception as e:
            resultados["pipeline"] = f"ERROR: {e}"

    # Usar modelo/tokenizer si se proporcionaron
    if tokenizer is not None and model is not None:
        try:
            inputs = tokenizer.encode(texto, return_tensors="pt", max_length=max_length, truncation=True)
            outputs = model.generate(inputs, max_length=max_length, num_beams=num_beams, early_stopping=True)
            resultados["modelo"] = tokenizer.decode(outputs[0], skip_special_tokens=True)
        except Exception as e:
            resultados["modelo"] = f"ERROR: {e}"

    return resultados


def _cargar_pipeline():
    """Carga y retorna un pipeline de traducción."""
    from transformers import pipeline

    return pipeline("translation", model="Helsinki-NLP/opus-mt-es-en")


def _cargar_modelo_y_tokenizer():
    """Carga y retorna (tokenizer, model)."""
    from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

    tokenizer = AutoTokenizer.from_pretrained("Helsinki-NLP/opus-mt-es-en")
    model = AutoModelForSeq2SeqLM.from_pretrained("Helsinki-NLP/opus-mt-es-en")
    return tokenizer, model


def main():
    """Modo interactivo: pide parámetros por terminal y ejecuta la traducción."""
    print("=== Traductor ES -> EN (Helsinki-NLP/opus-mt-es-en) ===")

    # Texto a traducir
    try:
        texto = input("Ingrese el texto en español (o dejar vacío para salir): ").strip()
    except EOFError:
        print("No se recibió entrada. Saliendo.")
        return

    if not texto:
        print("Texto vacío. Saliendo.")
        return

    # Método: pipeline / modelo / ambos
    metodo = input("¿Qué método usar? [pipeline/modelo/ambos] (por defecto: ambos): ").strip().lower() or "ambos"
    if metodo not in {"pipeline", "modelo", "ambos"}:
        print("Opción no válida, usando 'ambos'.")
        metodo = "ambos"

    # Parámetros opcionales para el modelo
    def _leer_int(prompt: str, default: int) -> int:
        val = input(f"{prompt} (por defecto {default}): ").strip()
        if not val:
            return default
        try:
            return int(val)
        except ValueError:
            print("Valor no válido, usando valor por defecto.")
            return default

    max_length = _leer_int("max_length para la generación", 512)
    num_beams = _leer_int("num_beams para la generación", 4)

    # Cargar solo lo necesario
    pipe = tokenizer = model = None
    if metodo in {"pipeline", "ambos"}:
        print("Cargando pipeline... esto puede tardar la primera vez")
        pipe = _cargar_pipeline()
    if metodo in {"modelo", "ambos"}:
        print("Cargando tokenizer y modelo... esto puede tardar la primera vez")
        tokenizer, model = _cargar_modelo_y_tokenizer()

    resultados = traducir_texto(texto, pipe=pipe, tokenizer=tokenizer, model=model, max_length=max_length, num_beams=num_beams)

    if resultados.get("pipeline") is not None:
        print("\n--- Traducción (pipeline) ---")
        print(resultados["pipeline"])

    if resultados.get("modelo") is not None:
        print("\n--- Traducción (modelo directo) ---")
        print(resultados["modelo"])


def traducir(texto: str) -> dict:
    """Versión simple: recibe `texto` y traduce usando 'ambos' con valores por defecto."""
    if not texto:
        print("Texto vacío. Saliendo.")
        return {}

    print("Cargando pipeline y modelo... esto puede tardar la primera vez")
    pipe = _cargar_pipeline()
    tokenizer, model = _cargar_modelo_y_tokenizer()

    resultados = traducir_texto(texto, pipe=pipe, tokenizer=tokenizer, model=model)

    if resultados.get("pipeline") is not None:
        print("\n--- Traducción (pipeline) ---")
        print(resultados["pipeline"])

    if resultados.get("modelo") is not None:
        print("\n--- Traducción (modelo directo) ---")
        print(resultados["modelo"])

    return resultados


if __name__ == "__main__":
    main()