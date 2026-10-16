"""The Playwright CLI installer's success path, Node bootstrap and generated wrapper.

A successful install writes a wrapper pinned to its Node and the registry it was
told, a pinned version already present is not reinstalled, the Node bootstrap
verifies the download before it installs it, and the wrapper escapes every path it
embeds. Shared stubs, environments and constants live in
``playwright_cli_installer_helpers.py``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest
from installer_test_helpers import run_bounded
from playwright_cli_installer_helpers import (  # noqa: F401 -- fixtures are used by name
    EXIT_CODES,
    INSTALLER_SH,
    TESTED_NODE_VERSION,
    _expected_node_base,
    _fake_curl,
    _fake_node,
    _fake_npm_succeeding,
    _isolated_tool_env,
    _node_tarball,
    _run,
    node_mirror_fixture,
    posix_only,
    stubs_fixture,
)

# ── success path ─────────────────────────────────────────────────────


@posix_only
def test_successful_install_writes_a_node_pinning_wrapper(tmp_path: Path, stubs: Path) -> None:
    """The generated wrapper uses only the managed Node and package entry.

    npm's ``#!/usr/bin/env node`` shim would select an interpreter from the
    caller's PATH, which is both unreliable and unsafe for gateway-owned calls.
    """
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(tmp_path, stubs, "--version", "0.1.18")
    assert result.returncode == 0, result.stderr

    wrapper = tmp_path / "datahome" / "playwright-cli" / "managed-bin" / "playwright-cli"
    assert wrapper.exists()
    assert os.access(wrapper, os.X_OK)
    body = wrapper.read_text()
    managed_root = tmp_path / "datahome" / "playwright-cli"
    assert str(managed_root / "gateway-node") in body
    assert (
        str(managed_root / "lib" / "node_modules" / "@playwright" / "cli" / "playwright-cli.js")
        in body
    )
    assert str(stubs) not in body, "the wrapper must not retain the source Node path"
    assert "exec " in body
    # No half-written wrapper is left behind by the atomic move.
    assert not (wrapper.parent / "playwright-cli.incoming").exists()

    # The installer reports the version it got from the tool it just installed,
    # which is what proves the wrapper actually runs.
    assert "0.1.18" in result.stdout


@posix_only
def test_install_pins_the_public_registry_and_honors_flags(tmp_path: Path, stubs: Path) -> None:
    """An ambient .npmrc pointing at an expired private mirror is the most common
    enterprise failure, so the public registry is pinned by default rather than
    inherited; --isolated-npmrc goes further and ignores the config entirely."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        "--isolated-npmrc",
        "--skip-browsers",
        "--download-host",
        "https://playwright.internal.example/",
    )
    assert result.returncode == 0, result.stderr
    recorded = (tmp_path / "datahome" / "playwright-cli" / "npm-args").read_text()
    assert "registry=https://registry.npmjs.org/" in recorded
    # Two DISTINCT empty scope files, both inside the prefix. npm rejects one path
    # used as two scopes, so asserting a single sentinel here (as this once did)
    # pinned a flag that npm refuses outright.
    recorded_lines = recorded.splitlines()
    user_line = next(x for x in recorded_lines if x.startswith("userconfig="))
    global_line = next(x for x in recorded_lines if x.startswith("globalconfig="))
    assert user_line != global_line
    assert user_line.endswith("/isolated-npmrc/user")
    assert global_line.endswith("/isolated-npmrc/global")
    assert "skip_browsers=1" in recorded
    assert "download_host=https://playwright.internal.example/" in recorded
    assert "args=install --global @playwright/cli@0.1.18" in recorded


@posix_only
def test_a_corporate_registry_overrides_the_default(tmp_path: Path, stubs: Path) -> None:
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        "--registry",
        "https://npm.internal.example/",
    )
    assert result.returncode == 0, result.stderr
    recorded = (tmp_path / "datahome" / "playwright-cli" / "npm-args").read_text()
    assert "registry=https://npm.internal.example/" in recorded


@posix_only
def test_a_pinned_version_already_present_is_not_reinstalled(tmp_path: Path, stubs: Path) -> None:
    """Re-running the one-liner is the normal way users upgrade or repair, so the
    common case must not re-download an unchanged pinned version."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    first = _run(tmp_path, stubs, "--version", "0.1.18")
    assert first.returncode == 0, first.stderr

    marker = tmp_path / "datahome" / "playwright-cli" / "npm-args"
    marker.unlink()
    second = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        extra_env={"PWCLI_FAKE_INSTALLED_VERSION": "0.1.18"},
    )
    assert second.returncode == 0, second.stderr
    assert "already installed" in second.stdout
    assert not marker.exists(), "npm ran again despite the pinned version being present"

    forced = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        "--force",
        extra_env={"PWCLI_FAKE_INSTALLED_VERSION": "0.1.18"},
    )
    assert forced.returncode == 0, forced.stderr
    assert marker.exists(), "--force must reinstall"


@posix_only
def test_latest_is_never_treated_as_already_installed(tmp_path: Path, stubs: Path) -> None:
    """`latest` is a moving target: skipping the install because some version is
    present would silently pin the user to whatever they first installed."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(tmp_path, stubs, extra_env={"PWCLI_FAKE_INSTALLED_VERSION": "0.1.18"})
    assert result.returncode == 0, result.stderr
    assert "already installed" not in result.stdout
    assert (tmp_path / "datahome" / "playwright-cli" / "npm-args").exists()


@posix_only
def test_too_old_node_on_path_is_rejected_not_used(tmp_path: Path, stubs: Path) -> None:
    """An existing Node below the package's floor must trigger a bootstrap rather
    than being used and failing later with an opaque engine error."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(
        tmp_path,
        stubs,
        "--dry-run",
        extra_env={"PWCLI_FAKE_NODE_MAJOR": "16"},
    )
    assert result.returncode == 0, result.stderr
    assert "node           install" in result.stdout


@posix_only
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory permissions, so the refusal cannot be provoked",
)
def test_unwritable_prefix_is_reported_as_such(tmp_path: Path, stubs: Path) -> None:
    _fake_node(stubs)
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        result = _run(tmp_path, stubs, "--prefix", str(blocked / "prefix"))
        assert result.returncode == EXIT_CODES["not_writable"]
        assert "prefix" in result.stderr
    finally:
        blocked.chmod(0o700)


# ── Node bootstrap ───────────────────────────────────────────────────


@posix_only
def test_node_bootstrap_verifies_the_download_and_then_installs(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    """A bootstrapped Node is downloaded from the internet and then EXECUTED, so
    it is only ever trusted after its release manifest checksum matches."""
    base = _expected_node_base(TESTED_NODE_VERSION)
    archive = _node_tarball(tmp_path, base)
    shutil.copy(archive, node_mirror / archive.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (node_mirror / "SHASUMS256.txt").write_text(
        f"{'0' * 64}  node-v{TESTED_NODE_VERSION}-aix-ppc64.tar.gz\n" f"{digest}  {archive.name}\n"
    )
    _fake_curl(stubs, node_mirror)

    result = _run(
        tmp_path,
        stubs,
        "--node-mirror",
        "https://node.internal.example/dist",
        "--node-version",
        TESTED_NODE_VERSION,
        isolated=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "verified Node SHA-256" in result.stdout
    installed = tmp_path / "datahome" / "playwright-cli" / "node" / "bin" / "node"
    assert installed.exists()
    # An interrupted extract must not leave a tree the next run mistakes for a
    # complete install.
    assert not (tmp_path / "datahome" / "playwright-cli" / "node.incoming").exists()


@posix_only
def test_node_bootstrap_refuses_a_tampered_archive(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    base = _expected_node_base(TESTED_NODE_VERSION)
    archive = _node_tarball(tmp_path, base)
    shutil.copy(archive, node_mirror / archive.name)
    (node_mirror / "SHASUMS256.txt").write_text(f"{'0' * 64}  {archive.name}\n")
    _fake_curl(stubs, node_mirror)

    result = _run(
        tmp_path,
        stubs,
        "--node-mirror",
        "https://node.internal.example/dist",
        "--node-version",
        TESTED_NODE_VERSION,
        isolated=True,
    )
    assert result.returncode == EXIT_CODES["checksum"]
    assert "checksum mismatch" in result.stderr
    assert not (tmp_path / "datahome" / "playwright-cli" / "node").exists()


@posix_only
def test_node_bootstrap_refuses_an_archive_absent_from_the_manifest(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    """A manifest that simply omits our artifact must fail closed: treating a
    missing line as "nothing to check" would skip verification entirely."""
    base = _expected_node_base(TESTED_NODE_VERSION)
    archive = _node_tarball(tmp_path, base)
    shutil.copy(archive, node_mirror / archive.name)
    other = f"node-v{TESTED_NODE_VERSION}-aix-ppc64.tar.gz"
    assert other != archive.name
    (node_mirror / "SHASUMS256.txt").write_text(f"{'0' * 64}  {other}\n")
    _fake_curl(stubs, node_mirror)

    result = _run(
        tmp_path,
        stubs,
        "--node-mirror",
        "https://node.internal.example/dist",
        "--node-version",
        TESTED_NODE_VERSION,
        isolated=True,
    )
    assert result.returncode == EXIT_CODES["checksum"]
    assert "absent from" in result.stderr


@posix_only
def test_unreachable_node_mirror_is_reported_with_the_mirror_flag(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    _fake_curl(stubs, node_mirror)  # serves nothing
    result = _run(
        tmp_path,
        stubs,
        "--node-mirror",
        "https://node.internal.example/dist",
        isolated=True,
    )
    assert result.returncode == EXIT_CODES["node_bootstrap"]
    assert "--node-mirror" in result.stderr


def test_musl_and_old_glibc_hosts_select_an_executable_node_build() -> None:
    """nodejs.org publishes no musl build and nothing for pre-2.28 glibc: an
    official tarball on Alpine reports "not found", and on a RHEL 7-era host
    fails a GLIBC_2.28 symbol lookup. Both need the unofficial-builds mirror."""
    body = INSTALLER_SH.read_text(encoding="utf-8")
    selector = body.split("_resolve_node_artifact() {", 1)[1].split("\n}", 1)[0]
    assert "musl" in selector
    assert "glibc-217" in selector
    assert "NODE_UNOFFICIAL_MIRROR" in selector
    # The glibc-217 variant is published for x64 only, so selecting it on arm64
    # would 404 on a URL that never existed.
    assert 'NODE_ARCH" = "x64"' in selector


def test_an_unpublished_platform_pair_is_named_not_reported_as_a_network_fault() -> None:
    """No musl build exists for armv7l at any Node version. Letting that 404 come
    back as "check proxy access" sends the user to debug a firewall that is
    working, so the combination is refused up front -- and it must be refused on
    BOTH paths that can reach a download, including --dry-run's report."""
    body = INSTALLER_SH.read_text(encoding="utf-8")
    guard = body.split("_assert_bootstrap_supported() {", 1)[1].split("\n}", 1)[0]
    assert "armv7l" in guard and "musl" in guard
    assert "EX_MISSING_TOOL" in guard
    assert body.count("_assert_bootstrap_supported\n") >= 2


# ── the wrapper is generated code, so its inputs must be escaped ─────


@posix_only
@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param("pre fix", id="space"),
        pytest.param("pre$(touch INJECTED)fix", id="command-substitution"),
        pytest.param("pre`touch INJECTED`fix", id="backtick"),
        pytest.param("pre'quote'fix", id="single-quote"),
        pytest.param("pre$HOMEfix", id="variable-expansion"),
    ],
)
def test_wrapper_generation_escapes_the_paths_it_embeds(
    tmp_path: Path, stubs: Path, hostile: str
) -> None:
    """The wrapper is a shell script this installer WRITES, so every path
    interpolated into it is code. Unescaped, a directory containing a space
    merely produces a broken wrapper, while one containing $(...) or a backtick
    executes when the wrapper is invoked."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    prefix = tmp_path / hostile / "prefix"
    bindir = tmp_path / "bin"
    # Both child processes run with their CWD inside tmp_path. A payload that
    # escapes would `touch INJECTED` relative to the CWD, so anchoring it here is
    # what puts the artifact where the assertion below looks. Left at the default
    # the marker landed in the repo root instead, and this test passed while its
    # own payload had executed.
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        "--prefix",
        str(prefix),
        "--bin-dir",
        str(bindir),
        cwd=workdir,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    wrapper = bindir / "playwright-cli"
    assert wrapper.exists()
    # Nothing embedded in the wrapper may have executed during generation or
    # during the installer's own verification run.
    assert not list(tmp_path.rglob("INJECTED"))
    # And the wrapper must actually work, which is what proves the escaping
    # preserved the path rather than merely neutering it.
    # Bounded and given an isolated environment: this spawns the wrapper, which
    # spawns node, so an unbounded call leaves a process tree behind on hang.
    ran = run_bounded(
        [str(wrapper), "--version"],
        env=_isolated_tool_env(tmp_path),
        cwd=str(workdir),
        timeout=120,
    )
    assert ran.returncode == 0, ran.stderr
    assert "0.1.18" in ran.stdout
    # Re-checked after the wrapper ran: the exec line is the other place an
    # unescaped path would fire.
    assert not list(tmp_path.rglob("INJECTED"))
