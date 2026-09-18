"""The /context boundary: what a context key and a source label may be.

A ``contextKey`` is an IDENTITY the reload-dedup compares, so anything that silently
rewrites it before comparison aliases two distinct keys onto one and drops a post the
API already answered 200 for. These pin the refusals that keep the key verbatim, the
source-label sanitisation that stops a crafted label forging a prompt frame, and the
malformed-TTL arithmetic that must report EXPIRED rather than immortal.
"""

from __future__ import annotations

import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.state import _ChatSlot


def _entry(
    content: str,
    *,
    source: str = "test",
    max_age: float | None = 86400,
    injected_at: float | None = None,
    **extra: object,
) -> dict:
    """A pending-context entry in the shape `_build_pending_context_entry` produces.

    No ``ephemeral`` key: the builder omits it unless a caller asks, and it means
    MEMORY-ONLY, so stamping every fixture entry would withhold the whole queue from disk.
    """
    e: dict = {
        "content": content,
        "source": source,
        "injectedAt": time.time() if injected_at is None else injected_at,
        "maxAge": max_age,
    }
    e.update(extra)
    return e


def _seed(state, key: str, entries: list[dict]) -> _ChatSlot:
    """A titled, published slot carrying *entries*."""
    slot = _ChatSlot(key)
    slot.title = f"title-{key}"
    slot._titled = True
    slot.append(role="user", content="a real message", cls="msg msg-u")
    for e in entries:
        slot.append_pending_context(e)
    state._slots[key] = slot
    return slot


def _context_app(state):
    from kiro_crew.dashboard.chat import api_chat_slot_context

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/context", api_chat_slot_context)
    return app


@pytest.mark.parametrize("bad", ["60", [1], True, float("nan"), float("inf")])
def test_context_entry_expired_never_raises_on_a_bad_max_age(bad):
    """Hardened at the arithmetic itself, so every caller is protected.

    A malformed value reports EXPIRED rather than "never expires": unparseable
    data must be pruned, not made immortal.
    """
    from kiro_crew.dashboard.state import context_entry_expired

    assert context_entry_expired({"content": "x", "maxAge": bad}, time.time()) is True


@pytest.mark.asyncio
async def test_an_overlong_context_key_is_refused_not_truncated(tmp_path, monkeypatch):
    """Clipping the key to the cap aliased two distinct keys and dropped a post.

    The key is an IDENTITY the dedup compares. Truncating it to ``MAX_SOURCE_LEN`` made two
    keys sharing a 64-char prefix collapse onto one, so the second post matched the first,
    answered 200 and appended nothing -- acknowledged and silently lost. ``source`` is already
    refused at the same limit, so refusal is the existing convention rather than a new one.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    from kiro_crew.dashboard.state import MAX_SOURCE_LEN

    state = _make_state(tmp_path)
    key = "chat-ctx-longkey"
    _seed(state, key, [])

    prefix = "k" * MAX_SOURCE_LEN
    first = prefix + "-alpha"
    second = prefix + "-beta"
    assert first[:MAX_SOURCE_LEN] == second[:MAX_SOURCE_LEN], "precondition: they alias on clip"

    async with TestClient(TestServer(_context_app(state))) as client:
        for ck in (first, second):
            resp = await client.post(
                "/api/chat/slots/" + key + "/context",
                json={"content": "c " + ck[-5:], "source": "artifact-companion", "contextKey": ck},
            )
            assert resp.status == 400, (
                f"an overlong contextKey was accepted ({resp.status}); truncation then aliases "
                "it onto its sibling and the second post is dropped with a 200"
            )
            assert (await resp.json())["code"] == "context_key_too_long"

    assert not state._slots[key]._pending_context, "a refused post must queue nothing"

    # DISCRIMINATING CONTROL: a key AT the limit is still accepted, so the refusal is a length
    # rule rather than the key having been disabled outright.
    async with TestClient(TestServer(_context_app(state))) as client:
        ok = await client.post(
            "/api/chat/slots/" + key + "/context",
            json={"content": "at the cap", "source": "artifact-companion", "contextKey": prefix},
        )
        assert ok.status == 200, await ok.text()
    assert len(state._slots[key]._pending_context) == 1


@pytest.mark.asyncio
async def test_a_context_key_with_a_leading_newline_is_refused_before_stripping(
    tmp_path, monkeypatch
):
    """The control-char check ran on the STRIPPED key, so a newline slipped past.

    ``"\\nkey"`` strips to ``"key"``, so a check on the stripped form finds no control
    character and validation passes. The dedup then strips the key too and matches the
    earlier ``"key"`` entry, answering 200 while appending nothing -- the second post's
    content is acknowledged and silently dropped. ``_validate_source`` already checks the
    raw value before stripping to honour the documented contract, so checking the raw value
    here follows that convention rather than inventing a second one.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

    state = _make_state(tmp_path)
    key = "chat-ctx-ctrlkey"
    _seed(state, key, [])

    async with TestClient(TestServer(_context_app(state))) as client:
        first = await client.post(
            "/api/chat/slots/" + key + "/context",
            json={"content": "alpha", "source": "artifact-companion", "contextKey": "v7"},
        )
        assert first.status == 200, await first.text()
        assert len(state._slots[key]._pending_context) == 1

        padded = await client.post(
            "/api/chat/slots/" + key + "/context",
            json={"content": "beta", "source": "artifact-companion", "contextKey": "\nv7"},
        )
        assert padded.status == 400, (
            f"a contextKey carrying a leading newline was accepted ({padded.status}); it then "
            "strips onto the earlier key, so this post answers 200 and queues nothing and its "
            "content is lost with no surface reporting it"
        )
        assert (await padded.json())["code"] == "invalid_context_key"

    # The content must not have been swallowed: still exactly the first entry, and the
    # refusal is what stopped the second rather than a silent dedup match.
    assert [e["content"] for e in state._slots[key]._pending_context] == ["alpha"]

    # DISCRIMINATING CONTROL: a clean, genuinely distinct key is still accepted, so the
    # refusal is a control-character rule and not the key having been disabled outright.
    async with TestClient(TestServer(_context_app(state))) as client:
        ok = await client.post(
            "/api/chat/slots/" + key + "/context",
            json={"content": "gamma", "source": "artifact-companion", "contextKey": "v8"},
        )
        assert ok.status == 200, await ok.text()
    assert [e["content"] for e in state._slots[key]._pending_context] == ["alpha", "gamma"]


@pytest.mark.asyncio
async def test_an_expired_key_does_not_falsely_acknowledge_a_repost(tmp_path, monkeypatch):
    """The dedup matched an EXPIRED entry, so a repost was acknowledged and lost.

    An expired entry is discarded by the drain rather than delivered. Suppressing on it answered
    200 to a caller whose replacement content then reached the model never -- the
    acknowledged-then-dropped defect this change exists to close, reached through the dedup.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    key = "chat-ctx-expiredkey"
    slot = _seed(state, key, [])
    # Seated DIRECTLY so it survives to the check: `append_pending_context` reclaims expired
    # entries on the way in, which would remove the very row under test.
    slot._pending_context.append(
        {
            "content": "stale v7 snapshot",
            "source": "artifact-companion",
            "contextKey": "7",
            "injectedAt": time.time() - 7200,
            "maxAge": 60,
        }
    )
    body = {
        "content": "fresh v7 snapshot",
        "source": "artifact-companion",
        "maxAge": 3600,
        "contextKey": "7",
    }

    async with TestClient(TestServer(_context_app(state))) as client:
        resp = await client.post("/api/chat/slots/" + key + "/context", json=body)
        assert resp.status == 200, await resp.text()

    live = state._slots[key]
    _fresh = [e for e in live._pending_context if e.get("content") == "fresh v7 snapshot"]
    assert _fresh, (
        "the repost was suppressed by an EXPIRED entry carrying the same key, so it was "
        "acknowledged with 200 and its content never reaches the model"
    )

    # DISCRIMINATING CONTROL: an UNEXPIRED entry with that key still suppresses, or the fix
    # has simply disabled the dedup the previous round added.
    async with TestClient(TestServer(_context_app(state))) as client:
        before = len(state._slots[key]._pending_context)
        again = await client.post("/api/chat/slots/" + key + "/context", json=body)
        assert again.status == 200
        assert (
            len(state._slots[key]._pending_context) == before
        ), "a live duplicate was queued, so the reload suppression is gone"
