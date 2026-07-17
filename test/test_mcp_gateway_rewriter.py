"""Per-agent rewriter wrapping guards.

A poolable stdio server that the user has explicitly disabled must never be
wrapped into a live pooling stub -- ``_build_stub_entry`` returns a fixed shape
and would drop the ``disabled`` flag, silently re-enabling the muted server in
the agent overlay. These tests pin that guard (mirroring the settings-inject
guard in ``_injectable_settings_servers``).
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER, _rewrite_single_spec


def _rewrite(spec: dict, tmp_path: Path) -> tuple[dict, int]:
    return _rewrite_single_spec(
        spec,
        stub_wrapper=tmp_path / "stub_wrapper.sh",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "wd",
        sandbox_mode="auto",
        approval_mode="interactive",
        poolable_servers=frozenset(),
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
