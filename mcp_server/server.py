"""LostLink MCP server.

Exposes the campus lost-and-found database as MCP tools and resources.
Any MCP client can use it: the LangGraph agents, the officer dashboard,
or an MCP-aware desktop app.

Run standalone (stdio):   python mcp_server/server.py

Security model
--------------
* Agent-facing tools never return confidential data (lost_secrets,
  found_hidden_marks).
* `actor_id` / `thread_id` are injected by the client from the logged-in
  session, not chosen by the LLM.
* Officer tools require OFFICER_TOKEN, which is never given to any agent.
"""
from __future__ import annotations

import os
import random
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from mcp_server import db, matching, seed, verification  # noqa: E402

CAMPUS_LOCATIONS = [
    "Library", "Food Court", "Main Block", "Lab Block", "Auditorium", "Admin Block",
    "Classroom Block", "Boys Hostel", "Girls Hostel", "Sports Ground", "Parking", "Bus Bay",
]
SECURITY_OFFICE = "Security Office, Admin Block (ground floor), 9:00 AM - 5:00 PM, Mon-Sat"

mcp = FastMCP(
    "LostLink",
    instructions="Campus lost-and-found tools: report, search, match, verify and release items.",
    log_level="WARNING",
)

seed.ensure_seeded()


# --------------------------------------------------------------------------- helpers

def _officer_ok(token: str) -> None:
    expected = os.getenv("OFFICER_TOKEN", "demo-officer-token")
    if not token or token != expected:
        raise PermissionError("Officer authorisation required. This tool is not available to students.")


def _public_found(row) -> dict:
    return {
        "found_code": db.found_code(row["id"]),
        "category": row["category"],
        "color": row["color"],
        "brand": row["brand"],
        "location": row["location"],
        "date_found": row["date_found"],
        "description": row["description"],
        "status": row["status"],
    }


def _public_lost(row) -> dict:
    return {
        "lost_code": db.lost_code(row["id"]),
        "category": row["category"],
        "color": row["color"],
        "brand": row["brand"],
        "location": row["location"],
        "date_lost": row["date_lost"],
        "description": row["description"],
        "status": row["status"],
    }


def _clean_date(value: str) -> str:
    value = (value or "").strip()
    return value or date.today().isoformat()


def _lines(text: str) -> str:
    parts = [p.strip(" -•\t") for p in (text or "").replace(";", "\n").split("\n")]
    return "\n".join(p for p in parts if p)


# --------------------------------------------------------------------------- resources

@mcp.resource("lostlink://campus-info")
def campus_info() -> str:
    """Campus locations, security office details and lost-and-found policy."""
    return (
        "Known campus locations: " + ", ".join(CAMPUS_LOCATIONS) + ".\n"
        f"Found items must be handed in at the {SECURITY_OFFICE}.\n"
        "Policy: items are released only after ownership verification and officer approval. "
        "Each student gets at most 2 verification attempts per item. "
        "Unclaimed items are kept for 30 days."
    )


# --------------------------------------------------------------------------- student-safe tools

@mcp.tool()
def report_lost_item(category: str, description: str, location: str = "", date_lost: str = "",
                     color: str = "", brand: str = "", secret_details: str = "",
                     actor_id: str = "") -> dict:
    """Record an item the current user has LOST.

    category: one of bottle, wallet, id_card, keys, phone, laptop, charger, earphones, bag,
      umbrella, book, calculator, watch, spectacles, jewellery, clothing, other.
    description: short PUBLIC description (no confidential details).
    location: campus place where it was lost. date_lost: YYYY-MM-DD.
    secret_details: CONFIDENTIAL identifying details only the owner knows (stickers, contents,
      names, engravings), one per line. Stored privately; never shown to other students.
    """
    if not actor_id:
        return {"error": "No logged-in user."}
    category = matching.normalize_category(category)
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO lost_items (owner_id, category, color, brand, location, date_lost,"
            " description, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (actor_id, category, color.strip(), brand.strip(), location.strip(),
             _clean_date(date_lost), description.strip(), db.now()),
        )
        lid = cur.lastrowid
        secrets = _lines(secret_details)
        if secrets:
            conn.execute("INSERT INTO lost_secrets (lost_id, details) VALUES (?, ?)", (lid, secrets))
        db.log(conn, "report_lost_item", actor_id, f"{db.lost_code(lid)} {category} at {location}")
    return {
        "lost_code": db.lost_code(lid),
        "category": category,
        "secret_details_saved": bool(secrets),
        "message": "Lost report filed. Confidential details are visible only to the security office.",
    }


@mcp.tool()
def report_found_item(category: str, description: str, location: str = "", date_found: str = "",
                      color: str = "", brand: str = "", hidden_marks: str = "",
                      actor_id: str = "") -> dict:
    """Record an item the current user has FOUND on campus.

    description: short PUBLIC description (what and where) - do not include names, contents,
      stickers or other identifying marks here.
    hidden_marks: identifying marks the finder noticed (names, contents, stickers, damage),
      one per line. Kept confidential and used only to verify the true owner.
    """
    if not actor_id:
        return {"error": "No logged-in user."}
    category = matching.normalize_category(category)
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO found_items (finder_id, category, color, brand, location, date_found,"
            " description, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (actor_id, category, color.strip(), brand.strip(), location.strip(),
             _clean_date(date_found), description.strip(), db.now()),
        )
        fid = cur.lastrowid
        marks = _lines(hidden_marks)
        if marks:
            conn.execute("INSERT INTO found_hidden_marks (found_id, marks) VALUES (?, ?)", (fid, marks))
        db.log(conn, "report_found_item", actor_id, f"{db.found_code(fid)} {category} at {location}")
    return {
        "found_code": db.found_code(fid),
        "category": category,
        "hidden_marks_saved": bool(marks),
        "next_step": f"Please hand the item in at the {SECURITY_OFFICE}.",
    }


@mcp.tool()
def search_found_items(query: str = "", category: str = "", location: str = "",
                       days: int = 30) -> dict:
    """Search PUBLIC listings of found items still waiting for their owner.

    query: free text (e.g. 'blue bottle'); category and location are optional filters;
    days: only items found in the last N days.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM found_items WHERE status != 'returned' "
            "AND date(date_found) >= date('now', ?) ORDER BY date_found DESC",
            (f"-{int(days)} day",),
        ).fetchall()
    cat = matching.normalize_category(category) if category else ""
    results = []
    for r in rows:
        if cat and r["category"] != cat:
            continue
        if location and location.lower() not in r["location"].lower():
            continue
        item = _public_found(r)
        if query:
            text = " ".join(str(v) for v in item.values()).lower()
            from rapidfuzz import fuzz
            rel = fuzz.token_set_ratio(query.lower(), text)
            if rel < 45:
                continue
            item["relevance"] = rel
        results.append(item)
    results.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    return {"count": len(results), "items": results[:10]}


@mcp.tool()
def find_matches_for_lost(lost_code: str, actor_id: str = "") -> dict:
    """Rank found items that could be the current user's lost item (score 0-100 with reasons)."""
    lid = db.parse_code(lost_code, "L")
    with db.connect() as conn:
        lost = conn.execute("SELECT * FROM lost_items WHERE id = ?", (lid,)).fetchone()
        if not lost:
            return {"error": f"No lost report {lost_code}."}
        if actor_id and lost["owner_id"] != actor_id:
            return {"error": "You can only run matching on your own lost reports."}
        found = conn.execute("SELECT * FROM found_items WHERE status != 'returned'").fetchall()
        ranked = matching.rank_matches(dict(lost), [dict(f) for f in found], target_is_lost=True)
        db.log(conn, "find_matches_for_lost", actor_id, f"{lost_code}: {len(ranked)} candidates")
    return {
        "lost_code": db.lost_code(lid),
        "matches": [
            {**_public_found(m["candidate"]), "match_score": m["score"], "reasons": m["reasons"]}
            for m in ranked
        ],
    }


@mcp.tool()
def find_matches_for_found(found_code: str) -> dict:
    """Rank open lost reports that a found item might belong to. Owner identities are NOT returned."""
    fid = db.parse_code(found_code, "F")
    with db.connect() as conn:
        found = conn.execute("SELECT * FROM found_items WHERE id = ?", (fid,)).fetchone()
        if not found:
            return {"error": f"No found item {found_code}."}
        lost = conn.execute("SELECT * FROM lost_items WHERE status != 'recovered'").fetchall()
        ranked = matching.rank_matches(dict(found), [dict(r) for r in lost], target_is_lost=False)
        db.log(conn, "find_matches_for_found", "", f"{found_code}: {len(ranked)} candidates")
    return {
        "found_code": db.found_code(fid),
        "matches": [
            {**_public_lost(m["candidate"]), "match_score": m["score"], "reasons": m["reasons"]}
            for m in ranked
        ],
    }


@mcp.tool()
def notify_owner_of_match(lost_code: str, found_code: str, note: str = "") -> dict:
    """Notify the owner of a lost report that a found item may be theirs.

    The server looks up the owner privately; their identity is never returned.
    """
    lid, fid = db.parse_code(lost_code, "L"), db.parse_code(found_code, "F")
    with db.connect() as conn:
        lost = conn.execute("SELECT * FROM lost_items WHERE id = ?", (lid,)).fetchone()
        found = conn.execute("SELECT * FROM found_items WHERE id = ?", (fid,)).fetchone()
        if not lost or not found:
            return {"error": "Unknown lost or found code."}
        msg = (f"Possible match for your lost report {db.lost_code(lid)}: found item "
               f"{db.found_code(fid)} ({found['color']} {found['category']}, {found['location']}). "
               f"{note.strip()} Ask LostLink to verify and claim it.").strip()
        db.notify(conn, lost["owner_id"], msg)
        if lost["status"] == "open":
            conn.execute("UPDATE lost_items SET status = 'matched' WHERE id = ?", (lid,))
        db.log(conn, "notify_owner_of_match", "", f"{db.lost_code(lid)} <- {db.found_code(fid)}")
    return {"notified": True, "lost_code": db.lost_code(lid), "found_code": db.found_code(fid)}


@mcp.tool()
def my_reports(actor_id: str = "") -> dict:
    """List the current user's lost reports, found reports, claims and notifications."""
    with db.connect() as conn:
        lost = conn.execute("SELECT * FROM lost_items WHERE owner_id = ?", (actor_id,)).fetchall()
        found = conn.execute("SELECT * FROM found_items WHERE finder_id = ?", (actor_id,)).fetchall()
        claims = conn.execute("SELECT * FROM claims WHERE claimant_id = ?", (actor_id,)).fetchall()
        notes = conn.execute(
            "SELECT message, created_at FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 5",
            (actor_id,),
        ).fetchall()
    return {
        "lost_reports": [_public_lost(r) for r in lost],
        "found_reports": [_public_found(r) for r in found],
        "claims": [
            {"claim_code": db.claim_code(c["id"]), "found_code": db.found_code(c["found_id"]),
             "status": c["status"], "pickup_code": c["pickup_code"] if c["status"] == "approved" else ""}
            for c in claims
        ],
        "recent_notifications": [dict(n) for n in notes],
    }


@mcp.tool()
def get_verification_questions(found_code: str) -> dict:
    """Get generic ownership questions for a found item. They never reveal its hidden marks."""
    fid = db.parse_code(found_code, "F")
    with db.connect() as conn:
        found = conn.execute("SELECT * FROM found_items WHERE id = ?", (fid,)).fetchone()
    if not found:
        return {"error": f"No found item {found_code}."}
    if found["status"] == "returned":
        return {"error": f"{found_code} has already been returned to its owner."}
    return {
        "found_code": db.found_code(fid),
        "category": found["category"],
        "questions": verification.questions_for(found["category"]),
        "instruction": "Ask the claimant these questions, then send ALL their answers to verify_claim.",
    }


@mcp.tool()
def verify_claim(found_code: str, answers: str, actor_id: str = "", thread_id: str = "") -> dict:
    """Blind-verify the current user's ownership claim on a found item.

    answers: the claimant's own answers to the verification questions, combined into one text.
    Comparison with the item's hidden marks happens privately inside this server; only a
    verdict and confidence band are returned. Max 2 attempts per user per item.
    """
    if not actor_id:
        return {"error": "No logged-in user."}
    fid = db.parse_code(found_code, "F")
    with db.connect() as conn:
        found = conn.execute("SELECT * FROM found_items WHERE id = ?", (fid,)).fetchone()
        if not found:
            return {"error": f"No found item {found_code}."}
        if found["status"] == "returned":
            return {"error": f"{found_code} has already been returned."}
        attempts = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE found_id = ? AND claimant_id = ?", (fid, actor_id)
        ).fetchone()[0]
        if attempts >= verification.MAX_ATTEMPTS:
            db.log(conn, "verify_claim", actor_id, f"{found_code}: blocked, attempt limit reached")
            return {"status": "locked",
                    "message": "Attempt limit reached for this item. Please visit the security office in person."}
        open_claim = conn.execute(
            "SELECT id FROM claims WHERE found_id = ? AND claimant_id = ? AND status = 'awaiting_officer'",
            (fid, actor_id),
        ).fetchone()
        if open_claim:
            return {"status": "awaiting_officer", "claim_code": db.claim_code(open_claim["id"]),
                    "message": "You already have a claim on this item waiting for the security officer."}

        marks_row = conn.execute("SELECT marks FROM found_hidden_marks WHERE found_id = ?", (fid,)).fetchone()
        marks = marks_row["marks"].split("\n") if marks_row else []

        # Pre-registered evidence: a lost report by this claimant, same category, filed earlier.
        prereg = conn.execute(
            "SELECT l.id, s.details FROM lost_items l LEFT JOIN lost_secrets s ON s.lost_id = l.id "
            "WHERE l.owner_id = ? AND l.category = ? AND l.status != 'recovered' ORDER BY l.id DESC LIMIT 1",
            (actor_id, found["category"]),
        ).fetchone()

        if marks:
            confidence = verification.score_answers(marks, answers)
            if prereg and prereg["details"]:
                pre = verification.score_answers(marks, prereg["details"])
                confidence = round(0.75 * confidence + 0.25 * pre, 3)
        else:
            confidence = None
        verdict = verification.verdict_for(confidence)
        status = "failed_verification" if verdict == "no_match" else "awaiting_officer"

        cur = conn.execute(
            "INSERT INTO claims (found_id, claimant_id, lost_id, answers, confidence, verdict, status,"
            " thread_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (fid, actor_id, prereg["id"] if prereg else None, answers, confidence, verdict, status,
             thread_id, db.now()),
        )
        cid = cur.lastrowid
        if status == "awaiting_officer":
            conn.execute("UPDATE found_items SET status = 'claim_pending' WHERE id = ?", (fid,))
        db.log(conn, "verify_claim", actor_id,
               f"{db.claim_code(cid)} on {found_code}: {verdict} (confidence {confidence})")

    left = verification.MAX_ATTEMPTS - attempts - 1
    if status == "awaiting_officer":
        message = "Verification passed the automated check and is forwarded to the security officer for approval."
    else:
        message = f"The answers did not match the item's identifying details. Attempts left: {left}."
    return {
        "claim_code": db.claim_code(cid),
        "found_code": db.found_code(fid),
        "verdict": verdict,
        "confidence_band": verification.band(confidence),
        "status": status,
        "attempts_left": left,
        "message": message,
    }


# --------------------------------------------------------------------------- officer-only tools

@mcp.tool()
def officer_list_claims(officer_token: str, status: str = "awaiting_officer") -> dict:
    """[OFFICER ONLY] Claims with full evidence (answers vs hidden marks). status='' for all."""
    _officer_ok(officer_token)
    with db.connect() as conn:
        sql = ("SELECT c.*, f.category, f.color, f.brand, f.location, f.description, "
               "m.marks AS hidden_marks, u.name AS claimant_name, s.details AS prereg_secrets "
               "FROM claims c JOIN found_items f ON f.id = c.found_id "
               "LEFT JOIN found_hidden_marks m ON m.found_id = f.id "
               "LEFT JOIN users u ON u.id = c.claimant_id "
               "LEFT JOIN lost_secrets s ON s.lost_id = c.lost_id ")
        rows = (conn.execute(sql + "WHERE c.status = ? ORDER BY c.id DESC", (status,)).fetchall()
                if status else conn.execute(sql + "ORDER BY c.id DESC").fetchall())
    out = []
    for r in rows:
        d = dict(r)
        d["claim_code"] = db.claim_code(r["id"])
        d["found_code"] = db.found_code(r["found_id"])
        d["lost_code"] = db.lost_code(r["lost_id"]) if r["lost_id"] else ""
        out.append(d)
    return {"claims": out}


@mcp.tool()
def officer_decide_claim(officer_token: str, claim_code: str, approve: bool, note: str = "") -> dict:
    """[OFFICER ONLY] Approve or reject a claim. Approval releases the item with a pickup code."""
    _officer_ok(officer_token)
    cid = db.parse_code(claim_code, "C")
    with db.connect() as conn:
        claim = conn.execute("SELECT * FROM claims WHERE id = ?", (cid,)).fetchone()
        if not claim:
            return {"error": f"No claim {claim_code}."}
        if claim["status"] != "awaiting_officer":
            return {"error": f"{claim_code} is already {claim['status']}."}
        fcode = db.found_code(claim["found_id"])
        if approve:
            pickup = f"{random.randint(1000, 9999)}"
            conn.execute("UPDATE claims SET status='approved', officer_note=?, pickup_code=?, decided_at=? "
                         "WHERE id=?", (note, pickup, db.now(), cid))
            conn.execute("UPDATE found_items SET status='returned' WHERE id=?", (claim["found_id"],))
            if claim["lost_id"]:
                conn.execute("UPDATE lost_items SET status='recovered' WHERE id=?", (claim["lost_id"],))
            # any other pending claims on the same item are closed
            conn.execute("UPDATE claims SET status='rejected', officer_note='Item released to verified owner', "
                         "decided_at=? WHERE found_id=? AND id != ? AND status='awaiting_officer'",
                         (db.now(), claim["found_id"], cid))
            db.notify(conn, claim["claimant_id"],
                      f"Claim {db.claim_code(cid)} approved. Collect {fcode} from the {SECURITY_OFFICE} "
                      f"with your college ID. Pickup code: {pickup}.")
        else:
            pickup = ""
            conn.execute("UPDATE claims SET status='rejected', officer_note=?, decided_at=? WHERE id=?",
                         (note, db.now(), cid))
            still = conn.execute("SELECT COUNT(*) FROM claims WHERE found_id=? AND status='awaiting_officer'",
                                 (claim["found_id"],)).fetchone()[0]
            if not still:
                conn.execute("UPDATE found_items SET status='in_office' WHERE id=?", (claim["found_id"],))
            db.notify(conn, claim["claimant_id"],
                      f"Claim {db.claim_code(cid)} for {fcode} was not approved. {note}".strip())
        db.log(conn, "officer_decide_claim", "officer",
               f"{db.claim_code(cid)} {'approved' if approve else 'rejected'} {note}")
    return {"claim_code": db.claim_code(cid), "found_code": fcode,
            "status": "approved" if approve else "rejected", "pickup_code": pickup,
            "claimant_id": claim["claimant_id"], "thread_id": claim["thread_id"]}


@mcp.tool()
def officer_list_found(officer_token: str) -> dict:
    """[OFFICER ONLY] Full found-item register including hidden marks."""
    _officer_ok(officer_token)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT f.*, m.marks AS hidden_marks, u.name AS finder_name FROM found_items f "
            "LEFT JOIN found_hidden_marks m ON m.found_id = f.id "
            "LEFT JOIN users u ON u.id = f.finder_id ORDER BY f.id DESC").fetchall()
    return {"items": [{**_public_found(r), "hidden_marks": r["hidden_marks"] or "",
                       "finder": r["finder_name"]} for r in rows]}


@mcp.tool()
def officer_list_lost(officer_token: str) -> dict:
    """[OFFICER ONLY] All lost reports including owners' confidential details."""
    _officer_ok(officer_token)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT l.*, s.details AS secret_details, u.name AS owner_name FROM lost_items l "
            "LEFT JOIN lost_secrets s ON s.lost_id = l.id "
            "LEFT JOIN users u ON u.id = l.owner_id ORDER BY l.id DESC").fetchall()
    return {"items": [{**_public_lost(r), "secret_details": r["secret_details"] or "",
                       "owner": r["owner_name"]} for r in rows]}


@mcp.tool()
def officer_add_hidden_marks(officer_token: str, found_code: str, marks: str) -> dict:
    """[OFFICER ONLY] Record identifying marks noticed when an item is handed in."""
    _officer_ok(officer_token)
    fid = db.parse_code(found_code, "F")
    new = _lines(marks)
    with db.connect() as conn:
        row = conn.execute("SELECT marks FROM found_hidden_marks WHERE found_id = ?", (fid,)).fetchone()
        if row:
            conn.execute("UPDATE found_hidden_marks SET marks = ? WHERE found_id = ?",
                         (row["marks"] + "\n" + new, fid))
        else:
            conn.execute("INSERT INTO found_hidden_marks (found_id, marks) VALUES (?, ?)", (fid, new))
        db.log(conn, "officer_add_hidden_marks", "officer", f"{db.found_code(fid)} marks updated")
    return {"found_code": db.found_code(fid), "updated": True}


@mcp.tool()
def officer_audit_log(officer_token: str, limit: int = 50) -> dict:
    """[OFFICER ONLY] Recent tool activity for transparency."""
    _officer_ok(officer_token)
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return {"entries": [dict(r) for r in rows]}


@mcp.tool()
def officer_reset_demo(officer_token: str) -> dict:
    """[OFFICER ONLY] Wipe the database and reload demo data."""
    _officer_ok(officer_token)
    seed.reset_and_seed()
    return {"reset": True}


# --------------------------------------------------------------------------- UI helpers (no secrets)

@mcp.tool()
def list_notifications(actor_id: str = "", mark_read: bool = False) -> dict:
    """Notifications for the current user."""
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 20",
                            (actor_id,)).fetchall()
        if mark_read:
            conn.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (actor_id,))
    return {"notifications": [dict(r) for r in rows]}


@mcp.tool()
def list_users() -> dict:
    """Demo users (no secrets)."""
    with db.connect() as conn:
        rows = conn.execute("SELECT id, name, role FROM users ORDER BY id").fetchall()
    return {"users": [dict(r) for r in rows]}


if __name__ == "__main__":
    mcp.run()  # stdio transport
