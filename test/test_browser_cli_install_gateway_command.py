"""The command and seams the gateway runs the vetted browser CLI through.

The sealed Node-plus-JS command on Windows, POSIX and system installs, the staged Node
and its runtime-executable probe, and the lifecycle-env and dashboard-layout seam
verdicts read from the serving package. Detection and install sequencing live in
``test_browser_cli_install.py``; the shared autouse pins and fakes in
``browser_cli_install_helpers.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from browser_cli_install_helpers import _clear_node_bin_dir_caches  # noqa: F401
from browser_cli_install_helpers import _default_no_os_deps  # noqa: F401
from browser_cli_install_helpers import isolated_browser_cache_fixture  # noqa: F401

from kiro_crew.browser_cli import install as mod
from kiro_crew.browser_cli import install_launcher as launcher


class TestWindowsGatewayCommand:
    @staticmethod
    def _managed_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
        crew = tmp_path / "crew"
        root = crew / "playwright-cli"
        package = root / "node_modules" / "@playwright" / "cli"
        package.mkdir(parents=True)
        cmd = root / "playwright-cli.cmd"
        cmd.write_text("@echo off\n", encoding="utf-8")
        cmd.chmod(0o755)
        node = root / "node.exe"
        node.write_bytes(b"node")
        node.chmod(0o755)
        entry = package / "playwright-cli.js"
        entry.write_text("// cli\n", encoding="utf-8")
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        monkeypatch.setattr(mod, "_system_cli_candidates", lambda: ())
        monkeypatch.setattr(mod, "_agent_writable_roots", lambda: ())
        monkeypatch.setattr(mod, "_warned_cli_refusals", set())
        return root, node, entry

    def test_gateway_invocation_uses_node_and_javascript_not_cmd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _root, node, entry = self._managed_tree(tmp_path, monkeypatch)

        command = mod.cli_command()

        assert command == [str(node.resolve()), str(entry.resolve())]
        assert not any(part.casefold().endswith((".cmd", ".bat")) for part in command)

    def test_javascript_entrypoint_cannot_escape_the_sealed_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _root, _node, entry = self._managed_tree(tmp_path, monkeypatch)
        outside = tmp_path / "outside.js"
        outside.write_text("// attacker\n", encoding="utf-8")
        entry.unlink()
        entry.symlink_to(outside)

        assert mod.cli_command() is None

    def test_install_stages_node_inside_the_managed_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crew = tmp_path / "crew"
        source = tmp_path / "node.exe"
        source.write_bytes(b"trusted node")
        source.chmod(0o755)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        probed: list[str] = []
        probe_cwds: list[str | None] = []

        def fake_run(argv, timeout, *, cwd=None):
            probed.append(argv[0])
            probe_cwds.append(cwd)
            return (0, "v24.18.0\n", "")

        monkeypatch.setattr(mod, "_run", fake_run)

        staged = mod._stage_managed_node(str(source))

        assert staged == crew / "playwright-cli" / "node.exe"
        assert staged.read_bytes() == b"trusted node"
        # The smoke run exercised the staged copy inside the leaf, not the source,
        # and ran FROM the leaf rather than from wherever the gateway was started.
        assert probed and Path(probed[-1]).parent == staged.parent
        assert Path(probed[-1]) != source
        assert probe_cwds[-1] == str(staged.parent)

        source.write_bytes(b"replacement node")
        replaced = mod._stage_managed_node(str(source))

        assert replaced == staged
        assert replaced.read_bytes() == b"replacement node"

    def test_install_replaces_a_running_node_exe_on_windows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reinstall while a managed ``node.exe`` is still running must not die
        at stage-node. Windows refuses to rename over a running executable, so
        the first replace raises ``PermissionError`` ("Access is denied"); the
        running image is then renamed aside (which Windows allows), freeing the
        name, and the staged copy lands. The real ``os.replace`` runs the swap
        here -- only the first overwrite of the destination is forced to raise."""
        crew = tmp_path / "crew"
        source = tmp_path / "node.exe"
        source.write_bytes(b"new node")
        source.chmod(0o755)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        monkeypatch.setattr(mod, "_run", lambda argv, timeout, cwd=None: (0, "v24.18.0\n", ""))

        destination = crew / "playwright-cli" / "node.exe"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"running node")

        real_replace = os.replace
        refused: dict[str, int] = {"count": 0}

        def flaky_replace(src, dst, *args, **kwargs):
            # Only the first attempt to overwrite the running destination is the
            # Windows "Access is denied" the running image produces.
            if str(dst) == str(destination) and refused["count"] == 0:
                refused["count"] += 1
                raise PermissionError(13, "Access is denied")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(launcher.os, "replace", flaky_replace)

        staged = mod._stage_managed_node(str(source))

        assert staged == destination
        assert staged.read_bytes() == b"new node"
        assert refused["count"] == 1, "the running-image overwrite was exercised"
        # No dead sidecar is left behind: the retired running image was removed.
        assert not list(destination.parent.glob(".node.exe.*.old"))

    def test_two_swaps_in_one_process_use_distinct_retired_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two reinstalls in one gateway process, each hitting the running-image
        lock, must not collide on the retired sidecar name. If the first retired
        node.exe is still running (its best-effort delete failed, so it stays on
        disk), a pid-only name would make the second move-aside try to overwrite
        that running retired image and fail. A unique token per swap keeps the
        second move-aside landing on a free name."""
        crew = tmp_path / "crew"
        source = tmp_path / "node.exe"
        source.write_bytes(b"gen1")
        source.chmod(0o755)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        monkeypatch.setattr(mod, "_run", lambda argv, timeout, cwd=None: (0, "v24.18.0\n", ""))

        destination = crew / "playwright-cli" / "node.exe"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"gen0")

        real_replace = os.replace
        locked_retired: set[str] = set()

        def flaky_replace(src, dst, *args, **kwargs):
            # Overwriting a node.exe that is still present (the running image) is
            # locked, forcing the move-aside. Once it has been renamed away the
            # name is free, so the staged copy's move-in succeeds. A retired
            # sidecar that is "still running" cannot be overwritten either.
            if str(dst) == str(destination) and str(src) != str(destination) and Path(dst).exists():
                raise PermissionError(13, "Access is denied")
            if str(dst) in locked_retired:
                raise PermissionError(13, "Access is denied")
            return real_replace(src, dst, *args, **kwargs)

        real_unlink = os.unlink

        def locking_unlink(path, *args, **kwargs):
            # A retired running node.exe cannot be deleted: it stays on disk and
            # its name stays locked against a later overwrite. Any other unlink
            # (temp cleanup, tmp_path teardown via dir_fd) is left real.
            if str(path).endswith(".old") and not args and not kwargs:
                locked_retired.add(str(path))
                return None
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(launcher.os, "replace", flaky_replace)
        monkeypatch.setattr(launcher.os, "unlink", locking_unlink)

        mod._stage_managed_node(str(source))
        assert destination.read_bytes() == b"gen1"

        source.write_bytes(b"gen2")
        mod._stage_managed_node(str(source))
        assert destination.read_bytes() == b"gen2"

        # Two distinct retired sidecars survive -- no name was reused.
        sidecars = sorted(p.name for p in destination.parent.glob(".node.exe.*.old"))
        assert len(sidecars) == 2, sidecars
        assert len(set(sidecars)) == 2, "each swap retired to its own name"

    def test_stage_clears_a_leftover_retired_node_image(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A prior swap left a ``.node.exe.<pid>.old`` the running process held.
        The next stage clears it before staging, never touching live node.exe."""
        crew = tmp_path / "crew"
        source = tmp_path / "node.exe"
        source.write_bytes(b"fresh node")
        source.chmod(0o755)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        monkeypatch.setattr(mod, "_run", lambda argv, timeout, cwd=None: (0, "v24.18.0\n", ""))

        leaf = crew / "playwright-cli"
        leaf.mkdir(parents=True)
        stale = leaf / ".node.exe.4242.old"
        stale.write_bytes(b"dead image")

        mod._stage_managed_node(str(source))

        assert not stale.exists()
        assert (leaf / "node.exe").read_bytes() == b"fresh node"

    def test_install_names_the_running_node_when_the_swap_cannot_proceed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If even renaming the running image aside fails, the destination is
        genuinely pinned. The error names the running ``node.exe`` and the
        remedy instead of propagating a bare "Access is denied"."""
        crew = tmp_path / "crew"
        source = tmp_path / "node.exe"
        source.write_bytes(b"new node")
        source.chmod(0o755)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        monkeypatch.setattr(mod, "_run", lambda argv, timeout, cwd=None: (0, "v24.18.0\n", ""))

        destination = crew / "playwright-cli" / "node.exe"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"running node")

        def always_refuse_destination(src, dst, *args, **kwargs):
            # Overwriting node.exe AND moving it aside both fail: the whole leaf
            # is locked, as a directory held open on Windows would be.
            if str(src) == str(destination) or str(dst) == str(destination):
                raise PermissionError(13, "Access is denied")
            return os.replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(launcher.os, "replace", always_refuse_destination)

        with pytest.raises(OSError) as excinfo:
            mod._stage_managed_node(str(source))

        message = str(excinfo.value)
        assert str(destination) in message
        assert "is in use" in message
        assert "install the browser" in message
        # The surfaced error is actionable, not the bare OS text.
        assert message != "Access is denied"
        # The running image was left in place; no half-done swap.
        assert destination.read_bytes() == b"running node"

    def test_posix_staging_never_renames_the_destination_aside(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-Windows behaviour is unchanged: a single plain ``os.replace`` and
        no retired-image sidecar, because POSIX renames over an open executable."""
        crew = tmp_path / "crew"
        source = tmp_path / "node"
        source.write_bytes(b"new node")
        source.chmod(0o755)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        monkeypatch.setattr(mod, "_run", lambda argv, timeout, cwd=None: (0, "v24.18.0\n", ""))

        destination = crew / "playwright-cli" / "gateway-node"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"running node")

        replace_dests: list[str] = []
        real_replace = os.replace

        def record_replace(src, dst, *args, **kwargs):
            replace_dests.append(str(dst))
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(launcher.os, "replace", record_replace)

        mod._stage_managed_node(str(source))

        assert destination.read_bytes() == b"new node"
        assert replace_dests == [str(destination)], "exactly one direct replace, no aside"
        assert not list(destination.parent.glob(".gateway-node.*.old"))


def _attributed_lifecycle_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A launcher, and the registry/bundle of the package attributable to it.

    A real-FILE launcher beside its install prefix's ``node_modules``, which is
    the shape ``_launcher_node_modules`` resolves. Deliberately not a symlink, so
    the seam tests need no symlink privilege and run on every platform.
    """
    prefix = tmp_path / "prefix"
    package = prefix / "node_modules" / "@playwright" / "cli"
    core = package / "node_modules" / "playwright-core"
    (core / "lib" / "tools" / "cli-client").mkdir(parents=True)
    (core / "browsers.json").write_text('{"browsers": []}', encoding="utf-8")
    launcher = prefix / "bin" / "playwright-cli"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    return (
        launcher,
        core / "lib" / "tools" / "cli-client" / "registry.js",
        core / "lib" / "coreBundle.js",
    )


def _lifecycle_contract_tree(tmp_path: Path) -> tuple[Path, Path]:
    package = tmp_path / "node_modules" / "@playwright" / "cli"
    core = package / "node_modules" / "playwright-core"
    (core / "lib" / "tools" / "cli-client").mkdir(parents=True)
    (core / "browsers.json").write_text('{"browsers": []}', encoding="utf-8")
    registry = core / "lib" / "tools" / "cli-client" / "registry.js"
    bundle = core / "lib" / "coreBundle.js"
    return package, registry, bundle


def test_lifecycle_env_contract_is_confirmed_from_serving_cli_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher, registry, bundle = _attributed_lifecycle_tree(tmp_path)
    registry.write_text("process.env.PWTEST_DAEMON_SESSION_DIR", encoding="utf-8")
    bundle.write_text("process.env.PWTEST_SOCKETS_DIR || os.tmpdir()", encoding="utf-8")
    monkeypatch.setattr(mod, "cli_path", lambda: str(launcher))
    mod._source_contains.cache_clear()

    assert mod.cli_lifecycle_env_support() == (mod.SeamSupport.SUPPORTED, "")


def test_lifecycle_env_contract_fails_when_upstream_hook_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher, registry, bundle = _attributed_lifecycle_tree(tmp_path)
    registry.write_text("process.env.PWTEST_DAEMON_SESSION_DIR", encoding="utf-8")
    bundle.write_text("// lifecycle socket override removed upstream", encoding="utf-8")
    monkeypatch.setattr(mod, "cli_path", lambda: str(launcher))
    mod._source_contains.cache_clear()

    assert mod.cli_lifecycle_env_support()[0] is mod.SeamSupport.UNSUPPORTED


def test_lifecycle_contract_never_falls_through_to_a_stale_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launcher's own package answers for it, or nothing does.

    The standalone install prefix is a legitimate fallback for a revision lookup
    but not for a seam verdict: it describes a DIFFERENT install, so letting it
    answer for an unattributable launcher would put the original misreport back
    in a narrower case. Here the standalone package is healthy and the launcher
    is unattributable -- the verdict must be UNVERIFIED, not SUPPORTED.
    """
    _launcher, standalone_registry, standalone_bundle = _attributed_lifecycle_tree(tmp_path)
    standalone_registry.write_text("process.env.PWTEST_DAEMON_SESSION_DIR", encoding="utf-8")
    standalone_bundle.write_text("process.env.PWTEST_SOCKETS_DIR || os.tmpdir()", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PLAYWRIGHT_CLI_HOME", str(tmp_path / "prefix"))
    orphan = tmp_path / "elsewhere" / "playwright-cli"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(mod, "cli_path", lambda: str(orphan))
    mod._source_contains.cache_clear()

    # The standalone package on its own would answer SUPPORTED.
    assert mod._cli_package_dirs()
    support, detail = mod.cli_lifecycle_env_support()

    assert support is mod.SeamSupport.UNVERIFIED
    assert str(orphan) in detail


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink launcher shape")
def test_a_symlink_launcher_resolving_into_the_package_is_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape that DOES attribute by ANCESTRY: an ``npm install -g`` symlink.

    The launcher resolves INTO the package tree, so an ancestor is the package
    and no ``node_modules`` probe is needed. Its sibling above covers the
    real-file shape, which reaches the package the other way.
    """
    package, registry, bundle = _lifecycle_contract_tree(tmp_path)
    registry.write_text("process.env.PWTEST_DAEMON_SESSION_DIR", encoding="utf-8")
    bundle.write_text("process.env.PWTEST_SOCKETS_DIR || os.tmpdir()", encoding="utf-8")
    entrypoint = package / "cli.js"
    entrypoint.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    launcher = tmp_path / "bin" / "playwright-cli"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(entrypoint)
    monkeypatch.setenv("KIROCREW_PLAYWRIGHT_CLI_HOME", str(tmp_path / "absent-prefix"))
    monkeypatch.setattr(mod, "cli_path", lambda: str(launcher))
    mod._source_contains.cache_clear()

    assert mod.cli_lifecycle_env_support() == (mod.SeamSupport.SUPPORTED, "")


def test_an_unattributable_launcher_is_unverified_and_names_the_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version-manager shim resolves to itself, not into any package tree.

    No bundle is read, so the CLI's capability is unknown. Reporting that as
    UNSUPPORTED is what sent an operator to upgrade a CLI that carried both
    hooks; the verdict must name the launcher it could not attribute instead.
    """
    shim = tmp_path / "shims" / "playwright-cli"
    shim.parent.mkdir(parents=True)
    shim.write_text('#!/bin/sh\nexec versionctl exec -- "$0" "$@"\n', encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PLAYWRIGHT_CLI_HOME", str(tmp_path / "absent-prefix"))
    monkeypatch.setattr(mod, "cli_path", lambda: str(shim))

    support, detail = mod.cli_lifecycle_env_support()

    assert support is mod.SeamSupport.UNVERIFIED
    assert str(shim) in detail
    assert "attributable" in detail


def test_no_resolved_launcher_is_unverified_rather_than_a_capability_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "cli_path", lambda: None)
    monkeypatch.setenv("KIROCREW_PLAYWRIGHT_CLI_HOME", "/nonexistent-prefix")

    support, detail = mod.cli_lifecycle_env_support()

    assert support is mod.SeamSupport.UNVERIFIED
    assert "no trusted playwright-cli launcher" in detail


def test_a_package_without_a_serving_core_tree_is_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attribution reached the CLI package but no ``playwright-core`` serves it."""
    prefix = tmp_path / "prefix"
    package = prefix / "node_modules" / "@playwright" / "cli"
    package.mkdir(parents=True)
    launcher = prefix / "bin" / "playwright-cli"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(mod, "cli_path", lambda: str(launcher))

    support, detail = mod.cli_lifecycle_env_support()

    assert support is mod.SeamSupport.UNVERIFIED
    assert str(package) in detail


def test_an_unreadable_bundle_is_unverified_rather_than_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle that cannot be stat'd was never searched for the hook."""
    launcher, registry, _bundle = _attributed_lifecycle_tree(tmp_path)
    registry.write_text("process.env.PWTEST_DAEMON_SESSION_DIR", encoding="utf-8")
    monkeypatch.setattr(mod, "cli_path", lambda: str(launcher))
    mod._source_contains.cache_clear()

    support, detail = mod.cli_lifecycle_env_support()

    assert support is mod.SeamSupport.UNVERIFIED
    assert "could not be read" in detail


@pytest.mark.parametrize(
    ("size", "why"),
    [(0, "an empty bundle is not a bundle without the needle"), (None, "past the size ceiling")],
)
def test_a_bundle_the_reader_refuses_is_unverified_not_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int | None, why: str
) -> None:
    """The size guard REFUSES to read; a refusal measures nothing.

    Reporting it as UNSUPPORTED is the same defect as an unattributable
    launcher: a claim about bytes that were never searched.
    """
    launcher, registry, bundle = _attributed_lifecycle_tree(tmp_path)
    registry.write_text("process.env.PWTEST_DAEMON_SESSION_DIR", encoding="utf-8")
    bundle.write_text("" if size == 0 else "process.env.PWTEST_SOCKETS_DIR || x", encoding="utf-8")
    if size is None:
        monkeypatch.setattr(mod, "_LIFECYCLE_SOURCE_MAX_BYTES", 1)
    monkeypatch.setattr(mod, "cli_path", lambda: str(launcher))
    mod._source_contains.cache_clear()

    support, detail = mod.cli_lifecycle_env_support()

    assert support is mod.SeamSupport.UNVERIFIED, why
    assert "could not be read" in detail


def test_the_dashboard_layout_gate_separates_attribution_from_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sibling gate carries the same three-valued contract."""
    launcher, _registry, bundle = _attributed_lifecycle_tree(tmp_path)
    monkeypatch.setattr(mod, "cli_path", lambda: str(launcher))

    bundle.write_text(
        'makeSocketPath("dashboard", "app"); process.env.PWTEST_SOCKETS_DIR || x',
        encoding="utf-8",
    )
    mod._source_contains.cache_clear()
    assert mod.cli_dashboard_socket_support() == (mod.SeamSupport.SUPPORTED, "")

    bundle.write_text("// dashboard socket layout renamed upstream", encoding="utf-8")
    mod._source_contains.cache_clear()
    assert mod.cli_dashboard_socket_support()[0] is mod.SeamSupport.UNSUPPORTED

    monkeypatch.setattr(mod, "cli_path", lambda: None)
    assert mod.cli_dashboard_socket_support()[0] is mod.SeamSupport.UNVERIFIED


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable and permission semantics")
class TestPosixGatewayCommand:
    @staticmethod
    def _managed_tree(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Path, Path, Path]:
        crew = tmp_path / "crew"
        root = crew / "playwright-cli"
        package = root / "lib" / "node_modules" / "@playwright" / "cli"
        package.mkdir(parents=True)
        wrapper = root / "managed-bin" / "playwright-cli"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("#!/bin/sh\nexec /agent/node ...\n", encoding="utf-8")
        wrapper.chmod(0o755)
        node = root / "gateway-node"
        node.write_bytes(b"node")
        node.chmod(0o755)
        entry = package / "playwright-cli.js"
        entry.write_text("// cli\n", encoding="utf-8")
        attacker_node = tmp_path / "agent-bin" / "node"
        attacker_node.parent.mkdir(parents=True)
        attacker_node.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        attacker_node.chmod(0o755)
        monkeypatch.setenv("PATH", str(attacker_node.parent))
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        monkeypatch.setattr(mod, "_system_cli_candidates", lambda: ())
        monkeypatch.setattr(mod, "_agent_writable_roots", lambda: ())
        monkeypatch.setattr(mod, "_warned_cli_refusals", set())
        return wrapper, node, entry, attacker_node

    def test_gateway_invocation_uses_sealed_node_and_javascript_not_the_wrapper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wrapper, node, entry, attacker_node = self._managed_tree(tmp_path, monkeypatch)

        command = mod.cli_command()

        assert command == [str(node.resolve()), str(entry.resolve())]
        assert str(wrapper.resolve()) not in command
        assert str(attacker_node.resolve()) not in command

    def test_javascript_entrypoint_cannot_escape_the_sealed_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wrapper, _node, entry, _attacker_node = self._managed_tree(tmp_path, monkeypatch)
        outside = tmp_path / "outside.js"
        outside.write_text("// attacker\n", encoding="utf-8")
        entry.unlink()
        entry.symlink_to(outside)

        assert mod.cli_command() is None

    def test_install_stages_node_inside_the_managed_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crew = tmp_path / "crew"
        source = tmp_path / "node"
        source.write_bytes(b"trusted node")
        source.chmod(0o755)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        monkeypatch.setattr(mod, "_run", lambda argv, timeout, cwd=None: (0, "v24.18.0\n", ""))

        staged = mod._stage_managed_node(str(source))

        assert staged == crew / "playwright-cli" / "gateway-node"
        assert staged.read_bytes() == b"trusted node"
        assert staged.stat().st_mode & 0o777 == 0o500

    def test_install_refuses_a_launcher_script_that_cannot_run_from_the_leaf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``bin/node`` that is a shell script locates the binary it starts
        relative to its own path. It copies cleanly and passes the mode checks,
        but a copy in the managed leaf points at a file that is not there. The
        smoke run is a REAL process here: the copied script runs from the leaf,
        its target is missing, and the install refuses, naming the source. The
        process is the test's own shell script, never a host ``node``, and both
        spawns run in a directory the test owns."""
        crew = tmp_path / "crew"
        bin_dir = tmp_path / "node" / "bin"
        bin_dir.mkdir(parents=True)
        wrapper = bin_dir / "node"
        wrapper.write_text('#!/bin/sh\nexec "$(dirname "$0")/node_real" "$@"\n')
        wrapper.chmod(0o755)
        real = bin_dir / "node_real"
        real.write_text("#!/bin/sh\necho v24.18.0\n")
        real.chmod(0o755)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)

        # In place the wrapper works; that is what fooled the mode checks.
        assert mod._run([str(wrapper), "--version"], 20.0, cwd=str(tmp_path))[0] == 0

        with pytest.raises(OSError) as excinfo:
            mod._stage_managed_node(str(wrapper))

        message = str(excinfo.value)
        assert str(wrapper) in message
        assert "does not run from the managed leaf" in message
        # The copied script looked for its sibling next to ITSELF, i.e. inside
        # the leaf. The shell's wording differs (dash: "not found", bash: "No such
        # file or directory"); the path it names is the shell-independent fact.
        assert str(crew / "playwright-cli" / "node_real") in message

        leaf = crew / "playwright-cli"
        assert not (leaf / "gateway-node").exists()
        assert not list(leaf.glob(".gateway-node-*.incoming")), "dead copy left in the leaf"

    def test_install_refuses_a_binary_that_only_runs_under_its_wrapper_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Some distributions wrap Node to inject ``LD_LIBRARY_PATH`` for a
        bundled ``libstdc++``. The kernel names the real binary, it is a plain
        executable with no ``#!``, and it still cannot run outside the wrapper.
        Only running the staged copy catches that class, so the smoke run is
        what refuses it; a magic-bytes check would have staged a dead file."""
        crew = tmp_path / "crew"
        binary = tmp_path / "node_real"
        binary.write_bytes(b"\x7fELF needs the wrapper's LD_LIBRARY_PATH")
        binary.chmod(0o755)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv, timeout, cwd=None: (
                127,
                "",
                "error while loading shared libraries: libstdc++.so.6",
            ),
        )

        with pytest.raises(
            OSError, match=r"does not run from the managed leaf \(exit 127: .*libstdc\+\+"
        ):
            mod._stage_managed_node(str(binary))

        assert not (crew / "playwright-cli" / "gateway-node").exists()

    def test_install_refuses_a_copy_that_runs_but_is_not_node(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit 0 with no version banner is not a Node either."""
        crew = tmp_path / "crew"
        binary = tmp_path / "node"
        binary.write_bytes(b"\x7fELF something else")
        binary.chmod(0o755)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        monkeypatch.setattr(
            mod, "_run", lambda argv, timeout, cwd=None: (0, "usage: not-node\n", "")
        )

        with pytest.raises(OSError, match=r"did not report a version"):
            mod._stage_managed_node(str(binary))

        assert not (crew / "playwright-cli" / "gateway-node").exists()

    # What dyld prints for Homebrew's split-libnode node run outside its Cellar:
    # the library on the first line, the paths it tried after it.
    _DYLD_SPLIT_LIBNODE = (
        "#!/bin/sh\n"
        "echo 'dyld[4242]: Library not loaded: @rpath/libnode.147.dylib' >&2\n"
        "echo '  Referenced from: <ABCD> /opt/homebrew/Cellar/node/26.11.1/bin/node' >&2\n"
        'echo "  Reason: tried: \'$(dirname "$0")/../lib/libnode.147.dylib\' (no such file)" >&2\n'
        "exit 134\n"
    )

    def test_install_names_a_missing_shared_library_and_how_to_get_a_node_that_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Homebrew's node links libnode from its Cellar, so no copy of it runs
        from the leaf. The refusal names the library (not dyld's last "tried"
        line) and the remedy that works: a self-contained Node selected through
        KIROCREW_NODE_BIN_DIR. The probe is a REAL process: the test's own shell
        script, run from a directory the test owns."""
        crew = tmp_path / "crew"
        source = tmp_path / "Cellar" / "node" / "bin" / "node"
        source.parent.mkdir(parents=True)
        source.write_text(self._DYLD_SPLIT_LIBNODE, encoding="utf-8")
        source.chmod(0o755)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)

        with pytest.raises(OSError) as excinfo:
            mod._stage_managed_node(str(source))

        message = str(excinfo.value)
        assert "exit 134: dyld[4242]: Library not loaded: @rpath/libnode.147.dylib" in message
        assert "loads @rpath/libnode.147.dylib from its own install tree" in message
        assert "such as the official build from nodejs.org, set KIROCREW_NODE_BIN_DIR" in message
        assert not (crew / "playwright-cli" / "gateway-node").exists()

    def test_install_browser_reports_a_staged_node_that_cannot_run_before_downloading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A gateway-node staged before stage-node probed its copy (0.8.x) is
        still vetted by location only. install-browser runs it first and reports
        it, instead of letting the download die in a loader crash."""
        _wrapper, node, _entry, _attacker = self._managed_tree(tmp_path, monkeypatch)
        node.write_text(self._DYLD_SPLIT_LIBNODE, encoding="utf-8")
        node.chmod(0o755)
        downloads: list[list[str]] = []
        monkeypatch.setattr(
            mod, "_download_browser", lambda command, engine=None: downloads.append(command) or []
        )

        result = mod.install_browser("chromium")

        assert result["ok"] is False
        assert [s["name"] for s in result["steps"]] == ["check-node"]
        detail = result["steps"][0]["stderr"]
        assert "does not run" in detail
        assert "@rpath/libnode.147.dylib" in detail
        assert "KIROCREW_NODE_BIN_DIR" in detail
        assert downloads == [], "the download must not start on a Node that cannot run"

    @pytest.mark.parametrize("probe_rc", [mod.TIMEOUT_RC, mod.INTERRUPTED_RC])
    def test_a_probe_that_did_not_finish_keeps_its_returncode_and_no_node_remedy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe_rc: int
    ) -> None:
        """install_job.outcome_of reads the last step's returncode to tell a
        timeout or an interruption from a failed step, so the check must not
        flatten either into 127, nor tell a user whose Node works to replace it."""
        self._managed_tree(tmp_path, monkeypatch)
        monkeypatch.setattr(
            mod, "_run", lambda argv, timeout, cwd=None: (probe_rc, "", "timed out after 20s")
        )
        downloads: list[list[str]] = []
        monkeypatch.setattr(
            mod, "_download_browser", lambda command, engine=None: downloads.append(command) or []
        )

        result = mod.install_browser("chromium")

        assert result["ok"] is False
        step = result["steps"][-1]
        assert step["name"] == "check-node"
        assert step["returncode"] == probe_rc
        assert "did not finish" in step["stderr"]
        assert "KIROCREW_NODE_BIN_DIR" not in step["stderr"]
        assert "self-contained" not in step["stderr"]
        assert downloads == []

    def test_install_browser_downloads_with_a_staged_node_that_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wrapper, node, entry, _attacker = self._managed_tree(tmp_path, monkeypatch)
        node.write_text("#!/bin/sh\necho v24.22.0\n", encoding="utf-8")
        node.chmod(0o755)
        downloads: list[list[str]] = []

        def fake_download(command: list[str], engine: str | None = None) -> list[dict]:
            downloads.append(command)
            return [{"name": "install-browser", "ok": True, "returncode": 0}]

        monkeypatch.setattr(mod, "_download_browser", fake_download)

        result = mod.install_browser("chromium")

        assert result["ok"] is True
        assert downloads == [[str(node.resolve()), str(entry.resolve())]]


class TestNodeRuntimeExecutableProbe:
    """The staged binary is the one the KERNEL says is running, not the one
    Node names: ``process.execPath`` is argv[0], and a launcher script that
    starts the binary with ``exec -a <script>`` makes Node report the script."""

    def _resolved(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        binary = tmp_path / "bin" / "node_real"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"\x7fELF native node")
        binary.chmod(0o755)
        monkeypatch.setattr(mod, "_run", lambda argv, timeout: (0, f"{binary}\n", ""))
        return binary

    def test_probe_asks_the_kernel_first_and_falls_back_to_execpath(self) -> None:
        probe = mod._NODE_EXECUTABLE_PROBE
        assert "realpathSync('/proc/self/exe')" in probe
        assert probe.index("/proc/self/exe") < probe.index("process.execPath")

    def test_probe_output_is_resolved_as_an_executable_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binary = self._resolved(tmp_path, monkeypatch)
        seen: list[list[str]] = []

        def run(argv: list[str], timeout: float) -> tuple[int, str, str]:
            seen.append(argv)
            return (0, f"{binary}\n", "")

        monkeypatch.setattr(mod, "_run", run)

        assert mod._node_runtime_executable("node") == str(binary.resolve())
        assert seen == [["node", "-p", mod._NODE_EXECUTABLE_PROBE]]

    def test_probe_rejects_a_relative_or_multiline_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "_run", lambda argv, timeout: (0, "node_real\n", ""))
        assert mod._node_runtime_executable("node") is None
        monkeypatch.setattr(mod, "_run", lambda argv, timeout: (0, "/a\n/b\n", ""))
        assert mod._node_runtime_executable("node") is None


class TestSystemGatewayCommand:
    def test_posix_node_candidates_never_consume_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launcher = tmp_path / "system" / "playwright-cli"
        package = tmp_path / "system" / "lib" / "node_modules" / "@playwright" / "cli"
        package.mkdir(parents=True)
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        attacker = tmp_path / "agent-bin" / "node"
        attacker.parent.mkdir()
        attacker.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("PATH", str(attacker.parent))
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(mod.platform_compat, "trusted_system_path", lambda: "/usr/bin")
        monkeypatch.setattr(mod.github_runner, "PROVIDER_EXECUTABLE_DIRS", ())

        candidates = mod._system_node_candidates(launcher, package)

        assert Path("/usr/bin/node") in candidates
        assert attacker not in candidates

    def test_a_path_only_node_cannot_complete_a_system_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launcher = tmp_path / "system" / "playwright-cli"
        launcher.parent.mkdir()
        launcher.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        launcher.chmod(0o755)
        package = tmp_path / "system" / "package"
        package.mkdir()
        entry = package / "playwright-cli.js"
        entry.write_text("// cli\n", encoding="utf-8")
        attacker = tmp_path / "agent-bin" / "node"
        attacker.parent.mkdir()
        attacker.write_text("#!/bin/sh\n", encoding="utf-8")
        attacker.chmod(0o755)
        monkeypatch.setenv("PATH", str(attacker.parent))
        monkeypatch.setattr(mod, "_cli_package_for_launcher", lambda _launcher: package)
        monkeypatch.setattr(mod, "_managed_cli_root", lambda: tmp_path / "absent-managed")
        monkeypatch.setattr(mod, "_system_node_candidates", lambda _launcher, _package: ())
        monkeypatch.setattr(
            mod,
            "_resolve_executable_file_for_system",
            lambda _entry: (entry.resolve(), None),
        )

        command, reason = mod._direct_cli_command(str(launcher))

        assert command is None
        assert reason is not None and "no fixed non-writable Node" in reason
        assert str(attacker) not in reason
