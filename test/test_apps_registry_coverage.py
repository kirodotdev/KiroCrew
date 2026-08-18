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
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from conftest import requires_symlinks
from kiro_crew.apps import registry
from kiro_crew.platform import PlatformCompositionError

# ---------------------------------------------------------------------------
# Fixtures / shared fakes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _explicit_registry_execution_admission(monkeypatch):
    """These tests reach admitted registry code paths unless they say otherwise."""
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """Redirect the manifest cache to a temp directory (never the real home)."""
    cache = tmp_path / "cache" / "app-manifests"
    cache.mkdir(parents=True)
    monkeypatch.setattr(registry, "_manifest_cache_dir", lambda: cache)
    return cache


class _FakeProc:
    """Minimal stand-in for ``asyncio.subprocess.Process``.

    *stdout_lines* makes ``proc.stdout`` async-iterable, which is what
    ``_run_app_build``'s drain loop consumes.
    """

    def __init__(
        self,
        returncode: int = 0,
        stdout_lines: list[bytes] | None = None,
        output: bytes = b"",
    ) -> None:
        self.returncode = returncode
        self.pid = 31337
        self.kill_calls = 0
        self.wait_calls = 0
        self._output = output
        self.stdout = _AsyncLines(stdout_lines or [])

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._output, b""

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> int:
        self.wait_calls += 1
        return self.returncode or 0


class _AsyncLines:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


def _fake_sandbox(monkeypatch, procs):
    """Neutralize the sandbox wrappers and hand out *procs* in order.

    Returns the list that each spawn's argv is appended to.
    """
    spawned: list[list[str]] = []
    queue = list(procs)

    async def _spawn(*argv, **kwargs):
        spawned.append(list(argv))
        return queue.pop(0) if queue else _FakeProc()

    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (list(cmd), None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: list(cmd))
    monkeypatch.setattr(registry, "create_subprocess_limited", _spawn)
    return spawned


def _reg(name: str, repo: str, branch: str = "main") -> SimpleNamespace:
    """A configured-registry stand-in with the fields the module reads."""
    return SimpleNamespace(name=name, repo=repo, branch=branch)


def _config_with(monkeypatch, registries: list[SimpleNamespace]) -> None:
    """Make every ``KiroCrewConfig.load()`` in this module see *registries*."""
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load",
        classmethod(lambda cls: SimpleNamespace(registries=registries)),
    )


# ---------------------------------------------------------------------------
# StreamingLogLines
# ---------------------------------------------------------------------------


class TestStreamingLogLines:
    def test_append_stores_and_forwards(self):
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        lines = registry.StreamingLogLines(queue)
        lines.append("hello")
        assert list(lines) == ["hello"]
        assert queue.get_nowait() == "hello"

    def test_extend_forwards_every_line(self):
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        lines = registry.StreamingLogLines(queue)
        lines.extend(["a", "b"])
        assert list(lines) == ["a", "b"]
        assert [queue.get_nowait(), queue.get_nowait()] == ["a", "b"]

    def test_full_queue_drops_without_raising(self):
        """A slow SSE consumer must not break the install it is watching."""
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1)
        lines = registry.StreamingLogLines(queue)
        lines.append("kept")
        lines.append("dropped")
        # The list keeps everything; only the queue drops the overflow.
        assert list(lines) == ["kept", "dropped"]
        assert queue.get_nowait() == "kept"
        assert queue.empty()


# ---------------------------------------------------------------------------
# Environment construction
# ---------------------------------------------------------------------------


class TestEnvHelpers:
    def test_minimal_env_keeps_allowlisted_and_drops_the_rest(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("MY_SECRET_TOKEN_VALUE", "hunter2")
        env = registry.minimal_env()
        assert env["PATH"] == "/usr/bin"
        assert "MY_SECRET_TOKEN_VALUE" not in env

    def test_minimal_env_applies_extras(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        assert registry.minimal_env(PATH="/opt/bin")["PATH"] == "/opt/bin"

    def test_anonymous_git_env_strips_credential_carriers(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
        monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /home/me/.ssh/id_ed25519")
        env = registry.anonymous_git_env()
        assert "SSH_AUTH_SOCK" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
        assert "id_ed25519" not in env["GIT_SSH_COMMAND"]


# ---------------------------------------------------------------------------
# URL shape helpers
# ---------------------------------------------------------------------------


class TestUrlHelpers:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ({"gitUrl": " https://example.com/a.git "}, "https://example.com/a.git"),
            ({"repo": "https://example.com/b.git"}, "https://example.com/b.git"),
            ({"gitUrl": "", "repo": "legacy-name"}, "legacy-name"),
            ({}, ""),
            ({"gitUrl": {"nested": "object"}}, ""),
            ({"gitUrl": 42}, ""),
        ],
    )
    def test_entry_git_url(self, raw, expected):
        assert registry._entry_git_url(raw) == expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://example.com/a.git", True),
            ("http://example.com/a.git", True),
            ("ssh://git@example.com/a.git", True),
            ("git://example.com/a.git", True),
            ("git+ssh://example.com/a.git", True),
            ("git@example.com:owner/a.git", True),
            ("bare-name", False),
            ("", False),
            ("/abs/path", False),
        ],
    )
    def test_looks_like_git_url(self, url, expected):
        assert registry._looks_like_git_url(url) is expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://Example.COM/a.git", "example.com"),
            ("ssh://git@Example.com:2222/a.git", "example.com"),
            ("git@example.com:owner/a.git", "example.com"),
            ("git+ssh://user@host.internal/a.git", "host.internal"),
            ("  ", ""),
            ("not a url", ""),
        ],
    )
    def test_git_url_host(self, url, expected):
        assert registry._git_url_host(url) == expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("ssh://git@github.com/a.git", True),
            ("git+ssh://git@github.com/a.git", True),
            ("git@github.com:owner/a.git", True),
            ("https://github.com/owner/a.git", False),
            ("", False),
        ],
    )
    def test_is_ssh_git_url(self, url, expected):
        assert registry._is_ssh_git_url(url) is expected


class TestCloneSandboxMode:
    def test_public_ssh_forge_gets_standard(self):
        assert registry._clone_sandbox_mode("git@github.com:owner/a.git") == "standard"

    def test_configured_host_is_added_to_the_trusted_set(self):
        mode = registry._clone_sandbox_mode(
            "git@gitea.internal:owner/a.git", frozenset({"gitea.internal"})
        )
        assert mode == "standard"

    def test_untrusted_ssh_host_stays_strict(self):
        assert registry._clone_sandbox_mode("git@evil.example:owner/a.git") == "strict"

    def test_https_never_needs_ssh_keys(self):
        assert registry._clone_sandbox_mode("https://github.com/owner/a.git") == "strict"

    def test_hostless_ssh_url_fails_closed(self, monkeypatch):
        monkeypatch.setattr(registry, "_is_ssh_git_url", lambda url: True)
        monkeypatch.setattr(registry, "_git_url_host", lambda url: "")
        assert registry._clone_sandbox_mode("nonsense") == "strict"


class TestConfiguredRegistryHosts:
    def test_collects_hosts_of_configured_registries(self, monkeypatch):
        _config_with(
            monkeypatch,
            [
                _reg("a", "https://gitea.internal/org/idx.git"),
                _reg("b", "bare-name-no-host"),
            ],
        )
        assert registry._configured_registry_hosts() == frozenset({"gitea.internal"})

    def test_config_load_failure_degrades_to_empty(self, monkeypatch):
        def _boom(cls):
            raise OSError("config unreadable")

        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", classmethod(_boom)
        )
        assert registry._configured_registry_hosts() == frozenset()


class TestContextCloneSandboxMode:
    def test_composition_error_is_never_swallowed(self, monkeypatch):
        def _boom():
            raise PlatformCompositionError("companion missing")

        monkeypatch.setattr(registry, "current_context", _boom)
        with pytest.raises(PlatformCompositionError):
            registry._context_clone_sandbox_mode("git@github.com:o/a.git")

    def test_adapter_failure_falls_back_to_the_module_decision(self, monkeypatch):
        def _boom():
            raise RuntimeError("adapter down")

        monkeypatch.setattr(registry, "current_context", _boom)
        monkeypatch.setattr(registry, "_configured_registry_hosts", frozenset)
        # The security gate must survive the adapter: a public forge still
        # resolves, an unknown host still fails closed.
        assert registry._context_clone_sandbox_mode("git@github.com:o/a.git") == "standard"
        assert registry._context_clone_sandbox_mode("git@evil.example:o/a.git") == "strict"


class TestIsCloneHostTrusted:
    def test_hostless_url_is_untrusted(self):
        assert registry.is_clone_host_trusted("bare-name") is False

    def test_composition_error_propagates(self, monkeypatch):
        def _boom():
            raise PlatformCompositionError("companion missing")

        monkeypatch.setattr(registry, "current_context", _boom)
        with pytest.raises(PlatformCompositionError):
            registry.is_clone_host_trusted("https://github.com/o/a.git")

    def test_adapter_failure_keeps_the_default_trust_set(self, monkeypatch):
        def _boom():
            raise RuntimeError("adapter down")

        monkeypatch.setattr(registry, "current_context", _boom)
        monkeypatch.setattr(registry, "_configured_registry_hosts", frozenset)
        assert registry.is_clone_host_trusted("https://github.com/o/a.git") is True
        assert registry.is_clone_host_trusted("https://127.0.0.1:8443/x.git") is False


class TestOwnerDesignatedRepo:
    def test_bundled_entry_is_not_index_originated(self):
        assert registry._is_owner_designated_repo({"gitUrl": "https://x/y.git"}) is False

    def test_entry_without_resolvable_url_is_refused(self, monkeypatch):
        assert registry._is_owner_designated_repo({"_registry": "mine"}) is False

    def test_byte_identical_url_is_owner_designated(self, monkeypatch):
        _config_with(monkeypatch, [_reg("mine", "https://gitea.internal/org/idx.git")])
        entry = {"_registry": "mine", "gitUrl": "https://gitea.internal/org/idx.git"}
        assert registry._is_owner_designated_repo(entry) is True

    def test_sibling_repo_on_the_same_host_is_not(self, monkeypatch):
        _config_with(monkeypatch, [_reg("mine", "https://gitea.internal/org/idx.git")])
        entry = {"_registry": "mine", "gitUrl": "https://gitea.internal/org/private.git"}
        assert registry._is_owner_designated_repo(entry) is False


class TestSelCredentialGrant:
    def test_audit_failure_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(
            registry, "_sel_fn", MagicMock(side_effect=RuntimeError("sel down"))
        )
        registry._sel_credential_grant("op", "https://x/y.git")  # must not raise

    def test_grant_is_logged_when_sel_is_present(self, monkeypatch):
        sel_obj = MagicMock()
        monkeypatch.setattr(registry, "_sel_fn", lambda: sel_obj)
        registry._sel_credential_grant("install_from_registry", "https://x/y.git")
        assert sel_obj.log_api_access.call_args.kwargs["outcome"] == "granted"


# ---------------------------------------------------------------------------
# Registry file loading + edition rows
# ---------------------------------------------------------------------------


class TestLoadRegistryFile:
    def test_missing_file_yields_no_rows(self, monkeypatch, tmp_path):
        monkeypatch.setattr(registry, "_REGISTRY_FILE", tmp_path / "absent.json")
        monkeypatch.setattr(registry, "_edition_registry_rows", list)
        assert registry._load_registry_file() == []

    def test_non_array_json_is_rejected(self, monkeypatch, tmp_path):
        path = tmp_path / "app-registry.json"
        path.write_text('{"not": "an array"}', encoding="utf-8")
        monkeypatch.setattr(registry, "_REGISTRY_FILE", path)
        monkeypatch.setattr(registry, "_edition_registry_rows", list)
        assert registry._load_registry_file() == []

    def test_invalid_json_is_rejected(self, monkeypatch, tmp_path):
        path = tmp_path / "app-registry.json"
        path.write_text("{{{ not json", encoding="utf-8")
        monkeypatch.setattr(registry, "_REGISTRY_FILE", path)
        monkeypatch.setattr(registry, "_edition_registry_rows", list)
        assert registry._load_registry_file() == []

    def test_edition_rows_are_add_only_and_never_repoint_a_core_row(
        self, monkeypatch, tmp_path
    ):
        path = tmp_path / "app-registry.json"
        path.write_text(
            json.dumps([{"name": "core-app", "repo": "core/repo"}]), encoding="utf-8"
        )
        monkeypatch.setattr(registry, "_REGISTRY_FILE", path)
        monkeypatch.setattr(
            registry,
            "_edition_registry_rows",
            lambda: [
                {"name": "core-app", "repo": "attacker/repo"},
                {"name": "edition-app", "repo": "edition/repo"},
            ],
        )
        rows = registry._load_registry_file()
        assert [r["name"] for r in rows] == ["core-app", "edition-app"]
        assert rows[0]["repo"] == "core/repo"


class TestEditionRegistryRows:
    def test_malformed_rows_are_dropped(self, monkeypatch):
        loader = SimpleNamespace(
            registry_rows=lambda: [
                {"name": "good"},
                {"name": 7},
                "not-a-dict",
                {},
            ]
        )
        monkeypatch.setattr(
            registry, "current_context", lambda: SimpleNamespace(apps_loader=loader)
        )
        assert registry._edition_registry_rows() == [{"name": "good"}]

    def test_seam_failure_falls_back_to_bundled_only(self, monkeypatch):
        def _boom():
            raise RuntimeError("loader down")

        monkeypatch.setattr(registry, "current_context", _boom)
        assert registry._edition_registry_rows() == []


# ---------------------------------------------------------------------------
# Manifest cache
# ---------------------------------------------------------------------------


class TestManifestCache:
    def test_missing_cache_reads_none(self, cache_dir):
        assert registry._read_manifest_cache("nope") is None

    def test_round_trip(self, cache_dir):
        registry._write_manifest_cache("demo", {"name": "demo", "version": "1.0.0"})
        assert registry._read_manifest_cache("demo") == {"name": "demo", "version": "1.0.0"}

    def test_stale_cache_reads_none(self, cache_dir):
        registry._write_manifest_cache("demo", {"name": "demo"})
        path = registry._manifest_cache_path("demo")
        past = time.time() - registry._MANIFEST_CACHE_TTL - 3600
        os.utime(path, (past, past))
        assert registry._read_manifest_cache("demo") is None

    def test_corrupt_cache_reads_none(self, cache_dir):
        registry._manifest_cache_path("demo").write_text("not json", encoding="utf-8")
        assert registry._read_manifest_cache("demo") is None

    def test_write_failure_is_swallowed(self, cache_dir, monkeypatch):
        def _boom(path, data):
            raise OSError("disk full")

        monkeypatch.setattr(registry, "atomic_write", _boom)
        registry._write_manifest_cache("demo", {"name": "demo"})  # must not raise
        assert registry._read_manifest_cache("demo") is None

    def test_traversing_name_is_confined_to_the_cache_dir(self, cache_dir):
        path = registry._manifest_cache_path("../../escape")
        assert cache_dir.resolve() == path.parent.resolve()

    def test_external_cache_write_failure_is_swallowed(self, cache_dir, monkeypatch):
        def _boom(path, data):
            raise OSError("disk full")

        monkeypatch.setattr(registry, "atomic_write", _boom)
        registry._write_external_registry_cache("mine", [{"name": "a"}])
        assert registry._read_external_registry_cache("mine") is None


class TestSafeCacheStem:
    def test_pure_name_is_byte_identical(self):
        assert registry._safe_cache_stem("my-app_1.0") == "my-app_1.0"

    def test_traversal_is_slugified_and_disambiguated(self):
        stem = registry._safe_cache_stem("../../config")
        assert "/" not in stem and ".." not in stem
        # Distinct originals must not collide after slugification.
        assert stem != registry._safe_cache_stem("..-..-config")

    def test_all_disallowed_characters_still_yield_a_stem(self):
        assert registry._safe_cache_stem("///").startswith("app-")


class TestExpireCacheFile:
    def test_backdates_instead_of_unlinking(self, cache_dir):
        registry._write_manifest_cache("demo", {"name": "demo"})
        path = registry._manifest_cache_path("demo")
        registry._expire_cache_file(path)
        assert path.is_file()  # data survives as stale fallback
        assert registry._read_manifest_cache("demo") is None

    def test_missing_file_is_a_no_op(self, cache_dir):
        registry._expire_cache_file(cache_dir / "absent.json")

    def test_path_outside_the_cache_dir_is_refused(self, cache_dir, tmp_path):
        outside = tmp_path / "victim.json"
        outside.write_text("{}", encoding="utf-8")
        before = outside.stat().st_mtime
        registry._expire_cache_file(outside)
        assert outside.stat().st_mtime == before

    def test_utime_failure_is_swallowed(self, cache_dir, monkeypatch):
        registry._write_manifest_cache("demo", {"name": "demo"})

        def _boom(path, times):
            raise OSError("read-only fs")

        monkeypatch.setattr(registry.os, "utime", _boom)
        registry._expire_cache_file(registry._manifest_cache_path("demo"))


# ---------------------------------------------------------------------------
# Subdirectory containment
# ---------------------------------------------------------------------------


class TestSubdirGates:
    @pytest.mark.parametrize(
        "subdir,expected",
        [
            (None, True),
            ("", True),
            ("apps/demo", True),
            ("..", False),
            ("apps/../..", False),
            ("./apps", False),
            ("/etc", False),
            ("C:/Windows", False),
            ("apps\\demo", False),
            ("apps/\x00demo", False),
            (7, False),
            (["apps"], False),
        ],
    )
    def test_is_safe_registry_subdir(self, subdir, expected):
        assert registry._is_safe_registry_subdir(subdir) is expected

    def test_contained_join_returns_root_for_empty_subdir(self, tmp_path):
        assert registry._contained_join(tmp_path, "") == tmp_path

    def test_contained_join_resolves_inside(self, tmp_path):
        (tmp_path / "apps" / "demo").mkdir(parents=True)
        joined = registry._contained_join(tmp_path, "apps/demo")
        assert joined is not None
        assert os.path.realpath(joined) == os.path.realpath(tmp_path / "apps" / "demo")

    def test_contained_join_rejects_traversal(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        assert registry._contained_join(root, "../outside") is None

    @requires_symlinks
    def test_contained_join_rejects_symlink_escape(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(str(outside), str(root / "link"))
        assert registry._contained_join(root, "link") is None

    def test_contained_join_degrades_to_none_on_os_error(self, tmp_path, monkeypatch):
        def _boom(self, strict=False):
            raise OSError("too many levels")

        monkeypatch.setattr(Path, "resolve", _boom)
        assert registry._contained_join(tmp_path, "sub") is None


# ---------------------------------------------------------------------------
# Manifest fetch
# ---------------------------------------------------------------------------


def _tmp_clone_dir(monkeypatch, tmp_path, name: str = "clone") -> Path:
    """Point ``tempfile.mkdtemp`` at a directory under *tmp_path*.

    Keeps the throwaway manifest clone inside the test sandbox, so the module's
    own ``shutil.rmtree`` in its ``finally`` block leaves nothing behind.
    """
    import tempfile

    target = tmp_path / name
    target.mkdir()
    monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **k: str(target))
    return target


class TestFetchAppManifest:
    @pytest.mark.asyncio
    async def test_local_checkout_is_used_when_origin_and_branch_match(
        self, monkeypatch, tmp_path
    ):
        src = tmp_path / "app-sources" / "demo"
        src.mkdir(parents=True)
        (src / "app.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")
        monkeypatch.setattr(registry, "app_source_dir", lambda n: src)

        async def _origin_matches(dest, git_url):
            return True

        async def _branch_matches(dest, branch):
            return True

        monkeypatch.setattr(registry, "_clone_origin_matches", _origin_matches)
        monkeypatch.setattr(registry, "_clone_branch_matches", _branch_matches)

        got = await registry._fetch_app_manifest(
            "o/demo", "main", app_name="demo", git_url="https://github.com/o/demo.git"
        )
        assert got == {"name": "demo"}

    @pytest.mark.asyncio
    async def test_corrupt_local_manifest_falls_through(self, monkeypatch, tmp_path):
        src = tmp_path / "app-sources" / "demo"
        src.mkdir(parents=True)
        (src / "app.json").write_text("not json", encoding="utf-8")
        monkeypatch.setattr(registry, "app_source_dir", lambda n: src)

        async def _true(*a, **k):
            return True

        monkeypatch.setattr(registry, "_clone_origin_matches", _true)
        monkeypatch.setattr(registry, "_clone_branch_matches", _true)
        # Not cloneable, so the fall-through path ends in None rather than a clone.
        assert (
            await registry._fetch_app_manifest(
                "o/demo", "main", app_name="demo", git_url="bare-name"
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_non_cloneable_url_returns_none(self):
        assert await registry._fetch_app_manifest("bare", "main") is None

    @pytest.mark.asyncio
    async def test_untrusted_host_is_refused(self, monkeypatch):
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: False)
        got = await registry._fetch_app_manifest(
            "o/demo", "main", git_url="https://127.0.0.1:8443/x.git"
        )
        assert got is None

    @pytest.mark.asyncio
    async def test_failed_clone_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
        _tmp_clone_dir(monkeypatch, tmp_path)
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=128)])
        got = await registry._fetch_app_manifest(
            "o/demo", "main", git_url="https://github.com/o/demo.git"
        )
        assert got is None

    @pytest.mark.asyncio
    async def test_missing_manifest_in_clone_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
        _tmp_clone_dir(monkeypatch, tmp_path)
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        got = await registry._fetch_app_manifest(
            "o/demo", "main", git_url="https://github.com/o/demo.git"
        )
        assert got is None

    @pytest.mark.asyncio
    async def test_successful_clone_reads_the_manifest(self, monkeypatch, tmp_path):
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
        clone = _tmp_clone_dir(monkeypatch, tmp_path)
        (clone / "app.json").write_text(
            json.dumps({"name": "demo", "version": "2.0.0"}), encoding="utf-8"
        )
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        got = await registry._fetch_app_manifest(
            "o/demo", "main", git_url="https://github.com/o/demo.git"
        )
        assert got == {"name": "demo", "version": "2.0.0"}

    @pytest.mark.asyncio
    async def test_subdirectory_escape_after_clone_is_refused(self, monkeypatch, tmp_path):
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
        _tmp_clone_dir(monkeypatch, tmp_path)
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        monkeypatch.setattr(registry, "_contained_join", lambda root, sub: None)
        got = await registry._fetch_app_manifest(
            "o/demo", "main", "evil", git_url="https://github.com/o/demo.git"
        )
        assert got is None

    @pytest.mark.asyncio
    async def test_owner_designated_clone_uses_owner_credentials(self, monkeypatch, tmp_path):
        """The same-repo carve-out must flip env AND audit the grant."""
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
        clone = _tmp_clone_dir(monkeypatch, tmp_path)
        (clone / "app.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")

        grants: list[str] = []
        monkeypatch.setattr(
            registry, "_sel_credential_grant", lambda op, url: grants.append(op)
        )
        monkeypatch.setattr(registry, "_context_clone_sandbox_mode", lambda url: "standard")
        monkeypatch.setattr(registry, "minimal_env", lambda **kw: {"SENTINEL": "owner"})

        seen_env: list[dict[str, str]] = []

        async def _spawn(*argv, **kwargs):
            seen_env.append(kwargs["env"])
            return _FakeProc(returncode=0)

        monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (list(cmd), None))
        monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: list(cmd))
        monkeypatch.setattr(registry, "create_subprocess_limited", _spawn)

        got = await registry._fetch_app_manifest(
            "o/demo",
            "main",
            git_url="https://github.com/o/demo.git",
            owner_designated=True,
        )
        assert got == {"name": "demo"}
        assert seen_env == [{"SENTINEL": "owner"}]
        assert grants == ["fetch_app_manifest"]

    @pytest.mark.asyncio
    async def test_anonymous_clone_is_the_default_posture(self, monkeypatch, tmp_path):
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
        clone = _tmp_clone_dir(monkeypatch, tmp_path)
        (clone / "app.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")
        monkeypatch.setattr(registry, "anonymous_git_env", lambda **kw: {"SENTINEL": "anon"})

        modes: list[str] = []
        seen_env: list[dict[str, str]] = []

        async def _spawn(*argv, **kwargs):
            seen_env.append(kwargs["env"])
            return _FakeProc(returncode=0)

        def _wrap(cmd, mode=""):
            modes.append(mode)
            return list(cmd), None

        monkeypatch.setattr(registry, "wrap_argv", _wrap)
        monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: list(cmd))
        monkeypatch.setattr(registry, "create_subprocess_limited", _spawn)

        await registry._fetch_app_manifest(
            "o/demo", "main", git_url="https://github.com/o/demo.git"
        )
        assert seen_env == [{"SENTINEL": "anon"}]
        assert modes == ["strict"]


# ---------------------------------------------------------------------------
# Manifest resolution / merge / enrichment
# ---------------------------------------------------------------------------


class TestResolveManifest:
    @pytest.mark.asyncio
    async def test_entry_without_url_is_returned_unchanged(self):
        entry = {"name": "demo"}
        assert await registry._resolve_manifest(entry) is entry

    @pytest.mark.asyncio
    async def test_cached_manifest_short_circuits_the_fetch(self, monkeypatch):
        monkeypatch.setattr(
            registry, "_read_manifest_cache", lambda name: {"description": "cached"}
        )

        async def _never(*a, **k):
            raise AssertionError("fetch must not run when a fresh cache exists")

        monkeypatch.setattr(registry, "_fetch_app_manifest", _never)
        got = await registry._resolve_manifest(
            {"name": "demo", "gitUrl": "https://github.com/o/demo.git"}
        )
        assert got["description"] == "cached"

    @pytest.mark.asyncio
    async def test_fetched_manifest_is_cached_and_merged(self, monkeypatch):
        monkeypatch.setattr(registry, "_read_manifest_cache", lambda name: None)
        monkeypatch.setattr(registry, "_is_owner_designated_repo", lambda entry: False)
        written: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            registry,
            "_write_manifest_cache",
            lambda name, data: written.append((name, data)),
        )

        async def _fetch(*a, **k):
            return {"description": "fresh"}

        monkeypatch.setattr(registry, "_fetch_app_manifest", _fetch)
        got = await registry._resolve_manifest(
            {"name": "demo", "gitUrl": "https://github.com/o/demo.git"}
        )
        assert got["description"] == "fresh"
        assert written == [("demo", {"description": "fresh"})]

    @pytest.mark.asyncio
    async def test_unavailable_manifest_leaves_a_minimal_row(self, monkeypatch):
        monkeypatch.setattr(registry, "_read_manifest_cache", lambda name: None)
        monkeypatch.setattr(registry, "_is_owner_designated_repo", lambda entry: False)

        async def _fetch(*a, **k):
            return None

        monkeypatch.setattr(registry, "_fetch_app_manifest", _fetch)
        entry = {"name": "demo", "gitUrl": "https://github.com/o/demo.git"}
        assert await registry._resolve_manifest(entry) == entry


class TestMergeManifest:
    def test_display_fields_come_from_the_manifest(self):
        merged = registry._merge_manifest(
            {"name": "demo", "repo": "o/demo", "branch": "main"},
            {
                "displayName": "Demo",
                "description": "d",
                "version": "1.2.3",
                "author": "someone",
                "tags": ["a"],
                "highlights": ["h"],
                "license": "MIT",
                "minKiroCrewVersion": "0.1.0",
            },
        )
        assert merged["displayName"] == "Demo"
        assert merged["tags"] == ["a"]
        assert merged["minKiroCrewVersion"] == "0.1.0"
        # Registry-only fields survive.
        assert merged["name"] == "demo" and merged["branch"] == "main"

    def test_runtime_fields_are_nested_under_manifest(self):
        merged = registry._merge_manifest(
            {"name": "demo", "repo": "o/demo"},
            {
                "agents": ["a"],
                "skills": ["s"],
                "crons": [],
                "mcpServers": {},
                "permissions": {"x": 1},
                "setup": {"onInstall": "echo hi"},
                "ui": {"panel": True},
                "openCommand": "open",
            },
        )
        assert merged["manifest"]["setup"] == {"onInstall": "echo hi"}
        assert merged["manifest"]["openCommand"] == "open"

    def test_no_manifest_key_means_no_manifest_block(self):
        merged = registry._merge_manifest({"name": "demo", "repo": "o/demo"}, {})
        assert "manifest" not in merged

    def test_platform_config_is_carried_over(self):
        merged = registry._merge_manifest(
            {"name": "demo", "repo": "o/demo"}, {"platform": {"os": ["macos"]}}
        )
        assert merged["platform"] == {"os": ["macos"]}

    def test_image_paths_become_blob_proxy_urls(self):
        merged = registry._merge_manifest(
            {"name": "demo", "repo": "o/demo"},
            {
                "iconPath": "assets/icon.png",
                "icon": "sparkles",
                "screenshots": ["a.png", "b.png"],
                "screenshotsDark": ["a-dark.png"],
                "heroImage": "hero.png",
                "heroImageDark": "hero-dark.png",
                "heroImageDetail": "detail.png",
                "heroImageDetailDark": "detail-dark.png",
            },
        )
        assert merged["iconUrl"] == "/api/apps/blob?repo=o/demo&path=assets/icon.png"
        assert merged["icon"] == "sparkles"
        assert merged["screenshots"] == [
            "/api/apps/blob?repo=o/demo&path=a.png",
            "/api/apps/blob?repo=o/demo&path=b.png",
        ]
        assert merged["screenshotsDark"] == ["/api/apps/blob?repo=o/demo&path=a-dark.png"]
        assert merged["heroImage"] == "/api/apps/blob?repo=o/demo&path=hero.png"
        assert merged["heroImageDark"] == "/api/apps/blob?repo=o/demo&path=hero-dark.png"
        assert merged["heroImageDetail"] == "/api/apps/blob?repo=o/demo&path=detail.png"
        assert (
            merged["heroImageDetailDark"]
            == "/api/apps/blob?repo=o/demo&path=detail-dark.png"
        )

    def test_without_a_repo_no_blob_urls_are_minted(self):
        merged = registry._merge_manifest(
            {"name": "demo"},
            {"iconPath": "icon.png", "screenshots": ["a.png"], "heroImage": "h.png"},
        )
        assert "iconUrl" not in merged
        assert "screenshots" not in merged
        assert "heroImage" not in merged

    def test_the_entry_is_not_mutated(self):
        entry = {"name": "demo", "repo": "o/demo"}
        registry._merge_manifest(entry, {"description": "d"})
        assert entry == {"name": "demo", "repo": "o/demo"}


class TestEnrichWithInstallStatus:
    def test_installed_app_carries_manager_state(self):
        rows = registry._enrich_with_install_status(
            [{"name": "demo", "version": "2.0.0"}],
            {
                "demo": {
                    "version": "1.0.0",
                    "enabled": True,
                    "origin": "registry",
                    "resources": "app",
                    "lifecycle": "app",
                }
            },
        )
        row = rows[0]
        assert row["installed"] is True
        assert row["installedVersion"] == "1.0.0"
        assert row["enabled"] is True
        assert row["resources"] == "app"
        assert row["updateAvailable"] is True

    def test_externally_detected_app_is_marked_external(self):
        rows = registry._enrich_with_install_status(
            [{"name": "demo", "version": "2.0.0"}], {}, detected={"demo"}
        )
        row = rows[0]
        assert row["installed"] is True
        assert row["installedVersion"] == "unknown"
        assert row["origin"] == "external"
        assert row["updateAvailable"] is False

    def test_not_installed_app_reports_no_update(self):
        rows = registry._enrich_with_install_status([{"name": "demo"}], {})
        assert rows[0]["installed"] is False
        assert rows[0]["updateAvailable"] is False


class TestApplyTrustFields:
    def test_external_row_can_never_self_verify_or_self_feature(self):
        rows = registry._apply_trust_fields(
            [
                {
                    "name": "demo",
                    "_registry": "third-party",
                    "_index_author": "kirocrew",
                    "verified": True,
                    "provenance": "official",
                    "featured": True,
                }
            ]
        )
        assert rows[0]["provenance"] == "external"
        assert rows[0]["verified"] is False
        assert "featured" not in rows[0]
        assert "_index_author" not in rows[0]

    def test_builtin_row_is_verified(self):
        rows = registry._apply_trust_fields([{"name": "demo", "origin": "builtin"}])
        assert rows[0]["provenance"] == "builtin"
        assert rows[0]["verified"] is True

    def test_core_row_is_verified_only_from_the_index_author(self):
        rows = registry._apply_trust_fields(
            [
                {"name": "a", "_index_author": "KiroCrew"},  # brand-ok: registry.py compares author.lower() == "kirocrew"
                {"name": "b", "_index_author": "someone-else"},
                {"name": "c", "_index_author": {"name": "kirocrew"}},
            ]
        )
        assert [r["verified"] for r in rows] == [True, False, False]
        assert all(r["provenance"] == "official" for r in rows)


class TestVersionNewer:
    @pytest.mark.parametrize(
        "registry_ver,installed_ver,expected",
        [
            ("2.0.0", "1.0.0", True),
            ("1.0.1", "1.0.0", True),
            ("1.0.0", "1.0.0", False),
            ("1.0.0", "2.0.0", False),
            ("1.1", "1.0.9", True),
            ("2", "1.9.9", True),
            ("2.0.0-beta.1", "1.9.9", True),
            ("1.0.0+build.9", "1.0.0", False),
            ("not-a-version", "1.0.0", False),
            ("1.0.0", "", False),
            ("", "", False),
        ],
    )
    def test_version_newer(self, registry_ver, installed_ver, expected):
        assert registry._version_newer(registry_ver, installed_ver) is expected

    def test_non_string_input_is_conservative(self):
        assert registry._version_newer(None, "1.0.0") is False


# ---------------------------------------------------------------------------
# Candidate resolution / provenance pinning
# ---------------------------------------------------------------------------


class TestCandidateResolution:
    def test_candidates_span_bundled_and_every_configured_registry(
        self, monkeypatch, cache_dir
    ):
        # `_registry_app_candidates` consults the official catalog with a fresh
        # uncached HTTPS fetch, and DROPS every candidate when that lookup
        # fails (#4236); pin "catalog reachable, app absent" so the assertion
        # exercises the bundled + external span deterministically.
        monkeypatch.setattr(
            "kiro_crew.apps.official_catalog.inventory_for_install",
            lambda name: None,
        )
        monkeypatch.setattr(
            registry,
            "_load_registry_file",
            lambda: [{"name": "demo", "gitUrl": "https://github.com/core/demo.git"}, "junk"],
        )
        _config_with(monkeypatch, [_reg("mine", "https://gitea.internal/idx.git")])
        registry._write_external_registry_cache(
            "mine",
            [
                {"name": "demo", "gitUrl": "https://gitea.internal/other/demo.git"},
                {"name": "unrelated"},
            ],
        )
        candidates = registry._registry_app_candidates("demo")
        assert [registry._entry_git_url(c) for c in candidates] == [
            "https://github.com/core/demo.git",
            "https://gitea.internal/other/demo.git",
        ]

    def test_pinned_entry_requires_both_url_and_registry_to_match(self, monkeypatch):
        monkeypatch.setattr(
            registry,
            "_registry_app_candidates",
            lambda name: [
                {"gitUrl": "https://x/other.git", "_registry": "mine"},
                {"gitUrl": "https://x/demo.git", "_registry": "someone-else"},
                {"gitUrl": "https://x/demo.git", "_registry": "mine", "hit": True},
            ],
        )
        entry = registry._pinned_registry_entry(
            "demo", {"sourceUrl": "https://x/demo.git", "sourceRegistry": "mine"}
        )
        assert entry is not None and entry.get("hit") is True

    def test_pinned_entry_returns_none_when_the_source_is_gone(self, monkeypatch):
        monkeypatch.setattr(
            registry,
            "_registry_app_candidates",
            lambda name: [{"gitUrl": "https://x/other.git"}],
        )
        assert (
            registry._pinned_registry_entry("demo", {"sourceUrl": "https://x/demo.git"})
            is None
        )

    def test_bundled_candidate_matches_the_bundled_source(self, monkeypatch):
        monkeypatch.setattr(
            registry,
            "_registry_app_candidates",
            lambda name: [{"gitUrl": "https://x/demo.git"}],
        )
        entry = registry._pinned_registry_entry("demo", {"sourceUrl": "https://x/demo.git"})
        assert entry == {"gitUrl": "https://x/demo.git"}


class TestResolveInstallEntry:
    def test_record_without_provenance_keeps_first_match_wins(self, monkeypatch):
        monkeypatch.setattr(registry, "get_app", lambda name: {"name": "demo"})
        monkeypatch.setattr(registry, "get_registry_app", lambda name: {"name": "demo"})
        entry, err = registry._resolve_install_entry("demo")
        assert err == ""
        assert entry == {"name": "demo"}

    def test_fresh_install_uses_the_bare_name_lookup(self, monkeypatch):
        monkeypatch.setattr(registry, "get_app", lambda name: None)
        monkeypatch.setattr(registry, "get_registry_app", lambda name: {"name": "demo"})
        entry, err = registry._resolve_install_entry("demo")
        assert (entry, err) == ({"name": "demo"}, "")

    def test_pinned_record_resolves_to_its_own_source(self, monkeypatch):
        monkeypatch.setattr(
            registry, "get_app", lambda name: {"sourceUrl": "https://x/demo.git"}
        )
        monkeypatch.setattr(
            registry, "_pinned_registry_entry", lambda name, meta: {"name": "demo"}
        )
        entry, err = registry._resolve_install_entry("demo")
        assert (entry, err) == ({"name": "demo"}, "")

    def test_missing_pinned_source_refuses_instead_of_falling_back(self, monkeypatch):
        monkeypatch.setattr(
            registry, "get_app", lambda name: {"sourceUrl": "https://x/demo.git"}
        )
        monkeypatch.setattr(registry, "_pinned_registry_entry", lambda name, meta: None)
        monkeypatch.setattr(
            registry,
            "get_registry_app",
            lambda name: pytest.fail("must not fall back to a bare-name lookup"),
        )
        entry, err = registry._resolve_install_entry("demo")
        assert entry is None
        assert "refusing to update it from a different source" in err


class TestRepoLookups:
    def test_bundled_repo_wins_before_external(self, monkeypatch):
        monkeypatch.setattr(
            registry, "_load_registry_file", lambda: [{"name": "demo", "repo": "o/demo"}]
        )
        assert registry.get_registry_app_by_repo("o/demo") == {
            "name": "demo",
            "repo": "o/demo",
        }

    def test_external_repo_is_resolved_from_the_sync_cache(self, monkeypatch, cache_dir):
        monkeypatch.setattr(registry, "_load_registry_file", list)
        _config_with(monkeypatch, [_reg("mine", "https://gitea.internal/idx.git")])
        registry._write_external_registry_cache(
            "mine", [{"name": "demo", "repo": "ext/demo", "branch": "trunk"}]
        )
        assert registry.get_registry_app_by_repo("ext/demo")["branch"] == "trunk"

    def test_unknown_repo_resolves_to_none(self, monkeypatch, cache_dir):
        monkeypatch.setattr(registry, "_load_registry_file", list)
        _config_with(monkeypatch, [])
        assert registry.get_registry_app_by_repo("nope/nope") is None

    def test_external_lookup_fails_open_on_a_config_error(self, monkeypatch):
        def _boom(cls):
            raise RuntimeError("config exploded")

        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", classmethod(_boom)
        )
        assert registry._external_registry_app_by_repo("ext/demo") is None
        assert registry._external_registry_repos() == set()

    def test_known_repos_union_bundled_and_external(self, monkeypatch, cache_dir):
        monkeypatch.setattr(
            registry, "_load_registry_file", lambda: [{"name": "a", "repo": "core/a"}]
        )
        _config_with(monkeypatch, [_reg("mine", "https://gitea.internal/idx.git")])
        registry._write_external_registry_cache(
            "mine", [{"name": "b", "repo": "ext/b"}, {"name": "c"}]
        )
        assert registry.known_registry_repos() == {"core/a", "ext/b"}


class TestSourceStrings:
    def test_is_registry_source(self):
        assert registry.is_registry_source("registry:demo") is True
        assert registry.is_registry_source("git:demo") is False

    def test_registry_name_from_source(self):
        assert registry.registry_name_from_source("registry:demo") == "demo"

    def test_app_source_dir_is_under_app_sources(self, monkeypatch, tmp_path):
        monkeypatch.setattr(registry, "config_dir", lambda: tmp_path)
        assert registry.app_source_dir("demo") == tmp_path / "app-sources" / "demo"


class TestServerPlatform:
    def test_reports_os_and_arch(self):
        info = registry.get_server_platform()
        assert set(info) == {"os", "arch"}
        assert info["os"] and info["arch"]


# ---------------------------------------------------------------------------
# Git provenance reader
# ---------------------------------------------------------------------------


class TestResolvedCloneCommit:
    _SHA = "a" * 40

    def test_missing_head_yields_no_commit(self, tmp_path):
        assert registry._resolved_clone_commit(tmp_path) == ""

    def test_detached_head_holds_the_sha_directly(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text(self._SHA + "\n", encoding="utf-8")
        assert registry._resolved_clone_commit(tmp_path) == self._SHA

    def test_detached_head_with_a_non_sha_is_rejected(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("garbage", encoding="utf-8")
        assert registry._resolved_clone_commit(tmp_path) == ""

    def test_loose_ref_is_read(self, tmp_path):
        git = tmp_path / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "refs" / "heads" / "main").write_text(self._SHA + "\n", encoding="utf-8")
        assert registry._resolved_clone_commit(tmp_path) == self._SHA

    def test_packed_refs_fallback_for_a_repacked_clone(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "packed-refs").write_text(
            f"# pack-refs with: peeled\n{self._SHA} refs/heads/main\n", encoding="utf-8"
        )
        assert registry._resolved_clone_commit(tmp_path) == self._SHA

    @pytest.mark.parametrize("ref", ["/etc/passwd", "../../escape", ""])
    def test_a_ref_that_could_escape_the_git_dir_is_refused(self, tmp_path, ref):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text(f"ref: {ref}\n", encoding="utf-8")
        assert registry._resolved_clone_commit(tmp_path) == ""

    def test_unresolvable_ref_degrades_to_no_commit(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        assert registry._resolved_clone_commit(tmp_path) == ""

    def test_short_sha_in_a_loose_ref_is_not_accepted(self, tmp_path):
        git = tmp_path / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "refs" / "heads" / "main").write_text("abc123\n", encoding="utf-8")
        assert registry._resolved_clone_commit(tmp_path) == ""


class TestReadCloneBranch:
    def test_missing_head_yields_none(self, tmp_path):
        assert registry._read_clone_branch(tmp_path) is None

    def test_branch_checkout_is_read(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/release/1.x\n", encoding="utf-8")
        assert registry._read_clone_branch(tmp_path) == "release/1.x"

    def test_detached_head_fails_closed(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("a" * 40, encoding="utf-8")
        assert registry._read_clone_branch(tmp_path) is None

    def test_undecodable_head_fails_closed(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_bytes(b"\xff\xfe not utf8")
        assert registry._read_clone_branch(tmp_path) is None

    @pytest.mark.asyncio
    async def test_branch_matches_requires_a_non_empty_branch(self, tmp_path):
        assert await registry._clone_branch_matches(tmp_path, "") is False

    @pytest.mark.asyncio
    async def test_branch_matches_compares_exactly(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        assert await registry._clone_branch_matches(tmp_path, "main") is True
        assert await registry._clone_branch_matches(tmp_path, "mainline") is False

    @pytest.mark.asyncio
    async def test_origin_matches_requires_a_url_to_compare(self, tmp_path):
        assert await registry._clone_origin_matches(tmp_path, "") is False

    @pytest.mark.asyncio
    async def test_origin_matches_is_byte_identical(self, tmp_path, monkeypatch):
        async def _origin(dest):
            return "https://github.com/o/demo.git"

        monkeypatch.setattr(registry, "_clone_origin_url", _origin)
        assert (
            await registry._clone_origin_matches(tmp_path, "https://github.com/o/demo.git")
            is True
        )
        assert (
            await registry._clone_origin_matches(tmp_path, "https://github.com/o/demo")
            is False
        )


class TestCloneOriginUrl:
    @pytest.mark.asyncio
    async def test_non_git_directory_yields_none(self, tmp_path):
        assert await registry._clone_origin_url(tmp_path) is None

    @pytest.mark.asyncio
    async def test_origin_is_returned_stripped(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        _fake_sandbox(
            monkeypatch, [_FakeProc(returncode=0, output=b"https://github.com/o/demo.git\n")]
        )
        assert await registry._clone_origin_url(tmp_path) == "https://github.com/o/demo.git"

    @pytest.mark.asyncio
    async def test_failed_git_yields_none(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=1)])
        assert await registry._clone_origin_url(tmp_path) is None

    @pytest.mark.asyncio
    async def test_spawn_failure_yields_none(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()

        async def _boom(*argv, **kwargs):
            raise OSError("no git binary")

        monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (list(cmd), None))
        monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: list(cmd))
        monkeypatch.setattr(registry, "create_subprocess_limited", _boom)
        assert await registry._clone_origin_url(tmp_path) is None

    @pytest.mark.asyncio
    async def test_timeout_kills_the_group_and_yields_none(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()

        class _Hang(_FakeProc):
            async def communicate(self):
                await asyncio.sleep(30)
                return b"", b""

        _fake_sandbox(monkeypatch, [_Hang()])
        killed: list[int] = []

        async def _kill(proc):
            killed.append(proc.pid)

        monkeypatch.setattr(registry, "_kill_process_group", _kill)
        monkeypatch.setattr(registry.asyncio, "wait_for", _immediate_timeout)
        assert await registry._clone_origin_url(tmp_path) is None
        assert killed == [31337]


async def _immediate_timeout(awaitable, timeout=None):
    """Stand-in for ``asyncio.wait_for`` that times out without waiting.

    The awaitable is closed so no "never awaited" warning escapes.
    """
    awaitable.close()
    raise asyncio.TimeoutError


# ---------------------------------------------------------------------------
# Stale checkout sweep
# ---------------------------------------------------------------------------


class TestStaleCheckoutSweep:
    @staticmethod
    def _aged(path: Path) -> None:
        past = time.time() - (registry._STALE_CHECKOUT_RETENTION_DAYS + 1) * 86400
        os.utime(path, (past, past))

    def test_missing_sources_dir_is_a_no_op(self, tmp_path):
        assert registry._sweep_stale_checkouts_sync(tmp_path / "absent", time.time()) == []

    def test_aged_stale_and_partial_dirs_are_removed(self, tmp_path):
        for name in ("demo.stale-0123abcd", "demo.partial-89abcdef"):
            d = tmp_path / name
            d.mkdir()
            (d / "file.txt").write_text("x", encoding="utf-8")
            self._aged(d)
        removed = registry._sweep_stale_checkouts_sync(tmp_path, time.time())
        assert sorted(removed) == ["demo.partial-89abcdef", "demo.stale-0123abcd"]

    def test_fresh_stale_dir_is_kept(self, tmp_path):
        d = tmp_path / "demo.stale-0123abcd"
        d.mkdir()
        assert registry._sweep_stale_checkouts_sync(tmp_path, time.time()) == []
        assert d.is_dir()

    @pytest.mark.parametrize(
        "name", ["demo", "demo.stale-xyz", "demo.stale-0123abc", ".stale-0123abcd"]
    )
    def test_names_outside_the_convention_are_never_touched(self, tmp_path, name):
        d = tmp_path / name
        d.mkdir()
        self._aged(d)
        assert registry._sweep_stale_checkouts_sync(tmp_path, time.time()) == []
        assert d.is_dir()

    def test_unlistable_sources_dir_degrades_to_no_removals(self, tmp_path, monkeypatch):
        def _boom(self):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "iterdir", _boom)
        assert registry._sweep_stale_checkouts_sync(tmp_path, time.time()) == []

    @requires_symlinks
    def test_symlink_pointing_outside_is_not_followed(self, tmp_path):
        sources = tmp_path / "app-sources"
        sources.mkdir()
        outside = tmp_path / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        link = sources / "demo.stale-0123abcd"
        os.symlink(str(outside), str(link))
        self._aged(outside)
        assert registry._sweep_stale_checkouts_sync(sources, time.time()) == []
        assert (outside / "keep.txt").is_file()

    @requires_symlinks
    def test_dangling_symlink_is_skipped_rather_than_deleted(self, tmp_path):
        link = tmp_path / "demo.stale-0123abcd"
        os.symlink(str(tmp_path / "gone"), str(link))
        assert registry._sweep_stale_checkouts_sync(tmp_path, time.time()) == []

    @pytest.mark.asyncio
    async def test_async_sweep_reports_removals(self, tmp_path, monkeypatch):
        monkeypatch.setattr(registry, "_app_sources_dir", lambda: tmp_path)
        d = tmp_path / "demo.stale-0123abcd"
        d.mkdir()
        self._aged(d)
        await registry._sweep_stale_checkouts()
        assert not d.exists()

    @pytest.mark.asyncio
    async def test_async_sweep_never_fails_the_install(self, tmp_path, monkeypatch):
        monkeypatch.setattr(registry, "_app_sources_dir", lambda: tmp_path)

        def _boom(sources_dir, now_ts):
            raise RuntimeError("sweep exploded")

        monkeypatch.setattr(registry, "_sweep_stale_checkouts_sync", _boom)
        await registry._sweep_stale_checkouts()  # must not raise

    def test_is_stale_candidate(self, tmp_path):
        assert registry._is_stale_candidate(tmp_path / "a.stale-0123abcd") is True
        assert registry._is_stale_candidate(tmp_path / "a.partial-0123abcd") is True
        assert registry._is_stale_candidate(tmp_path / "a") is False


# ---------------------------------------------------------------------------
# Process-group kill
# ---------------------------------------------------------------------------


class TestKillProcessGroup:
    @pytest.mark.asyncio
    async def test_sigterm_then_reap_is_enough_for_a_cooperative_child(self, monkeypatch):
        signals: list[object] = []

        async def _tree_kill(pid, sig):
            signals.append(sig)
            return True

        monkeypatch.setattr(registry.platform_compat, "kill_process_tree_async", _tree_kill)
        proc = _FakeProc(returncode=0)
        await registry._kill_process_group(proc)
        assert signals == [registry.platform_compat.SIGTERM]
        assert proc.wait_calls == 1
        assert proc.kill_calls == 0

    @pytest.mark.asyncio
    async def test_sigterm_os_error_still_reaps_the_child(self, monkeypatch):
        """A child that already exited makes killpg raise; the reap must still run."""

        async def _tree_kill(pid, sig):
            raise OSError("no such process")

        monkeypatch.setattr(registry.platform_compat, "kill_process_tree_async", _tree_kill)
        proc = _FakeProc(returncode=0)
        await registry._kill_process_group(proc)
        assert proc.wait_calls == 1
        assert proc.kill_calls == 0

    @pytest.mark.asyncio
    async def test_failed_sigkill_falls_back_to_a_pid_scoped_kill(self, monkeypatch):
        """If the group SIGKILL cannot be delivered the child is never left unreaped."""
        sent: list[object] = []

        async def _tree_kill(pid, sig):
            sent.append(sig)
            if sig == registry.platform_compat.SIGKILL:
                raise OSError("not a group leader")
            return True

        monkeypatch.setattr(registry.platform_compat, "kill_process_tree_async", _tree_kill)
        monkeypatch.setattr(registry, "_KILL_GRACE_PERIOD", 0.01)

        class _Stubborn(_FakeProc):
            def __init__(self) -> None:
                super().__init__(returncode=0)
                self._reaped = False

            async def wait(self) -> int:
                self.wait_calls += 1
                if not self._reaped:
                    self._reaped = True
                    await asyncio.sleep(5)
                return 0

        proc = _Stubborn()
        await registry._kill_process_group(proc)
        assert sent == [
            registry.platform_compat.SIGTERM,
            registry.platform_compat.SIGKILL,
        ]
        assert proc.kill_calls == 1

    @pytest.mark.asyncio
    async def test_an_unresponsive_child_is_escalated_to_sigkill(self, monkeypatch):
        signals: list[object] = []

        async def _tree_kill(pid, sig):
            signals.append(sig)
            return True

        monkeypatch.setattr(registry.platform_compat, "kill_process_tree_async", _tree_kill)
        monkeypatch.setattr(registry, "_KILL_GRACE_PERIOD", 0.01)

        class _Stubborn(_FakeProc):
            def __init__(self) -> None:
                super().__init__(returncode=0)
                self._reaped = False

            async def wait(self) -> int:
                self.wait_calls += 1
                if not self._reaped:
                    self._reaped = True
                    await asyncio.sleep(5)
                return 0

        proc = _Stubborn()
        await registry._kill_process_group(proc)
        assert signals == [
            registry.platform_compat.SIGTERM,
            registry.platform_compat.SIGKILL,
        ]


# ---------------------------------------------------------------------------
# Build step selection + execution
# ---------------------------------------------------------------------------


class TestRunAppBuild:
    @pytest.mark.asyncio
    async def test_no_recognized_ecosystem_means_no_build(self, tmp_path):
        log: list[str] = []
        result = await registry._run_app_build(tmp_path, "demo", log)
        assert result == {"ok": True}
        assert "No build step detected — using source as-is" in log

    @pytest.mark.asyncio
    async def test_missing_npm_is_a_soft_skip(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(registry.shutil, "which", lambda name: None)
        log: list[str] = []
        result = await registry._run_app_build(tmp_path, "demo", log)
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
        assert await registry._run_app_build(tmp_path, "demo", log) == {"ok": True}
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
        assert await registry._run_app_build(tmp_path, "demo", []) == {"ok": True}
        assert spawned == [
            ["/usr/bin/npm", "install"],
            ["/usr/bin/npm", "run", "build"],
        ]

    @pytest.mark.asyncio
    async def test_unparseable_package_json_still_installs(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text("{ not json", encoding="utf-8")
        monkeypatch.setattr(registry.shutil, "which", lambda name: "/usr/bin/npm")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        assert await registry._run_app_build(tmp_path, "demo", []) == {"ok": True}
        assert spawned == [["/usr/bin/npm", "install"]]

    @pytest.mark.asyncio
    async def test_requirements_only_uses_the_requirements_file(self, tmp_path, monkeypatch):
        (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        assert await registry._run_app_build(tmp_path, "demo", []) == {"ok": True}
        assert spawned == [[sys.executable, "-m", "pip", "install", "-r", "requirements.txt"]]

    @pytest.mark.asyncio
    async def test_pyproject_installs_the_project(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        assert await registry._run_app_build(tmp_path, "demo", []) == {"ok": True}
        assert spawned == [[sys.executable, "-m", "pip", "install", "."]]

    @pytest.mark.asyncio
    async def test_setup_py_installs_the_project(self, tmp_path, monkeypatch):
        (tmp_path / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        assert await registry._run_app_build(tmp_path, "demo", []) == {"ok": True}
        assert spawned == [[sys.executable, "-m", "pip", "install", "."]]

    @pytest.mark.asyncio
    async def test_missing_path_pip_does_not_skip_the_python_build(self, tmp_path, monkeypatch):
        """The Python build runs via ``sys.executable -m pip`` — the gateway's own
        interpreter — so a host with no pip anywhere on PATH must still build."""
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        monkeypatch.setattr(registry.shutil, "which", lambda name: None)
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        assert await registry._run_app_build(tmp_path, "demo", []) == {"ok": True}
        assert spawned == [[sys.executable, "-m", "pip", "install", "."]]

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
        the tests in ``test_platform_compat.py`` pin its sentinel to the
        packaging layer so a bundler rename cannot silently un-match this guard.
        """
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        bundled = tmp_path / "App.app" / "Contents" / "Resources" / "backend-dist"
        bundled = bundled / "kirocrew-backend-arm64" / "bin" / "python3.12"
        bundled.parent.mkdir(parents=True, exist_ok=True)
        bundled.write_text("", encoding="utf-8")
        monkeypatch.setattr(registry.sys, "executable", str(bundled))
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        result = await registry._run_app_build(tmp_path, "demo", [])
        assert result["ok"] is False
        assert "bundled interpreter" in result["error"]
        assert spawned == []

    @pytest.mark.asyncio
    async def test_build_output_is_streamed_into_the_log(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(registry.shutil, "which", lambda name: "/usr/bin/npm")
        _fake_sandbox(
            monkeypatch,
            [_FakeProc(returncode=0, stdout_lines=[b"added 1 package\n", b"done\n"])],
        )
        log: list[str] = []
        await registry._run_app_build(tmp_path, "demo", log)
        assert "added 1 package" in log and "done" in log

    @pytest.mark.asyncio
    async def test_nonzero_exit_fails_the_build(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(registry.shutil, "which", lambda name: "/usr/bin/npm")
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=7)])
        result = await registry._run_app_build(tmp_path, "demo", [])
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
        result = await registry._run_app_build(tmp_path, "demo", [])
        assert result["ok"] is False
        assert "build timed out" in result["error"]
        assert killed == [31337]


# ---------------------------------------------------------------------------
# Post-rejection un-poison
# ---------------------------------------------------------------------------


class TestUnpoisonRejectedCheckout:
    @pytest.mark.asyncio
    async def test_fresh_checkout_is_deleted_without_residue(self, tmp_path):
        pkg = tmp_path / "demo"
        pkg.mkdir()
        (pkg / "app.json").write_text("{}", encoding="utf-8")
        log: list[str] = []
        await registry._unpoison_rejected_checkout(
            "demo", pkg, log, checkout_preexisted=False, pre_pull_commit=""
        )
        assert not pkg.exists()

    @pytest.mark.asyncio
    async def test_previous_checkout_is_restored_into_the_slot(self, tmp_path):
        pkg = tmp_path / "demo"
        pkg.mkdir()
        stale = tmp_path / "demo.stale-0123abcd"
        stale.mkdir()
        (stale / "mine.txt").write_text("local edit", encoding="utf-8")
        log: list[str] = []
        await registry._unpoison_rejected_checkout(
            "demo",
            pkg,
            log,
            checkout_preexisted=False,
            pre_pull_commit="",
            restore_from=stale,
        )
        assert (pkg / "mine.txt").read_text(encoding="utf-8") == "local edit"
        assert any("Restored the previous checkout" in line for line in log)

    @pytest.mark.asyncio
    async def test_failed_restore_tells_the_user_where_the_files_are(
        self, tmp_path, monkeypatch
    ):
        pkg = tmp_path / "demo"
        pkg.mkdir()
        stale = tmp_path / "demo.stale-0123abcd"
        stale.mkdir()

        def _boom(self, target):
            raise OSError("locked")

        monkeypatch.setattr(Path, "rename", _boom)
        log: list[str] = []
        await registry._unpoison_rejected_checkout(
            "demo",
            pkg,
            log,
            checkout_preexisted=False,
            pre_pull_commit="",
            restore_from=stale,
        )
        assert any("retained there for manual recovery" in line for line in log)

    @pytest.mark.asyncio
    async def test_preexisting_checkout_is_rolled_back_to_its_pre_pull_commit(
        self, tmp_path, monkeypatch
    ):
        pkg = tmp_path / "demo"
        pkg.mkdir()
        spawned = _fake_sandbox(
            monkeypatch, [_FakeProc(returncode=0), _FakeProc(returncode=0)]
        )
        log: list[str] = []
        await registry._unpoison_rejected_checkout(
            "demo", pkg, log, checkout_preexisted=True, pre_pull_commit="b" * 40
        )
        assert spawned[0] == ["git", "reset", "--keep", "b" * 40]
        assert spawned[1] == ["git", "--literal-pathspecs", "checkout", "--", "app.json"]
        assert pkg.is_dir()  # the workspace is preserved
        assert any("Rolled checkout back to pre-update commit" in line for line in log)

    @pytest.mark.asyncio
    async def test_failed_rollback_and_restore_both_warn(self, tmp_path, monkeypatch):
        pkg = tmp_path / "demo"
        pkg.mkdir()
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=1), _FakeProc(returncode=1)])
        log: list[str] = []
        await registry._unpoison_rejected_checkout(
            "demo", pkg, log, checkout_preexisted=True, pre_pull_commit="c" * 40
        )
        assert any("could not roll the checkout back" in line for line in log)
        assert any("could not restore app.json" in line for line in log)

    @pytest.mark.asyncio
    async def test_manifest_snapshot_restores_exact_pre_update_bytes(
        self, tmp_path, monkeypatch
    ):
        pkg = tmp_path / "demo"
        pkg.mkdir()
        (pkg / "app.json").write_text('{"name": "evil"}', encoding="utf-8")
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        log: list[str] = []
        await registry._unpoison_rejected_checkout(
            "demo",
            pkg,
            log,
            checkout_preexisted=True,
            pre_pull_commit="",
            manifest_snapshot=b'{"name": "demo"}',
        )
        assert (pkg / "app.json").read_bytes() == b'{"name": "demo"}'
        assert any("exact pre-update contents" in line for line in log)

    @pytest.mark.asyncio
    async def test_a_sandbox_failure_never_masks_the_refusal(self, tmp_path, monkeypatch):
        pkg = tmp_path / "demo"
        pkg.mkdir()

        def _boom(cmd, mode=""):
            raise RuntimeError("sandbox unavailable")

        monkeypatch.setattr(registry, "wrap_argv", _boom)
        await registry._unpoison_rejected_checkout(
            "demo", pkg, [], checkout_preexisted=True, pre_pull_commit="d" * 40
        )  # must not raise

    @pytest.mark.asyncio
    async def test_custom_manifest_relpath_is_used(self, tmp_path, monkeypatch):
        pkg = tmp_path / "demo"
        (pkg / "sub").mkdir(parents=True)
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        await registry._unpoison_rejected_checkout(
            "demo",
            pkg,
            [],
            checkout_preexisted=True,
            pre_pull_commit="",
            manifest_relpath="sub/app.json",
            manifest_snapshot=b"{}",
        )
        assert (pkg / "sub" / "app.json").read_bytes() == b"{}"


# ---------------------------------------------------------------------------
# install_from_registry — pre-clone refusals
# ---------------------------------------------------------------------------


class TestInstallFromRegistryRefusals:
    @pytest.fixture(autouse=True)
    def _no_side_effects(self, monkeypatch, tmp_path):
        """Every test here must return before any clone/build/registration.

        ``app-sources`` is redirected at *tmp_path* as a belt-and-braces guard:
        the refusals all return before the stale-checkout sweep, and this makes
        a future regression fail loudly in the sandbox instead of quietly
        touching the real Kiro Crew home.
        """
        monkeypatch.setattr(registry, "sel", lambda: MagicMock())
        monkeypatch.setattr(registry, "_app_sources_dir", lambda: tmp_path / "app-sources")

        async def _never_clone(*a, **k):
            raise AssertionError("install must refuse before cloning")

        monkeypatch.setattr(registry, "_clone_build_app", _never_clone)

    @pytest.mark.asyncio
    async def test_provenance_mismatch_is_refused_and_audited(self, monkeypatch):
        monkeypatch.setattr(
            registry, "_resolve_install_entry", lambda name: (None, "pinned elsewhere")
        )
        result = await registry.install_from_registry("demo")
        assert result == {"ok": False, "name": "demo", "error": "pinned elsewhere"}

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_mask_the_refusal(self, monkeypatch):
        monkeypatch.setattr(
            registry, "_resolve_install_entry", lambda name: (None, "pinned elsewhere")
        )
        broken = MagicMock()
        broken.log_api_access.side_effect = RuntimeError("sel down")
        monkeypatch.setattr(registry, "sel", lambda: broken)
        result = await registry.install_from_registry("demo")
        assert result["error"] == "pinned elsewhere"

    @pytest.mark.asyncio
    async def test_unknown_app_is_reported_as_not_found(self, monkeypatch):
        monkeypatch.setattr(registry, "_resolve_install_entry", lambda name: (None, ""))
        result = await registry.install_from_registry("demo")
        assert result == {"ok": False, "error": "app 'demo' not found in registry"}

    @pytest.mark.asyncio
    async def test_entry_without_a_git_url_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            registry, "_resolve_install_entry", lambda name: ({"name": "demo"}, "")
        )
        result = await registry.install_from_registry("demo")
        assert result == {"ok": False, "error": "app 'demo' has no git URL configured"}

    @pytest.mark.asyncio
    async def test_admission_denial_stops_before_the_clone(self, monkeypatch):
        monkeypatch.setattr(
            registry,
            "_resolve_install_entry",
            lambda name: ({"name": "demo", "gitUrl": "https://github.com/o/demo.git"}, ""),
        )

        async def _manifest(*a, **k):
            return {"name": "demo"}

        monkeypatch.setattr(registry, "_fetch_app_manifest", _manifest)
        monkeypatch.setattr(registry, "get_app", lambda name: None)
        monkeypatch.setattr(
            registry, "app_admission_denied", lambda name, manifest=None, action="": "banned"
        )
        result = await registry.install_from_registry("demo")
        assert result["ok"] is False
        assert "blocked by admission policy: banned" in result["error"]

    @pytest.mark.asyncio
    async def test_client_only_app_asks_for_a_local_install(self, monkeypatch):
        monkeypatch.setattr(
            registry,
            "_resolve_install_entry",
            lambda name: ({"name": "demo", "gitUrl": "https://github.com/o/demo.git"}, ""),
        )

        async def _manifest(*a, **k):
            # A platform this host is not: "haiku" is never sys.platform.
            return {
                "name": "demo",
                "platform": {
                    "os": ["haiku"],
                    "installMode": "client",
                    "clientInstall": {"cmd": "brew install demo"},
                },
            }

        monkeypatch.setattr(registry, "_fetch_app_manifest", _manifest)
        monkeypatch.setattr(registry, "get_app", lambda name: None)
        monkeypatch.setattr(
            registry, "app_admission_denied", lambda name, manifest=None, action="": None
        )
        result = await registry.install_from_registry("demo")
        assert result["needsClientInstall"] is True
        assert result["clientInstall"] == {"cmd": "brew install demo"}
        assert result["platform"]["required"] == ["haiku"]

    @pytest.mark.asyncio
    async def test_min_version_gate_refuses_an_old_gateway(self, monkeypatch):
        monkeypatch.setattr(
            registry,
            "_resolve_install_entry",
            lambda name: ({"name": "demo", "gitUrl": "https://github.com/o/demo.git"}, ""),
        )

        async def _manifest(*a, **k):
            return {"name": "demo", "minKiroCrewVersion": "999.0.0"}

        monkeypatch.setattr(registry, "_fetch_app_manifest", _manifest)
        monkeypatch.setattr(registry, "get_app", lambda name: None)
        monkeypatch.setattr(
            registry, "app_admission_denied", lambda name, manifest=None, action="": None
        )
        monkeypatch.setattr(
            "kiro_crew.apps.version.check_min_version", lambda mv: "needs 999.0.0"
        )
        result = await registry.install_from_registry("demo")
        assert result == {"ok": False, "name": "demo", "error": "needs 999.0.0"}

    @pytest.mark.asyncio
    async def test_execution_denial_carries_the_consent_error_code(self, monkeypatch):
        monkeypatch.setattr(
            registry,
            "_resolve_install_entry",
            lambda name: ({"name": "demo", "gitUrl": "https://github.com/o/demo.git"}, ""),
        )

        async def _manifest(*a, **k):
            return {"name": "demo"}

        monkeypatch.setattr(registry, "_fetch_app_manifest", _manifest)
        monkeypatch.setattr(registry, "get_app", lambda name: None)
        monkeypatch.setattr(
            registry, "app_admission_denied", lambda name, manifest=None, action="": None
        )
        monkeypatch.setattr(
            registry,
            "app_execution_denied",
            lambda name, action="", caller="": "needs a trust grant",
        )
        result = await registry.install_from_registry("demo")
        assert result["ok"] is False
        assert result["code"] == "app_execution_denied"
        assert "needs a trust grant" in result["error"]
