"""SQLite store for dispute records, raw emails and the audit log.

For production on Azure, swap for Azure SQL / Cosmos DB behind the same methods.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Optional

from .models import ACTIVE_STATUSES, DisputeRecord, DisputeStatus, Email, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS disputes (
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, status TEXT NOT NULL,
  record TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_disputes_conv ON disputes(conversation_id);
CREATE INDEX IF NOT EXISTS ix_disputes_status ON disputes(status);
CREATE TABLE IF NOT EXISTS emails (
  message_id TEXT PRIMARY KEY, dispute_id TEXT, conversation_id TEXT, received_at TEXT, raw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, dispute_id TEXT, ts TEXT NOT NULL, actor TEXT NOT NULL,
  event TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_audit_dispute ON audit(dispute_id);
"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    # --- disputes ------------------------------------------------------------
    def new_id(self) -> str:
        return f"DA-{utcnow():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"

    def save(self, rec: DisputeRecord) -> DisputeRecord:
        rec.updated_at = utcnow()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO disputes(id, conversation_id, status, record, created_at, updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, record=excluded.record, updated_at=excluded.updated_at",
                (rec.id, rec.conversation_id, rec.status.value, rec.model_dump_json(), rec.created_at.isoformat(),
                 rec.updated_at.isoformat()))
        return rec

    def get(self, dispute_id: str) -> Optional[DisputeRecord]:
        row = self._db.execute("SELECT record FROM disputes WHERE id=?", (dispute_id,)).fetchone()
        return DisputeRecord.model_validate_json(row["record"]) if row else None

    def active_for_conversation(self, conversation_id: str) -> Optional[DisputeRecord]:
        rows = self._db.execute("SELECT record FROM disputes WHERE conversation_id=? ORDER BY created_at DESC",
                                (conversation_id,)).fetchall()
        for r in rows:
            rec = DisputeRecord.model_validate_json(r["record"])
            if rec.status in ACTIVE_STATUSES:
                return rec
        return None

    def list(self, status: DisputeStatus | None = None) -> list[DisputeRecord]:
        if status:
            rows = self._db.execute("SELECT record FROM disputes WHERE status=? ORDER BY created_at", (status.value,)).fetchall()
        else:
            rows = self._db.execute("SELECT record FROM disputes ORDER BY created_at").fetchall()
        return [DisputeRecord.model_validate_json(r["record"]) for r in rows]

    # --- emails ----------------------------------------------------------------
    def add_email(self, email: Email, dispute_id: str | None) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO emails VALUES (?,?,?,?,?)",
                             (email.message_id, dispute_id, email.conversation_id, email.received_at.isoformat(),
                              email.model_dump_json()))

    def seen(self, message_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM emails WHERE message_id=?", (message_id,)).fetchone() is not None

    def emails_for(self, dispute_id: str) -> list[Email]:
        rows = self._db.execute("SELECT raw FROM emails WHERE dispute_id=? ORDER BY received_at", (dispute_id,)).fetchall()
        return [Email.model_validate_json(r["raw"]) for r in rows]

    # --- audit -------------------------------------------------------------------
    def audit(self, dispute_id: str | None, event: str, detail: dict | None = None, actor: str = "agent") -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO audit(dispute_id, ts, actor, event, detail) VALUES (?,?,?,?,?)",
                             (dispute_id, utcnow().isoformat(), actor, event, json.dumps(detail or {}, default=str)))

    def audit_for(self, dispute_id: str) -> list[dict]:
        rows = self._db.execute("SELECT ts, actor, event, detail FROM audit WHERE dispute_id=? ORDER BY seq",
                                (dispute_id,)).fetchall()
        return [{"ts": r["ts"], "actor": r["actor"], "event": r["event"], "detail": json.loads(r["detail"])} for r in rows]

    def metrics(self) -> dict:
        counts = {r["status"]: r["n"] for r in self._db.execute("SELECT status, COUNT(*) n FROM disputes GROUP BY status")}
        events = {r["event"]: r["n"] for r in self._db.execute("SELECT event, COUNT(*) n FROM audit GROUP BY event")}
        return {
            "by_status": counts,
            "emails_received": events.get("email_received", 0),
            "auto_replies_sent": events.get("customer_reply_sent:missing_details", 0) + events.get("customer_reply_sent:reminder", 0),
            "fusion_disputes_created": events.get("fusion_dispute_created", 0),
            "straight_through": events.get("fusion_dispute_created_autonomous", 0),
            "review_queue": counts.get(DisputeStatus.IN_REVIEW.value, 0),
            "l1_corrections": events.get("review_fields_edited", 0),
        }
