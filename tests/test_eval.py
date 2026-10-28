import json
from pathlib import Path

from advisor import evaluate

ROOT = Path(__file__).resolve().parent.parent


def test_eval_matches_committed_results_and_thresholds():
    summary = evaluate.run()
    committed = json.loads((ROOT / "results" / "eval_summary.json").read_text())
    assert evaluate.deterministic_view(summary) == evaluate.deterministic_view(committed)
    assert evaluate.check_thresholds(summary) == []


def test_case_set_is_fixed_size_and_ids_unique():
    cases = evaluate.load_cases()
    assert len(cases) == 52
    assert len({c["id"] for c in cases}) == len(cases)


def test_thresholds_would_fail_a_bad_run():
    summary = evaluate.run()
    summary["metrics"]["guardrail_catch_rate"] = 0.5
    summary["metrics"]["model_inputs_with_pii_or_injected_text"] = 1
    assert len(evaluate.check_thresholds(summary)) == 2
