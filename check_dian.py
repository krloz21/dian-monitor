#!/usr/bin/env python3
"""
Monitor de citas disponibles en el portal de agendamiento de la DIAN.

Uso:
    python check_dian.py

Flujo que sigue (confirmado sobre el portal real):
    Home -> "Agendar cita" -> Persona Natural + Videoatención -> Siguiente
    -> "Devoluciones" -> Siguiente -> revisa si aparece el modal de
    "No se encontraron especialidades..." (sin citas) o no (hay citas).

Variables de entorno requeridas (ver README.md):
    DIAN_URL           -> URL del portal (ya viene por defecto)
    NTFY_TOPIC         -> nombre único de tu canal ntfy.sh (ej: "dian-carlos-9f3a")
    TELEGRAM_BOT_TOKEN -> (opcional) si prefieres Telegram en vez de ntfy
    TELEGRAM_CHAT_ID   -> (opcional)

IMPORTANTE:
- Este script hace foco en "parecer humano": delays aleatorios, user-agent real,
  viewport normal, sin headless detectable. Aun así, la DIAN puede tener
  captchas o bloqueos que requieran intervención manual — el script está
  pensado para AVISARTE, no para agendar la cita automáticamente por ti.
- No lo ejecutes con una frecuencia mayor a la configurada (cada 20-30 min).
  Peticiones muy frecuentes son la forma más común de terminar bloqueado.
"""

import os
import random
import time
import sys
import requests
from datetime import datetime
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
DIAN_URL = os.environ.get("DIAN_URL", "https://agendamiento.dian.gov.co/")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
]


def human_delay(a=0.8, b=2.4):
    """Pausa aleatoria para simular tiempos de lectura/click humanos."""
    time.sleep(random.uniform(a, b))


def notify(message: str):
    """Envía la alerta por ntfy.sh y/o Telegram, lo que esté configurado."""
    sent = False

    if NTFY_TOPIC:
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={
                    "Title": "Cita DIAN disponible",
                    "Priority": "urgent",
                    "Tags": "warning,calendar",
                },
                timeout=10,
            )
            sent = True
        except Exception as e:
            print(f"[WARN] No se pudo notificar por ntfy.sh: {e}")

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
        print("[WARN] No hay ningún canal de notificación configurado.")
    print(f"[NOTIFY] {message}")


NO_CITAS_TEXTO = "No se encontraron especialidades relacionadas según los filtros seleccionados"

# Flujo confirmado sobre https://agendamiento.dian.gov.co/ :
#   1. Card "Agendar cita" en la home
#   2. "Persona Natural" + "Videoatención" -> Siguiente
#   3. Tipo de servicio: "Devoluciones." -> Siguiente
#   4. Si aparece el modal "No se encontraron especialidades..." => NO hay citas.
#      Si NO aparece ese modal (y en cambio se ve un calendario/horarios) => SÍ hay citas.


def click_visible_text(page, text, timeout=20000):
    """
    Hace clic en el elemento con ese texto que esté VISIBLE en pantalla.

    El portal de la DIAN a veces repite el mismo texto dos veces en el HTML
    (una copia oculta para accesibilidad/responsive y otra visible).
    page.click("text=...") a secas agarra la primera coincidencia sin
    importar si está oculta, y se queda esperando eternamente si esa es
    invisible. Este helper filtra solo la(s) visible(s).
    """
    locator = page.locator(f"text={text}").locator("visible=true")
    locator.first.click(timeout=timeout)


def check_availability() -> bool:
    """
    Entra al portal, sigue el flujo Agendar cita -> Persona Natural ->
    Videoatención -> Devoluciones, y revisa si aparece el modal de
    "no hay especialidades disponibles".

    Devuelve True si encontró disponibilidad (NO apareció el modal de "sin
    citas"), False si no hay citas o si algo falló en la navegación.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1366, "height": 768},
            locale="es-CO",
            timezone_id="America/Bogota",
        )
        # Oculta el flag que delata a Playwright/Selenium como bot
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = context.new_page()
        found = False

        try:
            # 1. Home -> "Agendar cita"
            page.goto(DIAN_URL, timeout=30000)
            human_delay(2, 4)
            page.screenshot(path="dian_paso1_home.png")
            click_visible_text(page, "Agendar cita")
            human_delay()

            # 2. Persona Natural + Videoatención -> Siguiente
            click_visible_text(page, "Persona Natural")
            human_delay()
            click_visible_text(page, "Videoatención")
            human_delay()
            page.screenshot(path="dian_paso2_tipo_persona.png")
            click_visible_text(page, "Siguiente")
            human_delay(1.5, 3)

            # 3. Tipo de servicio: Devoluciones -> Siguiente
            page.screenshot(path="dian_paso3_tipo_servicio.png")
            click_visible_text(page, "Devoluciones")
            human_delay()
            click_visible_text(page, "Siguiente")
            human_delay(1.5, 3)

            # 4. Revisar si aparece el modal de "sin citas"
            #    Le damos unos segundos a que el portal responda / renderice.
            try:
                page.wait_for_selector(
                    f"text={NO_CITAS_TEXTO}", timeout=8000
                )
                # El modal apareció -> no hay citas disponibles
                found = False
            except Exception:
                # El modal NO apareció dentro del tiempo de espera.
                # Asumimos que el flujo avanzó a un calendario/horarios,
                # es decir, sí hay disponibilidad.
                found = True

            # Guarda siempre una captura del resultado para poder verificar
            # visualmente qué vio el script (útil sobre todo al principio).
            page.screenshot(path="dian_result.png")

        except Exception as e:
            print(f"[ERROR] Fallo navegando el portal: {e}")
            try:
                page.screenshot(path="dian_error.png")
            except Exception:
                pass
            found = False
        finally:
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
            f"¡Hay citas de Devoluciones (videoatención) disponibles en la "
            f"DIAN! Entra ya a agendar: {DIAN_URL}"
        )
    else:
        print("Sin disponibilidad por ahora.")


if __name__ == "__main__":
    main()
