"""A rejected MCP sign-in banner is shown once per server per sign-in state.

kiro-cli retries a signed-out server's OAuth on every turn, and the ACP layer's
dedupe is cleared by the server's init failure, so without the chat's own check
every user turn and every auto-nudge cycle posts the same "authentication
failed" banner again. These tests drive the dashboard's two real entry points (the session-init drain
and the mid-turn ``mcp_oauth`` arm) and check what changes do and do not bring
the banner back.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew import mcp_discovery
from kiro_crew.dashboard import mcp_banner_dedupe
from kiro_crew.dashboard.chat_runner import (
    _drain_session_init_oauth_requests,
    _emit_mcp_oauth_request,
    _mark_mcp_oauth_completed,
    _on_mcp_oauth_request,
)
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.security import oauth_url_contains_credential

# A consent URL the banner gate rejects: an unlisted IdP whose PKCE and state
# push the query past the length gate (same shape as test_mcp_oauth_banner's
# Cloudflare Access fixture).
_QUERY = (
    "?response_type=code&client_id=Qm9hcmRJZGVudGl0eUNsaWVudA"
    "&redirect_uri=http%3A%2F%2F127.0.0.1%3A49152%2Fcallback"
    "&scope=openid%20profile%20email%20offline_access"
    "&state=Zk3pQ9rT2vWx8yLmN4bC7dHs1aJ6eUoI"
    "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    "&code_challenge_method=S256"
)
POSTHOG_URL = "https://oauth.posthog-idp.example/oauth/authorize/" + _QUERY
OTHER_IDP_URL = "https://idp.other.example/authorize" + _QUERY
GOOD_URL = "https://mcp.linear.app/authorize?client_id=abc"


class _State:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, dict]] = []

    def broadcast_ws(self, kind: str, payload: dict) -> None:
        self.broadcasts.append((kind, payload))


class _Client:
    process_instance = "child-1"

    def __init__(self, session_id: str = "sess-1", pending=None) -> None:
        self.session_id = session_id
        self.client = SimpleNamespace(pop_pending_oauth_requests=lambda: list(pending or []))

    def mcp_session_report(self):
        return None


def _turn(slot: _ChatSlot, session_id: str = "sess-1", state=None):
    return SimpleNamespace(client=_Client(session_id), slot=slot, state=state or _State())


def _event(server_name: str, url: str):
    return SimpleNamespace(
        server_name=server_name,
        oauth_url=url,
        kind="mcp_oauth_request",
        runtime_global=False,
    )


def _rejections(slot: _ChatSlot, server: str | None = None) -> list[dict]:
    return [
        m
        for m in slot.messages
        if m.get("role") == "mcp_oauth"
        and m["meta"].get("rejected_url")
        and (server is None or m["meta"].get("server_name") == server)
    ]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    # No server is Connections-owned, and every test starts with no retry rounds.
    patch = _floor_monkeypatch
    patch.setattr(mcp_discovery, "_MCP_JSON_PATHS", (tmp_path / "absent.json",))
    patch.setattr(mcp_banner_dedupe, "_sign_in_rounds", {})
    patch.setattr(mcp_banner_dedupe, "_generation", 0)
    patch.setattr(mcp_banner_dedupe, "_evicted_floor", 0)
    assert oauth_url_contains_credential(POSTHOG_URL), "fixture must trip the gate"
    assert oauth_url_contains_credential(OTHER_IDP_URL), "fixture must trip the gate"


@pytest.mark.asyncio
async def test_unchanged_rejection_is_not_reposted_on_later_turns():
    """The reported flood: the same rejection on each user or nudge turn."""
    slot = _ChatSlot("s1")
    state = _State()
    for i in range(6):
        # Alternate user turns and monitor / auto-nudge wakes on one session.
        turn = _turn(slot, state=state)
        turn._is_monitor_wake = bool(i % 2)
        await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
    assert len(_rejections(slot)) == 1


@pytest.mark.asyncio
async def test_init_drain_and_mid_turn_arm_share_one_record():
    """A banner from session init is not repeated by the next turn's retry."""
    slot = _ChatSlot("s1")
    pending = [{"serverName": "posthog", "oauthUrl": POSTHOG_URL}]
    await _drain_session_init_oauth_requests(_State(), slot, _Client(pending=pending))
    await _on_mcp_oauth_request(_turn(slot), _event("posthog", POSTHOG_URL))
    await _drain_session_init_oauth_requests(_State(), slot, _Client(pending=pending))
    assert len(_rejections(slot)) == 1


@pytest.mark.asyncio
async def test_two_servers_failing_at_once_each_show_once():
    slot = _ChatSlot("s1")
    turn = _turn(slot)
    for _ in range(3):
        await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
        await _on_mcp_oauth_request(turn, _event("other", OTHER_IDP_URL))
    assert len(_rejections(slot, "posthog")) == 1
    assert len(_rejections(slot, "other")) == 1


@pytest.mark.asyncio
async def test_a_different_rejection_is_shown():
    slot = _ChatSlot("s1")
    turn = _turn(slot)
    await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
    # Same server, now rejected at a different endpoint: the error differs.
    await _on_mcp_oauth_request(turn, _event("posthog", OTHER_IDP_URL))
    # And an unsafe scheme is a different rejection again.
    _emit_mcp_oauth_request(turn.state, slot, "posthog", "javascript:alert(1)", session_id="sess-1")
    errors = [m["meta"]["error"] for m in _rejections(slot)]
    assert len(errors) == 3
    assert len(set(errors)) == 3
    # Going back to the first rejection is a change too.
    await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
    assert len(_rejections(slot)) == 4


@pytest.mark.parametrize(
    "url",
    [
        # The loopback fallback: a local server's OAuth retry on this machine.
        "http://localhost:3100/authorize"
        "?response_type=code&client_id=kiro-cli-mcp-client-0123456789abcdef"
        "&redirect_uri=http%3A%2F%2Flocalhost%3A49954%2Foauth%2Fcallback"
        "&state=Zk3mQ9pL2vX8rT5wN1bC7hJ4yA6sD0eF"
        "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        "&code_challenge_method=S256",
        # An unsafe scheme.
        "javascript:alert(1)",
    ],
    ids=["loopback", "unsafe-scheme"],
)
def test_every_rejection_kind_is_shown_once(url):
    slot = _ChatSlot("s1")
    for _ in range(3):
        _emit_mcp_oauth_request(_State(), slot, "local-tool", url, session_id="sess-1")
    assert len(_rejections(slot)) == 1


@pytest.mark.asyncio
async def test_a_new_session_shows_it_again():
    slot = _ChatSlot("s1")
    await _on_mcp_oauth_request(_turn(slot, "sess-1"), _event("posthog", POSTHOG_URL))
    await _on_mcp_oauth_request(_turn(slot, "sess-2"), _event("posthog", POSTHOG_URL))
    await _on_mcp_oauth_request(_turn(slot, "sess-2"), _event("posthog", POSTHOG_URL))
    assert len(_rejections(slot)) == 2


@pytest.mark.asyncio
async def test_signed_in_then_signed_out_shows_it_again():
    slot = _ChatSlot("s1")
    turn = _turn(slot)
    await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
    _mark_mcp_oauth_completed(turn.state, slot, "posthog", success=True)
    await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
    assert len(_rejections(slot)) == 2


@pytest.mark.asyncio
async def test_a_sign_in_seen_only_by_the_session_report_shows_it_again():
    """A connect the init drain or a session load consumed raises no event, so
    the turn-start report publish is what clears the record."""
    from kiro_crew.acp.mcp_session_report import McpSessionReport
    from kiro_crew.acp.types import EVENT_MCP_SERVER_INITIALIZED
    from kiro_crew.dashboard.chat_runner import _publish_session_mcp_report

    report = McpSessionReport()
    report.begin_session([])

    class _Reporting(_Client):
        def mcp_session_report(self):
            return report

    slot = _ChatSlot("s1")
    turn = SimpleNamespace(client=_Reporting(), slot=slot, state=_State())
    await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
    # Still awaiting sign-in: publishing the report keeps the record.
    _publish_session_mcp_report(turn.state, slot, turn.client)
    await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
    assert len(_rejections(slot)) == 1
    # Signed in outside chat, learned only from the report.
    report.record_event(EVENT_MCP_SERVER_INITIALIZED, "posthog")
    _publish_session_mcp_report(turn.state, slot, turn.client)
    await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
    assert len(_rejections(slot)) == 2


@pytest.mark.asyncio
async def test_an_authorize_banner_in_between_resets_it():
    """Rejected, then a working link (the fix), then rejected again: all shown."""
    slot = _ChatSlot("s1")
    turn = _turn(slot)
    await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
    await _on_mcp_oauth_request(turn, _event("posthog", GOOD_URL))
    await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
    assert len(_rejections(slot)) == 2


@pytest.mark.asyncio
async def test_a_failed_sign_in_does_not_reset_it():
    """kiro-cli reports init failure after each retry; that is not a change."""
    slot = _ChatSlot("s1")
    turn = _turn(slot)
    for _ in range(3):
        await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
        _mark_mcp_oauth_completed(turn.state, slot, "posthog", success=False, error="x")
    assert len(_rejections(slot)) == 1


@pytest.mark.asyncio
async def test_a_retry_from_the_mcp_servers_page_shows_it_again():
    slot = _ChatSlot("s1")
    turn = _turn(slot)
    await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
    mcp_banner_dedupe.note_mcp_sign_in_retry("posthog")
    await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
    await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
    assert len(_rejections(slot)) == 2


@pytest.mark.asyncio
async def test_a_retry_for_another_server_does_not_reshow_this_one():
    slot = _ChatSlot("s1")
    turn = _turn(slot)
    await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
    mcp_banner_dedupe.note_mcp_sign_in_retry("other")
    await _on_mcp_oauth_request(turn, _event("posthog", POSTHOG_URL))
    assert len(_rejections(slot)) == 1


@pytest.mark.asyncio
async def test_a_retry_by_configured_name_reaches_the_alias_kiro_cli_uses():
    slot = _ChatSlot("s1")
    turn = _turn(slot)
    await _on_mcp_oauth_request(turn, _event("acme-mcp", POSTHOG_URL))
    mcp_banner_dedupe.note_mcp_sign_in_retry("npm:@acme/mcp")
    await _on_mcp_oauth_request(turn, _event("acme-mcp", POSTHOG_URL))
    assert len(_rejections(slot)) == 2


@pytest.mark.asyncio
async def test_other_chats_keep_their_own_record():
    first, second = _ChatSlot("s1"), _ChatSlot("s2")
    await _on_mcp_oauth_request(_turn(first), _event("posthog", POSTHOG_URL))
    await _on_mcp_oauth_request(_turn(second), _event("posthog", POSTHOG_URL))
    assert len(_rejections(first)) == 1
    assert len(_rejections(second)) == 1


@pytest.mark.asyncio
async def test_authorize_banners_are_never_suppressed():
    """Each carries a fresh link; dropping one would leave a dead button."""
    slot = _ChatSlot("s1")
    turn = _turn(slot)
    await _on_mcp_oauth_request(turn, _event("linear", GOOD_URL))
    await _on_mcp_oauth_request(turn, _event("linear", GOOD_URL))
    live = [m for m in slot.messages if m["meta"].get("oauth_url")]
    assert len(live) == 1
    assert len(slot.messages) == 2


def test_unknown_session_never_deduplicates():
    """Without a session id a new session cannot be told apart, so show it."""
    slot = _ChatSlot("s1")
    for _ in range(3):
        _emit_mcp_oauth_request(_State(), slot, "posthog", POSTHOG_URL)
    assert len(_rejections(slot)) == 3
    assert slot._mcp_banner_shown == {}


def test_a_credential_shaped_name_is_never_deduplicated():
    """Redaction collapses such names onto one value, which could hide another
    server's banner, so they keep today's behaviour."""
    slot = _ChatSlot("s1")
    name = "AKIAIOSFODNN7EXAMPLE"
    for _ in range(2):
        _emit_mcp_oauth_request(_State(), slot, name, POSTHOG_URL, session_id="sess-1")
    assert len(_rejections(slot)) == 2
    assert slot._mcp_banner_shown == {}


def test_the_slot_record_is_bounded_and_holds_no_banner_text():
    slot = _ChatSlot("s1")
    for i in range(mcp_banner_dedupe.SLOT_CAP + 10):
        _emit_mcp_oauth_request(_State(), slot, f"srv{i}", POSTHOG_URL, session_id="sess-1")
    shown = slot._mcp_banner_shown
    assert len(shown) == mcp_banner_dedupe.SLOT_CAP
    # The oldest entries were evicted, the newest kept.
    assert "srv0" not in shown
    assert f"srv{mcp_banner_dedupe.SLOT_CAP + 9}" in shown
    stored = json.dumps(list(shown.values()))
    assert "posthog-idp" not in stored
    assert "authorize" not in stored
    assert "oauth_endpoints" not in stored


def test_the_retry_round_table_is_bounded():
    for i in range(mcp_banner_dedupe.ROUND_CAP + 10):
        mcp_banner_dedupe.note_mcp_sign_in_retry(f"srv{i}")
    assert len(mcp_banner_dedupe._sign_in_rounds) == mcp_banner_dedupe.ROUND_CAP


@pytest.mark.parametrize("recorded_before_retry", [True, False], ids=["round-0", "after-retry"])
def test_an_evicted_retry_never_reads_as_an_older_round(recorded_before_retry):
    """A retry whose round entry was evicted must still show the next banner:
    the evicted name may read as the same round or a newer one, never an
    older round a chat already recorded."""
    slot = _ChatSlot("s1")
    if not recorded_before_retry:
        mcp_banner_dedupe.note_mcp_sign_in_retry("posthog")
    _emit_mcp_oauth_request(_State(), slot, "posthog", POSTHOG_URL, session_id="sess-1")
    # The user retries, then enough other sign-ins evict posthog's entry.
    mcp_banner_dedupe.note_mcp_sign_in_retry("posthog")
    for i in range(mcp_banner_dedupe.ROUND_CAP):
        mcp_banner_dedupe.note_mcp_sign_in_retry(f"other{i}")
    assert "posthog" not in mcp_banner_dedupe._sign_in_rounds
    _emit_mcp_oauth_request(_State(), slot, "posthog", POSTHOG_URL, session_id="sess-1")
    assert len(_rejections(slot)) == 2


def test_a_long_session_id_is_stored_as_a_fixed_size_digest():
    slot = _ChatSlot("s1")
    session_id = "s" * 1_000_000
    for _ in range(2):
        _emit_mcp_oauth_request(_State(), slot, "posthog", POSTHOG_URL, session_id=session_id)
    assert len(_rejections(slot)) == 1
    stored_session, _round, stored_text = slot._mcp_banner_shown["posthog"]
    assert len(stored_session) == len(stored_text) == 16
    _emit_mcp_oauth_request(_State(), slot, "posthog", POSTHOG_URL, session_id=session_id + "x")
    assert len(_rejections(slot)) == 2


def test_a_name_over_the_cap_is_not_tracked():
    mcp_banner_dedupe.note_mcp_sign_in_retry("x" * (mcp_banner_dedupe.NAME_CAP + 1))
    assert mcp_banner_dedupe._sign_in_rounds == {}
