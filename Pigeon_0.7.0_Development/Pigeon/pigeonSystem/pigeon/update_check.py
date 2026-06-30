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


def _ascii_only(value: str) -> str:
    """Keep printable ASCII only (HTTP URLs and slugs)."""
    return "".join(ch for ch in value if 32 <= ord(ch) < 127)


def _latin1_header(value: str) -> str:
    """HTTP/1.1 header values must be encodable as latin-1."""
    return value.encode("latin-1", errors="ignore").decode("latin-1")


def _latin1_safe_env() -> dict[str, str]:
    """Environment dict safe for subprocess (no U+202F etc. in values)."""
    out: dict[str, str] = {}
    for key, val in os.environ.items():
        ks = str(key)
        vs = str(val)
        try:
            ks.encode("latin-1")
            vs.encode("latin-1")
        except UnicodeEncodeError:
            ks = ks.encode("latin-1", errors="replace").decode("latin-1")
            vs = vs.encode("latin-1", errors="replace").decode("latin-1")
        out[ks] = vs
    return out


def _scrub_github_token_file() -> None:
    """Rewrite token file as ASCII-only (fixes pasted narrow no-break spaces)."""
    try:
        token_path = Path.home() / ".pigeon_0_6" / "github_update_token"
        if not token_path.is_file():
            return
        cleaned = _sanitize_github_token(token_path.read_bytes())
        if cleaned:
            token_path.write_text(cleaned + "\n", encoding="ascii")
        else:
            token_path.unlink(missing_ok=True)
    except OSError:
        pass


def _sanitize_github_token(raw: str) -> str:
    """Strip BOM, whitespace (incl. U+202F), and non-ASCII from pasted tokens."""
    if not raw:
        return ""
    if isinstance(raw, bytes):
        return bytes(b for b in raw if 32 <= b < 127).decode("ascii")
    cleaned: list[str] = []
    for ch in raw.lstrip("\ufeff"):
        if ch.isspace():
            continue
        if ord(ch) < 128:
            cleaned.append(ch)
    return "".join(cleaned)


def _github_repo_name() -> str:
    return _ascii_only(os.environ.get("PIGEON_UPDATE_GITHUB_REPO", _DEFAULT_GITHUB_REPO).strip())


def _github_repo_is_public() -> bool:
    return _github_repo_name() == "pigeon_0.7.x"


def github_token() -> str:
    """Token from env or ``~/.pigeon_0_6/github_update_token`` (skipped for public repo)."""
    if _github_repo_is_public() and os.environ.get("PIGEON_UPDATE_REQUIRE_TOKEN", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        return ""
    token = os.environ.get("PIGEON_UPDATE_GITHUB_TOKEN", "")
    if not token.strip():
        token = os.environ.get("GITHUB_TOKEN", "")
    if not token.strip():
        try:
            token_path = Path.home() / ".pigeon_0_6" / "github_update_token"
            if token_path.is_file():
                token = token_path.read_bytes()
        except OSError:
            token = b""
    return _sanitize_github_token(token if isinstance(token, bytes) else str(token))


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
    user = _ascii_only(os.environ.get("PIGEON_UPDATE_GITHUB_USER", _DEFAULT_GITHUB_USER).strip())
    repo = _github_repo_name()
    return f"https://github.com/{user}/{repo}"


def github_http_get(url: str, *, timeout_s: float, headers: dict[str, str] | None = None) -> bytes:
    """HTTPS GET with latin-1-safe headers; follows redirects (GitHub zipballs → codeload)."""
    import http.client
    import shutil
    import subprocess
    from urllib.parse import urljoin, urlparse

    safe_headers = {
        _latin1_header(str(k)): _latin1_header(str(v)) for k, v in (headers or {}).items()
    }
    safe_url = _ascii_only(url)

    curl = shutil.which("curl")
    if curl:
        cmd = [
            curl,
            "-fsSL",
            "--max-time",
            str(max(1, int(timeout_s))),
        ]
        for hk, hv in safe_headers.items():
            if hk and hv is not None:
                cmd.extend(["-H", f"{hk}: {hv}"])
        cmd.append(safe_url)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                env=_latin1_safe_env(),
            )
        except (OSError, UnicodeEncodeError):
            proc = None
        if proc is not None and proc.returncode == 0 and proc.stdout:
            return proc.stdout

    current = safe_url
    max_redirects = 8

    for _ in range(max_redirects + 1):
        parsed = urlparse(current)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError(f"Invalid GitHub URL: {url!r}")
        host = _ascii_only(parsed.hostname)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        path = _ascii_only(path)
        conn = http.client.HTTPSConnection(host, parsed.port or 443, timeout=timeout_s)
        try:
            conn.request("GET", path, headers=safe_headers)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                if not location:
                    raise urllib.error.HTTPError(
                        current,
                        resp.status,
                        resp.reason or "redirect without Location",
                        resp.headers,
                        None,
                    )
                current = _ascii_only(urljoin(current, location))
                continue
            if resp.status >= 400:
                raise urllib.error.HTTPError(
                    current,
                    resp.status,
                    resp.reason,
                    resp.headers,
                    None,
                )
            return body
        finally:
            conn.close()

    raise urllib.error.HTTPError(url, 302, "Too many redirects", {}, None)


def _branch_candidates() -> list[str]:
    out: list[str] = []
    env_branch = _ascii_only(os.environ.get("PIGEON_UPDATE_GITHUB_BRANCH", "").strip())
    if env_branch:
        out.append(env_branch)
    for b in (_DEFAULT_GITHUB_BRANCH, "main", "master"):
        if b and b not in out:
            out.append(b)
    return out


def _path_candidates() -> list[str]:
    env_path = _ascii_only(os.environ.get("PIGEON_UPDATE_VERSION_PATH", "").strip().lstrip("/"))
    if env_path:
        return [env_path]
    return list(_DEFAULT_VERSION_PATHS)


def version_py_raw_url(*, branch: str | None = None, path: str | None = None) -> str:
    override = os.environ.get("PIGEON_UPDATE_GITHUB_RAW", "").strip()
    if override:
        return _ascii_only(override)
    user = _ascii_only(os.environ.get("PIGEON_UPDATE_GITHUB_USER", _DEFAULT_GITHUB_USER).strip())
    repo = _github_repo_name()
    br = _ascii_only((branch or _branch_candidates()[0]).strip())
    rel = _ascii_only((path or _path_candidates()[0]).strip().lstrip("/"))
    return f"https://raw.githubusercontent.com/{user}/{repo}/{br}/{rel}"


def version_py_api_url(*, branch: str | None = None, path: str | None = None) -> str:
    from urllib.parse import quote

    user = _ascii_only(os.environ.get("PIGEON_UPDATE_GITHUB_USER", _DEFAULT_GITHUB_USER).strip())
    repo = _github_repo_name()
    br = _ascii_only((branch or _branch_candidates()[0]).strip())
    rel = _ascii_only((path or _path_candidates()[0]).strip().lstrip("/"))
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
    try:
        body = github_http_get(url, timeout_s=timeout_s, headers=headers)
        return body.decode("utf-8", errors="replace"), None
    except UnicodeEncodeError as e:
        return None, f"Encoding error talking to GitHub: {e}"
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
    _scrub_github_token_file()
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
