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

from kiro_crew.mcp_gateway.rewriter import (
    _WRAPPER_MARKER,
    _injectable_settings_servers,
    _rewrite_single_spec,
)


class TestSettingsRelocationMatchesInjection:
    """``_injectable_settings_servers`` drives BOTH the per-agent injection and
    the removal from the global settings overlay, so it must return exactly the
    servers that get injected.

    A name returned here but not injected is deleted from the only overlay that
    still lists it — the server vanishes and its MCP tools disappear, which is
    strictly worse than either stubbing it or leaving it alone.
    """

    def _spec(self) -> dict:
        return {
            "mcpServers": {
                "alpha-mcp": {"command": "/usr/bin/alpha"},
                "beta-mcp": {"command": "/usr/bin/beta"},
                "http-mcp": {"url": "https://example.invalid/mcp"},
            }
        }

    def test_unstubbed_stdio_server_is_left_in_the_settings_overlay(self) -> None:
        out = _injectable_settings_servers(self._spec(), frozenset(["beta-mcp"]))
        # beta is stubbed -> relocated. alpha is NOT -> must stay put, or it is
        # dropped from settings while nothing injects it.
        assert set(out) == {"beta-mcp"}

    def test_nothing_stubbed_relocates_nothing(self) -> None:
        """The shipped default. Every server stays raw in the settings overlay."""
        assert _injectable_settings_servers(self._spec(), frozenset()) == {}

    def test_alias_spelling_is_honoured(self) -> None:
        """The config may carry the slash-free alias while settings keeps the raw
        key; matching only the raw name would silently fail to relocate a
        stubbed slash-named server."""
        spec = {"mcpServers": {"npm:@playwright/mcp": {"command": "/usr/bin/pw"}}}
        from kiro_crew.mcp_gateway.rewriter import mcp_server_alias

        alias = mcp_server_alias("npm:@playwright/mcp")
        assert alias != "npm:@playwright/mcp"
        out = _injectable_settings_servers(spec, frozenset([alias]))
        # Keyed by the RAW name, because the caller filters raw-keyed src_servers.
        assert set(out) == {"npm:@playwright/mcp"}

    def test_http_server_is_never_relocated_even_when_listed(self) -> None:
        """HTTP/SSE needs no stub and merges globally; relocating it would strip
        it from settings for no gain."""
        out = _injectable_settings_servers(self._spec(), frozenset(["http-mcp"]))
        assert out == {}

    def test_end_to_end_unstubbed_server_survives_in_the_written_overlay(
        self, tmp_path: Path
    ) -> None:
        """Drive the real ``rewrite_agents`` and read the overlay it writes.

        The unit tests above pin the producer; this pins the WIRING. Without it
        the call site could pass the wrong set (or none) and no test would fail,
        while every unstubbed global server silently vanished from the only
        overlay that lists it.
        """
        from kiro_crew.mcp_gateway.rewriter import rewrite_agents

        source_dir = tmp_path / "agents"
        source_dir.mkdir()
        (source_dir / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "mcpServers": {}}), encoding="utf-8"
        )
        settings_dir = tmp_path / "settings"
        settings_dir.mkdir()
        (settings_dir / "mcp.json").write_text(
            json.dumps(self._spec()), encoding="utf-8"
        )

        overlay_dir = tmp_path / "overlay" / "agents"
        rewrite_agents(
            source_dir=source_dir,
            overlay_dir=overlay_dir,
            socket_path=tmp_path / "gw.sock",
            work_dir=tmp_path / "wd",
            stub_servers=frozenset(["beta-mcp"]),
        )

        written = json.loads(
            (overlay_dir.parent / "settings" / "mcp.json").read_text(encoding="utf-8")
        )
        names = set(written["mcpServers"])
        # alpha was never stubbed: it must still be here, raw, for the session to
        # launch itself. beta was stubbed, so it moved to the per-agent overlay.
        assert "alpha-mcp" in names
        assert "beta-mcp" not in names
        # HTTP always stays and merges globally.
        assert "http-mcp" in names


def _rewrite(
    spec: dict,
    tmp_path: Path,
    *,
    stub_servers: frozenset[str] = frozenset(),
    pooling_enabled: bool = True,
) -> tuple[dict, int]:
    return _rewrite_single_spec(
        spec,
        stubs_dir=tmp_path / "stubs",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "wd",
        sandbox_mode="auto",
        approval_mode="interactive",
        stub_servers=stub_servers,
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


def test_enabled_listed_server_is_still_wrapped(tmp_path: Path) -> None:
    """Guard against over-correction: a listed server that is not disabled or
    denylisted is still wrapped into a stub."""
    spec = {
        "name": "agent-a",
        "mcpServers": {
            "live": {"command": "some-mcp"},
        },
    }
    new_spec, wrapped = _rewrite(spec, tmp_path, stub_servers=frozenset({"live"}))
    entry = new_spec["mcpServers"]["live"]

    assert wrapped == 1
    assert entry.get(_WRAPPER_MARKER) is True


def test_unstubbed_server_is_left_for_the_session_to_launch(
    tmp_path: Path,
) -> None:
    """A server nobody stubbed gets NO stub, and its entry is untouched.

    Routing is the per-server opt-in, and it is what puts a stub in the path at
    all. Emitting one for every server made an upgrade add a daemon plus a proxy
    process per (server, session) to installs that asked for neither, so absence
    of a choice must mean absence of a stub — the session launches the server
    itself, exactly as with no gateway present.
    """
    spec = {
        "name": "agent-a",
        "mcpServers": {
            "stateful": {"command": "some-mcp"},
        },
    }
    new_spec, wrapped = _rewrite(spec, tmp_path)
    entry = new_spec["mcpServers"]["stateful"]

    assert wrapped == 0
    assert _WRAPPER_MARKER not in entry
    assert entry.get("command") == "some-mcp"
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
        spec, tmp_path, stub_servers=frozenset({"shareable"})
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
    non-problem. Reachable whenever a stubbed server runs with sharing off, which
    is the useful middle state for a stateful server.
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
        new_spec, wrapped = _rewrite(
            spec,
            tmp_path,
            stub_servers=frozenset({"needs-env"}),
            pooling_enabled=False,
        )

    assert wrapped == 1  # stubbed
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
            spec, tmp_path, stub_servers=frozenset({"needs-env"})
        )

    msgs = [r.getMessage() for r in caplog.records if "declares" in r.getMessage()]
    assert len(msgs) == 1, msgs
    assert "shared" in msgs[0]


def test_pooling_disabled_still_wraps_but_shares_nothing(tmp_path: Path) -> None:
    """Pooling off is not stubs off.

    With ``mcp_gateway.enabled`` false every LISTED server keeps its stub -- so
    MCP Apps keeps working -- and nothing is marked shareable, so each connection
    gets its own backend. A spec-level ``poolable: true`` neither overrides the
    operator's global switch nor opts the server in: the config list is the only
    thing that produces a stub.
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
        stub_servers=frozenset({"listed"}),
        pooling_enabled=False,
    )

    assert wrapped == 1
    listed = new_spec["mcpServers"]["listed"]
    assert listed.get(_WRAPPER_MARKER) is True, "listed lost its stub"
    assert "--poolable" not in listed["args"], "listed still marked shareable"

    declared = new_spec["mcpServers"]["declared"]
    assert declared.get(_WRAPPER_MARKER) is not True, (
        "a spec-level poolable key must not opt a server in"
    )
    assert "poolable" not in declared, "the internal hint must never reach the overlay"


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
            stub_servers=frozenset(["myserver"]),
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
        stub_servers=frozenset(["myserver"]),
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
            stub_servers=frozenset(["myserver"]),
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
            stub_servers=frozenset(["myserver"]),
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
