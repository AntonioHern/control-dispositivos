import asyncio
import time
import threading
import winsound
from bleak import BleakScanner
import tkinter as tk

import pystray
from pystray import Menu as TrayMenu, MenuItem as TrayItem
from PIL import Image, ImageDraw
from winotify import Notification, audio

import os
import json
from pathlib import Path
import sys

# ------------------------------------------------------------------------------------
# CONFIGURACIÓN
# ------------------------------------------------------------------------------------
# Puedes poner aquí:
#   - nombre (o parte): "Holy-IOT"
#   - o MAC completa:   "AA:BB:CC:DD:EE:FF"
TARGET_NAME = "DD:B2:82:4A:58:6D"

# Umbral base (se puede cambiar desde el menú de la bandeja)
RSSI_THRESHOLD = -90

# Histéresis en dB (diferencia entre ir a FAR y volver a NEAR)
HYST_DB = 5

# Estos se recalculan a partir de RSSI_THRESHOLD + HYST_DB
FAR_THRESHOLD = RSSI_THRESHOLD              # por debajo -> FAR
NEAR_THRESHOLD = RSSI_THRESHOLD + HYST_DB   # por encima -> NEAR

LOSS_TIMEOUT = 8          # segundos sin señal para considerar "perdido"
ALERT_INTERVAL = 5        # segundos entre alertas cuando está lejos/perdido

# EMA (media exponencial) para suavizar RSSI
ALPHA = 0.3               # 0..1 (más alto = más rápido, menos suavizado)

# Opciones de umbral base que se podrán elegir desde el menú
THRESHOLD_OPTIONS = [-20, -30, -40, -50, -60, -70, -80, -90, -100]

CONFIG_DIR = Path(os.getenv("APPDATA", ".")) / "ble_monitor"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Estado
rssi_smooth = None
last_seen = None
state = "unknown"
last_alert = 0

tray_icon = None

# Tkinter
tk_root = None
alert_window = None

# Flags de comportamiento
ALERTS_ENABLED = True              # alertas periódicas (toasts + beep)
FULLSCREEN_ALERT_ENABLED = True    # ventana roja a pantalla completa

# Beep cooldown
last_beep = 0
BEEP_INTERVAL = 15  # segundos entre pitidos

# Info para tooltip
current_rssi_str = "RSSI: N/A"


# ------------------------------------------------------------------------------------
# CONFIG: CARGA / GUARDA UMBRAL + HISTÉRESIS
# ------------------------------------------------------------------------------------
def recalc_thresholds():
    global FAR_THRESHOLD, NEAR_THRESHOLD
    FAR_THRESHOLD = RSSI_THRESHOLD
    NEAR_THRESHOLD = RSSI_THRESHOLD + HYST_DB
    print(f"[CONFIG] Umbrales con histéresis: FAR={FAR_THRESHOLD}, NEAR={NEAR_THRESHOLD}")


def load_config():
    global RSSI_THRESHOLD
    try:
        if CONFIG_FILE.exists():
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if "rssi_threshold" in data:
                RSSI_THRESHOLD = int(data["rssi_threshold"])
                print(f"[CONFIG] Umbral RSSI cargado: {RSSI_THRESHOLD}")
        # recalcular siempre (tenga o no config)
        recalc_thresholds()
    except Exception as e:
        print(f"[CONFIG] Error cargando configuración: {e}")
        recalc_thresholds()


def save_config():
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {"rssi_threshold": RSSI_THRESHOLD}
        with CONFIG_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f)
        print(f"[CONFIG] Umbral RSSI guardado: {RSSI_THRESHOLD}")
    except Exception as e:
        print(f"[CONFIG] Error guardando configuración: {e}")


# ------------------------------------------------------------------------------------
# ICONOS PARA LA BANDEJA
# ------------------------------------------------------------------------------------
def icon_circle(color):
    img = Image.new("RGB", (64, 64), color=(0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=color)
    return img


ICON_GREEN = icon_circle("green")
ICON_YELLOW = icon_circle("yellow")
ICON_RED = icon_circle("red")


# ------------------------------------------------------------------------------------
# NOTIFICACIONES (CON EMOJIS)
# ------------------------------------------------------------------------------------
def notify(kind: str, title: str, message: str):
    """
    kind: 'near', 'far', 'lost', 'alert', 'info'...
    Se añade un emoji al título según el tipo de aviso.
    """
    icons = {
        "near":  "🟢 ",
        "far":   "🟡 ",
        "lost":  "🔴 ",
        "alert": "⚠️ ",
        "info":  "ℹ️ ",
    }

    prefix = icons.get(kind, "")
    n = Notification(
        app_id="BLE Monitor",
        title=prefix + title,
        msg=message
    )
    n.set_audio(audio.Default, loop=False)
    n.show()


# ------------------------------------------------------------------------------------
# LÓGICA BLE
# ------------------------------------------------------------------------------------
def play_alert():
    """
    Beep con cooldown propio, para no estar pitando cada ALERT_INTERVAL.
    """
    global last_beep
    now = time.time()
    if now - last_beep < BEEP_INTERVAL:
        return

    last_beep = now
    try:
        winsound.Beep(1000, 300)
    except Exception:
        pass


def smooth_rssi(value):
    """
    Media exponencial (EMA) para suavizar las lecturas de RSSI.
    """
    global rssi_smooth
    if rssi_smooth is None:
        rssi_smooth = value
    else:
        rssi_smooth = ALPHA * value + (1 - ALPHA) * rssi_smooth
    return rssi_smooth


def is_target_device(device) -> bool:
    """
    Devuelve True si el dispositivo coincide con TARGET_NAME, ya sea por nombre o por MAC.
    - Si TARGET_NAME contiene ':' o '-', se interpreta como MAC completa.
    - Si no, se interpreta como nombre (substring en device.name).
    """
    if not TARGET_NAME:
        return False

    target = TARGET_NAME.strip().lower()
    dev_name = (device.name or "").lower()
    dev_addr = (getattr(device, "address", "") or "").lower()

    # Si tiene ":" o "-", lo tratamos como dirección MAC
    if ":" in target or "-" in target:
        # Normalizamos ambos quitando guiones y mayúsculas
        t_norm = target.replace("-", "").replace(":", "")
        a_norm = dev_addr.replace("-", "").replace(":", "")
        return t_norm == a_norm

    # Si no es una MAC, lo tratamos como nombre parcial
    if target in dev_name:
        return True

    # Por si el usuario pone directamente la dirección pero sin :
    t_norm = target.replace("-", "").replace(":", "")
    a_norm = dev_addr.replace("-", "").replace(":", "")
    return t_norm and t_norm == a_norm


def detection_callback(device, advertisement_data):
    global last_seen, state, tray_icon, current_rssi_str

    if is_target_device(device):
        rssi = advertisement_data.rssi
        last_seen = time.time()
        rssi_avg = smooth_rssi(rssi)

        current_rssi_str = f"RSSI: {rssi_avg:.1f} dBm"

        print(
            f"Detectado | nombre={device.name} "
            f"addr={getattr(device, 'address', 'N/A')} "
            f"RSSI: {rssi_avg:.1f} "
            f"(base={RSSI_THRESHOLD}, FAR={FAR_THRESHOLD}, NEAR={NEAR_THRESHOLD})"
        )

        # Actualizar tooltip del icono
        if tray_icon:
            tray_icon.title = f"BLE Monitor - {current_rssi_str}"

        # Histéresis:
        # - Si la señal cae por debajo de FAR_THRESHOLD -> FAR
        # - Si la señal sube por encima de NEAR_THRESHOLD -> NEAR
        if rssi_avg < FAR_THRESHOLD and state != "far":
            state = "far"
            notify("far", "El dispositivo se aleja", "Se detecta señal débil.")
            if tray_icon:
                tray_icon.icon = ICON_YELLOW

        elif rssi_avg > NEAR_THRESHOLD and state != "near":
            state = "near"
            notify("near", "Dispositivo cercano", "Se detecta buena señal.")
            if tray_icon:
                tray_icon.icon = ICON_GREEN
            hide_red_alert()  # si se recupera la señal, cerramos alerta roja


async def monitor_ble():
    global state, last_alert, tray_icon, last_seen

    scanner = BleakScanner(detection_callback)
    await scanner.start()

    try:
        while True:
            now = time.time()

            # ¿Hemos perdido la señal? (solo si alguna vez se ha visto el dispositivo)
            if last_seen is not None and now - last_seen > LOSS_TIMEOUT:
                if state != "lost":
                    state = "lost"
                    notify("lost", "Señal perdida", "No se reciben datos del dispositivo.")
                    if tray_icon:
                        tray_icon.icon = ICON_RED
                    show_red_alert()

            # Alertas periódicas cuando está lejos o perdido
            if ALERTS_ENABLED and state in ("far", "lost"):
                if now - last_alert >= ALERT_INTERVAL:
                    if state == "far":
                        notify("alert", "Alerta", "El dispositivo está lejos.")
                    else:
                        notify("alert", "Alerta", "SIN señal del dispositivo.")
                    play_alert()
                    last_alert = now

            await asyncio.sleep(1)

    finally:
        await scanner.stop()


def run_ble_in_mta():
    """
    Ejecuta asyncio + Bleak en un hilo MTA separado.
    """
    asyncio.set_event_loop(asyncio.new_event_loop())
    loop = asyncio.get_event_loop()
    loop.run_until_complete(monitor_ble())


# ------------------------------------------------------------------------------------
# MENÚ: CAMBIO DE UMBRAL Y OPCIONES DESDE LA BANDEJA
# ------------------------------------------------------------------------------------
def make_set_threshold_callback(value):
    def inner(icon, item):
        global RSSI_THRESHOLD
        RSSI_THRESHOLD = value
        recalc_thresholds()
        save_config()
        notify(
            "info",
            "Umbral RSSI actualizado",
            f"Umbral base: {RSSI_THRESHOLD} dBm\n"
            f"FAR: {FAR_THRESHOLD} dBm | NEAR: {NEAR_THRESHOLD} dBm"
        )
    return inner


def make_is_current_callback(value):
    def inner(item):
        return RSSI_THRESHOLD == value
    return inner


def build_threshold_submenu():
    """
    Construye el submenú de 'Umbral RSSI' con opciones tipo radio.
    """
    items = []
    for val in THRESHOLD_OPTIONS:
        items.append(
            TrayItem(
                f"{val} dBm",
                make_set_threshold_callback(val),
                checked=make_is_current_callback(val),
                radio=True
            )
        )
    return TrayMenu(*items)


# Flags de menú
def toggle_alerts(icon, item):
    global ALERTS_ENABLED
    ALERTS_ENABLED = not ALERTS_ENABLED


def alerts_checked(item):
    return ALERTS_ENABLED


def toggle_fullscreen_alert(icon, item):
    global FULLSCREEN_ALERT_ENABLED
    FULLSCREEN_ALERT_ENABLED = not FULLSCREEN_ALERT_ENABLED


def fullscreen_checked(item):
    return FULLSCREEN_ALERT_ENABLED


# ------------------------------------------------------------------------------------
# BANDEJA DEL SISTEMA
# ------------------------------------------------------------------------------------
def on_exit(icon, item):
    # Parar icono y cerrar ventana Tk
    icon.stop()
    if tk_root is not None:
        tk_root.after(0, tk_root.quit)
    sys.exit(0)


def setup_tray():
    global tray_icon

    menu = TrayMenu(
        TrayItem("Umbral RSSI", build_threshold_submenu()),
        TrayItem("Alertas periódicas", toggle_alerts, checked=alerts_checked),
        TrayItem("Alerta pantalla completa", toggle_fullscreen_alert, checked=fullscreen_checked),
        TrayItem("Salir", on_exit)
    )

    tray_icon = pystray.Icon(
        "ble_monitor",
        ICON_YELLOW,
        "BLE Monitor - " + current_rssi_str,
        menu=menu
    )
    # No bloquea el hilo actual (el principal), corre en otro hilo
    tray_icon.run_detached()


# ------------------------------------------------------------------------------------
# VENTANA ROJA DE ALERTA (TKINTER, SIEMPRE EN HILO PRINCIPAL)
# ------------------------------------------------------------------------------------
def _show_red_alert_impl():
    global alert_window
    if alert_window is not None:
        return  # ya está abierta

    alert_window = tk.Toplevel(tk_root)
    alert_window.title("ALERTA - SIN SEÑAL")
    alert_window.attributes("-topmost", True)
    alert_window.attributes("-fullscreen", True)
    alert_window.configure(bg="red")

    label = tk.Label(
        alert_window,
        text="⚠️  ¡SIN SEÑAL DEL DISPOSITIVO!  ⚠️",
        font=("Arial", 60, "bold"),
        fg="white",
        bg="red"
    )
    label.pack(expand=True)

    # Cerrar con cualquier tecla o clic
    alert_window.bind("<Key>", lambda e: hide_red_alert())
    alert_window.bind("<Button>", lambda e: hide_red_alert())


def _hide_red_alert_impl():
    global alert_window
    if alert_window is not None:
        try:
            alert_window.destroy()
        except Exception:
            pass
        alert_window = None


def show_red_alert():
    """
    Llamable desde cualquier hilo. Programa la creación
    de la ventana roja en el hilo principal (Tk).
    """
    if not FULLSCREEN_ALERT_ENABLED:
        return
    if tk_root is not None:
        tk_root.after(0, _show_red_alert_impl)


def hide_red_alert():
    """
    Llamable desde cualquier hilo. Programa el cierre
    de la ventana roja en el hilo principal (Tk).
    """
    if tk_root is not None:
        tk_root.after(0, _hide_red_alert_impl)


# ------------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------------
if __name__ == "__main__":
    # Cargar configuración previa (si existe) y recalcular histéresis
    load_config()

    # Inicializar Tkinter en el hilo principal
    tk_root = tk.Tk()
    tk_root.withdraw()  # ocultamos la ventana principal; solo usaremos Toplevel para la alerta

    # HILO BLE (MTA)
    ble_thread = threading.Thread(target=run_ble_in_mta, daemon=True)
    ble_thread.start()

    # ICONO DE BANDEJA (en otro hilo, no bloquea)
    setup_tray()

    # Bucle principal de Tkinter (hilo principal)
    tk_root.mainloop()
