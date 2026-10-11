"""Deterministic keyword planner.

Used when no LLM is configured, or when the LLM fails. It covers every analysis
kind but extracts entities conservatively: anything it cannot confidently type
as a drug goes to free-text search (`query.term`) rather than being guessed.
Structured request fields are the reliable way to pass entities in this mode.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..schemas import Comparison, CompareField, Dimension, OverallStatus, Phase
from .plan import Filters, QueryPlan, validate_plan

DRUG_SUFFIX = re.compile(r"(mab|nib|lib|parib|ciclib|lisib|cept|tide|stat|vir|platin|taxel|rubicin|mide|cel)$", re.I)

DIM_KEYWORDS: list[tuple[Dimension, re.Pattern]] = [  # order = priority on ties
    (Dimension.start_year, re.compile(r"per year|each year|by year|over time|trend|since \d{4}|annual", re.I)),
    (Dimension.sponsor_class, re.compile(r"sponsor (categor|class|type)|industry[- ]funded|funder type", re.I)),
    (Dimension.intervention_type, re.compile(r"intervention types?|types? of intervention|modalit", re.I)),
    (Dimension.overall_status, re.compile(r"\bstatus(es)?\b|recruitment state", re.I)),
    (Dimension.study_type, re.compile(r"study types?|observational vs|interventional vs", re.I)),
    (Dimension.country, re.compile(r"countr|geograph|\bwhere\b|location|region", re.I)),
    (Dimension.phase, re.compile(r"\bphases?\b(?!\s*(\d|i{1,3}\b|iv\b))", re.I)),
    (Dimension.sponsor, re.compile(r"sponsor|compan|pharma|who (runs|funds)", re.I)),
    (Dimension.drug, re.compile(r"\bdrugs?\b|medication|therap(y|ies)|agents?\b", re.I)),
    (Dimension.intervention, re.compile(r"intervention", re.I)),
    (Dimension.condition, re.compile(r"condition|disease|indication", re.I)),
]
TIME_RX = re.compile(r"per year|each year|by year|over time|trend|annual|year over year|yearly", re.I)
ENTITY_WORDS = {
    "sponsor": Dimension.sponsor, "sponsors": Dimension.sponsor, "drug": Dimension.drug, "drugs": Dimension.drug,
    "condition": Dimension.condition, "conditions": Dimension.condition, "disease": Dimension.condition,
    "diseases": Dimension.condition, "country": Dimension.country, "countries": Dimension.country,
    "intervention": Dimension.intervention, "interventions": Dimension.intervention,
}
ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}


class RulePlanner:
    name = "rules"

    def plan(self, query: str, structured: dict[str, Any]) -> QueryPlan:
        q = query.strip()
        ql = q.lower()
        filters = Filters(**_filters_from_text(q))
        plan: Optional[QueryPlan] = None

        gate = _scope_gate(ql, filters, structured)
        if gate is not None:
            return validate_plan(gate)

        if re.search(r"network|co-?occur|↔|<->|connections?|relationships? between (sponsors|drugs|conditions)|"
                     r"combination (studies|trials)|who works with", ql):
            ents = [ENTITY_WORDS[w] for w in re.findall(r"[a-z]+", ql) if w in ENTITY_WORDS]
            ents = list(dict.fromkeys(ents))  # dedupe, keep order
            if re.search(r"co-?occur|combination", ql) and (not ents or ents[0] == Dimension.drug):
                plan = QueryPlan(analysis="network", dimension=Dimension.drug)
            elif len(ents) >= 2:
                plan = QueryPlan(analysis="network", dimension=ents[0], secondary_dimension=ents[1])
            else:
                plan = QueryPlan(analysis="network", dimension=Dimension.sponsor, secondary_dimension=Dimension.drug)
        elif re.search(r"scatter|correlat|relationship between (enrollment|duration)", ql):
            plan = QueryPlan(analysis="scatter", dimension=Dimension.enrollment, secondary_dimension=Dimension.duration_months)
        elif re.search(r"histogram|enrollment|sample size|participants|how (big|large)|how long|duration", ql):
            dim = Dimension.duration_months if re.search(r"how long|duration", ql) else Dimension.enrollment
            plan = QueryPlan(analysis="histogram", dimension=dim)

        comp = _comparison_from_text(q)
        if plan is None and (comp or re.search(r"\bcompare\b", ql)):
            dim = _detect_dimension(ql, exclude={Dimension.drug, Dimension.condition}) or Dimension.phase
            plan = QueryPlan(analysis="comparison", dimension=dim, compare=comp)
            if comp:  # cohort names must not also be filters
                for v in comp.values:
                    for k in ("drug", "free_text", "condition"):
                        if getattr(filters, k) and getattr(filters, k).lower() == v.lower():
                            setattr(filters, k, None)

        if plan is None:
            dim = _detect_dimension(ql)
            if TIME_RX.search(ql):
                dim = Dimension.start_year
            if dim == Dimension.start_year or dim is None and re.search(r"how many|number of", ql):
                series = _detect_dimension(re.sub(r"per year|each year|by year|over time|trend", "", ql),
                                           exclude={Dimension.start_year, Dimension.drug, Dimension.condition})
                by = re.search(r"\b(by|split by|broken down by|per) (phase|status|sponsor class|country)", ql)
                plan = QueryPlan(analysis="time_trend", dimension=Dimension.start_year,
                                 series_dimension=series if by else None)
            else:
                plan = QueryPlan(analysis="distribution", dimension=dim or Dimension.phase)

        plan.filters = filters
        plan.rationale = f"Keyword rules matched analysis '{plan.analysis}'" + (
            f" on '{plan.dimension.value}'." if plan.dimension else ".")
        # comparison cohorts may come from structured fields, so semantic validation happens in resolve()
        return plan if plan.analysis == "comparison" and plan.compare is None else validate_plan(plan)


OUT_OF_SCOPE = re.compile(
    r"\b(best|most effective|more effective|better than|works? better|should i|recommend|cures?|safest|safer|"
    r"side effects?|adverse events?|efficacy|survival rates?|success rates?|response rates?|how much does|cost|price|"
    r"what dose|dosage|am i eligible|near me|enroll me|weather|stock)\b", re.I)
REFERENCE = re.compile(r"\b(this|that|these|those|the same) (drug|drugs|condition|disease|sponsor|company|trial|trials)\b"
                       r"|\b(them|it|they)\b", re.I)
ANALYTIC_WORDS = re.compile(r"trial|study|studies|phase|sponsor|countr|enrol|recruit|drug|condition|network|trend|"
                            r"distribution|compare|histogram|scatter|intervention|status", re.I)
ANSWERABLE = ["How are breast cancer trials distributed across phases?",
              "How many trials started each year for type 2 diabetes?",
              "Show a network of sponsors and drugs for melanoma trials"]


def _scope_gate(ql: str, f: Filters, structured: dict[str, Any]) -> Optional[QueryPlan]:
    """Decide early whether the question should be charted at all."""
    has_entity = bool(f.entity_values()) or any(k in structured for k in ("drug_name", "condition", "sponsor", "country", "compare"))
    if OUT_OF_SCOPE.search(ql):
        return QueryPlan(action="decline", reason="out_of_scope", suggestions=ANSWERABLE[:2], message=(
            "ClinicalTrials.gov registry data describes how trials are designed and run (phases, sponsors, sites, "
            "dates, enrollment), not which treatment works best, is safest, or is right for someone. "
            "I can chart how the research landscape looks instead."))
    if REFERENCE.search(ql) and not has_entity:
        return QueryPlan(action="clarify", reason="ambiguous_reference", suggestions=ANSWERABLE[:2], message=(
            "Your question refers to something ('this', 'them', ...) I can't resolve. Which drug, condition or "
            "sponsor do you mean? You can also pass it as a structured field such as drug_name."))
    if not ANALYTIC_WORDS.search(ql) and not has_entity:
        return QueryPlan(action="clarify", reason="too_vague", suggestions=ANSWERABLE, message=(
            "I couldn't find a clinical trials topic or measure in that question. Try naming a drug or condition "
            "and what you want to see (phases, sponsors, countries, trend over time, ...)."))
    return None


def _detect_dimension(ql: str, exclude: set[Dimension] = frozenset()) -> Optional[Dimension]:
    priority = [d for d, _ in DIM_KEYWORDS]
    hits = [(m.start(), priority.index(d), d) for d, rx in DIM_KEYWORDS if d not in exclude
            for m in [rx.search(ql)] if m]
    if not hits:
        return None
    # prefer dimension phrases that follow "across/by/per/which/most common", else first listed priority
    for anchor in ("across", " by ", " per ", "which", "most common", "top", "breakdown"):
        i = ql.find(anchor)
        if i >= 0:
            after = [h for h in hits if h[0] > i]
            if after:
                return min(after)[2]
    return min(hits, key=lambda h: h[1])[2]


def _filters_from_text(q: str) -> dict[str, Any]:
    ql = q.lower()
    out: dict[str, Any] = {}
    if m := re.search(r"(?:since|after|from)\s+((?:19|20)\d{2})", ql):
        out["start_year"] = int(m[1])
    if m := re.search(r"between\s+((?:19|20)\d{2})\s+and\s+((?:19|20)\d{2})", ql):
        out["start_year"], out["end_year"] = int(m[1]), int(m[2])
    elif m := re.search(r"(?:to|until|before|through)\s+((?:19|20)\d{2})", ql):
        out["end_year"] = int(m[1])
    statuses = []
    if re.search(r"\b(currently )?recruiting\b", ql) and "not yet recruiting" not in ql:
        statuses.append(OverallStatus.RECRUITING)
    if re.search(r"\bcompleted (trials|studies)\b", ql):
        statuses.append(OverallStatus.COMPLETED)
    if re.search(r"\bterminated\b", ql):
        statuses.append(OverallStatus.TERMINATED)
    if statuses:
        out["statuses"] = statuses
    phases = [Phase(f"PHASE{ROMAN.get(p, p)}") for p in re.findall(r"phase\s*(1|2|3|4|iv|iii|ii|i)\b", ql)]
    if phases:
        out["phases"] = list(dict.fromkeys(phases))

    # entity phrases: "for X trials", "involving X", "trials for X", "most X trials", "how are X trials".
    # Lookaheads give overlapping candidates; the shortest acceptable one wins, so
    # "network of sponsors and drugs for melanoma trials" yields "melanoma".
    stop = (r"(?:\s+(?:trials?|studies|study|changed|change|since|per|over|in|by|across|distributed|"
            r"started|from|between|being|are|is|typically|last)\b|\s*[?.,]|\s*$)")
    term_rx = r"([A-Za-z0-9][\w\-' ]{1,60}?)"
    pats = (rf"(?=\b(?:for|involving|of|on|testing|with|in|most|many)\s+{term_rx}{stop})",
            rf"(?=\b(?:how|where|what|which) (?:are|do|have|long do)\s+{term_rx}\s+(?:trials|studies)\b)")
    cands = []
    for pat in pats:
        for m in re.finditer(pat, q, re.I):
            term = re.sub(r"^(?:(?:the|a|an|currently|recruiting|completed|active|ongoing|terminated|current|"
                          r"phase\s*(?:1|2|3|4|i{1,3}|iv))\s+)+", "", m[1].strip(), flags=re.I)
            low = term.lower()
            if (low in _GENERIC or re.fullmatch(r"(19|20)\d{2}", term) or set(low.split()) & _REFERENT_WORDS
                    or " vs" in low or "versus" in low or re.search(r"\bphase\s*\w+$", low)):
                continue
            followed_by_trials = bool(re.match(r"\s+(?:trials?|studies)\b", q[m.end(1):], re.I))
            cands.append((not followed_by_trials, len(term), term))
    if cands:
        term = min(cands)[2]
        out["drug" if DRUG_SUFFIX.search(term.split()[-1]) else "free_text"] = term
    # any standalone word that looks like an INN drug name (e.g. "nivolumab", "semaglutide")
    if "drug" not in out:
        for w in re.findall(r"\b[A-Za-z][a-z]{5,}\b", q):
            if DRUG_SUFFIX.search(w) and w.lower() not in _NOT_DRUGS:
                out["drug"] = w
                if out.get("free_text", "").lower() == w.lower():
                    out.pop("free_text")
                break
    return out


_GENERIC = {"the", "a", "all", "each", "this", "that", "these", "clinical", "phase", "phases", "number", "this drug",
            "this condition", "trials", "two conditions", "two drugs", "time", "year", "each year", "drug a", "drug b"}


_NOT_DRUGS = {"compare", "describe", "estimate", "relative", "positive", "negative", "inactive", "active",
              "objective", "duration", "alternative", "collaborative", "comparative", "medicine", "routine"}

# phrases containing these are references ("this drug", "the trials"), not entity names
_REFERENT_WORDS = {"trial", "trials", "study", "studies", "this", "that", "these", "those", "number",
                   "drug", "drugs", "condition", "conditions", "sponsor", "sponsors", "each", "all", "phase", "phases",
                   "enrollment", "size", "sizes", "duration", "participants", "combination", "network", "countries",
                   "most", "common", "status", "statuses", "often", "tested", "studied", "frequently", "commonly",
                   "recruiting", "completed", "terminated", "active", "ongoing", "industry", "academic", "share", "relationships", "where"}


def _comparison_from_text(q: str) -> Optional[Comparison]:
    m = re.search(r"([A-Z][\w\-]*(?:\s[A-Z][\w\-]*)?)\s+(?:vs\.?|versus)\s+([A-Z][\w\-]*(?:\s[A-Z][\w\-]*)?)", q)
    if not m:
        m = re.search(r"\bbetween\s+([A-Z][\w\-]*(?:\s[A-Z][\w\-]*)?)\s+and\s+([A-Z][\w\-]*(?:\s[A-Z][\w\-]*)?)", q)
    if not m:
        m = re.search(r"([\w\-]+)\s+(?:vs\.?|versus)\s+([\w\-]+)", q, re.I)
        if m and not (DRUG_SUFFIX.search(m[1]) or DRUG_SUFFIX.search(m[2])):
            m = None
    if not m:
        return None
    a, b = m[1].strip(), m[2].strip()
    if a.lower() in _GENERIC or b.lower() in _GENERIC:
        return None
    field = CompareField.drug if DRUG_SUFFIX.search(a) or DRUG_SUFFIX.search(b) or "drug" in q.lower() \
        else CompareField.condition if re.search(r"condition|disease|cancer", q, re.I) else CompareField.drug
    try:
        return Comparison(field=field, values=[a, b])
    except ValueError:
        return None
