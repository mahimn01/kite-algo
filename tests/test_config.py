"""Tests for config loading + session file I/O."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kite_algo.config import (
    KiteConfig,
    TradingConfig,
    load_session,
    save_session,
)


def test_kite_config_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.delenv("KITE_API_SECRET", raising=False)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)  # so load_session looks at an empty data/
    cfg = KiteConfig.from_env()
    assert cfg.api_key == ""
    assert cfg.api_secret == ""
    assert cfg.access_token == ""
    assert "NSE" in cfg.exchanges


def test_kite_config_requires_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.delenv("KITE_API_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = KiteConfig.from_env()
    with pytest.raises(SystemExit):
        cfg.require_credentials()


def test_kite_config_require_session_blocks_when_no_token(monkeypatch, tmp_path):
    monkeypatch.setenv("KITE_API_KEY", "test_key")
    monkeypatch.setenv("KITE_API_SECRET", "test_secret")
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = KiteConfig.from_env()
    with pytest.raises(SystemExit):
        cfg.require_session()


def test_session_roundtrip(tmp_path):
    path = tmp_path / "session.json"
    save_session({"access_token": "abc", "user_id": "AB1234"}, path=path)
    assert path.exists()
    loaded = load_session(path)
    assert loaded["access_token"] == "abc"
    assert loaded["user_id"] == "AB1234"


def test_trading_config_dry_run_default(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADING_DRY_RUN", raising=False)
    monkeypatch.delenv("TRADING_ALLOW_LIVE", raising=False)
    monkeypatch.delenv("TRADING_LIVE_ENABLED", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = TradingConfig.from_env()
    assert cfg.dry_run is True
    assert cfg.allow_live is False
    assert cfg.live_enabled is False


def test_assert_order_authorized_blocks_on_dry_run():
    cfg = TradingConfig(dry_run=True)
    # Short-circuits silently in dry_run
    cfg.assert_order_authorized(confirm_token=None)


def test_assert_order_authorized_blocks_without_live_enabled():
    cfg = TradingConfig(dry_run=False, allow_live=True, live_enabled=False)
    with pytest.raises(SystemExit):
        cfg.assert_order_authorized(confirm_token=None)
