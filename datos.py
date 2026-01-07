import asyncio
from bleak import BleakScanner

async def scan():
    print("Escaneando anuncios BLE…")

    def callback(device, adv):
        if "holy-iot" in (device.name or "").lower():
            print("----")
            print("Nombre:", device.name)
            print("MAC:", device.address)
            print("RSSI:", adv.rssi)
            print("Manufacturer Data:", adv.manufacturer_data)
            print("Service Data:", adv.service_data)
            print("UUIDs:", adv.service_uuids)

    scanner = BleakScanner(callback)
    await scanner.start()
    await asyncio.sleep(10)
    await scanner.stop()

asyncio.run(scan())
