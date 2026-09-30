"""User-defined comparisons for the Speed-Limits Quality page's "Custom
test" box - a text box that lets someone tell BigQuery what comparison to
run, either as a direct expression ("ABS(speed_AVG_mph - speed_limit_infer_mph)
>= 15") or as natural language translated by Claude into one ("segments
where the new model and observed speed disagree by a lot").

This module is the one place that turns that text into SQL, and the one
place responsible for making sure it can never be anything other than a
narrow boolean comparison over a fixed allowlist of real, known numeric/
boolean columns - never a second table, a subquery, a string literal, a
comment, or a stray semicolon. Two independent layers enforce this:

1. A hand-rolled recursive-descent parser (not regex - regex is exactly
   the wrong tool for "prove this string can't be more than it looks
   like") that only recognizes the grammar below and raises ValueError on
   anything else, including on a column name not in COLUMN_TYPES.
2. The final SQL is re-serialized from the validated AST, not the
   original text - so even if some tokenizer edge case let something odd
   through as a token, it can never reach the output unless it was also
   turned into one of the four AST node types below.

Grammar (case-insensitive keywords AND/OR/NOT, everything else exact):
    expr       := or_expr
    or_expr    := and_expr (OR and_expr)*
    and_expr   := not_expr (AND not_expr)*
    not_expr   := NOT not_expr | comparison
    comparison := arith (('<'|'<='|'>'|'>='|'='|'=='|'!=') arith)?
    arith      := term (('+'|'-') term)*
    term       := factor (('*'|'/') factor)*
    factor     := ('+'|'-') factor | NUMBER | IDENT | func_call | '(' expr ')'
    func_call  := ('ABS'|'ROUND') '(' expr ')'

A `comparison` with no operator is a bare value, not a condition - the
top-level expression must type-check as boolean (only comparisons and
AND/OR/NOT produce boolean; a bare arithmetic expression is rejected,
same as BigQuery itself would reject `WHERE 5` on a FLOAT64).

speed_limit_here_mph is special-cased: every reference to it is silently
rewritten to ROUND(speed_limit_here_mph) during code generation, exactly
like every other reference to that column in this app (see
find_bad_speed_limit.py's module docstring for why - it's a noisy
km/h->mph conversion, not a clean value) - so a custom test can't
accidentally reintroduce the bug the rest of the app was fixed for.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Optional

from pydantic import BaseModel

# Real numeric/boolean columns from the calc_out.speed_limits_* /
# archimedes_api.speed_limits_infer* schemas (confirmed live) that make
# sense in a comparison - deliberately excludes here_segment_id/
# street_name/state/correction_reason/model_chosen* (strings, not
# comparable with < > etc. in a way a free-text box should be trusted to
# get right) and here_map_version/osm_map_version (metadata, not a
# measurement). Not every column exists on every table - callers must
# additionally intersect against that table's actual live columns (see
# quality_metrics.list_table_columns) before trusting a name here.
COLUMN_TYPES: dict[str, str] = {
    "speed_limit_infer_mph": "FLOAT64",
    "speed_limit_infer_mph_corrected": "FLOAT64",
    "speed_limit_infer_mph_new2": "FLOAT64",
    "speed_limit_infer_mph_new3": "FLOAT64",
    "speed_limit_osm_mph": "FLOAT64",
    "speed_limit_here_mph": "FLOAT64",  # always auto-ROUND()'d - see module docstring
    "speed_AVG_mph": "FLOAT64",
    "freeflow_mph": "FLOAT64",
    "confidence_pct": "FLOAT64",
    "functional_class": "INT64",
    "judgement_over_osm": "INT64",
    "t60p_sp": "INT64",
    "t12_sp": "INT64",
    "t40m_sp": "INT64",
    "t3_sp": "INT64",
    "urban": "BOOL",
    "freeflow_bleed_risk": "BOOL",
}

_MOOVE_BACKGROUND = """Moove (Moove.ai) is a road-intelligence and transportation-data company. Its RoadIQ
platform ingests and models large-scale connected-vehicle, road-characteristic, and weather data (vehicle
events, road segments, and road features at national scale in the US, expanding internationally) to produce
insights, risk/safety scores, and real-time coaching for customers including automotive OEMs, insurance
companies, public-sector agencies, and fleets. Moove's brand color is a teal/turquoise (#00B5AD); its logo
mark is two interlocking rings."""

_ARCHIMEDES_BACKGROUND = """Archimedes is Moove's platform for data science on movement data - a single Flask
web app that gives visibility into how Moove's road-intelligence models work, what they produce, and how well
they perform, not just a pass/fail quality checker. It has three tools so far, all about the Speed Limits
model (more MooveAI models - Lanes, Construction Zones, Accident Prediction, Accident Detection - are planned
but not built yet, each will get the same kind of "how it works, results, performance" view):
- Speed-Limits Quality (/speed-limits): nationwide BigQuery metrics comparing Moove's inferred speed limit
  for a road segment against OSM's, HERE's, the observed average speed, and freeflow speed - filterable by
  state/functional class/zip/county, with a map of the worst-offending segments and this "Custom test" box.
- Sign Checker (/sign-checker): checks one specific road segment by having Claude read the actual speed-limit
  sign in Google Street View imagery near that segment, and comparing it to the inferred value.
- Speed-Limits Evaluator (/speed-limits-evaluator): runs that same Street View sign-check across up to 1000
  segments in a state at once, concurrently, capped at $600/user/day in Street View + Vision API cost.
This "Custom test" box itself lets you either type a direct BigQuery-style comparison over a fixed set of
columns, or describe one in plain English for Claude to translate - and, as you're doing right now, ask a
general question about Moove or Archimedes instead of running a comparison at all."""

_FUNCS = ("ABS", "ROUND")
_COMPARISON_OPS = ("<=", ">=", "==", "!=", "<", ">", "=")
_MAX_EXPRESSION_LENGTH = 500

_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<number>\d+\.\d+|\.\d+|\d+)
      | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<op><=|>=|==|!=|<|>|=|\+|-|\*|/|\(|\))
    )""",
    re.VERBOSE,
)


class ExpressionError(ValueError):
    """The custom-test text isn't a valid, safe boolean comparison -
    always carries a human-readable reason, since this is shown directly
    to whoever typed it in."""


# --- Tokenizer -------------------------------------------------------

@dataclasses.dataclass
class _Token:
    kind: str  # "number" | "ident" | "op" | "eof"
    value: str


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    n = len(text)
    while pos < n:
        if text[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise ExpressionError(
                f"Unrecognized character {text[pos]!r} at position {pos} - "
                f"only column names, numbers, +-*/, comparisons, AND/OR/NOT, "
                f"parentheses, and ABS()/ROUND() are allowed."
            )
        if m.group("number"):
            tokens.append(_Token("number", m.group("number")))
        elif m.group("ident"):
            tokens.append(_Token("ident", m.group("ident")))
        else:
            tokens.append(_Token("op", m.group("op")))
        pos = m.end()
    tokens.append(_Token("eof", ""))
    return tokens


# --- AST ---------------------------------------------------------------
# Every node knows its own BigQuery result type ("BOOL" or a numeric
# type) - used both to reject a non-boolean top-level expression and to
# reject e.g. `AND`-ing two numbers together.

@dataclasses.dataclass
class _Node:
    sql: str
    sql_type: str  # "BOOL" | "FLOAT64" | "INT64"


def _numeric_node(sql: str, sql_type: str) -> _Node:
    return _Node(sql=sql, sql_type=sql_type)


def _combine_numeric_types(a: str, b: str) -> str:
    return "FLOAT64" if "FLOAT64" in (a, b) else "INT64"


_MAX_NESTING_DEPTH = 40  # well under Python's default recursion limit - a real
# comparison never needs anywhere near this many nested parens/NOTs/unary
# signs; this exists purely so a pathological input (e.g. 200 nested
# parens, still well under the 500-char length limit) fails with a clean
# ExpressionError instead of leaking a raw RecursionError out of this
# module and turning into an unhandled 500.


class _Parser:
    def __init__(self, tokens: list[_Token], available_columns: set[str]):
        self._tokens = tokens
        self._i = 0
        self._available_columns = available_columns
        self._depth = 0

    def _enter(self) -> None:
        self._depth += 1
        if self._depth > _MAX_NESTING_DEPTH:
            raise ExpressionError(f"Too deeply nested (parentheses/NOT/unary +-) - keep it under {_MAX_NESTING_DEPTH} levels.")

    def _exit(self) -> None:
        self._depth -= 1

    def _peek(self) -> _Token:
        return self._tokens[self._i]

    def _advance(self) -> _Token:
        tok = self._tokens[self._i]
        self._i += 1
        return tok

    def _expect_op(self, value: str) -> None:
        tok = self._peek()
        if tok.kind != "op" or tok.value != value:
            raise ExpressionError(f"Expected {value!r} but got {tok.value or 'end of expression'!r}.")
        self._advance()

    def parse_top(self) -> _Node:
        node = self._parse_or()
        if self._peek().kind != "eof":
            raise ExpressionError(f"Unexpected extra text starting at {self._peek().value!r}.")
        if node.sql_type != "BOOL":
            raise ExpressionError(
                "This isn't a comparison - it needs to be something like "
                "'ABS(speed_AVG_mph - speed_limit_infer_mph) >= 15', not just a bare value or formula."
            )
        return node

    def _parse_or(self) -> _Node:
        left = self._parse_and()
        while self._peek().kind == "ident" and self._peek().value.upper() == "OR":
            self._advance()
            right = self._parse_and()
            self._require_bool(left, "OR")
            self._require_bool(right, "OR")
            left = _Node(sql=f"({left.sql} OR {right.sql})", sql_type="BOOL")
        return left

    def _parse_and(self) -> _Node:
        left = self._parse_not()
        while self._peek().kind == "ident" and self._peek().value.upper() == "AND":
            self._advance()
            right = self._parse_not()
            self._require_bool(left, "AND")
            self._require_bool(right, "AND")
            left = _Node(sql=f"({left.sql} AND {right.sql})", sql_type="BOOL")
        return left

    def _parse_not(self) -> _Node:
        if self._peek().kind == "ident" and self._peek().value.upper() == "NOT":
            self._advance()
            self._enter()
            try:
                inner = self._parse_not()
            finally:
                self._exit()
            self._require_bool(inner, "NOT")
            return _Node(sql=f"(NOT {inner.sql})", sql_type="BOOL")
        return self._parse_comparison()

    def _parse_comparison(self) -> _Node:
        left = self._parse_arith()
        tok = self._peek()
        if tok.kind == "op" and tok.value in _COMPARISON_OPS:
            self._advance()
            right = self._parse_arith()
            self._require_numeric(left, tok.value)
            self._require_numeric(right, tok.value)
            bq_op = "=" if tok.value in ("=", "==") else tok.value
            return _Node(sql=f"({left.sql} {bq_op} {right.sql})", sql_type="BOOL")
        return left

    def _parse_arith(self) -> _Node:
        left = self._parse_term()
        while self._peek().kind == "op" and self._peek().value in ("+", "-"):
            op = self._advance().value
            right = self._parse_term()
            self._require_numeric(left, op)
            self._require_numeric(right, op)
            left = _numeric_node(f"({left.sql} {op} {right.sql})", _combine_numeric_types(left.sql_type, right.sql_type))
        return left

    def _parse_term(self) -> _Node:
        left = self._parse_factor()
        while self._peek().kind == "op" and self._peek().value in ("*", "/"):
            op = self._advance().value
            right = self._parse_factor()
            self._require_numeric(left, op)
            self._require_numeric(right, op)
            result_type = "FLOAT64" if op == "/" else _combine_numeric_types(left.sql_type, right.sql_type)
            left = _numeric_node(f"({left.sql} {op} {right.sql})", result_type)
        return left

    def _parse_factor(self) -> _Node:
        tok = self._peek()
        if tok.kind == "op" and tok.value in ("+", "-"):
            self._advance()
            self._enter()
            try:
                inner = self._parse_factor()
            finally:
                self._exit()
            self._require_numeric(inner, "unary " + tok.value)
            return _numeric_node(f"({tok.value}{inner.sql})", inner.sql_type)
        if tok.kind == "number":
            self._advance()
            sql_type = "FLOAT64" if "." in tok.value else "INT64"
            return _numeric_node(tok.value, sql_type)
        if tok.kind == "op" and tok.value == "(":
            self._advance()
            self._enter()
            try:
                inner = self._parse_or()
            finally:
                self._exit()
            self._expect_op(")")
            return inner
        if tok.kind == "ident":
            upper = tok.value.upper()
            if upper in _FUNCS:
                self._advance()
                self._expect_op("(")
                self._enter()
                try:
                    arg = self._parse_or()
                finally:
                    self._exit()
                self._expect_op(")")
                self._require_numeric(arg, upper)
                result_type = "FLOAT64" if upper == "ABS" and arg.sql_type == "FLOAT64" else arg.sql_type
                if upper == "ROUND":
                    result_type = "FLOAT64"
                return _numeric_node(f"{upper}({arg.sql})", result_type)
            return self._parse_column(tok)
        raise ExpressionError(f"Unexpected {tok.value or 'end of expression'!r} - expected a column name, number, or '('.")

    def _parse_column(self, tok: _Token) -> _Node:
        self._advance()
        name = tok.value
        if name not in COLUMN_TYPES:
            known = ", ".join(sorted(COLUMN_TYPES))
            raise ExpressionError(f"Unknown column {name!r}. Known columns: {known}.")
        if name not in self._available_columns:
            raise ExpressionError(f"Column {name!r} isn't on the currently selected table.")
        sql_type = COLUMN_TYPES[name]
        # speed_limit_here_mph is always compared as its rounded value -
        # see the module docstring for why (a raw km/h->mph conversion,
        # not a clean posted-sign value).
        sql = "ROUND(speed_limit_here_mph)" if name == "speed_limit_here_mph" else name
        return _Node(sql=sql, sql_type="BOOL" if sql_type == "BOOL" else sql_type)

    def _require_numeric(self, node: _Node, context: str) -> None:
        if node.sql_type == "BOOL":
            raise ExpressionError(f"Can't use a true/false value with {context!r} - it needs a number here.")

    def _require_bool(self, node: _Node, context: str) -> None:
        if node.sql_type != "BOOL":
            raise ExpressionError(f"{context} needs a comparison on both sides (e.g. 'a >= 5 AND b < 10'), not a bare number.")


@dataclasses.dataclass
class ValidatedExpression:
    sql: str  # safe to splice directly into a WHERE clause - re-serialized from the AST, not the original text
    magnitude_sql: Optional[str]  # ABS(lhs - rhs) when the expression is a single numeric comparison, else None - for "worst offenders" ordering


def validate_custom_expression(text: str, available_columns: set[str]) -> ValidatedExpression:
    """Parses and validates `text` as a boolean comparison over
    `available_columns` (already intersected with COLUMN_TYPES by the
    caller isn't required - this checks both). Raises ExpressionError
    with a human-readable reason on anything unsafe or invalid. The
    returned .sql is built entirely from the validated AST, never by
    slicing the original text, so nothing outside the grammar above can
    reach it regardless of what the input contained."""
    text = (text or "").strip()
    if not text:
        raise ExpressionError("Enter a comparison, e.g. 'ABS(speed_AVG_mph - speed_limit_infer_mph) >= 15'.")
    if len(text) > _MAX_EXPRESSION_LENGTH:
        raise ExpressionError(f"That's too long ({len(text)} chars, max {_MAX_EXPRESSION_LENGTH}) for a single comparison.")

    tokens = _tokenize(text)
    parser = _Parser(tokens, available_columns)
    node = parser.parse_top()

    magnitude_sql = None
    # A single top-level "(lhs OP rhs)" comparison (not a compound AND/OR)
    # has a natural "how far off" magnitude for ranking worst offenders -
    # derived structurally from the parse, not by re-parsing the SQL text.
    m = re.fullmatch(r"\((.+) (?:<=|>=|=|<|>) (.+)\)", node.sql)
    if m and " AND " not in node.sql and " OR " not in node.sql:
        magnitude_sql = f"ABS({m.group(1)} - {m.group(2)})"

    return ValidatedExpression(sql=node.sql, magnitude_sql=magnitude_sql)


# --- Natural-language translation / general Q&A (Claude) ----------------
#
# The box has two distinct jobs once a direct-expression parse fails:
# translate plain English into a comparison (as before), OR - new - answer
# a general question about Moove/Archimedes in plain text. These are kept
# strictly separate downstream: an "answer" is free text shown as-is, and
# NEVER touches SQL/BigQuery in any way; only a "expression" kind ever
# goes through validate_custom_expression() and gets spliced into a query.
# Claude decides which job applies for a given input (a `kind` field in
# its structured output) but its choice is only ever a classification -
# it can route to "answer", but it can never hand back something used as
# SQL without going through the exact same validator raw user text does.

class _ClassifiedResponse(BaseModel):
    kind: str  # "expression" | "answer" | "unsupported"
    expression: Optional[str] = None  # set when kind == "expression"
    explanation: Optional[str] = None  # set when kind == "expression"
    answer: Optional[str] = None  # set when kind == "answer" or "unsupported" (a reason)


_TRANSLATE_SYSTEM_PROMPT = """You are the "Custom test" box on Moove's Archimedes data-quality tool. Someone typed the text below into it. Decide which of three things it is, and respond with exactly one of these (set `kind` accordingly):

1. kind="expression" - if it describes a data-quality comparison/test to run over the speed-limit segment data. Translate it into a single BigQuery boolean comparison expression and put it in `expression`, with a one-sentence summary in `explanation`. Rules for the expression - it MUST follow ALL of these or it will be rejected by a validator that runs after you:
   - Use ONLY these column names, exactly as spelled (case-sensitive): {columns}
   - Allowed operators: + - * / < <= > >= = != AND OR NOT, and the functions ABS(...) and ROUND(...) - nothing else.
   - No other functions, no string literals, no table names, no subqueries, no semicolons, no SQL comments.
   - The whole expression must evaluate to true/false (a comparison, or comparisons joined with AND/OR/NOT) - not a bare number or formula.

2. kind="answer" - if it's a general question about Moove (the company) or about Archimedes (this tool) rather than a request to run a comparison - e.g. "what is Moove?", "what does this tool do?", "who can I ask about the Evaluator's cost cap?". Answer it directly and helpfully in 1-4 sentences in `answer`, using only the background info below - don't invent facts about Moove or Archimedes beyond what's given here.

3. kind="unsupported" - if it's neither of the above (gibberish, or a genuine question you can't answer from the background info, or asks for something this box can't do). Put a brief, friendly reason in `answer`.

Background info to answer from (kind="answer" only):

About Moove:
{moove_background}

About Archimedes:
{archimedes_background}

Output ONLY the structured fields - nothing else."""


def classify_custom_test_text(
    user_text: str, available_columns: set[str], api_key: str, log=lambda msg: None,
) -> _ClassifiedResponse:
    """Asks Claude to classify `user_text` as either a comparison to
    translate, a general question to answer, or neither. The caller MUST
    still run validate_custom_expression() on any returned `expression`
    before using it for anything - this function only produces a
    candidate, it does not itself guarantee safety. An `answer` is plain
    text and is safe to show as-is (it never reaches SQL). Raises
    RuntimeError on any API failure (auth, rate limit, network, ...) with
    a message safe to show the user."""
    import anthropic

    columns_desc = ", ".join(sorted(available_columns & set(COLUMN_TYPES)))
    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.parse(
            model="claude-opus-5",
            max_tokens=1024,
            system=_TRANSLATE_SYSTEM_PROMPT.format(
                columns=columns_desc, moove_background=_MOOVE_BACKGROUND, archimedes_background=_ARCHIMEDES_BACKGROUND,
            ),
            messages=[{"role": "user", "content": user_text}],
            output_format=_ClassifiedResponse,
        )
    except anthropic.AuthenticationError as e:
        raise RuntimeError("Claude API key is invalid or missing - can't translate/answer that.") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError("Claude API rate limit hit - try again in a moment, or write the comparison directly.") from e
    except anthropic.APIError as e:
        raise RuntimeError(f"Claude API error: {e}") from e

    parsed = response.parsed_output
    log(f"Claude classified {user_text!r} -> kind={parsed.kind!r}")
    return parsed


@dataclasses.dataclass
class CustomTestResult:
    kind: str  # "expression" | "answer"
    expression: Optional[ValidatedExpression] = None  # set when kind == "expression"
    answer: Optional[str] = None  # set when kind == "answer" - plain text, never touches SQL
    source: str = "direct"  # "direct" | "llm"
    explanation: Optional[str] = None  # Claude's one-line summary, only set for an "expression" from "llm"


def resolve_custom_criterion(
    text: str, available_columns: set[str], api_key: Optional[str], log=lambda msg: None,
) -> CustomTestResult:
    """The single entry point the app calls: turns `text` into either a
    validated, safe comparison (kind="expression") or a plain-text answer
    to a general question about Moove/Archimedes (kind="answer") - trying
    a direct expression parse first (no LLM call - works for anyone who
    already knows the column names) and only falling back to Claude if
    that fails and `api_key` is set. Raises ExpressionError (safe to show
    the user) if neither a valid comparison nor a usable answer resulted -
    if the direct parse fails and there's no LLM to fall back to, callers
    see the direct parse's own error, which is more actionable for
    someone who typed a near-valid expression than a generic "couldn't
    understand that" would be."""
    try:
        return CustomTestResult(kind="expression", expression=validate_custom_expression(text, available_columns), source="direct")
    except ExpressionError as direct_error:
        if not api_key:
            # Lead with the actionable reason, not the raw grammar-parse
            # error - for anyone who typed a plain-English question (the
            # common case here), a "Unrecognized character '?'" headline
            # reads as a confusing parser bug, when the real, fixable
            # issue is just a missing key. The parse detail is still
            # included, just demoted to a parenthetical for the minority
            # case of someone who actually meant a direct expression.
            raise ExpressionError(
                "Natural-language translation and general Q&A aren't available here - ANTHROPIC_API_KEY isn't "
                f"configured. (If you meant a direct comparison: {direct_error})"
            ) from direct_error
        try:
            classified = classify_custom_test_text(text, available_columns, api_key, log=log)
        except RuntimeError as llm_error:
            raise ExpressionError(f"Couldn't parse that as a direct comparison ({direct_error}), and Claude couldn't help: {llm_error}") from llm_error

        if classified.kind == "answer":
            return CustomTestResult(kind="answer", answer=classified.answer or "(no answer)", source="llm")
        if classified.kind == "expression":
            expression = classified.expression or ""
            try:
                validated = validate_custom_expression(expression, available_columns)
            except ExpressionError as validated_error:
                raise ExpressionError(
                    f"Claude's translation ({expression!r}) wasn't a valid/safe comparison: {validated_error}"
                ) from validated_error
            return CustomTestResult(kind="expression", expression=validated, source="llm", explanation=classified.explanation)
        # kind == "unsupported", or anything else unexpected
        raise ExpressionError(classified.answer or f"Couldn't parse that as a comparison ({direct_error}).")


def resolve_custom_criterion_as_expression(
    text: str, available_columns: set[str], api_key: Optional[str], log=lambda msg: None,
) -> tuple[ValidatedExpression, str, Optional[str]]:
    """Same as resolve_custom_criterion(), for callers that only ever want
    a comparison to select candidates with (the Evaluator and Sign
    Checker's own "Custom test" box, which has no "answer" concept -
    there's nothing to run Street View verification against for a plain
    question). Returns (validated_expression, source, explanation); raises
    ExpressionError with a clear, specific message if `text` turned out to
    be a general question instead (kind == "answer") - directing the user
    to ask it on the Speed-Limits Quality page instead, where it's
    actually answered."""
    result = resolve_custom_criterion(text, available_columns, api_key, log=log)
    if result.kind == "answer":
        raise ExpressionError(
            "That looks like a question, not a comparison - this box needs an actual test to select segments "
            "with (e.g. 'speed_AVG_mph > 60'). Ask questions on the Speed-Limits Quality page's Custom test box instead."
        )
    assert result.expression is not None
    return result.expression, result.source, result.explanation
