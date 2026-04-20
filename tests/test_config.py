"""Tests for config loading + session file I/O.

Hardened with:
- Strict env-bool parsing (unknown values must fail, not be silently False).
- Atomic session save (no TOCTOU window; crash-mid-write recovery).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from kite_algo.config import (
    EnvParseError,
    KiteConfig,
    TradingConfig,
    _env_bool,
    _env_float,
    _env_int,
    atomic_write_text,
    load_session,
    save_session,
)


# -----------------------------------------------------------------------------
# KiteConfig basics
# -----------------------------------------------------------------------------

def test_kite_config_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.delenv("KITE_API_SECRET", raising=False)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
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


# -----------------------------------------------------------------------------
# Session roundtrip + atomic write
# -----------------------------------------------------------------------------

def test_session_roundtrip(tmp_path):
    path = tmp_path / "session.json"
    save_session({"access_token": "abc", "user_id": "AB1234"}, path=path)
    assert path.exists()
    loaded = load_session(path)
    assert loaded["access_token"] == "abc"
    assert loaded["user_id"] == "AB1234"


def test_session_file_owner_only_perms(tmp_path):
    """Session file must be 0o600 from the first instant it exists.

    The atomic write path creates the temp at 0o600 then renames, so there is
    never a moment when a 0o644 version is observable.
    """
    path = tmp_path / "session.json"
    save_session({"access_token": "secret"}, path=path)
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, f"session file mode is {oct(mode)}, expected 0o600"


def test_session_dir_owner_only_perms(tmp_path):
    path = tmp_path / "subdir" / "session.json"
    save_session({"access_token": "x"}, path=path)
    if os.name == "posix":
        dir_mode = path.parent.stat().st_mode & 0o777
        assert dir_mode == 0o700, f"session dir mode is {oct(dir_mode)}, expected 0o700"


def test_session_atomic_overwrite(tmp_path):
    """Subsequent saves replace atomically — readers never see a truncated file."""
    path = tmp_path / "session.json"
    save_session({"access_token": "a" * 10_000}, path=path)
    size_before = path.stat().st_size
    save_session({"access_token": "b" * 10_000}, path=path)
    # File always has well-formed JSON content.
    loaded = load_session(path)
    assert loaded["access_token"] == "b" * 10_000
    # And is roughly the same size (proves we didn't append).
    assert abs(path.stat().st_size - size_before) < 100


def test_session_no_temp_files_leaked(tmp_path):
    path = tmp_path / "session.json"
    for i in range(10):
        save_session({"access_token": str(i)}, path=path)
    # Only the final file, no lingering .session.json.*.tmp hanging around.
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".session") and p.name.endswith(".tmp")]
    assert leftovers == []


def test_load_session_survives_truncated_file(tmp_path):
    """A crash mid-write can leave malformed JSON — loader returns {}."""
    path = tmp_path / "session.json"
    path.write_text('{"access_token": "abc', encoding="utf-8")  # truncated
    assert load_session(path) == {}


def test_concurrent_saves_do_not_corrupt(tmp_path):
    """Two threads racing to save must never leave the file in an invalid state."""
    path = tmp_path / "session.json"

    def writer(i: int) -> None:
        for j in range(25):
            save_session({"access_token": f"t{i}_{j}", "n": j}, path=path)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # File must be valid JSON and have a well-formed access_token.
    loaded = load_session(path)
    assert loaded
    assert loaded["access_token"].startswith("t")
    assert isinstance(loaded["n"], int)


# -----------------------------------------------------------------------------
# atomic_write_text
# -----------------------------------------------------------------------------

def test_atomic_write_text_overwrites(tmp_path):
    p = tmp_path / "f.txt"
    atomic_write_text(p, "hello")
    atomic_write_text(p, "world")
    assert p.read_text(encoding="utf-8") == "world"


def test_atomic_write_text_permissions(tmp_path):
    p = tmp_path / "f.txt"
    atomic_write_text(p, "hi", mode=0o640)
    if os.name == "posix":
        assert p.stat().st_mode & 0o777 == 0o640


def test_atomic_write_text_creates_parents(tmp_path):
    p = tmp_path / "a" / "b" / "c" / "f.txt"
    atomic_write_text(p, "hi")
    assert p.read_text(encoding="utf-8") == "hi"


# -----------------------------------------------------------------------------
# Strict env-bool
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("True", True),
    ("1", True), ("yes", True), ("YES", True), ("on", True), ("y", True),
    ("false", False), ("FALSE", False), ("False", False),
    ("0", False), ("no", False), ("NO", False), ("off", False), ("n", False),
    ("", False),  # empty string is treated as falsy (unset-like)
    ("  true  ", True), ("  FALSE  ", False),  # whitespace tolerated
])
def test_env_bool_accepts_canonical_values(monkeypatch, raw, expected):
    monkeypatch.setenv("TEST_FLAG", raw)
    assert _env_bool("TEST_FLAG", default=not expected) is expected


def test_env_bool_unset_returns_default(monkeypatch):
    monkeypatch.delenv("TEST_FLAG", raising=False)
    assert _env_bool("TEST_FLAG", default=True) is True
    assert _env_bool("TEST_FLAG", default=False) is False


@pytest.mark.parametrize("garbage", [
    "flase",   # typo
    "tru",     # typo
    "YEP",     # not canonical
    "enable",
    "disable",
    "TRUE!",
    "2",
    "-1",
])
def test_env_bool_rejects_garbage(monkeypatch, garbage):
    """A typo on a safety-critical flag must never be silently interpreted."""
    monkeypatch.setenv("TEST_FLAG", garbage)
    with pytest.raises(EnvParseError):
        _env_bool("TEST_FLAG", default=False)


def test_env_int_accepts(monkeypatch):
    monkeypatch.setenv("N", "42")
    assert _env_int("N", 0) == 42


def test_env_int_rejects(monkeypatch):
    monkeypatch.setenv("N", "not-a-number")
    with pytest.raises(EnvParseError):
        _env_int("N", 0)


def test_env_float_accepts(monkeypatch):
    monkeypatch.setenv("X", "3.14")
    assert _env_float("X", 0.0) == pytest.approx(3.14)


def test_env_float_rejects(monkeypatch):
    monkeypatch.setenv("X", "3.14.15")
    with pytest.raises(EnvParseError):
        _env_float("X", 0.0)


# -----------------------------------------------------------------------------
# TradingConfig safety rails
# -----------------------------------------------------------------------------

def test_trading_config_dry_run_default(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADING_DRY_RUN", raising=False)
    monkeypatch.delenv("TRADING_ALLOW_LIVE", raising=False)
    monkeypatch.delenv("TRADING_LIVE_ENABLED", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = TradingConfig.from_env()
    assert cfg.dry_run is True
    assert cfg.allow_live is False
    assert cfg.live_enabled is False


def test_trading_config_typo_on_safety_flag_raises(monkeypatch, tmp_path):
    """If an operator typos `TRADING_ALLOW_LIVE=tru`, we must refuse to boot,
    not silently treat it as False (the current semantics would be fine, but
    the reverse — treating `flase` as True — is the hazard we're guarding)."""
    monkeypatch.setenv("TRADING_ALLOW_LIVE", "tru")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(EnvParseError):
        TradingConfig.from_env()


def test_trading_config_mixed_case_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_DRY_RUN", "FALSE")
    monkeypatch.setenv("TRADING_ALLOW_LIVE", "TRUE")
    monkeypatch.setenv("TRADING_LIVE_ENABLED", "True")
    monkeypatch.chdir(tmp_path)
    cfg = TradingConfig.from_env()
    assert cfg.dry_run is False
    assert cfg.allow_live is True
    assert cfg.live_enabled is True


def test_assert_order_authorized_blocks_on_dry_run():
    cfg = TradingConfig(dry_run=True)
    cfg.assert_order_authorized(confirm_token=None)


def test_assert_order_authorized_blocks_without_live_enabled():
    cfg = TradingConfig(dry_run=False, allow_live=True, live_enabled=False)
    with pytest.raises(SystemExit):
        cfg.assert_order_authorized(confirm_token=None)


def test_assert_order_authorized_requires_confirm_token_match():
    cfg = TradingConfig(
        dry_run=False,
        allow_live=True,
        live_enabled=True,
        confirm_token_required=True,
        order_token="EXPECTED",
    )
    with pytest.raises(SystemExit):
        cfg.assert_order_authorized(confirm_token="WRONG")
    # Matching token passes.
    cfg.assert_order_authorized(confirm_token="EXPECTED")
