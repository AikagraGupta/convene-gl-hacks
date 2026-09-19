"""Event-scoped private inputs. Never store these in shared chat history.

SQLite lets the bot and call desk read the same revision without sharing a
Python process. Only structured, explicitly saved requirements leave the DM.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "private_inputs.sqlite3"
REQUIREMENTS = {
    "vegetarian": "Vegetarian meal available",
    "vegan": "Vegan meal available",
    "no_pork": "Meal without pork available",
    "step_free": "Step-free access to the table",
}


@contextmanager
def connect():
    db = sqlite3.connect(DB_PATH, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS plans (
                token TEXT PRIMARY KEY, chat_id INTEGER, title TEXT,
                expires REAL, revision INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS inputs (
                token TEXT, user_id INTEGER, draft TEXT, saved TEXT,
                PRIMARY KEY(token, user_id));
            CREATE TABLE IF NOT EXISTS sessions (user_id INTEGER PRIMARY KEY, token TEXT);
        """)
        db.execute("DELETE FROM inputs WHERE token IN (SELECT token FROM plans WHERE expires < ?)", (time.time(),))
        db.execute("DELETE FROM sessions WHERE token IN (SELECT token FROM plans WHERE expires < ?)", (time.time(),))
        db.execute("DELETE FROM plans WHERE expires < ?", (time.time(),))
        yield db
        db.commit()
    finally:
        db.close()


def create(chat_id: int, title: str) -> str:
    token = secrets.token_urlsafe(20)
    with connect() as db:
        db.execute("INSERT INTO plans VALUES (?, ?, ?, ?, 0)",
                   (token, chat_id, title[:100], time.time() + 86400))
    return token


def retire(token: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM inputs WHERE token=?", (token,))
        db.execute("DELETE FROM sessions WHERE token=?", (token,))
        db.execute("DELETE FROM plans WHERE token=?", (token,))


def plan(token: str | None) -> dict | None:
    if not token:
        return None
    with connect() as db:
        row = db.execute("SELECT * FROM plans WHERE token=? AND expires > ?", (token, time.time())).fetchone()
        return dict(row) if row else None


def join(user_id: int, token: str) -> None:
    if not plan(token):
        raise ValueError("This plan has expired. Open a new link from your group.")
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (user_id, token))


def session(user_id: int) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT token FROM sessions WHERE user_id=?", (user_id,)).fetchone()
    return plan(row["token"]) if row else None


def draft(token: str, user_id: int) -> dict:
    with connect() as db:
        row = db.execute("SELECT draft FROM inputs WHERE token=? AND user_id=?", (token, user_id)).fetchone()
    return json.loads(row["draft"]) if row else {}


def edit(token: str, user_id: int, patch: dict) -> dict:
    value = draft(token, user_id)
    value.update(patch)
    with connect() as db:
        db.execute("INSERT INTO inputs VALUES (?, ?, ?, NULL) ON CONFLICT(token,user_id) DO UPDATE SET draft=excluded.draft",
                   (token, user_id, json.dumps(value)))
    return value


def save(token: str, user_id: int) -> None:
    with connect() as db:
        db.execute("UPDATE inputs SET saved=draft WHERE token=? AND user_id=?", (token, user_id))
        db.execute("UPDATE plans SET revision=revision+1 WHERE token=?", (token,))


def forget(token: str, user_id: int) -> None:
    with connect() as db:
        db.execute("DELETE FROM inputs WHERE token=? AND user_id=?", (token, user_id))
        db.execute("UPDATE plans SET revision=revision+1 WHERE token=?", (token,))


def snapshot(token: str | None) -> dict:
    if not token:
        return {"revision": 0, "inputs": []}
    with connect() as db:
        row = db.execute("SELECT revision FROM plans WHERE token=? AND expires > ?", (token, time.time())).fetchone()
        if not row:
            raise ValueError("Private input link expired. Start a new /private plan before approving.")
        values = db.execute("SELECT saved FROM inputs WHERE token=? AND saved IS NOT NULL", (token,)).fetchall()
        # No user identities or raw messages cross into the booking pipeline.
        return {"revision": row["revision"], "inputs": [json.loads(v["saved"]) for v in values]}


def current(pending: dict) -> bool:
    token = pending.get("private_plan")
    if not token:
        return True
    try:
        return snapshot(token)["revision"] == pending.get("private_revision")
    except ValueError:
        return False
