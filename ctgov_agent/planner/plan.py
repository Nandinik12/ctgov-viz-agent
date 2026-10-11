"""The QueryPlan: the *only* thing the LLM is allowed to produce.

It is a closed, typed description of "which trials" (filters / cohorts) and
"what to compute" (analysis + dimensions). No numbers, no NCT ids, no chart data.
Everything downstream is deterministic code operating on API responses.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import (
    CATEGORICAL_DIMENSIONS, NUMERIC_DIMENSIONS, Comparison, Dimension, OverallStatus, Phase,
)

Analysis = Literal["distribution", "time_trend", "comparison", "histogram", "scatter", "network"]

# Dimensions where one trial can carry several values (needed for co-occurrence networks).
MULTI_VALUED = {Dimension.intervention, Dimension.drug, Dimension.condition, Dimension.country,
                Dimension.intervention_type}
NETWORK_DIMENSIONS = MULTI_VALUED | {Dimension.sponsor, Dimension.sponsor_class, Dimension.phase}


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    drug: Optional[str] = Field(None, description="Intervention/drug name exactly as written by the user.")
    condition: Optional[str] = Field(None, description="Condition/disease exactly as written by the user.")
    sponsor: Optional[str] = Field(None, description="Sponsor name exactly as written by the user.")
    country: Optional[str] = Field(None, description="Country/location exactly as written by the user.")
    free_text: Optional[str] = Field(None, description="Other search terms that are not a drug/condition/sponsor/country.")
    phases: Optional[list[Phase]] = None
    statuses: Optional[list[OverallStatus]] = None
    start_year: Optional[int] = Field(None, ge=1900, le=2100)
    end_year: Optional[int] = Field(None, ge=1900, le=2100)

    def entity_values(self) -> dict[str, str]:
        return {k: v for k, v in {
            "drug": self.drug, "condition": self.condition, "sponsor": self.sponsor,
            "country": self.country, "free_text": self.free_text,
        }.items() if v}


Action = Literal["visualize", "clarify", "decline"]


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action = Field("visualize", description=(
        "visualize: the question can be answered with registry data. "
        "clarify: it could be answered, but something essential is missing or ambiguous (an unresolved reference "
        "like 'this drug' or 'them', a comparison without named cohorts, or no topic at all). "
        "decline: registry metadata cannot answer it (efficacy, safety, 'best' treatment, medical advice, "
        "outcomes/results, prices, or topics unrelated to clinical trials)."))
    reason: Optional[Literal["ambiguous_reference", "missing_cohorts", "too_vague", "out_of_scope"]] = Field(
        None, description="For clarify/decline only: the category of the problem.")
    message: str = Field("", max_length=400, description=(
        "For clarify/decline only: one or two sentences to the user explaining what is missing or why it "
        "cannot be answered from ClinicalTrials.gov registry data."))
    suggestions: list[str] = Field(default_factory=list, max_length=3, description=(
        "For clarify/decline only: up to 3 rephrased questions this system CAN answer."))
    analysis: Optional[Analysis] = Field(None, description=(
        "distribution: count trials per category of `dimension`. "
        "time_trend: trials per start year (optionally split by `series_dimension`). "
        "comparison: compare `dimension` across the cohorts in `compare`. "
        "histogram: distribution of a numeric `dimension` (enrollment, duration_months). "
        "scatter: one point per trial, x=`dimension`, y=`secondary_dimension` (numeric). "
        "network: nodes are values of `dimension` and `secondary_dimension` (or co-occurrence "
        "within `dimension` if secondary is null); edge weight = number of shared trials."))
    dimension: Optional[Dimension] = None
    secondary_dimension: Optional[Dimension] = None
    series_dimension: Optional[Dimension] = None
    filters: Filters = Field(default_factory=Filters)
    compare: Optional[Comparison] = None
    rationale: str = Field("", max_length=500, description="One sentence: why this analysis answers the question.")


class PlanError(ValueError):
    """Raised when a plan is structurally valid JSON but semantically unusable."""


def validate_plan(plan: QueryPlan, compare_supplied: bool = False) -> QueryPlan:
    """Semantic checks. Error messages are written to be fed back to the LLM on retry."""
    if plan.action != "visualize":
        if not plan.message.strip():
            raise PlanError(f"action={plan.action} requires a non-empty `message` for the user")
        if plan.reason is None:
            plan.reason = "out_of_scope" if plan.action == "decline" else "too_vague"
        return plan
    if plan.analysis is None:
        raise PlanError("action=visualize requires `analysis`")
    a, d, d2, s = plan.analysis, plan.dimension, plan.secondary_dimension, plan.series_dimension

    if a == "time_trend":
        plan.dimension = Dimension.start_year
        if s is not None and (s not in CATEGORICAL_DIMENSIONS or s == Dimension.start_year):
            raise PlanError("time_trend.series_dimension must be a categorical dimension other than start_year")
    elif a in ("distribution", "comparison"):
        if d is None or d not in CATEGORICAL_DIMENSIONS:
            raise PlanError(f"{a} requires a categorical `dimension` from {sorted(x.value for x in CATEGORICAL_DIMENSIONS)}")
        if a == "comparison" and plan.compare is None and not compare_supplied:
            raise PlanError("comparison requires `compare` with field and >= 2 values")
    elif a == "histogram":
        if d not in NUMERIC_DIMENSIONS:
            raise PlanError("histogram requires a numeric `dimension`: enrollment or duration_months")
    elif a == "scatter":
        allowed = NUMERIC_DIMENSIONS | {Dimension.start_year}
        if d not in allowed or d2 not in allowed or d == d2:
            raise PlanError("scatter requires two different dimensions from enrollment, duration_months, start_year")
    elif a == "network":
        if d not in NETWORK_DIMENSIONS:
            raise PlanError(f"network `dimension` must be one of {sorted(x.value for x in NETWORK_DIMENSIONS)}")
        if d2 is None or d2 == d:
            if d not in MULTI_VALUED:
                raise PlanError(f"co-occurrence network needs a multi-valued dimension; {d.value} has one value per trial. "
                                "Set secondary_dimension to build a bipartite network instead.")
            plan.secondary_dimension = None
        elif d2 not in NETWORK_DIMENSIONS:
            raise PlanError(f"network `secondary_dimension` must be one of {sorted(x.value for x in NETWORK_DIMENSIONS)}")

    f = plan.filters
    if f.start_year and f.end_year and f.start_year > f.end_year:
        raise PlanError("filters.start_year must be <= filters.end_year")
    return plan
