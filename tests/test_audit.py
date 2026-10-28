import json

from advisor.audit import AuditLog


def test_tampering_is_detected(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(3):
        log.append("request", {"n": i})
    assert log.verify() == (True, 3, "chain intact")
    lines = log.path.read_text().splitlines()
    e = json.loads(lines[1])
    e["n"] = 99
    lines[1] = json.dumps(e)
    log.path.write_text("\n".join(lines) + "\n")
    ok, line, msg = log.verify()
    assert not ok and line == 2


def test_deleted_line_is_detected(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(3):
        log.append("request", {"n": i})
    lines = log.path.read_text().splitlines()
    log.path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert log.verify()[0] is False


def test_chain_continues_after_restart(tmp_path):
    AuditLog(tmp_path / "a.jsonl").append("request", {"n": 1})
    log = AuditLog(tmp_path / "a.jsonl")
    log.append("request", {"n": 2})
    assert log.verify() == (True, 2, "chain intact")
