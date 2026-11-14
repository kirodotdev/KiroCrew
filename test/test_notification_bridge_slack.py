"""The notification bridge's governed delivery and Slack sink (RFC phase B1b).

Pins the exit criterion (a critical note on a routed channel lands as a Slack DM
with redacted content and paired SEL records), fail-closed governance for the host
and the publishing session, the per-transport rate limit, the audited skip of a
disconnected transport, and the escaped mrkdwn rendering.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from real_dashboard_state import settle_notification_writes
from redaction_structured_cases import github_classic

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.state_parts import dashboard_services
from kiro_crew.notifications import bridge_egress
from kiro_crew.notifications.bridge import BridgeDispatcher
from kiro_crew.notifications.bridge_slack import SlackBridgeSink, render_slack_parts
from kiro_crew.notifications.bus import NotificationPayload
from kiro_crew.notifications.rate_limit import RATE_LIMIT_BURST
from kiro_crew.notifications.settings import ChannelSettings
from kiro_crew.slack.format import SLACK_MAX_TEXT, escape_mrkdwn

#: A synthetic GitHub classic token (valid checksum) the redactor removes.
_SECRET = github_classic(seed=102)
_EXFIL_URL = "https://evil.example.com/steal?data=" + "A" * 250


class _FakeSlack:
    def __init__(self, dm: str | None = "D-OWNER") -> None:
        self.dm = dm
        self.opened: list[str] = []
        self.posts: list[tuple[str, str]] = []

    async def open_dm(self, user_id: str) -> str | None:
        self.opened.append(user_id)
        return self.dm

    async def post_message(self, channel: str, text: str) -> str:
        self.posts.append((channel, text))
        return "1.0"


class _Sel:
    def __init__(self) -> None:
        self.decisions: list[dict[str, Any]] = []
        self.deliveries: list[dict[str, Any]] = []

    def log_governance_decision(self, **kw: Any) -> None:
        self.decisions.append(kw)

    def log_tool_invocation(self, **kw: Any) -> None:
        self.deliveries.append(kw)


@pytest.fixture(autouse=True)
def _settle_writes():
    """Wait for queued notification appends before the conftest home pin is undone."""
    yield
    settle_notification_writes()


@pytest.fixture()
def sel_log(monkeypatch) -> _Sel:
    log = _Sel()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: log)
    return log


@pytest.fixture()
def settings(monkeypatch, tmp_path) -> ChannelSettings:
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    store = ChannelSettings()
    store.update("system.cron", deliver_to=["slack"])
    return store


def _dispatcher(settings: ChannelSettings, slack: Any, owner: str = "U-OWNER") -> BridgeDispatcher:
    dispatcher = BridgeDispatcher(settings=lambda: settings)
    dispatcher.register_sink(SlackBridgeSink(SimpleNamespace(slack_client=slack, owner_id=owner)))
    return dispatcher


def _note(**fields: Any) -> dict[str, Any]:
    return {
        "ts": "t1",
        "source": "system",
        "channel": "system.cron",
        "priority": "critical",
        "title": "Job failed",
        "body": "details",
        **fields,
    }


def _deny(scope_to_deny: str):
    def _vet(scope, item, **kw):
        permitted = scope != scope_to_deny
        return SimpleNamespace(permitted=permitted, reason="" if permitted else f"{scope} off")

    return _vet


class TestExitCriterion:
    @pytest.mark.asyncio
    async def test_critical_note_lands_as_a_redacted_owner_dm_with_paired_records(
        self, monkeypatch, tmp_path, sel_log
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
        slack = _FakeSlack()
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
            slack_client=slack,
            owner_id="U-OWNER",
        )
        state.notification_channel_settings.update("system.cron", deliver_to=["slack"])
        state.notification_bus.push(
            NotificationPayload(
                source="system",
                channel="system.cron",
                title=f"key {_SECRET}",
                body=f"see {_EXFIL_URL}",
                priority="critical",
            )
        )
        assert slack.posts == []  # scheduled, not awaited by the producer
        await state.notification_bridge.drain()

        assert slack.opened == ["U-OWNER"]
        [(channel, text)] = slack.posts
        assert channel == "D-OWNER"
        assert _SECRET not in text
        assert "evil.example.com/steal" not in text
        assert "[REDACTED" in text
        # Paired governance records, both scopes, then one delivery record.
        assert {(d["scope"], d["item"], d["outcome"]) for d in sel_log.decisions} == {
            ("capabilities.messaging", "", "allowed"),
            ("channels", "slack", "allowed"),
        }
        [delivery] = sel_log.deliveries
        assert delivery["outcome"] == "completed"
        assert delivery["tool_name"] == "notification_bridge:slack"
        assert delivery["downstream_service"] == "slack"
        assert _SECRET not in str(delivery)

    @pytest.mark.asyncio
    async def test_a_published_note_is_vetted_for_its_session_through_the_bus(
        self, monkeypatch, tmp_path, sel_log
    ):
        """The local sink pops ``publisher_session``; the bridge must still see it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            lambda scope, item, *, session_key, **kw: SimpleNamespace(
                permitted=session_key == "_host", reason="session denied"
            ),
        )
        slack = _FakeSlack()
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
            slack_client=slack,
            owner_id="U-OWNER",
        )
        state.notification_channel_settings.update("system.agent", deliver_to=["slack"])
        state.notification_bus.push(
            NotificationPayload(
                source="agent",
                channel="system.agent",
                title="t",
                body="b",
                priority="critical",
                publisher_session="dashboard:slot-1",
            )
        )
        await state.notification_bridge.drain()
        assert slack.posts == []
        [delivery] = sel_log.deliveries
        assert delivery["outcome"] == "denied"
        assert delivery["session_key"] == "dashboard:slot-1"
        assert "publisher_session" not in state._notification_log[-1]


class TestGovernance:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("scope", ["capabilities.messaging", "channels"])
    async def test_a_denied_scope_sends_nothing_and_is_audited(
        self, monkeypatch, settings, sel_log, scope
    ):
        monkeypatch.setattr("kiro_crew.platform.governance_profiles.vet_and_audit", _deny(scope))
        slack = _FakeSlack()
        dispatcher = _dispatcher(settings, slack)
        dispatcher.schedule(_note())
        await dispatcher.drain()
        assert slack.posts == [] and slack.opened == []
        [delivery] = sel_log.deliveries
        assert delivery["outcome"] == "denied"
        assert delivery["error"] == f"{scope} off"

    @pytest.mark.asyncio
    async def test_the_publishing_session_is_vetted_too_and_attributed(
        self, monkeypatch, settings, sel_log
    ):
        seen: list[str] = []

        def _vet(scope, item, *, session_key, **kw):
            seen.append(session_key)
            return SimpleNamespace(permitted=session_key == "_host", reason="session denied")

        monkeypatch.setattr("kiro_crew.platform.governance_profiles.vet_and_audit", _vet)
        slack = _FakeSlack()
        dispatcher = _dispatcher(settings, slack)
        dispatcher.schedule(_note(), publisher="dashboard:slot-1")
        await dispatcher.drain()
        assert seen == ["_host", "_host", "dashboard:slot-1"]
        assert slack.posts == []
        [delivery] = sel_log.deliveries
        assert delivery["session_key"] == "dashboard:slot-1"
        assert delivery["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_a_governance_error_denies(self, monkeypatch, settings, sel_log):
        def _boom(*a, **kw):
            raise RuntimeError("profile store unreadable")

        monkeypatch.setattr("kiro_crew.platform.governance_profiles.vet_and_audit", _boom)
        slack = _FakeSlack()
        dispatcher = _dispatcher(settings, slack)
        dispatcher.schedule(_note())
        await dispatcher.drain()
        assert slack.posts == []
        assert sel_log.deliveries[0]["outcome"] == "denied"
        assert "fail-closed" in sel_log.deliveries[0]["error"]

    def test_a_decision_without_permitted_denies(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            lambda *a, **kw: SimpleNamespace(),
        )
        assert bridge_egress.vet_delivery("slack", _note())

    def test_the_publishing_app_is_vetted_with_its_own_binding(self, monkeypatch):
        seen: list[tuple[str, str]] = []

        def _vet(scope, item, *, session_key, app="", **kw):
            seen.append((session_key, app))
            return SimpleNamespace(permitted=app != "demo", reason="app denied")

        monkeypatch.setattr("kiro_crew.platform.governance_profiles.vet_and_audit", _vet)
        assert bridge_egress.vet_delivery("slack", _note(source="app:demo")) == "app denied"
        assert seen == [("_host", ""), ("_host", ""), ("_host", "demo")]
        seen.clear()
        assert bridge_egress.vet_delivery("slack", _note(source="system")) is None
        assert {app for _, app in seen} == {""}


class TestSubjects:
    """The gateway resolves every session a note names before governance sees it."""

    def _state(self, monkeypatch, tmp_path) -> DashboardState:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
            slack_client=_FakeSlack(),
            owner_id="U-OWNER",
        )
        state.get_or_create_slot("s1")
        return state

    @pytest.mark.parametrize(
        ("note", "publisher"),
        [
            # A dead-turn alert publishes under the bare slot key.
            ({}, "s1"),
            # A note that offers choices drops its publisher but keeps its slot link.
            ({"slot": "s1"}, ""),
            ({"history_key": "dashboard:s1"}, ""),
            # A turn-ceiling notice names its session only in meta.
            ({"session_key": "dashboard:s1"}, ""),
        ],
    )
    def test_a_slot_resolves_to_the_session_its_turns_run_on(
        self, monkeypatch, tmp_path, note, publisher
    ):
        state = self._state(monkeypatch, tmp_path)
        assert dashboard_services.bridge_subjects(state, note, publisher) == ("dashboard:s1",)

    def test_an_unknown_session_key_is_kept_as_written(self, monkeypatch, tmp_path):
        state = self._state(monkeypatch, tmp_path)
        note = {"session_key": "telegram:123"}
        assert dashboard_services.bridge_subjects(state, note, "cron:job") == (
            "cron:job",
            "telegram:123",
        )

    def test_a_bare_slot_key_no_slot_holds_refuses(self, monkeypatch, tmp_path):
        state = self._state(monkeypatch, tmp_path)
        with pytest.raises(LookupError):
            dashboard_services.bridge_subjects(state, {}, "closed-slot")

    @pytest.mark.asyncio
    async def test_a_note_from_a_closed_slot_is_not_bridged(self, monkeypatch, tmp_path, sel_log):
        state = self._state(monkeypatch, tmp_path)
        state.notification_channel_settings.update("system.cron", deliver_to=["slack"])
        state.notification_bus.push(
            NotificationPayload(
                source="system",
                channel="system.cron",
                title="t",
                body="b",
                priority="critical",
                publisher_session="closed-slot",
            )
        )
        await state.notification_bridge.drain()
        assert state.slack_client.posts == []
        assert sel_log.deliveries == []
        assert len(state._notification_log) == 1  # the dashboard still shows it

    @pytest.mark.asyncio
    async def test_a_bare_slot_publisher_is_denied_by_its_dashboard_profile(
        self, monkeypatch, tmp_path, sel_log
    ):
        state = self._state(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            lambda scope, item, *, session_key, **kw: SimpleNamespace(
                permitted=not session_key.startswith("dashboard:"), reason="dashboard denied"
            ),
        )
        state.notification_channel_settings.update("system.cron", deliver_to=["slack"])
        state.notification_bus.push(
            NotificationPayload(
                source="system",
                channel="system.cron",
                title="t",
                body="b",
                priority="critical",
                publisher_session="s1",
            )
        )
        await state.notification_bridge.drain()
        assert state.slack_client.posts == []
        [delivery] = sel_log.deliveries
        assert (delivery["outcome"], delivery["session_key"]) == ("denied", "dashboard:s1")


class TestKeyedCommit:
    """A keyed replacement is bridged only once the dashboard committed it."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("committed", "posts"), [(True, 1), (False, 0)])
    async def test_the_bridge_waits_for_the_commit(self, settings, sel_log, committed, posts):
        commit = asyncio.get_running_loop().create_future()
        slack = _FakeSlack()
        dispatcher = BridgeDispatcher(settings=lambda: settings, committed=lambda note: commit)
        dispatcher.register_sink(
            SlackBridgeSink(SimpleNamespace(slack_client=slack, owner_id="U-OWNER"))
        )
        dispatcher.schedule(_note(key="k1"))
        # Only the wait for the commit is in flight; no delivery has started.
        assert len(dispatcher._tasks) == 1 and slack.posts == []
        commit.set_result(committed)
        await dispatcher.drain()
        assert len(slack.posts) == posts

    @pytest.mark.asyncio
    async def test_a_failed_keyed_rewrite_sends_nothing_through_the_bus(
        self, monkeypatch, tmp_path, sel_log
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
        slack = _FakeSlack()
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
            slack_client=slack,
            owner_id="U-OWNER",
        )
        coordinator = dashboard_services._notifications_for(state)
        monkeypatch.setattr(coordinator, "_rewrite_all", lambda rows: False)
        state.notification_channel_settings.update("system.cron", deliver_to=["slack"])
        state.notification_bus.push(
            NotificationPayload(
                source="system",
                channel="system.cron",
                title="t",
                body="b",
                priority="critical",
                key="k1",
            )
        )
        assert await state.last_notification_persist is False
        await state.notification_bridge.drain()
        assert slack.posts == []

    @pytest.mark.asyncio
    async def test_a_quiet_replacement_posts_no_second_chat_message(
        self, monkeypatch, tmp_path, sel_log
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
        slack = _FakeSlack()
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
            slack_client=slack,
            owner_id="U-OWNER",
        )
        state.notification_channel_settings.update("system.cron", deliver_to=["slack"])
        for title in ("build 1 of 3", "build 2 of 3"):
            state.notification_bus.push(
                NotificationPayload(
                    source="system",
                    channel="system.cron",
                    title=title,
                    body="b",
                    priority="critical",
                    key="build",
                )
            )
            assert await state.last_notification_persist is True
        await state.notification_bridge.drain()
        # The second note replaces the first at the same priority, so it is quiet:
        # the owner was told once, as the bell and banner tell them once.
        assert len(slack.posts) == 1


class TestTransport:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("slack", "owner"), [(None, "U-OWNER"), (_FakeSlack(), "")])
    async def test_disconnected_slack_is_an_audited_skip(self, settings, sel_log, slack, owner):
        dispatcher = _dispatcher(settings, slack, owner=owner)
        dispatcher.schedule(_note())
        await dispatcher.drain()
        [delivery] = sel_log.deliveries
        assert delivery["outcome"] == "skipped"
        assert dispatcher._limiter._buckets["slack"].tokens == RATE_LIMIT_BURST

    @pytest.mark.asyncio
    async def test_no_dm_channel_is_an_audited_skip(self, settings, sel_log):
        slack = _FakeSlack(dm=None)
        dispatcher = _dispatcher(settings, slack)
        dispatcher.schedule(_note())
        await dispatcher.drain()
        assert slack.posts == []
        assert sel_log.deliveries[0]["outcome"] == "skipped"

    @pytest.mark.asyncio
    async def test_a_post_failure_is_audited_as_an_error(self, settings, sel_log):
        slack = _FakeSlack()

        async def _fail(channel, text):
            raise ConnectionError("slack down")

        slack.post_message = _fail
        dispatcher = _dispatcher(settings, slack)
        dispatcher.schedule(_note())
        await dispatcher.drain()
        [delivery] = sel_log.deliveries
        assert delivery["outcome"] == "error"
        assert delivery["error"] == "ConnectionError"

    @pytest.mark.asyncio
    async def test_the_per_transport_budget_caps_a_firehose(self, monkeypatch, settings, sel_log):
        # Freeze the limiter's clock so no token refills mid-test.
        monkeypatch.setattr(
            "kiro_crew.notifications.rate_limit.time", SimpleNamespace(monotonic=lambda: 1000.0)
        )
        slack = _FakeSlack()
        dispatcher = _dispatcher(settings, slack)
        for _ in range(RATE_LIMIT_BURST + 2):
            dispatcher.schedule(_note())
        await dispatcher.drain()
        assert len(slack.posts) == RATE_LIMIT_BURST
        outcomes = [d["outcome"] for d in sel_log.deliveries]
        assert outcomes.count("rate_limited") == 2


class TestRender:
    def test_entities_are_escaped_so_nothing_pings(self):
        [text] = render_slack_parts(_note(title="<!channel> hi", body="<@U123> & co"))
        assert "<!channel>" not in text and "<@U123>" not in text
        assert "&lt;!channel&gt;" in text

    def test_critical_note_is_marked_and_carries_its_link(self):
        [text] = render_slack_parts(_note(url="/chat/abc"))
        first, *rest = text.split("\n")
        assert first.startswith(":rotating_light: *Job failed*")
        assert rest == ["details", "/chat/abc"]

    def test_default_note_has_no_marker_and_skips_empty_fields(self):
        assert render_slack_parts(_note(priority="default", body="")) == ["*Job failed*"]

    def test_a_body_that_escaping_grows_splits_without_losing_anything(self):
        """The bus admits a 20,000-char body; escaped it can pass Slack's limit."""
        body = "<x/>" * 5000
        parts = render_slack_parts(_note(title="t" * 500, body=body, url="/chat/" + "a" * 600))
        assert len(parts) > 1
        assert all(len(part) <= SLACK_MAX_TEXT for part in parts)
        # Nothing is cut: the escaped pieces rejoin to the escaped whole.
        joined = "".join(parts).removeprefix(":rotating_light: *" + "t" * 500 + "*\n")
        assert joined == escape_mrkdwn(body + "\n/chat/" + "a" * 600)

    @pytest.mark.asyncio
    async def test_every_part_is_posted_in_order(self, settings, sel_log):
        slack = _FakeSlack()
        dispatcher = _dispatcher(settings, slack)
        dispatcher.schedule(_note(body="&" * 20000))
        await dispatcher.drain()
        assert len(slack.posts) > 1
        assert "".join(text for _, text in slack.posts).count("&amp;") == 20000
        assert sel_log.deliveries[-1]["outcome"] == "completed"


class _BlocksSlack(_FakeSlack):
    """A Slack client that also posts Block Kit, as the action notes need."""

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[tuple[list[dict[str, Any]], str]] = []

    async def post_blocks(self, channel: str, blocks: list[dict[str, Any]], text: str) -> str:
        self.blocks.append((blocks, text))
        return "2.0"


def _shown(slack: _BlocksSlack) -> str:
    """Every text a person sees in the DM: plain messages and the blocks' sections.

    A message's top-level ``text`` is not here: with blocks present, Slack shows it
    only as the notification fallback, never in the message.
    """
    texts = [text for _, text in slack.posts]
    for blocks, _fallback in slack.blocks:
        texts += [b["text"]["text"] for b in blocks if b.get("type") == "section"]
    return "\n".join(texts)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["question", "approval"])
async def test_an_action_note_too_long_for_one_section_still_shows_its_text(kind):
    from kiro_crew.notifications import bridge_approval_note as approval_mod
    from kiro_crew.notifications import bridge_question_note as question_mod

    body = "q" * 4000  # Longer than one section block, shorter than one message.
    note = _note(channel="system.approval", body=body)
    note[approval_mod.ACTION_TOKEN_KEY] = "dashboard:s|7|nonce"
    if kind == "question":
        note[approval_mod.PRODUCER_KEY] = question_mod.PRODUCER
        note[question_mod.OPTIONS_KEY] = ["Yes", "No"]
    else:
        note[approval_mod.PRODUCER_KEY] = approval_mod.PRODUCER
        note[approval_mod.APPROVAL_DETAIL_KEY] = "ls"
    slack = _BlocksSlack()
    state = SimpleNamespace(slack_client=slack, owner_id="U-OWNER", bridge_actions=None)
    await SlackBridgeSink(state).deliver(note)
    assert body in _shown(slack), "the owner can read what the buttons answer"
    [(blocks, _fallback)] = slack.blocks
    assert blocks[-1]["type"] == "actions", "the buttons still sit under the note"
