"""Regenerate the example outputs against the live ClinicalTrials.gov API.

    python examples/run_examples.py            # uses the LLM planner if ANTHROPIC_API_KEY is set
    python examples/run_examples.py --rules    # force the keyword planner

Writes examples/NN_<slug>.json, each holding {"request": ..., "response": ...}.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ctgov_agent.schemas import VizRequest  # noqa: E402
from ctgov_agent.service import VizService  # noqa: E402

EXAMPLES = [
    {"query": "How has the number of trials for Pembrolizumab changed per year since 2015?",
     "drug_name": "Pembrolizumab"},
    {"query": "How are breast cancer trials distributed across phases?", "condition": "breast cancer",
     "max_records": 20000},
    {"query": "Compare phases for trials involving Pembrolizumab vs Nivolumab"},
    {"query": "Which countries have the most recruiting trials for type 2 diabetes?"},
    {"query": "Show a network of sponsors and drugs for melanoma trials", "condition": "melanoma", "top_n": 12},
    # extra coverage
    {"query": "Which drugs frequently co-occur in combination studies for non-small cell lung cancer?",
     "condition": "non-small cell lung cancer", "start_year": 2018, "top_n": 15},
    {"query": "What is the distribution of enrollment sizes for Alzheimer disease phase 3 trials?",
     "condition": "Alzheimer disease", "trial_phase": ["PHASE3"]},
    # knowing its limits
    {"query": "What is the best drug for breast cancer?"},
    {"query": "How are pembrolizumav trials distributed across phases?"},
]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:50]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", action="store_true")
    args = ap.parse_args()
    svc = VizService(use_llm=False if args.rules else None)
    out_dir = Path(__file__).resolve().parent
    for i, body in enumerate(EXAMPLES, 1):
        t0 = time.time()
        resp = svc.run(VizRequest(**body))
        path = out_dir / f"{i:02d}_{slug(body['query'])}.json"
        path.write_text(json.dumps({"request": body, "response": resp.model_dump(mode="json", exclude_none=True)},
                                   indent=2))
        v = resp.visualization
        if v is None:
            kind, n = resp.status, resp.clarification.reason
        else:
            kind = v.type.value
            n = len(v.data) if isinstance(v.data, list) else f"{len(v.data['nodes'])} nodes/{len(v.data['edges'])} edges"
        print(f"[{i}] {kind:20s} {n!s:>22}  {time.time() - t0:5.1f}s  planner={resp.meta.interpretation.planner}"
              f"  -> {path.name}")


if __name__ == "__main__":
    main()
