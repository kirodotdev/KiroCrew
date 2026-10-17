"""The ``Gateway Boots`` section of ``kirocrew doctor`` and the reader behind it.

Every gateway start pre-creates a crash-dump file, so the retained files are gateway starts; the
start-up sweep removes the header-only file of a session that is gone and rotation keeps at most
ten, so what remains is the stalls on record plus the newest start, not a complete boot history.
``crash_dump_store.last_boots`` reads the newest five retained dumps as ``(started, state)``
pairs, labelling the newest header-only file by its writer's liveness and the rest by what the file
shows, and the section prints them. The section is information only. A run of stalls turns the
report red through the repeated-stall issue of ``_doctor_crash_dumps`` and through nothing here.
"""

from __future__ import annotations

import ast
import inspect
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import cli_doctor
from kiro_crew.dashboard import crash_dump_store
from kiro_crew.dashboard.crash_dump_store import DUMP_PREFIX, DUMP_SUFFIX
from kiro_crew.doctor_checks import workload

#: The first header line the store writes; the ``opened`` stamp follows it.
_HEADER = "# KiroCrew loop-stall crash dump"  # brand-ok: mirrors the store's header bytes
#: A pid above every pid_max Kiro Crew runs under and below the ceiling a header may name, so
#: the header parses and no process can own it: the writer of such a dump is gone.
_GONE_PID = 2**30
#: What the section prints above its rows.
_SECTION_HEAD = (
    "\nGateway Boots\n"
    "  boots:       newest first, one per retained crash dump\n"
    "               The newest five retained crash-dump files are shown, one per gateway start. They\n"
    "               are the retained stalls and newest start within that slice, not a complete boot\n"
    "               history.\n"
)
_STACKS = (
    "Thread 0x00007f1234 (most recent call first):\n"
    '  File "/usr/lib/python3.12/threading.py", line 359 in wait\n'
)


def _at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _dump_path(dumps_dir: Path, started: datetime) -> Path:
    return dumps_dir / f"{DUMP_PREFIX}{_stamp(started)}{DUMP_SUFFIX}"


def _boot_dump(
    dumps_dir: Path,
    started: datetime,
    *,
    mtime: datetime | None = None,
    stalled: bool = False,
    pid: int | None = _GONE_PID,
    domain: str | None = None,
    start_id: str | None = None,
    header_stamp: str | None = None,
) -> Path:
    """A dump as one boot leaves it: the store's 4-line header, plus stacks when the boot wedged.

    ``pid`` is the writer the header names (``None`` leaves the ``# PID:`` line out, as a header
    from before it was recorded); ``start_id`` supplies the optional process identity; and
    ``header_stamp`` replaces the ``opened`` stamp (an empty one leaves it out).
    """
    stamp = _stamp(started) if header_stamp is None else header_stamp
    opened = f" — opened {stamp}" if stamp else ""
    if pid is None:
        pid_line = ""
    else:
        identity = "" if start_id is None else f" start={start_id}"
        pid_line = f"# PID: {pid} @ {domain or crash_dump_store._pid_domain()}{identity}\n"
    path = _dump_path(dumps_dir, started)
    path.write_text(
        f"{_HEADER}{opened}\n"
        f"{pid_line}"
        "# If thread stacks appear below, the event loop wedged and faulthandler fired.\n"
        "\n" + (_STACKS if stalled else ""),
        encoding="utf-8",
    )
    when = (mtime or started).timestamp()
    os.utime(path, (when, when))
    return path


def _unreadable_dump(dumps_dir: Path, started: datetime) -> Path:
    """A dump name the bounded reader refuses: a directory is not a regular file."""
    path = _dump_path(dumps_dir, started)
    path.mkdir()
    os.utime(path, (started.timestamp(), started.timestamp()))
    return path


@pytest.fixture
def dumps_dir(tmp_path: Path) -> Path:
    path = tmp_path / "crash-dumps"
    path.mkdir()
    return path


@pytest.fixture
def doctor_dumps(dumps_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The doctor reading ``dumps_dir`` and an empty data home instead of the host's."""
    monkeypatch.setattr(cli_doctor, "get_dumps_dir", lambda: dumps_dir)
    monkeypatch.setattr(cli_doctor, "config_dir", lambda: tmp_path / "home")
    return dumps_dir


# ── the reader ───────────────────────────────────────────────────────────────


def test_the_last_five_dumps_read_as_boots_newest_first(dumps_dir: Path) -> None:
    _boot_dump(dumps_dir, _at(9, 1))  # older than the five: not listed
    _boot_dump(dumps_dir, _at(10, 1), stalled=True, mtime=_at(10, 3))
    _boot_dump(dumps_dir, _at(11, 1))
    _boot_dump(dumps_dir, _at(12, 1), stalled=True, mtime=_at(12, 3))
    _boot_dump(dumps_dir, _at(13, 1))
    _boot_dump(dumps_dir, _at(14, 1), pid=os.getpid())  # no identity proof: diagnostic is unknown

    boots = crash_dump_store.last_boots(dumps_dir)

    assert boots == [
        (_at(14, 1), "unknown"),
        (_at(13, 1), "clean so far"),
        (_at(12, 1), "stalled"),
        (_at(11, 1), "clean so far"),
        (_at(10, 1), "stalled"),
    ]
    # "stalled" is the test the repeated-stall issue counts, not a second reading of it.
    stalled = [state for _, state in boots].count("stalled")
    assert stalled == crash_dump_store.dumps_with_stacks(dumps_dir)


@pytest.mark.parametrize(
    ("pid", "domain", "probe", "state"),
    [
        pytest.param(os.getpid(), None, None, "unknown", id="current PID without identity proof"),
        pytest.param(_GONE_PID, None, None, "not running", id="no process owns it"),
        pytest.param(
            _GONE_PID, None, lambda _pid: True, "unknown", id="live probe without identity proof"
        ),
        pytest.param(_GONE_PID, None, lambda _pid: False, "not running", id="the probe says gone"),
        pytest.param(_GONE_PID, "another-host", None, "unknown", id="another PID domain"),
        pytest.param(None, None, None, "unknown", id="no PID line"),
    ],
)
def test_the_newest_header_only_file_is_labelled_by_its_writers_liveness(
    dumps_dir: Path, pid, domain, probe, state
) -> None:
    """The sweep's own owner test decides ``running``; an owner it cannot probe is ``unknown``."""
    _boot_dump(dumps_dir, _at(13, 1), stalled=True)
    _boot_dump(dumps_dir, _at(14, 1), pid=pid, domain=domain)
    kwargs = {} if probe is None else {"is_pid_alive": probe}

    boots = crash_dump_store.last_boots(dumps_dir, **kwargs)

    assert boots == [(_at(14, 1), state), (_at(13, 1), "stalled")]


def test_current_pid_with_mismatched_start_id_is_not_running(
    dumps_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The doctor must not trust its own PID without matching the gateway start
    # identity: the PID may have been recycled after the gateway exited.
    monkeypatch.setattr(crash_dump_store, "_pid_start_id", lambda _pid: "200")
    path = _dump_path(dumps_dir, _at(14, 1))
    path.write_text(
        f"{_HEADER} — opened 20261014T010000Z\n"
        f"# PID: {os.getpid()} @ {crash_dump_store._pid_domain()} start=100\n"
        "# If thread stacks appear below, the event loop wedged and faulthandler fired.\n"
        "\n",
        encoding="utf-8",
    )

    assert crash_dump_store.last_boots(dumps_dir) == [(_at(14, 1), "not running")]


def test_only_the_newest_file_is_probed(dumps_dir: Path) -> None:
    _boot_dump(dumps_dir, _at(12, 1), stalled=True)
    _boot_dump(dumps_dir, _at(13, 1))
    _boot_dump(dumps_dir, _at(14, 1))
    probed: list[int] = []

    crash_dump_store.last_boots(dumps_dir, is_pid_alive=lambda pid: probed.append(pid) is None)

    assert probed == [_GONE_PID]


def test_a_dump_that_cannot_be_read_is_unknown_never_clean(dumps_dir: Path) -> None:
    _boot_dump(dumps_dir, _at(12, 1))
    _unreadable_dump(dumps_dir, _at(13, 1))
    _boot_dump(dumps_dir, _at(14, 1))

    assert crash_dump_store.last_boots(dumps_dir) == [
        (_at(14, 1), "not running"),
        (_at(13, 1), "unknown"),
        (_at(12, 1), "clean so far"),
    ]


@pytest.mark.parametrize(
    ("successor_is_stalled", "successor_domain"),
    [(True, None), (False, "another-host")],
    ids=["stalled successor", "another host's successor"],
)
def test_a_boot_with_no_local_header_only_successor_is_unknown_not_clean(
    dumps_dir: Path, successor_is_stalled: bool, successor_domain: str | None
) -> None:
    _boot_dump(dumps_dir, _at(13, 1))
    _boot_dump(dumps_dir, _at(14, 1), stalled=successor_is_stalled, domain=successor_domain)

    assert crash_dump_store.last_boots(dumps_dir)[1] == (_at(13, 1), "unknown")


def test_the_start_time_is_the_header_stamp_else_the_mtime(dumps_dir: Path) -> None:
    # A stalled dump's mtime is the stall, hours after the start: the stamp wins.
    _boot_dump(dumps_dir, _at(11, 1), stalled=True, mtime=_at(11, 9))
    # A stamp that is not a date, and a header with no stamp: the mtime stands in.
    _boot_dump(dumps_dir, _at(12, 1), mtime=_at(12, 2), header_stamp="20261313T250000Z")
    _boot_dump(dumps_dir, _at(13, 1), mtime=_at(13, 2), header_stamp="")

    started = [when for when, _ in reversed(crash_dump_store.last_boots(dumps_dir))]

    assert started == [_at(11, 1), _at(12, 2), _at(13, 2)]


def test_a_time_the_platform_cannot_convert_is_no_time(dumps_dir: Path) -> None:
    _boot_dump(dumps_dir, _at(14, 1), header_stamp="")

    with patch.object(crash_dump_store, "datetime") as refusing:
        refusing.fromtimestamp.side_effect = OverflowError("year out of range")
        boots = crash_dump_store.last_boots(dumps_dir)

    assert boots == [(None, "not running")]


def test_the_header_the_dump_writer_leaves_reads_back_as_the_start(
    dumps_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crash_dump_store, "_active_dump_file", None)
    opened = crash_dump_store.open_dump_file(dumps_dir)
    try:
        stamp = opened.path.read_text(encoding="utf-8").splitlines()[0].rsplit(" ", 1)[1]
        yesterday = time.time() - 86400
        os.utime(opened.path, (yesterday, yesterday))  # the mtime must not be what is read

        boots = crash_dump_store.last_boots(dumps_dir)
    finally:
        os.close(opened.fileno())

    started = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    state = "running" if crash_dump_store._pid_start_id(os.getpid()) is not None else "unknown"
    assert boots == [(started, state)]  # missing identity proof is unknown


def test_no_dump_is_no_boot(dumps_dir: Path, tmp_path: Path) -> None:
    assert crash_dump_store.last_boots(dumps_dir) == []
    assert crash_dump_store.last_boots(tmp_path / "never-created") == []


def test_a_start_that_never_wedged_is_swept_by_the_next_start(
    dumps_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway sweeps, rotates, then opens its dump at start, so a clean restart leaves no row."""
    # The new dump is stamped by the real clock, so the earlier ones are ordered before it.
    now = datetime.now(timezone.utc)
    _boot_dump(dumps_dir, _at(12, 1), stalled=True, mtime=now - timedelta(hours=2))
    _boot_dump(dumps_dir, _at(13, 1), mtime=now - timedelta(hours=1))  # exited without ever wedging
    monkeypatch.setattr(crash_dump_store, "_active_dump_file", None)

    crash_dump_store.sweep_stale_dumps(dumps_dir, is_pid_alive=lambda _pid: False)
    crash_dump_store.rotate_dumps(dumps_dir=dumps_dir, is_pid_alive=lambda _pid: False)
    opened = crash_dump_store.open_dump_file(dumps_dir)
    try:
        boots = crash_dump_store.last_boots(dumps_dir)
    finally:
        os.close(opened.fileno())

    assert [state for _, state in boots] == ["running", "stalled"]
    assert boots[1][0] == _at(12, 1)
    assert not _dump_path(dumps_dir, _at(13, 1)).exists()


# ── the doctor section ───────────────────────────────────────────────────────


def test_the_section_prints_one_row_per_boot_in_order(doctor_dumps: Path, capsys) -> None:
    _boot_dump(doctor_dumps, _at(10, 1), stalled=True, mtime=_at(10, 3))
    _boot_dump(doctor_dumps, _at(11, 1))
    _boot_dump(doctor_dumps, _at(12, 1), stalled=True, mtime=_at(12, 3))
    _boot_dump(doctor_dumps, _at(13, 1))
    _boot_dump(doctor_dumps, _at(14, 1))  # the newest writer is gone: a stopped gateway

    workload._doctor_last_boots()

    assert capsys.readouterr().out == (
        _SECTION_HEAD + "    2026-10-14T01:00:00Z  not running\n"
        "    2026-10-13T01:00:00Z  clean so far\n"
        "    2026-10-12T01:00:00Z  stalled\n"
        "    2026-10-11T01:00:00Z  clean so far\n"
        "    2026-10-10T01:00:00Z  stalled\n"
    )


def test_the_section_prints_running_for_a_live_writer(doctor_dumps: Path, capsys) -> None:
    start_id = crash_dump_store._pid_start_id(os.getpid())
    if start_id is None:
        pytest.skip("no process start-id probe on this platform")
    _boot_dump(doctor_dumps, _at(13, 1), stalled=True, mtime=_at(13, 3))
    _boot_dump(doctor_dumps, _at(14, 1), pid=os.getpid(), start_id=start_id)

    workload._doctor_last_boots()

    assert capsys.readouterr().out == (
        _SECTION_HEAD + "    2026-10-14T01:00:00Z  running\n    2026-10-13T01:00:00Z  stalled\n"
    )


def test_the_section_prints_an_unreadable_dump_as_unknown(doctor_dumps: Path, capsys) -> None:
    _boot_dump(doctor_dumps, _at(13, 1))
    _unreadable_dump(doctor_dumps, _at(14, 1))

    workload._doctor_last_boots()

    assert capsys.readouterr().out == (
        _SECTION_HEAD + "    2026-10-14T01:00:00Z  unknown\n    2026-10-13T01:00:00Z  unknown\n"
    )


def test_the_section_is_silent_when_no_dump_is_retained(doctor_dumps: Path, capsys) -> None:
    workload._doctor_last_boots()

    assert capsys.readouterr().out == ""


def test_a_reader_that_raises_is_one_warning_row_without_its_message(
    doctor_dumps: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_dumps_dir: Path | None = None) -> list:
        raise PermissionError("/private/data-home/logs is not readable")

    monkeypatch.setattr(cli_doctor, "last_boots", refuse)

    workload._doctor_last_boots()

    assert capsys.readouterr().out == (
        "\nGateway Boots\n  boots:       ⚠️  could not read the retained crash dumps\n"
    )


def test_five_stalled_dumps_raise_the_existing_issue_and_the_section_adds_none(
    doctor_dumps: Path, capsys
) -> None:
    now = time.time()
    for i in range(5):
        recent = datetime.fromtimestamp(now - 600 * (5 - i), timezone.utc)
        _boot_dump(doctor_dumps, _at(10 + i, 1), stalled=True, mtime=recent)
    issues: list[str] = []
    workload._doctor_crash_dumps(issues)
    capsys.readouterr()
    issues_after_dumps = list(issues)

    workload._doctor_last_boots()

    assert len(issues_after_dumps) == 1
    assert issues_after_dumps[0].startswith("recent loop-stall crash dump")
    assert capsys.readouterr().out.count("  stalled\n") == 5
    assert issues == issues_after_dumps
    # It cannot add an issue because it is never handed the list.
    assert not inspect.signature(workload._doctor_last_boots).parameters


def test_doctor_runs_the_section_after_the_crash_dump_section_and_hands_it_nothing() -> None:
    calls = sorted(
        (
            node
            for node in ast.walk(ast.parse(inspect.getsource(cli_doctor._doctor)))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "workload"
            and node.func.attr in {"_doctor_crash_dumps", "_doctor_last_boots"}
        ),
        key=lambda node: node.lineno,
    )

    assert [call.func.attr for call in calls] == ["_doctor_crash_dumps", "_doctor_last_boots"]
    assert not calls[1].args and not calls[1].keywords
