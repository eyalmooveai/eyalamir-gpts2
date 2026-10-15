from __future__ import annotations

from dataclasses import dataclass

METRICS = ("pearson", "spearman", "kendall", "mutual_info", "r_squared",
           "cosine_similarity", "distance_corr", "ccc")
OPS = ("multiply", "divide")

DATASETS = {
    "quanata_ma": {
        "targets": ("frequency",),   # accident counts only; no severity for this dataset
        "unit": "road segment (5,558 units, Massachusetts)",
        "pairs_table": "moove-archimedes-staging.bucket_in.quanata_accidents_MA_correlations",
        "single_table": "moove-archimedes-staging.cache.quanata_accident_univariate_correlations_2",
    },
}


@dataclass
class Combo:
    feature1: str
    feature2: str
    operation: str
    metrics: dict
    gain: float | None = None   # |metric| minus the better parent feature (in-sample)

    def key(self) -> str:
        return f"{self.feature1}|{self.operation}|{self.feature2}"

    def compact(self, metric: str) -> dict:
        return {"id": self.key(), "feature1": self.feature1, "operation": self.operation,
                "feature2": self.feature2, "metric": metric,
                "value": round(self.metrics[metric], 3),
                "gain_over_best_parent": None if self.gain is None else round(self.gain, 3)}


def _diversify(rows, singles, metric, n, cap=2):
    """Keep the best rows, but at most `cap` combinations per parent feature."""
    used, out = {}, []
    for r in rows:
        f1, f2 = r["feature1"], r["feature2"]
        if used.get(f1, 0) >= cap or used.get(f2, 0) >= cap:
            continue
        parent = max(abs(singles.get(f, {}).get(metric) or 0) for f in (f1, f2))
        out.append(Combo(f1, f2, r["operation"], {m: r[m] for m in METRICS},
                         abs(r[metric]) - parent))
        used[f1] = used.get(f1, 0) + 1
        used[f2] = used.get(f2, 0) + 1
        if len(out) == n:
            break
    return out


class Engine:
    def pairs(self, dataset, metric, limit): raise NotImplementedError
    def singles(self, dataset): raise NotImplementedError
    def pair(self, dataset, f1, op, f2): raise NotImplementedError

    def top(self, dataset, metric, n, cap=2):
        return _diversify(self.pairs(dataset, metric, 500), self.singles(dataset), metric, n, cap)

    def score(self, dataset, f1, op, f2, metric="spearman"):
        r = self.pair(dataset, f1, op, f2)
        if not r:
            return None
        s = self.singles(dataset)
        parent = max(abs(s.get(f, {}).get(metric) or 0) for f in (f1, f2))
        return Combo(f1, f2, op, {m: r[m] for m in METRICS}, abs(r[metric]) - parent)


class BigQueryEngine(Engine):
    """Read-only: SELECT queries with a bytes-billed cap."""

    def __init__(self, project=None, max_bytes=2 * 10**9):
        from google.cloud import bigquery
        self.bq = bigquery
        self.client = bigquery.Client(project=project)
        self.max_bytes = max_bytes

    def _select(self, sql, params=()):
        if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
            raise ValueError("read-only queries only")
        cfg = self.bq.QueryJobConfig(query_parameters=list(params),
                                     maximum_bytes_billed=self.max_bytes)
        return [dict(r) for r in self.client.query(sql, job_config=cfg).result()]

    def pairs(self, dataset, metric, limit):
        t, cols = DATASETS[dataset]["pairs_table"], ", ".join(METRICS)
        return self._select(
            f"SELECT feature1, feature2, operation, {cols} FROM `{t}` "
            f"WHERE {metric} IS NOT NULL ORDER BY ABS({metric}) DESC LIMIT {int(limit)}")

    def singles(self, dataset):
        t, cols = DATASETS[dataset]["single_table"], ", ".join(METRICS)
        return {r["feature"]: r for r in self._select(f"SELECT feature, {cols} FROM `{t}`")}

    def pair(self, dataset, f1, op, f2):
        t, cols = DATASETS[dataset]["pairs_table"], ", ".join(METRICS)
        P = self.bq.ScalarQueryParameter
        rows = self._select(
            f"SELECT feature1, feature2, operation, {cols} FROM `{t}` "
            "WHERE feature1=@a AND feature2=@b AND operation=@o LIMIT 1",
            [P("a", "STRING", f1), P("b", "STRING", f2), P("o", "STRING", op)])
        return rows[0] if rows else None


class DuckDBEngine(Engine):
    """Offline: reads Parquet snapshots made by scripts/make_snapshot.py. No network."""

    def __init__(self, folder):
        import duckdb
        self.db, self.folder = duckdb.connect(), folder

    def _rows(self, sql, params=()):
        cur = self.db.execute(sql, list(params))
        names = [c[0] for c in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def _f(self, dataset, kind):
        return f"read_parquet('{self.folder}/{dataset}_{kind}.parquet')"

    def pairs(self, dataset, metric, limit):
        cols = ", ".join(METRICS)
        return self._rows(f"SELECT feature1, feature2, operation, {cols} FROM {self._f(dataset, 'pairs')} "
                          f"WHERE {metric} IS NOT NULL ORDER BY ABS({metric}) DESC LIMIT {int(limit)}")

    def singles(self, dataset):
        cols = ", ".join(METRICS)
        return {r["feature"]: r for r in self._rows(f"SELECT feature, {cols} FROM {self._f(dataset, 'singles')}")}

    def pair(self, dataset, f1, op, f2):
        cols = ", ".join(METRICS)
        rows = self._rows(f"SELECT feature1, feature2, operation, {cols} FROM {self._f(dataset, 'pairs')} "
                          "WHERE feature1=? AND feature2=? AND operation=? LIMIT 1", [f1, f2, op])
        return rows[0] if rows else None
