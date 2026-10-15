# Testing

```bash
pip install -e ".[test]"
python -m pytest            # about 7 s; no network, no model, no real installs
```

## Layout

| File | Covers |
|---|---|
| `tests/test_manifests_usage.py` | requirements and pyproject parsing (groups, comments, options), drift, manifest editing; AST usage (aliases, subclass, monkeypatch, private API, stdlib/local exclusion); tooling scan |
| `tests/test_ai_validation.py` | plan validation (hallucinated package, Python-incompatible version, stdlib module, replace-with-itself), API-mapping review and coverage, blocking vs advisory, edit validation (traversal, syntax, unplanned imports, files not given), JSON extraction, deterministic fake model replies |
| `tests/test_verification.py` | every requirement and status; **migration fails verification**, **install success alone is not verified**, **incomplete verification**, probe errors, residual usage, version mismatch, vanished tests, **individual pass but combined fails**, partial combines |
| `tests/test_pipeline_capsule.py` | the real `Pipeline` end to end over a scripted execution layer: plan stage modifies nothing, failed and incomplete exits, combined failure, events, capsule and report contents, target validation |
| `tests/test_server.py` | the HTTP API with the real pipeline: guards (host, JSON, acknowledgement, allowed roots, URL scheme), unavailable-model fallback, remote-endpoint refusal, full plan → review → execute → capsule → apply workflow, file-serving containment |

## Fakes (`tests/conftest.py`)

- `FakeHttp`: canned PyPI JSON for `package_exists` / `validate_plan`.
- `FakeLLM` (in `test_ai_validation.py`): returns fixed model text, so AI parsing and
  validation are deterministic.
- `fake_exec` fixture: replaces `sandbox.create_env`, `run_tests` and `import_check`, plus
  PyPI metadata. `state["behaviour"]` is a function from the working copy's
  `requirements.txt` text to `{test_id: outcome}`, and `state["install_ok"]` decides install
  success. Everything above the sandbox (pipeline, merge, verification, capsules, API) is the
  real code.

Example: make each exit pass alone but fail together.

```python
fake_exec["behaviour"] = lambda reqs: {
    "tests/test_app.py::t_a": "passed",
    "tests/test_app.py::t_b": "passed" if ("six" in reqs or "idna" in reqs) else "failed",
}
```

## Real end-to-end check

The suite deliberately does not hit the network. To exercise real installs, tests and
Gemma, run the CLI or the UI against LinkWatch (see [getting started](getting-started.md)).
