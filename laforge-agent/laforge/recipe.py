from __future__ import annotations

import re

from .catalog import Catalog, check_ident

DIALECTS = ("bigquery", "snowflake", "databricks", "pandas")
OPS = ("multiply", "divide")
_TABLE = re.compile(r"[A-Za-z0-9_.\-]+")


def _alias(a: str, op: str, b: str) -> str:
    name = f"{a}__{'div' if op == 'divide' else 'mul'}__{b}"
    if len(name) > 250:
        raise ValueError("combination name too long for a column")
    return name


def _expr(dialect: str, a: str, op: str, b: str) -> str:
    # Divide by zero -> NULL, never 0 or infinity. NULL inputs stay NULL.
    if dialect == "bigquery":
        return f"SAFE_DIVIDE({a}, {b})" if op == "divide" else f"{a} * {b}"
    if dialect in ("snowflake", "databricks"):
        return f"{a} / NULLIF({b}, 0)" if op == "divide" else f"{a} * {b}"
    if dialect == "pandas":
        return (f"df['{a}'] / df['{b}'].where(df['{b}'] != 0)" if op == "divide"
                else f"df['{a}'] * df['{b}']")
    raise ValueError(f"unknown dialect: {dialect!r}")


def render(spec: dict, catalog: Catalog, dialect: str) -> str:
    """Render a recipe. Applies f to unit-level feature columns, i.e. f(avg(a), avg(b)).
    Only allow-listed features and operations can appear in the output."""
    table, id_col = spec["source_table"], spec["id_column"]
    if not _TABLE.fullmatch(table):
        raise ValueError("bad table name")
    check_ident(id_col)
    cols = []
    for c in spec["combos"]:
        a, op, b = c["feature1"], c["operation"], c["feature2"]
        if op not in OPS:
            raise ValueError(f"unsupported operation: {op!r}")
        catalog.check(a, b)
        cols.append((_alias(a, op, b), _expr(dialect, a, op, b)))
    if dialect == "pandas":
        lines = ["import pandas as pd", "",
                 "def add_laforge_features(df: pd.DataFrame) -> pd.DataFrame:",
                 "    out = df.copy()"]
        lines += [f"    out['{n}'] = {e}" for n, e in cols]
        return "\n".join(lines + ["    return out"])
    select = ",\n  ".join(f"{e} AS {n}" for n, e in cols)
    src = f"`{table}`" if dialect == "bigquery" else table
    return f"SELECT {id_col},\n  {select}\nFROM {src}"
