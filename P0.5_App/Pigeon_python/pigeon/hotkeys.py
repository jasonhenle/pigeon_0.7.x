"""Human-readable hotkey list for Developer settings (keep in sync with pigeon_0_5 binds)."""

from __future__ import annotations

# (key combo, description)
PIGEON_HOTKEY_ROWS: list[tuple[str, str]] = [
    ("Tab", "Cycle developer mode: off → grid overlay → settings → off"),
    ("Shift+Tab", "Ignored (no cycle)"),
    ("Ctrl+Tab", "Same as Tab (cycle developer mode)"),
    ("F9", "Same as Tab (cycle developer mode)"),
    ("Return / Enter", "In developer mode (grid or settings): open command bar; again focuses entry"),
    ("Return / Enter (in command bar)", "Submit command and close bar"),
    (
        "Command bar (TMDb)",
        "Title only: searches movies and TV, picks by popularity. Prefix tv <title> or movie <title> to force. tmdb … same.",
    ),
    ("Esc", "Close command bar if open; otherwise quit"),
    ("Space", "Play / pause scene (when scene is on)"),
    ("S", "Toggle scene on/off (grid overlay only)"),
    (
        "F10",
        "Developer grid: cycle scene file on → black → TMDb backdrop → scene on (backdrop step needs a prior TMDb fetch). "
        "Other modes: toggle scene on/off.",
    ),
    ("Double-click video", "Toggle scene"),
    ("Ctrl+Shift+S", "Toggle scene"),
    ("Right-click video", "Cycle developer mode"),
    ("Video (button)", "Toggle scene / black (close video file)"),
    ("Dev (button)", "Cycle developer mode"),
]


def format_hotkey_help_text() -> str:
    lines = [f"{keys}\t{desc}" for keys, desc in PIGEON_HOTKEY_ROWS]
    return "\n".join(lines)
