"""Structured output envelope for agent consumption.

Every non-streaming CLI command wraps its output as:

    {
      "ok": true,
      "cmd": "place",
      "schema_version": "1",
      "request_id": "01HNKXXXX...",        // ULID-ish, time-sortable
      "data": { ... },                      // command-specific payload
      "warnings": [{"code": "...", "message": "..."}],
      "meta": {
        "elapsed_ms": 423,
        "retries": 0,
        "replayed": false,
        "parent_request_id": null          // inherited from KITE_PARENT_REQUEST_ID
      }
    }

On error, `ok=false`, `data=null`, and a top-level `error` object (see
`kite_algo.errors`) is added.

Why this shape:
- `ok` first so a quick peek at the top of the blob tells agents pass/fail.
- `cmd` lets log aggregators filter without parsing CLI invocation.
- `schema_version` is how we signal breaking changes — bump on any
  backwards-incompatible `data` shape change.
- `request_id` is a ULID: lexicographically sortable by creation time, 26
  base32 chars. Every log line, every SQLite audit row, every outbound Kite
  call carries this; an agent can reconstruct the full history from it.
- `meta.parent_request_id` supports nested agent workflows — if the parent
  agent sets `KITE_PARENT_REQUEST_ID=...` in the subprocess env, the child
  inherits and every action traces back to the parent turn.
- `warnings` is an array (not a single string) because multiple market-rule
  warnings can fire on one order (e.g. MIS near cutoff + freeze-qty autoslice).

Streaming commands (`stream`, future `tail-ticks`) bypass the envelope —
they emit one JSON object per line (NDJSON). The envelope is for
request/response, not streams.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from typing import Any

# Bump when the envelope shape changes in a backwards-incompatible way.
SCHEMA_VERSION = "1"

# Environment toggles.
ENV_NO_ENVELOPE = "KITE_NO_ENVELOPE"
ENV_PARENT_REQUEST_ID = "KITE_PARENT_REQUEST_ID"
ENV_FORCE_JSON = "KITE_JSON"


# ---------------------------------------------------------------------------
# ULID-ish request IDs
# ---------------------------------------------------------------------------
#
# A real ULID is 128 bits: 48 bits of time (ms since epoch) + 80 bits of
# randomness, base32-encoded to 26 chars (Crockford's base32). We implement
# that minimally here so we don't add a dependency.

# Crockford's base32 alphabet (excludes I, L, O, U to avoid confusion).
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_base32(n: int, length: int) -> str:
    """Little-endian-style base32: emit `length` chars, LSB last."""
    out: list[str] = []
    for _ in range(length):
        out.append(_ULID_ALPHABET[n & 0x1F])
        n >>= 5
    return "".join(reversed(out))


def new_request_id(clock_ms: int | None = None) -> str:
    """Generate a ULID-compatible 26-char time-sortable ID.

    The first 10 chars encode milliseconds since the Unix epoch (48 bits),
    the last 16 chars are crypto-random (80 bits). Sorting by the string
    gives you creation-time order. Collisions within the same millisecond
    are 1 in 2^80 — zero for practical purposes.
    """
    ms = clock_ms if clock_ms is not None else int(time.time() * 1000)
    rand = secrets.randbits(80)
    return _encode_base32(ms, 10) + _encode_base32(rand, 16)


def parent_request_id() -> str | None:
    """The parent agent's request ID, if set via env.

    Lets a top-level agent turn propagate its ID to every tool subprocess it
    spawns, so the full execution tree is reconstructible from audit logs.
    """
    v = os.getenv(ENV_PARENT_REQUEST_ID)
    return v.strip() if v else None


# ---------------------------------------------------------------------------
# Envelope construction
# ---------------------------------------------------------------------------

@dataclass
class Envelope:
    """In-memory representation of the output envelope.

    We use a dataclass (not a TypedDict) because agents use `meta.retries` /
    `meta.elapsed_ms` as writable counters during command execution before
    the envelope is finalised for emission.
    """
    ok: bool
    cmd: str
    request_id: str
    data: Any = None
    error: dict | None = None
    warnings: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Serialise to the canonical dict shape."""
        out: dict = {
            "ok": self.ok,
            "cmd": self.cmd,
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "data": self.data,
            "warnings": list(self.warnings),
            "meta": dict(self.meta),
        }
        if self.error is not None:
            out["error"] = self.error
        return out

    def add_warning(self, code: str, message: str, **extra: Any) -> None:
        entry = {"code": code, "message": message}
        entry.update(extra)
        self.warnings.append(entry)


def new_envelope(cmd: str) -> Envelope:
    """Start an envelope for the current command. Fills `request_id`,
    `parent_request_id` (if present), and `started_at_epoch_ms`.
    """
    rid = new_request_id()
    meta: dict = {
        "started_at_epoch_ms": int(time.time() * 1000),
    }
    parent = parent_request_id()
    if parent:
        meta["parent_request_id"] = parent
    return Envelope(ok=True, cmd=cmd, request_id=rid, meta=meta)


def finalize_envelope(env: Envelope) -> Envelope:
    """Populate `meta.elapsed_ms` from `started_at_epoch_ms`. Idempotent."""
    start = env.meta.get("started_at_epoch_ms")
    if start is not None and "elapsed_ms" not in env.meta:
        env.meta["elapsed_ms"] = max(0, int(time.time() * 1000) - int(start))
    return env


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

def envelopes_disabled() -> bool:
    """Respect the `KITE_NO_ENVELOPE=1` escape hatch for one release of
    backwards compatibility. Once downstream scripts are updated, this env
    var is removed.
    """
    raw = os.getenv(ENV_NO_ENVELOPE) or ""
    return raw.strip().lower() in ("1", "true", "yes", "on")


def json_is_default_for(stream=None) -> bool:
    """Default output format resolution.

    Rules (in order):
    - `KITE_JSON=1` forces JSON regardless of TTY.
    - If `stream` (default stdout) is NOT a TTY, default to JSON (pipes,
      subprocesses, agent runs).
    - Otherwise default to table (interactive human).
    """
    if (os.getenv(ENV_FORCE_JSON) or "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    s = stream if stream is not None else sys.stdout
    try:
        return not s.isatty()
    except (AttributeError, ValueError):
        # closed stdout, non-tty object, etc. — default to JSON.
        return True


def envelope_to_json(env: Envelope) -> str:
    """Serialise to pretty-printed JSON. Unknown objects fall through to
    `str()`. We never let a single unserialisable value crash an envelope
    emission — the agent needs to receive *something*, even if degraded.
    """
    return json.dumps(env.to_dict(), indent=2, default=str, ensure_ascii=False)
