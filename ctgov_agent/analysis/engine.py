"""Deterministic aggregation: studies -> visualization rows with citations.

One function per analysis kind. Each returns a `Built` (type, encoding, data,
plus stats/warnings). No I/O and no LLM here, so it is fully unit-testable.
"""
from __future__ import annotations

import math
import statistics
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations
from typing import Any, Optional

from ..ctgov.client import STUDY_URL
from ..ctgov.extract import (
    CATEGORICAL, LABELS, NUMERIC, PHASE_LABELS, PHASE_ORDER, STATUS_ORDER, UNITS, Fact, nct_id,
)
from ..schemas import Channel, Dimension, VizType

Studies = list[dict[str, Any]]
MAX_EDGES = 150
MAX_SERIES = 8


@dataclass
class Cohort:
    label: str
    studies: Studies


@dataclass
class Built:
    type: VizType
    encoding: dict[str, Channel]
    data: Any
    statistics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    sort: Optional[str] = None
    time_granularity: Optional[str] = None
    units: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# buckets: the unit of aggregation and of citation
# --------------------------------------------------------------------------- #
class Bucket:
    __slots__ = ("key", "label", "evidence")

    def __init__(self, key: str, label: str) -> None:
        self.key, self.label = key, label
        self.evidence: "OrderedDict[str, list[Fact]]" = OrderedDict()  # nct_id -> facts

    def add(self, nct: str, *facts: Fact) -> None:
        self.evidence.setdefault(nct, []).extend(facts)

    @property
    def count(self) -> int:
        return len(self.evidence)

    def citations(self, k: int) -> list[dict[str, str]]:
        """Detailed citations for the first k contributing trials (all facts for each trial;
        an edge carries one fact per endpoint)."""
        out = []
        for i, (nct, facts) in enumerate(self.evidence.items()):
            if i >= k:
                break
            for f in facts:
                out.append({"nct_id": nct, "field": f.path, "excerpt": f.excerpt,
                            "url": STUDY_URL.format(nct_id=nct)})
        return out

    def provenance(self, k: int) -> dict[str, Any]:
        return {"nct_ids": list(self.evidence.keys()), "citations": self.citations(k)}


def bucketize(studies: Studies, dim: Dimension) -> dict[str, Bucket]:
    ex = CATEGORICAL[dim]
    buckets: dict[str, Bucket] = {}
    for s in studies:
        nct = nct_id(s)
        for f in ex(s):
            b = buckets.get(f.key)
            if b is None:
                b = buckets[f.key] = Bucket(f.key, f.label)
            b.add(nct, f)
    return buckets


def order_buckets(buckets: list[Bucket], dim: Dimension) -> tuple[list[Bucket], str]:
    """Natural order for ordinal dimensions, otherwise by count desc (ties by label)."""
    if dim == Dimension.phase:
        idx = {lab: i for i, lab in enumerate(PHASE_ORDER)}
        return sorted(buckets, key=lambda b: idx.get(b.label, 99)), "phase_order"
    if dim == Dimension.overall_status:
        idx = {lab: i for i, lab in enumerate(STATUS_ORDER)}
        return sorted(buckets, key=lambda b: idx.get(b.key, 99)), "status_order"
    if dim == Dimension.start_year:
        return sorted(buckets, key=lambda b: int(b.key)), "chronological"
    return sorted(buckets, key=lambda b: (-b.count, b.label.lower())), "count_desc"


def top_buckets(buckets: dict[str, Bucket], dim: Dimension, top_n: int) -> tuple[list[Bucket], int]:
    ranked = sorted(buckets.values(), key=lambda b: (-b.count, b.label.lower()))
    if dim in (Dimension.phase, Dimension.overall_status, Dimension.study_type, Dimension.sponsor_class):
        return ranked, 0  # small closed vocabularies: never truncate
    return ranked[:top_n], max(0, len(ranked) - top_n)


def _cat_type(dim: Dimension) -> str:
    return "ordinal" if dim in (Dimension.phase, Dimension.overall_status, Dimension.start_year) else "nominal"


# --------------------------------------------------------------------------- #
# analyses
# --------------------------------------------------------------------------- #
def distribution(cohort: Cohort, dim: Dimension, top_n: int, k: int) -> Built:
    buckets = bucketize(cohort.studies, dim)
    kept, omitted = top_buckets(buckets, dim, top_n)
    kept, sort = order_buckets(kept, dim)
    n = len(cohort.studies)
    f = dim.value
    rows = [{f: b.label, "trial_count": b.count,
             "share_pct": round(100 * b.count / n, 1) if n else 0.0, **b.provenance(k)} for b in kept]
    built = Built(
        type=VizType.bar_chart,
        encoding={
            "x": Channel(field=f, type=_cat_type(dim), title=LABELS[dim],
                         sort=[r[f] for r in rows] if sort != "count_desc" else "descending"),
            "y": Channel(field="trial_count", type="quantitative", title="Number of trials", unit="trials"),
            "tooltip": Channel(field="share_pct", type="quantitative", title="% of analyzed trials", unit="%"),
        },
        data=rows, sort=sort, units={"trial_count": "trials", "share_pct": "%"},
        statistics={"trials_analyzed": n, "categories_total": len(buckets), "categories_shown": len(rows)},
    )
    if omitted:
        built.warnings.append(f"{omitted} less frequent {LABELS[dim].lower()} categories omitted (top_n={top_n}).")
    if dim in (Dimension.country, Dimension.condition, Dimension.intervention, Dimension.drug, Dimension.intervention_type):
        built.assumptions.append(f"A trial can list several {LABELS[dim].lower()} values, so counts can sum to more "
                                 "than the number of trials; each trial counts at most once per category.")
    no_value = sum(1 for s in cohort.studies if not CATEGORICAL[dim](s))
    if no_value:
        built.statistics["trials_without_value"] = no_value
    return built


def time_trend(cohorts: list[Cohort], top_n: int, k: int, year_range: tuple[Optional[int], Optional[int]],
               series_dim: Optional[Dimension] = None) -> Built:
    """Trials per start year. Multi-series when several cohorts or a series_dimension is given."""
    series: list[tuple[str, dict[str, Bucket]]] = []
    if series_dim is not None:
        (cohort,) = cohorts
        # split cohort studies by the series dimension, then bucket each split by year
        split = bucketize(cohort.studies, series_dim)
        kept, _ = top_buckets(split, series_dim, min(top_n, MAX_SERIES))
        kept, _ = order_buckets(kept, series_dim)
        by_id = {nct_id(s): s for s in cohort.studies}
        for b in kept:
            series.append((b.label, bucketize([by_id[n] for n in b.evidence], Dimension.start_year)))
    else:
        for c in cohorts:
            series.append((c.label, bucketize(c.studies, Dimension.start_year)))

    years = [int(y) for _, bs in series for y in bs]
    built_warn: list[str] = []
    if not years:
        lo = hi = None
    else:
        lo = year_range[0] or min(years)
        hi = year_range[1] or max(years)
    rows: list[dict[str, Any]] = []
    multi = len(series) > 1 or series_dim is not None
    if lo is not None:
        for name, bs in series:
            for y in range(lo, hi + 1):  # fill gaps with explicit zeros so lines don't interpolate
                b = bs.get(str(y))
                row = {"start_year": y, "trial_count": b.count if b else 0,
                       **(b.provenance(k) if b else {"nct_ids": [], "citations": []})}
                if multi:
                    row = {"series": name, **row}
                rows.append(row)
    this_year = date.today().year
    if hi is not None and hi >= this_year:
        built_warn.append(f"{this_year} is a partial year; later years also include trials with anticipated start dates.")
    enc = {
        "x": Channel(field="start_year", type="temporal", title="Start year", sort="ascending"),
        "y": Channel(field="trial_count", type="quantitative", title="Trials started", unit="trials"),
    }
    if multi:
        title = LABELS[series_dim] if series_dim else "Cohort"
        enc["series"] = Channel(field="series", type="nominal", title=title, sort=[n for n, _ in series])
    total_with_year = sum(b.count for _, bs in series for b in bs.values())
    return Built(type=VizType.time_series, encoding=enc, data=rows, sort="chronological",
                 time_granularity="year", units={"trial_count": "trials"}, warnings=built_warn,
                 statistics={"year_min": lo, "year_max": hi, "trial_year_points": total_with_year},
                 assumptions=["Year = study start date (actual or anticipated) from statusModule.startDateStruct."])


def comparison(cohorts: list[Cohort], dim: Dimension, top_n: int, k: int) -> Built:
    per = [(c, bucketize(c.studies, dim)) for c in cohorts]
    merged: dict[str, Bucket] = {}
    for _, bs in per:
        for key, b in bs.items():
            m = merged.setdefault(key, Bucket(key, b.label))
            for nct, facts in b.evidence.items():
                m.add(nct, *facts)
    kept, omitted = top_buckets(merged, dim, top_n)
    kept, sort = order_buckets(kept, dim)
    rows = []
    for b in kept:
        for c, bs in per:
            cb = bs.get(b.key)
            n = len(c.studies)
            cnt = cb.count if cb else 0
            rows.append({"category": b.label, "cohort": c.label, "trial_count": cnt,
                         "share_pct": round(100 * cnt / n, 1) if n else 0.0,
                         **(cb.provenance(k) if cb else {"nct_ids": [], "citations": []})})
    built = Built(
        type=VizType.grouped_bar_chart,
        encoding={
            "x": Channel(field="category", type=_cat_type(dim), title=LABELS[dim], sort=[b.label for b in kept]),
            "y": Channel(field="trial_count", type="quantitative", title="Number of trials", unit="trials"),
            "series": Channel(field="cohort", type="nominal", title="Cohort", sort=[c.label for c in cohorts]),
            "tooltip": Channel(field="share_pct", type="quantitative", title="% of cohort's trials", unit="%"),
        },
        data=rows, sort=sort, units={"trial_count": "trials", "share_pct": "% of cohort"},
        statistics={"cohort_sizes": {c.label: len(c.studies) for c in cohorts}},
    )
    sizes = [len(c.studies) for c in cohorts if c.studies]
    if sizes and max(sizes) > 2 * min(sizes):
        built.warnings.append("Cohort sizes differ by more than 2x; compare share_pct rather than raw counts.")
    if omitted:
        built.warnings.append(f"{omitted} categories omitted (top_n={top_n}).")
    overlap = set.intersection(*[{nct_id(s) for s in c.studies} for c in cohorts]) if cohorts else set()
    if overlap:
        built.statistics["trials_in_multiple_cohorts"] = len(overlap)
        built.assumptions.append(f"{len(overlap)} trials match more than one cohort (e.g. combination studies) and "
                                 "are counted in each.")
    return built


def _nice_step(span: float, target_bins: int) -> float:
    raw = span / max(target_bins, 1)
    if raw <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def histogram(cohort: Cohort, dim: Dimension, k: int, target_bins: int = 12) -> Built:
    ex = NUMERIC[dim]
    vals: list[tuple[float, str, list[Fact]]] = []
    for s in cohort.studies:
        r = ex(s)
        if r:
            vals.append((r[0], nct_id(s), r[1]))
    unit = UNITS.get(dim, "")
    if not vals:
        return Built(type=VizType.histogram, encoding=_hist_enc(dim, unit), data=[],
                     warnings=[f"No trials report {LABELS[dim].lower()}."])
    xs = sorted(v for v, _, _ in vals)
    p95 = xs[min(len(xs) - 1, int(0.95 * len(xs)))]
    step = _nice_step(p95 if p95 > 0 else xs[-1] or 1, target_bins)
    cap = step * math.ceil((p95 or step) / step)
    buckets: dict[int, Bucket] = {}
    nbins = int(cap / step)
    for v, nct, facts in vals:
        i = min(int(v // step), nbins)  # last index = overflow bin
        b = buckets.get(i)
        if b is None:
            b = buckets[i] = Bucket(str(i), "")
        b.add(nct, *facts)
    rows = []
    for i in range(nbins + 1):
        lo = i * step
        overflow = i == nbins
        if overflow and i not in buckets:
            continue
        b = buckets.get(i)
        hi = None if overflow else lo + step
        label = f">= {_fmt(lo)}" if overflow else f"{_fmt(lo)}-{_fmt(hi)}"
        rows.append({"bin_start": lo, "bin_end": hi, "bin_label": label, "trial_count": b.count if b else 0,
                     **(b.provenance(k) if b else {"nct_ids": [], "citations": []})})
    return Built(
        type=VizType.histogram, encoding=_hist_enc(dim, unit), data=rows, sort="bin_order",
        units={"bin_start": unit, "bin_end": unit, "trial_count": "trials"},
        statistics={"n": len(xs), "missing": len(cohort.studies) - len(xs), "min": xs[0], "max": xs[-1],
                    "median": statistics.median(xs), "mean": round(statistics.fmean(xs), 1),
                    "bin_width": step, "overflow_threshold": cap},
        assumptions=[f"Bins are [bin_start, bin_end). Values >= {_fmt(cap)} (about the 95th percentile) are grouped "
                     "into one overflow bin (bin_end = null) to keep the long tail readable."]
        + (["Duration = start date to primary completion date (falls back to completion date)."]
           if dim == Dimension.duration_months else []),
    )


def _hist_enc(dim: Dimension, unit: str) -> dict[str, Channel]:
    return {
        "x": Channel(field="bin_start", type="quantitative", title=f"{LABELS[dim]} ({unit})", unit=unit),
        "x2": Channel(field="bin_end", type="quantitative", title="Bin end", unit=unit),
        "y": Channel(field="trial_count", type="quantitative", title="Number of trials", unit="trials"),
        "label": Channel(field="bin_label", type="ordinal", title="Bin"),
    }


def _fmt(x: Optional[float]) -> str:
    return "" if x is None else (str(int(x)) if float(x).is_integer() else f"{x:g}")


def scatter(cohort: Cohort, x_dim: Dimension, y_dim: Dimension, k: int) -> Built:
    rows, missing = [], 0
    for s in cohort.studies:
        xr, yr = NUMERIC[x_dim](s), NUMERIC[y_dim](s)
        if not xr or not yr:
            missing += 1
            continue
        nct = nct_id(s)
        phase = CATEGORICAL[Dimension.phase](s)[0]
        facts = xr[1] + [f for f in yr[1] if f not in xr[1]]
        rows.append({"nct_id": nct, x_dim.value: xr[0], y_dim.value: yr[0], "phase": phase.label,
                     "title": (s.get("protocolSection", {}).get("identificationModule", {}).get("briefTitle") or "")[:160],
                     "citations": [{"nct_id": nct, "field": f.path, "excerpt": f.excerpt,
                                    "url": STUDY_URL.format(nct_id=nct)} for f in facts][: max(k, 1) * 2]})

    def ch(d: Dimension) -> Channel:
        return Channel(field=d.value, type="temporal" if d == Dimension.start_year else "quantitative",
                       title=LABELS[d], unit=UNITS.get(d), scale="log" if d == Dimension.enrollment else "linear")

    stats: dict[str, Any] = {"points": len(rows), "trials_missing_values": missing}
    if len(rows) >= 3:
        xs = [r[x_dim.value] for r in rows]
        ys = [r[y_dim.value] for r in rows]
        try:
            stats["spearman_rho"] = round(_spearman(xs, ys), 3)
        except ZeroDivisionError:
            pass
    return Built(
        type=VizType.scatter_plot,
        encoding={"x": ch(x_dim), "y": ch(y_dim),
                  "color": Channel(field="phase", type="ordinal", title="Phase", sort=PHASE_ORDER),
                  "tooltip": Channel(field="title", type="nominal", title="Brief title"),
                  "id": Channel(field="nct_id", type="nominal", title="NCT ID")},
        data=rows, units={d.value: UNITS[d] for d in (x_dim, y_dim) if d in UNITS}, statistics=stats,
        assumptions=["One point per trial; trials missing either value are excluded."],
    )


def _rank(v: list[float]) -> list[float]:
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for t in range(i, j + 1):
            r[order[t]] = (i + j) / 2 + 1
        i = j + 1
    return r


def _spearman(x: list[float], y: list[float]) -> float:
    rx, ry = _rank(x), _rank(y)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den


def network(cohort: Cohort, dim_a: Dimension, dim_b: Optional[Dimension], top_n: int, k: int,
            exclude_keys: Optional[dict[Dimension, set[str]]] = None) -> Built:
    """Bipartite (dim_a <-> dim_b) or co-occurrence (dim_a <-> dim_a) network.

    Node weight = trials mentioning the entity; edge weight = trials mentioning both.
    `exclude_keys` drops the query's own anchor entity (e.g. the condition the user filtered
    on), which would otherwise connect to everything and say nothing.
    """
    exclude_keys = exclude_keys or {}
    bipartite = dim_b is not None
    dims = [dim_a, dim_b] if bipartite else [dim_a]

    node_b: dict[str, Bucket] = {}
    per_trial: list[tuple[str, list[tuple[str, Fact]]]] = []
    for s in cohort.studies:
        nct = nct_id(s)
        ents: list[tuple[str, Fact]] = []
        for d in dims:
            for f in CATEGORICAL[d](s):
                if f.key in exclude_keys.get(d, set()):
                    continue
                nid = f"{d.value}:{f.key}"
                b = node_b.get(nid)
                if b is None:
                    b = node_b[nid] = Bucket(nid, f.label)
                b.add(nct, f)
                ents.append((nid, f))
        per_trial.append((nct, ents))

    # keep top_n nodes per node type
    keep: set[str] = set()
    for d in dims:
        cands = sorted((b for nid, b in node_b.items() if nid.startswith(d.value + ":")),
                       key=lambda b: (-b.count, b.label.lower()))
        keep |= {b.key for b in cands[:top_n]}

    edges: dict[tuple[str, str], Bucket] = {}
    for nct, ents in per_trial:
        ents = [(n, f) for n, f in ents if n in keep]
        for (n1, f1), (n2, f2) in combinations(ents, 2):
            if n1 == n2:
                continue
            if bipartite and n1.split(":", 1)[0] == n2.split(":", 1)[0]:
                continue  # bipartite: only cross-type edges
            a, b = sorted([(n1, f1), (n2, f2)], key=lambda t: t[0])
            e = edges.get((a[0], b[0]))
            if e is None:
                e = edges[(a[0], b[0])] = Bucket(f"{a[0]}|{b[0]}", "")
            e.add(nct, a[1], b[1])

    ranked = sorted(edges.items(), key=lambda kv: (-kv[1].count, kv[0]))
    warnings = []
    if len(ranked) > MAX_EDGES:
        warnings.append(f"Showing the {MAX_EDGES} strongest of {len(ranked)} edges.")
        ranked = ranked[:MAX_EDGES]
    connected = {n for (a, b), _ in ranked for n in (a, b)}
    nodes = [{"id": nid, "label": node_b[nid].label, "group": nid.split(":", 1)[0],
              "trial_count": node_b[nid].count, **node_b[nid].provenance(k)}
             for nid in sorted(connected, key=lambda n: (-node_b[n].count, n))]
    edge_rows = [{"source": a, "target": b, "weight": e.count, **e.provenance(k)} for (a, b), e in ranked]
    if not edge_rows:
        warnings.append("No co-occurring entities found among the top nodes; try a broader query.")
    groups = [d.value for d in dims]
    return Built(
        type=VizType.network_graph,
        encoding={
            "node_id": Channel(field="id", type="nominal", title="Node id"),
            "node_label": Channel(field="label", type="nominal", title="Entity"),
            "node_group": Channel(field="group", type="nominal", title="Entity type", sort=groups),
            "node_size": Channel(field="trial_count", type="quantitative", title="Trials", unit="trials"),
            "edge_source": Channel(field="source", type="nominal", title="Source node"),
            "edge_target": Channel(field="target", type="nominal", title="Target node"),
            "edge_weight": Channel(field="weight", type="quantitative", title="Shared trials", unit="trials"),
        },
        data={"nodes": nodes, "edges": edge_rows},
        units={"trial_count": "trials", "weight": "shared trials"}, warnings=warnings,
        statistics={"nodes": len(nodes), "edges": len(edge_rows), "layout_hint": "bipartite" if bipartite else "force",
                    "node_types": groups},
        assumptions=["Edge weight = number of trials that list both entities; node size = trials listing the entity."]
        + (["Placebo, saline and standard-of-care arms are excluded from drug nodes."] if Dimension.drug in dims else []),
    )


def phase_label(code: str) -> str:
    return PHASE_LABELS.get(code, code)


_COUNTRIES: Optional[dict[str, dict[str, Any]]] = None


def country_codes(name: str) -> Optional[dict[str, Any]]:
    global _COUNTRIES
    if _COUNTRIES is None:
        import json
        from pathlib import Path
        _COUNTRIES = json.loads((Path(__file__).resolve().parent.parent / "data" / "countries.json").read_text())
    # live records spell some countries "Turkey (Türkiye)" or with a typographic apostrophe ("Côte d’Ivoire")
    name = name.replace("\u2019", "'").replace("\u2018", "'").strip()
    hit = _COUNTRIES.get(name.casefold())
    if hit is None and "(" in name:
        outer, inner = name.split("(", 1)
        hit = _COUNTRIES.get(outer.strip().casefold()) or _COUNTRIES.get(inner.rstrip(") ").casefold())
    return hit


def geo_distribution(cohort: Cohort, k: int) -> Built:
    """Trials per country as a choropleth. Every country is returned (the map has room for all of them)."""
    buckets = bucketize(cohort.studies, Dimension.country)
    ranked = sorted(buckets.values(), key=lambda b: (-b.count, b.label.lower()))
    n = len(cohort.studies)
    rows, unmapped = [], []
    for b in ranked:
        codes = country_codes(b.label)
        if not codes or not codes.get("iso_numeric"):
            unmapped.append(b.label)
        rows.append({"country": b.label, "iso3": codes["iso3"] if codes else None,
                     "iso_numeric": codes["iso_numeric"] if codes else None, "trial_count": b.count,
                     "share_pct": round(100 * b.count / n, 1) if n else 0.0, **b.provenance(k)})
    built = Built(
        type=VizType.choropleth_map,
        encoding={
            "geo": Channel(field="iso_numeric", type="nominal", title="Country (ISO 3166 numeric)"),
            "color": Channel(field="trial_count", type="quantitative", title="Number of trials", unit="trials"),
            "label": Channel(field="country", type="nominal", title="Country"),
            "x": Channel(field="country", type="nominal", title="Country", sort="descending"),
            "y": Channel(field="trial_count", type="quantitative", title="Number of trials", unit="trials"),
            "tooltip": Channel(field="share_pct", type="quantitative", title="% of analyzed trials", unit="%"),
        },
        data=rows, sort="count_desc", units={"trial_count": "trials", "share_pct": "%"},
        statistics={"trials_analyzed": n, "countries": len(rows), "trials_without_location": sum(
            1 for s in cohort.studies if not CATEGORICAL[Dimension.country](s)), "basemap": "world-atlas@2 countries-110m"},
        assumptions=["A trial counts once per country it has at least one site in, so counts can sum to more than the "
                     "number of trials.", "Color scale is sequential (one hue, light to dark = fewer to more trials)."],
    )
    if unmapped:
        built.warnings.append(f"{len(unmapped)} location names could not be mapped to ISO codes and are listed but not "
                              f"drawn on the map: {', '.join(unmapped[:5])}.")
    return built
