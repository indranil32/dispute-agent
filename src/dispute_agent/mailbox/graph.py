"""Microsoft Graph mailbox adapter for Exchange Online.

Permissions (application): Mail.ReadWrite, Mail.Send - restricted to the dispute mailbox only
via Exchange Online RBAC for Applications (or an ApplicationAccessPolicy). See README.

Processed emails are moved to a folder (default "Agent-Processed") so they are never fetched twice.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone
from io import BytesIO

import httpx
import msal

from ..models import Attachment, Email
from .base import Mailbox

log = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"


class GraphMailbox(Mailbox):
    def __init__(self, tenant_id: str, client_id: str, client_secret: str, mailbox: str,
                 processed_folder: str = "Agent-Processed", page_size: int = 50):
        self.mailbox = mailbox
        self.processed_folder = processed_folder
        self.page_size = page_size
        self._app = msal.ConfidentialClientApplication(
            client_id, authority=f"https://login.microsoftonline.com/{tenant_id}", client_credential=client_secret)
        self._http = httpx.Client(timeout=30)
        self._folder_id: str | None = None

    # --- auth -------------------------------------------------------------
    def _headers(self) -> dict:
        tok = self._app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in tok:
            raise RuntimeError(f"Graph auth failed: {tok.get('error_description')}")
        return {"Authorization": f"Bearer {tok['access_token']}", "Prefer": 'outlook.body-content-type="text"'}

    def _get(self, url: str, **params) -> dict:
        r = self._http.get(url, headers=self._headers(), params=params or None)
        r.raise_for_status()
        return r.json()

    def _post(self, url: str, json: dict) -> dict:
        r = self._http.post(url, headers=self._headers(), json=json)
        r.raise_for_status()
        return r.json() if r.content else {}

    # --- mailbox ----------------------------------------------------------
    def fetch_new(self) -> list[Email]:
        url = f"{GRAPH}/users/{self.mailbox}/mailFolders/inbox/messages"
        data = self._get(url, **{
            "$select": "id,conversationId,from,subject,body,receivedDateTime,hasAttachments",
            "$orderby": "receivedDateTime asc", "$top": str(self.page_size)})
        emails = []
        for m in data.get("value", []):
            attachments = self._attachments(m["id"]) if m.get("hasAttachments") else []
            emails.append(Email(
                message_id=m["id"],
                conversation_id=m["conversationId"],
                sender=(m.get("from") or {}).get("emailAddress", {}).get("address", ""),
                subject=m.get("subject") or "",
                body=(m.get("body") or {}).get("content", ""),
                received_at=datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")),
                attachments=attachments,
            ))
        return emails

    def _attachments(self, message_id: str) -> list[Attachment]:
        data = self._get(f"{GRAPH}/users/{self.mailbox}/messages/{message_id}/attachments")
        out = []
        for a in data.get("value", []):
            if a.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue
            raw = base64.b64decode(a.get("contentBytes") or b"")
            out.append(Attachment(name=a["name"], content_type=a.get("contentType") or "", size=a.get("size", 0),
                                  text=_extract_text(a["name"], a.get("contentType") or "", raw)))
        return out

    def reply(self, original, to, subject, body, conversation_id) -> None:
        if original is not None:
            self._post(f"{GRAPH}/users/{self.mailbox}/messages/{original.message_id}/reply",
                       {"message": {"body": {"contentType": "Text", "content": body}}})
        else:
            self._post(f"{GRAPH}/users/{self.mailbox}/sendMail", {"message": {
                "subject": subject, "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to}}]}, "saveToSentItems": True})

    def mark_processed(self, email: Email) -> None:
        self._post(f"{GRAPH}/users/{self.mailbox}/messages/{email.message_id}/move",
                   {"destinationId": self._processed_folder_id()})

    def _processed_folder_id(self) -> str:
        if self._folder_id:
            return self._folder_id
        base = f"{GRAPH}/users/{self.mailbox}/mailFolders"
        found = self._get(base, **{"$filter": f"displayName eq '{self.processed_folder}'"}).get("value", [])
        self._folder_id = found[0]["id"] if found else self._post(base, {"displayName": self.processed_folder})["id"]
        return self._folder_id

    # --- change notifications (webhook) --------------------------------------
    def ensure_subscription(self, notification_url: str, client_state: str) -> dict:
        """Create a subscription for new inbox messages. Renew before expiry (max ~7 days for mail)."""
        expiry = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat().replace("+00:00", "Z")
        return self._post(f"{GRAPH}/subscriptions", {
            "changeType": "created",
            "notificationUrl": notification_url,
            "resource": f"users/{self.mailbox}/mailFolders('inbox')/messages",
            "expirationDateTime": expiry,
            "clientState": client_state,
        })


def _extract_text(name: str, content_type: str, raw: bytes) -> str | None:
    """Best-effort text from attachments. Scanned images need OCR (Azure AI Document Intelligence) - hook here."""
    lname = name.lower()
    try:
        if content_type.startswith("text/") or lname.endswith((".txt", ".csv")):
            return raw.decode("utf-8", errors="replace")
        if lname.endswith(".pdf"):
            try:
                from pypdf import PdfReader  # optional dependency
            except ImportError:
                return None
            return "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(raw)).pages)
    except Exception as exc:  # never fail an email because of one attachment
        log.warning("Could not read attachment %s: %s", name, exc)
    return None
