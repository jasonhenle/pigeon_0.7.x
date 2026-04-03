"""Persisted app state (~/.pigeon_0_5/state.json). Shared by UI and image pipeline (no Tk)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def state_file() -> Path:
    return Path.home() / ".pigeon_0_5" / "state.json"


def read_app_state() -> dict[str, Any]:
    p = state_file()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_app_state(**updates: Any) -> None:
    """Merge updates into existing JSON and atomically replace the file."""
    try:
        p = state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        cur = read_app_state()
        cur.update(updates)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def auto_delete_pulled_media() -> bool:
    v = read_app_state().get("auto_delete_pulled_media")
    return bool(v) if isinstance(v, bool) else False


def read_last_apple_tv() -> dict[str, str]:
    v = read_app_state().get("last_apple_tv")
    if not isinstance(v, dict):
        return {}
    out: dict[str, str] = {}
    for key in ("identifier", "address", "name", "label"):
        value = v.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


def write_last_apple_tv(
    *,
    identifier: str,
    address: str,
    name: str | None = None,
    label: str | None = None,
) -> None:
    payload = {
        "identifier": identifier.strip(),
        "address": address.strip(),
    }
    if isinstance(name, str) and name.strip():
        payload["name"] = name.strip()
    if isinstance(label, str) and label.strip():
        payload["label"] = label.strip()
    write_app_state(last_apple_tv=payload)


def clear_last_apple_tv() -> None:
    write_app_state(last_apple_tv={})


def filter_discovery_for_streaming(rows: list[dict[str, object]]) -> list[dict[str, str]]:
    """Discovery rows suitable for pyatv now-playing (Apple TV / MRP / Companion)."""
    return [dict(r) for r in rows if row_is_playback_apple_tv(r)]


def filter_discovery_for_receiver(rows: list[dict[str, object]]) -> list[dict[str, str]]:
    """Discovery rows that are AirPlay-only / non-tvOS (typical AVR advertisement)."""
    return [dict(r) for r in rows if not row_is_playback_apple_tv(r)]


def row_is_playback_apple_tv(row: dict[str, object]) -> bool:
    """
    True if this saved row should drive pyatv now-playing (tvOS / MRP / Companion).

    False for Denon-style receivers (``denon:`` id), persisted ``looks_like_apple_tv=false``,
    or labels that show AirPlay-only discovery.
    """
    ident = str(row.get("identifier") or "").strip()
    if ident.startswith("denon:"):
        return False
    lk = str(row.get("looks_like_apple_tv") or "").strip().lower()
    if lk in ("true", "1", "yes"):
        return True
    if lk in ("false", "0", "no"):
        return False
    lab = str(row.get("label") or "")
    if "Apple TV / tvOS" in lab:
        return True
    if "AirPlay / other" in lab:
        return False
    return True


def _parse_one_saved_device_row(item: Any) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    row = {k: str(item.get(k, "")).strip() for k in ("identifier", "address", "name", "label")}
    if not row["identifier"] or not row["address"]:
        return None
    if not row["label"]:
        nm = row["name"] or "Device"
        row["label"] = f"{nm} — {row['address']}"
    lk = item.get("looks_like_apple_tv")
    if isinstance(lk, bool):
        row["looks_like_apple_tv"] = "true" if lk else "false"
    elif isinstance(lk, str) and lk.strip().lower() in ("true", "false"):
        row["looks_like_apple_tv"] = lk.strip().lower()
    return row


def _slot_row_to_state_dict(row: dict[str, str] | None) -> dict[str, Any]:
    if not row:
        return {}
    out: dict[str, Any] = {
        "identifier": str(row.get("identifier", "")).strip(),
        "address": str(row.get("address", "")).strip(),
        "name": str(row.get("name", "")).strip(),
        "label": str(row.get("label", "")).strip(),
    }
    lk = row.get("looks_like_apple_tv")
    if isinstance(lk, str) and lk.strip().lower() in ("true", "false"):
        out["looks_like_apple_tv"] = lk.strip().lower()
    return out


def migrate_device_slots_from_legacy_if_needed() -> None:
    """
    One-time: split legacy ``saved_apple_tv_devices`` into ``saved_streaming_device`` and
    ``saved_av_receiver`` (at most one each), then clear the legacy list.
    """
    cur = read_app_state()
    if cur.get("device_slots_v1_migrated") is True:
        return
    legacy_rows: list[dict[str, str]] = []
    raw_legacy = cur.get("saved_apple_tv_devices")
    if isinstance(raw_legacy, list):
        for item in raw_legacy:
            parsed = _parse_one_saved_device_row(item)
            if parsed is not None:
                legacy_rows.append(parsed)

    stream = _parse_one_saved_device_row(cur.get("saved_streaming_device"))
    avr = _parse_one_saved_device_row(cur.get("saved_av_receiver"))

    for row in legacy_rows:
        if stream is None and row_is_playback_apple_tv(row):
            stream = dict(row)
        elif avr is None and not row_is_playback_apple_tv(row):
            avr = dict(row)
        if stream is not None and avr is not None:
            break

    write_app_state(
        device_slots_v1_migrated=True,
        saved_apple_tv_devices=[],
        saved_streaming_device=_slot_row_to_state_dict(stream),
        saved_av_receiver=_slot_row_to_state_dict(avr),
    )


def read_saved_streaming_device() -> dict[str, str] | None:
    migrate_device_slots_from_legacy_if_needed()
    return _parse_one_saved_device_row(read_app_state().get("saved_streaming_device"))


def write_saved_streaming_device(row: dict[str, str] | None) -> None:
    migrate_device_slots_from_legacy_if_needed()
    write_app_state(saved_streaming_device=_slot_row_to_state_dict(row))


def read_saved_av_receiver() -> dict[str, str] | None:
    migrate_device_slots_from_legacy_if_needed()
    return _parse_one_saved_device_row(read_app_state().get("saved_av_receiver"))


def write_saved_av_receiver(row: dict[str, str] | None) -> None:
    migrate_device_slots_from_legacy_if_needed()
    write_app_state(saved_av_receiver=_slot_row_to_state_dict(row))


def clear_all_persisted_devices_and_targets() -> None:
    """Clear saved device slots, last playback/receiver targets, and legacy receiver lists."""
    migrate_device_slots_from_legacy_if_needed()
    write_app_state(
        saved_streaming_device={},
        saved_av_receiver={},
        saved_apple_tv_devices=[],
        last_apple_tv={},
        last_receiver={},
        saved_receivers=[],
    )


def read_saved_apple_tv_devices() -> list[dict[str, str]]:
    """Deprecated combined list; returns streaming + AVR slots only (for old callers)."""
    migrate_device_slots_from_legacy_if_needed()
    out: list[dict[str, str]] = []
    s = _parse_one_saved_device_row(read_app_state().get("saved_streaming_device"))
    if s:
        out.append(s)
    a = _parse_one_saved_device_row(read_app_state().get("saved_av_receiver"))
    if a:
        out.append(a)
    return out


def write_saved_apple_tv_devices(rows: list[dict[str, str]]) -> None:
    """Writes the two single-device slots from at most two legacy-shaped rows."""
    migrate_device_slots_from_legacy_if_needed()
    stream: dict[str, str] | None = None
    avr: dict[str, str] | None = None
    for r in rows:
        if not isinstance(r, dict):
            continue
        row = {k: str(r.get(k, "")).strip() for k in ("identifier", "address", "name", "label")}
        if not row["identifier"] or not row["address"]:
            continue
        if not row["label"]:
            nm = row["name"] or "Device"
            row["label"] = f"{nm} — {row['address']}"
        lk = r.get("looks_like_apple_tv")
        if isinstance(lk, bool):
            row["looks_like_apple_tv"] = "true" if lk else "false"
        elif isinstance(lk, str) and lk.strip().lower() in ("true", "false"):
            row["looks_like_apple_tv"] = lk.strip().lower()
        if stream is None and row_is_playback_apple_tv(row):
            stream = row
        elif avr is None and not row_is_playback_apple_tv(row):
            avr = row
        if stream is not None and avr is not None:
            break
    write_saved_streaming_device(stream)
    write_saved_av_receiver(avr)


def merge_legacy_saved_receivers_into_av_slot() -> bool:
    """
    One-time: if the AVR slot is empty, copy the first ``saved_receivers`` entry into
    ``saved_av_receiver`` as a synthetic ``denon:host`` row, then clear ``saved_receivers``.
    """
    migrate_device_slots_from_legacy_if_needed()
    legacy = read_saved_receivers()
    if not legacy:
        return False
    if read_saved_av_receiver() is not None:
        write_saved_receivers([])
        return False

    item = legacy[0]
    host = str(item.get("host") or "").strip()
    if not host:
        write_saved_receivers([])
        return False
    nm = str(item.get("name") or "").strip() or "Receiver"
    lb = str(item.get("label") or "").strip() or f"{nm} — {host}"
    write_saved_av_receiver(
        {
            "identifier": f"denon:{host}",
            "address": host,
            "name": nm,
            "label": lb,
            "looks_like_apple_tv": "false",
        }
    )
    write_saved_receivers([])
    return True


def merge_legacy_saved_receivers_into_apple_tv_rows(rows: list[dict[str, str]]) -> bool:
    """Deprecated: use ``merge_legacy_saved_receivers_into_av_slot``."""
    _ = rows
    return merge_legacy_saved_receivers_into_av_slot()


def read_last_receiver() -> dict[str, str]:
    v = read_app_state().get("last_receiver")
    if not isinstance(v, dict):
        return {}
    out: dict[str, str] = {}
    for key in ("host", "name", "label", "id"):
        val = v.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
    return out


def write_last_receiver(
    *,
    host: str,
    name: str | None = None,
    label: str | None = None,
    device_id: str | None = None,
) -> None:
    payload: dict[str, str] = {"host": host.strip()}
    if isinstance(name, str) and name.strip():
        payload["name"] = name.strip()
    if isinstance(label, str) and label.strip():
        payload["label"] = label.strip()
    if isinstance(device_id, str) and device_id.strip():
        payload["id"] = device_id.strip()
    write_app_state(last_receiver=payload)


def clear_last_receiver() -> None:
    write_app_state(last_receiver={})


def read_saved_receivers() -> list[dict[str, str]]:
    raw = read_app_state().get("saved_receivers")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        row = {k: str(item.get(k, "")).strip() for k in ("host", "name", "label", "id")}
        if not row["host"]:
            continue
        if not row["label"]:
            nm = row["name"] or "Receiver"
            row["label"] = f"{nm} — {row['host']}"
        out.append(row)
    return out


def write_saved_receivers(rows: list[dict[str, str]]) -> None:
    clean: list[dict[str, str]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        row = {k: str(r.get(k, "")).strip() for k in ("host", "name", "label", "id")}
        if not row["host"]:
            continue
        if not row["label"]:
            nm = row["name"] or "Receiver"
            row["label"] = f"{nm} — {row['host']}"
        clean.append(row)
    write_app_state(saved_receivers=clean)
