"""
Hole 1 of the skill-view growth: every subagent and stateless cron run got a
``workspace_root()/<key>`` directory holding one file and nothing ever removed
it. These tests pin the rule that makes the removal safe -- only the two names
Crew writes, inside the two directories Crew creates, and ``rmdir`` for every
directory -- and the invariant behind every decision: a reclaim uses only
evidence the deciding process OWNS (its registry, its own data home's pid
ledger), never a liveness probe of a pid another process wrote and never a
directory another data home marked.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import unittest.mock
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import requires_symlinks
from kiro_crew import session_pid, session_work_dir
from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.workspace_cli_settings import CLI_SETTINGS_LOCK_NAME

OTHER_HOME = "f" * 24
PREDECESSOR_PID = 12345


def _marker_text(home: str | None = None, pid: int | str | None = None) -> str:
    return f"{session_work_dir.data_home_id() if home is None else home}\n" + str(
        os.getpid() if pid is None else pid
    )


def _residue_dir(
    root: Path,
    name: str,
    *,
    age_secs: float = 0.0,
    marked: bool = True,
    home: str | None = None,
    owner_pid: int | str | None = None,
    raw_marker: str | None = None,
) -> Path:
    """A run directory exactly as a marked spawn leaves it.

    *marked* False is the directory an older build left: same residue, no
    provenance marker, which only the shutdown path (whose provenance is the
    factory flag) may reclaim. The marker names this data home and this process
    unless *home* / *owner_pid* say otherwise; *raw_marker* plants bytes as is.
    """
    work_dir = root / name
    settings = work_dir / ".kiro" / "settings"
    settings.mkdir(parents=True)
    (settings / "cli.json").write_text(json.dumps({"chat.modelDefaults": {}}), encoding="utf-8")
    (settings / CLI_SETTINGS_LOCK_NAME).write_bytes(b"")
    if marked:
        text = _marker_text(home, owner_pid) if raw_marker is None else raw_marker
        (work_dir / session_work_dir.RUN_DIR_MARKER).write_text(text, encoding="ascii")
    if age_secs:
        old = time.time() - age_secs
        for path in (
            work_dir,
            work_dir / ".kiro",
            settings,
            *settings.iterdir(),
            *work_dir.iterdir(),
        ):
            if path.exists():
                os.utime(path, (old, old))
    return work_dir


def _tree(work_dir: Path) -> list[str]:
    return sorted(str(p.relative_to(work_dir)) for p in work_dir.rglob("*"))


class TestDisposableSessionKey:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("cron:aaaa1111:bbbb2222", True),
            ("cron:aaaa1111:myagent", False),
            ("cron:aaaa1111", False),
            ("subagent:0123456789abcdef", True),
            ("subagent:0123abcd", True),
            ("subagent:0123abc", False),
            ("subagent:notes", False),
        ],
    )
    def test_only_generated_one_run_key_shapes_are_disposable(
        self, key: str, expected: bool
    ) -> None:
        assert session_work_dir.is_disposable_session_key(key) is expected
        name = key.replace(":", "_")
        assert (session_work_dir.DERIVED_NAME_RE.fullmatch(name) is not None) is expected


class TestMarker:
    def test_the_home_id_is_stable_hex_and_differs_per_data_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = session_work_dir.data_home_id()
        assert first == session_work_dir.data_home_id()
        assert session_work_dir._HOME_ID_RE.fullmatch(first)
        monkeypatch.setattr(session_work_dir, "config_dir", lambda: tmp_path / "another-home")
        other = session_work_dir.data_home_id()
        assert other != first and session_work_dir._HOME_ID_RE.fullmatch(other)

    def test_mark_run_dir_creates_and_is_idempotent(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "subagent_11111111"
        assert session_work_dir.mark_run_dir(work_dir) is True
        marker = work_dir / session_work_dir.RUN_DIR_MARKER
        assert marker.is_file()
        assert marker.read_text(encoding="ascii").splitlines() == [
            session_work_dir.data_home_id(),
            str(os.getpid()),
        ]
        assert session_work_dir.mark_run_dir(work_dir) is True
        assert marker.read_text(encoding="ascii") == _marker_text()

    def test_mark_run_dir_replaces_this_homes_predecessor(self, tmp_path: Path) -> None:
        """One gateway per data home: a same-home marker with another pid is not running."""
        work_dir = _residue_dir(tmp_path, "subagent_11111111", owner_pid=PREDECESSOR_PID)
        assert session_work_dir.mark_run_dir(work_dir) is True
        assert (work_dir / session_work_dir.RUN_DIR_MARKER).read_text(
            encoding="ascii"
        ) == _marker_text()

    def test_mark_run_dir_never_touches_another_homes_marker(self, tmp_path: Path) -> None:
        """Whatever that pid is doing, it is not this process's to judge."""
        work_dir = _residue_dir(tmp_path, "subagent_11111111", home=OTHER_HOME)
        original = (work_dir / session_work_dir.RUN_DIR_MARKER).read_text(encoding="ascii")
        assert session_work_dir.mark_run_dir(work_dir) is False
        assert (work_dir / session_work_dir.RUN_DIR_MARKER).read_text(encoding="ascii") == original

    @pytest.mark.parametrize(
        "raw",
        [
            "garbled",
            "",
            "12345",
            "not-a-home-id\n12345",
            "F" * 24 + "\n12345",
            "f" * 23 + "\n12345",
            "f" * 24 + "\n0",
            "f" * 24 + f"\n{2**31}",
            "f" * 24 + "\n12345\nextra",
            "f" * 24 + "\n" + json.dumps({"pid": 12345, "start": None}),
        ],
    )
    def test_mark_run_dir_refuses_an_unreadable_marker(self, tmp_path: Path, raw: str) -> None:
        work_dir = _residue_dir(tmp_path, "subagent_11111111", raw_marker=raw)
        assert session_work_dir.mark_run_dir(work_dir) is False
        assert (work_dir / session_work_dir.RUN_DIR_MARKER).read_text(encoding="ascii") == raw

    def test_an_oversized_marker_is_unreadable(self, tmp_path: Path) -> None:
        raw = _marker_text() + " " * 4096
        work_dir = _residue_dir(tmp_path, "subagent_11111111", raw_marker=raw)
        assert session_work_dir._read_run_dir_marker(work_dir) is None
        assert session_work_dir.mark_run_dir(work_dir) is False

    @requires_symlinks
    def test_mark_run_dir_refuses_a_linked_directory(self, tmp_path: Path) -> None:
        real = tmp_path / "elsewhere"
        real.mkdir()
        link = tmp_path / "subagent_22222222"
        link.symlink_to(real, target_is_directory=True)
        assert session_work_dir.mark_run_dir(link) is False
        assert not (real / session_work_dir.RUN_DIR_MARKER).exists()


class TestReclaimRule:
    def test_a_residue_only_directory_is_removed(self, tmp_path: Path) -> None:
        work_dir = _residue_dir(tmp_path, "subagent_deadbeef")
        assert session_work_dir.reclaim_session_work_dir(work_dir) is True
        assert not work_dir.exists()

    def test_an_emptied_prefix_of_the_chain_is_still_removed(self, tmp_path: Path) -> None:
        """A run whose overlay was never written, or already cleared, is residue too."""
        bare = tmp_path / "subagent_00000001"
        bare.mkdir()
        (tmp_path / "subagent_00000002" / ".kiro").mkdir(parents=True)
        assert session_work_dir.reclaim_session_work_dir(bare) is True
        assert session_work_dir.reclaim_session_work_dir(tmp_path / "subagent_00000002") is True
        assert not bare.exists() and not (tmp_path / "subagent_00000002").exists()

    @pytest.mark.parametrize(
        "plant",
        [
            lambda d: (d / "report.md").write_text("the run's output", encoding="utf-8"),
            lambda d: (d / "src").mkdir(),
            lambda d: (d / ".kiro" / "steering").mkdir(),
            lambda d: (d / ".kiro" / "settings" / "mcp.json").write_text("{}", encoding="utf-8"),
            lambda d: (d / ".git").mkdir(),
        ],
    )
    def test_anything_that_is_not_residue_keeps_the_whole_directory(
        self, tmp_path: Path, plant
    ) -> None:
        """THE safety property: a file the run wrote is never reachable by this code."""
        work_dir = _residue_dir(tmp_path, "subagent_cafef00d")
        plant(work_dir)
        before = _tree(work_dir)
        assert session_work_dir.reclaim_session_work_dir(work_dir) is False
        assert _tree(work_dir) == before, "a refusal must leave the tree exactly as it was"

    def test_a_missing_directory_is_a_quiet_no(self, tmp_path: Path) -> None:
        assert session_work_dir.reclaim_session_work_dir(tmp_path / "gone") is False

    def test_a_young_directory_is_kept_when_an_age_is_required(self, tmp_path: Path) -> None:
        """The sweep's brake: a spawn between mkdir and registration is not dead."""
        fresh = _residue_dir(tmp_path, "cron_aaaa_bbbb")
        assert session_work_dir.reclaim_session_work_dir(fresh, min_age_secs=3600) is False
        assert fresh.exists()
        old = _residue_dir(tmp_path, "cron_cccc_dddd", age_secs=7200)
        assert session_work_dir.reclaim_session_work_dir(old, min_age_secs=3600) is True

    def test_a_fresh_overlay_write_renews_an_old_directory(self, tmp_path: Path) -> None:
        """Age is the NEWEST mtime in the tree: a live run rewriting cli.json is young."""
        work_dir = _residue_dir(tmp_path, "subagent_01234567", age_secs=7200)
        (work_dir / ".kiro" / "settings" / "cli.json").write_text("{}", encoding="utf-8")
        assert session_work_dir.reclaim_session_work_dir(work_dir, min_age_secs=3600) is False

    @requires_symlinks
    def test_a_linked_work_directory_is_refused(self, tmp_path: Path) -> None:
        real = _residue_dir(tmp_path, "elsewhere")
        link = tmp_path / "subagent_11111111"
        link.symlink_to(real, target_is_directory=True)
        assert session_work_dir.reclaim_session_work_dir(link) is False
        assert real.exists() and (real / ".kiro" / "settings" / "cli.json").exists()

    @requires_symlinks
    def test_a_link_inside_the_chain_is_refused(self, tmp_path: Path) -> None:
        """A run could plant ``.kiro/settings -> <somewhere>``; nothing there is unlinked."""
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "cli.json").write_text("precious", encoding="utf-8")
        work_dir = tmp_path / "subagent_22222222"
        (work_dir / ".kiro").mkdir(parents=True)
        (work_dir / ".kiro" / "settings").symlink_to(victim, target_is_directory=True)
        assert session_work_dir.reclaim_session_work_dir(work_dir) is False
        assert (victim / "cli.json").read_text(encoding="utf-8") == "precious"

    @requires_symlinks
    def test_a_linked_marker_is_not_a_marker(self, tmp_path: Path) -> None:
        work_dir = _residue_dir(tmp_path, "subagent_00000009", age_secs=7200, marked=False)
        real = tmp_path / "real-marker"
        real.write_text(_marker_text(pid=PREDECESSOR_PID), encoding="ascii")
        (work_dir / session_work_dir.RUN_DIR_MARKER).symlink_to(real)
        assert session_work_dir.reclaim_session_work_dir(work_dir, require_marker=True) is False
        assert session_work_dir.reclaim_session_work_dir(work_dir) is False
        assert work_dir.exists() and real.exists()

    def test_the_marker_is_the_only_provenance_the_sweep_accepts(self, tmp_path: Path) -> None:
        """A run-prefixed name under the workspace root is not provenance.

        Someone can make ``workspace_root()/subagent_deadbeef`` and put their own
        ``.kiro/settings/cli.json`` in it; by name and shape it is residue. No
        marker means Crew never derived it, so the sweep must leave it however
        old it is. The shutdown path's provenance is the factory flag, so it does
        not need the file.
        """
        theirs = _residue_dir(tmp_path, "subagent_deadbeef", age_secs=10**6, marked=False)
        assert session_work_dir.reclaim_session_work_dir(theirs, require_marker=True) is False
        assert (theirs / ".kiro" / "settings" / "cli.json").exists()
        assert session_work_dir.reclaim_session_work_dir(theirs) is True

    def test_shutdown_reclaim_requires_this_home_and_this_process(self, tmp_path: Path) -> None:
        """The default rule is equality on (home, pid); nothing else is probed."""
        own = _residue_dir(tmp_path, "subagent_00000001")
        predecessor = _residue_dir(tmp_path, "subagent_00000002", owner_pid=PREDECESSOR_PID)
        foreign = _residue_dir(tmp_path, "subagent_00000003", home=OTHER_HOME)
        garbled = _residue_dir(tmp_path, "subagent_00000004", raw_marker="not-a-marker")
        with unittest.mock.patch.object(
            session_work_dir.platform_compat, "pid_liveness", side_effect=AssertionError
        ):
            assert session_work_dir.reclaim_session_work_dir(own) is True
            for kept in (predecessor, foreign, garbled):
                before = _tree(kept)
                assert session_work_dir.reclaim_session_work_dir(kept) is False
                assert _tree(kept) == before
        assert not own.exists()

    def test_the_predecessor_rule_is_this_homes_ledger_and_nothing_else(
        self, tmp_path: Path
    ) -> None:
        """The sweep's rule: same home, another pid, no ledger entry left.

        The pid's liveness is never asked: a same-home pid the ledger retains
        is kept even though nothing is alive here, and one the ledger dropped
        is reclaimed even though the pid (this test's parent) is alive.
        """
        live_pid = os.getppid()
        assert live_pid != os.getpid()
        rule = session_work_dir.predecessor_marker_rule(frozenset({PREDECESSOR_PID}))
        dropped = _residue_dir(tmp_path, "subagent_00000001", owner_pid=live_pid, age_secs=7200)
        retained = _residue_dir(tmp_path, "subagent_00000002", owner_pid=PREDECESSOR_PID)
        own = _residue_dir(tmp_path, "subagent_00000003")
        foreign = _residue_dir(tmp_path, "subagent_00000004", home=OTHER_HOME, owner_pid=1)
        with unittest.mock.patch.object(
            session_work_dir.platform_compat, "pid_liveness", side_effect=AssertionError
        ):
            assert (
                session_work_dir.reclaim_session_work_dir(
                    dropped, require_marker=True, marker_permits=rule
                )
                is True
            )
            for kept in (retained, own, foreign):
                assert (
                    session_work_dir.reclaim_session_work_dir(
                        kept, require_marker=True, marker_permits=rule
                    )
                    is False
                )
        assert not dropped.exists()
        assert all(k.exists() for k in (retained, own, foreign))

    def test_a_marked_directory_is_reclaimed_marker_included(self, tmp_path: Path) -> None:
        marked = _residue_dir(tmp_path, "subagent_0badf00d", age_secs=7200)
        assert session_work_dir.reclaim_session_work_dir(marked, require_marker=True) is True
        assert not marked.exists()

    def test_a_marked_directory_that_gained_a_file_is_kept(self, tmp_path: Path) -> None:
        marked = _residue_dir(tmp_path, "subagent_0badf00d", age_secs=7200)
        (marked / "notes.txt").write_text("mine", encoding="utf-8")
        assert session_work_dir.reclaim_session_work_dir(marked, require_marker=True) is False
        assert (marked / "notes.txt").exists() and (
            marked / session_work_dir.RUN_DIR_MARKER
        ).exists()

    def test_counting_what_the_sweep_cannot_reclaim_for_the_doctor(self, tmp_path: Path) -> None:
        """Two figures, judged by the sweep's own rule over the ledger it is given.

        Unmarked directories are one figure. The other is every marked directory
        this home cannot act on: another home's, an unreadable marker, a same-home
        gateway the ledger still retains. A same-home predecessor the ledger has
        dropped is the sweep's to reclaim and is not counted.
        """
        _residue_dir(tmp_path, "subagent_0000000000000001", marked=False)
        _residue_dir(tmp_path, "cron_aaaaaaaa_bbbbbbbb", marked=False)
        _residue_dir(tmp_path, "subagent_notes", marked=False)
        (tmp_path / "subagent_0000000000000003").write_text("a file, not a dir", encoding="utf-8")
        _residue_dir(tmp_path, "subagent_00000011", home=OTHER_HOME)
        _residue_dir(tmp_path, "subagent_00000012", raw_marker="garbled")
        _residue_dir(tmp_path, "subagent_00000013", owner_pid=PREDECESSOR_PID)
        _residue_dir(tmp_path, "subagent_00000014", owner_pid=PREDECESSOR_PID + 1)
        census = session_work_dir.count_run_dirs(
            tmp_path, retained_gateway_pids=frozenset({PREDECESSOR_PID})
        )
        assert census == session_work_dir.RunDirCensus(unmarked=2, refused=3, floor=False)
        capped = session_work_dir.count_run_dirs(
            tmp_path, retained_gateway_pids=frozenset(), max_entries=1
        )
        assert capped.floor is True and capped.unmarked + capped.refused <= 1
        assert session_work_dir.count_run_dirs(tmp_path / "gone", retained_gateway_pids=()) == (
            session_work_dir.RunDirCensus()
        )
        assert all(p.exists() for p in tmp_path.iterdir()), "the census removed something"


class TestByNameForm:
    """The platform form without descriptor-relative opens (Windows), driven here.

    Same rule, same refusals as the pinned walk; these tests drive it directly
    on a POSIX host so the contract is pinned on every platform the suite runs.
    """

    @pytest.fixture(autouse=True)
    def _unpinned(self, monkeypatch):
        monkeypatch.setattr(session_work_dir.pinned_fs, "supports_pinned_walk", lambda: False)

    def test_marked_residue_is_removed_marker_included(self, tmp_path: Path) -> None:
        own = _residue_dir(tmp_path, "subagent_0000000a", age_secs=7200)
        assert session_work_dir.reclaim_session_work_dir(own, require_marker=True) is True
        assert not own.exists()
        predecessor = _residue_dir(
            tmp_path, "subagent_0000000b", age_secs=7200, owner_pid=PREDECESSOR_PID
        )
        rule = session_work_dir.predecessor_marker_rule(frozenset())
        assert session_work_dir.reclaim_session_work_dir(predecessor) is False
        assert (
            session_work_dir.reclaim_session_work_dir(
                predecessor, require_marker=True, marker_permits=rule
            )
            is True
        )
        assert not predecessor.exists()

    def test_another_homes_marker_and_a_retained_pid_are_kept(self, tmp_path: Path) -> None:
        foreign = _residue_dir(tmp_path, "subagent_00000017", age_secs=7200, home=OTHER_HOME)
        retained = _residue_dir(
            tmp_path, "subagent_00000018", age_secs=7200, owner_pid=PREDECESSOR_PID
        )
        rule = session_work_dir.predecessor_marker_rule({PREDECESSOR_PID})
        for kept in (foreign, retained):
            before = _tree(kept)
            assert session_work_dir.reclaim_session_work_dir(kept, marker_permits=rule) is False
            assert _tree(kept) == before

    def test_unmarked_residue_is_kept_when_the_marker_is_required(self, tmp_path: Path) -> None:
        work_dir = _residue_dir(tmp_path, "subagent_0000000b", age_secs=7200, marked=False)
        assert session_work_dir.reclaim_session_work_dir(work_dir, require_marker=True) is False
        assert (work_dir / ".kiro" / "settings" / "cli.json").exists()
        assert session_work_dir.reclaim_session_work_dir(work_dir) is True

    @pytest.mark.parametrize(
        "plant",
        [
            lambda d: (d / "report.md").write_text("output", encoding="utf-8"),
            lambda d: (d / ".kiro" / "steering").mkdir(),
            lambda d: (d / ".kiro" / "settings" / "mcp.json").write_text("{}", encoding="utf-8"),
        ],
    )
    def test_anything_that_is_not_residue_keeps_the_directory(self, tmp_path: Path, plant):
        work_dir = _residue_dir(tmp_path, "subagent_0000000c", age_secs=7200)
        plant(work_dir)
        before = _tree(work_dir)
        assert session_work_dir.reclaim_session_work_dir(work_dir) is False
        assert _tree(work_dir) == before

    def test_emptied_prefixes_missing_and_young_directories(self, tmp_path: Path) -> None:
        bare = tmp_path / "subagent_0000000d"
        bare.mkdir()
        assert session_work_dir.reclaim_session_work_dir(bare) is True
        (tmp_path / "subagent_0000000e" / ".kiro").mkdir(parents=True)
        assert session_work_dir.reclaim_session_work_dir(tmp_path / "subagent_0000000e") is True
        assert session_work_dir.reclaim_session_work_dir(tmp_path / "gone") is False
        (tmp_path / "a-file").write_text("x", encoding="utf-8")
        assert session_work_dir.reclaim_session_work_dir(tmp_path / "a-file") is False
        young = _residue_dir(tmp_path, "subagent_0000000f")
        assert session_work_dir.reclaim_session_work_dir(young, min_age_secs=3600) is False

    @requires_symlinks
    def test_links_are_refused_at_every_level(self, tmp_path: Path) -> None:
        real = _residue_dir(tmp_path, "elsewhere", age_secs=7200)
        link = tmp_path / "subagent_00000010"
        link.symlink_to(real, target_is_directory=True)
        assert session_work_dir.reclaim_session_work_dir(link) is False
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "cli.json").write_text("precious", encoding="utf-8")
        work_dir = tmp_path / "subagent_00000011"
        (work_dir / ".kiro").mkdir(parents=True)
        (work_dir / ".kiro" / "settings").symlink_to(victim, target_is_directory=True)
        assert session_work_dir.reclaim_session_work_dir(work_dir) is False
        assert (victim / "cli.json").read_text(encoding="utf-8") == "precious"
        linked_marker = _residue_dir(tmp_path, "subagent_00000012", age_secs=7200, marked=False)
        (tmp_path / "real-marker").write_text(_marker_text(), encoding="ascii")
        (linked_marker / session_work_dir.RUN_DIR_MARKER).symlink_to(tmp_path / "real-marker")
        assert (
            session_work_dir.reclaim_session_work_dir(linked_marker, require_marker=True) is False
        )
        assert session_work_dir._read_run_dir_marker(linked_marker) is None

    def test_mark_run_dir_by_name_creates_replaces_a_predecessor_and_keeps_other_homes(
        self, tmp_path: Path
    ) -> None:
        work_dir = tmp_path / "subagent_00000013"
        assert session_work_dir.mark_run_dir(work_dir) is True
        marker = work_dir / session_work_dir.RUN_DIR_MARKER
        assert marker.read_text(encoding="ascii") == _marker_text()
        assert session_work_dir.mark_run_dir(work_dir) is True
        assert session_work_dir.reclaim_session_work_dir(work_dir) is True
        predecessor = _residue_dir(tmp_path, "subagent_00000014", owner_pid=PREDECESSOR_PID)
        assert session_work_dir.mark_run_dir(predecessor) is True
        assert (predecessor / session_work_dir.RUN_DIR_MARKER).read_text(
            encoding="ascii"
        ) == _marker_text()
        foreign = _residue_dir(tmp_path, "subagent_00000015", home=OTHER_HOME)
        assert session_work_dir.mark_run_dir(foreign) is False
        assert (foreign / session_work_dir.RUN_DIR_MARKER).read_text(
            encoding="ascii"
        ) == _marker_text(home=OTHER_HOME)
        garbled = _residue_dir(tmp_path, "subagent_00000016", raw_marker="garbled")
        assert session_work_dir.mark_run_dir(garbled) is False

    def test_sweep_by_name_keeps_another_homes_directory(self, tmp_path: Path) -> None:
        work_dir = _residue_dir(
            tmp_path, "subagent_0000000000000013", age_secs=7200, home=OTHER_HOME
        )
        assert (
            session_work_dir.sweep_predecessor_work_dirs(
                tmp_path, retained_gateway_pids=frozenset(), live_work_dirs=[]
            )
            == 0
        )
        assert work_dir.exists()

    @requires_symlinks
    def test_mark_run_dir_by_name_refuses_links(self, tmp_path: Path) -> None:
        real = tmp_path / "elsewhere"
        real.mkdir()
        link = tmp_path / "subagent_00000014"
        link.symlink_to(real, target_is_directory=True)
        assert session_work_dir.mark_run_dir(link) is False
        work_dir = tmp_path / "subagent_00000015"
        work_dir.mkdir()
        (tmp_path / "real-marker").write_bytes(b"")
        (work_dir / session_work_dir.RUN_DIR_MARKER).symlink_to(tmp_path / "real-marker")
        assert session_work_dir.mark_run_dir(work_dir) is False


def _sweep(root: Path, *, retained=frozenset(), live=(), **kwargs) -> int:
    return session_work_dir.sweep_predecessor_work_dirs(
        root, retained_gateway_pids=retained, live_work_dirs=list(live), **kwargs
    )


class TestSweepBounds:
    def test_a_root_too_large_to_count_inside_the_budget_sweeps_nothing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        for n in range(3):
            _residue_dir(tmp_path, f"subagent_{n:016x}", age_secs=7200, owner_pid=PREDECESSOR_PID)
        ticks = iter([0.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
        monkeypatch.setattr(session_work_dir.time, "monotonic", lambda: next(ticks, 10.0))
        assert _sweep(tmp_path, max_seconds=1.0) == 0
        assert all((tmp_path / f"subagent_{n:016x}").exists() for n in range(3))

    def test_a_linked_root_and_an_unlistable_root_sweep_nothing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(session_work_dir.platform_compat, "is_link_or_junction", lambda p: True)
        assert _sweep(tmp_path) == 0
        monkeypatch.setattr(
            session_work_dir.platform_compat, "is_link_or_junction", lambda p: False
        )
        assert _sweep(tmp_path / "gone") == 0
        assert session_work_dir.count_run_dirs(tmp_path / "gone", retained_gateway_pids=()) == (
            session_work_dir.RunDirCensus()
        )


class TestSweep:
    def test_reclaims_a_dead_predecessors_idle_residue_and_nothing_else(
        self, tmp_path: Path
    ) -> None:
        idle = _residue_dir(
            tmp_path, "subagent_0000000000000001", age_secs=7200, owner_pid=PREDECESSOR_PID
        )
        idle_cron = _residue_dir(
            tmp_path, "cron_11112222_33334444", age_secs=7200, owner_pid=PREDECESSOR_PID
        )
        persistent_cron = _residue_dir(
            tmp_path, "cron_11112222", age_secs=7200, owner_pid=PREDECESSOR_PID
        )
        live = _residue_dir(
            tmp_path, "subagent_0000000000000002", age_secs=7200, owner_pid=PREDECESSOR_PID
        )
        young = _residue_dir(tmp_path, "subagent_0000000000000003", owner_pid=PREDECESSOR_PID)
        own = _residue_dir(tmp_path, "subagent_0000000000000005", age_secs=7200)
        foreign = _residue_dir(
            tmp_path, "subagent_0000000000000006", age_secs=7200, home=OTHER_HOME
        )
        retained = _residue_dir(
            tmp_path, "subagent_0000000000000007", age_secs=7200, owner_pid=PREDECESSOR_PID + 1
        )
        garbled = _residue_dir(
            tmp_path, "subagent_0000000000000008", age_secs=7200, raw_marker="garbled"
        )
        dashboard = _residue_dir(tmp_path, "dashboard_slot-1", age_secs=7200)
        data = _residue_dir(
            tmp_path, "subagent_0000000000000004", age_secs=7200, owner_pid=PREDECESSOR_PID
        )
        (data / "out.txt").write_text("kept", encoding="utf-8")
        legacy = _residue_dir(tmp_path, "subagent_000000000000abcd", age_secs=10**6, marked=False)
        theirs = _residue_dir(tmp_path, "subagent_project", age_secs=10**6, marked=False)

        with unittest.mock.patch.object(
            session_work_dir.platform_compat, "pid_liveness", side_effect=AssertionError
        ):
            reclaimed = _sweep(tmp_path, retained={PREDECESSOR_PID + 1}, live=[str(live)])

        assert reclaimed == 2
        assert not idle.exists() and not idle_cron.exists()
        assert persistent_cron.exists(), "a persistent cron directory was swept"
        assert live.exists(), "a directory a registered session names was swept"
        assert young.exists(), "a directory inside the grace window was swept"
        assert own.exists(), "a directory this process marked is not the sweep's business"
        assert foreign.exists(), "another data home's directory was swept"
        assert retained.exists(), "a gateway the ledger still retains was swept"
        assert garbled.exists(), "an unreadable marker authorized a sweep"
        assert dashboard.exists(), "a long-lived session's directory was swept"
        assert (data / "out.txt").exists()
        assert legacy.exists(), "an unmarked directory from an older build was swept"
        assert (
            theirs / ".kiro" / "settings" / "cli.json"
        ).exists(), "a person's own directory under a run prefix was swept"

    def test_the_sweep_is_bounded_and_finishes_on_later_wakes(self, tmp_path: Path) -> None:
        dirs = [
            _residue_dir(tmp_path, f"subagent_{n:016x}", age_secs=7200, owner_pid=PREDECESSOR_PID)
            for n in range(10)
        ]
        first = _sweep(tmp_path, max_entries=4)
        assert first == 4
        assert sum(d.exists() for d in dirs) == 6
        total = first
        for _ in range(3):
            total += _sweep(tmp_path, max_entries=4)
        assert total == 10 and not any(d.exists() for d in dirs)

    def test_a_missing_root_sweeps_nothing(self, tmp_path: Path) -> None:
        assert _sweep(tmp_path / "no") == 0


class TestRetainedGatewayPids:
    """The one read the sweep makes of the pid ledger; the file is never changed."""

    @pytest.fixture
    def ledger(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "home" / "kiro_session_pids.txt"
        path.parent.mkdir()
        monkeypatch.setattr(session_pid, "_session_pid_file_path", lambda: path)
        return path

    def test_every_gateway_with_an_entry_is_retained(self, ledger: Path) -> None:
        ledger.write_text(
            "111:222:tok\n111:223\n333:444:tok\n\nnot-a-line\n555\n0:9:tok\nx:1:tok\n",
            encoding="utf-8",
        )
        before = ledger.read_bytes()
        assert session_pid.retained_gateway_pids() == frozenset({111, 333})
        assert ledger.read_bytes() == before

    def test_a_missing_ledger_retains_nothing(self, ledger: Path) -> None:
        assert session_pid.retained_gateway_pids() == frozenset()

    def test_an_unreadable_ledger_raises_rather_than_answering_nothing(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ledger.write_text("111:222\n", encoding="utf-8")
        monkeypatch.setattr(
            Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(OSError("io"))
        )
        with pytest.raises(OSError):
            session_pid.retained_gateway_pids()


class TestGatewayWiring:
    """The gateway sweeps predecessors at boot, after the ledger reap, before writers."""

    def test_the_boot_sweep_follows_the_ledger_reap_and_precedes_every_session_writer(
        self,
    ) -> None:
        from kiro_crew.slack import gateway

        source = Path(gateway.__file__).read_text(encoding="utf-8")
        reap = source.index(
            "await asyncio.to_thread(cleanup_orphaned_sessions, narrow_with_leaders=False)"
        )
        ready = source.index('print(f"KIROCREW_READY:{json.dumps(ready_payload)}", flush=True)')
        sweep = source.index("_sweep_predecessor_session_work_dirs,\n", reap)
        writers = source.index("await self._ensure_subagent_coordinator()", ready)
        hourly = source.index("_sweep_predecessor_session_work_dirs,\n", sweep + 1)
        assert reap < ready < sweep < writers, (
            "the boot sweep must run after cleanup_orphaned_sessions reaped the ledger, "
            "past KIROCREW_READY (no-new-work-on-gateway-boot-path), and before the "
            "first session writer starts"
        )
        assert (
            "_run_agent_scratch_sweep" in source[writers:hourly]
        ), "the hourly wake keeps calling the same predecessor-only sweep"
        assert "sweep_disposable_work_dirs" not in source

    def test_the_wrapper_hands_the_sweep_this_homes_ledger_and_live_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.slack import gateway

        seen: dict = {}

        def fake_sweep(root, **kwargs):
            seen["root"] = root
            seen.update(kwargs)
            return 3

        monkeypatch.setattr(gateway.session_work_dir, "sweep_predecessor_work_dirs", fake_sweep)
        monkeypatch.setattr(gateway, "workspace_root", lambda: tmp_path)
        monkeypatch.setattr(session_pid, "retained_gateway_pids", lambda: frozenset({7}))
        assert gateway._sweep_predecessor_session_work_dirs(["/a/b"]) == 3
        assert seen == {
            "root": tmp_path,
            "retained_gateway_pids": frozenset({7}),
            "live_work_dirs": ["/a/b"],
        }

    def test_an_unreadable_ledger_sweeps_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.slack import gateway

        monkeypatch.setattr(gateway, "workspace_root", lambda: tmp_path)
        monkeypatch.setattr(
            session_pid,
            "retained_gateway_pids",
            lambda: (_ for _ in ()).throw(OSError("ledger unreadable")),
        )
        monkeypatch.setattr(
            gateway.session_work_dir,
            "sweep_predecessor_work_dirs",
            lambda *a, **k: pytest.fail("swept on an unreadable ledger"),
        )
        with pytest.raises(OSError):
            gateway._sweep_predecessor_session_work_dirs([])

    def test_live_work_dirs_are_the_registered_providers_cwds(self) -> None:
        from kiro_crew.slack import gateway

        assert gateway._live_session_work_dirs(None) == []
        sessions = MagicMock()
        sessions.active_providers.return_value = [
            MagicMock(cwd="/w/one"),
            MagicMock(cwd=""),
            MagicMock(spec=[]),
        ]
        assert gateway._live_session_work_dirs(sessions) == ["/w/one"]


def _cfg(tmp_path: Path) -> KiroCrewConfig:
    cfg_file = tmp_path / "kirocrew.json"
    cfg_file.write_text(
        json.dumps({"agent": {"provider": "acp", "acp_backend": ACP_BACKEND_KIRO}}),
        encoding="utf-8",
    )
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
        return KiroCrewConfig.load()


class TestFactoryMarksOnlyDerivedOneRunDirectories:
    """The provider learns from the factory which directories are its to reclaim."""

    @staticmethod
    def _captured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **factory_kwargs) -> dict:
        import kiro_crew.providers.acp as acp_mod

        monkeypatch.setenv("KIROCREW_WORKSPACE", str(tmp_path / "ws"))
        seen: list[dict] = []

        class _FakeProvider:
            def __init__(self, **kwargs: object) -> None:
                seen.append(kwargs)

        with unittest.mock.patch.object(acp_mod, "AcpProvider", _FakeProvider):
            _cfg(tmp_path).create_provider_factory()(**factory_kwargs)
        return seen[0]

    @pytest.mark.parametrize("key", ["subagent:0123456789abcdef", "cron:aaaa1111:bbbb2222"])
    def test_a_derived_one_run_directory_is_marked(self, tmp_path, monkeypatch, key) -> None:
        got = self._captured(tmp_path, monkeypatch, session_key=key)
        assert got["disposable_work_dir"] is True
        assert Path(got["work_dir"]).parent == Path(os.path.realpath(tmp_path / "ws"))

    @pytest.mark.parametrize(
        "key",
        [
            "cron:aaaa1111",
            "cron:aaaa1111:myagent",
            "subagent:notes",
            "dashboard:slot-1",
            "slack:C123:456.789",
            None,
        ],
    )
    def test_a_long_lived_session_directory_is_not_marked(self, tmp_path, monkeypatch, key):
        got = self._captured(tmp_path, monkeypatch, session_key=key)
        assert got["disposable_work_dir"] is False

    def test_an_explicit_cwd_is_never_marked_whatever_the_key(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        project.mkdir()
        got = self._captured(
            tmp_path, monkeypatch, session_key="subagent:0123abcd", cwd=str(project)
        )
        assert got["disposable_work_dir"] is False
        assert Path(got["work_dir"]) == project


class TestProviderReclaimsAtShutdown:
    @staticmethod
    def _provider(work_dir: Path, *, disposable: bool, claimed: bool = True):
        from kiro_crew.providers.acp import AcpProvider

        with unittest.mock.patch("kiro_crew.providers.acp.AcpClient"):
            provider = AcpProvider(acp_backend=ACP_BACKEND_KIRO, disposable_work_dir=disposable)
        provider._client = MagicMock()
        provider._client.backend = ACP_BACKEND_KIRO
        provider._client._work_dir = work_dir
        provider._client.session_id = None
        provider._client.process_tree_confirmed_dead = True
        provider._client.shutdown = AsyncMock()
        if claimed:

            @asynccontextmanager
            async def claim():
                yield True

            provider.set_work_dir_claim_probe(claim)
        return provider

    @pytest.mark.asyncio
    async def test_a_run_directory_disappears_when_its_run_ends(self, tmp_path: Path) -> None:
        work_dir = _residue_dir(tmp_path, "subagent_deadbeef")
        provider = self._provider(work_dir, disposable=True)
        await provider.shutdown()
        provider._client.shutdown.assert_awaited_once()
        assert not work_dir.exists()

    @pytest.mark.asyncio
    async def test_a_surviving_process_tree_keeps_the_run_directory(self, tmp_path: Path) -> None:
        work_dir = _residue_dir(tmp_path, "subagent_deadbeef")
        provider = self._provider(work_dir, disposable=True)
        provider._client.process_tree_confirmed_dead = False
        await provider.shutdown()
        assert work_dir.exists()

    @pytest.mark.asyncio
    async def test_an_unknown_process_tree_keeps_the_run_directory(self, tmp_path: Path) -> None:
        work_dir = _residue_dir(tmp_path, "subagent_deadbeef")
        provider = self._provider(work_dir, disposable=True)
        provider._client.process_tree_confirmed_dead = None
        await provider.shutdown()
        assert work_dir.exists()

    @pytest.mark.asyncio
    async def test_a_marker_that_is_not_this_home_and_process_keeps_the_directory(
        self, tmp_path: Path
    ) -> None:
        """Shutdown's provenance is the flag plus (home, pid) equality; no probe."""
        predecessor = _residue_dir(tmp_path, "subagent_00000001", owner_pid=PREDECESSOR_PID)
        foreign = _residue_dir(tmp_path, "subagent_00000002", home=OTHER_HOME)
        with unittest.mock.patch.object(
            session_work_dir.platform_compat, "pid_liveness", side_effect=AssertionError
        ):
            for work_dir in (predecessor, foreign):
                provider = self._provider(work_dir, disposable=True)
                await provider.shutdown()
                assert (work_dir / session_work_dir.RUN_DIR_MARKER).exists()

    @pytest.mark.asyncio
    async def test_a_provider_without_a_registry_claim_keeps_the_run_directory(
        self, tmp_path: Path
    ) -> None:
        work_dir = _residue_dir(tmp_path, "subagent_deadbeef")
        provider = self._provider(work_dir, disposable=True, claimed=False)
        await provider.shutdown()
        assert work_dir.exists()

    @pytest.mark.asyncio
    async def test_a_broken_registry_claim_keeps_the_run_directory(self, tmp_path: Path) -> None:
        work_dir = _residue_dir(tmp_path, "subagent_deadbeef")
        provider = self._provider(work_dir, disposable=True, claimed=False)

        @asynccontextmanager
        async def broken_claim():
            raise RuntimeError("registry unavailable")
            yield True  # pragma: no cover - makes this an async context manager

        provider.set_work_dir_claim_probe(broken_claim)
        await provider.shutdown()
        assert work_dir.exists()

    @pytest.mark.asyncio
    async def test_a_run_that_wrote_a_file_keeps_its_directory(self, tmp_path: Path) -> None:
        work_dir = _residue_dir(tmp_path, "subagent_deadbeef")
        (work_dir / "result.json").write_text("{}", encoding="utf-8")
        provider = self._provider(work_dir, disposable=True)
        await provider.shutdown()
        assert (work_dir / "result.json").exists()

    @pytest.mark.asyncio
    async def test_an_unmarked_directory_is_untouched(self, tmp_path: Path) -> None:
        work_dir = _residue_dir(tmp_path, "dashboard_slot-1")
        provider = self._provider(work_dir, disposable=False)
        await provider.shutdown()
        assert (work_dir / ".kiro" / "settings" / "cli.json").exists()

    @pytest.mark.asyncio
    async def test_start_marks_a_derived_directory_before_any_writer(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The marker is written on start, so a crash after it leaves a sweepable dir."""
        work_dir = tmp_path / "subagent_deadbeef"
        provider = self._provider(work_dir, disposable=True)
        provider._client.ensure_ready = AsyncMock()
        provider._client.memory_mode = "persistent"
        monkeypatch.setattr(provider, "_apply_effort_overlay", lambda: None)
        monkeypatch.setattr(provider, "_apply_tool_search_overlay", lambda: None)
        monkeypatch.setattr(type(provider), "is_acp_runtime_backend", property(lambda self: False))
        monkeypatch.setattr(provider, "_apply_initial_effort", AsyncMock())
        await provider.start()
        provider._client.ensure_ready.assert_awaited_once()
        marker = work_dir / session_work_dir.RUN_DIR_MARKER
        assert marker.read_text(encoding="ascii").splitlines() == [
            session_work_dir.data_home_id(),
            str(os.getpid()),
        ]
        # A caller's cwd is never marked.
        project = tmp_path / "project"
        project.mkdir()
        other = self._provider(project, disposable=False)
        other._client.ensure_ready = AsyncMock()
        other._client.memory_mode = "persistent"
        monkeypatch.setattr(other, "_apply_effort_overlay", lambda: None)
        monkeypatch.setattr(other, "_apply_tool_search_overlay", lambda: None)
        monkeypatch.setattr(other, "_apply_initial_effort", AsyncMock())
        await other.start()
        assert not (project / session_work_dir.RUN_DIR_MARKER).exists()

    @pytest.mark.asyncio
    async def test_a_reclaim_failure_never_fails_the_shutdown(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        work_dir = _residue_dir(tmp_path, "subagent_deadbeef")
        provider = self._provider(work_dir, disposable=True)

        def boom(*args, **kwargs):
            raise RuntimeError("filesystem said no")

        import kiro_crew.providers.acp as acp_mod

        monkeypatch.setattr(acp_mod, "reclaim_session_work_dir", boom)
        await provider.shutdown()
        assert work_dir.exists()

    @pytest.mark.asyncio
    async def test_cancellation_waits_for_reclaim_before_releasing_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work_dir = _residue_dir(tmp_path, "subagent_deadbeef")
        provider = self._provider(work_dir, disposable=True, claimed=False)
        worker_entered = threading.Event()
        release_worker = threading.Event()
        claim_exited = asyncio.Event()
        events: list[str] = []

        @asynccontextmanager
        async def claim():
            events.append("claim-enter")
            try:
                yield True
            finally:
                events.append("claim-exit")
                claim_exited.set()

        def blocked_reclaim(candidate: Path) -> bool:
            assert candidate == work_dir
            events.append("worker-enter")
            worker_entered.set()
            if not release_worker.wait(timeout=3.0):
                raise TimeoutError("test did not release reclaim worker")
            events.append("worker-return")
            return True

        import kiro_crew.providers.acp as acp_mod

        provider.set_work_dir_claim_probe(claim)
        monkeypatch.setattr(acp_mod, "reclaim_session_work_dir", blocked_reclaim)
        task = asyncio.create_task(provider._reclaim_work_dir())
        try:
            assert await asyncio.to_thread(worker_entered.wait, 3.0)
            assert task.cancel()
            await asyncio.sleep(0)
            assert not claim_exited.is_set(), "cancellation released the claim before reclaim ended"
        finally:
            release_worker.set()

        (result,) = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result, asyncio.CancelledError)
        assert events == ["claim-enter", "worker-enter", "worker-return", "claim-exit"]


class TestRegistryLeavesASiblingsDirectory:
    """Two providers for one KEY derive one directory; the discarded one leaves it.

    The factory flag says "derived from a one-run key", not "this instance is
    the one the registry kept". Where the registry shuts down a provider whose
    key another live provider holds -- the loser of a cold-start race, the
    predecessor of a recycle whose successor is already registered, the
    bootstrap provider that borrowed a parent's key -- it first tells that
    provider the directory is not its to reclaim. The live sibling still
    reclaims at its own shutdown, which is what the ``close_all`` at the end
    of each test pins.
    """

    KEY = "subagent:0123abcd"

    @staticmethod
    def _factory(work_dir: Path, made: list, gate: "asyncio.Event | None" = None):
        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            provider = TestProviderReclaimsAtShutdown._provider(work_dir, disposable=True)
            provider._client._session_id = ""
            provider._client._pid = None
            provider._client._runtime = None

            async def _start() -> None:
                if gate is not None:
                    await gate.wait()

            provider.start = _start  # type: ignore[method-assign]
            provider.is_process_alive = lambda: True  # type: ignore[method-assign]
            provider.is_alive = lambda: True  # type: ignore[method-assign]
            provider.context_usage_pct = lambda: 0.0  # type: ignore[method-assign]
            provider.context_window_tokens = lambda: 0  # type: ignore[method-assign]
            provider.has_active_turn = lambda: False  # type: ignore[method-assign]
            provider.runtime_info = lambda: (None, None)  # type: ignore[method-assign]
            made.append(provider)
            return provider

        return factory

    @pytest.mark.asyncio
    async def test_the_loser_of_a_cold_start_race_leaves_the_winners_directory(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.session import SessionManager

        work_dir = _residue_dir(tmp_path, "subagent_0123abcd")
        made: list = []
        gate = asyncio.Event()
        mgr = SessionManager(_cfg(tmp_path), provider_factory=self._factory(work_dir, made, gate))
        first = asyncio.create_task(mgr.get_or_create(self.KEY))
        second = asyncio.create_task(mgr.get_or_create(self.KEY))
        await asyncio.sleep(0.05)
        gate.set()
        done, pending = await asyncio.wait({first, second}, timeout=3.0)
        assert len(done) == 1 and len(pending) == 1, "exactly one cold start wins the key"
        await asyncio.sleep(0.05)

        winner = mgr._sessions[mgr._fold_key(self.KEY)].provider
        (loser,) = [p for p in made if p is not winner]
        loser._client.shutdown.assert_awaited_once()
        assert work_dir.exists(), "the discarded provider reclaimed the live sibling's cwd"
        assert loser._disposable_work_dir is False
        assert winner._disposable_work_dir is True

        mgr.release(self.KEY)
        await asyncio.wait_for(next(iter(pending)), timeout=3.0)
        mgr.release(self.KEY)
        await mgr.close_all()
        assert not work_dir.exists(), "the winner still owned the directory at its shutdown"

    @pytest.mark.asyncio
    async def test_a_recycled_session_leaves_its_registered_successors_directory(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.session import SessionManager

        work_dir = _residue_dir(tmp_path, "subagent_0123abcd")
        made: list = []
        mgr = SessionManager(_cfg(tmp_path), provider_factory=self._factory(work_dir, made))
        key = mgr._fold_key(self.KEY)
        await mgr.get_or_create(self.KEY)
        predecessor = mgr._sessions[key]
        mgr.release(self.KEY)
        # A successor registers while the predecessor is being recycled: the
        # marker is what lets allocation treat the key as free.
        mgr._recycling[key] = predecessor
        await mgr.get_or_create(self.KEY)
        mgr.release(self.KEY)
        mgr._recycling.pop(key, None)
        successor = mgr._sessions[key]
        assert successor is not predecessor

        await mgr._recycle_held(key, predecessor, 95.0)

        predecessor.provider._client.shutdown.assert_awaited_once()
        assert mgr._sessions[key] is successor
        assert work_dir.exists(), "the recycled predecessor reclaimed its successor's cwd"
        assert predecessor.provider._disposable_work_dir is False
        assert successor.provider._disposable_work_dir is True
        await mgr.close_all()
        assert not work_dir.exists()

    @pytest.mark.asyncio
    async def test_a_successor_registering_during_shutdown_keeps_its_directory(
        self, tmp_path: Path
    ) -> None:
        """The final registry claim spans process teardown and filesystem reclaim."""
        from kiro_crew.session import SessionManager

        work_dir = _residue_dir(tmp_path, "subagent_0123abcd")
        made: list = []
        mgr = SessionManager(_cfg(tmp_path), provider_factory=self._factory(work_dir, made))
        key = mgr._fold_key(self.KEY)
        await mgr.get_or_create(self.KEY)
        predecessor = mgr._sessions[key]
        mgr.release(self.KEY)

        async def register_successor() -> None:
            await mgr.get_or_create(self.KEY)
            mgr.release(self.KEY)

        predecessor.provider._client.shutdown = AsyncMock(side_effect=register_successor)
        await mgr._recycle_held(key, predecessor, 95.0)

        successor = mgr._sessions[key]
        assert successor is not predecessor
        assert predecessor.provider._disposable_work_dir is True
        assert work_dir.exists(), "the predecessor reclaimed a successor registered mid-shutdown"
        await mgr.close_all()
        assert not work_dir.exists()

    @pytest.mark.asyncio
    async def test_a_recycle_with_no_successor_still_reclaims(self, tmp_path: Path) -> None:
        """The disown is keyed on a successor being registered, not on recycling."""
        from kiro_crew.session import SessionManager

        work_dir = _residue_dir(tmp_path, "subagent_0123abcd")
        made: list = []
        mgr = SessionManager(_cfg(tmp_path), provider_factory=self._factory(work_dir, made))
        key = mgr._fold_key(self.KEY)
        await mgr.get_or_create(self.KEY)
        session = mgr._sessions[key]
        await mgr._recycle_held(key, session, 95.0)
        assert key not in mgr._sessions
        assert not work_dir.exists()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_runtime_bootstrap_provider_leaves_the_parents_directory(
        self, tmp_path: Path
    ) -> None:
        """A bootstrap provider borrows the PARENT's key, so it derives the parent's cwd."""
        from kiro_crew.session import SessionManager

        work_dir = _residue_dir(tmp_path, "subagent_0123abcd")
        made: list = []
        mgr = SessionManager(_cfg(tmp_path), provider_factory=self._factory(work_dir, made))
        adopted = MagicMock()
        mgr.get_subagent_runtime = AsyncMock(return_value=adopted)  # type: ignore[method-assign]

        # ``_runtime`` is None on the bootstrap provider's client, so no runtime
        # is adopted and the provider is shut down instead.
        assert await mgr._get_or_bootstrap_run_runtime(self.KEY, agent="kirocrew") is adopted

        (bootstrap,) = made
        bootstrap._client.shutdown.assert_awaited_once()
        assert bootstrap._disposable_work_dir is False
        assert work_dir.exists(), "the bootstrap provider reclaimed the parent's cwd"
