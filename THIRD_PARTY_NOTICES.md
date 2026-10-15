# Third-party notices

depexit is released under the MIT License (see [LICENSE](LICENSE)). It depends on, or
interoperates with, the third-party components below. None of them is vendored into this
repository; each one is installed separately and stays under its own license.

## Runtime dependencies (installed by pip)

| Component | Used for | License |
|---|---|---|
| [packaging](https://github.com/pypa/packaging) | parsing requirements, specifiers and versions | Apache-2.0 or BSD-2-Clause |
| [Flask](https://github.com/pallets/flask) (optional `web` extra) | local web workbench | BSD-3-Clause |

## Tools invoked at run time (not distributed)

| Component | Used for | License |
|---|---|---|
| [uv](https://github.com/astral-sh/uv) | creating virtual environments, installing the analysed project | Apache-2.0 or MIT |
| [pytest](https://github.com/pytest-dev/pytest) | running the analysed project's test suite | MIT |
| [git](https://git-scm.com/) | cloning repositories, `git merge-file`, `git apply` | GPL-2.0 (executed as a separate program, not linked) |
| [Ollama](https://github.com/ollama/ollama) | local model server | MIT |

## AI model

| Component | Used for | Terms |
|---|---|---|
| Gemma 4 (Google DeepMind) | proposals, code migration, behaviour probes, reviews | Gemma weights are released under the license Google publishes with each Gemma release (stated in the README as Apache-2.0 for Gemma 4). Check the license of the exact model you pull. |

depexit does not ship model weights. Every model call is logged under `llm_calls/` in each
run, together with the model identifier the server reported.

## Public data sources

Risk metadata is read from the [PyPI JSON API](https://docs.pypi.org/api/json/) and the
[GitHub REST API](https://docs.github.com/en/rest), subject to their own terms of use.
