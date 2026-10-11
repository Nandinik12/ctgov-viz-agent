"""Probe live ClinicalTrials.gov API behavior that the code depends on.

    python scripts/probe_api.py   -> writes scripts/probe_out.json

Checks Essie filter syntax used for exact counts, and captures real derivedSection
(MeSH) payloads used for entity normalization and test fixtures.
"""
import json, pathlib, time, httpx

API = "https://clinicaltrials.gov/api/v2/studies"
out = {"counts": {}, "samples": {}}
c = httpx.Client(timeout=30)

def count(params):
    time.sleep(1.0)  # the API rate limits bursts (429 after ~10 quick calls)
    r = c.get(API, params={**params, "countTotal": "true", "pageSize": 1, "fields": "protocolSection.identificationModule.nctId", "format": "json"})
    return {"status": r.status_code, "total": r.json().get("totalCount") if r.status_code == 200 else r.text[:200]}

base = {"query.cond": '"breast cancer"'}
tests = {
    "none": {},
    "phase_missing_only": {"filter.advanced": "AREA[Phase]MISSING"},
    "phase2_any": {"filter.advanced": "AREA[Phase]PHASE2"},
    "phase2_only": {"filter.advanced": "AREA[Phase]PHASE2 AND NOT AREA[Phase]PHASE1 AND NOT AREA[Phase]PHASE3"},
    "phase1_and_2": {"filter.advanced": "AREA[Phase]PHASE1 AND AREA[Phase]PHASE2"},
    "phase_missing": {"filter.advanced": "AREA[Phase]MISSING"},
    "phase_na": {"filter.advanced": "AREA[Phase]NA"},
    "status_recruiting_adv": {"filter.advanced": "AREA[OverallStatus]RECRUITING"},
    "status_recruiting_filter": {"filter.overallStatus": "RECRUITING"},
    "studytype_interventional": {"filter.advanced": "AREA[StudyType]INTERVENTIONAL"},
    "sponsorclass_industry": {"filter.advanced": "AREA[LeadSponsorClass]INDUSTRY"},
    "start_2015": {"filter.advanced": "AREA[StartDate]RANGE[2015-01-01,2015-12-31]"},
    "start_missing": {"filter.advanced": "AREA[StartDate]MISSING"},
}
for k, p in tests.items():
    try:
        out["counts"][k] = count({**base, **p})
    except Exception as e:
        out["counts"][k] = {"error": str(e)}

fields = ",".join([
    "protocolSection.identificationModule.nctId",
    "protocolSection.conditionsModule.conditions",
    "protocolSection.armsInterventionsModule.interventions",
    "derivedSection.conditionBrowseModule",
    "derivedSection.interventionBrowseModule",
])
time.sleep(2)
for name, params in {"melanoma": {"query.cond": "melanoma"}, "pembrolizumab": {"query.intr": "pembrolizumab"}}.items():
    time.sleep(1.5)
    r = c.get(API, params={**params, "pageSize": 15, "fields": fields, "format": "json"})
    out["samples"][name] = {"status": r.status_code, "body": r.json() if r.status_code == 200 else r.text[:300]}

time.sleep(1.5)
leaf = c.get(API, params={"query.cond": "melanoma", "pageSize": 2, "format": "json",
                          "fields": "protocolSection.identificationModule.nctId,protocolSection.statusModule.startDateStruct"})
out["leaf_fields"] = {"status": leaf.status_code, "body": leaf.json() if leaf.status_code == 200 else leaf.text[:300]}

p = pathlib.Path(__file__).with_name("probe_out.json")
p.write_text(json.dumps(out, indent=1))
print(json.dumps(out["counts"], indent=1)); print("wrote", p)
