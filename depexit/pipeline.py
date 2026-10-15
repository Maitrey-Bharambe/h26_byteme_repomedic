"""Orchestration: inspect -> baseline -> risk -> propose -> migrate -> verify -> capsule."""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from packaging.utils import canonicalize_name

from . import TOOL_NAME, __version__, ai, capsule, manifests, metadata, risk, sandbox, usage, verification
from .compare import compare_probes, compare_tests, compare_warnings, failure_feedback, unified_diff
from .llm import LLM, LLMConfig, LLMError
from .util import HttpCache, log, run, write_json

EXECUTION_BOUNDARY = {
    "kind": "host-process",
    "os_isolation": False,
    "detail": ("Repository code (install hooks, tests, probes) runs as child processes of this tool "
               "inside per-run working copies, with a scrubbed environment (no API keys/tokens), "
               "HOME/TMP redirected into the work directory and timeouts. Python virtual "
               "environments are NOT a security boundary: the code can still read files and use "
               "the network as your user. Analyse only repositories you trust, or run depexit "
               "itself inside a disposable VM/container."),
}


class PipelineError(RuntimeError):
    pass


def validate_target(target: str) -> str:
    """Accept https/ssh git URLs or an existing local directory; nothing else."""
    t = (target or "").strip()
    if not t or t.startswith("-"):
        raise PipelineError("target must be a repository URL or a local directory")
    if "://" in t:
        if not t.startswith("https://"):
            raise PipelineError("only https:// repository URLs are supported")
        return t
    if t.startswith("git@"):
        return t
    if not Path(t).is_dir():
        raise PipelineError(f"target not found: {t}")
    return t


class Pipeline:
    def __init__(self, target: str, out: Path, pythons: list[str], llm_cfg: LLMConfig | None,
                 only: list[str] | None = None, jobs: int = 4, max_fix_attempts: int = 2,
                 include_low: bool = False, on_event=None):
        self.target = validate_target(target)
        self.out = out.resolve()
        self.pythons = pythons
        self.only = {canonicalize_name(o) for o in only} if only else None
        self.jobs = jobs
        self.max_fix = max_fix_attempts
        self.include_low = include_low
        self.work = self.out / ".work"
        self.http = HttpCache(self.out / ".cache" / "http")
        self.llm = LLM(llm_cfg, self.out / "llm_calls") if llm_cfg else None
        self.llm_status = None
        self.lock = threading.Lock()
        self.events: list[dict] = []
        self.on_event = on_event
        self.results: dict = {}
        self.combined_result = None
        self.stage = "created"
        self.started = time.monotonic()

    def say(self, msg: str, stage: str | None = None, dep: str | None = None) -> None:
        """Log a real progress event (CLI stderr + optional listener, e.g. the web UI)."""
        if stage:
            self.stage = stage
        log(msg)
        ev = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "stage": self.stage,
              "dep": dep, "msg": msg}
        with self.lock:
            self.events.append(ev)
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:  # a broken listener must never break the analysis
                pass

    # ------------------------------------------------------------------ repo
    def resolve_repo(self) -> Path:
        t = self.target
        if t.startswith(("http://", "https://", "git@")):
            dst = self.work / "source"
            if dst.exists():
                shutil.rmtree(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            r = run(["git", "clone", "--depth", "1", "--no-recurse-submodules", "--", t, str(dst)],
                    timeout=600, untrusted=True)
            if not r.ok:
                raise PipelineError(f"git clone failed:\n{r.stderr.strip()}")
            return dst
        p = Path(t).resolve()
        if not p.is_dir():
            raise PipelineError(f"target not found: {t}")
        return p

    @staticmethod
    def commit_of(repo: Path) -> str | None:
        r = run(["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=30)
        return r.stdout.strip() if r.ok else None

    # ------------------------------------------------------------------ main
    def execute(self) -> Path:
        """Full run (CLI): analyse, plan, migrate + verify every at-risk dependency, combine."""
        self.analyze()
        self.plan_all()
        self.run_exits()
        return self.finalize()

    def analyze(self) -> None:
        """Inspect + baseline + risk. Nothing in the repository is modified."""
        self.started = time.monotonic()
        self.out.mkdir(parents=True, exist_ok=True)
        self.say(f"cloning / locating {self.target}", stage="inspect")
        repo = self.resolve_repo()
        self.repo = repo
        self.say(f"target: {repo}")
        if self.llm:
            ok, msg = self.llm.ping()
            self.llm_status = {"reachable": ok, "detail": msg, **self.llm.cfg.public()}
            self.say(f"Gemma endpoint {'reachable' if ok else 'NOT reachable: ' + msg}")
            if not ok:
                self.llm = None

        # 1. static inspection --------------------------------------------
        decls = manifests.collect(repo)
        drift = manifests.find_drift(decls)
        sites, parse_errors = usage.scan_python(repo)
        self.drift, self.parse_errors = drift, parse_errors
        self.say(f"manifests: {len(decls)} declarations, usage sites: {len(sites)}")

        # 2. baseline execution on every Python ---------------------------
        self.say(f"baseline: installing and testing the unmodified repo on Python {', '.join(self.pythons)}",
                 stage="baseline")
        self.baselines = {}
        with ThreadPoolExecutor(max_workers=len(self.pythons)) as ex:
            for py, b in zip(self.pythons, ex.map(lambda p: self.baseline(repo, p), self.pythons)):
                self.baselines[py] = b
        modules: dict[str, list[str]] = {}
        scripts: dict[str, list[str]] = {}
        for b in self.baselines.values():
            for m, ds in b["env"].pkgmap.get("modules", {}).items():
                modules.setdefault(m, [])
                modules[m] = sorted(set(modules[m]) | set(ds))
            for d, ss in b["env"].pkgmap.get("scripts", {}).items():
                scripts[d] = sorted(set(scripts.get(d, [])) | set(ss))
        self.modules = modules
        self.scripts = scripts
        self.stdlib = {py: set(b["env"].pkgmap.get("stdlib", [])) for py, b in self.baselines.items()}

        # 3. assemble per-dependency facts --------------------------------
        self.say("risk: import checks, PyPI/GitHub metadata, attribution", stage="risk")
        project_name = self.project_name = self._project_name(repo)
        deps: dict[str, risk.DepFacts] = {}

        def get(name):
            name = canonicalize_name(name)
            if name not in deps:
                deps[name] = risk.DepFacts(name)
            return deps[name]

        for d in decls:
            if d.group == "build" or d.name == project_name:
                continue
            get(d.name).declared.append(d.to_dict())
        for s in sites:
            ds = modules.get(s.module) or [canonicalize_name(s.module)]
            for dn in ds:
                if dn == project_name:
                    continue
                dep = get(dn)
                dep.usage.append(s.to_dict())
                if s.module not in dep.modules:
                    dep.modules.append(s.module)
        mods_by_dist: dict[str, list[str]] = {}
        for m, ds in modules.items():
            for d in ds:
                mods_by_dist.setdefault(d, []).append(m)
        for dn, s in usage.scan_tooling(repo, {d: scripts.get(d, []) for d in deps},
                                        {d: mods_by_dist.get(d, []) for d in deps}):
            if dn in deps:
                deps[dn].usage.append(s.to_dict())
        usage_by_loc = {(u["file"], u["line"]): d.name for d in deps.values() for u in d.usage}

        # import checks + metadata
        for py, b in self.baselines.items():
            versions = b["env"].pkgmap.get("versions", {})
            for dep in deps.values():
                if dep.name in versions:
                    dep.installed[py] = versions[dep.name]
                    mods = dep.modules or [m for m in mods_by_dist.get(dep.name, [])
                                           if not m.startswith("_")][:2]
                    b["imports"][dep.name] = [sandbox.import_check(b["env"], repo, m) for m in mods[:3]]

        def fetch_meta(dep):
            ver = next(iter(dep.installed.values()), None)
            if not ver:
                for d in dep.declared:
                    ver = manifests.pinned_version(d["specifier"]) or ver
            dep.pypi = metadata.pypi_info(self.http, dep.name, ver, self.pythons)
            gh = metadata.github_repo(dep.pypi) if dep.pypi.get("found") else None
            dep.github = metadata.github_info(self.http, gh) if gh else None

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(fetch_meta, deps.values()))
        imported = {d.name for d in deps.values() if d.usage}
        required_by: dict[str, set] = {}
        from packaging.requirements import InvalidRequirement, Requirement
        for b in self.baselines.values():
            for parent, reqs in b["env"].pkgmap.get("requires", {}).items():
                for r in reqs:
                    try:
                        req = Requirement(r)
                    except InvalidRequirement:
                        continue
                    if req.marker and "extra" in str(req.marker):
                        continue
                    required_by.setdefault(canonicalize_name(req.name), set()).add(parent)
        self.required_by = required_by
        for dep in deps.values():
            risk.build_indicators(dep, drift=drift, baselines=self.baselines, all_modules=modules,
                                  usage_by_loc=usage_by_loc, imported_dists=imported,
                                  required_by=required_by)

        self.deps = deps
        self.ranked = sorted(deps.values(), key=lambda d: -d.score)
        self.selected = [d for d in self.ranked if (d.at_risk or (self.include_low and d.indicators))
                         and (self.only is None or d.name in self.only)]
        self.say("at-risk: " + (", ".join(f"{d.name}({d.max_severity})" for d in self.selected) or "none"))

    def _parallel(self, fn, deps: list) -> None:
        with ThreadPoolExecutor(max_workers=max(1, self.jobs)) as ex:
            futs = {d.name: ex.submit(fn, d) for d in deps}
            for name, f in futs.items():
                try:
                    self.results[name] = f.result()
                except Exception as exc:  # keep going; record the failure honestly
                    self.say(f"{name}: pipeline error {exc!r}", dep=name)
                    self.results[name] = {**self.results.get(name, {}), "status": "tool_error",
                                          "error": repr(exc)}

    def plan_all(self, names: list[str] | None = None) -> None:
        """Propose + validate a migration plan per dependency. No code is modified."""
        self.say("planning: usage review, proposal, API mapping, verification requirements", stage="plan")
        deps = [d for d in self.selected if names is None or d.name in names]
        self._parallel(self.propose, deps)
        self.write_outputs()

    def run_exits(self, names: list[str] | None = None) -> None:
        """Migrate + verify planned dependencies, each in its own working copy."""
        self.say("migrate + verify in isolated working copies", stage="verify")
        deps = [d for d in self.selected if (names is None or d.name in names)
                and self.results.get(d.name, {}).get("status") == "planned"]
        self._parallel(lambda d: self.exit_dependency(d, self.results[d.name]), deps)

    def finalize(self) -> Path:
        """Combine every verified exit, re-verify together, write capsules."""
        self.say("combining every verified exit and re-verifying", stage="combine")
        self.combined_result = self.combined(self.repo, self.selected, self.results)
        self.write_outputs()
        self.say(f"done -> {self.out}", stage="done")
        return self.out

    def run_meta(self) -> dict:
        return {
            "tool": {"name": TOOL_NAME, "version": __version__},
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stage": self.stage,
            "target": {"source": self.target, "commit": self.commit_of(self.repo),
                       "pythons": self.pythons, "project": self.project_name},
            "ai": self.llm_status or {"reachable": False, "detail": "no model configured (--no-llm)"},
            "execution_boundary": EXECUTION_BOUNDARY,
            "baseline": {py: {"env": b["env"].summary(), "tests": b["tests"].to_dict()}
                         for py, b in self.baselines.items()},
            "manifest_drift": self.drift,
            "parse_errors": self.parse_errors,
            "seconds": round(time.monotonic() - self.started, 1),
            "_repo": str(self.repo),
            "_modules": self.modules,
        }

    def write_outputs(self) -> None:
        capsule.write_all(self.out, self.run_meta(), self.ranked, self.selected, self.results,
                          self.combined_result)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _project_name(repo: Path) -> str | None:
        try:
            import tomllib
            data = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
            n = data.get("project", {}).get("name")
            return canonicalize_name(n) if n else None
        except (OSError, ValueError):
            return None

    def baseline(self, repo: Path, py: str) -> dict:
        base = self.work / f"baseline-py{py}"
        rcopy = sandbox.copy_repo(repo, base / "repo")
        env = sandbox.create_env(rcopy, base / "venv", py)
        self.say(f"baseline py{py}: install {'ok' if env.ok else 'FAILED'}")
        tests = sandbox.run_tests(env, rcopy, base / "results")
        c = tests.counts()
        self.say(f"baseline py{py}: {c['passed']} passed, {c['failed']} failed, {c['error']} errors"
            + (" (collection error)" if tests.collection_error else ""))
        return {"env": env, "repo": rcopy, "tests": tests, "imports": {}, "dir": base}

    def _usage_files(self, dep) -> list[str]:
        return sorted({u["file"] for u in dep.usage if u["file"].endswith(".py")})

    def _texts(self, root: Path, files: list[str]) -> dict[str, str]:
        return {f: (root / f).read_text(encoding="utf-8") for f in files if (root / f).is_file()}

    # ------------------------------------------------------------------ one dependency
    def _allowed_new_imports(self, plan: dict) -> set[str]:
        """Modules a migration may newly import: stdlib, repo-local, already installed, planned."""
        allowed = set().union(*self.stdlib.values()) | {"__future__"}
        allowed |= usage.local_top_level_names(self.repo)
        allowed |= set(self.modules)
        if plan.get("package"):
            pkg = canonicalize_name(plan["package"])
            allowed |= {pkg.replace("-", "_"), plan["package"].replace("-", "_")}
            allowed |= {m for m, ds in self.modules.items() if pkg in ds}
        if plan.get("stdlib_module"):
            allowed.add(plan["stdlib_module"].split(".")[0])
        return allowed

    def propose(self, dep: risk.DepFacts) -> dict:
        """Stage 1 of an exit: usage review, plan, API mapping, requirements, probes."""
        name = dep.name
        res: dict = {"llm_calls": [], "validation": [], "attempts": [],
                     "_baseline_msgs": {py: {n: t["message"] for n, t in b["tests"].tests.items()}
                                        for py, b in self.baselines.items()}}
        usage_files = self._usage_files(dep)
        orig_texts = self._texts(self.repo, usage_files)
        self.say(f"{name}: proposing ({len(dep.usage)} usage sites in {len(usage_files)} files)", dep=name)

        # --- proposal ------------------------------------------------------
        plan, provenance = None, "rule_based"
        if self.llm:
            try:
                plan = ai.ai_plan(self.llm, dep, self.pythons, orig_texts)
                res["llm_calls"].append(plan.pop("_meta"))
                checks = ai.validate_plan(plan, dep, self.http, self.pythons, self.stdlib)
                res["validation"] += checks
                provenance = "ai_generated"
                if not ai.plan_accepted(checks):
                    res["rejected_ai_plan"] = plan
                    self.say(f"{name}: Gemma proposal failed validation, falling back to rules", dep=name)
                    plan, provenance = None, "rule_based"
            except LLMError as exc:
                res["llm_error"] = str(exc)
        if plan is None:
            plan = ai.rule_based_plan(dep)
            res["validation"] += ai.validate_plan(plan, dep, self.http, self.pythons, self.stdlib) \
                if plan["action"] != "keep" else []
        plan["provenance"] = provenance
        res["plan"] = plan
        res["api_mapping_review"] = ai.review_api_mapping(plan, dep)
        unc = [c for c in res["validation"] if not c["ok"] and not c.get("blocking", True)]
        res["uncertainties"] = [{"provenance": "observed", "text": f"{c['check']}: {c.get('detail')}"}
                                for c in unc]
        if not self.llm and plan["action"] in {"replace", "stdlib"}:
            res["uncertainties"].append({"provenance": "observed",
                                         "text": "Code rewrite needs Gemma, which is unavailable"})
        code_expected = bool(self.llm and plan.get("needs_code_changes") and orig_texts)
        res["verification_requirements"] = verification.requirements_for(plan, self.pythons, code_expected)
        self.say(f"{name}: plan = {plan['action']} {plan.get('package') or plan.get('stdlib_module') or ''}"
                 f"{plan.get('specifier') or ''} ({provenance})", dep=name)
        if plan["action"] == "keep":
            res["status"] = "not_executed"
            res["status_reason"] = "No safe replacement proposed"
            return res

        # --- probes (AI-generated suggested tests) -------------------------
        probe_code = None
        if self.llm and usage_files:
            try:
                probe_code, pchecks, meta = ai.ai_probes(self.llm, dep, plan, orig_texts, usage_files)
                res["llm_calls"].append(meta)
                res["validation"] += pchecks
            except LLMError as exc:
                res["probe_error"] = str(exc)
        res["probe_code"] = probe_code
        res["status"] = "planned"
        res["status_reason"] = "Plan ready for review; no code has been modified yet"
        return res

    def exit_dependency(self, dep: risk.DepFacts, res: dict | None = None) -> dict:
        """Stage 2 of an exit: migrate in a working copy, verify, fix loop, AI review."""
        if res is None:
            res = self.propose(dep)
            if res.get("status") != "planned":
                return res
        name = dep.name
        dwork = self.work / "deps" / name
        plan = res["plan"]
        usage_files = self._usage_files(dep)
        orig_texts = self._texts(self.repo, usage_files)
        res["attempts"] = []
        probe_file = None
        if res.get("probe_code"):
            probe_file = dwork / "probes.py"
            probe_file.parent.mkdir(parents=True, exist_ok=True)
            probe_file.write_text(res["probe_code"], encoding="utf-8")
        self.say(f"{name}: migrating in a working copy", dep=name)

        # --- migrate + verify, with a bounded fix loop ---------------------
        feedback = None
        best = None
        for attempt in range(1 + (self.max_fix if self.llm else 0)):
            att = self._attempt(dep, plan, dwork / f"attempt{attempt}", orig_texts, usage_files,
                                probe_file, feedback, res)
            res["attempts"].append(att["summary"])
            best = att
            self.say(f"{name}: attempt {attempt}: {att['evaluation']['status']}", dep=name)
            if att["clean"] or not self.llm or attempt >= self.max_fix:
                break
            feedback = failure_feedback(att["comparison"], self.modules, att["residual"])
            if not feedback or not orig_texts:
                break
            # a manifest-only change broke something: let Gemma adapt the code next time
            plan["needs_code_changes"] = True
            self.say(f"{name}: attempt {attempt} not clean, asking Gemma to fix "
                     f"({len(feedback)} chars of evidence)", dep=name)
        res.update({k: best[k] for k in ("comparison", "edits", "manifest_files", "patch", "repo", "residual")})
        res["verification"] = best["evaluation"]
        res["status"] = best["evaluation"]["status"]
        res["status_reason"] = "; ".join(
            f"{r['id']}: {r['result']}" for r in best["evaluation"]["requirements"]
            if r["result"] != verification.MET and r["required"]) or None

        # --- AI review of executed results ---------------------------------
        if self.llm:
            rv = ai.ai_review(self.llm, dep, plan, self._compact(best["comparison"]))
            if rv:
                res["llm_calls"].append(rv.pop("_meta"))
                res["review"] = rv
        self.say(f"{name}: {res['status']}", dep=name)
        return res

    def _attempt(self, dep, plan, adir: Path, orig_texts, usage_files, probe_file, feedback, res) -> dict:
        rcopy = sandbox.copy_repo(self.repo, adir / "repo")
        new_req = None
        if plan["action"] in {"upgrade", "replace", "declare"}:
            new_req = f"{plan['package']}{plan.get('specifier') or ''}"
        decls = [manifests.Declared(**d) for d in dep.declared]
        group = "dev" if decls and all(d.group == "dev" for d in decls) else "runtime"
        manifest_files = manifests.apply_dependency_change(rcopy, dep.name, new_req, decls, group)
        edits, checks = {}, []
        if self.llm and plan.get("needs_code_changes") and orig_texts:
            try:
                edits, checks, meta = ai.ai_migration(self.llm, dep, plan, orig_texts, rcopy, feedback,
                                                      self._allowed_new_imports(plan))
                res["llm_calls"].append(meta)
            except LLMError as exc:
                checks = [{"check": "Gemma produced a migration", "ok": False, "detail": str(exc),
                           "source": "llm"}]
        res["validation"] += checks
        for rel, content in edits.items():
            (rcopy / rel).write_text(content, encoding="utf-8")
        changed = sorted(set(manifest_files) | set(edits))
        patch = unified_diff(self.repo, rcopy, changed)
        residual = []
        if plan["action"] in {"remove", "replace", "stdlib"}:
            # observed re-scan of the migrated tree: is the old dependency really gone?
            sites, _ = usage.scan_python(rcopy)
            residual = [s.to_dict() for s in sites if s.module in dep.modules]
            residual += [s.to_dict() for d, s in usage.scan_tooling(
                rcopy, {dep.name: self.scripts.get(dep.name, [])}, {dep.name: dep.modules}) if d == dep.name]

        comparison = {}
        with ThreadPoolExecutor(max_workers=len(self.pythons)) as ex:
            for py, c in zip(self.pythons, ex.map(
                    lambda p: self._verify(dep, plan, rcopy, adir / f"py{p}", p, probe_file),
                    self.pythons)):
                comparison[py] = c
        code_expected = bool(edits) or bool(self.llm and plan.get("needs_code_changes") and orig_texts)
        reqs = verification.requirements_for(plan, self.pythons, code_expected)
        evaluation = verification.evaluate(plan, reqs, comparison, residual)
        clean = verification.is_clean(evaluation)
        summary = {"dir": str(adir.relative_to(self.out)), "feedback_given": bool(feedback),
                   "edited_files": sorted(edits), "clean": clean, "status": evaluation["status"],
                   "per_python": {py: {"install_ok": c["install_ok"],
                                       "tests": c["tests"]["replacement"],
                                       "regressed": len(c["tests"]["regressed"]),
                                       "probe_diffs": len((c.get("probes") or {}).get("different", []))}
                                  for py, c in comparison.items()}}
        summary["residual_usage"] = len(residual)
        return {"comparison": comparison, "edits": edits, "manifest_files": manifest_files,
                "patch": patch, "repo": rcopy, "clean": clean, "summary": summary, "residual": residual,
                "evaluation": evaluation}

    def _verify(self, dep, plan, rcopy: Path, vdir: Path, py: str, probe_file) -> dict:
        b = self.baselines[py]
        env = sandbox.create_env(rcopy, vdir / "venv", py)
        out = {"python": env.pkgmap.get("python", py), "install_ok": env.ok,
               "install_cmds": [" ".join(c) for c in env.install_cmds]}
        if not env.ok:
            out["install_error"] = env.install_log[-3000:]
            empty = sandbox.TestRun(-1, 0, {}, [], "replacement could not be installed", "", "")
            out["tests"] = compare_tests(b["tests"], empty)
            return out
        versions = env.pkgmap.get("versions", {})
        out["installed"] = {"original": b["env"].pkgmap.get("versions", {}).get(dep.name),
                            "replacement_package": plan.get("package"),
                            "replacement_version": versions.get(canonicalize_name(plan["package"]))
                            if plan.get("package") else None,
                            "original_still_installed": dep.name in versions and plan["action"] in
                            {"remove", "stdlib", "replace"}}
        tests = sandbox.run_tests(env, rcopy, vdir / "results")
        out["tests"] = compare_tests(b["tests"], tests)
        out["warnings"] = compare_warnings(b["tests"].warnings, tests.warnings)
        mod = plan.get("stdlib_module") or None
        if not mod and plan.get("package"):
            mods = [m for m, ds in env.pkgmap.get("modules", {}).items()
                    if canonicalize_name(plan["package"]) in ds and not m.startswith("_")]
            mod = mods[0] if mods else None
        if mod:
            out["replacement_import"] = sandbox.import_check(env, rcopy, mod)
        if probe_file:
            orig = sandbox.run_probes(b["env"], b["repo"], probe_file, vdir / "probes_original.json")
            repl = sandbox.run_probes(env, rcopy, probe_file, vdir / "probes_replacement.json")
            out["probes"] = compare_probes(orig, repl)
            out["probes"]["original_results"] = orig
            out["probes"]["replacement_results"] = repl
        out["test_log"] = str((vdir / "results" / "pytest.log").relative_to(self.out))
        return out

    @staticmethod
    def _compact(comparison: dict) -> dict:
        c = {}
        for py, v in comparison.items():
            c[py] = {"install_ok": v["install_ok"], "tests": {k: v["tests"][k] for k in
                     ("original", "replacement", "fixed", "regressed", "still_failing",
                      "replacement_collection_error")},
                     "new_warnings": (v.get("warnings") or {}).get("new", [])[:10],
                     "probe_diffs": (v.get("probes") or {}).get("different", [])[:10]}
        return c

    # ------------------------------------------------------------------ combined
    def combined(self, repo: Path, selected, results) -> dict | None:
        ok = [d for d in selected if results.get(d.name, {}).get("status") in ("verified", "verified_with_gaps")]
        excluded = [{"dependency": d.name, "status": results[d.name].get("status")} for d in selected
                    if d not in ok and results.get(d.name, {}).get("status")
                    not in (None, "not_executed", "planned")]
        if not ok:
            return None
        cdir = self.work / "combined"
        rcopy = sandbox.copy_repo(repo, cdir / "repo")
        merged, conflicts, rebased, skipped, applied, files = [], [], [], [], [], set()
        for dep in ok:
            r = results[dep.name]
            plan = r["plan"]
            new_req = f"{plan['package']}{plan.get('specifier') or ''}" if plan["action"] in {"upgrade", "replace", "declare"} else None
            decls = [manifests.Declared(**d) for d in dep.declared]
            group = "dev" if decls and all(d.group == "dev" for d in decls) else "runtime"
            edits = r.get("edits") or {}
            # snapshot everything this dependency may touch so a failed merge can be rolled back
            touch = {f.relative_to(rcopy).as_posix() for f in manifests.requirement_files(rcopy)}
            touch |= {"pyproject.toml"} | set(edits)
            snap = {t: (rcopy / t).read_text(encoding="utf-8") for t in touch if (rcopy / t).is_file()}
            dep_files = set(manifests.apply_dependency_change(rcopy, dep.name, new_req, decls, group))
            clash = []
            for rel, content in edits.items():
                cur = rcopy / rel
                base_f = cdir / "merge" / dep.name / (rel.replace("/", "__") + ".base")
                other_f = base_f.with_suffix(".other")
                base_f.parent.mkdir(parents=True, exist_ok=True)
                base_f.write_text((repo / rel).read_text(encoding="utf-8"), encoding="utf-8")
                other_f.write_text(content, encoding="utf-8")
                before = cur.read_text(encoding="utf-8")
                p = subprocess.run(["git", "merge-file", str(cur), str(base_f), str(other_f)],
                                   capture_output=True, text=True)
                if p.returncode:
                    cur.write_text(before, encoding="utf-8")
                    clash.append(rel)
                    conflicts.append(f"{dep.name}:{rel}")
                else:
                    merged.append(f"{dep.name}:{rel}")
                dep_files.add(rel)
            if clash and self.llm:
                # let Gemma re-apply this migration on top of the already-combined code
                texts = self._texts(rcopy, sorted(set(clash) | set(self._usage_files(dep))))
                try:
                    new_edits, checks, meta = ai.ai_migration(self.llm, dep, plan, texts, rcopy)
                    if all(c["ok"] for c in checks) and set(clash) <= set(new_edits):
                        for rel, content in new_edits.items():
                            (rcopy / rel).write_text(content, encoding="utf-8")
                        rebased.append({"dependency": dep.name, "files": sorted(new_edits),
                                        "provenance": "ai_generated", "llm_call": meta})
                        clash = []
                except LLMError:
                    pass
            if clash:
                for t, txt in snap.items():
                    (rcopy / t).write_text(txt, encoding="utf-8")
                skipped.append({"dependency": dep.name, "reason": f"merge conflict in {', '.join(clash)}"})
                continue
            applied.append(dep.name)
            files |= dep_files
        comparison = {}
        for py in self.pythons:
            env = sandbox.create_env(rcopy, cdir / f"py{py}" / "venv", py)
            if not env.ok:
                comparison[py] = {"install_ok": False, "install_error": env.install_log[-2000:]}
                continue
            t = sandbox.run_tests(env, rcopy, cdir / f"py{py}" / "results")
            comparison[py] = {"install_ok": True, "tests": compare_tests(self.baselines[py]["tests"], t),
                              "warnings": compare_warnings(self.baselines[py]["tests"].warnings, t.warnings)}
        self.say("combined: " + ", ".join(f"py{py} {c.get('tests', {}).get('replacement')}"
                                          for py, c in comparison.items()))
        # observed re-scan of the combined tree for every dependency that should be gone
        residual_by_dep = {}
        sites, _ = usage.scan_python(rcopy)
        for dep in ok:
            if dep.name in applied and results[dep.name]["plan"]["action"] in verification.REMOVES_ORIGINAL:
                residual_by_dep[dep.name] = [s.to_dict() for s in sites if s.module in dep.modules]
        ev = verification.evaluate_combined(applied, skipped, comparison, residual_by_dep)
        self.say(f"combined: {ev['status']}")
        return {"dependencies": applied, "merged_edits": merged, "conflicts": conflicts,
                "rebased_by_ai": rebased, "skipped": skipped, "excluded_unverified": excluded,
                "comparison": comparison, "residual": residual_by_dep,
                "status": ev["status"], "verification": ev,
                "patch": unified_diff(repo, rcopy, sorted(files)), "repo": rcopy}
