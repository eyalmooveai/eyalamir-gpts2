"""BigQuery-backed nationwide speed-limit quality metrics, for the
Speed-Limits Quality page of the Archimedes hub.

Queries `speed_limits_<STATE>_<YEAR>_<MONTH>_details` tables (the same
family find_bad_speed_limit.py uses, but here the nationwide "US" one) to
compute what percentage of road segments show a large mismatch between
the corrected inferred speed limit and each of OSM/HERE/observed average
speed/freeflow speed, or an implausibly high observed speed - optionally
broken down by state and/or functional_class.
"""
from __future__ import annotations

import dataclasses
from typing import Optional

from google.cloud import bigquery

from find_bad_speed_limit import DATASET_RE, PROJECT_RE, STATE_RE, _validate_identifier, table_name

# The 50 states + DC, for the filter UI - independent of which states
# actually have data in a given month's table (the query's own results
# simply come back empty for any that don't).
US_STATE_CODES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY",
]

GROUP_BY_CHOICES = ("none", "state", "functional_class", "state,functional_class")

# Each metric's SQL condition, parameterized on @param1 (mph gap threshold
# for the four "disagrees with X" comparisons) or @param2 (mph threshold
# for the two "implausibly fast" checks). Shared between the query builder
# and the results table so there's one definition of what each column means.
QUALITY_METRICS = [
    {
        "key": "diff_osm",
        "label": "|infer_corrected − OSM| ≥ param1 mph",
        "sql": "ABS(speed_limit_infer_mph_corrected - speed_limit_osm_mph) >= @param1",
    },
    {
        "key": "diff_here",
        "label": "|infer_corrected − HERE| ≥ param1 mph",
        "sql": "ABS(speed_limit_infer_mph_corrected - speed_limit_here_mph) >= @param1",
    },
    {
        "key": "diff_speed_avg",
        "label": "|infer_corrected − observed avg speed| ≥ param1 mph",
        "sql": "ABS(speed_limit_infer_mph_corrected - speed_AVG_mph) >= @param1",
    },
    {
        "key": "diff_freeflow",
        "label": "|infer_corrected − freeflow speed| ≥ param1 mph",
        "sql": "ABS(speed_limit_infer_mph_corrected - freeflow_mph) >= @param1",
    },
    {
        "key": "speed_avg_high",
        "label": "observed avg speed > param2 mph",
        "sql": "speed_AVG_mph > @param2",
    },
    {
        "key": "freeflow_high",
        "label": "freeflow speed > param2 mph",
        "sql": "freeflow_mph > @param2",
    },
]


@dataclasses.dataclass
class QualityFilters:
    project: str
    dataset: str
    year: str
    month: str
    param1: float = 10.0
    param2: float = 80.0
    states: tuple[str, ...] = ()  # empty = all states
    functional_classes: tuple[int, ...] = ()  # empty = all classes
    group_by: str = "none"  # one of GROUP_BY_CHOICES


def build_quality_query(f: QualityFilters) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    _validate_identifier(f.project, PROJECT_RE, "project")
    _validate_identifier(f.dataset, DATASET_RE, "dataset")
    if f.group_by not in GROUP_BY_CHOICES:
        raise ValueError(f"Invalid group_by {f.group_by!r} - must be one of {GROUP_BY_CHOICES}")
    table = table_name("US", f.year, f.month)

    where_parts = ["speed_limit_infer_mph_corrected IS NOT NULL"]
    params: list[bigquery.ScalarQueryParameter] = [
        bigquery.ScalarQueryParameter("param1", "FLOAT64", float(f.param1)),
        bigquery.ScalarQueryParameter("param2", "FLOAT64", float(f.param2)),
    ]
    if f.states:
        for s in f.states:
            _validate_identifier(s, STATE_RE, "state (expected 2 letters, e.g. NC)")
        where_parts.append("state IN UNNEST(@states)")
        params.append(bigquery.ArrayQueryParameter("states", "STRING", [s.upper() for s in f.states]))
    if f.functional_classes:
        where_parts.append("functional_class IN UNNEST(@functional_classes)")
        params.append(bigquery.ArrayQueryParameter("functional_classes", "INT64", list(f.functional_classes)))

    group_cols = f.group_by.split(",") if f.group_by != "none" else []
    select_cols = [f"{c}," for c in group_cols]
    metric_cols = [f"100.0 * SUM(CASE WHEN {m['sql']} THEN 1 ELSE 0 END) / COUNT(*) AS pct_{m['key']}" for m in QUALITY_METRICS]

    query = f"""
        SELECT
          {' '.join(select_cols)}
          COUNT(*) AS total_segments,
          {', '.join(metric_cols)}
        FROM `{f.project}.{f.dataset}.{table}`
        WHERE {' AND '.join(where_parts)}
        {f"GROUP BY {', '.join(group_cols)}" if group_cols else ""}
        {f"ORDER BY {', '.join(group_cols)}" if group_cols else ""}
    """
    return query, params


def fetch_quality_metrics(f: QualityFilters) -> list[dict]:
    client = bigquery.Client(project=f.project)
    query, params = build_quality_query(f)
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    return [dict(row.items()) for row in client.query(query, job_config=job_config).result()]
