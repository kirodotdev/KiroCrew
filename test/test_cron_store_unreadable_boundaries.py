"""``CronStoreUnreadable`` must be TRANSLATED at every user-initiated mutation
boundary, not propagate raw.

``CronService._save`` refuses to persist after a failed ``_load`` and RAISES
:exc:`CronStoreUnreadable` so a mutation cannot report success for a write that
never happened. Raising is only half the contract: the boundaries a user
actually drives -- the CLI, the dashboard REST API, the MCP tools, Slack -- have
to turn that into an intelligible failure. Untranslated it surfaces as a CLI
traceback and a generic dashboard 500, which tells the user nothing about the
one thing that would fix it (move the unreadable file aside).

Each positive test drives a REAL ``CronService`` over a genuinely unreadable
``crons.json`` rather than injecting the exception, so it exercises the same
``_load_failed`` path production does.

The vacuous-pass trap this file exists to avoid: a test that adds a cron over a
HEALTHY store passes identically with and without the translation. So every
assertion here is about the shape of the FAILURE, and the two negative controls
at the bottom prove a legitimate write is still silent and successful -- a guard
that blocks a fresh install would be worse than the defect it closes.
"""

from __future__ import annotations

import ast
import contextlib
import errno
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronService, CronStoreBusy, CronStoreUnreadable
from kiro_crew.messaging.commands import cron_remove_all_reply

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
    return TestClient(TestServer(app))


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
# Refusing to write the FILE is only half of a refusal. Both add paths mutate
# `self._jobs` before reaching `_save()`, so a job the caller was told was
# rejected (a non-retryable 409) stayed in the in-memory list. `_tick_scan_locked`
# documents an in-memory-snapshot fallback for a contended lock -- it skips
# `_sync()` and returns `list(self._jobs)` -- so the due-scan could hand back a
# job the API had just refused.
#
# The vacuous-pass trap specific to these tests: asserting only that the mutator
# RAISES passes with and without the fix, because `_save()` already raised. Every
# assertion below is therefore about `self._jobs`, not about the exception.


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
# The message this refusal prints tells the operator to move the unreadable file
# aside. `_sync` returned early on a missing path without reaching `_load`, which
# is the only thing that clears the latch -- so on a long-lived gateway every
# later write kept refusing until a restart, and the printed instruction did
# nothing. A fresh CLI/MCP process was unaffected because it reconstructs.
#
# The vacuous-pass trap here: asserting a mutation RAISES on a corrupt store
# passes with and without this fix, because `_save` already raised. Every
# assertion below is about what happens AFTER the store changes on disk.


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
    """The digest path already self-heals, which is why it was left untouched.

    Both `_load` failure paths reset the fingerprint, so the next `_sync` over a
    file that is still PRESENT cannot match the cleared digest, reloads, and
    re-evaluates the latch. Only the missing-path early return skipped that.
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
# `_load` latches on both of its failure paths, but `_sync` reads the file itself
# first -- to decide whether a reload is even needed -- and its `except OSError`
# returned bare. So a store that becomes unreadable AT THE OS LEVEL after a
# successful load (EIO, EACCES, a botched restore) never reaches `_load` at all:
# `_sync` swallows the read error, `_load_failed` stays clear from the earlier
# good load, and `_save` -- whose guard is the only thing standing between stale
# memory and the file -- writes straight over it.
#
# Injected at the FILESYSTEM boundary rather than with a genuinely unreadable
# file, which is the one place this file departs from its own "real store" rule,
# for a reason: a real EACCES needs a non-root uid and goes vacuous under root,
# and putting a directory at the path makes the RENAME fail too, masking the very
# clobber T1 has to observe. `CronStoreUnreadable` itself is never injected -- the
# real `_sync` -> latch -> `_save` chain runs.


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

    Separate assertion from the latch, and it is the one the fingerprint reset
    carries on its own. The content on disk NEVER changes here, so a latch set
    without clearing the digest leaves the next `_sync` matching its tracked
    fingerprint, skipping the reload that is the only thing that clears the
    latch -- and a perfectly healthy store refuses every write until the process
    restarts. Clearing the digest is what forces that reload.
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


# ── The TEARDOWN contract: an empty owned set is not an authoritative one ──
#
# `_load` degrades an unreadable store to an empty job list, so "which jobs does
# this app own?" answers zero for a reason that has nothing to do with ownership.
# App uninstall reads that zero as authoritative and deletes the app, while the
# app's still-ENABLED jobs sit on disk and resume the moment the store parses
# again -- now owned by an app that no longer exists.
#
# The vacuous-pass trap specific to these tests: asserting `_jobs == []` over an
# unreadable store passes with AND without the fix, because the empty list is
# present in both worlds. The defect is that the empty list is TRUSTED. So every
# assertion below is about the TEARDOWN OUTCOME -- the removal refuses, and the
# boundary propagates that refusal instead of reporting a successful zero.

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
    """The selection itself must refuse -- returning [] is the defect.

    `_remove_jobs_by_owner_locked` selects AFTER an in-lock reload. Over an
    unreadable store that reload yields nothing, so the removal has nothing to
    mutate, never reaches `_save()` (the only raiser on this path) and returns an
    empty list indistinguishable from "this app owned nothing".
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

    `deregister_app_crons_from_service` re-raises `CronStoreBusy` for exactly this
    reason -- its docstring says a contended cleanup must be REPORTED rather than
    "masked as a successful 0 while owned jobs stay enabled". `CronStoreUnreadable`
    is a sibling of `CronStoreBusy`, not a subclass, so without its own arm it
    falls to the generic handler and returns 0.
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
# `hooks_integration`'s disable hook catches `CronStoreBusy` and records a
# "failed" cleanup note, because its own comment requires it to "REPORT it
# (rather than crash the disable or claim a false success)". `CronStoreUnreadable`
# is a sibling class, so it escaped that arm entirely and would crash the disable
# -- the exact outcome the comment forbids.
#
# The vacuous-pass trap here: asserting the hook "does not report success" passes
# under the defect too, because a crash also fails to report success. So the
# assertion is on the REPORTED NOTE, and on the hook returning at all.


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
# Two handlers swallow `CronStoreUnreadable` around a removal that has already
# been applied to the in-memory list. Neither leaves the intent anywhere:
#
#   * the deferred drain CLAIMS `_pending_removals` with an atomic swap BEFORE
#     the save it might lose, so its own comment ("the deferred delete simply
#     stays pending until the store is readable again") is not what the code
#     does -- the set is already empty by then;
#   * the run-result merge removes a `delete_after_run` job and returns without
#     queueing it anywhere at all.
#
# Either way the next healthy `_sync` reloads the job from the file that still
# holds it, and a completed one-shot RUNS AND NOTIFIES AGAIN.
#
# The vacuous-pass trap: asserting the drain returned `[]`, or that the store
# still holds the job, passes with AND without the fix -- nothing was persisted
# in either world. The assertions that carry the coverage are that the intent
# SURVIVES, and that the delete then actually lands once the store is readable.


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
# `_load`'s own missing-file branch empties `_jobs`; `_sync`'s does not, it only
# clears the refusal latch. So a snapshot taken before the store became
# unreadable survives the remediation and is what the next `_save()` writes --
# resurrecting whatever another writer had removed in the meantime.
#
# The vacuous-pass trap: asserting the latch cleared, or that the new job saved,
# passes either way. The assertion that carries the coverage is the ABSENCE of
# the externally-removed job from the file afterwards.


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
# The result merge derives ONE flag from presence (`job.id in by_id`) and used it
# for two questions with opposite needs: "did I actually remove a present job?"
# (the audit, which must stay presence-keyed) and "is a delete OWED?" (the
# deferred queue, which must fire even when the read failed). A corrupt store
# makes `_load` empty `_jobs` WITHOUT raising, so presence is exactly what is
# destroyed -- the queue never fired and the next healthy sync restored the
# one-shot from disk and ran it a SECOND time.
#
# The vacuous-pass trap: asserting the merge logs a warning passes under the
# defect and the fix alike, because the defect path logs too and then silently
# drops the consumption. The assertion carrying the coverage is the HARM -- once
# the store is readable again the one-shot must be GONE, not merely mentioned.


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

    The audit stays keyed on PRESENCE. Nothing was removed from a store that
    could not be read, so filing a removal record here would describe a delete
    that never happened -- the honesty the presence check exists to protect.
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
# `_save`'s refusal is the LAST thing on a user-facing write path: the locked core
# takes the lock, syncs, mutates `self._jobs`, and only then reaches the disk
# boundary that raises. The caller sees a failure while the cached job carries the
# mutation -- and the timer reads that cache, so a "failed" resume can still fire.
#
# Reachability: it needs a read failure that RETAINS `_jobs`. `_load`'s parse
# failures empty the list, so the loop finds nothing and never mutates; only
# `_sync`'s own EIO branch latches with the jobs still in memory. That is the
# shape driven below.
#
# The vacuous-pass trap: asserting the call RAISES passes with and without the
# fix, because the pre-fix path raises too -- just later. The assertion carrying
# the coverage is the CACHED STATE afterwards.


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

    One test cannot cover seven call sites, but acking is the one whose mutation
    is not observable from `enabled` -- so it catches a fix applied only to the
    resume path GPT happened to name.
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
# The requeue arm added above guards the drain's `_save()`. It cannot cover the
# earlier exit: the drain CLAIMS `_pending_removals` with the atomic swap, then
# intersects it with `present` (built from `self._jobs`). A PARSE failure --
# unlike the EIO failure the test above drives -- makes `_load` set
# `self._jobs = []`, so `present` is empty, the intersection is empty, and the
# drain returns at `if not to_remove` having already emptied the queue. The
# requeue arm is never reached.
#
# The docstring line "an id no longer present was already removed elsewhere, so
# dropping it is correct" is what makes this subtle: it is true only when the
# load SUCCEEDED. Under a failed load, absence means the list is unknown, not
# empty, and dropping the intent lets the repaired store re-run a completed
# one-shot and notify a second time.
#
# The vacuous-pass trap: `drain() == []` and "the job is still on disk" both hold
# with and without the fix. The two assertions that carry the coverage are that
# the intent SURVIVES, and that the delete then actually lands once repaired --
# and they need separate break-arms, since a fix that requeues but never retries
# would satisfy the first alone.


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
# `CronService.raise_if_store_unreadable` is a PER-CALLER obligation, not a
# structural guarantee, and that asymmetry is the whole reason these tests
# exist. Every WRITE is already guarded (`_sync_for_write` refuses before the
# mutation), but a READ degrades silently: `_load` empties `_jobs` and latches
# `_load_failed` WITHOUT raising. So a caller that reads, decides from what it
# read, and only then writes can find nothing to do, attempt no write, and
# report success over a corrupt store -- the quiet-versus-broken conflation.
#
# A prose note cannot fail, so it cannot stop that being reintroduced by
# omission. These tests can: they ENUMERATE the read-decide-write callers from
# source and require each one to either probe or carry a written exemption.
# The next `apply_tiers`-shaped caller inherits the rule by going red.
_PROBE = "raise_if_store_unreadable"

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
    ("src/kiro_crew/apps/bridges.py", "register_app_crons_with_service"): (
        "An empty read makes every definition look ABSENT, so the add is still "
        "attempted and refuses at the write. The degraded read biases TOWARD the "
        "write rather than around it, so the refusal cannot be skipped."
    ),
    ("src/kiro_crew/onboarding_import.py", "_write_schedule"): (
        "Same shape: no matching job means it falls through to `add_job`, which "
        "refuses. Absence routes INTO the write, so the store cannot stay silent."
    ),
    ("src/kiro_crew/dashboard/state.py", "delete_cron_folder"): (
        "The authoritative write is `save_cron_folders()` on a DIFFERENT store and "
        "happens BEFORE the read. The job loop that follows is documented "
        "best-effort cleanup whose per-job failure is benign (an unknown folder_id "
        "renders as ungrouped and self-heals), so the outcome this reports does not "
        "depend on the cron read at all."
    ),
    ("src/kiro_crew/cli_commands.py", "_cron_dispatch"): (
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
    ``getattr(svc, "raise_if_store_unreadable", None)``, which the codebase uses
    where the service is duck-typed (``CronService | Any``) and a fake need not
    carry every method. Recognising only the direct call would read a real,
    working guard as absent -- so the enforcement would demand a change that
    breaks the fakes, which is a fact about this scanner, not about the caller.
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


def _read_decide_write_callers() -> list[tuple[str, str, int, bool]]:
    """Scan the shipped package for read-decide-write cron callers.

    ``cron.py`` is excluded because it IS the service: its internals reach the
    store directly and are guarded by `_sync_for_write`, not by the public probe.
    Tests are excluded because a test may model a bad caller deliberately.
    """
    src_root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    callers: list[tuple[str, str, int, bool]] = []
    for path in sorted(src_root.rglob("*.py")):
        # `.as_posix()`, NOT `str(path)`: `str()` renders the OS-NATIVE separator, so
        # on Windows this string is `src\kiro_crew\...`. That broke this scan two ways
        # at once, both Windows-only. The `/tests/` filter below never fired, so the
        # Windows shard silently scanned a DIFFERENT file set than Linux; and the
        # backslash-spelled `rel` matched no key in the forward-slash-keyed
        # `_EXEMPT_READ_DECIDE_WRITE`, which made the enforcement report all five
        # exempt callers as unprobed while the hygiene test simultaneously reported
        # all five exemptions as stale -- two contradictory failures from one lookup
        # miss. POSIX is the portable spelling and the one the table is keyed in, so
        # normalise here rather than branching on the platform.
        text = path.as_posix()
        if path.name == "cron.py" or "__pycache__" in text:
            continue
        if "/tests/" in text or path.name.startswith("test_"):
            continue
        rel = path.relative_to(src_root.parents[1]).as_posix()
        # `encoding="utf-8"` explicitly, NOT the platform default. Without it this
        # read uses cp1252 on Windows, whose codec is named `charmap`, and any
        # source file holding a byte cp1252 leaves undefined raises
        # UnicodeDecodeError -- so these guard tests failed on the Windows shard
        # while Linux and macOS passed. Measured on this tree: 95 of the 1002
        # scanned files raise under cp1252, the first being `acp/client.py`, where
        # the offending 0x90 is the third byte of `←` (U+2190) in a docstring.
        # Strict rather than errors="replace": Python source is UTF-8 by
        # definition (PEP 3120), every scanned file decodes cleanly today, and a
        # file that genuinely did not is a fault worth raising -- `ast.parse`
        # below could not do anything useful with mojibake anyway.
        callers.extend(_scan_module_for_read_decide_write(path.read_text(encoding="utf-8"), rel))
    return callers


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

    ``cron remove all`` read the store, saw an empty list, and answered "No cron
    jobs to remove." -- the SAME sentence an honestly empty store earns. Its
    ``except CronStoreUnreadable`` could not help, because with no jobs it
    attempted no removal to raise from. The pairing is the assertion: a corrupt
    store must say something different, and an empty one must NOT regress into
    saying the refusal.
    """
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / "crons.json").write_bytes(b"{not json at all")
    broken = CronService(base_dir=corrupt)
    assert broken.list_jobs(include_disabled=True) == [], "precondition: reads as empty"
    refusal = await cron_remove_all_reply(broken, source="test", caller="test")

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "crons.json").write_text(json.dumps({"jobs": []}))
    healthy = CronService(base_dir=empty)
    quiet = await cron_remove_all_reply(healthy, source="test", caller="test")

    assert (
        quiet == "No cron jobs to remove."
    ), "an honestly empty store must still get the quiet answer"
    assert refusal != quiet, "a corrupt store must not earn the same sentence as an empty one"
    assert (
        "empty for that reason rather than because the store is empty" in refusal
    ), "the refusal must name the cause, which is the whole point of distinguishing them"
