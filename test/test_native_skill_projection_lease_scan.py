"""The lease scan, the workspace settings lock, the census and the boot drain.

Crash-residue reclaim and fail-closed uncertainty in the lease scan, the workspace
``cli.json`` lock, the read-only census, the boot drain's batching and stop rule, and the
one batch lease scan per prune.
"""

from __future__ import annotations

import gc
import itertools
import json
import os
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import native_skill_projection_helpers
import pytest
from native_skill_projection_helpers import (
    _alias_file,
    _alias_is_live,
    _hold_the_prune_clock,
    _legacy_alias,
    _metadata_file,
)

from conftest import requires_symlinks
from kiro_crew.acp import skill_projection as projection

# The shared fixtures, bound here by name so pytest registers them for this module.
_no_eager_prune = native_skill_projection_helpers._no_eager_prune
_prune_on_every_spawn = native_skill_projection_helpers._prune_on_every_spawn
cyclic_gc_quiesced = native_skill_projection_helpers.cyclic_gc_quiesced
native_tree = native_skill_projection_helpers.native_tree


def test_lease_scan_reclaims_valid_unlocked_crash_residue(native_tree):
    _home, agents, _project = native_tree
    lease_dir = projection._projection_lease_dir()
    lease_dir.mkdir(parents=True)
    stale = lease_dir / "crashed.json"
    holder = lease_dir / "crashed.hold"
    projection.atomic_write(
        stale,
        json.dumps({"aliases": ["kirocrew-skill-view-stale"]}),
        restrict_to_owner=True,
    )
    projection.atomic_write(holder, "", restrict_to_owner=True)

    assert not _alias_is_live(agents, "kirocrew-skill-view-stale")
    assert not stale.exists()
    assert not holder.exists()


def test_lease_scan_does_not_mutate_before_finding_later_held_match(native_tree, monkeypatch):
    _home, agents, _project = native_tree
    alias = "kirocrew-skill-view-held"
    lease_dir = projection._projection_lease_dir()
    lease_dir.mkdir(parents=True)
    stale = lease_dir / "stale-first.json"
    stale_holder = lease_dir / "stale-first.hold"
    projection.atomic_write(
        stale,
        json.dumps({"aliases": ["kirocrew-skill-view-stale"]}),
        restrict_to_owner=True,
    )
    projection.atomic_write(stale_holder, "", restrict_to_owner=True)
    held = projection._acquire_projection_lease(agents, {alias})
    held_record = next(path for path in lease_dir.glob("*.json") if path != stale)

    class MutationSensitiveEntries:
        def __init__(self):
            self.active = False
            self.index = 0

        def __enter__(self):
            self.active = True
            return self

        def __exit__(self, *_args):
            self.active = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.index == 0:
                self.index += 1
                return SimpleNamespace(name=stale.name)
            if self.index == 1 and stale.exists():
                self.index += 1
                return SimpleNamespace(name=held_record.name)
            raise StopIteration

    entries = MutationSensitiveEntries()
    real_scandir = projection.os.scandir
    in_scan_unlinks = []
    real_unlink = projection._unlink_projection_lease_if_unchanged

    def mutation_sensitive_scandir(path, *args, **kwargs):
        if path == lease_dir:
            return entries
        return real_scandir(path, *args, **kwargs)

    def record_unlink(path, identity):
        if entries.active:
            in_scan_unlinks.append(path)
        return real_unlink(path, identity)

    try:
        with monkeypatch.context() as scan_patch:
            scan_patch.setattr(projection.os, "scandir", mutation_sensitive_scandir)
            scan_patch.setattr(projection, "_unlink_projection_lease_if_unchanged", record_unlink)
            # The alias is live because a held lease names it; the scan collects
            # that into its union rather than early-returning on the match.
            assert _alias_is_live(agents, alias) is True
            # The invariant that still holds: nothing is unlinked DURING the
            # active scandir iteration -- cleanup is deferred until it closes.
            assert in_scan_unlinks == []
        # Held leases preserve liveness without suppressing reclamation of residue
        # in the SAME clean scan: liveness is carried by the union, so after the
        # scandir closes the unlocked crash residue is reclaimed even though a
        # held lease was found later in the pass.
        assert not stale.exists() and not stale_holder.exists()
    finally:
        held.close()


def test_lease_scan_error_defers_unlocked_residue_cleanup(native_tree, monkeypatch):
    _home, agents, _project = native_tree
    lease_dir = projection._projection_lease_dir()
    lease_dir.mkdir(parents=True)
    stale = lease_dir / "stale-before-error.json"
    holder = lease_dir / "stale-before-error.hold"
    projection.atomic_write(
        stale,
        json.dumps({"aliases": ["kirocrew-skill-view-stale"]}),
        restrict_to_owner=True,
    )
    projection.atomic_write(holder, "", restrict_to_owner=True)

    class ErrorAfterFirstEntry:
        def __init__(self):
            self.yielded = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __iter__(self):
            return self

        def __next__(self):
            if not self.yielded:
                self.yielded = True
                return SimpleNamespace(name=stale.name)
            raise OSError("test scandir iteration error")

    real_scandir = projection.os.scandir
    normalized_lease_dir = os.path.normcase(os.path.normpath(os.fspath(lease_dir)))

    def error_after_first_entry_scandir(path, *args, **kwargs):
        try:
            normalized_path = os.path.normcase(os.path.normpath(os.fspath(path)))
        except TypeError:
            return real_scandir(path, *args, **kwargs)
        if normalized_path == normalized_lease_dir:
            return ErrorAfterFirstEntry()
        return real_scandir(path, *args, **kwargs)

    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", error_after_first_entry_scandir)
        assert _alias_is_live(agents, "kirocrew-skill-view-other") is True

    assert stale.exists() and holder.exists()


def test_lease_scan_cap_drains_validated_residue_before_answering_live(native_tree, monkeypatch):
    """A cap keeps the answer live but reclaims the residue it already validated.

    Hitting the record ceiling is uncertainty, so the alias is retained -- but the
    crash/finalizer residue the scan lock-validated BEFORE the cap has been proven
    stale and must not be absorbed. A prior build discarded it on any short scan,
    so a flood of records past the ceiling could permanently defer cleanup of
    leases already known dead. Only the records seen before the cap are reclaimed;
    a record beyond the ceiling is never scanned and never touched. This is the
    one behaviour that separates a cap from an open/iteration error, which trusts
    nothing it saw.
    """
    _home, agents, _project = native_tree
    lease_dir = projection._projection_lease_dir()
    lease_dir.mkdir(parents=True)
    stale = lease_dir / "stale-before-cap.json"
    stale_holder = lease_dir / "stale-before-cap.hold"
    projection.atomic_write(
        stale,
        json.dumps({"aliases": ["kirocrew-skill-view-stale"]}),
        restrict_to_owner=True,
    )
    projection.atomic_write(stale_holder, "", restrict_to_owner=True)
    beyond = lease_dir / "beyond-cap.json"
    beyond_holder = lease_dir / "beyond-cap.hold"
    projection.atomic_write(
        beyond,
        json.dumps({"aliases": ["kirocrew-skill-view-beyond"]}),
        restrict_to_owner=True,
    )
    projection.atomic_write(beyond_holder, "", restrict_to_owner=True)

    monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 1, raising=False)

    class OrderedEntries:
        def __init__(self):
            self.active = False
            self._entries = iter(())

        def __enter__(self):
            self.active = True
            self._entries = iter(
                (SimpleNamespace(name=stale.name), SimpleNamespace(name=beyond.name))
            )
            return self

        def __exit__(self, *_args):
            self.active = False

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._entries)

    entries = OrderedEntries()
    in_scan_unlinks = []
    real_scandir = projection.os.scandir
    real_unlink = projection._unlink_projection_lease_if_unchanged
    normalized_lease_dir = os.path.normcase(os.path.normpath(os.fspath(lease_dir)))

    def ordered_scandir(path, *args, **kwargs):
        try:
            normalized_path = os.path.normcase(os.path.normpath(os.fspath(path)))
        except TypeError:
            return real_scandir(path, *args, **kwargs)
        if normalized_path == normalized_lease_dir:
            return entries
        return real_scandir(path, *args, **kwargs)

    def record_unlink(path, identity):
        if entries.active:
            in_scan_unlinks.append(path)
        return real_unlink(path, identity)

    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", ordered_scandir)
        scan_patch.setattr(projection, "_unlink_projection_lease_if_unchanged", record_unlink)
        assert _alias_is_live(agents, "kirocrew-skill-view-query") is True

    # The residue validated before the cap is reclaimed only after iterator
    # closure; the record past the cap was never scanned and remains on disk.
    assert in_scan_unlinks == []
    assert not stale.exists() and not stale_holder.exists()
    assert beyond.exists() and beyond_holder.exists()


def test_lease_scan_overflow_is_uncertain_and_retains_stale_alias(native_tree, monkeypatch):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    stale = _alias_file(agents, first)
    del first
    gc.collect()
    source.unlink()

    # The scan ceiling bounds EVERY directory entry, not just the ``.json``
    # records (see _scan_projection_leases / defect: unrelated or ``.hold``
    # entries once bypassed the cap and could force an unbounded walk). With the
    # entries ordered record, holder, record, holder, ... a ceiling of 3 admits
    # residue-0's record and holder plus residue-1's record before it trips, so
    # exactly two records are probed and residue past that is never scanned.
    monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 3, raising=False)
    lease_dir = projection._projection_lease_dir()
    lease_dir.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        stem = f"residue-{index}"
        (lease_dir / f"{stem}{projection._PROJECTION_LEASE_RECORD_SUFFIX}").write_text(
            json.dumps({"aliases": [f"other-{index}"]}), encoding="utf-8"
        )
        (lease_dir / f"{stem}{projection._PROJECTION_LEASE_HOLDER_SUFFIX}").write_text(
            "", encoding="utf-8"
        )

    # Pin the entry order so the ceiling's interaction with the record/holder
    # interleave is deterministic rather than dependent on filesystem scandir
    # order. Records and holders alternate, matching on-disk creation order.
    ordered_names = []
    for index in range(3):
        ordered_names.append(f"residue-{index}{projection._PROJECTION_LEASE_RECORD_SUFFIX}")
        ordered_names.append(f"residue-{index}{projection._PROJECTION_LEASE_HOLDER_SUFFIX}")

    class OrderedEntries:
        def __init__(self):
            self._entries = iter(SimpleNamespace(name=name) for name in ordered_names)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._entries)

    real_scandir = projection.os.scandir
    normalized_lease_dir = os.path.normcase(os.path.normpath(os.fspath(lease_dir)))

    def ordered_scandir(path, *args, **kwargs):
        try:
            same = os.path.normcase(os.path.normpath(os.fspath(path))) == normalized_lease_dir
        except TypeError:
            same = False
        if same:
            return OrderedEntries()
        return real_scandir(path, *args, **kwargs)

    # Scoped: os.scandir is process-global, and the tmp-tree teardown (an
    # os.walk-based rmtree on Windows) must see the real one.
    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", ordered_scandir)

        holder_probes = 0
        real_open_lock_file = projection.platform_compat.open_lock_file

        def count_holder_probes(path):
            nonlocal holder_probes
            if path.parent == lease_dir:
                holder_probes += 1
            return real_open_lock_file(path)

        monkeypatch.setattr(projection.platform_compat, "open_lock_file", count_holder_probes)

        projection._prune_stale_managed_aliases(
            agents, projection.data_home().absolute().as_posix(), keep=set()
        )

    # Ceiling 3 over record, holder, record, ... admits residue-0's record and
    # holder and residue-1's record before tripping, so two records are probed.
    assert holder_probes == 2
    assert stale.exists(), "a capped lease scan must retain rather than authorize deletion"
    # The two residue leases lock-validated before the cap are reclaimed; residue
    # past the ceiling was never scanned and must remain untouched.
    remaining_records = sum(
        1
        for index in range(3)
        if (lease_dir / f"residue-{index}{projection._PROJECTION_LEASE_RECORD_SUFFIX}").exists()
    )
    remaining_holders = sum(
        1
        for index in range(3)
        if (lease_dir / f"residue-{index}{projection._PROJECTION_LEASE_HOLDER_SUFFIX}").exists()
    )
    assert remaining_records == 1
    assert remaining_holders == 1


def test_prune_work_cap_counts_retained_candidates_before_reclaims(native_tree, monkeypatch):
    """Reconciled contract: retained candidates do NOT starve the stale-work budget.

    Before this fix the bounded candidate scan counted kept/active/live aliases
    toward the work limit, so a retained entry sorting ahead of the backlog
    consumed a stale-work slot and left real backlog unexamined -- and a run's own
    just-published aliases sort early, making the starvation order-dependent
    rather than rare. The scan now skips retained entries WITHOUT charging the
    stale-work budget, while still bounding total traversal at the class limit
    plus the bounded skip set, so it is not an unbounded bypass. Here three live
    aliases are interleaved ahead of the stale backlog; with a stale-work limit of
    3 all three stale candidates are still examined and reclaimed, and the lease
    directory is scanned exactly once.
    """
    live_stems = {f"kirocrew-skill-view-{0xA00 + index:024x}" for index in range(3)}
    _home, agents, _project = native_tree
    stale = [_legacy_alias(agents, f"{index:024x}") for index in range(3)]

    monkeypatch.setattr(projection, "_PROJECTION_PRUNE_WORK_LIMIT", 3, raising=False)
    monkeypatch.setattr(projection, "_PRUNE_MAX_RECLAIMS_PER_RUN", 64)
    # Every stale candidate must be reached, so the walk must not end on the clock.
    _hold_the_prune_clock(monkeypatch)

    scans = 0

    def one_batch_scan(_directory, **_kwargs):
        nonlocal scans
        scans += 1
        # Every live alias is reported by the single batch scan; the prune folds
        # them into the skip set the candidate generator excludes without charging
        # the stale-work budget.
        return set(live_stems), False

    monkeypatch.setattr(projection, "_scan_projection_leases", one_batch_scan)

    projection._prune_stale_managed_aliases(
        agents, projection.data_home().absolute().as_posix(), keep=set()
    )

    assert scans == 1, "the lease directory must be scanned once per prune, never per candidate"
    assert not any(
        path.exists() for path in stale
    ), "live aliases consumed stale-work budget and starved backlog reclamation"


def test_is_legacy_projected_view_deeply_nested_alias_reads_as_unreadable(
    native_tree, cyclic_gc_quiesced
):
    """A legacy-named alias nested past the interpreter limit is not legacy, and does not abort.

    ``_is_legacy_projected_view`` is the third ownership JSON reader; its
    ``json.loads`` guard caught only ``(ValueError, TypeError)``, so a
    hand-authored alias whose JSON nests past the recursion limit raised
    ``RecursionError`` (a ``RuntimeError``) straight through the reader and
    aborted the caller instead of failing closed. The reader must treat such a
    file as an unreadable view (return ``False``), never legacy to reclaim.
    Fails at pytest call on 694b43971 (2000 levels does not trip recursion, so
    the fixture nests 100000 deep, matching the sidecar/lease readers' fixtures).
    """
    _home, agents, _project = native_tree
    alias_path = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}{'0' * 24}.json"
    deep = ("[" * 100000 + "]" * 100000).encode("utf-8")
    alias_path.write_bytes(deep)
    assert projection._is_legacy_projected_view(alias_path, deep) is False


def test_windows_alias_unlink_rechecks_identity_under_publication_lock(tmp_path, monkeypatch):
    alias = tmp_path / "alias.json"
    alias.write_text("managed", encoding="utf-8")
    identity = alias.stat().st_dev, alias.stat().st_ino
    monkeypatch.setattr(projection.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", True)

    assert projection._unlink_alias_if_unchanged(alias, identity)
    assert not alias.exists()


def test_projection_and_provider_serialize_workspace_settings_writes(native_tree, monkeypatch):
    from kiro_crew.providers import acp as provider_acp

    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    provider_write_reached = threading.Event()
    provider_done = threading.Event()
    provider_errors = []
    real_provider_atomic_write = provider_acp.atomic_write
    real_projection_atomic_write = projection.atomic_write
    update_thread = None
    started = False

    def observed_provider_atomic_write(*args, **kwargs):
        provider_write_reached.set()
        return real_provider_atomic_write(*args, **kwargs)

    def update_tool_search():
        try:
            provider_acp._write_tool_search_overlay(project, True, 17, 4096)
        except BaseException as exc:
            provider_errors.append(exc)
        finally:
            provider_done.set()

    def start_concurrent_writer(path, *args, **kwargs):
        nonlocal update_thread, started
        if (
            not started
            and path.parent == agents
            and path.stem.startswith(projection.NATIVE_SKILL_ALIAS_PREFIX)
        ):
            started = True
            update_thread = threading.Thread(target=update_tool_search)
            update_thread.start()
            # flake-ok: a correct settings lock keeps the event unset, so the 100 ms window can only miss a regression, never fail a correct run
            assert not provider_write_reached.wait(
                0.1
            ), "the provider reached its cli.json commit while projection held the settings lock"
        return real_projection_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(provider_acp, "atomic_write", observed_provider_atomic_write)
    monkeypatch.setattr(projection, "atomic_write", start_concurrent_writer)

    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    assert update_thread is not None
    # flake-ok: the writer finishes in a few milliseconds once the lock is released; 3 s only names a wedged writer
    update_thread.join(timeout=3.0)
    assert not update_thread.is_alive()
    assert provider_done.is_set() and not provider_errors
    settings = json.loads((project / ".kiro/settings/cli.json").read_text(encoding="utf-8"))
    assert settings[projection._MANAGED_SETTING] is True
    assert settings["toolSearch.enabled"] is True
    assert settings["toolSearch.minPct"] == 17
    assert settings["toolSearch.minTokens"] == 4096


@requires_symlinks
def test_workspace_settings_lock_refuses_a_planted_symlink(tmp_path):
    from kiro_crew.workspace_cli_settings import (
        CLI_SETTINGS_LOCK_NAME,
        workspace_cli_settings_lock,
    )

    project = tmp_path / "project"
    settings = project / ".kiro" / "settings"
    settings.mkdir(parents=True)
    target = tmp_path / "unrelated.lock"
    target.write_text("unrelated", encoding="utf-8")
    (settings / CLI_SETTINGS_LOCK_NAME).symlink_to(target)

    with pytest.raises(OSError, match="symlink or junction"):
        with workspace_cli_settings_lock(project):
            pytest.fail("a planted settings lock must never be acquired")

    assert target.read_text(encoding="utf-8") == "unrelated"


def test_workspace_settings_lock_failure_publishes_no_alias(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")

    @contextmanager
    def unavailable(_work_dir):
        raise OSError("test settings lock unavailable")
        yield

    monkeypatch.setattr(projection, "workspace_cli_settings_lock", unavailable)

    assert projection.prepare_native_skill_projection(project) is None
    assert not list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert not (project / ".kiro/settings/cli.json").exists()


def test_census_counts_what_the_reclaim_would_keep_and_remove(native_tree, tmp_path, monkeypatch):
    """The read-only census agrees with the lifecycle it describes, and changes nothing."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    crew_home = projection.data_home().absolute().as_posix()

    ended_dir = tmp_path / "subagent_00000001"
    ended_dir.mkdir()
    ended = projection.prepare_native_skill_projection(ended_dir)
    assert ended is not None
    published = len(ended.aliases)
    del ended
    gc.collect()
    live = projection.prepare_native_skill_projection(project)
    assert live is not None

    before = sorted(str(p) for p in agents.rglob("*"))
    census = projection.census_projected_aliases(agents)
    assert sorted(str(p) for p in agents.rglob("*")) == before
    # `live` reclaimed the ended run's aliases on its own spawn (cap covers
    # them), so the directory holds exactly the live set, all lease-named.
    assert census == {
        "total": published,
        "leased": published,
        "foreign_home": 0,
        "foreign_leased": 0,
        "unreadable_leases": 0,
        "truncated": 0,
    }

    # An alias another data home recorded is counted as foreign, never as
    # something this gateway will drain; a malformed lease record is reported
    # rather than skipped, because the reclaim treats it as "everything live".
    foreign = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}{'f' * 24}.json"
    foreign.write_text("{}", encoding="utf-8")
    (projection._projection_metadata_dir() / f"{foreign.stem}.json").write_text(
        json.dumps(
            {
                projection._MANAGED_MARKER: projection._MANAGED_MARKER_VALUE,
                projection._MANAGED_CREW_HOME: crew_home + "-other",
            }
        ),
        encoding="utf-8",
    )
    (projection._projection_lease_dir() / "9-broken.json").write_text("{", encoding="utf-8")
    census = projection.census_projected_aliases(agents)
    assert census == {
        "total": published + 1,
        "leased": published,
        "foreign_home": 1,
        "foreign_leased": 0,
        "unreadable_leases": 1,
        "truncated": 0,
    }

    # The other home's LIVE aliases -- named by its lease -- are split out too,
    # since this gateway's reclaim refuses them whether or not the lease holds.
    (projection._projection_lease_dir() / "9-theirs.json").write_text(
        json.dumps({"aliases": [foreign.stem]}), encoding="utf-8"
    )
    census = projection.census_projected_aliases(agents)
    assert census["leased"] == published + 1
    assert census["foreign_home"] == 0
    assert census["foreign_leased"] == 1

    # A RecursionError from json.loads is an unreadable record, not an abort.
    (projection._projection_lease_dir() / "9-deep.json").write_text(
        "[" * 100000 + "]" * 100000, encoding="utf-8"
    )
    census = projection.census_projected_aliases(agents)
    assert census["unreadable_leases"] == 2
    assert projection._read_lease_record(projection._projection_lease_dir() / "9-deep.json") is None

    # Retention is bounded and the bound is reported, not silently exceeded.
    monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 1, raising=False)
    census = projection.census_projected_aliases(agents)
    assert census["truncated"] == 1
    assert census["total"] == published + 1
    del live


def test_boot_drain_pause_outlasts_the_lock_poll_cap():
    """A spawn blocked on the publication lock polls with backoff up to the
    lock's poll cap. A between-batch gap shorter than that cap can open and
    close while the waiter sleeps, so it never takes the lock and runs into
    the 2s acquisition ceiling instead."""
    from kiro_crew import platform_compat

    assert projection._DRAIN_BATCH_PAUSE_SECS > platform_compat._LOCK_POLL_MAX_SECS
    assert projection._DRAIN_BATCH_PAUSE_SECS < projection._PROJECTION_LOCK_TIMEOUT_SECS


def _released_backlog(agents, project, count):
    """*count* stale aliases of one agent, each with its sidecar, no lease held.

    Every version stays held while the next is published, so no spawn prunes
    it; releasing them all at once leaves a backlog, as a capped prune does.
    """
    source = agents / "custom.json"
    held = []
    for n in range(count):
        source.write_text(json.dumps({"name": "custom", "prompt": f"v{n}"}), encoding="utf-8")
        held.append(projection.prepare_native_skill_projection(project))
    backlog = [(_alias_file(agents, p), _metadata_file(agents, p)) for p in held]
    del held
    gc.collect()
    assert all(alias.exists() and meta.exists() for alias, meta in backlog)
    return backlog


def _recorded_batches(monkeypatch):
    """What each of the drain's prune batches reclaimed, in order."""
    real_prune = projection._prune_stale_managed_aliases
    batches = []

    def recording(directory, crew_home_id, **kwargs):
        reclaimed = real_prune(directory, crew_home_id, **kwargs)
        batches.append(reclaimed)
        return reclaimed

    monkeypatch.setattr(projection, "_prune_stale_managed_aliases", recording)
    return batches


def test_boot_drain_clears_a_backlog_past_the_per_spawn_cap(native_tree, monkeypatch):
    _home, agents, project = native_tree
    backlog = _released_backlog(agents, project, 7)
    monkeypatch.setattr(projection, "_PRUNE_MAX_RECLAIMS_PER_RUN", 2)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)

    assert projection.drain_stale_aliases() == 7
    assert not any(alias.exists() or meta.exists() for alias, meta in backlog)


def test_boot_drain_keeps_aliases_a_live_projection_holds(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    live = projection.prepare_native_skill_projection(project)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)
    projection.drain_stale_aliases()
    assert _alias_file(agents, live).exists()
    assert _metadata_file(agents, live).exists()


def test_boot_drain_retries_a_batch_that_missed_the_lock(native_tree, monkeypatch):
    """One contended batch at boot -- a spawn publishing while the drain
    runs -- must not abandon the whole backlog until the next restart."""
    _home, agents, project = native_tree
    backlog = _released_backlog(agents, project, 5)
    real_lock = projection._projection_alias_lock
    # The second batch misses the lock; every later one takes it, however many
    # the drain needs (a scripted list that runs dry would raise StopIteration
    # into the drain's catch-all and end it early, hiding a short count).
    outcomes = itertools.chain(["ok", "held"], itertools.repeat("ok"))
    attempts = []

    def flaky(directory):
        outcome = next(outcomes)
        attempts.append(outcome)
        if outcome == "held":
            raise OSError("held")
        return real_lock(directory)

    monkeypatch.setattr(projection, "_projection_alias_lock", flaky)
    monkeypatch.setattr(projection, "_PRUNE_MAX_RECLAIMS_PER_RUN", 2)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)
    # The exact batch sequence below assumes each walk ends on the cap, not the clock.
    _hold_the_prune_clock(monkeypatch)

    assert projection.drain_stale_aliases() == 5
    assert not any(alias.exists() or meta.exists() for alias, meta in backlog)
    # Batches reclaim 2, miss, 2, 1, then three idle batches end the drain: the
    # miss counted as one idle batch, and the reclaims after it restarted the
    # idle run rather than ending the loop.
    assert attempts == ["ok", "held", "ok", "ok", "ok", "ok", "ok"]


def test_boot_drain_ends_when_the_lock_is_never_available(native_tree, monkeypatch):
    """The lock helper raises OSError for a busy lock AND for a lock-file fault
    (a symlinked lock, a permission error). Either one, repeated, is a run of
    idle batches: the drain ends at that bound instead of paying every batch,
    and raises nothing into the boot janitor."""
    _home, agents, _project = native_tree
    attempts = []

    def faulted(_directory):
        attempts.append(1)
        raise OSError("skill projection lock is a symlink or junction")

    monkeypatch.setattr(projection, "_projection_alias_lock", faulted)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)

    assert projection.drain_stale_aliases() == 0
    assert len(attempts) == projection._DRAIN_IDLE_BATCHES


def test_boot_drain_continues_past_a_batch_the_budget_cut_short(native_tree, monkeypatch):
    """A batch that spends its time budget before reaching a reclaimable entry
    returns zero, and that zero is not the end of the backlog: the per-spawn
    prune walks from a random offset, so a prefix of kept or leased entries
    can eat a whole budget. Only a run of consecutive zeros, each from a fresh
    offset, means the sweep is done."""
    _home, agents, project = native_tree
    backlog = _released_backlog(agents, project, 5)
    real_budget = projection._PRUNE_MAX_SECONDS_PER_RUN
    batches = _recorded_batches(monkeypatch)
    recording = projection._prune_stale_managed_aliases
    # Every batch after the starved one must finish its walk for the reclaim
    # counts below to mean anything; with the clock held, the real budget is
    # never spent by wall-clock time.
    _hold_the_prune_clock(monkeypatch)

    def starve_the_first_batch(directory, crew_home_id, **kwargs):
        # A zero budget is spent at the first candidate, before any is classified.
        monkeypatch.setattr(
            projection, "_PRUNE_MAX_SECONDS_PER_RUN", 0.0 if not batches else real_budget
        )
        return recording(directory, crew_home_id, **kwargs)

    monkeypatch.setattr(projection, "_prune_stale_managed_aliases", starve_the_first_batch)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)

    assert projection.drain_stale_aliases() == 5
    # The reclaiming batch restarted the idle count, so the drain paid the full
    # run of idle batches after it rather than stopping at the first zero.
    assert batches == [0, 5] + [0] * projection._DRAIN_IDLE_BATCHES
    assert not any(alias.exists() or meta.exists() for alias, meta in backlog)


def test_boot_drain_ends_after_a_run_of_batches_that_reclaim_nothing(native_tree, monkeypatch):
    """The continue-past-a-cut-short-batch rule must not turn a clean directory
    into a thousand-batch walk: a short run of batches that reclaim nothing
    ends it."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    live = projection.prepare_native_skill_projection(project)
    batches = _recorded_batches(monkeypatch)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)

    assert projection.drain_stale_aliases() == 0
    assert batches == [0] * projection._DRAIN_IDLE_BATCHES
    assert _alias_file(agents, live).exists()


def test_boot_drain_ends_on_idle_batches_when_every_budget_is_spent(native_tree, monkeypatch):
    """The stopping rule cannot be "a walk that saw every candidate": on a large
    directory whose entries mostly belong to another data home or are leased,
    every batch spends its time budget first, so no walk is ever exhaustive.
    Such a drain must still end on the idle-batch run instead of paying all
    _DRAIN_MAX_BATCHES batches for nothing."""
    _home, agents, project = native_tree
    backlog = _released_backlog(agents, project, 3)
    batches = _recorded_batches(monkeypatch)
    # Spent at the first candidate, on every batch: reclaimed is always zero and
    # the walk never classifies anything.
    monkeypatch.setattr(projection, "_PRUNE_MAX_SECONDS_PER_RUN", 0.0)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)

    assert projection.drain_stale_aliases() == 0
    assert batches == [0] * projection._DRAIN_IDLE_BATCHES
    # Nothing was reclaimable within the budget, so the backlog is untouched and
    # later spawns (or the next boot) clear it.
    assert all(alias.exists() and meta.exists() for alias, meta in backlog)


def test_boot_drain_idle_count_restarts_on_any_reclaim(native_tree, monkeypatch):
    """Only CONSECUTIVE zeros end the drain. A reclaim restarts the count, so a
    backlog that yields intermittently is cleared rather than abandoned at the
    first pair of quiet batches."""
    _home, agents, _project = native_tree
    # Never runs dry: a StopIteration would end the drain through its catch-all.
    reclaims = itertools.chain([0, 0, 1], itertools.repeat(0))
    batches = []

    def scripted(directory, crew_home_id, **kwargs):
        reclaimed = next(reclaims)
        batches.append(reclaimed)
        return reclaimed

    monkeypatch.setattr(projection, "_prune_stale_managed_aliases", scripted)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)

    assert projection.drain_stale_aliases() == 1
    # Two idle, one reclaim that restarts the count, then a full idle run.
    assert batches == [0, 0, 1] + [0] * projection._DRAIN_IDLE_BATCHES


def test_boot_drain_ends_when_the_alias_directory_cannot_be_listed(native_tree, monkeypatch):
    """An unlistable directory reclaims nothing on every batch, so the drain
    ends on the idle run without touching the backlog and without raising."""
    import errno

    _home, agents, project = native_tree
    backlog = _released_backlog(agents, project, 2)
    batches = _recorded_batches(monkeypatch)
    real_scandir = projection.os.scandir
    normalized_agents = os.path.normcase(os.path.normpath(os.fspath(agents)))

    def unlistable(path, *args, **kwargs):
        try:
            same = os.path.normcase(os.path.normpath(os.fspath(path))) == normalized_agents
        except TypeError:
            same = False
        if same:
            raise OSError(errno.EIO, "input/output error", str(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)
    # Scoped: os.scandir is process-global, and the tmp-tree teardown (an
    # os.walk-based rmtree on Windows) must see the real one.
    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", unlistable)
        drained = projection.drain_stale_aliases()

    assert drained == 0
    assert batches == [0] * projection._DRAIN_IDLE_BATCHES
    assert all(alias.exists() and meta.exists() for alias, meta in backlog)


# ── Residual bounds: dual-coverage adoption, crash-residue recovery, batch lease scan ──


def test_lease_scan_unions_aliases_from_every_held_lease(native_tree):
    """Finding 3: one scan unions the aliases named by every held lease."""
    _home, agents, _project = native_tree
    a = projection._acquire_projection_lease(
        agents, {"kirocrew-skill-view-aaa", "kirocrew-skill-view-bbb"}
    )
    b = projection._acquire_projection_lease(agents, {"kirocrew-skill-view-ccc"})
    try:
        live, uncertain = projection._scan_projection_leases(agents)
    finally:
        a.close()
        b.close()
    assert uncertain is False
    assert live == {
        "kirocrew-skill-view-aaa",
        "kirocrew-skill-view-bbb",
        "kirocrew-skill-view-ccc",
    }


def test_prune_scans_the_lease_directory_once_for_many_candidates(native_tree, monkeypatch):
    """Finding 3: the lease directory is scanned ONCE per prune, not once per candidate.

    On the old per-candidate path this scanned the whole lease directory once for
    every retained candidate; the batch scan reads it once. Fails at pytest call
    on fcef4c9a4 (four candidates -> four scans there, one here).
    """
    _home, agents, _project = native_tree
    # The walk below must reach and reclaim every unleased candidate; that only
    # holds if the real wall-clock prune budget is not spent mid-walk. A slow
    # shared runner can spend it before the last candidate and leave the rest
    # "for later spawns", which is correct product behaviour but reads here as a
    # candidate that was not reclaimed. Freeze the clock so the reclaim is
    # runner-speed independent; the scandir-count assertion the test is named for
    # is unaffected either way.
    _hold_the_prune_clock(monkeypatch)
    candidates = [_legacy_alias(agents, f"{index:024x}") for index in range(4)]
    walked = [_legacy_alias(agents, f"{index:024x}") for index in range(100, 104)]
    external = projection._acquire_projection_lease(agents, {p.stem for p in candidates})
    lease_dir = projection._projection_lease_dir()
    normalized_lease_dir = os.path.normcase(os.path.normpath(os.fspath(lease_dir)))
    scandir_calls = 0
    real_scandir = projection.os.scandir

    def counting_scandir(path, *args, **kwargs):
        nonlocal scandir_calls
        try:
            same = os.path.normcase(os.path.normpath(os.fspath(path))) == normalized_lease_dir
        except TypeError:
            same = False
        if same:
            scandir_calls += 1
        return real_scandir(path, *args, **kwargs)

    # Scoped: os.scandir is process-global, and the tmp-tree teardown (an
    # os.walk-based rmtree on Windows) must see the real one.
    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", counting_scandir)
        try:
            projection._prune_stale_managed_aliases(
                agents, projection.data_home().absolute().as_posix(), keep=set()
            )
        finally:
            external.close()

    assert scandir_calls == 1, "the lease directory must be scanned once, not once per candidate"
    assert all(p.exists() for p in candidates)
    assert not any(p.exists() for p in walked), "unleased candidates are walked and reclaimed"


def test_a_lease_released_during_the_walk_keeps_its_aliases_for_that_call(native_tree, monkeypatch):
    """A release races the snapshot lock-free, so a stale snapshot may only keep.

    The lease is closed after the one scan and before the walk; the aliases it
    named survive this call, and an unleased stale alias beside them is still
    reclaimed, so the walk did run.
    """
    _home, agents, _project = native_tree
    leased = [_legacy_alias(agents, f"{index:024x}") for index in range(3)]
    free = _legacy_alias(agents, "f" * 24)
    lease = projection._acquire_projection_lease(agents, {p.stem for p in leased})
    real_candidates = projection._projection_prune_candidates

    def release_then_enumerate(directory, skip):
        lease.close()
        return real_candidates(directory, skip)

    monkeypatch.setattr(projection, "_projection_prune_candidates", release_then_enumerate)
    reclaimed = projection._prune_stale_managed_aliases(
        agents, projection.data_home().absolute().as_posix(), keep=set()
    )

    assert all(p.exists() for p in leased), "a lease released mid-walk must still keep its aliases"
    assert not free.exists() and reclaimed == 1


@pytest.mark.parametrize("ceiling", [projection._PROJECTION_LOCK_TIMEOUT_SECS, 1.7, 4.0])
def test_prune_budget_follows_the_lock_ceiling_and_keeps_the_publication_reserve(ceiling):
    """The walk budget is the ceiling minus the lease scan and a named reserve."""
    budget = projection._prune_budget_within(ceiling)
    reserve = projection._PROJECTION_PUBLICATION_RESERVE_SECS
    scan = projection._PROJECTION_LEASE_SCAN_MAX_SECONDS
    assert reserve > 0
    assert budget + scan + reserve <= ceiling + 1e-9
    assert budget == pytest.approx(ceiling - scan - reserve)
    shipped = projection._prune_budget_within(projection._PROJECTION_LOCK_TIMEOUT_SECS)
    assert projection._PRUNE_MAX_SECONDS_PER_RUN == shipped > 0


def test_prune_skips_all_candidate_deletion_when_lease_scan_is_uncertain(native_tree, monkeypatch):
    """Finding 3: any lease-scan uncertainty authorizes NO candidate deletion."""
    _home, agents, _project = native_tree
    stale = [_legacy_alias(agents, f"{index:024x}") for index in range(3)]
    monkeypatch.setattr(
        projection, "_scan_projection_leases", lambda _directory, **_kwargs: (set(), True)
    )

    projection._prune_stale_managed_aliases(
        agents, projection.data_home().absolute().as_posix(), keep=set()
    )

    assert all(p.exists() for p in stale), "an uncertain lease scan must authorize no deletion"
