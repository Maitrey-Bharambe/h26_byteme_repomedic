"""Test blindness + replay with REAL pytest subprocesses on a tiny fixture repo.

No network and no model: the "dependencies" are two local packages placed on the path
through Env.pythonpath, and the current interpreter (which has pytest) runs the suite.
"""
import json
import sys
from pathlib import Path

import pytest

from depexit import blindness, replay, risk, sandbox, usage, verification

from conftest import write


@pytest.fixture
def world(tmp_path):
    site = tmp_path / "site"
    write(site, "libwatched/__init__.py", "def shout(s):\n    return s.upper()\n")
    write(site, "libhidden/__init__.py", "def hidden(x):\n    return x * 2\n")
    # the "replacement" version of libwatched: differs for one real input
    write(tmp_path / "site_new", "libwatched/__init__.py",
          "def shout(s):\n    return s.upper() if s != 'hi bob' else 'HI BOB!'\n")
    write(tmp_path / "site_new", "libhidden/__init__.py", "def hidden(x):\n    return x * 2\n")
    repo = tmp_path / "repo"
    write(repo, "app/__init__.py", "")
    write(repo, "app/core.py", """
        import os

        import libhidden
        from libwatched import shout


        def greet(name):
            return shout("hi " + name)


        def maybe_double(x):
            if os.environ.get("APP_FAST", "1") == "1":   # short circuit: libhidden never runs in tests
                return x
            return libhidden.hidden(x)


        def apply(cb):
            return shout(cb("x"))
        """)
    write(repo, "tests/test_core.py", """
        from app import core


        def test_greet():
            assert core.greet("bob") == "HI BOB"
            assert core.greet("ann") == "HI ANN"
            assert core.greet("cy") == "HI CY"


        def test_maybe():
            assert core.maybe_double(2) == 2


        def test_apply_unpicklable():
            assert core.apply(lambda v: v + "y") == "XY"
        """)
    env_old = sandbox.Env("cur", tmp_path / "env_old", Path(sys.executable), True, "", pythonpath=[str(site)])
    env_new = sandbox.Env("cur", tmp_path / "env_new", Path(sys.executable), True, "", pythonpath=[str(tmp_path / "site_new")])
    sites, _ = usage.scan_python(repo)
    deps = {}
    for s in sites:
        if s.module.startswith("lib"):
            deps.setdefault(s.module, risk.DepFacts(s.module)).usage.append(s.to_dict())
            deps[s.module].modules = [s.module]
    base = sandbox.run_tests(env_old, repo, tmp_path / "base")
    assert base.counts()["passed"] == 3, base.log
    return {"tmp": tmp_path, "repo": repo, "old": env_old, "new": env_new, "deps": deps, "base": base}


def test_usage_records_enclosing_function(world):
    fns = {(s["symbol"], s["function"]) for d in world["deps"].values() for s in d.usage}
    assert ("libwatched.shout", "greet") in fns
    assert ("libhidden.hidden", "maybe_double") in fns


def test_blindness_watched_and_not_exercised(world):
    res = blindness.run_check(world["old"], world["repo"], world["base"],
                              [world["deps"]["libwatched"], world["deps"]["libhidden"]],
                              world["tmp"] / "blind", "cur", jobs=3)
    w, hdn = res["libwatched"], res["libhidden"]
    assert w["classification"] == "watched", w
    assert {r["mode"] for r in w["runs"]} == {"raise", "none", "wrong"}
    assert all(r["calls"] > 0 for r in w["runs"])
    assert "tests/test_core.py::test_greet" in w["catching_tests"]
    assert hdn["classification"] == "not_exercised", hdn          # env-var short circuit
    assert all(r["calls"] == 0 for r in hdn["runs"])
    assert w["provenance"] == "executed" and "depexit_sabotage" in w["runs"][0]["command"]


def test_blindness_check_that_crashes_is_not_run(world):
    broken = sandbox.Env("cur", world["tmp"] / "x", world["tmp"] / "no-python.exe", True, "")
    res = blindness.run_check(broken, world["repo"], world["base"], [world["deps"]["libwatched"]],
                              world["tmp"] / "blind2", "cur")
    assert res["libwatched"]["classification"] == "not_run"
    status, why = verification.final_verdict("verified", res["libwatched"], None)
    assert status == "incomplete" and "could not run" in why


def _record(world):
    targets = replay.targets_for(world["repo"], world["deps"]["libwatched"])
    rec = replay.record(world["old"], world["repo"], targets, world["tmp"] / "rec")
    assert rec["error"] is None, rec
    return rec, [t["function"] for t in targets]


def test_record_and_replay_detects_changed_output(world):
    rec, fns = _record(world)
    calls = rec["calls"]["app.core.greet"]
    assert len(calls) == 3 and {c["test"] for c in calls} == {"tests/test_core.py::test_greet"}
    rp = replay.replay(rec, fns, world["old"], world["repo"], world["new"], world["repo"],
                       world["tmp"] / "rp", "cur")
    assert rp["state"] == "compared"
    assert rp["counts"]["different"] == 1 and rp["counts"]["identical"] == 2
    d = rp["differences"][0]
    assert d["function"] == "app.core.greet" and "bob" in d["input"]
    assert d["original"] == "HI BOB" and d["replacement"] == "HI BOB!"
    assert d["test"] == "tests/test_core.py::test_greet"
    status, _ = verification.final_verdict("verified", {"classification": "watched"}, rp)
    assert status == "failed"


def test_unpicklable_argument_is_not_replayable_and_never_verified(world):
    rec, fns = _record(world)
    apply_calls = rec["calls"]["app.core.apply"]
    assert apply_calls and "not picklable" in apply_calls[0]["not_replayable"]
    rp = replay.replay(rec, ["app.core.apply"], world["old"], world["repo"], world["old"], world["repo"],
                       world["tmp"] / "rp2", "cur")
    assert rp["counts"]["not_replayable"] == 1 and rp["counts"]["identical"] == 0
    for blind in ({"classification": "watched"}, {"classification": "blind"}):
        status, _ = verification.final_verdict("verified", blind, rp)
        assert status not in ("verified", "verified_by_replay")


def test_verdicts():
    same = {"state": "compared", "counts": {"identical": 4, "different": 0, "exception_changed": 0, "not_replayable": 0}}
    none = {"state": "no_recorded_calls", "counts": {}}
    probes = {"state": "no_recorded_calls", "counts": {}, "ai_generated_inputs": {"probes": {"executed": True}}}
    assert verification.final_verdict("verified", {"classification": "watched"}, same)[0] == "verified"
    assert verification.final_verdict("verified", {"classification": "blind"}, same)[0] == "verified_by_replay"
    assert verification.final_verdict("verified", {"classification": "partly_blind"}, same)[0] == "verified_by_replay"
    s, why = verification.final_verdict("verified", {"classification": "not_exercised"}, probes)
    assert s == "weakly_verified" and "AI-generated" in why
    assert verification.final_verdict("verified", {"classification": "blind"}, none)[0] == "weakly_verified"
    assert verification.final_verdict("failed", {"classification": "watched"}, same)[0] == "failed"
    assert verification.final_verdict("verified", {"classification": "watched"},
                                      {"state": "not_run", "reason": "x", "counts": {}})[0] == "incomplete"
    mostly = {"state": "compared", "counts": {"identical": 1, "different": 0, "exception_changed": 0, "not_replayable": 5}}
    assert verification.final_verdict("verified", {"classification": "watched"}, mostly)[0] == "incomplete"
    assert verification.final_verdict("verified_with_gaps", {"classification": "not_applicable"}, None)[0] == "verified_with_gaps"


def test_replay_targets_skip_network_and_decorated(tmp_path):
    write(tmp_path, "pkg/__init__.py", "")
    write(tmp_path, "pkg/m.py", """
        import functools
        import requests
        import chardet


        def fetch(u):
            return requests.get(u)


        def check(u):
            return fetch(u)


        def guess(b):
            return chardet.detect(b)


        @functools.lru_cache
        def cached(b):
            return chardet.detect(b)
        """)
    sites, _ = usage.scan_python(tmp_path)
    t = {x["function"]: x for x in usage.replay_targets(tmp_path, [s.to_dict() for s in sites])}
    assert t["pkg.m.guess"]["replayable"]
    assert not t["pkg.m.fetch"]["replayable"] and "network I/O" in t["pkg.m.fetch"]["skip"]
    assert not t["pkg.m.cached"]["replayable"] and "decorated" in t["pkg.m.cached"]["skip"]
    net = usage.network_functions(tmp_path)
    assert "pkg.m.check" in net and "pkg.m.fetch" in net["pkg.m.check"]   # propagated through the call
    assert "pkg.m.guess" not in net
