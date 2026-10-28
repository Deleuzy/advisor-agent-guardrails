"""Append-only audit log (JSONL). Each line carries the hash of the previous line, so an
edited or deleted line breaks the chain and verify() reports where."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def sha256_json(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last = self._tail_hash()

    def _tail_hash(self) -> str:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return GENESIS
        last = self.path.read_text(encoding="utf-8").rstrip("\n").rsplit("\n", 1)[-1]
        return json.loads(last)["hash"]

    def append(self, event: str, payload: dict) -> dict:
        with self._lock:
            entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                     "event": event, "prev_hash": self._last, **payload}
            entry["hash"] = sha256_json(entry)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._last = entry["hash"]
            return entry

    def verify(self) -> tuple[bool, int, str]:
        """Returns (ok, lines checked, message)."""
        prev = GENESIS
        if not self.path.exists():
            return True, 0, "empty log"
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, 1):
            entry = json.loads(line)
            stored = entry.pop("hash")
            if entry["prev_hash"] != prev:
                return False, i, f"line {i}: previous hash does not match"
            if sha256_json(entry) != stored:
                return False, i, f"line {i}: content was changed"
            prev = stored
        return True, len(lines), "chain intact"
