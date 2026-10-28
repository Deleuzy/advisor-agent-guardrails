"""python -m advisor {eval,ask,make-data,serve}"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import data, evaluate


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m advisor")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("eval", help="run the fixed test set, write results, gate on thresholds")
    e.add_argument("--out", default="results")
    e.add_argument("--provider", default="mock", help="mock (default), anthropic or mistral")
    e.add_argument("--compare", help="fail if the deterministic results differ from this committed summary")

    a = sub.add_parser("ask", help="send one request as an advisor")
    a.add_argument("advisor")
    a.add_argument("text")

    sub.add_parser("make-data", help="regenerate data/synthetic.json (seeded)")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8000)

    args = ap.parse_args(argv)

    if args.cmd == "make-data":
        print(data.write())
        return 0

    if args.cmd == "ask":
        from .agent import AdvisorAgent
        rec = AdvisorAgent().handle(args.advisor, args.text)
        print(json.dumps(rec.public(), indent=1, ensure_ascii=False))
        return 0 if rec.status != "refused" else 2

    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("advisor.api:app", host="0.0.0.0", port=args.port)
        return 0

    llm = None
    if args.provider != "mock":
        from .llm import select_llm
        from .tools import product_catalog
        llm = select_llm(product_catalog(data.connect()), args.provider)
    summary = evaluate.run(provider_llm=llm)
    evaluate.write_report(summary, Path(args.out))
    print((Path(args.out) / "eval_results.md").read_text().split("\n| Case |")[0])
    fails = evaluate.check_thresholds(summary)
    if args.compare:
        committed = json.loads(Path(args.compare).read_text())
        if evaluate.deterministic_view(committed) != evaluate.deterministic_view(summary):
            fails.append(f"results differ from {args.compare}: re-run and commit, or find the regression")
    for f in fails:
        print(f"THRESHOLD FAILED: {f}", file=sys.stderr)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
