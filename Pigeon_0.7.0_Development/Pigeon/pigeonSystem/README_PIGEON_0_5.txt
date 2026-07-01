# Pigeon 0.5

**Pigeon** is a fixed-resolution (800×480) “Now Playing” display app prototype.  
Version **0.5** implements a **scene** as a background video with play/pause + looping.

## What it does (0.5)

- Opens an **800×480** window.
- Loads a scene MP4 and starts **paused**.
- **SPACEBAR** toggles play/pause.
- Loops back to the beginning when the video ends.
- While **playing**: brightness **100%**.
- While **paused**: brightness **30%**.
- Scales the video to **fill the Y axis** (400px tall), and **center-crops** any extra width (no stretching).

## Install

```bash
python3 -m pip install -r requirements.txt
```

## Layout (Pigeon0.5 bundle)

Typical **iCloud Drive → Desktop → Pigeon0.5** tree:

- **`P0.5_App/Pigeon_python/`** — this app (`pigeon_0_7.py`, `pigeon/` package).
- **`P0.5_App/P0.5_Assets/`** or **`P0.5_Assets/`** (at bundle root) — **`P0.5_Scenes/`** (video) and **`P0.5_Widgets/P0.5_WIDGET_POSTER_4X6_MEDIUM/`** (poster assets).

Paths are resolved automatically from the script location plus Desktop / iCloud fallbacks. Folder names can differ (e.g. `Scenes` vs `P0.5_Scenes`, `Widgets` vs `P0.5_Widgets`); the app also looks for the poster folder by finding **`P_0.5_WIDGET_POSTER_4x6_MEDIUM_border.png`** inside a widgets directory. Override anytime with **`PIGEON_SCENE`**, **`PIGEON_POSTER_ART_DIR`**, **`PIGEON_REFORMATTED_POSTER_DIR`**.

## Run

If your scene is in the default expected location:

```bash
python3 pigeon_0_7.py
```

If the default clip was renamed or moved, the app searches your **Pigeon0.5** folder for `.mp4` / `.mov` files (scene folders first, then the rest of the bundle, skipping `pigeonOld` and `.venv`) and picks the best filename match. Check Terminal for `default scene (discovered) → …`.

Or pass an explicit path:

```bash
python3 pigeon_0_7.py --scene "/path/to/your_scene.mp4"
```

Or set an environment variable:

```bash
export PIGEON_SCENE="/path/to/SCEENE_001_60SECS_scarytrain_718.300.mp4"
python3 pigeon_0_7.py
```

## Controls

- **SPACE**: play/pause toggle
- **ESC**: quit

