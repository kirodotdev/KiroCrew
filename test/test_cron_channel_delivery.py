"""A cron job's output reaches the channel that scheduled it, not Slack alone.

An unattended run used to be delivered to Slack and nowhere else, so a job
created from Discord (or any other transport) was invisible on the surface its
owner actually watches. Routing is keyed off the job's ORIGIN session key -- the
session that created it -- because a ``cron:{id}`` key carries no channel
namespace of its own and so can never name the surface the job belongs to.

Pinned here, one leg at a time:

* the agent-result delivery in ``_cron_callback``;
* the post-subagent response (``_deliver_cron_response``);
* the script/command run-failure alert (``_alert_cron_failure``);
* the agent-job crash alert.

plus the four properties that make the routing safe and complete: Slack keeps
its existing delivery untouched, the fail-closed ``channels`` governance gate is
genuinely on the path, the egress redacts credentials, and a job with no
reachable channel still lands on the dashboard notification path instead of
being dropped.

Every collaborator is a double (transport, governance seam, SEL, cron service,
session manager), so no socket, no subprocess and no write outside the per-test
``KIROCREW_HOME`` pinned by ``test/conftest.py`` happens.
"""

from __future__ import annotations

from contextlib import ExitStack, asynccontextmanager, contextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.platform import governance_profiles
from kiro_crew.slack import gateway as gw

#: A Discord DM session key: ``{channel}:{agent}:{chatType}:{user}``. Direct, so
#: it is allowed to take ``_channel_reply_link``'s stored-channel rung.
DISCORD_KEY = "discord:kirocrew:direct:U9"

#: The postable Discord conversation the origin link names.
DISCORD_CONVERSATION = "C_DISCORD"

#: A credential shaped exactly like the one the redactors exist to catch.
LEAKED_KEY = "AKIAIOSFODNN7EXAMPLE"


# ─── Doubles ─────────────────────────────────────────────────────────────


def _make_orchestrator() -> Any:
    """A GatewayOrchestrator with mocked credentials and the cron timer disarmed.

    Returned as ``Any`` on purpose: every test swaps real collaborators for
    doubles, which do not satisfy the declared attribute types.
    """
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        return gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)


def _transport(*, proactive: bool = True, max_chars: int = 4000) -> MagicMock:
    tr = MagicMock()
    # ``max_message_bytes=0`` mirrors the real ``TransportCapabilities`` default:
    # the proactive egress legs chunk via ``chunk_for_transport``, which reads it
    # to pick the byte-aware splitter for a byte-capped channel (Webex) and the
    # char path otherwise. A fake omitting it would ``AttributeError`` on that read.
    tr.capabilities = SimpleNamespace(
        supports_proactive_send=proactive, max_message_chars=max_chars, max_message_bytes=0
    )
    tr.send_message = AsyncMock()
    tr.resolve_configured_target = AsyncMock(return_value=(DISCORD_CONVERSATION, ""))
    return tr


def _dashboard_state(transport: Any = None) -> MagicMock:
    ds = MagicMock()
    ds.notify = MagicMock()
    ds.has_slot = MagicMock(return_value=False)
    ds.get_slot = MagicMock(return_value=None)
    ds.conversation_log = None
    ds.get_channel_transport = MagicMock(return_value=transport)
    return ds


def _sessions(
    *,
    origin: Any = None,
    origins: dict[str, Any] | None = None,
    channels: dict[str, Any] | None = None,
) -> MagicMock:
    """A SessionManager double keyed by session key, not by a single return value.

    Two different keys are read on the cron path -- the job's ORIGIN key for the
    channel leg and the ``cron:{id}`` key for Slack's stored thread -- so a
    flat ``return_value`` would let one leg answer for the other. ``origin`` is
    shorthand for binding the Discord key; ``origins`` binds arbitrary keys, so a
    guard can be tested against a link it would otherwise happily send to.
    """
    stored = channels or {}
    links = dict(origins or {})
    if origin is not None:
        links[DISCORD_KEY] = origin
    sessions = MagicMock()
    sessions.get_origin_link = MagicMock(side_effect=links.get)
    sessions.get_mirror_link = MagicMock(return_value=None)
    sessions.get_channel = MagicMock(side_effect=stored.get)
    sessions.get_thread = MagicMock(return_value=None)
    sessions.set_channel = AsyncMock()
    sessions.set_thread = AsyncMock()
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.cancel_current = AsyncMock()
    sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    return sessions


def _cron_service_double(job: CronJob | None = None) -> MagicMock:
    svc = MagicMock()
    svc.start = AsyncMock()
    svc.start_reaper = MagicMock()
    svc.set_refresh_callback = MagicMock()
    svc.register_active_session_key = MagicMock()
    svc.clear_active_session_key = MagicMock()
    svc.get_job = MagicMock(return_value=job)
    return svc


def _slack_double() -> MagicMock:
    slack = MagicMock()
    slack.open_dm = AsyncMock(return_value="D_OWNER")
    slack.post_message = AsyncMock(return_value="111.1")
    slack.post_blocks = AsyncMock(return_value="222.2")
    return slack


def _job(**overrides: Any) -> CronJob:
    defaults: dict[str, Any] = dict(
        id="j1",
        name="digest",
        message="summarize the inbox",
        schedule=CronSchedule(kind="every", every_secs=300),
        approval_mode="auto",
        session_key=DISCORD_KEY,
        channel="",
        created_by="",
    )
    defaults.update(overrides)
    return CronJob(**defaults)


@contextmanager
def _governance(*, permitted: bool = True, deny_sessions: tuple[str, ...] = ()) -> Iterator[Any]:
    """Stub the audited governance seam BOTH gates resolve at call time.

    Two profiles govern one cron send: the cron surface (vetted against
    ``cron:{id}``) and the destination conversation (vetted against the origin
    key, inside ``_resolve_channel_target``). ``deny_sessions`` denies only the
    named session keys, which is what makes the two gates separable -- a single
    boolean would let either one alone account for a refusal.

    ``_resolve_channel_target`` imports ``vet_and_audit`` inside its own body and
    the gateway imports it at module load, so both bindings are patched.
    """
    denied = set(deny_sessions)

    def _vet(_scope: str, _item: str, *, session_key: str = "", **_kw: Any) -> Any:
        allowed = permitted and session_key not in denied
        return SimpleNamespace(permitted=allowed)

    vet = MagicMock(side_effect=_vet)
    with patch.object(governance_profiles, "vet_and_audit", vet):
        with patch.object(gw, "vet_and_audit", vet):
            yield vet


@asynccontextmanager
async def _cron_callback(orch: Any, *, svc: MagicMock) -> AsyncIterator[Any]:
    """Run ``_init_cron`` with its collaborators patched and yield ``on_job``.

    The callback resolves ``sel`` / ``vet_job_at_fire_time`` from module globals
    at CALL time, so it must be invoked while the patches are live -- hence a
    context manager rather than a plain factory.
    """
    captured: dict[str, Any] = {}

    async def _create(**kw: Any) -> MagicMock:
        captured["on_job"] = kw["on_job"]
        return svc

    with ExitStack() as stack:
        for patcher in (
            patch.object(gw.CronService, "create", AsyncMock(side_effect=_create)),
            patch.object(gw, "vet_job_at_fire_time", lambda job: ""),
            patch.object(gw, "sel", lambda: MagicMock()),
            patch.object(
                gw, "build_cron_session_context", lambda job: (f"cron:{job.id}", job.message)
            ),
        ):
            stack.enter_context(patcher)
        await orch._init_cron()
        assert "on_job" in captured
        yield captured["on_job"]


async def _run_result_leg(
    orch: Any, job: CronJob, *, result: str = "all clear", permitted: bool = True
) -> Any:
    """Drive the agent-result arm of ``_cron_callback`` once."""
    async with _cron_callback(orch, svc=_cron_service_double(job)) as on_job:
        with _governance(permitted=permitted):
            with patch.object(gw, "stream_and_collect", AsyncMock(return_value=result)):
                return await on_job(job)


def _discord_orch(*, slack: Any = None, transport: Any = None) -> tuple[Any, MagicMock]:
    """An orchestrator whose Discord-origin session resolves to a live transport."""
    tr = transport if transport is not None else _transport()
    orch = _make_orchestrator()
    orch.slack = slack
    orch.conv_log = None
    orch.subagent_mgr = None
    orch.dashboard_state = _dashboard_state(tr)
    orch.sessions = _sessions(origin=gw.ChannelLink("discord", channel_id=DISCORD_CONVERSATION))
    orch.ctx_builder = MagicMock()
    orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    return orch, tr


def _sent(transport: MagicMock) -> str:
    """Everything *transport* was asked to send, joined."""
    return "\n".join(call.args[1] for call in transport.send_message.await_args_list)


# ═══════════════════════════════════════════════════════════════════════════
# Origin resolution
# ═══════════════════════════════════════════════════════════════════════════


class TestCronOriginKey:
    """``_cron_origin_key`` maps a cron run back to the session that scheduled it."""

    def test_persistent_and_ephemeral_keys_both_resolve(self):
        orch = _make_orchestrator()
        orch.cron_svc = _cron_service_double(_job())
        assert orch._cron_origin_key("cron:j1") == DISCORD_KEY
        assert orch._cron_origin_key("cron:j1:run7") == DISCORD_KEY

    def test_unknown_job_and_missing_service_resolve_to_nothing(self):
        orch = _make_orchestrator()
        orch.cron_svc = _cron_service_double(None)
        assert orch._cron_origin_key("cron:j1") == ""
        orch.cron_svc = None
        assert orch._cron_origin_key("cron:j1") == ""

    def test_non_cron_key_resolves_to_nothing(self):
        orch = _make_orchestrator()
        orch.cron_svc = _cron_service_double(_job())
        assert orch._cron_origin_key(DISCORD_KEY) == ""
        assert orch._cron_origin_key("cron") == ""

    def test_non_string_origin_degrades_instead_of_raising(self):
        """``cron.json`` does not coerce this field, so a corrupt store must not raise."""
        orch = _make_orchestrator()
        corrupt = _job()
        corrupt.session_key = {"not": "a key"}  # type: ignore[assignment]
        orch.cron_svc = _cron_service_double(corrupt)
        assert orch._cron_origin_key("cron:j1") == ""


class TestDeliverCronToChannel:
    """``_deliver_cron_to_channel`` only sends where a channel actually owns the job."""

    @pytest.mark.asyncio
    async def test_slack_origin_is_left_to_the_slack_leg(self):
        """A live sendable link is present, so only the namespace guard refuses."""
        orch, tr = _discord_orch()
        orch.sessions = _sessions(
            origins={"slack:1785.1": gw.ChannelLink("discord", channel_id=DISCORD_CONVERSATION)}
        )
        with _governance(permitted=True):
            assert (
                await orch._deliver_cron_to_channel("slack:1785.1", "done", actor_key="cron:j1")
                is False
            )
        tr.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dashboard_origin_is_not_a_channel(self):
        orch, tr = _discord_orch()
        orch.sessions = _sessions(
            origins={
                "dashboard:chat-1-2": gw.ChannelLink("discord", channel_id=DISCORD_CONVERSATION)
            }
        )
        with _governance(permitted=True):
            assert (
                await orch._deliver_cron_to_channel(
                    "dashboard:chat-1-2", "done", actor_key="cron:j1"
                )
                is False
            )
        tr.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blank_origin_and_blank_text_send_nothing(self):
        orch, tr = _discord_orch()
        assert await orch._deliver_cron_to_channel("", "done", actor_key="cron:j1") is False
        assert await orch._deliver_cron_to_channel(DISCORD_KEY, "   ", actor_key="cron:j1") is False
        tr.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cron_surfaces_own_denial_refuses_the_send(self):
        """The destination permits, the cron surface does not: tightest wins."""
        orch, tr = _discord_orch()
        with _governance(deny_sessions=("cron:j1",)):
            delivered = await orch._deliver_cron_to_channel(
                DISCORD_KEY, "done", actor_key="cron:j1"
            )
        assert delivered is False
        tr.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_destination_denial_refuses_the_send(self):
        """The cron surface permits, the destination conversation does not."""
        orch, tr = _discord_orch()
        with _governance(deny_sessions=(DISCORD_KEY,)):
            delivered = await orch._deliver_cron_to_channel(
                DISCORD_KEY, "done", actor_key="cron:j1"
            )
        assert delivered is False
        tr.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_both_permitting_delivers(self):
        orch, tr = _discord_orch()
        with _governance() as vet:
            assert (
                await orch._deliver_cron_to_channel(DISCORD_KEY, "done", actor_key="cron:j1")
                is True
            )
        assert "done" in _sent(tr)
        vetted = {c.kwargs.get("session_key") for c in vet.call_args_list}
        assert vetted == {"cron:j1", DISCORD_KEY}

    @pytest.mark.asyncio
    async def test_unusable_gate_answer_is_not_permission(self):
        orch, tr = _discord_orch()
        with patch.object(gw, "vet_and_audit", MagicMock(return_value=object())):
            delivered = await orch._deliver_cron_to_channel(
                DISCORD_KEY, "done", actor_key="cron:j1"
            )
        assert delivered is False
        tr.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raising_gate_refuses_rather_than_degrades(self):
        orch, tr = _discord_orch()
        with patch.object(
            gw, "vet_and_audit", MagicMock(side_effect=RuntimeError("profile dir unreadable"))
        ):
            delivered = await orch._deliver_cron_to_channel(
                DISCORD_KEY, "done", actor_key="cron:j1"
            )
        assert delivered is False
        tr.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_audit_names_cron_not_the_subagent_surface(self):
        """The stored-channel rung logs an allow-list decision; it must name cron."""
        orch, tr = _discord_orch()
        orch.sessions = _sessions(channels={DISCORD_KEY: "discord:U9"})
        audit = MagicMock()
        with _governance(permitted=True):
            with patch.object(gw, "sel", lambda: audit):
                assert (
                    await orch._deliver_cron_to_channel(DISCORD_KEY, "done", actor_key="cron:j1")
                    is True
                )
        operations = [c.kwargs.get("operation") for c in audit.log_api_access.call_args_list]
        assert "cron.reply_target_resolve" in operations


# ═══════════════════════════════════════════════════════════════════════════
# Leg 1: the agent-result delivery
# ═══════════════════════════════════════════════════════════════════════════


class TestResultReachesOriginChannel:
    """A job's result lands on the channel that scheduled it."""

    @pytest.mark.asyncio
    async def test_discord_origin_job_delivers_to_discord(self):
        orch, tr = _discord_orch()
        job = _job()
        assert await _run_result_leg(orch, job, result="3 new issues") == "3 new issues"
        tr.send_message.assert_awaited()
        assert tr.send_message.await_args.args[0] == DISCORD_CONVERSATION
        assert "3 new issues" in _sent(tr)

    @pytest.mark.asyncio
    async def test_delivery_advances_dedup_without_slack(self):
        """A Slack-less install must be able to suppress an unchanged result."""
        orch, _tr = _discord_orch()
        job = _job()
        await _run_result_leg(orch, job, result="unchanged")
        assert job.last_posted_hash != ""
        assert job.last_posted_at > 0

    @pytest.mark.asyncio
    async def test_slack_origin_job_still_delivers_to_slack_only(self):
        slack = _slack_double()
        orch, tr = _discord_orch(slack=slack)
        job = _job(session_key="slack:1785370133.085469", channel="C_SLACK")
        assert await _run_result_leg(orch, job, result="slack result") == "slack result"
        slack.post_blocks.assert_awaited_once()
        assert slack.post_blocks.await_args.args[0] == "C_SLACK"
        tr.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_originless_job_is_not_dropped(self):
        """No channel and no Slack: the dashboard bell is the floor, not a drop."""
        orch, tr = _discord_orch()
        job = _job(session_key="")
        assert await _run_result_leg(orch, job, result="orphan result") == "orphan result"
        tr.send_message.assert_not_awaited()
        bodies = [c.args[2] for c in orch.dashboard_state.notify.call_args_list]
        assert any("orphan result" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_governance_denial_sends_nothing_and_still_notifies(self):
        orch, tr = _discord_orch()
        job = _job()
        with _governance(permitted=False):
            await _run_result_leg(orch, job, result="denied result", permitted=False)
        tr.send_message.assert_not_awaited()
        orch.dashboard_state.get_channel_transport.assert_not_called()
        bodies = [c.args[2] for c in orch.dashboard_state.notify.call_args_list]
        assert any("denied result" in b for b in bodies)
        # A denied egress never reached anyone, so dedup must not advance.
        assert job.last_posted_hash == ""

    @pytest.mark.asyncio
    async def test_transport_without_proactive_send_is_refused(self):
        orch, tr = _discord_orch(transport=_transport(proactive=False))
        job = _job()
        await _run_result_leg(orch, job, result="no proactive")
        tr.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_credential_in_result_is_redacted_before_the_transport(self):
        orch, tr = _discord_orch()
        job = _job()
        await _run_result_leg(orch, job, result=f"key is {LEAKED_KEY} ok")
        body = _sent(tr)
        assert body, "nothing was delivered, so redaction was not exercised"
        assert LEAKED_KEY not in body

    @pytest.mark.asyncio
    async def test_delivery_failure_does_not_fail_the_job(self):
        """A messaging fault must not march the job toward auto-pause."""
        tr = _transport()
        tr.send_message = AsyncMock(side_effect=RuntimeError("gateway 503"))
        orch, _tr = _discord_orch(transport=tr)
        job = _job()
        assert await _run_result_leg(orch, job, result="fine") == "fine"
        tr.send_message.assert_awaited()
        assert job.consecutive_failures == 0
        assert job.last_posted_hash == ""

    @pytest.mark.asyncio
    async def test_silent_job_delivers_nowhere(self):
        orch, tr = _discord_orch()
        job = _job(silent=True)
        await _run_result_leg(orch, job, result="quiet")
        tr.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_escaping_delivery_error_never_fails_the_run(self):
        """The transport leg swallows its own faults; this pins the hard boundary.

        An exception escaping into the callback's outer handler would record a
        failure for a run that SUCCEEDED and march the job toward auto-pause.
        """
        orch, _tr = _discord_orch()
        orch._deliver_cron_to_channel = AsyncMock(side_effect=RuntimeError("transport imploded"))
        job = _job()
        assert await _run_result_leg(orch, job, result="fine") == "fine"
        assert job.consecutive_failures == 0


# ═══════════════════════════════════════════════════════════════════════════
# Leg 2: the post-subagent response
# ═══════════════════════════════════════════════════════════════════════════


class TestDeliverCronResponseChannelLeg:
    """``_deliver_cron_response`` routes a post-subagent turn to the origin channel."""

    @pytest.mark.asyncio
    async def test_discord_origin_delivers_without_slack(self):
        orch, tr = _discord_orch()
        orch.cron_svc = _cron_service_double(_job())
        with _governance(permitted=True):
            assert await orch._deliver_cron_response("cron:j1", "subagent says hi") is True
        assert tr.send_message.await_args.args[0] == DISCORD_CONVERSATION
        assert "subagent says hi" in _sent(tr)

    @pytest.mark.asyncio
    async def test_slack_leg_is_unchanged_for_a_slack_origin(self):
        slack = _slack_double()
        orch, tr = _discord_orch(slack=slack)
        orch.cron_svc = _cron_service_double(_job(session_key="slack:1785370133.085469"))
        orch.sessions = _sessions(channels={"cron:j1": "C_SLACK"})
        with _governance(permitted=True):
            assert await orch._deliver_cron_response("cron:j1", "hello") is True
        assert slack.post_message.await_args.args[0] == "C_SLACK"
        tr.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_channel_delivery_survives_an_unresolvable_slack_channel(self):
        """A resolved channel leg must not be reported as a drop by the Slack leg."""
        slack = _slack_double()
        slack.open_dm = AsyncMock(return_value=None)
        orch, tr = _discord_orch(slack=slack)
        orch.cron_svc = _cron_service_double(_job())
        with _governance(permitted=True):
            assert await orch._deliver_cron_response("cron:j1", "reached discord") is True
        assert "reached discord" in _sent(tr)
        slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_silent_delivers_to_no_surface(self):
        orch, tr = _discord_orch()
        orch.cron_svc = _cron_service_double(_job())
        with _governance(permitted=True):
            assert await orch._deliver_cron_response("cron:j1", "hi", silent=True) is False
        tr.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_governance_denial_reports_no_delivery(self):
        orch, tr = _discord_orch()
        orch.cron_svc = _cron_service_double(_job())
        with _governance(permitted=False):
            assert await orch._deliver_cron_response("cron:j1", "blocked") is False
        tr.send_message.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# Legs 3 and 4: the failure alerts
# ═══════════════════════════════════════════════════════════════════════════


async def _alert_run_failure(orch: Any, job: CronJob, detail: str) -> None:
    """Reach ``_alert_cron_failure`` through the script arm that calls it."""
    async with _cron_callback(orch, svc=_cron_service_double(job)) as on_job:
        with _governance(permitted=True):
            with patch.object(
                gw, "run_script_sandboxed", MagicMock(side_effect=RuntimeError(detail))
            ):
                await on_job(job)


class TestRunFailureAlertChannelLeg:
    """A script/command job's failure reason reaches its own channel."""

    @pytest.mark.asyncio
    async def test_discord_origin_hears_about_the_failure(self):
        orch, tr = _discord_orch()
        job = _job(script="raise SystemExit(1)", message="")
        await _alert_run_failure(orch, job, "boom in the script")
        assert "boom in the script" in _sent(tr)
        assert "digest" in _sent(tr)

    @pytest.mark.asyncio
    async def test_alert_is_plain_text_not_slack_mrkdwn(self):
        """No other transport parses mrkdwn, so the fence and bolding stay Slack's."""
        orch, tr = _discord_orch()
        job = _job(script="raise SystemExit(1)", message="")
        await _alert_run_failure(orch, job, "boom")
        body = _sent(tr)
        assert body
        assert "```" not in body
        assert "*Cron:" not in body

    @pytest.mark.asyncio
    async def test_channel_delivery_advances_dedup_and_stands_slack_down(self):
        """The reason reached the user, so an identical next failure is a dup."""
        slack = _slack_double()
        orch, tr = _discord_orch(slack=slack)
        job = _job(script="raise SystemExit(1)", message="")
        await _alert_run_failure(orch, job, "boom")
        assert "boom" in _sent(tr)
        slack.post_message.assert_not_awaited()
        assert job.last_failure_hash != ""
        assert job.last_failure_at > 0

    @pytest.mark.asyncio
    async def test_a_pinned_channel_takes_the_alert_and_the_origin_does_not(self):
        """A pin names where the user asked to be told; the origin is not that.

        Without the pin guard the channel leg runs anyway, reports a delivery, and
        stands the Slack leg down -- so the alert lands on the origin conversation
        and never on the destination the user named.
        """
        slack = _slack_double()
        orch, tr = _discord_orch(slack=slack)
        job = _job(script="raise SystemExit(1)", message="", channel="C_PINNED")
        await _alert_run_failure(orch, job, "boom")
        tr.send_message.assert_not_awaited()
        slack.post_message.assert_awaited()
        assert slack.post_message.await_args.args[0] == "C_PINNED"

    @pytest.mark.asyncio
    async def test_a_pinned_alert_that_throws_leaves_dedup_alone(self):
        """Nobody heard, so an identical next failure must alert again."""
        slack = _slack_double()
        slack.post_message = AsyncMock(side_effect=RuntimeError("slack 500"))
        orch, tr = _discord_orch(slack=slack)
        job = _job(script="raise SystemExit(1)", message="", channel="C_PINNED")
        await _alert_run_failure(orch, job, "boom")
        tr.send_message.assert_not_awaited()
        assert job.last_failure_hash == ""

    @pytest.mark.asyncio
    async def test_silent_job_alerts_no_channel(self):
        orch, tr = _discord_orch()
        job = _job(script="raise SystemExit(1)", message="", silent=True)
        await _alert_run_failure(orch, job, "boom")
        tr.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_governance_denial_alerts_no_channel(self):
        orch, tr = _discord_orch()
        job = _job(script="raise SystemExit(1)", message="")
        async with _cron_callback(orch, svc=_cron_service_double(job)) as on_job:
            with _governance(permitted=False):
                with patch.object(
                    gw, "run_script_sandboxed", MagicMock(side_effect=RuntimeError("boom"))
                ):
                    await on_job(job)
        tr.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_escaping_channel_error_still_leaves_the_slack_alert(self):
        """The alert's outer guard would swallow the whole rest of the alert."""
        slack = _slack_double()
        orch, _tr = _discord_orch(slack=slack)
        orch._deliver_cron_to_channel = AsyncMock(side_effect=RuntimeError("transport imploded"))
        job = _job(script="raise SystemExit(1)", message="", channel="C_SLACK")
        await _alert_run_failure(orch, job, "boom")
        slack.post_message.assert_awaited()
        assert "boom" in slack.post_message.await_args.args[1]


class TestAgentCrashAlertChannelLeg:
    """An agent job that crashes tells its own channel why."""

    async def _crash(self, orch: Any, job: CronJob, *, permitted: bool = True) -> None:
        async with _cron_callback(orch, svc=_cron_service_double(job)) as on_job:
            with _governance(permitted=permitted):
                with patch.object(
                    gw,
                    "stream_and_collect",
                    AsyncMock(side_effect=ValueError("context assembly exploded")),
                ):
                    with pytest.raises(ValueError):
                        await on_job(job)

    @pytest.mark.asyncio
    async def test_discord_origin_hears_about_the_crash(self):
        orch, tr = _discord_orch()
        job = _job()
        await self._crash(orch, job)
        body = _sent(tr)
        assert "context assembly exploded" in body
        assert "```" not in body

    @pytest.mark.asyncio
    async def test_crash_delivery_advances_dedup_and_stands_slack_down(self):
        """The reason reached the user, so an identical next crash is a dup."""
        slack = _slack_double()
        orch, tr = _discord_orch(slack=slack)
        job = _job()
        await self._crash(orch, job)
        assert "context assembly exploded" in _sent(tr)
        slack.post_message.assert_not_awaited()
        assert job.last_failure_hash != ""
        assert job.last_failure_at > 0

    @pytest.mark.asyncio
    async def test_a_pinned_channel_takes_the_crash_alert_too(self):
        """Same pin rule as the run-failure and result legs."""
        slack = _slack_double()
        orch, tr = _discord_orch(slack=slack)
        job = _job(channel="C_PINNED")
        await self._crash(orch, job)
        tr.send_message.assert_not_awaited()
        slack.post_message.assert_awaited()
        assert slack.post_message.await_args.args[0] == "C_PINNED"

    @pytest.mark.asyncio
    async def test_silent_job_crashes_quietly(self):
        orch, tr = _discord_orch()
        job = _job(silent=True)
        await self._crash(orch, job)
        tr.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_governance_denial_sends_nothing(self):
        orch, tr = _discord_orch()
        job = _job()
        await self._crash(orch, job, permitted=False)
        tr.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_escaping_channel_error_does_not_replace_the_run_error(self):
        """The run's own exception is the story; an alert fault must not mask it."""
        orch, _tr = _discord_orch()
        orch._deliver_cron_to_channel = AsyncMock(side_effect=RuntimeError("transport imploded"))
        job = _job()
        await self._crash(orch, job)


class TestSlackStandsDownOnlyOnConfirmedDelivery:
    """Slack stands down for a DELIVERY, never for a prediction.

    A predicate saying "a channel would take this" is not the same claim as "a
    channel took it". Standing the Slack leg down on the former loses the result
    outright whenever the channel send is refused by governance or fails on the
    wire, which is the whole class of failure the fallback exists for.
    """

    @pytest.mark.asyncio
    async def test_a_governance_denied_channel_send_still_reaches_slack(self) -> None:
        slack = _slack_double()
        orch, tr = _discord_orch(slack=slack)
        with _governance(permitted=False):
            delivered = await orch._deliver_cron_to_channel(
                DISCORD_KEY, "done", actor_key="cron:j1"
            )
        assert delivered is False
        tr.send_message.assert_not_awaited()
        # False is the signal the caller gates its Slack leg on, so a refusal
        # leaves Slack as the delivery rather than silently dropping the result.

    @pytest.mark.asyncio
    async def test_a_failing_channel_transport_reports_not_delivered(self) -> None:
        tr = _transport()
        tr.send_message = AsyncMock(side_effect=RuntimeError("wire down"))
        orch, _tr = _discord_orch(transport=tr)
        with _governance():
            try:
                delivered = await orch._deliver_cron_to_channel(
                    DISCORD_KEY, "done", actor_key="cron:j1"
                )
            except RuntimeError:
                # Raising is also acceptable: the caller wraps this leg and falls
                # through to Slack. What must NOT happen is a truthy return.
                delivered = False
        assert delivered is False


class TestFailureAuditNamesOnlyLandedSurfaces:
    """The SEL trail names the surfaces an alert LEFT ON, not the ones configured.

    ``self.slack`` being set means a Slack client exists, which is a different
    claim from "this alert went out over Slack": the one-surface rule stands the
    owner DM down whenever the originating channel already heard, and the post can
    also resolve no channel or raise on the wire. Recording a Slack egress that
    never happened corrupts the one question the record is kept to answer -- where
    did this content go -- and it does so silently, in the direction that
    overstates exposure.
    """

    @staticmethod
    def _downstream(audit: MagicMock, tool: str) -> list[str]:
        return [
            call.kwargs.get("downstream_service")
            for call in audit.log_tool_invocation.call_args_list
            if call.kwargs.get("tool_name") == tool
        ]

    @staticmethod
    async def _script_failure(orch: Any, job: CronJob, audit: MagicMock) -> None:
        async with _cron_callback(orch, svc=_cron_service_double(job)) as on_job:
            with _governance(permitted=True):
                with patch.object(gw, "sel", lambda: audit):
                    with patch.object(
                        gw, "run_script_sandboxed", MagicMock(side_effect=RuntimeError("boom"))
                    ):
                        await on_job(job)

    @staticmethod
    async def _agent_crash(orch: Any, job: CronJob, audit: MagicMock) -> None:
        async with _cron_callback(orch, svc=_cron_service_double(job)) as on_job:
            with _governance(permitted=True):
                with patch.object(gw, "sel", lambda: audit):
                    with patch.object(
                        gw, "stream_and_collect", AsyncMock(side_effect=ValueError("crashed"))
                    ):
                        with pytest.raises(ValueError):
                            await on_job(job)

    @pytest.mark.asyncio
    async def test_run_failure_answered_on_discord_does_not_claim_slack(self) -> None:
        orch, tr = _discord_orch(slack=_slack_double())
        job = _job(script="raise SystemExit(1)", message="")
        audit = MagicMock()
        await self._script_failure(orch, job, audit)
        assert "boom" in _sent(tr)
        assert self._downstream(audit, "cron_run_failure_alert") == ["discord"]

    @pytest.mark.asyncio
    async def test_run_failure_names_slack_when_slack_is_the_one_that_posted(self) -> None:
        """The dashboard origin has no channel leg, so the owner DM is the delivery."""
        slack = _slack_double()
        orch, tr = _discord_orch(slack=slack)
        job = _job(
            script="raise SystemExit(1)",
            message="",
            session_key="dashboard:kirocrew:direct:local",
            channel="C_SLACK",
        )
        audit = MagicMock()
        await self._script_failure(orch, job, audit)
        tr.send_message.assert_not_awaited()
        slack.post_message.assert_awaited()
        assert self._downstream(audit, "cron_run_failure_alert") == ["slack"]

    @pytest.mark.asyncio
    async def test_run_failure_claims_nothing_when_the_slack_post_raises(self) -> None:
        slack = _slack_double()
        slack.post_message = AsyncMock(side_effect=RuntimeError("slack 500"))
        orch, _tr = _discord_orch(slack=slack)
        job = _job(
            script="raise SystemExit(1)",
            message="",
            session_key="dashboard:kirocrew:direct:local",
            channel="C_SLACK",
        )
        audit = MagicMock()
        await self._script_failure(orch, job, audit)
        assert self._downstream(audit, "cron_run_failure_alert") == ["none"]

    @pytest.mark.asyncio
    async def test_crash_answered_on_discord_does_not_claim_slack(self) -> None:
        orch, tr = _discord_orch(slack=_slack_double())
        job = _job()
        audit = MagicMock()
        await self._agent_crash(orch, job, audit)
        assert "crashed" in _sent(tr)
        assert self._downstream(audit, "cron_failure_alert") == ["discord"]

    @pytest.mark.asyncio
    async def test_crash_names_slack_when_slack_is_the_one_that_posted(self) -> None:
        slack = _slack_double()
        orch, tr = _discord_orch(slack=slack)
        job = _job(session_key="dashboard:kirocrew:direct:local", channel="C_SLACK")
        audit = MagicMock()
        await self._agent_crash(orch, job, audit)
        tr.send_message.assert_not_awaited()
        slack.post_message.assert_awaited()
        assert self._downstream(audit, "cron_failure_alert") == ["slack"]


class TestChannelEgressMeetsTheDisplayFloor:
    """`_deliver_channel_reply` is where every proactive channel egress converges.

    Cron results, run-failure alerts, crash alerts and subagent completions all
    land here, and NONE of them passes a renderer -- a renderer is where a turn
    gets the display-form floor. So a literal-only scan at this chokepoint is a
    gap every one of those callers inherits: neither `AKIA**<rest>**` nor
    `[AKIA](https://x)<rest>` matches a credential pattern as written, and the
    client renders the markup away and shows the reader an intact key.
    """

    _COLLAPSING = "AKIA**IOSFODNN7EXAMPLE**"

    @pytest.mark.asyncio
    async def test_a_markdown_collapse_credential_never_reaches_the_transport(self):
        orch, tr = _discord_orch()
        with _governance():
            assert (
                await orch._deliver_cron_to_channel(
                    DISCORD_KEY, f"done {self._COLLAPSING}", actor_key="cron:j1"
                )
                is True
            )
        body = _sent(tr)
        assert body
        assert "AKIAIOSFODNN7EXAMPLE" not in body.replace("*", "")
        assert self._COLLAPSING not in body

    @pytest.mark.asyncio
    async def test_the_literal_form_is_still_caught(self):
        orch, tr = _discord_orch()
        with _governance():
            await orch._deliver_cron_to_channel(
                DISCORD_KEY, f"done {LEAKED_KEY}", actor_key="cron:j1"
            )
        assert LEAKED_KEY not in _sent(tr)

    @pytest.mark.asyncio
    async def test_an_ordinary_body_keeps_its_formatting(self):
        """The floor must not reformat a message that carries no credential."""
        orch, tr = _discord_orch()
        body = "Run **finished** in `2m` - see [the log](https://example.com/r/1)."
        with _governance():
            await orch._deliver_cron_to_channel(DISCORD_KEY, body, actor_key="cron:j1")
        assert _sent(tr) == body
