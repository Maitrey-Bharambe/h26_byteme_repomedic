"""Runs INSIDE the target venv with the repo on sys.path.

Loads a probe module (AI-generated) exposing `PROBES = {name: callable}` and records
each probe's observable result, so the same probes can be compared across environments.
"""
import importlib.util
import json
import sys
import traceback
import warnings


def describe(value, depth=0):
    if depth > 3:
        return repr(value)[:300]
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [describe(v, depth + 1) for v in list(value)[:50]]
    if isinstance(value, dict):
        return {str(k): describe(v, depth + 1) for k, v in list(value.items())[:50]}
    return {"type": type(value).__name__, "repr": repr(value)[:300]}


path, out = sys.argv[1], sys.argv[2]
results = {}
try:
    spec = importlib.util.spec_from_file_location("depexit_probes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    probes = dict(getattr(mod, "PROBES", {}))
except BaseException as exc:  # noqa: BLE001
    results["__load__"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                           "traceback": traceback.format_exc()[-1500:]}
    probes = {}
for name, fn in probes.items():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            value = fn()
            results[name] = {"ok": True, "value": describe(value)}
        except BaseException as exc:  # noqa: BLE001
            results[name] = {"ok": False, "error_type": type(exc).__name__,
                             "error": str(exc)[:400]}
    results[name]["warnings"] = sorted({w.category.__name__ for w in caught})
with open(out, "w", encoding="utf-8") as fh:
    json.dump(results, fh, indent=1, default=str)
