"""
MOTOR — el circuito del dibujo de Leandro (12/10/2026), en un solo lugar.

    Stud Book publica
        → reunión más próxima
            → carrera (en orden)
                → caballos (ficha al día)
                    → todas sus tabuladas, de la más cercana a la más lejana
                        → pronóstico armado y GUARDADO
    La pantalla solo muestra lo guardado. Nada se busca ni se calcula al abrir.
    Solo cambia si un caballo no corre (eso lo hace refrescar_las_que_estan_por_correrse).

Reglas de Leandro que este archivo respeta:
  - De a UNO, con pausa. Nunca de a varios.
  - Nadie va al Stud Book desde la pantalla.
  - Lo que falla se anota con motivo, se reintenta, y a los 3 va a la lista "a mano".
  - No toca usuarios, pagos, avisos, panel, afinamiento, anuncios, transmisiones.

Qué toca: historico (tabuladas), fichas (caballos), por_explorar (cola),
las carreras guardadas (cache carrera:/reuniones:/horarios:/calendario)
y el pronóstico guardado (cache listo:). Nada más.

Se prende y apaga desde el panel: interruptor "motor". Apagado, la app es la
de siempre. Prendido, las dos tareas viejas (22 h y 0 a 7) se quedan quietas
y este circuito hace el trabajo.
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

A = None   # el modulo app, se conecta con conectar(app)
# Para saber en qué proceso se importó. Si el que contesta el panel tiene
# otro pid, la app se importó en un proceso padre y se bifurcó (los hilos
# no sobreviven a eso).
PID_AL_IMPORTAR = os.getpid()

ESTADO = {
    "prendido": False,
    "corriendo": False,
    "arranco": "",
    "pasada": 0,
    "ultimo": "",
    "reunion": "",
    "carrera": "",
    "caballo": "",
    "fichas_traidas": 0,
    "tabuladas_traidas": 0,
    "pronosticos_armados": 0,
    "fallas": 0,
    "carreras_listas": 0,
    "carreras_pendientes": 0,
    "ultima_pasada_termino": "",
    "saltadas": [],          # carreras que el motor no armó, con motivo
    "cortes": 0,             # veces que el sitio no contestó (no cuentan como intento)
    "esperando_al_sitio": "",
    "tabuladas_sin_pedir": 0,  # resultados que ya tenía la app y pasaron a tabulada
    "esperando_pagos": [],     # llegadas sin pagos todavía: no se guardan en cero (14/10)
}
# Si el sitio no contesta: cuánto esperar antes de seguir (segundos, escalonado)
ESPERAS_POR_CORTE = [int(x) for x in os.getenv("MOTOR_ESPERAS_CORTE", "60,120,300").split(",")]
_cortes_seguidos = 0

# Cada cuánto vuelve a mirar el calendario del Stud Book (1 pedido).
# "Ni bien salgan las carreras": cada hora.
CADA_CUANTO_CALENDARIO = int(os.getenv("MOTOR_CALENDARIO_MIN", "60")) * 60
# Pausa entre pasadas enteras del circuito.
PAUSA_ENTRE_PASADAS = int(os.getenv("MOTOR_PAUSA_SEG", "120"))
# Las tabuladas viejas (el grueso de los pedidos) solo en este horario,
# para no molestar al sitio de día. Las fichas y el pronóstico de una
# carrera recién publicada se hacen a cualquier hora: son pocos pedidos.
TABULADAS_DESDE = int(os.getenv("RECOLECCION_DESDE", "0"))
TABULADAS_HASTA = int(os.getenv("RECOLECCION_HASTA", "7"))

_ultimo_calendario = 0.0
_ultima_vista = {}      # url de reunión -> cuándo se releyó por última vez
# La reunión de HOY se relee cada tanto para ver retirados y resultados.
# No en cada pasada: la tarea de cada 15 min ya mira la hora y media
# anterior a cada carrera.
RELEER_HOY_CADA = int(os.getenv("MOTOR_RELEER_HOY_MIN", "30")) * 60


def conectar(app_modulo):
    """app.py llama esto al final: le pasa su propio modulo."""
    global A
    A = app_modulo


def prendido():
    try:
        return A.ajuste("motor")
    except Exception:
        return False


# ───────────────────────────────────────────── piezas chicas
def _hora_arg():
    return A.ahora_argentina().hour


def _hora_de_la_carrera_en_el_reloj_del_servidor(hora):
    """
    La hora de largada viene en hora ARGENTINA ("12:45"). Las fichas se
    guardan con el reloj del SERVIDOR (Render está en UTC, +3 h). Para
    compararlas hay que pasar la largada al reloj del servidor.

    Error real del 12/10/2026: usaba datetime.now() (UTC) con la hora
    argentina; desde las 21 h argentinas el servidor ya está en el día
    siguiente y "hoy 12:45" caía mañana → toda ficha parecía vieja →
    pedía las 130 fichas de Palermo en cada pasada.
    """
    try:
        hh, mm = (hora or "00:00")[:5].split(":")
        largada_arg = A.ahora_argentina().replace(hour=int(hh), minute=int(mm),
                                                  second=0, microsecond=0)
    except Exception:
        largada_arg = A.ahora_argentina().replace(hour=0, minute=0, second=0, microsecond=0)
    # .astimezone() sin argumento = al huso del servidor; sin tzinfo queda
    # igual que datetime.now(), que es lo que guarda actualizada_en.
    largada_servidor = largada_arg.astimezone().replace(tzinfo=None)
    # Nunca en el futuro: una ficha no puede ser "anterior a la largada"
    # si la largada todavía no pasó (pasaría a pedirla en cada pasada).
    return min(largada_servidor, datetime.now()).isoformat(timespec="seconds")


def _es_hora_de_tabuladas():
    h = _hora_arg()
    if TABULADAS_DESDE <= TABULADAS_HASTA:
        return TABULADAS_DESDE <= h < TABULADAS_HASTA
    return h >= TABULADAS_DESDE or h < TABULADAS_HASTA


def _a_mano():
    """Si apretaron Arrancar, trabaja todo a cualquier hora."""
    try:
        return bool(A.A_MANO.get("prendido"))
    except Exception:
        return False


def _anotar_salto(reunion, carrera, motivo):
    """Una carrera que no se armó. Antes se saltaba en silencio: el panel
    no se enteraba (error visto el 12/10 con Palermo 13ª y La Plata 1ª)."""
    texto = (f"{reunion.get('fecha', '')} "
             f"{A._limpiar_nombre_hipodromo(reunion.get('hipodromo', ''))} "
             f"{carrera.get('numero', '?')}ª: {motivo}")
    if texto not in ESTADO["saltadas"]:
        ESTADO["saltadas"] = (ESTADO["saltadas"] + [texto])[-30:]


def _latir():
    try:
        ESTADO["latido"] = A.ahora_argentina().strftime("%H:%M:%S")
    except Exception:
        pass


def _motivo_de(url):
    """El último motivo anotado por _anotar_falla para esa dirección."""
    try:
        con = A.db()
        f = con.execute("SELECT valor FROM ajustes WHERE clave=?",
                        (f"falla:{url[-60:]}",)).fetchone()
        con.close()
        return (f["valor"] if f else "") or ""
    except Exception:
        return ""


def _fue_corte_del_sitio(url):
    """Falló porque el sitio no contestó (no por la carrera en sí)."""
    m = _motivo_de(url)
    return m.startswith("no se pudo conectar") or m.startswith("tardo demasiado")


def _esperar_al_sitio():
    """
    El Stud Book no contestó. Regla de Leandro: no castigar a la carrera,
    esperar y seguir cuando vuelve. Espera 1, 2, 5 minutos (escalonado)
    y mientras tanto mira el interruptor para poder apagarse.
    MEDIDO 13/10: una racha de 50 min mandó 31 carreras a "a mano" sin culpa.
    """
    global _cortes_seguidos
    _cortes_seguidos += 1
    ESTADO["cortes"] += 1
    espera = ESPERAS_POR_CORTE[min(_cortes_seguidos, len(ESPERAS_POR_CORTE)) - 1]
    ESTADO["esperando_al_sitio"] = f"corte {_cortes_seguidos}: espero {espera} s"
    fin = time.time() + espera
    while time.time() < fin and prendido():
        _latir()
        time.sleep(min(5, max(0, fin - time.time())))
    ESTADO["esperando_al_sitio"] = ""


def _sitio_contesto():
    global _cortes_seguidos
    _cortes_seguidos = 0


def _clave_listo(url_reunion, numero):
    return f"listo:{url_reunion}:{numero}"


def _huella(participantes):
    """Quiénes corren (sin los retirados), para saber si cambió algo."""
    return sorted(A.normalize_text(p.get("nombre", ""))
                  for p in participantes if not p.get("retirado"))


def pronostico_guardado(url_reunion, numero):
    """Lo que la pantalla muestra. Sin ir al sitio, sin calcular."""
    return A.lo_guardado(_clave_listo(url_reunion, numero))


# ───────────────────────────────────────────── 1) calendario y reuniones
def _traer_calendario():
    """1 pedido. Guarda el calendario como lo usa toda la app."""
    global _ultimo_calendario
    if time.time() - _ultimo_calendario < CADA_CUANTO_CALENDARIO:
        return A.lo_guardado("calendario") or []
    try:
        cal = A.calendario_completo()
        A.cache_set("calendario", cal)
        _ultimo_calendario = time.time()
        return cal
    except Exception as e:
        A._anotar_falla("calendario", e)
        ESTADO["fallas"] += 1
        return A.lo_guardado("calendario") or []


def _reuniones_publicadas():
    """De hoy en adelante, la más próxima primero."""
    hoy = A.hoy_argentina()
    cal = _traer_calendario()
    return sorted([r for r in cal if r.get("fecha", "") >= hoy],
                  key=lambda r: r["fecha"])


def _carreras_de(reunion):
    """
    Las carreras de una reunión, con sus participantes, guardadas.
    Va al sitio UNA vez por reunión si todavía no la tiene (o si es la
    de hoy, para ver retirados y resultados). Devuelve la lista de
    carreras [{numero, hora}] o [] si no se pudo.
    """
    clave_r = f"reuniones:{reunion['fecha']}:{A.normalize_text(reunion['hipodromo'])}"
    guardada = A.lo_guardado(clave_r) or []
    hoy = A.hoy_argentina()
    faltan_carreras = False
    if guardada:
        for c in guardada[0].get("carreras", []):
            if A.lo_guardado(f"carrera:{reunion['url']}:{c['numero']}") is None:
                faltan_carreras = True
                break
    if guardada and not faltan_carreras:
        if reunion["fecha"] != hoy:
            return guardada[0].get("carreras", [])
        if time.time() - _ultima_vista.get(reunion["url"], 0) < RELEER_HOY_CADA:
            return guardada[0].get("carreras", [])

    try:
        soup = A.fetch(reunion["url"], espera=A.ESPERA_DE_NOCHE)
    except Exception as e:
        A._anotar_falla(reunion["url"], e)
        ESTADO["fallas"] += 1
        return guardada[0].get("carreras", []) if guardada else []
    A._pausa_prudente()
    _ultima_vista[reunion["url"]] = time.time()

    carreras = A.extract_races_from_meeting(soup)
    if not carreras:
        return guardada[0].get("carreras", []) if guardada else []
    A.cache_set(clave_r, [{
        "hipodromo": A._limpiar_nombre_hipodromo(reunion["hipodromo"]),
        "url": reunion["url"],
        "carreras": carreras,
    }])
    A.cache_set(f"horarios:{reunion['url']}", [c.get("hora", "") for c in carreras])
    for c in carreras:
        try:
            data = A.parse_race(soup, c["numero"])
            if data:
                A.cache_set(f"carrera:{reunion['url']}:{c['numero']}", data)
        except Exception:
            continue
    return carreras


# ───────────────────────────────────────────── 2) caballos: ficha al día
def _ficha_al_dia(perfil, despues_de=None):
    """
    La lista de carreras del caballo, fresca. Va al sitio si la ficha no
    existe, tiene más de 1 día, o es anterior a `despues_de`:
      - la madrugada del día que corre (regla de Leandro 13/10: la ficha se
        pide de nuevo el día de la carrera, antes del pronóstico; INMATERIAL
        corrió con una victoria menos por no hacerlo), o
      - la hora en que ya corrió hoy (una vez después de la carrera).
    Devuelve True si fue al sitio.
    """
    con = A.db()
    f = con.execute("SELECT actualizada_en FROM fichas WHERE perfil=?",
                    (perfil,)).fetchone()
    con.close()
    limite = (datetime.now() - timedelta(days=A.DIAS_FICHA_FRESCA)
              ).isoformat(timespec="seconds")
    if despues_de and despues_de > limite:
        limite = despues_de
    vieja = (f is None) or ((f["actualizada_en"] or "") < limite)
    if not vieja:
        return False
    ESTADO["caballo"] = perfil[-30:]
    while prendido():
        _latir()
        r = A.explorar_caballo(perfil)     # anota motivo si falla; -1 si falló
        A._pausa_prudente()
        if r >= 0:
            ESTADO["fichas_traidas"] += 1
            _sitio_contesto()
            break
        if not _fue_corte_del_sitio(perfil):
            ESTADO["fallas"] += 1
            break
        _esperar_al_sitio()                # no es culpa del caballo: espera y reintenta
    return True


def _campana(perfil):
    con = A.db()
    f = con.execute("SELECT carreras FROM fichas WHERE perfil=?", (perfil,)).fetchone()
    con.close()
    try:
        return json.loads(f["carreras"]) if f and f["carreras"] else []
    except Exception:
        return []


# ───────────────────────────────────────────── 3) tabuladas: todas, cercana → lejana
def _fecha_iso(ddmmaaaa):
    try:
        return datetime.strptime(ddmmaaaa.strip(), "%d/%m/%Y").strftime("%Y-%m-%d")
    except Exception:
        return ""


def _tabulada_desde_lo_guardado(url, c):
    """
    Regla de Leandro (13/10): "ver los resultados y cargarlos en las
    tabuladas". Si la app ya tiene el resultado de esa carrera (la tarea
    de cada 15 min lo trae el mismo día, a `carrera:reunion:n`), se pasa
    al histórico como tabulada SIN pedir nada al sitio.
    Devuelve True si quedó guardada.
    """
    try:
        fecha = _fecha_iso(c.get("fecha", ""))
        hip = A.normalize_text(A._limpiar_nombre_hipodromo(c.get("hipodromo", "")))
        # El número de carrera: en la ficha, la columna "Nº" queda en
        # `reunion` (y `numero` es el número de partida del caballo:
        # pendiente 6 del documento). Si no está, va en el link:
        # .../carrera/231968/5-galitzia-...  → 5ª.
        numero = str(c.get("reunion", "")).strip()
        m = re.search(r"/carrera/\d+/(\d+)-", url)
        if m and (not numero.isdigit() or numero != m.group(1)):
            numero = m.group(1)
        if not (fecha and numero.isdigit() and hip):
            return False
        reunion = None
        for r in A.lo_guardado("calendario") or []:
            if (r.get("fecha") == fecha
                    and A.normalize_text(A._limpiar_nombre_hipodromo(r.get("hipodromo", ""))) == hip):
                reunion = r
                break
        if not reunion:
            return False
        data = A.lo_guardado(f"carrera:{reunion['url']}:{int(numero)}")
        if not data:
            return False
        corredores = [p for p in data.get("participantes", []) if not p.get("retirado")]
        if len(corredores) < 2 or not any(p.get("puesto") for p in corredores):
            return False
        # Regla de Leandro (14/10/2026): "no debe guardar en cero". El Stud
        # Book publica la llegada primero y los pagos unos minutos después;
        # si la relectura de la reunión cayó en el medio, los pagos vienen
        # 0.00. Entonces NO se guarda: queda pendiente y se vuelve a mirar
        # en la pasada siguiente (0 pedidos). MEDIDO 14/10: San Isidro 11ª
        # quedó en el histórico con 0.00 por esto.
        if not _hay_pagos(corredores):
            _anotar_sin_pagos(url)
            return False
        participantes = [{
            "nombre": p.get("nombre"), "numero": p.get("numero"), "puesto": p.get("puesto"),
            "peso": p.get("peso"), "peso_corporal": p.get("peso_corporal"),
            "jockey": p.get("jockey"), "entrenador": p.get("entrenador"),
            "caballeriza": p.get("caballeriza"), "cuerpos": p.get("cuerpos"),
            "acumulado": p.get("acumulado"), "pago": p.get("pago"),
            "perfil": p.get("perfil", ""),
            "rend_jockey": p.get("rend_jockey"), "rend_entrenador": p.get("rend_entrenador"),
            "rend_caballeriza": p.get("rend_caballeriza"),
            "edad": p.get("edad"), "sexo_tabla": p.get("sexo_tabla"), "forma": p.get("forma"),
            "dias_sin_correr": p.get("dias_sin_correr"), "campana_nums": p.get("campana_nums"),
            "efectividad": p.get("efectividad"), "ganado": p.get("ganado"),
            "detalle": p.get("detalle"), "retirado": False,
        } for p in corredores]
        con = A.db()
        con.execute("""
            INSERT OR REPLACE INTO historico(url, fecha, hipodromo, numero,
                distancia, pista, estado, participantes, guardado_en)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (url, fecha, A._limpiar_nombre_hipodromo(reunion.get("hipodromo", "")),
              data.get("carrera") or int(numero), data.get("distancia", ""),
              data.get("superficie", ""), data.get("estado", ""),
              json.dumps(participantes, ensure_ascii=False),
              datetime.now().isoformat(timespec="seconds")))
        con.commit()
        con.close()
        A._marcar_hecho(url)
        ESTADO["tabuladas_sin_pedir"] += 1
        return True
    except Exception:
        return False


def _hay_pagos(corredores):
    """True si al menos un corredor con puesto tiene un pago mayor a 0."""
    for p in corredores:
        if not p.get("puesto"):
            continue
        try:
            if float(str(p.get("pago") or "0").replace("$", "").replace(",", ".")) > 0:
                return True
        except ValueError:
            continue
    return False


def _anotar_sin_pagos(url):
    """Para el panel: qué carreras esperan los pagos (no se guardan en cero)."""
    texto = f"{A.ahora_argentina().strftime('%H:%M')} · {url[-45:]}"
    if not any(t.endswith(url[-45:]) for t in ESTADO["esperando_pagos"]):
        ESTADO["esperando_pagos"] = (ESTADO["esperando_pagos"] + [texto])[-20:]


def _guardada_en_cero(url, c):
    """
    True si esa carrera ya está en el histórico con la llegada pero sin
    pagos, y es de hoy o de ayer (las viejas no se tocan). Son las que
    quedaron guardadas en 0.00 antes de la regla del 14/10.
    """
    try:
        fecha = _fecha_iso(c.get("fecha", ""))
        if not fecha:
            return False
        hoy = datetime.strptime(A.hoy_argentina(), "%Y-%m-%d")
        if (hoy - datetime.strptime(fecha, "%Y-%m-%d")).days > 1:
            return False
        con = A.db()
        f = con.execute("SELECT participantes FROM historico WHERE url=?", (url,)).fetchone()
        con.close()
        if not f or not f["participantes"]:
            return False
        corredores = json.loads(f["participantes"])
        return any(p.get("puesto") for p in corredores) and not _hay_pagos(corredores)
    except Exception:
        return False


def _tabuladas_que_faltan(perfil):
    """
    Links de carreras de la campaña que no están guardadas, en orden
    (de la más nueva a la más vieja). Antes de darlas por faltantes,
    las que la app ya tiene como resultado pasan a tabulada sin pedir.
    """
    faltan = []
    for c in _campana(perfil):          # la campaña viene de la más nueva a la más vieja
        url = A._url_canonica(c.get("enlace") or "")
        if not url or not url.startswith("http"):
            continue
        if A._sirve_para_tabulada(url):
            # Guardada, pero si quedó en cero (hoy/ayer) se vuelve a mirar lo
            # que trajo la relectura de la reunión y se pisa cuando hay pagos.
            # 0 pedidos: nunca va al sitio por esto.
            if _guardada_en_cero(url, c):
                _tabulada_desde_lo_guardado(url, c)
            continue
        if _tabulada_desde_lo_guardado(url, c):
            continue
        faltan.append(url)
    return faltan


def _traer_tabulada(url):
    """1 pedido. True si quedó guardada."""
    ESTADO["ultimo"] = f"tabulada · {url[-38:]}"
    try:
        con = A.db()
        con.execute("UPDATE por_explorar SET intentos=intentos+1 WHERE url=?", (url,))
        con.commit(); con.close()
    except Exception:
        pass
    while prendido():
        _latir()
        r = A.guardar_carrera_historica(url)   # anota motivo si falla
        A._pausa_prudente()
        if r >= 0:
            ESTADO["tabuladas_traidas"] += 1
            _sitio_contesto()
            return True
        if not _fue_corte_del_sitio(url):
            ESTADO["fallas"] += 1
            return False
        # El sitio no contestó: no es culpa de la carrera. Se devuelve el
        # intento, se espera, y se vuelve a probar LA MISMA carrera.
        try:
            con = A.db()
            con.execute("UPDATE por_explorar SET intentos=MAX(intentos-1,0) WHERE url=?", (url,))
            con.commit(); con.close()
        except Exception:
            pass
        _esperar_al_sitio()
    return False


def _intentos(url):
    try:
        con = A.db()
        f = con.execute("SELECT intentos FROM por_explorar WHERE url=?", (url,)).fetchone()
        con.close()
        return (f["intentos"] or 0) if f else 0
    except Exception:
        return 0


# ───────────────────────────────────────────── 4) pronóstico guardado
def _armar_pronostico(reunion, carrera, data):
    """Arma y guarda. Mismo cálculo que la pantalla usaba al abrir."""
    corredores = [p for p in data.get("participantes", []) if not p.get("retirado")]
    if len(corredores) < 2:
        _anotar_salto(reunion, carrera,
                      f"{len(corredores)} corredores de {len(data.get('participantes', []))} anotados")
        return False
    completos = [A.enrich_horse(dict(p)) for p in corredores]   # de la base, sin sitio
    fecha = A.meeting_date_from_url(reunion["url"]) or reunion.get("fecha", "")
    hip = A._limpiar_nombre_hipodromo(reunion["hipodromo"])
    oficiales = A.condiciones_de(fecha, hip)
    contexto = {
        "participantes": completos,
        "distancia": data.get("distancia", ""),
        "hora": carrera.get("hora", ""),
        "hipodromo": hip,
        "categoria": data.get("categoria", ""),
        "condicion": data.get("condicion", ""),
    }
    for campo in A.OPCIONES_CONDICIONES:
        contexto[campo["clave"]] = oficiales.get(campo["clave"], "")
    pesos = A.cargar_pesos()
    _, top = A.rankear(completos, contexto, pesos)
    A.cache_set(_clave_listo(reunion["url"], carrera["numero"]), {
        "participantes": completos,
        "ranking": top,
        "confianza": round(top[0]["score"], 1) if top else 0,
        "ya_corrida": any(p.get("puesto") for p in corredores),
        "condiciones_oficiales": {c["clave"]: contexto.get(c["clave"], "")
                                  for c in A.OPCIONES_CONDICIONES},
        "huella": _huella(corredores),
        "armado_en": datetime.now().isoformat(timespec="seconds"),
        "motor": True,
    })
    ESTADO["pronosticos_armados"] += 1
    return True


def _pronostico_sirve(reunion, carrera, data):
    """Ya está guardado y los que corren son los mismos."""
    listo = pronostico_guardado(reunion["url"], carrera["numero"])
    if not listo or not listo.get("ranking"):
        return False
    corredores = [p for p in data.get("participantes", []) if not p.get("retirado")]
    return listo.get("huella") == _huella(corredores)


# ───────────────────────────────────────────── el circuito
def _una_carrera(reunion, carrera, hoy):
    """
    Una carrera, completa: caballos → fichas → tabuladas → pronóstico.
    Devuelve (lista: bool, tabuladas_pendientes: int).
    """
    data = A.lo_guardado(f"carrera:{reunion['url']}:{carrera['numero']}")
    if not data:
        _anotar_salto(reunion, carrera, "carrera no guardada")
        return False, 0
    corredores = [p for p in data.get("participantes", []) if not p.get("retirado")]
    ESTADO["carrera"] = f"{reunion['fecha']} {A._limpiar_nombre_hipodromo(reunion['hipodromo'])} {carrera['numero']}ª"

    # Si la carrera ya se corrió hoy, la ficha de cada corredor tiene una
    # carrera nueva: se vuelve a pedir una vez, después de la largada.
    despues_de = None
    if reunion["fecha"] == hoy:
        # La madrugada del día que corre: ficha de nuevo, antes del pronóstico.
        despues_de = _hora_de_la_carrera_en_el_reloj_del_servidor("00:00")
        if any(p.get("puesto") for p in corredores):
            # Y una vez más después de la carrera (trae la carrera nueva).
            despues_de = _hora_de_la_carrera_en_el_reloj_del_servidor(carrera.get("hora"))

    # caballos → ficha al día
    for p in corredores:
        if not prendido():
            return False, 0
        perfil = p.get("perfil", "")
        if perfil:
            _ficha_al_dia(perfil, despues_de)

    # pronóstico (no necesita las tabuladas: usa la campaña de cada ficha)
    if _pronostico_sirve(reunion, carrera, data) or _armar_pronostico(reunion, carrera, data):
        # tiene pronóstico: si antes figuraba como saltada, ya no
        ESTADO["saltadas"] = [t for t in ESTADO["saltadas"]
                              if not t.startswith(f"{reunion['fecha']} ")
                              or f" {carrera.get('numero')}ª:" not in t]

    # tabuladas: todas, de la más cercana a la más lejana, caballo por caballo
    pendientes = 0
    for p in corredores:
        perfil = p.get("perfil", "")
        if not perfil:
            continue
        for url in _tabuladas_que_faltan(perfil):
            if not prendido():
                return False, pendientes
            if _intentos(url) >= 3:
                pendientes += 1          # queda para la lista "a mano"
                continue
            if not (_es_hora_de_tabuladas() or _a_mano()):
                pendientes += 1          # de día no se molesta al sitio
                continue
            if not _traer_tabulada(url):
                pendientes += 1
    return pendientes == 0, pendientes


def una_pasada():
    """Recorre todo lo publicado, lo más próximo primero. Vuelve enseguida."""
    if ESTADO["corriendo"]:
        return {"hecho": False, "motivo": "ya hay una pasada en curso"}
    ESTADO["corriendo"] = True
    ESTADO["pasada"] += 1
    listas = pendientes_total = 0
    try:
        hoy = A.hoy_argentina()
        for reunion in _reuniones_publicadas():
            if not prendido():
                break
            ESTADO["reunion"] = f"{reunion['fecha']} {A._limpiar_nombre_hipodromo(reunion['hipodromo'])}"
            carreras = _carreras_de(reunion)
            for c in carreras:
                if not prendido():
                    break
                lista, pend = _una_carrera(reunion, c, hoy)
                listas += 1 if lista else 0
                pendientes_total += pend
        ESTADO["carreras_listas"] = listas
        ESTADO["carreras_pendientes"] = pendientes_total
        ESTADO["ultima_pasada_termino"] = A.ahora_argentina().strftime("%H:%M:%S")
        ESTADO["ultimo"] = (f"pasada {ESTADO['pasada']}: {listas} carreras listas, "
                            f"{pendientes_total} tabuladas pendientes")
        # Si quedó algo sin traer y es nuevo, un aviso al celular de Leandro
        # (regla del 13/10: "que me mande una notificación y yo lo voy a prender").
        try:
            ESTADO["avisos_enviados"] = ESTADO.get("avisos_enviados", 0) + (A.avisar_lo_que_viene() or 0)
        except Exception:
            pass
        return {"hecho": True, "listas": listas, "pendientes": pendientes_total}
    finally:
        ESTADO["corriendo"] = False
        ESTADO["caballo"] = ""


def bucle():
    """Hilo de fondo. Si el interruptor está apagado, no hace nada."""
    ESTADO["latido"] = "esperando 45 s"
    time.sleep(45)
    while True:
        ESTADO["latido"] = A.ahora_argentina().strftime("%H:%M:%S")
        try:
            if prendido():
                if not ESTADO["prendido"]:
                    ESTADO["prendido"] = True
                    ESTADO["arranco"] = A.ahora_argentina().strftime("%Y-%m-%d %H:%M")
                una_pasada()
                # Resultados del día y retirados de la próxima hora y media:
                # los sigue haciendo la tarea de cada 15 min (revision_de_avisos),
                # que no se toca. Si cambia un corredor, esa tarea rehace el
                # pronóstico y lo guarda; el motor lo ve y no lo pisa.
            else:
                ESTADO["prendido"] = False
        except Exception as e:
            ESTADO["ultimo"] = f"error: {str(e)[:80]}"
            ESTADO["fallas"] += 1
        time.sleep(PAUSA_ENTRE_PASADAS)


def _bucle_protegido():
    """Si el hilo se muere, que quede escrito por qué."""
    try:
        bucle()
    except BaseException as e:      # noqa
        ESTADO["hilo_murio"] = f"{type(e).__name__}: {str(e)[:120]}"
        raise


def arrancar_hilo():
    threading.Thread(target=_bucle_protegido, daemon=True, name="motor").start()


def esta_sano(minutos=15):
    """
    Para el plan B de app.py: si el motor está prendido pero muerto (el
    hilo no existe o lleva `minutos` sin latir), las tareas de siempre
    vuelven a trabajar solas. Regla de Leandro (13/10): "si el motor no
    anda, que la otra app siga".
    """
    try:
        if not any(t.name == "motor" and t.is_alive() for t in threading.enumerate()):
            return False
        latido = ESTADO.get("latido", "")
        if not latido or ":" not in latido:
            return True        # recién arrancó, todavía en los 45 s
        hh, mm, ss = [int(x) for x in latido.split(":")]
        ahora = A.ahora_argentina()
        ultimo = ahora.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        if ultimo > ahora:                  # pasó la medianoche
            ultimo -= timedelta(days=1)
        return (ahora - ultimo).total_seconds() < minutos * 60
    except Exception:
        return False


def estado():
    d = dict(ESTADO)
    d["prendido"] = prendido()      # lo que dice el interruptor, no el hilo
    d["hora_argentina"] = A.ahora_argentina().strftime("%H:%M")
    d["hora_de_tabuladas"] = _es_hora_de_tabuladas() or _a_mano()
    d["hilos_vivos"] = sorted(t.name for t in threading.enumerate())
    d["hilo_motor_vivo"] = any(t.name == "motor" for t in threading.enumerate())
    d["sano"] = esta_sano()
    d["reloj_servidor"] = datetime.now().isoformat(timespec="seconds")
    d["pid"] = os.getpid()
    d["pid_al_importar"] = PID_AL_IMPORTAR
    d["pid_padre"] = os.getppid()
    d["gunicorn_args"] = os.environ.get("GUNICORN_CMD_ARGS", "")
    d["PARA_LEER"] = (
        ("Motor prendido. " if d["prendido"] else "Motor apagado. ")
        + (f"Ahora en {d['carrera']}. " if d["corriendo"] and d["carrera"] else "")
        + f"Pasada {d['pasada']}: {d['carreras_listas']} carreras listas, "
        + f"{d['carreras_pendientes']} tabuladas pendientes. "
        + f"Trajo {d['fichas_traidas']} fichas, {d['tabuladas_traidas']} tabuladas, "
        + f"armó {d['pronosticos_armados']} pronósticos, {d['fallas']} fallas, "
        + f"{d['cortes']} cortes del sitio, {d['tabuladas_sin_pedir']} tabuladas sin pedir."
        + (f" {d['esperando_al_sitio']}." if d["esperando_al_sitio"] else "")
        + (f" Saltó {len(d['saltadas'])}: " + " · ".join(d["saltadas"][-5:]) if d["saltadas"] else "")
    )
    return d
