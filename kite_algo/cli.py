"""Trading-engine CLI.

Small argparse surface on top of `kite_algo.engine.Engine`:

  kite-algo-cli status        — effective config + session state
  kite-algo-cli run-once      — one tick; connect, fetch, strategy, risk, submit
  kite-algo-cli run           — the polling loop (Ctrl+C to stop)

The full data/ops surface lives in `kite_algo.kite_tool`. This CLI is only
for operators who want to run an actual strategy loop — most agent-driven
interactions use `kite_tool` directly.

A strategy is referenced by `module:ClassName` (Python dotted import). The
class must expose `.name: str` and `.on_tick(ctx) -> list[TradeIntent]`.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Any

from kite_algo.broker.kite import KiteBroker
from kite_algo.broker.sim import SimBroker
from kite_algo.config import TradingConfig
from kite_algo.engine import Engine, default_risk_manager
from kite_algo.risk import RiskManager, risk_limits_from_env


def _make_broker(cfg: TradingConfig):
    """Route to the right broker adapter."""
    if cfg.broker == "sim":
        return SimBroker()
    return KiteBroker(cfg)


def _load_strategy(spec: str) -> Any:
    """Import `module:ClassName` and return an instance."""
    if ":" not in spec:
        raise SystemExit(
            f"--strategy must be 'module.path:ClassName', got {spec!r}"
        )
    mod_path, cls_name = spec.split(":", 1)
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name, None)
    if cls is None:
        raise SystemExit(f"no class {cls_name!r} in module {mod_path}")
    return cls()


def _make_engine(args: argparse.Namespace) -> Engine:
    cfg = TradingConfig.from_env()
    if args.dry_run:
        cfg = _override_dry_run(cfg, True)
    if args.db_path:
        cfg = _override_db_path(cfg, args.db_path)

    broker = _make_broker(cfg)
    strategy = _load_strategy(args.strategy)
    risk = default_risk_manager(risk_limits_from_env())
    return Engine(
        broker=broker, config=cfg, strategy=strategy, risk=risk,
        confirm_token=args.confirm_token,
        market_data_ttl=args.market_data_ttl,
    )


def _override_dry_run(cfg: TradingConfig, val: bool) -> TradingConfig:
    import dataclasses
    return dataclasses.replace(cfg, dry_run=val)


def _override_db_path(cfg: TradingConfig, path: str) -> TradingConfig:
    import dataclasses
    return dataclasses.replace(cfg, db_path=path)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_status(args: argparse.Namespace) -> int:
    cfg = TradingConfig.from_env()
    print(f"broker               = {cfg.broker}")
    print(f"allow_live           = {cfg.allow_live}")
    print(f"live_enabled         = {cfg.live_enabled}")
    print(f"dry_run              = {cfg.dry_run}")
    print(f"kite api_key set     = {bool(cfg.kite.api_key)}")
    print(f"kite api_secret set  = {bool(cfg.kite.api_secret)}")
    print(f"kite access_token    = {'set' if cfg.kite.access_token else 'MISSING (run kite_tool login)'}")
    print(f"kite exchanges       = {','.join(cfg.kite.exchanges)}")
    print(f"db_path              = {cfg.db_path or '(disabled — set TRADING_DB_PATH)'}")
    print(f"poll_seconds         = {cfg.poll_seconds}")
    print()
    print("Engine components:")
    print(f"  RiskManager         — {risk_limits_from_env()}")
    return 0


def _cmd_run_once(args: argparse.Namespace) -> int:
    engine = _make_engine(args)
    try:
        engine.run_once()
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    engine = _make_engine(args)
    try:
        engine.run_forever()
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kite_algo.cli",
        description="Kite Algo — trading engine CLI.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="Print effective config + session status")
    s.set_defaults(func=_cmd_status)

    def _add_engine_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--strategy", required=True, metavar="module:Class",
                        help="Python dotted path to the strategy class, "
                             "e.g. my_strategies.wheel:Wheel")
        sp.add_argument("--dry-run", action="store_true",
                        help="Force TRADING_DRY_RUN=true for this invocation")
        sp.add_argument("--db-path", default=None,
                        help="Override TRADING_DB_PATH for the audit SQLite store")
        sp.add_argument("--confirm-token", default=None,
                        help="Matches TRADING_ORDER_TOKEN when "
                             "TRADING_CONFIRM_TOKEN_REQUIRED=true")
        sp.add_argument("--market-data-ttl", type=float, default=1.0,
                        help="MarketDataClient cache TTL (seconds, default 1.0)")

    sp = sub.add_parser("run-once", help="Run one tick of the engine loop")
    _add_engine_flags(sp)
    sp.set_defaults(func=_cmd_run_once)

    sp = sub.add_parser("run", help="Run the engine loop (Ctrl+C to stop)")
    _add_engine_flags(sp)
    sp.set_defaults(func=_cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
