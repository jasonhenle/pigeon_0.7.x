# pigeon_0.7.x

**Pigeon** is a full-screen media display and control surface (Apple TV / Roku metadata, TMDb artwork, clocks, status bar, remotes).

This repository is the **canonical source** for the app — fresh installs and in-app updates both pull from here. You do not need a Mac to build or distribute Pigeon.

## Quick links

| Goal | Where |
|------|--------|
| **Install from scratch (Pi, Mac, Linux)** | [`Pigeon_0.7.0_Development/Pigeon/GITHUB.md`](Pigeon_0.7.0_Development/Pigeon/GITHUB.md) |
| **Raspberry Pi** | [`Pigeon_0.7.0_Development/Pigeon/raspberryPi/README_RASPBERRY_PI.md`](Pigeon_0.7.0_Development/Pigeon/raspberryPi/README_RASPBERRY_PI.md) |
| **App folder (code + assets)** | [`Pigeon_0.7.0_Development/Pigeon/`](Pigeon_0.7.0_Development/Pigeon/) |
| **GitHub Releases** (Pi tarballs) | https://github.com/jasonhenle/pigeon_0.7.x/releases |

## One-command install

**Raspberry Pi / Linux:**

```bash
curl -fsSL -o /tmp/pigeon-install.sh \
  "https://raw.githubusercontent.com/jasonhenle/pigeon_0.7.x/main/Pigeon_0.7.0_Development/Pigeon/installer/install_from_github.sh"
bash /tmp/pigeon-install.sh
```

**macOS:** same script (downloads the main-branch zip and runs the Mac installer).

**Already installed:** open Pigeon → Settings → **Updates** (or `installer/pi_update_from_github.sh` on Pi).

## What is in git

The tracked tree includes **all runtime code and UI assets** (`pigeonAssets/`, `installer/`, `pigeonSystem/`). It does **not** include your personal settings (`~/.pigeon_0_6/`) or local TMDb cache (`pigeonTMDB/`).

When `version.py` changes on `main`, GitHub Actions builds `pigeon_<version>_raspberry_pi.tar.gz` and attaches it to [Releases](https://github.com/jasonhenle/pigeon_0.7.x/releases).
