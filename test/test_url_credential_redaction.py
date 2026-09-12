"""URLs must survive credential redaction; credentials inside them must not.

``/`` is in the base64 alphabet, so ``_BARE_SECRET_RUN_RE`` does not stop at a URL
path separator. In ``https://docs.google.com/document/d/<44-char id>/edit`` the
whole of ``com/document/d/<id>/edit`` is scanned as ONE 64-char run; a sliding
40-char window lands inside the document ID, and a Google document ID is
structurally identical to an AWS secret access key -- ~40 uniformly random base64
characters -- so every entropy and structure gate passes. ``redact_credentials``
then replaced the whole run, taking the host and path with it and emitting
``https://docs.google.[REDACTED: credential]?usp=sharing``.

Whether a link survived was a coin flip: an ID containing a ``-`` or ``_``
(neither is in the run alphabet) broke the run into sub-40 pieces and escaped.
The document IDs below are synthetic but were each selected to reproduce the
defect, so this file fails on the unfixed code rather than passing vacuously.

The fix (``_url_anchored_secret_windows``) requires a window inside a URL to
occupy WHOLE path segments -- both ends anchored on a ``/`` boundary or the end
of the run. It must NOT regress detection: an AWS secret key contains a ``/``
about 46% of the time, so the naive "split the run on /" alternative would
shatter and leak roughly half of all real keys. Both properties are asserted.
"""

import base64
import os

import pytest

from kiro_crew.security import redact_credentials
from kiro_crew.security.redaction import (
    _looks_like_secret_key,
    _url_anchored_secret_windows,
)

# Synthetic, but each reproduces the defect on the unfixed code: 44 chars, no
# '-' or '_', high enough entropy that a 40-char window clears every gate.
DOC_IDS = [
    "1MGmoFUttsfqbA7oQDRBZASmPInLd0HNlXVU1spM9NVU",
    "1L56xKh58IjAg1M4KJpjGROQDiVCYyt2p0HmgenHlwLo",
    "1Q4uQ9ifwf7EtwZRVkWop3yZQEwo3ppEmENbk0vdAuP3",
    "17DaHa5crAyHhwuRQKsSaKzsAhEvkEW25dgO2hP02AAq",
    "1siCg83gYKlHO8XmlBvLhcMYuEpsvhhMU59w172nSZnv",
    "1ahx96VrqiVKk4NOGuSeknuhE4ccfuf7QOvxUKEMsJKv",
    "1anlMMGnQhbL1fMWnVfSxDAfcJKgkXBSI5gX89qfgOIP",
    "18e1aYVZzJ1FkbqVaYo9aWv4tTddDOeqwdN5LoCcyila",
]

BENIGN_URLS = [
    *[f"https://docs.google.com/document/d/{i}/edit?usp=sharing" for i in DOC_IDS],
    f"https://docs.google.com/spreadsheets/d/{DOC_IDS[0]}/edit#gid=0",
    f"https://docs.google.com/presentation/d/{DOC_IDS[1]}/edit",
    f"https://drive.google.com/file/d/{DOC_IDS[2]}/view",
    "https://github.com/example/example/blob/mainBranchAbc123XyZ/README.md",
    "https://www.notion.so/Meeting-Notes-a1b2c3d4e5f6478392bcde0192837465",
    "https://example.slack.com/archives/C01234567AB/p1785511539154849",
    "https://www.dropbox.com/scl/fi/aB3xK9mQ2pR7tZ1vY4nL8/Report.pdf",
]


def _aws_key() -> str:
    """A value shaped exactly like an AWS secret access key: 40 base64 chars."""
    while True:
        candidate = base64.b64encode(os.urandom(30)).decode()[:40]
        if _looks_like_secret_key(candidate):
            return candidate


@pytest.mark.parametrize("url", BENIGN_URLS)
def test_benign_document_urls_survive_verbatim(url):
    cleaned, warnings = redact_credentials(url)
    assert cleaned == url, f"link was corrupted: {cleaned}"
    assert warnings == []


def test_a_document_link_inside_prose_is_untouched():
    msg = (
        "The doc is ready for tomorrow's meeting:\n"
        f"https://docs.google.com/document/d/{DOC_IDS[0]}/edit?usp=sharing\n"
        "Anyone with the link can view it."
    )
    cleaned, _ = redact_credentials(msg)
    assert cleaned == msg
    assert "REDACTED" not in cleaned


def test_bare_key_in_url_path_is_still_redacted_and_host_survives():
    key = _aws_key()
    cleaned, warnings = redact_credentials(f"https://collector.example/collect/{key}")
    assert key not in cleaned, "a bare key in a URL path leaked"
    assert "collector.example" in cleaned, "host should stay visible for triage"
    assert warnings


def test_bare_key_in_prose_is_still_redacted():
    key = _aws_key()
    cleaned, warnings = redact_credentials(f"the key is {key} ok")
    assert key not in cleaned
    assert warnings


def test_glued_key_in_prose_is_still_redacted():
    key = _aws_key()
    assert key not in redact_credentials(f"SECRET={key}ABC")[0]


def test_slash_bearing_keys_in_urls_are_not_shattered():
    """Rules out the naive 'split the run on /' alternative.

    ~46% of real AWS secret keys contain a '/'. Segment-wise testing would break
    them into sub-40 fragments and leak them; anchoring only the ENDS does not.
    """
    checked = 0
    for _ in range(4000):
        key = _aws_key()
        if "/" not in key:
            continue
        cleaned, _ = redact_credentials(f"https://collector.example/collect/{key}")
        assert key not in cleaned, f"slash-bearing key leaked: {key}"
        checked += 1
        if checked >= 100:
            break
    assert checked >= 25, f"corpus too small to be meaningful ({checked})"


def test_detection_rate_on_keys_in_url_paths_is_total():
    misses = [
        key
        for key in (_aws_key() for _ in range(500))
        if key in redact_credentials(f"https://collector.example/collect/{key}")[0]
    ]
    assert not misses, f"{len(misses)} keys survived redaction in a URL path"


def test_anchored_windows_require_both_ends_on_a_boundary():
    key = _aws_key()
    assert _url_anchored_secret_windows(f"collect/{key}"), "whole-segment key must match"
    assert not _url_anchored_secret_windows(f"d/{key}XYZ/edit"), (
        "a window floating inside a longer opaque ID must not match"
    )


def test_bare_key_in_a_query_string_is_still_redacted():
    """The anchored rule is about PATH structure and must not reach the query.

    A query value has no '/' boundaries, so requiring an anchored window there
    would silently stop catching keys glued to extra characters --
    ``?code_challenge=<key>abc``. Caught by an upstream OAuth-redaction test
    before this reached anyone; guarded here so it stays caught.
    """
    key = _aws_key()
    url = f"https://idp.example/authorize?code_challenge={key}abc&state=xyz"
    cleaned, warnings = redact_credentials(url)
    assert key not in cleaned, "a bare key in a URL query string leaked"
    assert warnings
