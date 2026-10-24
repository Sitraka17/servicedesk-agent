"""Morning briefing on the incident queue. Plain SQL checks, no LLM needed, so it can run from cron.

  python -m servicenow_mcp.briefing                      # today, Markdown on stdout
  python -m servicenow_mcp.briefing --as-of 2026-10-24 --out brief.md
  python -m servicenow_mcp.briefing --send               # posts to BRIEFING_WEBHOOK_URL (Slack-style {"text": ...})
"""

import argparse
import json
import os
import urllib.request
from collections import Counter
from datetime import date, timedelta

from . import store


def _days(since: str, as_of: date) -> int:
    return (as_of - date.fromisoformat(since[:10])).days


def compute(as_of: date | None = None, stale_days: int = 3) -> dict:
    """Return the briefing as plain data: urgent, stale, backup risks, repeat CIs, workload."""
    as_of = as_of or date.today()
    states = ",".join("?" * len(store.OPEN_STATES))
    with store.connect() as conn:
        open_inc = [dict(r) for r in conn.execute(
            f"SELECT number, short_description, ci, priority, state, assigned_group, opened_at, updated_at"
            f" FROM incidents WHERE state IN ({states}) ORDER BY priority, opened_at",
            store.OPEN_STATES,
        )]
        no_backup = {r[0] for r in conn.execute(
            "SELECT name FROM cmdb_ci WHERE environment = 'production' AND backup_enabled = 0"
        )}
    for inc in open_inc:
        inc["age_days"] = _days(inc["opened_at"], as_of)
        inc["idle_days"] = _days(inc["updated_at"] or inc["opened_at"], as_of)

    cutoff = as_of - timedelta(days=stale_days)
    per_ci = Counter(i["ci"] for i in open_inc)
    return {
        "as_of": as_of.isoformat(),
        "stale_days": stale_days,
        "open_total": len(open_inc),
        "urgent": [i for i in open_inc if i["priority"] <= 2],
        "stale": sorted(
            (i for i in open_inc if date.fromisoformat((i["updated_at"] or i["opened_at"])[:10]) < cutoff),
            key=lambda i: -i["idle_days"],
        ),
        "backup_risk": [i for i in open_inc if i["ci"] in no_backup],
        "repeat_cis": [
            {"ci": ci, "open": n, "numbers": [i["number"] for i in open_inc if i["ci"] == ci]}
            for ci, n in per_ci.most_common() if n > 1
        ],
        "per_group": dict(Counter(i["assigned_group"] for i in open_inc).most_common()),
    }


def render_markdown(b: dict) -> str:
    lines = [f"**Service desk, {b['as_of']}**: {b['open_total']} open incidents.", ""]

    lines.append(f"**P1 and P2 still open ({len(b['urgent'])})**")
    lines += [f"- {i['number']} P{i['priority']} on {i['ci']}: {i['short_description']}. "
              f"{i['state']}, {i['assigned_group']}, open {i['age_days']} d." for i in b["urgent"]] or ["- None."]
    lines.append("")

    lines.append(f"**No update for more than {b['stale_days']} days ({len(b['stale'])})**")
    lines += [f"- {i['number']} P{i['priority']} ({i['assigned_group']}): {i['short_description']}. "
              f"Last update {i['idle_days']} d ago." for i in b["stale"]] or ["- None."]
    lines.append("")

    lines.append(f"**Open incident on a production server with no backup ({len(b['backup_risk'])})**")
    lines += [f"- {i['number']} on {i['ci']}: {i['short_description']}. Check the backup before any fix."
              for i in b["backup_risk"]] or ["- None."]
    lines.append("")

    lines.append(f"**Same server, several open incidents ({len(b['repeat_cis'])})**")
    lines += [f"- {r['ci']}: {', '.join(r['numbers'])}. Maybe one root cause, worth a problem record."
              for r in b["repeat_cis"]] or ["- None."]
    lines.append("")

    lines.append("**Open incidents per team**")
    lines += [f"- {g}: {n}" for g, n in b["per_group"].items()]
    return "\n".join(lines) + "\n"


def send(text: str, url: str) -> int:
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status


def main():
    parser = argparse.ArgumentParser(description="Morning briefing on the incident queue.")
    parser.add_argument("--as-of", type=date.fromisoformat, help="date of the briefing, YYYY-MM-DD (default: today)")
    parser.add_argument("--stale-days", type=int, default=3)
    parser.add_argument("--out", help="write the Markdown to this file")
    parser.add_argument("--json", action="store_true", help="print the raw data instead of Markdown")
    parser.add_argument("--send", action="store_true", help="post to the webhook in BRIEFING_WEBHOOK_URL")
    args = parser.parse_args()

    data = compute(args.as_of, args.stale_days)
    text = json.dumps(data, indent=2) if args.json else render_markdown(data)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
    else:
        print(text)
    if args.send:
        url = os.environ.get("BRIEFING_WEBHOOK_URL")
        if not url:
            raise SystemExit("Set BRIEFING_WEBHOOK_URL to send the briefing.")
        print(f"Sent, HTTP {send(render_markdown(data), url)}")


if __name__ == "__main__":
    main()
