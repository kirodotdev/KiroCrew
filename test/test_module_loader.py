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
    is_app_module_loaded,
    load_app_module,
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

        monkeypatch.setattr(ml, "_third_party_apps_allowed", lambda: False)
        ml._warned_third_party_apps.discard("evil-app")
        app_dir = self._make_app(tmp_path)
        unique_name = ml._module_namespace("evil-app", "backend.routes")
        with pytest.raises(ImportError, match="apps_allow_third_party"):
            load_app_module("evil-app", app_dir, "backend.routes:register_routes")
        # The gate raised before spec_from_file_location/exec_module — no module
        # was ever registered in sys.modules (untrusted code never executed).
        assert unique_name not in sys.modules
        unload_app_modules("evil-app")

    def test_third_party_allowed_by_default(self, tmp_path: Path) -> None:
        import kiro_crew.apps.module_loader as ml

        ml._warned_third_party_apps.discard("evil-app")
        app_dir = self._make_app(tmp_path)
        # Default config leaves the gate open — the load succeeds.
        func = load_app_module("evil-app", app_dir, "backend.routes:register_routes")
        assert func(None) == "ok"
        unload_app_modules("evil-app")

    def test_builtin_load_not_blocked_by_gate(self, monkeypatch) -> None:
        import kiro_crew.apps.module_loader as ml

        # Gate closed, but builtins are trusted — they must still load.
        monkeypatch.setattr(ml, "_third_party_apps_allowed", lambda: False)
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

        count = unload_app_modules("test-app")
        assert count == 2
        assert not is_app_module_loaded("test-app")

        # Verify specific keys are gone
        assert "_kirocrew_app_test-app.mod_a" not in sys.modules
        assert "_kirocrew_app_test-app.sub.mod_b" not in sys.modules

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
