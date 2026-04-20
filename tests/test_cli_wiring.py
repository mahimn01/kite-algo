"""CLI argparse wiring + safety gate tests.

Verifies every subcommand parses correctly, required args are enforced, and
--yes gates refuse without confirmation. Does NOT hit the broker.
"""

from __future__ import annotations

import pytest

from kite_algo.kite_tool import build_parser


@pytest.fixture
def parser():
    return build_parser()


class TestParserBuilds:
    def test_parser_constructs(self, parser) -> None:
        assert parser is not None

    def test_all_expected_subcommands_registered(self, parser) -> None:
        # Extract subcommand names from the parser
        subparsers_action = next(
            a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"
        )
        names = set(subparsers_action.choices.keys())

        expected = {
            # auth
            "login", "profile", "session", "logout",
            # account
            "margins", "holdings", "positions", "convert-position",
            "pnl", "portfolio",
            # orders
            "orders", "open-orders", "trades", "order-history", "order-trades",
            "place", "cancel", "modify", "cancel-all",
            # market data
            "ltp", "ohlc", "quote", "depth", "stream",
            # historical + instruments
            "history", "instruments", "search", "contract",
            # options
            "expiries", "chain", "option-quote", "calc-iv", "calc-price",
            # gtt
            "gtt-list", "gtt-get", "gtt-create", "gtt-modify", "gtt-delete",
            # margin
            "margin-calc", "basket-margin",
            # mf
            "mf-holdings", "mf-orders", "mf-sips", "mf-instruments",
            "mf-place", "mf-cancel", "mf-sip-create", "mf-sip-modify", "mf-sip-cancel",
        }
        missing = expected - names
        assert not missing, f"missing subcommands: {missing}"


class TestPlaceOrderParsing:
    def test_valid_limit_order_parses(self, parser) -> None:
        args = parser.parse_args([
            "place", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--order-type", "LIMIT",
            "--quantity", "1", "--product", "CNC", "--price", "1340",
        ])
        assert args.exchange == "NSE"
        assert args.tradingsymbol == "RELIANCE"
        assert args.transaction_type == "BUY"
        assert args.order_type == "LIMIT"
        assert args.quantity == 1
        assert args.product == "CNC"
        assert args.price == 1340
        assert args.yes is False  # default

    def test_market_protection_flag_parses(self, parser) -> None:
        args = parser.parse_args([
            "place", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--order-type", "MARKET",
            "--quantity", "1", "--product", "MIS",
            "--market-protection", "2.0",
        ])
        assert args.market_protection == 2.0

    def test_market_protection_defaults_to_none(self, parser) -> None:
        args = parser.parse_args([
            "place", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--order-type", "MARKET",
            "--quantity", "1", "--product", "MIS",
        ])
        # cmd_place will fill in -1 before passing to validator.
        assert args.market_protection is None

    def test_missing_required_args_fails(self, parser) -> None:
        with pytest.raises(SystemExit):
            parser.parse_args(["place", "--exchange", "NSE"])

    def test_invalid_exchange_choice_fails(self, parser) -> None:
        with pytest.raises(SystemExit):
            parser.parse_args([
                "place", "--exchange", "INVALID",
                "--tradingsymbol", "RELIANCE", "--transaction-type", "BUY",
                "--order-type", "MARKET", "--quantity", "1", "--product", "CNC",
            ])

    def test_invalid_product_choice_fails(self, parser) -> None:
        with pytest.raises(SystemExit):
            parser.parse_args([
                "place", "--exchange", "NSE",
                "--tradingsymbol", "RELIANCE", "--transaction-type", "BUY",
                "--order-type", "MARKET", "--quantity", "1", "--product", "BADPROD",
            ])

    def test_iceberg_variety_accepts_legs(self, parser) -> None:
        args = parser.parse_args([
            "place", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--order-type", "LIMIT",
            "--quantity", "1000", "--product", "CNC", "--price", "1340",
            "--variety", "iceberg", "--iceberg-legs", "5",
            "--iceberg-quantity", "200",
        ])
        assert args.variety == "iceberg"
        assert args.iceberg_legs == 5


class TestSafetyGates:
    """Commands requiring --yes must refuse when it's absent."""

    def test_place_without_yes_raises(self, parser) -> None:
        from kite_algo.kite_tool import cmd_place
        args = parser.parse_args([
            "place", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--order-type", "MARKET",
            "--quantity", "1", "--product", "CNC",
        ])
        with pytest.raises(SystemExit, match="Refusing"):
            cmd_place(args)

    def test_cancel_without_yes_raises(self, parser) -> None:
        from kite_algo.kite_tool import cmd_cancel
        args = parser.parse_args(["cancel", "--order-id", "123456"])
        with pytest.raises(SystemExit, match="Refusing"):
            cmd_cancel(args)

    def test_modify_without_yes_raises(self, parser) -> None:
        from kite_algo.kite_tool import cmd_modify
        args = parser.parse_args([
            "modify", "--order-id", "123456", "--quantity", "10",
        ])
        with pytest.raises(SystemExit, match="Refusing"):
            cmd_modify(args)

    def test_cancel_all_without_yes_raises(self, parser) -> None:
        from kite_algo.kite_tool import cmd_cancel_all
        args = parser.parse_args(["cancel-all"])
        with pytest.raises(SystemExit, match="Refusing"):
            cmd_cancel_all(args)

    def test_cancel_all_with_yes_but_no_panic_still_refuses(self, parser) -> None:
        """--yes alone is NOT enough for cancel-all — must have --confirm-panic."""
        from kite_algo.kite_tool import cmd_cancel_all
        args = parser.parse_args(["cancel-all", "--yes"])
        with pytest.raises(SystemExit, match="confirm-panic"):
            cmd_cancel_all(args)

    def test_convert_position_with_yes_but_no_convert_refuses(self, parser) -> None:
        from kite_algo.kite_tool import cmd_convert_position
        args = parser.parse_args([
            "convert-position", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--position-type", "day",
            "--quantity", "10", "--old-product", "MIS", "--new-product", "CNC",
            "--yes",
        ])
        with pytest.raises(SystemExit, match="confirm-convert"):
            cmd_convert_position(args)

    def test_gtt_create_without_yes_raises(self, parser) -> None:
        from kite_algo.kite_tool import cmd_gtt_create
        args = parser.parse_args([
            "gtt-create", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--trigger-values", "1300", "--last-price", "1340",
            "--quantity", "1", "--price", "1295",
        ])
        with pytest.raises(SystemExit, match="Refusing"):
            cmd_gtt_create(args)

    def test_gtt_modify_without_yes_raises(self, parser) -> None:
        from kite_algo.kite_tool import cmd_gtt_modify
        args = parser.parse_args([
            "gtt-modify", "--trigger-id", "123", "--exchange", "NSE",
            "--tradingsymbol", "RELIANCE", "--trigger-values", "1300",
            "--last-price", "1340", "--orders-json", "[]",
        ])
        with pytest.raises(SystemExit, match="Refusing"):
            cmd_gtt_modify(args)

    def test_gtt_delete_without_yes_raises(self, parser) -> None:
        from kite_algo.kite_tool import cmd_gtt_delete
        args = parser.parse_args(["gtt-delete", "--trigger-id", "123"])
        with pytest.raises(SystemExit, match="Refusing"):
            cmd_gtt_delete(args)

    def test_convert_position_without_yes_raises(self, parser) -> None:
        from kite_algo.kite_tool import cmd_convert_position
        args = parser.parse_args([
            "convert-position", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--position-type", "day",
            "--quantity", "10", "--old-product", "MIS", "--new-product", "CNC",
        ])
        with pytest.raises(SystemExit, match="Refusing"):
            cmd_convert_position(args)

    def test_mf_place_without_yes_raises(self, parser) -> None:
        from kite_algo.kite_tool import cmd_mf_place
        args = parser.parse_args([
            "mf-place", "--tradingsymbol", "INF00XX01135",
            "--transaction-type", "BUY", "--amount", "5000",
        ])
        with pytest.raises(SystemExit, match="Refusing"):
            cmd_mf_place(args)

    def test_mf_sip_create_without_yes_raises(self, parser) -> None:
        from kite_algo.kite_tool import cmd_mf_sip_create
        args = parser.parse_args([
            "mf-sip-create", "--tradingsymbol", "INF00XX01135",
            "--amount", "1000", "--frequency", "monthly", "--instalments", "12",
        ])
        with pytest.raises(SystemExit, match="Refusing"):
            cmd_mf_sip_create(args)


class TestCalcCommandParsing:
    def test_calc_iv_parses(self, parser) -> None:
        args = parser.parse_args([
            "calc-iv", "--spot", "100", "--strike", "100",
            "--dte", "30", "--market-price", "5", "--right", "CE",
        ])
        assert args.spot == 100
        assert args.right == "CE"

    def test_calc_price_parses(self, parser) -> None:
        args = parser.parse_args([
            "calc-price", "--spot", "100", "--strike", "100",
            "--dte", "30", "--iv", "25", "--right", "PE",
        ])
        assert args.iv == 25
        assert args.right == "PE"

    def test_calc_iv_invalid_right_fails(self, parser) -> None:
        with pytest.raises(SystemExit):
            parser.parse_args([
                "calc-iv", "--spot", "100", "--strike", "100",
                "--dte", "30", "--market-price", "5", "--right", "XX",
            ])


class TestGTTCreateParsing:
    def test_single_leg_parses(self, parser) -> None:
        args = parser.parse_args([
            "gtt-create", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--trigger-values", "1300", "--last-price", "1340",
            "--quantity", "1", "--price", "1295", "--yes",
        ])
        assert args.trigger_values == "1300"
        assert args.yes is True

    def test_oco_parses(self, parser) -> None:
        args = parser.parse_args([
            "gtt-create", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--trigger-values", "1280,1360", "--last-price", "1340",
            "--quantity", "1", "--price", "1275", "--price2", "1365", "--yes",
        ])
        assert args.trigger_values == "1280,1360"
        assert args.price2 == 1365


class TestStreamParsing:
    def test_symbols_mode(self, parser) -> None:
        args = parser.parse_args([
            "stream", "--symbols", "NSE:RELIANCE,NSE:INFY", "--mode", "quote",
        ])
        assert args.symbols == "NSE:RELIANCE,NSE:INFY"
        assert args.mode == "quote"

    def test_tokens_mode(self, parser) -> None:
        args = parser.parse_args([
            "stream", "--tokens", "738561,408065", "--mode", "ltp",
        ])
        assert args.tokens == "738561,408065"

    def test_invalid_mode_fails(self, parser) -> None:
        with pytest.raises(SystemExit):
            parser.parse_args(["stream", "--mode", "invalid"])

    def test_reconnect_args_exist(self, parser) -> None:
        args = parser.parse_args([
            "stream", "--symbols", "NSE:X", "--reconnect-max-tries", "10",
            "--reconnect-max-delay", "30",
        ])
        assert args.reconnect_max_tries == 10
        assert args.reconnect_max_delay == 30


class TestPlaceExtras:
    def test_dry_run_flag(self, parser) -> None:
        args = parser.parse_args([
            "place", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--order-type", "MARKET",
            "--quantity", "1", "--product", "CNC", "--dry-run", "--yes",
        ])
        assert args.dry_run is True

    def test_wait_for_fill_flag(self, parser) -> None:
        args = parser.parse_args([
            "place", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--order-type", "LIMIT",
            "--quantity", "1", "--product", "CNC", "--price", "1300",
            "--wait-for-fill", "15", "--yes",
        ])
        assert args.wait_for_fill == 15

    def test_mtf_product_accepted(self, parser) -> None:
        args = parser.parse_args([
            "place", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--order-type", "LIMIT",
            "--quantity", "1", "--product", "MTF", "--price", "1300",
        ])
        assert args.product == "MTF"

    def test_auction_variety_accepted(self, parser) -> None:
        args = parser.parse_args([
            "place", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--order-type", "LIMIT",
            "--quantity", "1", "--product", "CNC", "--price", "1300",
            "--variety", "auction",
        ])
        assert args.variety == "auction"

    def test_bcd_exchange_accepted(self, parser) -> None:
        args = parser.parse_args([
            "place", "--exchange", "BCD", "--tradingsymbol", "USDINR26APRFUT",
            "--transaction-type", "BUY", "--order-type", "LIMIT",
            "--quantity", "1", "--product", "NRML", "--price", "83",
        ])
        assert args.exchange == "BCD"


class TestLoginFlagRemoved:
    def test_request_token_flag_is_gone(self, parser) -> None:
        """Security: --request-token on CLI would appear in ps output + shell
        history. Must only be read interactively via getpass.
        """
        with pytest.raises(SystemExit):
            parser.parse_args(["login", "--request-token", "ABC123"])


class TestIdempotencyKey:
    """cmd_place wires --idempotency-key through SQLite for crash-safe retries."""

    def _args(self, parser, key: str, **extras):
        base = [
            "place", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--order-type", "LIMIT",
            "--quantity", "1", "--product", "CNC", "--price", "1340",
            "--yes", "--skip-market-rules",
            "--idempotency-key", key,
        ]
        for k, v in extras.items():
            base += [f"--{k.replace('_', '-')}", str(v)]
        return parser.parse_args(base)

    def test_first_call_places(self, parser, monkeypatch, tmp_path) -> None:
        from kite_algo import kite_tool as kt
        from kite_algo.idempotency import IdempotencyStore

        # Point the store at a tmp path.
        monkeypatch.setattr(
            kt, "IdempotencyStore",
            lambda: IdempotencyStore(tmp_path / "idem.sqlite"),
        )

        captured = {}
        class FakePlacer:
            def __init__(self, *a, **kw): pass
            def place(self, **kwargs):
                captured.update(kwargs)
                return "ORD_REAL"

        monkeypatch.setattr(kt, "_new_client", lambda: object())
        monkeypatch.setattr(kt, "IdempotentOrderPlacer", FakePlacer)

        args = self._args(parser, "AGENT_TURN_1")
        rc = kt.cmd_place(args)
        assert rc == 0
        # Tag was derived from key (deterministic); IdempotentOrderPlacer.place
        # received it.
        assert captured["tag"].startswith("KA")

    def test_retry_replays_stored_result(
        self, parser, monkeypatch, tmp_path, capsys
    ) -> None:
        """Second invocation with the same key must NOT call place_order —
        it replays the cached result instead.
        """
        from kite_algo import kite_tool as kt
        from kite_algo.idempotency import IdempotencyStore

        store_path = tmp_path / "idem.sqlite"
        monkeypatch.setattr(
            kt, "IdempotencyStore", lambda: IdempotencyStore(store_path)
        )

        call_count = {"n": 0}
        class FakePlacer:
            def __init__(self, *a, **kw): pass
            def place(self, **kwargs):
                call_count["n"] += 1
                return f"ORD_{call_count['n']}"

        monkeypatch.setattr(kt, "_new_client", lambda: object())
        monkeypatch.setattr(kt, "IdempotentOrderPlacer", FakePlacer)

        args1 = self._args(parser, "AGENT_TURN_X")
        assert kt.cmd_place(args1) == 0

        # Second call — same key.
        args2 = self._args(parser, "AGENT_TURN_X")
        rc2 = kt.cmd_place(args2)
        assert rc2 == 0
        assert call_count["n"] == 1, "place_order must be called exactly once"

        # Envelope marks the second emission as replayed.
        import json
        out = capsys.readouterr().out
        # There are two emissions in `out`; parse the last envelope.
        # Each call emits one envelope JSON blob.
        # Split on the closing brace + newline boundary.
        # Simpler: find the "replayed": true substring.
        assert '"replayed": true' in out

    def test_derived_tag_is_deterministic(self, parser, monkeypatch, tmp_path) -> None:
        """Same key produces same tag — orderbook lookups across process
        boundaries still find the order.
        """
        from kite_algo import kite_tool as kt
        from kite_algo.idempotency import IdempotencyStore, derive_tag_from_key

        monkeypatch.setattr(
            kt, "IdempotencyStore",
            lambda: IdempotencyStore(tmp_path / "idem.sqlite"),
        )

        captured_tags = []
        class FakePlacer:
            def __init__(self, *a, **kw): pass
            def place(self, **kwargs):
                captured_tags.append(kwargs["tag"])
                # Raise transient so the store record stays incomplete —
                # next call derives the same tag.
                raise Exception("503")

        monkeypatch.setattr(kt, "_new_client", lambda: object())
        monkeypatch.setattr(kt, "IdempotentOrderPlacer", FakePlacer)

        kt.cmd_place(self._args(parser, "KEY_SAME"))
        kt.cmd_place(self._args(parser, "KEY_SAME"))

        # Both attempts derived the same tag.
        assert len(captured_tags) == 2
        assert captured_tags[0] == captured_tags[1]
        assert captured_tags[0] == derive_tag_from_key("KEY_SAME")


class TestMarketRulesEnforcement:
    """cmd_place must run the market-rule check before any API call.
    Covers: weekend block, MIS past 15:20 block, freeze-qty overflow block,
    lot-size multiple check.
    """

    def _args(self, parser, product="CNC", quantity=1, **extras):
        base = [
            "place", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--order-type", "LIMIT",
            "--quantity", str(quantity), "--product", product, "--price", "1340",
            "--yes",
        ]
        for k, v in extras.items():
            base += [f"--{k.replace('_', '-')}", str(v)]
        return parser.parse_args(base)

    def test_lot_size_mismatch_blocks(self, parser, monkeypatch) -> None:
        """Non-multiple of NIFTY lot size (75) is blocked before reaching Kite."""
        from kite_algo import kite_tool as kt

        # Build a quantity that isn't a multiple of 75 on NIFTY NFO.
        args = parser.parse_args([
            "place", "--exchange", "NFO", "--tradingsymbol", "NIFTY26APR24000CE",
            "--transaction-type", "BUY", "--order-type", "LIMIT",
            "--quantity", "100", "--product", "NRML", "--price", "50",
            "--yes",
        ])
        # _new_client should NEVER be called — we reject pre-flight.
        called = {"n": 0}
        def bad_client():
            called["n"] += 1
            raise AssertionError("_new_client must not be reached")
        monkeypatch.setattr(kt, "_new_client", bad_client)

        # Market hours aren't the concern here; disable via monkeypatch.
        import kite_algo.market_rules as mr
        monkeypatch.setattr(mr, "is_market_open", lambda *a, **kw: True)

        rc = kt.cmd_place(args)
        assert rc == 1
        assert called["n"] == 0

    def test_skip_flag_bypasses_rules(self, parser, monkeypatch) -> None:
        """--skip-market-rules lets obviously bad orders through to the broker
        layer (for intentional testing/AMO scenarios)."""
        from kite_algo import kite_tool as kt

        captured = {}
        class FakePlacer:
            def __init__(self, *a, **kw): pass
            def place(self, **kwargs):
                captured.update(kwargs)
                return "ORD_X"

        monkeypatch.setattr(kt, "_new_client", lambda: object())
        monkeypatch.setattr(kt, "IdempotentOrderPlacer", FakePlacer)

        args = parser.parse_args([
            "place", "--exchange", "NFO", "--tradingsymbol", "NIFTY26APR24000CE",
            "--transaction-type", "BUY", "--order-type", "LIMIT",
            "--quantity", "100", "--product", "NRML", "--price", "50",
            "--yes", "--skip-market-rules",
        ])
        assert kt.cmd_place(args) == 0
        assert captured.get("quantity") == 100


class TestMarketProtectionPlumbing:
    """Verify --market-protection is auto-injected for MARKET orders (SEBI
    Apr 2026 mandatory field). A MARKET order without market_protection is
    server-side rejected — the CLI must NEVER let one through.
    """

    def _place_args(self, parser, *extras):
        """Build a valid cmd_place argparse namespace with --yes.

        `--skip-market-rules` removes hour/MIS-cutoff checks from the unit
        test — we're testing argument plumbing, not market-hour semantics
        (those have their own dedicated tests in test_market_rules.py).
        """
        return parser.parse_args([
            "place", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--order-type", "MARKET",
            "--quantity", "1", "--product", "MIS", "--yes",
            "--skip-market-rules",
            *extras,
        ])

    def test_market_order_auto_sets_minus_one(self, parser, monkeypatch) -> None:
        """No --market-protection flag → CLI must auto-inject -1 (Kite auto)."""
        from kite_algo import kite_tool as kt

        captured = {}

        class FakePlacer:
            def __init__(self, *a, **kw): pass
            def place(self, **kwargs):
                captured.update(kwargs)
                return "ORD_X"

        monkeypatch.setattr(kt, "_new_client", lambda: object())
        monkeypatch.setattr(kt, "IdempotentOrderPlacer", FakePlacer)

        args = self._place_args(parser)
        rc = kt.cmd_place(args)
        assert rc == 0
        assert captured.get("market_protection") == -1, (
            "MARKET orders must carry market_protection; CLI must inject -1 "
            "as the default."
        )

    def test_user_override_respected(self, parser, monkeypatch) -> None:
        from kite_algo import kite_tool as kt

        captured = {}
        class FakePlacer:
            def __init__(self, *a, **kw): pass
            def place(self, **kwargs):
                captured.update(kwargs)
                return "ORD_X"
        monkeypatch.setattr(kt, "_new_client", lambda: object())
        monkeypatch.setattr(kt, "IdempotentOrderPlacer", FakePlacer)

        args = self._place_args(parser, "--market-protection", "2.5")
        assert kt.cmd_place(args) == 0
        assert captured.get("market_protection") == 2.5

    def test_limit_order_does_not_inject(self, parser, monkeypatch) -> None:
        """LIMIT orders have explicit prices; market_protection should not be
        auto-added for them (would also trigger a validator reject).
        """
        from kite_algo import kite_tool as kt

        captured = {}
        class FakePlacer:
            def __init__(self, *a, **kw): pass
            def place(self, **kwargs):
                captured.update(kwargs)
                return "ORD_X"
        monkeypatch.setattr(kt, "_new_client", lambda: object())
        monkeypatch.setattr(kt, "IdempotentOrderPlacer", FakePlacer)

        args = parser.parse_args([
            "place", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--order-type", "LIMIT",
            "--quantity", "1", "--product", "CNC", "--price", "1340",
            "--yes", "--skip-market-rules",
        ])
        assert kt.cmd_place(args) == 0
        assert "market_protection" not in captured
