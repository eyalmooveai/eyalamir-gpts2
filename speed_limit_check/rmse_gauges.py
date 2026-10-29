"""The three RMSE "pressure gauges" (OSM / HERE / Model) at the top of the Quality page, and the cache behind them.

Each gauge shows one source's RMSE (mph) against the real posted signs, aggregated over everything measured so far, by one
of three STATS METHODS (the page's toggle):

  precision_inferred  (default) - every saved precision-sample run's INFERRED OVERALL RMSE (the run's disagreement-sample MSE scaled by
                      its domain's disagree share, assuming agreeing segments are right - see precision_sample.infer_overall_metrics),
                      combined across runs weighted by how many observations each run had. Free: local/GCS files only.
  precision_pooled    - all saved precision-sample runs' observations pooled and the plain RMSE taken over them. This is the error ON THE
                      DISAGREEING SEGMENTS (what the samples draw from), so it reads worse than the overall figure. Free.
  analyzed            - every sign ever analyzed (calc_out.road_signs, latest check per segment, sampled + traversed + Evaluator /
                      Sign Checker), pooled RMSE - not a random sample. One read-only BigQuery query (road_signs_report), dry-run cost
                      first and the usual $10 confirmation.

Results are cached per method in one JSON file (local disk, mirrored to GCS like every other cache here) so the page can show them
instantly after a restart; "Update" recomputes one method and rewrites its entry. Nothing here ever calls Street View, Vision or Gemini.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

METHODS = {
    "precision_inferred": "Precision runs - inferred overall (default)",
    "precision_pooled": "Precision runs - pooled disagreement sample",
    "analyzed": "All analyzed signs (reads BigQuery)",
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


def _empty_sources() -> dict:
    return {key: {"label": label, "rmse_mph": None, "n": 0} for key, label, _ in SOURCES}


def compute_precision(payloads: Iterable[dict], method: str) -> dict:
    """Combine finished precision-sample payloads (as stored: stats come from their own observations, recomputed here so older
    runs without mse/recall still count). Returns {"sources": {...}, "runs": k, "observations": m}."""
    from precision_sample import _source_stats
    sums = {key: {"w": 0.0, "mse": 0.0} for key, _, _ in SOURCES}
    pooled: list[dict] = []
    runs = 0
    for payload in payloads:
        obs = payload.get("observations") or []
        if not obs:
            continue
        tol = ((payload.get("stats") or {}).get("osm") or {}).get("within_tolerance_mph") or 5.0
        pop = payload.get("population")
        if method == "precision_inferred":
            if not pop or not (pop.get("agree_count", 0) + pop.get("disagree_count", 0)):
                continue   # no domain split saved: can't scale to an overall figure
            d = pop["disagree_count"] / (pop["agree_count"] + pop["disagree_count"])
            for key, _, column in SOURCES:
                st = _source_stats(obs, column, tol)
                if st["n_compared"] and st["mse_mph2"] is not None:
                    sums[key]["w"] += st["n_compared"]
                    sums[key]["mse"] += st["n_compared"] * d * st["mse_mph2"]
        else:
            pooled.extend(obs)
        runs += 1
    sources = _empty_sources()
    if method == "precision_inferred":
        for key in sources:
            w = sums[key]["w"]
            if w:
                sources[key]["rmse_mph"] = math.sqrt(sums[key]["mse"] / w)
                sources[key]["n"] = int(w)
        n_obs = max((s["n"] for s in sources.values()), default=0)
    else:
        for key, _, column in SOURCES:
            st = _source_stats(pooled, column, 5.0)
            sources[key]["rmse_mph"], sources[key]["n"] = st["rmse_mph"], st["n_compared"]
        n_obs = len(pooled)
    return {"sources": sources, "runs": runs, "observations": n_obs}


def compute_analyzed(run_report: Callable[..., dict], *, confirmed: bool) -> dict:
    """RMSE over every analyzed sign via road_signs_report.run_report (already wrapped with project/table/state=US/origin=both).
    Returns the cost-confirmation request unchanged when BigQuery says it is over the threshold and `confirmed` is false."""
    report = run_report(confirmed=confirmed)
    if report.get("needs_confirmation"):
        return {"needs_confirmation": True, "estimate": report["estimate"]}
    stats = (report.get("summary") or {}).get("stats") or {}
    sources = _empty_sources()
    for key in sources:
        st = stats.get(key) or {}
        sources[key]["rmse_mph"], sources[key]["n"] = st.get("rmse_mph"), st.get("n_compared", 0)
    return {
        "sources": sources, "runs": None, "observations": (report.get("summary") or {}).get("observations", 0),
        "segments": (report.get("summary") or {}).get("segments_checked"),
        "gb_billed": report.get("gb_billed"), "cost_usd_billed": report.get("cost_usd_billed"),
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
    }
