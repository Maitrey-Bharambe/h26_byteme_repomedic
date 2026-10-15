# Architecture

```
            ┌──────────── analyze() ────────────┐   ┌─ plan_all() ─┐   ┌─ run_exits() ─┐   ┌─ finalize() ─┐
 target ──▶ inspect ─▶ baseline ─▶ risk scoring ─▶ propose+validate ─▶ migrate+verify ─▶ combine+verify ─▶ capsules
            observed    executed    observed+exec     AI / rule          executed            executed
                                                         ▲                    │ fix loop (Gemma, bounded)
                                                         └────────────────────┘
```

`Pipeline` (`depexit/pipeline.py`) exposes each stage separately so the web UI can stop
after planning for human review. `Pipeline.execute()` runs them all, which is what the CLI
does.

| Stage | Method | Modifies code? | Produces |
|---|---|---|---|
| Inspect | `analyze()` | no | declarations, drift, AST usage sites |
| Baseline | `analyze()` → `baseline()` | no (copies) | venv per Python, pytest results, warnings, installed metadata |
| Risk | `analyze()` | no | `DepFacts.indicators` with evidence |
| Plan | `plan_all()` → `propose()` | no | plan, validation checks, API-mapping review, uncertainties, verification requirements, probes |
| Exit | `run_exits()` → `exit_dependency()` → `_attempt()` / `_verify()` | **working copy only** | patch, per-Python comparison, evaluation, fix-loop attempts, AI review |
| Combine | `finalize()` → `combined()` | **working copy only** | 3-way merge of verified exits, re-verification, combined evaluation |
| Write | `write_outputs()` → `capsule.write_all()` | no | `index.json`, `REPORT.md`, capsules |

Progress is reported through `Pipeline.say()`, which logs to stderr, appends to
`Pipeline.events` and calls the optional `on_event` listener (used by the web UI).

## Modules

| Module | Responsibility | Provenance of its output |
|---|---|---|
| `manifests.py` | parse/edit `requirements*.txt` and PEP 621 `pyproject.toml`; drift | observed |
| `usage.py` | AST usage map (aliases, attribute chains, subclassing, monkeypatching, private API); build/CI tool references | observed |
| `metadata.py` | PyPI and GitHub metadata; `package_exists` hallucination guard | observed |
| `sandbox.py` | uv venvs, installs, pytest with JUnit and warning capture, import checks, probe runner | executed |
| `risk.py` | turns facts into indicators with evidence | observed + executed |
| `ai.py` | Gemma steps plus deterministic validation of every model output | ai_generated / observed |
| `prompts.py` | narrow, checkable prompts | – |
| `llm.py` | Ollama-native or OpenAI-compatible client; logs every call | – |
| `compare.py` | original vs replacement: tests, warnings, probes, failure feedback | executed |
| `verification.py` | requirements and status derivation ([details](verification.md)) | executed |
| `capsule.py` | capsule JSON and Markdown, index, report, replay script | – |
| `server.py` + `web/` | local Flask API and vanilla JS UI | renders capsules only |

## What the model may and may not do

Gemma writes **proposals, API mappings, code rewrites, behaviour probes, fix attempts,
merge re-applications and reviews**. It never edits manifests (the tool does that
deterministically), never decides pass/fail, and never produces an `observed` or
`executed` field. Model output is checked before use:

- the plan: the action is valid; the package exists on PyPI with a version that satisfies
  the specifier on every target Python; the stdlib module exists on every target Python;
  replace ≠ original; mapped replacements don't use the old dependency; mapping coverage
  (advisory);
- edits: the path stays inside the repo, the file was given to the model, the file
  exists, it parses, and it adds no unplanned third-party imports;
- probes: the module parses, defines `PROBES`, and imports no
  socket/subprocess/shutil/ctypes.

If a blocking check fails, the plan is rejected, recorded in the capsule
(`rejected_ai_plan`), and replaced by the rule-based plan.
