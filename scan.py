import asyncio
from bleak import BleakScanner

# Tabla donde guardamos resultados del callback
found_devices = {}

def detection_callback(device, advertisement_data):
    # Guardamos el último RSSI del dispositivo
    found_devices[device.address] = {
        "name": device.name,
        "rssi": advertisement_data.rssi
    }

async def scan():
    print("🔍 Escaneando BLE durante 5 segundos…")

    # Crear escáner con callback
    scanner = BleakScanner(detection_callback)
    await scanner.start()

    # Escanear por 3 segundos
    await asyncio.sleep(10)

    # Detener el escáner
    await scanner.stop()

    print("\n📋 Dispositivos detectados:")
    if not found_devices:
        print("❌ No se detectó ningún dispositivo BLE.")
        return

    for addr, info in found_devices.items():
        print(f"{addr}   {info['name']}   RSSI: {info['rssi']} dBm")

asyncio.run(scan())
