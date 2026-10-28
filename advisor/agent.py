"""Planner and executor with guardrails around every step, plus the approval workflow.

    request -> validate -> injection check -> redact -> scope (one client, own book)
            -> plan (model) -> validate plan -> execute tools -> scan tool output
            -> redact facts -> draft note (model) -> output checks
            -> pending_approval  |  refused
    pending_approval -> approved | rejected   (by an advisor who owns the client)
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import data as data_mod
from .audit import AuditLog, sha256_json
from .guardrails import (Decision, NoteContext, Redactor, check_note, clients_mentioned,
                         detect_injection, scan_tool_output, validate_input)
from .llm import MockLLM, cost_usd, select_llm
from .tools import NOTE_TOOL, TOOLS, ToolError, product_catalog, validate_args

MAX_STEPS = 8

PENDING, APPROVED, REJECTED, REFUSED = "pending_approval", "approved", "rejected", "refused"


@dataclass
class RequestRecord:
    request_id: str
    advisor_id: str
    status: str = REFUSED
    client_id: int | None = None
    request_redacted: str = ""
    plan: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    guardrails: list[dict] = field(default_factory=list)
    note: str | None = None
    refusal_reason: str | None = None
    model: str = ""
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = 0.0
    approver: str | None = None
    decided_at: str | None = None
    decision_comment: str | None = None
    # what the model was sent, kept in memory only (for the evaluation's leakage test), never logged
    model_inputs: list[str] = field(default_factory=list, repr=False)

    def public(self) -> dict:
        d = asdict(self)
        d.pop("model_inputs")
        return d


class Refusal(Exception):
    def __init__(self, check: str, reason: str):
        super().__init__(reason)
        self.check, self.reason = check, reason


class AdvisorAgent:
    def __init__(self, llm=None, audit_path: Path | str = "logs/audit.jsonl", data_path: Path | None = None):
        self.con = data_mod.connect(data_path or data_mod.DATA_PATH)
        self.catalog = product_catalog(self.con)
        clients = [dict(r) for r in self.con.execute("SELECT client_id, name, advisor_id FROM clients")]
        self.client_names = {c["client_id"]: c["name"] for c in clients}
        self.book = {c["client_id"]: c["advisor_id"] for c in clients}
        self.advisors = {r[0] for r in self.con.execute("SELECT advisor_id FROM advisors")}
        self.redactor = Redactor(clients)
        self.llm = llm or select_llm(self.catalog)
        self.audit = AuditLog(audit_path)
        self.requests: dict[str, RequestRecord] = {}

    # ---------- request ----------

    def handle(self, advisor_id: str, text: str) -> RequestRecord:
        rec = RequestRecord(request_id=uuid.uuid4().hex[:12], advisor_id=advisor_id, model=self.llm.name)
        t0 = time.perf_counter()
        try:
            self._run(rec, text)
            rec.status = PENDING
        except Refusal as r:
            rec.status, rec.refusal_reason = REFUSED, f"{r.check}: {r.reason}"
            rec.note = None
            if not any(g["check"] == r.check and g["outcome"] == "fail" for g in rec.guardrails):
                rec.guardrails.append(Decision(r.check, "fail", r.reason).as_dict())
        rec.latency_ms = round((time.perf_counter() - t0) * 1000, 3)
        rec.cost_usd = 0.0 if isinstance(self.llm, MockLLM) or self.llm.name.startswith("mock") \
            else cost_usd(rec.tokens_in, rec.tokens_out)
        self.requests[rec.request_id] = rec
        self.audit.append("request", {
            "request_id": rec.request_id, "advisor_id": advisor_id, "client_id": rec.client_id,
            "status": rec.status, "request_redacted": rec.request_redacted, "plan": rec.plan,
            "tool_calls": rec.tool_calls, "guardrails": rec.guardrails, "refusal_reason": rec.refusal_reason,
            "note_redacted_sha256": sha256_json(rec.note) if rec.note else None,
            "model": rec.model, "latency_ms": rec.latency_ms, "tokens_in": rec.tokens_in,
            "tokens_out": rec.tokens_out, "cost_usd": rec.cost_usd})
        return rec

    def _decide(self, rec: RequestRecord, check: str, outcome: str, detail: str = "") -> None:
        rec.guardrails.append(Decision(check, outcome, detail).as_dict())

    def _run(self, rec: RequestRecord, text: str) -> None:
        # 1. input validation
        if rec.advisor_id not in self.advisors:
            raise Refusal("input_validation", f"unknown advisor {rec.advisor_id!r}")
        err = validate_input(text)
        if err:
            raise Refusal("input_validation", err)
        self._decide(rec, "input_validation", "pass")

        # 2. prompt injection in the request
        hit = detect_injection(text)
        if hit:
            raise Refusal("prompt_injection_input", f"matched {hit!r}")
        self._decide(rec, "prompt_injection_input", "pass")

        # 3. redaction before anything reaches the model
        redacted, counts = self.redactor.redact(text)
        rec.request_redacted = redacted
        self._decide(rec, "pii_redaction_input", "pass", json.dumps(counts) if counts else "nothing to redact")

        # 4. scope: exactly one client, and it must be in this advisor's book
        ids = clients_mentioned(redacted)
        if len(ids) != 1:
            raise Refusal("client_scope", "a request must name exactly one client" if not ids
                          else f"request refers to {len(ids)} clients; one client per request")
        cid = ids.pop()
        if cid not in self.book:
            raise Refusal("client_scope", f"unknown client {cid}")
        if self.book[cid] != rec.advisor_id:
            raise Refusal("client_scope", f"client {cid} is not in advisor {rec.advisor_id}'s book")
        rec.client_id = cid
        self._decide(rec, "client_scope", "pass", f"client {cid}")

        # 5. plan (model output is untrusted: parse and validate every step)
        rec.model_inputs.append(redacted)
        c = self.llm.plan(redacted)
        rec.tokens_in += c.tokens_in
        rec.tokens_out += c.tokens_out
        try:
            steps = json.loads(c.text).get("steps", [])
            assert isinstance(steps, list)
        except Exception:
            raise Refusal("plan_validation", "planner did not return a valid plan")
        if not steps:
            raise Refusal("plan_validation", "no plan for this request (supported: client notes, portfolio, "
                                             "transactions, suitability of a named product)")
        if len(steps) > MAX_STEPS:
            raise Refusal("plan_validation", f"plan has {len(steps)} steps, limit {MAX_STEPS}")
        for s in steps:
            if not isinstance(s, dict) or not isinstance(s.get("args", {}), dict):
                raise Refusal("plan_validation", "malformed step")
            try:
                validate_args(s.get("tool", ""), s.get("args", {}))
            except ToolError as e:
                raise Refusal("plan_validation", str(e))
            if s["args"].get("client_id") != cid:
                raise Refusal("client_scope", f"plan step {s['tool']} targets client "
                                              f"{s['args'].get('client_id')}, request is about {cid}")
        if steps[-1]["tool"] != NOTE_TOOL or sum(s["tool"] == NOTE_TOOL for s in steps) != 1:
            raise Refusal("plan_validation", f"plan must end with a single {NOTE_TOOL} step")
        rec.plan = steps
        self._decide(rec, "plan_validation", "pass", f"{len(steps)} steps")

        # 6. execute tools; scan output for instructions hidden in data
        facts = []
        for s in steps[:-1]:
            try:
                result = TOOLS[s["tool"]].fn(self.con, **s["args"])
            except ToolError as e:
                raise Refusal("tool_error", str(e))
            clean, flagged = scan_tool_output(result)
            rec.tool_calls.append({"tool": s["tool"], "args": s["args"], "result_sha256": sha256_json(result),
                                   "flagged_fields": flagged})
            if flagged:
                self._decide(rec, "prompt_injection_tool_output", "flagged",
                             f"{s['tool']}: removed {', '.join(flagged)}")
            facts.append({"tool": s["tool"], "args": s["args"], "result": clean})

        # 7. redact the facts, then draft the note
        counts: dict[str, int] = {}
        facts_redacted = self.redactor.redact_obj(facts, counts)
        self._decide(rec, "pii_redaction_facts", "pass", json.dumps(counts) if counts else "nothing to redact")
        rec.model_inputs.append(redacted + json.dumps(facts_redacted, ensure_ascii=False))
        c = self.llm.write_note(redacted, facts_redacted)
        rec.tokens_in += c.tokens_in
        rec.tokens_out += c.tokens_out
        note = c.text.strip()

        # 8. output checks in code; any failure means refusal
        ctx = NoteContext(client_id=cid, all_client_ids=set(self.book), all_client_names=self.client_names,
                          catalog=self.catalog, facts=facts_redacted, request_text=redacted)
        failed = []
        for d in check_note(note, ctx):
            rec.guardrails.append(d.as_dict())
            if d.outcome == "fail":
                failed.append(d)
        rec.tool_calls.append({"tool": NOTE_TOOL, "args": {"client_id": cid}, "result_sha256": sha256_json(note),
                               "flagged_fields": []})
        if failed:
            raise Refusal(failed[0].check, "; ".join(d.detail if d is failed[0] else f"{d.check}: {d.detail}"
                                                     for d in failed))
        rec.note = self.redactor.restore(note, cid)

    # ---------- approval ----------

    def decide(self, request_id: str, advisor_id: str, approve: bool, comment: str | None = None) -> RequestRecord:
        rec = self.requests.get(request_id)
        if rec is None:
            raise KeyError(request_id)
        if rec.status != PENDING:
            raise PermissionError(f"request is {rec.status}; only pending requests can be decided")
        if self.book.get(rec.client_id) != advisor_id:
            raise PermissionError(f"advisor {advisor_id} does not own client {rec.client_id}")
        rec.status = APPROVED if approve else REJECTED
        rec.approver = advisor_id
        rec.decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rec.decision_comment = comment
        self.audit.append("decision", {"request_id": request_id, "status": rec.status, "approver": advisor_id,
                                       "comment": comment,
                                       "note_sha256": sha256_json(self.redactor.redact(rec.note)[0])})
        return rec
