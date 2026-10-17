"""Bounds on the lease scan and the candidate walk, refused unlinks, and the settings reader.

Entry and deadline bounds and their INFO reports, refused-unlink warnings and their
throttle, the ``cli.json`` reader and its read retry, ``set_mode`` sending the fresh
alias, and ``announce_alias``.
"""

from __future__ import annotations

import errno
import gc
import json
import os
import stat
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import native_skill_projection_helpers
import pytest
from native_skill_projection_helpers import (
    _alias_file,
    _crew_home_id,
    _hold_the_prune_clock,
    _legacy_alias,
    _metadata_file,
)

from kiro_crew.acp import skill_projection as projection

# The shared fixtures, bound here by name so pytest registers them for this module.
_no_eager_prune = native_skill_projection_helpers._no_eager_prune
_prune_on_every_spawn = native_skill_projection_helpers._prune_on_every_spawn
cyclic_gc_quiesced = native_skill_projection_helpers.cyclic_gc_quiesced
native_tree = native_skill_projection_helpers.native_tree


# ── Residual bounds, second pass: unbounded lease traversal, keep-starved prune,
#    crash-residue digest validity, sidecar-less foreign adoption, cap summary ──


def test_lease_record_scan_bounds_all_entries_not_only_records(native_tree, monkeypatch):
    """Defect 1: the scan ceiling must bound EVERY entry, not only ``.json`` records.

    The lease scan counted only ``.json`` records toward
    _PROJECTION_LEASE_SCAN_LIMIT, so a directory padded with arbitrary ``.hold``
    or unrelated entries slipped past the ceiling: a scan holding the publication
    lock could then walk a number of entries proportional to the whole directory
    rather than to the limit. With the ceiling of 2 and a leading pad of
    non-record entries, the fixed scan stops after 2 walked entries and yields no
    record; the old code walked every pad entry looking for records. Counting the
    walked entries proves the bound is over entries, not records.
    """
    lease_dir = projection._projection_lease_dir()
    lease_dir.mkdir(parents=True)
    monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 2, raising=False)

    # Many non-record entries first, then a real record far past the ceiling.
    pad_names = [f"pad-{index}{projection._PROJECTION_LEASE_HOLDER_SUFFIX}" for index in range(50)]
    record_name = f"real{projection._PROJECTION_LEASE_RECORD_SUFFIX}"
    ordered = [*pad_names, record_name]

    walked = 0

    class CountingEntries:
        def __init__(self):
            self._entries = iter(ordered)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal walked
            name = next(self._entries)
            walked += 1
            return SimpleNamespace(name=name)

    real_scandir = projection.os.scandir
    normalized = os.path.normcase(os.path.normpath(os.fspath(lease_dir)))

    def counting_scandir(path, *args, **kwargs):
        try:
            same = os.path.normcase(os.path.normpath(os.fspath(path))) == normalized
        except TypeError:
            same = False
        return CountingEntries() if same else real_scandir(path, *args, **kwargs)

    # Scoped: os.scandir is process-global, and the tmp-tree teardown (an
    # os.walk-based rmtree on Windows) must see the real one.
    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", counting_scandir)

        records = []

        def recording_probe(lease_path):
            records.append(lease_path.name)
            return [], None

        scan_patch.setattr(projection, "_probe_projection_lease", recording_probe)
        live, uncertain = projection._scan_projection_leases(agents_dir(native_tree))

    # Fixed: the ceiling stops the walk after it has counted the limit, pulling
    # at most _PROJECTION_LEASE_SCAN_LIMIT + 1 entries from the iterator (the last
    # one is the entry that trips the break), so no record is reached. The old
    # code walked all 51 entries because pads did not count toward the ceiling.
    assert walked <= projection._PROJECTION_LEASE_SCAN_LIMIT + 1
    assert walked < len(ordered), "the scan walked the whole padded directory"
    assert records == []
    assert live == set() and uncertain is True


def test_lease_record_scan_still_yields_records_within_the_bound(native_tree, monkeypatch):
    """Defect 1 companion: a record within the ceiling is still yielded.

    Bounding all entries must not stop yielding legitimate records that fall
    within the ceiling -- only the padding past it is skipped.
    """
    lease_dir = projection._projection_lease_dir()
    lease_dir.mkdir(parents=True)
    monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 4, raising=False)
    ordered = [
        f"a{projection._PROJECTION_LEASE_RECORD_SUFFIX}",
        f"a{projection._PROJECTION_LEASE_HOLDER_SUFFIX}",
        f"b{projection._PROJECTION_LEASE_RECORD_SUFFIX}",
        f"b{projection._PROJECTION_LEASE_HOLDER_SUFFIX}",
        f"c{projection._PROJECTION_LEASE_RECORD_SUFFIX}",
    ]

    class OrderedEntries:
        def __init__(self):
            self._entries = iter(SimpleNamespace(name=name) for name in ordered)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._entries)

    real_scandir = projection.os.scandir
    normalized = os.path.normcase(os.path.normpath(os.fspath(lease_dir)))

    def ordered_scandir(path, *args, **kwargs):
        try:
            same = os.path.normcase(os.path.normpath(os.fspath(path))) == normalized
        except TypeError:
            same = False
        return OrderedEntries() if same else real_scandir(path, *args, **kwargs)

    # Scoped: os.scandir is process-global, and the tmp-tree teardown (an
    # os.walk-based rmtree on Windows) must see the real one.
    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", ordered_scandir)

        record_names = []

        def recording_probe(lease_path):
            record_names.append(lease_path.name)
            return [lease_path.stem], None

        scan_patch.setattr(projection, "_probe_projection_lease", recording_probe)
        live, uncertain = projection._scan_projection_leases(agents_dir(native_tree))

    # Ceiling 4 over record, holder, record, holder, record admits a.json and
    # b.json (records at walked positions 1 and 3); c.json sits past the ceiling.
    assert record_names == [
        f"a{projection._PROJECTION_LEASE_RECORD_SUFFIX}",
        f"b{projection._PROJECTION_LEASE_RECORD_SUFFIX}",
    ]
    # Both held records union into the live set, and the cap answers uncertain.
    assert live == {"a", "b"} and uncertain is True


def test_prune_does_not_let_the_run_keep_alias_starve_backlog_reclamation(native_tree, monkeypatch):
    """Defect 2: a kept alias sorting early must not consume a stale-work slot.

    The bounded candidate scan counted the run's own just-published keep alias
    toward the stale-work limit. Because that alias sorts early in the directory,
    it stole a work slot and left one backlog alias unexamined: with a work limit
    of 4 and reclaim cap _PRUNE_MAX_RECLAIMS_PER_RUN + len(keep) = 4, the old code
    reclaimed only 3 of the first 4 stale candidates. The fix skips the keep alias
    without charging the stale budget, so all 4 stale candidates in the window are
    examined and reclaimed. A pinned entry order puts the keep alias FIRST, which
    is the order-dependent worst case the CI regression only hits by luck.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(projection, "_PROJECTION_PRUNE_WORK_LIMIT", 4, raising=False)
    monkeypatch.setattr(projection, "_PRUNE_MAX_RECLAIMS_PER_RUN", 3)
    # The exact reclaim count below assumes the walk ends on the cap, not the clock.
    _hold_the_prune_clock(monkeypatch)
    backlog = [_legacy_alias(agents, f"{index:024x}") for index in range(8)]

    # The run's own alias is named by its view digest, so it is identified at
    # scan time as the one Crew-prefixed alias that is not part of the backlog.
    backlog_names = {path.name for path in backlog}
    real_scandir = projection.os.scandir
    normalized_agents = os.path.normcase(os.path.normpath(os.fspath(agents)))

    def keep_first_scandir(path, *args, **kwargs):
        try:
            same = os.path.normcase(os.path.normpath(os.fspath(path))) == normalized_agents
        except TypeError:
            same = False
        if not same:
            return real_scandir(path, *args, **kwargs)
        # Materialize the real entries, then force the run's own keep alias to the
        # front so it lands inside the bounded candidate window.
        entries = list(real_scandir(path, *args, **kwargs))
        entries.sort(
            key=lambda e: (
                0
                if e.name.startswith(projection.NATIVE_SKILL_ALIAS_PREFIX)
                and e.name.endswith(".json")
                and e.name not in backlog_names
                else 1
            )
        )

        class _Ctx:
            def __init__(self):
                self._entries = iter(entries)

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return None

            def __iter__(self):
                return self

            def __next__(self):
                return next(self._entries)

        return _Ctx()

    # Scoped: os.scandir is process-global, and the tmp-tree teardown (an
    # os.walk-based rmtree on Windows) must see the real one.
    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", keep_first_scandir)

        prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    # Exactly four backlog aliases reclaimed in the first run despite the keep
    # alias sorting first; the old keep-starved scan reclaimed only three.
    assert sum(1 for path in backlog if not path.exists()) == 4
    keep_stem = prepared.aliases["custom"]
    assert (agents / f"{keep_stem}.json").exists(), "the run's own alias must survive"


def test_prune_candidate_traversal_counts_unrelated_entries_and_caps_skip_credit(
    native_tree, monkeypatch
):
    """Neither arbitrary padding nor a large skip set can extend the walk."""
    _home, agents, _project = native_tree
    work_limit = 3
    skip = {f"kirocrew-skill-view-{0xB00 + index:024x}" for index in range(20)}
    names = [
        *(f"unrelated-{index}.json" for index in range(50)),
        *(f"{stem}.json" for stem in sorted(skip)),
        *(f"kirocrew-skill-view-{index:024x}.json" for index in range(2)),
    ]
    walked = 0

    class CountingEntries:
        def __init__(self):
            self._entries = iter(names)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal walked
            name = next(self._entries)
            walked += 1
            return SimpleNamespace(name=name)

    real_scandir = projection.os.scandir

    def counting_scandir(path, *args, **kwargs):
        if path == agents:
            return CountingEntries()
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(projection, "_PROJECTION_PRUNE_WORK_LIMIT", work_limit, raising=False)
    # Scoped: os.scandir is process-global, and the tmp-tree teardown (an
    # os.walk-based rmtree on Windows) must see the real one.
    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", counting_scandir)

        yielded = list(projection._projection_prune_candidates(agents, skip))
    assert yielded == []
    ceiling = (2 * work_limit) + work_limit
    assert walked <= ceiling + 1
    assert walked < len(names), "the prune walked the padded directory"

    spec = (Path(__file__).parents[1] / "docs/system-specs/modules/acp-client.md").read_text(
        encoding="utf-8"
    )
    normalized_spec = " ".join(spec.split())
    for contract in (
        "Reclaims, stale-candidate work, and total traversal have separate PER RUN ceilings",
        "every directory entry consumes the separately bounded traversal budget",
        "skip credit is capped at one work-limit",
        "the ceiling guarantees bounded entry traversal rather than eventual drain",
    ):
        assert contract in normalized_spec


def test_deeply_nested_ownership_sidecar_reads_as_unowned_in_the_prune(
    native_tree, cyclic_gc_quiesced
):
    """A recursive JSON shape is unreadable ownership, not a prune exception."""
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    assert first is not None
    alias_path = _alias_file(agents, first)
    metadata_path = _metadata_file(agents, first)
    del first
    gc.collect()

    metadata_path.write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
    alias_raw = alias_path.read_bytes()

    assert projection._managed_metadata_for_alias(agents, alias_path, alias_raw) is None
    projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())
    assert alias_path.read_bytes() == alias_raw


def test_alias_has_external_lease_product_symbol_is_removed(native_tree):
    """Defect 6: the zero-consumer product wrapper is deleted, not merely unused.

    Its only callers were tests, which now exercise _scan_projection_leases
    directly. Leaving it in place kept misleading "for direct callers"
    documentation alive, so its removal is part of the fix.
    """
    assert not hasattr(projection, "_alias_has_external_lease")
    # The batch scan the tests now use directly is present and callable.
    _home, agents, _project = native_tree
    live, uncertain = projection._scan_projection_leases(agents)
    assert live == set() and uncertain is False


def agents_dir(native_tree):
    return native_tree[1]


def test_prune_classification_seam_is_the_production_step(native_tree):
    """The per-candidate unit a test observes is the production classification.

    Kept, active and leased names never reach the budgeted loop, so the unit it
    pays for is the ownership classification in ``_reclaim_prune_candidate``.
    """
    _home, agents, _project = native_tree
    backlog = _legacy_alias(agents, "c" * 24)
    assert (
        projection._reclaim_prune_candidate(
            agents, backlog, _crew_home_id(), projection._ViewLedgerWrites(agents, _crew_home_id())
        )
        is True
    )
    assert not backlog.exists()


def test_capped_lease_scan_reports_its_ceiling_and_reclaimed_count(
    native_tree, monkeypatch, caplog
):
    """A capped scan authorizes no alias deletion, so it has to say it capped.

    Otherwise an incremental drain is invisible: the prune returns before its own
    reclaim log, and per-lease reclaims are DEBUG only.
    """
    _home, agents, _project = native_tree
    lease_dir = projection._projection_lease_dir()
    lease_dir.mkdir(parents=True)
    for n in range(3):
        projection.atomic_write(
            lease_dir / f"residue-{n}.json",
            json.dumps({"aliases": [f"kirocrew-skill-view-{n}"]}),
            restrict_to_owner=True,
        )
        projection.atomic_write(lease_dir / f"residue-{n}.hold", "", restrict_to_owner=True)
    monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 3, raising=False)

    with caplog.at_level("INFO", logger=projection.logger.name):
        live, uncertain = projection._scan_projection_leases(agents)

    assert live == set() and uncertain is True
    remaining = {p.name for p in lease_dir.glob("*.json")}
    reclaimed = 3 - len(remaining)
    assert f"lease scan reached its 3-entry ceiling; reclaimed {reclaimed} stale" in caplog.text


def test_lease_scan_stops_on_its_deadline_and_answers_uncertain(native_tree, monkeypatch, caplog):
    """A count alone cannot bound wall-clock time under the publication lock.

    The entry cap bounds how many parse-plus-lock-probe steps run, not how long a
    slow filesystem takes over them, so the scan also checks a deadline between
    entries. Expiry is treated like the cap: residue validated before it drains,
    nothing past it is examined, and the pass answers uncertain so no alias is
    deleted. The clock jumps past the budget once the first entry has been
    examined, so no sleep is needed.
    """
    _home, agents, _project = native_tree
    lease_dir = projection._projection_lease_dir()
    lease_dir.mkdir(parents=True)
    for n in range(3):
        projection.atomic_write(
            lease_dir / f"residue-{n}.json",
            json.dumps({"aliases": [f"kirocrew-skill-view-{n}"]}),
            restrict_to_owner=True,
        )
        projection.atomic_write(lease_dir / f"residue-{n}.hold", "", restrict_to_owner=True)
    budget = projection._PROJECTION_LEASE_SCAN_MAX_SECONDS
    calls = [0]

    def clock():
        # The deadline read and the first entry's check see t=0; every later
        # check sees the budget spent.
        calls[0] += 1
        return 0.0 if calls[0] <= 2 else budget

    probed: list[str] = []
    real_probe = projection._probe_projection_lease

    def probe(path):
        probed.append(path.name)
        return real_probe(path)

    # The schedule is keyed on call count, so ONLY the module under test may read
    # it: patching ``time.monotonic`` itself hands the same three slots to every
    # other thread in the worker (the subprocess-pool reaper polls it every
    # 0.5 s), and one consumed zero makes the first entry's check read the budget
    # as already spent -- the scan then reports 0 entries where the test expects 1.
    real_time = projection.time

    class _ModuleClock:
        monotonic = staticmethod(clock)

        def __getattr__(self, name):
            return getattr(real_time, name)

    monkeypatch.setattr(projection, "time", _ModuleClock())
    monkeypatch.setattr(projection, "_probe_projection_lease", probe)

    with caplog.at_level("INFO", logger=projection.logger.name):
        live, uncertain = projection._scan_projection_leases(agents)

    assert live == set() and uncertain is True
    assert len(probed) <= 1, "the scan probed a lease after its deadline was spent"
    assert "lease scan spent its 0.4-second budget after 1 entr(ies)" in caplog.text
    remaining = {p.name for p in lease_dir.iterdir()}
    assert len(remaining) >= 4, "the scan reclaimed residue it never examined"


def test_candidate_walk_deferral_is_logged_at_info(native_tree, monkeypatch, caplog):
    """Past the candidate window an alias is not promised a turn, so say so at INFO.

    The lease-scan ceiling is already INFO; the candidate-walk ceiling reports
    the same kind of deferral and must be visible the same way.
    """
    _home, agents, _project = native_tree
    for n in range(3):
        _legacy_alias(agents, f"{n:024x}")
    monkeypatch.setattr(projection, "_PROJECTION_PRUNE_WORK_LIMIT", 1)

    with caplog.at_level("INFO", logger=projection.logger.name):
        candidates = list(projection._projection_prune_candidates(agents, set()))

    assert len(candidates) == 1
    assert any(
        record.levelname == "INFO" and "deferred remaining alias pruning" in record.getMessage()
        for record in caplog.records
    )


def test_capped_lease_scan_counts_only_fully_removed_pairs(native_tree, monkeypatch, caplog):
    """A lease whose holder unlink fails is still half present, so it is not reclaimed.

    The cap log reports how much of a backlog drained; counting a pair whose
    ``.hold`` survived would report progress the next scan will not see.
    """
    _home, agents, _project = native_tree
    lease_dir = projection._projection_lease_dir()
    lease_dir.mkdir(parents=True)
    for n in range(3):
        projection.atomic_write(
            lease_dir / f"residue-{n}.json",
            json.dumps({"aliases": [f"kirocrew-skill-view-{n}"]}),
            restrict_to_owner=True,
        )
        projection.atomic_write(lease_dir / f"residue-{n}.hold", "", restrict_to_owner=True)
    monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 4, raising=False)
    real_unlink = projection._unlink_projection_lease_if_unchanged

    def holder_unlink_fails(path, identity):
        if path.name.endswith(projection._PROJECTION_LEASE_HOLDER_SUFFIX):
            return False
        return real_unlink(path, identity)

    monkeypatch.setattr(projection, "_unlink_projection_lease_if_unchanged", holder_unlink_fails)

    with caplog.at_level("INFO", logger=projection.logger.name):
        live, uncertain = projection._scan_projection_leases(agents)

    assert live == set() and uncertain is True
    # Every holder survived its failed unlink, so each record is kept too: the
    # pair is retried whole by the next scan and no pair counts as reclaimed.
    assert len(list(lease_dir.glob("*.json"))) == 3
    assert len(list(lease_dir.glob("*.hold"))) == 3
    assert "lease scan reached its 4-entry ceiling; reclaimed 0 stale" in caplog.text


def test_unreadable_lease_record_still_drains_validated_residue(native_tree, monkeypatch, caplog):
    """One persistent bad record must not stop stale-lease cleanup forever.

    The pass stays uncertain, so no alias is deleted, but the residue it already
    lock-validated is reclaimed and the blocking record is reported at INFO.
    """
    _home, agents, _project = native_tree
    lease_dir = projection._projection_lease_dir()
    lease_dir.mkdir(parents=True)
    stale = lease_dir / "a-stale.json"
    stale_holder = lease_dir / "a-stale.hold"
    projection.atomic_write(
        stale, json.dumps({"aliases": ["kirocrew-skill-view-stale"]}), restrict_to_owner=True
    )
    projection.atomic_write(stale_holder, "", restrict_to_owner=True)
    bad = lease_dir / "b-bad.json"
    projection.atomic_write(bad, "not json", restrict_to_owner=True)
    projection.atomic_write(lease_dir / "b-bad.hold", "", restrict_to_owner=True)
    real_scandir = projection.os.scandir

    def ordered_scandir(path, *args, **kwargs):
        if Path(path) == lease_dir:
            names = [stale.name, stale_holder.name, bad.name, "b-bad.hold"]

            class Entries:
                def __enter__(self):
                    return iter(SimpleNamespace(name=name) for name in names)

                def __exit__(self, *_args):
                    return None

            return Entries()
        return real_scandir(path, *args, **kwargs)

    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", ordered_scandir)
        with caplog.at_level("INFO", logger=projection.logger.name):
            live, uncertain = projection._scan_projection_leases(agents)

    assert live == set() and uncertain is True
    assert not stale.exists() and not stale_holder.exists()
    assert bad.exists()
    assert "lease record b-bad.json is unreadable; reclaimed 1 stale lease(s)" in caplog.text


def test_unreadable_lease_record_does_not_hide_stale_leases_after_it(native_tree, monkeypatch):
    """A bad record EARLY in directory order must not stop the walk.

    Stopping at the first unreadable record left every stale lease behind it
    unreclaimed on every stable-order scan, for as long as the bad record stayed.
    The pass stays uncertain, so no alias is deleted, but the stale lease after
    the bad record is reclaimed.
    """
    _home, agents, _project = native_tree
    lease_dir = projection._projection_lease_dir()
    lease_dir.mkdir(parents=True)
    bad = lease_dir / "a-bad.json"
    projection.atomic_write(bad, "not json", restrict_to_owner=True)
    projection.atomic_write(lease_dir / "a-bad.hold", "", restrict_to_owner=True)
    stale = lease_dir / "b-stale.json"
    stale_holder = lease_dir / "b-stale.hold"
    projection.atomic_write(
        stale, json.dumps({"aliases": ["kirocrew-skill-view-stale"]}), restrict_to_owner=True
    )
    projection.atomic_write(stale_holder, "", restrict_to_owner=True)
    real_scandir = projection.os.scandir

    def ordered_scandir(path, *args, **kwargs):
        if Path(path) == lease_dir:
            names = [bad.name, "a-bad.hold", stale.name, stale_holder.name]

            class Entries:
                def __enter__(self):
                    return iter(SimpleNamespace(name=name) for name in names)

                def __exit__(self, *_args):
                    return None

            return Entries()
        return real_scandir(path, *args, **kwargs)

    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", ordered_scandir)
        live, uncertain = projection._scan_projection_leases(agents)

    assert live == set() and uncertain is True
    assert not stale.exists() and not stale_holder.exists()
    assert bad.exists()


def test_record_less_holder_litter_is_reclaimed_and_cannot_pin_the_cap(native_tree, monkeypatch):
    """A ``.hold`` whose record is gone names no alias but consumes the ceiling.

    Left alone, enough of it would cap every scan and defer pruning forever, so
    an unlocked record-less holder is reclaimed as residue. A held one is kept.
    """
    _home, agents, _project = native_tree
    lease_dir = projection._projection_lease_dir()
    lease_dir.mkdir(parents=True)
    orphans = [lease_dir / f"{1000 + n}-{uuid.uuid4().hex}.hold" for n in range(4)]
    for orphan in orphans:
        projection.atomic_write(orphan, "", restrict_to_owner=True)
    held = projection._acquire_projection_lease(agents, {"kirocrew-skill-view-live"})
    try:
        monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 3, raising=False)
        drained = 0
        for _ in range(4):
            live, uncertain = projection._scan_projection_leases(agents)
            if not uncertain:
                break
            drained += 1
        assert not any(orphan.exists() for orphan in orphans)
        assert live == {"kirocrew-skill-view-live"} and uncertain is False
        assert drained >= 1, "the ceiling was never reached, so the litter was not exercised"
        # The live lease's own pair is untouched.
        assert len(list(lease_dir.glob("*.json"))) == 1
        assert len(list(lease_dir.glob("*.hold"))) == 1
    finally:
        held.close()


def test_record_less_holder_reclaim_spares_files_the_writer_did_not_name(native_tree):
    """Only a ``<pid>-<uuid4 hex>`` holder is ever reclaimed as record-less litter.

    An empty ``.hold`` carries no proof that this module wrote it, so a file
    such as an operator's ``notes.hold`` in the lease directory must survive a
    scan; the old head unlinked every unlocked record-less ``*.hold``. A
    writer-shaped stem with content survives too: the writer only publishes
    empty holders, and head ``f0ecf1e46`` unlinked it on the name alone.
    """
    _home, agents, _project = native_tree
    lease_dir = projection._projection_lease_dir()
    lease_dir.mkdir(parents=True)
    foreign = [
        lease_dir / "notes.hold",
        lease_dir / f"{uuid.uuid4().hex}.hold",
        lease_dir / f"12-{uuid.uuid4().hex.upper()}.hold",
        lease_dir / f"12-{uuid.uuid4().hex}-copy.hold",
    ]
    # The writer's exact stem shape, but with content: the module only ever
    # publishes EMPTY holders, so the name alone does not prove authorship.
    foreign.append(lease_dir / f"34-{uuid.uuid4().hex}.hold")
    for path in foreign:
        path.write_text("operator data", encoding="utf-8")
    ours = lease_dir / f"12-{uuid.uuid4().hex}.hold"
    projection.atomic_write(ours, "", restrict_to_owner=True)

    live, uncertain = projection._scan_projection_leases(agents)

    assert live == set() and uncertain is False
    assert not ours.exists()
    for path in foreign:
        assert path.read_text(encoding="utf-8") == "operator data", path.name


def test_census_truncates_wherever_the_reclaim_lease_scan_caps(native_tree, monkeypatch):
    """The census and the reclaim scan share one entry-level lease ceiling.

    The scan charges every lease-directory entry (records AND ``.hold``
    sidecars), so a directory of held pairs can cap it -- and defer every
    prune -- while the record count alone is still under the census bound.
    The census must then report ``truncated`` instead of promising a drain.
    """
    _home, agents, _project = native_tree
    projection.atomic_write(
        agents / "kirocrew-skill-view-orphan.json", "{}", restrict_to_owner=True
    )
    leases = [
        projection._acquire_projection_lease(agents, {f"kirocrew-skill-view-live{n}"})
        for n in range(3)
    ]
    try:
        # Three records (under the census's own record bound) but six entries.
        monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 4, raising=False)
        _live, uncertain = projection._scan_projection_leases(agents)
        assert uncertain is True
        census = projection.census_projected_aliases(agents)
        assert census["truncated"] == 1
    finally:
        for lease in leases:
            lease.close()


# ── A refused unlink is reported, not swallowed ──


_dir_fd_seam_only = pytest.mark.skipif(
    os.name != "posix",
    reason="these drive the dir_fd unlink seam; Windows unlinks by name and reports the "
    "parent open instead, so the injected errno never reaches the reporter there",
)


def _seed_backlog(agents, count):
    """*count* reclaimable legacy aliases, backdated past the minimum age."""
    return [_legacy_alias(agents, f"{n:024x}") for n in range(count)]


@_dir_fd_seam_only
def test_a_refused_unlink_is_reported_once_per_interval_with_its_count(
    native_tree, monkeypatch, caplog
):
    """Silence here is what sent an outside report after the wrong root cause.

    A permission or read-only refusal answering ``False`` like a deliberate keep,
    with nothing logged, makes an agents directory this process cannot write look
    exactly like one with nothing to reclaim. One warning per interval,
    carrying how many refusals it stands for -- never one line per file.
    """
    _home, agents, _project = native_tree
    backlog = _seed_backlog(agents, 5)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_LAST", 0.0)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_SUPPRESSED", 0)
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", False)

    def refuse(*args, on_error=None, **kwargs):
        # What pinned_fs.unlink_verified does when the OS refuses the unlink
        # itself: report through the seam, answer False.
        assert on_error is not None, "the prune did not ask to hear about a refusal"
        on_error(PermissionError(13, "Permission denied"))
        return False

    monkeypatch.setattr(projection.pinned_fs, "unlink_verified", refuse)
    monkeypatch.setattr(projection.pinned_fs, "supports_pinned_walk", lambda: True)
    monkeypatch.setattr(projection.os, "supports_dir_fd", {projection.os.unlink})
    with caplog.at_level("WARNING", logger=projection.logger.name):
        projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())
        warnings = [r for r in caplog.records if "cannot remove stale alias" in r.getMessage()]
        assert len(warnings) == 1, "a refused unlink was either silent or logged per file"
        assert "unlink refused, EACCES (Permission denied)" in warnings[0].getMessage()
        assert "is not writable by this process" in warnings[0].getMessage()
        assert "0 similar refusal(s)" in warnings[0].getMessage()
        assert all(p.exists() for p in backlog)

        # Past the interval the next refusal reports again, carrying the ones it
        # swallowed in between.
        monkeypatch.setattr(
            projection,
            "_UNLINK_WARNING_LAST",
            time.monotonic() - projection._UNLINK_WARNING_INTERVAL_SECS - 1,
        )
        projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())
        warnings = [r for r in caplog.records if "cannot remove stale alias" in r.getMessage()]
        assert len(warnings) == 2
        assert "4 similar refusal(s)" in warnings[1].getMessage()


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (FileNotFoundError(errno.ENOENT, "No such file or directory"), "ENOENT"),
        (OSError(errno.EMFILE, "Too many open files"), "EMFILE"),
        (OSError(errno.EIO, "Input/output error"), "EIO"),
    ],
)
@_dir_fd_seam_only
def test_a_refusal_that_is_not_a_permission_problem_is_worded_neutrally(
    native_tree, monkeypatch, caplog, exc, code
):
    """Only EACCES/EPERM/EROFS mean the directory is unwritable.

    A prune in another process can take the file between the walk's stat and this
    unlink (ENOENT), or the process can be out of descriptors (EMFILE); calling
    either "the directory is not writable" sends an operator to fix a mount or an
    owner that is fine. The line still says what failed and with which errno,
    through the same throttle and count.
    """
    _home, agents, _project = native_tree
    backlog = _seed_backlog(agents, 2)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_LAST", 0.0)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_SUPPRESSED", 0)
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", False)

    def refuse(*args, on_error=None, **kwargs):
        assert on_error is not None
        on_error(exc)
        return False

    monkeypatch.setattr(projection.pinned_fs, "unlink_verified", refuse)
    monkeypatch.setattr(projection.pinned_fs, "supports_pinned_walk", lambda: True)
    monkeypatch.setattr(projection.os, "supports_dir_fd", {projection.os.unlink})
    with caplog.at_level("WARNING", logger=projection.logger.name):
        projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())
    warnings = [r for r in caplog.records if "cannot remove stale alias" in r.getMessage()]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert f"unlink refused, {code} ({exc.strerror})" in message
    assert str(agents) in message and "0 similar refusal(s)" in message
    assert "not writable" not in message
    assert projection._UNLINK_WARNING_SUPPRESSED == 1, "the second refusal was not counted"
    assert all(p.exists() for p in backlog)


@_dir_fd_seam_only
@pytest.mark.parametrize("code", [errno.EPERM, errno.EROFS])
def test_every_permission_class_errno_is_diagnosed_as_unwritable(
    native_tree, monkeypatch, caplog, code
):
    _home, agents, _project = native_tree
    _seed_backlog(agents, 1)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_LAST", 0.0)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_SUPPRESSED", 0)
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", False)

    def refuse(*args, on_error=None, **kwargs):
        on_error(OSError(code, os.strerror(code)))
        return False

    monkeypatch.setattr(projection.pinned_fs, "unlink_verified", refuse)
    monkeypatch.setattr(projection.pinned_fs, "supports_pinned_walk", lambda: True)
    monkeypatch.setattr(projection.os, "supports_dir_fd", {projection.os.unlink})
    with caplog.at_level("WARNING", logger=projection.logger.name):
        projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())
    (warning,) = [r for r in caplog.records if "cannot remove stale alias" in r.getMessage()]
    assert errno.errorcode[code] in warning.getMessage()
    assert "is not writable by this process" in warning.getMessage()


@_dir_fd_seam_only
def test_a_refused_lease_unlink_reports_through_the_same_throttle(native_tree, monkeypatch, caplog):
    """The lease and sidecar unlinks share the alias reporter, not a second one.

    The same read-only or foreign-owned directory that refuses an alias unlink
    refuses its lease record, holder and ownership sidecar, and a rule applied
    to one of two paths is the same silence on the other. One reporter, one
    interval, one suppressed count: a refused lease unlink is the first line,
    the refused alias unlinks in the same interval are the count on the next.
    """
    _home, agents, _project = native_tree
    backlog = _seed_backlog(agents, 3)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_LAST", 0.0)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_SUPPRESSED", 0)
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", False)

    def refuse(*args, on_error=None, **kwargs):
        assert on_error is not None, "the lease unlink did not ask to hear about a refusal"
        on_error(PermissionError(13, "Permission denied"))
        return False

    monkeypatch.setattr(projection.pinned_fs, "unlink_verified", refuse)
    monkeypatch.setattr(projection.pinned_fs, "supports_pinned_walk", lambda: True)
    monkeypatch.setattr(projection.os, "supports_dir_fd", {projection.os.unlink})
    # Off the prune's walk, so the count below is exactly the alias refusals.
    lease_dir = agents.parent / "elsewhere" / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir(parents=True)
    record = lease_dir / f"1-{'ab' * 16}{projection._PROJECTION_LEASE_RECORD_SUFFIX}"
    record.write_text('{"aliases":[]}')
    info = os.stat(record)
    identity = (info.st_dev, info.st_ino)
    with caplog.at_level("WARNING", logger=projection.logger.name):
        assert (
            projection._unlink_projection_lease_if_unchanged(
                record, identity, what="stale lease record"
            )
            is False
        )
        warnings = [r for r in caplog.records if "cannot remove" in r.getMessage()]
        assert len(warnings) == 1, "a refused lease unlink was silent"
        message = warnings[0].getMessage()
        assert "stale lease record" in message and record.name in message
        assert "unlink refused, EACCES (Permission denied)" in message
        assert "is not writable by this process" in message and str(lease_dir) in message
        assert "0 similar refusal(s)" in message
        assert record.exists()

        # Within the interval the alias refusals are swallowed into the SAME count.
        projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())
        assert len([r for r in caplog.records if "cannot remove" in r.getMessage()]) == 1
        assert all(p.exists() for p in backlog)
        monkeypatch.setattr(
            projection,
            "_UNLINK_WARNING_LAST",
            time.monotonic() - projection._UNLINK_WARNING_INTERVAL_SECS - 1,
        )
        assert (
            projection._unlink_projection_lease_if_unchanged(
                record, identity, what="stale lease record"
            )
            is False
        )
        warnings = [r for r in caplog.records if "cannot remove" in r.getMessage()]
        assert len(warnings) == 2
        assert "3 similar refusal(s)" in warnings[1].getMessage()

    # An identity change is a deliberate keep and stays silent: no report, no count.
    caplog.clear()
    changed = (identity[0], identity[1] + 1)
    with caplog.at_level("WARNING", logger=projection.logger.name):
        assert projection._unlink_projection_lease_if_unchanged(record, changed) is False
    assert not [r for r in caplog.records if "cannot remove" in r.getMessage()]
    assert projection._UNLINK_WARNING_SUPPRESSED == 0


@pytest.mark.skipif(
    not projection.pinned_fs.supports_pinned_walk() or os.name != "posix",
    reason="a directory without write permission is a POSIX way to refuse an unlink",
)
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permission bits"
)
def test_a_read_only_agents_directory_produces_the_refusal_warning(
    native_tree, monkeypatch, caplog
):
    """End to end through the real unlink: the reported field case, with no seam.

    The prune classifies the alias as reclaimable, the kernel refuses the unlink,
    and the warning names the directory -- which is what an operator needs to
    stop looking for a code bug and fix a mount or an owner.
    """
    _home, agents, _project = native_tree
    backlog = _seed_backlog(agents, 3)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_LAST", 0.0)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_SUPPRESSED", 0)
    original_mode = stat.S_IMODE(os.stat(agents).st_mode)
    os.chmod(agents, original_mode & ~stat.S_IWUSR)
    try:
        with caplog.at_level("WARNING", logger=projection.logger.name):
            projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())
    finally:
        os.chmod(agents, original_mode)
    warnings = [r for r in caplog.records if "cannot remove stale alias" in r.getMessage()]
    assert len(warnings) == 1
    assert str(agents) in warnings[0].getMessage()
    # The kernel's real answer is EACCES, the one case the line may diagnose as
    # an unwritable directory.
    assert "EACCES" in warnings[0].getMessage()
    assert "is not writable by this process" in warnings[0].getMessage()
    assert all(p.exists() for p in backlog)


@pytest.mark.parametrize(
    ("workspace", "global_value"),
    [
        ({}, None),
        ({"chat.disableInheritingDefaultResources": True}, None),
        ({}, True),
        ({"chat.disableInheritingDefaultResources": False}, True),
        ({"chat.disableInheritingDefaultResources": "true"}, None),
    ],
)
def test_reader_agrees_with_the_projection_across_its_overlay(native_tree, workspace, global_value):
    """Readers that mirror native loading must decode Crew's overlay as the projection wrote it.

    After the projection runs, the workspace's native key reads ``true`` whatever
    the user chose, so a reader of the raw key would see an opt-out everywhere.
    """
    home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    if workspace:
        settings = project / ".kiro" / "settings" / "cli.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps(workspace), encoding="utf-8")
    if global_value is not None:
        settings = home / "settings" / "cli.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            json.dumps({"chat.disableInheritingDefaultResources": global_value}), encoding="utf-8"
        )
    before = projection.inherits_default_resources(project)
    prepared = projection.prepare_native_skill_projection(project)
    projected = any("steering" in item for item in prepared.specs["custom"]["resources"])
    assert before is projected
    assert projection.inherits_default_resources(project) is projected


def test_reader_follows_a_live_global_preference_under_the_overlay(native_tree):
    home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    settings = home / "settings" / "cli.json"
    settings.parent.mkdir()
    settings.write_text('{"chat.disableInheritingDefaultResources":true}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    assert projection.inherits_default_resources(project) is False
    settings.write_text('{"chat.disableInheritingDefaultResources":false}', encoding="utf-8")
    assert projection.inherits_default_resources(project) is True


class TestSettingsReadRetry:
    """A transiently refused ``cli.json`` read must not fail a new chat.

    The gated read of a healthy settings file answers ``None`` when a writer
    renames a new file over the one that was opened, or when the sensitive-path
    resolver misses its budget under load. ``_settings`` retries through
    a bounded retry; a read that stays refused still
    raises.
    """

    @staticmethod
    def _flaky_reader(monkeypatch, refusals):
        real = projection.safe_read_file_bytes
        calls = []

        def flaky(raw):
            calls.append(raw)
            return None if len(calls) <= refusals else real(raw)

        monkeypatch.setattr(projection, "safe_read_file_bytes", flaky)
        return calls

    def test_one_refusal_then_bytes_prepares_the_projection(self, native_tree, monkeypatch):
        home, agents, project = native_tree
        (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
        global_settings = home / "settings" / "cli.json"
        global_settings.parent.mkdir(parents=True)
        global_settings.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            projection,
            "_settings_read_sleep",
            lambda secs: pytest.fail(f"paused {secs}s for a re-check"),
        )
        calls = self._flaky_reader(monkeypatch, refusals=1)

        prepared = projection.prepare_native_skill_projection(project)

        assert prepared is not None and prepared.agent("custom")
        assert calls[0] == calls[1] == str(global_settings)

    def test_rollback_with_one_refusal_still_restores(self, native_tree, monkeypatch):
        # KIROCREW_NATIVE_SKILL_PROJECTION=0 still reads the workspace cli.json
        # during rollback, so the retry must cover that path too.
        _home, agents, project = native_tree
        (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
        settings = project / ".kiro/settings/cli.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("{}", encoding="utf-8")
        projection.prepare_native_skill_projection(project)
        monkeypatch.setattr(projection, "_settings_read_sleep", lambda secs: None)
        monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
        self._flaky_reader(monkeypatch, refusals=1)

        assert projection.prepare_native_skill_projection(project) is None
        assert json.loads(settings.read_text(encoding="utf-8")) == {}

    def test_a_refusal_that_clears_after_the_pause_is_read(self, tmp_path, monkeypatch):
        path = tmp_path / "cli.json"
        path.write_text('{"a": 1}', encoding="utf-8")
        slept = []
        monkeypatch.setattr(projection, "_settings_read_sleep", slept.append)
        calls = self._flaky_reader(monkeypatch, refusals=2)

        assert projection._settings(path) == {"a": 1}
        assert len(calls) == 3
        assert slept == [2.0]

    def test_a_read_that_stays_refused_still_raises(self, native_tree, monkeypatch):
        home, agents, project = native_tree
        (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
        global_settings = home / "settings" / "cli.json"
        global_settings.parent.mkdir(parents=True)
        global_settings.write_text("{}", encoding="utf-8")
        slept = []
        monkeypatch.setattr(projection, "_settings_read_sleep", slept.append)
        calls = self._flaky_reader(monkeypatch, refusals=10**6)

        with pytest.raises(ValueError, match="Cannot read Kiro settings"):
            projection.prepare_native_skill_projection(project)
        # First attempt, immediate re-check, one pause, final attempt.
        assert len(calls) == 3
        assert slept == [2.0]

    def test_a_missing_file_reads_empty_without_retry(self, tmp_path, monkeypatch):
        calls = self._flaky_reader(monkeypatch, refusals=10**6)
        assert projection._settings(tmp_path / "absent.json") == {}
        assert calls == []

    def test_an_empty_file_is_not_mistaken_for_absence(self, tmp_path):
        path = tmp_path / "cli.json"
        path.write_bytes(b"")
        with pytest.raises(ValueError):
            projection._settings(path)

    @pytest.mark.asyncio
    async def test_on_the_event_loop_one_attempt_runs_and_never_pauses(self, tmp_path, monkeypatch):
        path = tmp_path / "cli.json"
        path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            projection,
            "_settings_read_sleep",
            lambda secs: pytest.fail(f"paused {secs}s on the loop"),
        )
        calls = self._flaky_reader(monkeypatch, refusals=1)

        with pytest.raises(ValueError, match="Cannot read Kiro settings"):
            projection._settings(path)
        assert len(calls) == 1


class TestLockedSettingsReadNeverPauses:
    """A read under the projection's cross-process locks never sleeps.

    Contenders for the alias and ``cli.json`` locks give up after 2 s, so a 2 s
    pause while holding them would drop a concurrent spawn's projection.
    """

    def test_locked_read_rechecks_once_and_never_pauses(self, native_tree, monkeypatch):
        _home, agents, project = native_tree
        (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
        settings = project / ".kiro/settings/cli.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("{}", encoding="utf-8")
        real = projection.safe_read_file_bytes
        refused = []

        def refuse_local_once(raw):
            if raw == str(settings) and not refused:
                refused.append(raw)
                return None
            return real(raw)

        monkeypatch.setattr(projection, "safe_read_file_bytes", refuse_local_once)
        monkeypatch.setattr(
            projection,
            "_settings_read_sleep",
            lambda secs: pytest.fail(f"paused {secs}s while holding the projection locks"),
        )

        prepared = projection.prepare_native_skill_projection(project)

        assert refused == [str(settings)]
        assert prepared is not None and prepared.agent("custom")

    def test_locked_read_that_stays_refused_raises_without_pausing(self, tmp_path, monkeypatch):
        path = tmp_path / "cli.json"
        path.write_text("{}", encoding="utf-8")
        calls = []

        def refused(raw):
            calls.append(raw)
            return None

        monkeypatch.setattr(projection, "safe_read_file_bytes", refused)
        monkeypatch.setattr(
            projection, "_settings_read_sleep", lambda secs: pytest.fail(f"paused {secs}s")
        )
        with pytest.raises(ValueError, match="Cannot read Kiro settings"):
            projection._settings(path, may_pause=False)
        assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("refreshed_alias", ["same", "changed", None])
async def test_set_mode_sends_the_fresh_alias_never_a_changed_spawn_one(
    monkeypatch, tmp_path, refreshed_alias
):
    """``set_mode`` names the alias just prepared, never an older one.

    The alias kiro-cli loaded at spawn may hold a generation of the spec an edit
    has since revoked grants from, so a changed view sends the FRESH alias (the
    reload, retry and refusal for a host that has not loaded it are pinned in
    test_acp_set_mode_projection_fallback.py). A re-preparation that could not run
    (``None``) fails the start and sends nothing. Either way the adopted projection
    still recognises the spawn alias inbound.
    """
    import types

    SPAWN_ALIAS = projection.NATIVE_SKILL_ALIAS_PREFIX + "1" * 24
    CHANGED_ALIAS = projection.NATIVE_SKILL_ALIAS_PREFIX + "2" * 24
    OTHER_ALIAS = projection.NATIVE_SKILL_ALIAS_PREFIX + "3" * 24

    import kiro_crew.agent as agent_module
    from kiro_crew.acp import runtime as runtime_module

    spawn = projection.NativeSkillProjection({"crew": SPAWN_ALIAS})
    if refreshed_alias == "same":
        fresh = projection.NativeSkillProjection({"crew": SPAWN_ALIAS, "other": OTHER_ALIAS})
    elif refreshed_alias == "changed":
        fresh = projection.NativeSkillProjection({"crew": CHANGED_ALIAS})
    else:
        fresh = None
    monkeypatch.setattr(projection, "prepare_native_skill_projection", lambda *a, **k: fresh)
    monkeypatch.setattr(agent_module, "require_fresh_derived_spec", lambda *a, **k: None)
    monkeypatch.setattr(agent_module, "require_unchanged_derived_spec", lambda *a, **k: None)

    sent: list[dict] = []
    terminated: list[str] = []

    class Stub:
        _native_skill_projection = spawn
        _spawn_skill_projection = spawn
        _work_dir = tmp_path
        # The bracket's own projection-ordering helpers, borrowed unchanged.
        _skill_projection_lock = runtime_module.AcpRuntime._skill_projection_lock
        _issue_skill_projection_generation = (
            runtime_module.AcpRuntime._issue_skill_projection_generation
        )
        _adopted_skill_projection_generation = (
            runtime_module.AcpRuntime._adopted_skill_projection_generation
        )
        _adopt_skill_projection = runtime_module.AcpRuntime._adopt_skill_projection
        _resolve_start_alias = runtime_module.AcpRuntime._resolve_start_alias
        _superseding_alias = runtime_module.AcpRuntime._superseding_alias
        _refuse_if_view_superseded = runtime_module.AcpRuntime._refuse_if_view_superseded
        _refuse_if_view_unverified = runtime_module.AcpRuntime._refuse_if_view_unverified
        _note_unadopted_skill_projection = (
            runtime_module.AcpRuntime._note_unadopted_skill_projection
        )
        _unadopted_skill_projection_generation = (
            runtime_module.AcpRuntime._unadopted_skill_projection_generation
        )
        # The launched agent is some OTHER agent, not "crew": "crew" is a prepared
        # agent (it has an alias), so activating it must take the strict alias path
        # this test asserts, not the launched-agent allowance.
        _agent = "launcher"

        async def terminate_session(self, sid):
            terminated.append(sid)

        async def _send_and_await(self, method, params, timeout=None, *, translate=True):
            sent.append(
                self._native_skill_projection.request(method, params) if translate else params
            )
            return {}

    stub = Stub()
    activate = types.MethodType(runtime_module.AcpRuntime._activate_mode_bracketed, stub)
    if fresh is None:
        with pytest.raises(runtime_module.AcpRuntimeError, match="could not be prepared"):
            await activate("sid", "crew", budget=5.0, payload_snapshot=None, wire_registered=False)
        assert sent == [] and terminated == ["sid"]
        assert stub._native_skill_projection is spawn
        return
    activated_alias = await activate(
        "sid", "crew", budget=5.0, payload_snapshot=None, wire_registered=False
    )

    assert not terminated
    assert activated_alias == fresh.aliases["crew"]
    assert sent[0]["modeId"] == fresh.aliases["crew"]
    assert stub._native_skill_projection is fresh
    listed = fresh.frame({"availableModes": [{"id": SPAWN_ALIAS}]})
    assert listed["availableModes"] == [{"id": "crew"}]


def test_announce_alias_rewrites_the_same_bytes_in_place(native_tree, monkeypatch):
    """The rescan nudge is a data write to the published inode that leaves its
    bytes unchanged, never a rename, and never touches a name this module does
    not mint or bytes that moved under it."""
    _home, agents, _project = native_tree
    alias = projection.NATIVE_SKILL_ALIAS_PREFIX + "c" * 24
    path = agents / f"{alias}.json"
    path.write_bytes(b'{"name": "x"}')
    inode = path.stat().st_ino
    writes: list[int] = []
    real_write = os.write
    monkeypatch.setattr(
        projection.os, "write", lambda fd, data: writes.append(len(data)) or real_write(fd, data)
    )

    projection.announce_alias(alias)
    assert path.read_bytes() == b'{"name": "x"}' and path.stat().st_ino == inode
    assert writes == [len(b'{"name": "x"}')]

    writes.clear()
    projection.announce_alias("not-an-alias")
    projection.announce_alias(projection.NATIVE_SKILL_ALIAS_PREFIX + "d" * 24)  # absent
    assert writes == []


def test_announce_alias_never_raises_on_a_failing_close(native_tree, monkeypatch):
    """A network filesystem can report a deferred write error at close; the
    rescan nudge is best effort and must not fail the session start behind it."""
    _home, agents, _project = native_tree
    alias = projection.NATIVE_SKILL_ALIAS_PREFIX + "e" * 24
    (agents / f"{alias}.json").write_bytes(b'{"name": "x"}')
    real_close = os.close

    def failing_close(fd):
        real_close(fd)
        raise OSError(5, "deferred write error")

    monkeypatch.setattr(projection.os, "close", failing_close)
    projection.announce_alias(alias)
