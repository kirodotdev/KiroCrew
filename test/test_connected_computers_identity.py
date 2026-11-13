"""Connected computers: device identity and the connector credential (B09, B11).

Scenarios (docs/system-specs/modules/providers.md, "Connected computers"):

* B09 -- two computers with the same human name get distinct, stable ids; only
  the one a chat selected is the device its credential proves.
* B11 -- a wrong-owner, wrong-device, wrong-gateway, expired, revoked or
  superseded credential is refused, and a refused one cannot take over a live
  device's identity.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew.connected_computers import connector_token, protocol
from kiro_crew.connected_computers.connector_token import CredentialRejected, mint, verify
from kiro_crew.connected_computers.devices import (
    STATUS_REMOVED,
    STATUS_REVOKED,
    DeviceRegistry,
    EnrollmentRefused,
)
from kiro_crew.connected_computers.identity import (
    IdentityError,
    assign_device_id,
    hostname_slug,
    install_fingerprint,
    is_device_id,
    validate_device_id,
    validate_fingerprint,
)
from kiro_crew.secrets.vault import SecretVault

GATEWAY = "gw-test-0001"


def _fp(seed: int) -> str:
    return install_fingerprint(bytes([seed]) * 32)


@pytest.fixture
def registry(tmp_path):
    return DeviceRegistry(SecretVault(tmp_path / "vault-home"))


@pytest.fixture(autouse=True)
def _isolated_token_secret(tmp_path, _floor_monkeypatch):
    # The credential is signed with the gateway's token secret under the data home.
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))


class TestDeviceIds:
    @pytest.mark.parametrize(
        "hostname,slug",
        [
            ("Zezhen's MacBook-Pro.local", "zezhen-s-macbook-pro"),
            ("WORKSTATION", "workstation"),
            ("dev-dsk-1.example.com", "dev-dsk-1"),
            ("", "computer"),
            ("!!!", "computer"),
        ],
    )
    def test_slug_comes_from_the_host_name(self, hostname, slug):
        assert hostname_slug(hostname) == slug
        assert is_device_id(slug)

    def test_b09_same_host_name_gets_a_differentiator_only_on_collision(self):
        first = assign_device_id("laptop", _fp(1), {})
        second = assign_device_id("laptop", _fp(2), {first: _fp(1)})
        assert first == "laptop"
        assert second.startswith("laptop-") and second != first
        assert second == f"laptop-{_fp(2)[:4]}"
        assert is_device_id(second)

    def test_b09_an_install_keeps_its_id_after_a_rename(self):
        existing = {"laptop": _fp(1)}
        assert assign_device_id("renamed-host", _fp(1), existing) == "laptop"

    def test_b09_never_returns_an_id_another_fingerprint_holds(self):
        other = _fp(9)
        mine = _fp(2)
        existing = {"laptop": other, f"laptop-{mine[:4]}": other}
        got = assign_device_id("laptop", mine, existing)
        assert got not in existing

    def test_fingerprint_needs_a_32_byte_key(self):
        with pytest.raises(IdentityError):
            install_fingerprint(b"short")


class TestIdentityGrammar:
    """The id, fingerprint and slug grammars, pinned at their boundaries."""

    @pytest.mark.parametrize(
        "value",
        ["a", "0", "ab", "a-b", "a--b", "a" * 48, "a" + "-" * 46 + "b", "laptop-1f2e"],
    )
    def test_a_device_id_is_accepted(self, value):
        assert validate_device_id(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "a" * 49,
            "-a",
            "a-",
            "-",
            "A",
            "Laptop",
            "lap_top",
            "lap.top",
            "lap top",
            "lap/top",
            "laptop\n",
            "\nlaptop",
            "lapt\u00f6p",
            "\uff41",
            "\u0661",
            None,
            7,
            b"laptop",
        ],
    )
    def test_a_malformed_device_id_is_refused(self, value):
        with pytest.raises(IdentityError, match="device id is invalid"):
            validate_device_id(value)

    def test_a_fingerprint_is_64_lower_case_hex(self):
        fp = _fp(3)
        assert validate_fingerprint(fp) == fp
        assert validate_fingerprint("0" * 64) == "0" * 64

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "0" * 63,
            "0" * 65,
            "A" * 64,
            "g" * 64,
            "0" * 63 + "\n",
            "0" * 64 + "\n",
            " " + "0" * 63,
            "00 " * 21 + "0",
            "\u0660" * 64,
            None,
            b"0" * 64,
        ],
    )
    def test_a_malformed_fingerprint_is_refused(self, value):
        with pytest.raises(IdentityError, match="device fingerprint is invalid"):
            validate_fingerprint(value)

    @pytest.mark.parametrize(
        "hostname,slug",
        [
            ("a--b", "a-b"),
            ("a_.b", "a"),
            ("a_b.c", "a-b"),
            ("--a--", "a"),
            ("caf\u00e9-bar", "caf-bar"),
            ("\u00c9cole", "cole"),
            ("x" * 40, "x" * 36),
            ("x" * 35 + "-y", "x" * 35),
            ("  Host Name  ", "host-name"),
            ("\u4e2d\u6587", "computer"),
        ],
    )
    def test_a_slug_folds_each_run_of_other_characters_into_one_hyphen(self, hostname, slug):
        assert hostname_slug(hostname) == slug
        assert is_device_id(slug)


class TestRegistry:
    def test_b09_two_computers_with_one_name_are_two_records(self, registry):
        a = registry.enroll(base="laptop", fingerprint=_fp(1), label="Laptop", owner="me")
        b = registry.enroll(base="laptop", fingerprint=_fp(2), label="Laptop", owner="me")
        assert a.device_id != b.device_id
        assert a.label == b.label == "Laptop"
        assert {r.device_id for r in registry.list()} == {a.device_id, b.device_id}

    def test_reenrolling_keeps_the_id_and_bumps_the_generation(self, registry):
        first = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        again = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        assert again.device_id == first.device_id
        assert again.cgen == first.cgen + 1

    def test_a_renewal_keeps_the_generation(self, registry):
        first = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        renewed = registry.enroll(
            base="laptop", fingerprint=_fp(1), label="L", owner="me", renew=True, renew_cgen=1
        )
        assert renewed.cgen == first.cgen == 1 and renewed.active

    def test_b11_another_owner_cannot_reenroll_a_computer(self, registry):
        registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        with pytest.raises(EnrollmentRefused):
            registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="someone-else")

    def test_b07_b11_a_renewal_never_revives_a_revoked_computer(self, registry):
        rec = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        registry.revoke(rec.device_id)
        with pytest.raises(EnrollmentRefused, match="revoked"):
            registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me", renew=True)
        assert registry.get(rec.device_id).status == STATUS_REVOKED

    def test_b12_a_removed_computer_is_never_reenrolled(self, registry):
        rec = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        registry.remove(rec.device_id)
        for renew in (True, False):
            with pytest.raises(EnrollmentRefused, match="removed"):
                registry.enroll(
                    base="laptop", fingerprint=_fp(1), label="L", owner="me", renew=renew
                )
        assert registry.get(rec.device_id).status == STATUS_REMOVED

    @pytest.mark.parametrize("action", ["revoke", "remove"])
    def test_a_removed_record_is_terminal_even_after_another_narrowing(self, registry, action):
        rec = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        removed = registry.remove(rec.device_id)
        assert getattr(registry, action)(rec.device_id) == removed
        assert registry.get(rec.device_id) == removed
        for renew in (True, False):
            with pytest.raises(EnrollmentRefused, match="removed"):
                registry.enroll(
                    base="laptop", fingerprint=_fp(1), label="L", owner="me", renew=renew
                )

    def test_b02_a_fresh_consent_may_reenroll_a_revoked_computer(self, registry):
        rec = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        registry.revoke(rec.device_id)
        again = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        assert again.active and again.device_id == rec.device_id

    def test_renewal_needs_an_existing_active_record(self, registry):
        with pytest.raises(EnrollmentRefused):
            registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me", renew=True)

    def test_b12_revoke_and_remove_keep_the_record_and_the_id(self, registry):
        rec = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        revoked = registry.revoke(rec.device_id)
        assert revoked is not None and revoked.status == STATUS_REVOKED
        removed = registry.remove(rec.device_id)
        assert removed is not None and removed.status == STATUS_REMOVED
        # The id is never handed to another computer.
        other = registry.enroll(base="laptop", fingerprint=_fp(2), label="L", owner="me")
        assert other.device_id != rec.device_id

    def test_state_lives_in_the_vault_not_a_plain_file(self, tmp_path, registry):
        registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        for dirpath, _dirs, files in os.walk(tmp_path / "vault-home"):
            for name in files:
                with open(os.path.join(dirpath, name), "rb") as fh:
                    assert b"laptop" not in fh.read(), "device state must be encrypted"


class TestCredential:
    def test_a_minted_credential_verifies(self, registry):
        rec = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        token, _expires = mint(rec, GATEWAY)
        assert verify(token, gateway_id=GATEWAY, registry=registry) == rec

    def test_b11_wrong_gateway_is_refused(self, registry):
        rec = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        token, _ = mint(rec, GATEWAY)
        with pytest.raises(CredentialRejected) as exc:
            verify(token, gateway_id="gw-other", registry=registry)
        assert exc.value.code == "wrong_gateway"

    @pytest.mark.parametrize("action", ["revoke", "remove"])
    def test_b11_revoked_or_removed_is_refused(self, registry, action):
        rec = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        token, _ = mint(rec, GATEWAY)
        getattr(registry, action)(rec.device_id)
        with pytest.raises(CredentialRejected) as exc:
            verify(token, gateway_id=GATEWAY, registry=registry)
        # Named by the record's status: the computer ends its grant on it.
        assert exc.value.code == {"revoke": "revoked", "remove": "removed"}[action]
        assert exc.value.code in protocol.TERMINAL_REFUSALS

    def test_b11_superseded_credential_is_refused(self, registry):
        rec = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        old, _ = mint(rec, GATEWAY)
        newer = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        fresh, _ = mint(newer, GATEWAY)
        with pytest.raises(CredentialRejected) as exc:
            verify(old, gateway_id=GATEWAY, registry=registry)
        assert exc.value.code == "superseded"
        assert verify(fresh, gateway_id=GATEWAY, registry=registry).cgen == newer.cgen

    def test_b11_one_computers_credential_never_proves_another(self, registry):
        a = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        b = registry.enroll(base="laptop", fingerprint=_fp(2), label="L", owner="me")
        token_a, _ = mint(a, GATEWAY)
        assert verify(token_a, gateway_id=GATEWAY, registry=registry).device_id == a.device_id
        assert verify(token_a, gateway_id=GATEWAY, registry=registry).device_id != b.device_id

    def test_b11_expired_credential_is_refused(self, registry, monkeypatch):
        from kiro_crew.dashboard import token_auth_tokens
        from kiro_crew.testing.clock import ManualClock

        clock = ManualClock()
        clock.install(monkeypatch, token_auth_tokens)
        clock.install(monkeypatch, connector_token)
        rec = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        token, _ = mint(rec, GATEWAY)
        clock.advance(connector_token.CREDENTIAL_TTL_SECS + 1)
        with pytest.raises(CredentialRejected) as exc:
            verify(token, gateway_id=GATEWAY, registry=registry)
        # Cured by renewing over SSH, which still accepts it.
        assert exc.value.code == "expired" == protocol.EXPIRED_REFUSAL
        assert connector_token.verify_standing(token, gateway_id=GATEWAY, registry=registry) == rec

    def test_a_dashboard_logout_does_not_reach_a_connector_credential(self, registry, monkeypatch):
        """Neither the global revocation generation nor the logged-out nonce list."""
        from kiro_crew.dashboard import token_auth, token_auth_tokens

        rec = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        token, _ = mint(rec, GATEWAY)
        minted_gen = token_auth_tokens.current_revocation_gen()
        monkeypatch.setattr(token_auth, "current_revocation_gen_or_none", lambda: minted_gen + 1)
        monkeypatch.setattr(
            token_auth,
            "_get_revoked_store",
            lambda: (_ for _ in ()).throw(AssertionError("the nonce list was consulted")),
        )
        assert verify(token, gateway_id=GATEWAY, registry=registry) == rec

    def test_b11_a_dashboard_token_is_not_a_connector_credential(self, registry):
        from kiro_crew.dashboard.token_auth_tokens import generate_token

        registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        dashboard = generate_token("me", 3600, session=True, register_nonce=False)
        with pytest.raises(CredentialRejected) as exc:
            verify(dashboard, gateway_id=GATEWAY, registry=registry)
        assert exc.value.code == "wrong_audience"

    def test_a_connector_credential_is_refused_by_every_dashboard_sink(self, registry):
        from kiro_crew.dashboard import token_auth_tokens as tokens

        assert tokens.CONNECTOR_AUDIENCE == connector_token.AUDIENCE
        rec = registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")
        token, _ = mint(rec, GATEWAY)
        refused = (False, "", tokens.CONNECTOR_CREDENTIAL_REASON)
        assert tokens.validate_token(token) == refused, "the link path"
        assert tokens.validate_token(token, use_session_exp=True) == refused, "the cookie path"
        assert tokens.validate_token_with_app(token, use_session_exp=True)[0] is False
        assert tokens.extract_claims_from_token(token, ("aud", "device")) == {}
        assert tokens.token_embed_parent_port(token) is None
        assert tokens.revoke_access_cookie(token) is False, "logout cannot revoke it"

    @pytest.mark.parametrize("bad", ["", "x", "a.b", "x" * 5000])
    def test_b11_garbage_is_refused(self, registry, bad):
        with pytest.raises(CredentialRejected):
            verify(bad, gateway_id=GATEWAY, registry=registry)
