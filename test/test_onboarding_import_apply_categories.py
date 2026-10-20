"""What ``apply_import`` writes for every category except instructions, and when it refuses.

``TestApply`` pins config and MCP writes (a corrupt destination fails closed with its bytes
unchanged, a name, scope or edition collision is a conflict, a secret-bearing server is
dropped whole, the shared dashboard lock is taken), workspace merges, and predecessor
memories written through the native vector store. ``TestApplySchedules`` pins imported
schedules: disabled, deduplicated, timezone-preserving, and refused when a field is unknown,
the prompt carries a secret or the job's meaning cannot be kept. ``TestApplySkills`` pins
skill packages: namespaced, copied whole, and refused through a symlink, a reparse point, a
capped traversal or a failed write. ``TestTransitiveReimport`` pins that skills Hermes
imported from another agent are not offered through Hermes, so a shared skill installs once,
from its original source. The preview classes live in ``test_onboarding_import.py`` and
``test_onboarding_import_preview_predecessor_claude_hermes.py``; apply's merge safety in
``test_onboarding_import_safety_floors.py``; instruction import and conflict strategies in
``test_onboarding_import_instructions_and_conflicts.py``.
"""

from __future__ import annotations

import importlib
import json
import math
import sqlite3
import stat
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from onboarding_import_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _categories,
    _lineage_source,
    _register_predecessor_source,
    _select,
    _write_lineage_memory_db,
)

from kiro_crew import onboarding_apply, onboarding_scan
from kiro_crew.cron import CronService, CronStoreUnreadable
from kiro_crew.onboarding_sources import hermes as hermes_source
from kiro_crew.vector_memory import VectorMemoryStore

# Keep apply_import's MCP sidecar lock off the real ~/.kiro/settings (see the
# shared fixture in test/conftest.py).
pytestmark = pytest.mark.usefixtures("isolate_mcp_host_paths")


def _api() -> ModuleType:
    try:
        return importlib.import_module("kiro_crew.onboarding_import")
    except ModuleNotFoundError:
        pytest.fail("kiro_crew.onboarding_import is not implemented")


class TestApply:
    def test_corrupt_existing_config_fails_closed_without_changing_bytes(
        self, tmp_path: Path
    ) -> None:
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "config.json").write_text(
            json.dumps({"timezone": "Europe/London"}),
            encoding="utf-8",
        )
        data_home = tmp_path / "destination"
        data_home.mkdir()
        destination = data_home / "config.json"
        original = b'{"existing": [invalid}\n'
        destination.write_bytes(original)
        plan = _select(
            _api().preview_import(home=tmp_path / "home", env={}),
            ("predecessor", "settings"),
        )

        result = _api().apply_import(plan, data_home=data_home)

        assert destination.read_bytes() == original
        assert result["imported"]["settings"] == 0
        assert result["item_outcomes"][0]["outcome"] == "rejected"
        assert not (data_home / "imports" / "foreign-agent-imports.json").exists()

    def test_corrupt_existing_mcp_config_fails_closed_without_changing_bytes(
        self, tmp_path: Path
    ) -> None:
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "safe-local": {
                            "command": "safe-mcp",
                            "args": ["serve"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        data_home = tmp_path / "destination"
        data_home.mkdir()
        destination = data_home / "mcp.json"
        original = b'{"mcpServers": {"existing": invalid}}\n'
        destination.write_bytes(original)
        plan = _select(
            _api().preview_import(home=tmp_path / "home", env={}),
            ("predecessor", "mcp_servers"),
        )

        result = _api().apply_import(plan, data_home=data_home)

        assert destination.read_bytes() == original
        assert result["imported"]["mcp_servers"] == 0
        assert result["item_outcomes"][0]["outcome"] == "rejected"
        assert not (data_home / "imports" / "foreign-agent-imports.json").exists()

    def test_mcp_import_uses_the_shared_dashboard_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api()
        mcp_handlers = importlib.import_module("kiro_crew.dashboard.handlers.mcp")
        entered: list[bool] = []

        class Lock:
            def __enter__(self) -> None:
                entered.append(True)

            def __exit__(self, *args: object) -> None:
                return None

        monkeypatch.setattr(mcp_handlers, "_get_mcp_lock_sync", lambda: Lock())
        item = api._Item(
            "predecessor",
            "mcp_servers",
            "shared",
            {
                "name": "shared",
                "spec": {"command": "safe-mcp", "disabled": True},
            },
        )

        assert (
            onboarding_apply._write_mcp(item, tmp_path / "destination", tmp_path / "home").status
            == "imported"
        )
        assert entered == [True]

    @pytest.mark.parametrize(
        ("effective_source", "existing_name", "imported_name"),
        [
            ("global", "shared", "shared"),
            ("global", "namespace/shared", "namespace-shared"),
            ("installed", "shared", "shared"),
            ("installed", "namespace/shared", "namespace-shared"),
        ],
    )
    def test_mcp_import_rejects_enabled_effective_name_collisions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        effective_source: str,
        existing_name: str,
        imported_name: str,
    ) -> None:
        home = tmp_path / "home"
        predecessor_root = home / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        imported_name: {
                            "command": "foreign-command",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        global_path = home / ".kiro" / "settings" / "mcp.json"
        installed_path = home / ".kiro" / "agents" / "kirocrew.json"
        mcp_handlers = importlib.import_module("kiro_crew.dashboard.handlers.mcp")
        monkeypatch.setattr(mcp_handlers, "_GLOBAL_MCP_JSON", global_path)
        monkeypatch.setattr(mcp_handlers, "_MCP_LOCK_PATH", global_path.with_suffix(".lock"))
        effective_path = global_path if effective_source == "global" else installed_path
        effective_path.parent.mkdir(parents=True)
        effective_config: dict[str, Any] = {
            "mcpServers": {
                existing_name: {
                    "command": "trusted-command",
                }
            }
        }
        if effective_source == "installed":
            effective_config["tools"] = [f"@{existing_name}"]
            effective_config["allowedTools"] = [f"@{existing_name}"]
        effective_path.write_text(json.dumps(effective_config), encoding="utf-8")
        original_effective = effective_path.read_bytes()
        data_home = tmp_path / "destination"
        plan = _select(
            _api().preview_import(home=home, env={}),
            ("predecessor", "mcp_servers"),
        )

        result = _api().apply_import(plan, data_home=data_home)

        assert result["imported"]["mcp_servers"] == 0
        assert result["conflicts"] == [
            {
                "source_id": "predecessor",
                "category_id": "mcp_servers",
                "reason": "destination_conflict",
                # An alias collision IS resolvable — a rename gives the user a
                # way out without shadowing the other source's server.
                "resolvable": True,
            }
        ]
        assert not (data_home / "mcp.json").exists()
        assert effective_path.read_bytes() == original_effective

    @pytest.mark.parametrize("scope_location", ["global", "agent"])
    def test_mcp_import_rejects_edition_scope_collisions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        scope_location: str,
    ) -> None:
        from kiro_crew.platform.interfaces import McpScope

        home = tmp_path / "home"
        predecessor_root = home / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "namespace-shared": {
                            "command": "foreign-command",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        scope_global = home / ".provider.json"
        scope_agent = home / ".provider" / "agent.json"
        effective_path = scope_global if scope_location == "global" else scope_agent
        effective_path.parent.mkdir(parents=True, exist_ok=True)
        effective_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "namespace/shared": {
                            "command": "trusted-command",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        mcp_discovery = importlib.import_module("kiro_crew.mcp_discovery")
        monkeypatch.setattr(
            mcp_discovery,
            "_extra_scopes",
            lambda: [McpScope("provider", scope_global, scope_agent)],
        )
        global_path = home / ".kiro" / "settings" / "mcp.json"
        mcp_handlers = importlib.import_module("kiro_crew.dashboard.handlers.mcp")
        monkeypatch.setattr(mcp_handlers, "_GLOBAL_MCP_JSON", global_path)
        monkeypatch.setattr(mcp_handlers, "_MCP_LOCK_PATH", global_path.with_suffix(".lock"))
        data_home = tmp_path / "destination"
        plan = _select(
            _api().preview_import(home=home, env={}),
            ("predecessor", "mcp_servers"),
        )

        result = _api().apply_import(plan, data_home=data_home)

        assert result["imported"]["mcp_servers"] == 0
        assert result["conflicts"][0]["reason"] == "destination_conflict"
        assert not (data_home / "mcp.json").exists()

    def test_mcp_import_rejects_edition_provided_server_collision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        predecessor_root = home / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "namespace-shared": {
                            "command": "foreign-command",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        class McpTooling:
            @staticmethod
            def extra_mcp_servers() -> dict[str, dict]:
                return {"namespace/shared": {"command": "trusted-command"}}

            @staticmethod
            def extra_mcp_scopes() -> list:
                return []

        class _Provider:
            def import_sources(self):
                return [_lineage_source()]

        class Context:
            mcp_tooling = McpTooling()
            import_sources = _Provider()

        platform_context = importlib.import_module("kiro_crew.platform.context")
        monkeypatch.setattr(platform_context, "current_context", lambda: Context())
        global_path = home / ".kiro" / "settings" / "mcp.json"
        mcp_handlers = importlib.import_module("kiro_crew.dashboard.handlers.mcp")
        monkeypatch.setattr(mcp_handlers, "_GLOBAL_MCP_JSON", global_path)
        monkeypatch.setattr(mcp_handlers, "_MCP_LOCK_PATH", global_path.with_suffix(".lock"))
        data_home = tmp_path / "destination"
        plan = _select(
            _api().preview_import(home=home, env={}),
            ("predecessor", "mcp_servers"),
        )

        result = _api().apply_import(plan, data_home=data_home)

        assert result["imported"]["mcp_servers"] == 0
        assert result["conflicts"][0]["reason"] == "destination_conflict"
        assert not (data_home / "mcp.json").exists()

    @pytest.mark.parametrize(
        "installed_config",
        [
            pytest.param({"mcpServers": None}, id="null-server-map"),
            pytest.param({"mcpServers": []}, id="list-server-map"),
            pytest.param({"mcpServers": 123}, id="scalar-server-map"),
            pytest.param({"mcpServers": "invalid"}, id="string-server-map"),
            pytest.param(None, id="null-top-level"),
            pytest.param([], id="list-top-level"),
            pytest.param(123, id="scalar-top-level"),
            pytest.param("invalid", id="string-top-level"),
        ],
    )
    def test_mcp_import_tolerates_malformed_installed_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        installed_config: Any,
    ) -> None:
        home = tmp_path / "home"
        predecessor_root = home / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "mcp.json").write_text(
            json.dumps({"mcpServers": {"new-server": {"command": "safe-command"}}}),
            encoding="utf-8",
        )
        installed_path = home / ".kiro" / "agents" / "kirocrew.json"
        installed_path.parent.mkdir(parents=True)
        installed_path.write_text(json.dumps(installed_config), encoding="utf-8")
        global_path = home / ".kiro" / "settings" / "mcp.json"
        mcp_handlers = importlib.import_module("kiro_crew.dashboard.handlers.mcp")
        monkeypatch.setattr(mcp_handlers, "_GLOBAL_MCP_JSON", global_path)
        monkeypatch.setattr(mcp_handlers, "_MCP_LOCK_PATH", global_path.with_suffix(".lock"))
        data_home = tmp_path / "destination"
        plan = _select(
            _api().preview_import(home=home, env={}),
            ("predecessor", "mcp_servers"),
        )

        result = _api().apply_import(plan, data_home=data_home)

        assert result["imported"]["mcp_servers"] == 1
        assert (
            json.loads((data_home / "mcp.json").read_text(encoding="utf-8"))["mcpServers"][
                "new-server"
            ]["disabled"]
            is True
        )

    def test_mcp_secret_fields_reject_the_entire_server_definition(self, tmp_path: Path) -> None:
        secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "safe-local": {
                            "command": "safe-mcp",
                            "args": ["serve"],
                        },
                        "env-local": {
                            "command": "env-mcp",
                            "env": {"API_TOKEN": secret},
                        },
                        "safe-remote": {
                            "url": "https://mcp.example.test/api",
                        },
                        "header-remote": {
                            "url": "https://header.example.test/api",
                            "headers": {"Authorization": f"Bearer {secret}"},
                        },
                        "credential-local": {
                            "command": "credential-mcp",
                            "credentials": {"token": secret},
                        },
                        "kirocrew-core": {"command": "foreign-managed"},
                    }
                }
            ),
            encoding="utf-8",
        )
        data_home = tmp_path / "destination"
        plan = _select(
            _api().preview_import(home=tmp_path / "home", env={}),
            ("predecessor", "mcp_servers"),
        )

        result = _api().apply_import(plan, data_home=data_home)
        written = json.loads((data_home / "mcp.json").read_text(encoding="utf-8"))
        serialized = json.dumps(written)

        assert written["mcpServers"]["safe-local"] == {
            "command": "safe-mcp",
            "args": ["serve"],
            "disabled": True,
        }
        assert written["mcpServers"]["safe-remote"] == {
            "url": "https://mcp.example.test/api",
            "disabled": True,
        }
        assert "env-local" not in written["mcpServers"]
        assert "header-remote" not in written["mcpServers"]
        assert "credential-local" not in written["mcpServers"]
        assert "kirocrew-core" not in written["mcpServers"]
        assert "env" not in serialized
        assert "headers" not in serialized
        assert "credentials" not in serialized
        assert secret not in serialized
        assert result["secret_count"] >= 3
        assert any(
            item["source_id"] == "predecessor"
            and item["category_id"] == "mcp_servers"
            and item["reason"] == "credential_bearing_server"
            for item in result["skipped"]
        )

    def test_workspaces_merge_into_config_and_invalid_paths_are_not_offered(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        predecessor_root = home / ".predecessor"
        predecessor_root.mkdir(parents=True)
        workspace = tmp_path / "customer-project"
        workspace.mkdir()
        missing_workspace = tmp_path / "missing-project"
        (predecessor_root / "recent_projects.json").write_text(
            json.dumps([str(workspace), str(missing_workspace)]),
            encoding="utf-8",
        )
        data_home = tmp_path / "destination"
        data_home.mkdir()
        (data_home / "config.json").write_text(
            json.dumps({"workspaces": {"default": {"dir": "workspace"}}}),
            encoding="utf-8",
        )
        plan = _select(
            _api().preview_import(home=home, env={}),
            ("predecessor", "workspaces"),
        )

        result = _api().apply_import(plan, data_home=data_home)
        config = json.loads((data_home / "config.json").read_text(encoding="utf-8"))

        assert _categories(_api().preview_import(home=home, env={}), "predecessor") == {
            "workspaces": 1
        }
        assert config["workspaces"] == {
            "default": {"dir": "workspace"},
            "customer-project": {"dir": str(workspace.resolve())},
        }
        assert not (data_home / "recent_projects.json").exists()
        assert result["imported"]["workspaces"] == 1

    def test_predecessor_vector_memories_use_native_store_and_are_idempotent(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        _write_lineage_memory_db(home / ".predecessor" / "memory.db")
        data_home = tmp_path / "destination"
        vector_store = VectorMemoryStore(db_path=data_home / "memory.db")
        vector_store.init()
        plan = _select(
            _api().preview_import(home=home, env={}),
            ("predecessor", "memories"),
        )

        try:
            first = _api().apply_import(
                plan,
                data_home=data_home,
                vector_store=vector_store,
            )
            second = _api().apply_import(
                plan,
                data_home=data_home,
                vector_store=vector_store,
            )
            semantic = vector_store.get_semantic("pref.editor")
            episodic = vector_store.get_episodic_list()
        finally:
            vector_store.close()

        assert semantic is not None
        assert json.loads(semantic["value_json"]) == "vim"
        assert [entry["text"] for entry in episodic] == ["Remember the dashboard uses port 6777."]
        assert first["imported"]["memories"] == 2
        assert second["imported"]["memories"] == 0
        assert second["already_imported"] >= 2

    def test_fallback_vector_store_uses_native_embedding_callable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api()
        _write_lineage_memory_db(tmp_path / "home" / ".predecessor" / "memory.db")
        calls: list[str] = []

        def fake_make_sync_embed_fn() -> object:
            calls.append("created")
            return lambda _text: [0.25] * 1024

        monkeypatch.setattr(api, "make_sync_embed_fn", fake_make_sync_embed_fn)
        plan = _select(
            api.preview_import(home=tmp_path / "home", env={}),
            ("predecessor", "memories"),
        )

        result = api.apply_import(plan, data_home=tmp_path / "destination")
        with sqlite3.connect(tmp_path / "destination" / "memory.db") as connection:
            (embedding,) = connection.execute(
                "SELECT embedding FROM episodic_memories WHERE text = ?",
                ("Remember the dashboard uses port 6777.",),
            ).fetchone()

        assert result["imported"]["memories"] == 2
        assert calls == ["created"]
        assert embedding is not None


class TestApplySchedules:
    """Imported schedules land disabled, deduplicated and with their timezone kept.

    A job with an unknown field, a secret-bearing prompt or semantics the import cannot keep
    is refused.
    """

    def test_imported_schedules_are_disabled_and_not_duplicated(self, tmp_path: Path) -> None:
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "crons.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "source-id",
                            "name": "morning summary",
                            "message": "summarize yesterday",
                            "enabled": True,
                            "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        data_home = tmp_path / "destination"
        cron_service = CronService(base_dir=data_home)
        plan = _select(
            _api().preview_import(home=tmp_path / "home", env={}),
            ("predecessor", "schedules"),
        )

        first = _api().apply_import(
            plan,
            data_home=data_home,
            cron_service=cron_service,
        )
        second = _api().apply_import(
            plan,
            data_home=data_home,
            cron_service=cron_service,
        )
        jobs = cron_service.list_jobs(include_disabled=True)

        assert len(jobs) == 1
        assert jobs[0].name == "morning summary"
        assert jobs[0].enabled is False
        assert jobs[0].user_paused is True
        assert first["imported"]["schedules"] == 1
        assert second["imported"]["schedules"] == 0

    def test_an_unreadable_cron_store_rejects_the_item_instead_of_failing_the_apply(
        self, tmp_path: Path
    ) -> None:
        """A refused cron write must land in the per-item handler, not escape.

        ``apply_import``'s per-item arm catches ``(OSError, ValueError, TypeError,
        sqlite3.Error)``. A refusal raised outside that tuple escapes the whole
        apply, so one corrupt ``crons.json`` fails the entire onboarding request
        rather than rejecting the single schedule it actually blocks -- and every
        later item in the plan is lost with it.

        The vacuous-pass trap: asserting ``schedules == 0`` alone passes in a world
        where the apply raised, because the assertion is never reached. The
        assertions that carry the coverage are that the call RETURNS at all and
        that the item is reported rejected with a reason.
        """
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "crons.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "name": "morning summary",
                            "message": "summarize yesterday",
                            "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        data_home = tmp_path / "destination"
        cron_service = CronService(base_dir=data_home)

        def _refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise CronStoreUnreadable("move the file aside")

        # Patch the instance so the refusal comes from the real entry point the
        # importer calls, rather than from a stand-in service object.
        cron_service.add_job_if_absent = _refuse  # type: ignore[method-assign]

        plan = _select(
            _api().preview_import(home=tmp_path / "home", env={}),
            ("predecessor", "schedules"),
        )

        result = _api().apply_import(plan, data_home=data_home, cron_service=cron_service)

        assert result["imported"]["schedules"] == 0
        rejected = [o for o in result["item_outcomes"] if o.get("outcome") == "rejected"]
        assert rejected, "the refused schedule must be reported as a rejected item"
        assert any(s.get("reason") == "write_failed" for s in result.get("skipped", []))

    def test_schedule_timezone_is_passed_in_initial_add_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        timezone = "America/Los_Angeles"
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "crons.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "name": "morning summary",
                            "message": "summarize yesterday",
                            "schedule": {
                                "kind": "cron",
                                "cron_expr": "0 9 * * *",
                                "timezone": timezone,
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        data_home = tmp_path / "destination"
        cron_service = CronService(base_dir=data_home)
        real_add_job = cron_service.add_job
        real_update_job = cron_service.update_job
        add_calls: list[dict[str, object]] = []
        update_calls: list[dict[str, object]] = []

        def record_add_job(*args: object, **kwargs: object):
            add_calls.append(dict(kwargs))
            return real_add_job(*args, **kwargs)

        def record_update_job(job_id: str, **kwargs: object):
            update_calls.append({"job_id": job_id, **kwargs})
            return real_update_job(job_id, **kwargs)

        monkeypatch.setattr(cron_service, "add_job", record_add_job)
        monkeypatch.setattr(cron_service, "update_job", record_update_job)
        plan = _select(
            _api().preview_import(home=tmp_path / "home", env={}),
            ("predecessor", "schedules"),
        )

        _api().apply_import(
            plan,
            data_home=data_home,
            cron_service=cron_service,
        )

        assert add_calls[0].get("timezone") == timezone
        assert update_calls == []
        assert cron_service.list_jobs(include_disabled=True)[0].timezone == timezone

    def test_string_schedule_preserves_top_level_timezone(self, tmp_path: Path) -> None:
        timezone = "America/New_York"
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "crons.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "name": "morning summary",
                            "message": "summarize yesterday",
                            "schedule": "0 9 * * *",
                            "timezone": timezone,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        data_home = tmp_path / "destination"
        cron_service = CronService(base_dir=data_home)
        plan = _select(
            _api().preview_import(home=tmp_path / "home", env={}),
            ("predecessor", "schedules"),
        )

        _api().apply_import(
            plan,
            data_home=data_home,
            cron_service=cron_service,
        )

        jobs = cron_service.list_jobs(include_disabled=True)
        assert len(jobs) == 1
        assert jobs[0].timezone == timezone

    def test_schedule_rejects_non_string_timezone(self, tmp_path: Path) -> None:
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "crons.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "name": "morning summary",
                            "message": "summarize yesterday",
                            "schedule": "0 9 * * *",
                            "timezone": 123,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        plan = _api().preview_import(home=tmp_path / "home", env={})

        assert _categories(plan, "predecessor").get("schedules", 0) == 0
        assert any(
            item["source_id"] == "predecessor"
            and item["category_id"] == "schedules"
            and item["reason"] == "invalid_timezone"
            for item in plan["skipped"]
        )

    def test_schedule_semantic_dedup_ignores_created_by(self, tmp_path: Path) -> None:
        timezone = "America/Los_Angeles"
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "crons.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "name": "morning summary",
                            "message": "summarize yesterday",
                            "schedule": {
                                "kind": "cron",
                                "cron_expr": "0 9 * * *",
                                "timezone": timezone,
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        data_home = tmp_path / "destination"
        cron_service = CronService(base_dir=data_home)
        existing = cron_service.add_job(
            name="morning summary",
            message="summarize yesterday",
            cron_expr="0 9 * * *",
            timezone=timezone,
            created_by="dashboard-owner",
        )
        plan = _select(
            _api().preview_import(home=tmp_path / "home", env={}),
            ("predecessor", "schedules"),
        )

        result = _api().apply_import(
            plan,
            data_home=data_home,
            cron_service=cron_service,
        )
        jobs = cron_service.list_jobs(include_disabled=True)

        assert [job.id for job in jobs] == [existing.id]
        assert jobs[0].created_by == "dashboard-owner"
        assert result["imported"]["schedules"] == 0
        assert result["already_imported"] == 1

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("command", ""),
            ("script", ""),
            ("env", {}),
            ("tool", ""),
            ("tools", []),
            ("toolFilter", []),
            ("tool_filter", []),
            ("cwd", ""),
            ("workingDirectory", ""),
            ("working_directory", ""),
            ("skills", []),
            ("chain", []),
            ("delivery", {}),
            ("channel", ""),
            ("repeat", False),
            ("count", 0),
            ("provider", ""),
            ("model", ""),
            ("agent", None),
            ("session", {}),
            ("approval", False),
            ("sandbox", False),
        ],
    )
    def test_schedule_rejects_fields_with_unpreserved_semantics(
        self, tmp_path: Path, field: str, value: object
    ) -> None:
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        record = {
            "name": "unsafe schedule",
            "message": "must never be narrowed",
            "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
            field: value,
        }
        (predecessor_root / "crons.json").write_text(
            json.dumps({"jobs": [record]}),
            encoding="utf-8",
        )

        plan = _api().preview_import(home=tmp_path / "home", env={})

        assert "schedules" not in _categories(plan, "predecessor")
        assert any(
            item["source_id"] == "predecessor"
            and item["category_id"] == "schedules"
            and item["reason"] == "unsupported_schedule_semantics"
            for item in plan["skipped"]
        )

    @pytest.mark.parametrize("container", ["payload", "schedule"])
    def test_schedule_rejects_nested_unpreserved_semantics(
        self, tmp_path: Path, container: str
    ) -> None:
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        record: dict[str, object] = {
            "name": "nested unsafe schedule",
            "message": "must never be narrowed",
            "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
        }
        if container == "payload":
            record["payload"] = {"channel": "foreign-channel"}
        else:
            record["schedule"] = {
                "kind": "cron",
                "cron_expr": "0 9 * * *",
                "channel": "foreign-channel",
            }
        (predecessor_root / "crons.json").write_text(
            json.dumps({"jobs": [record]}),
            encoding="utf-8",
        )

        plan = _api().preview_import(home=tmp_path / "home", env={})

        assert "schedules" not in _categories(plan, "predecessor")

    @pytest.mark.parametrize(
        ("container", "field"),
        [
            ("record", "webhook"),
            ("payload", "metadata"),
            ("schedule", "jitter"),
        ],
    )
    def test_schedule_rejects_unknown_fields(
        self, tmp_path: Path, container: str, field: str
    ) -> None:
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        record: dict[str, object] = {
            "name": "unknown semantics",
            "message": "must never be narrowed",
            "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
        }
        if container == "record":
            record[field] = {"url": "https://example.com/hook"}
        else:
            nested = record.setdefault(container, {})
            assert isinstance(nested, dict)
            nested[field] = True
        (predecessor_root / "crons.json").write_text(
            json.dumps({"jobs": [record]}),
            encoding="utf-8",
        )

        plan = _api().preview_import(home=tmp_path / "home", env={})

        assert "schedules" not in _categories(plan, "predecessor")
        assert any(
            item["source_id"] == "predecessor"
            and item["category_id"] == "schedules"
            and item["reason"] == "unsupported_schedule_semantics"
            for item in plan["skipped"]
        )

    @pytest.mark.parametrize(
        ("source_id", "schedule_path"),
        [
            ("predecessor", ".predecessor/crons.json"),
            ("openclaw", ".openclaw/cron/jobs.json"),
            ("hermes", ".hermes/cron/jobs.json"),
        ],
    )
    def test_schedule_rejects_unpreserved_semantics_across_sources(
        self, tmp_path: Path, source_id: str, schedule_path: str
    ) -> None:
        path = tmp_path / "home" / schedule_path
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "name": "foreign model schedule",
                            "message": "must never be narrowed",
                            "model": "foreign-model",
                            "schedule": {
                                "kind": "cron",
                                "cron_expr": "0 9 * * *",
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        plan = _api().preview_import(home=tmp_path / "home", env={})

        assert "schedules" not in _categories(plan, source_id)

    @pytest.mark.parametrize(
        "value",
        [0, -1, math.nan, math.inf, -math.inf],
        ids=["zero", "negative", "nan", "positive-infinity", "negative-infinity"],
    )
    def test_schedule_rejects_nonpositive_or_nonfinite_interval_values(
        self, tmp_path: Path, value: float
    ) -> None:
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "crons.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "name": "invalid interval",
                            "message": "must never be scheduled",
                            "schedule": {
                                "kind": "every",
                                "every_secs": value,
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        plan = _api().preview_import(home=tmp_path / "home", env={})

        assert "schedules" not in _categories(plan, "predecessor")
        assert not any(
            item["source_id"] == "predecessor" and item["category_id"] == "schedules"
            for item in plan["selection"]
        )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("every_secs", 1),
            ("every_secs", 0.5),
            ("every_secs", 60.5),
            ("minutes", 1.01),
            ("every_ms", 1),
            ("every_ms", 60_001),
        ],
    )
    def test_schedule_rejects_intervals_not_exactly_representable_in_seconds(
        self, tmp_path: Path, field: str, value: float
    ) -> None:
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "crons.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "name": "lossy interval",
                            "message": "must never be rounded",
                            "schedule": {"kind": "every", field: value},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        plan = _api().preview_import(home=tmp_path / "home", env={})

        assert "schedules" not in _categories(plan, "predecessor")
        assert any(
            item["source_id"] == "predecessor" and item["category_id"] == "schedules"
            for item in plan["skipped"]
        )

    def test_schedule_rejects_secret_bearing_prompt_instead_of_redacting_it(
        self, tmp_path: Path
    ) -> None:
        secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "crons.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "name": "secret schedule",
                            "message": f"query GitHub with {secret}",
                            "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        plan = _api().preview_import(home=tmp_path / "home", env={})

        assert "schedules" not in _categories(plan, "predecessor")
        assert secret not in json.dumps(plan)
        assert plan["secret_count"] >= 1
        assert any(
            item["source_id"] == "predecessor"
            and item["category_id"] == "schedules"
            and item["reason"] == "credential_bearing_schedule"
            for item in plan["skipped"]
        )

    @pytest.mark.parametrize(
        "value",
        [0, -1, math.nan, math.inf, -math.inf],
        ids=["zero", "negative", "nan", "positive-infinity", "negative-infinity"],
    )
    def test_schedule_rejects_nonpositive_or_nonfinite_at_values(
        self, tmp_path: Path, value: float
    ) -> None:
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "crons.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "name": "invalid one-shot",
                            "message": "must never be scheduled",
                            "schedule": {
                                "kind": "at",
                                "at_ts": value,
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        plan = _api().preview_import(home=tmp_path / "home", env={})

        assert "schedules" not in _categories(plan, "predecessor")
        assert not any(
            item["source_id"] == "predecessor" and item["category_id"] == "schedules"
            for item in plan["selection"]
        )

    def test_schedule_rejects_mixed_trigger_families(self, tmp_path: Path) -> None:
        predecessor_root = tmp_path / "home" / ".predecessor"
        predecessor_root.mkdir(parents=True)
        (predecessor_root / "crons.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "name": "ambiguous",
                            "message": "must not change meaning",
                            "schedule": {
                                "kind": "cron",
                                "cron_expr": "0 9 * * *",
                                "at_ts": 1_800_000_000,
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        plan = _api().preview_import(home=tmp_path / "home", env={})

        assert "schedules" not in _categories(plan, "predecessor")
        assert any(
            item["source_id"] == "predecessor"
            and item["category_id"] == "schedules"
            and item["reason"] == "ambiguous_schedule_trigger"
            for item in plan["skipped"]
        )


class TestApplySkills:
    """Imported skill packages land namespaced and whole.

    A symlink or reparse point on either side, a capped traversal or a failed write rejects
    the package without leaving a partial copy.
    """

    def test_skills_are_namespaced_and_symlinks_are_rejected(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        skills = home / ".claude" / "skills"
        real_skill = skills / "writer"
        real_skill.mkdir(parents=True)
        (real_skill / "SKILL.md").write_text("# Writer\n", encoding="utf-8")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("# Secret outside skill\n", encoding="utf-8")
        try:
            (skills / "linked-secret").symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are unavailable on this platform")
        data_home = tmp_path / "destination"
        plan = _select(
            _api().preview_import(home=home, env={}),
            ("claude_code", "skills"),
        )

        result = _api().apply_import(plan, data_home=data_home)

        assert (
            data_home / "skills" / "imported" / "claude_code" / "writer" / "SKILL.md"
        ).read_text(encoding="utf-8") == "# Writer\n"
        assert not (data_home / "skills" / "imported" / "claude_code" / "linked-secret").exists()
        assert result["imported"]["skills"] == 1
        assert any(item["reason"] == "symlink_rejected" for item in plan["skipped"])

    def test_skill_package_is_rejected_when_traversal_is_capped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(onboarding_scan, "_MAX_WALK_ENTRIES", 1)
        skill = tmp_path / "home" / ".claude" / "skills" / "review"
        (skill / "assets-a").mkdir(parents=True)
        (skill / "assets-b").mkdir()
        (skill / "SKILL.md").write_text("# Review\n", encoding="utf-8")
        (skill / "assets-a" / "a.txt").write_text("a\n", encoding="utf-8")
        (skill / "assets-b" / "b.txt").write_text("b\n", encoding="utf-8")

        scan = onboarding_scan._Scan("claude_code", tmp_path / "home", tmp_path / "home")

        assert onboarding_scan._skill_package(scan, scan.root, skill / "SKILL.md") is None
        assert any(item["reason"] == "skill_package_truncated" for item in scan.skipped)

    def test_windows_reparse_attributes_are_link_like(self) -> None:

        class ReparseStat:
            st_mode = stat.S_IFDIR
            st_file_attributes = onboarding_scan._FILE_ATTRIBUTE_REPARSE_POINT

        assert onboarding_scan._stat_is_link_like(ReparseStat())

    def test_source_reparse_component_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api()
        skill = tmp_path / "home" / ".claude" / "skills" / "junction-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Junction skill\n", encoding="utf-8")
        real_is_link_like = onboarding_scan._is_link_like

        def fake_is_link_like(path: Path, file_stat: object | None = None) -> bool:
            return path == skill or real_is_link_like(path, file_stat)

        monkeypatch.setattr(onboarding_scan, "_is_link_like", fake_is_link_like)

        plan = api.preview_import(home=tmp_path / "home", env={})

        assert "skills" not in _categories(plan, "claude_code")
        assert any(
            item["source_id"] == "claude_code"
            and item["category_id"] == "skills"
            and item["reason"] == "symlink_rejected"
            for item in plan["skipped"]
        )

    def test_skill_import_rejects_preexisting_destination_reparse_ancestor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api()
        skill = tmp_path / "home" / ".claude" / "skills" / "review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Review\n", encoding="utf-8")
        data_home = tmp_path / "destination"
        ancestor = data_home / "skills" / "imported" / "claude_code"
        ancestor.mkdir(parents=True)
        plan = _select(
            api.preview_import(home=tmp_path / "home", env={}),
            ("claude_code", "skills"),
        )
        # ``onboarding_scan`` holds the one binding the apply owner reads.
        real_is_link_like = onboarding_scan._is_link_like

        def fake_is_link_like(path: Path, file_stat: object | None = None) -> bool:
            return path == ancestor or real_is_link_like(path, file_stat)

        monkeypatch.setattr(onboarding_scan, "_is_link_like", fake_is_link_like)

        result = api.apply_import(plan, data_home=data_home)

        assert not (ancestor / "review").exists()
        assert result["imported"]["skills"] == 0
        assert result["item_outcomes"][0]["outcome"] == "rejected"

    def test_skill_import_rejects_preexisting_destination_symlink_ancestor(
        self, tmp_path: Path
    ) -> None:
        skill = tmp_path / "home" / ".claude" / "skills" / "review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Review\n", encoding="utf-8")
        data_home = tmp_path / "destination"
        ancestor = data_home / "skills" / "imported" / "claude_code"
        ancestor.parent.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        try:
            ancestor.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are unavailable on this platform")
        plan = _select(
            _api().preview_import(home=tmp_path / "home", env={}),
            ("claude_code", "skills"),
        )

        result = _api().apply_import(plan, data_home=data_home)

        assert ancestor.is_symlink()
        assert not (outside / "review").exists()
        assert result["imported"]["skills"] == 0
        assert result["item_outcomes"][0]["outcome"] == "rejected"
        assert not (data_home / "imports" / "foreign-agent-imports.json").exists()

    def test_skill_write_failure_removes_partial_package_and_skips_ledger(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api()
        skill = tmp_path / "home" / ".claude" / "skills" / "review"
        (skill / "scripts").mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Review\n", encoding="utf-8")
        (skill / "scripts" / "check.py").write_text("print('check')\n", encoding="utf-8")
        data_home = tmp_path / "destination"
        destination = data_home / "skills" / "imported" / "claude_code" / "review"
        real_write_bytes = Path.write_bytes
        package_writes = 0

        def fail_second_package_write(path: Path, content: bytes) -> int:
            nonlocal package_writes
            try:
                relative = path.relative_to(destination.parent)
            except ValueError:
                relative = None
            if relative is not None and relative.parts[0].startswith(".review.import-"):
                package_writes += 1
                if package_writes == 2:
                    raise OSError("injected package write failure")
            return real_write_bytes(path, content)

        monkeypatch.setattr(Path, "write_bytes", fail_second_package_write)
        plan = _select(
            api.preview_import(home=tmp_path / "home", env={}),
            ("claude_code", "skills"),
        )

        result = api.apply_import(plan, data_home=data_home)

        assert package_writes == 2
        assert not destination.exists()
        assert not (data_home / "imports" / "foreign-agent-imports.json").exists()
        assert result["imported"]["skills"] == 0
        assert result["item_outcomes"][0]["outcome"] == "rejected"

    def test_skill_auxiliary_package_files_are_copied_with_the_manifest(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        skill = home / ".claude" / "skills" / "review"
        (skill / "scripts").mkdir(parents=True)
        (skill / "references").mkdir()
        (skill / "SKILL.md").write_text("# Review\n", encoding="utf-8")
        (skill / "scripts" / "check.py").write_text("print('check')\n", encoding="utf-8")
        (skill / "references" / "checklist.md").write_text("- review\n", encoding="utf-8")
        plan = _select(
            _api().preview_import(home=home, env={}),
            ("claude_code", "skills"),
        )

        result = _api().apply_import(plan, data_home=tmp_path / "destination")
        destination = tmp_path / "destination" / "skills" / "imported" / "claude_code" / "review"

        assert (destination / "SKILL.md").read_text(encoding="utf-8") == "# Review\n"
        assert (destination / "scripts" / "check.py").read_text(
            encoding="utf-8"
        ) == "print('check')\n"
        assert (destination / "references" / "checklist.md").read_text(
            encoding="utf-8"
        ) == "- review\n"
        assert result["imported"]["skills"] == 1

    def test_clean_skill_assets_preserve_large_content_and_whitespace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        skill = home / ".claude" / "skills" / "review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Review\n", encoding="utf-8")
        original = b" \r\n" + (b"clean package content\n" * 5_001) + b"\r\n "
        assert len(original) > 100_000
        (skill / "reference.txt").write_bytes(original)
        plan = _select(
            _api().preview_import(home=home, env={}),
            ("claude_code", "skills"),
        )
        atomic_write_module = importlib.import_module("kiro_crew.atomic_write")
        real_fdopen = atomic_write_module.os.fdopen

        def windows_fdopen(fd: int, *args: Any, **kwargs: Any):
            mode = str(args[0]) if args else str(kwargs.get("mode", "r"))
            if "b" not in mode and kwargs.get("newline") is None:
                kwargs["newline"] = "\r\n"
            return real_fdopen(fd, *args, **kwargs)

        monkeypatch.setattr(atomic_write_module.os, "fdopen", windows_fdopen)

        result = _api().apply_import(plan, data_home=tmp_path / "destination")
        destination = (
            tmp_path
            / "destination"
            / "skills"
            / "imported"
            / "claude_code"
            / "review"
            / "reference.txt"
        )

        assert destination.read_bytes() == original
        assert result["imported"]["skills"] == 1


class TestTransitiveReimport:
    """A source's OWN importer output must not be re-imported through it.

    Hermes ships `hermes import-agent` / `hermes claw migrate`, which write
    foreign skills into `skills/<source>-imports/`. Importing those from Hermes
    duplicates what the original source already contributes, and NEITHER dedupe
    layer catches it: the fingerprint is source-scoped and the destination dirs
    differ, so not even a conflict is reported.
    """

    def test_hermes_reimport_dirs_are_excluded(self, tmp_path: Path) -> None:
        api = _api()
        home = tmp_path / "home"
        hermes = home / ".hermes"
        # Hermes's own skill, plus three it imported from other agents.
        own = hermes / "skills" / "hermes-own"
        own.mkdir(parents=True)
        (own / "SKILL.md").write_text("# Hermes own\n", encoding="utf-8")
        for directory in hermes_source._FOREIGN_REIMPORT_SKILL_DIRS:
            imported = hermes / "skills" / directory / "foo"
            imported.mkdir(parents=True)
            (imported / "SKILL.md").write_text("# Foo\n", encoding="utf-8")

        plan = api.preview_import(home=home, env={})

        # Only Hermes's own skill is offered.
        assert _categories(plan, "hermes") == {"skills": 1}
        destination = tmp_path / "destination"
        api.apply_import(plan, data_home=destination)
        installed = {
            path.parent.name for path in (destination / "skills" / "imported").rglob("SKILL.md")
        }
        assert installed == {"hermes-own"}

    def test_the_original_source_still_imports_the_shared_skill(self, tmp_path: Path) -> None:
        api = _api()
        home = tmp_path / "home"
        # Same skill present in Claude Code (the original) and in Hermes's
        # claude-code-imports/ (Hermes's copy). Exactly one must be imported.
        original = home / ".claude" / "skills" / "foo"
        original.mkdir(parents=True)
        (original / "SKILL.md").write_text("# Foo\n", encoding="utf-8")
        copied = home / ".hermes" / "skills" / "claude-code-imports" / "foo"
        copied.mkdir(parents=True)
        (copied / "SKILL.md").write_text("# Foo\n", encoding="utf-8")

        plan = api.preview_import(home=home, env={})
        destination = tmp_path / "destination"
        api.apply_import(plan, data_home=destination)

        skill_dirs = sorted(
            path.parent.relative_to(destination / "skills" / "imported").as_posix()
            for path in (destination / "skills" / "imported").rglob("SKILL.md")
        )
        assert skill_dirs == ["claude_code/foo"]
