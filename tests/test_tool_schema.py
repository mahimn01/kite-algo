"""Tests for `tools describe` JSONSchema generation.

The CLI drives agents; tool specs drive the agents that drive the CLI. If
the schema is wrong, the agent either produces invalid args or refuses to
call the tool.
"""

from __future__ import annotations

import pytest

from kite_algo.kite_tool import build_parser
from kite_algo.tool_schema import describe_tools


@pytest.fixture
def parser():
    return build_parser()


@pytest.fixture
def tools(parser):
    return describe_tools(parser)


def _by_name(tools: list[dict], name: str) -> dict:
    for t in tools:
        if t["name"] == name:
            return t
    raise AssertionError(f"tool not found: {name}")


# -----------------------------------------------------------------------------
# Overall shape
# -----------------------------------------------------------------------------

class TestOverallShape:
    def test_returns_list_of_dicts(self, tools) -> None:
        assert isinstance(tools, list)
        assert all(isinstance(t, dict) for t in tools)
        assert len(tools) >= 20

    def test_every_tool_has_required_keys(self, tools) -> None:
        for t in tools:
            assert "name" in t
            assert "description" in t
            assert "input_schema" in t
            assert "output_schema" in t
            schema = t["input_schema"]
            assert schema["type"] == "object"
            assert "properties" in schema

    def test_alphabetical_order(self, tools) -> None:
        names = [t["name"] for t in tools]
        assert names == sorted(names)

    def test_tool_names_match_cli_subcommands(self, parser, tools) -> None:
        import argparse
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        cli_names = set(sub.choices.keys())
        tool_names = {t["name"] for t in tools}
        assert cli_names == tool_names


# -----------------------------------------------------------------------------
# Specific commands
# -----------------------------------------------------------------------------

class TestPlaceSchema:
    def test_place_has_required_flags(self, tools) -> None:
        place = _by_name(tools, "place")
        req = set(place["input_schema"].get("required", []))
        # Flags marked required=True in argparse.
        expected_required = {
            "exchange", "tradingsymbol", "transaction_type",
            "order_type", "quantity", "product",
        }
        assert expected_required.issubset(req)

    def test_place_market_protection_is_number(self, tools) -> None:
        place = _by_name(tools, "place")
        mp = place["input_schema"]["properties"]["market_protection"]
        assert mp["type"] == "number"

    def test_place_exchange_has_enum(self, tools) -> None:
        place = _by_name(tools, "place")
        exch = place["input_schema"]["properties"]["exchange"]
        assert set(exch["enum"]) == {"NSE", "BSE", "NFO", "BFO", "MCX", "CDS", "BCD"}

    def test_place_variety_has_enum(self, tools) -> None:
        place = _by_name(tools, "place")
        var = place["input_schema"]["properties"]["variety"]
        assert set(var["enum"]) == {"regular", "amo", "co", "iceberg", "auction"}

    def test_place_yes_is_boolean(self, tools) -> None:
        place = _by_name(tools, "place")
        yes = place["input_schema"]["properties"]["yes"]
        assert yes["type"] == "boolean"

    def test_place_idempotency_key_is_string(self, tools) -> None:
        place = _by_name(tools, "place")
        k = place["input_schema"]["properties"]["idempotency_key"]
        assert k["type"] == "string"


class TestCommonFlags:
    def test_every_tool_has_format_flag(self, tools) -> None:
        for t in tools:
            assert "format" in t["input_schema"]["properties"]

    def test_every_tool_has_fields_flag(self, tools) -> None:
        for t in tools:
            assert "fields" in t["input_schema"]["properties"]

    def test_every_tool_has_summary_flag(self, tools) -> None:
        for t in tools:
            assert "summary" in t["input_schema"]["properties"]

    def test_every_tool_has_explain_flag(self, tools) -> None:
        for t in tools:
            assert "explain" in t["input_schema"]["properties"]

    def test_format_has_auto_choice(self, tools) -> None:
        for t in tools:
            fmt = t["input_schema"]["properties"]["format"]
            assert "auto" in fmt["enum"]


class TestOutputSchema:
    def test_envelope_shape(self, tools) -> None:
        place = _by_name(tools, "place")
        out = place["output_schema"]
        props = out["properties"]
        for k in ("ok", "cmd", "schema_version", "request_id", "data", "warnings", "meta"):
            assert k in props

    def test_cmd_const_matches_tool_name(self, tools) -> None:
        """The envelope's `cmd` field is pinned via `const` to the tool name."""
        for t in tools:
            assert t["output_schema"]["properties"]["cmd"]["const"] == t["name"]


class TestJSONSerialisable:
    def test_describe_tools_is_json(self, tools) -> None:
        import json
        text = json.dumps(tools)
        parsed = json.loads(text)
        assert isinstance(parsed, list)
        assert len(parsed) == len(tools)


# -----------------------------------------------------------------------------
# Real CLI invocation
# -----------------------------------------------------------------------------

class TestToolsDescribeCli:
    def test_subcommand_exists(self, parser) -> None:
        args = parser.parse_args(["tools-describe"])
        assert args.cmd == "tools-describe"
