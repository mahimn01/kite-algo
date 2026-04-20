"""NDJSON audit log for SEBI-compliant algorithmic trade records.

Every CLI invocation appends one JSON line to a day-rotated file under
`data/audit/YYYY-MM-DD.jsonl`. This is the compliance artifact: per
SEBI's April 2026 retail algo circular, every algorithmic order must
have a retained audit trail including timestamp, agent/algo identifier,
and request parameters. Retention: 5 years per algo circular, 8 years
under the broader 2026 stock-broker regulations — we default to 8 to
cover the stricter bound.

Why NDJSON + daily rotation (not SQLite)?

- **Append-only line writes are atomic** on POSIX up to PIPE_BUF (4KiB),
  so concurrent writes never interleave partial lines.
- **Daily rotation is simple** (filename contains date) and makes long-
  term archival trivial (tar one file = tar one trading day).
- **SQLite is already used** for idempotency and groups — audit has a
  different access pattern (massive sequential appends + rare ranged
  reads) that NDJSON handles better.
- **Tools like `jq`, `grep`, `awk`** work on it without extra infra.

Audit line shape:

    {
      "ts": "2026-04-21T10:15:30.123+05:30",
      "ts_epoch_ms": 1745234130123,
      "request_id": "01JBP...",
      "parent_request_id": "01JBO..." | null,
      "cmd": "place",
      "args": {...redacted...},
      "exit_code": 0,
      "error_code": "HARD_REJECT" | null,
      "elapsed_ms": 234,
      "kite_request_id": "..." | null,
      "kite_order_id": "..." | null,
      "strategy_id": "..." | null,
      "agent_id": "..." | null
    }
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from kite_algo.market_rules import IST
from kite_algo.redaction import redact_text


DEFAULT_AUDIT_DIR = Path("data/audit")

# SEBI stock-broker 2026 rule. 8 years > 5 algo-circular requirement.
RETENTION_YEARS = 8


def audit_dir() -> Path:
    raw = os.getenv("KITE_AUDIT_DIR")
    return Path(raw) if raw else DEFAULT_AUDIT_DIR


def audit_path_for(when: datetime | date | None = None, *, root: Path | None = None) -> Path:
    """Return the audit file path for a given calendar day (IST).

    Default = today's IST date.
    """
    if when is None:
        d = datetime.now(tz=IST).date()
    elif isinstance(when, datetime):
        d = (when.astimezone(IST) if when.tzinfo else when.replace(tzinfo=IST)).date()
    else:
        d = when
    root = root or audit_dir()
    return root / f"{d.isoformat()}.jsonl"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _redact_args(args: dict) -> dict:
    """Redact values in an args dict before persisting. We don't want to
    write access_tokens or api_secrets that might have been passed via
    CLI or env. The redactor also scrubs token-shaped substrings.
    """
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str):
            out[k] = redact_text(v)
        elif isinstance(v, (dict, list)):
            # Serialise-then-redact so nested strings are covered too.
            try:
                serialised = json.dumps(v, default=str)
            except (TypeError, ValueError):
                out[k] = str(v)
                continue
            out[k] = json.loads(redact_text(serialised))
        else:
            out[k] = v
    return out


@dataclass
class AuditEntry:
    """In-memory view of one audit line."""
    ts: str
    ts_epoch_ms: int
    request_id: str
    cmd: str
    args: dict
    exit_code: int | None = None
    error_code: str | None = None
    elapsed_ms: int | None = None
    kite_request_id: str | None = None
    kite_order_id: str | None = None
    parent_request_id: str | None = None
    strategy_id: str | None = None
    agent_id: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out: dict = {
            "ts": self.ts,
            "ts_epoch_ms": self.ts_epoch_ms,
            "request_id": self.request_id,
            "parent_request_id": self.parent_request_id,
            "cmd": self.cmd,
            "args": self.args,
            "exit_code": self.exit_code,
            "error_code": self.error_code,
            "elapsed_ms": self.elapsed_ms,
            "kite_request_id": self.kite_request_id,
            "kite_order_id": self.kite_order_id,
            "strategy_id": self.strategy_id,
            "agent_id": self.agent_id,
        }
        if self.extra:
            out.update(self.extra)
        return out


def _atomic_append_line(path: Path, line: str) -> None:
    """Append a single newline-terminated line via one `write()` syscall.

    POSIX guarantees writes under PIPE_BUF bytes (4096 on Linux, macOS) are
    atomic when the file is opened O_APPEND. Lines longer than that could
    theoretically interleave — we don't truncate because truncating audit
    data is a bigger harm than (rare) interleave. If a line approaches 4KB
    of purely-agent-controlled payload, the agent's problem starts earlier.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    # O_APPEND → atomic append on POSIX.
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(str(path), flags, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def write_entry(entry: AuditEntry, *, root: Path | None = None) -> Path:
    """Append `entry` to today's audit file. Returns the file path."""
    now_dt = datetime.now(tz=IST)
    if not entry.ts:
        entry.ts = now_dt.isoformat(timespec="milliseconds")
    if not entry.ts_epoch_ms:
        entry.ts_epoch_ms = int(now_dt.timestamp() * 1000)
    path = audit_path_for(now_dt, root=root)
    line = json.dumps(entry.to_dict(), default=str, ensure_ascii=False) + "\n"
    _atomic_append_line(path, line)
    return path


def log_command(
    *,
    cmd: str,
    request_id: str,
    args: dict,
    exit_code: int | None = None,
    error_code: str | None = None,
    elapsed_ms: int | None = None,
    kite_request_id: str | None = None,
    kite_order_id: str | None = None,
    parent_request_id: str | None = None,
    strategy_id: str | None = None,
    agent_id: str | None = None,
    extra: dict | None = None,
    root: Path | None = None,
) -> Path:
    """One-call convenience wrapper over AuditEntry + write_entry."""
    entry = AuditEntry(
        ts="",
        ts_epoch_ms=0,
        request_id=request_id,
        cmd=cmd,
        args=_redact_args(args),
        exit_code=exit_code,
        error_code=error_code,
        elapsed_ms=elapsed_ms,
        kite_request_id=kite_request_id,
        kite_order_id=kite_order_id,
        parent_request_id=parent_request_id,
        strategy_id=strategy_id,
        agent_id=agent_id,
        extra=extra or {},
    )
    return write_entry(entry, root=root)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def iter_entries(
    *,
    since: datetime | date | None = None,
    until: datetime | date | None = None,
    cmd: str | None = None,
    outcome: str | None = None,   # "ok" | "error" | None
    root: Path | None = None,
) -> Iterable[dict]:
    """Iterate audit entries in chronological order.

    `since` / `until` are inclusive date filters in IST. `outcome='ok'` =
    exit_code 0; `outcome='error'` = non-zero exit_code. Commands with
    exit_code=None (rare — crash-before-log) are treated as errors.

    Yields dicts, not AuditEntry objects, so downstream code can freely
    add fields we didn't model up here.
    """
    root = root or audit_dir()
    if not root.exists():
        return
    files = sorted(root.glob("*.jsonl"))
    for f in files:
        # Quick date filter from filename before opening the file.
        try:
            day = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if since is not None:
            since_date = since.date() if isinstance(since, datetime) else since
            if day < since_date:
                continue
        if until is not None:
            until_date = until.date() if isinstance(until, datetime) else until
            if day > until_date:
                continue

        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        # A partially-written line is rare (atomic append)
                        # but possible if an OS killed us mid-write for a
                        # >4KB record. Skip.
                        continue
                    if cmd is not None and entry.get("cmd") != cmd:
                        continue
                    if outcome == "ok" and entry.get("exit_code") != 0:
                        continue
                    if outcome == "error" and entry.get("exit_code") == 0:
                        continue
                    yield entry
        except OSError:
            continue


def tail(
    n: int = 100,
    *,
    cmd: str | None = None,
    outcome: str | None = None,
    root: Path | None = None,
) -> list[dict]:
    """Return the last `n` matching entries across all audit files."""
    # Simple implementation: iterate all, keep last n. Daily rotation keeps
    # this bounded for any reasonable query.
    from collections import deque
    buf: deque[dict] = deque(maxlen=max(1, n))
    for e in iter_entries(cmd=cmd, outcome=outcome, root=root):
        buf.append(e)
    return list(buf)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def purge_older_than(days: int, *, root: Path | None = None) -> int:
    """Delete audit files older than `days` days. Returns number deleted.

    Default SEBI retention: 8 years ≈ 2920 days. Operators that don't need
    full retention (e.g. paper/testing) can call this with a shorter cutoff.
    """
    root = root or audit_dir()
    if not root.exists():
        return 0
    cutoff = datetime.now(tz=IST).date()
    cutoff = cutoff.replace(year=cutoff.year - 100) if days > 365_000 else cutoff
    # Simpler cutoff calculation.
    from datetime import timedelta
    cutoff_date = datetime.now(tz=IST).date() - timedelta(days=days)
    deleted = 0
    for f in root.glob("*.jsonl"):
        try:
            day = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if day < cutoff_date:
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass
    return deleted
