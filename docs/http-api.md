# HTTP API

Served by `depexit serve` on `127.0.0.1`. Guards on every request:

- `Host` must be `localhost`, `127.0.0.1` or `[::1]` (otherwise **403**; override with
  `DEPEXIT_ALLOW_ANY_HOST=1`);
- `POST` bodies must be JSON (otherwise **415**);
- errors are returned as `{"error": "<message>"}`.

| Method & path | Purpose |
|---|---|
| `GET /api/llm` | Real model status: `base_url`, `configured_model`, `local`, `reachable`, `models`, `gemma_models`, `model_available`, `suggested_model`, `unavailable_features`, `fallback`. Never returns credentials. |
| `GET /api/config` | `allowed_roots`, `execution_boundary`, `busy` |
| `GET /api/jobs` | runs in this server process, newest first |
| `POST /api/jobs` | start a run (analyse + plan) |
| `GET /api/jobs/<id>?since=N` | state, stage, events since index *N*, `index.json`, options, error |
| `POST /api/jobs/<id>/execute` | migrate + verify the selected planned dependencies, then combine |
| `GET /api/jobs/<id>/capsules/<dep>` | `{capsule, files}` from `capsules/<dep>/capsule.json` |
| `GET /api/jobs/<id>/files/<path>` | a file under `capsules/`, `combined/`, `llm_calls/` or `applied/`, or `REPORT.md` / `index.json` (`?download=1` to download) |
| `POST /api/jobs/<id>/apply` | apply a patch to a fresh working copy |

## `POST /api/jobs`

```json
{
  "target": "https://github.com/rishrek/LinkWatch",
  "pythons": ["3.12", "3.13"],
  "use_llm": true,
  "model": "gemma4:e4b",
  "include_low": false,
  "max_fix_attempts": 2,
  "jobs": 2,
  "only": null,
  "auto_execute": false,
  "acknowledge_untrusted_execution": true
}
```

- `acknowledge_untrusted_execution` must be `true` (otherwise **400**).
- `target`: an https URL, `git@…`, or a local directory under `DEPEXIT_ALLOWED_ROOTS`.
- `pythons`: 1–3 values like `3.12`.
- If the model endpoint is unreachable, the run starts in deterministic mode and
  `options.llm_note` says why. An unknown model on a reachable endpoint returns **400**.
- Returns **202** with the job view; **409** if another run is executing.

Job `state`: `queued` → `running` → `awaiting_review` → (`execute`) `running` → `done`, or
`error` with `error` set. Stages: `inspect`, `baseline`, `risk`, `plan`, `verify`,
`combine`, `done`.

## `POST /api/jobs/<id>/execute`

`{"deps": ["six", "idna"]}`, a subset of the dependencies whose status is `planned`. Omit
`deps` to execute every planned dependency. Allowed in states `awaiting_review` and `done`.

## `POST /api/jobs/<id>/apply`

`{"which": "<dep>"}` or `{"which": "combined"}`. Copies the analysed repository to
`<run>/applied/<which>/` and runs `git apply`. Returns `status` (`applied`/`failed`),
`working_copy`, `patch`, `output`. The analysed repository is never modified.
