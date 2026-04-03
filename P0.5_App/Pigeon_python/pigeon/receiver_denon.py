"""Poll Denon/Marantz-class receivers over HTTP (Main Zone status XML)."""

from __future__ import annotations

import concurrent.futures
import platform
import re
import select
import socket
import ssl
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlparse

# Same endpoints the Denon 2016+ web UI and denonavr use for main zone snapshot.
_STATUS_PATHS = (
    "/goform/formMainZone_MainZoneXml.xml",
    "/goform/formMainZone_MainZoneXmlStatus.xml",
)


@dataclass(frozen=True)
class ReceiverPollResult:
    ok: bool
    volume: str
    incoming: str
    config: str


def _normalize_host(host: str) -> str:
    h = (host or "").strip()
    h = re.sub(r"^https?://", "", h, flags=re.I).strip().rstrip("/")
    return h


_SSL_UNVERIFIED = ssl.create_default_context()
_SSL_UNVERIFIED.check_hostname = False
_SSL_UNVERIFIED.verify_mode = ssl.CERT_NONE


def _fetch(host: str, path: str, timeout: float, *, scheme: str = "http") -> str | None:
    sch = scheme if scheme in ("http", "https") else "http"
    url = f"{sch}://{host}{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Pigeon/0.5"})
        ctx = _SSL_UNVERIFIED if sch == "https" else None
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _parse_item_xml(xml_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    item = root if root.tag.lower() == "item" else None
    if item is None:
        for el in root.iter():
            if el.tag.lower() == "item":
                item = el
                break
    if item is None:
        return out
    skip_containers = {
        "videoselectlists",
        "ecomodelists",
        "inputfunclist",
        "renamesource",
        "sourcedelete",
    }
    for child in list(item):
        if child.tag.lower() in skip_containers:
            continue
        val_el = child.find("value")
        if val_el is not None and val_el.text is not None:
            t = val_el.text.strip()
            if t:
                out[child.tag] = t
        elif child.text and str(child.text).strip():
            out[child.tag] = str(child.text).strip()
    return out


def _schemes_for_host(host: str) -> tuple[str, ...]:
    """Pick URL schemes for a probe host (may include ``host:port``)."""
    m = re.match(r"^(.+):(\d+)$", host)
    if not m:
        return ("http", "https")
    port = int(m.group(2))
    if port == 443:
        return ("https",)
    if port in (80, 8080):
        return ("http",)
    return ("https", "http")


def _merge_zone_status(
    host: str,
    timeout: float,
    *,
    scheme_order: tuple[str, ...] | None = None,
) -> dict[str, str] | None:
    schemes = scheme_order if scheme_order is not None else _schemes_for_host(host)
    for scheme in schemes:
        merged: dict[str, str] = {}
        ok_any = False
        for path in _STATUS_PATHS:
            body = _fetch(host, path, timeout, scheme=scheme)
            if not body:
                continue
            ok_any = True
            merged.update(_parse_item_xml(body))
        if ok_any:
            return merged
    return None


def _merge_zone_status_with_fallback(host: str, timeout: float) -> dict[str, str] | None:
    """Try primary host, then bare IPv4 with :8080 (some Denon/Marantz UIs listen there)."""
    d = _merge_zone_status(host, timeout)
    if d:
        return d
    hn = _normalize_host(host)
    if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", hn) and ":" not in hn:
        d = _merge_zone_status(f"{hn}:8080", timeout)
        if d:
            return d
    return None


def poll_denon_like_receiver(host: str, timeout: float = 4.0) -> ReceiverPollResult:
    """
    Return overlay strings. On transport/parse failure or no signal: ok=False and empty
    incoming/config/volume lines.
    """
    h = _normalize_host(host)
    if not h:
        return ReceiverPollResult(False, "", "", "")

    d = _merge_zone_status_with_fallback(h, timeout)
    if not d:
        return ReceiverPollResult(False, "", "", "")

    power = (d.get("Power") or d.get("ZonePower") or "").upper()
    if power in ("OFF", "STANDBY"):
        return ReceiverPollResult(True, "", "", "")

    mute = (d.get("Mute") or "").strip().lower()
    mv = (d.get("MasterVolume") or "").strip()
    if mute == "on":
        vol_s = "mute"
    elif mv:
        vol_s = f"{mv}dB"
    else:
        vol_s = ""

    incoming = ""
    for key in (
        "HDMIAudio",
        "HDsignalMode",
        "HDMISig",
        "InputSignal",
        "AudioInputSignal",
        "DigitalInputSignal",
        "signalDisplay",
        "AudioCodec",
        "CodecDisp",
    ):
        v = d.get(key)
        if v:
            incoming = v
            break
    if not incoming:
        for k, v in d.items():
            if not v or len(v) < 2:
                continue
            kl = k.lower()
            if ("signal" in kl or "codec" in kl) and "power" not in kl and "mute" not in kl:
                incoming = v
                break

    cfg = (d.get("selectSurround") or d.get("SurrMode") or "").strip()
    cfg = " ".join(cfg.split())

    if incoming:
        incoming = incoming.lower()

    if cfg:
        cfg = cfg.lower()

    return ReceiverPollResult(True, vol_s, incoming, cfg)


def _darwin_extra_lan_ipv4() -> list[str]:
    """macOS often reports one address via ``getaddrinfo``; query common interfaces."""
    if platform.system() != "Darwin":
        return []
    ips: list[str] = []
    for iface in ("en0", "en1", "en2", "en3", "bridge100", "bridge101"):
        try:
            out = subprocess.run(
                ["ipconfig", "getifaddr", iface],
                capture_output=True,
                text=True,
                timeout=0.4,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        s = (out.stdout or "").strip()
        if s and re.match(r"^\d{1,3}(\.\d{1,3}){3}$", s) and not s.startswith("127."):
            ips.append(s)
    return ips


def _local_class_c_bases() -> list[str]:
    """Unique ``a.b.c`` /24 prefixes for this machine's non-loopback IPv4 addresses."""
    seen: set[str] = set()
    bases: list[str] = []
    candidate_ips: list[str] = []
    try:
        hn = socket.gethostname()
        for res in socket.getaddrinfo(hn, None, socket.AF_INET, socket.SOCK_STREAM):
            candidate_ips.append(res[4][0])
    except Exception:
        pass
    candidate_ips.extend(_darwin_extra_lan_ipv4())
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        candidate_ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    for ip in candidate_ips:
        if ip.startswith("127."):
            continue
        parts = ip.split(".")
        if len(parts) == 4:
            b = f"{parts[0]}.{parts[1]}.{parts[2]}"
            if b not in seen:
                seen.add(b)
                bases.append(b)
    if not bases:
        bases.append("192.168.1")
    return bases


_SSDP_BRAND_MARKERS = ("denon", "marantz", "sound united", "heos")


def _ssdp_collect_probe_hints(total_wait: float = 3.5) -> list[tuple[str, tuple[str, ...] | None]]:
    """
    Broadcast SSDP (``upnp:rootdevice`` + MediaRenderer) and turn matching replies
    into HTTP probe targets. Denon/Marantz AVRs advertise over UPnP even when a blind
    /24 port-80 sweep would miss them (different subnet inference, HTTPS-only UI, etc.).
    """
    hints: list[tuple[str, tuple[str, ...] | None]] = []
    seen: set[str] = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        except OSError:
            pass
        sock.bind(("", 0))
        sock.setblocking(False)
        searches = (
            (
                "M-SEARCH * HTTP/1.1\r\n"
                "HOST: 239.255.255.250:1900\r\n"
                'MAN: "ssdp:discover"\r\n'
                "ST: upnp:rootdevice\r\n"
                "MX: 2\r\n"
                "\r\n"
            ),
            (
                "M-SEARCH * HTTP/1.1\r\n"
                "HOST: 239.255.255.250:1900\r\n"
                'MAN: "ssdp:discover"\r\n'
                "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
                "MX: 2\r\n"
                "\r\n"
            ),
        )
        for pkt in searches:
            try:
                sock.sendto(pkt.encode("ascii"), ("239.255.255.250", 1900))
            except OSError:
                pass
        deadline = time.monotonic() + total_wait
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            r, _, _ = select.select([sock], [], [], min(remaining, 0.5))
            if not r:
                continue
            try:
                data, _addr = sock.recvfrom(16384)
            except OSError:
                continue
            text = data.decode("utf-8", errors="replace")
            low = text.lower()
            if not any(m in low for m in _SSDP_BRAND_MARKERS):
                continue
            loc_raw: str | None = None
            for line in text.split("\r\n"):
                if line.lower().startswith("location:"):
                    loc_raw = line.split(":", 1)[1].strip()
                    break
            if not loc_raw:
                continue
            try:
                u = urlparse(loc_raw)
            except Exception:
                continue
            hostname = (u.hostname or "").strip()
            if not hostname:
                continue
            scheme_l = (u.scheme or "http").lower()
            port = u.port
            scheme_order: tuple[str, ...] | None
            if scheme_l == "https":
                host_str = f"{hostname}:{port}" if port and port != 443 else hostname
                scheme_order = ("https", "http")
            elif scheme_l == "http":
                host_str = f"{hostname}:{port}" if port and port != 80 else hostname
                scheme_order = None
            else:
                continue
            if not host_str or host_str in seen:
                continue
            seen.add(host_str)
            hints.append((host_str, scheme_order))
    finally:
        sock.close()
    return hints


def _looks_like_denon_zone_status(d: dict[str, str]) -> bool:
    if not d:
        return False
    if d.get("FriendlyName"):
        return True
    if (d.get("Power") or d.get("ZonePower")) and (
        "MasterVolume" in d or "InputFuncSelect" in d or "SurrMode" in d or "selectSurround" in d
    ):
        return True
    return False


def _receiver_row_from_status(host: str, d: dict[str, str]) -> dict[str, str]:
    name = (d.get("FriendlyName") or "").strip()
    if not name or name.upper() in ("MARANTZ_MODEL", "DENON_MODEL"):
        name = "Receiver"
    return {
        "host": host,
        "name": name,
        "label": f"{name} — {host}",
        "id": "",
    }


def _canonical_receiver_key(host: str) -> str:
    """Stable dedupe key per physical host (``split(':')[0]`` breaks ``foo.local:8080``)."""
    h = (host or "").strip()
    if not h:
        return ""
    m = re.match(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d+)$", h)
    if m:
        return m.group(1)
    m2 = re.match(r"^(.+):(\d+)$", h)
    if m2 and m2.group(2).isdigit():
        return m2.group(1).lower()
    return h.lower()


def _dedupe_host_list(hosts: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in hosts or []:
        t = str(raw or "").strip()
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _host_has_explicit_trailing_port(h: str) -> bool:
    """True for ``192.168.1.5:8080`` or ``avr.local:8080``; false for IPv6 like ``::1``."""
    if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}:\d+$", h):
        return True
    if re.search(r"\]:\d+$", h):
        return True
    if h.count(":") == 1:
        left, right = h.rsplit(":", 1)
        if right.isdigit() and not re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", left):
            return True
        if right.isdigit() and "." in left:
            return True
    return False


def _probe_host_for_receiver(host: str, timeout: float) -> dict[str, str] | None:
    """
    Probe a saved or mDNS address (IPv4, hostname, optional ``:port``) for Denon/Marantz XML.
    IPv4 gets a second try on ``:8080``; explicit ports are probed once.
    """
    h = _normalize_host(host)
    if not h:
        return None
    half = max(0.12, timeout * 0.5)
    if _host_has_explicit_trailing_port(h):
        d = _merge_zone_status(h, half)
        if d is not None and _looks_like_denon_zone_status(d):
            return _receiver_row_from_status(h, d)
        return None
    candidates = [h]
    if "." in h and h.count(":") == 0:
        candidates.append(f"{h}:8080")
    for cand in candidates:
        d = _merge_zone_status(cand, half)
        if d is not None and _looks_like_denon_zone_status(d):
            return _receiver_row_from_status(cand, d)
    return None


def _probe_ip_for_receiver(ip: str, timeout: float) -> dict[str, str] | None:
    """Subnet sweep: same as :func:`_probe_host_for_receiver` for a bare IPv4."""
    return _probe_host_for_receiver(ip, timeout)


def _probe_host_hint(
    host: str,
    timeout: float,
    scheme_order: tuple[str, ...] | None,
) -> dict[str, str] | None:
    d = _merge_zone_status(host, timeout, scheme_order=scheme_order)
    if d is not None and _looks_like_denon_zone_status(d):
        return _receiver_row_from_status(host, d)
    return None


def scan_denon_like_receivers_on_lan(
    *,
    timeout_per_host: float = 0.4,
    max_workers: int = 64,
    ssdp_wait: float = 3.5,
    extra_hosts: list[str] | None = None,
) -> tuple[bool, str, list[dict[str, str]]]:
    """
    Discover receivers via SSDP (UPnP), optional ``extra_hosts`` (e.g. IPs from pyatv /
    AirPlay discovery), plus a sweep of inferred local /24 subnet(s) for Denon/Marantz
    ``MainZone`` HTTP(S) XML.

    Returns ``(ok, message, rows)`` where each row has ``host``, ``name``, ``label``, ``id``.
    """
    bases = _local_class_c_bases()
    if not bases:
        return False, "Could not determine a local subnet to scan.", []

    hints = _ssdp_collect_probe_hints(ssdp_wait)
    ips = [f"{b}.{i}" for b in bases for i in range(1, 255)]
    airplay_or_saved = _dedupe_host_list(extra_hosts)
    by_canonical: dict[str, dict[str, str]] = {}

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures: list[concurrent.futures.Future[dict[str, str] | None]] = []
            for host, scheme_order in hints:
                futures.append(
                    ex.submit(_probe_host_hint, host, timeout_per_host, scheme_order)
                )
            for h in airplay_or_saved:
                futures.append(
                    ex.submit(_probe_host_for_receiver, h, timeout_per_host)
                )
            futures.extend(ex.submit(_probe_ip_for_receiver, ip, timeout_per_host) for ip in ips)
            for fut in concurrent.futures.as_completed(futures, timeout=240):
                try:
                    row = fut.result()
                except Exception:
                    row = None
                if not row:
                    continue
                canon = _canonical_receiver_key(str(row.get("host") or ""))
                if not canon:
                    continue
                if canon not in by_canonical:
                    by_canonical[canon] = row
    except concurrent.futures.TimeoutError:
        pass

    rows = sorted(by_canonical.values(), key=lambda r: (r.get("host") or ""))
    if not rows:
        return (
            True,
            "No Denon/Marantz-style receivers answered via UPnP (SSDP), HTTP(S) on addresses "
            "from Apple TV / AirPlay discovery (when available), or the subnet sweep. "
            "Use “Add receiver…” with the IP from your AVR’s network menu, or enable the "
            "receiver’s network / IP control / web UI option if your model hides the API.",
            [],
        )
    return True, f"Found {len(rows)} receiver(s).", rows
