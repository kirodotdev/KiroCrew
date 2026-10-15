"""Mermaid pako links survive only the bare-secret pass and only after validation."""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import re
import time
import zlib
from typing import Any

import pytest

from conftest import REDOS_SCALING_FLOOR_SECONDS, REDOS_SCALING_RATIO
from kiro_crew.security import StreamRedactor, redact, redact_credentials
from kiro_crew.security import redaction as _redaction
from kiro_crew.security import text_redaction as _text_redaction

KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
SEGMENT_KEY = "wJalrXUtnFEMIzK7MDENGqbPxRfiCYEXAMPLEKEY"
TAG = "[REDACTED: credential]"
PREFIX = "https://mermaid.live/edit#pako:"


def _encode_state(state: bytes) -> str:
    return base64.urlsafe_b64encode(zlib.compress(state, 9)).decode().rstrip("=")


def _payload(code: str) -> str:
    state = json.dumps({"code": code}, separators=(",", ":")).encode()
    return _encode_state(state)


def _url(code: str) -> str:
    return PREFIX + _payload(code)


def _raw_state_url(state: str) -> str:
    return PREFIX + _encode_state(state.encode())


def _incompressible_url(code_chars: int) -> str:
    raw = b"".join(hashlib.sha256(i.to_bytes(4, "big")).digest() for i in range(2_200))
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    code = " ".join(encoded[i : i + 20] for i in range(0, len(encoded), 20))[:code_chars]
    return _url(code)


def _near_cap_url() -> str:
    url = _incompressible_url(64_700)
    payload = url[len(PREFIX) :]
    assert _redaction._MERMAID_PAKO_PAYLOAD_CAP - 128 < len(payload)
    assert len(payload) <= _redaction._MERMAID_PAKO_PAYLOAD_CAP
    return url


def _stream(text: str, width: int) -> str:
    redactor = StreamRedactor()
    return (
        "".join(redactor.feed(text[i : i + width]) for i in range(0, len(text), width))
        + redactor.flush()
    )


def _key_shaped_url(code: str) -> str:
    for salt in range(2_000):
        candidate = _url(f"{code}\n%% {salt}")
        if _redaction._text_contains_bare_secret(candidate):
            return candidate
    raise AssertionError("fixture did not produce a pass-3 candidate")


def test_valid_pass_3_candidate_survives_batch_and_streams_byte_for_byte() -> None:
    code = "flowchart TD\n" + "\n".join(f"  A{i} --> B{i}: label {i}" for i in range(300))
    url = _key_shaped_url(code)

    assert len(url) > 512
    assert _redaction._text_contains_bare_secret(url)
    assert redact_credentials(url) == (url, [])
    for width in (1, 7, 31, 511):
        assert _stream(url, width) == url


def test_terminating_slash_cannot_split_a_secret_across_pako_state() -> None:
    slash = KEY.index("/")
    redactor = StreamRedactor()

    first = redactor.feed(PREFIX + KEY[:slash])
    second = redactor.feed(KEY[slash:] + " done")
    output = first + second + redactor.flush()

    assert KEY not in output
    assert output == PREFIX + TAG + " done"


def _one_byte_stream_cpu(url: str, *, readings: int, settle_below: float) -> float:
    """Cheapest thread CPU of up to ``readings`` one-byte streams of ``url``,
    stopping at the first one under ``settle_below``; every stream must
    reproduce ``url`` exactly."""
    best = float("inf")
    for _ in range(readings):
        redactor = StreamRedactor()
        started = time.thread_time()
        parts = [redactor.feed(char) for char in url]
        parts.append(redactor.flush())
        best = min(best, time.thread_time() - started)
        assert "".join(parts) == url
        if best < settle_below:
            break
    return best


def test_near_cap_one_byte_stream_stays_linear_in_cpu() -> None:
    """A near-cap link streamed one byte at a time costs about 10x a link a
    tenth its size (0.2 s plain, 0.74 s under coverage on a dev host; CI runners
    are ~3x slower), held to ``REDOS_SCALING_RATIO`` per 10x above the floor, as
    ``conftest.assert_rejected_without_backtracking`` holds its long pumps. A
    quadratic rescan of the held candidate grows ~100x and is refused on any
    runner. The baseline keeps all three readings: an inflated one would lift
    the bound and hide a regression. The near-cap size takes a second reading
    only after the first overran the bound."""
    tenth = _one_byte_stream_cpu(_incompressible_url(6_470), readings=3, settle_below=0.0)
    bound = max(REDOS_SCALING_RATIO * tenth, REDOS_SCALING_FLOOR_SECONDS)
    near_cap = _one_byte_stream_cpu(_near_cap_url(), readings=2, settle_below=bound)
    assert near_cap <= bound, (
        f"a near-cap pako link cost {near_cap:.3f}s of thread CPU against {tenth:.4f}s "
        f"at a tenth its size -- past the {REDOS_SCALING_RATIO}x a linear stream stays under"
    )


def _long_link() -> str:
    code = "flowchart TD\n" + "\n".join(f"  A{i} --> B{i}: label {i}" for i in range(300))
    url = _key_shaped_url(code)
    assert len(url) > 2_000
    return url


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _jwt() -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = _b64url(json.dumps({"sub": "kc-pako-stream", "iat": 1}).encode())
    return f"{header}.{claims}.{_b64url(hashlib.sha256(b'kc-pako-jwt').digest())}"


@functools.lru_cache(maxsize=2)
def _jwt_bearing_link(within: int = 2_048) -> str:
    """A valid link past the 4,096-char JWT ceiling with `eyJ` in its first *within* payload chars.

    Base64url of compressed bytes holds `eyJ` by chance, so the fixture searches
    salted diagrams for one; the search adapts to whichever zlib build runs it.
    """
    for salt in range(20_000):
        lines = []
        for i in range(300):
            digest = hashlib.sha256(f"{salt}:{i}".encode()).hexdigest()
            lines.append(f"  N{i}[{digest[:8]} {digest[8:16]} {digest[16:24]}]")
        url = _url("flowchart TD\n" + "\n".join(lines))
        at = url.find("eyJ", len(PREFIX))
        if len(PREFIX) <= at < len(PREFIX) + within and redact_credentials(url) == (url, []):
            assert len(url) > 6_000
            return url
    raise AssertionError("fixture did not produce a payload carrying eyJ")


def _chunks(text: str, width: int = 7) -> list[str]:
    return [text[i : i + width] for i in range(0, len(text), width)]


def _stream_chunks(chunks: list[str]) -> str:
    redactor = StreamRedactor()
    return "".join(redactor.feed(chunk) for chunk in chunks) + redactor.flush()


@pytest.mark.parametrize(
    "deltas",
    [["."], [".", " done"], [":", "/", " next"], [".", " ", "next"]],
)
def test_credential_class_delta_after_link_streams_like_batch(deltas: list[str]) -> None:
    url = _long_link()
    chunks = ["See ", *_chunks(url), *deltas]
    text = "".join(chunks)

    assert redact_credentials(text)[0] == text
    assert _stream_chunks(chunks) == text


@pytest.mark.parametrize("after", [[" done"], []])
@pytest.mark.parametrize("tail", [".", ":", "...:"])
def test_a_link_and_its_punctuation_in_one_delta_stream_like_batch(
    tail: str, after: list[str]
) -> None:
    # The delta ends on punctuation, so the partial route (payload to the end)
    # misses it; the tail must be read off before the candidate is recognized.
    url = _long_link()
    chunks = [f"See {url}{tail}", *after]
    text = "".join(chunks)

    assert redact(text) == text
    assert _stream_chunks(chunks) == text


def test_a_credential_after_punctuation_in_the_same_delta_is_still_redacted() -> None:
    url = _long_link()
    chunks = [f"See {url}.{KEY}", " done"]
    expected = redact("".join(chunks))

    assert KEY not in expected and url in expected
    assert _stream_chunks(chunks) == expected


@pytest.mark.parametrize("glue", [".", " "])
def test_credential_after_held_link_tail_is_still_redacted(glue: str) -> None:
    url = _long_link()
    chunks = ["See ", *_chunks(url), glue, *_chunks(KEY, 5), " done"]
    expected = redact_credentials("".join(chunks))[0]

    assert expected == f"See {url}{glue}{TAG} done"
    assert _stream_chunks(chunks) == expected


@pytest.mark.parametrize("lead", ['href="', '{"url":"', "password="])
def test_credential_class_lead_glued_to_link_streams_like_batch(lead: str) -> None:
    url = _long_link()
    chunks = [f"See {lead}", *_chunks(url), '" done']
    text = "".join(chunks)

    assert redact_credentials(text)[0] == text
    assert _stream_chunks(chunks) == text


_OPAQUE_BEARER = "opaqueTok" + "Q7x" * 12


@pytest.mark.parametrize("width", [1_000, 2_048, None])
@pytest.mark.parametrize(
    "header",
    ["Authorization: Bearer {}/", '{{"authorization": "bearer {}/', "authorization:bearer {}/"],
    ids=["header", "json", "lowercase"],
)
@pytest.mark.parametrize("token", [_OPAQUE_BEARER, _jwt()], ids=["opaque", "jwt"])
def test_a_bearer_header_glued_to_a_link_is_redacted_with_its_value(
    token: str, header: str, width: int | None
) -> None:
    # The scheme's `:` ends the Bearer value, so the natural cut falls after
    # `Bearer `; the pako hold must not commit the header without its value.
    text = header.format(token) + _long_link()
    chunks = [text] if width is None else _chunks(text, width)
    expected = redact(text + " done")
    live = _stream_chunks([*chunks, " done"])

    assert token not in expected
    assert token not in live
    assert live.split("://", 1)[0] == expected.split("://", 1)[0]


@pytest.mark.parametrize("width", [7, 1_000, None])
@pytest.mark.parametrize(
    "lead",
    ["The diagram is at ", "Authorization: granted, see ", f"Authorization: {_OPAQUE_BEARER}/"],
)
def test_a_credential_free_sentence_ending_in_a_link_streams_intact(
    lead: str, width: int | None
) -> None:
    text = lead + _long_link()
    chunks = [text] if width is None else _chunks(text, width)

    assert redact(text) == text
    assert _stream_chunks(chunks) == text


@pytest.mark.parametrize("width", [7, 2_048, 3_000, 4_096])
@pytest.mark.parametrize("within", [400, 2_048], ids=["eyj-early", "eyj-late"])
@pytest.mark.parametrize("token", [_OPAQUE_BEARER, _jwt()], ids=["opaque", "jwt"])
def test_a_bearer_header_glued_to_a_jwt_bearing_link_never_streams_its_value(
    token: str, within: int, width: int
) -> None:
    # The scheme's `:` lets the header commit in an earlier delta; the payload's
    # `eyJ` must then fail closed on the JWT ceiling rather than ride the hold
    # with the token glued in front of the route, also when the `eyJ` arrives in
    # a delta after the one that began the hold.
    text = f"Authorization: Bearer {token}/" + _jwt_bearing_link(within)

    assert token not in _stream_chunks([*_chunks(text, width), " done"])


@pytest.mark.parametrize("within", [400, 2_048], ids=["eyj-early", "eyj-late"])
def test_an_eyj_ending_a_delta_still_ends_the_hold(within: int) -> None:
    # The byte after `eyJ` decides whether the run ends in a partial JWT, so a
    # delta ending on `eyJ` keeps the hold until that byte arrives in the next.
    text = f"Authorization: Bearer {_OPAQUE_BEARER}/" + _jwt_bearing_link(within)
    cut = text.index("eyJ", text.index(PREFIX)) + 3
    live = _stream_chunks([*_chunks(text[:cut]), *_chunks(text[cut:]), " done"])

    assert _OPAQUE_BEARER not in live


@pytest.mark.parametrize("width", [7, None], ids=["small-deltas", "one-delta"])
@pytest.mark.parametrize("lead", ['href="', ""], ids=["glued", "bare"])
def test_a_label_glued_to_a_jwt_bearing_link_fails_closed(lead: str, width: int | None) -> None:
    # Accepted cost: glued text ahead of the route refuses the partial-JWT ride,
    # so the run is masked by the JWT ceiling as before the pako hold existed,
    # also when the `eyJ` arrives after the hold began.
    url = _jwt_bearing_link()
    payload = url[len(PREFIX) :]
    chunks = [f"See {lead}{url}"] if width is None else [f"See {lead}", *_chunks(url, width)]
    live = _stream_chunks([*chunks, '" done'])

    if lead:
        assert live == f"See {TAG}" + '" done'
        assert payload[:64] not in live
    else:
        assert live == f'See {url}" done'


# The three tests below compare live output with `redact`, the composed batch
# pass the stream redactor applies to each committed window.


@pytest.mark.parametrize("width", [4_096, 1_000_000])
def test_partial_jwt_inside_a_valid_payload_streams_like_batch(width: int) -> None:
    # The first delta carries the payload's `eyJ`, which reads as a partial JWT
    # at the buffer end; it must ride the pako hold instead of the JWT ceiling.
    url = _jwt_bearing_link()
    chunks = [*_chunks(f"See {url}", width), " done"]
    text = "".join(chunks)

    assert redact_credentials(text) == (text, [])
    assert redact(text) == text
    assert _stream_chunks(chunks) == text


@pytest.mark.parametrize("width", [7, 4_096])
@pytest.mark.parametrize("where", ["before", "after"])
def test_a_jwt_beside_a_jwt_bearing_link_is_still_redacted_live(where: str, width: int) -> None:
    url, jwt = _jwt_bearing_link(), _jwt()
    text = f"token {jwt} see {url} done" if where == "before" else f"see {url} token {jwt} done"
    expected = redact(text)

    assert jwt not in expected and url in expected
    assert _stream_chunks(_chunks(text, width)) == expected


def test_a_dot_ends_the_pako_hold_and_a_jwt_spanning_it_is_redacted() -> None:
    jwt = _jwt()
    header, rest = jwt.split(".", 1)
    text = f"see {PREFIX}{jwt} done"
    expected = redact(text)

    assert header not in expected and rest not in expected
    for chunks in ([f"see {PREFIX}{header}", f".{rest}", " done"], _chunks(text), [text]):
        assert _stream_chunks(chunks) == expected


@functools.lru_cache(maxsize=1)
def _two_eyj_link() -> str:
    """A valid link whose payload holds two `eyJ` runs, over 4,096 chars after the first.

    The second `eyJ` lets `_partial_jwt_tail` accept the first one's header, so
    a trailing `.` makes the match run from inside the payload into its tail.
    """
    for salt in range(20_000):
        lines = []
        for i in range(1_500):
            digest = hashlib.sha256(f"{salt}:{i}".encode()).hexdigest()
            lines.append(f"  N{i}[{digest[:8]} {digest[8:16]} {digest[16:24]}]")
        url = _url("flowchart TD\n" + "\n".join(lines))
        first = url.find("eyJ", len(PREFIX))
        if (
            first >= 0
            and url.find("eyJ", first + 3) >= 0
            and len(url) - first > 4_096
            and redact_credentials(url) == (url, [])
        ):
            return url
    raise AssertionError("fixture did not produce a payload carrying eyJ twice")


@pytest.mark.parametrize("after", [[], [" done"]])
@pytest.mark.parametrize("width", [4_096, 10_000, 1_000_000])
def test_a_jwt_shaped_payload_and_its_punctuation_in_one_delta_stream_like_batch(
    width: int, after: list[str]
) -> None:
    # The partial JWT starts inside the payload and ends on the sentence's `.`,
    # which arrives in the payload's own delta: it is held with the link.
    url = _two_eyj_link()
    chunks = [*_chunks(f"See {url}.", width), *after]
    text = "".join(chunks)

    assert redact(text) == text
    assert _stream_chunks(chunks) == text


@pytest.mark.parametrize(("glue", "keeps_link"), [(". ", True), (".\n", True), (".", False)])
def test_a_jwt_after_a_jwt_shaped_link_and_its_punctuation_is_still_redacted(
    glue: str, keeps_link: bool
) -> None:
    # Glued to the dot, the JWT joins the link's run and batch voids the link.
    url, jwt = _two_eyj_link(), _jwt()
    text = f"See {url}{glue}{jwt} done"
    expected = redact(text)

    assert jwt not in expected and (url in expected) is keeps_link
    for chunks in ([f"See {url}{glue}{jwt}", " done"], [f"See {url}{glue}", jwt, " done"], [text]):
        assert _stream_chunks(chunks) == expected


@pytest.mark.parametrize("width", [64, 1_000_000])
def test_a_partial_jwt_past_the_held_tail_still_fails_closed(width: int) -> None:
    # Only the bounded tail may carry the match; past it the JWT ceiling drops
    # the run rather than streaming its head.
    url = _two_eyj_link()
    tail = "." + "A" * 600
    chunks = [f"See {url}", *_chunks(tail, width), " done"]

    assert _stream_chunks(chunks) == f"See {TAG} done"


class _HandedCharacters:
    """A compiled pattern that adds up the characters each ``search`` is handed."""

    def __init__(self, real: re.Pattern[str]) -> None:
        self._real = real
        self.handed = 0

    def search(self, text: str, pos: int = 0) -> re.Match[str] | None:
        self.handed += len(text) - pos
        return self._real.search(text, pos)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _terminated_jwt_prefix_run_tail_scan(monkeypatch: pytest.MonkeyPatch, repeats: int) -> int:
    """Characters the partial-JWT tail search reads while a held run of ``repeats``
    ``eyJ`` prefixes streams in 64-character deltas until a space ends it."""
    payload = "A" + "eyJ" * repeats
    counted = _HandedCharacters(_text_redaction._PARTIAL_JWT_TAIL_RE)
    with monkeypatch.context() as patch:
        patch.setattr(_text_redaction, "_PARTIAL_JWT_TAIL_RE", counted)
        redactor = StreamRedactor()
        parts = [redactor.feed("x " + PREFIX)]
        parts.extend(redactor.feed(chunk) for chunk in _chunks(payload, 64))
        parts.append(redactor.feed(" done"))
        parts.append(redactor.flush())

    assert "".join(parts) == redact_credentials(f"x {PREFIX}{payload} done")[0]
    return counted.handed


def test_terminated_held_candidate_of_jwt_prefixes_scans_only_its_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tail search starts where a match can begin, so a longer held run hands it no
    more characters. A search from the buffer start reads the whole run and retries at
    every ``eyJ`` in it, which is quadratic. Counted, so the runner cannot decide it."""
    short = _terminated_jwt_prefix_run_tail_scan(monkeypatch, 1_000)
    long = _terminated_jwt_prefix_run_tail_scan(monkeypatch, 4_000)

    assert long == short, f"the tail search read {long} characters of a 4x run, {short} of 1x"


@pytest.mark.parametrize(
    "rewrite",
    [
        lambda url: url.replace("https://", "http://", 1),
        lambda url: url.replace("mermaid.live", "MERMAID.LIVE", 1),
        lambda url: url.replace("/edit#", "/view#", 1),
        lambda url: url.replace("mermaid.live", "mermaid.live:443", 1),
        lambda url: url + "=",
        lambda url: url + "/suffix",
    ],
)
def test_route_and_encoding_variants_earn_no_exemption(rewrite) -> None:
    assert _redaction._document_link_spans(rewrite(_url("flowchart TD\n  A --> B"))) == []


@pytest.mark.parametrize("payload", ["A" * 80, "eJw", "____", "abcdA"])
def test_invalid_payload_earns_no_exemption(payload: str) -> None:
    assert _redaction._document_link_spans(PREFIX + payload) == []


@pytest.mark.parametrize(
    "state",
    [
        r'{"code":"flowchart TD","aws\u005faccess\u005fkey\u005fid":"opaque"}',
        r'{"code":"flowchart TD","Authoriz\u0061tion":"Bearer opaque-token"}',
    ],
)
def test_escaped_json_credential_composition_earns_no_exemption(state: str) -> None:
    assert _redaction._document_link_spans(_raw_state_url(state)) == []


def _escaped_tab_bearer_url() -> str:
    """A pass-3 candidate whose `Authorization` value is `Bearer`, a JSON-escaped
    tab and a short opaque token."""
    for salt in range(2_000):
        state = (
            '{"code":"flowchart TD\\n  A --> B\\n%% ' + str(salt) + '",'
            '"Authorization":"Bearer\\tq7Zp2xK"}'
        )
        url = _raw_state_url(state)
        if _redaction._text_contains_bare_secret(url):
            return url
    raise AssertionError("fixture did not produce a pass-3 candidate")


@pytest.mark.parametrize("width", [7, 4_096])
def test_an_escaped_tab_cannot_split_a_bearer_value_from_its_label(width: int) -> None:
    # Both JSON spellings keep the tab escaped, so neither shows `Bearer` and the
    # token apart, and the decoded value alone has lost its `Authorization` label.
    url = _escaped_tab_bearer_url()
    text = f"See {url} done"
    expected = redact(text)

    assert _redaction._document_link_spans(url) == []
    assert url[len(PREFIX) :] not in expected and TAG in expected
    assert _stream_chunks(_chunks(text, width)) == expected


def test_decompression_bomb_earns_no_exemption() -> None:
    url = _url("flowchart TD\n  A[" + "x" * (_redaction._MERMAID_PAKO_SOURCE_CAP * 4) + "]")

    assert _redaction._document_link_spans(url) == []


@functools.lru_cache(maxsize=2)
def _views_url(views: int) -> str:
    """A credential-free pass-3 candidate whose state yields *views* decoded views:
    its JSON, the keys `code` and `items`, the `code` string alone and beside its
    key, and one per item."""
    items = [f"n{i}" for i in range(views - 5)]
    for salt in range(2_000):
        state = {"code": f"flowchart TD\n  A --> B\n%% {salt}", "items": items}
        url = _raw_state_url(json.dumps(state, separators=(",", ":")))
        if _redaction._text_contains_bare_secret(url):
            return url
    raise AssertionError("fixture did not produce a pass-3 candidate")


def test_a_state_at_the_view_cap_keeps_its_link() -> None:
    url = _views_url(_redaction._MERMAID_PAKO_VIEW_CAP)
    payload = url[len(PREFIX) :]
    views, _ = _redaction._mermaid_pako_views(payload, _redaction._MERMAID_PAKO_SOURCE_CAP)

    assert views is not None and len(views) == _redaction._MERMAID_PAKO_VIEW_CAP
    assert redact_credentials(url) == (url, [])


@pytest.mark.parametrize("width", [7, 4_096])
def test_a_state_past_the_view_cap_fails_closed(width: int) -> None:
    # Each view costs one composed pass, so a state carrying more than the cap
    # fails closed and pass 3 judges its payload like any other run.
    url = _views_url(_redaction._MERMAID_PAKO_VIEW_CAP + 1)
    text = f"See {url} done"
    expected = redact(text)

    assert _redaction._document_link_spans(url) == []
    assert url[len(PREFIX) :] not in expected and TAG in expected
    assert _stream_chunks(_chunks(text, width)) == expected


@pytest.mark.parametrize(
    "code",
    [
        "flowchart TD\n  A[AKIAIOSFODNN7EXAMPLE]",
        "flowchart TD\n  A["
        + base64.b64encode(f"aws_secret_access_key={KEY}".encode()).decode()
        + "]",
        f"flowchart TD\n  A[{SEGMENT_KEY}]",
        "flowchart TD\n  A[https://example.com/hook?token=Zq81xLmP0vR7tYc2]",
    ],
)
def test_every_standard_credential_pass_scans_decoded_source(code: str) -> None:
    assert _redaction._document_link_spans(_url(code)) == []


# The decoded views are judged by `redact`, the composed pass the stream applies
# to each window, so the URL pass's percent-decoding sees a link in the diagram.
GITHUB_PAT_BODY = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


@pytest.mark.parametrize(
    "target",
    [
        pytest.param(f"https://example.com/hook/ghp%5F{GITHUB_PAT_BODY}", id="github-pat-%5F"),
        pytest.param("https://example.com/upload/AKIA%49OSFODNN7EXAMPLE", id="aws-key-id-%49"),
        pytest.param("https://example.com/upload/AKIA%2549OSFODNN7EXAMPLE", id="aws-key-id-%2549"),
    ],
)
@pytest.mark.parametrize("width", [7, 4_096])
def test_percent_encoded_credential_in_a_decoded_url_voids_the_link(
    target: str, width: int
) -> None:
    assert redact(target) != target  # the URL pass flags it once decoded
    url = _key_shaped_url(f"flowchart TD\n  A[{target}]")
    text = f"See {url} done"
    expected = redact(text)

    assert _redaction._document_link_spans(url) == []
    assert url not in expected
    assert _stream_chunks(_chunks(text, width)) == expected


# The URL pass skips an upper-case scheme and a fragment right after the host,
# so the check also judges each view with those normalized and its escapes decoded.
# A multiply-encoded PAT escapes one body character too, so no 40-char run of it
# fires pass 3 on its own.


def _encoded_pat(layers: int) -> str:
    wrap = "%" + "25" * (layers - 1)
    body = GITHUB_PAT_BODY
    return f"ghp{wrap}5F{body[:17]}{wrap}{ord(body[17]):X}{body[18:]}"


DOUBLE_ENCODED_PAT = _encoded_pat(2)


@pytest.mark.parametrize(
    "target",
    [
        pytest.param(f"HTTPS://example.com/?t=ghp%5F{GITHUB_PAT_BODY}", id="upper-scheme"),
        pytest.param(f"https://example.com#ghp%5F{GITHUB_PAT_BODY}", id="fragment-only"),
        pytest.param(f"HTTPS://example.com/?t={DOUBLE_ENCODED_PAT}", id="upper-scheme-%25"),
        pytest.param(f"https://example.com#{DOUBLE_ENCODED_PAT}", id="fragment-only-%25"),
        # Still encoded after the bounded decode rounds: fail closed.
        pytest.param(f"https://example.com#{_encoded_pat(4)}", id="beyond-the-decode-cap"),
    ],
)
@pytest.mark.parametrize("width", [7, 4_096])
def test_percent_encoded_credential_in_an_upper_scheme_or_fragment_url_voids_the_link(
    target: str, width: int
) -> None:
    url = _key_shaped_url(f"flowchart TD\n  A[{target}]")
    text = f"See {url} done"
    expected = redact(text)

    assert _redaction._document_link_spans(url) == []
    assert url not in expected
    assert _stream_chunks(_chunks(text, width)) == expected


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("https://example.com/docs/getting-started", id="plain"),
        pytest.param("https://example.com/docs/My%20Diagram.md", id="%20"),
        pytest.param("HTTPS://example.com/docs/getting-started", id="upper-scheme"),
        pytest.param("https://example.com#getting-started", id="fragment-only"),
    ],
)
@pytest.mark.parametrize("width", [7, 4_096])
def test_credential_free_decoded_url_keeps_the_link(target: str, width: int) -> None:
    url = _key_shaped_url(f"flowchart TD\n  A[{target}]")
    text = f"See {url} done"

    assert _redaction._document_link_spans(url) == [(0, len(url))]
    assert redact(text) == text
    assert _stream_chunks(_chunks(text, width)) == text


def test_a_pako_link_inside_a_decoded_view_is_ordinary_text() -> None:
    inner = _key_shaped_url("flowchart TD\n  A --> B")
    assert _redaction._document_link_spans(inner) == [(0, len(inner))]

    assert _redaction._document_link_spans(_url(f"flowchart TD\n  A[{inner}]")) == []


def test_decode_failure_does_not_change_fixed_or_higher_confidence_passes() -> None:
    text = f"AKIAIOSFODNN7EXAMPLE {PREFIX}{'A' * 80} ?token=opaque-value"
    redacted, warnings = redact_credentials(text)

    assert redacted.startswith(f"{TAG} {PREFIX}")
    assert redacted.endswith(f"?token={TAG}")
    assert len(warnings) >= 2


def test_multiple_links_report_exact_input_spans() -> None:
    first = _url("flowchart TD\n  A --> B")
    second = _url("sequenceDiagram\n  Alice->>Bob: hello")
    text = f"before {first} middle {second} after"

    assert _redaction._document_link_spans(text) == [
        (text.index(first), text.index(first) + len(first)),
        (text.index(second), text.index(second) + len(second)),
    ]


def test_top_level_pako_links_share_one_decoded_byte_budget() -> None:
    first = _url("flowchart TD\n  A[" + "x" * 33_000 + "]")
    second = _url("flowchart TD\n  B[" + "y" * 33_000 + "]")
    text = first + " " + second

    assert _redaction._document_link_spans(first) == [(0, len(first))]
    assert _redaction._document_link_spans(second) == [(0, len(second))]
    assert _redaction._document_link_spans(text) == [(0, len(first))]


def _counted_composes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every text the composed pass is asked to judge."""
    from kiro_crew.security import pass_compose

    seen: list[str] = []
    real = pass_compose.compose

    def counting(text: str):
        seen.append(text)
        return real(text)

    monkeypatch.setattr(pass_compose, "compose", counting)
    return seen


def _distinct_views_url(views: int, tag: str) -> str:
    """A credential-free state yielding *views* distinct views (its JSON, the keys
    `code` and `items`, the `code` string alone and beside its key, and one item
    each)."""
    items = [f"{tag}{i}" for i in range(views - 5)]
    state = {"code": f"flowchart TD\n  {tag} --> B", "items": items}
    return _raw_state_url(json.dumps(state, separators=(",", ":")))


def test_top_level_pako_links_share_one_view_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each link alone is under the per-state view cap, but the links of one call
    # share one budget of that many views, so many links cannot multiply the cost
    # of the composed passes: the link that overruns it fails closed.
    first = _distinct_views_url(600, "p")
    second = _distinct_views_url(600, "q")
    text = first + " " + second

    assert _redaction._document_link_spans(first) == [(0, len(first))]
    assert _redaction._document_link_spans(second) == [(0, len(second))]
    composed = _counted_composes(monkeypatch)
    assert _redaction._document_link_spans(text) == [(0, len(first))]
    assert len(composed) <= 2 * _redaction._MERMAID_PAKO_VIEW_CAP


def test_identical_views_are_composed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # A state of 1,000 empty strings is 1,004 views but only four distinct ones;
    # judging each distinct view once keeps one link to a handful of passes.
    url = _raw_state_url(json.dumps({"code": "", "a": [""] * 1_000}, separators=(",", ":")))
    composed = _counted_composes(monkeypatch)

    assert _redaction._document_link_spans(url) == [(0, len(url))]
    assert len(composed) <= 8


def test_records_stay_explainable_for_a_rejected_decoded_secret() -> None:
    unsafe = _key_shaped_url(f"flowchart TD\n  A[{SEGMENT_KEY}]")
    redacted, warnings, records = _redaction.redact_credentials_with_records(
        f"before {unsafe} after"
    )

    assert warnings
    assert unsafe not in redacted
    assert records
    assert all(record.rule == "bare_aws_secret" for record in records)
    assert [record.ordinal for record in records] == list(range(len(records)))


def test_valid_link_creates_no_redaction_record() -> None:
    url = _url("flowchart TD\n  A --> B")

    assert _redaction.redact_credentials_with_records(url) == (url, [], [])
