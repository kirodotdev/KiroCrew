"""A key-named session's mirror row must be one the gateway wrote.

For a channel-born key (``discord:kirocrew:direct:42``) the per-send recipient
check reads the peer off the KEY, so the roster cannot tell which conversation the
row points at. The session map is writable by in-sandbox code: a row whose
``channel_id`` is rewritten to another allow-listed user's DM would otherwise pass
and deliver the session's replies, cron results and notices into that DM. The row
is therefore signed by the gateway's own binds and refused at send unless its
admission verifies or it is the conversation the gateway saw the session read in.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

import pytest

from kiro_crew.messaging.link import ChannelLink, bind_origin_mirror, rebind_conversation_location
from kiro_crew.mirror_admission import (
    admission_required,
    sign_mirror_admission,
    verify_mirror_admission,
)

KEY = "discord:kirocrew:direct:42"
OWN_DM = "dm-channel-of-42"
OTHER_DM = "dm-channel-of-77"


class _Client:
    pass


class _Sessions:
    """The slice of ``SessionManager`` the binds and the ladder read."""

    def __init__(self) -> None:
        self.rows: dict[str, ChannelLink] = {}
        self.origins: dict[str, ChannelLink] = {}
        self.inbound: set[str] = set()
        self.writes: list[tuple[str, bool]] = []

    def mirror_opt_out(self, key: str) -> bool:
        return False

    def set_mirror_opt_out(self, key: str, value: bool) -> None:
        pass

    def get_mirror_link(self, key: str) -> ChannelLink | None:
        return self.rows.get(key)

    def set_mirror_link(self, key: str, link: ChannelLink, **kw: Any) -> None:
        # The store keeps what serializes: a round trip through the row's dict.
        self.rows[key] = ChannelLink.from_dict(link.to_dict())
        self.writes.append((key, bool(kw.get("accepts_inbound"))))

    def clear_mirror_link(self, key: str, **_: Any) -> bool:
        return self.rows.pop(key, None) is not None

    def replace_mirror_link_if(self, key: str, expected: ChannelLink, link: ChannelLink) -> bool:
        current = self.rows.get(key)
        if current is None or not current.same_row(expected):
            return False
        self.set_mirror_link(key, link, accepts_inbound=key in self.inbound)
        return True

    def find_mirror_sessions(self, link: ChannelLink, *, inbound_only: bool = False) -> list[str]:
        return [k for k, row in self.rows.items() if row == link and k in self.inbound]

    def get_origin_link(self, key: str) -> ChannelLink | None:
        return self.origins.get(key)

    def batched_save(self) -> Any:
        import contextlib

        return contextlib.nullcontext()


class _State:
    def __init__(self, transport: Any, sessions: _Sessions) -> None:
        self._transport = transport
        self.sessions = sessions

    def get_channel_transport(self, channel_type: str) -> Any:
        return self._transport


@pytest.fixture(autouse=True)
def _permit_governance(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the row decision from the channel-scope decision, and give every
    test a fresh said-once set so a refusal's first line is logged in any order."""
    from kiro_crew.dashboard import chat_runner

    _floor_monkeypatch.setattr(chat_runner, "_RECIPIENT_LOGGED", set())
    _floor_monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.vet_and_audit",
        lambda *a, **k: type("D", (), {"permitted": True})(),
    )


def _discord() -> Any:
    from kiro_crew.discord.transport import DiscordTransport

    # Two allow-listed users: the attack needs the victim on the roster.
    return DiscordTransport(_Client(), allowed_user_ids=["42", "77"], allowed_thread_ids=[])


def _resolve(state: _State, key: str = KEY) -> Any:
    from kiro_crew.dashboard.chat_runner import _resolve_mirror_target

    return _resolve_mirror_target(state, key)


def test_a_rewritten_channel_id_on_a_key_named_dm_is_refused() -> None:
    """The reported vector: an unsigned row aimed at another allow-listed DM."""
    sessions = _Sessions()
    sessions.rows[KEY] = ChannelLink("discord", channel_id=OTHER_DM)
    assert _resolve(_State(_discord(), sessions)) is None


def test_a_signed_row_rewritten_to_another_dm_is_refused() -> None:
    """The gateway signed the bind; in-sandbox code then repointed the row."""
    sessions = _Sessions()
    assert bind_origin_mirror(sessions, key=KEY, location=ChannelLink("discord", channel_id=OWN_DM))
    sessions.rows[KEY] = dataclasses.replace(sessions.rows[KEY], channel_id=OTHER_DM)
    assert _resolve(_State(_discord(), sessions)) is None


def test_a_signed_row_moved_to_another_session_is_refused() -> None:
    other_key = "discord:kirocrew:direct:77"
    sessions = _Sessions()
    bind_origin_mirror(
        sessions, key=other_key, location=ChannelLink("discord", channel_id=OTHER_DM)
    )
    sessions.rows[KEY] = sessions.rows[other_key]
    assert _resolve(_State(_discord(), sessions)) is None


def test_the_origin_bind_signs_the_row_and_it_is_delivered() -> None:
    """The ordinary path keeps working, across the store's serialization."""
    sessions = _Sessions()
    transport = _discord()
    assert bind_origin_mirror(sessions, key=KEY, location=ChannelLink("discord", channel_id=OWN_DM))
    row = sessions.rows[KEY]
    assert row.principal is None and row.admission
    assert verify_mirror_admission(KEY, row)
    target = _resolve(_State(transport, sessions))
    assert target is not None and target[0].channel_id == OWN_DM


def test_a_legacy_unsigned_row_is_refused_until_re_bound(caplog: pytest.LogCaptureFixture) -> None:
    """A row written before rows were signed: refused, the log names the remedy,
    and the in-channel link re-binds it."""
    sessions = _Sessions()
    state = _State(_discord(), sessions)
    sessions.rows[KEY] = ChannelLink("discord", channel_id=OWN_DM)
    with caplog.at_level(logging.WARNING):
        assert _resolve(state) is None
    assert "Re-bind it" in caplog.text
    rebind_conversation_location(
        sessions,
        key=KEY,
        location=ChannelLink("discord", channel_id=OWN_DM),
        unlink_command="!unlink",
    )
    assert _resolve(state) is not None


def test_the_next_inbound_turn_re_signs_a_legacy_row_at_its_own_location() -> None:
    """A row from before rows were signed heals on the user's next message from
    that conversation, keeping its inbound flag; it then survives a restart."""
    sessions = _Sessions()
    state = _State(_discord(), sessions)
    sessions.rows[KEY] = ChannelLink("discord", channel_id=OWN_DM)
    sessions.inbound.add(KEY)
    assert _resolve(state) is None
    assert not bind_origin_mirror(
        sessions, key=KEY, location=ChannelLink("discord", channel_id=OWN_DM)
    )
    assert sessions.writes == [(KEY, True)]
    assert _resolve(state) is not None
    # Steady state: a verifying row is a read, not a write.
    bind_origin_mirror(sessions, key=KEY, location=ChannelLink("discord", channel_id=OWN_DM))
    assert sessions.writes == [(KEY, True)]


def test_the_re_sign_keeps_a_muted_mirror_muted_and_its_inbound_flag(tmp_path: Any) -> None:
    """On the real store: repairing the admission is not a reconnect, so the user's
    mute and the resume binding both survive it."""
    from unittest.mock import patch

    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        store = SessionMap()
        legacy = ChannelLink("discord", channel_id=OWN_DM)
        store.set_mirror_link(KEY, legacy, accepts_inbound=True)
        store.set_mirror_paused(KEY, True)
        assert not verify_mirror_admission(KEY, store.get_mirror_link(KEY))
        # The origin bind's repair step, driven on the store itself.
        from kiro_crew.messaging.link import _resign_own_location

        here = ChannelLink("discord", channel_id=OWN_DM)
        _resign_own_location(store, key=KEY, existing=store.get_mirror_link(KEY), location=here)
        assert verify_mirror_admission(KEY, store.get_mirror_link(KEY))
        assert store.is_mirror_paused(KEY) is True
        assert KEY in store.find_mirror_sessions(legacy, inbound_only=True)


def test_the_re_sign_keeps_a_rebind_that_landed_after_its_read(tmp_path: Any) -> None:
    """The repair's read and its write are not one step for the caller; the store's
    compare-and-set is. A dashboard re-link committed in between survives."""
    from unittest.mock import patch

    from kiro_crew.messaging.link import _resign_own_location
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        store = SessionMap()
        here = ChannelLink("discord", channel_id=OWN_DM)
        store.set_mirror_link(KEY, here)
        stale = store.get_mirror_link(KEY)
        newer = dataclasses.replace(
            ChannelLink("discord", channel_id="dm-picked-from-dashboard"),
            admission=sign_mirror_admission(
                KEY, ChannelLink("discord", channel_id="dm-picked-from-dashboard")
            ),
        )
        store.set_mirror_link(KEY, newer, accepts_inbound=True)
        _resign_own_location(store, key=KEY, existing=stale, location=here)
        kept = store.get_mirror_link(KEY)
        assert kept is not None and kept.same_row(newer)
        assert KEY in store.find_mirror_sessions(newer, inbound_only=True)


@pytest.mark.asyncio
async def test_the_re_sign_never_writes_the_map_on_the_event_loop(tmp_path: Any) -> None:
    """On the loop the repair only marks the map dirty; the debounced flush writes
    off the loop and still sees the mute."""
    from unittest.mock import patch

    from kiro_crew.messaging.link import _resign_own_location
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        store = SessionMap()
        here = ChannelLink("discord", channel_id=OWN_DM)
        with store.batched_save():
            store.set_mirror_link(KEY, here)
            store.set_mirror_paused(KEY, True)
        inline: list[bool] = []
        real_write = store._write
        with patch.object(store, "_write", side_effect=lambda: inline.append(True)):
            _resign_own_location(store, key=KEY, existing=store.get_mirror_link(KEY), location=here)
            assert inline == []
        store._write = real_write  # type: ignore[method-assign]
        assert verify_mirror_admission(KEY, store.get_mirror_link(KEY))
        assert store.is_mirror_paused(KEY) is True
        await store.aflush()
        reread = SessionMap()
        assert verify_mirror_admission(KEY, reread.get_mirror_link(KEY))
        assert reread.is_mirror_paused(KEY) is True


def test_an_inbound_turn_never_re_signs_a_row_aimed_elsewhere() -> None:
    """The forged row points at another DM: the inbound location does not vouch."""
    sessions = _Sessions()
    sessions.rows[KEY] = ChannelLink("discord", channel_id=OTHER_DM)
    bind_origin_mirror(sessions, key=KEY, location=ChannelLink("discord", channel_id=OWN_DM))
    assert sessions.writes == []
    assert _resolve(_State(_discord(), sessions)) is None


def test_the_conversation_the_gateway_saw_is_admitted_unsigned() -> None:
    """The in-memory origin record is gateway-written, so it vouches for its own
    location even on a legacy row -- and only for that location."""
    sessions = _Sessions()
    state = _State(_discord(), sessions)
    sessions.origins[KEY] = ChannelLink("discord", channel_id=OWN_DM)
    sessions.rows[KEY] = ChannelLink("discord", channel_id=OWN_DM)
    assert _resolve(state) is not None
    sessions.rows[KEY] = ChannelLink("discord", channel_id=OTHER_DM)
    assert _resolve(state) is None


def test_a_forged_row_on_a_key_named_forum_route_is_refused() -> None:
    """A room-audience key names no peer, but its row is just as agent-writable."""
    from kiro_crew.dashboard.chat_runner import _resolve_channel_target

    class _Permissive:
        capabilities = type("Caps", (), {"supports_proactive_send": True})()

        def may_send_to(self, *a: Any, **k: Any) -> bool:
            return True

    key = "telegram:kirocrew:forum:-100123:7"
    sessions = _Sessions()
    forged = ChannelLink("telegram", channel_id="-100999", thread_id="1")
    assert _resolve_channel_target(_State(_Permissive(), sessions), key, forged) is None
    signed = dataclasses.replace(forged, admission=sign_mirror_admission(key, forged))
    assert _resolve_channel_target(_State(_Permissive(), sessions), key, signed) is not None


def test_a_link_addressed_to_the_keys_own_peer_by_id_is_admitted() -> None:
    """Telegram's DM id is the user id, and the cron leg's stored-channel rung
    carries the peer's user id: aimed at the key's peer, the destination is the
    peer. The same shape aimed at another user is refused."""
    from kiro_crew.dashboard.chat_runner import _resolve_channel_target

    key = "telegram:kirocrew:direct:111"
    sessions = _Sessions()
    transport = _StubTelegram()
    own = ChannelLink("telegram", channel_id="111")
    assert _resolve_channel_target(_State(transport, sessions), key, own) is not None
    other = ChannelLink("telegram", channel_id="222")
    assert _resolve_channel_target(_State(transport, sessions), key, other) is None
    topic = ChannelLink("telegram", channel_id="111", thread_id="5")
    assert _resolve_channel_target(_State(transport, sessions), key, topic) is None
    # The same digits on another channel are another person.
    elsewhere = ChannelLink("wecom", channel_id="111")
    assert _resolve_channel_target(_State(transport, sessions), key, elsewhere) is None


class _StubTelegram:
    """Permits any allow-listed id, as Telegram's DM roster does for both users."""

    capabilities = type("Caps", (), {"supports_proactive_send": True})()

    def may_send_to(self, conversation_id: str, thread_id: Any = None, **_: Any) -> bool:
        return conversation_id in {"111", "222"}


def test_a_caller_supplied_principal_is_not_held_to_the_row_rule() -> None:
    """The drop notice builds its link from in-memory entry metadata."""
    from kiro_crew.dashboard.chat_runner import _resolve_channel_target

    sessions = _Sessions()
    link = ChannelLink("discord", channel_id=OWN_DM)
    assert (
        _resolve_channel_target(_State(_discord(), sessions), KEY, link, principal="42") is not None
    )


def test_a_dashboard_born_row_keeps_the_principal_rule() -> None:
    """A ``chat-*`` key names nobody: unsigned peer-less rows stay unsigned."""
    assert not admission_required("dashboard:chat-1")
    assert sign_mirror_admission("dashboard:chat-1", ChannelLink("discord", channel_id="x")) == ""
    assert admission_required(KEY)


def test_a_key_named_admission_survives_serialization_without_a_peer() -> None:
    link = ChannelLink("discord", channel_id=OWN_DM)
    signed = dataclasses.replace(link, admission=sign_mirror_admission(KEY, link))
    stored = signed.to_dict()
    assert "principal" not in stored and stored["admission"] == signed.admission
    assert verify_mirror_admission(KEY, ChannelLink.from_dict(stored))


def test_the_cron_leg_signs_the_link_it_reads_from_the_live_origin_record() -> None:
    """A real class's bound methods are new objects on every access, so the origin
    rung is told apart by position, not identity: its link comes back signed, and
    survives the ``reset()`` that evicts the record before the ladder runs."""
    from unittest.mock import patch

    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.slack import gateway as gw

    class _RealSessions:
        def get_origin_link(self, key: str) -> ChannelLink | None:
            return ChannelLink("discord", channel_id=OWN_DM)

        def get_mirror_link(self, key: str) -> ChannelLink | None:
            return None

    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch.sessions = _RealSessions()
    assert orch.sessions.get_origin_link is not orch.sessions.get_origin_link
    resolved = orch._channel_reply_link(KEY)
    assert resolved is not None
    link, needs_dm_resolution = resolved
    assert not needs_dm_resolution and verify_mirror_admission(KEY, link)
    # The record is gone by the time the ladder judges the snapshot.
    sessions = _Sessions()
    from kiro_crew.dashboard.chat_runner import _resolve_channel_target

    assert _resolve_channel_target(_State(_discord(), sessions), KEY, link) is not None
