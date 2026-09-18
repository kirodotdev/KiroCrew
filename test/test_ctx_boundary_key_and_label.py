"""The /context boundary: what a context key and a source label may be.

A ``contextKey`` is an IDENTITY the live-queue dedup compares, so anything that silently
rewrites it before comparison aliases two distinct keys onto one and drops a post the
API already answered 200 for. These pin the refusals that keep the key verbatim, the
ownership and content rules that decide when a repost is genuinely a repeat, and the
drain's refusal to deliver a keyed entry into a session it was not posted under.
"""

from __future__ import annotations

import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_utils import effective_session_key
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

    No ``ephemeral`` key, because these fixtures are appended to the queue directly rather
    than built by the endpoint, and no assertion here reads it.
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


@pytest.mark.asyncio
async def test_an_overlong_context_key_is_refused_not_truncated(tmp_path, monkeypatch):
    """Clipping the key to the cap aliased two distinct keys and dropped a post.

    The key is an IDENTITY the dedup compares. Truncating it to ``MAX_SOURCE_LEN`` made two
    keys sharing a 64-char prefix collapse onto one, so the second post matched the first,
    answered 200 and appended nothing -- acknowledged and silently lost. ``source`` is already
    refused at the same limit, so refusal is the existing convention rather than a new one.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    from kiro_crew.dashboard.chat_handlers import _MAX_SOURCE_LEN as MAX_SOURCE_LEN

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
    character and validation passes, leaving a newline inside an IDENTITY.
    ``_validate_source`` already checks the raw value before stripping to honour the
    documented contract, so checking the raw value here follows that convention rather
    than inventing a second one.
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
@pytest.mark.parametrize("padded", [" v7", "v7 ", "\tv7", "v7\t", "  v7  "])
async def test_a_whitespace_padded_context_key_is_refused_not_stripped(
    tmp_path, monkeypatch, padded
):
    """Stripping the key collapsed ``" v7"`` and ``"v7"`` onto one identity.

    The dedup compares the key AFTER a strip, so a caller whose key carries accidental
    padding matched the earlier entry, was answered 200, and appended nothing -- distinct
    content acknowledged and silently dropped, with no surface reporting it. Newlines were
    already refused, so space and tab padding was the whole residual.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

    state = _make_state(tmp_path)
    key = "chat-ctx-padkey"
    _seed(state, key, [])

    async with TestClient(TestServer(_context_app(state))) as client:
        first = await client.post(
            "/api/chat/slots/" + key + "/context",
            json={"content": "alpha", "source": "artifact-companion", "contextKey": "v7"},
        )
        assert first.status == 200, await first.text()

        resp = await client.post(
            "/api/chat/slots/" + key + "/context",
            json={"content": "beta", "source": "artifact-companion", "contextKey": padded},
        )
        assert resp.status == 400, (
            f"a whitespace-padded contextKey {padded!r} was accepted ({resp.status}); it then "
            "strips onto the earlier key, so this post answers 200, queues nothing, and its "
            "distinct content is lost with no surface reporting it"
        )
        assert (await resp.json())["code"] == "invalid_context_key"

    # The refusal, not a silent dedup match, is what stopped the second post.
    assert [e["content"] for e in state._slots[key]._pending_context] == ["alpha"]

    # DISCRIMINATING CONTROL: the UNPADDED spelling is still accepted, and an exact repeat of
    # it is still suppressed, so the refusal is a padding rule rather than the key disabled.
    async with TestClient(TestServer(_context_app(state))) as client:
        clean = await client.post(
            "/api/chat/slots/" + key + "/context",
            json={"content": "gamma", "source": "artifact-companion", "contextKey": "v8"},
        )
        assert clean.status == 200, await clean.text()
        repeat = await client.post(
            "/api/chat/slots/" + key + "/context",
            json={"content": "gamma", "source": "artifact-companion", "contextKey": "v8"},
        )
        assert repeat.status == 200, await repeat.text()
    assert [e["content"] for e in state._slots[key]._pending_context] == [
        "alpha",
        "gamma",
    ], "an exact repost must still be suppressed, or the fix disabled the dedup itself"


@pytest.mark.asyncio
async def test_an_expired_key_does_not_falsely_acknowledge_a_repost(tmp_path, monkeypatch):
    """The dedup matched an EXPIRED entry, so a repost was acknowledged and lost.

    An expired entry is discarded by the drain rather than delivered. Suppressing on it answered
    200 to a caller whose replacement content then reached the model never -- the
    acknowledged-then-dropped defect this change exists to close, reached through the dedup.

    The seeded row carries a ``ctxSession`` stamp for the live session so EXPIRY stays the only
    reason it fails to suppress: an unstamped row is not owned-current, and would be skipped for
    that reason instead, leaving the expiry rule untested.
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
            "ctxSession": effective_session_key(slot),
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


@pytest.mark.asyncio
async def test_a_rebound_slot_does_not_treat_the_old_sessions_entry_as_ours(tmp_path, monkeypatch):
    """Ownership read as the ABSENCE of a foreign stamp, so a rebind kept matching the old entry.

    A /context entry carried no session stamp at all, and the dedup asked only whether some
    OTHER session had claimed it. An entry queued before a cron bound this slot therefore still
    counted as owned under the NEW session: a same-key repost matched it, answered 200 and queued
    nothing, so the replacement content reached the model never. Both shapes below must fail to
    suppress -- one stamped for the pre-rebind session, and one carrying no stamp, which is what
    a reload leaves behind because the restore rebuilds entries from the known keys only.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    key = "chat-ctx-rebind"
    slot = _seed(state, key, [])
    before_bind = effective_session_key(slot)

    slot.append_pending_context(
        _entry("pre-bind a", source="artifact-companion", contextKey="a", ctxSession=before_bind)
    )
    slot.append_pending_context(_entry("reloaded b", source="artifact-companion", contextKey="b"))

    slot.linked_session_key = "cron:job42"
    assert effective_session_key(slot) != before_bind, "precondition: the rebind moved the session"

    async with TestClient(TestServer(_context_app(state))) as client:
        for ck, content in (("a", "fresh a"), ("b", "fresh b")):
            resp = await client.post(
                "/api/chat/slots/" + key + "/context",
                json={"content": content, "source": "artifact-companion", "contextKey": ck},
            )
            assert resp.status == 200, await resp.text()

    queued = [e.get("content") for e in state._slots[key]._pending_context]
    assert "fresh a" in queued, (
        "an entry stamped for the PRE-REBIND session suppressed the repost, so it was "
        "acknowledged with 200 and its content never reaches the model"
    )
    assert "fresh b" in queued, (
        "an UNSTAMPED entry suppressed the repost, so a slot that merely reloaded swallows "
        "every later post under that key"
    )

    # DISCRIMINATING CONTROL: an exact repost under an UNCHANGED session still suppresses, so
    # the fix narrowed ownership rather than disabling the keyed dedup outright.
    before = len(state._slots[key]._pending_context)
    async with TestClient(TestServer(_context_app(state))) as client:
        again = await client.post(
            "/api/chat/slots/" + key + "/context",
            json={"content": "fresh a", "source": "artifact-companion", "contextKey": "a"},
        )
        assert again.status == 200, await again.text()
    assert (
        len(state._slots[key]._pending_context) == before
    ), "a duplicate was queued under an unchanged session, so the keyed dedup is gone"


@pytest.mark.asyncio
async def test_the_drain_will_not_deliver_a_keyed_entry_into_a_session_it_left(
    tmp_path, monkeypatch
):
    """The stamp gated only the dedup, so a rebind still delivered the old session's content.

    ``drop_foreign_authorized_notes`` keys on ``noteSession``, which only /note writes, so a
    /context entry stamped for the pre-rebind session passed it untouched and the drain injected
    that content into the session the slot had since moved to -- content crossing a boundary the
    POST had already recorded, with nothing reporting it.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    from kiro_crew.dashboard.chat_runner import drain_pending_context

    state = _make_state(tmp_path)
    key = "chat-ctx-drainrebind"
    slot = _seed(state, key, [])
    before_bind = effective_session_key(slot)

    async with TestClient(TestServer(_context_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/" + key + "/context",
            json={"content": "pre-bind snap", "source": "artifact-companion", "contextKey": "a"},
        )
        assert resp.status == 200, await resp.text()
    assert (
        slot._pending_context[0].get("ctxSession") == before_bind
    ), "precondition: the POST stamped the session it was made under"

    slot.linked_session_key = "cron:job42"
    assert effective_session_key(slot) != before_bind, "precondition: the rebind moved the session"

    assert "pre-bind snap" not in drain_pending_context(slot), (
        "an entry posted under the PREVIOUS session was injected into the new one, so keyed "
        "context crossed a session boundary the POST had already recorded"
    )

    # DISCRIMINATING CONTROL: an UNSTAMPED entry still drains, so the rule keys on a FOREIGN
    # stamp rather than withholding every queued entry once a slot has been rebound.
    slot.append_pending_context(_entry("unstamped", source="artifact-companion"))
    assert "unstamped" in drain_pending_context(slot), (
        "an entry carrying no session stamp was withheld, so the filter suppresses ordinary "
        "context instead of only what belongs to another session"
    )

    # ... and one stamped for the CURRENT session drains, so the stamp is compared rather than
    # merely required to be absent.
    slot.append_pending_context(
        _entry(
            "current snap",
            source="artifact-companion",
            contextKey="c",
            ctxSession=effective_session_key(slot),
        )
    )
    assert "current snap" in drain_pending_context(slot), (
        "an entry stamped for the LIVE session was withheld, so keyed context is never "
        "deliverable at all and the dedup guards a queue nothing can leave"
    )


@pytest.mark.asyncio
async def test_a_changed_snapshot_under_a_reused_key_is_queued_not_acknowledged(
    tmp_path, monkeypatch
):
    """The match ignored ``content``, so an updated snapshot was answered 200 and dropped.

    A caller that reuses one key for successive snapshots is the whole point of naming a post,
    and suppression compared key and source only. The second, DIFFERENT snapshot therefore
    matched the first, answered 200, and appended nothing -- the acknowledged-then-dropped
    class this change exists to close, reached through the dedup itself.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    key = "chat-ctx-changedsnap"
    _seed(state, key, [])

    async with TestClient(TestServer(_context_app(state))) as client:
        for content in ("alpha", "beta"):
            resp = await client.post(
                "/api/chat/slots/" + key + "/context",
                json={"content": content, "source": "artifact-companion", "contextKey": "v7"},
            )
            assert resp.status == 200, await resp.text()

    assert [e["content"] for e in state._slots[key]._pending_context] == ["alpha", "beta"], (
        "a CHANGED snapshot under a reused key was suppressed, so it was acknowledged with 200 "
        "and its content never reaches the model"
    )

    # DISCRIMINATING CONTROL: an EXACT repeat is still suppressed, so the rule narrowed the
    # match to identical content rather than removing the dedup.
    before = len(state._slots[key]._pending_context)
    async with TestClient(TestServer(_context_app(state))) as client:
        again = await client.post(
            "/api/chat/slots/" + key + "/context",
            json={"content": "beta", "source": "artifact-companion", "contextKey": "v7"},
        )
        assert again.status == 200, await again.text()
    assert (
        len(state._slots[key]._pending_context) == before
    ), "an exact repeat was queued twice, so the keyed dedup no longer suppresses anything"
