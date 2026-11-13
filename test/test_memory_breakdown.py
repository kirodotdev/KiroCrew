"""Tests for the whole-product memory breakdown behind the Sessions tab total.

The behaviours a naive implementation gets wrong: a process reachable from two
roots must be counted once (or the parts would add up to more than the host
holds), the desktop shell must not swallow the gateway it launched, and an
adopted MCP gateway daemon must be found without guessing at a recycled pid.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Optional

import pytest

from kiro_crew.dashboard import memory_breakdown as mb

_MIB = 1024 * 1024
_APP = "/Applications/Kiro Crew.app/Contents"
_PY = f"{_APP}/Resources/backend-dist/kirocrew-backend/bin/python3.12"

# A host as one parent map. 900 is the desktop shell, and it launched the
# gateway (100), as a freshly started desktop app does. Under the gateway:
# a session runtime (200) and its MCP stub (201), a warm-pool runtime (300),
# the MCP gateway daemon (400) with one shared server (401), and a tunnel (500).
# 901 is the shell's renderer. 950 is an unrelated process.
_CHILDREN: dict[int, list[int]] = {
    1: [900, 950],
    900: [100, 901],
    100: [200, 300, 400, 500],
    200: [201],
    400: [401],
}
_EXES = {
    900: f"{_APP}/MacOS/Kiro Crew",
    901: f"{_APP}/Frameworks/Kiro Crew Helper (Renderer).app/Contents/MacOS/Kiro Crew Helper (Renderer)",
    100: _PY,
    200: "/usr/local/bin/kiro-cli",
    201: _PY,
    300: "/usr/local/bin/kiro-cli",
    400: _PY,
    401: _PY,
    500: "/usr/bin/ssh",
    950: "/usr/bin/vim",
}


def _exe(pid: int) -> Optional[str]:
    return _EXES.get(pid)


def _name(pid: int) -> Optional[str]:
    exe = _EXES.get(pid)
    return exe.rsplit("/", 1)[-1] if exe else None


def _roots(**overrides: frozenset[int]) -> mb.Roots:
    base = {
        "sessions": frozenset({200}),
        "warm_pool": frozenset({300}),
        "other_agents": frozenset(),
        "mcp_gateway": frozenset({400}),
    }
    base.update(overrides)
    return mb.Roots(**base)


def _desktop() -> Callable[[str], bool]:
    matcher = mb.desktop_shell_matcher(_PY)
    assert matcher is not None
    return matcher


# ── attribute ───────────────────────────────────────────────────────────────


def test_every_process_lands_in_exactly_one_category() -> None:
    groups = mb.attribute(_roots(), _CHILDREN, gateway_pid=100, executable=_exe, desktop=_desktop())

    assert groups == {
        "sessions": [200, 201],
        "warm_pool": [300],
        "other_agents": [],
        "mcp_gateway": [400, 401],
        "gateway": [100],
        "gateway_helpers": [500],
        "desktop": [900, 901],
    }
    seen = [pid for pids in groups.values() for pid in pids]
    assert len(seen) == len(set(seen))


def test_the_desktop_shell_never_claims_the_gateway_it_launched() -> None:
    """The shell is the gateway's parent here, so walking the shell first would
    attribute the whole gateway tree to the desktop app."""
    groups = mb.attribute(
        mb.Roots(), _CHILDREN, gateway_pid=100, executable=_exe, desktop=_desktop()
    )

    assert groups["gateway"] == [100]
    assert sorted(groups["gateway_helpers"]) == [200, 201, 300, 400, 401, 500]
    assert groups["desktop"] == [900, 901]


def test_a_root_claimed_twice_is_counted_by_the_first_category() -> None:
    """A task runtime is often a session's own runtime: listing it under other
    agents too would count the same memory twice."""
    groups = mb.attribute(
        _roots(other_agents=frozenset({200, 300})), _CHILDREN, gateway_pid=100, executable=_exe
    )

    assert groups["sessions"] == [200, 201]
    assert groups["warm_pool"] == [300]
    assert groups["other_agents"] == []


def test_a_root_that_has_exited_contributes_nothing() -> None:
    groups = mb.attribute(_roots(warm_pool=frozenset({777})), _CHILDREN, gateway_pid=100)

    assert groups["warm_pool"] == []


def test_no_desktop_matcher_means_no_desktop_category() -> None:
    groups = mb.attribute(_roots(), _CHILDREN, gateway_pid=100, executable=_exe, desktop=None)

    assert groups["desktop"] == []


# ── desktop_shell_matcher ─────────────────────────────────────────────────


def test_a_macos_bundle_matches_its_app_and_helpers_but_not_its_own_backend() -> None:
    matcher = _desktop()

    assert matcher(f"{_APP}/MacOS/Kiro Crew")
    assert matcher(
        f"{_APP}/Frameworks/Electron Framework.framework/Helpers/chrome_crashpad_handler"
    )
    assert not matcher(_PY)
    assert not matcher("/Applications/Other.app/Contents/MacOS/Other")


def test_a_resources_layout_matches_everything_beside_resources() -> None:
    matcher = mb.desktop_shell_matcher(
        "/opt/KiroCrew/resources/backend-dist/kirocrew-backend/bin/python3"
    )
    assert matcher is not None

    assert matcher("/opt/KiroCrew/kirocrew")
    assert matcher("/opt/KiroCrew/chrome_crashpad_handler")
    assert not matcher("/opt/KiroCrew/resources/backend-dist/kirocrew-backend/bin/python3")
    assert not matcher("/usr/bin/ssh")


@pytest.mark.parametrize(
    "executable",
    [
        "/usr/local/bin/python3.12",
        "/Users/me/.venv/bin/python",
        # A Python that lives inside some other bundle is not a desktop shell.
        "/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python",
    ],
)
def test_an_install_outside_a_desktop_bundle_has_no_shell(executable: str) -> None:
    assert mb.desktop_shell_matcher(executable) is None


# ── build ───────────────────────────────────────────────────────────────────


def _sizes(pid: int) -> Optional[int]:
    return {
        100: 1300 * _MIB,
        200: 230 * _MIB,
        201: 30 * _MIB,
        300: 260 * _MIB,
        400: 70 * _MIB,
        401: 90 * _MIB,
        500: 50 * _MIB,
        900: 100 * _MIB,
        901: 1100 * _MIB,
    }.get(pid)


def _build(roots: Optional[mb.Roots] = None, **kwargs: object) -> dict[str, object]:
    params: dict[str, object] = {
        "sessions_mb": 260.0,
        "gateway_pid": 100,
        "children": _CHILDREN,
        "memory_bytes": _sizes,
        "tree_mb": lambda pid: None,
        "name": _name,
        "executable": _exe,
        "desktop": _desktop(),
        "host_mb": 32768.0,
    }
    params.update(kwargs)
    return mb.build(roots or _roots(), **params)  # type: ignore[arg-type]


def test_the_parts_add_up_to_the_total() -> None:
    out = _build()
    categories = {c["id"]: c for c in out["categories"]}  # type: ignore[union-attr]

    assert [c["id"] for c in out["categories"]] == [  # type: ignore[union-attr]
        "sessions",
        "warm_pool",
        "mcp_gateway",
        "gateway",
        "gateway_helpers",
        "desktop",
    ]
    assert categories["gateway"]["mb"] == 1300.0
    assert categories["desktop"]["mb"] == 1200.0
    assert categories["mcp_gateway"]["procs"] == 2
    assert out["total_mb"] == pytest.approx(sum(c["mb"] for c in categories.values()))
    assert out["total_mb"] == 3230.0
    assert out["host_pct"] == pytest.approx(3230.0 / 32768.0 * 100, abs=0.01)
    assert out["partial"] is False


def test_the_sessions_part_is_the_rows_own_total() -> None:
    """The panel must not disagree with the table it sits under, even when a
    per-process reading of the same tree would come out differently."""
    out = _build(sessions_mb=999.0)
    sessions = next(c for c in out["categories"] if c["id"] == "sessions")  # type: ignore[union-attr]

    assert sessions["mb"] == 999.0
    assert sessions["procs"] == 2


def test_top_names_group_by_executable_and_rank_by_memory() -> None:
    out = _build()
    mcp = next(c for c in out["categories"] if c["id"] == "mcp_gateway")  # type: ignore[union-attr]

    assert mcp["top"] == [{"name": "python3.12", "procs": 2, "mb": 160.0}]


def test_without_a_parent_map_the_roots_are_measured_as_trees_and_the_rest_is_unknown() -> None:
    trees = {300: 260.0, 400: 160.0}
    out = _build(children=None, tree_mb=trees.get)
    categories = {c["id"]: c for c in out["categories"]}  # type: ignore[union-attr]

    assert out["partial"] is True
    assert categories["warm_pool"]["mb"] == 260.0
    assert categories["mcp_gateway"]["mb"] == 160.0
    assert categories["gateway"]["mb"] == 1300.0
    assert categories["gateway_helpers"]["mb"] is None
    assert categories["desktop"]["mb"] is None
    assert out["total_mb"] == 260.0 + 260.0 + 160.0 + 1300.0


def test_without_a_parent_map_a_root_in_two_categories_is_measured_once() -> None:
    """A swept warm-pool pid stays in the pool's roots after a chat claims it,
    so the same runtime can be a session root and a warm-pool root at once.
    Measuring its tree under both would add up to more than the host holds."""
    roots = mb.Roots(
        sessions=frozenset({200}),
        warm_pool=frozenset({200, 300}),
        other_agents=frozenset({300}),
        mcp_gateway=frozenset({300, 400}),
    )
    trees = {200: 260.0, 300: 260.0, 400: 160.0}
    measured: list[int] = []

    def tree_mb(pid: int) -> Optional[float]:
        measured.append(pid)
        return trees.get(pid)

    out = _build(roots, children=None, tree_mb=tree_mb)
    categories = {c["id"]: c for c in out["categories"]}  # type: ignore[union-attr]

    assert measured == [300, 400]
    assert categories["warm_pool"]["mb"] == 260.0
    assert "other_agents" not in categories
    assert categories["mcp_gateway"]["mb"] == 160.0
    assert out["total_mb"] == 260.0 + 260.0 + 160.0 + 1300.0


# ── mcp_daemon_roots ────────────────────────────────────────────────────────


class _Info:
    def __init__(self, pid: int, start_time: str = "") -> None:
        self.pid = pid
        self.start_time = start_time


@pytest.fixture(autouse=True)
def _no_confirmed_daemon(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(mb, "_confirmed_daemon", None)


def test_a_spawned_daemon_is_known_by_its_handle(tmp_path: Path) -> None:
    assert mb.mcp_daemon_roots(4242, tmp_path / "gateway.sock") == frozenset({4242})


def test_an_adopted_daemon_is_asked_for_its_pid_through_daemon_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asked: list[Path] = []
    monkeypatch.setattr(mb, "describe_daemon", lambda path: asked.append(path) or _Info(400))
    monkeypatch.setattr(mb, "_is_daemon_process", lambda pid: pid == 400)

    assert mb.mcp_daemon_roots(None, tmp_path / "gateway.sock") == frozenset({400})
    assert asked == [tmp_path / "gateway.sock"]


def test_a_pong_naming_a_process_that_is_not_the_daemon_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Attributing a tree to whatever pid a pong names would count that whole
    tree as the MCP gateway; daemon_control's own check decides."""
    monkeypatch.setattr(mb, "describe_daemon", lambda path: _Info(777))
    monkeypatch.setattr(mb, "_is_daemon_process", lambda pid: False)

    assert mb.mcp_daemon_roots(None, tmp_path / "gateway.sock") == frozenset()


@pytest.mark.parametrize("pid", [0, 1])
def test_a_pong_without_a_usable_pid_yields_no_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pid: int
) -> None:
    monkeypatch.setattr(mb, "describe_daemon", lambda path: _Info(pid))
    monkeypatch.setattr(mb, "_is_daemon_process", lambda pid: True)

    assert mb.mcp_daemon_roots(None, tmp_path / "gateway.sock") == frozenset()


def test_no_answer_or_no_socket_yields_no_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mb, "describe_daemon", lambda path: None)

    assert mb.mcp_daemon_roots(None, tmp_path / "gateway.sock") == frozenset()
    assert mb.mcp_daemon_roots(None, None) == frozenset()


def test_only_a_bundled_interpreter_looks_for_a_desktop_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mb, "is_bundled_interpreter", lambda: False)

    assert mb.own_desktop_shell_matcher() is None


def test_a_confirmed_daemon_is_not_re_checked_on_every_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The check spawns a process off Linux, so it runs once per daemon identity,
    and again when the pong names a different start time."""
    pong = {"info": _Info(400, "1700.5")}
    checked: list[int] = []
    monkeypatch.setattr(mb, "describe_daemon", lambda path: pong["info"])
    monkeypatch.setattr(mb, "_is_daemon_process", lambda pid: checked.append(pid) or True)
    sock = tmp_path / "gateway.sock"

    assert mb.mcp_daemon_roots(None, sock) == frozenset({400})
    assert mb.mcp_daemon_roots(None, sock) == frozenset({400})
    assert checked == [400]

    pong["info"] = _Info(400, "1800.1")
    assert mb.mcp_daemon_roots(None, sock) == frozenset({400})
    assert checked == [400, 400]


def test_a_daemon_without_a_start_time_is_checked_every_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checked: list[int] = []
    monkeypatch.setattr(mb, "describe_daemon", lambda path: _Info(400))
    monkeypatch.setattr(mb, "_is_daemon_process", lambda pid: checked.append(pid) or True)

    mb.mcp_daemon_roots(None, tmp_path / "gateway.sock")
    mb.mcp_daemon_roots(None, tmp_path / "gateway.sock")

    assert checked == [400, 400]


def test_a_bundle_at_the_filesystem_root_is_not_a_desktop_shell() -> None:
    """``/x/backend-dist/...`` would make ``/`` the app root and every process on
    the host part of the shell."""
    assert mb.desktop_shell_matcher("/x/backend-dist/kirocrew-backend/bin/python3") is None
    assert mb.desktop_shell_matcher("C:/x/backend-dist/kirocrew-backend/python.exe") is None
