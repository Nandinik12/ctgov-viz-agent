"""Translate plan Filters into ClinicalTrials.gov v2 query parameters."""
from __future__ import annotations

from typing import Any

from ..planner.plan import Filters


def _essie_quote(term: str) -> str:
    # Wrap multi-word terms so Essie treats them as a phrase; strip quotes to avoid syntax errors.
    t = term.replace('"', "").strip()
    return f'"{t}"' if " " in t else t


def build_params(f: Filters) -> dict[str, Any]:
    p: dict[str, Any] = {}
    if f.drug:
        p["query.intr"] = _essie_quote(f.drug)
    if f.condition:
        p["query.cond"] = _essie_quote(f.condition)
    if f.sponsor:
        p["query.spons"] = _essie_quote(f.sponsor)
    if f.country:
        p["query.locn"] = _essie_quote(f.country)
    if f.free_text:
        p["query.term"] = f.free_text.replace('"', "")
    if f.statuses:
        p["filter.overallStatus"] = ",".join(s.value for s in f.statuses)

    adv: list[str] = []
    if f.phases:
        adv.append("AREA[Phase](" + " OR ".join(ph.value for ph in f.phases) + ")")
    if f.start_year or f.end_year:
        lo = f"{f.start_year}-01-01" if f.start_year else "MIN"
        hi = f"{f.end_year}-12-31" if f.end_year else "MAX"
        adv.append(f"AREA[StartDate]RANGE[{lo},{hi}]")
    if adv:
        p["filter.advanced"] = " AND ".join(adv)
    return p
