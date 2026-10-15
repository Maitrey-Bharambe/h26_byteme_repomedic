# Contributing to depexit

Thanks for helping! depexit takes part in **Hacktoberfest**: pull requests that follow this
guide and pass CI are welcome.

## Set up

```bash
git clone <your fork>
cd depexit
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[test,web]"
python -m pytest
```

To run real analyses you also need [uv](https://docs.astral.sh/uv/), git, the target
Pythons (3.12 and 3.13 by default), and optionally [Ollama](https://ollama.com) with a
Gemma 4 model (`ollama pull gemma4:e4b`).

```bash
depexit analyze https://github.com/rishrek/LinkWatch --no-llm   # deterministic, quick
depexit serve                                                   # web workbench
```

## Ground rules

1. **Never overstate a verification result.** A skipped or failed check is never a pass.
   New checks belong in `depexit/verification.py`, as a requirement evaluated to
   `met` / `failed` / `not_checked`.
2. **Label provenance.** Every fact in a capsule is `observed`, `executed`, `ai_generated`
   or `rule_based`. Model output must be validated deterministically before it is used
   (`depexit/ai.py`).
3. **Keep the UI honest.** The web UI renders the backend's `index.json` and `capsule.json`;
   it never computes or invents results.
4. **No new heavy dependencies.** The core depends only on `packaging`; the UI adds Flask and
   plain HTML, CSS and JavaScript.
5. **Tests are deterministic.** No network and no real model in unit tests: use the fakes in
   `tests/conftest.py` (`FakeHttp`, `FakeLLM`, `fake_exec`).

## Pull requests

- Keep each PR focused, and describe *what* changed and *how you verified it*.
- Add or update tests (`python -m pytest` must pass) and docs under `docs/`.
- Match the surrounding code style: type hints, small functions, comments only where they
  explain *why*.
- For UI changes, include a screenshot in light and dark mode.

## Good first issues

- More manifest formats (Poetry, `setup.cfg`, `Pipfile`).
- Probe-quality heuristics (reject probes that never touch the migrated module).
- An optional container execution backend (Docker/Podman) for OS-level isolation.
- Rewriting Makefile/CI references (today they are only reported).

By contributing you agree that your contributions are licensed under the MIT License, and
that you will follow the [Code of Conduct](CODE_OF_CONDUCT.md).
