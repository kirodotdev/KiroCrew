"""Two conductor installs racing a card apply keep every approved server.

Each conductor installer reads the template MCP additions record and then writes
its spec. Two installs of the same conductor (two rebuilds at once) could
interleave there: the install that read the record BEFORE a card recorded a new
server wrote AFTER the install that read it after, so the spec on disk lost the
server until some later rebuild. The installers now hold ``agents_spec_lock`` from
the read to the write, and these tests drive that exact interleave with events.

The rig, per installer: install A merges the record and parks; the card records a
server; install B starts. B either finishes (no lock: it reads the new record and
writes first) or stops at the spec lock (A still holds it). Only then is A let go,
so the order is fixed by events, never by timing.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent, template_mcp_additions
from kiro_crew.agent_files import (
    CONDUCTOR_AGENT_FILENAME,
    LEDGER_CONDUCTOR_AGENT_FILENAME,
    PIPELINE_CONDUCTOR_AGENT_FILENAME,
    SECURITY_CONDUCTOR_AGENT_FILENAME,
)
from kiro_crew.agent_materialization import conductor_agents
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.platform import governance

_INSTALLERS = [
    pytest.param(
        "kirocrew-conductor",
        CONDUCTOR_AGENT_FILENAME,
        "_install_conductor_agent",
        id="conductor",
    ),
    pytest.param(
        "kirocrew-ledger-conductor",
        LEDGER_CONDUCTOR_AGENT_FILENAME,
        "_install_ledger_conductor_agent",
        id="ledger-alias",
    ),
    pytest.param(
        "kirocrew-pipeline-conductor",
        PIPELINE_CONDUCTOR_AGENT_FILENAME,
        "_install_pipeline_conductor_agent",
        id="pipeline",
    ),
    pytest.param(
        "kirocrew-security-conductor",
        SECURITY_CONDUCTOR_AGENT_FILENAME,
        "_install_security_conductor_agent",
        id="security",
    ),
]

_ENTRY = {"command": "/opt/fetch/bin/mcp-fetch", "args": ["--stdio"]}

#: A bound on a wait that only a broken rig reaches; the order never rests on it.
_HANG_GUARD_SECS = 60.0


@pytest.fixture
def agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private agents directory and crew home, an ungoverned ceiling, an accepting kiro-cli."""
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: agents)
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )
    monkeypatch.setattr(
        agent,
        "build_agent_config",
        lambda: {
            "name": "kirocrew",
            "prompt": "file://x",
            "mcpServers": {
                "kirocrew-core": {
                    "command": "/resolved/kirocrew",
                    "args": ["mcp-core"],
                },
            },
            "tools": ["fs_write", "@kirocrew-core"],
            "allowedTools": ["@kirocrew-core"],
        },
    )
    monkeypatch.setattr(
        agent, "_kirocrew_mcp_invocation", lambda sub: ("/resolved/kirocrew", [sub])
    )
    monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: governance.may_skip_gate(ref, None))
    return agents


def _servers(agents_dir: Path, filename: str) -> dict[str, Any]:
    return json.loads((agents_dir / filename).read_text(encoding="utf-8"))["mcpServers"]


@pytest.mark.parametrize(("name", "filename", "installer"), _INSTALLERS)
def test_a_server_approved_between_two_racing_installs_stays_in_the_spec(
    agents_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    filename: str,
    installer: str,
) -> None:
    a_merged = threading.Event()
    release_a = threading.Event()
    b_moved = threading.Event()
    errors: list[BaseException] = []

    real_merge = conductor_agents._merge_template_additions

    def merge(config: dict[str, Any], template: str) -> None:
        real_merge(config, template)
        if threading.current_thread().name == "install-a":
            a_merged.set()
            assert release_a.wait(_HANG_GUARD_SECS), "install A was never released"

    real_lock = agent.agents_spec_lock

    @contextlib.contextmanager
    def spec_lock(path: Path) -> Iterator[None]:
        # B reaching the lock is one of the two ways B can "move": the other is
        # finishing. Either one is the point where A may be let go.
        if threading.current_thread().name == "install-b":
            b_moved.set()
        with real_lock(path):
            yield

    monkeypatch.setattr(conductor_agents, "_merge_template_additions", merge)
    monkeypatch.setattr(agent, "agents_spec_lock", spec_lock)

    def run() -> None:
        try:
            assert getattr(agent, installer)() is True
        except BaseException as exc:  # noqa: BLE001 - surfaced on the main thread below
            errors.append(exc)
        finally:
            if threading.current_thread().name == "install-b":
                b_moved.set()

    a = threading.Thread(target=run, name="install-a", daemon=True)
    a.start()
    assert a_merged.wait(_HANG_GUARD_SECS), "install A never reached the record merge"
    # The card apply: the record write, under the record's own lock only.
    template_mcp_additions.add_addition(name, "fetch", _ENTRY, approved_by="owner", card_id="c")
    b = threading.Thread(target=run, name="install-b", daemon=True)
    b.start()
    assert b_moved.wait(_HANG_GUARD_SECS), "install B neither finished nor reached the lock"
    release_a.set()
    a.join(_HANG_GUARD_SECS)
    b.join(_HANG_GUARD_SECS)
    assert not a.is_alive() and not b.is_alive()
    assert errors == []
    assert "fetch" in _servers(
        agents_dir, filename
    ), "the install that read the record before the approval wrote last"
