"""Tests for the engine CLI (kite_algo.cli)."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from kite_algo.cli import build_parser, main
from kite_algo.orders import TradeIntent
from kite_algo.instruments import InstrumentSpec


@pytest.fixture
def parser():
    return build_parser()


# A minimal strategy that emits one HOLD per tick. Lives at module scope so
# it's importable via `tests.test_cli_engine:NoopStrategy`.
class NoopStrategy:
    name = "noop"
    def on_tick(self, ctx):
        return []


class YieldOne:
    name = "y1"
    def on_tick(self, ctx):
        return [TradeIntent(
            instrument=InstrumentSpec(symbol="RELIANCE", exchange="NSE"),
            side="BUY", quantity=1, order_type="LIMIT",
            product="CNC", limit_price=100.0,
        )]


# -----------------------------------------------------------------------------
# status
# -----------------------------------------------------------------------------

class TestStatus:
    def test_status_prints(self, capsys) -> None:
        rc = main(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "broker" in out
        assert "dry_run" in out


# -----------------------------------------------------------------------------
# strategy loader
# -----------------------------------------------------------------------------

class TestStrategyLoader:
    def test_bad_format_raises(self, parser) -> None:
        args = parser.parse_args([
            "run-once", "--strategy", "no_colon_here",
        ])
        with pytest.raises(SystemExit, match="module.path:ClassName"):
            from kite_algo.cli import _load_strategy
            _load_strategy(args.strategy)

    def test_unknown_class_raises(self, parser) -> None:
        from kite_algo.cli import _load_strategy
        with pytest.raises(SystemExit, match="no class"):
            _load_strategy("tests.test_cli_engine:NoSuchClass")


# -----------------------------------------------------------------------------
# run-once end-to-end with SimBroker
# -----------------------------------------------------------------------------

class TestRunOnceCli:
    def test_dry_run_exits_zero(self, parser, monkeypatch, tmp_path) -> None:
        # Force SimBroker via TRADING_BROKER=sim.
        monkeypatch.setenv("TRADING_BROKER", "sim")
        monkeypatch.delenv("TRADING_DB_PATH", raising=False)
        rc = main([
            "run-once",
            "--strategy", "tests.test_cli_engine:NoopStrategy",
            "--dry-run",
        ])
        assert rc == 0

    def test_live_sim_with_audit(self, parser, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("TRADING_BROKER", "sim")
        # Disable dry_run so the OMS actually submits through the SimBroker.
        monkeypatch.setenv("TRADING_DRY_RUN", "false")
        db = tmp_path / "t.sqlite"
        # Permissive risk limits so YieldOne's intent isn't rejected.
        monkeypatch.setenv("KITE_RISK_MAX_ORDER_QTY", "1000000")
        monkeypatch.setenv("KITE_RISK_MAX_SINGLE_ORDER_INR", "1000000000000")
        monkeypatch.setenv("KITE_RISK_MAX_POS_PER_SYMBOL", "1000000")
        monkeypatch.setenv("KITE_RISK_MARKET_HOURS", "false")
        monkeypatch.setenv("KITE_RISK_MIS_CUTOFF", "false")
        monkeypatch.setenv("KITE_RISK_FREEZE_QTY", "false")
        monkeypatch.setenv("KITE_RISK_LOT_SIZE", "false")
        rc = main([
            "run-once",
            "--strategy", "tests.test_cli_engine:YieldOne",
            "--db-path", str(db),
        ])
        assert rc == 0

        from kite_algo.persistence import SqliteStore
        store = SqliteStore(db)
        orders = store.query("SELECT * FROM orders")
        assert len(orders) == 1
        assert orders[0]["tradingsymbol"] == "RELIANCE"
