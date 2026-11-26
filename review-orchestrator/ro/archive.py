"""Records archive: one SQLite index over every task folder (old manual runs and new ledgers).

Files stay where they are; the database holds facts about them (task, kind, SHA-256, size)
plus an append-only event log. Re-importing is idempotent: unchanged files are left alone,
changed files get a new hash row, so nothing is overwritten silently.

CLI session material (OpenCode/Claude transcripts, prompts, command captures) is counted but
never read or hashed: only the file count is recorded.

  python -m ro.archive --db <records.sqlite> import <repos-dir>
  python -m ro.archive --db <records.sqlite> list [repo]
  python -m ro.archive --db <records.sqlite> verify
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY, repo TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL,
  source TEXT NOT NULL, phase TEXT, patch TEXT, files INTEGER, bytes INTEGER,
  session_files INTEGER, imported_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(repo, name));
CREATE TABLE IF NOT EXISTS artifacts(
  id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id), relpath TEXT NOT NULL,
  kind TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
  recorded_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(task_id, relpath, sha256));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, at TEXT DEFAULT CURRENT_TIMESTAMP, action TEXT NOT NULL, detail TEXT);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""

SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SESSION_DIR = re.compile(r"^(opencode|claude|session|transcripts?)([-_].*)?$", re.I)
SESSION_FILE = re.compile(r"^(cli\.(jsonl|err|out|json)|stdout|stderr|prompt\.txt|.*\.jsonl|[0-9a-f]{32}\.(out|json))$", re.I)
KINDS = [
    ("patch", re.compile(r"\.(diff|patch)$")),
    ("ledger", re.compile(r"^review\.sqlite$")),
    ("report", re.compile(r"(gpt|cc|claude|gemini|deepseek)[-_].*\.(md|json)$|^report\.(md|json)$|objection|verdict")),
    ("receipt", re.compile(r"receipt|exit\.txt$|SHA\d*SUMS|SHANSUMS|manifest", re.I)),
    ("github", re.compile(r"^(issues?|pulls?|pullRequests|pr|timeline)[-_\d].*\.json$|^\d+-(timeline|comments)\.json$|^pr-\d+\.json$")),
    ("deliverable", re.compile(r"^(pr|body|deliverable|draft).*\.md$|packet|scope")),
    ("note", re.compile(r"\.(md|txt)$")),
    ("log", re.compile(r"\.log$")),
    ("archive", re.compile(r"\.(zip|tar|gz|tgz)$")),
    ("data", re.compile(r"\.(json|ya?ml|csv|toml)$")),
]


def kind_of(name):
    return next((k for k, rx in KINDS if rx.search(name)), "other")


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def walk(task_dir):
    """Yield (path, is_session) for a task folder, skipping checkouts and session folders."""
    for p in sorted(task_dir.iterdir()):
        if p.is_dir():
            if p.name in SKIP_DIRS or (p / ".git").exists():
                continue
            if SESSION_DIR.match(p.name):
                yield from ((q, True) for q in p.rglob("*") if q.is_file())
                continue
            yield from walk(p)
        elif p.is_file():
            yield p, bool(SESSION_FILE.match(p.name))


def ledger_facts(db):
    """Phase and patch hash from a new-style task ledger (read-only)."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = conn.execute("SELECT state FROM ledger ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        s = json.loads(row[0]) if row else {}
        return s.get("phase"), s.get("patch")
    except sqlite3.Error:
        return None, None


def task_units(repos):
    """Each repos/<repo>/tasks/<name> folder (or loose file) is one task; repos/<repo>/files too."""
    for repo in sorted(p for p in Path(repos).iterdir() if p.is_dir()):
        tasks = repo / "tasks"
        if tasks.is_dir():
            for t in sorted(tasks.iterdir()):
                yield repo.name, t.name if t.is_dir() else t.stem, t
        for extra in ("files", "archives"):
            if (repo / extra).is_dir():
                yield repo.name, f"_{extra}", repo / extra


def connect(db):
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    return conn


def import_repos(conn, repos):
    added = 0
    for repo, name, path in task_units(repos):
        files = [(path, False)] if path.is_file() else list(walk(path))
        kept = [p for p, s in files if not s]
        phase = patch = None
        ledger = path / "review.sqlite" if path.is_dir() else None
        if ledger and ledger.is_file():
            phase, patch = ledger_facts(ledger)
        conn.execute(
            "INSERT INTO tasks(repo,name,path,source,phase,patch,files,bytes,session_files) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(repo,name) DO UPDATE SET path=excluded.path, source=excluded.source, phase=excluded.phase, patch=excluded.patch,"
            " files=excluded.files, bytes=excluded.bytes, session_files=excluded.session_files",
            (repo, name, str(path.resolve()), "ledger" if phase is not None else "legacy", phase, patch,
             len(kept), sum(p.stat().st_size for p in kept), len(files) - len(kept)))
        tid = conn.execute("SELECT id FROM tasks WHERE repo=? AND name=?", (repo, name)).fetchone()[0]
        base = path.parent if path.is_file() else path
        for p in kept:
            cur = conn.execute("INSERT OR IGNORE INTO artifacts(task_id,relpath,kind,sha256,size) VALUES (?,?,?,?,?)",
                               (tid, p.relative_to(base).as_posix(), kind_of(p.name), sha_file(p), p.stat().st_size))
            added += cur.rowcount
    conn.execute("INSERT INTO events(action,detail) VALUES ('import', ?)",
                 (json.dumps(dict(repos=str(Path(repos).resolve()), new_rows=added)),))
    return added


def latest(conn):
    return conn.execute(
        "SELECT t.path, a.relpath, a.sha256 FROM artifacts a JOIN tasks t ON t.id=a.task_id"
        " WHERE a.id=(SELECT MAX(id) FROM artifacts b WHERE b.task_id=a.task_id AND b.relpath=a.relpath)")


def verify(conn):
    """Files whose current bytes differ from the newest recorded hash, or that disappeared."""
    bad = []
    for root, rel, h in latest(conn):
        p = Path(root) / rel if Path(root).is_dir() else Path(root)
        if not p.is_file():
            bad.append(("missing", str(p)))
        elif sha_file(p) != h:
            bad.append(("changed", str(p)))
    conn.execute("INSERT INTO events(action,detail) VALUES ('verify', ?)", (json.dumps(dict(problems=len(bad))),))
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import").add_argument("repos")
    sub.add_parser("list").add_argument("repo", nargs="?")
    sub.add_parser("verify")
    a = ap.parse_args(argv)
    conn = connect(a.db)
    with conn:
        if a.cmd == "import":
            print(f"new artifact rows: {import_repos(conn, a.repos)}")
        elif a.cmd == "list":
            q = "SELECT repo,name,source,phase,files,session_files FROM tasks" + (" WHERE repo=?" if a.repo else "")
            for row in conn.execute(q + " ORDER BY repo,name", (a.repo,) if a.repo else ()):
                print("  ".join("-" if x is None else str(x) for x in row))
        else:
            bad = verify(conn)
            for k, p in bad:
                print(k, p)
            print(f"{len(bad)} problem(s)")
            return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
