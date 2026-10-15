"""Replay check: record real calls during the ORIGINAL test run, replay them on the migration.

1. record():  one extra run of the original suite with the depexit_record plugin, wrapping
   the repository's own functions that contain dependency usage (usage.replay_targets).
2. replay():  replay_runner.py calls each recorded function with the same pickled inputs,
   once in the ORIGINAL environment (control) and once in the MIGRATED environment.
3. compare.compare_replay() classifies each call: identical / different /
   exception_changed / not_replayable. A call whose control replay differs from the
   recording depends on test context (fixtures, monkeypatching), so it is reported as
   not_replayable rather than compared.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import sandbox, usage
from .compare import compare_replay

MIN_RECORDED = 3   # below this, Gemma-generated probes are reported as extra (ai_generated) inputs


def targets_for(repo: Path, dep) -> list[dict]:
    return usage.replay_targets(repo, dep.usage)


def record(env, repo: Path, targets: list[dict], work: Path, timeout: int = 1200) -> dict:
    """Run the original suite once, recording every call of every replayable target."""
    replayable = [t for t in targets if t["replayable"]]
    out = {"provenance": "executed_recorded", "targets": targets, "calls": {}, "status": {},
           "error": None, "command": None}
    if not replayable:
        return out
    work.mkdir(parents=True, exist_ok=True)
    rec_file = work / "recorded.jsonl"
    cfg_file = work / "record_config.json"
    rec_file.unlink(missing_ok=True)
    cfg_file.write_text(json.dumps({"targets": replayable, "out": str(rec_file), "max_calls": 200}),
                        encoding="utf-8")
    rcopy = sandbox.copy_repo(repo, work / "repo")
    tr = sandbox.run_tests(env, rcopy, work / "results", plugins=("depexit_record",),
                           extra_env={"DEPEXIT_RECORD": str(cfg_file)}, timeout=timeout)
    out["command"] = tr.command
    out["test_counts"] = tr.counts()
    status_file = Path(str(rec_file) + ".status.json")
    if not status_file.exists():
        out["error"] = f"recording run did not complete (exit {tr.exit_code}): " \
                       + (tr.collection_error or tr.log[-500:])
        return out
    out["status"] = json.loads(status_file.read_text(encoding="utf-8"))
    by_module = {t["function"]: t for t in replayable}
    for i, line in enumerate(rec_file.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        r = json.loads(line)
        t = by_module.get(r["function"])
        if t:
            r.update(id=i, module=t["module"], qualname=t["qualname"])
            out["calls"].setdefault(r["function"], []).append(r)
    out["repo"] = str(rcopy)
    out["env_dir"] = str(env.dir)
    return out


def _run(env, repo: Path, records: list[dict], work: Path, timeout: int) -> tuple[list | None, str | None]:
    work.mkdir(parents=True, exist_ok=True)
    src, dst = work / "replay_in.jsonl", work / "replay_out.json"
    dst.unlink(missing_ok=True)
    src.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    r = sandbox.run_helper(env, repo, "replay_runner.py", [str(src), str(dst)], work, timeout=timeout)
    if not dst.exists():
        return None, f"replay runner failed (exit {r.returncode}): {(r.stderr or r.stdout)[-600:]}"
    return json.loads(dst.read_text(encoding="utf-8")), None


def replay(recording: dict, functions: list[str], base_env, base_repo: Path, new_env, new_repo: Path,
           work: Path, python: str, probe_summary: dict | None = None, timeout: int = 600) -> dict:
    """Replay the recorded calls of `functions` on original (control) and migrated code."""
    targets = list({t["function"]: t for t in recording.get("targets", [])
                    if t["function"] in functions}.values())
    functions = list(dict.fromkeys(functions))
    section = {"provenance": "executed_recorded", "python": python,
               "functions": [], "counts": {"identical": 0, "different": 0, "exception_changed": 0,
                                           "not_replayable": 0},
               "differences": [], "not_replayable": [], "skipped": [], "state": None,
               "command": "replay_runner.py <records> <out> (original control env, then migrated env)"}
    for t in targets:
        if not t["replayable"]:
            section["skipped"].append({"function": t["function"], "reason": t["skip"]})
    if recording.get("error"):
        section.update(state="not_run", reason=recording["error"])
        return section
    records = [r for f in functions for r in recording.get("calls", {}).get(f, [])]
    for t in targets:
        if t["replayable"]:
            n = len(recording.get("calls", {}).get(t["function"], []))
            err = (recording.get("status") or {}).get("errors", {}).get(t["function"])
            section["functions"].append({"function": t["function"], "recorded_calls": n,
                                         "record_error": err})
    if not targets:
        section.update(state="no_targets", reason="no repository function contains usage of this dependency")
        return section
    if not records:
        section.update(state="no_recorded_calls",
                       reason="no test called the functions that use this dependency"
                       if not section["skipped"] or section["functions"] else
                       "every function using this dependency was skipped as unsafe to replay")
    else:
        control, err1 = _run(base_env, base_repo, records, work / "control", timeout)
        migrated, err2 = (None, None) if err1 else _run(new_env, new_repo, records, work / "migrated", timeout)
        if err1 or err2:
            section.update(state="not_run", reason=err1 or err2)
            return section
        roots = {"original": [base_repo, base_env.dir, Path(recording.get("repo") or base_repo)],
                 "migrated": [new_repo, new_env.dir]}
        cmp = compare_replay(records, control, migrated, roots)
        section["counts"] = cmp["counts"]
        section["differences"] = cmp["differences"]
        section["not_replayable"] = cmp["not_replayable"]
        section["identical_examples"] = cmp["identical_examples"]
        section["state"] = "compared"
    few = [f["function"] for f in section["functions"] if f["recorded_calls"] < MIN_RECORDED]
    if few:
        section["ai_generated_inputs"] = {
            "provenance": "ai_generated", "functions": few,
            "note": f"fewer than {MIN_RECORDED} recorded calls; Gemma behaviour probes add extra inputs "
                    "(reported separately, not counted as recorded evidence)",
            "probes": probe_summary or {"executed": False}}
    return section


def summary(section: dict | None) -> dict:
    """Short form for index.json / REPORT.md."""
    if not section:
        return {"state": "not_applicable"}
    c = section.get("counts", {})
    return {"state": section.get("state"), "identical": c.get("identical", 0),
            "different": c.get("different", 0) + c.get("exception_changed", 0),
            "not_replayable": c.get("not_replayable", 0), "skipped": len(section.get("skipped", []))}
