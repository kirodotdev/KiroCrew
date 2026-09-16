"""Reading a pending-context sidecar back, and refusing an unusable one."""

import json
import os
import stat

import pytest


def test_an_unusable_sidecar_line_is_reported_not_silently_dropped(tmp_path, caplog):
    """Per-line tolerance must not be silent: a dropped row is an acknowledged entry gone.

    The read keeps one bad row from costing the whole queue, which is right. What was missing is the
    report -- a partially corrupt spill lost entries the API had answered 200 for and said nothing,
    which is the same silent-loss class this file exists to close.
    """
    import logging

    from kiro_crew import history as h

    path = h._ctx_overflow_path("chat-corrupt-spill", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written DIRECTLY, not through the writer: this is a file an earlier release or a hand edit
    # left behind, and the writer would refuse to produce it.
    path.write_text(
        '{"ctxId": "good-1", "content": "a"}\n'
        "{this is not json\n"
        '["not", "an", "object"]\n'
        '{"ctxId": "good-2", "content": "b"}\n',
        encoding="utf-8",
    )

    with caplog.at_level(logging.ERROR):
        entries = h.read_ctx_overflow("chat-corrupt-spill", tmp_path)

    assert [e.get("ctxId") for e in entries] == [
        "good-1",
        "good-2",
    ], f"the readable rows must survive, got {[e.get('ctxId') for e in entries]}"
    reports = [r for r in caplog.records if "unusable" in r.getMessage()]
    assert reports, (
        "two rows were dropped and nothing was logged, so a partially corrupt spill loses "
        "acknowledged entries silently"
    )
    msg = reports[0].getMessage()
    assert "2 of 4" in msg, f"the report must count what was lost against the whole file: {msg!r}"


def test_deleting_an_absent_transcript_discards_its_quarantined_sidecar(tmp_path):
    """An absent transcript owns nothing, so its spill must be discarded rather than restored.

    ``delete_session`` reports ``existed``, so a False answer covers a pinned SKIP and an ABSENT
    transcript alike. Only the skip leaves a live transcript owed its spill; for an absent one the
    restore puts an orphaned file back on the hydration stem, and the next session created at that
    key -- a reused channel key, or a recreated tab -- hydrates it as another session's context.

    Reachable exactly where this change is aimed: a crash between the sidecar write and the
    transcript commit leaves the spill with no transcript at all.
    """
    from kiro_crew import history as h

    log = h.ConversationLog(tmp_path)
    key = "chat-orphaned-spill"

    # A sidecar with NO transcript: written directly, because that is the state a crash between the
    # spill and the commit leaves behind, and the writer would not produce it.
    h.write_ctx_overflow(
        key, [{"ctxId": "orphan-1", "content": "another session's context"}], tmp_path
    )
    assert h.read_ctx_overflow(key, tmp_path), "precondition: the spill must exist to be discarded"
    assert not log._path(key).exists(), "precondition: this key must have NO transcript"

    result = log._delete_session_locked(key)
    assert result is False, f"an absent transcript reports False, got {result!r}"

    assert h.read_ctx_overflow(key, tmp_path) == [], (
        "the spill was put back on the hydration stem for a transcript that does not exist, so a "
        "session later created at this key would re-inject another session's context"
    )
    leftovers = sorted(p.name for p in h._archive_dir(tmp_path).glob("*") if p.is_file())
    assert not any(
        n.startswith(h._safe_key(key)) for n in leftovers
    ), f"a holding for this key outlived the absent transcript: {leftovers}"


def test_an_oversized_spill_is_refused_rather_than_emitted_unreadable(tmp_path, monkeypatch):
    """A spill is published as ONE generation, and one the reader would refuse is never emitted.

    Spreading a large spill over continuation files gave it no atomic publish: an interrupted
    rewrite, or an unlink failure on a stale continuation, left a MIXED generation that the next
    hydration recombined -- losing entries the new generation dropped and re-seating ones it did
    not. One file has exactly one rename, so no such state exists.

    The bound is therefore enforced by REFUSING the write, not by truncating it: the caller still
    holds these entries in its live queue, while a partial file would be silent loss and an
    oversized one would be quarantined on the way back in.
    """
    import kiro_crew.history as hist

    monkeypatch.setattr(hist, "_MAX_CTX_OVERFLOW_BYTES", 4096)

    key = "chat-oversized-refusal"
    with pytest.raises(hist.CtxOverflowTooLarge):
        hist.write_ctx_overflow(
            key, [{"ctxId": f"big-{i}", "content": "y" * 900} for i in range(12)], tmp_path
        )
    assert not hist._ctx_overflow_path(key, tmp_path).exists(), (
        "an oversized spill was emitted anyway, so hydration will refuse the very file the writer "
        "just wrote and the content is stranded"
    )

    # CONTROL: a spill inside the bound still round-trips, or the refusal has broken every spill.
    fits = [{"ctxId": f"ok-{i}", "content": "z" * 200} for i in range(4)]
    hist.write_ctx_overflow(key, fits, tmp_path)
    assert [e["ctxId"] for e in hist.read_ctx_overflow(key, tmp_path)] == [e["ctxId"] for e in fits]


def test_a_read_refuses_to_materialize_more_than_the_entry_ceiling(tmp_path, monkeypatch):
    """The size check bounded BYTES ON DISK while the decoded entries accumulated in memory.

    Hydration builds a list, so a spill inside the byte ceiling can still materialize an unbounded
    number of entries. The aggregate cap is what actually bounds the read, and it refuses rather
    than returning a silent prefix of acknowledged content.
    """
    import kiro_crew.history as hist

    monkeypatch.setattr(hist, "_MAX_CTX_OVERFLOW_ENTRIES", 5)

    key = "chat-entry-ceiling"
    # WRITTEN DIRECTLY: the writer refuses this same ceiling, so the case under test is a file an
    # earlier release left on disk.
    spill = hist._ctx_overflow_path(key, tmp_path)
    spill.parent.mkdir(parents=True, exist_ok=True)
    spill.write_text(
        "".join(json.dumps({"ctxId": f"e-{i}", "content": "x"}) + "\n" for i in range(9)),
        encoding="utf-8",
    )
    with pytest.raises(hist.CtxOverflowTooLarge):
        hist.read_ctx_overflow(key, tmp_path)
    # SELF-HEALING, like the byte path: the over-count file must leave the hydration stem, or every
    # later read raises again on it with no way out.
    assert not spill.exists(), "the over-count spill stayed hydratable, so the refusal never clears"

    # CONTROL: a spill under the cap reads normally, so the cap is not refusing everything.
    hist.write_ctx_overflow(key, [{"ctxId": "solo", "content": "x"}], tmp_path)
    assert [e["ctxId"] for e in hist.read_ctx_overflow(key, tmp_path)] == ["solo"]


def test_an_oversized_sidecar_is_quarantined_instead_of_read_whole(tmp_path, monkeypatch):
    """A whole-file read of a writable sidecar on the hydration path is an unbounded allocation.

    The spill file's size is not bounded by the writer: the union keeps its on-disk side across
    an unbounded number of distinct-slot rows-only saves onto one transcript key, and the per-slot
    caps count entries rather than bytes. Reading it whole during hydration therefore lets one
    oversized file allocate the gateway out of memory.

    Quarantine rather than plain refusal is the point: a refusal that left the file in place would
    repeat the same oversized read on every later hydration.
    """
    import kiro_crew.history as hist

    key = "chat-oversized-sidecar"
    monkeypatch.setattr(hist, "_MAX_CTX_OVERFLOW_BYTES", 2048)
    # WRITTEN DIRECTLY, not through `write_ctx_overflow`, which now refuses to emit a file past the
    # ceiling. The case under test is one an EARLIER release left on disk.
    spill = hist._ctx_overflow_path(key, tmp_path)
    spill.parent.mkdir(parents=True, exist_ok=True)
    spill.write_text(json.dumps({"ctxId": "big-1", "content": "y" * 4096}) + "\n", encoding="utf-8")
    assert spill.exists() and spill.stat().st_size > 2048, "precondition: the file is over the cap"

    with pytest.raises(hist.CtxOverflowTooLarge):
        hist.read_ctx_overflow(key, tmp_path)
    assert not spill.exists(), (
        "the oversized sidecar stayed on the hydration stem, so every later hydration repeats "
        "the same unbounded read"
    )
    # RECOVERABLE, not destroyed: quarantine renames off the stem rather than unlinking.
    assert list(tmp_path.rglob("*.orphaned-*")), "the bytes were deleted instead of quarantined"

    # CONTROL: a sidecar inside the cap still reads normally, or the cap has broken hydration.
    small = "chat-small-sidecar"
    hist.write_ctx_overflow(small, [{"ctxId": "ok-1", "content": "fits"}], tmp_path)
    assert [e.get("ctxId") for e in hist.read_ctx_overflow(small, tmp_path)] == ["ok-1"]


def test_a_legacy_slack_spill_lands_beside_its_own_transcript(tmp_path):
    """The spill went to the CANONICAL stem while the transcript lived at the BARE one.

    ``ConversationLog._path`` keeps reading a pre-migration Slack thread under its bare
    ``thread_ts`` filename, so pairing the sidecar with the canonical stem put it beside a
    transcript that does not exist. History resumed the bare stem, found no sidecar, and never
    restored context the API had acknowledged.
    """
    from kiro_crew import history as h
    from kiro_crew.messaging.link import legacy_key

    key = "slack:1699999999.123456"
    bare = legacy_key(key)
    assert bare, "precondition: this is a legacy-shaped Slack key"

    log = h.ConversationLog(base_dir=tmp_path)
    # The pre-migration transcript: the BARE filename, with no canonical file beside it.
    (tmp_path / f"{h._safe_key(bare)}.jsonl").write_text("", encoding="utf-8")
    assert log._path(key).stem == h._safe_key(
        bare
    ), "precondition: _path resolves to the legacy stem"

    written = h.write_ctx_overflow(key, [{"ctxId": "sp-1", "content": "acknowledged"}], tmp_path)

    assert written.stem == h._safe_key(bare), (
        f"the spill landed on {written.stem!r}, not beside its own transcript "
        f"({h._safe_key(bare)!r}), so a resume of the legacy stem cannot see it"
    )
    assert [e["ctxId"] for e in h.read_ctx_overflow(key, tmp_path)] == ["sp-1"]
    # CONTROL: a MODERN key, whose canonical transcript exists, must still use the canonical
    # stem -- otherwise this passes for an implementation that always prefers the legacy alias.
    modern = "slack:1799999999.500000"
    (tmp_path / f"{h._safe_key(modern)}.jsonl").write_text("", encoding="utf-8")
    assert h.write_ctx_overflow(modern, [{"ctxId": "sp-2"}], tmp_path).stem == h._safe_key(modern)


def test_a_failed_empty_clear_cannot_leave_a_hydratable_sidecar(tmp_path):
    """The clear REPORTED its failure and the caller threw the report away.

    ``sync_ctx_overflow`` called ``clear_ctx_overflow`` and ignored the returned survivors, so an
    unlink failure committed an empty metadata line while the stale sidecar stayed hydratable --
    the next fold then re-injected already-delivered context. The clear now retries, quarantines
    off the ``.jsonl`` stem what still will not unlink, and raises if even that fails.
    """
    import errno
    from unittest import mock

    from kiro_crew import history as h

    key = "chat-empty-clear-fails"
    h.write_ctx_overflow(key, [{"ctxId": "delivered-1", "content": "already delivered"}], tmp_path)
    assert h.read_ctx_overflow(key, tmp_path), "precondition: the sidecar is hydratable"

    import pathlib

    locked = OSError(errno.EACCES, "permission denied")
    _real_unlink = os.unlink
    _real_path_unlink = pathlib.Path.unlink

    def _locked_unlink(name, *a, dir_fd=None, **kw):
        # BOTH FORMS, because the platform decides whether the clear addresses a descriptor or a
        # path: patching one form injects nothing on the other and the test passes vacuously.
        if dir_fd is not None:
            raise locked
        return _real_unlink(name, *a, dir_fd=dir_fd, **kw)

    def _locked_path_unlink(self, *a, **kw):
        if h.CTX_OVERFLOW_DIR_NAME in self.parts:
            raise locked
        return _real_path_unlink(self, *a, **kw)

    with (
        mock.patch.object(h.os, "unlink", _locked_unlink),
        mock.patch.object(pathlib.Path, "unlink", _locked_path_unlink),
    ):
        h.sync_ctx_overflow(key, [], tmp_path)

    assert h.read_ctx_overflow(key, tmp_path) == [], (
        "the sidecar is still hydratable after an empty-queue sync, so the next fold re-injects "
        "context that was already delivered"
    )
    # CONTROL: the bytes were quarantined rather than destroyed, so this is not passing merely
    # because the entries were thrown away.
    holdings = list((tmp_path / h.CTX_OVERFLOW_DIR_NAME).glob("*.orphaned-*"))
    assert holdings, "the retired entries were destroyed instead of quarantined"


def test_a_skipped_pinned_delete_keeps_the_pending_context(tmp_path):
    """The sidecar was unlinked BEFORE the skip decision, so a pin lost its context outright.

    ``clear_ctx_overflow`` unlinked on the success path and only quarantined when the unlink had
    already failed, so on the ordinary path the restore list was EMPTY. A bulk clear that reached
    a session pinned in the meantime therefore returned ``None`` -- transcript kept, pending
    context permanently destroyed. Quarantine now RENAMES first and the unlink waits for success.
    """
    from kiro_crew import history as h

    log = h.ConversationLog(base_dir=tmp_path)
    key = "chat-pinned-keeps-context"
    log.append(key, "user", "a turn")
    log.update_metadata(key, {"pinned": True})
    h.write_ctx_overflow(key, [{"ctxId": "keep-1", "content": "acknowledged"}], tmp_path)

    skipped = log.delete_session(key, skip_pinned=True)

    assert skipped is None, "a pinned session must report the delete as SKIPPED"
    survived = h.read_ctx_overflow(key, tmp_path)
    assert [e.get("ctxId") for e in survived] == ["keep-1"], (
        "the pinned session kept its transcript but LOST its pending context, which the API had "
        "already acknowledged as durable"
    )
    # CONTROL: an unpinned delete must still take the sidecar with it, or the assertion above
    # would pass for an implementation that simply never clears.
    other = "chat-unpinned-clears"
    log.append(other, "user", "a turn")
    h.write_ctx_overflow(other, [{"ctxId": "gone-1", "content": "delivered"}], tmp_path)
    assert log.delete_session(other) is True
    assert h.read_ctx_overflow(other, tmp_path) == []


def test_a_failed_sidecar_deletion_refuses_to_delete_the_transcript(tmp_path):
    """A suppressed unlink reported success while leaving the spill HYDRATABLE.

    The transcript went first and the clear came second under ``contextlib.suppress(OSError)``,
    so a locked sidecar survived a "successful" delete and the next session created at that key
    re-injected the deleted session's context. The clear now runs FIRST and a survivor refuses
    the delete outright.
    """
    import errno
    from unittest import mock

    from kiro_crew import history as h

    log = h.ConversationLog(base_dir=tmp_path)
    key = "chat-delete-locked-spill"
    log.append(key, "user", "a turn")
    h.write_ctx_overflow(key, [{"ctxId": "spill-1", "content": "acknowledged"}], tmp_path)

    import pathlib

    locked = OSError(errno.EACCES, "permission denied")
    real_unlink = os.unlink
    real_rename = os.rename
    real_path_unlink = pathlib.Path.unlink
    real_path_rename = pathlib.Path.rename

    def _unlink(name, *a, dir_fd=None, **kw):
        # ONLY the sidecar is locked: a call carrying dir_fd is relative to the vetted root.
        # Locking every unlink would break the transcript delete and pass for the wrong reason.
        if dir_fd is not None:
            raise locked
        return real_unlink(name, *a, dir_fd=dir_fd, **kw)

    def _rename(src, dst, *a, src_dir_fd=None, **kw):
        if src_dir_fd is not None:
            raise locked
        return real_rename(src, dst, *a, src_dir_fd=src_dir_fd, **kw)

    def _path_unlink(self, *a, **kw):
        # The PATH form is the same mutation where the platform has no dir_fd support, so it is
        # locked on the same condition: a name under the sidecar root.
        if h.CTX_OVERFLOW_DIR_NAME in self.parts:
            raise locked
        return real_path_unlink(self, *a, **kw)

    def _path_rename(self, target, *a, **kw):
        if h.CTX_OVERFLOW_DIR_NAME in self.parts:
            raise locked
        return real_path_rename(self, target, *a, **kw)

    with (
        mock.patch.object(h.os, "unlink", _unlink),
        mock.patch.object(h.os, "rename", _rename),
        mock.patch.object(pathlib.Path, "unlink", _path_unlink),
        mock.patch.object(pathlib.Path, "rename", _path_rename),
    ):
        deleted = log.delete_session(key)

    assert deleted is False, (
        "the delete reported success while the sidecar survived, so a session reusing this key "
        "would hydrate the deleted session's context"
    )
    assert h.read_ctx_overflow(key, tmp_path), "the surviving spill must not have been destroyed"
    # CONTROL: with the filesystem working, the same delete succeeds and clears the spill.
    assert log.delete_session(key) is True
    assert h.read_ctx_overflow(key, tmp_path) == []


def test_an_unreadable_sidecar_is_not_reported_as_empty(tmp_path):
    """An I/O failure returned ``[]``, indistinguishable from having no spill at all.

    So a hydration read "no spilled entries" from a file it could not open, dropped acknowledged
    context, and the next save committed a metadata line without it. Absence is ordinary and
    still returns ``[]``; an unreadable file must surface as an error.
    """
    import errno
    from unittest import mock

    from kiro_crew import history as h

    key = "chat-unreadable-spill"
    h.write_ctx_overflow(key, [{"ctxId": "spill-1", "content": "acknowledged"}], tmp_path)

    # A genuine ABSENCE stays quiet -- the control that stops this passing for a read that
    # simply raises on everything.
    assert h.read_ctx_overflow("chat-no-spill-at-all", tmp_path) == []

    boom = OSError(errno.EIO, "I/O error")
    # INJECTED AT the no-follow opener, which is what the bounded streamed read calls: the read is
    # deliberately not a whole-file `read_text`, so patching that would inject a fault it never hits.
    with mock.patch.object(h, "open_regular_nofollow", side_effect=boom):
        try:
            got = h.read_ctx_overflow(key, tmp_path)
        except h.CtxOverflowUnreadable:
            return
    raise AssertionError(
        f"an unreadable sidecar returned {got!r} instead of raising, so a caller cannot tell it "
        "from a session that has no spilled context"
    )


def test_the_sidecar_follows_a_legacy_transcript_alias(tmp_path):
    """GPT BLOCKING F2: sidecars keyed on the canonical stem only, so a legacy thread split.

    ``ConversationLog._path`` falls back to the pre-migration bare ``thread_ts`` filename, so one
    session key can resolve to either stem -- which is exactly why ``transcript_stems`` exists.
    ``_ctx_overflow_path`` ignored that, so a legacy thread could carry a sidecar under one stem
    while deletion cleared the other, orphaning a file that resurrects deleted context when the
    canonical key is reused.
    """
    from kiro_crew import history as h
    from kiro_crew.messaging.link import legacy_key

    canonical = "slack:1699999999.123456"
    legacy = legacy_key(canonical)
    assert legacy, "precondition: this key has a legacy alias"

    # The sidecar exists under the LEGACY stem, as a pre-migration thread's would.
    (tmp_path / h.CTX_OVERFLOW_DIR_NAME).mkdir(parents=True, exist_ok=True)
    h.write_ctx_overflow(legacy, [{"ctxId": "legacy-1", "content": "x"}], tmp_path)

    seen = {e.get("ctxId") for e in h.read_ctx_overflow(canonical, tmp_path)}
    assert "legacy-1" in seen, (
        "a read via the canonical key missed the legacy-stem sidecar, so the two stems hold "
        "separate queues for ONE transcript"
    )

    h.clear_ctx_overflow(canonical, tmp_path)
    left = {e.get("ctxId") for e in h.read_ctx_overflow(legacy, tmp_path)}
    assert not left, (
        f"deleting via the canonical key orphaned the legacy-stem sidecar ({sorted(left)}); "
        "a later session reusing the key silently inherits deleted context"
    )


def test_a_planted_symlink_at_the_sidecar_path_is_refused_not_followed(tmp_path):
    """The sidecar tree is agent-writable and its filename derives from the session key.

    A plain binary open FOLLOWS a symlink, so a planted link makes any file this process can read
    get parsed as queue records. Its pair, the FIFO test below, fails for a different reason: that
    node wedges the reader inside the open call rather than yielding foreign content.
    """
    from kiro_crew import history as h

    foreign = tmp_path / "not-a-sidecar.txt"
    foreign.write_bytes(b'{"ctxId": "smuggled", "content": "from another file"}\n')

    key = "chat-symlink-sidecar"
    target = h._ctx_overflow_path(key, tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(foreign, target)
    assert (
        target.is_symlink() and target.resolve() == foreign.resolve()
    ), "precondition: the planted node must resolve to the foreign file, so a follow would succeed"

    with pytest.raises(h.CtxOverflowUnreadable):
        h.read_ctx_overflow(key, tmp_path)


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="a FIFO cannot be planted where the OS has none"
)
def test_a_planted_fifo_at_the_sidecar_path_refuses_instead_of_wedging_the_reader(tmp_path):
    """A FIFO makes the OPEN itself block until a writer arrives, so a later type check never runs.

    The refusal has to come from the open's own flags. Paired with the symlink test above, which
    fails the other way: that one returns foreign content instead of blocking forever.
    """
    import threading

    from kiro_crew import history as h

    key = "chat-fifo-sidecar"
    target = h._ctx_overflow_path(key, tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(target)
    assert stat.S_ISFIFO(target.stat().st_mode) and target.stat().st_size == 0, (
        "precondition: a FIFO reports st_size 0, so a pre-open size check cannot substitute for "
        "the type check this test exercises"
    )

    outcome: list[str] = []

    def _read() -> None:
        try:
            h.read_ctx_overflow(key, tmp_path)
            outcome.append("read")
        except (h.CtxOverflowUnreadable, OSError):
            outcome.append("refused")

    worker = threading.Thread(target=_read, daemon=True)
    worker.start()
    worker.join(20)
    assert (
        not worker.is_alive()
    ), "the sidecar read is still blocked on a planted FIFO: the open must not wait for a writer"
    assert outcome == ["refused"], outcome


def test_sidecar_cleanup_succeeds_where_a_platform_cannot_address_a_directory_descriptor(tmp_path):
    """Windows has no ``dir_fd``, so the mutation must fall back to a path under the held pin.

    Its pin is a handle no rename can move the directory out from under, which is what makes the
    path-based call safe there. Without this arm cleanup would fail on every Windows restart.
    """
    from unittest import mock

    from kiro_crew import history as h

    key = "chat-no-dirfd-platform"
    h.write_ctx_overflow(key, [{"ctxId": "owed", "content": "acknowledged"}], tmp_path)
    assert h.read_ctx_overflow(key, tmp_path), "precondition: the sidecar is hydratable"

    with mock.patch.object(h, "_CTX_RELATIVE_MUTATION", False):
        cleared = h.clear_ctx_overflow(key, tmp_path, quarantine="on_failure")

    assert (
        not cleared.survivors
    ), f"cleanup refused where the platform cannot address a descriptor: {cleared.survivors}"
    assert not cleared.quarantined, cleared.quarantined
    assert (
        h.read_ctx_overflow(key, tmp_path) == []
    ), "the sidecar stayed hydratable, so a restart re-injects context already delivered"


def test_no_path_probe_traverses_a_planted_sidecar_link_before_the_nofollow_open(tmp_path):
    """The PROBE is the leak, not just the read: a followed link authenticates during the check.

    ``Path.exists()`` and ``Path.stat()`` traverse, so a reparse point aimed at a UNC share makes
    the probe itself reach that host before any guard runs. Every sidecar presence and size check
    must therefore come from ``os.lstat`` or from the already-open descriptor.
    """
    import errno
    from pathlib import Path
    from unittest import mock

    from kiro_crew import history as h

    root = tmp_path / h.CTX_OVERFLOW_DIR_NAME
    root.mkdir()
    key = "chat-probe-free"
    elsewhere = tmp_path / "foreign.jsonl"
    elsewhere.write_bytes(b'{"ctxId": "not-ours"}\n')
    planted = h._ctx_overflow_path(key, tmp_path)
    os.symlink(elsewhere, planted)

    traversed: list[str] = []
    real_stat, real_exists = Path.stat, Path.exists

    def _stat_spy(self, *a, **kw):
        traversed.append(f"stat:{self}")
        return real_stat(self, *a, **kw)

    def _exists_spy(self, *a, **kw):
        traversed.append(f"exists:{self}")
        return real_exists(self, *a, **kw)

    with mock.patch.object(Path, "stat", _stat_spy), mock.patch.object(Path, "exists", _exists_spy):
        with pytest.raises(h.CtxOverflowUnreadable) as caught:
            h.read_ctx_overflow(key, tmp_path)

    assert str(errno.ELOOP) in str(
        caught.value
    ), f"the refusal must carry the no-follow refusal, not a generic read error: {caught.value}"
    assert not [
        t for t in traversed if str(planted) in t
    ], f"a traversing probe touched the planted sidecar before the no-follow open: {traversed}"
    assert elsewhere.read_bytes() == b'{"ctxId": "not-ours"}\n', "the foreign file was disturbed"


def test_a_junction_at_the_sidecar_ROOT_is_refused_not_traversed_on_read(tmp_path):
    """Refusing only the FINAL component leaves the parent traversable, which is the whole leak.

    ``O_NOFOLLOW`` and ``FILE_FLAG_OPEN_REPARSE_POINT`` settle the leaf. Resolving the path TO that
    leaf still walks the parent, so a junction planted at the agent-writable sidecar root sends the
    gateway to its target -- on Windows authenticating to an attacker's UNC share.

    The control below is the fail-first arm: given a root descriptor obtained WITHOUT validation,
    the same read returns the foreign content, which is what shipped before the root was pinned.
    """
    from kiro_crew import history as h
    from kiro_crew.jsonl_util import open_regular_nofollow

    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    key = "chat-planted-root"
    stem = h.transcript_stems(key)[0]
    (elsewhere / f"{stem}.jsonl").write_text(
        '{"ctxId": "planted", "content": "from the attacker share"}\n', encoding="utf-8"
    )
    root = tmp_path / h.CTX_OVERFLOW_DIR_NAME
    # target_is_directory is IGNORED on POSIX and decides the link TYPE on Windows, where a
    # file-type link to a directory leaves the tree unremovable and fails the run in teardown.
    try:
        os.symlink(elsewhere, root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"planting a directory link needs a privilege this host withholds: {exc}")

    with pytest.raises(h.CtxOverflowUnreadable) as caught:
        h.read_ctx_overflow(key, tmp_path)
    assert "not a plain directory" in str(caught.value), caught.value

    # The control needs a raw directory descriptor for a dir_fd-relative open, and Windows offers
    # neither; the refusal asserted above is the product behaviour under test on every platform.
    if os.open not in os.supports_dir_fd:
        return
    unvalidated = os.open(root, os.O_RDONLY)
    try:
        with open_regular_nofollow(
            h._ctx_overflow_path(key, tmp_path), max_bytes=4096, dir_fd=unvalidated
        ) as handle:
            leaked = handle.read()
    finally:
        os.close(unvalidated)
    assert b"from the attacker share" in leaked, (
        "CONTROL FAILED: the planted root did not yield foreign content even unvalidated, so this "
        "test cannot show the guard is what refuses"
    )
