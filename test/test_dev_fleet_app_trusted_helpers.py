"""Dev Fleet trusted helpers and binaries, and the service-state fields of the fleet.

Covers ``gateway_service_active``, re-injecting the operator's trusted git credential
helpers, trusted-binary resolution, the build steps' credential isolation and the
smaller handler hardening around them.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from dev_fleet_app_helpers import _reset_make_live_committed_latch  # noqa: F401 - fixture
from dev_fleet_app_helpers import _reset_shutdown_admission_state  # noqa: F401 - fixture
from dev_fleet_app_helpers import _sweep_staged_sync_tempdirs  # noqa: F401 - fixture
from dev_fleet_app_helpers import _venv_maps_to_the_synced_checkout  # noqa: F401 - fixture
from dev_fleet_app_helpers import _POSIX_ONLY, _reap_cleanup_paths, _sync_step_argvs

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.dev_fleet import fleet_state as fleet_state_mod
from kiro_crew.apps.builtins.dev_fleet import http_api as http_api_mod
from kiro_crew.apps.builtins.dev_fleet import live as live_mod
from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.apps.builtins.dev_fleet import worktree_ops as worktree_ops_mod

# =============================================================================
# Task 2b: fleet gateway_service_active
# =============================================================================


@pytest.mark.asyncio
async def test_fleet_includes_gateway_service_active(monkeypatch):
    """_gateway_service_active probes via the sandboxed _run_cmd chokepoint."""
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="linux"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
    )
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return 0, "active", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    assert await mod._gateway_service_active() is True
    assert calls and calls[0][:3] == ["systemctl", "--user", "is-active"]


@pytest.mark.asyncio
async def test_gateway_service_active_no_manager(monkeypatch):
    """A platform with neither systemd nor launchd -> False, and NO spawn.

    ``platform="darwin"`` is a SUPPORTED backend, so the "no manager at all"
    case has to be expressed with
    a platform that really has none -- mirroring
    ``test_live_user_unit_status_no_manager``. Asserting darwin here made the
    verdict depend on whether the *host* happened to have the agent loaded,
    because neither ``shutil`` nor ``_run_cmd`` was faked.
    """
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="win32"))
    run = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(runtime_mod, "_run_cmd", run)
    assert await mod._gateway_service_active() is False
    # The "without spawning" half of the original intent, now actually verified:
    # backend() returns None for win32, so nothing may be probed.
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_service_active_darwin_live_agent(monkeypatch):
    """macOS with a loaded agent reporting a live pid -> True.

    The darwin-True path had no *direct* assertion: the only ``is True`` case in
    this area runs under ``platform="linux"``. It was covered indirectly via
    ``test_restart_gateway_darwin_requests_graceful_stop``.
    """
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return 0, "  pid = 4242\n", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    assert await mod._gateway_service_active() is True
    assert calls and calls[0][:2] == ["launchctl", "print"]


@pytest.mark.asyncio
async def test_gateway_service_active_darwin_loaded_without_pid(monkeypatch):
    """macOS agent loaded but not running (no pid line) -> False.

    ``LaunchdBackend.active`` treats only a live pid as active, mirroring
    ``systemctl is-active``; a zero exit from ``launchctl print`` alone is not
    enough.
    """
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(
        runtime_mod, "_run_cmd", AsyncMock(return_value=(0, "  state = waiting\n", ""))
    )
    assert await mod._gateway_service_active() is False


# ---- trusted global credential helper re-injection ----


@pytest.mark.asyncio
async def test_trusted_helpers_loaded_from_global_config(monkeypatch):
    """Operator-global gh helper is SYNTHESIZED and re-pinned after the
    reset; the persistent `store` helper is rejected."""
    monkeypatch.setattr(runtime_mod, "_GIT_TRUSTED_HELPERS", None)
    monkeypatch.setattr(runtime_mod, "_trusted_bin", lambda n: f"/usr/bin/{n}")

    scopes: list = []

    async def fake_run_cmd(cmd, **kw):
        assert cmd[:2] == ["git", "config"]
        assert cmd[3] == "--get-regexp"
        scopes.append(cmd[2])
        if cmd[2] != "--global":
            return 1, "", ""  # nothing machine-wide
        return (
            0,
            (
                "credential.https://github.com.helper !gh auth git-credential\n"
                "credential.helper store\n"
            ),
            "",
        )

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    await mod._load_trusted_credential_helpers()
    h = mod._GIT_TRUSTED_HELPERS
    assert h is not None
    base = int(mod._GIT_ENV_NEUTRALIZERS["GIT_CONFIG_COUNT"])
    assert h[f"GIT_CONFIG_KEY_{base}"] == "credential.https://github.com.helper"
    assert h[f"GIT_CONFIG_VALUE_{base}"] == "!/usr/bin/gh auth git-credential"
    assert f"GIT_CONFIG_KEY_{base + 1}" not in h
    assert h["GIT_CONFIG_COUNT"] == str(base + 1)
    # Both operator-owned scopes are probed, and repo-LOCAL never is: a checkout
    # Dev Fleet builds can write .git/config, and a helper from there would run
    # in the credential-bearing standard tier.
    assert scopes == ["--system", "--global"]


@pytest.mark.asyncio
async def test_trusted_helpers_loaded_from_system_config(monkeypatch):
    """A SYSTEM-scope helper is re-pinned — the stock-macOS case.

    Xcode's Command Line Tools ship `credential.helper = osxkeychain` in the
    system gitconfig and a stock install has nothing in global, so scanning only
    --global left the neutralizer's reset unrepaired and `git fetch` died with
    "could not read Username" (no tty to prompt on).
    """
    monkeypatch.setattr(runtime_mod, "_GIT_TRUSTED_HELPERS", None)

    async def fake_run_cmd(cmd, **kw):
        if cmd[2] == "--system":
            return 0, "credential.helper osxkeychain\n", ""
        return 1, "", ""  # nothing in global, as on a stock macOS host

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    await mod._load_trusted_credential_helpers()
    h = mod._GIT_TRUSTED_HELPERS or {}
    base = int(mod._GIT_ENV_NEUTRALIZERS["GIT_CONFIG_COUNT"])
    assert h[f"GIT_CONFIG_KEY_{base}"] == "credential.helper"
    assert h[f"GIT_CONFIG_VALUE_{base}"] == "osxkeychain"
    assert h["GIT_CONFIG_COUNT"] == str(base + 1)


@pytest.mark.asyncio
async def test_trusted_helpers_global_is_pinned_after_system(monkeypatch):
    """Ordering mirrors git's own precedence: system first, then global.

    credential.helper is multi-valued and the LAST entry wins, so an operator's
    own global setting must still override a machine-wide default.
    """
    monkeypatch.setattr(runtime_mod, "_GIT_TRUSTED_HELPERS", None)

    async def fake_run_cmd(cmd, **kw):
        if cmd[2] == "--system":
            return 0, "credential.helper osxkeychain\n", ""
        return 0, "credential.helper manager\n", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    await mod._load_trusted_credential_helpers()
    h = mod._GIT_TRUSTED_HELPERS or {}
    base = int(mod._GIT_ENV_NEUTRALIZERS["GIT_CONFIG_COUNT"])
    assert h[f"GIT_CONFIG_VALUE_{base}"] == "osxkeychain"  # system
    assert h[f"GIT_CONFIG_VALUE_{base + 1}"] == "manager"  # global wins
    assert h["GIT_CONFIG_COUNT"] == str(base + 2)


@pytest.mark.asyncio
async def test_trusted_helpers_never_read_repo_local_scope(monkeypatch):
    """--local is never probed.

    Repo-local config is the attack surface the neutralizer's reset exists for:
    a checkout Dev Fleet builds can write .git/config, and a helper from there
    would run in the credential-bearing standard tier.
    """
    monkeypatch.setattr(runtime_mod, "_GIT_TRUSTED_HELPERS", None)
    scopes: list = []

    async def fake_run_cmd(cmd, **kw):
        scopes.append(cmd[2])
        return 1, "", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    await mod._load_trusted_credential_helpers()
    assert "--local" not in scopes
    assert set(scopes) == {"--system", "--global"}


@pytest.mark.asyncio
async def test_trusted_helpers_empty_when_no_global_config(monkeypatch):
    """No global helpers -> reset stands, no env additions."""
    monkeypatch.setattr(runtime_mod, "_GIT_TRUSTED_HELPERS", None)

    async def fake_run_cmd(cmd, **kw):
        return 1, "", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    await mod._load_trusted_credential_helpers()
    assert mod._GIT_TRUSTED_HELPERS == {}


def _fake_helpers():
    base = int(mod._GIT_ENV_NEUTRALIZERS["GIT_CONFIG_COUNT"])
    return base, {
        f"GIT_CONFIG_KEY_{base}": "credential.https://github.com.helper",
        f"GIT_CONFIG_VALUE_{base}": "!gh auth git-credential",
        "GIT_CONFIG_COUNT": str(base + 1),
    }


def test_build_env_credentials_only_when_requested(monkeypatch):
    """Trusted helpers layer over the neutralizer reset ONLY for the
    credentialed (fetch) variant; the default build env never sees them."""
    base, helpers = _fake_helpers()
    monkeypatch.setattr(runtime_mod, "_GIT_TRUSTED_HELPERS", helpers)
    env = mod._build_env(with_credentials=True)
    assert env["GIT_CONFIG_COUNT"] == str(base + 1)
    assert env[f"GIT_CONFIG_VALUE_{base}"] == "!gh auth git-credential"
    assert env["GIT_CONFIG_VALUE_2"] == ""  # repo-helper reset still present
    plain = mod._build_env()
    assert f"GIT_CONFIG_KEY_{base}" not in plain
    assert plain["GIT_CONFIG_COUNT"] == str(base)


def _is_stage_step(argv: list) -> bool:
    # The sync stages via the combined build+stage entry point, which holds the
    # staging lock across both halves.
    return any("stage_built_dist" in str(part) or "build_and_stage" in str(part) for part in argv)


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_sync_stages_dist_on_a_stock_checkout(monkeypatch):
    """The staging step is part of the sync on a stock checkout.

    `npm run build` writes website/dist while the dashboard serves
    src/kiro_crew/static/dist; without this step Pull+Build reports success and
    the gateway keeps serving the previous bundle.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    argvs = await _sync_step_argvs(monkeypatch)
    stage_at = [i for i, a in enumerate(argvs) if _is_stage_step(a)]
    assert stage_at, f"no staging step in {argvs}"
    # It must be the COMBINED entry point: a staging-only step would put the
    # build back outside the lock holder without tripping the guards below.
    assert any(
        "build_and_stage" in str(x) for x in argvs[stage_at[0]]
    ), f"the stage step must also perform the build: {argvs[stage_at[0]]}"
    # Staging cannot precede the build: they are the SAME step, which holds the
    # staging lock across both so no peer flow can copy a half-written tree.
    ci_at = [i for i, a in enumerate(argvs) if Path(a[0]).name == "npm" and "ci" in a]
    assert ci_at and stage_at[0] > ci_at[0], "the build+stage step must run after npm ci"
    assert not any(
        Path(a[0]).name == "npm" and "build" in a for a in argvs
    ), "a separate npm build step would run outside the staging lock holder"
    # THIS backend's interpreter, not the target checkout's: resolving the
    # helper from the pulled revision would make the step's existence contingent
    # on that revision already carrying it, so an older target would turn the
    # whole Pull+Build into an ImportError.
    assert argvs[stage_at[0]][0] == sys.executable


@pytest.mark.asyncio
async def test_sync_never_stages_dist_on_an_edition_checkout(monkeypatch):
    """An edition checkout must NOT have its dashboard rebuilt or staged over.

    The sync build runs under _build_env(), whose allowlist drops
    KIROCREW_EDITION_DIR / KIROCREW_ALLOW_EDITION, so on an edition composition
    root `npm run build` compiles the STOCK SPA. Staging that would silently
    replace the edition dashboard with upstream's; leaving the shipped bundle in
    place is what frontend's own edition guards already do.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: True)
    argvs = await _sync_step_argvs(monkeypatch)
    assert not any(_is_stage_step(a) for a in argvs)
    # The BUILD is skipped too. The build publishes into website/dist, so on a
    # source-tree install -- where static/dist is a symlink to it -- the stock build
    # alone would replace the served edition dashboard, staging step or not.
    assert not any(Path(a[0]).name == "npm" for a in argvs)
    # The backend half of the sync is untouched: an edition still gets the pull
    # and the editable reinstall.
    assert any(Path(a[0]).name == "git" and "fetch" in a for a in argvs)
    assert any("pip" in a for a in argvs)


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_sync_build_steps_never_see_credential_helpers(monkeypatch):
    """Only the network fetch step carries operator credential helpers;
    worktree-controlled merge/pip/npm steps must not (token minting via
    `git credential fill` from a malicious install script)."""
    base, helpers = _fake_helpers()
    monkeypatch.setattr(runtime_mod, "_GIT_TRUSTED_HELPERS", helpers)
    captured: list[tuple[list, dict]] = []

    def fake_sandbox(argv, mode, *, env=None, **kw):
        captured.append((list(argv), dict(env or {})))
        return list(argv), dict(env or {}), None

    with (
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value=mod.BASE_BRANCH),
        patch.object(worktree_ops_mod, "_venv_python", return_value=Path("/fake/.venv/bin/python")),
        patch.object(runtime_mod, "_trusted_bin", side_effect=lambda n: f"/usr/bin/{n}"),
        patch.object(worktree_ops_mod, "sandboxed_spawn_argv", fake_sandbox),
        patch.object(
            runtime_mod, "_start_run", new_callable=AsyncMock, return_value="rid-cred-test"
        ),
    ):
        worktree_ops_mod._SYNC_RID = None
        res = await mod._sync()
    assert res["ok"] is True
    key = f"GIT_CONFIG_KEY_{base}"

    def _base(a):
        # Skip leading `-c <value>` pairs (the config-hook disable + submodule-recursion
        # pins) so the git SUBCOMMAND is matched, not the first pin flag.
        i = 1
        while i + 1 < len(a) and a[i] == "-c":
            i += 2
        return [Path(a[0]).name, *([a[i]] if i < len(a) else [])]

    fetch_envs = [e for a, e in captured if _base(a) == ["git", "fetch"]]
    build_envs = [
        e
        for a, e in captured
        if _base(a) == ["git", "merge"] or "pip" in a or Path(a[0]).name == "npm"
        # The build+stage step is a build step too and must not be exempt from
        # the credential-absence invariant just because it runs via `python -c`.
        or any("build_and_stage" in str(x) for x in a)
        # Neither is the preflight: it runs npm against the incoming lockfile,
        # so it is squarely in the worktree-controlled tier. With the operator
        # repair seam removed, NOTHING on the sync path carries credentials
        # except the fetch step.
        or any("npm_preflight" in str(x) for x in a)
    ]
    assert fetch_envs and all(key in e for e in fetch_envs)
    assert len(build_envs) == 5  # merge + preflight + pip + npm ci + (build + stage)
    assert all(key not in e for e in build_envs)


@pytest.mark.asyncio
async def test_fetch_pr_head_oid_refuses_non_merged(monkeypatch):
    """Branch-name reuse: a fresh OPEN PR on a recycled name must NOT yield a
    head OID at the destructive boundary, even if a stale MERGED verdict is
    cached elsewhere. The query is ``gh pr list`` (a JSON array) so it survives
    the head branch being deleted on merge."""

    async def fake_run(cmd, **kw):
        return 0, json.dumps([{"headRefOid": "a" * 40, "state": "OPEN"}]), ""

    monkeypatch.setattr(fleet_state_mod, "_get_owner_repo", AsyncMock(return_value="o/r"))
    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run)
    assert await mod._fetch_pr_head_oid("feature-x") is None

    async def fake_run_merged(cmd, **kw):
        return 0, json.dumps([{"headRefOid": "b" * 40, "state": "MERGED"}]), ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_merged)
    assert await mod._fetch_pr_head_oid("feature-x") == "b" * 40


@_POSIX_ONLY
def test_trusted_bin_rejects_agent_writable_path(monkeypatch, tmp_path):
    """Bare command names resolve only inside the trusted bin dirs; a planted
    shim in an agent-writable PATH entry is never selected."""
    mod._TRUSTED_BIN_CACHE.clear()
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\necho pwned\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    resolved = mod._trusted_bin("git")
    assert resolved is not None and resolved.startswith(("/usr/", "/bin", "/opt/homebrew/"))
    mod._TRUSTED_BIN_CACHE.clear()
    assert mod._trusted_bin("definitely-not-a-real-tool-xyz") is None
    mod._TRUSTED_BIN_CACHE.clear()


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="POSIX bin dirs")
def test_trusted_bin_dirs_cover_homebrew_prefixes():
    """A `gh`/`git` the user installed with Homebrew must be reachable: without
    the brew prefixes Dev Fleet could not find gh at all on a stock macOS host
    (only Xcode's /usr/bin/git), and its PATH pin excluded them too."""
    assert "/opt/homebrew/bin" in mod._TRUSTED_BIN_DIRS
    assert "/home/linuxbrew/.linuxbrew/bin" in mod._TRUSTED_BIN_DIRS
    assert "/opt/homebrew/bin" in mod._TRUSTED_PATH.split(os.pathsep)


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="POSIX symlink layout")
def test_trusted_bin_pins_the_resolved_target_not_the_symlink(monkeypatch, tmp_path):
    """Homebrew's `bin/gh` is a user-writable symlink into `Cellar/`. Caching the
    LINK would let it be repointed between validation and execution, so the
    vetted real path is what gets cached and spawned.

    ``_trusted_bin`` refuses any resolved target under ``Path.home()``. Whether
    ``tmp_path`` is inside HOME is a property of the HOST (a ``TMPDIR`` under
    ``~`` puts it there; CI and macOS keep it outside), so the home root is
    pinned to a sibling directory that is NOT an ancestor of the fake Cellar.
    """
    mod._TRUSTED_BIN_CACHE.clear()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    cellar = tmp_path / "Cellar" / "gh" / "1.0" / "bin"
    cellar.mkdir(parents=True)
    target = cellar / "gh"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o555)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").symlink_to(target)
    monkeypatch.setattr(runtime_mod, "_TRUSTED_BIN_DIRS", (str(bin_dir),))

    assert mod._trusted_bin("gh") == str(target.resolve())
    mod._TRUSTED_BIN_CACHE.clear()


def test_trusted_bin_refuses_target_under_home(monkeypatch, tmp_path):
    """The HOME refusal is the guard the test above pins around: an otherwise
    system-shaped target (0o555, not writable by us) whose resolved path lies
    under ``Path.home()`` is never selected, because anything under the user's
    home is the agent's to replace. Every platform: the refusal is decided on
    the resolved path before any POSIX mode check, so the candidate is placed
    directly in the trusted dir (no symlink) and the test runs on Windows too.
    """
    mod._TRUSTED_BIN_CACHE.clear()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    bin_dir = home / "Cellar" / "gh" / "1.0" / "bin"
    bin_dir.mkdir(parents=True)
    exe = "gh.exe" if platform_compat.IS_WINDOWS else "gh"
    target = bin_dir / exe
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o555)
    monkeypatch.setattr(runtime_mod, "_TRUSTED_BIN_DIRS", (str(bin_dir),))

    assert mod._trusted_bin("gh") is None
    mod._TRUSTED_BIN_CACHE.clear()


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_run_cmd_pins_trusted_path(monkeypatch):
    """_run_cmd rewrites bare names to trusted absolute paths and pins PATH."""
    captured: dict = {}

    def fake_sandbox(argv, mode, *, env=None, **kw):
        captured["argv"] = list(argv)
        captured["env"] = dict(env or {})
        return ["/bin/false"], dict(env or {}), None

    monkeypatch.setattr(runtime_mod, "sandboxed_spawn_argv", fake_sandbox)
    mod._TRUSTED_BIN_CACHE.clear()
    await mod._run_cmd(["git", "--version"], timeout=5)
    assert captured["argv"][0].startswith(("/usr/", "/bin"))
    assert captured["env"]["PATH"] == mod._TRUSTED_PATH
    mod._TRUSTED_BIN_CACHE.clear()


@pytest.mark.asyncio
async def test_upstream_remote_rejects_option_injection(monkeypatch):
    """A repo-writable `branch.main.remote` shaped like an option must never
    be interpolated into later git argv — fall back to origin."""

    async def fake_run(cmd, **kw):
        if "config" in cmd:
            return 0, "--exec=touch /tmp/pwned #", ""
        return 0, "origin\nkirocrew\n", ""

    monkeypatch.setattr(repository_mod, "_UPSTREAM_REMOTE", None)
    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run)
    assert await mod._upstream_remote() == "origin"

    async def fake_run_valid(cmd, **kw):
        if "config" in cmd:
            return 0, "kirocrew", ""
        return 0, "origin\nkirocrew\n", ""

    monkeypatch.setattr(repository_mod, "_UPSTREAM_REMOTE", None)
    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_valid)
    assert await mod._upstream_remote() == "kirocrew"

    async def fake_run_unlisted(cmd, **kw):
        if "config" in cmd:
            return 0, "evil", ""
        return 0, "origin\nkirocrew\n", ""

    monkeypatch.setattr(repository_mod, "_UPSTREAM_REMOTE", None)
    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_unlisted)
    assert await mod._upstream_remote() == "origin"
    monkeypatch.setattr(repository_mod, "_UPSTREAM_REMOTE", None)


def test_find_cli_is_module_invocation_only(nonbundled_python_without_user_site):
    """No filesystem resolution: a planted `kirocrew` shim must never become
    the pod CLI. Always our interpreter + the RUNNABLE ``kiro_crew`` package
    entry (its __main__), never ``kiro_crew.cli`` (no __main__ guard -> #220)."""
    import sys as _sys

    assert mod._find_cli() == [_sys.executable, "-s", "-P", "-m", "kiro_crew"]

    import subprocess as _sp

    cp = _sp.run(
        mod._find_cli() + ["pod"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert "Usage" in (cp.stdout + cp.stderr) or cp.returncode == 2


def test_sanitize_helper_rejects_shell_and_persistent(monkeypatch):
    """The configured value is never executed as-is: trusted argv[0] with
    attacker arguments (`!/usr/bin/sh -c ...`), persistent helpers
    (store/cache), absolute paths, and argument-carrying names are all
    rejected -- only exact allowlisted shapes select a synthesized command."""
    monkeypatch.setattr(runtime_mod, "_trusted_bin", lambda n: "/usr/bin/gh" if n == "gh" else None)
    reject = [
        "",
        "!malicious-command --steal",
        "!/home/user/.local/bin/evil",
        "!/usr/bin/sh -c 'cat > /tmp/creds'",  # trusted argv[0], evil args
        "!/usr/bin/gh auth git-credential --extra",  # extra argv
        "!gh api /user",  # gh but wrong subcommand
        "store",  # persists creds to file
        "cache --timeout=999999",
        "store --file=/tmp/x",
        "/usr/bin/git-credential-store",  # absolute path form
        "osxkeychain --flag",
        '!"unterminated',  # shlex ValueError
    ]
    for val in reject:
        assert mod._sanitize_helper_value(val) is None, val


def test_sanitize_helper_synthesizes_gh(monkeypatch):
    """A gh-shaped helper is re-synthesized from _trusted_bin -- the
    configured path (e.g. ~/.local/bin/gh via operator override) is
    discarded, so a HOME-planted binary never runs unless the operator
    unit file explicitly designates it."""
    monkeypatch.setattr(
        runtime_mod, "_trusted_bin", lambda n: "/opt/trusted/gh" if n == "gh" else None
    )
    expected = "!/opt/trusted/gh auth git-credential"
    assert mod._sanitize_helper_value("!gh auth git-credential") == expected
    assert (
        mod._sanitize_helper_value("!/local/home/user/.local/bin/gh auth git-credential")
        == expected
    )
    monkeypatch.setattr(runtime_mod, "_trusted_bin", lambda n: None)
    assert mod._sanitize_helper_value("!gh auth git-credential") is None
    for name in ("osxkeychain", "manager", "manager-core", "libsecret", "wincred"):
        assert mod._sanitize_helper_value(name) == name


@pytest.mark.asyncio
async def test_load_helpers_skips_untrusted(monkeypatch):
    """Untrusted helper lines are dropped; trusted ones survive with a
    correct GIT_CONFIG_COUNT."""

    async def fake_run(cmd, **kw):
        if cmd[2] != "--global":
            return 1, "", ""
        return (
            0,
            (
                "credential.helper !evil-shim\n"
                "credential.https://github.com.helper !gh auth git-credential\n"
            ),
            "",
        )

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run)
    monkeypatch.setattr(
        runtime_mod,
        "_trusted_bin",
        lambda n: "/usr/bin/gh" if n == "gh" else None,
    )
    await mod._load_trusted_credential_helpers()
    h = mod._GIT_TRUSTED_HELPERS or {}
    vals = [v for k, v in h.items() if k.startswith("GIT_CONFIG_VALUE_")]
    assert vals == ["!/usr/bin/gh auth git-credential"]
    monkeypatch.setattr(runtime_mod, "_GIT_TRUSTED_HELPERS", None)


@pytest.mark.asyncio
async def test_hmac_no_secret_always_denies(monkeypatch):
    """No app secret = fail closed, no env bypass, and the denial is SEL-audited."""
    events: list = []

    class FakeSel:
        def log_tool_invocation(self, **kw):
            events.append(kw)

    monkeypatch.setattr(http_api_mod, "_load_app_secret", lambda: "")
    monkeypatch.setattr(runtime_mod, "_sel", lambda: FakeSel())
    monkeypatch.setenv("KIROCREW_DEVFLEET_INSECURE", "1")
    app = mod.create_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/fleet")
        assert resp.status == 401
    denials = [e for e in events if e.get("outcome") == "denied"]
    assert denials and denials[0]["tool_name"] == "dev-fleet:proxy-hmac"


@pytest.mark.asyncio
async def test_hmac_invalid_signature_denial_is_audited(monkeypatch):
    """Every HMAC 401 path emits exactly one SEL denied event."""
    events: list = []

    class FakeSel:
        def log_tool_invocation(self, **kw):
            events.append(kw)

    monkeypatch.setattr(http_api_mod, "_load_app_secret", lambda: "sekrit")
    monkeypatch.setattr(runtime_mod, "_sel", lambda: FakeSel())
    app = mod.create_app()
    async with TestClient(TestServer(app)) as client:
        r1 = await client.get("/api/fleet")  # missing header
        r2 = await client.get("/api/fleet", headers={"X-KiroCrew-Proxy": "junk"})
        ts = str(int(time.time()))
        r3 = await client.get("/api/fleet", headers={"X-KiroCrew-Proxy": f"{ts}:deadbeef"})
        assert (r1.status, r2.status, r3.status) == (401, 401, 401)
    assert len([e for e in events if e.get("outcome") == "denied"]) == 3


@pytest.mark.asyncio
async def test_startup_skips_background_tasks_when_disabled(monkeypatch):
    """``dev_fleet_startup`` must not start the refresher/reaper/warm tasks
    when background tasks are disabled, so tests that boot the real app via
    ``create_app()`` (e.g. the HMAC tests above) never drag in a live network
    ``git fetch``. An unstubbed ``_status_refresher`` leaks into unrelated tests
    and flakes the macOS backend job."""
    monkeypatch.setattr(http_api_mod, "_load_app_secret", lambda: "sekrit")
    monkeypatch.setattr(worktree_ops_mod, "_background_tasks_disabled", lambda: True)
    app = mod.create_app()
    async with TestClient(TestServer(app)):
        assert mod._refresher_task is None
        assert mod._reaper_task is None
        assert mod._warm_task is None


@pytest.mark.asyncio
async def test_startup_starts_background_tasks_when_enabled(monkeypatch):
    """The opposite of the above: with the gate off (production default),
    startup still creates all three background tasks."""
    monkeypatch.setattr(http_api_mod, "_load_app_secret", lambda: "sekrit")
    monkeypatch.setattr(worktree_ops_mod, "_background_tasks_disabled", lambda: False)
    monkeypatch.setattr(repository_mod, "_upstream_remote", AsyncMock(return_value="origin"))
    monkeypatch.setattr(runtime_mod, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    monkeypatch.setattr(fleet_state_mod, "_fleet_refresh", AsyncMock(return_value=None))
    monkeypatch.setattr(worktree_ops_mod, "_auto_prune_reaper", AsyncMock(return_value=None))
    app = mod.create_app()
    async with TestClient(TestServer(app)):
        assert mod._refresher_task is not None
        assert mod._reaper_task is not None
        assert mod._warm_task is not None


@pytest.mark.asyncio
async def test_prunable_merged_unverified_when_oid_lookup_fails():
    """OID verification unavailable -> never a prune candidate (preview must
    match the removal path, which would refuse anyway)."""
    with (
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=2),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value=None),
        patch.object(
            fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value=None
        ),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
    ):
        v = await mod._prunable("/fake/path", "feat-branch")
    assert v["ok"] is False
    assert v["code"] == "merged_unverified"


def test_strict_sandbox_hides_gh_config():
    """.config/gh (the gh helper's token store) must be hidden from the
    strict tier where worktree-controlled code executes."""
    import kiro_crew.sandbox as sandbox_mod

    assert ".config/gh" in sandbox_mod._STRICT_DIRS
    assert ".config/gh" not in sandbox_mod._STANDARD_DIRS


@_POSIX_ONLY
def test_build_preexec_raises_nofile_ceiling(monkeypatch):
    """Build-class spawns get a 65536 NOFILE ceiling (default 1024 EMFILEs vite)."""
    import kiro_crew.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "_BUILD_RESOURCE_PREEXEC", sandbox_mod._UNSET)
    captured: dict = {}

    def fake_apply(cfg):
        captured.update((cfg or {}).get("resource_limits") or {})
        return lambda: None

    monkeypatch.setattr("kiro_crew.security.apply_resource_limits", fake_apply)
    fn = sandbox_mod.build_resource_limit_preexec()
    assert fn is not None
    assert captured["max_open_files"] >= 65536


@_POSIX_ONLY
def test_build_preexec_tolerates_malformed_config(monkeypatch):
    """A junk operator value ("lots") must not raise — the spawn falls back
    to the ceiling instead of leaving Dev Fleet unable to start."""
    import kiro_crew.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "_BUILD_RESOURCE_PREEXEC", sandbox_mod._UNSET)
    monkeypatch.setattr(
        "kiro_crew.config.loader._raw_config",
        lambda: {"resource_limits": {"max_open_files": "lots"}},
    )
    captured: dict = {}

    def fake_apply(cfg):
        captured.update((cfg or {}).get("resource_limits") or {})
        return lambda: None

    monkeypatch.setattr("kiro_crew.security.apply_resource_limits", fake_apply)
    assert sandbox_mod.build_resource_limit_preexec() is not None
    assert captured["max_open_files"] == sandbox_mod._BUILD_NOFILE_CEILING


def test_build_pending_dist_path_is_package_static_dist():
    """The dist probe must resolve to kiro_crew/static/dist — the parent-chain
    silently broke when this module moved from dashboard/handlers/."""
    import pathlib

    root = pathlib.Path(mod.__file__).resolve().parents[3]
    assert root.name == "kiro_crew"
    assert mod._build_pending() in (True, False)
    probed = root / "static" / "dist"
    assert probed.parts[-3:] == ("kiro_crew", "static", "dist")


@pytest.mark.asyncio
async def test_pr_status_falls_back_to_ancestor_verified_repo(monkeypatch):
    """No PR in the upstream repo -> ancestor-verified legacy repo is queried."""
    monkeypatch.setattr(repository_mod, "_FALLBACK_REPOS", ["old-org/old-repo"])
    queried: list = []

    async def fake_owner_repo():
        return "new-org/new-repo"

    async def fake_query(repo, branch):
        queried.append(repo)
        if repo == "old-org/old-repo":
            return {"number": 31, "state": "MERGED", "_repo": repo}
        return None

    monkeypatch.setattr(fleet_state_mod, "_get_owner_repo", fake_owner_repo)
    monkeypatch.setattr(fleet_state_mod, "_pr_query_one", fake_query)
    pr = await mod._fetch_pr_status("feat/legacy")
    assert queried == ["new-org/new-repo", "old-org/old-repo"]
    assert pr is not None and pr["state"] == "MERGED" and pr["_repo"] == "old-org/old-repo"


def test_redact_pr_strips_internal_fields():
    out = mod._redact_pr({"number": 31, "state": "MERGED", "_repo": "o/r"})
    assert out is not None
    assert "_repo" not in out and out["number"] == 31


@pytest.mark.asyncio
async def test_sync_pip_uses_target_repo_venv(monkeypatch, tmp_path):
    """The pip step must run the MAIN_REPO's own venv python — using the
    backend's sys.executable hijacked the gateway venv's editable install."""
    import sys as _sys

    repo = tmp_path / "mainrepo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("")
    monkeypatch.setattr(repository_mod, "MAIN_REPO", str(repo))
    monkeypatch.setattr(worktree_ops_mod, "_SYNC_RID", None)

    async def fake_remote():
        return "origin"

    async def fake_head():
        return "main"

    monkeypatch.setattr(repository_mod, "_upstream_remote", fake_remote, raising=False)
    monkeypatch.setattr(runtime_mod, "_trusted_bin", lambda n: f"/usr/bin/{n}")
    captured: dict = {}

    def fake_sandboxed(argv, mode, env=None):
        captured.setdefault("argvs", []).append(list(argv))
        return list(argv), dict(env or {}), None

    monkeypatch.setattr(worktree_ops_mod, "sandboxed_spawn_argv", fake_sandboxed)

    async def fake_run_cmd(cmd, **kw):
        return 0, "main", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)

    async def fake_start_run(label, cmd, **kw):
        captured["cmd"] = cmd
        captured["start_run_kw"] = kw
        # The real run removes these when it ends; the stub must too.
        _reap_cleanup_paths(kw.get("cleanup_paths"))
        return "rid-1"

    monkeypatch.setattr(runtime_mod, "_start_run", fake_start_run)
    res = await mod._sync_start_locked()
    assert res.get("ok"), res
    pip_argvs = [a for a in captured.get("argvs", []) if "-m" in a and "pip" in a]
    assert pip_argvs, captured.get("argvs")
    assert pip_argvs[0][0] == str(repo / ".venv" / "bin" / "python")
    assert pip_argvs[0][0] != _sys.executable
    # A dependency sync writes into the measured repo (.venv, node_modules),
    # so the run must carry the disk-cache invalidation hook.
    assert captured["start_run_kw"].get("on_finish") is fleet_state_mod._disk_invalidate


@pytest.mark.asyncio
async def test_sync_refuses_when_target_repo_has_no_venv(monkeypatch, tmp_path):
    monkeypatch.setattr(repository_mod, "MAIN_REPO", str(tmp_path / "novenv"))
    monkeypatch.setattr(worktree_ops_mod, "_SYNC_RID", None)

    async def fake_run_cmd(cmd, **kw):
        return 0, "main", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._sync_start_locked()
    assert res.get("ok") is False and "venv" in res.get("error", "")


def test_gateway_unit_resolves_pod_instance(monkeypatch, tmp_path):
    """Inside a pod HOME the restart target is the pod unit, never the live one."""
    pod_home = tmp_path / ".kirocrew-pods" / "kirocrew-wt-feature"
    pod_home.mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(pod_home))
    from kiro_crew.config import loader as cfg_loader

    if hasattr(cfg_loader, "_config_dir_cache"):
        monkeypatch.setattr(cfg_loader, "_config_dir_cache", None, raising=False)
    assert mod._gateway_unit_name() == "kirocrew-pod@kirocrew-wt-feature.service"


def test_gateway_unit_resolves_live_outside_pods(monkeypatch, tmp_path):
    home = tmp_path / ".kirocrew"
    home.mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    assert mod._gateway_unit_name() == "kirocrew-gateway.service"


@pytest.mark.asyncio
async def test_run_records_authoritative_step_index(monkeypatch):
    """::step:: markers update run['step'] so a chatty build flooding the
    60-line output window cannot lose the phase (reattach correctness)."""
    lines = [b"::step::0::Pull\n", b"noise\n", b"::step::3::npm build\n"] + [b"asset line\n"] * 5

    class FakeStdout:
        def __init__(self):
            self._lines = list(lines)

        async def readline(self):
            return self._lines.pop(0) if self._lines else b""

    class FakeProc:
        stdout = FakeStdout()
        pid = 4242
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*a, **k):
        return FakeProc()

    monkeypatch.setattr(runtime_mod.asyncio, "create_subprocess_exec", fake_exec)
    rid = await mod._start_run("sync", ["true"], env={})
    for _ in range(50):
        # flake-ok: pre-existing bounded 50 ms poll of the run status, moved verbatim; on expiry the test reads the record as it stands and its assertions fail
        await runtime_mod.asyncio.sleep(0.05)
        async with mod._RUNS_LOCK:
            if mod._RUNS.get(rid, {}).get("status") == "done":
                break
    async with mod._RUNS_LOCK:
        run = dict(mod._RUNS[rid])
    assert run.get("step") == 3


@pytest.mark.asyncio
async def test_head_contained_when_ancestor(monkeypatch):
    """Local HEAD behind the merged PR head (remote gained commits pre-merge)
    is fully contained in the merge — removal must be allowed."""

    async def fake_run_cmd(cmd, **kw):
        assert cmd[3:5] == ["merge-base", "--is-ancestor"]
        return 0, "", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    assert await mod._head_contained_in_pr("/wt", "aaa", "bbb") is True


@pytest.mark.asyncio
async def test_head_not_contained_when_diverged(monkeypatch):
    async def fake_run_cmd(cmd, **kw):
        return 1, "", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    assert await mod._head_contained_in_pr("/wt", "aaa", "bbb") is False


@pytest.mark.asyncio
async def test_head_contained_equal_oids_no_spawn(monkeypatch):
    called = []

    async def fake_run_cmd(cmd, **kw):
        called.append(cmd)
        return 1, "", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    assert await mod._head_contained_in_pr("/wt", "same", "same") is True
    assert not called
