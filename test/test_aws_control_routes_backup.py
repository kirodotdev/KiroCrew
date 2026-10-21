"""AWS Control routes: backup and the install's local settings.

Covers the backup verbs (status, run, nightly, nightly sessions, the layer-B
toggle, restore), the retention route, the install label (a local rename the
next backup carries), and the IAM policy render.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

from aws_control_routes_helpers import (
    ACCOUNT,
    _consent_ok,
    _drive_found,
    _enabled_owner_env,
    _payload,
    _registered,
    _request,
)

from kiro_crew.apps.builtins.aws_control.backend import routes as routes_mod
from kiro_crew.deploy.engine import AWSError

# ---------------------------------------------------------------------------
# Backup — status, run, nightly, restore
# ---------------------------------------------------------------------------


class TestBackupEndpoints:
    def test_status_reports_toggle_runs_and_remote_listing(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=True),
            mock.patch.object(routes_mod.backup_mod, "last_runs", return_value={"snapshot": {}}),
            mock.patch.object(
                routes_mod.backup_mod,
                "list_remote_backups",
                return_value=[{"key": "snapshots/x"}],
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/backup/{account}")](  # type: ignore[operator]
                    # `remote=1` is required now: the remote half is opt-in so the
                    # poll that follows a run does not spend paid AWS calls on it.
                    _request("GET", f"/backup/{ACCOUNT}?remote=1", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert body["nightly"] is True
        assert body["remote"] == [{"key": "snapshots/x"}]

    def test_status_records_a_remote_error_but_still_returns_local_state(self):
        # Consent granted but the remote LIST fails: the page must still render
        # the local toggle/runs and label the remote error, not 502.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=False),
            mock.patch.object(routes_mod.backup_mod, "last_runs", return_value={}),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", side_effect=AWSError("list denied")
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/backup/{account}")](  # type: ignore[operator]
                    # `remote=1` is required now: the remote half is opt-in so the
                    # poll that follows a run does not spend paid AWS calls on it.
                    _request("GET", f"/backup/{ACCOUNT}?remote=1", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert body["nightly"] is False
        assert "remoteError" in body

    def test_status_leaves_remote_none_when_consent_is_missing(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=False)
            ),
            mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=False),
            mock.patch.object(routes_mod.backup_mod, "last_runs", return_value={}),
            mock.patch.object(routes_mod.storage_mod, "find_drive") as find,
        ):
            resp = asyncio.run(
                handlers[("GET", "/backup/{account}")](  # type: ignore[operator]
                    # `remote=1` is required now: the remote half is opt-in so the
                    # poll that follows a run does not spend paid AWS calls on it.
                    _request("GET", f"/backup/{ACCOUNT}?remote=1", match_info={"account": ACCOUNT})
                )
            )
        assert _payload(resp)["remote"] is None
        find.assert_not_called()

    def _run_backup(self, kind, *, start=None, sdk_present=True):
        """Drive ``POST /backup/{account}/run``.

        The handler does not perform the backup itself: it claims a durable Job SDK
        run and returns its id. So this stubs the SDK rather than the backup
        functions. The runner's own behaviour -- resolving its account, refusing
        a key that names none, and the reconciliation of a run left behind by a
        dead gateway -- lives in ``test_aws_control_backup_job.py``.
        """
        from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod

        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/backup/{ACCOUNT}/run", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"kind": kind})  # type: ignore[method-assign]
        fake = (
            SimpleNamespace(start_async=start or AsyncMock(return_value="e" * 32))
            if sdk_present
            else None
        )
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod, "get_job_sdk", return_value=fake),
            # The platform-availability pre-check (kind_unavailable_reason) is
            # its own guard with its own dedicated tests in
            # test_aws_control_windows.py; this helper is about the claim/
            # dispatch mechanics for a kind that IS available, so the guard
            # must read as satisfied here too, including on the Windows CI
            # shard where the real value is False.
            mock.patch.object(backup_mod, "_CAN_PIN_TRAVERSAL", True),
        ):
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/run")](req)  # type: ignore[operator]
            )
        return resp

    def test_run_snapshot_backup_starts_a_job_and_returns_its_id(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod

        resp = self._run_backup(backup_mod.KIND_SNAPSHOT)
        assert resp.status == 200
        body = _payload(resp)
        assert body["started"] is True
        assert body["kind"] == backup_mod.KIND_SNAPSHOT
        assert body["runId"] == "e" * 32

    def test_run_sessions_backup_claims_the_sessions_kind(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod

        start = AsyncMock(return_value="f" * 32)
        resp = self._run_backup(backup_mod.KIND_SESSIONS, start=start)
        assert resp.status == 200
        assert _payload(resp)["kind"] == backup_mod.KIND_SESSIONS
        # The account is the dedupe key, so a double click adopts the first run
        # instead of doing the paid upload twice.
        start.assert_awaited_once_with(backup_mod.KIND_SESSIONS, dedupe_key=ACCOUNT)

    def test_run_reports_an_absent_job_runtime_as_503(self):
        # Enabled, but no SDK was published: the `jobs` grant is missing or the
        # context build failed. The runtime is absent — not a bad request.
        from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod

        resp = self._run_backup(backup_mod.KIND_SNAPSHOT, sdk_present=False)
        assert resp.status == 503
        assert _payload(resp)["code"] == "jobs_unavailable"

    def test_run_reports_a_refused_claim_as_503(self):
        # The SDK could not persist the initial record, or the host refused a
        # thread. Nothing started, and the code says which layer refused.
        from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod
        from kiro_crew.apps.job_sdk import JobError

        resp = self._run_backup(
            backup_mod.KIND_SNAPSHOT, start=AsyncMock(side_effect=JobError("no disk"))
        )
        assert resp.status == 503
        assert _payload(resp)["code"] == "backup_start_failed"

    def test_nightly_toggle_persists_the_flag(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/backup/{ACCOUNT}/nightly", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"enabled": True})  # type: ignore[method-assign]
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.backup_mod, "set_nightly") as set_nightly,
        ):
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/nightly")](req)  # type: ignore[operator]
            )
        assert _payload(resp) == {"nightly": True}
        set_nightly.assert_called_once_with(ACCOUNT, True)

    def test_a_non_boolean_enabled_is_refused_and_never_persisted(self):
        # `bool("false")` is True in Python, so coercing this field would turn
        # UNATTENDED PAID uploads ON for a caller that asked for off. Every shape
        # below is rejected, and set_nightly is never reached.
        handlers = _registered()
        for raw in ("false", "true", 0, 1, "", None, [], {}):
            p1, p2, p3 = _enabled_owner_env()
            req = _request("POST", f"/backup/{ACCOUNT}/nightly", match_info={"account": ACCOUNT})
            req.json = AsyncMock(return_value={"enabled": raw})  # type: ignore[method-assign]
            with (
                p1,
                p2,
                p3,
                mock.patch.object(routes_mod.backup_mod, "set_nightly") as set_nightly,
            ):
                resp = asyncio.run(
                    handlers[("POST", "/backup/{account}/nightly")](req)  # type: ignore[operator]
                )
            assert resp.status == 400, f"{raw!r} was accepted"
            assert _payload(resp)["code"] == "invalid_enabled"
            set_nightly.assert_not_called()

    def test_a_toggle_that_could_not_persist_fails_with_a_structured_error(self):
        # `set_nightly` now propagates rather than publishing over state it could
        # not read, so this endpoint has a reachable failure. It must fail loudly
        # -- reporting a setting the next read contradicts is worse than an error
        # -- but with the machine-readable `code` every non-2xx here carries.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/backup/{ACCOUNT}/nightly", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"enabled": True})  # type: ignore[method-assign]
        boom = OSError(28, "No space left on device", "/home/someone/.kirocrew/backup.json")
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.backup_mod, "set_nightly", side_effect=boom),
        ):
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/nightly")](req)  # type: ignore[operator]
            )
        body = _payload(resp)
        assert resp.status == 500
        assert body["code"] == "state_persist_failed"
        # The response must not report the toggle as applied...
        assert "nightly" not in body
        # ...and must not echo the OSError's rendering, which carries the
        # absolute path of the state file. The log has it; a response body does
        # not need to disclose the local filesystem layout.
        assert ".kirocrew" not in body["error"]

    def test_layer_b_toggle_persists_the_permission(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/backup/{ACCOUNT}/layer-b", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"enabled": True})  # type: ignore[method-assign]
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.backup_mod, "set_sessions_layer_b") as setter,
        ):
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/layer-b")](req)  # type: ignore[operator]
            )
        assert _payload(resp) == {"sessionsIncludeLayerB": True}
        setter.assert_called_once_with(ACCOUNT, True)

    def test_a_non_boolean_layer_b_is_refused_and_never_persisted(self):
        # Same rule as the nightly toggle above, and the cost of coercing is
        # higher here: `bool("false")` is True, so a caller asking for off would
        # switch unredacted model context ON, and an object already uploaded
        # cannot be recalled.
        handlers = _registered()
        for raw in ("false", "true", 0, 1, "", None, [], {}):
            p1, p2, p3 = _enabled_owner_env()
            req = _request("POST", f"/backup/{ACCOUNT}/layer-b", match_info={"account": ACCOUNT})
            req.json = AsyncMock(return_value={"enabled": raw})  # type: ignore[method-assign]
            with (
                p1,
                p2,
                p3,
                mock.patch.object(routes_mod.backup_mod, "set_sessions_layer_b") as setter,
            ):
                resp = asyncio.run(
                    handlers[("POST", "/backup/{account}/layer-b")](req)  # type: ignore[operator]
                )
            assert resp.status == 400, f"{raw!r} was accepted"
            assert _payload(resp)["code"] == "invalid_enabled"
            setter.assert_not_called()

    def test_a_named_layer_b_scope_is_passed_through_and_echoed(self):
        # The wider scope ships host-wide terminal conversations off-host,
        # unrecallably, so it must be NAMED in the request rather than derived from
        # the act of enabling: a bare `{"enabled": true}` is indistinguishable from an
        # idempotent retry or a client still rendering older copy. The resulting scope
        # is echoed because an unrecognised value records the narrower grant rather
        # than failing, and a caller that named one must be able to see what it got.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/backup/{ACCOUNT}/layer-b", match_info={"account": ACCOUNT})
        req.json = AsyncMock(  # type: ignore[method-assign]
            return_value={"enabled": True, "scope": "cli+conversations"}
        )
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.backup_mod, "set_sessions_layer_b") as setter,
            mock.patch.object(
                routes_mod.backup_mod, "layer_b_grant_covers_conversations", return_value=True
            ),
        ):
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/layer-b")](req)  # type: ignore[operator]
            )
        assert _payload(resp) == {
            "sessionsIncludeLayerB": True,
            "sessionsLayerBScope": "cli+conversations",
        }
        setter.assert_called_once_with(ACCOUNT, True, scope="cli+conversations")

    def test_a_non_string_layer_b_scope_is_refused_and_never_persisted(self):
        # Same posture as the boolean above. A coerced scope is a consent boundary
        # decided by `str()` rather than by the operator.
        handlers = _registered()
        for raw in (True, 1, 0, [], {}, 1.5):
            p1, p2, p3 = _enabled_owner_env()
            req = _request("POST", f"/backup/{ACCOUNT}/layer-b", match_info={"account": ACCOUNT})
            req.json = AsyncMock(  # type: ignore[method-assign]
                return_value={"enabled": True, "scope": raw}
            )
            with (
                p1,
                p2,
                p3,
                mock.patch.object(routes_mod.backup_mod, "set_sessions_layer_b") as setter,
            ):
                resp = asyncio.run(
                    handlers[("POST", "/backup/{account}/layer-b")](req)  # type: ignore[operator]
                )
            assert resp.status == 400, f"{raw!r} was accepted"
            assert _payload(resp)["code"] == "invalid_scope"
            setter.assert_not_called()

    def test_a_layer_b_write_that_failed_does_not_report_success(self):
        # A permission the console renders as stored while the next read denies it
        # is worse than an error, so the failure is loud, structured, and does not
        # echo the state file's absolute path.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/backup/{ACCOUNT}/layer-b", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"enabled": True})  # type: ignore[method-assign]
        boom = OSError(28, "No space left on device", "/home/someone/.kirocrew/backup.json")
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.backup_mod, "set_sessions_layer_b", side_effect=boom),
        ):
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/layer-b")](req)  # type: ignore[operator]
            )
        body = _payload(resp)
        assert resp.status == 500
        assert body["code"] == "state_persist_failed"
        assert "sessionsIncludeLayerB" not in body
        assert ".kirocrew" not in body["error"]

    def test_a_real_false_still_disables_nightly(self):
        # The validation must not break the ordinary off path.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/backup/{ACCOUNT}/nightly", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"enabled": False})  # type: ignore[method-assign]
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.backup_mod, "set_nightly") as set_nightly,
        ):
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/nightly")](req)  # type: ignore[operator]
            )
        assert _payload(resp) == {"nightly": False}
        set_nightly.assert_called_once_with(ACCOUNT, False)

    def test_the_transcript_toggle_flips_only_the_transcript_bit(self):
        # Its own route and its own field. A caller asking for nightly
        # transcripts must not be able to reach the snapshot grant, in either
        # direction -- that separation is the whole reason for a second bit.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request(
            "POST", f"/backup/{ACCOUNT}/nightly-sessions", match_info={"account": ACCOUNT}
        )
        req.json = AsyncMock(return_value={"enabled": True})  # type: ignore[method-assign]
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.backup_mod, "set_nightly_sessions") as set_sessions,
            mock.patch.object(routes_mod.backup_mod, "set_nightly") as set_nightly,
        ):
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/nightly-sessions")](req)  # type: ignore[operator]
            )
        assert _payload(resp) == {"nightlySessions": True}
        set_sessions.assert_called_once_with(ACCOUNT, True)
        set_nightly.assert_not_called()

    def test_a_non_boolean_never_starts_uploading_transcripts(self):
        # `bool("false")` is True, and here that would begin uploading the most
        # sensitive payload in the product for a caller that asked for off. Same
        # validation as the snapshot toggle because it is the same code, and this
        # pins that it really is reached on this route too.
        handlers = _registered()
        for raw in ("false", "true", 0, 1, "", None, [], {}):
            p1, p2, p3 = _enabled_owner_env()
            req = _request(
                "POST", f"/backup/{ACCOUNT}/nightly-sessions", match_info={"account": ACCOUNT}
            )
            req.json = AsyncMock(return_value={"enabled": raw})  # type: ignore[method-assign]
            with (
                p1,
                p2,
                p3,
                mock.patch.object(routes_mod.backup_mod, "set_nightly_sessions") as set_sessions,
            ):
                resp = asyncio.run(
                    handlers[("POST", "/backup/{account}/nightly-sessions")](req)  # type: ignore[operator]
                )
            assert resp.status == 400, f"{raw!r} was accepted"
            assert _payload(resp)["code"] == "invalid_enabled"
            set_sessions.assert_not_called()

    def test_a_transcript_toggle_that_could_not_persist_says_so(self):
        # A setting the next read contradicts is worse than an error, and the
        # response must not echo the OSError's rendering, which carries the
        # absolute path of the state file.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request(
            "POST", f"/backup/{ACCOUNT}/nightly-sessions", match_info={"account": ACCOUNT}
        )
        req.json = AsyncMock(return_value={"enabled": True})  # type: ignore[method-assign]
        boom = OSError(28, "No space left on device", "/home/someone/.kirocrew/backup.json")
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.backup_mod, "set_nightly_sessions", side_effect=boom),
        ):
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/nightly-sessions")](req)  # type: ignore[operator]
            )
        body = _payload(resp)
        assert resp.status == 500
        assert body["code"] == "state_persist_failed"
        assert "nightlySessions" not in body
        assert ".kirocrew" not in body["error"]

    def test_the_status_payload_reports_the_transcript_grant_separately(self):
        # The console renders two switches, so the payload must carry two fields.
        # Folded into one, the page could not show that transcripts are still off
        # while the snapshot nightly is on -- the exact state most installs are in.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("GET", f"/backup/{ACCOUNT}", match_info={"account": ACCOUNT})
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=True),
            mock.patch.object(
                routes_mod.backup_mod, "nightly_sessions_enabled", return_value=False
            ),
            mock.patch.object(routes_mod.backup_mod, "last_runs", return_value={}),
            mock.patch.object(routes_mod, "_account_jobs", return_value={}),
        ):
            resp = asyncio.run(
                handlers[("GET", "/backup/{account}")](req)  # type: ignore[operator]
            )
        body = _payload(resp)
        assert body["nightly"] is True
        assert body["nightlySessions"] is False

    def test_the_status_payload_says_when_this_account_is_not_the_scheduled_one(self):
        # The grant is settable on every registered account and the nightly loop
        # runs for the one the default key belongs to, so a grant recorded on any
        # other account is authorized and unreachable at once. The payload has to
        # carry that, or the console can only show a schedule nothing honours.
        #
        # The host is pinned SUPPORTED and the redaction gap cleared, because both
        # outrank the account by design. Without that this asserts the account
        # answer on a platform whose truthful answer is the capability one, so the
        # outcome turns on the runner rather than on the code. Pinning them keeps
        # the assertion universal rather than gating it behind a platform check.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("GET", f"/backup/{ACCOUNT}", match_info={"account": ACCOUNT})
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.backup_mod, "_CAN_PIN_TRAVERSAL", True),
            mock.patch.object(
                routes_mod.backup_mod, "_unattended_sessions_redaction_gap", return_value=None
            ),
            mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=False),
            mock.patch.object(routes_mod.backup_mod, "nightly_sessions_enabled", return_value=True),
            mock.patch.object(routes_mod.backup_mod, "last_runs", return_value={}),
            mock.patch.object(routes_mod, "_account_jobs", return_value={}),
            mock.patch.object(
                routes_mod.accounts_mod,
                "default_account_id",
                AsyncMock(return_value="999988887777"),
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/backup/{account}")](req)  # type: ignore[operator]
            )
        body = _payload(resp)
        # The grant still reads back exactly as the owner set it; the notice is a
        # second field beside it, never a correction of it.
        assert body["nightlySessions"] is True
        assert body["nightlySessionsBlocked"] == routes_mod.backup_mod.BLOCK_OTHER_ACCOUNT

    def test_the_scheduled_account_gets_no_account_notice(self):
        # The other direction, so the notice cannot be one that renders always: on
        # the account the loop does run for, with the host able and nothing else
        # withholding, the field is EMPTY. Asserting merely "not the account code"
        # would also pass on a platform that answers with a DIFFERENT code, which
        # is how a negative control ends up green by coincidence.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("GET", f"/backup/{ACCOUNT}", match_info={"account": ACCOUNT})
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.backup_mod, "_CAN_PIN_TRAVERSAL", True),
            mock.patch.object(
                routes_mod.backup_mod, "_unattended_sessions_redaction_gap", return_value=None
            ),
            mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=False),
            mock.patch.object(routes_mod.backup_mod, "nightly_sessions_enabled", return_value=True),
            mock.patch.object(routes_mod.backup_mod, "last_runs", return_value={}),
            mock.patch.object(routes_mod, "_account_jobs", return_value={}),
            mock.patch.object(
                routes_mod.accounts_mod, "default_account_id", AsyncMock(return_value=ACCOUNT)
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/backup/{account}")](req)  # type: ignore[operator]
            )
        assert _payload(resp)["nightlySessionsBlocked"] is None

    def test_the_status_payload_says_when_the_snapshot_grant_is_on_the_wrong_account(self):
        # The snapshot mirror of the transcript notice above. set_nightly records
        # the snapshot grant per account while the nightly loop runs for the
        # default one, so a snapshot grant on a non-default account is authorized
        # and never visited. The payload carries that as nightlyBlocked beside the
        # grant, or the console shows a schedule that nothing honours.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("GET", f"/backup/{ACCOUNT}", match_info={"account": ACCOUNT})
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=True),
            mock.patch.object(
                routes_mod.backup_mod, "nightly_sessions_enabled", return_value=False
            ),
            mock.patch.object(routes_mod.backup_mod, "last_runs", return_value={}),
            mock.patch.object(routes_mod, "_account_jobs", return_value={}),
            mock.patch.object(
                routes_mod.accounts_mod,
                "default_account_id",
                AsyncMock(return_value="999988887777"),
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/backup/{account}")](req)  # type: ignore[operator]
            )
        body = _payload(resp)
        # The grant reads back as set; the notice is a second field beside it.
        assert body["nightly"] is True
        assert body["nightlyBlocked"] == routes_mod.backup_mod.BLOCK_OTHER_ACCOUNT

    def test_the_scheduled_account_gets_no_snapshot_account_notice(self):
        # The other direction, so the snapshot notice cannot be one that renders
        # always: on the account the loop runs for, the field is EMPTY. The
        # snapshot kind has no host or redaction block, so the default account
        # with the grant on is the clean case.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("GET", f"/backup/{ACCOUNT}", match_info={"account": ACCOUNT})
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=True),
            mock.patch.object(
                routes_mod.backup_mod, "nightly_sessions_enabled", return_value=False
            ),
            mock.patch.object(routes_mod.backup_mod, "last_runs", return_value={}),
            mock.patch.object(routes_mod, "_account_jobs", return_value={}),
            mock.patch.object(
                routes_mod.accounts_mod, "default_account_id", AsyncMock(return_value=ACCOUNT)
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/backup/{account}")](req)  # type: ignore[operator]
            )
        assert _payload(resp)["nightlyBlocked"] is None

    def test_restore_downloads_a_valid_archive_key(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/backup/{ACCOUNT}/restore", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"key": "snapshots/a.tar.gz"})  # type: ignore[method-assign]
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "validate_key", return_value=None),
            mock.patch.object(
                routes_mod.backup_mod,
                "restore_download",
                return_value={"path": "/staging/a.tar.gz"},
            ) as restore,
        ):
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/restore")](req)  # type: ignore[operator]
            )
        body = _payload(resp)
        assert body["downloaded"] is True and body["path"] == "/staging/a.tar.gz"
        restore.assert_called_once()

    def test_restore_surfaces_an_aws_error(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/backup/{ACCOUNT}/restore", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"key": "sessions/a.tar.gz"})  # type: ignore[method-assign]
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "validate_key", return_value=None),
            mock.patch.object(
                routes_mod.backup_mod,
                "restore_download",
                side_effect=AWSError("download denied"),
            ),
        ):
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/restore")](req)  # type: ignore[operator]
            )
        assert resp.status == 502

    def test_status_carries_this_installs_identity_with_no_aws_call(self):
        # The install block rides the UN-POLLED payload: it is local state, so a
        # poll every few seconds must learn it without a paid round trip. A
        # 32-hex id and a non-empty label are what let every archive row be told
        # from every other install's.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        identity = {"id": "a" * 32, "label": "install-aaaa"}
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=False),
            mock.patch.object(routes_mod.backup_mod, "last_runs", return_value={}),
            mock.patch.object(routes_mod, "_account_jobs", return_value={}),
            mock.patch.object(routes_mod.backup_mod, "install_identity", return_value=identity),
            mock.patch.object(routes_mod.aws_consent, "refuse_and_log") as consent,
            mock.patch.object(routes_mod.storage_mod, "find_drive") as find,
        ):
            resp = asyncio.run(
                handlers[("GET", "/backup/{account}")](  # type: ignore[operator]
                    _request("GET", f"/backup/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert re.fullmatch(r"[0-9a-f]{32}", body["install"]["id"])
        assert body["install"]["label"]
        assert body["remote"] is None
        # The un-polled payload reaches neither consent nor the drive.
        consent.assert_not_called()
        find.assert_not_called()

    def test_remote_listing_is_opt_in_and_others_is_a_second_opt_in(self):
        # `?remote=1` lists this install's own prefixes only (include_others
        # False); `?remote=1&others=1` also enumerates the OTHER installs'
        # prefixes -- a second, costlier opt-in because each other install
        # costs a list per kind plus a label read. The flag reaches
        # list_remote_backups exactly as the query said.
        handlers = _registered()
        for query, expected in (("remote=1", False), ("remote=1&others=1", True)):
            p1, p2, p3 = _enabled_owner_env()
            with (
                p1,
                p2,
                p3,
                _consent_ok(),
                _drive_found(),
                mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=False),
                mock.patch.object(routes_mod.backup_mod, "last_runs", return_value={}),
                mock.patch.object(routes_mod, "_account_jobs", return_value={}),
                mock.patch.object(
                    routes_mod.backup_mod,
                    "install_identity",
                    return_value={"id": "a" * 32, "label": "install-aaaa"},
                ),
                mock.patch.object(
                    routes_mod.backup_mod, "list_remote_backups", return_value={"listed": query}
                ) as listed,
            ):
                resp = asyncio.run(
                    handlers[("GET", "/backup/{account}")](  # type: ignore[operator]
                        _request(
                            "GET",
                            f"/backup/{ACCOUNT}?{query}",
                            match_info={"account": ACCOUNT},
                        )
                    )
                )
            assert resp.status == 200, query
            assert _payload(resp)["remote"] == {"listed": query}
            assert listed.call_args.kwargs["include_others"] is expected, query

    def _restore(self, body: dict, *, restore=None):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/backup/{ACCOUNT}/restore", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
        restore_patch = (
            mock.patch.object(routes_mod.backup_mod, "restore_download", side_effect=restore)
            if isinstance(restore, Exception)
            else mock.patch.object(
                routes_mod.backup_mod,
                "restore_download",
                return_value=restore if restore is not None else {"path": "/staging/x"},
            )
        )
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "validate_key", return_value=None),
            restore_patch as restored,
        ):
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/restore")](req)  # type: ignore[operator]
            )
        return resp, restored

    def test_restore_of_a_foreign_archive_is_409_naming_the_owning_install(self):
        # A restore of a key another install wrote is a 409 (a conflict with the
        # state of the thing, not a caller-authority problem). The machine
        # readable `code` is mandatory -- test_error_code_contract.py reds every
        # shard without one -- and the owning id rides along so the dialog can
        # say WHOSE archive this is.
        owner = "b" * 32
        resp, _ = self._restore(
            {"key": f"snapshots/{owner}/x.tar.gz"},
            restore=routes_mod.backup_mod.UnprovenArchive(
                routes_mod.backup_mod.ORIGIN_OTHER, owner
            ),
        )
        assert resp.status == 409
        body = _payload(resp)
        assert body["code"] == "foreign_install_archive"
        assert body["install"] == owner

    def test_restore_with_foreign_ok_true_reaches_the_download_with_the_override(self):
        # The same request with an explicit override succeeds, and foreign_ok
        # travels to restore_download as True -- the override is the operator's
        # deliberate decision, which is the whole point of the gate.
        resp, restored = self._restore(
            {"key": "snapshots/" + "b" * 32 + "/x.tar.gz", "foreignOk": True},
            restore={"path": "/staging/x", "origin": "other", "install": "b" * 32},
        )
        assert resp.status == 200
        assert _payload(resp)["downloaded"] is True
        assert restored.call_args.kwargs["foreign_ok"] is True

    def test_foreign_ok_sent_as_the_string_false_is_a_400_and_downloads_nothing(self):
        # THE stringly-typed trap: `bool("false")` is True in Python, so a caller
        # sending {"foreignOk": "false"} to ask NOT to override would be granted
        # the override -- overwriting this machine's memory with another's. The
        # flag is validated, never coerced, so a non-bool is a 400 and
        # restore_download is never reached.
        resp, restored = self._restore({"key": "snapshots/x.tar.gz", "foreignOk": "false"})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_foreign_ok"
        restored.assert_not_called()

    def _restore_for_real(self, body: dict, tmp_path, *, get_file=None):
        """Run the route against the REAL ``restore_download``.

        The rest of this class stands a mock in for that function, which is the right
        shape for testing the route's own translation of a result into a response.
        It is the wrong shape for asserting what the backend DECIDES: a mock returns
        whatever the test says, so an assertion about a refusal is really an
        assertion about the mock. Only ``storage.get_file`` is stubbed here -- the
        classification and both override gates are the shipped ones.
        """
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/backup/{ACCOUNT}/restore", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
        backup = routes_mod.backup_mod
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "validate_key", return_value=None),
            mock.patch.object(backup, "_state_path", lambda: tmp_path / "backup.json"),
            mock.patch.object(backup, "app_data_dir", lambda name: tmp_path / "appdata"),
            mock.patch.object(backup.storage, "get_file", side_effect=get_file) as got,
        ):
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/restore")](req)  # type: ignore[operator]
            )
        return resp, got

    def test_a_legacy_key_is_refused_by_the_real_backend_before_any_download(self, tmp_path):
        # A pre-namespace archive carries no id, so nothing proves whose it is, and
        # the shipped rule refuses every origin except a proven self one until the
        # caller accepts the risk. Being plausibly this operator's own pre-upgrade
        # backup is a reason to word the confirmation for that case, not a reason to
        # skip it. Asserted against the REAL backend: the refusal has to come from
        # the code, not from a mock told to produce it.
        resp, got = self._restore_for_real(
            {"key": "snapshots/kirocrew-snapshot-20260101T000000Z-abcdef.tar.gz"},
            tmp_path,
        )
        assert resp.status == 409
        body = _payload(resp)
        assert body["code"] == "foreign_install_archive"
        assert body["origin"] == "legacy"
        # Nothing was transferred: the origins local state can settle are refused
        # before a paid GET, so a planted object cannot bill the account.
        got.assert_not_called()

    def test_a_legacy_key_downloads_once_the_caller_accepts_it(self, tmp_path):
        # The same request with the override reaches the download and the reply names
        # the origin the caller accepted, so a client learns WHICH unproven case it
        # just took.
        def fake_get(profile, region, bucket, section, key, dest, *, account, timeout=600):
            Path(dest).write_bytes(b"legacy archive")

        resp, got = self._restore_for_real(
            {
                "key": "snapshots/kirocrew-snapshot-20260101T000000Z-abcdef.tar.gz",
                "foreignOk": True,
            },
            tmp_path,
            get_file=fake_get,
        )
        assert resp.status == 200
        body = _payload(resp)
        assert body["downloaded"] is True
        assert body["origin"] == "legacy"
        got.assert_called_once()


# ---------------------------------------------------------------------------
# Install label — rename THIS install (display only, local, no AWS)
# ---------------------------------------------------------------------------


class TestInstallLabel:
    def _label(self, body: dict, *, set_label=None):
        handlers = _registered()
        req = _request("POST", "/install/label")
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
        label_patch = (
            mock.patch.object(routes_mod.backup_mod, "set_install_label", side_effect=set_label)
            if isinstance(set_label, Exception)
            else mock.patch.object(
                routes_mod.backup_mod,
                "set_install_label",
                return_value=set_label if set_label is not None else {"id": "a" * 32, "label": "x"},
            )
        )
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod.storage_mod, "find_drive") as find,
            label_patch as labeled,
        ):
            resp = asyncio.run(handlers[("POST", "/install/label")](req))  # type: ignore[operator]
        return resp, labeled, find

    def test_a_valid_label_returns_the_stored_identity_with_no_aws_call(self):
        # The rename is local: it changes what is displayed and reaches the drive
        # only on the next backup, so this endpoint must make no AWS call. The
        # stored identity comes back so the console shows the sanitised value.
        stored = {"id": "a" * 32, "label": "my laptop"}
        resp, labeled, find = self._label({"label": "my laptop"}, set_label=stored)
        assert resp.status == 200
        assert _payload(resp) == {"install": stored}
        labeled.assert_called_once_with("my laptop")
        find.assert_not_called()

    def test_a_non_string_label_is_a_400_and_never_stored(self):
        # The id decides what is allowed; the label decides only what is shown --
        # but it must still be a string. A non-string shape is refused before
        # set_install_label is reached.
        for raw in (123, None, [], {}, True):
            resp, labeled, _ = self._label({"label": raw})
            assert resp.status == 400, f"{raw!r} was accepted"
            assert _payload(resp)["code"] == "invalid_label"
            labeled.assert_not_called()

    def test_a_label_that_could_not_persist_fails_with_a_structured_error(self):
        # set_install_label can genuinely fail to write. It must fail loudly with
        # the machine-readable `code` every non-2xx here carries, and must not
        # echo the OSError's own text, which renders the state file's absolute
        # path.
        boom = OSError(28, "No space left on device", "/home/someone/.kirocrew/backup.json")
        resp, _labeled, _find = self._label({"label": "ok"}, set_label=boom)
        body = _payload(resp)
        assert resp.status == 500
        assert body["code"] == "state_persist_failed"
        assert ".kirocrew" not in body["error"]


# ---------------------------------------------------------------------------
# IAM policy render
# ---------------------------------------------------------------------------


class TestIamPolicy:
    def test_iam_policy_renders_the_drive_tier_locally(self):
        # A pure local render — no AWS reached — returning the drive-tier JSON
        # the owner pastes into their account.
        handlers = _registered()
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch(
                "kiro_crew.deploy.iam.policy_json", return_value={"Version": "2012-10-17"}
            ) as policy,
        ):
            resp = asyncio.run(
                handlers[("GET", "/iam-policy")](_request("GET", "/iam-policy"))  # type: ignore[operator]
            )
        assert _payload(resp) == {"policy": {"Version": "2012-10-17"}}
        policy.assert_called_once_with(tier="drive")


class TestBackupRetentionRoute:
    """The only shipped way to turn retention on, and everything it refuses.

    Retention erases object versions permanently and ships off, so this route IS the
    switch. It takes the count rather than a flag because there is no separate enable
    bit: a count is on and ``null`` is off. Anything it accepts authorizes permanent
    deletion of the owner's archives, which is why it validates and never coerces.
    """

    _PATH = "/backup/{account}/retention"

    def _post(self, payload, *, setter_error=None):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/backup/{ACCOUNT}/retention", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value=payload)  # type: ignore[method-assign]
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(
                routes_mod.backup_mod, "set_retention_keep", side_effect=setter_error
            ) as setter,
        ):
            resp = asyncio.run(handlers[("POST", self._PATH)](req))  # type: ignore[operator]
        return resp, setter

    def test_the_route_is_registered(self):
        # The finding this answers was that nothing shipped could set the key, so the
        # registration itself is the load-bearing part and is asserted directly.
        assert ("POST", self._PATH) in _registered()

    def test_a_count_is_written_and_echoed(self):
        resp, setter = self._post({"keep": 5})
        assert resp.status == 200
        assert _payload(resp)["retentionKeep"] == 5
        setter.assert_called_once_with(ACCOUNT, 5)

    def test_null_turns_retention_back_off(self):
        # A switch that can only be turned ON is worse than none: an operator who
        # enabled pruning has to be able to stop it without hand-editing a file.
        resp, setter = self._post({"keep": None})
        assert resp.status == 200
        assert _payload(resp)["retentionKeep"] is None
        setter.assert_called_once_with(ACCOUNT, None)

    def test_true_is_refused_rather_than_stored_as_keep_one(self):
        # `True` IS an int in Python, so a coercing handler would store keep=1 -- the
        # most destructive value available -- for a caller that believed it sent a flag.
        resp, setter = self._post({"keep": True})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_keep"
        setter.assert_not_called()

    def test_a_string_count_is_refused(self):
        resp, setter = self._post({"keep": "5"})
        assert resp.status == 400
        setter.assert_not_called()

    def test_a_count_below_the_floor_is_refused_rather_than_clamped(self):
        # Refused, not clamped: the stored value must be the one the caller asked for,
        # so nobody configures 0 and is later told they configured 1.
        resp, setter = self._post({"keep": 0})
        assert resp.status == 400
        setter.assert_not_called()

    def test_a_large_count_is_accepted_because_there_is_no_ceiling(self):
        # Keeping more than exists is not a harm, so there is nothing to refuse. The
        # route must not invent a bound the module does not have.
        resp, setter = self._post({"keep": 10_000})
        assert resp.status == 200
        setter.assert_called_once()
        assert setter.call_args.args[1] == 10_000

    def test_a_missing_key_is_refused_rather_than_read_as_off(self):
        # An absent field must not silently mean "turn it off": a caller that omitted
        # the value by mistake would then disable pruning without asking.
        resp, setter = self._post({})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_keep"
        setter.assert_not_called()

    def test_a_failed_state_write_reports_the_failure_and_not_the_value(self):
        # Reporting a setting the next read contradicts is worse than an error, and the
        # message is fixed because the OSError's own text carries the state file path.
        resp, _setter = self._post({"keep": 2}, setter_error=OSError("disk full"))
        assert resp.status == 500
        body = _payload(resp)
        assert body["code"] == "state_persist_failed"
        assert "retentionKeep" not in body
        assert "disk full" not in body["error"]

    def test_the_status_read_reports_the_effective_count(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=False),
            mock.patch.object(routes_mod.backup_mod, "last_runs", return_value={}),
            mock.patch.object(routes_mod.backup_mod, "retention_keep", return_value=4) as reader,
        ):
            resp = asyncio.run(
                handlers[("GET", "/backup/{account}")](  # type: ignore[operator]
                    _request("GET", f"/backup/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert _payload(resp)["retentionKeep"] == 4
        # The sweep's own resolution, so the panel cannot show a number the sweep would
        # clamp or ignore.
        reader.assert_called_once_with(ACCOUNT)

    def test_the_status_read_reports_the_unclaimed_floor(self):
        # The count above says what retention WILL collect. Without this an operator
        # cannot see the part it never will, which is why a bill can fail to fall after
        # they enable it -- and until now the number reached only a SEL event.
        floor = {"snapshot": {"archives": 2, "bytes": 4096, "at": "2026-01-01T00:00:00+00:00"}}
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=False),
            mock.patch.object(routes_mod.backup_mod, "last_runs", return_value={}),
            mock.patch.object(routes_mod.backup_mod, "retention_keep", return_value=3),
            mock.patch.object(
                routes_mod.backup_mod, "retention_unclaimed", return_value=floor
            ) as reader,
        ):
            resp = asyncio.run(
                handlers[("GET", "/backup/{account}")](  # type: ignore[operator]
                    _request("GET", f"/backup/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert _payload(resp)["retentionUnclaimed"] == floor
        # Per account, like every other field here: two connected accounts are two
        # buckets and two bills.
        reader.assert_called_once_with(ACCOUNT)

    def test_the_unclaimed_floor_rides_the_unpolled_half(self):
        # It is read from local state and costs no AWS call, so it must NOT be gated
        # behind `remote=1` the way the bucket listing is. Gating it there would hide
        # the permanent cost behind the opt-in an operator opens last.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=False),
            mock.patch.object(routes_mod.backup_mod, "last_runs", return_value={}),
            mock.patch.object(routes_mod.backup_mod, "retention_keep", return_value=None),
            mock.patch.object(routes_mod.backup_mod, "retention_unclaimed", return_value={}),
        ):
            resp = asyncio.run(
                handlers[("GET", "/backup/{account}")](  # type: ignore[operator]
                    _request("GET", f"/backup/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        # The remote half stayed unrequested, which is what makes the presence of the
        # field below evidence about the unpolled payload rather than about a listing.
        assert body["remote"] is None
        assert body["retentionUnclaimed"] == {}

    def test_the_status_read_reports_the_unrecorded_objects_beside_the_floor(self):
        # Beside it, never instead of it. The floor above is what retention will never
        # collect out of the set it REMEMBERS; this counts what the listing held that it
        # has no record of, which that floor reads as 0 by design. One number would
        # answer neither question, so both are served and the second claims nothing.
        floor = {"snapshot": {"archives": 2, "bytes": 4096, "at": "2026-01-01T00:00:00+00:00"}}
        other = {"snapshot": {"objects": 7, "bytes": 8192, "at": "2026-01-01T00:00:00+00:00"}}
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=False),
            mock.patch.object(routes_mod.backup_mod, "last_runs", return_value={}),
            mock.patch.object(routes_mod.backup_mod, "retention_keep", return_value=3),
            mock.patch.object(routes_mod.backup_mod, "retention_unclaimed", return_value=floor),
            mock.patch.object(
                routes_mod.backup_mod, "retention_unrecorded", return_value=other
            ) as reader,
        ):
            resp = asyncio.run(
                handlers[("GET", "/backup/{account}")](  # type: ignore[operator]
                    _request("GET", f"/backup/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert body["retentionUnrecorded"] == other
        # Both, and distinct: a payload serving one value under both names would hide
        # exactly the gap the second field exists to disclose.
        assert body["retentionUnclaimed"] == floor
        reader.assert_called_once_with(ACCOUNT)

    def test_the_unrecorded_count_rides_the_unpolled_half(self):
        # Local state, no AWS call, so it must not sit behind `remote=1` -- the opt-in an
        # operator opens last is the wrong place for a permanent cost.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=False),
            mock.patch.object(routes_mod.backup_mod, "last_runs", return_value={}),
            mock.patch.object(routes_mod.backup_mod, "retention_keep", return_value=None),
            mock.patch.object(routes_mod.backup_mod, "retention_unclaimed", return_value={}),
            mock.patch.object(routes_mod.backup_mod, "retention_unrecorded", return_value={}),
        ):
            resp = asyncio.run(
                handlers[("GET", "/backup/{account}")](  # type: ignore[operator]
                    _request("GET", f"/backup/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert body["remote"] is None
        assert body["retentionUnrecorded"] == {}

    def test_the_status_read_reports_how_the_last_sweep_ended(self):
        # Beside the count, never folded into it. `retentionKeep` says what a sweep
        # would act on; this says whether one has, so a sweep refused every night stops
        # looking identical to a working one. Local and free, so it rides the unpolled
        # half like the other retention fields.
        last = {
            "snapshot": {
                "result": "refused",
                "skipped": "authorization refused",
                "retired": 0,
                "versions": 0,
                "at": "2026-01-01T00:00:00+00:00",
            }
        }
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.backup_mod, "nightly_enabled", return_value=False),
            mock.patch.object(routes_mod.backup_mod, "last_runs", return_value={}),
            mock.patch.object(routes_mod.backup_mod, "retention_keep", return_value=3),
            mock.patch.object(
                routes_mod.backup_mod, "retention_last_sweep", return_value=last
            ) as reader,
        ):
            resp = asyncio.run(
                handlers[("GET", "/backup/{account}")](  # type: ignore[operator]
                    _request("GET", f"/backup/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert body["remote"] is None
        assert body["retentionLastSweep"] == last
        assert body["retentionKeep"] == 3
        # Per account, like every other field here.
        reader.assert_called_once_with(ACCOUNT)
