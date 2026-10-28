"""Checks that the two things every real-money run depends on - the GCS
cache bucket and BigQuery (read AND write) - are actually usable, BEFORE
any Street View/Vision/Gemini call is paid for, and keeps watching them
while the run goes.

- preflight(): called at the top of a run. Raises PreflightError naming
  every problem found, so a missing IAM role shows up in seconds instead of
  after thousands of paid calls.
    * GCS (only when GCS_CACHE_BUCKET is set): writes, reads back and
      deletes a tiny probe object.
    * BigQuery read: a free dry-run `SELECT 1` (needs bigquery.jobs.create).
    * BigQuery write: a real one-row load into `_preflight_probe`
      (WRITE_TRUNCATE) in each dataset this app appends result tables to
      (calc_out, calc_tmp) - the only reliable test of "can this identity
      create/append tables there". The probe tables are tiny and stay
      around; the check is overwritten each run.
- HealthMonitor: a background thread re-running the cheap checks (GCS
  probe + BigQuery dry run) every `interval_s`. After `max_failures`
  consecutive failures it sets `stop_event` and `reason`; the run then
  stops dispatching new segments, finishes what's in flight, saves its
  checkpoint and reports itself paused/resumable (RunPaused) - see
  precision_sample.run_precision_sample and batch_evaluator.run_batch.
"""
from __future__ import annotations

import threading
import uuid
from typing import Optional

from google.cloud import bigquery

PROBE_TABLE = "_preflight_probe"
PROBE_DATASETS = ("calc_out", "calc_tmp")


class PreflightError(RuntimeError):
    """A required service isn't usable - nothing has been spent."""


class RunPaused(RuntimeError):
    """A required service became unavailable mid-run; the checkpoint is saved and the run can be resumed."""


def check_gcs() -> Optional[str]:
    """None if the GCS cache works (or isn't configured), else why not."""
    from find_bad_speed_limit import GCS_CACHE_BUCKET, GCS_CACHE_PREFIX, _gcs_bucket
    if not GCS_CACHE_BUCKET:
        return None
    try:
        blob = _gcs_bucket().blob(f"{GCS_CACHE_PREFIX}/_preflight/{uuid.uuid4().hex}")
        blob.upload_from_string(b"ok")
        if blob.download_as_bytes() != b"ok":
            return f"GCS bucket {GCS_CACHE_BUCKET}: read-back mismatch"
        blob.delete()
    except Exception as e:  # noqa: BLE001
        return f"GCS bucket {GCS_CACHE_BUCKET} is not usable ({type(e).__name__}: {e})"
    return None


def check_bq_read(project: str) -> Optional[str]:
    try:
        job = bigquery.Client(project=project).query("SELECT 1", job_config=bigquery.QueryJobConfig(dry_run=True))
        job.total_bytes_processed  # noqa: B018 - raises if the dry run failed
    except Exception as e:  # noqa: BLE001
        return f"BigQuery (project {project}) cannot run queries ({type(e).__name__}: {e})"
    return None


def check_bq_write(project: str, on_step=lambda msg: None) -> list[str]:
    problems = []
    client = bigquery.Client(project=project)
    for dataset in PROBE_DATASETS:
        on_step(f"Preflight: checking BigQuery write access to {dataset} (one-row test load)...")
        try:
            job = client.load_table_from_json(
                [{"probe": uuid.uuid4().hex}], f"{project}.{dataset}.{PROBE_TABLE}",
                job_config=bigquery.LoadJobConfig(
                    source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON, autodetect=True,
                    write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
                    create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
                ),
            )
            job.result()
        except Exception as e:  # noqa: BLE001
            problems.append(f"BigQuery cannot write to {project}.{dataset} ({type(e).__name__}: {e}) - needs roles/bigquery.dataEditor there")
    return problems


def preflight(project: str, log=lambda msg: None, on_step=lambda msg: None) -> None:
    """Raises PreflightError listing every problem; returns quietly if all is well.
    `on_step(message)` is called before each check so a UI can show what the
    (few-seconds) delay before a run starts is actually doing."""
    def step(msg: str) -> None:
        log(msg)
        on_step(msg)

    step("Preflight: checking the GCS cache bucket (write, read back, delete a test object)...")
    gcs_problem = check_gcs()
    step("Preflight: checking BigQuery read access (dry-run query)...")
    read_problem = check_bq_read(project)
    problems = [p for p in (gcs_problem, read_problem) if p]
    if not read_problem:
        problems += check_bq_write(project, on_step=step)
    if problems:
        raise PreflightError("Not started - nothing was spent. " + " | ".join(problems))
    step("Preflight OK: GCS and BigQuery (read + write) are available - starting.")


class HealthMonitor:
    """Re-checks GCS + BigQuery every `interval_s`; trips `stop_event` after `max_failures` consecutive failures."""

    def __init__(self, project: str, interval_s: float = 60.0, max_failures: int = 3, log=lambda msg: None):
        self.project, self.interval_s, self.max_failures, self.log = project, interval_s, max_failures, log
        self.stop_event = threading.Event()
        self.reason: Optional[str] = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="health-monitor")

    def start(self) -> "HealthMonitor":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._done.set()

    def _run(self) -> None:
        failures = 0
        while not self._done.wait(self.interval_s):
            problem = check_gcs() or check_bq_read(self.project)
            failures = failures + 1 if problem else 0
            if failures >= self.max_failures:
                self.reason = f"{problem} (failed {failures} checks in a row)"
                self.log(f"PAUSING run: {self.reason}")
                self.stop_event.set()
                return
