"""The MCP page's measured numbers: kiro_crew.mcp_gateway.footprint.

Every figure is read from a fixture process table, so the arithmetic is pinned
on readings, never on a constant. A process with no PSS reading turns both
figures off; a stale chat is one whose own tree runs a copy of a server that is
shared for new chats now.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from kiro_crew.dashboard.handlers.gateway_footprint import _specs  # noqa: F401  (import guard)
from kiro_crew.mcp_gateway import STUB_MODULE, footprint
from kiro_crew.mcp_gateway.footprint import ServerSpec

KIB = 1024


def _proc(root: Path, pid: int, ppid: int, argv: list[str], pss_kb: int | None) -> None:
    d = root / str(pid)
    d.mkdir()
    (d / "stat").write_bytes(f"{pid} (x) S {ppid} 0 0 0".encode())
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    if pss_kb is not None:
        (d / "smaps_rollup").write_text(f"Rss: 1 kB\nPss:   {pss_kb} kB\n", encoding="ascii")


def _stub(server: str) -> list[str]:
    flags = base64.b64encode(
        json.dumps(["--server", server, "--poolable"]).encode(), altchars=b"-_"
    )
    return ["/usr/bin/python", "-m", STUB_MODULE, f"--stub-flags-b64={flags.decode()}"]


SPECS = {
    "docs": ServerSpec(launches=(("/opt/docs/bin/docs-mcp", "serve"),), shareable=True),
    "creds": ServerSpec(launches=(("creds-agent", "mcp"),), shareable=False),
    "npm-one": ServerSpec(launches=(("npx", "-y", "@acme/one"),), shareable=True),
}


def _two_chats(tmp_path: Path, *, stub_pss: int | None = 10) -> Path:
    # Chat A (runtime 100) and chat B (runtime 200), each with its own copy of
    # docs (a wrapper + child) and creds, and chat A one stub on a shared server.
    _proc(tmp_path, 100, 1, ["kiro-cli", "acp"], 50)
    _proc(tmp_path, 101, 100, ["docs-mcp", "serve"], 5)
    _proc(tmp_path, 102, 101, ["/opt/docs/bin/docs-mcp", "serve"], 95)
    _proc(tmp_path, 103, 100, ["/usr/bin/creds-agent", "mcp"], 40)
    _proc(tmp_path, 104, 100, _stub("other"), stub_pss)
    _proc(tmp_path, 200, 1, ["kiro-cli", "acp"], 50)
    _proc(tmp_path, 201, 200, ["/opt/docs/bin/docs-mcp", "serve"], 100)
    _proc(tmp_path, 203, 200, ["creds-agent", "mcp"], 40)
    return tmp_path


def test_sums_pss_of_each_copy_tree_and_replaces_shareable_copies_with_one(
    tmp_path: Path,
) -> None:
    root = _two_chats(tmp_path)
    r = footprint.measure([("a", 100), ("b", 200)], SPECS, proc_root=root)
    assert r is not None
    # Now: docs 100+100, creds 40+40, one stub 10. The runtimes themselves are
    # not MCP servers and are not counted.
    assert r.using_now_bytes == (100 + 100 + 40 + 40 + 10) * KIB
    # With sharing: one docs copy (mean 100) plus one stub per chat that had a
    # copy (2 x 10); creds never shares; the existing stub stays.
    assert r.with_sharing_bytes == (100 + 2 * 10 + 40 + 40 + 10) * KIB
    assert r.stale_session_keys == []


def test_a_running_shared_backend_replaces_the_copies_instead_of_the_mean(
    tmp_path: Path,
) -> None:
    root = _two_chats(tmp_path)
    _proc(root, 900, 1, ["/opt/docs/bin/docs-mcp", "serve"], 70)
    r = footprint.measure([("a", 100), ("b", 200)], SPECS, [("docs", 900)], proc_root=root)
    assert r is not None
    assert r.using_now_bytes == (200 + 80 + 10 + 70) * KIB
    assert r.with_sharing_bytes == (70 + 2 * 10 + 80 + 10) * KIB


def test_one_unreadable_process_hides_both_figures(tmp_path: Path) -> None:
    root = _two_chats(tmp_path)
    (root / "102" / "smaps_rollup").unlink()
    r = footprint.measure([("a", 100), ("b", 200)], SPECS, proc_root=root)
    assert r is not None
    assert r.using_now_bytes is None and r.with_sharing_bytes is None


def test_no_stub_to_measure_hides_with_sharing_but_keeps_using_now(
    tmp_path: Path,
) -> None:
    root = _two_chats(tmp_path)
    for name in ("cmdline", "smaps_rollup", "stat"):
        (root / "104" / name).unlink()
    (root / "104").rmdir()
    r = footprint.measure([("a", 100), ("b", 200)], SPECS, proc_root=root)
    assert r is not None
    assert r.using_now_bytes == (200 + 80) * KIB
    assert r.with_sharing_bytes is None


def test_a_stub_running_its_server_below_it_is_a_copy(tmp_path: Path) -> None:
    _proc(tmp_path, 100, 1, ["kiro-cli", "acp"], 50)
    _proc(tmp_path, 101, 100, _stub("docs"), 12)
    _proc(tmp_path, 102, 101, ["/opt/docs/bin/docs-mcp", "serve"], 88)
    _proc(tmp_path, 103, 100, _stub("other"), 10)
    r = footprint.measure([("a", 100)], SPECS, proc_root=tmp_path)
    assert r is not None
    assert r.using_now_bytes == (12 + 88 + 10) * KIB


def test_npx_is_matched_under_its_npm_exec_title(tmp_path: Path) -> None:
    _proc(tmp_path, 100, 1, ["kiro-cli", "acp"], 50)
    _proc(tmp_path, 101, 100, ["npm", "exec", "@acme/one"], 30)
    _proc(tmp_path, 102, 101, ["node", "/cache/one/bin.js"], 60)
    r = footprint.measure([("a", 100)], SPECS, proc_root=tmp_path)
    assert r is not None and r.using_now_bytes == 90 * KIB


def test_marks_chats_whose_own_copy_runs_a_server_shared_now(tmp_path: Path) -> None:
    root = _two_chats(tmp_path)
    specs = {
        **SPECS,
        "docs": ServerSpec(SPECS["docs"].launches, shareable=True, shared_now=True),
    }
    # Two keys on runtime 200 (a multiplexed runtime) are both stale.
    r = footprint.measure([("a", 100), ("b", 200), ("b2", 200)], specs, proc_root=root)
    assert r is not None
    assert sorted(r.stale_session_keys) == ["a", "b", "b2"]


def test_a_process_outside_every_session_tree_is_never_counted(tmp_path: Path) -> None:
    _proc(tmp_path, 100, 1, ["kiro-cli", "acp"], 50)
    _proc(tmp_path, 300, 1, ["/opt/docs/bin/docs-mcp", "serve"], 999)
    r = footprint.measure([("a", 100)], SPECS, proc_root=tmp_path)
    assert r is not None and r.using_now_bytes == 0


def test_no_process_table_is_no_reading(tmp_path: Path) -> None:
    assert footprint.measure([("a", 1)], SPECS, proc_root=tmp_path / "missing") is None


def test_npm_single_field_process_title_is_matched(tmp_path: Path) -> None:
    """npm rewrites its title to ONE field (``npm exec @acme/one``)."""
    _proc(tmp_path, 100, 1, ["kiro-cli", "acp"], 50)
    d = tmp_path / "101"
    d.mkdir()
    (d / "stat").write_bytes(b"101 (npm exec) S 100 0 0 0")
    (d / "cmdline").write_bytes(b"npm exec @acme/one\0")
    (d / "smaps_rollup").write_text("Pss: 30 kB\n", encoding="ascii")
    _proc(tmp_path, 102, 101, ["node", "/cache/one/bin.js"], 60)
    r = footprint.measure([("a", 100)], SPECS, proc_root=tmp_path)
    assert r is not None and r.using_now_bytes == 90 * KIB


def test_two_launches_of_one_server_have_no_forecast(tmp_path: Path) -> None:
    """Two declared launches pool as two backends; one budget per name would be
    too low, so the forecast is withheld."""
    specs = {
        "docs": ServerSpec(
            launches=(
                ("/opt/docs/bin/docs-mcp", "serve"),
                ("/opt/docs/bin/docs-mcp", "serve", "--ro"),
            ),
            shareable=True,
        )
    }
    _proc(tmp_path, 100, 1, ["kiro-cli", "acp"], 50)
    _proc(tmp_path, 101, 100, ["/opt/docs/bin/docs-mcp", "serve"], 100)
    _proc(tmp_path, 102, 100, _stub("other"), 10)
    _proc(tmp_path, 200, 1, ["kiro-cli", "acp"], 50)
    _proc(tmp_path, 201, 200, ["/opt/docs/bin/docs-mcp", "serve", "--ro"], 100)
    r = footprint.measure([("a", 100), ("b", 200)], specs, proc_root=tmp_path)
    assert r is not None
    assert r.using_now_bytes == 210 * KIB
    assert r.with_sharing_bytes is None


def test_unreadable_pool_backends_null_both_figures(tmp_path: Path) -> None:
    root = _two_chats(tmp_path)
    r = footprint.measure([("a", 100), ("b", 200)], SPECS, None, proc_root=root)
    assert r is not None
    assert r.using_now_bytes is None and r.with_sharing_bytes is None


def test_a_slash_named_server_is_read_under_its_alias(tmp_path: Path) -> None:
    """A stub's ``--server`` and a pool row carry the alias; a spec keyed by the
    raw ``npm:@acme/one`` name must still be the same server."""
    specs = {"npm:@acme/one": ServerSpec(launches=(("npx", "-y", "@acme/one"),), shareable=True)}
    from kiro_crew.mcp_utils import mcp_server_alias

    alias = mcp_server_alias("npm:@acme/one")
    _proc(tmp_path, 100, 1, ["kiro-cli", "acp"], 50)
    _proc(tmp_path, 101, 100, _stub(alias), 12)
    _proc(tmp_path, 102, 101, ["npm", "exec", "@acme/one"], 88)
    _proc(tmp_path, 103, 100, _stub("other"), 10)
    _proc(tmp_path, 900, 1, ["node", "/cache/one/bin.js"], 70)
    r = footprint.measure([("a", 100)], specs, [(alias, 900)], proc_root=tmp_path)
    assert r is not None
    # The copy is shareable and a backend for it runs: no second mean copy.
    assert r.with_sharing_bytes == (10 + 70 + 10) * KIB


def test_declarations_with_different_env_have_no_forecast(tmp_path: Path) -> None:
    root = _two_chats(tmp_path)
    specs = {
        **SPECS,
        "docs": ServerSpec(SPECS["docs"].launches, shareable=True, one_identity=False),
    }
    r = footprint.measure([("a", 100), ("b", 200)], specs, proc_root=root)
    assert r is not None
    assert r.using_now_bytes is not None
    assert r.with_sharing_bytes is None


def test_two_declared_launches_have_no_forecast_even_if_one_runs(tmp_path: Path) -> None:
    """A pooled backend row names its server, not its launch; with two declared
    launches one backend can hide the other's, so no forecast."""
    specs = {
        "docs": ServerSpec(
            launches=(
                ("/opt/docs/bin/docs-mcp", "serve"),
                ("/opt/docs/bin/docs-mcp", "serve", "--ro"),
            ),
            shareable=True,
        )
    }
    _proc(tmp_path, 100, 1, ["kiro-cli", "acp"], 50)
    _proc(tmp_path, 101, 100, ["/opt/docs/bin/docs-mcp", "serve", "--ro"], 100)
    _proc(tmp_path, 102, 100, _stub("other"), 10)
    _proc(tmp_path, 900, 1, ["/opt/docs/bin/docs-mcp", "serve"], 70)
    r = footprint.measure([("a", 100)], specs, [("docs", 900)], proc_root=tmp_path)
    assert r is not None
    assert r.using_now_bytes is not None
    assert r.with_sharing_bytes is None


def test_the_most_specific_launch_wins_across_servers(tmp_path: Path) -> None:
    """A shorter launch on an earlier server must not claim a longer one's copy."""
    specs = {
        "fs-data": ServerSpec(launches=(("fs-mcp", "/data"),), shareable=True, shared_now=True),
        "fs-extra": ServerSpec(launches=(("fs-mcp", "/data", "/extra"),), shareable=True),
    }
    _proc(tmp_path, 100, 1, ["kiro-cli", "acp"], 50)
    _proc(tmp_path, 101, 100, ["fs-mcp", "/data", "/extra"], 30)
    r = footprint.measure([("a", 100)], specs, proc_root=tmp_path)
    assert r is not None
    assert r.using_now_bytes == 30 * KIB
    # The copy runs fs-extra, which is not shared: no reload can move it.
    assert r.stale_session_keys == []


def test_two_servers_tying_on_one_launch_claim_neither(tmp_path: Path) -> None:
    specs = {
        "fs-a": ServerSpec(launches=(("fs-mcp", "/data"),), shareable=True, shared_now=True),
        "fs-b": ServerSpec(launches=(("fs-mcp", "/data"),), shareable=True),
    }
    _proc(tmp_path, 100, 1, ["kiro-cli", "acp"], 50)
    _proc(tmp_path, 101, 100, ["fs-mcp", "/data"], 30)
    r = footprint.measure([("a", 100)], specs, proc_root=tmp_path)
    assert r is not None
    assert r.stale_session_keys == []
    # The copy is MCP memory no one server owns: no figure, never an undercount.
    assert r.using_now_bytes is None and r.with_sharing_bytes is None
