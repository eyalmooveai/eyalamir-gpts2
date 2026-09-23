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
import re
from typing import Optional

from google.cloud import bigquery

from bq_cache import cache_key, cached_query
from find_bad_speed_limit import DATASET_RE, PROJECT_RE, STATE_RE, _validate_identifier, table_name

# calc_out table/column names are plain identifiers (letters/digits/
# underscore) - same shape find_bad_speed_limit.py validates dataset/
# project names with, just without the project/dataset-specific character
# restrictions. Used for both table names and the infer-field column name
# below, since both get interpolated directly into SQL (not passable as
# a query parameter, unlike a value).
TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# How long a schema-shaped listing (which tables exist, which columns a
# table has) is trusted before re-querying - these change far less often
# than the data itself, so a short TTL is enough to avoid a live
# INFORMATION_SCHEMA round-trip on every single page load without risking
# a long-stale view of what's actually there.
SCHEMA_CACHE_TTL_SECONDS = 300

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
# for the two "implausibly fast" checks), and - for the four diff_*
# metrics - {infer_field}, the specific inferred-speed-limit column being
# evaluated (see QualityFilters.infer_field / list_infer_fields below;
# this varies by table - e.g. speed_limit_infer_mph_corrected doesn't
# exist on every table, but speed_limit_infer_mph or
# speed_limit_infer_mph_new2/new3 might). Shared between the query
# builder and the results table so there's one definition of what each
# column means. label_template's {param1}/{param2} placeholders get
# filled in with the actual submitted threshold (see metric_labels below)
# - so the stat tile itself says e.g. "10 mph", not the literal word
# "param1".
QUALITY_METRICS = [
    {
        "key": "diff_osm",
        "label_template": "Disagrees with OSM by {param1}+ mph",
        "sql": "ABS({infer_field} - speed_limit_osm_mph) >= @param1",
    },
    {
        "key": "diff_here",
        "label_template": "Disagrees with HERE by {param1}+ mph",
        "sql": "ABS({infer_field} - speed_limit_here_mph) >= @param1",
    },
    {
        "key": "diff_speed_avg",
        "label_template": "Disagrees with observed avg speed by {param1}+ mph",
        "sql": "ABS({infer_field} - speed_AVG_mph) >= @param1",
    },
    {
        "key": "diff_freeflow",
        "label_template": "Disagrees with freeflow speed by {param1}+ mph",
        "sql": "ABS({infer_field} - freeflow_mph) >= @param1",
    },
    {
        "key": "speed_avg_high",
        "label_template": "Observed avg speed over {param2} mph (implausible)",
        "sql": "speed_AVG_mph > @param2",
    },
    {
        "key": "freeflow_high",
        "label_template": "Freeflow speed over {param2} mph (implausible)",
        "sql": "freeflow_mph > @param2",
    },
]

DEFAULT_INFER_FIELD = "speed_limit_infer_mph_corrected"


def _fmt_mph(value: float) -> str:
    """10.0 -> '10', 12.5 -> '12.5' - a param value as someone would
    actually type it, for splicing into a metric's label_template."""
    return f"{value:g}"


def metric_labels(param1: float, param2: float) -> dict[str, str]:
    """QUALITY_METRICS's label_template for each metric, with {param1}/
    {param2} filled in from the thresholds actually in effect - so the
    Quality page's stat tiles and breakdown table read e.g. "Disagrees
    with OSM by 10+ mph" instead of the literal word "param1"."""
    p1, p2 = _fmt_mph(param1), _fmt_mph(param2)
    return {m["key"]: m["label_template"].format(param1=p1, param2=p2) for m in QUALITY_METRICS}


@dataclasses.dataclass
class QualityFilters:
    project: str
    dataset: str
    table: str  # a calc_out table, e.g. speed_limits_US_2026_08_details
    infer_field: str = DEFAULT_INFER_FIELD  # the inferred-speed-limit column being evaluated - see list_infer_fields
    param1: float = 10.0
    param2: float = 80.0
    states: tuple[str, ...] = ()  # empty = all states
    functional_classes: tuple[int, ...] = ()  # empty = all classes
    group_by: str = "none"  # one of GROUP_BY_CHOICES


def default_table_name(year: str, month: str) -> str:
    """The nationwide _details table for a given year/month - used as the
    page's default selection before the user picks another one from the
    live table list."""
    return table_name("US", year, month)


# Where evaluable tables live and what to look for in each: calc_out's
# speed_limits_US* family (what the sign checker itself is built on), plus
# archimedes_api's speed_limits_infer* views over it - the ones the user
# originally expected this page to show, and still worth offering even
# though they're missing columns some metrics need (picking one just
# surfaces that as a normal BigQuery error in the page's error card).
TABLE_SOURCES = (("calc_out", "speed_limits_US"), ("archimedes_api", "speed_limits_infer"))


def list_evaluable_tables(project: str, log=lambda msg: None) -> list[dict]:
    """Live list of {dataset, table} across every source in TABLE_SOURCES -
    so the Quality page's table selector always reflects what's actually
    there (including views), rather than assuming a naming pattern or a
    single dataset. Cached for SCHEMA_CACHE_TTL_SECONDS - see bq_cache."""
    _validate_identifier(project, PROJECT_RE, "project")

    def run() -> list[dict]:
        client = bigquery.Client(project=project)
        results: list[dict] = []
        for dataset, prefix in TABLE_SOURCES:
            _validate_identifier(dataset, DATASET_RE, "dataset")
            query = f"""
                SELECT table_name
                FROM `{project}.{dataset}.INFORMATION_SCHEMA.TABLES`
                WHERE table_name LIKE '{prefix}%'
                ORDER BY table_name
            """
            results.extend({"dataset": dataset, "table": row["table_name"]} for row in client.query(query).result())
        return results

    key = cache_key("evaluable_tables", project)
    return cached_query(key, run, ttl_seconds=SCHEMA_CACHE_TTL_SECONDS, log=log)


def list_infer_fields(project: str, dataset: str, table: str, log=lambda msg: None) -> list[str]:
    """Live list of this specific table's columns matching
    speed_limit_infer* - which inferred-speed-limit column(s) it actually
    has varies by table (e.g. speed_limit_infer_mph_corrected doesn't
    exist on every one, some instead have speed_limit_infer_mph_new2/
    new3), so this is discovered per table rather than assumed. Cached
    for SCHEMA_CACHE_TTL_SECONDS - see bq_cache."""
    _validate_identifier(project, PROJECT_RE, "project")
    _validate_identifier(dataset, DATASET_RE, "dataset")
    _validate_identifier(table, TABLE_RE, "table")

    def run() -> list[str]:
        client = bigquery.Client(project=project)
        query = f"""
            SELECT column_name
            FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS`
            WHERE table_name = @table AND column_name LIKE 'speed_limit_infer%'
            ORDER BY column_name
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("table", "STRING", table)])
        return [row["column_name"] for row in client.query(query, job_config=job_config).result()]

    key = cache_key("infer_fields", project, dataset, table)
    return cached_query(key, run, ttl_seconds=SCHEMA_CACHE_TTL_SECONDS, log=log)


def full_table_name(f: QualityFilters) -> str:
    """The fully-qualified table these metrics are actually computed from -
    for display, so the page is explicit about its real data source. Not
    archimedes_api.speed_limits_infer_details: that view is missing
    speed_limit_here_mph and freeflow_mph, two of the six metrics here
    depend on them, so this queries a dated nationwide _details table
    directly instead."""
    return f"{f.project}.{f.dataset}.{f.table}"


def build_quality_query(f: QualityFilters) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    _validate_identifier(f.project, PROJECT_RE, "project")
    _validate_identifier(f.dataset, DATASET_RE, "dataset")
    _validate_identifier(f.table, TABLE_RE, "table")
    _validate_identifier(f.infer_field, TABLE_RE, "infer_field")
    if f.group_by not in GROUP_BY_CHOICES:
        raise ValueError(f"Invalid group_by {f.group_by!r} - must be one of {GROUP_BY_CHOICES}")
    table = f.table

    where_parts = [f"{f.infer_field} IS NOT NULL"]
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
    metric_cols = [
        f"100.0 * SUM(CASE WHEN {m['sql'].format(infer_field=f.infer_field)} THEN 1 ELSE 0 END) / COUNT(*) AS pct_{m['key']}"
        for m in QUALITY_METRICS
    ]

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


def fetch_quality_metrics(f: QualityFilters, log=lambda msg: None) -> list[dict]:
    """Runs (or serves from cache) the aggregate query build_quality_query
    builds for `f`. Cached on the selected table's own last-modified time
    (a metadata GET, not a query job - effectively free) alongside every
    other input, so an identical request only re-queries BigQuery once
    the table it's actually reading has changed - not on every request,
    and not stale forever either. If that metadata lookup itself fails
    (bad table name, a transient error, ...), skips caching and just
    queries directly - the query itself will surface the real error."""
    client = bigquery.Client(project=f.project)
    query, params = build_quality_query(f)

    def run() -> list[dict]:
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        return [dict(row.items()) for row in client.query(query, job_config=job_config).result()]

    try:
        modified = client.get_table(f"{f.project}.{f.dataset}.{f.table}").modified.isoformat()
    except Exception:
        return run()

    key = cache_key("quality_metrics", dataclasses.asdict(f), modified)
    return cached_query(key, run, log=log)
