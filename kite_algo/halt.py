"""Trading halt sentinel — emergency kill switch for write commands.

When an operator or an automated circuit breaker detects something wrong
(a runaway strategy, a suspicious loss, an infrastructure incident), they
run `kite-algo halt --reason "..."`. This writes a sentinel file at
`data/HALTED` containing the reason, the timestamp, and an optional
expiry. Every write command checks for this sentinel at entry and
refuses with exit code `HALTED` (11) until cleared.

Key properties:

- **Sentinel survives process restarts.** Not in memory. A crash doesn't
  silently lift the halt.
- **Independent of the Kite broker.** A halt blocks LOCAL writes, not
  orders already at Kite. Use `cancel-all --confirm-panic` to flatten the
  book after halting.
- **Optional auto-expiry.** `halt --expires-in 1h` — useful for a
  time-boxed circuit breaker. Expired sentinels are treated as cleared;
  first check after expiry removes the file.
- **Resume requires a distinct confirmation token.** `resume --confirm-resume`
  uses a different flag name from `--yes` so `resume --yes` is not valid
  — an agent can't accidentally lift the halt the same way it might
  accidentally confirm a place order.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_PATH = Path("data/HALTED")


@dataclass(frozen=True)
class HaltState:
    """Shape of the sentinel file's contents."""
    reason: str
    since_epoch_ms: int
    by: str                           # operator / agent id
    expires_epoch_ms: int | None = None
    request_id: str | None = None     # request that created the halt

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "reason": self.reason,
            "since_epoch_ms": self.since_epoch_ms,
            "by": self.by,
        }
        if self.expires_epoch_ms is not None:
            out["expires_epoch_ms"] = self.expires_epoch_ms
        if self.request_id is not None:
            out["request_id"] = self.request_id
        return out

    def is_expired(self, *, now_ms: int | None = None) -> bool:
        if self.expires_epoch_ms is None:
            return False
        n = now_ms if now_ms is not None else int(time.time() * 1000)
        return n >= self.expires_epoch_ms


def halt_path() -> Path:
    """Configurable via env for testing / multi-instance deployments."""
    raw = os.getenv("KITE_HALT_PATH")
    return Path(raw) if raw else DEFAULT_PATH


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def read_halt(path: Path | None = None) -> HaltState | None:
    """Return the current HaltState, or None if not halted.

    Auto-clears an expired sentinel: if the file exists but `expires_epoch_ms`
    is in the past, we unlink it and return None. This keeps the
    "expires-in" convenience non-racy — a caller doesn't have to remember
    to clear the sentinel after expiry.
    """
    p = path or halt_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Malformed sentinel — treat as still halted (fail-closed) so a
        # corrupt file can't accidentally enable trading.
        return HaltState(
            reason="(sentinel file corrupt; assumed halted)",
            since_epoch_ms=int(time.time() * 1000),
            by="unknown",
        )
    state = HaltState(
        reason=str(data.get("reason") or "(no reason given)"),
        since_epoch_ms=int(data.get("since_epoch_ms") or 0),
        by=str(data.get("by") or "unknown"),
        expires_epoch_ms=(
            int(data["expires_epoch_ms"])
            if data.get("expires_epoch_ms") is not None else None
        ),
        request_id=data.get("request_id"),
    )
    if state.is_expired():
        # Best-effort cleanup. If another process is also checking, both
        # might try to unlink; the second one gets FileNotFoundError, fine.
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return None
    return state


def is_halted(path: Path | None = None) -> bool:
    return read_halt(path) is not None


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def write_halt(
    *,
    reason: str,
    by: str,
    expires_in_seconds: float | None = None,
    request_id: str | None = None,
    path: Path | None = None,
) -> HaltState:
    """Create or overwrite the halt sentinel atomically."""
    from kite_algo.config import atomic_write_text

    now_ms = int(time.time() * 1000)
    expires_ms = (
        now_ms + int(expires_in_seconds * 1000)
        if expires_in_seconds is not None else None
    )
    state = HaltState(
        reason=reason, since_epoch_ms=now_ms, by=by,
        expires_epoch_ms=expires_ms, request_id=request_id,
    )
    p = path or halt_path()
    atomic_write_text(p, json.dumps(state.to_dict(), indent=2))
    return state


def clear_halt(path: Path | None = None) -> bool:
    """Remove the sentinel. Returns True if the sentinel existed and was
    cleared, False if no halt was active."""
    p = path or halt_path()
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Guard decorator for write commands
# ---------------------------------------------------------------------------

# Duration-parsing helper for `--expires-in`.
# Accepts `30s`, `5m`, `1h`, `2d`, or a bare float (seconds).
_DURATION_UNITS: dict[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(raw: str) -> float:
    """Parse durations like '30s', '5m', '1h', '2d', or bare float seconds."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty duration")
    suffix = raw[-1].lower()
    if suffix in _DURATION_UNITS:
        try:
            n = float(raw[:-1])
        except ValueError as exc:
            raise ValueError(f"bad duration: {raw!r}") from exc
        return n * _DURATION_UNITS[suffix]
    # Bare float — assume seconds.
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(
            f"bad duration: {raw!r} (use 30s, 5m, 1h, 2d, or a bare number of seconds)"
        ) from exc


class HaltActive(RuntimeError):
    """Raised by `assert_not_halted` when a write command runs while halted."""
    def __init__(self, state: HaltState):
        self.state = state
        super().__init__(
            f"Trading is HALTED: {state.reason!r} since "
            f"{state.since_epoch_ms / 1000:.0f}. "
            f"Run `kite-algo resume --confirm-resume` to clear."
        )


def assert_not_halted(path: Path | None = None) -> None:
    """Raise HaltActive if the sentinel is present (and not expired).

    Call at the top of every write command. `HaltActive` classifies as
    exit code 11 (HALTED) via `kite_algo.exit_codes`.
    """
    state = read_halt(path)
    if state is not None:
        raise HaltActive(state)
