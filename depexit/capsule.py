"""Dependency Exit Capsules: reusable, evidence-carrying migration packages.

Every section declares its provenance:
  observed      - read from the repository or public metadata, with the source
  executed      - produced by actually running something, with command + log
  ai_generated  - written by Gemma 4 (model + prompt hash + full call log recorded)
  rule_based    - deterministic fallback logic of this tool
"""
from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path

from . import sandbox
from .risk import SEVERITY
from .replay import summary as replay_summary
from .util import write_json

VISIBILITY = {"watched": "👁 watched", "partly_blind": "◐ partly blind", "blind": "🙈 blind",
              "not_exercised": "∅ not exercised", "not_run": "⚠ not run", "not_applicable": "– n/a"}

SCHEMA = "dependency-exit-capsule/1.0"
BADGE = {"observed": "🔎 OBSERVED", "executed": "🧪 EXECUTED", "ai_generated": "🤖 AI-GENERATED (Gemma 4)",
         "rule_based": "⚙️ RULE-BASED"}
STATUS_TEXT = {
    "verified": "✅ Verified: every verification requirement was executed and met",
    "verified_with_gaps": "🟡 Verified with gaps: all required checks met; advisory checks failed or were not run",
    "incomplete": "⚪ Incomplete: nothing failed, but at least one required check could not be performed",
    "verified_by_replay": "🔵 Verified by replay: tests are blind to it, but real recorded calls replay identically",
    "weakly_verified": "🟤 Weakly verified: tests pass but cannot see this dependency, and no real call was replayed",
    "failed": "❌ Failed verification: a required check failed (see requirements and gaps)",
    "planned": "📝 Planned: proposal ready for review, nothing executed yet",
    "partial": "🟠 Partial: some verified migrations could not be merged",
    "not_executed": "⏸️ Not executed: no safe replacement proposed",
    "tool_error": "⚠️ Tool error during this capsule",
}
RESULT_ICON = {"met": "✅", "failed": "❌", "not_checked": "⚪", "pending": "⏳"}
INFO_ONLY = "ℹ️ Informational only"


def _test_imports(repo: Path) -> set[str]:
    mods: set[str] = set()
    for p in repo.rglob("test*.py"):
        if any(x in p.parts for x in (".git", ".venv", "venv", "site-packages")):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
                mods.add(n.module)
                mods |= {f"{n.module}.{a.name}" for a in n.names}
    return mods


def compatibility_gaps(dep, res, modules) -> list[dict]:
    gaps = []
    for py, c in (res.get("comparison") or {}).items():
        if not c.get("install_ok", True):
            gaps.append({"provenance": "executed", "python": py, "kind": "install-failed",
                         "detail": (c.get("install_error") or "")[-600:]})
            continue
        t = c["tests"]
        if t["replacement_collection_error"] and not t["original_collection_error"]:
            gaps.append({"provenance": "executed", "python": py, "kind": "test-suite-broken",
                         "detail": t["replacement_collection_error"].splitlines()[-1][:400]})
        for r in t["regressed"]:
            last = [l for l in r["message"].splitlines() if l.strip()]
            gaps.append({"provenance": "executed", "python": py, "kind": "test-regression",
                         "detail": f"{r['test']}: {last[-1][:300] if last else ''}"})
        for nid in t.get("vanished_passing") or []:
            gaps.append({"provenance": "executed", "python": py, "kind": "test-not-run",
                         "detail": f"{nid} passed on the baseline but did not run after migration"})
        for nid in t["still_failing"]:
            orig_msg = (res.get("_baseline_msgs") or {}).get(py, {}).get(nid, "")
            if modules is not None and dep.name not in sandbox.attribute_text(orig_msg, modules):
                continue
            gaps.append({"provenance": "executed", "python": py, "kind": "still-failing",
                         "detail": f"{nid} fails before and after (not fixed by this change)"})
        for d in (c.get("probes") or {}).get("different", []):
            gaps.append({"provenance": "executed", "python": py, "kind": "behaviour-difference",
                         "detail": f"probe `{d['probe']}`: original={_short(d['original'])} "
                                   f"replacement={_short(d['replacement'])}"})
        for w in (c.get("warnings") or {}).get("new", [])[:5]:
            gaps.append({"provenance": "executed", "python": py, "kind": "new-warning",
                         "detail": f"{w['category']}: {w['message'][:200]}"})
        inst = c.get("installed") or {}
        if inst.get("original_still_installed"):
            gaps.append({"provenance": "executed", "python": py, "kind": "still-installed-transitively",
                         "detail": f"{dep.name} is still installed by another package after removal"})
    for r in res.get("residual_usage_list") or res.get("residual") or []:
        gaps.append({"provenance": "observed", "python": "all", "kind":
                     "leftover-tooling-reference" if r["kind"] == "tooling" else "leftover-code-reference",
                     "detail": f"{r['file']}:{r['line']} still references {dep.name}: `{r['code'][:120]}`"
                               + (" (update manually; not Python code)" if r["kind"] == "tooling" else "")})
    # merge identical gaps across pythons
    merged = {}
    for g in gaps:
        key = (g["kind"], g["detail"])
        if key in merged:
            merged[key]["pythons"].append(g["python"])
        else:
            merged[key] = {**{k: v for k, v in g.items() if k != "python"}, "pythons": [g["python"]]}
    return list(merged.values())


def _short(r):
    if not isinstance(r, dict):
        return repr(r)[:120]
    if r.get("ok"):
        return json.dumps(r.get("value"), default=str)[:140]
    return f"{r.get('error_type', 'error')}({(r.get('error') or '')[:100]})"


def open_concerns(dep, res, repo: Path, llm_on: bool) -> list[dict]:
    out = []
    tested = _test_imports(repo)
    for f in sorted({u["file"] for u in dep.usage if u["file"].endswith(".py")}):
        if f.split("/")[-1].startswith(("test", "conftest")):
            continue
        mod = f[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[:-9]
        if not any(t == mod or t.startswith(mod + ".") for t in tested):
            out.append({"provenance": "observed",
                        "text": f"`{f}` uses {dep.name} but no test imports `{mod}` directly; "
                                "only indirect coverage" + (" and probes" if res.get("probe_code") else "")})
    if not res.get("probe_code"):
        out.append({"provenance": "observed",
                    "text": "No behavioural probes were executed"
                            + ("" if llm_on else " (no model configured)")
                            + "; only the existing test suite was compared."})
    priv = [u for u in dep.usage if u["private"]]
    if priv:
        out.append({"provenance": "observed",
                    "text": f"Private API `{priv[0]['symbol']}` remains a future-break risk "
                            f"({priv[0]['file']}:{priv[0]['line']})."})
    out += res.get("uncertainties") or []
    for c in (res.get("review") or {}).get("open_concerns", [])[:6]:
        out.append({"provenance": "ai_generated", "text": str(c)})
    for r in (res.get("plan") or {}).get("risks", [])[:6]:
        out.append({"provenance": res["plan"].get("provenance", "ai_generated"), "text": str(r)})
    return out


def _execution_section(res, out_root: Path) -> dict:
    ex = {}
    for py, c in (res.get("comparison") or {}).items():
        e = {"install_ok": c.get("install_ok"), "install_commands": c.get("install_cmds"),
             "installed": c.get("installed"), "tests": None}
        if "tests" in c:
            e["tests"] = {k: c["tests"][k] for k in ("original", "replacement", "fixed", "regressed",
                                                     "still_failing", "original_collection_error",
                                                     "replacement_collection_error")}
        e["warnings"] = c.get("warnings")
        e["replacement_import"] = c.get("replacement_import")
        p = c.get("probes")
        if p:
            e["probes"] = {k: p.get(k) for k in ("executed", "total", "identical", "different",
                                                  "infrastructure_error")}
            e["probe_results"] = {"original": p.get("original_results"),
                                  "replacement": p.get("replacement_results")}
        e["log"] = c.get("test_log")
        ex[py] = e
    return ex


def build_capsule(dep, res, run_meta, out_root: Path) -> dict:
    plan = res.get("plan") or {}
    prov = plan.get("provenance", "rule_based")
    files = sorted({u["file"] for u in dep.usage})
    ver = res.get("verification")
    if ver:
        verif = {"provenance": "executed", "status": ver["status"], "requirements": ver["requirements"]}
    else:  # planned or not executed: requirements are known, results are not
        verif = {"provenance": "rule_based", "status": None,
                 "requirements": [{**r, "result": "pending", "evidence": []}
                                  for r in res.get("verification_requirements") or []]}
    return {
        "schema": SCHEMA,
        "tool": run_meta["tool"],
        "generated_at": run_meta["generated_at"],
        "target": run_meta["target"],
        "ai": run_meta["ai"],
        "execution_boundary": run_meta.get("execution_boundary"),
        "status": res.get("status"),
        "status_reason": res.get("status_reason") or res.get("error"),
        "dependency": {
            "provenance": "observed",
            "name": dep.name,
            "import_names": dep.modules,
            "declared": dep.declared,
            "installed_versions": dep.installed,
            "pypi": {k: dep.pypi.get(k) for k in ("source", "summary", "latest_version",
                                                   "latest_compatible", "latest_release_date",
                                                   "days_since_latest_release", "release_count_last_2y",
                                                   "classifiers_inactive")},
            "installed_release": (dep.pypi or {}).get("installed"),
            "github": dep.github,
        },
        "risk_indicators": {"provenance": "observed+executed", "score": dep.score,
                            "max_severity": dep.max_severity, "items": dep.indicators},
        "usage_locations": {"provenance": "observed", "files": files, "count": len(dep.usage),
                            "sites": dep.usage},
        "proposed_replacement": {
            "provenance": prov,
            "action": plan.get("action"), "package": plan.get("package"),
            "specifier": plan.get("specifier"), "stdlib_module": plan.get("stdlib_module"),
            "rationale": plan.get("rationale"), "api_mapping": plan.get("api_mapping", []),
            "api_mapping_review": res.get("api_mapping_review"),
            "needs_code_changes": plan.get("needs_code_changes"),
            "model_calls": res.get("llm_calls", []),
            "rejected_ai_plan": res.get("rejected_ai_plan"),
        },
        "migration_steps": {"provenance": prov, "steps": plan.get("migration_steps", []),
                            "manifest_files_changed": res.get("manifest_files", []),
                            "code_files_changed": sorted((res.get("edits") or {}).keys()),
                            "code_changes_provenance": "ai_generated" if res.get("edits") else None,
                            "patch": "migration.patch" if res.get("patch") else None},
        "validation_of_ai_output": {"provenance": "observed", "checks": res.get("validation", [])},
        "verification": verif,
        "test_blindness": res.get("test_blindness"),
        "replay": res.get("replay"),
        "suggested_tests": {
            "existing_suite": {"provenance": "observed", "runner": "pytest",
                               "command": "python -m pytest -q"},
            "behaviour_probes": {"provenance": "ai_generated", "file": "probes.py"} if res.get("probe_code") else None,
        },
        "executed_test_results": {"provenance": "executed", "per_python": _execution_section(res, out_root),
                                  "attempts": res.get("attempts", [])},
        "compatibility_gaps": compatibility_gaps(dep, res, run_meta.get("_modules")),
        "ai_review": {"provenance": "ai_generated", **res["review"]} if res.get("review") else None,
        "open_concerns_and_limitations": open_concerns(dep, res, res.get("_repo_root"), run_meta["ai"].get("reachable")),
        "reuse": {"apply": "git apply migration.patch", "replay": "./replay.sh <path-to-repo> [python]",
                  "schema": SCHEMA},
    }


def _md_list(items, fmt):
    return "\n".join(fmt(i) for i in items) if items else "_none_"


def render_md(c: dict) -> str:
    d, r, u, p = c["dependency"], c["risk_indicators"], c["usage_locations"], c["proposed_replacement"]
    prov = p["provenance"]
    lines = [f"# Dependency Exit Capsule: `{d['name']}`", "",
             f"**Status:** {STATUS_TEXT.get(c['status'], c['status'])}  ",
             *( [f"**Why:** {c['status_reason']}  "] if c.get("status_reason") else []),
             f"**Target:** {c['target']['source']} @ `{(c['target'].get('commit') or 'n/a')[:10]}` · "
             f"Python {', '.join(c['target']['pythons'])}  ",
             f"**AI:** " + (f"{c['ai'].get('model')} via {c['ai'].get('base_url')}" if c["ai"].get("reachable")
                            else "not used (rule-based fallback)"), "",
             "> Provenance legend: 🔎 observed in repo/metadata · 🧪 executed by this tool · "
             "🤖 written by Gemma 4 · ⚙️ deterministic rule", "",
             f"## 1. Dependency details {BADGE['observed']}", "",
             f"- Installed: {', '.join(f'py{k}: `{v}`' for k, v in d['installed_versions'].items()) or 'not installed'}",
             f"- Latest on PyPI: `{d['pypi'].get('latest_version')}` ({d['pypi'].get('latest_release_date')}), "
             f"latest compatible with targets: `{d['pypi'].get('latest_compatible')}`",
             f"- Declared in: " + (", ".join(f"`{x['file']}:{x['line']}` `{x['raw']}`"
                                             + (" (commented)" if x['commented'] else "") for x in d["declared"])
                                   or "**not declared**"),
             f"- Import names: {', '.join(f'`{m}`' for m in d['import_names']) or '_none_'}", "",
             f"## 2. Risk indicators {BADGE['observed']} / {BADGE['executed']}", "",
             f"Score **{r['score']}**, max severity **{r['max_severity']}**", "",
             "| Severity | Indicator | Detail | Evidence |", "|---|---|---|---|"]
    for i in r["items"]:
        ev = i["evidence"]
        lines.append(f"| {i['severity']} | {i['title']} | {i['detail'][:220].replace('|', '/')} | "
                     f"{'🧪' if ev['type'] == 'executed' else '🔎'} {str(ev.get('source'))[:80]} |")
    lines += ["", f"## 3. Usage locations {BADGE['observed']}", "",
              f"{u['count']} site(s) in {len(u['files'])} file(s)", "",
              "| Location | Kind | Symbol | Code |", "|---|---|---|---|"]
    for s in u["sites"][:60]:
        flag = " ⚠️private" if s["private"] else ""
        lines.append(f"| `{s['file']}:{s['line']}` | {s['kind']}{flag} | `{s['symbol']}` | "
                     f"`{s['code'][:90].replace('|', '/')}` |")
    lines += ["", f"## 4. Proposed replacement {BADGE[prov]}", "",
              f"**Action:** `{p['action']}`"
              + (f" → `{p['package']}{p['specifier'] or ''}`" if p.get("package") else "")
              + (f" → stdlib `{p['stdlib_module']}`" if p.get("stdlib_module") else ""), "",
              p.get("rationale") or ""]
    if p.get("api_mapping"):
        lines += ["", "| Old | New | Note |", "|---|---|---|"]
        lines += [f"| `{m.get('old')}` | `{m.get('new')}` | {m.get('note', '')} |" for m in p["api_mapping"]]
    if p.get("rejected_ai_plan"):
        lines += ["", f"> ⚠️ Gemma's first proposal was **rejected by validation** "
                      f"(`{p['rejected_ai_plan'].get('action')} {p['rejected_ai_plan'].get('package')}`); "
                      "rule-based fallback used."]
    m = c["migration_steps"]
    lines += ["", f"## 5. Migration steps {BADGE[m['provenance']]}", "",
              _md_list(m["steps"], lambda s: f"1. {s}"), "",
              f"Files changed: manifests {m['manifest_files_changed'] or '–'}, code {m['code_files_changed'] or '–'}"
              + (f" (code by {BADGE['ai_generated']})" if m["code_files_changed"] else "")
              + (". Full diff: [`migration.patch`](migration.patch)" if m.get("patch") else ""), ""]
    v = c["validation_of_ai_output"]["checks"]
    lines += [f"### Validation of proposal and AI output {BADGE['observed']}", "",
              _md_list(v, lambda x: f"- {'✅' if x['ok'] else '❌'} {x['check']}"), ""]
    st = c["suggested_tests"]
    lines += [f"## 6. Suggested tests", "",
              f"- {BADGE['observed']} Existing suite: `{st['existing_suite']['command']}`"]
    if st.get("behaviour_probes"):
        lines.append(f"- {BADGE['ai_generated']} Behaviour probes: [`probes.py`](probes.py), run on "
                     "original **and** replacement, results diffed")
    lines += ["", f"## 7. Executed test results {BADGE['executed']}", "",
              "| Python | Install | Original (pass/fail/err) | Replacement (pass/fail/err) | Fixed | Regressed | Probes identical |",
              "|---|---|---|---|---|---|---|"]
    for py, e in c["executed_test_results"]["per_python"].items():
        t = e.get("tests") or {}
        o, n = t.get("original") or {}, t.get("replacement") or {}
        pr = e.get("probes") or {}
        lines.append(
            f"| {py} | {'✅' if e['install_ok'] else '❌'} | {o.get('passed', '-')}/{o.get('failed', '-')}/{o.get('error', '-')}"
            f" | {n.get('passed', '-')}/{n.get('failed', '-')}/{n.get('error', '-')}"
            + (" ⚠️collection error" if t.get("replacement_collection_error") and not t.get("original_collection_error") else "")
            + f" | {len(t.get('fixed', []))} | {len(t.get('regressed', []))} | "
            + (f"{pr.get('identical')}/{pr.get('total')}" if pr.get("executed") else "–") + " |")
    att = c["executed_test_results"]["attempts"]
    if len(att) > 1:
        lines += ["", f"Fix loop: {len(att)} attempts; Gemma received the executed failures as feedback after each failed attempt."]
    tb = c.get("test_blindness")
    if tb:
        lines += ["", f"### Test visibility {BADGE['executed']}", "",
                  f"**{VISIBILITY.get(tb['classification'], tb['classification'])}**: {tb.get('reason', '')} "
                  f"(original suite, Python {tb.get('python')})", ""]
        if tb.get("runs"):
            lines += ["| Symbol | Sabotage | Calls | Outcome | Caught by |", "|---|---|---|---|---|"]
            for r in tb["runs"]:
                lines.append(f"| `{r['symbol']}` | {r['mode']} | {r.get('calls', '–')} | {r['outcome']} | "
                             f"{', '.join(r.get('catching_tests', [])[:3]) or r.get('reason', '–')} |")
    rp = c.get("replay")
    if rp:
        cn = rp.get("counts") or {}
        lines += ["", f"### Replay of real recorded calls {BADGE['executed']} (recorded data)", "",
                  f"State: **{rp.get('state')}**" + (f" ({rp.get('reason')})" if rp.get("reason") else "")
                  + f" · identical {cn.get('identical', 0)} · different {cn.get('different', 0)}"
                  f" · exception changed {cn.get('exception_changed', 0)} · not replayable {cn.get('not_replayable', 0)}", ""]
        if rp.get("differences"):
            lines += ["| Function | Input | Original | Replacement | Source test |", "|---|---|---|---|---|"]
            for d in rp["differences"][:20]:
                lines.append(f"| `{d['function']}` | `{d['input'][:60].replace('|', '/')}` | "
                             f"`{str(d['original'])[:60].replace('|', '/')}` | `{str(d['replacement'])[:60].replace('|', '/')}` | "
                             f"{d.get('test') or '–'} |")
        for sk in rp.get("skipped", []):
            lines.append(f"- skipped `{sk['function']}`: {sk['reason']}")
        if rp.get("ai_generated_inputs"):
            ag = rp["ai_generated_inputs"]
            lines.append(f"- {BADGE['ai_generated']} extra inputs for {', '.join(ag['functions'])}: {ag['note']}")
    vr = c.get("verification") or {}
    if vr.get("requirements"):
        lines += ["", f"### Verification requirements {BADGE.get(vr['provenance'], '')}", "",
                  "| Result | Required | Requirement | Evidence |", "|---|---|---|---|"]
        for r in vr["requirements"]:
            ev = "; ".join(str(e) for e in (r.get("evidence") or []))[:220].replace("|", "/")
            lines.append(f"| {RESULT_ICON.get(r['result'], r['result'])} {r['result']} | "
                         f"{'yes' if r['required'] else 'advisory'} | {r['description']} | {ev or '–'} |")
    lines += ["", f"## 8. Compatibility gaps {BADGE['executed']}", "",
              _md_list(c["compatibility_gaps"],
                       lambda g: f"- **{g['kind']}** (py {', '.join(g['pythons'])}): {g['detail']}")]
    if c.get("ai_review"):
        rv = c["ai_review"]
        lines += ["", f"### Interpretation of gaps {BADGE['ai_generated']}", "", rv.get("summary", ""), ""]
        lines += [f"- *{g.get('gap')}*: {g.get('likely_cause')} → {g.get('suggested_fix')}"
                  for g in rv.get("gap_explanations", [])]
        lines.append(f"\nModel confidence: {rv.get('confidence', 'n/a')}")
    lines += ["", "## 9. Open concerns and limitations", "",
              _md_list(c["open_concerns_and_limitations"],
                       lambda x: f"- {BADGE.get(x['provenance'], x['provenance'])}: {x['text']}"),
              "", "## Reuse", "", "```bash", "git apply migration.patch       # apply the migration",
              "./replay.sh /path/to/repo 3.12  # re-run the verification from scratch", "```", ""]
    return "\n".join(lines)


REPLAY = """#!/usr/bin/env bash
# Re-run this capsule's verification from scratch. Usage: ./replay.sh <path-to-target-repo> [python]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${1:?path to the target repository}"; PY="${2:-%(py)s}"
WORK="$(mktemp -d)"; cp -r "$REPO" "$WORK/repo"; cd "$WORK/repo"
for side in original replacement; do
  if [ "$side" = replacement ] && [ -s "$HERE/migration.patch" ]; then
    git apply "$HERE/migration.patch" 2>/dev/null || patch -p1 < "$HERE/migration.patch"
  fi
  uv venv --quiet --python "$PY" "$WORK/venv-$side"
  uv pip install --quiet --python "$WORK/venv-$side/bin/python" %(install)s pytest || { echo "[$side] install FAILED"; continue; }
  echo "== $side: pytest =="
  PYTHONPATH=. "$WORK/venv-$side/bin/python" -m pytest -q -p no:cacheprovider || true
  if [ -f "$HERE/probes.py" ]; then
    PYTHONPATH=. "$WORK/venv-$side/bin/python" "$HERE/probe_runner.py" "$HERE/probes.py" "$WORK/probes-$side.json"
  fi
done
if [ -f "$WORK/probes-original.json" ] && [ -f "$WORK/probes-replacement.json" ]; then
  diff <(python3 -m json.tool --sort-keys "$WORK/probes-original.json") \\
       <(python3 -m json.tool --sort-keys "$WORK/probes-replacement.json") && echo "probes identical"
fi
echo "work dir: $WORK"
"""


def write_capsule(dep, res, run_meta, out_root: Path, repo_root: Path) -> dict:
    cdir = out_root / "capsules" / dep.name
    if cdir.exists():
        shutil.rmtree(cdir)
    (cdir / "logs").mkdir(parents=True)
    res["_repo_root"] = repo_root
    cap = build_capsule(dep, res, run_meta, out_root)
    res.pop("_repo_root", None)
    write_json(cdir / "capsule.json", cap)
    (cdir / "CAPSULE.md").write_text(render_md(cap), encoding="utf-8")
    if res.get("patch"):
        (cdir / "migration.patch").write_text(res["patch"], encoding="utf-8")
    if res.get("probe_code"):
        (cdir / "probes.py").write_text(res["probe_code"], encoding="utf-8")
        shutil.copy(sandbox.HELPERS / "probe_runner.py", cdir / "probe_runner.py")
    for py, c in (res.get("comparison") or {}).items():
        if c.get("test_log") and (out_root / c["test_log"]).exists():
            shutil.copy(out_root / c["test_log"], cdir / "logs" / f"replacement-py{py}.log")
        if c.get("install_error"):
            (cdir / "logs" / f"install-py{py}.log").write_text(c["install_error"], encoding="utf-8")
    for py in run_meta["target"]["pythons"]:
        src = out_root / ".work" / f"baseline-py{py}" / "results" / "pytest.log"
        if src.exists():
            shutil.copy(src, cdir / "logs" / f"original-py{py}.log")
    install = " ".join(f"'{a}'" for args in sandbox.install_plan(repo_root)[:1] for a in args)
    rp = cdir / "replay.sh"
    rp.write_text(REPLAY % {"py": run_meta["target"]["pythons"][0], "install": install}, encoding="utf-8")
    rp.chmod(0o755)
    return cap


def _target(p: dict) -> str | None:
    if p.get("package"):
        return f"{p['package']}{p.get('specifier') or ''}"
    if p.get("stdlib_module"):
        return f"stdlib {p['stdlib_module']}"
    return None


def write_all(out: Path, run_meta, ranked, selected, results, combined) -> None:
    repo_root = Path(run_meta.get("_repo") or ".")
    caps = {}
    for dep in selected:
        res = results.get(dep.name) or {"status": "tool_error"}
        caps[dep.name] = write_capsule(dep, res, run_meta, out, repo_root)
    sel = {d.name for d in selected}
    index = {
        "schema": "dependency-exit-index/1.0", **{k: v for k, v in run_meta.items() if not k.startswith("_")},
        "dependencies": [{"name": d.name, "at_risk": d.name in sel, "score": d.score,
                          "max_severity": d.max_severity,
                          "indicators": [i["id"] for i in d.indicators],
                          "status": caps.get(d.name, {}).get("status"),
                          "status_reason": caps.get(d.name, {}).get("status_reason"),
                          "test_visibility": (caps.get(d.name, {}).get("test_blindness") or {}).get("classification"),
                          "test_visibility_reason": (caps.get(d.name, {}).get("test_blindness") or {}).get("reason"),
                          "catching_tests": (caps.get(d.name, {}).get("test_blindness") or {}).get("catching_tests", []),
                          "replay": replay_summary(caps.get(d.name, {}).get("replay")) if d.name in sel else None,
                          "action": (caps.get(d.name, {}).get("proposed_replacement") or {}).get("action"),
                          "action_target": _target((caps.get(d.name, {}).get("proposed_replacement") or {})),
                          "capsule": f"capsules/{d.name}/CAPSULE.md" if d.name in sel else None}
                         for d in ranked],
    }
    if combined:
        cdir = out / "combined"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "all-migrations.patch").write_text(combined["patch"], encoding="utf-8")
        index["combined"] = {k: v for k, v in combined.items() if k not in ("patch", "repo")}
    write_json(out / "index.json", index)
    (out / "REPORT.md").write_text(render_index(index), encoding="utf-8")


def render_index(ix: dict) -> str:
    t = ix["target"]
    lines = ["# Dependency Exit Report", "",
             f"Target: {t['source']} @ `{(t.get('commit') or 'n/a')[:10]}` · Python {', '.join(t['pythons'])} · "
             f"AI: {ix['ai'].get('model') if ix['ai'].get('reachable') else 'not used'} · {ix['seconds']}s", "",
             f"> Execution boundary: {(ix.get('execution_boundary') or {}).get('detail', 'n/a')}", "",
             f"## Baseline {BADGE['executed']}", "", "| Python | Install | Passed | Failed | Errors | Collection error |",
             "|---|---|---|---|---|---|"]
    for py, b in ix["baseline"].items():
        c = b["tests"]["counts"]
        ce = b["tests"]["collection_error"]
        lines.append(f"| {py} | {'✅' if b['env']['ok'] else '❌'} | {c['passed']} | {c['failed']} | {c['error']} | "
                     f"{(ce.splitlines()[-1][:80] if ce else '–')} |")
    lines += ["", "## Dependencies", "",
              "| Dependency | Risk | Indicators | Action | Test visibility | Replay | Status | Why | Capsule |",
              "|---|---|---|---|---|---|---|---|---|"]
    for d in ix["dependencies"]:
        rp = d.get("replay") or {}
        rtxt = ("–" if not rp or rp.get("state") in (None, "not_applicable") else
                f"{rp['identical']}✓ {rp['different']}✗ {rp['not_replayable']}?" if rp.get("state") == "compared"
                else rp.get("state"))
        lines.append(f"| `{d['name']}` | {d['max_severity']} ({d['score']}) | {', '.join(d['indicators']) or '–'} | "
                     f"{d['action'] or '–'} | {VISIBILITY.get(d.get('test_visibility'), '–')} | {rtxt} | "
                     f"{STATUS_TEXT.get(d['status'], '–').split(':')[0] if d['status'] else 'healthy'} | "
                     f"{(d.get('status_reason') or '').replace('|', '/')[:120]} | "
                     + (f"[open]({d['capsule']})" if d["capsule"] else "–") + " |")
    if ix.get("combined"):
        cb = ix["combined"]
        lines += ["", f"## All verified exits applied together {BADGE['executed']}", "",
                  f"**Combined status:** {STATUS_TEXT.get(cb.get('status'), cb.get('status'))}  ",
                  f"Dependencies: {', '.join(cb['dependencies'])}  ",
                  f"Not combined (not verified individually): "
                  f"{', '.join(e['dependency'] + ' (' + str(e['status']) + ')' for e in cb.get('excluded_unverified', [])) or 'none'}  ",
                  f"Merge conflicts: {cb['conflicts'] or 'none'}  ",
                  f"Re-applied by Gemma on the combined tree: "
                  f"{', '.join(r['dependency'] for r in cb.get('rebased_by_ai', [])) or 'none'}  ",
                  f"Excluded: {', '.join(s['dependency'] + ' (' + s['reason'] + ')' for s in cb.get('skipped', [])) or 'none'}  ",
                  "Patch: [`combined/all-migrations.patch`](combined/all-migrations.patch)", "",
                  "| Python | Passed | Failed | Errors | Fixed vs baseline | Regressed |", "|---|---|---|---|---|---|"]
        for py, c in cb["comparison"].items():
            if not c.get("install_ok"):
                lines.append(f"| {py} | install failed | | | | |")
                continue
            n = c["tests"]["replacement"]
            lines.append(f"| {py} | {n['passed']} | {n['failed']} | {n['error']} | {len(c['tests']['fixed'])} | "
                         f"{len(c['tests']['regressed'])} |")
    if ix.get("manifest_drift"):
        lines += ["", f"## Manifest drift {BADGE['observed']}", ""]
        for name, dr in ix["manifest_drift"].items():
            lines.append(f"- `{name}`{' **(incompatible)**' if dr['conflict'] else ''}: "
                         + "; ".join(f"`{x['file']}:{x['line']}` `{x['spec']}`" for x in dr["declarations"]))
    return "\n".join(lines) + "\n"
