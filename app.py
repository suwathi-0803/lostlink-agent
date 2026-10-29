"""LostLink - Streamlit front end.

Run:  streamlit run app.py
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pandas as pd
import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from lostlink import config, runner
from lostlink.graph import AGENT_LABELS
from lostlink.llm import MissingAPIKey, check_api_key

st.set_page_config(page_title="LostLink - Agentic Campus Lost & Found", page_icon="🔎", layout="wide")

AVATARS = {"intake": "📝", "matcher": "🔗", "verifier": "🛡️", "general": "💬", "security_office": "👮"}
LABELS = {**AGENT_LABELS, "security_office": "Security Office (human)"}
EXAMPLES = [
    "I lost my blue Milton water bottle in the library yesterday. It has a Pikachu sticker near the cap.",
    "Has anyone found a black wallet near the food court?",
    "I found a black Puma backpack at the bus bay just now. There's a Casio calculator and a notebook with the name Meera inside.",
    "What can you do?",
]

st.markdown("""
<style>
.block-container {padding-top: 1.6rem; max-width: 1200px;}
.route-note {font-size: 0.82rem; color: #5b6472; background: rgba(120,130,150,0.10);
  border-left: 3px solid #7c8cff; padding: 4px 10px; margin: 2px 0 8px 0; border-radius: 4px;}
.agent-badge {display:inline-block; font-size:0.72rem; font-weight:600; letter-spacing:.02em;
  padding:1px 8px; border-radius:999px; background:#eef0ff; color:#3b44a8; margin-bottom:4px;}
.badge-security_office {background:#e7f6ec; color:#1d6b3a;}
.secret-box {background: rgba(255,170,0,0.08); border:1px dashed #d9a400; padding:8px 10px;
  border-radius:6px; font-size:0.9rem; white-space: pre-wrap;}
.small-muted {font-size:0.8rem; color:#7a8290;}
</style>
""", unsafe_allow_html=True)


def arun(coro):
    return asyncio.run(coro)


def root_cause(exc: BaseException) -> BaseException:
    """MCP sessions run inside task groups, so real errors arrive wrapped in ExceptionGroups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def friendly_error(exc: BaseException) -> str:
    exc = root_cause(exc)
    text = str(exc)
    if isinstance(exc, MissingAPIKey):
        return text
    if "429" in text or "rate limit" in text.lower() or "quota" in text.lower():
        return ("The free LLM API rate limit was reached. Wait about a minute and send the message "
                "again (or switch provider/model in .env).")
    if "api key" in text.lower() or "401" in text:
        return "The API key was rejected. Check the key in your .env file."
    if "connect" in type(exc).__name__.lower() or "connection error" in text.lower():
        return "Couldn't reach the LLM API. Check your internet connection and try again."
    return f"{type(exc).__name__}: {text[:400]}"


@st.cache_data(ttl=300, show_spinner=False)
def get_users() -> list[dict]:
    return arun(runner.list_users())


# --------------------------------------------------------------------------- chat rendering

def render_messages(messages: list) -> None:
    calls: dict[str, dict] = {}
    pending: list[dict] = []
    for m in messages:
        if isinstance(m, HumanMessage):
            with st.chat_message("user"):
                st.markdown(m.text)
        elif isinstance(m, AIMessage) and m.name == "router":
            note = m.additional_kwargs.get("note", "")
            st.markdown(f"<div class='route-note'>🧭 {note}</div>", unsafe_allow_html=True)
        elif isinstance(m, AIMessage) and m.tool_calls:
            for tc in m.tool_calls:
                entry = {"tool": tc["name"], "args": tc["args"], "result": None}
                calls[tc["id"]] = entry
                pending.append(entry)
        elif isinstance(m, ToolMessage):
            if m.tool_call_id in calls:
                calls[m.tool_call_id]["result"] = m.text
        elif isinstance(m, AIMessage) and m.text.strip():
            key = m.name or "general"
            with st.chat_message("assistant", avatar=AVATARS.get(key, "🤖")):
                st.markdown(f"<span class='agent-badge badge-{key}'>{LABELS.get(key, key)}</span>",
                            unsafe_allow_html=True)
                st.markdown(m.text)
                if pending:
                    with st.expander(f"MCP tool calls ({len(pending)})"):
                        for e in pending:
                            st.markdown(f"**`{e['tool']}`**")
                            st.code(json.dumps(e["args"], indent=2, ensure_ascii=False), language="json")
                            if e["result"]:
                                try:
                                    pretty = json.dumps(json.loads(e["result"]), indent=2, ensure_ascii=False)
                                except Exception:
                                    pretty = e["result"]
                                st.caption("Result")
                                st.code(pretty[:2500], language="json")
            pending = []


# --------------------------------------------------------------------------- sidebar

users = get_users()
students = [u for u in users if u["role"] == "student"]

with st.sidebar:
    st.markdown("## 🔎 LostLink")
    st.caption("Agentic campus lost & found · LangGraph multi-agent + MCP tools")
    view = st.radio("View", ["Student", "Security Officer", "How it works"], label_visibility="collapsed")
    st.divider()

    key_problem = check_api_key()
    st.markdown(f"<div class='small-muted'>LLM: <b>{config.LLM_PROVIDER}</b> · {config.LLM_MODEL}</div>",
                unsafe_allow_html=True)
    if key_problem:
        st.warning(key_problem)

# --------------------------------------------------------------------------- student view

if view == "Student":
    with st.sidebar:
        st.divider()
        names = {u["id"]: u["name"] for u in students}
        user_id = st.selectbox("Logged in as", list(names), format_func=lambda k: names[k])
        user_name = names[user_id]
        threads = st.session_state.setdefault("threads", {})
        thread_id = threads.setdefault(user_id, f"chat-{user_id}")
        c1, c2 = st.columns(2)
        if c1.button("New chat", width="stretch"):
            threads[user_id] = f"chat-{user_id}-{uuid.uuid4().hex[:6]}"
            st.rerun()
        if c2.button("Refresh", width="stretch"):
            st.rerun()
        try:
            snap = arun(runner.student_snapshot(user_id, mark_read=True))
        except Exception as exc:  # noqa: BLE001
            snap = {"notifications": [], "reports": {}}
            st.error(friendly_error(exc))
        notes = snap["notifications"]
        unread = sum(1 for n in notes if not n["is_read"])
        with st.expander(f"🔔 Notifications ({unread} new)" if unread else "🔔 Notifications", expanded=bool(unread)):
            if not notes:
                st.caption("Nothing yet.")
            for n in notes[:8]:
                st.markdown(f"{'🟣 ' if not n['is_read'] else ''}{n['message']}")
                st.caption(n["created_at"])
        rep = snap.get("reports", {})
        with st.expander("📋 My reports"):
            for r in rep.get("lost_reports", []):
                st.markdown(f"**{r['lost_code']}** lost · {r['color']} {r['category']} · `{r['status']}`")
            for r in rep.get("found_reports", []):
                st.markdown(f"**{r['found_code']}** found · {r['color']} {r['category']} · `{r['status']}`")
            for c in rep.get("claims", []):
                extra = f" · pickup **{c['pickup_code']}**" if c.get("pickup_code") else ""
                st.markdown(f"**{c['claim_code']}** claim on {c['found_code']} · `{c['status']}`{extra}")
            if not any(rep.get(k) for k in ("lost_reports", "found_reports", "claims")):
                st.caption("No reports yet.")

    st.markdown(f"### Hi {user_name.split()[0]} 👋")
    st.caption("Tell LostLink what you lost or found, ask whether your item has turned up, "
               "or claim an item by its code (e.g. \"F001 is mine\").")

    history = arun(runner.load_thread(thread_id))
    render_messages(history)

    prompt = st.chat_input("Describe what you lost or found…")
    if not history and not prompt:
        st.markdown("**Try one of these:**")
        cols = st.columns(2)
        for i, ex in enumerate(EXAMPLES):
            if cols[i % 2].button(ex, key=f"ex{i}", width="stretch"):
                st.session_state["queued_prompt"] = ex
                st.rerun()
    prompt = prompt or st.session_state.pop("queued_prompt", None)

    if prompt:
        with st.chat_message("user"):
            st.markdown(prompt)
        error = None
        with st.spinner("Router and agents working…"):
            try:
                arun(runner.run_turn(user_id, user_name, thread_id, prompt))
            except Exception as exc:  # noqa: BLE001
                error = friendly_error(exc)
        if error:
            st.error(error)
            st.stop()
        st.rerun()

# --------------------------------------------------------------------------- officer view

elif view == "Security Officer":
    with st.sidebar:
        st.divider()
        if st.button("Reset demo data", width="stretch"):
            arun(runner.reset_demo())
            st.session_state.pop("threads", None)
            get_users.clear()
            st.success("Demo data reloaded.")
            st.rerun()

    st.markdown("### 👮 Security Office dashboard")
    st.caption("Human-in-the-loop: claims that pass the AI's blind verification wait here. "
               "Approving resumes the paused LangGraph workflow and releases the item.")
    data = arun(runner.officer_snapshot())
    names = {u["id"]: u["name"] for u in users}

    tabs = st.tabs([f"Pending approvals ({len(data['pending'])})", "Found register", "Lost reports",
                    "All claims", "Audit log"])

    with tabs[0]:
        if not data["pending"]:
            st.info("No claims waiting. Claims appear here after a student passes AI verification.")
        for c in data["pending"]:
            with st.container(border=True):
                st.markdown(f"#### {c['claim_code']} · {c['found_code']} — {c['color']} {c['category']} "
                            f"({c['location']})")
                conf = c["confidence"]
                m1, m2, m3 = st.columns(3)
                m1.metric("Claimant", c.get("claimant_name") or c["claimant_id"])
                m2.metric("AI verdict", c["verdict"].replace("_", " "))
                m3.metric("AI confidence", f"{conf * 100:.0f}%" if conf is not None else "n/a")
                a, b, d = st.columns(3)
                a.markdown("**Claimant's answers**")
                a.markdown(f"<div class='secret-box'>{c['answers']}</div>", unsafe_allow_html=True)
                b.markdown("**Hidden marks on item** 🔒")
                b.markdown(f"<div class='secret-box'>{c.get('hidden_marks') or '(none recorded)'}</div>",
                           unsafe_allow_html=True)
                d.markdown("**Owner's pre-registered details** 🔒")
                d.markdown(f"<div class='secret-box'>{c.get('prereg_secrets') or '(no earlier lost report)'}</div>",
                           unsafe_allow_html=True)
                note = st.text_input("Officer note", key=f"note-{c['claim_code']}",
                                     placeholder="e.g. College ID checked at counter")
                x, y, _ = st.columns([1, 1, 3])
                if x.button("✅ Approve & release", key=f"ok-{c['claim_code']}", type="primary"):
                    res = arun(runner.resume_claim(c["claim_code"], True, note))
                    st.success(f"Approved. Pickup code {res.get('pickup_code')} sent to the student.")
                    st.rerun()
                if y.button("❌ Reject", key=f"no-{c['claim_code']}"):
                    arun(runner.resume_claim(c["claim_code"], False, note or "Details could not be confirmed"))
                    st.warning("Claim rejected and the student was informed.")
                    st.rerun()

    with tabs[1]:
        df = pd.DataFrame(data["found"])
        if not df.empty:
            st.dataframe(df[["found_code", "category", "color", "brand", "location", "date_found",
                             "status", "finder", "hidden_marks", "description"]],
                         width="stretch", hide_index=True)
        with st.form("marks"):
            st.markdown("**Record hidden marks when an item is handed in**")
            fc = st.selectbox("Found item", [r["found_code"] for r in data["found"]])
            marks = st.text_area("Identifying marks (one per line)")
            if st.form_submit_button("Save marks") and marks.strip():
                arun(runner.add_hidden_marks(fc, marks))
                st.success(f"Saved for {fc}.")
                st.rerun()

    with tabs[2]:
        df = pd.DataFrame(data["lost"])
        if not df.empty:
            st.dataframe(df[["lost_code", "category", "color", "brand", "location", "date_lost", "status",
                             "owner", "secret_details", "description"]],
                         width="stretch", hide_index=True)

    with tabs[3]:
        df = pd.DataFrame(data["all_claims"])
        if not df.empty:
            df["claimant"] = df["claimant_id"].map(names)
            st.dataframe(df[["claim_code", "found_code", "claimant", "verdict", "confidence", "status",
                             "pickup_code", "officer_note", "created_at"]],
                         width="stretch", hide_index=True)
        else:
            st.caption("No claims yet.")

    with tabs[4]:
        st.dataframe(pd.DataFrame(data["audit"]), width="stretch", hide_index=True)

# --------------------------------------------------------------------------- architecture view

else:
    st.markdown("### 🧩 How LostLink works")
    arch = arun(runner.architecture())

    left, right = st.columns([3, 2])
    with left:
        st.markdown("**LangGraph – chat workflow (multi-agent supervisor)**")
        st.graphviz_chart(arch["chat_dot"], width="stretch")
    with right:
        st.markdown("**LangGraph – claim workflow (human-in-the-loop)**")
        st.graphviz_chart(arch["claim_dot"], width="content")
        st.markdown("""
- **Router** (LLM, structured output) picks a specialist each turn.
- **Intake Agent** extracts fields and splits public vs confidential details.
- **Matching Agent** scores lost ↔ found items; runs **automatically** after every new report.
- **Verification Agent** quizzes the claimant; the comparison happens **inside the MCP server**.
- **Security officer** approves via `interrupt()` / `Command(resume=…)`.
- State persists in a **SQLite checkpointer**.
""")
    st.markdown("**MCP tools discovered from the LostLink server**")
    tools = pd.DataFrame(arch["tools"])
    tools["available to agents"] = tools["name"].isin(arch["agent_tools"]).map({True: "yes", False: "no (officer/UI only)"})
    st.dataframe(tools, width="stretch", hide_index=True)
