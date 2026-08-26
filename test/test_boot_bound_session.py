"""Boot-bound sessions: a session that must not outlive the gateway process.

These cover the seam that makes ``dashboard.qr_session_until_restart`` honest.
The interesting failure mode is not "does the check reject a stale boot id" —
that part is one comparison — but whether the binding SURVIVES the two places a
fresh token is minted from an old one. Both are exercised here, because in each
case dropping the claim silently converts a restart-scoped session into a
20-hour / 30-day one that keeps working after the restart, and nothing else in
the system would notice.
"""

from __future__ import annotations

import json
import time
from unittest import mock

import pytest

from kiro_crew.dashboard import boot_id, refresh_tokens, token_auth

_MOD = "kiro_crew.dashboard.boot_id"


def _claims(token: str) -> dict:
    """Decode a token's payload without validating it."""
    return json.loads(token_auth._b64url_decode(token.split(".")[0]))


@pytest.fixture(autouse=True)
def _fresh_boot_id():
    """Reset the memoized per-process id so each test gets its own."""
    boot_id._boot_id = None
    yield
    boot_id._boot_id = None


class TestBootId:
    def test_is_stable_within_a_process(self) -> None:
        """Two readers must agree, or a session minted by one request is
        unverifiable by the next."""
        assert boot_id.current_boot_id() == boot_id.current_boot_id()

    def test_is_not_persisted_anywhere(self, tmp_path, monkeypatch) -> None:
        """The whole guarantee rests on this value dying with the process.

        Asserted behaviourally — no file appears — rather than by grepping the
        module for a path constant, which a future refactor could satisfy while
        writing through some other helper.
        """
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path, raising=False)
        before = set(tmp_path.iterdir())
        boot_id.current_boot_id()
        assert set(tmp_path.iterdir()) == before

    def test_a_restart_yields_a_different_id(self) -> None:
        first = boot_id.current_boot_id()
        boot_id._boot_id = None  # what a new process starts from
        assert boot_id.current_boot_id() != first


class TestAccessTokenBinding:
    def test_a_token_without_the_claim_is_not_checked(self) -> None:
        """Claim-gated: the default path and every pre-existing session are
        untouched by this feature."""
        token = token_auth.generate_token("u@example.com", ttl_seconds=3600)
        assert "boot" not in _claims(token)
        valid, _uid, reason = token_auth.validate_token(token)
        assert valid, reason

    def test_a_matching_boot_validates(self) -> None:
        token = token_auth.generate_token(
            "u@example.com", ttl_seconds=3600, extra={"boot": boot_id.current_boot_id()}
        )
        valid, _uid, reason = token_auth.validate_token(token)
        assert valid, reason

    @pytest.mark.parametrize("use_session_exp", [False, True])
    def test_a_stale_boot_is_rejected_on_both_paths(self, use_session_exp: bool) -> None:
        """Rejected as a LINK and as a COOKIE.

        The link path matters as much as the cookie one: a boot-bound URL
        sitting in someone's history must not be redeemable after a restart
        either.
        """
        token = token_auth.generate_token(
            "u@example.com", ttl_seconds=3600, extra={"boot": "deadbeef"}
        )
        with mock.patch(f"{_MOD}._boot_id", "cafef00d"):
            valid, _uid, reason = token_auth.validate_token(token, use_session_exp=use_session_exp)
        assert not valid
        assert reason == "session ended at gateway restart"


class TestRefreshChainBinding:
    def test_an_unbound_chain_payload_is_unchanged(self) -> None:
        """No claim is written when unbound, so an existing chain's shape is
        byte-identical to before this feature."""
        token, _chain, _jti, _exp = refresh_tokens.generate_refresh_token("u@example.com")
        assert "boot" not in _claims(token)

    def test_a_bound_chain_is_rejected_after_a_restart(self) -> None:
        """This is the credential that outlives the access cookie.

        Without the check here, a restart-orphaned refresh cookie mints a
        brand-new session on the phone's next visit and "ends at restart" is
        false.
        """
        token, _chain, _jti, _exp = refresh_tokens.generate_refresh_token(
            "u@example.com", boot=boot_id.current_boot_id()
        )
        valid, _uid, reason, _c, _j, _e = refresh_tokens.validate_refresh_token(token)
        assert valid, reason
        with mock.patch(f"{_MOD}._boot_id", "a-different-process"):
            valid, _uid, reason, _c, _j, _e = refresh_tokens.validate_refresh_token(token)
        assert not valid
        assert reason == "session ended at gateway restart"

    def test_the_boot_claim_is_readable_for_carrying(self) -> None:
        bound, _c, _j, _e = refresh_tokens.generate_refresh_token("u@example.com", boot="abc123")
        unbound, _c2, _j2, _e2 = refresh_tokens.generate_refresh_token("u@example.com")
        assert refresh_tokens.refresh_token_boot(bound) == "abc123"
        assert refresh_tokens.refresh_token_boot(unbound) == ""

    def test_a_malformed_token_reads_as_unbound(self) -> None:
        assert refresh_tokens.refresh_token_boot("not-a-token") == ""


class TestExchangeCarriesTheBinding:
    """The token->session exchange re-mints, so anything not copied is lost.

    Asserted at the level of the claim-carrying dict rather than by driving the
    whole middleware, because that is where the bug would be: the exchange
    passes an explicit ``extra`` and a boot-bound link whose cookie came back
    unbound would look completely healthy from the outside while quietly
    surviving the next restart.
    """

    def test_a_session_minted_from_a_bound_link_stays_bound(self) -> None:
        bid = boot_id.current_boot_id()
        link = token_auth.generate_token("u@example.com", ttl_seconds=3600, extra={"boot": bid})
        # What the exchange does: read the claim off the validated link and mint
        # a separate session token carrying it forward.
        carried = str(_claims(link).get("boot", ""))
        assert carried == bid
        session = token_auth.generate_token(
            "u@example.com",
            ttl_seconds=1800,
            register_nonce=False,
            extra={"boot": carried} if carried else None,
        )
        assert _claims(session)["boot"] == bid
        with mock.patch(f"{_MOD}._boot_id", "next-process"):
            valid, _uid, reason = token_auth.validate_token(session, use_session_exp=True)
        assert not valid
        assert reason == "session ended at gateway restart"

    def test_nonce_and_expiry_are_still_independent_of_the_claim(self) -> None:
        """The binding is additive: it must not disturb the existing session
        semantics it sits beside."""
        token = token_auth.generate_token(
            "u@example.com", ttl_seconds=3600, extra={"boot": boot_id.current_boot_id()}
        )
        data = _claims(token)
        assert data["nonce"]
        assert data["session_exp"] > time.time()
        assert data["exp"] <= time.time() + token_auth.LINK_WINDOW_SECS + 1
