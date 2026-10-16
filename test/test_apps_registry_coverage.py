"""Behaviour coverage for the un-exercised helpers of ``kiro_crew.apps.registry``.

The registry module's install path is the interesting half (git clone, build,
identity gates) but most of its surface is small, deterministic helpers that had
no direct test: manifest merge/enrich, cache read/write, sandbox-mode and
trusted-host gates, the stale-checkout sweep, the git-provenance reader, the
build-command chooser, and the post-rejection un-poison routine.

Every subprocess is faked at this module's own chokepoints
(``wrap_argv`` / ``cgroup_scope_argv`` / ``create_subprocess_limited``), matching
the harness already used by ``test_apps_registry.py``, so nothing here spawns
git, npm, or pip. All filesystem work happens under ``tmp_path`` with
``_manifest_cache_dir`` redirected, so no test touches the real Kiro Crew home.

This module holds the build step: ``_run_app_build``'s command selection and execution.
The other helpers are covered by its siblings ``test_apps_registry_coverage_gates.py``,
``_manifests.py``, ``_provenance.py``, ``_install.py`` and ``_characterization.py``.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import pytest
from apps_registry_coverage_helpers import _explicit_registry_execution_admission  # noqa: F401
from apps_registry_coverage_helpers import bundled_interpreter_fixture  # noqa: F401
from apps_registry_coverage_helpers import pip_importable_fixture  # noqa: F401
from apps_registry_coverage_helpers import _DATA_IS_A_FILE, _asgi_backend, _fake_sandbox, _FakeProc

from conftest import requires_symlinks
from kiro_crew.apps import registry
from kiro_crew.apps.manifest import AppManifest

# ---------------------------------------------------------------------------
# Build step selection + execution
# ---------------------------------------------------------------------------


class TestRunAppBuild:
    @pytest.mark.asyncio
    async def test_no_recognized_ecosystem_means_no_build(self, tmp_path):
        log: list[str] = []
        result = await registry._run_app_build(tmp_path, "demo", log, manifest=AppManifest.from_dict({}), self_managed=False)
        assert result == {"ok": True}
        assert "No build step detected — using source as-is" in log

    @pytest.mark.asyncio
    async def test_missing_npm_is_a_soft_skip(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(registry.shutil, "which", lambda name: None)
        log: list[str] = []
        result = await registry._run_app_build(tmp_path, "demo", log, manifest=AppManifest.from_dict({}), self_managed=False)
        assert result == {"ok": True}
        assert any("npm not found on PATH" in line for line in log)

    @pytest.mark.asyncio
    async def test_npm_install_only_when_no_build_script(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest"}}), encoding="utf-8"
        )
        monkeypatch.setattr(registry.shutil, "which", lambda name: "/usr/bin/npm")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        log: list[str] = []
        assert await registry._run_app_build(tmp_path, "demo", log, manifest=AppManifest.from_dict({}), self_managed=False) == {"ok": True}
        assert spawned == [["/usr/bin/npm", "install"]]
        assert log[-1] == "build succeeded"

    @pytest.mark.asyncio
    async def test_declared_build_script_adds_a_second_command(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"build": "vite build"}}), encoding="utf-8"
        )
        monkeypatch.setattr(registry.shutil, "which", lambda name: "/usr/bin/npm")
        spawned = _fake_sandbox(
            monkeypatch, [_FakeProc(returncode=0), _FakeProc(returncode=0)]
        )
        assert await registry._run_app_build(tmp_path, "demo", [], manifest=AppManifest.from_dict({}), self_managed=False) == {"ok": True}
        assert spawned == [
            ["/usr/bin/npm", "install"],
            ["/usr/bin/npm", "run", "build"],
        ]

    @pytest.mark.asyncio
    async def test_unparseable_package_json_still_installs(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text("{ not json", encoding="utf-8")
        monkeypatch.setattr(registry.shutil, "which", lambda name: "/usr/bin/npm")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        assert await registry._run_app_build(tmp_path, "demo", [], manifest=AppManifest.from_dict({}), self_managed=False) == {"ok": True}
        assert spawned == [["/usr/bin/npm", "install"]]

    @pytest.mark.asyncio
    async def test_requirements_only_uses_the_requirements_file(
        self, tmp_path, monkeypatch, pip_importable
    ):
        (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        assert await registry._run_app_build(tmp_path, "demo", [], manifest=AppManifest.from_dict({}), self_managed=False) == {"ok": True}
        assert spawned == [[sys.executable, "-s", "-m", "pip", "install", "-r", "requirements.txt"]]

    @pytest.mark.asyncio
    async def test_pyproject_installs_the_project(
        self, tmp_path, monkeypatch, pip_importable
    ):
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        assert await registry._run_app_build(tmp_path, "demo", [], manifest=AppManifest.from_dict({}), self_managed=False) == {"ok": True}
        assert spawned == [[sys.executable, "-s", "-m", "pip", "install", "."]]

    @pytest.mark.asyncio
    async def test_setup_py_installs_the_project(
        self, tmp_path, monkeypatch, pip_importable
    ):
        (tmp_path / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        assert await registry._run_app_build(tmp_path, "demo", [], manifest=AppManifest.from_dict({}), self_managed=False) == {"ok": True}
        assert spawned == [[sys.executable, "-s", "-m", "pip", "install", "."]]

    @pytest.mark.asyncio
    async def test_missing_path_pip_does_not_skip_the_python_build(
        self, tmp_path, monkeypatch, pip_importable
    ):
        """The Python build runs via ``sys.executable -m pip`` — the gateway's own
        interpreter — so a host with no pip anywhere on PATH must still build."""
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        monkeypatch.setattr(registry.shutil, "which", lambda name: None)
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        assert await registry._run_app_build(tmp_path, "demo", [], manifest=AppManifest.from_dict({}), self_managed=False) == {"ok": True}
        assert spawned == [[sys.executable, "-s", "-m", "pip", "install", "."]]

    @pytest.mark.asyncio
    async def test_desktop_bundled_interpreter_never_runs_pip(self, tmp_path, monkeypatch):
        """pip must never write into the desktop app's signed bundle — and the
        refusal must be LOUD.

        The desktop build ships a python-build-standalone runtime under
        ``Resources/backend-dist/``; on macOS the bundle is code-signed, so a pip
        install into its site-packages invalidates the signature and breaks the
        next launch/update. Reporting a skipped build as ok would recreate the
        silent-broken-install failure this function exists to prevent, so the
        build fails with an explicit error instead.

        Detection routes through ``platform_compat.is_bundled_interpreter()``;
        the tests in ``test_platform_compat_pinned_tools.py`` pin its sentinel to the
        packaging layer so a bundler rename cannot silently un-match this guard.
        """
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        bundled = tmp_path / "App.app" / "Contents" / "Resources" / "backend-dist"
        bundled = bundled / "kirocrew-backend-arm64" / "bin" / "python3.12"
        bundled.parent.mkdir(parents=True, exist_ok=True)
        bundled.write_text("", encoding="utf-8")
        monkeypatch.setattr(registry.sys, "executable", str(bundled))
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(tmp_path, "demo", [], manifest=AppManifest.from_dict({}), self_managed=False)
        assert result["ok"] is False
        assert "bundled interpreter" in result["error"]
        assert spawned == []

    @pytest.mark.asyncio
    async def test_backend_entry_point_requirements_pass_the_bundled_gate(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """A root requirements.txt for an OUT-OF-PROCESS backend is not a
        gateway-import dependency, so the desktop gate must let it through.

        ``apps/backend_runtime/provisioning.py::provision_app_deps`` installs exactly this file with
        ``pip install --target`` into the app's own deps dir at backend start,
        which works on the bundled interpreter. Refusing it here blocked an app
        class the runtime serves. Nothing is pip-installed AT INSTALL TIME — the
        runtime owns it — so no build command may be planned either.
        """
        (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
        log: list[str] = []
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", log, manifest=_asgi_backend(), self_managed=False
        )
        assert result == {"ok": True}
        assert spawned == []
        assert any("provisioned at runtime" in line and "requirements.txt" in line for line in log)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb", ["install", "update"])
    async def test_pyproject_still_refused_on_the_bundled_interpreter(
        self, tmp_path, monkeypatch, bundled_interpreter, verb
    ):
        """A declared backend entry point does not rescue ``pyproject.toml``:
        ``pip install .`` targets the GATEWAY's interpreter, which is the write
        into the signed bundle the refusal exists to prevent. The refusal is
        streamed into the install log like every other refusal, with the run's
        verb -- this is the commonest path to it (an already-trusted app whose
        checkout carries a build step), and the page shows the log."""
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        log: list[str] = []
        result = await registry._run_app_build(
            tmp_path, "demo", log, manifest=_asgi_backend(), self_managed=False, verb=verb
        )
        assert result["ok"] is False
        assert "bundled interpreter" in result["error"]
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert log == [f"Refusing {verb}: {result['error']}"]
        assert spawned == []

    @pytest.mark.asyncio
    async def test_setup_py_still_refused_on_the_bundled_interpreter(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        (tmp_path / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result["ok"] is False
        assert "bundled interpreter" in result["error"]
        assert spawned == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("entry", ["run.sh", "server.js", "server.mjs", "server.cjs"])
    async def test_a_shell_or_node_entry_point_with_requirements_keeps_the_refusal(
        self, tmp_path, monkeypatch, bundled_interpreter, entry
    ):
        """The backend spawn runs ``provision_app_deps`` for ANY file-style entry,
        so the file is pip-installed at spawn for a shell or node entry too -- but
        the spawn hands the tree to a Python child only (the ``deps_boot`` shim on
        the gateway interpreter, the shim under an ABI-matched shebang, or
        ``PYTHONPATH`` on an ABI match); a shell or node child gets none of the
        three. Waiving here would install an app whose declared dependencies land
        beside a process that can never import them -- the silent-broken shape
        the refusal exists to prevent -- so the gate counts such an entry as no
        consumer and keeps the refusal, with the desktop code."""
        (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
        (tmp_path / entry).write_text("", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        log: list[str] = []
        result = await registry._run_app_build(
            tmp_path, "demo", log, manifest=AppManifest.from_dict({"name": "demo", "backend": {"entryPoint": entry}}), self_managed=False
        )
        assert result["ok"] is False
        assert "bundled interpreter" in result["error"]
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []
        assert not any("provisioned at runtime" in line for line in log)

    @pytest.mark.asyncio
    async def test_a_shell_entry_point_without_requirements_is_untouched(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """No ``requirements.txt`` means nothing for the gate to judge: a shell
        entry installs as it always did, with no build step."""
        (tmp_path / "run.sh").write_text("", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        log: list[str] = []
        result = await registry._run_app_build(
            tmp_path, "demo", log, manifest=AppManifest.from_dict({"name": "demo", "backend": {"entryPoint": "run.sh"}}), self_managed=False
        )
        assert result == {"ok": True}
        assert spawned == []
        assert "No build step detected — using source as-is" in log

    @pytest.mark.asyncio
    async def test_a_stdio_server_still_waives_beside_a_shell_entry_point(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """The stdio server is a consumer the deps reach (``bridges.py`` provisions
        for it at registration and launches it as Python), so it waives the file
        beside a shell entry exactly as it does beside an entry the spawn would
        refuse."""
        (tmp_path / "requirements.txt").write_text("mcp\n", encoding="utf-8")
        (tmp_path / "run.sh").write_text("", encoding="utf-8")
        manifest = AppManifest.from_dict(
            {
                "name": "demo",
                "backend": {"entryPoint": "run.sh"},
                "mcpServers": {"tool": {"command": "python3", "args": ["srv.py"]}},
            }
        )
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=manifest, self_managed=False
        )
        assert result == {"ok": True}
        assert spawned == []

    @pytest.mark.asyncio
    async def test_an_absent_shell_entry_point_is_refused_at_the_build_pass_too(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """The build pass defers a merely-absent PYTHON entry to the final pass
        (the script window may create the file); a declared shell entry is refused
        at once -- its suffix is the manifest's, and no script window changes what
        the deps tree reaches."""
        (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=AppManifest.from_dict({"name": "demo", "backend": {"entryPoint": "run.sh"}}), self_managed=False
        )
        assert result["ok"] is False
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []

    @pytest.mark.asyncio
    async def test_requirements_without_an_out_of_process_consumer_stay_refused(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """No ``backend.entryPoint`` and no stdio ``mcpServers`` entry means
        nothing spawns for this app, so neither runtime provisioner would ever
        install the file — a pass would report success for an install that put
        the dependencies nowhere, the broken-install shape the loud refusal
        prevents.
        """
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=AppManifest.from_dict({"name": "demo"}), self_managed=False
        )
        assert result["ok"] is False
        assert "bundled interpreter" in result["error"]
        assert spawned == []

    @pytest.mark.asyncio
    async def test_requirements_beside_pyproject_stay_refused(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """The refusal wins when both are present: the non-bundled branch would
        run ``pip install .`` for this layout, so the gateway-import dependency
        is the one that decides."""
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result["ok"] is False
        assert "bundled interpreter" in result["error"]
        assert spawned == []

    @pytest.mark.asyncio
    async def test_a_manifest_declaring_nothing_is_refused(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """Fail-closed: ``{}`` is what a caller passes for "declares nothing", and
        it must not read as "no hooks declared, so allow"."""
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(tmp_path, "demo", [], manifest=AppManifest.from_dict({}), self_managed=False)
        assert result["ok"] is False
        assert spawned == []

    @pytest.mark.asyncio
    async def test_declared_hooks_keep_the_refusal_even_with_an_entry_point(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """``backend.hooks`` runs INSIDE the gateway process.

        A manifest may declare both an entry point and hooks, and the deps tree
        the backend runner provisions never joins the gateway's import path
        (module_loader loads a hook straight into this process). Waiving the
        refusal on the entry point alone would install such an app "successfully"
        with its hook imports broken and its routes degraded — the silent-broken
        install the loud refusal exists to prevent.
        """
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        for hook in ("routes", "on_startup", "on_shutdown"):
            spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
            result = await registry._run_app_build(
                tmp_path,
                "demo",
                [],
                manifest=_asgi_backend(hooks={hook: "backend.hooks:fn"}),
                self_managed=False,
            )
            assert result["ok"] is False, hook
            assert "bundled interpreter" in result["error"]
            assert spawned == []

    @pytest.mark.asyncio
    async def test_blank_hook_fields_are_not_a_declared_hook(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """``hooks: {}`` — and an EMPTY field — declares no in-gateway code, so
        the waiver still applies: the check is on a declared hook, not on the key.

        A whitespace-only field is the other way round and is refused: the typed
        view keeps it, so ``lifecycle.py`` would take it as a hook path, fail to
        resolve it and mark the app degraded. That app has in-gateway Python as
        far as the runtime is concerned, so the gate must not waive for it.
        """
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path,
            "demo",
            [],
            manifest=_asgi_backend(hooks={"routes": "", "on_startup": ""}),
            self_managed=False,
        )
        assert result == {"ok": True}
        assert spawned == []

        blank = _asgi_backend(hooks={"on_startup": "  "})
        assert blank.backend.hooks.to_dict() == {"on_startup": "  "}
        result = await registry._run_app_build(tmp_path, "demo", [], manifest=blank, self_managed=False)
        assert result["ok"] is False
        assert "bundled interpreter" in result["error"]
        assert spawned == []

    @pytest.mark.asyncio
    async def test_the_gate_reads_hooks_the_way_the_loaders_are_fed(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """A hooks value the manifest view drops is a hook that never loads.

        ``manager.py`` hands the hook loaders
        ``AppManifest.from_json_file(...).to_dict()``, and
        ``BackendConfig.from_dict`` drops a non-object ``hooks`` — so for a
        manifest like this one NOTHING is imported into the gateway, and the gate
        must agree with that rather than refuse an app the runtime would run
        hookless. Pinned because the opposite reading (refuse whatever looks
        unreadable in the raw bytes) is the tempting one, and it would disagree
        with the only view that decides.
        """
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        manifest = _asgi_backend(hooks="backend.hooks:fn")
        assert manifest.backend.hooks.to_dict() == {}, "the typed view must drop it"
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(tmp_path, "demo", [], manifest=manifest, self_managed=False)
        assert result == {"ok": True}
        assert spawned == []

    @pytest.mark.asyncio
    async def test_a_stdio_mcp_server_app_passes_the_bundled_gate(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """The waiver is the runtime's OWN condition, not the entry point.

        ``bridges.py::_maybe_provision_backendless_deps`` provisions a root
        requirements.txt for an app whose only Python is a stdio ``mcpServers``
        entry — the same ``pip install --target`` into the app's deps dir, so the
        same out-of-process consumer. Refusing it while waiving the entry-point
        shape would be a point patch on the reported symptom.
        """
        (tmp_path / "requirements.txt").write_text("mcp\n", encoding="utf-8")
        manifest = AppManifest.from_dict(
            {"name": "demo", "mcpServers": {"tool": {"command": "python3", "args": ["srv.py"]}}}
        )
        log: list[str] = []
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(tmp_path, "demo", log, manifest=manifest, self_managed=False)
        assert result == {"ok": True}
        assert spawned == []
        assert any("requirements.txt" in line for line in log)

    @pytest.mark.asyncio
    async def test_a_url_only_mcp_server_is_not_an_out_of_process_consumer(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """A ``url`` server is remote: nothing spawns for it, so the runtime
        provisions nothing (``bridges.py`` counts only entries WITHOUT ``url``),
        and the gate must agree — refused, like any requirements.txt with no
        consumer."""
        (tmp_path / "requirements.txt").write_text("mcp\n", encoding="utf-8")
        manifest = AppManifest.from_dict(
            {"name": "demo", "mcpServers": {"remote": {"url": "https://example.test/mcp"}}}
        )
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(tmp_path, "demo", [], manifest=manifest, self_managed=False)
        assert result["ok"] is False
        assert "bundled interpreter" in result["error"]
        assert spawned == []

    @pytest.mark.asyncio
    async def test_a_stdio_app_with_hooks_stays_refused(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """Hooks import into the gateway process regardless of what else the app
        ships, so a stdio server does not rescue an app that also declares one."""
        (tmp_path / "requirements.txt").write_text("mcp\n", encoding="utf-8")
        manifest = AppManifest.from_dict(
            {
                "name": "demo",
                "mcpServers": {"tool": {"command": "python3", "args": ["srv.py"]}},
                "backend": {"hooks": {"on_startup": "backend.hooks:start"}},
            }
        )
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(tmp_path, "demo", [], manifest=manifest, self_managed=False)
        assert result["ok"] is False
        assert "bundled interpreter" in result["error"]
        assert spawned == []

    @pytest.mark.asyncio
    async def test_a_module_style_entry_point_keeps_the_refusal(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """Neither provisioner installs requirements.txt for a MODULE-style entry.

        A dotted, extensionless ``backend.entryPoint`` with no file of that name
        runs as ``python -m`` from trusted package code; ``backend.py`` skips
        ``provision_app_deps`` for it and ``bridges.py`` returns before provisioning
        (trust boundary: an app-dir requirements file must never load ahead of a
        trusted module). Waiving on the entry point's mere presence would report a
        successful install whose dependencies land nowhere -- the same silent-broken
        install as a pass with no consumer at all -- so the shape the provisioners
        refuse is refused here, by the predicate they share.
        """
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path,
            "demo",
            [],
            manifest=_asgi_backend("kiro_crew.apps.builtins.demo.server"),
            self_managed=False,
        )
        assert result["ok"] is False
        assert "bundled interpreter" in result["error"]
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []

    @pytest.mark.asyncio
    async def test_a_self_managed_entry_keeps_the_refusal(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """Neither provisioner runs for a self-managed app, whatever it declares.

        A registry entry with ``resources: "app"`` is registered from its manifest
        alone: ``install_from_registry`` copies no source into the app directory,
        ``bridges.py`` skips every registration for it, and the app launches
        itself. On a source install the build step's ``pip install -r`` was the
        only thing that installed its requirements.txt, and that step is what the
        bundled interpreter cannot run -- so the very manifest that waives for a
        gateway-managed entry (``test_backend_entry_point_requirements_pass_the_bundled_gate``)
        refuses here. Ownership is a registry fact the manifest does not carry,
        which is why it is a separate, required input to the verdict.
        """
        (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=True
        )
        assert result["ok"] is False
        assert "bundled interpreter" in result["error"]
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []

    @pytest.mark.parametrize("decided_by", ["self-managed", "hooks"])
    @pytest.mark.asyncio
    async def test_a_refusal_the_manifest_decides_takes_no_preview_copy(
        self, tmp_path, monkeypatch, bundled_interpreter, decided_by
    ):
        """A self-managed entry and a declared `backend.hooks` are refused by the
        manifest and the registry entry alone, in both passes; nothing of theirs
        reads the tree, so the gate answers before it copies it -- otherwise every
        such install would run two full copies (build pass and final pass) that no
        predicate ever looked at."""
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        copies: list[Path] = []

        def _counting_copy(source, dest, **kwargs):
            copies.append(Path(dest))
            pytest.fail("the preview copy must not be produced for a manifest-decided refusal")

        monkeypatch.setattr(registry, "copy_app_tree_as_installed", _counting_copy)
        if decided_by == "self-managed":
            manifest, self_managed = _asgi_backend(), True
        else:
            manifest, self_managed = _asgi_backend(hooks={"on_startup": "backend.hooks:fn"}), False
        for final in (False, True):
            assert (
                registry._desktop_build_refusal(
                    tmp_path, manifest, self_managed=self_managed, final=final
                )
                == registry._DESKTOP_BUILD_REFUSAL
            )
        assert copies == []

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_a_requirements_symlink_escaping_the_app_root_keeps_the_refusal(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """The runtime refuses to READ a requirements.txt that resolves outside the
        app root (``provision_app_deps``: "out-of-root symlinked requirements are
        not installed") and the backend spawns without its deps -- so the waiver,
        which exists because the runtime provisions the file, must not apply to a
        link the runtime will refuse. ``install_app`` copies with ``symlinks=True``,
        so the link in the checkout is the link in the app directory. Waiving would
        report a successful install with nothing installed, and let the gate
        treat bytes outside the app directory as the app's own.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        app_root = tmp_path / "app"
        app_root.mkdir()
        (app_root / "server.py").write_text("", encoding="utf-8")
        (app_root / "requirements.txt").symlink_to(outside / "requirements.txt")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            app_root, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result["ok"] is False
        assert "bundled interpreter" in result["error"]
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_an_in_tree_requirements_symlink_is_waived_like_the_runtime_reads_it(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """``requirements.txt -> requirements/prod.txt`` is layout the provisioner
        accepts (a link whose strict resolution stays inside the app root), so the
        gate accepts it too: refusing it would re-create the refused-what-the-
        runtime-serves defect for that layout."""
        (tmp_path / "requirements").mkdir()
        (tmp_path / "requirements" / "prod.txt").write_text("fastapi\n", encoding="utf-8")
        (tmp_path / "requirements.txt").symlink_to(Path("requirements") / "prod.txt")
        log: list[str] = []
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", log, manifest=_asgi_backend(), self_managed=False
        )
        assert result == {"ok": True}
        assert spawned == []
        assert any("provisioned at runtime" in line for line in log)

    @requires_symlinks
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "target_dir",
        ["data", "node_modules", ".venv", "vendor/node_modules"],
        ids=["data (replaced on update)", "node_modules", ".venv", "nested node_modules"],
    )
    async def test_a_link_into_a_tree_the_install_does_not_carry_over_keeps_the_refusal(
        self, tmp_path, monkeypatch, bundled_interpreter, target_dir
    ):
        """The provisioner reads requirements.txt in the APP DIRECTORY. The copy
        keeps the link as a link but drops the build-input dirs at every depth
        and, on an update, replaces ``data/`` with the preserved previous one --
        so a link that resolves in this checkout dangles there, the provisioner
        refuses it, and the install would already have reported success. The
        copy itself decides: the gate judges a real copy of the checkout."""
        monkeypatch.setattr(registry, "preserved_data_awaits", lambda name: True)  # an update
        target = tmp_path / target_dir / "requirements.txt"
        target.parent.mkdir(parents=True)
        target.write_text("fastapi\n", encoding="utf-8")
        (tmp_path / "requirements.txt").symlink_to(Path(target_dir) / "requirements.txt")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result["ok"] is False
        assert "bundled interpreter" in result["error"]
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_on_a_first_install_the_source_s_data_dir_is_carried_and_a_link_into_it_is_waived(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """With no preserved `data/` to put back, `install_app` copies the source's
        `data/` and leaves it, so `requirements.txt -> data/requirements.txt`
        resolves in the app directory exactly as it does here: the runtime reads it
        and the gate waives it. The first UPDATE replaces `data/` and this same gate
        then refuses the link, loudly, before the working install is touched."""
        monkeypatch.setattr(registry, "preserved_data_awaits", lambda name: False)
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        (tmp_path / "requirements.txt").symlink_to(Path("data") / "requirements.txt")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result == {"ok": True}
        assert spawned == []
        assert (
            registry._desktop_build_refusal(
                tmp_path, _asgi_backend(), self_managed=False, final=True
            )
            == ""
        )

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_a_dangling_requirements_link_passes_the_build_pass_and_fails_the_final_one(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """``provision_app_deps`` treats a present-but-unreadable requirements.txt
        (a dangling link) as a provisioning FAILURE; the backend then spawns
        without its deps and dies on import, so the waiver -- granted because the
        runtime provisions the file -- must not pass an app that cannot run. But
        the link's target may be what ``setup.onInstall`` generates, so the build
        pass lets a dangling link through and the final pass, on the post-script
        checkout, is the one that refuses. Presence is the provisioner's
        ``lexists``, not the build detector's ``is_file``, in both."""
        (tmp_path / "requirements.txt").symlink_to(tmp_path / "gone.txt")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result == {"ok": True}
        assert spawned == []
        assert (
            registry._desktop_build_refusal(
                tmp_path, _asgi_backend(), self_managed=False, final=True
            )
            == registry._DESKTOP_BUILD_REFUSAL
        )

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_a_dangling_requirements_link_nothing_reads_is_no_file(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """With no consumer, nothing of ours ever reads the entry, and the build's
        own detection (``is_file``, on every host) sees no Python build files: it
        passes, exactly as the same checkout does on a source install."""
        (tmp_path / "requirements.txt").symlink_to(tmp_path / "gone.txt")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=AppManifest.from_dict({"name": "demo"}), self_managed=False
        )
        assert result == {"ok": True}
        assert spawned == []

    @pytest.mark.asyncio
    async def test_a_requirements_file_over_the_provisioners_cap_keeps_the_refusal(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """The provisioner reads requirements.txt through a bounded buffer and
        refuses one over its cap ("exceeds the size cap"), after which the
        backend never gets its deps -- so the waiver, granted because the
        provisioner installs the file, does not apply to one it will refuse. The
        cap is the provisioner's own constant, applied by the shared rule."""
        from kiro_crew.apps.manifest import REQUIREMENTS_TXT_MAX_BYTES

        (tmp_path / "requirements.txt").write_bytes(b"#" + b"x" * REQUIREMENTS_TXT_MAX_BYTES)
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result["ok"] is False
        assert "bundled interpreter" in result["error"]
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []

    @pytest.mark.asyncio
    async def test_a_merely_absent_entry_point_passes_the_build_pass_pending_the_final_one(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """The build pass runs BEFORE ``setup.onInstall``, whose documented window
        ("after clone/build, before the installed copy is created") is exactly
        where an app may generate its entry file. Refusing the declared name here
        would refuse that app before its script ever ran, while a gateway host
        installs it -- so absence is let through now, nothing pip-installed either
        way, and the final pass (``final=True``, on the post-script checkout)
        refuses if the file never appeared: see the end-to-end cases in
        ``test_apps_registry_desktop_layout.py``.
        """
        (tmp_path / "server.py").unlink()  # the fixture's entry file: this case is about its absence
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result == {"ok": True}
        assert spawned == []
        # The same absence IS refused by the final pass's strict verdict.
        assert (
            registry._desktop_build_refusal(
                tmp_path, _asgi_backend(), self_managed=False, final=True
            )
            == registry._DESKTOP_BUILD_REFUSAL
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("entry", ["data/server.py", "node_modules/server.py"])
    async def test_an_entry_point_the_install_does_not_carry_over_keeps_the_refusal(
        self, tmp_path, monkeypatch, bundled_interpreter, entry
    ):
        """The spawn looks for the entry file in the APP DIRECTORY. `install_app`
        drops the build-input dirs and an update replaces ``data/`` with the
        preserved previous one, so an entry file that exists here under either is
        stale or missing there -- the backend provisioner is no reason to waive,
        in either pass. The copy itself (the gate judges a real copy)
        decides; a stdio server beside it, which `bridges.py` provisions for on
        its own, still can."""
        monkeypatch.setattr(registry, "preserved_data_awaits", lambda name: True)  # an update
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        target = tmp_path / entry
        target.parent.mkdir(parents=True)
        target.write_text("", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(entry), self_managed=False
        )
        assert result["ok"] is False
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []
        assert (
            registry._desktop_build_refusal(
                tmp_path, _asgi_backend(entry), self_managed=False, final=True
            )
            == registry._DESKTOP_BUILD_REFUSAL
        )
        with_stdio = AppManifest.from_dict(
            {
                "name": "demo",
                "backend": {"entryPoint": entry, "type": "asgi"},
                "mcpServers": {"tool": {"command": "python3", "args": ["srv.py"]}},
            }
        )
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=with_stdio, self_managed=False
        )
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_on_a_first_install_an_entry_point_under_data_is_the_source_s_and_waives(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """No preserved `data/` awaits on a first install, so `install_app` leaves the
        source's `data/server.py` where the spawn will find it: the gate waives it,
        while `node_modules/server.py`, dropped by the copy on every install, is
        still refused."""
        monkeypatch.setattr(registry, "preserved_data_awaits", lambda name: False)
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        for entry in ("data/server.py", "node_modules/server.py"):
            target = tmp_path / entry
            target.parent.mkdir(parents=True)
            target.write_text("", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend("data/server.py"), self_managed=False
        )
        assert result == {"ok": True}
        assert (
            registry._desktop_build_refusal(
                tmp_path, _asgi_backend("data/server.py"), self_managed=False, final=True
            )
            == ""
        )
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend("node_modules/server.py"), self_managed=False
        )
        assert result["ok"] is False
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_an_entry_point_linked_into_a_tree_the_install_replaces_keeps_the_refusal(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """The declared spelling can be clean while the FILE is not: `install_app`
        keeps an in-tree link as a link, so a ``server.py -> data/server.py``
        would run the preserved OLD data file after an update. The copy rule is
        asked about where the entry resolves, not only what it is called."""
        monkeypatch.setattr(registry, "preserved_data_awaits", lambda name: True)  # an update
        (tmp_path / "server.py").unlink()  # the fixture's regular file; here it is a link
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "server.py").write_text("", encoding="utf-8")
        (tmp_path / "server.py").symlink_to(Path("data") / "server.py")
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result["ok"] is False
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []
        assert (
            registry._desktop_build_refusal(
                tmp_path, _asgi_backend(), self_managed=False, final=True
            )
            == registry._DESKTOP_BUILD_REFUSAL
        )

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_a_requirements_link_that_climbs_out_and_re_enters_by_name_keeps_the_refusal(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """The copy keeps a relative link's TEXT verbatim. A text that climbs above
        the root and re-enters by naming the checkout resolves to an in-tree file
        HERE and, from the app directory, to the checkout's file -- outside the
        root the provisioner reads under, so it refuses what the gate waived."""
        app_root = tmp_path / "app-sources" / "demo"
        (app_root / "requirements").mkdir(parents=True)
        (app_root / "requirements" / "prod.txt").write_text("fastapi\n", encoding="utf-8")
        (app_root / "server.py").write_text("", encoding="utf-8")
        (app_root / "requirements.txt").symlink_to(
            Path("..") / ".." / "app-sources" / "demo" / "requirements" / "prod.txt"
        )
        assert (app_root / "requirements.txt").is_file()  # in-tree, in the checkout
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            app_root, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result["ok"] is False
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []
        assert (
            registry._desktop_build_refusal(
                app_root, _asgi_backend(), self_managed=False, final=True
            )
            == registry._DESKTOP_BUILD_REFUSAL
        )

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_an_entry_point_link_that_climbs_out_and_re_enters_by_name_keeps_the_refusal(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """Same relocation rule for the declared entry: `server.py ->
        ../../app-sources/demo/real/server.py` runs here and, in the app
        directory, points back at the checkout -- outside the spawn's root."""
        app_root = tmp_path / "app-sources" / "demo"
        (app_root / "real").mkdir(parents=True)
        (app_root / "real" / "server.py").write_text("", encoding="utf-8")
        (app_root / "server.py").symlink_to(
            Path("..") / ".." / "app-sources" / "demo" / "real" / "server.py"
        )
        (app_root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            app_root, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result["ok"] is False
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []

    @requires_symlinks
    @pytest.mark.parametrize(
        ("leaf", "waived"),
        [
            # A registry `subdirectory` named `app`: `../app/...` is in-tree in the
            # checkout, whose leaf directory IS `app`, and lands in `apps/app/` --
            # another app's directory, or nothing -- once installed under
            # `apps/demo/`. The preview must resolve it the way the app directory
            # will, not the way a temporary directory named `app` would.
            pytest.param("app", False, id="re-enters-by-the-subdirectory-name"),
            # The same shape naming the APP: `../demo/...` is in-tree in the
            # checkout and in-tree under `apps/demo/` alike, so the runtime reads
            # it and the gate waives it.
            pytest.param("demo", True, id="re-enters-by-the-app-name"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_link_that_re_enters_by_the_leaf_name_resolves_as_it_will_in_the_app_dir(
        self, tmp_path, monkeypatch, bundled_interpreter, leaf, waived
    ):
        """The copy keeps a relative link's TEXT verbatim, and a text that climbs one
        level and re-enters by NAME resolves by the leaf name actually around it.
        The preview is therefore written under the destination's own leaf name --
        the manifest's app name, what `app_dir` joins -- so `../<leaf>/...` is
        judged exactly as the runtime will judge it from `apps/<name>/`."""
        app_root = tmp_path / "app-sources" / "demo" / leaf
        (app_root / "requirements").mkdir(parents=True)
        (app_root / "requirements" / "prod.txt").write_text("fastapi\n", encoding="utf-8")
        (app_root / "server.py").write_text("", encoding="utf-8")
        (app_root / "requirements.txt").symlink_to(
            Path("..") / leaf / "requirements" / "prod.txt"
        )
        assert (app_root / "requirements.txt").is_file()  # in-tree, in the checkout
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            app_root, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        final = registry._desktop_build_refusal(
            app_root, _asgi_backend(), self_managed=False, final=True
        )
        if waived:
            assert result["ok"] is True
            assert final == ""
        else:
            assert result["ok"] is False
            assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
            assert spawned == []
            assert final == registry._DESKTOP_BUILD_REFUSAL

    @pytest.mark.asyncio
    async def test_the_preview_is_written_beside_the_apps_root_under_the_app_s_name(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """Where the preview lands is part of the model: under the destination's own
        leaf name, in a `<name>.partial-<hex>` sibling of the checkout under
        app-sources -- the filesystem the install writes to (the one the copy's
        case-folding probe must answer for), and the one name the retention sweep
        already retires, so a copy an unclean exit leaves behind is collected like
        any abandoned partial tree -- and gone again once judged."""
        home = tmp_path / "home"
        source = home / "app-sources" / "demo"
        source.mkdir(parents=True)
        (source / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        (source / "server.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(registry, "_app_sources_dir", lambda: home / "app-sources")
        seen: list[Path] = []
        real_copy = registry.copy_app_tree_as_installed

        def _recording_copy(source, dest, **kwargs):
            seen.append(Path(dest))
            real_copy(source, dest, **kwargs)

        monkeypatch.setattr(registry, "copy_app_tree_as_installed", _recording_copy)
        assert (
            registry._desktop_build_refusal(
                source, _asgi_backend(), self_managed=False, final=True
            )
            == ""
        )
        assert len(seen) == 1
        preview = seen[0]
        assert preview.name == "demo"
        assert preview.parent.parent == home / "app-sources"
        assert preview.parent.name.startswith("demo.partial-")
        assert registry._is_stale_candidate(preview.parent)
        assert not preview.parent.exists()
        assert [p.name for p in (home / "app-sources").iterdir()] == ["demo"]

    async def _judge_a_deep_self_link(self, tmp_path, monkeypatch, depth):
        """Declare `B -> .` plus ``backend.entryPoint: "B/B/.../B/server.py"`` with
        *depth* components and put it through the three judgements the install
        makes of a declared entry -- the runtime's own file check, the build gate
        and the final gate -- timed together. Returns the runtime's refusal (``""``
        when it accepts the path), the build result, the final refusal and the
        elapsed seconds."""
        from kiro_crew.apps.manifest import file_entry_point_refusal

        (tmp_path / "B").symlink_to(Path("."))
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        deep = "/".join(["B"] * depth) + "/server.py"
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        started = time.monotonic()
        runtime_refusal = file_entry_point_refusal(deep, tmp_path)
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(deep), self_managed=False
        )
        final = registry._desktop_build_refusal(
            tmp_path, _asgi_backend(deep), self_managed=False, final=True
        )
        elapsed = time.monotonic() - started
        assert spawned == []  # nothing planned either way
        return runtime_refusal, result, final, elapsed

    @requires_symlinks
    @pytest.mark.timeout(60)
    @pytest.mark.parametrize("depth", [30, 60])
    @pytest.mark.asyncio
    async def test_a_self_pointing_directory_link_under_a_deep_declared_path_is_judged_in_bounded_time(
        self, tmp_path, monkeypatch, bundled_interpreter, depth
    ):
        """A registry app controls both the tree and the declared path: `B -> .`
        plus ``backend.entryPoint: "B/B/.../B/server.py"``. The verdict is
        derived from the install's own copy, which keeps `B` as one link and never
        walks through it, and from the runtime's own file check, which the
        kernel resolves in one pass -- so the judgment is linear in the path's
        components, and on every host it comes back within the bound as one of
        the verdicts each judge owns, never as an exception or an ordinary
        install error. The final gate derives its verdict from the runtime's own
        file check (`_requirements_owned_by_the_runtime` asks
        `file_entry_point_refusal` of the install's preview copy), so on every
        host the two agree: what the runtime accepts the final pass waives, what
        it refuses the final pass refuses. WHICH way a 60-link path goes belongs
        to the host -- the POSIX symlink budget refuses it (pinned by the
        sibling below), and on Windows the shared path screen, which reads the
        link chain hop by hop before anything follows it, refuses a chain past
        its own hop budget -- so no single verdict is asserted here."""
        runtime_refusal, result, final, elapsed = await self._judge_a_deep_self_link(
            tmp_path, monkeypatch, depth
        )
        # Each judge answers with a verdict it owns -- never an escape, since the
        # link points at its own directory, and never an ordinary install error.
        assert runtime_refusal in (
            "",
            "not found",
            "path resolution failed",
            "refused by the path screen",
        ), runtime_refusal
        assert result["ok"] or result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED, result
        assert final in ("", registry._DESKTOP_BUILD_REFUSAL)
        # The final pass says exactly what the runtime's own file check says: the
        # waiver it grants is the provisioning the runtime would perform, and the
        # refusal is the entry the spawn would refuse -- whatever the kernel made
        # of the link.
        assert (final == "") is (runtime_refusal == ""), (runtime_refusal, final)
        # The two gate passes differ in one respect only -- the build pass waives an
        # entry that is merely absent, the final pass refuses it -- so a refusal
        # from the build pass is always the final pass's desktop refusal.
        assert result["ok"] or final == registry._DESKTOP_BUILD_REFUSAL, (result, final)
        assert elapsed < 20, f"the desktop gate took {elapsed:.1f}s on a {depth}-component path"

    @requires_symlinks
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="the symlink budget a deep self-link exhausts is POSIX ELOOP; Windows "
        "reparse-point resolution has no per-walk budget to pin",
    )
    @pytest.mark.timeout(60)
    @pytest.mark.parametrize("depth", [30, 60])
    @pytest.mark.asyncio
    async def test_the_desktop_gate_says_what_the_spawn_meets_at_the_kernel_symlink_budget(
        self, tmp_path, monkeypatch, bundled_interpreter, depth
    ):
        """A POSIX kernel budgets the links one resolution may follow (40 on
        Linux, 32 on macOS): 30 links through `B -> .` resolve to the root's
        `server.py` (waived) and 60 exceed the budget (refused here, loudly,
        instead of failing at spawn). Both gate passes say exactly what the
        runtime's own file check says, because a budget the kernel refuses is a
        layout no script window lifts, not an absent file the build pass waives."""
        runtime_refusal, result, final, _ = await self._judge_a_deep_self_link(
            tmp_path, monkeypatch, depth
        )
        runtime_accepts = runtime_refusal == ""
        assert runtime_accepts is (depth == 30), runtime_refusal
        assert result["ok"] is runtime_accepts, result
        assert (final == "") is runtime_accepts

    @pytest.mark.asyncio
    async def test_a_preview_copy_the_install_copy_cannot_produce_is_an_ordinary_error(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """The verdict is derived from the install's own copy of the checkout. When
        that copy itself fails (disk full, a permission), the install would fail on
        the very same call -- so the gate raises an ordinary error naming the cause,
        never the permanent refusal: the UI keeps Try again, and the app's author is
        not told to fix a layout that is fine."""
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")

        def _copy_fails(source, dest, **kwargs):
            raise OSError(28, "No space left on device", str(dest))

        monkeypatch.setattr(registry, "copy_app_tree_as_installed", _copy_fails)
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        with pytest.raises(RuntimeError, match="could not preview the installed tree"):
            await registry._run_app_build(
                tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
            )
        assert spawned == []
        with pytest.raises(RuntimeError, match="No space left on device"):
            registry._desktop_build_refusal(
                tmp_path, _asgi_backend(), self_managed=False, final=True
            )

    @pytest.mark.asyncio
    async def test_a_root_data_file_is_refused_by_the_preview_with_the_install_s_own_refusal(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """A first-install preview kept the source's root `data` whatever it was, so
        a registry app shipping a FILE of that name passed the waiver; `install_app`
        then wrote its record and raised at `app_data_dir()`, leaving a partial
        installed record. The preview now asks the install's own question of the
        copy and raises the install's own refusal -- in both passes, without the
        desktop code (a browser install refuses it too) -- and the holder is gone.
        An UPDATE puts the preserved directory back over the file: nothing to refuse."""
        from kiro_crew.apps.manager import InstalledTreeRefused

        home = tmp_path / "home"
        source = home / "app-sources" / "demo"
        source.mkdir(parents=True)
        (source / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        (source / "server.py").write_text("", encoding="utf-8")
        (source / "data").write_text("a file\n", encoding="utf-8")
        monkeypatch.setattr(registry, "_app_sources_dir", lambda: home / "app-sources")
        monkeypatch.setattr(registry, "preserved_data_awaits", lambda name: False)
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        log: list[str] = []
        result = await registry._run_app_build(
            source, "demo", log, manifest=_asgi_backend(), self_managed=False
        )
        assert result["ok"] is False
        assert result["error"] == _DATA_IS_A_FILE, result
        assert "code" not in result  # not the desktop code: a browser install refuses it too
        assert log == [f"Refusing install: {result['error']}"]
        assert spawned == []
        assert [p.name for p in (home / "app-sources").iterdir()] == ["demo"]
        # The bare gate raises the install's own refusal; the final pass's caller
        # converts it the same way (see test_apps_registry_desktop_layout.py, end to end).
        with pytest.raises(InstalledTreeRefused, match=re.escape(_DATA_IS_A_FILE)):
            registry._desktop_build_refusal(
                source, _asgi_backend(), self_managed=False, final=True
            )
        assert [p.name for p in (home / "app-sources").iterdir()] == ["demo"]
        monkeypatch.setattr(registry, "preserved_data_awaits", lambda name: True)
        assert (
            registry._desktop_build_refusal(
                source, _asgi_backend(), self_managed=False, final=True
            )
            == ""
        )

    @pytest.mark.asyncio
    async def test_an_entry_point_under_a_case_variant_of_the_data_dir_is_judged_as_the_host_would(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """On a case-folding filesystem `Data/` IS the gateway's `data`, replaced on
        update by the preserved previous one, so `Data/server.py` would run stale
        code there and is refused; on a case-sensitive one (a Linux desktop) it is
        the app's own directory, carried as itself, and is waived. The preview
        removes by the install's own direct path, so the filesystem it stands on
        decides -- checked here against a direct look at this host."""
        monkeypatch.setattr(registry, "preserved_data_awaits", lambda name: True)  # an update
        (tmp_path / "Data").mkdir()
        (tmp_path / "Data" / "server.py").write_text("", encoding="utf-8")
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        folds = (tmp_path / "DATA").is_dir()  # the host's own answer for the same name
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend("Data/server.py"), self_managed=False
        )
        assert result["ok"] is (not folds), (folds, result)
        if folds:
            assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []

    @pytest.mark.asyncio
    async def test_a_requirements_link_to_a_file_the_copy_drops_by_name_keeps_the_refusal(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """The copy's ``ignore`` callback drops by NAME, whatever the entry's type:
        a regular file called ``node_modules`` is dropped like the directory would
        be. So ``requirements.txt -> node_modules`` resolves to a real in-tree file
        here and dangles in the app directory, where the provisioner reads it. The
        copy rule is asked about every component of the target, the leaf included."""
        (tmp_path / "node_modules").write_text("fastapi\n", encoding="utf-8")
        (tmp_path / "requirements.txt").symlink_to(Path("node_modules"))
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result["ok"] is False
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []
        assert (
            registry._desktop_build_refusal(
                tmp_path, _asgi_backend(), self_managed=False, final=True
            )
            == registry._DESKTOP_BUILD_REFUSAL
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("entry", ["node_modules", ".venv"])
    async def test_an_entry_point_named_like_a_dropped_entry_keeps_the_refusal(
        self, tmp_path, monkeypatch, bundled_interpreter, entry
    ):
        """A declared entry FILE whose own name is one the copy drops (``node_modules``,
        ``.venv`` -- files, not directories, and not module-style since the file
        exists) is a real regular file the spawn would run here and nothing in the
        app directory: the leaf is judged like every other component, and only a
        stdio server beside it can still waive."""
        (tmp_path / entry).write_text("", encoding="utf-8")
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(entry), self_managed=False
        )
        assert result["ok"] is False
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []
        assert (
            registry._desktop_build_refusal(
                tmp_path, _asgi_backend(entry), self_managed=False, final=True
            )
            == registry._DESKTOP_BUILD_REFUSAL
        )

    @pytest.mark.asyncio
    async def test_a_requirements_txt_that_is_a_directory_keeps_the_refusal(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """A directory named ``requirements.txt`` beside a consumer: present, so the
        build's detection sees a build file, and nothing the provisioner can read
        (``requirements_in_tree`` is ``None`` for a non-file) -- refused in both
        passes, never waived as "the runtime will install it"."""
        (tmp_path / "requirements.txt").mkdir()
        (tmp_path / "requirements.txt" / "prod.txt").write_text("fastapi\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result["ok"] is False
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []
        assert (
            registry._desktop_build_refusal(
                tmp_path, _asgi_backend(), self_managed=False, final=True
            )
            == registry._DESKTOP_BUILD_REFUSAL
        )

    @pytest.mark.asyncio
    async def test_an_entry_point_that_is_a_directory_keeps_the_refusal_in_both_passes(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """The build pass defers only ABSENCE -- the ``onInstall`` window is where a
        file may appear. A directory standing at the entry path is not absent, and
        no script window turns it into the regular file the spawn requires, so
        the refusal stands in the build pass as in the final one."""
        (tmp_path / "server.py").unlink()  # the fixture's regular file; here a directory
        (tmp_path / "server.py").mkdir()
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result["ok"] is False
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []
        assert (
            registry._desktop_build_refusal(
                tmp_path, _asgi_backend(), self_managed=False, final=True
            )
            == registry._DESKTOP_BUILD_REFUSAL
        )

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_an_entry_point_link_escaping_the_app_root_keeps_the_refusal(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """An entry that resolves outside the app root is one the spawn refuses
        ("escapes app root") before it provisions anything, on every host -- so the
        backend provisioner never runs for it, and no script window changes where
        the link points. Refused in both passes."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "server.py").write_text("", encoding="utf-8")
        app_root = tmp_path / "app"
        app_root.mkdir()
        (app_root / "server.py").symlink_to(outside / "server.py")
        (app_root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            app_root, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result["ok"] is False
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []
        assert (
            registry._desktop_build_refusal(
                app_root, _asgi_backend(), self_managed=False, final=True
            )
            == registry._DESKTOP_BUILD_REFUSAL
        )

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_a_dangling_entry_point_link_defers_to_the_final_pass(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """The one non-absent shape the build pass still defers: a link whose
        target is not there YET (``server.py -> generated/server.py``, the script's
        to produce). Like a dangling requirements link, it passes the build pass
        and is refused by the final one if the target never appeared."""
        (tmp_path / "server.py").unlink()  # the fixture's regular file; here a dangling link
        (tmp_path / "server.py").symlink_to(Path("generated") / "server.py")
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result == {"ok": True}
        assert spawned == []
        assert (
            registry._desktop_build_refusal(
                tmp_path, _asgi_backend(), self_managed=False, final=True
            )
            == registry._DESKTOP_BUILD_REFUSAL
        )

    @requires_symlinks
    @pytest.mark.asyncio
    @pytest.mark.parametrize("owned", ["data", ".app_secret"])
    async def test_a_requirements_link_to_a_root_entry_the_gateway_owns_keeps_the_refusal(
        self, tmp_path, monkeypatch, bundled_interpreter, owned
    ):
        """Three owners meet in the installed directory: the INSTALLER copies the
        source tree, the RUNTIME provisions its artifacts afterwards, and the
        GATEWAY writes a few root entries of its own -- ``data``, which an update
        replaces with the preserved previous directory (and a source FILE so
        named fails ``app_data_dir`` before an install completes), and
        ``.app_secret``, written after the copy and preserved over it. A
        ``requirements.txt`` linked at one of those resolves to a real file HERE
        and to the gateway's own entry, or to nothing, in the app directory, where
        the provisioner reads it -- so the copy rule answers "not carried" and the
        gate refuses instead of waiving an install whose provisioning then fails.
        ``data`` is the gateway's once a preserved directory awaits (an update);
        ``.app_secret`` on every install."""
        monkeypatch.setattr(registry, "preserved_data_awaits", lambda name: True)
        (tmp_path / owned).write_text("fastapi\n", encoding="utf-8")
        (tmp_path / "requirements.txt").symlink_to(Path(owned))
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result["ok"] is False
        assert result["code"] == registry.DESKTOP_BUILD_STEP_UNSUPPORTED
        assert spawned == []
        assert (
            registry._desktop_build_refusal(
                tmp_path, _asgi_backend(), self_managed=False, final=True
            )
            == registry._DESKTOP_BUILD_REFUSAL
        )

    @pytest.mark.asyncio
    async def test_runtime_provisioned_artifacts_are_neither_required_nor_examined(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """The runtime's own leaves -- the provisioned deps tree, the install
        metadata -- are provisioned AFTER the install and never copied by it, so
        the gate neither requires them (their absence is the normal state of a
        fresh checkout) nor judges them when a stale one is present: the waiver
        is about the installer-copied ``requirements.txt`` and the runtime's
        promise to read it, nothing else. Both shapes pass in both passes."""
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result == {"ok": True}
        # A stale provisioned tree and a source-shipped metadata file: dropped by
        # the copy, replaced by the runtime, and not the gate's to refuse over.
        (tmp_path / ".kirocrew-deps").mkdir()
        (tmp_path / ".kirocrew-deps" / "fastapi.dist-info").mkdir()
        (tmp_path / "installed.json").write_text("{}", encoding="utf-8")
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result == {"ok": True}
        assert spawned == []
        assert (
            registry._desktop_build_refusal(tmp_path, _asgi_backend(), self_managed=False, final=True)
            == ""
        )

    @pytest.mark.asyncio
    async def test_a_stdio_server_still_waives_beside_an_entry_the_spawn_would_refuse(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """bridges.py provisions at registration for a stdio server whenever the
        entry point is absent or file-style, with no existence requirement of its
        own -- the server is the consumer it provisions for -- so the file is
        installed and read even though the backend spawn would refuse."""
        (tmp_path / "server.py").unlink()
        (tmp_path / "requirements.txt").write_text("mcp\n", encoding="utf-8")
        manifest = AppManifest.from_dict(
            {
                "name": "demo",
                "backend": {"entryPoint": "server.py", "type": "asgi"},
                "mcpServers": {"tool": {"command": "python3", "args": ["srv.py"]}},
            }
        )
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=manifest, self_managed=False
        )
        assert result == {"ok": True}
        assert spawned == []

    @pytest.mark.asyncio
    async def test_a_dotted_entry_point_that_names_a_file_is_file_style(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """The shape test is the spawn's own: a file with the literal dotted name
        under the app root makes the entry a FILE, which the spawn provisions."""
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        (tmp_path / "server.main").write_text("", encoding="utf-8")
        log: list[str] = []
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", log, manifest=_asgi_backend("server.main"), self_managed=False
        )
        assert result == {"ok": True}
        assert spawned == []

    @pytest.mark.asyncio
    async def test_the_refusal_carries_its_machine_code(
        self, tmp_path, monkeypatch, bundled_interpreter
    ):
        """Every desktop refusal names its condition by code, beside the sentence.

        The dashboard's consent modal keys its plain-language copy on the code
        (the condition is permanent for this gateway, so the retry instruction is
        dropped), and a client is meant to act on a code rather than regex-match
        prose. Pinned on both refusal layouts so neither can lose it.
        """
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        no_consumer = await registry._run_app_build(
            tmp_path, "demo", [], manifest=AppManifest.from_dict({"name": "demo"}), self_managed=False
        )
        assert no_consumer["code"] == "desktop_build_step_unsupported"
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        gateway_import = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert gateway_import["code"] == "desktop_build_step_unsupported"
        assert gateway_import["error"] == no_consumer["error"]

    @pytest.mark.asyncio
    async def test_a_source_install_still_pip_installs_the_requirements(
        self, tmp_path, monkeypatch, pip_importable
    ):
        """The gate change is scoped to the BUNDLED interpreter: an ordinary
        source install keeps installing requirements.txt at install time, so a
        hooks-only app on a normal host is unaffected."""
        (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(
            tmp_path, "demo", [], manifest=_asgi_backend(), self_managed=False
        )
        assert result == {"ok": True}
        assert spawned == [[sys.executable, "-s", "-m", "pip", "install", "-r", "requirements.txt"]]

    @pytest.mark.asyncio
    async def test_build_output_is_streamed_into_the_log(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(registry.shutil, "which", lambda name: "/usr/bin/npm")
        _fake_sandbox(
            monkeypatch,
            [_FakeProc(returncode=0, stdout_lines=[b"added 1 package\n", b"done\n"])],
        )
        log: list[str] = []
        await registry._run_app_build(tmp_path, "demo", log, manifest=AppManifest.from_dict({}), self_managed=False)
        assert "added 1 package" in log and "done" in log

    @pytest.mark.asyncio
    async def test_nonzero_exit_fails_the_build(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(registry.shutil, "which", lambda name: "/usr/bin/npm")
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=7)])
        result = await registry._run_app_build(tmp_path, "demo", [], manifest=AppManifest.from_dict({}), self_managed=False)
        assert result["ok"] is False
        assert result["name"] == "demo"
        assert "build failed (exit 7)" in result["error"]

    @pytest.mark.asyncio
    async def test_timeout_kills_the_group_and_fails(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(registry.shutil, "which", lambda name: "/usr/bin/npm")
        monkeypatch.setattr(registry, "_BUILD_TIMEOUT", 0.05)

        class _Hang(_FakeProc):
            async def wait(self) -> int:
                await asyncio.sleep(5)
                return 0

        _fake_sandbox(monkeypatch, [_Hang(returncode=0)])
        killed: list[int] = []

        async def _kill(proc):
            killed.append(proc.pid)

        monkeypatch.setattr(registry, "_kill_process_group", _kill)
        result = await registry._run_app_build(tmp_path, "demo", [], manifest=AppManifest.from_dict({}), self_managed=False)
        assert result["ok"] is False
        assert "build timed out" in result["error"]
        assert killed == [31337]
