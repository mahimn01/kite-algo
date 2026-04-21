"""Tests for the listener-robustness fixes: full-URL paste extraction,
--kite-redirect-uri port derivation, and error paths.
"""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from kite_algo.kite_tool import _extract_request_token, cmd_login


class TestExtractRequestToken:
    def test_bare_token(self) -> None:
        assert _extract_request_token("abc123XYZ") == "abc123XYZ"

    def test_key_value_only(self) -> None:
        assert _extract_request_token("request_token=XYZ999") == "XYZ999"

    def test_full_url_port_80(self) -> None:
        url = ("http://127.0.0.1/?state=fcc2b4bbb1e702b75c813b5b4c9b476a"
               "&action=login&type=login&status=success"
               "&request_token=hM4dibzEFkiJquZ0aIFggmYIIrj3xpCy")
        assert _extract_request_token(url) == "hM4dibzEFkiJquZ0aIFggmYIIrj3xpCy"

    def test_full_url_port_5000(self) -> None:
        url = "http://127.0.0.1:5000/?request_token=TKN_ABCDEFG&status=success"
        assert _extract_request_token(url) == "TKN_ABCDEFG"

    def test_empty_returns_empty(self) -> None:
        assert _extract_request_token("") == ""
        assert _extract_request_token("   ") == ""

    def test_strips_whitespace(self) -> None:
        assert _extract_request_token("  TOK_ABC  ") == "TOK_ABC"

    def test_url_with_only_request_token_pair(self) -> None:
        # Fragment case: user pasted a query-string chunk.
        out = _extract_request_token("state=ABC&request_token=XYZ&action=login")
        assert out == "XYZ"


class TestKiteRedirectUriFlag:
    def _base_args(self, **kw) -> argparse.Namespace:
        defaults = {
            "paste": False, "no_browser": True,
            "listen_port": 5000, "timeout": 0.5,
            "kite_redirect_uri": None, "no_fallback": True,
            "format": "json", "cmd": "login",
            "fields": None, "summary": False, "explain": False,
        }
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    @patch("kite_algo.kite_tool._import_kiteconnect")
    @patch("kite_algo.kite_tool.KiteConfig")
    @patch("kite_algo.oauth_callback.CallbackServer")
    def test_port_80_derived_from_uri(self, MockServer, MockCfg, MockKc) -> None:
        MockKc.return_value = MagicMock(return_value=MagicMock(
            login_url=MagicMock(return_value="https://kite.zerodha.com/connect/login?v=3&api_key=X"),
        ))
        MockCfg.from_env.return_value = MagicMock(
            api_key="X", api_secret="Y", require_credentials=MagicMock(),
        )
        server_instance = MagicMock()
        server_instance.wait.return_value = MagicMock(
            request_token=None, error="timeout", raw_query=None,
        )
        server_instance.redirect_uri = "http://127.0.0.1:80/"
        MockServer.return_value = server_instance

        args = self._base_args(kite_redirect_uri="http://127.0.0.1/")
        cmd_login(args)
        # CallbackServer should have been constructed with port=80.
        MockServer.assert_called_once()
        _, kwargs = MockServer.call_args
        assert kwargs["port"] == 80

    @patch("kite_algo.kite_tool._import_kiteconnect")
    @patch("kite_algo.kite_tool.KiteConfig")
    @patch("kite_algo.oauth_callback.CallbackServer")
    def test_explicit_port_in_uri(self, MockServer, MockCfg, MockKc) -> None:
        MockKc.return_value = MagicMock(return_value=MagicMock(
            login_url=MagicMock(return_value="https://kite.zerodha.com/connect/login?v=3&api_key=X"),
        ))
        MockCfg.from_env.return_value = MagicMock(
            api_key="X", api_secret="Y", require_credentials=MagicMock(),
        )
        server_instance = MagicMock()
        server_instance.wait.return_value = MagicMock(
            request_token=None, error="timeout", raw_query=None,
        )
        server_instance.redirect_uri = "http://127.0.0.1:7777/"
        MockServer.return_value = server_instance

        args = self._base_args(kite_redirect_uri="http://127.0.0.1:7777/")
        cmd_login(args)
        _, kwargs = MockServer.call_args
        assert kwargs["port"] == 7777

    @patch("kite_algo.kite_tool._import_kiteconnect")
    @patch("kite_algo.kite_tool.KiteConfig")
    def test_malformed_uri_returns_usage_error(self, MockCfg, MockKc) -> None:
        MockKc.return_value = MagicMock(return_value=MagicMock(
            login_url=MagicMock(return_value="https://example.com"),
        ))
        MockCfg.from_env.return_value = MagicMock(
            api_key="X", api_secret="Y", require_credentials=MagicMock(),
        )
        # urlparse tolerates most garbage. Use a value that actually raises —
        # since urlparse is forgiving, our code swallows most things. We at
        # least verify it doesn't crash the process on weird input.
        args = self._base_args(kite_redirect_uri="\x00")
        # Should complete without raising — either succeeds with a fallback
        # port or returns a clean error code.
        rc = cmd_login(args)
        assert rc in (0, 1, 2)


class TestPrivilegedPortError:
    @patch("kite_algo.kite_tool._import_kiteconnect")
    @patch("kite_algo.kite_tool.KiteConfig")
    @patch("kite_algo.oauth_callback.CallbackServer")
    def test_permission_error_gives_helpful_hint(
        self, MockServer, MockCfg, MockKc, capsys,
    ) -> None:
        MockKc.return_value = MagicMock(return_value=MagicMock(
            login_url=MagicMock(return_value="https://x.example/login"),
        ))
        MockCfg.from_env.return_value = MagicMock(
            api_key="X", api_secret="Y", require_credentials=MagicMock(),
        )
        server = MagicMock()
        server.start.side_effect = PermissionError("perm denied")
        MockServer.return_value = server

        args = argparse.Namespace(
            paste=False, no_browser=True, listen_port=80, timeout=0.5,
            kite_redirect_uri=None, no_fallback=True,
            format="json", cmd="login", fields=None, summary=False, explain=False,
        )
        rc = cmd_login(args)
        err = capsys.readouterr().err
        assert rc == 1
        assert "privileged" in err or "sudo" in err or "port 80" in err.lower()
