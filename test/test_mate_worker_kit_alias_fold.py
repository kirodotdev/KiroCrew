# SPDX-License-Identifier: Apache-2.0
"""Add abilities accepts an agent that sets one tool under two spellings, or sets
sub-agent keys no engine reads.

The two spellings of one tool (``shell`` and ``execute_bash``, ``write`` and
``fs_write``) are folded into one entry under the spelling kiro-cli reads, keeping
the stricter value of each key, and the preview names the fold. A key the fold
cannot combine is refused by name. ``subagent.autoAllow`` and
``subagent.defaultAgent`` are carried as written.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import mate_capabilities, mate_worker_kit
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.config import paths
from kiro_crew.mate_alias_fold import fold_alias_settings
from kiro_crew.mate_worker_kit import WorkerKitError

WORKER_BASE = {
    "name": "kirocrew-mate-worker",
    "prompt": "base",
    "tools": ["*"],
    "allowedTools": ["fs_read"],
    "mcpServers": {},
}

#: An agent written for kiro-cli: each tool under two spellings, plus sub-agent
#: keys that neither engine reads.
AGENT = {
    "name": "release-bot",
    "prompt": "Release SOP.",
    "tools": ["*"],
    "mcpServers": {},
    "toolsSettings": {
        "shell": {
            "allowedCommands": ["git status", "git log .*"],
            "deniedCommands": ["rm .*"],
            "autoAllowReadonly": True,
        },
        "execute_bash": {
            "allowedCommands": ["git status"],
            "deniedCommands": ["git push .*"],
            "autoAllowReadonly": True,
        },
        "write": {"allowedPaths": ["~/work/**"]},
        "fs_write": {"allowedPaths": ["~/work/**"]},
        "subagent": {
            "availableAgents": ["reviewer"],
            "autoAllow": ["reviewer"],
            "defaultAgent": "reviewer",
        },
    },
}


@pytest.fixture(autouse=True)
def _no_ceiling(_floor_monkeypatch):
    """The governance ceiling is its own module's concern; here it passes grants."""
    from kiro_crew.platform import governance

    _floor_monkeypatch.setattr(
        governance, "sanitize_agent_config_governance", lambda spec, **_k: None
    )


def _home(tmp_path, monkeypatch, agent):
    user, mates = tmp_path / "agents", tmp_path / "mates" / "agents"
    user.mkdir()
    mates.mkdir(parents=True)
    (user / "release-bot.json").write_text(json.dumps(agent), encoding="utf-8")
    (mates / "pixel.json").write_text(
        json.dumps({"name": "pixel", "prompt": "", "tools": [], "mcpServers": {}}),
        encoding="utf-8",
    )
    (mates / "kirocrew-mate-worker.json").write_text(json.dumps(WORKER_BASE), encoding="utf-8")
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_capabilities, "binding_is_mate", lambda member: member == "pixel")
    monkeypatch.setattr(mate_worker_kit, "_crewmate_backend", lambda member, held: ACP_BACKEND_KAS)
    return mates


def test_an_agent_with_two_spellings_and_inert_subagent_keys_is_added(tmp_path, monkeypatch):
    mates = _home(tmp_path, monkeypatch, AGENT)
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    # Each folded tool is one ``added`` row under the key the copy writes, naming
    # the spellings it came from; no tool gets a second row.
    tool_rows = [c for c in preview["changes"] if c["section"] == "toolsSettings"]
    assert {c["id"]: (c["change"], c.get("merged_from")) for c in tool_rows} == {
        "shell": ("added", ["shell", "execute_bash"]),
        "write": ("added", ["write", "fs_write"]),
        "subagent": ("added", None),
    }
    assert len(tool_rows) == 3

    mate_worker_kit.apply("pixel", {"template": "release-bot", "revision": preview["revision"]})
    written = json.loads((mates / "pixel.json").read_text(encoding="utf-8"))["toolsSettings"]
    assert set(written) == {"shell", "write", "subagent"}
    # Allows both spellings grant, every deny either one makes.
    assert written["shell"] == {
        "allowedCommands": ["git status"],
        "deniedCommands": ["rm .*", "git push .*"],
        "autoAllowReadonly": True,
    }
    assert written["write"] == {"allowedPaths": ["~/work/**"]}
    assert written["subagent"] == AGENT["toolsSettings"]["subagent"]
    # The worker the Bot runs holds the same entries, one spelling each.
    from kiro_crew.mate_worker import merge_worker_spec, worker_additions

    bot_file = json.loads((mates / "pixel.json").read_text(encoding="utf-8"))
    worker = merge_worker_spec(WORKER_BASE, worker_additions(WORKER_BASE, bot_file))
    assert worker["toolsSettings"] == written


def test_a_spelling_that_does_not_set_a_grant_drops_it():
    settings = {
        "shell": {"allowedCommands": ["ls"], "autoAllowReadonly": True},
        "execute_bash": {"denyByDefault": True},
    }
    folded, folds, refused = fold_alias_settings(settings)
    assert refused == []
    assert folds == [{"kept": "shell", "spellings": ["shell", "execute_bash"]}]
    assert folded == {"shell": {"denyByDefault": True}}


def test_restricting_lists_keep_the_narrower_one():
    settings = {
        "subagent": {"availableAgents": ["a", "b"]},
        "use_subagent": {"availableAgents": ["b", "c"]},
        "read": {"deniedPaths": ["~/.ssh"]},
        "fs_read": {},
    }
    folded, _folds, refused = fold_alias_settings(settings)
    assert refused == []
    assert folded == {"subagent": {"availableAgents": ["b"]}, "read": {"deniedPaths": ["~/.ssh"]}}
    one_sided, _f, _r = fold_alias_settings(
        {"subagent": {}, "use_subagent": {"availableAgents": ["c"]}}
    )
    assert one_sided == {"subagent": {"availableAgents": ["c"]}}


@pytest.mark.parametrize(
    "settings, field",
    [
        # A key with no combining rule that the spellings set differently.
        (
            {"shell": {"fallbackAction": "ask"}, "execute_bash": {"fallbackAction": "allow"}},
            "toolsSettings.shell.fallbackAction",
        ),
        # A value of the wrong type cannot be combined.
        (
            {"write": {"allowedPaths": "~/x"}, "fs_write": {"allowedPaths": ["~/x"]}},
            "toolsSettings.write.allowedPaths",
        ),
        ({"shell": {}, "execute_bash": "deny"}, "toolsSettings.execute_bash"),
    ],
)
def test_what_the_fold_cannot_combine_is_refused_by_name(tmp_path, monkeypatch, settings, field):
    folded, folds, refused = fold_alias_settings(settings)
    assert (folded, folds, refused) == (settings, [], [field])
    _home(tmp_path, monkeypatch, {**AGENT, "toolsSettings": settings})
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.preview("pixel", {"template": "release-bot"})
    assert caught.value.code == "template_restrictions_uncarried"
    assert caught.value.detail == field


def test_one_spelling_alone_is_left_as_written():
    settings = {"execute_bash": {"allowedCommands": ["ls"]}}
    assert fold_alias_settings(settings) == (settings, [], [])


def test_a_deny_fallback_on_one_write_spelling_is_kept(tmp_path, monkeypatch):
    settings = {
        "write": {"allowedPaths": ["~/w/**"], "fallbackAction": "deny"},
        "fs_write": {"allowedPaths": ["~/w/**"]},
    }
    folded, _folds, refused = fold_alias_settings(settings)
    assert refused == []
    assert folded == {"write": {"allowedPaths": ["~/w/**"], "fallbackAction": "deny"}}
    mates = _home(tmp_path, monkeypatch, {**AGENT, "toolsSettings": settings})
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    mate_worker_kit.apply("pixel", {"template": "release-bot", "revision": preview["revision"]})
    written = json.loads((mates / "pixel.json").read_text(encoding="utf-8"))["toolsSettings"]
    assert written["write"] == {"allowedPaths": ["~/w/**"], "fallbackAction": "deny"}


def test_a_looser_write_fallback_on_one_spelling_is_refused_then_left_out(tmp_path, monkeypatch):
    settings = {
        "write": {"allowedPaths": ["~/w/**"], "fallbackAction": "allow"},
        "fs_write": {"allowedPaths": ["~/w/**"]},
    }
    field = "toolsSettings.write.fallbackAction"
    assert fold_alias_settings(settings) == (settings, [], [field])
    mates = _home(tmp_path, monkeypatch, {**AGENT, "toolsSettings": settings})
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.preview("pixel", {"template": "release-bot"})
    assert caught.value.code == "template_restrictions_uncarried"
    assert caught.value.detail == field

    body = {"template": "release-bot", "without_write_fallback": True}
    preview = mate_worker_kit.preview("pixel", body)
    mate_worker_kit.apply("pixel", {**body, "revision": preview["revision"]})
    written = json.loads((mates / "pixel.json").read_text(encoding="utf-8"))["toolsSettings"]
    assert written["write"] == {"allowedPaths": ["~/w/**"]}


def test_a_write_fallback_left_out_is_named_under_the_folded_row(tmp_path, monkeypatch):
    settings = {
        "write": {"allowedPaths": ["~/w/**"]},
        "fs_write": {"allowedPaths": ["~/w/**"], "fallbackAction": "allow"},
    }
    mates = _home(tmp_path, monkeypatch, {**AGENT, "toolsSettings": settings})
    body = {"template": "release-bot", "without_write_fallback": True}
    preview = mate_worker_kit.preview("pixel", body)
    # The pane matches ``left_out`` against the row's own key, the folded one.
    rows = [c["id"] for c in preview["changes"] if c["section"] == "toolsSettings"]
    assert "write" in rows
    assert preview["left_out"] == ["toolsSettings.write.fallbackAction"]
    mate_worker_kit.apply("pixel", {**body, "revision": preview["revision"]})
    written = json.loads((mates / "pixel.json").read_text(encoding="utf-8"))["toolsSettings"]
    assert written["write"] == {"allowedPaths": ["~/w/**"]}


@pytest.mark.parametrize("value", ["", None])
def test_a_write_fallback_the_gate_reads_as_default_is_added_without_it(
    tmp_path, monkeypatch, value
):
    settings = {
        "write": {"allowedPaths": ["~/w/**"], "fallbackAction": value},
        "fs_write": {"allowedPaths": ["~/w/**"]},
    }
    folded, _folds, refused = fold_alias_settings(settings)
    assert refused == []
    assert folded == {"write": {"allowedPaths": ["~/w/**"]}}
    mates = _home(tmp_path, monkeypatch, {**AGENT, "toolsSettings": settings})
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    mate_worker_kit.apply("pixel", {"template": "release-bot", "revision": preview["revision"]})
    written = json.loads((mates / "pixel.json").read_text(encoding="utf-8"))["toolsSettings"]
    assert written["write"] == {"allowedPaths": ["~/w/**"]}


@pytest.mark.parametrize("value", ["Deny", " DENY ", True])
def test_a_write_fallback_the_gate_reads_as_deny_folds_to_deny(tmp_path, monkeypatch, value):
    settings = {
        "write": {"allowedPaths": ["~/w/**"], "fallbackAction": value},
        "fs_write": {"allowedPaths": ["~/w/**"]},
    }
    folded, _folds, refused = fold_alias_settings(settings)
    assert refused == []
    assert folded == {"write": {"allowedPaths": ["~/w/**"], "fallbackAction": "deny"}}
    mates = _home(tmp_path, monkeypatch, {**AGENT, "toolsSettings": settings})
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    mate_worker_kit.apply("pixel", {"template": "release-bot", "revision": preview["revision"]})
    written = json.loads((mates / "pixel.json").read_text(encoding="utf-8"))["toolsSettings"]
    assert written["write"]["fallbackAction"] == "deny"


@pytest.mark.parametrize("value", ["Deny", True])
def test_adding_without_the_write_fallback_keeps_a_deny(tmp_path, monkeypatch, value):
    settings = {
        "write": {"allowedPaths": ["~/w/**"], "fallbackAction": value},
        "fs_write": {"allowedPaths": ["~/w/**"], "fallbackAction": "allow"},
    }
    mates = _home(tmp_path, monkeypatch, {**AGENT, "toolsSettings": settings})
    body = {"template": "release-bot", "without_write_fallback": True}
    preview = mate_worker_kit.preview("pixel", body)
    mate_worker_kit.apply("pixel", {**body, "revision": preview["revision"]})
    written = json.loads((mates / "pixel.json").read_text(encoding="utf-8"))["toolsSettings"]
    assert written["write"]["fallbackAction"] == "deny"
