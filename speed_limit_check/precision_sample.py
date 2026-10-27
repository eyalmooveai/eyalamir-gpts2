#!/usr/bin/env python3
"""Sample road segments at random (weighted by length) within a functional-class
range, find the real-world posted speed limit for each one by reading a Street
View sign - walking back up to a few segments against the direction of travel
when the sampled segment itself has none - and report how often OSM, HERE, and
our own inferred speed limit actually match that ground truth.

This is a PRECISION/ACCURACY EVAL, not a mismatch-finder: find_bad_speed_limit.py
(and the Evaluator built on it) deliberately picks segments where sources
*disagree*, which is exactly the wrong sampling frame for measuring how often a
source is *right* - that needs a sample that doesn't already know the answer.
This script instead samples segments the same unbiased way a survey would:
uniformly at random across the table's domain, weighted by each segment's own
length (ST_LENGTH(geom), in meters) so a long highway segment isn't counted the
same as a 20m connector - then reads the real sign.

Built on top of find_bad_speed_limit.py's existing pipeline - it does NOT
reimplement the Street View / Vision OCR mechanics:
  - _process_one_candidate() is the exact same "walk this segment's positions,
    look for a sign, OCR it" unit of work the Evaluator and Sign Checker use.
  - fetch_candidate_by_id() is the exact same "look up one here_segment_id's
    full row" used when walking back to a predecessor segment.
  - The global Street View throttle (_streetview_throttle_lock) already
    serializes every worker thread's calls regardless of --concurrency, same
    as batch_evaluator.py relies on.

Walking back across segments (new - nothing in this app's existing code has a
notion of segment-to-segment topology): calc_out's own speed_limits_*_details
tables have no from/to-node or adjacency column at all (confirmed live via
INFORMATION_SCHEMA). The real HERE street-network topology lives in a
DIFFERENT, currently-unused-by-this-app dataset: `maps.road_segments_<STATE>`
carries `start_node_ref`/`end_node_ref` HERE node IDs per segment (joinable to
calc_out's `here_segment_id` via its own `id` column, same
"here:cm:segment:<digits>" format - confirmed by a live round-trip lookup).
"The segment(s) immediately before segment X" is found by a self-join:
predecessor.end_node_ref = X.start_node_ref - i.e. segments that feed INTO
X's start node. At a branching intersection with more than one predecessor,
this picks the one with the same street_name as X first (continuing the same
road), falling back to the one with the same functional_class, then just the
first one found - logged as a note either way, since this is a heuristic, not
a certainty.

IMPORTANT CAVEAT, inherited from the rest of this app (see run_pipeline's own
docstring in find_bad_speed_limit.py): HERE's digitized line direction for a
segment isn't guaranteed to match real/legal direction of travel. This script
assumes it does (start_node -> end_node is "forward") when deciding which
direction is "back" - the same simplifying assumption the Evaluator/Sign
Checker already make for "left"/"right" side probing. There is no more
precise per-segment signal available in this project's data to do better.

Also note: BigQuery's RAND() has no seed parameter, so re-running this script
against the same inputs will draw a different sample each time - this is a
survey tool, not something that needs bit-for-bit reproducibility.

Usage (local pilot - this will eventually run as a script invoked from the
archimedes.moove.ai Cloud Run service, not just a laptop):
    python precision_sample.py --table speed_limits_CO_2026_08_details --state CO \\
        --functional-class-lt 3 --sample-size 50

Keys/credentials: same as find_bad_speed_limit.py - GOOGLE_MAPS_API_KEY from
the environment or --keys-file (default ~/Claude/MooveAI/keys.env); BigQuery
and Vision use standard Application Default Credentials.

No built-in daily cost cap (unlike the web app's Evaluator, which caps
cross-user spend) - this is a single-operator CLI tool, same as
find_bad_speed_limit.py itself. Start with a small --sample-size.
"""
from __future__ import annotations

import argparse
import csv as csv_module
import json
import os
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from google.cloud import bigquery, vision

from find_bad_speed_limit import (
    DATASET_RE,
    DEFAULT_DATASET,
    DEFAULT_FOV,
    DEFAULT_HEADINGS,
    DEFAULT_KEYS_FILE,
    DEFAULT_PROJECT,
    MAX_FOV,
    MIN_FOV,
    NoUsableApiKey,
    PROJECT_RE,
    STATE_RE,
    SIDE_MODES,
    StreetViewAuthError,
    _jsonable_row,
    _validate_identifier,
    fetch_candidate_by_id,
    get_api_call_counts,
    load_keys_file,
    parse_headings,
    reset_api_call_counts,
    safe_segment_dirname,
    _process_one_candidate,
)

# calc_out's own table names are validated via find_bad_speed_limit.table_name()
# (state/year/month -> a deterministic name) - this script instead takes an
# arbitrary --table (it may be a per-state _details table or the nationwide
# one), so it needs its own identifier check before that name is ever
# interpolated into SQL (same character class as find_bad_speed_limit's
# DATASET_RE - BigQuery table names are also just [A-Za-z0-9_]).
TABLE_RE = re.compile(r"^[A-Za-z0-9_]{1,1024}$")

# The HERE street-network topology dataset (see module docstring) - a fixed
# project dataset name, not user input, so it isn't validated like a
# user-supplied identifier.
MAPS_DATASET = "maps"

DEFAULT_MAX_WALK_BACK = 5
DEFAULT_CONCURRENCY = 5
MAX_CONCURRENCY = 20  # a sanity ceiling on the CLI flag, not a hard API limit
# A hard ceiling independent of what's typed on the command line, same
# instinct as batch_evaluator.MAX_SEGMENT_COUNT - each sampled segment can
# fan out into up to (1 + max_walk_back) real Street View/Vision lookups, so
# this is deliberately lower than that 1000-segment batch cap.
MAX_SAMPLE_SIZE = 500


def sample_segments_weighted_by_length(
    project: str, dataset: str, table: str, *, state: str, functional_class_lt: int, sample_size: int,
) -> list[bigquery.table.Row]:
    """Picks `sample_size` segments uniformly at random, weighted by each
    segment's own length - true probability-proportional-to-size sampling in
    one pass, via the Efraimidis-Spirakis weighted-reservoir-sampling trick:
    for weight w_i, draw key_i = ln(u_i) / w_i for u_i ~ Uniform(0,1) (u_i is
    RAND() here); the N rows with the LARGEST keys are a sample drawn without
    replacement with probability proportional to w_i. Since ln(u_i) < 0
    always, dividing by a larger weight (a longer segment) pushes the key
    closer to zero - i.e. larger - which is exactly "longer segments are more
    likely to win." RAND() is evaluated exactly once per row (in the SELECT
    list, aliased as `_sample_key`) and ORDER BY re-uses that same computed
    value rather than re-evaluating RAND() a second time - BigQuery (like
    standard SQL) resolves an ORDER BY name against the SELECT list first.
    speed_limit_here_mph is rounded here for the same reason every other
    query in this app rounds it (see CLAUDE.md) - it's stored as a precise,
    noisy km/h->mph conversion, not a clean posted-sign value."""
    client = bigquery.Client(project=project)
    query = f"""
        SELECT
          * EXCEPT (geom) REPLACE (ROUND(speed_limit_here_mph) AS speed_limit_here_mph),
          ST_Y(ST_CENTROID(geom)) AS centroid_lat,
          ST_X(ST_CENTROID(geom)) AS centroid_lon,
          ST_ASGEOJSON(geom) AS geom_geojson,
          LN(RAND()) / GREATEST(ST_LENGTH(geom), 1.0) AS _sample_key
        FROM `{project}.{dataset}.{table}`
        WHERE functional_class < @fc_lt AND state = @state
        ORDER BY _sample_key DESC
        LIMIT @sample_size
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("fc_lt", "INT64", functional_class_lt),
        bigquery.ScalarQueryParameter("state", "STRING", state.upper()),
        bigquery.ScalarQueryParameter("sample_size", "INT64", sample_size),
    ])
    return list(client.query(query, job_config=job_config).result())


def _maps_table_for_state(state: str) -> str:
    table = f"road_segments_{state.upper()}"
    _validate_identifier(table, TABLE_RE, "maps table")
    return table


def _lookup_segment_topology(project: str, state: str, segment_ids: list[str]) -> dict[str, dict]:
    """start_node_ref/end_node_ref/street_name/functional_class for a batch
    of here_segment_id values, from maps.road_segments_<STATE> - the HERE
    street-network topology table calc_out itself doesn't carry (see module
    docstring). Returns {segment_id: row_dict}; a segment_id not present in
    the result just wasn't found in this state's topology table (e.g. it's
    right at a state border, or the two datasets' coverage doesn't line up
    perfectly) - callers treat that as "can't walk back from here," not an
    error."""
    if not segment_ids:
        return {}
    table = _maps_table_for_state(state)
    client = bigquery.Client(project=project)
    query = f"""
        SELECT id AS here_segment_id, start_node_ref, end_node_ref, street_name, functional_class
        FROM `{project}.{MAPS_DATASET}.{table}`
        WHERE id IN UNNEST(@ids)
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", segment_ids)])
    rows = client.query(query, job_config=job_config).result()
    return {r["here_segment_id"]: dict(r.items()) for r in rows}


def _find_predecessor_segments(project: str, state: str, start_node_ref: str, *, exclude_segment_id: str) -> list[dict]:
    """Segments whose end_node_ref feeds INTO `start_node_ref` - i.e.
    segments that come immediately BEFORE a segment starting there, under
    this script's documented "digitized direction = direction of travel"
    assumption."""
    if not start_node_ref:
        return []
    table = _maps_table_for_state(state)
    client = bigquery.Client(project=project)
    query = f"""
        SELECT id AS here_segment_id, start_node_ref, end_node_ref, street_name, functional_class
        FROM `{project}.{MAPS_DATASET}.{table}`
        WHERE end_node_ref = @node AND id != @exclude_id
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("node", "STRING", start_node_ref),
        bigquery.ScalarQueryParameter("exclude_id", "STRING", exclude_segment_id),
    ])
    return [dict(r.items()) for r in client.query(query, job_config=job_config).result()]


def _pick_predecessor(predecessors: list[dict], *, street_name: Optional[str], functional_class: Optional[int]) -> Optional[dict]:
    """At a branching intersection (more than one segment feeds the same
    node), prefer the one continuing the same named road, then the one with
    the same functional_class, else just the first candidate - a heuristic,
    not a guarantee (see module docstring)."""
    if not predecessors:
        return None
    if street_name:
        same_name = [p for p in predecessors if p.get("street_name") == street_name]
        if same_name:
            return same_name[0]
    if functional_class is not None:
        same_fc = [p for p in predecessors if p.get("functional_class") == functional_class]
        if same_fc:
            return same_fc[0]
    return predecessors[0]


def _walk_back_chain(project: str, state: str, start_topo: dict, max_walk_back: int) -> list[dict]:
    """[start segment's topology row, predecessor 1, predecessor 2, ...], up
    to max_walk_back predecessors. Stops early at a dead end (no predecessor
    found) or if a segment already in the chain would be revisited (guards
    against an accidental loop in the graph)."""
    chain = [start_topo]
    visited = {start_topo["here_segment_id"]}
    current = start_topo
    while len(chain) <= max_walk_back:
        preds = _find_predecessor_segments(
            project, state, current.get("start_node_ref"), exclude_segment_id=current["here_segment_id"],
        )
        nxt = _pick_predecessor(preds, street_name=current.get("street_name"), functional_class=current.get("functional_class"))
        if not nxt or nxt["here_segment_id"] in visited:
            break
        chain.append(nxt)
        visited.add(nxt["here_segment_id"])
        current = nxt
    return chain


def _process_sampled_segment(
    i: int, n: int, sampled_row: bigquery.table.Row, *,
    project: str, dataset: str, table: str, state: str, max_walk_back: int,
    api_key: str, vision_client, out_root: Path,
    walk_segment: bool, walk_segment_spacing_m: float,
    side_mode: str, side_offset_m: float, auto_side_offset: bool,
    headings: tuple[int, ...], headings_relative: bool, fov: int,
    log=lambda msg: None,
) -> dict:
    """Runs the full per-sample pipeline: check the sampled segment itself
    for a sign, and if none is found, walk back up to max_walk_back segments
    (against the assumed direction of travel) trying each one in turn, same
    per-segment "walk positions, check Street View, OCR" logic the Evaluator
    uses (_process_one_candidate) at every step. Returns one flat result
    dict - never raises for an ordinary "no sign anywhere in the chain"
    outcome, only for a real error (caller decides how to handle that)."""
    seg_id = sampled_row["here_segment_id"]
    tag = f"[{i + 1}/{n}]"
    sample_out_root = out_root / safe_segment_dirname(seg_id)

    topo_by_id = _lookup_segment_topology(project, state, [seg_id])
    start_topo = topo_by_id.get(seg_id)
    if start_topo is None:
        log(f"{tag} {seg_id}: not found in maps.road_segments_{state.upper()} - checking this segment only, no walk-back.")
        chain_topo = [{
            "here_segment_id": seg_id, "street_name": sampled_row.get("street_name"),
            "functional_class": sampled_row.get("functional_class"), "start_node_ref": None, "end_node_ref": None,
        }]
    else:
        chain_topo = _walk_back_chain(project, state, start_topo, max_walk_back)
        log(f"{tag} {seg_id}: walk-back chain has {len(chain_topo)} segment(s) "
            f"({', '.join(s['here_segment_id'] for s in chain_topo)}).")

    attempts_detail = []  # [(segment_id, status, note), ...] - every step tried, not just the winner
    match = None
    matched_step_index = None
    matched_segment_id = None

    for step_index, step_topo in enumerate(chain_topo):
        step_seg_id = step_topo["here_segment_id"]
        if step_index == 0:
            step_row = sampled_row  # already have the full row (incl. geom_geojson) from the sampling query
        else:
            rows = fetch_candidate_by_id(project, dataset, table, step_seg_id)
            if not rows:
                log(f"{tag}   step {step_index} ({step_seg_id}): not found in {dataset}.{table} - skipping this step.")
                attempts_detail.append((step_seg_id, "not_in_table", ""))
                continue
            step_row = rows[0]

        attempt, step_match = _process_one_candidate(
            step_index, len(chain_topo), step_row,
            api_key=api_key, vision_client=vision_client, out_root=sample_out_root,
            walk_segment=walk_segment, walk_segment_spacing_m=walk_segment_spacing_m,
            side_mode=side_mode, side_offset_m=side_offset_m, auto_side_offset=auto_side_offset,
            headings=headings, headings_relative=headings_relative, fov=fov,
            log=log, progress=lambda fraction, message: None,
        )
        attempts_detail.append((step_seg_id, attempt.status, attempt.note))
        if step_match:
            match = step_match
            matched_step_index = step_index
            matched_segment_id = step_seg_id
            break

    sampled_dict = _jsonable_row(sampled_row)
    found = match is not None
    sign_detail = match.image_details[match.matched_image_index] if found else None

    return {
        "segment_id": seg_id,
        "state": state.upper(),
        "street_name": sampled_dict.get("street_name"),
        "functional_class": sampled_dict.get("functional_class"),
        "centroid_lat": sampled_dict.get("centroid_lat"),
        "centroid_lon": sampled_dict.get("centroid_lon"),
        "speed_limit_osm_mph": sampled_dict.get("speed_limit_osm_mph"),
        "speed_limit_here_mph": sampled_dict.get("speed_limit_here_mph"),
        "speed_limit_infer_mph": sampled_dict.get("speed_limit_infer_mph"),
        "speed_limit_infer_mph_corrected": sampled_dict.get("speed_limit_infer_mph_corrected"),
        "speed_AVG_mph": sampled_dict.get("speed_AVG_mph"),
        "freeflow_mph": sampled_dict.get("freeflow_mph"),
        "found_sign": found,
        "ground_truth_mph": match.reading.speed_mph if found else None,
        "sign_lat": sign_detail.lat if sign_detail else None,
        "sign_lon": sign_detail.lon if sign_detail else None,
        "sign_evidence": match.reading.evidence if found else None,
        "streetview_capture_date": sign_detail.capture_date if sign_detail else None,
        "annotated_image": str(match.annotated_image) if found else None,
        "matched_segment_id": matched_segment_id,
        "steps_walked_back": matched_step_index if found else (len(attempts_detail) - 1),
        "segments_tried": [sid for sid, _, _ in attempts_detail],
        "status": "match" if found else "no_sign",
        "note": "; ".join(f"{sid}:{status}" for sid, status, _ in attempts_detail if status != "match"),
    }


def _streetview_url(lat, lon) -> str:
    return f"https://www.google.com/maps?q=&layer=c&cbll={lat},{lon}" if lat is not None and lon is not None else ""


CSV_FIELDNAMES = [
    "segment_id", "state", "street_name", "functional_class",
    "status", "found_sign", "ground_truth_mph",
    "speed_limit_osm_mph", "speed_limit_here_mph", "speed_limit_infer_mph", "speed_limit_infer_mph_corrected",
    "speed_AVG_mph", "freeflow_mph",
    "centroid_lat", "centroid_lon", "sign_lat", "sign_lon", "streetview_url",
    "matched_segment_id", "steps_walked_back", "segments_tried",
    "streetview_capture_date", "annotated_image", "sign_evidence", "note",
]


def write_results_csv(path: Path, results: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv_module.DictWriter(f, fieldnames=CSV_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            row = dict(r)
            row["streetview_url"] = _streetview_url(r.get("sign_lat") or r.get("centroid_lat"), r.get("sign_lon") or r.get("centroid_lon"))
            row["segments_tried"] = ",".join(r.get("segments_tried") or [])
            writer.writerow(row)


def _source_value(result: dict, column: Optional[str]) -> Optional[float]:
    if column is None:  # "model" - prefer the corrected inference, fall back to the base one
        value = result.get("speed_limit_infer_mph_corrected")
        return value if value is not None else result.get("speed_limit_infer_mph")
    return result.get(column)


def _source_stats(found_results: list[dict], column: Optional[str], tolerance_mph: float) -> dict:
    diffs = []
    exact = 0
    within_tolerance = 0
    for r in found_results:
        truth = r.get("ground_truth_mph")
        value = _source_value(r, column)
        if truth is None or value is None:
            continue
        diff = abs(value - truth)
        diffs.append(diff)
        if diff == 0:
            exact += 1
        if diff <= tolerance_mph:
            within_tolerance += 1
    n = len(diffs)
    return {
        "n_compared": n,
        "exact_match_count": exact,
        "exact_match_rate": (exact / n) if n else None,
        "within_tolerance_mph": tolerance_mph,
        "within_tolerance_count": within_tolerance,
        "within_tolerance_rate": (within_tolerance / n) if n else None,
        "mean_abs_error_mph": (sum(diffs) / n) if n else None,
        "median_abs_error_mph": statistics.median(diffs) if diffs else None,
    }


def compute_precision_stats(results: list[dict], tolerance_mph: float) -> dict:
    found = [r for r in results if r.get("found_sign")]
    return {
        "sample_size": len(results),
        "signs_found": len(found),
        "sign_find_rate": (len(found) / len(results)) if results else None,
        "osm": _source_stats(found, "speed_limit_osm_mph", tolerance_mph),
        "here": _source_stats(found, "speed_limit_here_mph", tolerance_mph),
        "model": _source_stats(found, None, tolerance_mph),
    }


def print_report(stats: dict, api_calls: dict) -> None:
    print("\n=== Precision sample report ===")
    print(f"Sample size: {stats['sample_size']}")
    print(f"Signs found: {stats['signs_found']} "
          f"({stats['sign_find_rate'] * 100:.1f}% of sample)" if stats["sign_find_rate"] is not None else "")
    print(f"Street View calls: {api_calls['streetview']}  Vision OCR calls: {api_calls['vision']}")
    print(f"\n{'Source':<8}{'n':>6}{'Exact match':>14}{'Within tol.':>14}{'Mean |err|':>12}{'Median |err|':>14}")
    for label in ("osm", "here", "model"):
        s = stats[label]
        exact_pct = f"{s['exact_match_rate'] * 100:.1f}%" if s["exact_match_rate"] is not None else "n/a"
        tol_pct = f"{s['within_tolerance_rate'] * 100:.1f}%" if s["within_tolerance_rate"] is not None else "n/a"
        mean_err = f"{s['mean_abs_error_mph']:.2f}" if s["mean_abs_error_mph"] is not None else "n/a"
        median_err = f"{s['median_abs_error_mph']:.2f}" if s["median_abs_error_mph"] is not None else "n/a"
        print(f"{label:<8}{s['n_compared']:>6}{exact_pct:>14}{tol_pct:>14}{mean_err:>12}{median_err:>14}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--table", required=True, help="Table name (not dataset-qualified), e.g. speed_limits_CO_2026_08_details")
    p.add_argument("--state", required=True, help="Two-letter state code, e.g. CO - scopes the sample's domain AND "
                   "selects which maps.road_segments_<STATE> topology table to walk back through")
    p.add_argument("--functional-class-lt", type=int, required=True, help="Only sample segments with functional_class < this value, e.g. 3")
    p.add_argument("--sample-size", type=int, required=True, help=f"How many segments to sample (hard ceiling: {MAX_SAMPLE_SIZE})")
    p.add_argument("--max-walk-back", type=int, default=DEFAULT_MAX_WALK_BACK,
                   help=f"How many segments to walk back (against the assumed direction of travel) looking for a "
                        f"sign before giving up on a sampled segment (default: {DEFAULT_MAX_WALK_BACK})")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"How many sampled segments to process concurrently (default: {DEFAULT_CONCURRENCY}, max {MAX_CONCURRENCY}) "
                        f"- Street View calls are still globally throttled regardless of this value")
    p.add_argument("--tolerance-mph", type=float, default=0.0,
                   help="In addition to the exact-match rate (always reported), also report the rate each source "
                        "is within this many mph of the real sign (default: 0, i.e. same as exact)")
    p.add_argument("--project", default=DEFAULT_PROJECT, help=f"BigQuery project (default: {DEFAULT_PROJECT})")
    p.add_argument("--dataset", default=DEFAULT_DATASET, help=f"BigQuery dataset holding --table (default: {DEFAULT_DATASET})")
    p.add_argument("--out-dir", default="precision_sample_output", help="Directory to write Street View images and results into (default: ./precision_sample_output)")
    p.add_argument("--out-json", default=None, type=Path, help="Path for the full JSON results+stats file (default: <out-dir>/<run>/results.json)")
    p.add_argument("--out-csv", default=None, type=Path, help="Path for the flat per-segment CSV (default: <out-dir>/<run>/results.csv)")
    p.add_argument("--walk-segment", action="store_true",
                   help="At each segment checked (sampled or walked-back), sample several positions along its full "
                        "length instead of just its centroid - same flag/meaning as find_bad_speed_limit.py")
    p.add_argument("--walk-segment-spacing-m", type=float, default=15.0, help="See find_bad_speed_limit.py --walk-segment-spacing-m (default: 15)")
    p.add_argument("--side-mode", choices=SIDE_MODES, default="center", help="See find_bad_speed_limit.py --side-mode (default: center)")
    p.add_argument("--side-offset-m", type=float, default=20.0, help="See find_bad_speed_limit.py --side-offset-m (default: 20)")
    p.add_argument("--auto-side-offset", action="store_true", help="See find_bad_speed_limit.py --auto-side-offset")
    p.add_argument("--headings", type=parse_headings, default=DEFAULT_HEADINGS,
                   help=f"Comma-separated headings to capture per position (default: {','.join(str(h) for h in DEFAULT_HEADINGS)})")
    p.add_argument("--headings-relative", action="store_true", help="See find_bad_speed_limit.py --headings-relative")
    p.add_argument("--fov", type=int, default=DEFAULT_FOV, help=f"Street View field of view, {MIN_FOV}-{MAX_FOV} (default: {DEFAULT_FOV})")
    p.add_argument("--keys-file", default=DEFAULT_KEYS_FILE, type=Path, help=f"KEY=VALUE file for API keys (default: {DEFAULT_KEYS_FILE})")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    load_keys_file(args.keys_file)
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key or api_key == "your-key-here":
        print(f"ERROR: No usable GOOGLE_MAPS_API_KEY. Set it in the environment, or add a line "
              f"GOOGLE_MAPS_API_KEY=... to {args.keys_file}.", file=sys.stderr)
        return 1

    try:
        _validate_identifier(args.project, PROJECT_RE, "project")
        _validate_identifier(args.dataset, DATASET_RE, "dataset")
        _validate_identifier(args.table, TABLE_RE, "table")
        _validate_identifier(args.state.upper(), STATE_RE, "state")
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not (MIN_FOV <= args.fov <= MAX_FOV):
        print(f"ERROR: --fov must be between {MIN_FOV} and {MAX_FOV}.", file=sys.stderr)
        return 1
    if args.sample_size < 1:
        print("ERROR: --sample-size must be at least 1.", file=sys.stderr)
        return 1
    sample_size = min(args.sample_size, MAX_SAMPLE_SIZE)
    if sample_size < args.sample_size:
        print(f"Note: --sample-size {args.sample_size} exceeds the hard ceiling of {MAX_SAMPLE_SIZE} - clamped.")
    concurrency = max(1, min(args.concurrency, MAX_CONCURRENCY))
    max_walk_back = max(0, args.max_walk_back)

    run_label = f"{args.state.upper()}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_root = Path(args.out_dir) / run_label
    out_root.mkdir(parents=True, exist_ok=True)
    out_json_path = args.out_json or (out_root / "results.json")
    out_csv_path = args.out_csv or (out_root / "results.csv")

    print(f"Sampling {sample_size} segment(s) from `{args.project}.{args.dataset}.{args.table}` "
          f"(state={args.state.upper()}, functional_class<{args.functional_class_lt}), weighted by segment length...")
    try:
        sampled_rows = sample_segments_weighted_by_length(
            args.project, args.dataset, args.table,
            state=args.state, functional_class_lt=args.functional_class_lt, sample_size=sample_size,
        )
    except Exception as e:
        print(f"ERROR: Sampling query failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if not sampled_rows:
        print("No segments matched the sampling criteria (check --state/--functional-class-lt/--table).", file=sys.stderr)
        return 1
    print(f"Sampled {len(sampled_rows)} segment(s). Looking for speed-limit signs "
          f"(walking back up to {max_walk_back} segment(s) when a sampled segment has none)...\n")

    vision_client = vision.ImageAnnotatorClient()
    reset_api_call_counts()
    results: list[dict] = []
    n = len(sampled_rows)
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(
                    _process_sampled_segment, i, n, row,
                    project=args.project, dataset=args.dataset, table=args.table, state=args.state,
                    max_walk_back=max_walk_back, api_key=api_key, vision_client=vision_client, out_root=out_root,
                    walk_segment=args.walk_segment, walk_segment_spacing_m=args.walk_segment_spacing_m,
                    side_mode=args.side_mode, side_offset_m=args.side_offset_m, auto_side_offset=args.auto_side_offset,
                    headings=args.headings, headings_relative=args.headings_relative, fov=args.fov,
                    log=lambda msg: None,
                ): i
                for i, row in enumerate(sampled_rows)
            }
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:
                    result = {
                        "segment_id": sampled_rows[i]["here_segment_id"], "status": "error",
                        "found_sign": False, "ground_truth_mph": None, "note": f"{type(e).__name__}: {e}",
                    }
                results.append(result)
                outcome = f"{result['status']}" + (f" ({result.get('ground_truth_mph')} mph)" if result.get("found_sign") else "")
                print(f"  [{len(results)}/{n}] {result.get('segment_id')}: {outcome}")
    except StreetViewAuthError as e:
        print(f"\nERROR: {e}\nThis means the API key/billing/API-enablement is broken, not that there's no "
              f"imagery. Check GOOGLE_MAPS_API_KEY and that billing/Street View Static API are enabled.", file=sys.stderr)
        return 1
    except NoUsableApiKey as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1

    stats = compute_precision_stats(results, tolerance_mph=args.tolerance_mph)
    api_calls = get_api_call_counts()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "project": args.project, "dataset": args.dataset, "table": args.table, "state": args.state.upper(),
            "functional_class_lt": args.functional_class_lt,
            "sample_size_requested": args.sample_size, "sample_size_actual": len(sampled_rows),
            "max_walk_back": max_walk_back, "tolerance_mph": args.tolerance_mph, "concurrency": concurrency,
            "walk_segment": args.walk_segment, "walk_segment_spacing_m": args.walk_segment_spacing_m,
            "side_mode": args.side_mode, "side_offset_m": args.side_offset_m, "auto_side_offset": args.auto_side_offset,
            "headings": list(args.headings), "headings_relative": args.headings_relative, "fov": args.fov,
        },
        "api_calls": api_calls,
        "results": results,
        "stats": stats,
    }
    out_json_path.write_text(json.dumps(payload, indent=2, default=str))
    write_results_csv(out_csv_path, results)

    print_report(stats, api_calls)
    print(f"\nFull results: {out_json_path}")
    print(f"CSV: {out_csv_path}")
    print(f"Images: {out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
