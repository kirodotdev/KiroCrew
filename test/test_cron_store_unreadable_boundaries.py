"""``CronStoreUnreadable`` must be TRANSLATED at every user-initiated mutation
boundary, not propagate raw.

The CLI, REST API, MCP tools and Slack must tell the user to move the unreadable
file aside, not show a traceback or a generic 500. Tests drive a real
``CronService`` over a genuinely unreadable ``crons.json``.

A test over a HEALTHY store passes with or without the translation, so every
assertion is about the shape of the failure; the negative controls prove a
legitimate write still succeeds silently.
"""

from __future__ import annotations

import ast
import contextlib
import errno
import functools
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from source_corpus import candidate_sources, src_root

from kiro_crew.cron import CronService, CronStoreBusy, CronStoreUnreadable
from kiro_crew.messaging.commands import cron_remove_all_reply

# One xdist worker for the whole module: the two ratchets at the bottom derive from
# ONE module-cached scan of src/. Under `--dist loadgroup` an unmarked module is spread
# across workers and each worker re-pays that scan. One group PER FILE, never shared:
# a shared group would serialize unrelated modules onto one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_cron_store_unreadable_boundaries")

# An unreadable store: valid-looking JSON bytes that are not valid UTF-8, so
# `_load` fails in the widened handler and `_load_failed` is set. The record it
# carries is real, which is what makes the refusal load-bearing -- overwriting
# would erase "j-keep".
_UNREADABLE = b'{"version": 2, "jobs": [{"id": "j-keep", "name": "keep"}], "n": "\xff\xfe"}'

# The remediation sentence the exception already carries. The boundaries must
# surface it rather than inventing their own wording.
_REMEDIATION = "Move the unreadable file aside"


def _svc_with_unreadable_store(tmp_path: Path) -> CronService:
    """A service whose last load failed, with real records still on disk."""
    (tmp_path / "crons.json").write_bytes(_UNREADABLE)
    svc = CronService(base_dir=tmp_path)
    assert svc._load_failed, "fixture precondition: the load must have failed"
    return svc


def _dashboard_client(svc: CronService) -> TestClient:
    """Minimal app exposing POST /api/crons backed by *svc*."""
    from kiro_crew.dashboard.handlers.cron import api_crons_create

    app = web.Application()
    app["state"] = SimpleNamespace(crons=svc, push_refresh=lambda _k: None)
    app.router.add_post("/api/crons", api_crons_create)
    return TestClient(TestServer(as_owner(app)))


# ── Positive A: the dashboard REST boundary ──


@pytest.mark.asyncio
async def test_dashboard_create_translates_unreadable_store(tmp_path: Path) -> None:
    """A structured non-2xx naming the cause, NOT a generic 500.

    The dashboard middleware chain has no catch-all handler, so an untranslated
    raise leaves aiohttp to answer 500 with no body a client can branch on.
    """
    svc = _svc_with_unreadable_store(tmp_path)
    async with _dashboard_client(svc) as client:
        resp = await client.post("/api/crons", json={"name": "n", "message": "m", "every": 300})

        assert resp.status != 500, "an unreadable store must not surface as a generic 500"
        assert 400 <= resp.status < 600
        body = await resp.json()
        # Machine-readable, per the repo's non-2xx contract.
        assert body.get("code") == "cron_store_unreadable", body
        assert _REMEDIATION in body.get("error", ""), body

    # The refusal is the point: the pre-existing record is still on disk.
    assert b'"j-keep"' in (tmp_path / "crons.json").read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule", [{"every": 300}, {"cron": "0 * * * *"}])
@pytest.mark.parametrize("member", ["unknown", "broken"])
async def test_dashboard_create_refuses_unavailable_member_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schedule: dict, member: str
) -> None:
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

    config = KiroCrewConfig()
    config.agents["broken"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", memory_store="missing-store"
    )
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: config))
    svc = await CronService.create(base_dir=tmp_path)
    keep = await svc.add_job_async("keep", "existing task", every_secs=300)
    before = (tmp_path / "crons.json").read_bytes()
    refresh = Mock()
    async with _dashboard_client(svc) as client:
        client.app["state"].push_refresh = refresh
        resp = await client.post(
            "/api/crons",
            json={"name": "private", "message": "member task", "member_id": member, **schedule},
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["code"] == "invalid_cron"
        reason = (
            "member identity is missing or ambiguous"
            if member == "unknown"
            else "is unavailable; Global was not used"
        )
        assert reason in body["error"]
    refresh.assert_not_called()
    assert (tmp_path / "crons.json").read_bytes() == before
    reloaded = await CronService.create(base_dir=tmp_path)
    assert [job.id for job in reloaded.list_jobs()] == [keep.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule", [{"every": 300}, {"cron": "0 * * * *"}])
async def test_dashboard_create_keeps_valid_member_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schedule: dict
) -> None:
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
    from kiro_crew.memory_stores import provision_member_memory

    config = KiroCrewConfig()
    config.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    store = provision_member_memory(config, "writer")
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: config))
    svc = await CronService.create(base_dir=tmp_path)
    refresh = Mock()
    async with _dashboard_client(svc) as client:
        client.app["state"].push_refresh = refresh
        resp = await client.post(
            "/api/crons",
            json={"name": "private", "message": "member task", "member_id": "writer", **schedule},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
    refresh.assert_called_once_with("crons")
    reloaded = await CronService.create(base_dir=tmp_path)
    job = reloaded.get_job(body["id"])
    assert job is not None
    assert (job.member_id, job.memory_store) == ("writer", store)


# ── Positive B: the CLI boundary -- the reproduction named by review ──


def test_cli_cron_add_translates_unreadable_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``cron add`` must fail with a message, not a traceback.

    The CLI had no cron-store error handling at all, so the exception escaped
    argparse dispatch and printed a stack trace.
    """
    import argparse

    import kiro_crew.cli_commands as cc

    (tmp_path / "crons.json").write_bytes(_UNREADABLE)
    monkeypatch.setattr(cc, "config_dir", lambda: tmp_path)

    args = argparse.Namespace(
        cron_action="add",
        name="n",
        message="m",
        every=300,
        cron_expr=None,
        channel=None,
        approval_mode="",
        agent="",
        silent=False,
    )
    with pytest.raises(SystemExit) as exc:
        cc._cron(args)

    assert exc.value.code != 0, "a refused write must exit non-zero"
    err = capsys.readouterr().err
    assert "cron store" in err.lower(), err
    assert _REMEDIATION in err, err
    assert "Traceback" not in err, err
    assert b'"j-keep"' in (tmp_path / "crons.json").read_bytes()


# ── Positive C: the MCP tool boundary ──


def test_mcp_cron_add_translates_unreadable_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP tool returns an Error string, not an exception to the client.

    The session key is required because ``cron_add`` refuses an unidentified
    caller BEFORE it reaches the store -- without it this test would pass on the
    authz message and never exercise the write at all.
    """
    import kiro_crew.mcp_cron as mc

    (tmp_path / "crons.json").write_bytes(_UNREADABLE)
    monkeypatch.setattr("kiro_crew.mcp_cron.config_dir", lambda: tmp_path)
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:unreadable-probe")

    out = mc._call_tool_inner("cron_add", {"name": "n", "message": "m", "every": 300})

    assert isinstance(out, str)
    assert "session" not in out.lower(), f"authz gate fired, test is vacuous: {out}"
    assert "cron store" in out.lower(), out
    assert _REMEDIATION in out, out
    assert b'"j-keep"' in (tmp_path / "crons.json").read_bytes()


# ── NEGATIVE CONTROL 2: a legitimate write must stay silent and successful ──


@pytest.mark.asyncio
async def test_absent_store_still_creates_silently(tmp_path: Path) -> None:
    """NC2, first half: a fresh install has no crons.json at all."""
    assert not (tmp_path / "crons.json").exists()
    svc = CronService(base_dir=tmp_path)
    assert not svc._load_failed

    async with _dashboard_client(svc) as client:
        resp = await client.post("/api/crons", json={"name": "fresh", "message": "m", "every": 300})
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["ok"] is True

    assert b'"fresh"' in (tmp_path / "crons.json").read_bytes()


@pytest.mark.asyncio
async def test_honestly_empty_store_still_creates_silently(tmp_path: Path) -> None:
    """NC2, second half: an EMPTY job list is not an unreadable one."""
    (tmp_path / "crons.json").write_text(json.dumps({"version": 2, "jobs": []}), encoding="utf-8")
    svc = CronService(base_dir=tmp_path)
    assert not svc._load_failed

    async with _dashboard_client(svc) as client:
        resp = await client.post("/api/crons", json={"name": "empty", "message": "m", "every": 300})
        assert resp.status == 200, await resp.text()

    assert b'"empty"' in (tmp_path / "crons.json").read_bytes()


# ── The IN-MEMORY contract: a rejected mutation must leave no trace ──
#
# Both add paths mutate `self._jobs` before `_save()`, and `_tick_scan_locked`
# falls back to `list(self._jobs)` on a contended lock, so a refused job left in
# memory could still be scanned as due. `_save()` raises either way, so every
# assertion below is about `self._jobs`, not about the exception.


def test_a_rejected_add_leaves_no_job_in_memory(tmp_path: Path) -> None:
    """`add_job` over an unreadable store must not leave the job in memory."""
    svc = _svc_with_unreadable_store(tmp_path)

    with pytest.raises(CronStoreUnreadable):
        svc.add_job(name="rejected-job", message="m", every_secs=3600)

    assert [j.name for j in svc._jobs] == [], (
        "a rejected add must leave no job in memory; the caller was told the "
        f"write was refused, but the scheduler can still see {[j.name for j in svc._jobs]}"
    )
    # The refusal must still protect the file it could not read.
    assert b'"j-keep"' in (tmp_path / "crons.json").read_bytes()


def test_a_rejected_add_if_absent_leaves_no_job_in_memory(tmp_path: Path) -> None:
    """The add-if-absent path has the identical append-then-save shape."""
    svc = _svc_with_unreadable_store(tmp_path)

    with pytest.raises(CronStoreUnreadable):
        svc.add_job_if_absent(
            lambda existing: existing.name == "rejected-2",
            name="rejected-2",
            message="m",
            every_secs=3600,
        )

    assert [
        j.name for j in svc._jobs
    ] == [], f"add_job_if_absent left {[j.name for j in svc._jobs]} in memory"


def test_a_contended_due_scan_cannot_see_a_rejected_job(tmp_path: Path) -> None:
    """The tick's documented in-memory-snapshot fallback must stay clean.

    `_tick_scan_locked` skips `_sync()` when the store lock is contended and
    returns `list(self._jobs)`, so a residual rejected job would be handed to
    the runner. Driving the fallback requires a contended lock, which is why
    `_file_lock` is replaced with one that raises `CronStoreBusy`.
    """
    svc = _svc_with_unreadable_store(tmp_path)
    with pytest.raises(CronStoreUnreadable):
        svc.add_job(name="rejected-job", message="m", every_secs=3600)

    @contextlib.contextmanager
    def _contended(*_args: object, **_kwargs: object) -> Iterator[None]:
        raise CronStoreBusy("contended")
        yield  # pragma: no cover - unreachable, keeps this a generator

    svc._file_lock = _contended  # type: ignore[method-assign]

    seen = svc._tick_scan_locked()
    assert [
        j.name for j in seen
    ] == [], f"the contended due-scan can see a rejected job: {[j.name for j in seen]}"


# ── NEGATIVE CONTROL 3: a healthy store must still append on BOTH add paths ──


def test_a_healthy_store_still_appends_on_both_add_paths(tmp_path: Path) -> None:
    """NC3: the refusal must be invisible when the last load succeeded.

    A guard that fires on a readable store would break creation itself to
    satisfy the in-memory contract above.
    """
    svc = CronService(base_dir=tmp_path)
    assert not svc._load_failed

    svc.add_job(name="healthy-a", message="m", every_secs=3600)
    svc.add_job_if_absent(
        lambda existing: existing.name == "healthy-b",
        name="healthy-b",
        message="m",
        every_secs=3600,
    )

    assert sorted(j.name for j in svc._jobs) == ["healthy-a", "healthy-b"]
    on_disk = (tmp_path / "crons.json").read_bytes()
    assert b'"healthy-a"' in on_disk and b'"healthy-b"' in on_disk


# ── The refusal must UN-LATCH once the operator follows the remediation ──
#
# Only `_load` clears the latch, so `_sync` must reach it even when the path is
# missing; otherwise a long-lived gateway refuses every write until a restart.
# Every assertion is about what happens AFTER the store changes on disk.


def test_removing_the_unreadable_store_unlatches_the_refusal(tmp_path: Path) -> None:
    """POSITIVE A: the remediation the error message prints must actually work."""
    svc = _svc_with_unreadable_store(tmp_path)
    with pytest.raises(CronStoreUnreadable):
        svc.add_job(name="refused", message="m", every_secs=3600)

    # Exactly what the message instructs, on the same live service object.
    (tmp_path / "crons.json").unlink()

    job = svc.add_job(name="after-remediation", message="m", every_secs=3600)
    assert job.name == "after-remediation"
    assert not svc._load_failed, "the latch must clear once the store is gone"
    assert [j.name for j in svc._jobs] == ["after-remediation"]
    assert b'"after-remediation"' in (tmp_path / "crons.json").read_bytes()


def test_a_still_present_corrupt_store_keeps_refusing(tmp_path: Path) -> None:
    """NEGATIVE CONTROL 2: un-latching a store that is STILL corrupt would
    re-open the data loss this refusal exists to close, so the refusal must
    survive any number of syncs while the bad file is still there."""
    svc = _svc_with_unreadable_store(tmp_path)

    for _ in range(3):
        with pytest.raises(CronStoreUnreadable):
            svc.add_job(name="still-refused", message="m", every_secs=3600)
        assert svc._load_failed, "the latch must stay set while the file is unreadable"

    # The records it could not read are still on disk, unharmed.
    assert b'"j-keep"' in (tmp_path / "crons.json").read_bytes()


def test_a_repaired_store_heals_on_the_next_sync(tmp_path: Path) -> None:
    """A store repaired in place clears the latch through the digest path.

    Both `_load` failure paths reset the fingerprint, so the next `_sync` over a
    file still present reloads and re-evaluates the latch.
    """
    svc = _svc_with_unreadable_store(tmp_path)
    with pytest.raises(CronStoreUnreadable):
        svc.add_job(name="refused", message="m", every_secs=3600)

    # Repaired in place rather than removed -- the digest branch, not the fix's.
    (tmp_path / "crons.json").write_text(json.dumps({"version": 2, "jobs": []}), encoding="utf-8")

    svc.add_job(name="after-repair", message="m", every_secs=3600)
    assert not svc._load_failed


# ── `_sync`'s OWN read branch must latch too, not just `_load`'s parse paths ──
#
# `_sync` reads the file itself before deciding to reload, so an OS-level read
# error (EIO, EACCES) after a good load never reaches `_load`. Unless `_sync`
# latches too, `_save` writes stale memory straight over the file.
#
# The error is injected at the filesystem boundary: a real EACCES is vacuous
# under root, and a directory at the path also fails the rename, masking the
# clobber T1 must observe. The real `_sync` -> latch -> `_save` chain still runs.


def _loadable_store(tmp_path: Path, name: str) -> str:
    """A schema-VALID store document carrying one job named *name*.

    Built by the real writer in a scratch dir rather than hand-rolled: a literal
    missing one required key is dropped as malformed by `_load`, which would make
    the store below load EMPTY and the tests pass on a fixture artefact.
    """
    scratch = tmp_path / f"_mk_{name}"
    maker = CronService(base_dir=scratch)
    maker.add_job(name=name, message="m", every_secs=3600)
    return (scratch / "crons.json").read_text(encoding="utf-8")


@contextlib.contextmanager
def _read_failures_on(target: Path) -> Iterator[None]:
    """Make ``read_bytes()`` raise EIO for *target* only, leaving writes working."""
    real = Path.read_bytes

    def failing(self: Path) -> bytes:
        if self == target:
            raise OSError(errno.EIO, "Input/output error")
        return real(self)

    Path.read_bytes = failing  # type: ignore[method-assign]
    try:
        yield
    finally:
        Path.read_bytes = real  # type: ignore[method-assign]


def test_a_read_failure_latches_so_a_newer_store_is_not_clobbered(tmp_path: Path) -> None:
    """POSITIVE 1 (the harm): stale memory must not overwrite what it could not read.

    The vacuous-pass trap: asserting `_jobs` still holds the old job passes with
    and without the latch, because the in-memory list is identical in both
    worlds. The assertion carrying the coverage is the FILE -- the externally
    written record has to still be there afterwards.
    """
    store = tmp_path / "crons.json"
    store.write_text(_loadable_store(tmp_path, "old"), encoding="utf-8")
    svc = CronService(base_dir=tmp_path)
    assert not svc._load_failed, "precondition: a healthy store must load cleanly"
    assert [j.name for j in svc._jobs] == ["old"], "precondition: the record must LOAD"

    # Another writer replaces the store, and only then does the file stop being
    # readable -- so in-memory state is now strictly older than the disk.
    store.write_text(_loadable_store(tmp_path, "new-external"), encoding="utf-8")

    with _read_failures_on(store):
        with pytest.raises(CronStoreUnreadable):
            svc.add_job(name="stale-writer", message="m", every_secs=3600)
        assert svc._load_failed, "the read failure itself must set the latch"

    on_disk = store.read_bytes()
    assert b'"new-external"' in on_disk, "the unread record must survive the refusal"
    assert b'"stale-writer"' not in on_disk


def test_a_transient_read_failure_does_not_brick_an_unchanged_store(tmp_path: Path) -> None:
    """POSITIVE 2 (the fingerprint reset): the latch must not outlive the fault.

    The bytes on disk never change, so unless the latch also clears the digest the
    next `_sync` matches its fingerprint, skips the reload that clears the latch,
    and a healthy store refuses every write until a restart.
    """
    store = tmp_path / "crons.json"
    tracked = _loadable_store(tmp_path, "old")
    store.write_text(tracked, encoding="utf-8")
    svc = CronService(base_dir=tmp_path)
    assert [j.name for j in svc._jobs] == ["old"], "precondition: the record must LOAD"

    with _read_failures_on(store):
        with pytest.raises(CronStoreUnreadable):
            svc.add_job(name="during-outage", message="m", every_secs=3600)

    # The fault clears with the bytes on disk byte-identical to what was tracked.
    assert store.read_text(encoding="utf-8") == tracked

    job = svc.add_job(name="after-outage", message="m", every_secs=3600)
    assert not svc._load_failed, "a transient read fault must not latch permanently"
    assert job.name == "after-outage"
    assert b'"after-outage"' in store.read_bytes()


def test_a_kept_record_does_not_come_back_once_the_unreadable_store_is_moved_aside(
    tmp_path: Path,
) -> None:
    """The kept records came from the store the operator moved aside, like the jobs."""
    store = tmp_path / "crons.json"
    document = json.loads(_loadable_store(tmp_path, "old"))
    document["jobs"].append({"id": "kept", "message": "m", "schedule": {"kind": "every"}})
    store.write_text(json.dumps(document), encoding="utf-8")
    svc = CronService(base_dir=tmp_path)
    assert [j.name for j in svc._jobs] == ["old"], "precondition: the record must LOAD"

    with _read_failures_on(store):
        with pytest.raises(CronStoreUnreadable):
            svc.add_job(name="during-outage", message="m", every_secs=3600)
    store.unlink()

    svc.add_job(name="fresh", message="m", every_secs=3600)
    assert [rec.get("name") for rec in json.loads(store.read_bytes())["jobs"]] == ["fresh"]


# ── The TEARDOWN contract: an empty owned set is not an authoritative one ──
#
# `_load` degrades an unreadable store to an empty list, so uninstall would read
# "this app owns zero jobs", delete the app, and leave its ENABLED jobs on disk to
# resume once the store parses. `_jobs == []` holds either way, so every assertion
# is about the teardown outcome: the removal refuses and the boundary propagates it.

# An unreadable store whose surviving record is APP-OWNED and ENABLED. That is
# what makes the refusal load-bearing: a teardown that trusts the empty set
# deletes the app and leaves this job to resume.
_UNREADABLE_APP_OWNED = (
    b'{"version": 2, "jobs": [{"id": "j-app", "name": "owned", "enabled": true, '
    b'"created_by": "app:demo"}], "n": "\xff\xfe"}'
)


def _svc_with_unreadable_app_owned_store(tmp_path: Path) -> CronService:
    """A service whose last load failed over a store holding an app-owned job."""
    (tmp_path / "crons.json").write_bytes(_UNREADABLE_APP_OWNED)
    svc = CronService(base_dir=tmp_path)
    assert svc._load_failed, "fixture precondition: the load must have failed"
    assert svc._jobs == [], "fixture precondition: the degraded load yields an empty set"
    return svc


@pytest.mark.asyncio
async def test_owner_teardown_refuses_over_an_unreadable_store(tmp_path: Path) -> None:
    """The selection itself must refuse; returning [] would mean "owned nothing".

    Over an unreadable store the in-lock reload yields nothing, so the removal never
    reaches `_save()`, the only raiser on this path.
    """
    svc = _svc_with_unreadable_app_owned_store(tmp_path)
    with pytest.raises(CronStoreUnreadable) as exc:
        await svc.remove_jobs_by_owner("app:demo")
    assert _REMEDIATION in str(exc.value)
    # The job the caller was NOT told about is still on disk, unharmed.
    assert b'"j-app"' in (tmp_path / "crons.json").read_bytes()


@pytest.mark.asyncio
async def test_app_deregister_propagates_unreadable_rather_than_reporting_zero(
    tmp_path: Path,
) -> None:
    """The boundary must not mask the refusal as a successful zero.

    `CronStoreUnreadable` is a sibling of `CronStoreBusy`, not a subclass, so
    `deregister_app_crons_from_service` needs its own arm or the generic handler
    returns 0.
    """
    from kiro_crew.apps.bridges import deregister_app_crons_from_service

    svc = _svc_with_unreadable_app_owned_store(tmp_path)
    with pytest.raises(CronStoreUnreadable) as exc:
        await deregister_app_crons_from_service("demo", svc)
    assert _REMEDIATION in str(exc.value)


@pytest.mark.asyncio
async def test_absent_store_still_tears_down_cleanly(tmp_path: Path) -> None:
    """NC, first half: a fresh install has no crons.json, and uninstall must work.

    A fail-closed check tight enough to break this would trade the finding for a
    real regression. `_load` returns at its `exists()` check WITHOUT setting
    `_load_failed`, which is what keeps the two cases distinguishable.
    """
    from kiro_crew.apps.bridges import deregister_app_crons_from_service

    assert not (tmp_path / "crons.json").exists()
    svc = CronService(base_dir=tmp_path)
    assert not svc._load_failed
    assert await deregister_app_crons_from_service("demo", svc) == 0
    assert await svc.remove_jobs_by_owner("app:demo") == []


@pytest.mark.asyncio
async def test_honestly_empty_store_still_tears_down_cleanly(tmp_path: Path) -> None:
    """NC, second half: an EMPTY job list is not an unreadable one."""
    from kiro_crew.apps.bridges import deregister_app_crons_from_service

    (tmp_path / "crons.json").write_text(json.dumps({"version": 2, "jobs": []}), encoding="utf-8")
    svc = CronService(base_dir=tmp_path)
    assert not svc._load_failed
    assert await deregister_app_crons_from_service("demo", svc) == 0
    assert await svc.remove_jobs_by_owner("app:demo") == []


# ── The DISABLE path: report the refusal, never crash and never claim success ──
#
# `hooks_integration`'s disable hook must record a "failed" cleanup note for
# `CronStoreUnreadable` as it does for its sibling `CronStoreBusy`. A crash also
# fails to report success, so the assertion is on the reported note and on the
# hook returning at all.


_CRON_APP_INFO = {"manifest": {"permissions": {"cron": True}}}


def _dispatcher_stub(svc: CronService) -> SimpleNamespace:
    """Minimal lifecycle dispatcher: the cron service plus the startup-ownership
    probe `on_app_disable` calls before it touches teardown state."""

    async def _stop(app_name: str, *, bounded: bool = False) -> bool:
        return True

    return SimpleNamespace(_cron_service=svc, stop_detached_startup_hooks=_stop)


@pytest.mark.asyncio
async def test_disable_reports_unreadable_cleanup_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disable hook must record a 'failed' cleanup note, not propagate."""
    from kiro_crew.apps import hooks_integration as hi

    svc = _svc_with_unreadable_app_owned_store(tmp_path)
    monkeypatch.setattr(hi, "_lifecycle_dispatcher", _dispatcher_stub(svc))
    result = await hi.on_app_disable("demo", _CRON_APP_INFO, run_app_hooks=False)
    note = str(result.get("cron_cleanup", ""))
    assert note.startswith("failed:"), result
    assert "unreadable" in note.lower(), result


@pytest.mark.asyncio
async def test_disable_over_a_healthy_store_still_reports_no_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NC: a readable store must still clean up without a failure note.

    Guards against buying the lane by turning every store read into a refusal.
    """
    from kiro_crew.apps import hooks_integration as hi

    (tmp_path / "crons.json").write_text(json.dumps({"version": 2, "jobs": []}), encoding="utf-8")
    svc = CronService(base_dir=tmp_path)
    assert not svc._load_failed
    monkeypatch.setattr(hi, "_lifecycle_dispatcher", _dispatcher_stub(svc))
    result = await hi.on_app_disable("demo", _CRON_APP_INFO, run_app_hooks=False)
    assert not str(result.get("cron_cleanup", "")).startswith("failed:"), result


# ── An unpersisted REMOVAL must stay queued, or the one-shot fires twice ──
#
# The deferred drain claims `_pending_removals` with an atomic swap before the
# save it might lose, and the run-result merge removes a `delete_after_run` job
# in memory. Both must requeue on `CronStoreUnreadable`, or the next healthy
# `_sync` reloads the job and a completed one-shot runs and notifies again.
#
# Nothing is persisted either way, so the assertions are that the intent survives
# and that the delete lands once the store is readable.


def test_a_failed_drain_requeues_so_the_one_shot_is_not_redelivered(tmp_path: Path) -> None:
    """POSITIVE 1: an unpersisted deferred removal must be retried, not dropped."""
    store = tmp_path / "crons.json"
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="one-shot", message="m", every_secs=3600, delete_after_run=True)
    assert b'"one-shot"' in store.read_bytes(), "precondition: the job is on disk"

    svc.defer_removal(job.id)
    assert svc._pending_removals == {job.id}, "precondition: the intent is queued"

    # The tick's own order: sync (which latches on the read failure), then drain.
    with _read_failures_on(store):
        svc._sync()
        assert svc._load_failed, "precondition: the store must be refusing writes"
        assert svc._drain_pending_removals_locked() == []

    assert job.id in svc._pending_removals, "the removal intent must survive an unpersisted drain"

    # Behavioural half: once readable, the retry must actually delete it.
    svc._sync()
    assert svc._drain_pending_removals_locked() == [job.id]
    assert b'"one-shot"' not in store.read_bytes(), "the retried delete must reach disk"


def test_a_failed_result_merge_requeues_the_consumed_one_shot(tmp_path: Path) -> None:
    """POSITIVE 2: the run-result merge must not drop the consume either."""
    store = tmp_path / "crons.json"
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="ran-once", message="m", every_secs=3600, delete_after_run=True)
    job.last_status = "ok"

    with _read_failures_on(store):
        svc._merge_job_result(job)

    assert job.id in svc._pending_removals, "a consumed one-shot must stay queued for deletion"

    svc._sync()
    assert svc._drain_pending_removals_locked() == [job.id]
    assert b'"ran-once"' not in store.read_bytes()


# ── The missing-store remedy must not write back a stale snapshot ──
#
# `_sync`'s missing-file branch clears the latch; a snapshot from before the store
# became unreadable must not survive it, or the next `_save()` resurrects jobs
# another writer removed. The assertion is the removed job's absence from the file.


def test_the_missing_store_remedy_does_not_resurrect_a_removed_job(tmp_path: Path) -> None:
    """POSITIVE 3: clearing the latch must not license a stale write."""
    store = tmp_path / "crons.json"
    store.write_text(_loadable_store(tmp_path, "removed-elsewhere"), encoding="utf-8")
    svc = CronService(base_dir=tmp_path)
    assert [j.name for j in svc._jobs] == ["removed-elsewhere"], "precondition: it must LOAD"

    # Another writer deletes it, and only then does the file stop being readable,
    # so our in-memory snapshot is now strictly older than the store.
    store.write_text(json.dumps({"version": 2, "jobs": []}), encoding="utf-8")
    with _read_failures_on(store):
        svc._sync()
        assert svc._load_failed
        assert [j.name for j in svc._jobs] == ["removed-elsewhere"], "the snapshot is stale here"

    # Exactly what the refusal message instructs: move the unreadable file aside.
    store.unlink()

    svc.add_job(name="fresh", message="m", every_secs=3600)
    on_disk = store.read_bytes()
    assert b'"fresh"' in on_disk, "the remediation must still leave the store writable"
    assert (
        b'"removed-elsewhere"' not in on_disk
    ), "a job removed by another writer must stay removed"


# ── A consumed one-shot must not re-run when the store was corrupt at merge ──
#
# The result merge answers two questions: "did I remove a present job?" (the
# audit, keyed on presence) and "is a delete owed?" (the queue, which must fire
# even when the read failed). A corrupt store empties `_jobs` without raising, so
# the queue must not be keyed on presence. A logged warning is not enough: once
# the store is readable again the one-shot must be gone.


def test_a_corrupt_store_at_result_merge_does_not_let_the_one_shot_rerun(
    tmp_path: Path,
) -> None:
    """POSITIVE: the consume survives a corrupt-store merge and then lands."""
    store = tmp_path / "crons.json"
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="one-shot", message="m", every_secs=3600, delete_after_run=True)
    healthy = store.read_bytes()
    assert b'"one-shot"' in healthy, "precondition: the job is on disk"

    # Corrupt in place: invalid UTF-8, so `_load` fails and latches WITHOUT raising.
    store.write_bytes(healthy[:-1] + b"\xff\xfe}")
    svc._sync()
    assert svc._load_failed, "precondition: the store must be unreadable"
    assert svc._jobs == [], "precondition: _load empties the list on this path"
    # Pin the CLASS: a fixture that merely made the lock contended would raise
    # CronStoreBusy and the test would pass for the wrong reason.
    with pytest.raises(CronStoreUnreadable):
        svc._save()

    svc._merge_job_result(job)

    assert job.id in svc._pending_removals, "the owed consume must be queued for retry"

    # Repaired, and the disk copy still holds the one-shot -- so without the
    # queued consume it would run again.
    store.write_bytes(healthy)
    svc._sync()
    assert [j.name for j in svc._jobs] == ["one-shot"], "the store restored it, as it would live"

    assert svc._drain_pending_removals_locked() == [job.id]
    assert b'"one-shot"' not in store.read_bytes(), "THE HARM: it must not survive to re-run"


def test_a_corrupt_store_at_result_merge_files_no_audit_record(tmp_path: Path) -> None:
    """NEGATIVE: queueing the retry must not also claim a removal happened.

    The audit stays keyed on presence: nothing was removed from a store that could
    not be read.
    """
    store = tmp_path / "crons.json"
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="one-shot", message="m", every_secs=3600, delete_after_run=True)
    healthy = store.read_bytes()
    store.write_bytes(healthy[:-1] + b"\xff\xfe}")

    audited: list[str] = []
    svc.audit_one_shot_removal = lambda jid, reason: audited.append(jid)  # type: ignore[method-assign]
    svc._merge_job_result(job)

    assert job.id in svc._pending_removals, "the retry is still queued"
    assert audited == [], "no removal reached disk, so no removal may be audited"


# ── A refused mutation must not have already altered live scheduler state ──
#
# The refusal must come before `self._jobs` is mutated: the timer reads that
# cache, so a "failed" resume could still fire. Only `_sync`'s EIO branch latches
# with the jobs still in memory (parse failures empty the list), so that is the
# shape driven below. Both orders raise, so the assertion is the cached state.


def test_a_read_failure_refuses_resume_without_enabling_the_cached_job(
    tmp_path: Path,
) -> None:
    """POSITIVE: a refused resume must leave the in-memory job still paused."""
    store = tmp_path / "crons.json"
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="paused-job", message="m", every_secs=3600)
    svc.enable_job(job.id, enabled=False)
    assert [(j.enabled, j.user_paused) for j in svc._jobs] == [(False, True)], "precondition"

    with _read_failures_on(store):
        with pytest.raises(CronStoreUnreadable):
            svc.enable_job(job.id, enabled=True)
        # THE HARM: the timer reads this cache, so a refused resume that left the
        # job enabled would run a job the caller was told had not been resumed.
        assert [(j.enabled, j.user_paused) for j in svc._jobs] == [
            (False, True)
        ], "a refused resume must not enable the cached job"


def test_a_read_failure_refuses_an_ack_without_consuming_it(tmp_path: Path) -> None:
    """POSITIVE: the same contract on a second user-facing mutator.

    Acking's mutation is not observable from `enabled`, so this catches a fix
    applied only to the resume path.
    """
    store = tmp_path / "crons.json"
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="ackable", message="m", every_secs=3600)
    assert svc._jobs[0].acked_items == [], "precondition: nothing acked yet"

    with _read_failures_on(store):
        with pytest.raises(CronStoreUnreadable):
            svc.ack_job(job.id, "summary-text")
        assert svc._jobs[0].acked_items == [], "a refused ack must not be recorded in memory"


# ── A queued one-shot removal must survive a store that will not PARSE ──
#
# A PARSE failure (unlike the EIO above) empties `self._jobs`, so the drain's
# intersection with `present` is empty and it returns at `if not to_remove`
# before the requeue arm. "Absent means removed elsewhere" holds only after a
# successful load; under a failed one the list is unknown, not empty.
#
# Assert both that the intent survives and that the delete lands once repaired:
# a fix that requeues but never retries would satisfy the first alone.


def test_a_corrupt_store_does_not_discard_a_queued_one_shot_removal(tmp_path: Path) -> None:
    """POSITIVE: an unparseable store must not consume the deferred-removal queue."""
    store = tmp_path / "crons.json"
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="one-shot", message="m", every_secs=3600, delete_after_run=True)
    healthy = store.read_bytes()
    assert b'"one-shot"' in healthy, "precondition: the job is on disk"

    svc.defer_removal(job.id)
    assert svc._pending_removals == {job.id}, "precondition: the intent is queued"

    # Invalid UTF-8, so `_load` fails to PARSE (not to read) and empties `_jobs`.
    store.write_bytes(healthy[:-1] + b"\xff\xfe}")
    svc._sync()
    assert svc._load_failed, "precondition: the store must be refusing writes"
    assert svc._jobs == [], "precondition: the parse failure emptied the in-memory list"

    assert svc._drain_pending_removals_locked() == []
    assert job.id in svc._pending_removals, "a queued one-shot removal must not be discarded"

    # Behavioural half: once the store parses again, the retry must actually delete.
    store.write_bytes(healthy)
    svc._sync()
    assert svc._drain_pending_removals_locked() == [job.id]
    assert b'"one-shot"' not in store.read_bytes(), "the retried delete must reach disk"


# ── The probe obligation, pinned ───────────────────────────────────────────
#
# `CronService.raise_if_store_unreadable` is a per-caller obligation. Writes are
# guarded by `_sync_for_write`, but a read degrades silently, so a caller that
# reads, decides and then writes can find nothing to do and report success over
# a corrupt store. These tests enumerate the read-decide-write callers from
# source and require each to probe or carry a written exemption.
_PROBE = "raise_if_store_unreadable"

#: The one module the scan skips: the store service itself (see the scan below).
_STORE_MODULE = "src/kiro_crew/cron.py"

#: Public reads. A read never raises on an unreadable store -- that IS the hazard.
_CRON_READS = frozenset(
    {
        "count_enabled_from_disk",
        "get_job",
        "get_job_async",
        "list_jobs",
        "list_jobs_async",
    }
)

#: Public mutators. Each already refuses via `_sync_for_write` / `_save`.
_CRON_WRITES = frozenset(
    {
        "ack_job",
        "ack_job_async",
        "add_job",
        "add_job_async",
        "add_job_if_absent",
        "add_job_if_absent_async",
        "add_managed_job",
        "enable_job",
        "enable_job_async",
        "remove_job",
        "remove_job_async",
        "remove_jobs",
        "remove_jobs_by_owner",
        "remove_jobs_by_owner_sync",
        "remove_jobs_sync",
        "unack_job",
        "unack_job_async",
        "update_job",
        "update_job_async",
    }
)

#: Callers that read and write but CANNOT exhibit the defect, each with the
#: structural reason. An exemption is a visible, reviewable line on purpose --
#: adding one should feel like a claim someone can check, because it is.
_EXEMPT_READ_DECIDE_WRITE: dict[tuple[str, str], str] = {
    ("src/kiro_crew/apps/bridges_crons.py", "register_app_crons_with_service"): (
        "An empty read makes every definition look ABSENT, so the add is still "
        "attempted and refuses at the write. The degraded read biases TOWARD the "
        "write rather than around it, so the refusal cannot be skipped."
    ),
    ("src/kiro_crew/onboarding_import.py", "_write_schedule"): (
        "Same shape: no matching job means it falls through to `add_job`, which "
        "refuses. Absence routes INTO the write, so the store cannot stay silent."
    ),
    ("src/kiro_crew/dashboard/state_parts/sidebar_stores.py", "delete_cron_folder"): (
        "The authoritative write is `save_cron_folders()` on a DIFFERENT store and "
        "happens BEFORE the read. The job loop that follows is documented "
        "best-effort cleanup whose per-job failure is benign (an unknown folder_id "
        "renders as ungrouped and self-heals), so the outcome this reports does not "
        "depend on the cron read at all."
    ),
    ("src/kiro_crew/cli_commands_cron.py", "_cron_dispatch"): (
        "A verb dispatcher: the read and the writes are ALTERNATIVE branches of one "
        "request, never a read that decides a write. Its own dispatch-boundary "
        "wrapper already translates the refusal for every verb, including ones "
        "added later."
    ),
    ("src/kiro_crew/mcp_cron.py", "_call_tool_inner"): (
        "The same verb-dispatcher shape as `_cron_dispatch` -- one branch per tool, "
        "so no branch reads and then decides a write."
    ),
}


def _called_attribute_names(fn: ast.AST) -> set[str]:
    """Every ``obj.NAME(...)`` attribute called anywhere inside ``fn``."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def _probes_the_store(fn: ast.AST) -> bool:
    """Whether ``fn`` reaches the probe, by either sanctioned spelling.

    A direct ``svc.raise_if_store_unreadable()`` is one; the other is
    ``getattr(svc, "raise_if_store_unreadable", None)``, used where the service is
    duck-typed and a fake need not carry every method.
    """
    if _PROBE in _called_attribute_names(fn):
        return True
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and any(isinstance(arg, ast.Constant) and arg.value == _PROBE for arg in node.args)
        ):
            return True
    return False


def _scan_module_for_read_decide_write(source: str, rel: str) -> list[tuple[str, str, int, bool]]:
    """Functions in ``source`` that call BOTH a cron read and a cron write.

    Returns ``(rel, func_name, lineno, probes)``. Shared by the real scan and by
    the synthetic control below, so the control exercises the same code path the
    enforcement does -- a scanner proved only against real files could be broken
    in a way that makes the enforcement vacuous.
    """
    found: list[tuple[str, str, int, bool]] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called = _called_attribute_names(node)
        if called & _CRON_READS and called & _CRON_WRITES:
            found.append((rel, node.name, node.lineno, _probes_the_store(node)))
    return found


@functools.lru_cache(maxsize=1)
def _read_decide_write_callers() -> tuple[tuple[str, str, int, bool], ...]:
    """Scan the shipped package for read-decide-write cron callers, once.

    ``cron.py`` is excluded because it IS the service: its internals reach the
    store directly and are guarded by `_sync_for_write`, not by the public probe.
    Tests are excluded because a test may model a bad caller deliberately.

    Cached because two ratchets share the result. Files come from
    ``test/source_corpus.py``, which NFKC-folds like CPython identifiers (so a
    homoglyph spelling is still caught) and records undecodable files rather than
    swallowing them; a file lacking either name set cannot match, so skipping it
    is safe.
    """
    root = src_root()
    writers = {path for path, _text in candidate_sources(require_any=sorted(_CRON_WRITES))}
    callers: list[tuple[str, str, int, bool]] = []
    for path, text in candidate_sources(require_any=sorted(_CRON_READS)):
        if path not in writers:
            continue
        # `.as_posix()`, NOT `str(path)`: `str()` renders the OS-native separator, so on
        # Windows the `/tests/` filter below would never fire and the backslash-spelled
        # `rel` would match no key in the forward-slash-keyed `_EXEMPT_READ_DECIDE_WRITE`
        # -- reporting every exempt caller as unprobed and every exemption as stale at
        # once. POSIX is the portable spelling and the one the table is keyed in.
        posix = path.as_posix()
        if "__pycache__" in posix:
            continue
        if "/tests/" in posix or path.name.startswith("test_"):
            continue
        # Keyed as `src/kiro_crew/...` from the package root, not from the checkout,
        # so the exemption keys resolve identically for an editable and an installed
        # package layout.
        rel = "src/kiro_crew/" + path.relative_to(root).as_posix()
        # Only the store module itself is the service. Matched on the full package
        # path, not the bare name: a name match also hid the dashboard's own
        # `handlers/cron.py` and the grant handlers split out of it.
        if rel == _STORE_MODULE:
            continue
        callers.extend(_scan_module_for_read_decide_write(text, rel))
    return tuple(callers)


def test_the_read_and_write_name_sets_still_match_the_service() -> None:
    """Guard the scan's own inputs: a rename must not silently empty it.

    Without this, renaming a mutator turns every scan below into a zero that
    reads as "no vulnerable callers" -- a fact about the name list, not the code.
    """
    for name in sorted(_CRON_READS | _CRON_WRITES):
        assert callable(getattr(CronService, name, None)), (
            f"{name!r} is no longer a CronService method, so the read-decide-write "
            "scan is blind to it. Update _CRON_READS/_CRON_WRITES with the new name."
        )
    assert callable(
        getattr(CronService, _PROBE, None)
    ), f"CronService.{_PROBE} is gone; the obligation these tests pin cannot hold."


def test_the_scanner_flags_an_unprobed_read_decide_write_caller() -> None:
    """Negative control: prove the scan can FAIL for the intended reason.

    Deliberately synthetic rather than keyed on a real caller, so this control
    stays valid however the shipped callers change -- including if the only
    probing caller today is later moved or removed.
    """
    unprobed = "async def arm(svc):\n    jobs = svc.list_jobs_async(True)\n    svc.enable_job_async(jobs[0].id)\n"
    hits = _scan_module_for_read_decide_write(unprobed, "synthetic.py")
    assert [(h[1], h[3]) for h in hits] == [("arm", False)], (
        "the scanner must flag a read-then-write function that never probes; " f"got {hits!r}"
    )

    probed = (
        "async def arm(svc):\n"
        "    jobs = svc.list_jobs_async(True)\n"
        "    svc.raise_if_store_unreadable()\n"
        "    svc.enable_job_async(jobs[0].id)\n"
    )
    hits = _scan_module_for_read_decide_write(probed, "synthetic.py")
    assert [(h[1], h[3]) for h in hits] == [("arm", True)], (
        "adding the probe must flip the same function to compliant; " f"got {hits!r}"
    )

    # And a read-only function is not a candidate at all -- otherwise the
    # enforcement would demand the probe from callers that never write.
    read_only = "def show(svc):\n    return svc.list_jobs()\n"
    assert _scan_module_for_read_decide_write(read_only, "synthetic.py") == []

    # The duck-typed spelling must count too. Without this the scan would read a
    # real guard as missing wherever the service is `CronService | Any`.
    via_getattr = (
        "async def wipe(svc):\n"
        "    jobs = svc.list_jobs()\n"
        '    probe = getattr(svc, "raise_if_store_unreadable", None)\n'
        "    if callable(probe):\n"
        "        probe()\n"
        "    svc.remove_jobs([j.id for j in jobs])\n"
    )
    hits = _scan_module_for_read_decide_write(via_getattr, "synthetic.py")
    assert [(h[1], h[3]) for h in hits] == [("wipe", True)], (
        "a probe reached through getattr must count as compliant; " f"got {hits!r}"
    )


def test_every_read_decide_write_caller_probes_or_is_exempt() -> None:
    """THE obligation. A new read-decide-write caller must probe, or say why not."""
    callers = _read_decide_write_callers()
    assert callers, (
        "the scan found no read-decide-write callers at all, which means it is "
        "broken rather than that the codebase is clean -- see the synthetic control"
    )

    offenders = [
        f"{rel}:{lineno} {func}()"
        for rel, func, lineno, probes in callers
        if not probes and (rel, func) not in _EXEMPT_READ_DECIDE_WRITE
    ]
    assert not offenders, (
        "these callers read the cron store, decide from what they read, and then "
        f"write, without calling {_PROBE}():\n  "
        + "\n  ".join(offenders)
        + "\n\nAn unreadable store loads as an EMPTY list without raising, so such a "
        "caller finds nothing to do, attempts no write, and reports success over a "
        f"corrupt store. Either call {_PROBE}() right after the read, or add an "
        "entry to _EXEMPT_READ_DECIDE_WRITE stating why this one structurally cannot."
    )


def test_no_exemption_outlives_the_caller_it_describes() -> None:
    """Exemption hygiene: a stale entry is a blanket nobody reviewed.

    If a caller stops reading-and-writing (or moves), its exemption must go with
    it -- otherwise the list quietly grows into permission for a shape that is no
    longer the one that was argued.
    """
    live = {(rel, func) for rel, func, _lineno, _probes in _read_decide_write_callers()}
    stale = sorted(key for key in _EXEMPT_READ_DECIDE_WRITE if key not in live)
    assert not stale, (
        "these exemptions no longer describe a read-decide-write caller and must be "
        f"deleted: {stale}"
    )
    for key, reason in _EXEMPT_READ_DECIDE_WRITE.items():
        assert (
            len(reason.strip()) >= 40
        ), f"exemption for {key} needs a reason someone can check, not a label"


@pytest.mark.asyncio
async def test_remove_all_tells_a_corrupt_store_from_an_empty_one(tmp_path: Path) -> None:
    """The behavioural half of the obligation, for the caller the scan found.

    With no jobs loaded, ``cron remove all`` attempts no removal, so its
    ``except CronStoreUnreadable`` cannot fire. A corrupt store must not get the
    empty store's "No cron jobs to remove.", and an empty one must not get the refusal.
    """
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / "crons.json").write_bytes(b"{not json at all")
    broken = CronService(base_dir=corrupt)
    assert broken.list_jobs(include_disabled=True) == [], "precondition: reads as empty"
    refusal = await cron_remove_all_reply(broken, source="test", caller="test")

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "crons.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")
    healthy = CronService(base_dir=empty)
    quiet = await cron_remove_all_reply(healthy, source="test", caller="test")

    assert (
        quiet == "No cron jobs to remove."
    ), "an honestly empty store must still get the quiet answer"
    assert refusal != quiet, "a corrupt store must not earn the same sentence as an empty one"
    assert (
        "empty for that reason rather than because the store is empty" in refusal
    ), "the refusal must name the cause, which is the whole point of distinguishing them"
