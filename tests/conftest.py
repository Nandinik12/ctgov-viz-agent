"""Test fixtures: a fake ClinicalTrials.gov v2 endpoint.

Records follow the real v2 study JSON shape (verified against a live response for
NCT02934503) but the trials themselves are synthetic, so tests are hermetic and
expected counts are known exactly.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx
import pytest

from ctgov_agent.ctgov.client import CTGovClient
from ctgov_agent.service import VizService


def study(nct: str, *, phases: Optional[list[str]], start: Optional[str], end: Optional[str] = None,
          sponsor: str = "Merck Sharp & Dohme LLC", sponsor_class: str = "INDUSTRY",
          interventions: list[tuple[str, str]] = (), conditions: list[str] = (),
          countries: list[str] = (), status: str = "COMPLETED", enrollment: Optional[int] = 100,
          other_names: Optional[dict[str, list[str]]] = None, condition_mesh: Optional[list[str]] = None,
          intervention_mesh: Optional[list[str]] = None) -> dict[str, Any]:
    design: dict[str, Any] = {"studyType": "INTERVENTIONAL"}
    if phases is not None:
        design["phases"] = phases
    if enrollment is not None:
        design["enrollmentInfo"] = {"count": enrollment, "type": "ACTUAL"}
    sm: dict[str, Any] = {"overallStatus": status}
    if start:
        sm["startDateStruct"] = {"date": start, "type": "ACTUAL"}
    if end:
        sm["primaryCompletionDateStruct"] = {"date": end, "type": "ACTUAL"}
    other_names = other_names or {}
    rec: dict[str, Any] = {"protocolSection": {
        "identificationModule": {"nctId": nct, "briefTitle": f"Synthetic study {nct}"},
        "statusModule": sm,
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": sponsor, "class": sponsor_class}},
        "conditionsModule": {"conditions": list(conditions)},
        "designModule": design,
        "armsInterventionsModule": {"interventions": [
            {"type": t, "name": n, **({"otherNames": other_names[n]} if n in other_names else {})}
            for t, n in interventions]},
        "contactsLocationsModule": {"locations": [{"facility": f"Site {i}", "country": c}
                                                  for i, c in enumerate(countries)]},
    }}
    derived: dict[str, Any] = {}
    if condition_mesh:
        derived["conditionBrowseModule"] = {"meshes": [{"id": f"D{i:06d}", "term": t} for i, t in enumerate(condition_mesh)]}
    if intervention_mesh:
        derived["interventionBrowseModule"] = {"meshes": [{"id": f"C{i:06d}", "term": t} for i, t in enumerate(intervention_mesh)]}
    if derived:
        rec["derivedSection"] = derived
    return rec


PEMBRO = ("BIOLOGICAL", "Pembrolizumab")
NIVO = ("BIOLOGICAL", "Nivolumab")
CHEMO = ("DRUG", "Carboplatin")
PLACEBO = ("DRUG", "Placebo")

STUDIES = [
    study("NCT00000001", phases=["PHASE1"], start="2016-03-01", end="2018-03-01", interventions=[PEMBRO],
          conditions=["Melanoma"], countries=["United States", "United States", "Canada"], enrollment=30),
    study("NCT00000002", phases=["PHASE2"], start="2017-05", end="2019-11", interventions=[PEMBRO, CHEMO],
          conditions=["Non-small Cell Lung Cancer"], countries=["United States"], enrollment=120),
    study("NCT00000003", phases=["PHASE3"], start="2017-09-15", end="2021-09-15", interventions=[PEMBRO, CHEMO, PLACEBO],
          conditions=["Non-small Cell Lung Cancer"], countries=["Japan", "Germany"], enrollment=900, status="RECRUITING"),
    study("NCT00000004", phases=["PHASE1", "PHASE2"], start="2019-01-01", end="2020-06-01",
          interventions=[PEMBRO, NIVO], conditions=["Melanoma"], countries=["Germany"], enrollment=60,
          sponsor="NYU Langone Health", sponsor_class="OTHER"),
    study("NCT00000005", phases=["PHASE2"], start="2019-07-01", end=None, interventions=[NIVO, CHEMO],
          conditions=["Melanoma"], countries=["Canada"], enrollment=None,
          sponsor="Bristol-Myers Squibb", status="RECRUITING"),
    study("NCT00000006", phases=None, start="2020-02-01", end="2020-08-01", interventions=[NIVO],
          conditions=["Renal Cell Carcinoma"], countries=[], enrollment=45,
          sponsor="Bristol-Myers Squibb"),
    study("NCT00000007", phases=["PHASE3"], start=None, interventions=[NIVO, PLACEBO],
          conditions=["Renal Cell Carcinoma"], countries=["United States"], enrollment=5000,
          sponsor="Bristol-Myers Squibb"),
]


FIELD_SCOPE = {  # roughly mirrors which record sections each v2 query.* param searches
    "query.intr": lambda p: p["armsInterventionsModule"],
    "query.cond": lambda p: p["conditionsModule"],
    "query.spons": lambda p: p["sponsorCollaboratorsModule"],
    "query.locn": lambda p: p["contactsLocationsModule"],
    "query.term": lambda p: p,
}


def _matches(s: dict, params: dict[str, str]) -> bool:
    for k, scope in FIELD_SCOPE.items():
        if k in params and params[k].strip('"').lower() not in json.dumps(scope(s["protocolSection"])).lower():
            return False
    if "filter.overallStatus" in params:
        if s["protocolSection"]["statusModule"]["overallStatus"] not in params["filter.overallStatus"].split(","):
            return False
    if "filter.advanced" in params and not _advanced_ok(s, params["filter.advanced"]):
        return False
    return True


def _advanced_ok(s: dict, adv: str) -> bool:
    """Tiny evaluator for the Essie clauses this codebase emits (AND of AREA[...] terms, optional NOT)."""
    import re
    ps = s["protocolSection"]
    phases = ps["designModule"].get("phases") or []
    start = (ps["statusModule"].get("startDateStruct") or {}).get("date")
    for raw in re.split(r"\s+AND\s+(?![^\[]*\])", adv.replace("(", " ").replace(")", " ")):
        term = raw.strip()
        if not term:
            continue
        neg = term.startswith("NOT ")
        term = term[4:].strip() if neg else term
        m = re.match(r"AREA\[(\w+)\](.*)", term)
        area, val = m[1], m[2].strip()
        if area == "Phase":
            ok = (not phases) if val == "MISSING" else any(v in phases for v in re.split(r"\s+OR\s+", val))
        elif area == "OverallStatus":
            ok = ps["statusModule"]["overallStatus"] == val
        elif area == "StudyType":
            ok = ps["designModule"].get("studyType") == val
        elif area == "LeadSponsorClass":
            ok = ps["sponsorCollaboratorsModule"]["leadSponsor"]["class"] == val
        elif area == "StartDate":
            lo, hi = re.match(r"RANGE\[([^,]+),([^\]]+)\]", val).groups()
            ok = bool(start) and (lo == "MIN" or start >= lo[:len(start)]) and (hi == "MAX" or start <= hi[:len(start)])
        else:
            raise AssertionError(f"fake API does not understand {term}")
        if ok == neg:
            return False
    return True


class FakeCTGov:
    def __init__(self, studies: list[dict]) -> None:
        self.studies = studies
        self.calls: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.calls.append(params)
        hits = [s for s in self.studies if _matches(s, params)]
        size = int(params.get("pageSize", 10))
        off = int(params.get("pageToken", "0"))
        page = hits[off: off + size]
        body: dict[str, Any] = {"studies": page, "totalCount": len(hits)}
        if off + size < len(hits):
            body["nextPageToken"] = str(off + size)
        return httpx.Response(200, json=body)


@pytest.fixture
def fake():
    return FakeCTGov(STUDIES)


@pytest.fixture
def service(fake):
    client = CTGovClient(transport=httpx.MockTransport(fake), cache_ttl_s=0)
    client.min_interval_s = 0
    return VizService(client=client, use_llm=False)


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setenv("CTGOV_MIN_INTERVAL_S", "0")
