"""The four agent-facing SOA services (see soa-contract/openapi.yaml).

The agent never calls Fusion directly. The SOA layer (Oracle SOA Suite / OIC) owns Fusion credentials,
field mapping and the call to Fusion's Receivables Disputes resource.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

from ..models import Invoice


class SoaBusinessError(Exception):
    """A coded business rejection from SOA/Fusion (e.g. INVOICE_CLOSED, AMOUNT_EXCEEDS_BALANCE)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class SoaTechnicalError(Exception):
    """SOA unreachable or failed after retries. The case is retried later / sent to review."""


class OpenDispute(BaseModel):
    dispute_id: str
    amount: Decimal
    status: str


class CreateDisputeRequest(BaseModel):
    request_id: str  # idempotency key: the agent's dispute record id
    invoice_number: str
    ucid: str
    customer_account: str
    bill_to_site: str
    business_unit: str
    amount: Decimal
    currency: str
    reason_code: str
    description: str
    contact_email: str
    attachment_names: list[str] = []
    source: str = "DISPUTE_AI_AGENT"


class CreateDisputeResult(BaseModel):
    dispute_id: str
    dispute_number: str
    status: str


class DisputeStatusResult(BaseModel):
    dispute_id: str
    status: str  # PENDING_APPROVAL | APPROVED | REJECTED
    outcome_detail: Optional[str] = None
    credit_memo_number: Optional[str] = None


class SoaClient(ABC):
    @abstractmethod
    def get_invoice(self, invoice_number: str) -> Optional[Invoice]: ...

    @abstractmethod
    def find_open_disputes(self, invoice_number: str) -> list[OpenDispute]: ...

    @abstractmethod
    def create_dispute(self, req: CreateDisputeRequest) -> CreateDisputeResult: ...

    @abstractmethod
    def get_dispute_status(self, dispute_id: str) -> DisputeStatusResult: ...
