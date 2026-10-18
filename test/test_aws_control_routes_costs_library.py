"""AWS Control routes: costs and the library.

Covers the cost endpoint (fresh cache, fetch success, fetch error with and
without a cache, missing consent) and the library's list, push and remove
bodies: the ledger reconcile, the library lock and the re-checks it runs once
held, and the error mapping.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
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

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import routes as routes_mod
from kiro_crew.deploy.engine import AWSError

# ---------------------------------------------------------------------------
# Costs — fresh cache, fetch success, fetch error with/without cache
# ---------------------------------------------------------------------------


class TestCostsEndpoint:
    def test_fresh_cache_is_served_without_touching_consent(self):
        handlers = _registered()
        cached = {"account": ACCOUNT, "monthToDate": 7.0}
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.costs_mod, "read_cached", return_value=cached),
            mock.patch.object(routes_mod.costs_mod, "is_fresh", return_value=True),
            mock.patch.object(routes_mod.aws_consent, "refuse_and_log") as consent,
        ):
            resp = asyncio.run(
                handlers[("GET", "/costs/{account}")](  # type: ignore[operator]
                    _request("GET", f"/costs/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert body["fresh"] is True and body["monthToDate"] == 7.0
        consent.assert_not_called()

    def test_refresh_fetches_and_returns_fresh_result(self):
        handlers = _registered()
        result = {"account": ACCOUNT, "monthToDate": 2.5}
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.costs_mod, "read_cached", return_value=None),
            mock.patch.object(routes_mod.costs_mod, "is_fresh", return_value=False),
            _consent_ok(),
            mock.patch.object(
                routes_mod.costs_mod, "fetch_month_costs", return_value=result
            ) as fetch,
        ):
            resp = asyncio.run(
                handlers[("GET", "/costs/{account}")](  # type: ignore[operator]
                    _request("GET", f"/costs/{ACCOUNT}?refresh=1", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert body["fresh"] is True and body["monthToDate"] == 2.5
        fetch.assert_called_once()

    def test_fetch_error_with_cache_returns_stale_and_the_error(self):
        # A live fetch that fails but a cache exists: keep the page alive with
        # the stale numbers and a labelled fetchError, not a 502.
        handlers = _registered()
        cached = {"account": ACCOUNT, "monthToDate": 9.9}
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.costs_mod, "read_cached", return_value=cached),
            mock.patch.object(routes_mod.costs_mod, "is_fresh", return_value=False),
            _consent_ok(),
            mock.patch.object(
                routes_mod.costs_mod,
                "fetch_month_costs",
                side_effect=AWSError("ce throttled"),
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/costs/{account}")](  # type: ignore[operator]
                    _request("GET", f"/costs/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert body["fresh"] is False and body["monthToDate"] == 9.9
        assert "fetchError" in body

    def test_fetch_error_without_cache_is_a_502(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.costs_mod, "read_cached", return_value=None),
            mock.patch.object(routes_mod.costs_mod, "is_fresh", return_value=False),
            _consent_ok(),
            mock.patch.object(
                routes_mod.costs_mod,
                "fetch_month_costs",
                side_effect=AWSError("ce throttled"),
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/costs/{account}")](  # type: ignore[operator]
                    _request("GET", f"/costs/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert resp.status == 502

    def test_consent_missing_without_cache_returns_the_consent_refusal(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.costs_mod, "read_cached", return_value=None),
            mock.patch.object(routes_mod.costs_mod, "is_fresh", return_value=False),
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=False)
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/costs/{account}")](  # type: ignore[operator]
                    _request("GET", f"/costs/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert resp.status == 409
        assert _payload(resp)["code"] == "aws_consent_required"


# ---------------------------------------------------------------------------
# Library — list + push success and error mapping
# ---------------------------------------------------------------------------


class TestLibrary:
    def test_library_list_renders_rows_when_the_bucket_cannot_be_read(self):
        # The load-bearing degradation: no working connection means the ledger's
        # claim is UNVERIFIED, so the rows still render but `reconciled` is false
        # and the reason is stated. A caller that could not tell this apart from
        # "nothing in the cloud" is how a delete control gets offered for an item
        # nothing is known about.
        handlers = _registered()
        rows = [{"slug": "x", "name": "X"}]
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.accounts_mod,
                "resolve_account_profile",
                AsyncMock(return_value=None),
            ),
            mock.patch.object(routes_mod.library_mod, "list_pushable", return_value=rows),
            mock.patch.object(routes_mod.library_mod, "reconcile") as reconcile,
        ):
            resp = asyncio.run(
                handlers[("GET", "/library/{account}")](  # type: ignore[operator]
                    _request("GET", f"/library/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert body["artifacts"] == rows
        assert body["reconciled"] is False
        assert body["remoteError"]
        # Nothing was concluded about absence, so nothing was pruned.
        reconcile.assert_not_called()
        # And no remoteOnly key: an empty list would read as "no untracked
        # copies", which was never established.
        assert "remoteOnly" not in body

    def test_library_list_reconciles_the_ledger_and_reports_untracked_copies(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        rows = [{"slug": "local-one", "name": "L"}]
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(
                routes_mod.storage_mod,
                "list_library_folders",
                return_value=["local-one", "pushed-elsewhere"],
            ),
            mock.patch.object(routes_mod.library_mod, "list_pushable", return_value=rows),
            mock.patch.object(routes_mod.library_mod, "reconcile", return_value=["stale"]) as rec,
        ):
            resp = asyncio.run(
                handlers[("GET", "/library/{account}")](  # type: ignore[operator]
                    _request("GET", f"/library/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert body["reconciled"] is True
        assert "remoteError" not in body
        # The bucket listing is what corrects the ledger, and it is the SERVER's
        # own read -- never a set handed in by the caller.
        assert rec.call_args.args[0] == ACCOUNT
        assert rec.call_args.args[1] == {"local-one", "pushed-elsewhere"}
        # The snapshot's own time travels with it: reconcile refuses to prune a
        # record written after the listing, and cannot do that without knowing
        # when the listing was taken.
        assert isinstance(rec.call_args.kwargs["observed_at"], dt.datetime)
        # A cloud copy with no local artifact row has nothing to carry it in
        # `artifacts`; without this it would be unreachable from the console.
        assert body["remoteOnly"] == ["pushed-elsewhere"]

    def test_library_list_ignores_a_folder_that_is_not_a_slug(self):
        # A prefix written by another tool ("my uploads/") is not a slug the
        # store could have produced, so it cannot answer for a ledger key. It is
        # dropped before reconcile rather than counted as a cloud copy.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(
                routes_mod.storage_mod,
                "list_library_folders",
                return_value=["good-slug", "my uploads", "Not_A_Slug"],
            ),
            mock.patch.object(routes_mod.library_mod, "list_pushable", return_value=[]),
            mock.patch.object(routes_mod.library_mod, "reconcile", return_value=[]) as rec,
        ):
            resp = asyncio.run(
                handlers[("GET", "/library/{account}")](  # type: ignore[operator]
                    _request("GET", f"/library/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert rec.call_args.args[1] == {"good-slug"}
        assert _payload(resp)["remoteOnly"] == ["good-slug"]

    def test_library_list_skips_reconcile_when_the_listing_fails(self):
        # An AWS failure is not evidence of an empty bucket. The reason is
        # reported and the ledger is left exactly as it was.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(
                routes_mod.storage_mod,
                "list_library_folders",
                side_effect=AWSError("list denied"),
            ),
            mock.patch.object(routes_mod.library_mod, "list_pushable", return_value=[]),
            mock.patch.object(routes_mod.library_mod, "reconcile") as rec,
        ):
            resp = asyncio.run(
                handlers[("GET", "/library/{account}")](  # type: ignore[operator]
                    _request("GET", f"/library/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert body["reconciled"] is False and "list denied" in body["remoteError"]
        rec.assert_not_called()

    def test_library_list_skips_reconcile_when_the_account_has_no_drive(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            mock.patch.object(routes_mod.storage_mod, "find_drive", return_value=""),
            mock.patch.object(routes_mod.library_mod, "list_pushable", return_value=[]),
            mock.patch.object(routes_mod.library_mod, "reconcile") as rec,
        ):
            resp = asyncio.run(
                handlers[("GET", "/library/{account}")](  # type: ignore[operator]
                    _request("GET", f"/library/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        # Still 200 with rows: the Library list is a LOCAL view first, and an
        # account with no drive yet must not blank the page.
        assert resp.status == 200
        assert _payload(resp)["reconciled"] is False
        rec.assert_not_called()

    def test_the_listing_and_the_prune_run_under_one_lock(self):
        # The window GPT and Design both flagged: a push completing between the
        # reconcile's listing and its prune has its fresh record deleted on a
        # snapshot taken before it existed. The lock is what makes the two a
        # single step, so the listing must observe it HELD -- asserting on the
        # lock rather than on a sleep, which would only prove timing.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        seen: dict[str, bool] = {}

        def _list_folders(*_a, **_kw):
            seen["locked_during_listing"] = routes_mod._library_lock.locked()
            return ["a"]

        def _reconcile(*_a, **_kw):
            seen["locked_during_prune"] = routes_mod._library_lock.locked()
            return []

        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(
                routes_mod.storage_mod, "list_library_folders", side_effect=_list_folders
            ),
            mock.patch.object(routes_mod.library_mod, "list_pushable", return_value=[]),
            mock.patch.object(routes_mod.library_mod, "reconcile", side_effect=_reconcile),
        ):
            asyncio.run(
                handlers[("GET", "/library/{account}")](  # type: ignore[operator]
                    _request("GET", f"/library/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert seen == {"locked_during_listing": True, "locked_during_prune": True}
        # And released afterwards, or the next render would deadlock behind it.
        assert not routes_mod._library_lock.locked()

    def test_library_list_skips_reconcile_when_consent_is_withdrawn_while_queued(self):
        # The lock makes the reconcile read WAIT too, and a listing is still a
        # call into a paid service -- so it re-checks inside the lock like the two
        # mutations. Failure degrades to "not reconciled" rather than erroring:
        # this route's local half must keep rendering.
        handlers = _registered()
        calls = {"n": 0}

        async def _consent_then_deny(*_a, **_kw):
            calls["n"] += 1
            return calls["n"] <= 1

        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.accounts_mod,
                "resolve_account_profile",
                AsyncMock(return_value=("prof", "us-west-2")),
            ),
            mock.patch.object(
                routes_mod.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account=ACCOUNT)),
            ),
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(side_effect=_consent_then_deny)
            ),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "list_library_folders") as lister,
            mock.patch.object(routes_mod.library_mod, "list_pushable", return_value=[]),
            mock.patch.object(routes_mod.library_mod, "reconcile") as rec,
        ):
            resp = asyncio.run(
                handlers[("GET", "/library/{account}")](  # type: ignore[operator]
                    _request("GET", f"/library/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert resp.status == 200
        assert body["reconciled"] is False and body["remoteError"]
        # No AWS call and no prune on a grant that does not hold.
        lister.assert_not_called()
        rec.assert_not_called()

    def test_library_list_skips_reconcile_when_the_drive_changes_while_queued(self):
        # Identity unchanged is NOT enough: tag discovery can return a different
        # bucket while the profile still names the same account, and this module
        # keeps no bucket-name cache precisely because that identity must not be
        # stale. A queued caller holding a pre-wait name is that staleness.
        handlers = _registered()
        seen = {"n": 0}

        def _drive_then_move(*_a, **_kw):
            seen["n"] += 1
            return "kirocrew-drive-abc" if seen["n"] <= 1 else "kirocrew-drive-def"

        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.accounts_mod,
                "resolve_account_profile",
                AsyncMock(return_value=("prof", "us-west-2")),
            ),
            mock.patch.object(
                routes_mod.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account=ACCOUNT)),
            ),
            _consent_ok(),
            mock.patch.object(routes_mod.storage_mod, "find_drive", side_effect=_drive_then_move),
            mock.patch.object(routes_mod.storage_mod, "list_library_folders") as lister,
            mock.patch.object(routes_mod.library_mod, "list_pushable", return_value=[]),
            mock.patch.object(routes_mod.library_mod, "reconcile") as rec,
        ):
            resp = asyncio.run(
                handlers[("GET", "/library/{account}")](  # type: ignore[operator]
                    _request("GET", f"/library/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert resp.status == 200
        assert _payload(resp)["reconciled"] is False
        lister.assert_not_called()
        rec.assert_not_called()

    def test_library_list_survives_an_unwritable_ledger(self):
        # The reconcile WRITES, and this route is best-effort by contract. An
        # unwritable ledger dir must not turn a page render into a 500 -- the rows
        # are still renderable, they are just unverified.
        handlers = _registered()
        rows = [{"slug": "x", "name": "X"}]
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "list_library_folders", return_value=["x"]),
            mock.patch.object(routes_mod.library_mod, "list_pushable", return_value=rows),
            mock.patch.object(
                routes_mod.library_mod,
                "reconcile",
                side_effect=OSError("read-only file system"),
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/library/{account}")](  # type: ignore[operator]
                    _request("GET", f"/library/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert resp.status == 200
        assert body["artifacts"] == rows
        # Reported, not swallowed: the payload must not claim a reconcile happened.
        assert body["reconciled"] is False and body["remoteError"]

    def test_library_list_survives_a_corrupt_ledger(self):
        # The strict update reader refuses a corrupt ledger with
        # JSONDecodeError. The list route is best-effort by contract and its
        # rows come from the LENIENT display read, so the render must survive
        # and the degradation must be reported — with a reason that says
        # "repair", not "retry".
        handlers = _registered()
        rows = [{"slug": "x", "name": "X"}]
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "list_library_folders", return_value=["x"]),
            mock.patch.object(routes_mod.library_mod, "list_pushable", return_value=rows),
            mock.patch.object(
                routes_mod.library_mod,
                "reconcile",
                side_effect=json.JSONDecodeError("Expecting value", "{ not json", 2),
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/library/{account}")](  # type: ignore[operator]
                    _request("GET", f"/library/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert resp.status == 200
        assert body["artifacts"] == rows
        assert body["reconciled"] is False
        assert "corrupt" in body["remoteError"]

    def test_library_list_audits_an_identity_denial_it_degrades_past(self):
        # _guarded's own rule: a permission DECISION reaches SEL. This route
        # degrades instead of failing, so without an explicit audit the decision
        # would go unrecorded -- the one event an incident review asks about.
        handlers = _registered()
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.accounts_mod,
                "resolve_account_profile",
                AsyncMock(return_value=None),
            ),
            mock.patch.object(routes_mod.library_mod, "list_pushable", return_value=[]),
            mock.patch.object(routes_mod, "_audit") as audit,
        ):
            resp = asyncio.run(
                handlers[("GET", "/library/{account}")](  # type: ignore[operator]
                    _request("GET", f"/library/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert resp.status == 200
        assert _payload(resp)["reconciled"] is False
        denials = [c for c in audit.call_args_list if "denied" in c.args]
        assert denials, "the degraded identity denial was not audited"

    def test_library_list_audits_a_queued_identity_denial_it_degrades_past(self):
        # The SECOND site of the same class: the pre-lock denial was audited last
        # round, this one fires inside _reauthorize_in_lock. On the read path that
        # response becomes a degraded 200, so the decision has to be recorded at
        # the point it is made or it vanishes on this path entirely.
        handlers = _registered()
        calls = {"n": 0}

        async def _resolve_then_lose(*_a, **_kw):
            calls["n"] += 1
            return ("prof", "us-west-2") if calls["n"] <= 1 else None

        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.accounts_mod,
                "resolve_account_profile",
                AsyncMock(side_effect=_resolve_then_lose),
            ),
            mock.patch.object(
                routes_mod.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account=ACCOUNT)),
            ),
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "list_library_folders") as lister,
            mock.patch.object(routes_mod.library_mod, "list_pushable", return_value=[]),
            mock.patch.object(routes_mod, "_audit") as audit,
        ):
            resp = asyncio.run(
                handlers[("GET", "/library/{account}")](  # type: ignore[operator]
                    _request("GET", f"/library/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert resp.status == 200
        assert _payload(resp)["reconciled"] is False
        lister.assert_not_called()
        denials = [c for c in audit.call_args_list if "denied" in c.args]
        assert denials, "the queued identity denial was not audited"

    def test_library_list_gives_up_the_reconcile_rather_than_waiting_on_a_slow_mutation(self):
        # The lock is also held across a push, whose upload allows up to 600s. An
        # unbounded wait here would hang every Library page render for that long.
        # Errors on this path already degrade to reconciled:false; slowness has to
        # degrade the same way, or the degradation is only half real.
        handlers = _registered()
        rows = [{"slug": "x", "name": "X"}]
        p1, p2, p3 = _enabled_owner_env()

        async def _run():
            # Hold the lock the way a slow push would, then render.
            # flake-ok: pre-existing unbounded acquire of the module-level library lock standing in for a slow push that would block forever if the lock were left held, moved verbatim; tracked for a de-flake pass
            await routes_mod._library_lock.acquire()
            try:
                return await handlers[("GET", "/library/{account}")](  # type: ignore[operator]
                    _request("GET", f"/library/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            finally:
                routes_mod._library_lock.release()

        # flake-ok: pre-existing 50 ms real lock-wait timeout that the route's wait_for on the library lock must expire on while the test holds the lock, moved verbatim; tracked for a de-flake pass
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod, "_LIBRARY_RECONCILE_LOCK_WAIT_SECS", 0.05),
            mock.patch.object(routes_mod.storage_mod, "list_library_folders") as lister,
            mock.patch.object(routes_mod.library_mod, "list_pushable", return_value=rows),
            mock.patch.object(routes_mod.library_mod, "reconcile") as rec,
        ):
            resp = asyncio.run(_run())
        body = _payload(resp)
        # The rows still render; only the re-read was skipped, and it says so.
        assert resp.status == 200 and body["artifacts"] == rows
        assert body["reconciled"] is False and body["remoteError"]
        lister.assert_not_called()
        rec.assert_not_called()
        # Released, so the next render is not stuck behind this one.
        assert not routes_mod._library_lock.locked()

    def _push(self, *, side_effect=None, record=None):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/library/{ACCOUNT}/push", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"slug": "art-1"})  # type: ignore[method-assign]
        push = mock.patch.object(
            routes_mod.library_mod,
            "push_artifact",
            side_effect=side_effect,
            return_value=record if record is not None else {"key": "artifacts/art-1"},
        )
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod, "publish_denied_reason", return_value=""),
            push,
        ):
            resp = asyncio.run(
                handlers[("POST", "/library/{account}/push")](req)  # type: ignore[operator]
            )
        return resp

    def test_push_uploads_the_artifact(self):
        resp = self._push()
        assert resp.status == 200
        body = _payload(resp)
        assert body["pushed"] is True and body["key"] == "artifacts/art-1"

    def test_push_requires_a_slug(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/library/{ACCOUNT}/push", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={})  # type: ignore[method-assign]
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod, "publish_denied_reason", return_value=""),
            mock.patch.object(routes_mod.library_mod, "push_artifact") as push,
        ):
            resp = asyncio.run(
                handlers[("POST", "/library/{account}/push")](req)  # type: ignore[operator]
            )
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_slug"
        push.assert_not_called()

    def test_push_404s_an_unknown_artifact(self):
        from kiro_crew.artifacts import ArtifactNotFoundError

        resp = self._push(side_effect=ArtifactNotFoundError("nope"))
        assert resp.status == 404
        assert _payload(resp)["code"] == "unknown_artifact"

    def test_push_maps_a_not_pushable_value_error_to_400(self):
        # A credential-bearing or otherwise unpushable artifact raises
        # ValueError from the scan; the route reports it as not_pushable, 400.
        resp = self._push(side_effect=ValueError("credential-like content"))
        assert resp.status == 400
        assert _payload(resp)["code"] == "not_pushable"

    def test_push_reports_a_corrupt_ledger_not_a_client_error(self):
        # The trap here: JSONDecodeError subclasses ValueError,
        # so without its own arm the ledger's corruption refusal would be
        # reported as 400 not_pushable — blaming the artifact for a store the
        # operator has to repair, on a push whose upload may already be in the
        # bucket.
        resp = self._push(side_effect=json.JSONDecodeError("Expecting value", "{ not json", 2))
        assert resp.status == 500
        assert _payload(resp)["code"] == "library_ledger_corrupt"

    def test_push_surfaces_an_aws_error(self):
        resp = self._push(side_effect=AWSError("put denied"))
        assert resp.status == 502

    def _remove(self, *, body=None, side_effect=None, result=None, publish_reason=""):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/library/{ACCOUNT}/remove", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"slug": "art-1"} if body is None else body)  # type: ignore[method-assign]
        remove = mock.patch.object(
            routes_mod.library_mod,
            "library_remove",
            side_effect=side_effect,
            return_value=(
                result
                if result is not None
                else {"slug": "art-1", "account": ACCOUNT, "objects": 2, "forgotten": True}
            ),
        )
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod, "publish_denied_reason", return_value=publish_reason),
            remove as removed,
        ):
            resp = asyncio.run(
                handlers[("POST", "/library/{account}/remove")](req)  # type: ignore[operator]
            )
        return resp, removed

    def test_remove_deletes_the_cloud_copy_and_reports_both_halves(self):
        resp, removed = self._remove()
        assert resp.status == 200
        body = _payload(resp)
        assert body["removed"] is True
        # Objects AND record, told apart: a copy pushed from another machine has
        # objects with no local record, and one number for both would hide which
        # of the two was emptied.
        assert body["objects"] == 2 and body["forgotten"] is True
        assert removed.call_args.args == (
            "prof",
            "us-west-2",
            "kirocrew-drive-abc",
            ACCOUNT,
            "art-1",
        )

    def test_remove_is_not_gated_by_the_publish_gate(self):
        # Publish governance decides whether BYTES MAY LEAVE the box. A removal
        # sends nothing out, so a profile that forbids publishing must still be
        # able to empty a bucket it is paying for -- otherwise denying publish
        # traps whatever was pushed before it was denied.
        resp, removed = self._remove(publish_reason="capability denied")
        assert resp.status == 200
        removed.assert_called_once()

    def test_remove_requires_a_slug(self):
        resp, removed = self._remove(body={})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_slug"
        removed.assert_not_called()

    def test_remove_refuses_a_slug_that_would_widen_the_delete_prefix(self):
        # "a/b" is a KEY, not a slug: it would address a prefix below one
        # artifact. The empty and '/'-shaped values are the same class of
        # widening, and none of them reach the storage layer.
        for bad in ("", "/", "..", "a/b", "Upper"):
            resp, removed = self._remove(body={"slug": bad})
            assert resp.status == 400, bad
            assert _payload(resp)["code"] == "invalid_slug"
            removed.assert_not_called()

    def test_remove_maps_a_rejected_slug_from_the_engine_to_400(self):
        # library_remove re-checks the shape itself, so the route reports that
        # refusal as a bad request rather than a 500.
        resp, _removed = self._remove(side_effect=ValueError("'x/y' is not an artifact slug"))
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_slug"

    def test_remove_reports_a_corrupt_ledger_not_an_invalid_slug(self):
        # JSONDecodeError subclasses ValueError, so without its own arm
        # the ledger's corruption refusal reads as 400 invalid_slug — blaming
        # the request for a store the operator has to repair.
        resp, _removed = self._remove(
            side_effect=json.JSONDecodeError("Expecting value", "{ not json", 2)
        )
        assert resp.status == 500
        assert _payload(resp)["code"] == "library_ledger_corrupt"

    def test_remove_surfaces_an_aws_error(self):
        # A failed delete must NOT report success: the objects are still there,
        # and the ledger still says so, which reconcile will confirm.
        resp, _removed = self._remove(side_effect=AWSError("delete denied"))
        assert resp.status == 502

    def test_push_and_remove_both_hold_the_library_lock(self):
        # Both are a network round trip followed by a ledger write, and the two
        # interleaving on one slug can leave an object behind the delete sweep or
        # forget a record the other is about to write. One lock covers push,
        # remove, and the reconcile read.
        held: dict[str, bool] = {}

        def _record_push(*_a, **_kw):
            held["push"] = routes_mod._library_lock.locked()
            return {"slug": "art-1"}

        def _record_remove(*_a, **_kw):
            held["remove"] = routes_mod._library_lock.locked()
            return {"slug": "art-1", "account": ACCOUNT, "objects": 1, "forgotten": True}

        self._push(side_effect=_record_push)
        self._remove(side_effect=_record_remove)
        assert held == {"push": True, "remove": True}
        assert not routes_mod._library_lock.locked()

    def _queued_then_revoked(self, path: str, *, revoke: str):
        """Run push/remove with authorization that FAILS on the second check.

        The lock makes a caller wait, and the wait sits between the checks
        _require_drive ran and the AWS call they authorized. These fakes pass the
        first time and fail the second, standing in for the policy changing while
        the caller was queued.
        """
        handlers = _registered()
        calls = {"consent": 0, "identity": 0, "publish": 0}

        async def _consent_then_deny(*_a, **_kw):
            calls["consent"] += 1
            return not (revoke == "consent" and calls["consent"] > 1)

        async def _identity_then_move(*_a, **_kw):
            calls["identity"] += 1
            if revoke == "identity" and calls["identity"] > 1:
                return aws_consent.Identity(ok=True, account="999988887777")
            return aws_consent.Identity(ok=True, account=ACCOUNT)

        def _publish_then_deny(*_a, **_kw):
            calls["publish"] += 1
            return "capability denied" if (revoke == "publish" and calls["publish"] > 1) else ""

        req = _request("POST", f"{path}", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"slug": "art-1"})  # type: ignore[method-assign]
        route = "/library/{account}/push" if path.endswith("push") else "/library/{account}/remove"
        target = "push_artifact" if path.endswith("push") else "library_remove"
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.accounts_mod,
                "resolve_account_profile",
                AsyncMock(return_value=("prof", "us-west-2")),
            ),
            mock.patch.object(
                routes_mod.aws_consent, "probe_identity", AsyncMock(side_effect=_identity_then_move)
            ),
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(side_effect=_consent_then_deny)
            ),
            _drive_found(),
            mock.patch.object(routes_mod, "publish_denied_reason", side_effect=_publish_then_deny),
            mock.patch.object(routes_mod.library_mod, target) as engine,
        ):
            resp = asyncio.run(handlers[("POST", route)](req))  # type: ignore[operator]
        return resp, engine

    def test_push_refuses_when_consent_is_withdrawn_while_queued(self):
        # The gap the lock introduced: a queued push must not upload on an
        # authorization it has outlived. Same re-check drive_upload runs after its
        # spool, for the same reason.
        resp, engine = self._queued_then_revoked(f"/library/{ACCOUNT}/push", revoke="consent")
        assert resp.status == 409
        assert _payload(resp)["code"] == "aws_consent_required"
        engine.assert_not_called()

    def test_push_refuses_when_the_profile_moves_account_while_queued(self):
        # A profile repointed A -> B while queued: the upload would still write
        # into the bucket resolved for A, so it is refused rather than run.
        resp, engine = self._queued_then_revoked(f"/library/{ACCOUNT}/push", revoke="identity")
        assert resp.status == 409
        engine.assert_not_called()

    def test_push_refuses_when_publish_governance_starts_denying_while_queued(self):
        resp, engine = self._queued_then_revoked(f"/library/{ACCOUNT}/push", revoke="publish")
        assert resp.status == 403
        assert _payload(resp)["code"] == "publish_denied"
        engine.assert_not_called()

    def test_remove_refuses_when_consent_is_withdrawn_while_queued(self):
        # A queued DELETE can outlive its authorization too, and a delete under
        # withdrawn consent is still an unauthorized call into the account.
        resp, engine = self._queued_then_revoked(f"/library/{ACCOUNT}/remove", revoke="consent")
        assert resp.status == 409
        assert _payload(resp)["code"] == "aws_consent_required"
        engine.assert_not_called()

    def test_remove_does_not_consult_the_publish_gate_even_in_the_lock(self):
        # Removal sends nothing out, so the egress gate does not apply on the way
        # in OR on the re-check -- a profile denied publishing must still be able
        # to empty a bucket it pays for.
        resp, engine = self._queued_then_revoked(f"/library/{ACCOUNT}/remove", revoke="publish")
        assert resp.status == 200
        engine.assert_called_once()
