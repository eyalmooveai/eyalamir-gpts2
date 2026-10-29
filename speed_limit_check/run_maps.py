"""Data behind the Quality page's two maps (no imagery/Gemini/Vision spend; the only BigQuery use is two tiny read-only lookups, cached):

1. SAMPLES MAP - every sign observation of one precision-sample run, or of all saved runs together, as a point coloured by which source
   (OSM / HERE / Model) was CLOSEST to the real posted limit (|value - sign| smallest among the sources that had a value; two or more equally
   close = "tie"). `sample_points()` builds the compact point list the page draws.

2. RMSE BY STATE - the INFERRED overall RMSE (never the raw sample RMSE - the samples are drawn only from disagreeing segments, so it is biased
   by the selection; see rmse_gauges.py) of one chosen source, per state. A run of a single state scales its sample MSE by that state's own
   disagree share. A nationwide (`US`) run is split by each sampled segment's state and scaled by THAT state's disagree share, which needs
   per-state agree/disagree counts: one read-only GROUP BY over the details table per (table, functional class), ~1 GB, dry-run priced first
   and cached (`state_populations.json`). `us_states_geojson()` fetches simplified state outlines once from bigquery-public-data
   (~20 MB scan) and keeps them on disk / GCS.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Iterable, Optional

SOURCES = (("osm", "OSM", "speed_limit_osm_mph"), ("here", "HERE", "speed_limit_here_mph"), ("model", "Model", None))


def _value(o: dict, column: Optional[str]):
    if column is None:   # "model": the corrected inference when there is one
        v = o.get("speed_limit_infer_mph_corrected")
        return v if v is not None else o.get("speed_limit_infer_mph")
    return o.get(column)


def closest_sources(o: dict) -> tuple[list[str], dict]:
    """(sources tied for closest to the real sign, {source: |value - sign| or None}). Empty list when no source had a value."""
    truth = o.get("ground_truth_mph")
    diffs: dict[str, Optional[float]] = {}
    for key, _, column in SOURCES:
        v = _value(o, column)
        diffs[key] = abs(v - truth) if truth is not None and v is not None else None
    present = {k: d for k, d in diffs.items() if d is not None}
    if not present:
        return [], diffs
    best = min(present.values())
    return [k for k, d in present.items() if abs(d - best) < 1e-9], diffs


def _result_maps(payload: dict) -> tuple[dict, dict]:
    """({segment_id: state}, {segment_id: (lat, lon)}) from the run's per-segment results (older observations don't carry these)."""
    state, centre = {}, {}
    for r in payload.get("results") or []:
        sid = r.get("segment_id")
        if not sid:
            continue
        if r.get("state"):
            state[sid] = str(r["state"]).upper()
        if r.get("centroid_lat") is not None and r.get("centroid_lon") is not None:
            centre[sid] = (r["centroid_lat"], r["centroid_lon"])
    return state, centre


def sample_points(runs: Iterable[tuple[str, str, dict]]) -> dict:
    """runs = (run id, run label, payload with display urls). Returns {"points": [...], "summary": {...}, "runs": n}. A point is positioned at
    the Street View camera that saw the sign (the best location we have for it), else at the sampled segment's centre."""
    points, summary, n_runs = [], {"osm": 0, "here": 0, "model": 0, "tie": 0, "none": 0}, 0
    for run_id, label, payload in runs:
        obs = payload.get("observations") or []
        if not obs:
            continue
        n_runs += 1
        seg_state, seg_centre = _result_maps(payload)
        for o in obs:
            lat, lon = o.get("sign_lat"), o.get("sign_lon")
            if lat is None or lon is None:
                lat, lon = seg_centre.get(o.get("segment_id"), (None, None))
            if lat is None or lon is None:
                continue
            closest, diffs = closest_sources(o)
            kind = "none" if not closest else (closest[0] if len(closest) == 1 else "tie")
            summary[kind] += 1
            points.append({
                "lat": lat, "lon": lon, "kind": kind, "closest": closest,
                "exact": bool(closest) and min(d for d in diffs.values() if d is not None) == 0,
                "truth": o.get("ground_truth_mph"),
                "osm": _value(o, "speed_limit_osm_mph"), "here": _value(o, "speed_limit_here_mph"), "model": _value(o, None),
                "street": o.get("street_name"), "segment": o.get("segment_id"), "direction": o.get("direction"),
                "state": seg_state.get(o.get("segment_id")), "run": run_id, "run_label": label, "snippet": o.get("snippet_url"),
            })
    return {"points": points, "summary": summary, "runs": n_runs}


# ---------------------------------------------------------------- RMSE by state
def _mse_stats(obs: list[dict], column: Optional[str]) -> tuple[int, Optional[float]]:
    diffs = []
    for o in obs:
        v, t = _value(o, column), o.get("ground_truth_mph")
        if v is not None and t is not None:
            diffs.append((v - t) ** 2)
    return len(diffs), (sum(diffs) / len(diffs) if diffs else None)


def needed_state_populations(payloads: Iterable[dict]) -> dict[str, dict]:
    """{cache key: run config} for each (table, functional class) of a nationwide run - the per-state agree/disagree split they need."""
    out = {}
    for p in payloads:
        cfg = p.get("config") or {}
        if str(cfg.get("state", "")).upper() == "US" and cfg.get("table") and p.get("observations"):
            out.setdefault(f"{cfg['table']}|{cfg.get('functional_class_lt')}", cfg)
    return out


def _state_pop_sql(project: str, dataset: str, table: str) -> str:
    # Same agree / disagree definition as precision_sample._population_sql, grouped by state.
    return f"""
        SELECT state,
          COUNTIF(speed_limit_osm_mph IS NOT NULL AND speed_limit_here_mph IS NOT NULL
                  AND COALESCE(speed_limit_infer_mph_corrected, speed_limit_infer_mph) IS NOT NULL
                  AND speed_limit_osm_mph = ROUND(speed_limit_here_mph)
                  AND ROUND(speed_limit_here_mph) = COALESCE(speed_limit_infer_mph_corrected, speed_limit_infer_mph)) AS agree_count,
          COUNTIF(speed_limit_osm_mph IS NOT NULL AND speed_limit_here_mph IS NOT NULL
                  AND COALESCE(speed_limit_infer_mph_corrected, speed_limit_infer_mph) IS NOT NULL
                  AND NOT (speed_limit_osm_mph = ROUND(speed_limit_here_mph)
                           AND ROUND(speed_limit_here_mph) = COALESCE(speed_limit_infer_mph_corrected, speed_limit_infer_mph))) AS disagree_count
        FROM `{project}.{dataset}.{table}`
        WHERE functional_class < @fc_lt
        GROUP BY state
    """


def fetch_state_populations(missing: dict[str, dict], *, max_cost_usd: float, price_per_tib: float) -> dict[str, dict]:
    """{cache key: {STATE: {"agree_count": a, "disagree_count": d}}} - dry-run priced first, raises above `max_cost_usd`."""
    from google.cloud import bigquery
    clients: dict[str, "bigquery.Client"] = {}

    def run(cfg, dry):
        c = clients.setdefault(cfg["project"], bigquery.Client(project=cfg["project"]))
        return c.query(_state_pop_sql(cfg["project"], cfg["dataset"], cfg["table"]), job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("fc_lt", "INT64", cfg["functional_class_lt"])], dry_run=dry, use_query_cache=not dry))

    total_bytes = sum((run(cfg, True).total_bytes_processed or 0) for cfg in missing.values())
    cost = total_bytes / 1024 ** 4 * price_per_tib
    if cost > max_cost_usd:
        raise RuntimeError(f"The per-state counts would read about {total_bytes / 1024 ** 3:.1f} GB (about ${cost:.2f}).")
    return {k: {str(r["state"]).upper(): {"agree_count": r["agree_count"], "disagree_count": r["disagree_count"]} for r in run(cfg, False).result()}
            for k, cfg in missing.items()}


def rmse_by_state(payloads: Iterable[dict], source: str, state_pops: dict) -> dict:
    """{"states": {ST: {"rmse_mph", "n", "runs"}}, "skipped": [..notes..]} - inferred overall RMSE of `source` ('osm'|'here'|'model') per state,
    runs combined weighted by observations."""
    column = {k: c for k, _, c in SOURCES}[source]
    acc: dict[str, dict] = {}   # state -> {"w", "tot", "n", "runs"}
    skipped = []

    def add(state: str, obs: list[dict], d: float):
        n, mse = _mse_stats(obs, column)
        if not n or mse is None:
            return
        a = acc.setdefault(state, {"w": 0.0, "tot": 0.0, "n": 0, "runs": 0})
        a["w"] += n
        a["tot"] += n * d * mse
        a["n"] += n
        a["runs"] += 1

    for p in payloads:
        obs = p.get("observations") or []
        if not obs:
            continue
        cfg = p.get("config") or {}
        run_state = str(cfg.get("state", "")).upper()
        if run_state and run_state != "US":
            pop = p.get("population")
            if not pop or not (pop.get("agree_count", 0) + pop.get("disagree_count", 0)):
                skipped.append(f"{run_state} run without a saved domain split")
                continue
            add(run_state, obs, pop["disagree_count"] / (pop["agree_count"] + pop["disagree_count"]))
            continue
        seg_state, _ = _result_maps(p)
        pops = state_pops.get(f"{cfg.get('table')}|{cfg.get('functional_class_lt')}") or {}
        by_state: dict[str, list[dict]] = {}
        for o in obs:
            st = seg_state.get(o.get("segment_id"))
            if st:
                by_state.setdefault(st, []).append(o)
        for st, st_obs in by_state.items():
            sp = pops.get(st)
            if not sp or not (sp["agree_count"] + sp["disagree_count"]):
                skipped.append(f"{st}: no per-state counts")
                continue
            add(st, st_obs, sp["disagree_count"] / (sp["agree_count"] + sp["disagree_count"]))
    states = {s: {"rmse_mph": math.sqrt(a["tot"] / a["w"]), "n": a["n"], "runs": a["runs"]} for s, a in acc.items() if a["w"]}
    return {"states": states, "skipped": sorted(set(skipped))}


# ---------------------------------------------------------------- caches
def load_json(path: Path, default):
    try:
        from find_bad_speed_limit import gcs_cache_pull
        gcs_cache_pull(path)
    except Exception:  # noqa: BLE001
        pass
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, default=str))
    tmp.replace(path)
    try:
        from find_bad_speed_limit import gcs_cache_push
        gcs_cache_push(path)
    except Exception:  # noqa: BLE001
        pass


def us_states_geojson(path: Path, project: str) -> dict:
    """GeoJSON FeatureCollection of simplified US state outlines (properties.state = USPS code), fetched from BigQuery's public geo_us_boundaries
    once (~20 MB scan, read-only) and then served from `path`."""
    cached = load_json(path, None)
    if cached:
        return cached
    from google.cloud import bigquery
    rows = bigquery.Client(project=project).query(
        "SELECT state, state_name, ST_ASGEOJSON(ST_SIMPLIFY(state_geom, 15000)) AS geojson FROM `bigquery-public-data.geo_us_boundaries.states`"
    ).result()
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"state": r["state"], "name": r["state_name"]}, "geometry": json.loads(r["geojson"])} for r in rows]}
    save_json(path, fc)
    return fc
