"""``config_snapshot_hash``: the PoolKey dimension that detects non-security
config drift across pooled MCP backends.

The stub registers a digest of the rewriter's passthrough config as the
``config_snapshot_hash`` PoolKey dimension. Without it, two agents pooling the
same backend share one backend even when their preserved operator config (the
``disabledTools`` tool allowlist, ``timeout``, ``initializationOptions``,
vendor keys, ...) diverges. The snapshot itself travels through a 0600 sidecar
in the owner-only sidecar directory, the same home the declared env uses;
argv carries only the sidecar path because /proc/<pid>/cmdline is
world-readable and free-form server config can carry secret-bearing vendor
keys. The stub hashes the sidecar contents with the shared hashing leaf.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from kiro_crew.mcp_gateway import stub as stub_mod
from kiro_crew.mcp_gateway.hashing import hash_config_snapshot
from kiro_crew.mcp_gateway.rewriter import _rewrite_single_spec


def _rewrite(spec: dict, tmp_path: Path) -> tuple[dict, int]:
    return _rewrite_single_spec(
        spec,
        stubs_dir=tmp_path / "stubs",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "wd",
        sandbox_mode="auto",
        approval_mode="interactive",
        stub_servers=frozenset({"shareable"}),
        pooling_enabled=True,
    )


def _spec(disabled_tools: list[str] | None = None) -> dict:
    # No declared env: a poolable server whose env would be withheld from a
    # shared backend is deliberately left unwrapped by the rewriter, so the
    # snapshot tests exercise the wrappable shape.
    entry: dict = {
        "command": sys.executable,
        "args": ["-m", "some_server"],
    }
    if disabled_tools is not None:
        entry["disabledTools"] = disabled_tools
    return {"name": "agent-a", "mcpServers": {"shareable": entry}}


def _argv_flag(args: list[str], flag: str) -> str | None:
    if flag not in args:
        return None
    return args[args.index(flag) + 1]


def test_rewriter_ships_the_passthrough_config_through_a_protected_sidecar(
    tmp_path: Path,
) -> None:
    new_spec, _ = _rewrite(_spec(disabled_tools=["dangerous_tool"]), tmp_path)
    argv = new_spec["mcpServers"]["shareable"]["args"]
    sidecar = _argv_flag(argv, "--config-snapshot-file")
    assert sidecar is not None, "rewriter must pass --config-snapshot-file"
    assert (
        _argv_flag(argv, "--config-snapshot-hash") is None
    ), "the config digest never belongs on world-readable argv"
    sidecar_path = Path(sidecar)
    assert sidecar_path.is_file()
    if sys.platform != "win32":
        assert sidecar_path.stat().st_mode & 0o077 == 0, "the config sidecar must be owner-only"
    assert sidecar_path.read_text(encoding="utf-8") == json.dumps(
        {"disabledTools": ["dangerous_tool"]},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    # The stub hashes exactly those contents into the PoolKey dimension.
    assert hash_config_snapshot(sidecar_path.read_text(encoding="utf-8")) == hash_config_snapshot(
        json.dumps(
            {"disabledTools": ["dangerous_tool"]},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )


def test_rewriter_snapshot_ignores_command_args_env(tmp_path: Path) -> None:
    """command/args already carry their own PoolKey dimensions; an entry whose
    snapshot is empty ships no sidecar and no flag, so specs differing only in
    already-hashed fields stay poolable onto one backend."""
    spec_b = _spec()
    spec_b["mcpServers"]["shareable"]["args"] = ["-m", "other_server"]
    argv_a = _rewrite(_spec(), tmp_path)[0]["mcpServers"]["shareable"]["args"]
    argv_b = _rewrite(spec_b, tmp_path)[0]["mcpServers"]["shareable"]["args"]
    assert _argv_flag(argv_a, "--config-snapshot-file") is None
    assert _argv_flag(argv_b, "--config-snapshot-file") is None


def test_rewriter_snapshot_tracks_passthrough_drift(tmp_path: Path) -> None:
    """Sidecar names are content-addressed, so a config drift publishes the
    new snapshot under a new name and both files coexist: a retained overlay
    keeps pointing at the snapshot it was written with, and a fresh overlay
    reads the fresh one."""
    sidecar_a = Path(
        _argv_flag(
            _rewrite(_spec(disabled_tools=["tool_a"]), tmp_path)[0]["mcpServers"]["shareable"][
                "args"
            ],
            "--config-snapshot-file",
        )
    )
    sidecar_b = Path(
        _argv_flag(
            _rewrite(_spec(disabled_tools=["tool_b"]), tmp_path)[0]["mcpServers"]["shareable"][
                "args"
            ],
            "--config-snapshot-file",
        )
    )
    assert sidecar_a != sidecar_b, "the sidecar name must address the content"
    assert sidecar_a.is_file() and sidecar_b.is_file()
    assert sidecar_a.read_text(encoding="utf-8") != sidecar_b.read_text(encoding="utf-8")


def test_rewriter_registers_the_sidecar_against_the_prune_pass(
    tmp_path: Path,
) -> None:
    """The prune pass sweeps every sidecar the rewrite did not write, and the
    fingerprint's protect list keys off the same names. A config sidecar left
    out of ``sidecars_written`` would be deleted on the next boot and the
    stub would silently fall back to the empty-snapshot partition."""
    written: set[str] = set()
    spec = {
        "name": "agent-a",
        "mcpServers": {
            "shareable": {
                "command": sys.executable,
                "args": ["-m", "some_server"],
                "disabledTools": ["dangerous_tool"],
            }
        },
    }
    _rewrite_single_spec(
        spec,
        stubs_dir=tmp_path / "stubs",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "wd",
        sandbox_mode="auto",
        approval_mode="interactive",
        stub_servers=frozenset({"shareable"}),
        pooling_enabled=True,
        sidecars_written=written,
    )
    assert any(
        name.startswith("cfg.") for name in written
    ), "the config sidecar must be registered as a written sidecar"


def test_hash_config_snapshot_is_canonical_over_key_order() -> None:
    hash_first = hash_config_snapshot('{"b": 1, "a": 2}')
    hash_second = hash_config_snapshot('{"a": 2, "b": 1}')
    assert hash_first == hash_second
    assert len(hash_first) == 64
    int(hash_first, 16)  # hex
    # Deterministic for the empty snapshot too (the stale-overlay default).
    assert hash_config_snapshot("") == hash_config_snapshot("")


def test_stub_register_hashes_the_snapshot_sidecar(tmp_path: Path) -> None:
    snapshot = tmp_path / "cfg.json"
    snapshot.write_text(
        json.dumps({"disabledTools": ["dangerous_tool"]}, sort_keys=True),
        encoding="utf-8",
    )
    base = [
        "--server",
        "s",
        "--agent",
        "a",
        "--target-command",
        "echo",
        "--work-dir",
        "/tmp",
        "--socket",
        "/tmp/gw.sock",
    ]
    payload = stub_mod.build_register_payload(
        stub_mod._parse_args(base + ["--config-snapshot-file", str(snapshot)])
    )
    assert payload["config_snapshot_hash"] == hash_config_snapshot(
        snapshot.read_text(encoding="utf-8")
    )


def test_stub_register_defaults_to_the_empty_snapshot_digest(tmp_path: Path) -> None:
    base = [
        "--server",
        "s",
        "--agent",
        "a",
        "--target-command",
        "echo",
        "--work-dir",
        "/tmp",
        "--socket",
        "/tmp/gw.sock",
    ]
    # An overlay without --config-snapshot-file registers the empty-snapshot
    # digest: that partition is RESERVED for sessions whose config is absent,
    # so a session with a real config never lands on it.
    payload_no_flag = stub_mod.build_register_payload(stub_mod._parse_args(base))
    assert payload_no_flag["config_snapshot_hash"] == hash_config_snapshot("")

    # A sidecar the stub cannot read is a different case entirely: the config
    # is unknown, so the session gets its own per-register digest and pools
    # onto nothing rather than reusing the valid empty-config partition.
    payload_missing = stub_mod.build_register_payload(
        stub_mod._parse_args(base + ["--config-snapshot-file", str(tmp_path / "absent.json")])
    )
    digest_missing = payload_missing["config_snapshot_hash"]
    assert len(digest_missing) == 64
    int(digest_missing, 16)  # hex
    assert digest_missing != hash_config_snapshot("")
    payload_missing_again = stub_mod.build_register_payload(
        stub_mod._parse_args(base + ["--config-snapshot-file", str(tmp_path / "absent.json")])
    )
    assert (
        payload_missing_again["config_snapshot_hash"] != digest_missing
    ), "each unreadable-snapshot register pools onto its own backend"
