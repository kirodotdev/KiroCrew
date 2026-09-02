"""Tests for ``ConversationLog._rebuild_tab_id_index``.

The rebuild reads each session file's ``tab_id`` through ``_read_metadata`` so an
unchanged file costs a stat rather than an open. These tests pin both halves of
that: the I/O actually goes away, and it does NOT go away in the one case a
naive stat-keyed cache would get wrong (a metadata rewrite restores the
pre-write mtime, so mtime alone cannot see a tab_id change).
"""

from __future__ import annotations

import builtins
import json
import os
from pathlib import Path

from kiro_crew.history import ConversationLog


class OpenCounter:
    """Count file opens of session files in *directory*.

    Patches ``Path.open`` as well as ``builtins.open``: ``Path.open`` does not
    route through ``builtins.open``, so counting only the builtin silently misses
    a caller that uses the ``Path`` method and the count is then not comparable
    across implementations. The wrappers are plain functions (not bound methods)
    so setting one as a class attribute still binds ``self``.
    """

    def __init__(self, monkeypatch, directory):
        real_builtin = builtins.open
        real_path = Path.open
        self._dir = str(directory)
        self.count = 0
        counter = self

        def wrapped_builtin(file, *args, **kwargs):
            counter._tally(file)
            return real_builtin(file, *args, **kwargs)

        def wrapped_path(path_self, *args, **kwargs):
            counter._tally(path_self)
            return real_path(path_self, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", wrapped_builtin)
        monkeypatch.setattr(Path, "open", wrapped_path)

    def _tally(self, name):
        text = str(name)
        if text.startswith(self._dir) and text.endswith(".jsonl"):
            self.count += 1


def test_open_counter_sees_both_open_flavours(tmp_path, monkeypatch):
    """Instrument self-test: a counter blind to either flavour proves nothing."""
    target = tmp_path / "dashboard_chat-probe-1.jsonl"
    target.write_text("{}\n", encoding="utf-8")
    counter = OpenCounter(monkeypatch, tmp_path)
    with open(target, encoding="utf-8") as fh:
        fh.read()
    assert counter.count == 1, "builtins.open not counted"
    with target.open(encoding="utf-8") as fh:
        fh.read()
    assert counter.count == 2, "Path.open not counted"


def _seed(tmp_path, n=3, tab_id="aaaaaaaaaaaa"):
    """Create *n* dashboard session files sharing *tab_id*."""
    log = ConversationLog(base_dir=tmp_path)
    keys = []
    for i in range(n):
        key = f"dashboard:chat-{i}-100{i}"
        log.append(key, "user", f"hello {i}", tab_id=tab_id)
        keys.append(key)
    return log, keys


def _rebuild(log):
    with log._lock:
        log._rebuild_tab_id_index()
    return log._tab_id_index


def test_first_rebuild_reads_every_file(tmp_path, monkeypatch):
    log, keys = _seed(tmp_path, n=3)
    log._meta_cache.clear()
    counter = OpenCounter(monkeypatch, tmp_path)
    index = _rebuild(log)
    assert sorted(index["aaaaaaaaaaaa"]) == sorted(keys)
    assert counter.count == 3


def test_second_rebuild_opens_nothing(tmp_path, monkeypatch):
    log, keys = _seed(tmp_path, n=3)
    _rebuild(log)
    counter = OpenCounter(monkeypatch, tmp_path)
    index = _rebuild(log)
    assert sorted(index["aaaaaaaaaaaa"]) == sorted(keys)
    assert counter.count == 0


def test_rebuild_after_one_append_opens_one_file(tmp_path, monkeypatch):
    log, keys = _seed(tmp_path, n=3)
    _rebuild(log)
    counter = OpenCounter(monkeypatch, tmp_path)
    log.append(keys[1], "user", "another")
    opens_from_append = counter.count
    counter.count = 0
    index = _rebuild(log)
    assert sorted(index["aaaaaaaaaaaa"]) == sorted(keys)
    assert counter.count == 1, (
        f"expected exactly one re-read, got {counter.count} "
        f"(append itself did {opens_from_append} opens)"
    )


def test_rebuild_sees_tab_id_backfill_despite_pinned_mtime(tmp_path):
    """A tab_id backfill restores the pre-write mtime — the rebuild must still see it.

    ``_update_metadata_locked`` calls ``_restore_mtime`` so a metadata edit does
    not reorder ``list_sessions``. A ``(path, mtime)``-keyed cache would keep its
    "no tab_id" entry and silently drop this session from its chain.
    """
    log = ConversationLog(base_dir=tmp_path)
    plain = "dashboard:chat-9-9009"
    log.append(plain, "user", "no tab yet")
    _, keys = _seed(tmp_path, n=1)

    before = _rebuild(log)
    assert plain not in before.get("aaaaaaaaaaaa", [])
    mtime_before = (tmp_path / "dashboard_chat-9-9009.jsonl").stat().st_mtime

    log.update_metadata(plain, {"tab_id": "aaaaaaaaaaaa"})

    path = tmp_path / "dashboard_chat-9-9009.jsonl"
    assert path.stat().st_mtime == mtime_before, (
        "precondition: the metadata rewrite must preserve mtime, otherwise this "
        "test is not exercising the stat-blind case"
    )
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["tab_id"] == (
        "aaaaaaaaaaaa"
    )

    after = _rebuild(log)
    assert plain in after["aaaaaaaaaaaa"]
    assert sorted(after["aaaaaaaaaaaa"]) == sorted([plain, *keys])


def test_index_is_none_after_invalidation_not_empty_dict(tmp_path):
    """Invalidation must mean "stale", never "built and empty"."""
    log, _ = _seed(tmp_path, n=2)
    _rebuild(log)
    assert isinstance(log._tab_id_index, dict)
    log.invalidate_tab_id_cache()
    assert log._tab_id_index is None


def test_absent_tab_id_gets_no_sentinel_entry(tmp_path):
    """A tid with no files must be absent from the index, not mapped to ``[]``."""
    log, _ = _seed(tmp_path, n=1)
    index = _rebuild(log)
    assert "bbbbbbbbbbbb" not in index
    assert all(v for v in index.values())


def _corrupt_tab_id(path, value):
    """Rewrite *path*'s metadata line so its tab_id is *value*."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    meta = json.loads(lines[0])
    meta["tab_id"] = value
    lines[0] = json.dumps(meta) + "\n"
    path.write_text("".join(lines), encoding="utf-8")


def test_unhashable_tab_id_skips_one_file_instead_of_aborting_rebuild(tmp_path):
    """One corrupt file must not take the whole index down with it.

    ``tab_id`` is minted as a uuid4 hex slice, so a non-str only arrives from
    corrupt on-disk metadata -- but it round-trips through JSON unvalidated. An
    unhashable value reaching ``index.setdefault(tid, ...)`` raises TypeError,
    which would abort the rebuild for every session rather than skip one file.
    """
    log, keys = _seed(tmp_path, n=3, tab_id="aaaaaaaaaaaa")
    victim = tmp_path / "dashboard_chat-1-1001.jsonl"
    assert victim.exists(), "seed layout changed; test would vacuously pass"
    _corrupt_tab_id(victim, ["not", "hashable"])

    index = _rebuild(log)

    # The two intact files are still chained; only the corrupt one is dropped.
    assert index["aaaaaaaaaaaa"] == ["dashboard:chat-0-1000", "dashboard:chat-2-1002"]
    assert not any("chat-1-1001" in k for v in index.values() for k in v)


def test_dict_tab_id_is_also_survivable(tmp_path):
    """Negative control for the guard: a dict tid is unhashable the same way."""
    log, _ = _seed(tmp_path, n=2, tab_id="aaaaaaaaaaaa")
    _corrupt_tab_id(tmp_path / "dashboard_chat-0-1000.jsonl", {"a": 1})
    index = _rebuild(log)
    assert index["aaaaaaaaaaaa"] == ["dashboard:chat-1-1001"]


def test_warm_rebuild_opens_nothing_above_the_transcript_cache_bound(tmp_path, monkeypatch):
    """The win must not collapse once the file count exceeds the shared LRU bound.

    The rebuild is a cyclic scan over every dashboard file. Backed by a *bounded*
    cache, a cyclic scan larger than the bound evicts each entry one step before
    its next read and the hit rate goes to zero -- the failure mode
    ``_SearchTextCache``'s docstring describes. ``_tab_id_by_key`` is unbounded
    precisely so this holds at any session count.
    """
    over = 260  # > _TRANSCRIPT_CACHE_MAX (256)
    log, _ = _seed(tmp_path, n=over, tab_id="aaaaaaaaaaaa")
    _rebuild(log)  # cold: populates the memo

    counter = OpenCounter(monkeypatch, tmp_path)
    index = _rebuild(log)

    assert counter.count == 0, f"warm rebuild opened {counter.count} files above the bound"
    assert len(index["aaaaaaaaaaaa"]) == over, "every session must still be chained"


def test_rebuild_does_not_evict_another_readers_metadata_entry(tmp_path):
    """The rebuild must not flush `_meta_cache` entries other callers warmed.

    Above the bound, routing every file through `_read_metadata` cycled one
    insertion per file through a 256-slot LRU shared with ``get_metadata`` /
    ``list_sessions`` / ``update_metadata_if``, so a walk evicted whatever they
    were holding. The private memo means a warm rebuild touches it not at all.
    """
    log, _ = _seed(tmp_path, n=260, tab_id="aaaaaaaaaaaa")
    _rebuild(log)  # cold

    foreign = "slack:C0123-456"
    log._meta_cache[foreign] = (1.0, {"_type": "metadata", "title": "foreign"})
    assert log._meta_cache.get(foreign) is not None, "precondition: entry was planted"

    _rebuild(log)

    assert log._meta_cache.get(foreign) is not None, "rebuild evicted another reader's entry"


def _hand_edit_tab_id(path: Path, tid: str) -> None:
    """Rewrite a file's tab_id on disk directly -- no ConversationLog API call.

    Deliberately bypasses every write path, so nothing calls ``_invalidate_cache``.
    This is the shape of an out-of-band edit.
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    meta = json.loads(lines[0])
    meta["tab_id"] = tid
    lines[0] = json.dumps(meta) + "\n"
    path.write_text("".join(lines), encoding="utf-8")


def test_out_of_band_tab_id_edit_is_not_served_stale(tmp_path):
    """A rewrite that goes AROUND this class must still be picked up.

    ``_invalidate_cache`` covers writes through the class; it cannot cover a
    hand-edited file, which never calls it. The memo's mtime half is what does.
    Without it the pre-edit id is served forever and the session either vanishes
    from its chain or joins the wrong one.
    """
    log, _ = _seed(tmp_path, n=3, tab_id="aaaaaaaaaaaa")
    _rebuild(log)  # memoize the pre-edit ids

    victim = tmp_path / "dashboard_chat-1-1001.jsonl"
    before = victim.stat().st_mtime
    _hand_edit_tab_id(victim, "bbbbbbbbbbbb")
    # Force an unambiguous mtime move so the assertion cannot pass or fail on
    # filesystem timestamp granularity.
    os.utime(victim, (before + 10, before + 10))

    index = _rebuild(log)

    assert "bbbbbbbbbbbb" in index, "out-of-band tab_id edit was served stale from the memo"
    assert index["bbbbbbbbbbbb"] == ["dashboard:chat-1-1001"]
    assert "dashboard:chat-1-1001" not in index.get("aaaaaaaaaaaa", [])


def test_memo_store_is_declined_when_a_writer_pops_mid_read(tmp_path, monkeypatch):
    """The store must not land after a concurrent writer's pop.

    ``_invalidate_cache`` takes no lock, so it can interleave with a rebuild that
    holds ``self._lock``. If the rebuild read a value, a writer then popped the
    key, and the rebuild stored afterwards, the memo would hold a resurrected
    stale id. The generation counter is what declines that store. This drives the
    interleaving deterministically rather than waiting for the cache to settle,
    which would order the window away.
    """
    log, _ = _seed(tmp_path, n=1, tab_id="aaaaaaaaaaaa")
    key = "dashboard:chat-0-1000"
    log._tab_id_by_key.clear()

    # Patch the method the rebuild actually calls: it reads through
    # _read_metadata_status so a transient failure is distinguishable from an
    # empty record, and a patch on the flag-dropping wrapper would not intercept.
    real = log._read_metadata_status

    def racing_read(k, *a, **kw):
        out = real(k, *a, **kw)
        # Simulate the concurrent writer landing between our read and our store.
        log._invalidate_cache(k)
        return out

    monkeypatch.setattr(log, "_read_metadata_status", racing_read)
    _rebuild(log)

    assert (
        key not in log._tab_id_by_key
    ), "memo stored a value after a concurrent pop -- generation guard did not fire"


def test_a_session_without_a_tab_id_is_not_reread_every_rebuild(tmp_path, monkeypatch):
    """A file with no ``tab_id`` must be memoized too, or it never stops costing I/O.

    Such a file cannot be indexed, so an early ``continue`` before the memo store
    looks harmless -- but it means every rebuild re-reads it through the SHARED
    ``_meta_cache``, evicting entries the other readers warmed. The sentinel is
    what makes a warm rebuild free for these files as well.
    """
    log, _ = _seed(tmp_path, n=1, tab_id="aaaaaaaaaaaa")
    bare = tmp_path / "dashboard_chat-bare-2000.jsonl"
    bare.write_text(json.dumps({"_type": "metadata"}) + "\n", encoding="utf-8")

    _rebuild(log)  # cold: populates the memo, including the sentinel
    counter = OpenCounter(monkeypatch, tmp_path)
    _rebuild(log)  # warm

    assert counter.count == 0, (
        f"warm rebuild opened {counter.count} file(s); a session without a tab_id "
        "is still being re-read"
    )
    assert (
        log._tab_id_by_key["dashboard:chat-bare-2000"][1] == ""
    ), "no-tab_id sentinel was not memoized"


def test_a_session_that_gains_a_tab_id_is_picked_up(tmp_path, monkeypatch):
    """The sentinel must not outlive the absence it records.

    Negative control on the memo itself: if "" were sticky, a session that later
    gains a ``tab_id`` would stay invisible to every chained read -- the silent
    direction, since nothing errors.
    """
    log, _ = _seed(tmp_path, n=1, tab_id="aaaaaaaaaaaa")
    bare_key = "dashboard:chat-bare-2000"
    bare = tmp_path / "dashboard_chat-bare-2000.jsonl"
    bare.write_text(json.dumps({"_type": "metadata"}) + "\n", encoding="utf-8")

    _rebuild(log)
    assert log._tab_id_by_key[bare_key][1] == ""

    # update_metadata is the path that sets tab_id on an EXISTING session --
    # append only stamps it at file creation, so it leaves this file bare.
    log.update_metadata(bare_key, {"tab_id": "aaaaaaaaaaaa"})
    _rebuild(log)

    assert bare_key in (log._tab_id_index or {}).get(
        "aaaaaaaaaaaa", []
    ), "a session that gained a tab_id stayed invisible -- the sentinel went stale"


def test_a_transient_read_failure_is_not_memoized_as_no_tab_id(tmp_path, monkeypatch):
    """An unreadable file must be retried, never cached as a definitive absence.

    ``_read_metadata`` drops the readability flag, so a transient failure (an AV
    scanner holding a freshly appended file: ``stat`` succeeds, ``open`` does not)
    arrives as ``{}``. Memoizing that against an unchanged stamp would drop the
    session from its chain on every later rebuild until its next write -- the
    vanished-history failure this index exists to prevent. Reading through
    ``_read_metadata_status`` and skipping the store is what keeps it self-healing.
    """
    log, keys = _seed(tmp_path, n=1, tab_id="aaaaaaaaaaaa")
    key = keys[0]
    log._tab_id_by_key.clear()
    log._meta_cache.pop(key, None)

    real_status = log._read_metadata_status
    failing = {"on": True}

    def flaky_status(k, *a, **kw):
        if failing["on"] and k == key:
            return ({}, False)  # what an exhausted-retry read returns
        return real_status(k, *a, **kw)

    monkeypatch.setattr(log, "_read_metadata_status", flaky_status)
    _rebuild(log)

    assert key not in log._tab_id_by_key, (
        "a transient read failure was memoized -- the session is now dropped "
        "from its chain until its next write"
    )

    # The failure clears; nothing wrote to the file, so the stamp is unchanged.
    failing["on"] = False
    _rebuild(log)

    assert key in (log._tab_id_index or {}).get(
        "aaaaaaaaaaaa", []
    ), "the session did not self-heal after the transient failure cleared"


def test_a_same_mtime_rewrite_that_changes_size_is_not_served_stale(tmp_path):
    """The same-tick window, constructed rather than raced.

    ``test_out_of_band_tab_id_edit_is_not_served_stale`` moves the mtime forward
    by 10s so its assertion cannot turn on timestamp granularity -- which orders
    THIS window away. Here the mtime is pinned BACK to its pre-edit value, so the
    stamp differs only in size. That misses the memo, and the reread then lands on
    ``_meta_cache``, which is keyed on mtime alone and would hand back the old
    metadata for the rebuild to re-memoize under the new stamp.
    """
    log, keys = _seed(tmp_path, n=1, tab_id="aaaaaaaaaaaa")
    key = keys[0]
    path = tmp_path / "dashboard_chat-0-1000.jsonl"

    _rebuild(log)  # memoizes the tab_id AND warms _meta_cache for this key
    assert key in (log._tab_id_index or {}).get("aaaaaaaaaaaa", [])

    before_mtime = path.stat().st_mtime
    before_size = path.stat().st_size
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    meta = json.loads(lines[0])
    meta["tab_id"] = "bbbbbbbbbbbbbbbb"  # deliberately a DIFFERENT length
    path.write_text("".join([json.dumps(meta) + "\n"] + lines[1:]), encoding="utf-8")
    os.utime(path, (before_mtime, before_mtime))

    # Fixture controls: without both of these the test could pass vacuously.
    assert path.stat().st_mtime == before_mtime, "mtime did not pin"
    assert path.stat().st_size != before_size, "size did not change"

    _rebuild(log)

    assert key in (log._tab_id_index or {}).get("bbbbbbbbbbbbbbbb", []), (
        "the new tab_id was not picked up -- the reread was served stale "
        "metadata from the mtime-keyed _meta_cache"
    )
    assert key not in (log._tab_id_index or {}).get(
        "aaaaaaaaaaaa", []
    ), "the session is still attached to its pre-edit chain"


def test_another_instances_metadata_rewrite_is_not_served_stale(tmp_path):
    """A write through ANOTHER instance of this class must not leave us stale.

    Both guards miss this one, which is why it needs its own test. The write
    goes THROUGH the class, so it restores the pre-write mtime and the stamp
    cannot see it; but ``_invalidate_cache`` pops the memo of the instance that
    did the writing, and ``_tab_id_generation`` is per-instance too, so a
    reader's memo survives untouched. Two instances is how one process sees
    another's write -- the cross-process lock this class holds exists precisely
    because that is a supported deployment.

    A tab_id is a fixed-format equal-length string, so the replacement leaves
    ``st_size`` identical and size cannot discriminate either -- provided the
    file's line endings already match what the rewrite emits, which the fixture
    below arranges so the premise holds on Windows as well as POSIX.
    """
    key = "dashboard:chat-0-1000"
    path = tmp_path / "dashboard_chat-0-1000.jsonl"
    reader = ConversationLog(base_dir=tmp_path)
    writer = ConversationLog(base_dir=tmp_path)

    reader.append(key, "user", "hello", tab_id="aaaaaaaaaaaa")
    # append() writes in text mode, so on Windows the file lands with CRLF while
    # the metadata rewrite below reads with universal newlines and writes raw LF
    # bytes -- shrinking it a byte per line, which would let size discriminate the
    # very write this test needs both guards blind to. Normalise first (a no-op on
    # POSIX), and before the rebuild, so the memo stamps the file we compare against.
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    _rebuild(reader)  # memoize the pre-edit id in the READER's memo
    assert key in (reader._tab_id_index or {}).get("aaaaaaaaaaaa", [])

    before_mtime = path.stat().st_mtime
    before_size = path.stat().st_size

    writer.update_metadata(key, {"tab_id": "bbbbbbbbbbbb"})

    # Fixture controls: without all three the test could pass vacuously -- it
    # would no longer be exercising the case where BOTH guards are blind.
    assert path.stat().st_mtime == before_mtime, "writer did not restore mtime"
    assert path.stat().st_size == before_size, "replacement was not equal-length"
    assert key not in writer._tab_id_by_key, "writer did not pop its own memo"
    assert (
        reader._tab_id_by_key.get(key) is not None
    ), "reader's memo was already gone -- nothing left to serve stale"
    on_disk = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert on_disk["tab_id"] == "bbbbbbbbbbbb", "the new tab_id is not on disk"

    index = _rebuild(reader)

    assert key in index.get("bbbbbbbbbbbb", []), (
        "the reader served a stale tab_id from its memo: the session is missing "
        "from the chain it now belongs to"
    )
    assert key not in index.get(
        "aaaaaaaaaaaa", []
    ), "the session is still attached to its pre-edit chain"


def test_a_cold_memo_is_not_served_a_warm_meta_cache_line(tmp_path):
    """A key warm in ``_meta_cache`` but cold in the memo must still be re-read.

    ``get_metadata`` -- and the consolidation counters -- populate ``_meta_cache``
    without ever touching ``_tab_id_by_key``, so the two caches go out of step.
    ``_meta_cache`` is keyed on float mtime ALONE, which is strictly weaker than
    our stamp: after another instance's mtime-preserving equal-length rewrite it
    compares equal and hands back the PRE-write line. Memoizing that under the
    new, correct-looking stamp makes it permanent, because every later rebuild
    then takes the warm path and never re-reads.
    """
    key = "dashboard:chat-0-1000"
    path = tmp_path / "dashboard_chat-0-1000.jsonl"
    reader = ConversationLog(base_dir=tmp_path)
    writer = ConversationLog(base_dir=tmp_path)

    reader.append(key, "user", "hello", tab_id="aaaaaaaaaaaa")
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))

    # A real public caller, not a hand-poked cache: this is the precondition.
    reader.get_metadata(key)
    assert key in reader._meta_cache, "get_metadata did not warm _meta_cache"
    assert key not in reader._tab_id_by_key, "the memo must be COLD for this case"

    before_mtime = path.stat().st_mtime
    before_size = path.stat().st_size

    writer.update_metadata(key, {"tab_id": "bbbbbbbbbbbb"})

    assert path.stat().st_mtime == before_mtime, "writer did not restore mtime"
    assert path.stat().st_size == before_size, "replacement was not equal-length"
    assert key in reader._meta_cache, "reader's warm entry is gone -- case not exercised"

    index = _rebuild(reader)

    assert key in index.get("bbbbbbbbbbbb", []), (
        "a cold memo was served the stale line out of _meta_cache: the session is "
        "missing from the chain it now belongs to"
    )
    assert key not in index.get(
        "aaaaaaaaaaaa", []
    ), "the session is still attached to its pre-edit chain"
