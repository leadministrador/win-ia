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
import threading
import time
from datetime import datetime, timedelta

A = None   # el modulo app, se conecta con conectar(app)

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
}

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
    return largada_arg.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")


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
    existe, tiene más de 1 día, o es anterior a `despues_de` (la hora en
    que el caballo corrió hoy: así se pide UNA vez después de la carrera,
    no en cada pasada). Devuelve True si fue al sitio.
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
    r = A.explorar_caballo(perfil)     # anota motivo si falla; -1 si falló
    A._pausa_prudente()
    if r >= 0:
        ESTADO["fichas_traidas"] += 1
    else:
        ESTADO["fallas"] += 1
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
def _tabuladas_que_faltan(perfil):
    """Links de carreras de la campaña que no están guardadas, en orden."""
    faltan = []
    for c in _campana(perfil):          # la campaña viene de la más nueva a la más vieja
        url = A._url_canonica(c.get("enlace") or "")
        if not url or not url.startswith("http"):
            continue
        if A._sirve_para_tabulada(url):
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
    r = A.guardar_carrera_historica(url)   # anota motivo si falla
    A._pausa_prudente()
    if r >= 0:
        ESTADO["tabuladas_traidas"] += 1
        return True
    ESTADO["fallas"] += 1
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
        return False, 0
    corredores = [p for p in data.get("participantes", []) if not p.get("retirado")]
    ESTADO["carrera"] = f"{reunion['fecha']} {A._limpiar_nombre_hipodromo(reunion['hipodromo'])} {carrera['numero']}ª"

    # Si la carrera ya se corrió hoy, la ficha de cada corredor tiene una
    # carrera nueva: se vuelve a pedir una vez, después de la largada.
    despues_de = None
    if reunion["fecha"] == hoy and any(p.get("puesto") for p in corredores):
        despues_de = _hora_de_la_carrera_en_el_reloj_del_servidor(carrera.get("hora"))

    # caballos → ficha al día
    for p in corredores:
        if not prendido():
            return False, 0
        perfil = p.get("perfil", "")
        if perfil:
            _ficha_al_dia(perfil, despues_de)

    # pronóstico (no necesita las tabuladas: usa la campaña de cada ficha)
    if not _pronostico_sirve(reunion, carrera, data):
        _armar_pronostico(reunion, carrera, data)

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


def estado():
    d = dict(ESTADO)
    d["prendido"] = prendido()      # lo que dice el interruptor, no el hilo
    d["hora_argentina"] = A.ahora_argentina().strftime("%H:%M")
    d["hora_de_tabuladas"] = _es_hora_de_tabuladas() or _a_mano()
    d["hilos_vivos"] = sorted(t.name for t in threading.enumerate())
    d["hilo_motor_vivo"] = any(t.name == "motor" for t in threading.enumerate())
    d["reloj_servidor"] = datetime.now().isoformat(timespec="seconds")
    d["PARA_LEER"] = (
        ("Motor prendido. " if d["prendido"] else "Motor apagado. ")
        + (f"Ahora en {d['carrera']}. " if d["corriendo"] and d["carrera"] else "")
        + f"Pasada {d['pasada']}: {d['carreras_listas']} carreras listas, "
        + f"{d['carreras_pendientes']} tabuladas pendientes. "
        + f"Trajo {d['fichas_traidas']} fichas, {d['tabuladas_traidas']} tabuladas, "
        + f"armó {d['pronosticos_armados']} pronósticos, {d['fallas']} fallas."
    )
    return d
