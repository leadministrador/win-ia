from flask import Flask, render_template, request, jsonify
import requests, re, sqlite3, json, os, time, threading, hashlib, secrets
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote_plus, quote
from datetime import datetime, timedelta

app = Flask(__name__)
BASE = "https://www.studbook.org.ar"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LEA-WIN-IA/1.0)",
    "Accept-Language": "es-AR,es;q=0.9"
}
DB = os.getenv("LEA_DB", "lea_win.db")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")

def clean(v):
    return re.sub(r"\s+", " ", v or "").strip()

def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=(4, 8))
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS carreras(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fecha TEXT NOT NULL, hipodromo TEXT NOT NULL, numero INTEGER NOT NULL,
      premio TEXT, distancia INTEGER, superficie TEXT, estado_publicado TEXT,
      condicion TEXT, pista_dia TEXT, clima TEXT, viento TEXT, retiros TEXT,
      observaciones TEXT, participantes TEXT NOT NULL, analisis TEXT,
      resultado_real TEXT, creado_en TEXT NOT NULL,
      UNIQUE(fecha,hipodromo,numero)
    );
    CREATE TABLE IF NOT EXISTS cache(
      clave TEXT PRIMARY KEY,
      valor TEXT NOT NULL,
      actualizado_en TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS pronosticos(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      url TEXT NOT NULL, numero INTEGER NOT NULL,
      fecha TEXT, hipodromo TEXT,
      ranking TEXT NOT NULL,        -- lo que predijo la app
      resultado TEXT,               -- puestos reales, cuando la carrera se corre
      acierto_ganador INTEGER,      -- 1 si acerto el 1o, 0 si no, NULL si no corrio
      aciertos_top4 INTEGER,        -- cuantos de los 4 predichos entraron entre los 4
      pesos_usados TEXT,            -- version del algoritmo con la que se predijo
      creado_en TEXT NOT NULL,
      comparado_en TEXT,
      UNIQUE(url,numero)
    );
    CREATE TABLE IF NOT EXISTS algoritmo(
      clave TEXT PRIMARY KEY,
      valor REAL NOT NULL,
      actualizado_en TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS condiciones(
      fecha TEXT NOT NULL,
      hipodromo TEXT NOT NULL,
      pista TEXT, estado TEXT, viento TEXT, clima TEXT,
      observaciones TEXT,
      cargado_en TEXT NOT NULL,
      PRIMARY KEY(fecha, hipodromo)
    );
    CREATE TABLE IF NOT EXISTS usuarios(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      usuario TEXT NOT NULL UNIQUE,       -- en minusculas, para no repetir
      usuario_visible TEXT NOT NULL,      -- como lo escribio el
      clave_hash TEXT NOT NULL,
      telefono TEXT,                      -- opcional, para recuperar la clave
      email TEXT,                         -- opcional
      creado_en TEXT NOT NULL,
      ultimo_ingreso TEXT,
      bloqueado INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS sesiones(
      token TEXT PRIMARY KEY,
      usuario_id INTEGER NOT NULL,
      creada_en TEXT NOT NULL,
      ultima_vez TEXT NOT NULL
    );
    """)
    con.commit()
    con.close()

# --- Cache genérico con TTL, para no depender de scrapear en cada request ---
TTL_CALENDARIO = 2 * 60 * 60      # 2hs: el calendario cambia poco
TTL_REUNION = 15 * 60              # 15 min: carreras/participantes del día
TTL_CARRERA = 15 * 60

def cache_get(clave, ttl_seg):
    con = db()
    row = con.execute(
        "SELECT valor, actualizado_en FROM cache WHERE clave=?", (clave,)
    ).fetchone()
    con.close()
    if not row:
        return None, False
    edad = (datetime.now() - datetime.fromisoformat(row["actualizado_en"])).total_seconds()
    fresco = edad <= ttl_seg
    try:
        return json.loads(row["valor"]), fresco
    except (json.JSONDecodeError, TypeError):
        return None, False

def cache_set(clave, valor):
    con = db()
    con.execute("""
        INSERT INTO cache(clave, valor, actualizado_en) VALUES(?,?,?)
        ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor, actualizado_en=excluded.actualizado_en
    """, (clave, json.dumps(valor, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")))
    con.commit()
    con.close()

def con_cache(clave, ttl_seg, forzar, fetch_fn):
    """
    Usa cache fresco si existe. Si está vencido o no existe (o se fuerza refresh),
    intenta traer datos en vivo. Si la fuente en vivo falla, devuelve el cache
    aunque esté viejo (mejor dato viejo que error), indicando el origen.
    """
    cacheado, fresco = cache_get(clave, ttl_seg)
    if cacheado is not None and fresco and not forzar:
        return cacheado, "cache"
    try:
        dato_vivo = fetch_fn()
        cache_set(clave, dato_vivo)
        return dato_vivo, "vivo"
    except Exception:
        if cacheado is not None:
            return cacheado, "cache_vencido"
        raise

def extract_races_from_meeting(soup):
    """
    Lee las carreras de una reunion. Ademas del numero y el titulo,
    saca la HORA, que hace falta para saber cual es la proxima a correrse.
    """
    races = []
    for h in soup.find_all(["h1", "h2", "h3", "h4"]):
        texto = clean(h.get_text(" "))
        m = re.search(r"(\d+)\s*[º°ª]?\s*Carrera\b", texto, re.I)
        if not m:
            continue
        # La hora viene en el mismo titulo: "1º Carrera - 13:30"
        mh = re.search(r"(\d{1,2}):(\d{2})", texto)
        hora = ""
        if mh:
            h_, mi = int(mh.group(1)), int(mh.group(2))
            if 0 <= h_ <= 23 and 0 <= mi <= 59:
                hora = f"{h_:02d}:{mi:02d}"
        races.append({
            "numero": int(m.group(1)),
            "titulo": texto,
            "hora": hora,
        })
    # Sin repetidos, en orden de numero.
    vistos, unicas = set(), []
    for r in races:
        if r["numero"] in vistos:
            continue
        vistos.add(r["numero"])
        unicas.append(r)
    unicas.sort(key=lambda r: r["numero"])
    return unicas

def _cell_text(cell):
    return clean(cell.get_text(" ", strip=True))


def _map_headers(header_cells):
    """
    Mapea los encabezados de la tabla a indices de columna.
    La tabla del Stud Book usa: P | O | Ejemplar | S | P | E | Kg | Jockey | Kg |
    Entrenador | Caballeriza | Cpos | Acum. | Pago
    Hay DOS columnas 'Kg': la anterior al jockey es el peso corporal del animal,
    la posterior es el peso que lleva encima (la que importa para el analisis).
    """
    idx = {}
    kg_positions = []
    for i, cell in enumerate(header_cells):
        h = _cell_text(cell).lower().rstrip(".")
        if h == "ejemplar" and "nombre" not in idx:
            idx["nombre"] = i
        elif h == "jockey" and "jockey" not in idx:
            idx["jockey"] = i
        elif h == "entrenador" and "entrenador" not in idx:
            idx["entrenador"] = i
        elif h == "caballeriza" and "caballeriza" not in idx:
            idx["caballeriza"] = i
        elif h == "o" and "numero" not in idx:
            idx["numero"] = i
        elif h == "p" and "puesto" not in idx:
            idx["puesto"] = i          # el primer 'P' es el puesto final
        elif h == "e" and "edad" not in idx:
            idx["edad"] = i
        elif h == "s" and "sexo" not in idx:
            idx["sexo"] = i
        elif h == "kg":
            kg_positions.append(i)
        elif h in ("cpos", "cuerpos"):
            idx["cuerpos"] = i
        elif h in ("acum", "acumulado"):
            idx["acumulado"] = i
        elif h == "pago":
            idx["pago"] = i

    # Resolver cual de los dos 'Kg' es el peso que lleva encima.
    jockey_i = idx.get("jockey")
    if kg_positions:
        if jockey_i is not None:
            despues = [k for k in kg_positions if k > jockey_i]
            antes = [k for k in kg_positions if k < jockey_i]
            if despues:
                idx["peso"] = despues[0]
            if antes:
                idx["peso_corporal"] = antes[-1]
        if "peso" not in idx:
            idx["peso"] = kg_positions[-1]
    return idx


def _find_header_row(table):
    """Devuelve las celdas del encabezado, sea <th> o la primera fila."""
    for tr in table.find_all("tr"):
        ths = tr.find_all("th")
        if ths:
            return ths
    first = table.find("tr")
    return first.find_all(["th", "td"]) if first else []


def _parse_participants_table(table):
    """Lee una tabla de participantes y devuelve la lista de caballos."""
    header_cells = _find_header_row(table)
    if not header_cells:
        return []
    idx = _map_headers(header_cells)
    if "nombre" not in idx:
        return []

    participants = []
    header_texts = {_cell_text(c).lower() for c in header_cells}

    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if not cells or len(cells) <= idx["nombre"]:
            continue
        # saltear la fila de encabezado si vino como td
        if {_cell_text(c).lower() for c in cells} & header_texts == {
            _cell_text(c).lower() for c in cells
        }:
            continue

        name_cell = cells[idx["nombre"]]
        link = name_cell.find("a", href=True)
        name = clean(link.get_text(" ")) if link else _cell_text(name_cell)
        if not name:
            continue

        def col(key):
            i = idx.get(key)
            if i is None or i >= len(cells):
                return ""
            return _cell_text(cells[i])

        peso = col("peso").replace(",", ".")
        peso = peso if re.fullmatch(r"\d{2}(\.\d)?", peso or "") else ""

        numero_raw = col("numero")
        numero = int(numero_raw) if numero_raw.isdigit() else None

        puesto_raw = col("puesto")
        puesto = int(puesto_raw) if puesto_raw.isdigit() else None

        detalle_partes = [
            f"Jockey: {col('jockey')}" if col("jockey") else "",
            f"Entrenador: {col('entrenador')}" if col("entrenador") else "",
            f"Caballeriza: {col('caballeriza')}" if col("caballeriza") else "",
            f"Edad: {col('edad')}" if col("edad") else "",
            f"Sexo: {col('sexo')}" if col("sexo") else "",
        ]

        participants.append({
            "numero": numero,
            "nombre": name,
            "perfil": urljoin(BASE, link["href"]) if link else "",
            "jockey": col("jockey"),
            "entrenador": col("entrenador"),
            "caballeriza": col("caballeriza"),
            "edad": col("edad"),
            "sexo_tabla": col("sexo"),
            "peso": peso,
            "peso_corporal": col("peso_corporal"),
            "puesto": puesto,
            "cuerpos": col("cuerpos"),
            "acumulado": col("acumulado"),
            "pago": col("pago"),
            "detalle": " · ".join(p for p in detalle_partes if p)[:700],
            "retirado": False,
        })

    return participants


def parse_race(soup, numero):
    heading = None
    pat = re.compile(rf"^{numero}\s*[º°ª]?\s*Carrera\b", re.I)
    for h in soup.find_all(["h1", "h2", "h3", "h4"]):
        if pat.search(clean(h.get_text(" "))):
            heading = h
            break
    if not heading:
        return None

    # Recolectar todo lo que va desde este encabezado hasta el de la carrera siguiente.
    nodes = []
    for node in heading.find_all_next():
        if node is not heading and node.name in ["h1", "h2", "h3", "h4"] and re.search(
            r"\d+\s*[º°ª]?\s*Carrera\b", clean(node.get_text(" ")), re.I
        ):
            break
        nodes.append(node)

    block = clean(" ".join(
        n.get_text(" ", strip=True) for n in nodes if hasattr(n, "get_text")
    ))

    def get(pattern):
        m = re.search(pattern, block, re.I)
        return clean(m.group(1)) if m else ""

    # Buscar la tabla de participantes: la primera que tenga columna 'Ejemplar'
    # y filas con enlaces a /ejemplares/.
    # Se recorre en orden: todo lo que aparezca DESPUES de un titulo
    # 'RETIRADOS' corresponde a caballos que no corren.
    participants = []
    tablas_vistas = set()
    en_retirados = False
    for node in nodes:
        texto_nodo = clean(node.get_text(" ")) if hasattr(node, "get_text") else ""
        if getattr(node, "name", None) != "table":
            # Un titulo/celda corto que diga RETIRADOS marca el corte.
            if re.fullmatch(r"RETIRADOS?", texto_nodo, re.I):
                en_retirados = True
            continue
        if id(node) in tablas_vistas:
            continue
        tablas_vistas.add(id(node))
        filas = _parse_participants_table(node)
        for fila in filas:
            fila["retirado"] = en_retirados
        if filas:
            participants.extend(filas)

    # Deduplicar por nombre conservando el orden de aparicion.
    vistos, unicos = set(), []
    for p in participants:
        if p["nombre"] in vistos:
            continue
        vistos.add(p["nombre"])
        unicos.append(p)
    participants = unicos

    return {
        "carrera": numero,
        "premio": get(r"Premio:\s*(.+?)\s+Distancia:"),
        "distancia": get(r"Distancia:\s*(\d+)\s*mts"),
        "condicion": get(r"Condición:\s*(.+?)\s+Pista:"),
        "superficie": get(r"Pista:\s*(.+?)\s*\|\s*Estado:"),
        "estado": get(r"Estado:\s*(.+?)\s*\|\s*Categoria:"),
        "categoria": get(r"Categoria:\s*([A-Za-zÁÉÍÓÚáéíóúñÑ]+(?:\s+[A-Za-zÁÉÍÓÚáéíóúñÑ]+)?)"),
        "participantes": participants
    }


# Codigos que usa el Stud Book en la ficha del caballo, comprobados en el sitio.
CODIGOS_HIPODROMO = {
    "ARG": "Palermo", "SIS": "San Isidro", "LPA": "La Plata",
    "ROS": "Rosario", "TAN": "Tandil", "DOL": "Dolores",
    "AZL": "Azul", "TUC": "Tucumán", "SLU": "La Punta",
    "CBA": "Córdoba", "MZA": "Mendoza", "SFE": "Santa Fe",
    "NQN": "Neuquén", "SR": "San Rafael", "TDL": "Tandil",
    "LP": "La Plata", "SI": "San Isidro",
}

def nombre_hipodromo(codigo):
    """Devuelve el nombre del hipodromo a partir de su codigo."""
    c = clean(codigo).upper()
    return CODIGOS_HIPODROMO.get(c, codigo)


def _tabla_carreras_del_perfil(soup):
    """
    Busca la tabla CARRERAS de la ficha del ejemplar y la lee por columnas:
    Fecha | video | Hip. | Nº | O | Dist. | Tiempo | Premio | Cat. | Cond. |
    P | E | Kg | Jockey | Caballeriza
    Devuelve una lista de carreras corridas, cada una con su video si lo tiene.
    """
    mejor = []
    for table in soup.find_all("table"):
        encabezados = [
            clean(th.get_text(" ")).lower().rstrip(".")
            for th in (table.find_all("th") or [])
        ]
        # La tabla de campaña se reconoce por tener Dist. y Jockey.
        if not any("dist" in h for h in encabezados):
            continue
        if not any("jockey" in h for h in encabezados):
            continue

        idx = {}
        for i, h in enumerate(encabezados):
            # OJO con el orden de estas condiciones. Comprobado en el sitio,
            # los encabezados son:
            #   Hip. | N° | O | Dist. | Tiempo | Premio | Cat. | Cond. |
            #   P | E | Kg | Jockey | Caballeriza | Pos. | Importe | Pago
            # 'Pos.' es el PUESTO de llegada. 'N°' es el numero de reunion
            # y 'O' el numero que llevo el caballo. No confundirlos.
            if h.startswith("pos") and "puesto" not in idx: idx["puesto"] = i
            elif "hip" in h and "hipodromo" not in idx: idx["hipodromo"] = i
            elif h in ("n°", "n", "nº") and "reunion" not in idx: idx["reunion"] = i
            elif h == "o" and "numero" not in idx: idx["numero"] = i
            elif "dist" in h and "distancia" not in idx: idx["distancia"] = i
            elif "tiempo" in h and "tiempo" not in idx: idx["tiempo"] = i
            elif "premio" in h and "premio" not in idx: idx["premio"] = i
            elif h == "cat" and "categoria" not in idx: idx["categoria"] = i
            elif h == "cond" and "condicion" not in idx: idx["condicion"] = i
            elif h == "p" and "pista" not in idx: idx["pista"] = i
            elif h == "e" and "estado" not in idx: idx["estado"] = i
            elif h == "kg" and "kilos" not in idx: idx["kilos"] = i
            elif "jockey" in h and "jockey" not in idx: idx["jockey"] = i
            elif "caballeriza" in h and "caballeriza" not in idx: idx["caballeriza"] = i
            elif "importe" in h and "importe" not in idx: idx["importe"] = i
            elif h == "pago" and "pago" not in idx: idx["pago"] = i

        filas = []
        for tr in table.find_all("tr"):
            celdas = tr.find_all("td")
            if not celdas:
                continue
            texto_fila = clean(tr.get_text(" "))
            m_fecha = re.search(r"(\d{2}/\d{2}/\d{4})", texto_fila)
            if not m_fecha:
                continue

            def col(clave):
                i = idx.get(clave)
                if i is None or i >= len(celdas):
                    return ""
                return clean(celdas[i].get_text(" "))

            # El video es un enlace a youtube dentro de la fila.
            video = ""
            for a in tr.find_all("a", href=True):
                m_yt = re.search(r"youtube(?:-nocookie)?\.com/embed/([A-Za-z0-9_-]+)", a["href"])
                if m_yt:
                    video = m_yt.group(1)
                    break

            enlace_carrera = ""
            for a in tr.find_all("a", href=True):
                if "/reuniones/carrera/" in a["href"]:
                    enlace_carrera = urljoin(BASE, a["href"])
                    break

            puesto_txt = col("puesto")
            filas.append({
                "fecha": m_fecha.group(1),
                "hipodromo": nombre_hipodromo(col("hipodromo")),
                "hipodromo_codigo": col("hipodromo"),
                "puesto": int(puesto_txt) if puesto_txt.isdigit() else None,
                "numero": col("numero"),
                "reunion": col("reunion"),
                "distancia": col("distancia"),
                "tiempo": col("tiempo"),
                "premio": col("premio"),
                "categoria": col("categoria"),
                "condicion": col("condicion"),
                "pista": col("pista"),
                "estado": col("estado"),
                "kilos": col("kilos"),
                "jockey": col("jockey"),
                "caballeriza": col("caballeriza"),
                "importe": col("importe"),
                "pago": col("pago"),
                "video": video,
                "enlace": enlace_carrera,
            })

        if len(filas) > len(mejor):
            mejor = filas
    return mejor


def _resumen_del_perfil(soup, texto):
    """Saca un resumen corto y legible, sin el bloque gigante de porcentajes."""
    resumen = {}

    m = re.search(r"\b(Macho|Hembra)\b", texto, re.I)
    resumen["sexo"] = m.group(1) if m else ""

    m = re.search(r"(\d{2}/\d{2}/\d{4})\s*\((\d+)\s*años?\)", texto)
    if m:
        resumen["nacimiento"] = m.group(1)
        resumen["edad"] = m.group(2)

    # Padre y madre: aparecen como "por PADRE y MADRE"
    m = re.search(r"\bpor\s+(.+?)\s+y\s+(.+?)\s+por\b", texto)
    if m:
        resumen["padre"] = clean(m.group(1))[:60]
        resumen["madre"] = clean(m.group(2))[:60]

    # Frase resumen que el propio sitio arma, ej:
    # "Ganadora de 4 carreras en Palermo - $31.320.000, a los 4 y 5 años."
    # Se corta en el punto final real, no en los puntos de miles.
    m = re.search(
        r"(Ganador[a]?\s+de\s+\d+\s+carreras?.{0,160}?\.)(?:\s|$)",
        texto, re.I
    )
    if m:
        frase = clean(m.group(1))
        # Si se cortó dentro de un número (ej "$31."), estirar hasta el punto siguiente.
        if re.search(r"\$[\d.]*\.$", frase):
            m2 = re.search(
                r"(Ganador[a]?\s+de\s+\d+\s+carreras?.{0,200}?años?\.)",
                texto, re.I
            )
            if m2:
                frase = clean(m2.group(1))
        resumen["logro"] = frase

    m = re.search(r"CARRERAS\s*\((\d+)\)", texto, re.I)
    if m:
        resumen["total_carreras"] = m.group(1)

    return resumen


TTL_DETALLE_CARRERA = 30 * 24 * 60 * 60   # 30 dias: una carrera corrida ya no cambia


def detalle_de_carrera(url_carrera):
    """
    Entra a la pagina de una carrera y saca, en palabras, la condicion
    y el estado de la pista. Se guarda en cache porque una carrera ya
    corrida no cambia nunca.
    """
    if not url_carrera:
        return {}

    clave = f"detalle_carrera:{url_carrera}"
    cacheado, fresco = cache_get(clave, TTL_DETALLE_CARRERA)
    if cacheado is not None and fresco:
        return cacheado

    try:
        soup = fetch(url_carrera)
        texto = clean(soup.get_text(" "))

        def sacar(patron):
            m = re.search(patron, texto, re.I)
            return clean(m.group(1)) if m else ""

        detalle = {
            "condicion_txt": sacar(r"Condición:\s*(.+?)\s*Pista:"),
            "pista_txt": sacar(r"Pista:\s*(.+?)\s*\|\s*Estado:"),
            "estado_txt": sacar(r"Estado:\s*(.+?)\s*\|\s*Categoria"),
            "categoria_txt": sacar(
                r"Categoria:\s*([A-Za-zÁÉÍÓÚáéíóúñÑ]+(?:\s+de\s+[A-Za-zÁÉÍÓÚáéíóúñÑ]+)?)\b"
            ),
        }
        cache_set(clave, detalle)
        return detalle
    except Exception:
        return cacheado or {}


TTL_FICHA_CABALLO = 6 * 60 * 60   # 6 horas: la campaña no cambia en el día


def enrich_horse(horse):
    profile = horse.get("perfil", "")
    if not profile:
        return horse

    # Si ya se consultó hace poco, se usa lo guardado y no se vuelve a pedir.
    clave = f"ficha:{profile}"
    guardada, fresca = cache_get(clave, TTL_FICHA_CABALLO)
    if guardada is not None and fresca:
        horse.update(guardada)
        return horse

    try:
        soup = fetch(profile)
        texto = clean(soup.get_text(" "))

        resumen = _resumen_del_perfil(soup, texto)
        horse.update({k: v for k, v in resumen.items() if v})
        horse.setdefault("sexo", "")

        carreras = _tabla_carreras_del_perfil(soup)
        horse["carreras"] = carreras[:20]

        # El estado de la pista de cada carrera viene como codigo ("5", "A").
        # Para que el algoritmo pueda compararlo con la pista del dia hace
        # falta la palabra. Se traen las 4 mas recientes, TODAS JUNTAS.
        recientes = [c for c in horse["carreras"][:4] if c.get("enlace")]
        if recientes:
            def traer(c):
                try:
                    c.update(detalle_de_carrera(c["enlace"]))
                except Exception:
                    pass
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(traer, recientes))

        # Contadores para el pronostico y para mostrar.
        puestos = [c["puesto"] for c in carreras if c["puesto"]]
        horse["victorias"] = sum(1 for p in puestos if p == 1)
        horse["podios"] = sum(1 for p in puestos if p <= 3)
        horse["corridas"] = len(carreras)

        # Compatibilidad con el resto del codigo que espera 'actuaciones'.
        horse["actuaciones"] = [
            f"{c['fecha']} {c['hipodromo']} {c['puesto']}º {c['distancia']}m"
            for c in carreras if c["puesto"]
        ][:20]
        horse["campana"] = resumen.get("logro", "")
        horse["cargado"] = True

        # Guardar solo lo que se trajo del Stud Book, para no volver a pedirlo.
        cache_set(clave, {
            k: horse[k] for k in
            ("sexo", "edad", "nacimiento", "padre", "madre", "logro",
             "carreras", "victorias", "podios", "corridas",
             "actuaciones", "campana", "cargado")
            if k in horse
        })
    except Exception:
        horse.setdefault("sexo", "")
        horse.setdefault("campana", "")
        horse.setdefault("actuaciones", [])
        horse.setdefault("carreras", [])
        horse["cargado"] = True   # se intentó; no queda "cargando" para siempre
    return horse

# ============================================================
# APRENDIZAJE: los valores del algoritmo dejan de ser fijos.
# Se guardan en la base y se ajustan comparando pronostico vs resultado.
# ============================================================

PESOS_INICIALES = {
    "campana_disponible": 1.2,
    "registra_victorias": 8.0,
    "hipodromos_principales": 4.0,
    "peso_liviano": 5.0,
    "peso_pesado": -3.0,
    "victoria_reciente": 4.0,
    "podio_reciente": 2.0,
    "pista_compatible": 7.0,
}

def cargar_pesos():
    """Lee los pesos del algoritmo. Si no existen todavia, usa los iniciales."""
    try:
        con = db()
        filas = con.execute("SELECT clave, valor FROM algoritmo").fetchall()
        con.close()
        guardados = {f["clave"]: f["valor"] for f in filas}
    except Exception:
        guardados = {}
    pesos = dict(PESOS_INICIALES)
    pesos.update({k: v for k, v in guardados.items() if k in PESOS_INICIALES})
    return pesos

def guardar_pesos(pesos):
    con = db()
    ahora = datetime.now().isoformat(timespec="seconds")
    for clave, valor in pesos.items():
        con.execute("""
            INSERT INTO algoritmo(clave, valor, actualizado_en) VALUES(?,?,?)
            ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor,
                                            actualizado_en=excluded.actualizado_en
        """, (clave, float(valor), ahora))
    con.commit()
    con.close()


def score_horse(h, context, pesos=None):
    # Puntaje transparente. Solo usa datos detectados o cargados.
    P = pesos if pesos is not None else cargar_pesos()
    score, reasons = 50.0, []
    acts = h.get("actuaciones", [])
    campaign = h.get("campana", "").lower()
    detail = h.get("detalle", "").lower()

    if acts:
        score += min(14, len(acts) * P["campana_disponible"])
        reasons.append("tiene campaña reciente disponible")
    if "ganador" in campaign or "ganadora" in campaign:
        score += P["registra_victorias"]; reasons.append("registra victorias")
    if "debut" in campaign or not acts:
        score += 1
        reasons.append("debutante o historial limitado: se mantiene sin penalización fuerte")
    if any(x in campaign for x in ["palermo","san isidro","la plata"]):
        score += P["hipodromos_principales"]
        reasons.append("experiencia en hipódromos principales")
    # Peso relativo al resto de la carrera (no un umbral fijo).
    peso_propio = _to_float(h.get("peso"))
    pesos_carrera = [
        _to_float(x.get("peso"))
        for x in context.get("participantes", [])
        if _to_float(x.get("peso")) is not None
    ]
    if peso_propio is not None and len(pesos_carrera) >= 2:
        promedio = sum(pesos_carrera) / len(pesos_carrera)
        diferencia = promedio - peso_propio
        if diferencia >= 1.5:
            score += P["peso_liviano"]
            reasons.append(f"lleva {diferencia:.1f} kg menos que el promedio de la carrera")
        elif diferencia <= -1.5:
            score += P["peso_pesado"]
            reasons.append(f"lleva {abs(diferencia):.1f} kg más que el promedio de la carrera")

    # Victorias y podios: si vienen contados de la ficha se usan directo;
    # si no, se intentan leer del texto de las actuaciones.
    if h.get("corridas") is not None:
        victorias = h.get("victorias", 0)
        podios = h.get("podios", 0)
    else:
        victorias = len(re.findall(r"\b1\s*[º°]", " ".join(acts)))
        podios = len(re.findall(r"\b[123]\s*[º°]", " ".join(acts)))
    if victorias:
        score += min(10, victorias * P["victoria_reciente"])
        reasons.append(f"{victorias} victoria(s) en su campaña")
    if podios > victorias:
        score += min(6, (podios - victorias) * P["podio_reciente"])
        reasons.append(f"{podios} llegada(s) entre los tres primeros")

    # Pista exigente: se premia el antecedente en ESE estado.
    # El dato correcto esta en cada carrera de la campana (campo 'estado'),
    # NO en la frase resumen, donde nunca figura el tipo de piso.
    estado_dia = context.get("estado") or context.get("pista_dia") or ""
    if estado_dia:
        clave_estado = normalize_text(estado_dia)
        PARECIDOS = {
            "pesada": ["barrosa", "humeda"],
            "barrosa": ["pesada", "humeda"],
            "humeda": ["pesada", "barrosa"],
            "liviana": ["normal"],
            "normal": ["liviana"],
        }

        # Como le fue en ese estado de pista, y en los parecidos.
        exactas, parecidas = [], []
        for c in h.get("carreras", []):
            est = normalize_text(c.get("estado_txt") or c.get("estado") or "")
            if not est or not c.get("puesto"):
                continue
            if est == clave_estado:
                exactas.append(c["puesto"])
            elif est in PARECIDOS.get(clave_estado, []):
                parecidas.append(c["puesto"])

        def rinde_bien(puestos):
            # Entro entre los tres primeros en al menos un tercio de esas salidas.
            if not puestos:
                return False
            return sum(1 for p in puestos if p <= 3) >= max(1, len(puestos) / 3)

        if exactas:
            if rinde_bien(exactas):
                score += P["pista_compatible"]
                reasons.append(
                    f"corrió {len(exactas)} vez/veces en pista {estado_dia.lower()} y anduvo bien")
            else:
                score -= P["pista_compatible"] * 0.6
                reasons.append(
                    f"corrió {len(exactas)} vez/veces en pista {estado_dia.lower()} sin buen resultado")
        elif parecidas and rinde_bien(parecidas):
            score += P["pista_compatible"] * 0.5
            reasons.append(f"anduvo bien en pista parecida a {estado_dia.lower()}")

        # Respaldo: si no hay campana cargada, se mira la frase resumen.
        elif not h.get("carreras"):
            if clave_estado in normalize_text(campaign + " " + detail):
                score += P["pista_compatible"] * 0.5
                reasons.append(f"antecedente en pista {estado_dia.lower()}")

    # Viento en contra: castiga a los que llevan más peso que el promedio.
    if context.get("viento") == "En contra" and peso_propio is not None:
        if pesos_carrera and peso_propio > (sum(pesos_carrera)/len(pesos_carrera)):
            score += P["peso_pesado"] * 0.6
            reasons.append("viento en contra y lleva peso por encima del promedio")

    # Césped: es una superficie muy distinta, el que nunca corrió ahí arranca en desventaja.
    if normalize_text(context.get("pista", "")).startswith("cesped"):
        en_cesped = [c for c in h.get("carreras", [])
                     if "cesped" in normalize_text(c.get("pista_txt") or c.get("pista") or "")]
        if h.get("carreras") and not en_cesped:
            score -= P["pista_compatible"] * 0.5
            reasons.append("nunca corrió en césped")
        elif en_cesped:
            score += P["pista_compatible"] * 0.4
            reasons.append(f"tiene {len(en_cesped)} carrera(s) en césped")

    return round(max(1, min(99, score)), 1), reasons


def _to_float(value):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def normalize_text(value):
    value = clean(value).lower()
    for source, target in {
        "á": "a", "é": "e", "í": "i",
        "ó": "o", "ú": "u", "ü": "u",
    }.items():
        value = value.replace(source, target)
    return value


def meeting_date_from_url(url):
    match = re.search(r"(?<!\d)(20\d{6})(?!\d)", url or "")
    if not match:
        return ""
    try:
        return datetime.strptime(
            match.group(1),
            "%Y%m%d",
        ).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def calendar_from_meetings(soup):
    meetings = []
    seen = set()

    for link in soup.select('a[href*="/reuniones/detalle/"]'):
        href = urljoin(BASE, link.get("href", ""))
        date = meeting_date_from_url(href)
        racecourse = clean(link.get_text(" ")) or "Hipódromo"

        if not date:
            continue

        key = (normalize_text(racecourse), date, href)
        if key in seen:
            continue
        seen.add(key)

        meetings.append({
            "hipodromo": racecourse,
            "fecha": date,
            "url": href,
        })

    meetings.sort(
        key=lambda item: (
            item["fecha"],
            normalize_text(item["hipodromo"]),
        )
    )
    return meetings


def _codigo_recaptcha(soup):
    """
    El sitio exige un codigo 'recaptcha' en la direccion para cambiar de mes.
    Ese codigo viene dentro de la propia pagina de reuniones.
    """
    # 1) En algun enlace de la propia pagina.
    for a in soup.find_all("a", href=True):
        m = re.search(r"[?&]recaptcha=([^&\"']+)", a["href"])
        if m:
            return m.group(1)
    # 2) En un campo oculto del formulario.
    campo = soup.find("input", attrs={"name": "recaptcha"})
    if campo and campo.get("value"):
        return campo["value"]
    # 3) En el codigo de la pagina.
    m = re.search(r"recaptcha['\"]?\s*[:=]\s*['\"]([A-Za-z0-9_\-]{40,})", str(soup))
    if m:
        return m.group(1)
    return ""


def _sesion_studbook():
    """
    Una sesion que conserva las cookies y se presenta como un navegador
    real. Hace falta porque el sitio puede recordar el mes elegido en la
    sesion, y porque puede rechazar pedidos que no parezcan de un navegador.
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0 Safari/537.36"),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "es-AR,es;q=0.9",
        "Referer": BASE + "/reuniones",
        "Upgrade-Insecure-Requests": "1",
    })
    return s


def _datos_del_formulario(soup):
    """
    Lee el formulario de la pagina de reuniones y devuelve como se llaman
    sus campos y si se envia por GET o por POST. No se adivina: se lee.
    """
    for form in soup.find_all("form"):
        campos = {}
        nombres = []
        for inp in form.find_all(["input", "select"]):
            n = inp.get("name")
            if not n:
                continue
            nombres.append(n)
            if inp.name == "input":
                campos[n] = inp.get("value", "")
            else:
                sel = inp.find("option", selected=True) or inp.find("option")
                campos[n] = sel.get("value", "") if sel else ""
        # El formulario del calendario tiene los campos de mes y año.
        texto = " ".join(nombres).lower()
        if any(x in texto for x in ["mes", "month", "anio", "año", "year"]):
            return {
                "accion": urljoin(BASE, form.get("action") or "/reuniones"),
                "metodo": (form.get("method") or "get").lower(),
                "campos": campos,
                "nombres": nombres,
            }
    return None


def _traer_mes(anio, mes):
    """
    Trae la pagina de un mes concreto. Prueba, en orden, las tres vias
    posibles, cada una con la sesion abierta para conservar las cookies:
      1) el formulario tal como lo declara la pagina
      2) por direccion, con el codigo que traiga la pagina
      3) por direccion pelada
    Devuelve (reuniones, via_que_funciono).
    """
    prefijo = f"{anio}-{mes:02d}-"
    s = _sesion_studbook()

    # Primero se abre la pagina normal: asi se obtienen cookies y el formulario.
    r0 = s.get(BASE + "/reuniones", timeout=(5, 15))
    r0.raise_for_status()
    soup0 = BeautifulSoup(r0.text, "html.parser")

    formulario = _datos_del_formulario(soup0)
    codigo = _codigo_recaptcha(soup0)

    intentos = []

    # 1) El formulario, tal como lo declara la pagina.
    if formulario:
        campos = dict(formulario["campos"])
        for n in formulario["nombres"]:
            bajo = n.lower()
            if "mes" in bajo or "month" in bajo:
                campos[n] = f"{mes:02d}"
            elif "anio" in bajo or "año" in bajo or "year" in bajo:
                campos[n] = str(anio)
        intentos.append(("formulario", formulario["metodo"],
                         formulario["accion"], campos))

    # 2) Con el campo recaptcha VACIO, tal como lo declara el formulario.
    params_vacio = {"recaptcha": "", "mes": f"{mes:02d}", "anio": str(anio)}
    intentos.append(("recaptcha vacio", "get", BASE + "/reuniones", params_vacio))

    # 3) Por direccion, con el codigo si aparecio.
    params = {"mes": f"{mes:02d}", "anio": str(anio)}
    if codigo:
        intentos.append(("direccion con codigo", "get", BASE + "/reuniones",
                         {**params, "recaptcha": codigo}))
    # 4) Por direccion pelada, ya con las cookies de la sesion.
    intentos.append(("direccion con sesion", "get", BASE + "/reuniones", params))
    # 5) Por direccion, como POST.
    intentos.append(("direccion como POST", "post", BASE + "/reuniones", params))
    # 6) Con el orden de campos tal cual el formulario los declara.
    intentos.append(("orden del formulario", "get", BASE + "/reuniones",
                     {"recaptcha": "", "mes": str(mes), "anio": str(anio)}))

    detalle = []
    for etiqueta, metodo, accion, datos in intentos:
        try:
            if metodo == "post":
                r = s.post(accion, data=datos, timeout=(5, 15))
            else:
                r = s.get(accion, params=datos, timeout=(5, 15))
            reuniones = calendar_from_meetings(BeautifulSoup(r.text, "html.parser"))
            del_mes = [x for x in reuniones if x["fecha"].startswith(prefijo)]
            meses = sorted({x["fecha"][:7] for x in reuniones})
            detalle.append({
                "via": etiqueta, "metodo": metodo.upper(),
                "status": r.status_code, "url": r.url[:120],
                "total": len(reuniones), "DEL_MES": len(del_mes),
                "meses_que_trajo": meses,
            })
            if del_mes:
                return del_mes, etiqueta, detalle
        except Exception as e:
            detalle.append({"via": etiqueta, "error": str(e)[:120]})

    return [], "", detalle


def calendario_del_mes(anio, mes):
    """
    Devuelve las reuniones de un mes concreto (anio=2024, mes=3).
    Guarda en cache: un mes que ya paso no cambia mas.
    """
    clave = f"calendario_mes:{anio}-{mes:02d}"
    hoy = datetime.now()
    es_pasado = (anio, mes) < (hoy.year, hoy.month)
    ttl = 90 * 24 * 60 * 60 if es_pasado else TTL_CALENDARIO

    cacheado, fresco = cache_get(clave, ttl)
    if cacheado is not None and fresco:
        return cacheado

    try:
        reuniones, via, _ = _traer_mes(anio, mes)
        if reuniones:
            cache_set(clave, reuniones)
        return reuniones
    except Exception:
        return cacheado or []


def calendario_entre(desde, hasta):
    """Junta las reuniones de todos los meses entre dos fechas (AAAA-MM-DD)."""
    try:
        d = datetime.strptime(desde, "%Y-%m-%d")
        h = datetime.strptime(hasta, "%Y-%m-%d")
    except ValueError:
        return []

    todas = []
    anio, mes = d.year, d.month
    while (anio, mes) <= (h.year, h.month):
        todas.extend(calendario_del_mes(anio, mes))
        mes += 1
        if mes > 12:
            mes = 1
            anio += 1
    return todas


def saved_calendar():
    con = db()
    rows = con.execute(
        """
        SELECT DISTINCT fecha, hipodromo
        FROM carreras
        ORDER BY fecha DESC, hipodromo
        """
    ).fetchall()
    con.close()
    return [
        {
            "fecha": row["fecha"],
            "hipodromo": row["hipodromo"],
            "url": "",
        }
        for row in rows
    ]


@app.get("/api/calendario")
def calendario():
    forzar = request.args.get("refresh") == "1"
    try:
        meetings, origen = con_cache(
            "calendario", TTL_CALENDARIO, forzar,
            lambda: calendar_from_meetings(fetch(BASE + "/reuniones"))
        )
        if meetings:
            resp = {"ok": True, "reuniones": meetings, "fuente": "Stud Book"}
            if origen == "cache_vencido":
                resp["aviso"] = "La fuente oficial no respondió. Se muestra el último calendario guardado."
            return jsonify(**resp)
    except Exception:
        pass

    saved = saved_calendar()
    if saved:
        return jsonify(
            ok=True,
            reuniones=saved,
            fuente="Carreras guardadas",
            aviso=(
                "La fuente oficial no respondió. "
                "Se muestran fechas guardadas."
            ),
        )

    return jsonify(
        ok=False,
        error=(
            "El calendario oficial no está disponible "
            "en este momento."
        ),
        reuniones=[],
    ), 503


@app.get("/")
def home():
    return render_template("index.html")

@app.get("/api/reuniones")
def reuniones():
    fecha = request.args.get("fecha", "").strip()
    hipodromo = request.args.get("hipodromo", "").strip()

    if not fecha or not hipodromo:
        return jsonify(
            ok=False,
            error="Elegí hipódromo y fecha.",
        ), 400

    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        return jsonify(ok=False, error="Fecha inválida."), 400

    forzar = request.args.get("refresh") == "1"
    clave = f"reuniones:{fecha}:{normalize_text(hipodromo)}"

    def traer():
        calendar = calendar_from_meetings(fetch(BASE + "/reuniones"))
        selected = [
            meeting
            for meeting in calendar
            if meeting["fecha"] == fecha
            and normalize_text(meeting["hipodromo"])
            == normalize_text(hipodromo)
        ]
        output = []
        for meeting in selected:
            detail = fetch(meeting["url"])
            races = extract_races_from_meeting(detail)
            if races:
                output.append({
                    "hipodromo": meeting["hipodromo"],
                    "url": meeting["url"],
                    "carreras": races,
                })
        if not output:
            raise ValueError("sin carreras")
        return output

    try:
        output, origen = con_cache(clave, TTL_REUNION, forzar, traer)
        resp = {"ok": True, "reuniones": output}
        if origen == "cache_vencido":
            resp["aviso"] = "La fuente oficial no respondió. Se muestra la última versión guardada."
        return jsonify(**resp)
    except Exception:
        return jsonify(
            ok=False,
            error=(
                "No se encontraron carreras confirmadas para esa reunión, "
                "o la fuente oficial no respondió."
            ),
            reuniones=[],
        ), 404


def _es_la_proxima(url, numero):
    """
    Dice si esa carrera es la proxima a correrse en su reunion.
    Es la unica que ve el visitante que todavia no tiene cuenta.
    """
    fecha_url = meeting_date_from_url(url)
    hoy = datetime.now().strftime("%Y-%m-%d")
    # Solo las de hoy pueden ser "la proxima".
    if fecha_url != hoy:
        return False
    try:
        carreras = extract_races_from_meeting(fetch(url))
    except Exception:
        return False
    ahora = datetime.now().strftime("%H:%M")
    pendientes = [c for c in carreras if c.get("hora") and c["hora"] >= ahora]
    if pendientes:
        return int(numero) == pendientes[0]["numero"]
    # Si ya corrieron todas, la ultima queda como la de referencia.
    return bool(carreras) and int(numero) == carreras[-1]["numero"]


@app.get("/api/carrera")
def carrera():
    url = request.args.get("url","")
    numero = request.args.get("numero","")
    forzar = request.args.get("refresh") == "1"
    if not url.startswith(BASE) or not numero.isdigit():
        return jsonify(ok=False,error="Datos inválidos."),400

    # Sin cuenta solo se ve la proxima carrera a correrse.
    if not usuario_actual() and not es_admin():
        if not _es_la_proxima(url, numero):
            return jsonify(
                ok=False,
                necesita_cuenta=True,
                error=("Sin cuenta solo podés ver la carrera que está por correrse. "
                       "Creá una cuenta gratis para ver todas."),
            ), 401

    clave = f"carrera:{url}:{numero}"

    def traer():
        data = parse_race(fetch(url), int(numero))
        if not data:
            raise ValueError("carrera no encontrada")
        return data

    try:
        data, origen = con_cache(clave, TTL_CARRERA, forzar, traer)
        resp = {"ok": True, **data}
        if origen == "cache_vencido":
            resp["aviso"] = "La fuente oficial no respondió. Se muestra la última versión guardada."
        return jsonify(**resp)
    except Exception as e:
        return jsonify(ok=False,error="No se pudo cargar la carrera.",detalle=str(e)),502

TTL_BUSQUEDA = 6 * 60 * 60   # 6 horas

# Buscador real del Stud Book, verificado en el sitio:
# /ejemplares/autocomplete?tipo=1&muerto=1&term=NOMBRE
# Devuelve JSON con: id, text, leyenda, padre, madre, sexo, nacimiento,
# pelo, url_friendly.
RUTA_AUTOCOMPLETE = "/ejemplares/autocomplete?tipo=1&muerto=1&term={q}"


def _consultar_autocomplete(termino, tipo="1", muerto="1"):
    """
    Consulta cruda al autocompletado del Stud Book.
    Comprobado en el sitio: solo responde con UNA palabra (sin espacios) y
    devuelve como maximo 15 resultados, en orden alfabetico.
    El parametro 'tipo' filtra por categoria de ejemplar: con tipo=1 no
    aparecen todos, por eso se prueban varias variantes.
    """
    cabeceras = dict(HEADERS)
    cabeceras["Accept"] = "application/json, text/javascript, */*; q=0.01"
    cabeceras["X-Requested-With"] = "XMLHttpRequest"
    url = (f"{BASE}/ejemplares/autocomplete"
           f"?tipo={tipo}&muerto={muerto}&term={quote(termino)}")
    try:
        r = requests.get(url, headers=cabeceras, timeout=(4, 10))
        r.raise_for_status()
        datos = r.json()
    except Exception:
        return []
    if isinstance(datos, dict):
        datos = datos.get("results") or datos.get("data") or []
    return datos if isinstance(datos, list) else []


# Variantes de categoria a probar. La primera es la que usa el sitio; las
# demas existen porque se comprobo que con tipo=1 faltan ejemplares
# (por ejemplo CANDY GIRL, que tiene ficha propia pero no aparecia).
VARIANTES_TIPO = ["1", "2", "0", "", "3"]


def _armar_resultado(item):
    nombre = clean(item.get("text", ""))
    idd = item.get("id")
    if not nombre or idd is None or idd == "":
        return None
    slug = item.get("url_friendly") or normalize_text(nombre).replace(" ", "-")
    partes = []
    if item.get("leyenda"):
        partes.append(clean(str(item["leyenda"])))
    padres = " y ".join(
        clean(str(item[k])) for k in ("padre", "madre") if item.get(k)
    )
    if padres:
        partes.append("por " + padres)
    return {
        "nombre": nombre,
        "perfil": f"{BASE}/ejemplares/perfil/{idd}/{slug}",
        "detalle": " ".join(partes),
        "sexo": clean(str(item.get("sexo", ""))),
        "nacimiento": clean(str(item.get("nacimiento", ""))),
        "pelo": clean(str(item.get("pelo", ""))),
    }


def buscar_ejemplares(termino):
    """
    Busca caballos por nombre. Sortea las dos limitaciones del buscador del
    Stud Book, comprobadas en el sitio:
      1) con espacios devuelve vacio -> se consulta solo la primera palabra
      2) devuelve como maximo 15, en orden alfabetico -> si el buscado no
         entra en esa tanda, se agregan letras hasta alcanzarlo
    """
    termino = clean(termino)
    if len(termino) < 3:
        return []

    clave = f"busqueda4:{normalize_text(termino)}"
    cacheado, fresco = cache_get(clave, TTL_BUSQUEDA)
    if cacheado is not None and fresco:
        return cacheado

    objetivo = normalize_text(termino)
    objetivo_pegado = objetivo.replace(" ", "")
    palabras = termino.split()

    vistos, encontrados = set(), []
    consultas = 0
    MAX_CONSULTAS = 10

    def agregar(lista):
        for item in lista:
            if not isinstance(item, dict):
                continue
            r = _armar_resultado(item)
            if not r or r["perfil"] in vistos:
                continue
            vistos.add(r["perfil"])
            encontrados.append(r)

    def ya_esta():
        return any(
            normalize_text(e["nombre"]).replace(" ", "") == objetivo_pegado
            for e in encontrados
        )

    def consultar(q, tipo="1", muerto="1"):
        nonlocal consultas
        if consultas >= MAX_CONSULTAS or not q:
            return
        consultas += 1
        agregar(_consultar_autocomplete(q, tipo, muerto))

    # 1) El termino TAL COMO SE ESCRIBIO, con espacios y todo.
    #    Comprobado con el diagnostico: el sitio si acepta espacios.
    consultar(termino)

    # 2) Si no aparecio, probar las otras categorias de ejemplar.
    if not ya_esta():
        for tipo in VARIANTES_TIPO[1:]:
            if ya_esta():
                break
            consultar(termino, tipo)

    # 3) Todavia no: probar sin el filtro de fallecidos.
    if not ya_esta():
        consultar(termino, "1", "0")

    # 4) Ultimo recurso: el nombre pegado, por si el sitio lo indexa asi.
    if not ya_esta() and len(palabras) > 1:
        consultar(termino.replace(" ", ""))

    # 5) Si aun asi no hay NADA, mostrar al menos los parecidos de la
    #    primera palabra, para que el usuario elija.
    if not encontrados:
        consultar(palabras[0])

    # 6) Quedarse con los que contengan TODAS las palabras buscadas.
    piezas = [normalize_text(p) for p in palabras if p]
    filtrados = [
        e for e in encontrados
        if all(p in normalize_text(e["nombre"]) for p in piezas)
    ]
    # El nombre exacto va primero, despues los que empiezan igual.
    def orden(e):
        n = normalize_text(e["nombre"]).replace(" ", "")
        if n == objetivo_pegado:
            return (0, e["nombre"])
        if n.startswith(objetivo_pegado):
            return (1, e["nombre"])
        return (2, e["nombre"])

    filtrados.sort(key=orden)
    resultado = filtrados[:25] if filtrados else sorted(encontrados, key=orden)[:25]
    cache_set(clave, resultado)
    return resultado


@app.get("/api/buscar-caballo")
def api_buscar_caballo():
    if not usuario_actual() and not es_admin():
        return jsonify(
            ok=False, necesita_cuenta=True,
            error="Creá una cuenta gratis para buscar cualquier caballo.",
            resultados=[],
        ), 401
    termino = request.args.get("q", "").strip()
    if len(termino) < 3:
        return jsonify(ok=False, error="Escribí al menos 3 letras."), 400
    try:
        resultados = buscar_ejemplares(termino)
    except Exception:
        return jsonify(ok=False, error="No se pudo buscar en este momento."), 502
    if not resultados:
        return jsonify(
            ok=False,
            error=f"No se encontró ningún caballo con «{termino}».",
            resultados=[],
        ), 404
    return jsonify(ok=True, resultados=resultados)


@app.get("/api/caballo")
def api_caballo():
    """Ficha completa de un caballo: datos, próximas carreras y campaña."""
    perfil = request.args.get("perfil", "")
    if not (perfil.startswith(BASE) or perfil.startswith("https://studbook.org.ar")):
        return jsonify(ok=False, error="Dirección inválida."), 400

    clave = f"caballo:{perfil}"
    cacheado, fresco = cache_get(clave, TTL_CARRERA)
    if cacheado is not None and fresco:
        return jsonify(ok=True, **cacheado)

    try:
        soup = fetch(perfil)
        texto = clean(soup.get_text(" "))

        nombre = ""
        for etiqueta in ["h1", "h2"]:
            h = soup.find(etiqueta)
            if h and clean(h.get_text(" ")):
                nombre = clean(h.get_text(" "))
                break

        caballo = {"nombre": nombre, "perfil": perfil}
        caballo.update(_resumen_del_perfil(soup, texto))

        carreras = _tabla_carreras_del_perfil(soup)
        caballo["carreras"] = carreras[:30]
        puestos = [c["puesto"] for c in carreras if c["puesto"]]
        caballo["corridas"] = len(carreras)
        caballo["victorias"] = sum(1 for p in puestos if p == 1)
        caballo["podios"] = sum(1 for p in puestos if p <= 3)

        # Proximas carreras: buscarlas solo dentro de esa seccion del documento,
        # no en toda la pagina (sino se cuelan las carreras ya corridas).
        proximas = []
        titulo_prox = None
        for etiqueta in soup.find_all(["h1", "h2", "h3", "h4", "div", "span", "p"]):
            if re.fullmatch(r"PR[ÓO]XIMAS CARRERAS", clean(etiqueta.get_text(" ")), re.I):
                titulo_prox = etiqueta
                break

        if titulo_prox:
            for nodo in titulo_prox.find_all_next():
                texto_nodo = clean(nodo.get_text(" ")) if hasattr(nodo, "get_text") else ""
                # Cortar al llegar a la seccion siguiente.
                if re.fullmatch(r"(CAMPA[ÑN]A|EXPORTACI[ÓO]N.*|SERVICIOS|PEDIGREE)",
                                texto_nodo, re.I):
                    break
                if getattr(nodo, "name", None) == "a" and nodo.get("href"):
                    t = clean(nodo.get_text(" "))
                    if re.search(r"\d{2}/\d{2}/\d{4}", t):
                        proximas.append({
                            "texto": t,
                            "enlace": urljoin(BASE, nodo["href"]),
                        })

        caballo["proximas"] = proximas[:5]
        caballo["sin_proximas"] = len(proximas) == 0

        cache_set(clave, caballo)
        return jsonify(ok=True, **caballo)
    except Exception as e:
        return jsonify(ok=False, error="No se pudo cargar el caballo.", detalle=str(e)), 502


@app.get("/api/detalle-carrera")
def api_detalle_carrera():
    """Devuelve pista, estado y condicion en palabras de una carrera puntual."""
    url = request.args.get("url", "")
    if not url.startswith(BASE) and not url.startswith("https://studbook.org.ar"):
        return jsonify(ok=False, error="Dirección inválida."), 400
    detalle = detalle_de_carrera(url)
    if not detalle:
        return jsonify(ok=False, error="No se pudo leer el detalle."), 502
    return jsonify(ok=True, **detalle)


@app.post("/api/enriquecer")
def enriquecer():
    """
    Trae la campaña de todos los participantes.
    Se piden TODOS AL MISMO TIEMPO: antes se hacía uno por uno y con 14
    caballos eso tardaba más de diez segundos.
    """
    data = request.get_json(silent=True) or {}
    horses = data.get("participantes", [])
    if not horses:
        return jsonify(ok=True, participantes=[])

    # Tope de pedidos simultáneos, para no castigar al Stud Book.
    simultaneos = min(int(os.getenv("PEDIDOS_A_LA_VEZ", "8")), max(1, len(horses)))

    with ThreadPoolExecutor(max_workers=simultaneos) as pool:
        resultados = list(pool.map(lambda h: enrich_horse(dict(h)), horses))

    return jsonify(ok=True, participantes=resultados)

@app.post("/api/analizar")
def analizar():
    data = request.get_json(silent=True) or {}
    horses = [h for h in data.get("participantes",[]) if not h.get("retirado")]
    if len(horses) < 2:
        return jsonify(ok=False,error="Se necesitan al menos dos participantes confirmados."),400

    pesos = cargar_pesos()
    fecha = data.get("fecha", "")
    hipodromo = data.get("hipodromo", "")

    # --- CONDICIONES OFICIALES: las que cargo el admin para esa reunion ---
    oficiales = condiciones_de(fecha, hipodromo)
    contexto_oficial = {
        "participantes": horses,
        "pista_dia": data.get("pista_dia", ""),
    }
    for campo in OPCIONES_CONDICIONES:
        contexto_oficial[campo["clave"]] = oficiales.get(campo["clave"], "")

    ranked_oficial, top_oficial = rankear(horses, contexto_oficial, pesos)

    # SOLO el pronostico oficial se guarda y se compara con el resultado.
    ya_corrida = any(h.get("puesto") for h in horses)
    try:
        registrar_pronostico(
            url=data.get("url",""), numero=data.get("numero"),
            fecha=fecha, hipodromo=hipodromo,
            top=top_oficial, participantes=horses,
            pesos=pesos, ya_corrida=ya_corrida,
        )
    except Exception:
        pass  # que un fallo al guardar nunca rompa el pronostico al usuario

    # --- CONDICIONES DEL USUARIO: si cambio alguna, se recalcula para el ---
    del_usuario = data.get("condiciones_usuario") or {}
    cambiadas = {
        c["clave"]: clean(del_usuario.get(c["clave"], ""))
        for c in OPCIONES_CONDICIONES
        if clean(del_usuario.get(c["clave"], ""))
        and clean(del_usuario.get(c["clave"], "")) != contexto_oficial.get(c["clave"], "")
    }

    if cambiadas:
        contexto_usuario = dict(contexto_oficial)
        contexto_usuario.update(cambiadas)
        _, top_usuario = rankear(horses, contexto_usuario, pesos)
        return jsonify(
            ok=True,
            ranking=top_usuario,
            ranking_oficial=top_oficial,
            confianza=round(top_usuario[0]["score"], 1),
            ya_corrida=ya_corrida,
            condiciones_oficiales={c["clave"]: contexto_oficial.get(c["clave"], "")
                                   for c in OPCIONES_CONDICIONES},
            condiciones_usadas=cambiadas,
            es_personal=True,
            aviso=("Este pronóstico usa tus condiciones. No cambia el oficial "
                   "ni las estadísticas de la app."),
        )

    return jsonify(ok=True, ranking=top_oficial,
                   confianza=round(top_oficial[0]["score"], 1),
                   ya_corrida=ya_corrida,
                   condiciones_oficiales={c["clave"]: contexto_oficial.get(c["clave"], "")
                                          for c in OPCIONES_CONDICIONES},
                   es_personal=False)


def registrar_pronostico(url, numero, fecha, hipodromo, top, participantes,
                         pesos, ya_corrida):
    """
    Guarda lo que predijo la app. Si la carrera ya tiene puestos reales,
    calcula el acierto y ajusta el algoritmo automaticamente.
    IMPORTANTE: si ya habia un pronostico guardado, NO se pisa. El valor
    del pronostico esta en haberse hecho antes de conocer el resultado.
    """
    if not url or numero is None:
        return

    predichos = [x["nombre"] for x in top]

    # Si ya hay un pronostico guardado para esta carrera, se conserva ese.
    con = db()
    previo = con.execute(
        "SELECT ranking FROM pronosticos WHERE url=? AND numero=?",
        (url, int(numero))
    ).fetchone()
    con.close()
    if previo:
        try:
            predichos = json.loads(previo["ranking"]) or predichos
        except (json.JSONDecodeError, TypeError):
            pass

    resultado = None
    acierto_ganador = None
    aciertos_top4 = None

    if ya_corrida:
        llegados = sorted(
            [h for h in participantes if h.get("puesto")],
            key=lambda h: h["puesto"]
        )
        resultado = [h["nombre"] for h in llegados[:4]]
        if resultado:
            acierto_ganador = 1 if predichos[0] == resultado[0] else 0
            aciertos_top4 = len(set(predichos) & set(resultado))

    con = db()
    con.execute("""
        INSERT INTO pronosticos(url,numero,fecha,hipodromo,ranking,resultado,
                                acierto_ganador,aciertos_top4,pesos_usados,
                                creado_en,comparado_en)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(url,numero) DO UPDATE SET
          resultado=COALESCE(excluded.resultado, pronosticos.resultado),
          acierto_ganador=COALESCE(excluded.acierto_ganador, pronosticos.acierto_ganador),
          aciertos_top4=COALESCE(excluded.aciertos_top4, pronosticos.aciertos_top4),
          comparado_en=COALESCE(excluded.comparado_en, pronosticos.comparado_en)
    """, (
        url, int(numero), fecha, hipodromo,
        json.dumps(predichos, ensure_ascii=False),
        json.dumps(resultado, ensure_ascii=False) if resultado else None,
        acierto_ganador, aciertos_top4,
        json.dumps(pesos, ensure_ascii=False),
        datetime.now().isoformat(timespec="seconds"),
        datetime.now().isoformat(timespec="seconds") if resultado else None,
    ))
    con.commit()
    con.close()

    if resultado:
        ajustar_algoritmo()


def ordenar_para_pronosticar(participantes):
    """
    Ordena los caballos por su NUMERO antes de puntuarlos.
    Es imprescindible: la tabla de una carrera ya corrida viene ordenada
    por orden de llegada. Si se puntuara en ese orden y varios caballos
    empataran, el desempate copiaria el resultado y el acierto seria falso.
    """
    return sorted(
        participantes,
        key=lambda p: (p.get("numero") is None, p.get("numero") or 0,
                       p.get("nombre") or "")
    )


def rankear(participantes, contexto, pesos):
    """Puntua y ordena a los participantes. Devuelve los cuatro primeros."""
    base = ordenar_para_pronosticar(participantes)
    ranked = []
    for p in base:
        score, motivos = score_horse(p, contexto, pesos)
        ranked.append({**p, "score": score, "motivos": motivos})
    # Desempate por nombre, para que nunca dependa del orden de llegada.
    ranked.sort(key=lambda x: (-x["score"], x.get("nombre") or ""))
    top = ranked[:4]
    total = sum(x["score"] for x in top) or 1
    for x in top:
        x["probabilidad_relativa"] = round(x["score"] / total * 100, 1)
    return ranked, top


def ajustar_algoritmo():
    """
    Compara los pronosticos ya resueltos y afina los pesos.
    Cada peso tiene un piso y un techo para que nunca se anule: si todos
    los pesos llegaran a cero, todos los caballos puntuarian igual y el
    pronostico dejaria de significar algo.
    """
    con = db()
    filas = con.execute("""
        SELECT acierto_ganador, aciertos_top4, pesos_usados
        FROM pronosticos WHERE resultado IS NOT NULL
        ORDER BY id DESC LIMIT 200
    """).fetchall()
    con.close()

    if len(filas) < 20:
        return  # con menos de 20 carreras no hay con que aprender

    recientes = filas[:30]
    historico = filas

    tasa_reciente = sum(f["aciertos_top4"] or 0 for f in recientes) / (len(recientes) * 4)
    tasa_historica = sum(f["aciertos_top4"] or 0 for f in historico) / (len(historico) * 4)

    pesos = cargar_pesos()
    direccion = 1.0 if tasa_reciente >= tasa_historica else -1.0
    paso = 0.02 * direccion

    for clave in pesos:
        inicial = PESOS_INICIALES[clave]
        nuevo = pesos[clave] + (abs(inicial) * paso)   # paso fijo, no proporcional
        # Cada peso se mueve como mucho entre la mitad y el doble del inicial.
        piso = abs(inicial) * 0.5
        techo = abs(inicial) * 2.0
        if inicial >= 0:
            pesos[clave] = max(piso, min(techo, nuevo))
        else:
            pesos[clave] = min(-piso, max(-techo, nuevo))

    guardar_pesos(pesos)

# ============================================================
# PANEL DE ADMIN — solo accesible con la clave ADMIN_KEY.
# El usuario comun no ve nada de esto.
# ============================================================

ADMIN_KEY = os.getenv("ADMIN_KEY", "")

def es_admin():
    if not ADMIN_KEY:
        return False
    enviada = request.args.get("clave", "") or request.headers.get("X-Admin-Key", "")
    return enviada == ADMIN_KEY

@app.get("/admin")
def admin_panel():
    if not es_admin():
        return "Acceso restringido.", 403
    return render_template("admin.html")

@app.get("/api/admin/rendimiento")
def admin_rendimiento():
    if not es_admin():
        return jsonify(ok=False, error="Acceso restringido."), 403

    con = db()
    total = con.execute("SELECT COUNT(*) c FROM pronosticos").fetchone()["c"]
    resueltos = con.execute(
        "SELECT COUNT(*) c FROM pronosticos WHERE resultado IS NOT NULL"
    ).fetchone()["c"]
    stats = con.execute("""
        SELECT
          SUM(acierto_ganador) ganadores,
          SUM(aciertos_top4) aciertos,
          COUNT(*) n
        FROM pronosticos WHERE resultado IS NOT NULL
    """).fetchone()
    ultimos = con.execute("""
        SELECT fecha, hipodromo, numero, ranking, resultado,
               acierto_ganador, aciertos_top4, comparado_en
        FROM pronosticos WHERE resultado IS NOT NULL
        ORDER BY id DESC LIMIT 40
    """).fetchall()
    pesos_actuales = con.execute(
        "SELECT clave, valor, actualizado_en FROM algoritmo ORDER BY clave"
    ).fetchall()
    con.close()

    n = stats["n"] or 0
    return jsonify(
        ok=True,
        total_pronosticos=total,
        carreras_comparadas=resueltos,
        acierto_ganador_pct=round((stats["ganadores"] or 0) / n * 100, 1) if n else None,
        acierto_top4_pct=round((stats["aciertos"] or 0) / (n * 4) * 100, 1) if n else None,
        pesos=[dict(p) for p in pesos_actuales] or [
            {"clave": k, "valor": v, "actualizado_en": "inicial"}
            for k, v in PESOS_INICIALES.items()
        ],
        ultimas=[{
            "fecha": u["fecha"],
            "hipodromo": u["hipodromo"],
            "numero": u["numero"],
            "predicho": json.loads(u["ranking"]),
            "real": json.loads(u["resultado"]) if u["resultado"] else [],
            "acerto_ganador": bool(u["acierto_ganador"]),
            "aciertos_top4": u["aciertos_top4"],
        } for u in ultimos],
    )


@app.get("/api/admin/diagnostico")
def admin_diagnostico():
    """
    Herramienta de control: muestra exactamente que responde el Stud Book
    ante una busqueda. Sirve para cualquier caso futuro en que un caballo
    no aparezca, sin tener que adivinar el motivo.
    """
    if not es_admin():
        return jsonify(ok=False, error="Acceso restringido."), 403

    termino = request.args.get("q", "").strip()
    if not termino:
        return jsonify(ok=False, error="Falta el nombre a probar."), 400

    cabeceras = dict(HEADERS)
    cabeceras["Accept"] = "application/json, text/javascript, */*; q=0.01"
    cabeceras["X-Requested-With"] = "XMLHttpRequest"

    informe = {"termino": termino, "intentos": []}

    # Se prueban distintas variantes para ver cual devuelve resultados.
    variantes = [
        ("como lo manda el sitio (%20)", quote(termino)),
        ("con signo mas (+)", quote_plus(termino)),
        ("sin espacios", termino.replace(" ", "")),
        ("solo la primera palabra", quote(termino.split()[0])),
    ]
    # Y distintos valores de 'tipo', por si filtra por categoria de ejemplar.
    for etiqueta, q in variantes:
        for tipo in ("1", "2", "0", "", "3"):
            for muerto in ("1", "0"):
                url = (f"{BASE}/ejemplares/autocomplete"
                       f"?tipo={tipo}&muerto={muerto}&term={q}")
                intento = {"variante": etiqueta, "tipo": tipo,
                           "muerto": muerto, "url": url}
                try:
                    r = requests.get(url, headers=cabeceras, timeout=(4, 10))
                    intento["status"] = r.status_code
                    try:
                        datos = r.json()
                        lista = datos if isinstance(datos, list) else (
                            datos.get("results") or datos.get("data") or []
                        )
                        intento["cantidad"] = len(lista)
                        intento["nombres"] = [
                            clean(str(x.get("text", "")))
                            for x in lista[:15] if isinstance(x, dict)
                        ]
                        # Marcar si el buscado aparece en esta variante.
                        buscado = normalize_text(termino).replace(" ", "")
                        intento["ENCONTRADO"] = any(
                            normalize_text(n).replace(" ", "") == buscado
                            for n in intento["nombres"]
                        )
                    except Exception:
                        intento["cantidad"] = 0
                        intento["respuesta_cruda"] = r.text[:300]
                except Exception as e:
                    intento["error"] = str(e)
                informe["intentos"].append(intento)

    # Resumen: cuales encontraron exactamente el caballo buscado.
    aciertos = [i for i in informe["intentos"] if i.get("ENCONTRADO")]
    exitosos = [i for i in informe["intentos"] if i.get("cantidad")]
    informe["resumen"] = {
        "LO_ENCONTRARON": [
            {"variante": i["variante"], "tipo": i["tipo"], "muerto": i["muerto"]}
            for i in aciertos
        ],
        "variantes_con_algun_resultado": len(exitosos),
        "variantes_probadas": len(informe["intentos"]),
    }
    informe["lo_que_usa_la_app"] = [
        e["nombre"] for e in buscar_ejemplares(termino)
    ]

    return jsonify(ok=True, **informe)


@app.get("/api/admin/diag-calendario")
def admin_diag_calendario():
    """
    Comprueba si se pueden traer meses anteriores. Ahora mira el formulario
    real de la pagina y prueba cuatro vias distintas, con sesion abierta.
    """
    if not es_admin():
        return jsonify(ok=False, error="Acceso restringido."), 403

    anio = int(request.args.get("anio", "2026"))
    mes = int(request.args.get("mes", "7"))
    prefijo = f"{anio}-{mes:02d}-"
    informe = {"pedido": f"{anio}-{mes:02d}"}

    try:
        s = _sesion_studbook()
        r0 = s.get(BASE + "/reuniones", timeout=(5, 15))
        soup0 = BeautifulSoup(r0.text, "html.parser")
        informe["cookies"] = list(s.cookies.keys())
    except Exception as e:
        return jsonify(ok=False, error=f"No se pudo abrir la pagina: {e}"), 502

    # 1) Como es el formulario, de verdad
    formulario = _datos_del_formulario(soup0)
    informe["FORMULARIO"] = formulario or "no se encontro un formulario con mes/año"

    # Todos los formularios, por si el filtro fue muy estricto
    todos = []
    for f in soup0.find_all("form"):
        todos.append({
            "accion": f.get("action", ""),
            "metodo": f.get("method", "get"),
            "campos": [i.get("name") for i in f.find_all(["input","select"]) if i.get("name")],
        })
    informe["todos_los_formularios"] = todos[:6]

    # Todos los <select> de la pagina, con sus opciones
    selects = []
    for sel in soup0.find_all("select"):
        opciones = [o.get("value","") for o in sel.find_all("option")][:14]
        selects.append({"nombre": sel.get("name",""), "id": sel.get("id",""),
                        "opciones": opciones})
    informe["selectores"] = selects[:6]

    informe["codigo_recaptcha"] = bool(_codigo_recaptcha(soup0))

    # 2) Probar las vias
    try:
        reuniones, via, detalle = _traer_mes(anio, mes)
    except Exception as e:
        reuniones, via, detalle = [], f"error: {e}", []

    informe["RESUMEN"] = {
        "se_puede_traer_meses_viejos": bool(reuniones),
        "VIA_QUE_FUNCIONA": via,
        "reuniones_del_mes": len(reuniones),
        "ejemplos": [f"{r['fecha']} {r['hipodromo']}" for r in reuniones[:5]],
    }
    informe["DETALLE_DE_CADA_INTENTO"] = detalle
    return jsonify(ok=True, **informe)


SIGLAS = {
    "palermo": "PAL", "san isidro": "SI", "la plata": "LP",
    "la punta": "LPU", "rosario": "ROS", "tandil": "TAN",
    "dolores": "DOL", "azul": "AZL", "tucuman": "TUC",
    "cordoba": "CBA", "mendoza": "MZA", "santa fe": "SFE",
}

COLORES_HIP = {
    "PAL": "#8c2a2a", "SI": "#153832", "LP": "#2b4a8c",
    "LPU": "#5c2b8c", "ROS": "#2b8c6b", "TAN": "#8c6b2a",
    "DOL": "#2a6b8c", "AZL": "#6b2a8c", "TUC": "#8c4a2a",
}

def sigla_de(hipodromo):
    n = normalize_text(hipodromo)
    for nombre, sigla in SIGLAS.items():
        if nombre in n:
            return sigla
    # Si no esta en la lista, armar una sigla con las iniciales.
    palabras = [p for p in n.split() if len(p) > 2]
    return "".join(p[0] for p in palabras[:3]).upper() or "OTR"


@app.get("/api/calendario-meses")
def calendario_meses():
    """
    Calendario agrupado por mes, con los hipodromos de cada fecha.
    Parametros: desde y hasta (AAAA-MM-DD). Por defecto, desde 2024.
    """
    hoy = datetime.now().strftime("%Y-%m-%d")
    desde = request.args.get("desde", "2024-01-01")
    hasta = request.args.get("hasta", hoy)

    reuniones = calendario_entre(desde, hasta)
    if not reuniones:
        # Respaldo: lo que haya guardado en la base.
        guardadas = saved_calendar()
        reuniones = [r for r in guardadas if desde <= r["fecha"] <= hasta]
        if not reuniones:
            return jsonify(
                ok=False,
                error="No se pudo traer el calendario en este momento.",
                fechas=[],
            ), 503

    # Agrupar por fecha.
    por_fecha = {}
    for r in reuniones:
        f = r["fecha"]
        if f not in por_fecha:
            por_fecha[f] = {"fecha": f, "hipodromos": []}
        sigla = sigla_de(r["hipodromo"])
        if not any(h["sigla"] == sigla for h in por_fecha[f]["hipodromos"]):
            por_fecha[f]["hipodromos"].append({
                "nombre": r["hipodromo"],
                "sigla": sigla,
                "color": COLORES_HIP.get(sigla, "#5a6b66"),
                "url": r.get("url", ""),
            })

    fechas = sorted(por_fecha.values(), key=lambda x: x["fecha"], reverse=True)

    # Lista de hipodromos para el filtro.
    hips = {}
    for f in fechas:
        for h in f["hipodromos"]:
            hips[h["sigla"]] = {"nombre": h["nombre"], "sigla": h["sigla"],
                                "color": h["color"]}

    return jsonify(
        ok=True,
        fechas=fechas,
        hipodromos=sorted(hips.values(), key=lambda h: h["nombre"]),
        desde=desde,
        hasta=hasta,
    )


@app.get("/api/admin/diag-tabulada")
def admin_diag_tabulada():
    """
    Comprueba si de la pagina de una carrera se pueden sacar TODOS los datos
    que hacen falta para la tabulada: competidores, puestos y cuerpos.
    Se corre ANTES de programar la pantalla, para no trabajar a ciegas.
    """
    if not es_admin():
        return jsonify(ok=False, error="Acceso restringido."), 403

    url = request.args.get("url", "").strip()
    numero = request.args.get("numero", "1")

    if not url:
        return jsonify(ok=False,
                       error="Falta la direccion de la carrera (parametro url)."), 400

    informe = {"url": url, "numero": numero}

    try:
        soup = fetch(url)
    except Exception as e:
        return jsonify(ok=False, error=f"No se pudo abrir la pagina: {e}"), 502

    # 1) Que encabezados tiene la tabla
    encabezados_vistos = []
    for table in soup.find_all("table"):
        ths = [clean(th.get_text(" ")) for th in table.find_all("th")]
        if ths:
            encabezados_vistos.append(ths)
    informe["encabezados_de_las_tablas"] = encabezados_vistos[:4]

    # 2) Que saca el lector actual
    try:
        data = parse_race(soup, int(numero)) if str(numero).isdigit() else None
    except Exception as e:
        data = None
        informe["error_parse"] = str(e)

    if not data:
        informe["parse_race"] = "No encontro la carrera numero " + str(numero)
        informe["RESUMEN"] = {"SIRVE_PARA_TABULADA": False,
                              "motivo": "no se pudo leer la carrera"}
        return jsonify(ok=True, **informe)

    participantes = data.get("participantes", [])
    informe["cantidad_participantes"] = len(participantes)
    informe["muestra"] = [
        {
            "nombre": p.get("nombre"),
            "numero": p.get("numero"),
            "puesto": p.get("puesto"),
            "peso": p.get("peso"),
            "jockey": p.get("jockey"),
            "retirado": p.get("retirado"),
        }
        for p in participantes[:6]
    ]

    # 3) Los cuerpos: leerlos directo de la tabla para ver si estan
    cuerpos_encontrados = []
    for table in soup.find_all("table"):
        cabeceras = _find_header_row(table)
        if not cabeceras:
            continue
        idx = _map_headers(cabeceras)
        if "cuerpos" not in idx or "nombre" not in idx:
            continue
        for tr in table.find_all("tr"):
            celdas = tr.find_all("td")
            if len(celdas) <= max(idx["cuerpos"], idx["nombre"]):
                continue
            nombre = _cell_text(celdas[idx["nombre"]])
            cpos = _cell_text(celdas[idx["cuerpos"]])
            if nombre:
                cuerpos_encontrados.append({"nombre": nombre, "cpos": cpos})
    informe["cuerpos_leidos"] = cuerpos_encontrados[:8]

    con_puesto = sum(1 for p in participantes if p.get("puesto"))
    con_cuerpos = sum(1 for c in cuerpos_encontrados if c["cpos"])

    informe["RESUMEN"] = {
        "SIRVE_PARA_TABULADA": bool(participantes) and con_puesto > 0,
        "participantes": len(participantes),
        "con_puesto": con_puesto,
        "con_cuerpos": con_cuerpos,
        "falta": (
            [] if (participantes and con_puesto and con_cuerpos)
            else [x for x, ok in [
                ("participantes", bool(participantes)),
                ("puestos", con_puesto > 0),
                ("cuerpos", con_cuerpos > 0),
            ] if not ok]
        ),
    }
    return jsonify(ok=True, **informe)


@app.get("/api/tabulada")
def api_tabulada():
    """
    Tabulada de una carrera: todos los que corrieron, en orden de llegada,
    con los cuerpos al de adelante y al ganador.
    """
    url = request.args.get("url", "").strip()
    numero = request.args.get("numero", "").strip()

    if not (url.startswith(BASE) or url.startswith("https://studbook.org.ar")):
        return jsonify(ok=False, error="Dirección inválida."), 400

    clave = f"tabulada:{url}:{numero}"
    cacheado, fresco = cache_get(clave, TTL_DETALLE_CARRERA)
    if cacheado is not None and fresco:
        return jsonify(ok=True, **cacheado)

    try:
        soup = fetch(url)
    except Exception as e:
        return jsonify(ok=False, error=f"No se pudo abrir la carrera: {e}"), 502

    # La pagina de una carrera suele traer una sola; la de reunion, varias.
    data = None
    if numero.isdigit():
        data = parse_race(soup, int(numero))
    if not data:
        # Probar con el primer numero de carrera que aparezca en la pagina.
        for n in range(1, 21):
            data = parse_race(soup, n)
            if data and data.get("participantes"):
                break
    if not data or not data.get("participantes"):
        return jsonify(ok=False, error="No se encontraron los participantes."), 404

    participantes = [p for p in data["participantes"] if not p.get("retirado")]
    # Ordenar por puesto de llegada; los que no tienen puesto van al final.
    con_puesto = sorted(
        [p for p in participantes if p.get("puesto")],
        key=lambda p: p["puesto"]
    )
    sin_puesto = [p for p in participantes if not p.get("puesto")]

    filas = []
    for p in con_puesto + sin_puesto:
        filas.append({
            "puesto": p.get("puesto"),
            "numero": p.get("numero"),
            "nombre": p.get("nombre"),
            "cuerpos": p.get("cuerpos", ""),
            "acumulado": p.get("acumulado", ""),
            "pago": p.get("pago", ""),
            "peso": p.get("peso", ""),
            "jockey": p.get("jockey", ""),
            "entrenador": p.get("entrenador", ""),
            "caballeriza": p.get("caballeriza", ""),
            "perfil": p.get("perfil", ""),
        })

    detalle = detalle_de_carrera(url)

    resultado = {
        "carrera": data.get("carrera"),
        "premio": data.get("premio", ""),
        "distancia": data.get("distancia", ""),
        "pista": detalle.get("pista_txt") or data.get("superficie", ""),
        "estado": detalle.get("estado_txt") or data.get("estado", ""),
        "categoria": detalle.get("categoria_txt") or data.get("categoria", ""),
        "condicion": detalle.get("condicion_txt") or data.get("condicion", ""),
        "competidores": len(filas),
        "filas": filas,
    }
    cache_set(clave, resultado)
    return jsonify(ok=True, **resultado)


@app.post("/api/admin/reiniciar")
def admin_reiniciar():
    """
    Borra los pronosticos guardados y vuelve el algoritmo a sus valores
    iniciales. Sirve cuando los datos quedaron mal por un error de calculo.
    """
    if not es_admin():
        return jsonify(ok=False, error="Acceso restringido."), 403

    con = db()
    n = con.execute("SELECT COUNT(*) c FROM pronosticos").fetchone()["c"]
    con.execute("DELETE FROM pronosticos")
    con.execute("DELETE FROM algoritmo")
    con.commit()
    con.close()
    guardar_pesos(PESOS_INICIALES)

    return jsonify(ok=True,
                   mensaje=(f"Se borraron {n} pronósticos. "
                            "El algoritmo volvió a sus valores iniciales."),
                   borrados=n)


# ============================================================
# CONDICIONES DE LA REUNION
# Las carga el admin y valen para todas las carreras de ese dia.
# El usuario las puede cambiar para si mismo: eso altera SU pronostico,
# pero nunca el oficial ni las estadisticas de aciertos.
# Para agregar un campo nuevo alcanza con sumarlo a esta lista.
# ============================================================

OPCIONES_CONDICIONES = [
    {"clave": "pista", "titulo": "Pista",
     "opciones": ["Arena", "Arena (Codo)", "Césped"]},
    {"clave": "estado", "titulo": "Estado",
     "opciones": ["Normal", "Liviana", "Húmeda", "Pesada", "Barrosa"]},
    {"clave": "viento", "titulo": "Viento",
     "opciones": ["Sin viento", "A favor", "En contra", "Cruzado"]},
    {"clave": "clima", "titulo": "Clima",
     "opciones": ["Despejado", "Nublado", "Llovizna", "Lluvia"]},
]


def condiciones_de(fecha, hipodromo):
    """Devuelve las condiciones que cargo el admin para esa reunion."""
    try:
        con = db()
        fila = con.execute(
            "SELECT * FROM condiciones WHERE fecha=? AND hipodromo=?",
            (fecha, normalize_text(hipodromo))
        ).fetchone()
        con.close()
    except Exception:
        return {}
    return dict(fila) if fila else {}


@app.get("/api/condiciones")
def api_condiciones():
    """Las condiciones oficiales de una reunion, y las opciones disponibles."""
    fecha = request.args.get("fecha", "").strip()
    hipodromo = request.args.get("hipodromo", "").strip()
    guardadas = condiciones_de(fecha, hipodromo) if fecha and hipodromo else {}
    return jsonify(
        ok=True,
        oficiales={c["clave"]: guardadas.get(c["clave"], "")
                   for c in OPCIONES_CONDICIONES},
        observaciones=guardadas.get("observaciones", ""),
        cargado_en=guardadas.get("cargado_en", ""),
        campos=OPCIONES_CONDICIONES,
    )


@app.post("/api/admin/condiciones")
def admin_condiciones():
    """El admin carga las condiciones de una reunion."""
    if not es_admin():
        return jsonify(ok=False, error="Acceso restringido."), 403

    d = request.get_json(silent=True) or {}
    fecha = clean(d.get("fecha", ""))
    hipodromo = clean(d.get("hipodromo", ""))
    if not fecha or not hipodromo:
        return jsonify(ok=False, error="Faltan la fecha y el hipódromo."), 400

    # Solo se aceptan valores de la lista, para que no entre cualquier cosa.
    valores = {}
    for campo in OPCIONES_CONDICIONES:
        v = clean(d.get(campo["clave"], ""))
        valores[campo["clave"]] = v if v in campo["opciones"] else ""

    con = db()
    con.execute("""
        INSERT INTO condiciones(fecha,hipodromo,pista,estado,viento,clima,
                                observaciones,cargado_en)
        VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(fecha,hipodromo) DO UPDATE SET
          pista=excluded.pista, estado=excluded.estado,
          viento=excluded.viento, clima=excluded.clima,
          observaciones=excluded.observaciones, cargado_en=excluded.cargado_en
    """, (
        fecha, normalize_text(hipodromo),
        valores["pista"], valores["estado"], valores["viento"], valores["clima"],
        clean(d.get("observaciones", ""))[:400],
        datetime.now().isoformat(timespec="seconds"),
    ))
    con.commit()
    con.close()

    return jsonify(ok=True, mensaje="Condiciones guardadas para toda la reunión.",
                   oficiales=valores)


# ============================================================
# USUARIOS
# Usuario y contraseña, sin correo obligatorio.
# La contraseña NUNCA se guarda tal cual: se guarda cifrada.
# Recuperar la clave hoy es manual (el admin la resetea). El sistema
# queda preparado para sumar SMS, WhatsApp o correo sin rehacer nada.
# ============================================================

DIAS_SESION = 90     # cuanto dura la sesion sin volver a entrar


def _cifrar_clave(clave, sal=None):
    """Cifra la contraseña. Nunca se guarda como la escribió el usuario."""
    sal = sal or secrets.token_hex(16)
    mezcla = hashlib.pbkdf2_hmac("sha256", clave.encode(), sal.encode(), 120_000)
    return f"{sal}${mezcla.hex()}"


def _clave_correcta(clave, guardada):
    try:
        sal, _ = guardada.split("$", 1)
    except (ValueError, AttributeError):
        return False
    return secrets.compare_digest(_cifrar_clave(clave, sal), guardada)


def usuario_actual():
    """Devuelve el usuario de la sesión, o None si no ingresó."""
    token = (request.headers.get("X-Sesion", "")
             or request.cookies.get("lea_sesion", "")).strip()
    if not token:
        return None
    try:
        con = db()
        fila = con.execute("""
            SELECT u.id, u.usuario, u.usuario_visible, u.telefono, u.bloqueado,
                   s.creada_en
            FROM sesiones s JOIN usuarios u ON u.id = s.usuario_id
            WHERE s.token = ?
        """, (token,)).fetchone()
        if not fila:
            con.close()
            return None
        # Sesión vencida
        edad = (datetime.now() - datetime.fromisoformat(fila["creada_en"])).days
        if edad > DIAS_SESION or fila["bloqueado"]:
            con.execute("DELETE FROM sesiones WHERE token=?", (token,))
            con.commit()
            con.close()
            return None
        con.execute("UPDATE sesiones SET ultima_vez=? WHERE token=?",
                    (datetime.now().isoformat(timespec="seconds"), token))
        con.commit()
        con.close()
        return dict(fila)
    except Exception:
        return None


def _validar_registro(usuario, clave):
    """Devuelve un mensaje de error, o None si está todo bien."""
    if len(usuario) < 3:
        return "El usuario tiene que tener al menos 3 letras."
    if len(usuario) > 24:
        return "El usuario no puede tener más de 24 letras."
    if not re.fullmatch(r"[A-Za-z0-9_.\- ]+", usuario):
        return "El usuario solo puede tener letras, números, guiones y puntos."
    if len(clave) < 4:
        return "La contraseña tiene que tener al menos 4 caracteres."
    return None


@app.post("/api/registro")
def api_registro():
    d = request.get_json(silent=True) or {}
    usuario = clean(d.get("usuario", ""))
    clave = d.get("clave", "")
    telefono = clean(d.get("telefono", ""))[:30]

    error = _validar_registro(usuario, clave)
    if error:
        return jsonify(ok=False, error=error), 400

    clave_usuario = normalize_text(usuario)
    con = db()
    ya = con.execute("SELECT id FROM usuarios WHERE usuario=?",
                     (clave_usuario,)).fetchone()
    if ya:
        con.close()
        return jsonify(ok=False,
                       error="Ese nombre de usuario ya está tomado."), 409

    ahora = datetime.now().isoformat(timespec="seconds")
    cur = con.execute("""
        INSERT INTO usuarios(usuario, usuario_visible, clave_hash, telefono,
                             creado_en, ultimo_ingreso)
        VALUES(?,?,?,?,?,?)
    """, (clave_usuario, usuario, _cifrar_clave(clave), telefono, ahora, ahora))
    uid = cur.lastrowid
    token = secrets.token_urlsafe(32)
    con.execute("INSERT INTO sesiones(token,usuario_id,creada_en,ultima_vez) VALUES(?,?,?,?)",
                (token, uid, ahora, ahora))
    con.commit()
    con.close()

    resp = jsonify(ok=True, usuario=usuario, token=token,
                   mensaje=f"Bienvenido, {usuario}.")
    resp.set_cookie("lea_sesion", token, max_age=DIAS_SESION*24*3600,
                    samesite="Lax", secure=True, httponly=False)
    return resp


@app.post("/api/ingresar")
def api_ingresar():
    d = request.get_json(silent=True) or {}
    usuario = clean(d.get("usuario", ""))
    clave = d.get("clave", "")
    if not usuario or not clave:
        return jsonify(ok=False, error="Poné el usuario y la contraseña."), 400

    con = db()
    fila = con.execute("SELECT * FROM usuarios WHERE usuario=?",
                       (normalize_text(usuario),)).fetchone()
    if not fila or not _clave_correcta(clave, fila["clave_hash"]):
        con.close()
        # Mismo mensaje para los dos casos, para no dar pistas.
        return jsonify(ok=False, error="Usuario o contraseña incorrectos."), 401
    if fila["bloqueado"]:
        con.close()
        return jsonify(ok=False, error="Esta cuenta está bloqueada."), 403

    ahora = datetime.now().isoformat(timespec="seconds")
    token = secrets.token_urlsafe(32)
    con.execute("INSERT INTO sesiones(token,usuario_id,creada_en,ultima_vez) VALUES(?,?,?,?)",
                (token, fila["id"], ahora, ahora))
    con.execute("UPDATE usuarios SET ultimo_ingreso=? WHERE id=?", (ahora, fila["id"]))
    con.commit()
    con.close()

    resp = jsonify(ok=True, usuario=fila["usuario_visible"], token=token)
    resp.set_cookie("lea_sesion", token, max_age=DIAS_SESION*24*3600,
                    samesite="Lax", secure=True, httponly=False)
    return resp


@app.post("/api/salir")
def api_salir():
    token = (request.headers.get("X-Sesion", "")
             or request.cookies.get("lea_sesion", "")).strip()
    if token:
        con = db()
        con.execute("DELETE FROM sesiones WHERE token=?", (token,))
        con.commit()
        con.close()
    resp = jsonify(ok=True, mensaje="Sesión cerrada.")
    resp.set_cookie("lea_sesion", "", max_age=0)
    return resp


@app.get("/api/quien-soy")
def api_quien_soy():
    u = usuario_actual()
    if not u:
        return jsonify(ok=True, ingresado=False)
    return jsonify(ok=True, ingresado=True, usuario=u["usuario_visible"])


@app.get("/api/proxima-carrera")
def api_proxima_carrera():
    """
    La proxima carrera segun el horario oficial, para el visitante
    que todavia no tiene cuenta.
    """
    hipodromo = clean(request.args.get("hipodromo", ""))
    hoy = datetime.now().strftime("%Y-%m-%d")
    ahora = datetime.now().strftime("%H:%M")

    try:
        calendario = calendar_from_meetings(fetch(BASE + "/reuniones"))
    except Exception:
        return jsonify(ok=False, error="No se pudo consultar el calendario."), 503

    del_dia = [r for r in calendario if r["fecha"] == hoy]
    if hipodromo:
        del_dia = [r for r in del_dia
                   if normalize_text(r["hipodromo"]) == normalize_text(hipodromo)]
    if not del_dia:
        return jsonify(ok=True, hay=False,
                       mensaje="No hay carreras hoy en ese hipódromo.",
                       hipodromos=[r["hipodromo"] for r in calendario
                                   if r["fecha"] == hoy])

    reunion = del_dia[0]
    try:
        carreras = extract_races_from_meeting(fetch(reunion["url"]))
    except Exception:
        return jsonify(ok=False, error="No se pudo abrir la reunión."), 503

    # La primera cuya hora todavia no paso.
    pendientes = [c for c in carreras if c.get("hora") and c["hora"] >= ahora]
    proxima = pendientes[0] if pendientes else (carreras[-1] if carreras else None)
    if not proxima:
        return jsonify(ok=True, hay=False,
                       mensaje="Todavía no hay carreras publicadas.")

    return jsonify(
        ok=True, hay=True,
        fecha=hoy, hipodromo=reunion["hipodromo"], url=reunion["url"],
        carrera=proxima,
        ya_corrieron=len([c for c in carreras
                          if c.get("hora") and c["hora"] < ahora]),
        total=len(carreras),
        hipodromos=[r["hipodromo"] for r in calendario if r["fecha"] == hoy],
    )


@app.get("/api/admin/usuarios")
def admin_usuarios():
    if not es_admin():
        return jsonify(ok=False, error="Acceso restringido."), 403
    con = db()
    filas = con.execute("""
        SELECT id, usuario_visible, telefono, creado_en, ultimo_ingreso, bloqueado
        FROM usuarios ORDER BY id DESC LIMIT 200
    """).fetchall()
    total = con.execute("SELECT COUNT(*) c FROM usuarios").fetchone()["c"]
    con.close()
    return jsonify(ok=True, total=total, usuarios=[dict(f) for f in filas])


@app.post("/api/admin/resetear-clave")
def admin_resetear_clave():
    """
    El admin le pone una clave nueva a un usuario que la olvidó.
    Hoy es manual; mañana esto mismo puede dispararse por SMS o WhatsApp.
    """
    if not es_admin():
        return jsonify(ok=False, error="Acceso restringido."), 403

    d = request.get_json(silent=True) or {}
    usuario = clean(d.get("usuario", ""))
    nueva = d.get("clave", "")
    if not usuario or len(nueva) < 4:
        return jsonify(ok=False,
                       error="Falta el usuario o la clave es muy corta."), 400

    con = db()
    fila = con.execute("SELECT id FROM usuarios WHERE usuario=?",
                       (normalize_text(usuario),)).fetchone()
    if not fila:
        con.close()
        return jsonify(ok=False, error="No existe ese usuario."), 404
    con.execute("UPDATE usuarios SET clave_hash=? WHERE id=?",
                (_cifrar_clave(nueva), fila["id"]))
    # Se cierran sus sesiones abiertas, por seguridad.
    con.execute("DELETE FROM sesiones WHERE usuario_id=?", (fila["id"],))
    con.commit()
    con.close()
    return jsonify(ok=True,
                   mensaje=f"Clave nueva para {usuario}. Avisale cuál es.")


@app.post("/api/admin/bloquear")
def admin_bloquear():
    if not es_admin():
        return jsonify(ok=False, error="Acceso restringido."), 403
    d = request.get_json(silent=True) or {}
    usuario = clean(d.get("usuario", ""))
    bloquear = 1 if d.get("bloquear") else 0
    con = db()
    fila = con.execute("SELECT id FROM usuarios WHERE usuario=?",
                       (normalize_text(usuario),)).fetchone()
    if not fila:
        con.close()
        return jsonify(ok=False, error="No existe ese usuario."), 404
    con.execute("UPDATE usuarios SET bloqueado=? WHERE id=?", (bloquear, fila["id"]))
    if bloquear:
        con.execute("DELETE FROM sesiones WHERE usuario_id=?", (fila["id"],))
    con.commit()
    con.close()
    return jsonify(ok=True,
                   mensaje=("Usuario bloqueado." if bloquear else "Usuario habilitado."))


@app.get("/api/videos")
def videos():
    horse = request.args.get("caballo","").strip()
    if not horse:
        return jsonify(ok=False,error="Falta el caballo."),400
    query = f'{horse} carrera caballo Argentina'
    if not YOUTUBE_API_KEY:
        return jsonify(ok=True,modo="busqueda",url="https://www.youtube.com/results?search_query="+quote_plus(query),videos=[])
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {"part":"snippet","q":query,"type":"video","maxResults":5,"key":YOUTUBE_API_KEY}
    r = requests.get(url,params=params,timeout=20)
    r.raise_for_status()
    items = [{
        "id":x["id"]["videoId"],
        "titulo":x["snippet"]["title"],
        "miniatura":x["snippet"]["thumbnails"]["medium"]["url"]
    } for x in r.json().get("items",[])]
    return jsonify(ok=True,modo="api",videos=items)

@app.post("/api/guardar")
def guardar():
    data = request.get_json(silent=True) or {}
    if not all(data.get(k) for k in ["fecha","hipodromo","numero","participantes"]):
        return jsonify(ok=False,error="Faltan datos."),400
    con = db()
    con.execute("""
    INSERT INTO carreras(fecha,hipodromo,numero,premio,distancia,superficie,
    estado_publicado,condicion,pista_dia,clima,viento,retiros,observaciones,
    participantes,analisis,resultado_real,creado_en)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(fecha,hipodromo,numero) DO UPDATE SET
    pista_dia=excluded.pista_dia,clima=excluded.clima,viento=excluded.viento,
    retiros=excluded.retiros,observaciones=excluded.observaciones,
    participantes=excluded.participantes,analisis=excluded.analisis,
    creado_en=excluded.creado_en
    """,(
      data["fecha"],data["hipodromo"],int(data["numero"]),data.get("premio",""),
      data.get("distancia"),data.get("superficie",""),data.get("estado_publicado",""),
      data.get("condicion",""),data.get("pista_dia",""),data.get("clima",""),
      data.get("viento",""),json.dumps(data.get("retiros",[]),ensure_ascii=False),
      data.get("observaciones",""),json.dumps(data["participantes"],ensure_ascii=False),
      json.dumps(data.get("analisis",{}),ensure_ascii=False),"",
      datetime.now().isoformat(timespec="seconds")
    ))
    con.commit(); con.close()
    return jsonify(ok=True,mensaje="Carrera y análisis guardados.")

@app.get("/api/historial")
def historial():
    con=db()
    rows=con.execute("""SELECT id,fecha,hipodromo,numero,premio,pista_dia,clima,
    analisis,resultado_real FROM carreras ORDER BY fecha DESC,numero""").fetchall()
    con.close()
    return jsonify(ok=True,carreras=[dict(x) for x in rows])

# ============================================================
# RECOLECTOR AUTOMATICO
# Recorre las reuniones del Stud Book, analiza cada carrera y compara
# contra el resultado real. Corre solo en el servidor, sin que nadie
# tenga que abrir la app.
# ============================================================

RECOLECTOR = {
    "corriendo": False,
    "desde": "",
    "hasta": "",
    "reuniones_totales": 0,
    "reuniones_hechas": 0,
    "carreras_guardadas": 0,
    "carreras_comparadas": 0,
    "errores": 0,
    "ultimo_mensaje": "",
    "inicio": "",
    "fin": "",
}

PAUSA_ENTRE_PEDIDOS = float(os.getenv("PAUSA_SCRAPING", "1.5"))  # segundos


def _log_recolector(msg):
    RECOLECTOR["ultimo_mensaje"] = f"{datetime.now().strftime('%H:%M:%S')} — {msg}"


def procesar_carrera(url_reunion, numero, fecha, hipodromo):
    """
    Analiza una carrera y la registra. Devuelve 'comparada' si la carrera
    ya se corrio (habia puestos), 'guardada' si todavia no, o None si fallo.
    """
    try:
        soup = fetch(url_reunion)
        data = parse_race(soup, numero)
        if not data:
            return None
        participantes = [p for p in data.get("participantes", []) if not p.get("retirado")]
        if len(participantes) < 2:
            return None

        # Enriquecer solo si la carrera todavia no corrio: si ya corrio,
        # el puesto real ya alcanza y evitamos miles de pedidos extra.
        ya_corrida = any(p.get("puesto") for p in participantes)
        if not ya_corrida:
            participantes = [enrich_horse(dict(p)) for p in participantes]

        pesos = cargar_pesos()
        ranked, top = rankear(
            participantes,
            {"participantes": participantes, "pista_dia": data.get("estado", "")},
            pesos,
        )

        registrar_pronostico(
            url=url_reunion, numero=numero, fecha=fecha, hipodromo=hipodromo,
            top=top, participantes=participantes, pesos=pesos, ya_corrida=ya_corrida,
        )
        return "comparada" if ya_corrida else "guardada"
    except Exception:
        return None


def recolectar(desde, hasta):
    """Recorre todas las reuniones entre dos fechas y procesa sus carreras."""
    RECOLECTOR.update({
        "corriendo": True, "desde": desde, "hasta": hasta,
        "reuniones_totales": 0, "reuniones_hechas": 0,
        "carreras_guardadas": 0, "carreras_comparadas": 0, "errores": 0,
        "inicio": datetime.now().isoformat(timespec="seconds"), "fin": "",
    })
    try:
        _log_recolector("Pidiendo el calendario oficial…")
        calendario = calendar_from_meetings(fetch(BASE + "/reuniones"))

        reuniones = [r for r in calendario if desde <= r["fecha"] <= hasta]
        RECOLECTOR["reuniones_totales"] = len(reuniones)
        _log_recolector(f"{len(reuniones)} reuniones encontradas entre {desde} y {hasta}")

        for reunion in reuniones:
            if not RECOLECTOR["corriendo"]:
                _log_recolector("Detenido a pedido.")
                break
            try:
                soup = fetch(reunion["url"])
                carreras = extract_races_from_meeting(soup)
                _log_recolector(
                    f"{reunion['fecha']} {reunion['hipodromo']}: {len(carreras)} carreras"
                )
                for c in carreras:
                    if not RECOLECTOR["corriendo"]:
                        break
                    r = procesar_carrera(
                        reunion["url"], c["numero"], reunion["fecha"], reunion["hipodromo"]
                    )
                    if r == "comparada":
                        RECOLECTOR["carreras_comparadas"] += 1
                    elif r == "guardada":
                        RECOLECTOR["carreras_guardadas"] += 1
                    else:
                        RECOLECTOR["errores"] += 1
                    time.sleep(PAUSA_ENTRE_PEDIDOS)
            except Exception:
                RECOLECTOR["errores"] += 1
            RECOLECTOR["reuniones_hechas"] += 1
            time.sleep(PAUSA_ENTRE_PEDIDOS)

        _log_recolector("Terminado.")
    except Exception as e:
        _log_recolector(f"Error general: {e}")
    finally:
        RECOLECTOR["corriendo"] = False
        RECOLECTOR["fin"] = datetime.now().isoformat(timespec="seconds")


@app.post("/api/admin/recolectar")
def admin_recolectar():
    if not es_admin():
        return jsonify(ok=False, error="Acceso restringido."), 403
    if RECOLECTOR["corriendo"]:
        return jsonify(ok=False, error="Ya hay una recolección en curso."), 409

    body = request.get_json(silent=True) or {}
    hoy = datetime.now().strftime("%Y-%m-%d")
    desde = body.get("desde") or request.args.get("desde") or "2026-01-01"
    hasta = body.get("hasta") or request.args.get("hasta") or hoy

    hilo = threading.Thread(target=recolectar, args=(desde, hasta), daemon=True)
    hilo.start()
    return jsonify(ok=True, mensaje=f"Recolección iniciada de {desde} a {hasta}.")


@app.post("/api/admin/detener")
def admin_detener():
    if not es_admin():
        return jsonify(ok=False, error="Acceso restringido."), 403
    RECOLECTOR["corriendo"] = False
    return jsonify(ok=True, mensaje="Se pidió detener la recolección.")


@app.get("/api/admin/estado")
def admin_estado():
    if not es_admin():
        return jsonify(ok=False, error="Acceso restringido."), 403
    return jsonify(ok=True, **RECOLECTOR)


def revision_diaria():
    """
    Tarea de fondo permanente: cada 6 horas revisa los ultimos 7 dias.
    Asi las carreras que se van corriendo se comparan solas, sin que
    nadie abra la app.
    """
    time.sleep(60)  # dejar que el servidor termine de arrancar
    while True:
        try:
            if not RECOLECTOR["corriendo"]:
                hoy = datetime.now()
                desde = (hoy - timedelta(days=7)).strftime("%Y-%m-%d")
                hasta = hoy.strftime("%Y-%m-%d")
                recolectar(desde, hasta)
        except Exception:
            pass
        time.sleep(6 * 60 * 60)


init_db()

if os.getenv("REVISION_AUTOMATICA", "1") == "1":
    threading.Thread(target=revision_diaria, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=os.getenv("FLASK_DEBUG","0")=="1")
