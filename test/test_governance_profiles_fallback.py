"""Phase 5 — the policy's declared loosened ``fallback`` profile.

Covers: the top-level ``fallback`` key parsed into the ceiling, an unusable
profile FILE resolved to that fallback instead of deny-all, deny-all kept when
none is declared, and the fallback picked up once governance composes.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.governance import resolve


@pytest.fixture
def profiles_dir(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    d.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", d)
    gp.reset_store()
    yield d
    gp.reset_store()


def _write(d, name, body):
    (d / f"{name}.json").write_text(json.dumps(body))


# ── Configurable loosened fallback (policy top-level ``fallback``): when a
#    profile FILE is unusable, an enterprise ceiling may substitute a looser
#    profile than deny-all — e.g. deny only channels + apps, leave the basic
#    operational planes (subagent/cron/heartbeat, tools, commands, fs, network) to
#    the ceiling. Default (no ``fallback`` declared) stays deny-all. ──


def test_policy_fallback_key_parses_into_ceiling():
    from kiro_crew.platform.context import PlatformCompositionError
    from kiro_crew.platform.governance import parse_policy

    # Absent → None: the deny-all default is preserved (public edition unchanged).
    c0 = parse_policy({"version": 1, "boot": {"fail_closed": True}})
    assert c0.fallback_profile is None

    # Present → a Profile that lists ONLY channels + apps (both denied), leaving
    # every other plane ABSENT so the ceiling alone governs it.
    c1 = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "fallback": {
                "channels": {"members": {"mode": "allow", "allow": []}},
                "apps": {"mode": "allow", "allow": []},
            },
        }
    )
    assert c1.fallback_profile is not None
    # The two named planes are governed (present) and denied…
    assert "channels" in c1.fallback_profile.controls
    assert "apps" in c1.fallback_profile.controls
    assert not resolve(None, c1.fallback_profile, "apps", "some-app").permitted
    # …every unlisted plane is ABSENT, so the profile does not narrow it (the
    # ceiling governs → permitted at the profile level).
    assert "tools" not in c1.fallback_profile.controls
    assert "capabilities.spawn" not in c1.fallback_profile.controls
    assert resolve(None, c1.fallback_profile, "tools", "read").permitted
    assert resolve(None, c1.fallback_profile, "capabilities.spawn", "researcher").permitted

    # Non-dict fallback → fail closed at boot.
    with pytest.raises(PlatformCompositionError):
        parse_policy({"version": 1, "boot": {"fail_closed": True}, "fallback": []})
    # A STRUCTURAL key inside the fallback (extends/name/bind/updates/fallback/…)
    # would be silently dropped by _parse_controls, so it must fail closed rather
    # than vanish — e.g. `fallback: {"extends": "lockdown"}` losing its intent.
    with pytest.raises(PlatformCompositionError):
        parse_policy(
            {"version": 1, "boot": {"fail_closed": True}, "fallback": {"extends": "lockdown"}}
        )
    # An unknown scope INSIDE the fallback fails closed exactly like a real
    # profile would (the fallback is parsed with the same scope validation).
    with pytest.raises(PlatformCompositionError):
        parse_policy(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "fallback": {"capabilities": {"zzz_not_a_scope": {"enabled": False}}},
            }
        )


def _install_ceiling(monkeypatch, ceiling):
    """Point governance_profiles' current_context() at a ceiling for the test."""
    import types

    from kiro_crew.platform import context as ctx_mod

    monkeypatch.setattr(
        ctx_mod, "current_context", lambda: types.SimpleNamespace(governance=ceiling)
    )


def test_unusable_profile_uses_declared_loosened_fallback(profiles_dir, monkeypatch):
    # With a ceiling that declares a loosened fallback (deny only channels+apps),
    # an unusable profile FILE falls back to THAT profile, NOT deny-all: the basic
    # operational planes stay permitted, only channels + apps are denied.
    from kiro_crew.platform.governance import parse_policy

    ceiling = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "fallback": {
                "channels": {"members": {"mode": "allow", "allow": []}},
                "apps": {"mode": "allow", "allow": []},
            },
        }
    )
    _install_ceiling(monkeypatch, ceiling)

    # Schema-invalid profile bound to the cron surface → unusable → fallback.
    _write(
        profiles_dir,
        "cron",
        {"name": "cron", "bind": {"type": "surface", "id": "cron"}, "tools": {"mode": "banana"}},
    )
    prof = gp.resolve_active_scope("cron:job-1:run-1")
    assert prof is not None, "bound surface must resolve to the loosened fallback, not None"
    # Only channels + apps are denied by the fallback…
    assert "channels" in prof.controls and "apps" in prof.controls
    assert not resolve(None, prof, "apps", "some-app").permitted
    # …the basic operational planes are left to the ceiling (NOT deny-all'd).
    assert resolve(
        None, prof, "capabilities.spawn", "researcher"
    ).permitted, "spawn must stay available under the loosened fallback"
    assert resolve(None, prof, "tools", "read").permitted


def test_unusable_profile_defaults_to_deny_all_without_declared_fallback(profiles_dir, monkeypatch):
    # No declared fallback (governance present but fallback_profile is None) →
    # deny-all is preserved: the public/default posture is unchanged.
    from kiro_crew.platform.governance import parse_policy

    _install_ceiling(monkeypatch, parse_policy({"version": 1, "boot": {"fail_closed": True}}))
    _write(
        profiles_dir,
        "cron",
        {"name": "cron", "bind": {"type": "surface", "id": "cron"}, "tools": {"mode": "banana"}},
    )
    prof = gp.resolve_active_scope("cron:job-1:run-1")
    assert prof is not None
    assert not resolve(None, prof, "tools", "read").permitted
    assert not resolve(None, prof, "capabilities.spawn", "researcher").permitted  # deny-all


def test_declared_fallback_applies_after_governance_composes(profiles_dir, monkeypatch):
    # Boot-order race guard: if the store is first touched BEFORE governance
    # composes, an unusable profile bakes deny-all. Once governance composes with a
    # declared fallback, the NEXT resolve must pick it up WITHOUT any file mtime
    # change — the fallback token folded into the freshness key forces one reload.
    # Without that token the baked deny-all would persist until a file changed,
    # silently ignoring the declared fallback (the boot-order race the fix closes).
    import types

    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform.governance import parse_policy

    _write(
        profiles_dir,
        "cron",
        {"name": "cron", "bind": {"type": "surface", "id": "cron"}, "tools": {"mode": "banana"}},
    )

    # Phase 1: governance not composed yet → deny-all baked into the snapshot.
    monkeypatch.setattr(ctx_mod, "current_context", lambda: types.SimpleNamespace(governance=None))
    prof1 = gp.resolve_active_scope("cron:job-1:run-1")
    assert prof1 is not None
    assert not resolve(None, prof1, "capabilities.spawn", "researcher").permitted

    # Phase 2: governance composes with a declared loosened fallback. NO file change.
    ceiling = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "fallback": {
                "channels": {"members": {"mode": "allow", "allow": []}},
                "apps": {"mode": "allow", "allow": []},
            },
        }
    )
    monkeypatch.setattr(
        ctx_mod, "current_context", lambda: types.SimpleNamespace(governance=ceiling)
    )
    prof2 = gp.resolve_active_scope("cron:job-1:run-1")
    assert prof2 is not None
    assert resolve(
        None, prof2, "capabilities.spawn", "researcher"
    ).permitted, "declared fallback must apply once governance composes, without a file change"
