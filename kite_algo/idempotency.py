"""Durable idempotency cache for write commands.

Kite Connect has **no server-side idempotency**. If an agent invokes
`place --tag ABC`, the process crashes after the order lands at Kite but
before the CLI writes the response, a naive retry produces a double-fill.
The in-memory dedup in `IdempotentOrderPlacer` covers the single-process
case (orderbook polling); this module covers the **cross-process / crash
recovery** case.

Model:

- Every write command can accept `--idempotency-key KEY`. The agent
  supplies the same key on retry.
- On first invocation: `INSERT OR IGNORE` a row into `writes` table with
  the command name, request args (JSON), and a `first_seen_at` timestamp.
  Return the row's `key` so subsequent code paths (e.g. tag generation)
  can derive a deterministic tag.
- On completion: `UPDATE` the same row with `result_json`, `completed_at`,
  and `exit_code`. Future invocations with the same key short-circuit with
  the stored result and `meta.replayed=true`.
- On crash between start and completion: the row has no `completed_at`.
  Retry with same key sees "started but not completed" and can either
  replay from scratch (safe) or inspect the live broker state first.

Schema:

    CREATE TABLE writes (
      key                 TEXT PRIMARY KEY,
      cmd                 TEXT NOT NULL,
      request_json        TEXT NOT NULL,
      first_seen_at_ms    INTEGER NOT NULL,
      result_json         TEXT,
      completed_at_ms     INTEGER,
      exit_code           INTEGER,
      kite_order_id       TEXT,
      tag                 TEXT,
      request_id          TEXT
    );
    CREATE INDEX idx_writes_cmd ON writes(cmd);
    CREATE INDEX idx_writes_completed ON writes(completed_at_ms);

Storage location: `data/idempotency.sqlite` by default (override via
`KITE_IDEMPOTENCY_PATH` env). WAL mode for concurrent-reader safety.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

DEFAULT_PATH = Path("data/idempotency.sqlite")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS writes (
  key                 TEXT PRIMARY KEY,
  cmd                 TEXT NOT NULL,
  request_json        TEXT NOT NULL,
  first_seen_at_ms    INTEGER NOT NULL,
  result_json         TEXT,
  completed_at_ms     INTEGER,
  exit_code           INTEGER,
  kite_order_id       TEXT,
  tag                 TEXT,
  request_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_writes_cmd ON writes(cmd);
CREATE INDEX IF NOT EXISTS idx_writes_completed ON writes(completed_at_ms);
"""


# -----------------------------------------------------------------------------
# Records
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class WriteRecord:
    """A row in `writes`. `None` for write-not-yet-completed fields."""
    key: str
    cmd: str
    request_json: str
    first_seen_at_ms: int
    result_json: str | None = None
    completed_at_ms: int | None = None
    exit_code: int | None = None
    kite_order_id: str | None = None
    tag: str | None = None
    request_id: str | None = None

    @property
    def completed(self) -> bool:
        return self.completed_at_ms is not None

    @property
    def result(self) -> Any:
        if self.result_json is None:
            return None
        try:
            return json.loads(self.result_json)
        except json.JSONDecodeError:
            return self.result_json


# -----------------------------------------------------------------------------
# Store
# -----------------------------------------------------------------------------

class IdempotencyStore:
    """Thread-safe SQLite-backed idempotency cache.

    Usage:

        store = IdempotencyStore()
        existing = store.lookup(key)
        if existing is not None and existing.completed:
            # Short-circuit replay.
            return existing.result, existing.exit_code

        store.record_attempt(key=..., cmd=..., request=..., request_id=...)
        try:
            result = do_the_thing()
            store.record_completion(key=..., result=result, exit_code=0, ...)
        except Exception:
            # Row stays in "started" state; next retry sees it and can
            # reconcile before deciding.
            raise
    """

    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path is not None else DEFAULT_PATH
        self._lock = threading.Lock()
        self._ensure_parent()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _ensure_parent(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            try:
                os.chmod(self._path.parent, 0o700)
            except OSError:
                pass

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Per-call connection with WAL and normal synchronous. Thread-safe
        because each `with` creates its own connection; SQLite handles the
        write-ahead log locking.
        """
        with self._lock:
            conn = sqlite3.connect(str(self._path), timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=5000;")  # 5s wait on lock
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup(self, key: str) -> WriteRecord | None:
        """Return the stored record for `key`, or None if unseen.

        The returned record may be "started but not completed" — caller
        should check `.completed` before replaying the result.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT key, cmd, request_json, first_seen_at_ms, result_json, "
                "completed_at_ms, exit_code, kite_order_id, tag, request_id "
                "FROM writes WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            return WriteRecord(
                key=row[0], cmd=row[1], request_json=row[2],
                first_seen_at_ms=int(row[3]),
                result_json=row[4],
                completed_at_ms=int(row[5]) if row[5] is not None else None,
                exit_code=int(row[6]) if row[6] is not None else None,
                kite_order_id=row[7],
                tag=row[8],
                request_id=row[9],
            )

    def record_attempt(
        self,
        *,
        key: str,
        cmd: str,
        request: Any,
        request_id: str | None = None,
        tag: str | None = None,
    ) -> bool:
        """Register an in-flight attempt. Returns True if this is a new row,
        False if an entry already existed (idempotent insert)."""
        now_ms = int(time.time() * 1000)
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO writes "
                "(key, cmd, request_json, first_seen_at_ms, request_id, tag) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, cmd, json.dumps(request, default=str), now_ms,
                 request_id, tag),
            )
            return cur.rowcount == 1

    def record_completion(
        self,
        *,
        key: str,
        result: Any,
        exit_code: int,
        kite_order_id: str | None = None,
    ) -> None:
        """Mark the attempt complete. Idempotent — safe to call twice."""
        now_ms = int(time.time() * 1000)
        with self._conn() as c:
            c.execute(
                "UPDATE writes SET result_json=?, completed_at_ms=?, "
                "exit_code=?, kite_order_id=COALESCE(?, kite_order_id) "
                "WHERE key=?",
                (json.dumps(result, default=str), now_ms, exit_code,
                 kite_order_id, key),
            )

    def purge_older_than(self, cutoff_ms: int) -> int:
        """Delete completed rows whose `first_seen_at_ms` is before `cutoff_ms`.
        Returns the number of rows deleted. Incomplete rows are NEVER purged —
        they represent potential ghost orders needing reconciliation.
        """
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM writes WHERE completed_at_ms IS NOT NULL AND "
                "first_seen_at_ms < ?",
                (cutoff_ms,),
            )
            return int(cur.rowcount)


# -----------------------------------------------------------------------------
# Key derivation helpers
# -----------------------------------------------------------------------------

def derive_tag_from_key(
    key: str,
    *,
    prefix: str = "KA",
    length: int = 18,
) -> str:
    """Deterministically derive a Kite `tag` from an idempotency key.

    Kite tags are ≤20 chars alphanumeric. We use BLAKE2b for a
    collision-resistant derivation that the same key always maps to. On
    retry with the same key, the same tag goes out — so the orderbook
    polling in `IdempotentOrderPlacer` correctly finds the prior attempt.

    Default length 18 = 2-char prefix + 16 hex chars (within the 20-char
    cap, with room for a future prefix extension).
    """
    body_len = max(1, min(length - len(prefix), 18))
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=16).hexdigest().upper()
    tag = f"{prefix}{digest[:body_len]}"
    assert len(tag) <= 20 and tag.isalnum(), f"bad tag derivation: {tag!r}"
    return tag
