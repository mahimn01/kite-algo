"""Multi-leg transaction groups.

A common pattern: an agent places two or more related orders (e.g. a bear
put spread's long + short leg, or an iron condor's four legs). If the
process crashes between legs, the agent needs to reconstruct which legs
landed — by group.

Kite has no server-side concept of a group, so we track locally in SQLite.
Every `place --group-id G --leg-name short_put` registers a member;
`group-status` and `group-cancel` iterate the members.

Schema:

    CREATE TABLE groups (
      id              TEXT PRIMARY KEY,    -- ULID
      name            TEXT NOT NULL,
      expected_legs   INTEGER,             -- null = unknown upfront
      created_at_ms   INTEGER NOT NULL,
      closed_at_ms    INTEGER,             -- set on group-close / cancel
      meta_json       TEXT
    );

    CREATE TABLE group_members (
      group_id        TEXT NOT NULL,
      order_id        TEXT NOT NULL,
      leg_name        TEXT,
      tag             TEXT,
      idempotency_key TEXT,
      added_at_ms     INTEGER NOT NULL,
      PRIMARY KEY (group_id, order_id),
      FOREIGN KEY (group_id) REFERENCES groups(id)
    );

Storage: reuses the idempotency SQLite file at data/idempotency.sqlite so
a single data/ audit footprint covers both. WAL-safe for concurrent reads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from kite_algo.envelope import new_request_id


DEFAULT_PATH = Path("data/idempotency.sqlite")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  expected_legs   INTEGER,
  created_at_ms   INTEGER NOT NULL,
  closed_at_ms    INTEGER,
  meta_json       TEXT
);
CREATE TABLE IF NOT EXISTS group_members (
  group_id        TEXT NOT NULL,
  order_id        TEXT NOT NULL,
  leg_name        TEXT,
  tag             TEXT,
  idempotency_key TEXT,
  added_at_ms     INTEGER NOT NULL,
  PRIMARY KEY (group_id, order_id)
);
CREATE INDEX IF NOT EXISTS idx_group_members_group ON group_members(group_id);
"""


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    expected_legs: int | None
    created_at_ms: int
    closed_at_ms: int | None
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GroupMember:
    group_id: str
    order_id: str
    leg_name: str | None
    tag: str | None
    idempotency_key: str | None
    added_at_ms: int


class GroupStore:
    """SQLite-backed group registry (reuses the idempotency DB file)."""

    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path is not None else DEFAULT_PATH
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(str(self._path), timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def start(self, *, name: str, expected_legs: int | None = None,
              meta: dict | None = None) -> Group:
        """Create a new group, return the Group (with a fresh ULID id)."""
        gid = new_request_id()  # ULID-style, sortable
        now_ms = int(time.time() * 1000)
        with self._conn() as c:
            c.execute(
                "INSERT INTO groups (id, name, expected_legs, created_at_ms, meta_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (gid, name, expected_legs, now_ms,
                 json.dumps(meta or {}, default=str)),
            )
        return Group(
            id=gid, name=name, expected_legs=expected_legs,
            created_at_ms=now_ms, closed_at_ms=None, meta=meta or {},
        )

    def close(self, group_id: str) -> bool:
        now_ms = int(time.time() * 1000)
        with self._conn() as c:
            cur = c.execute(
                "UPDATE groups SET closed_at_ms=? WHERE id=? AND closed_at_ms IS NULL",
                (now_ms, group_id),
            )
            return cur.rowcount > 0

    def get(self, group_id: str) -> Group | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT id, name, expected_legs, created_at_ms, closed_at_ms, meta_json "
                "FROM groups WHERE id=?",
                (group_id,),
            ).fetchone()
            if row is None:
                return None
            meta = {}
            if row[5]:
                try:
                    meta = json.loads(row[5])
                except json.JSONDecodeError:
                    pass
            return Group(
                id=row[0], name=row[1], expected_legs=row[2],
                created_at_ms=int(row[3]),
                closed_at_ms=(int(row[4]) if row[4] is not None else None),
                meta=meta,
            )

    def add_member(
        self,
        *,
        group_id: str,
        order_id: str,
        leg_name: str | None = None,
        tag: str | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        now_ms = int(time.time() * 1000)
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO group_members "
                "(group_id, order_id, leg_name, tag, idempotency_key, added_at_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (group_id, order_id, leg_name, tag, idempotency_key, now_ms),
            )
            return cur.rowcount == 1

    def members(self, group_id: str) -> list[GroupMember]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT group_id, order_id, leg_name, tag, idempotency_key, added_at_ms "
                "FROM group_members WHERE group_id=? ORDER BY added_at_ms",
                (group_id,),
            ).fetchall()
        return [
            GroupMember(
                group_id=r[0], order_id=r[1],
                leg_name=r[2], tag=r[3], idempotency_key=r[4],
                added_at_ms=int(r[5]),
            ) for r in rows
        ]

    def list_active(self, *, limit: int = 50) -> list[Group]:
        """Groups not yet marked closed. Most recent first."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, name, expected_legs, created_at_ms, closed_at_ms, meta_json "
                "FROM groups WHERE closed_at_ms IS NULL "
                "ORDER BY created_at_ms DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            Group(
                id=r[0], name=r[1], expected_legs=r[2],
                created_at_ms=int(r[3]),
                closed_at_ms=(int(r[4]) if r[4] is not None else None),
                meta=(json.loads(r[5]) if r[5] else {}),
            )
            for r in rows
        ]
