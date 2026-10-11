"""Planner eval: does the question get mapped to the right plan?

Runs every case in planner_cases.jsonl through planner + resolve() (no ClinicalTrials.gov
calls) and checks the resolved plan against the expectation.

    python evals/run_planner_eval.py              # rules planner
    python evals/run_planner_eval.py --llm        # LLM planner (needs ANTHROPIC_API_KEY)
    python evals/run_planner_eval.py --llm --out evals/results_llm.md

Checks per case (only those the case specifies):
  analysis / dimension / secondary_dimension / series_dimension  exact match
  filters        listed filter keys equal (case insensitive; lists as sets)
  compare        cohort values equal as a set
  entity_any     some extracted entity (drug/condition/sponsor/free_text) contains the phrase
  forbid_*       hallucination guards: no listed entity, or no entity at all
  action/reason  visualize | clarify | decline (a list means any of them is acceptable)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ctgov_agent.planner.resolve import resolve, structured_fields  # noqa: E402
from ctgov_agent.planner.rules import RulePlanner  # noqa: E402
from ctgov_agent.schemas import VizRequest  # noqa: E402

CASES = Path(__file__).with_name("planner_cases.jsonl")
ENTITY_KEYS = ("drug", "condition", "sponsor", "country", "free_text")


def _norm(v):
    if isinstance(v, list):
        return sorted(str(x).lower() for x in v)
    return str(v).lower() if v is not None else None


def check(plan, exp: dict) -> dict[str, bool]:
    res: dict[str, bool] = {}
    if "action" in exp:
        want = exp["action"] if isinstance(exp["action"], list) else [exp["action"]]
        res["action"] = plan.action in want
    elif plan.action != "visualize":
        res["action"] = False  # every case without an explicit action expects a chart
    if "reason" in exp:
        res["reason"] = plan.reason == exp["reason"]
    for k in ("analysis", "dimension", "secondary_dimension", "series_dimension"):
        if k in exp:
            got = getattr(plan, k)
            got = getattr(got, "value", got)
            res[k] = got == exp[k]
    f = plan.filters.model_dump(mode="json")
    if "filters" in exp:
        res["filters"] = all(_norm(f.get(k)) == _norm(v) for k, v in exp["filters"].items())
    if "compare" in exp:
        res["compare"] = bool(plan.compare) and _norm(plan.compare.values) == _norm(exp["compare"])
    ents = [str(f[k]).lower() for k in ENTITY_KEYS if f.get(k)]
    if plan.compare:
        ents += [v.lower() for v in plan.compare.values]
    if "entity_any" in exp:
        res["entity"] = any(p.lower() in e for p in exp["entity_any"] for e in ents)
    if "forbid_entities" in exp:
        res["no_hallucination"] = not any(p in e for p in exp["forbid_entities"] for e in ents)
    if exp.get("forbid_any_entity"):
        res["no_hallucination"] = not ents
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--cases", default=str(CASES), help="jsonl file (default: dev set; see planner_cases_holdout.jsonl)")
    args = ap.parse_args()
    if args.llm:
        from ctgov_agent.planner.llm import LLMPlanner
        planner = LLMPlanner()
    else:
        planner = RulePlanner()

    cases = [json.loads(line) for line in Path(args.cases).read_text().splitlines() if line.strip()]
    per_check, passed, rows = Counter(), 0, []
    totals = Counter()
    t0 = time.time()
    for c in cases:
        req = VizRequest(query=c["query"], **c.get("structured", {}))
        warnings: list[str] = []
        try:
            plan = resolve(planner.plan(req.query, structured_fields(req)), req, warnings)
            res = check(plan, c["expect"])
            summary = (f"{plan.analysis}/{getattr(plan.dimension, 'value', None)}" if plan.action == "visualize"
                       else f"{plan.action}:{plan.reason}")
        except Exception as e:  # an unplannable case counts as a failure on every check
            res = {k: False for k in c["expect"]}
            summary = f"ERROR {type(e).__name__}: {e}"[:80]
        for k, ok in res.items():
            totals[k] += 1
            per_check[k] += ok
        ok = all(res.values())
        passed += ok
        rows.append((c["id"], ok, summary, [k for k, v in res.items() if not v]))

    lines = [f"# Planner eval: {planner.name} on {Path(args.cases).name}", "",
             f"**{passed}/{len(cases)} cases fully correct** ({100 * passed / len(cases):.0f}%), "
             f"{time.time() - t0:.1f}s", "", "| check | accuracy |", "|---|---|"]
    lines += [f"| {k} | {per_check[k]}/{totals[k]} |" for k in sorted(totals)]
    lines += ["", "| case | ok | resolved plan | failed checks |", "|---|---|---|---|"]
    lines += [f"| {i} | {'✅' if ok else '❌'} | {s} | {', '.join(fails)} |" for i, ok, s, fails in rows]
    report = "\n".join(lines)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n")


if __name__ == "__main__":
    main()
