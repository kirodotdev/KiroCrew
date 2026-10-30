"""Tests for the link-preview vet and extraction: what ``link_unfurl`` decides from a URL or a page.

The SSRF vet's scheme, host, port, IP-literal and DNS rules (every resolution goes
through an injected resolver, so no test opens a socket), metadata extraction, the
icon colour-scheme lanes and the icon data URI. The endpoint, login-gate titles, the
pinned resolver and connector and ``address_is_not_public`` are in
``test_link_unfurl.py``.
"""

from __future__ import annotations

import ipaddress
from typing import List, Tuple

import pytest
import yarl
from link_unfurl_helpers import _isolate  # noqa: F401
from link_unfurl_helpers import _public
from named_cases_helpers import named_cases

from kiro_crew import link_unfurl as lu

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


@pytest.mark.parametrize(
    "host",
    [
        *named_cases(
            "test_vet_rejects_blocked_suffixes",
            "host",
            ["secret.onion", "printer.local", "DEEP.SUB.ONION"],
        ),
        *named_cases(
            "test_vet_rejects_internal_ipv4_literals",
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
        ),
        *named_cases(
            "test_vet_rejects_internal_ipv6_literals",
            "host",
            [
                "[::1]",  # loopback
                "[::]",  # unspecified
                "[fe80::1]",  # link-local
                "[fc00::1]",  # unique local (private)
                "[fd12:3456::1]",  # unique local
                "[ff02::1]",  # multicast
            ],
        ),
        *named_cases(
            "test_vet_rejects_multicast_despite_is_global",
            "host",
            [
                "224.0.0.1",  # IPv4 multicast — CPython reports is_global True for these,
                "[ff02::1]",  # so `not is_global` alone would admit them
            ],
        ),
    ],
)
def test_vet_rejects_an_internal_or_blocked_host(host: str) -> None:
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url(f"http://{host}/", resolve=_public)
    assert exc.value.code == "blocked_url"


# --- vet: IP literals ------------------------------------------------------


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


@pytest.mark.parametrize("embedded", ["10.0.0.1", "127.0.0.1", "192.168.1.1", "169.254.169.254"])
def test_6to4_is_refused_by_unwrapping_not_by_cpythons_table(
    embedded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 6to4 address is judged by the IPv4 address it carries, on every 3.x.

    ``2002::/16`` entered CPython's IPv6 private table only with gh-113171
    (3.10.14, 3.11.9, 3.12.4). On an older patch release of a version this project
    supports, ``2002:0a00:0001::1`` reports ``is_global`` and every category flag
    clean, while the packet is routed to ``10.0.0.1``.

    The table-driven pin above lists ``2002::/16``, but on a fixed interpreter it
    passes through ``is_private`` alone and so cannot detect a missing unwrap. This
    drops that entry for the test's duration to reproduce the older behavior: what
    is asserted is that the refusal comes from unwrapping the embedded address, not
    from the interpreter agreeing with us.
    """
    v4 = ipaddress.ip_address(embedded)
    sixtofour = ipaddress.ip_address(f"2002:{int(v4) >> 16:04x}:{int(v4) & 0xFFFF:04x}::1")
    assert sixtofour.sixtofour == v4

    without_6to4 = tuple(
        net for net in ipaddress._IPv6Constants._private_networks if str(net) != "2002::/16"
    )
    monkeypatch.setattr(ipaddress._IPv6Constants, "_private_networks", without_6to4, raising=True)
    # The premise: with that entry gone the interpreter now calls it public, so a
    # check that consulted only the flags would let it through.
    assert ipaddress.ip_address(str(sixtofour)).is_global

    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url(f"https://[{sixtofour}]/x")
    assert exc.value.code == "blocked_url"


@pytest.mark.parametrize(
    "url",
    [
        "https://\ud800.example/",
        "https://ex\udcffample.test/",
        "https://\udfff/",
    ],
)
def test_a_host_the_resolver_cannot_encode_is_refused_not_raised(url: str) -> None:
    """A lone surrogate in the host is a refusal, not an uncaught ``UnicodeError``.

    ``getaddrinfo`` raises ``UnicodeError`` for these — a ``ValueError``, not an
    ``OSError`` — so the fail-closed catch around the resolver missed it and the
    exception escaped the vet. It reaches here intact because a JSON string can
    carry an unpaired surrogate, so this is an ordinary bad input rather than a
    contrived one: `link_meta` takes the URL from a request body, and the meetings
    calendar takes it from a config value.

    Refused rather than merely "not crashing": a host whose addresses were never
    checked cannot be declared safe to connect to.
    """
    with pytest.raises(lu.UnfurlRejected) as exc:
        lu.vet_unfurl_url(url)
    assert exc.value.code in {"blocked_url", "invalid_url"}


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


# --- icon colour-scheme lanes ----------------------------------------------


def test_dark_scoped_icon_goes_to_its_own_lane() -> None:
    """A dark-scheme icon must not be served as the default one.

    Using it on a light surface is the same invisible-glyph bug as using a
    light-mode icon on a dark surface, in mirror image.
    """
    html = (
        '<head><link rel="icon" href="/light.png" media="(prefers-color-scheme: light)">'
        '<link rel="icon" href="/dark.png" media="(prefers-color-scheme: dark)"></head>'
    )
    meta = lu.extract_meta(html, base_url="https://example.com/")
    assert meta.icon_candidates == (
        "https://example.com/light.png",
        "https://example.com/favicon.ico",
    )
    assert meta.dark_icon_candidates == ("https://example.com/dark.png",)


def test_unscoped_icon_stays_in_the_default_lane() -> None:
    """The overwhelmingly common shape: one icon, no media attribute."""
    html = '<head><link rel="icon" href="/i.png"></head>'
    meta = lu.extract_meta(html, base_url="https://example.com/")
    assert meta.icon_candidates[0] == "https://example.com/i.png"
    assert meta.dark_icon_candidates == ()


def test_dark_lane_has_no_favicon_fallback() -> None:
    """`/favicon.ico` is not a dark variant of anything, so it is not appended.

    Appending it would spend a request per link on bytes the default lane
    already fetched, and then hand the client the same picture twice.
    """
    html = '<head><link rel="icon" href="/i.png"></head>'
    meta = lu.extract_meta(html, base_url="https://example.com/")
    assert meta.dark_icon_candidates == ()


def test_dark_lane_drops_an_href_the_default_lane_already_has() -> None:
    # One href declared twice is not a variant; it is one picture.
    html = (
        '<head><link rel="icon" href="/i.png">'
        '<link rel="icon" href="/i.png" media="(prefers-color-scheme: dark)"></head>'
    )
    meta = lu.extract_meta(html, base_url="https://example.com/")
    assert meta.icon_candidates[0] == "https://example.com/i.png"
    assert meta.dark_icon_candidates == ()


def test_dark_scoped_apple_touch_icon_is_ordered_after_plain_icons() -> None:
    html = (
        '<head><link rel="apple-touch-icon" href="/apple-dark.png"'
        ' media="(prefers-color-scheme: dark)">'
        '<link rel="icon" href="/dark.png" media="screen and (prefers-color-scheme:dark)">'
        "</head>"
    )
    meta = lu.extract_meta(html, base_url="https://example.com/")
    assert meta.dark_icon_candidates == (
        "https://example.com/dark.png",
        "https://example.com/apple-dark.png",
    )


@pytest.mark.parametrize(
    "media,expected",
    [
        ("", ""),
        ("screen", ""),
        ("(prefers-color-scheme: dark)", "dark"),
        ("(prefers-color-scheme:DARK)", "dark"),
        ("screen and (prefers-color-scheme: light)", "light"),
        # A negation inverts the match, and this is a regex rather than a media
        # query engine — so it declines to guess and the icon stays unscoped.
        ("not all and (prefers-color-scheme: dark)", ""),
    ],
)
def test_icon_color_scheme_reads_only_what_it_can_prove(media: str, expected: str) -> None:
    assert lu.icon_color_scheme(media) == expected


def test_negated_dark_media_icon_stays_usable_as_the_default() -> None:
    """An icon this parser cannot classify stays in the default lane."""
    html = '<head><link rel="icon" href="/i.png" media="not all and (prefers-color-scheme: dark)">'
    meta = lu.extract_meta(html, base_url="https://example.com/")
    assert meta.icon_candidates[0] == "https://example.com/i.png"
    assert meta.dark_icon_candidates == ()


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
