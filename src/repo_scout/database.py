from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .common import canonical, digest, now


class ConflictError(ValueError):
    pass


class Store:
    """Versioned entities, append-only events, cache, approvals and durable outbox.

    Domain records are schema-validated at service boundaries; JSON keeps adapters
    extensible. Every write and event is atomic. expected_version implements CAS.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=20, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=20000;
            CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
            INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
            CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL,
                status TEXT, data TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(kind,id));
            CREATE INDEX IF NOT EXISTS records_status ON records(kind,status);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, entity_id TEXT NOT NULL,
                old_status TEXT, new_status TEXT, revision INTEGER NOT NULL,
                reason TEXT NOT NULL, actor TEXT NOT NULL, evidence TEXT NOT NULL,
                payload_hash TEXT NOT NULL, at TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT,'event history is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT,'event history is append-only'); END;
            CREATE TABLE IF NOT EXISTS cache (
                namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                updated_at TEXT NOT NULL, PRIMARY KEY(namespace,key));
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, entity_id TEXT NOT NULL,
                digest TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL, consumed_at TEXT, reason TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS approval_lookup ON approvals(kind,entity_id,digest);
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL,
                payload TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
        """)
        with self.atomic():
            version = self.connection.execute(
                "SELECT version FROM schema_version"
            ).fetchone()[0]
            if version not in (1, 2):
                raise ValueError("Unsupported database version")
            if version == 1:
                self.connection.execute(
                    "CREATE TABLE review_rounds(seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, subject TEXT NOT NULL, kind TEXT NOT NULL, artifact TEXT NOT NULL, policy TEXT NOT NULL)"
                )
                self.connection.execute(
                    "CREATE INDEX current_round ON review_rounds(subject,kind,seq DESC)"
                )
                self.connection.execute(
                    "CREATE TABLE review_receipts(round_id TEXT NOT NULL REFERENCES review_rounds(id), role TEXT NOT NULL, phase TEXT NOT NULL, session TEXT NOT NULL UNIQUE, envelope TEXT NOT NULL, PRIMARY KEY(round_id,role,phase))"
                )
                for table in ("review_rounds", "review_receipts"):
                    for action in ("UPDATE", "DELETE"):
                        self.connection.execute(
                            f"CREATE TRIGGER {table}_no_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable review history'); END"
                        )
                self.connection.execute("UPDATE schema_version SET version=2")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @contextmanager
    def atomic(self) -> Iterator[None]:
        nested = self.connection.in_transaction
        name = "s" + uuid.uuid4().hex
        self.connection.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.execute(f"ROLLBACK TO {name}" if nested else "ROLLBACK")
            if nested:
                self.connection.execute(f"RELEASE {name}")
            raise
        else:
            self.connection.execute(f"RELEASE {name}" if nested else "COMMIT")

    def get(self, kind: str, key: str) -> dict | None:
        row = self.connection.execute(
            "SELECT data,version FROM records WHERE kind=? AND id=?", (kind, key)
        ).fetchone()
        if row is None:
            return None
        return {**json.loads(row["data"]), "_version": row["version"]}

    def require(self, kind: str, key: str) -> dict:
        value = self.get(kind, key)
        if value is None:
            raise ValueError(f"Unknown {kind}: {key}")
        return value

    def list(self, kind: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT data,version FROM records WHERE kind=? ORDER BY id", (kind,)
        ).fetchall()
        return [{**json.loads(r["data"]), "_version": r["version"]} for r in rows]

    def put(
        self,
        kind: str,
        key: str,
        data: dict,
        *,
        reason: str = "",
        actor: str = "local",
        evidence: Any = None,
        expected_version: int | None = None,
    ) -> dict:
        clean = {k: v for k, v in data.items() if k != "_version"}
        encoded = canonical(clean)
        with self.atomic():
            old = self.get(kind, key)
            version = old["_version"] if old else 0
            if expected_version is not None and version != expected_version:
                raise ConflictError(
                    f"Concurrent update of {kind}/{key}: expected {expected_version}, found {version}"
                )
            if (
                old
                and canonical({k: v for k, v in old.items() if k != "_version"})
                == encoded
            ):
                return old
            status = clean.get("status", clean.get("state"))
            previous = old.get("status", old.get("state")) if old else None
            timestamp = now()
            self.connection.execute(
                """INSERT INTO records VALUES(?,?,?,?,?,?)
                ON CONFLICT(kind,id) DO UPDATE SET version=excluded.version,status=excluded.status,
                data=excluded.data,updated_at=excluded.updated_at""",
                (kind, key, version + 1, status, encoded, timestamp),
            )
            self.connection.execute(
                """INSERT INTO events(kind,entity_id,old_status,new_status,revision,reason,actor,evidence,payload_hash,at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    kind,
                    key,
                    previous,
                    status,
                    version + 1,
                    reason,
                    actor,
                    canonical(evidence or []),
                    digest(clean),
                    timestamp,
                ),
            )
        return {**clean, "_version": version + 1}

    def events(self, kind: str | None = None, key: str | None = None) -> list[dict]:
        clauses, params = [], []
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        if key is not None:
            clauses.append("entity_id=?")
            params.append(key)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT * FROM events" + where + " ORDER BY seq", params
        )
        return [{**dict(row), "evidence": json.loads(row["evidence"])} for row in rows]

    def cache_get(self, namespace: str, key: str) -> dict | None:
        row = self.connection.execute(
            "SELECT value FROM cache WHERE namespace=? AND key=?", (namespace, key)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def cache_set(self, namespace: str, key: str, value: dict) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO cache VALUES(?,?,?,?)",
            (namespace, key, canonical(value), now()),
        )

    def approve(
        self,
        kind: str,
        key: str,
        payload_digest: str,
        *,
        actor: str,
        reason: str,
        hours: int = 48,
    ) -> dict:
        if not actor.strip() or not reason.strip() or not 1 <= hours <= 168:
            raise ValueError(
                "Approval requires an explicit actor, reason and expiry of 1..168 hours"
            )
        record = {
            "id": "APP-" + uuid.uuid4().hex[:16],
            "kind": kind,
            "entity_id": key,
            "digest": payload_digest,
            "actor": actor,
            "created_at": now(),
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(hours=hours)
            ).isoformat(),
            "consumed_at": None,
            "reason": reason,
        }
        with self.atomic():
            existing = self.connection.execute(
                """SELECT * FROM approvals WHERE kind=? AND entity_id=? AND digest=?
                AND consumed_at IS NULL AND expires_at>? ORDER BY created_at DESC LIMIT 1""",
                (kind, key, payload_digest, now()),
            ).fetchone()
            if existing:
                return dict(existing)
            self.connection.execute(
                "INSERT INTO approvals VALUES(:id,:kind,:entity_id,:digest,:actor,:created_at,:expires_at,:consumed_at,:reason)",
                record,
            )
            self.put("approval", record["id"], record, reason=reason, actor=actor)
        return record

    def check_approval(
        self, kind: str, key: str, payload_digest: str, *, consume: bool = False
    ) -> dict:
        with self.atomic():
            row = self.connection.execute(
                """SELECT * FROM approvals WHERE kind=? AND entity_id=? AND digest=?
                AND consumed_at IS NULL AND expires_at>? ORDER BY created_at DESC LIMIT 1""",
                (kind, key, payload_digest, now()),
            ).fetchone()
            if row is None:
                raise ValueError(
                    "Missing, expired, already consumed or stale approval. Review the exact current payload and approve it."
                )
            result = dict(row)
            if consume:
                result["consumed_at"] = now()
                # Older databases can contain duplicate confirmations. None may
                # survive an attempted send as authorization for another try.
                for sibling in self.connection.execute(
                    """SELECT * FROM approvals WHERE kind=? AND entity_id=?
                        AND digest=? AND consumed_at IS NULL""",
                    (kind, key, payload_digest),
                ).fetchall():
                    self.connection.execute(
                        "UPDATE approvals SET consumed_at=? WHERE id=?",
                        (result["consumed_at"], sibling["id"]),
                    )
                    self.put(
                        "approval",
                        sibling["id"],
                        {**dict(sibling), "consumed_at": result["consumed_at"]},
                        reason="approval consumed by exact authorized action",
                        actor="workflow",
                    )
            return result

    def operation(self, key: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM operations WHERE id=?", (key,)
        ).fetchone()
        if not row:
            return None
        return {
            **dict(row),
            "payload": json.loads(row["payload"]),
            "result": json.loads(row["result"]) if row["result"] else None,
        }

    def begin_operation(self, key: str, payload: dict) -> dict:
        with self.atomic():
            old = self.operation(key)
            if old:
                if old["digest"] != digest(payload):
                    raise ConflictError("Operation ID reused with different content")
                return old
            self.connection.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,?,?)",
                (
                    key,
                    digest(payload),
                    "PREPARED",
                    canonical(payload),
                    None,
                    now(),
                    now(),
                ),
            )
            return self.operation(key)  # type: ignore[return-value]

    def finish_operation(
        self, key: str, status: str, result: dict | None = None
    ) -> None:
        if status not in {"PREPARED", "SENDING", "SUCCEEDED", "UNKNOWN", "FAILED"}:
            raise ValueError("Invalid outbox state")
        self.connection.execute(
            "UPDATE operations SET status=?,result=?,updated_at=? WHERE id=?",
            (status, canonical(result) if result else None, now(), key),
        )
