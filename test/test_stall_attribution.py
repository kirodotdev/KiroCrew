"""In-flight cron markers and loop-stall attribution.

The dumps come from ``stall_dump_helpers`` (faulthandler's own format behind
the store's real header). Every PID used is one that cannot be alive: the tests use a
``pid_exists`` stand-in wired through ``RunningMarker.owner_alive`` so no real
process table is consulted.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest
from stall_dump_helpers import (
    CHAT_STACK,
    CRON_STACK,
    DISPATCH_STACK,
    IDLE_WORKER,
    PROMPT_LOOP_STACK,
    SLACK_STACK,
    write_dump,
)

from kiro_crew import cron_inflight, stall_attribution
from kiro_crew.dashboard import crash_dump_store
from kiro_crew.stall_attribution import (
    attribute_dump,
    attribute_latest_stall,
    classify_surface,
    describe,
    parse_frames,
)

# One xdist worker for the whole module: every test here derives from ONE module-cached
# scan of src/ (rglob + ast.parse, ~30s). Under `--dist loadgroup` an unmarked module is
# spread across workers and each worker re-pays that scan -- measured at 5 workers x 40-75s
# per full run for this file alone. Grouping keeps the cache single-copy per run.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_stall_attribution")

#: A ``def`` or ``async def`` at any indentation: the names the rule table may cite.
_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(", re.MULTILINE)


@pytest.fixture
def dead_pids(monkeypatch: pytest.MonkeyPatch) -> set[int]:
    """Every PID is dead except this process (and those a test adds)."""
    alive: set[int] = set()
    monkeypatch.setattr(crash_dump_store, "pid_exists", lambda pid: pid in alive)
    return alive


# ─────────────────────────────────────────────────────────────────────────────
# cron_inflight
# ─────────────────────────────────────────────────────────────────────────────


class TestMarkers:
    def test_write_read_clear_round_trip(self, tmp_path: Path) -> None:
        cron_inflight.write_marker(tmp_path, "caeb441a", "twb-refresh", 1000.0, run="aaaa")
        (marker,) = cron_inflight.read_markers(tmp_path)
        assert (marker.job_id, marker.name, marker.started_at, marker.pid, marker.path.name) == (
            "caeb441a",
            "twb-refresh",
            1000.0,
            os.getpid(),
            "caeb441a.aaaa.json",
        )
        # The token lives in the name only: the payload carries no copy of it.
        assert "run" not in json.loads(marker.path.read_text(encoding="utf-8"))
        assert marker.owner_alive() is True
        cron_inflight.clear_marker(tmp_path, "caeb441a", "aaaa")
        assert cron_inflight.read_markers(tmp_path) == []
        # clearing twice is not an error
        cron_inflight.clear_marker(tmp_path, "caeb441a", "aaaa")

    def test_a_clear_removes_only_its_own_runs_marker(self, tmp_path: Path) -> None:
        """Two runs of one job are two files; a run's clear cannot reach the other's.

        The finalizer of a cancelled run clears after an unwind that can span a
        whole session teardown, by which time a replacement run of the same job
        has written its own marker: a clear keyed by job id alone would take it.
        """
        cron_inflight.write_marker(tmp_path, "caeb441a", "twb-refresh", 1000.0, run="aaaa")
        cron_inflight.write_marker(tmp_path, "caeb441a", "twb-refresh", 1010.0, run="bbbb")
        assert [(m.path.name, m.started_at) for m in cron_inflight.read_markers(tmp_path)] == [
            ("caeb441a.aaaa.json", 1000.0),
            ("caeb441a.bbbb.json", 1010.0),
        ]
        cron_inflight.clear_marker(tmp_path, "caeb441a", "aaaa")
        (survivor,) = cron_inflight.read_markers(tmp_path)
        assert (survivor.path.name, survivor.started_at) == ("caeb441a.bbbb.json", 1010.0), (
            "the first run's clear removed the replacement run's marker: "
            f"{[m.path.name for m in cron_inflight.read_markers(tmp_path)]!r}"
        )
        cron_inflight.clear_marker(tmp_path, "caeb441a", "bbbb")
        assert cron_inflight.read_markers(tmp_path) == []

    def test_a_write_and_a_clear_name_a_run(self, tmp_path: Path) -> None:
        """There is no token-less form left to write or clear."""
        with pytest.raises(TypeError):
            cron_inflight.write_marker(tmp_path, "caeb441a", "x", 1.0)  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            cron_inflight.clear_marker(tmp_path, "caeb441a")  # type: ignore[call-arg]
        assert cron_inflight.read_markers(tmp_path) == []

    @pytest.mark.parametrize("bad", ["", "a/b", "a\\b", "..", "a.b", "x" * 65])
    def test_marker_path_refuses_tokens_that_are_not_a_nonce(
        self, tmp_path: Path, bad: str
    ) -> None:
        with pytest.raises(ValueError):
            cron_inflight.marker_path(tmp_path, "caeb441a", bad)
        cron_inflight.write_marker(tmp_path, "caeb441a", "x", 1.0, run=bad)  # refused, no raise
        assert cron_inflight.read_markers(tmp_path) == []

    def test_a_marker_without_a_run_token_is_read(self, tmp_path: Path) -> None:
        """A file written before the token existed still names its job, as does
        one carrying the payload key an older build wrote beside the name."""
        d = cron_inflight.running_dir(tmp_path)
        d.mkdir(parents=True)
        (d / "legacy1.json").write_text(
            json.dumps(
                {
                    "job_id": "legacy1",
                    "name": "l",
                    "started_at": 5.0,
                    "pid": os.getpid(),
                    "pid_domain": crash_dump_store._pid_domain(),
                }
            ),
            encoding="utf-8",
        )
        (d / "legacy2.cccc.json").write_text(
            json.dumps(
                {
                    "job_id": "legacy2",
                    "name": "l",
                    "started_at": 6.0,
                    "pid": os.getpid(),
                    "pid_domain": crash_dump_store._pid_domain(),
                    "run": "cccc",
                }
            ),
            encoding="utf-8",
        )
        assert [(m.job_id, m.path.name) for m in cron_inflight.read_markers(tmp_path)] == [
            ("legacy1", "legacy1.json"),
            ("legacy2", "legacy2.cccc.json"),
        ]

    @pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", "../x"])
    def test_marker_path_refuses_ids_that_leave_the_directory(
        self, tmp_path: Path, bad: str
    ) -> None:
        with pytest.raises(ValueError):
            cron_inflight.marker_path(tmp_path, bad, "aaaa")

    def test_write_is_best_effort(self, tmp_path: Path) -> None:
        # A bad id raises inside; the writer swallows it -- a marker must never
        # fail a run.
        cron_inflight.write_marker(tmp_path, "../escape", "x", 1.0, run="aaaa")
        assert not (tmp_path.parent / "escape.aaaa.json").exists()

    def test_unparseable_and_tmp_files_are_skipped(self, tmp_path: Path) -> None:
        d = cron_inflight.running_dir(tmp_path)
        d.mkdir()
        (d / "junk.json").write_text("not json", encoding="utf-8")
        (d / "half.json.tmp").write_text("{}", encoding="utf-8")
        (d / "missing.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
        (d / "big.json").write_text("{" + " " * 5000 + "}", encoding="utf-8")
        assert cron_inflight.read_markers(tmp_path) == []

    def test_abandoned_is_dead_owner_only(self, tmp_path: Path, dead_pids: set[int]) -> None:
        cron_inflight.write_marker(tmp_path, "live", "l", 1.0, run="aaaa")  # this process
        d = cron_inflight.running_dir(tmp_path)
        (d / "dead.json").write_text(
            json.dumps(
                {
                    "job_id": "dead",
                    "name": "d",
                    "started_at": 2.0,
                    "pid": 4_000_001,
                    "pid_domain": crash_dump_store._pid_domain(),
                }
            ),
            encoding="utf-8",
        )
        (d / "other.json").write_text(
            json.dumps(
                {
                    "job_id": "other",
                    "name": "o",
                    "started_at": 3.0,
                    "pid": 4_000_002,
                    "pid_domain": crash_dump_store._pid_domain(),
                }
            ),
            encoding="utf-8",
        )
        dead_pids.add(4_000_002)  # a live sibling gateway
        assert [m.job_id for m in cron_inflight.abandoned_markers(tmp_path)] == ["dead"]
        assert cron_inflight.sweep_abandoned_markers(tmp_path) == 1
        assert sorted(m.job_id for m in cron_inflight.read_markers(tmp_path)) == ["live", "other"]

    @pytest.mark.skipif(os.name != "posix", reason="symlinks are a POSIX fixture")
    def test_a_leaf_link_is_refused_even_without_o_nofollow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows has no ``O_NOFOLLOW`` (the flag opens as 0), so the name is
        checked before the open; modelled here by zeroing the flag on POSIX."""
        monkeypatch.setattr(cron_inflight, "_O_NOFOLLOW", 0)
        d = cron_inflight.running_dir(tmp_path)
        d.mkdir()
        real = tmp_path / "elsewhere.json"
        real.write_text(
            json.dumps({"job_id": "linked", "name": "l", "started_at": 1.0, "pid": 1}),
            encoding="utf-8",
        )
        os.symlink(real, d / "linked.json")
        assert cron_inflight.read_markers(tmp_path) == []
        assert cron_inflight._read_own_file(d / "linked.json", 4096) is None

    @pytest.mark.skipif(os.name != "posix", reason="symlinks are a POSIX fixture")
    def test_clear_does_not_delete_through_a_linked_directory(self, tmp_path: Path) -> None:
        """``unlink`` follows every component but the last, so a link AT
        ``cron-running`` would make a run's cleanup delete a file in its target."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        victim = elsewhere / "abc123.aaaa.json"
        victim.write_text("{}", encoding="utf-8")
        os.symlink(elsewhere, cron_inflight.running_dir(tmp_path))
        cron_inflight.clear_marker(tmp_path, "abc123", "aaaa")
        assert victim.exists()


# ─────────────────────────────────────────────────────────────────────────────
# stall_attribution
# ─────────────────────────────────────────────────────────────────────────────


class TestSurface:
    def test_frames_parse(self) -> None:
        frames = parse_frames(CRON_STACK)
        assert frames[0].func == "is_sensitive_bash_command"
        assert frames[0].line == 7969
        assert frames[0].short == "__init__.py:7969 is_sensitive_bash_command"
        assert len(frames) == len(CRON_STACK)

    def test_cron_wins_over_the_slack_gateway_module_it_passes_through(self) -> None:
        # A cron turn's stack contains slack/gateway.py frames; the OUTERMOST
        # Kiro Crew frame is the cron run, and that is what names the surface.
        assert classify_surface(parse_frames(CRON_STACK)) == "cron"

    def test_dashboard_chat_and_slack(self) -> None:
        assert classify_surface(parse_frames(CHAT_STACK)) == "dashboard chat"
        assert classify_surface(parse_frames(SLACK_STACK)) == "slack"

    def test_session_event_dispatch_named_instead_of_unknown(self) -> None:
        """The per-turn event drain is a named surface.

        Its outer frames are ``dashboard/state.py``, which matches no rule of
        its own, so the drain's own entry is the only thing that keeps a dump
        wedged in this loop out of "unknown" -- where the doctor can say
        nothing about it."""
        assert classify_surface(parse_frames(DISPATCH_STACK)) == "event dispatch (read-loop drain)"

    def test_both_read_loops_share_the_one_surface(self) -> None:
        # Same defect class, two transports (shared runtime vs dedicated
        # process). One label, so a dump is attributed whichever loop wedged.
        assert classify_surface(parse_frames(PROMPT_LOOP_STACK)) == (
            "event dispatch (read-loop drain)"
        )

    def test_an_entry_point_further_out_still_names_the_surface(self) -> None:
        # The drain rule must not shadow a real surface: the walk is bottom-up,
        # so a chat turn whose stack happens to pass through the drain is still
        # attributed to the turn that started it.
        assert classify_surface(parse_frames(DISPATCH_STACK[:1] + CHAT_STACK)) == "dashboard chat"

    def test_unknown_when_no_crew_frame(self) -> None:
        assert classify_surface(parse_frames(IDLE_WORKER)) == "unknown"
        assert classify_surface([]) == "unknown"

    def test_windows_paths_classify_too(self) -> None:
        win = [r'  File "C:\Users\u\venv\Lib\site-packages\kiro_crew\cron.py", line 1 in _execute']
        assert classify_surface(parse_frames(win)) == "cron"

    def test_rules_name_modules_and_functions_that_exist(self) -> None:
        """Every path fragment and function name in the rule table resolves in
        the package. A rename that stranded a rule would otherwise degrade
        ``classify_surface`` to "unknown" -- and the breaker to a no-op -- with
        CI still green."""
        import kiro_crew

        pkg = Path(kiro_crew.__file__).resolve().parent
        defs: set[str] = set()
        for py in pkg.rglob("*.py"):
            if "_vendor" in py.parts:
                continue
            defs.update(_DEF_RE.findall(py.read_text(encoding="utf-8", errors="replace")))
        for label, fragments, funcs in stall_attribution._SURFACE_RULES:
            for fragment in fragments:
                rel = fragment.removeprefix("/kiro_crew/")
                assert (pkg / rel).exists(), f"{label}: no such module or package {fragment!r}"
            for func in funcs:
                assert func in defs, f"{label}: no function named {func!r} in kiro_crew"
        for gate in stall_attribution._GATE_FILES:
            # ``exists`` rather than ``is_file``: a gate entry names a module file or
            # a package directory whose submodules all count, the same shape the
            # surface fragments above are checked with.
            assert (pkg / gate.removeprefix("/kiro_crew/")).exists(), gate


class TestAttribution:
    def _marker(
        self,
        base: Path,
        job_id: str,
        name: str,
        pid: int,
        started_at: float,
        *,
        domain: str | None = None,
        start: str | None = None,
    ) -> None:
        d = cron_inflight.running_dir(base)
        d.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "job_id": job_id,
            "name": name,
            "started_at": started_at,
            "pid": pid,
            "pid_domain": domain or crash_dump_store._pid_domain(),
        }
        if start is not None:
            payload["pid_start"] = start
        (d / f"{job_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_replacement_container_pid_1_is_not_the_crashed_gateway(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        """In a container the gateway is PID 1, and so is its replacement. The
        marker and the dump both carry the dying process's PID domain (host +
        PID namespace) and start id; the reader is in a different domain, so a
        local probe of PID 1 says nothing -- the identity match is the join."""
        now = time.time()
        old_domain = "crew-7f3a/pid:[4026532001]"
        dead_pids.add(1)  # the REPLACEMENT is alive as PID 1 here
        dump = write_dump(
            tmp_path / "dumps", 1, CRON_STACK, mtime=now - 30, domain=old_domain, start="4711"
        )
        self._marker(tmp_path, "c1", "in-container", 1, now - 60, domain=old_domain, start="4711")
        a = attribute_dump(dump, tmp_path)
        assert a.owner_pid == 1
        assert a.job is not None and a.job.job_id == "c1"
        # And the marker is not swept as "abandoned" either: this host cannot
        # judge a foreign-domain PID, so only the dump may speak for it.
        assert a.job.owner_alive() is None
        assert cron_inflight.abandoned_markers(tmp_path) == []

    def test_recycled_pid_does_not_keep_a_marker_alive(
        self, tmp_path: Path, dead_pids: set[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = time.time()
        domain = crash_dump_store._pid_domain()
        dead_pids.add(4_000_020)  # the number is live again...
        monkeypatch.setattr(crash_dump_store, "_pid_start_id", lambda pid: "9999")
        dump = write_dump(tmp_path / "dumps", 4_000_020, CRON_STACK, mtime=now, start="1234")
        # ...but under a different start id, so the owner of the marker is gone.
        self._marker(tmp_path, "r1", "recycled", 4_000_020, now - 10, domain=domain, start="1234")
        # A marker whose start id does not match the dump's belongs to some
        # other incarnation of that PID: dead, but not this dump's run.
        self._marker(tmp_path, "r2", "other-life", 4_000_020, now - 20, domain=domain, start="0001")
        a = attribute_dump(dump, tmp_path)
        assert a.job is not None and a.job.job_id == "r1"
        assert [m.job_id for m in a.unrelated_abandoned] == ["r2"]
        assert sorted(m.job_id for m in cron_inflight.abandoned_markers(tmp_path)) == ["r1", "r2"]

    @pytest.mark.skipif(os.name != "posix", reason="symlinks are a POSIX fixture")
    def test_a_linked_marker_directory_is_not_read(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        """A link AT ``cron-running`` would make every O_NOFOLLOW child open
        resolve inside its target; a marker forged there must name nothing."""
        now = time.time()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "forged.json").write_text(
            json.dumps(
                {"job_id": "forged", "name": "victim", "started_at": now - 10, "pid": 4_000_021}
            ),
            encoding="utf-8",
        )
        (elsewhere / cron_inflight.BREAKER_CLAIM_FILE).write_text("x\n", encoding="utf-8")
        os.symlink(elsewhere, cron_inflight.running_dir(tmp_path))
        dump = write_dump(tmp_path / "dumps", 4_000_021, CRON_STACK, mtime=now)
        assert cron_inflight.read_markers(tmp_path) == []
        assert cron_inflight.read_claim(tmp_path) == ""
        assert cron_inflight.read_recorded_attribution(tmp_path, dump.name) is None
        a = attribute_dump(dump, tmp_path)
        assert a.job is None and a.candidates == []
        # Nor written through: the linked parent refuses the marker write.
        cron_inflight.write_marker(tmp_path, "w", "w", now, run="aaaa")
        assert sorted(p.name for p in elsewhere.iterdir()) == [
            cron_inflight.BREAKER_CLAIM_FILE,
            "forged.json",
        ]

    def test_single_matching_marker_names_the_job(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        dumps = tmp_path / "dumps"
        now = time.time()
        dump = write_dump(dumps, 4_000_010, CRON_STACK, mtime=now - 60)
        self._marker(tmp_path, "caeb441a", "twb-refresh", 4_000_010, now - 90)
        a = attribute_dump(dump, tmp_path)
        assert a.surface == "cron"
        assert a.owner_pid == 4_000_010
        assert a.gate is not None and a.gate.func == "is_sensitive_bash_command"
        assert a.job is not None and a.job.job_id == "caeb441a"
        lines = describe(a)
        assert (
            lines[0]
            == "stuck in the tool permission gate (__init__.py:7969 is_sensitive_bash_command)"
        )
        assert "was executing cron job 'twb-refresh' (caeb441a)" in lines[1]
        assert lines[2] == "recommended: kirocrew cron pause caeb441a"

    def test_two_markers_name_candidates_not_a_job(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        now = time.time()
        dump = write_dump(tmp_path / "dumps", 4_000_011, CRON_STACK, mtime=now)
        self._marker(tmp_path, "a1", "hourly", 4_000_011, now - 10)
        self._marker(tmp_path, "b2", "daily", 4_000_011, now - 5)
        a = attribute_dump(dump, tmp_path)
        assert a.job is None and len(a.candidates) == 2
        text = "\n".join(describe(a))
        assert "2 jobs were in flight" in text
        assert "cannot name one job" in text
        assert "recommended:" not in text

    def test_two_runs_of_one_job_are_one_candidate(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        """Markers are one file per RUN: a cancelled run's finalizer still pending
        when the replacement wrote its own leaves two files for one job at a hard
        exit. They name one job, and the newest start is the run in flight."""
        now = time.time()
        domain = crash_dump_store._pid_domain()
        dump = write_dump(tmp_path / "dumps", 4_000_012, CRON_STACK, mtime=now)
        cron_inflight.write_marker(tmp_path, "c3", "hourly", now - 30, run="aaaa")
        cron_inflight.write_marker(tmp_path, "c3", "hourly", now - 5, run="bbbb")
        # The writer is this (live) process; re-home both to the dead one.
        for path in sorted(cron_inflight.running_dir(tmp_path).glob("c3.*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            data.update({"pid": 4_000_012, "pid_domain": domain, "pid_start": None})
            path.write_text(json.dumps(data), encoding="utf-8")
        a = attribute_dump(dump, tmp_path)
        assert a.job is not None and (a.job.job_id, a.job.path.name) == ("c3", "c3.bbbb.json"), (
            "two markers of one job read as two jobs in flight: "
            f"{[(m.job_id, m.path.name) for m in a.candidates]!r}"
        )
        assert "recommended: kirocrew cron pause c3" in describe(a)
        # Both files are still swept once the verdict is recorded.
        assert sorted(m.path.name for m in cron_inflight.abandoned_markers(tmp_path)) == [
            "c3.aaaa.json",
            "c3.bbbb.json",
        ]
        assert cron_inflight.record_attribution(tmp_path, dump.name, a.candidates, [])
        recorded = cron_inflight.read_recorded_attribution(tmp_path, dump.name)
        assert recorded is not None and [(m.job_id, m.started_at) for m in recorded[0]] == [
            ("c3", now - 5)
        ]

    def test_pid_mismatch_and_later_marker_are_unrelated(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        now = time.time()
        dump = write_dump(tmp_path / "dumps", 4_000_012, CRON_STACK, mtime=now - 100)
        self._marker(tmp_path, "older", "from-another-crash", 4_000_099, now - 500)
        self._marker(tmp_path, "later", "next-session", 4_000_012, now - 50)  # after the dump
        a = attribute_dump(dump, tmp_path)
        assert a.job is None and a.candidates == []
        assert sorted(m.job_id for m in a.unrelated_abandoned) == ["later", "older"]
        text = "\n".join(describe(a))
        assert "no in-flight marker matches the dump's PID" in text
        assert "not by this dump" in text

    def test_live_owner_marker_is_not_evidence(self, tmp_path: Path, dead_pids: set[int]) -> None:
        now = time.time()
        dump = write_dump(tmp_path / "dumps", 4_000_013, CRON_STACK, mtime=now)
        self._marker(tmp_path, "running", "still-running", 4_000_013, now - 10)
        dead_pids.add(4_000_013)
        a = attribute_dump(dump, tmp_path)
        assert a.candidates == [] and a.unrelated_abandoned == []

    def test_non_cron_surface_is_named_and_implicates_no_job(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        now = time.time()
        dump = write_dump(tmp_path / "dumps", 4_000_014, CHAT_STACK, mtime=now)
        self._marker(tmp_path, "x", "bystander", 4_000_014, now - 10)
        a = attribute_dump(dump, tmp_path)
        assert a.surface == "dashboard chat"
        assert a.is_cron is False
        # The marker still matches by PID (the job WAS in flight) but the stack
        # says the loop was serving chat, so no job is named.
        assert len(a.candidates) == 1
        text = "\n".join(describe(a))
        assert "serving a dashboard chat turn; no cron job is implicated" in text
        assert "recommended:" not in text

    def test_unknown_surface_says_so(self, tmp_path: Path, dead_pids: set[int]) -> None:
        dump = write_dump(tmp_path / "dumps", 4_000_015, IDLE_WORKER, mtime=time.time())
        a = attribute_dump(dump, tmp_path)
        text = "\n".join(describe(a))
        assert "names no known surface; cannot attribute" in text

    def test_latest_picks_the_newest_dump_with_stacks(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        dumps = tmp_path / "dumps"
        now = time.time()
        write_dump(dumps, 4_000_016, CHAT_STACK, mtime=now - 200)
        newest = write_dump(dumps, 4_000_017, CRON_STACK, mtime=now - 100)
        # A header-only dump (a session that never wedged), written as bytes
        # rather than via open_dump_file, which would hold a process-lifetime
        # descriptor and set the module's active-dump global.
        header_only = dumps / f"{crash_dump_store.DUMP_PREFIX}20260999T000000Z.txt"
        header_only.write_text(
            "# KiroCrew loop-stall crash dump — opened test\n"  # brand-ok: mirrors the store's header bytes
            f"# PID: {os.getpid()} @ {crash_dump_store._pid_domain()}\n"
            "# If thread stacks appear below, the event loop wedged and faulthandler fired.\n\n",
            encoding="utf-8",
        )
        a = attribute_latest_stall(tmp_path, dumps)
        assert a is not None and a.dump == newest and a.surface == "cron"

    def test_describe_renders_disk_strings_inert(self, tmp_path: Path, dead_pids: set[int]) -> None:
        """A job name or frame path read off disk cannot carry a carriage return
        or an escape sequence into the doctor's terminal; printable text,
        including non-ASCII, is shown as written."""
        now = time.time()
        hostile = "\x1b[2Kok\rALL CLEAR 刷新"
        stack = [
            '  File "/opt/venv/lib/python3.12/site-packages/kiro_crew/security/__init__.py\x07", '
            "line 1 in is_sensitive_bash_command",
        ] + CRON_STACK
        dump = write_dump(tmp_path / "dumps", 4_000_030, stack, mtime=now)
        self._marker(tmp_path, "h1", hostile, 4_000_030, now - 5)
        text = "\n".join(describe(attribute_dump(dump, tmp_path)))
        assert "\x1b" not in text and "\r" not in text and "\x07" not in text
        assert "\\x1b[2Kok\\rALL CLEAR 刷新" in text
        assert "__init__.py\\x07:1" in text

    def test_record_is_kept_within_what_its_reader_accepts(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        """Many in-flight runs with long names must not produce a record the
        reader refuses (which would lose every candidate once the markers are
        swept): names are bounded and the unrelated list is trimmed first."""
        now = time.time()
        dump = write_dump(tmp_path / "dumps", 4_000_040, CRON_STACK, mtime=now)
        long_name = "n" * 2000
        for i in range(120):
            self._marker(tmp_path, f"c{i:03d}", long_name, 4_000_040, now - 10)
        for i in range(3000):
            self._marker(tmp_path, f"u{i:04d}", long_name, 4_000_099, now - 900)
        first = attribute_dump(dump, tmp_path)
        assert len(first.candidates) == 120 and len(first.unrelated_abandoned) == 3000
        assert cron_inflight.record_attribution(
            tmp_path, dump.name, first.candidates, first.unrelated_abandoned
        )
        cron_inflight.sweep_abandoned_markers(tmp_path)
        again = attribute_dump(dump, tmp_path)
        assert len(again.candidates) == 120  # never dropped
        assert all(len(m.name) == cron_inflight._RECORD_NAME_MAX_CHARS for m in again.candidates)
        assert 0 < len(again.unrelated_abandoned) < 3000  # trimmed to fit, not lost

    def test_a_marker_without_identity_names_nothing(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        """A PID-only marker was not written by this module (every marker
        carries its PID domain), so a file planted before the identity fields
        existed cannot name a job on the upgraded breaker."""
        now = time.time()
        dump = write_dump(tmp_path / "dumps", 4_000_050, CRON_STACK, mtime=now)
        d = cron_inflight.running_dir(tmp_path)
        d.mkdir(parents=True, exist_ok=True)
        (d / "legacy.json").write_text(
            json.dumps({"job_id": "legacy", "name": "l", "started_at": now - 5, "pid": 4_000_050}),
            encoding="utf-8",
        )
        assert cron_inflight.read_markers(tmp_path) == []
        a = attribute_dump(dump, tmp_path)
        assert a.job is None and a.candidates == []

    def test_no_dump_no_attribution(self, tmp_path: Path) -> None:
        assert attribute_latest_stall(tmp_path, tmp_path / "empty") is None

    def test_recorded_verdict_survives_the_marker_sweep(
        self, tmp_path: Path, dead_pids: set[int]
    ) -> None:
        # The breaker reads the markers, records what they said, then sweeps
        # them; the doctor runs afterwards and must reach the same verdict.
        now = time.time()
        dump = write_dump(tmp_path / "dumps", 4_000_018, CRON_STACK, mtime=now)
        self._marker(tmp_path, "a1", "hourly", 4_000_018, now - 10)
        self._marker(tmp_path, "b2", "daily", 4_000_018, now - 5)
        self._marker(tmp_path, "z9", "older-crash", 4_000_077, now - 900)
        first = attribute_dump(dump, tmp_path)
        cron_inflight.record_attribution(
            tmp_path, dump.name, first.candidates, first.unrelated_abandoned
        )
        assert cron_inflight.sweep_abandoned_markers(tmp_path) == 3
        assert cron_inflight.read_markers(tmp_path) == []
        again = attribute_dump(dump, tmp_path)
        assert again.job is None
        assert [m.job_id for m in again.candidates] == ["a1", "b2"]
        assert [m.job_id for m in again.unrelated_abandoned] == ["z9"]
        assert "\n".join(describe(again)) == "\n".join(describe(first))
        # A record is about ONE dump: a newer crash does not inherit it.
        newer = write_dump(tmp_path / "dumps", 4_000_019, CRON_STACK, mtime=now + 5)
        assert attribute_dump(newer, tmp_path).candidates == []

    def test_recorded_verdict_is_not_read_as_a_marker(self, tmp_path: Path) -> None:
        cron_inflight.record_attribution(tmp_path, "loopstall-x.txt", [], [])
        assert cron_inflight.read_markers(tmp_path) == []
        assert cron_inflight.read_recorded_attribution(tmp_path, "other.txt") is None
        assert cron_inflight.read_recorded_attribution(tmp_path, "loopstall-x.txt") == ([], [])
