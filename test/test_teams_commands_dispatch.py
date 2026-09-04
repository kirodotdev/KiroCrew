"""Teams command handlers: /yolo, /dashboard, /link, /unlink, and the prompt card.

These are the affordances that make the channel usable rather than just
conversational, and each has a property worth pinning: `/yolo` must stay
conversation-scoped (a process-wide grant from an allow-list member would reach
the operator's dashboard and cron sessions), `/dashboard` mints a CREDENTIAL and
must audit either outcome, and the mirror commands must go through the shared
link helpers rather than hand-rolling the session-map writes.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from kiro_crew.safety_override import safety_override
from kiro_crew.teams.approvals import TeamsApprovalDecider
from kiro_crew.teams.client import TeamsInbound
from kiro_crew.teams.transport_dispatch import TeamsDispatcher

_SVC = "https://smba.trafficmanager.net/teams"
_EMAIL = "me@example.com"


@pytest.fixture(autouse=True)
def _clean_trust():
    safety_override().deactivate("test-setup")
    TeamsApprovalDecider.reset_for_tests()
    yield
    safety_override().deactivate("test-teardown")
    TeamsApprovalDecider.reset_for_tests()


class _Client:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.cards: list[dict] = []

    async def send_message(self, conversation_id, content, service_url):
        self.sent.append(content)
        return f"mid-{len(self.sent)}"

    async def send_card(self, conversation_id, card, service_url):
        self.cards.append(card)
        return f"card-{len(self.cards)}"

    async def update_card(self, conversation_id, activity_id, card, service_url) -> bool:
        self.cards.append(card)
        return True

    async def update_message(self, conversation_id, activity_id, content, service_url) -> bool:
        return True

    async def send_typing(self, conversation_id, service_url) -> None:
        return None


class _Sessions:
    def __init__(self, *, busy: bool = False) -> None:
        self._busy = busy
        self.mirror_links: dict = {}
        self.opt_outs: dict = {}
        self.cleared: list[str] = []
        self.queues: dict[str, list] = {}
        self.released: list[str] = []

    async def aflush(self) -> None:
        # The resume release flushes the session map before it reports success; a
        # double without this correctly surfaces as a release FAILURE.
        return None

    def find_mirror_sessions(self, link, *, inbound_only: bool = False) -> list:
        # No resumed dashboard session in these tests, so routing is a no-op. Present
        # because Teams routes EVERY message through the resume resolver.
        return []

    def is_busy(self, key) -> bool:
        return self._busy

    def get_provider(self, key):
        return None

    def max_generation(self, bucket: str) -> int:
        return -1

    def clear_queue(self, key) -> None:
        self.cleared.append(key)

    def enqueue(self, key, msg_ts, text, *, force=False, **kw) -> bool:
        if not force and not self._busy:
            return False
        self.queues.setdefault(key, []).append((msg_ts, text, kw))
        return True

    def dequeue(self, key):
        queue = self.queues.get(key) or []
        return queue.pop(0) if queue else None

    def mirror_opt_out(self, key) -> bool:
        return bool(self.opt_outs.get(key))

    def set_mirror_opt_out(self, key, value) -> None:
        self.opt_outs[key] = value

    def get_mirror_link(self, key):
        return self.mirror_links.get(key)

    def set_mirror_link(self, key, link, *, reason="") -> None:
        self.mirror_links[key] = link

    def clear_mirror_link(self, key, *, reason="") -> bool:
        return self.mirror_links.pop(key, None) is not None

    def clear_mirror_links_at(self, location, *, reason="", **kw):
        """Release every binding pointing at one location (the unlink path)."""
        freed = [k for k, v in self.mirror_links.items() if v == location]
        for key in freed:
            self.mirror_links.pop(key, None)
        return freed

    def is_mirror_paused(self, key, *, origin=False) -> bool:
        return False

    def batched_save(self):
        return contextlib.nullcontext()


def _dispatcher(client, sessions=None) -> TeamsDispatcher:
    d = TeamsDispatcher(
        sessions=sessions or _Sessions(),
        ctx_builder=SimpleNamespace(hooks=SimpleNamespace(auto_approve_subagent_spawn=False)),
        cfg=SimpleNamespace(
            messaging=SimpleNamespace(
                queue_mode="steer", dm_scope="per_user", idle_reset_minutes=0, daily_reset_hour=-1
            ),
            agent=SimpleNamespace(default_agent="kirocrew", approval_mode="interactive"),
            teams=SimpleNamespace(soft_threshold_pct=80, hard_threshold_pct=95),
            dashboard=SimpleNamespace(url=""),
        ),
    )
    d.client = client
    return d


def _inbound(text: str) -> TeamsInbound:
    return TeamsInbound(
        conversation_id="CONV",
        conversation_type="personal",
        service_url=_SVC,
        text=text,
        user_email=_EMAIL,
        activity_id="act-1",
    )


class TestYolo:
    """Teams drives the ONE shared grant, through the shared helper.

    Deliberately NOT a channel-local trusted-session store: that would be a second
    grant with its own lifetime, its own audit trail and its own way to disagree with
    the dashboard about whether auto-approve is on, and "is YOLO on?" has to have one
    answer. So these tests assert against ``safety_override`` — the same object the
    dashboard toggle and every other channel's ``/yolo`` read.
    """

    @pytest.mark.asyncio
    async def test_on_arms_the_shared_grant(self) -> None:
        client = _Client()
        d = _dispatcher(client)
        assert safety_override().is_active() is False

        await d.handle_message(_inbound("/yolo on"))

        assert safety_override().is_active() is True
        assert "ON" in client.sent[-1]

    @pytest.mark.asyncio
    async def test_off_disarms_the_shared_grant(self) -> None:
        client = _Client()
        d = _dispatcher(client)
        await d.handle_message(_inbound("/yolo on"))

        await d.handle_message(_inbound("/yolo off"))

        assert safety_override().is_active() is False
        assert "OFF" in client.sent[-1]

    @pytest.mark.asyncio
    async def test_a_bare_yolo_reports_status_without_changing_it(self) -> None:
        client = _Client()
        d = _dispatcher(client)

        await d.handle_message(_inbound("/yolo"))

        assert safety_override().is_active() is False
        assert "Usage" in client.sent[-1]

    @pytest.mark.asyncio
    async def test_the_usage_line_renders_as_markdown_code(self) -> None:
        """Teams renders inline code in a plain message; Telegram does not.

        That spelling is the ONLY per-channel difference in these replies, which is
        why it is a ``YoloPhrasing`` constant rather than a per-channel sentence.
        """
        client = _Client()
        d = _dispatcher(client)

        await d.handle_message(_inbound("/yolo"))

        assert "`/yolo on`" in client.sent[-1]

    @pytest.mark.asyncio
    async def test_status_reports_the_grants_lifetime(self) -> None:
        client = _Client()
        d = _dispatcher(client)
        await d.handle_message(_inbound("/yolo on"))

        await d.handle_message(_inbound("/yolo"))

        assert "ON" in client.sent[-1]
        # The shared helper reports the deadline; expiry is its job, not ours.
        assert "remaining" in client.sent[-1], client.sent[-1]

    @pytest.mark.asyncio
    async def test_renew_is_an_action_not_a_status_query(self) -> None:
        client = _Client()
        d = _dispatcher(client)

        await d.handle_message(_inbound("/yolo renew"))

        assert "Usage" not in client.sent[-1], "renew must not fall through to status"

    @pytest.mark.asyncio
    async def test_teams_keeps_no_grant_of_its_own(self) -> None:
        """The regression guard for the whole design: one grant, not two."""
        from kiro_crew.teams import approvals

        client = _Client()
        d = _dispatcher(client)
        await d.handle_message(_inbound("/yolo on"))

        assert not [n for n in dir(approvals) if "trust" in n.lower() and n != "trusted"], dir(
            approvals
        )
        # Disarming the SHARED grant is enough to disarm Teams, because there is
        # nothing else holding one.
        safety_override().deactivate("test")
        assert safety_override().is_active() is False


class TestDashboardLink:
    @pytest.mark.asyncio
    async def test_a_link_is_minted_and_sent(self) -> None:
        client = _Client()
        d = _dispatcher(client)

        await d.handle_message(_inbound("/dashboard"))

        assert "token=" in client.sent[-1]
        assert "Dashboard link" in client.sent[-1]

    @pytest.mark.asyncio
    async def test_a_ttl_argument_is_honoured(self) -> None:
        client = _Client()
        d = _dispatcher(client)

        await d.handle_message(_inbound("/dashboard 30m"))

        assert "30m" in client.sent[-1]

    @pytest.mark.asyncio
    async def test_a_failure_is_reported_not_swallowed(self, monkeypatch) -> None:
        """A credential that could not be minted must say so."""
        import kiro_crew.dashboard.token_auth as ta

        def _boom(*a, **kw):
            raise RuntimeError("no signing key")

        monkeypatch.setattr(ta, "generate_token", _boom)
        client = _Client()
        d = _dispatcher(client)

        await d.handle_message(_inbound("/dashboard"))

        assert "Could not generate" in client.sent[-1]


class TestMirrorCommands:
    @pytest.mark.asyncio
    async def test_link_rebinds_through_the_shared_helper(self) -> None:
        client = _Client()
        sessions = _Sessions()
        d = _dispatcher(client, sessions)
        key = d._session_key(_EMAIL)
        sessions.opt_outs[key] = True

        await d.handle_message(_inbound("/link"))

        assert sessions.opt_outs[key] is False, "the opt-out must be withdrawn"
        assert key in sessions.mirror_links, "and the location claimed"
        assert client.sent[-1]

    @pytest.mark.asyncio
    async def test_unlink_persists_the_opt_out_before_releasing(self) -> None:
        """Mirroring is re-asserted every turn, so a release alone is undone."""
        client = _Client()
        sessions = _Sessions()
        d = _dispatcher(client, sessions)
        key = d._session_key(_EMAIL)

        await d.handle_message(_inbound("/unlink"))

        assert sessions.opt_outs[key] is True
        assert client.sent[-1]

    @pytest.mark.asyncio
    async def test_stop_goes_through_the_shared_handler(self) -> None:
        client = _Client()
        sessions = _Sessions()
        d = _dispatcher(client, sessions)

        await d.handle_message(_inbound("/stop"))

        assert sessions.cleared == [d._session_key(_EMAIL)]
        assert "🛑" in client.sent[-1]

    @pytest.mark.asyncio
    async def test_help_lists_every_command(self) -> None:
        from kiro_crew.teams.commands import COMMAND_SPEC

        client = _Client()
        d = _dispatcher(client)

        await d.handle_message(_inbound("/help"))

        for canonical, _, _ in COMMAND_SPEC:
            assert f"/{canonical}" in client.sent[-1]


class TestPromptCard:
    @pytest.mark.asyncio
    async def test_a_prompt_posts_a_card_and_arms_the_nonce(self) -> None:
        from kiro_crew.teams.renderer import TeamsRenderer
        from kiro_crew.teams.transport import TEAMS_CAPABILITIES

        client = _Client()
        decider = TeamsApprovalDecider(session_key="teams:s1")
        r = TeamsRenderer(
            client, "CONV", _SVC, TEAMS_CAPABILITIES, session_key="teams:s1", decider=decider
        )

        await r.on_prompt_choice([{"title": "fs_read", "purpose": "read a file"}], "9")

        assert len(client.cards) == 1
        actions = client.cards[0]["content"]["actions"]
        nonce = actions[0]["data"]["nonce"]
        # Armed BEFORE the card was posted, so a click that races the render is
        # still resolvable rather than refused as stale.
        assert decider._nonces["9"] == nonce

    @pytest.mark.asyncio
    async def test_no_decider_means_no_dead_buttons(self) -> None:
        from kiro_crew.teams.renderer import TeamsRenderer
        from kiro_crew.teams.transport import TEAMS_CAPABILITIES

        client = _Client()
        r = TeamsRenderer(client, "CONV", _SVC, TEAMS_CAPABILITIES)

        await r.on_prompt_choice([{"title": "fs_read"}], "9")

        assert client.cards == [], "buttons with nothing awaiting them are dead controls"

    @pytest.mark.asyncio
    async def test_a_prompt_falls_back_to_the_last_tool_title(self) -> None:
        from kiro_crew.teams.renderer import TeamsRenderer
        from kiro_crew.teams.transport import TEAMS_CAPABILITIES

        client = _Client()
        decider = TeamsApprovalDecider(session_key="teams:s1")
        r = TeamsRenderer(
            client, "CONV", _SVC, TEAMS_CAPABILITIES, session_key="teams:s1", decider=decider
        )
        await r.on_tool_call("t1", "execute_bash")

        await r.on_prompt_choice([{}], "9")

        body = client.cards[0]["content"]["body"][0]["text"]
        assert "execute_bash" in body


class TestChipsOutliveTheirTurn:
    """An [OPTIONS:] chip is posted at on_done and tapped LATER.

    The approval card resolves while its turn is still blocked on the decider, so
    it needs no reprieve. A chip does: if the dispatcher retires the renderer when
    the turn ends, every chip resolves against nothing and the advertised
    `rich_blocks` / `max_buttons` capability is unreachable in practice.
    """

    @pytest.mark.asyncio
    async def test_a_chip_is_still_resolvable_after_the_turn(self, monkeypatch) -> None:
        turns: list[str] = []

        async def _fake_drive(turn, **kw):
            # Stand in for the real pipeline: run the renderer's own finalization,
            # which is what posts the chip card.
            await turn.renderer.on_text_chunk("pick one\n[OPTIONS: yes | no]")
            await turn.renderer.on_done()
            turns.append(turn.user_text)

        async def _permitted(_c):
            return True

        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", _fake_drive)
        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.inbound_permitted", _permitted)
        client = _Client()
        d = _dispatcher(client)

        await d.handle_message(_inbound("choose something"))

        session_key = d._session_key(_EMAIL)
        assert (
            session_key in d._active_renderers
        ), "the renderer that posted chips must outlive its turn or the chips are dead"
        nonce = client.cards[-1]["content"]["actions"][0]["data"]["nonce"]

        # Now the user taps "no" — a separate inbound activity, after the turn.
        await d._handle_card_action(_click_option(nonce=nonce, index=1, label="no"))

        assert turns[-1] == "no", "the chip must run as a turn carrying its label"

    @pytest.mark.asyncio
    async def test_a_new_conversation_retires_the_chip_renderer(self, monkeypatch) -> None:
        """Otherwise nothing ever pops it: the entry is keyed by the OLD session key.

        Each retained renderer holds a whole answer buffer, so a user who works in
        short `/new`-separated conversations leaks one per conversation for the
        process lifetime -- and those chips are unreachable anyway, because `/new`
        cleared the session they belonged to.
        """

        async def _fake_drive(turn, **kw):
            await turn.renderer.on_text_chunk("pick one\n[OPTIONS: yes | no]")
            await turn.renderer.on_done()

        async def _permitted(_c):
            return True

        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", _fake_drive)
        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.inbound_permitted", _permitted)
        client = _Client()
        d = _dispatcher(client)
        await d.handle_message(_inbound("choose something"))
        assert d._active_renderers, "precondition: the chip renderer was retained"

        await d.handle_message(_inbound("/new"))

        assert d._active_renderers == {}

    @pytest.mark.asyncio
    async def test_a_pick_retires_the_renderer_it_resolved(self, monkeypatch) -> None:
        """A chip can only be picked once, so holding the renderer after that leaks."""

        calls = [0]

        async def _fake_drive(turn, **kw):
            calls[0] += 1
            if calls[0] == 1:
                # Only the FIRST turn offers chips; the chip's own turn is an
                # ordinary reply, exactly as a real model's would be.
                await turn.renderer.on_text_chunk("pick one\n[OPTIONS: yes | no]")
            await turn.renderer.on_done()

        async def _permitted(_c):
            return True

        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", _fake_drive)
        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.inbound_permitted", _permitted)
        client = _Client()
        d = _dispatcher(client)
        await d.handle_message(_inbound("choose something"))
        nonce = client.cards[-1]["content"]["actions"][0]["data"]["nonce"]

        await d._handle_card_action(_click_option(nonce=nonce, index=1, label="no"))

        assert d._active_renderers == {}

    @pytest.mark.asyncio
    async def test_a_turn_with_no_chips_retires_its_renderer(self, monkeypatch) -> None:
        """The reprieve is scoped: an ordinary turn must not leak its renderer."""

        async def _fake_drive(turn, **kw):
            await turn.renderer.on_text_chunk("just an answer")
            await turn.renderer.on_done()

        async def _permitted(_c):
            return True

        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", _fake_drive)
        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.inbound_permitted", _permitted)
        d = _dispatcher(_Client())

        await d.handle_message(_inbound("hello"))

        assert d._active_renderers == {}


def _click_option(*, nonce: str, index: int, label: str) -> TeamsInbound:
    from kiro_crew.teams.cards import KIND_OPTION

    return TeamsInbound(
        conversation_id="CONV",
        conversation_type="personal",
        service_url=_SVC,
        text="",
        user_email=_EMAIL,
        activity_id="act-2",
        card_value={"kc": KIND_OPTION, "nonce": nonce, "index": index, "label": label},
    )
