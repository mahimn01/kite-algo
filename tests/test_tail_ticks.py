"""Tests for `tail-ticks` NDJSON buffer reader."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from kite_algo.kite_tool import build_parser, cmd_tail_ticks


@pytest.fixture
def parser():
    return build_parser()


def _write_ticks(path: Path, ticks: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for t in ticks:
            f.write(json.dumps(t) + "\n")


class TestTailTicks:
    def test_reads_all_lines(self, parser, tmp_path) -> None:
        p = tmp_path / "ticks.jsonl"
        _write_ticks(p, [
            {"_seq": 1, "last_price": 100, "tradingsymbol": "RELIANCE"},
            {"_seq": 2, "last_price": 101, "tradingsymbol": "RELIANCE"},
            {"_seq": 3, "last_price": 102, "tradingsymbol": "RELIANCE"},
        ])
        args = parser.parse_args(["tail-ticks", str(p)])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_tail_ticks(args)
        assert rc == 0
        lines = buf.getvalue().strip().split("\n")
        assert len(lines) == 3

    def test_missing_file_returns_1(self, parser, tmp_path) -> None:
        args = parser.parse_args(["tail-ticks", str(tmp_path / "nope.jsonl")])
        assert cmd_tail_ticks(args) == 1

    def test_from_seq_resumes(self, parser, tmp_path) -> None:
        p = tmp_path / "ticks.jsonl"
        _write_ticks(p, [
            {"_seq": i, "last_price": 100 + i, "tradingsymbol": "X"}
            for i in range(1, 6)
        ])
        args = parser.parse_args([
            "tail-ticks", str(p), "--from-seq", "3",
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_tail_ticks(args)
        seqs = [json.loads(l)["_seq"] for l in buf.getvalue().strip().split("\n")]
        assert seqs == [3, 4, 5]

    def test_limit_stops_early(self, parser, tmp_path) -> None:
        p = tmp_path / "ticks.jsonl"
        _write_ticks(p, [
            {"_seq": i, "tradingsymbol": "X"} for i in range(1, 11)
        ])
        args = parser.parse_args([
            "tail-ticks", str(p), "--limit", "3",
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_tail_ticks(args)
        lines = buf.getvalue().strip().split("\n")
        assert len(lines) == 3

    def test_symbol_filter_by_tradingsymbol(self, parser, tmp_path) -> None:
        p = tmp_path / "ticks.jsonl"
        _write_ticks(p, [
            {"_seq": 1, "tradingsymbol": "RELIANCE", "last_price": 100},
            {"_seq": 2, "tradingsymbol": "INFY", "last_price": 200},
            {"_seq": 3, "tradingsymbol": "RELIANCE", "last_price": 101},
        ])
        args = parser.parse_args([
            "tail-ticks", str(p), "--symbols", "RELIANCE",
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_tail_ticks(args)
        emitted = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        assert all(t["tradingsymbol"] == "RELIANCE" for t in emitted)
        assert len(emitted) == 2

    def test_symbol_filter_by_token(self, parser, tmp_path) -> None:
        p = tmp_path / "ticks.jsonl"
        _write_ticks(p, [
            {"_seq": 1, "instrument_token": 738561, "last_price": 100},
            {"_seq": 2, "instrument_token": 408065, "last_price": 200},
        ])
        args = parser.parse_args([
            "tail-ticks", str(p), "--symbols", "738561",
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_tail_ticks(args)
        emitted = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        assert len(emitted) == 1
        assert emitted[0]["instrument_token"] == 738561

    def test_skips_malformed_lines(self, parser, tmp_path) -> None:
        p = tmp_path / "ticks.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            '{"_seq":1,"last_price":100}\n'
            'not-json\n'
            '{"_seq":2,"last_price":101}\n'
        )
        args = parser.parse_args(["tail-ticks", str(p)])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_tail_ticks(args)
        lines = buf.getvalue().strip().split("\n")
        # Two good lines emitted; malformed skipped.
        assert len(lines) == 2

    def test_empty_file(self, parser, tmp_path) -> None:
        p = tmp_path / "ticks.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
        args = parser.parse_args(["tail-ticks", str(p)])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_tail_ticks(args)
        assert rc == 0
        assert buf.getvalue() == ""


class TestStreamBufferFlagPresent:
    """--buffer-to is on the stream parser."""

    def test_flag_exists(self, parser) -> None:
        args = parser.parse_args([
            "stream", "--symbols", "NSE:RELIANCE",
            "--buffer-to", "/tmp/ticks.jsonl",
        ])
        assert args.buffer_to == "/tmp/ticks.jsonl"
