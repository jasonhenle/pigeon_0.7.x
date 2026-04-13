"""Human-readable hotkey list for Developer settings (keep in sync with pigeon_0_5 binds)."""

from __future__ import annotations

# (key combo, description)
PIGEON_HOTKEY_ROWS: list[tuple[str, str]] = [
    ("Tab", "Toggle Settings ↔ off (does not enter Grid)"),
    ("Shift+Tab", "Toggle Settings ↔ off (same as Tab)"),
    ("Ctrl+Tab", "Same as Shift+Tab"),
    ("Ctrl+Shift+Tab", "Open advanced capability matrix (extension build)"),
    ("F9", "Same as Shift+Tab"),
    (
        "Return / Enter",
        "Settings, grid overlay (key 5), or legacy developer grid: open command bar. Normal view: Select on the player.",
    ),
    ("Return / Enter (in command bar)", "Submit command and close bar"),
    (
        "Command bar (TMDb)",
        "Title only: searches movies and TV; auto mode prefers TV on popularity ties. Prefix tv <title> or movie <title> to force. tmdb … same.",
    ),
    ("Esc", "Close command bar if open; otherwise quit"),
    (
        "Space",
        "Play/pause on the current player when possible; else if a TMDb backdrop is saved: show backdrop + title logo; "
        "else landing brightness pulse (when scene is on).",
    ),
    (
        "1–5",
        "Views: 1 default now playing UI (auto-switches to 2 if thin), "
        "2 backdrop + title logo + mic, "
        "3 clock saver, "
        "4 plain background + rawTitle debug text only, "
        "5 design grid overlay (19×8) on the composite.",
    ),
    (
        "Arrow keys (Pigeon_ext)",
        "Plain arrows: TV navigation. Shift+arrows: volume / skip. Cmd+arrows: TV back, home, power (device-dependent).",
    ),
    ("S", "Toggle scene on/off (only while grid overlay is visible: key 5 or developer grid)"),
    (
        "F10",
        "While grid overlay is visible: cycle scene landing → black → TMDb backdrop → landing. "
        "Otherwise: toggle scene on/off.",
    ),
    ("Double-click video", "Toggle scene"),
    ("Ctrl+Shift+S", "Toggle scene"),
    ("Right-click video", "Same as Shift+Tab (Settings ↔ off)"),
    (
        "Apple TV (settings)",
        "Apple TV → TMDb: Developer settings auto-discovers Apple TVs, remembers the current one, and still lets you switch before using “Selected → TMDb” (needs pyatv + pairing; see pigeon/apple_tv_now_playing.py).",
    ),
]


def format_hotkey_help_text() -> str:
    lines = [f"{keys}\t{desc}" for keys, desc in PIGEON_HOTKEY_ROWS]
    return "\n".join(lines)
