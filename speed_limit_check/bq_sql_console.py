"""Detects when the Custom test box's text is a full SQL statement (not
the narrow single-comparison grammar custom_metrics.py validates) and
handles it on its own track: a BigQuery dry-run cost estimate shown
before any real execution, a second confirmation once that estimate
crosses COST_CONFIRMATION_THRESHOLD_USD, privileged-only CALL (stored
procedure) execution, and - when BigQuery rejects the SQL as malformed -
a Gemini-powered fix suggestion grounded in the project's real schema
and callable procedures.

This is a DIFFERENT trust model from custom_metrics.py's hand-rolled
grammar: that module exists to protect against untrusted/ambiguous text
turning into SQL the person typing it didn't intend - the final SQL is
re-serialized from a validated parse tree, never the original text.
Here, by contrast, the person has explicitly written real SQL they
intend to run themselves; the open questions are authorization (should
THIS person be allowed to run a stored procedure that might mutate
data?) and cost (how much will this bill?), not injection. Never
conflate the two, and never route text through this module that didn't
already look like a real SQL statement (see sql_statement_kind()) - a
narrow comparison like "speed_AVG_mph > 60" must keep going through
custom_metrics.py's validator, not this one.
"""
from __future__ import annotations

import dataclasses
import re
import time
from typing import Optional

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery
from pydantic import BaseModel

import gemini_client

# Emails allowed to CALL a stored procedure. CALL can run arbitrary
# procedural SQL including DML (INSERT/UPDATE/DELETE/MERGE) - unlike a
# read-only SELECT/WITH (gated only by cost, not by who's asking), this
# is an explicit allowlist, not "anyone with IAP access to this tool".
# Hardcoded rather than an env var/config file since changing who can run
# procedures against production data is a deliberate, rare decision that
# should show up as a reviewed code change, not a silent config edit.
PRIVILEGED_SQL_USERS = frozenset({"eyal@moove.ai", "justin@moove.ai"})

# BigQuery's on-demand analysis pricing at the time this was written -
# verify against the current BigQuery pricing page
# (cloud.google.com/bigquery/pricing) before trusting this for real
# budgeting if it's been a while; this app has no live pricing API call
# to keep it honest automatically, and Google has changed this number
# before.
BQ_ON_DEMAND_PRICE_PER_TIB_USD = 6.25
_BYTES_PER_GB = 1024 ** 3
_BYTES_PER_TIB = 1024 ** 4

# Above this estimated cost, the box requires an explicit second
# confirmation before actually running the query.
COST_CONFIRMATION_THRESHOLD_USD = 10.0

# A result set can be far larger than anyone actually wants rendered in a
# browser tab - caps the SELECT/WITH results path independently of the
# narrow comparison path's own "up to 300 sample points" cap
# (quality_metrics.PREVIEW_MAX_SEGMENTS), since this is a different,
# general-purpose results table, not a map of segments.
MAX_RESULT_ROWS = 500

# A project's schema doesn't change often enough to justify a fresh
# INFORMATION_SCHEMA scan on every single malformed-query fix request -
# same TTL-cache pattern quality_metrics.py already uses for its own
# (much narrower) schema lookups.
SCHEMA_CACHE_TTL_SECONDS = 300
_MAX_SCHEMA_TEXT_CHARS = 60_000  # keeps the fix-advice prompt bounded even for a project with many datasets/tables


# --- Detecting a real SQL statement (routing only, not a safety check) --

_LEADING_KEYWORD_RE = re.compile(r"^\s*(?:--[^\n]*\n\s*)*\(*\s*([A-Za-z]+)", re.IGNORECASE)
_SELECT_LIKE_KEYWORDS = frozenset({"SELECT", "WITH"})
_CALL_KEYWORD = "CALL"


def sql_statement_kind(text: str) -> Optional[str]:
    """Returns "select" if `text` looks like a real SQL SELECT/WITH
    query, "call" if it looks like a CALL to a stored procedure, or None
    if it doesn't look like a SQL statement at all (e.g. a narrow
    comparison like "speed_AVG_mph > 60", or plain English). A
    first-keyword check, not a parser - this only decides ROUTING (does
    this text go through custom_metrics.py's narrow-grammar/LLM-classify
    path, or this module's dry-run/cost-gate/execute path). It is
    deliberately NOT a safety boundary the way validate_custom_expression()
    is: anyone submitting SQL here is explicitly asking to run SQL they
    themselves wrote, not having free text turned into SQL on their
    behalf, so there's nothing to defend against by hardening this regex -
    see the module docstring."""
    m = _LEADING_KEYWORD_RE.match(text or "")
    if not m:
        return None
    keyword = m.group(1).upper()
    if keyword in _SELECT_LIKE_KEYWORDS:
        return "select"
    if keyword == _CALL_KEYWORD:
        return "call"
    return None


# --- Cost estimation (a free BigQuery dry run - validates + reports bytes
# processed without running or billing anything) ------------------------

@dataclasses.dataclass
class QueryCostEstimate:
    bytes_processed: int
    gb: float
    cost_usd: float


def estimate_query_cost(project: str, sql: str) -> QueryCostEstimate:
    """A BigQuery dry run - validates the SQL and reports how many bytes
    it would process WITHOUT running it or billing anything (dry runs
    are free and don't count against any quota). Raises
    google.api_core.exceptions.GoogleAPICallError if `sql` is malformed -
    callers should catch that and route to suggest_sql_fix() rather than
    showing BigQuery's raw error with nothing actionable."""
    client = bigquery.Client(project=project)
    job = client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
    bytes_processed = job.total_bytes_processed or 0
    gb = bytes_processed / _BYTES_PER_GB
    cost_usd = (bytes_processed / _BYTES_PER_TIB) * BQ_ON_DEMAND_PRICE_PER_TIB_USD
    return QueryCostEstimate(bytes_processed=bytes_processed, gb=gb, cost_usd=cost_usd)


# --- Whole-project schema summary, for Gemini's fix advice --------------

_schema_cache: dict[str, tuple[float, str]] = {}  # project -> (fetched_at, text)


def fetch_project_schema_summary(project: str, *, force: bool = False) -> str:
    """Every dataset/table/column, and every CALL-able stored procedure,
    in `project` - fed to Gemini so a malformed-query fix suggestion is
    grounded in what actually exists, not a guess. Pure INFORMATION_SCHEMA
    metadata queries (no table data scanned), so this is free/cheap
    regardless of how large the underlying tables are - but it loops once
    per dataset (list_datasets() doesn't offer a single project-wide
    INFORMATION_SCHEMA view that's guaranteed to cover datasets in every
    region), so it's still cached rather than re-run on every request."""
    cached = _schema_cache.get(project)
    if cached and not force and (time.time() - cached[0]) < SCHEMA_CACHE_TTL_SECONDS:
        return cached[1]

    client = bigquery.Client(project=project)
    sections: list[str] = []
    total_len = 0
    truncated = False

    for dataset in client.list_datasets(project=project):
        if truncated:
            break
        dataset_id = dataset.dataset_id
        tables_text = _describe_dataset_tables(client, project, dataset_id)
        routines_text = _describe_dataset_routines(client, project, dataset_id)
        section = f"\nDataset {dataset_id}:\n{tables_text}{routines_text}"
        if total_len + len(section) > _MAX_SCHEMA_TEXT_CHARS:
            sections.append("\n[...schema truncated - too many datasets/tables to list in full...]")
            truncated = True
            break
        sections.append(section)
        total_len += len(section)

    text = "".join(sections) or "(no datasets found)"
    _schema_cache[project] = (time.time(), text)
    return text


def _describe_dataset_tables(client: bigquery.Client, project: str, dataset_id: str) -> str:
    rows = list(client.query(
        f"SELECT table_name, column_name, data_type FROM `{project}.{dataset_id}`.INFORMATION_SCHEMA.COLUMNS "
        f"ORDER BY table_name, ordinal_position"
    ).result())
    lines = []
    current_table = None
    cols: list[str] = []
    for row in rows:
        if row.table_name != current_table:
            if current_table is not None:
                lines.append(f"  {current_table}({', '.join(cols)})")
            current_table = row.table_name
            cols = []
        cols.append(f"{row.column_name} {row.data_type}")
    if current_table is not None:
        lines.append(f"  {current_table}({', '.join(cols)})")
    return "\n".join(lines) + ("\n" if lines else "  (no tables)\n")


def _describe_dataset_routines(client: bigquery.Client, project: str, dataset_id: str) -> str:
    rows = list(client.query(
        f"SELECT routine_name, routine_type FROM `{project}.{dataset_id}`.INFORMATION_SCHEMA.ROUTINES "
        f"WHERE routine_type = 'PROCEDURE' ORDER BY routine_name"
    ).result())
    if not rows:
        return ""
    names = ", ".join(f"{dataset_id}.{r.routine_name}" for r in rows)
    return f"  Callable procedures: {names}\n"


# --- Gemini-powered fix advice when BigQuery rejects the SQL ------------

class _SqlFixAdvice(BaseModel):
    explanation: str  # what's likely wrong, in plain language, 1-3 sentences
    suggested_sql: Optional[str] = None  # a corrected version, only when Gemini is confident - NOT validated or executed, shown as-is for the user to review and resubmit themselves


_FIX_SYSTEM_PROMPT = """You are helping someone fix a BigQuery SQL query that failed on Moove's Archimedes platform. You'll be given the query they wrote, the exact error BigQuery returned, and a summary of the real tables/columns/stored procedures that exist in this project. Explain in 1-3 sentences what's likely wrong (e.g. a misspelled table/column name, a type mismatch, the wrong number of CALL arguments) using the real schema below - don't guess at a cause that doesn't match it. If you can see a concrete fix, put a corrected version of the query in `suggested_sql`; leave it unset if you're not confident. Only ever reference tables/columns/procedures that actually appear in the schema below - never invent one.

Project schema:
{schema}

Output ONLY the structured fields - nothing else."""


def suggest_sql_fix(sql: str, bq_error: str, project: str) -> _SqlFixAdvice:
    """Raises gemini_client.CredentialsNotConfigured/RuntimeError exactly
    as gemini_client.call_structured() does - callers should treat a
    failure here as "no fix advice available", not fail the whole
    request, since the raw BigQuery error is still useful on its own."""
    schema = fetch_project_schema_summary(project)
    return gemini_client.call_structured(
        system_instruction=_FIX_SYSTEM_PROMPT.format(schema=schema),
        user_text=f"Query:\n{sql}\n\nError from BigQuery:\n{bq_error}",
        response_schema=_SqlFixAdvice,
        project=project,
    )


# --- The single entry point app.py calls --------------------------------

def _json_safe(value):
    """BigQuery rows can carry date/datetime/Decimal/bytes values, none of
    which Flask's jsonify() can serialize directly - stringified here
    rather than at the Flask layer, since this module is the one that
    knows these are BigQuery values in the first place."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@dataclasses.dataclass
class SqlConsoleResult:
    kind: str  # "cost_estimate" | "results" | "error"
    statement_kind: Optional[str] = None  # "select" | "call"
    gb: Optional[float] = None
    cost_usd: Optional[float] = None
    columns: Optional[list[str]] = None
    rows: Optional[list[dict]] = None
    truncated: bool = False
    bq_error: Optional[str] = None
    fix_explanation: Optional[str] = None
    fix_suggested_sql: Optional[str] = None


def run_sql_console(
    sql: str, project: str, requester_email: str, confirmed: bool, log=lambda msg: None,
) -> SqlConsoleResult:
    """The one entry point app.py's Custom test routes call once
    sql_statement_kind(text) has already identified `sql` as real SQL
    (not the narrow comparison grammar). Always estimates cost via a
    free dry run first; returns kind="cost_estimate" (no execution yet)
    whenever that estimate needs confirming and `confirmed` isn't already
    true - the caller re-submits with confirmed=True once the user says
    yes. A CALL always needs confirmation regardless of estimated cost
    (its real risk is side effects, not billing) and is refused outright
    for anyone not in PRIVILEGED_SQL_USERS, before any BigQuery call at
    all."""
    kind = sql_statement_kind(sql)
    assert kind in ("select", "call"), f"run_sql_console called with non-SQL text: {sql!r}"

    if kind == "call" and requester_email not in PRIVILEGED_SQL_USERS:
        allowed = ", ".join(sorted(PRIVILEGED_SQL_USERS))
        return SqlConsoleResult(
            kind="error",
            bq_error=(
                f"Running a stored procedure (CALL) is restricted to {allowed} - "
                f"{requester_email or 'your account'} can still run SELECT/WITH queries directly."
            ),
        )

    try:
        estimate = estimate_query_cost(project, sql)
    except GoogleAPICallError as e:
        return _error_with_fix_advice(sql, str(e), project, log)

    needs_confirmation = kind == "call" or estimate.cost_usd > COST_CONFIRMATION_THRESHOLD_USD
    if needs_confirmation and not confirmed:
        return SqlConsoleResult(kind="cost_estimate", statement_kind=kind, gb=estimate.gb, cost_usd=estimate.cost_usd)

    try:
        client = bigquery.Client(project=project)
        job = client.query(sql)
        result = job.result(max_results=MAX_RESULT_ROWS)
        columns = [f.name for f in result.schema] if result.schema else []
        rows = [{k: _json_safe(v) for k, v in dict(row).items()} for row in result]
        truncated = bool(result.total_rows is not None and result.total_rows > MAX_RESULT_ROWS)
        log(f"Ran {kind} SQL ({estimate.gb:.3f} GB, ${estimate.cost_usd:.4f} estimated) for {requester_email}")
        return SqlConsoleResult(
            kind="results", statement_kind=kind, gb=estimate.gb, cost_usd=estimate.cost_usd,
            columns=columns, rows=rows, truncated=truncated,
        )
    except GoogleAPICallError as e:
        return _error_with_fix_advice(sql, str(e), project, log)


def _error_with_fix_advice(sql: str, bq_error: str, project: str, log) -> SqlConsoleResult:
    log(f"SQL failed: {bq_error}")
    try:
        fix = suggest_sql_fix(sql, bq_error, project)
    except Exception as e:
        log(f"Couldn't get Gemini fix advice: {e}")
        return SqlConsoleResult(kind="error", bq_error=bq_error)
    return SqlConsoleResult(kind="error", bq_error=bq_error, fix_explanation=fix.explanation, fix_suggested_sql=fix.suggested_sql)
