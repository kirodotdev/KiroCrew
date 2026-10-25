"""Native Windows Crew rollouts stay private and continuable across runtimes."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kiro_crew import platform_compat, security
from kiro_crew.acp import client
from kiro_crew.acp.codex_home import configure_codex_broker, prepare_codex_home
from kiro_crew.acp.harness import SpawnContext
from kiro_crew.acp.harness import codex as codex_mod
from kiro_crew.acp.harness.codex import CodexHarness
from kiro_crew.agent_sdk.backends import ACP_BACKEND_PI
from kiro_crew.agent_sdk.tool_gate import adapter_hidden_credential_dirs
from kiro_crew.config.paths import data_home

CREW_ID = "11111111-1111-4111-8111-111111111111"
DESKTOP_ID = "22222222-2222-4222-8222-222222222222"


def _rollout(home: Path, sid: str) -> Path:
    path = home / f"sessions/2026/01/01/rollout-2026-01-01T00-00-00-{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": sid}})
        + "\n"
        + json.dumps({"type": "response_item", "payload": {"text": "continuable history"}})
        + "\n",
        encoding="utf-8",
    )
    return path


def _map(crew: Path) -> None:
    (crew / "session_map.json").write_text(
        json.dumps({"dashboard:owned": {"sid": CREW_ID, "provider": "codex"}}), "utf-8"
    )


def test_private_home_imports_only_crew_owned_rollouts_without_auth_or_desktop_index(tmp_path):
    source = tmp_path / ".codex"
    owned = _rollout(source, CREW_ID)
    desktop = _rollout(source, DESKTOP_ID)
    (source / "auth.json").write_text("synthetic auth: never copy", "utf-8")
    (source / "config.toml").write_text('model = "operator-model"\n', "utf-8")
    with closing(sqlite3.connect(source / "state_5.sqlite")) as db:
        db.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT, archived INTEGER)")
        db.executemany(
            "INSERT INTO threads VALUES (?, ?, 0)",
            [(CREW_ID, str(owned)), (DESKTOP_ID, str(desktop))],
        )
        db.commit()
    crew = data_home()
    _map(crew)
    before = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}

    target = prepare_codex_home({}, tmp_path)

    assert target == crew / "codex"
    assert (target / owned.relative_to(source)).read_bytes() == owned.read_bytes()
    assert not (target / desktop.relative_to(source)).exists()
    assert not (target / "auth.json").exists()
    assert not (target / "state_5.sqlite").exists()
    assert (target / "config.toml").read_bytes() == (source / "config.toml").read_bytes()
    assert before == {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}


def test_profile_and_continuation_survive_repeated_preparation(tmp_path):
    source = tmp_path / ".codex"
    owned = _rollout(source, CREW_ID)
    crew = data_home()
    _map(crew)
    target = prepare_codex_home({}, tmp_path)
    assert target is not None
    private_rollout = target / owned.relative_to(source)
    private_rollout.write_text(private_rollout.read_text("utf-8") + "new private turn\n", "utf-8")
    (target / "auth.json").write_text("private independent login", "utf-8")
    (target / "config.toml").write_text("private configuration", "utf-8")
    expected = private_rollout.read_bytes()

    assert prepare_codex_home({}, tmp_path) == target
    assert private_rollout.read_bytes() == expected
    assert (target / "auth.json").read_text("utf-8") == "private independent login"
    assert (target / "config.toml").read_text("utf-8") == "private configuration"
    assert "new private turn" not in owned.read_text("utf-8")


@pytest.mark.parametrize("index", ["empty", "stale", "invalid"])
def test_crew_rollout_migration_survives_incomplete_desktop_index(tmp_path, index):
    source = tmp_path / ".codex"
    owned = _rollout(source, CREW_ID)
    _map(data_home())
    database = source / "state_5.sqlite"
    if index == "invalid":
        database.write_bytes(b"synthetic damaged index")
    else:
        with closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT, archived INTEGER)")
            if index == "stale":
                db.execute(
                    "INSERT INTO threads VALUES (?, ?, 0)", (CREW_ID, str(source / "missing.jsonl"))
                )
            db.commit()
    target = prepare_codex_home({}, tmp_path)
    assert target is not None
    assert (target / owned.relative_to(source)).read_bytes() == owned.read_bytes()


def test_explicit_home_is_left_unchanged(tmp_path):
    assert prepare_codex_home({"CODEX_HOME": str(tmp_path / "operator")}, tmp_path) is None
    assert not (data_home() / "codex").exists()


def test_invalid_ids_and_other_provider_ids_are_not_imported(tmp_path):
    source = tmp_path / ".codex"
    desktop = _rollout(source, DESKTOP_ID)
    (data_home() / "session_map.json").write_text(
        json.dumps(
            {
                "foreign": {"sid": DESKTOP_ID, "provider": "other"},
                "traversal": {"sid": "../../outside", "provider": "codex"},
            }
        ),
        "utf-8",
    )
    target = prepare_codex_home({}, tmp_path)
    assert target is not None
    assert not (target / desktop.relative_to(source)).exists()


def test_mismatching_rollout_header_is_not_imported(tmp_path):
    source = tmp_path / ".codex"
    owned = _rollout(source, CREW_ID)
    owned.write_text(json.dumps({"type": "session_meta", "payload": {"id": DESKTOP_ID}}), "utf-8")
    _map(data_home())
    target = prepare_codex_home({}, tmp_path)
    assert target is not None
    assert not (target / owned.relative_to(source)).exists()


def test_private_auth_and_rollouts_are_fenced_from_other_harnesses():
    private = data_home() / "codex"
    private.mkdir()
    assert security.is_sensitive_path(str(private / "auth.json"))
    assert security.is_sensitive_path(str(private / "auth.json.tmp"))
    assert security.is_sensitive_path(str(private / "sessions/rollout.jsonl"))
    assert str(private) in adapter_hidden_credential_dirs(ACP_BACKEND_PI)


def test_explicit_setup_routes_host_tools_without_changing_sharing_or_unrelated_choices():
    from kiro_crew.config.loader import config_path

    path = config_path()
    path.write_text(
        json.dumps(
            {
                "mcp_gateway": {
                    "enabled": False,
                    "stub_servers": ["operator-server"],
                    "stub_overrides": {"other-server": False},
                },
                "agent": {"acp_backend": "codex"},
            }
        ),
        "utf-8",
    )

    configure_codex_broker()

    configured = json.loads(path.read_text("utf-8"))
    assert configured["mcp_gateway"]["enabled"] is False
    assert configured["mcp_gateway"]["stub_servers"] == ["operator-server"]
    assert configured["mcp_gateway"]["stub_overrides"] == {
        "other-server": False,
        "kirocrew-core": True,
        "kirocrew-cron": True,
    }
    assert configured["agent"] == {"acp_backend": "codex"}


@pytest.mark.asyncio
async def test_native_windows_plan_binds_storage_environment_to_exact_visibility_lift(
    monkeypatch, tmp_path
):
    private = data_home() / "codex"
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(client, "_resolve_codex_acp_bin", lambda: (["node", "adapter.js"], ""))
    monkeypatch.setattr(codex_mod, "_sandbox_wrapper_generations", lambda mode: 0)
    monkeypatch.setattr(
        codex_mod,
        "resolve_spawn_masks",
        AsyncMock(return_value=((str(private), "other-secret"), ())),
    )
    plan = await CodexHarness().resolve_spawn(
        SpawnContext(
            agent="",
            work_dir=tmp_path,
            model=None,
            environ={},
            home=tmp_path,
        )
    )
    assert plan.env_defaults == {"CODEX_HOME": str(private)}
    assert plan.extra_visible_dirs == (str(private),)
    assert plan.extra_hidden_dirs == ("other-secret",)


@pytest.mark.asyncio
async def test_other_platforms_keep_existing_profile_behavior(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(client, "_resolve_codex_acp_bin", lambda: (["node", "adapter.js"], ""))
    monkeypatch.setattr(codex_mod, "_sandbox_wrapper_generations", lambda mode: 0)
    monkeypatch.setattr(codex_mod, "resolve_spawn_masks", AsyncMock(return_value=((), ())))
    plan = await CodexHarness().resolve_spawn(
        SpawnContext(
            agent="",
            work_dir=tmp_path,
            model=None,
            environ={},
            home=tmp_path,
        )
    )
    assert plan.env_defaults == {}
    assert plan.extra_visible_dirs == ()
    assert not (data_home() / "codex").exists()
