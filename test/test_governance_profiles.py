"""Phase 5 — profile store + active-scope resolution.

Covers: per-surface / per-app / per-task binding, deny-by-default on unproven
unattended identity, schema-invalid → deny-all (not the ceiling), ``extends``
narrowing, and mtime hot-reload.
"""

from __future__ import annotations

import json
import threading

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


def _make_read_text_raise(monkeypatch, target: "object", exc: Exception):
    """Monkeypatch ``Path.read_text`` to raise ``exc`` for ``target`` only.

    Portable simulation of a present-but-unreadable file (chmod 000 is a no-op on
    Windows). ``target`` is compared by resolved path so it matches regardless of
    how the store constructs the Path.
    """
    from pathlib import Path

    real_read_text = Path.read_text
    target_str = str(target)

    def _patched(self, *args, **kwargs):
        if str(self) == target_str:
            raise exc
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _patched)


def _write_host_profile(path):
    path.write_text(
        json.dumps(
            {
                "name": "host",
                "bind": {"type": "surface", "id": "host"},
                "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
            }
        )
    )


def _make_ceiling():
    from kiro_crew.platform.governance import parse_policy

    return parse_policy({"version": 1, "boot": {"fail_closed": True}})


def test_unreadable_profile_governed_fleet_boot_aborts(profiles_dir, monkeypatch):
    # F1-1 (corrected): a PRESENT-but-UNREADABLE profile whose bind cannot be
    # recovered must NOT be guessed by filename (the stem does not reliably encode
    # the bind). For a GOVERNED fleet (ceiling present) it fails CLOSED to a
    # boot-abort: assert_profiles_within_ceiling raises PlatformCompositionError
    # rather than run with a silently-dropped restrictive profile.
    from kiro_crew.platform.context import PlatformCompositionError
    from kiro_crew.platform.governance_profiles import assert_profiles_within_ceiling

    path = profiles_dir / "host.json"
    _write_host_profile(path)
    _make_read_text_raise(monkeypatch, path, OSError("permission denied"))
    gp.reset_store()

    with pytest.raises(PlatformCompositionError):
        assert_profiles_within_ceiling(_make_ceiling())


def test_unreadable_profile_standalone_is_lenient(profiles_dir, monkeypatch):
    # F1-1 (corrected): a standalone/ungoverned host (no ceiling) must NOT crash
    # on an unreadable profile blip. assert_profiles_within_ceiling(None) is a
    # no-op, and the unbound deny-all fallback simply drops out — the surface
    # falls to policy-only (matches pre-split standalone behavior, no regression).
    from kiro_crew.platform.governance_profiles import (
        HOST_SESSION_KEY,
        assert_profiles_within_ceiling,
    )

    path = profiles_dir / "host.json"
    _write_host_profile(path)
    _make_read_text_raise(monkeypatch, path, OSError("permission denied"))
    gp.reset_store()

    assert_profiles_within_ceiling(None)  # no ceiling → no crash
    # Unbound deny-all drops from the bind index → host surface is policy-only.
    assert gp.resolve_active_scope(HOST_SESSION_KEY) is None


def test_invalid_utf8_profile_governed_fleet_boot_aborts(profiles_dir, monkeypatch):
    # F2-1 (UTF-8): an invalid-encoding file raises UnicodeDecodeError (base
    # UnicodeError), which is NOT an OSError. The read guard must catch
    # (OSError, UnicodeError) so it does not escape both handlers and crash boot
    # uncaught. Treated as present-but-unreadable → governed fleet boot-aborts.
    from kiro_crew.platform.context import PlatformCompositionError
    from kiro_crew.platform.governance_profiles import assert_profiles_within_ceiling

    path = profiles_dir / "host.json"
    # Write raw invalid UTF-8 bytes (0xff is never valid UTF-8).
    path.write_bytes(b'{"name": "host", "bind": {"type": "surface", "id": "\xff\xfe"}}')
    gp.reset_store()

    # Must not escape uncaught; a governed fleet boot-aborts fail-closed.
    with pytest.raises(PlatformCompositionError):
        assert_profiles_within_ceiling(_make_ceiling())


def test_invalid_utf8_profile_standalone_does_not_crash(profiles_dir):
    # F2-1: the SAME invalid-encoding file on a standalone host (no ceiling) must
    # be tolerated — resolve must not raise UnicodeDecodeError.
    from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY

    path = profiles_dir / "host.json"
    path.write_bytes(b'{"name": "host", "bind": {"type": "surface", "id": "\xff\xfe"}}')
    gp.reset_store()
    # No crash; unbound deny-all → policy-only.
    assert gp.resolve_active_scope(HOST_SESSION_KEY) is None


def test_cold_store_transient_error_recovers_on_metadata_change(profiles_dir, monkeypatch):
    # F2-3 (revised for the merge-preserve redesign): on a COLD store (no prior
    # entry to preserve) a transient read error yields an unbound deny-all →
    # cron surface policy-only. The fingerprint IS committed (NOT uncached — the
    # old uncache hack re-ran iterdir/read_text on every hot-path resolve, an
    # event-loop wedge GPT flagged). Recovery is therefore picked up the normal
    # hot-reload way: when the file's metadata changes again, the next access
    # reloads and the real profile takes effect.
    import os
    from pathlib import Path

    path = profiles_dir / "cron.json"
    path.write_text(
        json.dumps(
            {
                "name": "cron",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read"]},
            }
        )
    )
    real_read_text = Path.read_text
    state = {"fail": True}
    target = str(path)

    def _patched(self, *args, **kwargs):
        if str(self) == target and state["fail"]:
            raise OSError("transient NFS blip")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _patched)
    gp.reset_store()
    # First access hits the transient error on a cold store → unbound deny-all →
    # cron surface policy-only (nothing to preserve).
    assert gp.resolve_active_scope("cron:job-1:run-1") is None
    # Read recovers AND the file's metadata changes (the realistic recovery: a
    # chmod/rewrite). The next access reloads and picks up the real profile.
    state["fail"] = False
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))
    prof = gp.resolve_active_scope("cron:job-1:run-1")
    assert prof is not None and prof.name == "cron"


def test_unreadable_runtime_reload_fails_closed_bind_preserving(profiles_dir, monkeypatch):
    # A profile that already loaded and then becomes UNREADABLE on a later reload
    # must FAIL CLOSED, not preserve its old permissions. The surface stays BOUND
    # (a bind-preserving deny-all — NOT dropped to policy-only), so a
    # tightened-then-unreadable profile can never keep a newly-denied op authorized.
    # Recovery is the normal hot-reload path: a metadata change reloads the fixed
    # file. (assert_profiles_within_ceiling guards BOOT; this is the RUNTIME path.)
    import os
    from pathlib import Path

    path = profiles_dir / "cron.json"
    path.write_text(
        json.dumps(
            {
                "name": "cron",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read"]},
            }
        )
    )
    gp.reset_store()
    # 1) First access loads the real profile (permits read).
    prof = gp.resolve_active_scope("cron:job-1:run-1")
    assert prof is not None and prof.name == "cron"
    assert resolve(None, prof, "tools", "read").permitted

    # 2) Make the read fail AND bump mtime so the fingerprint changes → a reload
    #    is forced, and that reload cannot read the file.
    real_read_text = Path.read_text
    state = {"fail": True}
    target = str(path)

    def _patched(self, *args, **kwargs):
        if str(self) == target and state["fail"]:
            raise OSError("transient NFS blip on reload")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _patched)
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))

    # 3) The surface must STILL resolve (bound, not policy-only) but now DENY —
    #    the old ``read`` permission must NOT survive the unreadable reload.
    prof2 = gp.resolve_active_scope("cron:job-1:run-1")
    assert prof2 is not None, "unreadable reload must stay BOUND (bind-preserving deny-all)"
    assert not resolve(None, prof2, "tools", "read").permitted, (
        "an unreadable profile must fail CLOSED (deny-all), not preserve its "
        "last-known-good permissions"
    )

    # 4) Recovery via the normal hot-reload path: the read clears and metadata
    #    changes → the fixed profile reloads and permits again.
    state["fail"] = False
    st2 = path.stat()
    os.utime(path, (st2.st_atime, st2.st_mtime + 5))
    prof3 = gp.resolve_active_scope("cron:job-1:run-1")
    assert prof3 is not None and prof3.name == "cron"
    assert resolve(None, prof3, "tools", "read").permitted


def test_unreadable_reload_commits_fingerprint_no_reread_storm(profiles_dir, monkeypatch):
    # HIGH (GPT pass 1 #2): a PERSISTENTLY unreadable profile must NOT force a
    # re-read (iterdir + read_text) on EVERY synchronous resolve — that runs on the
    # event loop (hooks.on_tool_call) and can wedge the gateway on a slow FS. After
    # the first unrecoverable reload, the fingerprint is committed, so subsequent
    # resolves with unchanged file metadata do NOT re-enter _reload.
    import os
    from pathlib import Path

    path = profiles_dir / "cron.json"
    path.write_text(
        json.dumps(
            {
                "name": "cron",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read"]},
            }
        )
    )
    gp.reset_store()
    gp.resolve_active_scope("cron:j:r")  # establish last-known-good

    real_read_text = Path.read_text
    reads = {"n": 0}
    target = str(path)

    def _patched(self, *args, **kwargs):
        if str(self) == target:
            reads["n"] += 1
            raise OSError("still unreadable")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _patched)
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))

    # First access after the metadata change reloads once (and fails to read).
    gp.resolve_active_scope("cron:j:r")
    assert reads["n"] == 1
    # Subsequent accesses with UNCHANGED metadata must NOT re-read (fingerprint
    # committed). The old uncache hack re-ran read_text on every call here.
    for _ in range(5):
        gp.resolve_active_scope("cron:j:r")
    assert reads["n"] == 1, "unreadable profile must not trigger a re-read storm on the hot path"


def test_reload_publishes_valid_update_while_other_profile_unreadable(profiles_dir, monkeypatch):
    # HIGH (GPT pass 3): a valid TIGHTENING of profile A must be published even when
    # profile B is unreadable in the same reload. The whole-store rollback would
    # have discarded A's update (kept the old permissive A). Per-profile handling
    # publishes A's update; B fails CLOSED (bind-preserving deny-all), staying bound
    # to its surface rather than dropping to policy-only.
    import os
    from pathlib import Path

    a = profiles_dir / "a.json"
    b = profiles_dir / "b.json"
    a.write_text(
        json.dumps(
            {
                "name": "a",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read", "code"]},
            }
        )
    )
    b.write_text(
        json.dumps(
            {
                "name": "b",
                "bind": {"type": "surface", "id": "dashboard"},
                "tools": {"mode": "allow", "allow": ["read"]},
            }
        )
    )
    gp.reset_store()
    pa = gp.resolve_active_scope("cron:j:r")
    assert pa is not None and resolve(None, pa, "tools", "code").permitted  # A permits code

    # TIGHTEN A (drop code), and make B unreadable, in the same reload.
    a.write_text(
        json.dumps(
            {
                "name": "a",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read"]},
            }
        )
    )
    real_read_text = Path.read_text
    target_b = str(b)

    def _patched(self, *args, **kwargs):
        if str(self) == target_b:
            raise OSError("b unreadable")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _patched)
    # Bump both mtimes so the dir fingerprint changes.
    for p in (a, b):
        st = p.stat()
        os.utime(p, (st.st_atime, st.st_mtime + 5))

    # A's tightening IS published (code now denied) …
    pa2 = gp.resolve_active_scope("cron:j:r")
    assert pa2 is not None and pa2.name == "a"
    assert resolve(None, pa2, "tools", "read").permitted
    assert not resolve(None, pa2, "tools", "code").permitted, "valid tightening of A must publish"
    # … while B fails CLOSED: still BOUND to its surface (not policy-only), but a
    # bind-preserving deny-all — its old ``read`` permission does NOT survive.
    pb = gp.resolve_active_scope("dashboard:x")
    assert pb is not None, "unreadable B must stay bound (bind-preserving deny-all), not drop out"
    assert not resolve(None, pb, "tools", "read").permitted, "unreadable B must fail closed"


def test_malformed_profile_fails_closed_and_recovers_on_metadata_change(profiles_dir, monkeypatch):
    # A profile that becomes MALFORMED (valid JSON, unsalvageable bind) after having
    # loaded must FAIL CLOSED, not preserve its old permissions — a tightened-then-
    # malformed profile must never keep newly-denied ops authorized. The surface
    # stays BOUND (bind recovered from the prior entry → deny-all, not policy-only),
    # and recovers via the normal hot-reload path when the file is fixed (a metadata
    # change busts the fingerprint). There is NO same-metadata retry.
    import os

    path = profiles_dir / "cron.json"
    loose = {
        "name": "cron",
        "bind": {"type": "surface", "id": "cron"},
        "tools": {"mode": "allow", "allow": ["read", "code"]},
    }
    path.write_text(json.dumps(loose))
    gp.reset_store()
    prof0 = gp.resolve_active_scope("cron:j:r")
    assert prof0 is not None and resolve(None, prof0, "tools", "code").permitted

    # Rewrite as VALID JSON but an unsalvageable bind → parse_profile raises,
    # _salvage_bind returns None → recover the bind from the prior entry → deny-all.
    path.write_text(
        json.dumps({"name": "cron", "bind": {"type": "galaxy"}, "tools": {"mode": "x"}})
    )
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))

    prof1 = gp.resolve_active_scope("cron:j:r")
    assert prof1 is not None, "malformed profile must stay BOUND (bind-preserving deny-all)"
    assert not resolve(None, prof1, "tools", "code").permitted, "malformed profile must fail closed"
    assert not resolve(None, prof1, "tools", "read").permitted

    # Fix the file → metadata changes → reloads and permits again (normal path).
    path.write_text(json.dumps(loose))
    st2 = path.stat()
    os.utime(path, (st2.st_atime, st2.st_mtime + 5))
    prof2 = gp.resolve_active_scope("cron:j:r")
    assert prof2 is not None and resolve(None, prof2, "tools", "code").permitted


def test_directory_unenumerable_preserves_last_known_good(profiles_dir, monkeypatch):
    # HIGH (GPT pass 2): a directory iterdir() OSError on a WARM store must NOT
    # clear the bind index to policy-only. The prior snapshot is preserved.
    from pathlib import Path

    path = profiles_dir / "cron.json"
    path.write_text(
        json.dumps(
            {
                "name": "cron",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read"]},
            }
        )
    )
    gp.reset_store()
    prof = gp.resolve_active_scope("cron:j:r")
    assert prof is not None and prof.name == "cron"  # last-known-good established

    # Now make BOTH iterdir (fingerprint + reload enumeration) fail. The
    # fingerprint becomes the ``<absent>`` sentinel (differs from cached), forcing
    # a reload whose iterdir also fails → the early-return preserve path runs.
    real_iterdir = Path.iterdir
    target_dir = str(profiles_dir)

    def _patched(self, *args, **kwargs):
        if str(self) == target_dir:
            raise OSError("dir unreadable")
        return real_iterdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", _patched)

    # The warm snapshot must survive a directory-read failure (not drop to None).
    prof2 = gp.resolve_active_scope("cron:j:r")
    assert prof2 is not None, "dir iterdir() failure must preserve last-known-good, not clear it"
    assert prof2.name == "cron"
    assert resolve(None, prof2, "tools", "read").permitted


def test_missing_profiles_dir_is_absent_no_warning(profiles_dir, monkeypatch, caplog):
    # A NON-EXISTENT profiles dir is the normal "no profiles configured" case
    # (fresh KIROCREW_HOME). It must publish an empty index (policy-only) WITHOUT
    # a WARNING — sandbox/no-isolation tests assert no unexpected WARNING logs and
    # the earlier fix regressed them by warning on every such host.
    import logging

    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles_dir.parent / "does-not-exist")
    gp.reset_store()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.platform.governance_profiles"):
        assert gp.resolve_active_scope("cli_chat") is None
    assert not [r for r in caplog.records if r.levelno == logging.WARNING], caplog.records


def test_cold_start_dir_error_governed_boot_aborts(profiles_dir, monkeypatch):
    # HIGH (GPT pass 2): a NON-FileNotFoundError directory error at COLD start
    # (unreadable EXISTING dir, no prior snapshot) must NOT silently clear to
    # policy-only and pass the boot assertion. It is flagged unrecoverable so a
    # governed fleet boot-aborts.
    from pathlib import Path

    from kiro_crew.platform.context import PlatformCompositionError
    from kiro_crew.platform.governance_profiles import assert_profiles_within_ceiling

    real_iterdir = Path.iterdir
    target_dir = str(profiles_dir)

    def _patched(self, *args, **kwargs):
        if str(self) == target_dir:
            raise PermissionError("EACCES on profiles dir")
        return real_iterdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", _patched)
    gp.reset_store()  # COLD: no prior snapshot

    with pytest.raises(PlatformCompositionError):
        assert_profiles_within_ceiling(_make_ceiling())


def test_parse_error_no_salvageable_bind_stays_bound_via_prior_bind(profiles_dir, monkeypatch):
    # A readable-but-invalid profile with NO salvageable bind must recover its BIND
    # from the last-known-good entry so the surface stays BOUND (a deny-all), rather
    # than dropping from the bind index to policy-only (fail-OPEN of the operator's
    # narrowing). It must NOT preserve the prior PERMISSIONS — the surface denies.
    import os

    path = profiles_dir / "cron.json"
    path.write_text(
        json.dumps(
            {
                "name": "cron",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read"]},
            }
        )
    )
    gp.reset_store()
    prof0 = gp.resolve_active_scope("cron:j:r")
    assert prof0 is not None and resolve(None, prof0, "tools", "read").permitted

    # Rewrite as VALID JSON but with a garbage bind (no salvageable surface/app/
    # task id) → parse_profile raises, _salvage_bind returns None → bind recovered
    # from the prior entry so the surface stays bound (to a deny-all).
    path.write_text(
        json.dumps({"name": "cron", "bind": {"type": "galaxy"}, "tools": {"mode": "x"}})
    )
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))

    prof = gp.resolve_active_scope("cron:j:r")
    assert prof is not None, "must stay BOUND via the recovered prior bind, not drop to policy-only"
    assert not resolve(None, prof, "tools", "read").permitted, "must fail CLOSED (deny-all)"


def test_runtime_hot_added_unrecoverable_marks_health_incident(profiles_dir, monkeypatch):
    # HIGH (GPT round-4 pass 3): a GOVERNED running host that hot-loads a NEW
    # unreadable profile (no prior entry) can't honour it and the boot floor never
    # re-runs. We don't lock the fleet down (one bad file must not DoS every
    # surface), but we MUST make it observable: an ERROR + a governance-health
    # incident so the fail-open window is caught.
    import os
    from pathlib import Path

    # The reload does `from kiro_crew.platform.context import current_context`
    # locally, so patch the SOURCE symbol (not a governance_profiles attribute).
    monkeypatch.setattr(
        "kiro_crew.platform.context.current_context",
        lambda: type("C", (), {"governance": object()})(),
    )
    incidents: list = []
    import kiro_crew.platform.governance_health as gh

    monkeypatch.setattr(
        gh, "mark_governance_incident", lambda k, detail="": incidents.append((k, detail))
    )

    # Start clean (a readable profile), then hot-add a NEW unreadable file.
    path = profiles_dir / "cron.json"
    path.write_text(
        json.dumps(
            {
                "name": "cron",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read"]},
            }
        )
    )
    gp.reset_store()
    assert gp.resolve_active_scope("cron:j:r") is not None

    newp = profiles_dir / "brandnew.json"
    newp.write_text("{}")  # create so iterdir sees it
    real_read_text = Path.read_text

    def _patched(self, *a, **k):
        if str(self) == str(newp):
            raise OSError("unreadable brand-new profile")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _patched)
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))  # bump so dir fingerprint changes

    gp.resolve_active_scope("cron:j:r")
    assert incidents, "a governed runtime unrecoverable profile must raise a health incident"
    assert incidents[0][0] == "unrecoverable_profile"


def test_preserved_dir_error_then_delete_forces_rescan(profiles_dir, monkeypatch):
    # MEDIUM (GPT round-4 pass 1): after preserving on a dir enumeration ERROR,
    # DELETING the dir must force a rescan (distinct <unreadable> vs <absent>
    # fingerprints), not leave stale profiles active.
    from pathlib import Path

    from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY

    (profiles_dir / "host.json").write_text(
        json.dumps(
            {
                "name": "host",
                "bind": {"type": "surface", "id": "host"},
                "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
            }
        )
    )
    gp.reset_store()
    assert gp.resolve_active_scope(HOST_SESSION_KEY) is not None  # warm

    # Enumeration ERROR (not absent) → preserve the warm profile.
    real_iterdir = Path.iterdir
    state = {"mode": "error"}
    target = str(profiles_dir)

    def _patched(self, *a, **k):
        if str(self) == target:
            if state["mode"] == "error":
                raise PermissionError("EACCES")
            raise FileNotFoundError("deleted")
        return real_iterdir(self, *a, **k)

    monkeypatch.setattr(Path, "iterdir", _patched)
    assert gp.resolve_active_scope(HOST_SESSION_KEY) is not None  # preserved on error

    # Now the dir is DELETED (absent). The fingerprint sentinel differs from the
    # <unreadable> one, so the store rescans and the surface drops to policy-only.
    state["mode"] = "absent"
    assert (
        gp.resolve_active_scope(HOST_SESSION_KEY) is None
    ), "deleting the dir after a preserved error must force a rescan, not keep stale profiles"


def test_non_directory_profiles_path_governed_boot_aborts(tmp_path, monkeypatch):
    # HIGH (GPT round-5 pass 3): a `profiles` path that is a regular FILE (misconfig)
    # must NOT be treated as benign absence (which would drop all Level-2 narrowing
    # to policy-only). It routes through the unreadable/OSError branch → a governed
    # cold boot aborts.
    from kiro_crew.platform.context import PlatformCompositionError
    from kiro_crew.platform.governance_profiles import assert_profiles_within_ceiling

    not_a_dir = tmp_path / "profiles"
    not_a_dir.write_text("i am a file, not a directory")  # NotADirectoryError on iterdir
    monkeypatch.setattr(gp, "_PROFILES_DIR", not_a_dir)
    gp.reset_store()

    with pytest.raises(PlatformCompositionError):
        assert_profiles_within_ceiling(_make_ceiling())


def test_non_directory_profiles_path_standalone_is_lenient(tmp_path, monkeypatch):
    # The same misconfig on a standalone host (no ceiling) must not crash — it
    # yields policy-only (empty index), matching pre-split lenient behavior.
    from kiro_crew.platform.governance_profiles import (
        HOST_SESSION_KEY,
        assert_profiles_within_ceiling,
    )

    not_a_dir = tmp_path / "profiles"
    not_a_dir.write_text("file")
    monkeypatch.setattr(gp, "_PROFILES_DIR", not_a_dir)
    gp.reset_store()

    assert_profiles_within_ceiling(None)  # no ceiling → no crash
    assert gp.resolve_active_scope(HOST_SESSION_KEY) is None


def test_warm_store_non_blocking_under_contention_serves_prior_snapshot(profiles_dir):
    # HIGH (GPT round-5 pass 1): on a WARM store _ensure_fresh must NEVER block
    # waiting on the reload lock (it is reachable on the event loop). If another
    # thread holds the lock, the caller returns promptly serving the current
    # snapshot. Unlike the original version of this test, this one PRIMES the store
    # first and asserts WHAT gets served — an assertion-free "it returned" test
    # passes even when the served snapshot is empty (a fail-open; see
    # test_cold_store_contention_never_serves_ungoverned_permit).
    import os
    import time

    path = profiles_dir / "cron.json"
    path.write_text(
        json.dumps(
            {
                "name": "cron",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read"]},
            }
        )
    )
    gp.reset_store()
    # PRIME: the store is now warm (loaded=True) with cron bound.
    assert gp.resolve_active_scope("cron:j:r") is not None
    store = gp._STORE
    assert store._snap.loaded

    # Bump mtime so the fingerprint differs (forcing the reload path), then hold the
    # reload lock as if another thread were mid-reload.
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))
    assert store._lock.acquire(blocking=False)
    try:
        started = time.monotonic()
        prof = gp.resolve_active_scope("cron:j:r")
        elapsed = time.monotonic() - started
    finally:
        store._lock.release()

    assert elapsed < 1.0, f"a warm caller must not block on the reload lock ({elapsed:.2f}s)"
    # And it served the PRIOR (coherent, still-restrictive) snapshot — not nothing.
    assert (
        prof is not None
    ), "warm contention must serve the prior snapshot, not drop to policy-only"
    assert resolve(None, prof, "tools", "read").permitted


def test_cold_store_contention_never_serves_ungoverned_permit(profiles_dir):
    # BLOCKING (security regression guard): the FIRST load must not be skippable.
    # A never-loaded snapshot is EMPTY, and an empty snapshot is indistinguishable
    # from "no profiles configured" — resolve_active_scope returns None and
    # governance_permits hands back Decision(True, "ungoverned"), a fail-OPEN that
    # fail_closed=True cannot catch (the default-permit is a normal return, not an
    # exception). Concurrent first-touch is the EXPECTED case: an unprimed store plus
    # a startup burst across the five transports puts several mc-gov threads here at
    # once. Regression-locks that every concurrent caller sees the profile's DENY.
    (profiles_dir / "host.json").write_text(
        json.dumps(
            {
                "name": "host",
                "bind": {"type": "surface", "id": "host"},
                "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
            }
        )
    )
    results: "list[bool]" = []
    lock = threading.Lock()

    def _worker() -> None:
        decision = gp.governance_permits(
            "channels", "discord", session_key=gp.HOST_SESSION_KEY, fail_closed=True
        )
        with lock:
            results.append(bool(getattr(decision, "permitted", False)))

    # COLD store (no boot priming — the ungoverned/Level-2-only path never calls
    # assert_profiles_within_ceiling), several threads racing the first load.
    gp.reset_store()
    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 4, "every worker must produce a decision (no deadlock)"
    assert not any(results), (
        "a cold-store concurrent first-touch must NEVER permit a profile-denied "
        f"transport (got {results}) — the first load may not be skipped"
    )


def test_failed_first_load_does_not_cache_a_permissive_state(profiles_dir, monkeypatch):
    # BLOCKING (fail-closed guard): if the FIRST reload raises, the store must not
    # commit the fingerprint or mark itself loaded — otherwise the empty
    # never-loaded snapshot would be cached as authoritative and every later call
    # would fast-path to the ``ungoverned`` default-PERMIT, making one transient
    # read error a permanent fail-open. The failing call itself must DENY
    # (fail_closed=True), and the next call must RETRY the load and enforce.
    (profiles_dir / "host.json").write_text(
        json.dumps(
            {
                "name": "host",
                "bind": {"type": "surface", "id": "host"},
                "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
            }
        )
    )
    gp.reset_store()
    store = gp._STORE
    real_reload = store._reload
    calls = {"n": 0}

    def _failing_once(directory):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient reload failure")
        return real_reload(directory)

    monkeypatch.setattr(store, "_reload", _failing_once)

    first = gp.governance_permits(
        "channels", "discord", session_key=gp.HOST_SESSION_KEY, fail_closed=True
    )
    assert not first.permitted, "a failed first load must DENY under fail_closed"
    assert not store._snap.loaded, "a failed load must not mark the store loaded"
    assert store._fingerprint is None, "a failed load must not commit a fingerprint"

    second = gp.governance_permits(
        "channels", "discord", session_key=gp.HOST_SESSION_KEY, fail_closed=True
    )
    assert calls["n"] == 2, "the next access must RETRY the load, not serve a cached empty"
    assert not second.permitted, "the profile's deny must be enforced once the load succeeds"
    assert second.layer == "profile"


def test_under_lock_restat_commits_fingerprint_of_published_snapshot(profiles_dir, monkeypatch):
    # MEDIUM: the fingerprint committed after a reload must describe the snapshot
    # actually published. Re-statting UNDER the lock (rather than reusing a pre-lock
    # value) is what guarantees that: a pre-lock read can go stale while this thread
    # waits, and committing it would cache a fingerprint for a state never loaded,
    # forcing a redundant reload on the next access. Pin it by asserting the
    # committed fingerprint equals a fresh stat of the final on-disk state.
    path = profiles_dir / "cron.json"
    path.write_text(
        json.dumps(
            {
                "name": "cron",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read"]},
            }
        )
    )
    gp.reset_store()
    assert gp.resolve_active_scope("cron:j:r") is not None
    store = gp._STORE
    assert store._fingerprint == (gp._dir_fingerprint(profiles_dir), gp._fallback_token())

    # A further edit reloads and re-commits, still matching the on-disk state.
    path.write_text(
        json.dumps(
            {
                "name": "cron",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read", "code"]},
            }
        )
    )
    prof = gp.resolve_active_scope("cron:j:r")
    assert prof is not None and resolve(None, prof, "tools", "code").permitted
    assert store._fingerprint == (gp._dir_fingerprint(profiles_dir), gp._fallback_token())

    # And the store has converged: a further access does NOT reload again.
    calls = {"n": 0}
    real_reload = store._reload

    def _counting_reload(directory):
        calls["n"] += 1
        return real_reload(directory)

    monkeypatch.setattr(store, "_reload", _counting_reload)
    gp.resolve_active_scope("cron:j:r")
    assert calls["n"] == 0, "a converged store must not reload again on the next access"


def test_unreadable_profile_recovers_on_ctime_change(profiles_dir, monkeypatch):
    # HIGH (GPT round-7 pass 1 #305): a chmod that FIXES perms on a previously-
    # unreadable profile changes ctime but NOT mtime/size — so the fingerprint must
    # include ctime, else the unreadable fallback stays cached forever and the
    # profile's restrictions remain bypassed. Simulate: file readable → unreadable
    # (fallback) → readable again with ONLY ctime bumped → must re-read.
    import os
    from pathlib import Path

    path = profiles_dir / "cron.json"
    path.write_text(
        json.dumps(
            {
                "name": "cron",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read"]},
            }
        )
    )
    gp.reset_store()
    assert gp.resolve_active_scope("cron:j:r") is not None  # last-known-good

    real_read_text = Path.read_text
    state = {"fail": True}
    target = str(path)

    def _patched(self, *a, **k):
        if str(self) == target and state["fail"]:
            raise OSError("EACCES")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _patched)
    # Make it unreadable and bump mtime so the store reloads and hits the failure.
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))
    # Preserved (last-known-good) while unreadable — still resolves.
    assert gp.resolve_active_scope("cron:j:r") is not None

    # Now perms are "fixed": readable again, but ONLY ctime changes (a chmod does
    # not touch mtime/size). Force a ctime bump by leaving mtime/size identical and
    # relying on the fingerprint including st_ctime_ns. On most FSes any metadata
    # write bumps ctime; simulate by re-writing identical bytes then restoring mtime.
    state["fail"] = False
    before_ct = path.stat().st_ctime_ns
    os.chmod(path, 0o644)  # a real chmod — bumps ctime, not mtime/size
    # If the platform's chmod didn't move ctime (rare), skip rather than false-fail.
    if path.stat().st_ctime_ns == before_ct:
        import pytest as _pytest

        _pytest.skip("platform chmod did not change st_ctime_ns")
    prof = gp.resolve_active_scope("cron:j:r")
    assert prof is not None and prof.name == "cron"
    assert resolve(None, prof, "tools", "read").permitted


def test_index_published_atomically_as_one_snapshot(profiles_dir):
    # HIGH (GPT round-8): by_name + by_bind must be published as ONE immutable
    # snapshot, never two separate assignments — else a lock-free reader could see
    # new names with old bindings after a rename and for_bind would miss the new
    # binding (fail-open to policy-only). Assert the store exposes a single _snap
    # object and that a reader reads all fields from it consistently.
    from kiro_crew.platform.governance_profiles import _Snapshot

    _write(
        profiles_dir,
        "cron",
        {
            "name": "cron",
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    gp.reset_store()
    # Resolve once to populate.
    assert gp.resolve_active_scope("cron:j:r") is not None
    store = gp._STORE
    # The whole view is one frozen object; by_bind's target always exists in
    # by_name (no torn state where a bind points at a missing name).
    snap = store._snap
    assert isinstance(snap, _Snapshot)
    for (_btype, _bid), name in snap.by_bind.items():
        assert name in snap.by_name, f"bind → {name!r} missing from by_name (torn snapshot)"


def test_rename_does_not_expose_new_name_with_old_binding(profiles_dir):
    # HIGH (GPT round-8): renaming a profile file (stem change) must swap the
    # WHOLE snapshot — the new stem and its binding land together, and the old
    # stem+binding vanish together. No intermediate state where the cron surface
    # resolves to None because by_bind was updated but by_name wasn't (or vice
    # versa). Since the swap is a single assignment, any read sees fully-old or
    # fully-new — verified by resolving before and after.
    import os

    p1 = profiles_dir / "cron-a.json"
    p1.write_text(
        json.dumps(
            {
                "name": "cron-a",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read"]},
            }
        )
    )
    gp.reset_store()
    prof = gp.resolve_active_scope("cron:j:r")
    assert prof is not None and prof.name == "cron-a"

    # Rename the file (new stem, SAME bind). One reload → one snapshot swap.
    p2 = profiles_dir / "cron-b.json"
    p1.rename(p2)
    p2.write_text(
        json.dumps(
            {
                "name": "cron-b",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read"]},
            }
        )
    )
    st = p2.stat()
    os.utime(p2, (st.st_atime, st.st_mtime + 5))

    # The cron surface still resolves (never a torn None) — now to the new stem.
    prof2 = gp.resolve_active_scope("cron:j:r")
    assert prof2 is not None, "rename must not expose a torn snapshot (bind → missing name)"
    assert prof2.name == "cron-b"


def test_metadata_change_reload_walks_dir_once(profiles_dir, monkeypatch):
    # BLOCKING (GPT round-15): on a genuine metadata change the reload must walk the
    # profiles dir exactly ONCE. _ensure_fresh is reachable on the event loop (the
    # synchronous PreToolUse gate), and it acquires the lock with blocking=False —
    # so it never waited and its pre-lock fingerprint is still current. Re-statting
    # under the lock would run a SECOND iterdir+stat walk on the loop (a slow-FS
    # stall) for no freshness gain. This pins _dir_fingerprint at one call per
    # reload.
    import os

    import kiro_crew.platform.governance_profiles as gpm

    path = profiles_dir / "cron.json"
    path.write_text(
        json.dumps(
            {
                "name": "cron",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read"]},
            }
        )
    )
    gp.reset_store()
    assert gp.resolve_active_scope("cron:j:r") is not None  # prime the cache

    # Genuine metadata change (bump mtime) so the caller takes the reload branch
    # (fingerprint changed → acquire lock → _reload), not the fast path.
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))

    real_fp = gpm._dir_fingerprint
    calls = {"n": 0}

    def _counting_fp(directory):
        calls["n"] += 1
        return real_fp(directory)

    monkeypatch.setattr(gpm, "_dir_fingerprint", _counting_fp)
    assert gp.resolve_active_scope("cron:j:r") is not None
    assert calls["n"] == 1, (
        "a reload must walk the profiles dir exactly ONCE (pre-lock); a second "
        f"under-lock walk stalls the event loop (got {calls['n']})"
    )


def test_absent_profile_still_yields_policy_only(profiles_dir):
    # GUARDRAIL: the fix must NOT manufacture a deny for a surface that has NO
    # profile at all. An absent host profile → resolve_active_scope returns None
    # (attended/host surface, policy ceiling alone governs), NOT a false deny.
    from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY

    # profiles_dir is empty (no host.json).
    assert gp.resolve_active_scope(HOST_SESSION_KEY) is None


def test_vanished_profile_mid_reload_is_absent_not_deny(profiles_dir, monkeypatch):
    # A file present at iterdir() but gone at read (TOCTOU) is treated as ABSENT
    # (skipped), not as a present-but-unreadable deny — a missing file is not a
    # policy. FileNotFoundError is a subclass of OSError, so the reload must
    # distinguish it from the genuine-unreadable case.
    from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY

    path = profiles_dir / "host.json"
    path.write_text(
        json.dumps(
            {
                "name": "host",
                "bind": {"type": "surface", "id": "host"},
                "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
            }
        )
    )
    _make_read_text_raise(monkeypatch, path, FileNotFoundError("vanished"))
    gp.reset_store()
    # Vanished → absent → policy-only (None), no false deny.
    assert gp.resolve_active_scope(HOST_SESSION_KEY) is None


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


def test_unreadable_composed_child_denies_when_parent_tightened(profiles_dir, monkeypatch):
    # A composed child (``extends`` a parent) that becomes UNREADABLE while its
    # parent is TIGHTENED in the same reload must NOT keep its stale inherited
    # permissions — the newly-denied parent op must not stay authorized. Under the
    # blanket fail-closed policy an unreadable profile is a bind-preserving deny-all
    # regardless of whether it was a composed child, which covers this case. We
    # prove: child permits ``code`` while the parent allows it; then the parent
    # drops ``code`` AND the child becomes unreadable → the child must now DENY.
    from pathlib import Path

    base = profiles_dir / "base.json"
    child = profiles_dir / "child.json"
    base.write_text(
        json.dumps({"name": "base", "tools": {"mode": "allow", "allow": ["read", "code"]}})
    )
    child.write_text(
        json.dumps(
            {
                "name": "child",
                "extends": "base",
                "bind": {"type": "surface", "id": "dashboard"},
                "tools": {"mode": "allow", "allow": ["read", "code"]},
            }
        )
    )
    gp.reset_store()
    prof = gp.resolve_active_scope("dashboard:x")
    assert (
        prof is not None and resolve(None, prof, "tools", "code").permitted
    )  # composed, permits code

    # Tighten the parent (drop ``code``) AND make the CHILD unreadable in the same
    # reload. Bump both files' mtime so the fingerprint busts and the reload runs.
    import os

    base.write_text(json.dumps({"name": "base", "tools": {"mode": "allow", "allow": ["read"]}}))
    real_read_text = Path.read_text
    child_str = str(child)

    def _patched(self, *a, **k):
        if str(self) == child_str:
            raise OSError("child unreadable during parent tightening")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _patched)
    for p in (base, child):
        st = p.stat()
        os.utime(p, (st.st_atime, st.st_mtime + 5))

    prof2 = gp.resolve_active_scope("dashboard:x")
    assert prof2 is not None, "the bound surface must still resolve (bind-preserving deny-all)"
    assert not resolve(None, prof2, "tools", "code").permitted, (
        "an unreadable composed child must FAIL CLOSED on a newly-denied op, not "
        "serve the stale inherited permission from before the parent was tightened"
    )
    assert not resolve(None, prof2, "tools", "read").permitted  # deny-all, not the stale child


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


def test_resolution_is_checked_before_bind_lookups(profiles_dir, monkeypatch):
    # BLOCKING (GPT #593 round 2): resolution must be confirmed BEFORE any bind
    # lookup, not after. Checking afterwards is a check-AFTER-use: the lookup can
    # read the empty never-loaded snapshot, the first load can then complete, and
    # the late check reports "resolved" — so a miss that really meant "not loaded
    # yet" is reported as the authoritative "no profile bound" (policy-only), a
    # fail-OPEN. Pin the ordering: on an unprimed store whose first load is owned
    # by another thread, resolve_active_scope must DENY without consulting binds.
    (profiles_dir / "host.json").write_text(
        json.dumps(
            {
                "name": "host",
                "bind": {"type": "surface", "id": "host"},
                "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
            }
        )
    )
    gp.reset_store()
    store = gp._STORE
    assert not store._snap.loaded

    # Hold the reload lock: the caller is an unprimed contender that cannot load.
    assert store._lock.acquire(blocking=False)
    try:
        looked_up: "list[object]" = []
        real_snapshot = store.snapshot

        def _tracking_snapshot():
            looked_up.append(True)
            return real_snapshot()

        monkeypatch.setattr(store, "snapshot", _tracking_snapshot)
        prof = gp.resolve_active_scope(gp.HOST_SESSION_KEY)
    finally:
        store._lock.release()

    assert prof is not None, "an unprimed contender must not resolve to policy-only"
    assert prof.name.startswith("_deny_all_unloaded"), prof.name
    assert not resolve(None, prof, "channels", "slack").permitted
    assert not looked_up, "resolution must be checked BEFORE any bind lookup"


# ──────────────────────────────────────────────────────────────────────────
# fallback_profile_names — visibility into which profiles are deny-all fallbacks
# ──────────────────────────────────────────────────────────────────────────


def test_fallback_profile_names_empty_on_valid_profiles(profiles_dir):
    """A correctly-parsed profile does not appear in fallback_profile_names."""
    _write(
        profiles_dir,
        "host",
        {
            "name": "host",
            "bind": {"type": "surface", "id": "host"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    assert gp.fallback_profile_names() == frozenset()


def test_fallback_profile_names_includes_unreadable_file(profiles_dir, monkeypatch):
    """A present-but-unreadable file lands in fallback_profile_names."""
    path = profiles_dir / "host.json"
    _write_host_profile(path)
    _make_read_text_raise(monkeypatch, path, OSError("permission denied"))
    gp.reset_store()
    assert "host" in gp.fallback_profile_names()


def test_fallback_profile_names_includes_invalid_utf8(profiles_dir):
    """An invalid-encoding file lands in fallback_profile_names."""
    path = profiles_dir / "cron.json"
    path.write_bytes(b"\xff\xfe invalid utf8")
    gp.reset_store()
    assert "cron" in gp.fallback_profile_names()


def test_fallback_profile_names_includes_invalid_json(profiles_dir):
    """A file with invalid JSON lands in fallback_profile_names."""
    (profiles_dir / "host.json").write_text("not valid json {{{")
    gp.reset_store()
    assert "host" in gp.fallback_profile_names()


def test_fallback_profile_names_includes_invalid_schema(profiles_dir):
    """A file with valid JSON but invalid schema lands in fallback_profile_names."""
    _write(
        profiles_dir,
        "host",
        {
            "name": "host",
            "bind": {"type": "surface", "id": "host"},
            "tools": {"mode": "banana"},  # invalid mode
        },
    )
    assert "host" in gp.fallback_profile_names()


def test_fallback_profile_names_valid_profile_not_in_set(profiles_dir):
    """A valid profile coexisting with a broken one does not appear in fallback."""
    _write(
        profiles_dir,
        "cron",
        {
            "name": "cron",
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    (profiles_dir / "broken.json").write_text("not json")
    gp.reset_store()
    names = gp.fallback_profile_names()
    assert "broken" in names
    assert "cron" not in names


def test_fallback_profile_names_includes_broken_extends_chain(profiles_dir):
    """A profile whose ``extends`` parent is missing is also a deny-all fallback.

    This is the third substitution site in ``_reload`` and the least obvious one:
    the file itself parses perfectly, so nothing about it looks broken — only the
    unresolvable chain makes it deny-all. Without it recorded here the operator
    sees the same total-lockdown page with no explanation, which is exactly the
    symptom the fallback signal exists to remove.
    """
    _write(
        profiles_dir,
        "cron",
        {
            "name": "cron",
            "bind": {"type": "surface", "id": "cron"},
            "extends": "no-such-parent",
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    gp.reset_store()
    assert "cron" in gp.fallback_profile_names()
    # And the surface is still BOUND to that deny-all rather than falling through
    # to the ceiling alone — the fail-closed invariant the branch exists for.
    resolved = gp.resolve_active_scope("cron:job-7:run-1")
    assert resolved is not None
    assert resolved.name == "cron"


def test_fallback_profile_names_excludes_a_valid_extends_chain(profiles_dir):
    """The companion guard: a RESOLVABLE chain must not be reported as a fallback.

    Without this, the assertion above would still pass if every ``extends`` user
    were flagged, which would put a permanent false banner on a perfectly good
    profile hierarchy.
    """
    _write(
        profiles_dir,
        "base",
        {"name": "base", "tools": {"mode": "allow", "allow": ["read", "write"]}},
    )
    _write(
        profiles_dir,
        "cron",
        {
            "name": "cron",
            "bind": {"type": "surface", "id": "cron"},
            "extends": "base",
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    gp.reset_store()
    assert gp.fallback_profile_names() == frozenset()


def test_fallback_profile_names_bind_preserved_unreadable(profiles_dir, monkeypatch):
    """A bind-preserving deny-all (from a prior load) still appears in fallback."""
    import os

    path = profiles_dir / "host.json"
    _write_host_profile(path)
    gp.reset_store()
    # First load succeeds.
    assert gp.fallback_profile_names() == frozenset()

    # Make unreadable and bump mtime.
    _make_read_text_raise(monkeypatch, path, OSError("permission denied"))
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))

    assert "host" in gp.fallback_profile_names()


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
