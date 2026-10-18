"""App registry install pipeline: the post-script desktop gates and layout cleanup.

The desktop requirements waiver is judged again on the final manifest, and every
post-script refusal first removes or sets aside, with link and junction containment, the
layout files the install script created. The module-wide autouse fixtures come from
``apps_registry_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from apps_registry_helpers import _catalog_absent  # noqa: F401 - fixture
from apps_registry_helpers import _explicit_registry_execution_admission  # noqa: F401 - fixture
from apps_registry_helpers import unsandboxed_spawn  # noqa: F401 - fixture
from apps_registry_helpers import _identity_harness

from conftest import requires_symlinks
from kiro_crew.apps import registry
from kiro_crew.apps.manifest import AppManifest

#: The install's refusal for a root `data` the data directory cannot stand beside,
#: as `manager.gateway_data_dir_obstruction` spells it -- the transaction must carry it
#: unchanged, so it is pinned here as text.
_DATA_IS_A_FILE = (
    "`data` in the app tree is a file; Kiro Crew creates the app's data directory at "
    "that path and cannot install beside it."
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "script_effect",
    ["adds_hooks", "drops_the_entry_point"],
    ids=["onInstall adds backend.hooks", "onInstall drops the entry point"],
)
async def test_onInstall_rewriting_the_manifest_loses_the_desktop_requirements_waiver(
    monkeypatch, tmp_path, script_effect
):
    """The bundled-interpreter waiver is re-derived from the FINAL manifest.

    The build judges a requirements-only app on the manifest that entered
    ``setup.onInstall``; the script then runs with write access to the checkout.
    One that adds ``backend.hooks`` turns the app into one whose Python imports
    INTO the gateway (which the runtime-provisioned deps tree never reaches), and
    one that drops the entry point leaves nothing that would ever provision the
    file. Either way the waiver's premise is gone, so the install must fail with
    the gate's own sentence, roll the pre-existing checkout back exactly like a
    post-script admission denial, and register nothing.
    """
    src = tmp_path / "app-sources" / "demoapp"
    src.mkdir(parents=True)
    (src / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (src / "server.py").write_text("", encoding="utf-8")
    entering = {
        "name": "demoapp",
        "backend": {"entryPoint": "server.py", "type": "asgi"},
        "setup": {"onInstall": "true"},
    }
    (src / "app.json").write_text(json.dumps(entering), encoding="utf-8")
    monkeypatch.setattr(registry.platform_compat, "is_bundled_interpreter", lambda: True)
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(
        registry,
        "get_registry_app",
        lambda n: {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"},
    )
    monkeypatch.setattr(
        registry,
        "_fetch_app_manifest",
        AsyncMock(return_value={"name": "demoapp", "version": "1.0.0"}),
    )
    registered: list[str] = []

    def _register(source, **kwargs):
        # Reached only if the final gate let the install through; the transaction
        # reports this sentinel as the outcome's error, which the assertions below
        # then name instead of the refusal.
        registered.append(str(source))
        raise RuntimeError("registration reached")

    monkeypatch.setattr(registry, "install_app", _register)
    monkeypatch.setattr(registry, "update_app", _register)

    async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
        # The build already granted the waiver on `entering` (an out-of-process
        # entry point, no hooks); what it returns is the checkout the script gets.
        return {
            "ok": True,
            "pkg_dir": src,
            "_checkout_preexisted": True,
            "_pre_pull_commit": "b" * 40,
        }

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)

    spawned: list[list[str]] = []

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append(list(argv))
        if argv[0] == "/bin/bash":
            # What the script does with its write access: rewrite app.json.
            rewritten = dict(entering)
            if script_effect == "adds_hooks":
                rewritten["backend"] = {
                    **entering["backend"],
                    "hooks": {"on_startup": "backend.hooks:start"},
                }
            else:
                rewritten["backend"] = {"type": "asgi"}
            (src / "app.json").write_text(json.dumps(rewritten), encoding="utf-8")
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(
        registry.platform_compat, "kill_process_tree_async", AsyncMock(), raising=False
    )
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: None, raising=False)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert result["error"] == (
        "Python apps that require a build step are not supported in Kiro Crew's desktop "
        "build: its bundled interpreter is inside the signed application bundle and cannot "
        "install packages"
    )
    assert result["code"] == "desktop_build_step_unsupported"
    # Nothing registered, nothing enabled: the refusal happens before install_app.
    assert registered == []
    # The pre-existing checkout is rolled back the way the post-script admission
    # denial rolls it back: reset to the pre-pull commit and the manifest restored.
    assert any(cmd[:4] == ["git", "reset", "--keep", "b" * 40] for cmd in spawned)
    assert any(cmd[:4] == ["git", "--literal-pathspecs", "checkout", "--"] for cmd in spawned)
    assert src.exists()


@pytest.mark.asyncio
async def test_a_script_that_leaves_the_waiver_intact_still_installs(monkeypatch, tmp_path):
    """The final desktop pass repeats the build's verdict on unchanged inputs: a
    script that touches nothing the waiver depends on does not cost the install."""
    src = tmp_path / "app-sources" / "demoapp"
    src.mkdir(parents=True)
    (src / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (src / "server.py").write_text("", encoding="utf-8")
    (src / "app.json").write_text(
        json.dumps(
            {
                "name": "demoapp",
                "backend": {"entryPoint": "server.py", "type": "asgi"},
                "setup": {"onInstall": "true"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(registry.platform_compat, "is_bundled_interpreter", lambda: True)
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(
        registry,
        "get_registry_app",
        lambda n: {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"},
    )
    monkeypatch.setattr(
        registry,
        "_fetch_app_manifest",
        AsyncMock(return_value={"name": "demoapp", "version": "1.0.0"}),
    )

    async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
        return {"ok": True, "pkg_dir": src, "_checkout_preexisted": False, "_pre_pull_commit": ""}

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(
        registry.platform_compat, "kill_process_tree_async", AsyncMock(), raising=False
    )
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: None, raising=False)
    # The final pass is the LAST gate before registration; stop there and read
    # whether it let the install through.
    reached: list[str] = []

    def _install_app(source, **kwargs):
        reached.append(str(source))
        raise RuntimeError("stop at registration")

    monkeypatch.setattr(registry, "install_app", _install_app)

    outcome = await registry.install_from_registry("demoapp")
    # The transaction reports the sentinel as the outcome's error: the flow got
    # past every gate, the final desktop pass included, and into registration.
    assert reached == [str(src)]
    assert outcome["error"] == "stop at registration"
    assert "code" not in outcome


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "script_creates_entry",
    [True, False],
    ids=["onInstall generates server.py", "server.py never appears"],
)
async def test_an_entry_file_the_script_generates_is_judged_at_the_final_pass(
    monkeypatch, tmp_path, script_creates_entry
):
    """The build pass runs before ``setup.onInstall``, whose window is where an app
    may generate its entry file, so a declared ``server.py`` that is absent at
    build time is let through -- and the final pass, on the post-script checkout,
    is the one that decides: it reaches registration when the script created the
    file, and refuses with the gate's own code when nothing did (the spawn would
    return "entry point not found" before provisioning, so the backend
    provisioner never runs for that app).
    """
    src = tmp_path / "app-sources" / "demoapp"
    src.mkdir(parents=True)
    (src / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (src / "app.json").write_text(
        json.dumps(
            {
                "name": "demoapp",
                "backend": {"entryPoint": "server.py", "type": "asgi"},
                "setup": {"onInstall": "true"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(registry.platform_compat, "is_bundled_interpreter", lambda: True)
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(
        registry,
        "get_registry_app",
        lambda n: {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"},
    )
    monkeypatch.setattr(
        registry,
        "_fetch_app_manifest",
        AsyncMock(return_value={"name": "demoapp", "version": "1.0.0"}),
    )
    build_verdicts: list[str] = []

    async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
        # The REAL build-pass verdict, on the checkout as it is before the script.
        build_verdicts.append(
            registry._desktop_build_refusal(
                src,
                AppManifest.from_dict(json.loads((src / "app.json").read_text("utf-8"))),
                self_managed=False,
                final=False,
            )
        )
        return {"ok": True, "pkg_dir": src, "_checkout_preexisted": False, "_pre_pull_commit": ""}

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        if argv[0] == "/bin/bash" and script_creates_entry:
            (src / "server.py").write_text("", encoding="utf-8")
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(
        registry.platform_compat, "kill_process_tree_async", AsyncMock(), raising=False
    )
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: None, raising=False)
    reached: list[str] = []

    def _install_app(source, **kwargs):
        reached.append(str(source))
        raise RuntimeError("stop at registration")

    monkeypatch.setattr(registry, "install_app", _install_app)

    outcome = await registry.install_from_registry("demoapp")

    # The build pass let the absent entry through in both cases.
    assert build_verdicts == [""]
    if script_creates_entry:
        assert reached == [str(src)]
        assert outcome["error"] == "stop at registration"
    else:
        assert reached == []
        assert outcome["code"] == "desktop_build_step_unsupported"


@pytest.mark.asyncio
async def test_a_self_managed_entry_keeps_the_desktop_refusal(monkeypatch, tmp_path):
    """The waiver is for apps the runtime provisions, and it never provisions a
    self-managed one.

    A registry entry with ``resources: "app"`` takes the metadata-only branch:
    no source is copied into the app directory, the bridges skip every
    registration for it, and the app launches itself -- so the file-style entry
    point that waives a gateway-managed app's requirements.txt names a consumer
    the runtime will never spawn. Ownership is a registry fact, not a manifest
    one: it reaches the build verdict as its own input and the final pass judges
    it again, so the install fails with the gate's own sentence and
    ``register_external_app`` is never reached.
    """
    src = tmp_path / "app-sources" / "demoapp"
    src.mkdir(parents=True)
    (src / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (src / "server.py").write_text("", encoding="utf-8")
    (src / "app.json").write_text(
        json.dumps({"name": "demoapp", "backend": {"entryPoint": "server.py", "type": "asgi"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(registry.platform_compat, "is_bundled_interpreter", lambda: True)
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(
        registry,
        "get_registry_app",
        lambda n: {
            "name": "demoapp",
            "repo": "https://example.com/demo.git",
            "branch": "main",
            "resources": "app",
        },
    )
    monkeypatch.setattr(
        registry,
        "_fetch_app_manifest",
        AsyncMock(return_value={"name": "demoapp", "version": "1.0.0"}),
    )
    build_kwargs: list[dict] = []

    async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
        # Records what the build verdict was told about ownership, and returns
        # the checkout as if the build had waived: the final pass must still
        # refuse from the same inputs.
        build_kwargs.append(kwargs)
        return {"ok": True, "pkg_dir": src, "_checkout_preexisted": False, "_pre_pull_commit": ""}

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)
    registered: list[str] = []

    def _register(**kwargs):
        registered.append(kwargs["name"])
        return MagicMock(ok=True, notice="")

    # Imported inside the self-managed branch, so patched at its home.
    monkeypatch.setattr("kiro_crew.apps.manager.register_external_app", _register)
    monkeypatch.setattr(registry, "set_app_provenance", lambda *a, **k: None)
    monkeypatch.setattr(registry.install_receipt, "dispatch_async", AsyncMock())
    monkeypatch.setattr(registry, "install_app", lambda *a, **k: pytest.fail("managed path"))

    result = await registry.install_from_registry("demoapp")

    assert build_kwargs and build_kwargs[0]["self_managed"] is True
    assert result["ok"] is False
    assert result["error"] == (
        "Python apps that require a build step are not supported in Kiro Crew's desktop "
        "build: its bundled interpreter is inside the signed application bundle and cannot "
        "install packages"
    )
    assert result["code"] == "desktop_build_step_unsupported"
    assert registered == []


@pytest.mark.asyncio
async def test_a_final_desktop_refusal_removes_the_layout_files_that_appeared(
    monkeypatch, tmp_path
):
    """The rollback leaves the build gate's inputs as they were before the script,
    by removing what the script created.

    ``setup.onInstall`` runs with write access to a PRE-EXISTING checkout. A
    ``pyproject.toml`` it creates fails the final desktop pass -- and it is
    untracked, so the post-refusal ``git reset --keep`` leaves it in place, where
    every retry would refuse at the BUILD gate before the script a fixed remote
    corrected could run again. Every layout input that appeared in the window
    (``setup.py`` too, written here by the script's stand-in) is set aside in ONE
    ``.stale-*`` sibling of the checkout -- the name the retention sweep already
    retires -- and the install log names each one and where it went. The checkout
    is left as it was before the script, and the files that predate the script --
    the user's own ``NOTES.md`` included -- stay where they are.
    """
    sources = tmp_path / "app-sources"
    src = sources / "demoapp"
    src.mkdir(parents=True)
    (src / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (src / "server.py").write_text("", encoding="utf-8")
    (src / "NOTES.md").write_text("mine\n", encoding="utf-8")
    entering = {
        "name": "demoapp",
        "backend": {"entryPoint": "server.py", "type": "asgi"},
        "setup": {"onInstall": "true"},
    }
    (src / "app.json").write_text(json.dumps(entering), encoding="utf-8")
    monkeypatch.setattr(registry.platform_compat, "is_bundled_interpreter", lambda: True)
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(
        registry,
        "get_registry_app",
        lambda n: {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"},
    )
    monkeypatch.setattr(
        registry,
        "_fetch_app_manifest",
        AsyncMock(return_value={"name": "demoapp", "version": "1.0.0"}),
    )
    monkeypatch.setattr(registry, "install_app", lambda *a, **k: pytest.fail("registered"))

    async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
        return {
            "ok": True,
            "pkg_dir": src,
            "_checkout_preexisted": True,
            "_pre_pull_commit": "b" * 40,
        }

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        if argv[0] == "/bin/bash":
            # The script: a build file that installs INTO the interpreter, which
            # the final pass refuses, and a second layout input generated beside it.
            (src / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            (src / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")
            # Neither a layout input: a RUNTIME artifact (the provisioned deps tree)
            # and a GATEWAY-owned root name. The removal touches the build gate's
            # four inputs and nothing else, so both stay put.
            (src / ".kirocrew-deps").mkdir()
            (src / ".kirocrew-deps" / "marker").write_text("", encoding="utf-8")
            (src / ".app_secret").write_text("not-the-gateway's\n", encoding="utf-8")
        # `git reset --keep` is recorded, not run: it restores tracked files only,
        # and both new files are untracked either way.
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(
        registry.platform_compat, "kill_process_tree_async", AsyncMock(), raising=False
    )
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: None, raising=False)

    # The streaming install hands `install_from_registry` a StreamingLogLines,
    # whose append feeds a loop-owned asyncio.Queue: it must only ever be called
    # on the event-loop thread, so the removal step (a worker-thread unlink)
    # has to hand its lines back instead of appending them itself.
    loop_thread = threading.get_ident()

    class _ThreadCheckedLog(registry.StreamingLogLines):
        off_loop: list[str] = []

        def append(self, line: str) -> None:
            if threading.get_ident() != loop_thread:
                self.off_loop.append(line)
            super().append(line)

    log_lines = _ThreadCheckedLog(asyncio.Queue())

    result = await registry.install_from_registry("demoapp", log_lines=log_lines)

    assert result["code"] == "desktop_build_step_unsupported"
    assert _ThreadCheckedLog.off_loop == []
    # The checkout is as it was before the script: neither new file wedges the
    # build gate on the next attempt, and what predates the script is untouched.
    assert not (src / "pyproject.toml").exists()
    assert not (src / "setup.py").exists()
    assert (src / "requirements.txt").is_file()
    assert (src / "server.py").is_file()
    assert (src / "NOTES.md").read_text(encoding="utf-8") == "mine\n"
    # The runtime's and the gateway's leaves are not the removal's to touch: they
    # are exactly where the script left them.
    assert (src / ".kirocrew-deps" / "marker").is_file()
    assert (src / ".app_secret").read_text(encoding="utf-8") == "not-the-gateway's\n"
    # Set aside, not removed: exactly one `.stale-*` sibling appears under
    # app-sources, holding exactly the two files that appeared, and the log names
    # each file and where it went. The sweep's own pattern recognises the sibling.
    siblings = sorted(p.name for p in sources.iterdir())
    assert len(siblings) == 2 and siblings[0] == "demoapp", siblings
    aside = sources / siblings[1]
    assert registry._is_stale_candidate(aside), aside.name
    assert sorted(p.name for p in aside.iterdir()) == ["pyproject.toml", "setup.py"]
    assert (aside / "pyproject.toml").read_text(encoding="utf-8") == "[project]\n"
    log = result["log"]
    assert (
        f"Set aside pyproject.toml, which appeared during the install script, at "
        f"{aside.name}/pyproject.toml; the retention sweep removes it after 7 days"
    ) in log
    assert (
        f"Set aside setup.py, which appeared during the install script, at {aside.name}/setup.py"
        in log
    )
    assert "Removed " not in log
    # ... and the existing sweep retires the sibling on its normal schedule --
    # kept inside the retention window, removed once older than it -- while the
    # checkout itself is never a candidate.
    now = aside.stat().st_mtime
    assert registry._sweep_stale_checkouts_sync(sources, now + 3600) == []
    aged = now + (registry._STALE_CHECKOUT_RETENTION_DAYS * 86400) + 3600
    assert registry._sweep_stale_checkouts_sync(sources, aged) == [aside.name]
    assert not aside.exists()
    assert [p.name for p in sources.iterdir()] == ["demoapp"]


@pytest.mark.asyncio
@pytest.mark.parametrize("refused_by", ["identity", "admission"])
async def test_every_post_script_refusal_removes_the_layout_files_that_appeared(
    monkeypatch, tmp_path, refused_by
):
    """The harm is the script's, not the gate's. ``setup.onInstall`` runs with
    write access before THREE gates re-judge the checkout -- identity, admission,
    the desktop pass -- and a ``setup.py`` it created is untracked, so whichever
    gate refuses, the rollback's ``git reset --keep`` leaves it to wedge every
    retry at the build gate. The identity re-check (the script renamed the
    manifest) and the admission re-check (the script rewrote what the policy
    judges) take the same rollback the desktop refusal takes: the appeared layout
    file removed first and named in the log, the checkout rolled back after, no
    desktop code, and the files that predate the script untouched."""
    sources = tmp_path / "app-sources"
    src = sources / "demoapp"
    src.mkdir(parents=True)
    (src / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (src / "server.py").write_text("", encoding="utf-8")
    (src / "NOTES.md").write_text("mine\n", encoding="utf-8")
    entering = {
        "name": "demoapp",
        "description": "as entered",
        "backend": {"entryPoint": "server.py", "type": "asgi"},
        "setup": {"onInstall": "true"},
    }
    (src / "app.json").write_text(json.dumps(entering), encoding="utf-8")
    monkeypatch.setattr(registry.platform_compat, "is_bundled_interpreter", lambda: True)
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())

    def _deny_what_the_script_wrote(name, *, manifest=None, action=""):
        # The post-script re-check reads the manifest the script rewrote; every
        # earlier check reads the entering one and admits it.
        if manifest is not None and manifest.description == "rewritten by the script":
            return "policy refuses the rewritten manifest"
        return None

    monkeypatch.setattr(registry, "app_admission_denied", _deny_what_the_script_wrote)
    monkeypatch.setattr(
        registry,
        "get_registry_app",
        lambda n: {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"},
    )
    monkeypatch.setattr(
        registry,
        "_fetch_app_manifest",
        AsyncMock(return_value={"name": "demoapp", "version": "1.0.0"}),
    )
    monkeypatch.setattr(registry, "install_app", lambda *a, **k: pytest.fail("registered"))

    async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
        return {
            "ok": True,
            "pkg_dir": src,
            "_checkout_preexisted": True,
            "_pre_pull_commit": "b" * 40,
        }

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        if argv[0] == "/bin/bash":
            # The script: a layout input generated in the window, and the
            # manifest rewritten so the gate under test is the one that refuses.
            (src / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")
            rewritten = dict(entering)
            if refused_by == "identity":
                rewritten["name"] = "squatter"
            else:
                rewritten["description"] = "rewritten by the script"
            (src / "app.json").write_text(json.dumps(rewritten), encoding="utf-8")
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(
        registry.platform_compat, "kill_process_tree_async", AsyncMock(), raising=False
    )
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: None, raising=False)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert "code" not in result
    if refused_by == "identity":
        assert "declares 'squatter'" in result["error"], result["error"]
    else:
        assert (
            result["error"] == "blocked by admission policy: policy refuses the rewritten manifest"
        )
    assert not (src / "setup.py").exists()  # the script's layout file left the checkout
    assert (src / "requirements.txt").is_file()
    assert (src / "server.py").is_file()
    assert (src / "NOTES.md").read_text(encoding="utf-8") == "mine\n"
    # ... into one `.stale-*` sibling the sweep retires, named in the log.
    siblings = sorted(p.name for p in sources.iterdir())
    assert len(siblings) == 2 and siblings[0] == "demoapp", siblings
    assert registry._is_stale_candidate(sources / siblings[1])
    assert (sources / siblings[1] / "setup.py").read_text(
        encoding="utf-8"
    ) == "from setuptools import setup\n"
    assert (
        f"Set aside setup.py, which appeared during the install script, at {siblings[1]}/setup.py"
        in result["log"]
    )


@pytest.mark.asyncio
async def test_the_removal_runs_before_the_rollback_restores_tracked_files(monkeypatch, tmp_path):
    """Order matters: ``git reset --keep`` can RESTORE a tracked layout file the
    pull had removed -- absent in the pre-script snapshot, present after the
    reset. A scan run after the reset would mistake that restored file for one
    that appeared during the script and remove the freshly restored checkout's
    own file. So the scan and the removals run first, on exactly the window
    between snapshot and refusal, and the rollback runs after.

    Here the pre-pull checkout carried ``pyproject.toml`` (a source install's
    layout), the update removed it, the script created ``setup.py``, and the
    final pass refuses ``setup.py``. The rollback puts ``pyproject.toml`` back;
    it must still be there afterwards, with only ``setup.py`` gone.
    """
    sources = tmp_path / "app-sources"
    src = sources / "demoapp"
    src.mkdir(parents=True)
    (src / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (src / "server.py").write_text("", encoding="utf-8")
    entering = {
        "name": "demoapp",
        "backend": {"entryPoint": "server.py", "type": "asgi"},
        "setup": {"onInstall": "true"},
    }
    (src / "app.json").write_text(json.dumps(entering), encoding="utf-8")
    monkeypatch.setattr(registry.platform_compat, "is_bundled_interpreter", lambda: True)
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(
        registry,
        "get_registry_app",
        lambda n: {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"},
    )
    monkeypatch.setattr(
        registry,
        "_fetch_app_manifest",
        AsyncMock(return_value={"name": "demoapp", "version": "1.0.0"}),
    )
    monkeypatch.setattr(registry, "install_app", lambda *a, **k: pytest.fail("registered"))

    async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
        return {
            "ok": True,
            "pkg_dir": src,
            "_checkout_preexisted": True,
            "_pre_pull_commit": "b" * 40,
        }

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        if argv[0] == "/bin/bash":
            (src / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")
        if list(argv[:3]) == ["git", "reset", "--keep"]:
            # What the rollback does to TRACKED files: the pre-pull commit had a
            # pyproject.toml the pull removed, and the reset puts it back.
            (src / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(
        registry.platform_compat, "kill_process_tree_async", AsyncMock(), raising=False
    )
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: None, raising=False)

    result = await registry.install_from_registry("demoapp")

    assert result["code"] == "desktop_build_step_unsupported"
    # The restored tracked file is exactly where the rollback put it.
    assert (src / "pyproject.toml").read_text(encoding="utf-8") == "[project]\n"
    # Only what appeared during the script left the checkout -- set aside in the
    # one `.stale-*` sibling -- and the restored tracked file was not mistaken
    # for it.
    assert not (src / "setup.py").exists()
    siblings = sorted(p.name for p in sources.iterdir())
    assert len(siblings) == 2 and siblings[0] == "demoapp", siblings
    assert sorted(p.name for p in (sources / siblings[1]).iterdir()) == ["setup.py"]
    assert (
        f"Set aside setup.py, which appeared during the install script, at {siblings[1]}/setup.py"
        in result["log"]
    )


def _fresh_checkout_refused_by_the_final_pass(monkeypatch, tmp_path):
    """Shared fixture: a FRESH clone whose ``setup.onInstall`` creates a file the
    final desktop pass refuses. Returns (sources, src)."""
    sources = tmp_path / "app-sources"
    src = sources / "demoapp"
    src.mkdir(parents=True)
    (src / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (src / "server.py").write_text("", encoding="utf-8")
    (src / "app.json").write_text(
        json.dumps(
            {
                "name": "demoapp",
                "backend": {"entryPoint": "server.py", "type": "asgi"},
                "setup": {"onInstall": "true"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(registry.platform_compat, "is_bundled_interpreter", lambda: True)
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(
        registry,
        "get_registry_app",
        lambda n: {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"},
    )
    monkeypatch.setattr(
        registry,
        "_fetch_app_manifest",
        AsyncMock(return_value={"name": "demoapp", "version": "1.0.0"}),
    )
    monkeypatch.setattr(registry, "install_app", lambda *a, **k: pytest.fail("registered"))

    async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
        return {"ok": True, "pkg_dir": src, "_checkout_preexisted": False, "_pre_pull_commit": ""}

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        if argv[0] == "/bin/bash":
            # A layout input the script generates, which the final pass refuses.
            (src / "setup.py").write_text("# my work in progress\n", encoding="utf-8")
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(
        registry.platform_compat, "kill_process_tree_async", AsyncMock(), raising=False
    )
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: None, raising=False)
    return sources, src


@pytest.mark.asyncio
async def test_a_fresh_checkout_s_appeared_file_is_removed_before_the_clone_is_deleted(
    monkeypatch, tmp_path
):
    """A FRESH clone is deleted whole by the rollback; the layout file that
    appeared during the script is removed first, the same way as on a
    pre-existing checkout, so one path handles both and the log names it either
    way. Nothing appears beside the checkout."""
    sources, src = _fresh_checkout_refused_by_the_final_pass(monkeypatch, tmp_path)

    result = await registry.install_from_registry("demoapp")

    assert result["code"] == "desktop_build_step_unsupported"
    assert not src.exists()  # the fresh clone was rolled back (deleted) as before
    assert list(sources.iterdir()) == []
    assert "Removed setup.py, which appeared during the install script" in result["log"]


@pytest.mark.asyncio
async def test_a_root_data_file_is_refused_before_any_record_is_written(monkeypatch, tmp_path):
    """End to end through the transaction: a fresh registry checkout shipping a
    root FILE named `data` passes every other gate; without the preview's own
    question it reaches `install_app`, which writes installed.json and then raises
    creating the data directory -- a partial installed record. The gate's preview
    copy raises the install's own refusal: the transaction reports it as a refusal
    (no desktop code -- a browser install refuses the same tree), registration is
    never reached, no record exists, and the fresh clone is rolled back."""
    from kiro_crew.apps.manager import _read_installed

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    sources, src = _fresh_checkout_refused_by_the_final_pass(monkeypatch, tmp_path)
    (src / "data").write_text("a file where the gateway's data directory goes\n", encoding="utf-8")

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _quiet_spawn(*argv, **kwargs):
        return _Proc()  # the script leaves the layout alone: `data` is the only thing wrong

    monkeypatch.setattr(registry, "create_subprocess_limited", _quiet_spawn)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert result["error"] == _DATA_IS_A_FILE, result["error"]
    assert "code" not in result
    assert f"Refusing install: {result['error']}" in result["log"]
    assert _read_installed("demoapp") is None
    assert not (home / "apps" / "demoapp").exists()
    assert not src.exists()  # the fresh clone was rolled back
    assert list(sources.iterdir()) == []
    holders = home / "app-sources"  # where the preview holder is written
    assert not holders.exists() or list(holders.iterdir()) == []


@pytest.mark.asyncio
async def test_a_preview_copy_failure_after_the_script_rolls_the_fresh_checkout_back(
    monkeypatch, tmp_path
):
    """FIRST install: the script ran with write access, and what it left made the
    final pass's preview COPY fail (an unreadable leaf, a FIFO, a symlink loop --
    here the copy raises the `OSError` directly). That is not a verdict, so
    without its own handler it escapes the post-script rollback through the
    generic handler: the poisoned fresh clone stays in the live slot, and every
    retry fails at the build pass before any script runs again. The failure takes
    the same rollback every post-script gate takes -- a fresh clone is deleted
    whole, the slot is empty -- it is reported as the ordinary error it is,
    without the desktop code, and a clean re-clone installs."""
    from kiro_crew.apps.manager import _read_installed

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    sources, src = _fresh_checkout_refused_by_the_final_pass(monkeypatch, tmp_path)

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _quiet_spawn(*argv, **kwargs):
        return _Proc()  # the script leaves the layout alone: the poison is what the copy meets

    monkeypatch.setattr(registry, "create_subprocess_limited", _quiet_spawn)
    real_copy = registry.copy_app_tree_as_installed
    copies: list[str] = []

    def _first_copy_fails(source, dest, *, data_preserved):
        copies.append("attempt")
        if len(copies) == 1:
            raise PermissionError(13, "Permission denied", str(source / "server.py"))
        return real_copy(source, dest, data_preserved=data_preserved)

    monkeypatch.setattr(registry, "copy_app_tree_as_installed", _first_copy_fails)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert "code" not in result  # an ordinary error, not the desktop refusal
    assert result["error"].startswith(
        "could not preview the installed tree of demoapp for the desktop gate: "
    )
    assert "Permission denied" in result["error"]
    assert result["error"] in result["log"]
    assert not src.exists()  # the poisoned fresh clone left the live slot
    assert list(sources.iterdir()) == []  # deleted whole, nothing set aside beside it
    assert _read_installed("demoapp") is None
    holders = home / "app-sources"  # the preview holder is gone too
    assert not holders.exists() or list(holders.iterdir()) == []

    # A retry starts from an empty slot: the clean re-clone (stood in for here)
    # passes the final pass and reaches registration.
    def _registered(*args, **kwargs):
        raise RuntimeError("registered")

    monkeypatch.setattr(registry, "install_app", _registered)
    src.mkdir(parents=True)
    (src / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (src / "server.py").write_text("", encoding="utf-8")
    (src / "app.json").write_text(
        json.dumps(
            {
                "name": "demoapp",
                "backend": {"entryPoint": "server.py", "type": "asgi"},
                "setup": {"onInstall": "true"},
            }
        ),
        encoding="utf-8",
    )
    retry = await registry.install_from_registry("demoapp")
    assert retry["error"] == "registered", retry  # `install_app` stand-in: registration reached
    assert copies == ["attempt", "attempt"]


@requires_symlinks
@pytest.mark.asyncio
async def test_a_subdirectory_swapped_for_a_symlink_loop_still_rolls_the_fresh_checkout_back(
    monkeypatch, tmp_path
):
    """FIRST install of a monorepo entry: ``setup.onInstall`` replaces the declared
    ``subdirectory`` with a symlink LOOP (``rm -rf pkg && ln -s pkg pkg``). The
    first gate after the script -- the identity re-read of ``app.json`` -- meets
    the loop and refuses, which takes the post-script rollback (the preview copy
    would have met the same loop one gate later; both refusals make the same
    call). The rollback's containment re-check resolves the package directory
    at the write, and ``Path.resolve`` reports a loop as ``RuntimeError``, not
    ``OSError``: a catch of ``OSError`` alone lets it escape the rollback, so the
    un-poison step never runs and the poisoned fresh clone sits in the live slot
    for every retry. The re-check must return its skip line for a loop exactly as
    for an escape, so the rollback completes and the fresh clone is deleted whole."""
    from kiro_crew.apps.manager import _read_installed

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    sources, src = _fresh_checkout_refused_by_the_final_pass(monkeypatch, tmp_path)
    pkg = src / "pkg"
    pkg.mkdir()
    for name in ("requirements.txt", "server.py", "app.json"):
        (src / name).rename(pkg / name)
    monkeypatch.setattr(
        registry,
        "get_registry_app",
        lambda n: {
            "name": "demoapp",
            "repo": "https://example.com/demo.git",
            "branch": "main",
            "subdirectory": "pkg",
        },
    )

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _swap_for_a_loop(*argv, **kwargs):
        if argv[0] == "/bin/bash":
            shutil.rmtree(pkg)
            os.symlink("pkg", pkg)  # the subdirectory now points at itself
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _swap_for_a_loop)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert "whose app.json declares '<missing>'" in result["error"]  # unreadable through the loop
    assert not src.exists()  # the poisoned fresh clone left the live slot
    assert list(sources.iterdir()) == []  # deleted whole, nothing set aside beside it
    assert _read_installed("demoapp") is None
    skip_lines = [
        line for line in result["log"].splitlines() if "skipping the layout cleanup" in line
    ]
    assert len(skip_lines) == 1 and "Symlink loop" in skip_lines[0], result["log"]


@pytest.mark.asyncio
async def test_the_streamed_refusal_names_the_update_when_the_app_is_installed(
    monkeypatch, tmp_path
):
    """The page titles its log panel "Update log" for an app with an installed
    record and "Install log" otherwise; the refusal line the server streams into
    that panel reads the same record, so the panel and the line never disagree.
    The fresh-install fixture above, plus the installed record the page's
    `app.installed` is read from: the final desktop pass refuses as before, and
    the streamed line says which action it refused."""
    from kiro_crew.apps.manager import InstalledApp, _write_installed, app_dir

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    sources, src = _fresh_checkout_refused_by_the_final_pass(monkeypatch, tmp_path)
    monkeypatch.setattr(registry, "update_app", lambda *a, **k: pytest.fail("registered"))
    app_dir("demoapp").mkdir(parents=True)
    _write_installed(
        "demoapp",
        InstalledApp(
            name="demoapp",
            version="0.9.0",
            displayName="Demo",
            installedAt="2026-09-01T10:00:00Z",
            source="registry:demoapp",
        ),
    )

    result = await registry.install_from_registry("demoapp")

    assert result["code"] == "desktop_build_step_unsupported"
    assert f"Refusing update: {result['error']}" in result["log"]
    assert "Refusing install" not in result["log"]


@pytest.mark.asyncio
@pytest.mark.parametrize("installed", [True, False])
async def test_the_refusal_verb_is_read_from_the_installed_record(monkeypatch, tmp_path, installed):
    """`install_from_registry` reads the verb once, from the installed record --
    the fact the copy step chooses `update_app` or `install_app` by -- and hands
    it to the clone/build step, whose gates stream the refusals before the copy."""
    src = tmp_path / "app-sources" / "demoapp"
    _identity_harness(monkeypatch, src, cloned_manifest={"name": "demoapp"})
    seen: dict[str, object] = {}

    async def _capture(git_url, name, log_lines, branch="main", **kwargs):
        seen.update(kwargs)
        return {"ok": False, "name": name, "error": "stopped at the build step"}

    monkeypatch.setattr(registry, "_clone_build_app", _capture)
    monkeypatch.setattr(registry, "get_app", lambda name: {"name": name} if installed else None)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert seen["verb"] == ("update" if installed else "install")


def test_a_failed_set_aside_is_reported_and_touches_nothing_else(monkeypatch, tmp_path):
    """Best-effort, like the rollback it precedes: a move that fails is named in the
    lines returned and the file stays, the other appeared file still goes -- into
    ONE `.stale-*` sibling of the checkout, created only because there was
    something to set aside -- a directory standing under a layout name is left
    and reported, and a file that predates the script is not touched."""
    sources = tmp_path / "app-sources"
    clone_root = sources / "demoapp"
    app_source = clone_root / "apps" / "sub" / "demo"
    app_source.mkdir(parents=True)
    (app_source / "setup.py").write_text("# appeared\n", encoding="utf-8")
    (app_source / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (app_source / "requirements.txt").mkdir()  # a directory under a layout name
    (app_source / "package.json").write_text("{}", encoding="utf-8")  # predates the script
    (app_source / "server.py").write_text("", encoding="utf-8")
    real_rename = registry.os.rename

    def _rename_refused(src, dst, *args, **kwargs):
        if os.path.basename(src) == "setup.py":
            raise PermissionError(13, "Operation not permitted", str(src))
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(registry.os, "rename", _rename_refused)

    messages = registry._set_aside_new_layout_files(
        app_source, frozenset({"package.json"}), clone_root=clone_root
    )

    assert (app_source / "setup.py").exists()
    assert not (app_source / "pyproject.toml").exists()
    assert (app_source / "requirements.txt").is_dir()
    assert (app_source / "package.json").is_file()
    assert (app_source / "server.py").is_file()
    siblings = sorted(p.name for p in sources.iterdir())
    assert len(siblings) == 2 and siblings[0] == "demoapp", siblings
    aside = sources / siblings[1]
    assert registry._is_stale_candidate(aside)
    assert [p.name for p in aside.iterdir()] == ["pyproject.toml"]
    assert (aside / "pyproject.toml").read_text(encoding="utf-8") == "[project]\n"
    assert any(m.startswith("WARNING: could not set aside setup.py") for m in messages)
    assert any(
        m.startswith("WARNING: could not set aside requirements.txt") and "is a directory" in m
        for m in messages
    )
    assert (
        f"Set aside pyproject.toml, which appeared during the install script, at "
        f"{aside.name}/pyproject.toml; the retention sweep removes it after 7 days"
    ) in messages


def test_nothing_to_set_aside_creates_no_sibling(tmp_path):
    """The sibling exists only when something went into it."""
    sources = tmp_path / "app-sources"
    clone_root = sources / "demoapp"
    clone_root.mkdir(parents=True)
    (clone_root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")

    messages = registry._set_aside_new_layout_files(
        clone_root, frozenset({"requirements.txt"}), clone_root=clone_root
    )

    assert messages == []
    assert [p.name for p in sources.iterdir()] == ["demoapp"]


def test_a_holder_that_cannot_be_created_is_retried_per_file_and_names_the_parents_error(
    tmp_path, monkeypatch
):
    """When the `.stale-*` holder cannot be created (the parent refuses the mkdir),
    every layout file's warning names THAT error, and each later file tries a
    holder of its own: binding the holder's path before its mkdir succeeded would
    leave the "no holder yet" test false for the rest of the loop, so every later
    file would skip the mkdir and report the rename's `No such file or directory`
    -- the missing holder -- instead of the unwritable parent that is the cause."""
    sources = tmp_path / "app-sources"
    clone_root = sources / "demoapp"
    clone_root.mkdir(parents=True)
    (clone_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (clone_root / "setup.py").write_text("# appeared\n", encoding="utf-8")
    real_mkdir = os.mkdir
    holder_attempts: list[str] = []

    def _parent_refuses_holders(path, *args, **kwargs):
        if ".stale-" in os.path.basename(path):
            holder_attempts.append(os.path.basename(path))
            raise PermissionError(13, "Permission denied", str(path))
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", _parent_refuses_holders)

    messages = registry._set_aside_new_layout_files(clone_root, frozenset(), clone_root=clone_root)

    # Nothing moved, no holder stands, and the checkout is intact.
    assert (clone_root / "pyproject.toml").read_text(encoding="utf-8") == "[project]\n"
    assert (clone_root / "setup.py").read_text(encoding="utf-8") == "# appeared\n"
    assert [p.name for p in sources.iterdir()] == ["demoapp"]
    # One holder attempt per file, each with its own name, and each warning
    # carries the parent's refusal -- never the rename's phantom ENOENT.
    assert len(holder_attempts) == 2 and len(set(holder_attempts)) == 2, holder_attempts
    assert [m.split(",")[0] for m in messages] == [
        "WARNING: could not set aside pyproject.toml",
        "WARNING: could not set aside setup.py",
    ]
    assert all("Permission denied" in m for m in messages), messages
    assert not any("No such file or directory" in m for m in messages), messages


def test_the_set_aside_sibling_is_retired_by_the_existing_sweep(tmp_path):
    """No new sweep and no new artifact class: the sibling carries the `.stale-*`
    name the retention sweep already retires -- kept inside the retention window,
    removed once older than it, on the sweep's normal schedule."""
    sources = tmp_path / "app-sources"
    clone_root = sources / "demoapp"
    clone_root.mkdir(parents=True)
    (clone_root / "setup.py").write_text("# appeared\n", encoding="utf-8")

    registry._set_aside_new_layout_files(clone_root, frozenset(), clone_root=clone_root)
    aside = next(p for p in sources.iterdir() if p.name != "demoapp")
    now = aside.stat().st_mtime

    assert registry._sweep_stale_checkouts_sync(sources, now + 3600) == []
    assert aside.is_dir()
    aged = now + (registry._STALE_CHECKOUT_RETENTION_DAYS * 86400) + 3600
    assert registry._sweep_stale_checkouts_sync(sources, aged) == [aside.name]
    assert not aside.exists()
    assert clone_root.is_dir()  # the checkout itself is never a sweep candidate


@requires_symlinks
@pytest.mark.parametrize("writer", ["remove", "set_aside"])
def test_a_planted_link_that_redirects_the_cleanup_touches_nothing_outside_the_checkout(
    tmp_path, writer
):
    """`setup.onInstall` ran with write access before the cleanup, so the package
    directory the caller computed (`clone_root / subdirectory`) cannot be trusted
    at the write: the script can replace the subdirectory with a link to a
    directory this process can write to (`rm -rf pkg && ln -s "$HOME" pkg`), and
    an `unlink`/`rename` of `<app_source>/setup.py` would reach an operator's
    file. Both writers re-verify containment at the write and skip, with the
    refusal logged, so nothing outside the checkout is touched."""
    sources = tmp_path / "app-sources"
    clone_root = sources / "demoapp"
    clone_root.mkdir(parents=True)
    operator_home = tmp_path / "operator-home"
    operator_home.mkdir()
    for name in registry._DESKTOP_LAYOUT_FILES:
        (operator_home / name).write_text(f"# the operator's own {name}\n", encoding="utf-8")
    (clone_root / "pkg").symlink_to(operator_home, target_is_directory=True)
    app_source = clone_root / "pkg"  # what the caller computed before the script ran
    assert os.path.lexists(app_source / "setup.py")  # lexists follows the swapped component

    if writer == "remove":
        messages = registry._remove_new_layout_files(app_source, frozenset(), clone_root=clone_root)
    else:
        messages = registry._set_aside_new_layout_files(
            app_source, frozenset(), clone_root=clone_root
        )

    assert messages == [
        "WARNING: 'pkg' no longer resolves inside the checkout; skipping the layout "
        "cleanup to avoid writing through a symlink escape. A retry may keep refusing "
        "until the source is repaired."
    ]
    for name in registry._DESKTOP_LAYOUT_FILES:
        assert (operator_home / name).read_text(
            encoding="utf-8"
        ) == f"# the operator's own {name}\n"
    assert os.path.islink(clone_root / "pkg")
    assert sorted(p.name for p in sources.iterdir()) == ["demoapp"]  # no `.stale-*` sibling either


@requires_symlinks
@pytest.mark.parametrize("writer", ["remove", "set_aside"])
def test_a_checkout_root_swapped_for_a_link_stops_the_cleanup_too(tmp_path, writer):
    """The other redirection: the script replaces the checkout ROOT with a link
    (`cd .. && rm -rf demoapp && ln -s "$HOME" demoapp`). Resolving both ends
    would then agree while both point at the operator's directory, so the root
    itself is refused as a link before anything resolves."""
    sources = tmp_path / "app-sources"
    sources.mkdir()
    operator_home = tmp_path / "operator-home"
    (operator_home / "pkg").mkdir(parents=True)
    (operator_home / "pkg" / "setup.py").write_text("# the operator's own\n", encoding="utf-8")
    clone_root = sources / "demoapp"
    clone_root.symlink_to(operator_home, target_is_directory=True)
    app_source = clone_root / "pkg"

    if writer == "remove":
        messages = registry._remove_new_layout_files(app_source, frozenset(), clone_root=clone_root)
    else:
        messages = registry._set_aside_new_layout_files(
            app_source, frozenset(), clone_root=clone_root
        )

    assert len(messages) == 1 and messages[0].startswith(
        "WARNING: the checkout 'demoapp' is now a link;"
    )
    assert (operator_home / "pkg" / "setup.py").read_text(
        encoding="utf-8"
    ) == "# the operator's own\n"
    assert sorted(p.name for p in sources.iterdir()) == ["demoapp"]


@pytest.mark.parametrize("writer", ["remove", "set_aside"])
def test_a_checkout_root_swapped_for_a_junction_stops_the_cleanup_too(
    tmp_path, writer, monkeypatch
):
    """The Windows shape of the root swap: a directory JUNCTION at the checkout root
    is not a symlink, so `islink` alone would pass it, both ends would resolve
    THROUGH it and agree, and the cleanup would write into the operator's
    directory. Fed as a shape through the platform seam (no POSIX junction exists):
    the root is a real directory here that `is_link_or_junction` reports as a
    junction, and both writers must refuse it exactly as they refuse a symlink root."""
    sources = tmp_path / "app-sources"
    clone_root = sources / "demoapp"
    (clone_root / "pkg").mkdir(parents=True)
    (clone_root / "pkg" / "setup.py").write_text("# appeared\n", encoding="utf-8")
    monkeypatch.setattr(
        registry.platform_compat,
        "is_link_or_junction",
        lambda path: os.fspath(path) == str(clone_root),
    )
    app_source = clone_root / "pkg"

    if writer == "remove":
        messages = registry._remove_new_layout_files(app_source, frozenset(), clone_root=clone_root)
    else:
        messages = registry._set_aside_new_layout_files(
            app_source, frozenset(), clone_root=clone_root
        )

    assert len(messages) == 1 and messages[0].startswith(
        "WARNING: the checkout 'demoapp' is now a link;"
    )
    assert (clone_root / "pkg" / "setup.py").read_text(encoding="utf-8") == "# appeared\n"
    assert sorted(p.name for p in sources.iterdir()) == ["demoapp"]


def test_a_junction_under_a_layout_name_is_set_aside_as_a_link(tmp_path, monkeypatch):
    """A junction standing under a layout name is a link, not the directory
    `isdir` (which follows it) reports: it is moved into the sibling whole, the
    way a symlink is, instead of being left in place to refuse every retry."""
    sources = tmp_path / "app-sources"
    clone_root = sources / "demoapp"
    clone_root.mkdir(parents=True)
    (clone_root / "setup.py").mkdir()  # a real directory standing in for the junction's shape
    (clone_root / "pyproject.toml").mkdir()  # a real directory: left and reported, as before
    monkeypatch.setattr(
        registry.platform_compat,
        "is_link_or_junction",
        lambda path: os.fspath(path) == str(clone_root / "setup.py"),
    )

    messages = registry._set_aside_new_layout_files(clone_root, frozenset(), clone_root=clone_root)

    aside = next(p for p in sources.iterdir() if p.name != "demoapp")
    assert not (clone_root / "setup.py").exists() and (aside / "setup.py").is_dir()
    assert (clone_root / "pyproject.toml").is_dir() and not (aside / "pyproject.toml").exists()
    assert any(m.startswith("Set aside setup.py,") for m in messages), messages
    assert any(
        m.startswith("WARNING: could not set aside pyproject.toml,") and "it is a directory" in m
        for m in messages
    ), messages


def test_a_failed_removal_is_reported_and_touches_nothing_else(monkeypatch, tmp_path):
    """Best-effort, like the rollback it precedes: a removal that fails is named in
    the lines returned, the file stays, the other appeared file still goes, and a
    directory standing under a layout name is refused by `unlink` rather than
    removed."""
    app_source = tmp_path / "app-sources" / "demoapp" / "apps" / "sub" / "demo"
    app_source.mkdir(parents=True)
    (app_source / "setup.py").write_text("# appeared\n", encoding="utf-8")
    (app_source / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (app_source / "requirements.txt").mkdir()  # a directory under a layout name
    (app_source / "server.py").write_text("", encoding="utf-8")
    real_unlink = registry.os.unlink

    def _unlink_refused(path, *args, **kwargs):
        if os.path.basename(path) == "setup.py":
            raise PermissionError(13, "Operation not permitted", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(registry.os, "unlink", _unlink_refused)

    messages = registry._remove_new_layout_files(
        app_source, frozenset(), clone_root=tmp_path / "app-sources" / "demoapp"
    )

    assert (app_source / "setup.py").exists()
    assert not (app_source / "pyproject.toml").exists()
    assert (app_source / "requirements.txt").is_dir()
    assert (app_source / "server.py").is_file()
    assert any(m.startswith("WARNING: could not remove setup.py") for m in messages)
    assert any(m.startswith("WARNING: could not remove requirements.txt") for m in messages)
    assert "Removed pyproject.toml, which appeared during the install script" in messages
    assert [p.name for p in (tmp_path / "app-sources").iterdir()] == ["demoapp"]
