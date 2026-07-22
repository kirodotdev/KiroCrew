"""Phase 5 — profile store + active-scope resolution.

Covers: per-surface / per-app / per-task binding, deny-by-default on unproven
unattended identity, schema-invalid → deny-all (not the ceiling), ``extends``
narrowing, and mtime hot-reload.
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


def test_surface_binding_resolves(profiles_dir):
    _write(
        profiles_dir,
        "cron-tight",
        {
            "name": "cron-tight",
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    prof = gp.resolve_active_scope("cron:job-7:run-1")
    assert prof is not None and prof.name == "cron-tight"


def test_app_binding_wins_over_surface(profiles_dir):
    _write(
        profiles_dir,
        "deploy",
        {
            "name": "deploy",
            "bind": {"type": "app", "id": "deploy-web"},
            "tools": {"mode": "allow", "allow": ["code"]},
        },
    )
    prof = gp.resolve_active_scope("dashboard:slot1", app="deploy-web")
    assert prof is not None and prof.name == "deploy"


def test_agent_task_binding(profiles_dir):
    _write(
        profiles_dir,
        "researcher",
        {
            "name": "researcher",
            "bind": {"type": "task", "id": "researcher"},
            "capabilities": {"spawn": {"enabled": False}},
        },
    )
    prof = gp.resolve_active_scope("subagent:abc", agent="researcher")
    assert prof is not None and prof.name == "researcher"


def test_unattended_unproven_identity_denies_all(profiles_dir):
    # No bound profile, unattended surface (_hb), unproven → deny-all.
    prof = gp.resolve_active_scope("_hb")
    assert prof is not None
    assert prof.name.startswith("_deny_all")
    # deny-all denies tools.
    assert not resolve(None, prof, "tools", "read").permitted


def test_attended_surface_no_profile_is_none(profiles_dir):
    # cli is attended; no bound profile → None (policy ceiling alone governs).
    assert gp.resolve_active_scope("cli_chat") is None


def test_proven_cron_no_profile_is_none(profiles_dir):
    # A cron job with a real session key (proven identity) and no bound profile
    # → None (policy governs); deny-all only kicks in on UNPROVEN identity.
    assert gp.resolve_active_scope("cron:job-9:run-2") is None


def test_invalid_profile_falls_back_to_deny_all(profiles_dir):
    # Schema-invalid profile (bad bind type) → deny-all sentinel, NOT ceiling.
    _write(
        profiles_dir,
        "broken",
        {"name": "broken", "bind": {"type": "galaxy"}, "tools": {"mode": "allow"}},
    )
    prof = gp.get_store_profile("broken")
    # Fallback keeps the file stem (so any bind index stays coherent) but is
    # behaviorally deny-all — NOT the permissive ceiling.
    assert prof is not None
    assert not resolve(None, prof, "tools", "read").permitted
    assert not resolve(None, prof, "capabilities.spawn", "researcher").permitted


def test_invalid_profile_with_valid_bind_still_denies_its_surface(profiles_dir):
    # A profile with a VALID bind but an INVALID control must still bind its
    # surface to deny-all (fail-closed) — NOT be dropped from the bind index and
    # fail open to policy-only.
    _write(
        profiles_dir,
        "cron",
        {
            "name": "cron",
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "banana"},  # invalid → parse_profile raises
        },
    )
    prof = gp.resolve_active_scope("cron:job-7:run-1")
    assert prof is not None, "bound surface must resolve to the deny-all fallback, not None"
    assert not resolve(None, prof, "tools", "read").permitted
    assert not resolve(None, prof, "capabilities.spawn", "researcher").permitted


def test_extends_narrows(profiles_dir):
    _write(
        profiles_dir,
        "base",
        {"name": "base", "tools": {"mode": "allow", "allow": ["read", "grep", "code"]}},
    )
    _write(
        profiles_dir,
        "child",
        {
            "name": "child",
            "extends": "base",
            "bind": {"type": "surface", "id": "dashboard"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    prof = gp.resolve_active_scope("dashboard:x")
    assert prof is not None
    assert resolve(None, prof, "tools", "read").permitted
    assert not resolve(None, prof, "tools", "grep").permitted


def test_extends_missing_parent_still_denies_its_surface(profiles_dir):
    # A profile bound to a surface whose ``extends`` parent is MISSING must
    # revert to deny-all WHILE PRESERVING its bind — so the bound surface
    # resolves to deny-all (fail-closed), not None (which would fall through to
    # the policy ceiling alone, bypassing operator narrowing). Mirrors
    # test_invalid_profile_with_valid_bind_still_denies_its_surface for the
    # Pass-2 (extends) path. (security-review blocking finding.)
    _write(
        profiles_dir,
        "cron",
        {
            "name": "cron",
            "bind": {"type": "surface", "id": "cron"},
            "extends": "does-not-exist",
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    prof = gp.resolve_active_scope("cron:job-7:run-1")
    assert prof is not None, "bound surface must resolve to deny-all, not None (fail-open)"
    assert not resolve(None, prof, "tools", "read").permitted
    assert not resolve(None, prof, "capabilities.spawn", "researcher").permitted


def test_extends_chain_still_denies_its_surface(profiles_dir):
    # A non-trivial chain (c -> b -> a, where the parent b ITSELF extends) is
    # rejected to deny-all; the bound surface must still fail CLOSED, not drop to
    # None.  Every profile in the chain allows ``read`` AND ``spawn``, so if c had
    # (wrongly) COMPOSED through the chain it would PERMIT both — asserting both
    # are DENIED distinguishes the deny-all branch from a mere empty-intersection
    # narrowing, which the prior version of this test failed to do.
    # Every member allows ``read`` (non-empty intersection) so a COMPOSE would
    # PERMIT ``tools/read``; and none narrows ``capabilities.spawn`` (default
    # true) so a COMPOSE would PERMIT spawn too — therefore asserting BOTH are
    # DENIED proves the deny-all branch, not a coincidental empty intersection
    # (the gap the prior version of this test had).
    _write(profiles_dir, "a", {"name": "a", "tools": {"mode": "allow", "allow": ["read"]}})
    _write(
        profiles_dir,
        "b",
        {"name": "b", "extends": "a", "tools": {"mode": "allow", "allow": ["read"]}},
    )
    _write(
        profiles_dir,
        "c",
        {
            "name": "c",
            "extends": "b",  # parent b itself extends -> non-trivial chain
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    prof = gp.resolve_active_scope("cron:job:run")
    assert prof is not None, "chained-extends bound surface must resolve to deny-all, not None"
    # deny-all signature: both would be ALLOWED if c had composed through the chain.
    assert not resolve(None, prof, "tools", "read").permitted
    assert not resolve(None, prof, "capabilities.spawn", "researcher").permitted


def test_extends_chain_rejection_is_order_independent(profiles_dir):
    # The compose-vs-deny-all verdict must NOT depend on the order the profile
    # files happen to sort.  Here the mid-parent ``mmm`` sorts BEFORE the child
    # ``zzz`` (so it is composed first, clearing its live ``extends``); a verdict
    # read from the live dict would then WRONGLY compose ``zzz`` (fail-open).  The
    # snapshot-of-original-extends fix must still deny-all.
    _write(profiles_dir, "bbb", {"name": "bbb", "tools": {"mode": "allow", "allow": ["read"]}})
    _write(
        profiles_dir,
        "mmm",
        {"name": "mmm", "extends": "bbb", "tools": {"mode": "allow", "allow": ["read"]}},
    )
    _write(
        profiles_dir,
        "zzz",
        {
            "name": "zzz",
            "extends": "mmm",  # mid-parent mmm itself extends -> non-trivial chain
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    prof = gp.resolve_active_scope("cron:job:run")
    assert prof is not None, "chained-extends bound surface must resolve to deny-all, not None"
    assert not resolve(
        None, prof, "tools", "read"
    ).permitted, "non-trivial chain must deny-all regardless of file sort order"


def test_hot_reload_picks_up_edit(profiles_dir):
    _write(
        profiles_dir,
        "cron-tight",
        {
            "name": "cron-tight",
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    prof = gp.resolve_active_scope("cron:j:r")
    assert resolve(None, prof, "tools", "read").permitted
    assert not resolve(None, prof, "tools", "code").permitted

    # Edit the file: widen to include code (still bounded by policy at runtime).
    import os

    path = profiles_dir / "cron-tight.json"
    _write(
        profiles_dir,
        "cron-tight",
        {
            "name": "cron-tight",
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read", "code"]},
        },
    )
    # Bump mtime explicitly so the fingerprint changes even on coarse clocks.
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))

    prof2 = gp.resolve_active_scope("cron:j:r")
    assert resolve(None, prof2, "tools", "code").permitted


def test_no_profiles_dir_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(gp, "_PROFILES_DIR", tmp_path / "does-not-exist")
    gp.reset_store()
    try:
        # Attended surface, no dir → None; unattended unproven → deny-all.
        assert gp.resolve_active_scope("cli_chat") is None
        assert gp.resolve_active_scope("_bg").name.startswith("_deny_all")
    finally:
        gp.reset_store()
