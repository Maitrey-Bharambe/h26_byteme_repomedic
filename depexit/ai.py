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


def validate_plan(plan: dict, dep, http: HttpCache, pythons: list[str],
                  stdlib_by_python: dict[str, set]) -> list[dict]:
    """Check AI claims against reality. Returns observed checks."""
    checks = []
    action = plan.get("action")
    checks.append({"check": "action is valid", "ok": action in ACTIONS, "detail": action,
                   "source": "schema"})
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
    return checks


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


def validate_edits(edits: dict[str, str], repo: Path) -> tuple[dict[str, str], list[dict]]:
    good, checks = {}, []
    for path, content in edits.items():
        pp = PurePosixPath(path)
        if pp.is_absolute() or ".." in pp.parts:
            checks.append({"check": f"{path} stays inside repo", "ok": False, "source": "path check"})
            continue
        exists = (repo / path).is_file()
        try:
            ast.parse(content)
            parses = True
            err = None
        except SyntaxError as exc:
            parses, err = False, str(exc)
        checks.append({"check": f"{path} is valid Python", "ok": parses, "detail": err,
                       "source": "ast.parse"})
        checks.append({"check": f"{path} is an existing repository file", "ok": exists,
                       "source": "filesystem"})
        if parses and exists:
            good[path] = content
    return good, checks


def ai_migration(llm: LLM, dep, plan, file_texts, repo: Path, feedback=None):
    resp = llm.chat(prompts.migration(dep, plan, file_texts, feedback),
                    f"migration_{dep.name}", system=prompts.SYSTEM, expect_json=False)
    edits = parse_file_blocks(resp["text"])
    good, checks = validate_edits(edits, repo)
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
