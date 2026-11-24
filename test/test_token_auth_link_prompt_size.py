"""A link that carries a prompt stays short enough to survive a chat message.

A messaging challenge link signs the person's message into its ``prompt`` claim, so
the URL grows with the message: roughly 4/3 of a byte per ASCII character and eight
URL characters per accented one, because the payload is ASCII-escaped JSON. Slack
splits a message line at about 4,000 bytes, which cuts such a URL. The cut token
fails its signature, the middleware falls back to the session cookie, and the link's
exchange (the one thing that opens a challenge window) never runs, so every message
after it is challenged again.

``generate_token`` therefore drops a prompt that does not fit. The link still
signs the person in; only the auto-sent text is lost, and it is still in the
chat it came from.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_request,
    _ok_handler,
    clear_nonces,
)

import kiro_crew.dashboard.token_auth as token_auth_mod
from kiro_crew.dashboard.token_auth import (
    _b64url_decode,
    generate_token,
    token_auth_middleware,
    validate_token,
)

_EXTRA = {
    "channel": "D0123456789",
    "thread_ts": "1795453427.764069",
    "msg_ts": "1795453427.764069",
    "session_key": "slack:D0123456789:1795453427.764069",
}

#: Slack splits a message line at about this many bytes.
_SLACK_LINE_SPLIT_BYTES = 3_990
#: The challenge line around the token ("🔐 <https://<host>/?token=…|<label>>")
#: gets this many bytes.
_LINE_AROUND_TOKEN_BYTES = 600


def _claims(token: str) -> dict:
    return json.loads(_b64url_decode(token.split(".", 1)[0]))


def test_the_cap_leaves_room_for_the_url_and_the_message_text() -> None:
    cap = token_auth_mod.LINK_TOKEN_MAX_CHARS
    assert cap + _LINE_AROUND_TOKEN_BYTES < _SLACK_LINE_SPLIT_BYTES


@pytest.mark.parametrize(
    "prompt",
    ["a" * 3_000, "é" * 500, "\U0001f600" * 300, "line\n" * 700],
    ids=["ascii", "accented", "emoji", "newlines"],
)
def test_a_long_prompt_is_dropped_and_the_link_fits(prompt: str, caplog) -> None:
    token = generate_token("U1", prompt=prompt, extra=_EXTRA)

    assert len(token) <= token_auth_mod.LINK_TOKEN_MAX_CHARS
    assert len(token) + _LINE_AROUND_TOKEN_BYTES < _SLACK_LINE_SPLIT_BYTES
    claims = _claims(token)
    assert set(claims) == {"sub", "exp", "session_exp", "iat", "nonce", "gen"} | set(_EXTRA)
    # Every other claim survives, so the exchange still opens the window for
    # this channel and thread (on_token_consumed reads channel and thread_ts).
    for key, value in _EXTRA.items():
        assert claims[key] == value
    assert validate_token(token)[0]
    assert "link prompt dropped" in caplog.text


def test_a_prompt_that_fits_is_kept() -> None:
    token = generate_token("U1", prompt="what does this error mean?", extra=_EXTRA)

    claims = _claims(token)
    assert claims["prompt"] == "what does this error mean?"


@pytest.mark.asyncio
async def test_a_link_minted_for_a_long_message_still_opens_its_window() -> None:
    """The whole point: the shortened link is exchanged and the observer runs."""
    link = generate_token("U1", prompt="x" * 5_000, extra=_EXTRA)
    consumed = MagicMock()
    ctx = SimpleNamespace(dashboard=SimpleNamespace(on_token_consumed=consumed))
    mw = token_auth_middleware()
    req = _make_request(query={"token": link})
    with patch("kiro_crew.platform.current_context", return_value=ctx):
        resp = await mw(req, _ok_handler)

    assert resp.status == 200
    consumed.assert_called_once()
    assert consumed.call_args.args[:2] == ("U1", "D0123456789")
    assert resp.cookies.get("mc_token_5476") is not None
