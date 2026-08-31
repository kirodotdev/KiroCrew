"""AWS Control routes — the success and error paths of the handler bodies.

``test_aws_control_app.py`` pins the P0 CONTRACT: which routes exist, that
every one is gated, that mutations refuse restricted sessions, and the guard
edges (consent 409, confirm gate, upload cap, publish gate). This companion
covers what that file deliberately stops short of — the inside of each
handler once the guards pass: the listing/download/upload/delete/share bodies,
the cost fetch success and fallbacks, library push, the four backup verbs,
the IAM render, and the small shared helpers (`_safe_error`, `_aws_failed`,
`_audit`, `_body`, `_valid_section`, and the `account_unavailable` branch of
`_account_target`).

Every case asserts real behaviour — a status code, a response ``code`` field,
or whether a collaborator was called — not merely that a line executed.

Helpers (`_request`, `_payload`, `_enabled_owner_env`, ``ACCOUNT``) mirror the
conventions in ``test_aws_control_app.py`` so the two files build requests and
patch the environment identically; they are copied here because they are
module-private there and the two files must not edit each other.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import routes as routes_mod
from kiro_crew.deploy.engine import AWSError

BASE = "/api/apps/aws-control"
ACCOUNT = "111122223333"


def _registered() -> dict[tuple[str, str], object]:
    app = web.Application()
    routes_mod.register_routes(app)
    return {
        (route.method, str(route.resource.canonical)[len(BASE) :]): route.handler
        for route in app.router.routes()
        if str(route.resource.canonical).startswith(BASE) and route.method != "HEAD"
    }


def _request(
    method: str,
    path: str,
    *,
    owner: bool = True,
    app_claim: str = "",
    match_info: dict | None = None,
    headers: dict | None = None,
) -> web.Request:
    """A real (mocked) aiohttp request carrying dashboard-owner identity.

    ``is_owner_dashboard_request`` reads ``request.app["state"].owner_id`` and
    the middleware-set ``app``/``user`` keys, so a real Application with a
    state object is attached rather than a duck-typed stub.
    """
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id="owner-1")
    kwargs: dict = {"app": app}
    if match_info is not None:
        kwargs["match_info"] = match_info
    if headers is not None:
        kwargs["headers"] = headers
    req = make_mocked_request(method, f"{BASE}{path}", **kwargs)
    req["app"] = app_claim
    req["user"] = "owner-1" if owner else "someone-else"
    return req


def _payload(response: web.StreamResponse) -> dict:
    raw = response.body  # type: ignore[attr-defined]
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


def _enabled_owner_env():
    """App on, account resolvable, live probe resolving to the requested account.

    The stale-mapping guard re-verifies profile->account on every target
    resolution, so an unpatched probe would 409 every guarded test.
    """
    return (
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
    )


def _consent_ok():
    return mock.patch.object(routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True))


def _drive_found(name: str = "kirocrew-drive-abc"):
    return mock.patch.object(routes_mod.storage_mod, "find_drive", return_value=name)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_safe_error_runs_both_redaction_passes(self):
        # Every outbound error string must be scrubbed of BOTH credentials and
        # exfiltration URLs — a leaked AWS key or the base64 beacon payload in
        # AWS CLI stderr is exactly what this boundary strips before it reaches
        # a body. (The URL redactor keeps the bare host in its marker; the
        # secret is the query payload, which must be gone.)
        beacon_payload = "QUJDREVGR0hJSktMTU5PUFFS" * 3
        exc = AWSError(
            "failed: aws_secret_access_key=AKIAIOSFODNN7EXAMPLEKEYX via "
            "https://collector.example.net/c?d=" + beacon_payload
        )
        text = routes_mod._safe_error(exc)
        assert "AKIAIOSFODNN7EXAMPLEKEYX" not in text
        assert beacon_payload not in text
        # It is redacted, not passed through untouched.
        assert "[RE" in text or "redacted" in text.lower()

    def test_aws_failed_is_a_502_with_a_stable_code(self):
        resp = routes_mod._aws_failed(AWSError("boom"))
        assert resp.status == 502
        assert _payload(resp)["code"] == "aws_call_failed"

    def test_audit_swallows_a_failing_sel_backend(self):
        # The audit is best-effort: a broken SEL sink must never propagate into
        # the response path, so a raising backend is logged and swallowed.
        with mock.patch.object(routes_mod, "sel", side_effect=RuntimeError("no sel")):
            routes_mod._audit("op", "res", "denied")  # must not raise

    def test_body_returns_empty_dict_for_a_non_dict_json(self):
        # A JSON list is valid JSON but not a body shape the handlers accept;
        # it must read as {} so `.get(...)` defaults apply instead of crashing.
        req = _request("POST", f"/drive/{ACCOUNT}/delete", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value=["not", "a", "dict"])  # type: ignore[method-assign]
        assert asyncio.run(routes_mod._body(req)) == {}

    def test_body_returns_empty_dict_when_json_raises(self):
        req = _request("POST", f"/drive/{ACCOUNT}/delete", match_info={"account": ACCOUNT})
        req.json = AsyncMock(side_effect=ValueError("bad json"))  # type: ignore[method-assign]
        assert asyncio.run(routes_mod._body(req)) == {}

    def test_valid_section_rejects_an_unknown_section(self):
        req = _request(
            "GET", f"/drive/{ACCOUNT}/list?section=nope", match_info={"account": ACCOUNT}
        )
        result = routes_mod._valid_section(req)
        assert isinstance(result, web.Response)
        assert _payload(result)["code"] == "invalid_section"


# ---------------------------------------------------------------------------
# _account_target — the account_unavailable branch
# ---------------------------------------------------------------------------


class TestAccountTarget:
    def test_unresolvable_account_is_a_409_before_any_probe(self):
        # resolve_account_profile returning None means "no working connection":
        # the operation must refuse (409) and never reach the identity probe.
        handlers = _registered()
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.accounts_mod,
                "resolve_account_profile",
                AsyncMock(return_value=None),
            ),
            mock.patch.object(routes_mod.aws_consent, "probe_identity") as probe,
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}")](  # type: ignore[operator]
                    _request("GET", f"/drive/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert resp.status == 409
        assert _payload(resp)["code"] == "account_unavailable"
        probe.assert_not_called()


# ---------------------------------------------------------------------------
# Drive status — cache, no-bucket, usage-error branches
# ---------------------------------------------------------------------------


class TestDriveStatus:
    def test_no_bucket_reports_exists_false(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            mock.patch.object(routes_mod.storage_mod, "find_drive", return_value=None),
            mock.patch.object(routes_mod.storage_mod, "usage") as usage,
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}")](  # type: ignore[operator]
                    _request("GET", f"/drive/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert _payload(resp) == {"exists": False}
        usage.assert_not_called()

    def test_status_returns_bucket_and_usage_then_serves_cache(self):
        handlers = _registered()
        routes_mod._usage_cache.pop(ACCOUNT, None)
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "usage", return_value={"bytes": 42}) as usage,
        ):
            first = asyncio.run(
                handlers[("GET", "/drive/{account}")](  # type: ignore[operator]
                    _request("GET", f"/drive/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
            # A second call inside the TTL must be served from the cache and
            # must NOT re-query usage — the quiet-quadratic guard the module
            # note describes.
            second = asyncio.run(
                handlers[("GET", "/drive/{account}")](  # type: ignore[operator]
                    _request("GET", f"/drive/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(first)
        assert body["exists"] is True and body["bucket"] == "kirocrew-drive-abc"
        assert body["usage"] == {"bytes": 42}
        assert _payload(second)["usage"] == {"bytes": 42}
        usage.assert_called_once()
        routes_mod._usage_cache.pop(ACCOUNT, None)

    def test_status_surfaces_a_bucket_discovery_error_as_502(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", side_effect=AWSError("list denied")
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}")](  # type: ignore[operator]
                    _request("GET", f"/drive/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert resp.status == 502
        assert _payload(resp)["code"] == "aws_call_failed"

    def test_status_surfaces_a_usage_error_as_502(self):
        handlers = _registered()
        routes_mod._usage_cache.pop(ACCOUNT, None)
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(
                routes_mod.storage_mod, "usage", side_effect=AWSError("usage denied")
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}")](  # type: ignore[operator]
                    _request("GET", f"/drive/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert resp.status == 502


# ---------------------------------------------------------------------------
# _require_drive — the drive_missing branch, shared by every drive body
# ---------------------------------------------------------------------------


class TestRequireDrive:
    def test_list_refuses_when_no_drive_exists(self):
        # _require_drive backs list/download/upload/delete/share/push/backup —
        # an account with no bucket yet must 409 drive_missing, not proceed.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            mock.patch.object(routes_mod.storage_mod, "find_drive", return_value=None),
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}/list")](  # type: ignore[operator]
                    _request("GET", f"/drive/{ACCOUNT}/list", match_info={"account": ACCOUNT})
                )
            )
        assert resp.status == 409
        assert _payload(resp)["code"] == "drive_missing"

    def test_list_surfaces_a_discovery_error_as_502(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            mock.patch.object(routes_mod.storage_mod, "find_drive", side_effect=AWSError("boom")),
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}/list")](  # type: ignore[operator]
                    _request("GET", f"/drive/{ACCOUNT}/list", match_info={"account": ACCOUNT})
                )
            )
        assert resp.status == 502


# ---------------------------------------------------------------------------
# Drive list
# ---------------------------------------------------------------------------


class TestDriveList:
    def test_list_returns_a_page_for_a_valid_section(self):
        handlers = _registered()
        page = {"items": [{"key": "a.txt"}], "token": ""}
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "list_section", return_value=page) as listed,
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}/list")](  # type: ignore[operator]
                    _request(
                        "GET",
                        f"/drive/{ACCOUNT}/list?section=drive&path=sub&token=t",
                        match_info={"account": ACCOUNT},
                    )
                )
            )
        assert resp.status == 200
        assert _payload(resp) == page
        # subpath and token are threaded through to the storage call verbatim.
        assert listed.call_args.args[3:6] == ("drive", "sub", "t")

    def test_list_rejects_a_hostile_subpath(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "validate_key", return_value="bad path"),
            mock.patch.object(routes_mod.storage_mod, "list_section") as listed,
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}/list")](  # type: ignore[operator]
                    _request(
                        "GET",
                        f"/drive/{ACCOUNT}/list?path=../evil",
                        match_info={"account": ACCOUNT},
                    )
                )
            )
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_key"
        listed.assert_not_called()

    def test_list_surfaces_an_aws_error(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "list_section", side_effect=AWSError("nope")),
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}/list")](  # type: ignore[operator]
                    _request("GET", f"/drive/{ACCOUNT}/list", match_info={"account": ACCOUNT})
                )
            )
        assert resp.status == 502


# ---------------------------------------------------------------------------
# Drive download — invalid key + aws error (success/backup/publish/missing
# covered in test_aws_control_app.py)
# ---------------------------------------------------------------------------


class TestDriveDownload:
    def test_download_rejects_an_invalid_key(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod, "publish_denied_reason", return_value=""),
            mock.patch.object(routes_mod.storage_mod, "validate_key", return_value="bad key"),
            mock.patch.object(routes_mod.storage_mod, "presign") as presign,
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}/download")](  # type: ignore[operator]
                    _request(
                        "GET",
                        f"/drive/{ACCOUNT}/download?section=drive&key=bad",
                        match_info={"account": ACCOUNT},
                    )
                )
            )
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_key"
        presign.assert_not_called()

    def test_download_surfaces_an_aws_error_during_presign(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod, "publish_denied_reason", return_value=""),
            mock.patch.object(routes_mod.storage_mod, "validate_key", return_value=None),
            mock.patch.object(routes_mod.storage_mod, "object_exists", return_value=True),
            mock.patch.object(
                routes_mod.storage_mod, "presign", side_effect=AWSError("sign failed")
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}/download")](  # type: ignore[operator]
                    _request(
                        "GET",
                        f"/drive/{ACCOUNT}/download?section=drive&key=a.txt",
                        match_info={"account": ACCOUNT},
                    )
                )
            )
        assert resp.status == 502


# ---------------------------------------------------------------------------
# Drive upload — full body (streaming spool, empty, over-cap, recheck, put)
# ---------------------------------------------------------------------------


class _FakeContent:
    """Minimal stand-in for ``request.content`` yielding fixed chunks."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


class TestDriveUpload:
    def _run(self, chunks, *, key="f.bin", app_enabled_recheck=True, consent_recheck=True):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request(
            "POST",
            f"/drive/{ACCOUNT}/upload?section=drive&key={key}",
            match_info={"account": ACCOUNT},
        )
        req._fake_content = _FakeContent(chunks)  # type: ignore[attr-defined]

        enabled_seq = [True, app_enabled_recheck]

        def enabled(_name):
            return enabled_seq.pop(0) if enabled_seq else True

        with (
            mock.patch.object(type(req), "content", new=property(lambda s: s._fake_content)),
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod, "is_app_enabled", side_effect=enabled),
            mock.patch.object(
                routes_mod.aws_consent,
                "refuse_and_log",
                AsyncMock(return_value=consent_recheck),
            ),
            mock.patch.object(routes_mod.storage_mod, "put_file") as put,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/upload")](req)  # type: ignore[operator]
            )
        return resp, put

    def test_upload_streams_to_a_spool_and_puts_the_file(self):
        resp, put = self._run([b"hello ", b"world"])
        assert resp.status == 200
        body = _payload(resp)
        assert body["uploaded"] is True and body["bytes"] == 11 and body["key"] == "f.bin"
        put.assert_called_once()

    def test_a_connection_change_during_the_spool_refuses_the_write(self):
        # A 512 MB stream takes minutes. The old order re-checked only the LOCAL
        # decisions (app enabled, consent) and never re-resolved the identity, so
        # a profile repointed A -> B mid-spool had consent verified for B while
        # put_file still wrote into A's bucket -- reachable whenever B holds
        # cross-account access. Consent is asked ABOUT a profile, so verifying it
        # against a stale pair proves nothing about where the bytes land.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request(
            "POST",
            f"/drive/{ACCOUNT}/upload?section=drive&key=f.bin",
            match_info={"account": ACCOUNT},
        )
        req._fake_content = _FakeContent([b"hello"])  # type: ignore[attr-defined]

        # First resolve authorizes the request; the re-resolve after the spool
        # reports a DIFFERENT profile, as a mid-transfer repoint would.
        targets = [
            (ACCOUNT, "personal", "us-west-2"),
            (ACCOUNT, "other-profile", "us-west-2"),
        ]

        async def target(_req):
            return targets.pop(0) if targets else (ACCOUNT, "other-profile", "us-west-2")

        with (
            mock.patch.object(type(req), "content", new=property(lambda s: s._fake_content)),
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod, "_account_target", side_effect=target),
            mock.patch.object(routes_mod.storage_mod, "put_file") as put,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/upload")](req)  # type: ignore[operator]
            )

        assert resp.status == 409
        assert _payload(resp)["code"] == "account_mismatch"
        # The decisive assertion: nothing was written.
        put.assert_not_called()

    def test_an_unchanged_connection_still_uploads(self):
        # The re-resolve must not refuse the ordinary case: a stable triple writes.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request(
            "POST",
            f"/drive/{ACCOUNT}/upload?section=drive&key=f.bin",
            match_info={"account": ACCOUNT},
        )
        req._fake_content = _FakeContent([b"hello"])  # type: ignore[attr-defined]

        async def target(_req):
            return (ACCOUNT, "personal", "us-west-2")

        with (
            mock.patch.object(type(req), "content", new=property(lambda s: s._fake_content)),
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod, "_account_target", side_effect=target),
            mock.patch.object(routes_mod.storage_mod, "put_file") as put,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/upload")](req)  # type: ignore[operator]
            )

        assert resp.status == 200
        put.assert_called_once()

    def test_a_drive_retag_during_the_spool_refuses_the_write(self):
        # The drive bucket is tag-discovered, and a 512 MB spool is long enough
        # for the tags to move to a DIFFERENT bucket while the identity triple
        # stays the same. A name resolved before the spool is exactly the
        # staleness the module's no-cache rule forbids, so the post-spool
        # re-authorization re-resolves the drive and refuses on a mismatch --
        # otherwise put_file would land the object in the previously-discovered
        # bucket.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request(
            "POST",
            f"/drive/{ACCOUNT}/upload?section=drive&key=f.bin",
            match_info={"account": ACCOUNT},
        )
        req._fake_content = _FakeContent([b"hello"])  # type: ignore[attr-defined]
        with (
            mock.patch.object(type(req), "content", new=property(lambda s: s._fake_content)),
            p1,
            p2,
            p3,
            _consent_ok(),
            mock.patch.object(
                routes_mod.storage_mod,
                "find_drive",
                side_effect=["drive-before", "drive-after"],
            ),
            mock.patch.object(routes_mod, "_audit") as audit,
            mock.patch.object(routes_mod.storage_mod, "put_file") as put,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/upload")](req)  # type: ignore[operator]
            )
        assert resp.status == 409
        assert _payload(resp)["code"] == "drive_changed"
        # The decisive assertions: nothing was written, and the denial is a
        # permission DECISION so it must reach the audit trail.
        put.assert_not_called()
        audit.assert_any_call("drive_upload", mock.ANY, "denied", error="drive_changed")

    def test_the_put_targets_the_bucket_the_post_spool_discovery_returned(self):
        # A pass through the re-authorization means the pre-spool name and the
        # post-spool resolution AGREE, so the put's target is the post-wait
        # answer, never a name only the pre-spool lookup vouched for. The second
        # find_drive call is that re-resolve; without it the equality was never
        # checked and the write trusts a stale name.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request(
            "POST",
            f"/drive/{ACCOUNT}/upload?section=drive&key=f.bin",
            match_info={"account": ACCOUNT},
        )
        req._fake_content = _FakeContent([b"hello"])  # type: ignore[attr-defined]
        with (
            mock.patch.object(type(req), "content", new=property(lambda s: s._fake_content)),
            p1,
            p2,
            p3,
            _consent_ok(),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="drive-stable"
            ) as find,
            mock.patch.object(routes_mod.storage_mod, "put_file") as put,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/upload")](req)  # type: ignore[operator]
            )
        assert resp.status == 200
        assert find.call_count == 2
        put.assert_called_once()
        assert put.call_args.args[2] == "drive-stable"

    def test_consent_withdrawn_during_the_spool_refuses_the_write(self):
        # Way-in consent PASSES and the withdrawal lands during the spool, so
        # the refusal can only come from the post-spool re-check. The blanket
        # always-deny variant cannot tell the two gates apart: it refuses on
        # the way in and never reaches the spool.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request(
            "POST",
            f"/drive/{ACCOUNT}/upload?section=drive&key=f.bin",
            match_info={"account": ACCOUNT},
        )
        req._fake_content = _FakeContent([b"hello"])  # type: ignore[attr-defined]
        with (
            mock.patch.object(type(req), "content", new=property(lambda s: s._fake_content)),
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent,
                "refuse_and_log",
                AsyncMock(side_effect=[True, False]),
            ),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "put_file") as put,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/upload")](req)  # type: ignore[operator]
            )
        assert resp.status == 409
        assert _payload(resp)["code"] == "aws_consent_required"
        put.assert_not_called()

    def test_empty_upload_is_refused_and_never_put(self):
        resp, put = self._run([])
        assert resp.status == 400
        assert _payload(resp)["code"] == "empty_upload"
        put.assert_not_called()

    def test_upload_streamed_over_the_cap_is_refused(self):
        # No Content-Length header, so the header check passes and the streaming
        # counter is what stops it — a chunk pushing past the ceiling aborts.
        big = b"x" * (routes_mod._MAX_UPLOAD_BYTES + 1)
        resp, put = self._run([big])
        assert resp.status == 400
        assert _payload(resp)["code"] == "upload_too_large"
        put.assert_not_called()

    def test_upload_rejects_an_invalid_key_before_reading_the_body(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request(
            "POST",
            f"/drive/{ACCOUNT}/upload?section=drive&key=bad",
            match_info={"account": ACCOUNT},
        )
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "validate_key", return_value="bad key"),
            mock.patch.object(routes_mod.storage_mod, "put_file") as put,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/upload")](req)  # type: ignore[operator]
            )
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_key"
        put.assert_not_called()

    def test_upload_rechecks_app_enabled_after_the_transfer(self):
        # A multi-minute transfer can outlive the app being disabled; the
        # post-transfer recheck must refuse before the bytes hit S3.
        resp, put = self._run([b"data"], app_enabled_recheck=False)
        assert resp.status == 403
        assert _payload(resp)["code"] == "app_disabled"
        put.assert_not_called()

    def test_upload_rechecks_consent_after_the_transfer(self):
        # Consent can be revoked mid-transfer; the recheck refuses with 409.
        resp, put = self._run([b"data"], consent_recheck=False)
        assert resp.status == 409
        assert _payload(resp)["code"] == "aws_consent_required"
        put.assert_not_called()

    def test_upload_surfaces_an_aws_error_from_put(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request(
            "POST",
            f"/drive/{ACCOUNT}/upload?section=drive&key=f.bin",
            match_info={"account": ACCOUNT},
        )
        req._fake_content = _FakeContent([b"data"])  # type: ignore[attr-defined]
        with (
            mock.patch.object(type(req), "content", new=property(lambda s: s._fake_content)),
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(
                routes_mod.storage_mod, "put_file", side_effect=AWSError("put denied")
            ),
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/upload")](req)  # type: ignore[operator]
            )
        assert resp.status == 502


# ---------------------------------------------------------------------------
# Drive delete
# ---------------------------------------------------------------------------


class TestDriveDelete:
    def _delete(self, body: dict):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/drive/{ACCOUNT}/delete", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "delete_key") as delete,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/delete")](req)  # type: ignore[operator]
            )
        return resp, delete

    def test_delete_removes_the_object(self):
        resp, delete = self._delete({"section": "drive", "key": "a.txt"})
        assert resp.status == 200
        assert _payload(resp) == {"deleted": True, "key": "a.txt"}
        delete.assert_called_once()

    def test_delete_rejects_an_unknown_section(self):
        resp, delete = self._delete({"section": "nope", "key": "a.txt"})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_section"
        delete.assert_not_called()

    def test_delete_rejects_an_invalid_key(self):
        resp, delete = self._delete({"section": "drive", "key": "../etc/passwd"})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_key"
        delete.assert_not_called()

    def test_delete_surfaces_an_aws_error(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/drive/{ACCOUNT}/delete", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"section": "drive", "key": "a.txt"})  # type: ignore[method-assign]
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "delete_key", side_effect=AWSError("denied")),
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/delete")](req)  # type: ignore[operator]
            )
        assert resp.status == 502


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


# ---------------------------------------------------------------------------
# Shares list + forget
# ---------------------------------------------------------------------------


class TestSharesListForget:
    def test_shares_list_filters_by_account(self):
        handlers = _registered()
        entries = [{"id": "sh-1", "key": "a.txt"}]
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod.shares_mod, "list_shares", return_value=entries) as listed,
        ):
            resp = asyncio.run(
                handlers[("GET", "/shares")](  # type: ignore[operator]
                    _request("GET", f"/shares?account={ACCOUNT}")
                )
            )
        assert _payload(resp) == {"shares": entries}
        listed.assert_called_once_with(ACCOUNT)

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
        # No AWS call and no prune on a grant that no longer holds.
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
            await routes_mod._library_lock.acquire()
            try:
                return await handlers[("GET", "/library/{account}")](  # type: ignore[operator]
                    _request("GET", f"/library/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            finally:
                routes_mod._library_lock.release()

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
                    _request("GET", f"/backup/{ACCOUNT}", match_info={"account": ACCOUNT})
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
                    _request("GET", f"/backup/{ACCOUNT}", match_info={"account": ACCOUNT})
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
                    _request("GET", f"/backup/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert _payload(resp)["remote"] is None
        find.assert_not_called()

    def _run_backup(self, kind, *, runner_side=None, run_record=None):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/backup/{ACCOUNT}/run", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value={"kind": kind})  # type: ignore[method-assign]
        record = run_record if run_record is not None else {"key": "snapshots/x.tar.gz"}
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(
                routes_mod.backup_mod,
                "run_snapshot_backup",
                side_effect=runner_side,
                return_value=record,
            ),
            mock.patch.object(
                routes_mod.backup_mod,
                "run_sessions_backup",
                side_effect=runner_side,
                return_value=record,
            ),
        ):
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/run")](req)  # type: ignore[operator]
            )
        return resp

    def test_run_snapshot_backup_returns_the_run_record(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod

        resp = self._run_backup(backup_mod.KIND_SNAPSHOT)
        assert resp.status == 200
        body = _payload(resp)
        assert body["ran"] is True and body["kind"] == backup_mod.KIND_SNAPSHOT

    def test_run_sessions_backup_uses_the_sessions_runner(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod

        resp = self._run_backup(backup_mod.KIND_SESSIONS)
        assert resp.status == 200
        assert _payload(resp)["kind"] == backup_mod.KIND_SESSIONS

    def test_run_maps_a_runtime_error_to_a_409(self):
        # The backup builder raising RuntimeError (e.g. a teardown signal) is a
        # conflict, not an AWS failure — it must surface as 409 backup_failed.
        from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod

        resp = self._run_backup(backup_mod.KIND_SNAPSHOT, runner_side=RuntimeError("shutting down"))
        assert resp.status == 409
        assert _payload(resp)["code"] == "backup_failed"

    def test_run_surfaces_an_aws_error(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod

        resp = self._run_backup(backup_mod.KIND_SNAPSHOT, runner_side=AWSError("upload denied"))
        assert resp.status == 502

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
