"""Tests for the link-preview backend — SSRF vet, extraction, and the endpoint.

No test in this file opens a socket. The vet takes an injected resolver and the
handler's transport is swapped for :class:`_FakeTransport`, so every path
(redirect re-vetting, the read cap, icon rejection, cache/dedup) is exercised
against canned bytes.

The URL vet's scheme, host, port, IP-literal and DNS rules, metadata extraction and
the icon lanes are in ``test_link_unfurl_vet_and_extraction.py``; the resolver stub and
the autouse isolation both modules use are in ``link_unfurl_helpers.py``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import pytest
from link_unfurl_helpers import _isolate  # noqa: F401
from link_unfurl_helpers import _public, _set_enabled

from kiro_crew import link_unfurl as lu
from kiro_crew.dashboard.handlers import link_meta as lm


def _run(coro):
    return asyncio.run(coro)


def _multi_homed(_host: str, _port: int) -> List[str]:
    """Resolver stub for a CDN: several public nodes, plus a v6 one.

    Shaped like what `getaddrinfo` returns for a real CDN host, including the
    duplicate a host answering on several socket protocols produces.
    """
    return [
        "93.184.216.34",
        "93.184.216.34",
        "93.184.216.35",
        "2606:4700::1111",
    ]


# --- login-gate titles -------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Sign in to Amazon | Slack",
        "Sign In",
        "sign-in",
        "Signin",
        "Log in",
        "Login",
        "Log On",
        "Sign in - Google Accounts",
        "Sign in to GitHub · GitHub",
        "Sign into your account",
        "Sign in with your Apple ID",
        "Please log in",
        "Acme Corp - Sign In",
        "Slack | Sign in",
        "Single Sign-On",
        "Authentication Required",
    ],
)
def test_login_gate_titles_are_detected(title: str) -> None:
    assert lu.is_login_page_title(title)


@pytest.mark.parametrize(
    "title",
    [
        "",
        "Slack is your digital HQ",
        "How to sign in to AWS",
        "Assign in bulk",
        "Logging in production systems",
        "Log4j vulnerability",
        "The Sign",
        "Design tokens - a primer",
        "Login security best practices",
        "Sign in with Apple: a guide",
        "Single sign-on explained",
        "Sign-in sheets for classrooms",
    ],
)
def test_ordinary_titles_are_not_login_gates(title: str) -> None:
    """Both ends are anchored: a leading gate phrase must be followed by the
    title's end, a separator, or a gate continuation — a real title that merely
    STARTS with "Login"/"Sign in" stays a title."""
    assert not lu.is_login_page_title(title)


# --- endpoint scaffolding --------------------------------------------------


class _FakeTransport:
    """Canned responses keyed by URL, with a fetch counter.

    ``routes[url]`` is ``(status, headers, body)``. ``chunk_size`` is small so a
    body that exceeds the read cap trips it mid-stream, the way a real oversized
    response does.
    """

    def __init__(self, routes: Dict[str, Tuple[int, Dict[str, str], bytes]]) -> None:
        self.routes = routes
        self.requested: List[str] = []

    async def get(self, vetted: Any) -> Tuple[lm._RawResponse, Any]:
        self.requested.append(vetted.url)
        try:
            status, headers, body = self.routes[vetted.url]
        except KeyError:  # pragma: no cover - a miss is a test bug
            raise AssertionError(f"unstubbed fetch: {vetted.url}")

        async def _chunks() -> AsyncIterator[bytes]:
            for i in range(0, max(len(body), 1), 8192):
                await asyncio.sleep(0)
                yield body[i : i + 8192]

        return lm._RawResponse(status=status, headers=headers, chunks=_chunks()), None

    @property
    def fetch_count(self) -> int:
        return len(self.requested)


class _FakeRequest:
    """Minimal stand-in for ``web.Request`` — the handler only reads ``query``."""

    def __init__(self, url: Optional[str]) -> None:
        self.query: Dict[str, str] = {} if url is None else {"url": url}


def _html(title: str = "Example page", extra: str = "") -> bytes:
    return (
        f"<html><head><title>{title}</title>"
        f'<meta name="description" content="Desc">{extra}'
        "</head><body>hi</body></html>"
    ).encode()


_HTML_HEADERS = {"Content-Type": "text/html; charset=utf-8"}


def _install(monkeypatch, routes: Dict[str, Tuple[int, Dict[str, str], bytes]]) -> _FakeTransport:
    transport = _FakeTransport(routes)
    monkeypatch.setattr(lm, "_TRANSPORT", transport)
    return transport


async def _call(url: Optional[str]) -> Tuple[int, Any]:
    resp = await lm.link_meta_get(_FakeRequest(url))  # type: ignore[arg-type]
    import json

    return resp.status, json.loads(resp.body)


# --- endpoint: gate --------------------------------------------------------


def test_disabled_returns_403_and_never_fetches(monkeypatch) -> None:
    _set_enabled(monkeypatch, False)
    transport = _install(monkeypatch, {})

    def _no_dns(_h: str, _p: int) -> List[str]:
        raise AssertionError("disabled endpoint must not resolve DNS")

    monkeypatch.setattr(lu, "_default_resolve", _no_dns)

    status, body = _run(_call("https://example.com/"))
    assert status == 403
    assert body == {"code": "link_previews_disabled"}
    assert transport.fetch_count == 0


def test_userinfo_is_refused_before_it_can_become_a_cache_key(monkeypatch) -> None:
    """A credential-bearing URL must never be retained as a cache/inflight key.

    The vet already refuses userinfo, but the handler derives the cache key from
    the RAW url — so a vet-only guard still parks `https://<key>:<secret>@host/`
    in the negative cache for the full failure TTL. This asserts the handler
    rejects it before any map is touched, and that no fetch is attempted.
    """
    _set_enabled(monkeypatch, True)
    transport = _install(monkeypatch, {})
    lm._CACHE.clear()
    lm._INFLIGHT.clear()

    status, body = _run(_call("https://key:secret@example.com/"))

    assert status == 400
    assert body == {"code": "invalid_url"}
    assert transport.fetch_count == 0
    retained = list(lm._CACHE) + list(lm._INFLIGHT)
    assert retained == [], f"credential retained in a key: {retained}"


@pytest.mark.parametrize("url", ["http://[", "http://[::1", "http://[fe80::1%25", "https://["])
def test_malformed_url_returns_coded_400_not_500(monkeypatch, url: str) -> None:
    """`urlsplit` RAISES on some shapes instead of returning empty parts.

    An unterminated IPv6 literal is the reachable one. The cache key is derived
    before the vet runs, so an unguarded `normalize_cache_key` turned a URL the
    endpoint should simply decline into an uncaught ValueError — a 500 on the
    dashboard's own API, from a string the model can emit by accident.
    """
    _set_enabled(monkeypatch, True)
    transport = _install(monkeypatch, {})
    lm._CACHE.clear()
    lm._INFLIGHT.clear()

    status, body = _run(_call(url))

    assert (status, body) == (400, {"code": "invalid_url"})
    assert transport.fetch_count == 0
    assert list(lm._CACHE) == [] and list(lm._INFLIGHT) == []


def test_missing_url_returns_400(monkeypatch) -> None:
    transport = _install(monkeypatch, {})
    for arg in (None, "", "   "):
        status, body = _run(_call(arg))
        assert (status, body) == (400, {"code": "invalid_url"})
    assert transport.fetch_count == 0


def test_blocked_url_returns_400_blocked(monkeypatch) -> None:
    transport = _install(monkeypatch, {})
    status, body = _run(_call("http://127.0.0.1/admin"))
    assert (status, body) == (400, {"code": "blocked_url"})
    assert transport.fetch_count == 0


def test_error_bodies_carry_only_a_code(monkeypatch) -> None:
    """No English prose in a non-2xx body — the frontend translates the code."""
    _install(monkeypatch, {})
    for arg, expected in (("", "invalid_url"), ("http://10.0.0.1/", "blocked_url")):
        _status, body = _run(_call(arg))
        assert set(body) == {"code"}
        assert body["code"] == expected


# --- endpoint: happy path --------------------------------------------------


def test_success_payload_shape(monkeypatch) -> None:
    _install(
        monkeypatch,
        {
            "https://www.example.com/page": (200, _HTML_HEADERS, _html()),
            "https://www.example.com/favicon.ico": (
                200,
                {"Content-Type": "image/png"},
                b"\x89PNG",
            ),
        },
    )
    status, body = _run(_call("https://www.example.com/page"))
    assert status == 200
    assert body["title"] == "Example page"
    assert body["description"] == "Desc"
    assert body["domain"] == "example.com"
    assert body["icon"] == "data:image/png;base64,iVBORw=="
    assert isinstance(body["fetched_at"], int) and body["fetched_at"] > 0


def test_login_gate_title_falls_back_to_domain_end_to_end(monkeypatch) -> None:
    """An auth wall's 200 must not label the chip with the gate's own title.

    The text fields are blanked so the client renders the domain; the entry
    still lands in the POSITIVE cache, because an anonymous re-fetch would be
    answered by the same gate.
    """
    _install(
        monkeypatch,
        {
            "https://ws.slack.com/archives/C123/p456": (
                200,
                _HTML_HEADERS,
                _html(
                    title="Sign in to Amazon | Slack",
                    extra='<meta property="og:site_name" content="Slack">',
                ),
            ),
            "https://ws.slack.com/favicon.ico": (404, {}, b""),
        },
    )
    status, body = _run(_call("https://ws.slack.com/archives/C123/p456"))
    assert status == 200
    assert body["title"] == ""
    assert body["description"] == ""
    assert body["site_name"] == ""
    assert body["domain"] == "ws.slack.com"
    entry = lm._CACHE[lu.normalize_cache_key("https://ws.slack.com/archives/C123/p456")]
    assert entry.payload is not None


def test_icon_failure_does_not_fail_the_preview(monkeypatch) -> None:
    _install(
        monkeypatch,
        {
            "https://example.com/p": (200, _HTML_HEADERS, _html()),
            "https://example.com/favicon.ico": (404, {}, b""),
        },
    )
    status, body = _run(_call("https://example.com/p"))
    assert status == 200
    assert body["icon"] == ""


def test_svg_favicon_is_dropped_end_to_end(monkeypatch) -> None:
    _install(
        monkeypatch,
        {
            "https://example.com/p": (
                200,
                _HTML_HEADERS,
                _html(extra='<link rel="icon" href="/logo.svg">'),
            ),
            "https://example.com/logo.svg": (
                200,
                {"Content-Type": "image/svg+xml"},
                b"<svg onload='fetch(1)'/>",
            ),
            "https://example.com/favicon.ico": (404, {}, b""),
        },
    )
    status, body = _run(_call("https://example.com/p"))
    assert (status, body["icon"]) == (200, "")


def test_oversized_icon_is_dropped_end_to_end(monkeypatch) -> None:
    """Over the 32 KB cap the read aborts, so the icon is dropped entirely."""
    _install(
        monkeypatch,
        {
            "https://example.com/p": (200, _HTML_HEADERS, _html()),
            "https://example.com/favicon.ico": (
                200,
                {"Content-Type": "image/png"},
                b"x" * (lu.MAX_ICON_BYTES + 4096),
            ),
        },
    )
    status, body = _run(_call("https://example.com/p"))
    assert (status, body["icon"]) == (200, "")


# --- endpoint: dark icon variant -------------------------------------------

_DARK_ICON_HTML = _html(
    extra=(
        '<link rel="icon" href="/light.png">'
        '<link rel="icon" href="/dark.png" media="(prefers-color-scheme: dark)">'
    )
)


def test_declared_dark_variant_is_returned_alongside_the_default(monkeypatch) -> None:
    """Both are sent: the CLIENT picks, because the theme switches at runtime."""
    transport = _install(
        monkeypatch,
        {
            "https://example.com/p": (200, _HTML_HEADERS, _DARK_ICON_HTML),
            "https://example.com/light.png": (200, {"Content-Type": "image/png"}, b"\x89PNG"),
            "https://example.com/dark.png": (200, {"Content-Type": "image/png"}, b"\x89PNGdark"),
        },
    )
    status, body = _run(_call("https://example.com/p"))
    assert status == 200
    assert body["icon"] == "data:image/png;base64,iVBORw=="
    assert body["icon_dark"] == "data:image/png;base64,iVBOR2Rhcms="
    assert transport.fetch_count == 3  # page + both icons, once each


def test_no_declared_variant_costs_no_extra_request(monkeypatch) -> None:
    """The common case must stay exactly as cheap as it was: one icon fetch."""
    transport = _install(
        monkeypatch,
        {
            "https://example.com/p": (200, _HTML_HEADERS, _html()),
            "https://example.com/favicon.ico": (200, {"Content-Type": "image/png"}, b"\x89PNG"),
        },
    )
    status, body = _run(_call("https://example.com/p"))
    assert (status, body["icon_dark"]) == (200, "")
    assert transport.fetch_count == 2  # page + favicon only


def test_dark_variant_is_dropped_when_it_is_the_same_picture(monkeypatch) -> None:
    """Identical bytes carry no information and would double the payload."""
    _install(
        monkeypatch,
        {
            "https://example.com/p": (
                200,
                _HTML_HEADERS,
                _html(
                    extra=(
                        '<link rel="icon" href="/a.png">'
                        '<link rel="icon" href="/b.png" media="(prefers-color-scheme: dark)">'
                    )
                ),
            ),
            "https://example.com/a.png": (200, {"Content-Type": "image/png"}, b"\x89PNG"),
            "https://example.com/b.png": (200, {"Content-Type": "image/png"}, b"\x89PNG"),
        },
    )
    status, body = _run(_call("https://example.com/p"))
    assert status == 200
    assert body["icon"] == "data:image/png;base64,iVBORw=="
    assert body["icon_dark"] == ""


def test_dark_variant_failure_does_not_fail_the_preview(monkeypatch) -> None:
    """A 404 on the nicety leaves the default icon and the card intact."""
    _install(
        monkeypatch,
        {
            "https://example.com/p": (200, _HTML_HEADERS, _DARK_ICON_HTML),
            "https://example.com/light.png": (200, {"Content-Type": "image/png"}, b"\x89PNG"),
            "https://example.com/dark.png": (404, {}, b""),
        },
    )
    status, body = _run(_call("https://example.com/p"))
    assert status == 200
    assert body["icon"] == "data:image/png;base64,iVBORw=="
    assert body["icon_dark"] == ""


def test_dark_lane_is_vetted_like_any_other_fetch(monkeypatch) -> None:
    """The variant href is a second attacker-chosen URL; the vet still owns it."""
    transport = _install(
        monkeypatch,
        {
            "https://example.com/p": (
                200,
                _HTML_HEADERS,
                _html(
                    extra=(
                        '<link rel="icon" href="/light.png">'
                        '<link rel="icon" href="http://169.254.169.254/latest/meta-data"'
                        ' media="(prefers-color-scheme: dark)">'
                    )
                ),
            ),
            "https://example.com/light.png": (200, {"Content-Type": "image/png"}, b"\x89PNG"),
        },
    )
    status, body = _run(_call("https://example.com/p"))
    assert (status, body["icon_dark"]) == (200, "")
    assert transport.fetch_count == 2  # page + the default icon; the link-local host was refused


def test_dark_svg_variant_is_dropped_like_any_other_svg(monkeypatch) -> None:
    """SVG is active content in an `<img src>`; a second icon field is no exemption."""
    _install(
        monkeypatch,
        {
            "https://example.com/p": (
                200,
                _HTML_HEADERS,
                _html(
                    extra=(
                        '<link rel="icon" href="/light.png">'
                        '<link rel="icon" href="/dark.svg" media="(prefers-color-scheme: dark)">'
                    )
                ),
            ),
            "https://example.com/light.png": (200, {"Content-Type": "image/png"}, b"\x89PNG"),
            "https://example.com/dark.svg": (
                200,
                {"Content-Type": "image/svg+xml"},
                b"<svg onload='fetch(1)'/>",
            ),
        },
    )
    status, body = _run(_call("https://example.com/p"))
    assert (status, body["icon_dark"]) == (200, "")


def test_dark_lane_spends_exactly_one_request(monkeypatch) -> None:
    """Two declared dark variants, one attempt: this lane buys a nicety."""
    transport = _install(
        monkeypatch,
        {
            "https://example.com/p": (
                200,
                _HTML_HEADERS,
                _html(
                    extra=(
                        '<link rel="icon" href="/light.png">'
                        '<link rel="icon" href="/d1.png" media="(prefers-color-scheme: dark)">'
                        '<link rel="icon" href="/d2.png" media="(prefers-color-scheme: dark)">'
                    )
                ),
            ),
            "https://example.com/light.png": (200, {"Content-Type": "image/png"}, b"\x89PNG"),
            "https://example.com/d1.png": (404, {}, b""),
            "https://example.com/d2.png": (200, {"Content-Type": "image/png"}, b"\x89PNGtwo"),
        },
    )
    status, body = _run(_call("https://example.com/p"))
    assert (status, body["icon_dark"]) == (200, "")
    assert transport.fetch_count == 3  # page + default icon + ONE dark attempt


# --- endpoint: body caps ---------------------------------------------------


def _raw(body: bytes, headers: Optional[Dict[str, str]] = None) -> Tuple[Any, List[int]]:
    """A ``_RawResponse`` over *body* plus a list recording bytes yielded.

    The recorder is what proves the cap stops pulling from the socket instead of
    draining the whole body and trimming afterwards.
    """
    served: List[int] = []

    async def _chunks() -> AsyncIterator[bytes]:
        for i in range(0, max(len(body), 1), 8192):
            chunk = body[i : i + 8192]
            served.append(len(chunk))
            yield chunk

    return lm._RawResponse(status=200, headers=headers or {}, chunks=_chunks()), served


def test_read_capped_truncates_a_page_to_the_cap() -> None:
    """A page over the cap yields exactly the cap, reports the cut, and stops."""
    payload = b"<html><head><title>t</title></head><body>" + b"A" * (lu.MAX_BODY_BYTES + 40960)
    raw, served = _raw(payload)
    body, cut = _run(lm._read_capped(raw, lu.MAX_BODY_BYTES, truncate=True))
    assert (len(body), cut) == (lu.MAX_BODY_BYTES, True)
    # Reading stopped on the chunk that crossed the cap — chunk granularity is as
    # tight as a stream read gets — rather than draining the body and trimming.
    assert sum(served) <= lu.MAX_BODY_BYTES + 8192 < len(payload)


def test_read_capped_keeps_a_body_that_ends_exactly_at_the_cap() -> None:
    """A body of exactly the cap is not a cut, so the caller's guard stays off."""
    raw, served = _raw(b"A" * lu.MAX_BODY_BYTES)
    body, cut = _run(lm._read_capped(raw, lu.MAX_BODY_BYTES, truncate=True))
    assert (len(body), cut) == (lu.MAX_BODY_BYTES, False)
    assert sum(served) == lu.MAX_BODY_BYTES


def test_read_capped_ignores_an_honest_oversized_length_when_truncating() -> None:
    """A page's declared length is not an early exit — only the read is a bound.

    Amazon-shaped responses declare (or chunk) far more than the cap while their
    ``<head>`` is tiny; short-circuiting on the header threw those away without
    reading a byte.
    """
    raw, _served = _raw(b"tiny", {"Content-Length": str(lu.MAX_BODY_BYTES + 1)})
    assert _run(lm._read_capped(raw, lu.MAX_BODY_BYTES, truncate=True)) == (b"tiny", False)


def test_read_capped_rejects_an_oversized_icon() -> None:
    """An icon is never truncated: half a PNG is corrupt, not smaller."""
    raw, _served = _raw(b"x" * (lu.MAX_ICON_BYTES + 4096))
    with pytest.raises(lm._UnfurlFailed):
        _run(lm._read_capped(raw, lu.MAX_ICON_BYTES, truncate=False))


def test_read_capped_accepts_an_icon_exactly_at_the_cap() -> None:
    raw, _served = _raw(b"x" * lu.MAX_ICON_BYTES)
    body, cut = _run(lm._read_capped(raw, lu.MAX_ICON_BYTES, truncate=False))
    assert (len(body), cut) == (lu.MAX_ICON_BYTES, False)


def test_read_capped_short_circuits_an_honest_oversized_icon_length() -> None:
    raw, served = _raw(b"tiny", {"Content-Length": str(lu.MAX_ICON_BYTES + 1)})
    with pytest.raises(lm._UnfurlFailed):
        _run(lm._read_capped(raw, lu.MAX_ICON_BYTES, truncate=False))
    assert served == []  # refused on the header, nothing read


def test_oversized_page_is_previewed_from_its_head(monkeypatch) -> None:
    """The bug this fixes: a page far over the cap still unfurls.

    ``<head>`` arrives in the first few KB, so a 700 KB homepage (amazon.com's
    shape) has delivered the whole preview long before the 256 KiB cap. It used
    to 502 — indistinguishable, in the UI, from the feature being off.
    """
    page = (
        b"<html><head><title>Amazon.com. Spend less. Smile more.</title>"
        b'<meta name="description" content="Free shipping on millions of items.">'
        b"</head><body>" + b"A" * (lu.MAX_BODY_BYTES * 3) + b"</body></html>"
    )
    _install(
        monkeypatch,
        {
            "https://example.com/big": (200, _HTML_HEADERS, page),
            "https://example.com/favicon.ico": (404, {}, b""),
        },
    )
    status, body = _run(_call("https://example.com/big"))
    assert status == 200
    assert body["title"] == "Amazon.com. Spend less. Smile more."
    assert body["description"] == "Free shipping on millions of items."


def test_page_whose_head_overruns_the_cap_is_a_502(monkeypatch) -> None:
    """A cut that took the metadata with it must not cache a titleless card.

    502 puts it on the 10 min negative TTL; a 200 would pin an empty preview for
    the 6 h positive TTL.
    """
    _install(
        monkeypatch,
        {
            "https://example.com/fathead": (
                200,
                _HTML_HEADERS,
                b"<html><head><style>" + b"z" * (lu.MAX_BODY_BYTES + 4096),
            )
        },
    )
    status, body = _run(_call("https://example.com/fathead"))
    assert (status, body) == (502, {"code": "fetch_failed"})


def test_head_end_markers_inside_a_script_do_not_pass_the_cut_guard(monkeypatch) -> None:
    """The guard is the parser's verdict, not a byte search.

    A fat head containing the literal ``</head>``/``<body>`` inside a `<script>`
    would satisfy any byte-pattern check while `_HeadParser` — correctly — treats
    them as script data and never leaves the head. The real `<title>` is past the
    cut, so this must fail, not return an empty-title 200.
    """
    _install(
        monkeypatch,
        {
            "https://example.com/tricky": (
                200,
                _HTML_HEADERS,
                b"<html><head><script>var s = '</head><body>';"
                + b"//"
                + b"z" * (lu.MAX_BODY_BYTES + 4096)
                + b"</script><title>Never read</title></head>",
            )
        },
    )
    status, body = _run(_call("https://example.com/tricky"))
    assert (status, body) == (502, {"code": "fetch_failed"})


def test_title_straddling_the_cap_is_not_served_as_a_fragment(monkeypatch) -> None:
    """A cut INSIDE `<title>` leaves a word-fragment that reads like a real title.

    `<title>` never closes in the prefix, so `title_complete` is False and the page
    takes the negative TTL instead of caching "Real Titl" for six hours. The whole
    point of asking the parser: the fragment is non-empty, so a "did we get *a*
    title" check would have accepted it.
    """
    filler = b"z" * (lu.MAX_BODY_BYTES - 40)
    page = b"<html><head><!--" + filler + b"--><title>Real Title Here</title></head>"
    _install(monkeypatch, {"https://example.com/straddle": (200, _HTML_HEADERS, page)})
    status, body = _run(_call("https://example.com/straddle"))
    assert (status, body) == (502, {"code": "fetch_failed"})


def test_extract_meta_reports_whether_the_title_closed() -> None:
    """`title_complete` is the parser's own verdict, per source of the title."""
    closed = lu.extract_meta("<html><head><title>Done</title>", base_url="https://e.com/")
    assert (closed.title, closed.title_complete) == ("Done", True)

    open_tag = lu.extract_meta("<html><head><title>Half wa", base_url="https://e.com/")
    assert (open_tag.title, open_tag.title_complete) == ("Half wa", False)

    # An `og:title` lives in an attribute: it parses whole or not at all.
    og = lu.extract_meta(
        '<html><head><meta property="og:title" content="Whole">', base_url="https://e.com/"
    )
    assert (og.title, og.title_complete) == ("Whole", True)

    none = lu.extract_meta("<html><head>", base_url="https://e.com/")
    assert (none.title, none.title_complete) == ("", False)


@pytest.mark.parametrize(
    "html,expected_complete",
    [
        # A stray `</title>` before any `<title>` must not credit the later,
        # unterminated element with the earlier close.
        ("<html><head></title><title>Frag", False),
        # Nor may a *previous* element's close carry over to a new one.
        ("<html><head><title>First</title><title>Frag", False),
        # Closed but empty is not a usable title, so it cannot be "complete".
        ("<html><head><title></title>", False),
        ("<html><head><title/>", False),
        # Two closed elements: whatever the concatenation, nothing was cut.
        ("<html><head><title>A</title><title>B</title>", True),
    ],
)
def test_title_completeness_is_not_fooled_by_stray_or_repeated_tags(
    html: str, expected_complete: bool
) -> None:
    assert lu.extract_meta(html, base_url="https://e.com/").title_complete is expected_complete


_STRAY_CLOSE_SHAPES = {
    # `</title>` arrives first, then the real element is cut mid-text.
    "unmatched_close": b"<html><head></title><!--",
    # A complete first title, then a second one cut mid-text.
    "second_title": b"<html><head><title>First</title><!--",
}


@pytest.mark.parametrize("shape", sorted(_STRAY_CLOSE_SHAPES))
def test_cut_title_after_a_stray_close_is_a_502(monkeypatch, shape: str) -> None:
    """A close that did not terminate THIS title must not license a fragment."""
    prefix = _STRAY_CLOSE_SHAPES[shape]
    opener = b"--><title>"
    # Land the cap five bytes into the title text, so the prefix ends mid-word and
    # the element never closes: `...<title>Real`.
    filler = b"z" * (lu.MAX_BODY_BYTES - len(prefix) - len(opener) - 5)
    page = prefix + filler + opener + b"Real Title Here</title></head>"
    _install(monkeypatch, {"https://example.com/stray": (200, _HTML_HEADERS, page)})
    status, body = _run(_call("https://example.com/stray"))
    assert (status, body) == (502, {"code": "fetch_failed"})


def test_untruncated_page_with_no_title_still_previews(monkeypatch) -> None:
    """`cut` is what gates the guard: a genuinely titleless small page is fine."""
    _install(
        monkeypatch,
        {
            "https://example.com/bare": (200, _HTML_HEADERS, b"<html><head></head><body>hi"),
            "https://example.com/favicon.ico": (404, {}, b""),
        },
    )
    status, body = _run(_call("https://example.com/bare"))
    assert (status, body["title"], body["domain"]) == (200, "", "example.com")


#: Head prefixes in which a literal ``</head>``/``<body>`` sits somewhere
#: `_HeadParser` treats as data, so a byte search would wrongly clear them.
_FAT_HEAD_DECOY_PREFIXES = {
    "comment": b"<html><head><!-- </head><body> ",
    "attribute": b'<html><head><meta name="a" content="</head><body>"><style>',
    "style": b"<html><head><style>a{}</head><body>{}",
}


def _fat_head(prefix: bytes) -> bytes:
    """*prefix*, filler past the cap, then the real title — which is never read."""
    return prefix + b"z" * (lu.MAX_BODY_BYTES + 4096) + b"<title>Never read</title>"


@pytest.mark.parametrize("shape", sorted(_FAT_HEAD_DECOY_PREFIXES))
def test_fat_head_with_a_decoy_marker_is_a_502(monkeypatch, shape: str) -> None:
    page = _fat_head(_FAT_HEAD_DECOY_PREFIXES[shape])
    _install(monkeypatch, {"https://example.com/decoy": (200, _HTML_HEADERS, page)})
    status, body = _run(_call("https://example.com/decoy"))
    assert (status, body) == (502, {"code": "fetch_failed"})


#: Truncated pages that DO carry their metadata before the cut, as
#: ``(head_prefix, filler_byte, expected_title)``. Each names the shape that
#: could be mis-rejected by a stricter guard. Only the prefix is held here: the
#: filler past the cap is appended in the test body, because a payload built at
#: module scope is allocated during collection and then held for the whole
#: session by every xdist worker, including those that never run this file.
_TRUNCATED_BUT_TITLED = {
    # `<body/>` as the sole head terminator — a byte pattern keyed on `<body[\s>]`
    # misses the self-closing form and would reject this.
    "self_closing_body": (b"<html><head><title>Ok</title><body/>", b"A", "Ok"),
    # No `<title>` at all, but an `og:title` — `extract_meta` prefers og, so the
    # preview is complete and the cut is harmless.
    "og_title_only": (
        b'<html><head><meta property="og:title" content="Via OG"><style>',
        b"z",
        "Via OG",
    ),
}


@pytest.mark.parametrize("shape", sorted(_TRUNCATED_BUT_TITLED))
def test_truncated_page_keeps_its_preview_when_the_title_arrived(monkeypatch, shape: str) -> None:
    prefix, filler, expected = _TRUNCATED_BUT_TITLED[shape]
    page = prefix + filler * (lu.MAX_BODY_BYTES + 4096)
    _install(
        monkeypatch,
        {
            "https://example.com/cut": (200, _HTML_HEADERS, page),
            "https://example.com/favicon.ico": (404, {}, b""),
        },
    )
    status, body = _run(_call("https://example.com/cut"))
    assert (status, body["title"]) == (200, expected)


def test_oversized_body_with_lying_content_length(monkeypatch) -> None:
    """A truthful-looking small Content-Length must not raise the real bound.

    The header claims 42 bytes and the body is 260 KB. The read cap — not the
    header — is what bounds it, so this succeeds off the truncated prefix.
    """
    transport = _install(
        monkeypatch,
        {
            "https://example.com/big": (
                200,
                {"Content-Type": "text/html", "Content-Length": "42"},
                b"<html><head><title>x</title></head><body>" + b"A" * (lu.MAX_BODY_BYTES + 4096),
            ),
            "https://example.com/favicon.ico": (404, {}, b""),
        },
    )
    status, body = _run(_call("https://example.com/big"))
    assert (status, body["title"]) == (200, "x")
    assert transport.requested == [
        "https://example.com/big",
        "https://example.com/favicon.ico",
    ]


def test_body_at_cap_is_accepted(monkeypatch) -> None:
    filler = b"<!--" + b"z" * (lu.MAX_BODY_BYTES - 200) + b"-->"
    page = b"<html><head><title>Big but ok</title></head>" + filler
    _install(
        monkeypatch,
        {
            "https://example.com/edge": (200, {"Content-Type": "text/html"}, page),
            "https://example.com/favicon.ico": (404, {}, b""),
        },
    )
    status, body = _run(_call("https://example.com/edge"))
    assert (status, body["title"]) == (200, "Big but ok")


@pytest.mark.parametrize(
    "content_type", ["application/pdf", "image/png", "text/plain", "application/json", ""]
)
def test_non_html_content_type_is_refused(monkeypatch, content_type: str) -> None:
    _install(
        monkeypatch,
        {"https://example.com/f": (200, {"Content-Type": content_type}, b"%PDF-1.7")},
    )
    status, body = _run(_call("https://example.com/f"))
    assert (status, body) == (502, {"code": "fetch_failed"})


def test_xhtml_is_parsed(monkeypatch) -> None:
    _install(
        monkeypatch,
        {
            "https://example.com/x": (
                200,
                {"Content-Type": "application/xhtml+xml"},
                _html("XHTML page"),
            ),
            "https://example.com/favicon.ico": (404, {}, b""),
        },
    )
    status, body = _run(_call("https://example.com/x"))
    assert (status, body["title"]) == (200, "XHTML page")


def test_upstream_error_status_becomes_502(monkeypatch) -> None:
    _install(monkeypatch, {"https://example.com/e": (500, _HTML_HEADERS, b"")})
    status, body = _run(_call("https://example.com/e"))
    assert (status, body) == (502, {"code": "fetch_failed"})


# --- endpoint: redirects ---------------------------------------------------


def test_redirect_chain_is_followed_and_final_url_reported(monkeypatch) -> None:
    transport = _install(
        monkeypatch,
        {
            "https://example.com/1": (301, {"Location": "/2"}, b""),
            "https://example.com/2": (302, {"Location": "https://example.com/3"}, b""),
            "https://example.com/3": (200, _HTML_HEADERS, _html("Final")),
            "https://example.com/favicon.ico": (404, {}, b""),
        },
    )
    status, body = _run(_call("https://example.com/1"))
    assert (status, body["title"]) == (200, "Final")
    assert body["url"] == "https://example.com/3"
    assert transport.requested[:3] == [
        "https://example.com/1",
        "https://example.com/2",
        "https://example.com/3",
    ]


def test_redirect_to_internal_address_is_revetted(monkeypatch) -> None:
    """The hop the UPSTREAM chose is vetted too — otherwise a public host that
    302s to the metadata endpoint would be fetched on the attacker's behalf."""
    transport = _install(
        monkeypatch,
        {
            "https://example.com/start": (
                302,
                {"Location": "http://169.254.169.254/latest/meta-data/"},
                b"",
            )
        },
    )
    status, body = _run(_call("https://example.com/start"))
    assert (status, body) == (400, {"code": "blocked_url"})
    assert transport.requested == ["https://example.com/start"]


def test_redirect_to_rebinding_host_is_revetted(monkeypatch) -> None:
    def _resolve(host: str, _port: int) -> List[str]:
        return ["127.0.0.1"] if host == "inside.example" else ["93.184.216.34"]

    monkeypatch.setattr(lu, "_default_resolve", _resolve)
    transport = _install(
        monkeypatch,
        {"https://example.com/s": (307, {"Location": "http://inside.example/"}, b"")},
    )
    status, body = _run(_call("https://example.com/s"))
    assert (status, body) == (400, {"code": "blocked_url"})
    assert transport.fetch_count == 1


def test_redirect_limit_is_enforced(monkeypatch) -> None:
    routes: Dict[str, Tuple[int, Dict[str, str], bytes]] = {
        f"https://example.com/{i}": (302, {"Location": f"/{i + 1}"}, b"") for i in range(12)
    }
    transport = _install(monkeypatch, routes)
    status, body = _run(_call("https://example.com/0"))
    assert (status, body) == (502, {"code": "fetch_failed"})
    # MAX_REDIRECTS hops means MAX_REDIRECTS + 1 requests, then give up.
    assert transport.fetch_count == lu.MAX_REDIRECTS + 1


def test_redirect_without_location_fails(monkeypatch) -> None:
    _install(monkeypatch, {"https://example.com/r": (302, {}, b"")})
    status, body = _run(_call("https://example.com/r"))
    assert (status, body) == (502, {"code": "fetch_failed"})


# --- endpoint: cache / dedup ----------------------------------------------


def test_cache_hit_avoids_second_fetch(monkeypatch) -> None:
    transport = _install(
        monkeypatch,
        {
            "https://example.com/c": (200, _HTML_HEADERS, _html("Cached")),
            "https://example.com/favicon.ico": (404, {}, b""),
        },
    )

    async def _twice() -> Tuple[Any, Any]:
        first = await _call("https://example.com/c")
        second = await _call("https://example.com/c#anchor")
        return first, second

    (s1, b1), (s2, b2) = _run(_twice())
    assert s1 == s2 == 200
    assert b1 == b2  # identical body, including fetched_at — served from cache
    assert transport.fetch_count == 2  # page + favicon, once each


def test_negative_result_is_cached(monkeypatch) -> None:
    transport = _install(monkeypatch, {"https://example.com/dead": (503, _HTML_HEADERS, b"")})

    async def _twice() -> Tuple[Any, Any]:
        return await _call("https://example.com/dead"), await _call("https://example.com/dead")

    (s1, b1), (s2, b2) = _run(_twice())
    assert (s1, b1) == (s2, b2) == (502, {"code": "fetch_failed"})
    assert transport.fetch_count == 1


def test_blocked_result_is_cached_with_its_own_code(monkeypatch) -> None:
    _install(monkeypatch, {})

    async def _twice() -> Tuple[Any, Any]:
        return await _call("http://192.168.0.1/"), await _call("http://192.168.0.1/")

    (s1, b1), (s2, b2) = _run(_twice())
    assert (s1, b1) == (s2, b2) == (400, {"code": "blocked_url"})


def test_expired_entry_is_refetched(monkeypatch) -> None:
    transport = _install(
        monkeypatch,
        {
            "https://example.com/e": (200, _HTML_HEADERS, _html("Fresh")),
            "https://example.com/favicon.ico": (404, {}, b""),
        },
    )

    async def _flow() -> int:
        await _call("https://example.com/e")
        for entry in lm._CACHE.values():
            entry.expires_at = 0.0
        await _call("https://example.com/e")
        return transport.fetch_count

    assert _run(_flow()) == 4  # page + favicon, twice


def test_negative_ttl_is_shorter_than_positive_ttl(monkeypatch) -> None:
    """A dead link is retried in minutes; a good one is held for hours."""
    _install(
        monkeypatch,
        {
            "https://example.com/ok": (200, _HTML_HEADERS, _html()),
            "https://example.com/favicon.ico": (404, {}, b""),
            "https://example.com/bad": (500, _HTML_HEADERS, b""),
        },
    )

    async def _flow() -> Tuple[float, float]:
        await _call("https://example.com/ok")
        await _call("https://example.com/bad")
        good = lm._CACHE["https://example.com/ok"].expires_at
        bad = lm._CACHE["https://example.com/bad"].expires_at
        return good, bad

    good, bad = _run(_flow())
    assert bad < good
    assert lm._NEGATIVE_TTL_SECONDS < lm._TTL_SECONDS


def test_inflight_dedup_collapses_concurrent_callers(monkeypatch) -> None:
    """N chips for one URL cause ONE fetch."""
    transport = _install(
        monkeypatch,
        {
            "https://example.com/hot": (200, _HTML_HEADERS, _html("Hot")),
            "https://example.com/favicon.ico": (404, {}, b""),
        },
    )

    async def _storm() -> List[Tuple[int, Any]]:
        return list(await asyncio.gather(*(_call("https://example.com/hot") for _ in range(12))))

    results = _run(_storm())
    assert all(status == 200 and body["title"] == "Hot" for status, body in results)
    assert transport.fetch_count == 2  # page + favicon, exactly once each


def test_distinct_urls_are_not_deduped(monkeypatch) -> None:
    transport = _install(
        monkeypatch,
        {
            "https://example.com/a": (200, _HTML_HEADERS, _html("A")),
            "https://example.com/b": (200, _HTML_HEADERS, _html("B")),
            "https://example.com/favicon.ico": (404, {}, b""),
        },
    )

    async def _both() -> List[Tuple[int, Any]]:
        return list(
            await asyncio.gather(_call("https://example.com/a"), _call("https://example.com/b"))
        )

    results = _run(_both())
    assert sorted(body["title"] for _s, body in results) == ["A", "B"]
    assert transport.fetch_count == 4


def test_query_string_is_part_of_the_cache_key(monkeypatch) -> None:
    """A query routinely selects the content, so it must not be normalized away."""
    assert lu.normalize_cache_key("https://e.com/p?a=1#x") != lu.normalize_cache_key(
        "https://e.com/p?a=2"
    )
    assert lu.normalize_cache_key("https://e.com/p#x") == "https://e.com/p"


def test_cache_evicts_oldest_beyond_the_cap(monkeypatch) -> None:
    lm.reset_link_meta_cache()
    for i in range(lm._MAX_CACHE_ENTRIES + 25):
        lm._cache_put(
            f"https://e.com/{i}",
            lm._CacheEntry(
                payload={"url": f"https://e.com/{i}"}, code="", status=200, expires_at=9e18
            ),
        )
    assert len(lm._CACHE) == lm._MAX_CACHE_ENTRIES
    assert "https://e.com/0" not in lm._CACHE
    assert f"https://e.com/{lm._MAX_CACHE_ENTRIES + 24}" in lm._CACHE


def test_concurrency_is_capped_at_four(monkeypatch) -> None:
    """The semaphore bounds simultaneous upstream fetches, not total fetches."""
    peak = 0
    live = 0

    class _Slow(_FakeTransport):
        async def get(self, vetted: Any) -> Tuple[lm._RawResponse, Any]:
            nonlocal peak, live
            live += 1
            peak = max(peak, live)
            try:
                await asyncio.sleep(0)
                return await super().get(vetted)
            finally:
                live -= 1

    routes = {f"https://example.com/p{i}": (200, _HTML_HEADERS, _html(f"P{i}")) for i in range(20)}
    routes["https://example.com/favicon.ico"] = (404, {}, b"")
    monkeypatch.setattr(lm, "_TRANSPORT", _Slow(routes))

    async def _storm() -> None:
        await asyncio.gather(*(_call(f"https://example.com/p{i}") for i in range(20)))

    _run(_storm())
    assert peak <= lm._MAX_CONCURRENT_FETCHES


def test_the_vet_records_every_approved_address_and_ip_is_the_first() -> None:
    """`ip` stays the first address for callers that dial one; `addresses` is the
    whole approved answer, which is what a pin must serve. Both are needed: every
    entry already passed `_reject_if_internal_ip`, so the set is as safe as `ip`."""
    vetted = lu.vet_unfurl_url("https://cdn.example/o", resolve=_multi_homed)
    assert vetted.ip == "93.184.216.34"
    assert vetted.addresses == ("93.184.216.34", "93.184.216.35", "2606:4700::1111")


def test_an_ip_literal_records_its_canonical_form_as_the_only_address() -> None:
    # No resolution happens, so the recorded set is the literal the vet canonicalized.
    vetted = lu.vet_unfurl_url("https://93.184.216.34/a", resolve=_public)
    assert vetted.addresses == ("93.184.216.34",)
    assert vetted.ip == "93.184.216.34"


def test_allow_any_port_admits_a_public_host_on_a_nonstandard_port() -> None:
    """A PUBLIC host on a non-web port passes when the caller lifts the port
    allowlist (the remote MCP probe does; MCP servers use :8443 / :8000 / :3000).
    Only the port rule is relaxed -- the address is still vetted."""
    vetted = lu.vet_unfurl_url(
        "https://mcp.example.com:8443/mcp", resolve=_public, allow_any_port=True
    )
    assert vetted.port == 8443
    assert vetted.addresses == ("93.184.216.34",)


def test_the_port_allowlist_still_applies_by_default() -> None:
    """Without the opt-in, a non-80/443 port is refused, as every other caller
    (link preview, calendar, WeCom, OAuth discovery) relies on."""
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url("https://mcp.example.com:8443/mcp", resolve=_public)
    assert exc.value.code == "blocked_url"


@pytest.mark.parametrize(
    "url,answer",
    [
        ("http://127.0.0.1:9100/mcp", None),  # loopback literal
        ("http://[::1]/mcp", None),
        ("http://10.0.0.5:8000/mcp", None),  # RFC1918 literal
        ("http://100.64.0.7/mcp", None),  # shared address space (a tailnet)
        ("http://[fd12:3456::1]/mcp", None),  # ULA other than the metadata endpoint
        ("http://devbox.internal.example/mcp", "192.168.1.20"),  # private DNS answer
        ("http://printer.local/mcp", "192.168.1.30"),  # mDNS name
    ],
)
def test_allow_private_admits_loopback_and_private_targets(url: str, answer: str | None) -> None:
    """The MCP probe's opt-in: a local or LAN target passes, and the resolved
    addresses still come back for the pin."""
    vetted = lu.vet_unfurl_url(
        url, resolve=lambda _h, _p: [answer or "unused"], allow_private=True, allow_any_port=True
    )
    assert vetted.addresses
    if answer:
        assert vetted.addresses == (answer,)


@pytest.mark.parametrize(
    "url,answer",
    [
        ("http://169.254.169.254/latest/meta-data/", None),  # cloud metadata
        ("http://169.254.170.2/v2/credentials", None),  # container metadata
        ("http://[fd00:ec2::254]/latest/meta-data/", None),  # IPv6 cloud metadata
        ("http://[fe80::1]/mcp", None),  # IPv6 link-local
        ("http://[::ffff:169.254.169.254]/", None),  # IPv4-mapped spelling
        ("http://[2002:a9fe:a9fe::1]/", None),  # 6to4 spelling
        ("http://[fd00:ec2::254%0]/", None),  # scoped IPv6 metadata (numeric zone)
        ("http://[fd00:ec2::254%25eth0]/", None),  # scoped IPv6 metadata (RFC 6874 %25)
        ("http://[fe80::1%0]/", None),  # scoped IPv6 link-local
        ("http://2852039166/", None),  # decimal spelling
        ("http://metadata.example/", "169.254.169.254"),  # a name answering metadata
        ("http://rebind.example/", "10.0.0.5,169.254.169.254"),  # one bad answer of two
    ],
)
def test_allow_private_still_refuses_link_local_and_metadata(url: str, answer: str | None) -> None:
    """Only the address rule narrows: link-local and the metadata endpoint are
    refused by literal, by encoding and by DNS answer, on any port."""
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url(
            url,
            resolve=lambda _h, _p: (answer or "93.184.216.34").split(","),
            allow_private=True,
            allow_any_port=True,
        )
    assert exc.value.code == "blocked_url"


def test_allow_private_is_off_by_default() -> None:
    """Every other caller keeps the public-only rule: loopback and ``.local``
    are refused without the opt-in."""
    for url in ("http://127.0.0.1/", "http://printer.local/"):
        with pytest.raises(lu.UnfurlRejected):
            lu.vet_unfurl_url(url, resolve=lambda _h, _p: ["192.168.1.30"])


def test_allow_any_port_still_refuses_a_non_public_address() -> None:
    """Lifting the port rule does NOT touch the address rules: an internal
    target is refused on any port, by literal and by DNS answer alike."""
    with pytest.raises(lu.UnfurlRejected):
        lu.vet_unfurl_url("http://169.254.169.254:8443/x", allow_any_port=True)
    with pytest.raises(lu.UnfurlRejected):
        lu.vet_unfurl_url(
            "https://internal.example:8000/mcp",
            resolve=lambda _h, _p: ["10.0.0.5"],
            allow_any_port=True,
        )


# --- PinnedResolver: the other half of the vet -------------------------------
#
# The vet resolves the host and approves an address; this is what makes the socket
# go to THAT address. Without it the connector performs a second lookup, which an
# attacker-controlled DNS server answers differently (rebinding) and the vet has
# proved nothing. Both the link-preview handler and WeCom media install it.


class TestPinnedResolver:
    def test_it_answers_with_the_pinned_ipv4_address(self) -> None:
        resolver = lu.PinnedResolver("example.com", ("93.184.216.34",), 443)
        (answer,) = _run(resolver.resolve("example.com", 443))
        assert answer["host"] == "93.184.216.34"
        assert answer["port"] == 443
        assert answer["family"] is socket.AF_INET
        # The NAME travels on, so TLS SNI and the `Host` header stay correct --
        # connecting by IP instead would break certificate validation.
        assert answer["hostname"] == "example.com"

    def test_an_ipv6_pin_reports_the_v6_family(self) -> None:
        # aiohttp opens the socket with the family the resolver reports, so a v6
        # literal announced as AF_INET is an immediate connection failure.
        resolver = lu.PinnedResolver("example.com", ("2606:4700::1111",), 443)
        (answer,) = _run(resolver.resolve("example.com", 443))
        assert answer["host"] == "2606:4700::1111"
        assert answer["family"] is socket.AF_INET6

    def test_a_zero_port_falls_back_to_the_pinned_port(self) -> None:
        resolver = lu.PinnedResolver("example.com", ("93.184.216.34",), 8080)
        (answer,) = _run(resolver.resolve("example.com", 0))
        assert answer["port"] == 8080

    def test_a_foreign_host_is_refused_rather_than_resolved(self) -> None:
        """Fails CLOSED: a session reused for a second host must not silently get
        the first host's address, and OSError is what aiohttp reports as a
        connection error rather than a crash."""
        resolver = lu.PinnedResolver("example.com", ("93.184.216.34",), 443)
        with pytest.raises(OSError, match="refusing evil.example"):
            _run(resolver.resolve("evil.example", 443))

    def test_it_returns_the_six_keys_every_aiohttp_version_reads(self) -> None:
        """A plain dict, not `ResolveResult`: that name landed in aiohttp 3.10 and
        setup.cfg allows `aiohttp>=3.9`, so a runtime import would break the
        gateway's import on an allowed install."""
        resolver = lu.PinnedResolver("example.com", ("93.184.216.34",), 443)
        (answer,) = _run(resolver.resolve("example.com", 443))
        assert set(answer) == {"hostname", "host", "port", "family", "proto", "flags"}

    def test_close_is_a_no_op(self) -> None:
        # The connector closes its resolver; there is nothing to release.
        assert _run(lu.PinnedResolver("example.com", ("93.184.216.34",), 443).close()) is None

    def test_the_handler_installs_it_on_its_connector(self) -> None:
        """Pins the lift: the pin moved to `link_unfurl`, the handler still gets
        its connector from there, so the dashboard fetch is still pinned."""
        assert lm.pinned_connector is lu.pinned_connector

    def test_it_serves_EVERY_vetted_address_so_fallbacks_survive(self) -> None:
        """The regression this class exists to prevent.

        aiohttp dials the addresses a resolver returns IN ORDER and falls back to
        the next when a connection fails. A pin that answers with only the first
        address turns one dead CDN node -- or an AAAA record on a host with no
        working IPv6 route -- into a failed fetch that succeeded before the pin
        existed. Every address here passed the same internal-address check, so
        serving the whole set costs the vet nothing.
        """
        vetted = lu.vet_unfurl_url("https://cdn.example/o", resolve=_multi_homed)
        resolver = lu.PinnedResolver(vetted.wire_host, vetted.addresses, vetted.port)
        answers = _run(resolver.resolve(vetted.wire_host, vetted.port))
        assert [a["host"] for a in answers] == [
            "93.184.216.34",
            "93.184.216.35",
            "2606:4700::1111",
        ], "every vetted address, resolver order, the duplicate dropped"

    def test_each_address_reports_its_own_family(self) -> None:
        # One family for the whole set would announce the v6 node as AF_INET, and
        # aiohttp would fail that connection instead of falling back.
        resolver = lu.PinnedResolver("cdn.example", ("93.184.216.34", "2606:4700::1111"), 443)
        answers = _run(resolver.resolve("cdn.example", 443))
        assert [a["family"] for a in answers] == [socket.AF_INET, socket.AF_INET6]

    def test_no_addresses_is_refused_at_construction(self) -> None:
        """Fails CLOSED, and early: a resolver with nothing to serve answers every
        lookup with an empty list, which aiohttp reports as a connection error far
        from the cause."""
        with pytest.raises(ValueError, match="no addresses"):
            lu.PinnedResolver("example.com", (), 443)

    def test_an_idn_pin_matches_what_aiohttp_will_ask_for(self) -> None:
        """`wire_host`, not `host`: aiohttp asks its resolver with the IDNA form, so
        a pin on the unicode name would refuse every internationalized link."""
        vetted = lu.vet_unfurl_url("https://例え.jp/a", resolve=_public)
        resolver = lu.PinnedResolver(vetted.wire_host, (vetted.ip,), vetted.port)
        (answer,) = _run(resolver.resolve(vetted.wire_host, vetted.port))
        assert answer["host"] == "93.184.216.34"
        with pytest.raises(OSError):
            _run(resolver.resolve("例え.jp", 443))


class TestPinnedConnector:
    """One factory, so no caller can copy the pin and drop what makes it hold.

    `limit=1` and `family=AF_UNSPEC` are not decoration. A caller that installs the
    resolver but lets aiohttp narrow the family either loses every IPv6 target or
    gets a second lookup — which is the rebinding window the pin exists to close.
    Asserting them here is what keeps a future caller from re-opening it.
    """

    def _vetted(self, url: str = "https://example.com/a"):
        return lu.vet_unfurl_url(url, resolve=_public)

    def test_it_pins_the_connector_to_the_vetted_address(self) -> None:
        vetted = self._vetted()

        async def _check() -> None:
            connector = lu.pinned_connector(vetted)
            try:
                (answer,) = await connector._resolver.resolve(vetted.wire_host, vetted.port)
                assert answer["host"] == "93.184.216.34"
                assert answer["hostname"] == "example.com", "SNI and Host keep the name"
            finally:
                await connector.close()

        _run(_check())

    def test_it_serves_the_whole_vetted_set(self) -> None:
        """The factory is where the fallbacks are kept or lost, so assert it here
        too: a caller that reads `vetted.ip` builds a one-address pin."""
        vetted = lu.vet_unfurl_url("https://cdn.example/o", resolve=_multi_homed)

        async def _check() -> None:
            connector = lu.pinned_connector(vetted)
            try:
                answers = await connector._resolver.resolve(vetted.wire_host, vetted.port)
                assert [a["host"] for a in answers] == [
                    "93.184.216.34",
                    "93.184.216.35",
                    "2606:4700::1111",
                ]
            finally:
                await connector.close()

        _run(_check())

    def test_it_carries_the_settings_the_pin_needs(self) -> None:
        async def _check() -> None:
            connector = lu.pinned_connector(self._vetted())
            try:
                assert connector.limit == 1, "nothing else may ride this pinned pool"
                assert (
                    connector.family is socket.AF_UNSPEC
                ), "the resolver reports the family; aiohttp must not re-derive it"
                assert isinstance(connector._resolver, lu.PinnedResolver)
            finally:
                await connector.close()

        _run(_check())

    def test_an_idn_target_is_pinned_on_the_wire_form(self) -> None:
        """The factory reads `wire_host`, so an internationalized host is pinned on
        the IDNA name aiohttp will actually ask for -- a pin on the unicode form
        refuses every such fetch."""
        vetted = self._vetted("https://例え.jp/a")

        async def _check() -> None:
            connector = lu.pinned_connector(vetted)
            try:
                (answer,) = await connector._resolver.resolve(vetted.wire_host, vetted.port)
                assert answer["host"] == "93.184.216.34"
                with pytest.raises(OSError):
                    await connector._resolver.resolve("例え.jp", 443)
            finally:
                await connector.close()

        _run(_check())


# --- route registration ----------------------------------------------------


def test_route_is_registered_on_the_app() -> None:
    from aiohttp import web

    app = web.Application()
    lm.setup_link_meta_routes(app)
    routes = {(r.method, r.resource.canonical) for r in app.router.routes()}
    assert ("GET", "/api/link-meta") in routes


# --- address_is_not_public: the vet for a caller holding a resolution result ---
#
# The channels' attachment downloads consult this instead of enumerating category
# flags locally, so a gap here is a server-side request forgery in each of them.


@pytest.mark.parametrize(
    "address",
    [
        # The two ranges every hand-rolled category-flag list has missed. Both were
        # APPROVED by the check this function replaced in `teams/client.py`.
        "100.64.0.1",  # RFC 6598 shared space: absent from CPython's is_private
        "100.127.255.254",  # last usable address of the same block
        "fec0::1",  # deprecated IPv6 site-local: reports is_global True
        # Tunnel encodings, which that check evaluated as WRITTEN.
        "::ffff:127.0.0.1",  # v4-mapped loopback: is_loopback is False on the wrapper
        "::ffff:169.254.169.254",
        "2002:0a00:0001::1",  # 6to4 naming the v4 tunnel endpoint 10.0.0.1
        # Classics, which must stay refused.
        "127.0.0.1",
        "169.254.169.254",  # the cloud instance-metadata endpoint
        "10.1.2.3",
        "192.168.0.9",
        "::1",
        "fe80::1",
        "0.0.0.0",
        "224.0.0.1",
        "ff02::1",
        # Alternate IPv4 encodings the OS resolver accepts but `ipaddress` rejects,
        # folded by `canonicalize_ip` so the verdict does not ride on getaddrinfo's
        # reading of a string the vet declined to parse.
        "0x7f000001",
        "2130706433",
        "127.1",
    ],
)
def test_address_is_not_public_refuses_every_non_public_form(address: str) -> None:
    assert lu.address_is_not_public(address) is True


@pytest.mark.parametrize(
    "address",
    ["93.184.216.34", "1.1.1.1", "8.8.8.8", "2606:4700::1111"],
)
def test_address_is_not_public_allows_globally_routable_addresses(address: str) -> None:
    """A vet that refused real addresses would break every inbound attachment.

    The refusal half is only half the property: this is the half that keeps the
    fix from being a channel-wide outage.
    """
    assert lu.address_is_not_public(address) is False


@pytest.mark.parametrize("address", ["", "   ", "not-an-ip", "example.com", "::gg"])
def test_address_is_not_public_fails_closed_on_an_unreadable_value(address: str) -> None:
    """The OPPOSITE default to :func:`_reject_if_internal_ip`, deliberately.

    That function passes a non-literal through because for it a non-literal is a
    hostname still to be resolved. Here the value is already a resolution result
    about to reach a socket, so one that cannot be parsed must not be approved --
    otherwise an unreadable answer reaches the pin unchecked.
    """
    assert lu.address_is_not_public(address) is True


def test_the_url_vet_still_treats_a_non_literal_as_a_hostname() -> None:
    """Pins the refactor: extracting the shared helper must not change this contract.

    `_reject_if_internal_ip` returning silently for a hostname is what lets
    `vet_unfurl_url` fall through to its DNS branch. If the extraction had given it
    the fail-closed default, every named host would be refused.
    """
    lu._reject_if_internal_ip("example.com")  # must not raise
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu._reject_if_internal_ip("127.0.0.1")
    assert exc.value.code == "blocked_url"


@pytest.mark.parametrize("address", ["100.64.0.1", "fec0::1"])
def test_the_replaced_category_flag_list_would_have_approved_these(address: str) -> None:
    """Proves the finding's premise rather than assuming it.

    Reconstructs the exact six-flag check from `teams/client.py::_vet_resolved_address`
    and shows it says "fine" for both ranges. Without this, a future
    reader cannot tell whether the delegation fixed a real hole or was cosmetic.
    """
    ip = ipaddress.ip_address(address)
    old_verdict = (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )
    assert old_verdict is False, "the old check already caught this; premise is wrong"
    assert lu.address_is_not_public(address) is True


@pytest.mark.parametrize("title_source", ["title", "og:title"])
def test_endpoint_decodes_html_entities_once(monkeypatch, title_source):
    escaped = "How to spell &amp;lt; and &amp;#65;"
    title = (
        f"<title>{escaped}</title>"
        if title_source == "title"
        else (f'<meta property="og:title" content="{escaped}">')
    )
    html = (
        f"<head>{title}"
        f'<meta name="description" content="{escaped}">'
        f'<meta property="og:site_name" content="{escaped}">'
        '<link rel="icon" href="/icon.png?name=one&amp;amp;two">'
        "</head>"
    ).encode()
    expected_icon = "https://example.com/icon.png?name=one&amp;two"
    transport = _install(
        monkeypatch,
        {
            "https://example.com/": (200, _HTML_HEADERS, html),
            expected_icon: (200, {"Content-Type": "image/png"}, b"\x89PNG"),
        },
    )
    status, body = _run(_call("https://example.com/"))
    assert status == 200
    for field in ("title", "description", "site_name"):
        assert body[field] == "How to spell &lt; and &#65;"
    assert body["icon"].startswith("data:image/png;base64,")
    assert transport.fetch_count == 2


def test_a_url_the_redactors_would_change_is_never_previewed(monkeypatch) -> None:
    """A preview is a fetch nobody clicked, so a link whose query the exfil check
    blocks is refused even when a reader allowed its host for opening: it is
    neither fetched nor cached."""
    from kiro_crew.security.exfil import scoped_exempt_hosts

    _set_enabled(monkeypatch, True)
    transport = _install(monkeypatch, {})
    long_query = "https://reviews.corp.example/r?filter=" + "a" * 260
    with scoped_exempt_hosts(frozenset({"reviews.corp.example"})):
        status, body = _run(_call(long_query))
    assert status == 400
    assert body == {"code": "blocked_url"}
    assert transport.fetch_count == 0
    assert lm._cache_get(lm.normalize_cache_key(long_query)) is None
