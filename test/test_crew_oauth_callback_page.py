"""The consent tab says "Signed in" only once the code swap and the write landed.

``SignIn._callback`` holds its answer until ``finish`` reports the outcome, so a
swap the provider refuses, a cancelled sign-in or a slow one never reads as
signed in.
"""

from __future__ import annotations

import asyncio
import types
import urllib.parse

import pytest

from kiro_crew.connections import crew_oauth
from kiro_crew.connections.crew_oauth import CrewOAuthError, TokenStore
from kiro_crew.secrets import SecretVault

#: Below half the 120 s pytest-timeout, so a stall fails by name.
BOUND_SECS = 30


def _sign_in() -> crew_oauth.SignIn:
    sign_in = crew_oauth.SignIn("github")
    sign_in._client = types.SimpleNamespace(client_id="cid", client_secret="sec")
    sign_in._redirect = "http://127.0.0.1:1/callback"
    return sign_in


async def _answer(sign_in, **query):
    from aiohttp.test_utils import make_mocked_request

    query = {"state": sign_in._state, **query}
    request = make_mocked_request("GET", "/callback?" + urllib.parse.urlencode(query))
    return await asyncio.wait_for(sign_in._callback(request), BOUND_SECS)


async def _drain(task: asyncio.Future) -> None:
    """Cancel *task* if the test left it running, and wait for it to end."""
    if not task.done():
        task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), BOUND_SECS)


def test_the_tab_says_signed_in_after_the_pair_is_stored(monkeypatch, tmp_path):
    async def _post(slug, form):
        return {"access_token": "a1", "refresh_token": "r1", "expires_in": 3600}

    monkeypatch.setattr(crew_oauth, "_post_token", _post)
    store = TokenStore(SecretVault(tmp_path))

    async def _run():
        sign_in = _sign_in()
        finish = asyncio.ensure_future(sign_in.finish(store))
        try:
            resp = await _answer(sign_in, code="c0de")
            # The page is answered only once the write has landed.
            assert store.read("github")[0].access == "a1"
            await asyncio.wait_for(finish, BOUND_SECS)
        finally:
            await _drain(finish)
            await sign_in.shutdown()
        return resp

    resp = asyncio.run(_run())
    assert resp.status == 200 and resp.text.startswith("Signed in.")


def test_a_failed_swap_tells_the_tab_it_did_not_finish(monkeypatch, tmp_path):
    async def _post(slug, form):
        raise CrewOAuthError("invalid_grant")

    monkeypatch.setattr(crew_oauth, "_post_token", _post)
    store = TokenStore(SecretVault(tmp_path))

    async def _run():
        sign_in = _sign_in()
        finish = asyncio.ensure_future(sign_in.finish(store))
        try:
            resp = await _answer(sign_in, code="c0de")
            with pytest.raises(CrewOAuthError) as err:
                await asyncio.wait_for(finish, BOUND_SECS)
        finally:
            await _drain(finish)
            await sign_in.shutdown()
        return resp, err.value.code

    resp, code = asyncio.run(_run())
    assert code == "invalid_grant"
    assert "Signed in" not in resp.text and resp.text == crew_oauth._PAGE_NOT_FINISHED
    assert store.read("github") is None


def test_a_sign_in_shut_down_mid_swap_does_not_read_as_signed_in():
    async def _run():
        sign_in = _sign_in()
        page = asyncio.ensure_future(_answer(sign_in, code="c0de"))
        try:
            await asyncio.wait_for(asyncio.shield(sign_in._done), BOUND_SECS)
            await sign_in.shutdown()
            return await asyncio.wait_for(page, BOUND_SECS)
        finally:
            await _drain(page)
            await sign_in.shutdown()

    assert asyncio.run(_run()).text == crew_oauth._PAGE_NOT_FINISHED


def test_a_swap_slower_than_the_page_wait_points_back_to_kiro_crew(monkeypatch):
    # 0: the page stops waiting at once, with no real clock in the test.
    monkeypatch.setattr(crew_oauth, "_CALLBACK_PAGE_WAIT_SECS", 0)

    async def _run():
        sign_in = _sign_in()
        try:
            return await _answer(sign_in, code="c0de")
        finally:
            await sign_in.shutdown()

    resp = asyncio.run(_run())
    assert resp.status == 200 and resp.text == crew_oauth._PAGE_PENDING
