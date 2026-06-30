# Pigeon and GitHub

Repository: **https://github.com/jasonhenle/pigeon_0.7.x**  
Default branch for downloads/updates: **`main`** (override with env `PIGEON_UPDATE_GITHUB_BRANCH`).

Pigeon splits **app files** (this folder) from **your settings** (`~/.pigeon_0_6/`). Updates never delete settings.

---

## 1. Full download (install Pigeon from scratch)

Use this for a new Mac, a new Pi, or a second machine.

### Option A — Download ZIP (no git)

1. Open:  
   **https://github.com/jasonhenle/pigeon_0.7.x/archive/refs/heads/main.zip**
2. Unzip the archive.
3. Go to:  
   **`Pigeon_0.7.0_Development/Pigeon/`** inside the extracted folder.
4. **Mac:** run `installer/install_pigeon.command` or `./installer/install_pigeon.sh`  
   **Pi:** open `installer/` and double-click **Install-Pigeon** (see `installer/START-HERE.txt`).

### Option B — Clone with git

```bash
git clone https://github.com/jasonhenle/pigeon_0.7.x.git
cd pigeon_0.7.x/Pigeon_0.7.0_Development/Pigeon
./installer/install_pigeon.sh   # Mac
# or installer/Install-Pigeon on Pi
```

### What lives where after install

| Location | Contents |
|----------|----------|
| App folder (e.g. `~/Pigeon_0.7.7`, `~/Applications/Pigeon_0.7.7`) | Code, launcher, venv, local TMDb cache folders |
| `~/.pigeon_0_6/` | **Settings:** locations, devices, TMDb API key, Apple TV pairing, logs |

Copy **`~/.pigeon_0_6/`** from another machine to move settings (see `installer/setup/README.txt` on Pi).

---

## 2. In-app update (Settings → red **Updates** button)

When GitHub has a **newer** `pigeonSystem/pigeon/version.py` than your installed copy:

1. Open **Settings** (Shift+Tab in developer mode).
2. Tap the red **Updates** button.
3. Confirm **Install update**.

Pigeon will:

- Download the latest app tree from GitHub (zip of the tracked branch).
- Merge into your **current install folder** only.
- Refresh Python dependencies (`installer/run_pigeon_0_7.sh --bootstrap-only`).
- **Not** modify `~/.pigeon_0_6/` (devices, TMDb key, pairing, locations).
- **Not** replace `pigeonTMDB/` cached artwork or `pigeonSystem/.venv` (venv is refreshed, not deleted blindly).

Quit and relaunch Pigeon when prompted.

If the button stays gray, you are already on the latest version GitHub reports, or `version.py` is not yet pushed to the branch Pigeon checks.

### Optional GitHub token

`jasonhenle/pigeon_0.7.x` is **public**, so Updates work without a token on Mac and Pi.

For a private fork or heavy API use, set `~/.pigeon_0_6/github_update_token` (one-line PAT) or `PIGEON_UPDATE_GITHUB_TOKEN` in the environment.

---

## Version bumps (for maintainers)

On each release pushed to GitHub, bump **`pigeonSystem/pigeon/version.py`** (`MAJOR` / `MINOR` / `PATCH`) and push. Installed copies compare that file to decide if **Updates** should turn red.

- **Patch** — small fixes (`0.7.6` → `0.7.7`)
- **Minor** — feature releases (human-driven)
- **Major** — breaking / rebrand (future `1.0.0`)

---

## Environment overrides

| Variable | Purpose |
|----------|---------|
| `PIGEON_UPDATE_GITHUB_USER` | Default `jasonhenle` |
| `PIGEON_UPDATE_GITHUB_REPO` | Default `pigeon_0.7.x` |
| `PIGEON_UPDATE_GITHUB_BRANCH` | Branch for version check and zip download (default `main`) |
| `PIGEON_UPDATE_GITHUB_TOKEN` | Optional GitHub PAT |
| `PIGEON_UPDATE_GITHUB_RAW` | Full URL to `version.py` (advanced) |
| `PIGEON_STATE_DIR` | Settings directory (default `~/.pigeon_0_6`) |
