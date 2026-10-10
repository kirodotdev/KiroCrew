"""The spawn gate checks the ``kirocrew-heartbeat`` copy against the default spec.

``kirocrew-heartbeat.json`` copies the default spec's ``kirocrew-core`` entry,
``autoApprove`` included. A copy left behind (a failed install during a rebuild, or a
process that never rebuilds) keeps an auto-approval the default has since dropped, so a
start on it runs that tool without a prompt. ``require_fresh_derived_spec`` re-derives a
stale copy once and refuses the start when that does not bring it current, the same way
it treats the worker mirror.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent
from kiro_crew.agent_files import AGENT_FILENAME, HEARTBEAT_AGENT_FILENAME
from kiro_crew.agent_materialization import worker_agent

HEARTBEAT = "kirocrew-heartbeat"


def _default_spec(auto_approve: list[str]) -> dict[str, Any]:
    return {
        "name": "kirocrew",
        "description": "the default agent",
        "prompt": "the default prompt",
        "tools": ["fs_read", "@kirocrew-core"],
        "allowedTools": ["fs_read"],
        "mcpServers": {
            "kirocrew-core": {
                "command": "kirocrew-mcp",
                "args": ["core", "--include-tools", "a,b"],
                "autoApprove": list(auto_approve),
            },
            "other": {"command": "other-mcp", "args": []},
        },
    }


def _write(path: Path, spec: dict[str, Any]) -> None:
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _heartbeat_auto_approve(agents_dir: Path) -> list[str]:
    spec = _read(agents_dir / HEARTBEAT_AGENT_FILENAME)
    return spec["mcpServers"]["kirocrew-core"]["autoApprove"]


@pytest.fixture()
def agents_dir(tmp_path, monkeypatch) -> Path:
    """A default spec granting two auto-approvals, and a heartbeat copy of it."""
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: agents)
    _write(agents / AGENT_FILENAME, _default_spec(["learn_add", "cron_add"]))
    agent._install_heartbeat_agent()
    assert _heartbeat_auto_approve(agents) == ["learn_add", "cron_add"]
    return agents


def _drop_cron_add_without_rederiving(agents_dir: Path) -> None:
    """The default drops an auto-approval and nothing re-derives the copy.

    That is the state a failed heartbeat install during a rebuild, or a process that
    never rebuilds, leaves behind.
    """
    _write(agents_dir / AGENT_FILENAME, _default_spec(["learn_add"]))
    assert "cron_add" in _heartbeat_auto_approve(agents_dir)


def test_a_stale_copy_is_re_derived_before_the_start(agents_dir) -> None:
    _drop_cron_add_without_rederiving(agents_dir)

    snapshot = agent.require_fresh_derived_spec(HEARTBEAT, None)

    assert _heartbeat_auto_approve(agents_dir) == ["learn_add"]
    assert snapshot is not None
    assert snapshot.agent == HEARTBEAT
    assert snapshot.spec is not None
    assert snapshot.spec["mcpServers"]["kirocrew-core"]["autoApprove"] == ["learn_add"]


def test_a_stale_copy_that_cannot_be_re_derived_refuses_the_start(agents_dir, monkeypatch) -> None:
    _drop_cron_add_without_rederiving(agents_dir)

    def failing() -> None:
        raise OSError("heartbeat spec is not writable")

    monkeypatch.setattr(agent, "_install_heartbeat_agent", failing)
    with pytest.raises(agent.DerivedSpecStale) as caught:
        agent.require_fresh_derived_spec(HEARTBEAT, None)

    assert HEARTBEAT_AGENT_FILENAME in str(caught.value)
    assert "cron_add" in _heartbeat_auto_approve(agents_dir)


def test_a_current_copy_is_not_rewritten(agents_dir, monkeypatch) -> None:
    calls: list[int] = []
    real = agent._install_heartbeat_agent
    monkeypatch.setattr(agent, "_install_heartbeat_agent", lambda: calls.append(1) or real())

    snapshot = agent.require_fresh_derived_spec(HEARTBEAT, None)

    assert calls == []
    assert snapshot is not None
    assert snapshot.spec == _read(agents_dir / HEARTBEAT_AGENT_FILENAME)


def test_a_default_edit_the_copy_does_not_carry_does_not_re_derive(agents_dir, monkeypatch) -> None:
    spec = _default_spec(["learn_add", "cron_add"])
    spec["allowedTools"].append("fs_write")
    spec["mcpServers"]["other"]["autoApprove"] = ["anything"]
    _write(agents_dir / AGENT_FILENAME, spec)

    calls: list[int] = []
    real = agent._install_heartbeat_agent
    monkeypatch.setattr(agent, "_install_heartbeat_agent", lambda: calls.append(1) or real())

    assert agent.require_fresh_derived_spec(HEARTBEAT, None) is not None
    assert calls == []


def test_a_copy_with_no_include_mcp_json_is_re_derived(agents_dir) -> None:
    """kiro-cli reads a missing ``includeMcpJson`` as true and merges the global
    ``mcp.json``, whose auto-approvals the default-spec comparison never sees."""
    path = agents_dir / HEARTBEAT_AGENT_FILENAME
    spec = _read(path)
    del spec["includeMcpJson"]
    _write(path, spec)

    agent.require_fresh_derived_spec(HEARTBEAT, None)

    assert _read(path)["includeMcpJson"] is False


def test_a_copy_with_include_mcp_json_true_is_re_derived(agents_dir) -> None:
    path = agents_dir / HEARTBEAT_AGENT_FILENAME
    spec = _read(path)
    spec["includeMcpJson"] = True
    _write(path, spec)

    agent.require_fresh_derived_spec(HEARTBEAT, None)

    assert _read(path)["includeMcpJson"] is False


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            lambda spec: spec["mcpServers"]["kirocrew-core"].__setitem__("autoApprove", ["*"]),
            id="widened-auto-approve",
        ),
        pytest.param(
            lambda spec: spec["mcpServers"].__setitem__(
                "extra", {"command": "x", "args": [], "autoApprove": ["*"]}
            ),
            id="extra-server",
        ),
        pytest.param(lambda spec: spec.__setitem__("allowedTools", ["*"]), id="allowed-tools"),
        pytest.param(lambda spec: spec["tools"].append("*"), id="tools"),
    ],
)
def test_a_copy_edited_outside_the_installer_is_re_derived(agents_dir, edit) -> None:
    """The check reads the copy itself, so a write the installer did not make -- a hand
    edit, a racing writer, another build -- is caught with the default spec untouched."""
    path = agents_dir / HEARTBEAT_AGENT_FILENAME
    spec = _read(path)
    edit(spec)
    _write(path, spec)

    snapshot = agent.require_fresh_derived_spec(HEARTBEAT, None)

    assert snapshot is not None
    assert snapshot.spec == _read(path)
    assert _heartbeat_auto_approve(agents_dir) == ["learn_add", "cron_add"]
    assert set(_read(path)["mcpServers"]) == {"kirocrew-core"}
    assert "allowedTools" not in _read(path)
    assert _read(path)["tools"] == ["@kirocrew-core"]


def test_a_current_copy_passes_without_any_recorded_bookkeeping(agents_dir, monkeypatch) -> None:
    """A data home that did not write the copy has no record of it, and a current copy
    still starts there without being rewritten."""
    from kiro_crew import agent_state

    agent_state.set_mirrored_from(HEARTBEAT, None)
    agent_state.set_mirrored_stat(HEARTBEAT, None)
    calls: list[int] = []
    real = agent._install_heartbeat_agent
    monkeypatch.setattr(agent, "_install_heartbeat_agent", lambda: calls.append(1) or real())

    assert agent.require_fresh_derived_spec(HEARTBEAT, None) is not None
    assert calls == []


def test_a_re_derive_that_leaves_a_stale_copy_refuses_the_start(agents_dir, monkeypatch) -> None:
    """An installer that reports success while the file still holds the old grants (a
    racing writer landing after it) does not get the start through."""
    _drop_cron_add_without_rederiving(agents_dir)
    monkeypatch.setattr(agent, "_install_heartbeat_agent", lambda: None)

    with pytest.raises(agent.DerivedSpecStale) as caught:
        agent.require_fresh_derived_spec(HEARTBEAT, None)
    assert "after a re-derive" in str(caught.value)


def test_a_volatile_env_value_alone_is_not_a_mismatch(agents_dir, monkeypatch) -> None:
    """A launcher re-stamping a per-launch nonce into the copy is not a grant change."""
    from kiro_crew.agent_spec_format import volatile_env_keys

    keys = sorted(volatile_env_keys())
    if not keys:
        pytest.skip("no volatile env keys on this build")
    path = agents_dir / HEARTBEAT_AGENT_FILENAME
    spec = _read(path)
    spec["mcpServers"]["kirocrew-core"]["env"] = {keys[0]: "nonce-a"}
    _write(path, spec)
    default = _read(agents_dir / AGENT_FILENAME)
    default["mcpServers"]["kirocrew-core"]["env"] = {keys[0]: "nonce-b"}
    _write(agents_dir / AGENT_FILENAME, default)

    calls: list[int] = []
    real = agent._install_heartbeat_agent
    monkeypatch.setattr(agent, "_install_heartbeat_agent", lambda: calls.append(1) or real())

    assert agent.require_fresh_derived_spec(HEARTBEAT, None) is not None
    assert calls == []


def test_the_installer_and_the_check_derive_the_same_servers(agents_dir) -> None:
    default = _read(agents_dir / AGENT_FILENAME)
    spec = _read(agents_dir / HEARTBEAT_AGENT_FILENAME)
    assert spec["mcpServers"] == worker_agent.heartbeat_mcp_servers(default)
    assert spec["mcpServers"]["kirocrew-core"]["args"] == ["core"]


def test_a_missing_default_spec_refuses_the_start(agents_dir) -> None:
    (agents_dir / AGENT_FILENAME).unlink()
    with pytest.raises(agent.DerivedSpecStale) as caught:
        agent.require_fresh_derived_spec(HEARTBEAT, None)
    assert "missing" in str(caught.value)


def test_an_unreadable_default_spec_refuses_the_start(agents_dir) -> None:
    (agents_dir / AGENT_FILENAME).write_text("{ not json", encoding="utf-8")
    with pytest.raises(agent.DerivedSpecStale) as caught:
        agent.require_fresh_derived_spec(HEARTBEAT, None)
    assert "cannot be read" in str(caught.value)


def test_a_project_local_heartbeat_spec_refuses_the_start(agents_dir, tmp_path) -> None:
    work = tmp_path / "checkout"
    shadow = work / ".kiro" / "agents" / HEARTBEAT_AGENT_FILENAME
    shadow.parent.mkdir(parents=True)
    _write(shadow, {"name": HEARTBEAT, "tools": ["*"], "allowedTools": ["*"]})

    with pytest.raises(agent.DerivedSpecStale) as caught:
        agent.require_fresh_derived_spec(HEARTBEAT, work)
    assert str(shadow) in str(caught.value)


def test_the_post_load_check_ignores_a_default_edit_the_copy_does_not_carry(
    agents_dir,
) -> None:
    snapshot = agent.require_fresh_derived_spec(HEARTBEAT, None)
    assert snapshot is not None
    spec = _default_spec(["learn_add", "cron_add"])
    spec["allowedTools"].append("fs_write")
    _write(agents_dir / AGENT_FILENAME, spec)

    agent.require_unchanged_derived_spec(snapshot)


def test_the_post_load_check_ends_a_session_whose_copy_moved(agents_dir) -> None:
    snapshot = agent.require_fresh_derived_spec(HEARTBEAT, None)
    assert snapshot is not None
    _write(agents_dir / AGENT_FILENAME, _default_spec(["learn_add"]))

    with pytest.raises(agent.DerivedSpecStale):
        agent.require_unchanged_derived_spec(snapshot)


def test_the_copy_and_the_check_name_the_same_servers(agents_dir) -> None:
    spec = _read(agents_dir / HEARTBEAT_AGENT_FILENAME)
    assert tuple(spec["mcpServers"]) == worker_agent.HEARTBEAT_MIRRORED_SERVERS


def test_an_agent_that_copies_nothing_is_still_out_of_scope(agents_dir) -> None:
    assert agent.require_fresh_derived_spec("kirocrew-lite", None) is None
    assert agent.require_fresh_derived_spec(None, None) is None


def test_the_installer_holds_both_writer_locks_in_the_worker_order(agents_dir, monkeypatch) -> None:
    """The main-spec read and the copy write are one critical section, so a copy derived
    from an older main spec cannot land after one derived from a newer main spec."""
    import contextlib

    from kiro_crew.apps import bridges

    events: list[str] = []
    real_write = agent._atomic_json_write

    @contextlib.contextmanager
    def recording_mcp_lock(*args, **kwargs):
        events.append("mcp+")
        yield
        events.append("mcp-")

    @contextlib.contextmanager
    def recording_agents_lock(path):
        events.append("agents+")
        yield
        events.append("agents-")

    def recording_write(path, data):
        events.append(f"write:{Path(path).name}")
        real_write(path, data)

    monkeypatch.setattr(bridges, "_mcp_lock", recording_mcp_lock)
    monkeypatch.setattr(agent, "agents_spec_lock", recording_agents_lock)
    monkeypatch.setattr(agent, "_atomic_json_write", recording_write)
    agent._install_heartbeat_agent()

    assert events == [
        "agents+",
        "mcp+",
        f"write:{HEARTBEAT_AGENT_FILENAME}",
        "mcp-",
        "agents-",
    ], events
