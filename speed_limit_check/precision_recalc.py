"""Fills in what a precision-sample run finished BEFORE the newer report fields existed is missing - on a button click,
with no new Street View / Vision / Gemini spend:

  - Recall, RMSE and "inferred" recall / RMSE: pure arithmetic over the observations and population the run already stored.
  - The sign snippet (thumbnail): re-found from the run's own cached Street View frames. Vision's answer for each frame is
    already cached next to it (`<frame>.ocr.json`), so re-reading the sign is free; a frame without a cached answer is
    skipped, never sent to Vision.
  - The map: the segments' geometry (one read-only BigQuery SELECT on the state's details table, dry-run cost checked
    first by the caller) and, when the sign was recorded in calc_out.signs, the camera heading (a second tiny SELECT).

Read-only against BigQuery; everything else is local/GCS cache files. Returns a NEW payload; the caller persists it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from google.cloud import bigquery

import find_bad_speed_limit as fbs
from precision_sample import (
    _observations, compute_precision_stats, infer_overall_metrics,
)

_BYTES_PER_GB = 1024 ** 3
_BYTES_PER_TIB = 1024 ** 4


def _segment_ids(results: list[dict]) -> set[str]:
    ids = set()
    for r in results:
        if r.get("segment_id"):
            ids.add(r["segment_id"])
        for key in ("forward", "backward"):
            d = r.get(key) or {}
            if d.get("matched_segment_id"):
                ids.add(d["matched_segment_id"])
    return ids


def _geometry_sql(dataset: str, table: str) -> str:
    return f"SELECT here_segment_id, ST_ASGEOJSON(geom) AS geom_geojson FROM {dataset}.{table} WHERE here_segment_id IN UNNEST(@ids)"


def estimate_geometry_cost(project: str, dataset: str, table: str, results: list[dict]) -> dict:
    """Free dry run of the one geometry query - {gb, cost_usd} (zero when there is nothing to look up)."""
    ids = sorted(_segment_ids(results))
    if not ids:
        return {"gb": 0.0, "cost_usd": 0.0}
    from bq_sql_console import BQ_ON_DEMAND_PRICE_PER_TIB_USD
    job = bigquery.Client(project=project).query(
        _geometry_sql(dataset, table),
        job_config=bigquery.QueryJobConfig(query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", ids)], dry_run=True, use_query_cache=False),
    )
    b = job.total_bytes_processed or 0
    return {"gb": b / _BYTES_PER_GB, "cost_usd": (b / _BYTES_PER_TIB) * BQ_ON_DEMAND_PRICE_PER_TIB_USD}


def _fetch_geometry(project: str, dataset: str, table: str, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    job = bigquery.Client(project=project).query(
        _geometry_sql(dataset, table),
        job_config=bigquery.QueryJobConfig(query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", ids)]),
    )
    return {r["here_segment_id"]: r["geom_geojson"] for r in job.result()}


def _gcs_path_for(local: str) -> Optional[str]:
    """The gs:// object path calc_out.signs.image_gcs_path holds for a local annotated image path."""
    if not fbs.GCS_CACHE_BUCKET:
        return None
    return f"gs://{fbs.GCS_CACHE_BUCKET}/{fbs._gcs_object_key(Path(local))}"


def _fetch_headings(project: str, annotated_paths: list[str]) -> dict[str, int]:
    """{annotated image path: camera heading} from calc_out.signs, where the sign was recorded. Best effort."""
    by_gcs = {g: p for p in annotated_paths if (g := _gcs_path_for(p))}
    if not by_gcs:
        return {}
    try:
        job = bigquery.Client(project=project).query(
            "SELECT image_gcs_path, direction_of_travel_deg FROM calc_out.signs WHERE image_gcs_path IN UNNEST(@paths)",
            job_config=bigquery.QueryJobConfig(query_parameters=[bigquery.ArrayQueryParameter("paths", "STRING", sorted(by_gcs))]),
        )
        return {by_gcs[r["image_gcs_path"]]: r["direction_of_travel_deg"] for r in job.result() if r["direction_of_travel_deg"] is not None}
    except Exception as e:  # noqa: BLE001 - e.g. the signs table doesn't exist yet: the map just has no heading line
        print(f"  (no camera headings available: {type(e).__name__}: {e})")
        return {}


def _pull_folder(folder: Path) -> None:
    """Make the run's cached frames and their cached OCR answers for one sign folder available locally."""
    if not fbs.GCS_CACHE_BUCKET:
        return
    prefix = fbs._gcs_object_key(folder) + "/"
    try:
        for blob in fbs._gcs_bucket().list_blobs(prefix=prefix):
            rest = blob.name[len(prefix):]
            if "/" in rest or not rest.startswith("streetview_heading"):
                continue
            local = folder / rest
            if not local.exists():
                local.parent.mkdir(parents=True, exist_ok=True)
                blob.download_to_filename(str(local))
    except Exception as e:  # noqa: BLE001
        print(f"  (could not list cached frames for {folder}: {type(e).__name__}: {e})")


def make_snippet(annotated_image: str, truth_mph: int) -> bool:
    """Creates sign_snippet.jpg next to the annotated image if missing, from a cached frame whose cached OCR re-reads
    `truth_mph`. True when a snippet exists afterwards. Never calls Vision."""
    annotated = Path(annotated_image)
    snippet = annotated.parent / "sign_snippet.jpg"
    if snippet.exists() or fbs.gcs_cache_pull(snippet):
        return True
    _pull_folder(annotated.parent)
    for frame in sorted(annotated.parent.glob("streetview_heading*.jpg")):
        if not frame.with_name(frame.name + ".ocr.json").exists():
            continue  # no cached OCR answer for this frame - would cost a Vision call, so skip it
        try:
            reading, _ = fbs.find_sign_in_image(None, frame)
        except Exception:  # noqa: BLE001
            continue
        if reading and reading.speed_mph == truth_mph:
            fbs.save_sign_snippet(reading, snippet)
            fbs.gcs_cache_push(snippet)
            return True
    return False


def recalculate(payload: dict, *, project: str, dataset: str, table: str, on_progress=lambda msg: None) -> dict:
    results = [dict(r) for r in payload.get("results", [])]
    tolerance = (payload.get("stats", {}).get("osm", {}) or {}).get("within_tolerance_mph") or 5.0

    on_progress("Looking up segment geometry (read-only BigQuery)...")
    geometry = _fetch_geometry(project, dataset, table, sorted(_segment_ids(results)))

    annotated = sorted({
        p for r in results for p in (
            [r.get("annotated_image")] + [(r.get(k) or {}).get("annotated_image") for k in ("forward", "backward")]
        ) if p
    })
    on_progress("Looking up camera headings recorded in calc_out.signs...")
    headings = _fetch_headings(project, annotated)

    snippets_made = 0
    for r in results:
        if not r.get("segment_geojson"):
            r["segment_geojson"] = geometry.get(r["segment_id"])
        if r.get("found_on_segment") and r.get("annotated_image"):
            r.setdefault("sign_heading", headings.get(r["annotated_image"]))
            if r.get("sign_heading") is None:
                r["sign_heading"] = headings.get(r["annotated_image"])
        for key in ("forward", "backward"):
            d = r.get(key)
            if d and d.get("found"):
                d = dict(d)
                if not d.get("segment_geojson"):
                    d["segment_geojson"] = geometry.get(d.get("matched_segment_id"))
                if d.get("sign_heading") is None:
                    d["sign_heading"] = headings.get(d.get("annotated_image"))
                r[key] = d

    on_progress("Re-creating sign snippets from cached frames (no new imagery calls)...")
    seen = set()
    for r in results:
        for truth, img in (
            [(r.get("ground_truth_mph"), r.get("annotated_image"))]
            + [((r.get(k) or {}).get("ground_truth_mph"), (r.get(k) or {}).get("annotated_image")) for k in ("forward", "backward")]
        ):
            if img and truth is not None and img not in seen:
                seen.add(img)
                if make_snippet(img, truth):
                    snippets_made += 1

    on_progress("Recomputing precision / recall / RMSE...")
    obs = _observations(results)
    stats = compute_precision_stats(results, obs, tolerance)
    new = dict(payload)
    new.update({"results": results, "observations": obs, "stats": stats})
    if payload.get("population"):
        new["inferred_metrics"] = infer_overall_metrics(payload["population"], stats)
    new["recalculated_at"] = datetime.now(timezone.utc).isoformat()
    new["recalculated_snippets"] = snippets_made
    return new
