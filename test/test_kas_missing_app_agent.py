"""A session whose agent has no definition says why, instead of quoting a failed open.

Disabling an app removes the agent specs it registered, but a Crew Member bound to
one of them stays configured so that re-enabling the app restores it untouched.
Starting that member's session on KAS reaches the projection with no spec to read.
These pin the handled refusal: a typed, non-transient error whose message names the
app and the fix rather than the projection's ``[Errno 2]`` file open, and the chat
row kind that withholds Resume for it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import members as members_mod
from kiro_crew.acp import kas_agents as kas_agents_mod
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.harness import kas as kas_mod
from kiro_crew.acp.session_handle import AcpAgentUnavailable, AcpRuntimeError
from kiro_crew.acp.types import ACP_BACKEND_KAS
from kiro_crew.apps import manager as manager_mod
from kiro_crew.apps.manager import InstalledApp, _write_installed
from kiro_crew.config import paths as paths_mod
from kiro_crew.mcp_gateway import session_servers as session_servers_mod

APP = "demo-app"
LABEL = "Demo App"


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """An isolated data home and an agents directory holding no spec at all."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(
        session_servers_mod, "injection_server_names", lambda overlay, agent, **_: frozenset()
    )
    return agents


def _install_app(*, enabled: bool, declared: str = "worker") -> None:
    """An installed app whose manifest declares one agent, as the app tree holds it."""
    root = manager_mod.app_dir(APP)
    (root / "agents").mkdir(parents=True)
    (root / manager_mod.APP_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": APP,
                "version": "1.0.0",
                "displayName": LABEL,
                "agents": ["agents/worker.json"],
            }
        ),
        encoding="utf-8",
    )
    (root / "agents" / "worker.json").write_text(
        json.dumps({"name": declared, "prompt": "p", "tools": []}), encoding="utf-8"
    )
    _write_installed(APP, InstalledApp(name=APP, version="1.0.0", enabled=enabled))


async def _start(agent: str, tmp_path: Path, session_key: str = "") -> None:
    await harness_for(ACP_BACKEND_KAS).session_extras(
        agent, work_dir=str(tmp_path / "work"), session_key=session_key
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bound_to", ["worker", f"{APP}--worker"])
async def test_disabled_app_agent_refuses_with_the_app_and_the_fix(agents_dir, tmp_path, bound_to):
    """The declared name and the namespaced file stem both resolve to the app."""
    _install_app(enabled=False)

    with pytest.raises(AcpAgentUnavailable) as caught:
        await _start(bound_to, tmp_path)

    message = str(caught.value)
    assert f"comes from the {LABEL} app, which is disabled" in message
    assert f"Enable {LABEL} in Apps \u203a Library" in message
    assert "Errno" not in message and str(agents_dir) not in message
    assert caught.value.agent == bound_to
    # A subclass of the runtime error every session-start caller already handles,
    # and a verdict the retry layer reads structurally: no retry ladder for it.
    assert isinstance(caught.value, AcpRuntimeError)
    assert caught.value.transient is False


@pytest.mark.asyncio
async def test_member_session_names_the_crew_member(agents_dir, tmp_path, monkeypatch):
    _install_app(enabled=False)
    monkeypatch.setattr(
        members_mod,
        "member_identity_for_session",
        lambda key: {"slug": "scout", "name": "scout"} if key == "member-scout" else None,
    )

    with pytest.raises(AcpAgentUnavailable) as caught:
        await _start("worker", tmp_path, session_key="member-scout")

    assert str(caught.value) == (
        f'Crew member "scout" comes from the {LABEL} app, which is disabled. '
        f"Enable {LABEL} in Apps \u203a Library to use this crew member."
    )


@pytest.mark.asyncio
async def test_enabled_app_with_a_missing_spec_says_to_register_it_again(agents_dir, tmp_path):
    _install_app(enabled=True)

    with pytest.raises(AcpAgentUnavailable) as caught:
        await _start("worker", tmp_path)

    message = str(caught.value)
    assert f"comes from the {LABEL} app, but its agent definition" in message
    assert "Disable and re-enable" in message


@pytest.mark.asyncio
async def test_agent_no_app_declares_gets_the_generic_reason(agents_dir, tmp_path):
    _install_app(enabled=False, declared="someone-else")

    with pytest.raises(AcpAgentUnavailable) as caught:
        await _start("worker", tmp_path)

    assert str(caught.value) == 'The agent "worker" can\'t start: its agent definition is missing.'


@pytest.mark.asyncio
async def test_a_spec_on_disk_is_still_projected(agents_dir, tmp_path):
    """Only a missing definition takes the new refusal; a present spec runs as before."""
    _install_app(enabled=True)
    (agents_dir / f"{APP}--worker.json").write_text(
        json.dumps({"name": "worker", "prompt": "p", "tools": []}), encoding="utf-8"
    )

    extras = await harness_for(ACP_BACKEND_KAS).session_extras(
        "worker", work_dir=str(tmp_path / "work")
    )

    assert extras.custom_agents


@pytest.mark.asyncio
async def test_an_unreadable_spec_keeps_the_translation_refusal(agents_dir, tmp_path, monkeypatch):
    """A spec that exists but cannot be read is not an absent one."""
    _install_app(enabled=False)
    monkeypatch.setattr(kas_agents_mod, "agent_spec_absent", lambda d, a: False)

    def _unreadable(d, a):
        raise kas_agents_mod.KasAgentTranslationError("agent spec x is not an object")

    monkeypatch.setattr(kas_agents_mod, "load_agent_spec", _unreadable)

    with pytest.raises(AcpRuntimeError, match="cannot project agent") as caught:
        await _start("worker", tmp_path)

    assert not isinstance(caught.value, AcpAgentUnavailable)


def test_resolve_projected_spec_types_the_absent_spec(agents_dir, tmp_path):
    with pytest.raises(kas_agents_mod.KasAgentSpecAbsent) as caught:
        kas_mod.resolve_projected_spec(agents_dir, "worker", str(tmp_path / "work"))

    # Still the translation refusal for every caller that only knows that type.
    assert isinstance(caught.value, kas_agents_mod.KasAgentTranslationError)
    assert caught.value.agent_id == "worker"


def test_owning_app_lookup_reads_disabled_apps(agents_dir):
    _install_app(enabled=False)

    owner = manager_mod.owning_app_for_agent("worker")

    assert owner == manager_mod.AgentOwner(app=APP, label=LABEL, enabled=False)
    assert manager_mod.owning_app_for_agent(f"{APP}--worker") == owner
    assert manager_mod.owning_app_for_agent("unrelated") is None


def test_owning_app_lookup_ignores_an_agent_path_outside_the_app(agents_dir, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"name": "worker"}), encoding="utf-8")
    root = manager_mod.app_dir(APP)
    root.mkdir(parents=True)
    (root / manager_mod.APP_MANIFEST_FILENAME).write_text(
        json.dumps({"name": APP, "version": "1.0.0", "agents": ["../../../outside.json"]}),
        encoding="utf-8",
    )
    _write_installed(APP, InstalledApp(name=APP, version="1.0.0", enabled=False))

    assert manager_mod.owning_app_for_agent("worker") is None


def test_owning_app_lookup_reads_a_builtin_from_its_package_root(agents_dir, tmp_path, monkeypatch):
    """A builtin's agents ship in the package, not in its installed data-home tree."""
    package = tmp_path / "package" / "demo_app"
    (package / "agents").mkdir(parents=True)
    (package / manager_mod.APP_MANIFEST_FILENAME).write_text(
        json.dumps(
            {"name": APP, "version": "1.0.0", "displayName": LABEL, "agents": ["agents/w.json"]}
        ),
        encoding="utf-8",
    )
    (package / "agents" / "w.json").write_text(json.dumps({"name": "worker"}), encoding="utf-8")
    _write_installed(APP, InstalledApp(name=APP, version="1.0.0", enabled=False))
    monkeypatch.setattr(
        manager_mod, "shipped_builtin_app_root", lambda name: package if name == APP else None
    )

    assert manager_mod.owning_app_for_agent("worker") == manager_mod.AgentOwner(
        app=APP, label=LABEL, enabled=False
    )


def test_owning_app_lookup_reads_nothing_the_screen_refuses(agents_dir, monkeypatch):
    """No metadata file is read unscreened: a refused ``installed.json`` skips the app."""
    from kiro_crew import agent_discovery

    _install_app(enabled=False)
    real_screen = manager_mod._screened_spec_path
    monkeypatch.setattr(
        manager_mod,
        "_screened_spec_path",
        lambda p: None if p.name == manager_mod.INSTALLED_META_FILENAME else real_screen(p),
    )

    assert manager_mod.owning_app_for_agent("worker") is None

    monkeypatch.setattr(manager_mod, "_screened_spec_path", real_screen)
    read = []
    real_reader = agent_discovery._read_agent_spec

    def _recording(path, *, operation, source):
        read.append((Path(path).name, operation))
        return real_reader(path, operation=operation, source=source)

    monkeypatch.setattr(agent_discovery, "_read_agent_spec", _recording)

    assert manager_mod.owning_app_for_agent("worker") is not None
    assert {name for name, op in read if op == "app_agent_owner"} == {
        manager_mod.INSTALLED_META_FILENAME,
        manager_mod.APP_MANIFEST_FILENAME,
        "worker.json",
    }


def test_owning_app_lookup_screens_an_entry_before_probing_it(agents_dir, monkeypatch):
    """An app entry is screened before anything follows it; a linked entry is never probed."""
    _install_app(enabled=False)
    (manager_mod.apps_dir() / "stray-file").write_text("x", encoding="utf-8")
    entries = {p.name for p in manager_mod.apps_dir().iterdir()}
    events: list[tuple[str, str]] = []
    real_screen = manager_mod._screened_spec_path
    real_is_dir = Path.is_dir

    def _screen(p):
        if p.parent.parent == manager_mod.apps_dir():
            events.append(("screen", p.parent.name))
        return real_screen(p)

    def _is_dir(self):
        if self.parent == manager_mod.apps_dir() and self.name in entries:
            events.append(("is_dir", self.name))
        return real_is_dir(self)

    monkeypatch.setattr(manager_mod, "_screened_spec_path", _screen)
    monkeypatch.setattr(Path, "is_dir", _is_dir)

    assert manager_mod.owning_app_for_agent("worker") is not None
    assert ("is_dir", APP) not in events and ("is_dir", "stray-file") not in events
    assert ("screen", APP) in events


def test_chat_row_withholds_resume_for_an_unavailable_agent():
    from kiro_crew.dashboard.chat_runner import APP_AGENT_REFUSED_KIND, _terminal_error_meta

    exc = AcpAgentUnavailable("the reason", agent="worker")

    assert _terminal_error_meta(exc) == {"kind": APP_AGENT_REFUSED_KIND}
    assert _terminal_error_meta(AcpRuntimeError("other")) is None
