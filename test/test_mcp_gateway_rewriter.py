"""Per-agent rewriter wrapping guards.

A poolable stdio server that the user has explicitly disabled must never be
wrapped into a live pooling stub -- ``_build_stub_entry`` returns a fixed shape
and would drop the ``disabled`` flag, silently re-enabling the muted server in
the agent overlay. These tests pin that guard (mirroring the settings-inject
guard in ``_injectable_settings_servers``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER, _rewrite_single_spec


def _rewrite(
    spec: dict,
    tmp_path: Path,
    *,
    poolable_servers: frozenset[str] = frozenset(),
    pooling_enabled: bool = True,
) -> tuple[dict, int]:
    return _rewrite_single_spec(
        spec,
        stubs_dir=tmp_path / "stubs",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "wd",
        sandbox_mode="auto",
        approval_mode="interactive",
        poolable_servers=poolable_servers,
        pooling_enabled=pooling_enabled,
    )


def test_disabled_poolable_server_is_not_wrapped(tmp_path: Path) -> None:
    """A poolable server explicitly disabled by the user is passed through with
    ``disabled`` intact and is NOT wrapped into a running stub."""
    spec = {
        "name": "agent-a",
        "mcpServers": {
            "muted": {"command": "some-mcp", "poolable": True, "disabled": True},
        },
    }
    new_spec, wrapped = _rewrite(spec, tmp_path)
    entry = new_spec["mcpServers"]["muted"]

    assert wrapped == 0
    assert entry.get("disabled") is True  # mute preserved
    assert _WRAPPER_MARKER not in entry  # never wrapped into a live stub
    assert "poolable" not in entry  # internal hint stripped
    assert entry.get("command") == "some-mcp"  # original launch left intact


def test_enabled_poolable_server_is_still_wrapped(tmp_path: Path) -> None:
    """Guard against over-correction: a non-disabled poolable server is still
    wrapped into a pooling stub."""
    spec = {
        "name": "agent-a",
        "mcpServers": {
            "live": {"command": "some-mcp", "poolable": True},
        },
    }
    new_spec, wrapped = _rewrite(spec, tmp_path)
    entry = new_spec["mcpServers"]["live"]

    assert wrapped == 1
    assert entry.get(_WRAPPER_MARKER) is True


def test_non_poolable_server_is_wrapped_without_the_poolable_flag(
    tmp_path: Path,
) -> None:
    """The decoupling: a server nobody declared poolable STILL gets a stub.

    The stub is the addressing layer MCP Apps routes callbacks through, so its
    presence cannot depend on the allowlist. What the allowlist decides is the
    ``--poolable`` flag on the stub's argv, which the gateway reads to choose
    between the shared bucket and a backend private to this connection.
    """
    spec = {
        "name": "agent-a",
        "mcpServers": {
            "stateful": {"command": "some-mcp"},
        },
    }
    new_spec, wrapped = _rewrite(spec, tmp_path)
    entry = new_spec["mcpServers"]["stateful"]

    assert wrapped == 1
    assert entry.get(_WRAPPER_MARKER) is True
    assert "--poolable" not in entry["args"]
    # The internal hint never reaches kiro-cli.
    assert "poolable" not in entry


def test_allowlisted_server_gets_the_poolable_flag(tmp_path: Path) -> None:
    spec = {
        "name": "agent-a",
        "mcpServers": {
            "shareable": {"command": "some-mcp"},
        },
    }
    new_spec, _ = _rewrite(
        spec, tmp_path, poolable_servers=frozenset({"shareable"})
    )

    assert "--poolable" in new_spec["mcpServers"]["shareable"]["args"]


def test_private_server_with_declared_env_is_not_warned_about(
    tmp_path: Path, caplog
) -> None:
    """The pooled warnings must not fire for a connection-private backend.

    Both reasons the shared path withholds declared env are absent when there is
    one stub, and gatewayd forwards the block in full — so the warning would be
    false on its face. Worse, its remedy ("stop sharing this server") names the
    state the server is already in, which sends an operator chasing a
    non-problem. This fires on a default install: sharing off plus an empty
    allowlist makes every env-declaring server private.
    """
    import logging

    spec = {
        "name": "agent-a",
        "mcpServers": {
            "needs-env": {
                "command": "some-mcp",
                "env": {"API_TOKEN": "x", "REGION": "us-west-2"},
            },
        },
    }
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.rewriter"):
        new_spec, wrapped = _rewrite(spec, tmp_path, pooling_enabled=False)

    assert wrapped == 1  # still stubbed — the stub is unconditional
    assert "--poolable" not in new_spec["mcpServers"]["needs-env"]["args"]
    env_warnings = [r for r in caplog.records if "declares" in r.getMessage()]
    assert env_warnings == [], (
        "a private backend was warned about with pooled-backend advice: "
        f"{[r.getMessage() for r in env_warnings]}"
    )


def test_shared_server_with_declared_env_is_still_warned_about(
    tmp_path: Path, caplog
) -> None:
    """The guard must not silence the case that IS real: a shared backend does
    drop the declared env, and an operator relying on it needs to know."""
    import logging

    spec = {
        "name": "agent-a",
        "mcpServers": {
            "needs-env": {"command": "some-mcp", "env": {"REGION": "us-west-2"}},
        },
    }
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.rewriter"):
        _rewrite(
            spec, tmp_path, poolable_servers=frozenset({"needs-env"})
        )

    msgs = [r.getMessage() for r in caplog.records if "declares" in r.getMessage()]
    assert len(msgs) == 1, msgs
    assert "shared" in msgs[0]


def test_pooling_disabled_still_wraps_but_shares_nothing(tmp_path: Path) -> None:
    """Pooling off is not stubs off.

    With ``mcp_gateway.enabled`` false every server keeps its stub -- so MCP Apps
    keeps working -- and nothing is marked shareable, so each connection gets its
    own backend. An entry declaring ``poolable: true`` cannot override the
    operator's global switch.
    """
    spec = {
        "name": "agent-a",
        "mcpServers": {
            "declared": {"command": "some-mcp", "poolable": True},
            "listed": {"command": "other-mcp"},
        },
    }
    new_spec, wrapped = _rewrite(
        spec,
        tmp_path,
        poolable_servers=frozenset({"listed"}),
        pooling_enabled=False,
    )

    assert wrapped == 2
    for name in ("declared", "listed"):
        entry = new_spec["mcpServers"][name]
        assert entry.get(_WRAPPER_MARKER) is True, f"{name} lost its stub"
        assert "--poolable" not in entry["args"], f"{name} still marked shareable"


def test_rewriter_calls_restrict_to_owner_on_windows(
    tmp_path: Path, monkeypatch
) -> None:
    """On Windows (IS_POSIX=False, IS_WINDOWS=True), rewrite_agents must call
    make_owner_only_dir on overlay directories (which internally calls
    restrict_to_owner for DACL lockdown) and restrict_to_owner directly on
    env sidecar files so credentials are not left world-readable under
    inherited ACLs.

    Regression test for GPT 5.6 findings:
    - Windows enablement exposes credential sidecars in inherited-readable
      overlay directories.
    - restrict_to_owner applies 0o600 on POSIX directories, removing the
      execute bit needed for traversal; directories must use make_owner_only_dir
      which applies 0o700 on POSIX and restrict_to_owner (DACL) on Windows.
    """
    from unittest.mock import patch

    from kiro_crew.mcp_gateway.rewriter import rewrite_agents

    # Scaffold a minimal agent spec with an env var (triggers sidecar write).
    source_dir = tmp_path / "agents"
    source_dir.mkdir()
    spec = {
        "name": "test-agent",
        "mcpServers": {
            "myserver": {
                "command": "echo",
                "args": ["hello"],
                "env": {"SECRET_TOKEN": "s3cr3t"},
                "poolable": True,
            }
        },
    }
    (source_dir / "test-agent.json").write_text(
        __import__("json").dumps(spec), encoding="utf-8"
    )

    restricted_paths: list[Path] = []
    made_owner_dirs: list[Path] = []

    def _mock_restrict(path):
        restricted_paths.append(Path(path))

    def _mock_make_owner_only_dir(path):
        """Record + actually create the directory (callers write into it)."""
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        made_owner_dirs.append(p)

    # Simulate Windows: IS_POSIX=False, IS_WINDOWS=True.
    monkeypatch.setattr("kiro_crew.mcp_gateway.rewriter.platform_compat.IS_POSIX", False)
    monkeypatch.setattr("kiro_crew.mcp_gateway.rewriter.platform_compat.IS_WINDOWS", True)
    with patch(
        "kiro_crew.mcp_gateway.rewriter.platform_compat.restrict_to_owner",
        side_effect=_mock_restrict,
    ), patch(
        "kiro_crew.mcp_gateway.rewriter.platform_compat.make_owner_only_dir",
        side_effect=_mock_make_owner_only_dir,
    ):
        rewrite_agents(
            source_dir=source_dir,
            overlay_dir=tmp_path / "overlay",
            socket_path=tmp_path / "gw.sock",
            work_dir=tmp_path / "wd",
            sandbox_mode="auto",
            approval_mode="interactive",
            poolable_servers=frozenset(["myserver"]),
        )

    # Directories MUST go through make_owner_only_dir (0o700 + DACL), NOT
    # restrict_to_owner (0o600, breaks POSIX traverse).
    made_dir_names = [p.name for p in made_owner_dirs]
    assert "overlay" in made_dir_names, f"overlay_dir not via make_owner_only_dir: {made_owner_dirs}"
    assert "stubs" in made_dir_names, f"stubs_dir not via make_owner_only_dir: {made_owner_dirs}"

    # Files (env sidecar, overlay spec) still use restrict_to_owner directly
    # because the non-POSIX guard in the write path fires.
    env_sidecars = [p for p in restricted_paths if "stubs" in str(p.parent)]
    assert env_sidecars, f"env sidecar file not restricted: {restricted_paths}"
    # The overlay agent spec lives in overlay/ directory
    overlay_specs = [
        p for p in restricted_paths
        if p.suffix == ".json" and p.parent.name == "overlay"
    ]
    assert overlay_specs, f"overlay spec file not restricted: {restricted_paths}"


def test_rewriter_overlay_dirs_are_traversable_on_posix(tmp_path: Path) -> None:
    """Overlay and stubs directories must be 0o700 (owner rwx) on POSIX,
    not 0o600 (owner rw-) which would block traversal and break pooling.

    Regression test for GPT 5.6 finding: restrict_to_owner applies 0o600 to
    directories, removing the execute bit needed for traversal.
    """
    from kiro_crew import platform_compat

    if not platform_compat.IS_POSIX:
        pytest.skip("POSIX-only: directory execute bit semantics")

    from kiro_crew.mcp_gateway.rewriter import rewrite_agents

    source_dir = tmp_path / "agents"
    source_dir.mkdir()
    spec = {
        "name": "test-agent",
        "mcpServers": {
            "myserver": {
                "command": "echo",
                "args": ["hello"],
                "env": {"SECRET_TOKEN": "s3cr3t"},
                "poolable": True,
            }
        },
    }
    (source_dir / "test-agent.json").write_text(
        __import__("json").dumps(spec), encoding="utf-8"
    )

    overlay_dir = tmp_path / "overlay"
    rewrite_agents(
        source_dir=source_dir,
        overlay_dir=overlay_dir,
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "wd",
        sandbox_mode="auto",
        approval_mode="interactive",
        poolable_servers=frozenset(["myserver"]),
    )

    # Both directories must be traversable (owner execute bit set).
    stubs_dir = overlay_dir.parent / "stubs"
    for d in (overlay_dir, stubs_dir):
        assert d.exists(), f"{d} not created"
        mode = d.stat().st_mode & 0o777
        assert mode == 0o700, (
            f"{d.name} has mode {oct(mode)}, expected 0o700 (owner rwx). "
            f"0o600 would break directory traversal."
        )


def _spec_with_env(source_dir: Path) -> None:
    """Minimal agent spec whose server declares an env block, which is what
    triggers the credential sidecar write."""
    source_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "name": "test-agent",
        "mcpServers": {
            "myserver": {
                "command": "echo",
                "args": ["hello"],
                "env": {"SECRET_TOKEN": "s3cr3t"},
                "poolable": True,
            }
        },
    }
    (source_dir / "test-agent.json").write_text(json.dumps(spec), encoding="utf-8")


def _overlay_stub_args(overlay_dir: Path) -> list[str]:
    spec = json.loads((overlay_dir / "test-agent.json").read_text(encoding="utf-8"))
    return list(spec["mcpServers"]["myserver"].get("args", []))


def test_env_sidecar_directory_goes_through_make_owner_only_dir(
    tmp_path: Path, monkeypatch
) -> None:
    """The directory holding credential sidecars was created with mkdir +
    chmod(0o700). The mode argument is inert on Windows, where the DACL is the
    only carrier of access, so that left every local principal able to read the
    sidecars. make_owner_only_dir applies 0o700 on POSIX and a DACL on Windows.
    """
    from unittest.mock import patch

    from kiro_crew.mcp_gateway.rewriter import rewrite_agents

    source_dir = tmp_path / "agents"
    _spec_with_env(source_dir)
    made: list[Path] = []

    def _mock_make_owner_only_dir(path):
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        made.append(p)

    with patch(
        "kiro_crew.mcp_gateway.rewriter.platform_compat.make_owner_only_dir",
        side_effect=_mock_make_owner_only_dir,
    ):
        rewrite_agents(
            source_dir=source_dir,
            overlay_dir=tmp_path / "overlay",
            socket_path=tmp_path / "gw.sock",
            work_dir=tmp_path / "wd",
            sandbox_mode="auto",
            approval_mode="interactive",
            poolable_servers=frozenset(["myserver"]),
        )

    assert "env" in [p.name for p in made], (
        f"env sidecar dir not created owner-only: {made}"
    )


def test_failed_sidecar_protection_leaves_no_readable_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    """An icacls failure must not leave the credentials on disk.

    The previous order wrote the sidecar first (with a mode argument that is
    inert on Windows) and applied the DACL afterwards, catching the failure with
    a bare warning -- so a readable file full of API keys stayed on disk AND the
    stub was still pointed at it via --env-file. Protection is now applied to
    the temp file before any secret byte is written, so a failure leaves nothing
    behind and the sidecar is not advertised.
    """
    from unittest.mock import patch

    from kiro_crew.mcp_gateway.rewriter import rewrite_agents

    source_dir = tmp_path / "agents"
    _spec_with_env(source_dir)
    overlay_dir = tmp_path / "overlay"

    def _mock_make_owner_only_dir(path):
        Path(path).mkdir(parents=True, exist_ok=True)

    def _fail_only_for_the_sidecar(path):
        """Fail the sidecar's protection only.

        Raising for every path would also fail the overlay spec write, so the
        test would stop short of the behaviour under test.
        """
        if Path(path).parent.name == "env":
            raise OSError("icacls: access denied")

    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.rewriter.platform_compat.IS_POSIX", False
    )
    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.rewriter.platform_compat.IS_WINDOWS", True
    )
    with patch(
        "kiro_crew.mcp_gateway.rewriter.platform_compat.restrict_to_owner",
        side_effect=_fail_only_for_the_sidecar,
    ), patch(
        "kiro_crew.mcp_gateway.rewriter.platform_compat.make_owner_only_dir",
        side_effect=_mock_make_owner_only_dir,
    ):
        rewrite_agents(
            source_dir=source_dir,
            overlay_dir=overlay_dir,
            socket_path=tmp_path / "gw.sock",
            work_dir=tmp_path / "wd",
            sandbox_mode="auto",
            approval_mode="interactive",
            poolable_servers=frozenset(["myserver"]),
        )

    # Nothing containing the secret may remain anywhere the rewriter wrote.
    # Scanning tmp_path, not overlay_dir: stubs_dir is `overlay_dir.parent /
    # "stubs"`, a SIBLING of the overlay, so an overlay-only scan would miss the
    # sidecar entirely and this assertion would be vacuous.
    leaked = [
        p
        for p in tmp_path.rglob("*")
        if p.is_file() and "s3cr3t" in p.read_text(encoding="utf-8", errors="replace")
    ]
    # The agent spec the test itself authored legitimately holds the value.
    leaked = [p for p in leaked if p != source_dir / "test-agent.json"]
    assert not leaked, f"credentials left on disk after failed protection: {leaked}"

    # And the stub must not be pointed at a sidecar we failed to protect.
    assert "--env-file" not in _overlay_stub_args(overlay_dir)
