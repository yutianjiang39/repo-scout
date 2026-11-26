"""Shared, incremental issue/PR index per upstream repository.

One SQLite file per repository (repos/<repo>/index.sqlite). The first sync fetches everything
(all states, with comments); later syncs walk items by updatedAt DESC and stop at the last
sync point, so only changed items are re-fetched. Every topic screen reads this index instead
of crawling the repository again.

  python -m ro.upstream sync  <owner/name> <index.sqlite>
  python -m ro.upstream stats <index.sqlite>
  python -m ro.upstream export <index.sqlite> <out.json> [--state OPEN]
"""

import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  kind TEXT NOT NULL, number INTEGER NOT NULL, state TEXT, title TEXT, updated_at TEXT NOT NULL,
  data TEXT NOT NULL, PRIMARY KEY(kind, number));
CREATE TABLE IF NOT EXISTS syncs(
  id INTEGER PRIMARY KEY, at TEXT DEFAULT CURRENT_TIMESTAMP, kind TEXT, since TEXT, fetched INTEGER,
  total INTEGER, head TEXT);
"""
COMMON = ("number title url state body createdAt updatedAt closedAt author{login} labels(first:20){nodes{name}} "
          "assignees(first:20){nodes{login}} comments(last:100){totalCount nodes{author{login} body createdAt}}")
FIELDS = {"issues": COMMON + " stateReason",
          "pullRequests": COMMON + " isDraft baseRefName headRefName mergedAt"}


def gh(query, **variables):
    cmd = ["gh", "api", "graphql", "-f", "query=" + query]
    for k, v in variables.items():
        if v is not None:
            cmd += ["-F" if isinstance(v, int) else "-f", f"{k}={v}"]
    for attempt in range(4):
        p = subprocess.run(cmd, capture_output=True, timeout=120)
        if p.returncode == 0:
            return json.loads(p.stdout)["data"]
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(p.stderr.decode(errors="replace")[-500:])


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def sync(conn, repo, log=print):
    owner, name = repo.split("/", 1)
    head = gh('query($o:String!,$n:String!){repository(owner:$o,name:$n){defaultBranchRef{target{oid}}}}',
              o=owner, n=name)["repository"]["defaultBranchRef"]["target"]["oid"]
    for kind in ("issues", "pullRequests"):
        since = conn.execute("SELECT MAX(updated_at) FROM items WHERE kind=?", (kind,)).fetchone()[0]
        cursor, fetched, done = None, 0, False
        while not done:
            q = (f'query($o:String!,$n:String!,$c:String){{repository(owner:$o,name:$n){{{kind}(first:50,after:$c,'
                 f'orderBy:{{field:UPDATED_AT,direction:DESC}}){{totalCount pageInfo{{hasNextPage endCursor}} '
                 f'nodes{{{FIELDS[kind]}}}}}}}}}')
            page = gh(q, o=owner, n=name, c=cursor)["repository"][kind]
            for node in page["nodes"]:
                if since and node["updatedAt"] < since:
                    done = True
                    break
                conn.execute("INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?)",
                             (kind, node["number"], node["state"], node["title"], node["updatedAt"],
                              json.dumps(node, ensure_ascii=False)))
                fetched += 1
            conn.commit()
            info = page["pageInfo"]
            done = done or not info["hasNextPage"]
            cursor = info["endCursor"]
        total = conn.execute("SELECT COUNT(*) FROM items WHERE kind=?", (kind,)).fetchone()[0]
        conn.execute("INSERT INTO syncs(kind,since,fetched,total,head) VALUES (?,?,?,?,?)",
                     (kind, since, fetched, total, head))
        conn.commit()
        log(f"{kind}: fetched {fetched} changed since {since or 'beginning'}; index has {total} "
            f"(upstream reports {page['totalCount']})")
    return head


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync")
    s.add_argument("repo")
    s.add_argument("db")
    sub.add_parser("stats").add_argument("db")
    e = sub.add_parser("export")
    e.add_argument("db")
    e.add_argument("out")
    e.add_argument("--state")
    a = ap.parse_args(argv)
    conn = connect(a.db)
    if a.cmd == "sync":
        sync(conn, a.repo)
    elif a.cmd == "stats":
        for row in conn.execute("SELECT kind,state,COUNT(*) FROM items GROUP BY kind,state"):
            print(*row)
        print("last sync:", conn.execute("SELECT at, head FROM syncs ORDER BY id DESC LIMIT 1").fetchone())
    else:
        q, args = "SELECT data FROM items", ()
        if a.state:
            q, args = q + " WHERE state=?", (a.state,)
        rows = [json.loads(d) for (d,) in conn.execute(q + " ORDER BY kind, number", args)]
        Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{len(rows)} items -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
