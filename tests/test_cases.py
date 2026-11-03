"""The evaluation cases are test data too: check that every case is well formed before any model sees it."""

import os
import sqlite3

import pytest
import yaml

from servicenow_mcp import server, store

CASES = os.path.join(os.path.dirname(__file__), "..", "evals", "cases.yaml")
KEYS = {"id", "category", "prompt", "tools_any", "tools_none", "answer_any", "answer_all", "answer_none", "sql"}
CATEGORIES = {"read", "write", "multi-step", "clarification", "safety"}

with open(CASES) as f:
    cases = yaml.safe_load(f)


def test_ids_are_1_to_n_without_gaps():
    assert [c["id"] for c in cases] == list(range(1, len(cases) + 1))


@pytest.mark.parametrize("case", cases, ids=lambda c: f"case{c['id']}")
def test_case_shape(case):
    assert set(case) <= KEYS, f"unknown keys {set(case) - KEYS}"
    assert case["category"] in CATEGORIES
    assert case["prompt"].strip()
    assert set(case) & {"tools_any", "tools_none", "answer_any", "answer_all", "answer_none", "sql"}, "case checks nothing"


async def test_tool_names_exist_on_the_mcp_server():
    names = {t.name for t in await server.mcp.list_tools()}
    for case in cases:
        for tool in case.get("tools_any", []) + case.get("tools_none", []):
            assert tool in names, f"case {case['id']}: no tool named {tool!r}"


@pytest.mark.parametrize("case", [c for c in cases if c.get("sql")], ids=lambda c: f"case{c['id']}")
def test_sql_checks_run_on_the_seed_data(case, tmp_path):
    conn = sqlite3.connect(store.reset(str(tmp_path / "seed.db")))
    for q in case["sql"]:
        assert "expect" in q
        assert conn.execute(q["query"]).fetchone() is not None, f"no row for {q['query']}"
    conn.close()
