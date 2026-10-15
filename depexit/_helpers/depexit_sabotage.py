"""pytest plugin (-p depexit_sabotage): break ONE dependency symbol, count its calls.

Config (JSON) in $DEPEXIT_SABOTAGE: {"symbol": "toml.load", "mode": "raise|none|wrong", "out": path}
The symbol is replaced before test modules are imported, and references already bound
in repository modules via `from x import y` are replaced as well. Result JSON is written
to `out` at session end: whether the patch was installed, how often it was called.
"""
import functools
import inspect
import json
import os

import pytest

from _depexit_common import rebind, resolve

CFG = json.loads(os.environ.get("DEPEXIT_SABOTAGE") or "{}")
STATE = {"symbol": CFG.get("symbol"), "mode": CFG.get("mode"), "installed": False, "calls": 0,
         "kind": None, "rebound": 0, "error": None, "not_applicable": None}
_PATCH = {}


def _flush():
    out = CFG.get("out")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(STATE, fh)


def _wrong(v):
    if isinstance(v, bool):
        return not v
    if isinstance(v, str):
        return "__depexit_wrong__"
    if isinstance(v, bytes):
        return b"__depexit_wrong__"
    if isinstance(v, dict):
        return {}
    if isinstance(v, list):
        return []
    if isinstance(v, tuple):
        return ()
    if isinstance(v, int):
        return -999
    if isinstance(v, float):
        return -999.0
    return None


def _install():
    if STATE["installed"] or STATE["error"] or STATE["not_applicable"] or not STATE["symbol"]:
        return
    mode = STATE["mode"]
    try:
        parent, name, orig = resolve(STATE["symbol"])
        if inspect.isclass(orig):
            STATE["kind"] = "class"
            if mode != "raise":
                STATE["not_applicable"] = "constructor can only be sabotaged with mode=raise"
                return
            real_init = orig.__init__

            def init(self, *a, **k):
                STATE["calls"] += 1
                if STATE["calls"] == 1:
                    _flush()
                raise RuntimeError("depexit sabotage")
            orig.__init__ = init
            _PATCH["restore"] = lambda: setattr(orig, "__init__", real_init)
        elif callable(orig):
            STATE["kind"] = "callable"

            def wrapper(*a, **k):
                STATE["calls"] += 1
                if STATE["calls"] == 1:
                    _flush()  # survives a crash of pytest itself
                if mode == "raise":
                    raise RuntimeError("depexit sabotage")
                if mode == "none":
                    return None
                return _wrong(orig(*a, **k))
            try:
                functools.update_wrapper(wrapper, orig)
            except Exception:
                pass
            static = isinstance(inspect.getattr_static(parent, name, None), staticmethod)
            setattr(parent, name, staticmethod(wrapper) if static else wrapper)
            _PATCH.update(orig=orig, wrapper=wrapper)
            STATE["rebound"] += rebind(orig, wrapper)
        else:
            STATE["kind"] = type(orig).__name__
            STATE["not_applicable"] = "symbol is not callable"
            return
        STATE["installed"] = True
        _flush()
    except Exception as exc:  # recorded, never treated as a detection
        STATE["error"] = f"{type(exc).__name__}: {exc}"
        _flush()


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config, parser, args):
    _install()


def pytest_configure(config):
    _install()


def pytest_collection_finish(session):
    if "orig" in _PATCH:  # modules imported during collection that captured the original
        STATE["rebound"] += rebind(_PATCH["orig"], _PATCH["wrapper"])


def pytest_sessionfinish(session, exitstatus):
    STATE["finished"] = True
    _flush()
