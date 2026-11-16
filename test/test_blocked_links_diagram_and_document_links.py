"""Diagram and shared-document links render live; the decision is per URL, not per host.

A PlantUML diagram link carries its deflated source in the path and a shared
document, mail or chat link carries its id there, with at most a short query. The
blocked-link decision (``security.blocked_links``) judges a URL by its payload, so
these links render live and their text is never changed, while the same link with a
long or encoded query, or a credential in it, is blocked like any other.
"""

from __future__ import annotations

import base64
import random
import zlib

import pytest

from kiro_crew.security import redact_credentials
from kiro_crew.security.blocked_links import blocked_links

_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
_LONG = "a1b2c3" * 50
_B64 = base64.b64encode(b"conversation text sent out as data, once more").decode()

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


DIAGRAMS = [_encode(source) for source in _sources()]
SAMPLE = DIAGRAMS[1]

ROUTES = [
    "https://www.plantuml.com/plantuml/svg/",
    "https://www.plantuml.com/plantuml/png/",
    "https://plantuml.com/plantuml/txt/",
    "https://plantuml.example.com/plantuml/uml/",
    "https://plantuml.example.com/svg/",
]


# Slack message stamps (`p` + 16 digits), assembled so no literal names one.
SLACK_MSG = "p" + "1721434710877893"
SLACK_MSG_2 = "p" + "1730160000123456"
# The shape of an Outlook on the web item id: base64 of an EWS id, its `+`, `/`
# and `=` percent-escaped.
OUTLOOK_ID = (
    "AAMkAGVmMDEzMTM4LTZmYWUtNDdkNC1hMDZiLTU1OGY5OTZhYmY4OABGAAAAAAAiQ8W967B7TKBjgx9rVEURBwAi"
    "IsqMbYjsT5e%2BT7KzowPTAAAAAAEMAAAiIsqMbYjsT5e%2FT7KzowPTAAAYbvZyAAA%3D"
)


# Shared-document, mail and chat links whose id or path holds a long run of
# id characters, beside shorter ones.
FIRING_LINKS = [
    "https://docs.google.com/document/d/1IMcAxYmfsB4HbQLXjjlAFbVV6q9rXxtNDFyuzX9k1gn/edit?usp=sharing",
    "https://docs.google.com/spreadsheets/d/1PdEMw-yUigxOZCMgGzk2WbIr45dbCG9YgsbGhy1LVQO/edit#gid=0",
    "https://docs.google.com/presentation/d/17E0QjfAipE7WJEZLGAROtZ8jhtPK8XYgrraqV56d-dV/edit",
    "https://docs.google.com/forms/d/e/1FAIpQLPhmpFFPmCFHOk2yTBIisxhOaFgAFdRgYGaLYD7HrA3gyrNOQb/viewform",
    "https://docs.google.com/document/u/1/d/1IMcAxYmfsB4HbQLXjjlAFbVV6q9rXxtNDFyuzX9k1gn/edit",
    "https://drive.google.com/file/d/15HFBc3YoxpsZXggLrQDtecyZaf6meJAV/view?usp=drive_link",
    "https://acme-corp.atlassian.net/wiki/spaces/MFFCTQ/pages/1812978809/Q3+KMS+RFC+KMS+Design",
    f"https://outlook.office.com/mail/drafts/id/{OUTLOOK_ID}",
    f"https://outlook.office.com/mail/inbox/id/{OUTLOOK_ID}?nativeVersion=1",
    f"https://outlook.office.com/mail/id/{OUTLOOK_ID}",
    f"https://outlook.office.com/mail/0/inbox/id/{OUTLOOK_ID}",
    f"https://outlook.office365.com/mail/deeplink/compose/{OUTLOOK_ID}",
    f"https://outlook.live.com/mail/0/inbox/id/{OUTLOOK_ID}",
    f"https://outlook.cloud.microsoft/mail/deeplink/read/{OUTLOOK_ID}",
    f"https://outlook.office.com/calendar/item/{OUTLOOK_ID}",
    f"https://outlook.office.com/calendar/deeplink/read/{OUTLOOK_ID}",
    # A Slack link whose ids and message timestamp join into one 40+ char run,
    # on each host shape, with and without its one optional trailing `/`.
    f"https://app.slack.com/client/E6MI0YHZ0NA/CRTBNLZGCJN/{SLACK_MSG}",
    f"https://app.slack.com/client/E6MI0YHZ0NA/CRTBNLZGCJN/{SLACK_MSG}/",
    f"https://acme.slack.com/client/E6MI0YHZ0NA/CRTBNLZGCJN/{SLACK_MSG}",
    f"https://acme.enterprise.slack.com/client/E6MI0YHZ0NA/CRTBNLZGCJN/{SLACK_MSG}",
]

OTHER_LINKS = [
    "https://docs.google.com/document/d/ABC123/edit",
    "https://docs.google.com/document/d/1IMcAxYmfsB4HbQLXjjlAFbVV6q9rXxtNDFyuzX9k1gn/edit/",
    "https://drive.google.com/drive/folders/1aB2cD3eF4gH5iJ6kL7mN8oP9qR0sT1uV/",
    "https://drive.google.com/drive/folders/1aB2cD3eF4gH5iJ6kL7mN8oP9qR0sT1uV",
    "https://acme-corp.atlassian.net/wiki/spaces/ENG/pages/1234567890/Draft+Weekly+Crew",
    "https://acme-corp.atlassian.net/wiki/spaces/ENG/overview",
    "https://acme-corp.atlassian.net/browse/ENG-4821",
    # Slack permalinks: their ids are short, so no window looks like a key.
    f"https://acme.slack.com/archives/C07ABCD1234/{SLACK_MSG_2}",
    f"https://acme.enterprise.slack.com/archives/C07ABCD1234/{SLACK_MSG_2}"
    "?thread_ts=1730160000.123456&cid=C07ABCD1234",
    "https://app.slack.com/client/E01ABCDEF12/C07ABCD1234/thread/C07ABCD1234-1730160000.123456",
]


TEMPLATES = [
    "{}",
    "see {} for the flow.",
    "[the doc]({})",
    "![flow]({})",
    "<{}>",
    "`{}`",
    '{{"url":"{}"}}',
]


@pytest.mark.parametrize("route", ROUTES)
def test_every_diagram_link_renders_live(route: str) -> None:
    for diagram in DIAGRAMS:
        url = route + diagram
        assert blocked_links(url) == [], url


def test_a_published_example_renders_live_and_is_not_masked() -> None:
    url = "https://www.plantuml.com/plantuml/png/SyfFKj2rKt3CoKnELR1Io4ZDoSa70000"
    assert blocked_links(url) == []
    assert redact_credentials(url) == (url, [])


@pytest.mark.parametrize("template", TEMPLATES)
def test_a_diagram_link_renders_live_in_context(template: str) -> None:
    text = template.format(ROUTES[0] + SAMPLE)
    assert blocked_links(text) == []
    assert redact_credentials(text) == (text, [])


@pytest.mark.parametrize("url", FIRING_LINKS + OTHER_LINKS)
@pytest.mark.parametrize("template", TEMPLATES)
def test_a_document_link_renders_live(url: str, template: str) -> None:
    text = template.format(url)
    assert blocked_links(text) == []
    assert redact_credentials(text) == (text, [])


@pytest.mark.parametrize(
    "url",
    [
        ROUTES[0] + SAMPLE + "?d=" + _LONG,
        ROUTES[0] + SAMPLE + "?d=" + _B64 * 2,
        FIRING_LINKS[0] + "&d=" + _LONG,
        OTHER_LINKS[0] + "?d=" + _B64 * 2,
        f"https://acme-corp.atlassian.net/wiki/spaces/ENG/pages/1/T?k={_KEY_ID}",
        f"https://docs.google.com/document/d/ABC123/edit?k={_KEY_ID}",
        ROUTES[0] + SAMPLE + f"?k={_KEY_ID}",
    ],
)
def test_the_same_link_with_a_long_or_encoded_query_or_a_credential_is_blocked(url: str) -> None:
    [record] = blocked_links(f"see {url} now")
    assert record["url"] == url
    if _KEY_ID in url:
        assert record["withheld"] == "credential"


def test_a_look_alike_host_is_judged_by_the_same_payload_rule() -> None:
    """No host is trusted for being a document host: a look-alike with the same
    short path renders live, and with a long query is blocked, exactly as the real
    host is."""
    for host in ("docs.google.com", "docs.google.com.evil.example", "evil-docs.google.com"):
        assert blocked_links(f"https://{host}/document/d/ABC123/edit") == []
        assert blocked_links(f"https://{host}/document/d/ABC123/edit?d={_LONG}") != []


def test_a_token_parameter_on_a_document_link_is_masked_and_the_link_kept() -> None:
    url = "https://docs.google.com/document/d/ABC123/edit?token=opaque-session-value"
    out, _ = redact_credentials(url)
    assert out == "https://docs.google.com/document/d/ABC123/edit?token=[REDACTED: credential]"
    assert blocked_links(out) == []
