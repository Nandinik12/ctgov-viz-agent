"""Merge structured request fields into a plan, apply overrides, and ground entities.

Order of authority: explicit request fields > planner output. Grounding drops any
entity the planner produced that does not appear in the user's own text, which is
the main defense against an LLM inventing a drug or condition.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from ..schemas import NUMERIC_DIMENSIONS, Dimension, VizRequest, VizType
from .plan import PlanError, QueryPlan, validate_plan

CHART_TO_ANALYSIS = {
    VizType.bar_chart: "distribution",
    VizType.grouped_bar_chart: "comparison",
    VizType.time_series: "time_trend",
    VizType.histogram: "histogram",
    VizType.scatter_plot: "scatter",
    VizType.network_graph: "network",
    VizType.choropleth_map: "distribution",
}


def _compare_suggestions(plan: QueryPlan, req: VizRequest) -> list[str]:
    what = plan.dimension.value.replace("_", " ") if plan.dimension else "phases"
    return [f"Compare {what} for Pembrolizumab vs Nivolumab trials",
            f"Compare {what} for breast cancer vs lung cancer trials"]


def structured_fields(req: VizRequest) -> dict[str, Any]:
    return req.model_dump(mode="json", exclude_none=True,
                          exclude={"query", "max_records", "top_n", "max_citations_per_datum"})


def _tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 1]


def ground(value: str, sources: list[str]) -> tuple[bool, list[tuple[str, str]]]:
    """Is `value` supported by the user's own words?

    Every token of `value` must appear in the user's text, either exactly or as a close spelling
    (tokens of 5+ chars, similarity >= 0.8) so that a planner fixing "pembrolizumav" is allowed while
    a planner adding "Keytruda" is not. Returns (grounded, [(user_token, planner_token), ...] corrections).
    """
    hay = set()
    for s in sources:
        hay.update(_tokens(s))
    toks = _tokens(value)
    if not toks:
        return False, []
    fixes: list[tuple[str, str]] = []
    for t in toks:
        if t in hay:
            continue
        near = [h for h in hay if len(t) >= 5 and len(h) >= 5 and SequenceMatcher(None, t, h).ratio() >= 0.8]
        if not near:
            return False, []
        fixes.append((max(near, key=lambda h: SequenceMatcher(None, t, h).ratio()), t))
    return True, fixes


def is_grounded(value: str, sources: list[str]) -> bool:
    return ground(value, sources)[0]


def resolve(plan: QueryPlan, req: VizRequest, warnings: list[str]) -> QueryPlan:
    plan = plan.model_copy(deep=True)
    if plan.action != "visualize":
        return validate_plan(plan)
    f = plan.filters

    # 1. ground planner-produced entities against the user's text
    sources = [req.query] + [str(v) for v in structured_fields(req).values()]
    for name, val in f.entity_values().items():
        ok, fixes = ground(val, sources)
        if not ok:
            setattr(f, name, None)
            warnings.append(f"Ignored inferred {name} '{val}' because it does not appear in the request.")
        for typed, used in fixes:
            warnings.append(f"Interpreted '{typed}' as '{used}' (spelling correction).")
    if plan.compare and not req.compare:
        bad = [v for v in plan.compare.values if not is_grounded(v, sources)]
        if bad:
            warnings.append(f"Ignored inferred comparison values {bad} (not in the request).")
            plan.compare = None

    # 2. structured fields override
    for src, dst in (("drug_name", "drug"), ("condition", "condition"), ("sponsor", "sponsor"), ("country", "country")):
        v = getattr(req, src)
        if v:
            setattr(f, dst, v)
    if req.trial_phase:
        f.phases = req.trial_phase
    if req.overall_status:
        f.statuses = req.overall_status
    if req.start_year:
        f.start_year = req.start_year
    if req.end_year:
        f.end_year = req.end_year
    # free-text search terms already covered by a structured entity would just re-filter the same thing
    explicit = [v for v in (req.drug_name, req.condition, req.sponsor, req.country) if v]
    if f.free_text and explicit and is_grounded(f.free_text, explicit):
        f.free_text = None
    if req.compare:
        plan.compare = req.compare
        if plan.analysis not in ("comparison",):
            plan.analysis = "comparison"
            if plan.dimension is None or plan.dimension in NUMERIC_DIMENSIONS:
                plan.dimension = Dimension.phase
    if plan.compare:
        # a cohort value must not also be a global filter on the same field
        cf = plan.compare.field.value
        if getattr(f, cf, None) and not getattr(req, {"drug": "drug_name"}.get(cf, cf), None):
            setattr(f, cf, None)

    # 3. analysis overrides
    if req.chart_type:
        target = CHART_TO_ANALYSIS[req.chart_type]
        if target != plan.analysis:
            if target == "comparison" and not plan.compare:
                raise PlanError("chart_type=grouped_bar_chart needs a comparison (provide `compare`).")
            plan.analysis = target
            if target == "histogram" and plan.dimension not in NUMERIC_DIMENSIONS:
                plan.dimension = Dimension.enrollment
            if target == "scatter":
                plan.dimension, plan.secondary_dimension = Dimension.enrollment, Dimension.duration_months
            if target == "network" and plan.dimension in (None, Dimension.start_year, *NUMERIC_DIMENSIONS):
                plan.dimension, plan.secondary_dimension = Dimension.sponsor, Dimension.drug
            if target == "distribution" and plan.dimension in (None, *NUMERIC_DIMENSIONS):
                plan.dimension = Dimension.phase
        if req.chart_type == VizType.choropleth_map:
            plan.analysis, plan.dimension = "distribution", Dimension.country
    if req.group_by:
        if plan.analysis == "time_trend":
            plan.series_dimension = req.group_by if req.group_by != Dimension.start_year else None
        else:
            plan.dimension = req.group_by
    if plan.analysis == "comparison" and plan.compare is None:
        # a comparison with nothing to compare is a question for the user, not something to guess
        return QueryPlan(
            action="clarify", reason="missing_cohorts", filters=plan.filters,
            message="Which drugs, conditions or sponsors should be compared? Name at least two.",
            suggestions=_compare_suggestions(plan, req))
    return validate_plan(plan)
