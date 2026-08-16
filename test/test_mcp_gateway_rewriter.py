"""Per-agent rewriter wrapping guards.

A poolable stdio server that the user has explicitly disabled must never be
wrapped into a live pooling stub -- ``_build_stub_entry`` returns a fixed shape
and would drop the ``disabled`` flag, silently re-enabling the muted server in
the agent overlay. These tests pin that guard (mirroring the settings-inject
guard in ``_injectable_settings_servers``).
"""

from __future__ import annotations

import json
import sys
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
                "alpha-mcp": {"command": sys.executable, "args": ["-a"]},
                "beta-mcp": {"command": sys.executable, "args": ["-b"]},
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
        spec = {"mcpServers": {"npm:@playwright/mcp": {"command": sys.executable}}}
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
    forward_env: bool = False,
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
        forward_env=forward_env,
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
            "live": {"command": sys.executable},
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
            "shareable": {"command": sys.executable},
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
                "command": sys.executable,
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
            "needs-env": {"command": sys.executable, "env": {"REGION": "us-west-2"}},
        },
    }
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.rewriter"):
        _rewrite(
            spec, tmp_path, stub_servers=frozenset({"needs-env"})
        )

    msgs = [r.getMessage() for r in caplog.records if "declares" in r.getMessage()]
    assert len(msgs) == 1, msgs


def test_unresolvable_bare_command_is_not_stubbed(tmp_path: Path, caplog) -> None:
    """Issue #3495 cause A: a bare command that resolves nowhere on the gateway
    search path must NOT get a stub — gatewayd's spawn would ENOENT on every
    session and degrade it through a fallback exec. The entry is left for the
    session to launch directly (its own environment may still resolve it)."""
    import logging

    spec = {
        "name": "agent-a",
        "mcpServers": {
            "ghost": {
                "command": "kirocrew-test-definitely-missing-cmd",
                "args": ["--serve"],
                "poolable": True,
            },
        },
    }
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.rewriter"):
        new_spec, wrapped = _rewrite(
            spec, tmp_path, stub_servers=frozenset({"ghost"})
        )

    entry = new_spec["mcpServers"]["ghost"]
    assert wrapped == 0
    assert _WRAPPER_MARKER not in entry
    assert entry.get("command") == "kirocrew-test-definitely-missing-cmd"
    assert "poolable" not in entry  # internal hint never reaches kiro-cli
    assert any("cannot resolve" in r.getMessage() for r in caplog.records)


def test_resolvable_bare_command_lands_absolute_in_the_stub(tmp_path: Path) -> None:
    """Issue #3495 cause A, positive half: a bare command that DOES resolve is
    baked into the stub as an absolute path, so gatewayd (running under the
    systemd --user PATH) can spawn it."""
    exe_dir, exe_name = str(Path(sys.executable).parent), Path(sys.executable).name
    spec = {
        "name": "agent-a",
        "mcpServers": {
            "bare": {"command": exe_name, "env": {"PATH": exe_dir}},
        },
    }
    new_spec, wrapped = _rewrite(
        spec, tmp_path, stub_servers=frozenset({"bare"}), forward_env=True
    )

    assert wrapped == 1
    args = new_spec["mcpServers"]["bare"]["args"]
    resolved = args[args.index("--target-command") + 1]
    assert Path(resolved).is_absolute(), resolved
    assert Path(resolved).name == exe_name


def test_env_declaring_server_is_declassified_when_forwarding_is_off(
    tmp_path: Path, caplog
) -> None:
    """Issue #3495 cause B: with declared-env forwarding OFF, pooling a server
    that declares env spawns it WITHOUT that env — it dies at prime on every
    session, trips the breaker, and falls back anyway. Pre-classify: leave it
    unwrapped so the session applies the declared env itself."""
    import logging

    spec = {
        "name": "agent-a",
        "mcpServers": {
            "needs-env": {
                "command": sys.executable,
                "env": {"API_TOKEN": "x"},
            },
        },
    }
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.rewriter"):
        off_spec, off_wrapped = _rewrite(
            spec, tmp_path, stub_servers=frozenset({"needs-env"}), forward_env=False
        )

    entry = off_spec["mcpServers"]["needs-env"]
    assert off_wrapped == 0
    assert _WRAPPER_MARKER not in entry
    assert entry.get("env") == {"API_TOKEN": "x"}  # session still gets the env
    assert any(
        "forward_declared_env" in r.getMessage() for r in caplog.records
    ), "the warning must name the knob that re-enables pooling"

    # ... and IS eligible when forwarding is on.
    on_spec, on_wrapped = _rewrite(
        spec, tmp_path, stub_servers=frozenset({"needs-env"}), forward_env=True
    )
    assert on_wrapped == 1
    assert on_spec["mcpServers"]["needs-env"].get(_WRAPPER_MARKER) is True


def test_secret_env_server_is_declassified_even_with_forwarding_on(
    tmp_path: Path,
) -> None:
    """Forwarding ON does not forward everything: rotating-secret and
    credential-scrub keys are still withheld from a shared backend (they are
    excluded from the PoolKey / re-stripped by the daemon scrub). A server
    whose declared env is entirely such keys keeps the exact cause-B
    crash-loop, so it must be declassified like the forwarding-off case."""
    spec = {
        "name": "agent-a",
        "mcpServers": {
            "needs-secret": {
                "command": sys.executable,
                "env": {"OAUTH_TOKEN": "x"},
            },
        },
    }
    out_spec, wrapped = _rewrite(
        spec, tmp_path, stub_servers=frozenset({"needs-secret"}), forward_env=True
    )
    entry = out_spec["mcpServers"]["needs-secret"]
    assert wrapped == 0
    assert _WRAPPER_MARKER not in entry
    # The session still gets the declared secret to launch it directly.
    assert entry.get("env") == {"OAUTH_TOKEN": "x"}


def test_spec_env_path_wins_over_augmented_host_path(
    tmp_path: Path, monkeypatch
) -> None:
    """The spec's declared env.PATH is the operator's explicit intent: the
    search is composed by the canonical ``env.spec_env_path`` (spec entries
    FIRST, augmented host PATH behind), so a well-known dir can never shadow a
    same-named binary the spec deliberately points elsewhere.

    ``shutil.which`` is faked (first matching dir in path order wins) so the
    ordering assertion is platform-independent; the search string itself
    comes from the REAL ``spec_env_path``, spied to prove the resolver
    delegates to it rather than hand-rolling the composition."""
    import os as _os

    from kiro_crew.mcp_gateway import rewriter as _rw

    spec_dir = tmp_path / "spec-bin"
    host_dir = tmp_path / "host-bin"
    spec_dir.mkdir()
    host_dir.mkdir()

    def _fake_which(cmd: str, path: str = "") -> str | None:
        for d in (path or "").split(_os.pathsep):
            if d in (str(spec_dir), str(host_dir)):
                return str(Path(d) / cmd)
        return None

    seen: list[str] = []
    real = _rw.spec_env_path

    def _spy(env_path: str) -> str:
        seen.append(env_path)
        return real(env_path)

    monkeypatch.setattr(_rw.shutil, "which", _fake_which)
    monkeypatch.setattr(_rw, "spec_env_path", _spy)
    monkeypatch.setenv("PATH", str(host_dir))

    resolved = _rw._resolve_target_command(
        "dupe-mcp", {"PATH": str(spec_dir)}, None
    )

    assert resolved == str(spec_dir / "dupe-mcp"), resolved
    # The resolver delegated to the canonical helper with the SPEC's PATH.
    assert seen == [str(spec_dir)]


def test_non_string_env_path_does_not_abort_the_rewrite(tmp_path: Path) -> None:
    """A hand-edited spec can carry ``"PATH": 7``; joining it would TypeError
    out of the rewrite pass and disable pooling for every agent."""
    from kiro_crew.mcp_gateway import rewriter as _rw

    resolved = _rw._resolve_target_command(
        "kirocrew-test-definitely-missing-cmd", {"PATH": 7}, None
    )
    assert resolved == ""  # unresolvable, but no exception


def test_dead_absolute_command_is_not_stubbed(tmp_path: Path) -> None:
    """An absolute path that does not exist (or is not executable) fails the
    same predicate the agent-config resolver applies — no stub, so the failure
    surfaces in the session instead of a per-session pooled-spawn ENOENT."""
    from kiro_crew.mcp_gateway import rewriter as _rw

    assert _rw._resolve_target_command(str(tmp_path / "gone-mcp"), {}, None) == ""
    live = Path(sys.executable)
    assert _rw._resolve_target_command(str(live), {}, None) == str(live)


def test_windows_authored_path_key_is_honoured(tmp_path: Path, monkeypatch) -> None:
    """A spec authored on Windows spells the key ``"Path"``; an exact
    ``"PATH"`` lookup would ignore the operator's pin."""
    import os as _os

    from kiro_crew.mcp_gateway import rewriter as _rw

    spec_dir = tmp_path / "spec-bin"
    spec_dir.mkdir()

    def _fake_which(cmd: str, path: str = "") -> str | None:
        for d in (path or "").split(_os.pathsep):
            if d == str(spec_dir):
                return str(Path(d) / cmd)
        return None

    monkeypatch.setattr(_rw.shutil, "which", _fake_which)
    resolved = _rw._resolve_target_command(
        "bare-mcp", {"Path": str(spec_dir)}, None
    )
    assert resolved == str(spec_dir / "bare-mcp"), resolved


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
            "declared": {"command": sys.executable, "poolable": True},
            "listed": {"command": sys.executable},
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
    # Forwarding ON or the env-declaring fixture is declassified (issue #3495
    # cause B) and no sidecar write happens at all.
    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.rewriter.forward_declared_env_enabled", lambda: True
    )
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

    # Sidecar machinery is under test, not pooling classification: forwarding
    # must be ON or the env-declaring fixture is declassified (issue #3495
    # cause B) and no sidecar is ever written.
    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.rewriter.forward_declared_env_enabled", lambda: True
    )

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

    # Sidecar machinery is under test, not pooling classification: forwarding
    # must be ON or the env-declaring fixture is declassified (issue #3495
    # cause B) and no sidecar is ever written.
    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.rewriter.forward_declared_env_enabled", lambda: True
    )

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


def test_stub_fingerprint_is_the_module_on_the_launch_line(tmp_path: Path) -> None:
    """``STUB_MODULE`` must be BOTH what the rewriter launches and what the
    Sessions surface matches a stub process by.

    The two counters (per-session and per-task) identify stubs by cmdline
    substring. A copy of the string that drifts from the launch line does not
    fail loudly — it reports zero stubs for a runtime that is carrying them,
    which is precisely the reading this pair of columns exists to give.
    """
    from kiro_crew import subagent
    from kiro_crew.dashboard import session_memory
    from kiro_crew.mcp_gateway import STUB_MODULE, rewriter

    source_dir = tmp_path / "agents"
    overlay_dir = tmp_path / "overlay"
    source_dir.mkdir(parents=True, exist_ok=True)
    # No ``env`` block: an env-declaring server is declassified from pooling
    # unless forwarding is enabled, and this test is about the launch line.
    (source_dir / "test-agent.json").write_text(
        json.dumps(
            {
                "name": "test-agent",
                "mcpServers": {"myserver": {"command": "echo", "args": ["hello"]}},
            }
        ),
        encoding="utf-8",
    )
    rewriter.rewrite_agents(
        source_dir=source_dir,
        overlay_dir=overlay_dir,
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "wd",
        sandbox_mode="off",
        approval_mode="auto",
        stub_servers=frozenset(["myserver"]),
    )
    args = _overlay_stub_args(overlay_dir)
    assert STUB_MODULE in args, f"launch line lost the stub module: {args}"
    # Both cmdline matchers must be that same string, not a private copy.
    assert session_memory._STUB_MARKER == STUB_MODULE
    assert subagent.STUB_MODULE == STUB_MODULE
