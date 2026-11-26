"""One-week, read-only documentation usability pilot. No upstream code execution."""

import argparse
import base64
import datetime as dt
import difflib
import hashlib
import json
import os
import posixpath
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .secrets import secret

import httpx

REPOS = (
    "hyperliquid-dex/hyperliquid-python-sdk",
    "encode/httpx",
    "pallets/click",
    "pallets/flask",
    "pydantic/pydantic",
)
# Restrict detection to simple, single-line Markdown links outside code examples.
LINK = re.compile(r"(?<!!)\[[^\[\]]+\]\(([^()<>\s]+)\)")


def findings(repo, sha, source, text, paths):
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or "`" in line or "<" in line or line.startswith(("    ", "\t")):
            continue
        for match in LINK.finditer(line):
            url = urlsplit(match[1])
            path = unquote(url.path)
            if (
                url.scheme
                or url.netloc
                or url.query
                or not path
                or path.startswith(("/", "~", "$"))
            ):
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
            if (
                target in paths
                or target.startswith("../")
                or not target.lower().endswith(
                    (".md", ".rst", ".txt", ".py", ".toml", ".yml", ".yaml")
                )
            ):
                continue
            candidates = [p for p in paths if p.casefold() == target.casefold()]
            fix = candidates[0] if len(candidates) == 1 else None
            title = f"Broken relative link to {path} in {source}"
            evidence = f"https://github.com/{repo}/blob/{sha}/{source}#L{number}"
            issue = f"{title}\n\nThe link on [line {number}]({evidence}) resolves to `{target}`, which is absent from the Git tree at `{sha}`. Please check the intended destination.\n"
            pr = patch = None
            if fix:
                replacement = posixpath.relpath(
                    fix, posixpath.dirname(source) or "."
                ) + ("#" + url.fragment if url.fragment else "")
                lines = text.splitlines(keepends=True)
                edited = lines.copy()
                edited[number - 1] = (
                    lines[number - 1][: match.start(1)]
                    + replacement
                    + lines[number - 1][match.end(1) :]
                )
                patch = "".join(
                    difflib.unified_diff(
                        lines, edited, fromfile="a/" + source, tofile="b/" + source
                    )
                )
                pr = f"Correct link casing in {source}\n\nPoint the link to `{fix}`, the unique case-insensitive match at `{sha}`. Verified target existence in the Git tree; documentation build not run."
            key = hashlib.sha256(
                json.dumps([repo, source, match[1]]).encode()
            ).hexdigest()[:20]
            yield key, title, issue, pr, patch


def scan(client, repo, sha):
    prefix = f"/repos/{repo}/git"
    commit = client.get(prefix + "/commits/" + sha).raise_for_status().json()
    tree = (
        client.get(prefix + "/trees/" + commit["tree"]["sha"], params={"recursive": 1})
        .raise_for_status()
        .json()
    )
    if tree.get("truncated"):
        raise ValueError("Incomplete Git tree; refusing to infer missing targets")
    paths = {item["path"] for item in tree["tree"]}
    result = {}
    checked = 0
    docs = sorted(
        (
            i
            for i in tree["tree"]
            if i["type"] == "blob"
            and i["path"].lower().endswith(".md")
            and i.get("size", 0) < 100000
        ),
        key=lambda i: (
            i["path"].lower() != "readme.md",
            i["path"].count("/"),
            i["path"],
        ),
    )
    for item in docs[:3]:
        blob = client.get(prefix + "/blobs/" + item["sha"]).raise_for_status().json()
        text = base64.b64decode(blob["content"]).decode("utf-8-sig")
        checked += 1
        for key, *data in findings(repo, sha, item["path"], text, paths):
            result.setdefault(key, data)
    return checked, result


def upstream_duplicate(client, repo, title):
    target, source = title.removeprefix("Broken relative link to ").rsplit(" in ", 1)
    result = (
        client.get(
            "/search/issues",
            params={"q": f'repo:{repo} in:title,body "{target}"', "per_page": 100},
        )
        .raise_for_status()
        .json()
    )
    if result["incomplete_results"] or result["total_count"] > len(result["items"]):
        raise ValueError("Incomplete upstream duplicate check")
    for item in result["items"]:
        text = (item["title"] + "\n" + (item.get("body") or "")).casefold()
        if target.casefold() in text and source.casefold() in text and "link" in text:
            return True
    return False


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--home", type=Path, default=Path("pilot-data"))
    subs = p.add_subparsers(dest="cmd", required=True)
    for cmd in ("tick", "queue", "stats", "review"):
        subs.add_parser(cmd)
    show = subs.add_parser("show")
    show.add_argument("id")
    judge = subs.add_parser("judge")
    judge.add_argument("id")
    judge.add_argument("decision", choices=["worth", "reject"])
    judge.add_argument("--reason", required=True)
    judge.add_argument("--sha", required=True)
    a = p.parse_args()
    a.home.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(a.home / "pilot.sqlite3", timeout=60)) as db:
        run_command(a, db)


def run_command(a, db):
    db.row_factory = sqlite3.Row
    db.executescript("""CREATE TABLE IF NOT EXISTS settings(start TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS scans(repo TEXT,sha TEXT,day TEXT,docs INTEGER,PRIMARY KEY(repo,sha));
    CREATE TABLE IF NOT EXISTS queue(id TEXT PRIMARY KEY,repo TEXT,sha TEXT,title TEXT,issue TEXT,pr TEXT,patch TEXT,decision TEXT,reason TEXT,current INTEGER);
    CREATE TABLE IF NOT EXISTS latest(repo TEXT PRIMARY KEY,sha TEXT);
    CREATE TABLE IF NOT EXISTS cooldown(until_epoch INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS judgments(id TEXT,sha TEXT,decision TEXT,reason TEXT,PRIMARY KEY(id,sha));""")
    if db.execute("PRAGMA user_version").fetchone()[0] == 0:
        with db:
            db.execute(
                "INSERT OR IGNORE INTO judgments SELECT id,sha,decision,reason FROM queue WHERE decision IS NOT NULL"
            )
            db.execute("DELETE FROM latest")  # recheck pre-search drafts
            db.execute("UPDATE queue SET current=0")
            db.execute("PRAGMA user_version=1")
    now = dt.datetime.now(dt.timezone.utc)
    day = now.date().isoformat()
    if a.cmd == "tick":
        with db:
            db.execute(
                "INSERT INTO settings SELECT ? WHERE NOT EXISTS(SELECT 1 FROM settings)",
                (now.isoformat(),),
            )
        if now >= dt.datetime.fromisoformat(
            db.execute("SELECT start FROM settings").fetchone()[0]
        ) + dt.timedelta(days=7):
            print("Pilot completed; polling stopped.")
            return
        pause = db.execute("SELECT max(until_epoch) FROM cooldown").fetchone()[0]
        if pause and pause > now.timestamp():
            print(
                "GitHub polling deferred until",
                dt.datetime.fromtimestamp(pause, dt.timezone.utc).isoformat(),
            )
            return
        token = secret("GITHUB_READ_TOKEN")
        headers = {"Authorization": "Bearer " + token} if token else {}
        failures = 0
        with httpx.Client(
            base_url="https://api.github.com", headers=headers, timeout=25
        ) as client:
            for repo in REPOS:
                try:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        sha = (
                            client.get(
                                f"/repos/{repo}/commits/HEAD",
                                headers={"Accept": "application/vnd.github.sha"},
                            )
                            .raise_for_status()
                            .text.strip()
                        )
                        if not re.fullmatch("[0-9a-f]{40}", sha):
                            raise ValueError("Invalid commit SHA")
                        latest = db.execute(
                            "SELECT sha FROM latest WHERE repo=?", (repo,)
                        ).fetchone()
                        if latest and latest[0] == sha:
                            print(repo, sha[:10], "unchanged")
                            continue
                        count, rows = scan(client, repo, sha)
                        db.execute("UPDATE queue SET current=0 WHERE repo=?", (repo,))
                        for key, data in rows.items():
                            if upstream_duplicate(client, repo, data[0]):
                                continue
                            db.execute(
                                """INSERT INTO queue VALUES(?,?,?,?,?,?,?,NULL,NULL,1) ON CONFLICT(id) DO UPDATE SET
                            decision=CASE WHEN queue.sha=excluded.sha AND queue.issue IS excluded.issue AND queue.pr IS excluded.pr AND queue.patch IS excluded.patch THEN queue.decision END,
                            reason=CASE WHEN queue.sha=excluded.sha AND queue.issue IS excluded.issue AND queue.pr IS excluded.pr AND queue.patch IS excluded.patch THEN queue.reason END,
                            sha=excluded.sha,title=excluded.title,issue=excluded.issue,pr=excluded.pr,patch=excluded.patch,current=1""",
                                (key, repo, sha, *data),
                            )
                        db.execute(
                            "INSERT OR REPLACE INTO scans VALUES(?,?,?,?)",
                            (repo, sha, day, count),
                        )
                        db.execute(
                            "INSERT OR REPLACE INTO latest VALUES(?,?)", (repo, sha)
                        )
                        print(repo, sha[:10], f"{count} docs, {len(rows)} candidates")
                except (httpx.HTTPError, ValueError) as exc:
                    print(repo, "deferred:", str(exc))
                    failures += 1
                    if isinstance(
                        exc, httpx.HTTPStatusError
                    ) and exc.response.status_code in (403, 429):
                        h = exc.response.headers
                        epoch = int(dt.datetime.now(dt.timezone.utc).timestamp())
                        delay = h.get("retry-after", "")
                        reset = h.get("x-ratelimit-reset", "")
                        until = max(
                            epoch + (int(delay) if delay.isdigit() else 60),
                            int(reset) if reset.isdigit() else 0,
                        )
                        with db:
                            db.execute("DELETE FROM cooldown")
                            db.execute("INSERT INTO cooldown VALUES(?)", (until,))
                        break
        if failures:
            raise SystemExit(1)
    elif a.cmd == "stats":
        judged, worth = db.execute(
            "SELECT count(*),count(CASE WHEN decision='worth' THEN 1 END) FROM judgments"
        ).fetchone()
        print(
            json.dumps(
                {
                    "daily_scans": [
                        dict(r)
                        for r in db.execute(
                            "SELECT day,count(*) AS repository_versions,sum(docs) AS docs FROM scans GROUP BY day"
                        )
                    ],
                    "model_calls": 0,
                    "model_cost_usd_per_day": 0,
                    "judged": judged,
                    "worth": worth,
                    "worth_ratio": worth / judged if judged else None,
                },
                indent=2,
            )
        )
    elif a.cmd == "judge":
        with db:
            if not db.execute(
                "UPDATE queue SET decision=?,reason=? WHERE id=? AND sha=? AND current=1",
                (a.decision, a.reason, a.id, a.sha),
            ).rowcount:
                raise SystemExit("Unknown or stale item")
            db.execute(
                "INSERT OR REPLACE INTO judgments VALUES(?,?,?,?)",
                (a.id, a.sha, a.decision, a.reason),
            )
        print("Decision recorded locally.")
    elif a.cmd == "review":
        rows = db.execute(
            "SELECT * FROM queue WHERE current=1 AND decision IS NULL ORDER BY (patch IS NOT NULL) DESC,repo,title"
        ).fetchall()
        print(f"待验收：{len(rows)} 条。")
        for row in rows:
            print(
                f"\n{row['repo']} · {row['id']} · {row['sha']}\n{row['title']}\n证据：{row['issue'].split('](')[1].split(')')[0]}\n草稿：{'Issue + PR（修正路径大小写）' if row['patch'] else 'Issue（目标待确认）'}；已核对 Git 树，未运行文档构建。"
            )
    else:
        rows = (
            db.execute(
                "SELECT id,repo,sha,title,decision FROM queue WHERE current=1 AND decision IS NULL ORDER BY repo,title"
            )
            if a.cmd == "queue"
            else db.execute("SELECT * FROM queue WHERE id=?", (a.id,))
        )
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
