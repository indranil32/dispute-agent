"""Agent orchestrator: one dispute record per email thread, moved through its states.

  Received -> (Awaiting customer) -> (In review) -> Created in Fusion -> Approved/Rejected (closed)

The LLM only analyzes. This module decides, using the rules engine, and is the only code that calls SOA.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from .config import Rulebook, Settings
from .llm.base import Analyzer
from .mailbox.base import Mailbox
from .models import Classification, DisputeFields, DisputeRecord, DisputeStatus, Email, EmailCategory, ExtractedField, utcnow
from .replies import Replies
from .rules import evaluate
from .soa.base import CreateDisputeRequest, SoaBusinessError, SoaClient, SoaTechnicalError
from .store import Store

log = logging.getLogger(__name__)


class DisputeAgent:
    def __init__(self, settings: Settings, rules: Rulebook, mailbox: Mailbox, analyzer: Analyzer,
                 soa: SoaClient, store: Store, replies: Replies):
        self.settings, self.rules, self.mailbox = settings, rules, mailbox
        self.analyzer, self.soa, self.store, self.replies = analyzer, soa, store, replies

    @property
    def mode(self) -> str:
        return self.settings.mode

    # ======================================================================================
    # Main loop entry points
    # ======================================================================================
    def run_cycle(self) -> dict:
        """One full pass: new emails, retries, Fusion status, reminders."""
        n = self.process_inbox()
        self.retry_pending_creates()
        closed = self.poll_fusion()
        self.send_reminders()
        return {"emails_processed": n, "disputes_closed": closed}

    def process_inbox(self) -> int:
        count = 0
        for email in self.mailbox.fetch_new():
            if self.store.seen(email.message_id):  # idempotent: never process an email twice
                self.mailbox.mark_processed(email)
                continue
            try:
                self.handle_email(email)
            except Exception:  # one bad email must not stop the queue
                log.exception("Failed to process %s", email.message_id)
                self.store.audit(None, "email_failed", {"message_id": email.message_id})
                continue
            self.mailbox.mark_processed(email)
            count += 1
        return count

    # ======================================================================================
    # Email handling
    # ======================================================================================
    def handle_email(self, email: Email) -> DisputeRecord:
        cls, fields = self.analyzer.analyze(email)
        existing = self.store.active_for_conversation(email.conversation_id)

        if existing is not None:
            self.store.add_email(email, existing.id)
            self.store.audit(existing.id, "email_received", {"subject": email.subject, "follow_up": True,
                                                              "category": cls.category.value})
            return self._handle_follow_up(existing, email, cls, fields)

        rec = DisputeRecord(id=self.store.new_id(), conversation_id=email.conversation_id, sender=email.sender,
                            subject=email.subject, status=DisputeStatus.RECEIVED, fields=fields)
        self.store.save(rec)
        self.store.add_email(email, rec.id)
        self.store.audit(rec.id, "email_received", {"subject": email.subject, "sender": email.sender})
        self.store.audit(rec.id, "analyzed", {"classification": cls.model_dump(), "fields": fields.model_dump()})
        return self._decide(rec, cls)

    def _handle_follow_up(self, rec: DisputeRecord, email: Email, cls: Classification, new: DisputeFields) -> DisputeRecord:
        if rec.status == DisputeStatus.AWAITING_CUSTOMER:
            rec.fields = _merge(rec.fields, new)
            rec.missing_fields = []
            self.store.audit(rec.id, "fields_merged", {"fields": rec.fields.model_dump()})
            # The thread is already a dispute: treat the reply as one, whatever its own wording
            return self._decide(rec, Classification(category=EmailCategory.NEW_DISPUTE, confidence=1.0))
        if rec.status == DisputeStatus.IN_REVIEW:
            rec.fields = _merge(rec.fields, new)
            rec.review_reasons.append("Customer sent a follow-up email")
            self.store.save(rec)
            return rec
        # Created / ready: just log it; L1 sees it in the audit trail
        self.store.audit(rec.id, "follow_up_logged", {"status": rec.status.value})
        return rec

    def _decide(self, rec: DisputeRecord, cls: Classification, human_approved: bool = False) -> DisputeRecord:
        invoice, open_disputes, looked_up = None, [], True
        inv_no = rec.fields.invoice_number.value
        if inv_no:
            try:
                invoice = self.soa.get_invoice(inv_no)
                if invoice:
                    open_disputes = self.soa.find_open_disputes(invoice.invoice_number)
            except SoaTechnicalError as exc:
                looked_up = False
                self.store.audit(rec.id, "soa_unavailable", {"error": str(exc)})
        rec.invoice = invoice

        d = evaluate(self.rules, cls, rec.fields, rec.sender, invoice, open_disputes, self.mode,
                     human_approved=human_approved, invoice_looked_up=looked_up)
        rec.reason_code = d.reason_code or rec.reason_code
        self.store.audit(rec.id, "rules_evaluated", {"action": d.action, "mode": self.mode,
                                                     "results": [r.model_dump() for r in d.results], "reasons": d.reasons})

        if self.mode == "shadow" and not human_approved:
            rec.status, rec.shadow_action = DisputeStatus.SHADOW, d.action
            self.store.save(rec)
            self.store.audit(rec.id, "shadow_decision", {"would_do": d.action, "reasons": d.reasons})
            return rec

        if d.action == "ignore":
            rec.status = DisputeStatus.NOT_A_DISPUTE
            self.store.save(rec)
        elif d.action == "reply_standard":
            rec.status = DisputeStatus.NOT_A_DISPUTE
            self.store.save(rec)
            self._send(rec, "not_a_dispute")
        elif d.action == "auto_reply":
            rec.status, rec.missing_fields = DisputeStatus.AWAITING_CUSTOMER, d.missing_fields
            rec.reminders_sent = 0
            self.store.save(rec)
            self._send(rec, "missing_details", missing_list=Replies.missing_list(d.missing_fields))
        elif d.action == "review":
            first_time = rec.status != DisputeStatus.IN_REVIEW
            rec.status, rec.review_reasons = DisputeStatus.IN_REVIEW, d.reasons
            self.store.save(rec)
            self.store.audit(rec.id, "sent_to_review", {"reasons": d.reasons})
            if first_time:
                self._send(rec, "received")
        elif d.action == "reply_not_disputable":
            rec.status = DisputeStatus.CLOSED
            self.store.save(rec)
            self._send(rec, "not_disputable", invoice_number=rec.fields.invoice_number.value, why=d.reasons[0])
        elif d.action == "thread_existing":
            rec.status, rec.fusion_dispute_id = DisputeStatus.CLOSED, d.existing_dispute_id
            self.store.save(rec)
            self.store.audit(rec.id, "duplicate_of_existing", {"fusion_dispute_id": d.existing_dispute_id})
            self._send(rec, "received")
        elif d.action == "create":
            self._create_in_fusion(rec, autonomous=not human_approved)
        return rec

    # ======================================================================================
    # Fusion via SOA
    # ======================================================================================
    def _create_in_fusion(self, rec: DisputeRecord, autonomous: bool) -> None:
        inv, f = rec.invoice, rec.fields
        attachments = [a.name for e in self.store.emails_for(rec.id) for a in e.attachments]
        req = CreateDisputeRequest(
            request_id=rec.id, invoice_number=inv.invoice_number, ucid=f.ucid.value, customer_account=inv.customer_account,
            bill_to_site=inv.bill_to_site, business_unit=inv.business_unit, amount=f.amount(),
            currency=(f.currency.value or inv.currency).upper(), reason_code=rec.reason_code,
            description=(f.description.value or "")[:2000], contact_email=rec.sender, attachment_names=attachments)
        try:
            res = self.soa.create_dispute(req)
        except SoaBusinessError as exc:
            self.store.audit(rec.id, "fusion_create_rejected", {"code": exc.code, "message": exc.message})
            if exc.code in ("INVOICE_CLOSED",):
                rec.status = DisputeStatus.CLOSED
                self.store.save(rec)
                self._send(rec, "not_disputable", invoice_number=inv.invoice_number, why=exc.message)
            elif exc.code in ("AMOUNT_EXCEEDS_BALANCE",):
                rec.status, rec.missing_fields = DisputeStatus.AWAITING_CUSTOMER, ["amount_invalid"]
                self.store.save(rec)
                self._send(rec, "missing_details", missing_list=Replies.missing_list(["amount_invalid"]))
            else:
                rec.status, rec.review_reasons = DisputeStatus.IN_REVIEW, [f"Fusion rejected: {exc.code} {exc.message}"]
                self.store.save(rec)
            return
        except SoaTechnicalError as exc:
            rec.status = DisputeStatus.READY_TO_CREATE
            self.store.save(rec)
            self.store.audit(rec.id, "fusion_create_retry_later", {"error": str(exc)})
            return

        rec.status, rec.fusion_dispute_id, rec.fusion_dispute_number = DisputeStatus.CREATED, res.dispute_id, res.dispute_number
        rec.review_reasons = []
        self.store.save(rec)
        self.store.audit(rec.id, "fusion_dispute_created", {"dispute_id": res.dispute_id, "number": res.dispute_number})
        if autonomous:
            self.store.audit(rec.id, "fusion_dispute_created_autonomous", {})
        self._send(rec, "acknowledgement", dispute_ref=res.dispute_number, invoice_number=inv.invoice_number,
                   amount=f"{f.amount():,.2f}", currency=req.currency)

    def retry_pending_creates(self) -> None:
        for rec in self.store.list(DisputeStatus.READY_TO_CREATE):
            self._create_in_fusion(rec, autonomous=False)

    def poll_fusion(self) -> int:
        closed = 0
        for rec in self.store.list(DisputeStatus.CREATED):
            try:
                st = self.soa.get_dispute_status(rec.fusion_dispute_id)
            except SoaTechnicalError:
                continue
            if st.status not in ("APPROVED", "REJECTED"):
                continue
            rec.fusion_outcome, rec.credit_memo_number = st.outcome_detail, st.credit_memo_number
            rec.status = DisputeStatus.APPROVED if st.status == "APPROVED" else DisputeStatus.REJECTED
            self.store.save(rec)
            self.store.audit(rec.id, "fusion_outcome", st.model_dump())
            tpl = "outcome_approved" if st.status == "APPROVED" else "outcome_rejected"
            self._send(rec, tpl, dispute_ref=rec.fusion_dispute_number, invoice_number=rec.fields.invoice_number.value,
                       outcome_detail=st.outcome_detail or "", credit_memo_number=st.credit_memo_number or "")
            closed += 1
        return closed

    def send_reminders(self) -> None:
        due = utcnow() - timedelta(days=self.rules.reminder_days)
        for rec in self.store.list(DisputeStatus.AWAITING_CUSTOMER):
            if rec.updated_at > due:
                continue
            if rec.reminders_sent < self.rules.max_reminders:
                rec.reminders_sent += 1
                self.store.save(rec)
                self._send(rec, "reminder", missing_list=Replies.missing_list(rec.missing_fields))
            else:
                rec.status = DisputeStatus.CLOSED
                self.store.save(rec)
                self._send(rec, "closed_no_response")

    # ======================================================================================
    # Human review (L1)
    # ======================================================================================
    def approve(self, dispute_id: str, reviewer: str, edits: Optional[dict] = None) -> DisputeRecord:
        rec = self._require(dispute_id, {DisputeStatus.IN_REVIEW})
        if edits:
            before = rec.fields.model_dump()
            for k, v in edits.items():
                if k in DisputeFields.model_fields:
                    setattr(rec.fields, k, ExtractedField(value=str(v), confidence=1.0))
            self.store.audit(rec.id, "review_fields_edited", {"before": before, "edits": edits}, actor=reviewer)
        self.store.audit(rec.id, "review_approved", {}, actor=reviewer)
        return self._decide(rec, Classification(category=EmailCategory.NEW_DISPUTE, confidence=1.0), human_approved=True)

    def reject(self, dispute_id: str, reviewer: str, note: str, reply: str = "none") -> DisputeRecord:
        rec = self._require(dispute_id, {DisputeStatus.IN_REVIEW})
        rec.status = DisputeStatus.NOT_A_DISPUTE if reply == "not_a_dispute" else DisputeStatus.CLOSED
        self.store.save(rec)
        self.store.audit(rec.id, "review_rejected", {"note": note, "reply": reply}, actor=reviewer)
        if reply == "not_a_dispute":
            self._send(rec, "not_a_dispute")
        elif reply == "ask_details":
            rec.status, rec.missing_fields = DisputeStatus.AWAITING_CUSTOMER, list(self.rules.required_fields)
            self.store.save(rec)
            self._send(rec, "missing_details", missing_list=Replies.missing_list(rec.missing_fields))
        return rec

    def _require(self, dispute_id: str, allowed: set) -> DisputeRecord:
        rec = self.store.get(dispute_id)
        if rec is None:
            raise KeyError(dispute_id)
        if rec.status not in allowed:
            raise ValueError(f"{dispute_id} is '{rec.status.value}', expected one of {[s.value for s in allowed]}")
        return rec

    # ======================================================================================
    # Outbound email (templates only)
    # ======================================================================================
    def _send(self, rec: DisputeRecord, template: str, **values) -> None:
        subject, body = self.replies.render(template, subject=rec.subject, **values)
        if self.mode == "shadow":
            self.store.audit(rec.id, f"reply_suppressed_shadow:{template}", {})
            return
        emails = self.store.emails_for(rec.id)
        self.mailbox.reply(emails[-1] if emails else None, rec.sender, subject, body, rec.conversation_id)
        self.store.audit(rec.id, f"customer_reply_sent:{template}", {"subject": subject})


def _merge(old: DisputeFields, new: DisputeFields) -> DisputeFields:
    """Values from the newer email win where present."""
    merged = old.model_copy(deep=True)
    for name in DisputeFields.model_fields:
        nv = getattr(new, name)
        if nv.value and name != "description":
            setattr(merged, name, nv)
    return merged
