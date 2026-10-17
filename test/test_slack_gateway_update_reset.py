"""The preconditions checked before the unattended reset of ``_auto_apply_update``.

Covers the refusals the gateway raises before it resets a source checkout: a
repo-declared git driver, local commits or an unknown ahead count, and a branch
that does not track the remote it resets to.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import (
    _make_orchestrator,
    _mock_dashboard_state,
)


class TestAutoApplyUpdatePreconditions:
    """The two refusals guarding the unattended reset, at the gateway level.

    The seam functions have their own unit tests; these assert the gateway
    actually HONOURS them — that it returns before spawning anything, rather than
    computing a refusal and proceeding anyway.
    """

    @pytest.mark.asyncio
    async def test_repo_declared_driver_refuses_before_running_a_driver(self):
        """A repo-named filter/textconv driver would be run BY these git commands.

        `-c` cannot pin an arbitrary driver name, so the run is refused. What must
        be proven is that the refusal lands before the commands that would EXECUTE
        such a driver — `status`, `diff` and `reset`. The branch probe ahead of it
        is a pure ref read that runs no driver, and it carries the neutralizer env
        regardless.
        """
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        argvs = []

        async def _fake_exec(*args, **kwargs):
            argvs.append(args)
            proc = AsyncMock()
            proc.kill = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"main\n", b""))
            proc.wait = AsyncMock(return_value=0)
            return proc

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
            patch(
                "kiro_crew.slack.gateway.repo_exec_config_reason",
                return_value="repository declares filter.evil.smudge",
            ),
            patch("kiro_crew.slack.gateway.is_primary_branch", return_value=True),
            patch("kiro_crew.slack.gateway.tracks_upstream", return_value=True),
        ):
            await orch._auto_apply_update()

        for forbidden in ("status", "diff", "reset", "fetch"):
            assert not any(forbidden in a for a in argvs), (forbidden, argvs)

    @pytest.mark.asyncio
    async def test_local_commits_refuse_before_the_reset(self):
        """A checkout ahead of origin must not be `reset --hard` unattended.

        Revalidated after the fetch, immediately before the reset, because the
        availability verdict was reached in an earlier pass and a checkout is a
        live tree. Asserts no `reset` spawn — a warning that still reset would
        already have destroyed the commits.
        """
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        argvs = []

        async def _fake_exec(*args, **kwargs):
            argvs.append(args)
            proc = AsyncMock()
            proc.kill = MagicMock()
            # `git diff --quiet` must report a difference so the run reaches the
            # revalidation rather than stopping at "already up to date".
            proc.returncode = 1 if "diff" in args else 0
            proc.communicate = AsyncMock(return_value=(b"main\n", b""))
            proc.wait = AsyncMock(return_value=proc.returncode)
            return proc

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
            patch(
                "kiro_crew.slack.gateway.repo_exec_config_reason",
                return_value="",
            ),
            patch("kiro_crew.slack.gateway.tracks_upstream", return_value=True),
            patch("kiro_crew.slack.gateway.commits_ahead", return_value=2),
        ):
            await orch._auto_apply_update()

        assert not any("reset" in a for a in argvs), argvs

    @pytest.mark.asyncio
    async def test_unknown_ahead_count_also_refuses(self):
        """`None` means git could not answer, which must not read as zero."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        argvs = []

        async def _fake_exec(*args, **kwargs):
            argvs.append(args)
            proc = AsyncMock()
            proc.kill = MagicMock()
            proc.returncode = 1 if "diff" in args else 0
            proc.communicate = AsyncMock(return_value=(b"main\n", b""))
            proc.wait = AsyncMock(return_value=proc.returncode)
            return proc

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
            patch(
                "kiro_crew.slack.gateway.repo_exec_config_reason",
                return_value="",
            ),
            patch("kiro_crew.slack.gateway.tracks_upstream", return_value=True),
            patch("kiro_crew.slack.gateway.commits_ahead", return_value=None),
        ):
            await orch._auto_apply_update()

        assert not any("reset" in a for a in argvs), argvs

    @pytest.mark.asyncio
    async def test_branch_not_tracking_origin_refuses_before_fetch(self):
        """The check measures `@{u}`; the apply resets `origin/<branch>`.

        When they are different remotes the reset would discard commits, so the
        apply stops. Only the branch probe may have run by then — nothing that
        fetches or writes.
        """
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        argvs = []

        async def _fake_exec(*args, **kwargs):
            argvs.append(args)
            proc = AsyncMock()
            proc.kill = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"main\n", b""))
            proc.wait = AsyncMock(return_value=0)
            return proc

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
            patch(
                "kiro_crew.slack.gateway.repo_exec_config_reason",
                return_value="",
            ),
            patch("kiro_crew.slack.gateway.tracks_upstream", return_value=False),
        ):
            await orch._auto_apply_update()

        assert not any("fetch" in a for a in argvs), argvs
        assert not any("reset" in a for a in argvs), argvs
