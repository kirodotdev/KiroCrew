"""A URL is never replaced whole: only a credential VALUE inside it is masked.

The rule table masks a credential wherever it sits in a URL (path, query, fragment)
and leaves the scheme, host, path and every other parameter readable. A URL with no
credential in it, however long or random-looking its query, is left as written.
"""

from __future__ import annotations

import pytest

from kiro_crew import security
from kiro_crew.security import exfil
from kiro_crew.testing.synthetic_credentials import ALNUM, github_classic, synth

_B64URL = ALNUM + "-_"
_TAG = security.REDACTED_CREDENTIAL_TAG

_BENIGN_URLS = [
    # An OAuth sign-in link: every parameter is public.
    "https://auth.example.test/oauth2/authorize?response_type=code&client_id=app-12345"
    "&redirect_uri=https%3A%2F%2Fapp.example.test%2Fcallback&scope=openid%20profile%20email"
    f"&state={synth(43, _B64URL, 1)}&code_challenge={synth(43, _B64URL, 2)}"
    "&code_challenge_method=S256&nonce=" + synth(32, _B64URL, 3),
    # Its callback: an authorization code is single use and needs the client secret
    # or the PKCE verifier to redeem, so it stays readable.
    f"https://app.example.test/callback?code={synth(40, _B64URL, 4)}&state={synth(43, _B64URL, 5)}",
    # A long tracking query and a base64-looking value that decodes to no credential.
    "https://shop.example.test/item/42?"
    + "&".join(f"utm_p{i}={synth(30, ALNUM, i)}" for i in range(20)),
    "https://docs.example.test/view?doc=" + synth(120, _B64URL, 7) + "#section-3",
    "https://github.com/kirodotdev/KiroCrew/pull/37408/files#diff-"
    + synth(64, "0123456789abcdef", 8),
]


@pytest.mark.parametrize("url", _BENIGN_URLS)
def test_a_url_with_no_credential_stays_as_written(url):
    text = f"open {url} please"
    assert security.redact(text) == text
    assert exfil.redact_exfiltration_urls(text) == (text, [])


def test_a_token_in_a_query_masks_the_value_only():
    token = github_classic()
    url = f"https://collect.example.test/p?ref=readme&t={token}&lang=en"
    assert security.redact(url) == url.replace(token, _TAG)


def test_a_token_parameter_masks_the_value_only():
    value = synth(40, _B64URL, 9)
    url = f"https://files.example.test/d/report.pdf?token={value}&dl=1"
    assert security.redact(url) == url.replace(value, _TAG)


def test_the_url_pass_leaves_text_outside_every_url_to_the_credential_pass():
    token = github_classic()
    text = f"token {token} and https://ok.example.test/x?a=1"
    out, warnings = exfil.redact_exfiltration_urls(text)
    assert out == text and warnings == []
    assert security.redact(text) == text.replace(token, _TAG)


def test_a_credential_inside_a_url_is_masked_by_the_url_pass_too():
    token = github_classic()
    text = f"see https://collect.example.test/p?t={token}&n=1 now"
    out, warnings = exfil.redact_exfiltration_urls(text)
    assert out == text.replace(token, _TAG)
    assert len(warnings) == 1 and token not in warnings[0]


def test_no_placeholder_replaces_a_url():
    text = "https://evil.example.test/c?d=" + synth(400, _B64URL, 10)
    assert security.redact(text) == text
    assert "suspicious URL" not in security.redact(text)
