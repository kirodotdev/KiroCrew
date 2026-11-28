"""MCP discovery: names that start the same process are probed one at a time.

One server is often configured under several names. ``probe_all`` keys each local
server by the process its probe starts (command plus args) and never runs two probes
with the same key at once, while servers with different keys still share the
``PROBE_MAX_CONCURRENCY`` slots. A timeout that only the overlap caused therefore
cannot build a quarantine streak against a server that works. The lock is the
gateway's, not one pass's: two passes at once and the shareability pre-flight wait
on the same one.

Each stand-in probe parks on a gate the test opens once the first wave of probes is
parked, so overlap is read from state, never from how long a sleep lasted.
"""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from copy import deepcopy

import pytest
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_cache,
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)

from kiro_crew.mcp_discovery import (
    PROBE_MAX_CONCURRENCY,
    McpServerInfo,
    _spawn_fingerprint,
)
from kiro_crew.testing.wait import async_wait_until

HEAVY = ("npx", ["-y", "@azure-devops/mcp", "contoso"])


def _aliases(count: int, *, env_per_name: bool = False) -> list[McpServerInfo]:
    command, args = HEAVY
    return [
        McpServerInfo(
            name=f"ado-{i}",
            command=command,
            args=list(args),
            env={"ADO_TOKEN": f"t{i}"} if env_per_name else {},
        )
        for i in range(count)
    ]


def _key(server: McpServerInfo) -> tuple[str, ...]:
    return (server.command, *map(str, server.args or []))


class _Tracker:
    """A ``probe_server`` stand-in that parks on a gate and records overlap per process.

    *verdict* turns the overlap a call saw into its status, so one stand-in serves
    both the healthy server and the one that only fails when crowded.
    """

    def __init__(self, verdict=lambda overlap: ("ok", "")) -> None:
        self.verdict = verdict
        self.gate = asyncio.Event()
        self.live: Counter = Counter()
        self.peak: Counter = Counter()
        self.live_total = 0
        self.peak_total = 0
        self.probed: list[str] = []
        self.envs: dict[str, dict] = {}

    async def __call__(self, server: McpServerInfo, **_kw) -> McpServerInfo:
        key = _key(server)
        self.live[key] += 1
        self.live_total += 1
        self.peak[key] = max(self.peak[key], self.live[key])
        self.peak_total = max(self.peak_total, self.live_total)
        self.probed.append(server.name)
        self.envs[server.name] = dict(server.env)
        try:
            await self.gate.wait()
            server.status, server.error = self.verdict(self.peak[key])
            return server
        finally:
            self.live[key] -= 1
            self.live_total -= 1

    async def run_pass(self, first_wave: int) -> list[McpServerInfo]:
        """One ``probe_all`` pass, opened once *first_wave* probes are parked."""
        from kiro_crew.mcp_discovery import probe_all

        self.gate.clear()
        self.peak.clear()
        self.peak_total = 0
        task = asyncio.ensure_future(probe_all())
        await async_wait_until(
            lambda: self.live_total >= first_wave or task.done(),
            describe=lambda: f"parked={self.live_total} probed={self.probed}",
        )
        self.gate.set()
        return await task


@pytest.fixture(autouse=True)
def _no_quarantine(tmp_path, _floor_monkeypatch):
    from kiro_crew import mcp_quarantine
    from kiro_crew.mcp_discovery import _quarantine_warned

    _floor_monkeypatch.setattr(mcp_quarantine, "_STORE_PATH", tmp_path / "quarantine.json")
    _floor_monkeypatch.setattr(mcp_quarantine, "threshold", lambda: 3)
    _clear_cache()
    _quarantine_warned.clear()
    yield
    _quarantine_warned.clear()
    _clear_cache()


def _configure(monkeypatch, rows: list[McpServerInfo], probe) -> None:
    monkeypatch.setattr("kiro_crew.mcp_discovery.list_servers", lambda: list(rows))
    monkeypatch.setattr("kiro_crew.mcp_discovery.probe_server", probe)


class TestAliasesDoNotOverlap:
    @pytest.mark.asyncio
    async def test_four_aliases_of_one_server_are_probed_one_at_a_time(self, monkeypatch) -> None:
        """The reported storm: four names of one heavy stdio server, one pass."""
        tracker = _Tracker()
        _configure(monkeypatch, _aliases(4), tracker)

        rows = await tracker.run_pass(first_wave=1)

        assert sorted(tracker.probed) == ["ado-0", "ado-1", "ado-2", "ado-3"]
        assert [r.status for r in rows] == ["ok"] * 4
        assert max(tracker.peak.values()) == 1, f"aliases overlapped: {dict(tracker.peak)}"

    @pytest.mark.asyncio
    async def test_aliases_that_differ_only_in_env_are_still_one_process(self, monkeypatch) -> None:
        """Env is not part of the key, yet each name keeps its own env and verdict."""
        tracker = _Tracker()
        _configure(monkeypatch, _aliases(3, env_per_name=True), tracker)

        await tracker.run_pass(first_wave=1)

        assert max(tracker.peak.values()) == 1
        assert tracker.envs == {f"ado-{i}": {"ADO_TOKEN": f"t{i}"} for i in range(3)}

    @pytest.mark.asyncio
    async def test_different_servers_still_fill_the_shared_slots(self, monkeypatch) -> None:
        """The rule is per process, not a serial walk: other servers take every slot.

        An alias waiting on its sibling must not hold a slot while it waits, or the
        first wave would be smaller than the cap.
        """
        tracker = _Tracker()
        rows = _aliases(3) + [
            McpServerInfo(name=f"other-{i}", command="uvx", args=[f"pkg-{i}"])
            for i in range(PROBE_MAX_CONCURRENCY + 2)
        ]
        _configure(monkeypatch, rows, tracker)

        out = await tracker.run_pass(first_wave=PROBE_MAX_CONCURRENCY)

        assert [r.name for r in out] == [r.name for r in rows], "order follows list_servers"
        assert max(tracker.peak.values()) == 1
        assert tracker.peak_total == PROBE_MAX_CONCURRENCY
        assert sorted(tracker.probed) == sorted(r.name for r in rows)

    @pytest.mark.asyncio
    async def test_remote_servers_are_not_keyed(self, monkeypatch) -> None:
        """A remote probe opens a connection, not a process, so it is not grouped."""
        tracker = _Tracker()
        rows = [McpServerInfo(name=f"r{i}", url="https://mcp.example/x") for i in range(3)]
        _configure(monkeypatch, rows, tracker)

        await tracker.run_pass(first_wave=3)

        assert tracker.peak_total == 3


def _queued(server: McpServerInfo) -> int:
    """How many checks hold or wait on the same-process lock of *server*."""
    from kiro_crew import mcp_discovery

    return mcp_discovery._same_process_users.get(_spawn_fingerprint(server), 0)


class TestAliasesDoNotOverlapAcrossChecks:
    """Checks that overlap share one lock per process.

    A Probe press during Measure All, or two Probe presses, run at the same time.
    Each test opens the gate once two copies of the process are live (the overlap
    this forbids) or once every check holds or waits on the lock, so a broken lock
    fails on the peak, never on a timeout.
    """

    @pytest.mark.asyncio
    async def test_two_overlapping_probe_passes_start_one_copy_at_a_time(self, monkeypatch) -> None:
        from kiro_crew.mcp_discovery import probe_all

        tracker = _Tracker()
        rows = _aliases(2)
        _configure(monkeypatch, rows, tracker)
        monkeypatch.setattr("kiro_crew.mcp_discovery.list_servers", lambda: deepcopy(rows))

        passes = [asyncio.ensure_future(probe_all()) for _ in range(2)]
        await async_wait_until(
            lambda: tracker.live_total >= 2 or _queued(rows[0]) == 4,
            describe=lambda: f"live={tracker.live_total} queued={_queued(rows[0])}",
        )
        tracker.gate.set()
        results = await asyncio.gather(*passes)

        assert max(tracker.peak.values()) == 1, f"aliases overlapped: {dict(tracker.peak)}"
        assert [[r.status for r in out] for out in results] == [["ok", "ok"], ["ok", "ok"]]
        assert _queued(rows[0]) == 0, "the lock is dropped once nobody holds it"

    @pytest.mark.asyncio
    async def test_the_preflight_measures_aliases_one_at_a_time(
        self, monkeypatch, tmp_path
    ) -> None:
        """Measure All: each name's two pre-flight spawns run alone, and both get a verdict."""
        from kiro_crew.mcp_gateway import evaluate, preflight

        tracker = _Tracker()
        rows = _aliases(2)
        monkeypatch.setattr(preflight, "probe_server", tracker)

        measuring = asyncio.ensure_future(
            evaluate.evaluate_new_servers(rows, tmp_path, budget=None)
        )
        await async_wait_until(
            lambda: tracker.live_total >= 2 or _queued(rows[0]) == 2 or measuring.done(),
            describe=lambda: f"live={tracker.live_total} queued={_queued(rows[0])}",
        )
        tracker.gate.set()
        known = await measuring

        assert max(tracker.peak.values()) == 1, f"aliases overlapped: {dict(tracker.peak)}"
        assert {name: row.ran for name, row in known.items()} == {"ado-0": True, "ado-1": True}
        assert sorted(tracker.probed) == ["ado-0", "ado-0", "ado-1", "ado-1"]

    @pytest.mark.asyncio
    async def test_the_preflight_waits_for_a_probe_pass_of_another_name(
        self, monkeypatch, tmp_path
    ) -> None:
        """A Probe press while Measure All runs: the pass and the pre-flight never overlap."""
        from kiro_crew.mcp_discovery import probe_all
        from kiro_crew.mcp_gateway import evaluate, preflight

        tracker = _Tracker()
        rows = _aliases(2)
        _configure(monkeypatch, rows, tracker)
        monkeypatch.setattr(preflight, "probe_server", tracker)

        probing = asyncio.ensure_future(probe_all())
        measuring = asyncio.ensure_future(
            evaluate.evaluate_new_servers(deepcopy(rows), tmp_path, budget=None)
        )
        await async_wait_until(
            lambda: tracker.live_total >= 2 or _queued(rows[0]) == 4,
            describe=lambda: f"live={tracker.live_total} queued={_queued(rows[0])}",
        )
        tracker.gate.set()
        probed, known = await asyncio.gather(probing, measuring)

        assert max(tracker.peak.values()) == 1, f"aliases overlapped: {dict(tracker.peak)}"
        assert [r.status for r in probed] == ["ok", "ok"]
        assert {name: row.ran for name, row in known.items()} == {"ado-0": True, "ado-1": True}


def _times_out_when_crowded(overlap: int) -> tuple[str, str]:
    if overlap > 1:
        return "timeout", "probe timed out after 15s"
    return "ok", ""


class TestStormTimeoutDoesNotQuarantine:
    @pytest.mark.asyncio
    async def test_a_server_that_only_fails_when_crowded_is_never_quarantined(
        self, monkeypatch
    ) -> None:
        """Each copy times out only when another copy of it ran at the same time.

        Three passes reach the threshold of three. The server answers whenever it
        runs alone, so no row may end up ``probeFailing``.
        """
        from kiro_crew import mcp_quarantine

        tracker = _Tracker(verdict=_times_out_when_crowded)
        _configure(monkeypatch, _aliases(4), tracker)

        for _ in range(3):
            rows = await tracker.run_pass(first_wave=1)
            mcp_quarantine.record_verdicts([(s.name, s.status, s.error) for s in rows])

        assert {r.status for r in rows} == {"ok"}
        failing = {n for n, st in mcp_quarantine.snapshot().items() if st.get("failing")}
        assert failing == set()

    @pytest.mark.asyncio
    async def test_a_server_that_really_fails_is_still_quarantined(self, monkeypatch) -> None:
        """The quarantine rule is unchanged for a server that fails on its own."""
        from kiro_crew import mcp_quarantine

        tracker = _Tracker(verdict=lambda overlap: ("timeout", "probe timed out after 15s"))
        _configure(monkeypatch, _aliases(2), tracker)

        for _ in range(3):
            rows = await tracker.run_pass(first_wave=1)
            mcp_quarantine.record_verdicts([(s.name, s.status, s.error) for s in rows])

        failing = {n for n, st in mcp_quarantine.snapshot().items() if st.get("failing")}
        assert failing == {"ado-0", "ado-1"}


class TestSpawnFingerprint:
    def test_command_and_args_make_the_key(self) -> None:
        a = McpServerInfo(name="a", command="npx", args=["-y", "pkg"], env={"T": "1"})
        b = McpServerInfo(name="b", command="npx", args=["-y", "pkg"], env={"T": "2"})
        c = McpServerInfo(name="c", command="npx", args=["-y", "other"])
        assert _spawn_fingerprint(a) == _spawn_fingerprint(b)
        assert _spawn_fingerprint(a) != _spawn_fingerprint(c)

    def test_remote_and_commandless_servers_have_no_key(self) -> None:
        assert _spawn_fingerprint(McpServerInfo(name="r", url="https://x.example")) is None
        assert _spawn_fingerprint(McpServerInfo(name="n")) is None

    def test_malformed_config_values_do_not_raise(self) -> None:
        """Config JSON is unvalidated and the key is computed outside ``gather``."""
        odd_args = McpServerInfo(name="x", command="bin")
        odd_args.args = 7  # type: ignore[assignment]
        assert _spawn_fingerprint(odd_args) == ("bin", "7")
        odd_command = McpServerInfo(name="y")
        odd_command.command = {"not": "a string"}  # type: ignore[assignment]
        assert _spawn_fingerprint(odd_command) is None

    def test_windows_spellings_of_one_launcher_share_a_key(self, monkeypatch) -> None:
        """Windows ignores case and the PATHEXT suffix, and a path names the same file."""
        from kiro_crew import mcp_discovery

        monkeypatch.setattr(mcp_discovery.platform_compat, "IS_WINDOWS", True)
        # Joined with the host's separator, as ``_win_exec_suffixes`` splits it.
        monkeypatch.setenv("PATHEXT", os.pathsep.join([".COM", ".EXE", ".BAT", ".CMD"]))
        keys = {
            _spawn_fingerprint(McpServerInfo(name=n, command=cmd, args=["-y", "pkg"]))
            for n, cmd in (
                ("a", "npx"),
                ("b", "npx.cmd"),
                ("c", "NPX.CMD"),
                ("d", "C:\\Program Files\\nodejs\\npx.cmd"),
            )
        }
        assert keys == {("npx", "-y", "pkg")}
        other = McpServerInfo(name="e", command="npx.cmd", args=["-y", "other"])
        assert _spawn_fingerprint(other) == ("npx", "-y", "other")
        # Only a PATHEXT suffix is one Windows ignores; any other stays in the name.
        script = McpServerInfo(name="f", command="C:\\tools\\Srv.py", args=[])
        assert _spawn_fingerprint(script) == ("srv.py",)

    def test_posix_keeps_the_command_as_written(self, monkeypatch) -> None:
        from kiro_crew import mcp_discovery

        monkeypatch.setattr(mcp_discovery.platform_compat, "IS_WINDOWS", False)
        a = McpServerInfo(name="a", command="tool", args=[])
        b = McpServerInfo(name="b", command="Tool", args=[])
        assert _spawn_fingerprint(a) != _spawn_fingerprint(b)


def _distinct(count: int) -> list[McpServerInfo]:
    return [McpServerInfo(name=f"srv-{i}", command="uvx", args=[f"pkg-{i}"]) for i in range(count)]


class TestATimedOutProbeIsNotRepeated:
    """``probe_server`` already gives a local server the longer wait it needs to start,
    so a pass keeps that one probe's verdict instead of starting the server again."""

    @pytest.mark.asyncio
    async def test_a_server_that_hangs_is_probed_once_per_pass_and_still_quarantined(
        self, monkeypatch
    ) -> None:
        from kiro_crew import mcp_quarantine

        tracker = _Tracker(verdict=lambda overlap: ("error", "timeout\nstderr: still importing"))
        _configure(monkeypatch, _distinct(2), tracker)

        for _ in range(3):
            rows = await tracker.run_pass(first_wave=2)
            mcp_quarantine.record_verdicts([(s.name, s.status, s.error) for s in rows])

        assert Counter(tracker.probed) == {"srv-0": 3, "srv-1": 3}, "one probe per pass"
        failing = {n for n, st in mcp_quarantine.snapshot().items() if st.get("failing")}
        assert failing == {"srv-0", "srv-1"}
