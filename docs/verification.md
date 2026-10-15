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
