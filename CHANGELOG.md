# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Test blindness check**: sabotages each used dependency symbol (raise / none / wrong) in the original suite and classifies the dependency as watched, partly blind, blind, not exercised, not run or not applicable, listing the tests that caught it.
- **Replay check**: records real calls of the repository's own functions during the original tests and replays them on the migrated code (identical / different / exception changed / not replayable). Network-touching functions are skipped.
- New verdicts `verified_by_replay` and `weakly_verified`, a *Test visibility* column, a replay table in the UI, and *Test visibility* and *Replay* columns in REPORT.md.
- Beige and brown UI palette (light and dark); earlier runs on disk are viewable after a server restart.
- **Web workbench** (`depexit serve`, Flask + vanilla HTML/CSS/JS): repository analysis with
  live pipeline events, summary tiles, a migration graph, a risk explorer, review of the
  plan before execution, migrate-and-verify for selected dependencies, a combined-result
  view, capsule artifact downloads, and patch application to a fresh working copy.
- **Staged pipeline API**: `analyze()` → `plan_all()` → `run_exits()` → `finalize()`, with
  progress events. `execute()` keeps the CLI behaviour.
- **Verification requirements** (`depexit/verification.py`): explicit per-exit requirements,
  each evaluated to `met`, `failed` or `not_checked`. New statuses: `incomplete` (a required
  check was not performed), `planned`, and `partial` for combined results.
- **Combined-migration verification**: status, residual-usage re-scan, and a list of exits
  not combined because they were not verified individually.
- **API-mapping review**: the model's mapping is cross-checked against observed usage
  sites, and unmapped symbols are reported as uncertainties.
- Stricter validation of model-written edits: only files given to the model, no new
  unplanned third-party imports, and an error when no `### FILE:` block is returned.
- Ollama native API support with an explicit context window (`DEPEXIT_LLM_NUM_CTX`) and
  detection of prompt truncation.
- Execution boundary: an allowlisted environment for repository code, HOME and TMP
  redirection, target validation, and documentation of the lack of OS-level isolation.
- Test suite (59 tests), CI workflow, docs, and community and security files.

### Changed
- A baseline-passing test that no longer runs after a migration counts as a regression.
- Advisory checks on model output (mapping coverage, step format) no longer reject a plan;
  they are recorded as uncertainties.
- PyPI and GitHub metadata fetches retry transient network errors.

### Fixed
- `git clone` refuses option-like targets and non-https URLs.
- Subprocess output is decoded as UTF-8 with replacement (Windows code-page errors), and
  the timeout handling that could return bytes is fixed.

## [0.1.0]
- Initial release: CLI analysis, Gemma-assisted proposals and migrations, execution-based
  verification, and Dependency Exit Capsules.
