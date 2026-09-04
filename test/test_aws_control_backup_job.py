"""AWS Control backups as durable Job SDK runs.

What moved: starting a backup used to execute it inside the request and return
its terminal record, so "a backup of mine is running" lived only in the React
component that started it -- a reload or a navigation destroyed the fact while
the work kept going. The route now claims a server-owned run and returns its
id, and the browser follows it on the shared ``_jobs`` surface.

The cases here are the ones that shape the design rather than merely cover it:

* the route claims and returns, and does NOT perform the backup itself;
* the runner is a plain ``def``, because ``JobSDK._execute`` discards its return
  value and an ``async def`` would therefore record ``done`` for a backup that
  never ran;
* the runner reads its account back out of its own record's ``dedupe_key``,
  which is the only channel P1 offers -- so the two keys the GENERIC start route
  can produce that this app's pre-flight would have rejected (absent, and not an
  account id) must fail with a clean recorded error and touch no AWS;
* a run left non-terminal by a process that is gone comes back RESOLVED, never
  ``running`` (``job_sdk.py:768``);
* the app's own terminal ledger -- what a backup PRODUCED -- still works.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps import job_sdk
from kiro_crew.apps.builtins.aws_control import hooks
from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod
from kiro_crew.apps.builtins.aws_control.backend import backup
from kiro_crew.apps.builtins.aws_control.backend import routes as routes_mod

BASE = "/api/apps/aws-control"
ACCOUNT = "111122223333"
PROFILE = "prof"
REGION = "us-west-2"
BUCKET = "kirocrew-drive-abc"


class TestNoAwsCallBeforeTheGate:
    """A refused run must not reach AWS in the course of refusing.

    `find_drive` resolves the drive bucket through the tagging API, so it is a
    real request on the owner's credentials. With consent withdrawn it must never
    happen: the gate needs no bucket, so nothing forces it to wait for discovery.
    """

    def test_discovery_never_runs_when_the_gate_refuses(self):
        order: list[str] = []

        def _gate(account, profile, region, *, caller):
            order.append("gate")
            raise PermissionError("consent withdrawn; upload refused")

        def _find(profile, region, *, account):
            order.append("discovery")
            return BUCKET

        sdk = SimpleNamespace(get=lambda _rid: _FakeRun(ACCOUNT))
        runner = backup.make_job_runner(sdk, backup.KIND_SNAPSHOT)
        with (
            _resolvable(),
            mock.patch.object(backup, "_authorize_upload", _gate),
            mock.patch.object(backup.storage, "find_drive", _find),
            mock.patch.object(backup, "run_snapshot_backup") as work,
        ):
            with pytest.raises(PermissionError):
                runner(SimpleNamespace(run_id="r"))

        # The assertion is about the CALLS, not the refusal. "It raised" is
        # compatible with having already probed AWS, which is the whole finding.
        assert order == ["gate"]
        work.assert_not_called()


class TestEveryUploadRefusalIsAudited:
    """One SEL record per refusal path in ``_authorize_upload``.

    Moving the upload off the request path moved the authorization decision off
    the audited path with it: the route's audit has already recorded
    ``successful`` when the request returns a run id, and the Job SDK only
    records that the run ``failed``. An auditor scanning SEL events for denials
    saw nothing at all.

    Covered per path rather than once, because partial coverage is worse than
    none -- an audited consent refusal beside a silent identity mismatch makes
    the mismatch look like a non-event.
    """

    @staticmethod
    def _arrange(stack: ExitStack, **over: Any) -> mock.MagicMock:
        """Put every check in a PASSING state, so each case fails exactly one."""
        stack.enter_context(
            mock.patch(
                "kiro_crew.deploy.engine._checked",
                return_value=json.dumps({"Account": over.get("live", ACCOUNT)}),
            )
        )
        stack.enter_context(
            mock.patch(
                "kiro_crew.apps.manager.is_app_enabled", return_value=over.get("enabled", True)
            )
        )
        granted = stack.enter_context(mock.patch("kiro_crew.aws_consent.is_granted"))
        granted.return_value = over.get("granted", (True, ""))
        grant = stack.enter_context(mock.patch("kiro_crew.aws_consent.read_grant"))
        grant.return_value = over.get("grant", SimpleNamespace(account=ACCOUNT))
        stack.enter_context(
            mock.patch.object(backup._STOP, "is_set", return_value=over.get("stopping", False))
        )
        return stack.enter_context(mock.patch.object(backup, "sel"))

    @pytest.mark.parametrize(
        "label,over,expect",
        [
            ("live account mismatch", {"live": "999988887777"}, "no longer points at"),
            ("app disabled", {"enabled": False}, "was disabled during"),
            ("consent not granted", {"granted": (False, "revoked_by_owner")}, "no longer holds"),
            ("grant missing", {"grant": None}, "was withdrawn during"),
            (
                "grant names another account",
                {"grant": SimpleNamespace(account="999988887777")},
                "does not name this account",
            ),
        ],
    )
    def test_each_access_decision_is_recorded_as_denied(
        self, label: str, over: dict[str, Any], expect: str
    ) -> None:
        with ExitStack() as stack:
            sel_factory = self._arrange(stack, **over)
            with pytest.raises(RuntimeError, match="upload refused"):
                backup._authorize_upload(ACCOUNT, PROFILE, REGION, caller=backup.CALLER_OWNER)

            sel_factory.return_value.log_api_access.assert_called_once()
            kwargs = sel_factory.return_value.log_api_access.call_args.kwargs
            assert kwargs["outcome"] == "denied", label
            assert kwargs["operation"] == "aws_control.backup_upload"
            assert kwargs["source"] == "aws-control"
            # The reason must reach the record: the value of a denial event is
            # WHICH check refused, not merely that something was refused.
            assert expect in kwargs["error"], label
            # An audit record that cannot say which account was refused is not
            # evidence. The SDK withholds the dedupe key from its own log and its
            # HTTP view, so this audit names the account deliberately.
            assert ACCOUNT in kwargs["resources"], label

    @pytest.mark.parametrize(
        "caller,expected",
        [("interactive", "dashboard-owner"), ("scheduled", "app:aws-control")],
    )
    def test_a_refusal_is_attributed_to_whoever_triggered_it(
        self, caller: str, expected: str
    ) -> None:
        """Attribution has to DISTINGUISH, so both paths are asserted.

        Covering the nightly path is what made a hardcoded interactive caller a
        lie: an unattended run refused at 03:00 was recorded against the dashboard
        owner, a person who was not there. That is the same defect as everywhere
        else in this change -- a record asserting something nobody observed --
        committed at the audit layer instead of the record layer.

        A neutral string for both paths would fix the lie by discarding the truth
        on the interactive path, where the owner really did trigger the work. So
        each entry point states its own, and a test that checked only one value
        would not be testing attribution at all.
        """
        chosen = backup.CALLER_OWNER if caller == "interactive" else backup.CALLER_SCHEDULED
        with ExitStack() as stack:
            sel_factory = self._arrange(stack, granted=(False, "revoked_by_owner"))
            with pytest.raises(RuntimeError, match="upload refused"):
                backup._authorize_upload(ACCOUNT, PROFILE, REGION, caller=chosen)

            kwargs = sel_factory.return_value.log_api_access.call_args.kwargs
            assert kwargs["caller"] == expected

    def test_the_two_entry_points_pass_different_callers(self) -> None:
        """The constants only matter if the call sites actually differ.

        Pinned by reading the sources: the Job SDK runner exists because an owner
        asked through an owner-gated route, and the nightly loop has no owner at
        all. If both ever named the same constant, the field would be decoration.
        """
        assert backup.CALLER_OWNER != backup.CALLER_SCHEDULED
        assert "CALLER_OWNER" in inspect.getsource(backup.make_job_runner)
        assert "CALLER_SCHEDULED" in inspect.getsource(hooks._run_once)

    def test_teardown_is_recorded_but_not_as_an_access_denial(self) -> None:
        """Every refusal leaves a record; only access decisions are denials.

        A routine restart filed as ``denied`` would sit in the log beside a
        withdrawn consent and devalue every real denial, so teardown records
        ``failed`` -- still from the vocabulary ``sel.py`` documents for this
        field, still not silent, and still raising so the run ends ``failed``.
        """
        with ExitStack() as stack:
            sel_factory = self._arrange(stack, stopping=True)
            with pytest.raises(RuntimeError, match="shutting down"):
                backup._authorize_upload(ACCOUNT, PROFILE, REGION, caller=backup.CALLER_OWNER)

            sel_factory.return_value.log_api_access.assert_called_once()
            kwargs = sel_factory.return_value.log_api_access.call_args.kwargs
            assert kwargs["outcome"] == "failed"
            assert kwargs["outcome"] != "denied"
            assert "shutting down" in kwargs["error"]

    def test_no_refusal_path_bypasses_the_audited_helper(self) -> None:
        """Ratchet: the audit sits on the single refusal return.

        The cases above prove today's paths are covered. This keeps a path added
        LATER from being silent, which is the failure mode that made the original
        defect invisible: a new bare ``raise`` here would emit no SEL record and
        no existing test would notice.
        """
        source = inspect.getsource(backup._authorize_upload)
        bare = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith("raise ") and "_refuse_upload" not in line
        ]
        assert bare == [], (
            "every refusal in _authorize_upload must go through _refuse_upload so it is "
            f"audited; these bypass it: {bare}"
        )
        # The helper must still be what raises, or the check above would pass
        # happily against a function that refused nothing.
        assert "raise RuntimeError(reason)" in inspect.getsource(backup._refuse_upload)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _registered() -> dict[tuple[str, str], object]:
    app = web.Application()
    routes_mod.register_routes(app)
    return {
        (route.method, str(route.resource.canonical)[len(BASE) :]): route.handler
        for route in app.router.routes()
        if str(route.resource.canonical).startswith(BASE) and route.method != "HEAD"
    }


class TestAccountScopedJobState:
    """`_account_jobs` answers for ONE account, which is the point of it.

    The generic ``_jobs/active`` surface is app-scoped and withholds
    ``dedupe_key``, so a browser reading it cannot tell whose run it is seeing.
    With two connected accounts that made account A's backup disable account B's
    button -- a false busy that blocks a legitimate action, in a PR whose whole
    thesis is that a running backup is a fact rather than a UI guess.

    The Sage audit predicted this when it established ``dedupe_key`` as write-only
    across the HTTP surface. These are that prediction arriving as a bug.
    """

    OTHER = "999988887777"
    #: Ids as the SDK mints them -- 32 hex, carrying nothing about the caller. The
    #: fixture must not embed the account either, or the leak assertion below
    #: would fail on the fixture's own naming rather than on a real leak.
    MINE_RID = "a" * 32
    THEIRS_RID = "b" * 32

    @classmethod
    def _run(
        cls, kind: str, account: str, status: str = "running", error: str = ""
    ) -> SimpleNamespace:
        return SimpleNamespace(
            run_id=cls.MINE_RID if account == ACCOUNT else cls.THEIRS_RID,
            kind=kind,
            status=status,
            dedupe_key=account,
            created_at="2026-09-01T00:00:00Z",
            updated_at="2026-09-01T00:00:01Z",
            finished_at="" if status == "running" else "2026-09-01T00:00:02Z",
            error=error,
            is_terminal=status != "running",
        )

    def _sdk(self, active: list, recent: list | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            list_active=lambda kind="": [r for r in active if not kind or r.kind == kind],
            list_recent=lambda kind="", limit=20: [
                r for r in (recent or []) if not kind or r.kind == kind
            ],
        )

    def test_another_accounts_run_does_not_make_this_account_busy(self) -> None:
        sdk = self._sdk([self._run("snapshot", self.OTHER)])
        with mock.patch.object(routes_mod, "get_job_sdk", return_value=sdk):
            mine = routes_mod._account_jobs(ACCOUNT)
            theirs = routes_mod._account_jobs(self.OTHER)
        assert mine["snapshot"]["active"] is None, "account A's run must not busy account B"
        assert theirs["snapshot"]["active"] is not None
        assert theirs["snapshot"]["active"]["run_id"] == self.THEIRS_RID

    def test_this_accounts_run_is_reported_per_kind(self) -> None:
        sdk = self._sdk([self._run("snapshot", ACCOUNT)])
        with mock.patch.object(routes_mod, "get_job_sdk", return_value=sdk):
            got = routes_mod._account_jobs(ACCOUNT)
        assert got["snapshot"]["active"]["status"] == "running"
        # The sibling kind stays usable: a snapshot in flight says nothing about
        # a sessions backup, which is what `(kind, dedupe_key)` indexing means.
        assert got["sessions"]["active"] is None

    def test_the_account_never_reaches_the_client(self) -> None:
        """``dedupe_key`` is the account and is caller-supplied; it stays server-side."""
        sdk = self._sdk([self._run("snapshot", ACCOUNT)])
        with mock.patch.object(routes_mod, "get_job_sdk", return_value=sdk):
            got = routes_mod._account_jobs(ACCOUNT)
        assert "dedupe_key" not in got["snapshot"]["active"]
        assert ACCOUNT not in json.dumps(got)

    def test_a_failed_run_is_reported_so_the_row_can_say_so(self) -> None:
        """The app's ledger records only successes, so failure must come from here."""
        sdk = self._sdk(
            [],
            [self._run("snapshot", ACCOUNT, status="failed", error="S3 consent no longer holds")],
        )
        with mock.patch.object(routes_mod, "get_job_sdk", return_value=sdk):
            got = routes_mod._account_jobs(ACCOUNT)
        assert got["snapshot"]["lastFailed"]["error"] == "S3 consent no longer holds"
        # Another account's failure is equally not ours.
        sdk2 = self._sdk([], [self._run("snapshot", self.OTHER, status="failed", error="nope")])
        with mock.patch.object(routes_mod, "get_job_sdk", return_value=sdk2):
            assert routes_mod._account_jobs(ACCOUNT)["snapshot"]["lastFailed"] is None

    def test_a_successful_run_is_not_reported_as_a_failure(self) -> None:
        sdk = self._sdk([], [self._run("snapshot", ACCOUNT, status="done")])
        with mock.patch.object(routes_mod, "get_job_sdk", return_value=sdk):
            assert routes_mod._account_jobs(ACCOUNT)["snapshot"]["lastFailed"] is None

    def test_a_success_after_a_failure_clears_the_failure(self) -> None:
        """The row must not contradict itself after a retry.

        Reporting the first non-``done`` run would skip past a NEWER success, so a
        fail-then-retry rendered "last run failed" directly above the fresh
        success the app's own ledger had just recorded -- and it persisted until
        the failure aged out of the window, which for a nightly user is weeks.
        Only the newest terminal run may speak, and only when it did not succeed.
        """
        sdk = self._sdk(
            [],
            # list_recent is newest-first: the success is the newer of the two.
            [
                self._run("snapshot", ACCOUNT, status="done"),
                self._run("snapshot", ACCOUNT, status="failed", error="transient"),
            ],
        )
        with mock.patch.object(routes_mod, "get_job_sdk", return_value=sdk):
            assert routes_mod._account_jobs(ACCOUNT)["snapshot"]["lastFailed"] is None

    def test_a_failure_after_a_success_is_still_reported(self) -> None:
        """The converse, so the fix above cannot be "never report anything"."""
        sdk = self._sdk(
            [],
            [
                self._run("snapshot", ACCOUNT, status="failed", error="S3 consent"),
                self._run("snapshot", ACCOUNT, status="done"),
            ],
        )
        with mock.patch.object(routes_mod, "get_job_sdk", return_value=sdk):
            got = routes_mod._account_jobs(ACCOUNT)["snapshot"]["lastFailed"]
        assert got is not None and "S3 consent" in got["error"]

    def test_the_error_is_clamped_for_the_caption_that_renders_it(self) -> None:
        """The SDK stores up to 2000 chars; the row shows it in a 12px caption.

        An expired-credential botocore message would blow the line out, so the
        view clamps. The full text stays on the run record.
        """
        long = "x" * 2000
        sdk = self._sdk([], [self._run("snapshot", ACCOUNT, status="failed", error=long)])
        with mock.patch.object(routes_mod, "get_job_sdk", return_value=sdk):
            got = routes_mod._account_jobs(ACCOUNT)["snapshot"]["lastFailed"]
        assert got is not None
        assert len(got["error"]) <= 180
        # Marked, not silently cut: a sentence truncated with no sign of it
        # reads as a complete thought that happens to be ungrammatical.
        assert got["error"].endswith("...")
        # A short error is left exactly as it is -- no gratuitous marker.
        short = self._sdk([], [self._run("snapshot", ACCOUNT, status="failed", error="nope")])
        with mock.patch.object(routes_mod, "get_job_sdk", return_value=short):
            assert routes_mod._account_jobs(ACCOUNT)["snapshot"]["lastFailed"]["error"] == "nope"

    def test_no_runtime_means_no_job_block_rather_than_an_error(self) -> None:
        with mock.patch.object(routes_mod, "get_job_sdk", return_value=None):
            assert routes_mod._account_jobs(ACCOUNT) == {}


def _request(method: str, path: str, *, match_info: dict | None = None) -> web.Request:
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id="owner-1")
    kwargs: dict = {"app": app}
    if match_info is not None:
        kwargs["match_info"] = match_info
    req = make_mocked_request(method, f"{BASE}{path}", **kwargs)
    req["app"] = ""
    req["user"] = "owner-1"
    return req


def _payload(response: web.StreamResponse) -> dict:
    raw = response.body  # type: ignore[attr-defined]
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


def _enabled_owner_env():
    """App on, account resolvable, live probe agreeing with the requested account."""
    return (
        mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
        mock.patch.object(
            routes_mod.accounts_mod,
            "resolve_account_profile",
            AsyncMock(return_value=(PROFILE, REGION)),
        ),
        mock.patch.object(
            routes_mod.aws_consent,
            "probe_identity",
            AsyncMock(return_value=SimpleNamespace(ok=True, account=ACCOUNT, arn="", detail="")),
        ),
        mock.patch.object(routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)),
        mock.patch.object(routes_mod.storage_mod, "find_drive", return_value=BUCKET),
    )


def _enter_all(stack: ExitStack, patches) -> None:
    for patch in patches:
        stack.enter_context(patch)


def _post_run(kind: str, *, sdk: object | None = None, start=None):
    """Drive ``POST /backup/{account}/run`` with the guards satisfied."""
    handlers = _registered()
    req = _request("POST", f"/backup/{ACCOUNT}/run", match_info={"account": ACCOUNT})
    req.json = AsyncMock(return_value={"kind": kind})  # type: ignore[method-assign]
    fake = sdk
    if fake is None:
        fake = SimpleNamespace(start_async=start or AsyncMock(return_value="a" * 32))
    with ExitStack() as stack:
        _enter_all(stack, _enabled_owner_env())
        stack.enter_context(mock.patch.object(routes_mod, "get_job_sdk", return_value=fake))
        return asyncio.run(
            handlers[("POST", "/backup/{account}/run")](req)  # type: ignore[operator]
        )


def _await_terminal(sdk: job_sdk.JobSDK, run_id: str, timeout: float = 5.0) -> job_sdk.JobRun:
    """Block until the worker has written its terminal record."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = sdk.get(run_id)
        if run is not None and run.is_terminal:
            return run
        time.sleep(0.01)
    run = sdk.get(run_id)
    raise AssertionError(f"run did not settle within {timeout}s: {run}")


@pytest.fixture
def sdk(tmp_path: Path):
    """A real JobSDK on its own store, with both backup kinds registered."""
    made = job_sdk.JobSDK("aws-control", tmp_path)
    for kind in backup.JOB_KINDS:
        made.register(kind, backup.make_job_runner(made, kind))
    yield made
    asyncio.run(made.remove_all_async())


@pytest.fixture(autouse=True)
def _isolated_backup_state(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
    backup.clear_stop()
    yield
    backup.clear_stop()


def _resolvable():
    return mock.patch.object(
        accounts_mod, "resolve_account_profile_cached", return_value=(PROFILE, REGION)
    )


def _authorized():
    """Let the runner's pre-discovery gate pass.

    The runner authorizes BEFORE it discovers the drive, so any test that expects
    it to reach AWS has to satisfy the gate. Stubbed rather than arranged from
    consent state because these cases are about WHAT the runner targets, not about
    the gate's own decisions -- those are pinned in TestEveryUploadRefusalIsAudited
    and TestNoAwsCallBeforeTheGate, which drive the real function.
    """
    return mock.patch.object(backup, "_authorize_upload", return_value=None)


# ---------------------------------------------------------------------------
# DoD 1 — the route claims a run and returns its id
# ---------------------------------------------------------------------------


class TestRouteStartsAJob:
    def test_run_returns_a_run_id_instead_of_a_terminal_record(self):
        # The whole point: the response is a HANDLE to work in flight, not the
        # outcome of work already done. A client that gets an id can re-find the
        # run after a reload; a client that gets only a record cannot.
        resp = _post_run(backup.KIND_SNAPSHOT, start=AsyncMock(return_value="b" * 32))
        assert resp.status == 200
        body = _payload(resp)
        assert body["started"] is True
        assert body["runId"] == "b" * 32
        assert body["kind"] == backup.KIND_SNAPSHOT

    def test_run_does_not_perform_the_backup_in_the_request(self):
        # If the route still did the work, this mock would be called. The point
        # of the migration is that the request returns while the worker runs.
        with (
            mock.patch.object(backup, "run_snapshot_backup") as work,
            mock.patch.object(backup, "run_sessions_backup") as work2,
        ):
            resp = _post_run(backup.KIND_SNAPSHOT)
        assert resp.status == 200
        work.assert_not_called()
        work2.assert_not_called()

    def test_run_uses_the_account_as_the_dedupe_key(self):
        # Two starts of one kind for one account must not both do the paid
        # upload -- the second adopts the first. The SDK indexes on
        # (kind, dedupe_key), so snapshot and sessions stay independent.
        start = AsyncMock(return_value="c" * 32)
        _post_run(backup.KIND_SESSIONS, start=start)
        start.assert_awaited_once_with(backup.KIND_SESSIONS, dedupe_key=ACCOUNT)

    def test_run_rejects_an_unknown_kind_before_claiming(self):
        start = AsyncMock()
        resp = _post_run("memory", start=start)
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_kind"
        start.assert_not_awaited()

    def test_run_answers_503_when_no_job_runtime_is_published(self):
        # Enabled but no SDK: the `jobs` grant is missing or the context build
        # failed. Not the owner's fault and not a bad request.
        handlers = _registered()
        req = _request("POST", f"/backup/{ACCOUNT}/run", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"kind": backup.KIND_SNAPSHOT})  # type: ignore[method-assign]
        with ExitStack() as stack:
            _enter_all(stack, _enabled_owner_env())
            stack.enter_context(mock.patch.object(routes_mod, "get_job_sdk", return_value=None))
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/run")](req)  # type: ignore[operator]
            )
        assert resp.status == 503
        assert _payload(resp)["code"] == "jobs_unavailable"

    def test_run_answers_503_when_the_kind_has_no_registered_runner(self):
        # A valid kind that nothing services must never queue a run nothing will
        # ever finish; startup registration did not happen.
        start = AsyncMock(side_effect=job_sdk.UnknownJobKind("no runner for 'snapshot'"))
        resp = _post_run(backup.KIND_SNAPSHOT, start=start)
        assert resp.status == 503
        assert _payload(resp)["code"] == "jobs_unavailable"

    def test_run_reports_a_refused_claim_as_503(self):
        start = AsyncMock(side_effect=job_sdk.JobError("could not persist the initial record"))
        resp = _post_run(backup.KIND_SNAPSHOT, start=start)
        assert resp.status == 503
        assert _payload(resp)["code"] == "backup_start_failed"


# ---------------------------------------------------------------------------
# The runner: shape, resolution, and the generic-surface keys
# ---------------------------------------------------------------------------


class TestRunnerShape:
    def test_the_runner_is_not_a_coroutine_function(self):
        # Load-bearing, not stylistic. `_execute` calls the runner and DISCARDS
        # the return value, and `register()` validates the kind but not the
        # callable -- so an `async def` here would hand back a coroutine nobody
        # awaits: the body would never run, nothing would raise, and the record
        # would settle on `done` for a backup that never happened.
        runner = backup.make_job_runner(
            SimpleNamespace(get=lambda _rid: None), backup.KIND_SNAPSHOT
        )
        assert not asyncio.iscoroutinefunction(runner)

    def test_make_job_runner_refuses_a_kind_it_does_not_own(self):
        with pytest.raises(ValueError, match="unknown backup job kind"):
            backup.make_job_runner(SimpleNamespace(), "memory")

    def test_the_work_function_is_resolved_at_call_time(self):
        # The factory must not capture the function object: patching the module
        # attribute is how every existing test drives this path, and a captured
        # reference would silently keep running the real backup.
        sdk = SimpleNamespace(get=lambda _rid: backup and _FakeRun(ACCOUNT))
        runner = backup.make_job_runner(sdk, backup.KIND_SNAPSHOT)
        with (
            _resolvable(),
            _authorized(),
            mock.patch.object(backup.storage, "find_drive", return_value=BUCKET),
            mock.patch.object(backup, "run_snapshot_backup") as work,
        ):
            runner(SimpleNamespace(run_id="r"))
        work.assert_called_once_with(ACCOUNT, PROFILE, REGION, BUCKET, caller=backup.CALLER_OWNER)


class _FakeRun:
    def __init__(self, dedupe_key: str) -> None:
        self.dedupe_key = dedupe_key


class TestRunnerResolvesItsTarget:
    def test_runner_backs_up_the_account_named_by_its_dedupe_key(self, sdk):
        with (
            _resolvable(),
            _authorized(),
            mock.patch.object(backup.storage, "find_drive", return_value=BUCKET),
            mock.patch.object(backup, "run_snapshot_backup") as work,
        ):
            run_id = sdk.start(backup.KIND_SNAPSHOT, dedupe_key=ACCOUNT)
            run = _await_terminal(sdk, run_id)
        assert run.status == job_sdk.DONE
        assert run.error == ""
        work.assert_called_once_with(ACCOUNT, PROFILE, REGION, BUCKET, caller=backup.CALLER_OWNER)

    def test_runner_rediscovers_the_drive_rather_than_trusting_a_carried_name(self, sdk):
        # The app's own rule for the nightly loop: the drive is tag-discovered
        # per run. The bucket is never carried on the record, so a drive that
        # moved between the click and the upload cannot be written to.
        with (
            _resolvable(),
            _authorized(),
            mock.patch.object(backup.storage, "find_drive", return_value="moved-bucket") as find,
            mock.patch.object(backup, "run_snapshot_backup") as work,
        ):
            run_id = sdk.start(backup.KIND_SNAPSHOT, dedupe_key=ACCOUNT)
            _await_terminal(sdk, run_id)
        find.assert_called_once_with(PROFILE, REGION, account=ACCOUNT)
        assert work.call_args.args[3] == "moved-bucket"

    def test_a_run_with_no_account_fails_and_touches_no_aws(self, sdk):
        # Reachable from `POST /_jobs/snapshot/start` with no body: the generic
        # surface defaults dedupe_key to "". It must record a clean failure, not
        # act on some account nobody named.
        with (
            mock.patch.object(backup.storage, "find_drive") as find,
            mock.patch.object(backup, "run_snapshot_backup") as work,
        ):
            run_id = sdk.start(backup.KIND_SNAPSHOT, dedupe_key="")
            run = _await_terminal(sdk, run_id)
        assert run.status == job_sdk.FAILED
        assert "names no account" in run.error
        find.assert_not_called()
        work.assert_not_called()

    def test_a_run_whose_key_is_not_an_account_fails_and_touches_no_aws(self, sdk):
        # The other key the generic surface can carry: a well-formed string that
        # is not an account id. Refused on shape, before any resolution.
        with (
            mock.patch.object(accounts_mod, "resolve_account_profile_cached") as resolve,
            mock.patch.object(backup.storage, "find_drive") as find,
            mock.patch.object(backup, "run_snapshot_backup") as work,
        ):
            run_id = sdk.start(backup.KIND_SNAPSHOT, dedupe_key="not-an-account")
            run = _await_terminal(sdk, run_id)
        assert run.status == job_sdk.FAILED
        assert "does not name an account id" in run.error
        resolve.assert_not_called()
        find.assert_not_called()
        work.assert_not_called()

    def test_a_failed_run_does_not_echo_its_dedupe_key(self, sdk):
        # The key is caller-supplied, and the SDK withholds it from both the log
        # and the HTTP view for that reason. An error that quotes it back would
        # undo that at the one point the record IS served.
        secret = "AKIAIOSFODNN7EXAMPLE"
        with mock.patch.object(backup, "run_snapshot_backup") as work:
            run_id = sdk.start(backup.KIND_SNAPSHOT, dedupe_key=secret)
            run = _await_terminal(sdk, run_id)
        assert run.status == job_sdk.FAILED
        assert secret not in run.error
        work.assert_not_called()

    def test_an_unreachable_account_fails_without_falling_back(self, sdk):
        # No healthy profile for this account. There must be no silent fallback
        # to another account's credentials -- the run fails and says to reconnect.
        with (
            mock.patch.object(accounts_mod, "resolve_account_profile_cached", return_value=None),
            mock.patch.object(backup.storage, "find_drive") as find,
            mock.patch.object(backup, "run_snapshot_backup") as work,
        ):
            run_id = sdk.start(backup.KIND_SNAPSHOT, dedupe_key=ACCOUNT)
            run = _await_terminal(sdk, run_id)
        assert run.status == job_sdk.FAILED
        assert "no working connection" in run.error
        find.assert_not_called()
        work.assert_not_called()

    def test_an_account_with_no_drive_fails_before_any_upload(self, sdk):
        with (
            _resolvable(),
            _authorized(),
            mock.patch.object(backup.storage, "find_drive", return_value=None),
            mock.patch.object(backup, "run_snapshot_backup") as work,
        ):
            run_id = sdk.start(backup.KIND_SNAPSHOT, dedupe_key=ACCOUNT)
            run = _await_terminal(sdk, run_id)
        assert run.status == job_sdk.FAILED
        assert "no drive yet" in run.error
        work.assert_not_called()

    def test_the_sessions_kind_runs_the_sessions_backup(self, sdk):
        with (
            _resolvable(),
            _authorized(),
            mock.patch.object(backup.storage, "find_drive", return_value=BUCKET),
            mock.patch.object(backup, "run_sessions_backup") as work,
            mock.patch.object(backup, "run_snapshot_backup") as other,
        ):
            run_id = sdk.start(backup.KIND_SESSIONS, dedupe_key=ACCOUNT)
            run = _await_terminal(sdk, run_id)
        assert run.status == job_sdk.DONE
        work.assert_called_once_with(ACCOUNT, PROFILE, REGION, BUCKET, caller=backup.CALLER_OWNER)
        other.assert_not_called()


class TestRunnerAuthorization:
    def test_a_run_started_off_the_generic_surface_is_still_consent_gated(self, sdk):
        # The security argument for the generic `_jobs/{kind}/start` entrance,
        # which does NOT run this app's HTTP pre-flight: the gate is
        # `_authorize_upload`, inside the worker. It runs BEFORE drive discovery
        # and again before put_file, so withdrawn consent stops the run before it
        # reaches AWS at all. Here S3 consent is withdrawn, so the run fails and
        # nothing is uploaded even though no route guard was involved.
        #
        # Deliberately NOT using `_authorized()`: this case drives the real gate.
        def fake_snapshot(argv):
            (Path(argv[0]) / "kirocrew-snapshot-20260101T000000Z.tar.gz").write_bytes(b"x")
            return 0

        with (
            _resolvable(),
            mock.patch.object(backup.storage, "find_drive", return_value=BUCKET),
            mock.patch.object(backup, "snapshot_main", side_effect=fake_snapshot),
            mock.patch.object(backup, "_redact_for_upload", create=True, side_effect=lambda p: p),
            mock.patch(
                "kiro_crew.deploy.engine._checked",
                return_value=json.dumps({"Account": ACCOUNT}),
            ),
            mock.patch("kiro_crew.apps.manager.is_app_enabled", return_value=True),
            mock.patch("kiro_crew.aws_consent.is_granted", return_value=(False, "not confirmed")),
            mock.patch.object(backup.storage, "put_file") as put_file,
        ):
            run_id = sdk.start(backup.KIND_SNAPSHOT, dedupe_key=ACCOUNT)
            run = _await_terminal(sdk, run_id)
        assert run.status == job_sdk.FAILED
        assert "consent" in run.error
        put_file.assert_not_called()


# ---------------------------------------------------------------------------
# DoD 3 — a record left by a dead process comes back resolved
# ---------------------------------------------------------------------------


class TestReconcile:
    def _write_orphan(self, data_dir: Path, *, status: str = job_sdk.RUNNING) -> str:
        """A record from a process that no longer exists (a FOREIGN origin)."""
        run_id = "d" * 32
        runs = data_dir / "jobs"
        runs.mkdir(parents=True, exist_ok=True)
        (runs / f"{run_id}.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "app": "aws-control",
                    "kind": backup.KIND_SNAPSHOT,
                    "status": status,
                    "origin": "a-gateway-that-is-gone",
                    "pid": 999999,
                    "dedupe_key": ACCOUNT,
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        return run_id

    def test_a_run_left_running_by_a_dead_process_is_resolved(self, tmp_path):
        # DoD 3. Before reconciliation the record asserts `running` and nothing
        # can ever finish it, so `list_active` would serve a phantom the backup
        # UI adopts on mount. `job_sdk.py:768` resolves it to `interrupted`.
        sdk = job_sdk.JobSDK("aws-control", tmp_path)
        for kind in backup.JOB_KINDS:
            sdk.register(kind, backup.make_job_runner(sdk, kind))
        run_id = self._write_orphan(tmp_path)
        assert [r.run_id for r in sdk.list_active()] == [run_id]

        flipped = sdk.reconcile()

        assert flipped == 1
        settled = sdk.get(run_id)
        assert settled is not None
        assert settled.status == job_sdk.INTERRUPTED
        assert settled.finished_at
        assert sdk.list_active() == []

    def test_on_startup_resolves_an_orphan_before_the_ui_can_adopt_it(self, tmp_path):
        # The gateway's own `reconcile_all()` runs once after the WHOLE enable
        # loop, so between this app's startup and that pass `_jobs/active` can
        # serve a dead process's run. Reconciling in the hook shortens that
        # window to this app's own startup.
        made = job_sdk.JobSDK("aws-control", tmp_path)
        run_id = self._write_orphan(tmp_path)
        ctx = SimpleNamespace(job=made)

        async def _drive() -> None:
            hooks._task = None
            with (
                mock.patch.object(hooks, "_loop", _never),
                mock.patch.object(hooks.backup_mod, "clear_stop"),
            ):
                await hooks.on_startup(ctx)
                await hooks.on_shutdown(ctx)

        with mock.patch.object(hooks.backup_mod, "signal_stop"):
            asyncio.run(_drive())
        hooks._task = None

        settled = made.get(run_id)
        assert settled is not None and settled.status == job_sdk.INTERRUPTED
        assert made.list_active() == []

    def test_reconcile_is_idempotent_so_the_later_gateway_pass_is_harmless(self, tmp_path):
        sdk = job_sdk.JobSDK("aws-control", tmp_path)
        for kind in backup.JOB_KINDS:
            sdk.register(kind, backup.make_job_runner(sdk, kind))
        self._write_orphan(tmp_path)
        assert sdk.reconcile() == 1
        # A terminal record is skipped, so the gateway's pass finds nothing left.
        assert sdk.reconcile() == 0


async def _never() -> None:
    await asyncio.Event().wait()


# ---------------------------------------------------------------------------
# Startup registration, and the manifest grant that makes it reachable
# ---------------------------------------------------------------------------


class TestStartupRegistration:
    def test_on_startup_registers_both_backup_kinds(self, tmp_path):
        made = job_sdk.JobSDK("aws-control", tmp_path)
        ctx = SimpleNamespace(job=made)

        async def _drive() -> None:
            hooks._task = None
            with (
                mock.patch.object(hooks, "_loop", _never),
                mock.patch.object(hooks.backup_mod, "clear_stop"),
            ):
                await hooks.on_startup(ctx)
                await hooks.on_shutdown(ctx)

        with mock.patch.object(hooks.backup_mod, "signal_stop"):
            asyncio.run(_drive())
        hooks._task = None
        assert made.kinds() == sorted(backup.JOB_KINDS)

    def test_registration_happens_even_when_the_nightly_task_is_already_live(self, tmp_path):
        # A re-enable builds a FRESH AppContext, so a fresh JobSDK with an empty
        # runner table. If registration sat behind the "task already running"
        # guard, the app's kinds would have no runners after a re-enable.
        made = job_sdk.JobSDK("aws-control", tmp_path)
        ctx = SimpleNamespace(job=made)

        async def _drive() -> None:
            with (
                mock.patch.object(hooks, "_loop", _never),
                mock.patch.object(hooks.backup_mod, "clear_stop"),
            ):
                hooks._task = asyncio.get_running_loop().create_task(_never())
                await hooks.on_startup(ctx)
                hooks._task.cancel()
                hooks._task = None

        asyncio.run(_drive())
        assert made.kinds() == sorted(backup.JOB_KINDS)

    def test_on_startup_survives_a_context_with_no_job_runtime(self):
        # The grant can be absent on an older installed manifest. The app reports
        # the gap; it does not crash the enable.
        async def _drive() -> None:
            hooks._task = None
            with (
                mock.patch.object(hooks, "_loop", _never),
                mock.patch.object(hooks.backup_mod, "clear_stop"),
            ):
                await hooks.on_startup(SimpleNamespace(job=None))
                assert hooks._task is not None
                await hooks.on_shutdown(None)

        with mock.patch.object(hooks.backup_mod, "signal_stop"):
            asyncio.run(_drive())
        hooks._task = None

    def test_the_manifest_declares_the_jobs_grant(self):
        # Without it `context.build_app_context` builds no SDK and the shared
        # `_jobs` surface answers 404 -- the backup UI would have nothing to poll.
        manifest = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "src/kiro_crew/apps/builtins/aws_control/app.json"
            ).read_text(encoding="utf-8")
        )
        assert manifest["permissions"]["jobs"] is True


# ---------------------------------------------------------------------------
# DoD 4 — the terminal ledger, which is the app's own record of WHAT was produced
# ---------------------------------------------------------------------------


class TestLedgerSurvives:
    def test_a_successful_run_still_writes_the_app_ledger(self, sdk):
        # The Job SDK records only that a run existed and how it ended. What the
        # backup PRODUCED -- key, size, when -- stays in the app's own state, and
        # `GET /backup/{account}` still serves it as `runs`.
        def fake_snapshot(argv):
            (Path(argv[0]) / "kirocrew-snapshot-20260101T000000Z.tar.gz").write_bytes(b"x" * 11)
            return 0

        with (
            _resolvable(),
            _authorized(),
            mock.patch.object(backup.storage, "find_drive", return_value=BUCKET),
            mock.patch.object(backup, "snapshot_main", side_effect=fake_snapshot),
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file"),
        ):
            run_id = sdk.start(backup.KIND_SNAPSHOT, dedupe_key=ACCOUNT)
            run = _await_terminal(sdk, run_id)

        assert run.status == job_sdk.DONE
        ledger = backup.last_runs(ACCOUNT)
        assert ledger[backup.KIND_SNAPSHOT]["key"].startswith("snapshots/")
        assert ledger[backup.KIND_SNAPSHOT]["bytes"] > 0

    def test_the_status_route_still_serves_the_ledger(self):
        handlers = _registered()
        req = _request("GET", f"/backup/{ACCOUNT}", match_info={"account": ACCOUNT})
        entry = {backup.KIND_SNAPSHOT: {"key": "snapshots/x.tar.gz", "bytes": 10, "at": "z"}}
        with ExitStack() as stack:
            _enter_all(stack, _enabled_owner_env())
            stack.enter_context(
                mock.patch.object(routes_mod.backup_mod, "last_runs", return_value=entry)
            )
            stack.enter_context(
                mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=False)
            )
            stack.enter_context(
                mock.patch.object(routes_mod.backup_mod, "list_remote_backups", return_value={})
            )
            resp = asyncio.run(
                handlers[("GET", "/backup/{account}")](req)  # type: ignore[operator]
            )
        assert resp.status == 200
        assert _payload(resp)["runs"] == entry
