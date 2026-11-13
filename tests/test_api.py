import shutil

from fastapi.testclient import TestClient

from dispute_agent import api
from dispute_agent.config import ROOT


def test_api_flow(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    shutil.copytree(ROOT / "samples" / "emails", inbox)
    for k, v in {"DB_PATH": str(tmp_path / "d.db"), "LOCAL_INBOX_DIR": str(inbox), "LOCAL_OUTBOX_DIR": str(tmp_path / "out"),
                 "AGENT_MODE": "assist", "MAILBOX_BACKEND": "local", "SOA_BACKEND": "mock", "LLM_BACKEND": "heuristic"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("REVIEW_API_KEY", raising=False)
    api._agent = None
    c = TestClient(api.app)

    assert c.post("/graph/notifications?validationToken=abc123").text == "abc123"
    assert c.post("/run-cycle").json()["emails_processed"] == 8
    queue = c.get("/review").json()
    target = next(d for d in queue if d["fields"]["invoice_number"]["value"] == "INV-2026-104233")
    r = c.post(f"/review/{target['id']}/approve", json={"reviewer": "L1"})
    assert r.status_code == 200 and r.json()["status"] == "Created in Fusion"
    detail = c.get(f"/disputes/{target['id']}").json()
    assert detail["audit"] and detail["emails"]
    assert c.get("/metrics").json()["fusion_disputes_created"] == 1
    assert "Dispute review queue" in c.get("/").text
    api._agent = None


def test_api_key_guard(monkeypatch):
    monkeypatch.setenv("REVIEW_API_KEY", "secret")
    c = TestClient(api.app)
    assert c.get("/metrics").status_code == 401
