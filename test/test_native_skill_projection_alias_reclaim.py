"""How a skill-view alias is named, and what each per-spawn prune reclaims or keeps.

Stale, legacy, foreign-home and leased aliases, the bounded rotating walk, and the
publication lock with its fail-closed fallback.
"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import native_skill_projection_helpers
import pytest
from native_skill_projection_helpers import (
    _alias_file,
    _alias_is_live,
    _crew_home_id,
    _hold_the_prune_clock,
    _legacy_alias,
    _metadata_file,
)

from conftest import requires_symlinks
from kiro_crew.acp import skill_projection as projection
from kiro_crew.hooks import FileTooLargeError

# The shared fixtures, bound here by name so pytest registers them for this module.
_no_eager_prune = native_skill_projection_helpers._no_eager_prune
_prune_on_every_spawn = native_skill_projection_helpers._prune_on_every_spawn
native_tree = native_skill_projection_helpers.native_tree


# ── Managed skill-view alias lifecycle ───────────────────────────────────────


def _spec_with_injected_env(value, extra=None):
    # The launcher's per-launch nonce key, which volatile_env_keys() names.
    env = {"AIM_CREDS_AGENT_INJECTION": value, **(extra or {})}
    return json.dumps(
        {"name": "custom", "mcpServers": {"injected": {"command": "helper", "env": env}}}
    )


def test_a_rewritten_env_value_reuses_the_alias(native_tree):
    home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text(_spec_with_injected_env("first-write"), encoding="utf-8")
    first = projection.prepare_native_skill_projection(project).agent("custom")
    source.write_text(_spec_with_injected_env("second-write"), encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    assert prepared.agent("custom") == first
    view = json.loads(_alias_file(agents, prepared).read_text(encoding="utf-8"))
    assert view["mcpServers"]["injected"]["env"]["AIM_CREDS_AGENT_INJECTION"] == "second-write"
    aliases = [
        p for p in agents.iterdir() if p.name.startswith(projection.NATIVE_SKILL_ALIAS_PREFIX)
    ]
    assert aliases == [agents / f"{first}.json"]


def test_an_added_env_key_names_a_new_alias(native_tree):
    home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text(_spec_with_injected_env("same"), encoding="utf-8")
    first = projection.prepare_native_skill_projection(project).agent("custom")
    source.write_text(_spec_with_injected_env("same", {"NEW_VAR": "1"}), encoding="utf-8")
    assert projection.prepare_native_skill_projection(project).agent("custom") != first


def test_project_agents_differing_only_in_env_values_never_share_an_alias(
    native_tree, tmp_path, monkeypatch
):
    home, agents, project = native_tree
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="custom", filename="custom.json", scope="project")],
    )
    views = {}
    for name, token in (("one", "token-one"), ("two", "token-two")):
        work = tmp_path / name
        (work / ".kiro" / "agents").mkdir(parents=True)
        (work / ".kiro" / "agents" / "custom.json").write_text(
            _spec_with_injected_env(token), encoding="utf-8"
        )
        prepared = projection.prepare_native_skill_projection(work)
        views[name] = (prepared.agent("custom"), _alias_file(agents, prepared))
    assert views["one"][0] != views["two"][0]
    first = json.loads(views["one"][1].read_text(encoding="utf-8"))
    assert first["mcpServers"]["injected"]["env"]["AIM_CREDS_AGENT_INJECTION"] == "token-one"


def test_generated_view_keeps_lifecycle_ownership_out_of_the_agent_spec(native_tree):
    home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    alias_path = _alias_file(agents, prepared)
    view = json.loads(alias_path.read_text(encoding="utf-8"))
    assert not any(str(key).startswith("x-kirocrew-") for key in view)
    metadata = json.loads(_metadata_file(agents, prepared).read_text(encoding="utf-8"))
    assert metadata[projection._MANAGED_MARKER] == projection._MANAGED_MARKER_VALUE
    assert metadata[projection._MANAGED_CREW_HOME] == (home.parent / "crew").as_posix()
    assert "x-kirocrew-work-dir" not in metadata
    assert metadata[projection._MANAGED_AGENT] == "custom"
    assert metadata[projection._MANAGED_SOURCE] == (agents / "custom.json").as_posix()
    assert (
        metadata[projection._MANAGED_ALIAS_SHA256]
        == hashlib.sha256(alias_path.read_bytes()).hexdigest()
    )


def test_current_publication_recovers_from_deeply_nested_sidecar(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    metadata_path = _metadata_file(agents, first)
    metadata_path.write_text("[" * 20000 + "]" * 20000, encoding="utf-8")

    prepared = projection.prepare_native_skill_projection(project)

    assert prepared is not None
    assert prepared.agent("custom") == first.agent("custom")
    alias_path = _alias_file(agents, prepared)
    assert json.loads(alias_path.read_text(encoding="utf-8")) == prepared.specs["custom"]
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert (
        metadata[projection._MANAGED_ALIAS_SHA256]
        == hashlib.sha256(alias_path.read_bytes()).hexdigest()
    )


def test_legacy_alias_rejects_deeply_nested_json(native_tree):
    _home, agents, _project = native_tree
    path = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}{'a' * 24}.json"

    assert not projection._is_legacy_projected_view(path, b"[" * 20000 + b"]" * 20000)


def test_prune_keeps_alias_held_by_a_live_projection(native_tree, monkeypatch):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    live = projection.prepare_native_skill_projection(project)
    live_alias = _alias_file(agents, live)
    source.unlink()
    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )

    projection.prepare_native_skill_projection(project)
    assert live_alias.exists()


def test_prune_reclaims_alias_whose_agent_file_was_deleted(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    stale = _alias_file(agents, first)
    assert stale.exists()
    del first
    gc.collect()
    # The agent stops resolving, and a spawn for a DIFFERENT live agent runs.
    (agents / "custom.json").unlink()
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )
    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    second = projection.prepare_native_skill_projection(project)
    assert not stale.exists()
    assert _alias_file(agents, second, "other").exists()


def test_every_reclaim_stage_admits_an_ordinary_local_alias(native_tree):
    """Each reclaim stage, asserted in order, so a failure names its own stage.

    The reclaim chain is a sequence of fail-safe gates: any one of them
    answering "uncertain" retains the alias, which is the right answer for a
    real hazard and an INVISIBLE no-op when the gate is simply wrong about an
    ordinary local file. The end-to-end prune tests above cannot tell those
    apart -- they assert only the final "file is gone", so every stage failing
    produces one identical message. This walks the same stages a stale alias
    passes through and asserts each separately, so a host where one gate
    misjudges an ordinary path reports WHICH gate rather than "not pruned".
    """
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    alias_path = _alias_file(agents, prepared)
    metadata_path = _metadata_file(agents, prepared)
    assert alias_path.exists(), "the alias was not published"
    assert metadata_path.exists(), "the ownership sidecar was not published"

    # Drop the projection: its lease must be released and reclaimed, which is
    # what makes the alias unused. Nothing about the authored source changes.
    del prepared
    gc.collect()

    assert projection._active_aliases() == set(), "a dropped projection still claims its aliases"
    assert not _alias_is_live(
        agents, alias_path.stem
    ), "the dropped projection's lease still reads as live"

    raw = alias_path.read_bytes()
    managed = projection._managed_metadata_for_alias(agents, alias_path, raw)
    assert managed is not None, "the ownership sidecar was not admitted for these alias bytes"
    metadata, recorded_path, recorded_identity, _recorded_raw = managed
    assert recorded_path == metadata_path
    assert recorded_identity is not None
    assert metadata[projection._MANAGED_CREW_HOME] == projection.data_home().absolute().as_posix()

    info = alias_path.stat()
    assert projection._unlink_alias_if_unchanged(
        alias_path, (info.st_dev, info.st_ino)
    ), "the identity-checked unlink refused an unchanged alias"
    assert not alias_path.exists()


def test_a_held_lease_stays_readable_so_it_only_keeps_the_aliases_it_names(native_tree):
    """A held lease must not make every OTHER alias read as live.

    Windows file locks are mandatory: ``file_lock`` takes ``msvcrt.locking`` on
    byte 0, and reading that byte from any other handle -- including another
    handle in this same process -- fails with a lock violation. A lease that
    carried its lifetime lock on the record a reader must parse therefore turned
    every probe into an exception, which this function answers as "live", so one
    held lease kept EVERY alias and nothing was ever reclaimed. Pruning always
    runs while the current projection holds its own lease, so that made the whole
    reclaim a no-op on Windows while passing on POSIX, where locks are advisory.
    The record and the lock target are separate files for exactly this reason.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    mine = _alias_file(agents, prepared).stem

    lease_dir = projection._projection_lease_dir()
    records = list(lease_dir.glob(f"*{projection._PROJECTION_LEASE_RECORD_SUFFIX}"))
    assert len(records) == 1, "the live projection published no readable lease record"
    assert json.loads(records[0].read_bytes())["aliases"] == [mine]

    # The structural half, checkable on any platform: the lock target is a
    # SEPARATE file. A single-file lease cannot satisfy the mandatory-lock
    # constraint above, so its absence is the regression, not a style choice.
    holder = records[0].with_name(
        records[0].name[: -len(projection._PROJECTION_LEASE_RECORD_SUFFIX)]
        + projection._PROJECTION_LEASE_HOLDER_SUFFIX
    )
    assert holder.exists(), "the lease has no separate lock target to hold"
    assert holder != records[0]

    # The lease naming `mine` is HELD by this process for prepared's lifetime.
    assert _alias_is_live(agents, mine) is True
    assert _alias_is_live(agents, "kirocrew-skill-view-notnamedhere") is False
    assert prepared is not None


def test_a_lease_is_never_published_past_the_bound_its_reader_enforces(native_tree):
    """The writer must not publish a record the reader answers "live" to.

    The reader treats an over-bound record as uncertain, which means live, which
    means keep. A writer allowed past that bound could therefore publish a
    record that is unreclaimable by construction: a crash leaves it behind and
    every later probe reads it as held, disabling pruning permanently. Refusing
    publication falls back to authored native agents, which the next spawn
    retries. Both ends read one constant so they cannot drift apart.
    """
    _home, agents, _project = native_tree
    cap = projection._PROJECTION_LEASE_MAX_ALIASES
    at_cap = {f"kirocrew-skill-view-{n:024x}" for n in range(cap)}

    stack = projection._acquire_projection_lease(agents, at_cap)
    try:
        lease_dir = projection._projection_lease_dir()
        records = list(lease_dir.glob(f"*{projection._PROJECTION_LEASE_RECORD_SUFFIX}"))
        assert len(records) == 1
        assert len(records[0].read_bytes()) <= projection._PROJECTION_LEASE_MAX_BYTES
        # A record AT the cap is admitted, so it keeps only what it names.
        assert _alias_is_live(agents, "kirocrew-skill-view-notnamedhere") is False
    finally:
        stack.close()

    with pytest.raises(OSError, match="exceed its reader's bound"):
        projection._acquire_projection_lease(agents, at_cap | {"kirocrew-skill-view-onemore"})


def test_prune_spares_a_legacy_alias_that_may_be_mid_publish(native_tree, monkeypatch):
    """The one window the re-preparation contract does not cover.

    A publisher from a build predating the lease holds no lease, so between its
    write and kiro-cli reading `--agent` its alias is indistinguishable from
    backlog -- and that process will NOT re-prepare, because it already did, so
    deleting it is a failed spawn rather than an eviction. The age gate exists
    only to exclude that window; it is not a liveness proxy.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    fresh = _legacy_alias(agents, "f" * 24, age_secs=0)
    stale = _legacy_alias(agents, "e" * 24)
    # Both candidates must be classified, so the walk must not end on the clock.
    _hold_the_prune_clock(monkeypatch)

    assert projection.prepare_native_skill_projection(project) is not None

    assert fresh.exists(), "an alias that may be mid-publish was reclaimed"
    assert not stale.exists(), "the aged backlog was not reclaimed"


def test_prune_reclaims_the_backlog_left_by_builds_that_wrote_no_ownership(native_tree):
    """The accumulated aliases are the harm; ownership-only reclaim never reaches them.

    Shipped builds published aliases with neither a sidecar nor an in-spec
    marker, so a reclaim keyed on a recorded pair skips every one of them and
    only post-upgrade growth is bounded -- on the hosts that reported thousands
    of these, the whole per-turn tool-spec cost would persist untouched.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    legacy = _legacy_alias(agents, "a" * 24)
    assert legacy.exists()

    prepared = projection.prepare_native_skill_projection(project)

    assert prepared is not None
    assert not legacy.exists(), "the pre-upgrade backlog was left on disk"
    assert _alias_file(agents, prepared).exists(), "this run's own alias was reclaimed"


def test_prune_leaves_an_unattributable_alias_even_with_the_right_name(native_tree, monkeypatch):
    """Shape is not provenance, and an unlink is not undoable.

    An operator's own agent could in principle carry this name, so one positive
    mark the projection itself writes is required as well. A view with neither
    Crew's managed server entry nor this host's absolute steering resource is
    left alone -- a smaller reclaim than the name shape would allow, and the
    right side to err on when the alternative is deleting somebody else's file.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    unattributable = _legacy_alias(agents, "9" * 24, owned=False)
    attributable = _legacy_alias(agents, "8" * 24)
    # Both candidates must be classified, so the walk must not end on the clock.
    _hold_the_prune_clock(monkeypatch)

    assert projection.prepare_native_skill_projection(project) is not None

    assert unattributable.exists(), "an alias Crew cannot claim was deleted on its name alone"
    assert not attributable.exists(), "an alias carrying Crew's own mark was left behind"


def test_prune_leaves_a_prefixed_file_that_is_not_a_projected_view(native_tree):
    """A name is not authorization. Only a view this module could have written goes."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    # Right prefix, wrong digest shape.
    short = _legacy_alias(agents, "b" * 12)
    # Right shape, but renamed -- not a projection, which always self-renames.
    renamed = _legacy_alias(agents, "c" * 24, name="someone-elses-agent")
    # Right shape, but it still carries skill resources, which a view never does.
    unstripped = _legacy_alias(agents, "d" * 24, resources=["skill://cat/s/SKILL.md"])

    assert projection.prepare_native_skill_projection(project) is not None

    assert short.exists()
    assert renamed.exists()
    assert unstripped.exists()


def test_prune_keeps_a_legacy_alias_a_held_lease_still_names(native_tree):
    """The lease gate governs the legacy path too; it is checked before ownership."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    legacy = _legacy_alias(agents, "e" * 24)
    external = projection._acquire_projection_lease(agents, {legacy.stem})
    try:
        assert projection.prepare_native_skill_projection(project) is not None
        assert legacy.exists(), "a held lease did not protect the legacy alias"
    finally:
        external.close()
    assert projection.prepare_native_skill_projection(project) is not None
    assert not legacy.exists()


def test_prune_caps_reclaims_per_run_so_the_backlog_drains_over_spawns(native_tree, monkeypatch):
    """A multi-thousand backlog must not be drained under one held lock.

    The prune runs while the publication lock is held, and that lock's own
    acquisition ceiling is 2s -- so a single sweep over the whole accumulated
    backlog would make a concurrent spawn fail to acquire and fall back to
    authored agents. The backlog is bounded and shrinking, so a per-run cap
    reclaims it just as completely across successive spawns.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(projection, "_PROJECTION_PRUNE_WORK_LIMIT", 4, raising=False)
    monkeypatch.setattr(projection, "_PRUNE_MAX_RECLAIMS_PER_RUN", 3)
    # The survivor counts below assume each walk ends on the cap, not the clock.
    _hold_the_prune_clock(monkeypatch)
    backlog = [_legacy_alias(agents, f"{n:024x}") for n in range(8)]

    assert projection.prepare_native_skill_projection(project) is not None
    assert sum(1 for p in backlog if p.exists()) == 4, "the cap did not bound one run"

    for _ in range(3):
        assert projection.prepare_native_skill_projection(project) is not None
    assert not any(p.exists() for p in backlog), "successive runs did not drain the backlog"


def _prune_walk(monkeypatch, classifications, blocked=frozenset()):
    """Record each candidate the walk classifies, and end it after *classifications*.

    The classification step runs once per candidate the budgeted loop examines,
    so the names it sees ARE the candidates this call examined, and answering
    "not reclaimed" for *blocked* pins an entry unreclaimable without inventing a
    live projection.

    Time is frozen and advanced ONLY by that probe, so one tick means one
    candidate. A clock that ticked per READ cannot express this: the
    classification path reads the clock too, so the budget would be spent by
    reads rather than by work. Nothing sleeps, and the count is exact.
    """
    seen = []
    real = projection._reclaim_prune_candidate
    if classifications == 0:
        monkeypatch.setattr(projection, "_PRUNE_MAX_SECONDS_PER_RUN", 0.0)
    budget = projection._PRUNE_MAX_SECONDS_PER_RUN
    ticks = [0]

    def clock():
        # Counted, never accumulated: summing budget/N N times lands either side of
        # the budget by one float ulp, which is one candidate either way.
        return 0.0 if not classifications else budget * ticks[0] / classifications

    def probe(directory, path, crew_home_id, *ledger):
        seen.append(path.stem)
        ticks[0] += 1
        return False if path.stem in blocked else real(directory, path, crew_home_id, *ledger)

    monkeypatch.setattr(projection, "_reclaim_prune_candidate", probe)
    monkeypatch.setattr(projection.time, "monotonic", clock)
    return seen


def test_a_backlog_cannot_stretch_the_locked_section_past_this_calls_budget(
    native_tree, monkeypatch
):
    """The prune holds the publication lock, whose acquisition ceiling is fixed.

    So the section it holds has to be bounded by something other than the size of
    the pile it is draining. The reclaim cap is not that bound: a candidate that
    is kept, active or leased costs a full classification and never increments
    it, so a backlog whose entries are ALL unreclaimable costs the full walk and
    buys no reclaim at all -- the case this budget exists for.
    """
    _home, agents, _project = native_tree
    backlog = [_legacy_alias(agents, f"{n:024x}") for n in range(24)]
    _prune_walk(monkeypatch, 8)

    projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())

    survivors = sum(1 for p in backlog if p.exists())
    assert survivors == len(backlog) - 8, "the walk did not stop on its own time budget"


def test_the_walk_stops_on_its_deadline_without_reclaiming_anything(native_tree, monkeypatch):
    """The deadline is the guarantee; the candidate cap only makes cost predictable.

    Per-candidate cost is not flat -- classification reads each alias and its
    sidecar -- so a count alone cannot bound wall-clock time. A spent
    budget is proved by a zero-length walk, which needs no clock and no sleep.
    """
    _home, agents, _project = native_tree
    backlog = [_legacy_alias(agents, f"{n:024x}") for n in range(6)]
    seen = _prune_walk(monkeypatch, 0)

    projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())

    assert seen == [], "the walk classified a candidate after its deadline had passed"
    assert all(p.exists() for p in backlog), "a deletion happened past the deadline"


def test_the_walk_starts_where_the_rotation_points(native_tree, monkeypatch):
    """A fixed start examines one prefix forever; the offset is what moves it.

    Driven through the prune itself rather than a spawn: publishing rewrites an
    alias and its sidecar, and a directory whose entries have been rewritten is
    free to enumerate them in a different order on another platform. Asserting a
    position across that would assert the filesystem, not the rotation.
    """
    _home, agents, _project = native_tree
    backlog = [_legacy_alias(agents, f"{n:024x}") for n in range(12)]
    order = list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert len(order) == len(backlog), "the walk sees entries this test did not seed"
    monkeypatch.setattr(projection, "_prune_start_offset", lambda count: count - 1)
    seen = _prune_walk(monkeypatch, 1, blocked={p.stem for p in order})

    projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())

    assert seen == [order[-1].stem], "the walk ignored the rotation and took the prefix"


def test_an_unreclaimable_prefix_cannot_hide_the_backlog_behind_it(native_tree, monkeypatch):
    """Rotation has to REACH every entry across calls, not merely differ per call.

    With a bounded walk and a fixed start, entries that are kept, active or leased
    at the front of the directory's own order hide everything behind them for
    good: the walk spends its whole budget on them every single call.
    """
    _home, agents, _project = native_tree
    backlog = [_legacy_alias(agents, f"{n:024x}") for n in range(12)]
    order = list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert len(order) == len(backlog), "the walk sees entries this test did not seed"
    pinned = order[:4]
    reclaimable = order[4:]
    assert reclaimable, "no entry sits behind the pinned prefix"

    turns = iter(range(0, 64, 4))

    def rotate(count):
        return next(turns, 0) % count if count else 0

    monkeypatch.setattr(projection, "_prune_start_offset", rotate)
    _prune_walk(monkeypatch, 4, blocked={p.stem for p in pinned})

    for _ in range(8):
        projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())

    assert all(p.exists() for p in pinned), "a pinned entry was reclaimed"
    assert not any(p.exists() for p in reclaimable), (
        "entries behind the unreclaimable prefix were never reached, so the bounded "
        "walk disabled its own cleanup"
    )


def test_the_rotation_offset_is_not_a_constant_and_stays_in_range(native_tree):
    """The seam's own contract: in range, and genuinely moving."""
    assert projection._prune_start_offset(0) == 0
    assert projection._prune_start_offset(1) == 0
    drawn = {projection._prune_start_offset(64) for _ in range(256)}
    assert drawn, "the seam returned nothing"
    assert all(0 <= offset < 64 for offset in drawn), "an offset fell outside the list"
    assert len(drawn) > 1, "a constant offset walks one prefix forever"


def test_every_work_dir_shares_one_alias_per_agent(native_tree, tmp_path):
    """The alias is named by the view, not by where it is spawned.

    Every subagent and cron run spawns in its own directory. Keying the alias on
    that directory wrote a full copy of every agent per run, and kiro-cli's
    subagent tool lists every copy it finds in the agents directory.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    runs = []
    for n in range(5):
        run_dir = tmp_path / f"subagent_{n:08x}"
        run_dir.mkdir()
        runs.append(projection.prepare_native_skill_projection(run_dir))
    live = projection.prepare_native_skill_projection(project)
    assert {run.agent("custom") for run in runs} == {live.agent("custom")}
    views = list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert views == [_alias_file(agents, live)]


def test_two_crew_homes_never_share_an_alias(native_tree, monkeypatch, tmp_path):
    """Identical views from two data homes must not contend for one file.

    A shared file would flip its ownership sidecar to whichever home spawned
    last, so each home's cleanup would misjudge the other's live view.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(projection, "data_home", lambda: tmp_path / "crew-a")
    first = projection.prepare_native_skill_projection(project)
    monkeypatch.setattr(projection, "data_home", lambda: tmp_path / "crew-b")
    second = projection.prepare_native_skill_projection(project)
    assert first.agent("custom") != second.agent("custom")
    assert _alias_file(agents, first).exists()
    assert _alias_file(agents, second).exists()


def test_republishing_identical_view_keeps_the_file_in_place(native_tree, tmp_path):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    before = os.stat(_alias_file(agents, first))
    other = tmp_path / "other"
    other.mkdir()
    second = projection.prepare_native_skill_projection(other)
    after = os.stat(_alias_file(agents, second))
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_editing_an_agent_publishes_a_new_alias_and_reclaims_the_old(native_tree):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom","prompt":"v1"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    stale = _alias_file(agents, first)
    del first
    gc.collect()
    source.write_text('{"name":"custom","prompt":"v2"}', encoding="utf-8")
    second = projection.prepare_native_skill_projection(project)
    assert _alias_file(agents, second) != stale
    assert not stale.exists()


def test_prune_reclaims_unused_alias_whose_work_dir_still_exists(native_tree, tmp_path):
    """A per-run work directory outlives its run; its aliases must not.

    Every subagent and cron run spawns in its own ``workspace_root()/<key>``
    directory, and nothing removes that directory when the run ends. Keying the
    reclaim on the directory's existence therefore keeps one alias per agent for
    every run ever spawned, until the directory holds enough files that kiro-cli
    fails with EMFILE on every spawn. Liveness is the lease, not the directory.
    """
    _home, agents, project = native_tree
    run_dir = tmp_path / "subagent_deadbeef"
    run_dir.mkdir()
    source = agents / "custom.json"
    source.write_text('{"name":"custom","prompt":"old"}', encoding="utf-8")
    ended = projection.prepare_native_skill_projection(run_dir)
    alias = _alias_file(agents, ended)
    metadata = _metadata_file(agents, ended)
    del ended
    gc.collect()
    assert run_dir.is_dir(), "the run directory is deliberately left in place"

    source.write_text('{"name":"custom","prompt":"new"}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    assert not alias.exists(), "an unused alias survived because its work dir still exists"
    assert not metadata.exists(), "the ownership sidecar outlived its alias"


def test_prune_reclaims_at_least_as_many_aliases_as_one_spawn_publishes(
    native_tree, monkeypatch, tmp_path
):
    """The reclaim cap covers the count one run publishes.

    Each spawn publishes one alias per agent and leaves that many behind when
    it ends. A cap below that count reclaims less than each run adds, so a
    steady spawn rate grows the directory without bound (143 agents against a
    cap of 64 on the reporting host).
    """
    _home, agents, project = native_tree
    names = ["alpha", "beta", "gamma"]
    for name in names:
        (agents / f"{name}.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name=n, filename=f"{n}.json", scope="global") for n in names],
    )
    monkeypatch.setattr(projection, "_PRUNE_MAX_RECLAIMS_PER_RUN", 1)
    _hold_the_prune_clock(monkeypatch)
    run_dir = tmp_path / "subagent_00000001"
    run_dir.mkdir()
    ended = projection.prepare_native_skill_projection(run_dir)
    left_behind = [_alias_file(agents, ended, n) for n in names]
    del ended
    gc.collect()
    for name in names:
        (agents / f"{name}.json").write_text(
            json.dumps({"name": name, "prompt": "edited"}), encoding="utf-8"
        )

    live = projection.prepare_native_skill_projection(project)
    assert live is not None
    assert not any(p.exists() for p in left_behind), "one spawn reclaimed fewer than it published"
    assert all(_alias_file(agents, live, n).exists() for n in names)


def test_prune_drains_headroom_beyond_one_spawn_publishes(native_tree, monkeypatch, tmp_path):
    """The cap drains headroom in addition to covering one spawn's aliases."""
    _home, agents, project = native_tree
    names = ["alpha", "beta", "gamma"]
    for name in names:
        (agents / f"{name}.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name=n, filename=f"{n}.json", scope="global") for n in names],
    )
    monkeypatch.setattr(projection, "_PRUNE_MAX_RECLAIMS_PER_RUN", 2)
    _hold_the_prune_clock(monkeypatch)

    def edit(version):
        for name in names:
            (agents / f"{name}.json").write_text(
                json.dumps({"name": name, "prompt": version}), encoding="utf-8"
            )

    run_dirs = [tmp_path / f"subagent_{n:08x}" for n in range(2)]
    ended = []
    for n, run_dir in enumerate(run_dirs):
        run_dir.mkdir()
        edit(f"v{n}")
        ended.append(projection.prepare_native_skill_projection(run_dir))
    edit("live")
    backlog = [_alias_file(agents, prepared, name) for prepared in ended for name in names]
    del ended
    gc.collect()
    assert sum(path.exists() for path in backlog) == 6

    live = projection.prepare_native_skill_projection(project)
    assert live is not None
    assert sum(path.exists() for path in backlog) == 1

    second_live = projection.prepare_native_skill_projection(project)
    assert second_live is not None
    assert not any(path.exists() for path in backlog)


def test_alias_count_stays_bounded_across_many_ended_runs(native_tree, tmp_path):
    """Spawning N runs in N directories, each ending, leaves one run's aliases."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    for n in range(12):
        run_dir = tmp_path / f"subagent_{n:08x}"
        run_dir.mkdir()
        prepared = projection.prepare_native_skill_projection(run_dir)
        assert prepared is not None
        del prepared
        gc.collect()
    live = projection.prepare_native_skill_projection(project)
    aliases = sorted(p.name for p in agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert aliases == [f"{live.agent('custom')}.json"]


def test_prune_keeps_alias_replaced_after_unused_classification(native_tree, monkeypatch):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    unused = _alias_file(agents, first)
    replacement = unused.read_text(encoding="utf-8")
    del first
    gc.collect()

    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )
    original_metadata = projection._managed_metadata_for_alias
    replacement_identity = []

    def replace_after_classification(directory, path, raw):
        result = original_metadata(directory, path, raw)
        if (
            result is not None
            and result[0].get(projection._MANAGED_AGENT) == "custom"
            and not replacement_identity
        ):
            # Model another gateway atomically recreating the alias after this
            # gateway classified the old one as unused and before it unlinks.
            projection.atomic_write(unused, replacement, restrict_to_owner=True)
            current = unused.stat()
            replacement_identity.append((current.st_dev, current.st_ino))
        return result

    monkeypatch.setattr(projection, "_managed_metadata_for_alias", replace_after_classification)
    projection.prepare_native_skill_projection(project)

    assert replacement_identity
    current = unused.stat()
    assert (current.st_dev, current.st_ino) == replacement_identity[0]


def test_projection_lock_covers_alias_publication_and_pruning(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    lock_held = 0
    real_file_lock = projection.platform_compat.file_lock
    real_atomic_write = projection.atomic_write
    real_prune = projection._prune_stale_managed_aliases

    @contextmanager
    def observed_file_lock(fd, **kwargs):
        nonlocal lock_held
        is_alias_lock = kwargs == {
            "exclusive": True,
            "timeout": projection._PROJECTION_LOCK_TIMEOUT_SECS,
        }
        with real_file_lock(fd, **kwargs):
            if is_alias_lock:
                lock_held += 1
            try:
                yield
            finally:
                if is_alias_lock:
                    lock_held -= 1

    def observed_atomic_write(path, *args, **kwargs):
        if path.parent == agents and path.stem.startswith(projection.NATIVE_SKILL_ALIAS_PREFIX):
            assert lock_held
        return real_atomic_write(path, *args, **kwargs)

    def observed_prune(*args, **kwargs):
        assert lock_held
        return real_prune(*args, **kwargs)

    monkeypatch.setattr(projection.platform_compat, "file_lock", observed_file_lock)
    monkeypatch.setattr(projection, "atomic_write", observed_atomic_write)
    monkeypatch.setattr(projection, "_prune_stale_managed_aliases", observed_prune)

    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    assert _alias_file(agents, prepared).exists()


def test_concurrent_threads_of_one_process_queue_instead_of_timing_out(tmp_path, monkeypatch):
    """N starts in ONE gateway must not each race the file lock on its own clock.

    Each holder keeps the lock 0.15 s and the file-lock ceiling is 0.3 s, so
    eight threads racing the file lock directly exceed it; queued in-process,
    each one's file-lock wait is near zero.
    """
    monkeypatch.setattr(projection, "_PROJECTION_LOCK_TIMEOUT_SECS", 0.3)
    directory = tmp_path / "agents"
    failures: list[BaseException] = []
    start = threading.Barrier(8)

    def hold() -> None:
        # flake-ok: the eight holders meet at the barrier so they contend at once; each join below is bounded at 30 s
        start.wait()
        try:
            with projection._projection_alias_lock(directory):
                # flake-ok: the 0.15 s hold is the subject; eight holders queued in-process stay under the 0.3 s file-lock ceiling
                # nosemgrep: kirocrew.test-sleep-as-barrier -- the hold time under the lock is the subject, not a barrier
                time.sleep(0.15)
        except OSError as exc:
            failures.append(exc)

    threads = [threading.Thread(target=hold) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)
    assert failures == []


def test_in_process_wait_is_bounded_and_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(projection, "_IN_PROCESS_PROJECTION_WAIT_SECS", 0.1)
    directory = tmp_path / "agents"
    result: list[BaseException] = []

    def contend() -> None:
        try:
            with projection._projection_alias_lock(directory):
                pass
        except OSError as exc:
            result.append(exc)

    with projection._projection_alias_lock(directory):
        t = threading.Thread(target=contend)
        t.start()
        t.join(timeout=10)
    assert len(result) == 1 and "inside this process" in str(result[0])
    # The holder released both locks, so the next acquire succeeds at once.
    with projection._projection_alias_lock(directory):
        pass


def test_a_failed_file_lock_releases_the_in_process_lock(tmp_path, monkeypatch):
    directory = tmp_path / "agents"

    @contextmanager
    def refusing_file_lock(fd, **kwargs):
        raise OSError("held elsewhere")
        yield  # pragma: no cover

    monkeypatch.setattr(projection.platform_compat, "file_lock", refusing_file_lock)
    with pytest.raises(OSError, match="held elsewhere"):
        projection._projection_alias_lock(directory)
    local = projection._in_process_projection_lock(directory)
    assert local.acquire(blocking=False)
    local.release()


def test_a_non_oserror_after_the_in_process_lock_releases_it(tmp_path, monkeypatch):
    directory = tmp_path / "agents"

    def broken_link_check(path):
        raise ValueError("unexpected path shape")

    monkeypatch.setattr(projection.platform_compat, "is_link_or_junction", broken_link_check)
    with pytest.raises(ValueError, match="unexpected path shape"):
        projection._projection_alias_lock(directory)
    local = projection._in_process_projection_lock(directory)
    assert local.acquire(blocking=False)
    local.release()


def test_a_burst_of_spawns_prunes_once_per_interval(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(projection, "_PER_SPAWN_PRUNE_MIN_INTERVAL_SECS", 60.0)
    monkeypatch.setattr(projection, "_LAST_PER_SPAWN_PRUNE", {})
    calls = 0
    real_prune = projection._prune_stale_managed_aliases

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_prune(*args, **kwargs)

    monkeypatch.setattr(projection, "_prune_stale_managed_aliases", counted)
    for _ in range(3):
        assert projection.prepare_native_skill_projection(project) is not None
    assert calls == 1
    monkeypatch.setattr(projection, "_PER_SPAWN_PRUNE_MIN_INTERVAL_SECS", 0.0)
    assert projection.prepare_native_skill_projection(project) is not None
    assert calls == 2


@requires_symlinks
def test_projection_lock_refuses_planted_symlink(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    target = agents / "unrelated.lock"
    target.write_text("unrelated", encoding="utf-8")
    projection._projection_lock_path().parent.mkdir(parents=True, exist_ok=True)
    projection._projection_lock_path().symlink_to(target)

    prepared = projection.prepare_native_skill_projection(project)

    assert prepared is None
    assert target.read_text(encoding="utf-8") == "unrelated"
    assert not list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert not (project / ".kiro/settings/cli.json").exists()


@pytest.mark.parametrize("failure_at", ["open", "acquire"])
def test_projection_lock_failure_keeps_aliases_and_preserves_startup(
    native_tree, monkeypatch, failure_at
):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    stale = _alias_file(agents, first)
    settings = project / ".kiro/settings/cli.json"
    assert json.loads(settings.read_text(encoding="utf-8"))[projection._INHERIT_SETTING] is True
    settings_before = settings.read_bytes()
    del first
    gc.collect()
    source.unlink()

    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )

    if failure_at == "open":

        def lock_failure(_path):
            raise OSError("test lock unavailable")

        monkeypatch.setattr(projection.platform_compat, "open_lock_file", lock_failure)
    else:

        @contextmanager
        def lock_failure(_fd, **_kwargs):
            raise OSError("test lock unavailable")
            yield

        monkeypatch.setattr(projection.platform_compat, "file_lock", lock_failure)
    prepared = projection.prepare_native_skill_projection(project)

    assert prepared is None
    assert list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json")) == [stale]
    assert settings.read_bytes() == settings_before


def test_prune_keeps_a_live_pairs_alias(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    alias = _alias_file(agents, first)
    # A second spawn for the same live pair must not remove the shared alias.
    projection.prepare_native_skill_projection(project)
    assert alias.exists()


def test_prune_never_touches_another_homes_alias(native_tree):
    _home, agents, project = native_tree
    # A foreign instance's alias: correct marker, DIFFERENT home, dead pair.
    foreign = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}foreignaliasfilename01.json"
    foreign.write_text(
        json.dumps(
            {
                "name": foreign.stem,
                projection._MANAGED_MARKER: projection._MANAGED_MARKER_VALUE,
                projection._MANAGED_CREW_HOME: "/some/other/crew/home",
                projection._MANAGED_AGENT: "ghost",
                projection._MANAGED_SOURCE: "/nonexistent/workdir/.kiro/agents/ghost.json",
            }
        ),
        encoding="utf-8",
    )
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    assert foreign.exists()


@pytest.mark.skipif(
    projection.platform_compat.IS_WINDOWS,
    reason="the resolved-path match is POSIX-only; on Windows _same_crew_home does not resolve",
)
@requires_symlinks
def test_same_crew_home_matches_across_symlinked_home_spellings(tmp_path):
    """`/home/<u>` (a symlink) and `/local/home/<u>` are the one data home.

    A sidecar recorded under one spelling and read under the other looked
    foreign, so nothing ever reclaimed it. The comparison is by resolved path,
    so both spellings -- in either role -- are the same home.
    """
    real = tmp_path / "local" / "home"
    real.mkdir(parents=True)
    linked = tmp_path / "home"
    linked.symlink_to(real, target_is_directory=True)

    recorded = (linked / ".kirocrew").as_posix()
    current = (real / ".kirocrew").as_posix()
    assert recorded != current, "the two spellings must be literally distinct for the test"

    assert projection._same_crew_home(recorded, current)
    assert projection._same_crew_home(current, recorded)


def test_same_crew_home_keeps_a_genuinely_different_home_foreign(tmp_path):
    """A real other home resolves to a different path and stays foreign."""
    mine = tmp_path / "crew-mine"
    mine.mkdir()
    theirs = tmp_path / "crew-theirs"
    theirs.mkdir()

    assert not projection._same_crew_home(theirs.as_posix(), mine.as_posix())
    # A non-string recorded value (a corrupt or absent record) is never a match.
    assert not projection._same_crew_home(None, mine.as_posix())
    assert not projection._same_crew_home(123, mine.as_posix())


def test_same_crew_home_never_resolves_an_untrusted_path_on_windows(monkeypatch):
    """On Windows the untrusted recorded path is never resolved, so no SMB touch.

    ``os.path.realpath`` on Windows reaches the filesystem, so resolving an
    attacker-planted value -- a bare ``\\\\host\\share`` OR a local ``C:\\link``
    that is a directory junction to one -- could open the remote host and leak
    SMB credentials. The converging property is "never resolve on Windows", not
    a per-spelling screen, so no realpath call happens for any unequal value.
    """
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", True)
    calls = []
    real_realpath = projection.os.path.realpath

    def _tracking(path):
        calls.append(path)
        return real_realpath(path)

    monkeypatch.setattr(projection.os.path, "realpath", _tracking)

    # A bare UNC spelling, a local path that could be a junction to UNC, and an
    # ordinary differing local path: none is resolved, each stays foreign.
    assert not projection._same_crew_home(r"\\attacker\share\.kirocrew", r"C:\Users\u\.kirocrew")
    assert not projection._same_crew_home(r"C:\local-link\.kirocrew", r"C:\Users\u\.kirocrew")
    assert not projection._same_crew_home(r"C:\other\.kirocrew", r"C:\Users\u\.kirocrew")
    # A literally identical value still matches without resolving.
    assert projection._same_crew_home(r"C:\Users\u\.kirocrew", r"C:\Users\u\.kirocrew")
    assert calls == [], "no untrusted path may reach os.path.realpath on Windows"


def test_same_crew_home_falls_back_to_literal_comparison_when_resolution_fails(monkeypatch):
    """A realpath that raises leaves the old exact-string behaviour in place."""

    def _boom(_path):
        raise OSError("resolution failed")

    monkeypatch.setattr(projection.os.path, "realpath", _boom)

    # Identical strings still match without ever reaching realpath.
    assert projection._same_crew_home("/home/u/.kirocrew", "/home/u/.kirocrew")
    # Different strings cannot be proven equal once resolution is unavailable, so
    # they stay foreign -- the pre-fix behaviour, which never widens this home.
    assert not projection._same_crew_home("/home/u/.kirocrew", "/local/home/u/.kirocrew")


@pytest.mark.skipif(
    projection.platform_compat.IS_WINDOWS,
    reason="the resolved-path match is POSIX-only; on Windows _same_crew_home does not resolve",
)
@requires_symlinks
def test_prune_reclaims_an_alias_recorded_under_a_symlinked_home_spelling(
    native_tree, monkeypatch, tmp_path
):
    """End to end: a dead alias whose sidecar names the other spelling drains.

    Before the fix the sidecar's recorded home (the ``/home`` symlink spelling)
    did not equal this process's resolved ``/local/home`` spelling, so the
    reclaim treated it as another home's and left it forever.
    """
    _home, agents, _project = native_tree
    real_home = tmp_path / "realhome" / "crew"
    real_home.mkdir(parents=True)
    linked_home = tmp_path / "linkhome"
    (tmp_path / "linkhome").symlink_to(tmp_path / "realhome", target_is_directory=True)
    linked_crew = linked_home / "crew"
    assert linked_crew.as_posix() != real_home.as_posix()

    # This process runs under the RESOLVED spelling.
    monkeypatch.setattr(projection, "data_home", lambda: real_home)

    # A dead alias (no agent file, no live lease) whose sidecar records the
    # OTHER spelling of the same home.
    alias = f"{projection.NATIVE_SKILL_ALIAS_PREFIX}aaaaaaaaaaaaaaaaaaaaaaaa"
    alias_path = agents / f"{alias}.json"
    alias_path.write_text(json.dumps({"name": alias}), encoding="utf-8")
    metadata_dir = projection._projection_metadata_dir()
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / f"{alias}.json").write_text(
        json.dumps(
            {
                projection._MANAGED_MARKER: projection._MANAGED_MARKER_VALUE,
                projection._MANAGED_CREW_HOME: linked_crew.as_posix(),
                projection._MANAGED_AGENT: "ghost",
                projection._MANAGED_SOURCE: "/nonexistent/.kiro/agents/ghost.json",
                projection._MANAGED_ALIAS_SHA256: hashlib.sha256(
                    alias_path.read_bytes()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    projection._prune_stale_managed_aliases(agents, real_home.absolute().as_posix(), keep=set())

    assert not alias_path.exists(), "the same-home alias under the other spelling was not reclaimed"


def test_prune_keeps_other_crew_homes_alias_after_its_work_dir_disappears(
    native_tree, monkeypatch, tmp_path
):
    _home, agents, project = native_tree
    first_crew_home = tmp_path / "crew-a"
    monkeypatch.setattr(projection, "data_home", lambda: first_crew_home)
    gone = tmp_path / "gone-for-first-home"
    gone.mkdir()
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(gone)
    foreign_after_switch = _alias_file(agents, first)
    shutil.rmtree(gone)

    monkeypatch.setattr(projection, "data_home", lambda: tmp_path / "crew-b")
    projection.prepare_native_skill_projection(project)
    assert foreign_after_switch.exists()


def test_prune_uses_the_recorded_source_when_filename_differs_from_agent(
    native_tree, monkeypatch, tmp_path
):
    _home, agents, first_project = native_tree
    source = agents / "authored-filename.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name="custom", filename="authored-filename.json", scope="global")
        ],
    )
    first = projection.prepare_native_skill_projection(first_project)
    live_alias = _alias_file(agents, first)

    second_project = tmp_path / "second-project"
    second_project.mkdir()
    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )
    projection.prepare_native_skill_projection(second_project)
    assert live_alias.exists()


def test_prune_keeps_oversized_alias_and_continues_startup(native_tree, monkeypatch):
    _home, agents, project = native_tree
    oversized = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}oversizedfilename0001.json"
    oversized.write_text("oversized", encoding="utf-8")
    original_read = projection.safe_read_file_bytes

    def read_with_oversized_failure(path):
        if path == str(oversized):
            raise FileTooLargeError("test oversized alias")
        return original_read(path)

    monkeypatch.setattr(projection, "safe_read_file_bytes", read_with_oversized_failure)
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    assert prepared.agent("custom")
    assert oversized.exists()


def test_prune_leaves_unmarked_and_malformed_prefix_files_alone(native_tree):
    _home, agents, project = native_tree
    # A prefix-named file with NO marker (a scanner-hostile squatter) and a
    # prefix-named file with unparseable content: neither is ours to delete.
    unmarked = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}unmarkedfilename000001.json"
    unmarked.write_text('{"name":"squatter"}', encoding="utf-8")
    malformed = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}malformedfilename00001.json"
    malformed.write_text("{ not json", encoding="utf-8")
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    assert unmarked.exists()
    assert malformed.exists()


def test_alias_deletion_is_retained_without_identity_safe_unlink(tmp_path, monkeypatch):
    alias = tmp_path / "alias.json"
    alias.write_text("managed", encoding="utf-8")
    identity = alias.stat().st_dev, alias.stat().st_ino
    monkeypatch.setattr(projection.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", False)

    def unexpected_unlink(_path):
        pytest.fail("a platform without identity-safe unlink must retain the alias")

    monkeypatch.setattr(type(alias), "unlink", unexpected_unlink)

    assert projection._unlink_alias_if_unchanged(alias, identity) is False
    assert alias.read_text(encoding="utf-8") == "managed"


def test_lock_failure_never_overwrites_a_newer_settings_generation(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    assert projection.prepare_native_skill_projection(project) is not None
    settings = project / ".kiro/settings/cli.json"
    newer = []

    def concurrent_update_then_failure(_directory):
        current = json.loads(settings.read_text(encoding="utf-8"))
        current["toolSearch.enabled"] = False
        settings.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")
        newer.append(settings.read_bytes())
        raise OSError("test lock timeout after concurrent settings update")

    monkeypatch.setattr(projection, "_projection_alias_lock", concurrent_update_then_failure)

    assert projection.prepare_native_skill_projection(project) is None
    assert newer and settings.read_bytes() == newer[0]


def test_alias_lock_fallback_logs_one_line_without_a_traceback(native_tree, monkeypatch, caplog):
    """A busy alias lock is routine contention, not a defect. The fallback logs
    a single WARNING that carries the lock helper's own wait detail, and no
    traceback -- a full stack repeated dozens of times a day only buries the
    log in contextlib frames that name the helper, not a cause.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    assert projection.prepare_native_skill_projection(project) is not None

    detail = (
        "could not acquire exclusive file lock: still held after waiting 2.00s "
        "(limit 2s); refusing to proceed unserialized"
    )

    def busy(_directory):
        raise OSError(detail)

    monkeypatch.setattr(projection, "_projection_alias_lock", busy)

    with caplog.at_level("WARNING", logger=projection.logger.name):
        assert projection.prepare_native_skill_projection(project) is None

    fallbacks = [r for r in caplog.records if "alias lock unavailable" in r.getMessage()]
    assert len(fallbacks) == 1
    (record,) = fallbacks
    assert record.levelname == "WARNING"
    # The helper's real wait detail reaches the operator inline...
    assert detail in record.getMessage()
    assert "using authored agents" in record.getMessage()
    # ...and no stack is attached: the one line is the whole report.
    assert record.exc_info is None


def test_prune_keeps_alias_while_an_external_projection_lease_is_locked(native_tree, monkeypatch):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    stale = _alias_file(agents, first)
    external = projection._acquire_projection_lease(agents, {stale.stem})
    del first
    gc.collect()
    source.unlink()

    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )
    live = projection.prepare_native_skill_projection(project)
    assert stale.exists()

    external.close()
    projection.prepare_native_skill_projection(project)
    assert live is not None
    assert not stale.exists()


def test_projection_finalizer_removes_its_lease_sidecar(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    lease_dir = projection._projection_lease_dir()
    assert len(list(lease_dir.glob("*.json"))) == 1

    del prepared
    gc.collect()

    assert list(lease_dir.glob("*.json")) == []
