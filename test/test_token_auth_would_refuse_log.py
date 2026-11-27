"""The log-only would-refuse line for an unattested session key on TCP loopback.

An internal request proves it came from this machine with the internal secret, and
on TCP no kernel peer check binds the ``X-Session-Key`` it declares. Token auth
logs and counts such a request when ``session_key_is_attested`` would refuse it,
and still answers it exactly as before. These tests drive the real
``token_auth_middleware`` over a real loopback TCP connection and pin both halves:
the answer does not change, and the line fires only for the case a strict check
would refuse.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from token_auth_helpers import clear_nonces  # noqa: F401 - autouse isolation

import kiro_crew.member_memory_auth as member_memory_auth
from kiro_crew.dashboard import token_auth, token_auth_callers

SECRET = "internal-secret-would-refuse"
ROUTE = "/api/probe/{item}"
INTERNAL = frozenset({"/api/probe"})
#: The token this file's verifier double maps back to ``ATTESTED_KEY``.
GOOD_TOKEN = "good-session-token-value"
ATTESTED_KEY = "dashboard:slot-1"
LOGGER = "kiro_crew.dashboard.token_auth"


class _Slot:
    def __init__(self, app: str = "") -> None:
        self._app = app
        self.linked_session_key = ""


async def _echo(request: web.Request) -> web.Response:
    """What the handler was handed: the decision the middleware made."""
    return web.json_response(
        {
            "internal_auth": request.get("internal_auth"),
            "app": request.get("app", "<absent>"),
            "is_dashboard_user": request.get("is_dashboard_user", "<absent>"),
        }
    )


def _client() -> TestClient:
    app = web.Application(
        middlewares=[
            token_auth.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
        ]
    )
    # An app owns slot-1, so a key naming it publishes an app claim: the case where
    # the declared key changes what the handler is handed.
    app["state"] = SimpleNamespace(_slots={"slot-1": _Slot("file-explorer")})
    app.router.add_get(ROUTE, _echo)
    return TestClient(TestServer(app, host="127.0.0.1"))


@pytest.fixture(autouse=True)
def _only_good_token_attests(_floor_monkeypatch):
    """``GOOD_TOKEN`` verifies to ``ATTESTED_KEY``; every other token to nothing.

    The throttle map and counter start empty in every test through the suite's
    ``_fresh_would_refuse_session_key_log`` reset.
    """
    _floor_monkeypatch.setattr(
        member_memory_auth,
        "verify_session_token",
        lambda token: ATTESTED_KEY if token == GOOD_TOKEN else "",
    )


def _headers(key: str | None, token: str | None = None, caller: str | None = None) -> dict:
    headers = {"X-Internal-Secret": SECRET}
    if key is not None:
        headers["X-Session-Key"] = key
    if token is not None:
        headers["X-Session-Token"] = token
    if caller is not None:
        headers["X-Internal-Caller"] = caller
    return headers


async def _get(headers: dict, path: str = "/api/probe/item-42?q=query-value") -> tuple[int, dict]:
    async with _client() as client:
        resp = await client.get(path, headers=headers)
        return resp.status, await resp.json()


def _would_refuse_lines(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == LOGGER and r.getMessage().startswith("would refuse:")
    ]


class TestTheAnswerDoesNotChange:
    """Every caller gets what main gives it: the check only logs."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "headers",
        [
            _headers(ATTESTED_KEY),
            _headers(ATTESTED_KEY, token="forged"),
            _headers(ATTESTED_KEY, token=GOOD_TOKEN),
            _headers("cron:job-1"),
            _headers("dashboard:ui"),
            _headers(None),
        ],
        ids=["no-token", "wrong-token", "attested", "cron-no-token", "dashboard-ui", "no-key"],
    )
    async def test_the_handler_is_handed_what_it_is_handed_without_the_check(
        self, monkeypatch, headers
    ) -> None:
        checked = await _get(headers)

        async def _no_check(_request: web.Request) -> None:
            return None

        monkeypatch.setattr(token_auth, "_note_unattested_tcp_session_key", _no_check)
        unchecked = await _get(headers)
        assert checked == unchecked
        assert checked[0] == 200 and checked[1]["internal_auth"] is True

    @pytest.mark.asyncio
    async def test_an_unattested_key_still_publishes_its_app_claim(self) -> None:
        status, body = await _get(_headers(ATTESTED_KEY))
        assert (status, body["app"], body["is_dashboard_user"]) == (200, "file-explorer", False)

    @pytest.mark.asyncio
    async def test_a_check_that_raises_leaves_the_request_answered(
        self, monkeypatch, caplog
    ) -> None:
        def _boom(_request, _key):
            raise OSError("token mapping unreadable")

        monkeypatch.setattr(token_auth_callers, "session_key_is_attested", _boom)
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        status, body = await _get(_headers(ATTESTED_KEY, token="forged"))
        assert (status, body["app"]) == (200, "file-explorer")
        assert _would_refuse_lines(caplog) == []

    @pytest.mark.asyncio
    async def test_a_tokenless_tcp_key_is_logged_without_verifying(
        self, monkeypatch, caplog
    ) -> None:
        """No token means nothing to verify, so no file is read for the answer."""

        def _never(_request, _key):
            raise AssertionError("a tokenless TCP request must not be verified")

        monkeypatch.setattr(token_auth_callers, "session_key_is_attested", _never)
        caplog.set_level(logging.WARNING, logger=LOGGER)
        status, _body = await _get(_headers(ATTESTED_KEY))
        assert status == 200
        assert len(_would_refuse_lines(caplog)) == 1


class TestTheLineFiresOnlyWhereAStrictCheckWouldRefuse:
    @pytest.mark.asyncio
    async def test_an_unattested_tcp_key_is_logged_and_counted(self, caplog) -> None:
        caplog.set_level(logging.WARNING, logger=LOGGER)
        await _get(_headers(ATTESTED_KEY, caller="kirocrew-dashboard"))
        (line,) = _would_refuse_lines(caplog)
        assert "kind=dashboard," in line
        assert "caller=kirocrew-dashboard" in line
        assert f"route=GET {ROUTE}" in line
        assert "session token absent" in line
        assert token_auth_callers._WOULD_REFUSE_COUNTS == {
            ("dashboard", "kirocrew-dashboard", ROUTE): 1
        }

    @pytest.mark.asyncio
    async def test_a_token_for_another_session_is_logged_as_unverified(self, caplog) -> None:
        caplog.set_level(logging.WARNING, logger=LOGGER)
        await _get(_headers("cron:job-1", token=GOOD_TOKEN))
        (line,) = _would_refuse_lines(caplog)
        assert "kind=cron," in line and "session token unverified" in line

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "headers",
        [
            _headers(ATTESTED_KEY, token=GOOD_TOKEN),
            _headers("dashboard:ui"),
            _headers(None),
            _headers("  "),
        ],
        ids=["attested", "dashboard-ui", "no-key", "blank-key"],
    )
    async def test_no_line_for_an_attested_or_exempt_caller(self, caplog, headers) -> None:
        caplog.set_level(logging.WARNING, logger=LOGGER)
        await _get(headers)
        assert _would_refuse_lines(caplog) == []
        assert not token_auth_callers._WOULD_REFUSE_COUNTS

    @pytest.mark.asyncio
    async def test_no_line_for_a_peer_verified_unix_caller(self, monkeypatch, caplog) -> None:
        """On the Unix socket the kernel peer check binds the key; nothing to log."""

        async def _verified(request, _sock, _path):
            request["peer_verified"] = True
            return None

        monkeypatch.setattr(token_auth, "_unix_request_socket", lambda _request: object())
        monkeypatch.setattr(token_auth, "_verify_unix_peer", _verified)
        caplog.set_level(logging.WARNING, logger=LOGGER)
        status, body = await _get(_headers(ATTESTED_KEY))
        assert (status, body["app"]) == (200, "file-explorer")
        assert _would_refuse_lines(caplog) == []
        assert not token_auth_callers._WOULD_REFUSE_COUNTS


class TestTheLineCarriesNoSecretAndIsThrottled:
    @pytest.mark.asyncio
    async def test_no_key_token_secret_or_query_reaches_the_line(self, caplog) -> None:
        caplog.set_level(logging.WARNING, logger=LOGGER)
        await _get(_headers("custom-prefix:private-name", token="forged-token-value"))
        (line,) = _would_refuse_lines(caplog)
        assert "kind=other," in line and "caller=other," in line
        for leaked in (
            "private-name",
            "custom-prefix",
            "forged-token-value",
            SECRET,
            "query-value",
            "item-42",
        ):
            assert leaked not in line

    @pytest.mark.asyncio
    async def test_repeats_are_counted_but_written_once_per_window(
        self, monkeypatch, caplog
    ) -> None:
        caplog.set_level(logging.WARNING, logger=LOGGER)
        for _ in range(3):
            await _get(_headers(ATTESTED_KEY))
        assert len(_would_refuse_lines(caplog)) == 1
        assert sum(token_auth_callers._WOULD_REFUSE_COUNTS.values()) == 3
        # Once the window has passed, the next line carries the running count.
        monkeypatch.setattr(token_auth_callers, "_WOULD_REFUSE_LOG_INTERVAL_SECS", 0.0)
        await _get(_headers(ATTESTED_KEY))
        lines = _would_refuse_lines(caplog)
        assert len(lines) == 2 and "4 such requests" in lines[1]


def _enforce(monkeypatch) -> None:
    monkeypatch.setattr(token_auth, "unattested_session_key_mode", lambda: "enforce")


class TestEnforceMode:
    """``dashboard.unattested_session_keys = enforce`` refuses what log mode records."""

    @pytest.mark.asyncio
    async def test_log_mode_still_admits_the_unattested_key(self, monkeypatch, caplog) -> None:
        # Default mode: the request is answered exactly as before, plus the log line.
        monkeypatch.setattr(token_auth, "unattested_session_key_mode", lambda: "log")
        caplog.set_level(logging.WARNING, logger=LOGGER)
        status, body = await _get(_headers(ATTESTED_KEY))
        assert status == 200 and body["internal_auth"] is True
        assert len(_would_refuse_lines(caplog)) == 1

    @pytest.mark.asyncio
    async def test_enforce_refuses_an_unattested_tcp_key(self, monkeypatch, caplog) -> None:
        _enforce(monkeypatch)
        caplog.set_level(logging.WARNING, logger=LOGGER)
        status, _body = await _get(_headers(ATTESTED_KEY))
        assert status == 403
        # The would-refuse line still fires, so the log and the refusal name one set.
        assert len(_would_refuse_lines(caplog)) == 1

    @pytest.mark.asyncio
    async def test_enforce_writes_a_sel_denied_row(self, monkeypatch) -> None:
        # The grant row is written before the key check, so only a denied row
        # keeps the audit log from reading the refused request as granted.
        _enforce(monkeypatch)
        rows: list[dict] = []
        sel = SimpleNamespace(log_api_access=lambda **kw: rows.append(kw))
        monkeypatch.setattr(token_auth, "_sel_fn", lambda: sel)
        status, _body = await _get(_headers(ATTESTED_KEY))
        assert status == 403
        denied = [
            r
            for r in rows
            if r.get("operation") == "internal_auth" and r.get("outcome") == "denied"
        ]
        assert len(denied) == 1
        assert denied[0]["error"] == "unattested session key"
        assert denied[0]["source"] == "token_auth"
        assert denied[0]["resources"] == "/api/probe/item-42"

    @pytest.mark.asyncio
    async def test_enforce_admits_an_attested_key(self, monkeypatch) -> None:
        _enforce(monkeypatch)
        status, body = await _get(_headers(ATTESTED_KEY, token=GOOD_TOKEN))
        assert status == 200 and body["app"] == "file-explorer"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", [None, "dashboard:ui"])
    async def test_enforce_never_touches_an_exempt_key(self, monkeypatch, key) -> None:
        _enforce(monkeypatch)
        status, _body = await _get(_headers(key))
        assert status == 200

    def test_the_config_default_is_log(self) -> None:
        from kiro_crew.config.sections_dashboard import DashboardConfig

        assert DashboardConfig().unattested_session_keys == "log"

    def test_the_mode_reads_the_live_value(self, monkeypatch) -> None:
        from kiro_crew.config import live

        cfg = SimpleNamespace(dashboard=SimpleNamespace(unattested_session_keys="enforce"))
        monkeypatch.setattr(live, "snapshot", lambda: cfg)
        assert token_auth_callers.unattested_session_key_mode() == "enforce"
        monkeypatch.setattr(
            live,
            "snapshot",
            lambda: SimpleNamespace(dashboard=SimpleNamespace(unattested_session_keys="bogus")),
        )
        assert token_auth_callers.unattested_session_key_mode() == "log"

    def test_enforce_on_disk_holds_before_the_watcher_is_primed(self, monkeypatch) -> None:
        # The boot window: no snapshot yet. The operator's on-disk "enforce" must
        # still apply, not the "log" default.
        from dataclasses import replace

        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        base = KiroCrewConfig()
        on_disk = replace(
            base, dashboard=replace(base.dashboard, unattested_session_keys="enforce")
        )
        monkeypatch.setattr(live, "snapshot", lambda: None)
        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: on_disk))
        assert token_auth_callers.unattested_session_key_mode() == "enforce"

    def test_an_unreadable_config_reads_as_log(self, monkeypatch) -> None:
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        def _unreadable() -> KiroCrewConfig:
            raise OSError("config.json unreadable")

        monkeypatch.setattr(live, "snapshot", lambda: None)
        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_unreadable))
        assert token_auth_callers.unattested_session_key_mode() == "log"
