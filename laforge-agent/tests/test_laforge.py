import pytest

from laforge import server
from laforge.catalog import Catalog
from laforge.engine import METRICS, Combo, _diversify
from laforge.recipe import render

CAT = Catalog({"a_speed": "speed", "b_count": "count"})


class FakeEngine:
    def top(self, dataset, metric, n, cap=2):
        return [Combo("a_speed", "b_count", "divide", {m: 0.3 for m in METRICS}, 0.1)]


@pytest.fixture(autouse=True)
def fakes(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "ENGINE", FakeEngine())
    monkeypatch.setattr(server, "CATALOG", CAT)
    monkeypatch.setattr(server, "OUT_DIR", tmp_path)
    server.SESSIONS.clear()
    server.RESULTS.clear()


def test_only_blind_mode():
    assert server.start_session(mode="data")["error"] == "mode_not_available"


def test_severity_is_refused_not_substituted():
    sid = server.start_session()["session_id"]
    assert server.suggest_combinations(sid, target="severity")["error"] == "target_not_available"


def test_results_carry_caveats_and_provenance():
    sid = server.start_session()["session_id"]
    out = server.suggest_combinations(sid)
    assert out["provenance"]["validation"] == "not_run" and out["caveats"]


def test_recipe_snowflake_uses_nullif():
    sid = server.start_session()["session_id"]
    rid = server.suggest_combinations(sid)["result_id"]
    out = server.generate_recipe(sid, rid, dialect="snowflake", source_table="db.sch.tbl")
    assert "a_speed / NULLIF(b_count, 0)" in open(out["recipe_file"]).read()


def test_bigquery_uses_safe_divide():
    spec = {"source_table": "p.d.t", "id_column": "id",
            "combos": [{"feature1": "a_speed", "operation": "divide", "feature2": "b_count"}]}
    assert "SAFE_DIVIDE(a_speed, b_count)" in render(spec, CAT, "bigquery")


def test_sql_injection_rejected():
    spec = {"source_table": "t", "id_column": "id", "combos": [
        {"feature1": "a_speed; DROP TABLE x", "operation": "divide", "feature2": "b_count"}]}
    with pytest.raises(ValueError):
        render(spec, CAT, "bigquery")


def test_bad_table_rejected():
    spec = {"source_table": "t; DROP", "id_column": "id", "combos": []}
    with pytest.raises(ValueError):
        render(spec, CAT, "bigquery")


def test_cap_per_parent_feature():
    rows = [{"feature1": "f", "feature2": f"g{i}", "operation": "divide",
             **{m: 0.5 for m in METRICS}} for i in range(5)]
    assert len(_diversify(rows, {}, "spearman", 10, cap=2)) == 2
