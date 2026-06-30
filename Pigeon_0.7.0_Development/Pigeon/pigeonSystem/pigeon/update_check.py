"""Compare local Pigeon version against version.py on GitHub."""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from pigeon.version import version_string, version_tuple

_DEFAULT_GITHUB_USER = "jasonhenle"
_DEFAULT_GITHUB_REPO = "pigeon_0.7.x"
_DEFAULT_GITHUB_BRANCH = "main"
_DEFAULT_VERSION_PATHS: tuple[str, ...] = (
    "Pigeon_0.7.0_Development/Pigeon/pigeonSystem/pigeon/version.py",
    "Pigeon_0.7.0_Development/Pigeon_0.7.0/pigeonSystem/pigeon/version.py",
    "Pigeon/pigeonSystem/pigeon/version.py",
    "Pigeon_0.7.0/pigeonSystem/pigeon/version.py",
    "Pigeon_0.6.0_Development/Pigeon_0.6.0/pigeonSystem/pigeon/version.py",
    "Pigeon_0.6.0/pigeonSystem/pigeon/version.py",
    "pigeonSystem/pigeon/version.py",
)
_UA = "Pigeon/0.7 (update-check)"
_VERSION_FIELD_RE = re.compile(r"^(MAJOR|MINOR|PATCH)\s*=\s*(\d+)\s*$", re.MULTILINE)


def _latin1_header(value: str) -> str:
    """HTTP/1.1 header values must be encodable as latin-1."""
    return value.encode("latin-1", errors="ignore").decode("latin-1")


def _sanitize_github_token(raw: str) -> str:
    """Strip BOM, whitespace (incl. U+202F), and non-ASCII from pasted tokens."""
    if not raw:
        return ""
    cleaned: list[str] = []
    for ch in raw.lstrip("\ufeff"):
        if ch.isspace():
            continue
        if ord(ch) < 128:
            cleaned.append(ch)
    return "".join(cleaned)


def github_token() -> str:
    """Token from env or ``~/.pigeon_0_6/github_update_token``."""
    token = os.environ.get("PIGEON_UPDATE_GITHUB_TOKEN", "").strip()
    if not token:
        token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        try:
            token_path = Path.home() / ".pigeon_0_6" / "github_update_token"
            if token_path.is_file():
                token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    return _sanitize_github_token(token)


def github_auth_headers(*, user_agent: str | None = None) -> dict[str, str]:
    """Optional token for private repos."""
    headers = {"User-Agent": _latin1_header(user_agent or _UA)}
    token = github_token()
    if token:
        headers["Authorization"] = _latin1_header(f"Bearer {token}")
    return headers


@dataclass(frozen=True)
class UpdateCheckResult:
    local_version: str
    remote_version: str | None
    update_available: bool
    error: str | None = None
    source_url: str | None = None
    github_branch: str | None = None


def github_repo_url() -> str:
    user = os.environ.get("PIGEON_UPDATE_GITHUB_USER", _DEFAULT_GITHUB_USER).strip()
    repo = os.environ.get("PIGEON_UPDATE_GITHUB_REPO", _DEFAULT_GITHUB_REPO).strip()
    return f"https://github.com/{user}/{repo}"


def _branch_candidates() -> list[str]:
    out: list[str] = []
    env_branch = os.environ.get("PIGEON_UPDATE_GITHUB_BRANCH", "").strip()
    if env_branch:
        out.append(env_branch)
    for b in (_DEFAULT_GITHUB_BRANCH, "main", "master"):
        if b and b not in out:
            out.append(b)
    return out


def _path_candidates() -> list[str]:
    env_path = os.environ.get("PIGEON_UPDATE_VERSION_PATH", "").strip().lstrip("/")
    if env_path:
        return [env_path]
    return list(_DEFAULT_VERSION_PATHS)


def version_py_raw_url(*, branch: str | None = None, path: str | None = None) -> str:
    override = os.environ.get("PIGEON_UPDATE_GITHUB_RAW", "").strip()
    if override:
        return override
    user = os.environ.get("PIGEON_UPDATE_GITHUB_USER", _DEFAULT_GITHUB_USER).strip()
    repo = os.environ.get("PIGEON_UPDATE_GITHUB_REPO", _DEFAULT_GITHUB_REPO).strip()
    br = (branch or _branch_candidates()[0]).strip()
    rel = (path or _path_candidates()[0]).strip().lstrip("/")
    return f"https://raw.githubusercontent.com/{user}/{repo}/{br}/{rel}"


def version_py_api_url(*, branch: str | None = None, path: str | None = None) -> str:
    from urllib.parse import quote

    user = os.environ.get("PIGEON_UPDATE_GITHUB_USER", _DEFAULT_GITHUB_USER).strip()
    repo = os.environ.get("PIGEON_UPDATE_GITHUB_REPO", _DEFAULT_GITHUB_REPO).strip()
    br = (branch or _branch_candidates()[0]).strip()
    rel = (path or _path_candidates()[0]).strip().lstrip("/")
    return f"https://api.github.com/repos/{user}/{repo}/contents/{quote(rel, safe='/')}?ref={br}"


def _private_repo_hint() -> str:
    user = os.environ.get("PIGEON_UPDATE_GITHUB_USER", _DEFAULT_GITHUB_USER).strip()
    repo = os.environ.get("PIGEON_UPDATE_GITHUB_REPO", _DEFAULT_GITHUB_REPO).strip()
    return (
        f"The GitHub repo ({user}/{repo}) is private.\n\n"
        "Create a one-line file on this Pi:\n"
        "  ~/.pigeon_0_6/github_update_token\n\n"
        "Put a GitHub personal access token with read access to that repo, "
        "then tap Updates again.\n\n"
        "Or reinstall from a fresh pigeon_*_raspberry_pi.tar.gz built on your Mac."
    )


def _fetch_version_text(url: str, *, timeout_s: float, api: bool = False) -> tuple[str | None, str | None]:
    headers = github_auth_headers()
    if api:
        headers["Accept"] = _latin1_header("application/vnd.github.raw")
        headers["X-GitHub-Api-Version"] = _latin1_header("2022-11-28")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.read().decode("utf-8", errors="replace"), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, f"Network error: {e.reason}"
    except OSError as e:
        return None, str(e)


def fetch_remote_version_tuple(*, timeout_s: float = 12.0) -> tuple[tuple[int, int, int] | None, str | None, str | None, str | None]:
    """Return ``(remote_tuple, error_message, winning_raw_url, github_branch)``."""
    token = github_token()
    saw_404 = False
    saw_auth_fail = False
    for branch in _branch_candidates():
        for path in _path_candidates():
            attempts: list[tuple[str, bool]] = []
            if token:
                attempts.append((version_py_api_url(branch=branch, path=path), True))
            attempts.append((version_py_raw_url(branch=branch, path=path), False))
            for url, api in attempts:
                body, err = _fetch_version_text(url, timeout_s=timeout_s, api=api)
                if body is None:
                    if err == "HTTP 404":
                        saw_404 = True
                    elif err in ("HTTP 401", "HTTP 403"):
                        saw_auth_fail = True
                    continue
                remote = parse_version_py(body)
                if remote is None:
                    continue
                return remote, None, url, branch
    if not token and saw_404:
        return None, _private_repo_hint(), version_py_raw_url(), None
    if saw_auth_fail:
        return (
            None,
            "GitHub rejected the update token (HTTP 401/403).\n\n"
            "Check ~/.pigeon_0_6/github_update_token — it needs read access to the repo.",
            version_py_raw_url(),
            None,
        )
    if saw_404:
        return (
            None,
            "version.py was not found on GitHub for branch "
            f"{_branch_candidates()[0]!r}. Push the latest code to main.",
            version_py_raw_url(),
            None,
        )
    return None, "Could not reach GitHub to check for updates.", version_py_raw_url(), None


def parse_version_py(text: str) -> tuple[int, int, int] | None:
    found: dict[str, int] = {}
    for m in _VERSION_FIELD_RE.finditer(text or ""):
        found[m.group(1)] = int(m.group(2))
    if not {"MAJOR", "MINOR", "PATCH"}.issubset(found.keys()):
        return None
    return (found["MAJOR"], found["MINOR"], found["PATCH"])


def format_version_tuple(t: tuple[int, int, int]) -> str:
    return f"{t[0]}.{t[1]}.{t[2]}"


def check_for_update(*, timeout_s: float = 12.0) -> UpdateCheckResult:
    local = version_string()
    local_t = version_tuple()
    remote_t, err, url, branch = fetch_remote_version_tuple(timeout_s=timeout_s)
    if remote_t is None:
        return UpdateCheckResult(
            local_version=local,
            remote_version=None,
            update_available=False,
            error=err,
            source_url=url,
            github_branch=branch,
        )
    remote_s = format_version_tuple(remote_t)
    return UpdateCheckResult(
        local_version=local,
        remote_version=remote_s,
        update_available=remote_t > local_t,
        error=None,
        source_url=url,
        github_branch=branch,
    )
