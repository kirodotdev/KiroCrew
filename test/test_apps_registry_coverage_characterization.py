"""Characterization of ``kiro_crew.apps.registry``: behaviour a structural move of the
module must not change.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from apps_registry_coverage_helpers import _explicit_registry_execution_admission  # noqa: F401
from apps_registry_coverage_helpers import cache_dir_fixture  # noqa: F401

from kiro_crew.apps import registry
from kiro_crew.testing.wait import finished

# ---------------------------------------------------------------------------
# Characterization: behaviour a structural move of this module must not change
# ---------------------------------------------------------------------------
#
# Each case below pins a value the registry derives from where its code lives
# (``__file__``), a byte-exact cache identity, a fixed classification, or a
# module-level seam that one function reads while another defines it. Every case
# goes through ``kiro_crew.apps.registry`` only, so it answers the same way against
# the one-module registry and against the split one.


class TestTheBundledSeedIsTheWheelsOwnFile:
    def test_the_seed_path_sits_beside_the_registry_module(self):
        # The seed ships as ``kiro_crew/apps/app-registry.json``; a module that
        # derived the path from its OWN ``__file__`` after moving would look in the
        # wrong directory and silently list no seed rows at all.
        assert registry._REGISTRY_FILE == Path(registry.__file__).parent / "app-registry.json"
        assert registry._REGISTRY_FILE.is_file()

    def test_the_unpatched_loader_returns_the_bundled_rows(self, monkeypatch):
        monkeypatch.setattr(registry, "_edition_registry_rows", lambda: [])
        bundled = json.loads(registry._REGISTRY_FILE.read_text(encoding="utf-8"))
        assert bundled, "the bundled seed is expected to list at least one app"
        assert registry._load_registry_file() == bundled


class TestCacheFilenamesAreFrozen:
    """Byte-exact cache file names: a renamed file is a silent cache miss for every
    operator, and the legacy name is what the migration must still find."""

    _ROOT = Path("/registry-cache")

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the class's own cache-root pin, moved verbatim; no test in the class calls monkeypatch.undo()
    def _root(self, monkeypatch):
        monkeypatch.setattr(registry, "_manifest_cache_dir", lambda: self._ROOT)

    def test_a_manifest_file_is_keyed_by_the_full_normalized_coordinates(self):
        entry = {
            "name": "demo-app",
            "gitUrl": "https://user:tok@GitHub.com/Acme/Demo.git/",
            "branch": "dev",
            "commit": "abc",
            "subdirectory": "apps/demo",
        }
        assert registry._manifest_source_coordinates(entry) == (
            "https://github.com/Acme/Demo",
            "branch:dev|commit:abc",
            "apps/demo",
            "demo-app",
        )
        assert registry._manifest_cache_path(entry) == (
            self._ROOT / "by-source" / "demo-app-a8369162f9698005.json"
        )

    def test_a_traversing_app_name_is_slugged_and_disambiguated(self):
        entry = {"name": "../../victim", "repo": "git@github.com:acme/x.git"}
        assert registry._manifest_cache_path(entry) == (
            self._ROOT / "by-source" / "victim-e2c35920-2adab573cf8d478f.json"
        )

    def test_an_index_cache_is_keyed_by_the_credential_free_source_identity(self):
        reg = SimpleNamespace(
            name="", repo="https://user:tok@forge.example.com/Org/Registry.git", branch="main"
        )
        identity = registry._external_registry_cache_identity(reg)
        assert identity == (
            "https://forge.example.com/Org/Registry.git|https://forge.example.com/Org/Registry|main"
        )
        assert registry._external_registry_cache_path(identity) == self._ROOT / (
            "_registry_https-forge-example-com-Org-Registry-git-https-forge-example-com-"
            "Org-Registry-main-4c88372e47c939563a95031708fea521cb97e46ce6ba87b79a6ce0c70dc83d25"
            ".json"
        )
        # The pre-hardening name the migration removes, byte-identical to what an
        # older release wrote -- userinfo and all.
        assert registry._legacy_external_registry_cache_path(reg.repo) == self._ROOT / (
            "_registry_https-user-tok-forge-example-com-Org-Registry-git-198629fa.json"
        )

    def test_a_named_registry_with_no_branch_and_a_bare_name(self):
        reg = SimpleNamespace(name="acme", repo="https://github.com/acme/registry", branch="")
        identity = registry._external_registry_cache_identity(reg)
        assert identity == "acme|https://github.com/acme/registry|"
        assert registry._external_registry_cache_path(identity) == self._ROOT / (
            "_registry_acme-https-github-com-acme-registry-"
            "a55b09c7018d33ab8793141f2d1304c39b7eba3ad79e2aa6bceafad6b9eda1e8.json"
        )
        assert registry._external_registry_cache_path("acme") == self._ROOT / "_registry_acme.json"

    def test_the_registry_identity_key_folds_case_through_the_cache_form(self):
        assert registry._registry_identity_key("Official") == "_registry_official.json"
        assert registry._registry_identity_key("official") == "_registry_official.json"
        assert registry._registry_identity_key("https://x.example/Org/Reg.git") == (
            "_registry_https-x-example-org-reg-git-"
            "d7cc655baa96264085c83e9f0dbea7b3b285af3d7c9bc22545afb87d6fcc960e.json"
        )


class TestCredentialedGitOutputIsReducedToFixedClasses:
    @pytest.mark.parametrize(
        ("text", "credentialed", "expected"),
        [
            (
                "fatal: Authentication failed for 'https://host/x'",
                True,
                "git authentication failed (credentialed transport details redacted)",
            ),
            (
                # Auth-shaped wins over a recognizable failure class in the same text.
                "fatal: Authentication failed; Could not resolve host: x",
                True,
                "git authentication failed (credentialed transport details redacted)",
            ),
            (
                "fatal: unable to access: Could not resolve host: x",
                True,
                "git transport failed: host could not be resolved (details redacted)",
            ),
            (
                "remote said something new",
                True,
                "git transport output redacted (credentialed remote)",
            ),
            ("", True, ""),
            (
                "fatal: Authentication failed for raw text",
                False,
                "fatal: Authentication failed for raw text",
            ),
        ],
    )
    def test_the_decision_and_its_fixed_strings(self, text, credentialed, expected):
        assert registry._loggable_git_transport_output(text, credentialed=credentialed) == expected


class TestTheOneShotTransportEnv:
    _SAFE = "https://github.com/acme/app.git"
    _CRED = "https://user:tok@github.com/acme/app.git"

    def test_a_credential_free_target_returns_the_same_env_object(self):
        env = {"PATH": "/usr/bin"}
        assert registry._git_transport_env(self._SAFE, self._SAFE, env) is env
        assert registry._git_transport_env("", "", env) is env

    def test_a_target_that_does_not_strip_to_the_safe_one_is_refused(self):
        with pytest.raises(ValueError, match="does not match the safe clone target"):
            registry._git_transport_env(self._CRED, "https://github.com/acme/other.git", {})

    def test_an_unsupported_target_is_refused_before_anything_else(self):
        with pytest.raises(ValueError, match="unsupported query or fragment"):
            registry._git_transport_env(self._CRED + "?ref=x", self._SAFE, {})

    def test_embedded_credentials_need_an_http_transport(self):
        with pytest.raises(ValueError, match="require an HTTP\\(S\\) target"):
            registry._git_transport_env("ftp://user:pw@host/x", "ftp://host/x", {})

    @pytest.mark.parametrize(("inherited", "first"), [(None, 0), ("abc", 0), ("-3", 0), ("2", 2)])
    def test_the_command_config_is_appended_after_every_inherited_entry(self, inherited, first):
        env = {"PATH": "/usr/bin"}
        if inherited is not None:
            env["GIT_CONFIG_COUNT"] = inherited
        before = dict(env)
        out = registry._git_transport_env(self._CRED, self._SAFE, env)
        assert env == before, "the caller's mapping must not be mutated"
        expected = [
            (f"url.{self._CRED}.insteadOf", self._SAFE),
            ("core.fsmonitor", "false"),
            ("credential.helper", ""),
            ("core.askPass", ""),
            ("core.hooksPath", os.devnull),
        ]
        got = [
            (out[f"GIT_CONFIG_KEY_{first + i}"], out[f"GIT_CONFIG_VALUE_{first + i}"])
            for i in range(len(expected))
        ]
        assert got == expected
        assert out["GIT_CONFIG_COUNT"] == str(first + len(expected))


class _NeverExits:
    """A process double whose ``wait`` and ``communicate`` never return on their own.

    Its pid is above any ``pid_max``, so no signal can reach a real process even if a
    kill stub stopped reaching the code under test.
    """

    pid = 99999999999
    returncode = None

    async def wait(self):
        await asyncio.Event().wait()

    async def communicate(self):
        await asyncio.Event().wait()


class TestTimeoutsAreReadWhereTheyAreUsed:
    """A facade patch of a timeout reaches the function that waits on it."""

    @pytest.mark.asyncio
    async def test_the_kill_grace_bounds_the_wait_before_the_hard_kill(self, monkeypatch):
        reaped = []

        async def _tree_kill(pid, sig):
            return None

        async def _reap(proc):
            reaped.append(proc)

        monkeypatch.setattr(registry.platform_compat, "kill_process_tree_async", _tree_kill)
        monkeypatch.setattr(registry.platform_compat, "kill_and_reap", _reap)
        monkeypatch.setattr(registry, "_KILL_GRACE_PERIOD", 0)
        proc = _NeverExits()
        # Bounded well below the real 5 s grace, so an inert patch fails here.
        await asyncio.wait_for(
            registry._kill_process_group(proc),
            timeout=2,  # flake-ok: the 2 s bound IS the assertion; an inert _KILL_GRACE_PERIOD patch waits the real 5 s grace
        )
        assert reaped == [proc]

    @pytest.mark.asyncio
    async def test_the_clone_timeout_bounds_a_fetch_and_discards_the_destination(
        self, tmp_path, monkeypatch
    ):
        dest = tmp_path / "slot" / "demo-app"
        killed = []

        class _Done:
            pid = _NeverExits.pid
            returncode = 0

            async def communicate(self):
                return b"", None

        async def _wrap(argv, **kwargs):
            return list(argv), None

        async def _spawn(*argv, **kwargs):
            if argv[:2] == ("git", "init"):
                dest.mkdir(parents=True)
            if "fetch" in argv:
                return _NeverExits()
            return _Done()

        async def _kill(proc):
            killed.append(proc)

        async def _no_signal(*args, **kwargs):
            raise AssertionError("the group kill must go through the patched seam")

        monkeypatch.setattr(registry.platform_compat, "kill_process_tree_async", _no_signal)
        monkeypatch.setattr(registry.platform_compat, "kill_and_reap", _no_signal)
        monkeypatch.setattr(registry, "wrap_argv_async", _wrap)
        monkeypatch.setattr(registry, "cgroup_scope_argv", lambda argv: argv)
        monkeypatch.setattr(registry, "create_subprocess_limited", _spawn)
        monkeypatch.setattr(registry, "_kill_process_group", _kill)
        monkeypatch.setattr(registry, "_CLONE_TIMEOUT", 0.01)
        log: list[str] = []
        # Bounded well below the real 60 s clone timeout, so an inert patch fails here.
        result = await finished(
            registry._git_fetch_branch(
                "https://github.com/acme/demo-app.git",
                "main",
                dest,
                log,
                clone_env={},
                sandbox_mode="strict",
            ),
            "fetch cut off by the clone timeout",
            timeout=5,  # flake-ok: the 5 s bound IS the assertion; an inert _CLONE_TIMEOUT patch waits the real 60 s
        )
        assert result == {"ok": False, "name": "demo-app", "error": "git fetch failed (exit 124)"}
        assert len(killed) == 1 and isinstance(killed[0], _NeverExits)
        assert "timed out" in log
        assert not dest.exists(), "a destination this call created is discarded on failure"


class TestTheIndexCacheMigrationFailsClosed:
    def _legacy_file(self, cache_dir):
        path = registry._external_registry_cache_path("acme")
        rows = [{"name": "demo-app", "repo": "https://user:tok@github.com/acme/demo-app.git"}]
        path.write_text(json.dumps(rows), encoding="utf-8")
        old = time.time() - 30
        os.utime(path, (old, old))
        return path

    def test_a_credential_bearing_cache_is_rewritten_in_place_with_its_clock(self, cache_dir):
        path = self._legacy_file(cache_dir)
        before = path.stat()
        rows = registry._read_external_registry_cache("acme")
        assert rows == [{"name": "demo-app", "repo": "https://github.com/acme/demo-app.git"}]
        assert "tok" not in path.read_text(encoding="utf-8")
        assert path.stat().st_mtime_ns == before.st_mtime_ns

    def test_a_failed_rewrite_removes_the_credential_bearing_file(self, cache_dir, monkeypatch):
        path = self._legacy_file(cache_dir)

        def _refuse(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(registry, "atomic_write", _refuse)
        rows = registry._read_external_registry_cache("acme")
        # This read still answers sanitized; nothing credential-bearing stays on disk.
        assert rows == [{"name": "demo-app", "repo": "https://github.com/acme/demo-app.git"}]
        assert not path.exists()

    def test_a_clean_cache_is_not_rewritten(self, cache_dir, monkeypatch):
        path = registry._external_registry_cache_path("acme")
        path.write_text(json.dumps([{"name": "demo-app"}]), encoding="utf-8")

        def _never(*args, **kwargs):
            raise AssertionError("a clean cache must not be rewritten on read")

        monkeypatch.setattr(registry, "atomic_write", _never)
        assert registry._read_external_registry_cache("acme") == [{"name": "demo-app"}]


class TestInstallRowPrecedence:
    """Which row ``_resolve_registry_row`` answers with, and when it refuses."""

    SEED = {"name": "demo-app", "gitUrl": "https://github.com/acme/demo-app.git", "branch": "main"}
    # Same repository: host case, a trailing slash and ``.git`` are cosmetic.
    SAME = {"name": "demo-app", "gitUrl": "https://GitHub.com/acme/demo-app/", "commit": "a" * 40}
    OTHER = {
        "name": "demo-app",
        "gitUrl": "https://github.com/evil/demo-app.git",
        "commit": "b" * 40,
    }
    EXTERNAL = {"name": "demo-app", "gitUrl": "https://example.com/x.git", "_registry": "acme"}

    def _resolve(self, monkeypatch, *, seed, catalog):
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [dict(seed)] if seed else [])
        monkeypatch.setattr(registry, "_external_registry_row", lambda name: dict(self.EXTERNAL))

        def _catalog(name):
            if isinstance(catalog, BaseException):
                raise catalog
            return dict(catalog) if catalog else None

        monkeypatch.setattr(registry.official_catalog, "inventory_for_install", _catalog)
        return registry._resolve_registry_row("demo-app")

    def test_a_catalog_row_with_no_seed_answers(self, monkeypatch):
        assert self._resolve(monkeypatch, seed=None, catalog=self.OTHER) == (self.OTHER, "")

    def test_a_same_repository_catalog_row_supersedes_the_seed(self, monkeypatch):
        assert self._resolve(monkeypatch, seed=self.SEED, catalog=self.SAME) == (self.SAME, "")

    def test_a_different_repository_catalog_row_keeps_the_seed(self, monkeypatch):
        assert self._resolve(monkeypatch, seed=self.SEED, catalog=self.OTHER) == (self.SEED, "")

    def test_no_catalog_row_falls_back_to_the_seed_then_the_external_cache(self, monkeypatch):
        assert self._resolve(monkeypatch, seed=self.SEED, catalog=None) == (self.SEED, "")
        assert self._resolve(monkeypatch, seed=None, catalog=None) == (self.EXTERNAL, "")

    @pytest.mark.parametrize(
        ("seed", "error", "detail"),
        [
            (
                SEED,
                registry.official_catalog.CatalogUnavailable("down"),
                "is bundled and may carry",
            ),
            (None, RuntimeError("boom"), "may be an official catalog app"),
        ],
    )
    def test_a_failed_catalog_lookup_refuses_before_any_fallback(
        self, monkeypatch, seed, error, detail
    ):
        row, refusal = self._resolve(monkeypatch, seed=seed, catalog=error)
        assert row is None
        assert detail in refusal and "refusing to resolve it from another source" in refusal
        with pytest.raises(registry.official_catalog.CatalogUnavailable):
            registry.get_registry_app("demo-app")


class TestGitFetchAndPullFailClosed:
    """The fetch and pull paths refuse rather than install bytes they cannot vouch for."""

    class _Proc:
        def __init__(self, returncode: int = 0, output: bytes = b"") -> None:
            self.pid = _NeverExits.pid
            self.returncode = returncode
            self._output = output

        async def communicate(self):
            return self._output, None

    def _spawns(self, monkeypatch, plan):
        """Fake the sandbox and spawn seams; *plan* maps an argv prefix to a process."""
        spawned: list[tuple[str, ...]] = []

        async def _wrap(argv, **kwargs):
            return list(argv), None

        async def _spawn(*argv, **kwargs):
            spawned.append(tuple(argv))
            for prefix, make in plan:
                if argv[: len(prefix)] == prefix:
                    return make(argv, kwargs)
            return self._Proc()

        monkeypatch.setattr(registry, "wrap_argv_async", _wrap)
        monkeypatch.setattr(registry, "cgroup_scope_argv", lambda argv: argv)
        monkeypatch.setattr(registry, "create_subprocess_limited", _spawn)
        return spawned

    @pytest.mark.asyncio
    @pytest.mark.parametrize("landed", ["", "b" * 40])
    async def test_a_pin_that_did_not_land_is_refused_and_its_checkout_discarded(
        self, tmp_path, monkeypatch, landed
    ):
        dest = tmp_path / "slot" / "demo-app"

        def _init(argv, kwargs):
            dest.mkdir(parents=True)
            return self._Proc()

        self._spawns(monkeypatch, [(("git", "init"), _init)])
        monkeypatch.setattr(registry, "_resolved_clone_commit", lambda root: landed)
        log: list[str] = []
        result = await registry._git_fetch_commit(
            "https://github.com/acme/demo-app.git",
            "a" * 40,
            dest,
            log,
            clone_env={},
            sandbox_mode="strict",
        )
        assert result == {
            "ok": False,
            "name": "demo-app",
            "error": "pinned commit verification failed",
        }
        assert any("pinned commit not honoured" in line for line in log)
        assert not dest.exists()

    @pytest.mark.asyncio
    async def test_an_existing_checkout_is_fetched_into_never_initialised_or_removed(
        self, tmp_path, monkeypatch
    ):
        dest = tmp_path / "slot" / "demo-app"
        (dest / ".git").mkdir(parents=True)
        (dest / "keep.txt").write_text("user state", encoding="utf-8")
        spawned = self._spawns(monkeypatch, [(("git",), lambda a, k: self._Proc(returncode=1))])
        result = await registry._git_fetch_branch(
            "https://github.com/acme/demo-app.git",
            "main",
            dest,
            [],
            clone_env={},
            sandbox_mode="strict",
        )
        assert result == {"ok": False, "name": "demo-app", "error": "git fetch failed (exit 1)"}
        assert not any(argv[:2] == ("git", "init") for argv in spawned)
        assert (dest / "keep.txt").read_text(encoding="utf-8") == "user state"

    @pytest.mark.asyncio
    async def test_a_failed_pull_refuses_to_install_what_the_checkout_holds(
        self, tmp_path, monkeypatch
    ):
        dest = tmp_path / "slot" / "demo-app"
        (dest / ".git").mkdir(parents=True)
        url = "https://github.com/acme/demo-app.git"

        async def _origin(path):
            return url

        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda target: True)
        monkeypatch.setattr(registry, "_clone_origin_url", _origin)
        monkeypatch.setattr(registry, "_read_clone_branch", lambda path: "main")
        spawned = self._spawns(
            monkeypatch,
            [(("git", "-c"), lambda a, k: self._Proc(returncode=1, output=b"no"))],
        )
        log: list[str] = []
        result = await registry._git_clone_or_pull(url, "main", dest, log)
        assert result == {
            "ok": False,
            "error": "git pull failed (exit 1); not installing stale code",
        }
        # The hooks/fsmonitor neutralizer is spliced in right after ``git``.
        hooks = ("-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false")
        assert spawned == [("git", *hooks, "pull", "--ff-only", url, "main")]
        assert log[-1] == "git pull failed (exit 1) — aborting"
        assert (dest / ".git").is_dir(), "a failed pull leaves the checkout in place"

    @pytest.mark.asyncio
    async def test_a_pull_that_outlives_its_budget_is_killed_and_refused(
        self, tmp_path, monkeypatch
    ):
        dest = tmp_path / "slot" / "demo-app"
        (dest / ".git").mkdir(parents=True)
        url = "https://github.com/acme/demo-app.git"
        killed = []

        async def _origin(path):
            return url

        async def _kill(proc):
            killed.append(proc)

        async def _no_wait(awaitable, timeout):
            awaitable.close()
            raise asyncio.TimeoutError

        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda target: True)
        monkeypatch.setattr(registry, "_clone_origin_url", _origin)
        monkeypatch.setattr(registry, "_read_clone_branch", lambda path: "main")

        async def _no_signal(*args, **kwargs):
            raise AssertionError("the group kill must go through the patched seam")

        monkeypatch.setattr(registry, "_kill_process_group", _kill)
        monkeypatch.setattr(registry.platform_compat, "kill_process_tree_async", _no_signal)
        monkeypatch.setattr(registry.platform_compat, "kill_and_reap", _no_signal)
        self._spawns(monkeypatch, [])
        monkeypatch.setattr(registry.asyncio, "wait_for", _no_wait)
        log: list[str] = []
        result = await registry._git_clone_or_pull(url, "main", dest, log)
        assert result == {"ok": False, "error": "git pull timed out; not installing stale code"}
        assert len(killed) == 1
        assert log[-1] == "git pull timed out — aborting"
