"""File-based mailbox for demos and tests.

Inbox: a folder of JSON files, one per email (see samples/emails).
Outbox: replies are written as JSON files to the outbox folder.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..models import Attachment, Email
from .base import Mailbox


class LocalMailbox(Mailbox):
    def __init__(self, inbox_dir: str, outbox_dir: str):
        self.inbox = Path(inbox_dir)
        self.outbox = Path(outbox_dir)
        self.outbox.mkdir(parents=True, exist_ok=True)
        self._processed_file = self.outbox / ".processed"
        self._processed = set(self._processed_file.read_text().split()) if self._processed_file.exists() else set()
        self.sent: list[dict] = []

    def fetch_new(self) -> list[Email]:
        emails = []
        for path in sorted(self.inbox.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["message_id"] in self._processed:
                continue
            data["attachments"] = [Attachment(**a) for a in data.get("attachments", [])]
            emails.append(Email(**data))
        return sorted(emails, key=lambda e: e.received_at)

    def inject(self, email: Email) -> None:
        """Test helper: drop an email into the inbox."""
        (self.inbox / f"{email.message_id}.json").write_text(email.model_dump_json(indent=2), encoding="utf-8")

    def reply(self, original, to, subject, body, conversation_id) -> None:
        msg = {
            "to": to,
            "subject": subject,
            "body": body,
            "conversation_id": conversation_id,
            "in_reply_to": original.message_id if original else None,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        self.sent.append(msg)
        name = f"{len(list(self.outbox.glob('*.json'))) + 1:04d}-{conversation_id}.json"
        (self.outbox / name).write_text(json.dumps(msg, indent=2), encoding="utf-8")

    def mark_processed(self, email: Email) -> None:
        self._processed.add(email.message_id)
        self._processed_file.write_text("\n".join(sorted(self._processed)))
