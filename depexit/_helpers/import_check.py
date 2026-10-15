"""Runs INSIDE the target venv: import one module, report error + warnings as JSON."""
import importlib
import json
import sys
import traceback
import warnings

mod = sys.argv[1]
out = {"module": mod, "ok": True, "error": None, "warnings": []}
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    try:
        importlib.import_module(mod)
    except BaseException as exc:  # noqa: BLE001 - we want everything, incl. SystemExit
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["traceback"] = traceback.format_exc()[-2000:]
for w in caught:
    out["warnings"].append({"category": w.category.__name__, "message": str(w.message)[:400],
                            "filename": w.filename, "lineno": w.lineno})
print(json.dumps(out))
