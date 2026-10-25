"""MCP server exposing a fake ServiceNow: incidents, CMDB and users.

Run with: python -m servicenow_mcp.server   (stdio transport)
"""

from datetime import date

from mcp.server.fastmcp import FastMCP

from . import briefing, store

mcp = FastMCP("servicenow")

WRITE_TOOLS = {"create_incident", "update_incident"}
VALID_STATES = ("New", "In Progress", "On Hold", "Resolved")


def _row(r) -> dict:
    return dict(r) if r is not None else None


@mcp.tool()
def search_incidents(
    query: str = "",
    ci: str = "",
    caller: str = "",
    priority: int = 0,
    open_only: bool = True,
    limit: int = 20,
) -> list[dict]:
    """Search incidents. Filters are combined with AND.

    query: words matched against the short description; ci: configuration item name;
    caller: user_name of the caller; priority: 1 (critical) to 4 (low), 0 for any;
    open_only: exclude resolved incidents.
    """
    sql = "SELECT number, short_description, ci, priority, state, assigned_group, caller, opened_at FROM incidents WHERE 1=1"
    args: list = []
    for word in query.split():
        sql += " AND short_description LIKE ?"
        args.append(f"%{word}%")
    if ci:
        sql += " AND LOWER(ci) = LOWER(?)"
        args.append(ci)
    if caller:
        with store.connect() as conn:
            known = conn.execute("SELECT 1 FROM users WHERE user_name = ?", (caller,)).fetchone()
        if not known:
            return [{"error": f"Unknown caller {caller!r}. Use find_user to get a user_name, or leave caller empty."}]
        sql += " AND caller = ?"
        args.append(caller)
    if priority:
        sql += " AND priority = ?"
        args.append(priority)
    if open_only:
        sql += f" AND state IN ({','.join('?' * len(store.OPEN_STATES))})"
        args.extend(store.OPEN_STATES)
    sql += " ORDER BY priority, number LIMIT ?"
    args.append(limit)
    with store.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


@mcp.tool()
def get_incident(number: str) -> dict:
    """Return every field of one incident, including description and work notes."""
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM incidents WHERE number = ?", (number.upper(),)).fetchone()
    if row is None:
        return {"error": f"Incident {number} not found."}
    return _row(row)


@mcp.tool()
def get_ci(name: str) -> dict:
    """Return a configuration item from the CMDB: class, environment, owner group, backup status, last seen."""
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM cmdb_ci WHERE LOWER(name) = LOWER(?)", (name.strip(),)).fetchone()
    if row is None:
        return {"error": f"Configuration item {name} not found."}
    ci = _row(row)
    ci["backup_enabled"] = bool(ci["backup_enabled"])
    return ci


@mcp.tool()
def list_cis(environment: str = "", ci_class: str = "", without_backup: bool = False) -> list[dict]:
    """List configuration items, optionally filtered by environment, class (partial match) or missing backup."""
    sql = "SELECT * FROM cmdb_ci WHERE 1=1"
    args: list = []
    if environment:
        sql += " AND LOWER(environment) = LOWER(?)"
        args.append(environment)
    if ci_class:
        sql += " AND ci_class LIKE ?"
        args.append(f"%{ci_class}%")
    if without_backup:
        sql += " AND backup_enabled = 0"
    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY name", args)]
    for r in rows:
        r["backup_enabled"] = bool(r["backup_enabled"])
    return rows


@mcp.tool()
def find_user(name: str) -> list[dict]:
    """Find users by part of their full name, user name or email."""
    like = f"%{name}%"
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE full_name LIKE ? OR user_name LIKE ? OR email LIKE ?",
            (like, like, like),
        )
        return [dict(r) for r in rows]


@mcp.tool()
def list_alerts(ci: str = "", firing_only: bool = True) -> list[dict]:
    """Monitoring alerts (Prometheus, Zabbix) received so far, with the incident each one is attached to."""
    sql = "SELECT source, ci, name, severity, status, incident, first_seen, last_seen, occurrences FROM alerts WHERE 1=1"
    args: list = []
    if ci:
        sql += " AND LOWER(ci) = LOWER(?)"
        args.append(ci)
    if firing_only:
        sql += " AND status = 'firing'"
    with store.connect() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY severity, last_seen DESC", args)]


@mcp.tool()
def morning_briefing(as_of: str = "", stale_days: int = 3) -> dict:
    """Health of the incident queue: open P1/P2, incidents with no update for stale_days,
    open incidents on production servers without backup, servers with several open incidents,
    open incidents per team. as_of is YYYY-MM-DD, empty for today."""
    return briefing.compute(date.fromisoformat(as_of) if as_of else None, stale_days)


@mcp.tool()
def create_incident(
    short_description: str,
    ci: str,
    priority: int,
    caller: str = "",
    description: str = "",
    assigned_group: str = "",
    different_problem: bool = False,
) -> dict:
    """Create an incident on an existing configuration item.

    priority: 1 critical outage, 2 high, 3 medium, 4 low. assigned_group defaults to the CI owner group.
    If the CI already has an open incident the call is refused: add a work note to that incident instead,
    or set different_problem=true only when the new issue is clearly unrelated.
    """
    if priority not in (1, 2, 3, 4):
        return {"error": "priority must be 1, 2, 3 or 4."}
    with store.connect() as conn:
        owner = conn.execute("SELECT owner_group FROM cmdb_ci WHERE name = ?", (ci,)).fetchone()
        if owner is None:
            return {"error": f"Configuration item {ci} not found; create the incident on an existing CI."}
        placeholders = ",".join("?" * len(store.OPEN_STATES))
        existing = conn.execute(
            f"SELECT number, short_description FROM incidents WHERE ci = ? AND state IN ({placeholders})",
            (ci, *store.OPEN_STATES),
        ).fetchall()
        if existing and not different_problem:
            found = "; ".join(f"{r['number']} ({r['short_description']})" for r in existing)
            return {"error": f"{ci} already has open incidents: {found}. Add a work note to the matching one "
                             "with update_incident instead of creating a duplicate."}
        number = store.next_number(conn)
        conn.execute(
            "INSERT INTO incidents (number, short_description, description, ci, priority, state,"
            " assigned_group, caller, opened_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (number, short_description, description, ci, priority, "New",
             assigned_group or owner[0], caller, store.now(), store.now()),
        )
    return {"created": number, "assigned_group": assigned_group or owner[0]}


@mcp.tool()
def update_incident(
    number: str,
    state: str = "",
    priority: int = 0,
    assigned_group: str = "",
    work_note: str = "",
    resolution_note: str = "",
) -> dict:
    """Update an incident. Only the fields you pass are changed.

    state: New, In Progress, On Hold or Resolved (Resolved requires resolution_note).
    work_note is appended to the incident history.
    """
    number = number.upper()
    with store.connect() as conn:
        inc = conn.execute("SELECT * FROM incidents WHERE number = ?", (number,)).fetchone()
        if inc is None:
            return {"error": f"Incident {number} not found."}
        changes = {}
        if state:
            if state not in VALID_STATES:
                return {"error": f"state must be one of {', '.join(VALID_STATES)}."}
            if state == "Resolved" and not (resolution_note or inc["resolution_note"]):
                return {"error": "Resolving an incident requires a resolution_note."}
            changes["state"] = state
        if priority:
            if priority not in (1, 2, 3, 4):
                return {"error": "priority must be 1, 2, 3 or 4."}
            changes["priority"] = priority
        if assigned_group:
            changes["assigned_group"] = assigned_group
        if resolution_note:
            changes["resolution_note"] = resolution_note
        if work_note:
            changes["work_notes"] = (inc["work_notes"] + f"\n[{store.now()}] {work_note}").strip()
        if not changes:
            return {"error": "Nothing to update."}
        changes["updated_at"] = store.now()
        sets = ", ".join(f"{k} = ?" for k in changes)
        conn.execute(f"UPDATE incidents SET {sets} WHERE number = ?", (*changes.values(), number))
    return {"updated": number, "fields": sorted(k for k in changes if k != "updated_at")}


if __name__ == "__main__":
    mcp.run()
