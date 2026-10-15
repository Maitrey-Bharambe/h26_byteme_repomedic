"""GhostBuster: is a retired dependency really gone after its exit?

Advisory, additive evidence for remove / replace / stdlib exits. It never changes a
migration verdict; it reports what is still haunting the migrated working copy:

  * tooling ghosts (observed): build / CI commands that still run the retired package
    (Makefile, tox.ini, Dockerfile, noxfile, GitHub workflows, pyproject.toml). Reuses
    usage.scan_tooling, so it agrees with the existing residual scan.
  * runtime ghosts (executed): imports of the retired package observed while the migrated
    test suite runs (plain imports, `from x import y` and importlib.import_module), via the
    _helpers/depexit_ghostbuster.py pytest plugin in a bounded child process.

Statuses: observed_ghost, not_observed, not_run, error. `not_observed` only means nothing was
seen in the executed paths and supported files; it is not proof of absence.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import sandbox, usage

ACTIONS = {"remove", "replace", "stdlib"}
# console scripts of well-known retired tools, used when the venv did not report them
KNOWN_SCRIPTS = {"nose": ["nosetests"], "pytest-runner": [], "pep8": ["pep8"], "pyflakes": ["pyflakes"]}
# rule-based replacements for retired test runners: (distribution, matched token) -> new command
SUGGEST = {("nose", "nosetests"): "python -m pytest", ("nose", "-m nose"): "-m pytest"}
EXTRA_FILES = ["pyproject.toml"]
SCOPE = {"runtime_paths": "executed_tests_only (first target Python)",
         "tooling_paths": "Makefile, makefile, GNUmakefile, Dockerfile, tox.ini, noxfile.py, setup.cfg, "
                          "Justfile, .pre-commit-config.yaml, .github/workflows/*.y*ml, pyproject.toml"}


def _is_comment(file: str, code: str) -> bool:
    s = code.lstrip()
    return s.startswith("#") or (file.endswith((".ini", ".cfg")) and s.startswith(";"))


def tooling_ghosts(root: Path, dist: str, scripts: list[str], modules: list[str]) -> list[dict]:
    """Executable build/CI references to the retired distribution (observed)."""
    scripts = sorted(set(scripts or []) | set(KNOWN_SCRIPTS.get(dist, [])))
    out = []
    for d, s in usage.scan_tooling(root, {dist: scripts}, {dist: modules}, extra_files=EXTRA_FILES):
        if d != dist or _is_comment(s.file, s.code):
            continue
        token = "-m " + s.symbol.split()[-1] if s.symbol.startswith("-m") else s.symbol
        new = SUGGEST.get((dist, token))
        out.append({"retired_distribution": dist, "source": "build_command", "provenance": "observed",
                    "file": s.file, "line": s.line, "command": s.code, "matched": s.symbol,
                    "suggested_command": s.code.replace(s.symbol, new) if new else None,
                    "suggestion_provenance": "rule_based" if new else None,
                    "status": "observed", "confidence": "high"})
    return out


def runtime_ghosts(env, repo: Path, roots: dict[str, str], work: Path, timeout: int = 900) -> dict:
    """Run the migrated suite once with the import observer (executed evidence)."""
    work.mkdir(parents=True, exist_ok=True)
    out = work / "ghost_imports.json"
    out.unlink(missing_ok=True)
    cfg = {"roots": roots, "repo": str(repo), "out": str(out)}
    tr = sandbox.run_tests(env, repo, work / "results", plugins=("depexit_ghostbuster",),
                           extra_env={"DEPEXIT_GHOST": json.dumps(cfg)}, timeout=timeout)
    res = {"provenance": "executed", "command": tr.command,
           "exit_code": tr.exit_code, "test_counts": tr.counts(), "findings": []}
    if tr.exit_code == -9:
        return {**res, "status": "error", "reason": "timed out"}
    st = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    if st is None:
        return {**res, "status": "error", "reason": "import observer did not report (pytest could not start?)"}
    if st.get("error") or not st.get("installed"):
        return {**res, "status": "error", "reason": st.get("error") or "import observer not installed"}
    res["findings"] = st["findings"]
    if not st.get("finished"):
        res["note"] = "pytest stopped before session end; findings up to that point are reported"
    res["status"] = "observed_ghost" if st["findings"] else "not_observed"
    return res


def check(dep, plan: dict, rcopy: Path, env, work: Path, scripts: list[str]) -> dict | None:
    """GhostBuster section for one exit, or None when the action does not retire anything."""
    if plan.get("action") not in ACTIONS:
        return None
    modules = list(dep.modules) or [dep.name.replace("-", "_")]
    section = {"retired_distribution": dep.name, "retired_import_roots": modules, "advisory": True,
               "verification_scope": SCOPE}
    try:
        tf = tooling_ghosts(rcopy, dep.name, scripts, modules)
        section["tooling"] = {"provenance": "observed", "status": "observed_ghost" if tf else "not_observed",
                              "findings": tf}
    except Exception as exc:
        section["tooling"] = {"provenance": "observed", "status": "error", "reason": f"{type(exc).__name__}: {exc}",
                              "findings": []}
    if env is None or not getattr(env, "ok", False):
        section["runtime"] = {"provenance": "executed", "status": "not_run", "findings": [],
                              "reason": "no Python environment to run the tests in" if env is None
                              else "migrated environment did not install"}
    else:
        try:
            section["runtime"] = runtime_ghosts(env, rcopy, {m: dep.name for m in modules}, work)
        except Exception as exc:
            section["runtime"] = {"provenance": "executed", "status": "error", "findings": [],
                                  "reason": f"{type(exc).__name__}: {exc}"}
    section["status"] = overall(section)
    section["summary"] = summarize(section)
    return section


def overall(section: dict) -> str:
    sts = [section.get("tooling", {}).get("status"), section.get("runtime", {}).get("status")]
    if "observed_ghost" in sts:
        return "observed_ghost"
    if "error" in sts:
        return "error"
    if "not_run" in sts:
        return "not_run"
    return "not_observed"


def summarize(section: dict) -> str:
    t, r = section.get("tooling", {}), section.get("runtime", {})
    parts = []
    tf = t.get("findings") or []
    parts.append(f"{len(tf)} zombie build command(s)" if tf else f"no zombie build commands ({t.get('status')})")
    rf = r.get("findings") or []
    repo_f = [f for f in rf if f.get("origin") == "repository"]
    if rf:
        parts.append(f"{len(rf)} runtime ghost import(s) ({len(repo_f)} from repository code)")
    else:
        parts.append({"not_observed": "no runtime ghost imports observed in the executed tests",
                      "not_run": "runtime check not run", "error": "runtime check failed"}.get(r.get("status"), "runtime unknown"))
    return "; ".join(parts)


def render_md(g: dict | None) -> list[str]:
    if not g:
        return []
    t, r = g.get("tooling", {}), g.get("runtime", {})
    lines = ["", "### GhostBuster: dependency exorcism (advisory; does not change the verdict)", "",
             f"Retired dependency: `{g['retired_distribution']}` · status **{g['status']}** · {g.get('summary', '')}", ""]
    tf = t.get("findings") or []
    lines.append(f"**Zombie build commands** 🔎 OBSERVED: {len(tf) if tf else 'none found'} ({t.get('status')})")
    for f in tf:
        lines.append(f"- `{f['file']}:{f['line']}` `{f['command']}`"
                     + (f" → suggested ⚙️ RULE-BASED: `{f['suggested_command']}`" if f.get("suggested_command") else
                        " (no safe replacement known; update manually)"))
    rf = r.get("findings") or []
    lines.append("")
    lines.append(f"**Runtime ghost imports** 🧪 EXECUTED: {len(rf) if rf else 'none observed'} ({r.get('status')})"
                 + (f": {r.get('reason')}" if r.get("reason") else ""))
    for f in rf:
        lines.append(f"- `{f['attempted_import']}` via {f['via']} at `{f['caller_file']}:{f['caller_line']}` "
                     f"({f['origin']}{', test ' + f['test'] if f.get('test') else ''})")
    lines += ["", "Coverage limitation: runtime evidence covers only code paths executed by the tests; "
                  "`not_observed` is not proof that no hidden import exists."]
    return lines
