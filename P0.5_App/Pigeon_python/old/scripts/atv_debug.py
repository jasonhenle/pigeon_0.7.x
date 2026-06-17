"""Small helper to debug Apple TV discovery/now-playing under Python 3.14.

`pyatv`'s bundled CLI (`atvremote`) may crash on Python 3.14 due to event-loop API changes.
This script avoids that by creating its own event loop.

Usage:
  ./.venv/bin/python atv_debug.py scan
  ./.venv/bin/python atv_debug.py playing --id <identifier>
  ./.venv/bin/python atv_debug.py playing --host <ip>
  ./.venv/bin/python atv_debug.py metadata --id <identifier>
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path


STATE_DIR = Path.home() / ".pigeon_0_5"
CREDENTIALS_FILE = STATE_DIR / "pyatv_credentials"


def _protocol_name(protocol) -> str:
    if protocol is None:
        return "automatic"
    try:
        return str(getattr(protocol, "name", protocol)).lower()
    except Exception:
        return str(protocol).lower()


def _parse_protocol(name: str | None):
    if not name:
        return None
    norm = name.strip().lower()
    if norm in ("auto", "automatic"):
        return None
    from pyatv.const import Protocol

    lookup = {
        "mrp": Protocol.MRP,
        "companion": Protocol.Companion,
        "airplay": Protocol.AirPlay,
        "raop": Protocol.RAOP,
    }
    if norm not in lookup:
        raise SystemExit(f"Unknown protocol: {name}")
    return lookup[norm]


def _print_playing_fields(playing) -> None:
    keys = (
        "device_state",
        "media_type",
        "app",
        "title",
        "series_name",
        "season_number",
        "episode_number",
        "album",
        "artist",
        "genre",
        "position",
        "total_time",
        "shuffle",
        "repeat",
        "hash",
    )
    found = False
    for key in keys:
        value = getattr(playing, key, None)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        found = True
        print(f"  {key}: {text}")
    if not found:
        print("  no fields")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.stop()
        except Exception:
            pass
        loop.close()


async def _scan(timeout_s: int) -> int:
    import pyatv
    from pyatv.storage.file_storage import FileStorage

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    storage = FileStorage(str(CREDENTIALS_FILE), loop)
    atvs = await pyatv.scan(loop, timeout=timeout_s, storage=storage)
    if not atvs:
        print("No Apple TVs found.")
        return 2
    for conf in atvs:
        print(f"Name: {conf.name}")
        print(f"Address: {conf.address}")
        print(f"Identifier: {conf.identifier}")
        try:
            services = getattr(conf, "services", []) or []
            if services:
                print("Services:")
                for svc in services:
                    print(f" - {svc.protocol} pairing={svc.pairing} enabled={getattr(svc,'enabled',None)}")
        except Exception:
            pass
        print("-" * 40)
    return 0


async def _playing(*, identifier: str | None, host: str | None, timeout_s: int) -> int:
    import pyatv
    from pyatv.storage.file_storage import FileStorage

    if not identifier and not host:
        raise SystemExit("Provide --id or --host")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    storage = FileStorage(str(CREDENTIALS_FILE), loop)

    if host:
        atvs = await pyatv.scan(loop, timeout=timeout_s, storage=storage, hosts=[host])
    else:
        atvs = await pyatv.scan(loop, timeout=timeout_s, storage=storage, identifier=identifier)

    if not atvs:
        print("No Apple TV matched.")
        return 2

    conf = atvs[0]
    protocol = _parse_protocol("mrp")
    atv = await pyatv.connect(conf, loop, protocol=protocol, storage=storage)
    try:
        playing = await atv.metadata.playing()
        fields = {}
        for k in ("device_state", "media_type", "app", "title", "series_name", "album", "artist"):
            v = getattr(playing, k, None)
            if v is not None and str(v).strip():
                fields[k] = str(v).strip()
        print("Playing:", ", ".join([f"{k}={v}" for k, v in fields.items()]) or "no fields")
        return 0
    finally:
        await atv.close()


async def _metadata(
    *,
    identifier: str | None,
    host: str | None,
    timeout_s: int,
    protocol_name: str | None,
    tries: int,
    delay_s: float,
) -> int:
    import pyatv
    from pyatv.const import Protocol
    from pyatv.storage.file_storage import FileStorage

    if not identifier and not host:
        raise SystemExit("Provide --id or --host")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    storage = FileStorage(str(CREDENTIALS_FILE), loop)

    if host:
        atvs = await pyatv.scan(loop, timeout=timeout_s, storage=storage, hosts=[host])
    else:
        atvs = await pyatv.scan(loop, timeout=timeout_s, storage=storage, identifier=identifier)

    if not atvs:
        print("No Apple TV matched.")
        return 2

    conf = atvs[0]
    services = getattr(conf, "services", []) or []
    print(f"Name: {getattr(conf, 'name', 'Apple TV')}")
    print(f"Address: {conf.address}")
    print(f"Identifier: {conf.identifier}")
    print("Services:")
    if services:
        for svc in services:
            print(
                f" - {_protocol_name(getattr(svc, 'protocol', None))} "
                f"pairing={getattr(svc, 'pairing', None)} enabled={getattr(svc, 'enabled', None)}"
            )
    else:
        print(" - none")

    chosen = _parse_protocol(protocol_name)
    if protocol_name:
        protocols = [chosen]
    else:
        advertised = {getattr(svc, "protocol", None) for svc in services if getattr(svc, "enabled", True)}
        protocols = []
        for candidate in (Protocol.MRP, Protocol.Companion, None):
            if candidate is None or candidate in advertised:
                protocols.append(candidate)
        if not protocols:
            protocols = [None]

    exit_code = 1
    for protocol in protocols:
        label = _protocol_name(protocol)
        print(f"\nProtocol: {label}")
        atv = None
        try:
            if protocol is None:
                atv = await pyatv.connect(conf, loop, storage=storage)
            else:
                atv = await pyatv.connect(conf, loop, protocol=protocol, storage=storage)
            for index in range(tries):
                print(f"Try {index + 1}/{tries}:")
                playing = await atv.metadata.playing()
                _print_playing_fields(playing)
                await asyncio.sleep(delay_s)
            exit_code = 0
        except Exception as exc:
            print(f"  ERROR: {exc}")
        finally:
            if atv is not None:
                await atv.close()
    return exit_code


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp_scan = sub.add_parser("scan")
    sp_scan.add_argument("--timeout", type=int, default=10)

    sp_play = sub.add_parser("playing")
    sp_play.add_argument("--id", dest="identifier", default=None)
    sp_play.add_argument("--host", dest="host", default=None)
    sp_play.add_argument("--timeout", type=int, default=10)

    sp_meta = sub.add_parser("metadata")
    sp_meta.add_argument("--id", dest="identifier", default=None)
    sp_meta.add_argument("--host", dest="host", default=None)
    sp_meta.add_argument("--timeout", type=int, default=10)
    sp_meta.add_argument("--protocol", default=None)
    sp_meta.add_argument("--tries", type=int, default=6)
    sp_meta.add_argument("--delay", type=float, default=0.5)

    args = ap.parse_args()
    if args.cmd == "scan":
        return int(_run(_scan(args.timeout)))
    if args.cmd == "playing":
        return int(_run(_playing(identifier=args.identifier, host=args.host, timeout_s=args.timeout)))
    if args.cmd == "metadata":
        return int(
            _run(
                _metadata(
                    identifier=args.identifier,
                    host=args.host,
                    timeout_s=args.timeout,
                    protocol_name=args.protocol,
                    tries=max(1, int(args.tries)),
                    delay_s=max(0.0, float(args.delay)),
                )
            )
        )
    raise SystemExit("unknown command")


if __name__ == "__main__":
    raise SystemExit(main())

