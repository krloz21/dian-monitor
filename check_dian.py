#!/usr/bin/env python3
"""
Monitor de citas de Devoluciones (Videoatención) en el portal de la DIAN.

MODOS DE USO
    python check_dian.py              -> una consulta ya, ignorando el horario
                                         (para pruebas manuales)
    python check_dian.py --una-vez    -> lo que usa GitHub: respeta el horario
                                         y reintenta si la página está caída

Los horarios (qué tan seguido se consulta cada día) están definidos en el
workflow .github/workflows/check-dian.yml, que es quien dispara el script a
la hora exacta. Aquí abajo solo queda el horario permitido general
(6:00-21:00) como red de seguridad.

VARIABLES DE ENTORNO
    DIAN_HEADLESS   -> "false" para ver el navegador (pruebas locales)
    NTFY_TOPIC      -> canal ntfy principal
    NTFY_TOPIC_2    -> (opcional) segundo celular
    FORZAR          -> "true" para consultar sin importar el horario
"""

import os
import sys
import json
import time
import random
import unicodedata
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ============================================================
# HORARIO  (todo en hora de Colombia) — ajústalo aquí
# ============================================================

COLOMBIA = timezone(timedelta(hours=-5))   # Colombia no tiene horario de verano

HORA_INICIO = 6    # no consulta antes de las 6:00
HORA_FIN = 21      # no consulta desde las 21:00 en adelante

# Minutos entre consultas según el día (0 = lunes ... 6 = domingo)
INTERVALO_POR_DIA = {
    0: 60,    # lunes
    1: 45,    # martes
    2: 30,    # miércoles
    3: 20,    # jueves
    4: 10,    # viernes (fuera de la franja pico)
    5: 120,   # sábado
    6: 120,   # domingo
}

VIERNES_PICO_INICIO = 10          # 10:00
VIERNES_MAXIMO_FIN = 12           # 10:00-12:00 -> lo más intenso
VIERNES_PICO_FIN = 14             # 12:00-14:00 -> intenso
INTERVALO_VIERNES_MAXIMO = 5      # minutos
INTERVALO_VIERNES_PICO = 10       # minutos
INTERVALO_REINTENTO_CAIDA = 5     # si la página se cayó en la franja pico

TOLERANCIA_MIN = 2   # margen por los retrasos del cron de GitHub

ARCHIVO_ESTADO = os.path.join(".estado", "estado.json")

# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

DIAN_URL = os.environ.get("DIAN_URL", "https://agendamiento.dian.gov.co/")
HEADLESS = os.environ.get("DIAN_HEADLESS", "true").lower() != "false"
FORZAR = os.environ.get("FORZAR", "false").lower() == "true"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_TOPIC_2 = os.environ.get("NTFY_TOPIC_2", "").strip()

NO_CITAS_TEXTO = (
    "No se encontraron especialidades relacionadas "
    "según los filtros seleccionados."
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
]

# Resultados posibles de una consulta
CITAS = "CITAS"
NO_CITAS = "NO_CITAS"
CAIDA = "CAIDA"


# ============================================================
# UTILIDADES
# ============================================================

def ahora():
    return datetime.now(COLOMBIA)


def log(msg):
    print(f"[{ahora().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def human_delay(a=0.8, b=2.4):
    time.sleep(random.uniform(a, b))


def normalizar_texto(texto):
    if not texto:
        return ""
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    texto = " ".join(texto.lower().split())
    return texto.rstrip(" .,:;!¡¿?")


# ============================================================
# LÓGICA DE HORARIO
# ============================================================

def en_franja_viernes(t):
    return t.weekday() == 4 and VIERNES_PICO_INICIO <= t.hour < VIERNES_PICO_FIN


def leer_estado():
    try:
        with open(ARCHIVO_ESTADO, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_estado(resultado):
    os.makedirs(os.path.dirname(ARCHIVO_ESTADO), exist_ok=True)
    with open(ARCHIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(
            {"ultima_consulta": ahora().isoformat(), "ultimo_resultado": resultado},
            f,
        )


# ============================================================
# NAVEGACIÓN DEL PORTAL
# ============================================================

def encontrar_boton_por_texto(page, texto_objetivo, selector=".boton"):
    objetivo = normalizar_texto(texto_objetivo)
    botones = page.main_frame.locator(selector)
    for i in range(botones.count()):
        boton = botones.nth(i)
        try:
            if boton.is_visible() and objetivo in normalizar_texto(boton.inner_text(timeout=1000)):
                return boton
        except Exception:
            continue
    return None


def hacer_clic_boton(page, texto, intentos=10):
    """Busca el botón varias veces (la página puede tardar en pintarlo)."""
    for _ in range(intentos):
        boton = encontrar_boton_por_texto(page, texto)
        if boton is not None:
            boton.scroll_into_view_if_needed()
            boton.click()
            human_delay(1.5, 3)
            return
        time.sleep(1.5)
    raise RuntimeError(f"No se encontró el botón: {texto}")


def obtener_modales_visibles(page):
    resultados = []
    for selector in (".contenedor-modal", '[nombrepantalla="ModalError"]'):
        try:
            elementos = page.main_frame.locator(selector)
            for i in range(elementos.count()):
                el = elementos.nth(i)
                try:
                    if el.is_visible():
                        texto = el.inner_text(timeout=500)
                        if texto and texto.strip():
                            resultados.append(normalizar_texto(texto))
                except Exception:
                    continue
        except Exception:
            continue
    return list(dict.fromkeys(resultados))


def detectar_respuesta_dian(page, timeout=30):
    objetivo = normalizar_texto(NO_CITAS_TEXTO)
    inicio = time.time()
    while time.time() - inicio < timeout:
        try:
            modales = obtener_modales_visibles(page)
            for m in modales:
                if objetivo in m:
                    return NO_CITAS
            for m in modales:
                if "aceptar" in m:
                    log(f"Modal distinto encontrado: {m}")
                    return CITAS
        except Exception:
            pass
        time.sleep(0.2)
    return None   # sin respuesta clara


def consultar_dian():
    """Hace una consulta completa. Devuelve CITAS, NO_CITAS o CAIDA."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1366, "height": 768},
            locale="es-CO",
            timezone_id="America/Bogota",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()
        resultado = CAIDA

        try:
            respuesta = page.goto(DIAN_URL, wait_until="domcontentloaded", timeout=60000)
            if respuesta is not None and respuesta.status >= 500:
                log(f"La DIAN respondió con error {respuesta.status} -> página caída.")
                page.screenshot(path="dian_error.png")
                return CAIDA

            human_delay(3, 5)
            page.screenshot(path="dian_paso1_home.png")

            hacer_clic_boton(page, "Agendar cita")
            hacer_clic_boton(page, "Persona Natural")
            hacer_clic_boton(page, "Videoatención")
            page.screenshot(path="dian_paso2_tipo_persona.png")
            hacer_clic_boton(page, "Devoluciones")

            r = detectar_respuesta_dian(page, timeout=30)
            page.screenshot(path="dian_result.png")

            if r == NO_CITAS:
                log("Resultado: NO hay citas.")
                resultado = NO_CITAS
            elif r == CITAS:
                log("Resultado: ¡POSIBLES CITAS DISPONIBLES!")
                resultado = CITAS
            else:
                log("La página no respondió a tiempo -> se trata como caída.")
                resultado = CAIDA

        except Exception as e:
            log(f"Fallo navegando el portal (se trata como caída): {e}")
            try:
                page.screenshot(path="dian_error.png")
            except Exception:
                pass
            resultado = CAIDA

        finally:
            if not HEADLESS:
                log("Dejo el navegador abierto 15 s para que lo veas...")
                time.sleep(15)
            browser.close()

        return resultado


# ============================================================
# NOTIFICACIONES
# ============================================================

def _enviar_ntfy(topic, mensaje, titulo):
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=mensaje.encode("utf-8"),
            headers={"Title": titulo, "Priority": "urgent", "Tags": "warning,calendar"},
            timeout=10,
        )
        return 200 <= r.status_code < 300, topic, None
    except Exception as e:
        return False, topic, str(e)


def notificar(mensaje, titulo="Cita DIAN disponible"):
    topics = [t for t in (NTFY_TOPIC, NTFY_TOPIC_2) if t]
    if not topics:
        log("[WARN] No hay NTFY_TOPIC configurado.")
        return
    with ThreadPoolExecutor(max_workers=len(topics)) as ex:
        for fut in as_completed([ex.submit(_enviar_ntfy, t, mensaje, titulo) for t in topics]):
            ok, topic, error = fut.result()
            log(f"ntfy {topic}: {'enviado' if ok else 'ERROR ' + str(error)}")


def consultar_y_avisar():
    resultado = consultar_dian()
    if resultado == CITAS:
        notificar(
            "¡Hay citas de Devoluciones (videoatención) posiblemente disponibles "
            f"en la DIAN! Entra ya: {DIAN_URL}"
        )
    return resultado


# ============================================================
# MODOS
# ============================================================

def modo_una_vez():
    """Una consulta. La dispara GitHub a la hora exacta que toca.

    Igual se verifica el horario permitido (6:00-21:00) como red de
    seguridad, por si un cron quedó mal escrito o GitHub se retrasa mucho.

    Si la página está caída, reintenta un par de veces antes de rendirse
    (esto es lo que pediste para el viernes, cuando el portal se cae).
    """
    t = ahora()
    if not FORZAR and not (HORA_INICIO <= t.hour < HORA_FIN):
        log(f"Son las {t.strftime('%H:%M')}. Fuera del horario permitido "
            f"({HORA_INICIO}:00-{HORA_FIN}:00). No se consulta.")
        return

    time.sleep(random.uniform(0, 40))   # variación para no ser predecible

    resultado = consultar_y_avisar()

    # Si la página se cayó, reintenta: en la franja pico del viernes hasta
    # 2 veces más (esperando 5 min), el resto de días 1 vez (esperando 2 min).
    reintentos = 2 if en_franja_viernes(ahora()) else 1
    espera_min = INTERVALO_REINTENTO_CAIDA if en_franja_viernes(ahora()) else 2

    intento = 0
    while resultado == CAIDA and intento < reintentos:
        intento += 1
        log(f"Página caída. Reintento {intento}/{reintentos} en {espera_min} min...")
        time.sleep(espera_min * 60)
        resultado = consultar_y_avisar()

    guardar_estado(resultado)


def main():
    args = sys.argv[1:]
    if "--una-vez" in args:
        modo_una_vez()
    else:
        log("Consulta manual única (ignora el horario)...")
        r = consultar_y_avisar()
        log(f"Resultado final: {r}")


if __name__ == "__main__":
    main()
