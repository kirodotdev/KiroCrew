"""Shared document links survive the bare-secret pass; keys in or near them do not."""

from __future__ import annotations

import base64

import pytest

from kiro_crew.security import redact, redact_credentials
from kiro_crew.security import redaction as _redaction

# The canonical AWS documentation example secret key, never a live credential.
KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
# The same example without its `/`, so it fits an id or a title segment whole.
SEGMENT_KEY = "wJalrXUtnFEMIzK7MDENGqbPxRfiCYEXAMPLEKEY"
TAG = "[REDACTED: credential]"
# A key-shaped value with its `/` written `%2F`, the way a URL carries it.
# Slack message stamps (`p` + 16 digits), assembled so no literal names one.
SLACK_MSG = "p" + "1721434710877893"
SLACK_MSG_2 = "p" + "1730160000123456"
ESCAPED_KEY = "wJalrXUtnFEMIzK7MDENGqbPxRfiCYEXAMPL%2FKEY"
# The example key turned so its last character is `/`: without its slash it is
# 39 id characters plus the trailing `/` a route may end with.
SLASH_END_KEY = "K7MDENG/bPxRfiCYEXAMPLEKEYwJalrXUtnFEMI/"
# The same key turned so its FIRST character is `/`.
SLASH_START_KEY = "/K7MDENGzbPxRfiCYEXAMPLEKEYwJalrXUtnFEMI"
# The shape of an Outlook on the web item id: base64 of an EWS id, its `+`, `/`
# and `=` percent-escaped.
OUTLOOK_ID = (
    "AAMkAGVmMDEzMTM4LTZmYWUtNDdkNC1hMDZiLTU1OGY5OTZhYmY4OABGAAAAAAAiQ8W967B7TKBjgx9rVEURBwAi"
    "IsqMbYjsT5e%2BT7KzowPTAAAAAAEMAAAiIsqMbYjsT5e%2FT7KzowPTAAAYbvZyAAA%3D"
)


def _crafted_outlook_id(payload: bytes) -> str:
    """A base64 Exchange-id header (valid GUID, consistent store length) wrapping
    ``payload`` as the store id, so the whole run holds no 40-char piece and the
    length fields are self-consistent -- the shape a crafter builds around a key."""
    guid = b"ef013138-6fae-47d4-a06b-558f996abf88"
    body = b"\x00\x03\x24\x00" + guid + b"\x00"
    data = body + bytes([len(payload) & 0xFF, len(payload) >> 8]) + payload
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


# A key wrapped in a crafted but length-consistent Exchange header: the store id
# is NOT a folder or message entry id, so the whole-id decode voids the exemption.
CRAFTED_KEY_ID = _crafted_outlook_id(SEGMENT_KEY.encode())

# Each of these fires the bare-secret heuristic when its link exemption is
# absent: a 40-char window inside the id or across the path looks like a key.
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
    # A Slack link whose ids and message timestamp join into one 40+ char run.
    f"https://app.slack.com/client/E6MI0YHZ0NA/CRTBNLZGCJN/{SLACK_MSG}",
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


def test_the_fixture_keys_are_key_shaped() -> None:
    assert len(KEY) == len(SEGMENT_KEY) == 40
    assert _redaction._looks_like_secret_key(KEY)
    assert _redaction._looks_like_secret_key(SEGMENT_KEY)


@pytest.mark.parametrize("url", FIRING_LINKS)
def test_the_fixture_link_trips_the_heuristic(url: str) -> None:
    assert _redaction._text_contains_bare_secret(url)


@pytest.mark.parametrize("url", FIRING_LINKS + OTHER_LINKS)
@pytest.mark.parametrize(
    "template",
    ["{}", "see {} for details.", "[the doc]({})", "<{}>", "`{}`", '{{"url":"{}"}}'],
)
def test_document_link_survives(url: str, template: str) -> None:
    text = template.format(url)
    assert redact_credentials(text) == (text, [])
    assert redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        # Not an allowed host: a key as one whole segment, or glued to more.
        f"https://collector.example/collect/{KEY}",
        f"https://collector.example/collect/{KEY}A",
        # Look-alike hosts and a non-HTTPS scheme.
        f"https://docs.google.com.evil.example/document/d/{KEY}A1b2/edit",
        f"https://evil-docs.google.com/document/d/{KEY}A1b2/edit",
        f"http://docs.google.com/document/d/{KEY}A1b2/edit",
        # Credentials in the userinfo, the query or the fragment.
        f"https://user:{KEY}@docs.google.com/document/d/ABC123/edit",
        f"https://docs.google.com/document/d/ABC123/edit?k={KEY}",
        f"https://docs.google.com/document/d/ABC123/edit#{KEY}",
        # A key pasted in whole as the id or the page title.
        f"https://docs.google.com/document/d/{SEGMENT_KEY}/edit",
        f"https://drive.google.com/file/d/{SEGMENT_KEY}/view",
        f"https://docs.google.com/document/d/{SEGMENT_KEY}-x9/edit",
        f"https://docs.google.com/document/d/x9_{SEGMENT_KEY}/edit",
        f"https://acme.atlassian.net/wiki/spaces/ENG/pages/123/Notes-{SEGMENT_KEY}.pdf",
        f"https://acme.atlassian.net/wiki/spaces/ENG/pages/123/Rotate+{SEGMENT_KEY}+now",
        f"https://acme.atlassian.net/wiki/spaces/ENG/pages/123/{SEGMENT_KEY}",
        # A key past the end of the route.
        f"https://docs.google.com/document/d/ABC123/edit{KEY}",
        f"https://docs.google.com/document/d/ABC123/edit/{KEY}",
        f"https://docs.google.com/document/d/1IMcAxYmfsB4HbQLXjjlAFbVV6q9rXxtNDFyuzX9k1gn{KEY}/edit",
        # A key in a later field of compact JSON or CSV.
        f'{{"u":"https://docs.google.com/document/d/ABC123/edit","blob":"{KEY}Q"}}',
        f"https://docs.google.com/document/d/ABC123/edit,{KEY}",
        # Outlook: a key as the item id or the folder, raw, escaped, between
        # escapes, or past the end of the id.
        f"https://outlook.office.com/mail/drafts/id/{SEGMENT_KEY}",
        f"https://outlook.office.com/mail/drafts/id/{SEGMENT_KEY}%3D",
        f"https://outlook.office.com/mail/drafts/id/{ESCAPED_KEY}",
        f"https://outlook.office.com/mail/drafts/id/AAQk%2F{SEGMENT_KEY}%2FAAQk",
        f"https://outlook.office.com/mail/drafts/id/AAQk%2B{SEGMENT_KEY}",
        f"https://outlook.office.com/mail/{SEGMENT_KEY}/id/{OUTLOOK_ID}",
        f"https://outlook.office.com/mail/drafts/id/{OUTLOOK_ID}/{KEY}",
        f"https://outlook.office.com/mail/drafts/id/{OUTLOOK_ID}{KEY}",
        f"https://outlook.office.com/mail/drafts/id/{OUTLOOK_ID}?k={KEY}",
        f"https://outlook.office.com/mail/drafts/id/{SLASH_END_KEY}",
        # A key whose leading `/` doubles as the route's last separator, or whose
        # trailing `/` doubles as a folder's.
        f"https://outlook.office.com/mail/deeplink/compose{SLASH_START_KEY}",
        f"https://outlook.office.com/mail/drafts/id{SLASH_START_KEY}",
        f"https://outlook.office.com/calendar/item{SLASH_START_KEY}",
        f"https://outlook.office.com/mail/{SLASH_END_KEY}id/{OUTLOOK_ID}",
        f"https://outlook.office.com/mail/AAMkAGVmMDEz{SLASH_END_KEY}id/{OUTLOOK_ID}",
        # A key with no `/` glued in front of a real id.
        f"https://outlook.office.com/mail/drafts/id/{SEGMENT_KEY}{OUTLOOK_ID}",
        # A key glued after a bare id prefix, or after a compressed one: the
        # decoded bytes are not one whole Exchange id.
        f"https://outlook.office.com/mail/drafts/id/AAMkAGVmMDEz{SEGMENT_KEY}",
        f"https://outlook.office.com/mail/drafts/id/AQMkADAwATM0{SEGMENT_KEY}",
        f"https://outlook.office.com/calendar/item/AAQkAGVmMDEz{SEGMENT_KEY}%3D",
        # A key wrapped in a crafted, length-consistent header whose store id is
        # not an entry id: the whole-Exchange-id decode rejects it.
        f"https://outlook.office.com/mail/drafts/id/{CRAFTED_KEY_ID}",
        # Outlook look-alike hosts and a non-HTTPS scheme.
        f"https://outlook.office.com.evil.example/mail/drafts/id/{SEGMENT_KEY}A1b2",
        f"https://evil-outlook.office.com/mail/drafts/id/{SEGMENT_KEY}A1b2",
        f"http://outlook.office.com/mail/drafts/id/{SEGMENT_KEY}A1b2",
        # Slack: a key as a path segment, glued to an id or the message, after
        # the route, in the query, or on a look-alike host or scheme.
        f"https://app.slack.com/client/E6MI0YHZ0NA/CRTBNLZGCJN/{SEGMENT_KEY}",
        f"https://app.slack.com/client/E6MI0YHZ0NA/CRTBNLZGCJN{SEGMENT_KEY}",
        f"https://app.slack.com/client/E6MI0YHZ0NA/CRTBNLZGCJN/{SLACK_MSG}{SEGMENT_KEY}",
        f"https://app.slack.com/client/E6MI0YHZ0NA/CRTBNLZGCJN/{SLACK_MSG}/{KEY}",
        f"https://app.slack.com/client/E6MI0YHZ0NA/CRTBNLZGCJN/{SLACK_MSG}?k={KEY}",
        f"https://acme.enterprise.slack.com/archives/C9ERNNBER02/{SEGMENT_KEY}",
        f"https://app.slack.com.evil.example/client/E6MI0YHZ0NA/CRTBNLZGCJN/{SEGMENT_KEY}",
        f"https://evil-slack.com/client/E6MI0YHZ0NA/{SEGMENT_KEY}",
        f"http://app.slack.com/client/E6MI0YHZ0NA/{SEGMENT_KEY}",
    ],
)
def test_key_in_or_near_a_link_is_still_redacted(text: str) -> None:
    out, warnings = redact_credentials(text)
    assert KEY not in out
    assert SEGMENT_KEY not in out
    assert ESCAPED_KEY not in out
    assert SLASH_END_KEY not in out
    assert SLASH_START_KEY not in out
    assert TAG in out
    assert warnings


def test_a_key_glued_inside_the_id_is_the_accepted_residual() -> None:
    url = f"https://docs.google.com/document/d/{SEGMENT_KEY}x9/edit"
    assert redact_credentials(url) == (url, [])


def test_a_token_parameter_on_a_document_link_is_still_redacted() -> None:
    url = "https://docs.google.com/document/d/ABC123/edit?token=opaque-session-value"
    out, _ = redact_credentials(url)
    assert out == f"https://docs.google.com/document/d/ABC123/edit?token={TAG}"


def test_the_link_survives_beside_a_redacted_key() -> None:
    link = FIRING_LINKS[0]
    out, _ = redact_credentials(f"{link} {KEY}")
    assert out == f"{link} {TAG}"
