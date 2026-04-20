"""Tests for the `watch` command + its restricted expression evaluator."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from unittest.mock import Mock

import pytest

from kite_algo.kite_tool import build_parser, cmd_watch
from kite_algo.watch_expr import UnsafeExpression, evaluate


# -----------------------------------------------------------------------------
# Expression evaluator — safety
# -----------------------------------------------------------------------------

class TestEvaluatorAllowed:
    def test_numeric_compare(self) -> None:
        assert evaluate("last_price > 1300", {"last_price": 1340}) is True
        assert evaluate("last_price > 1300", {"last_price": 1000}) is False

    def test_equals_and_not_equals(self) -> None:
        assert evaluate('status == "OPEN"', {"status": "OPEN"}) is True
        assert evaluate('status != "OPEN"', {"status": "COMPLETE"}) is True

    def test_and_or(self) -> None:
        snap = {"last_price": 1340, "volume": 50000}
        assert evaluate("last_price > 1300 and volume > 10000", snap) is True
        assert evaluate("last_price < 1300 or volume > 10000", snap) is True

    def test_not(self) -> None:
        assert evaluate("not is_halted", {"is_halted": False}) is True

    def test_arithmetic(self) -> None:
        assert evaluate("last_price * 2 > 2000", {"last_price": 1001}) is True

    def test_chained_comparison(self) -> None:
        assert evaluate("1000 < last_price < 2000", {"last_price": 1340}) is True

    def test_missing_field_is_none_not_crash(self) -> None:
        """Unknown names resolve to None; comparing None with a number
        returns False (not crash)."""
        assert evaluate("last_price > 1300", {}) is False

    def test_none_field_compare_none(self) -> None:
        assert evaluate("status == None", {"status": None}) is True


class TestEvaluatorForbidden:
    def test_no_function_calls(self) -> None:
        with pytest.raises(UnsafeExpression):
            evaluate("len(x)", {"x": "abc"})

    def test_no_attribute_access(self) -> None:
        with pytest.raises(UnsafeExpression):
            evaluate("x.foo", {"x": {}})

    def test_no_subscript(self) -> None:
        with pytest.raises(UnsafeExpression):
            evaluate("x[0]", {"x": [1, 2]})

    def test_no_lambda(self) -> None:
        with pytest.raises((UnsafeExpression, SyntaxError)):
            evaluate("(lambda: 1)()", {})

    def test_no_import(self) -> None:
        # This is a SyntaxError in eval mode, but make sure it's rejected.
        with pytest.raises(SyntaxError):
            evaluate("import os", {})

    def test_no_walrus(self) -> None:
        with pytest.raises((UnsafeExpression, SyntaxError)):
            evaluate("(y := 1)", {})

    def test_empty_expression_raises(self) -> None:
        with pytest.raises(ValueError):
            evaluate("", {})


# -----------------------------------------------------------------------------
# cmd_watch
# -----------------------------------------------------------------------------

@pytest.fixture
def parser():
    return build_parser()


class TestCmdWatch:
    def test_quote_condition_satisfied_immediately(self, parser, monkeypatch) -> None:
        from kite_algo import kite_tool as kt

        client = Mock()
        client.quote.return_value = {
            "NSE:RELIANCE": {
                "last_price": 1400,
                "volume": 99999,
                "ohlc": {"open": 1330, "high": 1405, "low": 1320, "close": 1340},
            }
        }
        monkeypatch.setattr(kt, "_new_client", lambda: client)

        args = parser.parse_args([
            "watch", "quote", "--symbol", "NSE:RELIANCE",
            "--until", "last_price > 1300",
            "--every", "0.1", "--timeout", "5",
            "--format", "json",
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = kt.cmd_watch(args)
        assert rc == 0
        parsed = json.loads(buf.getvalue())
        assert parsed["data"]["matched"] is True
        assert parsed["data"]["polls"] == 1

    def test_timeout_returns_124(self, parser, monkeypatch) -> None:
        from kite_algo import kite_tool as kt

        client = Mock()
        client.quote.return_value = {
            "NSE:RELIANCE": {"last_price": 100, "ohlc": {}}
        }
        monkeypatch.setattr(kt, "_new_client", lambda: client)

        args = parser.parse_args([
            "watch", "quote", "--symbol", "NSE:RELIANCE",
            "--until", "last_price > 10000",
            "--every", "0.05", "--timeout", "0.3",
            "--format", "json",
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = kt.cmd_watch(args)
        assert rc == 124
        data = json.loads(buf.getvalue())["data"]
        assert data["matched"] is False
        assert data["reason"] == "timeout"
        # Multiple polls happened in 300ms.
        assert data["polls"] >= 2

    def test_order_resource(self, parser, monkeypatch) -> None:
        from kite_algo import kite_tool as kt

        client = Mock()
        client.order_history.return_value = [
            {"order_timestamp": "2026-04-21 10:00:00", "status": "OPEN"},
            {"order_timestamp": "2026-04-21 10:00:02", "status": "COMPLETE",
             "filled_quantity": 1, "average_price": 1340},
        ]
        monkeypatch.setattr(kt, "_new_client", lambda: client)

        args = parser.parse_args([
            "watch", "order", "--order-id", "123",
            "--until", 'status == "COMPLETE"',
            "--every", "0.05", "--timeout", "2",
            "--format", "json",
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = kt.cmd_watch(args)
        assert rc == 0
        snap = json.loads(buf.getvalue())["data"]["snapshot"]
        assert snap["status"] == "COMPLETE"
        assert snap["filled_quantity"] == 1

    def test_invalid_expression_rejected(self, parser, monkeypatch) -> None:
        from kite_algo import kite_tool as kt
        monkeypatch.setattr(kt, "_new_client", lambda: Mock())

        args = parser.parse_args([
            "watch", "quote", "--symbol", "NSE:RELIANCE",
            "--until", "__import__('os').system('rm -rf /')",
            "--every", "0.05", "--timeout", "1",
        ])
        rc = kt.cmd_watch(args)
        assert rc == 2  # USAGE

    def test_transient_fetch_error_does_not_abort(self, parser, monkeypatch) -> None:
        """A flaky broker call must log and retry, not blow up the watch."""
        from kite_algo import kite_tool as kt

        calls = {"n": 0}
        def sfx(symbols):
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception("transient")
            return {"NSE:RELIANCE": {"last_price": 1400, "ohlc": {}}}

        client = Mock()
        client.quote.side_effect = sfx
        monkeypatch.setattr(kt, "_new_client", lambda: client)

        args = parser.parse_args([
            "watch", "quote", "--symbol", "NSE:RELIANCE",
            "--until", "last_price > 1300",
            "--every", "0.05", "--timeout", "5",
            "--format", "json",
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = kt.cmd_watch(args)
        assert rc == 0
        data = json.loads(buf.getvalue())["data"]
        assert data["polls"] >= 3
