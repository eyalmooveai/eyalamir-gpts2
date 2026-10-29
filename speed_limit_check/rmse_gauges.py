"""The three RMSE "pressure gauges" (OSM / HERE / Model) at the top of the Quality page, and the cache behind them.

Each gauge shows one source's INFERRED OVERALL RMSE (mph) against the real posted signs - never the raw sample RMSE. The precision samples
are drawn only from segments where the sources disagree (and weighted/balanced by the sampling method), so their plain RMSE is biased by how
the sample was selected. The inferred figure corrects for that: a run's RMSE on its disagreeing sample is scaled by the share of the whole
domain that disagrees (sqrt(disagree% x MSE|disagreement); segments where OSM, HERE and Model all agree count as zero error - see
precision_sample.infer_overall_metrics). Runs without the domain split saved can't be inferred and are skipped (and reported).

The toggle only chooses how several runs' inferred figures are COMBINED:
  precision_inferred         (default) all saved runs, weighted by how many observations each had.
  precision_inferred_latest  only the newest run of each domain (table + state + functional class), weighted by observations.
  precision_inferred_equal   every run counts the same, whatever its size.
Free: reads the stored run files only. Results are cached per method in one JSON file (local disk, mirrored to GCS like every other cache
here) so the page shows them instantly after a restart; "Update" recomputes one method. Nothing here calls Street View, Vision, Gemini or BigQuery.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

METHODS = {
    "precision_inferred": "Inferred overall - all runs, weighted by observations (default)",
    "precision_inferred_latest": "Inferred overall - newest run per state/class only",
    "precision_inferred_equal": "Inferred overall - every run counts equally",
}
DEFAULT_METHOD = "precision_inferred"
SOURCES = (("osm", "OSM", "speed_limit_osm_mph"), ("here", "HERE", "speed_limit_here_mph"), ("model", "Model", None))

# Gauge scale (mph of RMSE). Green near 0, yellow then red as the error grows; 5 mph is also the app's "within tolerance" distance.
GAUGE_MAX_MPH = 20.0
ZONES = [
    {"name": "green", "from": 0.0, "to": 5.0},
    {"name": "yellow", "from": 5.0, "to": 10.0},
    {"name": "red", "from": 10.0, "to": GAUGE_MAX_MPH},
]


# Coverage clocks: share (%) of the data a source has a value for. Higher is better, so the colours run the other way round
# (red = low coverage). A source below COVERAGE_MIN_PCT is flagged on the page (its coverage clock goes red, its RMSE clock is greyed).
COVERAGE_MIN_PCT = 80.0
COVERAGE_ZONES = [
    {"name": "red", "from": 0.0, "to": COVERAGE_MIN_PCT},
    {"name": "yellow", "from": COVERAGE_MIN_PCT, "to": 95.0},
    {"name": "green", "from": 95.0, "to": 100.0},
]


def _empty_sources() -> dict:
    return {key: {"label": label, "rmse_mph": None, "n": 0, "coverage": None, "have": None, "of": None} for key, label, _ in SOURCES}


def _set_coverage(src: dict, have: Optional[float], of: Optional[float]) -> None:
    if of:
        src["have"], src["of"], src["coverage"] = int(have or 0), int(of), (have or 0) / of


def domain_key(payload: dict) -> str:
    cfg = payload.get("config") or {}
    return f"{cfg.get('table')}|{cfg.get('state')}|{cfg.get('functional_class_lt')}"


def domains_missing_counts(payloads: Iterable[dict], known: dict) -> dict[str, dict]:
    """{domain key: run config} for every (table, state, functional class) whose newest run saved a domain split WITHOUT the per-source
    counts (a run from before coverage existed) and which `known` (counts already looked up earlier) doesn't cover."""
    out: dict[str, dict] = {}
    for p in payloads:
        pop = p.get("population")
        cfg = p.get("config") or {}
        if not (p.get("observations") and pop) or pop.get("total_count") or not cfg.get("table"):
            continue
        k = domain_key(p)
        if k not in known:
            out.setdefault(k, cfg)
    return out


def fetch_domain_counts(missing: dict[str, dict], *, max_cost_usd: float, price_per_tib: float) -> tuple[dict, dict]:
    """Looks up total/per-source counts for each missing domain with the same one read-only COUNT query the precision run itself uses
    (precision_sample.population_agreement_counts, ~1 GB each). A free dry run prices the lot first; over `max_cost_usd` it raises so the
    caller can say so instead of spending. Returns ({key: counts}, {"gb": ..., "cost_usd": ...})."""
    from google.cloud import bigquery
    from precision_sample import _population_sql, population_agreement_counts
    clients: dict[str, "bigquery.Client"] = {}
    total_bytes = 0
    for cfg in missing.values():
        c = clients.setdefault(cfg["project"], bigquery.Client(project=cfg["project"]))
        job = c.query(_population_sql(cfg["project"], cfg["dataset"], cfg["table"]), job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("fc_lt", "INT64", cfg["functional_class_lt"]),
                              bigquery.ScalarQueryParameter("state", "STRING", cfg["state"].upper())], dry_run=True, use_query_cache=False))
        total_bytes += job.total_bytes_processed or 0
    estimate = {"gb": total_bytes / 1024 ** 3, "cost_usd": total_bytes / 1024 ** 4 * price_per_tib}
    if estimate["cost_usd"] > max_cost_usd:
        raise RuntimeError(f"Looking up the missing coverage counts would read about {estimate['gb']:.1f} GB (about ${estimate['cost_usd']:.2f}) - "
                           "open those runs under Previous runs and use \"Calculate missing values\" instead.")
    out = {}
    for k, cfg in missing.items():
        out[k] = population_agreement_counts(cfg["project"], cfg["dataset"], cfg["table"], state=cfg["state"],
                                             functional_class_lt=cfg["functional_class_lt"], client=clients[cfg["project"]])
    return out, estimate


def compute_precision(payloads: Iterable[dict], method: str, domain_counts: Optional[dict] = None) -> dict:
    """Combine finished precision-sample payloads (newest first) into inferred-overall RMSE per source. Each run's stats come from its own
    observations (recomputed here so older runs without mse still count). Returns {"sources": {...}, "runs": used, "skipped_runs": n,
    "observations": m, "coverage_basis": "domain"}."""
    from precision_sample import _source_stats
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}")
    contributions: list[tuple[tuple, dict]] = []   # (domain key, {source: (n_compared, inferred MSE)}) per usable run, newest first
    domains: dict[tuple, dict] = {}                # newest population per (table, state, functional class) - for coverage, once per domain
    skipped = 0
    for payload in payloads:
        obs = payload.get("observations") or []
        pop = payload.get("population")
        if not obs:
            continue
        cfg = payload.get("config") or {}
        key = (cfg.get("table"), cfg.get("state"), cfg.get("functional_class_lt"))
        dom = pop if pop and pop.get("total_count") else (domain_counts or {}).get(domain_key(payload))   # counts looked up for older runs
        if dom and dom.get("total_count"):
            domains.setdefault(key, dom)
        if not pop or not (pop.get("agree_count", 0) + pop.get("disagree_count", 0)):
            skipped += 1   # no domain split saved: can't scale to an overall figure
            continue
        d = pop["disagree_count"] / (pop["agree_count"] + pop["disagree_count"])
        tol = ((payload.get("stats") or {}).get("osm") or {}).get("within_tolerance_mph") or 5.0
        per_source = {}
        for src, _, column in SOURCES:
            st = _source_stats(obs, column, tol)
            if st["n_compared"] and st["mse_mph2"] is not None:
                per_source[src] = (st["n_compared"], d * st["mse_mph2"])
        contributions.append((key, per_source))

    if method == "precision_inferred_latest":
        seen, kept = set(), []
        for key, per_source in contributions:
            if key not in seen:
                seen.add(key)
                kept.append((key, per_source))
        contributions = kept
    equal = method == "precision_inferred_equal"

    sources = _empty_sources()
    for src, _, _ in SOURCES:
        w = tot = n_total = 0.0
        for _, per_source in contributions:
            if src in per_source:
                n, mse = per_source[src]
                weight = 1.0 if equal else n
                w += weight
                tot += weight * mse
                n_total += n
        if w:
            sources[src]["rmse_mph"] = math.sqrt(tot / w)
            sources[src]["n"] = int(n_total)
    total = sum(p["total_count"] for p in domains.values())
    for src, _, _ in SOURCES:
        _set_coverage(sources[src], sum(p.get(src + "_count", 0) for p in domains.values()), total)
    return {
        "sources": sources, "runs": len(contributions), "skipped_runs": skipped,
        "observations": max((s["n"] for s in sources.values()), default=0), "coverage_basis": "domain" if total else None,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_cache(path: Path) -> dict:
    """The saved gauges: {"methods": {method: entry}}. Pulls the GCS copy first when the local disk has none (fresh instance)."""
    try:
        from find_bad_speed_limit import gcs_cache_pull
        gcs_cache_pull(path)
    except Exception:  # noqa: BLE001 - a missing GCS config just means local only
        pass
    try:
        data = json.loads(path.read_text())
        if isinstance(data.get("methods"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"methods": {}}


def save_domain_counts(path: Path, counts: dict) -> None:
    """Remember counts looked up for older runs, so later Updates don't query BigQuery again."""
    data = load_cache(path)
    data.setdefault("domain_counts", {}).update(counts)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, default=str))
    tmp.replace(path)


def save_entry(path: Path, method: str, entry: dict) -> None:
    data = load_cache(path)
    data["methods"][method] = {**entry, "calculated_at": _now()}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, default=str))
    tmp.replace(path)
    try:
        from find_bad_speed_limit import gcs_cache_push
        gcs_cache_push(path)
    except Exception:  # noqa: BLE001
        pass


def payload_for_ui(cache: dict, method: str) -> dict:
    return {
        "method": method, "methods": METHODS, "default_method": DEFAULT_METHOD,
        "zones": ZONES, "max_mph": GAUGE_MAX_MPH, "entry": cache["methods"].get(method),
        "coverage_zones": COVERAGE_ZONES, "coverage_min_pct": COVERAGE_MIN_PCT,
    }
