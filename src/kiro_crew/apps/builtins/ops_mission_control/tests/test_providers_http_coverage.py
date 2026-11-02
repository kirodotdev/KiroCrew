"""``providers/http.py``: the request path that ``test_providers.py`` leaves unpinned.

``test_providers.py`` covers ``_parse_retry_after``, ``HttpError.is_retryable`` and one https
refusal. What stays open is ``request_json`` itself: how it builds the URL, body and headers, and
how each failure becomes an ``HttpError`` whose message cannot carry a credential.
``urllib.request.urlopen`` is replaced in every test, so nothing here opens a socket.
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from email.message import Message
from typing import Any
from unittest import mock

from kiro_crew.apps.builtins.ops_mission_control.backend.providers import HTTP_TIMEOUT_SECS, http
from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import REDACTED_PLACEHOLDER

URL = "https://api.example.invalid/v1/incidents"


def _response(body: bytes) -> mock.MagicMock:
    """What ``urlopen`` returns: a context manager whose ``read`` yields ``body``."""
    resp = mock.MagicMock()
    resp.__enter__.return_value = resp
    resp.read.return_value = body
    return resp


def _http_error(
    code: int, body: bytes = b"", retry_after: str | None = None
) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(URL, code, "status", headers, io.BytesIO(body))


def _raised(failure: BaseException) -> http.HttpError:
    """Run ``request_json`` against a ``urlopen`` that raises ``failure``; return the ``HttpError``."""
    with mock.patch.object(http.urllib.request, "urlopen", side_effect=failure):
        try:
            http.request_json(URL)
        except http.HttpError as exc:
            return exc
    raise AssertionError("request_json did not raise HttpError")


class RequestBuilding(unittest.TestCase):
    def _call(self, url: str = URL, **kwargs: Any) -> tuple[mock.MagicMock, mock.MagicMock]:
        resp = _response(b'{"ok": true}')
        with mock.patch.object(http.urllib.request, "urlopen", return_value=resp) as urlopen:
            self.assertEqual(http.request_json(url, **kwargs), {"ok": True})
        return urlopen, resp

    def test_params_follow_a_question_mark_and_list_values_repeat(self) -> None:
        urlopen, _ = self._call(params={"statuses[]": ["triggered", "acked"], "limit": 5})
        req = urlopen.call_args.args[0]
        self.assertEqual(
            req.full_url, f"{URL}?statuses%5B%5D=triggered&statuses%5B%5D=acked&limit=5"
        )

    def test_params_join_an_existing_query_with_an_ampersand(self) -> None:
        urlopen, _ = self._call(f"{URL}?team=core", params={"limit": 5})
        self.assertEqual(urlopen.call_args.args[0].full_url, f"{URL}?team=core&limit=5")

    def test_get_sends_caller_headers_and_no_body(self) -> None:
        urlopen, _ = self._call(headers={"Authorization": "Token x", "Accept": "text/json"})
        req = urlopen.call_args.args[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertIsNone(req.data)
        self.assertEqual(req.get_header("Authorization"), "Token x")
        self.assertEqual(req.get_header("Accept"), "text/json")  # the caller's Accept wins
        self.assertFalse(req.has_header("Content-type"))

    def test_body_is_sent_as_json_with_a_content_type(self) -> None:
        urlopen, _ = self._call(method="POST", body={"action": "ack", "n": 2})
        req = urlopen.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data), {"action": "ack", "n": 2})
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(req.get_header("Accept"), "application/json")

    def test_timeout_defaults_to_the_provider_timeout_and_can_be_overridden(self) -> None:
        urlopen, _ = self._call()
        self.assertEqual(urlopen.call_args.kwargs["timeout"], HTTP_TIMEOUT_SECS)
        urlopen, _ = self._call(timeout=3.5)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 3.5)

    def test_the_response_read_is_capped(self) -> None:
        _, resp = self._call()
        resp.read.assert_called_once_with(http.MAX_RESPONSE_BYTES)

    def test_non_https_urls_are_refused_before_any_request_is_made(self) -> None:
        for url in ("http://api.example.invalid/x", "file:///tmp/x", "api.example.invalid/x"):
            with (
                self.subTest(url=url),
                mock.patch.object(http.urllib.request, "urlopen") as urlopen,
            ):
                with self.assertRaises(http.HttpError) as ctx:
                    http.request_json(url)
                self.assertEqual(ctx.exception.status, 0)
                urlopen.assert_not_called()


class Failures(unittest.TestCase):
    def test_http_status_error_keeps_status_trimmed_body_and_retry_after(self) -> None:
        exc = _raised(_http_error(503, b"overloaded " + b"x" * 1000, retry_after="30"))
        self.assertEqual((exc.status, exc.retry_after, exc.is_retryable), (503, 30, True))
        self.assertTrue(str(exc).startswith("HTTP 503: overloaded x"))
        self.assertEqual(len(str(exc)), len("HTTP 503: ") + 400)

    def test_a_missing_or_garbled_retry_after_reads_as_zero(self) -> None:
        for header in (None, "soon"):
            with self.subTest(header=header):
                exc = _raised(_http_error(500, b"boom", retry_after=header))
                self.assertEqual((exc.status, exc.retry_after, exc.is_retryable), (500, 0, False))

    def test_a_credential_echoed_in_the_error_body_is_scrubbed(self) -> None:
        # 32 hex characters, built at run time so the source holds no key-shaped literal
        blob = "0123456789abcdef" * 2
        exc = _raised(_http_error(401, f"invalid key {blob}".encode()))
        self.assertNotIn(blob, str(exc))
        self.assertIn(REDACTED_PLACEHOLDER, str(exc))

    def test_an_unreadable_error_body_leaves_an_empty_detail(self) -> None:
        class _NoBody(urllib.error.HTTPError):
            def read(self, *args: Any) -> bytes:
                raise OSError("socket closed")

        exc = _raised(_NoBody(URL, 502, "bad gateway", Message(), io.BytesIO(b"")))
        self.assertEqual((exc.status, str(exc)), (502, "HTTP 502: "))

    def test_transport_failures_become_status_zero_errors(self) -> None:
        cases = [
            (urllib.error.URLError("connection refused"), "transport error: connection refused"),
            (TimeoutError("timed out"), "transport error: timed out"),
            (ConnectionResetError("reset by peer"), "transport error: reset by peer"),
        ]
        for failure, message in cases:
            with self.subTest(message=message):
                exc = _raised(failure)
                self.assertEqual((exc.status, str(exc)), (0, message))

    def test_an_empty_body_is_an_empty_object(self) -> None:
        with mock.patch.object(http.urllib.request, "urlopen", return_value=_response(b"")):
            self.assertEqual(http.request_json(URL), {})

    def test_a_body_that_is_not_json_is_a_status_zero_error(self) -> None:
        for raw in (b"<html>oops</html>", b"\xff\xfe\x00"):
            with self.subTest(raw=raw):
                with mock.patch.object(http.urllib.request, "urlopen", return_value=_response(raw)):
                    with self.assertRaises(http.HttpError) as ctx:
                        http.request_json(URL)
                self.assertEqual(ctx.exception.status, 0)
                self.assertTrue(str(ctx.exception).startswith("malformed JSON response:"))
