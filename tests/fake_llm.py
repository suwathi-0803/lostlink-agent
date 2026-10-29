"""A deterministic stand-in for the LLM, used only by the automated tests.

It imitates what a real model does at each step (route, call a tool, write a reply)
so the LangGraph wiring, MCP tool calls, identity injection, handoffs and the
human-in-the-loop flow can be tested offline without an API key.
"""
from __future__ import annotations

import json
import re
import uuid

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult


def _call(name: str, **args) -> dict:
    return {"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "tool_call"}


def _json(msg: ToolMessage) -> dict:
    try:
        return json.loads(msg.text)
    except Exception:
        return {}


class FakeBrain(BaseChatModel):
    calls: list = []

    @property
    def _llm_type(self) -> str:
        return "fake-brain"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        msg = self._decide(messages)
        self.calls.append(msg)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    # ------------------------------------------------------------------ brain
    def _decide(self, messages) -> AIMessage:
        system = messages[0].content if isinstance(messages[0], SystemMessage) else ""
        last = messages[-1]
        humans = [m for m in messages if isinstance(m, HumanMessage)]
        user = humans[-1].text.lower() if humans else ""

        if "router of LostLink" in system:
            if "hidden" in user or "secret" in user or "sticker on f" in user:
                route = "general"
            elif "is mine" in user or "claim" in user or "verification agent" in system.lower() \
                    or ("previous turn was: verifier" in system):
                route = "verifier"
            elif user.startswith("i lost") or user.startswith("i found") or "found a" in user:
                route = "intake"
            elif "found?" in user or "search" in user or "any " in user:
                route = "matcher"
            else:
                route = "general"
            return AIMessage(content="", tool_calls=[_call("RouteDecision", route=route,
                                                           reason=f"keyword route to {route}")])

        if "Intake Agent" in system:
            if isinstance(last, ToolMessage):
                data = _json(last)
                code = data.get("lost_code") or data.get("found_code")
                return AIMessage(content=f"Report {code} filed. Identifying details kept private.")
            if "i lost" in user:
                return AIMessage(content="", tool_calls=[_call(
                    "report_lost_item", category="bottle", description="Blue Milton steel bottle",
                    location="Library", date_lost="", color="blue", brand="Milton",
                    secret_details="Pikachu sticker near the cap",
                    actor_id="u5-IMPERSONATION-ATTEMPT")])
            return AIMessage(content="", tool_calls=[_call(
                "report_found_item", category="bag", description="Black Puma backpack found at the bus bay",
                location="Bus Bay", date_found="", color="black", brand="Puma",
                hidden_marks="Casio calculator inside\nnotebook with the name Meera")])

        if "Matching Agent" in system:
            if isinstance(last, HumanMessage) and "SYSTEM EVENT" in last.text:
                code = re.search(r"\b([FL]\d{3})\b", last.text).group(1)
                tool = "find_matches_for_found" if code.startswith("F") else "find_matches_for_lost"
                arg = {"found_code": code} if code.startswith("F") else {"lost_code": code}
                return AIMessage(content="", tool_calls=[_call(tool, **arg)])
            if isinstance(last, ToolMessage) and last.name == "find_matches_for_found":
                data = _json(last)
                strong = [m for m in data.get("matches", []) if m["match_score"] >= 70]
                if strong:
                    return AIMessage(content="", tool_calls=[
                        _call("notify_owner_of_match", lost_code=m["lost_code"],
                              found_code=data["found_code"], note="Strong match") for m in strong])
                return AIMessage(content="No matching lost reports yet.")
            if isinstance(last, ToolMessage) and last.name == "notify_owner_of_match":
                n = sum(1 for m in messages if isinstance(m, ToolMessage) and m.name == "notify_owner_of_match")
                return AIMessage(content=f"I notified {n} possible owner(s).")
            if isinstance(last, ToolMessage) and last.name == "find_matches_for_lost":
                data = _json(last)
                top = data.get("matches", [])[:1]
                text = ", ".join(f"{m['found_code']} ({m['match_score']})" for m in top) or "none"
                return AIMessage(content=f"Best match: {text}. Say '<code> is mine' to claim.")
            return AIMessage(content="", tool_calls=[_call("search_found_items", query=user)])

        if "Verification Agent" in system:
            if isinstance(last, ToolMessage) and last.name == "get_verification_questions":
                qs = _json(last).get("questions", [])
                return AIMessage(content="Please answer:\n" + "\n".join(f"{i+1}. {q}" for i, q in enumerate(qs)))
            if isinstance(last, ToolMessage) and last.name == "verify_claim":
                d = _json(last)
                return AIMessage(content=f"Result: {d.get('status')} ({d.get('claim_code')}).")
            m = re.search(r"\b(f\d{3})\b", user)
            if m and "is mine" in user:
                return AIMessage(content="", tool_calls=[_call("get_verification_questions",
                                                               found_code=m.group(1).upper())])
            # user is answering: find the code from earlier conversation
            codes = re.findall(r"\b(F\d{3})\b", " ".join(x.text for x in messages[1:]), flags=re.I)
            code = codes[-1].upper() if codes else "F001"
            return AIMessage(content="", tool_calls=[_call("verify_claim", found_code=code,
                                                           answers=humans[-1].text, thread_id="spoofed")])

        # general / front desk
        return AIMessage(content="I can't share confidential details of any item. "
                                 "Ownership is checked privately during verification.")
