"""Fire-and-forget, resumable BigQuery appends (calc_out.signs, the
calc_tmp.precision_sample_* tables) - nothing in a pipeline ever waits on
BigQuery for these.

submit() writes the rows to a small spool file FIRST (local disk, mirrored
to the GCS cache like every other cache in this app) and returns
immediately; a background thread then loads it and deletes the spool file
only once the load succeeded. So a failed load (missing IAM, an outage) or
a process that died mid-write leaves the file behind, and replay_pending()
- called at app start and at the start of every CLI run - picks it up
again. Each spool file's load job uses a fixed BigQuery job ID derived from
the file's own id, so a replay after "loaded, but crashed before deleting
the file" is rejected by BigQuery as the same job (409 Conflict) and
treated as already done - never a duplicate append.

Same table conventions as before: schema autodetect on first load,
CREATE_IF_NEEDED, ALLOW_FIELD_ADDITION, no DDL anywhere.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from google.cloud import bigquery

SPOOL_DIR = Path(os.environ.get("BQ_SPOOL_DIR", "output/_bq_spool"))
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bq-write")
_inflight: set[str] = set()
_inflight_lock = threading.Lock()
_clients: dict[str, "bigquery.Client"] = {}
_clients_lock = threading.Lock()


def _client_for(project: str) -> "bigquery.Client":
    with _clients_lock:
        if project not in _clients:
            _clients[project] = bigquery.Client(project=project)
        return _clients[project]


def _spool_path(write_id: str) -> Path:
    return SPOOL_DIR / f"{write_id}.json"


def _run_in_background(write_id: str, log) -> None:
    with _inflight_lock:
        if write_id in _inflight:
            return
        _inflight.add(write_id)
    _POOL.submit(_load_now, write_id, log)


def _load_now(write_id: str, log) -> None:
    path = _spool_path(write_id)
    try:
        entry = json.loads(path.read_text())
        rows = entry["rows"]
        classify = entry.get("classify")
        if classify:
            # Slow, best-effort Gemini step, done here so it never blocks the pipeline. If the image is gone (replay
            # on a fresh instance) or Gemini fails, the visual columns just stay null - the sign itself is still recorded.
            import sign_registry
            image = Path(classify["image"])
            if image.exists() or _gcs_pull(image):
                rows[0].update(sign_registry.classify_sign_visual_attributes(image, classify["project"]))
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            autodetect=True,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
            schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
        )
        try:
            job = _client_for(entry["project"]).load_table_from_json(
                rows, entry["table_ref"], job_config=job_config, job_id=f"bqw_{write_id}"
            )
            job.result()
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ != "Conflict":
                raise
        path.unlink(missing_ok=True)
        log(f"  Appended {len(rows)} row(s) to {entry['table_ref']} (background).")
    except Exception as e:  # noqa: BLE001 - never raises; the spool file stays for replay_pending()
        log(f"  WARNING: background BigQuery write {write_id} failed ({type(e).__name__}: {e}) - kept for retry on next start.")
    finally:
        with _inflight_lock:
            _inflight.discard(write_id)


def _gcs_pull(path: Path) -> bool:
    try:
        from find_bad_speed_limit import gcs_cache_pull
        return gcs_cache_pull(path)
    except Exception:  # noqa: BLE001
        return False


def submit(project: str, table_ref: str, rows: list[dict], *, classify: Optional[dict] = None, log=lambda msg: None) -> None:
    """Queues `rows` for appending to `table_ref` and returns immediately.
    `classify` ({"image": path, "project": p}) asks the background step to
    add Gemini's visual attributes to rows[0] first. Never raises."""
    if not rows:
        return
    try:
        write_id = uuid.uuid4().hex
        SPOOL_DIR.mkdir(parents=True, exist_ok=True)
        path = _spool_path(write_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"project": project, "table_ref": table_ref, "rows": rows, "classify": classify}, default=str))
        tmp.replace(path)
        try:
            from find_bad_speed_limit import gcs_cache_push
            gcs_cache_push(path, log=log)
        except Exception:  # noqa: BLE001 - the GCS mirror is a bonus, not a requirement
            pass
        _run_in_background(write_id, log)
    except Exception as e:  # noqa: BLE001
        log(f"  WARNING: could not queue BigQuery write to {table_ref} ({type(e).__name__}: {e})")


def replay_pending(log=lambda msg: None) -> int:
    """Re-queues every spool file left over from an earlier run/crash (local
    disk, plus - when the GCS cache is configured - any mirrored to it by an
    instance that no longer exists). Returns immediately; the loads run in
    the background. Safe to call repeatedly."""
    try:
        from find_bad_speed_limit import GCS_CACHE_BUCKET, _gcs_bucket, _gcs_object_key
        if GCS_CACHE_BUCKET:
            prefix = _gcs_object_key(SPOOL_DIR) + "/"
            for blob in _gcs_bucket().list_blobs(prefix=prefix):
                local = SPOOL_DIR / Path(blob.name).name
                if blob.name.endswith(".json") and not local.exists():
                    SPOOL_DIR.mkdir(parents=True, exist_ok=True)
                    blob.download_to_filename(str(local))
    except Exception as e:  # noqa: BLE001
        log(f"  (could not list GCS for pending BigQuery writes: {type(e).__name__}: {e})")
    pending = sorted(SPOOL_DIR.glob("*.json")) if SPOOL_DIR.exists() else []
    for p in pending:
        _run_in_background(p.stem, log)
    if pending:
        log(f"  Resuming {len(pending)} pending BigQuery write(s) in the background.")
    return len(pending)
