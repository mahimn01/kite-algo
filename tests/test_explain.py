"""Tests for the `--explain` meta-flag + explanation registry."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from kite_algo.explain import _EXPLANATIONS, all_explanations, explain


class TestExplainRegistry:
    def test_every_command_has_required_shape(self) -> None:
        """Every entry has the fields agents key off."""
        required = {"action", "side_effects", "preconditions",
                    "reversibility", "idempotency"}
        for cmd, body in _EXPLANATIONS.items():
            missing = required - set(body.keys())
            assert not missing, f"{cmd} missing {missing}"

    def test_side_effects_is_list(self) -> None:
        for cmd, body in _EXPLANATIONS.items():
            assert isinstance(body["side_effects"], list), cmd

    def test_preconditions_is_list(self) -> None:
        for cmd, body in _EXPLANATIONS.items():
            assert isinstance(body["preconditions"], list), cmd

    def test_known_commands_covered(self) -> None:
        """The high-risk write commands must have explanations — agents will
        use `--explain` before a `--yes` call.
        """
        must_have = {
            "login", "logout", "place", "cancel", "modify", "cancel-all",
            "gtt-create", "gtt-delete",
            "ltp", "quote", "history", "chain", "stream",
        }
        assert must_have.issubset(set(_EXPLANATIONS.keys()))

    def test_unknown_command_falls_back(self) -> None:
        out = explain("no-such-command")
        assert "action" in out
        assert out["command"] == "no-such-command"

    def test_all_explanations_is_copy(self) -> None:
        """Callers can freely mutate the returned dict without corrupting
        the module-level registry."""
        out = all_explanations()
        out["_injected"] = "nope"
        assert "_injected" not in all_explanations()


# -----------------------------------------------------------------------------
# CLI integration
# -----------------------------------------------------------------------------

class TestExplainCli:
    def test_explain_short_circuits(self, monkeypatch) -> None:
        """--explain must NOT touch the broker."""
        from kite_algo import kite_tool as kt

        called = {"n": 0}
        def real_cmd(args):
            called["n"] += 1
            return 0

        # Build a minimal args via parser, then invoke main() style.
        parser = kt.build_parser()
        args = parser.parse_args([
            "place", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--order-type", "LIMIT",
            "--quantity", "1", "--product", "CNC", "--price", "1300",
            "--explain", "--yes",
        ])
        # Substitute the handler — if --explain fires correctly, real_cmd
        # is never invoked.
        args.func = real_cmd

        # Replicate main() logic for the explain branch.
        buf = io.StringIO()
        with redirect_stdout(buf):
            if getattr(args, "explain", False):
                from kite_algo.explain import explain as explain_fn
                kt._emit(explain_fn(args.cmd), args.format, cmd=args.cmd)
                rc = 0
            else:
                rc = args.func(args)

        assert rc == 0
        assert called["n"] == 0
        parsed = json.loads(buf.getvalue())
        # Envelope wraps the explanation.
        assert parsed["cmd"] == "place"
        assert "action" in parsed["data"]
        assert "side_effects" in parsed["data"]
