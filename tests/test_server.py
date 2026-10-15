"""HTTP API integration: the real Pipeline behind Flask, with the scripted execution layer."""
import time

import pytest

from depexit import server

pytest.importorskip("flask")
A, B = "tests/test_app.py::t_a", "tests/test_app.py::t_b"


@pytest.fixture
def client(tmp_path, monkeypatch, small_repo, fake_exec):
    monkeypatch.setenv("DEPEXIT_ALLOWED_ROOTS", str(small_repo.parent))
    monkeypatch.setenv("DEPEXIT_LLM_BASE_URL", "http://127.0.0.1:9/v1")   # closed port: no model
    app = server.create_app(tmp_path / "runs")
    return app.test_client()


def wait(client, jid, states=("awaiting_review", "done", "error"), timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        v = client.get(f"/api/jobs/{jid}").get_json()
        if v["state"] in states:
            return v
        time.sleep(0.05)
    raise AssertionError(f"run stuck in {v['state']}")


def start(client, small_repo, **kw):
    body = {"target": str(small_repo), "pythons": ["3.12"], "use_llm": True,
            "acknowledge_untrusted_execution": True, **kw}
    return client.post("/api/jobs", json=body)


def test_llm_unavailable_is_reported_with_fallback(client):
    s = client.get("/api/llm").get_json()
    assert s["reachable"] is False and s["local"] is True
    assert "code rewriting" in s["unavailable_features"]
    assert "Deterministic analysis still runs" in s["fallback"]
    assert "api_key" not in str(s).lower()


def test_remote_llm_endpoint_is_refused(monkeypatch):
    monkeypatch.setenv("DEPEXIT_LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.delenv("DEPEXIT_ALLOW_REMOTE_LLM", raising=False)
    s = server.llm_status()
    assert s["local"] is False and s["reachable"] is False and "disabled" in s["detail"]


def test_request_guards(client, small_repo, tmp_path):
    assert client.get("/api/llm", headers={"Host": "evil.example:8765"}).status_code == 403
    assert client.post("/api/jobs", data="target=x").status_code == 415
    r = start(client, small_repo, acknowledge_untrusted_execution=False)
    assert r.status_code == 400 and "acknowledge" in r.get_json()["error"]
    outside = tmp_path.parent
    assert start(client, outside).status_code == 400
    assert start(client, "file:///etc/passwd").status_code == 400
    assert start(client, small_repo, pythons=["3.12; rm -rf /"]).status_code == 400


def test_full_workflow_plan_review_execute_capsule_apply(client, small_repo, fake_exec):
    fake_exec["behaviour"] = lambda reqs: {A: "passed",
                                           B: "passed" if ("six" in reqs or "idna" in reqs) else "failed"}
    r = start(client, small_repo)
    assert r.status_code == 202
    jid = r.get_json()["id"]
    v = wait(client, jid)
    assert v["state"] == "awaiting_review", v["error"]
    assert v["options"]["use_llm"] is False                     # model unreachable -> deterministic
    deps = {d["name"]: d for d in v["index"]["dependencies"]}
    assert deps["six"]["status"] == "planned"
    assert v["index"]["ai"]["reachable"] is False
    stages = {e["stage"] for e in client.get(f"/api/jobs/{jid}").get_json()["events"]}
    assert {"inspect", "baseline", "risk", "plan"} <= stages

    cap = client.get(f"/api/jobs/{jid}/capsules/six").get_json()
    assert cap["capsule"]["status"] == "planned"
    assert "capsules/six/capsule.json" in cap["files"]

    # executing something that is not planned is refused
    assert client.post(f"/api/jobs/{jid}/execute", json={"deps": ["requests"]}).status_code == 400
    assert client.post(f"/api/jobs/{jid}/execute", json={"deps": ["six", "idna"]}).status_code == 202
    v = wait(client, jid, ("done", "error"))
    assert v["state"] == "done", v["error"]
    statuses = {d["name"]: d["status"] for d in v["index"]["dependencies"] if d["status"]}
    assert statuses["six"] in ("verified", "verified_with_gaps")
    assert v["index"]["combined"]["status"] == "failed"        # individual pass, combined fails

    # artifacts come straight from the capsule directory
    patch = client.get(f"/api/jobs/{jid}/files/capsules/six/migration.patch")
    assert patch.status_code == 200 and b"-six==1.16.0" in patch.data
    assert client.get(f"/api/jobs/{jid}/files/capsules/six/CAPSULE.md").status_code == 200
    assert client.get(f"/api/jobs/{jid}/files/.work/source/requirements.txt").status_code == 404
    assert client.get(f"/api/jobs/{jid}/files/capsules/../../../secret").status_code == 404

    # apply goes to a fresh working copy; the analysed repo is untouched
    before = (small_repo / "requirements.txt").read_text()
    a = client.post(f"/api/jobs/{jid}/apply", json={"which": "six"}).get_json()
    assert a["status"] == "applied", a["output"]
    from pathlib import Path
    assert "six" not in Path(a["working_copy"], "requirements.txt").read_text()
    assert (small_repo / "requirements.txt").read_text() == before
    assert client.post(f"/api/jobs/{jid}/apply", json={"which": "../x"}).status_code == 400


def test_unknown_job_and_capsule(client):
    assert client.get("/api/jobs/nope").status_code == 404
