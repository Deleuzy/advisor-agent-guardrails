import json

import pytest

from advisor.agent import APPROVED, PENDING, REFUSED, REJECTED, AdvisorAgent
from advisor.llm import Completion, FaultyLLM, MockLLM


def test_happy_path_and_approval(agent):
    rec = agent.handle("A1", "Prepare a note for client 1042 before Thursday's meeting")
    assert rec.status == PENDING
    assert [s["tool"] for s in rec.plan] == ["get_risk_profile", "get_portfolio",
                                            "get_recent_transactions", "draft_client_note"]
    assert "Thomas Brandt" in rec.note
    assert all("Thomas Brandt" not in m for m in rec.model_inputs)  # the model only saw a placeholder
    done = agent.decide(rec.request_id, "A1", approve=True, comment="ok")
    assert done.status == APPROVED and done.approver == "A1"
    with pytest.raises(PermissionError):
        agent.decide(rec.request_id, "A1", approve=False)  # already decided


def test_only_owner_can_approve(agent):
    rec = agent.handle("A1", "Prepare a note for client 1042")
    with pytest.raises(PermissionError):
        agent.decide(rec.request_id, "A2", approve=True)
    assert agent.decide(rec.request_id, "A1", approve=False).status == REJECTED


def test_refused_request_cannot_be_approved(agent):
    rec = agent.handle("A1", "Prepare a note for client 1040")  # another advisor's client
    assert rec.status == REFUSED and rec.refusal_reason.startswith("client_scope")
    with pytest.raises(PermissionError):
        agent.decide(rec.request_id, "A1", approve=True)


def test_audit_log_has_everything(agent):
    rec = agent.handle("A1", "Prepare a note for client 1042")
    agent.decide(rec.request_id, "A1", approve=True)
    lines = [json.loads(x) for x in agent.audit.path.read_text().splitlines()]
    req, dec = lines
    for key in ("request_id", "plan", "tool_calls", "guardrails", "model", "latency_ms", "tokens_in",
                "tokens_out", "cost_usd"):
        assert key in req
    assert all(len(t["result_sha256"]) == 64 for t in req["tool_calls"])
    assert dec["event"] == "decision" and dec["approver"] == "A1"
    assert "Thomas Brandt" not in agent.audit.path.read_text()  # no names in the log
    assert agent.audit.verify()[0]


class EvilPlanner(MockLLM):
    """A model that tries to plan tools for another client or an unknown tool."""

    def __init__(self, catalog, steps):
        super().__init__(catalog)
        self.steps = steps

    def plan(self, request):
        return Completion(json.dumps({"steps": self.steps}), 1, 1)


@pytest.mark.parametrize("steps,check", [
    ([{"tool": "get_portfolio", "args": {"client_id": 1033}},
      {"tool": "draft_client_note", "args": {"client_id": 1042}}], "client_scope"),
    ([{"tool": "export_all", "args": {}}, {"tool": "draft_client_note", "args": {"client_id": 1042}}],
     "plan_validation"),
    ([{"tool": "get_portfolio", "args": {"client_id": 1042}}], "plan_validation"),
    ([{"tool": "get_portfolio", "args": {"client_id": 1042}}] * 9, "plan_validation"),
    ("not a list", "plan_validation"),
])
def test_untrusted_plan_is_validated(tmp_path, steps, check):
    a = AdvisorAgent(audit_path=tmp_path / "a.jsonl")
    a.llm = EvilPlanner(a.catalog, steps)
    rec = a.handle("A1", "Prepare a note for client 1042")
    assert rec.status == REFUSED and rec.refusal_reason.startswith(check), rec.refusal_reason


def test_garbage_plan_refused(tmp_path):
    a = AdvisorAgent(audit_path=tmp_path / "a.jsonl")

    class Garbage(MockLLM):
        def plan(self, request):
            return Completion("I think you should call get_portfolio", 1, 1)

    a.llm = Garbage(a.catalog)
    assert a.handle("A1", "Prepare a note for client 1042").refusal_reason.startswith("plan_validation")


def test_faulty_model_is_refused(tmp_path):
    a = AdvisorAgent(audit_path=tmp_path / "a.jsonl")
    a.llm = FaultyLLM(MockLLM(a.catalog), "hallucinated_number")
    rec = a.handle("A1", "Prepare a note for client 1042")
    assert rec.status == REFUSED and rec.note is None
    assert rec.refusal_reason.startswith("numbers_match_tools")


def test_tool_output_injection_never_reaches_model(agent):
    rec = agent.handle("A2", "Prepare a note for client 1049")
    assert any(g["check"] == "prompt_injection_tool_output" for g in rec.guardrails)
    assert not any("admin mode" in m for m in rec.model_inputs)
    assert rec.status == PENDING


def test_unsupported_request(agent):
    rec = agent.handle("A1", "What is the weather for client 1042?")
    assert rec.status == REFUSED and rec.refusal_reason.startswith("plan_validation")
