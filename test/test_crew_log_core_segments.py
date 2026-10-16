"""Append-only crew log core: where a log may live, and the segments it is kept in.

Containment (an id that links out of its root, the owner-only root), the sandbox
disposition of every log, and the segment layout: found and ordered by first seq,
foreign, renamed and linked segments refused, retention distinguished from damage,
and the directory modes a filesystem that refuses chmod still gets.
"""

from __future__ import annotations

import os
import stat

import crew_log_core_helpers
import pytest
from crew_log_core_helpers import (
    SESSION,
    _code,
    _crew,
    _log_bytes,
    _raises,
    _session,
    _split_into_segments,
)

from kiro_crew import crew_log as lg
from kiro_crew import sandbox
from kiro_crew.config import paths
from kiro_crew.config.paths import data_home, ensure_data_home
from kiro_crew.crew_log import CrewLog, lease, store
from kiro_crew.security.paths import is_sensitive_path
from kiro_crew.session_ledger import _store_name

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_isolated_home = crew_log_core_helpers._isolated_home


# --- containment ----------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_an_id_that_symlinks_out_of_its_root_is_refused():
    # The shape gate cannot see this one: the id has no separator, the ESCAPE is
    # in the filesystem. Containment is re-checked on the resolved path.
    _crew()
    root = lg.crew_log_root(lg.KIND_CREW)
    outside = root.parent / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    (root / _store_name("escape")).symlink_to(outside, target_is_directory=True)
    with _raises(lg.CODE_INVALID_ID) as exc:
        _crew("escape")
    assert _code(exc) == lg.CODE_INVALID_ID


def test_the_root_is_established_owner_only_before_any_log_exists(monkeypatch, tmp_path):
    # The two other protections are stated per PATH and both are weaker while the
    # name is absent: the Linux bind-mask skips a leaf that does not exist, and an
    # absent directory has no mode to inherit. Establishing the root with the home
    # makes the guarantee a property of the home rather than of the first write.
    home = tmp_path / "fresh-home"
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    paths._config_dir_memo = None
    ensure_data_home()
    root = home / "crew-log"
    assert root.is_dir(), "no crew log has been written, yet the root exists"
    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_an_existing_root_with_a_loose_mode_is_tightened(monkeypatch, tmp_path):
    # mkdir's mode is a no-op on a directory that already exists, so a home
    # restored from a backup keeps whatever mode it arrived with unless startup
    # asserts the mode as well as the creation.
    if os.name != "posix":
        pytest.skip("POSIX mode semantics")
    home = tmp_path / "restored-home"
    (home / "crew-log").mkdir(parents=True)
    # Deliberately group- and world-readable: that is the state a restored backup
    # or an older build leaves behind, and the point is that startup re-asserts
    # owner-only over it rather than accepting what it found.
    (home / "crew-log").chmod(
        stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
    )
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    paths._config_dir_memo = None
    ensure_data_home()
    assert stat.S_IMODE((home / "crew-log").stat().st_mode) == 0o700


# --- sandbox disposition --------------------------------------------------


def test_every_log_is_refused_to_the_agents_own_file_tools():
    # The crew log is the AUTHORITY a conductor reads instead of re-deriving, so an
    # agent able to write here could forge an entry attributed to the gateway or
    # rewrite the history it is reporting into. The write-side rules bind callers
    # who go through the library; this floor is what stops a file tool going
    # around it. BOTH kinds, and the subpath is asked of the library rather than
    # spelled out, so renaming a root cannot leave this asserting a path nothing
    # writes.
    for kind in (lg.KIND_CREW, lg.KIND_SESSION):
        rel = lg.crew_log_path(kind, "anything").relative_to(data_home()).as_posix()
        for home in (".kiro/crew", ".kirocrew"):
            target = f"~/{home}/{rel}"
            assert is_sensitive_path(target), target


def test_every_log_is_masked_from_every_sandboxed_process():
    # One entry at the shared root covers every kind: the file-tool floor above
    # answers the agent's own tools, and this answers a spawned subprocess that
    # calls open() directly -- which no tool gate sees.
    assert "crew-log" in sandbox._CREW_HIDDEN_LEAVES


def test_a_session_log_lives_under_the_masked_leaf_not_the_transcript_root():
    # Why the session half needs its own root. Under `sessions/` a log has
    # NEITHER fence: that root is the flat transcript store and carries no mask
    # entry, so a sandboxed subprocess can forge a session's history whatever the
    # tool gate says. Asserted on the real created path rather than a root's
    # name -- a name check passes just as happily one directory above the masked
    # leaf.
    path = _session().path
    assert (data_home() / "crew-log") in path.parents
    assert (data_home() / "sessions") not in path.parents


# --- segments: the retention shape ---------------------------------------- #


def test_segments_are_found_and_ordered_by_their_first_seq(tmp_path):
    led = _session("seg-order")
    for n in range(1, 13):
        led.append("turn/started", {"turn": n, "actor": "user", "depth": 0}, src="gateway")
    _split_into_segments(lg.KIND_SESSION, "seg-order", 9)
    _split_into_segments(lg.KIND_SESSION, "seg-order", 5)

    names = [p.name for p in lg.segment_paths(lg.KIND_SESSION, "seg-order")]
    assert names == ["log.jsonl", "log.5.jsonl", "log.9.jsonl"], (
        "segments must order by first seq, not by name -- log.9 sorts before " "log.5 as a string"
    )


def test_a_valid_multi_segment_layout_reads_end_to_end(tmp_path):
    led = _session("seg-read")
    for n in range(1, 13):
        led.append("turn/started", {"turn": n, "actor": "user", "depth": 0}, src="gateway")
    _split_into_segments(lg.KIND_SESSION, "seg-read", 9)
    _split_into_segments(lg.KIND_SESSION, "seg-read", 5)

    reader = lg.CrewLog.open(lg.KIND_SESSION, "seg-read")
    seen = list(reader.iter_from(1))
    assert [e.seq for e in seen] == list(range(1, 13))
    assert [e.data["turn"] for e in seen] == list(range(1, 13))


def test_a_linked_kind_root_is_refused_and_named(tmp_path):
    """A symlinked ``crew-log/<kind>`` must not become the containment root.

    Containment resolves its base first and then checks only that the child stays
    under the resolved base -- so with the kind directory itself a link, the
    link's TARGET becomes the root and every log path under it passes while
    living outside the tree whose protections the data home establishes.
    """
    elsewhere = tmp_path / "writable-elsewhere"
    elsewhere.mkdir()
    kind_root = lg.crew_log_root(lg.KIND_SESSION)
    kind_root.parent.mkdir(parents=True, exist_ok=True)
    if kind_root.exists():
        for child in sorted(kind_root.iterdir()):
            child.unlink() if child.is_file() else None
        kind_root.rmdir()
    kind_root.symlink_to(elsewhere, target_is_directory=True)

    with _raises(lg.CODE_BAD_ROOT) as exc:
        lg.crew_log_dir(lg.KIND_SESSION, "link-victim")
    assert _code(exc) == lg.CODE_BAD_ROOT
    assert kind_root.name in str(exc.value)
    # The containment comparison below it would refuse this too, since the link's
    # target resolves elsewhere. What the link check adds is the DIAGNOSIS: an
    # operator reading "resolves to /some/other/path" has to work out why, while
    # "is a symbolic link" names the thing to go look at. So the message is the
    # property under test, not merely the refusal.
    assert "symbolic link" in str(exc.value), "the refusal did not name the link"


def test_a_kind_root_resolving_outside_the_data_home_is_refused(tmp_path):
    """Not only a link ON the kind directory: a linked ANCESTOR is the same escape.

    The check resolves both sides, so a ``crew-log`` directory that is itself a
    link is caught even though ``crew-log/sessions`` under it is a real directory.
    """
    outside = tmp_path / "outside-tree"
    (outside / "sessions").mkdir(parents=True)
    logs = lg.crew_log_root(lg.KIND_SESSION).parent
    if logs.exists():
        for child in sorted(logs.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        logs.rmdir()
    logs.symlink_to(outside, target_is_directory=True)

    with _raises(lg.CODE_BAD_ROOT) as exc:
        lg.crew_log_dir(lg.KIND_SESSION, "ancestor-victim")
    assert _code(exc) == lg.CODE_BAD_ROOT


def test_a_real_kind_root_still_reads_and_writes(tmp_path):
    """The guard is a refusal for a wrong directory, not a new failure mode."""
    led = _session("root-ok")
    led.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    reader = lg.CrewLog.open(lg.KIND_SESSION, "root-ok")
    assert [e.type for e in reader.iter_from()] == ["turn/started"]


def test_a_foreign_segment_is_refused_and_named(tmp_path):
    target = _session("seg-target")
    for n in range(1, 5):
        target.append("turn/started", {"turn": n, "actor": "user", "depth": 0}, src="gateway")

    foreign = _session("seg-foreign")
    for n in range(1, 9):
        foreign.append("turn/started", {"turn": n, "actor": "user", "depth": 0}, src="gateway")
    _split_into_segments(lg.KIND_SESSION, "seg-foreign", 5)
    foreign_segment = lg.crew_log_dir(lg.KIND_SESSION, "seg-foreign") / "log.5.jsonl"
    offending = lg.crew_log_dir(lg.KIND_SESSION, "seg-target") / "log.5.jsonl"
    offending.write_bytes(foreign_segment.read_bytes())

    reader = lg.CrewLog.open(lg.KIND_SESSION, "seg-target")
    with _raises(lg.CODE_BAD_SEGMENT) as exc:
        list(reader.iter_from())
    assert _code(exc) == lg.CODE_BAD_SEGMENT
    assert offending.name in str(exc.value)


def test_a_renamed_segment_is_refused_and_named(tmp_path):
    led = _session("seg-renamed")
    for n in range(1, 9):
        led.append("turn/started", {"turn": n, "actor": "user", "depth": 0}, src="gateway")
    _split_into_segments(lg.KIND_SESSION, "seg-renamed", 5)
    directory = lg.crew_log_dir(lg.KIND_SESSION, "seg-renamed")
    offending = directory / "log.6.jsonl"
    (directory / "log.5.jsonl").rename(offending)

    reader = lg.CrewLog.open(lg.KIND_SESSION, "seg-renamed")
    with _raises(lg.CODE_BAD_SEGMENT) as exc:
        list(reader.iter_from())
    assert _code(exc) == lg.CODE_BAD_SEGMENT
    assert offending.name in str(exc.value)
    assert "first entry is seq 5" in str(exc.value)


def test_dropping_the_oldest_segments_is_retention_not_damage(tmp_path):
    """The whole point of segments: retention deletes files, never rewrites one.

    A reader of a pruned log sees fewer entries and no error, because the lines
    that remain are byte-identical to the ones that were written.
    """
    led = _session("seg-prune")
    for n in range(1, 13):
        led.append("turn/started", {"turn": n, "actor": "user", "depth": 0}, src="gateway")
    _split_into_segments(lg.KIND_SESSION, "seg-prune", 9)
    lg.crew_log_path(lg.KIND_SESSION, "seg-prune").unlink()

    reader = lg.CrewLog.open(lg.KIND_SESSION, "seg-prune")
    seen = list(reader.iter_from(1))
    assert [e.seq for e in seen] == [9, 10, 11, 12]


def test_a_gap_between_segments_is_refused_rather_than_read_across(tmp_path):
    # A hole in the MIDDLE is damage or a partial copy. Yielding across it would
    # hand a fold consecutive entries that are not consecutive facts.
    led = _session("seg-gap")
    for n in range(1, 13):
        led.append("turn/started", {"turn": n, "actor": "user", "depth": 0}, src="gateway")
    # Newest boundary first: each split re-reads the head, so splitting low then
    # high would leave the second segment empty.
    _split_into_segments(lg.KIND_SESSION, "seg-gap", 9)
    _split_into_segments(lg.KIND_SESSION, "seg-gap", 5)
    (lg.crew_log_dir(lg.KIND_SESSION, "seg-gap") / "log.5.jsonl").unlink()

    reader = lg.CrewLog.open(lg.KIND_SESSION, "seg-gap")
    with _raises(lg.CODE_SEGMENT_GAP):
        list(reader.iter_from(1))


def test_a_neighbour_file_sharing_the_prefix_is_ignored_not_refused(tmp_path):
    # An editor backup or a stray copy must not make a readable log unreadable.
    led = _session("seg-neighbour")
    for n in range(1, 5):
        led.append("turn/started", {"turn": n, "actor": "user", "depth": 0}, src="gateway")
    directory = lg.crew_log_dir(lg.KIND_SESSION, "seg-neighbour")
    (directory / "log.backup.jsonl").write_text("not a segment\n")

    assert [p.name for p in lg.segment_paths(lg.KIND_SESSION, "seg-neighbour")] == ["log.jsonl"]
    reader = lg.CrewLog.open(lg.KIND_SESSION, "seg-neighbour")
    assert [e.seq for e in reader.iter_from(1)] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    "stray",
    [
        # isdigit() but not decimal: int() raises on it.
        "log.\u00b2.jsonl",
        # Decimal but not ASCII: int() parses it as 12, so it would pass for a
        # real segment starting at seq 12.
        "log.\u0661\u0662.jsonl",
        # A leading zero: int() parses it as 5, a second "segment" at seq 5.
        "log.05.jsonl",
    ],
)
def test_a_non_ascii_digit_neighbour_is_ignored_not_raised(tmp_path, stray):
    # The segment name's number is ASCII decimal only. Any other digit
    # character makes the file a stray like log.backup.jsonl, never a segment.
    led = _session("seg-unicode")
    for n in range(1, 5):
        led.append("turn/started", {"turn": n, "actor": "user", "depth": 0}, src="gateway")
    directory = lg.crew_log_dir(lg.KIND_SESSION, "seg-unicode")
    (directory / stray).write_text("not a segment\n")

    assert [p.name for p in lg.segment_paths(lg.KIND_SESSION, "seg-unicode")] == ["log.jsonl"]
    reader = lg.CrewLog.open(lg.KIND_SESSION, "seg-unicode")
    assert [e.seq for e in reader.iter_from(1)] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    ("name", "is_history"),
    [
        ("log.jsonl", True),
        # Removal is broader than a reader on purpose: no real segment starts
        # below seq 2, yet a numbered file is still history by shape.
        ("log.0.jsonl", True),
        ("log.1.jsonl", True),
        ("log.12.jsonl", True),
        # A name a reader ignores as a stray is not history either.
        ("log.\u00b2.jsonl", False),
        ("log.\u0661\u0662.jsonl", False),
        ("log.05.jsonl", False),
        ("log.backup.jsonl", False),
    ],
)
def test_removal_orders_history_by_the_readers_number_spelling(name, is_history):
    assert store._is_segment_name(name) is is_history


def test_a_chmod_refusing_filesystem_warns_once_not_once_per_append(monkeypatch, caplog):
    # Every append goes through the private-mkdir helper, so a filesystem that
    # refuses chmod would log a full traceback per entry. That buries the very
    # entries the warning is about, and one true fact about the host does not
    # become truer by being repeated.
    log = _session("flood-check")
    monkeypatch.setattr(store, "_restrict_failed", set())
    # Leave the directory loose so the restriction is actually attempted; when it
    # is already owner-only the helper skips the chmod entirely.
    if os.name == "posix":
        log.path.parent.chmod(0o755)

    def _refuse(directory):
        raise OSError("chmod not supported on this filesystem")

    monkeypatch.setattr(store, "restrict_dir_to_owner", _refuse)
    with caplog.at_level("WARNING", logger=store.__name__):
        for turn in range(1, 6):
            log.append("turn/started", {"turn": turn, "actor": "user", "depth": 0}, src="gateway")

    warnings = [r for r in caplog.records if "owner-only" in r.getMessage()]
    assert len(warnings) == 1, f"{len(warnings)} warnings for 5 appends, expected 1"
    # The appends themselves still succeed: the restriction is best-effort.
    body = _log_bytes("session", "flood-check").decode("utf-8").splitlines()
    assert len([line for line in body if '"turn/started"' in line]) == 5
    # The one warning carries the traceback as TEXT. It is raised inside
    # ``CrewLog.append``, so an ``exc_info`` triple would hold the frame whose
    # ``self`` is this handle -- and the captured record would then keep the
    # handle, and its write lease, alive for the rest of the worker.
    assert "Traceback (most recent call last)" in warnings[0].getMessage()
    assert warnings[0].exc_info is None
    lease_path = str(log.path.parent / lease.LEASE_FILE)
    assert lease_path in lease._held, "the open handle should hold its lease"
    del log
    assert lease_path not in lease._held, "dropping the handle must release the lease at once"


def test_an_already_restricted_directory_is_not_chmodded_again():
    # The mode is checked before it is set, so the steady state costs one stat
    # instead of a syscall that changes nothing on every entry.
    log = _session("skip-check")
    log.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    calls: list[object] = []
    real = store.restrict_dir_to_owner

    def _count(directory):
        calls.append(directory)
        real(directory)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "restrict_dir_to_owner", _count)
        log.append("turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="gateway")

    if os.name == "posix":
        assert calls == [], f"re-chmodded an already owner-only directory: {calls}"


def test_the_log_root_is_restricted_even_when_the_home_cannot_be(tmp_path, monkeypatch):
    # The two tightenings are independent claims. Skipping the crew log root when the
    # home's own chmod fails inverts the priority: that is the case where the root's
    # mode is the ONLY boundary left, because an unreadable parent is not there to
    # do the work -- and every lazily created crew log directory below it would keep
    # the process umask.
    from kiro_crew import platform_compat

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "config_dir", lambda: home)

    real = platform_compat.restrict_dir_to_owner
    refused: list[object] = []

    def _refuse_the_home(directory):
        if directory == home:
            refused.append(directory)
            raise OSError("read-only filesystem")
        real(directory)

    monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", _refuse_the_home)

    got = paths.ensure_data_home()

    assert got == home
    assert refused == [home], "the test did not exercise the failing branch"
    root = home / "crew-log"
    assert root.is_dir(), "the crew log root was skipped because the home's chmod failed"
    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_a_lazily_created_log_directory_is_not_world_readable():
    # The eager root is best-effort, so the per-unit directories the store creates
    # on first write assert the same thing for themselves rather than trusting a
    # parent that may not have been tightened.
    log = _session("hardening-check")
    log.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")

    if os.name == "posix":
        mode = stat.S_IMODE(log.path.parent.stat().st_mode)
        assert mode == 0o700, f"the crew log directory is {oct(mode)}, not owner-only"


def test_a_log_that_exists_but_will_not_open_is_damage_not_absence():
    # `gone` means "there is no such crew log" and tells a reader to stop looking. A
    # crew log whose header is corrupt is right there and broken, so answering
    # `gone` for it converts a recoverable alarm into silence.
    src = _session()
    victim = _session("other-session")
    entry = victim.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    ref = lg.Ref(unit="session", id="other-session", from_seq=entry.seq)
    lines = victim.path.read_text(encoding="utf-8").splitlines()
    lines[0] = '{"broken":'
    victim.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    got = src.resolve(ref)
    assert got.status == lg.STATUS_CORRUPT, f"a damaged header reported {got.status!r}"


def test_a_citation_past_a_stale_cached_tail_is_not_called_ok():
    # A second writer advances the file, so an older handle's cached `last_seq`
    # lags. Clamping the expected count to it computes zero expected lines, which
    # answers `ok` for a citation that cannot actually be read.
    first = _session()
    first.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    stale_tail = first.last_seq

    second = CrewLog.open("session", SESSION)
    newer = second.append("turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="gateway")
    assert newer.seq > stale_tail

    ref = lg.Ref(unit="session", id=SESSION, from_seq=newer.seq)
    got = first.resolve(ref)
    # The line EXISTS, so this must resolve it rather than shrug at it.
    assert got.status == lg.STATUS_OK, f"a readable line reported {got.status!r}"
    assert [e.seq for e in got.entries] == [newer.seq]
