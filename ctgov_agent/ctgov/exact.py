"""Exact per-bucket counts for cohorts too large to fetch.

When a cohort is truncated (e.g. 5,000 of 16,720 breast cancer trials), bars computed from the fetched records
are a sample. For dimensions the API can filter on, each bar's true size is one cheap request: the cohort's own
query plus a bucket clause, `countTotal=true`, `pageSize=1`. Bars are then exact regardless of cohort size;
citations still come from the fetched records.

Supported: phase (including combined phases and "Not reported" via MISSING), overall_status, study_type,
sponsor_class, start_year. Clause syntax was checked against the live API (scripts/probe_api.py).
Anything else (or any failed request) keeps the sample count and says so.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from ..schemas import Dimension
from .client import CTGovClient, CTGovError
from .extract import PHASE_LABELS

log = logging.getLogger(__name__)

EXACT_DIMENSIONS = {Dimension.phase, Dimension.overall_status, Dimension.study_type, Dimension.sponsor_class,
                    Dimension.start_year}
MAX_QUERIES = 60
_CODE_BY_LABEL = {v: k for k, v in PHASE_LABELS.items()}


def bucket_clause(dim: Dimension, label: Any) -> Optional[str]:
    """Essie clause selecting exactly the trials in one bucket, or None if not expressible."""
    if dim == Dimension.phase:
        if label == "Not reported":
            return "AREA[Phase]MISSING"  # verified against the live API: matches records with no phase
        codes = [_CODE_BY_LABEL.get(p) for p in str(label).split("/")]
        if not codes or None in codes:
            return None
        others = [c for c in PHASE_LABELS if c not in codes]
        return " AND ".join([f"AREA[Phase]{c}" for c in codes] + [f"NOT AREA[Phase]{c}" for c in others])
    if dim == Dimension.overall_status:
        return f"AREA[OverallStatus]{label}"
    if dim == Dimension.study_type:
        return f"AREA[StudyType]{label}"
    if dim == Dimension.sponsor_class:
        return f"AREA[LeadSponsorClass]{label}"
    if dim == Dimension.start_year:
        return f"AREA[StartDate]RANGE[{int(label)}-01-01,{int(label)}-12-31]"
    return None


def with_clause(params: dict[str, Any], clause: str) -> dict[str, Any]:
    p = dict(params)
    prev = p.get("filter.advanced")
    p["filter.advanced"] = f"({prev}) AND ({clause})" if prev else clause
    return p


def exact_counts(client: CTGovClient, jobs: list[tuple[str, dict[str, Any]]]) -> dict[str, int]:
    """jobs: [(job_id, params)] -> {job_id: totalCount}. Raises CTGovError on the first failure."""
    if len(jobs) > MAX_QUERIES:
        raise CTGovError(f"{len(jobs)} bucket queries exceeds the cap of {MAX_QUERIES}")
    with ThreadPoolExecutor(max_workers=4) as pool:  # the client's throttle spaces the actual requests
        results = list(pool.map(lambda j: (j[0], client.count(j[1])), jobs))
    return dict(results)
