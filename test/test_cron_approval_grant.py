"""A job's auto approval and Bot owner grant count only when the gateway signed them.

``cron_service.approval_grant`` stores an HMAC (``approval_grant``) on every job a
gateway write gives ``approval_mode="auto"`` or a Bot owner grant. These pin: a
hand-written or copied grant fires interactive and the job is kept; a job made
through the gateway keeps ``auto`` across a reload and across a change of who it
runs as; a process with no key signs nothing and erases nothing; the one-time
start pass signs older jobs once and its marker sits in a sandbox-hidden leaf.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import sandbox_leaves
from kiro_crew.cron import CronService
from kiro_crew.cron_service import approval_grant, owner_grant, store
from kiro_crew.cron_service.fields import job_fire_approval_mode
from kiro_crew.mate_worker import MATE_WORKER_AGENT

_MEMBER = "m-slack-1"
_OWNER = "U0OWNER0000"


def _config(mode: str = "") -> SimpleNamespace:
    return SimpleNamespace(agents={"slack": SimpleNamespace(approval_mode=mode)})


def _worker() -> SimpleNamespace:
    return SimpleNamespace(
        template_id=MATE_WORKER_AGENT, member_id=_MEMBER, selection_kind="template"
    )


@pytest.fixture(autouse=True)
def _bot_named_by_member(_floor_monkeypatch):
    """``crewmate_name_for`` answers ``slack`` for the Bot's member id only."""
    mp = _floor_monkeypatch
    mp.setattr(
        "kiro_crew.dashboard.crewmate_approval.crewmate_name_for",
        lambda config, execution: (
            "slack" if getattr(execution, "member_id", None) == _MEMBER else ""
        ),
    )
    mp.setattr("kiro_crew.approval_mode_policy.session_mode_permitted", lambda m: True)


def _store(tmp_path) -> dict:
    return json.loads((tmp_path / "crons.json").read_text())


def _rewrite(tmp_path, job_id: str, **fields: object) -> None:
    """Edit *job_id*'s stored row by hand, as a process outside the gateway would."""
    data = _store(tmp_path)
    for row in data["jobs"]:
        if row["id"] == job_id:
            row.update(fields)
            for name, value in fields.items():
                if value is None:
                    row.pop(name, None)
    (tmp_path / "crons.json").write_text(json.dumps(data))


def _row(tmp_path, job_id: str) -> dict:
    return next(r for r in _store(tmp_path)["jobs"] if r["id"] == job_id)


def _reload(tmp_path, job_id: str):
    job = CronService(base_dir=tmp_path).get_job(job_id)
    assert job is not None
    return job


def test_a_hand_written_auto_record_is_kept_and_fires_interactive(tmp_path) -> None:
    job = CronService(base_dir=tmp_path).add_job(name="j", message="go", every_secs=3600)
    _rewrite(tmp_path, job.id, approval_mode="auto", approval_grant=None)

    reloaded = _reload(tmp_path, job.id)

    assert reloaded.approval_mode == "auto"
    assert job_fire_approval_mode(reloaded) == ""
    assert owner_grant.fire_approval_mode(reloaded, None, None) == ""
    # Loading changed nothing on disk: the record is not deleted or rewritten.
    assert _row(tmp_path, job.id)["approval_mode"] == "auto"
    assert "approval_grant" not in _row(tmp_path, job.id)


def test_a_grant_copied_onto_another_job_fires_interactive(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    signed = svc.add_job(name="a", message="go", every_secs=3600, approval_mode="auto")
    other = svc.add_job(name="b", message="go", every_secs=3600)
    grant = _row(tmp_path, signed.id)["approval_grant"]
    assert grant
    _rewrite(tmp_path, other.id, approval_mode="auto", approval_grant=grant)

    assert job_fire_approval_mode(_reload(tmp_path, other.id)) == ""
    assert job_fire_approval_mode(_reload(tmp_path, signed.id)) == "auto"


def test_a_grant_kept_on_a_job_whose_session_changed_by_hand_fires_interactive(tmp_path) -> None:
    job = CronService(base_dir=tmp_path).add_job(
        name="a", message="go", every_secs=3600, approval_mode="auto"
    )
    _rewrite(tmp_path, job.id, session_key="dashboard:other")

    assert job_fire_approval_mode(_reload(tmp_path, job.id)) == ""


def _identity(**claims: object):
    @web.middleware
    async def middleware(request: web.Request, handler):
        for key, value in claims.items():
            request[key] = value
        return await handler(request)

    return middleware


@pytest.mark.asyncio
async def test_an_auto_job_the_owner_makes_on_the_schedule_page_fires_auto_after_reload(
    tmp_path,
) -> None:
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers import cron as handlers

    svc = CronService(base_dir=tmp_path)
    app = web.Application(middlewares=[_identity(user=_OWNER, app="")])
    app["state"] = MagicMock(crons=svc, owner_id=_OWNER)
    app.router.add_route("POST", "/api/crons", handlers.api_crons_create)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/crons",
            json={"name": "x", "message": "m", "every": 3600, "approval_mode": "auto"},
        )
        body = await resp.json()
    assert resp.status < 300, body
    (stored,) = _store(tmp_path)["jobs"]

    reloaded = _reload(tmp_path, stored["id"])

    assert reloaded.approval_mode == "auto"
    assert job_fire_approval_mode(reloaded) == "auto"


def test_a_gateway_change_of_who_the_job_runs_as_keeps_auto(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="a", message="go", every_secs=3600, approval_mode="auto")
    assert svc.adopt_job(job.id, "dashboard:slot2")
    assert job_fire_approval_mode(_reload(tmp_path, job.id)) == "auto"

    fresh = CronService(base_dir=tmp_path)
    assert fresh.update_job(job.id, agent_id="kirocrew") is not None

    reloaded = _reload(tmp_path, job.id)
    assert (reloaded.session_key, reloaded.agent_id) == ("dashboard:slot2", "kirocrew")
    assert job_fire_approval_mode(reloaded) == "auto"


def _hand_written_auto(tmp_path) -> str:
    """A job whose stored ``auto`` the gateway never signed."""
    job = CronService(base_dir=tmp_path).add_job(name="a", message="go", every_secs=3600)
    _rewrite(tmp_path, job.id, approval_mode="auto")
    return job.id


def test_an_edit_that_re_sends_an_unsigned_auto_does_not_sign_it(tmp_path) -> None:
    job_id = _hand_written_auto(tmp_path)

    svc = CronService(base_dir=tmp_path)
    assert svc.update_job(job_id, name="renamed", approval_mode="auto") is not None

    reloaded = _reload(tmp_path, job_id)
    assert (reloaded.name, reloaded.approval_mode) == ("renamed", "auto")
    assert job_fire_approval_mode(reloaded) == ""


@pytest.mark.asyncio
async def test_a_schedule_page_edit_of_an_unsigned_auto_job_does_not_sign_it(tmp_path) -> None:
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers import cron as handlers

    job_id = _hand_written_auto(tmp_path)
    svc = CronService(base_dir=tmp_path)
    app = web.Application(middlewares=[_identity(user=_OWNER, app="")])
    app["state"] = MagicMock(crons=svc, owner_id=_OWNER)
    app.router.add_route("PATCH", "/api/crons/{job_id}", handlers.api_cron_update)
    async with TestClient(TestServer(app)) as client:
        resp = await client.patch(
            f"/api/crons/{job_id}", json={"name": "renamed", "approval_mode": "auto"}
        )
        body = await resp.json()
    assert resp.status < 300, body

    reloaded = _reload(tmp_path, job_id)
    assert (reloaded.name, reloaded.approval_mode) == ("renamed", "auto")
    assert job_fire_approval_mode(reloaded) == ""


def test_turning_an_unsigned_auto_off_and_on_again_signs_it(tmp_path) -> None:
    job_id = _hand_written_auto(tmp_path)

    svc = CronService(base_dir=tmp_path)
    assert svc.update_job(job_id, approval_mode="") is not None
    assert svc.update_job(job_id, approval_mode="auto") is not None

    assert job_fire_approval_mode(_reload(tmp_path, job_id)) == "auto"


def test_turning_auto_on_through_an_update_signs_it(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="a", message="go", every_secs=3600)
    assert svc.update_job(job.id, approval_mode="auto") is not None

    assert job_fire_approval_mode(_reload(tmp_path, job.id)) == "auto"


def test_a_process_with_no_key_signs_nothing_and_erases_nothing(tmp_path, monkeypatch) -> None:
    signed = CronService(base_dir=tmp_path).add_job(
        name="a", message="go", every_secs=3600, approval_mode="auto"
    )
    grant = _row(tmp_path, signed.id)["approval_grant"]

    with monkeypatch.context() as mp:
        mp.setattr(approval_grant, "_key", lambda: None)
        unsigned = CronService(base_dir=tmp_path).add_job(
            name="b", message="go", every_secs=3600, approval_mode="auto"
        )

    assert "approval_grant" not in _row(tmp_path, unsigned.id)
    assert job_fire_approval_mode(_reload(tmp_path, unsigned.id)) == ""
    # That save wrote the store again, and left the other job's MAC as it was.
    assert _row(tmp_path, signed.id)["approval_grant"] == grant
    assert job_fire_approval_mode(_reload(tmp_path, signed.id)) == "auto"


def _older_store(tmp_path) -> tuple[str, str]:
    """An auto job and a Bot job as a build before ``approval_grant`` left them."""
    svc = CronService(base_dir=tmp_path)
    auto = svc.add_job(name="a", message="go", every_secs=3600, approval_mode="auto")
    bot = svc.add_job(name="b", message="go", every_secs=3600, owner_grant=(_MEMBER, "trust"))
    _rewrite(tmp_path, auto.id, approval_grant=None)
    _rewrite(tmp_path, bot.id, approval_grant=None)
    return auto.id, bot.id


def test_the_start_pass_signs_older_grants_under_a_hidden_marker(tmp_path) -> None:
    auto_id, bot_id = _older_store(tmp_path)
    assert job_fire_approval_mode(_reload(tmp_path, auto_id)) == ""

    assert sorted(approval_grant.sign_existing_grants(tmp_path)) == sorted([auto_id, bot_id])

    assert job_fire_approval_mode(_reload(tmp_path, auto_id)) == "auto"
    bot = _reload(tmp_path, bot_id)
    assert owner_grant.owner_grant_holds(bot, _worker(), _config())
    marker = tmp_path / approval_grant.MIGRATION_DIR_LEAF / approval_grant.MIGRATION_MARKER
    assert marker.is_file()
    assert approval_grant.MIGRATION_DIR_LEAF in sandbox_leaves._CREW_HIDDEN_LEAVES


def test_the_start_pass_runs_once(tmp_path) -> None:
    _older_store(tmp_path)
    approval_grant.sign_existing_grants(tmp_path)
    owner_grant.migrate_owner_grants(tmp_path, _config())
    later = CronService(base_dir=tmp_path).add_job(name="c", message="go", every_secs=3600)
    _rewrite(tmp_path, later.id, approval_mode="auto")
    # The owner-grant pass's marker sits in the writable root: removing it re-runs
    # that pass, which must not sign anything either.
    (tmp_path / owner_grant.MIGRATION_MARKER).unlink()

    owner_grant.migrate_owner_grants(tmp_path, _config())
    assert approval_grant.sign_existing_grants(tmp_path) == []

    assert "approval_grant" not in _row(tmp_path, later.id)
    assert job_fire_approval_mode(_reload(tmp_path, later.id)) == ""


def test_the_start_pass_waits_for_a_key(tmp_path, monkeypatch) -> None:
    auto_id, _ = _older_store(tmp_path)
    with monkeypatch.context() as mp:
        mp.setattr(approval_grant, "_key", lambda: None)
        assert approval_grant.sign_existing_grants(tmp_path) == []
    assert not (
        tmp_path / approval_grant.MIGRATION_DIR_LEAF / approval_grant.MIGRATION_MARKER
    ).exists()

    assert approval_grant.sign_existing_grants(tmp_path) != []
    assert job_fire_approval_mode(_reload(tmp_path, auto_id)) == "auto"


def test_a_bot_grant_holds_only_once_the_gateway_signed_it(tmp_path) -> None:
    job = CronService(base_dir=tmp_path).add_job(
        name="retro", message="go", every_secs=3600, owner_grant=(_MEMBER, "trust")
    )
    _rewrite(tmp_path, job.id, approval_grant=None)
    unsigned = _reload(tmp_path, job.id)

    assert not owner_grant.owner_grant_holds(unsigned, _worker(), _config())
    assert owner_grant.fire_approval_mode(unsigned, _worker(), _config()) == ""

    approval_grant.sign(unsigned)

    assert owner_grant.owner_grant_holds(unsigned, _worker(), _config())
    assert owner_grant.fire_approval_mode(unsigned, _worker(), _config()) == "auto"


def test_a_signed_raw_record_holds_once_decoded(tmp_path) -> None:
    """``sign_record`` on a raw dict and ``holds`` on the job decoded from it agree."""
    record = {
        "id": "r1",
        "name": "r",
        "message": "go",
        "schedule": {"kind": "every", "every_secs": 3600},
        "approval_mode": "auto",
        "session_key": "dashboard:slot1",
        "agent_id": "kirocrew",
        "member_id": "m-1",
        "memory_store": "member-m-1",
        "execution_context": {"member_id": "m-1", "store": {"store_id": "s"}, "z": [1, "\u00e9"]},
        "agent_sequence": ["a", "b"],
    }
    assert approval_grant.sign_record(record)

    (job,) = store.decode_jobs(
        json.dumps({"version": store._STORE_VERSION, "jobs": [record]}).encode()
    )

    assert approval_grant.holds(job)
    assert job_fire_approval_mode(job) == "auto"


@pytest.mark.parametrize(
    "fields",
    [
        {"agent_sequence": ["kirocrew", "other"]},
        {"execution_context": {"member_id": "m-other"}},
        {"member_id": "m-other"},
        {"memory_store": "member-other"},
    ],
    ids=("agent_sequence", "execution_context", "member_id", "memory_store"),
)
def test_a_hand_edit_of_what_a_signed_job_runs_as_fires_interactive(tmp_path, fields) -> None:
    job = CronService(base_dir=tmp_path).add_job(
        name="a", message="go", every_secs=3600, approval_mode="auto"
    )
    assert job_fire_approval_mode(_reload(tmp_path, job.id)) == "auto"
    _rewrite(tmp_path, job.id, **fields)

    assert job_fire_approval_mode(_reload(tmp_path, job.id)) == ""


@pytest.mark.parametrize("stored", ["\u00e9", 12345, ["x"]], ids=("non-ascii", "int", "list"))
def test_an_odd_stored_grant_is_kept_fires_interactive_and_is_signed_at_start(
    tmp_path, stored
) -> None:
    job = CronService(base_dir=tmp_path).add_job(
        name="a", message="go", every_secs=3600, approval_mode="auto"
    )
    _rewrite(tmp_path, job.id, approval_grant=stored)

    reloaded = _reload(tmp_path, job.id)
    assert reloaded.approval_mode == "auto"
    assert not approval_grant.holds(_row(tmp_path, job.id))
    assert job_fire_approval_mode(reloaded) == ""

    assert approval_grant.sign_existing_grants(tmp_path) == [job.id]
    assert job_fire_approval_mode(_reload(tmp_path, job.id)) == "auto"


def test_a_failed_save_puts_the_previous_grant_back(tmp_path, monkeypatch) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="a", message="go", every_secs=3600, approval_mode="auto")
    live = svc.get_job(job.id)
    assert live is not None
    grant = live.approval_grant
    assert grant

    def _refuse(*_args, **_kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as mp:
        mp.setattr("kiro_crew.atomic_write.atomic_write", _refuse)
        with pytest.raises(OSError):
            svc.update_job(job.id, approval_mode="")

    assert (live.approval_mode, live.approval_grant) == ("auto", grant)
    assert job_fire_approval_mode(live) == "auto"


def test_a_load_with_the_key_names_each_unsigned_grant_once(tmp_path, caplog) -> None:
    job = CronService(base_dir=tmp_path).add_job(name="a", message="go", every_secs=3600)
    _rewrite(tmp_path, job.id, approval_mode="auto")

    with caplog.at_level("WARNING", logger="kiro_crew.cron"):
        CronService(base_dir=tmp_path)

    lines = [r.getMessage() for r in caplog.records if "did not sign" in r.getMessage()]
    assert len(lines) == 1
    assert job.id in lines[0] and "Schedule page or the CLI" in lines[0]


def test_a_load_without_the_key_says_nothing(tmp_path, caplog, monkeypatch) -> None:
    job = CronService(base_dir=tmp_path).add_job(name="a", message="go", every_secs=3600)
    _rewrite(tmp_path, job.id, approval_mode="auto")

    with monkeypatch.context() as mp, caplog.at_level("WARNING", logger="kiro_crew.cron"):
        mp.setattr(approval_grant, "_key", lambda: None)
        CronService(base_dir=tmp_path)

    assert not [r for r in caplog.records if "did not sign" in r.getMessage()]


def _failing_for(target_name: str):
    """``atomic_write`` that raises OSError for a path named *target_name* only."""
    from kiro_crew import atomic_write as aw

    real = aw.atomic_write

    def _write(path, *args, **kwargs):
        if Path(path).name == target_name:
            raise OSError("no space left on device")
        return real(path, *args, **kwargs)

    return _write


def test_a_failed_marker_write_publishes_no_signature(tmp_path, monkeypatch) -> None:
    auto_id, _ = _older_store(tmp_path)
    before = (tmp_path / "crons.json").read_bytes()

    with monkeypatch.context() as mp:
        mp.setattr(
            "kiro_crew.atomic_write.atomic_write",
            _failing_for(approval_grant.MIGRATION_MARKER),
        )
        assert approval_grant.sign_existing_grants(tmp_path) == []

    assert (tmp_path / "crons.json").read_bytes() == before
    assert "approval_grant" not in _row(tmp_path, auto_id)
    assert job_fire_approval_mode(_reload(tmp_path, auto_id)) == ""
    # Nothing was published, so the next start runs the pass and signs it.
    assert auto_id in approval_grant.sign_existing_grants(tmp_path)
    assert job_fire_approval_mode(_reload(tmp_path, auto_id)) == "auto"


def test_a_failed_store_write_after_the_marker_does_not_run_the_pass_again(
    tmp_path, monkeypatch
) -> None:
    auto_id, _ = _older_store(tmp_path)

    with monkeypatch.context() as mp:
        mp.setattr("kiro_crew.atomic_write.atomic_write", _failing_for("crons.json"))
        assert approval_grant.sign_existing_grants(tmp_path) == []

    marker = tmp_path / approval_grant.MIGRATION_DIR_LEAF / approval_grant.MIGRATION_MARKER
    assert marker.is_file()
    later = CronService(base_dir=tmp_path).add_job(name="c", message="go", every_secs=3600)
    _rewrite(tmp_path, later.id, approval_mode="auto")

    assert approval_grant.sign_existing_grants(tmp_path) == []

    assert "approval_grant" not in _row(tmp_path, later.id)
    assert job_fire_approval_mode(_reload(tmp_path, later.id)) == ""
    assert job_fire_approval_mode(_reload(tmp_path, auto_id)) == ""


def test_an_unreadable_store_spends_the_pass(tmp_path) -> None:
    store_path = tmp_path / "crons.json"
    store_path.write_bytes(b"{not json")

    assert approval_grant.sign_existing_grants(tmp_path) == []

    marker = tmp_path / approval_grant.MIGRATION_DIR_LEAF / approval_grant.MIGRATION_MARKER
    assert marker.is_file()
    assert store_path.read_bytes() == b"{not json"

    store_path.unlink()
    job_id = _hand_written_auto(tmp_path)
    assert approval_grant.sign_existing_grants(tmp_path) == []

    assert "approval_grant" not in _row(tmp_path, job_id)
    assert job_fire_approval_mode(_reload(tmp_path, job_id)) == ""
