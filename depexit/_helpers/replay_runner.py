"""Runs INSIDE a target venv with a repository on sys.path: replay recorded calls.

usage: replay_runner.py <records.jsonl> <out.json>
For every recorded call with pickled inputs: import the function by module + qualname,
unpickle the inputs, call it, and record the normalised output or exception type.
Inputs that cannot be unpickled here (for example a class of the removed library) are
reported as not_replayable, never as passed.
"""
import base64
import json
import os
import pickle
import sys

sys.path.insert(0, os.getcwd())
from _depexit_common import describe, resolve_qualname  # noqa: E402

src, out = sys.argv[1], sys.argv[2]
results = []
with open(src, encoding="utf-8") as fh:
    records = [json.loads(line) for line in fh if line.strip()]
cache = {}
for i, rec in enumerate(records):
    res = {"id": rec.get("id", i), "function": rec["function"]}
    if not rec.get("args_b64"):
        res["not_replayable"] = rec.get("not_replayable") or "no recorded input"
        results.append(res)
        continue
    try:
        if rec["function"] not in cache:
            cache[rec["function"]] = resolve_qualname(rec["module"], rec["qualname"])[2]
        fn = cache[rec["function"]]
    except Exception as exc:
        res["not_replayable"] = f"function not importable: {type(exc).__name__}: {exc}"[:300]
        results.append(res)
        continue
    try:
        args, kwargs = pickle.loads(base64.b64decode(rec["args_b64"]))
    except Exception as exc:
        res["not_replayable"] = f"inputs cannot be unpickled here: {type(exc).__name__}: {exc}"[:300]
        results.append(res)
        continue
    try:
        res["output"] = {"ok": True, "value": describe(fn(*args, **kwargs))}
    except BaseException as exc:  # noqa: BLE001 - the exception type is the observation
        res["output"] = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:300]}
    results.append(res)
with open(out, "w", encoding="utf-8") as fh:
    json.dump(results, fh, default=str)
