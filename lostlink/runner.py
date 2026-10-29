"""Entry points used by the Streamlit app, the CLI and the tests.

Each call opens one MCP session (the server runs as a subprocess over stdio),
builds the graphs with a SQLite checkpointer, does its work and closes everything.
Conversation state survives between calls because it lives in the checkpointer.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from . import config, mcp_client
from .graph import build_graphs
from .llm import get_chat_model


@asynccontextmanager
async def runtime(user_id: str = "", user_name: str = "", thread_id: str = "",
                  model: BaseChatModel | None = None, need_llm: bool = True):
    if model is None:
        model = get_chat_model() if need_llm else FakeListChatModel(responses=["(no model)"])
    async with mcp_client.open_session() as session:
        async with AsyncSqliteSaver.from_conn_string(str(config.CHECKPOINT_DB)) as saver:
            tools = await mcp_client.load_agent_tools(session, user_id, thread_id)
            campus = await mcp_client.read_campus_info(session)

            async def officer_decide(code: str, approve: bool, note: str) -> dict:
                return await mcp_client.call_officer_tool(
                    session, "officer_decide_claim", claim_code=code, approve=approve, note=note)

            chat, claim = build_graphs(model, tools, campus, user_name, officer_decide, saver)
            yield SimpleNamespace(session=session, chat=chat, claim=claim, saver=saver, tools=tools)


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}


async def run_turn(user_id: str, user_name: str, thread_id: str, text: str,
                   model: BaseChatModel | None = None) -> list[AnyMessage]:
    """Send one user message through the multi-agent graph; return the new messages."""
    async with runtime(user_id, user_name, thread_id, model=model) as rt:
        cfg = _cfg(thread_id)
        snap = await rt.chat.aget_state(cfg)
        before = len(snap.values.get("messages", [])) if snap.values else 0
        result = await rt.chat.ainvoke(
            {"messages": [HumanMessage(content=text)], "user_id": user_id, "user_name": user_name}, cfg)
        return result["messages"][before:]


async def load_thread(thread_id: str) -> list[AnyMessage]:
    """Read a conversation straight from the checkpointer (no MCP or LLM needed)."""
    async with AsyncSqliteSaver.from_conn_string(str(config.CHECKPOINT_DB)) as saver:
        tup = await saver.aget_tuple(_cfg(thread_id))
    if not tup:
        return []
    return list(tup.checkpoint.get("channel_values", {}).get("messages", []))


async def resume_claim(claim_code: str, approve: bool, note: str = "") -> dict:
    """Officer decision: resumes the paused LangGraph claim workflow (human-in-the-loop)."""
    async with runtime(need_llm=False) as rt:
        cfg = _cfg(f"claim-{claim_code}")
        snap = await rt.claim.aget_state(cfg)
        if snap.next:
            out = await rt.claim.ainvoke(Command(resume={"approve": approve, "note": note}), cfg)
            return {**out.get("result", {}), "resumed_workflow": True}
        # Fallback: claim was created outside a chat workflow.
        res = await mcp_client.call_officer_tool(
            rt.session, "officer_decide_claim", claim_code=claim_code, approve=approve, note=note)
        if res.get("thread_id") and res.get("status"):
            text = (f"Security office decision on {claim_code}: {res['status']}."
                    + (f" Pickup code: **{res['pickup_code']}**." if res.get("pickup_code") else ""))
            await rt.chat.aupdate_state(_cfg(res["thread_id"]),
                                        {"messages": [AIMessage(content=text, name="security_office")]},
                                        as_node="announce")
        return {**res, "resumed_workflow": False}


async def student_snapshot(user_id: str, mark_read: bool = False) -> dict:
    async with mcp_client.open_session() as s:
        notes = await mcp_client.call_tool(s, "list_notifications", actor_id=user_id, mark_read=mark_read)
        reports = await mcp_client.call_tool(s, "my_reports", actor_id=user_id)
    return {"notifications": notes["notifications"], "reports": reports}


async def officer_snapshot() -> dict:
    async with mcp_client.open_session() as s:
        return {
            "pending": (await mcp_client.call_officer_tool(s, "officer_list_claims"))["claims"],
            "all_claims": (await mcp_client.call_officer_tool(s, "officer_list_claims", status=""))["claims"],
            "found": (await mcp_client.call_officer_tool(s, "officer_list_found"))["items"],
            "lost": (await mcp_client.call_officer_tool(s, "officer_list_lost"))["items"],
            "audit": (await mcp_client.call_officer_tool(s, "officer_audit_log", limit=60))["entries"],
        }


async def add_hidden_marks(found_code: str, marks: str) -> dict:
    async with mcp_client.open_session() as s:
        return await mcp_client.call_officer_tool(s, "officer_add_hidden_marks",
                                                  found_code=found_code, marks=marks)


async def list_users() -> list[dict]:
    async with mcp_client.open_session() as s:
        return (await mcp_client.call_tool(s, "list_users"))["users"]


async def architecture() -> dict:
    """Graph diagrams + the tool catalogue discovered from the MCP server."""
    async with runtime(need_llm=False) as rt:
        return {
            "chat_dot": to_dot(rt.chat.get_graph()),
            "claim_dot": to_dot(rt.claim.get_graph()),
            "chat_mermaid": rt.chat.get_graph().draw_mermaid(),
            "tools": await mcp_client.describe_tools(rt.session),
            "agent_tools": sorted(rt.tools),
        }


NODE_LABELS = {
    "__start__": "START", "__end__": "END", "router": "Router\\n(LLM structured output)",
    "intake": "Intake Agent", "auto_match": "Matching Agent\\n(automatic handoff)",
    "matcher": "Matching Agent", "verifier": "Verification Agent", "general": "Front Desk",
    "escalate": "Escalate to officer", "announce": "Officer message",
    "officer_review": "Officer review\\n(interrupt - waits for human)", "finalize": "Finalize\\n(release or reject)",
}


def to_dot(graph) -> str:
    """Graphviz DOT for a compiled LangGraph (rendered in the browser by Streamlit)."""
    out = ['digraph G {', 'rankdir=TB; bgcolor="transparent"; nodesep=0.35; ranksep=0.45;',
           'node [shape=box, style="rounded,filled", fillcolor="#eef0ff", color="#7c8cff",'
           ' fontname="Helvetica", fontsize=11];',
           'edge [color="#8a93a6", arrowsize=0.7];']
    for nid in graph.nodes:
        if nid == "announce":  # entry point only used to post the officer's decision
            continue
        label = NODE_LABELS.get(nid, nid)
        extra = ', shape=oval, fillcolor="#e9ecef", color="#adb5bd"' if nid.startswith("__") else ""
        if nid in ("officer_review", "finalize", "escalate", "announce"):
            extra = ', fillcolor="#e7f6ec", color="#3c9a5f"'
        out.append(f'"{nid}" [label="{label}"{extra}];')
    for e in graph.edges:
        if "announce" in (e.source, e.target):
            continue
        style = ' [style=dashed]' if e.conditional else ''
        out.append(f'"{e.source}" -> "{e.target}"{style};')
    out.append("}")
    return "\n".join(out)


async def reset_demo() -> None:
    async with mcp_client.open_session() as s:
        await mcp_client.call_officer_tool(s, "officer_reset_demo")
    # Clear saved conversations. Emptying the tables works even on Windows, where a
    # file that is still open somewhere cannot be deleted.
    if config.CHECKPOINT_DB.exists():
        import sqlite3
        conn = sqlite3.connect(config.CHECKPOINT_DB, timeout=10)
        try:
            for table in ("checkpoints", "writes"):
                try:
                    conn.execute(f"DELETE FROM {table}")
                except sqlite3.OperationalError:  # table not created yet
                    pass
            conn.commit()
        finally:
            conn.close()
