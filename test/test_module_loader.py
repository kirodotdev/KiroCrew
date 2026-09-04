"""Property tests for Module Loader isolation.

Feature: app-sdk-gateway-hooks
Properties 15, 16: Module isolation and unload.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kiro_crew.apps.module_loader import (
    _module_namespace,
    cache_shutdown_callable,
    clear_shutdown_callable,
    is_app_module_loaded,
    load_app_module,
    resolve_loaded_callable,
    unload_app_modules,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _create_app_module(app_dir: Path, module_path: str, content: str) -> None:
    """Create a Python module file in the app directory."""
    dotted, _ = module_path.rsplit(":", 1)
    rel_path = dotted.replace(".", "/") + ".py"
    file_path = app_dir / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


@pytest.fixture(autouse=True)
def _explicit_third_party_execution_admission(monkeypatch) -> None:
    """Most loader tests exercise isolation, so opt them in explicitly."""
    monkeypatch.setattr(
        "kiro_crew.apps.execution.third_party_execution_allowed", lambda: True
    )


# ---------------------------------------------------------------------------
# CSE SEC-012: third-party app code runs in-process — make the boundary loud
# ---------------------------------------------------------------------------


class TestThirdPartyTrustWarning:
    """A third-party (non-builtin) app load logs a one-time SECURITY warning;
    builtins do not."""

    def _make_app(self, tmp_path: Path) -> Path:
        app_dir = tmp_path / "evil-app"
        _create_app_module(app_dir, "backend.routes:register_routes", """
def register_routes(ctx):
    return "ok"
""")
        return app_dir

    def test_third_party_load_warns_once(self, tmp_path: Path, caplog) -> None:
        import logging

        import kiro_crew.apps.module_loader as ml

        ml._warned_third_party_apps.discard("evil-app")
        app_dir = self._make_app(tmp_path)
        with caplog.at_level(logging.WARNING, logger=ml.logger.name):
            load_app_module("evil-app", app_dir, "backend.routes:register_routes")
            load_app_module("evil-app", app_dir, "backend.routes:register_routes")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "third-party app" in warnings[0].getMessage()
        assert "NOT sandboxed" in warnings[0].getMessage()
        unload_app_modules("evil-app")

    def test_builtin_load_does_not_warn(self, caplog) -> None:
        import logging

        import kiro_crew.apps.module_loader as ml

        # The deploy_web builtin ships a backend module; loading it must not warn.
        builtins = ml._BUILTINS_DIR
        app_dir = builtins / "deploy_web"
        if not (app_dir / "handlers.py").is_file():
            pytest.skip("deploy_web builtin layout changed")
        ml._warned_third_party_apps.discard("deploy-web")
        with caplog.at_level(logging.WARNING, logger=ml.logger.name):
            try:
                load_app_module("deploy-web", app_dir, "handlers:register_routes")
            except ImportError:
                pass  # callable name may differ; we only assert on warnings
        assert not [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "third-party" in r.getMessage()
        ]
        unload_app_modules("deploy-web")


# ---------------------------------------------------------------------------
# CSE SEC-012: hard off switch — agent.apps_allow_third_party gate
# ---------------------------------------------------------------------------


class TestThirdPartyGate:
    """When agent.apps_allow_third_party is false, third-party (non-builtin) app
    modules are refused BEFORE exec_module runs; builtins are unaffected."""

    def _make_app(self, tmp_path: Path) -> Path:
        app_dir = tmp_path / "evil-app"
        _create_app_module(app_dir, "backend.routes:register_routes", """
def register_routes(ctx):
    return "ok"
""")
        return app_dir

    def test_third_party_denied_when_gate_off(self, tmp_path: Path, monkeypatch) -> None:
        import kiro_crew.apps.module_loader as ml

        monkeypatch.setattr(
            "kiro_crew.apps.execution.third_party_execution_allowed", lambda: False
        )
        ml._warned_third_party_apps.discard("evil-app")
        app_dir = self._make_app(tmp_path)
        unique_name = ml._module_namespace("evil-app", "backend.routes")
        with pytest.raises(ImportError, match="apps_allow_third_party"):
            load_app_module("evil-app", app_dir, "backend.routes:register_routes")
        # The gate raised before spec_from_file_location/exec_module — no module
        # was ever registered in sys.modules (untrusted code never executed).
        assert unique_name not in sys.modules
        unload_app_modules("evil-app")

    def test_third_party_allowed_with_explicit_admission(self, tmp_path: Path) -> None:
        import kiro_crew.apps.module_loader as ml

        ml._warned_third_party_apps.discard("evil-app")
        app_dir = self._make_app(tmp_path)
        # The autouse fixture explicitly admits execution — the load succeeds.
        func = load_app_module("evil-app", app_dir, "backend.routes:register_routes")
        assert func(None) == "ok"
        unload_app_modules("evil-app")

    def test_builtin_load_not_blocked_by_gate(self, monkeypatch) -> None:
        import kiro_crew.apps.module_loader as ml

        # Gate closed, but builtins are trusted — they must still load.
        monkeypatch.setattr(
            "kiro_crew.apps.execution.third_party_execution_allowed", lambda: False
        )
        app_dir = ml._BUILTINS_DIR / "deploy_web"
        if not (app_dir / "handlers.py").is_file():
            pytest.skip("deploy_web builtin layout changed")
        ml._warned_third_party_apps.discard("deploy-web")
        try:
            load_app_module("deploy-web", app_dir, "handlers:register_routes")
        except ImportError as exc:
            # A missing callable is fine; the gate's ImportError is NOT.
            assert "apps_allow_third_party" not in str(exc)
        unload_app_modules("deploy-web")


# ---------------------------------------------------------------------------
# Property 15: Module isolation prevents namespace collisions
# ---------------------------------------------------------------------------


class TestModuleIsolation:
    """Property 15: Module isolation prevents namespace collisions.

    **Validates: Requirements 1.4 (error resilience), Module Isolation design**
    """

    def test_two_apps_same_module_path_no_collision(self, tmp_path: Path) -> None:
        """Two apps with backend.routes:register_routes load independently."""
        app_a_dir = tmp_path / "app-a"
        app_b_dir = tmp_path / "app-b"

        _create_app_module(app_a_dir, "backend.routes:register_routes", """
def register_routes(ctx):
    return "routes_from_a"
""")
        _create_app_module(app_b_dir, "backend.routes:register_routes", """
def register_routes(ctx):
    return "routes_from_b"
""")

        func_a = load_app_module("app-a", app_a_dir, "backend.routes:register_routes")
        func_b = load_app_module("app-b", app_b_dir, "backend.routes:register_routes")

        assert func_a(None) == "routes_from_a"
        assert func_b(None) == "routes_from_b"

        # Verify they're in sys.modules under different keys
        assert "_kirocrew_app_app-a.backend.routes" in sys.modules
        assert "_kirocrew_app_app-b.backend.routes" in sys.modules

        # Cleanup
        unload_app_modules("app-a")
        unload_app_modules("app-b")

    def test_modules_registered_with_unique_names(self, tmp_path: Path) -> None:
        """Each loaded module gets a unique sys.modules key."""
        _create_app_module(tmp_path, "handlers:setup", """
def setup(ctx):
    return "ok"
""")

        load_app_module("my-app", tmp_path, "handlers:setup")
        key = _module_namespace("my-app", "handlers")
        assert key in sys.modules
        assert key == "_kirocrew_app_my-app.handlers"

        # Cleanup
        unload_app_modules("my-app")

    def test_path_containment_rejects_escape(self, tmp_path: Path) -> None:
        """Module paths that escape the app directory are rejected."""
        app_dir = tmp_path / "my-app"
        app_dir.mkdir(parents=True, exist_ok=True)

        # Create a file outside the app dir
        (tmp_path / "evil.py").write_text("x = 1")

        # The dotted path "..evil" resolves to ../evil.py which escapes app_dir
        # But our loader converts dots to / so "..evil" -> "../evil.py" which
        # won't exist as a file. Use a symlink attack instead.
        # Actually, the path containment check catches resolved symlinks.
        # Let's test with a direct path that resolves outside:
        evil_dir = app_dir / "sub"
        evil_dir.mkdir(exist_ok=True)
        # Create a symlink that points outside
        import os
        link_path = evil_dir / "escape.py"
        try:
            os.symlink(str(tmp_path / "evil.py"), str(link_path))
        except OSError:
            pytest.skip("Cannot create symlinks")

        with pytest.raises(ImportError, match="escapes app directory"):
            load_app_module("my-app", app_dir, "sub.escape:x")

    def test_missing_module_raises(self, tmp_path: Path) -> None:
        """Non-existent module file raises ImportError."""
        app_dir = tmp_path / "my-app"
        app_dir.mkdir()

        with pytest.raises(ImportError, match="not found"):
            load_app_module("my-app", app_dir, "nonexistent:func")

    def test_missing_callable_raises(self, tmp_path: Path) -> None:
        """Module without the specified callable raises ImportError."""
        _create_app_module(tmp_path, "mymod:missing_func", """
def other_func():
    pass
""")

        with pytest.raises(ImportError, match="no attribute"):
            load_app_module("test-app", tmp_path, "mymod:missing_func")

        # Cleanup
        unload_app_modules("test-app")

    def test_non_callable_raises(self, tmp_path: Path) -> None:
        """Non-callable attribute raises ImportError."""
        _create_app_module(tmp_path, "mymod:MY_CONST", """
MY_CONST = 42
""")

        with pytest.raises(ImportError, match="not callable"):
            load_app_module("test-app", tmp_path, "mymod:MY_CONST")

        unload_app_modules("test-app")

    def test_invalid_format_raises_valueerror(self) -> None:
        """Invalid hook path format raises ValueError."""
        with pytest.raises(ValueError, match="missing ':'"):
            load_app_module("app", Path("/tmp"), "no_colon_here")

        with pytest.raises(ValueError, match="Invalid hook path"):
            load_app_module("app", Path("/tmp"), ":just_callable")


# ---------------------------------------------------------------------------
# Property 16: Module unload cleans sys.modules
# ---------------------------------------------------------------------------


class TestModuleUnload:
    """Property 16: Module unload cleans sys.modules.

    **Validates: Requirements 1.3 (deregistration completeness)**
    """

    def test_unload_removes_all_app_modules(self, tmp_path: Path) -> None:
        """unload_app_modules removes all entries for the app."""
        _create_app_module(tmp_path, "mod_a:func_a", "def func_a(ctx): pass")
        _create_app_module(tmp_path, "sub.mod_b:func_b", "def func_b(ctx): pass")

        load_app_module("test-app", tmp_path, "mod_a:func_a")
        load_app_module("test-app", tmp_path, "sub.mod_b:func_b")

        assert is_app_module_loaded("test-app")

        # Two leaf modules plus the synthetic packages that carry their dotted
        # names: the app root, and "sub" for the nested module.
        count = unload_app_modules("test-app")
        assert count == 4
        assert not is_app_module_loaded("test-app")

        # Verify specific keys are gone
        assert "_kirocrew_app_test-app.mod_a" not in sys.modules
        assert "_kirocrew_app_test-app.sub.mod_b" not in sys.modules
        assert "_kirocrew_app_test-app.sub" not in sys.modules
        assert "_kirocrew_app_test-app" not in sys.modules

    def test_unload_does_not_affect_other_apps(self, tmp_path: Path) -> None:
        """Unloading app A does not remove app B's modules."""
        app_a = tmp_path / "a"
        app_b = tmp_path / "b"
        _create_app_module(app_a, "routes:reg", "def reg(ctx): pass")
        _create_app_module(app_b, "routes:reg", "def reg(ctx): pass")

        load_app_module("app-a", app_a, "routes:reg")
        load_app_module("app-b", app_b, "routes:reg")

        unload_app_modules("app-a")

        assert not is_app_module_loaded("app-a")
        assert is_app_module_loaded("app-b")

        # Cleanup
        unload_app_modules("app-b")

    def test_unload_empty_is_noop(self) -> None:
        """Unloading an app with no loaded modules returns 0."""
        count = unload_app_modules("never-loaded-app")
        assert count == 0

    def test_reload_after_unload_gets_fresh_code(self, tmp_path: Path) -> None:
        """After unload, re-loading gets fresh module code."""
        import importlib
        import uuid
        work_dir = tmp_path / uuid.uuid4().hex
        work_dir.mkdir()
        mod_path = work_dir / "mymod.py"
        mod_path.write_text("def func(ctx): return 'v1'")

        func_v1 = load_app_module("test-app-reload", work_dir, "mymod:func")
        assert func_v1(None) == "v1"

        unload_app_modules("test-app-reload")

        # Update the file — also invalidate any bytecode cache
        mod_path.write_text("def func(ctx): return 'v2'")
        # Remove __pycache__ if it exists
        pycache = work_dir / "__pycache__"
        if pycache.exists():
            import shutil
            shutil.rmtree(pycache)
        # Invalidate importlib caches
        importlib.invalidate_caches()

        func_v2 = load_app_module("test-app-reload", work_dir, "mymod:func")
        assert func_v2(None) == "v2"

        unload_app_modules("test-app-reload")


# ---------------------------------------------------------------------------
# Issue #6078: a multi-module app backend must be able to import its own siblings
# ---------------------------------------------------------------------------


class TestRelativeImports:
    """A hook entry file can reach its sibling modules with a relative import.

    The sys.modules key is deliberately dotted so two apps cannot collide, which
    makes the module's ``__package__`` name a synthetic parent. Unless every
    ancestor of that name is registered, CPython raises ``ModuleNotFoundError``
    for the top of the chain and the app silently gets no routes.
    """

    def test_nested_hook_module_imports_sibling(self, tmp_path: Path) -> None:
        """``from . import config`` works from a ``backend.routes`` entry file."""
        app_dir = tmp_path / "multi-file-app"
        _create_app_module(app_dir, "backend.config:_", "TIMEOUT = 30\n")
        _create_app_module(app_dir, "backend.routes:register", """
from . import config


def register(ctx):
    return config.TIMEOUT
""")

        func = load_app_module("multi-file-app", app_dir, "backend.routes:register")
        assert func(None) == 30

        unload_app_modules("multi-file-app")

    def test_root_level_hook_module_imports_sibling(self, tmp_path: Path) -> None:
        """The same holds when the hook module sits at the app root."""
        app_dir = tmp_path / "flat-app"
        _create_app_module(app_dir, "config:_", "TIMEOUT = 5\n")
        _create_app_module(app_dir, "routes:register", """
from . import config


def register(ctx):
    return config.TIMEOUT
""")

        func = load_app_module("flat-app", app_dir, "routes:register")
        assert func(None) == 5

        unload_app_modules("flat-app")

    def test_from_import_of_submodule_attribute(self, tmp_path: Path) -> None:
        """``from .render import to_html`` — the other spelling authors reach for."""
        app_dir = tmp_path / "render-app"
        _create_app_module(
            app_dir, "backend.render:_", "def to_html(v):\n    return f'<p>{v}</p>'\n"
        )
        _create_app_module(app_dir, "backend.routes:register", """
from .render import to_html


def register(ctx):
    return to_html("hi")
""")

        func = load_app_module("render-app", app_dir, "backend.routes:register")
        assert func(None) == "<p>hi</p>"

        unload_app_modules("render-app")

    def test_sibling_modules_stay_isolated_between_apps(self, tmp_path: Path) -> None:
        """Two apps shipping the same sibling filename see their own copy.

        This is the property the dotted namespace exists for, and the reason a
        bare ``import config`` is not an acceptable workaround.
        """
        for name, value in (("iso-a", "1"), ("iso-b", "2")):
            app_dir = tmp_path / name
            _create_app_module(app_dir, "backend.config:_", f"VALUE = {value}\n")
            _create_app_module(app_dir, "backend.routes:register", """
from . import config


def register(ctx):
    return config.VALUE
""")

        func_a = load_app_module("iso-a", tmp_path / "iso-a", "backend.routes:register")
        func_b = load_app_module("iso-b", tmp_path / "iso-b", "backend.routes:register")

        assert func_a(None) == 1
        assert func_b(None) == 2
        assert (
            sys.modules["_kirocrew_app_iso-a.backend.config"]
            is not sys.modules["_kirocrew_app_iso-b.backend.config"]
        )

        unload_app_modules("iso-a")
        unload_app_modules("iso-b")

    def test_relative_import_cannot_escape_the_app_root(self, tmp_path: Path) -> None:
        """A relative import that walks above the app root is refused.

        The synthetic root's ``__path__`` is the app directory, so there is no
        package above it to traverse into.
        """
        (tmp_path / "outside.py").write_text("SECRET = 'leaked'\n", encoding="utf-8")
        app_dir = tmp_path / "escape-app"
        _create_app_module(app_dir, "backend.routes:register", """
from ... import outside


def register(ctx):
    return outside.SECRET
""")

        with pytest.raises(ImportError):
            load_app_module("escape-app", app_dir, "backend.routes:register")

        assert not is_app_module_loaded("escape-app")

    def test_failed_load_leaves_no_synthetic_packages_behind(
        self, tmp_path: Path
    ) -> None:
        """A module whose body raises leaves sys.modules exactly as it was."""
        app_dir = tmp_path / "broken-app"
        _create_app_module(app_dir, "backend.routes:register", """
raise RuntimeError("boom")


def register(ctx):
    return None
""")

        with pytest.raises(ImportError):
            load_app_module("broken-app", app_dir, "backend.routes:register")

        assert "_kirocrew_app_broken-app" not in sys.modules
        assert "_kirocrew_app_broken-app.backend" not in sys.modules
        assert not is_app_module_loaded("broken-app")

    def test_failed_load_keeps_an_earlier_successful_load(
        self, tmp_path: Path
    ) -> None:
        """Rolling back a failed hook must not evict a hook that already loaded."""
        app_dir = tmp_path / "mixed-app"
        _create_app_module(app_dir, "backend.config:_", "VALUE = 11\n")
        _create_app_module(app_dir, "backend.routes:register", """
from . import config


def register(ctx):
    return config.VALUE
""")
        _create_app_module(app_dir, "backend.hooks:on_startup", """
raise RuntimeError("startup module is broken")


def on_startup(ctx):
    return None
""")

        func = load_app_module("mixed-app", app_dir, "backend.routes:register")
        assert func(None) == 11

        with pytest.raises(ImportError):
            load_app_module("mixed-app", app_dir, "backend.hooks:on_startup")

        # The working hook and the sibling it imported are still usable.
        assert "_kirocrew_app_mixed-app.backend.routes" in sys.modules
        assert "_kirocrew_app_mixed-app.backend.config" in sys.modules
        assert "_kirocrew_app_mixed-app.backend.hooks" not in sys.modules
        assert func(None) == 11

        unload_app_modules("mixed-app")

    def test_rollback_clears_the_parent_attribute_of_a_dropped_sibling(
        self, tmp_path: Path
    ) -> None:
        """A rolled-back sibling must not stay reachable as a parent attribute.

        CPython binds a submodule on its parent package, so popping only the
        ``sys.modules`` key leaves the module reachable as ``parent.sibling``.
        That is invisible to ``unload_app_modules`` / ``is_app_module_loaded``,
        and a later ``from . import sibling`` would find the attribute, skip the
        import, and silently reuse the stale module instead of the file on disk.

        The residue needs an earlier SUCCESSFUL load, so the parent package
        pre-exists and survives the rollback.
        """
        app_dir = tmp_path / "residue-app"
        _create_app_module(app_dir, "backend.config:_", "VALUE = 1\n")
        _create_app_module(app_dir, "backend.render:_", "MARK = 'first'\n")
        _create_app_module(app_dir, "backend.routes:register", """
from . import config


def register(ctx):
    return config.VALUE
""")
        _create_app_module(app_dir, "backend.hooks:on_startup", """
from . import render

raise RuntimeError("boom after importing render")


def on_startup(ctx):
    return None
""")

        load_app_module("residue-app", app_dir, "backend.routes:register")
        with pytest.raises(ImportError):
            load_app_module("residue-app", app_dir, "backend.hooks:on_startup")

        parent = sys.modules["_kirocrew_app_residue-app.backend"]
        assert "_kirocrew_app_residue-app.backend.render" not in sys.modules
        assert not hasattr(parent, "render"), (
            "the rolled-back sibling is still reachable as a parent attribute, so a "
            "later relative import would reuse it instead of re-reading the file"
        )
        # The earlier successful load and the sibling it imported are untouched.
        assert hasattr(parent, "routes")
        assert hasattr(parent, "config")

        unload_app_modules("residue-app")

    def test_failed_second_load_of_one_module_restores_the_first_object(
        self, tmp_path: Path
    ) -> None:
        """Two hooks may name the SAME module file, so a load can overwrite a key
        that is already present. If that second load fails, the entry must go back
        to the object the first hook's registered handlers came from.

        This is the documented ``on_startup`` / ``on_shutdown`` shape, not an
        exotic case: both name one ``backend.hooks`` module.
        """
        app_dir = tmp_path / "two-hooks-app"
        _create_app_module(app_dir, "backend.hooks:on_startup", """
def on_startup(ctx):
    return "started"
""")

        first = load_app_module("two-hooks-app", app_dir, "backend.hooks:on_startup")
        key = "_kirocrew_app_two-hooks-app.backend.hooks"
        first_module = sys.modules[key]
        parent = sys.modules["_kirocrew_app_two-hooks-app.backend"]

        # Same module, a callable it does not define: the load re-executes the
        # file (replacing the sys.modules entry) and then fails the attr check.
        with pytest.raises(ImportError):
            load_app_module("two-hooks-app", app_dir, "backend.hooks:on_shutdown")

        assert sys.modules[key] is first_module, (
            "the failed second load left its own module object registered, so a "
            "sibling import would see different state than the live handlers"
        )
        assert getattr(parent, "hooks") is first_module
        assert first(None) == "started"

        unload_app_modules("two-hooks-app")


def test_deploy_skill_install_copy_fallback(tmp_path, monkeypatch):
    """When symlinking fails (Windows/restricted FS), skills are copied — never skipped."""
    from pathlib import Path

    import kiro_crew.deploy as deploy_pkg

    monkeypatch.setattr(deploy_pkg, "config_dir", lambda: tmp_path)

    def _no_symlink(self, target, *a, **kw):
        raise OSError("symlink not permitted")
    monkeypatch.setattr(Path, "symlink_to", _no_symlink)

    deploy_pkg._register_core_skills()
    installed = tmp_path / "skills" / "artifact-deploy"
    assert installed.is_dir() and not installed.is_symlink()
    assert (installed / "SKILL.md").exists()


def test_deploy_skill_install_preserves_user_placed_dir(tmp_path, monkeypatch):
    """A user-placed directory without .kirocrew-managed marker is never removed."""
    from pathlib import Path

    import kiro_crew.deploy as deploy_pkg

    monkeypatch.setattr(deploy_pkg, "config_dir", lambda: tmp_path)

    # Pre-create a user-owned directory with the same name as a built-in skill
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    user_dir = skills_dir / "artifact-deploy"
    user_dir.mkdir()
    (user_dir / "my-custom-file.txt").write_text("user content")

    # No .kirocrew-managed marker — should survive
    def _no_symlink(self, target, *a, **kw):
        raise OSError("symlink not permitted")
    monkeypatch.setattr(Path, "symlink_to", _no_symlink)

    deploy_pkg._register_core_skills()

    # User directory must be untouched
    assert user_dir.is_dir()
    assert (user_dir / "my-custom-file.txt").read_text(encoding="utf-8") == "user content"
    # Our SKILL.md was NOT installed (user dir blocked it)
    assert not (user_dir / "SKILL.md").exists()


def test_deploy_skill_install_replaces_managed_dir(tmp_path, monkeypatch):
    """A directory WITH .kirocrew-managed marker is replaced on refresh."""
    from pathlib import Path

    import kiro_crew.deploy as deploy_pkg

    monkeypatch.setattr(deploy_pkg, "config_dir", lambda: tmp_path)

    # Pre-create a managed directory (stale copy from a prior version)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    managed_dir = skills_dir / "artifact-deploy"
    managed_dir.mkdir()
    (managed_dir / ".kirocrew-managed").write_text("")
    (managed_dir / "stale-file.txt").write_text("old")

    def _no_symlink(self, target, *a, **kw):
        raise OSError("symlink not permitted")
    monkeypatch.setattr(Path, "symlink_to", _no_symlink)

    deploy_pkg._register_core_skills()

    # Managed directory was replaced — stale file gone, new content present
    assert managed_dir.is_dir()
    assert not (managed_dir / "stale-file.txt").exists()
    assert (managed_dir / "SKILL.md").exists()
    # Marker was re-written by the copy fallback
    assert (managed_dir / ".kirocrew-managed").exists()


# ---------------------------------------------------------------------------
# resolve_loaded_callable — gone-app shutdown (issue #7880 reconciler teardown)
# ---------------------------------------------------------------------------


def test_resolve_loaded_callable_survives_deleted_files(tmp_path, monkeypatch):
    """GPT [BLOCKING]: CLI uninstall deletes an app's files, so the disk loader
    can no longer resolve its on_shutdown -- yet the module the gateway imported
    is still resident in sys.modules and a task its on_startup spawned is still
    live. resolve_loaded_callable resolves the callable from that cached module
    so trust revocation can still run on_shutdown, where load_app_module (disk)
    now raises."""
    app_dir = tmp_path / "gone-app"
    _create_app_module(
        app_dir, "backend.hooks:on_shutdown", "def on_shutdown(ctx):\n    return 'bye'\n"
    )
    # Load it as the gateway would, then delete the files (simulate uninstall).
    func = load_app_module("gone-app", app_dir, "backend.hooks:on_shutdown")
    assert func(None) == "bye"
    import shutil

    shutil.rmtree(app_dir)

    # Disk loader now fails (files gone)...
    with pytest.raises(ImportError):
        load_app_module("gone-app", app_dir, "backend.hooks:on_shutdown")
    # ...but the cached module still resolves the callable.
    cached = resolve_loaded_callable("gone-app", "backend.hooks:on_shutdown")
    assert cached is not None and cached(None) == "bye"

    unload_app_modules("gone-app")
    # Once unloaded, there is nothing cached to resolve.
    assert resolve_loaded_callable("gone-app", "backend.hooks:on_shutdown") is None


def test_resolve_loaded_callable_none_when_never_loaded():
    """An app the gateway never loaded has no cached module -> None (the caller
    then falls back to the disk loader / hooks-skipped teardown)."""
    assert resolve_loaded_callable("never-loaded-app", "backend.hooks:on_shutdown") is None
    # Malformed paths resolve to None rather than raising.
    assert resolve_loaded_callable("x", "no-colon") is None
    assert resolve_loaded_callable("x", ":only_callable") is None


def test_cached_shutdown_callable_survives_uninstall_of_uncached_module():
    """GPT [BLOCKING]: when on_startup and on_shutdown live in SEPARATE modules,
    only the startup module is in sys.modules, so resolve_loaded_callable would
    miss the shutdown module and the disk fallback would raise on the deleted
    files -- orphaning the app's background task after trust removal. The
    enable-time cache_shutdown_callable captures the bound on_shutdown callable
    while files exist, so resolve_loaded_callable returns it FIRST, without disk."""
    ran = {"v": False}

    def on_shutdown(ctx):
        ran["v"] = True
        return "stopped"

    # No module for this app is in sys.modules (startup never imported the
    # shutdown module) -> without the cache, resolution would be None.
    assert resolve_loaded_callable("sep-app", "backend.shutdown:on_shutdown") is None

    # Enable-time cache (populated while files existed) now covers it.
    cache_shutdown_callable("sep-app", on_shutdown)
    func = resolve_loaded_callable("sep-app", "backend.shutdown:on_shutdown")
    assert func is not None
    assert func(None) == "stopped" and ran["v"] is True

    # Cleared on unload -> re-enable re-caches fresh code.
    clear_shutdown_callable("sep-app")
    assert resolve_loaded_callable("sep-app", "backend.shutdown:on_shutdown") is None


def test_clear_all_shutdown_callables_drops_the_whole_cache():
    """GPT round-9: the gateway teardown sweep does not go through per-app
    unload_app_modules, so it must drop the whole shutdown cache -- otherwise a
    callable captured this generation survives into an in-process restart and is
    used to stop a NEWLY loaded worker."""
    import kiro_crew.apps.module_loader as ml

    ml._shutdown_callables.clear()
    ml.cache_shutdown_callable("app-a", lambda ctx: None)
    ml.cache_shutdown_callable("app-b", lambda ctx: None)
    assert ml._shutdown_callables  # populated

    ml.clear_all_shutdown_callables()
    assert ml._shutdown_callables == {}, "teardown sweep must drop every cached callable"
