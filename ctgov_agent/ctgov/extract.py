"""Field extractors: raw v2 study JSON -> facts with provenance.

Every value the engine aggregates comes out of `extract()` together with the JSON
path and the exact string it was read from. That pairing is what makes deep
citations free: a datum's citations are just the facts that were counted into it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Optional

from ..schemas import Dimension
from .normalize import canonical, canonical_condition

PHASE_LABELS = {
    "EARLY_PHASE1": "Early Phase 1", "PHASE1": "Phase 1", "PHASE2": "Phase 2",
    "PHASE3": "Phase 3", "PHASE4": "Phase 4", "NA": "Not Applicable",
}
PHASE_ORDER = ["Early Phase 1", "Phase 1", "Phase 1/Phase 2", "Phase 2", "Phase 2/Phase 3",
               "Phase 3", "Phase 4", "Not Applicable", "Not reported"]
STATUS_ORDER = ["NOT_YET_RECRUITING", "RECRUITING", "ENROLLING_BY_INVITATION", "ACTIVE_NOT_RECRUITING",
                "SUSPENDED", "TERMINATED", "COMPLETED", "WITHDRAWN", "UNKNOWN"]

# Comparator arms that would otherwise dominate every drug ranking / network.
NON_DRUG_PATTERN = re.compile(
    r"^(placebo|placebos|matching placebo|placebo comparator|saline|normal saline|"
    r"standard of care|best supportive care|vehicle)\b", re.I)


@dataclass(frozen=True)
class Fact:
    key: str        # normalized grouping key
    label: str      # display label
    path: str       # JSON path in the study record
    excerpt: str    # exact value at `path`


def nct_id(study: dict[str, Any]) -> str:
    return study.get("protocolSection", {}).get("identificationModule", {}).get("nctId", "UNKNOWN")


def _get(d: Any, *keys: Any) -> Any:
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k)
        elif isinstance(d, list) and isinstance(k, int) and 0 <= k < len(d):
            d = d[k]
        else:
            return None
    return d


def norm_key(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().strip(".,;:").casefold()


def parse_date(s: Optional[str]) -> Optional[date]:
    """API dates are 'YYYY-MM-DD' or 'YYYY-MM' (day omitted). Treat missing day as the 1st."""
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$", s.strip())
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3] or 1))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# categorical extractors -> list[Fact]
# --------------------------------------------------------------------------- #
P = "protocolSection"


def _phase(s: dict) -> list[Fact]:
    phases = _get(s, P, "designModule", "phases") or []
    path = f"{P}.designModule.phases"
    if not phases:
        return [Fact("not_reported", "Not reported", path, "(field absent)")]
    labels = [PHASE_LABELS.get(p, p) for p in phases]
    # Combined phases (e.g. PHASE1+PHASE2) are one trial in one bucket, never double counted.
    label = "/".join(labels)
    return [Fact(norm_key(label), label, path, ", ".join(phases))]


def _start_year(s: dict) -> list[Fact]:
    raw = _get(s, P, "statusModule", "startDateStruct", "date")
    d = parse_date(raw)
    if not d:
        return []
    return [Fact(str(d.year), str(d.year), f"{P}.statusModule.startDateStruct.date", raw)]


def _status(s: dict) -> list[Fact]:
    v = _get(s, P, "statusModule", "overallStatus")
    return [Fact(v, v, f"{P}.statusModule.overallStatus", v)] if v else []


def _sponsor(s: dict) -> list[Fact]:
    v = _get(s, P, "sponsorCollaboratorsModule", "leadSponsor", "name")
    return [Fact(norm_key(v), v, f"{P}.sponsorCollaboratorsModule.leadSponsor.name", v)] if v else []


def _sponsor_class(s: dict) -> list[Fact]:
    v = _get(s, P, "sponsorCollaboratorsModule", "leadSponsor", "class")
    return [Fact(v, v, f"{P}.sponsorCollaboratorsModule.leadSponsor.class", v)] if v else []


def _interventions(s: dict, drugs_only: bool) -> list[Fact]:
    out, seen = [], set()
    for i, iv in enumerate(_get(s, P, "armsInterventionsModule", "interventions") or []):
        name, typ = iv.get("name"), iv.get("type")
        if not name:
            continue
        if drugs_only and (typ not in ("DRUG", "BIOLOGICAL") or NON_DRUG_PATTERN.match(name)):
            continue
        k, label = canonical(s, i) or (norm_key(name), name.strip())  # alias-merged when annotated
        if k in seen:
            continue
        seen.add(k)
        out.append(Fact(k, label, f"{P}.armsInterventionsModule.interventions[{i}].name", name))
    return out


def _intervention_type(s: dict) -> list[Fact]:
    out, seen = [], set()
    for i, iv in enumerate(_get(s, P, "armsInterventionsModule", "interventions") or []):
        t = iv.get("type")
        if t and t not in seen:
            seen.add(t)
            out.append(Fact(t, t, f"{P}.armsInterventionsModule.interventions[{i}].type", t))
    return out


def _condition(s: dict) -> list[Fact]:
    """Conditions as written by the sponsor, mapped to a MeSH term when one of the record's MeSH terms explains
    the text (see normalize.annotate_conditions). Citations always quote the sponsor's own string."""
    out, seen = [], set()
    for i, c in enumerate(_get(s, P, "conditionsModule", "conditions") or []):
        k, label = canonical_condition(s, i) or (norm_key(c), c.strip())
        if k and k not in seen:
            seen.add(k)
            out.append(Fact(k, label, f"{P}.conditionsModule.conditions[{i}]", c))
    return out


def _country(s: dict) -> list[Fact]:
    out, seen = [], set()
    for i, loc in enumerate(_get(s, P, "contactsLocationsModule", "locations") or []):
        c = loc.get("country")
        if c and c not in seen:  # one count per trial per country, regardless of site count
            seen.add(c)
            out.append(Fact(norm_key(c), c, f"{P}.contactsLocationsModule.locations[{i}].country", c))
    return out


def _study_type(s: dict) -> list[Fact]:
    v = _get(s, P, "designModule", "studyType")
    return [Fact(v, v, f"{P}.designModule.studyType", v)] if v else []


# --------------------------------------------------------------------------- #
# numeric extractors -> (value, [Fact supporting it]) or None
# --------------------------------------------------------------------------- #
def _enrollment(s: dict) -> Optional[tuple[float, list[Fact]]]:
    v = _get(s, P, "designModule", "enrollmentInfo", "count")
    if not isinstance(v, (int, float)) or v < 0:
        return None
    return float(v), [Fact(str(v), str(v), f"{P}.designModule.enrollmentInfo.count", str(v))]


def _duration_months(s: dict) -> Optional[tuple[float, list[Fact]]]:
    sm = _get(s, P, "statusModule") or {}
    start_raw = _get(sm, "startDateStruct", "date")
    end_field = "primaryCompletionDateStruct" if _get(sm, "primaryCompletionDateStruct", "date") else "completionDateStruct"
    end_raw = _get(sm, end_field, "date")
    a, b = parse_date(start_raw), parse_date(end_raw)
    if not a or not b or b < a:
        return None
    months = round((b - a).days / 30.4375, 1)
    return months, [
        Fact(start_raw, start_raw, f"{P}.statusModule.startDateStruct.date", start_raw),
        Fact(end_raw, end_raw, f"{P}.statusModule.{end_field}.date", end_raw),
    ]


def _start_year_numeric(s: dict) -> Optional[tuple[float, list[Fact]]]:
    f = _start_year(s)
    return (float(f[0].key), f) if f else None


CATEGORICAL: dict[Dimension, Callable[[dict], list[Fact]]] = {
    Dimension.phase: _phase,
    Dimension.start_year: _start_year,
    Dimension.overall_status: _status,
    Dimension.sponsor: _sponsor,
    Dimension.sponsor_class: _sponsor_class,
    Dimension.intervention: lambda s: _interventions(s, drugs_only=False),
    Dimension.drug: lambda s: _interventions(s, drugs_only=True),
    Dimension.intervention_type: _intervention_type,
    Dimension.condition: _condition,
    Dimension.country: _country,
    Dimension.study_type: _study_type,
}
NUMERIC: dict[Dimension, Callable[[dict], Optional[tuple[float, list[Fact]]]]] = {
    Dimension.enrollment: _enrollment,
    Dimension.duration_months: _duration_months,
    Dimension.start_year: _start_year_numeric,
}

LABELS = {
    Dimension.phase: "Phase", Dimension.start_year: "Start year", Dimension.overall_status: "Overall status",
    Dimension.sponsor: "Lead sponsor", Dimension.sponsor_class: "Sponsor class",
    Dimension.intervention: "Intervention", Dimension.drug: "Drug", Dimension.intervention_type: "Intervention type",
    Dimension.condition: "Condition", Dimension.country: "Country", Dimension.study_type: "Study type",
    Dimension.enrollment: "Enrollment", Dimension.duration_months: "Duration",
}
UNITS = {Dimension.enrollment: "participants", Dimension.duration_months: "months"}


# Leaf paths each dimension needs. Requesting only these (instead of whole modules) keeps pages small
# enough that full coverage of a few thousand trials is a handful of fast requests.
_ID = ["protocolSection.identificationModule.nctId", "protocolSection.identificationModule.briefTitle"]
FIELDS_FOR: dict[Dimension, list[str]] = {
    Dimension.phase: [f"{P}.designModule.phases"],
    Dimension.start_year: [f"{P}.statusModule.startDateStruct"],
    Dimension.overall_status: [f"{P}.statusModule.overallStatus"],
    Dimension.sponsor: [f"{P}.sponsorCollaboratorsModule.leadSponsor"],
    Dimension.sponsor_class: [f"{P}.sponsorCollaboratorsModule.leadSponsor"],
    Dimension.intervention: [f"{P}.armsInterventionsModule.interventions", "derivedSection.interventionBrowseModule.meshes"],
    Dimension.drug: [f"{P}.armsInterventionsModule.interventions", "derivedSection.interventionBrowseModule.meshes"],
    Dimension.intervention_type: [f"{P}.armsInterventionsModule.interventions"],
    Dimension.condition: [f"{P}.conditionsModule.conditions", "derivedSection.conditionBrowseModule.meshes"],
    Dimension.country: [f"{P}.contactsLocationsModule.locations"],
    Dimension.study_type: [f"{P}.designModule.studyType"],
    Dimension.enrollment: [f"{P}.designModule.enrollmentInfo"],
    Dimension.duration_months: [f"{P}.statusModule.startDateStruct", f"{P}.statusModule.primaryCompletionDateStruct",
                                f"{P}.statusModule.completionDateStruct"],
}


def fields_for(*dims: Optional[Dimension]) -> list[str]:
    out = list(_ID) + FIELDS_FOR[Dimension.phase]  # phase is always useful context (scatter color)
    for d in dims:
        if d is not None:
            out += [f for f in FIELDS_FOR[d] if f not in out]
    return out
