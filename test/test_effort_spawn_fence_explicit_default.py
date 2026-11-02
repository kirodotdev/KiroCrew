"""cli.json overlay tests: a pending explicit Default at the next cold start.

A pending explicit Default makes startup remove the target model's entry once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from effort_helpers import _read_cli_overlay
from test_effort import _await_test

from conftest import requires_symlinks
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.config import paths as config_paths
from kiro_crew.effort import effort_settings_key
from kiro_crew.providers import acp as acp_provider
from kiro_crew.providers import acp_cli_overlay
from kiro_crew.providers.acp import _KIROCREW_EFFORT_OWNED_KEY
from kiro_crew.testing.wait import async_wait_until

pytestmark = pytest.mark.usefixtures("no_zero_tool_check")


class TestExplicitDefaultColdStart:
    """A pending explicit Default makes startup remove the target model's entry once.

    The pending projection does to ``cli.json`` what ``clear_effort(owned_only=False)``
    does on a live provider with the same backend, model and resolved level, and
    the provider reports it applied only when that projection succeeded.
    """

    _MODEL = "claude-opus-4.7"

    def _provider(self, work_dir, *, model=None, **kwargs):
        return acp_provider.AcpProvider(
            acp_backend=kwargs.pop("acp_backend", ACP_BACKEND_KIRO),
            work_dir=work_dir,
            model=model or self._MODEL,
            **kwargs,
        )

    @staticmethod
    def _write(work_dir, data):
        settings = work_dir / ".kiro" / "settings"
        settings.mkdir(parents=True, exist_ok=True)
        cli_json = settings / "cli.json"
        cli_json.write_text(json.dumps(data), encoding="utf-8")
        return cli_json

    def _operator_entry(self, work_dir, level, model=None):
        model = model or self._MODEL
        entry = {effort_settings_key(model): {"effort": level}}
        return self._write(work_dir, {"chat.modelDefaults": {model: entry}})

    @staticmethod
    def _seed_sessions_dir(tmp_path, monkeypatch, session_ids):
        """Redirect the kiro-cli transcript directory and seed a pair per session id."""
        sessions = tmp_path / "kiro-sessions"
        sessions.mkdir()
        monkeypatch.setattr(config_paths, "_sessions_dir_override", lambda: sessions)
        for sid in session_ids:
            for suffix in (".json", ".jsonl"):
                (sessions / f"{sid}{suffix}").write_text("{}", encoding="utf-8")
        return sessions

    @staticmethod
    def _session_files_present(sessions, sid):
        return [(sessions / f"{sid}{suffix}").exists() for suffix in (".json", ".jsonl")]

    @staticmethod
    def _live_client(
        work_dir,
        model,
        session_id,
        *,
        push_error=None,
        backend=ACP_BACKEND_KIRO,
        model_pin_refused="",
        requested_model="auto",
    ):
        alive = [True]

        async def kill(**_kwargs):
            alive[0] = False

        handle = SimpleNamespace(
            session_id=session_id,
            model=requested_model,
            served_model=model,
            model_pin_refused=model_pin_refused,
            memory_mode="persistent",
            keep_transcript=False,
            _cancel_hook_tasks=MagicMock(),
            destroy=AsyncMock(),
            send_command=AsyncMock(return_value=""),
            command_result=AsyncMock(
                side_effect=push_error,
                return_value={"success": True} if push_error is None else None,
            ),
        )
        runtime = SimpleNamespace(
            acp_backend=backend,
            _work_dir=work_dir,
            is_alive=lambda: alive[0],
            kill=AsyncMock(side_effect=kill),
            work_scratch_dir=None,
        )
        return AcpSessionProvider(handle, runtime, owns_runtime=True), handle, runtime

    def _stub_client_start(self, provider, monkeypatch, model, snapshots):
        """Stub the AcpClient start path, the one a harness off the shared runtime takes."""
        client = provider._client

        async def ensure_ready():
            snapshots.append(_read_cli_overlay(client._work_dir))
            client._resolved_model_id = model

        monkeypatch.setattr(client, "ensure_ready", ensure_ready)
        monkeypatch.setattr(client, "confirm_member_projection", AsyncMock())

    def _stub_runtime_starts(
        self,
        provider,
        monkeypatch,
        models,
        snapshots,
        clients,
        resumed_flags=None,
        replay_flags=None,
        replay_flag_snapshots=None,
        backend=ACP_BACKEND_KIRO,
        teardown_error=None,
        runtime_stays_alive=False,
        refused_models=None,
    ):
        async def start_runtime():
            snapshots.append(_read_cli_overlay(provider._client._work_dir))
            index = len(snapshots) - 1
            if replay_flag_snapshots is not None:
                replay_flag_snapshots.append(
                    (
                        provider._history_replay_needed,
                        provider._defer_replay_sid_promotion,
                    )
                )
            if replay_flags is not None and replay_flags[index] is not None:
                (
                    provider._history_replay_needed,
                    provider._defer_replay_sid_promotion,
                ) = replay_flags[index]
            if index:
                assert clients[-1][2].is_alive() is False
            refused_model = refused_models[index] if refused_models is not None else ""
            served_as_pinned = not refused_model and provider._spawn_effort_model != "auto"
            client, handle, runtime = self._live_client(
                provider._client._work_dir,
                models[index],
                f"session-{index + 1}",
                backend=backend,
                model_pin_refused=refused_model,
                requested_model=models[index] if served_as_pinned else "auto",
            )
            if index == 0 and teardown_error is not None:
                client.shutdown = AsyncMock(side_effect=teardown_error)
            if index == 0 and runtime_stays_alive:
                runtime.kill = AsyncMock()
            if resumed_flags is not None:
                client.resumed = resumed_flags[index]
            clients.append((client, handle, runtime, provider._client._resume_session_id))
            provider._client = client

        monkeypatch.setattr(provider, "_start_kiro_runtime", start_runtime)
        monkeypatch.setattr(provider, "_note_zero_tool_servers", AsyncMock())

    @pytest.mark.asyncio
    async def test_unpinned_start_restarts_after_clearing_only_the_served_model(
        self, tmp_path, monkeypatch
    ):
        other_model = "claude-sonnet-4.6"
        self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    self._MODEL: {"output_config": {"effort": "max"}},
                    other_model: {"output_config": {"effort": "high"}},
                }
            },
        )
        provider = self._provider(tmp_path, model="auto")
        sessions = self._seed_sessions_dir(tmp_path, monkeypatch, ["session-1", "session-2"])
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider, monkeypatch, [self._MODEL, self._MODEL], snapshots, clients
        )
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert snapshots == [
            {self._MODEL: "max", other_model: "high"},
            {other_model: "high"},
        ]
        assert _read_cli_overlay(tmp_path) == {other_model: "high"}
        assert len(clients) == 2
        first_client, first_handle, first_runtime, _ = clients[0]
        assert provider._client is clients[1][0]
        first_runtime.kill.assert_awaited_once_with(expected=True, reason="provider shutdown")
        first_handle.destroy.assert_not_awaited()
        assert clients[1][3] == ""
        assert self._session_files_present(sessions, "session-1") == [False, False]
        assert self._session_files_present(sessions, "session-2") == [True, True]
        assert provider.explicit_effort_default_applied is True
        assert provider._explicit_effort_default_pending is False

    @pytest.mark.asyncio
    async def test_pinned_start_finishes_default_for_the_substituted_served_model(
        self, tmp_path, monkeypatch
    ):
        served_model = "claude-sonnet-4.6"
        self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    self._MODEL: {"output_config": {"effort": "max"}},
                    served_model: {"output_config": {"effort": "high"}},
                }
            },
        )
        provider = self._provider(tmp_path)
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            [served_model, served_model],
            snapshots,
            clients,
            refused_models=[self._MODEL, self._MODEL],
        )
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert snapshots == [{served_model: "high"}, {}]
        assert _read_cli_overlay(tmp_path) == {}
        assert len(clients) == 2
        assert clients[0][2].is_alive() is False
        assert provider.explicit_effort_default_applied is True
        assert provider._explicit_effort_default_pending is False

    @pytest.mark.asyncio
    async def test_pinned_start_finishes_default_for_a_differently_spelled_served_model(
        self, tmp_path, monkeypatch
    ):
        served_model = self._MODEL.upper()
        self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    self._MODEL: {"output_config": {"effort": "max"}},
                    served_model: {"output_config": {"effort": "high"}},
                }
            },
        )
        provider = self._provider(tmp_path)
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            [served_model, served_model],
            snapshots,
            clients,
        )
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert snapshots == [{served_model: "high"}, {}]
        assert _read_cli_overlay(tmp_path) == {}
        assert len(clients) == 2
        assert clients[0][2].is_alive() is False
        assert provider.explicit_effort_default_applied is True
        assert provider._explicit_effort_default_pending is False

    @pytest.mark.asyncio
    async def test_unpinned_restart_keeps_the_first_session_that_is_the_resume_target(
        self, tmp_path, monkeypatch
    ):
        """A first session the chat can still resume is never deleted by the restart."""
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        provider._client._resume_session_id = "session-1"
        sessions = self._seed_sessions_dir(tmp_path, monkeypatch, ["session-1", "session-2"])
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider, monkeypatch, [self._MODEL, self._MODEL], snapshots, clients
        )
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert len(clients) == 2
        assert clients[1][3] == "session-1"
        assert self._session_files_present(sessions, "session-1") == [True, True]
        assert self._session_files_present(sessions, "session-2") == [True, True]
        assert provider.explicit_effort_default_applied is True

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KAS, "claude"], ids=["kas", "claude"])
    @pytest.mark.asyncio
    async def test_unpinned_default_leaves_the_overlay_alone_off_the_reader_set(
        self, tmp_path, monkeypatch, backend
    ):
        # A harness that never opens cli.json must not snapshot or rewrite the
        # entry a kiro-cli chat in the same work dir spawns on, and must not pay
        # a restart for a file its engine does not read. Its Default is spent at
        # the start, as a pinned start spends it.
        cli_json = self._operator_entry(tmp_path, "max")
        before = cli_json.read_bytes()
        provider = self._provider(tmp_path, model="auto", acp_backend=backend)
        snapshots = []
        clients = []
        if backend == ACP_BACKEND_KAS:
            # Two models are staged so a start that wrongly restarts reaches its
            # own assertions rather than running the stub out of answers.
            self._stub_runtime_starts(
                provider,
                monkeypatch,
                [self._MODEL, self._MODEL],
                snapshots,
                clients,
                backend=backend,
            )
        else:
            self._stub_client_start(provider, monkeypatch, self._MODEL, snapshots)
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert provider.served_model == self._MODEL
        assert len(snapshots) == 1
        assert cli_json.read_bytes() == before
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}
        assert provider.explicit_effort_default_applied is True
        assert provider._explicit_effort_default_pending is False

    @pytest.mark.asyncio
    async def test_unpinned_restart_repeats_the_first_start_resume_decision(
        self, tmp_path, monkeypatch
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        provider._client._resume_session_id = "persisted-session"
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider, monkeypatch, [self._MODEL, self._MODEL], snapshots, clients
        )
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert len(clients) == 2
        assert [client[3] for client in clients] == [
            "persisted-session",
            "persisted-session",
        ]
        assert all(client[0].resumed is False for client in clients)
        assert provider.explicit_effort_default_applied is True
        assert provider._explicit_effort_default_pending is False

    @pytest.mark.asyncio
    async def test_restart_uses_only_the_replacement_native_load_replay_flags(
        self, tmp_path, monkeypatch, caplog
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        replay_flag_snapshots = []
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            [self._MODEL, self._MODEL],
            snapshots,
            clients,
            resumed_flags=[False, True],
            replay_flags=[(True, True), None],
            replay_flag_snapshots=replay_flag_snapshots,
        )
        provider.arm_explicit_effort_default(nullcontext)

        with caplog.at_level(logging.INFO, logger=acp_provider.__name__):
            await provider.start()

        assert replay_flag_snapshots == [(False, False), (False, False)]
        assert provider._history_replay_needed is False
        assert provider._defer_replay_sid_promotion is False
        assert (
            sum(
                "restored with session/load keeps the level" in record.getMessage()
                for record in caplog.records
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_restart_reapplies_tool_search_replay_flags_from_replacement_start(
        self, tmp_path, monkeypatch
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        replay_flag_snapshots = []
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            [self._MODEL, self._MODEL],
            snapshots,
            clients,
            replay_flags=[(True, True), (True, True)],
            replay_flag_snapshots=replay_flag_snapshots,
        )
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert replay_flag_snapshots == [(False, False), (False, False)]
        assert provider._history_replay_needed is True
        assert provider._defer_replay_sid_promotion is True

    @pytest.mark.asyncio
    async def test_restart_preserves_start_entry_replay_state_only(self, tmp_path, monkeypatch):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        provider._history_replay_needed = True
        snapshots = []
        clients = []
        replay_flag_snapshots = []
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            [self._MODEL, self._MODEL],
            snapshots,
            clients,
            replay_flags=[(True, True), None],
            replay_flag_snapshots=replay_flag_snapshots,
        )
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert replay_flag_snapshots == [(True, False), (True, False)]
        assert provider._history_replay_needed is True
        assert provider._defer_replay_sid_promotion is False

    @pytest.mark.asyncio
    async def test_unpinned_native_loaded_session_is_not_restarted(
        self, tmp_path, monkeypatch, caplog
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            [self._MODEL, self._MODEL],
            snapshots,
            clients,
            resumed_flags=[True, False],
        )
        provider.arm_explicit_effort_default(nullcontext)

        with caplog.at_level(logging.INFO, logger=acp_provider.__name__):
            await provider.start()

        assert snapshots == [{self._MODEL: "max"}]
        assert _read_cli_overlay(tmp_path) == {}
        assert len(clients) == 1
        assert provider._client is clients[0][0]
        assert clients[0][2].is_alive() is True
        clients[0][2].kill.assert_not_awaited()
        assert provider.explicit_effort_default_applied is True
        assert provider._explicit_effort_default_pending is False
        assert (
            sum(
                "restored with session/load keeps the level" in record.getMessage()
                for record in caplog.records
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_unpinned_start_with_no_served_model_entry_changes_nothing(
        self, tmp_path, monkeypatch
    ):
        other_model = "claude-sonnet-4.6"
        self._operator_entry(tmp_path, "high", model=other_model)
        before = (tmp_path / ".kiro" / "settings" / "cli.json").read_bytes()
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        self._stub_runtime_starts(provider, monkeypatch, [self._MODEL], snapshots, clients)
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert len(clients) == 1
        assert snapshots == [{other_model: "high"}]
        assert (tmp_path / ".kiro" / "settings" / "cli.json").read_bytes() == before
        assert provider.explicit_effort_default_applied is True

    @pytest.mark.parametrize(
        ("model", "inactive_key"),
        [
            ("claude-opus-4.7", "reasoning"),
            ("gpt-5.6-sol", "output_config"),
        ],
        ids=["claude-reasoning", "gpt-output-config"],
    )
    @pytest.mark.asyncio
    async def test_unpinned_start_ignores_effort_under_inactive_family_key(
        self, tmp_path, monkeypatch, model, inactive_key
    ):
        cli_json = self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    model: {inactive_key: {"effort": "max"}},
                }
            },
        )
        before = cli_json.read_bytes()
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        self._stub_runtime_starts(provider, monkeypatch, [model, model], snapshots, clients)
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert len(clients) == 1
        assert cli_json.read_bytes() == before
        assert provider.explicit_effort_default_applied is True

    def test_overlay_snapshot_counts_only_each_models_active_effort_key(self, tmp_path):
        claude_model = "claude-opus-4.7"
        gpt_model = "gpt-5.6-sol"
        self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    claude_model: {"output_config": {"effort": "max"}},
                    gpt_model: {"output_config": {"effort": "high"}},
                }
            },
        )

        assert acp_cli_overlay._cli_overlay_effort_models(tmp_path) == {claude_model}

    @pytest.mark.asyncio
    async def test_unpinned_start_does_not_remove_peer_entry_written_after_spawn(
        self, tmp_path, monkeypatch
    ):
        other_model = "claude-sonnet-4.6"
        self._operator_entry(tmp_path, "high", model=other_model)
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []

        async def start_runtime():
            snapshots.append(_read_cli_overlay(provider._client._work_dir))
            self._write(
                tmp_path,
                {
                    "chat.modelDefaults": {
                        self._MODEL: {"output_config": {"effort": "max"}},
                        other_model: {"output_config": {"effort": "high"}},
                    }
                },
            )
            client, handle, runtime = self._live_client(
                provider._client._work_dir, self._MODEL, "session-1"
            )
            clients.append((client, handle, runtime))
            provider._client = client

        monkeypatch.setattr(provider, "_start_kiro_runtime", start_runtime)
        monkeypatch.setattr(provider, "_note_zero_tool_servers", AsyncMock())
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert snapshots == [{other_model: "high"}]
        assert len(clients) == 1
        assert _read_cli_overlay(tmp_path) == {
            self._MODEL: "max",
            other_model: "high",
        }
        assert provider.explicit_effort_default_applied is True

    @pytest.mark.asyncio
    async def test_unpinned_start_with_non_effort_model_keeps_its_entry(
        self, tmp_path, monkeypatch
    ):
        model = "claude-haiku-4.5"
        cli_json = self._operator_entry(tmp_path, "max", model=model)
        before = cli_json.read_bytes()
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        self._stub_runtime_starts(provider, monkeypatch, [model], snapshots, clients)
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert len(clients) == 1
        assert snapshots == [{model: "max"}]
        assert cli_json.read_bytes() == before
        assert provider.explicit_effort_default_applied is True

    @pytest.mark.asyncio
    async def test_unpinned_start_keeps_default_pending_when_served_model_is_unknown(
        self, tmp_path, monkeypatch, caplog
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        self._stub_runtime_starts(provider, monkeypatch, [""], snapshots, clients)
        provider.arm_explicit_effort_default(nullcontext)

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            await provider.start()

        assert len(clients) == 1
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}
        assert provider.explicit_effort_default_applied is False
        assert provider._explicit_effort_default_pending is True
        assert sum("served model is unknown" in r.getMessage() for r in caplog.records) == 1

    @pytest.mark.asyncio
    async def test_unpinned_start_applies_default_when_backend_reports_auto(
        self, tmp_path, monkeypatch, caplog
    ):
        # kiro-cli reports ``currentModelId`` ``auto`` for a session with no model
        # pinned. That session takes no effort, so the Default needs no file change
        # and is spent at this start; only a backend reporting NO model stays pending.
        cli_json = self._operator_entry(tmp_path, "max")
        before = cli_json.read_bytes()
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        self._stub_runtime_starts(provider, monkeypatch, ["auto"], snapshots, clients)
        provider.arm_explicit_effort_default(nullcontext)

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            await provider.start()

        assert len(clients) == 1
        assert snapshots == [{self._MODEL: "max"}]
        assert cli_json.read_bytes() == before
        assert provider.explicit_effort_default_applied is True
        assert provider._explicit_effort_default_pending is False
        assert not any("served model is unknown" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("push_fails", [False, True], ids=["push-succeeds", "push-fails"])
    async def test_unpinned_start_projects_workspace_default_and_requires_live_push(
        self, tmp_path, monkeypatch, push_fails
    ):
        other_model = "claude-sonnet-4.6"
        self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    self._MODEL: {"output_config": {"effort": "max"}},
                    other_model: {"output_config": {"effort": "high"}},
                }
            },
        )
        provider = self._provider(tmp_path, model="auto", effort_defaults={self._MODEL: "low"})
        snapshots = []
        clients = []

        async def start_runtime():
            snapshots.append(_read_cli_overlay(provider._client._work_dir))
            client, handle, runtime = self._live_client(
                provider._client._work_dir,
                self._MODEL,
                "session-1",
            )
            handle.command_result.return_value = {"success": not push_fails}
            clients.append((client, handle, runtime))
            provider._client = client

        monkeypatch.setattr(provider, "_start_kiro_runtime", start_runtime)
        monkeypatch.setattr(provider, "_note_zero_tool_servers", AsyncMock())
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert snapshots == [{self._MODEL: "max", other_model: "high"}]
        assert _read_cli_overlay(tmp_path) == {
            self._MODEL: "low",
            other_model: "high",
        }
        assert provider.explicit_effort_default_applied is not push_fails
        assert provider._explicit_effort_default_pending is push_fails
        clients[0][1].command_result.assert_awaited_once_with("/effort", {"level": "low"})

    @pytest.mark.asyncio
    async def test_unpinned_start_keeps_default_pending_when_rewrite_is_refused(
        self, tmp_path, monkeypatch, caplog
    ):
        other_model = "claude-sonnet-4.6"
        self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    self._MODEL: {"output_config": {"effort": "max"}},
                    other_model: {"output_config": {"effort": "high"}},
                }
            },
        )
        provider = self._provider(tmp_path, model="auto", effort_defaults={self._MODEL: "low"})
        snapshots = []
        clients = []
        self._stub_runtime_starts(provider, monkeypatch, [self._MODEL], snapshots, clients)
        monkeypatch.setattr(
            acp_provider,
            "_write_cli_overlay",
            MagicMock(side_effect=OSError("version guard refused the write")),
        )
        provider.arm_explicit_effort_default(nullcontext)

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            await provider.start()

        assert len(clients) == 1
        assert _read_cli_overlay(tmp_path) == {
            self._MODEL: "max",
            other_model: "high",
        }
        assert provider.explicit_effort_default_applied is False
        assert provider._explicit_effort_default_pending is True
        assert sum("overlay rewrite failed" in r.getMessage() for r in caplog.records) == 1

    @pytest.mark.asyncio
    async def test_unpinned_restart_stops_after_second_model_has_its_own_entry(
        self, tmp_path, monkeypatch, caplog
    ):
        second_model = "claude-sonnet-4.6"
        self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    self._MODEL: {"output_config": {"effort": "max"}},
                    second_model: {"output_config": {"effort": "high"}},
                }
            },
        )
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider, monkeypatch, [self._MODEL, second_model], snapshots, clients
        )
        provider.arm_explicit_effort_default(nullcontext)

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            with pytest.raises(
                RuntimeError,
                match="replacement runtime still reads its served model's effort entry",
            ):
                await provider.start()

        assert snapshots == [
            {self._MODEL: "max", second_model: "high"},
            {second_model: "high"},
        ]
        assert len(clients) == 2
        assert provider._client is clients[1][0]
        assert _read_cli_overlay(tmp_path) == {second_model: "high"}
        assert provider.explicit_effort_default_applied is False
        assert provider._explicit_effort_default_pending is True
        assert sum("no third runtime" in r.getMessage() for r in caplog.records) == 1

    @pytest.mark.asyncio
    async def test_unpinned_restart_applies_the_default_when_the_replacement_reports_auto(
        self, tmp_path, monkeypatch, caplog
    ):
        # The first runtime served a model whose entry had to be cleared; the
        # replacement's backend reports ``auto``, a session no effort level can be
        # keyed under, so it read no entry and the Default is spent.
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        self._seed_sessions_dir(tmp_path, monkeypatch, ["session-1", "session-2"])
        snapshots = []
        clients = []
        self._stub_runtime_starts(provider, monkeypatch, [self._MODEL, "auto"], snapshots, clients)
        provider.arm_explicit_effort_default(nullcontext)

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            await provider.start()

        assert len(clients) == 2
        assert snapshots == [{self._MODEL: "max"}, {}]
        assert _read_cli_overlay(tmp_path) == {}
        assert provider.explicit_effort_default_applied is True
        assert provider._explicit_effort_default_pending is False
        assert not any(
            "replacement runtime's served model is unknown" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_unpinned_restart_keeps_the_default_pending_when_the_replacement_reports_no_model(
        self, tmp_path, monkeypatch, caplog
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        self._seed_sessions_dir(tmp_path, monkeypatch, ["session-1", "session-2"])
        snapshots = []
        clients = []
        self._stub_runtime_starts(provider, monkeypatch, [self._MODEL, ""], snapshots, clients)
        provider.arm_explicit_effort_default(nullcontext)

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            await provider.start()

        assert len(clients) == 2
        assert snapshots == [{self._MODEL: "max"}, {}]
        assert _read_cli_overlay(tmp_path) == {}
        assert provider.explicit_effort_default_applied is False
        assert provider._explicit_effort_default_pending is True
        assert (
            sum(
                "replacement runtime's served model is unknown" in r.getMessage()
                for r in caplog.records
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_unpinned_restart_fails_when_first_client_shutdown_raises(
        self, tmp_path, monkeypatch
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        sessions = self._seed_sessions_dir(tmp_path, monkeypatch, ["session-1"])
        snapshots = []
        clients = []
        shutdown_error = RuntimeError("shutdown failed")
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            [self._MODEL, self._MODEL],
            snapshots,
            clients,
            teardown_error=shutdown_error,
        )
        provider.arm_explicit_effort_default(nullcontext)

        with pytest.raises(RuntimeError, match="shutting down the first runtime failed") as exc:
            await provider.start()

        assert exc.value.__cause__ is shutdown_error
        assert snapshots == [{self._MODEL: "max"}]
        assert provider._client is clients[0][0]
        assert provider.explicit_effort_default_applied is False
        assert provider._explicit_effort_default_pending is True
        assert self._session_files_present(sessions, "session-1") == [True, True]

    @pytest.mark.asyncio
    async def test_unpinned_restart_fails_when_first_runtime_remains_alive(
        self, tmp_path, monkeypatch
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        sessions = self._seed_sessions_dir(tmp_path, monkeypatch, ["session-1"])
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            [self._MODEL, self._MODEL],
            snapshots,
            clients,
            runtime_stays_alive=True,
        )
        provider.arm_explicit_effort_default(nullcontext)

        with pytest.raises(RuntimeError, match="the first runtime did not stop"):
            await provider.start()

        assert snapshots == [{self._MODEL: "max"}]
        assert provider._client is clients[0][0]
        assert provider.explicit_effort_default_applied is False
        assert provider._explicit_effort_default_pending is True
        assert self._session_files_present(sessions, "session-1") == [True, True]

    @pytest.mark.asyncio
    async def test_unpinned_restart_failure_raises_without_restoring_dead_client(
        self, tmp_path, monkeypatch
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        placeholder_client = provider._client
        snapshots = []
        clients = []

        async def start_runtime():
            snapshots.append(_read_cli_overlay(provider._client._work_dir))
            if len(snapshots) == 2:
                raise RuntimeError("replacement start failed")
            client, handle, runtime = self._live_client(
                provider._client._work_dir, self._MODEL, "session-1"
            )
            clients.append((client, handle, runtime))
            provider._client = client

        monkeypatch.setattr(provider, "_start_kiro_runtime", start_runtime)
        monkeypatch.setattr(provider, "_note_zero_tool_servers", AsyncMock())
        provider.arm_explicit_effort_default(nullcontext)

        with pytest.raises(RuntimeError, match="replacement start failed"):
            await provider.start()

        assert snapshots == [{self._MODEL: "max"}, {}]
        assert provider._client is placeholder_client
        assert clients[0][2].is_alive() is False
        assert provider.explicit_effort_default_applied is False
        assert provider._explicit_effort_default_pending is True

    @pytest.mark.asyncio
    async def test_start_removes_an_operator_entry_once_when_explicit_default_is_pending(
        self, tmp_path, monkeypatch
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path)
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider, monkeypatch, [self._MODEL, self._MODEL], snapshots, clients
        )
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert _read_cli_overlay(tmp_path) == {}
        assert provider.explicit_effort_default_applied is True

        # A later spawn of the same provider (a resume, a model swap) projects
        # with the ownership guard again.
        await provider._client.shutdown()
        self._operator_entry(tmp_path, "high")
        await provider.start()
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "high"}

    @pytest.mark.asyncio
    async def test_start_keeps_an_operator_entry_without_explicit_default(
        self, tmp_path, monkeypatch
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())

        await provider.start()

        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}
        assert provider.explicit_effort_default_applied is False

    @pytest.mark.asyncio
    async def test_a_refused_pin_spawns_on_a_file_without_another_chats_level(
        self, tmp_path, monkeypatch
    ):
        # The pin is refused only after the spawn has read the file, so the
        # backend default it falls back to must already carry no chat's level.
        backend_default = "claude-sonnet-4.6"
        acp_cli_overlay._write_cli_overlay(tmp_path, backend_default, "max")
        provider = self._provider(tmp_path)
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            [backend_default],
            snapshots,
            clients,
            refused_models=[self._MODEL],
        )

        await provider.start()

        assert snapshots == [{}]
        assert provider.served_model == backend_default
        assert len(clients) == 1
        clients[0][1].command_result.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_refused_pin_that_takes_no_effort_spawns_on_a_clean_file(
        self, tmp_path, monkeypatch
    ):
        backend_default = "claude-sonnet-4.6"
        acp_cli_overlay._write_cli_overlay(tmp_path, backend_default, "max")
        provider = self._provider(tmp_path, model="claude-haiku-4.5")
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            [backend_default],
            snapshots,
            clients,
            refused_models=["claude-haiku-4.5"],
        )

        await provider.start()

        assert snapshots == [{}]
        assert provider.served_model == backend_default
        clients[0][1].command_result.assert_not_awaited()

    @pytest.mark.asyncio
    @requires_symlinks
    async def test_a_refused_file_leaves_the_default_pending_for_the_next_start(
        self, tmp_path, monkeypatch
    ):
        # A linked cli.json is never rewritten, so the projection fails and the
        # intent is not spent. The next start that can rewrite the file applies it.
        target = tmp_path / "outside.json"
        outside = {"chat.modelDefaults": {self._MODEL: {"output_config": {"effort": "max"}}}}
        target.write_text(json.dumps(outside), encoding="utf-8")
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        cli_json = settings / "cli.json"
        try:
            cli_json.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")
        provider = self._provider(tmp_path)
        provider.arm_explicit_effort_default(nullcontext)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())

        await provider.start()

        assert provider.explicit_effort_default_applied is False
        assert json.loads(target.read_text(encoding="utf-8")) == outside

        cli_json.unlink()
        self._operator_entry(tmp_path, "max")
        snapshots = []
        clients = []
        self._stub_runtime_starts(provider, monkeypatch, [self._MODEL], snapshots, clients)
        await provider.start()

        assert provider.explicit_effort_default_applied is True
        assert _read_cli_overlay(tmp_path) == {}

    def _link_cli_json_to_outside_entry(self, tmp_path):
        target = tmp_path / "outside.json"
        outside = {"chat.modelDefaults": {self._MODEL: {"output_config": {"effort": "max"}}}}
        target.write_text(json.dumps(outside), encoding="utf-8")
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        (settings / "cli.json").symlink_to(target)
        return target, outside

    @pytest.mark.asyncio
    @requires_symlinks
    async def test_a_refused_file_keeps_the_default_pending_when_the_pin_is_served_as_auto(
        self, tmp_path, monkeypatch
    ):
        # The linked file is refused, so the pin's entry is never removed. kiro-cli
        # then refuses the pin and serves ``auto``: the file still holds the level,
        # so the Default is not spent on the sentinel alone.
        target, outside = self._link_cli_json_to_outside_entry(tmp_path)
        provider = self._provider(tmp_path)
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            ["auto"],
            snapshots,
            clients,
            refused_models=[self._MODEL],
        )
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert provider.explicit_effort_default_applied is False
        assert provider._explicit_effort_default_pending is True
        assert json.loads(target.read_text(encoding="utf-8")) == outside

    @pytest.mark.asyncio
    @requires_symlinks
    async def test_a_refused_file_keeps_the_default_pending_on_an_unpinned_auto_start(
        self, tmp_path, monkeypatch
    ):
        # Nothing was read before the spawn, so ``auto`` reported by the backend
        # cannot prove the file holds no level; the next start that can read the
        # file applies the Default.
        target, outside = self._link_cli_json_to_outside_entry(tmp_path)
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        self._stub_runtime_starts(provider, monkeypatch, ["auto"], snapshots, clients)
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert provider.explicit_effort_default_applied is False
        assert provider._explicit_effort_default_pending is True
        assert json.loads(target.read_text(encoding="utf-8")) == outside

    def test_each_projection_resets_its_receipt_before_the_write(self, tmp_path, monkeypatch):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path)
        provider.arm_explicit_effort_default(nullcontext)

        provider._project_explicit_effort_default()
        assert provider._explicit_effort_default_projected is True

        monkeypatch.setattr(provider, "_apply_effort_overlay", MagicMock(return_value=False))
        provider._project_explicit_effort_default()

        assert provider._explicit_effort_default_projected is False
        assert provider._explicit_effort_default_pending is True
        assert provider.explicit_effort_default_applied is False

    @pytest.mark.asyncio
    async def test_the_rewrite_runs_inside_its_fence_and_the_spawn_after_it(
        self, tmp_path, monkeypatch
    ):
        # The fence refuses warm-runtime claims while it is up and dates the
        # rewrite as it comes down: the write must be inside it, and the rest of
        # the start, which can take seconds, outside it.
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path)
        events: list[str] = []

        @contextmanager
        def fence():
            events.append("fence up")
            try:
                yield
            finally:
                events.append("fence down")

        real_atomic_write = acp_provider.atomic_write

        def record_write(path, contents, **kwargs):
            events.append("write")
            real_atomic_write(path, contents, **kwargs)

        monkeypatch.setattr(acp_provider, "atomic_write", record_write)
        monkeypatch.setattr(
            provider, "_start_kiro_runtime", AsyncMock(side_effect=lambda: events.append("spawn"))
        )
        provider.arm_explicit_effort_default(fence)

        await provider.start()

        assert events == ["fence up", "write", "fence down", "spawn"]
        assert _read_cli_overlay(tmp_path) == {}

    @pytest.mark.asyncio
    async def test_a_start_cancelled_during_the_rewrite_still_knows_it_ran(
        self, tmp_path, monkeypatch
    ):
        # A cold start cancelled here arms the session's flag again unless the
        # provider reports the replacement ran, and a flag armed after the
        # removal removes the next level anyone projects for this model.
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
        worker_started = threading.Event()
        release_worker = threading.Event()
        worker_finished = threading.Event()
        fence_down_after_the_worker: list[bool] = []
        real_apply = provider._apply_effort_overlay

        def blocked_apply(**kwargs):
            worker_started.set()
            try:
                assert release_worker.wait(timeout=5)
                return real_apply(**kwargs)
            finally:
                worker_finished.set()

        @contextmanager
        def fence():
            try:
                yield
            finally:
                fence_down_after_the_worker.append(worker_finished.is_set())

        monkeypatch.setattr(provider, "_apply_effort_overlay", blocked_apply)
        provider.arm_explicit_effort_default(fence)
        start_task = asyncio.create_task(provider.start())
        try:
            await async_wait_until(
                worker_started.is_set, describe=lambda: "worker_started was never set"
            )
            start_task.cancel()
            await asyncio.sleep(0)
            assert not start_task.done()
            assert fence_down_after_the_worker == []
        finally:
            release_worker.set()

        with pytest.raises(asyncio.CancelledError):
            await _await_test(start_task, "start_task")
        assert fence_down_after_the_worker == [True]
        assert _read_cli_overlay(tmp_path) == {}
        assert provider._explicit_effort_default_projected is True
        assert provider._explicit_effort_default_pending is True
        assert provider.explicit_effort_default_applied is False
        provider._start_kiro_runtime.assert_not_awaited()

    def test_no_level_removes_both_family_entries_only_for_explicit_default(self, tmp_path):
        cli_json = self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    self._MODEL: {
                        "output_config": {"effort": "max"},
                        "reasoning": {"effort": "high"},
                    }
                },
                "other": 1,
            },
        )
        provider = self._provider(tmp_path)

        assert provider._apply_effort_overlay() is True
        guarded = json.loads(cli_json.read_text(encoding="utf-8"))
        assert guarded["chat.modelDefaults"][self._MODEL]["output_config"] == {"effort": "max"}
        assert guarded["chat.modelDefaults"][self._MODEL]["reasoning"] == {"effort": "high"}

        assert provider._apply_effort_overlay(replace_unowned=True) is True
        data = json.loads(cli_json.read_text(encoding="utf-8"))
        entry = data.get("chat.modelDefaults", {}).get(self._MODEL, {})
        assert "effort" not in entry.get("output_config", {})
        assert "effort" not in entry.get("reasoning", {})
        assert data["other"] == 1

    def test_a_resolved_level_replaces_an_operator_entry(self, tmp_path):
        cli_json = self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, effort_per_model={self._MODEL: "high"})

        assert provider._apply_effort_overlay(replace_unowned=True) is True

        assert _read_cli_overlay(tmp_path) == {self._MODEL: "high"}
        data = json.loads(cli_json.read_text(encoding="utf-8"))
        assert data[_KIROCREW_EFFORT_OWNED_KEY] == {self._MODEL: "high"}

    def test_the_auto_model_keeps_the_ownership_guard(self, tmp_path):
        # Under auto no concrete model is known before kiro-cli reads the file,
        # so, as for a live auto session, only entries Kiro Crew recorded go.
        owned_model = "claude-sonnet-4.6"
        stamp = 1_700_000_000
        self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    owned_model: {"output_config": {"effort": "low"}},
                    self._MODEL: {"output_config": {"effort": "max"}},
                },
                _KIROCREW_EFFORT_OWNED_KEY: {owned_model: "low"},
                "kirocrew.effortOwnedStamp": stamp,
            },
        )
        os.utime(tmp_path / ".kiro" / "settings" / "cli.json", (stamp, stamp))
        provider = self._provider(tmp_path, model="auto")

        assert provider._apply_effort_overlay(replace_unowned=True) is True
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}

    @pytest.mark.parametrize(
        ("model", "backend"),
        [("claude-haiku-4.5", ACP_BACKEND_KIRO), ("claude-opus-4.7", "claude")],
        ids=["model-without-effort", "backend-that-reads-no-overlay"],
    )
    def test_nothing_to_remove_counts_as_applied(self, tmp_path, model, backend):
        cli_json = self._operator_entry(tmp_path, "max", model=model)
        before = cli_json.read_bytes()
        provider = self._provider(tmp_path, model=model, acp_backend=backend)

        assert provider._apply_effort_overlay(replace_unowned=True) is True
        assert cli_json.read_bytes() == before
