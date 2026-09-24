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
    load_keys_file,
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
                    elif attempt.status == "match":
                        status["matched_count"] += 1
                        seg_summary = {"segment_id": attempt.segment_id, "status": "match", "matched_speed_mph": match.reading.speed_mph}
                    elif attempt.status == "no_sign_read":
                        status["no_sign_count"] += 1
                        seg_summary = {"segment_id": attempt.segment_id, "status": "no_sign_read"}
                    else:
                        status["no_coverage_count"] += 1
                        seg_summary = {"segment_id": attempt.segment_id, "status": "no_coverage"}
                    status["results"].append(seg_summary)
                    status["message"] = f"{status['done_count']}/{n} segments checked ({status['matched_count']} sign(s) found so far)..."
                    status["heartbeat_at"] = _now_iso()
                    # Written periodically, not on every single completion -
                    # frequent enough for a "switch back in" check to see
                    # fresh progress, without a GCS round-trip per segment
                    # under high concurrency.
                    if status["done_count"] % 10 == 0 or status["done_count"] == n:
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
        _write_status(batch_id, status)
    except (NoUsableApiKey, StreetViewAuthError, ValueError) as e:
        status["run_status"] = "error"
        status["error"] = str(e)
        status["finished_at"] = _now_iso()
        _write_status(batch_id, status)
    except Exception as e:
        status["run_status"] = "error"
        status["error"] = f"{type(e).__name__}: {e}"
        status["finished_at"] = _now_iso()
        _write_status(batch_id, status)
