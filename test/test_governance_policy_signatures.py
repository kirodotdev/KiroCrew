"""Policy signature verification (``identity.signature``) -- mirrors admission.

The signing payload, the verified / unverified / unsigned / unchecked states the
loader records per tier, the opt-in ``require_policy_signature`` boot gate
(``assert_policy_signature_satisfied``) that absence must not satisfy, and the
provenance ``kirocrew policy show`` reports. The archetypes, the loader and the
resolution engine are in ``test_governance_policy.py``; the shared policy-body
builder is ``governance_policy_helpers.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from governance_policy_helpers import _policy_body

from kiro_crew.platform import governance
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import (
    SIGNATURE_UNCHECKED,
    SIGNATURE_UNSIGNED,
    SIGNATURE_UNVERIFIED,
    SIGNATURE_VERIFIED,
    GovernanceCeiling,
    assert_policy_signature_satisfied,
    load_security_policy,
    parse_policy,
    policy_signing_payload,
    resolve,
)


# ──────────────────────────────────────────────────────────────────────────
# Policy signature verification (identity.signature) — mirrors admission
# ──────────────────────────────────────────────────────────────────────────
def _sign_policy(body: dict, secret: str) -> dict:
    """Return *body* with a valid ``identity.signature`` for its issuer."""
    signed = json.loads(json.dumps(body))  # deep copy; body may be reused
    sig = hmac.new(
        secret.encode("utf-8"), policy_signing_payload(signed), hashlib.sha256
    ).hexdigest()
    signed.setdefault("identity", {})["signature"] = sig
    return signed


def _patch_trust(monkeypatch, *, require: bool, keys: dict):
    """Point the loader's trust root at fixed settings (no admission file I/O)."""
    monkeypatch.setattr(
        "kiro_crew.platform.governance._policy_trust_settings",
        lambda: (require, dict(keys), {}),
    )


def _real_trust_file(monkeypatch, tmp_path, *, require: bool, keys: dict):
    """Write a REAL admission file so both trust-root readers agree.

    ``_patch_trust`` stubs ``_policy_trust_settings``, which the loader uses for keys,
    but the enforcement gate reads the opt-in from the admission file directly. Tests
    that exercise the gate therefore need an actual file, not the stub.
    """
    adm = tmp_path / "admission_policy.json"
    adm.write_text(json.dumps({"require_policy_signature": require, "trust_keys": dict(keys)}))
    monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))


def _load_and_enforce(**kwargs):
    """Load the policy and apply the boot gate — the real two-step boot sequence.

    Load computes each tier's verdict; the gate judges the one that survived
    precedence.  Tests assert on the pair because that is what a host actually runs;
    asserting on ``load_security_policy`` alone would pin an intermediate state and
    miss the tier-precedence bug that split them apart.
    """
    ceiling = load_security_policy(**kwargs)
    assert_policy_signature_satisfied(ceiling)
    return ceiling


class TestPolicySigningPayload:
    def test_signature_field_is_excluded_but_issuer_is_covered(self):
        base = _policy_body(identity={"issuer": "fleet-control"})
        with_sig = _policy_body(identity={"issuer": "fleet-control", "signature": "deadbeef"})
        # Adding the signature must not change the payload (it cannot cover itself)…
        assert policy_signing_payload(base) == policy_signing_payload(with_sig)
        # …but the issuer IS inside it, so a validly-signed policy cannot be
        # re-labelled as issued by someone else.
        other = _policy_body(identity={"issuer": "attacker", "signature": "deadbeef"})
        assert policy_signing_payload(other) != policy_signing_payload(with_sig)

    def test_payload_is_stable_across_key_order_and_whitespace(self):
        a = {"version": 1, "boot": {"fail_closed": True}}
        b = {"boot": {"fail_closed": True}, "version": 1}
        assert policy_signing_payload(a) == policy_signing_payload(b)

    def test_payload_covers_unknown_forward_compatible_keys(self):
        # Signing the raw document (not a projection of the parsed ceiling) is what
        # makes a companion-registered or future scope tamper-evident on a build
        # that does not know the key.
        a = _policy_body()
        b = _policy_body(future_scope={"mode": "allow"})
        assert policy_signing_payload(a) != policy_signing_payload(b)

    def test_shares_admission_canonicalization(self):
        # One canonicalizer for both trust roots — a divergence here is exactly how
        # a signer and a verifier drift apart.
        from kiro_crew.platform.admission import canonical_signing_bytes

        body = {"version": 1, "boot": {"fail_closed": True}}
        assert policy_signing_payload(body) == canonical_signing_bytes(body)


class TestPolicySignatureStates:
    def test_verified_good_signature(self, monkeypatch, tmp_path):
        secret = "trust-key"
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": secret})
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_VERIFIED
        assert "verified" in ceiling.signature_summary()

    def test_verified_survives_reserialization(self, monkeypatch, tmp_path):
        # The signature covers the canonical form of the parsed JSON, so an
        # operator re-indenting or reordering the file does not invalidate it.
        secret = "trust-key"
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body, indent=4, sort_keys=True) + "\n")
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": secret})
        assert load_security_policy().signature_state == SIGNATURE_VERIFIED

    def test_non_ascii_signature_is_unverified_not_a_crash(self, monkeypatch, tmp_path):
        """A non-ASCII signature must be an ordinary UNVERIFIED verdict.

        ``hmac.compare_digest`` raises TypeError on a str carrying any non-ASCII
        character, and a policy file's signature is attacker- or paste-controlled
        text (a smart-quote or NBSP is enough).  A raised TypeError is NOT a
        denial: it escapes ``load_security_policy`` as a plain exception, the boot
        handler does not treat it as fatal (it only re-raises
        ``PlatformCompositionError``), and the later ``safe_context_call``
        degrades to the no-ceiling fallback — so a tampered policy would yield an
        UNGOVERNED host even with ``require_policy_signature`` on, inverting the
        flag.  Both sides are encoded before comparison.
        """
        for signature in ("abc\u2013def", "abc\u00a0def", "\u00e9" * 64):
            body = _policy_body(identity={"issuer": "fleet-control", "signature": signature})
            p = tmp_path / "policy.json"
            p.write_text(json.dumps(body))
            monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
            _patch_trust(monkeypatch, require=False, keys={"fleet-control": "trust-key"})
            ceiling = load_security_policy()
            assert ceiling is not None
            assert ceiling.signature_state == SIGNATURE_UNVERIFIED

    def test_a_lone_surrogate_signature_is_unverified_not_a_crash(self):
        """``json.loads`` accepts ``"\\udc80"``; a strict ``encode`` would raise.

        A UnicodeEncodeError is a ValueError, not a PlatformCompositionError, so it
        would escape the loader and the host would degrade to ungoverned. The tier
        ladder verifies a user-owned home file beneath the central document on every
        load, so this is one byte in that file removing the fleet ceiling. It must
        classify like every other malformed signature instead.
        """
        doc = json.loads(
            '{"version": 1, "boot": {"fail_closed": true}, '
            '"identity": {"issuer": "corp", "signature": "\\udc80"}}'
        )
        state, _detail = governance._policy_signature_state(doc, {"corp": "k"})
        assert state == SIGNATURE_UNVERIFIED

    def test_a_lone_surrogate_trust_key_is_unverified_not_a_crash(self):
        """The other text the compare depends on: the key from the JSON trust root.

        ``hmac_signature`` encodes the key before hashing; a strict encode raised on a
        lone surrogate there, one call before the compare this class fixes.
        """
        doc = json.loads(
            '{"version": 1, "boot": {"fail_closed": true}, '
            '"identity": {"issuer": "corp", "signature": "abcd"}}'
        )
        trust_keys = json.loads('{"corp": "\\udc80"}')
        assert len(trust_keys["corp"]) == 1  # a real lone surrogate, not the 6-char escape
        state, _detail = governance._policy_signature_state(doc, trust_keys)
        assert state == SIGNATURE_UNVERIFIED

    def test_non_ascii_signature_fails_closed_when_required(self, monkeypatch, tmp_path):
        """...and with the opt-in ON it must ABORT, not degrade to ungoverned."""
        body = _policy_body(identity={"issuer": "fleet-control", "signature": "tamper\u2013ed"})
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={"fleet-control": "trust-key"})
        with pytest.raises(PlatformCompositionError):
            _load_and_enforce()

    def test_verified_bad_signature_is_unverified(self, monkeypatch, tmp_path):
        body = _policy_body(identity={"issuer": "fleet-control", "signature": "not-the-mac"})
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": "trust-key"})
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_UNVERIFIED

    def test_tampered_payload_invalidates_a_good_signature(self, monkeypatch, tmp_path):
        # The core threat: an attacker edits a governed scope to WIDEN the ceiling
        # but cannot re-sign it.
        secret = "trust-key"
        body = _sign_policy(
            _policy_body(
                identity={"issuer": "fleet-control"},
                commands={"mode": "deny", "deny": ["git push*"]},
            ),
            secret,
        )
        body["commands"] = {"mode": "deny", "deny": []}  # ceiling widened in place
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": secret})
        assert load_security_policy().signature_state == SIGNATURE_UNVERIFIED

    def test_signature_without_issuer_is_unverified(self, monkeypatch, tmp_path):
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body(identity={"signature": "abc"})))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": "k"})
        assert load_security_policy().signature_state == SIGNATURE_UNVERIFIED

    def test_no_trust_key_for_issuer_is_unverified(self, monkeypatch, tmp_path):
        body = _sign_policy(_policy_body(identity={"issuer": "unknown-issuer"}), "k")
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": "k"})
        assert load_security_policy().signature_state == SIGNATURE_UNVERIFIED

    def test_wrong_trust_key_is_unverified(self, monkeypatch, tmp_path):
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), "real-key")
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": "other-key"})
        assert load_security_policy().signature_state == SIGNATURE_UNVERIFIED


class TestPolicySignatureOptIn:
    """The load-bearing design decision: verification is opt-in and advisory."""

    def test_unsigned_with_require_off_still_loads_and_governs(self, monkeypatch, tmp_path):
        # Backward-compatibility guarantee: every existing policy file keeps
        # working unchanged, with no signature and no trust key provisioned.
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body(commands={"mode": "deny", "deny": ["git push*"]})))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={})
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_UNSIGNED
        # …and the ceiling is still ENFORCED, not degraded to ungoverned.
        assert not resolve(ceiling, None, "commands", "git push origin").permitted

    def test_unverified_with_require_off_still_loads(self, monkeypatch, tmp_path):
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body(identity={"issuer": "x", "signature": "bogus"})))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={})
        assert load_security_policy() is not None

    def test_unsigned_with_require_on_fails_closed(self, monkeypatch, tmp_path):
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body()))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={"fleet-control": "k"})
        with pytest.raises(PlatformCompositionError):
            _load_and_enforce()

    def test_tampered_with_require_on_fails_closed(self, monkeypatch, tmp_path):
        secret = "trust-key"
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        body["version"] = 1
        body["boot"] = {"fail_closed": False}  # tampered after signing
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={"fleet-control": secret})
        with pytest.raises(PlatformCompositionError):
            _load_and_enforce()

    def test_verified_with_require_on_boots(self, monkeypatch, tmp_path):
        secret = "trust-key"
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=True, keys={"fleet-control": secret})
        assert load_security_policy().signature_state == SIGNATURE_VERIFIED

    def test_require_on_marks_governance_health_incident(self, monkeypatch, tmp_path):
        from kiro_crew.platform import governance_health

        governance_health.reset()
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body()))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={})
        with pytest.raises(PlatformCompositionError):
            _load_and_enforce()
        inc = governance_health.last_incident()
        assert inc is not None and inc["kind"] == "failed_closed"
        governance_health.reset()

    def test_home_tier_is_verified_too(self, monkeypatch, tmp_path):
        secret = "trust-key"
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        home = tmp_path / "security_policy.json"
        home.write_text(
            json.dumps(_sign_policy(_policy_body(identity={"issuer": "operator"}), secret))
        )
        monkeypatch.setattr("kiro_crew.platform.governance._policy_home_path", lambda: home)
        _patch_trust(monkeypatch, require=False, keys={"operator": secret})
        assert load_security_policy().signature_state == SIGNATURE_VERIFIED

    def test_home_tier_unsigned_with_require_on_fails_closed(self, monkeypatch, tmp_path):
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        home = tmp_path / "security_policy.json"
        home.write_text(json.dumps(_policy_body()))
        monkeypatch.setattr("kiro_crew.platform.governance._policy_home_path", lambda: home)
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={})
        with pytest.raises(PlatformCompositionError):
            _load_and_enforce()

    def test_signed_bundle_outranks_an_unsigned_home_file(self, monkeypatch, tmp_path):
        # GPT finding: enforcing at LOAD time raised on whichever tier a given pass
        # happened to reach. The core's loader-less pass falls through to the HOME
        # file, so an enterprise host with an unsigned home file and a correctly
        # signed companion BUNDLE aborted — even though the bundle outranks home and
        # is what the final ceiling comes from. Enforcement moved to the composed
        # result so precedence decides which verdict is judged.
        secret = "trust-key"
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        home = tmp_path / "security_policy.json"
        home.write_text(json.dumps(_policy_body()))  # unsigned, lower precedence
        monkeypatch.setattr("kiro_crew.platform.governance._policy_home_path", lambda: home)
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={"fleet-control": secret})
        # The core's loader-less pass must not abort on the lower-precedence tier…
        assert load_security_policy() is not None
        # …and the edition's signed bundle is what gets judged, so boot proceeds.
        bundle = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        assert _load_and_enforce(bundled_loader=lambda: bundle).signature_state == (
            SIGNATURE_VERIFIED
        )

    def test_bundled_tier_is_advisory_when_require_off(self, monkeypatch, tmp_path):
        # With require OFF (a downstream edition ships no require), an unsigned
        # bundled policy still loads — advisory, exactly like the file tiers.
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        monkeypatch.setattr(
            "kiro_crew.platform.governance._policy_home_path", lambda: tmp_path / "nope.json"
        )
        _patch_trust(monkeypatch, require=False, keys={})
        ceiling = load_security_policy(bundled_loader=lambda: _policy_body())
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_UNSIGNED

    def test_bundled_tier_is_NOT_exempt_when_required(self, monkeypatch, tmp_path):
        # GPT-review finding: the plugin-admission manifest signature covers only
        # name/publisher/version/capabilities, NOT the packaged
        # security_policy.json bytes, so "covered by admission" never protected
        # the resource — a tampered bundled policy loaded unchecked. Under
        # require_policy_signature the bundled tier must verify like any other.
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        monkeypatch.setattr(
            "kiro_crew.platform.governance._policy_home_path", lambda: tmp_path / "nope.json"
        )
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={"fleet-control": "trust-key"})
        # An unsigned bundled policy under require must abort, not load.
        with pytest.raises(PlatformCompositionError):
            _load_and_enforce(
                bundled_loader=lambda: _policy_body(identity={"issuer": "fleet-control"})
            )
        # A correctly-signed bundled policy verifies and loads.
        signed = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), "trust-key")
        ceiling = load_security_policy(bundled_loader=lambda: signed)
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_VERIFIED

    def test_no_policy_stays_none_when_require_off(self, monkeypatch, tmp_path):
        # require OFF: an ungoverned standalone host stays ungoverned.
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        monkeypatch.setattr(
            "kiro_crew.platform.governance._policy_home_path", lambda: tmp_path / "nope.json"
        )
        _patch_trust(monkeypatch, require=False, keys={})
        assert load_security_policy() is None

    def test_loader_never_raises_on_absence_whatever_the_optin(self, monkeypatch, tmp_path):
        # The loader is not the authority on absence: it runs once per composition
        # pass (core without a bundled_loader, edition with one) and cannot tell
        # whether it is the last word. Raising inside it either aborts a bundle-only
        # enterprise host before its edition is consulted, or — keyed on the loader
        # being present — misses a standalone host entirely (the GPT finding). So
        # absence always yields None here; the refusal lives in the boot gate.
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        monkeypatch.setattr(
            "kiro_crew.platform.governance._policy_home_path", lambda: tmp_path / "nope.json"
        )
        adm = tmp_path / "admission_policy.json"
        adm.write_text(
            json.dumps(
                {"require_policy_signature": True, "trust_keys": {"fleet-control": "trust-key"}}
            )
        )
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))
        assert load_security_policy() is None  # core's loader-less pass
        assert load_security_policy(bundled_loader=lambda: None) is None  # edition's pass


class TestPolicySignatureAbsenceGate:
    """``assert_policy_signature_satisfied`` — absence must not satisfy the mandate."""

    def test_no_policy_fails_closed_when_genuinely_required(self, monkeypatch, tmp_path):
        # With require ON and NO policy at any tier, handing back an ungoverned host
        # bypasses the requirement precisely when it matters (a mandated-signature
        # fleet that lost or never shipped its policy). Boot must abort instead.
        adm = tmp_path / "admission_policy.json"
        adm.write_text(
            json.dumps(
                {"require_policy_signature": True, "trust_keys": {"fleet-control": "trust-key"}}
            )
        )
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))
        with pytest.raises(PlatformCompositionError):
            assert_policy_signature_satisfied(None)

    def test_standalone_no_loader_is_covered_too(self, monkeypatch, tmp_path):
        # GPT finding on the previous shape: gating the refusal on "a bundled_loader
        # was consulted" meant a STANDALONE host (no edition, so no loader ever) with
        # a genuine opt-in and no policy ran with no ceiling. The gate runs on the
        # composed context, so it does not depend on a loader existing at all.
        adm = tmp_path / "admission_policy.json"
        adm.write_text(json.dumps({"require_policy_signature": True}))
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        monkeypatch.setattr(
            "kiro_crew.platform.governance._policy_home_path", lambda: tmp_path / "nope.json"
        )
        ceiling = load_security_policy()  # standalone: no bundled_loader, ever
        assert ceiling is None
        with pytest.raises(PlatformCompositionError):
            assert_policy_signature_satisfied(ceiling)

    def test_verified_ceiling_satisfies_the_gate(self, monkeypatch, tmp_path):
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={"fleet-control": "trust-key"})
        signed = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), "trust-key")
        assert_policy_signature_satisfied(parse_policy(signed, signature_state=SIGNATURE_VERIFIED))

    def test_present_but_unverified_ceiling_does_NOT_satisfy_the_gate(self, monkeypatch, tmp_path):
        # Presence alone is not enough — the gate is the enforcement point for the
        # verdict too, now that load time only computes it. A tampered or unsigned
        # ceiling that survived precedence must abort here.
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={"fleet-control": "trust-key"})
        for state in (SIGNATURE_UNSIGNED, SIGNATURE_UNVERIFIED, SIGNATURE_UNCHECKED):
            with pytest.raises(PlatformCompositionError):
                assert_policy_signature_satisfied(
                    parse_policy(_policy_body(), signature_state=state)
                )

    def test_unverified_ceiling_is_fine_when_require_off(self, monkeypatch, tmp_path):
        # The compatibility contract: with the flag off an unsigned ceiling loads
        # AND governs, so the gate must not touch it.
        _real_trust_file(monkeypatch, tmp_path, require=False, keys={})
        assert_policy_signature_satisfied(
            parse_policy(_policy_body(), signature_state=SIGNATURE_UNSIGNED)
        )

    def test_require_off_is_a_noop(self, monkeypatch, tmp_path):
        # The default and what a downstream edition ships: an ungoverned standalone
        # host stays ungoverned rather than being refused boot.
        adm = tmp_path / "admission_policy.json"
        adm.write_text(json.dumps({"mode": "open"}))
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))
        assert_policy_signature_satisfied(None)

    @pytest.mark.parametrize(
        "shape", ['{ "mode": "open",  <-- typo', "[]", "null", '"a string"', "123"]
    )
    def test_a_broken_trust_root_reads_as_no_optin_by_design(self, monkeypatch, tmp_path, shape):
        """A corrupt/malformed admission file does NOT fail closed. Deliberate.

        An attacker who can write this file is outside the policy-signature threat
        model — they would set the flag to ``false``, which parses fine — so
        fail-closing on a *malformed* file catches only a clumsy version of an attack
        the design concedes, while turning a non-atomic fleet push or a hand-edit
        typo into an unbootable host. Corruption here is a reliability event: logged,
        predictable, reported by ``kirocrew doctor``.
        """
        bad = tmp_path / "admission_policy.json"
        bad.write_text(shape)
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(bad))
        assert_policy_signature_satisfied(None)
        assert_policy_signature_satisfied(
            parse_policy(_policy_body(), signature_state=SIGNATURE_UNSIGNED)
        )

    @pytest.mark.parametrize("junk", ["false", None, 0, 1, ""])
    def test_junk_flag_in_wellformed_trust_root_fails_closed(self, monkeypatch, tmp_path, junk):
        # A well-formed trust root whose flag is PRESENT but not a boolean is a
        # different case from a broken file: the operator wrote the key down, so
        # it reads fail-closed as opted-in (via admission._coerce_flag) and an
        # unsigned policy is refused. Locks the enforcement reader to the same
        # strict read as the key store.
        adm = tmp_path / "admission_policy.json"
        adm.write_text(json.dumps({"require_policy_signature": junk}))
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))
        with pytest.raises(PlatformCompositionError):
            assert_policy_signature_satisfied(
                parse_policy(_policy_body(), signature_state=SIGNATURE_UNSIGNED)
            )

    def test_absent_admission_file_is_a_noop(self, monkeypatch, tmp_path):
        # No trust root: nobody opted in, so an unsigned policy still loads and
        # governs (the compatibility contract) and no policy stays ungoverned.
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(tmp_path / "missing.json"))
        assert_policy_signature_satisfied(None)
        assert_policy_signature_satisfied(
            parse_policy(_policy_body(), signature_state=SIGNATURE_UNSIGNED)
        )

    def test_absent_admission_policy_keeps_verification_advisory(self, monkeypatch, tmp_path):
        # An admission-policy problem is handled loudly in admission's OWN domain;
        # it must not additionally make the security ceiling unloadable here.
        monkeypatch.delenv("KIROCREW_ADMISSION_POLICY", raising=False)
        monkeypatch.setattr(
            "kiro_crew.platform.admission._policy_default_path",
            lambda: tmp_path / "no-admission.json",
        )
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body()))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_UNSIGNED

    def test_raising_trust_root_keeps_verification_advisory(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "kiro_crew.platform.admission.read_policy_trust_root",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body()))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        assert load_security_policy().signature_state == SIGNATURE_UNSIGNED

    def test_trust_settings_read_from_admission_policy_file(self, monkeypatch, tmp_path):
        # One key store: the flag + keys come from the admission policy file, not a
        # second bespoke file, and NOT from the security policy itself.
        adm = tmp_path / "admission_policy.json"
        adm.write_text(
            json.dumps({"require_policy_signature": True, "trust_keys": {"fleet-control": "k"}})
        )
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))
        from kiro_crew.platform.governance import _policy_trust_settings

        require, keys, public_keys = _policy_trust_settings()
        assert require is True
        assert keys == {"fleet-control": "k"}
        assert public_keys == {}

    def test_security_policy_cannot_self_declare_the_requirement(self, monkeypatch, tmp_path):
        # A require_policy_signature key inside security_policy.json is NOT a
        # governed scope, so it fails closed as an unknown key rather than being
        # honored — a document must not be the authority on its own authenticity.
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body(require_policy_signature=True)))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={})
        with pytest.raises(PlatformCompositionError):
            load_security_policy()

    def test_loading_does_not_disturb_admission_health_signal(self, monkeypatch, tmp_path):
        # The trust-root read must NOT run the audited admission loader: that
        # records posture + a critical SEL, and gatewayd re-loads the security
        # policy per app call.
        from kiro_crew.platform import governance_health

        governance_health.reset()
        monkeypatch.delenv("KIROCREW_ADMISSION_POLICY", raising=False)
        monkeypatch.setattr(
            "kiro_crew.platform.admission._policy_default_path",
            lambda: tmp_path / "no-admission.json",
        )
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body()))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        load_security_policy()
        assert governance_health.governance_status() == "unknown"
        assert governance_health.last_incident() is None
        governance_health.reset()


class TestParsePolicySignatureState:
    def test_direct_parse_is_unchecked_not_unsigned(self):
        # parse_policy has the document but no trust root; claiming "unsigned"
        # would conflate "we looked" with "nobody looked".
        ceiling = parse_policy(_policy_body())
        assert ceiling.signature_state == SIGNATURE_UNCHECKED
        assert "not checked" in ceiling.signature_summary()

    def test_identity_fields_still_parsed(self):
        ceiling = parse_policy(_policy_body(identity={"issuer": "fleet-control", "signature": "s"}))
        assert ceiling.identity_issuer == "fleet-control"
        assert ceiling.identity_signature == "s"


class TestPolicyShowReporting:
    """`kirocrew policy show` must distinguish the three provenance states."""

    def _show(self, capsys, ceiling):
        import argparse
        from unittest.mock import patch

        from kiro_crew import cli_commands

        args = argparse.Namespace(policy_action="show")
        # _policy imports current_context lazily from platform.context, so patch
        # it at the definition site.
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=type("Ctx", (), {"governance": ceiling})(),
        ):
            cli_commands._policy(args)
        return capsys.readouterr().out

    def _ceiling(self, state, issuer="fleet-control", signature="sig"):
        return GovernanceCeiling(
            version=1,
            boot=parse_policy(_policy_body()).boot,
            controls={},
            identity_issuer=issuer,
            identity_signature=signature,
            signature_state=state,
        )

    def test_show_reports_verified(self, capsys):
        out = self._show(capsys, self._ceiling(SIGNATURE_VERIFIED))
        assert "signed and verified" in out
        assert "fleet-control" in out

    def test_show_reports_signed_but_unverified(self, capsys):
        out = self._show(capsys, self._ceiling(SIGNATURE_UNVERIFIED))
        assert "UNVERIFIED" in out
        # An unproven issuer must NOT be presented as an established fact.
        assert "signed and verified" not in out

    def test_show_reports_unsigned(self, capsys):
        out = self._show(capsys, self._ceiling(SIGNATURE_UNSIGNED, issuer="", signature=""))
        assert "unsigned" in out
        assert "verified" not in out

    def test_show_no_policy_unchanged(self, capsys):
        out = self._show(capsys, None)
        assert "No enterprise security policy is active" in out
