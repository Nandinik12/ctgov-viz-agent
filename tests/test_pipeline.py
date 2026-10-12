from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from ctgov_agent.analysis import engine
from ctgov_agent.analysis.engine import Cohort
from ctgov_agent.ctgov.client import CTGovClient, CTGovError
from ctgov_agent.ctgov.extract import CATEGORICAL, NUMERIC, parse_date
from ctgov_agent.ctgov.query import build_params
from ctgov_agent.planner.plan import Filters, PlanError, QueryPlan, validate_plan
from ctgov_agent.planner.resolve import is_grounded, resolve
from ctgov_agent.planner.rules import RulePlanner
from ctgov_agent.schemas import Dimension, Phase, VizRequest, VizResponse, VizType

from .conftest import STUDIES


def _get(path: str, study: dict):
    """Resolve a citation path like a.b.c[2].d against a study record."""
    import re
    cur = study
    for part in path.split("."):
        m = re.fullmatch(r"(\w+)(?:\[(\d+)\])?", part)
        cur = cur[m[1]]
        if m[2] is not None:
            cur = cur[int(m[2])]
    return cur


BY_ID = {s["protocolSection"]["identificationModule"]["nctId"]: s for s in STUDIES}


def assert_citations_exact(rows):
    """Every citation excerpt must equal the value at its JSON path in the source record."""
    n = 0
    for r in rows:
        for c in r.get("citations", []):
            if c["excerpt"] == "(field absent)":
                with pytest.raises(KeyError):
                    _get(c["field"], BY_ID[c["nct_id"]])
                continue
            v = _get(c["field"], BY_ID[c["nct_id"]])
            got = ", ".join(v) if isinstance(v, list) else str(v)
            assert got == c["excerpt"], (c, got)
            assert c["nct_id"] in r.get("nct_ids", [c["nct_id"]])
            n += 1
    return n


# ---------------------------------------------------------------- extraction
def test_parse_date_handles_month_precision():
    assert parse_date("2017-05").isoformat() == "2017-05-01"
    assert parse_date("garbage") is None and parse_date(None) is None


def test_combined_phase_is_single_bucket_and_missing_is_explicit():
    labels = [CATEGORICAL[Dimension.phase](s)[0].label for s in STUDIES]
    assert "Phase 1/Phase 2" in labels and "Not reported" in labels


def test_country_counted_once_per_trial():
    facts = CATEGORICAL[Dimension.country](STUDIES[0])
    assert [f.label for f in facts] == ["United States", "Canada"]


def test_drug_dimension_excludes_placebo_and_nondrug():
    names = [f.label for f in CATEGORICAL[Dimension.drug](STUDIES[2])]
    assert names == ["Pembrolizumab", "Carboplatin"]


def test_duration_uses_primary_completion():
    v, facts = NUMERIC[Dimension.duration_months](STUDIES[0])
    assert 23.5 < v < 24.5 and facts[1].path.endswith("primaryCompletionDateStruct.date")


# ---------------------------------------------------------------- query building
def test_build_params():
    p = build_params(Filters(condition="breast cancer", phases=[Phase.PHASE2, Phase.PHASE3], start_year=2015))
    assert p["query.cond"] == '"breast cancer"'
    assert p["filter.advanced"] == "AREA[Phase](PHASE2 OR PHASE3) AND AREA[StartDate]RANGE[2015-01-01,MAX]"


# ---------------------------------------------------------------- engine
def test_distribution_counts_and_citations():
    b = engine.distribution(Cohort("all", STUDIES), Dimension.phase, 15, 5)
    counts = {r["phase"]: r["trial_count"] for r in b.data}
    assert counts == {"Phase 1": 1, "Phase 1/Phase 2": 1, "Phase 2": 2, "Phase 3": 2, "Not reported": 1}
    assert sum(counts.values()) == len(STUDIES)  # phase is single-valued: no double counting
    assert [r["phase"] for r in b.data][0] == "Phase 1"  # natural phase order, not count order
    assert assert_citations_exact(b.data) > 0


def test_time_trend_fills_gaps_with_zero():
    b = engine.time_trend([Cohort("all", STUDIES)], 15, 3, (None, None))
    years = {r["start_year"]: r["trial_count"] for r in b.data}
    assert years == {2016: 1, 2017: 2, 2018: 0, 2019: 2, 2020: 1}
    assert_citations_exact(b.data)


def test_histogram_overflow_and_stats():
    b = engine.histogram(Cohort("all", STUDIES), Dimension.enrollment, 2)
    assert sum(r["trial_count"] for r in b.data) == 6  # one study has no enrollment
    assert b.data[-1]["bin_end"] is None or b.data[-1]["bin_end"] > b.data[-1]["bin_start"]
    assert b.statistics["missing"] == 1


def test_network_cooccurrence_weights():
    b = engine.network(Cohort("all", STUDIES), Dimension.drug, None, 10, 2)
    w = {(e["source"], e["target"]): e["weight"] for e in b.data["edges"]}
    assert w[("drug:carboplatin", "drug:pembrolizumab")] == 2
    assert w[("drug:nivolumab", "drug:pembrolizumab")] == 1
    assert not any("placebo" in n["id"] for n in b.data["nodes"])
    for e in b.data["edges"]:  # each edge citation pair supports both endpoints
        assert len(e["citations"]) == 2 * min(2, e["weight"])
    assert_citations_exact(b.data["edges"])
    assert_citations_exact(b.data["nodes"])


def test_bipartite_network_has_only_cross_edges():
    b = engine.network(Cohort("all", STUDIES), Dimension.sponsor, Dimension.drug, 10, 1)
    for e in b.data["edges"]:
        assert e["source"].split(":")[0] != e["target"].split(":")[0]


def test_comparison_rows_cover_all_cohorts():
    a = Cohort("Pembrolizumab", STUDIES[:4])
    n = Cohort("Nivolumab", [STUDIES[3]] + STUDIES[4:])
    b = engine.comparison([a, n], Dimension.phase, 10, 1)
    cats = {r["category"] for r in b.data}
    assert all(sum(1 for r in b.data if r["category"] == c) == 2 for c in cats)
    assert b.statistics["trials_in_multiple_cohorts"] == 1


# ---------------------------------------------------------------- planning
@pytest.mark.parametrize("q,analysis,dim", [
    ("How has the number of trials for Pembrolizumab changed per year since 2015?", "time_trend", "start_year"),
    ("How are melanoma trials distributed across phases?", "distribution", "phase"),
    ("What are the most common intervention types for melanoma trials?", "distribution", "intervention_type"),
    ("Which countries have the most recruiting trials for melanoma?", "distribution", "country"),
    ("Compare phases for trials involving Pembrolizumab vs Nivolumab", "comparison", "phase"),
    ("Show a network of sponsors and drugs for melanoma trials", "network", "sponsor"),
    ("Which drugs frequently co-occur in combination studies?", "network", "drug"),
    ("What is the distribution of enrollment for melanoma trials?", "histogram", "enrollment"),
])
def test_rule_planner(q, analysis, dim):
    p = RulePlanner().plan(q, {})
    assert p.analysis == analysis and p.dimension.value == dim


def test_rule_planner_extracts_filters():
    p = RulePlanner().plan("Which countries have the most recruiting trials for melanoma since 2018?", {})
    assert p.filters.statuses[0].value == "RECRUITING" and p.filters.start_year == 2018
    assert p.filters.free_text == "melanoma"


def test_validate_plan_rejects_bad_combos():
    with pytest.raises(PlanError):
        validate_plan(QueryPlan(analysis="histogram", dimension=Dimension.phase))
    with pytest.raises(PlanError):
        validate_plan(QueryPlan(analysis="network", dimension=Dimension.sponsor))  # sponsor is single-valued


def test_grounding_drops_hallucinated_entities():
    req = VizRequest(query="how are lung cancer trials distributed by phase")
    plan = QueryPlan(analysis="distribution", dimension=Dimension.phase,
                     filters=Filters(condition="lung cancer", drug="Keytruda"))
    warnings: list[str] = []
    out = resolve(plan, req, warnings)
    assert out.filters.condition == "lung cancer" and out.filters.drug is None
    assert any("Keytruda" in w for w in warnings)
    assert is_grounded("Non-Small Cell", ["non-small cell lung cancer trials"])


def test_structured_fields_override_plan():
    req = VizRequest(query="trials over time", drug_name="Pembrolizumab", start_year=2015)
    out = resolve(QueryPlan(analysis="time_trend"), req, [])
    assert out.filters.drug == "Pembrolizumab" and out.filters.start_year == 2015


# ---------------------------------------------------------------- request validation
def test_request_validation():
    with pytest.raises(ValidationError):
        VizRequest(query="x")
    with pytest.raises(ValidationError):
        VizRequest(query="valid query", start_year=2020, end_year=2010)
    with pytest.raises(ValidationError):
        VizRequest(query="valid query", unknown_field=1)
    with pytest.raises(ValidationError):
        VizRequest(query="valid query", compare={"field": "drug", "values": ["a", "A"]})


# ---------------------------------------------------------------- end to end
def test_e2e_time_trend(service, fake):
    r = service.run(VizRequest(query="How has the number of trials for this drug changed over time?",
                               drug_name="Pembrolizumab"))
    VizResponse.model_validate(r.model_dump())
    assert r.visualization.type == VizType.time_series
    assert sum(d["trial_count"] for d in r.visualization.data) == 4
    assert fake.calls[0]["query.intr"] == "Pembrolizumab"
    assert r.meta.interpretation.planner == "rules"


def test_e2e_comparison_fetches_each_cohort(service, fake):
    r = service.run(VizRequest(query="Compare phases", compare={"field": "drug", "values": ["Pembrolizumab", "Nivolumab"]}))
    assert r.visualization.type == VizType.grouped_bar_chart
    assert [c.label for c in r.meta.cohorts] == ["Pembrolizumab", "Nivolumab"]
    assert {c["query.intr"] for c in fake.calls} == {"Pembrolizumab", "Nivolumab"}


def test_e2e_network(service):
    r = service.run(VizRequest(query="Show a network of sponsors and drugs", chart_type="network_graph"))
    assert r.visualization.type == VizType.network_graph
    assert r.visualization.data["nodes"] and r.visualization.data["edges"]


def test_e2e_pagination_and_truncation(service, fake):
    r = service.run(VizRequest(query="How are trials distributed across phases?", max_records=10))
    assert r.meta.cohorts[0].records_analyzed == 7 and not r.meta.cohorts[0].truncated


def test_e2e_broadens_on_zero_hits(service):
    r = service.run(VizRequest(query="trials by phase", sponsor="Synthetic study"))  # only matches full text
    assert any("broadened" in w for w in r.meta.warnings)


def test_client_retries_then_fails(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        return httpx.Response(503)
    c = CTGovClient(transport=httpx.MockTransport(flaky), max_retries=2)
    with pytest.raises(CTGovError):
        c.search_studies({}, 10)
    assert calls["n"] == 3


def test_client_400_not_retried():
    c = CTGovClient(transport=httpx.MockTransport(lambda r: httpx.Response(400, text="bad AREA")))
    with pytest.raises(CTGovError, match="400"):
        c.search_studies({}, 10)


# ---------------------------------------------------------------- llm planner (stubbed client)
class _Block:
    def __init__(self, inp, id_="tu_1"):
        self.type, self.input, self.id = "tool_use", inp, id_


class _Resp:
    def __init__(self, inp):
        self.content = [_Block(inp)]


class _StubMessages:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    def create(self, **kw):
        self.calls.append(kw)
        return _Resp(self.outputs.pop(0))


def _stub_llm(outputs):
    from ctgov_agent.planner.llm import LLMPlanner, SYSTEM
    p = LLMPlanner.__new__(LLMPlanner)
    p.model, p.max_attempts = "stub", 2
    p.tool = {"name": "submit_plan", "description": "", "input_schema": QueryPlan.model_json_schema()}
    p.client = type("C", (), {"messages": _StubMessages(outputs)})()
    return p


def test_llm_planner_self_corrects_after_validation_error():
    bad = {"analysis": "histogram", "dimension": "phase"}
    good = {"analysis": "distribution", "dimension": "phase", "filters": {"condition": "melanoma"}}
    p = _stub_llm([bad, good])
    plan = p.plan("how are melanoma trials distributed by phase", {})
    assert plan.analysis == "distribution"
    retry_msgs = p.client.messages.calls[1]["messages"]
    assert retry_msgs[-1]["content"][0]["is_error"] is True


def test_llm_failure_falls_back_to_rules(fake):
    from ctgov_agent.service import VizService
    bad = {"analysis": "histogram", "dimension": "phase"}
    client = CTGovClient(transport=httpx.MockTransport(fake), cache_ttl_s=0)
    svc = VizService(client=client, planner=_stub_llm([bad, bad]))
    r = svc.run(VizRequest(query="How are trials distributed across phases?"))
    assert r.meta.interpretation.planner == "rules"
    assert any("fell back" in w for w in r.meta.warnings)


# ---------------------------------------------------------------- field narrowing
def test_service_requests_only_needed_fields(service, fake):
    service.run(VizRequest(query="Which countries have the most trials?"))
    fields = fake.calls[0]["fields"].split(",")
    assert "protocolSection.contactsLocationsModule.locations" in fields
    assert "protocolSection.armsInterventionsModule.interventions" not in fields


def test_client_falls_back_to_modules_when_fields_rejected():
    seen = []

    def handler(req):
        f = req.url.params["fields"]
        seen.append(f)
        if "designModule.phases" in f and "protocolSection.designModule," not in f + ",":
            return httpx.Response(400, text="unknown field")
        return httpx.Response(200, json={"studies": STUDIES[:2], "totalCount": 2})
    c = CTGovClient(transport=httpx.MockTransport(handler))
    r = c.search_studies({}, 10, fields=["protocolSection.designModule.phases"])
    assert len(r.studies) == 2 and len(seen) == 2


# ---------------------------------------------------------------- knowing its limits
def test_out_of_scope_question_is_declined_without_calling_api(service, fake):
    r = service.run(VizRequest(query="What is the best drug for melanoma?"))
    assert r.status == "unsupported" and r.visualization is None
    assert r.clarification.reason == "out_of_scope" and r.clarification.suggestions
    assert fake.calls == []  # no wasted API traffic


def test_unresolved_reference_asks_for_clarification(service):
    r = service.run(VizRequest(query="How has the number of trials for this drug changed over time?"))
    assert r.status == "needs_clarification" and r.clarification.reason == "ambiguous_reference"


def test_reference_resolved_by_structured_field_is_charted(service):
    r = service.run(VizRequest(query="How has the number of trials for this drug changed over time?",
                               drug_name="Pembrolizumab"))
    assert r.status == "ok"


def test_comparison_without_cohorts_asks_instead_of_guessing(service):
    r = service.run(VizRequest(query="Compare sponsor categories across two conditions."))
    assert r.status == "needs_clarification" and r.clarification.reason == "missing_cohorts"


def test_zero_results_returns_clarification_not_empty_chart(service):
    r = service.run(VizRequest(query="trials by phase", drug_name="Notarealdrugzumab"))
    assert r.status == "needs_clarification" and r.clarification.reason == "no_matching_trials"
    assert r.meta.cohorts[0].records_analyzed == 0


def test_fuzzy_grounding_allows_spelling_fix_but_not_new_entities():
    from ctgov_agent.planner.resolve import ground
    ok, fixes = ground("pembrolizumab", ["how are pembrolizumav trials distributed"])
    assert ok and fixes == [("pembrolizumav", "pembrolizumab")]
    assert not ground("keytruda", ["how are pembrolizumab trials distributed"])[0]
    assert not ground("nivolumab", ["pembrolizumab trials"])[0]


def test_llm_decline_plan_flows_through(fake):
    from ctgov_agent.service import VizService
    decline = {"action": "decline", "reason": "out_of_scope", "message": "Registry data cannot rank efficacy.",
               "suggestions": ["How are melanoma trials distributed across phases?"]}
    client = CTGovClient(transport=httpx.MockTransport(fake), cache_ttl_s=0)
    r = VizService(client=client, planner=_stub_llm([decline])).run(VizRequest(query="Which melanoma drug works best?"))
    assert r.status == "unsupported" and r.meta.interpretation.planner == "llm"
    assert r.clarification.message.startswith("Registry")


# ---------------------------------------------------------------- entity normalization
def _variant_studies():
    from .conftest import study
    return [
        study("NCT10000001", phases=["PHASE2"], start="2018-01", interventions=[("BIOLOGICAL", "MK-3475")],
              other_names={"MK-3475": ["Pembrolizumab", "Keytruda"]}, conditions=["Breast Cancer"],
              condition_mesh=["Breast Neoplasms"]),
        study("NCT10000002", phases=["PHASE2"], start="2018-01", interventions=[("BIOLOGICAL", "Pembrolizumab 200 mg")],
              intervention_mesh=["pembrolizumab"], conditions=["breast carcinoma"], condition_mesh=["Breast Neoplasms"]),
        study("NCT10000003", phases=["PHASE3"], start="2019-01",
              interventions=[("BIOLOGICAL", "Keytruda"), ("DRUG", "Paclitaxel")],
              other_names={"Paclitaxel": ["chemotherapy"]}, conditions=["Cancer of the Breast"]),
        study("NCT10000004", phases=["PHASE3"], start="2019-01", interventions=[("DRUG", "Carboplatin")],
              other_names={"Carboplatin": ["chemotherapy"]}, conditions=["Lung Cancer"]),
    ]


def test_drug_aliases_merge_into_one_entity():
    from ctgov_agent.ctgov.normalize import annotate_interventions
    studies = _variant_studies()
    merges = annotate_interventions(studies)
    b = engine.distribution(Cohort("all", studies), Dimension.drug, 15, 5)
    counts = {r["drug"]: r["trial_count"] for r in b.data}
    assert counts["Pembrolizumab"] == 3  # MK-3475, "Pembrolizumab 200 mg" and Keytruda
    assert "Pembrolizumab" in merges and "MK-3475" in merges["Pembrolizumab"]
    # generic alias "chemotherapy" must not glue paclitaxel and carboplatin together
    assert counts["Paclitaxel"] == 1 and counts["Carboplatin"] == 1
    # citations still quote the original spelling at its original path
    by = {s["protocolSection"]["identificationModule"]["nctId"]: s for s in studies}
    for c in b.data[0]["citations"]:
        assert _get(c["field"], by[c["nct_id"]]) == c["excerpt"]


def test_different_mesh_anchors_never_merge():
    from ctgov_agent.ctgov.normalize import annotate_interventions
    from .conftest import study
    studies = [
        study("NCT20000001", phases=["PHASE1"], start="2020-01", interventions=[("DRUG", "Drug X")],
              other_names={"Drug X": ["ABC-1"]}, intervention_mesh=["drug x"]),
        study("NCT20000002", phases=["PHASE1"], start="2020-01", interventions=[("DRUG", "Drug Y")],
              other_names={"Drug Y": ["ABC-1"]}, intervention_mesh=["drug y"]),
    ]
    annotate_interventions(studies)
    labels = {f.label for s in studies for f in CATEGORICAL[Dimension.drug](s)}
    assert labels == {"Drug x", "Drug y"}


def test_conditions_use_mesh_terms_with_free_text_fallback():
    from ctgov_agent.ctgov.normalize import annotate_conditions
    studies = _variant_studies()
    annotate_conditions(studies)
    b = engine.distribution(Cohort("all", studies), Dimension.condition, 15, 5)
    counts = {r["condition"]: r["trial_count"] for r in b.data}
    assert counts["Breast Neoplasms"] == 2  # "Breast Cancer" and "breast carcinoma": one MeSH term
    assert counts["Cancer of the Breast"] == 1  # record has no MeSH terms: free text kept
    c = next(r for r in b.data if r["condition"] == "Breast Neoplasms")["citations"][0]
    assert c["field"].startswith("protocolSection.conditionsModule.conditions") and c["excerpt"] == "Breast Cancer"


# ---------------------------------------------------------------- normalization on real API records
def _live():
    import json
    from pathlib import Path
    return json.loads((Path(__file__).parent / "fixtures" / "ctgov_sample.json").read_text())["studies"]


def test_live_records_pembrolizumab_variants_merge_without_absorbing_other_drugs():
    from ctgov_agent.ctgov.normalize import annotate_interventions
    studies = _live()
    merges = annotate_interventions(studies)
    assert {"Keytruda", "MK-3475"} <= set(merges["Pembrolizumab"])
    # live traps: an umbrella arm listing [OPDIVO, Keytruda, Pembrolizumab, Nivolumab], and a pembrolizumab arm
    # whose otherNames are [Carboplatin, Pemetrexed]
    assert not {"Carboplatin", "Pemetrexed", "Nivolumab", "OPDIVO"} & set(merges["Pembrolizumab"])
    labels = {f.label for s in studies for f in CATEGORICAL[Dimension.drug](s)}
    assert "Pembrolizumab 50 MG Injection [Keytruda]" not in labels and "Pembrolizumab" in labels


def test_live_records_noisy_condition_mesh_is_not_used():
    from ctgov_agent.ctgov.normalize import annotate_conditions
    studies = _live()
    annotate_conditions(studies)
    labels = {f.label for s in studies for f in CATEGORICAL[Dimension.condition](s)}
    assert "Parkinson Disease 4, Autosomal Dominant Lewy Body" not in labels  # spurious MeSH on oncology trials
    assert "Hodgkin Disease" in labels and "Hodgkin Lymphoma" not in labels
    assert "Melanoma" in labels and "Melanoma (Skin Cancer)" not in labels


def test_essie_clauses_match_live_probe():
    """The exact-count clauses were verified live (tests/fixtures/essie_probe_counts.json); keep them in sync."""
    import json
    from pathlib import Path
    from ctgov_agent.ctgov.exact import bucket_clause
    probe = json.loads((Path(__file__).parent / "fixtures" / "essie_probe_counts.json").read_text())
    assert probe["phase1_and_2"]["status"] == 200 and probe["phase_missing"]["status"] == 200
    assert "AREA[Phase]PHASE1 AND AREA[Phase]PHASE2" in bucket_clause(Dimension.phase, "Phase 1/Phase 2")
    assert bucket_clause(Dimension.sponsor_class, "INDUSTRY") == "AREA[LeadSponsorClass]INDUSTRY"
    assert probe["sponsorclass_industry"]["status"] == 200 and probe["start_2015"]["status"] == 200


# ---------------------------------------------------------------- exact counts
def _big_service():
    import copy
    from ctgov_agent.service import VizService
    from .conftest import FakeCTGov
    many = []
    for rep in range(4):  # 28 trials
        for s0 in STUDIES:
            s1 = copy.deepcopy(s0)
            idm = s1["protocolSection"]["identificationModule"]
            idm["nctId"] = idm["nctId"][:-2] + f"{rep}{idm['nctId'][-1]}"
            many.append(s1)
    client = CTGovClient(transport=httpx.MockTransport(FakeCTGov(many)), cache_ttl_s=0)
    return VizService(client=client, use_llm=False)


def test_exact_counts_replace_sample_when_truncated():
    svc = _big_service()
    full = {d["phase"]: d["trial_count"] for d in
            svc.run(VizRequest(query="How are trials distributed across phases?", max_records=100)).visualization.data}
    r2 = svc.run(VizRequest(query="How are trials distributed across phases?", max_records=10))
    assert r2.meta.cohorts[0].truncated
    exact = [d for d in r2.visualization.data if d.get("count_method") == "exact"]
    assert exact
    for d in exact:
        assert d["trial_count"] == full[d["phase"]] and d["sample_count"] <= d["trial_count"]
    assert not any("analyzed the first" in w for w in r2.meta.warnings)


def test_exact_counts_time_trend():
    svc = _big_service()
    full = {d["start_year"]: d["trial_count"] for d in
            svc.run(VizRequest(query="How many trials started per year?", max_records=100)).visualization.data}
    r = svc.run(VizRequest(query="How many trials started per year?", max_records=10))
    exact = {d["start_year"]: d["trial_count"] for d in r.visualization.data if d.get("count_method") == "exact"}
    assert exact and all(full[y] == v for y, v in exact.items())


def test_bucket_clause_combined_phase():
    from ctgov_agent.ctgov.exact import bucket_clause
    c = bucket_clause(Dimension.phase, "Phase 1/Phase 2")
    assert c.startswith("AREA[Phase]PHASE1 AND AREA[Phase]PHASE2 AND NOT AREA[Phase]EARLY_PHASE1")
    assert bucket_clause(Dimension.phase, "Not reported") == "AREA[Phase]MISSING"


# ---------------------------------------------------------------- geography
def test_country_distribution_is_a_choropleth_with_iso_codes(service):
    r = service.run(VizRequest(query="Which countries have the most trials?"))
    v = r.visualization
    assert v.type == VizType.choropleth_map and v.encoding["geo"].field == "iso_numeric"
    us = next(d for d in v.data if d["country"] == "United States")
    assert us["iso_numeric"] == "840" and us["iso3"] == "USA"
    assert {"x", "y"} <= set(v.encoding)  # bar chart fallback channels
    assert_citations_exact(v.data)


def test_bar_chart_override_keeps_bars_for_countries(service):
    r = service.run(VizRequest(query="Which countries have the most trials?", chart_type="bar_chart"))
    assert r.visualization.type == VizType.bar_chart


def test_ctgov_country_spellings_map():
    from ctgov_agent.analysis.engine import country_codes
    assert country_codes("Korea, Republic of")["iso3"] == "KOR"
    assert country_codes("Iran, Islamic Republic of")["iso3"] == "IRN"
    assert country_codes("Russian Federation")["iso_numeric"] == "643"
    # spellings seen in live example output
    assert country_codes("Turkey (Türkiye)")["iso3"] == "TUR"
    assert country_codes("Côte d\u2019Ivoire")["iso3"] == "CIV"
