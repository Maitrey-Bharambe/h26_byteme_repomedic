# depexit: Dependency Exit Capsules, verified by execution

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

## Limitations

- Python projects only (pip/uv-style manifests: requirements files and PEP 621 `pyproject.toml`).
- Behaviour probes are only as good as the edge cases Gemma chooses; they add coverage but do not prove equivalence.
- Advisory data comes from the PyPI JSON API; reachability of a vulnerable function is not yet analysed.
- Non-Python references (Makefile, CI) are detected and reported as gaps but not rewritten.
