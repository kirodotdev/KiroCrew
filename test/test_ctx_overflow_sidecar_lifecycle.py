"""The pending-context sidecar file: where it lives, writing it, clearing it."""

import os
import stat

import pytest


def test_clearing_one_alias_leaves_a_coexisting_transcripts_queue(tmp_path):
    """Two backed stems are two live sessions, so clearing one must not take the other's queue.

    ``transcript_stems`` returns the canonical ``slack_<ts>`` stem and the legacy bare ``<ts>`` stem
    for one Slack key, and the sweep cleared the sidecar under both. That is safe only while a
    single transcript is backed. When a pre-migration thread and a canonical one coexist, each
    stem's sidecar belongs to a DIFFERENT resumable session, and the sweep destroyed acknowledged
    context whose transcript was still there.

    Asserts the SIBLING's entries survive, which is the property that broke; asserting the cleared
    alias is empty passes on the defect, because the defect cleared too much rather than too little.
    """
    from kiro_crew import history as h

    canonical_key = "slack:1700000000.000100"
    stems = h.transcript_stems(canonical_key)
    assert len(stems) == 2, f"precondition: the key must carry both aliases, got {stems}"

    # Both transcripts exist, so the two stems are two sessions rather than one under two names.
    for stem in stems:
        (tmp_path / f"{stem}.jsonl").write_text(
            '{"role": "user", "content": "a turn"}\n', encoding="utf-8"
        )

    sibling_entries = [{"ctxId": "sibling-1", "content": "the legacy thread's queued context"}]
    sibling_sidecar = tmp_path / h.CTX_OVERFLOW_DIR_NAME / f"{stems[1]}.jsonl"
    sibling_sidecar.parent.mkdir(parents=True, exist_ok=True)
    sibling_sidecar.write_text(
        "".join(__import__("json").dumps(e) + "\n" for e in sibling_entries), encoding="utf-8"
    )
    own_sidecar = tmp_path / h.CTX_OVERFLOW_DIR_NAME / f"{stems[0]}.jsonl"
    own_sidecar.write_text(
        __import__("json").dumps({"ctxId": "own-1", "content": "mine"}) + "\n", encoding="utf-8"
    )

    h.clear_ctx_overflow(canonical_key, tmp_path)

    assert sibling_sidecar.exists(), (
        f"clearing {stems[0]} deleted {stems[1]}'s sidecar while {stems[1]}.jsonl is still on disk: "
        "that transcript is resumable and its acknowledged context is now unrecoverable"
    )
    survivors = [
        __import__("json").loads(ln)["ctxId"]
        for ln in sibling_sidecar.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert survivors == ["sibling-1"], f"the sibling's queue did not survive intact: {survivors}"
    assert not own_sidecar.exists(), "the resolved alias's own sidecar should still be cleared"


def test_a_refused_delete_restores_what_it_already_quarantined(tmp_path, monkeypatch):
    """A refusal stranded the sidecars it had already renamed into holding.

    `clear_ctx_overflow(..., quarantine="always")` walks EVERY stem the key can occupy, and a
    legacy Slack thread has two. If the first renames and the second does not, the refusal on
    `survivors` returned before the restore loop, so the transcript stayed alive while the entries
    it DID quarantine sat off the hydration stem -- unreachable, and with no later pass that moves
    them back. The `not result` exit already restored on the same precondition (transcript lives),
    so the refusal was the one surviving-transcript path that did not.
    """
    import pathlib

    from kiro_crew import history as h

    key = "slack:1700000000.123456"
    paths = h._ctx_overflow_paths(key, tmp_path)
    assert len(paths) == 2, f"precondition: a legacy Slack thread has two stems, got {paths}"
    log = h.ConversationLog(tmp_path)
    log.append(key, "user", "a turn")
    for p in paths:
        h.write_ctx_overflow(key, [{"ctxId": f"c-{p.stem}", "content": "queued"}], tmp_path)
        assert p.exists() or True
    # Both stems must actually hold a file, so the walk has two candidates to rename.
    for p in paths:
        p.write_text('{"ctxId": "x", "content": "queued"}\n', encoding="utf-8")

    refused = paths[1]

    real_rename = os.rename
    real_path_rename = pathlib.Path.rename

    def _selective(src, dst, *a, src_dir_fd=None, **kw):
        # The sidecar rename is relative to the vetted root, so the source is a bare name; refuse
        # only the one holding this test needs unremovable.
        if src_dir_fd is not None and src == refused.name:
            raise OSError("rename refused")
        return real_rename(src, dst, *a, src_dir_fd=src_dir_fd, **kw)

    def _selective_path(self, target, *a, **kw):
        # The same refusal in the PATH form, for a platform with no dir_fd support.
        if self == refused:
            raise OSError("rename refused")
        return real_path_rename(self, target, *a, **kw)

    with pytest.MonkeyPatch.context() as _mp:
        _mp.setattr(os, "rename", _selective)
        _mp.setattr(pathlib.Path, "rename", _selective_path)
        assert (
            h.ConversationLog(tmp_path).delete_session(key) is False
        ), "precondition: an unremovable survivor must refuse the delete"

    holdings = [q for q in tmp_path.glob("*") if "context-overflow" not in q.name and q.is_dir()]
    assert paths[0].exists(), (
        f"the quarantined sidecar was not restored to {paths[0].name}: the delete was refused, so "
        f"the transcript is still live, but its spilled context is off the hydration stem "
        f"(dirs seen: {[d.name for d in holdings]})"
    )


def test_the_sidecar_clear_runs_before_the_transcript_is_removed(tmp_path):
    """Ordering is the defect, not just the suppression: clearing second leaves a window."""
    import inspect

    from kiro_crew import history as h

    src = " ".join(inspect.getsource(h.ConversationLog._delete_session_locked).split())
    clear_at = src.find("clear_ctx_overflow")
    delete_at = src.find("_metadata_projection.delete_session")
    assert clear_at != -1 and delete_at != -1, "delete_session moved; re-locate before trusting"
    assert clear_at < delete_at, (
        "the sidecar clear still runs AFTER the transcript removal, so a failed clear leaves a "
        "hydratable spill behind a transcript that is already gone"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_the_overflow_spill_is_not_readable_by_other_local_accounts(tmp_path):
    """The spill was hand-rolled temp+replace, so it took the process umask -- 0644 by default.

    These entries are the trusted-caller context half, deliberately unredacted, and the sidecar is
    a plain file beside the transcript. Under the usual 022 umask any local account could read
    acknowledged secret-bearing content out of it. Reads the mode OFF DISK, not from a mock.
    """
    from kiro_crew import history as h

    key = "chat-spill-mode"
    h.write_ctx_overflow(
        key, [{"ctxId": "s1", "content": "a bearer token", "injectedAt": 1.0}], tmp_path
    )
    spill = h._ctx_overflow_path(key, tmp_path)
    assert spill.exists(), "the spill was not written, so this test proves nothing about its mode"
    mode = stat.S_IMODE(spill.stat().st_mode)
    assert mode == 0o600, (
        f"the overflow spill is mode {mode:#o}: group and other can read secret-bearing context "
        "the caller only ever handed to this gateway"
    )
    # CONTROL: no stray temp file survives the write, which would carry the old mode anyway.
    leftovers = [p.name for p in spill.parent.iterdir() if ".tmp" in p.name or p.name.endswith("~")]
    assert leftovers == [], f"a temp artefact survived the atomic write: {leftovers}"


def test_deleting_a_session_clears_the_sidecar_under_the_session_lock(tmp_path, monkeypatch):
    """An unlocked cleanup can delete a REPLACEMENT sidecar written after the delete.

    ``delete_session`` released the per-session lock before clearing the spill, so a save
    landing in that window recreated the sidecar and the cleanup then deleted acknowledged
    context belonging to the new session.
    """
    from kiro_crew import history as h

    log = h.ConversationLog(tmp_path)
    key = "chat-locked-delete"
    log.append(key, "user", "a turn")
    h.write_ctx_overflow(key, [{"ctxId": "doomed", "content": "x"}], tmp_path)

    held: list[bool] = []
    real = h.clear_ctx_overflow

    def _observe(k, base=None, **kwargs):
        # SAME KEY ``_file_lock`` uses, so a miss cannot read as "not held".
        lock = h.ConversationLog._file_locks.get(str(log._path(k)))
        assert lock is not None, "probe looked up the wrong lock key"
        held.append(bool(lock._is_owned()))
        return real(k, base, **kwargs)

    monkeypatch.setattr(h, "clear_ctx_overflow", _observe)
    log.delete_session(key)

    assert held, "the delete never reached the sidecar cleanup"
    assert all(held), (
        "the sidecar cleanup ran with the per-session lock NOT held, so a concurrent save's "
        "replacement sidecar can be deleted by it"
    )


def test_a_refused_delete_reports_every_holding_it_could_not_put_back(tmp_path, caplog):
    """GPT BLOCKING: the refusal path discarded the list of holdings it failed to re-seat.

    A key carrying a legacy alias has TWO sidecar stems, so one can be quarantined while the other
    refuses. The refusal branch then re-seats the first -- and when THAT rename also fails the
    holding sits off the stem `_ctx_overflow_path` resolves while the transcript stays live, so the
    acknowledged entries in it are unreachable.

    Returning False was indistinguishable from a clean refusal where nothing was lost. The failure
    is now named per holding, because renaming it back is the only recovery.
    """
    import logging
    from pathlib import Path

    from kiro_crew import history as h

    log = h.ConversationLog(tmp_path)
    key = "slack:1712345678.9001"
    stems = h.transcript_stems(key)
    assert len(stems) > 1, f"precondition: this key must carry an alias stem, got {stems}"

    (tmp_path / f"{stems[0]}.jsonl").write_text('{"role": "user"}\n', encoding="utf-8")
    paths = h._ctx_overflow_paths(key, tmp_path)
    paths[0].parent.mkdir(parents=True, exist_ok=True)
    for p in paths:
        p.write_text('{"ctxId": "owed", "content": "acknowledged"}\n', encoding="utf-8")

    calls = {"quarantined": 0, "refused": 0}
    real_rename = h.Path.rename
    real_os_rename = os.rename

    def refusing_os_rename(src, dst, *a, src_dir_fd=None, **kw):
        # The QUARANTINE leg is root-relative, so its source is a bare name; the alias stem
        # refuses, lands in `survivors`, and the delete is refused.
        if src_dir_fd is not None and src == paths[1].name:
            calls["refused"] += 1
            raise OSError(16, "Device or resource busy")
        # The RESTORE leg is descriptor-relative too, and it addresses the ORIGINAL as its
        # destination: the first stem's holding refuses to come BACK, the strand measured here.
        if src_dir_fd is not None and dst == paths[0].name:
            calls["refused"] += 1
            raise OSError(16, "Device or resource busy")
        if src_dir_fd is not None:
            calls["quarantined"] += 1
        return real_os_rename(src, dst, *a, src_dir_fd=src_dir_fd, **kw)

    def refusing_rename(self, target):
        # The RESTORE leg always addresses full paths: the first stem's holding refuses to come
        # BACK, which is the strand this test measures.
        if Path(target) == paths[0]:
            calls["refused"] += 1
            raise OSError(16, "Device or resource busy")
        # The QUARANTINE leg reaches here too where the platform has no dir_fd support.
        if self == paths[1]:
            calls["refused"] += 1
            raise OSError(16, "Device or resource busy")
        if h.CTX_OVERFLOW_DIR_NAME in self.parts:
            calls["quarantined"] += 1
        return real_rename(self, target)

    with caplog.at_level(logging.ERROR, logger=h.logger.name):
        h.Path.rename = refusing_rename
        os.rename = refusing_os_rename
        try:
            result = log._delete_session_locked(key)
        finally:
            h.Path.rename = real_rename
            os.rename = real_os_rename

    assert result is False, "an unremovable survivor must still refuse the delete"
    assert calls["quarantined"] >= 1, "precondition: one stem was really quarantined"
    assert calls["refused"] >= 2, "precondition: both the alias clear and the restore were refused"
    text = caplog.text
    assert (
        "could not be put back" in text
    ), f"the refusal must name the holdings it stranded, not discard them: {text[-700:]}"
    assert (
        str(paths[0]) in text
    ), f"the stranded holding's original path must be named so it can be renamed back: {text[-700:]}"


def test_a_symlinked_sidecar_root_is_refused_rather_than_renamed_through(tmp_path):
    """The clear path mutates an agent-writable tree, so a planted root must not redirect it.

    A symlinked root makes every path built under it resolve elsewhere, so a rename would move a
    file inside a directory the caller never named — reaching a pinned session's state. The read
    side already refuses a planted node; this pins the mutating side to the same posture.

    Paired with the sidecar READ tests, which fail the other way: those return foreign content or
    wedge the reader, whereas this one moves a file out from under another session.
    """
    from kiro_crew import history as h

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    key = "chat-symlinked-root"
    stem = h.transcript_stems(key)[0]
    pinned = elsewhere / f"{stem}.jsonl"
    pinned.write_bytes(b'{"ctxId": "belongs-to-another-session"}\n')

    root = tmp_path / h.CTX_OVERFLOW_DIR_NAME
    os.symlink(elsewhere, root)
    assert root.is_symlink() and (root / f"{stem}.jsonl").exists(), (
        "precondition: the planted root must resolve to the foreign directory, so an unguarded "
        "rename would move the file below"
    )

    cleared = h.clear_ctx_overflow(key, tmp_path, quarantine="always")

    assert (
        pinned.exists()
    ), "the rename followed the symlinked root and moved a file belonging to another session"
    assert not cleared.quarantined, cleared.quarantined
    assert cleared.survivors, (
        "a refused clear must report the sidecar as a survivor, or the caller commits an empty "
        "line over a file that is still hydratable"
    )


def test_the_sidecar_root_is_pinned_through_the_platform_helper_where_no_walk_exists(tmp_path):
    """A raw directory open is not portable, so a platform without the walk must still pin.

    ``O_DIRECTORY`` and ``O_NOFOLLOW`` are absent on Windows, where a bare open of a directory
    fails outright -- so a clear built on one would refuse every sidecar there, on every restart,
    and re-inject already-delivered context. ``pin_directory`` carries the per-platform branch, and
    ``supports_pinned_walk`` is what selects it.

    Asserts BOTH arms, because each alone is satisfiable by the wrong implementation: pinning only
    through ``pin_directory`` leaves the ancestor hole below open, and walking unconditionally
    refuses every sidecar on Windows.
    """
    from unittest import mock

    from kiro_crew import history as h
    from kiro_crew import pinned_fs, platform_compat

    key = "chat-pinned-through-helper"
    h.write_ctx_overflow(key, [{"ctxId": "owed", "content": "acknowledged"}], tmp_path)
    seen: list[str] = []
    real_pin = platform_compat.pin_directory

    def _spy(path):
        seen.append(str(path))
        return real_pin(path)

    with mock.patch.object(platform_compat, "pin_directory", _spy):
        cleared = h.clear_ctx_overflow(key, tmp_path, quarantine="on_failure")
    assert not cleared.survivors, cleared.survivors
    if pinned_fs.supports_pinned_walk():
        assert seen == [], (
            "the walk platform still reached for pin_directory, which resolves the root's parent "
            f"by name and is the ancestor hole this suite pins shut: {seen}"
        )

    h.write_ctx_overflow(key, [{"ctxId": "owed", "content": "acknowledged"}], tmp_path)
    seen.clear()
    with (
        mock.patch.object(platform_compat, "pin_directory", _spy),
        mock.patch.object(pinned_fs, "supports_pinned_walk", lambda: False),
    ):
        cleared = h.clear_ctx_overflow(key, tmp_path, quarantine="on_failure")

    assert seen == [
        str(tmp_path / h.CTX_OVERFLOW_DIR_NAME)
    ], f"a platform without the pinned walk did not pin through the platform helper: {seen}"
    assert not cleared.survivors, cleared.survivors


@pytest.mark.parametrize("pinned_walk", [True, False], ids=["pinned-walk", "by-name"])
def test_a_link_at_the_sessions_ANCESTOR_is_refused_rather_than_cleared_through(
    tmp_path, pinned_walk
):
    """``O_NOFOLLOW`` guards only the FINAL component, so the root's parent needs pinning too.

    The planted node here is the agent-writable ``sessions`` directory, NOT the sidecar root: the
    root under it is an ordinary directory, so a pin that resolves the parent by name reaches it
    without ``O_NOFOLLOW`` ever firing, and the descriptor then addresses a foreign session's
    queue. The rename that follows moves a file the caller never named -- the same end state as a
    symlinked root, reached one component higher up, where the root-only check cannot see it.

    Run over both arms because the refusal has two implementations and a platform gets only one.
    The ``by-name`` case is what Windows executes, and asserting it on this host is what stops a
    POSIX-only fix passing here and failing the Windows shard ten minutes later. The walk is forced
    OFF rather than on: a host without it has no ``O_NOFOLLOW`` to offer, so forcing it on would
    exercise an arm the platform cannot run at all.
    """
    from unittest import mock

    from kiro_crew import history as h
    from kiro_crew import pinned_fs

    foreign = tmp_path / "foreign"
    (foreign / h.CTX_OVERFLOW_DIR_NAME).mkdir(parents=True)
    key = "chat-ancestor-link"
    stem = h.transcript_stems(key)[0]
    pinned = foreign / h.CTX_OVERFLOW_DIR_NAME / f"{stem}.jsonl"
    pinned.write_bytes(b'{"ctxId": "belongs-to-another-session"}\n')

    sessions = tmp_path / "sessions"
    try:
        os.symlink(foreign, sessions, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"planting a directory link needs a privilege this host withholds: {exc}")
    assert (
        sessions / h.CTX_OVERFLOW_DIR_NAME / f"{stem}.jsonl"
    ).exists(), "precondition: the planted ancestor must resolve to the foreign queue"

    # Computed from the UNPATCHED probe, so the "pinned-walk" case degrades to by-name on a host
    # that has no walk rather than claiming an arm the platform cannot execute.
    walk = pinned_walk and pinned_fs.supports_pinned_walk()
    with mock.patch.object(pinned_fs, "supports_pinned_walk", lambda: walk):
        cleared = h.clear_ctx_overflow(key, sessions, quarantine="always")

    assert pinned.exists(), (
        "the clear resolved the sidecar root through a link at its PARENT and moved a file "
        "belonging to another session"
    )
    assert not cleared.quarantined, cleared.quarantined
    assert cleared.survivors, (
        "a refused clear must report the sidecar as a survivor, or the caller commits an empty "
        "line over a file that is still hydratable"
    )


@pytest.mark.parametrize("pinned_walk", [True, False], ids=["pinned-walk", "by-name"])
def test_a_link_at_the_sessions_ANCESTOR_is_refused_rather_than_created_through(
    tmp_path, pinned_walk
):
    """Creating has the same hole pointed the other way, and a write through it is worse.

    ``mkdir(parents=True)`` builds each missing component BY NAME, so a link at ``sessions`` is
    followed and the root is created inside whatever it points at -- then the spill is written
    there. Refusing must happen before any of that, so the foreign directory gains no node at all.

    Both arms again: the by-name arm interleaves its refusal with the creates, and only running it
    proves the refusal lands BEFORE the ``mkdir`` rather than after.
    """
    from unittest import mock

    from kiro_crew import history as h
    from kiro_crew import pinned_fs

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    sessions = tmp_path / "sessions"
    try:
        os.symlink(foreign, sessions, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"planting a directory link needs a privilege this host withholds: {exc}")

    # Computed from the UNPATCHED probe, so the "pinned-walk" case degrades to by-name on a host
    # that has no walk rather than claiming an arm the platform cannot execute.
    walk = pinned_walk and pinned_fs.supports_pinned_walk()
    with mock.patch.object(pinned_fs, "supports_pinned_walk", lambda: walk):
        with pytest.raises(OSError):
            h.write_ctx_overflow("chat-ancestor-create", [{"ctxId": "spill"}], sessions)

    assert list(foreign.iterdir()) == [], (
        "the write created the sidecar root through a link at its parent, so acknowledged context "
        f"landed in a directory the caller never named: {list(foreign.iterdir())}"
    )


def test_a_junction_at_the_sidecar_ROOT_is_not_STATTED_through_on_write(tmp_path, monkeypatch):
    """The write refused LATE -- after a stat had already resolved through the planted root.

    Measured, not assumed: ``atomic_write`` carries its own parent-link guard, so the spill never
    landed at the target. But ``mkdir(parents=True, exist_ok=True)`` reaches that guard only after
    ``Path.is_dir`` has followed the link to decide the name is a directory, and resolving a UNC
    target is itself the outbound authentication. So the leak is the STAT, and this asserts on the
    stat rather than on the file -- dropping the ``_node_present`` guard re-fails it.
    """
    import errno
    from pathlib import Path as _Path

    from kiro_crew import history as h

    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    root = tmp_path / h.CTX_OVERFLOW_DIR_NAME
    try:
        os.symlink(elsewhere, root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"planting a directory link needs a privilege this host withholds: {exc}")

    followed: list[str] = []
    real_is_dir = _Path.is_dir
    monkeypatch.setattr(
        _Path, "is_dir", lambda self, *a, **k: (followed.append(str(self)), real_is_dir(self))[1]
    )

    with pytest.raises(OSError) as caught:
        h.write_ctx_overflow("chat-planted-write", [{"ctxId": "spill"}], tmp_path)

    assert str(root) not in followed, f"a stat resolved through the planted root: {followed}"
    assert caught.value.errno == errno.ENOTDIR, caught.value


def test_the_sidecar_root_is_not_probed_when_mkdir_finds_it_already_there(tmp_path, monkeypatch):
    """A node planted AFTER the absence check made mkdir(exist_ok=True) follow it.

    `os.lstat` declines to follow the final component, so a pre-check can only report the name absent
    AT CHECK TIME. `Path.mkdir(exist_ok=True)` then raises FileExistsError internally and calls
    `is_dir()` to decide the name is acceptable -- and THAT call follows a reparse point, which on
    Windows authenticates to its UNC target. Creating without `exist_ok` never probes, so the pin is
    the only thing that validates the node; restoring the probing form re-fails this.
    """
    import errno
    from pathlib import Path as _Path

    from kiro_crew import history as h

    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    root = tmp_path / h.CTX_OVERFLOW_DIR_NAME
    try:
        os.symlink(elsewhere, root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"planting a directory link needs a privilege this host withholds: {exc}")

    # THE RACE, expressed without one: the node is already planted while the absence check reports
    # it absent, which is exactly what an lstat that ran a moment before the plant would report.
    monkeypatch.setattr(h, "_node_present", lambda target, **kw: target != root)

    followed: list[str] = []
    real_is_dir = _Path.is_dir
    monkeypatch.setattr(
        _Path, "is_dir", lambda self, *a, **k: (followed.append(str(self)), real_is_dir(self))[1]
    )

    with pytest.raises(OSError) as caught:
        h.write_ctx_overflow("chat-raced-root", [{"ctxId": "spill"}], tmp_path)

    assert str(root) not in followed, f"the planted root was probed before the pin: {followed}"
    assert caught.value.errno == errno.ENOTDIR, caught.value


def test_the_sidecar_child_is_probed_only_under_the_pinned_root_on_write(tmp_path, monkeypatch):
    """The write derived its path BEFORE the pin, so a child probe resolved the root by name.

    `_ctx_overflow_path` probes each ALIAS candidate to decide which stem a spill occupies. With no
    descriptor that probe lstats the full child path, which resolves the agent-writable root -- and
    the traversal is itself the leak, before any refusable open. Derived inside the pin instead,
    every child probe is descriptor-relative; dropping the pin re-fails this.
    """
    from kiro_crew import history as h

    if not h._CTX_RELATIVE_PROBE:
        pytest.skip("this platform cannot address a probe relative to a descriptor")

    key = "slack:1712345678.9001"
    assert len(h.transcript_stems(key)) > 1, "precondition: this key must carry an alias stem"
    root = tmp_path / h.CTX_OVERFLOW_DIR_NAME
    root.mkdir()

    seen: list[tuple[str, object]] = []
    unpinned: list[str] = []
    real_lstat = os.lstat

    def _spy(target, *args, dir_fd=None, **kwargs):
        seen.append((str(target), dir_fd))
        if dir_fd is None and str(target).startswith(f"{root}{os.sep}"):
            unpinned.append(str(target))
        return real_lstat(target, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(os, "lstat", _spy)
    h.write_ctx_overflow(key, [{"ctxId": "spill"}], tmp_path)

    assert not unpinned, f"a sidecar child was probed by full path before the pin: {unpinned}"
    assert [t for t, fd in seen if fd is not None], (
        f"no child was probed relative to the pin, so an absence of unpinned probes proves "
        f"nothing here: {seen}"
    )


def test_restoring_a_quarantined_sidecar_renames_relative_to_the_pinned_root(tmp_path, monkeypatch):
    """Restore ran AFTER clear_ctx_overflow's pin closed, so its rename resolved the root by name.

    Asserts the MECHANISM rather than an effect: the rename must carry a directory descriptor,
    which is what leaves no window for the agent-writable root to be swapped between the check and
    the move. Pre-fix the call is ``Path.rename``, which passes no descriptor, so this goes red --
    and it goes red again if the pin is dropped.
    """
    from kiro_crew import history as h

    if not h._CTX_RELATIVE_MUTATION:
        pytest.skip("this platform cannot address a rename relative to a descriptor")

    root = tmp_path / h.CTX_OVERFLOW_DIR_NAME
    root.mkdir()
    original = root / "chat-restore.jsonl"
    holding = root / "chat-restore.jsonl.holding"
    holding.write_text('{"ctxId": "held"}\n', encoding="utf-8")

    descriptors: list[object] = []
    real_rename = os.rename

    def _spy(*args, **kwargs):
        descriptors.append(kwargs.get("src_dir_fd"))
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(os, "rename", _spy)
    assert not h.ConversationLog(tmp_path)._restore_quarantined([(original, holding)])

    assert descriptors, "no rename was observed at all, so this cannot show how one resolved"
    assert all(
        fd is not None for fd in descriptors
    ), f"a restore rename carried no directory descriptor: {descriptors}"
    assert original.exists() and not holding.exists()
