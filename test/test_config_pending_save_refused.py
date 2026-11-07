"""An ordinary save of a file with a pending change is refused; other files still save.

Where the namespace sandbox seals the keystone files, a file that differs from its
endorsed copy is pending (``config.loosening_provenance``). An ordinary save reads the
endorsed copy, so publishing it would replace the pending on-disk edit with nothing
keeping it. Each such save is refused with one plain sentence and a stable code, the
file stays pending, and a save to a DIFFERENT file goes through. Apply and Discard
(``test_config_pending.py``) are the only writers allowed to touch a pending file.

The conftest pins ``KIROCREW_HOME`` per test, so ``config_dir()`` is a fresh temp dir.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config import loosening_provenance as lp
from kiro_crew.config.paths import config_dir

_HAND_EDIT = (json.dumps({"agent": {"bot_name": "Hand"}}) + "\n").encode()


@pytest.fixture(autouse=True)
def enforcing(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(lp, "enforcement_active", lambda: True)


def _endorse(leaf: str, doc: dict) -> Path:
    path = config_dir() / leaf
    content = (json.dumps(doc) + "\n").encode()
    lp.record(leaf, content, strict=True)
    path.write_bytes(content)
    return path


def _hand_edit(leaf: str, doc: dict) -> Path:
    """*leaf* endorsed as *doc*, then changed outside Kiro Crew: pending."""
    path = _endorse(leaf, doc)
    path.write_bytes(_HAND_EDIT)
    assert lp.leaf_pending(leaf)
    return path


def _pending_leaves() -> list[str]:
    return [entry["leaf"] for entry in lp.pending_changes()]


@web.middleware
async def _owner_identity(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    return await handler(request)


def _patch_client() -> TestClient:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application(middlewares=[_owner_identity])
    app["state"] = SimpleNamespace(owner_id="", subagents=MagicMock())
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return TestClient(TestServer(app))


async def _patch_reuse_terminal(client: TestClient):
    return await client.patch(
        "/api/config/kirocrew",
        json={"path": "dashboard.terminal.reuse_current", "value": True},
    )


@pytest.mark.asyncio
async def test_a_settings_save_of_a_pending_config_json_is_refused_and_keeps_the_edit() -> None:
    path = _hand_edit(lp.LEAF_CONFIG, {"agent": {"bot_name": "Kiro"}})

    async with _patch_client() as client:
        resp = await _patch_reuse_terminal(client)
        body = await resp.json()

    assert resp.status == 400
    assert body == {"error": lp.PENDING_SAVE_REFUSED, "code": "file_pending_review"}
    assert path.read_bytes() == _HAND_EDIT
    assert _pending_leaves() == [lp.LEAF_CONFIG]


@pytest.mark.asyncio
async def test_a_settings_save_of_config_json_works_while_another_file_is_pending() -> None:
    config = _endorse(lp.LEAF_CONFIG, {"agent": {"bot_name": "Kiro"}})
    denied = _hand_edit(lp.LEAF_DENIED_COMMANDS, {"disabled_ids": []})

    async with _patch_client() as client:
        resp = await _patch_reuse_terminal(client)

    assert resp.status == 200
    assert json.loads(config.read_bytes())["dashboard"]["terminal"]["reuse_current"] is True
    assert denied.read_bytes() == _HAND_EDIT
    assert _pending_leaves() == [lp.LEAF_DENIED_COMMANDS]


@pytest.mark.asyncio
async def test_a_denied_command_save_of_a_pending_file_is_refused_and_keeps_the_edit() -> None:
    from kiro_crew.dashboard.handlers.security import api_denied_command_builtin_toggle
    from kiro_crew.security import builtin_denied_rules, floor_enforced_builtin_command_ids

    path = _hand_edit(lp.LEAF_DENIED_COMMANDS, {"disabled_ids": []})
    floor = floor_enforced_builtin_command_ids()
    rid = next(r["id"] for r in builtin_denied_rules() if r["id"] not in floor)
    app = web.Application()
    app.router.add_patch(
        "/api/security/denied-commands/builtins/{id}", api_denied_command_builtin_toggle
    )
    with patch("kiro_crew.dashboard.handlers.security._sel"):
        async with TestClient(TestServer(as_owner(app))) as client:
            resp = await client.patch(
                f"/api/security/denied-commands/builtins/{rid}", json={"enabled": False}
            )
            body = await resp.json()

    assert resp.status == 409
    assert body == {"error": lp.PENDING_SAVE_REFUSED, "code": "file_pending_review"}
    assert path.read_bytes() == _HAND_EDIT
    assert _pending_leaves() == [lp.LEAF_DENIED_COMMANDS]


def _config_set(local: bool = False) -> None:
    from kiro_crew.cli_config import _config_cmd

    with patch("kiro_crew.cli_config.sel"):
        _config_cmd(
            argparse.Namespace(
                config_action="set", key="agent.bot_name", value="Nova", file=None, local=local
            )
        )


def test_config_set_of_a_pending_config_json_is_refused_and_keeps_the_edit(capsys) -> None:
    path = _hand_edit(lp.LEAF_CONFIG, {"agent": {"bot_name": "Kiro"}})

    with pytest.raises(SystemExit) as exc:
        _config_set()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert lp.PENDING_SAVE_REFUSED in err
    assert "Traceback" not in err
    assert path.read_bytes() == _HAND_EDIT
    assert _pending_leaves() == [lp.LEAF_CONFIG]


def test_config_set_local_works_while_config_json_is_pending() -> None:
    config = _hand_edit(lp.LEAF_CONFIG, {"agent": {"bot_name": "Kiro"}})

    _config_set(local=True)

    local = json.loads((config_dir() / lp.LEAF_CONFIG_LOCAL).read_text(encoding="utf-8"))
    assert local["agent"]["bot_name"] == "Nova"
    assert config.read_bytes() == _HAND_EDIT
    assert _pending_leaves() == [lp.LEAF_CONFIG]


def _spa_fallback() -> object:
    """The dashboard chain's own ``spa_fallback`` layer, built by its installer."""
    from kiro_crew.dashboard import server

    @web.middleware
    async def _through(request, handler):
        return await handler(request)

    chain = web.Application()
    server._install_dashboard_middlewares(
        chain,
        deny_audit_middleware=_through,
        host_canonical_redirect=_through,
        host_validation_middleware=_through,
        csrf_middleware=_through,
        internal_secret="per-boot",
        port=25300,
        local_only=True,
        tailnet_trust=None,
        tailnet_host="",
        configured_host="127.0.0.1",
        dashboard_url="",
    )
    return next(mw for mw in chain.middlewares if mw.__name__ == "spa_fallback")


@pytest.mark.asyncio
async def test_a_save_handler_with_no_own_catch_is_answered_409_by_the_dashboard_chain() -> None:
    """``POST /api/update/auto`` saves config.json and catches no refusal itself: the
    chain answers the pending-file code the frontend translates, not a 500."""
    from kiro_crew.dashboard.handlers.updates_channel import api_update_auto

    path = _hand_edit(lp.LEAF_CONFIG, {"agent": {"bot_name": "Kiro"}})
    app = web.Application(middlewares=[_spa_fallback()])
    app["state"] = SimpleNamespace(owner_id="", push_refresh=MagicMock())
    app.router.add_post("/api/update/auto", api_update_auto)
    async with TestClient(TestServer(as_owner(app))) as client:
        resp = await client.post("/api/update/auto", json={"enabled": False})
        body = await resp.json()

    assert resp.status == 409
    assert body == {"error": lp.PENDING_SAVE_REFUSED, "code": "file_pending_review"}
    assert path.read_bytes() == _HAND_EDIT
    assert _pending_leaves() == [lp.LEAF_CONFIG]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("/api/whatsapp/config", {"enabled": False}),
        ("/api/dashboard/config", {"auto_open_git_panel": True}),
    ],
)
async def test_a_save_handler_with_a_broad_catch_still_answers_the_pending_code(
    route: str, body: dict
) -> None:
    """These savers catch every other write failure as a 500; a pending file is a 409."""
    from kiro_crew.dashboard.handlers.files import api_dashboard_config
    from kiro_crew.dashboard.handlers.whatsapp_setup import whatsapp_config_save

    path = _hand_edit(lp.LEAF_CONFIG, {"agent": {"bot_name": "Kiro"}})
    app = web.Application(middlewares=[_spa_fallback()])
    app["state"] = SimpleNamespace(owner_id="", push_refresh=MagicMock())
    app.router.add_put("/api/whatsapp/config", whatsapp_config_save)
    app.router.add_put("/api/dashboard/config", api_dashboard_config)
    async with TestClient(TestServer(as_owner(app))) as client:
        resp = await client.put(route, json=body)
        answer = await resp.json()

    assert resp.status == 409, answer
    assert answer == {"error": lp.PENDING_SAVE_REFUSED, "code": "file_pending_review"}
    assert path.read_bytes() == _HAND_EDIT
    assert _pending_leaves() == [lp.LEAF_CONFIG]


def test_a_cli_command_that_saves_a_pending_config_json_prints_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """``kirocrew telemetry disable`` has no catch of its own for the refusal: the CLI
    entry prints the sentence and exits 1, with no traceback."""
    from kiro_crew import cli

    path = _hand_edit(lp.LEAF_CONFIG, {"agent": {"bot_name": "Kiro"}})
    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "telemetry", "disable"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert f"❌ {lp.PENDING_SAVE_REFUSED}" in err
    assert "Traceback" not in err
    assert path.read_bytes() == _HAND_EDIT


def _writeback_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "Config write-back failed" in r.getMessage()]


def test_a_refused_write_back_is_logged_once_per_version_of_the_pending_file(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """Every load retries the migration write-back and is refused again while
    config.json is pending; it is logged once, and once more after a new edit."""
    import logging

    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setattr(lp, "_writeback_refusals_logged", set())
    path = _hand_edit(lp.LEAF_CONFIG, {"agent": {"bot_name": "Kiro"}})
    caplog.set_level(logging.WARNING)

    KiroCrewConfig.load()
    KiroCrewConfig.load()
    assert _writeback_warnings(caplog) == [f"Config write-back failed: {lp.PENDING_SAVE_REFUSED}"]

    path.write_bytes((json.dumps({"agent": {"bot_name": "Again"}}) + "\n").encode())
    KiroCrewConfig.load()
    KiroCrewConfig.load()
    assert len(_writeback_warnings(caplog)) == 2


@pytest.mark.parametrize("pending", [lp.LEAF_COMPUTER_USE, lp.LEAF_CONFIG])
def test_a_mixed_computer_use_save_refuses_a_pending_file_before_either_write(
    pending: str,
) -> None:
    """The preflight refuses first, so a refused save never half-applies the limits."""
    from kiro_crew.config.loader_document import PendingEditBlocksSave
    from kiro_crew.dashboard.handlers import computer_use as handler

    docs = {lp.LEAF_CONFIG: {"computer_use": {"max_tree_nodes": 100}}, lp.LEAF_COMPUTER_USE: {}}
    for leaf, doc in docs.items():
        (_hand_edit if leaf == pending else _endorse)(leaf, doc)
    with pytest.raises(PendingEditBlocksSave):
        handler._assert_writable()
    assert _pending_leaves() == [pending]
