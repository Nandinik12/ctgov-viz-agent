# ClinicalTrials.gov Query to Visualization Agent


A backend service that turns a clinical trials question into a renderable visualization spec, backed by live
ClinicalTrials.gov API v2 data, with a citation trail from every bar, point, node and edge back to the exact field
in the source record.

```
POST /v1/visualize  {"query": "Show a network of sponsors and drugs for melanoma trials", "condition": "melanoma"}
  -> {"visualization": {"type": "network_graph", "encoding": {...}, "data": {"nodes": [...], "edges": [...]}},
      "meta": {"interpretation": {...}, "cohorts": [...], "warnings": [...], ...}}
```

Supported outputs: `bar_chart`, `grouped_bar_chart`, `time_series` (single and multi series), `histogram`,
`scatter_plot`, `network_graph` (bipartite and co-occurrence), `choropleth_map`. Questions the registry cannot
answer ("what is the best drug for X") or that are ambiguous ("compare them") get a structured
`unsupported` / `needs_clarification` response with suggested rephrasings instead of a forced chart.

**Reviewers in a hurry:** [`DESIGN.md`](DESIGN.md) is the one page version (diagram, guardrails, contracts).

---

## 1. How to run

Requires Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# optional: enables the LLM planner (otherwise the keyword planner is used)
export ANTHROPIC_API_KEY=sk-ant-...
# optional: export CTGOV_AGENT_MODEL=claude-sonnet-4-5

uvicorn ctgov_agent.api:app --reload --port 8000
```

| What | How |
|---|---|
| Demo UI | open http://localhost:8000/ (click any mark to see its citations) |
| API docs (Swagger) | http://localhost:8000/docs |
| JSON Schemas | `GET /v1/schema`, also checked in at `docs/schema.json` |
| One query from the terminal | `python -m ctgov_agent.cli "How are breast cancer trials distributed across phases?" --condition "breast cancer"` |
| Regenerate example outputs | `python examples/run_examples.py` (add `--rules` to force the keyword planner) |
| Tests (hermetic, no network) | `pytest -q` |
| Planner eval | `python evals/run_planner_eval.py` (add `--llm` for the Claude planner; `--cases evals/planner_cases_holdout.jsonl` or `evals/planner_cases_adversarial.jsonl` for the other sets) |
| Live API probe | `python scripts/probe_api.py` (re-verifies filter syntax and refreshes the real record fixtures) |
| Docker | `docker build -t ctgov-viz . && docker run -p 8000:8000 -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY ctgov-viz` |

Deploying: the Dockerfile runs as is on Render, Railway or Fly (set `ANTHROPIC_API_KEY` as a secret; the service
honors `$PORT`).

```bash
curl -s localhost:8000/v1/visualize -H 'content-type: application/json' \
  -d '{"query":"How has the number of trials for this drug changed over time?","drug_name":"Pembrolizumab"}' | jq .
```

---

## 2. Architecture

```
            ┌──────────────┐   QueryPlan (closed schema,   ┌───────────────┐
 request ──▶│   Planner    │── no numbers, no NCT ids) ───▶│   resolve()   │  merge structured fields,
            │ LLM | rules  │                               │               │  ground entities, validate
            └──────────────┘                               └───────┬───────┘
                                                                   │ 1 cohort, or N for comparisons
                                                           ┌───────▼───────┐
                                                           │  CTGovClient  │  paginate, retry/backoff,
                                                           │  (API v2)     │  TTL cache, record cap
                                                           └───────┬───────┘
                                                                   │ raw study JSON
                                                           ┌───────▼───────┐
                                                           │  extract.py   │  field -> Fact(value, json path, exact excerpt)
                                                           └───────┬───────┘
                                                           ┌───────▼───────┐
                                                           │   engine.py   │  deterministic aggregation;
                                                           │               │  citations = the facts that were counted
                                                           └───────┬───────┘
                                                                   ▼
                                                     VizResponse {visualization, meta}
```

| Module | Responsibility |
|---|---|
| `ctgov_agent/schemas.py` | Public request/response contract (pydantic). Single source of truth for docs and validation. |
| `ctgov_agent/planner/plan.py` | `QueryPlan` model and `validate_plan` semantic rules. |
| `ctgov_agent/planner/llm.py` | Claude with forced tool use; schema validated; one self correction round. |
| `ctgov_agent/planner/rules.py` | Keyword planner: offline mode and LLM fallback. |
| `ctgov_agent/planner/resolve.py` | Structured field overrides, entity grounding, chart_type / group_by overrides. |
| `ctgov_agent/ctgov/client.py` | HTTP client: pagination via `nextPageToken`, retries on 429/5xx, no retry on 400, cache. |
| `ctgov_agent/ctgov/query.py` | Plan filters to API params (`query.*`, `filter.overallStatus`, `filter.advanced`). |
| `ctgov_agent/ctgov/extract.py` | One extractor per dimension, returning values with provenance. |
| `ctgov_agent/analysis/engine.py` | One function per analysis kind; pure functions, no I/O. |
| `ctgov_agent/service.py` | Orchestration, empty result broadening, deterministic titles, metadata. |
| `ctgov_agent/api.py` | FastAPI app. |
| `web/index.html` | Demo renderer (Vega-Lite + D3) driven only by the documented spec: chart / table / JSON views, count vs share toggle, interpretation panel with the exact API queries, click to cite, light and dark themes. |
| `evals/` | Labeled planner eval sets and runner. |

### Query coverage

The planner maps any question onto one of six analysis kinds over a shared dimension vocabulary, so new question
types are combinations rather than new code paths.

| Analysis | Output type | Example question |
|---|---|---|
| `distribution` | `bar_chart` | How are breast cancer trials distributed across phases? Which countries have the most recruiting trials for X? Most common intervention types? Top sponsors? |
| `time_trend` | `time_series` | How has the number of trials for X changed per year since 2015? Optional `series_dimension` (e.g. trials per year by phase). |
| `comparison` | `grouped_bar_chart` or multi series `time_series` | Compare phases for Drug A vs Drug B. Compare sponsor classes across two conditions. Compare trials per year for A vs B. |
| `histogram` | `histogram` | Distribution of enrollment sizes; distribution of trial durations. |
| `scatter` | `scatter_plot` | Enrollment vs duration (one point per trial, colored by phase, log scale hint for enrollment). |
| `network` | `network_graph` | Sponsors to drugs (bipartite); drug to drug co-occurrence in combination studies; condition to drug; sponsor to country. |

Dimensions: `phase`, `start_year`, `overall_status`, `sponsor`, `sponsor_class`, `intervention`, `drug`,
`intervention_type`, `condition`, `country`, `study_type`, `enrollment`, `duration_months`.

---

## 3. Request schema

`POST /v1/visualize`, `Content-Type: application/json`. Unknown fields are rejected (`extra="forbid"`), so typos
fail loudly with a 422 instead of being silently ignored.

| Field | Type | Required | Validation / notes |
|---|---|---|---|
| `query` | string | **yes** | 3 to 1000 chars. Natural language question. |
| `drug_name` | string | no | max 200. Sent as `query.intr`. |
| `condition` | string | no | max 200. Sent as `query.cond`. |
| `sponsor` | string | no | max 200. Sent as `query.spons`. |
| `country` | string | no | max 100. Sent as `query.locn`. |
| `trial_phase` | array of enum | no | `EARLY_PHASE1`, `PHASE1`, `PHASE2`, `PHASE3`, `PHASE4`, `NA`. |
| `overall_status` | array of enum | no | `RECRUITING`, `COMPLETED`, `ACTIVE_NOT_RECRUITING`, `TERMINATED`, ... (API values). |
| `start_year` | int | no | 1900 to 2100. Trials starting in or after this year. |
| `end_year` | int | no | 1900 to 2100, must be >= `start_year`. |
| `compare` | object | no | `{"field": "drug" \| "condition" \| "sponsor" \| "country", "values": [2 to 5 distinct strings]}` |
| `group_by` | enum (dimension) | no | Forces the primary dimension (or the series split for time trends). |
| `chart_type` | enum (viz type) | no | Forces the output type; incompatible combinations return 422. |
| `max_records` | int | no | default 5000, 10 to 20000. Max trials fetched per cohort (pages of 1000). |
| `top_n` | int | no | default 15, 3 to 50. Max categories, series, or nodes per type. |
| `max_citations_per_datum` | int | no | default 3, 0 to 20. Detailed citations per datum (all NCT ids are always listed). |

Precedence: structured fields always override what the planner inferred from `query`.

---

## 4. Response schema

```jsonc
{
  "request_id": "uuid",
  "status": "ok | needs_clarification | unsupported",
  "visualization": {                      // present when status == "ok", else null
    "type": "bar_chart | grouped_bar_chart | time_series | histogram | scatter_plot | network_graph | choropleth_map",
    "title": "string (built deterministically from the plan, never LLM written)",
    "subtitle": "string (trials analyzed, filters, source)",
    "encoding": { "<channel>": Channel },
    "data": [ Row ]                       // or {"nodes": [Node], "edges": [Edge]} for network_graph
  },
  "clarification": {                     // present when status != "ok"
    "reason": "out_of_scope | ambiguous_reference | missing_cohorts | too_vague | no_matching_trials",
    "message": "string to show the user",
    "suggestions": ["answerable rephrasing", "..."]
  },
  "meta": ResponseMeta
}
```

A client switches on `status` first: `ok` renders `visualization`; anything else renders
`clarification.message` with `suggestions` as buttons. No chart data is ever invented to fill the gap.

### Channel

| Field | Type | Meaning |
|---|---|---|
| `field` | string | Key in each data row. |
| `type` | `nominal` \| `ordinal` \| `quantitative` \| `temporal` | Vega-Lite style measurement type. `temporal` fields are integer years. |
| `title` | string | Axis / legend title. |
| `sort` | `"ascending"` \| `"descending"` \| string[] | Explicit category order when given as a list (e.g. phase order). Rows are already emitted in this order. |
| `unit` | string? | e.g. `trials`, `participants`, `months`, `%`. |
| `scale` | `linear` \| `log`? | Suggested axis scale. |

### Channels per type

| Type | Channels | Row fields |
|---|---|---|
| `bar_chart` | `x` category, `y` count, `tooltip` share | `<dimension>`, `trial_count`, `share_pct`, `nct_ids`, `citations` |
| `grouped_bar_chart` | `x` category, `y` count, `series` cohort, `tooltip` share within cohort | `category`, `cohort`, `trial_count`, `share_pct`, `nct_ids`, `citations` (one row per category x cohort, zeros included) |
| `time_series` | `x` year, `y` count, optional `series` | `start_year`, `trial_count`, optional `series`, `nct_ids`, `citations` (missing years filled with 0) |
| `histogram` | `x` bin start, `x2` bin end, `y` count, `label` | `bin_start`, `bin_end` (null for the overflow bin), `bin_label`, `trial_count`, `nct_ids`, `citations` |
| `scatter_plot` | `x`, `y`, `color` phase, `tooltip` title, `id` | `nct_id`, `<x dim>`, `<y dim>`, `phase`, `title`, `citations` |
| `network_graph` | `node_id`, `node_label`, `node_group`, `node_size`, `edge_source`, `edge_target`, `edge_weight` | nodes: `id` (`"<type>:<key>"`), `label`, `group`, `trial_count`, `nct_ids`, `citations`; edges: `source`, `target`, `weight`, `nct_ids`, `citations` |
| `choropleth_map` | `geo` (ISO 3166 numeric, joins to `world-atlas` topojson ids), `color` count, `label`; plus `x`/`y` so a renderer without maps can draw the same rows as bars | `country`, `iso3`, `iso_numeric`, `trial_count`, `share_pct`, `nct_ids`, `citations` (all countries, sorted by count) |

When a cohort exceeded `max_records` and the dimension is filterable, bar rows also carry
`count_method: "exact"` and `sample_count`: `trial_count` is the exact API count for that bar, `sample_count` how many
of those trials were fetched (and therefore listed in `nct_ids` / cited).

A renderer never needs to inspect `type` to find fields: it reads `encoding.<channel>.field`. `statistics.layout_hint`
(`bipartite` or `force`) is provided for networks.

### Citation (on every datum)

```json
{"nct_id": "NCT02934503",
 "field": "protocolSection.armsInterventionsModule.interventions[0].name",
 "excerpt": "Pembrolizumab",
 "url": "https://clinicaltrials.gov/study/NCT02934503"}
```

`excerpt` is the exact value at `field` in the API record (list fields such as `phases` are rendered comma
separated, e.g. `"PHASE1, PHASE2"`). `nct_ids` lists every contributing trial, so `len(nct_ids) == trial_count`
(or `weight` for edges). Edge citations carry one fact per endpoint, so each shared trial is shown supporting both
entities.

### ResponseMeta

| Field | Meaning |
|---|---|
| `interpretation` | `planner` (`llm` or `rules`), `action` (`visualize`, `clarify`, `decline`), `analysis`, `dimension`, `secondary_dimension`, `series_dimension`, `rationale`. |
| `filters` | Final filters after merging and grounding (plus `compare` if any). |
| `cohorts[]` | Per cohort: `label`, exact `api_params` sent, `total_matching` (API `totalCount`), `records_analyzed`, `truncated`. |
| `units`, `sort`, `time_granularity` | Rendering hints. |
| `statistics` | e.g. `median`, `bin_width`, `spearman_rho`, `cohort_sizes`, `nodes`, `edges`, `entity_merges` (canonical name to merged variants), `exact_count_queries`. |
| `assumptions[]` | How counts were defined (multi valued fields, date semantics, excluded placebo arms, ...). |
| `warnings[]` | Truncation, omitted categories, ignored hallucinated entities, spelling corrections, broadened searches, partial years. |

Errors: `422` invalid request or unplannable combination (`{"detail": {"error": "unplannable_query", "message": ...}}`),
`502` ClinicalTrials.gov failure after retries.

---

## 5. Key design decisions and tradeoffs

**The LLM plans; code computes.** The only LLM output is a `QueryPlan`: an enum constrained description of which
trials and which aggregation. It cannot emit counts, NCT ids, chart data, or titles. That removes the most
hallucination prone step (the model summarizing data) entirely, and makes every number reproducible from
`meta.cohorts[].api_params`.

**Validation in layers.**
1. JSON Schema via forced tool use (`tool_choice` = `submit_plan`), then pydantic parsing.
2. `validate_plan` semantic rules (e.g. histogram needs a numeric dimension; co-occurrence needs a multi valued one).
   Failures go back to the model as a `tool_result` with `is_error: true` for one correction round; a second failure
   falls back to the keyword planner, and the response says so.
3. Entity grounding: every drug, condition, sponsor, country or comparison value in the plan must have all its
   tokens present in the user's own text or structured fields. Anything else is dropped with a warning. This stops
   the model from "helpfully" adding Keytruda when the user typed pembrolizumab, which would silently change the
   cohort.

**Citations by construction.** Extractors return `Fact(key, label, json_path, excerpt)`; aggregation buckets store
facts per NCT id. A datum's citations are literally the facts that were counted into it, so they cannot drift from
the numbers. Tests resolve every citation path against the source record and assert the excerpt matches.

**Counting rules are explicit, not incidental.**
* Combined phases (`PHASE1`+`PHASE2`) are one bucket, `Phase 1/Phase 2`, so phase bars sum to the trial count.
* Multi valued fields (countries, conditions, interventions) count each trial at most once per category; a trial
  with 40 US sites counts once for the United States. The response states that sums can exceed the trial count.
* Missing values become an explicit `Not reported` bucket or are reported in `statistics` instead of disappearing.
* `drug` keeps only `DRUG`/`BIOLOGICAL` interventions and drops placebo, saline and standard of care arms, which
  otherwise dominate every drug ranking and become the hub of every network. `intervention` keeps everything.
* Comparison cohorts are fetched independently, so trials testing both drugs appear in both; the overlap is
  reported, and `share_pct` is provided because cohort sizes often differ by an order of magnitude.
* Networks drop the user's own anchor condition (it would connect to every node) and keep the top N nodes per type
  and at most 150 strongest edges.

**Real world API handling.** Each analysis requests only the leaf fields it reads (e.g. a country chart asks for
`nctId`, `phases`, `locations`), so pages stay small and full coverage of a few thousand trials is a handful of
requests; if the API ever rejects a narrowed field list the client retries once with whole modules. Comparison
cohorts are fetched concurrently. Pages of up to 1000 are followed through `nextPageToken`, 429/5xx are retried with exponential backoff honoring
`Retry-After`, 400s are surfaced immediately with the API's message, and identical queries hit a 15 minute cache.
Month precision dates (`2017-05`) are handled. If a field specific search (`query.cond`) returns nothing, the same
terms are retried as full text (`query.term`) and the response warns about it.

**Fetch and aggregate locally, capped.** One code path serves every analysis and gives record level citations.
The cost is a `max_records` cap (default 5000 per cohort, up to 20000), which covers the large majority of
realistic questions completely. When it binds, `truncated: true` and a warning give the exact coverage, and the
demo UI shows a coverage KPI. The API's default ordering is relevance, so a truncated sample is not random.

**Deterministic titles.** Titles are templated from the resolved plan, so a title can never claim a filter the
data does not have.

**Knowing its limits.** The plan has an `action`: `visualize`, `clarify` or `decline`. Efficacy, safety, "best
treatment", outcomes, advice and off topic questions are declined with an explanation of what registry data can
and cannot say, plus answerable rephrasings. Unresolved references ("this drug" with no `drug_name`), comparisons
without two named cohorts, and questions with no topic get `needs_clarification`. A plan whose cohorts were all
dropped by grounding becomes a clarification rather than a silently different chart, and a query that matches
nothing (after the full text retry) returns `no_matching_trials` instead of an empty chart. Grounding tolerates
misspellings: the planner may fix "pembrolizumav" (token similarity >= 0.8) and the fix is reported in `warnings`,
but it still cannot introduce a new entity.

**Entity normalization from the records themselves.** Raw strings split one drug across many bars ("Pembrolizumab",
"MK-3475", "Keytruda", "Pembrolizumab 50 MG Injection [Keytruda]"). Interventions are clustered with union-find over
the sponsor's `otherNames`, bracketed aliases, and the MeSH terms ClinicalTrials.gov attaches
(`derivedSection.interventionBrowseModule`). Live records showed why guards are needed, so: class or umbrella
names ("Single agent, adjuvant anti-PD1 therapy" listing both Keytruda and Opdivo) never contribute aliases;
generic aliases ("chemotherapy", "checkpoint inhibitor") never link; two clusters anchored to different MeSH
substances never merge; and an alias that is itself a different MeSH substance in the same trial is refused (a
pembrolizumab arm whose `otherNames` were `[Carboplatin, Pemetrexed]`). Conditions use MeSH only when the term
*explains* the sponsor's text (every content token present, with cancer/tumor/carcinoma/neoplasm unified),
because condition MeSH lists are noisy (live oncology trials tagged "Parkinson Disease 4, Autosomal Dominant Lewy
Body"). Citations always quote the original spelling at its original path, and `statistics.entity_merges` lists
what was merged. All of this is tested on real API records in `tests/fixtures/ctgov_sample.json`.

**Exact counts beyond the fetch cap.** When a cohort exceeds `max_records` (breast cancer: 16,720 trials), bars for
filterable dimensions (phase, status, study type, sponsor class, start year) are replaced by exact API counts: one
`countTotal` query per bar, built from the cohort's own query plus a bucket clause such as
`AREA[Phase]PHASE1 AND AREA[Phase]PHASE2 AND NOT AREA[Phase]PHASE3 ...` or `AREA[Phase]MISSING`. Every clause
form was verified against the live API (`scripts/probe_api.py`, results in `tests/fixtures/essie_probe_counts.json`).
Citations keep coming from the fetched records, and rows say which counts are exact.

**Rate limits.** The probe also showed the API returns 429 after roughly ten back to back calls, so the client
spaces requests (`CTGOV_MIN_INTERVAL_S`, default 0.35s) on top of exponential backoff that honors `Retry-After`.

**Two planners, one contract.** The keyword planner exists so the service works with no API key and so LLM outages
degrade rather than fail. It is deliberately conservative: entities it cannot confidently type go to full text
search instead of being guessed as a condition or drug.

---

## 6. Limitations and what I would improve with more time

* **Normalization coverage.** Aliases come only from the records in the cohort. A curated vocabulary (RxNorm for
  drugs, MeSH tree lookups for conditions) would merge variants that never co-occur, and roll conditions up the
  MeSH hierarchy (e.g. all lung neoplasms) on request.
* **Sponsor aliasing.** `Merck Sharp & Dohme LLC` vs `Merck Sharp & Dohme Corp.` should be one sponsor.
* **Exact counts for every dimension.** Countries, drugs and conditions have no clean per bucket filter, so beyond
  `max_records` they stay sample based (and say so). Site level `geoPoint` data could also drive a point map.
* **More analyses.** Status funnels, enrollment by phase box plots, investigator or site networks, and time to
  completion survival curves fit the same plan / engine split.
* **Bigger evals.** Grow the adversarial set and add questions sampled from real usage; track LLM planner drift in CI.
* **Async + persistent cache.** The client is synchronous (FastAPI runs it in a threadpool); an async client and a
  shared cache (Redis or SQLite) would cut latency and API load.
* **Multi turn clarification.** `needs_clarification` returns suggestions; a session id would let the follow up
  answer fill in only the missing slot.

---

## 7. Planner evaluation

`evals/` holds labeled questions with the expected plan (analysis, dimensions, filters, cohorts, and hallucination
guards such as "must not add Keytruda"). `run_planner_eval.py` runs each one through planner + `resolve()` with no
network calls and reports per check accuracy.

| Planner | Dev set (30) | Held out set (15) | Adversarial set (20) |
|---|---|---|---|
| rules | 29/30 (97%) | 15/15 | 16/20 (80%) |
| llm (claude-sonnet-4-5) | 30/30 (100%) | 15/15 (100%) | run `--cases evals/planner_cases_adversarial.jsonl` |

The dev set was used while tuning the keyword planner, so its score is optimistic; the held out cases were written
afterwards and not tuned against. The one remaining rules miss ("conditions and drugs in Pfizer trials") needs to
know Pfizer is a sponsor, which is exactly the kind of world knowledge the LLM planner adds, and the LLM gets it
right. Full reports: `evals/results_*.md`.

The dev and held out sets are small and written by me, so a perfect score there means they are saturated. The
adversarial set was built to break things: out of scope questions (best drug, side effects, survival rates,
personal advice, weather), unresolved references ("compare them"), comparisons without cohorts, misspelled drugs
and conditions, trap wording that *looks* out of scope but is a valid count question ("trials studying CAR-T
efficacy per year"), a prompt injection asking for fabricated numbers, and brand names that must not be expanded.
The keyword planner's four misses are exactly the trap wording and misspelled cohort cases, which is the
argument for the LLM planner; the runtime falls back to rules only when the LLM fails.

## 8. Example runs

`examples/` holds 5 primary queries plus 4 extra, each file containing the exact request and the actual JSON
response produced by the service against the live API:

| # | Question | Type |
|---|---|---|
| 01 | How has the number of trials for Pembrolizumab changed per year since 2015? | `time_series` |
| 02 | How are breast cancer trials distributed across phases? | `bar_chart` |
| 03 | Compare phases for trials involving Pembrolizumab vs Nivolumab | `grouped_bar_chart` |
| 04 | Which countries have the most recruiting trials for type 2 diabetes? | `choropleth_map` |
| 05 | Show a network of sponsors and drugs for melanoma trials | `network_graph` (bipartite) |
| 06 | Which drugs frequently co-occur in combination studies for NSCLC (since 2018)? | `network_graph` (co-occurrence) |
| 07 | Distribution of enrollment sizes for phase 3 Alzheimer disease trials | `histogram` |
| 08 | What is the best drug for breast cancer? | `unsupported` (out_of_scope) |
| 09 | How are pembrolizumav trials distributed across phases? (misspelled) | `bar_chart`, with the correction in `warnings` |

Regenerate with `python examples/run_examples.py`.

---

## 9. AI tools, validation, and what was designed vs generated

**Tools.** Claude (Anthropic) as a pair programmer for scaffolding, code drafting, and the demo UI

**How correctness was validated.**
* 55 hermetic tests (`pytest -q`) over a fake API whose records follow the real v2 JSON shape (checked against a live
  response). They cover extraction edge cases (month precision dates, combined phases, missing fields, duplicate
  countries, placebo exclusion), exact expected counts for every analysis, gap filling, overflow bins, network edge
  weights, comparison overlap, pagination, retry and no retry behavior, request validation, entity grounding,
  structured field precedence, field narrowing and its fallback, the LLM self correction loop (stubbed client), and
  LLM to rules fallback.
* Normalization, filter syntax and rate limiting were checked against the live API with `scripts/probe_api.py`; the
  captured records and counts are test fixtures, so the trickiest real data cases (umbrella arms, sponsor alias
  typos, noisy MeSH) are regression tested.
* The planner eval in section 7, on dev, held out and adversarial sets. A ready to use CI workflow
  (`.github/workflows/ci.yml`) runs the tests and the evals with regression floors once the repo is on GitHub.
* A citation integrity check: every emitted citation path is resolved against the source record and the excerpt
  must equal the stored value.
* The demo UI was driven headlessly through every preset to confirm the spec renders without type specific
  guessing and that clicking a mark surfaces its citations.
* Example outputs were spot checked against clinicaltrials.gov study pages.
* Independent cross check: example 02 fetched all 16,720 breast cancer trials and counted phases locally; the counts
  match the separate `countTotal` probe queries exactly (Phase 2 only 3,544; Phase 1/Phase 2 866; Not Applicable
  4,898; no phase reported 3,673), which validates both the extraction rules and the exact count clauses.
* Live runs surfaced a real failure: the installed Anthropic SDK rejected the `temperature` argument, so every LLM
  call failed. The fallback kept the service answering (every example still rendered via the keyword planner, with
  a warning in `meta.warnings`), which is the degradation path working as designed. The argument was removed and the
  examples regenerated with the LLM planner.

**Designed deliberately:** the plan / execute split and the closed `QueryPlan` vocabulary; the grounding check; the
provenance carrying `Fact` model that makes citations structural; the counting rules in section 5; the encoding
contract; the failure and fallback behavior.

**Generated and adapted:** boilerplate (FastAPI wiring, CLI, argparse), most test scaffolding, the keyword
planner's regexes, and the Vega-Lite / D3 demo page, all reviewed and iterated after running them.
