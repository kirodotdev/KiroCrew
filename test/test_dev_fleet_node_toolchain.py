"""Dev Fleet + pod provisioning must find a version-manager node toolchain.

Two symptoms, one cause. ``_trusted_bin`` fails closed on anything under
``$HOME`` — correct for ``git``/``gh``, which run in the credential-bearing
tier — but Kiro Crew's own installer puts node under ``$HOME``, so:

* Dev Fleet "Pull + build main" answered ``no trusted executable for 'npm'``;
* ``kirocrew pod provision`` raised an unhandled ``FileNotFoundError: 'npm'``.

The fix gives the node toolchain its own resolution tier. These tests pin the
security boundary that makes that safe: the toolchain dirs reach the
credential-FREE build environment only, never the credential-bearing one.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.pod import provision as prov


def _fake_node_bin(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for name in ("node", "npm"):
        f = d / name
        f.write_text("#!/bin/sh\nexit 0\n")
        f.chmod(0o755)
    return d


@pytest.fixture
def node_dir(tmp_path, monkeypatch):
    """A resolvable node bin dir, with the module's PATH memo reset."""
    d = _fake_node_bin(tmp_path / "home" / ".local/share/mise/installs/node/22.0.0/bin")
    monkeypatch.setattr(mod, "_BUILD_PATH_CACHE", None, raising=False)
    monkeypatch.setattr(mod, "node_bin_dirs", lambda: (str(d),), raising=True)
    yield d
    monkeypatch.setattr(mod, "_BUILD_PATH_CACHE", None, raising=False)


# --- the security boundary ---
def test_credential_free_build_env_gets_the_node_toolchain(node_dir):
    """npm run-scripts are `#!/usr/bin/env node`, so node must resolve by NAME."""
    path = mod._build_env()["PATH"]
    assert path.split(os.pathsep)[0] == str(node_dir)


def test_credential_bearing_env_keeps_the_pinned_trusted_path(node_dir):
    """git looks its OWN helpers up on PATH (git-remote-https, credential
    helpers). A same-user-writable dir there is a route to intercepting a
    credential-bearing fetch, so this side must never be widened."""
    assert mod._build_env(with_credentials=True)["PATH"] == mod._TRUSTED_PATH
    assert str(node_dir) not in mod._build_env(with_credentials=True)["PATH"]


def test_the_two_environments_actually_differ(node_dir):
    """Non-vacuity: if these ever collapse to the same string, the test above
    passes for the wrong reason."""
    assert mod._build_env()["PATH"] != mod._build_env(with_credentials=True)["PATH"]


def test_credential_helpers_are_still_withheld_from_the_build_tier(node_dir):
    """The PATH change must not have disturbed the pre-existing helper split."""
    with patch.object(mod, "_GIT_TRUSTED_HELPERS", {"GIT_CONFIG_KEY_9": "credential.helper"}):
        assert "GIT_CONFIG_KEY_9" not in mod._build_env()
        assert "GIT_CONFIG_KEY_9" in mod._build_env(with_credentials=True)


def test_build_path_still_contains_the_trusted_dirs(node_dir):
    """Prepend, not replace: system git/coreutils a build step shells out to
    must stay reachable."""
    assert mod._TRUSTED_PATH in mod._build_path()


def test_build_path_is_memoized(node_dir):
    calls = []

    def counting():
        calls.append(1)
        return (str(node_dir),)

    with patch.object(mod, "node_bin_dirs", counting):
        mod._BUILD_PATH_CACHE = None
        mod._build_path()
        mod._build_path()
    assert len(calls) == 1


def test_no_toolchain_leaves_the_build_path_unchanged(monkeypatch):
    """A host with a system-only node must behave exactly as before."""
    monkeypatch.setattr(mod, "_BUILD_PATH_CACHE", None, raising=False)
    monkeypatch.setattr(mod, "node_bin_dirs", lambda: (), raising=True)
    assert mod._build_path() == mod._TRUSTED_PATH
    monkeypatch.setattr(mod, "_BUILD_PATH_CACHE", None, raising=False)


# --- the filesystem scan must not run on the event loop ---
def test_warm_build_path_resolves_off_the_event_loop(node_dir):
    """node_bin_dirs() globs and stats. On an NFS-backed $HOME that is slow
    enough to stall every backend request, so it must be resolved on the
    executor, never inline on the loop."""
    ran_on: list[str] = []

    def tracking_dirs():
        ran_on.append(threading.current_thread().name)
        return (str(node_dir),)

    async def go():
        loop_thread = threading.current_thread().name
        with patch.object(mod, "node_bin_dirs", tracking_dirs):
            mod._BUILD_PATH_CACHE = None
            await mod._warm_build_path()
        return loop_thread

    loop_thread = asyncio.run(go())
    assert ran_on, "node_bin_dirs was never called — the warm did nothing"
    assert ran_on[0] != loop_thread, (
        f"the scan ran on the event-loop thread ({loop_thread})"
    )
    mod._BUILD_PATH_CACHE = None


def test_warm_build_path_is_idempotent(node_dir):
    """Called at the top of every async handler, so the warm-path check must be
    free once resolved."""
    calls = []

    async def go():
        with patch.object(mod, "node_bin_dirs", lambda: calls.append(1) or (str(node_dir),)):
            mod._BUILD_PATH_CACHE = None
            await mod._warm_build_path()
            await mod._warm_build_path()

    asyncio.run(go())
    assert len(calls) == 1
    mod._BUILD_PATH_CACHE = None


@pytest.mark.parametrize("fn", [
    "_pod_up", "_pod_down", "_pod_provision", "_sync_start_locked",
])
def test_every_async_build_env_caller_warms_first(fn):
    """A handler must not depend on dev_fleet_startup having run — tests and any
    future entry point that skips startup would otherwise scan on the loop."""
    src = inspect.getsource(getattr(mod, fn))
    assert "_warm_build_path()" in src, f"{fn} builds a build env without warming"


def test_startup_warms_the_build_path():
    assert "_warm_build_path()" in inspect.getsource(mod.dev_fleet_startup)


# --- the advertised remedy must actually work on retry ---
def test_invalidate_drops_both_cache_layers(node_dir):
    """The banner says "run ensure-node.sh and press Pull + build again".

    That is only true if BOTH memo layers are dropped: `_BUILD_PATH_CACHE` and
    `node_bin_dirs`'s lru_cache. Leaving either one makes the remedy inert for
    the long-lived gateway that printed it.
    """
    cleared: list[str] = []

    class _Resolver:
        def __call__(self):
            return (str(node_dir),)

        def cache_clear(self):
            cleared.append("lru")

    with patch.object(mod, "node_bin_dirs", _Resolver()):
        mod._BUILD_PATH_CACHE = None
        mod._build_path()
        assert mod._BUILD_PATH_CACHE is not None
        mod._invalidate_toolchain_cache()
        assert mod._BUILD_PATH_CACHE is None, "_BUILD_PATH_CACHE not dropped"
    assert cleared == ["lru"], "node_bin_dirs.cache_clear() was not called"


def test_npm_not_found_path_invalidates_so_a_retry_rescans():
    """Non-vacuity: the not-found branch must call the invalidator, and the
    success branch must NOT (a working resolution is worth keeping)."""
    src = inspect.getsource(mod._sync_start_locked)
    head, _, tail = src.partition("if npm_bin is None:")
    assert tail, "npm-not-found branch not found — test is stale"
    assert "_invalidate_toolchain_cache()" in tail.split("raw_steps")[0]
    assert "_invalidate_toolchain_cache()" not in head


def test_npm_not_found_message_separates_the_two_remedies():
    """ensure-node.sh works on retry; an env var needs a restart. Saying both
    need a restart, or neither, is what made the original message wrong."""
    src = inspect.getsource(mod._sync_start_locked)
    assert "no restart needed" in src
    assert "does need a restart" in src


# --- npm resolution for Pull + build ---
def test_toolchain_bin_finds_a_home_resident_npm(node_dir):
    """The exact case _trusted_bin rejects, and the one Kiro Crew's own
    installer creates."""
    with patch.object(mod, "find_node_tool", return_value=str(node_dir / "npm")), \
            patch.object(mod, "_trusted_bin", return_value=None):
        assert mod._toolchain_bin("npm") == str(node_dir / "npm")


def test_toolchain_bin_prefers_the_managed_toolchain_over_system_npm(node_dir):
    """A distro node can be older than website/package.json's engines field
    (AL2023 ships node 18 against `20 || >=22`); ensure-node.sh installs one
    chosen to satisfy the build."""
    with patch.object(mod, "find_node_tool", return_value=str(node_dir / "npm")), \
            patch.object(mod, "_trusted_bin", return_value="/usr/bin/npm"):
        assert mod._toolchain_bin("npm") == str(node_dir / "npm")


def test_toolchain_bin_falls_back_to_the_trusted_system_binary():
    with patch.object(mod, "find_node_tool", return_value=None), \
            patch.object(mod, "_trusted_bin", return_value="/usr/bin/npm"):
        assert mod._toolchain_bin("npm") == "/usr/bin/npm"


def test_toolchain_bin_returns_none_when_nothing_resolves():
    with patch.object(mod, "find_node_tool", return_value=None), \
            patch.object(mod, "_trusted_bin", return_value=None):
        assert mod._toolchain_bin("does-not-exist-anywhere") is None


def test_toolchain_bin_searches_the_trusted_path_as_its_base():
    """find_node_tool must not be handed the inherited service PATH, whose
    leading entries are agent-writable."""
    with patch.object(mod, "find_node_tool", return_value=None) as fnt, \
            patch.object(mod, "_trusted_bin", return_value=None):
        mod._toolchain_bin("npm")
    assert fnt.call_args.args == ("npm", mod._TRUSTED_PATH)


def test_git_resolution_is_untouched_by_the_toolchain_tier():
    """The credential-bearing binaries keep the strict rule. Guards against a
    later refactor routing git through _toolchain_bin."""
    src = Path(mod.__file__).read_text()
    assert '_toolchain_bin("git")' not in src
    assert "_toolchain_bin('git')" not in src
    assert '_toolchain_bin("gh")' not in src
    assert "_toolchain_bin('gh')" not in src


# --- pod provision ---
def test_provision_passes_an_absolute_npm_and_a_node_path(tmp_path, monkeypatch):
    website = tmp_path / "website"
    website.mkdir()
    node_bin = _fake_node_bin(tmp_path / "nb")
    seen: list[tuple[list[str], dict | None]] = []

    def fake_run(cmd, cwd, env=None):
        seen.append((cmd, env))
        return 0

    monkeypatch.setattr(prov, "_has_node_modules", lambda w: False)
    monkeypatch.setattr(prov, "find_node_tool", lambda n: str(node_bin / n))
    monkeypatch.setattr(
        prov, "node_augmented_path", lambda base: f"{node_bin}{os.pathsep}{base}"
    )
    monkeypatch.setattr(prov, "_run", fake_run)

    assert prov.ensure_node_modules(website) is True
    argv, env = seen[0]
    assert argv[0] == str(node_bin / "npm")
    assert os.path.isabs(argv[0])
    assert env is not None
    assert env["PATH"].split(os.pathsep)[0] == str(node_bin)


def test_provision_reports_a_remedy_instead_of_raising(tmp_path, monkeypatch, capsys):
    """Before the fix this surfaced as a raw FileNotFoundError traceback from
    subprocess.Popen — no message, no remedy."""
    website = tmp_path / "website"
    website.mkdir()
    monkeypatch.setattr(prov, "_has_node_modules", lambda w: False)
    monkeypatch.setattr(prov, "find_node_tool", lambda n: None)

    def exploding_run(cmd, cwd, env=None):  # pragma: no cover - must not run
        raise AssertionError(f"must not spawn a bare name: {cmd}")

    monkeypatch.setattr(prov, "_run", exploding_run)

    assert prov.ensure_node_modules(website) is False
    err = capsys.readouterr().err
    assert "npm not found" in err
    assert "ensure-node.sh" in err
    assert "KIROCREW_NODE_BIN_DIR" in err


def test_build_dist_bails_out_when_npm_is_unresolvable(tmp_path, monkeypatch):
    checkout = tmp_path / "wt"
    (checkout / "website").mkdir(parents=True)
    monkeypatch.setattr(prov, "has_dist", lambda c: False)
    monkeypatch.setattr(prov, "ensure_node_modules", lambda w: True)
    monkeypatch.setattr(prov, "find_node_tool", lambda n: None)

    def exploding_run(cmd, cwd, env=None):  # pragma: no cover - must not run
        raise AssertionError(f"must not spawn a bare name: {cmd}")

    monkeypatch.setattr(prov, "_run", exploding_run)
    assert prov.build_dist(checkout) is False


def test_run_without_env_still_inherits(tmp_path, monkeypatch):
    """The new `env` parameter defaults to None so existing callers (venv/pip
    steps) keep inheriting the parent environment unchanged."""
    captured: dict = {}

    class _CP:
        returncode = 0

    def fake_subprocess_run(cmd, cwd=None, stdout=None, env=None):
        captured["env"] = env
        return _CP()

    monkeypatch.setattr(prov.subprocess, "run", fake_subprocess_run)
    assert prov._run(["true"], tmp_path) == 0
    assert captured["env"] is None
