
from flask import Flask, render_template, request, jsonify
import requests, re, sqlite3, json, os, time, threading
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote_plus
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
    races = []
    for h in soup.find_all(["h2","h3"]):
        m = re.search(r"(\d+)\s*[º°ª]?\s*Carrera\b", clean(h.get_text(" ")), re.I)
        if m:
            races.append({"numero": int(m.group(1)), "titulo": clean(h.get_text(" "))})
    return races

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
        "categoria": get(r"Categoria:\s*(.+?)(?:Premios|PROGRAMA|RESULTADOS|$)"),
        "participantes": participants
    }


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
            if "hip" in h and "hipodromo" not in idx: idx["hipodromo"] = i
            elif h in ("n°", "n", "nº") and "puesto" not in idx: idx["puesto"] = i
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
                m_yt = re.search(r"youtube\.com/embed/([A-Za-z0-9_-]{6,})", a["href"])
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
                "hipodromo": col("hipodromo"),
                "puesto": int(puesto_txt) if puesto_txt.isdigit() else None,
                "numero": col("numero"),
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


def enrich_horse(horse):
    profile = horse.get("perfil", "")
    if not profile:
        return horse
    try:
        soup = fetch(profile)
        texto = clean(soup.get_text(" "))

        resumen = _resumen_del_perfil(soup, texto)
        horse.update({k: v for k, v in resumen.items() if v})
        horse.setdefault("sexo", "")

        carreras = _tabla_carreras_del_perfil(soup)
        horse["carreras"] = carreras[:20]

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
    except Exception:
        horse.setdefault("sexo", "")
        horse.setdefault("campana", "")
        horse.setdefault("actuaciones", [])
        horse.setdefault("carreras", [])
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

    if context.get("pista_dia") in ["Pesada","Barrosa","Húmeda"] and any(
        x in (campaign+" "+detail) for x in ["pesada","barrosa","húmeda","humeda"]
    ):
        score += P["pista_compatible"]
        reasons.append("antecedente compatible con la pista del día")
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


@app.get("/api/carrera")
def carrera():
    url = request.args.get("url","")
    numero = request.args.get("numero","")
    forzar = request.args.get("refresh") == "1"
    if not url.startswith(BASE) or not numero.isdigit():
        return jsonify(ok=False,error="Datos inválidos."),400

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

@app.post("/api/enriquecer")
def enriquecer():
    data = request.get_json(silent=True) or {}
    horses = data.get("participantes", [])
    return jsonify(ok=True,participantes=[enrich_horse(dict(h)) for h in horses])

@app.post("/api/analizar")
def analizar():
    data = request.get_json(silent=True) or {}
    horses = [h for h in data.get("participantes",[]) if not h.get("retirado")]
    if len(horses) < 2:
        return jsonify(ok=False,error="Se necesitan al menos dos participantes confirmados."),400
    pesos = cargar_pesos()
    ranked = []
    for h in horses:
        score, reasons = score_horse(h, data, pesos)
        ranked.append({**h,"score":score,"motivos":reasons})
    ranked.sort(key=lambda x:x["score"], reverse=True)
    top = ranked[:4]
    total = sum(x["score"] for x in top) or 1
    for x in top:
        x["probabilidad_relativa"] = round(x["score"]/total*100,1)

    # Guardar el pronostico y, si la carrera ya se corrio, comparar en el acto.
    ya_corrida = any(h.get("puesto") for h in horses)
    try:
        registrar_pronostico(
            url=data.get("url",""),
            numero=data.get("numero"),
            fecha=data.get("fecha",""),
            hipodromo=data.get("hipodromo",""),
            top=top,
            participantes=horses,
            pesos=pesos,
            ya_corrida=ya_corrida,
        )
    except Exception:
        pass  # que un fallo al guardar nunca rompa el pronostico al usuario

    return jsonify(ok=True,ranking=top,confianza=round(top[0]["score"],1),
                   ya_corrida=ya_corrida)


def registrar_pronostico(url, numero, fecha, hipodromo, top, participantes,
                         pesos, ya_corrida):
    """
    Guarda lo que predijo la app. Si la carrera ya tiene puestos reales,
    calcula el acierto y ajusta el algoritmo automaticamente.
    """
    if not url or numero is None:
        return

    predichos = [x["nombre"] for x in top]
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


def ajustar_algoritmo():
    """
    Compara todos los pronosticos ya resueltos y afina los pesos.
    Metodo conservador: mueve cada peso de a poco segun si viene acertando
    mejor o peor que el promedio historico. Nunca hace saltos bruscos.
    """
    con = db()
    filas = con.execute("""
        SELECT acierto_ganador, aciertos_top4, pesos_usados
        FROM pronosticos WHERE resultado IS NOT NULL
        ORDER BY id DESC LIMIT 200
    """).fetchall()
    con.close()

    if len(filas) < 10:
        return  # con menos de 10 carreras no hay con que aprender

    recientes = filas[:30]
    historico = filas

    tasa_reciente = sum(f["aciertos_top4"] or 0 for f in recientes) / (len(recientes) * 4)
    tasa_historica = sum(f["aciertos_top4"] or 0 for f in historico) / (len(historico) * 4)

    pesos = cargar_pesos()
    # Si lo reciente va peor que el historico, se explora un poco mas.
    # Si va mejor, se refuerza la direccion actual. Paso chico: 3%.
    direccion = 1.0 if tasa_reciente >= tasa_historica else -1.0
    paso = 0.03 * direccion

    for clave in pesos:
        nuevo = pesos[clave] * (1 + paso)
        limite = abs(PESOS_INICIALES[clave]) * 2.5
        if PESOS_INICIALES[clave] >= 0:
            pesos[clave] = max(0.0, min(limite, nuevo))
        else:
            pesos[clave] = min(0.0, max(-limite, nuevo))

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
        ranked = []
        for p in participantes:
            score, motivos = score_horse(p, {"participantes": participantes,
                                             "pista_dia": data.get("estado", "")}, pesos)
            ranked.append({**p, "score": score, "motivos": motivos})
        ranked.sort(key=lambda x: x["score"], reverse=True)
        top = ranked[:4]
        total = sum(x["score"] for x in top) or 1
        for x in top:
            x["probabilidad_relativa"] = round(x["score"] / total * 100, 1)

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
