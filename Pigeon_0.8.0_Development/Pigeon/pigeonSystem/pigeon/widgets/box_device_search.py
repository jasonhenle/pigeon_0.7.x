"""Box2/box3 LAN device discovery UI state (search icon → results list)."""

from __future__ import annotations

from dataclasses import dataclass

_BOX_DEVICE_ROW_COUNT = 5
_BOX_SEARCH_CENTERS_SVG: dict[int, tuple[float, float]] = {
    2: (399.32, 309.323),
    3: (644.32, 309.323),
}
_BOX_SCAN_MAX_DURATION_S = 25.0
_BOX_SCAN_ROTATION_DPS = 720.0


@dataclass
class BoxDevicePanelState:
    """Per-column device search + results picker."""

    active: bool = False
    phase: str = "idle"  # idle | scanning | results
    scanning: bool = False
    scan_started_mono: float = 0.0
    scan_angle_deg: float = 0.0
    devices: tuple[tuple[str, str], ...] = ()  # (name, ip)
    device_rows: tuple[dict[str, str], ...] = ()
    scroll: int = 0
    row: int = 0
    arrow: str = ""  # "up" | "down" | ""
    picked: tuple[str, str] | None = None

    @property
    def results_locked(self) -> bool:
        return self.active and self.phase == "results" and self.picked is None

    def max_scroll(self) -> int:
        return max(0, len(self.devices) - _BOX_DEVICE_ROW_COUNT)

    def can_scroll_up(self) -> bool:
        return int(self.scroll) > 0

    def can_scroll_down(self) -> bool:
        return int(self.scroll) < self.max_scroll()

    def absolute_row(self, visible_row: int | None = None) -> int:
        r = int(self.row) if visible_row is None else int(visible_row)
        return int(self.scroll) + r


def box_search_center_svg(box_num: int) -> tuple[float, float]:
    return _BOX_SEARCH_CENTERS_SVG.get(box_num, (400.0, 309.323))


def scan_lan_devices(box_num: int = 2) -> tuple[tuple[tuple[str, str], ...], tuple[dict[str, str], ...]]:
    """Return display names + full pyatv rows for a box column (AV-filtered)."""
    from pigeon.widgets.box_device_pairing import scan_devices_for_box

    return scan_devices_for_box(box_num)


__all__ = [
    "BoxDevicePanelState",
    "_BOX_DEVICE_ROW_COUNT",
    "box_search_center_svg",
    "scan_lan_devices",
]
