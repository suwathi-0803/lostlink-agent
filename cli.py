"""Terminal version of LostLink (handy for quick testing without the web UI).

    python cli.py                 # log in as a student and chat
    python cli.py --officer       # log in as the security officer and approve / reject claims
"""
from __future__ import annotations

import argparse
import asyncio
from getpass import getpass

from langchain_core.messages import AIMessage, ToolMessage

from lostlink import runner
from lostlink.graph import AGENT_LABELS

LABELS = {**AGENT_LABELS, "security_office": "Security Office"}


def show(messages) -> None:
    for m in messages:
        if isinstance(m, AIMessage) and m.name == "router":
            print(f"  [{m.additional_kwargs.get('note', '')}]")
        elif isinstance(m, AIMessage) and m.tool_calls:
            for tc in m.tool_calls:
                print(f"  -> MCP tool {tc['name']}({tc['args']})")
        elif isinstance(m, ToolMessage):
            print(f"  <- {m.text[:160]}")
        elif isinstance(m, AIMessage) and m.text.strip():
            print(f"\n{LABELS.get(m.name, 'LostLink')}: {m.text}\n")


async def sign_in(login_id: str, role: str) -> dict | None:
    res = await runner.login(login_id, getpass("Password: "))
    if not res.get("ok") or res["role"] != role:
        print("Wrong register number or password.")
        return None
    return res


async def chat() -> None:
    user = await sign_in(input("Register number: ").strip(), "student")
    if not user:
        return
    user_id, name = user["user_id"], user["name"]
    thread = f"chat-{user_id}"
    print(f"LostLink - logged in as {name}. Type 'quit' to exit.\n")
    show(await runner.load_thread(thread))
    while True:
        text = input(f"{name}> ").strip()
        if text.lower() in {"quit", "exit"}:
            break
        if text:
            try:
                show(await runner.run_turn(user_id, name, thread, text))
            except Exception as exc:  # noqa: BLE001
                while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
                    exc = exc.exceptions[0]
                print(f"  ! {type(exc).__name__}: {exc}")


async def officer() -> None:
    if not await sign_in("security", "officer"):
        return
    data = await runner.officer_snapshot()
    if not data["pending"]:
        print("No claims waiting for approval.")
        return
    for c in data["pending"]:
        print(f"\n{c['claim_code']} on {c['found_code']} by {c.get('claimant_name')} - "
              f"AI verdict {c['verdict']} ({c['confidence']})")
        print(f"  answers     : {c['answers']}")
        print(f"  hidden marks: {c.get('hidden_marks')}")
        choice = input("  approve? [y/n/skip] ").strip().lower()
        if choice in {"y", "n"}:
            res = await runner.resume_claim(c["claim_code"], choice == "y", "decided via CLI")
            print(f"  -> {res.get('status')} {res.get('pickup_code', '')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--officer", action="store_true")
    args = ap.parse_args()
    asyncio.run(officer() if args.officer else chat())
