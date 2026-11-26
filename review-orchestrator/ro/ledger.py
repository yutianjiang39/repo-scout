"""Append-only state log + immutable per-version file store (SQLite)."""

import json
from pathlib import Path
import sqlite3
from contextlib import contextmanager

from .util import Refused


class Ledger:
    def __init__(self, conn):
        self.conn = conn
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY, version INTEGER,"
            " action TEXT, state TEXT, at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS files(version INTEGER, name TEXT, data BLOB,"
            " PRIMARY KEY(version,name))"
        )
        row = conn.execute("SELECT version,state FROM ledger ORDER BY id DESC LIMIT 1").fetchone()
        self.v, self.s = (row[0], json.loads(row[1])) if row else (0, {})
        self.empty = row is None

    def put(self, name, data, version=None):
        try:
            self.conn.execute("INSERT INTO files VALUES (?,?,?)", (version or self.v, name, data))
        except sqlite3.IntegrityError:
            raise Refused(f"Sealed file already exists: {name}")

    def get(self, name, version=None):
        row = self.conn.execute(
            "SELECT data FROM files WHERE version=? AND name=?", (version or self.v, name)
        ).fetchone()
        return row[0] if row else None

    def names(self, version=None):
        return {n for (n,) in self.conn.execute("SELECT name FROM files WHERE version=?", (version or self.v,))}

    def files(self, version=None):
        return self.conn.execute("SELECT name,data FROM files WHERE version=?", (version or self.v,)).fetchall()

    def commit(self, action):
        self.conn.execute(
            "INSERT INTO ledger(version,action,state) VALUES (?,?,?)",
            (self.v, action, json.dumps(self.s, ensure_ascii=False)),
        )

    def history(self):
        return self.conn.execute("SELECT version,at,action FROM ledger ORDER BY id").fetchall()

    def all_states(self):
        return [json.loads(x) for (x,) in self.conn.execute("SELECT state FROM ledger")]


@contextmanager
def opened(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            yield Ledger(conn)
    finally:
        conn.close()
