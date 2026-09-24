# Monitor de citas DIAN

Revisa automáticamente el portal `https://agendamiento.dian.gov.co/` y te
avisa al celular (push, y opcionalmente Telegram) apenas aparezca un cupo.

## Por qué está armado así

- **Corre en GitHub Actions**, no en tu computador. No importa si tu PC está
  apagado: se ejecuta cada 30 min en la nube, gratis (el plan gratuito de
  GitHub da 2.000 minutos/mes, esto consume una fracción mínima).
- **Playwright con "modo sigiloso"**: navegador real (Chromium), user-agent
  normal, pausas aleatorias entre acciones, sin el flag que delata a los bots
  automatizados. Aun así, ningún script elimina el riesgo de un captcha o
  bloqueo — por eso el objetivo es *avisarte*, no agendar solo.
- **Notificación por ntfy.sh**: llega como notificación push directa a tu
  celular, sin necesidad de crear un bot ni cuentas de desarrollador. Si
  prefieres Telegram, también lo soporta (ver abajo).

## Paso 1 — Flujo que sigue el script (ya confirmado)

El script ya sigue el flujo real del portal, tal como lo indicaste:

1. Entra a `https://agendamiento.dian.gov.co/`
2. Click en la tarjeta **"Agendar cita"**
3. Selecciona **Persona Natural** + **Videoatención** → Siguiente
4. Selecciona el servicio **Devoluciones** → Siguiente
5. Si aparece el modal *"No se encontraron especialidades relacionadas
   según los filtros seleccionados"* → **no hay citas**.
   Si ese modal **no aparece** (avanzó a un calendario/horarios) →
   **sí hay citas**, y te llega la notificación.

Cada corrida guarda una captura (`dian_result.png`) con lo que vio el
script justo después del paso 4, para que puedas verificar visualmente que
el flujo sigue funcionando igual (la DIAN puede cambiar el sitio sin
aviso). En GitHub Actions esa captura queda disponible como "artifact" de
cada ejecución, en la pestaña **Actions → (la corrida) → Artifacts**.

Si en algún momento la DIAN cambia el texto de los botones o el mensaje
del modal, el script dejará de encontrar esos textos y lo vas a ver
reflejado como error — en ese caso avísame y ajustamos los textos.

## Paso 2 — Crear tu canal de notificación (ntfy.sh)

1. Instala la app **ntfy** en tu celular (Android/iOS, gratis).
2. Dentro de la app, suscríbete a un "topic" (nombre) único e improbable de
   adivinar, ej: `dian-carlos-9f3a21`. Cualquiera puede usar ese mismo
   nombre y ver tus alertas si lo adivina, así que que no sea algo obvio.
3. Ese nombre es el valor de `NTFY_TOPIC` que vas a configurar abajo.

(Alternativa: si prefieres Telegram, crea un bot con **@BotFather**, obtén
el `TELEGRAM_BOT_TOKEN`, y tu `TELEGRAM_CHAT_ID` hablándole a
**@userinfobot**.)

## Paso 3 — Subir esto a GitHub y configurarlo

1. Crea un repositorio **privado** en GitHub y sube esta carpeta.
2. Ve a **Settings → Secrets and variables → Actions**:
   - En **Secrets**, agrega:
     - `NTFY_TOPIC` = el nombre que elegiste
     - (opcional) `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
3. El workflow (`.github/workflows/check-dian.yml`) ya queda corriendo solo
   cada 30 minutos. También puedes lanzarlo manualmente desde la pestaña
   **Actions → Monitor citas DIAN → Run workflow**.

## Probarlo localmente y VER el navegador en tiempo real

Esto es lo más útil para depurar: corre el mismo script en tu computador
pero con una ventana de Chrome visible, para que veas exactamente dónde
hace clic el script paso a paso (en GitHub Actions esto no se puede ver,
solo queda el texto de log y las capturas).

```bash
pip install -r requirements.txt
playwright install chromium

export DIAN_HEADLESS=false
export NTFY_TOPIC="dian-carlos-9f3a21"

python check_dian.py
```

Se te va a abrir una ventana de Chrome sola (no la toques, no le hagas
clic tú) y vas a ver cómo navega el portal solo. Al final la deja abierta
15 segundos antes de cerrarla, para que revises el resultado.

Cuando ya confirmes que funciona bien así, para dejarlo corriendo
desatendido en GitHub Actions no necesitas tocar nada — ahí siempre corre
en modo invisible (`DIAN_HEADLESS` no está definido en el workflow, así
que usa el valor por defecto `true`).

## Sobre el riesgo de bloqueo

- No bajes el intervalo de 20-30 minutos. Consultas muy frecuentes son la
  forma más común de que un sitio bloquee la IP.
- GitHub Actions usa IPs compartidas; si la DIAN llegara a bloquearlas
  específicamente, la alternativa es correr esto en un VPS pequeño
  (ej. DigitalOcean/Contabo, ~5 USD/mes) en vez de GitHub Actions — el
  script (`check_dian.py`) es el mismo, solo cambia dónde se ejecuta.
- Este es un trámite gratuito y público; el script solo *consulta*
  disponibilidad, no agenda nada automáticamente ni evade ningún pago.
