"""Entity normalization: merge spelling, brand and code-name variants of the same intervention.

Registry text is messy: one drug appears as "Pembrolizumab", "MK-3475", "Keytruda", "pembrolizumab 200 mg".
Counting raw strings splits one entity across several bars or network nodes. This module builds alias
clusters for a cohort from two signals that come from the records themselves (no external lookup):

1. `interventions[i].otherNames` and parenthesised aliases, written by the sponsor ("Pembrolizumab (MK-3475)").
2. MeSH terms that ClinicalTrials.gov attaches in `derivedSection.interventionBrowseModule.meshes`.

Clusters are built with union-find. Two guards keep it from snowballing: generic aliases ("chemotherapy",
"placebo", "standard of care") never link anything, and two clusters anchored to different MeSH terms are never
merged. The cluster's display label is its MeSH term when it has one, else its most frequent spelling.

The result is written into each study under the private key `_norm` (index -> (key, label)); extractors read it
while citations keep pointing at the original, unmodified fields.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Optional

GENERIC_ALIASES = re.compile(
    r"^(placebo|matching placebo|saline|normal saline|vehicle|chemotherapy|chemo|standard of care|soc|"
    r"best supportive care|supportive care|control|drug|study drug|investigational product|ip|vaccine|"
    r"radiation|radiotherapy|surgery|observation|usual care|treatment|therapy|immunotherapy|"
    r"biological|biologic|antibody|monoclonal antibody|other|none|n/a|na|tbd|tumor tissue|blood)$", re.I)
# names/aliases describing a class or a choice rather than one substance; seen in live records, e.g.
# "Single agent, adjuvant anti-PD1 therapy" with otherNames [OPDIVO, Keytruda, Pembrolizumab, Nivolumab]
CLASS_WORDS = re.compile(r"\b(therapy|therapies|agent|agents|inhibitor|inhibitors|regimen|chemotherapy|treatment|"
                         r"medication|rescue|standard|any|single|either|investigator'?s choice|class|component)\b", re.I)
COMBO = re.compile(r"\+| and |/|,| or | with |;", re.I)
# MeSH terms that describe a form or a practice, not a substance (also seen in live records)
GENERIC_MESH = {"pharmaceutical preparations", "drug therapy", "injections", "antibodies", "vaccines",
                "antibodies, monoclonal", "immunotherapy", "radiotherapy", "tablets", "capsules", "solutions"}
DOSE = re.compile(r"\b\d+(\.\d+)?\s*(mg/kg|mg/m2|mg|mcg|µg|ug|g|ml|iu|units?|%)\b.*$", re.I)
PAREN = re.compile(r"[\(\[]([^)\]]{2,40})[\)\]]")
MARKS = re.compile(r"[®™©]")


def _clean(name: str) -> str:
    n = MARKS.sub("", PAREN.sub(" ", name))
    n = DOSE.sub("", n)
    return re.sub(r"\s+", " ", n).strip(" ,;:.-").casefold()


def _key(name: str) -> str:
    """Grouping key for an intervention name. "Immunotherapy (Pembrolizumab)" keys on the specific part."""
    outer = _clean(name)
    inner = [m for m in PAREN.findall(name) if _usable_alias(m)]
    if (not outer or GENERIC_ALIASES.match(outer) or CLASS_WORDS.search(outer)) and len(inner) == 1:
        return _clean(inner[0])
    return outer or name.casefold()


def _split_aliases(raw: list[str]) -> list[str]:
    out = []
    for a in raw or []:
        out += [p.strip() for p in re.split(r",\s+", MARKS.sub("", a))]  # "Mektovi, PF-06811462, MEK162"
    return out


def _usable_alias(a: str) -> bool:
    a = MARKS.sub("", a).strip()
    return (2 <= len(a) <= 60 and not GENERIC_ALIASES.match(a) and not CLASS_WORDS.search(a)
            and not COMBO.search(a))


class _UF:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.mesh: dict[str, set[str]] = defaultdict(set)

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        ma, mb = self.mesh[ra], self.mesh[rb]
        if ma and mb and ma != mb:
            return False  # two different MeSH anchors: these are different substances
        self.parent[rb] = ra
        self.mesh[ra] |= self.mesh.pop(rb, set())
        return True


def annotate_interventions(studies: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Annotate studies in place; return {canonical label: [variant spellings]} for merged clusters."""
    uf = _UF()
    spelling: Counter[tuple[str, str]] = Counter()  # (cleaned key, original spelling)

    for s in studies:
        s.setdefault("_norm", {})
    # pass 1: register names and MeSH anchors (before any alias links, so the conflict guard sees every anchor)
    pending: list[tuple[str, list[str], set[str], dict[str, str]]] = []
    for s in studies:
        ps = s.get("protocolSection", {})
        ivs = (ps.get("armsInterventionsModule") or {}).get("interventions") or []
        meshes = [m.get("term", "") for m in
                  ((s.get("derivedSection") or {}).get("interventionBrowseModule") or {}).get("meshes") or []
                  if m.get("term") and m.get("term", "").casefold() not in GENERIC_MESH]
        mesh_keys = {_clean(m): m for m in meshes}
        for iv in ivs:
            name = iv.get("name")
            if not name:
                continue
            key = _key(name)
            uf.find(key)
            spelling[(key, name.strip())] += 1
            specific = not (COMBO.search(_clean(name)) or CLASS_WORDS.search(_clean(name))
                            or GENERIC_ALIASES.match(_clean(name)))
            aliases = [a for a in _split_aliases(iv.get("otherNames")) if _usable_alias(a)] if specific else []
            aliases = aliases[:6] + [m for m in PAREN.findall(name) if _usable_alias(m) and _clean(m) != key]
            alias_keys = {_clean(a) for a in aliases}
            # anchor to MeSH: a direct match on the name wins; aliases only anchor when the name matched nothing
            # (live record: "Pembrolizumab 50 MG Injection [Keytruda]" lists otherNames [Carboplatin, Pemetrexed])
            direct, via_alias = set(), set()
            for mk, term in mesh_keys.items():
                if not mk:
                    continue
                contained = specific and re.search(rf"\b{re.escape(mk)}\b", key)       # "pembrolizumab 50 mg inj"
                salt = specific and mk.startswith(key + " ") and len(key) >= 6            # "bendamustine hydrochloride"
                if mk == key or contained or salt:
                    direct.add(term)
                elif mk in alias_keys:
                    via_alias.add(term)
            anchors = direct or (via_alias if len(via_alias) == 1 else set())
            uf.mesh[uf.find(key)] |= anchors
            pending.append((key, aliases, set(meshes), mesh_keys))

    # pass 2: link aliases. Refused when it would join two different MeSH anchors, and an alias that already
    # belongs to an anchored cluster can only be linked from a trial whose own MeSH terms include that anchor
    # whenever that trial has MeSH terms (stops a sponsor typo like otherNames=[Carboplatin] on a pembrolizumab
    # arm from merging the two).
    for key, aliases, trial_mesh, trial_mesh_keys in pending:
        for a in aliases:
            ak = _clean(a)
            if not ak or ak == key:
                continue
            own = uf.mesh.get(uf.find(key), set())
            if ak in trial_mesh_keys and own and trial_mesh_keys[ak] not in own:
                continue  # the alias is itself a different MeSH substance in this very trial
            target_mesh = uf.mesh.get(uf.find(ak), set()) if ak in uf.parent else set()
            if target_mesh and trial_mesh and not (target_mesh & trial_mesh):
                continue
            uf.find(ak)
            spelling[(ak, a.strip())] += 0  # register spelling without inflating counts
            uf.union(key, ak)

    # choose a label per cluster
    members: dict[str, set[str]] = defaultdict(set)
    for k in list(uf.parent):
        members[uf.find(k)].add(k)
    best_spelling: dict[str, str] = {}
    for (k, orig), n in spelling.most_common():
        best_spelling.setdefault(k, orig)
    label_of: dict[str, tuple[str, str]] = {}
    merges: dict[str, list[str]] = {}
    for root, keys in members.items():
        mesh = sorted(uf.mesh.get(root, set()))
        if mesh:
            label = mesh[0][0].upper() + mesh[0][1:]
        else:
            counts = Counter({k: sum(n for (kk, _), n in spelling.items() if kk == k) for k in keys})
            label = best_spelling.get(counts.most_common(1)[0][0], root)
        canon = re.sub(r"\s+", " ", label).casefold()
        for k in keys:
            label_of[k] = (canon, label)
        variants = sorted({best_spelling.get(k, k) for k in keys} - {label}, key=str.lower)
        if variants:
            merges[label] = variants

    for s in studies:
        ivs = ((s.get("protocolSection", {}).get("armsInterventionsModule") or {}).get("interventions") or [])
        norm: dict[int, tuple[str, str]] = {}
        for i, iv in enumerate(ivs):
            name = iv.get("name")
            if name:
                key = _key(name)
                norm[i] = label_of.get(key, (key, name.strip()))
        s.setdefault("_norm", {})["interventions"] = norm
    return merges


def canonical(study: dict[str, Any], index: int) -> Optional[tuple[str, str]]:
    return ((study.get("_norm") or {}).get("interventions") or {}).get(index)


# --------------------------------------------------------------------------- #
# conditions
# --------------------------------------------------------------------------- #
# MeSH lists on live records are noisy: oncology trials routinely carry terms like
# "Parkinson Disease 4, Autosomal Dominant Lewy Body" or "Vaccinia". So a MeSH term is only used when it
# *explains* one of the sponsor's own condition strings: every content token of the MeSH term must appear
# in that condition (after mapping cancer/tumor/carcinoma/neoplasm to one token). The most specific
# explaining term wins; unexplained conditions keep their free text.
_SYN = {"cancer": "neoplasm", "cancers": "neoplasm", "tumor": "neoplasm", "tumors": "neoplasm",
        "tumour": "neoplasm", "tumours": "neoplasm", "neoplasms": "neoplasm", "carcinoma": "neoplasm",
        "carcinomas": "neoplasm", "malignancy": "neoplasm", "malignancies": "neoplasm", "nsclc": "nsclc"}
_DROP = {"of", "the", "and", "disease", "diseases", "syndrome", "disorder", "disorders", "neoplasm", "type",
         "stage", "advanced", "metastatic", "recurrent", "primary", "adult", "in", "with", "s"}
_ABBREV = {"nsclc": ["non", "small", "cell", "lung", "neoplasm"], "sclc": ["small", "cell", "lung", "neoplasm"],
           "tnbc": ["triple", "negative", "breast", "neoplasm"], "t2d": ["2", "diabetes", "mellitus"],
           "t2dm": ["2", "diabetes", "mellitus"]}


def _ctoks(text: str) -> list[str]:
    out = []
    for t in re.findall(r"[a-z0-9]+", text.casefold()):
        out += _ABBREV.get(t, [_SYN.get(t, t)])
    return out


def _content(text: str) -> set[str]:
    return {t for t in _ctoks(text) if t not in _DROP}


def annotate_conditions(studies: list[dict[str, Any]]) -> dict[str, list[str]]:
    merged: dict[str, set[str]] = defaultdict(set)
    for s in studies:
        conds = ((s.get("protocolSection", {}).get("conditionsModule") or {}).get("conditions")) or []
        meshes = [m.get("term") for m in
                  ((s.get("derivedSection") or {}).get("conditionBrowseModule") or {}).get("meshes") or []
                  if m.get("term")]
        mesh_tok = [(t, _content(t), len(_ctoks(t))) for t in meshes]
        norm: dict[int, tuple[str, str]] = {}
        for i, c in enumerate(conds):
            ct = set(_ctoks(c))
            best = None
            for term, content, size in mesh_tok:
                if content and content <= ct and (best is None or (len(content), size) > (len(best[1]), best[2])):
                    best = (term, content, size)
            if best:
                label = best[0]
                norm[i] = (re.sub(r"\s+", " ", label).casefold(), label)
                if label.casefold() != c.strip().casefold():
                    merged[label].add(c.strip())
        s.setdefault("_norm", {})["conditions"] = norm
    return {k: sorted(v, key=str.lower) for k, v in merged.items()}


def canonical_condition(study: dict[str, Any], index: int) -> Optional[tuple[str, str]]:
    return ((study.get("_norm") or {}).get("conditions") or {}).get(index)
