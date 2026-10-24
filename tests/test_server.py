import pytest

from servicenow_mcp import server, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SNOW_DB", str(tmp_path / "snow.db"))
    store.reset()


def test_search_open_incidents_on_ci():
    numbers = [i["number"] for i in server.search_incidents(ci="app-payroll-01")]
    assert numbers == ["INC0010001", "INC0010006"]


def test_search_excludes_resolved_by_default():
    assert server.search_incidents(ci="fw-pa-01") == []
    assert len(server.search_incidents(ci="fw-pa-01", open_only=False)) == 1


def test_unknown_incident_and_ci():
    assert "error" in server.get_incident("INC0099999")
    assert "error" in server.get_ci("nope-01")


def test_production_cis_without_backup():
    names = [c["name"] for c in server.list_cis(environment="production", without_backup=True)]
    assert names == ["db-prod-03", "web-portal-01"]


def test_create_incident_defaults_to_owner_group():
    created = server.create_incident("DB down", ci="db-prod-03", priority=1)
    assert created == {"created": "INC0010009", "assigned_group": "DBA-Team"}
    assert server.get_incident("INC0010009")["state"] == "New"


def test_create_refuses_duplicate_on_ci_with_open_incident():
    refused = server.create_incident("VPN to Bank X drops", ci="vpn-gw-02", priority=2)
    assert "INC0010002" in refused["error"]
    allowed = server.create_incident("New VPN for supplier Y", ci="vpn-gw-02", priority=3, different_problem=True)
    assert allowed["created"] == "INC0010009"


def test_create_rejects_unknown_ci_and_bad_priority():
    assert "error" in server.create_incident("x", ci="ghost", priority=2)
    assert "error" in server.create_incident("x", ci="db-prod-03", priority=7)


def test_resolve_requires_a_note():
    assert "error" in server.update_incident("INC0010006", state="Resolved")
    server.update_incident("INC0010006", state="Resolved", resolution_note="Service restarted")
    assert server.get_incident("INC0010006")["state"] == "Resolved"


def test_work_notes_are_appended():
    server.update_incident("INC0010001", work_note="first")
    server.update_incident("INC0010001", work_note="second")
    notes = server.get_incident("INC0010001")["work_notes"]
    assert "first" in notes and "second" in notes


def test_filters_are_case_insensitive_and_callers_validated():
    assert len(server.list_cis(environment="Production", without_backup=True)) == 2
    assert server.get_ci("DB-PROD-03")["owner_group"] == "DBA-Team"
    assert "error" in server.search_incidents(caller="service_desk")[0]
