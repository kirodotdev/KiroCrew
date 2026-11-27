# SPDX-License-Identifier: Apache-2.0
"""Add abilities can leave out a write ``fallbackAction`` the bot cannot keep.

A bot keeps the write tool's ``fallbackAction`` only when it is a deny. For any
other value the add is refused by name, unless the owner asks for the add
``without_write_fallback``: then the key is left out of the bot's file, so a
write no ``allowedPaths`` entry covers is answered by the bot's approval mode
instead of running unasked. The
agent's own file is never changed, and a carried deny is never dropped.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import mate_capabilities, mate_worker_kit
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.config import paths
from kiro_crew.mate_worker_kit import WorkerKitError

WORKER_BASE = {
    "name": "kirocrew-mate-worker",
    "prompt": "base",
    "tools": ["*"],
    "allowedTools": ["fs_read"],
    "mcpServers": {},
}


def _agent(write: dict) -> dict:
    return {
        "name": "team-writer",
        "prompt": "Write team notes.",
        "tools": ["*"],
        "mcpServers": {},
        "toolsSettings": {**write},
    }


@pytest.fixture(autouse=True)
def _no_ceiling(_floor_monkeypatch):
    """The governance ceiling is its own module's concern; here it passes grants."""
    from kiro_crew.platform import governance

    _floor_monkeypatch.setattr(
        governance, "sanitize_agent_config_governance", lambda spec, **_k: None
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    user, mates = tmp_path / "agents", tmp_path / "mates" / "agents"
    user.mkdir()
    mates.mkdir(parents=True)
    (mates / "pixel.json").write_text(
        json.dumps({"name": "pixel", "prompt": "", "tools": [], "mcpServers": {}}),
        encoding="utf-8",
    )
    (mates / "kirocrew-mate-worker.json").write_text(json.dumps(WORKER_BASE), encoding="utf-8")
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_capabilities, "binding_is_mate", lambda member: member == "pixel")
    monkeypatch.setattr(mate_worker_kit, "_crewmate_backend", lambda member, held: ACP_BACKEND_KAS)
    return user, mates


def _use(user, settings: dict) -> str:
    text = json.dumps(_agent(settings))
    (user / "team-writer.json").write_text(text, encoding="utf-8")
    return text


def _file(mates) -> dict:
    return json.loads((mates / "pixel.json").read_text(encoding="utf-8"))


def _without(**extra) -> dict:
    return {"template": "team-writer", "without_write_fallback": True, **extra}


@pytest.mark.parametrize("tool", ["write", "fsWrite", "fs_write"])
def test_without_write_fallback_adds_the_agent_and_leaves_the_value_out(home, tool):
    user, mates = home
    agent_text = _use(user, {tool: {"allowedPaths": ["team/**"], "fallbackAction": "allow"}})
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.preview("pixel", {"template": "team-writer"})
    assert caught.value.detail == f"toolsSettings.{tool}.fallbackAction"

    preview = mate_worker_kit.preview("pixel", _without())
    assert preview["left_out"] == [f"toolsSettings.{tool}.fallbackAction"]
    mate_worker_kit.apply("pixel", _without(revision=preview["revision"]))

    assert _file(mates)["toolsSettings"] == {tool: {"allowedPaths": ["team/**"]}}
    # The agent's own file is left as its author wrote it.
    assert (user / "team-writer.json").read_text(encoding="utf-8") == agent_text


def test_a_lone_fallback_leaves_no_empty_entry(home):
    user, mates = home
    _use(user, {"write": {"fallbackAction": "allow"}})
    preview = mate_worker_kit.preview("pixel", _without())
    mate_worker_kit.apply("pixel", _without(revision=preview["revision"]))
    assert "write" not in (_file(mates).get("toolsSettings") or {})


def test_a_carried_deny_is_kept_and_nothing_is_left_out(home):
    user, mates = home
    _use(user, {"write": {"allowedPaths": ["team/**"], "fallbackAction": "deny"}})
    preview = mate_worker_kit.preview("pixel", _without())
    assert preview["left_out"] == []
    mate_worker_kit.apply("pixel", _without(revision=preview["revision"]))
    assert _file(mates)["toolsSettings"]["write"]["fallbackAction"] == "deny"


def test_the_flag_leaves_out_nothing_else_the_bot_cannot_keep(home):
    user, _mates = home
    _use(
        user,
        {
            "write": {"fallbackAction": "allow"},
            "execute_bash": {"fallbackAction": "allow"},
        },
    )
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.preview("pixel", _without())
    assert caught.value.code == "template_restrictions_uncarried"
    assert caught.value.detail == "toolsSettings.execute_bash.fallbackAction"


def test_an_apply_must_carry_the_same_choice_as_its_preview(home):
    user, _mates = home
    _use(user, {"write": {"allowedPaths": ["team/**"], "fallbackAction": "allow"}})
    preview = mate_worker_kit.preview("pixel", _without())
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.apply("pixel", {"template": "team-writer", "revision": preview["revision"]})
    assert caught.value.code == "template_restrictions_uncarried"


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_a_flag_that_is_not_a_boolean_is_refused(home, value):
    user, _mates = home
    _use(user, {"write": {"fallbackAction": "allow"}})
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.preview(
            "pixel", {"template": "team-writer", "without_write_fallback": value}
        )
    assert caught.value.code == "invalid_request"
