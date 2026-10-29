"""Login: hashed passwords, roles, sign-up, and upgrading a database made before login existed."""
import asyncio
import sqlite3

from lostlink import mcp_client, runner
from mcp_server import db, seed


def run(coro):
    return asyncio.run(coro)


def test_student_and_officer_login():
    run(runner.reset_demo())
    ok = run(runner.login("41110001", "karthik123"))
    assert ok["ok"] and ok["user_id"] == "u1" and ok["role"] == "student"
    assert not run(runner.login("41110001", "wrong"))["ok"]
    assert not run(runner.login("99999999", "karthik123"))["ok"]
    officer = run(runner.login("security", "officer123"))
    assert officer["ok"] and officer["role"] == "officer"


def test_passwords_are_hashed_not_stored():
    run(runner.reset_demo())
    with db.connect() as conn:
        rows = conn.execute("SELECT reg_no, password_hash, salt FROM users").fetchall()
    for r in rows:
        assert r["password_hash"] and r["salt"]
        assert "123" not in r["password_hash"]


def test_sign_up_then_login_and_duplicates_refused():
    run(runner.reset_demo())
    res = run(runner.sign_up("44120229", "Test Student", "secret12"))
    assert res["ok"] and res["role"] == "student"
    assert run(runner.login("44120229", "secret12"))["ok"]
    assert not run(runner.sign_up("44120229", "Someone Else", "secret12"))["ok"]
    assert not run(runner.sign_up("12", "Short Number", "secret12"))["ok"]
    assert not run(runner.sign_up("44120230", "Weak Password", "123"))["ok"]


def test_login_tools_are_never_given_to_agents():
    async def go():
        async with mcp_client.open_session() as s:
            return set(await mcp_client.load_agent_tools(s, "u1", "t"))
    names = run(go())
    assert "authenticate" not in names and "register_student" not in names


def test_old_database_without_login_is_upgraded():
    # Simulate a database created by the earlier version (users table had no password columns).
    path = db.db_path()
    with db.connect() as conn:
        for table in seed.RESET_ORDER:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'student')")
    raw.execute("INSERT INTO users VALUES ('u1', 'Karthik S', 'student')")
    raw.commit()
    raw.close()

    seed.ensure_seeded()  # what the server does on start-up
    assert run(runner.login("41110001", "karthik123"))["ok"]
