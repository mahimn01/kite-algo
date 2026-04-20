"""Tests for the raw-HTTP Alerts client + CLI subcommands."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from unittest.mock import Mock

import pytest

from kite_algo.alerts import (
    ALERTS_MAX_ACTIVE_PER_USER,
    AlertsAPIError,
    AlertsClient,
    _parse_response,
)


# -----------------------------------------------------------------------------
# _parse_response
# -----------------------------------------------------------------------------

class TestParseResponse:
    def _mock_resp(self, status_code: int, body: dict, request_id: str = "kite-abc") -> Mock:
        r = Mock()
        r.status_code = status_code
        r.json = Mock(return_value=body)
        r.headers = {"x-kite-request-id": request_id}
        return r

    def test_success_returns_data(self) -> None:
        resp = self._mock_resp(200, {"status": "success", "data": [{"uuid": "A"}]})
        data = _parse_response(resp)
        assert data == [{"uuid": "A"}]

    def test_error_raises(self) -> None:
        resp = self._mock_resp(400, {"status": "error", "message": "bad",
                                     "error_type": "InputException"})
        with pytest.raises(AlertsAPIError) as exc_info:
            _parse_response(resp)
        assert exc_info.value.status_code == 400
        assert exc_info.value.error_type == "InputException"
        assert exc_info.value.request_id == "kite-abc"

    def test_non_json_body_raises(self) -> None:
        r = Mock()
        r.status_code = 502
        r.json = Mock(side_effect=ValueError("not json"))
        r.headers = {}
        with pytest.raises(AlertsAPIError) as exc_info:
            _parse_response(r)
        assert exc_info.value.status_code == 502


# -----------------------------------------------------------------------------
# AlertsClient
# -----------------------------------------------------------------------------

@pytest.fixture
def http_session():
    """Mock requests.Session with a configurable `.request()` method."""
    return Mock()


@pytest.fixture
def client(http_session):
    return AlertsClient(
        api_key="KEY",
        access_token="TOK",
        base_url="https://fake.kite",
        http_session=http_session,
    )


def _ok(data, status=200):
    r = Mock()
    r.status_code = status
    r.json = Mock(return_value={"status": "success", "data": data})
    r.headers = {}
    return r


class TestAlertsClient:
    def test_list_sends_query_params(self, client, http_session) -> None:
        http_session.request.return_value = _ok([])
        client.list(status="enabled", page=2, page_size=25)
        args, kwargs = http_session.request.call_args
        assert args[0] == "GET"
        url = args[1]
        assert "status=enabled" in url
        assert "page=2" in url
        assert "page_size=25" in url

    def test_list_without_status(self, client, http_session) -> None:
        http_session.request.return_value = _ok([])
        client.list()
        url = http_session.request.call_args[0][1]
        assert "status" not in url

    def test_get_includes_uuid(self, client, http_session) -> None:
        http_session.request.return_value = _ok({"uuid": "A"})
        client.get("A")
        url = http_session.request.call_args[0][1]
        assert url.endswith("/alerts/A")

    def test_create_serialises_basket(self, client, http_session) -> None:
        http_session.request.return_value = _ok({"uuid": "NEW"})
        client.create({
            "name": "n", "type": "ato",
            "basket": [{"exchange": "NSE", "tradingsymbol": "X"}],
        })
        _, kwargs = http_session.request.call_args
        body = kwargs["data"]
        assert isinstance(body["basket"], str)
        # Round-trips to the original structure.
        assert json.loads(body["basket"]) == [{"exchange": "NSE", "tradingsymbol": "X"}]

    def test_create_passthrough_non_ato(self, client, http_session) -> None:
        http_session.request.return_value = _ok({"uuid": "NEW"})
        client.create({"name": "n", "type": "simple"})
        _, kwargs = http_session.request.call_args
        body = kwargs["data"]
        assert "basket" not in body

    def test_modify_url(self, client, http_session) -> None:
        http_session.request.return_value = _ok({})
        client.modify("UU", {"name": "x"})
        args, _ = http_session.request.call_args
        assert args[0] == "PUT"
        assert args[1].endswith("/alerts/UU")

    def test_delete_uuid_query(self, client, http_session) -> None:
        http_session.request.return_value = _ok(True)
        client.delete("UU")
        args, _ = http_session.request.call_args
        assert args[0] == "DELETE"
        assert "uuid=UU" in args[1]

    def test_history_url(self, client, http_session) -> None:
        http_session.request.return_value = _ok([])
        client.history("UU")
        args, _ = http_session.request.call_args
        assert args[1].endswith("/alerts/UU/history")

    def test_rate_limiter_called(self, http_session) -> None:
        from kite_algo.resilience import KiteRateLimiter

        limiter = KiteRateLimiter()
        http_session.request.return_value = _ok([])
        c = AlertsClient(
            api_key="K", access_token="T",
            base_url="https://fake", http_session=http_session,
            rate_limiter=limiter,
        )

        hits = {"n": 0}
        original = limiter.wait_general
        def counted() -> None:
            hits["n"] += 1
            original()
        limiter.wait_general = counted
        c.list()
        assert hits["n"] == 1


class TestConstants:
    def test_cap_is_500(self) -> None:
        assert ALERTS_MAX_ACTIVE_PER_USER == 500


# -----------------------------------------------------------------------------
# CLI integration
# -----------------------------------------------------------------------------

@pytest.fixture
def parser():
    from kite_algo.kite_tool import build_parser
    return build_parser()


class TestAlertsCli:
    def test_create_requires_yes(self, parser) -> None:
        from kite_algo.kite_tool import cmd_alerts_create
        args = parser.parse_args([
            "alerts-create", "--name", "A", "--type", "simple",
            "--lhs-exchange", "NSE", "--lhs-tradingsymbol", "RELIANCE",
            "--operator", ">", "--rhs-type", "constant", "--rhs-constant", "1400",
        ])
        with pytest.raises(SystemExit, match="Refusing"):
            cmd_alerts_create(args)

    def test_ato_requires_basket(self, parser, monkeypatch) -> None:
        from kite_algo import kite_tool as kt
        monkeypatch.setattr(kt, "_new_alerts_client", lambda: Mock())
        args = parser.parse_args([
            "alerts-create", "--name", "A", "--type", "ato",
            "--lhs-exchange", "NSE", "--lhs-tradingsymbol", "RELIANCE",
            "--operator", ">", "--rhs-type", "constant", "--rhs-constant", "1400",
            "--yes",
        ])
        rc = kt.cmd_alerts_create(args)
        assert rc == 2  # USAGE (missing --basket-json)

    def test_delete_requires_yes(self, parser) -> None:
        from kite_algo.kite_tool import cmd_alerts_delete
        args = parser.parse_args(["alerts-delete", "--uuid", "X"])
        with pytest.raises(SystemExit, match="Refusing"):
            cmd_alerts_delete(args)

    def test_list_emits(self, parser, monkeypatch) -> None:
        from kite_algo import kite_tool as kt
        fake = Mock()
        fake.list.return_value = [{"uuid": "A"}, {"uuid": "B"}]
        monkeypatch.setattr(kt, "_new_alerts_client", lambda: fake)

        args = parser.parse_args(["alerts-list", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = kt.cmd_alerts_list(args)
        assert rc == 0
        data = json.loads(buf.getvalue())["data"]
        assert data == [{"uuid": "A"}, {"uuid": "B"}]
