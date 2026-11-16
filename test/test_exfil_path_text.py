"""Encoded words in a URL path are not an exfiltration payload.

A browser percent-encodes a non-ASCII path as UTF-8, three octets per CJK
character, so a news headline in Chinese forms a long run of octets. Display
redaction masks only credential values inside a URL, so such a path renders as
written; the OAuth admission check, a blocking decision, keeps its strict floor.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from kiro_crew.security import oauth_url_contains_credential, redact_exfiltration_urls

_HEADLINE = "港股-恒指低開196點-科技股承壓-聯想集團低開逾6-翰森逆市走高-015534459"


def _redacted(url: str) -> bool:
    result, _warnings = redact_exfiltration_urls(f"see {url}")
    return "[REDACTED" in result


@pytest.mark.parametrize(
    "url",
    [
        "https://hk.finance.yahoo.com/news/" + quote(_HEADLINE) + ".html?guccounter=1",
        "https://ja.wikipedia.org/wiki/" + quote("東京都の歴史と文化"),
        "https://ru.wikipedia.org/wiki/" + quote("Достопримечательности_Москвы"),
        "https://example.com/" + quote("《港股》恒指低開，科技股承壓"),
    ],
)
def test_encoded_words_in_path_are_kept(url: str) -> None:
    assert not _redacted(url)


def test_oauth_admission_keeps_the_strict_floor() -> None:
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth/"
        + quote(_HEADLINE)
        + "?client_id=abc&response_type=code"
    )
    assert oauth_url_contains_credential(url)
