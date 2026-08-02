# LEA WIN IA — Cómo publicarla (paso a paso, sin experiencia previa)

Esto te deja la app con un link público real, funcionando en cualquier celu, PC o tablet.
No requiere instalar nada en tu computadora.

---

## Paso 1 — Crear cuenta en GitHub

1. Andá a https://github.com y creá una cuenta gratis (con tu email).
2. Una vez adentro, arriba a la derecha tocá el **+** → **New repository**.
3. Nombre del repositorio: `lea-win-ia`. Dejalo en **Public** o **Private**, como prefieras.
4. NO marques ninguna casilla adicional (README, .gitignore, licencia). Dejalo vacío.
5. Tocá **Create repository**.

## Paso 2 — Subir estos archivos a GitHub (sin usar la terminal)

1. En la página del repositorio que acabás de crear, buscá el link que dice
   **"uploading an existing file"**.
2. Arrastrá TODOS los archivos y carpetas de esta carpeta (`lea_win`) ahí:
   - `app.py`
   - `requirements.txt`
   - `Procfile`
   - `render.yaml`
   - `.gitignore`
   - la carpeta `templates` completa (con `index.html` adentro)
3. Abajo escribí un mensaje corto como "primera versión" y tocá **Commit changes**.

## Paso 3 — Crear cuenta en Render

1. Andá a https://render.com y creá una cuenta gratis.
   Lo más fácil: elegí "Sign up with GitHub" para que queden conectados automáticamente.

## Paso 4 — Publicar la app

1. Adentro de Render, tocá **New +** → **Web Service**.
2. Elegí el repositorio `lea-win-ia` que subiste.
3. Render va a detectar el archivo `render.yaml` solo y va a completar la configuración
   (build command, start command) automáticamente. Si te pide confirmar, dejá los valores
   que aparecen.
4. Elegí el plan **Free**.
5. Tocá **Create Web Service** o **Deploy**.

Vas a ver logs corriendo — tarda entre 2 y 5 minutos la primera vez.

## Paso 5 — Tu link real

Cuando termine, Render te va a dar una URL parecida a:

```
https://lea-win-ia.onrender.com
```

Esa es tu app, publicada, accesible desde cualquier lado. Guardala.

---

## Nota sobre el plan gratuito de Render

El plan Free "se duerme" después de 15 minutos sin uso, y tarda unos 30-50 segundos en
"despertarse" la próxima vez que alguien entra. Para uso personal o para probarla con
amigos está perfecto. Si más adelante la publicás en Play Store y cobrás entrada, vas a
necesitar pasar a un plan pago (arranca alrededor de USD 7/mes) para que no se duerma.

## Si algo falla

- Si el scraping a Stud Book no funciona (por ejemplo, si el sitio cambió su estructura
  HTML), vas a ver un error explicado en la propia app, no una pantalla en blanco. Guardá
  ese mensaje y lo revisamos juntos.
- Los "logs" en Render (pestaña **Logs** de tu servicio) muestran qué está pasando en el
  servidor en tiempo real — es lo primero que hay que mirar si algo no anda.
