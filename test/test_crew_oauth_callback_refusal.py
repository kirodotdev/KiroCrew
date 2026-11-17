"""A refused sign-in: the callback's ``error`` maps onto a closed set of codes.

``SignIn._callback`` answers every listener (the shared loopback, a
pre-registered card's own port, the gateway's public route), so it is driven
directly for the input table and through both real routes once each. Nothing
the vendor writes (``error``, ``error_description``, ``error_uri``) may reach
the page, the mint row, a log line or the audit.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse

import pytest

from kiro_crew.connections import crew_oauth, crew_oauth_dcr
from kiro_crew.connections.crew_oauth import CALLBACK_ERROR_CODES, CrewOAuthError

PROBE = "<script>alert(1)</script>"
PAGE = "Sign-in was not completed. You can close this tab."
RFC_6749_ERRORS = (
    "invalid_request",
    "unauthorized_client",
    "access_denied",
    "unsupported_response_type",
    "invalid_scope",
    "server_error",
    "temporarily_unavailable",
)


@pytest.fixture()
def recorded(monkeypatch):
    """Every SEL record the flow writes, captured instead of reaching the data home."""
    import kiro_crew.sel as sel_mod

    seen: list[dict] = []

    class _Sel:
        def log_api_access(self, **record):
            seen.append(record)

    monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
    return seen


def _emitted(caplog) -> str:
    """What the gateway would write: every ``kiro_crew`` record, and any other at WARNING+.

    ``cli_logging._setup_cli_logging`` holds third-party loggers at WARNING, so
    aiohttp's INFO access line (which carries the whole query) never reaches
    ``gateway.log``; the test server here emits it only because caplog listens
    at DEBUG.
    """
    return "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("kiro_crew") or record.levelno >= logging.WARNING
    )


def _sign_in(state: str = "s" * 43) -> crew_oauth.SignIn:
    sign_in = crew_oauth.SignIn("github")
    sign_in._state = state
    return sign_in


async def _answer(sign_in, query):
    from aiohttp.test_utils import make_mocked_request

    request = make_mocked_request("GET", "/callback?" + urllib.parse.urlencode(query))
    return await sign_in._callback(request)


def test_the_closed_set_is_exactly_rfc_6749_section_4_1_2_1():
    assert set(CALLBACK_ERROR_CODES) == set(RFC_6749_ERRORS)
    codes = set(CALLBACK_ERROR_CODES.values()) | {crew_oauth.CALLBACK_REFUSED}
    assert len(codes) == len(RFC_6749_ERRORS) + 1
    assert all(code.startswith("callback_") and code.isascii() for code in codes)


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ({}, "callback_refused"),
        *(({"error": e}, f"callback_{e}") for e in RFC_6749_ERRORS),
        ({"error": "access_denied", "code": "c"}, "callback_access_denied"),
        ({"error": "", "code": "c"}, "callback_refused"),
        ({"error": "ACCESS_DENIED"}, "callback_refused"),
        ({"error": "access_denied "}, "callback_refused"),
        ({"error": "consent_declined"}, "callback_refused"),
        ({"error": "x" * 10_000, "error_description": "y" * 10_000}, "callback_refused"),
        (
            {"error": PROBE, "error_description": PROBE, "error_uri": "javascript:" + PROBE},
            "callback_refused",
        ),
        ({"error": "accès_refusé", "error_description": "管理者がブロック"}, "callback_refused"),
        (
            {"error": "access_denied", "error_description": PROBE, "error_uri": PROBE},
            "callback_access_denied",
        ),
    ],
)
def test_a_callback_without_a_usable_code_records_a_closed_code(query, code):
    async def _run():
        sign_in = _sign_in()
        resp = await _answer(sign_in, {"state": sign_in._state, **query})
        assert resp.status == 200 and resp.text == PAGE
        with pytest.raises(CrewOAuthError) as err:
            await sign_in._done
        return err.value.code

    assert asyncio.run(_run()) == code


def test_a_wrong_state_settles_nothing_whatever_error_it_carries():
    async def _run():
        sign_in = _sign_in()
        resp = await _answer(
            sign_in, {"state": "other", "error": "access_denied", "error_description": PROBE}
        )
        assert resp.status == 400 and PROBE not in resp.text
        return sign_in._done.done()

    assert asyncio.run(_run()) is False


def test_a_refusal_reaches_the_mint_row_and_audit_as_its_code_only(monkeypatch, recorded, caplog):
    from kiro_crew.connections import mint

    monkeypatch.setattr(mint, "_mints", {"github": {"token": "t1", "state": "waiting"}})

    async def _run():
        sign_in = _sign_in()
        await _answer(
            sign_in,
            {
                "state": sign_in._state,
                "error": "access_denied",
                "error_description": PROBE,
                "error_uri": PROBE,
            },
        )
        await crew_oauth._await_consent("github", "t1", sign_in)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(_run())
    assert mint._mints["github"]["state"] == "failed"
    assert mint._mints["github"]["reason"] == "crew_oauth_callback_access_denied"
    assert [r["resources"] for r in recorded] == ["provider:github reason=callback_access_denied"]
    assert PROBE not in _emitted(caplog) and "script" not in _emitted(caplog)


def test_the_gateway_route_answers_a_refusal_with_its_fixed_page(monkeypatch, recorded, caplog):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers.connections import api_connections_oauth_callback

    monkeypatch.setattr(crew_oauth_dcr, "_pending", {})
    app = web.Application()
    app.router.add_get(
        "/api/connections/oauth/callback", api_connections_oauth_callback, allow_head=False
    )

    async def _run():
        sign_in = _sign_in()
        await crew_oauth_dcr.listen(
            sign_in._state, sign_in._callback, slug="github", loopback=False
        )
        async with TestClient(TestServer(app)) as client:
            query = {
                "state": sign_in._state,
                "error": PROBE,
                "error_description": PROBE,
                "error_uri": PROBE,
            }
            async with client.get("/api/connections/oauth/callback", params=query) as r:
                text = await r.text()
        await crew_oauth_dcr.unlisten(sign_in._state)
        with pytest.raises(CrewOAuthError) as err:
            await sign_in._done
        return text, err.value.code

    with caplog.at_level(logging.DEBUG):
        text, code = asyncio.run(_run())
    assert code == "callback_refused" and PAGE in text
    assert PROBE not in text and "script" not in text
    assert all(PROBE not in str(r) and "script" not in str(r) for r in recorded)
    assert PROBE not in _emitted(caplog) and "script" not in _emitted(caplog)


def test_the_shared_loopback_answers_a_refusal_with_its_fixed_page(monkeypatch, caplog):
    import aiohttp

    monkeypatch.setattr(crew_oauth_dcr, "DCR_CALLBACK_PORT", 0)
    monkeypatch.setattr(crew_oauth_dcr, "_pending", {})
    monkeypatch.setattr(crew_oauth_dcr, "_listener", {})

    async def _run():
        sign_in = _sign_in()
        port = await crew_oauth_dcr.listen(sign_in._state, sign_in._callback, slug="github")
        try:
            async with aiohttp.ClientSession() as s:
                query = {
                    "state": sign_in._state,
                    "error": "access_denied",
                    "error_description": PROBE,
                }
                async with s.get(f"http://127.0.0.1:{port}/callback", params=query) as r:
                    text = await r.text()
        finally:
            await crew_oauth_dcr.unlisten(sign_in._state)
        with pytest.raises(CrewOAuthError) as err:
            await sign_in._done
        return text, err.value.code

    with caplog.at_level(logging.DEBUG):
        text, code = asyncio.run(_run())
    assert (text, code) == (PAGE, "callback_access_denied")
    assert PROBE not in _emitted(caplog)


@pytest.mark.parametrize("iss", ["https://VENDOR\uff03TEXT/", "https://[vendor-text]/"])
def test_an_iss_urlsplit_refuses_is_a_mismatch_not_a_logged_traceback(iss, caplog):
    client = crew_oauth_dcr.Client(
        client_id="cid",
        client_secret=None,
        secret_expires_at=0.0,
        auth_method="none",
        redirect_uri="http://127.0.0.1:48100/callback",
        issuer="https://issuer.example",
        resource="https://mcp.example/mcp",
        authorize="https://issuer.example/authorize",
        token="https://issuer.example/token",
        revoke=None,
        iss_param=True,
    )

    async def _run():
        sign_in = _sign_in()
        sign_in._dcr = crew_oauth_dcr.Plan(client, "")
        resp = await _answer(sign_in, {"state": sign_in._state, "code": "c", "iss": iss})
        assert resp.status == 400 and "TEXT" not in resp.text and "vendor" not in resp.text
        with pytest.raises(CrewOAuthError) as err:
            await sign_in._done
        return err.value.code

    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(_run()) == "issuer_mismatch"
    assert "TEXT" not in _emitted(caplog) and "vendor-text" not in _emitted(caplog)
