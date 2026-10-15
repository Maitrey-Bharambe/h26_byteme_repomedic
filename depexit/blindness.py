"""Test blindness check: can the repository's own tests even notice a broken dependency?

For every dependency symbol the code actually uses (calls/attributes/references), the
ORIGINAL test suite is re-run with that symbol sabotaged (raise / return None / return a
wrong value of the same type). A passing suite under sabotage means the tests are blind
to that dependency, so a green test run after migrating it proves little.

Every result is executed evidence: command, exit code, call count and catching tests.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import sandbox
from .usage import _is_test_file

MODES = ("raise", "none", "wrong")
SABOTAGE_KINDS = {"call", "attribute", "reference"}
CLASSES = ("watched", "partly_blind", "blind", "not_exercised", "not_run", "not_applicable")


def candidate_symbols(dep, limit: int = 5) -> list[str]:
    """Most-used dependency symbols in non-test repository code."""
    counts: dict[str, int] = {}
    for u in dep.usage:
        if u["kind"] in SABOTAGE_KINDS and "." in u["symbol"] and not _is_test_file(u["file"]):
            counts[u["symbol"]] = counts.get(u["symbol"], 0) + 1
    # drop a symbol when a longer one under it is also used (patch the leaf that is called)
    syms = [s for s in counts if not any(o != s and o.startswith(s + ".") for o in counts)]
    return sorted(syms, key=lambda s: (-counts[s], s))[:limit]


def _catching(base: sandbox.TestRun, run: sandbox.TestRun) -> list[str]:
    caught = []
    for nid, t in base.tests.items():
        if t["outcome"] != "passed":
            continue
        n = run.tests.get(nid)
        if n is None or n["outcome"] in ("failed", "error"):
            caught.append(nid)
    if run.collection_error and not base.collection_error:
        caught.insert(0, "<test collection>")
    return caught


def _one(env, repo: Path, base: sandbox.TestRun, symbol: str, mode: str, wdir: Path, timeout: int) -> dict:
    rcopy = sandbox.copy_repo(repo, wdir / "repo")
    out = wdir / "sabotage.json"
    out.unlink(missing_ok=True)
    cfg = {"symbol": symbol, "mode": mode, "out": str(out)}
    tr = sandbox.run_tests(env, rcopy, wdir / "results", plugins=("depexit_sabotage",),
                           extra_env={"DEPEXIT_SABOTAGE": json.dumps(cfg)}, timeout=timeout)
    st = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    res = {"symbol": symbol, "mode": mode, "command": tr.command,
           "exit_code": tr.exit_code, "seconds": round(tr.seconds, 1),
           "log": str(wdir / "results" / "pytest.log")}
    if tr.exit_code == -9:
        return {**res, "outcome": "not_run", "reason": "timed out"}
    if st is None:
        return {**res, "outcome": "not_run", "reason": "sabotage plugin did not report (pytest crashed?)"}
    if st.get("not_applicable"):
        return {**res, "outcome": "not_applicable", "reason": st["not_applicable"], "calls": 0}
    if st.get("error") or not st.get("installed"):
        return {**res, "outcome": "not_run", "reason": st.get("error") or "patch not installed"}
    caught = _catching(base, tr)
    if not st.get("finished") and tr.exit_code != 0:
        # pytest itself died after the sabotage fired (e.g. at import/collection): that IS detection
        if st["calls"] == 0:
            return {**res, "outcome": "not_run", "reason": f"pytest crashed before calling the symbol (exit {tr.exit_code})"}
        caught = ["<pytest crashed at startup>"] + caught
    res.update(calls=st["calls"], rebound=st["rebound"], kind=st["kind"], catching_tests=caught[:25])
    if st["calls"] == 0:
        res["outcome"] = "not_exercised"
    else:
        res["outcome"] = "detected" if caught else "undetected"
    return res


def classify(runs: list[dict]) -> tuple[str, str]:
    """Dependency-level classification from its (symbol, mode) runs."""
    applicable = [r for r in runs if r["outcome"] != "not_applicable"]
    if not applicable:
        return "not_run", "no sabotage mode applies to the used symbols"
    broken = [r for r in applicable if r["outcome"] == "not_run"]
    if broken:
        return "not_run", "; ".join(f"{r['symbol']} [{r['mode']}]: {r['reason']}" for r in broken[:3])
    exercised = [r for r in applicable if r["outcome"] in ("detected", "undetected")]
    if not exercised:
        return "not_exercised", "no test calls any of the sabotaged symbols"
    per_mode = {}
    for r in exercised:
        per_mode[r["mode"]] = per_mode.get(r["mode"], False) or r["outcome"] == "detected"
    detected = sorted(m for m, d in per_mode.items() if d)
    missed = sorted(m for m, d in per_mode.items() if not d)
    if not missed:
        return "watched", f"every sabotage mode ({', '.join(detected)}) made a test fail"
    if not detected:
        return "blind", f"tests still pass when {', '.join(missed)} sabotage is applied"
    return "partly_blind", f"detected: {', '.join(detected)}; undetected: {', '.join(missed)}"


def run_check(env, repo: Path, base: sandbox.TestRun, deps: list, work: Path, python: str,
              jobs: int = 4, timeout: int = 600, log=lambda m: None) -> dict[str, dict]:
    """Blindness result per dependency name (provenance: executed)."""
    results: dict[str, dict] = {}
    plan: dict[str, list[str]] = {}
    for dep in deps:
        syms = candidate_symbols(dep)
        code_usage = [u for u in dep.usage if u["kind"] != "tooling" and not _is_test_file(u["file"])]
        if not syms:
            cls = "not_applicable" if not [u for u in code_usage if u["kind"] != "import"] else "not_run"
            why = ("the dependency is not used by repository code (nothing to sabotage)"
                   if cls == "not_applicable" else "only imports/subclassing/monkeypatching found; no callable symbol to sabotage")
            results[dep.name] = {"provenance": "executed", "classification": cls, "reason": why,
                                 "python": python, "symbols": [], "runs": []}
        else:
            plan[dep.name] = syms
    if not plan:
        return results
    base_passed = sum(1 for t in base.tests.values() if t["outcome"] == "passed")
    if not env.ok or base_passed == 0:
        why = "baseline environment failed to install" if not env.ok else "baseline has no passing test to break"
        for name, syms in plan.items():
            results[name] = {"provenance": "executed", "classification": "not_run", "reason": why,
                             "python": python, "symbols": syms, "runs": []}
        return results
    all_syms = sorted({s for syms in plan.values() for s in syms})
    work.mkdir(parents=True, exist_ok=True)
    info = sandbox.run_helper(env, repo, "symbol_info.py", [json.dumps(all_syms)], work, timeout=120)
    try:
        kinds = json.loads(info.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        kinds = {s: f"unresolvable: symbol_info failed: {(info.stderr or '')[-200:]}" for s in all_syms}
    jobs_list = []
    for name, syms in plan.items():
        for i, sym in enumerate(syms):
            kind = kinds.get(sym, "unresolvable")
            modes = MODES if kind == "callable" else ("raise",) if kind == "class" else ()
            if not modes:
                jobs_list.append((name, sym, None, kind))
            for m in modes:
                jobs_list.append((name, sym, m, kind))
    log(f"test blindness: {sum(1 for j in jobs_list if j[2])} sabotaged test runs on Python {python}")

    def work_one(job):
        name, sym, mode, kind = job
        if mode is None:
            outcome = "not_applicable" if kind == "other" else "not_run"
            return name, {"symbol": sym, "mode": "-", "outcome": outcome,
                          "reason": "constant/attribute, not callable" if kind == "other" else kind}
        wdir = work / name / f"{sym.replace('.', '_')}-{mode}"
        try:
            return name, _one(env, repo, base, sym, mode, wdir, timeout)
        except Exception as exc:  # an orchestration crash is recorded, never hidden
            return name, {"symbol": sym, "mode": mode, "outcome": "not_run",
                          "reason": f"{type(exc).__name__}: {exc}"}

    runs: dict[str, list] = {n: [] for n in plan}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        for name, r in ex.map(work_one, jobs_list):
            runs[name].append(r)
    for name, syms in plan.items():
        cls, why = classify(runs[name])
        caught = sorted({t for r in runs[name] for t in r.get("catching_tests", [])})
        results[name] = {"provenance": "executed", "classification": cls, "reason": why, "python": python,
                         "symbols": syms, "symbol_kinds": {s: kinds.get(s) for s in syms},
                         "runs": runs[name], "catching_tests": caught,
                         "calls": {s: max([r.get("calls", 0) for r in runs[name] if r["symbol"] == s] or [0]) for s in syms}}
    return results
