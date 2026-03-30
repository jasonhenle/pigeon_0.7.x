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
