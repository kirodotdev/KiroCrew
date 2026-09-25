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

    ``_CANONICAL_CREDENTIAL_HIT`` is None wherever ``kiro_crew`` is not importable --
    which is the container-adjacent case this module is built to survive -- so the
    local patterns have to find it on their own.
    """
    mod = load_build()
    monkeypatch.setattr(mod, "_CANONICAL_CREDENTIAL_HIT", None)
    leaks = mod.scan_text(f"SecretAccessKey={_DOC_SECRET}", "prompt")
    kinds = {leak.kind for leak in leaks}
    assert "aws-secret-labelled" in kinds, kinds


def test_the_key_id_pattern_still_works():
    """The original coverage must survive the addition."""
    mod = load_build()
    kinds = {leak.kind for leak in mod.scan_text(_DOC_KEY_ID, "prompt")}
    assert "aws-access-key" in kinds, kinds


_TAG = "[REDACTED: credential]"


@pytest.mark.parametrize(
    "line",
    [
        f"aws_secret_access_key={_TAG}",
        f'SecretAccessKey="{_TAG}"',
        f"aws_session_token: '{_TAG}' # rotated",
        f'{{"SessionToken": "{_TAG}", "Expiration": "2030-01-01T00:00:00Z"}}',
        f'{{"text": "aws_secret_access_key=\\"{_TAG}\\""}}',
        f"aws_secret_access_key={_TAG}{_TAG}",
    ],
)
def test_a_stored_skill_the_redactor_already_cleaned_is_not_a_finding(line, monkeypatch):
    """Skills are stored already redacted, and the redactor keeps the key that names a
    value: a cleaned skill reads ``aws_secret_access_key=[REDACTED: credential]``. The
    labelled pattern runs on every line whether or not the canonical detector loads, and
    a copy that matched the tag's ``[REDACTED:`` head as the value aborted the crew build
    on text that holds no secret -- with hand-editing the file the only way out. A tag
    run that FILLS the value (bare, quoted by the same quote, escaped inside a JSON
    string, or a run of two) is declined, on both branches."""
    mod = load_build()
    assert mod.scan_text(line, "skill") == [], line
    monkeypatch.setattr(mod, "_CANONICAL_CREDENTIAL_HIT", None)
    assert mod.scan_text(line, "skill") == [], line


@pytest.mark.parametrize(
    "line",
    [
        f'aws_secret_access_key="{_TAG} {_DOC_SECRET}"',
        f'aws_secret_access_key=\'{_TAG}"{_DOC_SECRET}"',
        f"SessionToken={_TAG}{_DOC_SECRET}",
        f"aws_secret_access_key={_TAG.lower()}",
        # An escape pair glued to the tag, a doubled quote taken for the close, and
        # a secret written with escaped slashes (PHP-style JSON), heading included.
        f"aws_secret_access_key={_TAG}\\/{_DOC_SECRET}",
        f"aws_secret_access_key='{_TAG}''{_DOC_SECRET}'",
        f'{{"SecretAccessKey": "\\/{_DOC_SECRET[:12]}\\/{_DOC_SECRET[12:]}"}}',
        f"aws_secret_access_key={_DOC_SECRET[:12]}\\/{_DOC_SECRET[12:]}",
    ],
)
def test_a_tag_that_does_not_fill_its_value_is_still_a_finding(line, monkeypatch):
    """The exemption is byte identity of the WHOLE value with a registered tag run:
    a tag heading a quoted value, a run closed by the other quote kind, glued bytes
    (an escape pair included), a run closed by a doubled quote and a tag in another
    case are values, and findings, on both branches -- and a secret whose ``/`` an
    encoder escaped is a finding whole, however the escape sits."""
    mod = load_build()
    monkeypatch.setattr(mod, "_CANONICAL_CREDENTIAL_HIT", None)
    kinds = {leak.kind for leak in mod.scan_text(line, "skill")}
    assert "aws-secret-labelled" in kinds, (line, kinds)


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
