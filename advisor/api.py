"""REST API: advisors submit requests and approve or reject drafts.

The advisor id comes from the X-Advisor-Id header. That is a stand-in for real
authentication (SSO in a bank); the authorisation rules behind it are real: an advisor
only reaches clients in their own book and only approves drafts for those clients.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .agent import AdvisorAgent

app = FastAPI(title="advisor-agent-guardrails", version="0.1.0")
agent = AdvisorAgent(audit_path=os.environ.get("ADVISOR_AUDIT_LOG", "logs/audit.jsonl"))


class AdvisorRequest(BaseModel):
    text: str = Field(..., max_length=4000)


class Decision(BaseModel):
    comment: str | None = Field(None, max_length=500)


@app.get("/health")
def health():
    return {"status": "ok", "model": agent.llm.name}


@app.post("/requests")
def create(body: AdvisorRequest, x_advisor_id: str = Header(...)):
    rec = agent.handle(x_advisor_id, body.text)
    return rec.public()


@app.get("/requests/{request_id}")
def get(request_id: str, x_advisor_id: str = Header(...)):
    rec = agent.requests.get(request_id)
    if rec is None or rec.advisor_id != x_advisor_id:
        raise HTTPException(404, "not found")
    return rec.public()


def _decide(request_id: str, advisor_id: str, approve: bool, comment: str | None):
    try:
        return agent.decide(request_id, advisor_id, approve, comment).public()
    except KeyError:
        raise HTTPException(404, "not found")
    except PermissionError as e:
        raise HTTPException(403 if "does not own" in str(e) else 409, str(e))


@app.post("/requests/{request_id}/approve")
def approve(request_id: str, body: Decision, x_advisor_id: str = Header(...)):
    return _decide(request_id, x_advisor_id, True, body.comment)


@app.post("/requests/{request_id}/reject")
def reject(request_id: str, body: Decision, x_advisor_id: str = Header(...)):
    return _decide(request_id, x_advisor_id, False, body.comment)


@app.get("/audit/verify")
def verify():
    ok, lines, msg = agent.audit.verify()
    return {"ok": ok, "lines": lines, "message": msg}
