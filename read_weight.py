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


def heuristic_parse(data: bytearray):
    """Tenta interpretar bytes brutos comuns como peso (heurísticas).

    Retorna tuple (value, unit, info) onde value pode ser None se não for interpretável.
    """
    if not data or len(data) < 2:
        return None, None, data

    # try uint16 little-endian -> kg (0.005) or lb (0.01)
    raw16 = int.from_bytes(data[0:2], byteorder="little", signed=False)
    kg = raw16 * 0.005
    lb = raw16 * 0.01
    # prefer kg if value reasonable (< 500 kg)
    if 0 < kg < 500:
        return kg, "kg", {"raw16": raw16, "method": "uint16*0.005"}
    if 0 < lb < 1000:
        return lb, "lb", {"raw16": raw16, "method": "uint16*0.01"}

    # try uint32 little-endian
    if len(data) >= 4:
        raw32 = int.from_bytes(data[0:4], byteorder="little", signed=False)
        kg32 = raw32 * 0.001
        if 0 < kg32 < 500:
            return kg32, "kg", {"raw32": raw32, "method": "uint32*0.001"}

    return None, None, data


def find_weight_in_raw(data: bytearray):
    """Busca heurística por possíveis valores de peso dentro do payload bruto.

    Tenta varrer offsets procurando uint16/uint32 que, escalados, caiam numa faixa humana plausível.
    Retorna (weight, unit, details) ou (None, None, None).
    """
    if not data:
        return None, None, None

    # tenta uint16 em todos offsets
    for off in range(0, max(0, len(data) - 1)):
        raw16 = int.from_bytes(data[off:off+2], byteorder="little", signed=False)
        kg = raw16 * 0.005
        if 2.0 <= kg <= 300.0:
            return kg, "kg", {"offset": off, "method": "uint16*0.005", "raw16": raw16}
        lb = raw16 * 0.01
        if 5.0 <= lb <= 660.0:
            return lb, "lb", {"offset": off, "method": "uint16*0.01", "raw16": raw16}

    # tenta uint32
    for off in range(0, max(0, len(data) - 3)):
        raw32 = int.from_bytes(data[off:off+4], byteorder="little", signed=False)
        kg32 = raw32 * 0.001
        if 2.0 <= kg32 <= 300.0:
            return kg32, "kg", {"offset": off, "method": "uint32*0.001", "raw32": raw32}

    return None, None, None


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
        # compat layer: alguns backends do bleak expõem get_services(), outros têm .services
        try:
            svcs = await client.get_services()
        except AttributeError:
            svcs = getattr(client, "services", None)

        if not svcs:
            print("Não foi possível obter serviços do cliente Bleak. Talvez o backend não exponha serviços aqui.")
            return

        for s in svcs:
            desc = getattr(s, "description", "")
            print(f"Service {s.uuid}: {desc}")
            for c in s.characteristics:
                props = ",".join(getattr(c, "properties", []))
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
                    # obter serviços de forma compatível com diferentes backends do bleak
                    try:
                        services = await client.get_services()
                    except AttributeError:
                        services = getattr(client, "services", None)
                    service_uuids = [s.uuid for s in services]
                    characteristic_uuids = [c.uuid for s in services for c in s.characteristics]

                    # Prioridade 1: característica padrão 0x2A9D
                    chosen_char = None
                    if WEIGHT_CHAR in characteristic_uuids:
                        chosen_char = WEIGHT_CHAR

                    # Prioridade 2: característica -- caso o usuário deseje indicar via env/arg (handled earlier)

                    # Prioridade 3: vendor-specific notify (ex: 00002b10) ou qualquer característica com 'notify'
                    if not chosen_char:
                        # prefer explicit 2b10
                        for s in services:
                            if s.uuid.lower().startswith("00001910"):
                                for c in s.characteristics:
                                    if 'notify' in getattr(c, 'properties', []):
                                        chosen_char = c.uuid
                                        break
                            if chosen_char:
                                break

                    if not chosen_char:
                        # fallback: pick any characteristic with notify
                        for s in services:
                            for c in s.characteristics:
                                if 'notify' in getattr(c, 'properties', []):
                                    chosen_char = c.uuid
                                    break
                            if chosen_char:
                                break

                    # Se ainda não encontrou, tente ler características 'read' que possam conter dados
                    if not chosen_char:
                        print("Nenhuma característica notify encontrada; tentando ler características 'read' para possíveis dados de peso...")
                        for s in services:
                            for c in s.characteristics:
                                if 'read' in getattr(c, 'properties', []):
                                    try:
                                        data = await client.read_gatt_char(c.uuid)
                                        raw_hex = data.hex()
                                        print(f"Leu {c.uuid}: {raw_hex}")
                                        # try heuristic
                                        val, unit, info = heuristic_parse(bytearray(data))
                                        if val is not None:
                                            ts = datetime.datetime.utcnow().isoformat()
                                            print(f"HEURÍSTICA encontrou peso aproximado: {val:.3f} {unit} (char {c.uuid})")
                                            if csv_writer:
                                                csv_writer.writerow([ts, f"{val:.3f}", unit, raw_hex, ";".join(service_uuids) if csv_raw else ""]) 
                                                csv_file.flush()
                                            chosen_char = c.uuid
                                            break
                                    except Exception:
                                        pass
                            if chosen_char:
                                break

                    if not chosen_char:
                        print("Não foi possível identificar uma característica para ler/notificar.")
                        return

                    print(f"Usando característica {chosen_char} para receber dados (notificar/ler)")

                    disconnected = asyncio.Event()

                    def handle_disconnect():
                        print("Desconectado do dispositivo.")
                        disconnected.set()

                    # Nem todos os backends do Bleak expõem set_disconnected_callback (ex: winrt).
                    # Fazemos um fallback: se o método existir, usamos; senão, criamos uma tarefa
                    # que monitora client.is_connected.
                    monitor_task = None
                    try:
                        if hasattr(client, 'set_disconnected_callback'):
                            client.set_disconnected_callback(lambda _c: handle_disconnect())
                        else:
                            # cria tarefa que monitora a propriedade is_connected
                            async def _monitor():
                                try:
                                    while getattr(client, 'is_connected', False):
                                        await asyncio.sleep(0.5)
                                except Exception:
                                    pass
                                handle_disconnect()

                            monitor_task = asyncio.create_task(_monitor())
                    except Exception:
                        # se qualquer problema, garante que monitor será criado
                        async def _monitor_fallback():
                            try:
                                while getattr(client, 'is_connected', False):
                                    await asyncio.sleep(0.5)
                            except Exception:
                                pass
                            handle_disconnect()

                        monitor_task = asyncio.create_task(_monitor_fallback())

                    async def generic_handler(sender, data):
                        raw = bytearray(data)
                        raw_hex = bytes(raw).hex()
                        ts = datetime.datetime.utcnow().isoformat()
                        # tentar parser padrão
                        weight, unit, _ = parse_weight(raw)
                        info = None
                        if weight is None:
                            # heurística simples
                            weight, unit, info = heuristic_parse(raw)
                        if weight is None:
                            # varrer o payload em busca de uint16/uint32 plausíveis
                            weight, unit, info = find_weight_in_raw(raw)
                        if weight is None:
                            print(f"{ts} - Raw ({sender}): {raw_hex}")
                            if csv_writer:
                                csv_writer.writerow([ts, "", "", raw_hex, ";".join(service_uuids) if csv_raw else ""])
                                csv_file.flush()
                        else:
                            print(f"{ts} - Peso: {weight:.3f} {unit} (raw={raw_hex})")
                            if csv_writer:
                                if csv_raw:
                                    csv_writer.writerow([ts, f"{weight:.3f}", unit, raw_hex, ";".join(service_uuids)])
                                else:
                                    csv_writer.writerow([ts, f"{weight:.3f}", unit])
                                csv_file.flush()

                    # subscribe if notify available, otherwise perform periodic reads
                    # find characteristic object for chosen_char
                    chosen_char_obj = None
                    for s in services:
                        for c in s.characteristics:
                            if c.uuid == chosen_char:
                                chosen_char_obj = c
                                break
                        if chosen_char_obj:
                            break

                    if chosen_char_obj and 'notify' in getattr(chosen_char_obj, 'properties', []):
                        await client.start_notify(chosen_char, generic_handler)
                    else:
                        # periodic read loop until timeout or disconnect
                        while time.time() < end_time and not disconnected.is_set():
                            try:
                                data = await client.read_gatt_char(chosen_char)
                                await generic_handler(chosen_char, data)
                            except Exception as e:
                                print(f"Erro lendo characteristic {chosen_char}: {e}")
                                break
                            await asyncio.sleep(1)

                        # aguarda até desconexão ou até o tempo total expirar
                        remaining = end_time - time.time()
                        try:
                            await asyncio.wait_for(disconnected.wait(), timeout=remaining)
                            # Se chegamos aqui, desconectou antes do fim
                            # se usamos notify, pare a notificação
                            try:
                                await client.stop_notify(chosen_char)
                            except Exception:
                                pass
                            print("Notificações paradas após desconexão.")
                        except asyncio.TimeoutError:
                            # tempo esgotado - fim normal
                            try:
                                await client.stop_notify(chosen_char)
                            except Exception:
                                pass
                            print("Duração concluída; parando leitura.")
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
    parser.add_argument("--scan-and-connect", action="store_true", help="Escanear continuamente pelo prefixo e conectar automaticamente quando visto")
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

        # se pediu scan-and-connect, faça scans repetidos até encontrar o prefixo (ou expirar)
        if args.scan_and_connect:
            if not args.prefix and not address:
                parser.error('--scan-and-connect requires --prefix or --address')

            # se o usuário passou explicitamente um address, usa direto
            if not address and args.prefix:
                print(f"Modo scan-and-connect: procurando por prefixo {args.prefix} até conectar...")
                scan_deadline = time.time() + max(args.scan_time, 60)
                found = None
                while time.time() < scan_deadline and not found:
                    found = await scan_for_prefix(args.prefix, timeout=min(5, args.scan_time))
                    if found:
                        print(f"Dispositivo visto: {found}. Tentando conectar...")
                        address = found
                        break
                    await asyncio.sleep(1)

                if not found and not address:
                    print("Nenhum dispositivo com esse prefixo encontrado dentro do tempo permitido.")
                    return

        # comportamento existente: permitir --prefix único
        if args.prefix and not address:
            found = await scan_for_prefix(args.prefix, timeout=args.scan_time)
            if not found:
                print("Nenhum dispositivo com esse prefixo encontrado.")
                return
            address = found

        if not address:
            parser.error('one of the arguments --address --prefix is required unless --scan-only is used')

        await run(address, duration=args.duration, csv_path=csv_path, reconnect=reconnect, retry_interval=retry_interval, csv_raw=csv_raw)

    try:
        asyncio.run(_wrapper())
    except KeyboardInterrupt:
        print("Interrompido pelo usuário")


if __name__ == "__main__":
    main()
