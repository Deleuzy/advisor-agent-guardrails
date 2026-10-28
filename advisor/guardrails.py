"""Guardrails that run in code, around the model, never inside it.

input:  validate_input, detect_injection, Redactor.redact (before anything reaches the model)
scope:  clients_mentioned (one client per request, in the advisor's own book)
tools:  scan_tool_output (instructions hidden in data are removed before the model sees them)
output: check_note (no other client, no personal identifiers, every number traceable to a
        tool result, suitability rule respected)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_REQUEST_CHARS = 1000

CPR_RE = re.compile(r"\b\d{6}-?\d{4}\b")
ACCOUNT_RE = re.compile(r"\b\d{4}[ -]?\d{10}\b")
IBAN_RE = re.compile(r"\bDK\d{2}(?: ?\d{4}){3} ?\d{2}\b", re.I)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
PHONE_RE = re.compile(r"\+45 ?\d{2} ?\d{2} ?\d{2} ?\d{2}\b")
CLIENT_TOKEN_RE = re.compile(r"\[CLIENT_(\d+)\]")
CLIENT_REF_RE = re.compile(r"\b(?:client|customer|kunde)\s*(?:no\.?|number|id|nr\.?)?\s*#?\s*(\d{4})\b", re.I)

INJECTION_PATTERNS = [
    r"\b(ignore|disregard|forget|override)\b.{0,30}\b(instructions?|rules|prompt|guidelines|policy|policies|guardrails?)\b",
    r"\bsystem\s*prompt\b",
    r"\breveal\b.{0,30}\b(prompt|instructions|configuration)\b",
    r"\byou are now\b",
    r"\b(act|behave) as\b",
    r"\bpretend (to be|you are)\b",
    r"\b(admin|developer|debug|god)\s*mode\b",
    r"\bjailbreak\b",
    r"(^|\n|\.\s)\s*system\s*:",
    r"\bnew instructions\b",
    r"\bnote to (the )?(assistant|ai|model|llm)\b",
    r"\b(include|list|show|give|export|dump|send)\b.{0,25}\b(all|every|full|other)\b.{0,15}\b(clients?|customers?)\b",
    r"\b(client|customer) list\b",
    r"\bwithout (advisor )?(approval|review|checks?)\b",
    r"\b(skip|bypass|turn off|disable)\b.{0,20}\b(approval|review|checks?|guardrails?|filters?)\b",
    r"\bignorer\b|\bglem\b.{0,20}\binstruktioner\b",
    r"<\|.*?\|>|\[/?INST\]",
]
INJECTION_RE = [re.compile(p, re.I) for p in INJECTION_PATTERNS]


@dataclass
class Decision:
    check: str
    outcome: str          # pass | flagged | fail
    detail: str = ""

    def as_dict(self) -> dict:
        return {"check": self.check, "outcome": self.outcome, "detail": self.detail}


def validate_input(text: Any) -> str | None:
    """Return an error message, or None if the request text is acceptable."""
    if not isinstance(text, str) or not text.strip():
        return "empty request"
    if len(text) > MAX_REQUEST_CHARS:
        return f"request longer than {MAX_REQUEST_CHARS} characters"
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        return "control characters in request"
    return None


def detect_injection(text: str) -> str | None:
    for rx in INJECTION_RE:
        m = rx.search(text)
        if m:
            return m.group(0)
    return None


class Redactor:
    """Replaces client names and personal identifiers with placeholders.

    Names come from the client table, so a name is replaced wherever it appears, also in
    tool output and in the advisor's own request. The model works with [CLIENT_1042];
    the real name is put back only after the output checks have passed."""

    def __init__(self, clients: list[dict]):
        self.names = {c["client_id"]: c["name"] for c in clients}
        self._name_re = [(cid, re.compile(r"\b" + re.escape(n) + r"\b", re.I))
                         for cid, n in sorted(self.names.items(), key=lambda kv: -len(kv[1]))]

    def redact(self, text: str) -> tuple[str, dict[str, int]]:
        counts: dict[str, int] = {}

        def sub(rx, repl, s, key):
            s, n = rx.subn(repl, s)
            if n:
                counts[key] = counts.get(key, 0) + n
            return s

        text = sub(IBAN_RE, "[IBAN]", text, "iban")
        text = sub(ACCOUNT_RE, "[ACCOUNT]", text, "account")
        text = sub(CPR_RE, "[CPR]", text, "cpr")
        text = sub(EMAIL_RE, "[EMAIL]", text, "email")
        text = sub(PHONE_RE, "[PHONE]", text, "phone")
        for cid, rx in self._name_re:
            text = sub(rx, f"[CLIENT_{cid}]", text, "name")
        return text, counts

    def redact_obj(self, obj: Any, counts: dict[str, int] | None = None) -> Any:
        counts = {} if counts is None else counts
        if isinstance(obj, str):
            out, c = self.redact(obj)
            for k, v in c.items():
                counts[k] = counts.get(k, 0) + v
            return out
        if isinstance(obj, list):
            return [self.redact_obj(v, counts) for v in obj]
        if isinstance(obj, dict):
            return {k: self.redact_obj(v, counts) for k, v in obj.items()}
        return obj

    def restore(self, text: str, client_id: int) -> str:
        return text.replace(f"[CLIENT_{client_id}]", self.names[client_id])


def clients_mentioned(redacted_text: str) -> set[int]:
    """Client ids referred to by placeholder (a name was redacted) or as 'client 1042'."""
    ids = {int(m) for m in CLIENT_TOKEN_RE.findall(redacted_text)}
    ids |= {int(m) for m in CLIENT_REF_RE.findall(redacted_text)}
    return ids


REMOVED = "[removed: suspected instruction in data]"


def scan_tool_output(obj: Any, path: str = "") -> tuple[Any, list[str]]:
    """Replace any string field that looks like an instruction. Returns (clean, flagged paths)."""
    if isinstance(obj, str):
        return (REMOVED, [path]) if detect_injection(obj) else (obj, [])
    if isinstance(obj, list):
        out, flagged = [], []
        for i, v in enumerate(obj):
            c, f = scan_tool_output(v, f"{path}[{i}]")
            out.append(c)
            flagged += f
        return out, flagged
    if isinstance(obj, dict):
        out, flagged = {}, []
        for k, v in obj.items():
            c, f = scan_tool_output(v, f"{path}.{k}" if path else k)
            out[k] = c
            flagged += f
        return out, flagged
    return obj, []


# ---------- output checks ----------

NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _norm(num: str) -> str:
    num = num.replace(",", "")
    if "." in num:
        num = num.rstrip("0").rstrip(".")
    return num.lstrip("0") or "0"


def allowed_numbers(*sources: Any) -> set[str]:
    """Every number that appears anywhere in the tool results (and the request)."""
    out: set[str] = set()

    def walk(o):
        if isinstance(o, bool):
            return
        if isinstance(o, (int, float)):
            out.add(_norm(str(o)))
            out.add(_norm(f"{o:.1f}"))
            out.add(_norm(str(abs(o))))
            out.add(_norm(f"{abs(o):.1f}"))
            out.add(_norm(str(round(abs(o)))))
        elif isinstance(o, str):
            out.update(_norm(n) for n in NUMBER_RE.findall(o))
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for s in sources:
        walk(s)
    return out


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


@dataclass
class NoteContext:
    client_id: int
    all_client_ids: set[int]
    all_client_names: dict[int, str]
    catalog: list[dict]                      # product_id, name, risk_class
    facts: list[dict]                        # [{"tool", "args", "result"}] as given to the model
    request_text: str = ""
    decisions: list[Decision] = field(default_factory=list)


def check_note(note: str, ctx: NoteContext) -> list[Decision]:
    """Run every output check on the (still redacted) note. Any 'fail' means refusal."""
    out: list[Decision] = []

    # 1. no other client's data: placeholders, ids, names
    other_tokens = {int(x) for x in CLIENT_TOKEN_RE.findall(note)} - {ctx.client_id}
    other_ids = {int(x) for x in re.findall(r"\b(\d{4})\b", note)} & (ctx.all_client_ids - {ctx.client_id})
    other_names = [n for cid, n in ctx.all_client_names.items()
                   if cid != ctx.client_id and re.search(r"\b" + re.escape(n) + r"\b", note, re.I)]
    leaks = sorted(other_tokens | other_ids)
    if leaks or other_names:
        out.append(Decision("other_client_data", "fail",
                            f"note mentions other clients: {leaks + other_names}"))
    else:
        out.append(Decision("other_client_data", "pass"))

    # 2. no personal identifiers in the note at all
    pii = [k for k, rx in (("cpr", CPR_RE), ("account", ACCOUNT_RE), ("iban", IBAN_RE), ("email", EMAIL_RE))
           if rx.search(note)]
    out.append(Decision("personal_identifiers", "fail", f"found {pii}") if pii
               else Decision("personal_identifiers", "pass"))

    # 3. every number must come from a tool result (or the advisor's request)
    stripped = CLIENT_TOKEN_RE.sub("", note)
    allowed = allowed_numbers([f["result"] for f in ctx.facts], [f["args"] for f in ctx.facts],
                              ctx.request_text)
    unknown = sorted({n for n in NUMBER_RE.findall(stripped) if _norm(n) not in allowed})
    out.append(Decision("numbers_match_tools", "fail", f"numbers not in any tool result: {unknown}") if unknown
               else Decision("numbers_match_tools", "pass"))

    # 4. suitability: a product the client does not hold may only be named after a suitability
    #    check, and an unsuitable product may only be named as not suitable, never recommended.
    held = {h["product_id"] for f in ctx.facts if f["tool"] == "get_portfolio"
            for h in f["result"].get("holdings", [])}
    checked = {f["result"]["product_id"]: f["result"]["suitable"] for f in ctx.facts
               if f["tool"] == "check_suitability"}
    problems = []
    for sent in _sentences(note):
        low = sent.lower()
        for p in ctx.catalog:
            if p["name"].lower() not in low:
                continue
            pid = p["product_id"]
            if pid not in checked:
                if pid not in held:
                    problems.append(f"{p['name']} named without a suitability check")
                elif re.search(r"\b(recommend|suggest|propose|increase|add|buy)\w*", low):
                    problems.append(f"{p['name']} proposed without a suitability check")
            elif not checked[pid]:
                says_not = "not suitable" in low or "unsuitable" in low
                recommends = re.search(r"\b(we|i) (recommend|suggest|propose)\b", low) and "not" not in low
                if not says_not or recommends:
                    problems.append(f"{p['name']} failed the suitability check but is presented as suitable")
    out.append(Decision("suitability_rule", "fail", "; ".join(sorted(set(problems)))) if problems
               else Decision("suitability_rule", "pass"))
    return out
