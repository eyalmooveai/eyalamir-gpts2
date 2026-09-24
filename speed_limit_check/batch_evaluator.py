"""Batch evaluation of many road segments for a state - "run the sign
checker's own per-candidate logic across up to `segment_count` segments
concurrently, durably, so the run can be walked away from and checked on
later." Built on top of find_bad_speed_limit.py's existing pipeline:
`_process_one_candidate` (the same per-segment "walk positions until a
sign is read" logic `run_pipeline` uses, extracted so it's independently
callable) does the actual work; this module is purely orchestration -
concurrency, durable status/results, CSV export, and a post-run GCS sync.

Durability: every batch run gets a `batch_id` and status/results are
written to disk (output/_batch_jobs/<batch_id>/...) and, when
GCS_CACHE_BUCKET is set, mirrored to GCS the same way every other cache
in this app is (see find_bad_speed_limit.gcs_cache_pull/gcs_cache_push).
That's the source of truth a status page reads from - not just an
in-memory job dict - so navigating back to a batch's page (even from a
different browser, or after this process restarts) shows real state.
This does NOT make an in-progress batch resumable across a process
restart, though - if this instance dies mid-run, the batch's background
thread dies with it, and status.json is left at its last written
snapshot ("running" but not actually progressing). See CLAUDE.md for the
--min-instances=1 follow-up that would prevent Cloud Run from recycling
the instance mid-run in the first place.

Concurrency: a `concurrent.futures.ThreadPoolExecutor` per batch, one
worker call per segment. Deliberately does NOT add a new rate limiter -
find_bad_speed_limit.py's Street View throttle
(_streetview_throttle_lock/_throttle_streetview_call) is already a
process-global, thread-safe lock, so every worker thread's Street View
calls are already correctly serialized to STREETVIEW_MIN_INTERVAL_S apart
regardless of how many workers are running. Higher concurrency here
mostly buys overlap on Vision OCR/network latency between workers, not a
bypass of that pacing - which is the point: safer against tripping
Google's rate limits than true unpaced parallelism would be.
"""
from __future__ import annotations

import csv as csv_module
import dataclasses
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from google.cloud import vision

from find_bad_speed_limit import (
    DATASET_RE,
    GCS_CACHE_BUCKET,
    DEFAULT_KEYS_FILE,
    NoUsableApiKey,
    PROJECT_RE,
    STATE_RE,
    StreetViewAuthError,
    _process_one_candidate,
    _validate_identifier,
    fetch_candidates,
    gcs_cache_pull,
    gcs_cache_push,
    get_api_call_counts,
    load_keys_file,
    reset_api_call_counts,
    table_name,
)

import os

BATCH_ROOT = Path("output") / "_batch_jobs"
INDEX_PATH = BATCH_ROOT / "index.json"

_index_lock = threading.Lock()

CSV_FIELDNAMES = [
    "segment_id", "state", "functional_class", "status", "matched_speed_mph",
    "lat", "lon", "speed_limit_osm_mph", "speed_limit_here_mph",
    "speed_limit_infer_mph", "speed_limit_infer_mph_corrected",
    "speed_AVG_mph", "freeflow_mph", "note", "image_count",
    "annotated_image_path", "error", "row_json",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclasses.dataclass
class BatchConfig:
    label: str
    state: str
    year: str
    month: str
    project: str
    dataset: str
    segment_count: int = 1000
    concurrency: int = 10
    criteria: dict = dataclasses.field(default_factory=dict)
    walk_segment: bool = True
    walk_segment_spacing_m: float = 15.0
    side_mode: str = "center"
    side_offset_m: float = 20.0
    auto_side_offset: bool = False
    headings: tuple = (0, 90, 180, 270)
    headings_relative: bool = False
    fov: int = 90
    started_by: str = ""


def _status_path(batch_id: str) -> Path:
    return BATCH_ROOT / batch_id / "status.json"


def _csv_path(batch_id: str) -> Path:
    return BATCH_ROOT / batch_id / "results.csv"


def csv_path_if_exists(batch_id: str) -> Optional[Path]:
    path = _csv_path(batch_id)
    return path if gcs_cache_pull(path) else None


def _read_index() -> list[dict]:
    if not gcs_cache_pull(INDEX_PATH):
        return []
    try:
        return json.loads(INDEX_PATH.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []


def _write_index(entries: list[dict]) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(entries))
    gcs_cache_push(INDEX_PATH)


def _upsert_index_entry(summary: dict) -> None:
    with _index_lock:
        entries = [e for e in _read_index() if e.get("batch_id") != summary["batch_id"]]
        entries.append(summary)
        entries.sort(key=lambda e: e.get("started_at", ""), reverse=True)
        _write_index(entries)


def list_batches() -> list[dict]:
    """Every batch run's summary (newest first), for the evaluator's
    index page - from the durable index, not any in-memory state, so it
    reflects runs from prior process lifetimes too."""
    return _read_index()


def read_status(batch_id: str) -> Optional[dict]:
    """A batch's full current status, from disk/GCS - always fresh, not
    dependent on this process being the one that's running it."""
    path = _status_path(batch_id)
    if not gcs_cache_pull(path):
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def breakdown_by_functional_class(results: list[dict]) -> list[dict]:
    """Segments-checked/matched/etc. counts grouped by functional_class,
    from a (possibly still-growing) batch's own status["results"] list -
    computed on the fly rather than persisted, so it's always consistent
    with whatever `results` the caller already has (server-side initial
    render, or the client's own poll data - see evaluator_status.html).
    functional_class missing/null groups under "(unknown)"."""
    buckets: dict = {}
    for r in results:
        fc = r.get("functional_class")
        key = fc if fc is not None else "(unknown)"
        b = buckets.setdefault(key, {"functional_class": key, "total": 0, "matched": 0, "no_sign_count": 0, "no_coverage_count": 0, "error_count": 0})
        b["total"] += 1
        if r.get("status") == "match":
            b["matched"] += 1
        elif r.get("status") == "no_sign_read":
            b["no_sign_count"] += 1
        elif r.get("status") == "no_coverage":
            b["no_coverage_count"] += 1
        elif r.get("status") == "error":
            b["error_count"] += 1
    for b in buckets.values():
        b["match_rate_pct"] = round(100.0 * b["matched"] / b["total"], 1) if b["total"] else 0.0
    # (unknown) sorts last - comparing it against a numeric functional_class
    # would otherwise raise (str vs int); the tuple's first element keeps
    # that comparison from ever happening except among same-"unknown-ness" buckets.
    return sorted(buckets.values(), key=lambda b: (b["functional_class"] == "(unknown)", b["functional_class"]))


def run_history_stats() -> list[dict]:
    """Empirical per-segment call rates and per-call timing, learned from
    every completed run's *real* measured usage (elapsed_seconds,
    actual_streetview_calls, actual_vision_calls - see run_batch) rather
    than a guessed formula. One bucket per distinct (walk_segment,
    side_mode, headings_count) config actually run, plus one overall
    bucket pooling everything, for a config with no exact match yet - the
    launch form's estimator uses whichever's the best available match.
    Runs from before this was tracked (no elapsed_seconds recorded) are
    skipped, not treated as zero.
    """
    samples = []
    for b in list_batches():
        s = read_status(b["batch_id"])
        if not s or s.get("run_status") not in ("done", "cancelled"):
            continue
        if not s.get("done_count") or not s.get("elapsed_seconds"):
            continue
        total_calls = (s.get("actual_streetview_calls") or 0) + (s.get("actual_vision_calls") or 0)
        if total_calls <= 0:
            continue
        cfg = s.get("config") or {}
        samples.append({
            "walk_segment": cfg.get("walk_segment"),
            "side_mode": cfg.get("side_mode"),
            "headings_count": len(cfg.get("headings") or []),
            "done_count": s["done_count"],
            "concurrency": s.get("concurrency") or cfg.get("concurrency") or 1,
            "elapsed_seconds": s["elapsed_seconds"],
            "streetview_calls": s.get("actual_streetview_calls") or 0,
            "vision_calls": s.get("actual_vision_calls") or 0,
        })

    if not samples:
        return []

    def aggregate(rows: list[dict]) -> Optional[dict]:
        n = sum(r["done_count"] for r in rows)
        total_calls = sum(r["streetview_calls"] + r["vision_calls"] for r in rows)
        if n <= 0 or total_calls <= 0:
            return None
        # "Worker-seconds per call": elapsed time already spread across
        # that run's own concurrency, normalized per call - so it can be
        # projected onto a *different* concurrency for a new estimate
        # (predicted_seconds = new_total_calls * this / new_concurrency).
        worker_seconds_per_call = sum(r["elapsed_seconds"] * r["concurrency"] for r in rows) / total_calls
        return {
            "sample_count": len(rows),
            "segment_count": n,
            "streetview_calls_per_segment": sum(r["streetview_calls"] for r in rows) / n,
            "vision_calls_per_segment": sum(r["vision_calls"] for r in rows) / n,
            "worker_seconds_per_call": worker_seconds_per_call,
        }

    buckets: dict[tuple, list[dict]] = {}
    for r in samples:
        key = (r["walk_segment"], r["side_mode"], r["headings_count"])
        buckets.setdefault(key, []).append(r)

    result = []
    for (walk_segment, side_mode, headings_count), rows in buckets.items():
        agg = aggregate(rows)
        if agg:
            result.append({"walk_segment": walk_segment, "side_mode": side_mode, "headings_count": headings_count, **agg})

    overall = aggregate(samples)
    if overall:
        result.append({"walk_segment": None, "side_mode": None, "headings_count": None, **overall})
    return result


def _write_status(batch_id: str, status: dict) -> None:
    path = _status_path(batch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status))
    gcs_cache_push(path)
    _upsert_index_entry({
        "batch_id": batch_id,
        "label": status["label"],
        "state": status["state"],
        "year": status["year"],
        "month": status["month"],
        "started_by": status["started_by"],
        "started_at": status["started_at"],
        "finished_at": status["finished_at"],
        "run_status": status["run_status"],
        "segment_count": status["segment_count"],
        "total_count": status["total_count"],
        "done_count": status["done_count"],
        "matched_count": status["matched_count"],
    })


def _row_context(row_dict: dict) -> dict:
    """The subset of a segment's source row worth carrying per-segment in
    status.json (results table + breakdown-by-category) - not the full
    row (the CSV's row_json column already has that), just the fields
    worth scanning/grouping by at a glance. Prefers the corrected
    inferred value when the table has it, same fallback as the CSV."""
    infer = row_dict.get("speed_limit_infer_mph_corrected")
    if infer is None:
        infer = row_dict.get("speed_limit_infer_mph")
    return {
        "functional_class": row_dict.get("functional_class"),
        "speed_limit_osm_mph": row_dict.get("speed_limit_osm_mph"),
        "speed_limit_here_mph": row_dict.get("speed_limit_here_mph"),
        "speed_limit_infer_mph": infer,
        "speed_AVG_mph": row_dict.get("speed_AVG_mph"),
        "freeflow_mph": row_dict.get("freeflow_mph"),
    }


def _build_csv_row(candidate_row, attempt, match, error: Optional[str]) -> dict:
    row_dict = dict(attempt.row) if attempt is not None else {}
    return {
        "segment_id": attempt.segment_id if attempt is not None else candidate_row.get("here_segment_id"),
        "state": row_dict.get("state", ""),
        "functional_class": row_dict.get("functional_class", ""),
        "status": attempt.status if attempt is not None else "error",
        "matched_speed_mph": match.reading.speed_mph if match is not None else "",
        "lat": attempt.lat if attempt is not None else candidate_row.get("centroid_lat"),
        "lon": attempt.lon if attempt is not None else candidate_row.get("centroid_lon"),
        "speed_limit_osm_mph": row_dict.get("speed_limit_osm_mph", ""),
        "speed_limit_here_mph": row_dict.get("speed_limit_here_mph", ""),
        "speed_limit_infer_mph": row_dict.get("speed_limit_infer_mph", ""),
        "speed_limit_infer_mph_corrected": row_dict.get("speed_limit_infer_mph_corrected", ""),
        "speed_AVG_mph": row_dict.get("speed_AVG_mph", ""),
        "freeflow_mph": row_dict.get("freeflow_mph", ""),
        "note": attempt.note if attempt is not None else "",
        "image_count": len(attempt.image_details) if attempt is not None else 0,
        "annotated_image_path": str(attempt.annotated_image) if attempt is not None and attempt.annotated_image else "",
        "error": error or "",
        "row_json": json.dumps(row_dict, default=str),
    }


def _write_csv(batch_id: str, rows: list[dict]) -> None:
    path = _csv_path(batch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv_module.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    gcs_cache_push(path)


def _sync_images_to_gcs(out_root: Path, log=lambda msg: None) -> None:
    """Best-effort sweep pushing every image under this batch's output
    directory to GCS. Most images already got pushed individually as
    they were fetched (_process_one_candidate's own gcs_cache_push calls,
    inherited unchanged from run_pipeline) - this is a safety net after
    the run concludes, catching anything an individual push failed on
    transiently mid-run, so the run's full image set ends up archived."""
    if not GCS_CACHE_BUCKET or not out_root.exists():
        return
    for image_path in out_root.rglob("*.jpg"):
        gcs_cache_push(image_path, log=log)


def _finalize_usage(status: dict, run_started_monotonic: float) -> None:
    """Records this run's real elapsed time and actual API call counts
    into its status - called at every exit point of run_batch (done,
    cancelled, or error) so even a failed/cancelled run leaves behind
    real partial-usage data for run_history_stats() to learn from,
    rather than only ever recording successful completions."""
    status["elapsed_seconds"] = time.monotonic() - run_started_monotonic
    counts = get_api_call_counts()
    status["actual_streetview_calls"] = counts["streetview"]
    status["actual_vision_calls"] = counts["vision"]


def _worker(i: int, n: int, row, config: BatchConfig, api_key: str, vision_client, out_root: Path, cancel_event: threading.Event):
    if cancel_event.is_set():
        return i, None, None, None
    try:
        attempt, match = _process_one_candidate(
            i, n, row,
            api_key=api_key, vision_client=vision_client, out_root=out_root,
            walk_segment=config.walk_segment, walk_segment_spacing_m=config.walk_segment_spacing_m,
            side_mode=config.side_mode, side_offset_m=config.side_offset_m, auto_side_offset=config.auto_side_offset,
            headings=config.headings, headings_relative=config.headings_relative, fov=config.fov,
            log=lambda msg: None, progress=lambda fraction, msg: None,
        )
        return i, attempt, match, None
    except Exception as e:
        return i, None, None, f"{type(e).__name__}: {e}"


def run_batch(batch_id: str, config: BatchConfig, cancel_event: threading.Event) -> None:
    """Runs one batch end to end: fetch up to config.segment_count
    candidates for config.state/year/month, process them concurrently
    (config.concurrency workers), writing durable status as it goes and
    a CSV + GCS image sync once done. Meant to run on its own background
    thread (see app.py) - never raises, all errors land in the durable
    status instead."""
    started_at = _now_iso()
    run_started_monotonic = time.monotonic()
    status = {
        "batch_id": batch_id, "label": config.label, "state": config.state,
        "year": config.year, "month": config.month, "project": config.project,
        "dataset": config.dataset, "segment_count": config.segment_count,
        "concurrency": config.concurrency, "started_by": config.started_by,
        "started_at": started_at, "finished_at": None, "run_status": "running",
        "message": "Querying candidate segments...", "heartbeat_at": started_at,
        "total_count": 0, "done_count": 0, "matched_count": 0,
        "no_sign_count": 0, "no_coverage_count": 0, "error_count": 0,
        "error": None, "results": [],
        # Full run config, for later retrieval/comparison ("same streets,
        # a new model") - every selection-criteria/walk/side/heading/fov
        # setting this run actually used, not just the headline label.
        "config": dataclasses.asdict(config),
        # Real, measured usage - not a formula's guess. elapsed_seconds is
        # wall-clock from the top of this function (BigQuery candidates
        # query included, since that's real time too); the call counts are
        # actual billed API calls (cache hits never reach those call
        # sites - see find_bad_speed_limit.get_api_call_counts). This is
        # what run_history_stats() below learns from for future estimates.
        "elapsed_seconds": None,
        "actual_streetview_calls": 0,
        "actual_vision_calls": 0,
    }
    _write_status(batch_id, status)

    try:
        load_keys_file(DEFAULT_KEYS_FILE)
        api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
        if not api_key or api_key == "your-key-here":
            raise NoUsableApiKey(
                f"No usable GOOGLE_MAPS_API_KEY. Set it in the environment, or add a line "
                f"GOOGLE_MAPS_API_KEY=... to {DEFAULT_KEYS_FILE}."
            )
        _validate_identifier(config.project, PROJECT_RE, "project")
        _validate_identifier(config.dataset, DATASET_RE, "dataset")
        _validate_identifier(config.state, STATE_RE, "state (expected 2 letters, e.g. NC)")

        table = table_name(config.state, config.year, config.month)
        out_root = Path("output") / f"{config.state.upper()}_{config.year}_{config.month}"
        candidates = fetch_candidates(config.project, config.dataset, table, config.criteria, config.segment_count)
        n = len(candidates)
        status["total_count"] = n
        status["message"] = f"Found {n} candidate segment(s)." if n else "No road segments matched the selection criteria."
        _write_status(batch_id, status)

        if not candidates:
            status["run_status"] = "done"
            status["finished_at"] = _now_iso()
            _write_status(batch_id, status)
            return

        vision_client = vision.ImageAnnotatorClient()
        results_lock = threading.Lock()
        csv_rows: list[dict] = []
        reset_api_call_counts()

        with ThreadPoolExecutor(max_workers=max(1, config.concurrency)) as pool:
            futures = [
                pool.submit(_worker, i, n, row, config, api_key, vision_client, out_root, cancel_event)
                for i, row in enumerate(candidates)
            ]
            for fut in as_completed(futures):
                i, attempt, match, error = fut.result()
                if attempt is None and error is None:
                    continue  # cancelled before this one started
                with results_lock:
                    csv_rows.append(_build_csv_row(candidates[i], attempt, match, error))
                    status["done_count"] += 1
                    if error:
                        status["error_count"] += 1
                        seg_summary = {"segment_id": candidates[i].get("here_segment_id"), "status": "error", "error": error}
                        seg_summary.update(_row_context(dict(candidates[i].items())))
                    elif attempt.status == "match":
                        status["matched_count"] += 1
                        seg_summary = {"segment_id": attempt.segment_id, "status": "match", "matched_speed_mph": match.reading.speed_mph}
                        seg_summary.update(_row_context(attempt.row))
                    elif attempt.status == "no_sign_read":
                        status["no_sign_count"] += 1
                        seg_summary = {"segment_id": attempt.segment_id, "status": "no_sign_read"}
                        seg_summary.update(_row_context(attempt.row))
                    else:
                        status["no_coverage_count"] += 1
                        seg_summary = {"segment_id": attempt.segment_id, "status": "no_coverage"}
                        seg_summary.update(_row_context(attempt.row))
                    status["results"].append(seg_summary)
                    status["message"] = f"{status['done_count']}/{n} segments checked ({status['matched_count']} sign(s) found so far)..."
                    status["heartbeat_at"] = _now_iso()
                    # Written periodically, not on every single completion -
                    # frequent enough for a "switch back in" check to see
                    # fresh progress, without a GCS round-trip per segment
                    # under high concurrency. Usage refreshed here too, so
                    # someone watching a long run sees real elapsed
                    # time/call counts climb, not just at the very end.
                    if status["done_count"] % 10 == 0 or status["done_count"] == n:
                        _finalize_usage(status, run_started_monotonic)
                        _write_status(batch_id, status)

        _write_csv(batch_id, csv_rows)
        _sync_images_to_gcs(out_root)

        status["run_status"] = "cancelled" if cancel_event.is_set() and status["done_count"] < n else "done"
        status["finished_at"] = _now_iso()
        status["message"] = (
            f"Cancelled after {status['done_count']}/{n} segment(s) checked ({status['matched_count']} sign(s) found)."
            if status["run_status"] == "cancelled"
            else f"Done. {status['matched_count']} sign(s) found out of {status['done_count']} segment(s) checked."
        )
        _finalize_usage(status, run_started_monotonic)
        _write_status(batch_id, status)
    except (NoUsableApiKey, StreetViewAuthError, ValueError) as e:
        status["run_status"] = "error"
        status["error"] = str(e)
        status["finished_at"] = _now_iso()
        _finalize_usage(status, run_started_monotonic)
        _write_status(batch_id, status)
    except Exception as e:
        status["run_status"] = "error"
        status["error"] = f"{type(e).__name__}: {e}"
        status["finished_at"] = _now_iso()
        _finalize_usage(status, run_started_monotonic)
        _write_status(batch_id, status)
