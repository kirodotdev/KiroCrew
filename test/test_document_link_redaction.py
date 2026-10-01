"""Shared document links survive the bare-secret pass; keys in or near them do not."""

from __future__ import annotations

import pytest

from kiro_crew.security import redact, redact_credentials
from kiro_crew.security import redaction as _redaction

# The canonical AWS documentation example secret key, never a live credential.
KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
# The same example without its `/`, so it fits an id or a title segment whole.
SEGMENT_KEY = "wJalrXUtnFEMIzK7MDENGqbPxRfiCYEXAMPLEKEY"
TAG = "[REDACTED: credential]"

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
]

OTHER_LINKS = [
    "https://docs.google.com/document/d/ABC123/edit",
    "https://docs.google.com/document/d/1IMcAxYmfsB4HbQLXjjlAFbVV6q9rXxtNDFyuzX9k1gn/edit/",
    "https://drive.google.com/drive/folders/1aB2cD3eF4gH5iJ6kL7mN8oP9qR0sT1uV/",
    "https://drive.google.com/drive/folders/1aB2cD3eF4gH5iJ6kL7mN8oP9qR0sT1uV",
    "https://acme-corp.atlassian.net/wiki/spaces/ENG/pages/1234567890/Draft+Weekly+Crew",
    "https://acme-corp.atlassian.net/wiki/spaces/ENG/overview",
    "https://acme-corp.atlassian.net/browse/ENG-4821",
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
    ],
)
def test_key_in_or_near_a_link_is_still_redacted(text: str) -> None:
    out, warnings = redact_credentials(text)
    assert KEY not in out
    assert SEGMENT_KEY not in out
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
