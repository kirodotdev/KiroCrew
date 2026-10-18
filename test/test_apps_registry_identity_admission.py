"""App registry install pipeline: the identity and admission gates.

Covers identity-refusal cleanup, provenance signer and commit freshness, the
retained-startup rechecks before a reinstall, pull integrity, and the checkout rollback
every identity or admission rejection takes. The module-wide autouse fixtures come from
``apps_registry_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from apps_registry_helpers import _catalog_absent  # noqa: F401 - fixture
from apps_registry_helpers import _explicit_registry_execution_admission  # noqa: F401 - fixture
from apps_registry_helpers import unsandboxed_spawn  # noqa: F401 - fixture
from apps_registry_helpers import _identity_harness

from kiro_crew import platform_compat
from kiro_crew.apps import registry


# --------------------------------------------------------------------------
# Identity-refusal cleanup + provenance-signer freshness
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_identity_refusal_preserves_preexisting_checkout(monkeypatch, tmp_path):
    """UPDATE path: a pull that brings a self-renaming manifest is refused
    WITHOUT deleting the installed app's pre-existing source workspace."""
    src = tmp_path / "app-sources" / "demoapp"
    (src / ".git").mkdir(parents=True)  # checkout pre-exists BEFORE the run
    (src / "keep.txt").write_text("user state", encoding="utf-8")
    _identity_harness(monkeypatch, src, cloned_manifest={"name": "renamed-app"})

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert "refusing" in result["error"].lower()
    # The workspace survived the refusal.
    assert (src / ".git").is_dir()
    assert (src / "keep.txt").read_text(encoding="utf-8") == "user state"


@pytest.mark.asyncio
async def test_identity_refusal_deletes_fresh_clone(monkeypatch, tmp_path):
    """FRESH install path: a clone created this run leaves no residue on refusal."""
    src = tmp_path / "app-sources" / "demoapp"  # does NOT exist before the run
    _identity_harness(monkeypatch, src, cloned_manifest={"name": "evil-app"})

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert "refusing" in result["error"].lower()
    assert not src.exists()


@pytest.mark.asyncio
async def test_provenance_signer_comes_from_cloned_manifest(monkeypatch, tmp_path):
    """The signer persisted as provenance is computed from the identity-checked
    CLONED manifest — never from the pre-clone prefetch, which can be stale
    (signed preview, unsigned pulled commit)."""
    src = tmp_path / "app-sources" / "demoapp"
    _identity_harness(
        monkeypatch,
        src,
        cloned_manifest={"name": "demoapp", "version": "9.9.9"},
        prefetched={"name": "demoapp", "version": "1.0.0"},
    )
    monkeypatch.setattr(registry, "_resolved_clone_commit", lambda root: "a" * 40)

    signer_calls: list[object] = []

    def _fake_signer(manifest):
        signer_calls.append(manifest)
        return "cloned-signer"

    monkeypatch.setattr(registry, "verified_signer", _fake_signer)

    provenance: dict[str, object] = {}

    def _fake_set_provenance(name, **kwargs):
        provenance.update(kwargs, name=name)

    monkeypatch.setattr(registry, "set_app_provenance", _fake_set_provenance)
    monkeypatch.setattr(registry, "get_app", lambda n: None)
    monkeypatch.setattr(
        registry,
        "install_app",
        lambda path, **kwargs: MagicMock(ok=True, name="demoapp", message="done", error=""),
    )

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is True
    # Exactly one signer computation, and it saw the CLONED manifest.
    assert len(signer_calls) == 1
    assert getattr(signer_calls[0], "version", "") == "9.9.9"
    assert provenance["signer"] == "cloned-signer"
    assert provenance["commit"] == "a" * 40


@pytest.mark.asyncio
async def test_install_persists_credential_free_source_provenance(monkeypatch, tmp_path):
    secret = "InstalledSourceSecret"
    raw_url = f"https://user:{secret}@example.com/o/demo.git"
    public_url = "https://example.com/o/demo.git"
    src = tmp_path / "app-sources" / "demoapp"
    _identity_harness(
        monkeypatch,
        src,
        cloned_manifest={"name": "demoapp", "version": "1.0.0"},
    )
    monkeypatch.setattr(registry, "_entry_git_url", lambda entry: raw_url)
    monkeypatch.setattr(registry, "_resolved_clone_commit", lambda root: "a" * 40)
    monkeypatch.setattr(registry, "verified_signer", lambda manifest: "")
    monkeypatch.setattr(registry, "get_app", lambda name: None)
    installed: dict[str, object] = {}
    provenance: dict[str, object] = {}

    def _install(path):
        # The manager callable contract remains one positional argument; the
        # safe server-resolved coordinate crosses the to_thread boundary in a
        # task-local context rather than in app-controlled manifest data.
        from kiro_crew.apps.manager import _effective_source_repository

        installed["source_repository"] = _effective_source_repository("")
        return MagicMock(ok=True, name="demoapp", message="done", error="")

    monkeypatch.setattr(registry, "install_app", _install)
    monkeypatch.setattr(
        registry,
        "set_app_provenance",
        lambda name, **kwargs: provenance.update(kwargs, name=name),
    )

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is True
    assert installed["source_repository"] == public_url
    assert provenance["url"] == public_url
    assert secret not in str({"installed": installed, "provenance": provenance})


@pytest.mark.asyncio
async def test_registry_fresh_reinstall_checks_retained_startup_before_clone(monkeypatch, tmp_path):
    """Missing metadata must not hide retained old-version startup ownership."""
    src = tmp_path / "app-sources" / "demoapp"
    _identity_harness(
        monkeypatch,
        src,
        cloned_manifest={"name": "demoapp", "version": "2.0.0"},
    )
    monkeypatch.setattr(registry, "get_app", lambda _name: None)

    from kiro_crew.apps import hooks_integration

    cleanup_calls: list[tuple[str, bool]] = []

    async def _cleanup(app_name: str, *, bounded: bool) -> bool:
        cleanup_calls.append((app_name, bounded))
        return False

    monkeypatch.setattr(hooks_integration, "stop_retained_startup_hooks", _cleanup)

    async def _must_not_clone(*args, **kwargs):
        raise AssertionError("fresh reinstall must not clone while old code runs")

    monkeypatch.setattr(registry, "_clone_build_app", _must_not_clone)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert result["code"] == "startup_hook_still_running"
    assert result["retryable"] is True
    assert cleanup_calls == [("demoapp", True)]


@pytest.mark.asyncio
async def test_registry_fresh_reinstall_rechecks_retained_startup_before_replacement(
    monkeypatch, tmp_path
):
    """A fresh-looking reinstall must recheck ownership after clone and build."""
    src = tmp_path / "app-sources" / "demoapp"
    _identity_harness(
        monkeypatch,
        src,
        cloned_manifest={"name": "demoapp", "version": "2.0.0"},
    )
    monkeypatch.setattr(registry, "get_app", lambda _name: None)

    from kiro_crew.apps import hooks_integration

    cleanup_calls: list[tuple[str, bool]] = []

    async def _cleanup(app_name: str, *, bounded: bool) -> bool:
        cleanup_calls.append((app_name, bounded))
        return len(cleanup_calls) == 1

    monkeypatch.setattr(hooks_integration, "stop_retained_startup_hooks", _cleanup)

    def _must_not_replace(*args, **kwargs):
        raise AssertionError("fresh reinstall must not replace files while old code runs")

    monkeypatch.setattr(registry, "install_app", _must_not_replace)
    monkeypatch.setattr(registry, "update_app", _must_not_replace)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert result["code"] == "startup_hook_still_running"
    assert result["retryable"] is True
    assert cleanup_calls == [("demoapp", True), ("demoapp", True)]


@pytest.mark.asyncio
async def test_registry_reinstall_rechecks_retained_startup_before_replacement(
    monkeypatch, tmp_path
):
    """A hook retained during clone/build must still block old-file replacement."""
    src = tmp_path / "app-sources" / "demoapp"
    _identity_harness(
        monkeypatch,
        src,
        cloned_manifest={"name": "demoapp", "version": "2.0.0"},
    )
    monkeypatch.setattr(registry, "get_app", lambda _name: {"name": "demoapp"})

    from kiro_crew.apps import hooks_integration

    cleanup_calls: list[tuple[str, bool]] = []

    async def _cleanup(app_name: str, *, bounded: bool) -> bool:
        cleanup_calls.append((app_name, bounded))
        # No task at admission time; one becomes retained while clone/build runs.
        return len(cleanup_calls) == 1

    monkeypatch.setattr(hooks_integration, "stop_retained_startup_hooks", _cleanup)

    def _must_not_update(*args, **kwargs):
        raise AssertionError("registry reinstall must not replace old files")

    monkeypatch.setattr(registry, "update_app", _must_not_update)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert result["code"] == "startup_hook_still_running"
    assert result["retryable"] is True
    assert cleanup_calls == [("demoapp", True), ("demoapp", True)]


@pytest.mark.asyncio
async def test_an_empty_streaming_log_is_the_list_the_install_writes_to(monkeypatch, tmp_path):
    """An empty ``StreamingLogLines`` is falsy but is still the caller's log: the
    install writes into it (and so into its queue) rather than into a fresh list."""
    src = tmp_path / "app-sources" / "demoapp"
    _identity_harness(monkeypatch, src, cloned_manifest={"name": "demoapp"})
    monkeypatch.setattr(registry, "get_app", lambda _name: None)

    from kiro_crew.apps import hooks_integration

    async def _still_running(app_name: str, *, bounded: bool) -> bool:
        return False

    monkeypatch.setattr(hooks_integration, "stop_retained_startup_hooks", _still_running)
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    log = registry.StreamingLogLines(queue)
    assert not log

    result = await registry.install_from_registry("demoapp", log)

    assert result["code"] == "startup_hook_still_running"
    assert list(log) == [result["error"]]
    assert queue.get_nowait() == result["error"]
    # A refusal before the clone returns its own dict: nothing re-stamps it.
    assert "log" not in result


@pytest.mark.asyncio
async def test_a_replacement_boundary_refusal_is_stamped_by_the_finally(monkeypatch, tmp_path):
    """The recheck after clone and build returns through the single ``finally``,
    which stamps the whole log and strips internal ``_`` keys."""
    src = tmp_path / "app-sources" / "demoapp"
    _identity_harness(monkeypatch, src, cloned_manifest={"name": "demoapp", "version": "2.0.0"})
    monkeypatch.setattr(registry, "get_app", lambda _name: None)
    monkeypatch.setattr(registry, "_resolved_clone_commit", lambda root: "a" * 40)
    monkeypatch.setattr(registry, "verified_signer", lambda manifest: "")

    from kiro_crew.apps import hooks_integration

    calls: list[str] = []

    async def _retained_after_the_build(app_name: str, *, bounded: bool) -> bool:
        calls.append(app_name)
        return len(calls) == 1

    monkeypatch.setattr(hooks_integration, "stop_retained_startup_hooks", _retained_after_the_build)
    log: list[str] = []

    result = await registry.install_from_registry("demoapp", log)

    assert calls == ["demoapp", "demoapp"]
    assert result["code"] == "startup_hook_still_running"
    assert result["log"] == "\n".join(log)
    assert log[-1] == result["error"]
    assert not [key for key in result if key.startswith("_")]


@pytest.mark.asyncio
async def test_identity_gate_runs_before_the_build(monkeypatch, tmp_path):
    """A mismatched repo must be refused BEFORE _run_app_build executes — build
    ecosystems run repo-authored lifecycle scripts (npm preinstall, setup.py),
    so a post-build gate would let rejected code execute anyway."""
    src = tmp_path / "app-sources" / "demoapp"
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())

    async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "app.json").write_text(json.dumps({"name": "evil-app"}), encoding="utf-8")
        return None

    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)

    build_calls: list[object] = []

    async def _fake_run_build(build_dir, app_name, log_lines):
        build_calls.append(build_dir)
        return {"ok": True}

    monkeypatch.setattr(registry, "_run_app_build", _fake_run_build)

    result = await registry._clone_build_app(
        "https://example.com/demo.git", "demoapp", [], entry_repo="example/demo"
    )

    assert result["ok"] is False
    assert "refusing" in result["error"].lower()
    assert build_calls == []  # the build never ran


@pytest.mark.asyncio
async def test_cloned_manifest_admission_is_revalidated_before_build(monkeypatch, tmp_path):
    """The repository can advance between the pre-clone prefetch and the clone:
    a signed preview resolving to an unsigned/banned manifest must be refused
    on the CLONED manifest, before any build command runs."""
    src = tmp_path / "app-sources" / "demoapp"
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())

    async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "app.json").write_text(json.dumps({"name": "demoapp"}), encoding="utf-8")
        return None

    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)

    admission_calls: list[object] = []

    def _deny_cloned(name, *, manifest=None, action=""):
        admission_calls.append(manifest)
        return "signature required but manifest is unsigned"

    monkeypatch.setattr(registry, "app_admission_denied", _deny_cloned)

    build_calls: list[object] = []

    async def _fake_run_build(build_dir, app_name, log_lines):
        build_calls.append(build_dir)
        return {"ok": True}

    monkeypatch.setattr(registry, "_run_app_build", _fake_run_build)

    result = await registry._clone_build_app("https://example.com/demo.git", "demoapp", [])

    assert result["ok"] is False
    assert "admission" in result["error"]
    assert build_calls == []  # refused before the build
    assert len(admission_calls) == 1  # the cloned manifest was what got checked


@pytest.mark.asyncio
async def test_reused_checkout_pull_never_repoints_origin(monkeypatch, tmp_path):
    """The reuse path only runs after the origin-mismatch gate has verified the
    checkout's origin is byte-identical to the catalog URL (a mismatch is moved
    aside and re-cloned). It must therefore pull directly — never rewrite the
    origin remote — so the fetched code and the persisted provenance URL name
    the same repository by construction, not by mutation."""
    dest = tmp_path / "demoapp"
    (dest / ".git").mkdir(parents=True)
    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)

    async def _fake_origin(path):
        return "https://example.com/new-home.git"

    monkeypatch.setattr(registry, "_clone_origin_url", _fake_origin)

    monkeypatch.setattr(registry, "_read_clone_branch", lambda clone_dir: "main")

    spawned: list[list[str]] = []

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append(list(argv))
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)

    err = await registry._git_clone_or_pull("https://example.com/new-home.git", "main", dest, [])

    assert err is None

    def _subcommand(argv):
        # Skip ``git`` and every leading ``-c KEY=VALUE`` pair the hooks/fsmonitor
        # neutralizer splices in, then read the subcommand slot.
        i = 1
        while i < len(argv) and argv[i] == "-c":
            i += 2
        return argv[i] if i < len(argv) else ""

    # ``git`` is argv[0]; the hooks/fsmonitor neutralizer ``-c`` pairs follow it,
    # then the ``pull --ff-only`` the reuse path has always run.
    git_subs = [_subcommand(cmd) for cmd in spawned if cmd and cmd[0] == "git"]
    assert git_subs == ["pull"], spawned
    assert spawned[0][0] == "git" and spawned[0][-4:] == [
        "pull",
        "--ff-only",
        "https://example.com/new-home.git",
        "main",
    ], spawned
    for cmd in spawned:
        if cmd and cmd[0] == "git":
            assert f"core.hooksPath={os.devnull}" in cmd, cmd
    # Never repoints the origin remote.
    assert not any(cmd[:3] == ["git", "remote", "set-url"] for cmd in spawned)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="onInstall scripts run via /bin/bash; the rewrite scenario is POSIX-only",
)
async def test_install_script_rewriting_manifest_is_refused(monkeypatch, tmp_path):
    """onInstall runs with write access to the checkout; if it rewrites
    app.json to a different identity, registration must be refused — the
    post-script re-read is what install_app would otherwise consume."""
    src = tmp_path / "app-sources" / "demoapp"
    script = "python3 -c \"import json;json.dump({'name':'evil-app'},open('app.json','w'))\""
    _identity_harness(
        monkeypatch,
        src,
        cloned_manifest={"name": "demoapp", "setup": {"onInstall": script}},
    )

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert "refusing" in result["error"].lower()


@pytest.mark.asyncio
async def test_failed_pull_aborts_instead_of_installing_stale_code(monkeypatch, tmp_path):
    """A failed fast-forward pull must abort the operation: installing the
    checkout's stale contents while recording the catalog URL as provenance
    would persist a source the code was never fetched from."""
    dest = tmp_path / "demoapp"
    (dest / ".git").mkdir(parents=True)
    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)

    async def _fake_origin(path):
        return "https://example.com/demo.git"

    monkeypatch.setattr(registry, "_clone_origin_url", _fake_origin)

    monkeypatch.setattr(registry, "_read_clone_branch", lambda clone_dir: "main")

    class _Proc:
        pid = 4242

        def __init__(self, rc):
            self.returncode = rc

        async def communicate(self):
            return b"", b""

    rcs = iter([1])  # the pull (first and only spawn) fails

    async def _fake_spawn(*argv, **kwargs):
        return _Proc(next(rcs))

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)

    err = await registry._git_clone_or_pull("https://example.com/demo.git", "main", dest, [])

    assert err is not None and err["ok"] is False
    assert "stale" in err["error"]


@pytest.mark.asyncio
@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="onInstall scripts run via /bin/bash; the swap scenario is POSIX-only",
)
async def test_provenance_signer_uses_post_script_manifest(monkeypatch, tmp_path):
    """onInstall can replace app.json with a differently signed, still-valid
    manifest; provenance must record the FINAL manifest's signer."""
    src = tmp_path / "app-sources" / "demoapp"
    script = (
        'python3 -c "import json;json.dump('
        "{'name':'demoapp','version':'2.0.0'},open('app.json','w'))\""
    )
    _identity_harness(
        monkeypatch,
        src,
        cloned_manifest={"name": "demoapp", "version": "1.0.0", "setup": {"onInstall": script}},
    )
    commit_reads: list[int] = []

    def _commit_by_read_order(root):
        # First read (if any) would be pre-script; the fix resolves it ONCE,
        # post-script — emulate a script advancing the checkout by returning a
        # different SHA per read and asserting the LAST one is persisted.
        commit_reads.append(len(commit_reads))
        return ("c" if len(commit_reads) == 1 else "d") * 40

    monkeypatch.setattr(registry, "_resolved_clone_commit", _commit_by_read_order)

    def _signer_by_version(manifest):
        return f"signer-of-{getattr(manifest, 'version', '?')}"

    monkeypatch.setattr(registry, "verified_signer", _signer_by_version)

    provenance: dict[str, object] = {}
    monkeypatch.setattr(
        registry,
        "set_app_provenance",
        lambda name, **kwargs: provenance.update(kwargs, name=name),
    )
    monkeypatch.setattr(registry, "get_app", lambda n: None)
    monkeypatch.setattr(
        registry,
        "install_app",
        lambda path, **kwargs: MagicMock(ok=True, name="demoapp", message="done", error=""),
    )

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is True
    # The script swapped in v2.0.0; the signer must be v2's, not v1's.
    assert provenance["signer"] == "signer-of-2.0.0"
    # And the commit is resolved exactly once, post-script — never a stale
    # pre-script read.
    assert len(commit_reads) == 1
    assert provenance["commit"] == "c" * 40


@pytest.mark.asyncio
async def test_admission_rejection_deletes_fresh_clone(monkeypatch, tmp_path):
    """A fresh clone rejected by the cloned-manifest admission gate must leave
    no residue — a leftover would be preferred by the prefetch and poison
    every subsequent attempt."""
    src = tmp_path / "app-sources" / "demoapp"
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())

    async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "app.json").write_text(json.dumps({"name": "demoapp"}), encoding="utf-8")
        return None

    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: "unsigned under policy")

    result = await registry._clone_build_app("https://example.com/demo.git", "demoapp", [])

    assert result["ok"] is False
    assert not src.exists()


@pytest.mark.asyncio
async def test_admission_rejection_rolls_back_preexisting_checkout(monkeypatch, tmp_path):
    """A pre-existing checkout whose pull advanced to a policy-rejected commit
    is rolled back to its pre-pull commit, keeping retries viable."""
    src = tmp_path / "app-sources" / "demoapp"
    (src / ".git").mkdir(parents=True)
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "_resolved_clone_commit", lambda root: "b" * 40)

    async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
        (dest / "app.json").write_text(json.dumps({"name": "demoapp"}), encoding="utf-8")
        return None

    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: "unsigned under policy")

    spawned: list[list[str]] = []

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append(list(argv))
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)

    result = await registry._clone_build_app("https://example.com/demo.git", "demoapp", [])

    assert result["ok"] is False
    assert (src / ".git").is_dir()  # workspace preserved
    assert spawned and spawned[0][:4] == ["git", "reset", "--keep", "b" * 40]


@pytest.mark.asyncio
async def test_postbuild_admission_rejection_deletes_fresh_clone(monkeypatch, tmp_path):
    """The POST-BUILD admission denial must clean the checkout exactly like the
    cloned-admission gate: a fresh clone left at the rejected commit would be
    preferred by the prefetch and poison every retry."""
    src = tmp_path / "app-sources" / "demoapp"
    src.mkdir(parents=True)
    (src / "app.json").write_text(json.dumps({"name": "demoapp"}), encoding="utf-8")
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
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
        return {
            "ok": True,
            "pkg_dir": src,
            "_checkout_preexisted": False,
            "_pre_pull_commit": "",
        }

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)

    calls = {"n": 0}

    def _deny_postbuild(*a, **k):
        calls["n"] += 1
        # 1st call = prefetch admission (pass); 2nd = post-build (deny).
        return "unsigned under policy" if calls["n"] >= 2 else None

    monkeypatch.setattr(registry, "app_admission_denied", _deny_postbuild)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert "admission policy" in result["error"]
    assert not src.exists()  # fresh clone removed — retry starts clean


@pytest.mark.asyncio
async def test_postscript_admission_rejection_rolls_back_preexisting_checkout(
    monkeypatch, tmp_path
):
    """The POST-SCRIPT admission denial must roll a pre-existing checkout back
    to its pre-pull commit — onInstall already ran with write access, so the
    checkout is otherwise left poisoned at the rejected state."""
    src = tmp_path / "app-sources" / "demoapp"
    src.mkdir(parents=True)
    (src / "app.json").write_text(
        json.dumps({"name": "demoapp", "setup": {"onInstall": "true"}}), encoding="utf-8"
    )
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
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
        return {
            "ok": True,
            "pkg_dir": src,
            "_checkout_preexisted": True,
            "_pre_pull_commit": "b" * 40,
        }

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)

    calls = {"n": 0}

    def _deny_postscript(*a, **k):
        calls["n"] += 1
        # 1st = prefetch (pass); 2nd = post-build (pass); 3rd = post-script (deny).
        return "unsigned under policy" if calls["n"] >= 3 else None

    monkeypatch.setattr(registry, "app_admission_denied", _deny_postscript)

    spawned: list[list[str]] = []

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append(list(argv))
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)

    reaped: list[int] = []

    async def _fake_tree_kill(pid, sig):
        reaped.append(pid)

    def _fake_killpg(pgid, sig):
        reaped.append(pgid)

    monkeypatch.setattr(registry.platform_compat, "kill_process_tree_async", _fake_tree_kill)
    monkeypatch.setattr(registry.os, "killpg", _fake_killpg, raising=False)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert "admission policy" in result["error"]
    assert (src / ".git").is_dir() or src.exists()  # workspace preserved, not deleted
    assert any(cmd[:4] == ["git", "reset", "--keep", "b" * 40] for cmd in spawned)
    # Surviving onInstall descendants are reaped BEFORE the final gates
    # re-read the manifest (closes the detached-child rewrite TOCTOU).
    assert 4242 in reaped
    # The manifest file is restored from HEAD as well: a script rewriting
    # app.json is a working-tree edit the reset alone cannot undo.
    assert any(cmd[:4] == ["git", "--literal-pathspecs", "checkout", "--"] for cmd in spawned)


@pytest.mark.asyncio
async def test_moveaside_reclone_retained_not_restored_on_rejection(monkeypatch, tmp_path):
    """When the origin-mismatch gate moves an old checkout aside and
    fresh-clones, a rejection must delete the fresh re-clone (never preserve it
    or reset it toward the moved-aside repository's commit) and must NOT
    restore the moved-aside previous checkout: an origin-mismatch move-aside is
    a DIFFERENT repository, so handing it back to the slot would give a later
    retry the very tree this gate already refused. It stays RETAINED as a
    `.stale-*` sibling (recoverable by hand, swept on a retention timer), which
    is why only a same-origin/branch-drift move-aside is ever restored."""
    src = tmp_path / "app-sources" / "demoapp"
    (src / ".git").mkdir(parents=True)  # OLD checkout pre-exists (origin A)
    (src / "old-work.txt").write_text("precious", encoding="utf-8")
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "_resolved_clone_commit", lambda root: "a" * 40)

    async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
        # Simulate the origin-mismatch move-aside + fresh re-clone. Only
        # `pending_cleanup` is populated, never `restorable_stale` — an
        # origin-mismatch move is never restorable.
        moved = dest.with_name("demoapp.stale-deadbeef")
        dest.rename(moved)
        cleanup = kwargs.get("pending_cleanup")
        if cleanup is not None:
            cleanup.append(moved)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "app.json").write_text(json.dumps({"name": "demoapp"}), encoding="utf-8")
        return None

    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: "unsigned under policy")

    spawned: list[list[str]] = []

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append(list(argv))
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)

    result = await registry._clone_build_app("https://example.com/demo.git", "demoapp", [])

    assert result["ok"] is False
    # No rollback is attempted toward the moved-aside repo's commit ...
    assert not any(cmd[:3] == ["git", "reset", "--keep"] for cmd in spawned)
    # ... the rejected re-clone is gone from the active slot ...
    assert not src.exists()
    # ... and the ORIGIN-mismatched previous checkout is retained, not
    # restored into the slot the gate just refused it for.
    stale = src.with_name("demoapp.stale-deadbeef")
    assert stale.exists()
    assert (stale / "old-work.txt").read_text(encoding="utf-8") == "precious"


@pytest.mark.asyncio
async def test_rejection_restores_users_pre_update_manifest_bytes(monkeypatch, tmp_path):
    """A rejection on a pre-existing checkout must restore app.json to its
    exact PRE-UPDATE working-tree bytes — including the user's uncommitted
    local edits — not to HEAD's version, which would silently discard them."""
    user_manifest = b'{"name": "demoapp", "_user_note": "my local tweak"}'
    src = tmp_path / "app-sources" / "demoapp"
    src.mkdir(parents=True)
    (src / "app.json").write_bytes(b'{"name": "demoapp"}')  # post-build (poisoned) state
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
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
        return {
            "ok": True,
            "pkg_dir": src,
            "_checkout_preexisted": True,
            "_pre_pull_commit": "b" * 40,
            "_pre_update_manifest": user_manifest,
        }

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)

    calls = {"n": 0}

    def _deny_postbuild(*a, **k):
        calls["n"] += 1
        return "unsigned under policy" if calls["n"] >= 2 else None

    monkeypatch.setattr(registry, "app_admission_denied", _deny_postbuild)

    spawned: list[list[str]] = []

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append(list(argv))
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    # The manifest holds the user's exact pre-update bytes again ...
    assert (src / "app.json").read_bytes() == user_manifest
    # ... restored from the snapshot, not via a HEAD checkout.
    assert not any(cmd[:2] == ["git", "--literal-pathspecs"] for cmd in spawned)
    assert any(cmd[:4] == ["git", "reset", "--keep", "b" * 40] for cmd in spawned)


@pytest.mark.asyncio
async def test_identity_refusal_rolls_back_preexisting_checkout(monkeypatch, tmp_path):
    """An IDENTITY refusal on a pre-existing checkout (pull brought a
    self-renaming manifest) must roll the workspace back like the admission
    gates do — preserved but left at the renamed manifest, the prefetch would
    re-reject every retry before a fixed remote could be pulled."""
    src = tmp_path / "app-sources" / "demoapp"
    (src / ".git").mkdir(parents=True)
    (src / "keep.txt").write_text("user state", encoding="utf-8")
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "_resolved_clone_commit", lambda root: "b" * 40)

    async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
        (dest / "app.json").write_text(json.dumps({"name": "renamed-app"}), encoding="utf-8")
        return None

    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)

    spawned: list[list[str]] = []

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append(list(argv))
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kwargs: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)

    result = await registry._clone_build_app("https://example.com/demo.git", "demoapp", [])

    assert result["ok"] is False
    assert "refusing" in result["error"].lower()
    # Workspace preserved ...
    assert (src / ".git").is_dir()
    assert (src / "keep.txt").read_text(encoding="utf-8") == "user state"
    # ... but un-poisoned: rolled back to the pre-pull commit AND the manifest
    # restored from HEAD.
    assert any(cmd[:4] == ["git", "reset", "--keep", "b" * 40] for cmd in spawned)
    assert any(cmd[:4] == ["git", "--literal-pathspecs", "checkout", "--"] for cmd in spawned)


@pytest.mark.asyncio
async def test_build_deleting_manifest_is_refused_with_cleanup(monkeypatch, tmp_path):
    """A build step that DELETES app.json must go through the identity refusal
    (fail-closed) and its checkout cleanup — not an early return that leaves a
    fresh checkout poisoned in the app-sources slot."""
    src = tmp_path / "app-sources" / "demoapp"
    src.mkdir(parents=True)  # fresh clone; build "deleted" app.json — none written
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
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
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)

    async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
        return {
            "ok": True,
            "pkg_dir": src,
            "_checkout_preexisted": False,
            "_pre_pull_commit": "",
        }

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert "refusing" in result["error"].lower()
    assert not src.exists()  # fresh checkout removed — no poisoned residue
