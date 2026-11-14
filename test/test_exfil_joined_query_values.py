"""The exfil URL checks also judge a query's parameter values joined.

A secret cut into several short values (each under the 40-char base64 run, the
whole query under the 200-char length signal) matched nothing while the values
were judged one by one. The base64 join counts only form-decoded pieces of at
least 16 chars, so ordinary ids and search words never join. Both directions are pinned: such a split payload is
flagged, and ordinary links with several short parameters stay clean. The browser
mirror is pinned by ``website/src/test/sanitizeExfiltrationJoinedQueryValues.test.ts``
with the same cases.

Sample secrets are built from parts so no literal credential appears here.
"""

from __future__ import annotations

import pytest

from kiro_crew.security import redact_exfiltration_urls, scan_exfiltration_urls
from kiro_crew.security.exfil import _exfil_url_warning, oauth_url_contains_credential

# AWS's documented example key pair, built from parts.
_KEY_ID = "AKIA" + "IOSFODNN7" + "EXAMPLE"
_SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
_SHA1 = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _chunks(value: str, size: int) -> list[str]:
    return [value[i : i + size] for i in range(0, len(value), size)]


def _split_query(value: str, size: int = 20, filler: str = "") -> str:
    parts = [f"p{i}={chunk}" for i, chunk in enumerate(_chunks(value, size))]
    return ("&" + filler + "&" if filler else "&").join(parts)


def _flagged(url: str) -> bool:
    result, warnings = redact_exfiltration_urls(f"see {url} for details")
    return bool(warnings) and url not in result and bool(scan_exfiltration_urls(url))


SPLIT_PAYLOADS = {
    "key id and secret in short values": "a=AKIA&b=IOSFODNN7&c=wJalrXUtnFEMI&d=K7MDENGbPxRfi&e=CYEXAMPLEKEY",
    "secret cut into 20-char values": _split_query(_SECRET),
    "secret cut into 16-char values": _split_query(_SECRET + "Zq9Xw2Lm", 16),
    "secret with filler param between chunks": _split_query(_SECRET, filler="utm_source=mail_list"),
    "secret with percent-encoded slashes": "a=wJalrXUtnFEMI%2FK7MDE&b=NG%2FbPxRfiCYEXAMPLEKEY",
    "key id cut in two": "x=" + _KEY_ID[:8] + "&y=" + _KEY_ID[8:],
    "secret cut at = inside one value": f"a={_SECRET[:20]}={_SECRET[20:]}",
    "key id cut at = inside one value": f"a={_KEY_ID[:8]}={_KEY_ID[8:]}",
    "slack token cut in three": "a=xo&b=xb-1234&c=5678-abcdEFGH",
}

BENIGN = {
    "github search": "https://github.com/search?q=exfil+redaction&type=code&p=2",
    "paging and sort": "https://shop.example.com/c?page=2&sort=asc&category=electronics",
    "utm tags": (
        "https://news.example.com/a?utm_source=newsletter&utm_medium=email"
        "&utm_campaign=spring_sale_2024&utm_content=hero"
    ),
    "numeric ids": "https://api.example.com/items?id=12345&user=67890&session=abc&lang=en",
    "date range": "https://github.com/o/r/commits?author=alice&since=2024-01-01&until=2024-02-01",
    "commit then more params": f"https://git.example.com/h?commit={_SHA1}&tab=files&view=split",
    "short flags": "https://docs.example.com/x?v=3&lang=en&theme=dark&q=install",
    "youtube playlist": "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf&index=2",
    "google search": "https://www.google.com/search?q=how+to+bake+bread&oq=how+to+bake+bread&sourceid=chrome",
}


@pytest.mark.parametrize("query", SPLIT_PAYLOADS.values(), ids=SPLIT_PAYLOADS.keys())
def test_secret_split_across_short_values_is_flagged(query: str) -> None:
    url = "https://evil.example/p?" + query
    assert len(query) < 200
    assert _flagged(url)


@pytest.mark.parametrize("url", BENIGN.values(), ids=BENIGN.keys())
def test_ordinary_multi_param_link_stays_clean(url: str) -> None:
    assert scan_exfiltration_urls(url) == []
    assert redact_exfiltration_urls(f"see {url} here")[0] == f"see {url} here"


def test_joined_blob_check_is_waived_on_an_exempt_host() -> None:
    """Same tier as the single-value blob check: an exempt host skips it."""
    path = "/p?" + _split_query(_SECRET)
    assert _exfil_url_warning("tenant.example.com", path, frozenset()) is not None
    assert _exfil_url_warning("tenant.example.com", path, frozenset({"tenant.example.com"})) is None


def test_joined_credential_check_is_not_waived_on_an_exempt_host() -> None:
    path = "/p?x=" + _KEY_ID[:8] + "&y=" + _KEY_ID[8:]
    assert _exfil_url_warning("tenant.example.com", path, frozenset({"tenant.example.com"}))


def test_joined_credential_check_holds_on_an_approved_oauth_endpoint() -> None:
    base = "https://api.notion.com/v1/oauth/authorize?client_id=abc&response_type=code"
    assert oauth_url_contains_credential(base) is False
    assert oauth_url_contains_credential(f"{base}&x={_KEY_ID[:8]}&y={_KEY_ID[8:]}") is True
