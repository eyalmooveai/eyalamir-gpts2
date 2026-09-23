"""Generic disk+GCS cache for BigQuery query results (JSON-serializable
row lists), so an identical query doesn't hit BigQuery again as long as
whatever the caller says it depends on hasn't changed - see
cached_query(). Mirrors find_bad_speed_limit.py's own candidates-query
caching (disk, mirrored to GCS via GCS_CACHE_BUCKET when set), pulled out
here so any BQ-backed page in this app can reuse the same pattern instead
of each writing its own.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Callable, Optional

from find_bad_speed_limit import gcs_cache_pull, gcs_cache_push

CACHE_ROOT = Path("output") / "_bq_cache"


def cache_key(*parts) -> str:
    """A stable key for cached_query(), from any JSON-serializable parts -
    typically a query's own inputs plus whatever should invalidate it
    (a table's last-modified time, say)."""
    payload = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def cached_query(key: str, fetch: Callable[[], list], *, ttl_seconds: Optional[float] = None, log=lambda msg: None) -> list:
    """Returns fetch()'s result, from a JSON cache file (disk, and GCS too
    when GCS_CACHE_BUCKET is set) keyed by `key`, when one exists and -
    if ttl_seconds is given - isn't older than that. Otherwise calls
    fetch(), caches its result, and returns it.

    Caching here is an optimization, never a correctness requirement: any
    read/write problem is logged and treated as a miss/no-op rather than
    raised, so a cache outage just means every request re-queries
    BigQuery, not that the page breaks.
    """
    path = CACHE_ROOT / f"{key}.json"
    if gcs_cache_pull(path, log=log):
        try:
            fresh_enough = ttl_seconds is None or (time.time() - path.stat().st_mtime) <= ttl_seconds
            if fresh_enough:
                return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass  # fall through and re-fetch

    rows = fetch()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, default=str))
        gcs_cache_push(path, log=log)
    except OSError:
        pass  # caching is an optimization - don't fail the call over it
    return rows
