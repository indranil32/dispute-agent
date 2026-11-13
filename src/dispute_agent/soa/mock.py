"""In-memory (file-backed) stand-in for SOA + Fusion, so the agent runs end to end without AD Ports systems.

Behaves like the real contract: invoice lookup, open-dispute check, idempotent create with business errors,
and a dispute that stays PENDING_APPROVAL until 'Finance' decides (see `finance_decide`).
All customers and invoices below are fictional.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Optional

from ..models import Invoice
from .base import CreateDisputeRequest, CreateDisputeResult, DisputeStatusResult, OpenDispute, SoaBusinessError, SoaClient

SAMPLE_INVOICES = [
    dict(invoice_number="INV-2026-104233", customer_ucid="UC100245", customer_name="Gulf Star Shipping LLC",
         customer_account="ACC-77812", bill_to_site="ABU DHABI-HQ", business_unit="ADPG Ports - Khalifa Port",
         currency="AED", amount="48250.00", balance_due="48250.00", status="OPEN", contact_domains=["gulfstarshipping.ae"]),
    dict(invoice_number="INV-2026-104871", customer_ucid="UC100245", customer_name="Gulf Star Shipping LLC",
         customer_account="ACC-77812", bill_to_site="ABU DHABI-HQ", business_unit="ADPG Ports - Khalifa Port",
         currency="AED", amount="12600.00", balance_due="0.00", status="CLOSED", contact_domains=["gulfstarshipping.ae"]),
    dict(invoice_number="INV-2026-105502", customer_ucid="UC200981", customer_name="Desert Line Logistics",
         customer_account="ACC-80455", bill_to_site="DUBAI-BR", business_unit="ADPG Ports - Zayed Port",
         currency="AED", amount="186400.00", balance_due="186400.00", status="OPEN", contact_domains=["desertline.com"]),
    dict(invoice_number="INV-2026-106010", customer_ucid="UC300410", customer_name="Arabian Reefer Lines",
         customer_account="ACC-91230", bill_to_site="ABU DHABI-MUSSAFAH", business_unit="ADPG Ports - Khalifa Port",
         currency="AED", amount="9800.00", balance_due="9800.00", status="OPEN", contact_domains=["arabianreefer.com"]),
]


class MockSoaClient(SoaClient):
    def __init__(self, state_file: str | None = None):
        self.state_file = Path(state_file) if state_file else None
        self.invoices = {i["invoice_number"]: Invoice(**i) for i in SAMPLE_INVOICES}
        self.disputes: dict[str, dict] = {}
        self.by_request: dict[str, str] = {}
        self.create_calls = 0
        self._load()

    # --- persistence (so the CLI, API and demo share state) ---------------------------
    def _load(self):
        if self.state_file and self.state_file.exists():
            s = json.loads(self.state_file.read_text())
            self.disputes, self.by_request = s["disputes"], s["by_request"]

    def _save(self):
        if self.state_file:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"disputes": self.disputes, "by_request": self.by_request}, indent=2))

    # --- contract -----------------------------------------------------------------
    def get_invoice(self, invoice_number: str) -> Optional[Invoice]:
        return self.invoices.get(invoice_number.upper())

    def find_open_disputes(self, invoice_number: str) -> list[OpenDispute]:
        return [OpenDispute(dispute_id=d["dispute_id"], amount=Decimal(d["amount"]), status=d["status"])
                for d in self.disputes.values()
                if d["invoice_number"] == invoice_number and d["status"] == "PENDING_APPROVAL"]

    def create_dispute(self, req: CreateDisputeRequest) -> CreateDisputeResult:
        self.create_calls += 1
        if req.request_id in self.by_request:  # idempotent replay
            d = self.disputes[self.by_request[req.request_id]]
            return CreateDisputeResult(dispute_id=d["dispute_id"], dispute_number=d["dispute_number"], status=d["status"])
        inv = self.invoices.get(req.invoice_number)
        if inv is None:
            raise SoaBusinessError("INVOICE_NOT_FOUND", f"Invoice {req.invoice_number} not found")
        if inv.status != "OPEN":
            raise SoaBusinessError("INVOICE_CLOSED", f"Invoice {req.invoice_number} is {inv.status}")
        if req.amount > inv.balance_due:
            raise SoaBusinessError("AMOUNT_EXCEEDS_BALANCE", "Disputed amount exceeds balance due")
        n = len(self.disputes) + 1
        d = {"dispute_id": f"300000{n:06d}", "dispute_number": f"DSP-{n:05d}", "status": "PENDING_APPROVAL",
             "invoice_number": req.invoice_number, "amount": str(req.amount), "reason_code": req.reason_code,
             "outcome_detail": None, "credit_memo_number": None}
        self.disputes[d["dispute_id"]] = d
        self.by_request[req.request_id] = d["dispute_id"]
        self._save()
        return CreateDisputeResult(dispute_id=d["dispute_id"], dispute_number=d["dispute_number"], status=d["status"])

    def get_dispute_status(self, dispute_id: str) -> DisputeStatusResult:
        self._load()
        d = self.disputes[dispute_id]
        return DisputeStatusResult(dispute_id=dispute_id, status=d["status"], outcome_detail=d["outcome_detail"],
                                   credit_memo_number=d["credit_memo_number"])

    # --- demo helper: Finance approves/rejects in the Fusion workflow --------------------
    def finance_decide(self, dispute_id: str, approve: bool, detail: str) -> None:
        self._load()
        d = self.disputes[dispute_id]
        d["status"] = "APPROVED" if approve else "REJECTED"
        d["outcome_detail"] = detail
        if approve:
            d["credit_memo_number"] = f"CM-{dispute_id[-6:]}"
        self._save()
