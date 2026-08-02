"""Tests for the link-preview backend — SSRF vet, extraction, and the endpoint.

No test in this file opens a socket. The vet takes an injected resolver and the
handler's transport is swapped for :class:`_FakeTransport`, so every path
(redirect re-vetting, the read cap, icon rejection, cache/dedup) is exercised
against canned bytes.
"""

from __future__ import annotations

import asyncio
import ipaddress
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import pytest
import yarl

from kiro_crew import link_unfurl as lu
from kiro_crew.dashboard.handlers import link_meta as lm


def _run(coro):
    return asyncio.run(coro)


def _public(_host: str, _port: int) -> List[str]:
    """Resolver stub: everything resolves to one public address."""
    return ["93.184.216.34"]


# --- vet: scheme / host / port ---------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<b>x</b>",
        "example.com/no-scheme",
        "",
        "   ",
    ],
)
def test_vet_rejects_non_http_scheme(url: str) -> None:
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url(url, resolve=_public)
    assert exc.value.code == "invalid_url"


def test_vet_rejects_missing_host() -> None:
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url("http:///path", resolve=_public)
    assert exc.value.code == "invalid_url"


@pytest.mark.parametrize("port", [22, 25, 3306, 6379, 8080, 5476])
def test_vet_rejects_disallowed_port(port: int) -> None:
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url(f"http://example.com:{port}/", resolve=_public)
    assert exc.value.code == "blocked_url"


@pytest.mark.parametrize("url", ["http://example.com:80/", "https://example.com:443/"])
def test_vet_allows_explicit_default_ports(url: str) -> None:
    assert lu.vet_unfurl_url(url, resolve=_public).ip == "93.184.216.34"


def test_vet_rejects_malformed_port() -> None:
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url("http://example.com:notaport/", resolve=_public)
    assert exc.value.code == "invalid_url"


@pytest.mark.parametrize("host", ["secret.onion", "printer.local", "DEEP.SUB.ONION"])
def test_vet_rejects_blocked_suffixes(host: str) -> None:
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url(f"http://{host}/", resolve=_public)
    assert exc.value.code == "blocked_url"


# --- vet: IP literals ------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",  # loopback
        "127.1",  # short form
        "0x7f000001",  # hex
        "2130706433",  # 32-bit decimal
        "0177.0.0.1",  # octal
        "10.0.0.5",  # private
        "172.16.3.4",  # private
        "192.168.1.1",  # private
        "169.254.169.254",  # link-local (cloud metadata)
        "0xa9fea9fe",  # link-local, hex-encoded
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "0.0.0.0",  # unspecified
    ],
)
def test_vet_rejects_internal_ipv4_literals(host: str) -> None:
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url(f"http://{host}/", resolve=_public)
    assert exc.value.code == "blocked_url"


@pytest.mark.parametrize(
    "host",
    [
        "[::1]",  # loopback
        "[::]",  # unspecified
        "[fe80::1]",  # link-local
        "[fc00::1]",  # unique local (private)
        "[fd12:3456::1]",  # unique local
        "[ff02::1]",  # multicast
    ],
)
def test_vet_rejects_internal_ipv6_literals(host: str) -> None:
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url(f"http://{host}/", resolve=_public)
    assert exc.value.code == "blocked_url"


@pytest.mark.parametrize(
    "host",
    [
        "100.64.0.1",  # first usable address of the shared block
        "100.100.100.100",
        "100.127.255.254",  # last usable address of the shared block
    ],
)
def test_vet_rejects_rfc6598_shared_address_space(host: str) -> None:
    """`100.64.0.0/10` is not `is_private`, so an enumerated denylist misses it.

    This is the range a Tailscale tailnet and most carrier-grade NAT deployments
    assign, so admitting it means a model-emitted link can reach a service on the
    user's private overlay network. Guarding the whole class (`is_global`) rather
    than a list of categories is what keeps this closed.
    """
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url(f"http://{host}/", resolve=_public)
    assert exc.value.code == "blocked_url"


@pytest.mark.parametrize(
    "host",
    [
        "224.0.0.1",  # IPv4 multicast — CPython reports is_global True for these,
        "[ff02::1]",  # so `not is_global` alone would admit them
    ],
)
def test_vet_rejects_multicast_despite_is_global(host: str) -> None:
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url(f"http://{host}/", resolve=_public)
    assert exc.value.code == "blocked_url"


@pytest.mark.parametrize(
    "prefix",
    [
        # IPv4 special-purpose (RFC 6890 + the ones CPython classifies unevenly)
        "0.0.0.0/8",  # this host, this network
        "10.0.0.0/8",
        "100.64.0.0/10",  # RFC 6598 shared address space (CGNAT, Tailscale)
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local, incl. the cloud metadata address
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",  # TEST-NET-1
        "192.168.0.0/16",
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "224.0.0.0/4",  # multicast — `is_global` is True for these
        "240.0.0.0/4",  # reserved
        # IPv6 special-purpose
        "::/128",  # unspecified
        "::1/128",  # loopback
        "100::/64",  # discard-only
        "2001:db8::/32",  # documentation
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local
        "fec0::/10",  # DEPRECATED site-local — `is_global` is True for these
        "ff00::/8",  # multicast — `is_global` is True for these
        # IPv6 prefixes that EMBED an IPv4 address, and so reach v4 space from a
        # v6 literal. `64:ff9b::7f00:1` is 127.0.0.1 behind a NAT64 gateway —
        # `is_global` is True for it, which is exactly why the guard cannot rest
        # on that property alone.
        "64:ff9b::/96",  # RFC 6052 NAT64 well-known prefix
        "64:ff9b:1::/48",  # RFC 8215 NAT64 local-use prefix
        "2002::/16",  # 6to4
    ],
)
def test_vet_rejects_every_special_purpose_range(prefix: str) -> None:
    """Table-driven: no IANA special-purpose prefix may pass the vet.

    Two separate gaps were found one at a time by reviewers — `100.64.0.0/10`
    (absent from CPython's IPv4 private table) and `fec0::/10` (absent from its
    IPv6 one, so `is_global` reports True). Both classes of mistake come from
    trusting a single property, so this asserts the union over a table instead:
    a future CPython reclassification or a new special-purpose assignment fails
    HERE rather than in a review comment.

    The first, last and a middle address of each prefix are checked — a guard
    written against one representative address can still miss a boundary.
    """
    net = ipaddress.ip_network(prefix)
    first, last = net[0], net[-1]
    middle = net[int(net.num_addresses // 2)]
    for addr in (first, middle, last):
        host = f"[{addr}]" if addr.version == 6 else str(addr)
        with pytest.raises(lu.UnfurlRejected) as exc:
            lu.vet_unfurl_url(f"http://{host}/", resolve=_public)
        assert exc.value.code == "blocked_url", f"{addr} in {prefix} was not blocked"


@pytest.mark.parametrize("host", ["8.8.8.8", "[2606:4700::1111]", "93.184.216.34"])
def test_vet_allows_globally_routable_literals(host: str) -> None:
    """The allowlist must not be so tight that ordinary public addresses fail."""
    lu.vet_unfurl_url(f"http://{host}/", resolve=_public)


@pytest.mark.parametrize(
    "url,expected_wire",
    [
        ("https://例え.jp/", "xn--r8jz45g.jp"),  # IDN: the client asks in punycode
        ("http://example.com./", "example.com."),  # trailing dot is kept on the wire
        ("https://EXAMPLE.com/x", "example.com"),
        ("http://[2606:4700::1111]/", "2606:4700::1111"),
    ],
)
def test_wire_host_is_what_the_client_will_ask_the_resolver(url: str, expected_wire: str) -> None:
    """`wire_host` must equal `yarl.URL(...).raw_host`, or the pin never matches.

    The resolver pin is the mechanism that makes the whole vet meaningful, and it
    compares by hostname. aiohttp asks with `raw_host` (IDNA-encoded, trailing dot
    intact) while `host` is the display form, so pinning on `host` refuses every
    internationalized link with our own guard — a 502 plus a ten-minute negative
    cache entry. Asserting the two agree locks the invariant rather than the
    incidental encoding.
    """
    vetted = lu.vet_unfurl_url(url, resolve=_public)
    assert vetted.wire_host == expected_wire
    assert vetted.wire_host == yarl.URL(vetted.url).raw_host


@pytest.mark.parametrize(
    "url",
    [
        "https://key:secret@example.com/",
        "https://user@example.com/",
        "http://:secret@example.com/",
    ],
)
def test_vet_rejects_userinfo(url: str) -> None:
    """aiohttp turns URL userinfo into `Authorization: Basic`, so it must not pass.

    A model-emitted URL carrying credentials would send them to a model-chosen
    host and echo them into the gateway log through the cache-key debug line.
    """
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url(url, resolve=_public)
    assert exc.value.code == "invalid_url"


@pytest.mark.parametrize(
    "host",
    [
        "[::ffff:127.0.0.1]",
        "[::ffff:10.0.0.1]",
        "[::ffff:169.254.169.254]",
        "[0:0:0:0:0:ffff:127.0.0.1]",
        "[::ffff:7f00:1]",
    ],
)
def test_vet_unwraps_ipv4_mapped_ipv6(host: str) -> None:
    """The mapped form is a clean bypass unless unwrapped.

    ``IPv6Address('::ffff:127.0.0.1').is_loopback`` is False, and its
    ``is_private`` only consults the mapped address on Python 3.13+.
    """
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url(f"http://{host}/", resolve=_public)
    assert exc.value.code == "blocked_url"


def test_vet_allows_public_ip_literal_without_dns() -> None:
    def _explode(_h: str, _p: int) -> List[str]:
        raise AssertionError("an IP literal must not be resolved")

    vetted = lu.vet_unfurl_url("https://93.184.216.34/x", resolve=_explode)
    assert (vetted.ip, vetted.port, vetted.domain) == ("93.184.216.34", 443, "93.184.216.34")


def test_vet_canonicalizes_encoded_public_literal() -> None:
    """A public host in an alternate encoding must be dialled in dotted-quad."""
    vetted = lu.vet_unfurl_url("http://1568397574/", resolve=_public)
    assert vetted.ip == "93.123.217.6"


# --- vet: DNS --------------------------------------------------------------


def test_vet_rejects_host_resolving_to_loopback() -> None:
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url("http://rebind.example/", resolve=lambda h, p: ["127.0.0.1"])
    assert exc.value.code == "blocked_url"


def test_vet_rejects_when_any_answer_is_internal() -> None:
    """A split answer is a rebind attempt whichever address we happen to pick."""
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url(
            "http://split.example/", resolve=lambda h, p: ["93.184.216.34", "10.1.2.3"]
        )
    assert exc.value.code == "blocked_url"


def test_vet_rejects_unresolvable_host() -> None:
    def _fail(_h: str, _p: int) -> List[str]:
        raise OSError("NXDOMAIN")

    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url("http://nope.example/", resolve=_fail)
    assert exc.value.code == "blocked_url"


def test_vet_rejects_empty_resolution() -> None:
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url("http://nope.example/", resolve=lambda h, p: [])
    assert exc.value.code == "blocked_url"


def test_vet_resolves_exactly_once_and_pins_the_address() -> None:
    """The returned IP is what the caller dials — that is the rebind guard."""
    calls: List[Tuple[str, int]] = []

    def _counting(host: str, port: int) -> List[str]:
        calls.append((host, port))
        return ["93.184.216.34"]

    vetted = lu.vet_unfurl_url("https://Example.COM./page?q=1#frag", resolve=_counting)
    assert calls == [("example.com", 443)]
    assert vetted.ip == "93.184.216.34"
    assert vetted.host == "example.com"
    assert vetted.url == "https://Example.COM./page?q=1"  # fragment dropped


def test_vet_strips_www_for_display_domain() -> None:
    vetted = lu.vet_unfurl_url("https://www.Example.com/", resolve=_public)
    assert vetted.domain == "example.com"
    assert vetted.host == "www.example.com"


# --- extraction ------------------------------------------------------------


def test_extract_prefers_open_graph() -> None:
    html = """
    <html><head>
      <title>Tag title</title>
      <meta property="og:title" content="OG title">
      <meta name="description" content="Name description">
      <meta property="og:description" content="OG description">
      <meta property="og:site_name" content="Site">
      <link rel="icon" href="/i.png">
    </head><body>x</body></html>
    """
    meta = lu.extract_meta(html, base_url="https://example.com/a/b")
    assert meta.title == "OG title"
    assert meta.description == "OG description"
    assert meta.site_name == "Site"
    assert meta.icon_candidates[0] == "https://example.com/i.png"


def test_extract_falls_back_to_title_tag_and_meta_description() -> None:
    html = (
        "<head><title>  Spaced\n  title </title>"
        '<meta name="Description" content="Plain &amp; escaped"></head>'
    )
    meta = lu.extract_meta(html, base_url="https://example.com/")
    assert meta.title == "Spaced title"
    assert meta.description == "Plain & escaped"
    assert meta.site_name == ""


def test_extract_caps_lengths() -> None:
    html = f"<head><title>{'t' * 900}</title>" f'<meta name="description" content="{"d" * 900}">'
    meta = lu.extract_meta(html, base_url="https://example.com/")
    assert len(meta.title) == lu.TITLE_MAX_CHARS
    assert len(meta.description) == lu.DESCRIPTION_MAX_CHARS


def test_extract_icon_order_and_favicon_fallback() -> None:
    html = (
        '<head><link rel="apple-touch-icon" href="/apple.png">'
        '<link rel="shortcut icon" href="sub/fav.png"></head>'
    )
    meta = lu.extract_meta(html, base_url="https://example.com/dir/page")
    assert meta.icon_candidates == (
        "https://example.com/dir/sub/fav.png",
        "https://example.com/apple.png",
        "https://example.com/favicon.ico",
    )


def test_extract_favicon_only_when_no_link_tag() -> None:
    meta = lu.extract_meta("<head></head>", base_url="https://example.com/deep/page")
    assert meta.icon_candidates == ("https://example.com/favicon.ico",)


def test_extract_drops_non_http_icon_href() -> None:
    """A data: icon href would skip the type allowlist and the size cap."""
    html = '<head><link rel="icon" href="data:image/svg+xml,<svg/>"></head>'
    meta = lu.extract_meta(html, base_url="https://example.com/")
    assert meta.icon_candidates == ("https://example.com/favicon.ico",)


def test_extract_ignores_head_content_after_body() -> None:
    html = "<head><title>Real</title></head><body><title>Injected</title></body>"
    assert lu.extract_meta(html, base_url="https://example.com/").title == "Real"


def test_extract_survives_garbage() -> None:
    meta = lu.extract_meta("<<<>>> not html at all \x00", base_url="https://example.com/")
    assert meta.title == ""


def test_decode_html_uses_header_then_meta_then_utf8() -> None:
    assert "café" in lu.decode_html("café".encode("latin-1"), "text/html; charset=latin-1")
    body = b'<meta charset="latin-1"><title>caf\xe9</title>'
    assert "café" in lu.decode_html(body, "text/html")
    assert "caf\ufffd" in lu.decode_html(b"caf\xe9", "text/html")


def test_decode_html_tolerates_unknown_charset() -> None:
    assert lu.decode_html(b"hello", "text/html; charset=not-a-codec") == "hello"


# --- icon data URI ---------------------------------------------------------


def test_icon_svg_is_rejected() -> None:
    assert lu.build_icon_data_uri("image/svg+xml", b"<svg/>") == ""
    assert lu.build_icon_data_uri("image/svg+xml; charset=utf-8", b"<svg/>") == ""


@pytest.mark.parametrize(
    "content_type", ["text/html", "application/octet-stream", "image/avif", ""]
)
def test_icon_non_allowlisted_type_is_rejected(content_type: str) -> None:
    assert lu.build_icon_data_uri(content_type, b"\x89PNG") == ""


def test_icon_oversized_is_dropped_not_truncated() -> None:
    assert lu.build_icon_data_uri("image/png", b"x" * (lu.MAX_ICON_BYTES + 1)) == ""
    assert lu.build_icon_data_uri("image/png", b"x" * lu.MAX_ICON_BYTES).startswith(
        "data:image/png;base64,"
    )


def test_icon_empty_body_is_dropped() -> None:
    assert lu.build_icon_data_uri("image/png", b"") == ""


def test_icon_data_uri_shape() -> None:
    uri = lu.build_icon_data_uri("image/x-icon; charset=binary", b"AB")
    assert uri == "data:image/x-icon;base64,QUI="


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


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Fresh cache per test, feature ON, DNS stubbed to one public address."""
    lm.reset_link_meta_cache()
    monkeypatch.setattr(lu, "_default_resolve", _public)
    _set_enabled(monkeypatch, True)
    yield
    lm.reset_link_meta_cache()


def _set_enabled(monkeypatch, enabled: bool) -> None:
    """Patch the config loader the handler imports at call time."""
    from kiro_crew.config import loader as loader_mod

    cfg = SimpleNamespace(dashboard=SimpleNamespace(link_previews=enabled))
    monkeypatch.setattr(loader_mod.KiroCrewConfig, "load", staticmethod(lambda: cfg))


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


# --- endpoint: body caps ---------------------------------------------------


def test_oversized_body_with_lying_content_length(monkeypatch) -> None:
    """A truthful-looking small Content-Length must not raise the real bound."""
    transport = _install(
        monkeypatch,
        {
            "https://example.com/big": (
                200,
                {"Content-Type": "text/html", "Content-Length": "42"},
                b"<title>x</title>" + b"A" * (lu.MAX_BODY_BYTES + 4096),
            )
        },
    )
    status, body = _run(_call("https://example.com/big"))
    assert (status, body) == (502, {"code": "fetch_failed"})
    assert transport.fetch_count == 1


def test_honest_oversized_content_length_short_circuits(monkeypatch) -> None:
    _install(
        monkeypatch,
        {
            "https://example.com/big": (
                200,
                {"Content-Type": "text/html", "Content-Length": str(lu.MAX_BODY_BYTES + 1)},
                b"tiny",
            )
        },
    )
    status, body = _run(_call("https://example.com/big"))
    assert (status, body) == (502, {"code": "fetch_failed"})


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


# --- route registration ----------------------------------------------------


def test_route_is_registered_on_the_app() -> None:
    from aiohttp import web

    app = web.Application()
    lm.setup_link_meta_routes(app)
    routes = {(r.method, r.resource.canonical) for r in app.router.routes()}
    assert ("GET", "/api/link-meta") in routes
