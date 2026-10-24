"""Interactive chat with the service desk agent. Writes to ServiceNow need your approval.

Run with: python -m agent.cli
"""

import asyncio
import uuid

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from servicenow_mcp import store

from .graph import build_graph, mcp_client, text_of
from .llm import default_spec, make_llm


async def main():
    store.reset()
    client = mcp_client()
    tools = await client.get_tools()
    graph = build_graph(make_llm(), tools, approval="ask", checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 40}
    print(f"Service desk agent ({default_spec()}). Fresh demo data loaded. Empty line to quit.\n")
    while True:
        text = input("you> ").strip()
        if not text:
            return
        result = await graph.ainvoke({"messages": [("user", text)]}, config)
        while "__interrupt__" in result:
            for write in result["__interrupt__"][0].value["pending_writes"]:
                print(f"  approve {write['tool']}({write['args']})?")
            answer = input("  [y/N] ").strip().lower()
            result = await graph.ainvoke(Command(resume="approve" if answer == "y" else "deny"), config)
        print(f"agent> {text_of(result['messages'][-1])}\n")


if __name__ == "__main__":
    asyncio.run(main())
