"""The reconciliation in ``e2e.process_inventory``, against synthetic trees.

The harness modules that use this reconciliation need a real gateway, real
processes and a real systemd user scope, so they are unresolved on a host
without one -- which is most hosts, and every stock CI runner. That leaves the
classifier itself untested exactly where it matters: a bug in "which population
does this pid belong to" would make those harnesses report a clean machine for
the wrong reason, and their skip would hide it.

So the classifier is tested here, on injected ``/proc`` and cgroup trees. Both
are plain directory layouts -- ``proc_root`` and ``cgroup_base`` are parameters
for this reason -- so a test can state a machine's exact shape and assert how
each pid is classified, with no processes and no systemd anywhere.

This is the unit layer under the integration harness, not a substitute for it.
It proves the arithmetic of ownership; only a real run proves the product puts
its processes where this reconciliation looks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e.process_inventory import (  # noqa: E402  (path set above)
    AGENTS_SLICE,
    PID_FILE,
    SESSION_PID_FILE,
    STUB_ARGV_TOKEN,
    crew_log_write_handles,
    descendants_of,
    instance_slice_dir,
    instance_slice_token,
    inventory,
    read_argv,
    read_registry,
    slice_pids,
    start_token,
    survivors,
)

#: A ``comm`` with a space and parentheses in it. The real ``/proc/<pid>/stat``
#: allows both, which is why the parser splits after the LAST ``)``; every fake
#: here carries this name so a parser that split on the first one fails.
AWKWARD_COMM = "(weird name) :)"


def _write_proc(proc_root: Path, pid: int, *, ppid: int, starttime: str, argv: str) -> None:
    """Lay down one synthetic ``/proc/<pid>`` entry.

    ``stat`` is built to the real field order so the offsets under test are the
    offsets in production: after the final ``)`` come ``state``, ``ppid``, and
    eighteen more fields before ``starttime``.
    """
    entry = proc_root / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    before = [str(pid), f"({AWKWARD_COMM})"]
    after = ["S", str(ppid)]
    # pgrp through itrealvalue: the seventeen fields that sit between ppid and
    # starttime, so starttime lands where the real file puts it. Cross-checked
    # against a live /proc entry: after the final ")", index 1 is ppid and index
    # 19 is starttime.
    after += ["0"] * 17
    after += [starttime]
    after += ["0"] * 30
    (entry / "stat").write_text(" ".join(before + after), encoding="utf-8")
    (entry / "cmdline").write_bytes(argv.encode("utf-8").replace(b" ", b"\0") + b"\0")


def _slice_for(cgroup_base: Path, home: Path) -> Path:
    """Create and return the instance slice directory for *home*."""
    token = instance_slice_token(home)
    path = (
        cgroup_base
        / "kirocrew.slice"
        / AGENTS_SLICE
        / f"{AGENTS_SLICE[: -len('.slice')]}-{token}.slice"
    )
    path.mkdir(parents=True, exist_ok=True)
    (path / "cgroup.procs").write_text("", encoding="utf-8")
    return path


def _scope(slice_dir: Path, name: str, pids: list[int]) -> Path:
    scope = slice_dir / name
    scope.mkdir(parents=True, exist_ok=True)
    (scope / "cgroup.procs").write_text("".join(f"{pid}\n" for pid in pids), encoding="utf-8")
    return scope


@pytest.fixture()
def machine(tmp_path: Path) -> tuple[Path, Path, Path]:
    """``(home, proc_root, cgroup_base)`` for one synthetic machine."""
    home = tmp_path / "home"
    home.mkdir()
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    cgroup_base = tmp_path / "cgroup"
    cgroup_base.mkdir()
    return home, proc_root, cgroup_base


def test_stat_parsing_survives_a_comm_with_spaces_and_parens(machine) -> None:
    """``start_token`` and ``ppid`` read the right fields despite the process name.

    The control for every other test here: all of them assert on populations
    that are decided by these two readings, so a parser that mis-split would
    make the rest agree with each other and with nothing real.
    """
    _, proc_root, _ = machine
    _write_proc(proc_root, 4242, ppid=99, starttime="76543", argv="python -m thing")
    assert start_token(4242, proc_root=proc_root) == "76543"
    assert read_argv(4242, proc_root=proc_root) == "python -m thing"


def test_a_tracked_live_pid_is_owned_alive(machine) -> None:
    home, proc_root, cgroup_base = machine
    slice_dir = _slice_for(cgroup_base, home)
    _write_proc(proc_root, 100, ppid=1, starttime="500", argv="kiro-cli acp")
    _scope(slice_dir, "run-a.scope", [100])
    (home / SESSION_PID_FILE).write_text("7:100:500\n", encoding="utf-8")

    inv = inventory(home, proc_root=proc_root, cgroup_base=str(cgroup_base))
    assert [fact.pid for fact in inv.owned_alive] == [100]
    assert not inv.owned_dead
    assert not inv.unowned_alive
    assert inv.scopes == 1


def test_a_tracked_pid_that_is_gone_is_owned_dead(machine) -> None:
    """A registry entry naming a vanished process is a stale entry, not a leak."""
    home, proc_root, cgroup_base = machine
    _slice_for(cgroup_base, home)
    (home / SESSION_PID_FILE).write_text("7:404:500\n", encoding="utf-8")

    inv = inventory(home, proc_root=proc_root, cgroup_base=str(cgroup_base))
    assert [fact.pid for fact in inv.owned_dead] == [404]
    assert not inv.owned_alive


def test_a_recycled_pid_counts_as_dead_not_alive(machine) -> None:
    """A live pid whose start identity differs from the record is a stranger.

    Counting it alive would be the dangerous direction: the tracked process is
    gone, and anything that signalled that number would hit an unrelated one.
    """
    home, proc_root, cgroup_base = machine
    slice_dir = _slice_for(cgroup_base, home)
    _write_proc(proc_root, 200, ppid=1, starttime="999", argv="someone else entirely")
    _scope(slice_dir, "run-b.scope", [200])
    (home / SESSION_PID_FILE).write_text("7:200:500\n", encoding="utf-8")

    inv = inventory(home, proc_root=proc_root, cgroup_base=str(cgroup_base))
    assert [fact.pid for fact in inv.owned_dead] == [200]
    assert not inv.owned_alive
    assert "recycled" in inv.owned_dead[0].note


def test_a_slice_pid_in_no_registry_entry_is_unowned(machine) -> None:
    """The leak: alive inside the instance's slice, owned by nothing."""
    home, proc_root, cgroup_base = machine
    slice_dir = _slice_for(cgroup_base, home)
    _write_proc(proc_root, 300, ppid=1, starttime="500", argv="orphaned helper")
    _scope(slice_dir, "run-c.scope", [300])

    inv = inventory(home, proc_root=proc_root, cgroup_base=str(cgroup_base))
    assert [fact.pid for fact in inv.unowned_alive] == [300]
    assert not inv.owned_alive
    assert not inv.owned_dead


def test_a_pid_directly_in_the_slice_is_seen(machine) -> None:
    """A process outside any scope still counts; it is in the slice.

    The scope wrapper is not always available, and a process that landed in the
    slice itself would be invisible to a probe that only walked ``*.scope``.
    """
    home, proc_root, cgroup_base = machine
    slice_dir = _slice_for(cgroup_base, home)
    _write_proc(proc_root, 310, ppid=1, starttime="500", argv="unwrapped spawn")
    (slice_dir / "cgroup.procs").write_text("310\n", encoding="utf-8")

    found = slice_pids(slice_dir)
    assert set(found) == {310}
    inv = inventory(home, proc_root=proc_root, cgroup_base=str(cgroup_base))
    assert [fact.pid for fact in inv.unowned_alive] == [310]


def test_ignored_pids_are_in_no_population(machine) -> None:
    """The observer must not count itself, or the inventory never reads zero."""
    home, proc_root, cgroup_base = machine
    slice_dir = _slice_for(cgroup_base, home)
    _write_proc(proc_root, 400, ppid=1, starttime="500", argv="the gateway itself")
    _write_proc(proc_root, 401, ppid=1, starttime="500", argv="real agent work")
    _scope(slice_dir, "run-d.scope", [400, 401])
    (home / SESSION_PID_FILE).write_text("7:400:500\n", encoding="utf-8")

    inv = inventory(
        home,
        proc_root=proc_root,
        cgroup_base=str(cgroup_base),
        ignore_pids=frozenset({400}),
    )
    assert [fact.pid for fact in inv.owned_alive] == []
    assert [fact.pid for fact in inv.unowned_alive] == [401]


def test_stubs_are_counted_whoever_owns_them(machine) -> None:
    """A stub is identified by argv, so a tracked and an untracked one both count."""
    home, proc_root, cgroup_base = machine
    slice_dir = _slice_for(cgroup_base, home)
    _write_proc(
        proc_root,
        500,
        ppid=1,
        starttime="500",
        argv=f"python -m {STUB_ARGV_TOKEN} --server alpha",
    )
    _write_proc(
        proc_root,
        501,
        ppid=1,
        starttime="500",
        argv=f"python -m {STUB_ARGV_TOKEN} --server beta",
    )
    _write_proc(proc_root, 502, ppid=1, starttime="500", argv="not a stub at all")
    _scope(slice_dir, "run-e.scope", [500, 501, 502])
    (home / SESSION_PID_FILE).write_text("7:500:500\n", encoding="utf-8")

    inv = inventory(home, proc_root=proc_root, cgroup_base=str(cgroup_base))
    assert set(inv.stub_pids) == {500, 501}
    assert set(inv.live_pids) == {500, 501, 502}


def test_both_registry_files_are_read(machine) -> None:
    """Roots come from the session file, descendants from the other one.

    A reconciliation that read only one file would classify every descendant as
    unowned and report a leak on a healthy machine.
    """
    home, proc_root, cgroup_base = machine
    slice_dir = _slice_for(cgroup_base, home)
    _write_proc(proc_root, 600, ppid=1, starttime="500", argv="root")
    _write_proc(proc_root, 601, ppid=600, starttime="501", argv="child")
    _write_proc(proc_root, 602, ppid=600, starttime="502", argv="bare-tracked child")
    _scope(slice_dir, "run-f.scope", [600, 601, 602])
    (home / SESSION_PID_FILE).write_text("7:600:500\n", encoding="utf-8")
    # The descendant form, plus the legacy bare-pid form.
    (home / PID_FILE).write_text("601:600:501\n602\n", encoding="utf-8")

    inv = inventory(home, proc_root=proc_root, cgroup_base=str(cgroup_base))
    assert {fact.pid for fact in inv.owned_alive} == {600, 601, 602}
    assert not inv.unowned_alive


def test_a_torn_registry_line_is_skipped_not_raised(machine) -> None:
    """A live gateway appends while this reads, so a half-written line is normal."""
    home, proc_root, cgroup_base = machine
    _slice_for(cgroup_base, home)
    (home / SESSION_PID_FILE).write_text("7:700:500\nnot-a-line\n7:\n:::\n", encoding="utf-8")
    _write_proc(proc_root, 700, ppid=1, starttime="500", argv="fine")

    entries = read_registry(home)
    assert [entry.pid for entry in entries] == [700]


def test_an_absent_slice_directory_is_reported_not_guessed(machine) -> None:
    """No slice means nowhere to look, which must be visible rather than read as clean."""
    home, proc_root, cgroup_base = machine
    inv = inventory(home, proc_root=proc_root, cgroup_base=str(cgroup_base))
    assert inv.slice_dir is None
    assert instance_slice_dir(home, cgroup_base=str(cgroup_base)) is None
    assert any("no agent slice directory" in note for note in inv.notes)


def test_render_names_the_offending_processes(machine) -> None:
    """A failing assertion has to say WHICH processes survived, not just how many."""
    home, proc_root, cgroup_base = machine
    slice_dir = _slice_for(cgroup_base, home)
    _write_proc(proc_root, 800, ppid=1, starttime="500", argv="the-guilty-process --flag")
    _scope(slice_dir, "run-g.scope", [800])

    text = inventory(home, proc_root=proc_root, cgroup_base=str(cgroup_base)).render()
    assert "the-guilty-process" in text
    assert "800" in text
    assert "run-g.scope" in text
    assert "unowned_alive=1" in text


def test_descendants_of_finds_kin_and_ignores_reparented(machine) -> None:
    """A chain reaching ``1`` before the root means the process was reparented."""
    _, proc_root, _ = machine
    _write_proc(proc_root, 900, ppid=1, starttime="500", argv="root")
    _write_proc(proc_root, 901, ppid=900, starttime="500", argv="child")
    _write_proc(proc_root, 902, ppid=901, starttime="500", argv="grandchild")
    _write_proc(proc_root, 903, ppid=1, starttime="500", argv="reparented stranger")

    kin = descendants_of(900, {901, 902, 903}, proc_root=proc_root)
    assert kin == {901, 902}


def test_descendants_of_terminates_on_a_ppid_cycle(machine) -> None:
    """A torn read can name a cycle; the walk must stop rather than spin."""
    _, proc_root, _ = machine
    _write_proc(proc_root, 910, ppid=911, starttime="500", argv="a")
    _write_proc(proc_root, 911, ppid=910, starttime="500", argv="b")

    assert descendants_of(999, {910, 911}, proc_root=proc_root) == set()


def test_survivors_reports_only_live_pids(machine) -> None:
    _, proc_root, _ = machine
    _write_proc(proc_root, 920, ppid=1, starttime="500", argv="alive")
    assert survivors({920, 921}, proc_root=proc_root) == {920}


def test_crew_log_handle_probe_matches_on_the_path(tmp_path: Path) -> None:
    """The fd probe names a crew-log target and ignores an unrelated one.

    Uses a synthetic ``/proc/<pid>/fd`` of real symlinks, so it runs on any host
    that has symlinks rather than needing a process to hold a file open. The
    integration harness holds a real handle; this pins which paths match.
    """
    fd_dir = tmp_path / "proc" / "55" / "fd"
    fd_dir.mkdir(parents=True)
    wanted = tmp_path / "home" / "crew-log" / "0001.jsonl"
    wanted.parent.mkdir(parents=True)
    wanted.write_text("", encoding="utf-8")
    other = tmp_path / "home" / "sessions" / "notes.jsonl"
    other.parent.mkdir(parents=True)
    other.write_text("", encoding="utf-8")
    (fd_dir / "3").symlink_to(wanted)
    (fd_dir / "4").symlink_to(other)

    found = crew_log_write_handles(55, proc_root=tmp_path / "proc")
    assert len(found) == 1, found
    assert "0001.jsonl" in found[0]
    assert found[0].startswith("3 -> ")


def test_the_slice_token_is_keyed_on_the_resolved_home(tmp_path: Path) -> None:
    """A symlinked path and its target name the same instance.

    The product keys the token on the RESOLVED data home, so a harness that
    hashed the unresolved string would look in a slice nothing writes to
    whenever the home is reached through a symlink.
    """
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "link-home"
    link.symlink_to(real, target_is_directory=True)
    assert instance_slice_token(link) == instance_slice_token(real)


def test_a_failed_slice_stop_raises_rather_than_returning_a_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop that did not happen must fail loudly; the residue is permanent.

    systemd keeps a slice loaded until someone stops it and nothing retries, so
    a cleanup that reports failure by returning a string leaves one unit behind
    per occurrence with no self-correction.

    ``posix_spawn`` is SIMULATED with ``raising=False`` rather than the test being
    skipped off POSIX. Skipping would leave the refusal path unexercised on the
    platform that actually lacks the attribute -- the one place it can regress
    while the shard stays green.
    """
    import e2e.process_inventory as inv_mod

    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setattr(inv_mod.os, "posix_spawn", lambda *a, **k: 4321, raising=False)
    monkeypatch.setattr(inv_mod.shutil, "which", lambda _name: None)
    with pytest.raises(inv_mod.SliceCleanupError, match="systemctl is not on PATH"):
        inv_mod.stop_instance_slice(home)

    monkeypatch.setattr(inv_mod.shutil, "which", lambda _name: "/bin/systemctl")
    # A non-zero exit status is an ordinary teardown failure, not an impossibility.
    monkeypatch.setattr(inv_mod.os, "waitpid", lambda *a, **k: (4321, 1 << 8), raising=False)
    with pytest.raises(inv_mod.SliceCleanupError, match="did not succeed"):
        inv_mod.stop_instance_slice(home)

    monkeypatch.setattr(inv_mod.os, "waitpid", lambda *a, **k: (4321, 0), raising=False)
    assert "stopped" in inv_mod.stop_instance_slice(home)


def test_cleanup_refuses_where_posix_spawn_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``os.posix_spawn`` the cleanup refuses by its own error type.

    An ``AttributeError`` escaping here would be an unhandled crash in a teardown
    path rather than the named refusal the caller can report. Simulated by
    deleting the attribute, so this runs on POSIX too instead of only where it is
    genuinely missing.
    """
    import e2e.process_inventory as inv_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delattr(inv_mod.os, "posix_spawn", raising=False)
    with pytest.raises(inv_mod.SliceCleanupError, match="posix_spawn is unavailable"):
        inv_mod.stop_instance_slice(home)


def test_a_spawn_failure_during_cleanup_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import e2e.process_inventory as inv_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(inv_mod.shutil, "which", lambda _name: "/bin/systemctl")

    def _boom(*_a, **_k):
        raise OSError("no fork for you")

    monkeypatch.setattr(inv_mod.os, "posix_spawn", _boom, raising=False)
    with pytest.raises(inv_mod.SliceCleanupError, match="could not stop"):
        inv_mod.stop_instance_slice(home)


def test_the_sampler_default_home_is_not_the_legacy_path() -> None:
    """The sampler must not default to the pre-move top-level home.

    ``~/.kirocrew`` is the LEGACY home; the product resolves its data home under
    kiro-cli's base. A sampler defaulting to the legacy path would report an
    empty machine forever on a stock run, which is the false-clean this whole
    change exists to prevent.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import soak_process_sampler

    from kiro_crew.config.paths import LEGACY_CONFIG_DIR_NAME, peek_data_home

    resolved = soak_process_sampler.default_home()
    assert resolved is not None
    assert resolved == Path(peek_data_home())
    assert resolved.name != LEGACY_CONFIG_DIR_NAME

    # And the parser itself carries no baked-in home, so the resolution above is
    # the only thing that decides it.
    assert soak_process_sampler.parse_args([]).home is None


def test_the_sampler_refuses_a_home_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refusing beats sampling: an absent home would report all-zero populations.

    The platform refusal sits AHEAD of this guard and returns the same code, so a
    Windows shard would satisfy the exit assertion while never reaching the home
    check. Claim the platform so the guard under test is the one that answers,
    and the assertion means the same thing on every shard.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import soak_process_sampler

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    missing = tmp_path / "not-there"
    assert soak_process_sampler.main(["--home", str(missing), "--once"]) == 2
    err = capsys.readouterr().err
    assert "does not exist" in err
    assert "needs cgroup v2" not in err


def test_the_sampler_refuses_by_name_off_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other direction of the same guard, run on every platform.

    Everything sampled is Linux-only, so the refusal must name the host rather
    than crash part-way through a first sample. The home here EXISTS, so only the
    platform guard can produce the refusal.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import soak_process_sampler

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    assert soak_process_sampler.main(["--home", str(tmp_path), "--once"]) == 2
    err = capsys.readouterr().err
    assert "needs cgroup v2 and /proc" in err
    assert "win32" in err


def test_the_classifier_reads_an_explicit_cgroup_base_on_any_platform(machine) -> None:
    """An injected tree is honoured even where the real hierarchy cannot exist.

    The platform guard belongs to DISCOVERING the base. Applying it to an
    explicit one would make the reconciliation testable on Linux only, and so
    untested on exactly the hosts where the harness above it is unresolved.
    """
    home, proc_root, cgroup_base = machine
    slice_dir = _slice_for(cgroup_base, home)
    _write_proc(proc_root, 1000, ppid=1, starttime="500", argv="agent work")
    _scope(slice_dir, "run-h.scope", [1000])

    inv = inventory(home, proc_root=proc_root, cgroup_base=str(cgroup_base))
    assert inv.slice_dir is not None
    assert [fact.pid for fact in inv.unowned_alive] == [1000]


def test_stub_and_live_counts_exclude_a_tracked_pid_outside_the_slice(machine) -> None:
    """A tracked process that is not a slice member is reported, but not counted in.

    It is real and it appears in ``owned_alive``, but attributing it to this
    slice's stub tally or RSS sum would charge another cgroup's weight here.
    """
    home, proc_root, cgroup_base = machine
    slice_dir = _slice_for(cgroup_base, home)
    _write_proc(
        proc_root,
        1100,
        ppid=1,
        starttime="500",
        argv=f"python -m {STUB_ARGV_TOKEN} --server inside",
    )
    _write_proc(
        proc_root,
        1101,
        ppid=1,
        starttime="500",
        argv=f"python -m {STUB_ARGV_TOKEN} --server outside",
    )
    # Only the first is placed in the slice; both are tracked.
    _scope(slice_dir, "run-i.scope", [1100])
    (home / SESSION_PID_FILE).write_text("7:1100:500\n7:1101:500\n", encoding="utf-8")

    inv = inventory(home, proc_root=proc_root, cgroup_base=str(cgroup_base))
    assert {fact.pid for fact in inv.owned_alive} == {1100, 1101}
    assert set(inv.live_pids) == {1100}
    assert set(inv.stub_pids) == {1100}
    assert [fact.pid for fact in inv.tracked_outside_slice] == [1101]
    assert inv.counts()["stubs"] == 1


def test_the_fd_probe_splits_a_windows_style_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backslash-separated target still yields components.

    The value comes from the host's own ``readlink``, so a Windows reader returns
    backslashes. Splitting through a posix path flavour would see the whole string
    as a single component and match nothing, which reads as "no handle held".
    """
    import e2e.process_inventory as inv_mod

    fd_dir = tmp_path / "proc" / "77" / "fd"
    fd_dir.mkdir(parents=True)
    # A plain file: readlink is stubbed below, so no real link is needed and
    # none is created -- a host without symlink privilege still runs this.
    (fd_dir / "3").write_text("", encoding="utf-8")

    target = r"C:\Users\someone\.kiro\crew\crew-log\0007.jsonl"
    monkeypatch.setattr(inv_mod.os, "readlink", lambda _p: target)
    found = inv_mod.crew_log_write_handles(77, proc_root=tmp_path / "proc")
    assert len(found) == 1, found
    assert "0007.jsonl" in found[0]


def test_a_deleted_target_still_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unlinked log file is the case most worth reporting, not one to drop."""
    import e2e.process_inventory as inv_mod

    fd_dir = tmp_path / "proc" / "78" / "fd"
    fd_dir.mkdir(parents=True)
    (fd_dir / "4").write_text("", encoding="utf-8")

    monkeypatch.setattr(
        inv_mod.os, "readlink", lambda _p: "/home/u/.kiro/crew/crew-log/9.jsonl (deleted)"
    )
    found = inv_mod.crew_log_write_handles(78, proc_root=tmp_path / "proc")
    assert len(found) == 1, found
    assert "(deleted)" in found[0]


def test_the_sampler_does_not_count_a_recycled_runtime_root(
    machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stranger that inherited a recorded pid is not a live runtime.

    Counting it would make a soak series report runtimes that do not exist, which
    is the direction that hides a leak: the count looks healthy while the real
    population drifts.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import e2e.process_inventory as inv_mod
    import soak_process_sampler

    home, proc_root, _ = machine
    _write_proc(proc_root, 1200, ppid=1, starttime="500", argv="the real runtime")
    _write_proc(proc_root, 1201, ppid=1, starttime="999", argv="a stranger")
    (home / SESSION_PID_FILE).write_text("7:1200:500\n7:1201:500\n", encoding="utf-8")

    # Both helpers read /proc through the module's defaults, so point those at
    # the synthetic tree for the duration.
    monkeypatch.setattr(
        inv_mod, "pid_alive", lambda pid, proc_root=proc_root: (proc_root / str(pid)).is_dir()
    )
    monkeypatch.setattr(
        inv_mod, "start_token", lambda pid, proc_root=proc_root: _synthetic_token(proc_root, pid)
    )
    monkeypatch.setattr(soak_process_sampler, "pid_alive", inv_mod.pid_alive)
    monkeypatch.setattr(soak_process_sampler, "start_token", inv_mod.start_token)

    assert soak_process_sampler.runtime_roots(home) == 1


def _synthetic_token(proc_root: Path, pid: int) -> str | None:
    raw_path = proc_root / str(pid) / "stat"
    try:
        raw = raw_path.read_text(encoding="utf-8")
    except OSError:
        return None
    return raw[raw.rindex(")") + 1 :].split()[19]
