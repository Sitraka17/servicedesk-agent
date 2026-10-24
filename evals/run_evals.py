"""Run the 20 evaluation cases against the agent and write a report.

  python -m evals.run_evals                 # all cases, model from DEMO_LLM
  python -m evals.run_evals --only 7 13     # selected cases
"""

import argparse
import asyncio
import json
import os
import sqlite3
import tempfile
import time
from collections import defaultdict
from datetime import datetime

import yaml
from langchain_core.messages import AIMessage

from agent.graph import build_graph, mcp_client, text_of
from agent.llm import default_spec, make_llm
from servicenow_mcp import store

HERE = os.path.dirname(os.path.abspath(__file__))


def check(case: dict, tools_called: list[str], answer: str, db: str) -> list[str]:
    """Return the list of failed checks (empty means the case passed)."""
    failures = []
    low = answer.lower()
    if case.get("tools_any") and not set(case["tools_any"]) & set(tools_called):
        failures.append(f"expected one of {case['tools_any']}, called {tools_called}")
    for name in case.get("tools_none", []):
        if name in tools_called:
            failures.append(f"must not call {name}")
    if case.get("answer_any") and not any(s.lower() in low for s in case["answer_any"]):
        failures.append(f"answer lacks any of {case['answer_any']}")
    for s in case.get("answer_all", []):
        if s.lower() not in low:
            failures.append(f"answer lacks {s!r}")
    for s in case.get("answer_none", []):
        if s.lower() in low:
            failures.append(f"answer mentions {s!r}")
    conn = sqlite3.connect(db)
    for q in case.get("sql", []):
        got = conn.execute(q["query"]).fetchone()[0]
        if str(got) != str(q["expect"]):
            failures.append(f"{q['query']} -> {got!r}, expected {q['expect']!r}")
    conn.close()
    return failures


async def run_case(case: dict, llm, workdir: str) -> dict:
    db = store.reset(os.path.join(workdir, f"case{case['id']}.db"))
    tools = await mcp_client(db).get_tools()
    graph = build_graph(llm, tools, approval="auto")
    start = time.perf_counter()
    try:
        result = await graph.ainvoke(
            {"messages": [("user", case["prompt"])]}, {"recursion_limit": 40}
        )
        messages = result["messages"]
        error = None
    except Exception as exc:  # a crash counts as a failure, the run goes on
        messages, error = [], f"{type(exc).__name__}: {str(exc)[:200]}"
    seconds = time.perf_counter() - start
    tools_called = [c["name"] for m in messages if isinstance(m, AIMessage) for c in m.tool_calls]
    answer = text_of(messages[-1]) if messages else ""
    failures = [error] if error else check(case, tools_called, answer, db)
    # A quota error says nothing about the agent: report it apart instead of as a failure.
    quota = bool(error) and ("RESOURCE_EXHAUSTED" in error or "429" in error)
    return {
        "id": case["id"], "category": case["category"], "prompt": case["prompt"],
        "passed": not failures, "quota_error": quota, "failures": failures, "tools_called": tools_called,
        "model_turns": sum(isinstance(m, AIMessage) for m in messages),
        "seconds": round(seconds, 1), "answer": answer,
        "tool_args": [{"tool": c["name"], "args": c["args"]} for m in messages if isinstance(m, AIMessage) for c in m.tool_calls],
    }


def write_report(results: list[dict], spec: str) -> str:
    skipped = [r for r in results if r.get("quota_error")]
    scored = [r for r in results if not r.get("quota_error")]
    passed = sum(r["passed"] for r in scored)
    by_cat = defaultdict(lambda: [0, 0])
    for r in scored:
        by_cat[r["category"]][0] += r["passed"]
        by_cat[r["category"]][1] += 1
    lines = [
        f"# Evaluation, {spec}, {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"**{passed}/{len(scored)} scored cases passed** "
        f"({100 * passed / max(len(scored), 1):.0f} %), "
        f"median latency {sorted(r['seconds'] for r in scored)[len(scored) // 2] if scored else '-'} s, "
        f"{sum(len(r['tools_called']) for r in scored)} tool calls in total."
        + (f" {len(skipped)} case(s) not scored because of API quota: {', '.join(str(r['id']) for r in skipped)}." if skipped else ""),
        "",
        "| Category | Passed |",
        "|---|---|",
        *[f"| {c} | {p}/{n} |" for c, (p, n) in by_cat.items()],
        "",
        "| # | Category | Result | Tools called | Turns | s | Failure |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['id']} | {r['category']} | {'quota' if r.get('quota_error') else 'pass' if r['passed'] else 'FAIL'} | "
            f"{', '.join(r['tools_called']) or '-'} | {r['model_turns']} | {r['seconds']} | "
            f"{'; '.join(r['failures']).replace('|', '/') or ''} |"
        )
    return "\n".join(lines) + "\n"


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", type=int, nargs="*")
    args = parser.parse_args()
    with open(os.path.join(HERE, "cases.yaml")) as f:
        cases = yaml.safe_load(f)
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    spec = default_spec()
    llm = make_llm(spec)
    results = []
    with tempfile.TemporaryDirectory() as workdir:
        for case in cases:
            r = await run_case(case, llm, workdir)
            results.append(r)
            status = "quota" if r["quota_error"] else "pass" if r["passed"] else "FAIL"
            print(f"[{status}] {r['id']:>2} {r['category']:<13} {r['seconds']:>5}s  {'; '.join(r['failures'])[:200]}")
    out = os.path.join(HERE, "results")
    os.makedirs(out, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    slug = spec.replace(":", "_").replace("/", "_")
    with open(os.path.join(out, f"{stamp}_{slug}.json"), "w") as f:
        json.dump({"model": spec, "results": results}, f, indent=2, ensure_ascii=False)
    report = write_report(results, spec)
    with open(os.path.join(out, "latest.md"), "w") as f:
        f.write(report)
    print("\n" + report.split("\n")[2])


if __name__ == "__main__":
    asyncio.run(main())
