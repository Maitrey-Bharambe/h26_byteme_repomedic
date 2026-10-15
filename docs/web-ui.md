# Web workbench

```bash
pip install -e ".[web]"
depexit serve [--host 127.0.0.1] [--port 8765] [--runs depexit-runs]
```

The UI is plain HTML, CSS and JavaScript (`depexit/web/`) served by Flask
(`depexit/server.py`). It renders only the backend's `index.json` and `capsule.json`, so
it cannot contradict the capsule files on disk. Light and dark themes follow the OS; the ◐
button overrides it.

## Header: model status
The pill shows the result of a real `GET <base_url>/models` on the configured local
endpoint. If the model is unreachable or not pulled, the form unticks *use model* and lists
the AI features that will be unavailable. The run then proceeds deterministically.

## 1 · Analyse a repository
- **Target**: an https git URL, `git@…`, or a local folder inside `DEPEXIT_ALLOWED_ROOTS`.
  **Use LinkWatch** fills the organizer repository.
- **Target Pythons**, **Gemma model** (from the endpoint's own model list), **low-risk too**,
  **fix attempts**.
- **Acknowledgement** (required): repository code will execute without OS-level isolation.

**Analyse & plan** runs *inspect → baseline → risk → plan* and stops. Nothing is modified.

## 2 · Run
- An eight-step progress bar driven by the pipeline's real stage.
- **Tiles**: dependencies, at risk, planned, verified, failed, incomplete, baseline pass
  rate, and the combined status.
- **Baseline** table per Python, plus the **execution boundary** statement.
- **Live pipeline events**: the same messages the CLI prints, with timestamps.

## 3 · Migration graph
Four columns: **dependency** (severity accent and risk score) → **strategy** (action icon and
target) → **individual verification** (status and the first unmet requirement) →
**combined** (status, merged and conflicted counts, per-Python pass and regression counts).

| Edge | Meaning |
|---|---|
| dashed blue | planned, awaiting approval |
| solid, coloured by status | executed |
| solid into *Combined* | this exit was merged into the combined tree |
| dashed red into *Combined* | verified individually but skipped because of a merge conflict |
| none into *Combined* | not verified individually, so not combined |

Click any node to open its capsule.

## 4 · Risk explorer & migration workbench
One row per dependency with indicators: a severity bar and score, indicator tags, the
planned action, and the status. Tick **planned** rows and click **Migrate & verify**. Each
exit runs in its own working copy, followed by the combined re-verification.

## 5 · Combined migration
Status, the requirements table, per-Python tests with all exits applied, merge conflicts,
Gemma re-applications, exclusions, and the `all-migrations.patch` link. **Apply combined
patch to a fresh working copy** creates `<run>/applied/combined/`.

## Capsule drawer
| Tab | Content |
|---|---|
| Overview | status with reason, proposed exit and rationale, declarations, installed and latest versions, PyPI/GitHub metadata |
| Usage | every usage site (file:line, kind, symbol, code), with private-API flags |
| Risk | indicators with severity, detail, and evidence type and source link |
| Plan | strategy, steps, **API mapping cross-checked against observed sites** (unmapped symbols highlighted), validation of the model output (blocking vs advisory), and **verification requirements** |
| Verification | the evaluated requirements, then per Python: install commands, versions, test deltas, import check, new warnings, probe results and differences, logs, and fix-loop attempts |
| Gaps | compatibility gaps, AI interpretation (labelled), open concerns and uncertainties |
| Proof | every capsule file, model-call logs with prompt hashes, the patch viewer, and **Apply to a fresh working copy** |

Provenance badges: **observed** (repo or metadata), **executed** (ran it),
**AI · Gemma** (validated model output), **rule** (deterministic fallback).

## Limits
- One execution at a time (runs are CPU and GPU heavy); a second start returns HTTP 409.
- Runs live in server memory: after a restart, earlier run directories remain on disk but
  can no longer be executed from the UI.
