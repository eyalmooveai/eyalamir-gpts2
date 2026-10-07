"""The Model Registry Compare panel: browse `calc_archive.model_registry_results`
(via BigQuery's own `calc.model_registry_list_*` procedures) by state, then
period, then pick 2-3 archived tags to compare on a shared numeric column.

Each step narrows the next so nothing ever lists the full registry table -
this matters once it grows past thousands of rows, which is exactly why
these list_states/list_periods/list_tags procedures exist (built
specifically for this panel - see calc.INFORMATION_SCHEMA.ROUTINES for
their DDL) rather than this module doing one `SELECT * FROM
model_registry_results` the way an earlier version of this panel did.

Two deliberate departures from what was originally proposed for this
panel, both confirmed live against BigQuery before deciding, not guessed:

1. `calc.model_registry_list_common_columns` does NOT exist (checked via
   `calc.INFORMATION_SCHEMA.ROUTINES` - only list_states/list_periods/
   list_tags/compare_models are actually deployed). list_common_columns()
   below computes the same thing directly via a per-table
   INFORMATION_SCHEMA.COLUMNS query instead of calling a procedure that
   isn't there - cheap, since it's always scoped to the 2-3 specific
   archived tables someone just picked, never a dataset-wide scan.

2. compare_models() here does NOT call `calc.model_registry_compare_models`.
   That procedure's final result (one row per here_segment_id) comes from
   an EXECUTE IMMEDIATE-constructed query inside calc.model_registry_relate_columns
   - confirmed live that a dry run of `CALL model_registry_compare_models(...)`
   reports total_bytes_processed=0, because a dry run can't see through
   dynamic SQL built at runtime. That makes it impossible to give an
   honest cost estimate before running it, which this app's standing rule
   requires for anything that scans a 700K-35M row archived table (see
   bq_sql_console.py). It would also hand back raw per-segment rows this
   panel must never materialize client-side (explicitly warned against,
   given table sizes up to 35M rows) - meaning a second query to aggregate
   them would be needed regardless. build_pairwise_comparison_sql() below
   builds the one literal, dry-runnable, already-aggregated query instead -
   same join-on-here_segment_id methodology model_registry_relate_columns
   itself implements, just written directly rather than through a wrapper
   whose cost this app can't see in advance.
"""
from __future__ import annotations

import dataclasses
from typing import Optional

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery

from bq_sql_console import COST_CONFIRMATION_THRESHOLD_USD, estimate_query_cost

REGISTRY_TABLE = "calc_archive.model_registry_results"

MAX_COMPARE_MODELS = 3
_ALIASES = ["a", "b", "c"]
_JOIN_COL = "here_segment_id"

# speed_limit_here_mph is stored as an unrounded km/h->mph conversion
# (e.g. 24.860161591050343) while every other speed column here is
# already a clean value - comparing it unrounded reads as spurious
# mismatch noise. Same fix this app already applies everywhere else this
# column is used (quality_metrics.py/find_bad_speed_limit.py) - see
# CLAUDE.md for the real-numbers confirmation of why this matters.
_NEEDS_ROUNDING = {"speed_limit_here_mph"}

# Only these BigQuery column types support "agree %"/"avg abs diff" -
# excludes identifiers (STRING, e.g. here_segment_id itself),
# GEOGRAPHY/BOOL, and anything else that isn't a plain number.
_COMPARABLE_DATA_TYPES = {"INT64", "FLOAT64", "NUMERIC", "BIGNUMERIC"}


def _call_rows(project: str, sql: str, params: Optional[list] = None) -> list:
    client = bigquery.Client(project=project)
    job_config = bigquery.QueryJobConfig(query_parameters=params or [])
    return list(client.query(sql, job_config=job_config).result())


@dataclasses.dataclass
class StateEntry:
    state: str
    run_count: int

    def to_json(self) -> dict:
        return {"state": self.state, "run_count": self.run_count}


@dataclasses.dataclass
class PeriodEntry:
    year_num: int
    month_num: int
    tag_count: int

    @property
    def key(self) -> str:
        return f"{self.year_num}-{self.month_num:02d}"

    def to_json(self) -> dict:
        return {
            "year_num": self.year_num, "month_num": self.month_num,
            "tag_count": self.tag_count, "key": self.key,
            "comparable": self.tag_count >= 2,
        }


@dataclasses.dataclass
class ModelEntry:
    tag: str
    state: str
    year_num: int
    month_num: int
    rows_total: Optional[int]
    archived_plain_table: Optional[str]
    archived_details_table: Optional[str]
    archived_at: Optional[str] = None
    predictions: Optional[int] = None
    errors: Optional[int] = None
    grievous_errors: Optional[int] = None

    @property
    def key(self) -> str:
        """A stable identity string the frontend round-trips - never a
        table name. The server always re-resolves it against a fresh
        list_tags(state, year_num, month_num) call before building any
        SQL (see compare_models), so a stale/forged key just fails the
        lookup instead of ever reaching a query."""
        return f"{self.tag}|{self.state}|{self.year_num}|{self.month_num}"

    @property
    def label(self) -> str:
        return f"{self.tag} · {self.state} {self.year_num}-{self.month_num:02d}"

    @property
    def error_rate(self) -> Optional[float]:
        if not self.predictions:
            return None
        return (self.errors or 0) / self.predictions

    @property
    def grievous_rate(self) -> Optional[float]:
        if not self.predictions:
            return None
        return (self.grievous_errors or 0) / self.predictions

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "tag": self.tag,
            "state": self.state,
            "year_num": self.year_num,
            "month_num": self.month_num,
            "archived_at": self.archived_at,
            "rows_total": self.rows_total,
            "predictions": self.predictions,
            "errors": self.errors,
            "grievous_errors": self.grievous_errors,
            "error_rate": self.error_rate,
            "grievous_rate": self.grievous_rate,
            "has_details": bool(self.archived_details_table),
            "archived_details_table": self.archived_details_table,
            "archived_plain_table": self.archived_plain_table,
        }


def list_states(project: str) -> list[StateEntry]:
    """Step 1 - CALL calc.model_registry_list_states(), grouped+counted
    server-side already, never a per-row scan of the registry."""
    rows = _call_rows(project, "CALL calc.model_registry_list_states()")
    return [StateEntry(state=r.state, run_count=r.run_count) for r in rows]


def list_periods(project: str, state: str) -> list[PeriodEntry]:
    """Step 2 - CALL calc.model_registry_list_periods(@p_state), scoped
    to one state. tag_count < 2 means nothing to compare for that
    period - the UI disables it rather than hiding it, so it's obvious
    why."""
    rows = _call_rows(
        project, "CALL calc.model_registry_list_periods(@p_state)",
        [bigquery.ScalarQueryParameter("p_state", "STRING", state)],
    )
    return [PeriodEntry(year_num=r.year_num, month_num=r.month_num, tag_count=r.tag_count) for r in rows]


def list_tags(project: str, state: str, year_num: int, month_num: int) -> list[ModelEntry]:
    """Step 3 - CALL calc.model_registry_list_tags(@p_state, @p_year_num,
    @p_month_num), scoped to one state+period (at most a handful of
    rows - one per archived tag). Enriched with each tag's
    predictions/errors/grievous_errors/archived_at via one small
    follow-up query against model_registry_results itself, filtered by
    the same state+period+tags - still never a full-table scan, since
    it's WHERE-scoped to at most a handful of rows."""
    rows = _call_rows(
        project, "CALL calc.model_registry_list_tags(@p_state, @p_year_num, @p_month_num)",
        [
            bigquery.ScalarQueryParameter("p_state", "STRING", state),
            bigquery.ScalarQueryParameter("p_year_num", "INT64", year_num),
            bigquery.ScalarQueryParameter("p_month_num", "INT64", month_num),
        ],
    )
    entries = [
        ModelEntry(
            tag=r.tag, state=state, year_num=year_num, month_num=month_num,
            rows_total=r.rows_total, archived_plain_table=r.archived_plain_table,
            archived_details_table=r.archived_details_table,
        )
        for r in rows
    ]
    if entries:
        _enrich_with_quality_stats(project, entries)
    return entries


def _enrich_with_quality_stats(project: str, entries: list[ModelEntry]) -> None:
    state, year_num, month_num = entries[0].state, entries[0].year_num, entries[0].month_num
    client = bigquery.Client(project=project)
    rows = client.query(
        f"""
        SELECT tag, archived_at, predictions, errors, grievous_errors
        FROM `{project}.{REGISTRY_TABLE}`
        WHERE state = @p_state AND year_num = @p_year_num AND month_num = @p_month_num
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("p_state", "STRING", state),
            bigquery.ScalarQueryParameter("p_year_num", "INT64", year_num),
            bigquery.ScalarQueryParameter("p_month_num", "INT64", month_num),
        ]),
    ).result()
    by_tag = {r.tag: r for r in rows}
    for entry in entries:
        stats = by_tag.get(entry.tag)
        if stats is None:
            continue
        entry.archived_at = stats.archived_at.isoformat() if stats.archived_at else None
        entry.predictions = stats.predictions
        entry.errors = stats.errors
        entry.grievous_errors = stats.grievous_errors


def find_model(models: list[ModelEntry], key: str) -> Optional[ModelEntry]:
    return next((m for m in models if m.key == key), None)


def list_common_columns(project: str, tables: list[str]) -> list[str]:
    """Step 4's options - numeric columns common to every given
    dataset-qualified table name (2 or 3 archived_details_table values).
    See the module docstring for why this is computed directly instead
    of calling calc.model_registry_list_common_columns (not deployed)."""
    if not tables:
        return []
    client = bigquery.Client(project=project)
    column_sets: list[dict[str, str]] = []
    for table in tables:
        dataset_id, table_id = table.split(".", 1)
        rows = client.query(
            f"SELECT column_name, data_type FROM `{project}.{dataset_id}`.INFORMATION_SCHEMA.COLUMNS "
            f"WHERE table_name = @p_table",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("p_table", "STRING", table_id),
            ]),
        ).result()
        column_sets.append({r.column_name: r.data_type for r in rows})
    common = set(column_sets[0])
    for s in column_sets[1:]:
        common &= set(s)
    return sorted(c for c in common if column_sets[0][c] in _COMPARABLE_DATA_TYPES)


def _column_expr(alias: str, column: str) -> str:
    if column in _NEEDS_ROUNDING:
        return f"ROUND({alias}.{column})"
    return f"{alias}.{column}"


def build_pairwise_comparison_sql(project: str, column: str, entries: list[ModelEntry]) -> str:
    """One query, joining 2 or 3 archived_details_table on
    here_segment_id, computing every pair's agreement %% and average
    absolute difference on `column` in a single pass - the same
    COUNTIF/AVG(ABS(...)) methodology as the original example query this
    whole feature was designed from, generalized to any chosen column
    and to 3-way as well as 2-way comparisons. `entries` are always
    resolved server-side against a fresh list_tags() call and `column`
    against a fresh list_common_columns() call before this is ever
    called - see compare_models() - never built from unchecked
    client-supplied strings."""
    n = len(entries)
    assert 2 <= n <= MAX_COMPARE_MODELS, f"need 2-{MAX_COMPARE_MODELS} models, got {n}"
    aliases = _ALIASES[:n]

    from_clause = f"`{project}.{entries[0].archived_details_table}` {aliases[0]}"
    join_clauses = "\n".join(
        f"JOIN `{project}.{entry.archived_details_table}` {alias} USING ({_JOIN_COL})"
        for entry, alias in zip(entries[1:], aliases[1:])
    )

    pair_exprs = []
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            ai, aj = aliases[i], aliases[j]
            ei, ej = _column_expr(ai, column), _column_expr(aj, column)
            pair_exprs.append(f"COUNTIF({ei} = {ej}) AS agree_{ai}{aj}")
            pair_exprs.append(f"ROUND(AVG(ABS({ei} - {ej})), 2) AS avg_abs_diff_{ai}{aj}")
            pairs.append((ai, aj))

    select_list = ["COUNT(*) AS total_segments"] + pair_exprs
    sql = "SELECT\n  " + ",\n  ".join(select_list) + f"\nFROM {from_clause}\n{join_clauses}"
    return sql


@dataclasses.dataclass
class PairResult:
    tag_a: str
    tag_b: str
    agree_count: int
    avg_abs_diff: Optional[float]

    def to_json(self, total_segments: int) -> dict:
        agree_pct = (self.agree_count / total_segments) if total_segments else None
        return {
            "tag_a": self.tag_a, "tag_b": self.tag_b,
            "agree_count": self.agree_count, "agree_pct": agree_pct,
            "avg_abs_diff": self.avg_abs_diff,
        }


@dataclasses.dataclass
class ModelComparisonResult:
    kind: str  # "cost_estimate" | "results" | "error"
    gb: Optional[float] = None
    cost_usd: Optional[float] = None
    total_segments: Optional[int] = None
    pairs: Optional[list[PairResult]] = None
    bq_error: Optional[str] = None


def compare_models(
    project: str, column: str, entries: list[ModelEntry], confirmed: bool, log=lambda msg: None,
) -> ModelComparisonResult:
    """Dry-run cost estimate first (free, and - unlike CALLing
    model_registry_compare_models directly - accurate, since this is a
    literal query BigQuery can actually analyze), same $10
    reconfirmation gate as the SQL console
    (bq_sql_console.COST_CONFIRMATION_THRESHOLD_USD). Returns
    kind="cost_estimate" (no execution) until `confirmed` is true."""
    missing = [e.label for e in entries if not e.archived_details_table]
    if missing:
        return ModelComparisonResult(kind="error", bq_error=f"No archived details table for: {', '.join(missing)}.")

    sql = build_pairwise_comparison_sql(project, column, entries)

    try:
        estimate = estimate_query_cost(project, sql)
    except GoogleAPICallError as e:
        log(f"Model comparison dry run failed: {e}")
        return ModelComparisonResult(kind="error", bq_error=str(e))

    if estimate.cost_usd > COST_CONFIRMATION_THRESHOLD_USD and not confirmed:
        return ModelComparisonResult(kind="cost_estimate", gb=estimate.gb, cost_usd=estimate.cost_usd)

    try:
        client = bigquery.Client(project=project)
        row = next(iter(client.query(sql).result()))
        row_dict = dict(row)
    except GoogleAPICallError as e:
        log(f"Model comparison query failed: {e}")
        return ModelComparisonResult(kind="error", bq_error=str(e))

    total_segments = row_dict["total_segments"]
    aliases = _ALIASES[:len(entries)]
    pairs = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            ai, aj = aliases[i], aliases[j]
            pairs.append(PairResult(
                tag_a=entries[i].tag, tag_b=entries[j].tag,
                agree_count=row_dict[f"agree_{ai}{aj}"],
                avg_abs_diff=row_dict[f"avg_abs_diff_{ai}{aj}"],
            ))
    log(f"Compared {[e.key for e in entries]} on {column} ({estimate.gb:.3f} GB, ${estimate.cost_usd:.4f})")
    return ModelComparisonResult(kind="results", gb=estimate.gb, cost_usd=estimate.cost_usd, total_segments=total_segments, pairs=pairs)
