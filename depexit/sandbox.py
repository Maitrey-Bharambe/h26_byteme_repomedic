"""Isolated environments: create a venv, install a repo's deps, run tests/imports/probes.

Everything returned from here is *executed* evidence with the command and log attached.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from . import manifests
from .util import RunResult, read_json, run

HELPERS = Path(__file__).parent / "_helpers"
COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", "venv", "__pycache__", ".pytest_cache",
                                     "*.pyc", ".depexit", ".tox")


def _home(base: Path) -> dict:
    """Redirect HOME/temp/cache of untrusted code into its own work directory."""
    h = Path(base) / "home"
    (h / "tmp").mkdir(parents=True, exist_ok=True)
    return {"HOME": str(h), "USERPROFILE": str(h), "TMP": str(h / "tmp"), "TEMP": str(h / "tmp"),
            "TMPDIR": str(h / "tmp"), "XDG_CACHE_HOME": str(h / "cache")}


def copy_repo(src: Path, dst: Path) -> Path:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=COPY_IGNORE)
    return dst


@dataclass
class Env:
    python_request: str
    dir: Path
    python: Path
    ok: bool
    install_log: str
    install_cmds: list[list[str]] = field(default_factory=list)
    pkgmap: dict = field(default_factory=dict)
    pythonpath: list = field(default_factory=list)   # extra entries (tests use this; venvs need none)

    def summary(self):
        return {"python": self.pkgmap.get("python", self.python_request), "ok": self.ok,
                "install_cmds": [" ".join(c) for c in self.install_cmds]}


def install_plan(repo: Path) -> list[list[str]]:
    """Decide how the repository itself expects to be installed (observed from its files)."""
    req_files = [f for f in manifests.requirement_files(repo)]
    if req_files:
        args = []
        for f in req_files:
            args += ["-r", f.relative_to(repo).as_posix()]
        return [args, ["pytest"]]
    if (repo / "pyproject.toml").exists() or (repo / "setup.py").exists():
        return [["-e", "."], ["pytest"]]
    return [["pytest"]]


def create_env(repo: Path, env_dir: Path, python: str) -> Env:
    if env_dir.exists():
        shutil.rmtree(env_dir)
    logs: list[str] = []
    cmds: list[list[str]] = []
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "venv", "--quiet", "--python", python, str(env_dir)]
    else:
        cmd = [f"python{python}", "-m", "venv", str(env_dir)]
    r = run(cmd, cwd=repo, timeout=300)
    cmds.append(cmd)
    logs.append(r.text())
    py = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not r.ok or not py.exists():
        return Env(python, env_dir, py, False, "\n".join(logs), cmds)
    ok = True
    for args in install_plan(repo):
        if uv:
            cmd = [uv, "pip", "install", "--python", str(py), *args]
        else:
            cmd = [str(py), "-m", "pip", "install", "-q", *args]
        r = run(cmd, cwd=repo, timeout=900, untrusted=True, env=_home(env_dir))
        cmds.append(cmd)
        logs.append(r.text())
        if not r.ok:
            ok = False
            break
    env = Env(python, env_dir, py, ok, "\n".join(logs), cmds)
    r = run([str(py), "-I", str(HELPERS / "pkgmap.py")], timeout=120, untrusted=True, env=_home(env_dir))
    try:
        env.pkgmap = json.loads(r.stdout)
    except ValueError:
        env.pkgmap = {}
    return env


# --------------------------------------------------------------------------- tests

@dataclass
class TestRun:
    __test__ = False  # not a pytest test class
    exit_code: int
    seconds: float
    tests: dict[str, dict]          # nodeid -> {outcome, message}
    warnings: list[dict]
    collection_error: str | None
    log: str
    command: str

    def counts(self):
        c = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
        for t in self.tests.values():
            c[t["outcome"]] = c.get(t["outcome"], 0) + 1
        return c

    def to_dict(self, with_log=False):
        d = {"exit_code": self.exit_code, "seconds": round(self.seconds, 2),
             "counts": self.counts(), "tests": self.tests, "warnings": self.warnings,
             "collection_error": self.collection_error, "command": self.command}
        if with_log:
            d["log"] = self.log
        return d


def _parse_junit(path: Path) -> dict[str, dict]:
    tests: dict[str, dict] = {}
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return tests
    for case in root.iter("testcase"):
        cls = case.get("classname", "")
        nodeid = cls.replace(".", "/") + ".py::" + case.get("name", "") if cls else case.get("name", "")
        outcome, message = "passed", ""
        for tag in ("failure", "error", "skipped"):
            el = case.find(tag)
            if el is not None:
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                message = ((el.get("message") or "") + "\n" + (el.text or ""))[-3000:]
                break
        tests[nodeid] = {"outcome": outcome, "message": message.strip()}
    return tests


def _pythonpath(env: Env, repo: Path) -> str:
    return os.pathsep.join([str(HELPERS), str(repo), *map(str, env.pythonpath)])


def run_tests(env: Env, repo: Path, out_dir: Path, plugins: tuple = (), extra_env: dict | None = None,
              timeout: int = 1200) -> TestRun:
    out_dir.mkdir(parents=True, exist_ok=True)
    junit = out_dir / "junit.xml"
    warn_out = out_dir / "warnings.json"
    for f in (junit, warn_out):
        f.unlink(missing_ok=True)
    cmd = [str(env.python), "-m", "pytest", "-q", "-rfE", "-p", "no:cacheprovider",
           "-p", "depexit_warnings", *[x for p in plugins for x in ("-p", p)],
           "-W", "default", f"--junitxml={junit}"]
    r: RunResult = run(cmd, cwd=repo, env={"PYTHONPATH": _pythonpath(env, repo),
                                           "DEPEXIT_WARNINGS_OUT": str(warn_out),
                                           "DEPEXIT_REPO_ROOT": str(repo),
                                           "PYTHONDONTWRITEBYTECODE": "1", **_home(out_dir),
                                           **(extra_env or {})},
                       timeout=timeout, untrusted=True)
    tests = _parse_junit(junit)
    collection_error = None
    if r.returncode in (2, 3, 4) or (not tests and r.returncode != 5):
        tail = (r.stdout + "\n" + r.stderr).strip().splitlines()
        collection_error = "\n".join(tail[-40:])
    log = r.text()
    (out_dir / "pytest.log").write_text(log, encoding="utf-8")
    command = f"PYTHONPATH=<helpers>:{repo.name} " + " ".join(["python", *cmd[1:]])
    return TestRun(r.returncode, r.seconds, tests, read_json(warn_out, []) or [],
                   collection_error, log, command)


def run_helper(env: Env, repo: Path, script: str, args: list[str], work: Path,
               timeout: int = 300) -> RunResult:
    """Run one of the _helpers scripts in the target venv with `repo` on sys.path."""
    return run([str(env.python), str(HELPERS / script), *args], cwd=repo,
               env={"PYTHONPATH": _pythonpath(env, repo), "DEPEXIT_REPO_ROOT": str(repo),
                    "PYTHONDONTWRITEBYTECODE": "1", **_home(work)},
               timeout=timeout, untrusted=True)


# --------------------------------------------------------------------------- imports

def import_check(env: Env, repo: Path, module: str) -> dict:
    r = run([str(env.python), "-I", str(HELPERS / "import_check.py"), module], cwd=repo, timeout=120,
            untrusted=True, env=_home(env.dir))
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"module": module, "ok": False, "error": (r.stderr or r.stdout)[-500:],
                "warnings": []}


# --------------------------------------------------------------------------- probes

def run_probes(env: Env, repo: Path, probe_file: Path, out_file: Path) -> dict:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.unlink(missing_ok=True)
    r = run([str(env.python), str(HELPERS / "probe_runner.py"), str(probe_file), str(out_file)],
            cwd=repo, env={"PYTHONPATH": str(repo), "PYTHONDONTWRITEBYTECODE": "1",
                           **_home(out_file.parent / "probe_home")},
            timeout=180, untrusted=True)
    data = read_json(out_file)
    if data is None:
        data = {"__runner__": {"ok": False, "error": (r.stderr or r.stdout)[-1500:] or
                               f"probe runner exited {r.returncode}"}}
    return data


# --------------------------------------------------------------------------- attribution

SITE_RE = re.compile(r"site-packages[/\\]([A-Za-z0-9_]+)")
NOMOD_RE = re.compile(r"No module named '([A-Za-z0-9_\.]+)'")
CANNOT_IMPORT_RE = re.compile(r"cannot import name '[^']+' from '([A-Za-z0-9_\.]+)'")


def attribute_text(text: str, modules: dict[str, list[str]]) -> list[str]:
    """Which distributions does a traceback / error text implicate? Deepest frame first."""
    found: list[str] = []
    for m in reversed(SITE_RE.findall(text or "")):
        for d in modules.get(m, []):
            if d not in found:
                found.append(d)
    for rx in (NOMOD_RE, CANNOT_IMPORT_RE):
        for m in rx.findall(text or ""):
            for d in modules.get(m.split(".")[0], []):
                if d not in found:
                    found.append(d)
    return found
