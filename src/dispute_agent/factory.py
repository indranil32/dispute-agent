"""Builds the agent from settings, choosing real or demo adapters."""
from __future__ import annotations

from pathlib import Path

from .config import Rulebook, Settings
from .llm.heuristic import HeuristicAnalyzer
from .orchestrator import DisputeAgent
from .replies import Replies
from .store import Store


def build_agent(settings: Settings | None = None) -> DisputeAgent:
    s = settings or Settings()
    rules = Rulebook.load(s.rules_path)

    if s.mailbox_backend == "graph":
        from .mailbox.graph import GraphMailbox
        mailbox = GraphMailbox(s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.mailbox_address,
                               s.graph_processed_folder)
    else:
        from .mailbox.local import LocalMailbox
        mailbox = LocalMailbox(s.local_inbox_dir, s.local_outbox_dir)

    if s.llm_backend == "azure_openai":
        from .llm.azure_openai import AzureOpenAIAnalyzer
        analyzer = AzureOpenAIAnalyzer(rules, s.aoai_endpoint, s.aoai_deployment, s.aoai_api_version, s.aoai_api_key)
    else:
        analyzer = HeuristicAnalyzer(rules)

    if s.soa_backend == "http":
        from .soa.http import HttpSoaClient
        soa = HttpSoaClient(s.soa_base_url, s.soa_token_url, s.soa_client_id, s.soa_client_secret, s.soa_scope, s.soa_timeout_s)
    else:
        from .soa.mock import MockSoaClient
        soa = MockSoaClient(state_file=str(Path(s.db_path).with_name("mock_fusion.json")))

    return DisputeAgent(s, rules, mailbox, analyzer, soa, Store(s.db_path), Replies(s.templates_dir))
