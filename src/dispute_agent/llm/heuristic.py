"""Offline analyzer (regex + keywords). Used for demos, tests, and as a fallback if the LLM is unavailable.

It is deliberately conservative: anything it is unsure about gets low confidence, which sends the
case to human review rather than letting it through.
"""
from __future__ import annotations

import re

from ..config import Rulebook
from ..models import Classification, DisputeFields, Email, EmailCategory, ExtractedField
from .base import Analyzer, email_as_text

UCID_RE = re.compile(r"\bUCID\b\s*(?:no\.?|number|#)?\s*(?:is\s+)?[:#-]?\s*([A-Z]{0,4}\d[\dA-Z-]{3,19})", re.I)
INVOICE_RE = re.compile(
    r"\binvoice\s*(?:no\.?|number|num|#|ref)?\s*(?:is\s+)?[:#-]?\s*([A-Z]{0,5}-?\d[\dA-Z-]{4,19})", re.I)
AMOUNT_RE = re.compile(
    r"(?:amount|disputed|overcharged by|difference of|refund of|credit of)[^\d]{0,30}?(AED|USD|EUR)?\s*([\d][\d,]*(?:\.\d{1,2})?)"
    r"|(AED|USD|EUR)\s*([\d][\d,]*(?:\.\d{1,2})?)", re.I)
AUTO_REPLY_RE = re.compile(r"\b(automatic reply|auto(?:matic)?-?reply|out of (the )?office|undeliverable|delivery status)\b", re.I)


class HeuristicAnalyzer(Analyzer):
    def __init__(self, rules: Rulebook):
        self.rules = rules

    def analyze(self, email: Email) -> tuple[Classification, DisputeFields]:
        text = email_as_text(email)
        low = text.lower()

        fields = DisputeFields()
        if m := UCID_RE.search(text):
            fields.ucid = ExtractedField(value=m.group(1).upper(), confidence=0.95)
        if m := INVOICE_RE.search(text):
            fields.invoice_number = ExtractedField(value=m.group(1).upper(), confidence=0.95)
        if m := AMOUNT_RE.search(text):
            cur = m.group(1) or m.group(3)
            amt = m.group(2) or m.group(4)
            fields.disputed_amount = ExtractedField(value=amt.replace(",", ""), confidence=0.92)
            if cur:
                fields.currency = ExtractedField(value=cur.upper(), confidence=0.95)
        for phrase in sorted(self.rules.reason_map, key=len, reverse=True):
            if phrase in low:
                fields.reason = ExtractedField(value=phrase, confidence=0.9)
                break
        fields.description = ExtractedField(value=email.body.strip()[:500], confidence=1.0)

        keywords = [k for k in self.rules.dispute_keywords if k in low]
        if AUTO_REPLY_RE.search(email.subject) or AUTO_REPLY_RE.search(email.body[:200]):
            cls = Classification(category=EmailCategory.OUT_OF_SCOPE, confidence=0.95)
        elif keywords:
            conf = 0.92 if (fields.invoice_number.value or len(keywords) > 1) else 0.86
            cls = Classification(category=EmailCategory.NEW_DISPUTE, confidence=conf, keywords_found=keywords)
        elif fields.invoice_number.value:
            # mentions an invoice but no dispute wording: unsure -> review
            cls = Classification(category=EmailCategory.NEW_DISPUTE, confidence=0.6)
        else:
            cls = Classification(category=EmailCategory.QUERY, confidence=0.88)
        return cls, fields
