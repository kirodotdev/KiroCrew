"""The session peer/IP pin must hold for every string that authenticates as
that session, not only for the exact spelling the pin was recorded against.

``_b64url_decode`` uses ``base64.urlsafe_b64decode`` with the stdlib default
``validate=False``, which silently DISCARDS characters outside the base64
alphabet. Inserting a multiple-of-four run of such characters into the encoded
payload therefore produces a DIFFERENT token string that decodes to byte-identical
payload bytes, so the HMAC still verifies and the string authenticates as the
same session.

``TokenStateManager`` therefore keys ``_peer_bindings`` on the signed payload, so
a re-encoded copy of a pinned cookie resolves to that session's entry. Keyed on
the token string it resolves to no entry at all, and ``check_peer`` reads an
absent entry as unbound (``entry is None`` -> ``(True, "")``) -- which is what
would let a stolen access cookie satisfy the pin from any address.

All values here are fabricated fixtures. Nothing reads the real crew home, the
signing key on disk, or any live session store: the HMAC secret and the
revocation generation are both monkeypatched.
"""

import time

import pytest

from kiro_crew.dashboard.token_auth import _state as _token_state
from kiro_crew.dashboard.token_auth import (
    bind_token_peer,
    check_token_peer,
    generate_token,
    validate_token,
)


def has_token_binding(token: str) -> bool:
    """Whether the state manager holds a peer binding for *token*.

    The middleware's first-use re-pin asks this question directly of the state
    manager, so the tests below ask it the same way rather than through a
    module-level wrapper that does not exist.
    """
    return _token_state.has_binding(token)


FIXTURE_SECRET = b"FABRICATED-FIXTURE-HMAC-SECRET-NOT-A-REAL-KEY"
BOUND_ADDR = "ip:10.0.0.1"
ATTACKER_ADDR = "ip:203.0.113.9"
# Four characters outside the base64 alphabet: a multiple of four keeps
# len(encoded) % 4 stable so _b64url_decode's padding arithmetic is unchanged.
JUNK_RUN = "!!!!"


@pytest.fixture
def isolated_token_state(tmp_path, monkeypatch):
    """Sign and validate against fabricated state only -- never the real home."""
    import kiro_crew.dashboard.revocation_gen as revocation_gen
    import kiro_crew.dashboard.token_auth as token_auth

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: FIXTURE_SECRET)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    token_auth._state.clear_all()
    yield
    token_auth._state.clear_all()
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)


def _mutate_encoded_payload(token: str) -> str:
    """A different token string that decodes to the same payload bytes."""
    encoded_payload, signature = token.split(".", 1)
    return f"{encoded_payload[:8]}{JUNK_RUN}{encoded_payload[8:]}.{signature}"


def test_peer_pin_holds_against_token_string_mutation(isolated_token_state):
    session_exp = time.time() + 600
    token = generate_token("fixture-user", ttl_seconds=600, register_nonce=False)
    bind_token_peer(token, BOUND_ADDR, session_exp)

    # The pinned session is refused from another address. This is the control.
    ok_original, _ = check_token_peer(token, ATTACKER_ADDR)
    assert not ok_original, "precondition: the pin must refuse the original token elsewhere"

    mutant = _mutate_encoded_payload(token)
    assert mutant != token, "precondition: the mutation must change the token string"

    valid, user_id, reason = validate_token(mutant, use_session_exp=True)
    assert valid, f"precondition: the mutated string must still validate (reason={reason!r})"
    assert user_id == "fixture-user"

    # The security property: a string that authenticates as this session must
    # satisfy the pin that session was bound to.
    ok_mutant, _mismatch = check_token_peer(mutant, ATTACKER_ADDR)
    assert not ok_mutant, (
        "peer pin bypassed: a base64-mutated copy of the pinned token validates "
        "as the same session but carries no binding, so check_peer fail-opens "
        "and the session is accepted from an unbound address"
    )


def test_first_use_repin_does_not_see_a_mutated_cookie_as_unbound(isolated_token_state):
    """``has_binding`` answers for the session, not for the spelling.

    The middleware re-pins a session it believes has no binding yet. Asked about
    a re-encoded copy of a bound cookie, a string-keyed map answers "unbound",
    which would re-pin that session to whatever address presented the copy.
    """
    session_exp = time.time() + 600
    token = generate_token("fixture-user", ttl_seconds=600, register_nonce=False)
    bind_token_peer(token, BOUND_ADDR, session_exp)

    assert has_token_binding(token), "precondition: the original token is bound"
    assert has_token_binding(_mutate_encoded_payload(token)), (
        "a re-encoded copy of a bound cookie reads as unbound, so the first-use "
        "re-pin would rebind this session to the address that presented the copy"
    )


def test_pin_still_admits_the_bound_peer_through_a_re_encoded_cookie(isolated_token_state):
    """Keying on the payload must not reject the legitimate holder.

    The pin has to keep saying yes from the address it was bound to, whichever
    spelling of the cookie arrives -- otherwise the fix trades a bypass for a
    lockout.
    """
    session_exp = time.time() + 600
    token = generate_token("fixture-user", ttl_seconds=600, register_nonce=False)
    bind_token_peer(token, BOUND_ADDR, session_exp)

    ok, mismatch = check_token_peer(_mutate_encoded_payload(token), BOUND_ADDR)
    assert ok, f"the bound peer was refused its own session (mismatch={mismatch!r})"


def test_trailing_junk_run_is_the_same_session_as_the_original(isolated_token_state):
    """A second mutation shape: the junk run appended rather than inserted.

    Any run of ignored characters whose length is a multiple of four decodes to
    the same payload, so the property cannot depend on where the run sits.
    """
    session_exp = time.time() + 600
    token = generate_token("fixture-user", ttl_seconds=600, register_nonce=False)
    bind_token_peer(token, BOUND_ADDR, session_exp)

    encoded_payload, signature = token.split(".", 1)
    mutant = f"{encoded_payload}{JUNK_RUN}.{signature}"
    valid, _user_id, reason = validate_token(mutant, use_session_exp=True)
    assert valid, f"precondition: the mutated string must still validate (reason={reason!r})"

    ok, _mismatch = check_token_peer(mutant, ATTACKER_ADDR)
    assert not ok, "a trailing junk run sheds the pin"


def test_two_mints_do_not_share_one_pin(isolated_token_state):
    """Distinct sessions must stay distinct.

    A key derived from the payload would be worthless if two mints collided:
    binding one session would silently pin the other. The signed payload carries
    a per-mint nonce and ``iat``, so they do not.
    """
    session_exp = time.time() + 600
    first = generate_token("fixture-user", ttl_seconds=600, register_nonce=False)
    second = generate_token("fixture-user", ttl_seconds=600, register_nonce=False)
    assert first != second

    bind_token_peer(first, BOUND_ADDR, session_exp)
    assert not has_token_binding(second), "a second mint inherited the first one's pin"
    ok, _mismatch = check_token_peer(second, ATTACKER_ADDR)
    assert ok, "an unbound session must stay unbound"


def test_undecodable_tokens_are_not_folded_together(isolated_token_state):
    """A token whose payload cannot be decoded keeps its own key.

    Such a string cannot authenticate at all, but it must not act as a wildcard:
    if every undecodable string shared one key, binding one would answer for all
    of them.
    """
    session_exp = time.time() + 600
    bind_token_peer("A.signature", BOUND_ADDR, session_exp)

    assert has_token_binding("A.signature")
    assert not has_token_binding("B.signature"), "two undecodable strings share one pin entry"
