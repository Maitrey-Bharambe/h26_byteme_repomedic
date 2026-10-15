"""Manifest handling and usage detection (observed facts)."""
from depexit import manifests, usage


def test_requirements_parsing_groups_comments_and_inline_comments(repo):
    decls = manifests.collect(repo)
    by = {(d.name, d.file): d for d in decls}
    assert by[("jinja2", "requirements.txt")].specifier == "==2.11.3"      # inline comment stripped
    assert by[("pyyaml", "requirements.txt")].commented is True           # `# PyYAML==5.1`
    assert by[("nose", "requirements-dev.txt")].group == "dev"
    assert by[("pyyaml", "pyproject.toml")].group == "optional:yaml"
    assert by[("setuptools", "pyproject.toml")].group == "build"
    assert not any(d.raw.startswith("-e") for d in decls)                 # options ignored


def test_drift_detects_incompatible_pins(repo):
    drift = manifests.find_drift(manifests.collect(repo))
    assert drift["requests"]["conflict"] is True          # ==2.31.0 vs >=2.32
    assert "jinja2" not in drift                          # identical in both files
    assert "pyyaml" not in drift                          # commented copy is ignored


def test_apply_dependency_change_replaces_in_every_manifest(repo):
    decls = [d for d in manifests.collect(repo) if d.name == "requests"]
    edited = manifests.apply_dependency_change(repo, "requests", "httpx>=0.27", decls)
    assert sorted(edited) == ["pyproject.toml", "requirements.txt"]
    assert "httpx>=0.27" in (repo / "requirements.txt").read_text()
    assert "requests" not in (repo / "requirements.txt").read_text()
    assert '"httpx>=0.27"' in (repo / "pyproject.toml").read_text()
    assert "-e ." in (repo / "requirements.txt").read_text()             # unrelated lines kept


def test_apply_dependency_change_remove_and_declare(repo):
    decls = [d for d in manifests.collect(repo) if d.name == "six"]
    assert manifests.apply_dependency_change(repo, "six", None, decls) == ["requirements.txt"]
    assert "six" not in (repo / "requirements.txt").read_text().split()
    added = manifests.apply_dependency_change(repo, "certifi", "certifi>=2024.1", [])
    assert set(added) == {"requirements.txt", "pyproject.toml"}
    assert "certifi>=2024.1" in (repo / "requirements.txt").read_text()
    assert "certifi" not in (repo / "requirements-dev.txt").read_text()   # runtime only


def test_usage_resolves_aliases_patches_subclass_private(repo):
    sites, errors = usage.scan_python(repo)
    assert errors == []
    syms = {(s.symbol, s.kind) for s in sites}
    assert ("requests.Session", "call") in syms                          # via `import requests as rq`
    assert ("requests.adapters.HTTPAdapter", "call") in syms             # via `as Adapter`
    assert ("requests.Session", "subclass") in syms
    assert ("requests.Session.request", "monkeypatch") in syms
    assert any(s.private and s.symbol == "requests.models._private_helper" for s in sites)
    mods = {s.module for s in sites}
    assert "json" not in mods and "os" not in mods                        # stdlib skipped
    assert "app" not in mods                                              # local package skipped
    assert "yaml" in mods


def test_tooling_scan_finds_console_scripts(repo):
    hits = usage.scan_tooling(repo, {"nose": ["nosetests"], "pytest": ["pytest", "py.test"]},
                              {"pytest": ["pytest"]})
    found = {(d, s.file, s.line) for d, s in hits}
    assert ("nose", "Makefile", 4) in found
    assert ("pytest", "Makefile", 2) in found                             # python -m pytest
