"""USB-serial / UNO Q Monitor bridge for Pigeon rotary controllers.

HID-capable boards (Leonardo / Pro Micro / …) already emit Left / Right / Space
and need nothing here. Serial-mode firmware (`hardware/rotary_hid/rotary_hid.ino`)
prints one line per action:

  RIGHT / CW / FORWARD     → forward
  LEFT  / CCW / BACKWARD   → backward
  PRESS / PUSH / SELECT    → activate

Transports (both tried):
  1) USB CDC serial (``/dev/ttyACM*``, ``PIGEON_ROTARY_PORT=…``)
  2) Arduino UNO Q Monitor TCP — MCU ``Monitor.println`` is forwarded by the
     board's Linux router to ``localhost:7500``. The Pi opens that via
     ``adb forward tcp:7500 tcp:7500`` (or ``PIGEON_ROTARY_TCP=host:port``).

Optional: ``PIGEON_ROTARY_INVERT=1`` swaps forward/backward.
Optional: ``PIGEON_ROTARY_SERIAL=0`` disables the whole bridge.
Optional: ``PIGEON_ROTARY_TCP=0`` disables the UNO Q TCP path only.
"""

from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Callable

_READY = "PIGEON_CONTROLLER_READY"
_UNO_Q_MONITOR_PORT = 7500
# Map every reasonable token the board (or a hand-rolled sketch) might send.
_LINE_TO_ACTION: dict[str, str] = {
    "RIGHT": "forward",
    "CW": "forward",
    "FORWARD": "forward",
    "FWD": "forward",
    "LEFT": "backward",
    "CCW": "backward",
    "BACKWARD": "backward",
    "BACK": "backward",
    "PREV": "backward",
    "PRESS": "activate",
    "PUSH": "activate",
    "CLICK": "activate",
    "SELECT": "activate",
    "SPACE": "activate",
    "ACTIVATE": "activate",
    "ENTER": "activate",
}
_ACTION_TO_KEYSYM = {
    "forward": "Right",
    "backward": "Left",
    "activate": "space",
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


def _env_invert() -> bool:
    flag = (os.environ.get("PIGEON_ROTARY_INVERT") or "").strip().lower()
    return flag in ("1", "true", "yes", "on")


def _normalize_line(raw: str) -> str:
    line = (raw or "").strip()
    if not line:
        return ""
    # Allow "PUSH\r", "push", "PUSH ", "action=PUSH", etc.
    if "=" in line:
        line = line.rsplit("=", 1)[-1].strip()
    return line.upper()


def _action_for_line(line: str) -> str | None:
    return _LINE_TO_ACTION.get(_normalize_line(line))


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
        if line == _READY or _action_for_line(line) is not None:
            return True
    return False


def inject_keysym(root, keysym: str) -> None:
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


class _ActionGate:
    """Deduplicate identical actions from serial + TCP within a short window."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: tuple[str, float] | None = None

    def accept(self, action: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if self._last is not None:
                prev, t0 = self._last
                if prev == action and (now - t0) < 0.08:
                    return False
            self._last = (action, now)
            return True


def _dispatch_action(
    root,
    action: str,
    on_action: Callable[[str], None] | None,
    gate: _ActionGate | None = None,
) -> None:
    if gate is not None and not gate.accept(action):
        return
    if on_action is not None:
        try:
            on_action(action)
            return
        except Exception as exc:
            _stderr(f"pigeon: rotary_serial: on_action({action}) failed: {exc}")
    keysym = _ACTION_TO_KEYSYM.get(action)
    if keysym:
        inject_keysym(root, keysym)


def _handle_line(
    root,
    line: str,
    *,
    on_action: Callable[[str], None] | None,
    invert: bool,
    gate: _ActionGate,
    logged_ok: list[int],
    ignored: list[int],
    source: str,
) -> None:
    if not line or line == _READY:
        return
    action = _action_for_line(line)
    if action is None:
        if ignored[0] < 12:
            _stderr(f"pigeon: rotary_serial: ignore unknown line {line!r} ({source})")
            ignored[0] += 1
        return
    if invert and action in ("forward", "backward"):
        action = "backward" if action == "forward" else "forward"
    if logged_ok[0] < 8:
        _stderr(f"pigeon: rotary_serial: {line!r} → {action} ({source})")
        logged_ok[0] += 1
    try:
        root.after(
            0,
            lambda act=action: _dispatch_action(root, act, on_action, gate),
        )
    except Exception:
        pass


def _read_loop(
    root,
    ser,
    stop: threading.Event,
    *,
    on_action: Callable[[str], None] | None,
    invert: bool,
    gate: _ActionGate,
) -> None:
    ignored = [0]
    logged_ok = [0]
    while not stop.is_set():
        try:
            raw = ser.readline()
        except Exception as exc:
            _stderr(f"pigeon: rotary_serial: read error: {exc}")
            break
        if not raw:
            continue
        line = raw.decode("utf-8", errors="ignore").strip()
        _handle_line(
            root,
            line,
            on_action=on_action,
            invert=invert,
            gate=gate,
            logged_ok=logged_ok,
            ignored=ignored,
            source="usb",
        )


def _env_tcp_endpoint() -> tuple[str, int] | None:
    """Return (host, port) for UNO Q Monitor TCP, or None if disabled."""
    raw = (os.environ.get("PIGEON_ROTARY_TCP") or "").strip()
    if raw.lower() in ("0", "false", "off", "no"):
        return None
    if raw and ":" in raw:
        host, _, port_s = raw.rpartition(":")
        try:
            return host.strip() or "127.0.0.1", int(port_s)
        except ValueError:
            pass
    # Default: try local ADB-forwarded Monitor port (UNO Q).
    return ("127.0.0.1", _UNO_Q_MONITOR_PORT)


def _adb_bin() -> str | None:
    bundled = os.environ.get("PIGEON_ADB", "").strip()
    if bundled and os.path.isfile(bundled):
        return bundled
    return shutil.which("adb")


def _ensure_adb_forward(port: int) -> bool:
    """Forward host TCP ``port`` to the UNO Q Monitor socket when possible."""
    adb = _adb_bin()
    if not adb:
        return False
    try:
        proc = subprocess.run(
            [adb, "devices"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _stderr(f"pigeon: rotary_serial: adb devices failed: {exc}")
        return False
    lines = [
        ln.strip()
        for ln in (proc.stdout or "").splitlines()
        if ln.strip() and not ln.startswith("List")
    ]
    devices = [ln.split()[0] for ln in lines if "\tdevice" in ln or ln.endswith(" device")]
    if not devices:
        # Also accept "serial device" formatting
        devices = []
        for ln in lines:
            parts = ln.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
    if not devices:
        _stderr("pigeon: rotary_serial: adb: no device (plug UNO Q USB-C data cable)")
        return False
    try:
        subprocess.run(
            [adb, "forward", f"tcp:{port}", f"tcp:{port}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        _stderr(f"pigeon: rotary_serial: adb forward tcp:{port} → device {devices[0]}")
        return True
    except (OSError, subprocess.TimeoutExpired) as exc:
        _stderr(f"pigeon: rotary_serial: adb forward failed: {exc}")
        return False


def _tcp_worker(
    root,
    stop: threading.Event,
    *,
    on_action: Callable[[str], None] | None,
    invert: bool,
    gate: _ActionGate,
) -> None:
    endpoint = _env_tcp_endpoint()
    if endpoint is None:
        return
    host, port = endpoint
    logged = False
    ignored = [0]
    logged_ok = [0]
    while not stop.is_set():
        if host in ("127.0.0.1", "localhost") and port == _UNO_Q_MONITOR_PORT:
            _ensure_adb_forward(port)
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection((host, port), timeout=2.0)
            sock.settimeout(0.5)
            if not logged:
                _stderr(f"pigeon: rotary_serial: connected tcp {host}:{port} (UNO Q Monitor)")
                logged = True
            buf = b""
            while not stop.is_set():
                try:
                    chunk = sock.recv(256)
                except socket.timeout:
                    continue
                except OSError as exc:
                    _stderr(f"pigeon: rotary_serial: tcp read error: {exc}")
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="ignore").strip()
                    _handle_line(
                        root,
                        line,
                        on_action=on_action,
                        invert=invert,
                        gate=gate,
                        logged_ok=logged_ok,
                        ignored=ignored,
                        source="tcp",
                    )
        except OSError:
            if not logged:
                _stderr(
                    f"pigeon: rotary_serial: waiting for UNO Q Monitor at {host}:{port} "
                    "(adb + Bridge.begin/Monitor.begin)"
                )
                logged = True
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if not stop.is_set():
            time.sleep(_RECONNECT_S)


def start_rotary_serial_listener(
    root,
    *,
    enabled: bool | None = None,
    on_action: Callable[[str], None] | None = None,
) -> Callable[[], None] | None:
    """Start daemons that map CW/CCW/PUSH → app actions (USB serial + UNO Q TCP).

    ``on_action`` receives ``\"forward\"``, ``\"backward\"``, or ``\"activate\"`` on
    the Tk thread. Returns a stop callable, or None if disabled.
    """
    if enabled is None:
        flag = (os.environ.get("PIGEON_ROTARY_SERIAL") or "1").strip().lower()
        enabled = flag not in ("0", "false", "off", "no")
    if not enabled:
        return None

    stop = threading.Event()
    invert = _env_invert()
    gate = _ActionGate()
    logged_ports = [False]

    def usb_worker() -> None:
        while not stop.is_set():
            ports, strong = _candidate_ports()
            if not ports:
                if not logged_ports[0]:
                    _stderr(
                        "pigeon: rotary_serial: no USB serial ports yet "
                        "(UNO Q Monitor TCP path may still work via adb)"
                    )
                    logged_ports[0] = True
                time.sleep(_RECONNECT_S)
                continue
            if not logged_ports[0]:
                _stderr(
                    "pigeon: rotary_serial: USB candidates "
                    + ", ".join(ports[:8])
                    + (" …" if len(ports) > 8 else "")
                )
                logged_ports[0] = True
            opened = False
            for port in ports:
                if stop.is_set():
                    break
                ser = None
                try:
                    ser = _open_port(port)
                except Exception as exc:
                    _stderr(f"pigeon: rotary_serial: open {port} failed: {exc}")
                    continue
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
                _stderr(
                    f"pigeon: rotary_serial: connected {port} @ {_BAUD}"
                    + (" (invert)" if invert else "")
                )
                opened = True
                try:
                    _read_loop(
                        root,
                        ser,
                        stop,
                        on_action=on_action,
                        invert=invert,
                        gate=gate,
                    )
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

    threading.Thread(target=usb_worker, name="pigeon-rotary-usb", daemon=True).start()
    threading.Thread(
        target=_tcp_worker,
        name="pigeon-rotary-tcp",
        args=(root, stop),
        kwargs={"on_action": on_action, "invert": invert, "gate": gate},
        daemon=True,
    ).start()
    return stop.set
