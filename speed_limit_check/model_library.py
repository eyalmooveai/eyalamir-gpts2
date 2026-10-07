"""The Model Registry Compare panel: browse `calc_archive.model_registry_results`
(via BigQuery's own `calc.model_registry_list_*`/`compare_models`
procedures) by state, then period, then pick 2-3 archived tags to
compare on a shared numeric column.

Each step narrows the next so nothing ever lists the full registry table -
this matters once it grows past thousands of rows, which is exactly why
these list_states/list_periods/list_tags procedures exist (built
specifically for this panel - see calc.INFORMATION_SCHEMA.ROUTINES for
their DDL) rather than this module doing one `SELECT * FROM
model_registry_results` the way an earlier version of this panel did.

Two things worth knowing about calc.model_registry_compare_models,
confirmed live against BigQuery rather than assumed:

1. It always needs THREE model identifiers (A, B, and a "relate" pivot),
   even for a plain two-way comparison - for a pair, this module passes
   B's identifiers again as the "relate" slot and simply ignores
   `value_relate` in the response (the same workaround its own author
   documented). For 2-3 selected models, compare_models() below calls
   this procedure once per PAIR among them (one call for 2 models, three
   calls for 3), since the procedure itself only ever compares two
   models at a time.

2. Its real query is built at runtime (EXECUTE IMMEDIATE, inside
   calc.model_registry_relate_columns) - a dry run of `CALL
   model_registry_compare_models(...)` reports total_bytes_processed=0,
   confirmed live, because a dry run can't see through dynamic SQL
   constructed after the fact. That makes a cost estimate BEFORE running
   it impossible - unlike every other BigQuery call in this app
   (bq_sql_console.py's $10 dry-run/confirm gate doesn't apply here for
   that reason, not because this query is assumed cheap). Instead, this
   module runs the comparison directly and reports the ACTUAL bytes
   billed/cost from the completed job afterward - real numbers from a
   real run, just not a prediction beforehand.

Its result (one row per here_segment_id, up to tens of millions of
rows for a nationwide comparison) is also never held in memory as a
list - _call_compare_pair() streams the BigQuery RowIterator's pages
and keeps only a handful of running totals (count, agreement count, sum
of absolute differences), the same "aggregate, don't materialize"
principle the earlier direct-SQL version of this module applied via
GROUP BY, just computed in Python here since the procedure's own output
is unaggregated per-segment rows.
"""
from __future__ import annotations

import dataclasses
from typing import Optional

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery

from bq_sql_console import BQ_ON_DEMAND_PRICE_PER_TIB_USD

REGISTRY_TABLE = "calc_archive.model_registry_results"
_BYTES_PER_GB = 1024 ** 3
_BYTES_PER_TIB = 1024 ** 4

MAX_COMPARE_MODELS = 3

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

# A numeric-typed common column still isn't necessarily something worth
# comparing - an archived_details_table also carries columns like
# functional_class/judgement_over_osm/t12_sp/t3_sp/t40m_sp/t60p_sp
# (internal thresholds/classifications, not speed metrics), which made
# the "Column to compare" picker cluttered and confusing in practice
# (real user feedback on the live UI). Narrowed to the columns this app
# already treats as real comparison metrics elsewhere -
# quality_metrics.QUALITY_METRICS' own `required_columns` entries
# (speed_limit_osm_mph/speed_limit_here_mph/speed_AVG_mph/freeflow_mph)
# plus the inferred-speed and confidence columns the original
# (pre-redesign) version of this module already singled out - rather
# than a type-only filter or a newly-invented list.
_METRIC_COLUMNS = {
    "speed_limit_infer_mph_corrected", "speed_limit_infer_mph",
    "speed_limit_osm_mph", "speed_limit_here_mph",
    "speed_AVG_mph", "freeflow_mph", "confidence_pct",
}


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
    is_backup: bool = False

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
            "is_backup": self.is_backup,
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
    """Step 3 - which tags exist for this state+period, and the one
    CURRENTLY USABLE row for each - "usable" meaning its
    archived_details_table still actually exists in BigQuery, not just
    that a registry row claims it does.

    `calc.model_registry_list_tags` is a plain SELECT with no DISTINCT/
    QUALIFY, so it returns one row per REGISTRY ROW, not one per tag -
    and `calc_archive.model_registry_results` is append-only
    (`calc.model_registry_insert_result` does a plain INSERT, confirmed
    by reading its DDL; nothing in this registry's design enforces
    uniqueness on tag/state/year/month). Re-archiving the same
    tag/period a second time - confirmed live for CO 2026-08's
    `avgspeed_only`, which has two rows with different `archived_at`
    but the IDENTICAL `archived_details_table` (the table name is
    deterministic, so a re-archive overwrites the same physical table,
    not a new one) - means the procedure can and does hand back the
    same tag twice. That duplicate showed up directly in the live UI as
    two identical "avgspeed_only" checkboxes, and indirectly broke
    /models/columns and /models/compare: both re-resolve the client's
    selected tags against a fresh list_tags() call and reject the
    request if the resolved count doesn't match the number of tags
    asked for - which a duplicate tag always triggers (2 tags selected,
    3 entries resolved) - though that failure was invisible in the UI
    too, see _model_library_sidebar.html's loadColumns()/runModelCompare()
    docstrings for the matching frontend-side fix.

    Explicit follow-up instruction: "if a registry [row] is of an older
    version, then we should also include the current (backed up)
    location if it exists. Otherwise, there's no way to use this model,
    so listing it is useless to the user." So this doesn't just trust
    `ORDER BY archived_at DESC LIMIT 1` the way
    calc.model_registry_resolve_table does - a registry row is a claim,
    not a guarantee, and these are BigQuery table SNAPSHOTs (confirmed
    via INFORMATION_SCHEMA.TABLES - table_type='SNAPSHOT', not a plain
    TABLE), which can expire or be cleaned up independently of the
    registry row that still names them. For every tag this fetches
    EVERY candidate row (newest archived_at first), checks which
    candidates' archived_details_table actually still exists
    (_existing_tables(), one INFORMATION_SCHEMA.TABLES query covering
    every candidate across every tag at once), and picks the first
    (most recent) one that does - `is_backup=True` on the ModelEntry
    whenever that's NOT the newest candidate, so the UI can say so
    rather than silently presenting an older archive as if it were
    current. A tag with NO existing candidate at all is dropped from
    the list entirely - exactly the "otherwise listing it is useless"
    case."""
    tag_rows = _call_rows(
        project, "CALL calc.model_registry_list_tags(@p_state, @p_year_num, @p_month_num)",
        [
            bigquery.ScalarQueryParameter("p_state", "STRING", state),
            bigquery.ScalarQueryParameter("p_year_num", "INT64", year_num),
            bigquery.ScalarQueryParameter("p_month_num", "INT64", month_num),
        ],
    )
    tags = {r.tag for r in tag_rows}
    if not tags:
        return []

    client = bigquery.Client(project=project)
    rows = list(client.query(
        f"""
        SELECT tag, archived_at, predictions, errors, grievous_errors,
               rows_total, archived_plain_table, archived_details_table
        FROM `{project}.{REGISTRY_TABLE}`
        WHERE state = @p_state AND year_num = @p_year_num AND month_num = @p_month_num
        ORDER BY tag, archived_at DESC
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("p_state", "STRING", state),
            bigquery.ScalarQueryParameter("p_year_num", "INT64", year_num),
            bigquery.ScalarQueryParameter("p_month_num", "INT64", month_num),
        ]),
    ).result())

    candidates_by_tag: dict[str, list] = {}
    for r in rows:
        if r.tag in tags:
            candidates_by_tag.setdefault(r.tag, []).append(r)

    all_tables = {r.archived_details_table for r in rows if r.archived_details_table}
    existing = _existing_tables(project, all_tables)

    entries = []
    for tag, candidates in candidates_by_tag.items():
        usable = next((i for i, r in enumerate(candidates) if r.archived_details_table in existing), None)
        if usable is None:
            continue  # not one candidate's table still exists - nothing usable to list
        r = candidates[usable]
        entries.append(ModelEntry(
            tag=tag, state=state, year_num=year_num, month_num=month_num,
            rows_total=r.rows_total, archived_plain_table=r.archived_plain_table,
            archived_details_table=r.archived_details_table,
            archived_at=r.archived_at.isoformat() if r.archived_at else None,
            predictions=r.predictions, errors=r.errors, grievous_errors=r.grievous_errors,
            is_backup=usable > 0,
        ))
    return entries


def _existing_tables(project: str, table_refs: set[str]) -> set[str]:
    """Which of these `dataset.table` names currently exist (as either a
    live TABLE or a SNAPSHOT - confirmed both surface in
    INFORMATION_SCHEMA.TABLES, e.g. model_registry_results itself is
    BASE TABLE while every archived_details_table is SNAPSHOT). Grouped
    by dataset since INFORMATION_SCHEMA.TABLES is a per-dataset view -
    in practice everything here is `calc_archive`, but this doesn't
    assume that."""
    if not table_refs:
        return set()
    client = bigquery.Client(project=project)
    by_dataset: dict[str, list[str]] = {}
    for ref in table_refs:
        dataset_id, table_id = ref.split(".", 1)
        by_dataset.setdefault(dataset_id, []).append(table_id)
    existing = set()
    for dataset_id, table_ids in by_dataset.items():
        rows = client.query(
            f"SELECT table_name FROM `{project}.{dataset_id}`.INFORMATION_SCHEMA.TABLES "
            f"WHERE table_name IN UNNEST(@p_tables)",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ArrayQueryParameter("p_tables", "STRING", table_ids),
            ]),
        ).result()
        existing.update(f"{dataset_id}.{r.table_name}" for r in rows)
    return existing


def find_model(models: list[ModelEntry], key: str) -> Optional[ModelEntry]:
    return next((m for m in models if m.key == key), None)


def list_common_columns(project: str, entries: list[ModelEntry]) -> list[str]:
    """Step 4's options - numeric columns common to every given model's
    archived_details_table. Calls the now-deployed
    calc.model_registry_list_common_columns for the raw common-column-
    name intersection (confirmed live via calc.INFORMATION_SCHEMA.ROUTINES -
    an earlier version of this module computed that intersection itself
    because the procedure didn't exist yet; it does now, so this calls
    it instead of duplicating its UNION ALL/HAVING COUNT(DISTINCT...)
    logic). That procedure returns every common column regardless of
    type (here_segment_id, geom, street_name, ... included), so this
    narrows the result to numeric types with one more
    INFORMATION_SCHEMA.COLUMNS query against a single resolved table -
    every archived_details_table in the same pipeline family shares the
    same column types, so checking just one is enough."""
    if not entries:
        return []
    state, year_num, month_num = entries[0].state, entries[0].year_num, entries[0].month_num
    tags = [e.tag for e in entries]
    rows = _call_rows(
        project,
        "CALL calc.model_registry_list_common_columns(@p_state, @p_year_num, @p_month_num, @p_tags, TRUE)",
        [
            bigquery.ScalarQueryParameter("p_state", "STRING", state),
            bigquery.ScalarQueryParameter("p_year_num", "INT64", year_num),
            bigquery.ScalarQueryParameter("p_month_num", "INT64", month_num),
            bigquery.ArrayQueryParameter("p_tags", "STRING", tags),
        ],
    )
    common_names = {r.column_name for r in rows}
    if not common_names:
        return []

    dataset_id, table_id = entries[0].archived_details_table.split(".", 1)
    client = bigquery.Client(project=project)
    type_rows = client.query(
        f"SELECT column_name, data_type FROM `{project}.{dataset_id}`.INFORMATION_SCHEMA.COLUMNS "
        f"WHERE table_name = @p_table",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("p_table", "STRING", table_id),
        ]),
    ).result()
    return sorted(
        r.column_name for r in type_rows
        if r.column_name in common_names and r.data_type in _COMPARABLE_DATA_TYPES
        and r.column_name in _METRIC_COLUMNS
    )


@dataclasses.dataclass
class PairResult:
    tag_a: str
    tag_b: str
    total_segments: int
    agree_count: int
    avg_abs_diff: Optional[float]
    bytes_billed: int

    def to_json(self) -> dict:
        agree_pct = (self.agree_count / self.total_segments) if self.total_segments else None
        return {
            "tag_a": self.tag_a, "tag_b": self.tag_b, "total_segments": self.total_segments,
            "agree_count": self.agree_count, "agree_pct": agree_pct,
            "avg_abs_diff": self.avg_abs_diff,
        }


@dataclasses.dataclass
class ModelComparisonResult:
    kind: str  # "results" | "error"
    gb: Optional[float] = None
    cost_usd: Optional[float] = None
    pairs: Optional[list[PairResult]] = None
    bq_error: Optional[str] = None


def _call_compare_pair(project: str, column: str, entry_a: ModelEntry, entry_b: ModelEntry) -> PairResult:
    """CALLs calc.model_registry_compare_models for one pair - B's
    identifiers doubled into the required third "relate" slot (ignoring
    value_relate in the response), per the module docstring. Streams the
    result's pages rather than materializing them into a list, keeping
    only the running totals needed for the agree-count/avg-abs-diff this
    panel actually shows."""
    params = [
        bigquery.ScalarQueryParameter("p_tag_a", "STRING", entry_a.tag),
        bigquery.ScalarQueryParameter("p_state_a", "STRING", entry_a.state),
        bigquery.ScalarQueryParameter("p_year_a", "INT64", entry_a.year_num),
        bigquery.ScalarQueryParameter("p_month_a", "INT64", entry_a.month_num),
        bigquery.ScalarQueryParameter("p_column_a", "STRING", column),
        bigquery.ScalarQueryParameter("p_use_details_a", "BOOL", True),
        bigquery.ScalarQueryParameter("p_tag_b", "STRING", entry_b.tag),
        bigquery.ScalarQueryParameter("p_state_b", "STRING", entry_b.state),
        bigquery.ScalarQueryParameter("p_year_b", "INT64", entry_b.year_num),
        bigquery.ScalarQueryParameter("p_month_b", "INT64", entry_b.month_num),
        bigquery.ScalarQueryParameter("p_column_b", "STRING", column),
        bigquery.ScalarQueryParameter("p_use_details_b", "BOOL", True),
        bigquery.ScalarQueryParameter("p_relate_tag", "STRING", entry_b.tag),
        bigquery.ScalarQueryParameter("p_relate_state", "STRING", entry_b.state),
        bigquery.ScalarQueryParameter("p_relate_year", "INT64", entry_b.year_num),
        bigquery.ScalarQueryParameter("p_relate_month", "INT64", entry_b.month_num),
        bigquery.ScalarQueryParameter("p_relate_column", "STRING", column),
        bigquery.ScalarQueryParameter("p_use_details_relate", "BOOL", True),
    ]
    sql = """CALL calc.model_registry_compare_models(
        @p_tag_a, @p_state_a, @p_year_a, @p_month_a, @p_column_a, @p_use_details_a,
        @p_tag_b, @p_state_b, @p_year_b, @p_month_b, @p_column_b, @p_use_details_b,
        @p_relate_tag, @p_relate_state, @p_relate_year, @p_relate_month, @p_relate_column, @p_use_details_relate)"""
    client = bigquery.Client(project=project)
    job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))

    needs_round = column in _NEEDS_ROUNDING
    total = 0
    agree = 0
    diff_sum = 0.0
    diff_count = 0
    for row in job.result():
        total += 1
        va, vb = row.value_a, row.value_b
        if va is None or vb is None:
            continue
        if needs_round:
            va, vb = round(va), round(vb)
        if va == vb:
            agree += 1
        diff_sum += abs(va - vb)
        diff_count += 1

    avg_abs_diff = round(diff_sum / diff_count, 2) if diff_count else None
    return PairResult(
        tag_a=entry_a.tag, tag_b=entry_b.tag, total_segments=total, agree_count=agree,
        avg_abs_diff=avg_abs_diff, bytes_billed=job.total_bytes_billed or 0,
    )


def compare_models(project: str, column: str, entries: list[ModelEntry], log=lambda msg: None) -> ModelComparisonResult:
    """Runs one calc.model_registry_compare_models CALL per pair among
    2-3 selected models - no pre-flight cost estimate/confirmation (see
    the module docstring for why that isn't possible here), just the
    real cost of each call, summed and reported once every pair has run."""
    missing = [e.label for e in entries if not e.archived_details_table]
    if missing:
        return ModelComparisonResult(kind="error", bq_error=f"No archived details table for: {', '.join(missing)}.")

    pairs = []
    total_bytes_billed = 0
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            try:
                pair = _call_compare_pair(project, column, entries[i], entries[j])
            except GoogleAPICallError as e:
                log(f"Model comparison failed on {entries[i].tag} vs {entries[j].tag}: {e}")
                return ModelComparisonResult(kind="error", bq_error=str(e))
            total_bytes_billed += pair.bytes_billed
            pairs.append(pair)

    gb = total_bytes_billed / _BYTES_PER_GB
    cost_usd = (total_bytes_billed / _BYTES_PER_TIB) * BQ_ON_DEMAND_PRICE_PER_TIB_USD
    log(f"Compared {[e.key for e in entries]} on {column} ({gb:.3f} GB, ${cost_usd:.4f} actual)")
    return ModelComparisonResult(kind="results", gb=gb, cost_usd=cost_usd, pairs=pairs)
