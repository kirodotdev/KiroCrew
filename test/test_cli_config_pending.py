"""``kirocrew config pending``: resolve a held outside edit without the dashboard.

Where the namespace sandbox seals the keystone files, a file that differs from its
endorsed copy is pending and its ordinary saves are refused until the owner applies or
discards it. A gateway started without the dashboard has no Settings page, so the CLI
runs the Settings routes' own writer, bound to the version it listed, and refuses inside
an agent sandbox.

The conftest pins ``KIROCREW_HOME`` per test, so ``config_dir()`` is a fresh temp dir.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from config_pending_helpers import HAND_EDIT

from kiro_crew import cli, cli_config_pending, sandbox_launch_mark
from kiro_crew.config import loosening_provenance as lp
from kiro_crew.config.loader import PendingEditBlocksSave
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.handlers import computer_use, security

SAVED = (json.dumps({"agent": {"bot_name": "Kiro"}}) + "\n").encode()


@pytest.fixture(autouse=True)
def audit(_floor_monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    _floor_monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    log = MagicMock()
    _floor_monkeypatch.setattr(cli_config_pending, "sel", lambda: log)
    return log


def _pending(leaf: str, saved: bytes, edit: bytes) -> str:
    """*leaf* endorsed as *saved*, then changed outside Kiro Crew to *edit*; its signature."""
    lp.record(leaf, saved, strict=True)
    (config_dir() / leaf).write_bytes(edit)
    return lp.disk_signature(edit)


def _on_disk(leaf: str) -> dict:
    return json.loads((config_dir() / leaf).read_bytes())


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "config", "pending", *argv])
    try:
        cli.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_the_listing_names_each_change_and_the_commands_that_resolve_it(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    signature = _pending(lp.LEAF_CONFIG, SAVED, HAND_EDIT)

    assert _run(monkeypatch) == 0

    out = capsys.readouterr().out
    assert 'changed "agent.bot_name": "Kiro" -> "Hand"' in out
    assert f"kirocrew config pending apply config.json {signature}" in out
    assert f"kirocrew config pending discard config.json {signature}" in out
    assert lp.leaf_pending(lp.LEAF_CONFIG)


def test_apply_endorses_the_listed_version_so_ordinary_saves_work_again(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    signature = _pending(lp.LEAF_CONFIG, SAVED, HAND_EDIT)

    assert _run(monkeypatch, "apply", "config.json", signature) == 0

    assert _on_disk(lp.LEAF_CONFIG)["agent"] == {"bot_name": "Hand"}
    assert lp.pending_changes() == []
    lp.refuse_save_if_pending(lp.LEAF_CONFIG)
    assert "Restart" not in capsys.readouterr().out


def test_discard_puts_the_saved_version_back(monkeypatch: pytest.MonkeyPatch) -> None:
    signature = _pending(lp.LEAF_CONFIG, SAVED, HAND_EDIT)

    assert _run(monkeypatch, "discard", "config.json", signature) == 0

    assert _on_disk(lp.LEAF_CONFIG)["agent"] == {"bot_name": "Kiro"}
    assert lp.pending_changes() == []


def test_a_version_other_than_the_listed_one_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _pending(lp.LEAF_CONFIG, SAVED, HAND_EDIT)

    assert _run(monkeypatch, "apply", "config.json", lp.disk_signature(b"{}\n")) == 1

    assert "not held for review at that version" in capsys.readouterr().err
    assert lp.endorsed_bytes(lp.LEAF_CONFIG) == SAVED
    assert lp.leaf_pending(lp.LEAF_CONFIG)


@pytest.mark.parametrize("action", ["apply", "discard"])
def test_inside_the_agent_sandbox_nothing_is_resolved(
    monkeypatch: pytest.MonkeyPatch, audit: MagicMock, action: str
) -> None:
    signature = _pending(lp.LEAF_CONFIG, SAVED, HAND_EDIT)
    monkeypatch.setattr(sandbox_launch_mark, "launched_in_sandbox", lambda: True)

    assert _run(monkeypatch, action, "config.json", signature) == 1

    assert (config_dir() / lp.LEAF_CONFIG).read_bytes() == HAND_EDIT
    assert lp.endorsed_bytes(lp.LEAF_CONFIG) == SAVED
    assert audit.log_api_access.call_args.kwargs["outcome"] == "denied"


def test_applying_denied_commands_says_a_running_gateway_needs_a_restart(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    edit = (json.dumps({"disable_all": True}) + "\n").encode()
    signature = _pending(lp.LEAF_DENIED_COMMANDS, b'{"disable_all": false}\n', edit)

    assert _run(monkeypatch, "apply", "denied_commands.json", signature) == 0

    assert _on_disk(lp.LEAF_DENIED_COMMANDS) == {"disable_all": True}
    assert lp.pending_changes() == []
    assert "Restart the gateway" in capsys.readouterr().out


def test_a_key_the_edit_wrote_cannot_drive_the_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    edit = (json.dumps({"agent": {"bot_name": "Kiro"}, "\x1b[2Kx": 1}) + "\n").encode()
    _pending(lp.LEAF_CONFIG, SAVED, edit)

    assert _run(monkeypatch) == 0

    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert 'added "\\u001b[2Kx": 1' in out


def test_a_changed_key_whose_new_value_is_not_shown_lists_no_value(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The diff leaves out a list too long to show; printed alone, the ``[]`` it
    replaced would read as what the edit wrote."""
    ids = [f"rule-{i}" for i in range(lp._MAX_SHOWN_LIST + 1)]
    edit = (json.dumps({"disabled_ids": ids}) + "\n").encode()
    _pending(lp.LEAF_DENIED_COMMANDS, b'{"disabled_ids": []}\n', edit)

    assert _run(monkeypatch) == 0

    assert '  changed "disabled_ids"\n' in capsys.readouterr().out


def _toggle_a_builtin() -> None:
    asyncio.run(security._write_denied_state(lambda d: d.update({"disabled_ids": ["x"]})))


@pytest.mark.parametrize(
    "leaf, saved, edit, reader, save",
    [
        (
            lp.LEAF_DENIED_COMMANDS,
            {"user_added": []},
            {"user_added": [{"id": "user-1", "pattern": "^rm -rf", "enabled": True}]},
            (security, "_read_denied_strict"),
            _toggle_a_builtin,
        ),
        (
            lp.LEAF_COMPUTER_USE,
            {"enabled": False},
            {"enabled": False, "allowed_apps": ["notes"]},
            (computer_use, "_read_state_strict"),
            lambda: computer_use._write_state({"extra_denied_apps": ["terminal"]}),
        ),
    ],
    ids=["denied-commands", "computer-use"],
)
def test_a_save_begun_while_the_file_is_pending_cannot_publish_over_its_apply(
    monkeypatch: pytest.MonkeyPatch, leaf: str, saved: dict, edit: dict, reader, save
) -> None:
    """The gateway and this command are two processes. A dashboard save that read the
    endorsed copy and then saw another process's Apply land would publish that stale
    copy over it, so the save refuses a pending file before it reads."""
    signature = _pending(
        leaf, (json.dumps(saved) + "\n").encode(), (json.dumps(edit) + "\n").encode()
    )
    module, name = reader
    real = getattr(module, name)
    reads: list[dict] = []

    def _read_then_apply() -> dict:
        stale = real()
        reads.append(stale)
        monkeypatch.setattr(module, name, real)  # the Apply reads for real
        assert _run(monkeypatch, "apply", leaf, signature) == 0
        return stale

    monkeypatch.setattr(module, name, _read_then_apply)

    with pytest.raises(PendingEditBlocksSave):
        save()
    assert reads == []

    monkeypatch.setattr(module, name, real)
    assert _run(monkeypatch, "apply", leaf, signature) == 0
    assert _on_disk(leaf) == edit
    assert json.loads(lp.endorsed_bytes(leaf)) == edit
