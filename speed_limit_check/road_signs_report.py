"""A report built ONLY from already-analyzed signs - no Street View, Vision or Gemini calls.

Reads `calc_out.road_signs` (one row per segment that was checked for a sign: the segment, the `calc_out.signs`
id of the sign found per direction, when it was checked, and whether the segment was `sampled` - the one a run
set out to check - or `traversed`, only reached while walking for a sign) joined to `calc_out.signs` (what the
sign read, where it was seen) and to the speed-limit details table (what OSM / HERE / the model say). From that it
builds the same per-source precision / recall / RMSE and segment-direction observations the precision-sample
report shows, so one can re-cut the evidence later (sampled only, traversed only, or both) without spending
anything on imagery.

Rules this follows: the only thing it ever does to BigQuery is a read-only SELECT, preceded by a free dry run whose
cost is reported, and above COST_CONFIRMATION_THRESHOLD_USD it asks for confirmation first (same rule as the
Custom-test SQL console). Project references are implicit (deployable to any project).

A segment can have been checked more than once (different runs); only its LATEST check, among the origins asked
for, is used. Not a statistical sample: `traversed` segments and Evaluator candidates were not drawn at random, so
no "inferred overall" figures are produced here - only measurements on the analyzed signs themselves.
"""
from __future__ import annotations

from typing import Optional

from google.cloud import bigquery

from bq_sql_console import BQ_ON_DEMAND_PRICE_PER_TIB_USD, COST_CONFIRMATION_THRESHOLD_USD
from find_bad_speed_limit import DATASET_RE, PROJECT_RE, STATE_RE, _validate_identifier
from precision_sample import TABLE_RE, _source_stats

ORIGINS = {"sampled": ["sampled"], "traversed": ["traversed"], "both": ["sampled", "traversed"]}
_BYTES_PER_GB = 1024 ** 3
_BYTES_PER_TIB = 1024 ** 4
TOLERANCE_MPH = 5.0


def build_sql(dataset: str, table: str) -> str:
    return f"""
WITH latest AS (
  SELECT * EXCEPT (rn) FROM (
    SELECT r.*, ROW_NUMBER() OVER (PARTITION BY r.here_segment_id ORDER BY r.datetime_checked DESC) AS rn
    FROM calc_out.road_signs r
    WHERE r.origin IN UNNEST(@origins)
  ) WHERE rn = 1
)
SELECT
  l.here_segment_id, l.origin, l.datetime_checked,
  l.sign_found_on_id_in_dir_1, l.dir_1, l.sign_found_on_id_in_dir_2, l.dir_2,
  s1.sign_reading AS reading_1, s1.lat AS lat_1, s1.lon AS lon_1, s1.direction_of_travel_deg AS heading_1, s1.image_gcs_path AS image_1,
  s2.sign_reading AS reading_2, s2.lat AS lat_2, s2.lon AS lon_2, s2.direction_of_travel_deg AS heading_2, s2.image_gcs_path AS image_2,
  t.street_name, t.functional_class,
  t.speed_limit_osm_mph, ROUND(t.speed_limit_here_mph) AS speed_limit_here_mph,
  t.speed_limit_infer_mph, t.speed_limit_infer_mph_corrected, t.speed_AVG_mph, t.freeflow_mph,
  ST_ASGEOJSON(t.geom) AS geom_geojson
FROM latest l
JOIN {dataset}.{table} t ON t.here_segment_id = l.here_segment_id
LEFT JOIN calc_out.signs s1 ON s1.sign_id = l.sign_found_on_id_in_dir_1
LEFT JOIN calc_out.signs s2 ON s2.sign_id = l.sign_found_on_id_in_dir_2
WHERE t.state = @state AND t.functional_class < @fc
"""


def _params(state: str, fc_lt: int, origins: list[str]) -> list:
    return [
        bigquery.ScalarQueryParameter("state", "STRING", state),
        bigquery.ScalarQueryParameter("fc", "INT64", fc_lt),
        bigquery.ArrayQueryParameter("origins", "STRING", origins),
    ]


def _image_urls(gcs_path: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(local-style path, full-image url, snippet url) from a `gs://bucket/cache/spl/gsv/output/<rest>` object path -
    the app's /images/<rest> route serves it (pulling from GCS when it isn't on local disk)."""
    if not gcs_path or "/output/" not in gcs_path:
        return None, None, None
    rest = gcs_path.split("/output/", 1)[1]
    folder = rest.rsplit("/", 1)[0]
    return "output/" + rest, "/images/" + rest, "/images/" + folder + "/sign_snippet.jpg"


def _observations(rows: list[dict]) -> list[dict]:
    """Segment-direction observations: one 'both' row when the same sign serves both directions (a sign found directly
    on the segment), else one per direction that has its own sign."""
    obs = []
    for r in rows:
        s1, s2 = r.get("sign_found_on_id_in_dir_1"), r.get("sign_found_on_id_in_dir_2")
        picks = []
        if s1 and s2 and s1 == s2:
            picks.append(("both", 1))
        else:
            if s1:
                picks.append((f"dir_1 ({r['dir_1']}°)" if r.get("dir_1") is not None else "dir_1", 1))
            if s2:
                picks.append((f"dir_2 ({r['dir_2']}°)" if r.get("dir_2") is not None else "dir_2", 2))
        for label, n in picks:
            reading = r.get(f"reading_{n}")
            if reading is None:
                continue  # sign id recorded but the signs row isn't (yet) loadable - nothing to compare against
            annotated, image_url, snippet_url = _image_urls(r.get(f"image_{n}"))
            obs.append({
                "segment_id": r["here_segment_id"], "direction": label, "origin": r["origin"],
                "datetime_checked": r["datetime_checked"].isoformat() if hasattr(r["datetime_checked"], "isoformat") else r["datetime_checked"],
                "street_name": r.get("street_name"), "functional_class": r.get("functional_class"),
                "ground_truth_mph": reading, "matched_segment_id": r["here_segment_id"], "steps_walked": None,
                "sign_lat": r.get(f"lat_{n}"), "sign_lon": r.get(f"lon_{n}"), "sign_heading": r.get(f"heading_{n}"),
                "speed_limit_osm_mph": r.get("speed_limit_osm_mph"), "speed_limit_here_mph": r.get("speed_limit_here_mph"),
                "speed_limit_infer_mph": r.get("speed_limit_infer_mph"),
                "speed_limit_infer_mph_corrected": r.get("speed_limit_infer_mph_corrected"),
                "speed_AVG_mph": r.get("speed_AVG_mph"), "freeflow_mph": r.get("freeflow_mph"),
                "annotated_image": annotated, "image_url": image_url, "snippet_url": snippet_url,
                "segment_geojson": r.get("geom_geojson"), "matched_segment_geojson": r.get("geom_geojson"),
            })
    return obs


def summarize(rows: list[dict], obs: list[dict]) -> dict:
    with_sign = [r for r in rows if r.get("sign_found_on_id_in_dir_1") or r.get("sign_found_on_id_in_dir_2")]
    by_origin = {}
    for r in rows:
        o = by_origin.setdefault(r["origin"], {"checked": 0, "with_sign": 0})
        o["checked"] += 1
        if r.get("sign_found_on_id_in_dir_1") or r.get("sign_found_on_id_in_dir_2"):
            o["with_sign"] += 1
    return {
        "segments_checked": len(rows), "segments_with_sign": len(with_sign),
        "sign_find_rate": (len(with_sign) / len(rows)) if rows else None,
        "observations": len(obs), "by_origin": by_origin,
        "stats": {
            "osm": _source_stats(obs, "speed_limit_osm_mph", TOLERANCE_MPH),
            "here": _source_stats(obs, "speed_limit_here_mph", TOLERANCE_MPH),
            "model": _source_stats(obs, None, TOLERANCE_MPH),
        },
    }


def run_report(
    project: str, dataset: str, table: str, *, state: str, functional_class_lt: int, origin: str, confirmed: bool = False,
) -> dict:
    """Either {"needs_confirmation": True, "estimate": {...}} (cost over the threshold, not yet confirmed), or the full
    report {"summary", "observations", "estimate", "bytes_billed", ...}. Raises ValueError for bad inputs."""
    _validate_identifier(project, PROJECT_RE, "project")
    _validate_identifier(dataset, DATASET_RE, "dataset")
    _validate_identifier(table, TABLE_RE, "table")
    _validate_identifier(state.upper(), STATE_RE, "state")
    if origin not in ORIGINS:
        raise ValueError("origin must be sampled, traversed or both.")
    sql = build_sql(dataset, table)
    params = _params(state.upper(), int(functional_class_lt), ORIGINS[origin])
    client = bigquery.Client(project=project)

    dry = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params, dry_run=True, use_query_cache=False))
    bytes_processed = dry.total_bytes_processed or 0
    estimate = {"gb": bytes_processed / _BYTES_PER_GB, "cost_usd": (bytes_processed / _BYTES_PER_TIB) * BQ_ON_DEMAND_PRICE_PER_TIB_USD}
    if estimate["cost_usd"] > COST_CONFIRMATION_THRESHOLD_USD and not confirmed:
        return {"needs_confirmation": True, "estimate": estimate}

    job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    rows = [dict(r) for r in job.result()]
    obs = _observations(rows)
    billed = job.total_bytes_billed or 0
    return {
        "needs_confirmation": False, "origin": origin, "state": state.upper(), "functional_class_lt": int(functional_class_lt),
        "summary": summarize(rows, obs), "observations": obs,
        "estimate": estimate, "bytes_billed": billed, "gb_billed": billed / _BYTES_PER_GB,
        "cost_usd_billed": (billed / _BYTES_PER_TIB) * BQ_ON_DEMAND_PRICE_PER_TIB_USD,
    }
