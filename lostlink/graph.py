"""LangGraph multi-agent workflow.

Chat graph (one thread per student conversation)

    START -> router -> intake ---(report filed)--> auto_match -> END
                    -> matcher -------------------------------> END
                    -> verifier --(claim passed)--> escalate --> END
                    -> general -------------------------------> END
    announce -> END   (used to post the security officer's decision into the chat)

Claim graph (one thread per claim, human-in-the-loop)

    START -> officer_review [interrupt: waits for the officer] -> finalize -> END
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Annotated, Literal, TypedDict

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from . import config, prompts

AGENT_LABELS = {
    "intake": "Intake Agent",
    "matcher": "Matching Agent",
    "verifier": "Verification Agent",
    "general": "Front Desk",
}


# --------------------------------------------------------------------------- state

class ChatState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: str
    user_name: str
    route: str
    last_agent: str
    events: list[dict]
    pending_claim: dict | None


class ClaimState(TypedDict, total=False):
    claim_code: str
    found_code: str
    user_id: str
    student_thread: str
    verdict: str
    confidence_band: str
    decision: dict
    result: dict


class RouteDecision(BaseModel):
    """Which specialist agent should handle the user's latest message."""
    route: Literal["intake", "matcher", "verifier", "general"]
    reason: str = Field(description="One short sentence explaining the choice.")


# --------------------------------------------------------------------------- helpers

def condense(messages: list[AnyMessage], limit: int = 16) -> list[AnyMessage]:
    """Plain user/assistant text history for the model (tool chatter removed, saves tokens)."""
    out: list[AnyMessage] = []
    for m in messages:
        if isinstance(m, HumanMessage):
            out.append(HumanMessage(content=m.text))
        elif isinstance(m, AIMessage) and m.text.strip() and m.name != "router":
            out.append(AIMessage(content=m.text))
    out = out[-limit:]
    while out and not isinstance(out[0], HumanMessage):  # some providers need a user turn first
        out.pop(0)
    return out


def tool_results(messages: list[AnyMessage], tool_name: str) -> list[dict]:
    found = []
    for m in messages:
        if isinstance(m, ToolMessage) and m.name == tool_name:
            text = m.text if hasattr(m, "text") else str(m.content)
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict):
                found.append(data)
    return found


def router_note(text: str, **extra) -> AIMessage:
    """An invisible-to-the-model marker the UI shows as a routing/handoff line."""
    return AIMessage(content="", name="router", additional_kwargs={"note": text, **extra})


def _dates() -> dict:
    today = date.today()
    return {"today": today.isoformat(), "yesterday": (today - timedelta(days=1)).isoformat()}


# --------------------------------------------------------------------------- builder

def build_graphs(
    model: BaseChatModel,
    tools: dict[str, BaseTool],
    campus_info: str,
    user_name: str,
    officer_decide: Callable[[str, bool, str], Awaitable[dict]],
    checkpointer,
):
    from .mcp_client import AGENT_TOOLSETS

    ctx = {**_dates(), "campus_info": campus_info, "user_name": user_name}

    def specialist(key: str, prompt: str):
        return create_agent(
            model,
            tools=[tools[n] for n in AGENT_TOOLSETS[key] if n in tools],
            system_prompt=prompt.format(**ctx),
            name=f"{key}_agent",
        )

    intake_agent = specialist("intake", prompts.INTAKE)
    matcher_agent = specialist("matcher", prompts.MATCHER)
    verifier_agent = specialist("verifier", prompts.VERIFIER)
    general_prompt = prompts.GENERAL.format(**ctx)
    _router = {}

    def router_model():
        if "m" not in _router:  # built lazily so the officer side can run without an LLM
            _router["m"] = model.with_structured_output(RouteDecision)
        return _router["m"]

    async def run_specialist(agent, state: ChatState, key: str, extra: list[AnyMessage] | None = None):
        inputs = condense(state.get("messages", [])) + (extra or [])
        result = await agent.ainvoke({"messages": inputs}, config={"recursion_limit": 25})
        new = result["messages"][len(inputs):]
        for m in new:
            if isinstance(m, AIMessage):
                m.name = key
        return new

    # ---- nodes ----------------------------------------------------------------
    async def router(state: ChatState):
        last = state.get("last_agent") or "none"
        history = condense(state["messages"], limit=6)
        decision: RouteDecision = await router_model().ainvoke(
            [SystemMessage(prompts.ROUTER.format(last_agent=last)), *history]
        )
        note = router_note(f"Router → {AGENT_LABELS[decision.route]}: {decision.reason}",
                           route=decision.route)
        return {"route": decision.route, "messages": [note], "events": [], "pending_claim": None}

    async def intake(state: ChatState):
        new = await run_specialist(intake_agent, state, "intake")
        events = []
        for r in tool_results(new, "report_found_item"):
            if r.get("found_code"):
                events.append({"kind": "found", "code": r["found_code"]})
        for r in tool_results(new, "report_lost_item"):
            if r.get("lost_code"):
                events.append({"kind": "lost", "code": r["lost_code"]})
        return {"messages": new, "last_agent": "intake", "events": events}

    async def auto_match(state: ChatState):
        out: list[AnyMessage] = []
        for ev in state.get("events", []):
            template = prompts.MATCHER_EVENT_FOUND if ev["kind"] == "found" else prompts.MATCHER_EVENT_LOST
            instruction = template.format(code=ev["code"], threshold=config.AUTO_NOTIFY_THRESHOLD)
            out.append(router_note(
                f"Handoff: Intake Agent → Matching Agent (new {ev['kind']} report {ev['code']}, "
                "auto-matching without being asked)", route="matcher"))
            out += await run_specialist(matcher_agent, {"messages": state["messages"] + out}, "matcher",
                                        extra=[HumanMessage(content=instruction)])
        return {"messages": out, "last_agent": "matcher", "events": []}

    async def matcher(state: ChatState):
        return {"messages": await run_specialist(matcher_agent, state, "matcher"), "last_agent": "matcher"}

    async def verifier(state: ChatState):
        new = await run_specialist(verifier_agent, state, "verifier")
        pending = None
        for r in tool_results(new, "verify_claim"):
            if r.get("status") == "awaiting_officer" and r.get("claim_code"):
                pending = r
        return {"messages": new, "last_agent": "verifier", "pending_claim": pending}

    async def general(state: ChatState):
        reply = await model.ainvoke([SystemMessage(general_prompt), *condense(state["messages"])])
        msg = AIMessage(content=reply.text, name="general")
        return {"messages": [msg], "last_agent": "general"}

    async def escalate(state: ChatState, config: RunnableConfig):
        claim = state["pending_claim"]
        student_thread = config["configurable"]["thread_id"]
        await claim_graph.ainvoke(
            {"claim_code": claim["claim_code"], "found_code": claim["found_code"],
             "user_id": state.get("user_id", ""), "student_thread": student_thread,
             "verdict": claim.get("verdict", ""), "confidence_band": claim.get("confidence_band", "")},
            config={"configurable": {"thread_id": f"claim-{claim['claim_code']}"}},
        )
        note = router_note(
            f"Handoff: Verification Agent → Security Officer (human-in-the-loop). Claim "
            f"{claim['claim_code']} is paused until an officer approves or rejects it.",
            route="officer")
        return {"messages": [note], "pending_claim": None}

    async def announce(state: ChatState):
        return {}

    def pick_route(state: ChatState) -> str:
        return state["route"]

    def after_intake(state: ChatState) -> str:
        return "auto_match" if state.get("events") else END

    def after_verifier(state: ChatState) -> str:
        return "escalate" if state.get("pending_claim") else END

    chat = StateGraph(ChatState)
    for name, fn in [("router", router), ("intake", intake), ("auto_match", auto_match),
                     ("matcher", matcher), ("verifier", verifier), ("general", general),
                     ("escalate", escalate), ("announce", announce)]:
        chat.add_node(name, fn)
    chat.add_edge(START, "router")
    chat.add_conditional_edges("router", pick_route, ["intake", "matcher", "verifier", "general"])
    chat.add_conditional_edges("intake", after_intake, ["auto_match", END])
    chat.add_conditional_edges("verifier", after_verifier, ["escalate", END])
    for n in ("auto_match", "matcher", "general", "escalate", "announce"):
        chat.add_edge(n, END)
    chat_graph = chat.compile(checkpointer=checkpointer)

    # ---- claim graph (human-in-the-loop) -------------------------------------
    async def officer_review(state: ClaimState):
        decision = interrupt({
            "claim_code": state["claim_code"],
            "found_code": state["found_code"],
            "verdict": state.get("verdict", ""),
            "question": "Release this item to the claimant?",
        })
        return {"decision": decision}

    async def finalize(state: ClaimState):
        d = state["decision"]
        res = await officer_decide(state["claim_code"], bool(d.get("approve")), d.get("note", ""))
        if res.get("status") == "approved":
            text = (f"Good news - the security office approved claim {res['claim_code']} for "
                    f"{res['found_code']}. Pickup code: **{res['pickup_code']}**. Collect it from the "
                    "Security Office, Admin Block (9 AM - 5 PM) with your college ID.")
        else:
            reason = d.get("note") or "the details could not be confirmed"
            text = (f"The security office did not approve claim {state['claim_code']} "
                    f"({reason}). You can visit the office in person if you believe the item is yours.")
        if state.get("student_thread"):
            await chat_graph.aupdate_state(
                {"configurable": {"thread_id": state["student_thread"]}},
                {"messages": [AIMessage(content=text, name="security_office")]},
                as_node="announce",
            )
        return {"result": res}

    claim = StateGraph(ClaimState)
    claim.add_node("officer_review", officer_review)
    claim.add_node("finalize", finalize)
    claim.add_edge(START, "officer_review")
    claim.add_edge("officer_review", "finalize")
    claim.add_edge("finalize", END)
    claim_graph = claim.compile(checkpointer=checkpointer)

    return chat_graph, claim_graph
