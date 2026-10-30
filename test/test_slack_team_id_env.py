"""KIROCREW_SLACK_TEAM_ID: the Slack workspace of the conversation an agent serves.

The spawn sets it beside KIROCREW_CHANNEL_ID when the workspace is known and
removes it otherwise, on both the kiro and the Claude backend, and the mirrors'
control-plane element carries the same pair. The value comes only from the Slack
client's channel -> home-workspace store (conversations.info), never from an
inbound event's author-side ``team`` and never from the bot's own auth.test
workspace.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture
from acp_client_helpers import _stop_stderr_drain
from acp_launch_capture import _windows_cleanup_passthrough

from kiro_crew.acp import client as client_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.slack import enterprise, team_ids
from kiro_crew.slack.client import RealSlackClient
from kiro_crew.slack.team_ids import SLACK_TEAM_ID_ENV

CHANNEL = "C0ABC123"
TEAM = "T0HOME42"


def _known(**channels: str) -> dict[str, str]:
    """Register a channel -> home-workspace store, as RealSlackClient does when built."""
    store = dict(channels)
    team_ids.use_channel_team_store(store)
    return store


@pytest.fixture(autouse=True)
def _clean_team_registry(_floor_monkeypatch: pytest.MonkeyPatch):
    # The store binding is process-global gateway state; every test starts unbound and a
    # stale value on the PARENT env must never leak into the child either way. Patched
    # through the floor monkeypatch so a test's own monkeypatch.undo() cannot lift it.
    team_ids._reset_for_tests()
    _floor_monkeypatch.delenv(SLACK_TEAM_ID_ENV, raising=False)
    for name in (
        "_validated_team_id",
        "_validated_enterprise_id",
        "_allowed_team_ids",
        "_allowlist_configured",
        "_configured_ids",
        "_validated_self_bot_id",
        "_validated_self_user_id",
    ):
        if hasattr(enterprise, name):
            _floor_monkeypatch.setattr(enterprise, name, getattr(enterprise, name))
    yield
    team_ids._reset_for_tests()


async def _spawn_env(client: AcpClient, *, claude: bool = False) -> dict[str, str]:
    """Spawn *client* against a mocked process and return the child env."""
    if claude:
        bin_patch = patch(
            "kiro_crew.acp.client._resolve_claude_acp_bin",
            return_value=(["/usr/bin/node", "/x/acp.js"], ""),
        )
        argv = ["/usr/bin/node", "/x/acp.js"]
    else:
        bin_patch = patch(
            "kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"
        )
        argv = ["/usr/bin/kiro-cli", "acp"]
    with (
        bin_patch,
        patch(
            "kiro_crew.acp.client._resolve_claude_code_executable",
            return_value="/usr/local/bin/claude",
        ),
        patch("kiro_crew.acp.client.wrap_argv", return_value=(argv, None)),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        # On Windows the launch spawns through the cleanup admission, which hands the
        # factory an owner and creates a real child instead of reaching the patched
        # coroutine. Run the factory the way its POSIX branch does, and skip the
        # suspended-child resume, which would act on the mocked pid as a real one.
        patch.object(
            client_mod.platform_compat,
            "create_windows_cleanup_owned_process",
            side_effect=_windows_cleanup_passthrough,
        ),
        patch("kiro_crew.acp.client.finish_suspended_spawn", return_value=True),
        patch("kiro_crew.session._track_pid"),
        patch("kiro_crew.session._track_session_pid"),
    ):
        mock_proc = MagicMock()
        # nosemgrep: kirocrew.mock-pid-allocatable -- mocked process, never signalled
        mock_proc.pid = 12345
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc
        try:
            await client._spawn()
            call = mock_exec.call_args
            env = call.kwargs.get("env") or call[1].get("env")
        finally:
            await _stop_stderr_drain(client)
    assert env is not None
    return env


# ---------------------------------------------------------------- spawn env


class TestSpawnEnv:
    @pytest.mark.asyncio
    async def test_set_beside_channel_when_home_team_known(self, tmp_path):
        _known(**{CHANNEL: TEAM})
        env = await _spawn_env(AcpClient(work_dir=tmp_path, session_key="k", channel_id=CHANNEL))
        assert env["KIROCREW_CHANNEL_ID"] == CHANNEL
        assert env[SLACK_TEAM_ID_ENV] == TEAM

    @pytest.mark.asyncio
    async def test_set_on_the_claude_backend_too(self, tmp_path):
        _known(**{CHANNEL: TEAM})
        client = AcpClient(
            work_dir=tmp_path, session_key="k", channel_id=CHANNEL, acp_backend=ACP_BACKEND_CLAUDE
        )
        env = await _spawn_env(client, claude=True)
        assert env["KIROCREW_CHANNEL_ID"] == CHANNEL
        assert env[SLACK_TEAM_ID_ENV] == TEAM

    @pytest.mark.asyncio
    async def test_absent_without_a_channel_even_if_the_parent_carries_one(self, tmp_path):
        _known(**{CHANNEL: TEAM})
        with patch.dict(os.environ, {SLACK_TEAM_ID_ENV: "T0STALE11"}):
            env = await _spawn_env(AcpClient(work_dir=tmp_path, session_key="k", channel_id=None))
        assert "KIROCREW_CHANNEL_ID" not in env
        assert SLACK_TEAM_ID_ENV not in env

    @pytest.mark.asyncio
    async def test_absent_when_the_workspace_is_unknown(self, tmp_path):
        _known(C0OTHER01="T0OTHER02")
        with patch.dict(os.environ, {SLACK_TEAM_ID_ENV: "T0STALE11"}):
            env = await _spawn_env(
                AcpClient(work_dir=tmp_path, session_key="k", channel_id=CHANNEL)
            )
        assert env["KIROCREW_CHANNEL_ID"] == CHANNEL
        assert SLACK_TEAM_ID_ENV not in env

    @pytest.mark.asyncio
    async def test_absent_for_a_non_slack_channel(self, tmp_path):
        # A Discord snowflake is never looked up, even if a store held it.
        _known(**{"123456789012345678": TEAM})
        env = await _spawn_env(
            AcpClient(work_dir=tmp_path, session_key="k", channel_id="123456789012345678")
        )
        assert env["KIROCREW_CHANNEL_ID"] == "123456789012345678"
        assert SLACK_TEAM_ID_ENV not in env

    @pytest.mark.asyncio
    async def test_a_rekeyed_client_respawns_with_the_new_channels_team(self, tmp_path):
        _known(C0OTHER01="T0OTHER02")
        client = AcpClient(work_dir=tmp_path, session_key="k", channel_id=CHANNEL)
        client.rekey("k2", "C0OTHER01")
        env = await _spawn_env(client)
        assert env["KIROCREW_CHANNEL_ID"] == "C0OTHER01"
        assert env[SLACK_TEAM_ID_ENV] == "T0OTHER02"


# ---------------------------------------------------------------- lookup


class TestLookup:
    def test_unbound_or_malformed_answers_nothing(self):
        assert team_ids.slack_team_id_for(CHANNEL) == ""
        _known(**{CHANNEL: "xoxb-not-a-team", "not-a-channel": TEAM})
        assert team_ids.slack_team_id_for(CHANNEL) == ""
        assert team_ids.slack_team_id_for("not-a-channel") == ""
        assert team_ids.slack_team_id_for(None) == ""

    def test_apply_sets_and_pops(self):
        env = {SLACK_TEAM_ID_ENV: "T0STALE11"}
        store = _known()
        team_ids.apply_slack_team_env(env, CHANNEL)
        assert SLACK_TEAM_ID_ENV not in env
        store[CHANNEL] = TEAM
        team_ids.apply_slack_team_env(env, CHANNEL)
        assert env[SLACK_TEAM_ID_ENV] == TEAM


# ---------------------------------------------------------------- writers


class TestWriters:
    def test_the_slack_clients_canonical_home_team_feeds_the_registry(self):
        client = RealSlackClient.__new__(RealSlackClient)
        client.record_channel_team(CHANNEL, TEAM)
        assert team_ids.slack_team_id_for(CHANNEL) == TEAM

    @pytest.mark.asyncio
    async def test_ensure_channel_team_records_context_team_id(self):
        client = RealSlackClient.__new__(RealSlackClient)
        resp = MagicMock()
        resp.data = {"channel": {"id": CHANNEL, "context_team_id": TEAM}}
        client._web = MagicMock()
        client._web.conversations_info = AsyncMock(return_value=resp)
        await client.ensure_channel_team(CHANNEL)
        assert team_ids.slack_team_id_for(CHANNEL) == TEAM

    def test_one_store_the_clients_own_cache(self):
        client = RealSlackClient("xoxb-token")
        client.record_channel_team(CHANNEL, TEAM)
        assert team_ids._store is client._channel_team
        assert team_ids.slack_team_id_for(CHANNEL) == TEAM

    def test_the_bots_own_workspace_is_never_a_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        RealSlackClient("xoxb-token")
        web_mod = types.ModuleType("slack_sdk.web")
        web_mod.WebClient = lambda *a, **k: types.SimpleNamespace(
            auth_test=lambda: {"team_id": "T0BOT999", "enterprise_id": "E0ORG777"}
        )
        with (
            patch.dict(sys.modules, {"slack_sdk.web": web_mod}),
            patch.object(enterprise, "sel", return_value=MagicMock()),
        ):
            assert enterprise.validate_enterprise("xoxb-token") is True
        assert team_ids.slack_team_id_for(CHANNEL) == ""


# ---------------------------------------------------------------- mirrors


class TestControlPlaneElement:
    def test_carries_the_team_beside_the_channel(self):
        from kiro_crew.providers.mirrors.identity import control_plane_identity_env

        _known(**{CHANNEL: TEAM})
        env = control_plane_identity_env("slack:1.2", CHANNEL, label="codex")
        assert env["KIROCREW_CHANNEL_ID"] == CHANNEL
        assert env[SLACK_TEAM_ID_ENV] == TEAM

    def test_omitted_when_unknown_or_without_a_channel(self):
        from kiro_crew.providers.mirrors.identity import control_plane_identity_env

        _known()
        assert SLACK_TEAM_ID_ENV not in control_plane_identity_env("k", CHANNEL, label="codex")
        _known(**{CHANNEL: TEAM})
        assert SLACK_TEAM_ID_ENV not in control_plane_identity_env("k", "", label="codex")
