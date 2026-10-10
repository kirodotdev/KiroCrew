"""PlantUML diagram links survive the bare-secret pass; keys in or near them do not."""

from __future__ import annotations

import base64
import random
import zlib

import pytest

from kiro_crew.security import StreamRedactor, redact, redact_credentials
from kiro_crew.security import redaction as _redaction

# The canonical AWS documentation example secret key, never a live credential.
KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
# The same example without its `/`, so it fits a diagram segment whole.
SEGMENT_KEY = "wJalrXUtnFEMIzK7MDENGqbPxRfiCYEXAMPLEKEY"
TAG = "[REDACTED: credential]"

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"


def _encode(source: str) -> str:
    """Encode *source* the way PlantUML servers expect: raw deflate, own base64."""
    deflater = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    data = deflater.compress(source.encode()) + deflater.flush()
    data += b"\0" * (-len(data) % 3)
    out = []
    for i in range(0, len(data), 3):
        word = int.from_bytes(data[i : i + 3], "big")
        out.extend(_ALPHABET[word >> shift & 0x3F] for shift in (18, 12, 6, 0))
    return "".join(out)


def _sources() -> list[str]:
    rng = random.Random(20841)
    fixed = [
        "@startuml\nAlice -> Bob: hi\n@enduml",
        "@startuml\nactor User\nparticipant Gateway\nUser -> Gateway: POST /api/chat\n"
        "Gateway --> User: stream\n@enduml",
        "Bob -> Alice : hello",
    ]
    generated = [
        "\n".join(
            ["@startuml"]
            + [
                f"C{rng.randint(0, 99)} -> C{rng.randint(0, 99)}: step {rng.randint(0, 999)}"
                for _ in range(rng.randint(2, 30))
            ]
            + ["@enduml"]
        )
        for _ in range(200)
    ]
    return fixed + generated


def _kept(encoded: str) -> bool:
    """Whether one diagram on its own would be kept whole."""
    return _redaction._plantuml_verdict(encoded, _redaction._PLANTUML_SOURCE_CAP)[0]


DIAGRAMS = [_encode(source) for source in _sources()]
SAMPLE = DIAGRAMS[1]

ROUTES = [
    "https://www.plantuml.com/plantuml/svg/",
    "https://www.plantuml.com/plantuml/png/",
    "https://plantuml.com/plantuml/txt/",
    "https://plantuml.example.com/plantuml/uml/",
    "https://plantuml.example.com/svg/",
]


def test_the_fixture_key_is_key_shaped() -> None:
    assert _redaction._looks_like_secret_key(SEGMENT_KEY)


def test_most_fixture_diagrams_trip_the_heuristic() -> None:
    # Without the link rule these are masked: the encoded text is random base64.
    fired = sum(_redaction._text_contains_bare_secret(f"{ROUTES[0]}{d}") for d in DIAGRAMS)
    assert fired > len(DIAGRAMS) * 0.9


@pytest.mark.parametrize("route", ROUTES)
def test_every_diagram_link_survives(route: str) -> None:
    for diagram in DIAGRAMS:
        url = route + diagram
        assert redact_credentials(url) == (url, []), url


def test_a_published_example_survives() -> None:
    url = "https://www.plantuml.com/plantuml/png/SyfFKj2rKt3CoKnELR1Io4ZDoSa70000"
    assert redact_credentials(url) == (url, [])


@pytest.mark.parametrize(
    "template",
    ["{}", "see {} for the flow.", "![flow]({})", "<{}>", "`{}`", '{{"url":"{}"}}', "{}/"],
)
def test_diagram_link_survives_in_context(template: str) -> None:
    text = template.format(ROUTES[0] + SAMPLE)
    assert redact_credentials(text) == (text, [])
    assert redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        # Credentials in the userinfo, the query or the fragment.
        f"https://user:{KEY}@www.plantuml.com/plantuml/svg/{SAMPLE}",
        f"https://www.plantuml.com/plantuml/svg/{SAMPLE}?k={KEY}",
        f"https://www.plantuml.com/plantuml/svg/{SAMPLE}?token={SEGMENT_KEY}",
        f"https://www.plantuml.com/plantuml/svg/{SAMPLE}#{KEY}",
        f"https://www.plantuml.com/plantuml/svg/{SAMPLE}#{SEGMENT_KEY}",
        # A key pasted in as the diagram, or glued to a real one.
        f"https://www.plantuml.com/plantuml/svg/{SEGMENT_KEY}",
        f"https://www.plantuml.com/plantuml/svg/{SEGMENT_KEY}{SAMPLE}",
        f"https://www.plantuml.com/plantuml/svg/{SAMPLE}{SEGMENT_KEY}",
        f"https://www.plantuml.com/plantuml/svg/{SAMPLE}-{SEGMENT_KEY}",
        f"https://www.plantuml.com/plantuml/svg/{SAMPLE}/{KEY}",
        # Not a PlantUML route: other hosts, look-alike hosts, plain HTTP.
        f"https://collector.example/plantuml/svg/{SEGMENT_KEY}",
        f"https://evilplantuml.example/plantuml/svg/{SEGMENT_KEY}",
        f"http://www.plantuml.com/plantuml/svg/{SEGMENT_KEY}",
        f"https://www.plantuml.com/plantuml/form/{SEGMENT_KEY}",
    ],
)
def test_key_in_or_near_a_diagram_link_is_still_redacted(text: str) -> None:
    redacted, _ = redact_credentials(text)
    assert TAG in redacted
    assert SEGMENT_KEY not in redacted
    assert KEY not in redacted


def test_key_beside_a_diagram_link_is_redacted_and_the_link_kept() -> None:
    url = ROUTES[0] + SAMPLE
    redacted, _ = redact_credentials(f"{url} {KEY}")
    assert redacted == f"{url} {TAG}"


@pytest.mark.parametrize(
    "source",
    [
        f"@startuml\nAlice -> Bob: {KEY}\n@enduml",
        f"@startuml\nnote over Bob: aws_secret_access_key={SEGMENT_KEY}\n@enduml",
        "@startuml\nAlice -> Bob: AKIA" + "IOSFODNN7EXAMPLE" + "\n@enduml",
        # Shapes only the decode-and-scan and the URL-parameter passes see.
        "@startuml\nnote: "
        + base64.b64encode(f"aws_secret_access_key={SEGMENT_KEY}".encode()).decode()
        + "\n@enduml",
        "@startuml\nAlice -> Bob: https://example.com/hook?token=Zq81xLmP0vR7tYc2\n@enduml",
    ],
)
def test_a_diagram_whose_source_holds_a_key_is_not_kept(source: str) -> None:
    encoded = _encode(source)
    assert not _kept(encoded)
    # No exempt span: the link is judged exactly as on any other host.
    assert _redaction._document_link_spans(ROUTES[0] + encoded) == []


@pytest.mark.parametrize(
    "encoded",
    ["", "SyfF", SEGMENT_KEY, SEGMENT_KEY + "AA", SAMPLE[:-4], SAMPLE + "AAAA", _encode("a\x00b")],
)
def test_text_that_is_not_a_whole_diagram_is_not_one(encoded: str) -> None:
    assert not _kept(encoded)


def test_a_diagram_that_inflates_far_past_its_length_is_not_kept() -> None:
    # Highly repetitive text inflates to many times its encoded length; past the
    # per-character cap the link is judged as on any other host.
    encoded = _encode("@startuml\nnote: " + "7" * 200_000 + "\n@enduml")
    assert len(encoded) * _redaction._PLANTUML_RATIO_CAP < 200_000
    assert not _kept(encoded)


def test_inflated_text_per_call_is_capped() -> None:
    # Each diagram fits on its own; together they pass the per-call cap, so only
    # the first few are exempt.
    source = "@startuml\n" + "\n".join(f"A{i} -> B{i}: step {i}" for i in range(220)) + "\n@enduml"
    encoded = _encode(source)
    assert len(source) < _redaction._PLANTUML_SOURCE_CAP < 4 * len(source)
    assert _kept(encoded)
    text = " ".join(ROUTES[0] + encoded for _ in range(6))
    assert len(_redaction._document_link_spans(text)) == _redaction._PLANTUML_SOURCE_CAP // len(
        source
    )


def test_a_diagram_link_inside_a_diagram_is_not_decoded_again() -> None:
    inner = ROUTES[0] + SAMPLE
    outer = _encode(f"@startuml\nnote: {inner}\n@enduml")
    assert _kept(SAMPLE)
    previous = _redaction._IN_DIAGRAM_SCAN.get()
    _redaction._IN_DIAGRAM_SCAN.set(True)
    try:
        assert _redaction._document_link_spans(inner) == []
        inner_unchanged = redact_credentials(inner)[0] == inner
    finally:
        _redaction._IN_DIAGRAM_SCAN.set(previous)
    # Inside a diagram the inner link is judged by the other passes only.
    assert _kept(outer) is inner_unchanged


@pytest.mark.parametrize(
    "encoded",
    [
        SAMPLE[:-1],
        SAMPLE[:-2],
        SAMPLE[:-3],
        SAMPLE + "A",
        SAMPLE + "AAAA",
        # `0` is PlantUML's zero digit: a further group of padding is still a group.
        SAMPLE + "0",
        SAMPLE + "0000",
        SAMPLE + SEGMENT_KEY[:4],
    ],
)
def test_a_diagram_cut_mid_group_or_with_a_further_group_is_not_kept(encoded: str) -> None:
    assert not _kept(encoded)


def test_no_cut_of_a_key_glued_to_a_diagram_is_kept() -> None:
    # A stream may cut the text anywhere and judge the prefix alone. No prefix
    # that runs past the diagram into the glued key gets an exempt span, so each
    # is judged exactly as on any other host.
    link = ROUTES[0] + SAMPLE
    text = link + SEGMENT_KEY + "-" + "0" * 474
    for end in range(len(link) + 1, len(link) + len(SEGMENT_KEY) + 2):
        assert _redaction._document_link_spans(text[:end]) == [], end


def test_a_key_beside_a_diagram_streams_out_masked() -> None:
    text = f"see {ROUTES[0]}{SAMPLE} then {KEY} done"
    stream = StreamRedactor()
    out = "".join(stream.feed(text[i : i + 7]) for i in range(0, len(text), 7)) + stream.flush()
    assert out == f"see {ROUTES[0]}{SAMPLE} then {TAG} done"
