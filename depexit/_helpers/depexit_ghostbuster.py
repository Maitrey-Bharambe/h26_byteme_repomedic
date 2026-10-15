"""pytest plugin (-p depexit_ghostbuster): observe imports of RETIRED dependencies.

Runs only inside the child pytest process of a migrated working copy. Config (JSON) in
$DEPEXIT_GHOST: {"roots": {"nose": "nose"}, "repo": path, "out": path}  (import root -> dist)

Wraps builtins.__import__ and importlib.import_module (import_module does not go through
__import__), records each attempt whose top-level name is a retired root (exact match:
"nose" or "nose.*", never "nose2"), then calls the original so behaviour and exceptions are
unchanged. Imports satisfied from sys.modules are seen too, because the wrappers sit in front
of the cache lookup. Nothing persists after the process exits.
"""
import builtins
import importlib
import json
import os
import sys

import pytest

CFG = json.loads(os.environ.get("DEPEXIT_GHOST") or "{}")
ROOTS = CFG.get("roots", {})                       # import root -> distribution
REPO = os.path.normcase(os.path.abspath(CFG.get("repo") or os.getcwd()))
STATE = {"installed": False, "findings": [], "error": None, "finished": False}
_SEEN = set()
_ORIG = {}


def _flush():
    out = CFG.get("out")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(STATE, fh)


def _lib_dirs():
    """Import locations that are neither the repository nor the standard library."""
    import sysconfig
    std = {os.path.normcase(os.path.abspath(sysconfig.get_paths()[k])) for k in ("stdlib", "platstdlib")}
    out = []
    for d in sys.path:
        nd = os.path.normcase(os.path.abspath(d or os.getcwd()))
        if nd != REPO and not nd.startswith(REPO + os.sep) and nd not in std and os.path.isdir(nd):
            out.append(nd)
    return sorted(out, key=len, reverse=True)


def _origin(path):
    if not path or path.startswith("<"):
        return "unknown", path or "<unknown>"
    p = os.path.normcase(os.path.abspath(path))
    if p.startswith(REPO + os.sep):
        return "repository", os.path.relpath(p, REPO).replace("\\", "/")
    for d in _lib_dirs():   # site-packages, PYTHONPATH entries, .pth locations
        if p.startswith(d + os.sep):
            return "third_party", os.path.relpath(p, d).replace("\\", "/")
    return "unknown", os.path.basename(path)


def _record(name, frame, how):
    if not isinstance(name, str) or not name:
        return
    root = name.split(".")[0]
    if root not in ROOTS:
        return
    code = frame.f_code if frame else None
    origin, rel = _origin(code.co_filename if code else None)
    if origin == "third_party" and rel.split("/")[0] == root:
        return  # the retired package importing its own submodules is not a new ghost
    key = (name, rel, frame.f_lineno if frame else 0)
    if key in _SEEN or len(_SEEN) >= 200:
        return
    _SEEN.add(key)
    STATE["findings"].append({"retired_distribution": ROOTS[root], "retired_import_root": root,
                              "attempted_import": name, "caller_file": rel,
                              "caller_line": frame.f_lineno if frame else None,
                              "origin": origin, "via": how, "test": STATE.get("test"),
                              "source": "runtime_import", "status": "observed"})
    _flush()


def _install():
    if STATE["installed"] or not ROOTS:
        return
    try:
        orig_import, orig_import_module = builtins.__import__, importlib.import_module
        _ORIG.update(builtins=orig_import, import_module=orig_import_module)

        def ghost_import(name, globals=None, locals=None, fromlist=(), level=0):
            if level == 0:
                _record(name, sys._getframe(1), "import")
            return orig_import(name, globals, locals, fromlist, level)

        def ghost_import_module(name, package=None):
            if not name.startswith("."):
                _record(name, sys._getframe(1), "importlib.import_module")
            return orig_import_module(name, package)

        builtins.__import__ = ghost_import
        importlib.import_module = ghost_import_module
        STATE["installed"] = True
        _flush()
    except Exception as exc:  # reported as error, never as "not observed"
        STATE["error"] = f"{type(exc).__name__}: {exc}"
        _flush()


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
    STATE["finished"] = True
    STATE.pop("test", None)
    _flush()
    if _ORIG:  # leave the interpreter as we found it
        builtins.__import__ = _ORIG["builtins"]
        importlib.import_module = _ORIG["import_module"]
