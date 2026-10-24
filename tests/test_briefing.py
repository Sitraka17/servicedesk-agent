from datetime import date

import pytest

from servicenow_mcp import briefing, server, store

AS_OF = date(2026, 10, 24)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SNOW_DB", str(tmp_path / "snow.db"))
    store.reset()


def numbers(rows):
    return [r["number"] for r in rows]


def test_briefing_sections_on_seed_data():
    b = briefing.compute(AS_OF)
    assert b["open_total"] == 7
    assert numbers(b["urgent"]) == ["INC0010005", "INC0010002"]
    assert numbers(b["stale"]) == ["INC0010001", "INC0010004", "INC0010003", "INC0010006"]
    assert numbers(b["backup_risk"]) == ["INC0010005"]
    assert b["repeat_cis"] == [{"ci": "app-payroll-01", "open": 2, "numbers": ["INC0010001", "INC0010006"]}]


def test_an_update_takes_an_incident_out_of_stale():
    server.update_incident("INC0010001", work_note="vendor called back")
    stale = numbers(briefing.compute(date.today())["stale"])
    assert "INC0010001" not in stale


def test_markdown_lists_every_section():
    text = briefing.render_markdown(briefing.compute(AS_OF))
    for title in ("P1 and P2 still open (2)", "No update for more than 3 days (4)",
                  "no backup (1)", "several open incidents (1)", "per team"):
        assert title in text


def test_briefing_is_an_mcp_tool():
    assert server.morning_briefing(as_of="2026-10-24")["open_total"] == 7
