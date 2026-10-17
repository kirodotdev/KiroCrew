"""Tests for PID tracking and orphan reclaim in session_pid.py.

This file holds the tracking-file API (track / untrack, identity-bound
retirement), the candidate scan of the untracked orphan-MCP sweep, and the
reclaim's recognition of every harness Crew spawns.

The rest of the suite lives beside it in ``test_pid_lifecycle_*.py``, one file per
behaviour, sharing fixtures through ``pid_lifecycle_helpers``.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from pid_lifecycle_helpers import (  # noqa: F401 -- fixtures are requested by name
    _launcher_cmdline,
    pid_file_fixture,
    session_pid_file_fixture,
)

from kiro_crew import platform_compat


class TestTrackUntrack:
    def test_track_pid_creates_file(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_pid

        _track_pid(12345)
        assert "12345" in pid_file.read_text(encoding="utf-8")

    def test_track_multiple(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_pid

        _track_pid(111)
        _track_pid(222)
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == ["111", "222"]

    def test_untrack_pid(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_pid, _untrack_pid

        _track_pid(111)
        _track_pid(222)
        _untrack_pid(111)
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == ["222"]

    def test_untrack_nonexistent(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_pid, _untrack_pid

        _track_pid(111)
        _untrack_pid(999)  # should not crash
        assert "111" in pid_file.read_text(encoding="utf-8")

    @pytest.mark.parametrize("token", [None, "tok123"])
    def test_untrack_session_pid(
        self,
        session_pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        token: str | None,
    ) -> None:
        """Untrack removes only the named PID's entry, in either record format.

        Pin the start token instead of inheriting it from the host: PIDs 111
        and 222 are live kernel threads on some machines (so _track writes the
        3-field ``gw:pid:token``) and absent on others (2-field ``gw:pid``),
        which would otherwise make the expected value host-dependent.
        """
        from kiro_crew.session_pid import _track_session_pid, _untrack_session_pid

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: token)
        _track_session_pid(111)
        _track_session_pid(222)
        _untrack_session_pid(111)
        gw = os.getpid()
        suffix = f":{token}" if token else ""
        lines = session_pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == [f"{gw}:222{suffix}"]

    def test_untrack_session_pid_missing_file(self, session_pid_file: Path) -> None:
        from kiro_crew.session_pid import _untrack_session_pid

        _untrack_session_pid(999)  # should not crash on missing file
        assert not session_pid_file.exists()

    def test_untrack_session_pid_other_gateway_untouched(self, session_pid_file: Path) -> None:
        """Untracking our PID must NOT remove other gateways' entries for same child PID."""
        from kiro_crew.session_pid import _track_session_pid, _untrack_session_pid

        _track_session_pid(111)
        # Simulate another gateway's entry for the same child PID
        with open(session_pid_file, "a", encoding="utf-8") as f:
            f.write("99999:111\n")
        _untrack_session_pid(111)
        lines = session_pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == ["99999:111"]

    def test_track_child_pids_with_parent(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_child_pids

        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({100: None, 200: None, 300: None}, parent_pid=999)
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert set(lines) == {"100:999", "200:999", "300:999"}

    def test_track_child_pids_dedup(self, pid_file: Path) -> None:
        """Duplicate child:parent entries should not be written."""
        from kiro_crew.session_pid import _track_child_pids

        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({100: None, 200: None}, parent_pid=999)
            _track_child_pids({100: None, 300: None}, parent_pid=999)
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert sorted(lines) == ["100:999", "200:999", "300:999"]

    def test_untrack_child_pids(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_child_pids, _untrack_child_pids

        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({100: None, 200: None, 300: None}, parent_pid=999)
        _untrack_child_pids({100: None, 300: None})
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == ["200:999"]

    def test_untrack_child_pids_preserves_bare_pid(self, pid_file: Path) -> None:
        """Untracking child PIDs must not remove bare PID lines (kiro-cli parents)."""
        from kiro_crew.session_pid import _track_child_pids, _track_pid, _untrack_child_pids

        _track_pid(100)  # bare parent line
        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({100: None}, parent_pid=999)  # child line with same PID
        _untrack_child_pids({100: None})
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert "100" in lines  # bare line preserved

    # ── _untrack_root_by_identity: an observed death retires ITS lines only ──
    #
    # The runtime reader that watches a root die has no process left to re-check,
    # so the number alone cannot say whose lines these are: a replacement root can
    # be handed the same number before the write lands. The recorded start token
    # is the identity the number lacks for the session line; for the bare line,
    # which carries none, the kernel is asked under the bare file's own lock.
    #
    # Every case here names a pid the host may genuinely be running, so the
    # liveness probe is pinned explicitly: a real ``pid_exists`` answering for a
    # stranger is not the case under test.

    @staticmethod
    def _pid_is(monkeypatch: pytest.MonkeyPatch, alive: bool) -> None:
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.platform_compat, "pid_exists", lambda p: alive)

    def test_untrack_root_by_identity_retires_both_lines_for_the_recorded_token(
        self, pid_file: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dead pid, our token: both lines go, an unrelated root stays."""
        from kiro_crew.session_pid import (
            _track_pid,
            _track_session_pid,
            _untrack_root_by_identity,
        )

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok-a")
        self._pid_is(monkeypatch, alive=False)
        _track_pid(4242)
        _track_session_pid(4242)
        _track_pid(7)  # an unrelated root stays
        assert _untrack_root_by_identity(4242, "tok-a") is True
        assert session_pid_file.read_text(encoding="utf-8").strip() == ""
        assert pid_file.read_text(encoding="utf-8").strip().splitlines() == ["7"]

    def test_untrack_root_by_identity_spares_a_successor_on_the_recycled_number(
        self, pid_file: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE finding: the dead root's number was handed to a fresh root this
        gateway already tracked, and its session line carries a different token.
        A token mismatch retains everything -- the bare line is never reached."""
        from kiro_crew.session_pid import (
            _track_pid,
            _track_session_pid,
            _untrack_root_by_identity,
        )

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok-new")
        # Even a probe that says DEAD must not reach the bare line here: the
        # session line is not ours, so nothing this call owns is on disk.
        self._pid_is(monkeypatch, alive=False)
        _track_pid(4242)
        _track_session_pid(4242)
        assert _untrack_root_by_identity(4242, "tok-old") is False
        assert session_pid_file.read_text(encoding="utf-8").strip() == f"{os.getpid()}:4242:tok-new"
        assert pid_file.read_text(encoding="utf-8").strip() == "4242"

    def test_untrack_root_by_identity_keeps_the_bare_line_of_a_live_holder(
        self, pid_file: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The successor has NO session line of its own yet: its spawn has taken
        the number and appended the bare line but not reached the session write.
        The session file cannot tell the number has a new holder; the kernel can.
        Our session line is retired, the bare line the two share stays because the
        number is alive, and that is a settled result."""
        from kiro_crew.session_pid import (
            _track_pid,
            _track_session_pid,
            _untrack_root_by_identity,
        )

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok-a")
        _track_pid(4242)
        _track_session_pid(4242)
        # The successor holds 4242 now: its own _track_pid dedups against the
        # retained bare line, and its _track_session_pid found ours.
        self._pid_is(monkeypatch, alive=True)
        assert _untrack_root_by_identity(4242, "tok-a") is True
        assert session_pid_file.read_text(encoding="utf-8").strip() == ""
        assert pid_file.read_text(encoding="utf-8").strip() == "4242"

    def test_untrack_root_by_identity_probes_liveness_under_the_bare_file_lock(
        self, pid_file: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe runs INSIDE ``_pid_file_lock``, the lock ``_track_pid``
        appends under: a successor cannot be mid-append when the answer is read,
        so a live answer means its bare line is either already there or is the
        one it is about to write itself -- retaining is never wrong."""
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp, "_pid_start_token", lambda p: "tok-a")
        sp._track_pid(4242)
        sp._track_session_pid(4242)
        probed_with_lock: list[bool] = []
        lock_depth = {"n": 0}
        real_lock = sp._pid_file_lock

        @contextmanager
        def _counting_lock():  # type: ignore[no-untyped-def]
            with real_lock():
                lock_depth["n"] += 1
                try:
                    yield
                finally:
                    lock_depth["n"] -= 1

        monkeypatch.setattr(sp, "_pid_file_lock", _counting_lock)
        monkeypatch.setattr(
            sp.platform_compat,
            "pid_exists",
            lambda p: probed_with_lock.append(lock_depth["n"] == 1) or False,
        )
        assert sp._untrack_root_by_identity(4242, "tok-a") is True
        assert probed_with_lock == [True], "liveness must be read while holding the bare-file lock"
        assert pid_file.read_text(encoding="utf-8").strip() == ""

    def test_untrack_root_by_identity_reports_a_refused_bare_write_as_failure(
        self, pid_file: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refusal is not a commit. The session rewrite landing is only half the
        retirement this call owes; when the bare `kiro_pids.txt` rewrite is refused
        the dead root's line survives, so a true answer would have the caller log a
        clean retirement over a registry that still names it."""
        from kiro_crew import session_pid as session_pid_module
        from kiro_crew.session_pid import (
            _track_pid,
            _track_session_pid,
            _untrack_root_by_identity,
        )

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok-a")
        self._pid_is(monkeypatch, alive=False)
        _track_pid(4242)
        _track_session_pid(4242)
        real_rewrite = session_pid_module._rewrite_pid_file

        def refuse_the_bare_file(path: Path, text: str) -> bool:
            # The session file must still commit: the finding is specifically about
            # the SECOND write being refused after the first one landed.
            if path == pid_file:
                return False
            return real_rewrite(path, text)

        monkeypatch.setattr("kiro_crew.session_pid._rewrite_pid_file", refuse_the_bare_file)
        assert _untrack_root_by_identity(4242, "tok-a") is False
        # The half that did commit stays committed -- this is about the REPORT.
        assert session_pid_file.read_text(encoding="utf-8").strip() == ""
        assert pid_file.read_text(encoding="utf-8").strip() == "4242"

    def test_untrack_root_by_identity_reports_a_refused_session_write_as_failure(
        self, pid_file: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The FIRST write refused: the session line survives, so the bare line
        must not be touched either (the two are retired together or not at all)
        and the answer is False."""
        from kiro_crew import session_pid as session_pid_module
        from kiro_crew.session_pid import (
            _track_pid,
            _track_session_pid,
            _untrack_root_by_identity,
        )

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok-a")
        self._pid_is(monkeypatch, alive=False)
        _track_pid(4242)
        _track_session_pid(4242)
        real_rewrite = session_pid_module._rewrite_pid_file
        monkeypatch.setattr(
            "kiro_crew.session_pid._rewrite_pid_file",
            lambda path, text: False if path == session_pid_file else real_rewrite(path, text),
        )
        assert _untrack_root_by_identity(4242, "tok-a") is False
        assert session_pid_file.read_text(encoding="utf-8").strip() == f"{os.getpid()}:4242:tok-a"
        assert pid_file.read_text(encoding="utf-8").strip() == "4242"

    # ── _track_session_pid: the line written is the identity later compared ──

    def test_track_session_pid_records_the_token_it_is_handed_not_a_reprobe(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spawn reads the identity once and hands it over; what lands in the
        file is THAT token. On the plain-append path no probe is made at all, and
        on the replacement path the probe is an occupancy check whose value is
        never written (see the test below)."""
        from kiro_crew.session_pid import _track_session_pid

        def _no_probe(pid: int) -> str:
            raise AssertionError("the tracker re-probed a token it was handed")

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", _no_probe)
        _track_session_pid(4242, "tok-spawn")
        assert (
            session_pid_file.read_text(encoding="utf-8").strip() == f"{os.getpid()}:4242:tok-spawn"
        )

    def test_track_session_pid_late_tracker_does_not_replace_a_live_successor(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The token in hand was read at spawn, and this write is LATE: the root it
        belongs to has died and the number was handed to a successor that already
        wrote its own line. That line is not a stale predecessor's -- it is the
        live root's only record -- and the number names the successor, not the
        process this token belongs to. Re-reading the identity under the lock says
        so; nothing is written."""
        from kiro_crew.session_pid import _track_session_pid

        session_pid_file.write_text(f"{os.getpid()}:4242:tok-succ\n")
        # The number is the successor's now: the live identity is ITS token.
        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok-succ")
        _track_session_pid(4242, "tok-old")
        assert (
            session_pid_file.read_text(encoding="utf-8").strip() == f"{os.getpid()}:4242:tok-succ"
        )

    def test_track_session_pid_replaces_a_stale_predecessor_on_the_same_number(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The number was recycled: a line under it carries a DIFFERENT token.
        Whatever that line named has exited -- this caller holds the number now.
        Keeping the stale line is the hole identity-bound retirement fell through
        (the successor never got a line, so nothing about it was in this file);
        the line is replaced, and the file holds one line for the number."""
        from kiro_crew.session_pid import _track_session_pid

        session_pid_file.write_text(f"{os.getpid()}:4242:tok-old\n{os.getpid()}:7:tok-x\n")
        # The number still names OUR process: the occupancy re-read agrees.
        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok-new")
        _track_session_pid(4242, "tok-new")
        lines = session_pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == [f"{os.getpid()}:7:tok-x", f"{os.getpid()}:4242:tok-new"]

    def test_track_session_pid_collapses_a_stale_line_listed_before_ours(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One line per number holds whatever order the file lists them in. A
        stale predecessor line ahead of our own exact entry must not make the
        re-track return early and leave both standing."""
        from kiro_crew.session_pid import _track_session_pid

        session_pid_file.write_text(f"{os.getpid()}:4242:tok-old\n{os.getpid()}:4242:tok-new\n")
        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok-new")
        _track_session_pid(4242, "tok-new")
        lines = session_pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == [f"{os.getpid()}:4242:tok-new"]

    def test_track_session_pid_exact_retrack_is_a_no_op(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.session_pid import _track_session_pid

        _track_session_pid(4242, "tok-a")
        _track_session_pid(4242, "tok-a")
        lines = session_pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == [f"{os.getpid()}:4242:tok-a"]

    def test_track_session_pid_tokenless_write_never_downgrades_a_tokened_line(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No identity to offer proves nothing about who holds the number, so a
        token-less track leaves a line that does carry one exactly as it is."""
        from kiro_crew.session_pid import _track_session_pid

        session_pid_file.write_text(f"{os.getpid()}:4242:tok-a\n")
        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: None)
        _track_session_pid(4242)
        assert session_pid_file.read_text(encoding="utf-8").strip() == f"{os.getpid()}:4242:tok-a"

    def test_track_session_pid_raises_when_the_replacement_is_refused(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failing to record a live root is the unrecoverable direction: a root
        in neither file is unreachable by every reaper. A refused rewrite must
        surface, not read as recorded."""
        from kiro_crew.session_pid import _track_session_pid

        session_pid_file.write_text(f"{os.getpid()}:4242:tok-old\n")
        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok-new")
        monkeypatch.setattr("kiro_crew.session_pid._rewrite_pid_file", lambda path, text: False)
        with pytest.raises(OSError):
            _track_session_pid(4242, "tok-new")

    def test_untrack_root_by_identity_refuses_without_an_identity(
        self, pid_file: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No token to compare, or a line that never recorded one: nothing is
        touched -- not even a bare line whose pid the probe would call dead. The
        sweep reaps a dead entry by liveness; erasing a live one by number is the
        failure this guard exists to prevent."""
        from kiro_crew.session_pid import (
            _track_pid,
            _track_session_pid,
            _untrack_root_by_identity,
        )

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: None)
        self._pid_is(monkeypatch, alive=False)
        _track_pid(4242)
        _track_session_pid(4242)  # legacy token-less line
        # "Touches nothing" means the registries are not even opened: with no
        # identity to compare there is no line this call could own.
        monkeypatch.setattr(
            "kiro_crew.session_pid._session_pid_file_path",
            lambda: pytest.fail("no token: the session registry must not be read"),
        )
        assert _untrack_root_by_identity(4242, None) is False
        assert _untrack_root_by_identity(4242, "") is False
        monkeypatch.setattr(
            "kiro_crew.session_pid._session_pid_file_path", lambda: session_pid_file
        )
        assert _untrack_root_by_identity(4242, "tok-a") is False
        assert session_pid_file.read_text(encoding="utf-8").strip() == f"{os.getpid()}:4242"
        assert pid_file.read_text(encoding="utf-8").strip() == "4242"

    def test_replace_child_pids_rewrites_only_the_children_it_names(self, pid_file: Path) -> None:
        """A whole-set write for the caller's OWN children, nothing else."""
        from kiro_crew.session_pid import _replace_child_pids, _track_child_pids, _track_pid

        _track_pid(100)  # bare root line, another owner's business
        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({200: None, 300: None}, parent_pid=999)
            _track_child_pids({400: None}, parent_pid=888)  # a different parent

        assert (
            _replace_child_pids(
                {300: ("t300", b"x"), 500: ("t500", b"y")}, parent_pid=999, drop=(200,)
            )
            is True
        )

        lines = set(pid_file.read_text(encoding="utf-8").strip().splitlines())
        # 200 dropped, 500 arrived, and neither the bare root nor 888's child moved.
        assert lines == {"100", "300:999:t300", "500:999:t500", "400:888"}

    def test_replace_child_pids_keeps_a_child_it_was_not_given(self, pid_file: Path) -> None:
        """A root PID is reused like any other number.

        A descendant that outlived an earlier runtime holding this number is
        still tracked under it. Wiping the block by owner alone would untrack
        that survivor permanently — the leak this file exists to prevent.
        """
        from kiro_crew.session_pid import _replace_child_pids, _track_child_pids

        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({777: None}, parent_pid=999)  # an older runtime's survivor

        assert _replace_child_pids({300: ("t300", b"x")}, parent_pid=999) is True

        lines = set(pid_file.read_text(encoding="utf-8").strip().splitlines())
        assert lines == {"777:999", "300:999:t300"}

    def test_replace_child_pids_writes_the_recorded_token_not_a_live_read(
        self, pid_file: Path
    ) -> None:
        """The writer must never read a live pid's identity for itself.

        Reading it here reopens the window the caller closed: a descendant that
        exited and had its number taken would be written with the STRANGER's
        token, and the sweep compares live against recorded — both the
        stranger's, so they agree and it kills an unrelated process.
        """
        from kiro_crew.session_pid import _replace_child_pids, _track_child_pids

        with patch("kiro_crew.session_pid._pid_start_token", return_value="old"):
            _track_child_pids({200: None}, parent_pid=999)
        assert pid_file.read_text(encoding="utf-8").strip() == "200:999:old"

        with patch("kiro_crew.session_pid._pid_start_token", return_value="live-stranger") as live:
            assert _replace_child_pids({200: ("recorded", b"x")}, parent_pid=999) is True

        live.assert_not_called()
        assert pid_file.read_text(encoding="utf-8").strip() == "200:999:recorded"

    def test_replace_child_pids_falls_back_to_two_fields(self, pid_file: Path) -> None:
        """An identity that cannot be a field leaves the sweep nothing to match."""
        from kiro_crew.session_pid import _replace_child_pids

        assert _replace_child_pids({200: (None, b"x"), 300: ("has:colon", b"y")}, 999) is True

        lines = set(pid_file.read_text(encoding="utf-8").strip().splitlines())
        assert lines == {"200:999", "300:999"}

    def test_replace_child_pids_drops_a_parents_last_child(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _replace_child_pids, _track_child_pids

        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({200: None}, parent_pid=999)

        assert _replace_child_pids({}, parent_pid=999, drop=(200,)) is True
        assert pid_file.read_text(encoding="utf-8").strip() == ""

    def test_replace_child_pids_with_nothing_named_writes_nothing(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _replace_child_pids, _track_child_pids

        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({200: None}, parent_pid=999)

        assert _replace_child_pids({}, parent_pid=999) is True
        assert pid_file.read_text(encoding="utf-8").strip() == "200:999"

    def test_replace_child_pids_reports_a_failed_write(self, pid_file: Path) -> None:
        """The caller publishes nothing on False, so the answer must be truthful."""
        from kiro_crew.session_pid import _replace_child_pids

        with patch("kiro_crew.session_pid._rewrite_pid_file", return_value=False):
            assert _replace_child_pids({200: ("t", b"x")}, parent_pid=999) is False

    def test_replace_child_pids_refuses_a_parentless_call(self, pid_file: Path) -> None:
        """parent_pid=0 names no owner's block, so there is nothing to replace."""
        from kiro_crew.session_pid import _replace_child_pids

        assert _replace_child_pids({200: ("t", b"x")}, parent_pid=0) is False


# ── Untracked orphan MCP sweep tests ───────────


class TestFindOrphanMcpCandidates:
    """Tests for find_orphan_mcp_candidates (process-table scan)."""

    def test_excludes_pids_in_active_set(self) -> None:
        """PIDs present in active_pids are never returned as candidates."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[100, 200]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"kirocrew_sandbox_abc.py"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids={100, 200})

        assert result == []

    def test_vanished_pid_logs_one_line_without_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A PID that exits between snapshot and probe logs no stack trace.

        The candidate is already gone, which is what the sweep wants, so the
        expected TOCTOU race must not emit exc_info.
        """
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[22620]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(
                Path,
                "read_bytes",
                side_effect=FileNotFoundError(2, "No such file or directory"),
            ),
            patch("os.getpid", return_value=1),
            caplog.at_level("DEBUG", logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []
        records = [r for r in caplog.records if "22620" in r.getMessage()]
        assert len(records) == 1
        assert records[0].exc_info is None
        assert "vanished before probe" in records[0].getMessage()

    def test_vanished_pid_on_macos_ps_exit_logs_no_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`ps -p <dead-pid>` exits non-zero — same race, same quiet handling."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[9140]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch(
                "kiro_crew.session_pid.subprocess.check_output",
                side_effect=subprocess.CalledProcessError(1, "ps"),
            ),
            patch("os.getpid", return_value=1),
            caplog.at_level("DEBUG", logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "darwin"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []
        records = [r for r in caplog.records if "9140" in r.getMessage()]
        assert len(records) == 1
        assert records[0].exc_info is None

    def test_unexpected_probe_error_keeps_traceback(self, caplog: pytest.LogCaptureFixture) -> None:
        """A genuinely unexpected probe failure still logs exc_info."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[555]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", side_effect=PermissionError(13, "denied")),
            patch("os.getpid", return_value=1),
            caplog.at_level("DEBUG", logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []
        records = [r for r in caplog.records if "555" in r.getMessage()]
        assert len(records) == 1
        assert records[0].exc_info is not None

    def test_excludes_non_kirocrew_processes(self) -> None:
        """Orphans without known MCP entrypoint markers are skipped."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[300]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(
                Path, "read_bytes", return_value=b"/usr/bin/python3\x00some_other_script.py"
            ),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_excludes_non_entrypoint_vim_grep(self) -> None:
        """Non-Python processes mentioning kirocrew in args (e.g. vim, grep) are skipped."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[350]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"vim\x00/tmp/kirocrew_sandbox_abc.log"),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_excludes_peer_gateway(self, tmp_path: Path) -> None:
        """Peer gateways with a LIVE socket path are never candidates.

        Age is patched above the min-age floor so the assertion depends on the
        _GATEWAY_MARKERS exclusion in _is_orphan_mcp, not on the age guard
        short-circuiting before the exclusion logic ever runs. The socket path
        must exist on disk: a gatewayd whose socket is GONE is deliberately
        sweepable via the reachability path (_is_sweepable_orphan_gatewayd).
        """
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        live_sock = tmp_path / "gw.sock"
        live_sock.write_text("")
        cmdline = (
            b"python3\x00-m\x00kiro_crew.mcp_gateway.gatewayd"
            b"\x00--socket\x00" + os.fsencode(str(live_sock))
        )
        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[360]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=cmdline),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_includes_kirocrew_orphan_not_in_active(self) -> None:
        """Orphaned process with sandbox wrapper entrypoint and not in active set is a candidate."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[400]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(
                Path,
                "read_bytes",
                return_value=b"python3\x00/tmp/kirocrew_sandbox_xyz.py",
            ),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == [400]

    def test_excludes_own_pid(self) -> None:
        """The gateway's own PID is never returned."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[999]),
            patch("os.getpid", return_value=999),
        ):
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_does_not_match_builder_mcp(self) -> None:
        """builder-mcp is NOT a process Kiro Crew spawns in this public fork.

        Regression guard: the upstream project's reaper lists ``builder-mcp`` (an
        internal server it manages), but the de-Amazoned fork never spawns
        it (the CPP companion contributes it, not the core). Reaping a user-owned
        ``builder-mcp`` orphan would SIGKILL an unrelated process, so the marker
        is deliberately absent here.
        """
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[410]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"builder-mcp\x00--stdio"),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_matches_macos_space_separated_cmdline(self) -> None:
        """macOS ps output (space-separated) is correctly parsed."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        def mock_check_output(cmd, **kwargs):
            # Single combined ps call returns "<etime> <command...>"
            if "etime=" in cmd and "command=" in cmd:
                return b"   05:00 python3 /tmp/kirocrew_sandbox_xyz.py"
            return b""

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[420]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch(
                "subprocess.check_output",
                side_effect=mock_check_output,
            ),
            patch("os.getpid", return_value=1),
        ):
            mock_sys.platform = "darwin"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == [420]

    def test_skips_young_processes(self) -> None:
        """Processes younger than _ORPHAN_MIN_AGE_SECONDS are never candidates."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[450]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(
                Path,
                "read_bytes",
                return_value=b"python3\x00/tmp/kirocrew_sandbox_new.py",
            ),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=50.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []


class TestReclaimOwnsEveryHarnessItTracked:
    """The reclaim recognises every harness Crew spawns, not two of them.

    A hand-written ``("kiro-cli", "claude")`` pair answered for two of eight, so a
    dead gateway's codex-acp, opencode, pi-acp, goose or dsh orphan answered "not
    ours" — and the branch for an unrecognised PID both SPARES the process and DROPS
    its tracking entry, the one file every sweep keys off to find it. Spared and
    forgotten.

    The marker set is projected from the backend registry, so the fix is a table a
    new harness joins rather than an edit here.
    """

    @staticmethod
    def _dead_gateway_liveness(gw_pid: int) -> "object":
        def fake_liveness(pid: int) -> str:
            return platform_compat.PID_DEAD if pid == gw_pid else platform_compat.PID_ALIVE

        return fake_liveness

    def test_every_registered_backend_has_a_process_name(self) -> None:
        """Ratchet: a harness with no name is a harness whose orphans leak.

        The reclaim cannot recognise a process it has no name for, and its failure
        mode is silent — the entry is dropped and the process spared. So the omission
        has to be a red test rather than something an operator finds later.
        """
        from kiro_crew.agent_sdk.backends import (
            ACP_BACKEND_PROCESS_NAMES,
            ACP_BACKENDS_KNOWN,
            agent_process_markers,
        )

        missing = sorted(ACP_BACKENDS_KNOWN - set(ACP_BACKEND_PROCESS_NAMES))
        assert not missing, (
            "these registered backends have no argv0 basename, so the PID-file "
            f"reclaim cannot recognise their orphans: {missing}"
        )
        markers = agent_process_markers()
        uncovered = sorted(
            name for name in ACP_BACKEND_PROCESS_NAMES.values() if name not in markers
        )
        assert not uncovered, f"named but absent from the marker set: {uncovered}"

    def test_the_self_served_names_come_from_the_launch_table(self) -> None:
        """The three harnesses with a launch row are not spelled twice."""
        from kiro_crew.agent_sdk.backends import (
            ACP_BACKEND_LAUNCH,
            ACP_BACKEND_PROCESS_NAMES,
        )

        for backend, record in ACP_BACKEND_LAUNCH.items():
            assert ACP_BACKEND_PROCESS_NAMES[backend] == record.binary, (
                f"{backend!r} names its process twice and the two disagree: "
                f"{ACP_BACKEND_PROCESS_NAMES[backend]!r} vs {record.binary!r}"
            )

    def test_the_adapter_basenames_agree_with_the_acp_layer(self) -> None:
        """The bespoke adapters' own constants READ this table, and must keep doing so.

        The names are declared once, in the registry, and ``acp.client`` indexes it --
        the import direction that is allowed, since ``agent_sdk.backends`` is a
        stdlib-only leaf while ``session_pid`` may not import ``kiro_crew.acp`` at all
        (``check_agent_sdk_boundary`` forbids it, ``test_agent_lifecycle_cycle`` pins the
        absence). This asserts the equality a re-spelling would break, so a literal
        reintroduced in either place is caught here rather than by a reclaim sweep
        failing to recognise the process the adapter spawns.
        """
        from kiro_crew.acp import runtime as acp_runtime
        from kiro_crew.acp.client import (
            CLAUDE_ACP_BIN,
            CODEX_ACP_BIN,
            KIRO_CLI_BIN,
            PI_ACP_BIN,
        )
        from kiro_crew.acp.types import (
            ACP_BACKEND_CLAUDE,
            ACP_BACKEND_CODEX,
            ACP_BACKEND_KIRO,
            ACP_BACKEND_PI,
        )
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_PROCESS_NAMES

        assert ACP_BACKEND_PROCESS_NAMES[ACP_BACKEND_CLAUDE] == CLAUDE_ACP_BIN
        assert ACP_BACKEND_PROCESS_NAMES[ACP_BACKEND_CODEX] == CODEX_ACP_BIN
        assert ACP_BACKEND_PROCESS_NAMES[ACP_BACKEND_PI] == PI_ACP_BIN
        # kiro's name stays a literal in the resolver's module -- indexing the table there
        # would make the DEFAULT backend's construction fail at import on a registry miss
        # -- so the equality is asserted instead, and the runtime module re-exports the
        # one value rather than declaring a second.
        assert ACP_BACKEND_PROCESS_NAMES[ACP_BACKEND_KIRO] == KIRO_CLI_BIN
        assert acp_runtime.KIRO_CLI_BIN is KIRO_CLI_BIN

    @pytest.mark.parametrize(
        "backend, cmdline",
        [
            ("kiro", "kiro-cli acp --agent-engine v3 --auth-method cli"),
            ("kas", "kiro-cli acp --agent-engine v3 --auth-method cli"),
            ("claude", "node /opt/n/bin/claude-agent-acp"),
            ("codex", "node /opt/n/bin/codex-acp"),
            ("pi", "node /opt/n/bin/pi-acp"),
            ("opencode", "/opt/n/bin/opencode serve"),
            ("goose", "goose acp"),
            ("deepseek", "dsh --profile acp"),
        ],
    )
    def test_each_harness_cmdline_is_recognised(self, backend: str, cmdline: str) -> None:
        """One case per backend, over the command lines they really run as.

        Command lines captured from the installed adapters; ``process_matches`` does a
        substring test over the whole cmdline on Linux and macOS, so this is the
        question the reclaim actually asks.
        """
        from kiro_crew.session_pid import _MANAGED_AGENT_MARKERS

        assert any(marker in cmdline for marker in _MANAGED_AGENT_MARKERS), (
            f"{backend}'s orphan reads as unmanaged, so the reclaim would drop its "
            f"entry and spare it: {cmdline!r}"
        )

    @pytest.mark.parametrize(
        "cmdline",
        [
            b"/usr/bin/python3\x00/home/u/friendship/app.py",
            b"/usr/local/bin/mongoose\x00--port\x008080",
            b"/bin/sh\x00-c\x00echo goosebumps",
            b"/opt/dshboard/bin/server\x00--serve",
        ],
    )
    def test_a_lookalike_cmdline_does_not_authorize_a_kill(self, cmdline: bytes) -> None:
        """The kill path matches per argv TOKEN, exactly, not as a substring.

        The projection introduced three-character names: ``dsh`` sits inside
        ``friendship``, ``goose`` inside ``mongoose``. Under a raw substring test over
        the whole command line, a recycled PID landing on any of these passes the
        recycle guard and is signalled.
        """
        from kiro_crew.session_pid import _cmdline_names_a_harness

        assert _cmdline_names_a_harness(cmdline) is False

    @pytest.mark.parametrize(
        "cmdline",
        [
            b"kiro-cli\x00acp\x00--agent-engine\x00v3",
            b"node\x00/opt/n/bin/codex-acp",
            b"node\x00/opt/n/bin/pi-acp",
            b"/opt/n/bin/opencode\x00serve",
            b"goose\x00acp",
            b"dsh\x00--profile\x00acp",
        ],
    )
    def test_a_real_harness_cmdline_still_authorizes(self, cmdline: bytes) -> None:
        """Tightening must not stop recognising the harnesses themselves.

        Two token slots answer, both by POSITION: ``argv[0]``, and ``argv[1]`` after an
        interpreter ``argv[0]`` -- a bespoke Node adapter is an entry script with a
        ``#!/usr/bin/env node`` shebang, so the kernel execs the interpreter and the
        adapter is at index 1.

        No interpreter-flag case is listed, deliberately. ``_resolve_node_adapter_argv``
        builds ``[node, script]`` and passes no Node options, so a flag between the two is
        a shape Crew does not produce -- and accepting one costs the whole positional rule,
        because scanning past options offers an option VALUE as the script slot. Same
        reasoning ``test_only_node_spellings_open_the_script_slot`` applies to the
        interpreter set: authority granted for a shape nothing produces is authority to
        signal a process Crew never spawned. (The ``node --experimental-wasm-modules`` line
        in ``kas_transport``'s docstring is built by kiro-cli for its OWN child; what Crew
        tracks for that backend is ``kiro-cli``, matched at ``argv[0]``.)

        If an adapter launch ever does need an interpreter flag, the cost of this rule is a
        missed reclaim -- the orphan is SPARED, not wrongly killed -- which is the direction
        this module fails in everywhere else.
        """
        from kiro_crew.session_pid import _cmdline_names_a_harness

        assert _cmdline_names_a_harness(cmdline) is True

    @pytest.mark.parametrize(
        "cmdline",
        [
            b"/usr/bin/node\x00build.js\x00--agent\x00goose",
            b"/usr/bin/vim\x00/home/u/notes/dsh",
            b"/usr/bin/tail\x00-f\x00/var/log/codex-acp",
            b"/usr/bin/grep\x00-rn\x00kiro-cli\x00/etc",
            b"/usr/bin/python3\x00-m\x00pytest\x00test/goose",
        ],
    )
    def test_an_argv_ARGUMENT_does_not_authorize_a_kill(self, cmdline: bytes) -> None:
        """Only argv0 and an interpreter's script slot may name a harness.

        A process's arguments are chosen by whoever started it and say nothing about
        what the process IS. Trying the basename of EVERY token therefore answered "this
        is a harness" for a training script passed ``--agent goose``, an editor opened
        on a file called ``dsh``, or a ``tail`` on an adapter's log -- and on the reclaim
        path that answer authorizes a SIGKILL of a PID this gateway never spawned. The
        interpreter cases are here on purpose: argv0 IS an interpreter in two of them, so
        the script slot opens, and what closes the hole is that only the FIRST non-flag
        token after argv0 is read.
        """
        from kiro_crew.session_pid import _cmdline_names_a_harness

        assert _cmdline_names_a_harness(cmdline) is False

    @pytest.mark.parametrize(
        "cmdline",
        [
            b"/Users/John Smith/.local/bin/node\x00/Users/John Smith/n/bin/codex-acp",
            b"/Users/John Smith/.local/bin/codex-acp\x00--stdio",
            b"/opt/My Tools/bin/node\x00/opt/My Tools/bin/pi-acp",
        ],
    )
    def test_a_harness_under_a_spaced_path_is_still_recognised(self, cmdline: bytes) -> None:
        """Linux ``/proc`` gives exact NUL boundaries; whitespace would break the path.

        A home directory named ``John Smith`` splits
        ``/Users/John Smith/.local/bin/node`` into ``/Users/John`` plus
        ``Smith/.local/bin/node`` under a whitespace split, so argv0's basename reads
        ``John``, the interpreter is not recognised, the script slot never opens, and a
        REAL adapter is treated as unmanaged -- the leak this module exists to close,
        reintroduced by the tokenizer.
        """
        from kiro_crew.session_pid import _cmdline_names_a_harness

        assert _cmdline_names_a_harness(cmdline) is True

    @pytest.mark.parametrize(
        "cmdline, expected",
        [
            # The three paths Crew actually launches, each under a different install root.
            (b"node\x00/opt/n/lib/node_modules/@agentclientprotocol/codex-acp/dist/index.js", True),
            (
                b"node\x00/home/u/proj/node_modules/@agentclientprotocol/"
                b"claude-agent-acp/dist/index.js",
                True,
            ),
            (b"node\x00/opt/n/lib/node_modules/pi-acp/dist/index.js", True),
            # THE FINDING'S CASE (span 99fb501fe250). ``goose``, ``opencode`` and ``dsh``
            # are single-binary harnesses -- Crew runs ``goose acp``, never
            # ``node .../goose/dist/index.js`` -- so a real unrelated npm application in a
            # directory of that name must not be taken for one of ours and SIGKILLed.
            (b"node\x00/srv/goose/dist/index.js", False),
            (b"node\x00/srv/opencode/dist/index.js", False),
            (b"node\x00/srv/dsh/dist/index.js", False),
            # Right leaf, wrong package: the claude adapter is published SCOPED, so an
            # unscoped directory of the same leaf name is somebody else's package.
            (b"node\x00/srv/node_modules/claude-agent-acp/dist/index.js", False),
            # A package whose name merely ends with an adapter's: the comparison is
            # segment-aligned, so this is a different package.
            (b"node\x00/srv/evil-pi-acp/dist/index.js", False),
            # A real adapter name under a build layout Crew does not produce. Only the
            # resolver's own relative path answers, and the resolver builds
            # ``<package>/dist/index.js``.
            (b"node\x00/opt/n/lib/node_modules/pi-acp/lib/index.mjs", False),
            # Ordinary Node applications.
            (b"node\x00/opt/n/lib/node_modules/express/dist/index.js", False),
            (b"node\x00/srv/app/dist/index.js", False),
            (b"node\x00/srv/notes/goose/app.js", False),
        ],
    )
    def test_a_package_entry_launch_is_recognised_by_its_resolved_path(
        self, cmdline: bytes, expected: bool
    ) -> None:
        """The resolver produces two script spellings, so both must be recognised --
        and the second is recognised by PATH, never by a name found along one.

        The bin shim (``node /opt/n/bin/codex-acp``) carries the name in the basename. The
        package entry the resolver builds has the basename ``index.js``, which names
        nothing, so that launch was retained as unmanaged by both reclaim arms forever.

        What identifies it is the resolved relative path -- one of
        ``backends.node_adapter_entry_relpaths()``, the same table the resolvers read to
        build it -- compared segment for segment against the token's tail. The install
        root above the package is free, because the resolver walks several.

        Reading a NAME out of the path instead leaves "what a process may call itself" an
        open axis, and the ``goose`` rows are what that costs: the harness set includes
        single-binary harnesses Crew never hands to Node, so a directory named for one of
        them made an unrelated application answer for a harness. Comparing a path this
        repository publishes closes the axis, because the set is one we own.
        """
        from kiro_crew.session_pid import _cmdline_names_a_harness

        assert _cmdline_names_a_harness(cmdline) is expected

    def test_every_launchable_adapter_path_is_recognised(self) -> None:
        """The identity set and the launch set are the SAME set, checked both ways.

        A path the resolver can produce and this gate does not recognise is an orphan
        nobody reclaims; a path this gate recognises and the resolver never produces is
        authority to signal a process Crew did not start. Derived from one table so
        neither can happen, and asserted here so the derivation cannot quietly stop.
        """
        from kiro_crew.agent_sdk.backends import (
            ACP_BACKEND_NODE_ADAPTER_PACKAGES,
            node_adapter_entry_relpaths,
        )
        from kiro_crew.session_pid import _cmdline_names_a_harness

        relpaths = node_adapter_entry_relpaths()
        assert len(relpaths) == len(ACP_BACKEND_NODE_ADAPTER_PACKAGES), (
            "an adapter package lost its entry path, so that launch is unrecognisable: "
            f"{relpaths} vs {sorted(ACP_BACKEND_NODE_ADAPTER_PACKAGES.values())}"
        )
        for relpath in relpaths:
            for root in (b"/opt/n/lib/node_modules/", b"/home/u/p/node_modules/"):
                cmdline = b"node\x00" + root + relpath.encode("utf-8")
                assert _cmdline_names_a_harness(cmdline) is True, (
                    f"the resolver can launch {relpath} and the reclaim does not "
                    "recognise it, so its orphans are never reaped"
                )

    @pytest.mark.parametrize(
        "cmdline, expected",
        [
            # The shape Crew launches: the script IS argv[1].
            (
                b"node\x00/opt/n/lib/node_modules/@agentclientprotocol/" b"codex-acp/dist/index.js",
                True,
            ),
            # THE FINDING (span 99fb501fe250, 4th spelling). An unrelated Node app whose
            # OPTION VALUE happens to be our adapter's real launch path. Scanning past
            # options and taking the next token offered that value as the script slot, so a
            # path a process merely MENTIONS authorized a SIGKILL of it.
            (
                b"node\x00app.js\x00--require\x00/opt/n/lib/node_modules/"
                b"@agentclientprotocol/codex-acp/dist/index.js",
                False,
            ),
            # The same shape with a NAME rather than a path, for the bin-shim arm.
            (b"node\x00app.js\x00--agent\x00codex-acp", False),
            # A value that precedes the real script: skipping `--require` reached
            # `codex-acp` before ever seeing `app.js`.
            (b"node\x00--require\x00codex-acp\x00app.js", False),
            # An interpreter option AT argv[1]. Crew emits none, so no slot opens -- rather
            # than scanning forward for something that looks like a script.
            (
                b"node\x00--inspect\x00/opt/n/lib/node_modules/@agentclientprotocol/"
                b"codex-acp/dist/index.js",
                False,
            ),
        ],
    )
    def test_only_the_script_position_may_name_a_harness(
        self, cmdline: bytes, expected: bool
    ) -> None:
        """The script slot is argv[1] by POSITION, never "the first non-flag token".

        Crew launches a Node adapter as ``[node, <script>]`` and passes no interpreter
        options, so the script is always at index 1. Scanning past options to find it hands
        an option VALUE to the name test instead: ``--require`` and its kin take one, so any
        process that merely MENTIONS our adapter's path or name in an argument was answered
        "this is a harness" -- and on the reclaim path that authorizes SIGKILL of a PID this
        gateway never spawned.

        Position is what makes the rule closed. Telling a value-taking Node option from a
        boolean one needs a table of Node's flags, which is an open set and a moving one;
        index 1 is neither.
        """
        from kiro_crew.session_pid import _cmdline_names_a_harness

        assert _cmdline_names_a_harness(cmdline) is expected

    def test_only_node_spellings_open_the_script_slot(self) -> None:
        """The interpreter set is authority, so it holds only shapes that exist.

        Every registered backend's bespoke adapter is a Node entry script. A name here
        widens the slot in which a harness name is accepted, so one added on speculation
        grants authority for a shape nothing produces.
        """
        from kiro_crew.session_pid import _HARNESS_INTERPRETERS

        assert {name.decode() for name in _HARNESS_INTERPRETERS} == {
            "node",
            "nodejs",
            "node.exe",
        }

    @pytest.mark.parametrize("basename", ["mongoose", "dshx", "xdsh", "gooseberry"])
    def test_a_lookalike_basename_is_not_taken_for_a_harness(self, basename: str) -> None:
        """Short generic names must not match as substrings of a basename.

        The projection introduced ``goose`` and ``dsh``, and the two consumers whose
        subject is an argv0 BASENAME rather than a command line would otherwise accept
        anything containing them. On the work sweep's negative gate that wrongly
        excludes a process from being swept; on the untracked-runtime report it names
        something that is not a harness at all.
        """
        from kiro_crew.session_pid import _MANAGED_AGENT_BASENAMES

        assert basename.encode() not in _MANAGED_AGENT_BASENAMES

    def test_every_harness_basename_matches_exactly(self) -> None:
        """The exact set still covers every harness, plus the two legacy spellings."""
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_PROCESS_NAMES
        from kiro_crew.session_pid import _MANAGED_AGENT_BASENAMES

        for name in ACP_BACKEND_PROCESS_NAMES.values():
            assert name.encode() in _MANAGED_AGENT_BASENAMES, name
        assert b"claude" in _MANAGED_AGENT_BASENAMES
        assert b"kiro-cli-chat" in _MANAGED_AGENT_BASENAMES

    def test_macos_reads_a_command_line_rather_than_substring_matching(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """macOS needs the tokenized test as much as Linux does.

        ``_pid_cmdline`` is a ``/proc`` read and answers empty off Linux, so without a
        darwin source the answer would fall back to a raw substring test over the whole
        command line — with three-character needles that sit inside ordinary words. That
        set is strictly more collision-prone than the two-name pair it replaced, so
        tightening only Linux would leave macOS worse off than before.
        """
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(sp, "_pid_cmdline", lambda pid, proc_root=None: b"")
        monkeypatch.setattr(
            sp.platform_compat,
            "process_command_line",
            lambda pid: "/usr/local/bin/mongoose --port 8080",
        )

        def _unexpected(pid, needles):
            raise AssertionError("macOS fell back to the substring test")

        monkeypatch.setattr(sp.platform_compat, "process_matches", _unexpected)

        assert sp._is_managed_agent_process(4242) is False

        monkeypatch.setattr(
            sp.platform_compat,
            "process_command_line",
            lambda pid: "node /opt/n/bin/codex-acp",
        )
        assert sp._is_managed_agent_process(4242) is True

    def test_the_scope_anchor_is_a_subset_of_the_projection(self) -> None:
        """The anchor selects from the projection; it does not spell names again.

        Both sets test an exact argv0 basename, so a name written twice can drift. The
        anchor is deliberately NARROWER — it authorizes an abandoned-scope reclaim to
        kill, where the projection only feeds a negative sweep gate and a report — and
        this pins that the narrowness is a selection rather than a stale copy. An empty
        selection would silently disarm the reaper, so that is checked too.
        """
        from kiro_crew.session_pid import (
            _MANAGED_AGENT_BASENAMES,
            _MANAGED_AGENT_RUNTIME_BASENAMES,
            _SCOPE_REAP_ANCHOR_NAMES,
        )

        assert _MANAGED_AGENT_RUNTIME_BASENAMES <= _MANAGED_AGENT_BASENAMES
        assert _MANAGED_AGENT_RUNTIME_BASENAMES, (
            "the scope-reaper anchor selected nothing out of the projection, which "
            "disarms it — a name in _SCOPE_REAP_ANCHOR_NAMES no longer appears there"
        )
        # The EXACT members, spelled out. A length check and a subset check both stay
        # green when a member is DELETED from ``_SCOPE_REAP_ANCHOR_NAMES``: the derived set
        # shrinks with it, the two lengths still agree, and that runtime's scope reclaim is
        # silently disarmed. Only naming the set catches a deletion.
        assert _SCOPE_REAP_ANCHOR_NAMES == {
            "claude",
            "claude-agent-acp",
            "kiro-cli",
            "kiro-cli-chat",
        }, (
            "the scope-reap anchor set changed; a DELETED member disarms that runtime's "
            f"scope reclaim without failing any other check: {sorted(_SCOPE_REAP_ANCHOR_NAMES)}"
        )
        assert len(_MANAGED_AGENT_RUNTIME_BASENAMES) == len(_SCOPE_REAP_ANCHOR_NAMES), (
            "a name the anchor selects is missing from the projection: "
            f"{sorted(_SCOPE_REAP_ANCHOR_NAMES - {n.decode() for n in _MANAGED_AGENT_BASENAMES})}"
        )

    def test_an_unrecognised_argv_orphan_is_never_killed(self, session_pid_file: Path) -> None:
        """The argv gate refuses the kill, and the entry's fate follows the TOKEN.

        Two questions, two answers. The argv gate authorizes the signal, so an
        unrecognised PID is never signalled. What happens to the ENTRY depends on which
        evidence is stronger: a settled token proves this PID still names the process the
        entry recorded, so dropping the record would spare the process and then make it
        unfindable by every sweep. A token-less entry has no such proof and is pruned.
        """
        from kiro_crew.session_pid import cleanup_orphaned_session_roots

        settled = "999999:99998:sometoken"
        tokenless = "999999:99997"
        session_pid_file.write_text(settled + "\n" + tokenless + "\n")
        kills: list[tuple[int, int]] = []

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=False),
            patch("kiro_crew.session_pid._pid_start_token", return_value="sometoken"),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                side_effect=self._dead_gateway_liveness(999999),
            ),
            patch("kiro_crew.session_pid.platform_compat.get_ppid", return_value=1),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
        ):
            cleanup_orphaned_session_roots()

        remaining = session_pid_file.read_text(encoding="utf-8")
        assert kills == [], "a matching token authorized a kill the argv test refused"
        assert settled in remaining, (
            "the entry was dropped for a process that was not killed, so nothing can "
            "reclaim that process afterwards"
        )
        assert tokenless not in remaining

    def test_a_launcher_rooted_orphan_is_killed_through_the_real_gate(
        self, session_pid_file: Path
    ) -> None:
        """The retain-vs-kill arm, driven by the gate's own answer on a launcher argv.

        ``test_an_unrecognised_argv_orphan_is_never_killed`` patches the gate out, so it
        proves the arm and not the gate. This drives the REAL
        ``_is_managed_agent_process`` off a faked ``/proc`` read, so the startup reclaim
        is covered for the sandboxed shape as well as the periodic sweep -- an entry that
        takes the retain branch here survives a gateway restart too.
        """
        from kiro_crew.session_pid import cleanup_orphaned_session_roots

        entry = "999999:99998:sometoken"
        session_pid_file.write_text(entry + "\n")
        cmdline = _launcher_cmdline("/usr/local/bin/kiro-cli", "acp")
        kills: list[tuple[int, int]] = []

        with (
            patch("kiro_crew.session_pid._pid_cmdline", return_value=cmdline),
            patch("kiro_crew.session_pid._pid_start_token", return_value="sometoken"),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                side_effect=self._dead_gateway_liveness(999999),
            ),
            patch("kiro_crew.session_pid.platform_compat.get_ppid", return_value=1),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
        ):
            cleanup_orphaned_session_roots()

        assert (99998, platform_compat.SIGKILL) in kills, (
            "the startup reclaim retained the sandboxed agent root, so its runtime "
            "survives a gateway restart as well as every periodic pass"
        )
        assert entry not in session_pid_file.read_text(encoding="utf-8")

    def test_a_recognised_orphan_is_killed(self, session_pid_file: Path) -> None:
        """The whole point: a harness the marker set now covers gets reaped."""
        from kiro_crew.session_pid import cleanup_orphaned_session_roots

        entry = "999999:99998:sometoken"
        session_pid_file.write_text(entry + "\n")
        kills: list[tuple[int, int]] = []

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value="sometoken"),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                side_effect=self._dead_gateway_liveness(999999),
            ),
            patch("kiro_crew.session_pid.platform_compat.get_ppid", return_value=1),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
        ):
            cleanup_orphaned_session_roots()

        assert (99998, platform_compat.SIGKILL) in kills
        assert entry not in session_pid_file.read_text(encoding="utf-8")

    def test_the_periodic_kill_phase_prunes_the_entry_it_actually_matched(
        self, session_pid_file: Path
    ) -> None:
        """The write-back matches on entry TEXT, so a rebuilt string prunes nothing.

        ``f"{gw}:{pid}"`` never equals a three-field token-bearing line, so a reaped
        process's entry survived in the file and every later pass met a dead PID
        there.
        """
        from kiro_crew.session_pid import _kill_confirmed_and_writeback

        my_gw = os.getpid()
        entry = f"{my_gw}:99998:sometoken"
        session_pid_file.write_text(entry + "\n")
        kills: list[int] = []

        with (
            patch("kiro_crew.session_pid.platform_compat.IS_WINDOWS", False),
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value="sometoken"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append(p),
            ),
        ):
            killed = _kill_confirmed_and_writeback(my_gw, [99998], set())

        assert kills == [99998]
        assert killed == 1
        assert entry not in session_pid_file.read_text(encoding="utf-8"), (
            "the three-field entry survived its own process, so the sweep meets a "
            "dead PID there on every later pass"
        )

    def test_a_settled_token_with_an_unrecognised_argv_retains_the_entry(
        self, session_pid_file: Path
    ) -> None:
        """Two pieces of evidence disagree, and the stronger one decides the entry.

        A settled token proves the PID still names the process this gateway spawned. If
        the argv gate does not recognise it -- which is what happens on Windows, where
        only an image name is readable and an interpreter-hosted adapter reads as
        ``node.exe`` -- then pruning spares the process AND discards the only record any
        sweep could find it by. That is the unreclaimable state: spared, then forgotten,
        which is the exact failure this change exists to remove.
        """
        from kiro_crew.session_pid import _sweep_pid_entries

        my_gw = os.getpid()
        entry = f"{my_gw}:99998:recorded"
        kills: list[int] = []

        with (
            patch("kiro_crew.session_pid.platform_compat.IS_WINDOWS", False),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            patch("kiro_crew.session_pid._pid_start_token", return_value="recorded"),
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=False),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append(p),
            ),
        ):
            killed, killed_or_dead, _candidates = _sweep_pid_entries(
                [entry],
                should_skip_tagged=lambda _gw, _p: False,
                should_skip_bare=lambda _p: True,
            )

        assert kills == [], "an argv-unrecognised process was signalled"
        assert killed == 0
        assert entry not in killed_or_dead, (
            "the entry was dropped for a process that was not killed, so nothing can "
            "reclaim that process afterwards"
        )

    def test_the_periodic_kill_phase_skips_a_candidate_with_no_entry(
        self, session_pid_file: Path
    ) -> None:
        """An entry absent from the kill phase's re-read is skipped, never killed.

        The scan phase reports PIDs across an event-loop hop. If the entry is gone by
        the time the kill phase re-reads the file, nothing records what that PID was
        when it was tracked, so the recycle guard has no input at all -- and standing in
        a rebuilt ``<gw>:<pid>`` default silently took the no-token branch, which skips
        the guard and signals on the strength of the stale verdict. The unreadable-file
        arm returns an empty index, so the same default made a transient read failure
        kill every candidate un-vouched.
        """
        from kiro_crew.session_pid import _kill_confirmed_and_writeback

        my_gw = os.getpid()
        session_pid_file.write_text("")
        kills: list[int] = []

        with (
            patch("kiro_crew.session_pid.platform_compat.IS_WINDOWS", False),
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append(p),
            ),
        ):
            killed = _kill_confirmed_and_writeback(my_gw, [99998], set())

        assert kills == [], "signalled a PID no entry in the file vouched for"
        assert killed == 0

    def test_the_periodic_kill_phase_prunes_a_recycled_candidate(
        self, session_pid_file: Path
    ) -> None:
        """The scan and the kill are separated by a loop hop, so identity is re-read.

        Without it the kill phase signalled whatever held the PID by then; the token
        is subtractive evidence and this is where it subtracts.
        """
        from kiro_crew.session_pid import _kill_confirmed_and_writeback

        my_gw = os.getpid()
        entry = f"{my_gw}:99998:recorded"
        session_pid_file.write_text(entry + "\n")
        kills: list[int] = []

        with (
            patch("kiro_crew.session_pid._pid_start_token", return_value="different"),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append(p),
            ),
        ):
            killed = _kill_confirmed_and_writeback(my_gw, [99998], set())

        assert kills == []
        assert killed == 0
        assert entry not in session_pid_file.read_text(encoding="utf-8")
