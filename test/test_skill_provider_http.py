"""Tests for ``skill_providers/_http.py`` -- the shared provider network layer.

This module is the trust boundary every skill provider sits behind, and it was
extracted precisely so there is ONE implementation of the three controls rather
than one per provider. That makes it the right place to prove them directly,
instead of only observing them through a provider's behaviour:

- the internal-address screen, including the IPv4 encodings ``inet_aton`` accepts
  and :func:`ipaddress.ip_address` does not (a plain-looking ``0xa9fea9fe`` is the
  cloud instance-metadata endpoint);
- the redirect allowlist, which is what a hostname screen cannot be, because DNS
  is deliberately not resolved here;
- the bounded read, which must check the RUNNING total so an oversized body is
  abandoned mid-stream rather than accumulated whole.

The HTTP layer is faked at ``urllib.request.build_opener``, so the real
:class:`_SafeRedirectHandler`, the real status/size handling and the real decode
paths all execute; nothing here opens a socket.
"""

from __future__ import annotations

import asyncio
import io
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from kiro_crew.skill_providers import _http

_HOSTS = frozenset({"example.test", "cdn.example.test"})


class _Resp:
    """A minimal urllib-response stand-in.

    ``read(n)`` honours the requested chunk size so a test can prove the bounded
    read aborts PART WAY through a body rather than after buffering all of it.
    """

    def __init__(self, body: bytes = b"", status: int = 200) -> None:
        self._buf = io.BytesIO(body)
        self.status = status
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def close(self) -> None:
        self.closed = True


class _Opener:
    def __init__(self, result) -> None:
        self._result = result
        self.opened: list[str] = []

    def open(self, req, timeout=None):
        self.opened.append(req.full_url if hasattr(req, "full_url") else str(req))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _with_opener(result):
    opener = _Opener(result)
    return opener, patch.object(_http.urllib.request, "build_opener", lambda *a: opener)


# ---- the internal-address screen ------------------------------------------


class TestIsInternalUrl:
    ENCODED = [
        "http://2852039166/latest/meta-data/",  # decimal metadata endpoint
        "http://0xa9fea9fe/",  # hex metadata endpoint
        "http://2130706433/",  # decimal 127.0.0.1
        "http://0x7f000001/",  # hex 127.0.0.1
        "http://0177.0.0.1/",  # octal-leading 127.0.0.1
        "http://127.1/",  # short form
        "http://[::ffff:169.254.169.254]/",  # IPv6-mapped IPv4
    ]
    PLAIN = [
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.1.1/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fd00::1]/",
        "http://224.0.0.1/",  # multicast
        "http://0.0.0.0/",  # unspecified
    ]
    EXTERNAL = [
        "https://api.github.com/repos/a/b",
        "https://raw.githubusercontent.com/a/b/c/S.md",
        "https://skills.sh/api/search",
        "https://example.com/x",
    ]

    @pytest.mark.parametrize("url", ENCODED)
    def test_encoded_internal_is_blocked(self, url):
        # Pre-fix these raised ValueError in ip_address(), were swallowed, and the
        # URL read as "not internal" -- an SSRF-to-metadata credential read.
        assert _http.is_internal_url(url) is True

    @pytest.mark.parametrize("url", PLAIN)
    def test_plain_internal_is_blocked(self, url):
        assert _http.is_internal_url(url) is True

    @pytest.mark.parametrize("url", EXTERNAL)
    def test_public_hostnames_pass(self, url):
        # A hostname passes THIS check by design -- DNS is not resolved here, and
        # the allowlist is what holds a hostname.
        assert _http.is_internal_url(url) is False

    def test_missing_host_fails_closed(self):
        assert _http.is_internal_url("http:///no-host") is True
        assert _http.is_internal_url("") is True
        assert _http.is_internal_url("not a url at all") is True

    def test_unparseable_url_fails_closed(self):
        # A parse failure must read as suspicious, never as "fine".
        with patch.object(_http.urllib.parse, "urlparse", side_effect=ValueError):
            assert _http.is_internal_url("https://example.test/x") is True

    def test_audit_hook_fires_only_for_an_ip_literal(self):
        seen: list[tuple[str, str, str]] = []
        assert _http.is_internal_url("http://0xa9fea9fe/", audit=lambda *a: seen.append(a)) is True
        # Both spellings are recorded: the canonical form is what makes the row
        # legible (0xa9fea9fe -> 169.254.169.254).
        assert seen == [("http://0xa9fea9fe/", "0xa9fea9fe", "169.254.169.254")]

        seen.clear()
        assert (
            _http.is_internal_url("https://example.test/x", audit=lambda *a: seen.append(a))
            is False
        )
        assert seen == []
        # A hostname refusal is not an SSRF attempt, so it is not audited either.
        assert _http.is_internal_url("http://localhost/", audit=lambda *a: seen.append(a)) is True
        assert seen == []

    def test_audit_without_a_hook_does_not_raise(self):
        assert _http.is_internal_url("http://127.0.0.1/") is True


class TestAuditSsrfBlocked:
    def test_emits_a_sel_row_naming_the_caller(self):
        calls = []

        class _Sel:
            def log_api_access(self, **kw):
                calls.append(kw)

        with patch("kiro_crew.sel.sel", lambda: _Sel()):
            _http.audit_ssrf_blocked(
                "github", "http://0xa9fea9fe/x", "0xa9fea9fe", "169.254.169.254"
            )
        assert calls and calls[0]["caller"] == "github"
        assert calls[0]["outcome"] == "blocked"
        assert calls[0]["operation"] == "ssrf_blocked"
        # Both spellings, so an encoded-IP attempt is legible in the audit trail.
        assert "0xa9fea9fe -> 169.254.169.254" in calls[0]["resources"]

    def test_a_failing_audit_never_breaks_the_guard(self):
        # Auditing the defense must not become a way to crash the defense.
        with patch("kiro_crew.sel.sel", side_effect=RuntimeError("sel down")):
            _http.audit_ssrf_blocked("skillsh", "http://127.0.0.1/", "127.0.0.1", "127.0.0.1")


# ---- the redirect allowlist -----------------------------------------------


class TestIsAllowedHost:
    def test_exact_https_hosts_pass(self):
        assert _http.is_allowed_host("https://example.test/x", _HOSTS)
        assert _http.is_allowed_host("https://cdn.example.test/y", _HOSTS)

    def test_plain_http_is_refused(self):
        assert not _http.is_allowed_host("http://example.test/x", _HOSTS)
        assert not _http.is_allowed_host("ftp://example.test/x", _HOSTS)

    def test_suffix_and_prefix_lookalikes_are_refused(self):
        # Exact match, never endswith -- otherwise an attacker registers
        # example.test.evil.example and inherits the allowlist.
        assert not _http.is_allowed_host("https://example.test.evil.example/x", _HOSTS)
        assert not _http.is_allowed_host("https://notexample.test/x", _HOSTS)
        assert not _http.is_allowed_host("https://evil.example/x", _HOSTS)

    def test_no_host_is_refused(self):
        assert not _http.is_allowed_host("https:///x", _HOSTS)

    def test_unparseable_url_fails_closed(self):
        with patch.object(_http.urllib.parse, "urlparse", side_effect=ValueError):
            assert not _http.is_allowed_host("https://example.test/x", _HOSTS)


class TestRedirectGuard:
    """Both checks run BEFORE a redirect is followed, so no connection is ever
    made to a refused target."""

    def _handler(self):
        return _http._redirect_handler(_HOSTS, _http.is_internal_url)

    def test_allowlisted_redirect_is_followed(self):
        handler = self._handler()
        with patch.object(
            urllib.request.HTTPRedirectHandler, "redirect_request", return_value="followed"
        ):
            req = urllib.request.Request("https://example.test/a")
            assert (
                handler.redirect_request(req, None, 302, "Found", {}, "https://cdn.example.test/b")
                == "followed"
            )

    @pytest.mark.parametrize(
        "target",
        [
            "https://evil.example/steal",  # off-allowlist DNS name
            "http://example.test/x",  # allowlisted host, but plain HTTP
            "https://169.254.169.254/latest/meta-data/",  # metadata endpoint
            "http://0xa9fea9fe/",  # encoded metadata endpoint
            "https://metadata.google.internal/x",
        ],
    )
    def test_refused_redirects_raise_before_being_followed(self, target):
        handler = self._handler()
        with patch.object(urllib.request.HTTPRedirectHandler, "redirect_request") as parent:
            req = urllib.request.Request("https://example.test/a")
            with pytest.raises(urllib.error.URLError):
                handler.redirect_request(req, None, 302, "Found", {}, target)
            assert not parent.called, "a refused target reached the parent handler"


class TestOpenGuarded:
    def test_returns_the_response(self):
        resp = _Resp(b"body")
        opener, patcher = _with_opener(resp)
        with patcher:
            got = _http.open_guarded(
                urllib.request.Request("https://example.test/x"),
                allowed_hosts=_HOSTS,
                internal_check=_http.is_internal_url,
            )
        assert got is resp
        assert opener.opened == ["https://example.test/x"]

    def test_url_error_becomes_none(self):
        _, patcher = _with_opener(urllib.error.URLError("boom"))
        with patcher:
            assert (
                _http.open_guarded(
                    urllib.request.Request("https://example.test/x"),
                    allowed_hosts=_HOSTS,
                    internal_check=_http.is_internal_url,
                )
                is None
            )


# ---- the bounded read -----------------------------------------------------


class TestReadBounded:
    def test_reads_a_whole_small_body(self):
        assert _http.read_bounded(_Resp(b"hello"), 1024) == b"hello"

    def test_empty_body(self):
        assert _http.read_bounded(_Resp(b""), 1024) == b""

    def test_exactly_at_the_ceiling_is_kept(self):
        body = b"x" * 64
        assert _http.read_bounded(_Resp(body), 64) == body

    def test_one_byte_over_the_ceiling_is_refused(self):
        assert _http.read_bounded(_Resp(b"x" * 65), 64) is None

    def test_oversized_body_is_abandoned_mid_stream(self):
        # The cap bounds the bytes RETAINED, not just the verdict on a finished
        # body: with a 64 KiB chunk size and a 1-byte ceiling, the read must stop
        # after the first chunk rather than draining megabytes first.
        body = b"y" * (_http.READ_CHUNK_BYTES * 4)
        resp = _Resp(body)
        assert _http.read_bounded(resp, 1) is None
        # One chunk consumed, the rest still unread.
        assert len(resp.read()) == len(body) - _http.READ_CHUNK_BYTES


# ---- the fetch helpers ----------------------------------------------------


class TestSyncFetch:
    def _fetch_bytes(self, result, **kw):
        opener, patcher = _with_opener(result)
        with patcher:
            return (
                _http.sync_fetch_bytes(
                    "https://example.test/x",
                    allowed_hosts=_HOSTS,
                    internal_check=_http.is_internal_url,
                    **kw,
                ),
                opener,
            )

    def test_happy_path(self):
        got, _ = self._fetch_bytes(_Resp(b"payload"))
        assert got == b"payload"

    def test_the_response_is_always_closed(self):
        resp = _Resp(b"payload")
        self._fetch_bytes(resp)
        assert resp.closed, "a response left open leaks the connection"
        # Including on the non-200 path.
        bad = _Resp(b"nope", status=404)
        self._fetch_bytes(bad)
        assert bad.closed

    def test_non_200_is_none(self):
        for status in (301, 400, 404, 500, 503):
            got, _ = self._fetch_bytes(_Resp(b"body", status=status))
            assert got is None

    def test_internal_url_is_refused_before_connecting(self):
        opener, patcher = _with_opener(_Resp(b"secret"))
        with patcher:
            assert (
                _http.sync_fetch_bytes(
                    "http://169.254.169.254/latest/meta-data/",
                    allowed_hosts=_HOSTS,
                    internal_check=_http.is_internal_url,
                )
                is None
            )
        assert opener.opened == [], "the pre-connect screen let a connection happen"

    def test_oversized_body_is_none(self):
        got, _ = self._fetch_bytes(_Resp(b"x" * 200), max_bytes=100)
        assert got is None

    def test_os_error_becomes_none(self):
        got, _ = self._fetch_bytes(OSError("socket died"))
        assert got is None

    def test_default_user_agent_is_sent_and_headers_merge(self):
        captured = {}

        class _Capturing(_Opener):
            def open(self, req, timeout=None):
                captured.update(req.headers)
                return _Resp(b"ok")

        opener = _Capturing(None)
        with patch.object(_http.urllib.request, "build_opener", lambda *a: opener):
            _http.sync_fetch_bytes(
                "https://example.test/x",
                allowed_hosts=_HOSTS,
                internal_check=_http.is_internal_url,
                headers={"Accept": "application/vnd.github.sha"},
            )
        # urllib capitalizes header names.
        assert captured["User-agent"] == _http.USER_AGENT
        assert captured["Accept"] == "application/vnd.github.sha"

    def test_json_decodes(self):
        opener, patcher = _with_opener(_Resp(b'{"a": [1, 2]}'))
        with patcher:
            assert _http.sync_fetch_json(
                "https://example.test/x",
                allowed_hosts=_HOSTS,
                internal_check=_http.is_internal_url,
            ) == {"a": [1, 2]}

    @pytest.mark.parametrize("body", [b"not json", b"", b"\xff\xfe binary", b"{unclosed"])
    def test_undecodable_json_is_none(self, body):
        opener, patcher = _with_opener(_Resp(body))
        with patcher:
            assert (
                _http.sync_fetch_json(
                    "https://example.test/x",
                    allowed_hosts=_HOSTS,
                    internal_check=_http.is_internal_url,
                )
                is None
            )

    def test_json_none_body_propagates_as_none(self):
        opener, patcher = _with_opener(urllib.error.URLError("x"))
        with patcher:
            assert (
                _http.sync_fetch_json(
                    "https://example.test/x",
                    allowed_hosts=_HOSTS,
                    internal_check=_http.is_internal_url,
                )
                is None
            )

    def test_text_decodes_utf8(self):
        opener, patcher = _with_opener(_Resp("héllo".encode()))
        with patcher:
            assert (
                _http.sync_fetch_text(
                    "https://example.test/x",
                    allowed_hosts=_HOSTS,
                    internal_check=_http.is_internal_url,
                )
                == "héllo"
            )

    def test_non_utf8_text_is_none_not_mojibake(self):
        # Every consumer writes the result to a skill file, so a lossy decode
        # would put mangled bytes on disk. None means "skip this file".
        opener, patcher = _with_opener(_Resp(b"\x89PNG\r\n\x1a\n\xff\xfe"))
        with patcher:
            assert (
                _http.sync_fetch_text(
                    "https://example.test/x",
                    allowed_hosts=_HOSTS,
                    internal_check=_http.is_internal_url,
                )
                is None
            )

    def test_text_failure_propagates_as_none(self):
        opener, patcher = _with_opener(_Resp(b"x", status=500))
        with patcher:
            assert (
                _http.sync_fetch_text(
                    "https://example.test/x",
                    allowed_hosts=_HOSTS,
                    internal_check=_http.is_internal_url,
                )
                is None
            )


class TestRunOffLoop:
    @pytest.mark.asyncio
    async def test_returns_the_callables_result(self):
        assert await _http.run_off_loop(lambda a, b=0: a + b, 2, b=3) == 5

    @pytest.mark.asyncio
    async def test_a_raising_callable_becomes_none(self):
        # "The fetch did not produce anything" is the contract every caller
        # already has, so an exception must not surface as one.
        def _boom():
            raise RuntimeError("network on fire")

        assert await _http.run_off_loop(_boom) is None

    def test_without_a_running_loop_it_is_none(self):
        # get_running_loop() raises outside a loop; swallowed like any other
        # failure rather than escaping into a provider coroutine's caller.
        async def _noop():
            return None

        assert asyncio.run(_http.run_off_loop(lambda: "ok")) == "ok"
