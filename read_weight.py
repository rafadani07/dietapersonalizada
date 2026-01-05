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
import csv
import datetime
import time
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


async def run(address: str,
              duration: int = 300,
              csv_path: Optional[str] = None,
              reconnect: bool = False,
              retry_interval: int = 5,
              csv_raw: bool = False):
    """Tenta conectar e ler a característica de peso.

    Se reconnect for True, tenta reconectar até o tempo total expirar.
    """
    print(f"Tentando conectar em {address}...")

    end_time = time.time() + duration

    # Prepara CSV se solicitado
    csv_file = None
    csv_writer = None
    if csv_path:
        # abre em append, escreve header se arquivo novo
        need_header = True
        try:
            with open(csv_path, "r", encoding="utf-8"):
                need_header = False
        except FileNotFoundError:
            need_header = True

        csv_file = open(csv_path, "a", newline="", encoding="utf-8")
        csv_writer = csv.writer(csv_file)
        if need_header:
            if csv_raw:
                csv_writer.writerow(["timestamp_utc", "weight", "unit", "raw_hex", "services"])
            else:
                csv_writer.writerow(["timestamp_utc", "weight", "unit"])

    try:
        while time.time() < end_time:
            try:
                async with BleakClient(address) as client:
                    if not client.is_connected:
                        print("Falha ao conectar.")
                        raise Exception("failed to connect")

                    print("Conectado. Procurando característica de peso...")
                    services = await client.get_services()
                    service_uuids = [s.uuid for s in services]
                    characteristic_uuids = [c.uuid for s in services for c in s.characteristics]
                    if WEIGHT_CHAR in characteristic_uuids:
                        print("Characteristic Weight Measurement encontrada. Subscribing...")

                        disconnected = asyncio.Event()

                        def handle_disconnect(_client):
                            print("Desconectado do dispositivo.")
                            disconnected.set()

                        client.set_disconnected_callback(handle_disconnect)

                        def notification_handler(sender, data):
                            weight, unit, raw = parse_weight(bytearray(data))
                            ts = datetime.datetime.utcnow().isoformat()
                            raw_hex = bytes(raw).hex() if raw is not None else ""
                            services_summary = ";".join(service_uuids) if csv_raw else ""
                            if weight is None:
                                print(f"Dados inválidos: {raw}")
                                if csv_writer:
                                    if csv_raw:
                                        csv_writer.writerow([ts, "", "", raw_hex, services_summary])
                                    else:
                                        csv_writer.writerow([ts, "", ""])
                                    csv_file.flush()
                            else:
                                print(f"{ts} - Peso: {weight:.3f} {unit}")
                                if csv_writer:
                                    if csv_raw:
                                        csv_writer.writerow([ts, f"{weight:.3f}", unit, raw_hex, services_summary])
                                    else:
                                        csv_writer.writerow([ts, f"{weight:.3f}", unit])
                                    csv_file.flush()

                        await client.start_notify(WEIGHT_CHAR, notification_handler)

                        # aguarda até desconexão ou até o tempo total expirar
                        remaining = end_time - time.time()
                        try:
                            await asyncio.wait_for(disconnected.wait(), timeout=remaining)
                            # Se chegamos aqui, desconectou antes do fim
                            await client.stop_notify(WEIGHT_CHAR)
                            print("Notificações paradas após desconexão.")
                        except asyncio.TimeoutError:
                            # tempo esgotado - fim normal
                            await client.stop_notify(WEIGHT_CHAR)
                            print("Duração concluída; parando leitura.")
                            return
                    else:
                        print("Característica padrão não encontrada. Listando serviços/characterísticas:")
                        await list_services(address)
                        return
            except Exception as e:
                print(f"Erro durante conexão/leitura: {e}")

            # Se não pedir reconectar, sai
            if not reconnect:
                break

            # tempo restante para tentativa de reconexão
            remaining_time = end_time - time.time()
            if remaining_time <= 0:
                break
            print(f"Tentando reconectar em {retry_interval}s... (tempo restante {int(remaining_time)}s)")
            await asyncio.sleep(retry_interval)
    finally:
        if csv_file:
            csv_file.close()


def ensure_python_platform():
    if platform.system() != "Windows":
        return
    # No-op for now; warn user about possible pairing requirements


def main():
    parser = argparse.ArgumentParser(description="Ler pesos de uma balança BLE")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--address", help="Endereço/UUID do dispositivo (ex: 80:F4:AD:DD:37:9A)")
    group.add_argument("--prefix", help="Prefixo do endereço (ex: 80:F4:AD:DD:37)")
    parser.add_argument("--scan-only", action="store_true", help="Apenas escanear e listar dispositivos BLE detectados e sair")
    parser.add_argument("--scan-time", type=int, default=10, help="Tempo de scan em segundos ao usar --prefix")
    parser.add_argument("--duration", type=int, default=300, help="Tempo em segundos para manter leitura (default 300s)")
    parser.add_argument("--csv", dest="csv", help="Caminho CSV para salvar leituras")
    parser.add_argument("--csv-raw", action="store_true", help="Incluir raw bytes hex e serviços detectados no CSV")
    parser.add_argument("--reconnect", action="store_true", help="Tentar reconectar automaticamente durante --duration")
    parser.add_argument("--retry-interval", type=int, default=5, help="Segundos entre tentativas de reconexão")

    args = parser.parse_args()
    ensure_python_platform()

    # permitir --scan-only sem --address/--prefix
    if not args.scan_only and not (args.address or args.prefix):
        parser.error('one of the arguments --address --prefix is required unless --scan-only is used')

    address = args.address
    csv_path = args.csv
    csv_raw = args.csv_raw
    reconnect = args.reconnect
    retry_interval = args.retry_interval

    async def _wrapper():
        nonlocal address
        # scan-only: lista todos os dispositivos detectados e sai
        if args.scan_only:
            print(f"Escaneando ({args.scan_time}s) e listando todos dispositivos BLE...")
            devices = await BleakScanner.discover(timeout=args.scan_time)
            if not devices:
                print("Nenhum dispositivo detectado.")
                return
            for d in devices:
                name = d.name or "<sem nome>"
                addr = d.address
                rssi = getattr(d, 'rssi', '')
                print(f"{name} | {addr} | RSSI={rssi}")
            return
        if args.prefix:
            found = await scan_for_prefix(args.prefix, timeout=args.scan_time)
            if not found:
                print("Nenhum dispositivo com esse prefixo encontrado.")
                return
            address = found

        await run(address, duration=args.duration, csv_path=csv_path, reconnect=reconnect, retry_interval=retry_interval, csv_raw=csv_raw)

    try:
        asyncio.run(_wrapper())
    except KeyboardInterrupt:
        print("Interrompido pelo usuário")


if __name__ == "__main__":
    main()
