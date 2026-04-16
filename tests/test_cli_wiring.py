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
