"""Tools the executor may call. Each reads the synthetic database for exactly one client.

Tools never return CPR or account numbers: the note does not need them, so they never
leave the database (data minimisation before any redaction is needed).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from .data import AS_OF

RECENT_DAYS = 90


class ToolError(Exception):
    pass


def _client(con: sqlite3.Connection, client_id: int) -> sqlite3.Row:
    row = con.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()
    if row is None:
        raise ToolError(f"unknown client {client_id}")
    return row


def _product(con: sqlite3.Connection, product_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()
    if row is None:
        raise ToolError(f"unknown product {product_id}")
    return row


def get_risk_profile(con, client_id: int) -> dict:
    c = _client(con, client_id)
    return {"client_id": c["client_id"], "client_name": c["name"], "risk_profile": c["risk_profile"],
            "risk_scale_max": 7, "horizon_years": c["horizon_years"],
            "complex_experience": bool(c["complex_experience"])}


def get_portfolio(con, client_id: int) -> dict:
    _client(con, client_id)
    rows = con.execute("""SELECT h.product_id, p.name, p.type, p.risk_class, h.value_dkk
                          FROM holdings h JOIN products p USING(product_id)
                          WHERE h.client_id = ? ORDER BY h.value_dkk DESC""", (client_id,)).fetchall()
    total = sum(r["value_dkk"] for r in rows)
    holdings = [{"product_id": r["product_id"], "product_name": r["name"], "type": r["type"],
                 "risk_class": r["risk_class"], "value_dkk": r["value_dkk"],
                 "weight_pct": round(100 * r["value_dkk"] / total, 1)} for r in rows]
    weighted = round(sum(r["risk_class"] * r["value_dkk"] for r in rows) / total, 1)
    return {"client_id": client_id, "total_value_dkk": total, "holding_count": len(holdings), "holdings": holdings,
            "weighted_risk_class": weighted}


def get_recent_transactions(con, client_id: int, days: int = RECENT_DAYS) -> dict:
    _client(con, client_id)
    since = (AS_OF - timedelta(days=days)).isoformat()
    rows = con.execute("""SELECT date, amount_dkk, memo FROM transactions
                          WHERE client_id = ? AND date >= ? ORDER BY date DESC""",
                       (client_id, since)).fetchall()
    tx = [dict(r) for r in rows]
    return {"client_id": client_id, "days": days, "count": len(tx),
            "net_flow_dkk": sum(t["amount_dkk"] for t in tx), "transactions": tx}


def check_suitability(con, client_id: int, product_id: str) -> dict:
    """The suitability rule, in code: risk class within profile, complex products only with
    documented experience, horizon at least the product's minimum."""
    c, p = _client(con, client_id), _product(con, product_id)
    reasons = []
    if p["risk_class"] > c["risk_profile"]:
        reasons.append(f"risk class {p['risk_class']} is above the client's risk profile {c['risk_profile']}")
    if p["complex"] and not c["complex_experience"]:
        reasons.append("complex product and the client has no documented experience with complex products")
    if c["horizon_years"] < p["min_horizon_years"]:
        reasons.append(f"horizon {c['horizon_years']} years is shorter than the product's minimum "
                       f"{p['min_horizon_years']} years")
    return {"client_id": client_id, "product_id": product_id, "product_name": p["name"],
            "product_risk_class": p["risk_class"], "client_risk_profile": c["risk_profile"],
            "suitable": not reasons, "reasons": reasons}


@dataclass(frozen=True)
class Tool:
    name: str
    fn: Callable[..., dict]
    args: dict[str, type]          # required argument name -> type
    optional: dict[str, type]


TOOLS: dict[str, Tool] = {
    "get_risk_profile": Tool("get_risk_profile", get_risk_profile, {"client_id": int}, {}),
    "get_portfolio": Tool("get_portfolio", get_portfolio, {"client_id": int}, {}),
    "get_recent_transactions": Tool("get_recent_transactions", get_recent_transactions,
                                    {"client_id": int}, {"days": int}),
    "check_suitability": Tool("check_suitability", check_suitability,
                              {"client_id": int, "product_id": str}, {}),
}
# draft_client_note is a tool in the plan, but the executor runs it itself: it calls the model
# with the redacted facts gathered so far and then runs the output checks.
NOTE_TOOL = "draft_client_note"
ALL_TOOL_NAMES = set(TOOLS) | {NOTE_TOOL}


def validate_args(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Reject unknown tools, missing or unexpected arguments and wrong types."""
    if tool == NOTE_TOOL:
        if set(args) - {"client_id"}:
            raise ToolError(f"unexpected arguments for {tool}: {sorted(set(args) - {'client_id'})}")
        return args
    if tool not in TOOLS:
        raise ToolError(f"unknown tool {tool!r}")
    spec = TOOLS[tool]
    allowed = {**spec.args, **spec.optional}
    extra = set(args) - set(allowed)
    if extra:
        raise ToolError(f"unexpected arguments for {tool}: {sorted(extra)}")
    for name, typ in spec.args.items():
        if name not in args:
            raise ToolError(f"missing argument {name!r} for {tool}")
    for name, value in args.items():
        if not isinstance(value, allowed[name]) or isinstance(value, bool):
            raise ToolError(f"argument {name!r} for {tool} must be {allowed[name].__name__}")
    if "days" in args and not 1 <= args["days"] <= 365:
        raise ToolError("days must be between 1 and 365")
    return args


def product_catalog(con) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT product_id, name, risk_class FROM products")]

