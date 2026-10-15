"""Small shared helpers: subprocess, JSON, HTTP with on-disk cache, logging."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", ".hg", ".venv", "venv", "env", ".tox", ".nox", "build", "dist",
             "node_modules", "__pycache__", "site-packages", ".mypy_cache", ".pytest_cache",
             ".depexit"}


def log(msg: str) -> None:
    print(f"[depexit {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


@dataclass
class RunResult:
    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str
    seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def text(self) -> str:
        return f"$ {' '.join(self.cmd)}\n[exit {self.returncode} in {self.seconds:.1f}s]\n{self.stdout}\n{self.stderr}"


# Variables passed through to untrusted repository code (installs, tests, probes).
# Everything else -- API keys, tokens, cloud credentials, DEPEXIT_LLM_* -- is dropped.
SAFE_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS", "OS", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
    "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_INDEX_URL", "UV_DEFAULT_INDEX", "PIP_INDEX_URL",
    "PIP_CACHE_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
}
SECRET_RE = None


def safe_env(extra: dict | None = None) -> dict:
    """Minimal environment for executing code we do not trust."""
    import re
    global SECRET_RE
    SECRET_RE = SECRET_RE or re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION", re.I)
    env = {k: v for k, v in os.environ.items()
           if k in SAFE_ENV_KEYS and not SECRET_RE.search(k)}
    for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if "@" in env.get(proxy, ""):
            env.pop(proxy)  # proxy URL with embedded credentials
    env.update({"PYTHONNOUSERSITE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "GIT_TERMINAL_PROMPT": "0"})
    if extra:
        env.update(extra)
    return env


def run(cmd: list[str], cwd: str | Path | None = None, env: dict | None = None,
        timeout: int = 900, untrusted: bool = False) -> RunResult:
    """Run a command. `untrusted=True` (repository code) gets only `safe_env()`."""
    started = time.monotonic()
    if untrusted:
        full_env = safe_env(env)
    else:
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
    try:
        p = subprocess.run(cmd, cwd=cwd, env=full_env, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return RunResult(cmd, p.returncode, p.stdout, p.stderr, time.monotonic() - started)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        return RunResult(cmd, -9, out, f"TIMEOUT after {timeout}s", time.monotonic() - started)
    except FileNotFoundError as exc:
        return RunResult(cmd, -1, "", str(exc), time.monotonic() - started)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class HttpCache:
    """GET JSON with a file cache so repeated runs are fast and reproducible."""

    def __init__(self, cache_dir: Path, ttl_seconds: int = 6 * 3600):
        self.dir = cache_dir
        self.ttl = ttl_seconds
        self.dir.mkdir(parents=True, exist_ok=True)

    def get_json(self, url: str, headers: dict | None = None):
        key = self.dir / (sha256(url)[:24] + ".json")
        if key.exists() and time.time() - key.stat().st_mtime < self.ttl:
            cached = read_json(key)
            if cached is not None:
                return cached.get("status"), cached.get("body")
        req = urllib.request.Request(url, headers={"User-Agent": "depexit/0.1", **(headers or {})})
        for attempt in range(3):  # transient DNS/network errors must not silently hide risks
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    status, body = resp.status, json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                status, body = exc.code, None
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    break
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                if attempt == 2:
                    log(f"metadata fetch failed: {url}: {exc}")
                    return None, {"error": str(exc)}
            time.sleep(1.5 * (attempt + 1))
        write_json(key, {"url": url, "status": status, "body": body})
        return status, body
