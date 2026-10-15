# Capsule format

## Run directory

```
<out>/
  REPORT.md                 human summary: baseline, dependencies, combined result, drift
  index.json                schema dependency-exit-index/1.0
  combined/all-migrations.patch
  capsules/<dep>/
    capsule.json            schema dependency-exit-capsule/1.0
    CAPSULE.md
    migration.patch         manifests + code, unified diff against the analysed repo
    probes.py, probe_runner.py
    logs/original-py<V>.log, logs/replacement-py<V>.log, logs/install-py<V>.log
    replay.sh               re-run original vs replacement from scratch
  llm_calls/NNN_<purpose>.json   prompt, response, model, prompt SHA-256, latency, token usage
  applied/<dep|combined>/   (web UI) patched working copies
  .work/                    clones, venvs, attempts (not served by the UI)
  .cache/http/              PyPI/GitHub responses (6 h)
```

## `capsule.json` (main fields)

| Field | Provenance | Content |
|---|---|---|
| `status`, `status_reason` | – | see [verification](verification.md) |
| `target`, `ai`, `execution_boundary`, `tool`, `generated_at` | – | run context |
| `dependency` | observed | name, import names, declarations, installed versions, PyPI, GitHub |
| `risk_indicators` | observed + executed | `score`, `max_severity`, `items[]` with `evidence {type, source, …}` |
| `usage_locations` | observed | `files`, `count`, `sites[]` (`module, symbol, kind, file, line, code, private, alias`) |
| `proposed_replacement` | ai_generated or rule_based | `action, package, specifier, stdlib_module, rationale, needs_code_changes, api_mapping[], api_mapping_review, model_calls[], rejected_ai_plan` |
| `migration_steps` | as plan | steps, files changed, `patch` |
| `validation_of_ai_output.checks[]` | observed | `{check, ok, blocking, detail, source}` |
| `verification` | executed (or rule_based while planned) | `{status, requirements[]}`; each requirement has `{id, required, description, result, evidence[], provenance}` |
| `suggested_tests` | observed / ai_generated | the existing suite command; the probes file |
| `executed_test_results.per_python` | executed | install, versions, test deltas (`fixed`, `regressed`, `still_failing`, collection errors), warnings, replacement import, probe summary and raw results, log path |
| `executed_test_results.attempts[]` | executed | the fix-loop history |
| `compatibility_gaps[]` | executed / observed | `{kind, detail, pythons[], provenance}`; kinds include `install-failed`, `test-suite-broken`, `test-regression`, `test-not-run`, `still-failing`, `behaviour-difference`, `new-warning`, `still-installed-transitively`, `leftover-code-reference`, `leftover-tooling-reference` |
| `ai_review` | ai_generated | summary, gap explanations, open concerns, confidence |
| `open_concerns_and_limitations[]` | mixed | `{provenance, text}`; includes plan uncertainties |

## `api_mapping_review`

```json
{
  "provenance": "observed",
  "entries": [{"old": "appdirs.user_cache_dir", "new": "platformdirs.user_cache_dir",
               "valid_shape": true, "references_dependency": true, "new_still_uses_old": false,
               "observed_sites": ["linkwatch/cache.py:4", "linkwatch/cache.py:10"]}],
  "unmapped_symbols": [],
  "observed_symbol_count": 1,
  "mapped_symbol_count": 1
}
```

## `index.json`

Run metadata (target, AI status, execution boundary, baseline per Python, manifest drift,
parse errors, seconds), plus:

- `dependencies[]`: `name, at_risk, score, max_severity, indicators[], status,
  status_reason, action, action_target, capsule`
- `combined`: `status, verification, dependencies[], merged_edits[], conflicts[],
  rebased_by_ai[], skipped[], excluded_unverified[], comparison{py: …}, residual{dep: […]}`

## Reuse

```bash
git apply capsules/<dep>/migration.patch
./capsules/<dep>/replay.sh /path/to/repo 3.12     # POSIX shell; uses uv
```
