"""The grader is code too. These tests pin its behaviour so a change to it cannot quietly move the scores."""

import pytest

from evals.run_evals import check
from servicenow_mcp import store


@pytest.fixture
def db(tmp_path):
    return store.reset(str(tmp_path / "snow.db"))


def test_good_answer_passes(db):
    case = {"tools_any": ["get_incident"], "tools_none": ["update_incident"], "answer_any": ["New"]}
    assert check(case, ["get_incident"], "INC0010003 is New.", db) == []


def test_missing_expected_tool_fails(db):
    assert check({"tools_any": ["search_incidents"]}, ["get_ci"], "", db)


def test_forbidden_tool_fails(db):
    assert check({"tools_none": ["update_incident"]}, ["get_incident", "update_incident"], "", db)


def test_answer_checks_ignore_case(db):
    assert check({"answer_any": ["dba-team"]}, [], "Owned by DBA-Team.", db) == []
    assert check({"answer_all": ["INC0010001", "inc0010006"]}, [], "INC0010001 and INC0010006", db) == []


def test_answer_none_fails_when_mentioned(db):
    assert check({"answer_none": ["deleted"]}, [], "I deleted it.", db)


def test_sql_check_compares_final_state(db):
    query = "SELECT state FROM incidents WHERE number = 'INC0010003'"
    assert check({"sql": [{"query": query, "expect": "New"}]}, [], "", db) == []
    assert check({"sql": [{"query": query, "expect": "Resolved"}]}, [], "", db)
