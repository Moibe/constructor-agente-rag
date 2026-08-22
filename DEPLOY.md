# Deploy — segunda instancia en el droplet de DigitalOcean

> Este proyecto ya corre en el servidor de CSI. Este documento describe cómo
> levantar una **segunda instancia independiente** en el droplet `gradioFish`
> (`165.22.53.200`), sin copiar el repo y sin tocar la instancia de CSI.

## 0. Por qué no hay una copia del repo

Todo lo que distingue a una instancia de otra ya sale de variables de entorno
(`AGENTES_DB_PATH`, `LOG_DB_PATH`, `CHROMA_PATH`, `TEMP_FOLDER`, `DOCS_FOLDER`,
`OLLAMA_URL`, `OPENAI_API_KEY`, `ADMIN_PASSWORD`, `CORS_ALLOWED_ORIGINS`), y no
hay ninguna IP ni host hardcodeado en el código Python.

Las bases SQLite y `chroma/` están en `.gitignore`, así que **un clone limpio
arranca vacío y se auto-inicializa** (`init_log_db()`, `init_agentes_db()`,
`init_modelos_db()` corren al importar `app`). Eso ya es una instancia aparte:
proyectos, agentes, bases de conocimiento y logs propios.

Copiar el repo obligaría a aplicar cada fix dos veces y las dos copias
divergirían en semanas. Lo único que cambia entre instancias es el `.env`.

## 1. Diferencia clave con CSI: acá no hay Ollama

El droplet comparte recursos con ~8 apps; correr un LLM local ahí no es viable.
Esta instancia funciona **sólo con OpenAI**, y eso tiene dos consecuencias:

- **LLMs**: el `.env` lleva `OLLAMA_HABILITADO=false`, con lo que el seed del
  registro de modelos da de alta los modelos de Ollama **desactivados**. No
  aparecen en el dropdown del asistente y no se pueden asignar. Los de OpenAI
  quedan activos con las tarifas verificadas.
- **Embeddings**: toda base de conocimiento creada acá tiene que usar
  `text-embedding-3-small` / `text-embedding-3-large`. Una BC embebida con
  `mxbai-embed-large` (Ollama) **no es portable a esta instancia** — Chroma
  necesita el mismo modelo de embedding para consultarla, así que habría que
  re-embeberla.

## 2. Prerrequisitos en el droplet (una sola vez)

```bash
# venv de Python
sudo apt-get install -y python3-venv

# Ingesta de PDFs: UnstructuredPDFLoader corre con languages=["spa", "eng"],
# o sea OCR. Sin estos paquetes de sistema, subir un PDF falla en runtime con
# un error que no dice que falta tesseract.
sudo apt-get install -y tesseract-ocr tesseract-ocr-spa poppler-utils
```

**Confirma el puerto antes de arrancar.** La convención del droplet es serie
8000 para Python, y hasta donde está documentado 8000/8001/8002 están tomados
(`python-mariadb`, `geospace-nucleo`, `nutricion-api`). Este deploy asume
**8003**; verifica que siga libre:

```bash
pm2 list
ss -ltnp | grep 800
```

Si 8003 está ocupado, cambia el puerto en `.github/workflows/deploy.yml` y en
el bloque de nginx del paso 6.

## 3. Secrets de GitHub

En `Settings → Secrets and variables → Actions` del repo:

| Secret | Valor |
|---|---|
| `SSH_PRIVATE_KEY` | La llave privada con acceso al droplet |
| `SSH_HOST` | `165.22.53.200` |
| `SSH_USER` | `root` |

## 4. Primer arranque

El workflow **no crea el `.env`** a propósito (lleva la API key y el password
de admin). Créalo una vez a mano:

```bash
ssh root@165.22.53.200
mkdir -p ~/code && cd ~/code
git clone git@github.com:Moibe/constructor-agente-rag.git
cd constructor-agente-rag && git checkout dev
nano .env      # contenido abajo
chmod 600 .env
```

Contenido del `.env`:

```ini
# --- Almacenamiento (relativo al repo; pm2 arranca con este cwd) ---
LOG_DB_PATH=logs.db
AGENTES_DB_PATH=agentes.db
CHROMA_PATH=chroma
TEMP_FOLDER=./_temp
DOCS_FOLDER=./data/documentos

# --- Modelos: instancia sólo-OpenAI ---
# Sin esta línea el seed daría de alta los modelos de Ollama como activos y
# aparecerían en el dropdown aunque no haya un Ollama que los sirva.
OLLAMA_HABILITADO=false
TEXT_EMBEDDING_MODEL=text-embedding-3-small
OPENAI_API_KEY=sk-...

# --- HTTP ---
# Con nginx sirviendo los fronts y proxeando /api, el navegador nunca hace una
# petición cross-origin: todo sale del mismo origen. Cuando haya dominio, poner
# aquí la allowlist explícita igual, como defensa en profundidad.
CORS_ALLOWED_ORIGINS=*

# --- Admin ---
# Distinto al de CSI: son instancias separadas y no tienen por qué compartir
# credencial.
ADMIN_PASSWORD=<algo largo>
```

Luego dispara el workflow desde la pestaña **Actions → Run workflow** (o haz
push a `dev`). El workflow hace clone-if-missing, crea el venv, instala
requirements y levanta pm2.

## 5. Verificación

```bash
ssh root@165.22.53.200
pm2 list | grep constructor-rag         # debe decir online
curl -s 127.0.0.1:8003/health           # debe responder
curl -s "127.0.0.1:8003/modelos?solo_activos=true" | head -c 400
```

Lo esperado en ese último: **sólo modelos `openai`**. Si aparece `mistral` u
otro de Ollama, el `.env` no se está leyendo o falta `OLLAMA_HABILITADO=false`.
Ojo: el seed sólo aplica sobre una BD nueva — si la instancia ya arrancó una vez
sin la variable, los modelos ya existen y hay que desactivarlos con
`PUT /modelos/{nombre}` y `{"activo": false}`.

> ⚠️ **Hasta acá el sistema NO es alcanzable desde internet**, y es
> intencional: el `ufw` del droplet sólo abre 22/80/443/8383 y nginx rutea por
> `server_name`. Sin dominio no hay entrada pública. El paso 6 es el que la abre.

## 6. Cuando haya dominio (pendiente)

Arquitectura objetivo — **un solo proceso pm2** en vez de los tres de CSI,
porque nginx hace lo que allá hace el proxy de `vite preview`:

```
<admin>.moibe.me       → nginx: estáticos del build del admin      + /api/ → 127.0.0.1:8003
<asistentes>.moibe.me  → nginx: estáticos del build de los widgets + /api/ → 127.0.0.1:8003
```

Ambos fronts llaman al API por ruta relativa (`/api/...`), así que **no
necesitan cambios de código para esto**.

Bloque de nginx para el host de widgets (va en el repo `nx-routes`, un archivo
por dominio, nombrado exactamente como el dominio):

```nginx
server {
    server_name <asistentes>.moibe.me;
    root /root/code/host-asistentes/dist;
    index index.html;

    # El fallback SPA de /embed/* lo hace un plugin de vite en dev/preview
    # (embedSpaFallback en vite.config.js). Acá lo tiene que hacer nginx, o
    # /embed/chat/<slug> devuelve 404 en vez de cargar el widget.
    location /embed/ { try_files $uri /embed/index.html; }
    location /       { try_files $uri $uri/ /index.html; }

    location /api/ {
        rewrite ^/api/(.*)$ /$1 break;   # el backend no conoce el prefijo /api
        proxy_pass http://127.0.0.1:8003;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # Embeber un PDF tarda: 120s se queda corto y da 504 a medio proceso.
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }

    # Los uploads de documentos pasan por acá. El default de nginx es 1M y
    # cortaría cualquier PDF real con un 413.
    client_max_body_size 50M;

    listen 80;
}
```

El del admin es igual, cambiando `root` a `/root/code/buzzword-agentes-ui/dist`
y sin el `location /embed/`.

Después, los pasos manuales por dominio (gotchas ya documentados en
`~/.claude/project-references.md`; se repiten acá porque muerden siempre):

1. Crear el registro DNS A → `165.22.53.200`.
2. **Crear el symlink a mano** — el Action de `nx-routes` sólo hace `git pull` +
   reload de nginx, no crea el symlink de un dominio nuevo:
   `cd /etc/nginx/sites-enabled && ln -s ../sites-available/<dominio> <dominio>`
3. `nginx -t && systemctl reload nginx`
4. **Recién entonces** certbot, y **uno por dominio, nunca combinados**:
   `certbot --nginx -d <dominio>`
   (Si corres certbot antes del symlink, emite el cert pero pega el bloque SSL
   en el archivo de `geoservices.space`, que es el `default_server`.)

## 7. Pendiente en el admin, antes del paso 6

`buzzword-agentes-ui/src/App.svelte:75-84` construye los links a los widgets
como `location.hostname:4176`:

```js
const HOST_ASISTENTES_PORT = 4176;
const hostAsistentesBase = `${location.protocol}//${location.hostname}:${HOST_ASISTENTES_PORT}`;
```

Se usa en 7 lugares. En el droplet ese puerto no es alcanzable (`ufw`) y sobre
HTTPS sería mixed-content, así que los links de los widgets saldrían rotos. Hay
que hacer la base configurable (p.ej. `import.meta.env.VITE_HOST_ASISTENTES_BASE`)
con fallback al comportamiento actual, para que CSI siga funcionando idéntico.

## 8. Nota sobre el tamaño de la instalación

`requirements.txt` arrastra `torch` + `transformers` (~2 GB) por dos caminos:

- `traduce.py`, que **no es parte del API** (sólo lo importa `hola.py`, un
  scratch). Esas líneas se podrían quitar.
- `unstructured_inference`, que sí es dependencia real de la ingesta de PDFs
  con estrategia `hi_res` (escaneados) y **depende de torch**.

O sea: quitar `torch`/`transformers` del requirements no ahorra nada mientras
`unstructured_inference` siga. Si confirmas que todos tus PDFs son de texto (no
escaneados), quitar `unstructured_inference` sí baja la instalación ~2 GB a
cambio de perder el OCR. Decisión pendiente — por ahora se instala completo
para que el comportamiento sea idéntico al de CSI.
