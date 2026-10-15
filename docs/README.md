# RepoMedic documentation

RepoMedic's engine and Python package are called `depexit`; the `repomedic` and
`depexit` commands are identical.

depexit finds risky dependencies in a Python repository, plans how to exit each one
(upgrade, replace, stdlib, remove, declare or keep), migrates the code in isolated working
copies, and **proves** the result by running the test suite and behaviour probes against both
the original and the migrated code. Each exit becomes a *Dependency Exit Capsule*.

| Page | What's in it |
|---|---|
| **[Technical approach](technical-approach.md)** | The whole design end to end: pipeline, Gemma integration, verification model, test blindness, replay, security, results on LinkWatch, limitations |
| [Getting started](getting-started.md) | Install, first run on LinkWatch, CLI and web UI |
| [Architecture](architecture.md) | Pipeline stages, modules, data flow |
| [Verification semantics](verification.md) | Requirements, statuses, and why "skipped" never means "passed" |
| [Web workbench](web-ui.md) | Every screen of the UI and what it shows |
| [HTTP API](http-api.md) | Endpoints behind the UI |
| [Capsule format](capsule-format.md) | `capsule.json`, `index.json` and the files in a run directory |
| [Configuration](configuration.md) | CLI options and environment variables |
| [Testing](testing.md) | How the test suite is built and how to extend it |

Also see [SECURITY.md](../SECURITY.md) (execution boundary), [CONTRIBUTING.md](../CONTRIBUTING.md)
and [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
