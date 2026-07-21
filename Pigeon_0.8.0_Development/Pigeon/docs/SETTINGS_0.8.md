# Pigeon 0.8 Settings

## Tab cycle

| Press | Result |
|-------|--------|
| 1st Tab | Opens **main_settings** (SVG menu from `settings_main.svg`, drawn on the video canvas) |
| 2nd Tab | Opens **legacy_settings** (Tk settings form; SVG sibling is `settings_0.7.svg`) |
| 3rd Tab | Closes settings → OFF |

Never enters the design grid via Tab (grid remains key 5 / Shift+Tab / F9 paths).

While in **main_settings**: Left/Right move focus; Spacebar activates the focused control (`exit` returns to OFF). Activating **location** or **network** opens the text keyboard overlay (same Left/Right/Space controls).

## Legacy vs main

| Name | Module / surface | Asset |
|------|------------------|-------|
| **main_settings** | `pigeon/widgets/main_settings.py` + OpenCV composite | `pigeonAssets/settings_0.8/settings_main.svg` |
| **legacy_settings** | Tk form in `pigeon_0_8.py`; SVG preview in `settings_page.py` | `settings_0.7.svg` |

`DevPhase.MAIN_SETTINGS = 3`, `DevPhase.SETTINGS = 2` (legacy Tk).

## Assets

## Keyboards

Runtime SVGs under `settings_0.8/`:

- `keyboard_bottom_row.svg` — shared ABC / abc / sym / space / delete / cancel / go
- `keyboard_qwerty_lower.svg` / `keyboard_qwerty_upper.svg`
- `keyboard_numeric_all.svg` (layer ids use `keyboard_numeric_full_*`)
- `keyboard_numeric_pin.svg` — Apple TV pairing PIN
- `keyboard_symbolic.svg`

Module: `pigeon/widgets/settings_keyboard.py`.

**Open:** Space on location or network in main_settings opens the qwerty keyboard overlay.
**Nav:** Left/Right move key focus; Space activates (type / mode switch / delete / cancel / go).
**Close:** cancel discards; go commits text back into location/network.

`keyboard_numeric_ip` is specified in instructions but not yet in the GFX export set.

Override main SVG path with env `PIGEON_MAIN_SETTINGS_SVG`.

## Layer classes

- `_text` — dynamic content; fill/stroke follow selection
- `_icon` — fixed form; fill/stroke follow selection
- `_button` — fixed form; fill/stroke follow selection; default fill ~`#202020`; bottommost vs paired layers
- `_accent` — theme accent (default white); **not** changed by selection
- `_group` — parent grouping / position
- `_container` — static chrome; bottommost in a group when present

Illustrator ids encode `_` as `_x5F_` and leading `0` as `_x30_`. Use `decode_svg_id` / `encode_svg_id` in `main_settings.py`.

## Colors

| Role | Default |
|------|---------|
| UI | `#ff0013` (user configurable later) |
| Selected | `#02e900` |
| Deselected | `#202020` |
| Accent | `#FFFFFF` (selection does not change accent) |
| Inactive | dark gray (not user configurable) |

Selected button → green fill, black contrasting text/icons.  
Deselected button → black fill, green contrasting text/icons.  
`text_pigeonVersion` stays black when present.

**Fonts:** layers under `main_instructions` and all keyboard text use **Sharp Sans Semibold**; other UI text (device names, IPs, EXIT, location/network labels, etc.) uses **Digital-7 Regular**.

Design source of truth: `settingInstructions_0.8.0.numbers` (under `pigeonAssets/settings_0.8/` and optionally `docs/`).
