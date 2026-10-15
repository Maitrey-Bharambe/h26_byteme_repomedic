"""pytest plugin (-p depexit_record): record real calls of repository functions.

Config file path in $DEPEXIT_RECORD: {"targets": [{"function", "module", "qualname"}],
"out": path.jsonl, "max_calls": 200}
Each call becomes one JSONL line: function, test nodeid, base64 pickle of (args, kwargs),
and the normalised output or the exception type. Identical inputs are recorded once.
Calls whose args or output cannot be pickled are kept as not_replayable with the reason.
"""
import base64
import functools
import hashlib
import inspect
import json
import os
import pickle

import pytest

from _depexit_common import describe, rebind, resolve_qualname

CFG = {}
_cfg_path = os.environ.get("DEPEXIT_RECORD")
if _cfg_path:
    with open(_cfg_path, encoding="utf-8") as _fh:
        CFG = json.load(_fh)
MAX = int(CFG.get("max_calls", 200))
STATE = {"test": None, "installed": False}
RECORDS, SEEN, COUNT, ERRORS = [], set(), {}, {}


def _wrap(fid, fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        if COUNT.get(fid, 0) >= MAX:
            return fn(*a, **k)
        rec = {"function": fid, "test": STATE["test"]}
        try:
            payload = pickle.dumps((a, k), protocol=4)
            key = hashlib.sha256(payload).hexdigest()
            rec["args_b64"] = base64.b64encode(payload).decode("ascii")
        except Exception as exc:
            payload, key = None, None
            rec["not_replayable"] = f"arguments not picklable: {type(exc).__name__}: {exc}"[:300]
        rec["input"] = repr((a, k))[:300]
        try:
            result = fn(*a, **k)
        except BaseException as exc:
            rec["output"] = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:300]}
            _store(fid, key, rec)
            raise
        rec["output"] = {"ok": True, "value": describe(result)}
        if payload is not None:
            try:
                pickle.dumps(result, protocol=4)
            except Exception as exc:
                rec.pop("args_b64", None)
                rec["not_replayable"] = f"output not picklable: {type(exc).__name__}"
        _store(fid, key, rec)
        return result
    return wrapper


def _store(fid, key, rec):
    dedupe = key or rec.get("input")
    if (fid, dedupe) in SEEN:
        return
    SEEN.add((fid, dedupe))
    COUNT[fid] = COUNT.get(fid, 0) + 1
    RECORDS.append(rec)


def _install():
    if STATE["installed"]:
        return
    STATE["installed"] = True
    for t in CFG.get("targets", []):
        fid = t["function"]
        try:
            parent, name, fn = resolve_qualname(t["module"], t["qualname"])
            if not callable(fn) or inspect.isclass(fn):
                ERRORS[fid] = "not a plain function"
                continue
            raw = inspect.getattr_static(parent, name, None)
            wrapped = _wrap(fid, fn)
            setattr(parent, name, staticmethod(wrapped) if isinstance(raw, staticmethod) else wrapped)
            rebind(fn, wrapped)
        except Exception as exc:
            ERRORS[fid] = f"{type(exc).__name__}: {exc}"[:300]


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config, parser, args):
    _install()


def pytest_configure(config):
    _install()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    STATE["test"] = item.nodeid
    yield
    STATE["test"] = None


def pytest_sessionfinish(session, exitstatus):
    out = CFG.get("out")
    if not out:
        return
    with open(out, "w", encoding="utf-8") as fh:
        for r in RECORDS:
            fh.write(json.dumps(r, default=str) + "\n")
    with open(out + ".status.json", "w", encoding="utf-8") as fh:
        json.dump({"installed": STATE["installed"], "errors": ERRORS, "counts": COUNT}, fh)
