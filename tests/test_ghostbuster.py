"""GhostBuster: runtime ghost imports (real child pytest processes) + zombie build commands."""
import builtins
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from depexit import capsule, ghostbuster, sandbox

from conftest import write


@pytest.fixture
def haunted(tmp_path):
    site = tmp_path / "site"
    write(site, "nose/__init__.py", "VERSION = '1.3.7'\n")
    write(site, "nose/tools.py", "NAME = 'tools'\n")
    write(site, "nose2/__init__.py", "VERSION = '0.15'\n")
    write(site, "thirdlib/__init__.py", "def uses():\n    import nose\n    return nose.VERSION\n")
    repo = tmp_path / "repo"
    write(repo, "app/__init__.py", "")
    write(repo, "app/core.py", """
        import importlib


        def old_feature():
            return importlib.import_module("nose.tools").NAME


        def plain():
            import nose
            return nose.VERSION


        def similar():
            import nose2
            return nose2.VERSION


        def via_lib():
            import thirdlib
            return thirdlib.uses()
        """)
    write(repo, "tests/test_core.py", """
        from app import core


        def test_all():
            assert core.old_feature() == "tools"
            assert core.plain() == "1.3.7"
            assert core.similar() == "0.15"
            assert core.via_lib() == "1.3.7"
        """)
    write(repo, "Makefile", "test:\n\tnosetests tests/\n# old: nosetests -v\nlint:\n\tpython -m nose2 --help\n"
                            "smoke:\n\tpython -m nose tests\n")
    write(repo, ".github/workflows/ci.yml", "jobs:\n  t:\n    steps:\n      - run: nosetests\n      # - run: nosetests\n")
    write(repo, "tox.ini", "[testenv]\ncommands = nosetests\n; commands = nosetests --old\n")
    write(repo, "README.md", "We used to run nosetests with nose.\n")   # documentation: not a build file
    env = sandbox.Env("cur", tmp_path / "env", Path(sys.executable), True, "", pythonpath=[str(site)])
    return {"tmp": tmp_path, "repo": repo, "env": env}


def dep(name="nose", modules=("nose",)):
    return SimpleNamespace(name=name, modules=list(modules))


# ------------------------------------------------------------------ runtime ghost imports

def test_runtime_detects_plain_and_importlib_imports_and_classifies_origin(haunted):
    before = builtins.__import__, importlib.import_module
    r = ghostbuster.runtime_ghosts(haunted["env"], haunted["repo"], {"nose": "nose"}, haunted["tmp"] / "w")
    assert r["status"] == "observed_ghost", r
    assert r["test_counts"]["passed"] == 1            # observation never changed behaviour
    f = {(x["attempted_import"], x["via"], x["origin"]) for x in r["findings"]}
    assert ("nose.tools", "importlib.import_module", "repository") in f
    assert ("nose", "import", "repository") in f
    assert ("nose", "import", "third_party") in f       # thirdlib still imports nose: not blamed on the repo
    assert all(x["attempted_import"].split(".")[0] == "nose" for x in r["findings"])   # nose2 ignored
    repo_hits = [x for x in r["findings"] if x["origin"] == "repository"]
    assert {x["caller_file"] for x in repo_hits} == {"app/core.py"}
    assert all(isinstance(x["caller_line"], int) and x["test"] == "tests/test_core.py::test_all" for x in repo_hits)
    assert not any(Path(x["caller_file"]).is_absolute() for x in r["findings"])
    assert "depexit_ghostbuster" in r["command"]
    # the observer lived only in the child process
    assert (builtins.__import__, importlib.import_module) == before


def test_runtime_clean_is_not_observed_not_proof(haunted, tmp_path):
    r = ghostbuster.runtime_ghosts(haunted["env"], haunted["repo"], {"pkg_never_used": "pkg-never-used"}, tmp_path / "w")
    assert r["status"] == "not_observed" and r["findings"] == []
    g = {"retired_distribution": "x", "status": "not_observed", "tooling": {"status": "not_observed", "findings": []},
         "runtime": r}
    md = "\n".join(ghostbuster.render_md(g))
    assert "none observed" in md and "not proof" in md


def test_instrumentation_failure_is_error_never_clean(haunted, tmp_path):
    broken = sandbox.Env("cur", tmp_path / "x", tmp_path / "no-python.exe", True, "")
    r = ghostbuster.runtime_ghosts(broken, haunted["repo"], {"nose": "nose"}, tmp_path / "w")
    assert r["status"] == "error" and r["findings"] == []
    g = ghostbuster.check(dep(), {"action": "replace"}, haunted["repo"], broken, tmp_path / "w2", [])
    assert g["runtime"]["status"] == "error"
    assert g["status"] == "observed_ghost"     # tooling ghosts are still real evidence
    g2 = ghostbuster.check(dep("pkg-x", ["pkg_x"]), {"action": "remove"}, haunted["repo"], broken, tmp_path / "w3", [])
    assert g2["status"] == "error"             # nothing found + a failed check is never "not_observed"


# ------------------------------------------------------------------ zombie build commands

def test_zombie_build_commands(haunted):
    f = ghostbuster.tooling_ghosts(haunted["repo"], "nose", [], ["nose"])
    got = {(x["file"], x["line"], x["command"]) for x in f}
    assert ("Makefile", 2, "nosetests tests/") in got
    assert ("Makefile", 7, "python -m nose tests") in got
    assert (".github/workflows/ci.yml", 4, "- run: nosetests") in got
    assert ("tox.ini", 2, "commands = nosetests") in got
    assert len(got) == 4                       # comments, `nose2` and README are not commands
    by = {(x["file"], x["line"]): x for x in f}
    assert by[("Makefile", 2)]["suggested_command"] == "python -m pytest tests/"
    assert by[("Makefile", 7)]["suggested_command"] == "python -m pytest tests"
    assert all(x["provenance"] == "observed" and x["suggestion_provenance"] == "rule_based" for x in f)


def test_no_fabricated_suggestion_for_other_tools(tmp_path):
    write(tmp_path, "Makefile", "lint:\n\tpep8 src/\n")
    f = ghostbuster.tooling_ghosts(tmp_path, "pep8", [], ["pep8"])
    assert len(f) == 1 and f[0]["suggested_command"] is None


def test_only_retiring_actions_run(haunted):
    for action in ("upgrade", "declare", "keep"):
        assert ghostbuster.check(dep(), {"action": action}, haunted["repo"], haunted["env"], haunted["tmp"] / "w", []) is None


def test_check_end_to_end_with_real_child_process(haunted):
    g = ghostbuster.check(dep(), {"action": "replace"}, haunted["repo"], haunted["env"], haunted["tmp"] / "w", [])
    assert g["status"] == "observed_ghost" and g["advisory"] is True
    assert g["tooling"]["status"] == "observed_ghost" and g["runtime"]["status"] == "observed_ghost"
    assert "zombie build command" in g["summary"] and "runtime ghost import" in g["summary"]


# ------------------------------------------------------------------ capsule + pipeline integration

def test_capsule_renders_with_and_without_ghostbuster(haunted, tmp_path):
    from conftest import dep_facts
    run_meta = {"tool": {}, "generated_at": "now", "target": {"source": "x", "pythons": ["3.12"], "commit": None},
                "ai": {"reachable": False}, "_modules": {}}
    old = {"plan": {"action": "remove"}, "status": "verified", "validation": []}   # an older capsule shape
    c_old = capsule.build_capsule(dep_facts(), {**old, "_repo_root": haunted["repo"]}, run_meta, tmp_path)
    assert c_old["ghostbuster"] is None
    assert "GhostBuster" not in capsule.render_md(c_old)
    g = ghostbuster.check(dep(), {"action": "replace"}, haunted["repo"], None, tmp_path / "w", [])
    c_new = capsule.build_capsule(dep_facts(), {**old, "ghostbuster": g, "_repo_root": haunted["repo"]}, run_meta, tmp_path)
    md = capsule.render_md(c_new)
    assert "GhostBuster" in md and "Makefile:2" in md and "python -m pytest tests/" in md
    assert "runtime check" in g["summary"] or "not run" in md
    json.dumps(c_new)  # serialisable


def test_pipeline_attaches_ghostbuster_without_changing_verdict(fake_exec, small_repo, tmp_path):
    from depexit.pipeline import Pipeline
    # nose is declared but never imported -> rule-based "remove"; the Makefile still runs it
    (small_repo / "requirements.txt").write_text("six==1.16.0\nidna==2.7\nrequests==2.31.0\nnose==1.3.7\n")
    write(small_repo, "Makefile", "test-nose:\n\tnosetests tests/\n")
    p = Pipeline(str(small_repo), tmp_path / "out", ["3.12"], None, jobs=1)
    p.analyze()
    p.plan_all()
    assert p.results["nose"]["plan"]["action"] == "remove"
    p.run_exits(["nose"])
    res = p.results["nose"]
    assert res["status"] in ("verified", "verified_with_gaps")      # unchanged by GhostBuster
    g = res["ghostbuster"]
    assert g["tooling"]["status"] == "observed_ghost"
    assert g["tooling"]["findings"][0]["file"] == "Makefile"
    assert g["runtime"]["status"] == "error"     # the scripted test runner cannot host the observer
    assert g["status"] == "observed_ghost"
    p.finalize()
    cap = json.loads((tmp_path / "out" / "capsules" / "nose" / "capsule.json").read_text())
    assert cap["ghostbuster"]["status"] == "observed_ghost"
    ix = json.loads((tmp_path / "out" / "index.json").read_text())
    assert next(d for d in ix["dependencies"] if d["name"] == "nose")["ghostbuster"]["build_commands"] == 1
    assert "GhostBuster" in (tmp_path / "out" / "REPORT.md").read_text(encoding="utf-8")
