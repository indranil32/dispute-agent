"""REST client for the SOA services (Oracle SOA Suite / OIC). Contract: soa-contract/openapi.yaml."""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from ..models import Invoice
from .base import (
    CreateDisputeRequest,
    CreateDisputeResult,
    DisputeStatusResult,
    OpenDispute,
    SoaBusinessError,
    SoaClient,
    SoaTechnicalError,
)

log = logging.getLogger(__name__)


class HttpSoaClient(SoaClient):
    def __init__(self, base_url: str, token_url: str, client_id: str, client_secret: str, scope: str = "",
                 timeout_s: float = 20, retries: int = 3, transport: httpx.BaseTransport | None = None,
                 backoff_s: float = 2):
        self.base = base_url.rstrip("/")
        self.token_url, self.client_id, self.client_secret, self.scope = token_url, client_id, client_secret, scope
        self.retries, self.backoff_s = retries, backoff_s
        self._http = httpx.Client(timeout=timeout_s, transport=transport)
        self._token: str | None = None
        self._token_exp = 0.0

    def _auth(self) -> dict:
        if not self._token or time.time() > self._token_exp - 60:
            r = self._http.post(self.token_url, data={
                "grant_type": "client_credentials", "client_id": self.client_id,
                "client_secret": self.client_secret, **({"scope": self.scope} if self.scope else {})})
            r.raise_for_status()
            body = r.json()
            self._token, self._token_exp = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}"}

    def _call(self, method: str, path: str, *, json=None, headers=None, allow_404=False):
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                r = self._http.request(method, f"{self.base}{path}", json=json, headers={**self._auth(), **(headers or {})})
            except httpx.TransportError as exc:
                last = exc
            else:
                if r.status_code == 404 and allow_404:
                    return None
                if 400 <= r.status_code < 500:
                    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    raise SoaBusinessError(body.get("code", f"HTTP_{r.status_code}"), body.get("message", r.text[:200]))
                if r.status_code < 400:
                    return r.json()
                last = SoaTechnicalError(f"HTTP {r.status_code}: {r.text[:200]}")
            time.sleep(min(self.backoff_s ** attempt, 10) if self.backoff_s else 0)
            log.warning("SOA %s %s failed (attempt %s/%s): %s", method, path, attempt, self.retries, last)
        raise SoaTechnicalError(str(last))

    def get_invoice(self, invoice_number: str) -> Optional[Invoice]:
        data = self._call("GET", f"/invoices/{invoice_number}", allow_404=True)
        return Invoice(**data) if data else None

    def find_open_disputes(self, invoice_number: str) -> list[OpenDispute]:
        data = self._call("GET", f"/invoices/{invoice_number}/disputes?status=OPEN", allow_404=True) or {"items": []}
        return [OpenDispute(**d) for d in data.get("items", [])]

    def create_dispute(self, req: CreateDisputeRequest) -> CreateDisputeResult:
        data = self._call("POST", "/disputes", json=req.model_dump(mode="json"), headers={"Idempotency-Key": req.request_id})
        return CreateDisputeResult(**data)

    def get_dispute_status(self, dispute_id: str) -> DisputeStatusResult:
        return DisputeStatusResult(**self._call("GET", f"/disputes/{dispute_id}"))
