# Configuration

## CLI

```
depexit analyze <target> [-o OUT] [-p 3.12 -p 3.13] [--only NAME]... [--no-llm]
                         [--model M] [--base-url URL] [--jobs N] [--max-fix-attempts N] [--include-low]
depexit serve [--host 127.0.0.1] [--port 8765] [--runs depexit-runs]
```

| Option | Default | Meaning |
|---|---|---|
| `target` | – | https/ssh git URL or local directory |
| `-o/--out` | `depexit-out` | output directory |
| `-p/--python` | 3.12, 3.13 | target Pythons (repeatable) |
| `--only` | all at-risk | restrict to these dependencies |
| `--no-llm` | off | deterministic mode |
| `--model`, `--base-url` | env / defaults | model endpoint |
| `--jobs` | 4 | dependencies processed in parallel (use 1–2 with one local GPU) |
| `--max-fix-attempts` | 2 | Gemma fix-loop iterations |
| `--include-low` | off | also build capsules for low and info risks |

Exit codes: `0` success, `2` invalid target or clone failure (message on stderr).

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DEPEXIT_LLM_BASE_URL` | `http://localhost:11434/v1` | model endpoint (an Ollama server is detected and driven via `/api/chat`) |
| `DEPEXIT_LLM_MODEL` | `gemma4:26b` | model name (the UI suggests an installed Gemma model if this one is not pulled) |
| `DEPEXIT_LLM_API_KEY` | – | bearer token for non-Ollama endpoints (CLI only; the UI refuses remote endpoints) |
| `DEPEXIT_LLM_NUM_CTX` | `8192` | Ollama context window; a prompt that fills it is reported as an error |
| `DEPEXIT_LLM_TIMEOUT` | `900` | seconds per model call |
| `DEPEXIT_ALLOWED_ROOTS` | server cwd | `os.pathsep`-separated directories that local targets must be inside (UI only) |
| `DEPEXIT_ALLOW_REMOTE_LLM` | – | `1` lets the UI use a non-local model endpoint |
| `DEPEXIT_ALLOW_ANY_HOST` | – | `1` disables the UI's `Host` header check (not recommended) |

Repository code never sees these variables. It runs with the allowlisted environment
defined in `depexit/util.py` (`SAFE_ENV_KEYS`).
