"""Verification outcomes and regression detection: skipped checks never count as passed."""
from depexit import verification as V
from depexit.compare import compare_probes, compare_tests, compare_warnings
from depexit.sandbox import TestRun

from conftest import outcomes

PY = ["3.12", "3.13"]


def trun(tests: dict, collection_error=None, warnings=None) -> TestRun:
    return TestRun(1 if any(t["outcome"] != "passed" for t in tests.values()) else 0, 1.0, tests,
                   warnings or [], collection_error, "", "pytest")


BASE = trun(outcomes(t_a="passed", t_b="passed", t_c="failed"))


def cmp(new: TestRun, base=BASE, install_ok=True, probes=None, import_ok=True, version="2.1",
        original_still_installed=False, new_warnings=()):
    c = {"install_ok": install_ok}
    if not install_ok:
        c["tests"] = compare_tests(base, TestRun(-1, 0, {}, [], "replacement could not be installed", "", ""))
        return c
    c["tests"] = compare_tests(base, new)
    c["warnings"] = {"new": [{"category": "DeprecationWarning", "message": m} for m in new_warnings], "resolved": []}
    c["replacement_import"] = {"module": "x", "ok": import_ok, "error": None if import_ok else "ImportError"}
    c["installed"] = {"replacement_version": version, "original_still_installed": original_still_installed}
    if probes is not None:
        c["probes"] = probes
    return c


GOOD = trun(outcomes(t_a="passed", t_b="passed", t_c="passed"))
SAME_PROBES = compare_probes({"p1": {"ok": True, "value": 1}}, {"p1": {"ok": True, "value": 1}})


def evaluate(plan, comparison, code_expected=False, residual=()):
    reqs = V.requirements_for(plan, PY, code_expected)
    return V.evaluate(plan, reqs, comparison, list(residual))


def req(ev, rid):
    return next(r for r in ev["requirements"] if r["id"] == rid)


UPGRADE = {"action": "upgrade", "package": "x", "specifier": ">=2,<3"}
REPLACE = {"action": "replace", "package": "y", "specifier": ">=1"}


def test_fully_verified_upgrade():
    ev = evaluate(UPGRADE, {py: cmp(GOOD, probes=SAME_PROBES) for py in PY})
    assert ev["status"] == "verified"
    assert all(r["result"] == "met" for r in ev["requirements"])


def test_migration_fails_verification_on_test_regression():
    broken = trun(outcomes(t_a="passed", t_b="failed", t_c="failed"))
    ev = evaluate(UPGRADE, {"3.12": cmp(GOOD), "3.13": cmp(broken)})
    assert ev["status"] == "failed"
    r = req(ev, "no_regressions")
    assert r["result"] == "failed" and any("t_b" in e for e in r["evidence"])
    assert not V.is_clean(ev)


def test_install_success_alone_never_verifies():
    """Installed fine, but the baseline had no passing test: nothing proves behaviour."""
    no_pass_base = trun(outcomes(t_c="failed"))
    ev = evaluate(UPGRADE, {py: cmp(trun(outcomes(t_c="failed")), base=no_pass_base) for py in PY})
    assert req(ev, "install")["result"] == "met"
    assert req(ev, "no_regressions")["result"] == "not_checked"
    assert ev["status"] == "incomplete"


def test_incomplete_when_required_probes_were_not_executed():
    """Code was rewritten (probes required) but no probe ran: incomplete, not verified."""
    ev = evaluate(REPLACE, {py: cmp(GOOD) for py in PY}, code_expected=True)
    r = req(ev, "behaviour_probes")
    assert r["result"] == "not_checked" and r["required"] is True
    assert ev["status"] == "incomplete"
    assert V.is_clean(ev)  # nothing failed, so the fix loop has nothing to fix


def test_probe_infrastructure_error_is_not_a_pass():
    probes = compare_probes({"__load__": {"ok": False}}, {"__load__": {"ok": False}})
    ev = evaluate(REPLACE, {py: cmp(GOOD, probes=probes) for py in PY}, code_expected=True)
    assert req(ev, "behaviour_probes")["result"] == "not_checked"
    assert ev["status"] == "incomplete"


def test_probe_difference_fails_when_code_changed():
    probes = compare_probes({"p": {"ok": True, "value": "a"}}, {"p": {"ok": False, "error_type": "ValueError"}})
    ev = evaluate(REPLACE, {py: cmp(GOOD, probes=probes) for py in PY}, code_expected=True)
    assert ev["status"] == "failed" and not V.is_clean(ev)


def test_advisory_checks_not_run_give_gaps_not_verified():
    ev = evaluate(UPGRADE, {py: cmp(GOOD) for py in PY})              # no probes, manifest-only change
    assert req(ev, "behaviour_probes")["required"] is False
    assert ev["status"] == "verified_with_gaps"


def test_install_failure_fails_and_marks_dependent_checks_not_checked():
    ev = evaluate(UPGRADE, {"3.12": cmp(GOOD), "3.13": cmp(GOOD, install_ok=False)})
    assert ev["status"] == "failed"
    assert req(ev, "install")["result"] == "failed"
    assert any("install failed" in e for e in req(ev, "replacement_imports")["evidence"])


def test_new_collection_error_fails():
    ev = evaluate(UPGRADE, {py: cmp(trun({}, collection_error="ModuleNotFoundError: pkg_resources")) for py in PY})
    assert req(ev, "suite_runs")["result"] == "failed"
    assert ev["status"] == "failed"


def test_baseline_passing_test_that_disappears_is_a_regression():
    vanished = trun(outcomes(t_a="passed", t_c="passed"))           # t_b silently gone
    ev = evaluate(UPGRADE, {py: cmp(vanished, probes=SAME_PROBES) for py in PY})
    assert req(ev, "no_regressions")["result"] == "failed"
    assert any("t_b" in e for e in req(ev, "no_regressions")["evidence"])


def test_upgrade_must_actually_install_the_requested_version():
    ev = evaluate(UPGRADE, {py: cmp(GOOD, probes=SAME_PROBES, version="1.9") for py in PY})
    assert req(ev, "version_satisfies")["result"] == "failed" and ev["status"] == "failed"


def test_residual_code_usage_fails_and_tooling_residual_is_gap():
    plan = {"action": "remove"}
    comp = {py: cmp(GOOD, probes=SAME_PROBES) for py in PY}
    code_left = [{"kind": "import", "file": "a.py", "line": 3, "code": "import six"}]
    assert evaluate(plan, comp, residual=code_left)["status"] == "failed"
    tool_left = [{"kind": "tooling", "file": "Makefile", "line": 4, "code": "nosetests"}]
    ev = evaluate(plan, comp, residual=tool_left)
    assert ev["status"] == "verified_with_gaps"
    assert req(ev, "no_residual_tooling")["result"] == "failed"


def test_transitively_installed_original_is_reported():
    ev = evaluate({"action": "remove"}, {py: cmp(GOOD, probes=SAME_PROBES, original_still_installed=True) for py in PY})
    assert req(ev, "original_uninstalled")["result"] == "failed"
    assert ev["status"] == "verified_with_gaps"


def test_new_warnings_are_gaps():
    ev = evaluate(UPGRADE, {py: cmp(GOOD, probes=SAME_PROBES, new_warnings=["old api"]) for py in PY})
    assert ev["status"] == "verified_with_gaps"


# ------------------------------------------------------------------ combined

def combined_cmp(new):
    return {py: {"install_ok": True, "tests": compare_tests(BASE, new),
                 "warnings": compare_warnings([], [])} for py in PY}


def test_individual_pass_but_combined_fails():
    # each individual migration verified (see test_fully_verified_upgrade); together t_a breaks
    together = trun(outcomes(t_a="failed", t_b="passed", t_c="passed"))
    ev = V.evaluate_combined(["x", "y"], [], combined_cmp(together), {})
    assert ev["status"] == "failed"
    assert req(ev, "no_regressions")["result"] == "failed"


def test_combined_partial_when_a_migration_conflicts():
    ev = V.evaluate_combined(["x"], [{"dependency": "y", "reason": "merge conflict in a.py"}], combined_cmp(GOOD), {})
    assert ev["status"] == "partial"
    assert req(ev, "all_merged")["evidence"] == ["y: merge conflict in a.py"]


def test_combined_residual_and_install_failure():
    ev = V.evaluate_combined(["six"], [], combined_cmp(GOOD),
                             {"six": [{"kind": "import", "file": "a.py", "line": 1, "code": "import six"}]})
    assert ev["status"] == "failed"
    ev = V.evaluate_combined(["x"], [], {"3.12": {"install_ok": False}}, {})
    assert ev["status"] == "failed"


# ------------------------------------------------------------------ comparison primitives

def test_compare_tests_classifies_outcomes():
    new = trun(outcomes(t_a="failed", t_c="passed", t_new="passed"))
    c = compare_tests(BASE, new)
    assert [r["test"] for r in c["regressed"]] == ["t_a"]
    assert c["fixed"] == ["t_c"] and c["vanished_passing"] == ["t_b"] and c["only_in_replacement"] == ["t_new"]


def test_compare_probes_detects_value_and_exception_type_differences():
    o = {"a": {"ok": True, "value": [1]}, "b": {"ok": False, "error_type": "KeyError"}, "c": {"ok": True, "value": 1}}
    r = {"a": {"ok": True, "value": [1]}, "b": {"ok": False, "error_type": "ValueError"}}
    p = compare_probes(o, r)
    assert p["identical"] == 1 and {d["probe"] for d in p["different"]} == {"b", "c"}
    assert compare_probes(None, r) == {"executed": False}
