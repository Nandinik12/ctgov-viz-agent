"""Pipeline orchestration: plan -> resolve -> retrieve -> aggregate -> spec.

    request ─▶ planner (LLM | rules) ─▶ resolve (merge, ground, validate)
            ─▶ cohorts ─▶ CTGovClient (paginate, retry, cache)
            ─▶ engine (deterministic aggregation + citations) ─▶ VizResponse
"""
from __future__ import annotations

import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from .analysis import engine
from .analysis.engine import Built, Cohort
from .ctgov.client import CTGovClient, CTGovError, FetchResult
from .ctgov.extract import LABELS, fields_for, norm_key
from .ctgov.exact import EXACT_DIMENSIONS, bucket_clause, exact_counts, with_clause
from .ctgov.normalize import annotate_conditions, annotate_interventions
from .ctgov.query import build_params
from .planner.plan import Filters, PlanError, QueryPlan
from .planner.resolve import resolve, structured_fields
from .planner.rules import RulePlanner
from .schemas import (
    Clarification, CohortQuery, VizType, Dimension, Interpretation, ResponseMeta, VisualizationSpec, VizRequest, VizResponse,
)

log = logging.getLogger(__name__)


class VizService:
    def __init__(self, client: Optional[CTGovClient] = None, planner=None, use_llm: Optional[bool] = None) -> None:
        self.client = client or CTGovClient()
        self.rules = RulePlanner()
        if planner is not None:
            self.planner = planner
        else:
            want_llm = use_llm if use_llm is not None else bool(os.getenv("ANTHROPIC_API_KEY"))
            self.planner = self._make_llm() if want_llm else self.rules

    @staticmethod
    def _make_llm():
        try:
            from .planner.llm import LLMPlanner
            return LLMPlanner()
        except Exception as e:  # missing key or SDK
            log.warning("LLM planner unavailable (%s); using rules planner", e)
            return RulePlanner()

    # ------------------------------------------------------------------ #
    def run(self, req: VizRequest) -> VizResponse:
        warnings: list[str] = []
        plan, planner_name = self._plan(req, warnings)
        plan = resolve(plan, req, warnings)
        if plan.action != "visualize":
            status = "unsupported" if plan.action == "decline" else "needs_clarification"
            return self._no_chart(plan, planner_name, status, plan.reason, plan.message, plan.suggestions, [], warnings)

        cohorts, cohort_meta = self._retrieve(plan, req, warnings)
        if all(not c.studies for c in cohorts):
            terms = ", ".join(f"{k}={v}" for k, v in plan.filters.entity_values().items()) or "these filters"
            return self._no_chart(
                plan, planner_name, "needs_clarification", "no_matching_trials",
                f"No ClinicalTrials.gov records matched {terms}, even with a broadened full text search. "
                "Check the spelling, try a broader term, or relax the phase/status/year filters.",
                [], cohort_meta, warnings)
        merges = self._normalize(plan, cohorts)
        built = self._analyze(plan, cohorts, req)
        if merges:
            shown = dict(sorted(merges.items(), key=lambda kv: -len(kv[1]))[:20])
            built.statistics["entity_merges"] = shown
            built.assumptions.append(
                f"Name variants were merged into {len(merges)} canonical entities using the records' own otherNames and "
                "MeSH terms (see statistics.entity_merges); citations quote the original spelling.")

        exact_done = self._exact_counts(plan, cohort_meta, built, warnings)
        warnings += built.warnings
        for cm in cohort_meta:
            if cm.truncated and not exact_done:
                warnings.append(f"Cohort '{cm.label}': analyzed the first {cm.records_analyzed} of "
                                f"{cm.total_matching} matching trials (raise max_records for full coverage).")

        viz = VisualizationSpec(type=built.type, title=_title(plan), subtitle=_subtitle(plan, cohort_meta),
                                encoding=built.encoding, data=built.data)
        meta = self._meta(plan, planner_name, cohort_meta, warnings, built)
        return VizResponse(request_id=str(uuid.uuid4()), status="ok", visualization=viz, meta=meta)

    def _exact_counts(self, plan: QueryPlan, cohort_meta: list[CohortQuery], built: Built,
                      warnings: list[str]) -> bool:
        """Replace sampled bar heights with exact API counts when a cohort was truncated."""
        if not any(cm.truncated for cm in cohort_meta) or not isinstance(built.data, list) or not built.data:
            return False
        if plan.analysis == "distribution":
            dim, field, cohort_of = plan.dimension, plan.dimension.value, (lambda r: cohort_meta[0].label)
        elif plan.analysis == "comparison" and plan.dimension != Dimension.start_year:
            dim, field, cohort_of = plan.dimension, "category", (lambda r: r["cohort"])
        elif plan.analysis in ("time_trend", "comparison") and plan.series_dimension is None:
            dim, field = Dimension.start_year, "start_year"
            cohort_of = (lambda r: r.get("series", cohort_meta[0].label))
        else:
            return False
        if dim not in EXACT_DIMENSIONS:
            warnings.append(f"Counts for '{dim.value}' come from the analyzed sample (no exact API filter for it).")
            return False

        by_label = {cm.label: cm for cm in cohort_meta}
        jobs, targets = [], []
        for i, r in enumerate(built.data):
            cm = by_label.get(cohort_of(r))
            clause = bucket_clause(dim, r[field])
            if cm is None or not cm.truncated or clause is None:
                continue
            jobs.append((str(i), with_clause(cm.api_params, clause)))
            targets.append((i, cm))
        if not jobs:
            return False
        try:
            totals = exact_counts(self.client, jobs)
        except CTGovError as e:
            log.warning("exact counts failed: %s", e)
            warnings.append("Exact per-bar counts were unavailable; bars reflect the analyzed sample.")
            return False
        for i, cm in targets:
            r = built.data[i]
            r["sample_count"] = r["trial_count"]
            r["trial_count"] = totals[str(i)]
            r["count_method"] = "exact"
            if "share_pct" in r and cm.total_matching:
                r["share_pct"] = round(100 * r["trial_count"] / cm.total_matching, 1)
        done = {i for i, _ in targets}
        for i, r in enumerate(built.data):
            if i not in done and by_label.get(cohort_of(r)) and by_label[cohort_of(r)].truncated:
                r["count_method"] = "sample"  # e.g. "Not reported" phase: no API filter expresses it
        built.statistics["exact_count_queries"] = len(jobs)
        built.assumptions.append(
            "Cohort exceeded max_records, so each bar's height is an exact ClinicalTrials.gov count (one filtered "
            "countTotal query per bar; trial_count) while citations and nct_ids come from the analyzed records "
            "(sample_count of them).")
        return True

    @staticmethod
    def _normalize(plan: QueryPlan, cohorts: list[Cohort]) -> dict[str, list[str]]:
        """Merge name variants across all cohorts (one shared alias map) for the dimensions in play."""
        dims = {plan.dimension, plan.secondary_dimension, plan.series_dimension}
        everything = [s for c in cohorts for s in c.studies]
        merges: dict[str, list[str]] = {}
        if dims & {Dimension.drug, Dimension.intervention}:
            merges.update(annotate_interventions(everything))
        if Dimension.condition in dims:
            merges.update(annotate_conditions(everything))
        return merges

    def _meta(self, plan: QueryPlan, planner_name: str, cohort_meta, warnings, built: Optional[Built] = None):
        return ResponseMeta(
            api_base=self.client.base_url,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            interpretation=Interpretation(
                planner=planner_name, action=plan.action, analysis=plan.analysis,
                dimension=plan.dimension.value if plan.dimension else None,
                secondary_dimension=plan.secondary_dimension.value if plan.secondary_dimension else None,
                series_dimension=plan.series_dimension.value if plan.series_dimension else None,
                rationale=plan.rationale or plan.message or ""),
            filters=_filters_dict(plan),
            cohorts=cohort_meta,
            units=built.units if built else {}, sort=built.sort if built else None,
            time_granularity=built.time_granularity if built else None,
            statistics=built.statistics if built else {}, assumptions=built.assumptions if built else [],
            warnings=warnings,
        )

    def _no_chart(self, plan, planner_name, status, reason, message, suggestions, cohort_meta, warnings) -> VizResponse:
        return VizResponse(
            request_id=str(uuid.uuid4()), status=status, visualization=None,
            clarification=Clarification(reason=reason or "too_vague", message=message, suggestions=suggestions or []),
            meta=self._meta(plan, planner_name, cohort_meta, warnings))

    # ------------------------------------------------------------------ #
    def _plan(self, req: VizRequest, warnings: list[str]) -> tuple[QueryPlan, str]:
        structured = structured_fields(req)
        if self.planner is not self.rules:
            try:
                return self.planner.plan(req.query, structured), "llm"
            except Exception as e:
                log.warning("LLM planning failed: %s", e)
                warnings.append("LLM planner failed; fell back to keyword rules.")
        return self.rules.plan(req.query, structured), "rules"

    def _retrieve(self, plan: QueryPlan, req: VizRequest, warnings: list[str]):
        specs: list[tuple[str, Filters]] = []
        if plan.compare:
            for v in plan.compare.values:
                f = plan.filters.model_copy(deep=True)
                setattr(f, plan.compare.field.value, v)
                specs.append((v, f))
        else:
            specs.append((_cohort_label(plan.filters), plan.filters))

        fields = fields_for(plan.dimension, plan.secondary_dimension, plan.series_dimension, Dimension.start_year)

        def fetch(spec: tuple[str, Filters]) -> tuple[FetchResult, list[str]]:
            label, f = spec
            notes: list[str] = []
            res = self.client.search_studies(build_params(f), req.max_records, fields)
            if not res.studies and any([f.drug, f.condition, f.sponsor]):
                res = self._broaden(f, req, notes, label, fields) or res
            return res, notes

        # cohorts are independent queries: fetch them concurrently (httpx.Client is thread safe)
        with ThreadPoolExecutor(max_workers=min(4, len(specs))) as pool:
            results = list(pool.map(fetch, specs))

        cohorts, meta = [], []
        for (label, _), (res, notes) in zip(specs, results):
            warnings.extend(notes)
            cohorts.append(Cohort(label, res.studies))
            meta.append(CohortQuery(label=label, api_params=res.params, total_matching=res.total_count,
                                    records_analyzed=len(res.studies), truncated=res.truncated))
        return cohorts, meta

    def _broaden(self, f: Filters, req: VizRequest, warnings: list[str], label: str,
                 fields: Optional[list[str]] = None) -> Optional[FetchResult]:
        """Zero hits on a field-specific search: retry the same terms as a full-text search."""
        terms = " ".join(x for x in (f.drug, f.condition, f.sponsor, f.free_text) if x)
        g = f.model_copy(update={"drug": None, "condition": None, "sponsor": None, "free_text": terms})
        res = self.client.search_studies(build_params(g), req.max_records, fields)
        if res.studies:
            warnings.append(f"Cohort '{label}': no field-specific matches; broadened to full-text search for '{terms}'.")
            return res
        return None

    def _analyze(self, plan: QueryPlan, cohorts: list[Cohort], req: VizRequest) -> Built:
        k, top = req.max_citations_per_datum, req.top_n
        yr = (plan.filters.start_year, plan.filters.end_year)
        a = plan.analysis
        if a == "time_trend":
            return engine.time_trend(cohorts, top, k, yr, plan.series_dimension)
        if a == "comparison":
            if plan.dimension == Dimension.start_year:
                return engine.time_trend(cohorts, top, k, yr)
            return engine.comparison(cohorts, plan.dimension, top, k)
        (c,) = cohorts
        if a == "distribution":
            if plan.dimension == Dimension.country and req.chart_type != VizType.bar_chart:
                return engine.geo_distribution(c, k)
            return engine.distribution(c, plan.dimension, top, k)
        if a == "histogram":
            return engine.histogram(c, plan.dimension, k)
        if a == "scatter":
            return engine.scatter(c, plan.dimension, plan.secondary_dimension, k)
        if a == "network":
            excl = {}
            if plan.filters.condition and Dimension.condition in (plan.dimension, plan.secondary_dimension):
                # the anchor condition (and its MeSH equivalents) sits on nearly every trial: drop it as a hub
                n = len(c.studies) or 1
                hubs = {k for k, b in engine.bucketize(c.studies, Dimension.condition).items() if b.count >= 0.8 * n}
                excl[Dimension.condition] = hubs | {norm_key(plan.filters.condition)}
            return engine.network(c, plan.dimension, plan.secondary_dimension, top, k, excl)
        raise PlanError(f"unsupported analysis {a}")


# --------------------------------------------------------------------------- #
# deterministic titles (never LLM-written, so they cannot misstate the data)
# --------------------------------------------------------------------------- #
def _cohort_label(f: Filters) -> str:
    parts = [x for x in (f.drug, f.condition, f.sponsor, f.free_text) if x]
    if f.country:
        parts.append(f"in {f.country}")
    return " ".join(parts) if parts else "All trials"


def _scope(plan: QueryPlan) -> str:
    f = plan.filters
    bits = [x for x in (f.drug, f.condition, f.free_text) if x]
    s = f" for {' / '.join(bits)}" if bits else ""
    if f.sponsor:
        s += f" sponsored by {f.sponsor}"
    if f.country:
        s += f" in {f.country}"
    return s


def _title(plan: QueryPlan) -> str:
    a, d, d2 = plan.analysis, plan.dimension, plan.secondary_dimension
    status = ""
    if plan.filters.statuses:
        status = " ".join(s.value.replace("_", " ").title() for s in plan.filters.statuses) + " "
    scope = _scope(plan)
    if a == "time_trend":
        by = f" by {LABELS[plan.series_dimension].lower()}" if plan.series_dimension else ""
        return f"{status or 'T'}{'t' if status else ''}rials started per year{scope}{by}"
    if a == "comparison":
        vs = " vs ".join(plan.compare.values)
        if d == Dimension.start_year:
            return f"Trials started per year: {vs}{scope}"
        return f"{LABELS[d]} of {status.lower()}trials: {vs}{scope}"
    if a == "distribution":
        return f"{status}trials by {LABELS[d].lower()}{scope}" if status else f"Trials by {LABELS[d].lower()}{scope}"
    if a == "histogram":
        return f"Distribution of {LABELS[d].lower()}{scope}"
    if a == "scatter":
        return f"{LABELS[d]} vs {LABELS[d2].lower()}{scope}"
    if a == "network":
        if d2:
            return f"{LABELS[d]} to {LABELS[d2].lower()} network{scope}"
        return f"{LABELS[d]} co-occurrence network{scope}"
    return "Clinical trials"


def _subtitle(plan: QueryPlan, cohorts: list[CohortQuery]) -> str:
    n = sum(c.records_analyzed for c in cohorts)
    tot = [c.total_matching for c in cohorts if c.total_matching is not None]
    f = plan.filters
    extra = []
    if f.phases:
        extra.append("phases " + ", ".join(p.value for p in f.phases))
    if f.start_year or f.end_year:
        extra.append(f"start {f.start_year or '...'} to {f.end_year or 'present'}")
    s = f"{n} trials analyzed" + (f" of {sum(tot)} matching" if tot and sum(tot) != n else "")
    return s + (f"; {'; '.join(extra)}" if extra else "") + ". Source: ClinicalTrials.gov"


def _filters_dict(plan: QueryPlan) -> dict:
    d = plan.filters.model_dump(mode="json", exclude_none=True)
    if plan.compare:
        d["compare"] = plan.compare.model_dump(mode="json")
    return d
