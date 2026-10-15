"""Shared by the helpers that run INSIDE the target venv (sabotage, record, replay).

Stdlib only: this module is imported into someone else's interpreter.
"""
import importlib
import os
import sys


def describe(value, depth=0):
    """JSON-friendly, comparable form of any value."""
    if depth > 4:
        return {"type": type(value).__name__, "repr": repr(value)[:300]}
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, bytes):
        return {"type": "bytes", "len": len(value), "head": value[:200].decode("latin-1")}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=repr) if isinstance(value, (set, frozenset)) else list(value)
        return {"type": type(value).__name__, "items": [describe(v, depth + 1) for v in items[:100]]}
    if isinstance(value, dict):
        return {"type": "dict", "items": {str(k): describe(v, depth + 1) for k, v in list(value.items())[:100]}}
    if isinstance(value, os.PathLike):
        return {"type": "path", "value": os.fspath(value)}
    fields = getattr(value, "__dict__", None)
    if isinstance(fields, dict) and depth < 3:
        return {"type": type(value).__name__,
                "fields": {k: describe(v, depth + 1) for k, v in list(fields.items())[:50] if not k.startswith("_")}}
    return {"type": type(value).__name__, "repr": repr(value)[:300]}


def resolve(dotted):
    """'pkg.mod.Class.attr' -> (parent, attr_name, current_value), longest importable module first."""
    parts = dotted.split(".")
    for i in range(len(parts) - 1, 0, -1):
        try:
            parent = importlib.import_module(".".join(parts[:i]))
        except Exception:
            continue
        try:
            for a in parts[i:-1]:
                parent = getattr(parent, a)
            return parent, parts[-1], getattr(parent, parts[-1])
        except AttributeError:
            continue
    raise LookupError(f"cannot resolve {dotted}")


def resolve_qualname(module, qualname):
    obj = importlib.import_module(module)
    parent = obj
    for part in qualname.split("."):
        parent, obj = obj, getattr(obj, part)
    return parent, qualname.split(".")[-1], obj


def repo_roots():
    return [os.path.normcase(os.path.abspath(os.environ.get("DEPEXIT_REPO_ROOT") or os.getcwd()))]


def rebind(original, replacement, roots=None):
    """Replace names already bound to `original` in imported repository modules
    (`from x import y` copies the reference, so patching x.y alone is not enough)."""
    roots = roots or repo_roots()
    n = 0
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if not f or not any(os.path.normcase(os.path.abspath(f)).startswith(r) for r in roots):
            continue
        try:
            items = list(vars(m).items())
        except TypeError:
            continue
        for k, v in items:
            if v is original:
                setattr(m, k, replacement)
                n += 1
    return n
