"""Command line.

  dispute-agent run-once            one cycle: inbox, retries, Fusion status, reminders
  dispute-agent loop --interval 60  run cycles forever (fallback if webhooks are not used)
  dispute-agent serve --port 8080   review API + review screen + Graph webhook
  dispute-agent demo                full offline demo with sample emails and mock Fusion
  dispute-agent list                show all dispute records
  dispute-agent finance-decide <fusion_dispute_id> approve|reject "detail"   (mock Fusion only)
  dispute-agent subscribe --url https://.../graph/notifications             (Graph only)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from .config import ROOT, Settings
from .factory import build_agent
from .models import DisputeStatus


def _table(agent) -> str:
    rows = agent.store.list()
    out = [f"{'Record':<20} {'Status':<20} {'Invoice':<17} {'Amount':>10}  {'Fusion':<10} Note"]
    for r in rows:
        note = "; ".join(r.review_reasons) or ", ".join(r.missing_fields) or (r.fusion_outcome or "") or (r.shadow_action or "")
        out.append(f"{r.id:<20} {r.status.value:<20} {(r.fields.invoice_number.value or '-'):<17} "
                   f"{(r.fields.disputed_amount.value or '-'):>10}  {(r.fusion_dispute_number or '-'):<10} {note[:70]}")
    return "\n".join(out)


def _approve_ready(agent, label: str = "") -> None:
    for rec in agent.store.list(DisputeStatus.IN_REVIEW):
        if any("Assist mode" in r for r in rec.review_reasons):
            agent.approve(rec.id, "L1")
            print(f"   approved {rec.id}{label} -> created in Fusion")
        elif not label:
            print(f"   left for L1 decision: {rec.id} ({'; '.join(rec.review_reasons)})")


def cmd_demo(args):
    work = Path(tempfile.mkdtemp(prefix="dispute-demo-"))
    inbox = work / "inbox"
    shutil.copytree(ROOT / "samples" / "emails", inbox)
    os.environ.update({"DB_PATH": str(work / "disputes.db"), "LOCAL_INBOX_DIR": str(inbox),
                       "LOCAL_OUTBOX_DIR": str(work / "outbox"), "AGENT_MODE": args.mode,
                       "MAILBOX_BACKEND": "local", "SOA_BACKEND": "mock",
                       "LLM_BACKEND": os.environ.get("LLM_BACKEND", "heuristic")})
    agent = build_agent(Settings())
    print(f"\n== Mode: {args.mode} | data in {work}\n")

    print("== 1. Agent reads the mailbox")
    agent.run_cycle()
    print(_table(agent))

    if args.mode != "shadow":
        print("\n== 2. L1 works the review queue")
        _approve_ready(agent)

        print("\n== 3. Customer replies with the missing details")
        shutil.copy(ROOT / "samples" / "followups" / "followup-missing-invoice.json", inbox / "zz-followup.json")
        agent.run_cycle()
        _approve_ready(agent, " after customer reply")

        print("\n== 4. Finance decides in the Fusion workflow (simulated)")
        for i, rec in enumerate(agent.store.list(DisputeStatus.CREATED)):
            approve = i % 2 == 0
            agent.soa.finance_decide(rec.fusion_dispute_id, approve,
                                     "Credit note issued for the overcharged amount" if approve
                                     else "Charges verified against the approved tariff")
            print(f"   {rec.fusion_dispute_number}: {'APPROVED' if approve else 'REJECTED'}")
        agent.run_cycle()

    print("\n== Final state")
    print(_table(agent))
    sent = sorted((work / "outbox").glob("*.json"))
    print(f"\n== {len(sent)} customer emails sent (in {work / 'outbox'}):")
    for p in sent:
        m = json.loads(p.read_text())
        print(f"   to {m['to']:<32} {m['subject']}")
    print("\n== Metrics:", agent.store.metrics())


def main(argv=None):
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="dispute-agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run-once")
    lp = sub.add_parser("loop"); lp.add_argument("--interval", type=int, default=60)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int, default=8080); sp.add_argument("--host", default="0.0.0.0")
    dp = sub.add_parser("demo"); dp.add_argument("--mode", default="assist", choices=["shadow", "assist", "autonomous"])
    sub.add_parser("list")
    fp = sub.add_parser("finance-decide")
    fp.add_argument("dispute_id")
    fp.add_argument("decision", choices=["approve", "reject"])
    fp.add_argument("detail")
    su = sub.add_parser("subscribe"); su.add_argument("--url", required=True)
    args = p.parse_args(argv)

    if args.cmd == "demo":
        logging.getLogger().setLevel(logging.WARNING)
        return cmd_demo(args)
    if args.cmd == "serve":
        import uvicorn
        return uvicorn.run("dispute_agent.api:app", host=args.host, port=args.port)

    agent = build_agent()
    if args.cmd == "run-once":
        print(agent.run_cycle())
    elif args.cmd == "loop":
        while True:
            try:
                logging.info("cycle %s", agent.run_cycle())
            except Exception:
                logging.exception("cycle failed")
            time.sleep(args.interval)
    elif args.cmd == "list":
        print(_table(agent))
    elif args.cmd == "finance-decide":
        agent.soa.finance_decide(args.dispute_id, args.decision == "approve", args.detail)
        print("ok")
    elif args.cmd == "subscribe":
        print(agent.mailbox.ensure_subscription(args.url, agent.settings.graph_webhook_client_state))


if __name__ == "__main__":
    main()
