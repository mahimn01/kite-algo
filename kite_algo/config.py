"""Configuration dataclasses with safety rails.

Mirrors the `trading_algo.config` module's shape so the broker interfaces line
up across repos, but specialised for Kite Connect's daily-rotating access
tokens and Indian market segments.

Hardened for enterprise use:
- `_env_bool` is strict: unknown values raise, not silently treated as False.
- `save_session` is atomic: O_CREAT|O_EXCL|O_WRONLY at 0o600, then os.rename.
  No TOCTOU window where the file is world-readable.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


# -----------------------------------------------------------------------------
# .env loader (minimal, no dotenv dependency required at import time)
# -----------------------------------------------------------------------------

def load_dotenv(path: str = ".env") -> None:
    """Load .env into os.environ if present. Leaves shell overrides intact."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if os.getenv(k) in (None, ""):
                os.environ[k] = v


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default) or default


_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSY = frozenset({"0", "false", "no", "off", "n", "f", ""})


class EnvParseError(ValueError):
    """Raised when an env var is set but cannot be parsed.

    Never silently defaulted — a typo on a safety-critical env var must fail
    loud, not quietly flip behaviour.
    """


def _env_bool(name: str, default: bool = False) -> bool:
    """Strict env-bool parser.

    Unset → `default`. Set to anything in `_TRUTHY` or `_FALSY` → explicit.
    Anything else → `EnvParseError`. This matters for safety flags like
    `TRADING_ALLOW_LIVE` where a typo must never be silently interpreted.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    norm = raw.strip().lower()
    if norm in _TRUTHY:
        return True
    if norm in _FALSY:
        return False
    raise EnvParseError(
        f"Env var {name}={raw!r} is not a recognised boolean. "
        f"Use one of: {sorted(_TRUTHY | _FALSY - {''})}. "
        f"Unset the var to get the default ({default})."
    )


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise EnvParseError(f"Env var {name}={raw!r} is not a float: {exc}") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise EnvParseError(f"Env var {name}={raw!r} is not an int: {exc}") from exc


# -----------------------------------------------------------------------------
# Atomic write helpers
# -----------------------------------------------------------------------------

def atomic_write_text(path: Path, data: str, *, mode: int = 0o600) -> None:
    """Atomically write `data` to `path` with the given file mode.

    Writes to a temp file in the same directory, fsyncs, then renames over the
    target. Guarantees:
    - No partial-write state visible on filesystem (rename is atomic on POSIX).
    - Temp file is created with `mode` permissions from the start (no TOCTOU
      window where another process could read a world-readable version).
    - Parent directory is created with 0o700 if missing.

    On Windows rename is best-effort (replace existing); mode is a no-op.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Restrict parent mode too on POSIX — mkdir(mode=0o700) only applies if it
    # didn't already exist, so defensively chmod here as well.
    if os.name == "posix":
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass

    # NamedTemporaryFile with delete=False so we can rename.  dir= ensures the
    # temp is on the same filesystem as the target (required for atomic rename).
    fd = None
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        if os.name == "posix":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None  # fdopen took ownership
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # fsync not supported on some FS (e.g. some network mounts)
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_name is not None and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


# -----------------------------------------------------------------------------
# Session file (stores the daily-rotating access token)
# -----------------------------------------------------------------------------

DEFAULT_SESSION_PATH = Path("data/session.json")


def load_session(path: Path = DEFAULT_SESSION_PATH) -> dict:
    """Load the cached Kite session (access_token + metadata).

    Returns `{}` on missing or malformed file. The caller treats that as
    "no session — run login". We never raise on decode errors because a
    half-written file from a crashed `login` shouldn't block recovery.
    """
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_session(data: dict, path: Path = DEFAULT_SESSION_PATH) -> None:
    """Write session atomically at mode 0o600.

    The file contains a live access token valid for the rest of the trading
    day.  Atomic rename guarantees the file is never visible in a truncated
    state even if the process is killed mid-write.
    """
    atomic_write_text(path, json.dumps(data, indent=2), mode=0o600)


def get_access_token() -> str:
    """Access token precedence: env var > session file."""
    env_tok = os.getenv("KITE_ACCESS_TOKEN")
    if env_tok:
        return env_tok
    return load_session().get("access_token", "")


# -----------------------------------------------------------------------------
# Kite-specific config
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class KiteConfig:
    api_key: str
    api_secret: str
    access_token: str
    user_id: str
    exchanges: tuple[str, ...] = ("NSE", "NFO")
    instruments_ttl_seconds: int = 86400

    @classmethod
    def from_env(cls) -> "KiteConfig":
        return cls(
            api_key=_env("KITE_API_KEY"),
            api_secret=_env("KITE_API_SECRET"),
            access_token=get_access_token(),
            user_id=_env("KITE_USER_ID"),
            exchanges=tuple(
                s.strip().upper()
                for s in _env("KITE_EXCHANGES", "NSE,NFO").split(",")
                if s.strip()
            ),
            instruments_ttl_seconds=_env_int("KITE_INSTRUMENTS_TTL_SECONDS", 86400),
        )

    def require_credentials(self) -> None:
        missing = [k for k, v in {
            "KITE_API_KEY": self.api_key,
            "KITE_API_SECRET": self.api_secret,
        }.items() if not v]
        if missing:
            raise SystemExit(
                f"Missing required Kite credentials: {', '.join(missing)}. "
                f"See .env.example."
            )

    def require_session(self) -> None:
        self.require_credentials()
        if not self.access_token:
            raise SystemExit(
                "No Kite access_token found. Run `python -m kite_algo.kite_tool "
                "login` to authenticate (tokens rotate daily between 06:45 and "
                "07:30 IST)."
            )


# -----------------------------------------------------------------------------
# Trading safety rails (mirrors trading_algo.config.TradingConfig)
# -----------------------------------------------------------------------------

BrokerKind = Literal["kite", "sim"]


@dataclass(frozen=True)
class TradingConfig:
    broker: BrokerKind = "kite"
    live_enabled: bool = False
    allow_live: bool = False
    require_paper: bool = True
    dry_run: bool = True
    order_token: str = ""
    confirm_token_required: bool = False
    db_path: str = ""
    poll_seconds: int = 5
    kite: KiteConfig = field(default_factory=KiteConfig.from_env)

    @classmethod
    def from_env(cls) -> "TradingConfig":
        broker_env = _env("TRADING_BROKER", "kite").lower()
        if broker_env not in ("kite", "sim"):
            broker_env = "kite"

        allow_live = _env_bool("TRADING_ALLOW_LIVE", False)

        return cls(
            broker=broker_env,  # type: ignore[arg-type]
            live_enabled=_env_bool("TRADING_LIVE_ENABLED", False),
            allow_live=allow_live,
            require_paper=not allow_live and _env_bool("TRADING_REQUIRE_PAPER", True),
            dry_run=_env_bool("TRADING_DRY_RUN", True),
            order_token=_env("TRADING_ORDER_TOKEN"),
            confirm_token_required=_env_bool("TRADING_CONFIRM_TOKEN_REQUIRED", False),
            db_path=_env("TRADING_DB_PATH"),
            poll_seconds=_env_int("TRADING_POLL_SECONDS", 5),
            kite=KiteConfig.from_env(),
        )

    def assert_order_authorized(self, confirm_token: str | None = None) -> None:
        """Second safety gate before any real order submission."""
        if self.dry_run:
            return
        if not self.live_enabled:
            raise SystemExit(
                "Refusing to place live orders with TRADING_LIVE_ENABLED=false."
            )
        if self.confirm_token_required:
            if not self.order_token:
                raise SystemExit(
                    "TRADING_CONFIRM_TOKEN_REQUIRED=true but TRADING_ORDER_TOKEN "
                    "is unset."
                )
            if confirm_token != self.order_token:
                raise SystemExit(
                    "Refusing to place live orders: --confirm-token did not match "
                    "TRADING_ORDER_TOKEN."
                )


# Eagerly populate env at import time so downstream modules can rely on it.
load_dotenv()
