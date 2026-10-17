"""App resource paths are screened by ``hooks.screen_file_path`` before they are followed.

Install-time containment (``AppManifest.validate``), agent and skill registration
(``bridges``, the agent prune included), the command-agent listing and the backend
entry check ask the shared link screen for a manifest-declared path before
canonicalising it. A path the screen declines is rejected, and it is never handed
to ``Path.resolve``. The screen says whether its decline is lasting or a check
that did not complete, and only the second is retried.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import hooks
from kiro_crew.apps import bridges as bridges_mod
from kiro_crew.apps import execution as execution_mod
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.manifest import (
    PATH_SCREEN_REFUSAL,
    PATH_UNVERIFIED_REFUSAL,
    AppManifest,
    Containment,
    _declared_agent_names,
    app_root_containment,
    file_entry_point_refusal,
    screen_app_path,
)
from kiro_crew.security import UNVERIFIABLE_PATH_PREFIX

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENT_FILE = "my-agent.json"


def _make_app_source(tmp_path: Path, name: str = "test-app") -> Path:
    src = tmp_path / "source" / name
    (src / "agents").mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Test App",
        "description": "A test app",
        "author": "tester",
        "agents": [f"agents/{_AGENT_FILE}"],
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    (src / "agents" / _AGENT_FILE).write_text(
        json.dumps({"name": "my-agent", "model": "auto"}), encoding="utf-8"
    )
    return src


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    monkeypatch.setattr(execution_mod, "third_party_execution_allowed", lambda: True)
    mcp_path = tmp_path / "mcp.json"
    monkeypatch.setattr(bridges_mod, "_mcp_json_path", lambda: mcp_path)
    return {"home": home, "kiro_agents": kiro_agents}


def _screen_refusing(
    monkeypatch, name: str, *, stalled: bool = True, clears: bool = False
) -> list[str]:
    """Make the real screen decline ``name``; return the names ``Path.resolve`` sees.

    The screen's sensitive-path check refuses ``name``. ``stalled`` chooses what its
    wording then says: the check ran out of time (a decline a later attempt may
    pass), or a confirmed match (a lasting refusal). ``clears`` makes the second
    reading answer cleanly, as it does once a short timeout has passed.
    """
    resolved: list[str] = []
    real_resolve = Path.resolve

    def recording_resolve(self, *args, **kwargs):
        resolved.append(self.name)
        return real_resolve(self, *args, **kwargs)

    def first_reading(path_str: str, base_dir: str | None = None) -> bool:
        return Path(path_str).name == name

    def second_reading(path_str: str, base_dir: str | None = None) -> str | None:
        if clears or Path(path_str).name != name:
            return None
        if stalled:
            return f"{UNVERIFIABLE_PATH_PREFIX}. Path: {path_str!r}"
        return f"Blocked: access to sensitive path: {path_str}"

    monkeypatch.setattr(hooks, "is_sensitive_path", first_reading)
    monkeypatch.setattr(hooks, "sensitive_path_refusal", second_reading)
    monkeypatch.setattr(Path, "resolve", recording_resolve)
    return resolved


def _refuse_agent_file(monkeypatch, *, stalled: bool = True) -> list[str]:
    return _screen_refusing(monkeypatch, _AGENT_FILE, stalled=stalled)


def test_install_rejects_an_agent_path_the_link_screen_refuses(tmp_path, app_env, monkeypatch):
    src = _make_app_source(tmp_path)
    resolved = _refuse_agent_file(monkeypatch)

    result = install_app(src)

    assert not result.ok
    assert "agents path" in result.error
    assert _AGENT_FILE not in resolved
    assert not (app_env["home"] / "apps" / "test-app").exists()


def test_register_agents_skips_an_agent_the_link_screen_refuses(tmp_path, app_env, monkeypatch):
    assert install_app(_make_app_source(tmp_path)).ok
    app_root = app_env["home"] / "apps" / "test-app"
    manifest = AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME)
    resolved = _refuse_agent_file(monkeypatch)

    assert bridges_mod._register_agents("test-app", manifest, app_root) == []
    assert _AGENT_FILE not in resolved
    assert not (app_env["kiro_agents"] / "test-app--my-agent.json").exists()


def test_prune_keeps_agent_configs_when_the_link_screen_refuses(tmp_path, app_env, monkeypatch):
    assert install_app(_make_app_source(tmp_path)).ok
    app_root = app_env["home"] / "apps" / "test-app"
    manifest = AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME)
    assert bridges_mod._register_agents("test-app", manifest, app_root) == ["test-app/my-agent"]
    config = app_env["kiro_agents"] / "test-app--my-agent.json"
    assert config.is_file()
    resolved = _refuse_agent_file(monkeypatch)

    bridges_mod._prune_stale_app_resources("test-app", manifest, app_root)

    assert _AGENT_FILE not in resolved
    assert config.is_file()


def test_register_agents_registers_an_agent_the_link_screen_passes(tmp_path, app_env):
    assert install_app(_make_app_source(tmp_path)).ok
    app_root = app_env["home"] / "apps" / "test-app"
    manifest = AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME)

    assert bridges_mod._register_agents("test-app", manifest, app_root) == ["test-app/my-agent"]


def test_app_root_containment_follows_the_root_screen(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "ok.json").write_text("{}", encoding="utf-8")
    assert app_root_containment(app_root / "ok.json", app_root) is Containment.INSIDE

    _screen_refusing(monkeypatch, "app", stalled=True)
    assert app_root_containment(app_root / "ok.json", app_root) is Containment.UNCHECKED

    _screen_refusing(monkeypatch, "app", stalled=False)
    assert app_root_containment(app_root / "ok.json", app_root) is Containment.REFUSED


def test_every_builtin_manifest_passes_containment_against_its_own_root():
    manifests = sorted((_REPO_ROOT / "src/kiro_crew/apps/builtins").glob("*/app.json"))
    assert manifests, "no built-in manifests found"
    offenders = {}
    for app_json in manifests:
        unchecked: list[str] = []
        errors = AppManifest.from_json_file(app_json).validate(
            app_root=app_json.parent, unchecked=unchecked
        )
        if errors or unchecked:
            offenders[app_json.parent.name] = errors + unchecked
    assert offenders == {}


def test_validate_reports_a_screen_refusal_apart_from_traversal(tmp_path, monkeypatch):
    src = _make_app_source(tmp_path)
    manifest = AppManifest.from_json_file(src / APP_MANIFEST_FILENAME)
    _screen_refusing(monkeypatch, _AGENT_FILE)

    errors = manifest.validate(app_root=src)

    assert any(PATH_UNVERIFIED_REFUSAL in e for e in errors), errors
    assert not any("path traversal" in e for e in errors), errors


def test_register_agents_reports_a_screen_refusal_as_retryable(tmp_path, app_env, monkeypatch):
    assert install_app(_make_app_source(tmp_path)).ok
    app_root = app_env["home"] / "apps" / "test-app"
    manifest = AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME)
    _screen_refusing(monkeypatch, _AGENT_FILE)
    io_failures: list[str] = []

    assert bridges_mod._register_agents("test-app", manifest, app_root, io_failures) == []
    assert [Path(p).name for p in io_failures] == [_AGENT_FILE]


def test_register_skills_skips_a_skill_the_link_screen_refuses(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    (app_root / "skills" / "my-skill").mkdir(parents=True)
    (app_root / "skills" / "my-skill" / "SKILL.md").write_text("# s\n", encoding="utf-8")
    skills_root = tmp_path / "skills"
    monkeypatch.setattr(bridges_mod, "_skills_dir", lambda: skills_root)
    manifest = SimpleNamespace(skills=["skills/my-skill"])
    resolved = _screen_refusing(monkeypatch, "my-skill")

    assert bridges_mod._register_skills("someapp", manifest, app_root) == []
    assert "my-skill" not in resolved


def test_backend_entry_refusal_screens_before_following(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "server.py").write_text("x = 1\n", encoding="utf-8")
    assert file_entry_point_refusal("server.py", app_root) == ""
    resolved = _screen_refusing(monkeypatch, "server.py")

    assert file_entry_point_refusal("server.py", app_root) == "path resolution failed"
    assert "server.py" not in resolved


def _builtins_with_one_app(tmp_path: Path) -> Path:
    builtins = tmp_path / "builtins"
    _make_app_source(builtins, name="shipped-app")
    return builtins / "source"


def test_discovery_keeps_a_builtin_whose_paths_the_screen_did_not_check(tmp_path, monkeypatch):
    from kiro_crew.apps.discovery import discover_builtin_apps

    builtins = _builtins_with_one_app(tmp_path)
    assert [a["name"] for a in discover_builtin_apps(builtins)] == ["shipped-app"]
    _screen_refusing(monkeypatch, _AGENT_FILE)

    assert [a["name"] for a in discover_builtin_apps(builtins)] == ["shipped-app"]


def test_discovery_still_skips_a_builtin_whose_path_leaves_its_root(tmp_path):
    from kiro_crew.apps.discovery import discover_builtin_apps

    builtins = _builtins_with_one_app(tmp_path)
    app_json = builtins / "shipped-app" / APP_MANIFEST_FILENAME
    raw = json.loads(app_json.read_text(encoding="utf-8"))
    raw["agents"] = ["../outside.json"]
    app_json.write_text(json.dumps(raw), encoding="utf-8")

    assert discover_builtin_apps(builtins) == []


def test_an_unchecked_builtin_is_not_reported_orphaned(tmp_path, app_env, monkeypatch):
    from kiro_crew.apps import discovery, manager

    builtins = _builtins_with_one_app(tmp_path)
    monkeypatch.setattr(discovery, "_get_builtins_dir", lambda: builtins)
    monkeypatch.setattr(manager, "_BUILTIN_APPS", [])
    monkeypatch.setattr(manager, "_edition_builtin_apps", lambda: [])
    monkeypatch.setattr(manager, "_edition_bundled_app_names", lambda: set())
    monkeypatch.setattr(manager, "_orphaned_builtins_cache", None)
    manager.register_builtin_apps()
    assert manager._read_installed("shipped-app").origin == "builtin"
    _screen_refusing(monkeypatch, _AGENT_FILE)

    assert "shipped-app" not in manager.detect_orphaned_builtins(force_refresh=True)


def test_discovery_skips_a_builtin_whose_error_only_quotes_the_unchecked_wording(tmp_path):
    from kiro_crew.apps.discovery import discover_builtin_apps

    builtins = _builtins_with_one_app(tmp_path)
    app_json = builtins / "shipped-app" / APP_MANIFEST_FILENAME
    raw = json.loads(app_json.read_text(encoding="utf-8"))
    raw["version"] = PATH_UNVERIFIED_REFUSAL
    app_json.write_text(json.dumps(raw), encoding="utf-8")

    assert discover_builtin_apps(builtins) == []


def test_validate_routes_only_unchecked_paths_to_the_unchecked_list(tmp_path, monkeypatch):
    src = _make_app_source(tmp_path)
    manifest = AppManifest.from_json_file(src / APP_MANIFEST_FILENAME)
    _screen_refusing(monkeypatch, _AGENT_FILE)
    unchecked: list[str] = []

    assert manifest.validate(app_root=src, unchecked=unchecked) == []
    assert len(unchecked) == 1 and PATH_UNVERIFIED_REFUSAL in unchecked[0]


def test_register_skills_reports_a_screen_refusal_as_retryable(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    (app_root / "skills" / "my-skill").mkdir(parents=True)
    (app_root / "skills" / "my-skill" / "SKILL.md").write_text("# s\n", encoding="utf-8")
    monkeypatch.setattr(bridges_mod, "_skills_dir", lambda: tmp_path / "skills")
    manifest = SimpleNamespace(skills=["skills/my-skill"])
    _screen_refusing(monkeypatch, "my-skill")
    io_failures: list[str] = []

    assert bridges_mod._register_skills("someapp", manifest, app_root, io_failures) == []
    assert [Path(p).name for p in io_failures] == ["my-skill"]


def test_validate_screens_the_app_root_once_per_pass(tmp_path, monkeypatch):
    src = _make_app_source(tmp_path)
    raw = json.loads((src / APP_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    raw["agents"] = [f"agents/{_AGENT_FILE}", "agents/second.json", "agents/third.json"]
    manifest = AppManifest.from_dict(raw)
    screened: list[str] = []

    real_screen = hooks.screen_file_path

    def counting_screen(raw_path: str, **kwargs):
        screened.append(raw_path)
        return real_screen(raw_path, **kwargs)

    monkeypatch.setattr(hooks, "screen_file_path", counting_screen)

    assert manifest.validate(app_root=src) == []
    assert screened.count(str(src)) == 1
    assert len(screened) == 1 + len(raw["agents"])


def test_validate_reports_a_lasting_screen_refusal_as_refused(tmp_path, monkeypatch):
    src = _make_app_source(tmp_path)
    manifest = AppManifest.from_json_file(src / APP_MANIFEST_FILENAME)
    _refuse_agent_file(monkeypatch, stalled=False)
    unchecked: list[str] = []

    errors = manifest.validate(app_root=src, unchecked=unchecked)

    assert any(PATH_SCREEN_REFUSAL in e for e in errors), errors
    assert unchecked == []


def test_register_agents_does_not_retry_a_lasting_screen_refusal(tmp_path, app_env, monkeypatch):
    assert install_app(_make_app_source(tmp_path)).ok
    app_root = app_env["home"] / "apps" / "test-app"
    manifest = AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME)
    _refuse_agent_file(monkeypatch, stalled=False)
    io_failures: list[str] = []

    assert bridges_mod._register_agents("test-app", manifest, app_root, io_failures) == []
    assert io_failures == []


def test_register_skills_does_not_retry_a_lasting_screen_refusal(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    (app_root / "skills" / "my-skill").mkdir(parents=True)
    (app_root / "skills" / "my-skill" / "SKILL.md").write_text("# s\n", encoding="utf-8")
    monkeypatch.setattr(bridges_mod, "_skills_dir", lambda: tmp_path / "skills")
    manifest = SimpleNamespace(skills=["skills/my-skill"])
    _screen_refusing(monkeypatch, "my-skill", stalled=False)
    io_failures: list[str] = []

    assert bridges_mod._register_skills("someapp", manifest, app_root, io_failures) == []
    assert io_failures == []


def test_prune_still_runs_past_a_lasting_screen_refusal(tmp_path, app_env, monkeypatch):
    assert install_app(_make_app_source(tmp_path)).ok
    app_root = app_env["home"] / "apps" / "test-app"
    manifest = AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME)
    stale = app_env["kiro_agents"] / "test-app--dropped-agent.json"
    stale.write_text(json.dumps({"name": "test-app/dropped-agent"}), encoding="utf-8")
    _refuse_agent_file(monkeypatch, stalled=False)

    bridges_mod._prune_stale_app_resources("test-app", manifest, app_root)

    assert not stale.exists()


def test_discovery_skips_a_builtin_the_screen_refuses(tmp_path, monkeypatch):
    from kiro_crew.apps.discovery import discover_builtin_apps

    builtins = _builtins_with_one_app(tmp_path)
    _refuse_agent_file(monkeypatch, stalled=False)

    assert discover_builtin_apps(builtins) == []


def _registered_test_app(tmp_path: Path, app_env) -> tuple[Path, AppManifest, Path]:
    assert install_app(_make_app_source(tmp_path)).ok
    app_root = app_env["home"] / "apps" / "test-app"
    manifest = AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME)
    assert bridges_mod._register_agents("test-app", manifest, app_root) == ["test-app/my-agent"]
    config = app_env["kiro_agents"] / "test-app--my-agent.json"
    assert config.is_file()
    return app_root, manifest, config


@pytest.mark.parametrize("declined", [_AGENT_FILE, "test-app"], ids=["agent-path", "app-root"])
def test_prune_keeps_the_config_when_a_timed_out_check_passes_on_the_re_ask(
    tmp_path, app_env, monkeypatch, declined
):
    app_root, manifest, config = _registered_test_app(tmp_path, app_env)
    _screen_refusing(monkeypatch, declined, clears=True)

    bridges_mod._prune_stale_app_resources("test-app", manifest, app_root)

    assert config.is_file()


def test_register_agents_retries_when_a_timed_out_check_passes_on_the_re_ask(
    tmp_path, app_env, monkeypatch
):
    app_root, manifest, _config = _registered_test_app(tmp_path, app_env)
    _screen_refusing(monkeypatch, _AGENT_FILE, clears=True)
    io_failures: list[str] = []

    assert bridges_mod._register_agents("test-app", manifest, app_root, io_failures) == []
    assert [Path(p).name for p in io_failures] == [_AGENT_FILE]


def test_a_timed_out_check_that_passes_on_the_re_ask_is_unchecked(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "ok.json").write_text("{}", encoding="utf-8")
    _screen_refusing(monkeypatch, "ok.json", clears=True)

    assert app_root_containment(app_root / "ok.json", app_root) is Containment.UNCHECKED


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (hooks.PathDecline.REFUSED, Containment.REFUSED),
        (hooks.PathDecline.UNCHECKED, Containment.UNCHECKED),
        (OSError("busy"), Containment.UNCHECKED),
    ],
    ids=["refused", "unchecked", "oserror"],
)
def test_screen_app_path_takes_the_decline_kind_from_the_screen(
    tmp_path, monkeypatch, answer, expected
):
    def screen(raw: str, **kwargs):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(hooks, "screen_file_path", screen)

    assert screen_app_path(tmp_path) is expected


def _command_agent_app(tmp_path: Path) -> Path:
    app_root = tmp_path / "app"
    (app_root / "agents").mkdir(parents=True)
    (app_root / "agents" / _AGENT_FILE).write_text(
        json.dumps({"name": "my-agent"}), encoding="utf-8"
    )
    return app_root


def test_command_agent_listing_names_an_agent_the_screen_passes(tmp_path):
    app_root = _command_agent_app(tmp_path)

    assert _declared_agent_names([f"agents/{_AGENT_FILE}"], app_root) == {"my-agent"}


@pytest.mark.parametrize("stalled", [True, False], ids=["unchecked", "refused"])
def test_command_agent_listing_names_no_agent_the_screen_declines(tmp_path, monkeypatch, stalled):
    app_root = _command_agent_app(tmp_path)
    resolved = _screen_refusing(monkeypatch, _AGENT_FILE, stalled=stalled)

    assert _declared_agent_names([f"agents/{_AGENT_FILE}"], app_root) == set()
    assert _AGENT_FILE not in resolved


def test_command_agent_listing_screens_the_app_root_once(tmp_path, monkeypatch):
    app_root = _command_agent_app(tmp_path)
    (app_root / "agents" / "second.json").write_text(
        json.dumps({"name": "second"}), encoding="utf-8"
    )
    screened: list[str] = []
    real_screen = hooks.screen_file_path

    def counting_screen(raw_path: str, **kwargs):
        screened.append(raw_path)
        return real_screen(raw_path, **kwargs)

    monkeypatch.setattr(hooks, "screen_file_path", counting_screen)

    names = _declared_agent_names([f"agents/{_AGENT_FILE}", "agents/second.json"], app_root)

    assert names == {"my-agent", "second"}
    assert screened.count(str(app_root)) == 1


def _bind_a_command_to_the_agent(app_json: Path) -> None:
    raw = json.loads(app_json.read_text(encoding="utf-8"))
    raw["contributes"] = {
        "commands": [{"id": "run-it", "title": "Run it", "prompt": "Go", "agent": "my-agent"}]
    }
    app_json.write_text(json.dumps(raw), encoding="utf-8")


def test_discovery_keeps_a_builtin_whose_command_agent_path_was_not_checked(tmp_path, monkeypatch):
    from kiro_crew.apps.discovery import discover_builtin_apps

    builtins = _builtins_with_one_app(tmp_path)
    _bind_a_command_to_the_agent(builtins / "shipped-app" / APP_MANIFEST_FILENAME)
    assert [a["name"] for a in discover_builtin_apps(builtins)] == ["shipped-app"]
    _screen_refusing(monkeypatch, _AGENT_FILE)

    assert [a["name"] for a in discover_builtin_apps(builtins)] == ["shipped-app"]


def test_validate_reports_a_command_agent_behind_an_unchecked_path_as_unchecked(
    tmp_path, monkeypatch
):
    src = _make_app_source(tmp_path)
    _bind_a_command_to_the_agent(src / APP_MANIFEST_FILENAME)
    manifest = AppManifest.from_json_file(src / APP_MANIFEST_FILENAME)
    _screen_refusing(monkeypatch, _AGENT_FILE)
    unchecked: list[str] = []

    assert manifest.validate(app_root=src, unchecked=unchecked) == []
    assert any("my-agent" in u and PATH_UNVERIFIED_REFUSAL in u for u in unchecked), unchecked


def test_validate_refuses_a_command_agent_behind_a_refused_path(tmp_path, monkeypatch):
    src = _make_app_source(tmp_path)
    _bind_a_command_to_the_agent(src / APP_MANIFEST_FILENAME)
    manifest = AppManifest.from_json_file(src / APP_MANIFEST_FILENAME)
    _refuse_agent_file(monkeypatch, stalled=False)
    unchecked: list[str] = []

    errors = manifest.validate(app_root=src, unchecked=unchecked)

    assert any("not one of this app's own agents" in e for e in errors), errors
    assert unchecked == []


def test_validate_screens_each_path_once_when_a_command_names_an_agent(tmp_path, monkeypatch):
    src = _make_app_source(tmp_path)
    _bind_a_command_to_the_agent(src / APP_MANIFEST_FILENAME)
    manifest = AppManifest.from_json_file(src / APP_MANIFEST_FILENAME)
    screened: list[str] = []
    real_screen = hooks.screen_file_path

    def counting_screen(raw_path: str, **kwargs):
        screened.append(raw_path)
        return real_screen(raw_path, **kwargs)

    monkeypatch.setattr(hooks, "screen_file_path", counting_screen)

    assert manifest.validate(app_root=src) == []
    assert screened.count(str(src)) == 1
    assert screened.count(str(src / "agents" / _AGENT_FILE)) == 1
