"""Tests for plugin admission control (kiro_crew.platform.admission)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from kiro_crew.platform import discovery as discovery_mod
from kiro_crew.platform.admission import (
    MODE_ENFORCE,
    MODE_OPEN,
    AdmissionPolicy,
    PluginManifest,
    evaluate_admission,
)
from kiro_crew.platform.discovery import PluginAdmissionError, discover_companion_context


class _FakeEntryPoint:
    """Stands in for an importlib.metadata.EntryPoint without a real dist.

    A captured manifest is returned by monkeypatching ``_read_plugin_manifest``;
    ``load`` returns a builder that yields a sentinel context.
    """

    def __init__(self, name="amazon", value="m:build", loaded=None):
        self.name = name
        self.value = value
        self.group = "kirocrew.plugins"
        self._loaded = loaded

    def load(self):
        return self._loaded


def _signed(manifest: PluginManifest, secret: str) -> PluginManifest:
    sig = hmac.new(secret.encode(), manifest.signing_payload(), hashlib.sha256).hexdigest()
    return PluginManifest(
        name=manifest.name,
        publisher=manifest.publisher,
        version=manifest.version,
        capabilities=manifest.capabilities,
        signature=sig,
    )


@pytest.fixture
def patch_manifest(monkeypatch):
    """Helper to set the manifest evaluate_admission will read for an entry point."""

    def _set(manifest):
        monkeypatch.setattr(
            "kiro_crew.platform.admission._read_plugin_manifest",
            lambda ep: manifest,
        )

    return _set


class TestOpenPolicy:
    def test_open_admits_unsigned_plugin(self, patch_manifest):
        patch_manifest(None)  # no manifest needed in open mode
        ep = _FakeEntryPoint()
        decision = evaluate_admission(ep, AdmissionPolicy.open_default())
        assert decision.allowed
        assert "open" in decision.reason

    def test_open_still_honors_ban(self, patch_manifest):
        patch_manifest(None)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(mode=MODE_OPEN, banned=["amazon"])
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "banned" in decision.reason


class TestKillSwitch:
    def test_ban_wins_over_everything(self, patch_manifest):
        # Even a fully-signed, allowlisted plugin is rejected if banned.
        secret = "k"
        m = _signed(
            PluginManifest(name="amazon", publisher="p13n", version="1"),
            secret,
        )
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(
            mode=MODE_ENFORCE,
            require_signature=True,
            trust_keys={"p13n": secret},
            approved=["amazon"],
            banned=["amazon"],
        )
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "kill-switch" in decision.reason

    def test_ban_is_case_and_whitespace_insensitive(self, patch_manifest):
        # A ban must not be evadable by a name-case or trailing-whitespace
        # mismatch between the policy and the manifest/entry-point name.
        patch_manifest(PluginManifest(name="Amazon-Evil", publisher="x", version="1"))
        ep = _FakeEntryPoint(name="Amazon-Evil")
        policy = AdmissionPolicy(mode=MODE_OPEN, banned=["amazon-evil "])
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "banned" in decision.reason

    def test_ban_is_unicode_canonical_insensitive(self, patch_manifest):
        # A ban on the NFC form of a name must not be evadable by publishing under
        # the NFD-decomposed form (visually identical, different code points). A
        # publisher controls its package's Unicode form, so the kill-switch must
        # NFKC-canonicalize both sides before comparing. (CR-284272012 Heimdall.)
        banned_nfc = "café-app"  # 'é' = U+00E9 (composed)
        plugin_nfd = "café-app"  # 'e' + U+0301 combining acute (decomposed)
        assert banned_nfc != plugin_nfd  # genuinely different code-point strings
        patch_manifest(PluginManifest(name=plugin_nfd, publisher="x", version="1"))
        ep = _FakeEntryPoint(name=plugin_nfd)
        policy = AdmissionPolicy(mode=MODE_OPEN, banned=[banned_nfc])
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed, "NFD-decomposed name must not evade an NFC-form ban"
        assert "banned" in decision.reason


class TestManifestParsing:
    def test_string_capability_value_is_not_exploded(self):
        # A capability value given as a string (not a list) must become a
        # single-element list, NOT be exploded into per-character entries by
        # ``list(v)`` — which would corrupt both the ceiling check and the
        # signed payload.
        m = PluginManifest.from_dict(
            {"name": "p", "capabilities": {"egress": "*.amazon.com"}}
        )
        assert m.capabilities["egress"] == ["*.amazon.com"]

    def test_non_list_non_str_capability_value_drops_to_empty(self):
        m = PluginManifest.from_dict({"name": "p", "capabilities": {"egress": 42}})
        assert m.capabilities["egress"] == []

    def test_policy_string_capability_ceiling_not_exploded(self):
        p = AdmissionPolicy.from_dict(
            {"mode": "enforce", "capability_ceiling": {"egress": "*.amazon.com"}}
        )
        assert p.capability_ceiling["egress"] == ["*.amazon.com"]


class TestAllowlist:
    def test_not_on_allowlist_rejected(self, patch_manifest):
        m = PluginManifest(name="rogue", publisher="x", version="1")
        patch_manifest(m)
        ep = _FakeEntryPoint(name="rogue")
        policy = AdmissionPolicy(mode=MODE_ENFORCE, approved=["amazon"])
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "allowlist" in decision.reason

    def test_on_allowlist_admitted(self, patch_manifest):
        m = PluginManifest(name="amazon", publisher="p13n", version="1")
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(mode=MODE_ENFORCE, approved=["amazon"])
        decision = evaluate_admission(ep, policy)
        assert decision.allowed

    def test_spoofed_manifest_name_rejected_when_ep_not_on_allowlist(self, patch_manifest):
        """A malicious package sets manifest.name to an approved value but its
        real entry-point identity is not on the allowlist.  Must be rejected."""
        m = PluginManifest(name="amazon", publisher="evil-corp", version="1")
        patch_manifest(m)
        # ep.name is the REAL distribution identity -- not on the allowlist
        ep = _FakeEntryPoint(name="evil-backdoor")
        policy = AdmissionPolicy(mode=MODE_ENFORCE, approved=["amazon"])
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "allowlist" in decision.reason

    def test_ep_on_allowlist_but_manifest_spoofed_rejected(self, patch_manifest):
        """Both identities must be on the allowlist -- a mismatch is suspicious."""
        m = PluginManifest(name="not-approved", publisher="x", version="1")
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")  # ep IS approved
        policy = AdmissionPolicy(mode=MODE_ENFORCE, approved=["amazon"])
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "allowlist" in decision.reason


class TestSignature:
    def test_valid_signature_admitted(self, patch_manifest):
        secret = "s3cret"
        m = _signed(PluginManifest(name="amazon", publisher="p13n", version="1"), secret)
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(
            mode=MODE_ENFORCE, require_signature=True, trust_keys={"p13n": secret}
        )
        assert evaluate_admission(ep, policy).allowed

    def test_unsigned_rejected_when_signature_required(self, patch_manifest):
        m = PluginManifest(name="amazon", publisher="p13n", version="1")  # no sig
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(
            mode=MODE_ENFORCE, require_signature=True, trust_keys={"p13n": "s3cret"}
        )
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "signature" in decision.reason

    def test_tampered_capabilities_invalidate_signature(self, patch_manifest):
        secret = "s3cret"
        signed = _signed(
            PluginManifest(
                name="amazon", publisher="p13n", version="1", capabilities={"egress": ["a"]}
            ),
            secret,
        )
        # attacker swaps capabilities but keeps the old signature
        tampered = PluginManifest(
            name="amazon",
            publisher="p13n",
            version="1",
            capabilities={"egress": ["evil.example"]},
            signature=signed.signature,
        )
        patch_manifest(tampered)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(
            mode=MODE_ENFORCE, require_signature=True, trust_keys={"p13n": secret}
        )
        assert not evaluate_admission(ep, policy).allowed


class TestCapabilityCeiling:
    def test_capability_over_ceiling_rejected(self, patch_manifest):
        m = PluginManifest(
            name="amazon", publisher="p13n", version="1", capabilities={"egress": ["*.evil.com"]}
        )
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(mode=MODE_ENFORCE, capability_ceiling={"egress": ["*.amazon.com"]})
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "egress" in decision.reason

    def test_capability_within_ceiling_admitted(self, patch_manifest):
        m = PluginManifest(
            name="amazon", publisher="p13n", version="1", capabilities={"egress": ["*.amazon.com"]}
        )
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(mode=MODE_ENFORCE, capability_ceiling={"egress": ["*.amazon.com"]})
        assert evaluate_admission(ep, policy).allowed

    def test_capability_glob_ceiling_admits_concrete_value(self, patch_manifest):
        # A concrete host must be admitted when it matches a glob ceiling entry
        # (e.g. "api.amazon.com" under "*.amazon.com") — the ceiling uses
        # fnmatch semantics, matching the documented policy shape.
        m = PluginManifest(
            name="amazon", publisher="p13n", version="1", capabilities={"egress": ["api.amazon.com"]}
        )
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(mode=MODE_ENFORCE, capability_ceiling={"egress": ["*.amazon.com"]})
        assert evaluate_admission(ep, policy).allowed

    def test_unceilinged_capability_category_rejected(self, patch_manifest):
        m = PluginManifest(
            name="amazon", publisher="p13n", version="1", capabilities={"paths": ["~/.ssh"]}
        )
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(mode=MODE_ENFORCE, capability_ceiling={"egress": ["*"]})
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "paths" in decision.reason

    def test_open_mode_still_enforces_capability_ceiling(self, patch_manifest):
        # A ceiling configured under an OPEN policy (no allowlist, no signature)
        # must still be enforced — the open-mode fast path must not bypass it.
        m = PluginManifest(
            name="amazon", publisher="p13n", version="1", capabilities={"egress": ["*.evil.com"]}
        )
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(mode=MODE_OPEN, capability_ceiling={"egress": ["*.amazon.com"]})
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "egress" in decision.reason

    def test_open_mode_no_ceiling_still_fast_path_admits(self, patch_manifest):
        # Truly-open policy (no ceiling, no allowlist, no signature) still admits
        # without requiring a manifest.
        patch_manifest(None)
        ep = _FakeEntryPoint(name="amazon")
        decision = evaluate_admission(ep, AdmissionPolicy(mode=MODE_OPEN))
        assert decision.allowed


class TestEnforceRequiresManifest:
    def test_enforce_rejects_plugin_without_manifest(self, patch_manifest):
        patch_manifest(None)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(mode=MODE_ENFORCE, approved=["amazon"])
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "manifest" in decision.reason


class TestPolicyLoading:
    def test_no_policy_fails_closed(self, monkeypatch, tmp_path):
        """AVP-23427: an absent policy file must fail closed, not admit-all."""
        monkeypatch.delenv("KIROCREW_ADMISSION_POLICY", raising=False)
        monkeypatch.setattr(
            "kiro_crew.platform.admission._POLICY_DEFAULT_PATH", tmp_path / "nope.json"
        )
        from kiro_crew.platform.admission import load_admission_policy

        policy = load_admission_policy()
        # fail-closed: enforce + signature + empty allowlist (admits nothing).
        assert policy.mode == MODE_ENFORCE
        assert policy.require_signature
        assert policy.approved == []

    def test_unreadable_policy_fails_closed(self, monkeypatch, tmp_path):
        bad = tmp_path / "admission_policy.json"
        bad.write_text("{ not valid json")
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(bad))
        from kiro_crew.platform.admission import load_admission_policy

        policy = load_admission_policy()
        # fail-closed: enforce + signature + empty allowlist (admits nothing)
        assert policy.mode == MODE_ENFORCE
        assert policy.require_signature
        assert policy.approved == []

    def test_seed_then_load_is_open(self, monkeypatch, tmp_path):
        """The first-run seed writes a permissive file so a fresh install stays open."""
        import kiro_crew.platform.admission as adm

        monkeypatch.delenv("KIROCREW_ADMISSION_POLICY", raising=False)
        monkeypatch.setattr(adm, "_POLICY_DEFAULT_PATH", tmp_path / "admission_policy.json")
        monkeypatch.setattr(adm, "_SEED_MARKER", tmp_path / ".migrations" / "seeded")
        monkeypatch.setattr(adm, "_CHECKSUM_PATH", tmp_path / ".migrations" / "policy.sha256")

        assert adm.seed_default_policy() is True
        policy = adm.load_admission_policy()
        assert policy.mode == MODE_OPEN
        assert policy.approved is None

    def test_deletion_after_seed_fails_closed_no_reseed(self, monkeypatch, tmp_path):
        """AVP-23427: deleting the seeded file must NOT re-seed; load fails closed."""
        import kiro_crew.platform.admission as adm

        monkeypatch.delenv("KIROCREW_ADMISSION_POLICY", raising=False)
        pol = tmp_path / "admission_policy.json"
        monkeypatch.setattr(adm, "_POLICY_DEFAULT_PATH", pol)
        monkeypatch.setattr(adm, "_SEED_MARKER", tmp_path / ".migrations" / "seeded")
        monkeypatch.setattr(adm, "_CHECKSUM_PATH", tmp_path / ".migrations" / "policy.sha256")

        assert adm.seed_default_policy() is True
        pol.unlink()  # attacker/accident deletes the file
        # Marker still present → no silent re-seed.
        assert adm.seed_default_policy() is False
        assert not pol.exists()
        policy = adm.load_admission_policy()
        assert policy.mode == MODE_ENFORCE and policy.approved == []

    def test_integrity_mismatch_is_advisory_not_deny(self, monkeypatch, tmp_path, caplog):
        """A modified seeded policy is still honored (user-owned) and detected,
        but a legitimate edit must NOT force the dashboard to 'degraded'."""
        import logging as _logging

        import kiro_crew.platform.admission as adm
        from kiro_crew.platform import governance_health as gh

        monkeypatch.delenv("KIROCREW_ADMISSION_POLICY", raising=False)
        pol = tmp_path / "admission_policy.json"
        monkeypatch.setattr(adm, "_POLICY_DEFAULT_PATH", pol)
        monkeypatch.setattr(adm, "_SEED_MARKER", tmp_path / ".migrations" / "seeded")
        monkeypatch.setattr(adm, "_CHECKSUM_PATH", tmp_path / ".migrations" / "policy.sha256")

        adm.seed_default_policy()
        body = json.loads(pol.read_text())
        body["banned"] = ["rogue-plugin"]  # legitimate operator edit
        pol.write_text(json.dumps(body))
        gh.reset()
        with caplog.at_level(_logging.ERROR):
            policy = adm.load_admission_policy()
        # Operator's edit is honored (NOT hard-denied)...
        assert policy.banned == ["rogue-plugin"]
        # ...the change IS detected (logged for the audit trail)...
        assert any("seed checksum" in r.getMessage() for r in caplog.records)
        # ...but a legitimate edit must NOT force the dashboard to "degraded".
        assert gh.governance_status() != "degraded"

    def test_absent_policy_reports_degraded_health(self, monkeypatch, tmp_path):
        import kiro_crew.platform.admission as adm
        from kiro_crew.platform import governance_health as gh

        monkeypatch.delenv("KIROCREW_ADMISSION_POLICY", raising=False)
        monkeypatch.setattr(adm, "_POLICY_DEFAULT_PATH", tmp_path / "nope.json")
        gh.reset()
        adm.load_admission_policy()
        assert gh.governance_status() == "degraded"

    def test_policy_round_trip(self, monkeypatch, tmp_path):
        p = tmp_path / "admission_policy.json"
        p.write_text(
            json.dumps(
                {
                    "mode": "enforce",
                    "require_signature": True,
                    "trust_keys": {"p13n": "s"},
                    "approved": ["amazon"],
                    "banned": ["rogue"],
                    "capability_ceiling": {"egress": ["*.amazon.com"]},
                }
            )
        )
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(p))
        from kiro_crew.platform.admission import load_admission_policy

        policy = load_admission_policy()
        assert policy.mode == MODE_ENFORCE
        assert policy.banned == ["rogue"]
        assert policy.approved == ["amazon"]


class TestDiscoveryGate:
    def test_rejected_plugin_aborts_discovery(self, monkeypatch):
        # A banned plugin must raise PluginAdmissionError BEFORE ep.load() runs.
        loaded_marker = {"called": False}

        def _should_not_run(_cfg):
            loaded_marker["called"] = True
            raise AssertionError("ep.load() ran for a rejected plugin")

        ep = _FakeEntryPoint(name="amazon", loaded=_should_not_run)
        monkeypatch.setattr(discovery_mod, "plugin_entry_points", lambda: [ep])
        monkeypatch.setattr(
            "kiro_crew.platform.admission._read_plugin_manifest",
            lambda e: PluginManifest(name="amazon", publisher="p13n", version="1"),
        )
        policy = AdmissionPolicy(mode=MODE_OPEN, banned=["amazon"])
        with pytest.raises(PluginAdmissionError):
            discover_companion_context("amazon", None, policy=policy)
        assert loaded_marker["called"] is False  # verify-before-run held

    def test_admitted_plugin_loads(self, monkeypatch):
        sentinel = object()
        ep = _FakeEntryPoint(name="amazon", loaded=lambda _cfg: sentinel)
        monkeypatch.setattr(discovery_mod, "plugin_entry_points", lambda: [ep])
        monkeypatch.setattr(
            "kiro_crew.platform.admission._read_plugin_manifest",
            lambda e: PluginManifest(name="amazon", publisher="p13n", version="1"),
        )
        policy = AdmissionPolicy(mode=MODE_OPEN)
        result = discover_companion_context("amazon", None, policy=policy)
        assert result is sentinel

    def test_first_boot_seeds_and_admits_companion(self, monkeypatch, tmp_path):
        """AVP-23427 ordering: discovery (which runs before the gateway seed on a
        fleet's first boot) seeds the permissive default, so the companion is
        admitted instead of fail-closing when no policy file exists yet."""
        import kiro_crew.platform.admission as adm

        monkeypatch.delenv("KIROCREW_ADMISSION_POLICY", raising=False)
        monkeypatch.setattr(adm, "_POLICY_DEFAULT_PATH", tmp_path / "admission_policy.json")
        monkeypatch.setattr(adm, "_SEED_MARKER", tmp_path / ".migrations" / "seeded")
        monkeypatch.setattr(adm, "_CHECKSUM_PATH", tmp_path / ".migrations" / "policy.sha256")

        sentinel = object()
        ep = _FakeEntryPoint(name="amazon", loaded=lambda _cfg: sentinel)
        monkeypatch.setattr(discovery_mod, "plugin_entry_points", lambda: [ep])
        # No explicit policy -> discovery must seed + load (not fail closed).
        result = discover_companion_context("amazon", None)
        assert result is sentinel
        assert (tmp_path / "admission_policy.json").exists()
