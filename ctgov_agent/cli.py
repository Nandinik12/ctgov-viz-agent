"""Command line: run one query without starting the server.

    python -m ctgov_agent.cli "How are breast cancer trials distributed across phases?" --condition "breast cancer"
"""
from __future__ import annotations

import argparse
import json
import sys

from .schemas import VizRequest
from .service import VizService


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query")
    ap.add_argument("--json", help="extra request fields as a JSON object", default="{}")
    for f in ("drug_name", "condition", "sponsor", "country"):
        ap.add_argument(f"--{f}")
    ap.add_argument("--start-year", type=int, dest="start_year")
    ap.add_argument("--end-year", type=int, dest="end_year")
    ap.add_argument("--max-records", type=int, dest="max_records")
    ap.add_argument("--rules", action="store_true", help="force the keyword planner (no LLM)")
    a = ap.parse_args(argv)

    body = {"query": a.query, **json.loads(a.json)}
    for k in ("drug_name", "condition", "sponsor", "country", "start_year", "end_year", "max_records"):
        if getattr(a, k) is not None:
            body[k] = getattr(a, k)
    resp = VizService(use_llm=not a.rules).run(VizRequest(**body))
    json.dump(resp.model_dump(mode="json", exclude_none=True), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
