"""End-to-end plumbing through the real MCP server, driven by a scripted model (no LLM, no network)."""

import sqlite3

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent.graph import build_graph, mcp_client
from servicenow_mcp import store


class ScriptedModel(BaseChatModel):
    """Returns the scripted messages one after another, whatever it is asked."""

    script: list

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


def call(name, args, i=1):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{i}"}])


def scripted_create():
    return ScriptedModel(script=[
        call("get_ci", {"name": "db-prod-03"}),
        call("create_incident", {"short_description": "db-prod-03 not responding", "ci": "db-prod-03", "priority": 1}, 2),
        AIMessage(content="Created INC0010009 for DBA-Team. Note: db-prod-03 has no backup."),
    ])


async def test_agent_reads_then_writes_through_mcp(tmp_path):
    db = store.reset(str(tmp_path / "snow.db"))
    tools = await mcp_client(db).get_tools()
    graph = build_graph(scripted_create(), tools, approval="auto")
    result = await graph.ainvoke({"messages": [("user", "db-prod-03 is down")]})
    assert "INC0010009" in result["messages"][-1].content
    row = sqlite3.connect(db).execute("SELECT assigned_group FROM incidents WHERE number='INC0010009'").fetchone()
    assert row == ("DBA-Team",)


async def test_denied_write_is_not_executed(tmp_path):
    db = store.reset(str(tmp_path / "snow.db"))
    tools = await mcp_client(db).get_tools()
    model = scripted_create()
    model.script.insert(2, AIMessage(content="I would have created an incident for DBA-Team."))
    graph = build_graph(model, tools, approval="ask", checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "t1"}}
    paused = await graph.ainvoke({"messages": [("user", "db-prod-03 is down")]}, config)
    pending = paused["__interrupt__"][0].value["pending_writes"]
    assert pending[0]["tool"] == "create_incident"
    await graph.ainvoke(Command(resume="deny"), config)
    count = sqlite3.connect(db).execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
    assert count == 8
