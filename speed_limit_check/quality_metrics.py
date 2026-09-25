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
from find_bad_speed_limit import DATASET_RE, PROJECT_RE, STATE_RE, _validate_identifier, build_geo_filter_sql, table_name

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
# "name" is a short, stable label for this metric independent of the
# current param1/param2 values (unlike label_template) - used when
# reporting which metric(s) a missing column affects (see
# missing_columns_report), where "Disagrees with OSM by 10+ mph" would be
# a strange thing to show since the query isn't even running.
# "required_columns" is every column besides {infer_field} this metric's
# SQL references - together with whether "{infer_field}" appears in
# "sql", that's everything missing_columns_report needs to know without
# having to parse the SQL string itself.
# "magnitude_expr" is how far off/how high this metric's own condition is
# for a given row - the same left-hand-side expression "sql" compares
# against @param1/@param2, factored out so it can also drive "ORDER BY
# ... DESC" (worst offenders first) for the issue map's sampled-segments
# query (see build_sample_mismatches_query) without re-deriving it from
# "sql" or keeping a second copy that could drift out of sync.
QUALITY_METRICS = [
    {
        "key": "diff_osm",
        "name": "vs. OSM",
        "label_template": "Disagrees with OSM by {param1}+ mph",
        "sql": "ABS({infer_field} - speed_limit_osm_mph) >= @param1",
        "magnitude_expr": "ABS({infer_field} - speed_limit_osm_mph)",
        "required_columns": ["speed_limit_osm_mph"],
    },
    {
        # speed_limit_here_mph is stored as a precise km/h->mph conversion
        # (e.g. 24.860161591050343, not 25), not a clean posted-sign value
        # like the others - always ROUND() it before comparing/displaying,
        # or the noise reads as spurious mismatch magnitude.
        "key": "diff_here",
        "name": "vs. HERE",
        "label_template": "Disagrees with HERE by {param1}+ mph",
        "sql": "ABS({infer_field} - ROUND(speed_limit_here_mph)) >= @param1",
        "magnitude_expr": "ABS({infer_field} - ROUND(speed_limit_here_mph))",
        "required_columns": ["speed_limit_here_mph"],
    },
    {
        "key": "diff_speed_avg",
        "name": "vs. observed avg speed",
        "label_template": "Disagrees with observed avg speed by {param1}+ mph",
        "sql": "ABS({infer_field} - speed_AVG_mph) >= @param1",
        "magnitude_expr": "ABS({infer_field} - speed_AVG_mph)",
        "required_columns": ["speed_AVG_mph"],
    },
    {
        "key": "diff_freeflow",
        "name": "vs. freeflow speed",
        "label_template": "Disagrees with freeflow speed by {param1}+ mph",
        "sql": "ABS({infer_field} - freeflow_mph) >= @param1",
        "magnitude_expr": "ABS({infer_field} - freeflow_mph)",
        "required_columns": ["freeflow_mph"],
    },
    {
        "key": "speed_avg_high",
        "name": "observed avg speed implausible",
        "label_template": "Observed avg speed over {param2} mph (implausible)",
        "sql": "speed_AVG_mph > @param2",
        "magnitude_expr": "speed_AVG_mph",
        "required_columns": ["speed_AVG_mph"],
    },
    {
        "key": "freeflow_high",
        "name": "freeflow speed implausible",
        "label_template": "Freeflow speed over {param2} mph (implausible)",
        "sql": "freeflow_mph > @param2",
        "magnitude_expr": "freeflow_mph",
        "required_columns": ["freeflow_mph"],
    },
]

QUALITY_METRIC_KEYS = tuple(m["key"] for m in QUALITY_METRICS)

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
    zip_codes: tuple[str, ...] = ()  # see find_bad_speed_limit.build_geo_filter_sql
    counties: tuple[str, ...] = ()  # scoped to `states` when set, else matched nationwide
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
# though some of them are missing columns these metrics need (picking one
# of those now shows a friendly warning instead of a raw BigQuery error -
# see missing_columns_report).
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


def list_table_columns(project: str, dataset: str, table: str, log=lambda msg: None) -> set[str]:
    """Every column this specific table actually has - used to check
    up front whether the metrics query below could even run against it
    (see missing_columns_report), rather than finding out from a raw
    BigQuery error after the fact. Cached for SCHEMA_CACHE_TTL_SECONDS -
    see bq_cache."""
    _validate_identifier(project, PROJECT_RE, "project")
    _validate_identifier(dataset, DATASET_RE, "dataset")
    _validate_identifier(table, TABLE_RE, "table")

    def run() -> list[str]:
        client = bigquery.Client(project=project)
        query = f"""
            SELECT column_name
            FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS`
            WHERE table_name = @table
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("table", "STRING", table)])
        return [row["column_name"] for row in client.query(query, job_config=job_config).result()]

    key = cache_key("table_columns", project, dataset, table)
    return set(cached_query(key, run, ttl_seconds=SCHEMA_CACHE_TTL_SECONDS, log=log))


def missing_columns_report(available_columns: set[str], infer_field: str) -> Optional[str]:
    """None if every metric in QUALITY_METRICS could actually run against
    a table with these `available_columns` (given the selected
    `infer_field`) - otherwise a short, plain-English explanation of
    what's missing and which metric(s) it affects, meant to replace a
    raw BigQuery "Unrecognized name" error on the page: that's an
    internal detail, not something to show an end user without
    explanation or a next step. The query is one shot computing all six
    metrics together, so even one missing column blocks the whole page -
    this is checked before attempting it at all."""
    missing_overall: set[str] = set()
    affected_metrics: list[str] = []
    for m in QUALITY_METRICS:
        needed = set(m["required_columns"])
        if "{infer_field}" in m["sql"]:
            needed.add(infer_field)
        missing = needed - available_columns
        if missing:
            missing_overall |= missing
            affected_metrics.append(m["name"])

    if not missing_overall:
        return None

    columns_str = ", ".join(f"`{c}`" for c in sorted(missing_overall))
    metrics_str = ", ".join(affected_metrics)
    return (
        f"This table is missing the column(s) {columns_str}, which the {metrics_str} metric(s) need - "
        f"so none of the metrics on this page can be computed from it (they're all one query). "
        f"Pick a different table from the dropdown above - the `_details` tables "
        f"(e.g. `calc_out.speed_limits_US_<YEAR>_<MONTH>_details`) have the full set of columns these metrics need."
    )


def full_table_name(f: QualityFilters) -> str:
    """The fully-qualified table these metrics are actually computed from -
    for display, so the page is explicit about its real data source. Not
    archimedes_api.speed_limits_infer_details: that view is missing
    speed_limit_here_mph and freeflow_mph, two of the six metrics here
    depend on them, so this queries a dated nationwide _details table
    directly instead."""
    return f"{f.project}.{f.dataset}.{f.table}"


def _common_filter_where_parts(f: QualityFilters) -> tuple[list[str], list[bigquery.ScalarQueryParameter]]:
    """The states/functional_classes/zip_codes/counties portion of a
    QualityFilters' WHERE clause - shared between build_quality_query
    (the nationwide aggregate) and build_sample_mismatches_query (the
    issue map's sampled worst-offenders query), so the two can never
    silently disagree about what a given filter selection means."""
    where_parts: list[str] = []
    params: list[bigquery.ScalarQueryParameter] = []
    if f.states:
        for s in f.states:
            _validate_identifier(s, STATE_RE, "state (expected 2 letters, e.g. NC)")
        where_parts.append("state IN UNNEST(@states)")
        params.append(bigquery.ArrayQueryParameter("states", "STRING", [s.upper() for s in f.states]))
    if f.functional_classes:
        where_parts.append("functional_class IN UNNEST(@functional_classes)")
        params.append(bigquery.ArrayQueryParameter("functional_classes", "INT64", list(f.functional_classes)))
    geo_where_parts, geo_params = build_geo_filter_sql(f.zip_codes, f.counties, f.states)
    where_parts.extend(geo_where_parts)
    params.extend(geo_params)
    return where_parts, params


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
    common_where_parts, common_params = _common_filter_where_parts(f)
    where_parts.extend(common_where_parts)
    params.extend(common_params)

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


def build_sample_mismatches_query(
    f: QualityFilters, metric_key: str, limit: int,
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    """Row-level (not aggregate) query for the Quality page's issue map:
    up to `limit` real segments failing `metric_key`'s own condition,
    worst offenders first (by that metric's magnitude_expr) - "where are
    the worst mismatches, not just how many are there." Same filters
    (table/infer_field/states/functional_classes/zip_codes/counties) as
    build_quality_query, plus the one metric's own condition/threshold -
    this is a fundamentally different query shape (individual rows with
    geom, not an aggregate), not something build_quality_query itself can
    answer, which is why this is a separate function/endpoint rather than
    an option on the existing one."""
    _validate_identifier(f.project, PROJECT_RE, "project")
    _validate_identifier(f.dataset, DATASET_RE, "dataset")
    _validate_identifier(f.table, TABLE_RE, "table")
    _validate_identifier(f.infer_field, TABLE_RE, "infer_field")
    metric = next((m for m in QUALITY_METRICS if m["key"] == metric_key), None)
    if metric is None:
        raise ValueError(f"Invalid metric {metric_key!r} - must be one of {QUALITY_METRIC_KEYS}")

    where_parts = [f"{f.infer_field} IS NOT NULL", metric["sql"].format(infer_field=f.infer_field)]
    params: list[bigquery.ScalarQueryParameter] = [
        bigquery.ScalarQueryParameter("param1", "FLOAT64", float(f.param1)),
        bigquery.ScalarQueryParameter("param2", "FLOAT64", float(f.param2)),
    ]
    common_where_parts, common_params = _common_filter_where_parts(f)
    where_parts.extend(common_where_parts)
    params.extend(common_params)

    magnitude_expr = metric["magnitude_expr"].format(infer_field=f.infer_field)
    query = f"""
        SELECT
          here_segment_id, street_name, functional_class, state,
          ST_Y(ST_CENTROID(geom)) AS lat, ST_X(ST_CENTROID(geom)) AS lon,
          {f.infer_field} AS infer_value,
          {magnitude_expr} AS magnitude
        FROM `{f.project}.{f.dataset}.{f.table}`
        WHERE {' AND '.join(where_parts)}
        ORDER BY magnitude DESC
        LIMIT {int(limit)}
    """
    return query, params


def fetch_sample_mismatches(f: QualityFilters, metric_key: str, limit: int, log=lambda msg: None) -> list[dict]:
    """Runs build_sample_mismatches_query - not cached the way
    fetch_quality_metrics is (a LIMIT'd, ORDER-BY-magnitude query is
    already cheap relative to the nationwide aggregate, and caching it
    keyed on every metric/limit combination isn't worth the complexity
    for what's meant to be an on-demand "show me" click, not something
    polled or reloaded repeatedly)."""
    client = bigquery.Client(project=f.project)
    query, params = build_sample_mismatches_query(f, metric_key, limit)
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    return [dict(row.items()) for row in client.query(query, job_config=job_config).result()]


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
