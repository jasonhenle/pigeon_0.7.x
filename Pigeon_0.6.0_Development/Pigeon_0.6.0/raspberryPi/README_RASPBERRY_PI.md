# Pigeon on Raspberry Pi

This folder contains everything needed to run Pigeon 0.6 on **Raspberry Pi OS** (64-bit recommended: Pi 4 / Pi 5 with desktop).

## What you need on the Pi

- Raspberry Pi OS with **desktop** (Pigeon uses Tkinter — a monitor and keyboard/mouse for setup).
- Network access (for `pip install` on first run and TMDb / device control).
- At least **2 GB RAM** recommended (Pi 4 2 GB or better).

## Quick start (from your Mac)

### 1. Build the transfer archive (on your Mac)

```bash
cd /path/to/Pigeon_0.6.0
bash raspberryPi/package_for_pi.sh
```

This writes `raspberryPi/dist/pigeon_0.6.0_raspberry_pi.tar.gz` (~60–80 MB without local TMDB cache).

### 2. Copy to the Pi

```bash
scp raspberryPi/dist/pigeon_0.6.0_raspberry_pi.tar.gz pi@YOUR_PI_IP:~
```

Or use a USB drive, `rsync`, AirDrop-to-files, etc.

### 3. Install on the Pi (no terminal)

1. On the Pi, open **File Manager** and go to your **Downloads** or **Home** folder.
2. Right-click `pigeon_0.6.0_raspberry_pi.tar.gz` → **Extract Here** (or use Archive Manager).
3. Open the **`Pigeon_0.6.0`** folder (not the folder above it).
4. **Double-click `Install-Pigeon`** (no spaces in the name).

Read **`START-HERE.txt`** in that folder if anything is unclear.

The first time, the Pi may ask you to trust the launcher — choose **Execute** (not Open).

You’ll get a confirmation dialog, then a **password prompt** (normal — it needs admin access to install packages). Wait a few minutes for it to finish.

After install, **Run Pigeon** appears on your **Desktop**.

**Terminal alternative** (if you prefer):

```bash
cd ~
tar -xzf pigeon_0.6.0_raspberry_pi.tar.gz
cd Pigeon_0.6.0
./install_pigeon.sh
```

The installer:

- Installs system packages (`python3-tk`, PortAudio for the mic visualizer, OpenCV libs, etc.)
- Copies Pigeon to `~/Pigeon_0.6.0`
- Creates the Python virtualenv and installs `requirements.txt`
- Adds **Run Pigeon** and **Install Pigeon** icons on the Desktop
- Registers a **systemd** service for optional boot autostart

### 4. Run Pigeon

**Double-click `Run Pigeon` on the Desktop.**

Or open the install folder and double-click **Run Pigeon** there.

**Terminal alternative:**

**Autostart after login:**

```bash
sudo systemctl start pigeon
sudo systemctl status pigeon
journalctl -u pigeon -f    # live logs
```

Disable autostart: `sudo systemctl disable --now pigeon`

## Display (800×480)

Pigeon on Pi runs **fullscreen** with a **800×480** window target. The UI is composed at **800×400**; extra space is **black letterbox bars** (or pillarbox if the monitor aspect differs).

To use a fixed window instead of fullscreen:

```bash
PIGEON_PI_FULLSCREEN=0 ./run_pigeon_0_6.sh
```

Optional overrides: `PIGEON_DISPLAY_W`, `PIGEON_DISPLAY_H` (default 800×480).

## Kiosk tips

- Set **Raspberry Pi OS** to auto-login to desktop (Preferences → Raspberry Pi Configuration → System → Auto Login).
- For a dedicated display, resize the Pigeon window to fullscreen from the window manager, or use your WM’s fullscreen shortcut after launch.
- State, pyatv credentials, and TMDb tokens live in `~/.pigeon_0_6` on the Pi (same as on Mac).

## Mic visualizer (view 2)

`sounddevice` needs PortAudio and a working capture device. The installer pulls in `libportaudio2`. If the Pi has no microphone, view 2 may be quiet — the rest of Pigeon still works.

## Differences from Mac

| Feature | Mac | Raspberry Pi |
|--------|-----|----------------|
| Launcher | `run_pigeon_0_6.command` | `run_pigeon_0_6.sh` |
| Splash video HW decode | VideoToolbox | Software decode via OpenCV (PNG splash also works) |
| Fonts | Menlo | DejaVu / system sans |
| Apple TV pairing | Full pyatv | pyatv works on Linux — pair from Settings on the Pi |

## Troubleshooting

**`no Python 3.10+ with tkinter found`**

```bash
sudo apt install python3 python3-venv python3-tk
```

**Black window / no display (systemd)**

- Ensure the Pi boots to desktop and `DISPLAY=:0` matches your session.
- Try manual `./run_pigeon_0_6.sh` from a terminal on the Pi first.

**`pip install` slow or fails on Pi**

- First install can take several minutes on older Pis. Ensure the Pi has internet and enough free disk (~500 MB for venv + packages).

**OpenCV import errors**

```bash
sudo apt install libgl1 libglib2.0-0 libgomp1
```

## Files in this folder

| File | Purpose |
|------|---------|
| `package_for_pi.sh` | Run on Mac — builds the `.tar.gz` |
| `install_on_pi.sh` | Run on Pi — apt deps, venv, systemd |
| `pigeon.service` | systemd unit template |
| `README_RASPBERRY_PI.md` | This guide |
