"""Routing through the gateway is opt-in, per server.

The previous default gave every stdio server a stub, so an upgrade added a daemon
plus one proxy process per (server, session) to installs that had asked for
neither. These tests pin the replacement: nothing is rewritten unless the
operator stubbed it, and sharing is a separate global decision over that set.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.mcp_gateway.rewriter import _rewrite_single_spec

STUB_MARKER = "mcp_gateway.stub"


def _rewrite(
    spec: dict,
    tmp_path: Path,
    *,
    stub: frozenset[str] = frozenset(),
    pooling_enabled: bool = False,
) -> tuple[dict, int]:
    return _rewrite_single_spec(
        spec,
        stubs_dir=tmp_path / "stubs",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "wd",
        sandbox_mode="auto",
        approval_mode="interactive",
        stub_servers=stub,
        pooling_enabled=pooling_enabled,
    )


def _spec() -> dict:
    return {
        "name": "kirocrew",
        "mcpServers": {
            "alpha-mcp": {"command": "alpha", "args": ["--serve"]},
            "beta-mcp": {"command": "beta", "args": []},
        },
    }


def _argv(entry: dict) -> str:
    return " ".join([str(entry.get("command", ""))] + [str(a) for a in entry.get("args") or []])


def test_nothing_is_routed_by_default(tmp_path: Path) -> None:
    """The shipped default costs nothing: no stub set, no stub, no daemon.

    This is the whole point of the change, so it is asserted on the emitted
    overlay rather than on a flag: every entry must come out byte-identical to
    the source, which is what makes the session launch the server itself.
    """
    spec = _spec()
    out, wrapped = _rewrite(spec, tmp_path)

    assert wrapped == 0
    for name, entry in spec["mcpServers"].items():
        assert out["mcpServers"][name] == entry, name
        assert STUB_MARKER not in _argv(out["mcpServers"][name])


def test_only_the_routed_server_gets_a_stub(tmp_path: Path) -> None:
    """Opting one server in must not drag its neighbours along."""
    out, wrapped = _rewrite(_spec(), tmp_path, stub=frozenset({"alpha-mcp"}))

    assert wrapped == 1
    assert STUB_MARKER in _argv(out["mcpServers"]["alpha-mcp"])
    assert STUB_MARKER not in _argv(out["mcpServers"]["beta-mcp"])


def test_routed_without_sharing_is_a_private_backend(tmp_path: Path) -> None:
    """Stub-only is the useful middle state for a stateful server: it can render
    server-authored UI without ever getting a co-tenant."""
    out, _ = _rewrite(
        _spec(), tmp_path, stub=frozenset({"alpha-mcp"}), pooling_enabled=False
    )

    argv = _argv(out["mcpServers"]["alpha-mcp"])
    assert STUB_MARKER in argv
    assert "--poolable" not in argv


def test_sharing_applies_to_every_routed_server(tmp_path: Path) -> None:
    """Sharing is global over the stub set — there is no per-server sharing
    switch left to consult, so both stubbed servers must be marked."""
    out, wrapped = _rewrite(
        _spec(),
        tmp_path,
        stub=frozenset({"alpha-mcp", "beta-mcp"}),
        pooling_enabled=True,
    )

    assert wrapped == 2
    for name in ("alpha-mcp", "beta-mcp"):
        assert "--poolable" in _argv(out["mcpServers"][name]), name


def test_sharing_alone_routes_nothing(tmp_path: Path) -> None:
    """Turning sharing on with an empty stub set must not resurrect the old
    default. Sharing decides how a stubbed backend is acquired; it never routes."""
    spec = _spec()
    out, wrapped = _rewrite(spec, tmp_path, stub=frozenset(), pooling_enabled=True)

    assert wrapped == 0
    for name, entry in spec["mcpServers"].items():
        assert out["mcpServers"][name] == entry, name


def test_a_spec_level_poolable_key_no_longer_opts_a_server_in(tmp_path: Path) -> None:
    """``poolable: true`` in an agent spec is retired as a stub trigger.

    It could not be honoured coherently. The broker's start gate and the
    session's overlay resolution both read ``mcp_gateway.stub_servers``, and
    teaching them to read agent specs instead would put filesystem IO behind
    every ``KiroCrewConfig.load()``. Wrapping the entry here while those gates
    stayed blind produced a stub nothing pointed at, plus a dashboard row that
    reported ``stub`` for a server that had none.

    So the config list is the single source of truth, and the key is stripped
    from the emitted entry exactly as before — it is ours, not kiro-cli's, and
    must never reach the overlay.
    """
    spec = {
        "name": "kirocrew",
        "mcpServers": {
            "alpha-mcp": {"command": "alpha", "args": [], "poolable": True},
            "beta-mcp": {"command": "beta", "args": []},
        },
    }
    out, wrapped = _rewrite(spec, tmp_path, stub=frozenset())

    assert wrapped == 0, "a spec key must not conjure a stub the gates know nothing about"
    assert STUB_MARKER not in _argv(out["mcpServers"]["alpha-mcp"])
    assert STUB_MARKER not in _argv(out["mcpServers"]["beta-mcp"])
    # The internal hint is ours, not kiro-cli's, and must never reach the overlay.
    assert "poolable" not in out["mcpServers"]["alpha-mcp"]
    assert "poolable" not in out["mcpServers"]["beta-mcp"]


def test_the_config_list_still_opts_that_same_server_in(tmp_path: Path) -> None:
    """The replacement path: list it, and the spec key is irrelevant either way."""
    spec = {
        "name": "kirocrew",
        "mcpServers": {
            "alpha-mcp": {"command": "alpha", "args": [], "poolable": True},
            "beta-mcp": {"command": "beta", "args": []},
        },
    }
    out, wrapped = _rewrite(spec, tmp_path, stub=frozenset({"alpha-mcp"}))

    assert wrapped == 1
    assert STUB_MARKER in _argv(out["mcpServers"]["alpha-mcp"])
    assert STUB_MARKER not in _argv(out["mcpServers"]["beta-mcp"])
    assert "poolable" not in out["mcpServers"]["beta-mcp"]
