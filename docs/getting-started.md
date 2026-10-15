# Getting started

## Requirements

- Python ≥ 3.11 to run depexit
- [uv](https://docs.astral.sh/uv/) and git on `PATH`
- the target Pythons available to uv (default **3.12** and **3.13**; `uv python install 3.12 3.13`)
- optional: [Ollama](https://ollama.com) with a Gemma 4 model for the AI steps

```bash
pip install -e ".[web]"
ollama pull gemma4:e4b        # small; gemma4:26b or gemma4:31b give better proposals
```

## First run (CLI)

```bash
# deterministic only: no model, rule-based plans, everything still executed
depexit analyze https://github.com/rishrek/LinkWatch --no-llm

# with local Gemma 4
export DEPEXIT_LLM_MODEL=gemma4:e4b
depexit analyze https://github.com/rishrek/LinkWatch --jobs 2
```

The command prints the path of `depexit-out/REPORT.md`. Open it, then any
`capsules/<dep>/CAPSULE.md`.

What to expect on LinkWatch (as observed during development; your numbers may differ as
PyPI changes):

- baseline: 9 tests pass and 1 fails on Python 3.12 and 3.13;
- about 11 at-risk dependencies, including `jinja2` (import fails, known
  vulnerabilities), `nose` (import fails on 3.12+), `setuptools` (runtime pin of a build
  tool), `pydantic` (manifest drift), `requests` (private API and monkeypatching),
  `six`/`idna` (unused) and `certifi` (undeclared);
- the rule-based run (`--no-llm`) verified several exits and failed `setuptools`: the
  upgraded setuptools no longer ships `pkg_resources`, so test collection breaks. That is a
  real compatibility gap, and the exit is reported as failed rather than hidden.

## First run (web workbench)

```bash
depexit serve          # http://127.0.0.1:8765
```

1. Click **Use LinkWatch** (or paste any https git URL or a local path), tick the
   execution acknowledgement, and click **Analyse & plan**.
2. Wait for **Plans ready · review**. Nothing has been modified yet.
3. Open dependencies from the graph or the table to review the plan, API mapping,
   validation and verification requirements.
4. Select the planned dependencies and click **Migrate & verify**.
5. Read the per-dependency verification, the combined result, and download the capsule
   artifacts. **Apply to a fresh working copy** gives you a patched copy to inspect.

See [web-ui.md](web-ui.md) for every screen.

## Performance notes

Model calls dominate run time. On a 4 GB laptop GPU, `gemma4:e4b` runs at roughly
6 tokens/s, so planning 11 dependencies took about 36 minutes. Use `--jobs 1-2` with a
single local GPU (parallel calls just queue), `--only NAME` to focus on one dependency, or
`--no-llm` for a quick deterministic pass.
