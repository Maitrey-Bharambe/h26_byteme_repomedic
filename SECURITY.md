# Security policy

## What depexit executes

depexit **runs code from the repository you analyse**: its install hooks (`setup.py`, build
backends), its test suite, and AI-generated behaviour probes. That is how it verifies
migrations, and it is inherently risky with untrusted code.

Current protections:

- each run uses fresh working copies under the run directory; the analysed repository is
  never modified;
- an **allowlisted environment** for repository code: API keys, tokens, `DEPEXIT_LLM_*` and
  similar variables are removed, and HOME, TMP and caches are redirected into the run directory;
- timeouts on every install, test and probe run;
- model-written code is validated before use (paths stay in the repo, it must parse, and it
  may not introduce unplanned third-party imports); probes may not import
  `socket`/`subprocess`/`shutil`/`ctypes`;
- the web server binds to `127.0.0.1`, rejects foreign `Host` headers, accepts JSON-only POSTs,
  confines local targets to `DEPEXIT_ALLOWED_ROOTS`, serves only capsule artifacts, and refuses
  non-local model endpoints unless `DEPEXIT_ALLOW_REMOTE_LLM=1` is set.

**What is not provided: OS-level isolation.** Python virtual environments are not a security
boundary. Repository code can still read your files and reach the network as your user. Only
analyse repositories you trust, or run depexit itself inside a disposable VM or container.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private
vulnerability reporting ("Security" tab → "Report a vulnerability") on this repository, or
contact the maintainers privately. Include:

- the affected version or commit,
- steps to reproduce (a minimal malicious repository is ideal),
- the impact you observed.

We aim to acknowledge reports within 7 days.

## In scope

- escaping the run directory through crafted paths, patches or model output;
- leaking secrets from the depexit process into repository code or into the web UI;
- bypassing the web server's request guards (host check, JSON-only POSTs, allowed roots);
- reports that claim a migration is verified when the evidence does not support it.

Executing malicious code from a repository you chose to analyse is an **expected
limitation** (see above), not a vulnerability, unless it bypasses one of the protections
listed.
