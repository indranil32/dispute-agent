import shutil
from datetime import timedelta

from conftest import by_conv, by_invoice
from dispute_agent.config import ROOT
from dispute_agent.models import DisputeStatus
from dispute_agent.soa.base import SoaTechnicalError
from dispute_agent.soa.mock import MockSoaClient


def test_assist_end_to_end(make_agent):
    a = make_agent("assist")
    a.run_cycle()
    rec = by_invoice(a, "INV-2026-104233")
    assert rec.status == DisputeStatus.IN_REVIEW
    rec = a.approve(rec.id, "L1")
    assert rec.status == DisputeStatus.CREATED and rec.fusion_dispute_id
    a.soa.finance_decide(rec.fusion_dispute_id, True, "Credit note issued")
    a.run_cycle()
    rec = a.store.get(rec.id)
    assert rec.status == DisputeStatus.APPROVED and rec.credit_memo_number
    subjects = [m["subject"] for m in a.mailbox.sent if m["conversation_id"] == rec.conversation_id]
    assert any("registered" in s for s in subjects) and any("approved" in s for s in subjects)


def test_each_email_processed_once(make_agent):
    a = make_agent("assist")
    a.run_cycle()
    n = len(a.store.list())
    a.run_cycle()
    assert len(a.store.list()) == n == 8


def test_scenarios_route_correctly(make_agent):
    a = make_agent("autonomous")
    a.run_cycle()
    assert by_conv(a, "conv-001").status == DisputeStatus.CREATED          # clean -> straight through
    assert by_conv(a, "conv-002").status == DisputeStatus.AWAITING_CUSTOMER  # missing invoice
    assert by_conv(a, "conv-003").status == DisputeStatus.CLOSED             # closed invoice
    assert by_conv(a, "conv-004").status == DisputeStatus.IN_REVIEW          # UCID mismatch
    assert by_conv(a, "conv-005").status == DisputeStatus.NOT_A_DISPUTE      # query
    assert by_conv(a, "conv-006").status == DisputeStatus.NOT_A_DISPUTE      # out of office, no reply
    assert by_conv(a, "conv-007").status == DisputeStatus.IN_REVIEW          # prompt injection from gmail
    assert by_conv(a, "conv-008").status == DisputeStatus.IN_REVIEW          # above AED cap
    assert not [m for m in a.mailbox.sent if m["conversation_id"] == "conv-006"]


def test_customer_follow_up_completes_dispute(make_agent):
    a = make_agent("autonomous")
    a.run_cycle()
    shutil.copy(ROOT / "samples" / "followups" / "followup-missing-invoice.json", a.mailbox.inbox / "zz.json")
    a.run_cycle()
    rec = by_conv(a, "conv-002")
    assert rec.status == DisputeStatus.CREATED
    assert rec.fields.invoice_number.value == "INV-2026-106010" and rec.reason_code == "DUPLICATE"


def test_shadow_mode_sends_nothing_and_creates_nothing(make_agent):
    a = make_agent("shadow")
    a.run_cycle()
    assert a.mailbox.sent == [] and a.soa.disputes == {}
    assert by_conv(a, "conv-001").shadow_action == "create"
    assert by_conv(a, "conv-008").shadow_action == "review"


def test_create_is_idempotent(make_agent):
    soa = MockSoaClient()
    a = make_agent("autonomous", soa=soa)
    a.run_cycle()
    rec = by_conv(a, "conv-001")
    # simulate a retry of the same create (e.g. after a timeout)
    a._create_in_fusion(a.store.get(rec.id), autonomous=False)
    assert len(soa.disputes) == 1 and soa.create_calls == 2


class FlakySoa(MockSoaClient):
    def __init__(self):
        super().__init__()
        self.fail = True

    def create_dispute(self, req):
        if self.fail:
            raise SoaTechnicalError("timeout")
        return super().create_dispute(req)


def test_soa_outage_is_retried(make_agent):
    soa = FlakySoa()
    a = make_agent("autonomous", soa=soa)
    a.run_cycle()
    assert by_conv(a, "conv-001").status == DisputeStatus.READY_TO_CREATE
    soa.fail = False
    a.run_cycle()
    assert by_conv(a, "conv-001").status == DisputeStatus.CREATED


def test_l1_corrects_fields_then_creates(make_agent):
    a = make_agent("assist")
    a.run_cycle()
    rec = by_conv(a, "conv-004")  # UCID mismatch
    rec = a.approve(rec.id, "L1", edits={"ucid": "UC200981"})
    assert rec.status == DisputeStatus.CREATED
    assert any(e["event"] == "review_fields_edited" for e in a.store.audit_for(rec.id))


def test_reject_closes_record(make_agent):
    a = make_agent("assist")
    a.run_cycle()
    rec = a.reject(by_conv(a, "conv-007").id, "L1", "suspicious sender", reply="none")
    assert rec.status == DisputeStatus.CLOSED


def test_reminders_then_close(make_agent):
    a = make_agent("assist")
    a.run_cycle()
    rec = by_conv(a, "conv-002")
    for _ in range(a.rules.max_reminders + 1):
        r = a.store.get(rec.id)
        r.updated_at -= timedelta(days=10)
        a.store._db.execute("UPDATE disputes SET record=? WHERE id=?", (r.model_dump_json(), r.id))
        a.send_reminders()
    assert a.store.get(rec.id).status == DisputeStatus.CLOSED
    subjects = [m["subject"] for m in a.mailbox.sent if m["conversation_id"] == "conv-002"]
    assert sum("reminder" in s for s in subjects) == a.rules.max_reminders and any("closed" in s for s in subjects)


def test_every_decision_is_audited(make_agent):
    a = make_agent("autonomous")
    a.run_cycle()
    events = [e["event"] for e in a.store.audit_for(by_conv(a, "conv-001").id)]
    assert events[:3] == ["email_received", "analyzed", "rules_evaluated"]
    assert "fusion_dispute_created" in events and "customer_reply_sent:acknowledgement" in events
