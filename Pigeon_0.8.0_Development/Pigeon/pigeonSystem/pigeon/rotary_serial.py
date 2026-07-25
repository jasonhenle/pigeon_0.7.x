"""USB-serial bridge for Pigeon rotary controllers (Arduino UNO Q, etc.).

HID-capable boards (Leonardo / Pro Micro / …) already emit Left / Right / Space
and need nothing here. Serial-mode firmware (`hardware/rotary_hid/rotary_hid.ino`
with ``PIGEON_USE_SERIAL``) prints one line per action:

  RIGHT → forward  (Tk Right)
  LEFT  → backward (Tk Left)   — host navigate(forward=False); not Wi‑Fi password
  PRESS → activate (Tk space)

Optional: ``PIGEON_ROTARY_PORT`` = explicit device path (e.g. ``/dev/ttyACM0``).
Optional dependency: ``pyserial`` (recommended on Pi). Without it, only an
explicit ``PIGEON_ROTARY_PORT`` is opened via POSIX termios.
"""

from __future__ import annotations

import glob
import os
import sys
import threading
import time
from typing import Callable

_READY = "PIGEON_CONTROLLER_READY"
_LINE_TO_KEYSYM = {
    "RIGHT": "Right",
    "LEFT": "Left",
    "PRESS": "space",
}
_BAUD = 115200
_PROBE_SECONDS = 1.25
_RECONNECT_S = 2.0


def _stderr(msg: str) -> None:
    try:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    try:
        from pigeon.pi_diagnostics import append_pigeon_log

        append_pigeon_log(msg)
    except Exception:
        pass


def _env_port() -> str | None:
    raw = (os.environ.get("PIGEON_ROTARY_PORT") or "").strip()
    return raw or None


def _port_blob(port_info) -> str:
    return " ".join(
        str(x or "")
        for x in (
            getattr(port_info, "description", ""),
            getattr(port_info, "manufacturer", ""),
            getattr(port_info, "product", ""),
            getattr(port_info, "hwid", ""),
            getattr(port_info, "device", ""),
        )
    ).lower()


def _is_strong_arduino_match(blob: str) -> bool:
    return any(
        k in blob
        for k in (
            "arduino",
            "uno q",
            "uno-q",
            "zephyr",
        )
    )


def _candidate_ports() -> tuple[list[str], set[str]]:
    """Return (ordered device paths, set of strong Arduino matches safe to open without probe)."""
    env = _env_port()
    if env:
        return [env], {env}
    found: list[str] = []
    strong: set[str] = set()
    try:
        import serial.tools.list_ports  # type: ignore[import-untyped]

        ports = list(serial.tools.list_ports.comports())
        preferred: list[str] = []
        other: list[str] = []
        for p in ports:
            dev = getattr(p, "device", "") or ""
            if not dev:
                continue
            blob = _port_blob(p)
            if "bluetooth" in blob or "debug-console" in blob:
                continue
            if _is_strong_arduino_match(blob):
                preferred.append(dev)
                strong.add(dev)
            elif any(
                k in blob
                for k in (
                    "stm32",
                    "cdc",
                    "usbmodem",
                    "ttyacm",
                    "ttyusb",
                )
            ):
                preferred.append(dev)
            elif "usb" in blob or "acm" in blob:
                other.append(dev)
        found = preferred + [d for d in other if d not in preferred]
    except Exception:
        pass
    if not found:
        for pattern in (
            "/dev/ttyACM*",
            "/dev/ttyUSB*",
            "/dev/cu.usbmodem*",
            "/dev/cu.usbserial*",
        ):
            found.extend(sorted(glob.glob(pattern)))
    out: list[str] = []
    seen: set[str] = set()
    for d in found:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out, strong


def _open_pyserial(port: str):
    import serial  # type: ignore[import-untyped]

    ser = serial.Serial(port=port, baudrate=_BAUD, timeout=0.2)
    return ser


def _open_posix(port: str):
    """Minimal serial reader when pyserial is unavailable (explicit port only)."""
    import termios

    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    attrs[4] = attrs[5] = termios.B115200  # ispeed / ospeed
    # 8N1, raw-ish
    attrs[2] &= ~(termios.PARENB | termios.CSTOPB | termios.CSIZE)
    attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHOE | termios.ISIG)
    termios.tcsetattr(fd, termios.TCSANOW, attrs)

    class _PosixSerial:
        def __init__(self, file_fd: int) -> None:
            self._fd = file_fd
            self._buf = b""

        def readline(self) -> bytes:
            import select

            deadline = time.monotonic() + 0.2
            while b"\n" not in self._buf and time.monotonic() < deadline:
                r, _, _ = select.select([self._fd], [], [], 0.05)
                if not r:
                    continue
                try:
                    chunk = os.read(self._fd, 256)
                except BlockingIOError:
                    continue
                if not chunk:
                    break
                self._buf += chunk
            if b"\n" not in self._buf:
                return b""
            line, self._buf = self._buf.split(b"\n", 1)
            return line + b"\n"

        def close(self) -> None:
            try:
                os.close(self._fd)
            except OSError:
                pass

    return _PosixSerial(fd)


def _open_port(port: str):
    try:
        return _open_pyserial(port)
    except ImportError:
        if sys.platform == "win32":
            raise
        return _open_posix(port)


def _looks_like_controller(ser) -> bool:
    """Read briefly; accept READY banner or any known action line."""
    deadline = time.monotonic() + _PROBE_SECONDS
    while time.monotonic() < deadline:
        try:
            raw = ser.readline()
        except Exception:
            return False
        if not raw:
            continue
        try:
            line = raw.decode("utf-8", errors="ignore").strip()
        except Exception:
            continue
        if not line:
            continue
        if line == _READY or line in _LINE_TO_KEYSYM:
            return True
    return False


def _inject_key(root, keysym: str) -> None:
    """Synthesize the same Tk events HID boards emit."""
    try:
        if keysym == "space":
            root.event_generate("<KeyPress-space>", when="tail")
            try:
                root.event_generate("<space>", when="tail")
            except Exception:
                pass
        else:
            root.event_generate(f"<KeyPress-{keysym}>", when="tail")
    except Exception as exc:
        _stderr(f"pigeon: rotary_serial: event_generate({keysym}) failed: {exc}")


def _read_loop(root, ser, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            raw = ser.readline()
        except Exception as exc:
            _stderr(f"pigeon: rotary_serial: read error: {exc}")
            break
        if not raw:
            continue
        line = raw.decode("utf-8", errors="ignore").strip()
        if not line or line == _READY:
            continue
        keysym = _LINE_TO_KEYSYM.get(line)
        if keysym is None:
            continue
        try:
            root.after(0, lambda ks=keysym: _inject_key(root, ks))
        except Exception:
            break


def start_rotary_serial_listener(root, *, enabled: bool | None = None) -> Callable[[], None] | None:
    """Start a daemon that maps serial LEFT/RIGHT/PRESS → Tk keys.

    Returns a stop callable, or None if disabled / unavailable.
    """
    if enabled is None:
        flag = (os.environ.get("PIGEON_ROTARY_SERIAL") or "1").strip().lower()
        enabled = flag not in ("0", "false", "off", "no")
    if not enabled:
        return None

    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            ports, strong = _candidate_ports()
            if not ports:
                time.sleep(_RECONNECT_S)
                continue
            opened = False
            for port in ports:
                if stop.is_set():
                    break
                ser = None
                try:
                    ser = _open_port(port)
                except Exception:
                    continue
                # Env / strong Arduino USB match: accept without waiting for banner
                # (READY may have been printed before we opened the port).
                trust = port in strong or _env_port() is not None
                try:
                    ok = True if trust else _looks_like_controller(ser)
                except Exception:
                    ok = trust
                if not ok:
                    try:
                        ser.close()
                    except Exception:
                        pass
                    continue
                _stderr(f"pigeon: rotary_serial: connected {port} @ {_BAUD}")
                opened = True
                try:
                    _read_loop(root, ser, stop)
                finally:
                    try:
                        ser.close()
                    except Exception:
                        pass
                    _stderr(f"pigeon: rotary_serial: disconnected {port}")
                break
            if not opened:
                time.sleep(_RECONNECT_S)
            elif not stop.is_set():
                time.sleep(_RECONNECT_S)

    t = threading.Thread(target=worker, name="pigeon-rotary-serial", daemon=True)
    t.start()
    return stop.set
