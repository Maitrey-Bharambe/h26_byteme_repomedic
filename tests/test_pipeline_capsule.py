"""Real Pipeline + capsule generation over a scripted execution layer (see conftest.fake_exec)."""
import json

import pytest

from depexit import capsule
from depexit.pipeline import Pipeline, PipelineError, validate_target

A, B = "tests/test_app.py::t_a", "tests/test_app.py::t_b"


def run(small_repo, tmp_path, **kw):
    p = Pipeline(str(small_repo), tmp_path / "out", ["3.12"], None, jobs=1, **kw)
    p.analyze()
    p.plan_all()
    return p


def test_plan_stage_modifies_nothing_and_writes_planned_capsules(fake_exec, small_repo, tmp_path):
    before = (small_repo / "requirements.txt").read_text()
    p = run(small_repo, tmp_path)
    assert {d.name for d in p.selected} == {"six", "idna"}           # declared, never imported
    for name in ("six", "idna"):
        cap = json.loads((tmp_path / "out" / "capsules" / name / "capsule.json").read_text())
        assert cap["status"] == "planned"
        assert cap["proposed_replacement"]["action"] == "remove"
        assert cap["verification"]["status"] is None
        assert {r["result"] for r in cap["verification"]["requirements"]} == {"pending"}
        assert cap["execution_boundary"]["os_isolation"] is False
    assert (small_repo / "requirements.txt").read_text() == before
    assert not (tmp_path / "out" / "combined").exists()


def test_individual_exits_pass_but_combined_fails(fake_exec, small_repo, tmp_path):
    # t_b only breaks when BOTH six and idna are gone
    fake_exec["behaviour"] = lambda reqs: {A: "passed",
                                           B: "passed" if ("six" in reqs or "idna" in reqs) else "failed"}
    p = run(small_repo, tmp_path)
    p.run_exits()
    out = p.finalize()
    assert p.results["six"]["status"] in ("verified", "verified_with_gaps")
    assert p.results["idna"]["status"] in ("verified", "verified_with_gaps")
    cb = p.combined_result
    assert cb["dependencies"] == ["six", "idna"] or set(cb["dependencies"]) == {"six", "idna"}
    assert cb["status"] == "failed"
    assert [r["test"] for r in cb["comparison"]["3.12"]["tests"]["regressed"]] == [B]
    index = json.loads((out / "index.json").read_text())
    assert index["combined"]["status"] == "failed"
    report = (out / "REPORT.md").read_text(encoding="utf-8")
    assert "Combined status:** ❌ Failed" in report
    assert (out / "combined" / "all-migrations.patch").read_text().count("-six==1.16.0") == 1


def test_migration_that_fails_verification(fake_exec, small_repo, tmp_path):
    fake_exec["behaviour"] = lambda reqs: {A: "passed" if "six" in reqs else "failed", B: "passed"}
    p = run(small_repo, tmp_path)
    p.run_exits()
    p.finalize()
    six = p.results["six"]
    assert six["status"] == "failed"
    assert "no_regressions: failed" in six["status_reason"]
    cap = json.loads((tmp_path / "out" / "capsules" / "six" / "capsule.json").read_text())
    assert cap["status"] == "failed"
    assert any(g["kind"] == "test-regression" for g in cap["compatibility_gaps"])
    md = (tmp_path / "out" / "capsules" / "six" / "CAPSULE.md").read_text(encoding="utf-8")
    assert "❌ failed" in md and "Every baseline-passing test" in md
    # a failed exit is never merged into the combined result
    assert p.combined_result["dependencies"] == ["idna"]
    assert p.combined_result["excluded_unverified"] == [{"dependency": "six", "status": "failed"}]


def test_incomplete_verification_when_baseline_has_no_passing_tests(fake_exec, small_repo, tmp_path):
    fake_exec["behaviour"] = lambda reqs: {A: "failed"}
    p = run(small_repo, tmp_path)
    p.run_exits()
    p.finalize()
    for name in ("six", "idna"):
        assert p.results[name]["status"] == "incomplete"
        assert "no_regressions: not_checked" in p.results[name]["status_reason"]
    assert p.combined_result is None                                  # nothing verified to combine
    cap = json.loads((tmp_path / "out" / "capsules" / "six" / "capsule.json").read_text())
    assert cap["status"] == "incomplete"
    assert capsule.STATUS_TEXT["incomplete"].startswith("⚪ Incomplete")


def test_install_failure_is_failed_not_verified(fake_exec, small_repo, tmp_path):
    fake_exec["install_ok"] = lambda reqs: "six" in reqs              # removing six breaks install
    p = run(small_repo, tmp_path)
    p.run_exits(["six"])
    assert p.results["six"]["status"] == "failed"
    assert p.results["idna"]["status"] == "planned"                   # untouched: not selected


def test_events_are_recorded_for_every_stage(fake_exec, small_repo, tmp_path):
    seen = []
    p = Pipeline(str(small_repo), tmp_path / "out", ["3.12"], None, on_event=seen.append)
    p.execute()
    stages = [e["stage"] for e in seen]
    for s in ("inspect", "baseline", "risk", "plan", "verify", "combine", "done"):
        assert s in stages
    assert p.events == seen


def test_target_validation():
    for bad in ("", "--upload-pack=evil", "file:///etc", "http://example.com/x.git", "ext::sh -c id"):
        with pytest.raises(PipelineError):
            validate_target(bad)
    assert validate_target("https://github.com/rishrek/LinkWatch").startswith("https://")
