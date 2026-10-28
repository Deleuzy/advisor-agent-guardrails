import pytest

from advisor import data
from advisor.tools import (ToolError, check_suitability, get_portfolio, get_recent_transactions,
                           get_risk_profile, validate_args)


@pytest.fixture(scope="module")
def con():
    return data.connect()


def test_dataset_is_reproducible(tmp_path):
    a = data.write(tmp_path / "a.json", seed=42).read_text()
    assert a == data.DATA_PATH.read_text(encoding="utf-8")


def test_tools_never_return_identifiers(con):
    for fn in (get_risk_profile, get_portfolio, get_recent_transactions):
        out = str(fn(con, 1042))
        assert "cpr" not in out.lower() and "account_no" not in out


def test_portfolio_weights_sum_to_100(con):
    p = get_portfolio(con, 1042)
    assert abs(sum(h["weight_pct"] for h in p["holdings"]) - 100) < 0.5
    assert p["holding_count"] == len(p["holdings"])


@pytest.mark.parametrize("cid,pid,suitable,reason", [
    (1045, "P07", False, "risk class 5 is above"),     # risk profile 1
    (1042, "P06", True, None),
    (1042, "P10", False, "complex product"),           # no complex experience
    (1049, "P06", False, "horizon 3 years"),           # horizon too short
    (1048, "P10", True, None),                         # experienced, long horizon
])
def test_suitability_rule(con, cid, pid, suitable, reason):
    r = check_suitability(con, cid, pid)
    assert r["suitable"] is suitable
    if reason:
        assert any(reason in x for x in r["reasons"])


def test_recent_window(con):
    assert get_recent_transactions(con, 1042, 30)["count"] <= get_recent_transactions(con, 1042, 120)["count"]


@pytest.mark.parametrize("tool,args", [
    ("delete_client", {"client_id": 1042}),
    ("get_portfolio", {}),
    ("get_portfolio", {"client_id": "1042"}),
    ("get_portfolio", {"client_id": True}),
    ("get_portfolio", {"client_id": 1042, "all_clients": True}),
    ("get_recent_transactions", {"client_id": 1042, "days": 5000}),
])
def test_validate_args_rejects(tool, args):
    with pytest.raises(ToolError):
        validate_args(tool, args)


def test_unknown_client_and_product(con):
    with pytest.raises(ToolError):
        get_portfolio(con, 9999)
    with pytest.raises(ToolError):
        check_suitability(con, 1042, "P99")
