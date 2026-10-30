"""AWS Control routes: drive folders and sharing.

Covers folder create and recursive folder delete, the share body (the minted
URL and its ledger entry, the input refusals, the in-lock publish-gate
re-check, AWS errors, and a ledger that refuses as corrupt), and the shares
list and forget routes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest import mock
from unittest.mock import AsyncMock

import pytest
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
# Drive folder create
# ---------------------------------------------------------------------------


class TestDriveFolderCreate:
    def _create(self, body: dict, *, err=None):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/drive/{ACCOUNT}/folder", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
        create = mock.patch.object(
            routes_mod.storage_mod,
            "create_folder",
            side_effect=err if err else None,
        )
        with p1, p2, p3, _consent_ok(), _drive_found(), create as folder:
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/folder")](req)  # type: ignore[operator]
            )
        return resp, folder

    def test_create_makes_the_folder(self):
        resp, folder = self._create({"section": "drive", "path": "photos"})
        assert resp.status == 200
        assert _payload(resp) == {"created": True, "path": "photos"}
        folder.assert_called_once()

    def test_create_rejects_an_unknown_section(self):
        resp, folder = self._create({"section": "nope", "path": "photos"})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_section"
        folder.assert_not_called()

    def test_create_rejects_a_prefix_escape(self):
        # A ".." segment must be refused by the shared validate_key BEFORE any
        # AWS call — a folder name cannot climb out of the section.
        resp, folder = self._create({"section": "drive", "path": "../evil"})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_key"
        folder.assert_not_called()

    def test_create_rejects_an_empty_path(self):
        # An empty path is refused: it would place the placeholder at the section
        # root, not create a named folder.
        resp, folder = self._create({"section": "drive", "path": ""})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_key"
        folder.assert_not_called()

    def test_create_surfaces_an_aws_error(self):
        # A wrong-bucket-owner put fails at the CLI; the head/put owner pin makes
        # S3 reject it and storage raises AWSError, which becomes a 502.
        resp, _folder = self._create(
            {"section": "drive", "path": "photos"}, err=AWSError("403 wrong owner")
        )
        assert resp.status == 502


# ---------------------------------------------------------------------------
# Drive folder delete (recursive)
# ---------------------------------------------------------------------------


class TestDriveFolderDelete:
    def _delete(self, body: dict, *, removed=3, err=None):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/drive/{ACCOUNT}/folder/delete", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
        delete = mock.patch.object(
            routes_mod.storage_mod,
            "delete_prefix",
            side_effect=err if err else None,
            return_value=removed,
        )
        with p1, p2, p3, _consent_ok(), _drive_found(), delete as prefix:
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/folder/delete")](req)  # type: ignore[operator]
            )
        return resp, prefix

    def test_delete_removes_the_folder_and_reports_the_count(self):
        resp, prefix = self._delete({"section": "drive", "path": "photos"}, removed=5)
        assert resp.status == 200
        assert _payload(resp) == {"deleted": True, "path": "photos", "objects": 5}
        prefix.assert_called_once()

    def test_delete_rejects_an_unknown_section(self):
        resp, prefix = self._delete({"section": "nope", "path": "photos"})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_section"
        prefix.assert_not_called()

    def test_delete_rejects_a_prefix_escape(self):
        resp, prefix = self._delete({"section": "drive", "path": "../.."})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_key"
        prefix.assert_not_called()

    def test_delete_rejects_an_empty_path(self):
        # THE guard that matters: an empty path must never be treated as
        # "delete everything" — it is refused before delete_prefix is reached.
        resp, prefix = self._delete({"section": "drive", "path": ""})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_key"
        prefix.assert_not_called()

    def test_delete_rejects_a_slash_only_path(self):
        # A bare "/" must not be read as "the whole section". validate_key rejects
        # a leading/trailing slash, so it never reaches storage.
        resp, prefix = self._delete({"section": "drive", "path": "/"})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_key"
        prefix.assert_not_called()

    def test_delete_surfaces_an_aws_error(self):
        resp, _prefix = self._delete({"section": "drive", "path": "photos"}, err=AWSError("denied"))
        assert resp.status == 502


# ---------------------------------------------------------------------------
# Drive share — success + validation edges (missing-object + governance + backup
# section covered in test_aws_control_app.py)
# ---------------------------------------------------------------------------


class TestDriveShare:
    def _share(self, body: dict, *, exists=True, record=None):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/drive/{ACCOUNT}/share", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
        rec = record if record is not None else {"id": "sh-1", "key": body.get("key")}
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod, "publish_denied_reason", return_value=""),
            mock.patch.object(routes_mod.storage_mod, "object_exists", return_value=exists),
            mock.patch.object(
                routes_mod.storage_mod, "presign", return_value="https://signed"
            ) as presign,
            mock.patch.object(routes_mod.shares_mod, "record_share", return_value=rec) as recorder,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/share")](req)  # type: ignore[operator]
            )
        return resp, presign, recorder

    def test_share_in_lock_reauthorization_rechecks_the_publish_gate(self):
        # The entry gate runs _publish_gate (a share is bytes-leave-the-box);
        # the in-lock re-check must re-run the SAME set — publish=True — or a
        # policy revoked during the lock wait would still mint a bearer URL.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/drive/{ACCOUNT}/share", match_info={"account": ACCOUNT})
        req.json = AsyncMock(  # type: ignore[method-assign]
            return_value={"section": "drive", "key": "a.txt", "expiresSecs": 3600}
        )
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod, "publish_denied_reason", return_value=""),
            mock.patch.object(
                routes_mod, "_reauthorize_in_lock", AsyncMock(return_value=None)
            ) as reauth,
            mock.patch.object(routes_mod.storage_mod, "object_exists", return_value=True),
            mock.patch.object(routes_mod.storage_mod, "presign", return_value="https://signed"),
            mock.patch.object(routes_mod.shares_mod, "record_share", return_value={"id": "sh-1"}),
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/share")](req)  # type: ignore[operator]
            )
        assert resp.status == 200
        assert reauth.await_count == 1
        assert reauth.await_args.kwargs["publish"] is True

    def test_share_mints_a_url_and_records_the_ledger_entry(self):
        resp, presign, recorder = self._share(
            {"section": "drive", "key": "a.txt", "note": "hi", "expiresSecs": 3600}
        )
        assert resp.status == 200
        body = _payload(resp)
        assert body["url"] == "https://signed"
        assert body["share"]["id"] == "sh-1"
        presign.assert_called_once()
        recorder.assert_called_once()

    def test_share_rejects_an_unknown_section(self):
        resp, presign, recorder = self._share({"section": "nope", "key": "a.txt"})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_section"
        presign.assert_not_called()
        recorder.assert_not_called()

    def test_share_rejects_an_invalid_key(self):
        resp, presign, _ = self._share({"section": "drive", "key": "../evil"})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_key"
        presign.assert_not_called()

    def test_share_rejects_a_non_numeric_expiry(self):
        resp, presign, _ = self._share({"section": "drive", "key": "a.txt", "expiresSecs": "soon"})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_expiry"
        presign.assert_not_called()

    @pytest.mark.parametrize("raw", ["1e999", "-1e999"])
    def test_share_rejects_an_infinite_expiry(self, raw):
        """JSON reads ``1e999`` as an infinite float, which ``int`` cannot convert."""
        body = json.loads('{"section": "drive", "key": "a.txt", "expiresSecs": %s}' % raw)
        resp, presign, recorder = self._share(body)
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_expiry"
        presign.assert_not_called()
        recorder.assert_not_called()

    def test_share_surfaces_an_aws_error_from_object_exists(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/drive/{ACCOUNT}/share", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"section": "drive", "key": "a.txt"})  # type: ignore[method-assign]
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod, "publish_denied_reason", return_value=""),
            mock.patch.object(
                routes_mod.storage_mod, "object_exists", side_effect=AWSError("head denied")
            ),
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/share")](req)  # type: ignore[operator]
            )
        assert resp.status == 502

    def test_share_surfaces_an_aws_error_from_presign(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/drive/{ACCOUNT}/share", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"section": "drive", "key": "a.txt"})  # type: ignore[method-assign]
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod, "publish_denied_reason", return_value=""),
            mock.patch.object(routes_mod.storage_mod, "object_exists", return_value=True),
            mock.patch.object(
                routes_mod.storage_mod, "presign", side_effect=AWSError("sign denied")
            ),
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/share")](req)  # type: ignore[operator]
            )
        assert resp.status == 502

    def test_share_withholds_the_url_when_the_ledger_refuses_as_corrupt(self):
        # The ledger reader refuses a corrupt document rather than
        # replacing it. A mint that could not be RECORDED must not be handed
        # out — the URL would be a live unrevokable bearer grant with no local
        # record, the exact under-reporting the strict reader exists to prevent.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/drive/{ACCOUNT}/share", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"section": "drive", "key": "a.txt"})  # type: ignore[method-assign]
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod, "publish_denied_reason", return_value=""),
            mock.patch.object(routes_mod.storage_mod, "object_exists", return_value=True),
            mock.patch.object(routes_mod.storage_mod, "presign", return_value="https://signed"),
            mock.patch.object(
                routes_mod.shares_mod,
                "record_share",
                side_effect=json.JSONDecodeError("Expecting value", "[ not json", 2),
            ),
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/share")](req)  # type: ignore[operator]
            )
        assert resp.status == 500
        body = _payload(resp)
        assert body["code"] == "share_ledger_corrupt"
        assert "https://signed" not in json.dumps(body)
        assert "[ not json" not in json.dumps(body)


# ---------------------------------------------------------------------------
# Shares list + forget
# ---------------------------------------------------------------------------


class TestSharesListForget:
    def test_shares_list_filters_by_account(self):
        handlers = _registered()
        entries = [{"id": "sh-1", "section": "drive", "key": "a.txt"}]
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.shares_mod, "list_shares", return_value=entries) as listed,
            mock.patch.object(
                routes_mod.storage_mod, "list_object_keys", return_value={"drive/a.txt"}
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/shares")](  # type: ignore[operator]
                    _request("GET", f"/shares?account={ACCOUNT}")
                )
            )
        assert _payload(resp) == {"shares": entries, "checked": True}
        listed.assert_called_once_with(ACCOUNT)

    def test_a_row_whose_object_is_gone_is_marked_in_the_payload(self):
        # The reported defect, at the route: a deleted object left the row
        # asserting a link that resolves to nothing, with no signal at all.
        handlers = _registered()
        entries = [
            {"id": "sh-1", "section": "drive", "key": "gone.txt"},
            {"id": "sh-2", "section": "drive", "key": "here.txt"},
        ]
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.shares_mod, "list_shares", return_value=entries),
            mock.patch.object(
                routes_mod.storage_mod, "list_object_keys", return_value={"drive/here.txt"}
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/shares")](  # type: ignore[operator]
                    _request("GET", f"/shares?account={ACCOUNT}")
                )
            )
        body = _payload(resp)
        assert body["checked"] is True
        # Marked, NOT dropped: both rows are still there. The ledger records an
        # unexpired URL, and deleting the object does not un-mint it.
        assert [row["id"] for row in body["shares"]] == ["sh-1", "sh-2"]
        assert body["shares"][0]["objectMissing"] is True
        assert "objectMissing" not in body["shares"][1]

    def test_the_ledger_is_read_before_the_drive_is_listed(self):
        # ORDER, pinned. A row read after the listing could be a share minted
        # while it was in flight -- absent from it for being newer, and marked
        # as pointing at a deleted object. Reading first makes that unreachable,
        # which is why this route needs no observed-at cutoff.
        handlers = _registered()
        order: list[str] = []
        p1, p2, p3 = _enabled_owner_env()

        def _list_shares(_account):
            order.append("ledger")
            return [{"id": "sh-1", "section": "drive", "key": "a.txt"}]

        def _list_keys(*_args, **_kwargs):
            order.append("drive")
            return {"drive/a.txt"}

        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.shares_mod, "list_shares", _list_shares),
            mock.patch.object(routes_mod.storage_mod, "list_object_keys", _list_keys),
        ):
            asyncio.run(
                handlers[("GET", "/shares")](  # type: ignore[operator]
                    _request("GET", f"/shares?account={ACCOUNT}")
                )
            )
        assert order == ["ledger", "drive"]

    def test_an_empty_ledger_takes_no_listing(self):
        # Nothing to check, so no paid call is made -- and `checked` is
        # vacuously true because no claim went unverified.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.shares_mod, "list_shares", return_value=[]),
            mock.patch.object(routes_mod.storage_mod, "list_object_keys") as keys,
        ):
            resp = asyncio.run(
                handlers[("GET", "/shares")](  # type: ignore[operator]
                    _request("GET", f"/shares?account={ACCOUNT}")
                )
            )
        assert _payload(resp) == {"shares": [], "checked": True}
        keys.assert_not_called()

    def test_an_unscoped_list_reports_that_it_checked_nothing(self):
        # The ledger spans accounts, so there is no single drive to read. The
        # rows still render; the payload says they are unverified rather than
        # letting an absent flag read as "the object is there".
        handlers = _registered()
        entries = [{"id": "sh-1", "section": "drive", "key": "a.txt"}]
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod.shares_mod, "list_shares", return_value=entries),
            mock.patch.object(routes_mod.storage_mod, "list_object_keys") as keys,
        ):
            resp = asyncio.run(
                handlers[("GET", "/shares")](_request("GET", "/shares"))  # type: ignore[operator]
            )
        body = _payload(resp)
        assert body == {"shares": entries, "checked": False}
        keys.assert_not_called()

    def test_an_unreadable_drive_leaves_every_row_unmarked(self, caplog):
        # THE degradation that matters. A throttle, a timeout or a garbled
        # listing must never be rendered as "these objects are gone" -- the rows
        # come back untouched, and the AWS reason reaches the operator's LOG
        # rather than the payload, which carries no reason field at all.
        handlers = _registered()
        entries = [{"id": "sh-1", "section": "drive", "key": "a.txt"}]
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            caplog.at_level(logging.INFO, logger=routes_mod.logger.name),
            mock.patch.object(routes_mod.shares_mod, "list_shares", return_value=entries),
            mock.patch.object(
                routes_mod.storage_mod,
                "list_object_keys",
                side_effect=AWSError("ThrottlingException: slow down"),
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/shares")](  # type: ignore[operator]
                    _request("GET", f"/shares?account={ACCOUNT}")
                )
            )
        assert resp.status == 200
        assert _payload(resp) == {"shares": entries, "checked": False}
        # Swallowing the reason entirely would leave an operator with a section
        # that silently stopped checking, so it is asserted where it now lives.
        assert "Throttling" in caplog.text

    def test_an_account_with_no_drive_still_renders_its_rows(self, caplog):
        handlers = _registered()
        entries = [{"id": "sh-1", "section": "drive", "key": "a.txt"}]
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(name=""),
            caplog.at_level(logging.INFO, logger=routes_mod.logger.name),
            mock.patch.object(routes_mod.shares_mod, "list_shares", return_value=entries),
        ):
            resp = asyncio.run(
                handlers[("GET", "/shares")](  # type: ignore[operator]
                    _request("GET", f"/shares?account={ACCOUNT}")
                )
            )
        assert _payload(resp) == {"shares": entries, "checked": False}
        assert "this account has no drive yet" in caplog.text

    def test_a_withheld_s3_consent_degrades_instead_of_409ing(self):
        # This GET now reaches S3, so it runs the consent gate -- but the Access
        # section is the ledger's only surface and must not disappear behind a
        # 409 for a grant that only governs the remote half.
        #
        # Everything PAST the gate is stubbed to succeed, so the gate is the only
        # reason the listing does not happen: without it this render would reach
        # a paid call on a withdrawn grant and report `checked: true`.
        handlers = _registered()
        entries = [{"id": "sh-1", "section": "drive", "key": "a.txt"}]
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=False)
            ),
            _drive_found(),
            mock.patch.object(routes_mod.shares_mod, "list_shares", return_value=entries),
            mock.patch.object(
                routes_mod.storage_mod, "list_object_keys", return_value={"drive/a.txt"}
            ) as keys,
        ):
            resp = asyncio.run(
                handlers[("GET", "/shares")](  # type: ignore[operator]
                    _request("GET", f"/shares?account={ACCOUNT}")
                )
            )
        assert resp.status == 200
        assert _payload(resp)["checked"] is False
        keys.assert_not_called()

    def test_an_unavailable_account_is_audited_as_a_denial(self):
        # A permission decision reaches SEL even though the route degrades: the
        # profile does not resolve to the requested account, and that is the
        # one event an incident review asks about.
        handlers = _registered()
        entries = [{"id": "sh-1", "section": "drive", "key": "a.txt"}]
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.accounts_mod, "resolve_account_profile", AsyncMock(return_value=None)
            ),
            mock.patch.object(routes_mod.shares_mod, "list_shares", return_value=entries),
            mock.patch.object(routes_mod, "_audit") as audited,
        ):
            resp = asyncio.run(
                handlers[("GET", "/shares")](  # type: ignore[operator]
                    _request("GET", f"/shares?account={ACCOUNT}")
                )
            )
        assert _payload(resp)["checked"] is False
        # Asserted BEFORE the fields are read: a regression that stops auditing
        # leaves `call_args` as None, and dereferencing it reports an
        # AttributeError instead of the event that went missing.
        assert audited.called
        assert audited.call_args.args[0] == "shares_list"
        assert audited.call_args.kwargs["error"] == "account_unavailable"

    def test_forget_removes_a_known_share(self):
        handlers = _registered()
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod.shares_mod, "forget_share", return_value={"id": "sh-1"}),
        ):
            req = _request("POST", "/shares/sh-1/forget", match_info={"id": "sh-1"})
            req.json = AsyncMock(return_value={})  # type: ignore[method-assign]
            resp = asyncio.run(
                handlers[("POST", "/shares/{id}/forget")](req)  # type: ignore[operator]
            )
        assert _payload(resp) == {"forgotten": True}

    def test_forget_404s_an_unknown_share(self):
        handlers = _registered()
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod.shares_mod, "forget_share", return_value=None),
        ):
            req = _request("POST", "/shares/ghost/forget", match_info={"id": "ghost"})
            req.json = AsyncMock(return_value={})  # type: ignore[method-assign]
            resp = asyncio.run(
                handlers[("POST", "/shares/{id}/forget")](req)  # type: ignore[operator]
            )
        assert resp.status == 404
        assert _payload(resp)["code"] == "unknown_share"

    def test_forget_reports_a_corrupt_ledger_instead_of_claiming_unknown(self):
        # A lenient read would make a corrupt ledger scan as every share absent,
        # so forget would answer 404 "unknown share" while the record sat readable
        # in the corrupt bytes — and the rewrite would then destroy it.
        handlers = _registered()
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.shares_mod,
                "forget_share",
                side_effect=json.JSONDecodeError("Expecting value", "[ not json", 2),
            ),
        ):
            req = _request("POST", "/shares/sh-1/forget", match_info={"id": "sh-1"})
            req.json = AsyncMock(return_value={})  # type: ignore[method-assign]
            resp = asyncio.run(
                handlers[("POST", "/shares/{id}/forget")](req)  # type: ignore[operator]
            )
        assert resp.status == 500
        assert _payload(resp)["code"] == "share_ledger_corrupt"
