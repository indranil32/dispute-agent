# Dispute AI Agent

![CI](../../actions/workflows/ci.yml/badge.svg)

> **Status:** prototype v0.1 - runs end to end against mocks; not yet connected to live Exchange, Azure OpenAI or SOA/Fusion.
> **Confidential:** internal AD Ports Group work. Keep this repository **private**. All customers, invoices and emails in `samples/` are fictional.

Reads every email in **Dispute.Ports@adports.ae**, checks it against Finance's rules, and creates the dispute
in **Oracle Fusion** through the **SOA layer** (Oracle SOA Suite / OIC), which starts the existing Finance approval
workflow. It then tracks the outcome and replies to the customer. Built to the *Dispute AI Agent - High-Level Design*.

```
Customer -> Exchange mailbox --Graph--> Agent (Azure) --REST--> SOA / OIC --> Fusion dispute -> Finance approves
                                         |  Azure OpenAI: classify + extract (read only)
                                         |  Rules engine: Finance rulebook (config/rules.yaml)
                                         |  Store + audit log, L1 review screen
```

**The LLM only reads.** Deterministic code (`rules.py`, `orchestrator.py`) decides and is the only code that
calls SOA. Customer emails come from fixed templates (`config/templates`). Nothing touches Fusion directly.

## Try it in 1 minute (no AD Ports systems needed)

```bash
pip install -e ".[dev]"
dispute-agent demo --mode assist        # or: --mode autonomous | --mode shadow
pytest                                  # 32 tests
ruff check src tests                    # lint
```

The demo runs 8 sample emails (`samples/emails`) through a mock SOA/Fusion and prints what happened:

| Sample | Scenario | Assist mode result |
| --- | --- | --- |
| 01 | Complete overcharge dispute | Review queue -> L1 approves -> created in Fusion |
| 02 | Missing invoice number | Auto-reply asking for it; customer replies; created |
| 03 | Invoice already closed | "Cannot be disputed" reply, closed |
| 04 | UCID does not match the invoice | Review queue (L1 can correct and approve) |
| 05 | Not a dispute (berth schedule) | Standard reply |
| 06 | Out-of-office auto-reply | Ignored, no reply |
| 07 | Prompt injection from a gmail address | Review queue (unregistered sender), never auto-created |
| 08 | AED 75,000 dispute | Review queue; in autonomous mode blocked by the AED 50,000 cap |

Run the review screen: `dispute-agent serve` then open http://localhost:8080 (after `dispute-agent run-once`).

## Modes (human-in-the-loop)

| `AGENT_MODE` | Agent does alone | Person approves |
| --- | --- | --- |
| `shadow` | Analyzes and records what it *would* do. Sends nothing, creates nothing | L1 works as in Phase 1 |
| `assist` | Auto-replies for missing details, standard replies | Every Fusion dispute (review screen) |
| `autonomous` | Creates disputes that pass every rule, below the AED cap | Low confidence, rule failures, above cap |

## Project layout

| Path | What it is |
| --- | --- |
| `src/dispute_agent/orchestrator.py` | State machine: email -> record -> rules -> SOA -> replies; review approve/reject; reminders |
| `src/dispute_agent/rules.py` | Finance rulebook checks (required fields, invoice, UCID, amount, duplicate, reason code, sender, mode gate) |
| `src/dispute_agent/llm/azure_openai.py` | Azure OpenAI classifier/extractor (JSON only, email treated as untrusted data) |
| `src/dispute_agent/llm/heuristic.py` | Offline regex analyzer for demo/tests/fallback |
| `src/dispute_agent/mailbox/graph.py` | Microsoft Graph: read inbox, attachments, reply, move to processed folder, webhook subscription |
| `src/dispute_agent/soa/http.py` | SOA REST client: OAuth client credentials, retries, idempotency key |
| `src/dispute_agent/soa/mock.py` | Mock SOA + Fusion with sample invoices |
| `src/dispute_agent/store.py` | SQLite records, raw emails, audit log, metrics |
| `src/dispute_agent/api.py` | Review API + review screen, Graph webhook, `/metrics`, `/run-cycle` |
| `config/rules.yaml` | **Finance-owned** rulebook: fields, keywords, thresholds, reason codes, AED cap, reminders |
| `config/templates/` | Customer reply templates |
| `soa-contract/openapi.yaml` | **The 4 services the SOA team builds** |

## Going live - what each team provides

1. **SOA / integration team:** implement `soa-contract/openapi.yaml` on SOA Suite or OIC, mapping *Create dispute* to
   Fusion (e.g. `POST /fscmRestApi/resources/11.13.18.05/receivablesDisputes`). Must honour `Idempotency-Key`.
2. **ERP team:** confirm the Fusion object/API starts the current dispute approval workflow; supply reason codes.
3. **Finance:** confirm `config/rules.yaml` (fields, keywords, thresholds, reason-code map, AED cap) and
   the reply templates.
4. **M365 / Exchange team:** Entra app registration with Graph `Mail.ReadWrite` + `Mail.Send` (application), scoped
   to the dispute mailbox only, e.g.
   `New-ManagementRoleAssignment -App <appId> -Role "Application Mail.ReadWrite" -CustomResourceScope <scope for Dispute.Ports>`
   (RBAC for Applications) or an `ApplicationAccessPolicy`.
5. **IT Security:** Azure OpenAI deployment region / data residency, Key Vault, private endpoints.
6. **Run it:** set `.env` (see `.env.example`) with `MAILBOX_BACKEND=graph`, `LLM_BACKEND=azure_openai`,
   `SOA_BACKEND=http`, `AGENT_MODE=shadow`. Deploy the container (Azure Container Apps), then
   `dispute-agent subscribe --url https://<app>/graph/notifications` (renew every 2 days, e.g. from a scheduled job),
   and run `POST /run-cycle` every 5 minutes as a safety net for missed notifications and to poll Fusion.

## Development

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                                # defaults run fully offline
pytest && ruff check src tests
docker build -t dispute-agent . && docker run -p 8080:8080 dispute-agent
```

CI (`.github/workflows/ci.yml`) runs lint, tests on Python 3.11 and 3.12, the demo, and a Docker build with a
`/health` check on every push and pull request. Secrets go in `.env` locally and in Azure Key Vault in Azure -
never in the repository (`.env` is git-ignored).

## Before production (not in this prototype)

- Swap SQLite for Azure SQL / Cosmos DB; run one cycle worker at a time (or add row locking).
- Entra ID sign-in on the review screen (Container Apps authentication); per-user audit names.
- OCR for scanned attachments (Azure AI Document Intelligence) - hook in `mailbox/graph.py::_extract_text`.
- Malware scanning of attachments before processing.
- Attachments forwarded to Fusion via SOA (the request carries names only today).
- Reminder timing in working days (currently calendar days).
- Graph subscription renewal job; dashboards/alerts on `/metrics` (Azure Monitor).
- Load test with Phase 1 volumes; prompt and threshold tuning on real (anonymised) emails in shadow mode.
