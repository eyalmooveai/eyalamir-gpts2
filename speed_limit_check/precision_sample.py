#!/usr/bin/env python3
"""Sample road segments at random (weighted by length) among those where OSM,
HERE, and our own inferred ("Moove") speed limit do NOT all three agree,
within a functional-class range, find the real-world posted speed limit for
each one by reading a Street View sign - walking up to a few segments in BOTH
directions of travel when the sampled segment itself has none - and report
how often OSM, HERE, and our own inferred speed limit actually match that
ground truth. Also infers each source's OVERALL precision across the FULL
domain (not just the disagreeing segments actually sampled), under an
explicit, stated assumption - see "Inferring overall precision" below.

This is a PRECISION/ACCURACY EVAL, not a mismatch-*finder* in find_bad_speed_limit.py's
sense: that module (and the Evaluator built on it) picks candidates to CHECK
by a narrower rule - HERE and OSM must already agree with EACH OTHER, and the
inferred value must then differ from that agreed value by at least a
configurable threshold (see CRITERIA_DEFS's here_osm_agree/infer_vs_here) -
tuned to surface likely model errors, not to measure a rate. This script's own
sampling frame is any pairwise disagreement among OSM/HERE/Moove (no magnitude
threshold) - a different, larger population - and within THAT population it
still samples uniformly at random, weighted by each segment's own length
(ST_LENGTH(geom), in meters) so a long highway segment isn't counted the same
as a 20m connector, rather than just taking the biggest disagreements first.

Inferring overall precision (explicit, directed design - not something this
script can verify, only assume): segments where OSM/HERE/Moove already all
three agree are NEVER sampled (there's nothing to adjudicate), so on their
own, this script's stats describe precision *conditioned on disagreement*,
not blanket network-wide accuracy. To get the latter, run_precision_sample()
also queries the domain's agree/disagree population split (population_agreement_counts())
and combines it with this run's own empirically-measured disagreement-only
precision (compute_precision_stats()'s exact_match_rate) via the law of total
probability, UNDER THE ASSUMPTION that whenever all three sources already
agree, they are in fact right:
    P(source correct) = P(all agree)*1 + P(disagree)*P(source correct | disagreement)
(see infer_overall_precision()). This assumption is a real, stated leap, not
a measured fact - it cannot detect the case where OSM/HERE/Moove are
correlated-wrong together (e.g. a shared stale upstream classification) while
still agreeing with each other. If that blind spot ever needs to be checked
directly, it needs an unconditional sample across the FULL domain (agreement
included), which would mean dropping the disagreement condition from
sample_segments_weighted_by_length()'s WHERE clause for that one run - not a
permanent change to this script's own default sampling frame.

Built on top of find_bad_speed_limit.py's existing pipeline - it does NOT
reimplement the Street View / Vision OCR mechanics:
  - _process_one_candidate() is the exact same "walk this segment's positions,
    look for a sign, OCR it" unit of work the Evaluator and Sign Checker use.
  - fetch_candidate_by_id() is the exact same "look up one here_segment_id's
    full row" used when walking to a neighboring segment in either direction.
  - The global Street View throttle (_streetview_throttle_lock) already
    serializes every worker thread's calls regardless of --concurrency, same
    as batch_evaluator.py relies on.

Walking across segments (new - nothing in this app's existing code has a
notion of segment-to-segment topology): calc_out's own speed_limits_*_details
tables have no from/to-node or adjacency column at all (confirmed live via
INFORMATION_SCHEMA). The real HERE street-network topology lives in a
DIFFERENT, currently-unused-by-this-app dataset: `maps.road_segments_<STATE>`
carries `start_node_ref`/`end_node_ref` HERE node IDs per segment (joinable to
calc_out's `here_segment_id` via its own `id` column, same
"here:cm:segment:<digits>" format - confirmed by a live round-trip lookup).

HERE's digitized line direction for a segment isn't guaranteed to match
real/legal direction of travel (see run_pipeline's own docstring in
find_bad_speed_limit.py) - so rather than assume one direction is "forward"
and only walk that way, this script checks the sampled segment itself first
(a hit there answers for both directions of travel - our sign-detection has
no way to attribute a sign found directly on the segment to one approach
direction over the other), and only if that finds nothing does it walk BOTH
directions independently, up to --max-walk-back segments each, reporting the
first sign found in each direction separately:
  - "backward": predecessor.end_node_ref = X.start_node_ref (segments that
    feed INTO X's digitized start - the approach for someone traveling in
    the segment's own digitized direction).
  - "forward": successor.start_node_ref = X.end_node_ref (segments leading
    OUT of X's digitized end - the approach for someone traveling the
    OPPOSITE of the segment's digitized direction).
These two directions can and sometimes do carry different posted limits
(the report below counts how often that happens among segments where both
were found) - that's a real property of the road, not a bug in the walk.
At a branching intersection with more than one neighbor in a given
direction, this picks the one with the same street_name as the current
segment first (continuing the same road), falling back to the one with the
same functional_class, then just the first one found - logged as a note
either way, since this is a heuristic, not a certainty.

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
    client: "bigquery.Client",
) -> list[bigquery.table.Row]:
    """Picks `sample_size` segments uniformly at random FROM AMONG THOSE WHERE
    OSM, HERE, and Moove's inferred value do NOT all three agree (any pairwise
    disagreement, no magnitude threshold - see module docstring for why this
    is a different, narrower frame than find_bad_speed_limit.py's own
    candidate criteria, and for how this combines with the "if all agree,
    assume correct" assumption to infer overall precision), weighted by each
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
    noisy km/h->mph conversion, not a clean posted-sign value; the agreement
    check uses that same rounded value so a segment isn't counted as
    "disagreeing" purely from HERE's km/h->mph conversion noise. Moove's own
    value prefers speed_limit_infer_mph_corrected, falling back to
    speed_limit_infer_mph when the corrected column is null - the same
    precedence _source_value(..., None) uses for the "model" column
    everywhere else in this module, kept in sync deliberately. A segment
    missing any of the three values can't be said to agree OR disagree (SQL's
    NULL comparisons are NULL, not TRUE/FALSE) and so is naturally excluded -
    same population population_agreement_counts() reports as "missing".

    Takes an already-constructed `client` rather than making its own - see
    run_precision_sample()'s own note on why: this module can issue dozens
    of BigQuery calls per sampled segment across several concurrent worker
    threads, and a fresh bigquery.Client() per call was confirmed (a real
    macOS crash, "multi-threaded process forked ... crashed on child side
    of fork pre-exec") to multiply how often a concurrent credential/mTLS
    subprocess check races against gRPC's own background threads - one
    shared, reused client cuts that down by roughly the same factor."""
    query = f"""
        SELECT
          * EXCEPT (geom) REPLACE (ROUND(speed_limit_here_mph) AS speed_limit_here_mph),
          ST_Y(ST_CENTROID(geom)) AS centroid_lat,
          ST_X(ST_CENTROID(geom)) AS centroid_lon,
          ST_ASGEOJSON(geom) AS geom_geojson,
          LN(RAND()) / GREATEST(ST_LENGTH(geom), 1.0) AS _sample_key
        FROM `{project}.{dataset}.{table}`
        WHERE functional_class < @fc_lt AND state = @state
          AND NOT (
            speed_limit_osm_mph = ROUND(speed_limit_here_mph)
            AND ROUND(speed_limit_here_mph) = COALESCE(speed_limit_infer_mph_corrected, speed_limit_infer_mph)
          )
        ORDER BY _sample_key DESC
        LIMIT @sample_size
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("fc_lt", "INT64", functional_class_lt),
        bigquery.ScalarQueryParameter("state", "STRING", state.upper()),
        bigquery.ScalarQueryParameter("sample_size", "INT64", sample_size),
    ])
    return list(client.query(query, job_config=job_config).result())


def population_agreement_counts(
    project: str, dataset: str, table: str, *, state: str, functional_class_lt: int, client: "bigquery.Client",
) -> dict:
    """One cheap COUNT query (metadata-scale, no imagery/API cost) over the
    SAME domain sample_segments_weighted_by_length() samples from
    (functional_class < fc_lt, state) - classifies every segment into
    "agree" (OSM, HERE, and Moove all three equal - same agreement check as
    the sampling query's own WHERE clause), "disagree" (the sampling query's
    actual population - at least one pairwise difference), or "missing" (any
    of the three values is null, so agreement can't be judged either way).
    This is what turns this run's own disagreement-only empirical precision
    into an inferred OVERALL-domain precision - see infer_overall_precision()
    and the module docstring's "Inferring overall precision" section for the
    formula and the explicit assumption it rests on."""
    query = f"""
        SELECT
          COUNTIF(speed_limit_osm_mph IS NULL OR speed_limit_here_mph IS NULL
                  OR COALESCE(speed_limit_infer_mph_corrected, speed_limit_infer_mph) IS NULL) AS missing_count,
          COUNTIF(speed_limit_osm_mph IS NOT NULL AND speed_limit_here_mph IS NOT NULL
                  AND COALESCE(speed_limit_infer_mph_corrected, speed_limit_infer_mph) IS NOT NULL
                  AND speed_limit_osm_mph = ROUND(speed_limit_here_mph)
                  AND ROUND(speed_limit_here_mph) = COALESCE(speed_limit_infer_mph_corrected, speed_limit_infer_mph)) AS agree_count,
          COUNTIF(speed_limit_osm_mph IS NOT NULL AND speed_limit_here_mph IS NOT NULL
                  AND COALESCE(speed_limit_infer_mph_corrected, speed_limit_infer_mph) IS NOT NULL
                  AND NOT (speed_limit_osm_mph = ROUND(speed_limit_here_mph)
                           AND ROUND(speed_limit_here_mph) = COALESCE(speed_limit_infer_mph_corrected, speed_limit_infer_mph))) AS disagree_count
        FROM `{project}.{dataset}.{table}`
        WHERE functional_class < @fc_lt AND state = @state
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("fc_lt", "INT64", functional_class_lt),
        bigquery.ScalarQueryParameter("state", "STRING", state.upper()),
    ])
    row = next(iter(client.query(query, job_config=job_config).result()))
    return {"missing_count": row["missing_count"], "agree_count": row["agree_count"], "disagree_count": row["disagree_count"]}


def infer_overall_precision(population: dict, stats: dict) -> dict:
    """Combines population_agreement_counts()'s domain-wide agree/disagree
    split with this run's own empirically-measured disagreement-only
    precision (compute_precision_stats()'s per-source exact_match_rate) into
    an inferred OVERALL precision for each source across the FULL domain -
    under the explicit, stated assumption that a segment where OSM/HERE/Moove
    already all agree is in fact correct (see module docstring - this is a
    real assumption, never verified by this script itself). Law of total
    probability over the two-population split:
        P(source correct) = P(all agree)*1 + P(disagree)*P(source correct | disagreement)
    "missing" segments (can't be judged either way) are excluded from the
    denominator, same as every per-source comparison elsewhere in this module
    already excludes a missing value rather than guessing. Returns None for
    a source whose disagreement-sample exact_match_rate is itself None (no
    comparable observations found in this run)."""
    agree, disagree = population["agree_count"], population["disagree_count"]
    total = agree + disagree
    if total == 0:
        return {"osm": None, "here": None, "model": None}
    agree_frac = agree / total
    disagree_frac = disagree / total
    result = {}
    for label in ("osm", "here", "model"):
        rate = stats[label]["exact_match_rate"]
        result[label] = (agree_frac * 1.0 + disagree_frac * rate) if rate is not None else None
    return result


def _maps_table_for_state(state: str) -> str:
    table = f"road_segments_{state.upper()}"
    _validate_identifier(table, TABLE_RE, "maps table")
    return table


def _lookup_segment_topology(project: str, state: str, segment_ids: list[str], *, client: "bigquery.Client") -> dict[str, dict]:
    """start_node_ref/end_node_ref/street_name/functional_class for a batch
    of here_segment_id values, from maps.road_segments_<STATE> - the HERE
    street-network topology table calc_out itself doesn't carry (see module
    docstring). Returns {segment_id: row_dict}; a segment_id not present in
    the result just wasn't found in this state's topology table (e.g. it's
    right at a state border, or the two datasets' coverage doesn't line up
    perfectly) - callers treat that as "can't walk back from here," not an
    error. Takes an already-constructed `client` - see
    sample_segments_weighted_by_length's docstring for why."""
    if not segment_ids:
        return {}
    table = _maps_table_for_state(state)
    query = f"""
        SELECT id AS here_segment_id, start_node_ref, end_node_ref, street_name, functional_class
        FROM `{project}.{MAPS_DATASET}.{table}`
        WHERE id IN UNNEST(@ids)
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", segment_ids)])
    rows = client.query(query, job_config=job_config).result()
    return {r["here_segment_id"]: dict(r.items()) for r in rows}


def _find_neighbor_segments(
    project: str, state: str, node_ref: Optional[str], *, exclude_segment_id: str, match_column: str, client: "bigquery.Client",
) -> list[dict]:
    """Segments adjacent to `node_ref` in the direction `match_column`
    implies: match_column="end_node_ref" finds PREDECESSORS (segments that
    feed INTO node_ref - the "backward" direction below); match_column=
    "start_node_ref" finds SUCCESSORS (segments leading OUT of node_ref -
    "forward"). match_column is always one of those two fixed literals from
    this module's own code, never user input, so it's safe to interpolate.
    Takes an already-constructed `client` - see
    sample_segments_weighted_by_length's docstring for why."""
    if not node_ref:
        return []
    assert match_column in ("start_node_ref", "end_node_ref")
    table = _maps_table_for_state(state)
    query = f"""
        SELECT id AS here_segment_id, start_node_ref, end_node_ref, street_name, functional_class
        FROM `{project}.{MAPS_DATASET}.{table}`
        WHERE {match_column} = @node AND id != @exclude_id
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("node", "STRING", node_ref),
        bigquery.ScalarQueryParameter("exclude_id", "STRING", exclude_segment_id),
    ])
    return [dict(r.items()) for r in client.query(query, job_config=job_config).result()]


def _pick_neighbor(neighbors: list[dict], *, street_name: Optional[str], functional_class: Optional[int]) -> Optional[dict]:
    """At a branching intersection (more than one segment adjacent to the
    same node), prefer the one continuing the same named road, then the one
    with the same functional_class, else just the first candidate - a
    heuristic, not a guarantee (see module docstring)."""
    if not neighbors:
        return None
    if street_name:
        same_name = [p for p in neighbors if p.get("street_name") == street_name]
        if same_name:
            return same_name[0]
    if functional_class is not None:
        same_fc = [p for p in neighbors if p.get("functional_class") == functional_class]
        if same_fc:
            return same_fc[0]
    return neighbors[0]


# direction -> (which node field of the CURRENT segment to branch from, which
# column of a CANDIDATE neighbor must match that node). "backward" walks
# against the segment's own digitized direction (predecessors); "forward"
# walks with it (successors). See module docstring for why both are walked
# rather than assuming one is the real direction of travel.
_DIRECTION_NODE_FIELDS = {
    "backward": ("start_node_ref", "end_node_ref"),
    "forward": ("end_node_ref", "start_node_ref"),
}


def _walk_chain(project: str, state: str, start_topo: dict, max_steps: int, direction: str, *, client: "bigquery.Client") -> list[dict]:
    """Up to `max_steps` neighbor topology rows in `direction`, NOT
    including start_topo itself. Stops early at a dead end (no neighbor
    found) or if a segment already walked would be revisited (guards
    against an accidental loop in the graph). Takes an already-constructed
    `client` - see sample_segments_weighted_by_length's docstring for why."""
    own_node_field, neighbor_match_column = _DIRECTION_NODE_FIELDS[direction]
    chain: list[dict] = []
    visited = {start_topo["here_segment_id"]}
    current = start_topo
    while len(chain) < max_steps:
        neighbors = _find_neighbor_segments(
            project, state, current.get(own_node_field), exclude_segment_id=current["here_segment_id"], match_column=neighbor_match_column,
            client=client,
        )
        nxt = _pick_neighbor(neighbors, street_name=current.get("street_name"), functional_class=current.get("functional_class"))
        if not nxt or nxt["here_segment_id"] in visited:
            break
        chain.append(nxt)
        visited.add(nxt["here_segment_id"])
        current = nxt
    return chain


def _check_segment_for_sign(
    step_index: int, n: int, row, *, api_key: str, vision_client, sample_out_root: Path,
    walk_segment: bool, walk_segment_spacing_m: float, side_mode: str, side_offset_m: float, auto_side_offset: bool,
    headings: tuple[int, ...], headings_relative: bool, fov: int, log,
):
    """Thin, shared wrapper around _process_one_candidate - the exact same
    "walk this segment's positions, look for a sign, OCR it" logic the
    Evaluator/Sign Checker use, reused unchanged for every segment this
    script ever checks (the sampled one or a walked neighbor)."""
    return _process_one_candidate(
        step_index, n, row, api_key=api_key, vision_client=vision_client, out_root=sample_out_root,
        walk_segment=walk_segment, walk_segment_spacing_m=walk_segment_spacing_m,
        side_mode=side_mode, side_offset_m=side_offset_m, auto_side_offset=auto_side_offset,
        headings=headings, headings_relative=headings_relative, fov=fov,
        log=log, progress=lambda fraction, message: None,
    )


def _empty_direction_result(note: str, steps_available: int) -> dict:
    return {
        "found": False, "ground_truth_mph": None, "sign_lat": None, "sign_lon": None,
        "sign_evidence": None, "streetview_capture_date": None, "annotated_image": None,
        "matched_segment_id": None, "steps_walked": steps_available, "note": note,
    }


def _walk_direction_for_sign(
    chain: list[dict], *, project: str, dataset: str, table: str, direction: str, tag: str,
    api_key: str, vision_client, sample_out_root: Path,
    walk_segment: bool, walk_segment_spacing_m: float, side_mode: str, side_offset_m: float, auto_side_offset: bool,
    headings: tuple[int, ...], headings_relative: bool, fov: int, log,
) -> dict:
    """Walks one direction's chain of neighbor segments, stopping at the
    first readable sign. Returns a dict shaped for this script's own result/
    CSV - never the raw CandidateAttempt/Match objects find_bad_speed_limit.py
    uses internally, since a direction's outcome here also needs to carry
    which segment actually had the sign and how many steps it took."""
    if not chain:
        return _empty_direction_result(f"no {direction} neighbor segments found", 0)
    for step_index, step_topo in enumerate(chain, start=1):
        step_seg_id = step_topo["here_segment_id"]
        rows = fetch_candidate_by_id(project, dataset, table, step_seg_id)
        if not rows:
            log(f"{tag} [{direction}] step {step_index} ({step_seg_id}): not found in {dataset}.{table} - skipping.")
            continue
        attempt, match = _check_segment_for_sign(
            step_index, len(chain), rows[0], api_key=api_key, vision_client=vision_client, sample_out_root=sample_out_root,
            walk_segment=walk_segment, walk_segment_spacing_m=walk_segment_spacing_m,
            side_mode=side_mode, side_offset_m=side_offset_m, auto_side_offset=auto_side_offset,
            headings=headings, headings_relative=headings_relative, fov=fov, log=log,
        )
        if match:
            sign = match.image_details[match.matched_image_index]
            return {
                "found": True, "ground_truth_mph": match.reading.speed_mph,
                "sign_lat": sign.lat, "sign_lon": sign.lon, "sign_evidence": match.reading.evidence,
                "streetview_capture_date": sign.capture_date, "annotated_image": str(match.annotated_image),
                "matched_segment_id": step_seg_id, "steps_walked": step_index, "note": "",
            }
    return _empty_direction_result(f"no readable sign in {len(chain)} {direction} segment(s)", len(chain))


def _process_sampled_segment(
    i: int, n: int, sampled_row: bigquery.table.Row, *,
    project: str, dataset: str, table: str, state: str, max_walk_back: int,
    api_key: str, vision_client, bq_client: "bigquery.Client", out_root: Path,
    walk_segment: bool, walk_segment_spacing_m: float,
    side_mode: str, side_offset_m: float, auto_side_offset: bool,
    headings: tuple[int, ...], headings_relative: bool, fov: int,
    log=lambda msg: None,
) -> dict:
    """Runs the full per-sample pipeline. First checks the sampled segment
    itself - a hit there answers for BOTH directions of travel, since this
    app's sign-detection can't attribute a sign found directly on a segment
    to one approach direction over the other. Only if that finds nothing
    does it walk forward and backward independently (see module docstring
    for why both, not one assumed "forward"), up to max_walk_back segments
    each, reporting the first sign found in each direction separately.
    Never raises for an ordinary "no sign anywhere" outcome, only for a
    real error (the caller decides how to handle that)."""
    seg_id = sampled_row["here_segment_id"]
    tag = f"[{i + 1}/{n}] {seg_id}"
    sample_out_root = out_root / safe_segment_dirname(seg_id)

    base_result = {
        "segment_id": seg_id,
        "state": state.upper(),
        "street_name": sampled_row.get("street_name"),
        "functional_class": sampled_row.get("functional_class"),
        "centroid_lat": sampled_row.get("centroid_lat"),
        "centroid_lon": sampled_row.get("centroid_lon"),
        "speed_limit_osm_mph": sampled_row.get("speed_limit_osm_mph"),
        "speed_limit_here_mph": sampled_row.get("speed_limit_here_mph"),
        "speed_limit_infer_mph": sampled_row.get("speed_limit_infer_mph"),
        "speed_limit_infer_mph_corrected": sampled_row.get("speed_limit_infer_mph_corrected"),
        "speed_AVG_mph": sampled_row.get("speed_AVG_mph"),
        "freeflow_mph": sampled_row.get("freeflow_mph"),
    }

    attempt0, match0 = _check_segment_for_sign(
        0, 1, sampled_row, api_key=api_key, vision_client=vision_client, sample_out_root=sample_out_root,
        walk_segment=walk_segment, walk_segment_spacing_m=walk_segment_spacing_m,
        side_mode=side_mode, side_offset_m=side_offset_m, auto_side_offset=auto_side_offset,
        headings=headings, headings_relative=headings_relative, fov=fov, log=log,
    )
    if match0:
        sign = match0.image_details[match0.matched_image_index]
        log(f"{tag}: sign found directly on the sampled segment - {match0.reading.speed_mph} mph (applies to both directions).")
        return {
            **base_result,
            "found_on_segment": True, "ground_truth_mph": match0.reading.speed_mph,
            "sign_lat": sign.lat, "sign_lon": sign.lon, "sign_evidence": match0.reading.evidence,
            "streetview_capture_date": sign.capture_date, "annotated_image": str(match0.annotated_image),
            "forward": None, "backward": None,
            "segments_tried": [seg_id], "status": "match_on_segment", "note": "",
        }

    topo_by_id = _lookup_segment_topology(project, state, [seg_id], client=bq_client)
    start_topo = topo_by_id.get(seg_id)
    segments_tried = [seg_id]
    if start_topo is None:
        log(f"{tag}: no sign on the segment itself, and not found in maps.road_segments_{state.upper()} - cannot walk either direction.")
        directions = {d: _empty_direction_result("no topology row - could not walk", 0) for d in ("forward", "backward")}
    else:
        directions = {}
        for direction in ("forward", "backward"):
            chain = _walk_chain(project, state, start_topo, max_walk_back, direction, client=bq_client)
            log(f"{tag} [{direction}]: {len(chain)} neighbor segment(s) to check"
                + (f" ({', '.join(s['here_segment_id'] for s in chain)})" if chain else ""))
            directions[direction] = _walk_direction_for_sign(
                chain, project=project, dataset=dataset, table=table, direction=direction, tag=tag,
                api_key=api_key, vision_client=vision_client, sample_out_root=sample_out_root,
                walk_segment=walk_segment, walk_segment_spacing_m=walk_segment_spacing_m,
                side_mode=side_mode, side_offset_m=side_offset_m, auto_side_offset=auto_side_offset,
                headings=headings, headings_relative=headings_relative, fov=fov, log=log,
            )
            segments_tried.extend(s["here_segment_id"] for s in chain)

    found_forward = directions["forward"]["found"]
    found_backward = directions["backward"]["found"]
    status = (
        "match_both" if found_forward and found_backward else
        "match_forward_only" if found_forward else
        "match_backward_only" if found_backward else
        "no_sign"
    )
    return {
        **base_result,
        "found_on_segment": False, "ground_truth_mph": None,
        "sign_lat": None, "sign_lon": None, "sign_evidence": None, "streetview_capture_date": None, "annotated_image": None,
        "forward": directions["forward"], "backward": directions["backward"],
        "segments_tried": segments_tried, "status": status,
        "note": f"on-segment attempt: {attempt0.status}",
    }


def _streetview_url(lat, lon) -> str:
    return f"https://www.google.com/maps?q=&layer=c&cbll={lat},{lon}" if lat is not None and lon is not None else ""


CSV_FIELDNAMES = [
    "segment_id", "state", "street_name", "functional_class", "status",
    "speed_limit_osm_mph", "speed_limit_here_mph", "speed_limit_infer_mph", "speed_limit_infer_mph_corrected",
    "speed_AVG_mph", "freeflow_mph", "centroid_lat", "centroid_lon",
    "found_on_segment", "ground_truth_mph", "sign_lat", "sign_lon", "streetview_url",
    "forward_found", "forward_ground_truth_mph", "forward_matched_segment_id", "forward_steps_walked",
    "backward_found", "backward_ground_truth_mph", "backward_matched_segment_id", "backward_steps_walked",
    "segments_tried", "note",
]


def _result_row_for_export(r: dict) -> dict:
    """Flat, export-ready view of one segment's result, keyed exactly by
    CSV_FIELDNAMES - the single source of truth for how the nested
    forward/backward dicts become their own columns, shared by
    write_results_csv() (CSV for a human) and save_results_to_bigquery()
    (JSON rows for the calc_tmp archive table), so the two never drift."""
    fwd = r.get("forward") or {}
    bwd = r.get("backward") or {}
    row = dict(r)
    row["streetview_url"] = _streetview_url(r.get("sign_lat") or r.get("centroid_lat"), r.get("sign_lon") or r.get("centroid_lon"))
    row["segments_tried"] = ",".join(r.get("segments_tried") or [])
    row["forward_found"] = fwd.get("found")
    row["forward_ground_truth_mph"] = fwd.get("ground_truth_mph")
    row["forward_matched_segment_id"] = fwd.get("matched_segment_id")
    row["forward_steps_walked"] = fwd.get("steps_walked")
    row["backward_found"] = bwd.get("found")
    row["backward_ground_truth_mph"] = bwd.get("ground_truth_mph")
    row["backward_matched_segment_id"] = bwd.get("matched_segment_id")
    row["backward_steps_walked"] = bwd.get("steps_walked")
    return {k: row.get(k) for k in CSV_FIELDNAMES}


def write_results_csv(path: Path, results: list[dict]) -> None:
    """One row per sampled segment - forward/backward readings flattened
    into their own columns rather than one row per direction, so the CSV
    stays easy to skim; the JSON alongside it has the full per-direction
    detail (sign_evidence, annotated_image, etc.) for anything deeper."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv_module.DictWriter(f, fieldnames=CSV_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            writer.writerow(_result_row_for_export(r))


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


def _observation_row(r: dict, *, direction: str, ground_truth_mph: float, matched_segment_id, steps_walked, sign_lat, sign_lon) -> dict:
    """One self-contained segment-direction row - every source's value
    alongside the real sign reading, suitable for direct display/export
    (print_observations_table/write_observations_csv) as well as the
    precision stats (_source_stats only ever reads ground_truth_mph plus
    the speed_limit_* columns, all present here)."""
    return {
        "segment_id": r["segment_id"], "direction": direction,
        "street_name": r.get("street_name"), "functional_class": r.get("functional_class"),
        "ground_truth_mph": ground_truth_mph,
        "matched_segment_id": matched_segment_id, "steps_walked": steps_walked,
        "sign_lat": sign_lat, "sign_lon": sign_lon,
        "speed_limit_osm_mph": r.get("speed_limit_osm_mph"),
        "speed_limit_here_mph": r.get("speed_limit_here_mph"),
        "speed_limit_infer_mph": r.get("speed_limit_infer_mph"),
        "speed_limit_infer_mph_corrected": r.get("speed_limit_infer_mph_corrected"),
        "speed_AVG_mph": r.get("speed_AVG_mph"), "freeflow_mph": r.get("freeflow_mph"),
    }


def _observations(results: list[dict]) -> list[dict]:
    """Flattens each sampled segment into 0-2 ground-truth observations:
    ONE shared observation (direction="both") when the sign was found
    directly on the sampled segment (our detection can't attribute that to
    one approach direction, so it answers for both and isn't double-
    counted), or up to one observation per direction (forward/backward)
    when the segment itself had none and a walk in that direction found
    something. This is the actual table the precision stats are computed
    from - also what print_observations_table()/write_observations_csv()
    show/export directly, one row per segment-direction with a real sign."""
    obs = []
    for r in results:
        if r.get("found_on_segment"):
            obs.append(_observation_row(
                r, direction="both", ground_truth_mph=r["ground_truth_mph"],
                matched_segment_id=r["segment_id"], steps_walked=0, sign_lat=r.get("sign_lat"), sign_lon=r.get("sign_lon"),
            ))
            continue
        for direction in ("forward", "backward"):
            d = r.get(direction) or {}
            if d.get("found"):
                obs.append(_observation_row(
                    r, direction=direction, ground_truth_mph=d["ground_truth_mph"],
                    matched_segment_id=d.get("matched_segment_id"), steps_walked=d.get("steps_walked"),
                    sign_lat=d.get("sign_lat"), sign_lon=d.get("sign_lon"),
                ))
    return obs


def compute_precision_stats(results: list[dict], obs: list[dict], tolerance_mph: float) -> dict:
    segments_with_any = [
        r for r in results
        if r.get("found_on_segment") or (r.get("forward") or {}).get("found") or (r.get("backward") or {}).get("found")
    ]
    both_found = [
        r for r in results
        if not r.get("found_on_segment") and (r.get("forward") or {}).get("found") and (r.get("backward") or {}).get("found")
    ]
    disagreeing = [r for r in both_found if r["forward"]["ground_truth_mph"] != r["backward"]["ground_truth_mph"]]
    return {
        "sample_size": len(results),
        "segments_with_any_sign": len(segments_with_any),
        "segment_sign_find_rate": (len(segments_with_any) / len(results)) if results else None,
        "observations": len(obs),
        "segments_with_both_directions_found": len(both_found),
        "direction_disagreement_count": len(disagreeing),
        "direction_disagreement_rate": (len(disagreeing) / len(both_found)) if both_found else None,
        "osm": _source_stats(obs, "speed_limit_osm_mph", tolerance_mph),
        "here": _source_stats(obs, "speed_limit_here_mph", tolerance_mph),
        "model": _source_stats(obs, None, tolerance_mph),
    }


def print_report(stats: dict, api_calls: dict, population: Optional[dict] = None, inferred_precision: Optional[dict] = None, population_error: Optional[str] = None) -> None:
    print("\n=== Precision sample report ===")
    print(f"Sample size: {stats['sample_size']} segment(s)")
    if stats["segment_sign_find_rate"] is not None:
        print(f"Segments with at least one sign found: {stats['segments_with_any_sign']} ({stats['segment_sign_find_rate'] * 100:.1f}%)")
    print(f"Directional observations compared below: {stats['observations']}")
    if stats["segments_with_both_directions_found"]:
        print(f"Segments where BOTH directions were found: {stats['segments_with_both_directions_found']} "
              f"(disagreed on the posted limit: {stats['direction_disagreement_count']}, "
              f"{stats['direction_disagreement_rate'] * 100:.1f}%)")
    print(f"Street View calls: {api_calls['streetview']}  Vision OCR calls: {api_calls['vision']}")
    print(f"\n{'Source':<8}{'n':>6}{'Exact match':>14}{'Within tol.':>14}{'Mean |err|':>12}{'Median |err|':>14}")
    for label in ("osm", "here", "model"):
        s = stats[label]
        exact_pct = f"{s['exact_match_rate'] * 100:.1f}%" if s["exact_match_rate"] is not None else "n/a"
        tol_pct = f"{s['within_tolerance_rate'] * 100:.1f}%" if s["within_tolerance_rate"] is not None else "n/a"
        mean_err = f"{s['mean_abs_error_mph']:.2f}" if s["mean_abs_error_mph"] is not None else "n/a"
        median_err = f"{s['median_abs_error_mph']:.2f}" if s["median_abs_error_mph"] is not None else "n/a"
        print(f"{label:<8}{s['n_compared']:>6}{exact_pct:>14}{tol_pct:>14}{mean_err:>12}{median_err:>14}")

    print("\n=== Inferred overall precision (assumes agreement = correct) ===")
    if population_error:
        print(f"Could not compute: {population_error}")
    elif population and inferred_precision:
        total = population["agree_count"] + population["disagree_count"]
        agree_pct = (population["agree_count"] / total * 100) if total else 0.0
        print(f"Domain: {total} segment(s) with all 3 values present ({population['agree_count']} agree, "
              f"{population['disagree_count']} disagree, {agree_pct:.1f}% agree) "
              f"- {population['missing_count']} more missing a value, excluded.")
        print("Assumes a segment where OSM/HERE/Moove already agree is in fact correct - see module docstring.")
        for label in ("osm", "here", "model"):
            pct = f"{inferred_precision[label] * 100:.1f}%" if inferred_precision[label] is not None else "n/a (no disagreement observations to anchor the estimate)"
            print(f"  {label:<8}{pct}")
    else:
        print("Not computed.")


def _fmt_mph(value: Optional[float]) -> str:
    return f"{value:.0f}" if value is not None else "n/a"


def print_observations_table(observations: list[dict]) -> None:
    """The detail table behind the summary stats above - one row per
    segment-direction that actually found a real sign, with all three
    sources' values side by side so each can be eyeballed directly rather
    than only seen as an aggregate percentage."""
    if not observations:
        print("\n(no segment-direction observations with a real sign found)")
        return
    print(f"\n{'Segment':<28}{'Dir':<9}{'Truth':>6}{'OSM':>6}{'HERE':>6}{'Model':>6}")
    for o in observations:
        model_mph = _source_value(o, None)
        print(
            f"{o['segment_id']:<28}{o['direction']:<9}"
            f"{_fmt_mph(o['ground_truth_mph']):>6}{_fmt_mph(o.get('speed_limit_osm_mph')):>6}"
            f"{_fmt_mph(o.get('speed_limit_here_mph')):>6}{_fmt_mph(model_mph):>6}"
        )


OBSERVATION_CSV_FIELDNAMES = [
    "segment_id", "direction", "street_name", "functional_class",
    "ground_truth_mph", "speed_limit_osm_mph", "speed_limit_here_mph", "model_mph",
    "osm_abs_diff", "here_abs_diff", "model_abs_diff",
    "matched_segment_id", "steps_walked", "sign_lat", "sign_lon", "streetview_url",
]


def _observation_row_for_export(o: dict) -> dict:
    """Flat, export-ready view of one observation, keyed exactly by
    OBSERVATION_CSV_FIELDNAMES - shared by write_observations_csv() and
    save_results_to_bigquery() for the same one-source-of-truth reason as
    _result_row_for_export() above."""
    truth = o["ground_truth_mph"]
    model_mph = _source_value(o, None)
    row = dict(o)
    row["model_mph"] = model_mph
    row["osm_abs_diff"] = abs(o["speed_limit_osm_mph"] - truth) if o.get("speed_limit_osm_mph") is not None else None
    row["here_abs_diff"] = abs(o["speed_limit_here_mph"] - truth) if o.get("speed_limit_here_mph") is not None else None
    row["model_abs_diff"] = abs(model_mph - truth) if model_mph is not None else None
    row["streetview_url"] = _streetview_url(o.get("sign_lat"), o.get("sign_lon"))
    return {k: row.get(k) for k in OBSERVATION_CSV_FIELDNAMES}


def write_observations_csv(path: Path, observations: list[dict]) -> None:
    """One row per segment-direction that actually found a real sign - the
    exact table _source_stats() computes its numbers from, made directly
    inspectable (every source's value AND its abs-diff from the real sign,
    side by side) instead of needing to reconstruct it from the per-segment
    CSV's separate forward_/backward_ columns by hand."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv_module.DictWriter(f, fieldnames=OBSERVATION_CSV_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for o in observations:
            row = _observation_row_for_export(o)
            # CSV wants "" for a missing abs-diff, not a literal "None" string.
            for key in ("osm_abs_diff", "here_abs_diff", "model_abs_diff"):
                if row[key] is None:
                    row[key] = ""
            writer.writerow(row)


def save_results_to_bigquery(
    project: str, run_id: str, config: dict, results: list[dict], observations: list[dict], *,
    client: "bigquery.Client", log=lambda msg: None,
) -> None:
    """Always called at the end of a run (CLI or web) - appends this run's
    segment-level results and segment-direction observations into two
    fixed, cross-run tables in `calc_tmp` (a scratch/staging dataset, not
    this app's own calc_out/calc_archive): calc_tmp.precision_sample_results
    and calc_tmp.precision_sample_observations. Every row carries run_id
    (the same <STATE>_<timestamp> label the output directory/filenames use)
    plus this run's config, so multiple runs over time accumulate into one
    queryable history instead of each run living in its own table.

    Both row shapes are already flat (see _result_row_for_export/
    _observation_row_for_export - the exact same shape as the CSV exports),
    so there's no nested-STRUCT schema-drift risk across appends. Schema is
    autodetected from the first load to each table (CREATE_IF_NEEDED) and
    ALLOW_FIELD_ADDITION is set so a future new column doesn't break the
    append - but autodetect from JSON can still misjudge a column's type if
    every row in a given batch happens to have it null; this is a scratch
    dataset, so that's an accepted, documented limitation rather than
    something this function works around with an explicit schema. Takes an
    already-constructed `client` - see
    sample_segments_weighted_by_length's docstring for why."""
    meta = {
        "run_id": run_id, "generated_at": config["generated_at"],
        "project": config["project"], "dataset": config["dataset"], "table": config["table"],
        "state": config["state"], "functional_class_lt": config["functional_class_lt"],
        "sample_size_requested": config["sample_size_requested"], "sample_size_actual": config["sample_size_actual"],
    }
    result_rows = [{**meta, **_result_row_for_export(r)} for r in results]
    _append_rows_to_bigquery(client, f"{project}.calc_tmp.precision_sample_results", result_rows, log=log)
    observation_rows = [{**meta, **_observation_row_for_export(o)} for o in observations]
    _append_rows_to_bigquery(client, f"{project}.calc_tmp.precision_sample_observations", observation_rows, log=log)


def _append_rows_to_bigquery(client: "bigquery.Client", table_ref: str, rows: list[dict], log=lambda msg: None) -> None:
    if not rows:
        log(f"  No rows to append to {table_ref} - skipping.")
        return
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        autodetect=True,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
        schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
    )
    job = client.load_table_from_json(rows, table_ref, job_config=job_config)
    job.result()
    log(f"  Appended {len(rows)} row(s) to {table_ref}.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--table", required=True, help="Table name (not dataset-qualified), e.g. speed_limits_CO_2026_08_details")
    p.add_argument("--state", required=True, help="Two-letter state code, e.g. CO - scopes the sample's domain AND "
                   "selects which maps.road_segments_<STATE> topology table to walk back through")
    p.add_argument("--functional-class-lt", type=int, required=True, help="Only sample segments with functional_class < this value, e.g. 3")
    p.add_argument("--sample-size", type=int, required=True, help=f"How many segments to sample (hard ceiling: {MAX_SAMPLE_SIZE})")
    p.add_argument("--max-walk-back", type=int, default=DEFAULT_MAX_WALK_BACK,
                   help=f"If a sampled segment has no sign of its own, how many segments to walk in EACH direction "
                        f"(forward and backward - see module docstring for why both) looking for one, before "
                        f"giving up on that direction (default: {DEFAULT_MAX_WALK_BACK})")
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
    p.add_argument("--out-observations-csv", default=None, type=Path,
                   help="Path for the one-row-per-segment-direction CSV (every source's value + abs-diff from the "
                        "real sign, for just the observations with a sign found) - default: <out-dir>/<run>/observations.csv")
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


class SamplingError(ValueError):
    """Raised for any ordinary "bad input/no results" condition in
    run_precision_sample() - a caller (the CLI's main() or a Flask route)
    can catch this one type for a clean user-facing message, same as the
    pre-existing StreetViewAuthError/NoUsableApiKey for the auth case."""


def run_precision_sample(
    *, project: str, dataset: str, table: str, state: str, functional_class_lt: int, sample_size: int,
    max_walk_back: int = DEFAULT_MAX_WALK_BACK, concurrency: int = DEFAULT_CONCURRENCY,
    tolerance_mph: float = 0.0,
    walk_segment: bool = False, walk_segment_spacing_m: float = 15.0,
    side_mode: str = "center", side_offset_m: float = 20.0, auto_side_offset: bool = False,
    headings: tuple[int, ...] = DEFAULT_HEADINGS, headings_relative: bool = False, fov: int = DEFAULT_FOV,
    api_key: str, out_root: Path,
    out_json_path: Optional[Path] = None, out_csv_path: Optional[Path] = None, out_observations_csv_path: Optional[Path] = None,
    on_event=lambda event, **kw: None, log=lambda msg: None,
) -> dict:
    """The full pipeline (sample -> walk/check every segment -> stats ->
    write outputs -> always save to BigQuery), factored out of main() so
    both the CLI and a Flask background job can call it directly instead
    of main() being the only entry point. Raises SamplingError for a bad/
    empty-result input, and StreetViewAuthError/NoUsableApiKey unchanged
    for a broken key/billing (the caller decides how each should surface -
    main() turns both into a clean stderr message + exit code; a web job
    records the message on the job's own status instead).

    on_event(event, **kwargs) is called for progress reporting, same shape
    model_library.compare_models()'s own callback convention uses:
      - "sampling": about to run the sampling query
      - "start", total=n: n segments sampled, about to start checking them
      - "segment_done", index=i, total=n, result=dict: one segment finished
      - "saving_to_bigquery": about to append to calc_tmp
      - "done", payload=dict: the full return value, right before returning
    log(msg) is passed straight through to _process_sampled_segment() for
    its own internal per-step tracing (silent by default, same as before -
    main() passes log=print to preserve the CLI's old verbose output)."""
    _validate_identifier(project, PROJECT_RE, "project")
    _validate_identifier(dataset, DATASET_RE, "dataset")
    _validate_identifier(table, TABLE_RE, "table")
    _validate_identifier(state.upper(), STATE_RE, "state")
    if not (MIN_FOV <= fov <= MAX_FOV):
        raise SamplingError(f"fov must be between {MIN_FOV} and {MAX_FOV}.")
    if sample_size < 1:
        raise SamplingError("sample_size must be at least 1.")
    sample_size = min(sample_size, MAX_SAMPLE_SIZE)
    concurrency = max(1, min(concurrency, MAX_CONCURRENCY))
    max_walk_back = max(0, max_walk_back)
    out_root.mkdir(parents=True, exist_ok=True)

    # One shared BigQuery client for the whole run - NOT one per call. This
    # script can issue dozens of BigQuery calls per sampled segment
    # (topology lookup + up to 2 directions * max_walk_back neighbor
    # lookups), across `concurrency` worker threads at once; a fresh
    # bigquery.Client() per call was confirmed (via a real crash report) to
    # multiply how often a concurrent credential/mTLS subprocess check
    # races against gRPC's own background threads into a macOS fork-safety
    # crash ("multi-threaded process forked ... crashed on child side of
    # fork pre-exec"). bigquery.Client is documented safe for concurrent
    # use from multiple threads, so sharing one here is not a correctness
    # risk, just far fewer client/channel constructions.
    bq_client = bigquery.Client(project=project)

    on_event("sampling")
    try:
        sampled_rows = sample_segments_weighted_by_length(
            project, dataset, table, state=state, functional_class_lt=functional_class_lt, sample_size=sample_size,
            client=bq_client,
        )
    except Exception as e:
        raise SamplingError(f"Sampling query failed: {type(e).__name__}: {e}") from e
    if not sampled_rows:
        raise SamplingError("No segments matched the sampling criteria (check state/functional_class_lt/table).")

    vision_client = vision.ImageAnnotatorClient()
    reset_api_call_counts()
    results: list[dict] = []
    n = len(sampled_rows)
    on_event("start", total=n)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                _process_sampled_segment, i, n, row,
                project=project, dataset=dataset, table=table, state=state,
                max_walk_back=max_walk_back, api_key=api_key, vision_client=vision_client, bq_client=bq_client, out_root=out_root,
                walk_segment=walk_segment, walk_segment_spacing_m=walk_segment_spacing_m,
                side_mode=side_mode, side_offset_m=side_offset_m, auto_side_offset=auto_side_offset,
                headings=headings, headings_relative=headings_relative, fov=fov,
                log=log,
            ): i
            for i, row in enumerate(sampled_rows)
        }
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                result = fut.result()
            except (StreetViewAuthError, NoUsableApiKey):
                # A broken API key/billing/auth error isn't specific to
                # this one segment - every other worker is about to hit
                # the exact same thing. Cancel whatever hasn't started
                # yet and re-raise to the caller instead of quietly
                # logging up to `sample_size` identical "error" results
                # while burning through the rest of the sample.
                for other_fut in futures:
                    other_fut.cancel()
                raise
            except Exception as e:
                result = {
                    "segment_id": sampled_rows[i]["here_segment_id"], "status": "error",
                    "found_on_segment": False, "forward": None, "backward": None, "ground_truth_mph": None,
                    "note": f"{type(e).__name__}: {e}",
                }
            results.append(result)
            on_event("segment_done", index=len(results), total=n, result=result)

    obs = _observations(results)
    stats = compute_precision_stats(results, obs, tolerance_mph=tolerance_mph)
    api_calls = get_api_call_counts()

    on_event("population")
    try:
        population = population_agreement_counts(project, dataset, table, state=state, functional_class_lt=functional_class_lt, client=bq_client)
        inferred_precision = infer_overall_precision(population, stats)
        population_error = None
    except Exception as e:
        population = None
        inferred_precision = None
        population_error = f"{type(e).__name__}: {e}"
        log(f"WARNING: could not compute the domain agree/disagree population split: {population_error}")

    run_id = f"{state.upper()}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    config = {
        "project": project, "dataset": dataset, "table": table, "state": state.upper(),
        "functional_class_lt": functional_class_lt,
        "sample_size_requested": sample_size, "sample_size_actual": len(sampled_rows),
        "max_walk_back": max_walk_back, "tolerance_mph": tolerance_mph, "concurrency": concurrency,
        "walk_segment": walk_segment, "walk_segment_spacing_m": walk_segment_spacing_m,
        "side_mode": side_mode, "side_offset_m": side_offset_m, "auto_side_offset": auto_side_offset,
        "headings": list(headings), "headings_relative": headings_relative, "fov": fov,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    payload = {
        "run_id": run_id, "config": config, "api_calls": api_calls, "results": results, "observations": obs, "stats": stats,
        "population": population, "inferred_precision": inferred_precision, "population_error": population_error,
    }

    out_json_path = out_json_path or (out_root / "results.json")
    out_csv_path = out_csv_path or (out_root / "results.csv")
    out_observations_csv_path = out_observations_csv_path or (out_root / "observations.csv")
    out_json_path.write_text(json.dumps(payload, indent=2, default=str))
    write_results_csv(out_csv_path, results)
    write_observations_csv(out_observations_csv_path, obs)
    payload["output_files"] = {"json": str(out_json_path), "csv": str(out_csv_path), "observations_csv": str(out_observations_csv_path)}

    on_event("saving_to_bigquery")
    try:
        save_results_to_bigquery(project, run_id, config, results, obs, client=bq_client, log=log)
        payload["bigquery_saved"] = True
    except Exception as e:
        log(f"WARNING: failed to save results to BigQuery: {type(e).__name__}: {e}")
        payload["bigquery_saved"] = False
        payload["bigquery_error"] = f"{type(e).__name__}: {e}"

    on_event("done", payload=payload)
    return payload


def _format_segment_outcome(result: dict) -> str:
    """The same per-segment one-line detail main() has always printed -
    factored out so the CLI's on_event callback can reuse it verbatim."""
    if result["status"] == "error":
        # A worker actually raised - don't let this read as an ordinary
        # "checked thoroughly, found nothing" outcome (real bug caught in
        # practice: this used to fall through to the "no sign" branch
        # below, silently hiding the real exception message that's still
        # saved in result["note"] either way).
        return result.get("note", "unknown error")
    if result.get("found_on_segment"):
        return f"{result['ground_truth_mph']} mph (both directions)"
    fwd, bwd = result.get("forward") or {}, result.get("backward") or {}
    bits = []
    if fwd.get("found"):
        bits.append(f"forward={fwd['ground_truth_mph']} mph")
    if bwd.get("found"):
        bits.append(f"backward={bwd['ground_truth_mph']} mph")
    return ", ".join(bits) if bits else "no sign"


def main() -> int:
    args = parse_args()

    load_keys_file(args.keys_file)
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key or api_key == "your-key-here":
        print(f"ERROR: No usable GOOGLE_MAPS_API_KEY. Set it in the environment, or add a line "
              f"GOOGLE_MAPS_API_KEY=... to {args.keys_file}.", file=sys.stderr)
        return 1

    if args.sample_size > MAX_SAMPLE_SIZE:
        print(f"Note: --sample-size {args.sample_size} exceeds the hard ceiling of {MAX_SAMPLE_SIZE} - clamped.")

    run_label = f"{args.state.upper()}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_root = Path(args.out_dir) / run_label

    def on_event(event, **kw):
        if event == "sampling":
            print(f"Sampling {min(args.sample_size, MAX_SAMPLE_SIZE)} segment(s) from `{args.project}.{args.dataset}.{args.table}` "
                  f"(state={args.state.upper()}, functional_class<{args.functional_class_lt}), weighted by segment length...")
        elif event == "start":
            print(f"Sampled {kw['total']} segment(s). Looking for speed-limit signs "
                  f"(walking up to {max(0, args.max_walk_back)} segment(s) in each direction when a sampled segment has none)...\n")
        elif event == "segment_done":
            result = kw["result"]
            print(f"  [{kw['index']}/{kw['total']}] {result.get('segment_id')}: {result['status']} ({_format_segment_outcome(result)})")
        elif event == "population":
            print("\nComputing the domain's agree/disagree population split (for the inferred overall precision below)...")

    try:
        payload = run_precision_sample(
            project=args.project, dataset=args.dataset, table=args.table, state=args.state,
            functional_class_lt=args.functional_class_lt, sample_size=args.sample_size,
            max_walk_back=args.max_walk_back, concurrency=args.concurrency, tolerance_mph=args.tolerance_mph,
            walk_segment=args.walk_segment, walk_segment_spacing_m=args.walk_segment_spacing_m,
            side_mode=args.side_mode, side_offset_m=args.side_offset_m, auto_side_offset=args.auto_side_offset,
            headings=args.headings, headings_relative=args.headings_relative, fov=args.fov,
            api_key=api_key, out_root=out_root,
            out_json_path=args.out_json, out_csv_path=args.out_csv, out_observations_csv_path=args.out_observations_csv,
            on_event=on_event, log=print,
        )
    except SamplingError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except StreetViewAuthError as e:
        print(f"\nERROR: {e}\nThis means the API key/billing/API-enablement is broken, not that there's no "
              f"imagery. Check GOOGLE_MAPS_API_KEY and that billing/Street View Static API are enabled.", file=sys.stderr)
        return 1
    except NoUsableApiKey as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1

    print_report(payload["stats"], payload["api_calls"], payload.get("population"), payload.get("inferred_precision"), payload.get("population_error"))
    print_observations_table(payload["observations"])
    print(f"\nFull results: {payload['output_files']['json']}")
    print(f"CSV (per segment): {payload['output_files']['csv']}")
    print(f"CSV (per segment-direction observation, the table above): {payload['output_files']['observations_csv']}")
    print(f"Images: {out_root}")
    if payload.get("bigquery_saved"):
        print(f"Saved to BigQuery: calc_tmp.precision_sample_results / calc_tmp.precision_sample_observations (run_id={payload['run_id']})")
    else:
        print(f"WARNING: could not save to BigQuery: {payload.get('bigquery_error')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
