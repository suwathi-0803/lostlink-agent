"""MCP client side: starts the LostLink MCP server over stdio and turns its tools
into LangChain tools for the agents.

Two safety layers live here:
1. Identity injection - `actor_id` and `thread_id` are removed from the schema the
   LLM sees and filled in from the logged-in session by an interceptor, so the model
   cannot act as another user.
2. Tool allowlists - each agent only receives the tools it needs; officer tools
   are never handed to any agent (the server also demands OFFICER_TOKEN).
"""
from __future__ import annotations

import copy
import json
import os
import sys
from contextlib import asynccontextmanager

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from . import config

SERVER_SCRIPT = config.PROJECT_ROOT / "mcp_server" / "server.py"
SERVER_NAME = "lostlink"

INJECTED_ARGS = ("actor_id", "thread_id")

AGENT_TOOLSETS = {
    "intake": ["report_lost_item", "report_found_item"],
    "matcher": ["search_found_items", "find_matches_for_lost", "find_matches_for_found",
                "notify_owner_of_match", "my_reports"],
    "verifier": ["get_verification_questions", "verify_claim", "search_found_items", "my_reports"],
}


def server_connection() -> dict:
    env = dict(os.environ)
    env.update({
        "LOSTLINK_DB": str(config.DB_PATH),
        "OFFICER_TOKEN": config.OFFICER_TOKEN,
        "PYTHONIOENCODING": "utf-8",
    })
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(SERVER_SCRIPT)],
        "env": env,
        "cwd": str(config.PROJECT_ROOT),
    }


@asynccontextmanager
async def open_session():
    """One MCP session (one server process) for the duration of a request."""
    client = MultiServerMCPClient({SERVER_NAME: server_connection()})
    async with client.session(SERVER_NAME) as session:
        yield session


def _identity_injector(actor_id: str, thread_id: str, accepts: dict[str, set[str]]):
    async def inject(request, handler):
        args = {k: v for k, v in request.args.items() if k not in INJECTED_ARGS}  # never trust the LLM
        wanted = accepts.get(request.name, set())
        if "actor_id" in wanted:
            args["actor_id"] = actor_id
        if "thread_id" in wanted:
            args["thread_id"] = thread_id
        return await handler(request.override(args=args))
    return inject


def _hide_args(tool: BaseTool) -> BaseTool:
    schema = copy.deepcopy(tool.args_schema) if isinstance(tool.args_schema, dict) else None
    if schema:
        for name in INJECTED_ARGS:
            schema.get("properties", {}).pop(name, None)
            if name in schema.get("required", []):
                schema["required"].remove(name)
        tool.args_schema = schema
    return tool


async def load_agent_tools(session, actor_id: str, thread_id: str) -> dict[str, BaseTool]:
    """Load the student-safe tools, with identity injected and hidden from the model."""
    listing = await session.list_tools()
    accepts = {
        t.name: {a for a in INJECTED_ARGS if a in (t.inputSchema or {}).get("properties", {})}
        for t in listing.tools
    }
    tools = await load_mcp_tools(
        session,
        tool_interceptors=[_identity_injector(actor_id, thread_id, accepts)],
        server_name=SERVER_NAME,
    )
    allowed = {name for names in AGENT_TOOLSETS.values() for name in names}
    return {t.name: _hide_args(t) for t in tools if t.name in allowed}


def _parse_result(result) -> dict:
    text = "".join(getattr(c, "text", "") for c in (result.content or []))
    if result.isError:
        raise RuntimeError(text or "MCP tool error")
    if result.structuredContent is not None:
        data = result.structuredContent
        return data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"text": text}


async def call_tool(session, name: str, **arguments) -> dict:
    """Call an MCP tool directly (used by the dashboard and the officer workflow)."""
    return _parse_result(await session.call_tool(name, arguments))


async def call_officer_tool(session, name: str, **arguments) -> dict:
    return await call_tool(session, name, officer_token=config.OFFICER_TOKEN, **arguments)


async def read_campus_info(session) -> str:
    res = await session.read_resource("lostlink://campus-info")
    return "".join(getattr(c, "text", "") for c in res.contents)


async def describe_tools(session) -> list[dict]:
    listing = await session.list_tools()
    return [{"name": t.name, "description": (t.description or "").strip().split("\n")[0]}
            for t in listing.tools]
