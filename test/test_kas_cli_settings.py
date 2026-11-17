"""kiro-cli's telemetry opt-out reaches a KAS session.

KAS reads no ``cli.json``, so Crew reads the opt-out and sends it itself as
``_meta.kiro.telemetryEnabled`` on ``initialize``, with a notice row because KAS does
not confirm it.
"""

from __future__ import annotations

import json
import logging

import pytest

from kiro_crew.acp import kas_cli_settings as kcs
from kiro_crew.acp import runtime as runtime_facade
from kiro_crew.acp import runtime_spawn_launch
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import ACP_BACKEND_KAS, KAS_CLIENT_CAPABILITIES


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """A private kiro home and workspace; no telemetry switch in the environment."""
    home = tmp_path / "kiro"
    work = tmp_path / "work"
    (home / "settings").mkdir(parents=True)
    (work / ".kiro" / "settings").mkdir(parents=True)
    monkeypatch.setenv("KIRO_HOME", str(home))
    for name in kcs.TELEMETRY_OFF_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(kcs, "_OPT_OUT_WARNED", False)
    monkeypatch.setattr(runtime_facade, "agent_env_overlay", dict)
    return home / "settings" / "cli.json", work / ".kiro" / "settings" / "cli.json", work


def test_telemetry_is_on_when_nothing_says_off(homes):
    _, _, work = homes
    assert kcs.kiro_cli_telemetry_enabled(work, environ={}) is True


@pytest.mark.parametrize("which", ["global", "workspace"])
def test_an_opt_out_in_either_file_turns_it_off(homes, which):
    glob, ws, work = homes
    (glob if which == "global" else ws).write_text(json.dumps({"telemetry.enabled": False}))
    assert kcs.kiro_cli_telemetry_enabled(work, environ={}) is False


def test_a_workspace_true_does_not_undo_a_global_false(homes):
    glob, ws, work = homes
    glob.write_text(json.dumps({"telemetry.enabled": False}))
    ws.write_text(json.dumps({"telemetry.enabled": True}))
    assert kcs.kiro_cli_telemetry_enabled(work, environ={}) is False


@pytest.mark.parametrize("name", kcs.TELEMETRY_OFF_ENV)
def test_the_environment_switch_turns_it_off(homes, name):
    _, _, work = homes
    assert kcs.kiro_cli_telemetry_enabled(work, environ={name: ""}) is False


@pytest.mark.parametrize(
    "body", ["{not json", "[1]", "", "  \n", json.dumps({"telemetry.enabled": "false"})]
)
def test_an_unreadable_file_or_value_fails_closed(homes, body):
    glob, _, work = homes
    glob.write_text(body)
    assert kcs.kiro_cli_telemetry_enabled(work, environ={}) is False


def test_the_opt_out_is_a_copy_and_warned_once(caplog):
    caplog.set_level(logging.WARNING, logger=kcs.__name__)
    before = json.dumps(KAS_CLIENT_CAPABILITIES, sort_keys=True)
    kcs._OPT_OUT_WARNED = False
    out = kcs.with_telemetry_opt_out(KAS_CLIENT_CAPABILITIES, False)
    kcs.with_telemetry_opt_out(KAS_CLIENT_CAPABILITIES, False)
    assert out["_meta"]["kiro"]["telemetryEnabled"] is False
    assert out["_meta"]["kiro"]["openExternalUrl"] is True
    assert json.dumps(KAS_CLIENT_CAPABILITIES, sort_keys=True) == before
    assert sum("cannot check" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("opted_out", [True, False])
async def test_the_kas_handshake_carries_the_opt_out(homes, monkeypatch, opted_out):
    glob, _, work = homes
    glob.write_text(json.dumps({"telemetry.enabled": not opted_out}))
    runtime = AcpRuntime(acp_backend=ACP_BACKEND_KAS)
    runtime._tool_search = None
    runtime._work_dir = work
    caps = await runtime_spawn_launch._handshake_client_capabilities(runtime)
    assert caps["_meta"]["kiro"]["telemetryEnabled"] is (not opted_out)
    assert runtime._kas_telemetry_sent is (not opted_out)


def test_the_session_notice_per_process_and_folder(homes):
    """Off was sent: unconfirmed. On was sent but this folder opts out: not applied."""
    glob, ws, work = homes
    assert kcs.session_telemetry_notice(None, work) == ""
    assert kcs.session_telemetry_notice(False, work) == kcs.TELEMETRY_UNCONFIRMED_NOTICE
    assert kcs.session_telemetry_notice(True, work) == ""
    ws.write_text(json.dumps({"telemetry.enabled": False}))
    assert kcs.session_telemetry_notice(True, work) == kcs.TELEMETRY_NOT_APPLIED_NOTICE


def test_an_opt_out_shows_one_session_notice_row(monkeypatch):
    """The user sees, in the chat, that Crew cannot confirm KAS turned telemetry off."""
    from types import SimpleNamespace

    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.providers.acp import AcpProvider

    session = AcpSessionProvider.__new__(AcpSessionProvider)
    session._handle = SimpleNamespace(kas_telemetry_notice=kcs.TELEMETRY_UNCONFIRMED_NOTICE)
    read = AcpProvider.telemetry_unconfirmed_notice.fget
    assert read(SimpleNamespace(_client=session)) == kcs.TELEMETRY_UNCONFIRMED_NOTICE
    assert read(SimpleNamespace(_client=SimpleNamespace())) == ""
    rows = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *a, **k: rows.append(a))
    chat_runner._notice_steer_upgrade(
        object(),
        SimpleNamespace(_telemetry_notice_shown=""),
        SimpleNamespace(telemetry_unconfirmed_notice=read(SimpleNamespace(_client=session))),
    )
    assert [r[2] for r in rows] == ["notice"] and "cannot check" in rows[0][3]
    rows.clear()
    chat_runner._notice_steer_upgrade(
        object(), object(), SimpleNamespace(telemetry_unconfirmed_notice="")
    )
    assert rows == []


def _telemetry_rows(monkeypatch) -> list[str]:
    from kiro_crew.dashboard import chat_runner

    rows: list[str] = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *a, **k: rows.append(a[3]))
    return rows


def _session_start(slot, notice: str, state: object = None) -> None:
    """One session start of *slot*'s chat on a KAS session that carries *notice*."""
    from types import SimpleNamespace

    from kiro_crew.dashboard import chat_runner

    chat_runner._notice_steer_upgrade(
        state, slot, SimpleNamespace(telemetry_unconfirmed_notice=notice)
    )


def test_a_chat_shows_the_telemetry_notice_once(monkeypatch):
    """Each KAS session a chat creates or reloads carries the notice; the chat shows it once.

    It shows the other notice when the answer changes, and each chat is told on its own.
    """
    from types import SimpleNamespace

    rows = _telemetry_rows(monkeypatch)
    chat = SimpleNamespace(_telemetry_notice_shown="")
    for _ in range(3):  # the first session, then two sessions that replace it
        _session_start(chat, kcs.TELEMETRY_UNCONFIRMED_NOTICE)
    assert rows == [kcs.TELEMETRY_UNCONFIRMED_NOTICE]
    for _ in range(2):  # the chat moves to an engine that did not take the opt-out
        _session_start(chat, kcs.TELEMETRY_NOT_APPLIED_NOTICE)
    assert rows == [kcs.TELEMETRY_UNCONFIRMED_NOTICE, kcs.TELEMETRY_NOT_APPLIED_NOTICE]
    _session_start(chat, kcs.TELEMETRY_UNCONFIRMED_NOTICE)
    assert rows[2:] == [kcs.TELEMETRY_UNCONFIRMED_NOTICE]
    _session_start(SimpleNamespace(_telemetry_notice_shown=""), kcs.TELEMETRY_UNCONFIRMED_NOTICE)
    assert rows[3:] == [kcs.TELEMETRY_UNCONFIRMED_NOTICE]


def test_a_restart_does_not_repeat_the_telemetry_notice(tmp_path, monkeypatch):
    """A chat saved after the notice and restored from disk stays told."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_persistence import (
        _rehydrate_slot_from_history,
        _save_slot_to_history,
    )

    def notices(slot) -> list[str]:
        return [row["content"] for row in slot.messages if row["role"] == "notice"]

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    chat = state.get_or_create_slot("s1")
    chat.append("user", "hello")
    _session_start(chat, kcs.TELEMETRY_UNCONFIRMED_NOTICE, state)
    chat.drain()
    _save_slot_to_history(state, chat, closed=False)
    del state._slots["s1"]

    restored = _rehydrate_slot_from_history(state, "s1")
    assert restored is not None and restored is not chat
    _session_start(restored, kcs.TELEMETRY_UNCONFIRMED_NOTICE, state)
    assert notices(restored) == [kcs.TELEMETRY_UNCONFIRMED_NOTICE]

    # A line that carries anything but a mark tells the chat again.
    del state._slots["s1"]
    state.conversation_log.update_metadata("dashboard:s1", {"telemetry_notice_shown": "no mark"})
    forged = _rehydrate_slot_from_history(state, "s1")
    assert forged is not None
    _session_start(forged, kcs.TELEMETRY_UNCONFIRMED_NOTICE, state)
    assert notices(forged) == [kcs.TELEMETRY_UNCONFIRMED_NOTICE] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("name", kcs.TELEMETRY_OFF_ENV)
async def test_a_switch_in_the_spawns_extra_env_turns_it_off(homes, name):
    """The KAS process gets the spawn's extra_env, so its switch counts too."""
    _, _, work = homes
    runtime = AcpRuntime(acp_backend=ACP_BACKEND_KAS, extra_env={name: "1"})
    runtime._tool_search = None
    runtime._work_dir = work
    caps = await runtime_spawn_launch._handshake_client_capabilities(runtime)
    assert caps["_meta"]["kiro"]["telemetryEnabled"] is False
    assert runtime._kas_telemetry_sent is False


def test_a_deeply_nested_file_fails_closed_without_aborting(homes):
    """A RecursionError from the parser is an unreadable file, not a failed start."""
    glob, _, work = homes
    glob.write_text("[" * 100_000 + "]" * 100_000)
    assert kcs.kiro_cli_telemetry_enabled(work, environ={}) is False


@pytest.mark.asyncio
async def test_a_switch_in_agent_env_turns_it_off_and_reaches_launch(homes, monkeypatch):
    """agent.env reaches the child, so its switch counts; launch gets the same snapshot."""
    _, _, work = homes
    overlay = {kcs.TELEMETRY_OFF_ENV[1]: "1"}
    monkeypatch.setattr(runtime_facade, "agent_env_overlay", lambda: dict(overlay))
    runtime = AcpRuntime(acp_backend=ACP_BACKEND_KAS)
    runtime._tool_search = None
    runtime._work_dir = work
    caps = await runtime_spawn_launch._handshake_client_capabilities(runtime)
    assert caps["_meta"]["kiro"]["telemetryEnabled"] is False
    assert runtime._spawn_agent_env == overlay


def test_default_model_workspace_wins_and_bad_values_are_skipped(homes):
    glob, ws, work = homes
    assert kcs.kiro_cli_default_model(work) == ""
    glob.write_text(json.dumps({"chat.defaultModel": "claude-sonnet-4.5"}))
    assert kcs.kiro_cli_default_model(work) == "claude-sonnet-4.5"
    ws.write_text(json.dumps({"chat.defaultModel": "claude-opus-4.6"}))
    assert kcs.kiro_cli_default_model(work) == "claude-opus-4.6"
    ws.write_text(json.dumps({"chat.defaultModel": "rm -rf /"}))
    assert kcs.kiro_cli_default_model(work) == "claude-sonnet-4.5"
    ws.write_text("{broken")
    assert kcs.kiro_cli_default_model(work) == "claude-sonnet-4.5"


@pytest.mark.parametrize("value", ["auto", "Auto"])
def test_an_auto_default_model_is_nothing_to_apply(homes, value):
    """KAS already starts a new session on auto, so there is nothing to inherit."""
    glob, _, work = homes
    glob.write_text(json.dumps({"chat.defaultModel": value}))
    assert kcs.kiro_cli_default_model(work) == ""


def test_the_default_model_notice_names_the_model_and_the_picker():
    assert kcs.default_model_notice("") == ""
    notice = kcs.default_model_notice("claude-sonnet-4.5")
    assert "claude-sonnet-4.5" in notice and "model picker" in notice


def test_the_default_model_notice_reaches_the_session_start_row(monkeypatch):
    from types import SimpleNamespace

    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.providers.acp import AcpProvider

    session = AcpSessionProvider.__new__(AcpSessionProvider)
    session.kas_default_model_notice = "ℹ names claude-sonnet-4.5"
    read = AcpProvider.kiro_cli_model_notice.fget
    assert read(SimpleNamespace(_client=session)) == "ℹ names claude-sonnet-4.5"
    assert read(SimpleNamespace(_client=SimpleNamespace())) == ""
    rows = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *a, **k: rows.append(a))
    client = SimpleNamespace(kiro_cli_model_notice=read(SimpleNamespace(_client=session)))
    chat_runner._notice_steer_upgrade(object(), object(), client)
    assert [r[2] for r in rows] == ["notice"] and "claude-sonnet-4.5" in rows[0][3]


def test_loading_it_first_leaves_the_skill_projection_installable(tmp_path):
    """A kiro_home patched after this module loads must not break the facade's install.

    Loading one skill-projection owner without its facade, then patching ``kiro_home``
    before the facade's first import, left that owner on the old binding, and the
    facade's family check refused to install. A fresh interpreter gives the real order.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    import kiro_crew
    from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

    code = (
        "import kiro_crew.acp.kas_cli_settings\n"
        "import kiro_crew.config.paths as paths\n"
        "paths.kiro_home = lambda: None\n"
        "import kiro_crew.acp.skill_projection\n"
    )
    env = dict(os.environ)
    package_root = str(Path(kiro_crew.__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, env.get("PYTHONPATH")]))
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    assert result.returncode == 0, result.stderr
