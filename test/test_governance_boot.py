"""Phase 2 — the governance ceiling composes onto PlatformContext at boot.

Confirms: ``build_default_context`` carries the loaded ceiling on every path
(boot + lazy default); a present env policy flows through; an unreadable policy
aborts; absent → ``governance=None``; CONTRACT_VERSION is pinned at 1 pre-launch.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import CONTRACT_VERSION, PlatformCompositionError


def test_contract_version_current():
    # The carrier the companion's _assert_contract pins against. PINNED AT 1
    # pre-launch: the seam is rebuilt in lockstep, so every pre-release field/
    # interface addition (governance carrier, knowledge/dashboard/jail seams)
    # landed under v1 with no bump. Start incrementing only post-launch.
    assert CONTRACT_VERSION == 1


def test_standalone_no_policy_is_none(monkeypatch, tmp_path):
    monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
    _nope = tmp_path / "nope.json"
    monkeypatch.setattr("kiro_crew.platform.governance._policy_home_path", lambda: _nope)
    ctx = build_default_context(KiroCrewConfig.load())
    assert ctx.governance is None
    assert ctx.contract_version == CONTRACT_VERSION


def test_env_policy_composes_onto_context(monkeypatch, tmp_path):
    p = tmp_path / "policy.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "commands": {"mode": "deny", "deny": ["git push*"]},
            }
        )
    )
    monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
    ctx = build_default_context(KiroCrewConfig.load())
    assert ctx.governance is not None
    assert "commands" in ctx.governance.controls


def test_unreadable_policy_aborts_boot(monkeypatch, tmp_path):
    bad = tmp_path / "policy.json"
    bad.write_text("{ not json")
    monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(bad))
    with pytest.raises(PlatformCompositionError):
        build_default_context(KiroCrewConfig.load())


def test_looser_profile_ordinal_aborts_boot(monkeypatch, tmp_path):
    """Validation rules 3 & 7: a profile looser than the ceiling aborts boot."""
    from kiro_crew.platform import governance_profiles as gp

    # Ceiling requires sandbox >= cc; a profile that sets sandbox off is looser.
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "weak.json").write_text(
        json.dumps(
            {
                "name": "weak",
                "bind": {"type": "surface", "id": "cron"},
                "sandbox": {"min_level": "off"},
            }
        )
    )
    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    p = tmp_path / "policy.json"
    p.write_text(
        json.dumps({"version": 1, "boot": {"fail_closed": True}, "sandbox": {"min_level": "cc"}})
    )
    monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
    try:
        from kiro_crew.platform import bootstrap

        with pytest.raises(PlatformCompositionError):
            bootstrap.bootstrap_context(KiroCrewConfig.load())
    finally:
        gp.reset_store()


def test_profile_within_ceiling_boots(monkeypatch, tmp_path):
    from kiro_crew.platform import governance_profiles as gp

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "ok.json").write_text(
        json.dumps(
            {
                "name": "ok",
                "bind": {"type": "surface", "id": "cron"},
                "sandbox": {"min_level": "strict"},  # stricter than ceiling cc → fine
            }
        )
    )
    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    p = tmp_path / "policy.json"
    p.write_text(
        json.dumps({"version": 1, "boot": {"fail_closed": True}, "sandbox": {"min_level": "cc"}})
    )
    monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
    try:
        from kiro_crew.platform import bootstrap

        bootstrap.bootstrap_context(KiroCrewConfig.load())  # no raise
    finally:
        gp.reset_store()


def test_lazy_default_carries_ceiling(monkeypatch, tmp_path):
    """The lazy current_context() path (stray subprocess) also gets the ceiling."""
    from kiro_crew.platform import context as ctx_mod

    p = tmp_path / "policy.json"
    p.write_text(json.dumps({"version": 1, "boot": {"fail_closed": True}}))
    monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
    # Pin standalone: this exercises the standalone lazy-default path. With the
    # companion co-installed in the same venv (the combined-build test setup), an
    # unset profile would auto-resolve ``amazon`` via the entry-point signal and
    # fail closed — unrelated to what this test covers.
    monkeypatch.setenv("KIROCREW_PROFILE", "standalone")
    ctx_mod.reset_context()
    try:
        ctx = ctx_mod.current_context()
        assert ctx.governance is not None
    finally:
        ctx_mod.reset_context()
