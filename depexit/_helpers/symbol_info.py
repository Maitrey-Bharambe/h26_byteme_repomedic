"""Runs INSIDE the target venv: classify dependency symbols before sabotaging them.

usage: symbol_info.py '["toml.load", "requests.Session"]'  -> prints {symbol: kind}
kind is "callable", "class", "other" or "unresolvable: <reason>".
"""
import inspect
import json
import os
import sys

sys.path.insert(0, os.getcwd())
from _depexit_common import resolve  # noqa: E402

out = {}
for sym in json.loads(sys.argv[1]):
    try:
        _, _, obj = resolve(sym)
        out[sym] = "class" if inspect.isclass(obj) else "callable" if callable(obj) else "other"
    except Exception as exc:  # noqa: BLE001
        out[sym] = f"unresolvable: {type(exc).__name__}: {exc}"[:200]
print(json.dumps(out))
