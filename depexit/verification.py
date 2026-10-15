"""Verification semantics: what must be true before a migration counts as verified.

Every exit gets an explicit list of verification requirements *before* anything is
executed (`requirements_for`). After execution each requirement is evaluated to one of

  met          - executed evidence shows it holds
  failed       - executed/observed evidence shows it does not hold
  not_checked  - it could not be checked (skipped, no tests, no probes, infra error)

and the overall status is derived from those results only (`evaluate`):

  failed              any required check failed
  incomplete          nothing failed, but at least one required check was not performed
  verified_with_gaps  all required checks met; advisory checks failed or were not performed
  verified            every check met

A skipped check is never counted as a pass, and a successful install alone never
verifies anything: the "no regressions" requirement needs at least one baseline test
that passed and was re-run.
"""
from __future__ import annotations

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

MET, FAILED, NOT_CHECKED = "met", "failed", "not_checked"
REMOVES_ORIGINAL = {"remove", "replace", "stdlib"}
INSTALLS_PACKAGE = {"upgrade", "replace", "declare"}


def requirements_for(plan: dict, pythons: list[str], code_changes_expected: bool) -> list[dict]:
    """The checks a migration must pass, decided from the plan before execution."""
    action = plan.get("action")
    py = ", ".join(pythons)
    reqs = [
        {"id": "install", "required": True,
         "description": f"Migrated project installs on Python {py}"},
        {"id": "suite_runs", "required": True,
         "description": "Test suite still starts (no new collection error)"},
        {"id": "no_regressions", "required": True,
         "description": "Every baseline-passing test is re-run and still passes"},
    ]
    if action in INSTALLS_PACKAGE or action == "stdlib":
        reqs.append({"id": "replacement_imports", "required": True,
                     "description": "Replacement module imports in the migrated environment"})
    if action == "upgrade" and plan.get("specifier"):
        reqs.append({"id": "version_satisfies", "required": True,
                     "description": f"Installed {plan.get('package')} satisfies `{plan.get('specifier')}`"})
    if action in REMOVES_ORIGINAL:
        reqs.append({"id": "no_residual_code", "required": True,
                     "description": "Re-scan finds no remaining Python import/use of the old dependency"})
        reqs.append({"id": "original_uninstalled", "required": False,
                     "description": "Old distribution is no longer installed at all"})
        reqs.append({"id": "no_residual_tooling", "required": False,
                     "description": "No build/CI file still references the old dependency"})
    reqs.append({"id": "behaviour_probes", "required": bool(code_changes_expected),
                 "description": "Behaviour probes give identical results on original and migrated code"
                 + (" (required: code is rewritten)" if code_changes_expected else "")})
    reqs.append({"id": "no_new_warnings", "required": False,
                 "description": "No new warnings compared with the baseline run"})
    return reqs


def _per_python(comparison: dict, fn) -> tuple[str, list[str]]:
    """Combine per-Python results: any failed -> failed, any not_checked -> not_checked."""
    results, ev = [], []
    for py, c in comparison.items():
        r, e = fn(py, c)
        results.append(r)
        if e:
            ev.append(f"py{py}: {e}")
    if not results:
        return NOT_CHECKED, ["no Python environment was verified"]
    if FAILED in results:
        return FAILED, ev
    if NOT_CHECKED in results:
        return NOT_CHECKED, ev
    return MET, ev


def _install(py, c):
    return (MET, "installed") if c.get("install_ok") else (FAILED, "install failed")


def _suite_runs(py, c):
    if not c.get("install_ok"):
        return NOT_CHECKED, "not run: install failed"
    t = c.get("tests") or {}
    if t.get("replacement_collection_error") and not t.get("original_collection_error"):
        return FAILED, "test collection fails after migration"
    if t.get("replacement_collection_error") and t.get("original_collection_error"):
        return NOT_CHECKED, "test collection already failed on the baseline"
    return MET, None


def _no_regressions(py, c):
    if not c.get("install_ok"):
        return NOT_CHECKED, "tests not run: install failed"
    t = c.get("tests") or {}
    base_passed = (t.get("original") or {}).get("passed", 0)
    regressed = t.get("regressed") or []
    vanished = t.get("vanished_passing") or []
    if regressed or vanished:
        names = [r["test"] if isinstance(r, dict) else r for r in regressed] + list(vanished)
        return FAILED, f"{len(names)} baseline-passing test(s) fail or did not run: " + ", ".join(names[:5])
    if base_passed == 0:
        return NOT_CHECKED, "baseline has no passing tests, so regressions cannot be detected"
    if (t.get("replacement") or {}).get("passed", 0) == 0:
        return NOT_CHECKED, "no test passed in the migrated run"
    return MET, f"{base_passed} baseline-passing test(s) still pass"


def _replacement_imports(py, c):
    if not c.get("install_ok"):
        return NOT_CHECKED, "install failed"
    imp = c.get("replacement_import")
    if not imp:
        return NOT_CHECKED, "could not determine which module to import"
    return (MET, f"import {imp.get('module')} ok") if imp.get("ok") else \
        (FAILED, f"import {imp.get('module')}: {imp.get('error')}")


def _version_satisfies(plan):
    def check(py, c):
        if not c.get("install_ok"):
            return NOT_CHECKED, "install failed"
        ver = (c.get("installed") or {}).get("replacement_version")
        if not ver:
            return FAILED, f"{plan.get('package')} is not installed"
        try:
            ok = SpecifierSet(plan.get("specifier") or "").contains(Version(ver), prereleases=True)
        except (InvalidSpecifier, InvalidVersion):
            return NOT_CHECKED, f"cannot compare {ver} with {plan.get('specifier')}"
        return (MET, f"{ver}") if ok else (FAILED, f"{ver} does not satisfy {plan.get('specifier')}")
    return check


def _probes(py, c):
    if not c.get("install_ok"):
        return NOT_CHECKED, "install failed"
    p = c.get("probes")
    if not p or not p.get("executed"):
        return NOT_CHECKED, "no behaviour probes were executed"
    if p.get("infrastructure_error"):
        return NOT_CHECKED, "probe runner failed to load the probes"
    if p.get("different"):
        return FAILED, f"{len(p['different'])} of {p.get('total')} probe(s) differ"
    if not p.get("total"):
        return NOT_CHECKED, "probe module defined no probes"
    return MET, f"{p.get('identical')}/{p.get('total')} identical"


def _original_uninstalled(py, c):
    if not c.get("install_ok"):
        return NOT_CHECKED, "install failed"
    inst = c.get("installed") or {}
    return (FAILED, "still installed by another package") if inst.get("original_still_installed") \
        else (MET, None)


def _warnings(py, c):
    if not c.get("install_ok"):
        return NOT_CHECKED, "install failed"
    w = c.get("warnings")
    if w is None:
        return NOT_CHECKED, "warnings not collected"
    return (FAILED, f"{len(w.get('new', []))} new warning(s)") if w.get("new") else (MET, None)


def evaluate(plan: dict, requirements: list[dict], comparison: dict, residual: list[dict]) -> dict:
    """Evaluate each requirement against executed evidence; derive the status."""
    code_residual = [r for r in residual or [] if r.get("kind") != "tooling"]
    tool_residual = [r for r in residual or [] if r.get("kind") == "tooling"]
    checks = {
        "install": lambda: _per_python(comparison, _install),
        "suite_runs": lambda: _per_python(comparison, _suite_runs),
        "no_regressions": lambda: _per_python(comparison, _no_regressions),
        "replacement_imports": lambda: _per_python(comparison, _replacement_imports),
        "version_satisfies": lambda: _per_python(comparison, _version_satisfies(plan)),
        "behaviour_probes": lambda: _per_python(comparison, _probes),
        "original_uninstalled": lambda: _per_python(comparison, _original_uninstalled),
        "no_new_warnings": lambda: _per_python(comparison, _warnings),
        "no_residual_code": lambda: (
            (FAILED, [f"{r['file']}:{r['line']} `{r['code'][:100]}`" for r in code_residual[:10]])
            if code_residual else (MET, ["re-scan of the migrated tree found no Python usage"])),
        "no_residual_tooling": lambda: (
            (FAILED, [f"{r['file']}:{r['line']} `{r['code'][:100]}`" for r in tool_residual[:10]])
            if tool_residual else (MET, [])),
    }
    out = []
    for req in requirements:
        fn = checks.get(req["id"])
        result, evidence = fn() if fn else (NOT_CHECKED, ["no evaluator for this requirement"])
        out.append({**req, "result": result, "evidence": evidence,
                    "provenance": "observed" if req["id"].startswith("no_residual") else "executed"})
    return {"status": status_from(out), "requirements": out}


def status_from(evaluated: list[dict]) -> str:
    req = [r for r in evaluated if r["required"]]
    adv = [r for r in evaluated if not r["required"]]
    if any(r["result"] == FAILED for r in req):
        return "failed"
    if any(r["result"] == NOT_CHECKED for r in req):
        return "incomplete"
    if any(r["result"] != MET for r in adv):
        return "verified_with_gaps"
    return "verified"


def is_clean(evaluation: dict) -> bool:
    """Good enough to stop the fix loop: no required failure and no behaviour difference."""
    for r in evaluation["requirements"]:
        if r["result"] == FAILED and (r["required"] or r["id"] == "behaviour_probes"):
            return False
    return True


def evaluate_combined(applied: list[str], skipped: list[dict], comparison: dict,
                      residual_by_dep: dict[str, list[dict]]) -> dict:
    """Status of every individually verified exit applied together, re-verified."""
    reqs = [
        {"id": "install", "required": True, "description": "Combined migration installs on every Python"},
        {"id": "suite_runs", "required": True, "description": "Test suite still starts"},
        {"id": "no_regressions", "required": True,
         "description": "No baseline-passing test fails with all migrations applied together"},
        {"id": "no_residual_code", "required": True,
         "description": "No removed/replaced dependency is still used in the combined tree"},
        {"id": "no_new_warnings", "required": False, "description": "No new warnings vs baseline"},
        {"id": "all_merged", "required": False,
         "description": "Every individually verified migration merged without conflict"},
    ]
    leftovers = [f"{d}: {r['file']}:{r['line']}" for d, rs in residual_by_dep.items()
                 for r in rs if r.get("kind") != "tooling"]
    evaluated = []
    for req in reqs:
        if req["id"] == "install":
            res, ev = _per_python(comparison, _install)
        elif req["id"] == "suite_runs":
            res, ev = _per_python(comparison, _suite_runs)
        elif req["id"] == "no_regressions":
            res, ev = _per_python(comparison, _no_regressions)
        elif req["id"] == "no_new_warnings":
            res, ev = _per_python(comparison, _warnings)
        elif req["id"] == "no_residual_code":
            res, ev = (FAILED, leftovers[:10]) if leftovers else (MET, [])
        else:
            res = FAILED if skipped else MET
            ev = [f"{s['dependency']}: {s['reason']}" for s in skipped]
        evaluated.append({**req, "result": res, "evidence": ev,
                          "provenance": "observed" if req["id"] == "no_residual_code" else "executed"})
    status = status_from(evaluated)
    if status in ("verified", "verified_with_gaps") and skipped:
        status = "partial"
    if not applied:
        status = "failed"
    return {"status": status, "requirements": evaluated}
