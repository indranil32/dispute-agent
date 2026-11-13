import shutil

import pytest

from dispute_agent.config import ROOT, Rulebook, Settings
from dispute_agent.llm.heuristic import HeuristicAnalyzer
from dispute_agent.mailbox.local import LocalMailbox
from dispute_agent.orchestrator import DisputeAgent
from dispute_agent.replies import Replies
from dispute_agent.soa.mock import MockSoaClient
from dispute_agent.store import Store


@pytest.fixture
def rules():
    return Rulebook.load(str(ROOT / "config" / "rules.yaml"))


@pytest.fixture
def make_agent(tmp_path, rules):
    def _make(mode="assist", samples=True, soa=None):
        inbox = tmp_path / "inbox"
        if samples:
            shutil.copytree(ROOT / "samples" / "emails", inbox)
        else:
            inbox.mkdir()
        s = Settings()
        s.mode = mode
        mailbox = LocalMailbox(str(inbox), str(tmp_path / "outbox"))
        return DisputeAgent(s, rules, mailbox, HeuristicAnalyzer(rules), soa or MockSoaClient(),
                            Store(str(tmp_path / "db.sqlite")), Replies(str(ROOT / "config" / "templates")))
    return _make


def by_invoice(agent, invoice, amount=None):
    for r in agent.store.list():
        if r.fields.invoice_number.value == invoice and (amount is None or r.fields.disputed_amount.value == amount):
            return r
    raise AssertionError(f"no record for {invoice} {amount}")


def by_conv(agent, conv):
    recs = [r for r in agent.store.list() if r.conversation_id == conv]
    assert recs, conv
    return recs[-1]
