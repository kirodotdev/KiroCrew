"""Tests for per-cron approval_mode feature.

Persistence tests live in test_cron.py. This file covers:
- Dataclass field basics
- Gateway policy selection (integration)
- Session storage
- Subagent policy inheritance (integration)
- MCP validation
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.llm_helpers import ToolApprovalPolicy


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """Every test in this module exercises cron field handling, not authorization.

    ``mcp_cron`` refuses a write from a caller it cannot name, so this states the
    precondition these tests always assumed. See the ``named_cron_caller``
    fixture in ``test/conftest.py``.
    """


class TestCronApprovalModeField:
    """CronJob dataclass approval_mode field behavior."""

    def test_default_is_empty(self) -> None:
        job = CronJob(id="t1", name="test", message="hi")
        assert job.approval_mode == ""

    def test_set_auto(self) -> None:
        job = CronJob(id="t2", name="test", message="hi", approval_mode="auto")
        assert job.approval_mode == "auto"


class TestCronApprovalModeGateway:
    """Gateway passes correct args to stream_and_collect based on approval_mode."""

    def _run_cron_callback(self, approval_mode: str) -> dict:
        """Invoke the real _cron_callback closure and capture stream_and_collect kwargs."""
        from kiro_crew.slack.gateway import GatewayOrchestrator

        gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
        gw.sessions = MagicMock()
        gw.sessions.get_pid = MagicMock(return_value=None)
        gw.ctx_builder = MagicMock()
        gw.slack = MagicMock()
        gw.conv_log = None
        gw.dashboard_state = None
        gw._owner_id = "U000"
        gw.subagent_mgr = None
        gw._cron_injecting = {}
        gw._no_crons = False
        gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
        gw.sessions.release = MagicMock()
        gw.sessions.reset = AsyncMock()
        gw.sessions.cancel_current = AsyncMock()
        gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        gw.ctx_builder.hooks = MagicMock()
        gw._interactive_approval = MagicMock(return_value="interactive_cb")

        captured = {}

        async def fake_stream(client, msg, **kwargs):
            captured.update(kwargs)
            return "done"

        job = CronJob(
            id="g1",
            name="test",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=300),
            approval_mode=approval_mode,
        )

        captured_cb = None

        with patch("kiro_crew.slack.gateway.stream_and_collect", fake_stream), patch(
            "kiro_crew.slack.gateway.CronService"
        ) as mock_cron_cls:

            def capture_cron(on_job=None, **kw):
                nonlocal captured_cb
                captured_cb = on_job
                svc = MagicMock()
                svc.start = AsyncMock()
                return svc

            mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

            async def _init_and_run():
                await gw._init_cron()
                assert captured_cb is not None
                await captured_cb(job)

            asyncio.run(_init_and_run())

        return captured

    def test_auto_mode_passes_auto_approve_and_no_callback(self) -> None:
        captured = self._run_cron_callback("auto")
        assert captured["approval_policy"] == ToolApprovalPolicy.AUTO_APPROVE
        assert captured["on_tool_approval"] is None

    def test_empty_mode_passes_hook_based_and_callback(self) -> None:
        captured = self._run_cron_callback("")
        assert captured["approval_policy"] == ToolApprovalPolicy.HOOK_BASED
        assert captured["on_tool_approval"] is not None


class TestCronApprovalModeExtraEnv:
    """Cron dispatch injects KIROCREW_APPROVAL_MODE into the spawned process
    env when approval_mode='auto', so spawn_run (mcp_core.py) can forward it to
    /api/spawn without depending on parent_session resolution back to the
    cron's session key."""

    def _run_cron_callback_capture_get_or_create(
        self, approval_mode: str, job_env: dict | None = None
    ) -> dict:
        """Invoke the real _cron_callback closure and capture get_or_create kwargs."""
        from kiro_crew.slack.gateway import GatewayOrchestrator

        gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
        gw.ctx_builder = MagicMock()
        gw.slack = MagicMock()
        gw.conv_log = None
        gw.dashboard_state = None
        gw._owner_id = "U000"
        gw.subagent_mgr = None
        gw._cron_injecting = {}
        gw._no_crons = False

        captured_kwargs: dict = {}

        async def fake_get_or_create(key, **kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock(), True, False

        gw.sessions = MagicMock()
        gw.sessions.get_pid = MagicMock(return_value=None)
        gw.sessions.get_or_create = fake_get_or_create
        gw.sessions.release = MagicMock()
        gw.sessions.reset = AsyncMock()
        gw.sessions.cancel_current = AsyncMock()
        gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        gw.ctx_builder.hooks = MagicMock()
        gw._interactive_approval = MagicMock(return_value="interactive_cb")

        job = CronJob(
            id="g1",
            name="test",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=300),
            approval_mode=approval_mode,
            env=job_env or {},
        )

        captured_cb = None

        with patch("kiro_crew.slack.gateway.stream_and_collect", AsyncMock(return_value="done")), patch(
            "kiro_crew.slack.gateway.CronService"
        ) as mock_cron_cls:

            def capture_cron(on_job=None, **kw):
                nonlocal captured_cb
                captured_cb = on_job
                svc = MagicMock()
                svc.start = AsyncMock()
                return svc

            mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

            async def _init_and_run():
                await gw._init_cron()
                assert captured_cb is not None
                await captured_cb(job)

            asyncio.run(_init_and_run())

        return captured_kwargs

    def test_auto_mode_injects_approval_mode_env_var(self) -> None:
        captured = self._run_cron_callback_capture_get_or_create("auto")
        assert captured["extra_env"] == {"KIROCREW_APPROVAL_MODE": "auto"}

    def test_empty_mode_does_not_inject_env_var(self) -> None:
        captured = self._run_cron_callback_capture_get_or_create("")
        assert captured["extra_env"] is None

    def test_auto_mode_preserves_existing_job_env(self) -> None:
        captured = self._run_cron_callback_capture_get_or_create(
            "auto", job_env={"SOME_OTHER_VAR": "x"}
        )
        assert captured["extra_env"] == {
            "SOME_OTHER_VAR": "x",
            "KIROCREW_APPROVAL_MODE": "auto",
        }

    def test_empty_mode_preserves_existing_job_env(self) -> None:
        captured = self._run_cron_callback_capture_get_or_create(
            "", job_env={"SOME_OTHER_VAR": "x"}
        )
        assert captured["extra_env"] == {"SOME_OTHER_VAR": "x"}

    def test_interactive_job_cannot_smuggle_auto_via_job_env(self) -> None:
        # Security: KIROCREW_APPROVAL_MODE is a RESERVED control var. An
        # app/user-controlled job.env that sets it must NOT let an interactive
        # cron auto-approve its spawn_run subagents -- it is stripped and only
        # re-injected for a VALIDATED approval_mode == "auto".
        captured = self._run_cron_callback_capture_get_or_create(
            "", job_env={"KIROCREW_APPROVAL_MODE": "auto", "SOME_OTHER_VAR": "x"}
        )
        assert captured["extra_env"] == {"SOME_OTHER_VAR": "x"}

    def test_auto_job_env_does_not_double_inject(self) -> None:
        # An auto job whose job.env already carries the reserved var yields
        # exactly one canonical entry (no duplication, value forced to "auto").
        captured = self._run_cron_callback_capture_get_or_create(
            "auto", job_env={"KIROCREW_APPROVAL_MODE": "auto"}
        )
        assert captured["extra_env"] == {"KIROCREW_APPROVAL_MODE": "auto"}


class TestCronApprovalModeValidation:
    """Validation schema accepts valid values, rejects invalid."""

    def _simulate_tool_call(self, tool_name: str, arguments: dict) -> str:
        from kiro_crew.mcp_cron import _call_tool

        return _call_tool(tool_name, arguments)

    def test_valid_auto(self) -> None:
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc:
            job = MagicMock()
            job.id = "v1"
            job.name = "test"
            job.schedule = MagicMock()
            job.schedule.kind = "every"
            job.schedule.every_secs = 300
            job.schedule.cron_expr = None
            job.schedule.at_ts = None
            job.agent_id = ""
            job.approval_mode = ""
            mock_svc.return_value.add_job.return_value = job
            result = self._simulate_tool_call(
                "cron_add",
                {"name": "test", "message": "go", "every": 300, "approval_mode": "auto"},
            )
        assert "v1" in result
        # #391: approval_mode is folded into add_job's single locked save, not
        # mutated onto the returned job -- assert it was passed INTO add_job.
        assert mock_svc.return_value.add_job.call_args.kwargs["approval_mode"] == "auto"

    def test_valid_empty(self) -> None:
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc:
            job = MagicMock()
            job.id = "v2"
            job.name = "test"
            job.schedule = MagicMock()
            job.schedule.kind = "every"
            job.schedule.every_secs = 300
            job.schedule.cron_expr = None
            job.schedule.at_ts = None
            job.agent_id = ""
            job.approval_mode = ""
            mock_svc.return_value.add_job.return_value = job
            result = self._simulate_tool_call(
                "cron_add",
                {"name": "test", "message": "go", "every": 300, "approval_mode": ""},
            )
        assert "v2" in result

    def test_invalid_value_rejected(self) -> None:
        result = self._simulate_tool_call(
            "cron_add",
            {"name": "test", "message": "go", "every": 300, "approval_mode": "yolo"},
        )
        assert "Error" in result


class TestSessionApprovalPolicy:
    """Session-level approval policy storage and retrieval."""

    def test_session_stores_policy(self) -> None:
        from kiro_crew.session import _Session

        sess = _Session(provider=MagicMock(), approval_policy="auto")
        assert sess.approval_policy == "auto"

    def test_session_default_empty(self) -> None:
        from kiro_crew.session import _Session

        sess = _Session(provider=MagicMock())
        assert sess.approval_policy == ""

    def test_no_parent_session_defaults_empty(self) -> None:
        from kiro_crew.session import SessionManager

        cfg = MagicMock()
        cfg.session.pool_size = 0
        cfg.session.pool_agent = ""
        cfg.session.pool_ttl_secs = 0
        mgr = SessionManager(cfg=cfg)
        assert mgr.get_approval_policy("nonexistent") == ""


class TestSubagentInheritsPolicy:
    """Subagent _run_inner passes parent's approval_policy to get_or_create."""

    def _run_inner_and_capture(self, parent_policy: str, parent_session_key: str = "parent-key") -> dict:
        """Invoke the real _run_inner and capture get_or_create kwargs."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        ctx_builder = MagicMock()

        # Parent session has the given policy
        sessions.get_approval_policy = MagicMock(return_value=parent_policy)
        sessions.get_agent = MagicMock(return_value="")

        captured = {}
        mock_client = MagicMock()

        async def fake_get_or_create(key, agent=None, approval_policy="", **_kwargs):
            captured["approval_policy"] = approval_policy
            return mock_client, True, False

        sessions.get_or_create = fake_get_or_create
        ctx_builder.build_message = MagicMock(return_value=("msg", None))
        ctx_builder.hooks.on_tool_call = MagicMock()

        # Client streams a single COMPLETE event (no tool calls)
        async def fake_stream(msg):
            yield LLMEvent(kind=EVENT_COMPLETE)

        mock_client.stream = fake_stream

        runner = SubagentManager(sessions=sessions, ctx_builder=ctx_builder)
        info = SubagentInfo(id="sub1", task="test", parent_session_key=parent_session_key)

        asyncio.run(runner._run_inner(info, "subagent:sub1"))
        return captured

    def test_auto_policy_flows_to_child_session(self) -> None:
        captured = self._run_inner_and_capture("auto")
        assert captured["approval_policy"] == "auto"

    def test_empty_policy_flows_to_child_session(self) -> None:
        # With default config (approval_mode="auto"), empty parent policy
        # on a parentless subagent falls back to "auto" via config fallback.
        with patch(
            "kiro_crew.subagent.KiroCrewConfig.load",
            return_value=MagicMock(agent=MagicMock(approval_mode="auto")),
        ):
            captured = self._run_inner_and_capture("", parent_session_key="")
        assert captured["approval_policy"] == "auto"

    def _run_inner_with_tool_event(self, parent_policy: str, on_tool_approval=None, parent_session_key: str = "parent-key") -> MagicMock:
        """Invoke _run_inner with a PERMISSION_REQUEST event and return the mock client."""
        from kiro_crew.hooks import TOOL_ALLOW, ToolHookResult
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, LLMEvent
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        ctx_builder = MagicMock()
        sessions.get_approval_policy = MagicMock(return_value=parent_policy)
        sessions.get_agent = MagicMock(return_value="")

        mock_client = MagicMock()
        mock_client.approve_tool = AsyncMock()
        mock_client.reject_tool = AsyncMock()

        async def fake_get_or_create(key, agent=None, approval_policy="", **_kwargs):
            return mock_client, True, False

        sessions.get_or_create = fake_get_or_create
        ctx_builder.build_message = MagicMock(return_value=("msg", None))
        ctx_builder.hooks.on_tool_call = MagicMock(return_value=ToolHookResult(action=TOOL_ALLOW))

        async def fake_stream(msg):
            yield LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="req-1",
                title="some_tool",
                tool_kind="mcp",
            )
            yield LLMEvent(kind=EVENT_COMPLETE)

        mock_client.stream = fake_stream

        runner = SubagentManager(
            sessions=sessions, ctx_builder=ctx_builder, on_tool_approval=on_tool_approval
        )
        info = SubagentInfo(id="sub1", task="test", parent_session_key=parent_session_key)

        with patch("kiro_crew.subagent.sel"):
            asyncio.run(runner._run_inner(info, "subagent:sub1"))
        return mock_client

    def test_auto_policy_approves_tool(self) -> None:
        client = self._run_inner_with_tool_event("auto")
        client.approve_tool.assert_called_once_with("req-1")
        client.reject_tool.assert_not_called()

    def test_deny_by_default_rejects_tool(self) -> None:
        # With interactive approval_mode, empty parent policy stays empty
        # and falls through to deny-by-default.
        with patch(
            "kiro_crew.subagent.KiroCrewConfig.load",
            return_value=MagicMock(agent=MagicMock(approval_mode="interactive")),
        ):
            client = self._run_inner_with_tool_event("", on_tool_approval=None, parent_session_key="")
        client.reject_tool.assert_called_once_with("req-1")
        client.approve_tool.assert_not_called()

    def test_empty_policy_with_parent_stays_empty(self) -> None:
        """When a real parent exists but returns empty policy, config fallback must NOT activate."""
        with patch(
            "kiro_crew.subagent.KiroCrewConfig.load",
            return_value=MagicMock(agent=MagicMock(approval_mode="auto")),
        ):
            captured = self._run_inner_and_capture("", parent_session_key="parent-key")
        assert captured["approval_policy"] == ""

    def test_deny_by_default_with_parent_rejects_tool(self) -> None:
        """With a real parent returning empty policy, tool calls are still denied."""
        with patch(
            "kiro_crew.subagent.KiroCrewConfig.load",
            return_value=MagicMock(agent=MagicMock(approval_mode="auto")),
        ):
            client = self._run_inner_with_tool_event(
                "", on_tool_approval=None, parent_session_key="parent-key"
            )
        client.reject_tool.assert_called_once_with("req-1")
        client.approve_tool.assert_not_called()


class TestCronSubagentInjection:
    """_subagent_done injects results into cron sessions and resets correctly."""

    def _build_gw(self):  # type: ignore[no-untyped-def]
        from kiro_crew.slack.gateway import GatewayOrchestrator

        gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
        gw.sessions = MagicMock()
        gw.sessions.get_pid = MagicMock(return_value=None)
        gw.ctx_builder = MagicMock()
        gw.slack = None
        gw.conv_log = None
        gw.dashboard_state = None
        gw._owner_id = "U000"
        gw._cron_injecting = {}
        gw._cfg = MagicMock()
        gw._cfg.agent.max_subagents = 5
        gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
        gw.sessions.release = MagicMock()
        gw.sessions.reset = AsyncMock()
        gw.sessions.cancel_current = AsyncMock()
        gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        gw.ctx_builder.memory = MagicMock()
        gw._interactive_approval = MagicMock(return_value=AsyncMock(return_value=True))
        return gw

    def _init_and_get_done_cb(self, gw):  # type: ignore[no-untyped-def]
        captured_done = None

        with patch("kiro_crew.slack.gateway.SubagentManager") as mock_cls:

            def capture_mgr(**kwargs):  # type: ignore[no-untyped-def]
                nonlocal captured_done
                captured_done = kwargs["on_done"]
                mgr = MagicMock()
                mgr.running = []
                mgr.queued_count_for = MagicMock(return_value=0)
                return mgr

            mock_cls.side_effect = capture_mgr
            gw._init_subagents()

        assert captured_done is not None
        return captured_done

    def _patches(self, stream_rv="ok"):  # type: ignore[no-untyped-def]
        return (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                AsyncMock(return_value=stream_rv),
            ),
            patch(
                "kiro_crew.slack.gateway.redact_exfiltration_urls",
                return_value=(stream_rv, False),
            ),
            patch(
                "kiro_crew.slack.gateway.redact_credentials",
                return_value=(stream_rv, False),
            ),
        )

    def test_cron_injection_happy_path(self) -> None:
        """Subagent result is injected into cron session and session is reset."""
        from kiro_crew.subagent import SubagentInfo

        gw = self._build_gw()
        done_cb = self._init_and_get_done_cb(gw)

        info = SubagentInfo(
            id="s1",
            task="search emails",
            result="found 3 threads",
            parent_session_key="cron:daily-prep",
        )

        p1, p2, p3 = self._patches("compiled report")
        with p1, p2, p3:
            asyncio.run(done_cb(info))

        gw.sessions.get_or_create.assert_awaited_once_with("cron:daily-prep")
        gw.sessions.release.assert_called_once_with("cron:daily-prep")
        gw.sessions.reset.assert_awaited_once_with("cron:daily-prep")
        assert gw._cron_injecting == {}

    def test_cron_injection_defers_reset_when_others_running(self) -> None:
        """Session is NOT reset when other subagents are still running."""
        from kiro_crew.subagent import SubagentInfo

        gw = self._build_gw()
        done_cb = self._init_and_get_done_cb(gw)

        other = SubagentInfo(id="s2", task="other", parent_session_key="cron:daily-prep")
        gw.subagent_mgr.running = [other]

        info = SubagentInfo(
            id="s1",
            task="search emails",
            result="found stuff",
            parent_session_key="cron:daily-prep",
        )

        p1, p2, p3 = self._patches()
        with p1, p2, p3:
            asyncio.run(done_cb(info))

        gw.sessions.release.assert_called_once_with("cron:daily-prep")
        gw.sessions.reset.assert_not_awaited()

    def test_cron_injection_failure_still_cleans_up(self) -> None:
        """On injection failure, counter is cleaned up and session is reset."""
        from kiro_crew.subagent import SubagentInfo

        gw = self._build_gw()
        done_cb = self._init_and_get_done_cb(gw)

        # stream_and_collect raises after acquire
        info = SubagentInfo(
            id="s1",
            task="search",
            result="",
            error="timeout",
            parent_session_key="cron:daily-prep",
        )

        p2, p3 = (
            patch(
                "kiro_crew.slack.gateway.redact_exfiltration_urls",
                return_value=("", False),
            ),
            patch(
                "kiro_crew.slack.gateway.redact_credentials",
                return_value=("", False),
            ),
        )
        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            AsyncMock(side_effect=RuntimeError("boom")),
        ), p2, p3:
            asyncio.run(done_cb(info))

        assert gw._cron_injecting == {}
        # Session was acquired, so release should be called exactly once
        gw.sessions.release.assert_called_once_with("cron:daily-prep")
        gw.sessions.reset.assert_awaited_once()

    def test_cron_injection_defers_reset_when_others_injecting(self) -> None:
        """Session is NOT reset when another subagent is mid-injection."""
        from kiro_crew.subagent import SubagentInfo

        gw = self._build_gw()
        done_cb = self._init_and_get_done_cb(gw)

        # .running is empty, but another subagent is mid-injection
        gw.subagent_mgr.running = []
        gw.subagent_mgr.queued_count_for = MagicMock(return_value=0)
        gw._cron_injecting["cron:daily-prep"] = 1

        info = SubagentInfo(
            id="s1",
            task="search emails",
            result="found stuff",
            parent_session_key="cron:daily-prep",
        )

        p1, p2, p3 = self._patches()
        with p1, p2, p3:
            asyncio.run(done_cb(info))

        gw.sessions.release.assert_called_once_with("cron:daily-prep")
        gw.sessions.reset.assert_not_awaited()

    def test_cron_injection_get_or_create_failure_no_release(self) -> None:
        """When get_or_create fails, release is NOT called but counter is cleaned up."""
        from kiro_crew.subagent import SubagentInfo

        gw = self._build_gw()
        done_cb = self._init_and_get_done_cb(gw)
        gw.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("no session"))

        info = SubagentInfo(
            id="s1",
            task="search",
            result="r",
            parent_session_key="cron:daily-prep",
        )

        p2, p3 = (
            patch(
                "kiro_crew.slack.gateway.redact_exfiltration_urls",
                return_value=("", False),
            ),
            patch(
                "kiro_crew.slack.gateway.redact_credentials",
                return_value=("", False),
            ),
        )
        with p2, p3:
            asyncio.run(done_cb(info))

        gw.sessions.release.assert_not_called()
        assert gw._cron_injecting == {}
        gw.sessions.reset.assert_awaited_once()


class TestNoCronsFlag:
    """Gateway --no-crons flag skips cron scheduler startup."""

    def _make_gateway(self, *, no_crons: bool = False):
        from kiro_crew.slack.gateway import GatewayOrchestrator

        gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
        gw.sessions = MagicMock()
        gw.ctx_builder = MagicMock()
        gw.slack = MagicMock()
        gw.conv_log = None
        gw.dashboard_state = None
        gw._owner_id = "U000"
        gw._no_crons = no_crons
        gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
        gw.sessions.release = MagicMock()
        gw.sessions.reset = AsyncMock()
        gw.sessions.cancel_current = AsyncMock()
        gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        gw.ctx_builder.hooks = MagicMock()
        gw._interactive_approval = MagicMock(return_value="interactive_cb")
        return gw

    def test_no_crons_skips_start(self) -> None:
        gw = self._make_gateway(no_crons=True)
        with patch("kiro_crew.slack.gateway.CronService") as mock_cls:
            svc = MagicMock()
            svc.start = AsyncMock()
            mock_cls.return_value = svc
            mock_cls.create = AsyncMock(return_value=svc)
            asyncio.run(gw._init_cron())
            svc.start.assert_not_called()
            assert gw.cron_svc is svc  # still instantiated, just not started

    def test_default_starts_crons_after_app_reconcile(self) -> None:
        gw = self._make_gateway(no_crons=False)
        with patch("kiro_crew.slack.gateway.CronService") as mock_cls, patch(
            "kiro_crew.apps.bridges.reconcile_app_crons_for_execution",
            new_callable=AsyncMock,
        ) as reconcile:
            svc = MagicMock()
            svc.start = AsyncMock()
            mock_cls.return_value = svc
            mock_cls.create = AsyncMock(return_value=svc)
            asyncio.run(gw._init_cron())
            reconcile.assert_awaited_once_with(svc)
            svc.start.assert_awaited_once()

    def test_reconcile_failure_leaves_cron_scheduler_stopped(self) -> None:
        gw = self._make_gateway(no_crons=False)
        with patch("kiro_crew.slack.gateway.CronService") as mock_cls, patch(
            "kiro_crew.apps.bridges.reconcile_app_crons_for_execution",
            new_callable=AsyncMock,
            side_effect=OSError("cron store unavailable"),
        ):
            svc = MagicMock()
            svc.start = AsyncMock()
            mock_cls.return_value = svc
            mock_cls.create = AsyncMock(return_value=svc)
            asyncio.run(gw._init_cron())
            svc.start.assert_not_awaited()

    def test_init_stores_no_crons(self) -> None:
        """GatewayOrchestrator.__init__ stores _no_crons attribute."""
        from kiro_crew.slack.gateway import GatewayOrchestrator

        cfg = MagicMock()
        cfg.load_credentials.return_value = {}
        cfg.slack = MagicMock()
        cfg.slack.allowed_users = []
        cfg.slack.tracking_channels = []
        cfg.slack.command = "kirocrew"
        gw = GatewayOrchestrator(cfg, no_crons=True)
        assert gw._no_crons is True

    def test_init_defaults_no_crons_false(self) -> None:
        """GatewayOrchestrator.__init__ defaults _no_crons to False."""
        from kiro_crew.slack.gateway import GatewayOrchestrator

        cfg = MagicMock()
        cfg.load_credentials.return_value = {}
        cfg.slack = MagicMock()
        cfg.slack.allowed_users = []
        cfg.slack.tracking_channels = []
        cfg.slack.command = "kirocrew"
        gw = GatewayOrchestrator(cfg)
        assert gw._no_crons is False

    def test_run_gateway_passes_no_crons(self) -> None:
        """run_gateway forwards no_crons to GatewayOrchestrator."""
        from kiro_crew.slack.gateway import run_gateway

        cfg = MagicMock()
        with (
            # The aggregate-cgroup-ceiling apply shells out to systemctl —
            # a host-service mutation the rootdir guard refuses; stub it.
            patch("kiro_crew.slack.gateway.ensure_agents_slice_limits", return_value=True),
            patch("kiro_crew.slack.gateway.GatewayOrchestrator") as mock_cls,
        ):
            mock_orch = MagicMock()
            mock_orch.run = AsyncMock()
            mock_cls.return_value = mock_orch
            asyncio.run(run_gateway(cfg, no_crons=True))
            mock_cls.assert_called_once_with(
                cfg,
                no_dashboard=False,
                no_crons=True,
                no_open=False,
                port_override=None,
                json_ready=False,
                approval_mode=None,
                test_mode=False,
            )

    def test_cli_gateway_passes_no_crons(self) -> None:
        """CLI _gateway function forwards no_crons to run_gateway."""
        from kiro_crew.cli_server import _gateway

        with patch("kiro_crew.cli_server.config_path") as mock_cp, patch(
            "kiro_crew.cli_server.KiroCrewConfig"
        ) as mock_cfg_cls, patch("kiro_crew.cli_chat._ensure_config_key"), patch(
            "kiro_crew.cli_server.run_gateway", new_callable=AsyncMock
        ) as mock_run:
            mock_cp.return_value.exists.return_value = True
            mock_cfg_cls.load.return_value = MagicMock()
            asyncio.run(_gateway(no_crons=True))
            mock_run.assert_called_once()
            assert mock_run.call_args[1]["no_crons"] is True

    def test_cli_argparse_no_crons_flag(self) -> None:
        """CLI argparse recognizes --no-crons flag."""
        import sys

        with patch.object(sys, "argv", ["kirocrew", "gateway", "--no-crons"]):
            from kiro_crew.cli import main

            with patch("kiro_crew.cli_server._gateway", new_callable=AsyncMock) as mock_gw, patch(
                "kiro_crew.cli.asyncio"
            ) as mock_asyncio:
                mock_asyncio.run = MagicMock()
                main()
                mock_gw.assert_called_once()
                assert mock_gw.call_args[1]["no_crons"] is True


class TestSubagentRoleModelForcesDedicatedPath:
    """A configured per-role sub-agent model/effort must force the dedicated
    (non-shared) session path, because the parent's shared runtime was spawned
    with the parent's model and cannot switch model per session. Otherwise the
    override would silently no-op on the default (session-sharing) path."""

    def _run(self, *, role_models=None, role_efforts=None):
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_approval_policy = MagicMock(return_value="")
        sessions.get_agent = MagicMock(return_value="")
        ctx_builder = MagicMock()
        ctx_builder.build_message = MagicMock(return_value=("msg", None))
        ctx_builder.hooks.auto_approve_subagent_tools = False

        captured: dict = {}
        mock_client = MagicMock()

        async def fake_get_or_create(key, agent=None, approval_policy="", **kwargs):
            captured.update(kwargs)
            return mock_client, True, False

        sessions.get_or_create = fake_get_or_create

        async def fake_stream(msg):
            yield LLMEvent(kind=EVENT_COMPLETE)

        mock_client.stream = fake_stream

        cfg = KiroCrewConfig(
            agent=AgentConfig(role_models=role_models or {}, role_efforts=role_efforts or {})
        )
        runner = SubagentManager(sessions=sessions, ctx_builder=ctx_builder)
        shared = AsyncMock(
            side_effect=AssertionError("shared path taken despite a per-role override")
        )
        info = SubagentInfo(id="sub1", task="test", parent_session_key="parent-key")
        with patch.object(runner, "_create_shared_session", shared), patch.object(
            runner, "_should_use_session_sharing", return_value=True
        ), patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)):
            asyncio.run(runner._run_inner(info, "subagent:sub1"))
        return captured, shared

    def test_pinned_model_forces_dedicated_and_applies(self) -> None:
        captured, shared = self._run(role_models={"subagent": "claude-sonnet-4.6"})
        shared.assert_not_called()
        assert captured.get("model") == "claude-sonnet-4.6"

    def test_pinned_effort_forces_dedicated_and_applies(self) -> None:
        captured, shared = self._run(role_efforts={"subagent": "low"})
        shared.assert_not_called()
        assert captured.get("reasoning_effort_override") == "low"
