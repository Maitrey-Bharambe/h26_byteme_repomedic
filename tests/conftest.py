"""Shared fixtures: small synthetic repositories and deterministic fakes (no network, no LLM)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from depexit import risk


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return p


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    write(root, "requirements.txt", """
        requests==2.31.0
        Jinja2==2.11.3  # pinned for templates
        six
        # PyYAML==5.1
        -e .
        """)
    write(root, "requirements-dev.txt", "pytest\nnose==1.3.7\n")
    write(root, "pyproject.toml", """
        [build-system]
        requires = ["setuptools>=61"]

        [project]
        name = "app"
        dependencies = [
            "requests>=2.32",
            "Jinja2==2.11.3",
        ]

        [project.optional-dependencies]
        yaml = ["PyYAML>=6"]
        """)
    write(root, "app/__init__.py", "")
    write(root, "app/net.py", """
        import json
        import requests as rq
        from requests.adapters import HTTPAdapter as Adapter
        from app import util
        import yaml


        class Sess(rq.Session):
            pass


        def get(url):
            s = rq.Session()
            s.mount("https://", Adapter())
            return s.get(url, timeout=3).status_code


        rq.Session.request = lambda *a, **k: None
        x = rq.models._private_helper
        """)
    write(root, "app/util.py", "import os\n")
    write(root, "Makefile", "test:\n\tpython -m pytest\nnose:\n\tnosetests tests/\n")
    return root


def dep_facts(name="requests", modules=("requests",), usage=None, declared=None, **kw) -> risk.DepFacts:
    d = risk.DepFacts(name)
    d.modules = list(modules)
    d.usage = usage if usage is not None else [
        {"module": "requests", "symbol": "requests.Session", "kind": "call", "file": "app/net.py",
         "line": 10, "code": "s = requests.Session()", "private": False, "alias": None},
        {"module": "requests", "symbol": "requests.get", "kind": "call", "file": "app/net.py",
         "line": 12, "code": "requests.get(url)", "private": False, "alias": None},
        {"module": "requests", "symbol": "requests", "kind": "import", "file": "app/net.py",
         "line": 1, "code": "import requests", "private": False, "alias": None},
    ]
    d.declared = declared if declared is not None else [
        {"name": name, "raw": f"{name}==1.0", "specifier": "==1.0", "file": "requirements.txt",
         "line": 1, "group": "runtime", "commented": False}]
    d.installed = kw.get("installed", {"3.12": "1.0"})
    d.pypi = kw.get("pypi", {"found": True, "latest_version": "2.0", "latest_compatible": "2.0",
                              "source": "https://pypi.org/pypi/x/json"})
    return d


class FakeHttp:
    """Stands in for util.HttpCache: canned PyPI JSON keyed by package name."""

    def __init__(self, packages: dict[str, dict[str, str | None]]):
        # packages: name -> {version: requires_python}
        self.packages = {k.lower(): v for k, v in packages.items()}

    def get_json(self, url, headers=None):
        name = url.split("/pypi/")[1].split("/")[0].lower()
        if name not in self.packages:
            return 404, None
        rel = {v: [{"requires_python": rp, "upload_time_iso_8601": "2024-01-01T00:00:00Z", "yanked": False}]
               for v, rp in self.packages[name].items()}
        return 200, {"info": {"name": name, "version": max(rel)}, "releases": rel}


def outcomes(**kw) -> dict:
    return {nid: {"outcome": o, "message": "" if o == "passed" else f"AssertionError in {nid}"}
            for nid, o in kw.items()}


# --------------------------------------------------------------------------- fake execution layer

class FakeEnv:
    def __init__(self, repo: Path, env_dir: Path, py: str, ok: bool = True):
        import sys
        reqs = (repo / "requirements.txt").read_text() if (repo / "requirements.txt").exists() else ""
        names = [l.split("=")[0].split(">")[0].strip().lower() for l in reqs.splitlines()
                 if l.strip() and not l.startswith("#")]
        self.python_request, self.dir, self.python, self.ok = py, env_dir, env_dir / "python", ok
        self.install_log = "" if ok else "ERROR: resolution impossible"
        self.install_cmds = [["uv", "pip", "install", "-r", "requirements.txt"]]
        self.pkgmap = {"python": py + ".0", "stdlib": sorted(sys.stdlib_module_names),
                       "modules": {n: [n] for n in names}, "versions": {n: "1.0" for n in names},
                       "scripts": {}, "requires": {}}

    def summary(self):
        return {"python": self.pkgmap["python"], "ok": self.ok, "install_cmds": []}


@pytest.fixture
def fake_exec(monkeypatch):
    """Replace real installs/test runs with a scripted `behaviour(repo_text) -> {test: outcome}`.

    Everything above the sandbox (pipeline, verification, combined merge, capsules) is real.
    """
    from depexit import metadata, pipeline, sandbox

    state = {"behaviour": lambda reqs: {"tests/test_app.py::t_a": "passed", "tests/test_app.py::t_b": "passed"},
             "install_ok": lambda reqs: True}

    def create_env(repo, env_dir, py):
        reqs = (repo / "requirements.txt").read_text()
        return FakeEnv(repo, env_dir, py, state["install_ok"](reqs))

    def run_tests(env, repo, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        res = state["behaviour"]((repo / "requirements.txt").read_text())
        (out_dir / "pytest.log").write_text(repr(res))
        return sandbox.TestRun(0, 0.1, outcomes(**res), [], None, repr(res), "pytest")

    monkeypatch.setattr(sandbox, "create_env", create_env)
    monkeypatch.setattr(sandbox, "run_tests", run_tests)
    monkeypatch.setattr(sandbox, "import_check", lambda env, repo, m: {"module": m, "ok": True, "warnings": []})
    monkeypatch.setattr(metadata, "pypi_info", lambda http, name, ver, pys: {"found": False, "source": "offline"})
    monkeypatch.setattr(pipeline.Pipeline, "commit_of", staticmethod(lambda repo: "0" * 40))
    return state


@pytest.fixture
def small_repo(tmp_path) -> Path:
    root = tmp_path / "small"
    write(root, "requirements.txt", "six==1.16.0\nidna==2.7\nrequests==2.31.0\n")
    write(root, "app/__init__.py", "")
    write(root, "app/main.py", "import requests\n\n\ndef ping():\n    return requests.get\n")
    write(root, "tests/test_app.py", "from app import main\n\n\ndef test_a():\n    assert main.ping\n")
    return root
