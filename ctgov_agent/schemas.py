"""Public request/response contract.

Everything a client sends or receives is defined here, so this file *is* the API
documentation. The README tables are generated from the same models
(see `GET /v1/schema`).
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# --------------------------------------------------------------------------- #
# Controlled vocabularies (mirror ClinicalTrials.gov API v2 enum values)
# --------------------------------------------------------------------------- #
class Phase(str, Enum):
    EARLY_PHASE1 = "EARLY_PHASE1"
    PHASE1 = "PHASE1"
    PHASE2 = "PHASE2"
    PHASE3 = "PHASE3"
    PHASE4 = "PHASE4"
    NA = "NA"


class OverallStatus(str, Enum):
    NOT_YET_RECRUITING = "NOT_YET_RECRUITING"
    RECRUITING = "RECRUITING"
    ENROLLING_BY_INVITATION = "ENROLLING_BY_INVITATION"
    ACTIVE_NOT_RECRUITING = "ACTIVE_NOT_RECRUITING"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    COMPLETED = "COMPLETED"
    WITHDRAWN = "WITHDRAWN"
    UNKNOWN = "UNKNOWN"


class Dimension(str, Enum):
    """Every field the engine knows how to extract from a study record.

    Categorical dimensions can be grouped/counted; numeric ones can be binned or
    plotted; `start_year` is the temporal axis.
    """
    phase = "phase"
    start_year = "start_year"
    overall_status = "overall_status"
    sponsor = "sponsor"                  # lead sponsor name
    sponsor_class = "sponsor_class"      # INDUSTRY / NIH / OTHER ...
    intervention = "intervention"        # intervention names (drugs, devices, ...)
    drug = "drug"                        # intervention names restricted to DRUG/BIOLOGICAL
    intervention_type = "intervention_type"
    condition = "condition"
    country = "country"
    study_type = "study_type"
    enrollment = "enrollment"            # numeric
    duration_months = "duration_months"  # numeric: start -> primary completion


CATEGORICAL_DIMENSIONS = {
    Dimension.phase, Dimension.start_year, Dimension.overall_status, Dimension.sponsor,
    Dimension.sponsor_class, Dimension.intervention, Dimension.drug,
    Dimension.intervention_type, Dimension.condition, Dimension.country, Dimension.study_type,
}
NUMERIC_DIMENSIONS = {Dimension.enrollment, Dimension.duration_months}


class VizType(str, Enum):
    bar_chart = "bar_chart"
    grouped_bar_chart = "grouped_bar_chart"
    time_series = "time_series"
    histogram = "histogram"
    scatter_plot = "scatter_plot"
    network_graph = "network_graph"
    choropleth_map = "choropleth_map"


class CompareField(str, Enum):
    drug = "drug"
    condition = "condition"
    sponsor = "sponsor"
    country = "country"


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #
class Comparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: CompareField = Field(description="Which entity type the cohorts differ on.")
    values: list[str] = Field(min_length=2, max_length=5,
                              description="2-5 cohort values, e.g. ['Pembrolizumab', 'Nivolumab'].")

    @field_validator("values")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        out = [s.strip() for s in v if s and s.strip()]
        if len({s.lower() for s in out}) != len(out) or len(out) < 2:
            raise ValueError("comparison values must be >= 2 distinct non-empty strings")
        return out


class VizRequest(BaseModel):
    """POST /v1/visualize body. Only `query` is required.

    Structured fields always win over anything inferred from `query`.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=3, max_length=1000, description="Natural-language question.")

    # entity filters
    drug_name: Optional[str] = Field(None, max_length=200, description="Intervention name (query.intr).")
    condition: Optional[str] = Field(None, max_length=200, description="Condition/disease (query.cond).")
    sponsor: Optional[str] = Field(None, max_length=200, description="Sponsor/collaborator (query.spons).")
    country: Optional[str] = Field(None, max_length=100, description="Trial location (query.locn).")
    trial_phase: Optional[list[Phase]] = Field(None, description="Restrict to these phases.")
    overall_status: Optional[list[OverallStatus]] = Field(None, description="Restrict to these statuses.")
    start_year: Optional[int] = Field(None, ge=1900, le=2100, description="Trials starting in/after this year.")
    end_year: Optional[int] = Field(None, ge=1900, le=2100, description="Trials starting in/before this year.")

    # analysis overrides
    compare: Optional[Comparison] = Field(None, description="Explicit cohort comparison.")
    group_by: Optional[Dimension] = Field(None, description="Force the primary dimension.")
    chart_type: Optional[VizType] = Field(None, description="Force a visualization type (validated for compatibility).")

    # execution knobs
    max_records: int = Field(5000, ge=10, le=20000, description="Max trials fetched per cohort (pages of 1000).")
    top_n: int = Field(15, ge=3, le=50, description="Max categories / network nodes per type.")
    max_citations_per_datum: int = Field(3, ge=0, le=20, description="Detailed citations per datum (all NCT ids are always listed).")

    @model_validator(mode="after")
    def _years(self) -> "VizRequest":
        if self.start_year and self.end_year and self.start_year > self.end_year:
            raise ValueError("start_year must be <= end_year")
        return self


# --------------------------------------------------------------------------- #
# Response
# --------------------------------------------------------------------------- #
FieldType = Literal["nominal", "ordinal", "quantitative", "temporal"]


class Channel(BaseModel):
    """One visual channel -> one data field (Vega-Lite flavoured)."""
    field: str
    type: FieldType
    title: str
    sort: Optional[Union[Literal["ascending", "descending"], list[str]]] = Field(
        None, description="'ascending'/'descending' by value, or an explicit category order.")
    unit: Optional[str] = None
    scale: Optional[Literal["linear", "log"]] = Field(None, description="Suggested axis scale.")


class Citation(BaseModel):
    nct_id: str
    field: str = Field(description="JSON path inside the ClinicalTrials.gov v2 study record.")
    excerpt: str = Field(description="Exact value found at `field` in the API response.")
    url: str


class VisualizationSpec(BaseModel):
    type: VizType
    title: str
    subtitle: Optional[str] = None
    encoding: dict[str, Channel] = Field(
        description="Channel name -> field mapping. Cartesian charts use x/y/series/color/size/tooltip; "
                    "network_graph uses node_id/node_label/node_group/node_size/edge_source/edge_target/edge_weight; "
                    "choropleth_map uses geo (ISO 3166 numeric code, joins to world-atlas topojson ids) and color, and "
                    "also carries x/y so a renderer without maps can draw the same rows as a bar chart.")
    data: Union[list[dict[str, Any]], dict[str, list[dict[str, Any]]]] = Field(
        description="Row list for cartesian charts; {nodes:[...], edges:[...]} for network_graph.")


class Interpretation(BaseModel):
    planner: Literal["llm", "rules"]
    action: Literal["visualize", "clarify", "decline"] = "visualize"
    analysis: Optional[str] = None
    dimension: Optional[str] = None
    secondary_dimension: Optional[str] = None
    series_dimension: Optional[str] = None
    rationale: str


class CohortQuery(BaseModel):
    label: str
    api_params: dict[str, Any]
    total_matching: Optional[int]
    records_analyzed: int
    truncated: bool


class ResponseMeta(BaseModel):
    source: str = "clinicaltrials.gov"
    api_base: str
    generated_at: str
    interpretation: Interpretation
    filters: dict[str, Any]
    cohorts: list[CohortQuery]
    units: dict[str, str] = {}
    sort: Optional[str] = None
    time_granularity: Optional[str] = None
    statistics: dict[str, Any] = Field(default_factory=dict, description="Summary numbers (n, median, ...).")
    assumptions: list[str] = []
    warnings: list[str] = []


ClarificationReason = Literal[
    "out_of_scope",          # registry data cannot answer this (efficacy, advice, unrelated topic)
    "ambiguous_reference",   # "this drug", "them" with nothing to resolve it to
    "missing_cohorts",       # a comparison without named cohorts
    "too_vague",             # no topic or measure to chart
    "no_matching_trials",    # valid plan, but ClinicalTrials.gov returned nothing
]


class Clarification(BaseModel):
    reason: ClarificationReason
    message: str = Field(description="Human readable explanation to show the user.")
    suggestions: list[str] = Field(default_factory=list, description="Answerable rephrasings to offer as buttons.")


class VizResponse(BaseModel):
    """`status` tells the client what to render:
    ok -> `visualization`; needs_clarification / unsupported -> `clarification` (visualization is null)."""
    request_id: str
    status: Literal["ok", "needs_clarification", "unsupported"] = "ok"
    visualization: Optional[VisualizationSpec] = None
    clarification: Optional[Clarification] = None
    meta: ResponseMeta
