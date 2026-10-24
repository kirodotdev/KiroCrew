"""Coverage for where the Kiro Crew gateway delivers a cron's results and alerts.

Covers ``_channel_reply_link``'s resolution ladder and ``_deliver_channel_reply``'s early
refusals, a cron created from a non-Slack channel reporting back to that channel (with
the duplicate-suppression anchor), failure alerts following the results, task-runner
approval notices preferring the originating conversation over the owner DM, and a reply's
embedded local images reaching Slack as native uploads. The shared harness comes from
``slack_gateway_cron_exec_coverage_helpers.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_cron_exec_coverage_helpers import (
    _cron_cb,
    _cron_message_cb,
    _job,
    _make_orchestrator,
    _message_arm_sessions,
    _mock_dashboard_state,
)

from kiro_crew.slack import gateway as gw


def _channel_sessions(
    *,
    origin: Any = None,
    mirror: Any = None,
    stored: Any = "",
    origin_raises: bool = False,
    stored_raises: bool = False,
) -> MagicMock:
    """A SessionManager double exposing only the link-resolution surface."""
    sessions = MagicMock()
    if origin_raises:
        sessions.get_origin_link = MagicMock(side_effect=RuntimeError("map wedged"))
    else:
        sessions.get_origin_link = MagicMock(return_value=origin)
    sessions.get_mirror_link = MagicMock(return_value=mirror)
    if stored_raises:
        sessions.get_channel = MagicMock(side_effect=RuntimeError("map wedged"))
    else:
        sessions.get_channel = MagicMock(return_value=stored)
    return sessions


# ═══════════════════════════════════════════════════════════════════════════
# Non-Slack channel reply resolution
# ═══════════════════════════════════════════════════════════════════════════


class TestChannelReplyLink:
    """``_channel_reply_link``'s resolution ladder and its refusals."""

    def test_slack_and_local_keys_return_none(self):
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions()
        assert orch._channel_reply_link("slack:C1") is None
        assert orch._channel_reply_link("dashboard:chat-1-2") is None

    def test_no_session_manager_returns_none(self):
        orch = _make_orchestrator()
        orch.sessions = None
        assert orch._channel_reply_link("discord:kirocrew:direct:U9") is None

    def test_link_getter_failure_falls_through_to_stored_channel(self):
        """A raising link getter degrades to the next rung, it does not propagate."""
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(origin_raises=True, stored="discord:U9")
        resolved = orch._channel_reply_link("discord:kirocrew:direct:U9")
        assert resolved is not None
        link, needs_dm = resolved
        assert (link.channel_type, link.channel_id, needs_dm) == ("discord", "U9", True)

    def test_stored_channel_lookup_failure_returns_none(self):
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(stored_raises=True)
        assert orch._channel_reply_link("discord:kirocrew:direct:U9") is None

    @pytest.mark.parametrize(
        ("stored", "key"),
        [
            pytest.param(
                "", "discord:kirocrew:direct:U9", id="test_no_stored_channel_returns_none"
            ),
            # A group key's stored value is the sender, so it must not become a DM.
            pytest.param(
                "discord:U9",
                "discord:kirocrew:group:C1",
                id="test_group_session_never_takes_the_stored_rung",
            ),
        ],
    )
    def test_stored_rung_yields_no_link(self, stored: str, key: str):
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(stored=stored)
        assert orch._channel_reply_link(key) is None

    @pytest.mark.parametrize(
        "stored",
        ["nocolon", "slack:U9", ":U9", "discord:"],
        ids=["no-separator", "slack-typed", "empty-type", "empty-peer"],
    )
    def test_unusable_stored_value_returns_none(self, stored):
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(stored=stored)
        assert orch._channel_reply_link("discord:kirocrew:direct:U9") is None

    @pytest.mark.parametrize(
        "stored",
        ["bogus:U9", "unified:U9"],
        ids=["unregistered-namespace", "unified-namespace"],
    )
    def test_unified_bucket_validates_stored_namespace(self, stored):
        """A unified DM bucket only accepts a registered non-unified namespace."""
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(stored=stored)
        assert orch._channel_reply_link("unified:kirocrew:direct:U9") is None

    def test_origin_link_wins_and_needs_no_dm_resolution(self):
        orch = _make_orchestrator()
        origin = gw.ChannelLink("discord", channel_id="C77", thread_id="T1")
        orch.sessions = _channel_sessions(origin=origin, stored="discord:U9")
        assert orch._channel_reply_link("discord:kirocrew:direct:U9") == (origin, False)


class TestDeliverChannelReply:
    """``_deliver_channel_reply``'s early refusals."""

    @pytest.mark.asyncio
    async def test_blank_text_is_not_delivered(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        assert await orch._deliver_channel_reply("discord:kirocrew:direct:U9", "   ") is False

    @pytest.mark.asyncio
    async def test_no_dashboard_state_is_not_delivered(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        assert await orch._deliver_channel_reply("discord:kirocrew:direct:U9", "hi") is False

    @pytest.mark.asyncio
    async def test_unresolvable_key_is_not_delivered(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch.sessions = _channel_sessions(stored="")
        assert await orch._deliver_channel_reply("discord:kirocrew:direct:U9", "hi") is False

    @pytest.mark.asyncio
    async def test_target_resolution_failure_degrades_to_false(self):
        """A raising governance ladder degrades the caller, it never propagates."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        link = gw.ChannelLink("discord", channel_id="C77")

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("profile dir unreadable")

        with patch.object(gw, "_resolve_channel_target", _boom):
            delivered = await orch._deliver_channel_reply(
                "discord:kirocrew:direct:U9", "hi", resolved_link=(link, False)
            )
        assert delivered is False

    @pytest.mark.asyncio
    async def test_no_governed_target_is_not_delivered(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        link = gw.ChannelLink("discord", channel_id="C77")
        with patch.object(gw, "_resolve_channel_target", MagicMock(return_value=None)):
            delivered = await orch._deliver_channel_reply(
                "discord:kirocrew:direct:U9", "hi", resolved_link=(link, False)
            )
        assert delivered is False

    # A sub-agent result whose code block spans the channel's message cap: the
    # shape of a diff or log dump, and the one a fixed-width slice mangles. The
    # lines inside carry prose markup a dialect converter WOULD rewrite if the
    # part they land in has lost the fence opener.
    _FENCED = (
        "Here is the diff:\n\n"
        "```diff\n"
        "**never bold** inside a code block\n"
        "# never a heading inside a code block\n"
        "- never a bullet inside a code block\n"
        "```\n"
    )

    @pytest.mark.asyncio
    async def test_a_long_fenced_result_keeps_every_code_line_inside_its_fence(self):
        """The pre-split IS final: each part arrives already under the cap.

        So the channel's own fence-safe splitter is a no-op on it, and a blind
        fixed-width cut through the block leaves part two with no opener: every
        line in it then takes the prose branch of the dialect converter and the
        markup INSIDE the code is rewritten.
        """
        from kiro_crew.messaging.split import FENCE_OUTSIDE, iter_fence_lines

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        link = gw.ChannelLink("discord", channel_id="C77")
        transport = SimpleNamespace(
            capabilities=SimpleNamespace(max_message_chars=90),
            send_message=AsyncMock(return_value="mid"),
        )
        resolved = SimpleNamespace(channel_id="C77", thread_id=None)
        with patch.object(
            gw, "_resolve_channel_target", MagicMock(return_value=(resolved, transport))
        ):
            delivered = await orch._deliver_channel_reply(
                "discord:kirocrew:direct:U9", self._FENCED, resolved_link=(link, False)
            )

        assert delivered is True
        parts = [c.args[1] for c in transport.send_message.await_args_list]
        assert len(parts) > 1, "precondition: the result must actually have been split"
        markers = [
            "**never bold** inside a code block",
            "# never a heading inside a code block",
            "- never a bullet inside a code block",
        ]
        for part in parts:
            prose = [ln for ln, role in iter_fence_lines(part) if role == FENCE_OUTSIDE]
            for marker in markers:
                assert marker not in prose, f"a code line landed outside its fence:\n{part}"
        joined = "\n".join(parts)
        for marker in markers:
            assert marker in joined, "content must survive the split"


# ═══════════════════════════════════════════════════════════════════════════
# Cron delivery to a non-Slack channel
# ═══════════════════════════════════════════════════════════════════════════


_TG_KEY = "telegram:kirocrew:direct:U9"


def _channel_transport() -> MagicMock:
    """A MessagingTransport double whose only job is to record outbound sends."""
    transport = MagicMock()
    # ``max_message_bytes=0`` explicitly: a bare MagicMock attribute is a child
    # OBJECT, not a number, so ``chunk_for_transport`` cannot compare it and the
    # send never happens. 0 = not byte-capped, which is the character path these
    # tests are about (Webex is the byte-capped one). Same reason the fakes in
    # test_cross_surface_mirror.py and test_chat_runner_coverage_output.py declare it.
    transport.capabilities = MagicMock(max_message_chars=4096, max_message_bytes=0)
    transport.send_message = AsyncMock(return_value="m1")
    transport.resolve_configured_target = AsyncMock(return_value=None)
    return transport


class TestCronChannelDelivery:
    """A cron created from a non-Slack channel reports back to THAT channel.

    The delivery fan-out and the duplicate-suppression anchor are one mechanism:
    the anchor is what the suppression read compares against, so a surface that
    delivers without advancing it can never suppress. These drive the real
    ``_deliver_channel_reply`` leg (only the governed target resolution and the
    transport are doubled) so the argument order and the Slack-skip are both
    observed, not asserted about a mock of the method under test.
    """

    @staticmethod
    def _slack_double() -> MagicMock:
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D1")
        slack.post_blocks = AsyncMock(return_value="ts1")
        slack.post_message = AsyncMock()
        return slack

    def _orch(self, transport: MagicMock) -> Any:
        orch = _make_orchestrator()
        orch.sessions = _message_arm_sessions()
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = self._slack_double()
        self._target = MagicMock(
            return_value=(gw.ChannelLink("telegram", channel_id="C77"), transport)
        )
        return orch

    @pytest.mark.asyncio
    async def test_channel_key_delivers_to_channel_and_skips_slack(self):
        """A ``telegram:`` originating key routes the result to Telegram, not Slack."""
        transport = _channel_transport()
        orch = self._orch(transport)
        job = _job(id="jc1", name="channel probe", session_key=_TG_KEY)

        with patch.object(gw, "_resolve_channel_target", self._target):
            async with _cron_message_cb(orch, result_text="all clear") as callback:
                assert await callback(job) == "all clear"

        transport.send_message.assert_awaited_once()
        sent = transport.send_message.await_args.args[1]
        assert "⏰ Cron: channel probe" in sent and "all clear" in sent
        orch.slack.post_blocks.assert_not_awaited()
        orch.slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_channel_delivery_advances_the_dedup_anchor(self):
        """Regression: the anchor is delivery-agnostic, so a channel post moves it.

        ``slack`` is None here on purpose. With a Slack client attached the Slack
        branch is a second writer of the same three fields, so it would keep this
        green with the channel-path write deleted — the very defect being pinned.
        """
        transport = _channel_transport()
        orch = self._orch(transport)
        orch.slack = None
        job = _job(id="jc2", name="anchor probe", session_key=_TG_KEY)
        assert job.last_posted_hash == ""

        with patch.object(gw, "_resolve_channel_target", self._target):
            async with _cron_message_cb(orch, result_text="unchanged") as callback:
                await callback(job)

        transport.send_message.assert_awaited_once()
        assert job.last_posted_hash == gw._result_hash("unchanged")
        assert job.consecutive_dupes == 0
        assert job.last_posted_at > 0

    @pytest.mark.asyncio
    async def test_identical_second_run_is_suppressed_on_the_channel(self):
        """An identical second run is suppressed: run two, deliver one.

        With the anchor left unadvanced on this path, ``last_posted_hash`` stayed
        ``""`` forever and every tick re-posted the same text — while Slack posted
        once and then went quiet for a day.
        """
        transport = _channel_transport()
        orch = self._orch(transport)
        job = _job(id="jc3", name="dupe probe", session_key=_TG_KEY)

        with patch.object(gw, "_resolve_channel_target", self._target):
            async with _cron_message_cb(orch, result_text="same every time") as callback:
                await callback(job)
                await callback(job)

        assert transport.send_message.await_count == 1
        assert job.consecutive_dupes == 1

    @pytest.mark.asyncio
    async def test_job_without_session_key_still_takes_the_slack_path(self):
        """No originating conversation ⇒ the Slack branch runs exactly as before."""
        transport = _channel_transport()
        orch = self._orch(transport)
        job = _job(id="jc4", name="slack probe", session_key="", created_by="U_OWNER")

        with patch.object(gw, "_resolve_channel_target", self._target):
            async with _cron_message_cb(orch, result_text="slack bound") as callback:
                assert await callback(job) == "slack bound"

        transport.send_message.assert_not_awaited()
        orch.slack.post_blocks.assert_awaited_once()
        # The Slack branch owns the same anchor write, through the same helper.
        assert job.last_posted_hash == gw._result_hash("slack bound")


# ═══════════════════════════════════════════════════════════════════════════
# Cron FAILURE delivery to a non-Slack channel
# ═══════════════════════════════════════════════════════════════════════════


class TestCronFailureChannelDelivery:
    """A cron's failure alert reports where its RESULTS report.

    The success leg already routes into the originating conversation, so a job
    that reported into Telegram while it worked and only into the dashboard bell
    once it started crashing reads as idle rather than broken — the exact state
    the alert exists to prevent, and worse than a uniform gap because the success
    path trained the expectation. Both failure surfaces are covered: the
    ``message`` arm's own ``except`` branch, and ``_alert_cron_failure``, which
    the deterministic script/command arms reach instead.

    These drive the real ``_deliver_channel_reply`` leg (only the governed target
    resolution and the transport are doubled), so the Slack-skip is observed
    rather than asserted about a mock of the method under test.
    """

    @staticmethod
    def _slack_double() -> MagicMock:
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D1")
        slack.post_blocks = AsyncMock(return_value="ts1")
        slack.post_message = AsyncMock()
        return slack

    def _orch(self, transport: MagicMock) -> Any:
        orch = _make_orchestrator()
        orch.sessions = _message_arm_sessions()
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = self._slack_double()
        self._target = MagicMock(
            return_value=(gw.ChannelLink("telegram", channel_id="C77"), transport)
        )
        return orch

    @staticmethod
    def _sent(transport: MagicMock) -> str:
        return str(transport.send_message.await_args.args[1])

    # ── the message (LLM) arm's own except branch ──

    @pytest.mark.asyncio
    async def test_a_crashing_message_cron_reports_into_the_channel(self):
        transport = _channel_transport()
        orch = self._orch(transport)
        job = _job(id="jf1", name="failing probe", session_key=_TG_KEY)

        with patch.object(gw, "_resolve_channel_target", self._target):
            async with _cron_message_cb(orch, error=RuntimeError("probe exploded")) as callback:
                with pytest.raises(RuntimeError):
                    await callback(job)

        transport.send_message.assert_awaited_once()
        sent = self._sent(transport)
        assert "Cron: failing probe" in sent and "probe exploded" in sent
        # The reason travels in the channel's own dialect, not Slack's: a mrkdwn
        # fence would reach a Telegram reader as three literal backticks.
        assert "```" not in sent
        orch.slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_silent_message_cron_reports_to_neither_surface(self):
        """``job.silent`` gates the channel leg exactly as it gates the Slack DM."""
        transport = _channel_transport()
        orch = self._orch(transport)
        job = _job(id="jf2", name="quiet probe", session_key=_TG_KEY, silent=True)

        with patch.object(gw, "_resolve_channel_target", self._target):
            async with _cron_message_cb(orch, error=RuntimeError("probe exploded")) as callback:
                with pytest.raises(RuntimeError):
                    await callback(job)

        transport.send_message.assert_not_awaited()
        orch.slack.post_message.assert_not_awaited()
        # Still counted toward auto-pause — silence suppresses surfaces, not
        # bookkeeping.
        assert job.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_a_message_cron_without_a_channel_still_dms_slack(self):
        """No originating conversation ⇒ the Slack DM runs exactly as before."""
        transport = _channel_transport()
        orch = self._orch(transport)
        job = _job(id="jf3", name="slack probe", session_key="", created_by="U_OWNER")

        with patch.object(gw, "_resolve_channel_target", self._target):
            async with _cron_message_cb(orch, error=RuntimeError("probe exploded")) as callback:
                with pytest.raises(RuntimeError):
                    await callback(job)

        transport.send_message.assert_not_awaited()
        channel, text = orch.slack.post_message.await_args.args
        assert channel == "D1"
        assert "*Cron: slack probe*" in text and "probe exploded" in text

    # ── _alert_cron_failure: the script / command arms' failure surface ──

    @pytest.mark.asyncio
    async def test_a_failing_command_cron_reports_into_the_channel(self):
        transport = _channel_transport()
        orch = self._orch(transport)
        job = _job(id="jf4", name="cmd probe", command="false", session_key=_TG_KEY)

        with patch.object(gw, "_resolve_channel_target", self._target):
            async with _cron_cb(orch, command_result={"status": "error", "output": ""}) as cb:
                assert await cb(job) is None

        transport.send_message.assert_awaited_once()
        sent = self._sent(transport)
        assert "Cron: cmd probe" in sent and "Run failed" in sent
        assert "```" not in sent
        orch.slack.post_message.assert_not_awaited()
        # Delivery confirmed on a surface ⇒ the failure dedup anchor advances, so
        # the next identical failure is suppressed instead of re-alerting.
        assert job.last_failure_hash != ""

    @pytest.mark.asyncio
    async def test_a_failing_command_cron_without_a_channel_still_dms_slack(self):
        transport = _channel_transport()
        orch = self._orch(transport)
        job = _job(
            id="jf5", name="cmd probe", command="false", session_key="", created_by="U_OWNER"
        )

        with patch.object(gw, "_resolve_channel_target", self._target):
            async with _cron_cb(orch, command_result={"status": "error", "output": ""}) as cb:
                assert await cb(job) is None

        transport.send_message.assert_not_awaited()
        channel, text = orch.slack.post_message.await_args.args
        assert channel == "D1"
        assert "*Cron: cmd probe*" in text


# ═══════════════════════════════════════════════════════════════════════════
# Task-runner approval notices: channel first, owner DM as the fallback
# ═══════════════════════════════════════════════════════════════════════════


class TestTaskNotifyChannelRouting:
    """``_task_notify`` prefers the run's originating conversation.

    An approval request is the one notification a task cannot proceed without,
    so where it lands decides whether the run stalls silently. The owner DM is
    Slack-only, which left a Telegram-only operator with a stalled task and no
    notice; the channel ladder runs first and the DM stays as the fallback.
    """

    def _notify(self, orch: Any) -> Any:
        orch.sessions = MagicMock()
        orch.ctx_builder = MagicMock()
        orch.conv_log = MagicMock()
        orch.consolidator = MagicMock()
        with patch.object(gw, "TaskRunner", MagicMock(return_value=MagicMock())) as mock_tr:
            orch._init_task_runner()
            return mock_tr.call_args.kwargs["on_notify"]

    @staticmethod
    def _slack_double() -> MagicMock:
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D1")
        slack.post_message = AsyncMock()
        return slack

    @pytest.mark.asyncio
    async def test_channel_wins_over_the_owner_dm(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.slack = self._slack_double()
        orch._owner_id = "U_OWNER"
        deliver = AsyncMock(return_value=True)
        notify = self._notify(orch)

        with patch.object(orch, "_deliver_channel_reply", deliver):
            await notify("Task 3 requires approval", "run the deploy?", "t-3", session_key=_TG_KEY)

        deliver.assert_awaited_once_with(_TG_KEY, "*Task 3 requires approval*\nrun the deploy?")
        orch.slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_owner_dm_is_the_fallback_when_the_channel_refuses(self):
        """A refused channel (no link, governance deny) must not lose the notice."""
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.slack = self._slack_double()
        orch._owner_id = "U_OWNER"
        notify = self._notify(orch)

        with patch.object(orch, "_deliver_channel_reply", AsyncMock(return_value=False)):
            await notify("Task 3 denied", "policy says no", "t-3", session_key=_TG_KEY)

        channel, text = orch.slack.post_message.await_args.args
        assert (channel, text) == ("D1", "*Task 3 denied*\npolicy says no")

    @pytest.mark.asyncio
    async def test_no_session_key_never_touches_the_channel_ladder(self):
        """A dashboard/CLI start has no originating conversation to route to."""
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.slack = self._slack_double()
        orch._owner_id = "U_OWNER"
        deliver = AsyncMock(return_value=True)
        notify = self._notify(orch)

        with patch.object(orch, "_deliver_channel_reply", deliver):
            await notify("Task 3 requires approval", "run the deploy?", "t-3")

        deliver.assert_not_awaited()
        orch.slack.post_message.assert_awaited_once_with(
            "D1", "*Task 3 requires approval*\nrun the deploy?"
        )

    @pytest.mark.asyncio
    async def test_ordinary_progress_title_notifies_neither(self):
        """The title gate is unchanged: only approval/denial escalates off-dashboard."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.slack = self._slack_double()
        orch._owner_id = "U_OWNER"
        deliver = AsyncMock(return_value=True)
        notify = self._notify(orch)

        with patch.object(orch, "_deliver_channel_reply", deliver):
            await notify("Task 3 complete", "all good", "t-3", session_key=_TG_KEY)

        assert ds.notify.call_args.args == ("taskrunner", "Task 3 complete", "all good")
        deliver.assert_not_awaited()
        orch.slack.post_message.assert_not_awaited()


class TestCronDeliversEmbeddedImages:
    """A cron reply that embeds a local image (![alt](/abs/path.png)) must have
    that image uploaded natively to Slack, not posted as literal Markdown text.

    Regression: the cron delivery path posted text only; the local-image
    extraction+upload lived solely in the chat renderer's on_done seal, so a
    cron reply's embedded PNG never reached the user.
    """

    @staticmethod
    def _slack_double() -> MagicMock:
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D1")
        slack.post_blocks = AsyncMock(return_value="parent-ts")
        slack.post_message = AsyncMock()
        slack.upload_file = AsyncMock()
        return slack

    def _orch_with_cwd(self, cwd: str) -> Any:
        orch = _make_orchestrator()
        sessions = _message_arm_sessions()
        client = MagicMock()
        client.cwd = cwd  # the extraction within_root
        sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        orch.sessions = sessions
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = self._slack_double()
        return orch

    @pytest.mark.asyncio
    async def test_embedded_local_png_is_uploaded_natively(self, tmp_path):
        # A real PNG inside the session cwd (the extraction within_root).
        img = tmp_path / "mon.png"
        # Minimal valid PNG (8-byte signature + IHDR is enough for the raster sniff).
        img.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        orch = self._orch_with_cwd(str(tmp_path))
        reply = f"Máy in RUNNING bình thường.\n\n![Máy in A1]({img})"
        # session_key="" → skip the channel leg, take the Slack branch directly.
        job = _job(
            id="jimg1",
            name="Chup may in",
            session_key="",
            created_by="U_OWNER",
            thread_ts="1789607951.617559",
        )

        with patch.object(gw, "_resolve_channel_target", MagicMock(return_value=None)):
            async with _cron_message_cb(orch, result_text=reply) as callback:
                await callback(job)

        # The image was uploaded natively into the requesting thread.
        orch.slack.upload_file.assert_awaited_once()
        up_args = orch.slack.upload_file.await_args.args
        # upload_file(channel, thread_ts, file, filename, title)
        assert up_args[1] == "1789607951.617559", "image not uploaded into the job's thread"
        # The literal local path Markdown was stripped from the posted text.
        posted = orch.slack.post_blocks.await_args.args[2]
        assert str(img) not in posted, "raw local image path leaked into the posted text"

    @pytest.mark.asyncio
    async def test_reply_without_images_uploads_nothing(self, tmp_path):
        orch = self._orch_with_cwd(str(tmp_path))
        job = _job(id="jimg2", name="Chup may in", session_key="", created_by="U_OWNER")

        with patch.object(gw, "_resolve_channel_target", MagicMock(return_value=None)):
            async with _cron_message_cb(orch, result_text="Máy in bình thường, không ảnh.") as cb:
                await cb(job)

        orch.slack.upload_file.assert_not_awaited()
        orch.slack.post_blocks.assert_awaited_once()
        assert "Máy in bình thường, không ảnh." in orch.slack.post_blocks.await_args.args[2]

    @pytest.mark.asyncio
    async def test_posts_into_job_thread_ts(self, tmp_path):
        """The cron result posts into the job's thread_ts (the requesting thread),
        not a new top-level channel message."""
        orch = self._orch_with_cwd(str(tmp_path))
        job = _job(
            id="jimg3",
            name="Chup may in",
            session_key="",
            created_by="U_OWNER",
            thread_ts="1789607951.617559",
        )

        with patch.object(gw, "_resolve_channel_target", MagicMock(return_value=None)):
            async with _cron_message_cb(orch, result_text="RUNNING 40%") as cb:
                await cb(job)

        # post_blocks(channel, blocks, fallback, thread_ts) — 4th arg is the thread.
        assert orch.slack.post_blocks.await_args.args[3] == "1789607951.617559"

    @staticmethod
    def _png_bytes() -> bytes:
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )

    @pytest.mark.asyncio
    async def test_return_value_keeps_the_image_ref_slack_body_is_stripped(self, tmp_path):
        """The callback's RETURN value — the job's stored result, which feeds the
        next run's "[Previous run result]" prompt — must keep the original image
        reference. Only the Slack-delivered body is stripped of the local path.
        """
        img = tmp_path / "mon.png"
        img.write_bytes(self._png_bytes())
        orch = self._orch_with_cwd(str(tmp_path))
        reply = f"RUNNING\n\n![chart]({img})"
        job = _job(id="jimg6", name="Chup may in", session_key="", created_by="U_OWNER")

        with patch.object(gw, "_resolve_channel_target", MagicMock(return_value=None)):
            async with _cron_message_cb(orch, result_text=reply) as cb:
                returned = await cb(job)

        # The stored result keeps the original markup verbatim.
        assert returned == reply, "the return value must not be rewritten with Slack-only text"
        # The Slack body had the local path stripped (image uploaded natively).
        posted = orch.slack.post_blocks.await_args.args[2]
        assert str(img) not in posted, "the Slack body should strip the local path"
        orch.slack.upload_file.assert_awaited()

    @pytest.mark.asyncio
    async def test_refused_upload_posts_a_note_into_the_thread(self, tmp_path):
        """A Slack upload Slack refuses is reported as a note in the job's thread.

        The image markup is already stripped from the posted text, so a silent
        upload failure would be the same disappearing-artifact bug this fixes.
        The shared upload-reporting helper must post a ⚠️ note instead.
        """
        img = tmp_path / "mon.png"
        img.write_bytes(self._png_bytes())
        orch = self._orch_with_cwd(str(tmp_path))
        # Slack refuses the upload.
        orch.slack.upload_file = AsyncMock(side_effect=RuntimeError("slack said no"))
        reply = f"RUNNING\n\n![chart]({img})"
        job = _job(
            id="jimg4",
            name="Chup may in",
            session_key="",
            created_by="U_OWNER",
            thread_ts="1789607951.617559",
        )

        with patch.object(gw, "_resolve_channel_target", MagicMock(return_value=None)):
            async with _cron_message_cb(orch, result_text=reply) as cb:
                await cb(job)

        # The failure is reported as a thread note (post_message), not swallowed.
        orch.slack.post_message.assert_awaited()
        note_call = orch.slack.post_message.await_args
        # channel unset + _resolve_channel_target None → the owner DM, open_dm="D1".
        assert note_call.args[0] == "D1", "note not posted to the resolved DM channel"
        assert note_call.args[2] == "1789607951.617559", "note not in the job's thread"
        assert "⚠️" in note_call.args[1], "the posted note is not a rejection notice"

    @pytest.mark.asyncio
    async def test_extraction_writes_sel_audit_for_admitted_and_refused(self, tmp_path):
        """File egress and refusals both leave a SEL audit record on the cron leg,
        matching the chat renderer (the GPT/Opus security-class finding)."""
        inside = tmp_path / "ok.png"
        inside.write_bytes(self._png_bytes())
        outside = Path("/outside/secret.png")  # outside client.cwd → rejected
        orch = self._orch_with_cwd(str(tmp_path))
        reply = f"RUNNING\n\n![a]({inside})\n\n![b]({outside})"
        job = _job(id="jimg5", name="Chup may in", session_key="", created_by="U_OWNER")

        sel_double = MagicMock()
        with (
            patch.object(gw, "_resolve_channel_target", MagicMock(return_value=None)),
            patch("kiro_crew.slack.files.sel", lambda: sel_double),
        ):
            async with _cron_message_cb(orch, result_text=reply) as cb:
                await cb(job)

        outcomes = {
            c.kwargs.get("outcome")
            for c in sel_double.log_api_access.call_args_list
            if c.kwargs.get("operation") == "slack_renderer.upload_files"
        }
        assert "allowed" in outcomes, "no SEL audit for the admitted file egress"
        assert "denied" in outcomes, "no SEL audit for the refused out-of-workspace ref"
