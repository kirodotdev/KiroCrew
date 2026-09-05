"""Durable, queryable tombstone registry — Task 5.2 / Requirement 7.3.

Requirement 7.3: "THE Tombstone SHALL be discoverable from the surface that
listed the unit before the move."

Before this, every tombstone lived in a per-adapter in-memory dict
(``CronMigrationAdapter._tombstones``, ``slot.new_home``). Two consequences,
both of which these tests pin:

* A restart lost it. The double-fire guard itself survived, because
  ``CronJob.enabled=False`` IS persisted (``cron.py`` writes ``"enabled"``) —
  so this is NOT a double-fire bug. What was lost is the *reason*: on reload
  ``cron.py:950`` derives ``user_paused`` from ``not enabled``, so a migrated
  job reads back as an ordinary user-paused job. The work moved to another crew
  and the surface that listed it could not say so.
* Nothing could QUERY it. A listing surface had no way to ask "where did this
  unit go?" without holding the very adapter instance that performed the move.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.migration import protocol as P
from kiro_crew.migration.tombstones import TombstoneRegistry


def _ts(target="remote-ec2", remote="remote-9", kind="cron"):
    return P.Tombstone(
        unit_kind=kind,
        target_crew=P.CrewRef(crew_id=target, label="EC2 box"),
        remote_unit_id=remote,
        migrated_ts=1234.5,
    )


def test_a_recorded_tombstone_is_found_again(tmp_path):
    reg = TombstoneRegistry(store_dir=tmp_path)
    reg.record("cron", "job-1", _ts())
    found = reg.lookup("cron", "job-1")
    assert found is not None
    assert found.target_crew.crew_id == "remote-ec2"
    assert found.remote_unit_id == "remote-9"


def test_a_tombstone_survives_a_restart(tmp_path):
    """The whole point. A fresh instance over the same dir — as a restarted
    gateway would build — still answers where the work went."""
    TombstoneRegistry(store_dir=tmp_path).record("cron", "job-1", _ts())

    reborn = TombstoneRegistry(store_dir=tmp_path)
    found = reborn.lookup("cron", "job-1")
    assert found is not None
    assert found.target_crew.crew_id == "remote-ec2"
    assert found.target_crew.label == "EC2 box"
    assert found.remote_unit_id == "remote-9"
    assert found.migrated_ts == 1234.5


def test_an_unmigrated_unit_reads_as_none_not_an_error(tmp_path):
    """A listing surface asks about every row it renders. The common answer is
    'this one did not move', so that must be a value, not an exception."""
    reg = TombstoneRegistry(store_dir=tmp_path)
    assert reg.lookup("cron", "never-moved") is None


def test_kinds_do_not_collide(tmp_path):
    """A cron job and a chat slot may share an id string; they are different
    units and must not shadow each other."""
    reg = TombstoneRegistry(store_dir=tmp_path)
    reg.record("cron", "x-1", _ts(target="crew-a", remote="a1"))
    reg.record("session", "x-1", _ts(target="crew-b", remote="b1", kind="session"))

    assert reg.lookup("cron", "x-1").target_crew.crew_id == "crew-a"
    assert reg.lookup("session", "x-1").target_crew.crew_id == "crew-b"


def test_list_for_kind_returns_only_that_kind(tmp_path):
    reg = TombstoneRegistry(store_dir=tmp_path)
    reg.record("cron", "job-1", _ts())
    reg.record("cron", "job-2", _ts(remote="remote-10"))
    reg.record("session", "chat-3", _ts(kind="session"))

    crons = reg.list_for_kind("cron")
    assert set(crons) == {"job-1", "job-2"}
    assert set(reg.list_for_kind("session")) == {"chat-3"}
    assert reg.list_for_kind("taskrun") == {}


def test_a_unit_that_came_back_is_no_longer_tombstoned(tmp_path):
    """Reversibility (Req 7.4) meets discoverability. A unit migrated away and
    then migrated BACK is live here again — leaving the old tombstone in place
    would tell the user their running job had moved elsewhere."""
    reg = TombstoneRegistry(store_dir=tmp_path)
    reg.record("cron", "job-1", _ts())
    reg.clear("cron", "job-1")
    assert reg.lookup("cron", "job-1") is None
    # and the clearing is durable, not just in-memory
    assert TombstoneRegistry(store_dir=tmp_path).lookup("cron", "job-1") is None


def test_clearing_a_unit_that_never_moved_is_not_an_error(tmp_path):
    """materialize() clears unconditionally; it does not know whether this unit
    was ever tombstoned here."""
    reg = TombstoneRegistry(store_dir=tmp_path)
    reg.clear("cron", "nope")  # must not raise
    assert reg.lookup("cron", "nope") is None


def test_re_migrating_replaces_the_old_target(tmp_path):
    """A→B then B→C: the tombstone must name C, the current home, not B."""
    reg = TombstoneRegistry(store_dir=tmp_path)
    reg.record("cron", "job-1", _ts(target="crew-b", remote="b1"))
    reg.record("cron", "job-1", _ts(target="crew-c", remote="c1"))
    found = reg.lookup("cron", "job-1")
    assert found.target_crew.crew_id == "crew-c"
    assert found.remote_unit_id == "c1"


def test_an_unreadable_registry_does_not_break_the_listing(tmp_path):
    """A corrupt tombstone file must degrade to 'nothing moved', never take the
    schedule list down with it. Losing a redirect hint is recoverable; a crashed
    listing surface hides every job the user has."""
    (tmp_path / "tombstones.json").write_text("{ this is not json", encoding="utf-8")
    reg = TombstoneRegistry(store_dir=tmp_path)
    assert reg.lookup("cron", "job-1") is None
    assert reg.list_for_kind("cron") == {}
    # and it recovers: a fresh record is written over the garbage
    reg.record("cron", "job-1", _ts())
    assert TombstoneRegistry(store_dir=tmp_path).lookup("cron", "job-1") is not None


def test_the_persisted_form_is_json_a_human_can_read(tmp_path):
    """This file is a forensic record of where work went. If it is only readable
    by this class, an operator debugging a lost job cannot use it."""
    reg = TombstoneRegistry(store_dir=tmp_path)
    reg.record("cron", "job-1", _ts())
    raw = json.loads((tmp_path / "tombstones.json").read_text(encoding="utf-8"))
    assert raw["cron"]["job-1"]["target_crew"]["crew_id"] == "remote-ec2"
    assert raw["cron"]["job-1"]["remote_unit_id"] == "remote-9"


def test_the_registry_never_stores_the_payload(tmp_path):
    """A tombstone is a redirect, not a copy. The bundle may have carried a
    command line or a transcript; none of it belongs in a file that exists only
    to answer 'where did this go?'."""
    reg = TombstoneRegistry(store_dir=tmp_path)
    reg.record("cron", "job-1", _ts())
    raw = (tmp_path / "tombstones.json").read_text(encoding="utf-8")
    assert set(json.loads(raw)["cron"]["job-1"]) == {
        "unit_kind",
        "target_crew",
        "remote_unit_id",
        "migrated_ts",
    }


# ---------------------------------------------------------- adapter integration


@pytest.mark.asyncio
async def test_the_cron_adapter_records_into_the_registry(tmp_path):
    """The adapter is what knows a migration happened; the registry is what
    outlives it. Without this wiring the registry is an empty file."""
    from kiro_crew.migration.cron_adapter import CronMigrationAdapter

    reg = TombstoneRegistry(store_dir=tmp_path)
    job = _fake_job("job-1")
    a = CronMigrationAdapter(job_lookup={"job-1": job}, registry=reg)

    await a.tombstone("job-1", P.CrewRef(crew_id="remote-ec2"), "remote-9")

    # discoverable from a DIFFERENT process's view of the registry
    found = TombstoneRegistry(store_dir=tmp_path).lookup("cron", "job-1")
    assert found is not None
    assert found.target_crew.crew_id == "remote-ec2"
    assert found.remote_unit_id == "remote-9"


@pytest.mark.asyncio
async def test_materializing_here_clears_a_stale_tombstone(tmp_path):
    """The move-back case, end to end through the adapter."""
    from kiro_crew.migration.cron_adapter import CronMigrationAdapter

    reg = TombstoneRegistry(store_dir=tmp_path)
    reg.record("cron", "job-1", _ts())
    a = CronMigrationAdapter(create_job=lambda fields: "job-1", registry=reg)

    await a.materialize({"id": "job-1", "name": "n"})

    assert reg.lookup("cron", "job-1") is None


@pytest.mark.asyncio
async def test_the_adapter_works_without_a_registry(tmp_path):
    """The registry is optional so existing call sites keep working; an adapter
    built without one must not start raising."""
    from kiro_crew.migration.cron_adapter import CronMigrationAdapter

    a = CronMigrationAdapter(job_lookup={"job-1": _fake_job("job-1")})
    await a.tombstone("job-1", P.CrewRef(crew_id="c"), "r")
    assert a.tombstone_of("job-1").remote_unit_id == "r"


def _fake_job(job_id):
    """Mirrors the fixture in test_migration_cron_adapter.py — the CronJob id
    field is ``id``, not ``job_id``."""
    from kiro_crew.cron import CronJob, CronSchedule

    return CronJob(
        id=job_id,
        name="nightly",
        message="run backup",
        schedule=CronSchedule(kind="cron", cron_expr="0 3 * * *"),
    )


# ------------------- the listing surface actually shows the redirect (Req 7.3)


def test_cron_list_tells_the_user_where_a_migrated_job_went(tmp_path, capsys, monkeypatch):
    """Req 7.3, end to end on the surface that listed the job before the move.

    Without this, a migrated job reads back as an ordinary paused job: cron
    persists ``enabled=False`` (so the double-fire guard holds), but on reload
    ``user_paused`` is derived from ``not enabled``, leaving nothing that says
    the work now lives on another crew.
    """
    import argparse

    from kiro_crew import cli_commands

    job = _fake_job("job-1")
    job.enabled = False  # as tombstone() left it

    reg = TombstoneRegistry(store_dir=tmp_path / "migration")
    reg.record("cron", "job-1", _ts(target="remote-ec2", remote="remote-9"))

    monkeypatch.setattr(cli_commands, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_commands, "CronService", lambda **kw: _FakeSvc([job]))

    cli_commands._cron_dispatch(argparse.Namespace(cron_action="list"))

    out = capsys.readouterr().out
    assert "remote-ec2" in out
    assert "remote-9" in out
    assert "migrated" in out.lower()


def test_cron_list_says_nothing_extra_for_a_job_that_never_moved(tmp_path, capsys, monkeypatch):
    """No tombstone, no redirect line — the common row stays clean."""
    import argparse

    from kiro_crew import cli_commands

    monkeypatch.setattr(cli_commands, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_commands, "CronService", lambda **kw: _FakeSvc([_fake_job("job-1")]))

    cli_commands._cron_dispatch(argparse.Namespace(cron_action="list"))

    out = capsys.readouterr().out
    assert "job-1" in out
    assert "migrated" not in out.lower()


def test_cron_list_survives_an_unreadable_registry(tmp_path, capsys, monkeypatch):
    """A broken tombstone file must not take the schedule listing down — the
    user would lose sight of every job they have over a missing hint."""
    import argparse

    from kiro_crew import cli_commands

    (tmp_path / "migration").mkdir(parents=True)
    (tmp_path / "migration" / "tombstones.json").write_text("nope", encoding="utf-8")
    monkeypatch.setattr(cli_commands, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_commands, "CronService", lambda **kw: _FakeSvc([_fake_job("job-1")]))

    cli_commands._cron_dispatch(argparse.Namespace(cron_action="list"))

    assert "job-1" in capsys.readouterr().out


class _FakeSvc:
    def __init__(self, jobs):
        self._jobs = jobs

    def list_jobs(self, include_disabled=False):
        return self._jobs


def test_cron_list_still_lists_when_the_registry_itself_blows_up(tmp_path, capsys, monkeypatch):
    """Exercises the guard branch for real. The registry swallows its own
    corrupt-file case, so only an import or permissions failure reaches here —
    and that must still leave the user with a full job listing."""
    import argparse

    from kiro_crew import cli_commands
    from kiro_crew.migration import tombstones as tomb_mod

    def boom(**_kw):
        raise OSError("registry directory is not writable")

    monkeypatch.setattr(tomb_mod, "TombstoneRegistry", boom)
    monkeypatch.setattr(cli_commands, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_commands, "CronService", lambda **kw: _FakeSvc([_fake_job("job-1")]))

    cli_commands._cron_dispatch(argparse.Namespace(cron_action="list"))

    out = capsys.readouterr().out
    assert "job-1" in out  # listing intact
    assert "migrated" not in out.lower()


# ------------- A3: the other two kinds are discoverable too (Req 7.3)
#
# Req 7.3 says "the surface that listed the unit", not "the cron surface". With
# only cron wired, a moved session's destination lived in slot.new_home — memory
# only, gone on restart — and a moved task run recorded its destination nowhere
# durable at all.


class _FakeSlot:
    def __init__(self):
        self.accepting = True
        self.new_home = None

    def block_new_turns(self):
        self.accepting = False


@pytest.mark.asyncio
async def test_a_migrated_session_is_discoverable_after_a_restart(tmp_path):
    from kiro_crew.migration.session_adapter import SessionMigrationAdapter

    reg = TombstoneRegistry(store_dir=tmp_path)
    slot = _FakeSlot()
    a = SessionMigrationAdapter(
        session_id="chat-3",
        controller=slot,
        bundle_builder=lambda sid: {"transcript": ["hi"]},
        importer=lambda p: "remote-chat-9",
        registry=reg,
    )

    await a.tombstone("chat-3", P.CrewRef(crew_id="remote-ec2"), "remote-chat-9")

    assert slot.accepting is False  # still non-executing
    found = TombstoneRegistry(store_dir=tmp_path).lookup("session", "chat-3")
    assert found is not None
    assert found.target_crew.crew_id == "remote-ec2"
    assert found.remote_unit_id == "remote-chat-9"


@pytest.mark.asyncio
async def test_a_session_arriving_here_clears_its_stale_tombstone(tmp_path):
    from kiro_crew.migration.session_adapter import SessionMigrationAdapter

    reg = TombstoneRegistry(store_dir=tmp_path)
    reg.record("session", "remote-chat-9", _ts(kind="session"))
    a = SessionMigrationAdapter(
        session_id="chat-3",
        controller=_FakeSlot(),
        bundle_builder=lambda sid: {},
        importer=lambda p: "remote-chat-9",
        registry=reg,
    )

    await a.materialize({"transcript": ["hi"]})

    assert reg.lookup("session", "remote-chat-9") is None


@pytest.mark.asyncio
async def test_a_migrated_task_run_is_discoverable_after_a_restart(tmp_path):
    from kiro_crew.migration.taskrun_adapter import TaskRunMigrationAdapter

    reg = TombstoneRegistry(store_dir=tmp_path)
    a = TaskRunMigrationAdapter(run_lookup={}, registry=reg)

    await a.tombstone("TASK_x", P.CrewRef(crew_id="remote-ec2"), "remote-run-4")

    found = TombstoneRegistry(store_dir=tmp_path).lookup("taskrun", "TASK_x")
    assert found is not None
    assert found.target_crew.crew_id == "remote-ec2"
    assert found.remote_unit_id == "remote-run-4"


@pytest.mark.asyncio
async def test_a_task_run_arriving_here_clears_its_stale_tombstone(tmp_path):
    from kiro_crew.migration.taskrun_adapter import TaskRunMigrationAdapter

    reg = TombstoneRegistry(store_dir=tmp_path)
    reg.record("taskrun", "TASK_x", _ts(kind="taskrun"))
    a = TaskRunMigrationAdapter(create_run=lambda p: "TASK_x", registry=reg)

    await a.materialize({"task_id": "TASK_x"})

    assert reg.lookup("taskrun", "TASK_x") is None


@pytest.mark.asyncio
async def test_a_refused_taskrun_tombstone_writes_nothing(tmp_path):
    """An empty remote id is refused (it would claim the work moved while naming
    nowhere). The registry must not have been touched before that refusal."""
    from kiro_crew.migration.taskrun_adapter import TaskRunMigrationAdapter

    reg = TombstoneRegistry(store_dir=tmp_path)
    a = TaskRunMigrationAdapter(run_lookup={}, registry=reg)

    with pytest.raises(ValueError):
        await a.tombstone("TASK_x", P.CrewRef(crew_id="c"), "")

    assert reg.lookup("taskrun", "TASK_x") is None


@pytest.mark.asyncio
async def test_both_kinds_work_without_a_registry(tmp_path):
    """Optional everywhere, so existing call sites keep working."""
    from kiro_crew.migration.session_adapter import SessionMigrationAdapter
    from kiro_crew.migration.taskrun_adapter import TaskRunMigrationAdapter

    s = SessionMigrationAdapter(
        session_id="chat-3",
        controller=_FakeSlot(),
        bundle_builder=lambda sid: {},
        importer=lambda p: "r",
    )
    await s.tombstone("chat-3", P.CrewRef(crew_id="c"), "r")

    t = TaskRunMigrationAdapter(run_lookup={})
    await t.tombstone("TASK_x", P.CrewRef(crew_id="c"), "r")
    assert t.tombstone_of("TASK_x").remote_unit_id == "r"
