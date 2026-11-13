from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Classification, DisputeFields, Email


class Analyzer(ABC):
    """Reads an email and returns a classification plus extracted fields.

    The analyzer only READS. It never decides whether to create a dispute and never writes to Fusion:
    the rules engine and orchestrator (deterministic code) do that.
    """

    @abstractmethod
    def analyze(self, email: Email) -> tuple[Classification, DisputeFields]:
        ...


def email_as_text(email: Email, max_attachment_chars: int = 6000) -> str:
    parts = [f"From: {email.sender}", f"Subject: {email.subject}", "", email.body]
    for a in email.attachments:
        if a.text:
            parts += ["", f"--- Attachment: {a.name} ---", a.text[:max_attachment_chars]]
    return "\n".join(parts)
