import pytest

from advisor.agent import AdvisorAgent


@pytest.fixture
def agent(tmp_path):
    return AdvisorAgent(audit_path=tmp_path / "audit.jsonl")
