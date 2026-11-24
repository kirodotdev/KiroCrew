"""A loopback sign-in lands its browser tab on the titled page the gateway route serves.

Both loopback listeners, a consent's own port (``SignIn._bind``) and the shared
DCR port, hand ``SignIn._callback``'s fixed text to the browser in the same
titled HTML page as ``gateway_callback``, signed in or not.
"""

from __future__ import annotations

import asyncio
import types

import aiohttp
import pytest

from kiro_crew.connections import crew_oauth, crew_oauth_dcr
from kiro_crew.connections.crew_oauth import TokenStore
from kiro_crew.secrets import SecretVault

#: Below half the 120 s pytest-timeout, so a stall fails by name.
BOUND_SECS = 30


async def _own_port(sign_in: crew_oauth.SignIn) -> int:
    await sign_in._bind(0, "/callback")
    return sign_in.bound_port


async def _shared_port(sign_in: crew_oauth.SignIn) -> int:
    return await crew_oauth_dcr.listen(sign_in._state, sign_in._callback, slug="github")


@pytest.mark.parametrize("listener", [_own_port, _shared_port], ids=["own", "shared"])
@pytest.mark.parametrize(
    ("query", "said"),
    [
        ({"code": "c0de"}, "Signed in. You can close this tab and return to Kiro Crew."),
        ({"error": "access_denied"}, "Sign-in was not completed. You can close this tab."),
    ],
    ids=["signed_in", "not_completed"],
)
def test_a_loopback_callback_answers_with_the_titled_page(
    monkeypatch, tmp_path, listener, query, said
):
    async def _post(slug, form):
        return {"access_token": "a1", "refresh_token": "r1", "expires_in": 3600}

    monkeypatch.setattr(crew_oauth, "_post_token", _post)
    monkeypatch.setattr(crew_oauth_dcr, "DCR_CALLBACK_PORT", 0)
    monkeypatch.setattr(crew_oauth_dcr, "_pending", {})
    monkeypatch.setattr(crew_oauth_dcr, "_listener", {})

    async def _run():
        sign_in = crew_oauth.SignIn("github")
        sign_in._client = types.SimpleNamespace(client_id="cid", client_secret="sec")
        sign_in._redirect = "http://127.0.0.1:1/callback"
        # The swap runs beside the tab, as in a real Connect.
        finish = asyncio.ensure_future(sign_in.finish(TokenStore(SecretVault(tmp_path))))
        try:
            port = await listener(sign_in)
            params = {"state": sign_in._state, **query}

            async def _get():
                async with aiohttp.ClientSession() as s:
                    async with s.get(f"http://127.0.0.1:{port}/callback", params=params) as r:
                        return r.status, r.headers["Content-Type"], await r.text()

            return await asyncio.wait_for(_get(), BOUND_SECS)
        finally:
            await sign_in.shutdown()
            await crew_oauth_dcr.unlisten(sign_in._state)
            await asyncio.wait_for(asyncio.gather(finish, return_exceptions=True), BOUND_SECS)

    status, content_type, text = asyncio.run(_run())
    assert status == 200 and content_type.startswith("text/html")
    assert text == crew_oauth_dcr._PAGE.format(said)
    assert "<title>Kiro Crew</title>" in text
