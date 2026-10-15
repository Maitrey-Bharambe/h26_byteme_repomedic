# Verification semantics

Implemented in `depexit/verification.py`; tested in `tests/test_verification.py` and
`tests/test_pipeline_capsule.py`.

## Requirements are fixed before execution

`requirements_for(plan, pythons, code_changes_expected)` decides what must be shown. The
list is stored in the capsule while the plan is still `planned` (with result `pending`), so
reviewers see the bar before anything runs.

| Id | Level | Applies to | Met when |
|---|---|---|---|
| `install` | required | all | the migrated project installs on every target Python |
| `suite_runs` | required | all | no **new** test-collection error |
| `no_regressions` | required | all | at least one baseline test passed, and every baseline-passing test was re-run and still passes |
| `replacement_imports` | required | upgrade, replace, declare, stdlib | the replacement module imports in the migrated env |
| `version_satisfies` | required | upgrade with a specifier | the installed version satisfies the planned specifier |
| `no_residual_code` | required | remove, replace, stdlib | an AST re-scan of the migrated tree finds no Python use of the old dependency |
| `original_uninstalled` | advisory | remove, replace, stdlib | the old distribution is not installed at all (not even transitively) |
| `no_residual_tooling` | advisory | remove, replace, stdlib | no Makefile/CI/tox reference remains |
| `behaviour_probes` | **required if code is rewritten**, else advisory | all | probes ran on both sides, loaded correctly, and gave identical results |
| `no_new_warnings` | advisory | all | no warnings appear that the baseline did not have |

## Each requirement has three outcomes

- **met**: executed or observed evidence shows it holds;
- **failed**: evidence shows it does not hold;
- **not_checked**: it could not be checked (install failed first, no passing baseline test,
  probes missing or broken, baseline collection already broken).

When several Pythons are verified, any `failed` wins, then any `not_checked`.

## Status

| Status | Rule |
|---|---|
| `failed` | any **required** requirement failed |
| `incomplete` | none failed, but a **required** requirement is `not_checked` |
| `verified_with_gaps` | every required requirement met; some advisory one failed or was not checked |
| `verified` | every requirement met |

Other statuses: `planned` (nothing executed yet), `not_executed` (plan is *keep*),
`tool_error` (the pipeline crashed for that dependency; the exception is recorded).

### Consequences

- **Install success alone never verifies anything.** If the baseline has no passing test,
  `no_regressions` is `not_checked`, so the status is `incomplete`.
- **Disappearing tests are regressions.** A baseline-passing test that does not run after
  the migration is in `vanished_passing` and fails `no_regressions`.
- **Rewritten code needs behaviour evidence.** When code is edited, probes are required.
  Missing or broken probes give `incomplete`; different results give `failed`.
- **Upgrades must actually upgrade.** `version_satisfies` catches resolver back-tracking to
  an old version.

## Fix loop

After each attempt, `is_clean()` asks whether any required check failed or probes
differed. If so, and Gemma is available, the executed evidence (tracebacks, probe diffs,
leftover imports, install errors) goes back to the model, up to `--max-fix-attempts` times.
The capsule keeps every attempt. `incomplete` does not trigger a retry, because there is
nothing concrete to fix.

## Combined result

Only exits that ended `verified` or `verified_with_gaps` are merged (3-way `git merge-file`
for code; manifests re-edited deterministically). Conflicts are re-applied by Gemma on the
combined tree if possible; otherwise that exit is skipped and rolled back.
`evaluate_combined()` then checks install, suite start, no regressions against the
baseline, no residual code for removed or replaced dependencies, new warnings (advisory) and
whether everything merged (advisory):

| Status | Rule |
|---|---|
| `failed` | a required combined check failed, or nothing could be applied |
| `incomplete` | a required check could not be performed |
| `partial` | all required checks met, but at least one verified exit had to be skipped (conflict) |
| `verified` / `verified_with_gaps` | as for individual exits |

Exits excluded because they were not individually verified are listed in
`excluded_unverified`, so the combined result never hides them.

## Evidence quality: test blindness and replay

Passing tests prove little if the tests cannot see the dependency. Two executed checks
measure this, and the final verdict combines them with the requirements above
(`verification.final_verdict`).

### Test blindness (`depexit/blindness.py`, `_helpers/depexit_sabotage.py`)

For each at-risk dependency, the most-used symbols in non-test code (usage kinds
`call` / `attribute` / `reference`) are sabotaged one at a time in the **original**
repository. The original suite runs in the baseline venv of the first target Python:

| Mode | Sabotaged symbol… |
|---|---|
| `raise` | raises `RuntimeError("depexit sabotage")` (classes: their `__init__` raises) |
| `none` | returns `None` |
| `wrong` | returns a wrong value of the real value's type (`"__depexit_wrong__"`, `{}`, `[]`, `-999`, `not bool`, …) |

The pytest plugin patches the symbol before test modules import. It also rebinds names
already copied into repository modules by `from x import y`, counts calls, and flushes its
state on the first call, so a pytest crash *caused* by the sabotage still counts as detection.

| Classification | Meaning |
|---|---|
| `watched` | every mode made at least one baseline-passing test fail |
| `partly_blind` | some modes were detected, others not |
| `blind` | the tests still pass under every mode |
| `not_exercised` | no test ever called the sabotaged symbols (call count 0) |
| `not_run` | the check could not run (reason recorded, e.g. the library cannot even be imported); never treated as watched |
| `not_applicable` | the dependency is not used by repository code (nothing to sabotage) |

### Replay of real recorded calls (`depexit/replay.py`, `_helpers/depexit_record.py`, `_helpers/replay_runner.py`)

1. **Targets**: the repository's own functions that contain a usage site
   (`usage.replay_targets`). Their names survive a migration. Skipped and listed: functions
   that do network I/O directly or through repo-local calls (`side_effect_skipped`), and
   async, generator, decorated or nested functions.
2. **Record**: one extra run of the original suite wraps those functions and stores every
   call (up to 200 per function, identical inputs de-duplicated) with its pickled inputs,
   normalised output or exception type, and the test that triggered it. Unpicklable inputs
   or outputs are kept as `not_replayable`.
3. **Replay**: `replay_runner.py` calls each function with the same inputs, first in the
   original env (control), then in the migrated env (first target Python). If the control
   replay disagrees with the recording, the call depended on test context (fixtures,
   monkeypatching), so it is `not_replayable` rather than compared.
4. **Compare** (`compare.compare_replay`): outputs are normalised (repo, venv, home and temp
   paths replaced, memory addresses dropped, keys sorted). Each call is `identical`,
   `different`, `exception_changed` (exceptions match by type) or `not_replayable`.
   Differences feed the fix loop (`compare.failure_feedback`) and the required
   `replay_identical` requirement.
5. Functions with fewer than 3 recorded calls list the Gemma behaviour probes as
   **ai_generated** extra inputs, reported separately from recorded evidence.

### GhostBuster: is the retired dependency really gone? (advisory)

`depexit/ghostbuster.py` and `_helpers/depexit_ghostbuster.py` run after verification, for
`remove`, `replace` and `stdlib` exits only:

| Part | Provenance | What it finds |
|---|---|---|
| Zombie build commands | observed | console scripts / `python -m <pkg>` of the retired distribution in Makefile, tox.ini, Dockerfile, noxfile, setup.cfg, Justfile, pre-commit, `.github/workflows/*.y*ml` and `pyproject.toml` (comments ignored; exact name match, so `nose2` is not `nose`). A known replacement (`nosetests` → `python -m pytest`) is suggested as **rule_based**; it is never applied automatically. |
| Runtime ghost imports | executed | one extra run of the migrated suite with an observer on `builtins.__import__` and `importlib.import_module`, recording each import of the retired root with caller file:line, triggering test and origin (`repository` / `third_party` / `unknown`). Only in the child process; restored at session end. |

Statuses: `observed_ghost`, `not_observed`, `not_run`, `error`. A failed check is never
reported as `not_observed`, and `not_observed` covers only the executed tests and supported
files. GhostBuster never changes the migration verdict.

### Final verdict

| Verdict | When |
|---|---|
| `failed` | install failure, test regression, another required check failed, or **any replay difference** |
| `incomplete` | blindness `not_run`, replay `not_run`, most recorded calls not replayable, or a required check not performed |
| `verified` / `verified_with_gaps` | tests pass and the dependency is **watched** (replay identical where recorded); or it is not used by code |
| `verified_by_replay` | tests pass but are partly blind, blind or not exercising it, **and** recorded real calls replay identically |
| `weakly_verified` | tests pass, are blind or not exercising it, and no recorded real call could be replayed (only AI probes, or nothing) |

The reason for every verdict is in `status_reason`, `CAPSULE.md`, `REPORT.md` and the UI.
