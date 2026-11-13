from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Email


class Mailbox(ABC):
    """Access to the single dispute mailbox (Dispute.Ports@adports.ae)."""

    @abstractmethod
    def fetch_new(self) -> list[Email]:
        """Return unprocessed emails, oldest first."""

    @abstractmethod
    def reply(self, original: Email | None, to: str, subject: str, body: str, conversation_id: str) -> None:
        """Send a reply on the email's thread (or a new mail on the conversation when original is None)."""

    @abstractmethod
    def mark_processed(self, email: Email) -> None:
        """Move/flag the email so it is not fetched again."""
