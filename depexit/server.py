"""Local web interface: a thin Flask API over the real Pipeline.

Every number the UI shows comes from the pipeline's own outputs (index.json and
capsules/<dep>/capsule.json written by capsule.py); this module only starts runs,
reports their live events and serves their artifacts.

Safety:
  * binds to 127.0.0.1 by default and rejects foreign Host headers (DNS rebinding)
  * state-changing endpoints accept JSON only (no cross-site form posts)
  * local targets must resolve inside DEPEXIT_ALLOWED_ROOTS (default: server cwd)
  * the model endpoint must be local (Ollama) unless DEPEXIT_ALLOW_REMOTE_LLM=1;
    API keys never leave the server
  * running repository code requires an explicit acknowledgement per run, and the
    execution boundary (no OS isolation) is reported with every result
  * migrations are applied only to fresh working copies under the run directory
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from . import sandbox
from .llm import LLMConfig
from .pipeline import EXECUTION_BOUNDARY, Pipeline, PipelineError, validate_target
from .util import read_json, run

WEB = Path(__file__).parent / "web"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
PY_RE = re.compile(r"^3\.\d{1,2}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,100}$")
SERVABLE = ("capsules/", "combined/", "llm_calls/", "applied/")
AI_FEATURES = ["replacement proposals for replace/stdlib migrations", "code rewriting",
               "behaviour probes", "fix loop on failed verification",
               "AI interpretation of compatibility gaps", "merge-conflict re-application"]


def allowed_roots() -> list[Path]:
    raw = os.environ.get("DEPEXIT_ALLOWED_ROOTS")
    roots = raw.split(os.pathsep) if raw else [os.getcwd()]
    return [Path(r).resolve() for r in roots if r.strip()]


def check_target(target: str) -> str:
    t = validate_target(target)
    if "://" in t or t.startswith("git@"):
        return t
    p = Path(t).resolve()
    if not any(p.is_relative_to(r) for r in allowed_roots()):
        raise PipelineError(f"local path is outside the allowed roots "
                            f"({', '.join(str(r) for r in allowed_roots())}); set DEPEXIT_ALLOWED_ROOTS")
    return str(p)


def llm_is_local(base_url: str) -> bool:
    host = (urllib.parse.urlparse(base_url).hostname or "").lower()
    return host in LOCAL_HOSTS


def llm_status(cfg: LLMConfig | None = None) -> dict:
    """Real connectivity check: GET <base>/models on the configured endpoint."""
    cfg = cfg or LLMConfig()
    out = {"base_url": cfg.base_url, "configured_model": cfg.model, "local": llm_is_local(cfg.base_url),
           "reachable": False, "models": [], "model_available": False, "detail": None}
    if not out["local"] and os.environ.get("DEPEXIT_ALLOW_REMOTE_LLM") != "1":
        out["detail"] = "endpoint is not local; remote/paid APIs are disabled"
    else:
        req = urllib.request.Request(cfg.base_url.rstrip("/") + "/models")
        if cfg.api_key:
            req.add_header("Authorization", f"Bearer {cfg.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            out["models"] = sorted(m.get("id") for m in data.get("data", []) if m.get("id"))
            out["reachable"] = True
            gemma = [m for m in out["models"] if "gemma" in m.lower()]
            out["gemma_models"] = gemma
            out["model_available"] = cfg.model in out["models"]
            if not out["model_available"] and gemma:
                out["suggested_model"] = gemma[0]
            out["detail"] = "ok" if out["model_available"] else f"model {cfg.model} is not pulled"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            out["detail"] = f"not reachable: {exc}"
    usable = out["reachable"] and (out["model_available"] or bool(out.get("suggested_model")))
    out["unavailable_features"] = [] if usable else AI_FEATURES
    out["fallback"] = None if usable else (
        "Deterministic analysis still runs: manifests, usage, metadata, baseline tests, risk "
        "indicators, rule-based upgrade/remove/declare plans and their full execution-based verification.")
    return out


class Job:
    def __init__(self, jid: str, out: Path, target: str, opts: dict):
        self.id, self.out, self.target, self.opts = jid, out, target, opts
        self.state = "queued"          # queued | running | awaiting_review | done | error
        self.error: str | None = None
        self.pipeline: Pipeline | None = None
        self.created = time.time()
        self.thread: threading.Thread | None = None

    def view(self, events_since: int = 0) -> dict:
        p = self.pipeline
        events = p.events[events_since:] if p else []
        return {"id": self.id, "state": self.state, "error": self.error, "target": self.target,
                "options": self.opts, "stage": p.stage if p else None,
                "event_count": len(p.events) if p else 0, "events": events,
                "index": read_json(self.out / "index.json"),
                "execution_boundary": EXECUTION_BOUNDARY}


def create_app(runs_dir: Path, pipeline_factory=Pipeline) -> Flask:
    app = Flask(__name__, static_folder=None)
    runs_dir = Path(runs_dir).resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)
    jobs: dict[str, Job] = {}
    busy = threading.Lock()   # one execution at a time: runs are CPU/GPU heavy

    @app.before_request
    def _guard():
        host = (request.host or "").rsplit(":", 1)[0].lower()
        if host not in LOCAL_HOSTS and os.environ.get("DEPEXIT_ALLOW_ANY_HOST") != "1":
            abort(403, "host not allowed")
        if request.method == "POST" and not request.is_json:
            abort(415, "JSON body required")

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(415)
    def _err(e):
        return jsonify({"error": getattr(e, "description", str(e))}), e.code

    def get_job(jid) -> Job:
        job = jobs.get(jid)
        if not job:
            abort(404, "unknown run")
        return job

    def background(job: Job, fn):
        def target():
            if not busy.acquire(blocking=False):
                job.state, job.error = "error", "another run is executing"
                return
            try:
                job.state = "running"
                fn()
            except PipelineError as exc:
                job.state, job.error = "error", str(exc)
            except Exception as exc:  # surfaced verbatim in the UI
                job.state, job.error = "error", f"{type(exc).__name__}: {exc}"
            finally:
                busy.release()
        job.thread = threading.Thread(target=target, daemon=True)
        job.thread.start()

    # ------------------------------------------------------------------ static UI
    @app.get("/")
    def index():
        return send_from_directory(WEB, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(WEB, name)

    # ------------------------------------------------------------------ API
    @app.get("/api/llm")
    def api_llm():
        return jsonify(llm_status())

    @app.get("/api/config")
    def api_config():
        return jsonify({"allowed_roots": [str(r) for r in allowed_roots()],
                        "execution_boundary": EXECUTION_BOUNDARY, "busy": busy.locked()})

    @app.get("/api/jobs")
    def api_jobs():
        return jsonify([{"id": j.id, "state": j.state, "target": j.target, "created": j.created,
                         "stage": j.pipeline.stage if j.pipeline else None}
                        for j in sorted(jobs.values(), key=lambda j: -j.created)])

    @app.post("/api/jobs")
    def api_create():
        body = request.get_json(silent=True) or {}
        if body.get("acknowledge_untrusted_execution") is not True:
            abort(400, "acknowledge that the repository's install hooks and tests will execute "
                       "on this machine without OS-level isolation")
        try:
            target = check_target(str(body.get("target") or ""))
        except PipelineError as exc:
            abort(400, str(exc))
        pythons = body.get("pythons") or ["3.12", "3.13"]
        if not (isinstance(pythons, list) and 0 < len(pythons) <= 3 and all(PY_RE.match(str(p)) for p in pythons)):
            abort(400, "pythons must be 1-3 versions like 3.12")
        only = body.get("only") or None
        if only and not all(isinstance(o, str) and NAME_RE.match(o.lower()) for o in only):
            abort(400, "invalid dependency name in `only`")
        cfg, llm_note = None, "not requested"
        if body.get("use_llm", True):
            cfg = LLMConfig()
            st = llm_status(cfg)
            model = body.get("model") or (cfg.model if st["model_available"] else st.get("suggested_model"))
            if not st["reachable"]:
                cfg, llm_note = None, st["detail"]  # deterministic mode; reason shown in the UI
            elif model and model in st["models"]:
                cfg.model, llm_note = model, "ok"
            else:
                abort(400, f"model {model!r} is not available at {cfg.base_url}")
        if busy.locked():
            abort(409, "another run is executing; wait for it to finish")
        jid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        out = runs_dir / jid
        opts = {"pythons": pythons, "use_llm": cfg is not None, "model": cfg.model if cfg else None,
                "llm_note": llm_note,
                "include_low": bool(body.get("include_low")), "only": only,
                "max_fix_attempts": max(0, min(int(body.get("max_fix_attempts", 2)), 3)),
                "jobs": max(1, min(int(body.get("jobs", 2)), 4))}
        job = Job(jid, out, target, opts)
        job.pipeline = pipeline_factory(target, out, pythons, cfg, only=only, jobs=opts["jobs"],
                                        max_fix_attempts=opts["max_fix_attempts"],
                                        include_low=opts["include_low"])
        jobs[jid] = job

        def plan_stage():
            job.pipeline.analyze()
            job.pipeline.plan_all()
            job.state = "awaiting_review"
            if body.get("auto_execute"):
                job.state = "running"
                job.pipeline.run_exits()
                job.pipeline.finalize()
                job.state = "done"
        background(job, plan_stage)
        return jsonify(job.view()), 202

    @app.get("/api/jobs/<jid>")
    def api_job(jid):
        since = request.args.get("since", default=0, type=int)
        return jsonify(get_job(jid).view(max(0, since)))

    @app.post("/api/jobs/<jid>/execute")
    def api_execute(jid):
        job = get_job(jid)
        if job.state not in ("awaiting_review", "done"):
            abort(409, f"run is {job.state}; plans must be ready before execution")
        body = request.get_json(silent=True) or {}
        names = body.get("deps")
        planned = {n for n, r in job.pipeline.results.items() if r.get("status") == "planned"}
        if names is not None:
            if not isinstance(names, list) or not set(names) <= planned:
                abort(400, f"deps must be a subset of the planned dependencies: {sorted(planned)}")
        if not (set(names) if names is not None else planned):
            abort(400, "no planned dependency selected")
        if busy.locked():
            abort(409, "another run is executing")

        def exec_stage():
            job.pipeline.run_exits(names)
            job.pipeline.finalize()
            job.state = "done"
        background(job, exec_stage)
        return jsonify(job.view()), 202

    @app.get("/api/jobs/<jid>/capsules/<name>")
    def api_capsule(jid, name):
        job = get_job(jid)
        if not NAME_RE.match(name):
            abort(404)
        cap = read_json(job.out / "capsules" / name / "capsule.json")
        if cap is None:
            abort(404, "no capsule for this dependency")
        cdir = job.out / "capsules" / name
        files = sorted(p.relative_to(job.out).as_posix() for p in cdir.rglob("*") if p.is_file())
        return jsonify({"capsule": cap, "files": files})

    @app.get("/api/jobs/<jid>/files/<path:rel>")
    def api_file(jid, rel):
        job = get_job(jid)
        if not (rel.startswith(SERVABLE) or rel in ("REPORT.md", "index.json")):
            abort(404)
        path = (job.out / rel).resolve()
        if not path.is_relative_to(job.out.resolve()) or not path.is_file():
            abort(404)
        return send_file(path, mimetype="text/plain; charset=utf-8", as_attachment=bool(request.args.get("download")),
                         download_name=path.name)

    @app.post("/api/jobs/<jid>/apply")
    def api_apply(jid):
        """Apply a verified patch to a FRESH working copy. The analysed repo is never touched."""
        job = get_job(jid)
        body = request.get_json(silent=True) or {}
        which = str(body.get("which") or "")
        if which == "combined":
            patch = job.out / "combined" / "all-migrations.patch"
        elif NAME_RE.match(which):
            patch = job.out / "capsules" / which / "migration.patch"
        else:
            abort(400, "which must be a dependency name or 'combined'")
        if not patch.is_file() or not patch.read_text(encoding="utf-8").strip():
            abort(404, "no patch available")
        p = job.pipeline
        if not p or not getattr(p, "repo", None):
            abort(409, "repository not available")
        dst = job.out / "applied" / which
        sandbox.copy_repo(p.repo, dst)
        r = run(["git", "apply", "--verbose", "--whitespace=nowarn", str(patch)], cwd=dst, timeout=60)
        status = "applied" if r.ok else "failed"
        if not r.ok:
            shutil.rmtree(dst, ignore_errors=True)
        return jsonify({"status": status, "working_copy": str(dst) if r.ok else None,
                        "patch": patch.relative_to(job.out).as_posix(),
                        "output": (r.stdout + r.stderr)[-4000:],
                        "note": "Original repository left unchanged."})

    return app


def serve(host: str, port: int, runs_dir: Path) -> None:
    if host not in LOCAL_HOSTS:
        print("WARNING: binding to a non-local address exposes code execution to the network.")
    app = create_app(runs_dir)
    print(f"depexit UI on http://{'127.0.0.1' if host in LOCAL_HOSTS else host}:{port}  (runs: {runs_dir})")
    app.run(host=host, port=port, debug=False, threaded=True)
