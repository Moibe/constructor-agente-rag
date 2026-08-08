# Handoff — Costos por consulta + soporte multi-proveedor

> Documento de arranque para una sesión nueva. Es autocontenido: no requiere
> contexto de la conversación donde se produjo. Escrito el 2026-08-07.

---

## 0. Qué se quiere

Poder ver **cuánto cuesta cada consulta** al chatbot, en dinero, y que el sistema
quede preparado para usar **otros proveedores además de OpenAI y Ollama**
(Anthropic/Claude y Google/Gemini en el futuro).

Dato duro que motiva el diseño: **ningún proveedor devuelve el costo en la
respuesta.** OpenAI, Anthropic y Google devuelven únicamente el conteo de tokens.
El costo se calcula siempre del lado nuestro: `tokens × tarifa del modelo`.
(OpenAI tiene una Costs API administrativa, `/v1/organization/costs`, pero es
agregada por día a nivel organización — sirve para conciliar la factura, no para
saber cuánto costó una consulta puntual.)

---

## 1. El sistema — tres repos hermanos

Viven lado a lado en `c:\Moibe\code\` y se conectan por convención de puertos
fija (sin `.env` ni build flags; el API siempre es `mismo-hostname:8077`).

| Repo | Rol | Puerto | Branch |
|---|---|---|---|
| `constructor-agente-rag` | Backend FastAPI + RAG (Python) | 8077 | **`dev`** |
| `buzzword-agentes-ui` | Admin completo (Svelte 5 + Vite) | 4175 | `main` |
| `host-asistentes` | Host público de widgets embebibles | 4176 | `main` |

**El grueso de este trabajo es backend** (`constructor-agente-rag`), no frontend.
El front solo recibe: columna de costo en Registros, un tab de tarifas, y que el
dropdown de modelos lea del registro nuevo.

`buzzword-agentes-ui/scripts/dev-local.mjs` es el orquestador: su `npm run dev`
levanta los tres (uvicorn con el venv de `constructor-agente-rag`, espera a que
:8077 responda, luego el admin y el host de widgets).

⚠️ `constructor-agente-rag` está en branch **`dev`**, no `main`. Verificar antes
de commitear.

---

## 2. Base que ya existe (verificado en código)

Hay más construido de lo que parece. **No arrancar de cero.**

### Persistencia
- `logs.db` → tabla `chat_logs`, creada en `app.py:225-265` (`init_log_db`).
  Ya tiene las columnas **`modelo`, `tokens_input`, `tokens_output`**, más
  `proyecto_id` / `proyecto_slug` / `asistente_slug` denormalizados.
- `agentes.db` → tablas `agentes`, `proyectos`, `_meta`.
- **Patrón de migración idempotente ya establecido** en `app.py:246-257`: lee
  `PRAGMA table_info(chat_logs)` y hace `ALTER TABLE ADD COLUMN` solo de las que
  falten. Agregar columnas nuevas es sumar entradas a esa tupla. Para migraciones
  destructivas one-shot hay otro patrón con flags en `_meta` (`migrate_agentes_v2`,
  `app.py:274+`).
- Índices ya creados sobre `fecha`, `agente_id`, `proyecto_slug`, `asistente_slug`.

### Costos — ya existen a medias
- `_OPENAI_PRICING` en `app.py:1596-1604`: dict de USD por 1M tokens
  (input/output) para gpt-5, gpt-5-mini, gpt-5-nano, gpt-4o, gpt-4o-mini.
  **Marcado en el propio código como placeholder sin verificar.**
- `_pricing_for(modelo)` en `app.py:1607-1618`: longest-prefix match.
- `/consumo/resumen` (`app.py:1621-1780`) ya agrega tokens por modelo, calcula
  costo y devuelve `tokens_openai: { input, output, costo_usd_estimado, por_modelo[] }`.
- El front **ya lo muestra**: tab Consumo, `App.svelte:4926-4955`, y ya existe un
  helper `formatUsd()`.

### Tokens
- `_extract_tokens()` en `chatbot.py:68-81` lee `usage_metadata` de LangChain
  (con fallback a `response_metadata.token_usage`).
  **Esto ya es multi-proveedor**: `usage_metadata` es el formato unificado de
  LangChain y `ChatAnthropic` / `ChatGoogleGenerativeAI` lo exponen igual que
  `ChatOpenAI`. No hay que tocarlo para agregar Claude o Gemini.
- El log se escribe en `app.py:1357-1370`, dentro de `POST /chatbot`.

### Registros
- `GET /registros` en `app.py:1783-1881`. Protegido con `require_admin`
  (`app.py:208-222`, header `Authorization: Bearer <ADMIN_PASSWORD>`).
  Filtros por rango de fechas (UTC), proyecto, asistente, `solo_errores`;
  paginación `limit` (max 200) / `offset`.
- UI en `App.svelte:392-457` (estado) y `App.svelte:5120-5320` (tabla con filas
  expandibles). El `modelo` ya se muestra en el detalle expandido.

---

## 3. Problemas encontrados (arreglar como parte de esto)

### 3.1 Error de precios silencioso — `gpt-5.5` se cobra como `gpt-5`
`gpt-5.5` y `gpt-5.5-pro` están ofrecidos en el dropdown del admin
(`App.svelte:135`) y aceptados por el backend (`herramientas.py:16-24`), pero
**no están en `_OPENAI_PRICING`**.

Como el match es por prefijo, `'gpt-5.5'.startswith('gpt-5')` → `True`, así que
ambos se tarifan a $1.25/$10.00. Un modelo "pro" cuesta bastante más: el costo
reportado sale subestimado y **no hay ninguna señal de que algo falló**.

Ese es el riesgo del prefix-match: no truena, miente. Cambiar a match exacto con
fallback ruidoso.

### 3.2 El costo se calcula al vuelo, no se persiste
Hoy la multiplicación ocurre al consultar `/consumo/resumen`. Si un proveedor
cambia tarifas, **los reportes históricos se reescriben solos**: un mes del año
pasado pasa a mostrarse con precios de hoy. Para algo que se usa como medida de
gasto real, eso no sirve.

### 3.3 La lista de modelos vive en 5 lugares que no coinciden
1. `globales.py:1-6` → `modelos` = `["phi3", "gemma:2b", "mistral", "llama3.1"]` + los GPT.
   Es la whitelist que valida `funciones.existe_modelo()` (`funciones.py:255-260`);
   si falla, `chat()` responde *"No existe ese modelo de lenguaje."*
2. `herramientas.py:16-29` → `OPENAI_LLM_MODELS` + `es_modelo_openai_llm()`, el routing de proveedor.
3. `chatbot.py:99-111` → el `if/else` que instancia `ChatOpenAI` vs `OllamaLLM`.
4. `app.py:1598` → `_OPENAI_PRICING`, las tarifas.
5. `App.svelte:134-135` → `MODELOS = ['mistral','llama3.1']` y `MODELOS_OPENAI`,
   que es lo que realmente puebla el dropdown del asistente (`App.svelte:3283-3297`).

**Consecuencia visible hoy:** el tab Modelos del admin lista ~10 LLMs instalados
en Ollama (consulta Ollama directo vía `GET /listarModelos`, `app.py:1884-1899`),
pero el dropdown del asistente solo ofrece `mistral` y `llama3.1`. O sea:
**qwen3, mixtral, deepseek-r1, gemma3, gpt-oss se ven pero no se pueden asignar
a ningún asistente.** Tres listas, tres respuestas distintas.

Agregar Claude hoy significa tocar los 5 lugares sin olvidarse de ninguno.

### 3.4 Ollama no reporta tokens
`OllamaLLM` (clase de *completions* de LangChain) devuelve un string plano sin
metadatos, así que `_extract_tokens()` retorna `(None, None)` y se guarda `NULL`.

Ollama **sí** expone `prompt_eval_count` / `eval_count` en su API; el problema es
la clase. Cambiar a **`ChatOllama`** (la variante de chat de `langchain-ollama`)
hace que la respuesta traiga `usage_metadata` igual que OpenAI, y
`_extract_tokens()` los captura sin modificar nada más.

Impacto actual del `NULL`: en Registros la UI hace `(tokens_in ?? 0) + (tokens_out ?? 0)`
→ muestra **0 tokens**, que se lee como "cero" cuando en realidad es "no se sabe".
En Consumo esas filas se excluyen del agregado (`app.py:1686`).

---

## 4. Diseño acordado

**Un registro único de modelos en SQLite, editable desde el admin**, como única
fuente de verdad. Colapsa las 5 listas del punto 3.3 en una, y convierte
"agregar Claude" en *insertar una fila + instalar el paquete de LangChain*.

```
tabla modelos
  nombre                 TEXT PRIMARY KEY   -- 'gpt-4o', 'qwen3:14b', 'claude-sonnet-4-5'
  proveedor              TEXT NOT NULL      -- 'openai' | 'ollama' | 'anthropic' | 'google'
  precio_input_usd_1m    REAL               -- NULL = sin tarifa conocida
  precio_output_usd_1m   REAL
  activo                 INTEGER NOT NULL DEFAULT 1
  notas                  TEXT
  actualizado_en         TEXT
```

Decisiones que acompañan:

- **Persistir el costo en `chat_logs`**: dos columnas nuevas, `proveedor` y
  `costo_usd`, calculadas y congeladas en el momento de loguear. Tarifa vigente
  → costo histórico inmutable. Se agregan con el loop idempotente de `app.py:248`.
- **Match exacto por nombre**, no por prefijo. Si un modelo no tiene tarifa,
  guardar `costo_usd = NULL` y mostrarlo en la UI como *"sin tarifa"* — nunca
  inventar un número (esto es lo que arregla 3.1).
- **Ollama = $0.00 explícito**, distinto de `NULL`/desconocido. Es hardware
  propio: no cuesta dinero, pero los tokens siguen sirviendo como medida de carga.
- **Factory de proveedor** que lee `proveedor` del registro en vez del `if/else`
  por nombre. Import diferido por proveedor: si falta el paquete
  (`langchain-anthropic`, `langchain-google-genai`), error claro y accionable.

---

## 5. Plan de implementación

### Fase 1 — Backend (`constructor-agente-rag`)

1. **Tabla `modelos`** en `agentes.db` + `init_modelos_db()` siguiendo el patrón
   de `init_agentes_db`. Seed idempotente con los modelos actuales.
2. **Verificar las tarifas reales** contra el pricing oficial de cada proveedor y
   sembrarlas. ⚠️ No copiar los valores de `_OPENAI_PRICING` tal cual: están
   marcados como placeholder sin verificar, y faltan `gpt-5.5` / `gpt-5.5-pro`.
3. **Factory de proveedor** (módulo nuevo, ej. `proveedores.py`): dado un nombre,
   consulta el registro y devuelve el cliente LangChain correcto. Soportar
   `openai` / `ollama` / `anthropic` / `google` desde el día uno.
4. **`chatbot.py`**: reemplazar el `if/else` de `chatbot.py:99-111` por el factory,
   y cambiar `OllamaLLM` → `ChatOllama` (arregla 3.4). `_extract_tokens()` no se toca.
5. **`funciones.existe_modelo()`** consulta el registro en vez de `globales.modelos`.
6. **`chat_logs`**: agregar `proveedor` y `costo_usd` al loop de migración
   (`app.py:248-255`) y calcularlos en el INSERT de `POST /chatbot` (`app.py:1357-1370`).
7. **Endpoints CRUD** de modelos/tarifas, protegidos con `require_admin`.
8. **`/registros`** devuelve `costo_usd` y `proveedor` por fila (leídos de la
   columna, no recalculados).
9. **`/consumo/resumen`** suma `costo_usd` persistido en vez de multiplicar al vuelo.

### Fase 2 — Frontend (`buzzword-agentes-ui`)

10. Columna **Costo** en la tabla de Registros, junto a Tokens
    (`App.svelte:5220-5257`). Ya existe `formatUsd()`. Mostrar "—" cuando sea `NULL`.
11. **Tab de tarifas** en Administración (modelo visual: el tab Alias, `App.svelte:4722+`).
12. El dropdown de modelo del asistente (`App.svelte:3283-3297`) lee del registro
    en vez de las constantes `MODELOS` / `MODELOS_OPENAI` de `App.svelte:134-135`
    (arregla 3.3 y desbloquea los ~8 modelos de Ollama hoy inutilizables).

---

## 6. Decisión — tomada 2026-08-07

**Tarifas editables desde el admin (tabla SQLite), no constante en el backend.**

Confirmado por Moibe. El plan de la sección 4/5 ya asumía esto — queda tal cual
descrito ahí, sin cambios. Fase 1, punto 1 puede arrancar.

---

## 7. Cómo correr y probar

```bash
cd c:\Moibe\code\buzzword-agentes-ui
npm run dev     # levanta los tres: API :8077, admin :4175, widgets :4176
```

URLs que imprime el orquestador:
- Admin: `http://localhost:4175` → tab Administración → Registros / Consumo
- API docs: `http://localhost:8077/docs`
- Chatbot: `http://localhost:4176/embed/chat/<slug>`

Para probar el cálculo de costos: mandar consultas desde el sandbox del admin o
desde el widget, y verificar que `chat_logs` guarde `costo_usd` con la tarifa
vigente. Los endpoints admin requieren `Authorization: Bearer <ADMIN_PASSWORD>`
(el valor sale del `.env` del backend).

---

## 8. Gotchas

- **`tokens_openai` es un nombre acoplado al proveedor**, pero el front ya lo lee
  en 8 lugares (`App.svelte:4926-4955`). Si se renombra a algo neutro
  (`tokens_ia`, `consumo_ia`), hay que actualizar el front en el mismo cambio o
  el tab Consumo queda en blanco.
- **`constructor-agente-rag` está en branch `dev`.** El otro repo en `main`.
- **`agentes.db` y `logs.db` están versionados en el repo** (no ignorados).
  Cuidado con commitear datos de prueba.
- Las fechas en `chat_logs` son **UTC** (`_now()` usa `datetime.now(timezone.utc)`).
  `/registros` ya calcula su rango default en UTC; `/consumo/resumen` todavía usa
  `date.today()` local (`app.py:1628`) — inconsistencia menor preexistente, no
  introducida por este trabajo.
- Los modelos de razonamiento (gpt-5, serie o) cobran los tokens de razonamiento
  como output, y los tokens cacheados se cobran más barato. `usage_metadata` trae
  ese desglose en `input_token_details` / `output_token_details` si se quiere
  afinar después; la fórmula simple input/output ya da un número muy cercano.
- El chunk de código que hoy calcula costo en `/consumo/resumen`
  (`app.py:1714-1745`) se vuelve redundante al persistir `costo_usd`. Borrarlo,
  no dejarlo conviviendo con la fuente nueva.
