"""SQLite audit store for the engine / OMS / risk layers.

Mirrors `trading_algo/persistence.py` in structure (runs + decisions +
orders + order_status_events + errors + actions) but extends the schemas
to capture Kite-specific fields (tag, product, variety, trigger_price,
validity_ttl, iceberg_legs, group_id, kite_request_id).

This complements — doesn't replace — the NDJSON audit log at
`data/audit/*.jsonl` (which captures every CLI invocation). The SQLite
store is specifically for **engine-driven** orders + decisions, where
relational queries ("show me all decisions for strategy X on 2026-04-21
that resulted in a place_order") matter.

Storage: defaults to `data/trading.sqlite`; override via `TRADING_DB_PATH`.
WAL mode for concurrent-reader safety.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB_PATH = Path("data/trading.sqlite")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_ms       INTEGER NOT NULL,
    ended_at_ms         INTEGER,
    config_json         TEXT NOT NULL,
    strategy            TEXT,
    strategy_id         TEXT,
    agent_id            TEXT,
    parent_request_id   TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL,
    ts_ms           INTEGER NOT NULL,
    strategy        TEXT NOT NULL,
    intent_json     TEXT NOT NULL,
    accepted        INTEGER NOT NULL,
    reason          TEXT,
    request_id      TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER,
    ts_ms               INTEGER NOT NULL,
    broker              TEXT NOT NULL,
    order_id            TEXT NOT NULL,
    tag                 TEXT,
    exchange            TEXT,
    tradingsymbol       TEXT,
    side                TEXT,
    order_type          TEXT,
    product             TEXT,
    variety             TEXT,
    quantity            INTEGER,
    price               REAL,
    trigger_price       REAL,
    validity            TEXT,
    validity_ttl        INTEGER,
    disclosed_quantity  INTEGER,
    iceberg_legs        INTEGER,
    iceberg_quantity    INTEGER,
    market_protection   REAL,
    request_json        TEXT,
    status              TEXT,
    group_id            TEXT,
    leg_name            TEXT,
    idempotency_key     TEXT,
    kite_request_id     TEXT,
    request_id          TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_orders_order_id  ON orders(order_id);
CREATE INDEX IF NOT EXISTS idx_orders_symbol    ON orders(tradingsymbol);
CREATE INDEX IF NOT EXISTS idx_orders_run       ON orders(run_id);
CREATE INDEX IF NOT EXISTS idx_orders_tag       ON orders(tag);
CREATE INDEX IF NOT EXISTS idx_orders_group     ON orders(group_id);
CREATE INDEX IF NOT EXISTS idx_orders_idem      ON orders(idempotency_key);

CREATE TABLE IF NOT EXISTS order_status_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER,
    ts_ms               INTEGER NOT NULL,
    broker              TEXT NOT NULL,
    order_id            TEXT NOT NULL,
    status              TEXT,
    filled              REAL,
    remaining           REAL,
    avg_fill_price      REAL,
    status_message      TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_status_events_order ON order_status_events(order_id);
CREATE INDEX IF NOT EXISTS idx_status_events_run   ON order_status_events(run_id);

CREATE TABLE IF NOT EXISTS errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER,
    ts_ms       INTEGER NOT NULL,
    where_text  TEXT NOT NULL,
    error_code  TEXT,
    message     TEXT NOT NULL,
    request_id  TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER,
    ts_ms       INTEGER NOT NULL,
    actor       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    accepted    INTEGER,
    reason      TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
"""


def default_db_path() -> Path:
    raw = os.getenv("TRADING_DB_PATH")
    return Path(raw) if raw else DEFAULT_DB_PATH


def _now_ms() -> int:
    return int(time.time() * 1000)


def _to_jsonable(obj: Any) -> Any:
    """Coerce dataclasses / dates / other to JSON-safe types."""
    if obj is None or isinstance(obj, (str, int, bool, float)):
        return obj
    if is_dataclass(obj):
        return _to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(x) for x in obj]
    from datetime import date, datetime
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return {k: _to_jsonable(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def _json_dumps(obj: Any) -> str:
    return json.dumps(_to_jsonable(obj), default=str, ensure_ascii=False)


class SqliteStore:
    """Thread-safe SQLite audit store for the engine.

    Every run gets its own `runs` row with start/end timestamps and config.
    Decisions, orders, status events, errors, and actions all reference the
    run.
    """

    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path is not None else default_db_path()
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(str(self._path), timeout=30.0)
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def close(self) -> None:
        """No long-lived connection; kept for symmetry with trading-algo."""
        return None

    # ----------------------------------------------------------------
    # Runs
    # ----------------------------------------------------------------

    def start_run(
        self,
        cfg: Any | None = None,
        *,
        strategy: str | None = None,
        strategy_id: str | None = None,
        agent_id: str | None = None,
        parent_request_id: str | None = None,
    ) -> int:
        """Open a new run row. Returns the auto-incremented id."""
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO runs "
                "(started_at_ms, config_json, strategy, strategy_id, agent_id, parent_request_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_now_ms(), _json_dumps(cfg), strategy, strategy_id, agent_id, parent_request_id),
            )
            return int(cur.lastrowid)

    def end_run(self, run_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET ended_at_ms=? WHERE id=?",
                (_now_ms(), int(run_id)),
            )

    def list_runs(self, limit: int = 25) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, started_at_ms, ended_at_ms, strategy, strategy_id, agent_id "
                "FROM runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0], "started_at_ms": int(r[1]),
                "ended_at_ms": int(r[2]) if r[2] is not None else None,
                "strategy": r[3], "strategy_id": r[4], "agent_id": r[5],
            }
            for r in rows
        ]

    # ----------------------------------------------------------------
    # Decisions
    # ----------------------------------------------------------------

    def log_decision(
        self,
        run_id: int | None,
        *,
        strategy: str,
        intent: Any,
        accepted: bool,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO decisions "
                "(run_id, ts_ms, strategy, intent_json, accepted, reason, request_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, _now_ms(), strategy, _json_dumps(intent),
                 1 if accepted else 0, reason, request_id),
            )
            return int(cur.lastrowid)

    # ----------------------------------------------------------------
    # Orders
    # ----------------------------------------------------------------

    def log_order(
        self,
        run_id: int | None,
        *,
        broker: str,
        order_id: str,
        request: Any,
        status: str | None = None,
        tag: str | None = None,
        group_id: str | None = None,
        leg_name: str | None = None,
        idempotency_key: str | None = None,
        kite_request_id: str | None = None,
        request_id: str | None = None,
    ) -> int:
        """Persist a placed order. `request` may be a dataclass / dict /
        OrderRequest; field extraction is best-effort.
        """
        r = request if isinstance(request, dict) else _to_jsonable(request)
        if not isinstance(r, dict):
            r = {}

        # Convert InstrumentSpec to a flat projection for indexed columns.
        inst = r.get("instrument")
        if isinstance(inst, dict):
            exchange = inst.get("exchange")
            tradingsymbol = inst.get("symbol")
        else:
            exchange = r.get("exchange")
            tradingsymbol = r.get("tradingsymbol") or r.get("symbol")

        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO orders ("
                " run_id, ts_ms, broker, order_id, tag,"
                " exchange, tradingsymbol, side, order_type, product, variety,"
                " quantity, price, trigger_price, validity, validity_ttl,"
                " disclosed_quantity, iceberg_legs, iceberg_quantity,"
                " market_protection, request_json, status,"
                " group_id, leg_name, idempotency_key, kite_request_id, request_id"
                ") VALUES (?,?,?,?,?, ?,?,?,?,?,?, ?,?,?,?,?, ?,?,?, ?,?,?, ?,?,?,?,?)",
                (
                    run_id, _now_ms(), broker, str(order_id), tag,
                    exchange, tradingsymbol,
                    r.get("side") or r.get("transaction_type"),
                    r.get("order_type"),
                    r.get("product"),
                    r.get("variety"),
                    r.get("quantity"),
                    r.get("limit_price") or r.get("price"),
                    r.get("trigger_price"),
                    r.get("validity"),
                    r.get("validity_ttl"),
                    r.get("disclosed_quantity"),
                    r.get("iceberg_legs"),
                    r.get("iceberg_quantity"),
                    r.get("market_protection"),
                    _json_dumps(r),
                    status,
                    group_id, leg_name, idempotency_key, kite_request_id, request_id,
                ),
            )
            return int(cur.lastrowid)

    def update_order_status(self, order_id: str, status: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE orders SET status=? WHERE order_id=?",
                (status, order_id),
            )

    def get_order(self, order_id: str) -> dict | None:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            cur = c.execute("SELECT * FROM orders WHERE order_id=?", (order_id,))
            row = cur.fetchone()
            if row is None:
                return None
            return dict(row)

    def list_non_terminal_order_ids(self) -> list[str]:
        TERMINAL = ("COMPLETE", "CANCELLED", "REJECTED")
        placeholders = ",".join("?" * len(TERMINAL))
        with self._conn() as c:
            rows = c.execute(
                f"SELECT order_id FROM orders WHERE status NOT IN ({placeholders}) "
                f"OR status IS NULL",
                TERMINAL,
            ).fetchall()
        return [r[0] for r in rows]

    # ----------------------------------------------------------------
    # Order status events
    # ----------------------------------------------------------------

    def log_order_status_event(
        self,
        run_id: int | None,
        broker: str,
        order_status: Any,
    ) -> int:
        d = order_status if isinstance(order_status, dict) else _to_jsonable(order_status)
        if not isinstance(d, dict):
            d = {}
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO order_status_events "
                "(run_id, ts_ms, broker, order_id, status, filled, remaining, "
                " avg_fill_price, status_message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, _now_ms(), broker, str(d.get("order_id") or ""),
                    d.get("status"),
                    d.get("filled") or d.get("filled_quantity"),
                    d.get("remaining") or d.get("pending_quantity"),
                    d.get("avg_fill_price") or d.get("average_price"),
                    d.get("status_message"),
                ),
            )
            return int(cur.lastrowid)

    def get_latest_status(self, order_id: str) -> dict | None:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            cur = c.execute(
                "SELECT * FROM order_status_events WHERE order_id=? "
                "ORDER BY ts_ms DESC LIMIT 1",
                (order_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    # ----------------------------------------------------------------
    # Errors + actions
    # ----------------------------------------------------------------

    def log_error(
        self,
        run_id: int | None,
        *,
        where: str,
        message: str,
        error_code: str | None = None,
        request_id: str | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO errors "
                "(run_id, ts_ms, where_text, error_code, message, request_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, _now_ms(), where, error_code, message, request_id),
            )
            return int(cur.lastrowid)

    def log_action(
        self,
        run_id: int | None,
        *,
        actor: str,
        payload: Any,
        accepted: bool | None = None,
        reason: str | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO actions (run_id, ts_ms, actor, payload_json, accepted, reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, _now_ms(), actor, _json_dumps(payload),
                 None if accepted is None else (1 if accepted else 0), reason),
            )
            return int(cur.lastrowid)

    # ----------------------------------------------------------------
    # Raw query (escape hatch for introspection tools)
    # ----------------------------------------------------------------

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Run a read-only SQL query. Intended for status/reconcile tools,
        not user-supplied input — we don't sandbox beyond what SQLite does.
        """
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
