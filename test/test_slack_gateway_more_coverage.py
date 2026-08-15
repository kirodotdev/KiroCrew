"""Further coverage for ``kiro_crew.slack.gateway``.

``test_slack_gateway.py``, ``test_slack_gateway_coverage.py`` and
``test_slack_gateway_cron_exec_coverage.py`` already drive the cron executor,
the autonudge fire paths and the happy arms of ``_deliver_result``. The
surfaces exercised here had no test anywhere in the suite before this file:

* ``_warn_if_kiro_cli_outdated`` — every arm: unspawnable binary, probe
  timeout (kill + bounded reap), cancellation, transport error, the
  outdated-version warning and the unparseable-output silence.
* ``_deliver_result`` — the ``slack`` / ``slack:<chan>`` / no-``dashboard_state``
  arms, the post-failure ``except``, and the ``default_deliver`` lookup failure.
* ``_connect_slack``'s reason extraction when the Slack error object's
  ``response`` cannot be read.
* ``_shutdown``'s slot-save failure arm and the three background-task
  cancellations (model download, auto-migration, boot update check).
* ``_init_dashboard`` / ``_init_api_server`` ``--port`` override + the
  ephemeral (``--port auto``) bound-port read-back.
* ``_start_embeddings``' custom-model arm, ``_auto_migrate_memory``'s
  reconcile-first / audit-failure / download-error arms and
  ``_set_memory_migrated``.
* ``_init_crew``'s failure arm, ``_init_mcp_discovery``'s populated arm,
  ``_subagent_coalescer``'s broadcast closures, ``_notify_nudge_expired``'s
  runtime-budget wording, ``_persist_slot_title``'s no-log guard,
  ``_remember_options``' best-effort ``except``, ``_deliver_cron_response``'s
  unresolvable-channel and OPTIONS-post-failure arms, and
  ``_is_heartbeat_safe_tool``'s malformed-``mcp__``-prefix normalisation.

Everything is driven through mocked collaborators: ``asyncio``'s process
spawner, the process-tree killer, the embedding backend, the dashboard/API
server starters and the Slack client are all patched, so no subprocess, no
signal, no socket and no write outside the per-test ``KIROCREW_HOME`` (pinned
by ``test/conftest.py``) happens. Style and patch seams mirror
``test_slack_gateway.py``.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.autonudge import NudgeLoop
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.slack import gateway as gw

# ─── Helpers ─────────────────────────────────────────────────────────────


def _make_orchestrator(**kwargs: Any) -> Any:
    """Build a GatewayOrchestrator with mocked credentials (no Slack tokens).

    Returned as ``Any`` on purpose: every test below swaps real collaborators
    for mocks, which do not satisfy the declared attribute types.
    """
    cfg = KiroCrewConfig()
    creds = {"KIROCREW_OWNER_ID": "U_OWNER"}
    with patch.object(cfg, "load_credentials", return_value=creds):
        return gw.GatewayOrchestrator(
            cfg,
            no_dashboard=kwargs.pop("no_dashboard", True),
            no_crons=kwargs.pop("no_crons", True),
            no_open=True,
        )


def _mock_dashboard_state() -> MagicMock:
    ds = MagicMock()
    ds._slots = {}
    ds.notify = MagicMock()
    ds.push_slots_update = MagicMock()
    ds.push_refresh = MagicMock()
    ds.push_slot_title = MagicMock()
    ds.broadcast_ws = MagicMock()
    ds.broadcast_ws_subagent_subscribers = MagicMock()
    ds.get_slot = MagicMock(return_value=None)
    ds.channel_transports = {}
    return ds


def _mock_slack(*, dm: str | None = "D1") -> MagicMock:
    slack = MagicMock()
    slack.open_dm = AsyncMock(return_value=dm)
    slack.post_message = AsyncMock(return_value="111.1")
    slack.post_blocks = AsyncMock(return_value="222.2")
    return slack


def _slack_default_deliver() -> Any:
    """Pin ``heartbeat.default_deliver`` to "slack" for tagless deliveries."""
    return patch.object(
        gw.KiroCrewConfig,
        "load",
        return_value=SimpleNamespace(heartbeat=SimpleNamespace(default_deliver="slack")),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _is_heartbeat_safe_tool normalisation
# ═══════════════════════════════════════════════════════════════════════════


class TestHeartbeatSafeToolNormalisation:
    """A malformed ``mcp__`` title yields no server-qualified identity."""

    def test_mcp_prefix_without_tool_segment_is_refused(self):
        # "mcp__foo".split("__", 2) is only 2 parts, so neither the qualified
        # form nor the bare-name strip applies: the title stays "mcp__foo",
        # misses HEARTBEAT_SAFE_TOOLS, and (qualified == "") can never match an
        # edition entry -> deny-by-default.
        assert gw._is_heartbeat_safe_tool("mcp__foo") is False

    def test_at_prefix_without_slash_is_refused(self):
        assert gw._is_heartbeat_safe_tool("@server-only") is False


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _warn_if_kiro_cli_outdated
# ═══════════════════════════════════════════════════════════════════════════


def _probe_proc(communicate: Any, *, returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.pid = 424242
    proc.returncode = returncode
    proc.communicate = communicate
    proc.kill = MagicMock()
    return proc


class TestWarnIfKiroCliOutdated:
    """The boot-time kiro-cli version probe never raises and never hangs."""

    @pytest.mark.asyncio
    async def test_unspawnable_binary_is_silent(self, capsys):
        orch = _make_orchestrator()
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("kiro-cli")):
            await orch._warn_if_kiro_cli_outdated()
        assert "outdated" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_probe_timeout_kills_and_reaps(self, caplog):
        calls: list[int] = []

        async def _communicate() -> tuple[bytes, bytes]:
            calls.append(1)
            if len(calls) == 1:
                await asyncio.sleep(0.05)  # outlives the 1ms budget below
            return (b"kiro-cli 9.9.9", b"")

        proc = _probe_proc(_communicate)
        orch = _make_orchestrator()
        orch._KIRO_CLI_VERSION_TIMEOUT_SECS = 0.001
        with caplog.at_level("WARNING"):
            with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
                with patch.object(
                    gw.platform_compat, "kill_process_tree_async", AsyncMock()
                ) as killer:
                    await orch._warn_if_kiro_cli_outdated()
        killer.assert_awaited_once()
        assert killer.await_args.args[0] == 424242
        # The reap re-entered communicate() rather than leaving a zombie.
        assert len(calls) == 2
        assert "timed out" in caplog.text

    @pytest.mark.asyncio
    async def test_cancellation_kills_child_and_propagates(self):
        async def _communicate() -> tuple[bytes, bytes]:
            raise asyncio.CancelledError

        proc = _probe_proc(_communicate)
        orch = _make_orchestrator()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with patch.object(gw.platform_compat, "kill_process_tree_async", AsyncMock()) as killer:
                with pytest.raises(asyncio.CancelledError):
                    await orch._warn_if_kiro_cli_outdated()
        killer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_transport_error_falls_back_to_plain_kill(self):
        async def _communicate() -> tuple[bytes, bytes]:
            raise BrokenPipeError("pipe gone")

        proc = _probe_proc(_communicate)
        orch = _make_orchestrator()
        # Tree kill refused (already-dead child) -> plain proc.kill() fallback.
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with patch.object(
                gw.platform_compat,
                "kill_process_tree_async",
                AsyncMock(side_effect=ProcessLookupError),
            ):
                await orch._warn_if_kiro_cli_outdated()  # must not raise
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_outdated_version_warns(self, capsys):
        async def _communicate() -> tuple[bytes, bytes]:
            return (b"kiro-cli 1.25.0\n", b"")

        proc = _probe_proc(_communicate)
        orch = _make_orchestrator()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            await orch._warn_if_kiro_cli_outdated()
        out = capsys.readouterr().out
        assert "1.25 is outdated" in out

    @pytest.mark.asyncio
    async def test_current_version_is_quiet(self, capsys):
        async def _communicate() -> tuple[bytes, bytes]:
            return (b"kiro-cli 1.26.0\n", b"")

        orch = _make_orchestrator()
        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=_probe_proc(_communicate))
        ):
            await orch._warn_if_kiro_cli_outdated()
        assert "outdated" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_unparseable_output_is_silent(self, capsys):
        async def _communicate() -> tuple[bytes, bytes]:
            return (b"some-other-binary\n", b"")

        orch = _make_orchestrator()
        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=_probe_proc(_communicate))
        ):
            await orch._warn_if_kiro_cli_outdated()
        assert "outdated" not in capsys.readouterr().out


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _deliver_result routing arms
# ═══════════════════════════════════════════════════════════════════════════


class TestDeliverResultRouting:
    """Background-result routing for the Slack and no-dashboard arms."""

    @pytest.mark.asyncio
    async def test_default_deliver_lookup_failure_falls_through(self):
        orch = _make_orchestrator()
        orch.slack = None
        orch.dashboard_state = None
        with patch.object(gw.KiroCrewConfig, "load", side_effect=RuntimeError("no config")):
            await orch._deliver_result("Title", "task", "result", "")  # must not raise

    @pytest.mark.asyncio
    async def test_dashboard_slot_arm_without_dashboard_state(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.slack = _mock_slack()
        await orch._deliver_result("Title", "task", "result", "dashboard:s1")
        # The dashboard arm returns early; nothing leaks to Slack.
        orch.slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_new_slot_arm_without_dashboard_state(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.slack = _mock_slack()
        await orch._deliver_result("Title", "task", "result", "dashboard")
        orch.slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slack_only_arm_posts_dm_without_notifying(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.slack = _mock_slack(dm="D9")
        await orch._deliver_result("Title", "task", "result", "slack")
        orch.slack.open_dm.assert_awaited_once_with("U_OWNER")
        assert orch.slack.post_message.await_count >= 1
        assert orch.slack.post_message.await_args.args[0] == "D9"
        # "slack" is Slack-ONLY: no bell notification.
        ds.notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_slack_only_arm_survives_dm_failure(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.slack = _mock_slack()
        orch.slack.open_dm = AsyncMock(side_effect=RuntimeError("slack down"))
        await orch._deliver_result("Title", "task", "result", "slack")  # must not raise
        orch.slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slack_thread_arm_without_ts_falls_back_to_dm(self):
        # "slack:C1" is only two segments, so there is no thread to reply in:
        # the fallback opens the owner's DM and still rings the bell.
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.slack = _mock_slack(dm="D7")
        await orch._deliver_result("Title", "task", "result", "slack:C1")
        orch.slack.open_dm.assert_awaited_once_with("U_OWNER")
        assert orch.slack.post_message.await_args.args[0] == "D7"
        ds.notify.assert_called_once()
        assert ds.notify.call_args.args[0] == "heartbeat"

    @pytest.mark.asyncio
    async def test_default_arm_notifies_even_when_dm_unavailable(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.slack = _mock_slack(dm=None)
        with _slack_default_deliver():
            await orch._deliver_result("Title", "task", "result", "")
        orch.slack.post_message.assert_not_awaited()
        ds.notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_default_arm_notifies_after_post_failure(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.slack = _mock_slack()
        orch.slack.post_message = AsyncMock(side_effect=RuntimeError("rate limited"))
        with _slack_default_deliver():
            await orch._deliver_result("Title", "task", "result", "")
        # The Slack failure is swallowed; the bell still fires.
        ds.notify.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _connect_slack
# ═══════════════════════════════════════════════════════════════════════════


class TestConnectSlack:
    """Connect failures stay non-fatal and record a short reason."""

    @pytest.mark.asyncio
    async def test_unreadable_error_response_falls_back_to_class_name(self):
        class _Boom(Exception):
            pass

        exc = _Boom("connect refused")
        resp = MagicMock()
        resp.get = MagicMock(side_effect=TypeError("not a mapping"))
        exc.response = resp  # type: ignore[attr-defined]

        orch = _make_orchestrator()
        orch._socket_client = MagicMock()
        orch._socket_client.connect = AsyncMock(side_effect=exc)
        with patch.object(gw, "_channel_transport_permitted", return_value=True):
            assert await orch._connect_slack() is False
        assert orch._slack_connect_error == "_Boom"

    @pytest.mark.asyncio
    async def test_governance_denial_drops_socket_client(self):
        orch = _make_orchestrator()
        orch._socket_client = MagicMock()
        orch._socket_client.connect = AsyncMock()
        with patch.object(gw, "_channel_transport_permitted", return_value=False):
            assert await orch._connect_slack() is False
        assert orch._socket_client is None


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _shutdown
# ═══════════════════════════════════════════════════════════════════════════


class TestShutdownExtras:
    """Teardown arms not reached by ``test_slack_gateway.py``."""

    @pytest.mark.asyncio
    async def test_slot_save_failure_does_not_abort_shutdown(self):
        orch = _make_orchestrator()
        orch.cron_svc = None
        orch.heartbeat_svc = None
        orch.subagent_mgr = None
        orch.sessions = None
        orch._dashboard_runner = None
        ds = _mock_dashboard_state()
        ds._loop_watchdog = None
        ds._loop_heartbeat = None
        orch.dashboard_state = ds
        orch._stop_mcp_broker = AsyncMock()
        with patch(
            "kiro_crew.dashboard.chat.save_all_slots_to_history",
            side_effect=RuntimeError("history lock"),
        ):
            await orch._shutdown()  # must not raise
        # Teardown continued past the failed save.
        ds.file_indexes.stop_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancels_background_tasks_and_closes_socket(self):
        orch = _make_orchestrator()
        orch.cron_svc = None
        orch.heartbeat_svc = None
        orch.subagent_mgr = None
        orch.sessions = None
        orch.dashboard_state = None
        orch._dashboard_runner = None
        orch._stop_mcp_broker = AsyncMock()
        orch._socket_client = MagicMock()
        orch._socket_client.close = AsyncMock()

        async def _forever() -> None:
            await asyncio.sleep(30)

        model = asyncio.create_task(_forever())
        migrate = asyncio.create_task(_forever())
        update = asyncio.create_task(_forever())
        orch._model_download_task = model
        orch._auto_migrate_task = migrate
        orch._update_check_task = update
        with patch.object(gw.registry, "shutdown_tasks", return_value=[]):
            await orch._shutdown()
        orch._socket_client.close.assert_awaited_once()
        assert model.cancelled() and migrate.cancelled() and update.cancelled()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_dashboard / _init_api_server port handling
# ═══════════════════════════════════════════════════════════════════════════


def _prepared_orchestrator() -> Any:
    orch = _make_orchestrator()
    orch._cfg.dashboard.url = "http://127.0.0.1:5476"
    orch.sessions = MagicMock()
    orch.cron_svc = MagicMock()
    orch.subagent_mgr = None
    orch.task_runner = None
    orch.slack = None
    return orch


class TestDashboardPortSelection:
    """``--port`` override + ephemeral bound-port read-back."""

    @pytest.mark.asyncio
    async def test_ephemeral_port_is_read_back_from_runner(self):
        orch = _prepared_orchestrator()
        orch._port_override = "auto"
        runner = MagicMock()
        runner.addresses = [("127.0.0.1", 54321)]
        ds = _mock_dashboard_state()
        with patch.object(gw, "LessonStore", MagicMock()):
            with patch.object(gw, "start_dashboard", AsyncMock(return_value=(runner, ds))) as start:
                await orch._init_dashboard()
        assert start.await_args.kwargs["port"] == 0
        assert orch._dashboard_port == 54321
        assert ds.no_crons is True

    @pytest.mark.asyncio
    async def test_literal_port_override_is_used_verbatim(self):
        orch = _prepared_orchestrator()
        orch._port_override = "8123"
        runner = MagicMock()
        runner.addresses = [("127.0.0.1", 999)]
        with patch.object(gw, "LessonStore", MagicMock()):
            with patch.object(
                gw, "start_dashboard", AsyncMock(return_value=(runner, _mock_dashboard_state()))
            ) as start:
                await orch._init_dashboard()
        assert start.await_args.kwargs["port"] == 8123
        # A non-zero request means the runner's bound address is never consulted.
        assert orch._dashboard_port == 8123

    @pytest.mark.asyncio
    async def test_api_server_ephemeral_port_is_read_back(self):
        orch = _prepared_orchestrator()
        orch._port_override = "auto"
        runner = MagicMock()
        runner.addresses = [("127.0.0.1", 45454)]
        ds = _mock_dashboard_state()
        with patch.object(gw, "LessonStore", MagicMock()):
            with patch(
                "kiro_crew.dashboard.start_api_server", AsyncMock(return_value=(runner, ds))
            ) as start:
                await orch._init_api_server()
        assert start.await_args.kwargs["port"] == 0
        assert orch._dashboard_port == 45454
        assert ds.no_crons is True

    @pytest.mark.asyncio
    async def test_api_server_literal_port_override(self):
        orch = _prepared_orchestrator()
        orch._port_override = "9100"
        runner = MagicMock()
        runner.addresses = [("127.0.0.1", 1)]
        with patch.object(gw, "LessonStore", MagicMock()):
            with patch(
                "kiro_crew.dashboard.start_api_server",
                AsyncMock(return_value=(runner, _mock_dashboard_state())),
            ) as start:
                await orch._init_api_server()
        assert start.await_args.kwargs["port"] == 9100
        assert orch._dashboard_port == 9100


# ═══════════════════════════════════════════════════════════════════════════
# Tests: embeddings + auto-migration
# ═══════════════════════════════════════════════════════════════════════════


class TestStartEmbeddings:
    """A custom model that cannot be used is reported, not downloaded."""

    @pytest.mark.asyncio
    async def test_unusable_custom_model_warns_and_skips_bind(self, caplog):
        orch = _make_orchestrator()
        orch.vector_memory = MagicMock()
        orch.vector_memory.embed_fn = None
        sentinel = object()
        with caplog.at_level("WARNING"):
            with patch.object(gw, "model_file_present", return_value=False):
                with patch.object(gw, "embedding_model_is_custom", return_value=True):
                    with patch.object(gw, "start_background_model_download", return_value=sentinel):
                        await orch._start_embeddings()
        assert orch.vector_memory.embed_fn is None
        assert orch.vector_memory.embed_fn_factory is gw.make_sync_embed_fn
        assert orch._model_download_task is sentinel
        assert "Custom embedding model is not usable" in caplog.text


class TestAutoMigrateMemory:
    """Boot-time markdown→vector migration and the re-embed sweep."""

    @pytest.mark.asyncio
    async def test_migration_flips_flag_and_survives_audit_failure(self):
        orch = _make_orchestrator()
        store = MagicMock()
        store.embed_fn = None
        store.migrate_from_markdown = MagicMock(
            return_value={"semantic": 2, "episodic": 3, "skipped": 1}
        )
        store._log_event = MagicMock(side_effect=RuntimeError("audit sink down"))
        store.backfill_missing_embeddings = MagicMock(return_value=3)
        orch.vector_memory = store
        orch._cfg.memory.migrated = False
        orch.consolidator = MagicMock()
        orch._set_memory_migrated = AsyncMock()
        embedder = MagicMock()
        embedder.is_ready = MagicMock(return_value=True)
        embedder.wait_ready = MagicMock(return_value=True)

        with patch.object(gw, "get_shared_embedder", return_value=embedder):
            with patch.object(gw, "model_file_present", return_value=True):
                with patch.object(gw, "make_sync_embed_fn", return_value=lambda s: [0.0]):
                    with patch.object(gw, "reconcile_store_embedding_space") as reconcile:
                        with patch("kiro_crew.memory.legacy_memory_present", return_value=True):
                            await orch._auto_migrate_memory()

        orch._set_memory_migrated.assert_awaited_once_with(True)
        assert orch._cfg.memory.migrated is True
        assert orch.consolidator._migrated is True
        store.migrate_from_markdown.assert_called_once()
        store.backfill_missing_embeddings.assert_called_once()
        # Reconciled once up front (ready backend) and once inside the sweep.
        assert reconcile.call_count == 2

    @pytest.mark.asyncio
    @pytest.mark.xdist_group("caplog_slack_gw")
    async def test_download_error_and_unready_model_defer_the_sweep(self, caplog):
        orch = _make_orchestrator()
        store = MagicMock()
        store.embed_fn = None
        orch.vector_memory = store
        orch._cfg.memory.migrated = True  # phase 1 already done
        orch.consolidator = None
        embedder = MagicMock()
        embedder.is_ready = MagicMock(return_value=False)
        embedder.wait_ready = MagicMock(return_value=False)

        async def _boom() -> None:
            raise RuntimeError("download failed")

        orch._model_download_task = asyncio.create_task(_boom())

        with caplog.at_level("INFO"):
            with patch.object(gw, "get_shared_embedder", return_value=embedder):
                with patch.object(gw, "model_file_present", return_value=False):
                    await orch._auto_migrate_memory()

        store.backfill_missing_embeddings.assert_not_called()
        assert "deferring" in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.xdist_group("caplog_slack_gw")
    async def test_missing_store_is_a_no_op(self, caplog):
        orch = _make_orchestrator()
        orch.vector_memory = None
        with caplog.at_level("DEBUG"):
            await orch._auto_migrate_memory()
        assert "vector memory not initialised" in caplog.text

    @pytest.mark.asyncio
    async def test_set_memory_migrated_delegates_to_handler(self):
        orch = _make_orchestrator()
        with patch(
            "kiro_crew.dashboard.handlers.memory._set_migrated", AsyncMock()
        ) as set_migrated:
            await orch._set_memory_migrated(True)
        set_migrated.assert_awaited_once_with(True)


# ═══════════════════════════════════════════════════════════════════════════
# Tests: small init / notification surfaces
# ═══════════════════════════════════════════════════════════════════════════


class TestInitCrew:
    """Crew mode is optional: a construction failure disables it silently."""

    def test_orchestrator_failure_disables_crew_mode(self, caplog):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        ds.crew = None
        orch.dashboard_state = ds
        with caplog.at_level("WARNING"):
            with patch(
                "kiro_crew.crew_chat.CrewOrchestrator", side_effect=RuntimeError("bad wiring")
            ):
                orch._init_crew()
        assert "crew mode disabled" in caplog.text

    def test_no_dashboard_state_skips_crew_setup(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        with patch("kiro_crew.crew_chat.CrewOrchestrator") as ctor:
            orch._init_crew()
        ctor.assert_not_called()


class TestInitMcpDiscovery:
    """Configured MCP servers are logged at boot for diagnosability."""

    @pytest.mark.xdist_group("caplog_slack_gw")
    def test_configured_servers_are_listed(self, caplog):
        orch = _make_orchestrator()
        servers = [SimpleNamespace(name="builder-mcp"), SimpleNamespace(name="playwright-mcp")]
        with caplog.at_level("INFO"):
            with patch("kiro_crew.mcp_discovery.list_servers", return_value=servers):
                orch._init_mcp_discovery()
        assert "builder-mcp, playwright-mcp" in caplog.text

    def test_listing_failure_is_swallowed(self):
        orch = _make_orchestrator()
        with patch("kiro_crew.mcp_discovery.list_servers", side_effect=RuntimeError("boom")):
            orch._init_mcp_discovery()  # must not raise


class TestSubagentCoalescer:
    """The lazily-built coalescer broadcasts through dashboard_state."""

    def test_broadcast_closures_route_to_dashboard_state(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        coalescer = orch._subagent_coalescer()
        assert orch._subagent_coalescer() is coalescer  # cached
        coalescer._broadcast_all("subagent_event", {"a": 1})
        coalescer._broadcast_subs("subagent_status", {"b": 2})
        ds.broadcast_ws.assert_called_once_with("subagent_event", {"a": 1})
        ds.broadcast_ws_subagent_subscribers.assert_called_once_with("subagent_status", {"b": 2})


class TestNotifyNudgeExpired:
    """Expiry wording distinguishes the runtime budget from the cycle cap."""

    def test_runtime_budget_wording(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        loop = NudgeLoop(
            id="loop-1",
            slot_key="chat-1",
            message="keep checking",
            max_cycles=10,
            cycle_count=3,
            max_runtime_secs=60,
            created_ts=time.time() - 600,
        )
        orch._notify_nudge_expired(loop)
        ds.notify.assert_called_once()
        title = ds.notify.call_args.args[1]
        body = ds.notify.call_args.args[2]
        assert title == "Monitoring loop spent its time budget"
        assert "60s wall-clock budget" in body

    def test_cycle_cap_wording(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        loop = NudgeLoop(
            id="loop-2",
            slot_key="chat-2",
            message="keep checking",
            max_cycles=4,
            cycle_count=4,
        )
        orch._notify_nudge_expired(loop)
        assert ds.notify.call_args.args[1] == "Monitoring loop hit its cycle cap"

    def test_no_dashboard_state_is_a_no_op(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch._notify_nudge_expired(
            NudgeLoop(id="l", slot_key="chat-3", message="m")
        )  # must not raise


class TestPersistSlotTitle:
    """Title persistence is skipped when there is no conversation log."""

    @pytest.mark.asyncio
    async def test_missing_conversation_log_skips_write(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        ds.conversation_log = None
        orch.dashboard_state = ds
        slot = MagicMock()
        slot.key = "chat-1"
        with patch("asyncio.to_thread", AsyncMock()) as to_thread:
            await orch._persist_slot_title(slot)
        to_thread.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_write_failure_is_best_effort(self, caplog):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        conv = MagicMock()
        conv.set_title = MagicMock(side_effect=RuntimeError("disk full"))
        ds.conversation_log = conv
        orch.dashboard_state = ds
        slot = MagicMock()
        slot.key = "chat-1"
        slot.title = "💓 something"
        with caplog.at_level("WARNING"):
            await orch._persist_slot_title(slot)  # must not raise
        assert "failed to persist slot title" in caplog.text


# ═══════════════════════════════════════════════════════════════════════════
# Tests: OPTIONS bookkeeping + cron subagent response delivery
# ═══════════════════════════════════════════════════════════════════════════


class TestRememberOptions:
    """Recording a posted OPTIONS control is best-effort."""

    @pytest.mark.xdist_group("caplog_slack_gw")
    def test_record_failure_is_swallowed(self, caplog):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        with caplog.at_level("DEBUG"):
            with patch.object(gw, "remember_slack_options", side_effect=RuntimeError("store busy")):
                orch._remember_options("cron:j1", "C1", "111.1", ["A", "B"], [{}], "Options")
        assert "Failed to record OPTIONS control" in caplog.text

    def test_no_ts_or_choices_records_nothing(self):
        orch = _make_orchestrator()
        with patch.object(gw, "remember_slack_options") as remember:
            orch._remember_options("cron:j1", "C1", "", ["A"], [{}], "Options")
            orch._remember_options("cron:j1", "C1", "111.1", [], [{}], "Options")
        remember.assert_not_called()


class TestDeliverCronResponse:
    """A cron session's post-subagent response routes to Slack."""

    @pytest.mark.asyncio
    async def test_unresolvable_channel_returns_false(self, caplog):
        orch = _make_orchestrator()
        orch.slack = _mock_slack()
        sessions = MagicMock()
        sessions.get_channel = MagicMock(return_value="")
        sessions.get_thread = MagicMock(return_value=None)
        orch.sessions = sessions
        orch._open_dm_with_retry = AsyncMock(return_value=None)
        with caplog.at_level("WARNING"):
            assert await orch._deliver_cron_response("cron:j1", "done") is False
        orch._open_dm_with_retry.assert_awaited_once()
        assert "no channel resolved" in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.xdist_group("caplog_slack_gw")
    async def test_options_post_failure_still_delivers_text(self, caplog):
        orch = _make_orchestrator()
        slack = _mock_slack()
        slack.post_blocks = AsyncMock(side_effect=RuntimeError("block kit rejected"))
        orch.slack = slack
        sessions = MagicMock()
        sessions.get_channel = MagicMock(return_value="C1")
        sessions.get_thread = MagicMock(return_value="111.1")
        orch.sessions = sessions
        with caplog.at_level("DEBUG"):
            ok = await orch._deliver_cron_response(
                "cron:j1", "All done.\n\n[OPTIONS: Merge it now | Hold off]"
            )
        assert ok is True
        assert slack.post_message.await_count >= 1
        assert slack.post_message.await_args.args[0] == "C1"
        assert "failed to post OPTIONS blocks" in caplog.text

    @pytest.mark.asyncio
    async def test_silent_and_empty_text_short_circuit(self):
        orch = _make_orchestrator()
        orch.slack = _mock_slack()
        orch.sessions = MagicMock()
        assert await orch._deliver_cron_response("cron:j1", "x", silent=True) is False
        assert await orch._deliver_cron_response("cron:j1", "   ") is False
        orch.slack.post_message.assert_not_awaited()
