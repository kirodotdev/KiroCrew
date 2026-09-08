"""A labelled AWS secret in a prompt must abort the build.

The scanner's AWS pattern matches a key ID, which carries a recognisable ``AKIA``/
``ASIA`` prefix. The SECRET access key is 40 characters of base64 with no prefix, so
nothing prefix-based can see it, and ``SecretAccessKey=<secret>`` in a prompt reached
the deployed image. What makes it findable is the label -- which is how this repo's own
detector finds it.

Both halves are pinned here: the local subset (which is what runs when ``kiro_crew``
is not importable) and the canonical detector (preferred when it is).
"""

from __future__ import annotations

import pytest

from .test_producer import load_build

# Example values from AWS's own documentation, so nothing here is a real credential.
_DOC_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_DOC_KEY_ID = "AKIAIOSFODNN7EXAMPLE"


@pytest.mark.parametrize(
    "text",
    [
        f"SecretAccessKey={_DOC_SECRET}",
        f"aws_secret_access_key = {_DOC_SECRET}",
        f'"SecretAccessKey": "{_DOC_SECRET}"',
        "aws_session_token: FQoGZXIvYXdzEBYaDEXAMPLETOKEN",
        "SessionToken=FQoGZXIvYXdzEBYaDEXAMPLETOKEN",
    ],
)
def test_a_labelled_secret_is_a_finding(text):
    mod = load_build()
    leaks = mod.scan_text(text, "prompt")
    assert leaks, f"scanner missed a labelled credential: {text[:32]}…"


def test_the_local_subset_catches_it_without_the_canonical_detector(monkeypatch):
    """The fallback branch must not be the weak one.

    ``_CANONICAL_CREDENTIAL_RE`` is None wherever ``kiro_crew`` is not importable --
    which is the container-adjacent case this module is built to survive -- so the
    local patterns have to find it on their own.
    """
    mod = load_build()
    monkeypatch.setattr(mod, "_CANONICAL_CREDENTIAL_RE", None)
    leaks = mod.scan_text(f"SecretAccessKey={_DOC_SECRET}", "prompt")
    kinds = {leak.kind for leak in leaks}
    assert "aws-secret-labelled" in kinds, kinds


def test_the_key_id_pattern_still_works():
    """The original coverage must survive the addition."""
    mod = load_build()
    kinds = {leak.kind for leak in mod.scan_text(_DOC_KEY_ID, "prompt")}
    assert "aws-access-key" in kinds, kinds


@pytest.mark.parametrize(
    "text",
    [
        "You are the front desk. Answer questions about hours and location.",
        "Explain how to rotate a secret without printing it.",
        "The access key id field is named AccessKeyId in the response schema.",
    ],
)
def test_innocent_prose_is_not_a_finding(text):
    """A scanner that fires on the word 'secret' would make the build unusable.

    The third case is the one worth having: it NAMES a credential field without
    assigning a value, which the labelled pattern must not treat as a leak.
    """
    mod = load_build()
    assert mod.scan_text(text, "prompt") == []
