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


def run(cmd: list[str], cwd: str | Path | None = None, env: dict | None = None,
        timeout: int = 900) -> RunResult:
    started = time.monotonic()
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        p = subprocess.run(cmd, cwd=cwd, env=full_env, capture_output=True, text=True,
                           timeout=timeout)
        return RunResult(cmd, p.returncode, p.stdout, p.stderr, time.monotonic() - started)
    except subprocess.TimeoutExpired as exc:
        return RunResult(cmd, -9, exc.stdout or "" if isinstance(exc.stdout, str) else "",
                         f"TIMEOUT after {timeout}s", time.monotonic() - started)
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
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                status, body = resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            status, body = exc.code, None
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            return None, {"error": str(exc)}
        write_json(key, {"url": url, "status": status, "body": body})
        return status, body
