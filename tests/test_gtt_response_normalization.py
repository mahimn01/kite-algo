"""Regression tests for Kite SDK GTT mutation response normalization."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from kite_algo import kite_tool as kt


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"trigger_id": 326995853}, 326995853),
        (326995853, 326995853),
        ("326995853", 326995853),
    ],
)
def test_normalize_gtt_trigger_id(response, expected: int) -> None:
    assert kt._normalize_gtt_trigger_id(response) == expected


@pytest.mark.parametrize(
    "response",
    [None, {}, {"trigger_id": None}, {"trigger_id": True}, {"trigger_id": 0}],
)
def test_normalize_gtt_trigger_id_rejects_invalid_values(response) -> None:
    with pytest.raises(ValueError):
        kt._normalize_gtt_trigger_id(response)


def _bypass_write_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kt, "_require_yes", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(kt, "_require_not_halted", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(kt, "_require_write_authorized", lambda *_args, **_kwargs: None)


def test_gtt_create_emits_and_audits_scalar_trigger_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Mock()
    client.GTT_TYPE_SINGLE = "single"
    client.place_gtt.return_value = {"trigger_id": 326995853}
    monkeypatch.setattr(kt, "_new_client", lambda: client)
    _bypass_write_guards(monkeypatch)

    emitted: list[dict] = []
    monkeypatch.setattr(
        kt,
        "_emit",
        lambda data, *_args, **_kwargs: emitted.append(data),
    )
    args = kt.build_parser().parse_args([
        "gtt-create",
        "--exchange", "NFO",
        "--tradingsymbol", "NIFTY26JUL25200CE",
        "--transaction-type", "SELL",
        "--trigger-values", "22",
        "--last-price", "16.65",
        "--quantity", "65",
        "--order-type", "LIMIT",
        "--product", "NRML",
        "--price", "21.70",
        "--yes",
    ])

    assert kt.cmd_gtt_create(args) == 0
    assert emitted == [{"trigger_id": 326995853}]
    assert args._audit_extra["gtt_trigger_id"] == 326995853
    assert args._audit_extra["created"] is True


def test_gtt_modify_normalizes_sdk_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Mock()
    client.GTT_TYPE_SINGLE = "single"
    client.modify_gtt.return_value = {"trigger_id": 326995853}
    monkeypatch.setattr(kt, "_new_client", lambda: client)
    _bypass_write_guards(monkeypatch)

    emitted: list[dict] = []
    monkeypatch.setattr(
        kt,
        "_emit",
        lambda data, *_args, **_kwargs: emitted.append(data),
    )
    args = kt.build_parser().parse_args([
        "gtt-modify",
        "--trigger-id", "326995853",
        "--exchange", "NFO",
        "--tradingsymbol", "NIFTY26JUL25200CE",
        "--trigger-values", "22",
        "--last-price", "16.65",
        "--orders-json",
        '[{"exchange":"NFO","tradingsymbol":"NIFTY26JUL25200CE",'
        '"transaction_type":"SELL","quantity":65,"order_type":"LIMIT",'
        '"product":"NRML","price":21.7}]',
        "--yes",
    ])

    assert kt.cmd_gtt_modify(args) == 0
    assert emitted == [{"trigger_id": 326995853}]
    assert args._audit_extra["gtt_trigger_id"] == 326995853
    assert args._audit_extra["modified"] is True
