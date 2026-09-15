"""The shared path-credential signer, and the isolation it must not lose.

Two channels now share this ALGORITHM. The point of these tests is that they do
not share a KEY: Amazon's "Avoid Hard-coded HMAC Key and Function used for URL
Redirect Token Generation" best practice warns that one centralized helper
backed by a single well-known key means one leak forges credentials for every
caller. A refactor that "simplified" the signer into a module-level secret would
be exactly that regression, and would otherwise pass every functional test.
"""

from __future__ import annotations

import time

import pytest

from kiro_crew.dashboard.handlers.path_token import PathTokenSigner


def test_two_signers_do_not_accept_each_others_credentials() -> None:
    """The isolation invariant: shared algorithm, independent keys.

    Same namespace, same parts, same moment — only the secret differs, so a
    token minted by one signer must be worthless to the other. If this passes
    for the wrong reason (a shared module-level secret), a leak in one channel
    forges credentials for all of them.
    """
    a = PathTokenSigner("same-name", 900)
    b = PathTokenSigner("same-name", 900)
    token = a.mint("doc1", "10.0.0.1")

    assert a.verify(token, "doc1", "10.0.0.1")
    assert not b.verify(token, "doc1", "10.0.0.1"), (
        "a second signer accepted the first's credential — the two are sharing a "
        "secret, so one leaked key now forges credentials for every channel"
    )


def test_a_namespace_separates_two_channels_messages() -> None:
    """Namespaces keep channels apart even in the same-secret hypothetical."""
    signer = PathTokenSigner("channel-one", 900)
    other = PathTokenSigner("channel-two", 900)
    # Reach past the constructor to model the one thing the design forbids, so
    # the namespace is proven to carry weight on its own.
    other._secret = signer._secret
    token = signer.mint("res")
    assert signer.verify(token, "res")
    assert not other.verify(token, "res"), (
        "a credential minted for one channel verified on another; the namespace "
        "is not actually bound into the message"
    )


def test_the_bound_parts_are_unambiguous() -> None:
    """Parts are delimited, so a regrouping cannot collide."""
    signer = PathTokenSigner("ns", 900)
    token = signer.mint("ab", "c")
    assert signer.verify(token, "ab", "c")
    assert not signer.verify(token, "a", "bc"), (
        "two different part groupings produced the same message — the parts are "
        "being concatenated rather than delimited"
    )


def test_an_expired_credential_is_refused() -> None:
    signer = PathTokenSigner("ns", 900)
    exp = int(time.time()) - 1
    stale = f"{exp}.{signer._mac(exp, ('res',))}"
    assert not signer.verify(stale, "res")


@pytest.mark.parametrize(
    "token",
    ["", "nonsense", "9999999999.", ".abc", "abc.def", f"{'9' * 400}.x"],
)
def test_a_malformed_credential_is_refused_without_raising(token: str) -> None:
    """Including the digit-limit case: a huge expiry must not become a 500."""
    signer = PathTokenSigner("ns", 900)
    assert signer.verify(token, "res") is False


def test_a_signer_refuses_a_nonsense_configuration() -> None:
    with pytest.raises(ValueError):
        PathTokenSigner("", 900)
    with pytest.raises(ValueError):
        PathTokenSigner("ns", 0)
