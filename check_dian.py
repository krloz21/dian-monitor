#!/usr/bin/env python3
"""
Monitor de citas disponibles en el portal de agendamiento de la DIAN.

Uso:
    python check_dian.py

Variables de entorno:
    DIAN_URL           -> URL del portal (ya viene por defecto)
    DIAN_HEADLESS       -> "false" para ver el navegador (debug local).
                           En GitHub Actions se deja sin definir (= true).
    NTFY_TOPIC         -> nombre de tu canal ntfy.sh principal
    NTFY_TOPIC_2       -> (opcional) un segundo canal/celular a avisar
    TELEGRAM_BOT_TOKEN -> (opcional) alternativa/adicional a ntfy
    TELEGRAM_CHAT_ID   -> (opcional)

Flujo que sigue (confirmado sobre el portal real):
    Home -> "Agendar cita" -> Persona Natural -> Videoatención
    -> "Devoluciones" -> revisa si aparece el modal de "sin citas".

Nota sobre el diseño: la búsqueda de botones NO depende de selectores CSS
frágiles (clases que la DIAN puede cambiar) sino de recorrer los elementos
".boton" visibles y comparar su texto ya normalizado (sin tildes, minúsculas,
sin espacios de más). Esto demostró ser más confiable que apuntar a clases
específicas o depender de "esperar visibilidad" con timeouts que se cuelgan
si hay pequeñas animaciones o reflows.
"""

import os
import random
import time
import sys
import unicodedata
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
DIAN_URL = os.environ.get("DIAN_URL", "https://agendamiento.dian.gov.co/")

# En tu computador puedes poner DIAN_HEADLESS=false para VER el navegador
# abrirse y hacer los clics en tiempo real (útil para probar/depurar).
# En GitHub Actions siempre debe quedar en true (no hay pantalla ahí).
HEADLESS = os.environ.get("DIAN_HEADLESS", "true").lower() != "false"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_TOPIC_2 = os.environ.get("NTFY_TOPIC_2", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

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


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def human_delay(a=0.8, b=2.4):
    """Pausa aleatoria para simular tiempos de lectura/click humanos."""
    time.sleep(random.uniform(a, b))


def normalizar_texto(texto):
    """Quita tildes, pasa a minúsculas, colapsa espacios y quita puntuación
    final, para poder comparar textos sin que un acento o una mayúscula
    haga fallar la comparación."""
    if not texto:
        return ""
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    texto = texto.lower()
    texto = " ".join(texto.split())
    texto = texto.rstrip(" .,:;!¡¿?")
    return texto


# ---------------------------------------------------------------------------
# Búsqueda y clic de botones (robusto: recorre y compara texto normalizado)
# ---------------------------------------------------------------------------

def encontrar_boton_por_texto(page, texto_objetivo, selector=".boton"):
    frame = page.main_frame
    objetivo = normalizar_texto(texto_objetivo)
    botones = frame.locator(selector)
    cantidad = botones.count()

    for i in range(cantidad):
        boton = botones.nth(i)
        try:
            if not boton.is_visible():
                continue
            texto = boton.inner_text(timeout=1000)
            if objetivo in normalizar_texto(texto):
                return boton
        except Exception:
            continue
    return None


def hacer_clic_boton(page, texto, selector=".boton"):
    boton = encontrar_boton_por_texto(page, texto, selector)
    if boton is None:
        raise RuntimeError(f"No se encontró el botón: {texto}")
    boton.scroll_into_view_if_needed()
    boton.click()
    human_delay(1.5, 3)


# ---------------------------------------------------------------------------
# Detección de modales (para saber si hay citas o no)
# ---------------------------------------------------------------------------

def obtener_modales_visibles(page):
    frame = page.main_frame
    selectores = [".contenedor-modal", '[nombrepantalla="ModalError"]']
    resultados = []

    for selector in selectores:
        try:
            elementos = frame.locator(selector)
            for i in range(elementos.count()):
                elemento = elementos.nth(i)
                try:
                    if not elemento.is_visible():
                        continue
                    texto = elemento.inner_text(timeout=500)
                    if texto and texto.strip():
                        resultados.append(normalizar_texto(texto))
                except Exception:
                    continue
        except Exception:
            continue

    return list(dict.fromkeys(resultados))


def detectar_respuesta_dian(page, timeout=30):
    """
    Después de seleccionar Devoluciones, espera hasta `timeout` segundos a
    que aparezca una respuesta clara:
      - "NO_CITAS"      -> apareció el modal conocido de "sin citas"
      - "OTRA_RESPUESTA" -> apareció un modal distinto (probablemente SÍ
                            hay citas: calendario/horarios)
      - "SIN_RESPUESTA"  -> no se pudo confirmar nada en el tiempo dado

    No se usa el texto general de toda la página para decidir, porque la
    página contiene palabras como "cita", "agenda", "fecha", etc. incluso
    cuando no hay disponibilidad — solo se confía en los modales.
    """
    objetivo_no_citas = normalizar_texto(NO_CITAS_TEXTO)
    inicio = time.time()

    while time.time() - inicio < timeout:
        try:
            modales = obtener_modales_visibles(page)

            for texto_modal in modales:
                if objetivo_no_citas in texto_modal:
                    return "NO_CITAS"

            for texto_modal in modales:
                if "aceptar" in texto_modal and objetivo_no_citas not in texto_modal:
                    print(f"[INFO] Modal distinto encontrado: {texto_modal}")
                    return "OTRA_RESPUESTA"
        except Exception:
            pass
        time.sleep(0.2)

    return "SIN_RESPUESTA"


def aceptar_modal(page):
    """Cierra el modal de 'sin citas' dándole clic a su botón Aceptar."""
    try:
        hacer_clic_boton(page, "Aceptar")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Notificaciones
# ---------------------------------------------------------------------------

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


def notify(message: str, title: str = "Cita DIAN disponible"):
    """Envía la alerta por ntfy.sh (uno o dos canales) y/o Telegram."""
    sent = False

    topics = [t for t in (NTFY_TOPIC, NTFY_TOPIC_2) if t]
    if topics:
        with ThreadPoolExecutor(max_workers=len(topics)) as executor:
            trabajos = [executor.submit(_enviar_ntfy, t, message, title) for t in topics]
            for trabajo in as_completed(trabajos):
                ok, topic, error = trabajo.result()
                if ok:
                    sent = True
                    print(f"[OK] ntfy enviado a: {topic}")
                else:
                    print(f"[WARN] No se pudo notificar a {topic}: {error}")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
                timeout=10,
            )
            sent = True
        except Exception as e:
            print(f"[WARN] No se pudo notificar por Telegram: {e}")

    if not sent:
        print("[WARN] No hay ningún canal de notificación configurado o todos fallaron.")
    print(f"[NOTIFY] {message}")


# ---------------------------------------------------------------------------
# Flujo principal de navegación
# ---------------------------------------------------------------------------

def check_availability() -> bool:
    """
    Entra al portal, sigue el flujo Agendar cita -> Persona Natural ->
    Videoatención -> Devoluciones, y revisa qué modal aparece.

    Devuelve True si encontró disponibilidad, False si no hay citas o si
    algo falló / no se pudo confirmar (por seguridad, ante la duda no se
    notifica una falsa alarma).
    """
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
        found = False

        try:
            # 1. Home -> Agendar cita
            page.goto(DIAN_URL, wait_until="domcontentloaded", timeout=60000)
            human_delay(3, 5)
            page.screenshot(path="dian_paso1_home.png")
            hacer_clic_boton(page, "Agendar cita")

            # 2. Persona Natural
            hacer_clic_boton(page, "Persona Natural")

            # 3. Videoatención
            hacer_clic_boton(page, "Videoatención")
            page.screenshot(path="dian_paso2_tipo_persona.png")

            # 4. Tipo de servicio: Devoluciones
            page.screenshot(path="dian_paso3_tipo_servicio.png")
            hacer_clic_boton(page, "Devoluciones")

            # 5. Esperar la respuesta (modal de sin citas, u otro modal)
            resultado = detectar_respuesta_dian(page, timeout=30)
            page.screenshot(path="dian_result.png")

            if resultado == "NO_CITAS":
                print("[RESULTADO] No hay citas disponibles.")
                aceptar_modal(page)
                found = False
            elif resultado == "OTRA_RESPUESTA":
                print("[RESULTADO] ¡Posible disponibilidad! Apareció una respuesta distinta.")
                found = True
            else:
                print("[RESULTADO] No se pudo confirmar disponibilidad (sin respuesta clara).")
                found = False

        except Exception as e:
            print(f"[ERROR] Fallo navegando el portal: {e}")
            try:
                page.screenshot(path="dian_error.png")
            except Exception:
                pass
            found = False
        finally:
            if not HEADLESS:
                print("Terminó el flujo. Dejo el navegador abierto 15s para que lo veas...")
                time.sleep(15)
            browser.close()

        return found


def main():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] Revisando disponibilidad en la DIAN...")

    try:
        disponible = check_availability()
    except Exception as e:
        print(f"[ERROR] Excepción no controlada: {e}")
        sys.exit(1)

    if disponible:
        notify(
            f"¡Hay citas de Devoluciones (videoatención) posiblemente disponibles "
            f"en la DIAN! Entra ya a revisar: {DIAN_URL}"
        )
    else:
        print("Sin disponibilidad confirmada por ahora.")


if __name__ == "__main__":
    main()
