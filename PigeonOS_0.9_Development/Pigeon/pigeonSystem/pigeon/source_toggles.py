"""On/off switches for Pigeon data sources (Wi‑Fi, Apple TV metadata, HDMI OCR, audio).

Persisted in ``state.json`` as ``source_toggles``. Default is on so existing
devices keep working. Audio is reserved (no recognizer yet) but the switch
is stored so the settings tile can toggle now.
"""

from __future__ import annotations

from typing import Any

_KINDS = ("wifi", "metadata", "hdmi", "audio")
_DEFAULTS: dict[str, bool] = {
    "wifi": True,
    "metadata": True,
    "hdmi": True,
    "audio": True,
}


def read_source_toggles() -> dict[str, bool]:
    from pigeon.app_state import read_app_state

    raw = read_app_state().get("source_toggles")
    out = dict(_DEFAULTS)
    if isinstance(raw, dict):
        for kind in _KINDS:
            if kind in raw:
                out[kind] = bool(raw[kind])
    return out


def source_enabled(kind: str) -> bool:
    """True when that source is allowed to feed Pigeon."""
    key = str(kind or "").strip().lower()
    if key not in _KINDS:
        return True
    return bool(read_source_toggles().get(key, True))


def set_source_enabled(kind: str, enabled: bool) -> bool:
    key = str(kind or "").strip().lower()
    if key not in _KINDS:
        return False
    flags = read_source_toggles()
    flags[key] = bool(enabled)
    from pigeon.app_state import write_app_state

    write_app_state(source_toggles=flags)
    return flags[key]


def toggle_source(kind: str) -> bool:
    """Flip one source. Returns the new enabled value."""
    key = str(kind or "").strip().lower()
    return set_source_enabled(key, not source_enabled(key))


def apply_toggles_to_settings_state(state: Any) -> None:
    """Copy persisted flags onto ``MainSettingsState`` for the LED tiles."""
    flags = read_source_toggles()
    state.source_wifi_on = flags["wifi"]
    state.source_metadata_on = flags["metadata"]
    state.source_hdmi_on = flags["hdmi"]
    state.source_audio_on = flags["audio"]
    # Keep legacy LED fields in sync so older render paths stay consistent.
    state.pigeon_hdmi_ok = flags["hdmi"]
    state.pigeon_audio_ok = flags["audio"]


_IDENTITY_KEYS = (
    "query",
    "title",
    "artist",
    "series_name",
    "album",
    "episode_title",
)


def strip_streaming_identity(metadata: dict[str, Any]) -> None:
    """Drop Apple TV / Roku title fields when the metadata source is off."""
    for key in _IDENTITY_KEYS:
        metadata[key] = ""
