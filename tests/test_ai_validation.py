"""Migration-plan validation, API mapping review and edit validation with mocked model output."""
from depexit import ai
from depexit.llm import LLMError, extract_json

from conftest import FakeHttp, dep_facts

PY = ["3.12", "3.13"]
STDLIB = {"3.12": {"urllib", "json", "importlib"}, "3.13": {"urllib", "json", "importlib"}}
HTTP = FakeHttp({"requests": {"2.31.0": ">=3.7", "2.32.3": ">=3.8"},
                 "httpx": {"0.27.0": ">=3.8"}, "oldlib": {"1.0": "<3.0"}})


def plan(**kw):
    base = {"action": "upgrade", "package": "requests", "specifier": ">=2.32,<3", "stdlib_module": None,
            "needs_code_changes": False, "api_mapping": [], "risks": [], "rationale": "x",
            "migration_steps": ["bump"]}
    base.update(kw)
    return base


def test_valid_upgrade_is_accepted():
    checks = ai.validate_plan(plan(), dep_facts(), HTTP, PY, STDLIB)
    assert ai.plan_accepted(checks)


def test_hallucinated_package_is_rejected():
    checks = ai.validate_plan(plan(action="replace", package="requests-ng"), dep_facts(), HTTP, PY, STDLIB)
    assert not ai.plan_accepted(checks)
    assert any("exists on PyPI" in c["check"] and not c["ok"] for c in checks)


def test_version_incompatible_with_target_python_is_rejected():
    checks = ai.validate_plan(plan(package="oldlib", specifier=">=1.0"), dep_facts(), HTTP, PY, STDLIB)
    assert not ai.plan_accepted(checks)


def test_unknown_action_and_missing_stdlib_module_rejected():
    assert not ai.plan_accepted(ai.validate_plan(plan(action="rewrite"), dep_facts(), HTTP, PY, STDLIB))
    checks = ai.validate_plan(plan(action="stdlib", package=None, stdlib_module="httplib2",
                                   api_mapping=[{"old": "requests.get", "new": "httplib2.get"}]),
                              dep_facts(), HTTP, PY, STDLIB)
    assert not ai.plan_accepted(checks)


def test_replace_with_itself_rejected():
    checks = ai.validate_plan(plan(action="replace", package="requests", specifier=">=2.32",
                                   api_mapping=[{"old": "requests.get", "new": "x"}]),
                              dep_facts(), HTTP, PY, STDLIB)
    assert any(c["check"] == "replacement differs from original" and not c["ok"] for c in checks)
    assert not ai.plan_accepted(checks)


def test_api_mapping_partial_coverage_is_advisory_not_blocking():
    p = plan(action="replace", package="httpx", specifier=">=0.27",
             api_mapping=[{"old": "requests.get", "new": "httpx.get", "note": "same"}])
    checks = ai.validate_plan(p, dep_facts(), HTTP, PY, STDLIB)
    cov = next(c for c in checks if c["check"].startswith("API mapping covers"))
    assert cov["ok"] is False and cov["blocking"] is False
    assert "requests.Session" in cov["detail"]
    assert ai.plan_accepted(checks)


def test_api_mapping_that_still_uses_old_dependency_is_blocking():
    p = plan(action="replace", package="httpx", specifier=">=0.27",
             api_mapping=[{"old": "requests.get", "new": "requests.get"}])
    assert not ai.plan_accepted(ai.validate_plan(p, dep_facts(), HTTP, PY, STDLIB))


def test_api_mapping_required_for_replace_when_code_uses_dependency():
    p = plan(action="stdlib", package=None, stdlib_module="urllib.request", api_mapping=[])
    assert not ai.plan_accepted(ai.validate_plan(p, dep_facts(), HTTP, PY, STDLIB))


def test_review_api_mapping_reports_sites_and_unmapped_symbols():
    p = plan(action="stdlib", stdlib_module="urllib.request",
             api_mapping=[{"old": "requests.get(url)", "new": "urllib.request.urlopen(url)"},
                          {"old": "Session", "new": "urllib.request.build_opener()"},
                          {"old": "requests.Timeout", "new": "TimeoutError"}])
    rv = ai.review_api_mapping(p, dep_facts())
    by = {e["old"]: e for e in rv["entries"]}
    assert by["requests.get(url)"]["observed_sites"] == ["app/net.py:12"]
    assert by["Session"]["observed_sites"] == ["app/net.py:10"]
    assert by["requests.Timeout"]["observed_sites"] == []          # mapped but never used
    assert by["requests.Timeout"]["references_dependency"] is True  # still a requests symbol
    assert rv["unmapped_symbols"] == [] and rv["mapped_symbol_count"] == 2
    assert rv["observed_symbol_count"] == 2                         # bare `import requests` not counted


def test_rule_based_plans():
    unused = dep_facts(usage=[])
    unused.indicators = [{"id": "unused", "severity": "medium", "evidence": {"required_by": ["x"]}}]
    assert ai.rule_based_plan(unused)["action"] == "remove"
    outdated = dep_facts()
    outdated.indicators = [{"id": "stale", "severity": "medium", "evidence": {}}]
    p = ai.rule_based_plan(outdated)
    assert p["action"] == "upgrade" and p["specifier"] == ">=2.0,<3"


# ------------------------------------------------------------------ model-written edits

def test_validate_edits_rejects_unsafe_or_unplanned_output(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("import requests\n")
    (tmp_path / "pkg" / "b.py").write_text("x = 1\n")
    edits = {
        "pkg/a.py": "import urllib.request\n",           # fine: stdlib
        "../evil.py": "print(1)\n",                       # traversal
        "pkg/b.py": "def f(:\n",                          # syntax error (and not given)
        "pkg/new.py": "x = 1\n",                          # not an existing file
    }
    good, checks = ai.validate_edits(edits, tmp_path, allowed_files={"pkg/a.py", "pkg/new.py"},
                                     allowed_new_imports={"urllib", "json"})
    assert set(good) == {"pkg/a.py"}
    failed = {c["check"] for c in checks if not c["ok"]}
    assert "../evil.py stays inside repo" in failed
    assert "pkg/b.py was one of the files given to the model" in failed
    assert "pkg/new.py is an existing repository file" in failed


def test_validate_edits_blocks_new_third_party_import(tmp_path):
    (tmp_path / "a.py").write_text("import requests\n")
    good, checks = ai.validate_edits({"a.py": "import httpx\nimport aiohttp\n"}, tmp_path, {"a.py"},
                                     allowed_new_imports={"httpx"})
    assert good == {}
    c = next(c for c in checks if "unplanned" in c["check"])
    assert c["detail"] == ["aiohttp"]


class FakeLLM:
    """Deterministic stand-in for llm.LLM."""

    def __init__(self, reply):
        self.reply = reply

    def chat(self, prompt, purpose, system=None, expect_json=True):
        meta = {"model": "fake-gemma", "prompt_sha256": "0" * 64, "log": f"llm_calls/{purpose}.json"}
        if not expect_json:
            return {"text": self.reply, "_meta": meta}
        data = extract_json(self.reply)
        if data is None:
            raise LLMError("no json")
        return {**data, "_meta": meta}


def test_ai_plan_normalises_model_json():
    reply = 'Sure!\n```json\n{"action": "replace", "package": " httpx ", "specifier": " >=0.27 ",' \
            ' "api_mapping": {"bad": 1}, "migration_steps": "do it"}\n```'
    p = ai.ai_plan(FakeLLM(reply), dep_facts(), PY, {})
    assert p["package"] == "httpx" and p["specifier"] == ">=0.27"
    assert p["api_mapping"] == [] and p["migration_steps"] == []     # wrong types coerced to empty lists
    assert p["needs_code_changes"] is True


def test_ai_migration_parses_file_blocks_and_flags_empty_reply(tmp_path):
    (tmp_path / "a.py").write_text("import requests\n")
    reply = "### FILE: a.py\n```python\nimport urllib.request\n```\n"
    good, checks, meta = ai.ai_migration(FakeLLM(reply), dep_facts(), plan(action="stdlib"),
                                         {"a.py": "import requests\n"}, tmp_path,
                                         allowed_new_imports={"urllib"})
    assert good == {"a.py": "import urllib.request\n"} and meta["model"] == "fake-gemma"
    good, checks, _ = ai.ai_migration(FakeLLM("I cannot do that."), dep_facts(), plan(),
                                      {"a.py": ""}, tmp_path)
    assert good == {} and any(not c["ok"] and "FILE" in c["check"] for c in checks)


def test_extract_json_handles_prose_and_nested_braces():
    assert extract_json('noise {"a": {"b": "}"}} trailing') == {"a": {"b": "}"}}
    assert extract_json("no json here") is None
