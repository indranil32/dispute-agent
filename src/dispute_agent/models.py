"""Core data models for the dispute agent."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Attachment(BaseModel):
    name: str
    content_type: str = "application/octet-stream"
    size: int = 0
    text: Optional[str] = None  # extracted text (PDF/OCR), if available


class Email(BaseModel):
    """An inbound email as read from the dispute mailbox."""

    message_id: str
    conversation_id: str
    sender: str
    subject: str
    body: str
    received_at: datetime = Field(default_factory=utcnow)
    attachments: list[Attachment] = Field(default_factory=list)


class EmailCategory(str, Enum):
    NEW_DISPUTE = "new_dispute"
    REPLY_TO_DISPUTE = "reply_to_dispute"
    QUERY = "query"  # a question, not a dispute
    OUT_OF_SCOPE = "out_of_scope"  # spam, auto-replies, newsletters


class Classification(BaseModel):
    category: EmailCategory
    confidence: float = Field(ge=0, le=1)
    keywords_found: list[str] = Field(default_factory=list)


class ExtractedField(BaseModel):
    value: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0, le=1)


class DisputeFields(BaseModel):
    """Structured fields the LLM extracts from an email. Values are strings as written by the customer."""

    ucid: ExtractedField = Field(default_factory=ExtractedField)
    invoice_number: ExtractedField = Field(default_factory=ExtractedField)
    disputed_amount: ExtractedField = Field(default_factory=ExtractedField)
    currency: ExtractedField = Field(default_factory=ExtractedField)
    reason: ExtractedField = Field(default_factory=ExtractedField)
    description: ExtractedField = Field(default_factory=ExtractedField)

    def amount(self) -> Optional[Decimal]:
        raw = (self.disputed_amount.value or "").replace(",", "").strip()
        if not raw:
            return None
        try:
            return Decimal(raw)
        except Exception:
            return None


class Invoice(BaseModel):
    """Invoice as returned by the SOA 'Get invoice' service."""

    invoice_number: str
    customer_ucid: str
    customer_name: str
    customer_account: str
    bill_to_site: str
    business_unit: str
    currency: str
    amount: Decimal
    balance_due: Decimal
    status: str  # OPEN | CLOSED
    contact_domains: list[str] = Field(default_factory=list)


class DisputeStatus(str, Enum):
    RECEIVED = "Received"
    AWAITING_CUSTOMER = "Awaiting customer"
    IN_REVIEW = "In review"
    READY_TO_CREATE = "Ready to create"
    CREATED = "Created in Fusion"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    CLOSED = "Closed"
    NOT_A_DISPUTE = "Not a dispute"
    SHADOW = "Shadow (no action)"


ACTIVE_STATUSES = {DisputeStatus.RECEIVED, DisputeStatus.AWAITING_CUSTOMER, DisputeStatus.IN_REVIEW,
                   DisputeStatus.READY_TO_CREATE, DisputeStatus.CREATED}


class RuleResult(BaseModel):
    rule: str
    passed: bool
    detail: str = ""
    on_fail: str = ""  # auto_reply | review | reply_not_disputable | thread_existing | reply_standard


class Decision(BaseModel):
    """Outcome of running the rulebook on one dispute."""

    action: str  # create | auto_reply | review | reply_not_disputable | thread_existing | reply_standard | ignore
    missing_fields: list[str] = Field(default_factory=list)
    results: list[RuleResult] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    reason_code: Optional[str] = None
    existing_dispute_id: Optional[str] = None


class DisputeRecord(BaseModel):
    id: str
    conversation_id: str
    sender: str
    subject: str
    status: DisputeStatus
    fields: DisputeFields = Field(default_factory=DisputeFields)
    invoice: Optional[Invoice] = None
    reason_code: Optional[str] = None
    fusion_dispute_id: Optional[str] = None
    fusion_dispute_number: Optional[str] = None
    fusion_outcome: Optional[str] = None
    credit_memo_number: Optional[str] = None
    review_reasons: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    shadow_action: Optional[str] = None
    reminders_sent: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
