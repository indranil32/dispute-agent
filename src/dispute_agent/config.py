"""Settings (environment) and the Finance rulebook (YAML)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    # Which adapters to use: "local" / "mock" / "heuristic" for demo, real ones for production
    mailbox_backend: str = field(default_factory=lambda: _env("MAILBOX_BACKEND", "local"))  # local | graph
    llm_backend: str = field(default_factory=lambda: _env("LLM_BACKEND", "heuristic"))  # heuristic | azure_openai
    soa_backend: str = field(default_factory=lambda: _env("SOA_BACKEND", "mock"))  # mock | http

    # Agent mode: shadow | assist | autonomous  (see README: human-in-the-loop)
    mode: str = field(default_factory=lambda: _env("AGENT_MODE", "assist"))

    db_path: str = field(default_factory=lambda: _env("DB_PATH", str(ROOT / "data" / "disputes.db")))
    rules_path: str = field(default_factory=lambda: _env("RULES_PATH", str(ROOT / "config" / "rules.yaml")))
    templates_dir: str = field(default_factory=lambda: _env("TEMPLATES_DIR", str(ROOT / "config" / "templates")))

    # Local mailbox (demo)
    local_inbox_dir: str = field(default_factory=lambda: _env("LOCAL_INBOX_DIR", str(ROOT / "samples" / "emails")))
    local_outbox_dir: str = field(default_factory=lambda: _env("LOCAL_OUTBOX_DIR", str(ROOT / "data" / "outbox")))

    # Microsoft Graph (Exchange Online)
    graph_tenant_id: str = field(default_factory=lambda: _env("GRAPH_TENANT_ID"))
    graph_client_id: str = field(default_factory=lambda: _env("GRAPH_CLIENT_ID"))
    graph_client_secret: str = field(default_factory=lambda: _env("GRAPH_CLIENT_SECRET"))
    mailbox_address: str = field(default_factory=lambda: _env("MAILBOX_ADDRESS", "Dispute.Ports@adports.ae"))
    graph_processed_folder: str = field(default_factory=lambda: _env("GRAPH_PROCESSED_FOLDER", "Agent-Processed"))
    graph_webhook_client_state: str = field(default_factory=lambda: _env("GRAPH_WEBHOOK_CLIENT_STATE"))

    # Azure OpenAI
    aoai_endpoint: str = field(default_factory=lambda: _env("AZURE_OPENAI_ENDPOINT"))
    aoai_api_key: str = field(default_factory=lambda: _env("AZURE_OPENAI_API_KEY"))  # empty -> managed identity
    aoai_deployment: str = field(default_factory=lambda: _env("AZURE_OPENAI_DEPLOYMENT", "gpt-4o"))
    aoai_api_version: str = field(default_factory=lambda: _env("AZURE_OPENAI_API_VERSION", "2024-10-21"))

    # SOA layer (Oracle SOA Suite / OIC)
    soa_base_url: str = field(default_factory=lambda: _env("SOA_BASE_URL"))
    soa_token_url: str = field(default_factory=lambda: _env("SOA_TOKEN_URL"))
    soa_client_id: str = field(default_factory=lambda: _env("SOA_CLIENT_ID"))
    soa_client_secret: str = field(default_factory=lambda: _env("SOA_CLIENT_SECRET"))
    soa_scope: str = field(default_factory=lambda: _env("SOA_SCOPE"))
    soa_timeout_s: float = field(default_factory=lambda: float(_env("SOA_TIMEOUT_S", "20")))


@dataclass
class Rulebook:
    raw: dict

    @classmethod
    def load(cls, path: str) -> "Rulebook":
        with open(path, encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh))

    @property
    def required_fields(self) -> list[str]:
        return self.raw["required_fields"]

    @property
    def dispute_keywords(self) -> list[str]:
        return [k.lower() for k in self.raw["dispute_keywords"]]

    @property
    def classifier_threshold(self) -> float:
        return float(self.raw["thresholds"]["classification"])

    @property
    def extraction_threshold(self) -> float:
        return float(self.raw["thresholds"]["extraction"])

    @property
    def autonomous_amount_cap(self) -> Decimal:
        return Decimal(str(self.raw["autonomous_amount_cap_aed"]))

    @property
    def reason_map(self) -> dict[str, str]:
        return {k.lower(): v for k, v in self.raw["reason_codes"].items()}

    @property
    def reminder_days(self) -> int:
        return int(self.raw["reminders"]["days_between"])

    @property
    def max_reminders(self) -> int:
        return int(self.raw["reminders"]["max"])

    @property
    def check_sender_domain(self) -> bool:
        return bool(self.raw.get("check_sender_domain", True))
