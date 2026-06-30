"""Download and apply Pigeon app updates from GitHub (settings-safe)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from pigeon.runtime_paths import PIGEON_STATE_DIR_TILDE, pigeon_state_dir
from pigeon.update_check import _branch_candidates, github_auth_headers, github_repo_url

_UA = "Pigeon/0.7 (github-update)"
_LAUNCHER_NAMES = ("run_pigeon_0_7.sh", "run_pigeon_0_6.sh", "Run-Pigeon", "run-pigeon.sh")
_INSTALLER_DIR = "installer"
_MAIN_PY_NAMES = ("pigeon_0_7.py", "pigeon_0_6.py")


@dataclass(frozen=True)
class ApplyUpdateResult:
    ok: bool
    message: str
    remote_version: str | None = None


def _has_launcher(root: Path) -> bool:
    """True if ``root`` has a known launcher in ``installer/`` (or legacy at root)."""
    installer = root / _INSTALLER_DIR
    for name in _LAUNCHER_NAMES:
        if (installer / name).is_file():
            return True
        if (root / name).is_file():
            return True
    return False


def resolve_install_root(*, script_path: str | Path | None = None) -> Path | None:
    """Return the Pigeon app folder (contains ``installer/`` launchers and ``pigeonSystem/``)."""
    candidates: list[Path] = []
    if script_path:
        p = Path(script_path).resolve()
        candidates.extend([p.parent.parent, p.parent.parent.parent])
    env = os.environ.get("PIGEON_INSTALL_DIR", "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    cwd = Path.cwd()
    candidates.extend([cwd, cwd.parent])
    seen: set[str] = set()
    for base in candidates:
        b = base.resolve()
        key = str(b)
        if key in seen:
            continue
        seen.add(key)
        if any((b / "pigeonSystem" / name).is_file() for name in _MAIN_PY_NAMES) and _has_launcher(b):
            return b
    return None


def github_zipball_url(*, branch: str) -> str:
    user = os.environ.get("PIGEON_UPDATE_GITHUB_USER", "jasonhenle").strip()
    repo = os.environ.get("PIGEON_UPDATE_GITHUB_REPO", "pigeon_0.7.x").strip()
    br = branch.strip()
    return f"https://github.com/{user}/{repo}/archive/refs/heads/{br}.zip"


def github_full_download_page_url() -> str:
    """Browser URL for downloading the full repository (zip of default branch)."""
    branch = _branch_candidates()[0]
    return github_zipball_url(branch=branch)


def _find_app_root_in_tree(root: Path) -> Path | None:
    if any((root / "pigeonSystem" / name).is_file() for name in _MAIN_PY_NAMES) and _has_launcher(root):
        return root
    try:
        for pattern in ("run_pigeon_0_7.sh", "run_pigeon_0_6.sh"):
            for launcher in root.rglob(pattern):
                parent = launcher.parent
                if parent.name == _INSTALLER_DIR:
                    parent = parent.parent
                if any((parent / "pigeonSystem" / name).is_file() for name in _MAIN_PY_NAMES):
                    return parent
    except OSError:
        pass
    return None


def _rsync_merge(source: Path, dest: Path) -> tuple[bool, str]:
    excludes = [
        "pigeonSystem/.venv",
        "pigeonCashe",
        "pigeonTMDB",
        "raspberryPi/dist",
        "pigeonSystem/__pycache__",
        ".DS_Store",
    ]
    if shutil.which("rsync"):
        cmd = ["rsync", "-a"]
        for ex in excludes:
            cmd.append(f"--exclude={ex}")
        cmd.extend([f"{source}/", f"{dest}/"])
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except OSError as e:
            return False, str(e)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return False, err or f"rsync exited {proc.returncode}"
        return True, "Files merged."

    # Fallback when rsync is unavailable (minimal copy of pigeonSystem + launchers).
    try:
        dst_installer = dest / _INSTALLER_DIR
        dst_installer.mkdir(parents=True, exist_ok=True)
        for name in _LAUNCHER_NAMES:
            src_f = source / _INSTALLER_DIR / name
            if not src_f.is_file():
                src_f = source / name
            if src_f.is_file():
                shutil.copy2(src_f, dst_installer / name)
        src_sys = source / "pigeonSystem"
        dst_sys = dest / "pigeonSystem"
        if src_sys.is_dir():
            if dst_sys.exists():
                shutil.rmtree(dst_sys)
            shutil.copytree(
                src_sys,
                dst_sys,
                ignore=shutil.ignore_patterns(".venv", "__pycache__", ".cursor"),
            )
        for sub in ("installer", "raspberryPi"):
            src_sub = source / sub
            if src_sub.is_dir():
                dst_sub = dest / sub
                if dst_sub.exists():
                    shutil.rmtree(dst_sub)
                shutil.copytree(src_sub, dst_sub)
    except OSError as e:
        return False, str(e)
    return True, "Files copied (rsync not found; partial sync)."


def _run_bootstrap(install_root: Path) -> tuple[bool, str]:
    installer = install_root / _INSTALLER_DIR
    launcher = installer / "run_pigeon_0_7.sh"
    if not launcher.is_file():
        launcher = installer / "run_pigeon_0_6.sh"
    if not launcher.is_file():
        launcher = install_root / "run_pigeon_0_7.sh"
    if not launcher.is_file():
        launcher = install_root / "run_pigeon_0_6.sh"
    if not launcher.is_file():
        return True, "Skipped pip bootstrap (launcher missing)."
    try:
        proc = subprocess.run(
            ["bash", str(launcher), "--bootstrap-only"],
            cwd=str(install_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-500:].strip()
        return False, tail or f"bootstrap exited {proc.returncode}"
    return True, "Python dependencies refreshed."


def apply_github_update(
    install_root: Path,
    *,
    branch: str | None = None,
    timeout_s: float = 120.0,
) -> ApplyUpdateResult:
    """
    Download GitHub zipball and merge into ``install_root``.

    Does **not** modify ``~/.pigeon_0_6`` (devices, TMDb keys, pairing, locations).
    Does **not** replace ``pigeonTMDB/`` cached artwork or ``pigeonSystem/.venv`` (re-bootstrap after).
    """
    br = (branch or _branch_candidates()[0]).strip()
    url = github_zipball_url(branch=br)
    install_root = install_root.resolve()
    state_dir = pigeon_state_dir()

    tmp_zip = tmp_dir = None
    try:
        tmp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp_zip.close()
        zip_path = Path(tmp_zip.name)
        req = urlopen(Request(url, headers=github_auth_headers(user_agent=_UA)), timeout=timeout_s)  # noqa: S310
        try:
            zip_path.write_bytes(req.read())
        finally:
            req.close()

        tmp_dir = Path(tempfile.mkdtemp(prefix="pigeon-update-"))
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp_dir)

        extracted_roots = [p for p in tmp_dir.iterdir() if p.is_dir()]
        if not extracted_roots:
            return ApplyUpdateResult(False, "Downloaded archive was empty.")
        app_src = _find_app_root_in_tree(extracted_roots[0])
        if app_src is None:
            app_src = _find_app_root_in_tree(tmp_dir)
        if app_src is None:
            return ApplyUpdateResult(
                False,
                "Could not find Pigeon app folder (installer/run_pigeon_0_7.sh) inside GitHub zip.",
            )

        ok, msg = _rsync_merge(app_src, install_root)
        if not ok:
            return ApplyUpdateResult(False, f"Could not install update: {msg}")

        ok_b, msg_b = _run_bootstrap(install_root)
        if not ok_b:
            return ApplyUpdateResult(
                False,
                f"Update files installed but pip bootstrap failed:\n{msg_b}",
            )

        return ApplyUpdateResult(
            True,
            f"Updated from GitHub ({br}).\n\n"
            f"Your settings in {PIGEON_STATE_DIR_TILDE} ({state_dir}) were not changed.\n"
            f"Cached TMDb art in the app folder was kept.\n\n"
            f"{msg_b}\n\nQuit and relaunch Pigeon to run the new version.",
        )
    except Exception as e:
        return ApplyUpdateResult(False, str(e))
    finally:
        if tmp_zip is not None:
            try:
                Path(tmp_zip.name).unlink(missing_ok=True)
            except OSError:
                pass
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
