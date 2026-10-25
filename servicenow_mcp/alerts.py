"""Turn monitoring alerts (Prometheus Alertmanager, Zabbix) into incidents, grouping duplicates.

Rules, in this order, for each server (CI) in a batch:
  1. Host not in the CMDB: no incident, the alert is reported as unmatched (that is a CMDB gap).
  2. The CI already has an open incident: add a work note to it, never a second incident.
  3. Otherwise: open one incident for all the new alerts of that CI.
  4. A resolved alert adds a note to its incident. Closing the incident stays a human decision.
An alert that keeps firing only bumps its counter, so a flapping check does not flood the ticket.

  python -m servicenow_mcp.alerts samples/alertmanager_firing.json
  python -m servicenow_mcp.alerts samples/zabbix_problem.json --source zabbix
  python -m servicenow_mcp.alerts --serve 8085      # POST /alertmanager and /zabbix
"""

import argparse
import json
import re
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import server, store

# Priority 1 critical ... 4 low, same scale as incidents.
PROMETHEUS_SEVERITY = {"critical": 1, "page": 1, "high": 2, "major": 2, "warning": 3, "minor": 3, "info": 4, "none": 4}
ZABBIX_SEVERITY = {"disaster": 1, "high": 2, "average": 3, "warning": 4, "information": 4, "not classified": 4}


@dataclass
class Alert:
    source: str
    key: str          # stable id of the check, so the same alert is recognised when it fires again
    host: str
    name: str
    severity: int
    firing: bool
    summary: str
    time: str


def parse_alertmanager(payload: dict) -> list[Alert]:
    """Alertmanager webhook, format version 4."""
    alerts = []
    for a in payload.get("alerts", []):
        labels, notes = a.get("labels", {}), a.get("annotations", {})
        host = labels.get("instance") or labels.get("host") or labels.get("node") or ""
        name = labels.get("alertname", "unnamed alert")
        alerts.append(Alert(
            source="prometheus",
            key=a.get("fingerprint") or f"prometheus:{name}:{host}",
            host=host,
            name=name,
            severity=PROMETHEUS_SEVERITY.get(labels.get("severity", "").lower(), 3),
            firing=a.get("status", payload.get("status")) == "firing",
            summary=notes.get("summary") or notes.get("description") or name,
            time=a.get("startsAt", "")[:16].replace("T", " "),
        ))
    return alerts


def parse_zabbix(payload: dict) -> list[Alert]:
    """Zabbix webhook media type. Expected fields, filled with Zabbix macros:
    trigger_id {TRIGGER.ID}, host {HOST.HOST}, trigger {EVENT.NAME}, severity {EVENT.SEVERITY},
    event_value {EVENT.VALUE} (1 problem, 0 recovery), time {EVENT.DATE} {EVENT.TIME}.
    A list of such objects is accepted too."""
    events = payload if isinstance(payload, list) else [payload]
    alerts = []
    for e in events:
        host = e.get("host", "")
        alerts.append(Alert(
            source="zabbix",
            key=f"zabbix:{e.get('trigger_id') or e.get('trigger')}:{host}",
            host=host,
            name=e.get("trigger", "Zabbix problem"),
            severity=ZABBIX_SEVERITY.get(str(e.get("severity", "")).lower(), 3),
            firing=str(e.get("event_value", "1")) == "1",
            summary=e.get("trigger", "Zabbix problem"),
            time=str(e.get("time", "")),
        ))
    return alerts


def ci_for_host(host: str, conn) -> tuple[str, str] | None:
    """Map 'db-prod-03:9100' or 'DB-PROD-03.example.lu' to the CMDB name and environment."""
    name = re.split(r"[:.]", host.strip().lower())[0]
    row = conn.execute("SELECT name, environment FROM cmdb_ci WHERE LOWER(name) = ?", (name,)).fetchone()
    return (row["name"], row["environment"]) if row else None


def open_incident_on(ci: str, conn) -> str | None:
    states = ",".join("?" * len(store.OPEN_STATES))
    row = conn.execute(
        f"SELECT number FROM incidents WHERE ci = ? AND state IN ({states}) ORDER BY priority, number LIMIT 1",
        (ci, *store.OPEN_STATES),
    ).fetchone()
    return row["number"] if row else None


def ingest(alerts: list[Alert]) -> list[dict]:
    """Apply the rules and return what was done, one line per action."""
    actions = []
    by_ci: dict[str, list[Alert]] = {}
    with store.connect() as conn:
        known = {r["alert_key"]: dict(r) for r in conn.execute("SELECT * FROM alerts")}
        env_of = {}
        for a in alerts:
            match = ci_for_host(a.host, conn)
            if match is None:
                actions.append({"action": "unmatched", "host": a.host, "alert": a.name,
                                "detail": "host not in the CMDB, no incident opened"})
                continue
            ci, env_of[ci] = match
            by_ci.setdefault(ci, []).append(a)

    for ci, batch in by_ci.items():
        # The same check can appear twice in one payload: keep its last state only.
        latest = {a.key: a for a in batch}
        firing = [a for a in latest.values() if a.firing]
        resolved = [a for a in latest.values() if not a.firing]

        with store.connect() as conn:
            still_open = {r[0] for r in conn.execute(
                f"SELECT number FROM incidents WHERE state IN ({','.join('?' * len(store.OPEN_STATES))})",
                store.OPEN_STATES)}
        repeats = [a for a in firing if a.key in known and known[a.key]["status"] == "firing"
                   and known[a.key]["incident"] in still_open]
        new = [a for a in firing if a not in repeats]

        with store.connect() as conn:
            for a in repeats:
                conn.execute("UPDATE alerts SET occurrences = occurrences + 1, last_seen = ? WHERE alert_key = ?",
                             (store.now(), a.key))
                actions.append({"action": "repeat", "ci": ci, "alert": a.name,
                                "incident": known[a.key]["incident"], "detail": "still firing, counter bumped"})
            existing = open_incident_on(ci, conn)

        if new:
            lines = "; ".join(f"[{a.source}] {a.name}: {a.summary} ({a.time})" for a in new)
            if existing:
                server.update_incident(existing, work_note=f"New alerts on {ci}: {lines}")
                number, verb = existing, "noted"
            else:
                priority = min(a.severity for a in new)
                if env_of[ci] != "production":
                    priority = max(priority, 3)  # a test box going down is not a P1
                worst = min(new, key=lambda a: a.severity)
                title = worst.summary + (f" (+{len(new) - 1} other alerts)" if len(new) > 2 else
                                         " (+1 other alert)" if len(new) == 2 else "")
                created = server.create_incident(title, ci=ci, priority=priority, caller="monitoring",
                                                 description=lines)
                number, verb = created["created"], "created"
            with store.connect() as conn:
                for a in new:
                    conn.execute(
                        "INSERT INTO alerts (alert_key, source, host, ci, name, severity, status, incident, first_seen, last_seen)"
                        " VALUES (?,?,?,?,?,?,'firing',?,?,?) ON CONFLICT(alert_key) DO UPDATE SET"
                        " status='firing', incident=excluded.incident, last_seen=excluded.last_seen,"
                        " occurrences=occurrences+1",
                        (a.key, a.source, a.host, ci, a.name, a.severity, number, store.now(), store.now()),
                    )
            actions.append({"action": verb, "ci": ci, "incident": number, "alerts": [a.name for a in new]})

        for a in resolved:
            incident = known.get(a.key, {}).get("incident")
            with store.connect() as conn:
                conn.execute("UPDATE alerts SET status = 'resolved', last_seen = ? WHERE alert_key = ?",
                             (store.now(), a.key))
                still = conn.execute("SELECT COUNT(*) FROM alerts WHERE incident = ? AND status = 'firing'",
                                     (incident,)).fetchone()[0] if incident else 0
            if incident:
                note = f"Alert cleared: {a.name} on {ci}."
                if still == 0:
                    note += " No alert is firing on this incident anymore, it can probably be resolved."
                server.update_incident(incident, work_note=note)
            actions.append({"action": "cleared", "ci": ci, "alert": a.name, "incident": incident,
                            "detail": "all alerts cleared" if incident and still == 0 else ""})
    return actions


PARSERS = {"alertmanager": parse_alertmanager, "prometheus": parse_alertmanager, "zabbix": parse_zabbix}


def serve(port: int):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            source = self.path.strip("/").split("/")[0]
            if source not in PARSERS:
                self.send_error(404, "POST to /alertmanager or /zabbix")
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                result = ingest(PARSERS[source](json.loads(body)))
            except (ValueError, KeyError) as exc:
                self.send_error(400, f"bad payload: {exc}")
                return
            out = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    print(f"Listening on http://127.0.0.1:{port}/alertmanager and /zabbix")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main():
    parser = argparse.ArgumentParser(description="Turn monitoring alerts into incidents.")
    parser.add_argument("file", nargs="?", help="JSON payload to ingest")
    parser.add_argument("--source", choices=sorted(PARSERS), default="alertmanager")
    parser.add_argument("--serve", type=int, metavar="PORT", help="run a webhook receiver instead")
    args = parser.parse_args()
    if args.serve:
        serve(args.serve)
        return
    if not args.file:
        parser.error("give a JSON file or --serve PORT")
    with open(args.file) as f:
        for action in ingest(PARSERS[args.source](json.load(f))):
            print(json.dumps(action))


if __name__ == "__main__":
    main()
