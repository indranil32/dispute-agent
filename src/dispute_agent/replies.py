"""Fixed customer reply templates. The LLM never writes free text to customers."""
from __future__ import annotations

from pathlib import Path
from string import Template

FIELD_LABELS = {
    "ucid": "Your UCID (customer ID)",
    "invoice_number": "The invoice number you are disputing",
    "disputed_amount": "The disputed amount (in AED or the invoice currency)",
    "reason": "The reason for the dispute (e.g. overcharge, duplicate charge, service not received)",
    "invoice_not_found": "A valid invoice number - we could not find the invoice you quoted",
    "amount_invalid": "The disputed amount - it must be more than zero and not exceed the invoice balance",
}


class Replies:
    def __init__(self, templates_dir: str):
        self.dir = Path(templates_dir)

    def render(self, name: str, **values) -> tuple[str, str]:
        """Return (subject, body). Template's first line is 'Subject: ...'."""
        text = (self.dir / f"{name}.txt").read_text(encoding="utf-8")
        filled = Template(text).safe_substitute({k: ("" if v is None else v) for k, v in values.items()})
        first, _, rest = filled.partition("\n")
        subject = first.removeprefix("Subject:").strip()
        return subject, rest.lstrip("\n")

    @staticmethod
    def missing_list(fields: list[str]) -> str:
        return "\n".join(f"  - {FIELD_LABELS.get(f, f)}" for f in fields)
