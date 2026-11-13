"""Rules engine: applies Finance's rulebook to one dispute. Deterministic - no LLM here.

Actions:
  create               -> send to SOA to create the Fusion dispute
  auto_reply           -> ask the customer for missing / invalid details (template)
  review               -> human review queue (L1)
  reply_not_disputable -> tell the customer the invoice cannot be disputed (template)
  thread_existing      -> same dispute already open in Fusion; link to it
  reply_standard       -> not a dispute: standard reply (template)
  ignore               -> out of scope (spam, auto-replies): no reply
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .config import Rulebook
from .models import Classification, Decision, DisputeFields, EmailCategory, Invoice, RuleResult
from .soa.base import OpenDispute


def map_reason(rules: Rulebook, reason_text: Optional[str]) -> Optional[str]:
    if not reason_text:
        return None
    low = reason_text.lower()
    for phrase in sorted(rules.reason_map, key=len, reverse=True):
        if phrase in low:
            return rules.reason_map[phrase]
    return None


def evaluate(rules: Rulebook, cls: Classification, fields: DisputeFields, sender: str,
             invoice: Optional[Invoice], open_disputes: list[OpenDispute], mode: str,
             human_approved: bool = False, invoice_looked_up: bool = True) -> Decision:
    results: list[RuleResult] = []

    def fail(rule, detail, on_fail):
        results.append(RuleResult(rule=rule, passed=False, detail=detail, on_fail=on_fail))

    def ok(rule, detail=""):
        results.append(RuleResult(rule=rule, passed=True, detail=detail))

    # 1. Is it a dispute? -------------------------------------------------------------
    if not human_approved:
        if cls.category == EmailCategory.OUT_OF_SCOPE and cls.confidence >= rules.classifier_threshold:
            ok("classification", "out of scope")
            return Decision(action="ignore", results=results)
        if cls.category == EmailCategory.QUERY and cls.confidence >= rules.classifier_threshold:
            ok("classification", "query, not a dispute")
            return Decision(action="reply_standard", results=results)
        if cls.confidence < rules.classifier_threshold:
            fail("classification", f"{cls.category.value} at confidence {cls.confidence:.2f}", "review")
            return Decision(action="review", results=results,
                            reasons=[f"Unsure if this is a dispute (confidence {cls.confidence:.2f})"])
        ok("classification", f"{cls.category.value} {cls.confidence:.2f}; keywords: {', '.join(cls.keywords_found)}")

    # 2. Required fields ----------------------------------------------------------------
    missing = [f for f in rules.required_fields if not getattr(fields, f).value]
    if missing:
        fail("required_fields", f"missing: {', '.join(missing)}", "auto_reply")
        return Decision(action="auto_reply", missing_fields=missing, results=results)
    ok("required_fields")

    # 3. Extraction confidence ----------------------------------------------------------
    if not human_approved:
        low = [f for f in rules.required_fields if getattr(fields, f).confidence < rules.extraction_threshold]
        if low:
            fail("extraction_confidence", f"low confidence: {', '.join(low)}", "review")
            return Decision(action="review", results=results, reasons=[f"Low extraction confidence: {', '.join(low)}"])
        ok("extraction_confidence")

    # 4. Invoice exists -----------------------------------------------------------------
    if not invoice_looked_up:
        fail("invoice_exists", "SOA unavailable", "review")
        return Decision(action="review", results=results, reasons=["SOA unavailable - retry lookup"])
    if invoice is None:
        fail("invoice_exists", f"{fields.invoice_number.value} not found in Fusion", "auto_reply")
        return Decision(action="auto_reply", missing_fields=["invoice_not_found"], results=results)
    ok("invoice_exists", invoice.invoice_number)

    # 5. Invoice belongs to customer ----------------------------------------------------------
    if invoice.customer_ucid.upper() != (fields.ucid.value or "").upper():
        fail("invoice_belongs_to_customer", f"UCID {fields.ucid.value} vs invoice customer {invoice.customer_ucid}", "review")
        return Decision(action="review", results=results,
                        reasons=[f"UCID {fields.ucid.value} does not match invoice customer {invoice.customer_ucid}"])
    ok("invoice_belongs_to_customer")

    # 6. Invoice is disputable ------------------------------------------------------------
    if invoice.status != "OPEN" or invoice.balance_due <= 0:
        fail("invoice_disputable", f"status {invoice.status}, balance {invoice.balance_due}", "reply_not_disputable")
        return Decision(action="reply_not_disputable", results=results,
                        reasons=["The invoice is closed or already fully paid/credited."])
    ok("invoice_disputable")

    # 7. Amount -------------------------------------------------------------------------------
    amount = fields.amount()
    if amount is None or amount <= 0 or amount > invoice.balance_due:
        fail("amount", f"{fields.disputed_amount.value} vs balance {invoice.balance_due}", "auto_reply")
        return Decision(action="auto_reply", missing_fields=["amount_invalid"], results=results)
    ok("amount", f"{amount} <= {invoice.balance_due}")

    cur = (fields.currency.value or invoice.currency).upper()
    if cur != invoice.currency.upper():
        fail("currency", f"{cur} vs invoice {invoice.currency}", "review")
        return Decision(action="review", results=results, reasons=[f"Currency {cur} differs from invoice {invoice.currency}"])

    # 8. Duplicate ---------------------------------------------------------------------------
    same = [d for d in open_disputes if Decimal(d.amount) == amount]
    if same:
        fail("duplicate", f"open dispute {same[0].dispute_id} for the same amount", "thread_existing")
        return Decision(action="thread_existing", results=results, existing_dispute_id=same[0].dispute_id)
    if open_disputes:
        fail("duplicate", f"{len(open_disputes)} other open dispute(s) on invoice", "review")
        return Decision(action="review", results=results, reasons=["Invoice already has another open dispute"])
    ok("duplicate")

    # 9. Reason code -------------------------------------------------------------------------
    code = map_reason(rules, fields.reason.value)
    if not code:
        fail("reason_code", f"no mapping for '{fields.reason.value}'", "review")
        return Decision(action="review", results=results, reasons=[f"Reason '{fields.reason.value}' has no Fusion reason code"])
    ok("reason_code", code)

    # 10. Sender ------------------------------------------------------------------------------
    if rules.check_sender_domain and not human_approved:
        domain = sender.rsplit("@", 1)[-1].lower()
        if invoice.contact_domains and domain not in [d.lower() for d in invoice.contact_domains]:
            fail("sender", f"{domain} not a registered contact domain", "review")
            return Decision(action="review", results=results, reason_code=code,
                            reasons=[f"Sender domain {domain} is not registered for {invoice.customer_name}"])
        ok("sender", domain)

    # 11. Mode gate -----------------------------------------------------------------------------
    if human_approved:
        return Decision(action="create", results=results, reason_code=code)
    # shadow mode records what autonomous mode WOULD do, so it falls through to the autonomous checks
    if mode == "assist":
        return Decision(action="review", results=results, reason_code=code,
                        reasons=["Assist mode: ready to create - L1 approval required"])
    if amount > rules.autonomous_amount_cap:
        return Decision(action="review", results=results, reason_code=code,
                        reasons=[f"Amount {amount} above autonomous cap {rules.autonomous_amount_cap}"])
    return Decision(action="create", results=results, reason_code=code)
