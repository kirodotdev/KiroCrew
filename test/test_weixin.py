"""Tests for the Weixin (iLink personal WeChat) channel.

Offline only: no network. The iLink HTTP surface is exercised through a fake
client so the transport's poll/authorize/normalize/send logic is covered
without touching ilinkai.weixin.qq.com.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.weixin.client import (
    ILINK_APP_ID,
    ITEM_TEXT,
    MSG_STATE_FINISH,
    MSG_TYPE_BOT,
    ContextTokenStore,
    TypingTicketCache,
    WeixinClient,
    WeixinSendError,
    _base_info,
    _headers,
    load_weixin_account,
    protocol_error_code,
    save_weixin_account,
)
from kiro_crew.weixin.renderer import (
    WEIXIN_CHUNK_LIMIT,
    normalize_markdown,
    render_chunks,
    split_markdown_blocks,
)
from kiro_crew.weixin.transport import WeixinTransport


# ── protocol headers ──────────────────────────────────────────────────────────
def test_declared_capabilities_do_not_promise_files_without_a_media_path():
    """``files`` must stay False while the transport has no media path.

    The flag is a contract read by capability-aware callers (and, per the
    channel-plugin RFC, eventually by the agent's own tool surface). iLink's
    send path carries text only and inbound media is never decrypted or cached,
    so declaring files=True advertises a capability the transport cannot
    perform. Flip this together with the media implementation, not before.
    """
    from kiro_crew.weixin.transport import WEIXIN_CAPABILITIES

    assert WEIXIN_CAPABILITIES.files_inbound is False
    assert WEIXIN_CAPABILITIES.files_outbound is False


def test_headers_carry_required_ilink_fields():
    h = _headers("abc123", '{"k":1}')
    assert h["AuthorizationType"] == "ilink_bot_token"
    assert h["Authorization"] == "Bearer abc123"
    assert h["Content-Type"] == "application/json"
    assert h["iLink-App-Id"] == ILINK_APP_ID
    assert int(h["iLink-App-ClientVersion"]) > 0
    # Content-Length must be the UTF-8 byte length, not the char count.
    assert h["Content-Length"] == "7"
    assert h["X-WECHAT-UIN"]


def test_headers_reuse_one_uin_across_requests():
    """iLink binds the bot session to the UIN seen at authorization.

    Re-rolling ``X-WECHAT-UIN`` per request made the first ``getupdates``
    long-poll after a QR login come back ``-14`` (session expired), so every
    request in a process must present the same UIN.
    """
    first = _headers("abc123", "{}")["X-WECHAT-UIN"]
    assert all(_headers("abc123", "{}")["X-WECHAT-UIN"] == first for _ in range(5))
    # Independent of token/body, since it identifies the client, not the call.
    assert _headers(None, '{"k":1}')["X-WECHAT-UIN"] == first


def test_headers_omit_authorization_without_credential():
    assert "Authorization" not in _headers(None, "{}")


def test_content_length_counts_utf8_bytes():
    body = '{"t":"你好"}'
    assert _headers(None, body)["Content-Length"] == str(len(body.encode("utf-8")))


def test_base_info_pins_channel_version():
    assert "channel_version" in _base_info()


# ── state stores ──────────────────────────────────────────────────────────────
def test_context_store_round_trips_through_disk(tmp_path):
    store = ContextTokenStore(str(tmp_path))
    store.set("acct1", "userA", "ctx-A")
    store.set("acct1", "userB", "ctx-B")
    assert store.get("acct1", "userA") == "ctx-A"

    # A fresh store restores what the previous one persisted (reply continuity
    # across gateway restarts).
    revived = ContextTokenStore(str(tmp_path))
    assert revived.get("acct1", "userA") is None  # not restored yet
    revived.restore("acct1")
    assert revived.get("acct1", "userA") == "ctx-A"
    assert revived.get("acct1", "userB") == "ctx-B"


def test_context_store_is_scoped_per_account(tmp_path):
    store = ContextTokenStore(str(tmp_path))
    store.set("acct1", "sameuser", "one")
    store.set("acct2", "sameuser", "two")
    assert store.get("acct1", "sameuser") == "one"
    assert store.get("acct2", "sameuser") == "two"


def test_typing_ticket_cache_expires_entries():
    cache = TypingTicketCache(ttl_seconds=0.0)
    cache.set("u1", "ticket")
    assert cache.get("u1") is None  # already stale


def test_typing_ticket_cache_returns_fresh_entries():
    cache = TypingTicketCache(ttl_seconds=600.0)
    cache.set("u1", "ticket")
    assert cache.get("u1") == "ticket"
    assert cache.get("absent") is None


def test_account_credentials_persist_and_round_trip(tmp_path):
    save_weixin_account(
        str(tmp_path), account_id="acct1", token="s3cr3t", base_url="https://x", user_id="u9"
    )
    loaded = load_weixin_account(str(tmp_path), "acct1")
    assert loaded is not None
    assert loaded["base_url"] == "https://x"
    assert loaded["user_id"] == "u9"
    assert load_weixin_account(str(tmp_path), "missing") is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")
def test_account_credentials_are_owner_only(tmp_path):
    """NTFS reports synthetic mode bits, so this guarantee is POSIX-checked;
    cross-platform enforcement comes from platform_compat.restrict_to_owner."""
    save_weixin_account(str(tmp_path), account_id="acct1", token="s3cr3t", base_url="https://x")
    path = tmp_path / "weixin" / "accounts" / "acct1.json"
    assert path.stat().st_mode & 0o077 == 0


# ── renderer ──────────────────────────────────────────────────────────────────
def test_normalize_markdown_collapses_blank_runs_outside_fences():
    assert normalize_markdown("a\n\n\n\nb") == "a\n\nb"


def test_normalize_markdown_preserves_blank_lines_inside_fences():
    src = "```\nx\n\n\n\ny\n```"
    assert normalize_markdown(src).count("\n\n\n") == 1


def test_split_markdown_blocks_keeps_a_fence_as_one_block():
    blocks = split_markdown_blocks("intro\n\n```py\na=1\n\nb=2\n```\n\nouttro")
    assert blocks[0] == "intro"
    assert blocks[1].startswith("```py") and blocks[1].endswith("```")
    assert "a=1" in blocks[1] and "b=2" in blocks[1]
    assert blocks[2] == "outtro"


def test_render_chunks_keeps_short_multiline_replies_as_one_message():
    # Regression: multi-paragraph replies must NOT fan out into many bubbles.
    out = render_chunks("line one\n\nline two\n\nline three")
    assert len(out) == 1


def test_render_chunks_returns_empty_for_blank_content():
    assert render_chunks("") == []
    assert render_chunks("   \n\n ") == []


def test_render_chunks_splits_oversized_content_within_the_limit():
    body = "\n\n".join(["x" * 500] * 20)  # ~10k chars
    out = render_chunks(body)
    assert len(out) > 1
    assert all(len(c) <= WEIXIN_CHUNK_LIMIT for c in out)


def test_render_chunks_hard_splits_a_single_oversized_block():
    out = render_chunks("y" * (WEIXIN_CHUNK_LIMIT * 2 + 10))
    assert len(out) == 3
    assert all(len(c) <= WEIXIN_CHUNK_LIMIT for c in out)


# ── transport ─────────────────────────────────────────────────────────────────
class FakeClient:
    """Records outbound sends; stands in for the iLink HTTP client."""

    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send_message(self, *, to, text, context_token, client_id):
        self.sent.append(
            {"to": to, "text": text, "context_token": context_token, "client_id": client_id}
        )
        return {"errcode": 0}

    async def connect(self):
        return None

    async def close(self):
        self.closed = True


def _make(tmp_path, **kw) -> tuple[WeixinTransport, FakeClient, list[InboundMessage]]:
    got: list[InboundMessage] = []

    async def dispatch(msg: InboundMessage) -> None:
        got.append(msg)

    # The shipped default is deny-by-default ("allowlist" + empty list); these
    # helpers exercise normalization/send, so opt in explicitly unless the test
    # is specifically about authorization.
    kw.setdefault("dm_policy", "open")
    client = FakeClient()
    t = WeixinTransport(
        client,
        account_id="acct1",
        ctx_store=ContextTokenStore(str(tmp_path)),
        dispatch=dispatch,
        **kw,
    )
    return t, client, got


def test_transport_declares_channel_type_and_dm_capabilities(tmp_path):
    t, _, _ = _make(tmp_path)
    assert t.channel_type == "weixin"
    assert t.capabilities.max_message_chars == WEIXIN_CHUNK_LIMIT
    assert t.capabilities.threads is False  # iLink DM-only


def test_authorize_denies_everyone_when_policy_disabled(tmp_path):
    t, _, _ = _make(tmp_path, dm_policy="disabled")
    msg = InboundMessage(channel_type="weixin", user_id="u1", conversation_id="u1", text="hi")
    assert t.authorize(msg) is False


def test_authorize_allowlist_is_deny_by_default_when_empty(tmp_path):
    t, _, _ = _make(tmp_path, dm_policy="allowlist", allowed_user_ids=[])
    msg = InboundMessage(channel_type="weixin", user_id="u1", conversation_id="u1", text="hi")
    assert t.authorize(msg) is False


def test_authorize_allowlist_admits_only_listed_users(tmp_path):
    t, _, _ = _make(tmp_path, dm_policy="allowlist", allowed_user_ids=["good"])
    ok = InboundMessage(channel_type="weixin", user_id="good", conversation_id="good", text="hi")
    bad = InboundMessage(channel_type="weixin", user_id="bad", conversation_id="bad", text="hi")
    assert t.authorize(ok) is True
    assert t.authorize(bad) is False


def test_authorize_open_policy_still_requires_a_sender_id(tmp_path):
    t, _, _ = _make(tmp_path, dm_policy="open")
    anon = InboundMessage(channel_type="weixin", user_id="", conversation_id="", text="hi")
    assert t.authorize(anon) is False


def test_authorize_denies_on_an_unknown_dm_policy(tmp_path):
    """A typo'd policy must fail CLOSED, never fall through to 'open'."""
    t, _, _ = _make(tmp_path, dm_policy="opne")  # typo
    msg = InboundMessage(channel_type="weixin", user_id="u1", conversation_id="u1", text="hi")
    assert t.authorize(msg) is False


def test_transport_default_policy_is_deny_by_default(tmp_path):
    """Constructing without a policy must NOT authorize arbitrary senders."""
    got: list[InboundMessage] = []

    async def dispatch(m):
        got.append(m)

    t = WeixinTransport(
        FakeClient(),
        account_id="acct1",
        ctx_store=ContextTokenStore(str(tmp_path)),
        dispatch=dispatch,
    )
    msg = InboundMessage(channel_type="weixin", user_id="u1", conversation_id="u1", text="hi")
    assert t.authorize(msg) is False


def test_config_dm_policy_defaults_to_allowlist():
    """A fresh QR login must not expose the agent to every sender."""
    from kiro_crew.config.loader import WeixinConfig

    assert WeixinConfig().dm_policy == "allowlist"
    assert WeixinConfig().allowed_user_ids == []


def test_receive_normalizes_a_text_item_and_stores_the_context(tmp_path):
    t, _, got = _make(tmp_path)
    envelope = {
        "from_user_id": "userA",
        "msg_id": "m1",
        "context_token": "ctx-1",
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hello bot"}}],
    }
    asyncio.run(t.receive(envelope))
    assert len(got) == 1
    assert got[0].channel_type == "weixin"
    assert got[0].user_id == "userA"
    assert got[0].conversation_id == "userA"  # DM: peer id IS the conversation
    assert got[0].text == "hello bot"
    # context_token persisted for the reply path
    assert t._ctx.get("acct1", "userA") == "ctx-1"


def test_receive_deduplicates_repeated_message_ids(tmp_path):
    t, _, got = _make(tmp_path)
    envelope = {
        "from_user_id": "userA",
        "msg_id": "dup",
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "once"}}],
    }
    asyncio.run(t.receive(envelope))
    asyncio.run(t.receive(envelope))
    assert len(got) == 1


def test_receive_drops_envelopes_without_a_sender_or_text(tmp_path):
    t, _, got = _make(tmp_path)
    asyncio.run(t.receive({"item_list": [{"type": ITEM_TEXT, "text_item": {"text": "x"}}]}))
    asyncio.run(t.receive({"from_user_id": "u", "item_list": []}))
    asyncio.run(t.receive("not-a-dict"))
    assert got == []


def test_receive_drops_unauthorized_senders_before_dispatch(tmp_path):
    t, _, got = _make(tmp_path, dm_policy="allowlist", allowed_user_ids=["good"])
    asyncio.run(
        t.receive(
            {
                "from_user_id": "intruder",
                "msg_id": "m2",
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "let me in"}}],
            }
        )
    )
    assert got == []


def test_open_policy_exposes_an_authorized_peer_as_a_dashboard_target(tmp_path):
    t, _, _ = _make(tmp_path, dm_policy="open")
    asyncio.run(
        t.receive(
            {
                "from_user_id": "friend",
                "msg_id": "m-target",
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hello"}}],
            }
        )
    )

    assert [target.target_id for target in t.configured_targets()] == ["user:friend"]
    assert asyncio.run(t.resolve_configured_target("user:friend")) == ("friend", None)


def test_send_message_echoes_the_stored_context(tmp_path):
    t, client, _ = _make(tmp_path)
    t._ctx.set("acct1", "userA", "ctx-9")
    asyncio.run(t.send_message("userA", "hi there"))
    assert len(client.sent) == 1
    assert client.sent[0]["to"] == "userA"
    assert client.sent[0]["context_token"] == "ctx-9"
    assert client.sent[0]["client_id"]


def test_send_message_chunks_oversized_replies(tmp_path):
    t, client, _ = _make(tmp_path)
    asyncio.run(t.send_message("userA", "\n\n".join(["z" * 500] * 20)))
    assert len(client.sent) > 1
    assert all(len(c["text"]) <= WEIXIN_CHUNK_LIMIT for c in client.sent)


def test_resolve_conversation_is_the_peer_id(tmp_path):
    t, _, _ = _make(tmp_path)
    assert asyncio.run(t.resolve_conversation("userA")) == "userA"


def test_fetch_history_is_empty_because_ilink_has_no_paging(tmp_path):
    t, _, _ = _make(tmp_path)
    assert asyncio.run(t.fetch_history("userA")) == []


def test_gateway_home_follows_the_configured_data_home(tmp_path, monkeypatch):
    """Peer context state must live under the ACTIVE data home.

    A hardcoded expanduser() would send isolated profiles (KIROCREW_HOME, the dev
    backend, tests) to the shared default home, where they would overwrite each
    other's reply state.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "isolated"))
    from kiro_crew.config import paths as paths_mod

    resolved = paths_mod.data_home()
    assert str(tmp_path / "isolated") in str(resolved)

    # The gateway must resolve the home through the canonical helper, not a
    # hardcoded path, so overriding the data home actually moves the state.
    import kiro_crew.weixin.gateway as gw

    assert gw.data_home is paths_mod.data_home


def test_rejected_sender_does_not_get_context_persisted(tmp_path, monkeypatch):
    """An unallowlisted stranger must not be able to mutate server-side state.

    The reply-context store is a JSON file on disk; writing it before the
    authorize() check would let anyone who can message the bot grow it at will.
    """
    t, _, got = _make(tmp_path, dm_policy="allowlist", allowed_user_ids=set())
    asyncio.run(
        t.receive(
            {
                "from_user_id": "stranger",
                "msg_id": "m-denied",
                "context_token": "ctx-should-not-persist",
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hello?"}}],
            }
        )
    )
    assert got == []  # never dispatched
    assert not t._ctx.get("acct1", "stranger")  # and nothing persisted


def test_allowed_sender_still_gets_context_persisted(tmp_path):
    t, _, got = _make(tmp_path, dm_policy="allowlist", allowed_user_ids={"friend"})
    asyncio.run(
        t.receive(
            {
                "from_user_id": "friend",
                "msg_id": "m-ok",
                "context_token": "ctx-keep",
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hi"}}],
            }
        )
    )
    assert len(got) == 1
    assert t._ctx.get("acct1", "friend") == "ctx-keep"


def test_options_trailer_is_stripped_from_the_end():
    from kiro_crew.weixin.turn_renderer import _strip_options

    assert _strip_options("Here you go.\n\n[OPTIONS: Yes | No]") == "Here you go."


def test_options_stripping_never_deletes_mid_response_content():
    """Regression: a non-trailing "[OPTIONS:" must not swallow the body.

    The hand-rolled MULTILINE|DOTALL pattern let ``.*?`` span newlines, so an
    ``[OPTIONS:`` quoted mid-answer matched a ``]`` further down and everything
    between was silently deleted from the user's reply. The shared
    OPTIONS_RE_TRAILER anchors to end-of-string instead.
    """
    from kiro_crew.weixin.turn_renderer import _strip_options

    body = (
        "The dashboard renders `[OPTIONS: a | b]` as tappable chips.\n"
        "Important paragraph that must survive.\n"
        "A list: [1] first [2] second\n"
    )
    out = _strip_options(body + "\n[OPTIONS: Keep | Discard]")
    assert "Important paragraph that must survive." in out
    assert "[1] first [2] second" in out
    assert "Keep | Discard" not in out


def test_text_without_a_trailer_is_untouched():
    from kiro_crew.weixin.turn_renderer import _strip_options

    assert _strip_options("just an answer") == "just an answer"


def test_poll_loop_backs_off_on_ret_keyed_session_expiry(tmp_path, monkeypatch):
    """A `ret`-keyed error must hit a backoff, not re-poll immediately.

    getupdates reports failure as ``ret`` (not ``errcode``) and returns HTTP 200,
    so reading only ``errcode`` let session-expiry / rate-limit responses fall
    through every backoff branch into a zero-delay re-poll — an unbounded hot
    loop against an API that limits (and can ban) the account.
    """
    t, client, _ = _make(tmp_path)
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)
        t._running = False  # stop after the first backoff

    async def expired(_buf):
        return {"ret": -14}

    client.get_updates = expired  # type: ignore[assignment]
    monkeypatch.setattr("kiro_crew.weixin.transport.asyncio.sleep", fake_sleep)
    t._running = True
    asyncio.run(t._poll_loop())
    assert sleeps == [600]  # the long session-expiry pause, not a spin


def test_poll_loop_backs_off_on_ret_keyed_rate_limit(tmp_path, monkeypatch):
    t, client, _ = _make(tmp_path)
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)
        t._running = False

    async def limited(_buf):
        return {"ret": -2}

    client.get_updates = limited  # type: ignore[assignment]
    monkeypatch.setattr("kiro_crew.weixin.transport.asyncio.sleep", fake_sleep)
    t._running = True
    asyncio.run(t._poll_loop())
    assert sleeps == [30]


def test_poll_loop_backs_off_on_an_unknown_error_code(tmp_path, monkeypatch):
    """Unknown nonzero codes must back off too, or they spin the same way."""
    t, client, _ = _make(tmp_path)
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)
        t._running = False

    async def weird(_buf):
        return {"ret": -999}

    client.get_updates = weird  # type: ignore[assignment]
    monkeypatch.setattr("kiro_crew.weixin.transport.asyncio.sleep", fake_sleep)
    t._running = True
    asyncio.run(t._poll_loop())
    assert sleeps and sleeps[0] > 0


def test_outbound_message_shape_marks_bot_and_finished():
    # Guards the iLink contract the send path depends on.
    assert MSG_TYPE_BOT == 2
    assert MSG_STATE_FINISH == 2
    assert ITEM_TEXT == 1


# ── protocol-level error detection ────────────────────────────────────────────
def testprotocol_error_code_treats_zero_and_absent_as_success():
    assert protocol_error_code({}) is None
    assert protocol_error_code({"errcode": 0}) is None
    assert protocol_error_code({"ret": 0}) is None
    assert protocol_error_code({"errcode": 0, "ret": 0, "msgs": []}) is None


def testprotocol_error_code_detects_either_key():
    assert protocol_error_code({"errcode": -2}) == -2
    assert protocol_error_code({"ret": -14}) == -14


def test_send_message_raises_on_a_rejected_message(tmp_path):
    """A 200 with nonzero errcode means NOT delivered — must not look like success.

    Regression: the renderer/dispatcher would otherwise persist a reply the user
    never received.
    """
    client = WeixinClient(token="t", account_id="acct1")

    async def fake_post(endpoint, payload, *, timeout_ms):
        return {"errcode": -2, "errmsg": "frequency limit"}

    client._post = fake_post  # type: ignore[assignment]

    async def go():
        with pytest.raises(WeixinSendError) as exc:
            await client.send_message(to="userA", text="hi", context_token=None, client_id="c1")
        assert exc.value.code == -2

    asyncio.run(go())


def test_send_message_returns_the_payload_on_success():
    client = WeixinClient(token="t", account_id="acct1")

    async def fake_post(endpoint, payload, *, timeout_ms):
        return {"errcode": 0, "msg_id": "m1"}

    client._post = fake_post  # type: ignore[assignment]
    out = asyncio.run(
        client.send_message(to="userA", text="hi", context_token="ctx", client_id="c1")
    )
    assert out["msg_id"] == "m1"


def test_receive_persists_the_context_off_the_event_loop(tmp_path, monkeypatch):
    """The store rewrites a JSON file; doing it inline would stall the poll loop."""
    import kiro_crew.weixin.transport as mod

    calls: list[tuple] = []
    real_to_thread = mod.asyncio.to_thread

    async def spy(fn, *args):
        calls.append(args)
        return await real_to_thread(fn, *args)

    monkeypatch.setattr(mod.asyncio, "to_thread", spy)
    t, _, got = _make(tmp_path)
    asyncio.run(
        t.receive(
            {
                "from_user_id": "userA",
                "msg_id": "m9",
                "context_token": "ctx-off-loop",
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hi"}}],
            }
        )
    )
    assert calls == [("acct1", "userA", "ctx-off-loop")]
    assert t._ctx.get("acct1", "userA") == "ctx-off-loop"
    assert len(got) == 1
