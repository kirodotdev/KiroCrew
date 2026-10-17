"""Unit coverage for :mod:`kiro_crew.apps.backend`: what the spawn body launches.

Node / npm binary resolution and the entry-point heuristics, the shared interpreter
resolution in ``apps/interpreter.py`` (which venv or deps-dir interpreter and console
script a backend runs under, and the probe that judges it usable), the Node and ASGI
dispatch branches, and the environment and ``Popen`` tail of a spawn.

One topic of the ``test_apps_backend_coverage*.py`` family; the doubles and fixtures
the family shares come from ``apps_backend_coverage_helpers.py``.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from apps_backend_coverage_helpers import _isolated_module_state  # noqa: F401
from apps_backend_coverage_helpers import spawn_root_fixture  # noqa: F401
from apps_backend_coverage_helpers import (
    _capture_popen,
    _FakeProc,
    _manifest,
    _record_runs,
    _StopSpawn,
)

import kiro_crew.apps.backend as bmod

# ---------------------------------------------------------------------------
# Node / npm binary resolution
# ---------------------------------------------------------------------------


class TestNvmResolution:
    def test_no_nvm_script_returns_none(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NVM_DIR", str(tmp_path / "absent"))
        assert bmod._resolve_nvm_path("node") is None

    def _nvm_dir(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        """An ``NVM_DIR`` with ``nvm.sh`` present, on the POSIX branch.

        The resolver is POSIX-only: on Windows it returns None before the
        ``nvm.sh`` probe (``test_apps_backend_nvm_windows.py`` covers that
        arm). These tests exercise the shell lookup, so they pin the platform
        flag rather than letting the Windows runner skip the code under test.
        """
        monkeypatch.setattr(bmod.platform_compat, "IS_WINDOWS", False)
        nvm = tmp_path / "nvm"
        nvm.mkdir()
        (nvm / "nvm.sh").write_text("# nvm\n")
        monkeypatch.setenv("NVM_DIR", str(nvm))
        return nvm

    def test_resolves_sibling_binary_of_the_nvm_node(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._nvm_dir(tmp_path, monkeypatch)
        bin_dir = tmp_path / "versions" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "node").write_text("")
        (bin_dir / "npm").write_text("")
        _record_runs(
            monkeypatch,
            result=SimpleNamespace(returncode=0, stdout=f"{bin_dir / 'node'}\n"),
        )
        assert bmod._resolve_nvm_path("npm") == str(bin_dir / "npm")

    def test_missing_sibling_binary_returns_none(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._nvm_dir(tmp_path, monkeypatch)
        bin_dir = tmp_path / "versions" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "node").write_text("")
        _record_runs(
            monkeypatch,
            result=SimpleNamespace(returncode=0, stdout=f"{bin_dir / 'node'}\n"),
        )
        assert bmod._resolve_nvm_path("npm") is None

    def test_nonzero_nvm_exit_returns_none(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._nvm_dir(tmp_path, monkeypatch)
        _record_runs(monkeypatch, result=SimpleNamespace(returncode=1, stdout=""))
        assert bmod._resolve_nvm_path("node") is None

    def test_nvm_probe_failure_returns_none(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._nvm_dir(tmp_path, monkeypatch)
        _record_runs(monkeypatch, exc=OSError("no bash"))
        assert bmod._resolve_nvm_path("node") is None

    # Branch-outcome logging: each exit of the POSIX/nvm branch
    # names itself, so a Windows log shows whether the branch ran or was skipped.
    def test_skipped_branch_is_logged(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Pin the platform flag off: the resolver is POSIX-only and returns
        # before this probe on Windows, so the branch under test runs on the
        # Windows runner only once the guard is neutralised (sibling tests get
        # this from ``_nvm_dir``; this one points NVM_DIR at an absent dir).
        monkeypatch.setattr(bmod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setenv("NVM_DIR", str(tmp_path / "absent"))
        with caplog.at_level("DEBUG", logger=bmod.logger.name):
            assert bmod._resolve_nvm_path("node") is None
        assert any("skipping nvm branch" in r.getMessage() for r in caplog.records)

    def test_resolved_branch_is_logged(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._nvm_dir(tmp_path, monkeypatch)
        bin_dir = tmp_path / "versions" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "node").write_text("")
        (bin_dir / "npm").write_text("")
        _record_runs(
            monkeypatch,
            result=SimpleNamespace(returncode=0, stdout=f"{bin_dir / 'node'}\n"),
        )
        with caplog.at_level("DEBUG", logger=bmod.logger.name):
            assert bmod._resolve_nvm_path("npm") == str(bin_dir / "npm")
        assert any("resolved 'npm'" in r.getMessage() for r in caplog.records)

    def test_probe_failure_branch_is_logged(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._nvm_dir(tmp_path, monkeypatch)
        _record_runs(monkeypatch, exc=OSError("no bash"))
        with caplog.at_level("DEBUG", logger=bmod.logger.name):
            assert bmod._resolve_nvm_path("node") is None
        assert any("shell invocation failed" in r.getMessage() for r in caplog.records)

    def test_node_and_npm_prefer_nvm_over_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod, "_resolve_nvm_path", lambda name: f"/nvm/{name}")
        monkeypatch.setattr(
            bmod.shutil, "which", lambda _n: pytest.fail("PATH consulted despite nvm hit")
        )
        assert bmod._find_node_binary() == "/nvm/node"
        assert bmod._find_npm_binary() == "/nvm/npm"

    def test_node_and_npm_fall_back_to_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod, "_resolve_nvm_path", lambda _name: None)
        monkeypatch.setattr(bmod.shutil, "which", lambda name: f"/usr/bin/{name}")
        assert bmod._find_node_binary() == "/usr/bin/node"
        assert bmod._find_npm_binary() == "/usr/bin/npm"


# ---------------------------------------------------------------------------
# Entry-point heuristics
# ---------------------------------------------------------------------------


class TestEntryHeuristics:
    def test_asgi_entry_needs_both_markers(self, tmp_path: Any) -> None:
        both = tmp_path / "asgi.py"
        both.write_text("app = FastAPI()\nimport uvicorn\n")
        assert bmod._is_asgi_entry(both) is True

        only_one = tmp_path / "plain.py"
        only_one.write_text("app = FastAPI()\n")
        assert bmod._is_asgi_entry(only_one) is False

    def test_asgi_entry_unreadable_is_not_asgi(self, tmp_path: Any) -> None:
        assert bmod._is_asgi_entry(tmp_path) is False  # a directory: OSError

    def test_shebang_argv_unreadable_falls_back_to_bin_sh(self, tmp_path: Any) -> None:
        assert bmod._shebang_argv(tmp_path) == ["/bin/sh"]

    def test_shebang_argv_non_utf8_falls_back_to_bin_sh(self, tmp_path: Any) -> None:
        entry = tmp_path / "weird"
        entry.write_bytes(b"#!\xff\xfe\n")
        assert bmod._shebang_argv(entry) == ["/bin/sh"]

    def test_shebang_argv_empty_interpreter_falls_back_to_bin_sh(self, tmp_path: Any) -> None:
        entry = tmp_path / "bare"
        entry.write_bytes(b"#!\n")
        assert bmod._shebang_argv(entry) == ["/bin/sh"]

    def test_shebang_argv_keeps_the_single_kernel_argument(self, tmp_path: Any) -> None:
        entry = tmp_path / "run"
        entry.write_text("#!/usr/bin/env bash\n")
        assert bmod._shebang_argv(entry) == ["/usr/bin/env", "bash"]


# ---------------------------------------------------------------------------
# Shared interpreter resolution (apps/interpreter.py)
# ---------------------------------------------------------------------------


def _write_runnable(path: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)


def _create_real_venv(root: Any) -> None:
    """Build a REAL ``.venv`` under ``root`` the way ``python -m venv`` does.

    The CLI symlinks the interpreter on POSIX and copies it only on Windows;
    the ``venv.create`` API defaults to copying everywhere. A copied binary
    breaks on a relocatable base interpreter (python-build-standalone, which
    ``uv`` installs): its ``@rpath/libpython`` is resolved relative to the
    binary's own location, so the copy aborts at load and
    ``_venv_is_usable``'s probe reads it as "not a venv". Mirror the CLI so
    the fixture is the venv production expects on every host.
    """
    import venv as _venv

    _venv.create(root / ".venv", with_pip=False, symlinks=not bmod.platform_compat.IS_WINDOWS)


@pytest.fixture()
def probe_sandbox_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the interpreter usability probe without OS confinement.

    The probe's sandbox wrapping is a host capability (absent on Windows CI
    and fail-closed there by design); these tests pin the probe's LOGIC -
    sys.prefix ownership and ABI match against a real venv interpreter - so
    the seams are passed through and the child runs directly.
    """
    from kiro_crew.apps import interpreter as imod

    monkeypatch.setattr(imod.sandbox, "wrap_argv", lambda argv, **_k: (list(argv), None))
    monkeypatch.setattr(imod.sandbox, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(imod.sandbox, "run_limited", lambda argv, **kw: subprocess.run(argv, **kw))


def _plant_stamp(app_root: Any) -> None:
    """Command selection requires a CURRENT stamp (stale trees crash at
    import); fixtures that expect deps-dir scripts to resolve plant one."""
    from kiro_crew.apps.interpreter import app_deps_dir as _add

    d = _add(app_root)
    d.mkdir(parents=True, exist_ok=True)
    (d / bmod._DEPS_STAMP_NAME).write_text(bmod._deps_digest(b"requests\n"))


class TestInterpreterResolution:
    def _venv_python(self, root: Any) -> Any:
        from kiro_crew.apps.interpreter import venv_python_path

        return venv_python_path(root)

    def _real_venv(self, root: Any) -> Any:
        """Build a REAL .venv under root (the resolver now probes the
        interpreter, so a stub file fails _venv_is_usable)."""
        _create_real_venv(root)
        return self._venv_python(root)

    def test_a_real_venv_is_preferred(self, tmp_path: Any, probe_sandbox_passthrough: Any) -> None:
        from kiro_crew.apps.interpreter import resolve_app_python

        py = self._real_venv(tmp_path)
        assert resolve_app_python(tmp_path) == str(py)

    def test_a_bootstrap_skeleton_is_not_preferred(
        self, tmp_path: Any, probe_sandbox_passthrough: Any
    ) -> None:
        """The defect the probe fixes: a failed `python -m venv` leaves a
        skeleton whose bin/python3 is the fully-runnable system interpreter
        and whose pyvenv.cfg names the current minor version - a version
        check ACCEPTS it, but its sys.prefix is not this venv (no working
        environment), so the probe rejects it."""
        from kiro_crew.apps.interpreter import resolve_app_python

        # A stub interpreter that starts but is NOT the venv's own python
        # (its sys.prefix will not point inside <root>/.venv).
        py = self._venv_python(tmp_path)
        py.parent.mkdir(parents=True)
        py.write_text("#!/bin/sh\nexit 0\n")
        py.chmod(0o755)
        (tmp_path / ".venv" / "pyvenv.cfg").write_text(
            f"version = {sys.version_info[0]}.{sys.version_info[1]}.0\n"
        )
        assert resolve_app_python(tmp_path) == sys.executable

    def test_a_venv_without_an_interpreter_is_not_preferred(self, tmp_path: Any) -> None:
        from kiro_crew.apps.interpreter import resolve_app_python

        (tmp_path / ".venv").mkdir()
        assert resolve_app_python(tmp_path) == sys.executable

    def test_the_probe_runs_with_the_sanitized_app_env(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, probe_sandbox_passthrough: Any
    ) -> None:
        """The probe executable is app-controlled, so it must receive the
        same sanitized environment every app subprocess gets - never the
        gateway's own, which can carry credentials."""
        from kiro_crew.apps import interpreter as imod

        _create_real_venv(tmp_path)
        monkeypatch.setenv("GATEWAY_SECRET_CANARY", "leak-me")
        seen: dict[str, Any] = {}
        real_run = imod.sandbox.run_limited

        def _spy(argv: Any, **kwargs: Any) -> Any:
            seen["env"] = kwargs.get("env")
            return real_run(argv, **kwargs)

        monkeypatch.setattr(imod.sandbox, "run_limited", _spy)
        imod._venv_is_usable(tmp_path)
        assert seen["env"] is not None, "probe ran with the inherited gateway env"
        assert "GATEWAY_SECRET_CANARY" not in seen["env"]

    def test_the_probe_fails_closed_when_no_sandbox_is_available(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe executes app-controlled code, so it runs ONLY under the
        OS sandbox; a host where wrap_argv fail-closes (no backend) gets no
        positive evidence and falls back to sys.executable - the exception
        must never propagate into spawn or registration."""
        from kiro_crew.apps import interpreter as imod

        _create_real_venv(tmp_path)

        def _raise(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("no sandbox backend")

        monkeypatch.setattr(imod.sandbox, "wrap_argv", _raise)
        assert imod.resolve_app_python(tmp_path) == sys.executable

    def test_hostile_probe_output_reads_as_not_usable(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, probe_sandbox_passthrough: Any
    ) -> None:
        """The probe output is app-controlled: a venv whose interpreter prints
        a null prefix (valid JSON, wrong shape) must read as "not usable" -
        Path(None) raising TypeError out of the probe would break spawn and
        registration."""
        py = self._venv_python(tmp_path)
        py.parent.mkdir(parents=True)
        py.write_text("#!/bin/sh\necho '[null, %d, %d]'\n" % sys.version_info[:2])
        py.chmod(0o755)
        from kiro_crew.apps import interpreter as imod

        assert imod._venv_is_usable(tmp_path) is False
        assert imod.resolve_app_python(tmp_path) == sys.executable

    def test_interpreter_choice_follows_the_active_tree(self, tmp_path: Any) -> None:
        """With an ACTIVE provisioned tree the gateway interpreter owns the
        launch (the wheels are ABI-bound to it). WITHOUT one - never
        provisioned or provisioning failed - a POSITIVELY usable venv is
        preferred even when requirements are declared: the bare gateway
        interpreter holds none of the declared deps either, and a failed
        provisioning must not also take a working legacy venv away."""
        import sys as _sys
        from unittest import mock

        from kiro_crew.apps import interpreter as imod

        (tmp_path / "requirements.txt").write_bytes(b"uvicorn\n")
        # No tree + usable venv: the venv wins (the F2 fallback).
        with mock.patch.object(imod, "_venv_is_usable", return_value=True):
            assert imod.resolve_app_python(tmp_path) != _sys.executable
        # No tree + NO usable venv: the gateway interpreter.
        with mock.patch.object(imod, "_venv_is_usable", return_value=False):
            assert imod.resolve_app_python(tmp_path) == _sys.executable
        # ACTIVE tree (current-interpreter stamp planted): the gateway
        # interpreter reclaims the launch, even with a usable venv present.
        deps = imod.app_deps_dir(tmp_path)
        deps.mkdir(parents=True)
        (deps / bmod._DEPS_STAMP_NAME).write_text(bmod._deps_digest(b"uvicorn\n"))
        with mock.patch.object(imod, "_venv_is_usable", return_value=True):
            assert imod.resolve_app_python(tmp_path) == _sys.executable
        # STALE tree (no stamp, foreign/absent ABI tag): the spawn would
        # refuse to inject it, so it must not suppress the venv fallback
        # either - a dead tree that delivers nothing cannot also take the
        # working environment away.
        (deps / bmod._DEPS_STAMP_NAME).unlink()
        with mock.patch.object(imod, "_venv_is_usable", return_value=True):
            assert imod.resolve_app_python(tmp_path) != _sys.executable

    @pytest.mark.skipif(sys.platform == "win32", reason="dangling-symlink semantics are POSIX-only")
    def test_a_dangling_requirements_symlink_surfaces_as_a_failure(self, spawn_root: Any) -> None:
        """is_file() answers False for a DANGLING requirements.txt link the
        same as for absence - but a present-yet-unreadable entry must NOT be
        silently skipped, or the backend spawns importing deps that were
        never installed. Absence returns "" (nothing to do); a broken link
        returns a provisioning-failure string."""
        import os as _os

        # Genuine absence: no error.
        assert bmod.provision_app_deps("noreq-app", spawn_root) == ""
        # Present but dangling: a failure string routed through the common
        # failure epilogue (SEL audit + ERROR log), not a silent skip.
        _os.symlink(str(spawn_root / "does-not-exist.txt"), str(spawn_root / "requirements.txt"))
        events: list = []

        class _Sel:
            def log_api_access(self, **kw: Any) -> None:
                events.append(kw)

        from unittest.mock import patch

        with patch.object(bmod, "sel", lambda: _Sel()):
            err = bmod.provision_app_deps("dangling-app", spawn_root)
        assert err and "requirements.txt" in err, err
        assert events and events[0]["outcome"] == "deps_provision_failed", events

    def test_the_spill_cap_truncates_a_flooding_child(self, tmp_path: Any) -> None:
        """The output spill is bounded on disk: when it crosses the cap the
        watchdog truncates it back to empty, so a flooding child cannot fill
        the host. Drive the watchdog directly against a real temp file."""
        import tempfile as _tf
        import time as _time

        with _tf.TemporaryFile() as f:
            with bmod._capped_spill(f, hard_cap_bytes=4096, poll_secs=0.05) as tripped:
                f.write(b"x" * (4096 * 4))
                f.flush()
                # flake-ok: a bounded 3 s poll for the spill watchdog's trip event, moved verbatim; tracked for a de-flake pass
                deadline = _time.monotonic() + 3
                while _time.monotonic() < deadline and not tripped.is_set():
                    _time.sleep(0.05)
            assert tripped.is_set(), "watchdog must trip on a spill over the cap"
            import os as _os

            assert _os.fstat(f.fileno()).st_size <= 4096, "spill must be truncated"

    def test_a_deps_dir_console_script_is_resolvable(self, tmp_path: Any) -> None:
        """pip --target puts console scripts in <target>/bin (Scripts on
        Windows); the resolver must find them there now that the gateway never
        creates the venv layout that carries them on real installs."""
        from kiro_crew import platform_compat as _pc
        from kiro_crew.apps.interpreter import app_deps_dir, venv_provided_command

        scripts = "Scripts" if _pc.IS_WINDOWS else "bin"
        name = "my-tool.exe" if _pc.IS_WINDOWS else "my-tool"
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_stamp(tmp_path)
        script = app_deps_dir(tmp_path) / scripts / name
        _write_runnable(script)
        assert venv_provided_command(tmp_path, "my-tool") == str(script)

    def test_provisioned_deps_pin_the_gateway_interpreter(self, tmp_path: Any) -> None:
        """Once the gateway provisioned .kirocrew-deps, those wheels were
        built by sys.executable, so the venv is not consulted at all."""
        from kiro_crew.apps.interpreter import app_deps_dir, resolve_app_python

        self._real_venv(tmp_path)
        app_deps_dir(tmp_path).mkdir(parents=True)
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_stamp(tmp_path)
        assert resolve_app_python(tmp_path) == sys.executable

    def test_a_venv_console_script_wins_only_without_provisioned_deps(
        self, tmp_path: Any, probe_sandbox_passthrough: Any
    ) -> None:
        from kiro_crew import platform_compat as _pc
        from kiro_crew.apps.interpreter import app_deps_dir, venv_provided_command

        scripts = "Scripts" if _pc.IS_WINDOWS else "bin"
        name = "my-tool.exe" if _pc.IS_WINDOWS else "my-tool"
        self._real_venv(tmp_path)
        venv_script = tmp_path / ".venv" / scripts / name
        _write_runnable(venv_script)
        # No deps dir: the usable venv's script is the answer.
        assert venv_provided_command(tmp_path, "my-tool") == str(venv_script)
        # Provisioned deps present: the deps-dir script (shebang:
        # sys.executable, ABI-consistent with the provisioned wheels) wins.
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_stamp(tmp_path)
        deps_script = app_deps_dir(tmp_path) / scripts / name
        _write_runnable(deps_script)
        assert venv_provided_command(tmp_path, "my-tool") == str(deps_script)

    def test_a_skeleton_venv_console_script_is_bypassed(self, tmp_path: Any) -> None:
        """A console script under a NON-usable venv (skeleton / no working
        interpreter) is skipped: its shebang interpreter would not carry the
        app's environment. The deps-dir script is used instead."""
        from kiro_crew import platform_compat as _pc
        from kiro_crew.apps.interpreter import app_deps_dir, venv_provided_command

        scripts = "Scripts" if _pc.IS_WINDOWS else "bin"
        name = "my-tool.exe" if _pc.IS_WINDOWS else "my-tool"
        # venv dir exists but has no runnable interpreter -> not usable.
        _write_runnable(tmp_path / ".venv" / scripts / name)
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_stamp(tmp_path)
        deps_script = app_deps_dir(tmp_path) / scripts / name
        _write_runnable(deps_script)
        assert venv_provided_command(tmp_path, "my-tool") == str(deps_script)


# ---------------------------------------------------------------------------
# Spawn body: dispatch branches
# ---------------------------------------------------------------------------


class TestNodeDispatch:
    def test_a_node_backend_without_a_node_binary_is_refused(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        (spawn_root / "server.js").write_text("// noop\n")
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: None)
        monkeypatch.setattr(
            bmod, "popen_limited", lambda *_a, **_k: pytest.fail("spawned without node")
        )
        with caplog.at_level(logging.ERROR):
            assert bmod._start_app_backend_body("nodeless", _manifest("server.js")) is None
        assert any("no node binary found" in r.message for r in caplog.records)

    def test_a_js_entry_runs_under_node_and_installs_npm_deps(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.js").write_text("// noop\n")
        (spawn_root / "package.json").write_text(json.dumps({"name": "x"}))
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: "/usr/bin/node")
        monkeypatch.setattr(bmod, "_find_npm_binary", lambda: "/usr/bin/npm")
        runs = _record_runs(monkeypatch)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("nodeapp", _manifest("server.js"))
        assert seen["argv"][0] == "/usr/bin/node"
        assert seen["kwargs"]["env"]["NODE_ENV"] == "production"
        assert runs == [["/usr/bin/npm", "install", "--production", "--no-audit", "--no-fund"]]

    def test_npm_install_is_skipped_when_node_modules_is_present(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.js").write_text("// noop\n")
        (spawn_root / "package.json").write_text(json.dumps({"name": "x"}))
        (spawn_root / "node_modules").mkdir()
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: "/usr/bin/node")
        monkeypatch.setattr(
            bmod, "_find_npm_binary", lambda: pytest.fail("npm resolved despite node_modules")
        )
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("nodeapp2", _manifest("server.js"))
        assert runs == []

    def test_a_failed_npm_install_does_not_block_the_spawn(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        (spawn_root / "server.js").write_text("// noop\n")
        (spawn_root / "package.json").write_text(json.dumps({"name": "x"}))
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: "/usr/bin/node")
        monkeypatch.setattr(bmod, "_find_npm_binary", lambda: "/usr/bin/npm")
        _record_runs(monkeypatch, exc=RuntimeError("registry down"))
        _capture_popen(monkeypatch)
        with caplog.at_level(logging.WARNING):
            with pytest.raises(_StopSpawn):
                bmod._start_app_backend_body("nodeapp3", _manifest("server.js"))
        assert any("Failed to install npm deps" in r.message for r in caplog.records)

    def test_an_explicit_node_type_wins_over_the_filename_heuristic(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``type: node`` must route a non-.js entry to node, not the Python branch."""

        (spawn_root / "launch.bundle").write_text("// noop\n")
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: "/usr/bin/node")
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body(
                "nodeapp4", _manifest("launch.bundle", backend_type="node")
            )
        assert seen["argv"][0] == "/usr/bin/node"


class TestAsgiDispatch:
    _ASGI_SRC = "from fastapi import FastAPI\napp = FastAPI()\nimport uvicorn\n"

    def test_ambient_pythonpath_keeps_user_site_for_uvicorn(
        self,
        spawn_root: Any,
        monkeypatch: pytest.MonkeyPatch,
        nonbundled_python_with_user_site,
    ) -> None:
        """An inherited PYTHONPATH is not proof that this launcher supplied
        uvicorn's import path. A user-site-only uvicorn must remain reachable."""
        (spawn_root / "app.py").write_text(self._ASGI_SRC)
        monkeypatch.setenv("PYTHONPATH", "/operator/own")
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body(
                "asgi-ambient-path", _manifest("app.py", backend_type="asgi")
            )
        assert seen["argv"][:3] == [sys.executable, "-m", "uvicorn"]
        assert seen["kwargs"]["env"]["PYTHONPATH"] == "/operator/own"

    @pytest.mark.parametrize(
        ("entry_name", "backend_type", "contents"),
        [
            pytest.param("app.py", "asgi", _ASGI_SRC, id="asgi"),
            pytest.param("server.py", "", "x = 1\n", id="plain"),
        ],
    )
    def test_bundled_interpreter_disables_user_site_for_python_backends(
        self,
        spawn_root: Any,
        monkeypatch: pytest.MonkeyPatch,
        bundled_python_with_user_site,
        entry_name: str,
        backend_type: str,
        contents: str,
    ) -> None:
        (spawn_root / entry_name).write_text(contents)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body(
                f"bundled-{entry_name}",
                _manifest(entry_name, backend_type=backend_type),
            )
        assert seen["argv"][:2] == [sys.executable, "-s"]

    def test_a_sniffed_asgi_entry_is_served_by_uvicorn(
        self,
        spawn_root: Any,
        monkeypatch: pytest.MonkeyPatch,
        nonbundled_python_without_user_site,
    ) -> None:
        (spawn_root / "backend").mkdir()
        (spawn_root / "backend" / "app.py").write_text(self._ASGI_SRC)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("asgi", _manifest("backend/app.py"))
        assert seen["argv"][1:4] == ["-s", "-m", "uvicorn"]
        assert seen["argv"][4] == "backend.app:app"
        assert seen["kwargs"]["cwd"] == str(spawn_root)

    def test_a_src_layout_asgi_entry_runs_from_the_src_root(
        self,
        spawn_root: Any,
        monkeypatch: pytest.MonkeyPatch,
        nonbundled_python_without_user_site,
    ) -> None:
        """Without the src/ rewrite uvicorn cannot import the declared module."""

        pkg = spawn_root / "src" / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "app.py").write_text(self._ASGI_SRC)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body(
                "asgi-src", _manifest("src/pkg/app.py", backend_type="asgi")
            )
        assert seen["argv"][4] == "pkg.app:app"
        assert seen["kwargs"]["cwd"] == str(spawn_root / "src")

    def test_the_app_venv_interpreter_is_preferred_when_present(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, probe_sandbox_passthrough: Any
    ) -> None:
        # A REAL venv: resolve_app_python now probes the interpreter (runs it
        # and checks sys.prefix + ABI), so a stub file fails the probe. A
        # bootstrap skeleton or wrong-ABI copy is rejected by that probe;
        # provisioned deps (absent here) would pin sys.executable instead.
        from kiro_crew.apps.interpreter import venv_python_path

        _create_real_venv(spawn_root)
        venv_py = venv_python_path(spawn_root)
        (spawn_root / "app.py").write_text(self._ASGI_SRC)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("asgi-venv", _manifest("app.py"))
        assert seen["argv"][0] == str(venv_py)

    def test_nonbundled_module_builtin_keeps_user_site_and_rejects_app_deps(
        self,
        spawn_root: Any,
        monkeypatch: pytest.MonkeyPatch,
        nonbundled_python_with_user_site,
    ) -> None:
        """A module-style builtin (entry is None) runs TRUSTED package code.
        A requirements.txt or .kirocrew-deps sitting in its writable app dir
        must NOT be provisioned or prepended to its PYTHONPATH - that would
        let agent-authored wheels load ahead of the trusted module."""
        from kiro_crew.apps.interpreter import app_deps_dir

        # Both attack surfaces present in the writable app dir:
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        app_deps_dir(spawn_root).mkdir(parents=True)
        (app_deps_dir(spawn_root) / "evil.py").write_text("x = 1\n")
        runs = _record_runs(monkeypatch)
        seen = _capture_popen(monkeypatch)
        # A module-style entry point: dotted, no separator, no extension, and
        # no such file under the app root.
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("mod-builtin", _manifest("kiro_crew.apps.builtins.demo"))
        assert seen["argv"] == [
            sys.executable,
            "-m",
            "kiro_crew.apps.builtins.demo",
        ]
        # No pip install ran, and the deps dir is NOT on the child PYTHONPATH.
        assert not any("install" in argv for argv in runs), runs
        child_pp = seen["kwargs"]["env"].get("PYTHONPATH", "")
        assert str(app_deps_dir(spawn_root)) not in child_pp, child_pp


# ---------------------------------------------------------------------------
# Spawn body: environment construction and the Popen tail
# ---------------------------------------------------------------------------


class TestSpawnEnvironment:
    def test_platform_overrides_and_the_proxy_secret_reach_the_backend(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """minimal_env() strips these, so the explicit forwards are the contract."""

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / ".app_secret").write_text("  s3cr3t  \n")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", "/checkout")
        monkeypatch.setenv("KIROCREW_EDITION_DIR", "/edition")
        monkeypatch.setenv("KIROCREW_DEVFLEET_REPO", "/opt/kirocrew")
        monkeypatch.setenv("KIROCREW_DEVFLEET_BIN_GH", "/opt/bin/gh")
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("envapp", _manifest("server.py"))
        env = seen["kwargs"]["env"]
        assert env["KIROCREW_PROJECT_DIR"] == "/checkout"
        assert env["KIROCREW_EDITION_DIR"] == "/edition"
        assert env["KIROCREW_DEVFLEET_REPO"] == "/opt/kirocrew"
        assert env["KIROCREW_DEVFLEET_BIN_GH"] == "/opt/bin/gh"
        assert env["KIROCREW_PROXY_SECRET"] == "s3cr3t"
        assert env["KIROCREW_APP_NAME"] == "envapp"

    def test_a_missing_proxy_secret_is_tolerated(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.py").write_text("x = 1\n")
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("nosecret", _manifest("server.py"))
        assert "KIROCREW_PROXY_SECRET" not in seen["kwargs"]["env"]

    def test_an_audit_sink_failure_does_not_block_the_spawn(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.py").write_text("x = 1\n")

        def _boom() -> Any:
            raise RuntimeError("sel unavailable")

        monkeypatch.setattr(bmod, "sel", _boom)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("audit-down", _manifest("server.py"))
        assert seen["argv"]


class TestSpawnOutcome:
    def test_a_surviving_child_is_recorded_and_persisted(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.py").write_text("x = 1\n")
        monkeypatch.setattr(bmod, "_survived_spawn", lambda _proc, _port=None: True)
        recorded: list[tuple[str, int, int, str | None]] = []
        monkeypatch.setattr(
            bmod,
            "_record_app_pid",
            lambda name, pid, port, instance=None: recorded.append((name, pid, port, instance)),
        )
        monkeypatch.setattr(bmod, "popen_limited", lambda *_a, **_k: _FakeProc(pid=777))
        ap = bmod._start_app_backend_body("okapp", _manifest("server.py"))
        assert ap is not None
        assert ap.pid == 777
        # Surviving the bind is NOT health: the health loop owns that transition.
        assert ap.healthy is False
        assert bmod._processes["okapp"] is ap
        # Exact arity, not a slice: the unpack fails if the call grows another
        # argument, which is what keeps this a ratchet on the recorded call.
        assert len(recorded) == 1
        name, pid, port, instance = recorded[0]
        assert (name, pid, port) == ("okapp", 777, ap.port)
        # The spawn's incarnation token is persisted WITH the pid: it is the only
        # thing that can vouch this backend's process group after the leader dies,
        # and a row without it costs the startup reap that group entirely. The
        # value is a fresh uuid per spawn, so only its presence is pinned.
        assert instance

    def test_a_child_that_dies_on_its_bind_is_not_reported_as_started(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A 'started' record for a dead pid makes the proxy 502 and respawn forever."""

        (spawn_root / "server.py").write_text("x = 1\n")
        monkeypatch.setattr(bmod, "_survived_spawn", lambda _proc, _port=None: False)

        def _popen(_argv: Any, **kwargs: Any) -> Any:
            # Write the crash reason the real child would have logged.
            kwargs["stdout"].write("OSError: [Errno 98] Address already in use\n")
            kwargs["stdout"].flush()
            return _FakeProc(returncode=1)

        monkeypatch.setattr(bmod, "popen_limited", _popen)
        with caplog.at_level(logging.ERROR):
            assert bmod._start_app_backend_body("dyingapp", _manifest("server.py")) is None
        assert any("PORT COLLISION" in r.getMessage() for r in caplog.records)
        assert "dyingapp" not in bmod._processes
