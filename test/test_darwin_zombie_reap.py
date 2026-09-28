"""The macOS arm of the provider teardown can see, and reap, a zombie root.

``proc_pidinfo`` refuses a zombie outright, so a liveness probe reads an
exited-but-unreaped group leader as "still running" (a zombie answers
``kill(pid, 0)`` as present) and libproc reads it as "identity unknown" (no
start instant to compare). A teardown built on those two probes holds the
leader's zombie until the last group signal and then cannot prove it is safe to
collect. ``sysctl KERN_PROC`` walks the kernel's zombie list too; these tests
pin the record parse, the three-way zombie verdict, the group listing and the
start-id fallback -- with a fake ``libc``, so they hold on every platform. The real-process end-to-end lives in
``test_pid_lifecycle.py::TestSyncKillProviderTree`` and runs on the macOS suite.
"""

from __future__ import annotations

import ctypes
import logging
import struct

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew import session_pid as sp

_SZOMB = 5
_SRUN = 2


def _record(pid: int, *, sec: int = 1_700_000_000, usec: int = 123_456, stat: int = _SRUN) -> bytes:
    """One ``kinfo_proc`` record with the fields the reader looks at filled in."""
    raw = bytearray(pc._DARWIN_KINFO_PROC_SIZE)
    struct.pack_into("<q", raw, pc._DARWIN_KP_START_TVSEC_OFFSET, sec)
    struct.pack_into("<i", raw, pc._DARWIN_KP_START_TVUSEC_OFFSET, usec)
    struct.pack_into("<b", raw, pc._DARWIN_KP_STAT_OFFSET, stat)
    struct.pack_into("<i", raw, pc._DARWIN_KP_PID_OFFSET, pid)
    return bytes(raw)


class _FakeLibc:
    """``sysctl`` over a canned process table.

    ``table`` maps ``(selector, arg)`` to the bytes the kernel would write; a
    missing key is the kernel's "no such process": success with zero length.
    ``fail`` makes every call return -1, the unreadable case.
    """

    def __init__(self, table: dict[tuple[int, int], bytes], *, fail: bool = False) -> None:
        self.table = table
        self.fail = fail
        self.calls: list[tuple[int, int, bool]] = []

    def sysctl(self, mib, namelen, buf, size_ref, _newp, _newlen) -> int:  # noqa: ANN001
        assert namelen == 4
        assert mib[0] == pc._DARWIN_CTL_KERN and mib[1] == pc._DARWIN_KERN_PROC
        key = (mib[2], mib[3])
        self.calls.append((mib[2], mib[3], buf is None))
        if self.fail:
            return -1
        data = self.table.get(key, b"")
        size = size_ref._obj
        if buf is None:  # size query
            size.value = len(data)
            return 0
        if len(data) > size.value:
            return -1  # ENOMEM: the caller's buffer is too small
        ctypes.memmove(buf, data, len(data))
        size.value = len(data)
        return 0


@pytest.fixture
def libc(monkeypatch: pytest.MonkeyPatch):
    def _install(table: dict[tuple[int, int], bytes], *, fail: bool = False) -> _FakeLibc:
        fake = _FakeLibc(table, fail=fail)
        monkeypatch.setattr(pc, "_darwin_sysctl_handle", lambda: fake)
        return fake

    return _install


PID = pc._DARWIN_KERN_PROC_PID
PGRP = pc._DARWIN_KERN_PROC_PGRP


class TestKinfoParse:
    def test_reads_pid_state_and_start_instant(self) -> None:
        facts = pc._darwin_kinfo_proc_parse(_record(4242, sec=1_700_000_000, usec=7, stat=_SZOMB))
        assert facts == pc.DarwinKinfoProc(pid=4242, zombie=True, start_id="1700000000.000007")

    def test_start_id_matches_the_libproc_format(self) -> None:
        """The two readers must agree byte-for-byte or a recorded identity
        (taken live via libproc) would never match the zombie's (via sysctl)."""
        facts = pc._darwin_kinfo_proc_parse(_record(1, sec=1_700_000_000, usec=123_456))
        assert facts is not None
        assert facts.start_id == f"{1_700_000_000}.{123_456:06d}"

    @pytest.mark.parametrize("sec,usec,pid", [(0, 0, 1), (-1, 0, 1), (10, -1, 1), (10, 0, 0)])
    def test_implausible_record_is_refused(self, sec: int, usec: int, pid: int) -> None:
        assert pc._darwin_kinfo_proc_parse(_record(pid, sec=sec, usec=usec)) is None


class TestPidIsZombie:
    def test_zombie_reads_true(self, libc) -> None:
        libc({(PID, 77): _record(77, stat=_SZOMB)})
        assert pc.darwin_pid_is_zombie(77) is True

    def test_live_reads_false(self, libc) -> None:
        libc({(PID, 77): _record(77, stat=_SRUN)})
        assert pc.darwin_pid_is_zombie(77) is False

    def test_gone_reads_true(self, libc) -> None:
        """No such process is the kernel answering success with zero bytes: the
        process finished running AND was reaped, which is 'exited' to a caller."""
        libc({})
        assert pc.darwin_pid_is_zombie(77) is True

    def test_unreadable_reads_none(self, libc) -> None:
        libc({}, fail=True)
        assert pc.darwin_pid_is_zombie(77) is None

    def test_wrong_struct_size_reads_none(self, libc) -> None:
        """A record of another length means the offsets do not describe it."""
        libc({(PID, 77): _record(77)[:-8]})
        assert pc.darwin_pid_is_zombie(77) is None

    def test_wrong_struct_size_warns_once(
        self, libc, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The size guard disables every zombie reader, so it must say so --
        once per process, not once per poll."""
        libc({(PID, 77): _record(77)[:-8]})
        monkeypatch.setattr(pc, "_darwin_kinfo_size_mismatch_logged", False)
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            assert pc.darwin_pid_is_zombie(77) is None
            assert pc.darwin_pid_is_zombie(77) is None
            assert pc.darwin_kinfo_proc(77) is None
        hits = [r for r in caplog.records if "kinfo_proc" in r.getMessage()]
        assert len(hits) == 1
        assert "640 bytes" in hits[0].getMessage() and "648-byte" in hits[0].getMessage()

    def test_record_for_another_pid_reads_none(self, libc) -> None:
        libc({(PID, 77): _record(78)})
        assert pc.darwin_pid_is_zombie(77) is None

    def test_non_positive_pid_reads_none(self, libc) -> None:
        fake = libc({})
        assert pc.darwin_pid_is_zombie(0) is None
        assert pc.darwin_pid_is_zombie(-5) is None
        assert fake.calls == [], "pid 0 / negative would be a group query, never made"


class TestKinfoProc:
    def test_zombie_still_has_an_identity(self, libc) -> None:
        libc({(PID, 77): _record(77, stat=_SZOMB)})
        facts = pc.darwin_kinfo_proc(77)
        assert facts is not None and facts.zombie and facts.start_id == "1700000000.123456"

    def test_gone_and_unreadable_both_read_none(self, libc) -> None:
        libc({})
        assert pc.darwin_kinfo_proc(77) is None
        libc({}, fail=True)
        assert pc.darwin_kinfo_proc(77) is None


class TestPgroupMembers:
    def test_lists_every_member_with_its_state(self, libc) -> None:
        libc({(PGRP, 100): _record(100, stat=_SZOMB) + _record(101) + _record(102)})
        members = pc.darwin_pgroup_members(100)
        assert members is not None
        assert [(m.pid, m.zombie) for m in members] == [(100, True), (101, False), (102, False)]

    def test_sizes_by_a_first_query_then_reads(self, libc) -> None:
        fake = libc({(PGRP, 100): _record(100)})
        pc.darwin_pgroup_members(100)
        assert fake.calls == [(PGRP, 100, True), (PGRP, 100, False)]

    def test_empty_group_is_an_empty_list(self, libc) -> None:
        libc({})
        assert pc.darwin_pgroup_members(100) == []

    def test_unreadable_is_none(self, libc) -> None:
        libc({}, fail=True)
        assert pc.darwin_pgroup_members(100) is None

    def test_partial_trailing_record_refuses_the_whole_answer(self, libc) -> None:
        libc({(PGRP, 100): _record(100) + _record(101)[:100]})
        assert pc.darwin_pgroup_members(100) is None

    def test_group_outgrowing_the_slack_is_resized_and_read(self, libc) -> None:
        """The listing authorises the group SIGKILL, so a tree that forks
        faster than the slack between the size query and the read must not be
        able to make its own group unreadable: the overflow is retried with
        more room, not reported."""
        fake = libc({(PGRP, 100): _record(100)})
        real_sysctl = fake.sysctl

        def _growing(mib, namelen, buf, size_ref, newp, newlen) -> int:  # noqa: ANN001
            rc = real_sysctl(mib, namelen, buf, size_ref, newp, newlen)
            if buf is None:  # after every size query the group grows past the slack
                fake.table[(PGRP, 100)] += b"".join(
                    _record(200 + i) for i in range(pc._DARWIN_KINFO_PGRP_SLACK + 1)
                )
            return rc

        fake.sysctl = _growing
        members = pc.darwin_pgroup_members(100)
        assert members is not None
        assert members[0].pid == 100 and len(members) > pc._DARWIN_KINFO_PGRP_SLACK
        reads = [c for c in fake.calls if not c[2]]
        assert len(reads) == 2, "first read overflowed, the resized second one succeeded"

    def test_group_still_growing_after_the_last_attempt_is_none(self, libc) -> None:
        fake = libc({(PGRP, 100): _record(100)})
        real_sysctl = fake.sysctl

        def _exploding(mib, namelen, buf, size_ref, newp, newlen) -> int:  # noqa: ANN001
            rc = real_sysctl(mib, namelen, buf, size_ref, newp, newlen)
            if buf is None:
                fake.table[(PGRP, 100)] += _record(200) * (len(fake.calls) * 64)
            return rc

        fake.sysctl = _exploding
        assert pc.darwin_pgroup_members(100) is None
        assert len([c for c in fake.calls if not c[2]]) == pc._DARWIN_KINFO_PGRP_ATTEMPTS


class TestStartIdFallsBackForAZombie:
    """``get_process_start_id`` on darwin: libproc first, sysctl for what it refuses."""

    @pytest.fixture(autouse=True)
    def _darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc.sys, "platform", "darwin")

    def test_libproc_answer_wins(self, monkeypatch: pytest.MonkeyPatch, libc) -> None:
        fake = libc({(PID, 77): _record(77, sec=1, usec=1)})
        monkeypatch.setattr(pc, "_darwin_libproc_start_id", lambda pid: "1700000000.000009")
        assert pc.get_process_start_id(77) == "1700000000.000009"
        assert fake.calls == [], "sysctl is not consulted while libproc answers"

    def test_zombie_identity_comes_from_sysctl(self, monkeypatch: pytest.MonkeyPatch, libc) -> None:
        libc({(PID, 77): _record(77, stat=_SZOMB)})
        monkeypatch.setattr(pc, "_darwin_libproc_start_id", lambda pid: None)
        assert pc.get_process_start_id(77) == "1700000000.123456"

    def test_gone_is_still_unknown(self, monkeypatch: pytest.MonkeyPatch, libc) -> None:
        libc({})
        monkeypatch.setattr(pc, "_darwin_libproc_start_id", lambda pid: None)
        assert pc.get_process_start_id(77) is None

    def test_no_libproc_at_all_is_unknown_not_a_zombie(
        self, monkeypatch: pytest.MonkeyPatch, libc
    ) -> None:
        """A host whose libproc cannot load has no identity oracle: the sysctl
        fallback complements a refusal for ONE pid, it does not replace the
        library."""
        fake = libc({(PID, 77): _record(77)})

        def _unavailable(pid: int) -> str | None:
            raise pc._DarwinLibprocUnavailable

        monkeypatch.setattr(pc, "_darwin_libproc_start_id", _unavailable)
        assert pc.get_process_start_id(77) is None
        assert fake.calls == []

    def test_loader_failure_is_raised_not_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc.ctypes.util, "find_library", lambda _n: "libproc.dylib")

        def _boom(_path: object) -> object:
            raise OSError("cannot load libproc")

        monkeypatch.setattr(pc.ctypes, "CDLL", _boom)
        with pytest.raises(pc._DarwinLibprocUnavailable):
            pc._darwin_libproc_start_id(77)
        monkeypatch.setattr(pc.ctypes.util, "find_library", lambda _n: None)
        with pytest.raises(pc._DarwinLibprocUnavailable):
            pc._darwin_libproc_start_id(77)


class TestTeardownDarwinArm:
    """``session_pid`` reads the zombie state instead of liveness on darwin."""

    @pytest.fixture(autouse=True)
    def _darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sp.sys, "platform", "darwin")

    def test_exited_but_unreaped_sees_a_zombie(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[int] = []
        monkeypatch.setattr(pc, "pid_exists", lambda pid: seen.append(pid) or True)
        monkeypatch.setattr(pc, "darwin_pid_is_zombie", lambda pid: True)
        assert sp._pid_exited_but_unreaped(77) is True
        assert seen == [], "liveness must not be the oracle: a zombie is 'alive' to it"

    def test_exited_but_unreaped_live_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc, "darwin_pid_is_zombie", lambda pid: False)
        assert sp._pid_exited_but_unreaped(77) is False

    def test_exited_but_unreaped_unreadable_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unknown means 'still running': the caller waits out its grace rather
        than declaring the root gone on a guess."""
        monkeypatch.setattr(pc, "darwin_pid_is_zombie", lambda pid: None)
        assert sp._pid_exited_but_unreaped(77) is False

    def test_group_held_only_by_its_zombie_leader_is_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            pc,
            "darwin_pgroup_members",
            lambda pgid: [pc.DarwinKinfoProc(pid=100, zombie=True, start_id="1.000001")],
        )
        monkeypatch.setattr(pc, "pgroup_exists", lambda pgid: True)
        assert sp._pgroup_has_member_besides(100, 100) is False

    def test_live_member_besides_the_root_holds_the_group(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            pc,
            "darwin_pgroup_members",
            lambda pgid: [
                pc.DarwinKinfoProc(pid=100, zombie=True, start_id="1.000001"),
                pc.DarwinKinfoProc(pid=101, zombie=False, start_id="1.000002"),
            ],
        )
        assert sp._pgroup_has_member_besides(100, 100) is True

    def test_zombie_member_besides_the_root_does_not_hold_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            pc,
            "darwin_pgroup_members",
            lambda pgid: [
                pc.DarwinKinfoProc(pid=100, zombie=False, start_id="1.000001"),
                pc.DarwinKinfoProc(pid=101, zombie=True, start_id="1.000002"),
            ],
        )
        assert sp._pgroup_has_member_besides(100, 100) is False

    def test_unreadable_group_is_assumed_held(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc, "darwin_pgroup_members", lambda pgid: None)
        assert sp._pgroup_has_member_besides(100, 100) is True


class TestPidInPgroup:
    """Group membership survives the leader becoming a zombie on darwin.

    ``getpgid`` refuses a macOS zombie, and the zombie root is the one verified
    member ``_pgroup_still_ours`` can still name on the SIGKILL round, so the
    group listing has to answer where ``getpgid`` cannot.
    """

    def test_getpgid_answer_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc, "pgroup_of", lambda pid: 100)
        monkeypatch.setattr(
            pc, "darwin_pgroup_members", lambda pgid: pytest.fail("listing not needed")
        )
        assert sp._pid_in_pgroup(100, 100, "1.000001") is True

    def test_zombie_leader_is_found_in_the_listing_on_darwin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "pgroup_of", lambda pid: None)
        monkeypatch.setattr(
            pc,
            "darwin_pgroup_members",
            lambda pgid: [
                pc.DarwinKinfoProc(pid=100, zombie=True, start_id="1.000001"),
                pc.DarwinKinfoProc(pid=101, zombie=False, start_id="1.000002"),
            ],
        )
        assert sp._pid_in_pgroup(100, 100, "1.000001") is True
        assert sp._pid_in_pgroup(102, 100, "1.000003") is False

    def test_recycled_pid_is_not_found_in_the_listing_on_darwin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "pgroup_of", lambda pid: None)
        monkeypatch.setattr(
            pc,
            "darwin_pgroup_members",
            lambda pgid: [pc.DarwinKinfoProc(pid=100, zombie=False, start_id="2.000001")],
        )
        assert sp._pid_in_pgroup(100, 100, "1.000001") is False

    def test_missing_start_id_is_not_a_member_on_darwin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "pgroup_of", lambda pid: None)
        monkeypatch.setattr(
            pc,
            "darwin_pgroup_members",
            lambda pgid: [pc.DarwinKinfoProc(pid=100, zombie=True, start_id="1.000001")],
        )
        assert sp._pid_in_pgroup(100, 100, None) is False

    def test_a_definite_other_group_is_final(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``getpgid`` naming another group means the pid left, or the number was
        handed on -- the listing must not overrule that towards signalling."""
        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "pgroup_of", lambda pid: 999_999)
        monkeypatch.setattr(
            pc, "darwin_pgroup_members", lambda pgid: pytest.fail("listing must not be consulted")
        )
        assert sp._pid_in_pgroup(100, 100, "1.000001") is False

    def test_unreadable_listing_is_not_a_member(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "pgroup_of", lambda pid: None)
        monkeypatch.setattr(pc, "darwin_pgroup_members", lambda pgid: None)
        assert sp._pid_in_pgroup(100, 100, "1.000001") is False

    def test_other_platforms_trust_getpgid_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sp.sys, "platform", "linux")
        monkeypatch.setattr(pc, "pgroup_of", lambda pid: None)
        monkeypatch.setattr(
            pc, "darwin_pgroup_members", lambda pgid: pytest.fail("darwin-only listing")
        )
        assert sp._pid_in_pgroup(100, 100, "1.000001") is False

    def test_group_still_ours_through_a_zombie_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The SIGKILL-round shape: root identity verified, root a zombie, no
        recorded descendants. The group must still read as ours."""
        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "IS_WINDOWS", False)  # the darwin arm, on every CI host
        monkeypatch.setattr(pc, "get_process_start_id", lambda pid: "1.000001")
        monkeypatch.setattr(pc, "pgroup_of", lambda pid: None)
        monkeypatch.setattr(
            pc,
            "darwin_pgroup_members",
            lambda pgid: [pc.DarwinKinfoProc(pid=100, zombie=True, start_id="1.000001")],
        )
        assert sp._pgroup_still_ours(100, 100, "1.000001", {}, gated=True) is True
