"""Prompts for Gemma 4. Each step asks for a narrow, checkable output."""
from __future__ import annotations

import json

SYSTEM = (
    "You are a senior Python maintainer helping migrate a repository away from a risky "
    "dependency. You are precise and conservative. Only use APIs that really exist in the "
    "stated package versions. Never invent package names. If unsure, say so in the 'risks' field."
)


def _facts(dep, pythons) -> str:
    usage = [{k: u[k] for k in ("symbol", "kind", "file", "line", "code", "private")}
             for u in dep.usage[:60]]
    return json.dumps({
        "dependency": dep.name,
        "declared": [{k: d[k] for k in ("file", "line", "raw", "group", "commented")}
                     for d in dep.declared],
        "installed_versions": dep.installed,
        "target_pythons": pythons,
        "pypi": {k: dep.pypi.get(k) for k in ("summary", "latest_version", "latest_compatible",
                                               "latest_release_date", "classifiers_inactive")},
        "risk_indicators": [{k: i[k] for k in ("id", "severity", "detail")} for i in dep.indicators],
        "usage_sites": usage,
    }, indent=1)


def proposal(dep, pythons, file_texts: dict[str, str]) -> str:
    files = "\n\n".join(f"--- {p} ---\n{t}" for p, t in file_texts.items())
    return f"""Analyse this at-risk dependency and propose how the repository should exit or fix it.

OBSERVED FACTS (from repository inspection, public metadata and test execution):
{_facts(dep, pythons)}

SOURCE OF FILES THAT USE IT:
{files or '(no source files reference it)'}

Choose ONE action:
- "upgrade": keep the package, move to a newer version range
- "replace": switch to a different PyPI package
- "stdlib": replace with a Python standard-library module available on ALL target Pythons
- "remove": the package is not needed
- "declare": the package is used but undeclared; declare it explicitly
- "keep": no safe change is possible now (explain why)

Return ONLY a JSON object with exactly these keys:
{{
  "action": "upgrade|replace|stdlib|remove|declare|keep",
  "package": "<PyPI name to install, or null for stdlib/remove/keep>",
  "specifier": "<PEP 440 specifier such as >=4.2,<5 or empty string>",
  "stdlib_module": "<module name if action is stdlib, else null>",
  "needs_code_changes": true,
  "rationale": "<2-4 sentences grounded in the facts above>",
  "migration_steps": ["<ordered, concrete steps>"],
  "api_mapping": [{{"old": "<old symbol>", "new": "<new symbol or code>", "note": "<behaviour difference>"}}],
  "risks": ["<what could behave differently after the change>"]
}}"""


def migration(dep, plan: dict, file_texts: dict[str, str], feedback: str | None) -> str:
    files = "\n\n".join(f"### FILE: {p}\n```python\n{t}\n```" for p, t in file_texts.items())
    fb = f"\nTHE PREVIOUS ATTEMPT FAILED VERIFICATION. Fix these observed failures:\n{feedback}\n" if feedback else ""
    return f"""Apply this dependency migration to the repository's code.

Dependency: {dep.name}
Plan (JSON): {json.dumps({k: plan.get(k) for k in ('action', 'package', 'specifier', 'stdlib_module', 'migration_steps', 'api_mapping')}, indent=1)}
Dependency manifests are edited separately by the tool; change ONLY Python code.
{fb}
Rules:
- Preserve existing behaviour exactly (same return values, same error handling, same public names).
- Keep changes minimal. Do not reformat unrelated code. Do not add new third-party dependencies.
- Output every file you change, in full, using exactly this format and nothing else:

### FILE: <relative/path.py>
```python
<complete new file content>
```

FILES THAT USE {dep.name}:
{files}"""


def probes(dep, plan: dict, file_texts: dict[str, str], usage_files: list[str]) -> str:
    files = "\n\n".join(f"--- {p} ---\n{t}" for p, t in file_texts.items())
    return f"""Write behavioural probes that will be run against BOTH the original repository
(with {dep.name}) and the migrated repository (with the replacement). Any difference in
their results reveals a compatibility gap that the existing test suite may miss.

Planned change: {json.dumps({k: plan.get(k) for k in ('action', 'package', 'stdlib_module', 'api_mapping', 'risks')})}

Rules:
- Exercise the REPOSITORY'S OWN functions/classes in these modules (not the library directly): {', '.join(usage_files)}
- Import repository modules by their package path (the repo root is on sys.path).
- Focus on edge cases where old and new behaviour could differ: whitespace, empty input,
  invalid input, encodings, unusual formats, timezones, paths, error types.
- NO network access, no sockets, no sleeping, no writing outside a temp directory.
- Each probe is a zero-argument callable returning a JSON-friendly value (str/int/bool/list/dict/None).
  Return normalised values (e.g. str(path) or path.name, isoformat strings), not objects.
- Exceptions are fine: the runner records their type, which is also compared.
- 6 to 15 probes. Define a module-level dict named PROBES mapping a short name to the callable.

Return ONLY one python code block.

SOURCE:
{files}"""


def review(dep, plan: dict, comparison: dict) -> str:
    return f"""Review the EXECUTED verification results of a dependency migration and list what a
maintainer must still worry about. Do not repeat facts; interpret them.

Dependency: {dep.name}
Plan: {json.dumps({k: plan.get(k) for k in ('action', 'package', 'specifier', 'stdlib_module')})}
Executed comparison (original vs replacement): {json.dumps(comparison, default=str)[:12000]}

Return ONLY JSON:
{{
  "summary": "<2 sentences>",
  "gap_explanations": [{{"gap": "<which observed difference>", "likely_cause": "<why>", "suggested_fix": "<what to do>"}}],
  "open_concerns": ["<risks not covered by the executed tests>"],
  "confidence": "high|medium|low"
}}"""
