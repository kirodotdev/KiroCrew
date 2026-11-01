"""App MCP registration follows the shared agent home's owner.

``_register_mcp_servers`` and ``_deregister_mcp_servers`` write ``kirocrew.json``, the
same shared spec the managed-spec rebuild guards with ``_decline_shared_agent_home``. An
instance on a non-default ``KIROCREW_HOME`` runs the boot reconcile over its own
installed apps and would otherwise rewrite the live gateway's app entries with its own
interpreter (``_targets_gateway_module`` pins ``sys.executable``). The tests pin that
the app MCP writer asks the same guard: a foreign-owned shared spec is left
byte-identical, a default-home instance still writes, and the dedicated
``isolated_agents_dir`` is still accepted. The app agent spec and launch-copy
removals (deregister and the upgrade prune) raise ``SharedAgentHomeRefused`` on the
same foreign-owned directory and delete nothing.
The guard's own arms are pinned in ``test_agent_home_isolation*.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_home_isolation_helpers import _capture_sel, _durable_checkout, _spec_pinned_to
from app_bridges_helpers import _stub_venv_probe  # noqa: F401
from app_bridges_helpers import app_env_fixture  # noqa: F401
from app_bridges_helpers import _make_app_source

from kiro_crew.agent import SharedAgentHomeRefused
from kiro_crew.apps.bridges import _deregister_mcp_servers, _register_mcp_servers
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.config.paths import isolated_agents_dir

_SERVER = {"command": "echo", "args": []}


@pytest.fixture(autouse=True)
def _pin_default_home(_floor_monkeypatch, tmp_path):
    """Keep the default-home resolution off the operator's real ``~/.kiro/crew``.

    The guard compares the ``KIROCREW_HOME`` override against the DEFAULT home, so
    without this pin the comparison would resolve, and create, the real one.
    """
    from kiro_crew.config import paths

    _floor_monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
    _floor_monkeypatch.setattr(
        paths, "_resolve_default_home", lambda: tmp_path / "pinned-default-home"
    )
    _floor_monkeypatch.setattr(
        paths, "_write_recovery_breadcrumb", lambda data_home: None, raising=False
    )
    _floor_monkeypatch.delenv("KIRO_HOME", raising=False)
    _floor_monkeypatch.delenv("KIROCREW_POD", raising=False)


def _share(monkeypatch, shared: Path) -> Path:
    """Make *shared* the app MCP write target AND the ambient agents dir.

    ``app_env`` redirects ``_mcp_json_path`` to a private file; the guard admits a
    private target without asking, so the test lines the target up with what the
    ambient environment resolves, which is what makes it shared.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew.apps import bridges as bridges_mod

    shared.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", shared)
    monkeypatch.setattr(bridges_mod, "_mcp_json_path", lambda: shared / "kirocrew.json")
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", shared)
    monkeypatch.setattr(agent_mod, "ambient_agents_dir", lambda: shared)
    _durable_checkout(monkeypatch, agent_mod)
    return shared / "kirocrew.json"


def _installed_manifest(tmp_path, app_env) -> AppManifest:
    install_app(_make_app_source(tmp_path, mcpServers={"srv": _SERVER}))
    return AppManifest.from_json_file(app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME)


def test_a_foreign_owned_shared_spec_is_left_byte_identical_by_app_mcp_registration(
    tmp_path, app_env, monkeypatch
):
    """The reported shape: a gateway on a scratch ``KIROCREW_HOME`` registers its apps
    at boot and must not touch the live gateway's ``kirocrew.json``."""
    from kiro_crew import agent as agent_mod

    events = _capture_sel(monkeypatch, agent_mod)
    spec_path = _share(monkeypatch, tmp_path / "agents")
    foreign = _spec_pinned_to(tmp_path / "someone-elses-home")
    spec_path.write_text(foreign, encoding="utf-8")
    manifest = _installed_manifest(tmp_path, app_env)

    registered = _register_mcp_servers("test-app", manifest)

    assert registered == []
    assert spec_path.read_bytes() == foreign.encode("utf-8")
    denied = [e for e in events if e.get("outcome") == "denied"]
    assert len(denied) == 1 and denied[0]["operation"] == "agent_home_write"


def test_a_foreign_owned_shared_spec_keeps_its_app_entries_through_deregistration(
    tmp_path, app_env, monkeypatch
):
    """The boot reconcile scrubs every app the INSTANCE has disabled; on a foreign
    home that would remove the owner's live entries."""
    spec_path = _share(monkeypatch, tmp_path / "agents")
    spec = json.loads(_spec_pinned_to(tmp_path / "someone-elses-home"))
    spec["mcpServers"]["test-app:srv"] = dict(_SERVER)
    before = (json.dumps(spec, indent=2) + "\n").encode("utf-8")
    spec_path.write_bytes(before)

    assert _deregister_mcp_servers("test-app") == 0
    assert spec_path.read_bytes() == before


def test_a_relocated_home_can_remove_the_app_entries_it_created(tmp_path, app_env, monkeypatch):
    spec_path = _share(monkeypatch, tmp_path / "agents")
    manifest = _installed_manifest(tmp_path, app_env)

    assert _register_mcp_servers("test-app", manifest) == ["test-app:srv"]
    assert _deregister_mcp_servers("test-app") == 1
    servers = json.loads(spec_path.read_text(encoding="utf-8"))["mcpServers"]
    assert "test-app:srv" not in servers
    assert servers["kirocrew-core"]["env"]["KIROCREW_HOME"] == str(app_env["home"])


def test_a_relocated_home_creates_no_shared_spec_it_could_not_mark_as_its_own(
    tmp_path, app_env, monkeypatch
):
    from kiro_crew import agent as agent_mod

    spec_path = _share(monkeypatch, tmp_path / "agents")
    manifest = _installed_manifest(tmp_path, app_env)
    monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", lambda name, **kw: None)

    assert _register_mcp_servers("test-app", manifest) == []
    assert not spec_path.exists()


def test_a_default_home_instance_still_registers_app_mcp_servers(tmp_path, app_env, monkeypatch):
    from kiro_crew.config import paths

    # An override equal to the default home IS the default-home instance.
    monkeypatch.setattr(paths, "_resolve_default_home", lambda: app_env["home"].resolve())
    spec_path = _share(monkeypatch, tmp_path / "agents")
    spec_path.write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
    manifest = _installed_manifest(tmp_path, app_env)

    assert _register_mcp_servers("test-app", manifest) == ["test-app:srv"]
    assert "test-app:srv" in json.loads(spec_path.read_text(encoding="utf-8"))["mcpServers"]


def test_the_isolated_agents_dir_still_accepts_app_mcp_registration(tmp_path, app_env, monkeypatch):
    """``<data home>/kiro/agents`` is the one agents dir a non-default home owns
    outright; the guard exempts it before reading any spec's provenance."""
    spec_path = _share(monkeypatch, isolated_agents_dir(app_env["home"].resolve()))
    spec_path.write_text(_spec_pinned_to(tmp_path / "someone-elses-home"), encoding="utf-8")
    manifest = _installed_manifest(tmp_path, app_env)

    assert _register_mcp_servers("test-app", manifest) == ["test-app:srv"]
    assert "test-app:srv" in json.loads(spec_path.read_text(encoding="utf-8"))["mcpServers"]


def _app_files(agents_dir: Path) -> list[Path]:
    """One stale app agent spec and one stale launch copy for ``test-app``."""
    from kiro_crew.apps import agent_launch
    from kiro_crew.apps.agent_launch import app_agent_launch_name

    spec = agents_dir / "test-app--dropped.json"
    spec.write_text(json.dumps({"name": "dropped"}), encoding="utf-8")
    launch = agents_dir / f"{app_agent_launch_name('test-app', 'dropped')}.json"
    launch.write_text(
        json.dumps({"name": launch.stem, "description": agent_launch.OWNER_MARKER}),
        encoding="utf-8",
    )
    return [spec, launch]


def test_a_foreign_owned_shared_dir_keeps_app_agent_files_through_deregistration(
    tmp_path, app_env, monkeypatch
):
    """The boot reconcile deregisters every app this INSTANCE denies or disabled; on
    a foreign home that would delete the owner's live agent specs."""
    from kiro_crew.apps.bridges import _deregister_agents

    spec_path = _share(monkeypatch, tmp_path / "agents")
    spec_path.write_text(_spec_pinned_to(tmp_path / "someone-elses-home"), encoding="utf-8")
    files = _app_files(spec_path.parent)

    with pytest.raises(SharedAgentHomeRefused):
        _deregister_agents("test-app")
    assert all(f.exists() for f in files)


def test_a_foreign_owned_shared_dir_keeps_app_agent_files_through_the_upgrade_prune(
    tmp_path, app_env, monkeypatch
):
    from kiro_crew.apps.bridges import _prune_stale_app_resources

    spec_path = _share(monkeypatch, tmp_path / "agents")
    spec_path.write_text(_spec_pinned_to(tmp_path / "someone-elses-home"), encoding="utf-8")
    manifest = _installed_manifest(tmp_path, app_env)
    files = _app_files(spec_path.parent)

    with pytest.raises(SharedAgentHomeRefused):
        _prune_stale_app_resources("test-app", manifest, app_env["home"] / "apps" / "test-app")

    assert all(f.exists() for f in files)


def test_a_default_home_instance_still_prunes_dropped_app_agents(tmp_path, app_env, monkeypatch):
    from kiro_crew.apps.bridges import _prune_stale_app_resources
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "_resolve_default_home", lambda: app_env["home"].resolve())
    spec_path = _share(monkeypatch, tmp_path / "agents")
    spec_path.write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
    manifest = _installed_manifest(tmp_path, app_env)
    files = _app_files(spec_path.parent)

    _prune_stale_app_resources("test-app", manifest, app_env["home"] / "apps" / "test-app")

    assert not any(f.exists() for f in files)


def test_deregister_app_reports_a_refused_agent_removal(tmp_path, app_env, monkeypatch):
    """A refusal is an error in the result, not a clean "removed 0 agent(s)"."""
    from kiro_crew.apps.bridges import deregister_app

    spec_path = _share(monkeypatch, tmp_path / "agents")
    spec_path.write_text(_spec_pinned_to(tmp_path / "someone-elses-home"), encoding="utf-8")
    files = _app_files(spec_path.parent)

    result = deregister_app("test-app")

    assert any("agent deregistration failed" in e for e in result.errors)
    assert all(f.exists() for f in files)


def test_the_ownership_check_and_every_delete_of_a_deregistration_share_the_spec_lock(
    tmp_path, app_env, monkeypatch
):
    """Ownership must not change between the guard's answer and an unlink."""
    import contextlib

    from kiro_crew import agent as agent_mod
    from kiro_crew.apps import agent_launch
    from kiro_crew.apps.bridges import _deregister_agents
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "_resolve_default_home", lambda: app_env["home"].resolve())
    spec_path = _share(monkeypatch, tmp_path / "agents")
    spec_path.write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
    files = _app_files(spec_path.parent)

    held = []
    seen = []
    real_lock = agent_mod.agents_spec_lock
    real_guard = agent_mod._declined_foreign_spec_write
    real_remove = agent_launch.remove_app_agent_launches

    @contextlib.contextmanager
    def recording_lock(agents_dir):
        with real_lock(agents_dir):
            held.append(True)
            try:
                yield
            finally:
                held.pop()

    def recording_guard(path):
        seen.append(("guard", bool(held)))
        return real_guard(path)

    def recording_remove(*args, **kwargs):
        seen.append(("launches", bool(held)))
        return real_remove(*args, **kwargs)

    monkeypatch.setattr(agent_mod, "agents_spec_lock", recording_lock)
    monkeypatch.setattr(agent_mod, "_declined_foreign_spec_write", recording_guard)
    monkeypatch.setattr(agent_launch, "remove_app_agent_launches", recording_remove)

    assert _deregister_agents("test-app") == 2

    assert seen == [("guard", True), ("launches", True)]
    assert not any(f.exists() for f in files)
