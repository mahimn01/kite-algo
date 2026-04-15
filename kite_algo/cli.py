"""Main trading-engine CLI (scaffold).

Parallel to `trading_algo.cli` but targeting Kite. For now this is a thin
skeleton — commands are stubs that delegate to the engine or broker once
those are built out. For live data + ops in the meantime use the full
`kite_algo.kite_tool` CLI.
"""

from __future__ import annotations

import argparse
import sys

from kite_algo.broker.kite import KiteBroker
from kite_algo.broker.sim import SimBroker
from kite_algo.config import TradingConfig


def _make_broker(cfg: TradingConfig):
    if cfg.broker == "sim":
        return SimBroker()
    return KiteBroker(cfg)


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
    return 0


def _cmd_stub(args: argparse.Namespace) -> int:
    print(
        f"`{args.cmd}` is not implemented yet. Use `python -m kite_algo.kite_tool` "
        f"for live data/ops commands until the engine lands.",
        file=sys.stderr,
    )
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kite_algo.cli",
        description="Kite Algo — trading engine CLI (scaffold).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="Print effective config + session status")
    s.set_defaults(func=_cmd_status)

    for stub_cmd, desc in [
        ("place-order", "Place a single order (pending implementation)"),
        ("cancel-order", "Cancel an order (pending implementation)"),
        ("modify-order", "Modify an order (pending implementation)"),
        ("run", "Run the strategy polling loop (pending implementation)"),
    ]:
        sp = sub.add_parser(stub_cmd, help=desc)
        sp.set_defaults(func=_cmd_stub)

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
