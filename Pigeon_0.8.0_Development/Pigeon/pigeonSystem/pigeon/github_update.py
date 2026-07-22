"""Download and apply Pigeon app updates from GitHub (settings-safe)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from pigeon.runtime_paths import PIGEON_STATE_DIR_TILDE, pigeon_state_dir
from pigeon.update_check import (
    _branch_candidates,
    _minimal_subprocess_env,
    prepare_github_update_environment,
    github_auth_headers,
    github_http_get,
    safe_subprocess_path,
)
from pigeon.update_assets import ensure_required_assets

_UA = "Pigeon/0.8 (github-update)"
_SHELL_UPDATE_SCRIPT = "pigeon_github_update.sh"
_LEGACY_SHELL_UPDATE_SCRIPT = "pi_update_from_github.sh"
_BOOTSTRAP_SCRIPT_RAW = (
    "https://raw.githubusercontent.com/jasonhenle/pigeon_0.7.x/main/"
    "Pigeon_0.8.0_Development/Pigeon/installer/pigeon_github_update.sh"
)
_LAUNCHER_NAMES = (
    "run_pigeon_0_8.command",  # macOS double-click
    "run_pigeon_0_8.sh",
    "run_pigeon_0_7.sh",
    "run_pigeon_0_6.sh",
    "Run-Pigeon",
    "run-pigeon.sh",
)
_INSTALLER_DIR = "installer"
_MAIN_PY_NAMES = ("pigeon_0_8.py", "pigeon_0_7.py", "pigeon_0_6.py")
_PREFERRED_APP_REL = Path("Pigeon_0.8.0_Development") / "Pigeon"


@dataclass(frozen=True)
class ApplyUpdateResult:
    ok: bool
    message: str
    remote_version: str | None = None


def _find_launcher_script(install_root: Path) -> Path | None:
    installer = install_root / _INSTALLER_DIR
    for name in _LAUNCHER_NAMES:
        for base in (installer, install_root):
            p = base / name
            if p.is_file():
                return p
    return None


def _preferred_launcher_script(install_root: Path) -> Path | None:
    """Prefer the 0.8 entry point after a 0.7 -> 0.8 GitHub update."""
    installer = install_root / _INSTALLER_DIR
    for name in (
        "run_pigeon_0_8.sh",
        "run_pigeon_0_8.command",
        "click_run_pigeon_pi.sh",
        "Run-Pigeon",
        "run-pigeon.sh",
    ):
        for base in (installer, install_root):
            p = base / name
            if p.is_file():
                return p
    return _find_launcher_script(install_root)


def _systemd_unit_path() -> Path:
    return Path("/etc/systemd/system/pigeon.service")


def _systemd_points_to_launcher(install_root: Path, launcher_name: str) -> bool:
    unit = _systemd_unit_path()
    if not unit.is_file():
        return False
    try:
        text = unit.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    root = str(install_root.resolve())
    return launcher_name in text and root in text


def _try_systemd_restart() -> tuple[bool, str]:
    """Restart the pigeon systemd unit when installed (Pi autostart)."""
    if not sys.platform.startswith("linux"):
        return False, ""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, ""
    sudo = shutil.which("sudo")
    env = _minimal_subprocess_env()
    for unit in ("pigeon.service", "pigeon"):
        for cmd in (
            [sudo, "-n", systemctl, "restart", unit] if sudo else None,
            [systemctl, "restart", unit],
        ):
            if not cmd:
                continue
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20.0,
                    env=env,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if proc.returncode == 0:
                return True, " ".join(cmd)
    return False, ""


def _schedule_delayed_launch(
    launcher: Path,
    install_root: Path,
    *,
    parent_pid: int,
) -> tuple[bool, str]:
    """
    Detach a tiny helper that waits for ``parent_pid`` to exit, then relaunches Pigeon.

    Used on macOS and when systemd restart is unavailable (manual Pi/desktop launch).
    """
    install_root = Path(safe_subprocess_path(install_root.resolve()))
    launcher = Path(safe_subprocess_path(launcher.resolve()))
    bash = shutil.which("bash") or "/bin/bash"

    if sys.platform == "darwin" and launcher.suffix == ".command":
        open_bin = shutil.which("open") or "/usr/bin/open"
        launch_body = f'exec "{open_bin}" "{launcher}"'
    else:
        launch_body = (
            f'cd "{install_root}" && exec "{bash}" "{launcher}"'
        )

    script = "\n".join(
        (
            "#!/bin/bash",
            "set -euo pipefail",
            f"PID={int(parent_pid)}",
            "sleep 0.5",
            'while kill -0 "$PID" 2>/dev/null; do sleep 0.15; done',
            "sleep 0.35",
            launch_body,
        )
    )
    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".sh",
            prefix="pigeon-restart-",
            delete=False,
            encoding="utf-8",
        )
        tmp.write(script)
        tmp.flush()
        tmp.close()
        script_path = Path(tmp.name)
        script_path.chmod(0o700)
        subprocess.Popen(
            [bash, safe_subprocess_path(script_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=_minimal_subprocess_env(),
        )
    except OSError as e:
        return False, str(e)
    return True, f"delayed launcher ({launcher.name})"


def restart_pigeon_after_update(
    install_root: Path,
    *,
    parent_pid: int | None = None,
) -> tuple[bool, str]:
    """
    Schedule a post-update restart (systemd on Pi/Linux, else relaunch installer script).

    Safe to call from the Tk thread while the current process is still running; the new
    instance starts after this one exits (or systemd replaces the service).
    """
    install_root = Path(safe_subprocess_path(install_root.resolve()))
    pid = int(parent_pid if parent_pid is not None else os.getpid())
    launcher = _preferred_launcher_script(install_root)

    if sys.platform.startswith("linux"):
        launcher_08 = install_root / _INSTALLER_DIR / "run_pigeon_0_8.sh"
        if launcher_08.is_file():
            # Never systemd-restart back into a legacy 0.7 entry point after updating files.
            if _systemd_points_to_launcher(install_root, "run_pigeon_0_8.sh"):
                ok, msg = _try_systemd_restart()
                if ok:
                    return True, msg
            return _schedule_delayed_launch(launcher_08, install_root, parent_pid=pid)

    if launcher is None:
        return False, "no launcher script found"

    return _schedule_delayed_launch(launcher, install_root, parent_pid=pid)


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
        from pigeon.update_check import _ascii_only

        candidates.append(Path(_ascii_only(env)).expanduser())
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
    from pigeon.update_check import _ascii_only

    user = _ascii_only(os.environ.get("PIGEON_UPDATE_GITHUB_USER", "jasonhenle").strip())
    repo = _ascii_only(os.environ.get("PIGEON_UPDATE_GITHUB_REPO", "pigeon_0.7.x").strip())
    br = _ascii_only(branch.strip())
    return f"https://codeload.github.com/{user}/{repo}/zip/refs/heads/{br}"


def github_full_download_page_url() -> str:
    """Browser URL for downloading the full repository (zip of default branch)."""
    branch = _branch_candidates()[0]
    return github_zipball_url(branch=branch)


def _find_app_root_in_tree(root: Path) -> Path | None:
    """Locate the Pigeon app folder inside a GitHub zip extract (prefer 0.8 layout)."""
    bases: list[Path] = [root]
    if root.is_dir():
        bases.extend(p for p in root.iterdir() if p.is_dir())

    for base in bases:
        preferred = base / _PREFERRED_APP_REL
        if (preferred / "pigeonSystem" / "pigeon_0_8.py").is_file() and _has_launcher(preferred):
            return preferred
        if any((preferred / "pigeonSystem" / name).is_file() for name in _MAIN_PY_NAMES) and _has_launcher(
            preferred
        ):
            return preferred
        if any((base / "pigeonSystem" / name).is_file() for name in _MAIN_PY_NAMES) and _has_launcher(base):
            if (base / "pigeonSystem" / "pigeon_0_8.py").is_file():
                return base
            return base

    try:
        for pattern in ("run_pigeon_0_8.sh", "run_pigeon_0_7.sh", "run_pigeon_0_6.sh"):
            for launcher in sorted(root.rglob(pattern)):
                parent = launcher.parent
                if parent.name == _INSTALLER_DIR:
                    parent = parent.parent
                if (parent / "pigeonSystem" / "pigeon_0_8.py").is_file():
                    return parent
        for pattern in ("run_pigeon_0_8.sh", "run_pigeon_0_7.sh", "run_pigeon_0_6.sh"):
            for launcher in sorted(root.rglob(pattern)):
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
        cmd.extend([f"{safe_subprocess_path(source)}/", f"{safe_subprocess_path(dest)}/"])
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                env=_minimal_subprocess_env(),
            )
        except (OSError, UnicodeEncodeError) as e:
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
        for sub in ("installer", "raspberryPi", "pigeonAssets"):
            src_sub = source / sub
            if src_sub.is_dir():
                dst_sub = dest / sub
                if dst_sub.exists() and sub != "pigeonAssets":
                    shutil.rmtree(dst_sub)
                if sub == "pigeonAssets":
                    shutil.copytree(src_sub, dst_sub, dirs_exist_ok=True)
                else:
                    shutil.copytree(src_sub, dst_sub)
    except OSError as e:
        return False, str(e)
    return True, "Files copied (rsync not found; partial sync)."


def _run_bootstrap(install_root: Path) -> tuple[bool, str]:
    installer = install_root / _INSTALLER_DIR
    launcher = installer / "run_pigeon_0_8.sh"
    if not launcher.is_file():
        launcher = installer / "run_pigeon_0_6.sh"
    if not launcher.is_file():
        launcher = install_root / "run_pigeon_0_8.sh"
    if not launcher.is_file():
        launcher = install_root / "run_pigeon_0_6.sh"
    if not launcher.is_file():
        return True, "Skipped pip bootstrap (launcher missing)."
    try:
        proc = subprocess.run(
            ["bash", safe_subprocess_path(launcher), "--bootstrap-only"],
            cwd=safe_subprocess_path(install_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=600,
            env=_minimal_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeEncodeError) as e:
        return False, str(e)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-500:].strip()
        return False, tail or f"bootstrap exited {proc.returncode}"
    return True, "Python dependencies refreshed."


def _display_path(path: Path) -> str:
    """User-visible path safe for Tk / latin-1 transports."""
    return str(path).encode("ascii", errors="replace").decode("ascii")


def _apply_linux_shell_update(install_root: Path) -> ApplyUpdateResult:
    """
    Pi/Linux: always run a fresh curl|bash updater from GitHub (never Python http.client).

    Fetches ``pigeon_github_update.sh`` from raw.githubusercontent.com every time so this
    works even when the installed copy is several versions behind.
    """
    if not sys.platform.startswith("linux"):
        return ApplyUpdateResult(False, "Internal error: shell update called off Linux.")
    bash = shutil.which("bash")
    curl = shutil.which("curl")
    if not bash or not curl:
        return ApplyUpdateResult(
            False,
            "Linux update requires bash and curl.\n\nRun: sudo apt install curl",
        )
    root = safe_subprocess_path(install_root.resolve())
    env = _minimal_subprocess_env()
    env["PIGEON_UPDATE_URL"] = _BOOTSTRAP_SCRIPT_RAW
    env["PIGEON_INSTALL_ROOT"] = root
    env["PIGEON_UPDATE_IN_APP"] = "1"
    env["PIGEON_UPDATE_PARENT_PID"] = str(os.getpid())
    try:
        proc = subprocess.run(
            [
                bash,
                "-c",
                'curl -fsSL "$PIGEON_UPDATE_URL" | bash -s -- "$PIGEON_INSTALL_ROOT"',
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=900,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeEncodeError) as e:
        return ApplyUpdateResult(False, f"Shell update failed: {e}")
    combined = "\n".join(x for x in ((proc.stdout or "").strip(), (proc.stderr or "").strip()) if x)
    try:
        from pigeon.pi_diagnostics import append_pigeon_log

        for line in combined.splitlines()[-20:]:
            append_pigeon_log(line)
    except Exception:
        pass
    if proc.returncode != 0:
        return ApplyUpdateResult(
            False,
            "GitHub update script failed.\n\n"
            + (combined[-1500:] if combined else f"exit code {proc.returncode}")
            + "\n\nFrom a Pi terminal you can also run:\n"
            "  curl -fsSL \"$URL\" | bash -s -- ~/Pigeon_*\n"
            f"  URL={_BOOTSTRAP_SCRIPT_RAW}",
        )
    ver = ""
    m = re.search(r"Pigeon (\d+\.\d+\.\d+) installed", combined)
    if m:
        ver = m.group(1)
    state_dir = pigeon_state_dir()
    return ApplyUpdateResult(
        True,
        "Updated from GitHub (curl shell).\n\n"
        f"Your settings in {PIGEON_STATE_DIR_TILDE} ({_display_path(state_dir)}) were not changed.\n\n"
        + (combined.split("\n")[-2:] and "\n".join(combined.split("\n")[-2:]) or "Update finished.")
        + "\n\nPigeon will restart automatically.",
        remote_version=ver or None,
    )


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
    Merges ``pigeonAssets/`` (status bar, logos, poster chrome) from GitHub.
    """
    prepare_github_update_environment()
    install_root = Path(safe_subprocess_path(install_root.resolve()))

    if sys.platform.startswith("linux"):
        return _apply_linux_shell_update(install_root)

    br = (branch or _branch_candidates()[0]).strip()

    url = github_zipball_url(branch=br)
    state_dir = pigeon_state_dir()

    tmp_zip = tmp_dir = None
    try:
        tmp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp_zip.close()
        zip_path = Path(tmp_zip.name)
        body = github_http_get(url, timeout_s=timeout_s, headers=github_auth_headers(user_agent=_UA))
        if len(body) < 4 or body[:2] != b"PK":
            snippet = body[:240].decode("utf-8", errors="replace").strip()
            return ApplyUpdateResult(
                False,
                "GitHub did not return a zip archive (download may have been blocked or redirected).\n\n"
                + (snippet[:200] if snippet else "(empty response)"),
            )
        zip_path.write_bytes(body)

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
                "Could not find Pigeon app folder (installer/run_pigeon_0_8.sh) inside GitHub zip.",
            )

        ok, msg = _rsync_merge(app_src, install_root)
        if not ok:
            return ApplyUpdateResult(False, f"Could not install update: {msg}")

        ok_a, msg_a = ensure_required_assets(
            install_root, source_root=app_src, branch=br
        )
        if not ok_a:
            return ApplyUpdateResult(
                False,
                f"Update installed but required UI assets could not be restored:\n\n{msg_a}",
            )

        ok_b, msg_b = _run_bootstrap(install_root)
        if not ok_b:
            return ApplyUpdateResult(
                False,
                f"Update files installed but pip bootstrap failed:\n{msg_b}",
            )

        return ApplyUpdateResult(
            True,
            f"Updated from GitHub ({br}).\n\n"
            f"Your settings in {PIGEON_STATE_DIR_TILDE} ({_display_path(state_dir)}) were not changed.\n"
            f"Cached TMDb art in the app folder was kept.\n\n"
            f"{msg_a}\n"
            f"{msg_b}\n\nPigeon will restart automatically.",
        )
    except UnicodeEncodeError as e:
        return ApplyUpdateResult(
            False,
            "Update failed due to a text encoding problem (often a bad character in "
            "github_update_token, GITHUB_TOKEN, or the install path).\n\n"
            "On Raspberry Pi, run once from a terminal:\n"
            "  rm -f ~/.pigeon_0_6/github_update_token\n"
            "  curl -fsSL -o /tmp/pigeon-install.sh \\\n"
            '    "https://raw.githubusercontent.com/jasonhenle/pigeon_0.7.x/main/'
            'Pigeon_0.8.0_Development/Pigeon/installer/install_from_github.sh"\n'
            "  bash /tmp/pigeon-install.sh\n\n"
            f"{e}",
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
