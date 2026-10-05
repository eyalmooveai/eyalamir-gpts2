"""The "library of models" shown in a sidebar on every Archimedes page,
and the backend for "pick two models, compare them".

Backed directly by `calc_archive.model_registry_results` - the one plain
BigQuery TABLE (not a SNAPSHOT) in the calc_archive dataset, and the same
table BigQuery's own `calc.model_registry_*` stored procedures
(model_registry_insert_result, model_registry_resolve_table,
model_registry_browse, ...) already read and write as the canonical
record of which archived model runs exist. Deliberately reusing that
table rather than inventing a second notion of "model" here keeps this
app's idea of "models that already exist" exactly in sync with what
those procedures consider canonical - confirmed by inspecting their DDL
directly (model_registry_insert_result is what populates
archived_details_table/archived_plain_table on every archive run, and
model_registry_resolve_table is what later looks them back up by
tag/state/year/month - the same four columns this module keys a "model"
by).

A "model" here is one row of that table: a (tag, state, year_num,
month_num) archived run, e.g. tag="avgspeed_only", state="CO",
year_num=2026, month_num=8. The comparison query below mirrors the
methodology already in production use for this - both the user-supplied
example query (COUNT/COUNTIF agreement + avg abs diff in mph, joined on
here_segment_id) and calc.compare_speed_limit_model_variants'/
calc.model_registry_compare_speed_limit_model_variants' own use of the
three avgspeed_only/freeflow_only/both variants for exactly this kind of
comparison. No written spec of this methodology exists in Drive (checked
during this feature's design) - the stored procedures and the example
query are the authoritative source.
"""
from __future__ import annotations

import dataclasses
from typing import Optional

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery

from bq_sql_console import COST_CONFIRMATION_THRESHOLD_USD, QueryCostEstimate, estimate_query_cost

REGISTRY_TABLE = "calc_archive.model_registry_results"

# The columns every archived_details_table shares (confirmed by comparing
# INFORMATION_SCHEMA.COLUMNS across the avgspeed_only/freeflow_only/both/
# baseline variants of speed_limits_CO_2026_08_details - identical column
# set in every one) - safe to reference unconditionally in the comparison
# query below without a per-table existence check first.
_PREDICTION_COL = "speed_limit_infer_mph_corrected"
_HERE_COL = "speed_limit_here_mph"
_CONFIDENCE_COL = "confidence_pct"
_JOIN_COL = "here_segment_id"


@dataclasses.dataclass
class ModelEntry:
    tag: str
    state: str
    year_num: int
    month_num: int
    archived_at: Optional[str]
    archived_details_table: Optional[str]
    archived_plain_table: Optional[str]
    rows_total: Optional[int]
    predictions: Optional[int]
    errors: Optional[int]
    grievous_errors: Optional[int]

    @property
    def key(self) -> str:
        """A stable identity string the frontend round-trips to name
        which two models to compare - never a table name, so the
        frontend can't hand back an arbitrary table for the comparison
        query to interpolate (see compare_models's docstring)."""
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
            # Full dataset-qualified table names, not just the tag/state/
            # date label - shown directly in the sidebar (and used as the
            # literal value "Use this model" writes into every other
            # tab's #table/#state/#year/#month fields) since "I want to
            # see the full table names in the sidebar" was explicit.
            "archived_details_table": self.archived_details_table,
            "archived_plain_table": self.archived_plain_table,
        }


def discover_models(project: str) -> list[ModelEntry]:
    """Every model archived in calc_archive.model_registry_results -
    the live, queried-fresh-each-time answer to "which models already
    exist", so a newly archived run shows up in the sidebar without any
    code change here. Cheap: this table has a handful of rows (one per
    archived run, not per segment), so no caching is applied."""
    client = bigquery.Client(project=project)
    rows = client.query(
        f"""
        SELECT tag, state, year_num, month_num, archived_at,
               archived_details_table, archived_plain_table,
               rows_total, predictions, errors, grievous_errors
        FROM `{project}.{REGISTRY_TABLE}`
        ORDER BY tag, state, year_num DESC, month_num DESC
        """
    ).result()
    return [
        ModelEntry(
            tag=row.tag, state=row.state, year_num=row.year_num, month_num=row.month_num,
            archived_at=row.archived_at.isoformat() if row.archived_at else None,
            archived_details_table=row.archived_details_table, archived_plain_table=row.archived_plain_table,
            rows_total=row.rows_total, predictions=row.predictions, errors=row.errors,
            grievous_errors=row.grievous_errors,
        )
        for row in rows
    ]


def find_model(models: list[ModelEntry], key: str) -> Optional[ModelEntry]:
    return next((m for m in models if m.key == key), None)


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@dataclasses.dataclass
class ModelComparisonResult:
    kind: str  # "cost_estimate" | "results" | "error"
    gb: Optional[float] = None
    cost_usd: Optional[float] = None
    stats: Optional[dict] = None
    bq_error: Optional[str] = None


def build_comparison_sql(project: str, entry_a: ModelEntry, entry_b: ModelEntry) -> str:
    """The aggregate agreement/disagreement query - the same methodology
    as the example query this feature was designed from: total segments,
    how many agree/disagree on the corrected inferred speed limit, the
    average absolute difference in mph, and (since every archived_details
    table also carries HERE's own posted limit and a model confidence
    score) how each side compares against HERE and its own average
    confidence. `entry_a`/`entry_b` are always resolved server-side
    against a fresh discover_models() lookup before this is called - see
    compare_models() - never built from client-supplied table names."""
    table_a = f"`{project}.{entry_a.archived_details_table}`"
    table_b = f"`{project}.{entry_b.archived_details_table}`"
    return f"""
SELECT
  COUNT(*) AS total_segments,
  COUNTIF(a.{_PREDICTION_COL} = b.{_PREDICTION_COL}) AS agree,
  COUNTIF(a.{_PREDICTION_COL} != b.{_PREDICTION_COL}) AS disagree,
  ROUND(AVG(ABS(a.{_PREDICTION_COL} - b.{_PREDICTION_COL})), 2) AS avg_abs_diff_mph,
  COUNTIF(a.{_HERE_COL} IS NOT NULL) AS has_here,
  COUNTIF(a.{_HERE_COL} IS NOT NULL AND a.{_PREDICTION_COL} = a.{_HERE_COL}) AS a_matches_here,
  COUNTIF(a.{_HERE_COL} IS NOT NULL AND b.{_PREDICTION_COL} = a.{_HERE_COL}) AS b_matches_here,
  ROUND(AVG(a.{_CONFIDENCE_COL}), 1) AS a_avg_confidence_pct,
  ROUND(AVG(b.{_CONFIDENCE_COL}), 1) AS b_avg_confidence_pct
FROM {table_a} a
JOIN {table_b} b USING ({_JOIN_COL})
""".strip()


def compare_models(
    project: str, entry_a: ModelEntry, entry_b: ModelEntry, confirmed: bool, log=lambda msg: None,
) -> ModelComparisonResult:
    """Dry-run cost estimate first (free), same $10 reconfirmation gate
    as the SQL console (bq_sql_console.COST_CONFIRMATION_THRESHOLD_USD) -
    these archived_details_table tables are full per-segment archives
    (hundreds of thousands of rows each), so a join across two of them is
    exactly the kind of query that gate exists for. Returns
    kind="cost_estimate" (no execution) until `confirmed` is true."""
    if not entry_a.archived_details_table or not entry_b.archived_details_table:
        return ModelComparisonResult(kind="error", bq_error="One of the selected models has no archived details table to compare.")

    sql = build_comparison_sql(project, entry_a, entry_b)

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
        stats = {k: _json_safe(v) for k, v in dict(row).items()}
        log(f"Compared models {entry_a.key} vs {entry_b.key} ({estimate.gb:.3f} GB, ${estimate.cost_usd:.4f})")
        return ModelComparisonResult(kind="results", gb=estimate.gb, cost_usd=estimate.cost_usd, stats=stats)
    except GoogleAPICallError as e:
        log(f"Model comparison query failed: {e}")
        return ModelComparisonResult(kind="error", bq_error=str(e))
