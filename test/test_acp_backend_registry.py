"""Tests for the ACP backend descriptor table."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from kiro_crew.acp import backends
from kiro_crew.acp.backends import (
    ALL_CAPABILITIES,
    CAP_SESSION_SHARING,
    CAP_TOOL_SEARCH,
    Dialect,
    Level,
    Routing,
    UnknownAcpBackend,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_SELECTABLE,
)


def test_descriptors_and_known_ids_agree_in_both_directions() -> None:
    """The descriptor table and the membership gate cannot drift apart.

    Checked both ways deliberately: a one-directional check passes when a
    descriptor exists for an id nobody may pass, and also when an id is
    accepted with no descriptor behind it.
    """
    assert backends.known_ids() == ACP_BACKENDS_KNOWN


def test_every_selectable_backend_has_a_descriptor() -> None:
    """An operator can never persist a value with no descriptor behind it."""
    for backend in ACP_BACKENDS_SELECTABLE:
        assert backends.descriptor_for(backend).id == backend


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_descriptor_declares_every_capability(backend: str) -> None:
    """A missing capability must be a hard error, never a silent False."""
    descriptor = backends.descriptor_for(backend)
    assert set(descriptor.capabilities) == set(ALL_CAPABILITIES)


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_descriptor_fields_are_populated(backend: str) -> None:
    """Every non-optional field carries a real value.

    ``credential_leaves`` is legitimately empty for a backend whose credential
    store Kiro Crew does not name, so it is excluded rather than asserted.
    """
    descriptor = backends.descriptor_for(backend)
    assert descriptor.label
    assert descriptor.signin_command
    assert descriptor.process_markers
    assert isinstance(descriptor.dialect, Dialect)
    assert isinstance(descriptor.routing, Routing)
    assert isinstance(descriptor.experimental, bool)


def test_descriptor_is_frozen() -> None:
    """A descriptor is data; a call site must not be able to mutate it."""
    descriptor = backends.descriptor_for(ACP_BACKEND_KIRO)
    with pytest.raises(dataclasses.FrozenInstanceError):
        descriptor.label = "mutated"  # type: ignore[misc]


def test_unknown_backend_raises_rather_than_defaulting() -> None:
    """An unrecognised id must not resolve to kiro.

    Falling back would spawn a different agent than the operator asked for,
    which is the failure the membership gate in AcpProvider already guards.
    """
    with pytest.raises(UnknownAcpBackend):
        backends.descriptor_for("not-a-backend")
    with pytest.raises(UnknownAcpBackend):
        backends.level("not-a-backend", CAP_TOOL_SEARCH)


def test_unknown_capability_raises() -> None:
    with pytest.raises(UnknownAcpBackend):
        backends.level(ACP_BACKEND_KIRO, "not-a-capability")


def test_kiro_supports_everything() -> None:
    """The default backend is the reference implementation."""
    for capability in ALL_CAPABILITIES:
        assert backends.supports(ACP_BACKEND_KIRO, capability)


def test_codex_effort_is_supported_and_unverified_stays_fail_closed() -> None:
    """Codex has a real selector; unknown behavior still cannot open a gate."""
    assert backends.level(ACP_BACKEND_CODEX, backends.CAP_REASONING_EFFORT) is Level.SUPPORTED
    assert backends.supports(ACP_BACKEND_CODEX, backends.CAP_REASONING_EFFORT)
    assert backends.level(ACP_BACKEND_KAS, backends.CAP_REASONING_EFFORT) is Level.UNVERIFIED
    assert not backends.supports(ACP_BACKEND_KAS, backends.CAP_REASONING_EFFORT)


def test_dialects() -> None:
    """KAS speaks kiro's dialect; the two adapters speak the public spec."""
    assert backends.dialect_of(ACP_BACKEND_KIRO) is Dialect.KIRO
    assert backends.dialect_of(ACP_BACKEND_KAS) is Dialect.KIRO
    assert backends.dialect_of(ACP_BACKEND_CLAUDE) is Dialect.SPEC
    assert backends.dialect_of(ACP_BACKEND_CODEX) is Dialect.SPEC
    assert backends.dialect_of(ACP_BACKEND_GOOSE) is Dialect.SPEC
    assert backends.dialect_of(ACP_BACKEND_OPENCODE) is Dialect.SPEC
    assert backends.dialect_of(ACP_BACKEND_PI) is Dialect.SPEC

    assert not backends.is_spec_dialect(ACP_BACKEND_KIRO)
    assert not backends.is_spec_dialect(ACP_BACKEND_KAS)
    assert backends.is_spec_dialect(ACP_BACKEND_CLAUDE)
    assert backends.is_spec_dialect(ACP_BACKEND_CODEX)
    assert backends.is_spec_dialect(ACP_BACKEND_GOOSE)
    assert backends.is_spec_dialect(ACP_BACKEND_OPENCODE)
    assert backends.is_spec_dialect(ACP_BACKEND_PI)


def test_session_sharing_matches_the_advertised_set_not_the_runtime_arm() -> None:
    """Sharing is narrower than "runs on the multiplexed runtime".

    This test previously asserted KAS claims the capability because it takes the
    runtime arm. That conflated two different facts:
    ``ACP_BACKENDS_ACP_RUNTIME`` is a deliberate SUPERSET of
    ``ACP_BACKENDS_SESSION_SHARING`` — running there is necessary for sharing but
    not sufficient, and KAS is held out until keep-aware teardown lands. The
    table must agree with the set the provider actually consults, so it is
    asserted against that set rather than restated by hand.
    """
    from kiro_crew.acp.types import ACP_BACKENDS_SESSION_SHARING

    for backend in sorted(ACP_BACKENDS_KNOWN):
        assert backends.supports(backend, CAP_SESSION_SHARING) is (
            backend in ACP_BACKENDS_SESSION_SHARING
        ), backend
    # Both spec adapters run one process per session on the legacy client path.
    assert not backends.supports(ACP_BACKEND_CLAUDE, CAP_SESSION_SHARING)
    assert not backends.supports(ACP_BACKEND_CODEX, CAP_SESSION_SHARING)


def test_kas_distinguishes_measured_absence_from_unverified_inheritance() -> None:
    """KAS must not turn a shared code path into a verified backend claim."""
    measured = {
        backends.CAP_SESSION_SHARING: Level.UNAVAILABLE,
        backends.CAP_AGENT_PROFILES: Level.DEGRADED,
        backends.CAP_MID_TURN_STEER: Level.SUPPORTED,
    }
    for capability in ALL_CAPABILITIES:
        level = backends.level(ACP_BACKEND_KAS, capability)
        if capability in measured:
            assert level is measured[capability]
        else:
            assert level is Level.UNVERIFIED


def test_only_kiro_is_non_experimental() -> None:
    assert not backends.descriptor_for(ACP_BACKEND_KIRO).experimental
    for backend in (ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX, ACP_BACKEND_KAS):
        assert backends.descriptor_for(backend).experimental


def test_signin_commands_are_backend_specific() -> None:
    """A Codex host must never be told to run kiro-cli login."""
    assert backends.descriptor_for(ACP_BACKEND_CODEX).signin_command == "codex login"
    assert backends.descriptor_for(ACP_BACKEND_KIRO).signin_command == "kiro-cli login"


def test_codex_credential_leaf_is_the_file_not_the_directory() -> None:
    """Protect the credential without denying the adapter's entire home."""
    leaves = backends.descriptor_for(ACP_BACKEND_CODEX).credential_leaves
    assert leaves == (".codex/auth.json",)
    assert ".codex" not in leaves


def test_credential_leaves_are_aggregated_and_deduped() -> None:
    aggregated = backends.credential_leaves()
    assert ".codex/auth.json" in aggregated
    assert len(aggregated) == len(set(aggregated))


def test_process_markers_cover_every_backend_and_dedupe() -> None:
    """KAS and kiro share a binary, so the marker list must not duplicate it."""
    markers = backends.process_markers()
    assert "kiro-cli" in markers
    assert "claude" in markers
    assert "codex" in markers
    assert len(markers) == len(set(markers))


def test_routing_records_how_each_backend_reaches_the_gate() -> None:
    """Routing is what the tool-gate enforcement branches on.

    Codex uses SESSION_CONFIG rather than its own config file: its ACP sessions
    ignore that file and default to a mode that writes inside the
    workspace without asking, so probing that file resolved ROUTED for a session
    that was ungated in practice. The enforceable fact is the mode the session
    itself advertises, applied and verified before the first prompt.
    """
    assert backends.descriptor_for(ACP_BACKEND_KIRO).routing is Routing.AGENT_SPEC
    assert backends.descriptor_for(ACP_BACKEND_KAS).routing is Routing.AGENT_SPEC
    assert backends.descriptor_for(ACP_BACKEND_CLAUDE).routing is Routing.SEEDED_SETTINGS
    assert backends.descriptor_for(ACP_BACKEND_CODEX).routing is Routing.SESSION_CONFIG
    assert backends.descriptor_for(ACP_BACKEND_GOOSE).routing is Routing.PERMISSION_REQUEST
    assert backends.descriptor_for(ACP_BACKEND_OPENCODE).routing is Routing.SEEDED_SETTINGS
    assert backends.descriptor_for(ACP_BACKEND_PI).routing is Routing.PERMISSION_REQUEST


def test_routing_vocabulary_contains_only_live_enforcement_paths() -> None:
    """Do not pre-arm permissive branches for routing modes no backend uses."""
    assert set(Routing) == {
        Routing.AGENT_SPEC,
        Routing.SEEDED_SETTINGS,
        Routing.SESSION_CONFIG,
        Routing.PERMISSION_REQUEST,
        Routing.UNVERIFIED,
    }


def test_session_config_routing_names_an_option_and_an_exact_value() -> None:
    """A SESSION_CONFIG backend is unenforceable without both halves.

    ``session_config_issue`` requires the session to advertise this exact option
    id carrying this exact value, so an empty half would make every session
    refuse (or, with the opt-out on, start ungated) rather than arm the route.
    """
    for backend in sorted(ACP_BACKENDS_KNOWN):
        descriptor = backends.descriptor_for(backend)
        if descriptor.routing is Routing.SESSION_CONFIG:
            assert descriptor.permission_config_id, backend
            assert descriptor.permission_config_value, backend
        else:
            # A value on a backend that does not route through it would never be
            # applied, so it can only mislead a reader into thinking it is.
            assert not descriptor.permission_config_id, backend
            assert not descriptor.permission_config_value, backend


def test_selectability_is_a_separate_axis_from_being_described() -> None:
    """Selectability is owned by ACP_BACKENDS_SELECTABLE alone.

    Asserts the invariant that survives membership changes, rather than naming a
    withheld backend: this test named KAS, then codex, then derived the example
    from the sets, and each graduated in turn until nothing was withheld. Every
    selectable backend must be fully described, and selectable can never contain
    something unknown — which is what keeps the two concepts from collapsing into
    one when a registry adapter arrives described but not yet selectable.
    """
    assert ACP_BACKENDS_SELECTABLE <= ACP_BACKENDS_KNOWN
    for backend in sorted(ACP_BACKENDS_SELECTABLE):
        descriptor = backends.descriptor_for(backend)
        assert descriptor.label, backend
        assert descriptor.id == backend


class TestCachedRegistryAdapters:
    @pytest.mark.parametrize(
        ("kind", "want"),
        [
            (
                "npx",
                [
                    "npx",
                    "--offline",
                    "--yes=false",
                    "--",
                    "example-acp@1.0.0",
                    "--acp",
                ],
            ),
        ],
    )
    def test_launch_argv_never_downloads_an_adapter(self, kind: str, want: list[str]) -> None:
        adapter = self._adapter(kind=kind)
        assert adapter.launch_argv == want

    def test_npx_offline_environment_is_mandatory(self) -> None:
        adapter = self._adapter(kind="npx")
        assert adapter.offline_env == {"npm_config_offline": "true"}

    def test_npx_refuses_to_materialize_a_missing_cached_package(self) -> None:
        """No-install is an argv contract, not just an offline environment hint."""
        adapter = self._adapter(kind="npx")

        assert adapter.launch_argv[:4] == ["npx", "--offline", "--yes=false", "--"]
        assert adapter.launch_argv[4] == adapter.package

    def test_uvx_cache_cannot_substitute_for_an_operator_install(self) -> None:
        """uvx is ephemeral execution even in offline mode, so it is withheld."""
        adapter = self._adapter(kind="uvx")

        assert not adapter.is_launchable
        assert adapter.launch_argv == []
        assert adapter.resolve_launch_argv() == []
        assert adapter.install_command == ""

    def test_global_npm_install_resolves_without_npx_or_a_warm_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The documented global install must be the exact launch source."""
        from kiro_crew import sandbox
        from kiro_crew.acp import registry

        toolchains = registry._npm_toolchains()
        toolchain = next(
            (candidate for candidate in toolchains if candidate.volta_home is None),
            None,
        )
        if toolchain is None:
            pytest.skip("A non-Volta Node/npm toolchain is not installed")
        invocation = toolchain.npm_argv

        package_dir = tmp_path / "package"
        package_dir.mkdir()
        (package_dir / "package.json").write_text(
            json.dumps(
                {
                    "name": "example-acp",
                    "version": "1.0.0",
                    "bin": "cli.js",
                }
            ),
            encoding="utf-8",
        )
        (package_dir / "cli.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
        prefix = tmp_path / "npm-prefix"
        npm_cache = tmp_path / "empty-npm-cache"
        env = {
            **os.environ,
            "PATH": toolchain.path,
            "NPM_CONFIG_PREFIX": str(prefix),
            "npm_config_cache": str(npm_cache),
            "npm_config_offline": "true",
            "NODE_COMPILE_CACHE": str(tmp_path / "node-compile-cache"),
        }
        packed = subprocess.run(
            [*invocation, "pack", str(package_dir), "--pack-destination", str(tmp_path)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        assert packed.returncode == 0, packed.stderr
        tarball = tmp_path / "example-acp-1.0.0.tgz"
        assert tarball.is_file()
        result = subprocess.run(
            [
                *invocation,
                "install",
                "-g",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--",
                str(tarball),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        monkeypatch.setenv("NPM_CONFIG_PREFIX", str(prefix))
        monkeypatch.setenv("npm_config_cache", str(npm_cache))
        monkeypatch.setenv("NODE_COMPILE_CACHE", str(tmp_path / "node-compile-cache"))
        monkeypatch.setenv("PATH", toolchain.path)

        def passthrough(
            argv: list[str],
            mode: str = "standard",
            *,
            env: dict[str, str] | None = None,
            **_kwargs: object,
        ) -> tuple[list[str], dict[str, str], None]:
            del mode
            return list(argv), dict(env or os.environ), None

        monkeypatch.setattr(sandbox, "sandboxed_spawn_argv", passthrough)
        registry._clear_npm_root_cache()
        try:
            argv = self._adapter(kind="npx").resolve_launch_argv()

            assert argv[0] == toolchain.node
            assert Path(argv[1]).name == "cli.js"
            assert str(prefix) in argv[1]
            assert argv[2:] == ["--acp"]
            assert not any("npx" in Path(arg).name.lower() for arg in argv[:2])
        finally:
            registry._clear_npm_root_cache()

    def test_windows_npm_cmd_is_replaced_with_node_and_npm_cli(self, tmp_path: Path) -> None:
        """CreateProcess never receives the shell-only npm/npx cmd shim."""
        from kiro_crew.acp import registry

        node = tmp_path / "node.exe"
        npm_cmd = tmp_path / "npm.cmd"
        npm_cli = tmp_path / "node_modules" / "npm" / "bin" / "npm-cli.js"
        npm_cli.parent.mkdir(parents=True)
        for path in (node, npm_cmd, npm_cli):
            path.write_text("", encoding="utf-8")

        assert registry._npm_invocation_for_path(str(npm_cmd), str(node)) == (
            str(node),
            str(npm_cli),
        )

    def test_executable_npm_shim_is_accepted_directly(self, tmp_path: Path) -> None:
        """An executable npm shim is runnable without knowing its internals."""
        from kiro_crew.acp import registry

        node = tmp_path / "node"
        npm = tmp_path / "npm"
        for path in (node, npm):
            path.write_text("", encoding="utf-8")

        assert registry._npm_invocation_for_path(str(npm), str(node)) == (str(npm),)

    def test_custom_volta_home_identifies_its_npm_shim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.acp import registry

        volta_home = tmp_path / "custom-volta"
        npm = volta_home / "bin" / "npm"
        npm.parent.mkdir(parents=True)
        npm.write_text("", encoding="utf-8")
        monkeypatch.setenv("VOLTA_HOME", str(volta_home))

        assert registry._volta_home_for_npm(str(npm)) == volta_home

    def test_windows_volta_install_dir_maps_to_its_separate_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows keeps Volta executables outside its per-user data home."""
        from kiro_crew.acp import registry

        volta_home = tmp_path / "LocalAppData" / "Volta"
        install_dir = tmp_path / "Program Files" / "Volta"
        npm = install_dir / "npm.exe"
        install_dir.mkdir(parents=True)
        npm.write_text("", encoding="utf-8")
        monkeypatch.setenv("VOLTA_HOME", str(volta_home))
        monkeypatch.setenv("VOLTA_INSTALL_DIR", str(install_dir))

        assert registry._volta_home_for_npm(str(npm)) == volta_home

    @pytest.mark.parametrize(
        ("windows", "package_parent"),
        [
            (False, ("lib", "node_modules")),
            (True, ("node_modules",)),
        ],
    )
    def test_volta_global_install_resolves_from_its_package_image(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        windows: bool,
        package_parent: tuple[str, ...],
    ) -> None:
        """Volta's isolated package image is the install source, not npm root."""
        from kiro_crew.acp import registry

        volta_home = tmp_path / "Volta"
        bin_dir = volta_home / "bin"
        bin_dir.mkdir(parents=True)
        suffix = ".exe" if windows else ""
        npm = bin_dir / f"npm{suffix}"
        node = bin_dir / f"node{suffix}"
        for path in (npm, node):
            path.write_text("", encoding="utf-8")
            path.chmod(0o755)

        image = volta_home / "tools" / "image" / "packages" / "example-acp"
        package_dir = image.joinpath(*package_parent, "example-acp")
        package_dir.mkdir(parents=True)
        (package_dir / "package.json").write_text(
            json.dumps({"name": "example-acp", "version": "1.0.0", "bin": "cli.js"}),
            encoding="utf-8",
        )
        (package_dir / "cli.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
        platform = {"node": "22.4.1", "npm": None, "pnpm": None, "yarn": None}
        package_config = volta_home / "tools" / "user" / "packages" / "example-acp.json"
        bin_config = volta_home / "tools" / "user" / "bins" / "example-acp.json"
        package_config.parent.mkdir(parents=True)
        bin_config.parent.mkdir(parents=True)
        package_config.write_text(
            json.dumps(
                {
                    "name": "example-acp",
                    "version": "1.0.0",
                    "platform": platform,
                    "bins": ["example-acp"],
                    "manager": "Npm",
                }
            ),
            encoding="utf-8",
        )
        bin_config.write_text(
            json.dumps(
                {
                    "name": "example-acp",
                    "package": "example-acp",
                    "version": "1.0.0",
                    "platform": platform,
                    "manager": "Npm",
                }
            ),
            encoding="utf-8",
        )
        node_runtime = (
            volta_home / "tools" / "image" / "node" / "22.4.1" / "node.exe"
            if windows
            else volta_home / "tools" / "image" / "node" / "22.4.1" / "bin" / "node"
        )
        node_runtime.parent.mkdir(parents=True)
        node_runtime.write_text("", encoding="utf-8")
        node_runtime.chmod(0o755)
        # A workspace-local command with the same name can redirect Volta's
        # generic shim, so the resolver must never return that shim.
        project_command = tmp_path / "workspace" / "node_modules" / ".bin" / "example-acp"
        project_command.parent.mkdir(parents=True)
        project_command.write_text("", encoding="utf-8")
        toolchain = registry._NpmToolchain(
            (str(npm),),
            str(node),
            str(bin_dir),
            volta_home=volta_home,
            is_windows=windows,
        )
        monkeypatch.setattr(registry, "_npm_toolchains", lambda: (toolchain,))
        monkeypatch.setattr(registry, "_npm_global_roots", lambda *_args: ())

        assert self._adapter(kind="npx").resolve_launch_argv() == [
            str(node_runtime.resolve()),
            str((package_dir / "cli.js").resolve()),
            "--acp",
        ]

    def test_volta_image_with_the_wrong_version_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.acp import registry

        volta_home = tmp_path / "volta"
        bin_dir = volta_home / "bin"
        bin_dir.mkdir(parents=True)
        package_dir = (
            volta_home
            / "tools"
            / "image"
            / "packages"
            / "example-acp"
            / "lib"
            / "node_modules"
            / "example-acp"
        )
        package_dir.mkdir(parents=True)
        (package_dir / "package.json").write_text(
            json.dumps({"name": "example-acp", "version": "2.0.0", "bin": "cli.js"}),
            encoding="utf-8",
        )
        (package_dir / "cli.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
        platform = {"node": "22.4.1", "npm": None, "pnpm": None, "yarn": None}
        package_config = volta_home / "tools" / "user" / "packages" / "example-acp.json"
        bin_config = volta_home / "tools" / "user" / "bins" / "example-acp.json"
        package_config.parent.mkdir(parents=True)
        bin_config.parent.mkdir(parents=True)
        package_config.write_text(
            json.dumps(
                {
                    "name": "example-acp",
                    "version": "1.0.0",
                    "platform": platform,
                    "bins": ["example-acp"],
                    "manager": "Npm",
                }
            ),
            encoding="utf-8",
        )
        bin_config.write_text(
            json.dumps(
                {
                    "name": "example-acp",
                    "package": "example-acp",
                    "version": "1.0.0",
                    "platform": platform,
                    "manager": "Npm",
                }
            ),
            encoding="utf-8",
        )
        node_runtime = volta_home / "tools" / "image" / "node" / "22.4.1" / "bin" / "node"
        node_runtime.parent.mkdir(parents=True)
        node_runtime.write_text("", encoding="utf-8")
        node_runtime.chmod(0o755)
        toolchain = registry._NpmToolchain(
            (str(bin_dir / "npm"),),
            str(bin_dir / "node"),
            str(bin_dir),
            volta_home=volta_home,
        )
        monkeypatch.setattr(registry, "_npm_toolchains", lambda: (toolchain,))
        monkeypatch.setattr(registry, "_npm_global_roots", lambda *_args: ())

        assert self._adapter(kind="npx").resolve_launch_argv() == []

    def test_volta_bin_mapping_to_another_package_fails_closed(self, tmp_path: Path) -> None:
        from kiro_crew.acp import registry

        home = tmp_path / "volta"
        package_dir = (
            home
            / "tools"
            / "image"
            / "packages"
            / "example-acp"
            / "lib"
            / "node_modules"
            / "example-acp"
        )
        package_dir.mkdir(parents=True)
        (package_dir / "package.json").write_text(
            json.dumps({"name": "example-acp", "version": "1.0.0", "bin": "cli.js"}),
            encoding="utf-8",
        )
        (package_dir / "cli.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
        platform = {"node": "22.4.1", "npm": None, "pnpm": None, "yarn": None}
        package_config = home / "tools" / "user" / "packages" / "example-acp.json"
        bin_config = home / "tools" / "user" / "bins" / "example-acp.json"
        package_config.parent.mkdir(parents=True)
        bin_config.parent.mkdir(parents=True)
        package_config.write_text(
            json.dumps(
                {
                    "name": "example-acp",
                    "version": "1.0.0",
                    "platform": platform,
                    "bins": ["example-acp"],
                    "manager": "Npm",
                }
            ),
            encoding="utf-8",
        )
        bin_config.write_text(
            json.dumps(
                {
                    "name": "example-acp",
                    "package": "other-package",
                    "version": "1.0.0",
                    "platform": platform,
                    "manager": "Npm",
                }
            ),
            encoding="utf-8",
        )
        node_runtime = home / "tools" / "image" / "node" / "22.4.1" / "bin" / "node"
        node_runtime.parent.mkdir(parents=True)
        node_runtime.write_text("", encoding="utf-8")
        node_runtime.chmod(0o755)
        toolchain = registry._NpmToolchain(
            (str(home / "bin" / "npm"),),
            str(home / "bin" / "node"),
            str(home / "bin"),
            volta_home=home,
        )

        assert (
            registry._resolve_volta_package(
                toolchain,
                "example-acp",
                "1.0.0",
                (),
            )
            == []
        )

    def test_global_resolution_checks_every_toolchain_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A package under a non-first manager root remains launchable."""
        from kiro_crew.acp import registry

        first = registry._NpmToolchain(("/first/npm",), "/first/node", "/first")
        second = registry._NpmToolchain(("/second/npm",), "/second/node", "/second")
        first_root = tmp_path / "first-root"
        second_root = tmp_path / "second-root"
        first_root.mkdir()
        package_dir = second_root / "example-acp"
        package_dir.mkdir(parents=True)
        (package_dir / "package.json").write_text(
            json.dumps({"name": "example-acp", "version": "1.0.0", "bin": "cli.js"}),
            encoding="utf-8",
        )
        (package_dir / "cli.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
        roots = {first: first_root.resolve(), second: second_root.resolve()}
        monkeypatch.setattr(registry, "_npm_toolchains", lambda: (first, second))
        monkeypatch.setattr(registry, "_query_npm_global_root", roots.get)
        registry._clear_npm_root_cache()
        try:
            argv = self._adapter(kind="npx").resolve_launch_argv()
        finally:
            registry._clear_npm_root_cache()

        assert argv == ["/second/node", str((package_dir / "cli.js").resolve()), "--acp"]

    def test_missing_global_root_is_retried_after_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An install after a miss takes effect without restarting the gateway."""
        from kiro_crew.acp import registry

        toolchain = registry._NpmToolchain(("/npm",), "/node", "/bin")
        root = tmp_path / "global-root"
        calls = 0

        def query(_toolchain: object) -> Path | None:
            nonlocal calls
            calls += 1
            return root.resolve() if root.is_dir() else None

        monkeypatch.setattr(registry, "_npm_toolchains", lambda: (toolchain,))
        monkeypatch.setattr(registry, "_query_npm_global_root", query)
        registry._clear_npm_root_cache()
        try:
            assert registry._npm_global_roots() == ()
            root.mkdir()
            assert registry._npm_global_roots() == (("/node", root.resolve()),)
        finally:
            registry._clear_npm_root_cache()

        assert calls == 2

    def test_resolution_snapshot_shares_a_failed_toolchain_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One Settings request does not retry the same broken npm per adapter."""
        from kiro_crew.acp import registry

        toolchain = registry._NpmToolchain(("/npm",), "/node", "/bin")
        calls = 0

        def query(_toolchain: object) -> None:
            nonlocal calls
            calls += 1
            return None

        monkeypatch.setattr(registry, "_npm_toolchains", lambda: (toolchain,))
        monkeypatch.setattr(registry, "_query_npm_global_root", query)
        registry._clear_npm_root_cache()
        try:
            snapshot = registry.npm_resolution_snapshot()
            first = self._adapter(kind="npx")
            second = registry.RegistryAdapter(
                **{
                    **first.__dict__,
                    "id": "other-acp",
                    "package": "other-acp@1.0.0",
                }
            )

            assert first.resolve_launch_argv(snapshot) == []
            assert second.resolve_launch_argv(snapshot) == []
            next_snapshot = registry.npm_resolution_snapshot()
            assert first.resolve_launch_argv(next_snapshot) == []
        finally:
            registry._clear_npm_root_cache()

        assert calls == 2

    @pytest.mark.parametrize(
        ("manifest_changes", "bin_text"),
        [
            ({"version": "2.0.0"}, "#!/usr/bin/env node\n"),
            ({"bin": {"first": "first.js", "second": "second.js"}}, ""),
            ({"bin": "../../outside.js"}, "#!/usr/bin/env node\n"),
            ({"bin": "cli.py"}, "#!/usr/bin/env python\n"),
        ],
    )
    def test_global_package_resolution_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        manifest_changes: dict[str, object],
        bin_text: str,
    ) -> None:
        """Only the exact unambiguous in-package Node entry may execute."""
        from kiro_crew.acp import registry

        root = tmp_path / "node_modules"
        package_dir = root / "example-acp"
        package_dir.mkdir(parents=True)
        manifest = {
            "name": "example-acp",
            "version": "1.0.0",
            "bin": "cli.js",
            **manifest_changes,
        }
        (package_dir / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
        rel_bin = manifest.get("bin")
        if isinstance(rel_bin, str) and ".." not in rel_bin:
            (package_dir / rel_bin).write_text(bin_text, encoding="utf-8")
        (tmp_path / "outside.js").write_text(bin_text, encoding="utf-8")
        monkeypatch.setattr(
            registry,
            "_npm_global_roots",
            lambda *_args: (("/node", root.resolve()),),
        )

        assert self._adapter(kind="npx").resolve_launch_argv() == []

    def test_registry_environment_cannot_control_spawn_runtime(self) -> None:
        from kiro_crew.acp import registry

        document = {
            "agents": [
                {
                    **self._registry_entry("example-acp"),
                    "distribution": {
                        "npx": {
                            "package": "example-acp@1.0.0",
                            "env": {
                                "EXAMPLE_MODE": "acp",
                                "PATH": "/tmp/attacker-bin",
                                "NODE_OPTIONS": "--require=/tmp/hook.js",
                                "LD_PRELOAD": "/tmp/hook.so",
                                "npm_config_userconfig": "/tmp/npmrc",
                                "BAD-NAME": "ignored",
                            },
                        }
                    },
                }
            ]
        }

        adapter = registry._parse(document)["example-acp"]
        assert dict(adapter.env) == {"EXAMPLE_MODE": "acp"}

    @pytest.mark.parametrize(
        ("version", "package", "args"),
        [
            ("1.0.0", "--offline=false", ["example-acp@1.0.0"]),
            ("1.0.0", "example-acp", []),
            ("1.0.0", "example-acp@latest", []),
            ("1.0.0", "example-acp@2.0.0", []),
            ("latest", "example-acp@latest", []),
            ("1.0.0", "example-acp@1.0.0", [123]),
            ("1.0.0", "example-acp@1.0.0", ["bad\x00arg"]),
        ],
    )
    def test_malformed_distribution_cannot_become_spawn_argv(
        self,
        version: str,
        package: str,
        args: list[object],
    ) -> None:
        """Registry data must name one exact package before adapter argv."""
        from kiro_crew.acp import registry

        document = {
            "agents": [
                {
                    "id": "example-acp",
                    "name": "Example ACP",
                    "version": version,
                    "distribution": {"npx": {"package": package, "args": args}},
                }
            ]
        }

        assert registry._parse(document) == {}

    def test_manually_constructed_unsafe_adapter_is_not_launchable(self) -> None:
        """Launch safety must survive a caller bypassing the JSON parser."""
        adapter = dataclasses.replace(
            self._adapter(),
            package="--offline=false",
            args=("example-acp@1.0.0",),
        )

        assert not adapter.is_launchable
        assert adapter.launch_argv == []

    @pytest.mark.parametrize(
        ("kind", "package"),
        [
            ("npx", "@agentclientprotocol/example-acp@1.0.0"),
        ],
    )
    def test_exact_supported_package_forms_remain_launchable(self, kind: str, package: str) -> None:
        adapter = dataclasses.replace(self._adapter(kind=kind), package=package)

        assert adapter.is_launchable
        assert package in adapter.launch_argv

    @pytest.mark.parametrize(
        ("package_name", "expected"),
        [
            ("example-acp", ("example-acp",)),
            ("@agentclientprotocol/example-acp", ("@agentclientprotocol", "example-acp")),
        ],
    )
    def test_npm_package_parts_use_registry_delimiters_on_every_platform(
        self,
        package_name: str,
        expected: tuple[str, ...],
    ) -> None:
        """npm scope syntax stays POSIX-shaped before native path joining."""
        from kiro_crew.acp import registry

        assert registry._npm_package_parts(package_name) == expected

    def test_cache_publish_is_atomic_for_concurrent_readers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import atomic_write as atomic_write_module
        from kiro_crew.acp import registry

        cache_path = tmp_path / "acp-registry.json"
        old = {"agents": [self._registry_entry("old-acp")]}
        new = {"agents": [self._registry_entry("new-acp")]}
        cache_path.write_text(json.dumps(old), encoding="utf-8")
        monkeypatch.setattr(registry, "_cache_path", lambda: cache_path)

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int) -> bytes:
                return json.dumps(new).encode("utf-8")

        before_replace = threading.Event()
        allow_replace = threading.Event()
        real_replace = atomic_write_module.os.replace

        def paused_replace(src: str, dst: str) -> None:
            before_replace.set()
            assert allow_replace.wait(timeout=5)
            real_replace(src, dst)

        monkeypatch.setattr(atomic_write_module.os, "replace", paused_replace)
        monkeypatch.setattr(registry.urllib.request, "urlopen", lambda *_a, **_k: _Response())
        worker = threading.Thread(target=lambda: registry.fetch(force=True))
        worker.start()
        assert before_replace.wait(timeout=5)
        assert set(registry.cached()) == {"old-acp"}
        allow_replace.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert set(registry.cached()) == {"new-acp"}

    def test_fetch_rejects_a_registry_url_outside_the_pinned_https_origin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.acp import registry

        monkeypatch.setattr(registry, "REGISTRY_URL", (tmp_path / "registry.json").as_uri())
        opened = False

        def unexpected_open(*_args: object, **_kwargs: object) -> None:
            nonlocal opened
            opened = True

        monkeypatch.setattr(registry.urllib.request, "urlopen", unexpected_open)
        monkeypatch.setattr(registry, "_read_cache", lambda _max_age: None)

        assert registry.fetch(force=True) == {}
        assert not opened

    @staticmethod
    def _registry_entry(ident: str) -> dict:
        return {
            "id": ident,
            "name": ident,
            "version": "1.0.0",
            "distribution": {"npx": {"package": f"{ident}@1.0.0"}},
        }

    @staticmethod
    def _adapter(*, kind: str = "npx"):
        from kiro_crew.acp import registry

        return registry.RegistryAdapter(
            id="example-acp",
            name="Example ACP",
            version="1.0.0",
            description="",
            repository="",
            license="MIT",
            icon="",
            kind=kind,
            package="example-acp@1.0.0",
            args=("--acp",),
            env=(("EXAMPLE_MODE", "acp"),),
        )

    def test_launchable_cached_adapter_is_described_but_not_selectable(self, monkeypatch) -> None:
        from kiro_crew.acp import registry

        adapter = self._adapter()
        monkeypatch.setattr(registry, "cached", lambda: {adapter.id: adapter})

        descriptor = backends.descriptor_for(adapter.id)
        assert descriptor.id == adapter.id
        assert descriptor.registry_id == adapter.id
        assert descriptor.routing is Routing.UNVERIFIED
        assert descriptor.dialect is Dialect.SPEC
        assert descriptor.process_markers == ()
        assert set(descriptor.capabilities.values()) == {Level.UNVERIFIED}
        assert adapter.id not in backends.selectable_ids()

    def test_hand_written_registry_id_is_not_a_second_trust_path(self, monkeypatch) -> None:
        from kiro_crew.acp import registry
        from kiro_crew.acp.backends import Routing, descriptor_for, selectable_ids

        adapter = registry.RegistryAdapter(
            id="codex-acp",
            name="Codex",
            version="1.4.0",
            description="",
            repository="",
            license="Apache-2.0",
            icon="",
            kind="npx",
            package="@agentclientprotocol/codex-acp@1.4.0",
            args=(),
            env=(),
        )
        monkeypatch.setattr(registry, "cached", lambda: {adapter.id: adapter})

        descriptor = descriptor_for(adapter.id)
        assert descriptor.id == "codex"
        assert descriptor.routing is Routing.SESSION_CONFIG
        assert "codex-acp" not in selectable_ids()
        assert "codex" not in selectable_ids()

    def test_hand_written_pi_is_not_a_second_unverified_path(self, monkeypatch) -> None:
        """``pi-acp`` is the registry spelling of the hand-written ``pi`` backend."""
        from kiro_crew.acp import registry
        from kiro_crew.acp.backends import Routing, descriptor_for, selectable_ids

        adapter = registry.RegistryAdapter(
            id="pi-acp",
            name="Pi ACP",
            version="1.0.0",
            description="",
            repository="",
            license="MIT",
            icon="",
            kind="npx",
            package="pi-acp@1.0.0",
            args=(),
            env=(),
        )
        monkeypatch.setattr(registry, "cached", lambda: {adapter.id: adapter})

        descriptor = descriptor_for(adapter.id)
        assert descriptor.id == ACP_BACKEND_PI
        assert descriptor.routing is Routing.PERMISSION_REQUEST
        assert "pi-acp" not in selectable_ids()
        assert ACP_BACKEND_PI not in selectable_ids()


def test_canonical_backend_id_maps_registry_ids() -> None:
    from kiro_crew.acp.backends import canonical_backend_id
    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX

    assert canonical_backend_id("codex-acp") == ACP_BACKEND_CODEX
    assert canonical_backend_id("claude-acp") == ACP_BACKEND_CLAUDE
    assert canonical_backend_id("codex") == ACP_BACKEND_CODEX
    assert canonical_backend_id("pi-acp") == ACP_BACKEND_PI
    assert canonical_backend_id("opencode") == ACP_BACKEND_OPENCODE
    assert canonical_backend_id("example-acp") == "example-acp"
