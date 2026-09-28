"""Config hot-reload across every channel, plus the Telegram / Discord / Webex specifics.

Nine channels are asked the same questions with a different transport class, a
different roster field name and different identity strings, so the channel is
DATA: the shared suite here parametrizes over ``_hot_reload_helpers``'
:class:`~_hot_reload_helpers.ChannelCase` table and each question is asked once.

* a reloaded ALLOW-LIST reaches the live transport, so an added id is authorized
  and a removed one is refused without a restart;
* a DEGRADED section, or a value of the wrong shape, keeps the PREVIOUS
  authorization state -- these are fail-closed boundaries, and rebuilding them
  from a document the loader could not parse would lock out every intended
  sender or serve a room nobody approved;
* a two-half group gate stays a CONJUNCTION across a reload, so neither half
  alone admits a room;
* every authorization change is SEL-audited by COUNT, and no identity reaches
  either the audit row or the log;
* a reloaded THRESHOLD is read at point of use with the loader's own clamp and
  pair normalization re-run, so a reloaded value cannot make the soft nudge
  unreachable;
* ``messaging.dm_scope`` is read live but PINNED for the life of a generation,
  so a flip cannot re-key a conversation that is already running.

The channel-specific claims that are not a per-channel copy of anything stay in
their own classes: Discord's runtime thread promotions must survive a reload
(they are not in ``config.json``, and dropping them would strand every follow-up
reply into a thread the bot just made), Webex's SECOND copy of the email roster
-- the card-press path, which does not flow through ``receive`` -- must follow the
same reload as the first, and Telegram's callback surface and session-resume
owner are two more holders of one roster. Teams / iMessage / WhatsApp are in
``test_channels_b_hot_reload.py`` and WeCom / Weixin / Feishu in
``test_channels_c_hot_reload.py``.

The appliers are driven both directly (a hand-built :class:`ConfigChange`, which
is what a dispatcher's subscriber actually receives) and through
``ConfigWatch.refresh_now()`` against a real temp config file, so the wiring from
a file write to a transport's frozenset is covered end to end.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from _hot_reload_helpers import (
    ChannelCase,
    FakeClient,
    FakeProvider,
    FakeSessions,
    apply,
    boot_cfg,
    cases_with_dm_scope,
    cases_with_inbound,
    cases_with_toggle,
    cfg_with,
    change,
    channel_cases,
    drive_one_reload,
    gated_cases,
    normalizer_cases,
    point_loader_at,
    prime_live,
    prime_raw_section,
    sel_outcomes,
)

from kiro_crew.config import live
from kiro_crew.discord.transport import DiscordTransport
from kiro_crew.discord.transport_dispatch import DiscordDispatcher
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.telegram.transport import TelegramTransport
from kiro_crew.telegram.transport_dispatch import TelegramDispatcher
from kiro_crew.webex.transport import WebexTransport

CASES = channel_cases()

# ------------------------------------------------------------------
# The reloaded allow-list
# ------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_a_reloaded_roster_authorizes_the_added_id_and_refuses_the_removed(
    case: ChannelCase,
) -> None:
    t = case.make_transport(case.boot)
    assert case.authorize(t, case.boot_id)
    assert not case.authorize(t, case.added_id)

    t.reconfigure(case.section(case.added))

    assert case.authorize(t, case.added_id)
    assert not case.authorize(t, case.boot_id)
    for extra in case.also_authorized:
        assert case.authorize(t, extra), extra


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_a_non_list_roster_keeps_the_previous_allow_list(case: ChannelCase) -> None:
    t = case.make_transport(case.boot)
    t.reconfigure(case.section(case.added, **{case.roster_key: case.added_id}))
    assert case.authorize(t, case.boot_id)
    assert not case.authorize(t, case.added_id)


@pytest.mark.parametrize("case", cases_with_toggle(), ids=lambda c: c.name)
def test_a_non_bool_toggle_keeps_the_previous_value(case: ChannelCase) -> None:
    assert case.toggle_reader is not None
    t = case.make_transport(case.boot)
    t.reconfigure(case.section(case.boot, **{case.toggle_key: False}))
    assert case.toggle_reader(t) is False
    t.reconfigure(case.section(case.boot, **{case.toggle_key: "yes"}))
    assert case.toggle_reader(t) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("case", gated_cases(), ids=lambda c: c.name)
async def test_the_group_gate_stays_a_conjunction_after_a_reload(case) -> None:
    """Neither half alone admits the room, across every reload ordering."""
    t, gate = case.build()
    assert not await gate()

    # The id alone is not enough: the toggle is still off.
    t.reconfigure(case.section(False, [case.target_id]))
    assert not await gate()

    # The toggle alone is not enough either: a different id is listed.
    t.reconfigure(case.section(True, [case.other_id]))
    assert not await gate()

    # Both halves satisfied.
    t.reconfigure(case.section(True, [case.target_id]))
    assert await gate()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_a_roster_change_is_audited_by_count_and_never_by_value(
    case: ChannelCase, monkeypatch, caplog
) -> None:
    audited: list[dict] = []
    monkeypatch.setattr(
        f"{case.transport_module}.sel",
        lambda: SimpleNamespace(log_api_access=lambda **kw: audited.append(kw)),
    )
    t = case.make_transport(case.boot)
    with caplog.at_level("INFO", logger=case.transport_module):
        t.reconfigure(SimpleNamespace(**case.audit_fields))

    assert sel_outcomes(audited, case.audit_operation) == list(case.audit_outcomes)
    assert case.audit_secret not in caplog.text
    assert all(case.audit_secret not in str(a.get("resources", "")) for a in audited)
    if case.audit_resources is not None:
        outcome, resources = case.audit_resources
        row = next(a for a in audited if a.get("outcome") == outcome)
        assert row["resources"] == resources


@pytest.mark.parametrize(
    "identifier,normalizer,raw,expected",
    normalizer_cases(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_the_roster_normalizer_drops_unusable_entries(
    identifier: str, normalizer, raw, expected
) -> None:
    """One contract, three pure functions: a bad ENTRY is dropped, a bad VALUE
    answers ``None`` so the caller keeps its previous roster."""
    assert normalizer(raw) == expected


# ------------------------------------------------------------------
# The dispatcher's applier
# ------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_the_applier_pushes_the_reloaded_section_at_the_live_transport(case: ChannelCase) -> None:
    t = case.make_transport(case.boot)
    apply(case.make_dispatcher(t), change(case.config(case.added), case.name))
    assert t._allowed == case.expect_added
    # Before the transport is up there is nothing to push at: a no-op, not a raise.
    apply(case.make_dispatcher(None), change(case.config(case.added), case.name))


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_a_degraded_section_keeps_the_previous_authorization_state(case: ChannelCase) -> None:
    sections = frozenset({case.name})
    t = case.make_transport(case.boot)
    # Deferred, not dropped: the applier names the paths it refused so the
    # watcher retries them once the loader reads a clean document -- a repair
    # back to the degraded defaults would otherwise diff empty and never apply.
    with pytest.raises(live.ConfigDeferred) as deferred:
        apply(
            case.make_dispatcher(t),
            change(case.config(case.added, degraded=sections), case.name),
        )
    assert t._allowed == case.expect_boot
    assert deferred.value.paths == frozenset({case.name})


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_the_sticky_whole_config_flag_alone_does_not_gate_the_applier(case: ChannelCase) -> None:
    """The loader keeps the whole-config flag for the life of the process once a
    file failed to parse, so a repaired document still carries it. The watcher
    never dispatches a document that is torn NOW (it keeps the previous snapshot
    while the file does not parse), so on a dispatched change the flag is only
    that memory, and refusing on it would freeze the allow-list until a restart --
    a revocation written after one transient typo would never land."""
    t = case.make_transport(case.boot)
    apply(
        case.make_dispatcher(t),
        change(case.config(case.added, degraded=frozenset({"*", "*config.json"})), case.name),
    )
    assert t._allowed == case.expect_added


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_a_change_outside_the_section_re_pushes_the_unchanged_section(case: ChannelCase) -> None:
    """``messaging`` is a watched prefix, so the section is pushed again.

    The push is a no-op: the section carries what the transport already adopted,
    because a roster edit would have been its own changed path.
    """
    t = case.make_transport(case.boot)
    apply(case.make_dispatcher(t), change(case.config(case.boot), "messaging.dm_scope"))
    assert t._allowed == case.expect_boot


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_the_dispatcher_subscribes_under_its_own_section(case: ChannelCase) -> None:
    """A prefix wider than the channel would fire on every unrelated write."""
    d = case.make_dispatcher()
    subs = [s for s in live.watch().subscriptions() if s.name == case.subscription_name]
    assert len(subs) == 1
    assert subs[0].prefixes == (case.name, "messaging")
    assert d._config_sub is subs[0]


# ------------------------------------------------------------------
# Point-of-use reads
# ------------------------------------------------------------------


@pytest.mark.parametrize("soft_in,hard_in", [(40, 60), (0, 0), (999, 999), (95, 50)])
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_a_reloaded_threshold_is_clamped_and_ordered(
    case: ChannelCase, soft_in: int, hard_in: int
) -> None:
    """The loader's own clamp and pair normalization are re-run at point of use.

    Read straight off the section, a hand-edited value can nudge on every turn,
    never nudge, or (with ``soft > hard``) be unreachable because the transports
    test ``pct >= hard`` first. The snapshot is primed RAW, so what is asserted
    is the reader's own normalization rather than the loader's.
    """
    d = case.make_dispatcher()
    prime_raw_section(case.name, **case.threshold_fields(soft_in, hard_in))
    assert case.read_thresholds(d) == case.expected_thresholds(soft_in, hard_in)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_an_unreadable_config_falls_back_to_the_boot_copy(case: ChannelCase, monkeypatch) -> None:
    """A threshold is not an authorization decision, so a turn keeps running."""
    d = case.make_dispatcher(soft=80, hard=95)
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load",
        staticmethod(lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))),
    )
    assert case.read_thresholds(d) == case.expected_thresholds(80, 95)


@pytest.mark.parametrize("case", cases_with_dm_scope(), ids=lambda c: c.name)
def test_dm_scope_is_boot_only(case: ChannelCase) -> None:
    """A dm_scope flip in ``config.json`` never re-keys a conversation.

    dm_scope selects the session-key NAMESPACE, and every conversation's
    generation counter is seeded from the namespace in force at boot. Adopting a
    new value live -- even at a generation boundary -- would mint keys in the
    other namespace with a counter seeded for this one, and could resume a stale
    session persisted there. So the field is ``restart=True`` and the dispatchers
    read the boot copy: a live change is invisible before AND after ``/new``.
    """
    from kiro_crew.config.schema import requires_restart

    assert requires_restart("messaging.dm_scope")
    assert case.dm_scope_probe is not None
    d = case.make_dispatcher()
    read_now, read_next = case.dm_scope_probe(d)
    first = read_now()
    prime_live("messaging", dm_scope="unified")
    assert read_now() == first
    assert "unified" not in str(read_next())


@pytest.mark.asyncio
@pytest.mark.parametrize("case", cases_with_inbound(), ids=lambda c: c.name)
async def test_a_reloaded_hard_threshold_compacts_this_turn(case: ChannelCase) -> None:
    assert case.inbound is not None
    d = case.make_dispatcher(pct=90.0, soft=70, hard=99)
    provider = FakeProvider()
    inbound = case.inbound()
    await d._maybe_notice(inbound, f"{case.name}:k", provider)
    assert not provider.compacted
    prime_live(case.name, **case.threshold_fields(70, 80))
    await d._maybe_notice(inbound, f"{case.name}:k", provider)
    assert provider.compacted


# ------------------------------------------------------------------
# End to end: a file write reaches each transport
# ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
async def test_a_config_file_write_reaches_the_transport(
    case: ChannelCase, tmp_path, monkeypatch
) -> None:
    """The whole path: write config.json -> ConfigWatch -> transport frozenset.

    Driven through ``refresh_now`` rather than the poll interval so the test does
    not sleep, and pointed at temp files exactly as the loader's own tests do.
    """
    cfg_path = point_loader_at(tmp_path, monkeypatch, case.document(case.boot))
    transport = case.make_transport(case.boot)
    dispatcher = case.make_dispatcher(transport)

    reloaded = await drive_one_reload(dispatcher, cfg_path, case.document(case.added))

    assert reloaded is not None and reloaded.touched(case.name)
    assert transport._allowed == case.expect_added


# ------------------------------------------------------------------
# Telegram
# ------------------------------------------------------------------


class TestTelegramAllowLists:
    def _transport(self, *, users=(7,), allow_forum=False, chats=()) -> TelegramTransport:
        return TelegramTransport(
            FakeClient(),
            allowed_user_ids=list(users),
            allow_forum=allow_forum,
            allowed_forum_chat_ids=list(chats),
        )

    def test_user_ids_are_coerced_to_strings_like_the_constructor(self):
        t = self._transport()
        t.reconfigure(
            SimpleNamespace(
                allowed_user_ids=[9, "11", " "], allow_forum=False, allowed_forum_chat_ids=[]
            )
        )
        assert t._allowed == frozenset({"9", "11"})

    def test_an_uncoercible_forum_chat_id_keeps_the_previous_set(self):
        t = self._transport(allow_forum=True, chats=(-100,))
        t.reconfigure(
            SimpleNamespace(
                allowed_user_ids=[7], allow_forum=True, allowed_forum_chat_ids=["not-a-number"]
            )
        )
        assert t._allowed_forum_chat_ids == frozenset({-100})


class TestTelegramDispatcherApplier:
    def _dispatcher(self) -> TelegramDispatcher:
        """A dispatcher rostered like the boot factory: INT user ids."""
        d = TelegramDispatcher(
            sessions=FakeSessions(),
            ctx_builder=SimpleNamespace(),
            cfg=boot_cfg("telegram", soft_threshold_pct=80, show_thinking=False),
            allowed_user_ids={7},
        )
        d.client = FakeClient()
        d.transport = None
        return d

    def test_a_removed_user_is_refused_by_the_dispatcher_after_a_reload(self):
        """``_authorized`` gates callbacks, which never reach transport.receive."""
        d = self._dispatcher()
        assert d._authorized(7)
        apply(d, change(cfg_with("telegram", allowed_user_ids=[9]), "telegram"))
        assert not d._authorized(7), "a removed id keeps the callback surface"
        assert d._authorized(9)

    def test_the_session_resume_owner_follows_the_same_reload(self):
        d = self._dispatcher()
        roster = d._allowed
        assert d._session_resume.owner_id == 7
        apply(d, change(cfg_with("telegram", allowed_user_ids=[9]), "telegram"))
        assert d._allowed is roster, "mutated in place, so a shared holder follows"
        assert d._session_resume.owner_id == 9
        # Two configured identities leave no unambiguous owner.
        apply(d, change(cfg_with("telegram", allowed_user_ids=[9, 10]), "telegram"))
        assert d._session_resume.owner_id == 0

    def test_an_unusable_roster_keeps_the_dispatcher_copy(self):
        d = self._dispatcher()
        apply(d, change(cfg_with("telegram", allowed_user_ids="9"), "telegram"))
        assert d._allowed == {7}

    def test_show_thinking_follows_the_live_snapshot(self):
        d = self._dispatcher()
        prime_live("telegram", show_thinking=True)
        assert bool(d._live_cfg().telegram.show_thinking) is True


# ------------------------------------------------------------------
# Discord
# ------------------------------------------------------------------


class TestDiscordAllowLists:
    def _transport(self, *, users=("11",), threads=(), channels=()) -> DiscordTransport:
        return DiscordTransport(
            FakeClient(),
            allowed_user_ids=list(users),
            allowed_thread_ids=list(threads),
            allowed_channel_ids=list(channels),
        )

    def _section(self, **over) -> SimpleNamespace:
        base = {
            "allowed_user_ids": ["11"],
            "allowed_thread_ids": [],
            "allowed_channel_ids": [],
            "auto_thread": True,
        }
        return SimpleNamespace(**{**base, **over})

    def test_a_runtime_promoted_thread_survives_the_reload(self):
        """A thread the bot created is not in config.json.

        Dropping it on a reload would strand every follow-up reply the user sends
        into the thread the bot just made, which is exactly why the set is
        mutable in the first place.
        """
        t = self._transport(threads=("t-configured",))
        t._allowed_threads.add("t-runtime")
        t.reconfigure(self._section(allowed_thread_ids=["t-configured"]))
        assert t._allowed_threads == {"t-configured", "t-runtime"}

    def test_a_thread_removed_from_the_config_is_dropped(self):
        t = self._transport(threads=("t-configured",))
        t.reconfigure(self._section())
        assert t._allowed_threads == set()

    def test_the_channel_allow_list_follows_the_reload(self):
        t = self._transport(channels=("c-old",))
        t.reconfigure(self._section(allowed_channel_ids=["c-new"]))
        assert t._allowed_channels == frozenset({"c-new"})

    def test_auto_thread_follows_the_reload(self):
        t = self._transport()
        t.reconfigure(self._section(auto_thread=False))
        assert t._auto_thread is False

    def test_a_non_list_channel_list_keeps_the_previous_set(self):
        t = self._transport(channels=("c-old",))
        t.reconfigure(self._section(allowed_thread_ids=None, allowed_channel_ids="c-new"))
        assert t._allowed_channels == frozenset({"c-old"})


class TestDiscordDispatcherApplier:
    def _dispatcher(self, **kw) -> DiscordDispatcher:
        d = DiscordDispatcher(
            sessions=FakeSessions(),
            ctx_builder=SimpleNamespace(),
            cfg=boot_cfg(
                "discord",
                soft_threshold_pct=80,
                reactions_enabled=True,
                show_thinking=False,
            ),
            allowed_user_ids={"11"},
            **kw,
        )
        d.client = FakeClient()
        d.transport = None
        return d

    def test_a_removed_user_is_refused_by_the_dispatcher_after_a_reload(self):
        """``_authorized`` gates interactions, which never reach transport.receive."""
        d = self._dispatcher()
        assert d._authorized("11")
        apply(d, change(cfg_with("discord", allowed_user_ids=["22"]), "discord"))
        assert not d._authorized("11"), "a removed id keeps the interaction surface"
        assert d._authorized("22")

    def test_the_dispatcher_roster_is_mutated_in_place(self):
        d = self._dispatcher()
        roster = d._allowed
        apply(d, change(cfg_with("discord", allowed_user_ids=["22"]), "discord"))
        assert d._allowed is roster

    def test_the_session_resume_owner_follows_the_same_reload(self):
        """The ``!sessions`` owner is the third copy of the roster; a reload that
        adds a second identity must revoke the surface now, not at restart."""
        d = self._dispatcher()
        assert d._session_resume.owner_id == "11"
        assert d._session_resume.is_owner("11")
        apply(d, change(cfg_with("discord", allowed_user_ids=["22"]), "discord"))
        assert d._session_resume.owner_id == "22"
        assert not d._session_resume.is_owner("11")
        # Two configured identities leave no unambiguous owner.
        apply(d, change(cfg_with("discord", allowed_user_ids=["22", "33"]), "discord"))
        assert d._session_resume.owner_id == ""
        assert not d._session_resume.is_owner("22")
        # An unusable roster keeps the previous owner along with the previous roster.
        apply(d, change(cfg_with("discord", allowed_user_ids=["44"]), "discord"))
        apply(d, change(cfg_with("discord", allowed_user_ids="55"), "discord"))
        assert d._session_resume.owner_id == "44"

    def test_a_promoted_thread_survives_a_reload_but_a_removed_configured_one_does_not(self):
        d = self._dispatcher(allowed_thread_ids={"cfg1"})
        d.register_allowed_thread("runtime1")
        apply(d, change(cfg_with("discord", allowed_thread_ids=["cfg2"]), "discord"))
        assert "runtime1" in d._allowed_threads, "a thread this process created is not in config"
        assert "cfg2" in d._allowed_threads
        assert "cfg1" not in d._allowed_threads, "a removed configured thread is dropped"
        # The promoted id stays promoted across a second reload.
        apply(d, change(cfg_with("discord", allowed_thread_ids=[]), "discord"))
        assert d._allowed_threads == {"runtime1"}

    def test_an_unusable_thread_list_keeps_the_dispatcher_set(self):
        d = self._dispatcher()
        d.register_allowed_thread("runtime1")
        apply(d, change(cfg_with("discord", allowed_thread_ids="cfg2"), "discord"))
        assert d._allowed_threads == {"runtime1"}

    def test_the_render_toggles_follow_the_live_snapshot(self):
        d = self._dispatcher()
        prime_live("discord", reactions_enabled=False, show_thinking=True)
        assert d._render_config() == (False, True)


# ------------------------------------------------------------------
# Webex
# ------------------------------------------------------------------


class TestWebexAllowLists:
    def _transport(self, *, emails=("a@example.com",), group=False, rooms=()) -> WebexTransport:
        return WebexTransport(
            FakeClient(),
            allowed_emails=list(emails),
            allow_group_rooms=group,
            allowed_room_ids=list(rooms),
        )

    def test_emails_are_lowercased_and_room_ids_are_not(self):
        t = self._transport()
        t.reconfigure(
            SimpleNamespace(
                allowed_emails=["Mixed@Case.COM"],
                allow_group_rooms=True,
                allowed_room_ids=["Y2lzY29:RoOm"],
            )
        )
        assert t._allowed == frozenset({"mixed@case.com"})
        assert t._allowed_rooms == frozenset({"Y2lzY29:RoOm"})

    def test_a_reloaded_email_matches_case_insensitively(self):
        t = self._transport()
        t.reconfigure(
            SimpleNamespace(
                allowed_emails=["B@Example.com"], allow_group_rooms=False, allowed_room_ids=[]
            )
        )
        assert t.authorize(
            InboundMessage(
                channel_type="webex", user_id="b@example.com", conversation_id="r1", text="hi"
            )
        )

    def test_an_emptied_room_list_still_denies_every_space(self):
        t = self._transport(group=True, rooms=("oc_room",))
        t.reconfigure(
            SimpleNamespace(
                allowed_emails=["a@example.com"], allow_group_rooms=True, allowed_room_ids=[]
            )
        )
        assert not t.may_send_to("oc_room")

    def test_a_non_list_room_list_keeps_the_previous_set(self):
        t = self._transport(group=True, rooms=("oc_room",))
        t.reconfigure(
            SimpleNamespace(
                allowed_emails=["b@example.com"],
                allow_group_rooms=True,
                allowed_room_ids="oc_new",
            )
        )
        assert t._allowed_rooms == frozenset({"oc_room"})


class TestWebexDispatcherApplier:
    def _dispatcher(self):
        webex = next(c for c in CASES if c.name == "webex")
        return webex.make_dispatcher()

    def test_the_card_press_copy_follows_the_same_reload(self):
        """Webex's SECOND roster copy: a press does not flow through receive."""
        d = self._dispatcher()
        prime_live("webex", allowed_emails=["a@example.com"])
        assert d._sender_allowed("a@example.com")
        prime_live("webex", allowed_emails=["b@example.com"])
        assert d._sender_allowed("b@example.com")
        assert not d._sender_allowed("a@example.com")

    def test_the_card_press_copy_denies_on_an_emptied_roster(self):
        d = self._dispatcher()
        prime_live("webex", allowed_emails=["a@example.com"])
        assert d._sender_allowed("a@example.com")
        prime_live("webex", allowed_emails=[])
        assert not d._sender_allowed("a@example.com")

    def test_reply_in_thread_follows_the_live_snapshot(self):
        d = self._dispatcher()
        prime_live("webex", reply_in_thread=True)
        assert d._reply_parent(SimpleNamespace(parent_id="p1")) == "p1"
