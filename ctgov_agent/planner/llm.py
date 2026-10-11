"""LLM planner: natural language -> QueryPlan via forced tool use.

The model sees the QueryPlan JSON schema as a tool and must call it. Output is
parsed by pydantic and checked by `validate_plan`; on failure the error text is
sent back for one self-correction round. The model never sees or produces data.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from pydantic import ValidationError

from .plan import PlanError, QueryPlan, validate_plan

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.getenv("CTGOV_AGENT_MODEL", "claude-sonnet-4-5")

SYSTEM = """You translate questions about clinical trials into a QueryPlan for a deterministic analytics engine
backed by the ClinicalTrials.gov API. You only plan; you never answer with numbers.

First decide `action`:
- visualize: registry metadata (phases, statuses, dates, sponsors, sites/countries, conditions, interventions,
  enrollment, durations) can answer the question.
- decline (reason=out_of_scope): it asks about efficacy, safety, side effects, which treatment is best, outcomes
  or results, personal medical advice or eligibility, prices, or anything unrelated to clinical trials.
- clarify: it could be charted but something essential is missing: reason=ambiguous_reference for "this drug",
  "them", "it" with no structured field to resolve it; reason=missing_cohorts for a comparison that names fewer
  than two things; reason=too_vague when there is no topic or measure at all.
For clarify/decline write a short `message` to the user and up to 3 `suggestions`: concrete rephrasings this
system can chart. Do not clarify when a reasonable default exists (e.g. "trials by phase" with no topic is fine:
all trials). Misspelled drug or condition names are not a reason to clarify: use the correct spelling.

Rules for visualize:
- Copy entity names (drug, condition, sponsor, country) as the user wrote them, fixing only obvious misspellings.
  Never add synonyms, brand names, or entities the user did not mention. If unsure whether something is an entity, leave it out.
- Pick the analysis that directly answers the question:
  * "how many per year / over time / trend / since YEAR" -> time_trend (filters.start_year for "since YEAR").
    Use series_dimension only if the user asks to break the trend down (e.g. "by phase").
  * "distributed across / breakdown / most common / top / which countries" -> distribution with a categorical dimension.
  * "compare A vs B" -> comparison with compare.field and compare.values, dimension = what is compared
    (phase, sponsor_class, overall_status, start_year, ...). Do NOT also put A or B in filters.
  * enrollment size / duration distribution -> histogram (enrollment | duration_months).
  * relationship between two numeric properties -> scatter.
  * "network / relationships / co-occur / connections / who works with" -> network.
    sponsors <-> drugs: dimension=sponsor, secondary_dimension=drug.
    drug <-> drug co-occurrence (combination studies): dimension=drug, secondary_dimension=null.
- "drug" counts only DRUG/BIOLOGICAL interventions; "intervention" counts all types; "intervention_type" is the category.
- "recruiting" -> filters.statuses=["RECRUITING"]. Phases use API codes (PHASE1, PHASE2, ...).
- Keep `rationale` to one sentence.
Always respond by calling the submit_plan tool."""


class LLMPlanner:
    name = "llm"

    def __init__(self, api_key: Optional[str] = None, model: str = DEFAULT_MODEL, max_attempts: int = 2) -> None:
        import anthropic  # optional dependency; imported lazily so the rules planner works without it

        self.client = anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
        self.model = model
        self.max_attempts = max_attempts
        self.tool = {
            "name": "submit_plan",
            "description": "Submit the query plan for the analytics engine.",
            "input_schema": QueryPlan.model_json_schema(),
        }

    def plan(self, query: str, structured: dict[str, Any]) -> QueryPlan:
        user = f"Question: {query}"
        if structured:
            user += ("\nStructured fields supplied by the client (authoritative, will override your filters): "
                     + json.dumps(structured))
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        last_err: Exception = RuntimeError("no attempt made")

        for attempt in range(self.max_attempts):
            resp = self.client.messages.create(
                model=self.model, max_tokens=1024, system=SYSTEM,
                tools=[self.tool], tool_choice={"type": "tool", "name": "submit_plan"}, messages=messages,
            )
            block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
            if block is None:
                last_err = PlanError("model did not call submit_plan")
                break
            try:
                return validate_plan(QueryPlan.model_validate(block.input),
                                     compare_supplied="compare" in structured)
            except (ValidationError, PlanError) as e:
                last_err = e
                log.info("plan rejected on attempt %d: %s", attempt + 1, e)
                messages += [
                    {"role": "assistant", "content": resp.content},
                    {"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": block.id, "is_error": True,
                        "content": f"Plan rejected: {e}. Fix it and call submit_plan again."}]},
                ]
        raise PlanError(f"LLM could not produce a valid plan: {last_err}")
