#!/usr/bin/env python3
"""
read_weight.py

Conecta a uma balança BLE e imprime leituras de peso.

Uso:
  - Para conectar a um endereço específico:
      python read_weight.py --address 80:F4:AD:DD:37:9A
  - Para escanear por um prefixo e conectar ao primeiro encontrado:
      python read_weight.py --prefix 80:F4:AD:DD:37

Requisitos: bleak (pip install bleak)

Observações:
  - A característica padrão de Weight Measurement tem UUID 00002a9d-0000-1000-8000-00805f9b34fb.
  - Algumas balanças BLE exigem emparelhamento via Configurações do Windows antes de acessar GATT.
"""
import asyncio
import argparse
import platform
import sys
from typing import Optional

from bleak import BleakScanner, BleakClient

WEIGHT_CHAR = "00002a9d-0000-1000-8000-00805f9b34fb"


def parse_weight(data: bytearray):
    """Interpreta payload da característica Weight Measurement (0x2A9D).

    Spec simplificada:
      Flags (1 byte)
      Weight (uint16) - unidades dependem do flag bit0
    """
    if len(data) < 3:
        return None, None, data

    flags = data[0]
    unit_imperial = bool(flags & 0x01)
    raw = int.from_bytes(data[1:3], byteorder="little", signed=False)
    if unit_imperial:
        weight = raw * 0.01  # lb
        unit = "lb"
    else:
        weight = raw * 0.005  # kg
        unit = "kg"
    return weight, unit, data


async def scan_for_prefix(prefix: str, timeout: int = 10) -> Optional[str]:
    print(f"Escaneando por dispositivos com prefixo '{prefix}' por {timeout}s...")
    devices = await BleakScanner.discover(timeout=timeout)
    for d in devices:
        addr = d.address.replace("-", ":")
        if addr.upper().startswith(prefix.upper()):
            print(f"Encontrado: {d.name} -> {addr}")
            return addr
    return None


async def list_services(address: str):
    print(f"Conectando para listar serviços: {address}...")
    async with BleakClient(address) as client:
        svcs = await client.get_services()
        for s in svcs:
            print(f"Service {s.uuid}: {s.description}")
            for c in s.characteristics:
                props = ",".join(c.properties)
                print(f"  Char {c.uuid} ({props})")


async def run(address: str, duration: int = 300):
    print(f"Tentando conectar em {address}...")
    try:
        async with BleakClient(address) as client:
            if not client.is_connected:
                print("Falha ao conectar.")
                return

            print("Conectado. Procurando característica de peso...")
            if WEIGHT_CHAR in [c.uuid for s in await client.get_services() for c in s.characteristics]:
                print("Characteristic Weight Measurement encontrada. Subscribing...")

                def callback(sender, data):
                    weight, unit, raw = parse_weight(bytearray(data))
                    if weight is None:
                        print(f"Dados inválidos: {raw}")
                    else:
                        print(f"Peso: {weight:.3f} {unit}")

                await client.start_notify(WEIGHT_CHAR, callback)
                print(f"Lendo por {duration} segundos (CTRL+C para parar)...")
                try:
                    await asyncio.sleep(duration)
                except asyncio.CancelledError:
                    pass
                await client.stop_notify(WEIGHT_CHAR)
            else:
                print("Característica padrão não encontrada. Listando serviços/characterísticas:")
                await list_services(address)
    except Exception as e:
        print(f"Erro durante conexão/leitura: {e}")


def ensure_python_platform():
    if platform.system() != "Windows":
        return
    # No-op for now; warn user about possible pairing requirements


def main():
    parser = argparse.ArgumentParser(description="Ler pesos de uma balança BLE")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--address", help="Endereço/UUID do dispositivo (ex: 80:F4:AD:DD:37:9A)")
    group.add_argument("--prefix", help="Prefixo do endereço (ex: 80:F4:AD:DD:37)")
    parser.add_argument("--scan-time", type=int, default=10, help="Tempo de scan em segundos ao usar --prefix")
    parser.add_argument("--duration", type=int, default=300, help="Tempo em segundos para manter leitura (default 300s)")

    args = parser.parse_args()
    ensure_python_platform()

    address = args.address

    async def _wrapper():
        nonlocal address
        if args.prefix:
            found = await scan_for_prefix(args.prefix, timeout=args.scan_time)
            if not found:
                print("Nenhum dispositivo com esse prefixo encontrado.")
                return
            address = found

        await run(address, duration=args.duration)

    try:
        asyncio.run(_wrapper())
    except KeyboardInterrupt:
        print("Interrompido pelo usuário")


if __name__ == "__main__":
    main()
