"""AI steps (Gemma 4) plus deterministic validation of everything the model says.

Provenance rule: anything produced here is tagged `ai_generated` unless it is the output
of a validation check, which is tagged `observed` with its source.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path, PurePosixPath

from packaging.version import InvalidVersion, Version

from . import prompts
from .llm import LLM, LLMError
from .metadata import package_exists
from .util import HttpCache

ACTIONS = {"upgrade", "replace", "stdlib", "remove", "declare", "keep"}


def rule_based_plan(dep) -> dict:
    """Deterministic fallback when no model is configured (tagged rule_based)."""
    ids = {i["id"] for i in dep.indicators}
    p = dep.pypi or {}
    inst_versions = sorted(set(dep.installed.values()))
    plan = {"action": "keep", "package": None, "specifier": "", "stdlib_module": None,
            "needs_code_changes": False, "api_mapping": [], "risks": [],
            "rationale": "", "migration_steps": []}
    if "unused" in ids:
        via = next((i["evidence"].get("required_by") for i in dep.indicators if i["id"] == "unused"), None)
        plan.update(action="remove",
                    rationale="Declared but never used in the repository."
                    + (f" It remains installed through {', '.join(via)}, which will pick a compatible "
                       "version once the stale pin is gone." if via else ""),
                    migration_steps=["Remove the requirement from every manifest.",
                                     "Re-run the test suite."])
    elif "undeclared" in ids and inst_versions:
        plan.update(action="declare", package=dep.name, specifier=f">={inst_versions[0]}",
                    rationale="Imported directly but only installed transitively.",
                    migration_steps=["Declare the dependency explicitly in runtime manifests."])
    elif p.get("latest_compatible") and inst_versions and inst_versions[-1] != p["latest_compatible"]:
        latest = p["latest_compatible"]
        try:
            upper = f",<{Version(latest).major + 1}"
        except InvalidVersion:
            upper = ""
        plan.update(action="upgrade", package=dep.name, specifier=f">={latest}{upper}",
                    rationale=f"Move to the latest release compatible with the target Pythons ({latest}).",
                    migration_steps=[f"Change the requirement to {dep.name}>={latest}{upper}.",
                                     "Re-run the test suite."])
    else:
        plan.update(rationale="No deterministic replacement available; needs AI or human input.")
    return plan


def _norm_symbol(s) -> str:
    return str(s or "").split("(")[0].strip().strip("`").strip()


def used_symbols(dep) -> dict[str, list[str]]:
    """Observed API surface of the dependency: symbol -> ['file:line', ...]."""
    out: dict[str, list[str]] = {}
    for u in dep.usage:
        if u["kind"] == "tooling" or u["symbol"].endswith(".*"):
            continue
        if u["kind"] == "import" and "." not in u["symbol"]:
            continue  # bare `import x` is not an API that needs mapping
        out.setdefault(u["symbol"], []).append(f"{u['file']}:{u['line']}")
    return out


def _covers(old: str, symbol: str) -> bool:
    if not old:
        return False
    return (symbol == old or symbol.startswith(old + ".") or symbol.endswith("." + old)
            or old.endswith("." + symbol.split(".", 1)[-1]))


def review_api_mapping(plan: dict, dep) -> dict:
    """Deterministic cross-check of the proposed API mapping against observed usage."""
    used = used_symbols(dep)
    modules = set(dep.modules) or {dep.name.replace("-", "_")}
    entries, mapped = [], set()
    for m in plan.get("api_mapping") or []:
        if not isinstance(m, dict):
            entries.append({"old": str(m), "new": None, "note": None, "valid_shape": False,
                            "references_dependency": False, "observed_sites": []})
            continue
        old, new = _norm_symbol(m.get("old")), str(m.get("new") or "").strip()
        hits = [s for s in used if _covers(old, s)]
        mapped |= set(hits)
        refs = bool(hits) or old.split(".")[0] in modules
        new_uses_old = any(new == mod or new.startswith(mod + ".") or f" {mod}." in f" {new}"
                           for mod in modules)
        entries.append({"old": m.get("old"), "new": m.get("new"), "note": m.get("note"),
                        "valid_shape": bool(old and new), "references_dependency": refs,
                        "new_still_uses_old": new_uses_old,
                        "observed_sites": sorted({site for s in hits for site in used[s]})[:8]})
    unmapped = [{"symbol": s, "sites": sites[:5]} for s, sites in sorted(used.items()) if s not in mapped]
    return {"provenance": "observed", "entries": entries, "unmapped_symbols": unmapped,
            "observed_symbol_count": len(used), "mapped_symbol_count": len(mapped)}


def validate_plan(plan: dict, dep, http: HttpCache, pythons: list[str],
                  stdlib_by_python: dict[str, set]) -> list[dict]:
    """Check AI claims against reality. Returns observed checks.

    Checks marked `blocking` reject the plan (rule-based fallback takes over); advisory
    checks are recorded as unresolved uncertainties.
    """
    checks = []
    action = plan.get("action")
    checks.append({"check": "action is valid", "ok": action in ACTIONS, "detail": action,
                   "source": "schema"})
    if action in {"replace", "stdlib", "upgrade", "declare"} and not (plan.get("package") or plan.get("stdlib_module")):
        checks.append({"check": f"`{action}` names a package or module", "ok": False, "source": "schema"})
    steps = plan.get("migration_steps")
    checks.append({"check": "migration steps are a non-empty list of strings",
                   "ok": isinstance(steps, list) and bool(steps) and all(isinstance(s, str) for s in steps),
                   "source": "schema", "blocking": False})
    if action in {"replace", "stdlib"}:
        review = review_api_mapping(plan, dep)
        used = review["observed_symbol_count"]
        entries = review["entries"]
        checks.append({"check": "API mapping entries have `old` and `new`",
                       "ok": bool(entries) and all(e["valid_shape"] for e in entries) if used else True,
                       "detail": f"{len(entries)} entries for {used} observed symbols", "source": "schema"})
        bad_new = [e["old"] for e in entries if e.get("new_still_uses_old")]
        checks.append({"check": "mapped replacements do not use the old dependency", "ok": not bad_new,
                       "detail": bad_new, "source": "API mapping review"})
        unknown = [e["old"] for e in entries if not e["references_dependency"]]
        checks.append({"check": "every mapped `old` symbol is used in the repository", "ok": not unknown,
                       "detail": unknown, "source": "AST usage scan", "blocking": False})
        checks.append({"check": f"API mapping covers all {used} observed symbols",
                       "ok": not review["unmapped_symbols"],
                       "detail": [u["symbol"] for u in review["unmapped_symbols"]][:20],
                       "source": "AST usage scan", "blocking": False})
    if action in {"upgrade", "replace", "declare"}:
        pkg = (plan.get("package") or "").strip()
        res = package_exists(http, pkg, plan.get("specifier") or "", pythons) if pkg else {"exists": False}
        ok = bool(res.get("exists") and res.get("specifier_valid", True)
                  and res.get("satisfying_versions", 1) > 0)
        checks.append({"check": f"package `{pkg}{plan.get('specifier') or ''}` exists on PyPI "
                                f"for Python {', '.join(pythons)}", "ok": ok,
                       "detail": res, "source": res.get("source", "pypi")})
        if action == "replace":
            checks.append({"check": "replacement differs from original", "ok": pkg.lower() != dep.name,
                           "detail": pkg, "source": "schema"})
    if action == "stdlib":
        mod = (plan.get("stdlib_module") or "").split(".")[0]
        for py in pythons:
            ok = mod in stdlib_by_python.get(py, set())
            checks.append({"check": f"`{mod}` is in the Python {py} standard library", "ok": ok,
                           "detail": mod, "source": f"sys.stdlib_module_names on Python {py}"})
    for c in checks:
        c.setdefault("blocking", True)
    return checks


def plan_accepted(checks: list[dict]) -> bool:
    return all(c["ok"] for c in checks if c.get("blocking", True))


def ai_plan(llm: LLM, dep, pythons, file_texts) -> dict:
    data = llm.chat(prompts.proposal(dep, pythons, file_texts), f"proposal_{dep.name}",
                    system=prompts.SYSTEM)
    data.setdefault("needs_code_changes", data.get("action") in {"replace", "stdlib", "upgrade"})
    for k in ("migration_steps", "api_mapping", "risks"):
        if not isinstance(data.get(k), list):
            data[k] = []
    data["specifier"] = (data.get("specifier") or "").strip()
    if data.get("package"):
        data["package"] = str(data["package"]).strip()
    return data


FILE_BLOCK_RE = re.compile(r"###\s*FILE:\s*(\S+)\s*\n```(?:python|py)?\s*\n(.*?)\n```", re.S)


def parse_file_blocks(text: str) -> dict[str, str]:
    return {m.group(1).strip("`'\" "): m.group(2) + "\n" for m in FILE_BLOCK_RE.finditer(text)}


def _top_imports(tree) -> set[str]:
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            mods.add(n.module.split(".")[0])
    return mods


def validate_edits(edits: dict[str, str], repo: Path, allowed_files=None,
                   allowed_new_imports: set[str] | None = None) -> tuple[dict[str, str], list[dict]]:
    """Accept a model-written file only if it is inside the repo, was given to the model,
    parses, and does not start importing third-party modules nobody planned for."""
    good, checks = {}, []
    root = repo.resolve()
    for path, content in edits.items():
        pp = PurePosixPath(path)
        target = (repo / path).resolve()
        if pp.is_absolute() or ".." in pp.parts or not target.is_relative_to(root):
            checks.append({"check": f"{path} stays inside repo", "ok": False, "source": "path check"})
            continue
        if allowed_files is not None and path not in allowed_files:
            checks.append({"check": f"{path} was one of the files given to the model", "ok": False,
                           "source": "path check"})
            continue
        exists = target.is_file()
        try:
            tree = ast.parse(content)
            parses, err = True, None
        except SyntaxError as exc:
            tree, parses, err = None, False, str(exc)
        checks.append({"check": f"{path} is valid Python", "ok": parses, "detail": err,
                       "source": "ast.parse"})
        checks.append({"check": f"{path} is an existing repository file", "ok": exists,
                       "source": "filesystem"})
        new_ok = True
        if parses and exists and allowed_new_imports is not None:
            try:
                before = _top_imports(ast.parse(target.read_text(encoding="utf-8")))
            except (SyntaxError, UnicodeDecodeError):
                before = set()
            added = _top_imports(tree) - before - allowed_new_imports
            new_ok = not added
            checks.append({"check": f"{path} adds no unplanned third-party imports", "ok": new_ok,
                           "detail": sorted(added), "source": "ast import scan"})
        if parses and exists and new_ok:
            good[path] = content
    return good, checks


def ai_migration(llm: LLM, dep, plan, file_texts, repo: Path, feedback=None,
                 allowed_new_imports: set[str] | None = None):
    resp = llm.chat(prompts.migration(dep, plan, file_texts, feedback),
                    f"migration_{dep.name}", system=prompts.SYSTEM, expect_json=False)
    edits = parse_file_blocks(resp["text"])
    good, checks = validate_edits(edits, repo, set(file_texts), allowed_new_imports)
    if not edits:
        checks.append({"check": "model returned at least one `### FILE:` block", "ok": False,
                       "source": "response parser"})
    return good, checks, resp["_meta"]


def ai_probes(llm: LLM, dep, plan, file_texts, usage_files) -> tuple[str | None, list[dict], dict]:
    resp = llm.chat(prompts.probes(dep, plan, file_texts, usage_files), f"probes_{dep.name}",
                    system=prompts.SYSTEM, expect_json=False)
    m = re.search(r"```(?:python|py)?\s*\n(.*?)\n```", resp["text"], re.S)
    code = (m.group(1) if m else resp["text"]).strip() + "\n"
    checks = []
    try:
        tree = ast.parse(code)
        ok = any(isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "PROBES" for t in n.targets)
                 for n in tree.body)
        checks.append({"check": "probe module parses and defines PROBES", "ok": ok, "source": "ast.parse"})
        bad_mods = {"socket", "subprocess", "shutil", "ctypes"}
        banned = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                banned += [a.name for a in n.names if a.name.split(".")[0] in bad_mods]
            elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in bad_mods:
                banned.append(n.module)
        checks.append({"check": "probe module has no socket/process/filesystem-destructive imports",
                       "ok": not banned, "source": "ast scan"})
        if not ok or banned:
            return None, checks, resp["_meta"]
    except SyntaxError as exc:
        checks.append({"check": "probe module parses", "ok": False, "detail": str(exc), "source": "ast.parse"})
        return None, checks, resp["_meta"]
    return code, checks, resp["_meta"]


def ai_review(llm: LLM, dep, plan, comparison) -> dict | None:
    try:
        return llm.chat(prompts.review(dep, plan, comparison), f"review_{dep.name}",
                        system=prompts.SYSTEM)
    except LLMError:
        return None
