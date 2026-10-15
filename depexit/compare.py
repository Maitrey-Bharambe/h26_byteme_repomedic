"""Compare executed results: original dependency vs proposed replacement."""
from __future__ import annotations

import difflib
from pathlib import Path

from .sandbox import TestRun, attribute_text


def compare_tests(base: TestRun, new: TestRun) -> dict:
    fixed, regressed, still, missing, added = [], [], [], [], []
    bad = {"failed", "error"}
    for nid, b in base.tests.items():
        n = new.tests.get(nid)
        if n is None:
            missing.append(nid)
        elif b["outcome"] in bad and n["outcome"] == "passed":
            fixed.append(nid)
        elif b["outcome"] == "passed" and n["outcome"] in bad:
            regressed.append({"test": nid, "message": n["message"][-1200:]})
        elif b["outcome"] in bad and n["outcome"] in bad:
            still.append(nid)
    for nid in new.tests:
        if nid not in base.tests:
            added.append(nid)
    # a baseline-passing test that silently disappears is not evidence of success
    vanished = [nid for nid in missing if base.tests[nid]["outcome"] == "passed"]
    return {
        "original": base.counts(), "replacement": new.counts(),
        "fixed": fixed, "regressed": regressed, "still_failing": still,
        "not_run_in_replacement": missing, "vanished_passing": vanished,
        "only_in_replacement": added,
        "original_collection_error": base.collection_error,
        "replacement_collection_error": new.collection_error,
    }


def _wkey(w):
    return (w.get("category"), (w.get("message") or "")[:160])


def compare_warnings(base: list, new: list) -> dict:
    b = {_wkey(w) for w in base}
    n = {_wkey(w) for w in new}
    return {"new": [{"category": c, "message": m} for c, m in sorted(n - b, key=str)][:30],
            "resolved": [{"category": c, "message": m} for c, m in sorted(b - n, key=str)][:30]}


def _probe_key(r: dict):
    if not isinstance(r, dict):
        return ("invalid", repr(r))
    if r.get("ok"):
        return ("ok", repr(r.get("value")))
    return ("error", r.get("error_type"))


def compare_probes(orig: dict | None, repl: dict | None) -> dict:
    if orig is None or repl is None:
        return {"executed": False}
    names = sorted(set(orig) | set(repl))
    same, diffs = [], []
    for name in names:
        o, r = orig.get(name), repl.get(name)
        if o is not None and r is not None and _probe_key(o) == _probe_key(r):
            same.append(name)
        else:
            diffs.append({"probe": name, "original": o, "replacement": r})
    return {"executed": True, "total": len(names), "identical": len(same), "different": diffs,
            "infrastructure_error": "__load__" in orig or "__runner__" in orig
                                    or "__load__" in repl or "__runner__" in repl}


def failure_feedback(cmp_by_py: dict, modules: dict, residual: list | None = None) -> str:
    lines = []
    for r in (residual or []):
        if r["kind"] != "tooling":
            lines.append(f"{r['file']}:{r['line']} still uses the removed dependency: `{r['code']}`")
    for py, c in cmp_by_py.items():
        t = c["tests"]
        if t["replacement_collection_error"] and not t["original_collection_error"]:
            lines.append(f"[Python {py}] test collection failed:\n{t['replacement_collection_error'][-1500:]}")
        for r in t["regressed"][:5]:
            lines.append(f"[Python {py}] {r['test']} now fails:\n{r['message'][-1200:]}")
        if c.get("install_ok", True) and t.get("vanished_passing"):
            lines.append(f"[Python {py}] these previously passing tests no longer run: "
                         + ", ".join(t["vanished_passing"][:10]))
        for d in (c.get("probes") or {}).get("different", [])[:8]:
            lines.append(f"[Python {py}] behaviour probe `{d['probe']}` differs: "
                         f"original={d['original']} replacement={d['replacement']}")
        if not c.get("install_ok", True):
            lines.append(f"[Python {py}] installing the replacement failed:\n{c.get('install_error', '')[-1200:]}")
    return "\n\n".join(lines)[:8000]


def unified_diff(orig_root: Path, new_root: Path, files: list[str]) -> str:
    out = []
    for rel in sorted(set(files)):
        a = (orig_root / rel).read_text(encoding="utf-8").splitlines(keepends=True) if (orig_root / rel).exists() else []
        b = (new_root / rel).read_text(encoding="utf-8").splitlines(keepends=True) if (new_root / rel).exists() else []
        out.extend(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}"))
    return "".join(out)
