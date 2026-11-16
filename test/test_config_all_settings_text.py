"""Settings -> Advanced: the free-text keys the page may change, and the check each runs."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.schema import SCHEMA_REGISTRY
from kiro_crew.dashboard.handlers import config_all_settings as all_settings

_BY_PATH = {e.path: e for e in SCHEMA_REGISTRY}

pytestmark = pytest.mark.asyncio


@web.middleware
async def _owner_identity(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    request.app["state"].owner_id = ""
    return await handler(request)


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application(middlewares=[_owner_identity])
    app["state"] = MagicMock()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return app


@pytest.fixture
def tmp_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        yield cfg_path


def _stored_at(cfg_path, path: str) -> object:
    node: object = json.loads(cfg_path.read_text(encoding="utf-8"))
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


async def _send(body: dict) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.patch("/api/config/kirocrew", json=body)
        return resp.status, await resp.json()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        # A path must be absolute once ~ is expanded.
        ("dashboard.default_project", "relative/dir"),
        ("taskrunner.workspace_dir", "work"),
        ("snapshot_dir", "line\nbreak"),
        # A URL must carry its reader's scheme and a host, and no user name or password.
        ("dashboard.url", "ftp://crew.example.com"),
        ("dashboard.url", "https://me:pw@crew.example.com"),
        ("dashboard.url", "https://"),
        ("agent.kas_bundle_url", "http://mirror.example.com"),
        ("memory.embed_model_url", "http://mirror.example.com/model.gguf"),
        ("memory.ollama_url", "http://ollama.example.com:11434"),
        ("memory.ollama_url", "http://127.0.0.1:11434/?x=1"),
        # A name is one line; a looked-up name is never empty.
        ("agent.bot_name", "two\nlines"),
        ("default_workspace", ""),
        ("agent.member_branch_prefix", "a..b/"),
        ("agent.member_branch_prefix", "has space/"),
        ("agent.member_acp_backend", "no-such-harness"),
        ("memory.semantic_keys", ["ok", "bad\nitem"]),
        ("taskrunner.test_command", ["python", ""]),
        ("work_ledger.remote_dispatch_tags", ["gpu", "host.example.com"]),
        ("memory.extraction_instructions", "x" * 2001),
    ],
)
async def test_patch_refuses_a_text_value_its_reader_cannot_use(tmp_config, path, value) -> None:
    assert all_settings.descriptor(_BY_PATH[path])["mode"] == "edit"
    status, body = await _send({"path": path, "value": value})
    assert status == 400, body
    assert isinstance(body.get("error"), str) and body["error"]
    assert _stored_at(tmp_config, path) is None


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("dashboard.terminal.cwd", ""),
        ("dashboard.url", "https://crew.example.com"),
        ("dashboard.url", "http://127.0.0.1:7777"),
        ("agent.kas_bundle_url", "https://mirror.example.com"),
        ("memory.ollama_url", "http://127.0.0.1:11434"),
        ("memory.ollama_url", "https://ollama.example.com"),
        ("agent.bot_name", "Pip"),
        ("agent.member_branch_prefix", "alice/"),
        ("agent.member_acp_backend", ""),
        ("taskrunner.test_command", ["python", "-m", "pytest", "-q"]),
        ("work_ledger.remote_dispatch_tags", ["gpu", "docker"]),
        ("memory.extraction_instructions", "keep build commands\nnever store names"),
        ("memory.extraction_instructions", "x" * 2000),
    ],
)
async def test_patch_stores_a_text_value_its_reader_can_use(tmp_config, path, value) -> None:
    status, body = await _send({"path": path, "value": value})
    assert status == 200, body
    assert _stored_at(tmp_config, path) == value


async def test_a_folder_key_refuses_an_existing_file(tmp_config, tmp_path) -> None:
    """A terminal handed a file as its start folder fails to open."""
    a_file = tmp_path / "notes.txt"
    a_file.write_text("x", encoding="utf-8")
    status, body = await _send({"path": "dashboard.terminal.cwd", "value": str(a_file)})
    assert (status, body.get("error")) == (400, "must be a folder, not a file")
    assert _stored_at(tmp_config, "dashboard.terminal.cwd") is None
    status, body = await _send({"path": "dashboard.terminal.cwd", "value": str(tmp_path)})
    assert status == 200, body


def test_the_decisions_model_links_to_its_own_page() -> None:
    """``PUT /api/decisions/provider`` writes the model with its endpoint; a model
    saved alone would split the pair the decision gate reads."""
    assert all_settings.descriptor(_BY_PATH["decisions.provider.model"]) == {
        "mode": "control",
        "page": {"tab": "developer", "sub": "feature-previews", "highlight": "developer.decisions"},
    }
    assert all_settings.generic_spec("decisions.provider.model") is None


async def test_a_tilde_path_is_saved_expanded(tmp_config, tmp_path, monkeypatch) -> None:
    """A terminal's start folder reaches the process as written, so ``~`` is expanded
    once, here, against this gateway's home."""
    import os

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    status, body = await _send({"path": "dashboard.terminal.cwd", "value": "~/work"})
    assert status == 200, body
    stored = _stored_at(tmp_config, "dashboard.terminal.cwd")
    assert stored == os.path.expanduser("~/work") and "~" not in str(stored)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("memory.semantic_keys", ["ok", 5]),
        ("taskrunner.test_command", ["python", None]),
        ("mcp_gateway.keep_alive_servers", [{"name": "github"}]),
    ],
)
async def test_patch_refuses_a_list_item_that_is_not_text(tmp_config, path, value) -> None:
    """The schema check reads the list, not its items; a non-text item is a 400, not a 500."""
    status, body = await _send({"path": path, "value": value})
    assert status == 400, body
    assert body["error"] == "each item must be text"
    assert _stored_at(tmp_config, path) is None


async def test_an_absolute_path_saves_and_the_loader_reads_it_back(tmp_config, tmp_path) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    work = str(tmp_path / "work")
    status, body = await _send({"path": "taskrunner.workspace_dir", "value": work})
    assert status == 200, body
    status, body = await _send(
        {"path": "taskrunner.test_command", "value": ["python", "-m", "pytest"]}
    )
    assert status == 200, body
    status, body = await _send({"path": "agent.member_branch_prefix", "value": "alice/"})
    assert status == 200, body

    cfg = KiroCrewConfig.load()
    assert cfg.taskrunner.workspace_dir == work
    assert cfg.taskrunner.test_command == ["python", "-m", "pytest"]
    assert cfg.agent.member_branch_prefix == "alice/"

    status, body = await _send({"path": "taskrunner.workspace_dir", "unset": True})
    assert status == 200, body
    assert _stored_at(tmp_config, "taskrunner.workspace_dir") is None


def test_the_member_backend_check_takes_every_harness_this_build_offers() -> None:
    from kiro_crew.agent_sdk.backends import selectable_stored_backend_values

    for backend in selectable_stored_backend_values():
        assert all_settings.generic_value_error("agent.member_acp_backend", backend) is None


def test_the_long_text_key_gets_its_readers_length() -> None:
    from kiro_crew.config.memory_sections import EXTRACTION_INSTRUCTIONS_MAX_CHARS

    record = all_settings.descriptor(_BY_PATH["memory.extraction_instructions"])
    assert record["max_len"] == EXTRACTION_INSTRUCTIONS_MAX_CHARS


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("memory.embed_model_id", all_settings.WHY_OWNED),
        ("agent.apps_trusted_local", all_settings.WHY_OWNED),
        ("memory.embed_rebuild_generation", all_settings.WHY_MARKER),
        ("memory.embed_model_legacy_ids", all_settings.WHY_MARKER),
        ("agent.apps_default_installs_applied", all_settings.WHY_MARKER),
        ("slack_dm_activation", all_settings.WHY_ALIAS),
        ("mcp_gateway.poolable_servers", all_settings.WHY_ALIAS),
        # Another route writes these with its own rule.
        ("default_agent", all_settings.WHY_OWNED),
        ("mcp_gateway.stub_servers", all_settings.WHY_OWNED),
    ],
)
async def test_text_another_step_owns_stays_read_only(tmp_config, path, reason) -> None:
    assert all_settings.descriptor(_BY_PATH[path]) == {"mode": "readonly", "reason": reason}
    assert all_settings.generic_spec(path) is None
    value: object = [] if _BY_PATH[path].type == "array" else "x"
    status, body = await _send({"path": path, "value": value})
    assert status == 400, body
