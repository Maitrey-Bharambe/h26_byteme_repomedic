"""Turn observed + executed facts into risk indicators per dependency.

No AI here: every indicator is derived from repository files, public metadata or
an execution log, and carries the evidence it came from.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .sandbox import attribute_text

SEVERITY = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
STALE_DAYS = 3 * 365


@dataclass
class DepFacts:
    name: str
    declared: list = field(default_factory=list)        # manifests.Declared dicts
    modules: list = field(default_factory=list)         # import names
    installed: dict = field(default_factory=dict)       # python -> version
    usage: list = field(default_factory=list)           # UsageSite dicts
    pypi: dict = field(default_factory=dict)
    github: dict | None = None
    indicators: list = field(default_factory=list)

    @property
    def score(self) -> int:
        return sum(2 ** SEVERITY[i["severity"]] for i in self.indicators)

    @property
    def max_severity(self) -> str:
        if not self.indicators:
            return "none"
        return max(self.indicators, key=lambda i: SEVERITY[i["severity"]])["severity"]

    @property
    def at_risk(self) -> bool:
        return any(SEVERITY[i["severity"]] >= SEVERITY["medium"] for i in self.indicators)


def ind(id_, severity, title, detail, ev_type, source, **extra):
    return {"id": id_, "severity": severity, "title": title, "detail": detail,
            "evidence": {"type": ev_type, "source": source, **extra}}


def attribute_warning(w: dict, modules: dict, usage_by_loc: dict) -> list[str]:
    dists = attribute_text(w.get("filename", ""), modules)
    if dists:
        return dists
    loc = (w.get("filename", ""), w.get("lineno"))
    for (f, line), d in usage_by_loc.items():
        if loc[1] == line and loc[0].replace("\\", "/").endswith(f):
            return [d]
    msg = w.get("message", "")
    for mod, ds in modules.items():
        if len(mod) > 3 and re.search(rf"\b{re.escape(mod)}\b", msg):
            return ds
    return []


def build_indicators(dep: DepFacts, *, drift: dict, baselines: dict, all_modules: dict,
                     usage_by_loc: dict, imported_dists: set[str],
                     required_by: dict | None = None) -> None:
    required_by = required_by or {}
    I = dep.indicators
    runtime_decls = [d for d in dep.declared if not d["commented"]]
    commented = [d for d in dep.declared if d["commented"]]
    if commented and not runtime_decls and not dep.usage and not dep.installed:
        c = commented[0]
        vulns = ((dep.pypi or {}).get("installed") or {}).get("vulnerabilities") or []
        note = f"; {len(vulns)} advisories for that version do not apply" if vulns else ""
        I.append(ind("commented-requirement", "info", "Commented-out requirement ignored",
                     f"{c['file']}:{c['line']} `{c['raw']}` is commented out, not installed and "
                     f"not imported{note}", "observed", c["file"]))
        return

    # --- declaration problems (observed) ---------------------------------
    if dep.name in drift:
        dr = drift[dep.name]
        where = "; ".join(f"{x['file']}:{x['line']} `{x['spec']}`" for x in dr["declarations"])
        I.append(ind("manifest-drift", "high" if dr["conflict"] else "medium",
                     "Manifests disagree" + (" (incompatible)" if dr["conflict"] else ""),
                     f"Declared differently across manifests: {where}", "observed",
                     "repository manifests"))
    if not runtime_decls and dep.usage and any(u["kind"] != "tooling" for u in dep.usage):
        first = next(u for u in dep.usage if u["kind"] != "tooling")
        I.append(ind("undeclared", "medium", "Imported but not declared",
                     f"`{first['code']}` at {first['file']}:{first['line']} works only because "
                     "another package installs it transitively", "observed", "AST import scan"))
    if runtime_decls and not dep.usage:
        via = sorted(required_by.get(dep.name, []))
        I.append(ind("unused", "medium", "Declared but never used directly",
                     "No import, attribute access or tooling reference found in the repository"
                     + (f"; it is installed anyway as a dependency of {', '.join(via)}, so the "
                        "explicit pin only constrains the resolver" if via else ""),
                     "observed", "AST + build-file scan + installed metadata", required_by=via))
    for d in runtime_decls:
        if d["group"] == "runtime" and dep.name in {"setuptools", "pip", "wheel"}:
            I.append(ind("build-tool-at-runtime", "medium", "Build tool pinned as runtime dependency",
                         f"{d['file']}:{d['line']} `{d['raw']}`", "observed", d["file"]))
            break

    # --- code usage patterns (observed) ----------------------------------
    priv = [u for u in dep.usage if u["private"]]
    if priv:
        u = priv[0]
        I.append(ind("private-api", "medium", "Uses private/internal API",
                     f"`{u['symbol']}` at {u['file']}:{u['line']} can change without notice",
                     "observed", "AST usage scan"))
    patches = [u for u in dep.usage if u["kind"] == "monkeypatch"]
    if patches:
        u = patches[0]
        I.append(ind("monkeypatch", "medium", "Monkeypatches the library",
                     f"`{u['code']}` at {u['file']}:{u['line']} depends on internal signatures",
                     "observed", "AST usage scan"))

    # --- public metadata (observed) --------------------------------------
    p = dep.pypi or {}
    inst = p.get("installed") or {}
    vulns = inst.get("vulnerabilities") or []
    if vulns:
        ids = sorted({v["id"] for v in vulns})
        fixed = sorted({f for v in vulns for f in v.get("fixed_in", [])})
        I.append(ind("known-vulnerabilities", "high", f"{len(ids)} known vulnerabilities",
                     f"{inst.get('version')} is affected by {', '.join(ids[:8])}"
                     + (f"; fixed in {', '.join(fixed[:4])}" if fixed else ""), "observed",
                     inst.get("source"), ids=ids, fixed_in=fixed))
    if inst.get("yanked"):
        I.append(ind("yanked", "high", "Installed version is yanked", inst.get("version"),
                     "observed", inst.get("source")))
    if p.get("classifiers_inactive"):
        I.append(ind("inactive", "high", "Marked inactive by maintainers",
                     ", ".join(p["classifiers_inactive"]), "observed", p.get("source")))
    days = p.get("days_since_latest_release")
    if days is not None and days > STALE_DAYS:
        I.append(ind("stale", "medium", "No release in over 3 years",
                     f"Latest release {p.get('latest_version')} on {p.get('latest_release_date')} "
                     f"({days} days ago)", "observed", p.get("source")))
    if dep.github and dep.github.get("available") and dep.github.get("archived"):
        I.append(ind("archived", "high", "Source repository archived",
                     f"github.com/{dep.github['repo']} is archived (read-only)", "observed",
                     dep.github.get("source")))
    mb = inst.get("major_behind")
    if mb and mb >= 1:
        I.append(ind("major-behind", "medium" if mb >= 1 else "low",
                     f"{mb} major version(s) behind",
                     f"Using {inst.get('version')}, latest is {p.get('latest_version')}",
                     "observed", p.get("source")))

    # --- executed baseline evidence --------------------------------------
    for py, b in baselines.items():
        for chk in b["imports"].get(dep.name, []):
            if not chk["ok"]:
                I.append(ind("import-fails", "critical", f"`import {chk['module']}` fails",
                             chk["error"], "executed", f"import check on Python {py}", python=py))
            for w in chk.get("warnings", []):
                if "Deprecat" in w["category"] or "Deprecat" in w["message"]:
                    I.append(ind("deprecation-on-import", "medium",
                                 f"Deprecation warning on `import {chk['module']}`",
                                 w["message"][:300], "executed", f"import check on Python {py}",
                                 python=py))
                    break
        tr = b["tests"]
        if tr.collection_error and dep.name in attribute_text(tr.collection_error, all_modules):
            I.append(ind("breaks-test-collection", "critical", "Test suite cannot start",
                         tr.collection_error.splitlines()[-1][:300], "executed",
                         f"pytest on Python {py}", python=py))
        failing = [(nid, t) for nid, t in tr.tests.items()
                   if t["outcome"] in ("failed", "error")
                   and dep.name in attribute_text(t["message"], all_modules)[:1]]
        if failing:
            nid, t = failing[0]
            last = [l for l in t["message"].splitlines() if l.strip()][:1]
            I.append(ind("test-failure", "critical", f"{len(failing)} test(s) fail inside this package",
                         f"{nid}: {last[0][:250] if last else ''}", "executed",
                         f"pytest on Python {py}", python=py, tests=[n for n, _ in failing]))
        warns = [w for w in tr.warnings
                 if dep.name in attribute_warning(w, all_modules, usage_by_loc)
                 and "Deprecat" in (w["category"] + w["message"])]
        if warns:
            w = warns[0]
            I.append(ind("deprecation-in-tests", "medium",
                         f"{len(warns)} deprecation warning(s) during tests",
                         f"{w['category']}: {w['message'][:250]}", "executed",
                         f"pytest on Python {py}", python=py))

    # de-duplicate identical indicators across Python versions
    seen, uniq = {}, []
    for i in I:
        key = (i["id"], i["title"])
        if key in seen:
            seen[key]["evidence"].setdefault("also_on", []).append(i["evidence"].get("python"))
            continue
        seen[key] = i
        uniq.append(i)
    dep.indicators = sorted(uniq, key=lambda i: -SEVERITY[i["severity"]])
