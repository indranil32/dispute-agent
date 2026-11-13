"""Review API + review screen for L1, Graph webhook, health and metrics.

Run: uvicorn dispute_agent.api:app --port 8080   (or: dispute-agent serve)
In Azure, put this behind Entra ID authentication (Container Apps "Easy Auth"); REVIEW_API_KEY is a
simple extra guard for non-browser callers.
"""
from __future__ import annotations

import os
import threading
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from .factory import build_agent
from .models import DisputeStatus

app = FastAPI(title="Dispute AI Agent", version="0.1.0")
_agent = None
_cycle_lock = threading.Lock()


def agent():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


def check_key(x_api_key: Optional[str] = Header(default=None)):
    expected = os.environ.get("REVIEW_API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(401, "invalid API key")


class ApproveBody(BaseModel):
    reviewer: str
    edits: Optional[dict] = None


class RejectBody(BaseModel):
    reviewer: str
    note: str = ""
    reply: str = "none"  # none | not_a_dispute | ask_details


def _view(rec):
    d = rec.model_dump(mode="json")
    d["status"] = rec.status.value
    return d


@app.get("/health")
def health():
    return {"status": "ok", "mode": agent().mode}


@app.get("/metrics", dependencies=[Depends(check_key)])
def metrics():
    return agent().store.metrics()


@app.get("/review", dependencies=[Depends(check_key)])
def review_queue():
    return [_view(r) for r in agent().store.list(DisputeStatus.IN_REVIEW)]


@app.get("/disputes", dependencies=[Depends(check_key)])
def disputes(status: Optional[str] = None):
    st = DisputeStatus(status) if status else None
    return [_view(r) for r in agent().store.list(st)]


@app.get("/disputes/{dispute_id}", dependencies=[Depends(check_key)])
def dispute(dispute_id: str):
    a = agent()
    rec = a.store.get(dispute_id)
    if not rec:
        raise HTTPException(404)
    return {"dispute": _view(rec), "emails": [e.model_dump(mode="json") for e in a.store.emails_for(dispute_id)],
            "audit": a.store.audit_for(dispute_id)}


@app.post("/review/{dispute_id}/approve", dependencies=[Depends(check_key)])
def approve(dispute_id: str, body: ApproveBody):
    try:
        return _view(agent().approve(dispute_id, body.reviewer, body.edits))
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/review/{dispute_id}/reject", dependencies=[Depends(check_key)])
def reject(dispute_id: str, body: RejectBody):
    try:
        return _view(agent().reject(dispute_id, body.reviewer, body.note, body.reply))
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/run-cycle", dependencies=[Depends(check_key)])
def run_cycle():
    with _cycle_lock:
        return agent().run_cycle()


@app.post("/graph/notifications")
async def graph_notifications(request: Request, background: BackgroundTasks, validationToken: Optional[str] = None):
    """Microsoft Graph change notifications for new inbox messages."""
    if validationToken:  # subscription validation handshake
        return PlainTextResponse(validationToken)
    body = await request.json()
    expected = os.environ.get("GRAPH_WEBHOOK_CLIENT_STATE")
    if expected and any(n.get("clientState") != expected for n in body.get("value", [])):
        raise HTTPException(403, "clientState mismatch")

    def _run():
        with _cycle_lock:
            agent().process_inbox()

    background.add_task(_run)
    return PlainTextResponse("", status_code=202)


@app.get("/", response_class=HTMLResponse)
def review_page():
    return REVIEW_HTML


REVIEW_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dispute review queue</title>
<style>
:root{--ink:#14213D;--muted:#4A5568;--line:#D9DEE4;--teal:#0B6E8C;--orange:#B4530A;--bg:#FBFBF8}
body{font-family:Segoe UI,Arial,sans-serif;color:var(--ink);background:var(--bg);margin:0;padding:24px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted);margin-bottom:20px}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:14px}
.why{color:var(--orange);font-weight:600;margin:6px 0}
table{border-collapse:collapse;margin:8px 0}td{padding:4px 10px 4px 0;vertical-align:top}
input{font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:6px;width:220px}
button{font:inherit;border:0;border-radius:6px;padding:7px 14px;cursor:pointer;margin-right:6px}
.ok{background:var(--teal);color:#fff}.no{background:#eee}.body{white-space:pre-wrap;color:var(--muted);font-size:13px;max-height:140px;overflow:auto;border-top:1px solid var(--line);padding-top:8px}
</style></head><body>
<h1>Dispute review queue</h1><div class="sub">Approve to create the dispute in Fusion via SOA. Edit fields first if the agent got them wrong.</div>
<div id="q">Loading...</div>
<script>
const F=['ucid','invoice_number','disputed_amount','currency','reason'];
async function load(){const r=await fetch('/review');const items=await r.json();const q=document.getElementById('q');
if(!items.length){q.textContent='Nothing to review.';return}
q.innerHTML='';for(const d of items){const det=await (await fetch('/disputes/'+d.id)).json();const em=det.emails.at(-1)||{};
const c=document.createElement('div');c.className='card';
c.innerHTML=`<b>${d.id}</b> &middot; ${d.sender} &middot; ${d.subject}<div class="why">${(d.review_reasons||[]).join('; ')}</div>
<table>${F.map(f=>`<tr><td>${f}</td><td><input data-f="${f}" value="${(d.fields[f]||{}).value??''}"></td><td style="color:#888">conf ${((d.fields[f]||{}).confidence??0).toFixed(2)}</td></tr>`).join('')}</table>
<button class="ok">Approve and create in Fusion</button><button class="no" data-r="ask_details">Ask customer for details</button><button class="no" data-r="not_a_dispute">Not a dispute</button>
<div class="body">${(em.body||'').replace(/</g,'&lt;')}</div>`;
c.querySelector('.ok').onclick=async()=>{const edits={};c.querySelectorAll('input').forEach(i=>{const o=(d.fields[i.dataset.f]||{}).value??'';if(i.value!==o)edits[i.dataset.f]=i.value});
const r=await fetch(`/review/${d.id}/approve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reviewer:'L1',edits})});alert(r.ok?'Result: '+(await r.json()).status:await r.text());load()};
c.querySelectorAll('.no').forEach(b=>b.onclick=async()=>{await fetch(`/review/${d.id}/reject`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reviewer:'L1',reply:b.dataset.r})});load()});
q.appendChild(c)}}
load();
</script></body></html>"""
