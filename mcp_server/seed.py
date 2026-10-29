"""Demo data so the agent has a realistic campus to work with."""
from __future__ import annotations

import os
from datetime import date, timedelta

from . import db

# (id, name, role, login id, demo password). Passwords are hashed before they are stored.
USERS = [
    ("u1", "Karthik S", "student", "41110001", "karthik123"),
    ("u2", "Aditi R", "student", "41110002", "aditi123"),
    ("u3", "Meera J", "student", "41110003", "meera123"),
    ("u4", "Rahul V", "student", "41110004", "rahul123"),
    ("u5", "Arjun P", "student", "41110005", "arjun123"),
]
OFFICER = ("officer", "Security Office", "officer", "security")


def officer_password() -> str:
    return os.getenv("OFFICER_PASSWORD", "") or "officer123"


def _d(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


# (finder, category, color, brand, location, days_ago, public description, hidden marks)
FOUND = [
    ("u2", "bottle", "blue", "Milton", "Library", 1,
     "Blue steel water bottle left on a reading table",
     "Pikachu sticker near the cap\nsmall dent at the bottom"),
    ("u3", "wallet", "black", "Woodland", "Food Court", 2,
     "Black leather wallet found near the juice counter",
     "college ID card of Rahul V\naround 200 rupees cash\nmetro rail card"),
    ("u5", "keys", "", "", "Parking", 0,
     "Bunch of keys found near the two-wheeler parking",
     "3 keys\nred Eiffel Tower keychain"),
    ("u1", "charger", "white", "Apple", "Lab Block", 3,
     "White laptop charger left in a lab",
     "initials KS written in marker on the adapter"),
    ("u4", "umbrella", "grey", "", "Main Block", 5,
     "Grey foldable umbrella near the staircase",
     "one broken spoke\nblue strap"),
    ("u2", "earphones", "black", "boAt", "Auditorium", 2,
     "Black boAt earbuds in a charging case",
     "scratch on the case lid\nleft ear tip missing"),
    ("u3", "bottle", "black", "", "Sports Ground", 1,
     "Black sipper bottle near the bench",
     "name Arun scratched on the bottom"),
]

# (owner, category, color, brand, location, days_ago, public description, secret details)
LOST = [
    ("u3", "bag", "black", "Puma", "Bus Bay", 1,
     "Black Puma backpack",
     "Casio fx-991 scientific calculator inside\nnotebook with my name Meera on the cover"),
    ("u4", "wallet", "black", "", "Food Court", 2,
     "Black leather wallet",
     "my college ID card\nabout 200 rupees\nmetro card"),
    ("u2", "id_card", "", "", "Library", 3,
     "College ID card on a blue lanyard",
     "name Aditi R, register number 41110002"),
]


RESET_ORDER = ["claims", "notifications", "audit_log", "lost_secrets", "found_hidden_marks",
               "lost_items", "found_items", "users"]  # children before parents (foreign keys)


def reset_and_seed() -> None:
    # Empty the tables instead of deleting the file: Windows will not delete a
    # database file that another process (or this one) still has open.
    db.init_db()
    with db.connect() as conn:
        for table in RESET_ORDER:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence")  # restart codes at L001 / F001 / C001
        for uid, name, role, login, password in USERS + [(*OFFICER, officer_password())]:
            pw_hash, salt = db.hash_password(password)
            conn.execute("INSERT INTO users (id, name, role, reg_no, password_hash, salt) VALUES (?, ?, ?, ?, ?, ?)",
                         (uid, name, role, login, pw_hash, salt))
        for finder, cat, color, brand, loc, ago, desc, marks in FOUND:
            cur = conn.execute(
                "INSERT INTO found_items (finder_id, category, color, brand, location, date_found,"
                " description, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (finder, cat, color, brand, loc, _d(ago), desc, db.now()),
            )
            conn.execute("INSERT INTO found_hidden_marks (found_id, marks) VALUES (?, ?)",
                         (cur.lastrowid, marks))
        for owner, cat, color, brand, loc, ago, desc, secret in LOST:
            cur = conn.execute(
                "INSERT INTO lost_items (owner_id, category, color, brand, location, date_lost,"
                " description, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (owner, cat, color, brand, loc, _d(ago), desc, db.now()),
            )
            conn.execute("INSERT INTO lost_secrets (lost_id, details) VALUES (?, ?)",
                         (cur.lastrowid, secret))
        db.log(conn, "seed", "system", "Demo data loaded")


def ensure_seeded() -> None:
    db.init_db()
    with db.connect() as conn:
        has_logins = conn.execute("SELECT COUNT(*) FROM users WHERE password_hash != ''").fetchone()[0]
    if not has_logins:  # new database, or one created before login existed
        reset_and_seed()
    sync_officer_password()


def sync_officer_password() -> None:
    """Keep the officer account's password in step with OFFICER_PASSWORD in .env."""
    with db.connect() as conn:
        row = conn.execute("SELECT password_hash, salt FROM users WHERE id = ?", (OFFICER[0],)).fetchone()
        if row and not db.check_password(officer_password(), row["password_hash"], row["salt"]):
            pw_hash, salt = db.hash_password(officer_password())
            conn.execute("UPDATE users SET password_hash = ?, salt = ? WHERE id = ?", (pw_hash, salt, OFFICER[0]))


if __name__ == "__main__":
    reset_and_seed()
    print(f"Seeded demo data into {db.db_path()}")
