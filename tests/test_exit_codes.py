"""Tests for the exit-code taxonomy + exception classifier.

Agents key off exit codes before reading any output — the mapping must be
stable and exhaustive. Any new Kite exception class we start handling has to
get an entry here.
"""

from __future__ import annotations

import pytest

from kite_algo import exit_codes as ec
from kite_algo.exit_codes import (
    ClassifiedError,
    classify_exception,
    exit_code_name,
)


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

class TestConstants:
    def test_ok_is_zero(self) -> None:
        assert ec.OK == 0

    def test_sigint_is_130(self) -> None:
        """Standard Unix convention 128+2=130."""
        assert ec.SIGINT == 130

    def test_timeout_is_124(self) -> None:
        """Matches coreutils `timeout(1)` convention."""
        assert ec.TIMEOUT == 124

    def test_all_codes_unique(self) -> None:
        """No two codes collide — otherwise a classifier result is ambiguous."""
        codes = [
            ec.OK, ec.GENERIC, ec.USAGE, ec.VALIDATION, ec.HARD_REJECT,
            ec.AUTH, ec.PERMISSION, ec.LEASE, ec.HALTED, ec.OUT_OF_WINDOW,
            ec.MARKET_CLOSED, ec.UNAVAILABLE, ec.INTERNAL, ec.TRANSIENT,
            ec.TIMEOUT, ec.SIGINT,
        ]
        assert len(codes) == len(set(codes))

    def test_all_codes_in_valid_exit_range(self) -> None:
        """Exit codes are 8-bit on POSIX — codes >255 get truncated."""
        for c in ec.ALL_CODES:
            assert 0 <= c <= 255

    def test_all_codes_frozenset_matches(self) -> None:
        """ALL_CODES should track every numeric constant in the module."""
        expected = {
            ec.OK, ec.GENERIC, ec.USAGE, ec.VALIDATION, ec.HARD_REJECT,
            ec.AUTH, ec.PERMISSION, ec.LEASE, ec.HALTED, ec.OUT_OF_WINDOW,
            ec.MARKET_CLOSED, ec.UNAVAILABLE, ec.INTERNAL, ec.TRANSIENT,
            ec.TIMEOUT, ec.SIGINT,
        }
        assert ec.ALL_CODES == expected


# -----------------------------------------------------------------------------
# classify_exception — Kite SDK exceptions
# -----------------------------------------------------------------------------

def _fake_exc(name: str, message: str = "boom") -> Exception:
    """Build a fake exception with a Kite-SDK-style class name."""
    cls = type(name, (Exception,), {})
    return cls(message)


class TestClassifyKiteExceptions:
    def test_token_exception_is_auth(self) -> None:
        cls = classify_exception(_fake_exc("TokenException", "invalid token"))
        assert cls.exit_code == ec.AUTH
        assert cls.error_code == "AUTH"
        assert cls.retryable is False

    def test_input_exception_is_hard_reject(self) -> None:
        cls = classify_exception(_fake_exc("InputException", "bad params"))
        assert cls.exit_code == ec.HARD_REJECT
        assert cls.retryable is False

    def test_order_exception_is_hard_reject(self) -> None:
        cls = classify_exception(_fake_exc("OrderException", "margin"))
        assert cls.exit_code == ec.HARD_REJECT

    def test_margin_exception_is_hard_reject(self) -> None:
        cls = classify_exception(_fake_exc("MarginException", "insufficient"))
        assert cls.exit_code == ec.HARD_REJECT

    def test_holding_exception_is_hard_reject(self) -> None:
        cls = classify_exception(_fake_exc("HoldingException", "authorise"))
        assert cls.exit_code == ec.HARD_REJECT

    def test_permission_exception_is_permission(self) -> None:
        cls = classify_exception(_fake_exc("PermissionException", "no"))
        assert cls.exit_code == ec.PERMISSION
        assert cls.retryable is False

    def test_network_exception_is_transient(self) -> None:
        cls = classify_exception(_fake_exc("NetworkException", "net"))
        assert cls.exit_code == ec.UNAVAILABLE
        assert cls.retryable is True

    def test_data_exception_is_transient(self) -> None:
        cls = classify_exception(_fake_exc("DataException", "parse"))
        assert cls.exit_code == ec.UNAVAILABLE
        assert cls.retryable is True

    def test_general_exception_is_internal_not_retryable(self) -> None:
        """CRITICAL: Kite's GeneralException must never be retried silently."""
        cls = classify_exception(_fake_exc("GeneralException", "account blocked"))
        assert cls.exit_code == ec.INTERNAL
        assert cls.retryable is False


# -----------------------------------------------------------------------------
# classify_exception — our own classes
# -----------------------------------------------------------------------------

class TestClassifyOwnClasses:
    def test_orderbook_lookup_error(self) -> None:
        from kite_algo.resilience import OrderbookLookupError
        cls = classify_exception(OrderbookLookupError("api down"))
        assert cls.exit_code == ec.UNAVAILABLE
        assert cls.retryable is True

    def test_modification_limit_exceeded(self) -> None:
        from kite_algo.resilience import ModificationLimitExceeded
        cls = classify_exception(ModificationLimitExceeded("too many"))
        assert cls.exit_code == ec.HARD_REJECT
        assert cls.retryable is False

    def test_kite_session_error(self) -> None:
        from kite_algo.broker.kite import KiteSessionError
        cls = classify_exception(KiteSessionError("expired"))
        assert cls.exit_code == ec.AUTH

    def test_env_parse_error(self) -> None:
        from kite_algo.config import EnvParseError
        cls = classify_exception(EnvParseError("typo"))
        assert cls.exit_code == ec.USAGE


# -----------------------------------------------------------------------------
# classify_exception — Python builtins
# -----------------------------------------------------------------------------

class TestClassifyBuiltins:
    def test_keyboard_interrupt(self) -> None:
        cls = classify_exception(KeyboardInterrupt())
        assert cls.exit_code == ec.SIGINT

    def test_system_exit_preserves_int(self) -> None:
        cls = classify_exception(SystemExit(42))
        assert cls.exit_code == 42

    def test_system_exit_with_string_is_usage(self) -> None:
        """Our `_require_yes` and similar use `SystemExit("message")`; treat
        as usage-level.
        """
        cls = classify_exception(SystemExit("Refusing to place"))
        assert cls.exit_code == ec.USAGE

    def test_value_error_is_validation(self) -> None:
        cls = classify_exception(ValueError("bad input"))
        assert cls.exit_code == ec.VALIDATION

    def test_type_error_is_validation(self) -> None:
        cls = classify_exception(TypeError("wrong type"))
        assert cls.exit_code == ec.VALIDATION

    def test_unknown_defaults_to_internal(self) -> None:
        cls = classify_exception(Exception("???"))
        assert cls.exit_code == ec.INTERNAL
        assert cls.retryable is False


# -----------------------------------------------------------------------------
# classify_exception — string markers
# -----------------------------------------------------------------------------

class TestClassifyByMessage:
    def test_429_is_transient(self) -> None:
        cls = classify_exception(Exception("429 too many requests"))
        assert cls.exit_code == ec.TRANSIENT
        assert cls.retryable is True

    def test_504_is_transient(self) -> None:
        cls = classify_exception(Exception("504 gateway timeout"))
        assert cls.exit_code == ec.TRANSIENT

    def test_plain_timeout_is_transient(self) -> None:
        cls = classify_exception(Exception("read timed out"))
        assert cls.exit_code == ec.TRANSIENT


# -----------------------------------------------------------------------------
# exit_code_name reverse lookup
# -----------------------------------------------------------------------------

class TestExitCodeName:
    def test_known_codes(self) -> None:
        assert exit_code_name(ec.OK) == "OK"
        assert exit_code_name(ec.AUTH) == "AUTH"
        assert exit_code_name(ec.TIMEOUT) == "TIMEOUT"

    def test_unknown_code(self) -> None:
        assert exit_code_name(200) == "UNKNOWN_200"
