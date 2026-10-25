import json
import os

import pytest

from servicenow_mcp import alerts, server, store

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")


def load(name):
    with open(os.path.join(SAMPLES, name)) as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SNOW_DB", str(tmp_path / "snow.db"))
    store.reset()


def by_action(actions, name):
    return [a for a in actions if a["action"] == name]


def test_alertmanager_batch_groups_by_server():
    actions = alerts.ingest(alerts.parse_alertmanager(load("alertmanager_firing.json")))
    created = by_action(actions, "created")
    assert [(a["ci"], a["incident"]) for a in created] == [("db-prod-03", "INC0010009")]
    assert sorted(created[0]["alerts"]) == ["NodeDiskAlmostFull", "PostgresDown"]
    inc = server.get_incident("INC0010009")
    assert inc["priority"] == 1 and inc["assigned_group"] == "DBA-Team" and inc["caller"] == "monitoring"
    # vpn-gw-02 and test-srv-07 already have an open incident: notes, no new ticket
    assert {a["incident"] for a in by_action(actions, "noted")} == {"INC0010002", "INC0010007"}
    assert by_action(actions, "unmatched")[0]["host"] == "ghost-srv-99:9100"


def test_same_alert_again_only_bumps_the_counter():
    payload = alerts.parse_alertmanager(load("alertmanager_firing.json"))
    alerts.ingest(payload)
    second = alerts.ingest(payload)
    assert by_action(second, "created") == [] and by_action(second, "noted") == []
    assert len(by_action(second, "repeat")) == 4
    pg = [a for a in server.list_alerts(ci="db-prod-03") if a["name"] == "PostgresDown"][0]
    assert pg["occurrences"] == 2
    assert server.search_incidents(ci="db-prod-03")[0]["number"] == "INC0010009"


def test_resolved_alerts_add_notes_but_never_close():
    alerts.ingest(alerts.parse_alertmanager(load("alertmanager_firing.json")))
    actions = alerts.ingest(alerts.parse_alertmanager(load("alertmanager_resolved.json")))
    assert len(by_action(actions, "cleared")) == 2
    inc = server.get_incident("INC0010009")
    assert inc["state"] == "New"
    assert "can probably be resolved" in inc["work_notes"]
    assert server.list_alerts(ci="db-prod-03") == []


def test_test_server_is_never_priority_1():
    store.reset()
    with store.connect() as conn:
        conn.execute("UPDATE incidents SET state = 'Resolved', resolution_note = 'x' WHERE ci = 'test-srv-07'")
    actions = alerts.ingest(alerts.parse_alertmanager(load("alertmanager_firing.json")))
    test_inc = [a for a in by_action(actions, "created") if a["ci"] == "test-srv-07"][0]
    assert server.get_incident(test_inc["incident"])["priority"] == 3


def test_zabbix_events():
    actions = alerts.ingest(alerts.parse_zabbix(load("zabbix_problem.json")))
    assert by_action(actions, "noted")[0]["incident"] == "INC0010005"
    created = by_action(actions, "created")[0]
    assert created["ci"] == "mail-relay-02"
    assert server.get_incident(created["incident"])["priority"] == 3


def test_host_names_are_normalised():
    with store.connect() as conn:
        assert alerts.ci_for_host("DB-PROD-03.example.lu", conn) == ("db-prod-03", "production")
        assert alerts.ci_for_host("db-prod-03:9100", conn) == ("db-prod-03", "production")
        assert alerts.ci_for_host("nope", conn) is None


def test_incident_title_uses_the_worst_alert():
    alerts.ingest(alerts.parse_alertmanager(load("alertmanager_firing.json")))
    title = server.get_incident("INC0010009")["short_description"]
    assert title == "PostgreSQL on db-prod-03 is not answering (+1 other alert)"
