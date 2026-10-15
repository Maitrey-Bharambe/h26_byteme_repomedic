# RepoMedic: dependency exits, verified by execution

> RepoMedic is the project; `depexit` is its analysis engine and Python package.
> Both `repomedic` and `depexit` CLI commands work.

**Track 1: Gemma 4, Dependency Resilience**

`depexit` finds the at-risk dependencies in a Python repository, works out where and how each one is used, has **Gemma 4** propose a replacement and migrate the code, then **runs the test suite and behavioural probes against both the original and the replacement** on every target Python. The result is one reusable **Dependency Exit Capsule** per dependency, in which every statement is labelled as observed, executed or AI-generated.

```bash
depexit analyze https://github.com/rishrek/LinkWatch -p 3.12 -p 3.13
```

## Why it is different

| Typical tool | depexit |
|---|---|
| Reads manifests only | Manifests **+ AST usage with alias resolution** (`from requests import Session as S`) **+ installed metadata** (catches undeclared `certifi` and unused `six`, and ignores the commented-out `# PyYAML==5.1`) |
| "Upgrade everything" | Each dependency is migrated **in isolation** and attributed, then all verified exits are **merged and re-verified together** |
| Trusts green tests | Gemma-written **behaviour probes** run on original *and* replacement; any difference is a gap, even when every test passes |
| LLM output is trusted | Every AI claim is **validated**: the package exists on PyPI for the target Pythons, the stdlib module exists on each target Python, code parses, paths stay inside the repo, the old dependency is really gone (re-scan) |
| One-shot | **Fix loop**: failed verification evidence (tracebacks, probe diffs, leftover imports) goes back to Gemma, which retries a bounded number of times |
| Report | **Capsule**: JSON + Markdown + patch + probes + logs + `replay.sh`, reusable on any checkout |

## Pipeline

```
inspect ─▶ baseline ─▶ risk ─▶ propose ─▶ migrate ─▶ verify ─▶ compare ─▶ capsule
(observed)  (executed) (obs+exec) (Gemma)   (Gemma)   (executed) (executed)
                                    ▲                     │
                                    └──── fix loop ◀──────┘
```

1. **Inspect** (observed): `requirements*.txt`, `pyproject.toml` (including drift between them), commented requirements, AST usage sites, Makefile/Dockerfile/CI tool references.
2. **Baseline** (executed): a fresh `uv` venv per Python; pytest with a warnings-capture plugin and JUnit XML; `import` smoke test per dependency.
3. **Risk** (observed + executed): PyPI advisories, release staleness, inactive classifiers, archived GitHub repo, major-version lag, manifest drift, undeclared/unused, private API, monkeypatching, import failures, test failures and deprecation warnings attributed to the package that raised them.
4. **Propose** (Gemma 4): one of upgrade / replace / stdlib / remove / declare / keep, with an API mapping and risks. It is validated against PyPI and the target Pythons' stdlib; invalid proposals are rejected and the deterministic rules take over.
5. **Migrate** (Gemma 4): manifests are edited deterministically by the tool; Python code is rewritten by Gemma and checked with `ast.parse`.
6. **Verify** (executed): install the replacement, run the full suite and probes, re-import, and re-scan for leftover references.
7. **Combine** (executed): apply every verified exit together with a 3-way merge. On conflict, Gemma re-applies that migration on top of the combined tree, then everything is re-verified.

## Dependency Exit Capsule

```
depexit-out/
  REPORT.md                      overview, baseline, all deps, combined result
  index.json
  combined/all-migrations.patch  every verified exit applied together
  capsules/<dep>/
    CAPSULE.md                   human-readable, provenance badge on every section
    capsule.json                 schema dependency-exit-capsule/1.0
    migration.patch              manifests + code
    probes.py, probe_runner.py   AI-generated behaviour probes and their runner
    logs/                        original and replacement pytest logs per Python
    replay.sh                    re-runs original-vs-replacement verification from scratch
  llm_calls/                     every Gemma call: prompt, response, model, prompt hash, latency
```

Capsule sections follow the challenge brief: dependency details · risk indicators · usage locations · proposed replacement · migration steps · suggested tests · executed test results · compatibility gaps · open concerns and limitations.

**Provenance labels**

| Label | Meaning |
|---|---|
| 🔎 `observed` | Read from the repository or public metadata; source URL or file:line attached |
| 🧪 `executed` | Produced by running something; command and log attached |
| 🤖 `ai_generated` | Written by Gemma 4; model, prompt SHA-256 and full call log attached |
| ⚙️ `rule_based` | Deterministic fallback when no model is available, or when the model's output failed validation |

## AI model: Gemma 4 (required disclosure)

| | |
|---|---|
| **Model** | Gemma 4 by Google DeepMind. Default `gemma4:26b` (MoE, 26B total / ~4B active); `gemma4:e4b` for laptops, `gemma4:31b` for best quality |
| **Version** | Record the exact tag/digest you ran: `ollama show gemma4:26b`, or the `model` field returned by the API. Each call's model string is stored in `llm_calls/*.json` and in every capsule |
| **Access method** | OpenAI-compatible Chat Completions. Local: Ollama (`http://localhost:11434/v1`). Hosted: Google AI Studio / Gemini API OpenAI-compatible endpoint (`https://generativelanguage.googleapis.com/v1beta/openai`); list available Gemma 4 model ids with `GET /models` |
| **Terms** | Gemma 4 weights are released under the **Apache License 2.0**. Hosted use is additionally subject to the Google AI Studio / Gemini API terms of service |
| **What Gemma does** | Replacement proposal · code migration · behaviour-probe generation · fix loop on executed failures · merge-conflict re-application · interpretation of observed gaps |
| **What Gemma never does** | Decide pass/fail, edit manifests, or produce any "observed"/"executed" field |

## Install and run

Requirements: Python ≥ 3.11 for the tool, [`uv`](https://docs.astral.sh/uv/), git, and the target Pythons (3.12, 3.13) available to uv.

```bash
pip install -e .

# local Gemma 4 via Ollama
ollama pull gemma4:26b
depexit analyze https://github.com/rishrek/LinkWatch

# hosted Gemma 4 via Google AI Studio
export DEPEXIT_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
export DEPEXIT_LLM_API_KEY=...            # AI Studio key
export DEPEXIT_LLM_MODEL=<gemma-4 model id from GET /models>
depexit analyze https://github.com/rishrek/LinkWatch

# no model: deterministic analysis + rule-based proposals, still fully executed
depexit analyze ./LinkWatch --no-llm
```

| Option | Default | Description |
|---|---|---|
| `-p/--python` | 3.12, 3.13 | Pythons to verify on (repeatable) |
| `--only NAME` | all at-risk | Process only these dependencies |
| `--max-fix-attempts` | 2 | Gemma fix-loop iterations |
| `--jobs` | 4 | Dependencies processed in parallel |
| `--include-low` | off | Also build capsules for low/info risks |

Ollama is driven through its native `/api/chat` endpoint with an explicit context window
(`DEPEXIT_LLM_NUM_CTX`, default 8192) and thinking disabled. Its OpenAI-compatible
endpoint silently truncates prompts to the server default (often 4096 tokens). A prompt
that fills the window is reported as an error, never silently truncated. Other knobs:
`DEPEXIT_LLM_TIMEOUT` (seconds per call, default 900).

## Web workbench

```bash
pip install -e ".[web]"
depexit serve                 # http://127.0.0.1:8765, local only
```

1. **Analyse & plan**: enter a repository URL (the LinkWatch button fills the organizer repo) or
   a local path inside `DEPEXIT_ALLOWED_ROOTS` (default: the server's working directory). This runs
   the real pipeline's inspect, baseline, risk and plan stages and streams its events. **No code is modified.**
2. **Risk explorer**: each dependency shows declarations, usage sites, risk indicators with their
   evidence source, the proposed strategy, the API mapping cross-checked against observed usage
   (unmapped symbols are listed), deterministic validation of the model output, the verification
   requirements decided *before* execution, and unresolved uncertainties. Provenance badges
   separate *observed*, *executed*, *AI · Gemma* and *rule* content.
3. **Migrate & verify**: select planned dependencies. Each one is migrated in its own working copy
   and verified on every target Python; then every verified exit is merged and re-verified together.
4. **Proof**: every view renders the backend's `capsule.json` and links the capsule files (JSON,
   Markdown, `migration.patch`, test logs, probes, model-call logs). A patch can be applied to a
   *fresh working copy* (`<run>/applied/<dep>`). The analysed repository is never changed.

The header shows the real model status (`GET /models` on the local endpoint). If Gemma is
unavailable, the UI lists the AI features that are off, and the run continues deterministically.

### Verification semantics

Each exit has explicit requirements; each one evaluates to `met`, `failed` or `not_checked`:

| Status | Meaning |
|---|---|
| `verified` | every requirement executed and met |
| `verified_with_gaps` | all required checks met; advisory ones (new warnings, probes for manifest-only changes, tooling references, transitive installs) failed or did not run |
| `incomplete` | nothing failed, but a required check could not be performed (for example, the baseline has no passing test, or the code was rewritten but no behaviour probe ran) |
| `failed` | a required check failed: install, new collection error, regression, a baseline-passing test that no longer runs, leftover usage, a replacement that won't import, or a version that doesn't satisfy the plan |

A successful install never verifies anything by itself, and a skipped check is never counted
as passed. The combined result has its own status (`verified` / `partial` / `failed` /
`incomplete`) and lists conflicts, exits that were not combined, regressions and leftover usage.

### Execution boundary

Repository code (install hooks, tests, probes) runs as child processes in per-run working
copies, with an **allowlisted environment** (no API keys, tokens or `DEPEXIT_LLM_*`), HOME and TMP
redirected into the run directory, and timeouts. **Virtual environments are not a security
boundary**: that code can still read your files and use the network as your user. The UI
requires an explicit acknowledgement per run, and every report carries this boundary. For
untrusted repositories, run depexit itself inside a disposable VM or container. The server
binds to 127.0.0.1, rejects foreign `Host` headers, accepts only JSON POSTs, serves only
capsule artifacts and refuses non-local model endpoints (`DEPEXIT_ALLOW_REMOTE_LLM=1` overrides).

## Tests

```bash
pip install -e ".[test]"
python -m pytest
```

The tests use no network and no model: model replies are deterministic fakes, and the
execution layer (`sandbox`) is scripted, while the pipeline, verification, merge, capsule and
HTTP API code under test is real. They cover manifest handling, usage detection, plan and
API-mapping validation, edit validation, verification outcomes, regression detection, capsule
generation and the HTTP API. Scenarios include a migration that fails verification, incomplete
verification, and individual passes with a failed combined result.

## Limitations

- Python projects only (pip/uv-style manifests: requirements files and PEP 621 `pyproject.toml`).
- Behaviour probes are only as good as the edge cases Gemma chooses; they add coverage but do not prove equivalence.
- Advisory data comes from the PyPI JSON API; reachability of a vulnerable function is not yet analysed.
- Non-Python references (Makefile, CI) are detected and reported as gaps but not rewritten.

## Can the tests even see the dependency?

A green test run only proves something if the tests notice when the dependency breaks.
depexit measures this directly:

- **Test blindness**: each used dependency symbol is sabotaged (raise, return None, return
  a wrong value) and the *original* suite is re-run. The result is *watched*,
  *partly blind*, *blind* or *not exercised*, together with the tests that caught it.
- **Replay**: real calls of the repository's own functions are recorded during the
  original tests and replayed on the migrated code. Any difference fails the migration and
  is sent to Gemma's fix loop.

Verdicts: `verified` (watched + identical), `verified_by_replay` (tests blind, but real
recorded calls identical), `weakly_verified` (tests blind, nothing real replayed),
`incomplete` (a check could not run) or `failed`. See
[docs/verification.md](docs/verification.md#evidence-quality-test-blindness-and-replay).

## Documentation

Full docs are in [`docs/`](docs/README.md). Start with the **[technical approach](docs/technical-approach.md)**, then: [getting started](docs/getting-started.md) ·
[architecture](docs/architecture.md) · [verification semantics](docs/verification.md) ·
[web workbench](docs/web-ui.md) · [HTTP API](docs/http-api.md) ·
[capsule format](docs/capsule-format.md) · [configuration](docs/configuration.md) ·
[testing](docs/testing.md).

## Contributing, security, license

- [CONTRIBUTING.md](CONTRIBUTING.md): setup, ground rules, good first issues (Hacktoberfest welcome)
- [SECURITY.md](SECURITY.md): what depexit executes, the execution boundary, private reporting
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) · [CHANGELOG.md](CHANGELOG.md)
- Licensed under the [MIT License](LICENSE). Third-party components and model terms:
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
