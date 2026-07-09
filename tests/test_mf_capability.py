"""Mutual-fund capability detection and SDK error translation."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from kite_algo.kite_tool import MFCapabilityUnavailable, _call_mf


def test_missing_mf_exchange_fails_before_endpoint_call() -> None:
    client = Mock()
    client.profile.return_value = {"exchanges": ["NSE", "NFO", "BSE"]}

    with pytest.raises(MFCapabilityUnavailable, match="does not advertise"):
        _call_mf(client, "mf_holdings")

    client.mf_holdings.assert_not_called()


def test_mf_capability_calls_requested_endpoint() -> None:
    client = Mock()
    client.profile.return_value = {"exchanges": ["NSE", "MF"]}
    client.mf_holdings.return_value = [{"tradingsymbol": "INF000000001"}]

    assert _call_mf(client, "mf_holdings") == [
        {"tradingsymbol": "INF000000001"}
    ]
    client.mf_holdings.assert_called_once_with()


def test_sdk_type_error_becomes_actionable_capability_error() -> None:
    client = Mock()
    client.profile.return_value = {"exchanges": ["MF"]}
    client.mf_orders.side_effect = TypeError(
        "attribute name must be string, not 'NoneType'"
    )

    with pytest.raises(MFCapabilityUnavailable, match="could not parse"):
        _call_mf(client, "mf_orders")
