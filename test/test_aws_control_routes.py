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
conventions in ``aws_control_app_helpers.py`` so the two files build requests and
patch the environment identically; they are copied into
``aws_control_routes_helpers.py`` because they are module-private there and the
two files must not edit each other.

This file holds the shared helpers' own tests, the ``account_unavailable``
branch of `_account_target`, and the drive object bodies: status,
`_require_drive`, list, download, preview, search, upload, delete and move,
with the per-key write locks and the section sweep lock. The sibling
``test_aws_control_routes_*.py`` files hold the rest: folders and sharing,
costs and the library, and backup.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import sys
import threading
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
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
from kiro_crew.testing.wait import until_parked

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
            routes_mod._audit_sync("op", "res", "denied")  # must not raise

    def test_audit_routes_the_sel_write_off_the_event_loop(self):
        # The SEL write's first touch pays log construction, so the async
        # _audit wrapper must hand the sync writer to a worker thread instead
        # of running it inline on the loop — the regression this test locks
        # out. Observed from inside the writer itself (which thread ran it)
        # rather than by patching the stdlib asyncio module object, which
        # would leak the mock to unrelated code on other threads.
        assert asyncio.iscoroutinefunction(routes_mod._audit)
        seen: dict[str, object] = {}

        def _record(*args: object, **kwargs: object) -> None:
            seen["thread"] = threading.current_thread()
            seen["args"] = args
            seen["kwargs"] = kwargs

        with mock.patch.object(routes_mod, "_audit_sync", side_effect=_record):
            asyncio.run(routes_mod._audit("op", "res", "denied", error="why"))
        assert seen["args"] == ("op", "res", "denied")
        assert seen["kwargs"] == {"error": "why"}
        # asyncio.run drives the loop on the calling thread, so a writer that
        # ran on the loop would report this thread.
        assert seen["thread"] is not threading.current_thread()

    def test_audit_swallows_a_failing_thread_dispatch(self):
        # The wrapper keeps the sync body's never-raises contract even when
        # the dispatched call raises out of the worker (the real _audit_sync
        # swallows its own errors; this pins the wrapper's guard for anything
        # that escapes thread dispatch itself).
        with mock.patch.object(
            routes_mod, "_audit_sync", side_effect=RuntimeError("executor down")
        ):
            asyncio.run(routes_mod._audit("op", "res", "denied"))  # must not raise

    def test_every_audit_call_site_awaits_the_wrapper(self):
        # Wiring pin: with _audit patched as an AsyncMock, an UNAWAITED
        # `_audit(...)` still lands in call_args_list, so the behavioural
        # tests cannot detect a dropped `await` — the audit would silently
        # stop being recorded (only a RuntimeWarning). Pin the source instead:
        # every call site must `await` the wrapper, directly or through
        # `asyncio.shield` (the post-side-effect sites). Same style as
        # test_api_health.py::test_every_middleware_denial_is_audited_off_the_loop.
        # The facade and every owner it serves: the handlers live in
        # ``routes_parts`` and are reached through ``routes``.
        src = "\n".join(
            inspect.getsource(module)
            for module in (routes_mod, *(sys.modules[name] for name in routes_mod._OWNERS))
        )
        sites = [m for m in re.finditer(r"_audit\(", src) if not src[: m.start()].endswith("def ")]
        assert len(sites) >= 13, "expected the module's audit call sites to be present"
        for m in sites:
            before = src[: m.start()]
            line = src[src.rfind("\n", 0, m.start()) + 1 : src.find("\n", m.end())]
            # `await _audit(` or `await asyncio.shield(  _audit(` — black may
            # break the shield form across lines, so allow whitespace between.
            assert re.search(
                r"await\s+(asyncio\.shield\(\s*)?$", before
            ), f"_audit call site is not awaited: {line.strip()!r}"

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
# covered in test_aws_control_app.py and test_aws_control_app_hardening.py)
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
        resp = self._download(meta={}, presign=AWSError("sign failed"))
        assert resp.status == 502

    def _download(self, *, meta: object, presign: object = "https://signed/x"):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        meta_patch = (
            mock.patch.object(routes_mod.storage_mod, "head_object_meta", side_effect=meta)
            if isinstance(meta, Exception)
            else mock.patch.object(routes_mod.storage_mod, "head_object_meta", return_value=meta)
        )
        presign_patch = (
            mock.patch.object(routes_mod.storage_mod, "presign", side_effect=presign)
            if isinstance(presign, Exception)
            else mock.patch.object(routes_mod.storage_mod, "presign", return_value=presign)
        )
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod, "publish_denied_reason", return_value=""),
            mock.patch.object(routes_mod.storage_mod, "validate_key", return_value=None),
            meta_patch,
            presign_patch,
        ):
            return asyncio.run(
                handlers[("GET", "/drive/{account}/download")](  # type: ignore[operator]
                    _request(
                        "GET",
                        f"/drive/{ACCOUNT}/download?section=drive&key=a.txt",
                        match_info={"account": ACCOUNT},
                    )
                )
            )

    def test_download_carries_the_stored_content_type(self):
        # The preview tells a real PDF from a `.pdf`-named object served as
        # octet-stream by this field; the same HEAD the presign precondition
        # already makes is where it comes from, so no extra round trip.
        resp = self._download(meta={"ContentType": "application/pdf", "ContentLength": 5})
        assert resp.status == 200
        body = _payload(resp)
        assert body["url"] == "https://signed/x"
        assert body["contentType"] == "application/pdf"

    def test_download_reports_no_content_type_as_null_not_a_guess(self):
        resp = self._download(meta={})
        assert _payload(resp)["contentType"] is None

    def test_download_404s_a_missing_object_before_presigning(self):
        resp = self._download(meta=None)
        assert resp.status == 404
        assert _payload(resp)["code"] == "object_missing"


# ---------------------------------------------------------------------------
# Drive preview — the gateway-proxied text read (missing object, decode,
# truncation, invalid key)
# ---------------------------------------------------------------------------


class TestDrivePreview:
    def _call(self, *, exists: object = True, head: object = (b"hello", 5), key: str = "a.txt"):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        exists_patch = (
            mock.patch.object(routes_mod.storage_mod, "object_exists", side_effect=exists)
            if isinstance(exists, Exception)
            else mock.patch.object(routes_mod.storage_mod, "object_exists", return_value=exists)
        )
        head_patch = (
            mock.patch.object(routes_mod.storage_mod, "get_object_head_bytes", side_effect=head)
            if isinstance(head, Exception)
            else mock.patch.object(
                routes_mod.storage_mod, "get_object_head_bytes", return_value=head
            )
        )
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            exists_patch,
            head_patch as headed,
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}/preview")](  # type: ignore[operator]
                    _request(
                        "GET",
                        f"/drive/{ACCOUNT}/preview?section=drive&key={key}",
                        match_info={"account": ACCOUNT},
                    )
                )
            )
        return resp, headed

    def test_preview_404s_a_missing_object(self):
        # The presign lesson applies to the proxy read too: a typo'd key must
        # answer 404, not surface as an opaque transfer failure.
        resp, headed = self._call(exists=False)
        assert resp.status == 404
        assert _payload(resp)["code"] == "object_missing"
        headed.assert_not_called()

    def test_preview_returns_the_decoded_head(self):
        resp, headed = self._call(head=(b"hello", 5))
        assert resp.status == 200
        assert _payload(resp) == {"content": "hello", "truncated": False, "redacted": False}
        # The window is the module constant, not a caller-tunable.
        # The window plus the redaction look-ahead, both module constants, not
        # caller-tunables.
        assert headed.call_args.kwargs["max_bytes"] == (
            routes_mod._PREVIEW_MAX_BYTES + routes_mod._PREVIEW_REDACT_LOOKAHEAD
        )

    def test_preview_reports_truncation_from_the_full_size(self):
        # truncated must come from the OBJECT's size against the WINDOW, not
        # from how many bytes came back (the look-ahead makes those differ) —
        # the frontend's "showing only the head" hint hangs off this bit.
        resp, _ = self._call(head=(b"head", routes_mod._PREVIEW_MAX_BYTES + 1))
        assert resp.status == 200
        assert _payload(resp) == {"content": "head", "truncated": True, "redacted": False}

    def test_preview_reports_no_truncation_for_an_object_inside_the_window(self):
        resp, _ = self._call(head=(b"head", 100))
        assert _payload(resp)["truncated"] is False

    def test_a_secret_straddling_the_window_never_reaches_the_browser(self):
        # The boundary case the look-ahead exists for: an access key that
        # begins inside the window and ends past it. Without the look-ahead
        # the redactor sees only a prefix it cannot recognise, and 19 of the
        # key's 20 characters ship. With it the key is masked whole, and the
        # trim then backs off to whitespace so no split run is shown either.
        window = 32
        head = b"line one\nkey = "  # 15 bytes
        secret = b"AKIAIOSFODNN7EXAMPLE"  # 20 bytes: straddles byte 32
        tail = b"\nline three\n"
        data = head + secret + tail
        with (
            mock.patch.object(routes_mod, "_PREVIEW_MAX_BYTES", window),
            mock.patch.object(routes_mod, "_PREVIEW_REDACT_LOOKAHEAD", 64),
            mock.patch.object(routes_mod, "_PREVIEW_TRIM_SEARCH", 64),
        ):
            resp, headed = self._call(head=(data, len(data) + 1000))
        body = _payload(resp)
        assert headed.call_args.kwargs["max_bytes"] == window + 64
        assert "AKIAIOSFODNN7EXAMPLE" not in body["content"]
        assert "AKIAIOSFODNN" not in body["content"]
        assert body["truncated"] is True
        assert body["redacted"] is True
        assert body["content"].startswith("line one\n")

    def test_a_whitespace_free_blob_still_previews(self):
        # The whitespace back-off is bounded: a minified blob has no boundary
        # to back off to, and trimming it to nothing would hide the file.
        window = 32
        data = b"x" * 200
        with (
            mock.patch.object(routes_mod, "_PREVIEW_MAX_BYTES", window),
            mock.patch.object(routes_mod, "_PREVIEW_REDACT_LOOKAHEAD", 16),
            mock.patch.object(routes_mod, "_PREVIEW_TRIM_SEARCH", 8),
        ):
            resp, _ = self._call(head=(data[: window + 16], len(data)))
        body = _payload(resp)
        assert body["content"] == "x" * window
        assert body["truncated"] is True

    def test_the_window_is_a_byte_budget_for_multibyte_text_too(self):
        # 3-byte code points: a CHARACTER count of `window` would ship three
        # times the budget, carrying the whole look-ahead past the window. The
        # cut lands on a code-point boundary (32 is not a multiple of 3), and
        # the split code point is dropped rather than shown as a glyph.
        window = 32
        data = ("中" * 200).encode("utf-8")
        with (
            mock.patch.object(routes_mod, "_PREVIEW_MAX_BYTES", window),
            mock.patch.object(routes_mod, "_PREVIEW_REDACT_LOOKAHEAD", 16),
            mock.patch.object(routes_mod, "_PREVIEW_TRIM_SEARCH", 8),
        ):
            resp, _ = self._call(head=(data[: window + 16], len(data)))
        body = _payload(resp)
        assert body["content"] == "中" * (window // 3)
        assert len(body["content"].encode("utf-8")) <= window
        assert body["truncated"] is True

    def test_preview_survives_bytes_that_are_not_utf8(self):
        # The frontend gates by extension, but nothing stops a .txt holding a
        # stray byte; one bad byte must degrade, not fail the whole preview.
        resp, _ = self._call(head=(b"\xff\xfegood", 6))
        assert resp.status == 200
        body = _payload(resp)
        assert body["content"].endswith("good")
        assert "\ufffd" in body["content"]

    def test_preview_redacts_credentials_like_every_other_egress(self):
        # A notes file that happens to hold an access key must render masked,
        # the same way listing and search names do -- the preview is a new
        # egress path and inherits the same floor.
        secret = b"notes\naws_access_key_id = AKIAIOSFODNN7EXAMPLE\nmore notes\n"
        resp, _ = self._call(head=(secret, len(secret)))
        assert resp.status == 200
        body = _payload(resp)
        content = body["content"]
        assert "AKIAIOSFODNN7EXAMPLE" not in content
        assert "REDACTED" in content
        assert content.startswith("notes\n")
        assert content.endswith("more notes\n")
        # The reader is TOLD the mask fired: without the bit, a masked value
        # reads as the file's actual bytes.
        assert body["redacted"] is True

    def test_preview_reports_a_staging_refusal_as_a_coded_500(self):
        # A link squatting the staging root (ValueError) or a local filesystem
        # failure (OSError) is neither an AWS failure nor the caller's doing;
        # it must come back as a coded body, never an uncoded crash -- and the
        # exception text (which carries the staging path) stays out of it.
        for exc in (ValueError("staging root is not a real directory"), OSError(13, "denied")):
            resp, _ = self._call(head=exc)
            assert resp.status == 500
            body = _payload(resp)
            assert body["code"] == "preview_staging_failed"
            assert "staging root" not in body["error"]
            assert "denied" not in body["error"]

    def test_preview_rejects_an_invalid_key(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "validate_key", return_value="bad key"),
            mock.patch.object(routes_mod.storage_mod, "get_object_head_bytes") as headed,
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}/preview")](  # type: ignore[operator]
                    _request(
                        "GET",
                        f"/drive/{ACCOUNT}/preview?section=drive&key=bad",
                        match_info={"account": ACCOUNT},
                    )
                )
            )
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_key"
        headed.assert_not_called()


# ---------------------------------------------------------------------------
# Drive search — the filename search body (empty query, success shape, error)
# ---------------------------------------------------------------------------


class TestDriveSearch:
    def _call(self, query_string: str, search: object = ([], False)):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        search_patch = (
            mock.patch.object(routes_mod.storage_mod, "search_keys", side_effect=search)
            if isinstance(search, Exception)
            else mock.patch.object(routes_mod.storage_mod, "search_keys", return_value=search)
        )
        with p1, p2, p3, _consent_ok(), _drive_found(), search_patch as searched:
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}/search")](  # type: ignore[operator]
                    _request(
                        "GET",
                        f"/drive/{ACCOUNT}/search?{query_string}",
                        match_info={"account": ACCOUNT},
                    )
                )
            )
        return resp, searched

    def test_search_requires_a_non_empty_query(self):
        # Whitespace-only is empty: an unfiltered walk of the whole section is
        # never what a blank search box meant.
        resp, searched = self._call("section=drive&q=%20%20")
        assert resp.status == 400
        assert _payload(resp)["code"] == "empty_query"
        searched.assert_not_called()

    def test_search_returns_results_and_the_capped_flag(self):
        hit = {"key": "notes/a.txt", "size": 7, "modified": "2026-01-01T00:00:00+00:00"}
        resp, searched = self._call("section=drive&q=notes", search=([hit], True))
        assert resp.status == 200
        assert _payload(resp) == {
            "results": [hit],
            "capped": True,
            "limit": routes_mod.storage_mod.SEARCH_MAX_RESULTS,
        }
        # The trimmed query is what reaches storage.
        assert searched.call_args.args[4] == "notes"

    def test_search_rejects_an_unknown_section(self):
        resp, searched = self._call("section=nope&q=x")
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_section"
        searched.assert_not_called()

    @pytest.mark.parametrize("section", ["library", "backup"])
    def test_search_is_scoped_to_the_file_drive(self, section):
        # A VALID section that is not the drive is still refused: the library
        # and backups have their own listing surfaces, and a search reaching
        # into backup archive keys would surface names the dashboard never
        # otherwise renders. Distinct code from invalid_section so the client
        # can tell "no such section" from "not searchable".
        resp, searched = self._call(f"section={section}&q=x")
        assert resp.status == 400
        assert _payload(resp)["code"] == "section_not_searchable"
        searched.assert_not_called()

    def test_search_surfaces_an_aws_error(self):
        resp, _ = self._call("section=drive&q=x", search=AWSError("nope"))
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
        # otherwise put_file would land the object in the already-discovered
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
# Drive move — copy-then-delete, no silent overwrite, drive section only
# ---------------------------------------------------------------------------


class TestDriveMove:
    def _move(self, body: dict, *, exists=(True, False), copy_err=None):
        """Run the move handler with src/dest existence and copy outcome faked.

        ``exists`` feeds the two ``object_exists`` probes in call order
        (source first, then destination). Both storage mocks are attached to
        one parent so a test can assert the copy-before-delete ORDER, not just
        that both were called.
        """
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/drive/{ACCOUNT}/move", match_info={"account": ACCOUNT})
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
        parent = mock.Mock()
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(
                routes_mod.storage_mod, "object_exists", side_effect=list(exists)
            ) as head,
            mock.patch.object(routes_mod.storage_mod, "copy_object", side_effect=copy_err) as copy,
            mock.patch.object(routes_mod.storage_mod, "delete_key") as delete,
        ):
            parent.attach_mock(copy, "copy_object")
            parent.attach_mock(delete, "delete_key")
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/move")](req)  # type: ignore[operator]
            )
        return resp, parent, head

    def test_move_copies_before_deleting(self):
        # The order is the safety property: a delete issued before the copy
        # succeeded turns a failed move into data loss.
        resp, parent, _head = self._move(
            {"section": "drive", "fromKey": "a.txt", "toKey": "b/a.txt"}
        )
        assert resp.status == 200
        assert _payload(resp) == {"moved": True}
        names = [name for name, _args, _kwargs in parent.mock_calls]
        assert names == ["copy_object", "delete_key"]

    def test_move_rejects_an_invalid_key(self):
        # validate_key runs on BOTH keys BEFORE any AWS call reaches storage.
        resp, parent, head = self._move(
            {"section": "drive", "fromKey": "../etc/passwd", "toKey": "b.txt"}
        )
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_key"
        assert parent.mock_calls == []
        head.assert_not_called()

    def test_move_rejects_an_empty_destination(self):
        resp, parent, head = self._move({"section": "drive", "fromKey": "a.txt", "toKey": ""})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_key"
        assert parent.mock_calls == []
        head.assert_not_called()

    def test_move_rejects_equal_keys(self):
        # A same-key move would head the destination (which exists — it is the
        # source) and answer a confusing 409; refuse it as the no-op it is.
        resp, parent, head = self._move({"section": "drive", "fromKey": "a.txt", "toKey": "a.txt"})
        assert resp.status == 400
        assert _payload(resp)["code"] == "same_key"
        assert parent.mock_calls == []
        head.assert_not_called()

    def test_move_rejects_a_non_drive_section(self):
        # library/backup are managed surfaces whose objects carry ledger state;
        # a move from here would orphan it. Known-but-refused answers the same
        # 400 an unknown section does.
        for section in ("library", "backup", "nope"):
            resp, parent, head = self._move(
                {"section": section, "fromKey": "a.txt", "toKey": "b.txt"}
            )
            assert resp.status == 400
            assert _payload(resp)["code"] == "invalid_section"
            assert parent.mock_calls == []
            head.assert_not_called()

    def test_move_missing_source_answers_404(self):
        resp, parent, _head = self._move(
            {"section": "drive", "fromKey": "gone.txt", "toKey": "b.txt"},
            exists=(False,),
        )
        assert resp.status == 404
        assert _payload(resp)["code"] == "object_missing"
        assert parent.mock_calls == []

    def test_move_existing_destination_answers_409_and_deletes_nothing(self):
        # NEVER silently overwrite: an occupied destination refuses before the
        # copy, and no delete is issued against either key.
        resp, parent, _head = self._move(
            {"section": "drive", "fromKey": "a.txt", "toKey": "b.txt"},
            exists=(True, True),
        )
        assert resp.status == 409
        assert _payload(resp)["code"] == "destination_exists"
        assert parent.mock_calls == []

    def test_move_copy_failure_issues_no_delete(self):
        # A failed copy must leave the source untouched — the delete is what
        # turns "copy failed" into "file lost".
        resp, parent, _head = self._move(
            {"section": "drive", "fromKey": "a.txt", "toKey": "b.txt"},
            copy_err=AWSError("denied"),
        )
        assert resp.status == 502
        names = [name for name, _args, _kwargs in parent.mock_calls]
        assert names == ["copy_object"]

    def test_move_of_a_shared_source_is_refused(self):
        # A presigned share URL is bound to the SOURCE key: copy+delete would
        # leave it 404ing while the Access ledger reports it live until
        # expiry. The move must refuse (409 share_active) with zero storage
        # calls — the URL cannot be re-pointed, so refusal is the only honest
        # answer.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/drive/{ACCOUNT}/move", match_info={"account": ACCOUNT})
        req.json = AsyncMock(  # type: ignore[method-assign]
            return_value={"section": "drive", "fromKey": "a.txt", "toKey": "b.txt"}
        )
        live = [{"account": ACCOUNT, "section": "drive", "key": "a.txt"}]
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.shares_mod, "list_shares", return_value=live),
            mock.patch.object(routes_mod.storage_mod, "object_exists") as head,
            mock.patch.object(routes_mod.storage_mod, "copy_object") as copy,
            mock.patch.object(routes_mod.storage_mod, "delete_key") as delete,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/move")](req)  # type: ignore[operator]
            )
        assert resp.status == 409
        assert _payload(resp)["code"] == "share_active"
        assert not head.called and not copy.called and not delete.called

    def test_move_destination_probe_failure_fails_closed(self):
        # object_exists raises on anything S3 did not answer 404 to. If the
        # DESTINATION probe fails transiently, the move must answer 502 and
        # touch nothing — reading the failure as "absent" would copy over the
        # destination and delete the source.
        resp, parent, _head = self._move(
            {"section": "drive", "fromKey": "a.txt", "toKey": "b.txt"},
            exists=(True, AWSError("head-object failed")),
        )
        assert resp.status == 502
        assert parent.mock_calls == []

    def test_move_reauthorizes_inside_the_lock_before_any_storage_call(self):
        # The lock wait can queue a move for minutes behind an upload; consent
        # withdrawn during that wait must refuse the queued mutation. The
        # re-authorization runs AFTER lock acquisition and BEFORE any probe.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/drive/{ACCOUNT}/move", match_info={"account": ACCOUNT})
        req.json = AsyncMock(  # type: ignore[method-assign]
            return_value={"section": "drive", "fromKey": "a.txt", "toKey": "b.txt"}
        )
        deny = web.json_response({"error": "consent withdrawn"}, status=403)
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(
                routes_mod, "_reauthorize_in_lock", AsyncMock(return_value=deny)
            ) as reauth,
            mock.patch.object(routes_mod.storage_mod, "object_exists") as head,
            mock.patch.object(routes_mod.storage_mod, "copy_object") as copy,
            mock.patch.object(routes_mod.storage_mod, "delete_key") as delete,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/move")](req)  # type: ignore[operator]
            )
        assert resp.status == 403
        assert reauth.await_count == 1
        assert not head.called and not copy.called and not delete.called


# ---------------------------------------------------------------------------
# Drive per-key write locks — the guard that makes "never overwrites" true
# against this gateway's own concurrent writers (S3 CopyObject has no
# destination precondition, so ordering is the only enforcement available).
# ---------------------------------------------------------------------------


class TestDriveKeyLocks:
    def test_same_key_serializes_and_registry_empties(self):
        # Two holders of one key run strictly one-after-the-other, and the
        # registry holds no entries once both released — boundedness comes
        # from refcounting, so a leak here would grow with drive history.
        async def run() -> list[str]:
            order: list[str] = []

            async def hold(tag: str, gate: asyncio.Event | None) -> None:
                async with routes_mod._locked_drive_keys("bkt", "drive", "a.txt"):
                    order.append(f"{tag}-in")
                    if gate:
                        await gate.wait()
                    order.append(f"{tag}-out")

            gate = asyncio.Event()
            first = asyncio.create_task(hold("first", gate))
            await asyncio.sleep(0)  # first acquires
            second = asyncio.create_task(hold("second", None))
            await asyncio.sleep(0)  # second must be parked, not interleaved
            assert order == ["first-in"]
            gate.set()
            await asyncio.gather(first, second)
            return order

        order = asyncio.run(run())
        assert order == ["first-in", "first-out", "second-in", "second-out"]
        assert routes_mod._drive_key_locks == {}
        assert routes_mod._drive_key_lock_refs == {}

    def test_distinct_keys_do_not_contend(self):
        # The lock is per key by design: a 512 MB upload may hold its key for
        # minutes, and an unrelated move must not queue behind it.
        async def run() -> bool:
            async with routes_mod._locked_drive_keys("bkt", "drive", "a.txt"):
                entered = False
                async with routes_mod._locked_drive_keys("bkt", "drive", "b.txt"):
                    entered = True
                return entered

        assert asyncio.run(run()) is True
        assert routes_mod._drive_key_locks == {}

    def test_move_probe_waits_for_a_held_destination_key(self):
        # The defect this guards: an upload landing between the move's
        # destination probe and its copy was overwritten and the source then
        # deleted. With the upload's put holding the destination key, the
        # move's FIRST storage call must not happen until the hold releases —
        # probe-after-release then reads the uploaded object and answers 409.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/drive/{ACCOUNT}/move", match_info={"account": ACCOUNT})
        req.json = AsyncMock(  # type: ignore[method-assign]
            return_value={"section": "drive", "fromKey": "a.txt", "toKey": "b.txt"}
        )

        async def run() -> tuple[int, list[str]]:
            calls: list[str] = []

            def probe(*_a, **kwargs) -> bool:
                calls.append("probe")
                return True  # source exists; destination exists -> 409

            with (
                p1,
                p2,
                p3,
                _consent_ok(),
                _drive_found(),
                mock.patch.object(routes_mod.storage_mod, "object_exists", side_effect=probe),
                mock.patch.object(routes_mod.storage_mod, "copy_object") as copy,
            ):
                bucket = "kirocrew-drive-abc"  # the name _drive_found answers
                async with routes_mod._locked_drive_keys(bucket, "drive", "b.txt"):
                    task = asyncio.create_task(
                        handlers[("POST", "/drive/{account}/move")](req)  # type: ignore[operator]
                    )
                    # The handler is observed parked on the held destination
                    # key before the absence is asserted: a probe before the
                    # lock would already be in ``calls`` by then, so the
                    # empty-calls assertion cannot pass vacuously.
                    await until_parked(routes_mod._drive_key_locks[f"{bucket}\ndrive\nb.txt"])
                    assert calls == []
                resp = await task
                assert not copy.called
            return resp.status, calls

        status, calls = asyncio.run(run())
        assert status == 409
        assert calls == ["probe", "probe"]


class TestDriveSectionSweepLock:
    def test_shared_holders_run_concurrently_and_sweep_excludes_them(self):
        # Two key-scoped mutations on DIFFERENT keys overlap (shared holds);
        # a sweep waits for both, then runs alone; a key op arriving while
        # the sweep waits queues BEHIND it (writer preference — a stream of
        # uploads cannot starve a folder delete).
        async def run() -> list[str]:
            order: list[str] = []
            gate = asyncio.Event()

            async def key_op(tag: str, wait: bool) -> None:
                async with routes_mod._locked_drive_write("bkt", "drive", tag):
                    order.append(f"{tag}-in")
                    if wait:
                        await gate.wait()
                    order.append(f"{tag}-out")

            async def sweep() -> None:
                async with routes_mod._locked_drive_sweep("bkt", "drive"):
                    order.append("sweep")

            first = asyncio.create_task(key_op("a", True))
            await asyncio.sleep(0)
            second = asyncio.create_task(key_op("b", True))
            await asyncio.sleep(0)
            assert order == ["a-in", "b-in"]  # shared: both inside at once
            sweeper = asyncio.create_task(sweep())
            await asyncio.sleep(0)
            third = asyncio.create_task(key_op("c", False))
            for _ in range(5):
                await asyncio.sleep(0)
            # Sweep is parked behind a+b; c is parked behind the WAITING sweep.
            assert "sweep" not in order and "c-in" not in order
            gate.set()
            await asyncio.gather(first, second, sweeper, third)
            return order

        order = asyncio.run(run())
        assert order.index("sweep") > order.index("a-out")
        assert order.index("sweep") > order.index("b-out")
        assert order.index("c-in") > order.index("sweep")

    def test_folder_sweep_makes_no_storage_call_while_a_key_op_is_in_flight(self):
        # The round-5 defect: a sweep landing between a move's copy and its
        # source delete removes the fresh destination and the move then
        # deletes the source — the file is lost at both ends. The sweep must
        # not touch storage until in-flight key-scoped mutations drain.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request("POST", f"/drive/{ACCOUNT}/folder/delete", match_info={"account": ACCOUNT})
        req.json = AsyncMock(  # type: ignore[method-assign]
            return_value={"section": "drive", "path": "docs"}
        )

        async def run() -> int:
            with (
                p1,
                p2,
                p3,
                _consent_ok(),
                _drive_found(),
                mock.patch.object(routes_mod.storage_mod, "delete_prefix", return_value=3) as sweep,
            ):
                bucket = "kirocrew-drive-abc"
                async with routes_mod._locked_drive_write(bucket, "drive", "docs/x.txt"):
                    task = asyncio.create_task(
                        handlers[("POST", "/drive/{account}/folder/delete")](req)  # type: ignore[operator]
                    )
                    # The sweep is observed parked on the section's exclusive
                    # wait before the absence is asserted.
                    section = routes_mod._drive_section_lock(bucket, "drive")
                    await until_parked(section._state()["cond"])
                    assert not sweep.called
                resp = await task
                assert sweep.called
            return resp.status

        assert asyncio.run(run()) == 200
