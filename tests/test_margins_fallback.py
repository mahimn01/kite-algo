"""Tests for the margins command's resilience to Kite /user/margins 500s.

Kite's server sometimes returns HTTP 500 with body
`{"status":"error","message":"Message build error","error_type":"GeneralException"}`
for NRO, F&O-disabled, and some other account subtypes. Rather than
crashing the CLI, the command:

  1. Retries `--retries` times with exponential backoff
  2. Falls back to a derived view from holdings + positions
  3. Surfaces the degradation as a `MARGINS_UNAVAILABLE` warning
"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import pytest


def _build_general_exception(message: str = "Message build error", code: int | None = 500):
    """Import-safe constructor for kiteconnect.exceptions.GeneralException.

    We don't want the test module to hard-import kiteconnect (some dev
    environments pin a different version). Instead we fabricate a class
    with the right name — the fixture under test matches on
    `type(exc).__name__`.
    """
    class GeneralException(Exception):
        def __init__(self, msg, code=None):
            super().__init__(msg)
            self.code = code
    return GeneralException(message, code=code)


class TestIsKiteMarginsBuildError:
    def test_matches_exact_error(self) -> None:
        from kite_algo.kite_tool import _is_kite_margins_build_error
        exc = _build_general_exception("Message build error", code=500)
        assert _is_kite_margins_build_error(exc)

    def test_matches_case_insensitive(self) -> None:
        from kite_algo.kite_tool import _is_kite_margins_build_error
        exc = _build_general_exception("MESSAGE BUILD ERROR", code=500)
        assert _is_kite_margins_build_error(exc)

    def test_no_code_attribute_still_matches(self) -> None:
        from kite_algo.kite_tool import _is_kite_margins_build_error
        exc = _build_general_exception("Message build error", code=None)
        assert _is_kite_margins_build_error(exc)

    def test_wrong_class_name_rejected(self) -> None:
        from kite_algo.kite_tool import _is_kite_margins_build_error
        assert not _is_kite_margins_build_error(ValueError("Message build error"))

    def test_unrelated_message_rejected(self) -> None:
        from kite_algo.kite_tool import _is_kite_margins_build_error
        exc = _build_general_exception("Insufficient funds", code=400)
        assert not _is_kite_margins_build_error(exc)


class TestDeriveFromPortfolio:
    def test_computes_holdings_and_positions(self) -> None:
        from kite_algo.kite_tool import _derive_margins_from_portfolio
        client = MagicMock()
        client.holdings.return_value = [
            {"tradingsymbol": "A", "quantity": 10, "last_price": 100.0,
             "average_price": 80.0, "day_change": 1.5},
            {"tradingsymbol": "B", "quantity": 5, "last_price": 200.0,
             "average_price": 210.0, "day_change": -2.0},
        ]
        client.positions.return_value = {
            "net": [
                {"tradingsymbol": "X", "quantity": -100, "value": 5000.0,
                 "unrealised": -500.0, "realised": 0.0},
                {"tradingsymbol": "Y", "quantity": 50, "value": 2500.0,
                 "unrealised": 100.0, "realised": 20.0},
            ],
            "day": [
                {"tradingsymbol": "X", "m2m": -120.0},
                {"tradingsymbol": "Y", "m2m": 30.0},
            ],
        }

        out = _derive_margins_from_portfolio(client)

        assert out["equity"]["holdings_value_inr"] == 2000.0  # 10*100 + 5*200
        assert out["equity"]["holdings_invested_inr"] == 1850.0  # 10*80 + 5*210
        assert out["equity"]["holdings_unrealised_pnl_inr"] == 150.0
        assert out["equity"]["holdings_day_pnl_inr"] == 5.0  # 10*1.5 + 5*-2
        assert out["equity"]["holdings_count"] == 2
        assert out["fno"]["open_positions_count"] == 2
        assert out["fno"]["exposure_inr"] == 7500.0  # 5000+2500 absolute
        assert out["fno"]["unrealised_pnl_inr"] == -400.0
        assert out["fno"]["realised_pnl_inr"] == 20.0
        assert out["fno"]["day_m2m_inr"] == -90.0
        assert out["available_cash_inr"] is None
        assert out["used_margin_inr"] is None
        assert out["net_liquidation_inr"] is None

    def test_handles_missing_fields(self) -> None:
        from kite_algo.kite_tool import _derive_margins_from_portfolio
        client = MagicMock()
        client.holdings.return_value = [{"tradingsymbol": "Z"}]  # no prices
        client.positions.return_value = {}  # no net/day keys
        out = _derive_margins_from_portfolio(client)
        assert out["equity"]["holdings_value_inr"] == 0.0
        assert out["fno"]["open_positions_count"] == 0

    def test_ignores_zero_quantity_positions(self) -> None:
        from kite_algo.kite_tool import _derive_margins_from_portfolio
        client = MagicMock()
        client.holdings.return_value = []
        client.positions.return_value = {
            "net": [
                {"quantity": 0, "value": 0, "unrealised": 0},  # squared off
                {"quantity": -10, "value": 500, "unrealised": -50},
            ],
            "day": [],
        }
        out = _derive_margins_from_portfolio(client)
        assert out["fno"]["open_positions_count"] == 1


class TestCmdMarginsFallbackFlow:
    def _args(self, **kw) -> argparse.Namespace:
        defaults = dict(
            segment=None, format="json", cmd="margins",
            fields=None, summary=False, explain=False,
            retries=0, no_fallback=False,
        )
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    @patch("kite_algo.kite_tool._new_client")
    def test_happy_path_no_retry(self, mock_new_client) -> None:
        from kite_algo.kite_tool import cmd_margins
        client = MagicMock()
        client.margins.return_value = {"equity": {"net": 100000}}
        mock_new_client.return_value = client
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_margins(self._args())
        assert rc == 0
        assert client.margins.call_count == 1

    @patch("kite_algo.kite_tool._new_client")
    def test_retries_on_build_error_then_succeeds(self, mock_new_client) -> None:
        from kite_algo.kite_tool import cmd_margins
        client = MagicMock()
        client.margins.side_effect = [
            _build_general_exception(),
            {"equity": {"net": 50000}},
        ]
        mock_new_client.return_value = client
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_margins(self._args(retries=2))
        assert rc == 0
        assert client.margins.call_count == 2

    @patch("kite_algo.kite_tool._new_client")
    def test_falls_back_after_exhausting_retries(self, mock_new_client) -> None:
        from kite_algo.kite_tool import cmd_margins
        client = MagicMock()
        client.margins.side_effect = _build_general_exception()
        client.holdings.return_value = [
            {"quantity": 1, "last_price": 100, "average_price": 90},
        ]
        client.positions.return_value = {"net": [], "day": []}
        mock_new_client.return_value = client

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_margins(self._args(retries=0))
        assert rc == 0
        out = json.loads(buf.getvalue())
        assert out["ok"] is True
        assert out["data"]["derived_from"].startswith("holdings + positions")
        assert any(w["code"] == "MARGINS_UNAVAILABLE" for w in out["warnings"])

    @patch("kite_algo.kite_tool._new_client")
    def test_no_fallback_hard_errors(self, mock_new_client, capsys) -> None:
        from kite_algo.kite_tool import cmd_margins
        client = MagicMock()
        client.margins.side_effect = _build_general_exception()
        mock_new_client.return_value = client

        rc = cmd_margins(self._args(retries=0, no_fallback=True))
        err = capsys.readouterr().err
        assert rc == 1
        assert "Kite /user/margins" in err
        assert "500" in err

    @patch("kite_algo.kite_tool._new_client")
    def test_non_build_error_reraised(self, mock_new_client) -> None:
        from kite_algo.kite_tool import cmd_margins
        client = MagicMock()
        client.margins.side_effect = RuntimeError("network dead")
        mock_new_client.return_value = client
        with pytest.raises(RuntimeError, match="network dead"):
            cmd_margins(self._args(retries=2))

    @patch("kite_algo.kite_tool._new_client")
    def test_fallback_itself_fails_returns_1(self, mock_new_client, capsys) -> None:
        from kite_algo.kite_tool import cmd_margins
        client = MagicMock()
        client.margins.side_effect = _build_general_exception()
        client.holdings.side_effect = RuntimeError("holdings also down")
        mock_new_client.return_value = client

        rc = cmd_margins(self._args(retries=0))
        err = capsys.readouterr().err
        assert rc == 1
        assert "fallback to holdings+positions also failed" in err


class TestHealthDegradedPath:
    @patch("kite_algo.kite_tool._new_client")
    def test_health_reports_degraded_when_margins_500(self, mock_new_client) -> None:
        from kite_algo.kite_tool import cmd_health
        client = MagicMock()
        client.profile.return_value = {"user_id": "TEST1"}
        client.margins.side_effect = _build_general_exception()
        client.holdings.return_value = [
            {"quantity": 10, "last_price": 100.0, "average_price": 95.0},
        ]
        client.positions.return_value = {"net": [], "day": []}
        client.ltp.return_value = {"NSE:RELIANCE": {"last_price": 1000.0}}
        mock_new_client.return_value = client

        # cmd_health has its own internal _emit — we capture via stdout.
        import argparse, io
        args = argparse.Namespace(
            format="json", cmd="health", fields=None, summary=False, explain=False,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_health(args)
        assert rc == 0
        out = json.loads(buf.getvalue())
        margins_row = next(c for c in out["data"] if c["check"] == "margins")
        assert margins_row["ok"] is True
        assert "DEGRADED" in margins_row["detail"]
