#!/usr/bin/env python3
"""
read_chars.py

Conecta a um dispositivo BLE e lê todas as características que suportam 'read'.

Uso:
  python read_chars.py --address FC:3C:D7:75:47:94

Requisitos: bleak (já listado em requirements.txt)
"""
import asyncio
import argparse
import platform
from typing import Optional

from bleak import BleakClient, BleakScanner


async def read_readable(address: str):
    print(f"Conectando a {address}...")
    async with BleakClient(address) as client:
        if not client.is_connected:
            print("Falha ao conectar")
            return

        # compat fallback
        try:
            services = await client.get_services()
        except AttributeError:
            services = getattr(client, "services", None)

        if not services:
            print("Não foi possível obter serviços/características")
            return

        for s in services:
            print(f"Service {s.uuid} {getattr(s, 'description', '')}")
            for c in s.characteristics:
                props = getattr(c, 'properties', [])
                print(f"  Char {c.uuid} props={props}")
                if 'read' in props:
                    try:
                        data = await client.read_gatt_char(c.uuid)
                        hexs = data.hex()
                        try:
                            txt = data.decode('utf-8')
                        except Exception:
                            txt = None
                        print(f"    -> read {len(data)} bytes: {hexs} {(' text='+txt) if txt else ''}")
                    except Exception as e:
                        print(f"    -> erro ao ler: {e}")


def main():
    parser = argparse.ArgumentParser(description='Ler características read do dispositivo BLE')
    parser.add_argument('--address', required=False, help='Endereço do dispositivo (ex: FC:3C:...)')
    parser.add_argument('--prefix', required=False, help='Prefixo para escanear e achar o primeiro endereço')
    parser.add_argument('--scan-time', type=int, default=8)
    args = parser.parse_args()

    address = args.address

    async def _wrap():
        nonlocal address
        if not address and args.prefix:
            print(f"Escaneando por prefixo {args.prefix}...")
            devices = await BleakScanner.discover(timeout=args.scan_time)
            for d in devices:
                if d.address.replace('-', ':').upper().startswith(args.prefix.upper()):
                    address = d.address.replace('-', ':')
                    print(f"Encontrado {d.name} -> {address}")
                    break

        if not address:
            print("Nenhum endereço informado nem encontrado com prefixo")
            return

        await read_readable(address)

    asyncio.run(_wrap())


if __name__ == '__main__':
    main()
