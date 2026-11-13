from decimal import Decimal

from dispute_agent.models import Classification, DisputeFields, EmailCategory, ExtractedField
from dispute_agent.rules import evaluate
from dispute_agent.soa.base import OpenDispute
from dispute_agent.soa.mock import MockSoaClient

SOA = MockSoaClient()
DISPUTE = Classification(category=EmailCategory.NEW_DISPUTE, confidence=0.95)


def fields(ucid="UC100245", inv="INV-2026-104233", amount="6400", reason="overcharge", conf=0.95):
    f = DisputeFields()
    for name, v in [("ucid", ucid), ("invoice_number", inv), ("disputed_amount", amount), ("reason", reason)]:
        setattr(f, name, ExtractedField(value=v, confidence=conf if v else 0))
    return f


def run(rules, f, mode="autonomous", sender="a@gulfstarshipping.ae", open_disputes=(), cls=DISPUTE, **kw):
    inv = SOA.get_invoice(f.invoice_number.value) if f.invoice_number.value else None
    return evaluate(rules, cls, f, sender, inv, list(open_disputes), mode, **kw)


def test_clean_dispute_is_created_in_autonomous_mode(rules):
    d = run(rules, fields())
    assert d.action == "create" and d.reason_code == "PRICING"


def test_assist_mode_always_goes_to_review(rules):
    assert run(rules, fields(), mode="assist").action == "review"


def test_shadow_mode_records_autonomous_decision(rules):
    assert run(rules, fields(), mode="shadow").action == "create"


def test_missing_fields_trigger_auto_reply(rules):
    d = run(rules, fields(inv=None, amount=None))
    assert d.action == "auto_reply" and d.missing_fields == ["invoice_number", "disputed_amount"]


def test_unknown_invoice_asks_customer(rules):
    d = run(rules, fields(inv="INV-0000-000000"))
    assert d.action == "auto_reply" and d.missing_fields == ["invoice_not_found"]


def test_ucid_mismatch_goes_to_review(rules):
    assert run(rules, fields(ucid="UC999999")).action == "review"


def test_closed_invoice_is_not_disputable(rules):
    assert run(rules, fields(inv="INV-2026-104871")).action == "reply_not_disputable"


def test_amount_above_balance_asks_customer(rules):
    d = run(rules, fields(amount="99999"))
    assert d.action == "auto_reply" and d.missing_fields == ["amount_invalid"]


def test_same_open_dispute_is_threaded(rules):
    d = run(rules, fields(), open_disputes=[OpenDispute(dispute_id="X1", amount=Decimal("6400"), status="PENDING_APPROVAL")])
    assert d.action == "thread_existing" and d.existing_dispute_id == "X1"


def test_unmapped_reason_goes_to_review(rules):
    assert run(rules, fields(reason="something odd")).action == "review"


def test_unknown_sender_domain_goes_to_review(rules):
    assert run(rules, fields(), sender="someone@gmail.com").action == "review"


def test_amount_cap_blocks_autonomous_create(rules):
    f = fields(ucid="UC200981", inv="INV-2026-105502", amount="75000")
    assert run(rules, f, sender="b@desertline.com").action == "review"


def test_low_extraction_confidence_goes_to_review(rules):
    assert run(rules, fields(conf=0.5)).action == "review"


def test_low_classification_confidence_goes_to_review(rules):
    cls = Classification(category=EmailCategory.NEW_DISPUTE, confidence=0.5)
    assert run(rules, fields(), cls=cls).action == "review"


def test_human_approval_skips_mode_gate_but_not_invoice_checks(rules):
    assert run(rules, fields(), mode="assist", human_approved=True).action == "create"
    assert run(rules, fields(inv="INV-2026-104871"), human_approved=True).action == "reply_not_disputable"
