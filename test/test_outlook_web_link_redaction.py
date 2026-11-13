"""Outlook's own `webLink` survives both link gates; keys in or near it do not.

Microsoft Graph returns a message's or event's Outlook on the web address as
`https://outlook.office365.com/owa/?ItemID=<id>&exvsurl=1&viewmodel=ReadMessageItem`
(an event: `?itemid=<id>&exvsurl=1&path=/calendar/item`). The id is the base64 of
an Exchange id, so the bare-secret pass masked it and the exfil gate blocked the
link for its 200+ char query and its base64 run (Slack report: OWA draft links).
"""

from __future__ import annotations

import pytest

from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.security import redaction as _redaction
from kiro_crew.security import scan_exfiltration_urls

# The canonical AWS documentation example secret key, never a live credential,
# and the same key without its `/`, so it fits an id or a field value whole.
KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
SEGMENT_KEY = "wJalrXUtnFEMIzK7MDENGqbPxRfiCYEXAMPLEKEY"
TAG = "[REDACTED: credential]"
# A real-shaped Outlook item id (see test_document_link_redaction.OUTLOOK_ID).
OUTLOOK_ID = (
    "AAMkAGVmMDEzMTM4LTZmYWUtNDdkNC1hMDZiLTU1OGY5OTZhYmY4OABGAAAAAAAiQ8W967B7TKBjgx9rVEURBwAi"
    "IsqMbYjsT5e%2BT7KzowPTAAAAAAEMAAAiIsqMbYjsT5e%2FT7KzowPTAAAYbvZyAAA%3D"
)
MESSAGE_QUERY = f"ItemID={OUTLOOK_ID}&exvsurl=1&viewmodel=ReadMessageItem"
EVENT_QUERY = f"itemid={OUTLOOK_ID}&exvsurl=1&path=/calendar/item"

WEB_LINKS = [
    f"https://outlook.office365.com/owa/?{MESSAGE_QUERY}",
    f"https://outlook.office365.com/owa/?{EVENT_QUERY}",
    f"https://outlook.office.com/owa/?{MESSAGE_QUERY}",
]
TEMPLATES = ["{}", "Draft: {}.", "[the draft]({})", "<{}>", "`{}`", '{{"webLink":"{}"}}']


def test_the_reported_link_trips_both_gates_on_its_own_merits() -> None:
    """Without the route, the id looks like a key and the query is over the cap."""
    assert len(MESSAGE_QUERY) == 201
    assert _redaction._text_contains_bare_secret(WEB_LINKS[0])
    assert scan_exfiltration_urls("https://collector.example/owa/?" + MESSAGE_QUERY)


@pytest.mark.parametrize("url", WEB_LINKS, ids=["message", "event", "office-host"])
@pytest.mark.parametrize(
    "template", TEMPLATES, ids=["bare", "sentence", "markdown", "angle", "code", "json"]
)
def test_the_web_link_survives_both_gates(url: str, template: str) -> None:
    text = template.format(url)
    assert redact_credentials(text) == (text, [])
    assert scan_exfiltration_urls(text) == []
    assert redact_exfiltration_urls(text) == (text, [])


@pytest.mark.parametrize(
    "url",
    [
        # A key as the id, raw or as the event's id.
        f"https://outlook.office365.com/owa/?ItemID={SEGMENT_KEY}&exvsurl=1&viewmodel=ReadMessageItem",
        f"https://outlook.office365.com/owa/?itemid={SEGMENT_KEY}%3D&exvsurl=1&path=/calendar/item",
        # A key glued after a real id, or after the last fixed field.
        f"https://outlook.office365.com/owa/?ItemID={OUTLOOK_ID}{SEGMENT_KEY}&exvsurl=1&viewmodel=ReadMessageItem",
        f"https://outlook.office365.com/owa/?{MESSAGE_QUERY}{SEGMENT_KEY}",
        # A key in an extra field, before or after the fixed ones.
        f"https://outlook.office365.com/owa/?{MESSAGE_QUERY}&k={SEGMENT_KEY}",
        f"https://outlook.office365.com/owa/?k={SEGMENT_KEY}&{MESSAGE_QUERY}",
        # A key in a fixed field's value, or in the path.
        f"https://outlook.office365.com/owa/?ItemID={OUTLOOK_ID}&exvsurl={SEGMENT_KEY}&viewmodel=ReadMessageItem",
        f"https://outlook.office365.com/owa/{SEGMENT_KEY}?{MESSAGE_QUERY}",
        # A key in the fragment.
        f"https://outlook.office365.com/owa/?{MESSAGE_QUERY}#{KEY}",
    ],
)
def test_a_key_in_or_near_the_web_link_is_still_blocked(url: str) -> None:
    text = f"Draft: {url}"
    out, warnings = redact_credentials(text)
    assert SEGMENT_KEY not in out and KEY not in out
    assert TAG in out and warnings
    assert scan_exfiltration_urls(text)


@pytest.mark.parametrize(
    "url",
    [
        # The same query on another host, a look-alike host, http, a port, or
        # another path is judged as before: over the cap and base64-shaped.
        f"https://collector.example/owa/?{MESSAGE_QUERY}",
        f"https://outlook.office365.com.evil.example/owa/?{MESSAGE_QUERY}",
        f"http://outlook.office365.com/owa/?{MESSAGE_QUERY}",
        f"https://outlook.office365.com:8443/owa/?{MESSAGE_QUERY}",
        f"https://outlook.office365.com/mail/?{MESSAGE_QUERY}",
        # The fixed fields out of order, renamed, or with another value.
        f"https://outlook.office365.com/owa/?exvsurl=1&{MESSAGE_QUERY}",
        f"https://outlook.office365.com/owa/?ItemId={OUTLOOK_ID}&exvsurl=1&viewmodel=ReadMessageItem",
        f"https://outlook.office365.com/owa/?ItemID={OUTLOOK_ID}&exvsurl=2&viewmodel=ReadMessageItem",
        # A long payload smuggled in an extra field with no key shape at all.
        f"https://outlook.office365.com/owa/?{MESSAGE_QUERY}&d=" + "a-b" * 80,
    ],
)
def test_anything_but_the_exact_web_link_is_still_blocked(url: str) -> None:
    assert scan_exfiltration_urls(f"see {url} now")


def test_the_span_covers_the_query_and_stops_before_a_sentence_end() -> None:
    url = WEB_LINKS[0]
    text = f"Draft: {url}. Next"
    start = text.index(url)
    assert _redaction._chat_mail_link_spans(text) == [(start, start + len(url))]


def test_another_routes_query_stays_outside_its_span() -> None:
    """Only `/owa/` reads on into its query; a mail route's query is judged as before."""
    route = f"https://outlook.office.com/mail/drafts/id/{OUTLOOK_ID}"
    text = f"{route}?{MESSAGE_QUERY}"
    assert _redaction._chat_mail_link_spans(text) == [(0, len(route))]
