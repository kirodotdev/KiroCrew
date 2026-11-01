"""Tests for kiro_crew.security: ``is_sensitive_path`` and the home-directory target cache.

The path fence over credential stores and keystone leaves, then the TTL cache in front of the
home-target rebuild and the adaptive expiry that tracks what each rebuild cost.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from kiro_crew.security import is_sensitive_path


class TestIsSensitivePath:
    """Tests for is_sensitive_path()."""

    def test_aws_credentials(self) -> None:
        assert is_sensitive_path("~/.aws/credentials") is True

    def test_aws_dir(self) -> None:
        assert is_sensitive_path("~/.aws") is True

    def test_ssh_dir(self) -> None:
        assert is_sensitive_path("~/.ssh/id_rsa") is True

    def test_gnupg(self) -> None:
        assert is_sensitive_path("~/.gnupg/private-keys-v1.d") is True

    def test_kirocrew_env(self) -> None:
        # The data home moved to ~/.kiro/crew; the legacy ~/.kirocrew stays gated
        # (migration leaves a rollback copy that still holds real secret bytes).
        assert is_sensitive_path("~/.kiro/crew/.env") is True
        assert is_sensitive_path("~/.kirocrew/.env") is True

    def test_browser_auth_cookie_paths(self) -> None:
        # The browser-auth cookie jar + the Playwright storage-state derived from
        # it hold reusable authenticated-session cookies. Agent file tools must
        # not read them through the shared gate, or a prompt-injected turn could
        # exfiltrate live browser sessions.
        home = str(Path.home())
        assert is_sensitive_path("~/.kiro/crew/browser-cookies.txt") is True
        assert is_sensitive_path("~/.kiro/crew/playwright-storage-state.json") is True
        assert is_sensitive_path(f"{home}/.kiro/crew/browser-cookies.txt") is True
        assert is_sensitive_path(f"{home}/.kiro/crew/playwright-storage-state.json") is True
        # Legacy pre-move home is still gated.
        assert is_sensitive_path("~/.kirocrew/browser-cookies.txt") is True
        assert is_sensitive_path(f"{home}/.kirocrew/playwright-storage-state.json") is True

    def test_sel_hmac_key(self) -> None:
        # The SEL HMAC signing key is the trust root of
        # the tamper-evident audit chain. If an audited agent could fs_read it,
        # it could forge the entire chain, so it must be sensitive (read-blocked).
        # The key lives at trust/sel_hmac.key (whole-dir gate); the bare leaf
        # covers pre-migration installs and stale post-restore leftovers.
        assert is_sensitive_path("~/.kiro/crew/sel_hmac.key") is True
        assert is_sensitive_path("~/.kirocrew/sel_hmac.key") is True
        assert is_sensitive_path("~/.kiro/crew/trust") is True
        assert is_sensitive_path("~/.kiro/crew/trust/sel_hmac.key") is True
        assert is_sensitive_path("~/.kirocrew/trust") is True
        assert is_sensitive_path("~/.kirocrew/trust/sel_hmac.key") is True

    def test_security_events_log(self) -> None:
        # The SEL audit log itself must not be
        # readable/rewritable by the audited agent (tamper of the evidence trail).
        assert is_sensitive_path("~/.kiro/crew/security_events.jsonl") is True
        assert is_sensitive_path("~/.kirocrew/security_events.jsonl") is True

    def test_rotated_security_event_segments(self) -> None:
        # A rotated segment holds exactly the same audit records the live log
        # does (sel_rotation.py closes the log at a size cap and renames it into this
        # dir), so rotation must not become the way around the fence.
        assert is_sensitive_path("~/.kiro/crew/security_events.d") is True
        assert (
            is_sensitive_path(
                "~/.kiro/crew/security_events.d/security_events-000001-20260821T045139Z.jsonl"
            )
            is True
        )
        assert is_sensitive_path("~/.kirocrew/security_events.d") is True
        assert (
            is_sensitive_path(
                "~/.kirocrew/security_events.d/security_events-000001-20260821T045139Z.jsonl"
            )
            is True
        )

    def test_sel_files_absolute_path(self) -> None:
        home = str(Path.home())
        assert is_sensitive_path(f"{home}/.kiro/crew/sel_hmac.key") is True
        assert is_sensitive_path(f"{home}/.kiro/crew/trust/sel_hmac.key") is True
        assert is_sensitive_path(f"{home}/.kiro/crew/security_events.jsonl") is True
        assert is_sensitive_path(f"{home}/.kirocrew/sel_hmac.key") is True
        assert is_sensitive_path(f"{home}/.kirocrew/trust/sel_hmac.key") is True
        assert is_sensitive_path(f"{home}/.kirocrew/security_events.jsonl") is True

    def test_app_admission_policy(self) -> None:
        # Keystone invariant: app_admission.json is the sole fleet-controlled
        # admission ceiling (apps/admission.py). An absent file falls open, so a
        # governed agent that could rewrite/delete it could neuter its own
        # admission ceiling — it must be read/write blocked via the shared gate.
        home = str(Path.home())
        assert is_sensitive_path("~/.kiro/crew/app_admission.json") is True
        assert is_sensitive_path(f"{home}/.kiro/crew/app_admission.json") is True
        assert is_sensitive_path("~/.kirocrew/app_admission.json") is True

    def test_token_signing_key(self) -> None:
        # token_signing.key (dashboard/token_secret.py) signs every
        # dashboard access + refresh token. An agent that could fs_read it could
        # forge auth tokens for itself, so it must be read-blocked like the SEL
        # HMAC key above.
        assert is_sensitive_path("~/.kiro/crew/token_signing.key") is True
        assert is_sensitive_path("~/.kirocrew/token_signing.key") is True

    def test_refresh_chains_json(self) -> None:
        # refresh_chains.json (dashboard/refresh_tokens.py) stores
        # refresh-token chain state for minting new access tokens.
        assert is_sensitive_path("~/.kiro/crew/refresh_chains.json") is True
        assert is_sensitive_path("~/.kirocrew/refresh_chains.json") is True

    def test_local_secret(self) -> None:
        # .local_secret is the shared internal-auth secret used, in turn, to
        # authenticate MCP/cron/hook callbacks back into the gateway
        # (mcp_core.py, cron_script_child_env.py, mcp_shared_policy.py, etc.).
        assert is_sensitive_path("~/.kiro/crew/.local_secret") is True
        assert is_sensitive_path("~/.kirocrew/.local_secret") is True

    def test_kiro_cli_binary_attestation(self) -> None:
        assert is_sensitive_path("~/.kiro/crew/.kiro_cli_binary_trust.json") is True
        assert is_sensitive_path("~/.kirocrew/.kiro_cli_binary_trust.json") is True

    def test_kiro_auth_staging_parent(self) -> None:
        assert is_sensitive_path("~/.kiro/crew-auth-staging") is True
        assert is_sensitive_path("~/.kiro/crew-auth-staging/auth-123/token.json") is True

    def test_dashboard_secrets_absolute_path(self) -> None:
        home = str(Path.home())
        assert is_sensitive_path(f"{home}/.kiro/crew/token_signing.key") is True
        assert is_sensitive_path(f"{home}/.kiro/crew/refresh_chains.json") is True
        assert is_sensitive_path(f"{home}/.kiro/crew/.local_secret") is True
        assert is_sensitive_path(f"{home}/.kirocrew/token_signing.key") is True
        assert is_sensitive_path(f"{home}/.kirocrew/refresh_chains.json") is True
        assert is_sensitive_path(f"{home}/.kirocrew/.local_secret") is True

    def test_non_sel_crew_file_not_blocked(self) -> None:
        # Regression guard: the SEL additions must not over-block routine
        # crew-home reads (config.json, sessions.db) that operators/tools need.
        assert is_sensitive_path("~/.kiro/crew/config.json") is False
        assert is_sensitive_path("~/.kiro/crew/sessions.db") is False
        assert is_sensitive_path("~/.kirocrew/config.json") is False
        assert is_sensitive_path("~/.kirocrew/sessions.db") is False

    def test_safe_path(self) -> None:
        assert is_sensitive_path("~/Documents/code/main.py") is False

    def test_absolute_aws_path(self) -> None:
        home = str(Path.home())
        assert is_sensitive_path(f"{home}/.aws/credentials") is True

    def test_unrelated_dotfile(self) -> None:
        assert is_sensitive_path("~/.bashrc") is False

    # ── Symlink bypass ──

    def test_absolute_symlink_to_aws_credentials(self, tmp_path, monkeypatch) -> None:
        """A symlink whose target resolves into ~/.aws must be caught."""
        home = tmp_path / "home"
        (home / ".aws").mkdir(parents=True)
        cred = home / ".aws" / "credentials"
        cred.write_text("[default]\n")
        # is_sensitive_path anchors on Path.home(), which reads USERPROFILE on
        # Windows and never HOME, so pinning only HOME builds a fixture in a home
        # directory the matcher never consults there.
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        ws = tmp_path / "workspace"
        ws.mkdir()
        link = ws / "cfg.ini"
        link.symlink_to(cred)  # absolute target
        assert is_sensitive_path(str(link)) is True

    def test_relative_symlink_to_aws_credentials(self, tmp_path, monkeypatch) -> None:
        """A relative-traversal symlink target must resolve and be caught."""
        home = tmp_path / "home"
        (home / ".aws").mkdir(parents=True)
        cred = home / ".aws" / "credentials"
        cred.write_text("[default]\n")
        # Both spellings: Path.home() reads USERPROFILE on Windows (see above).
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        ws = tmp_path / "workspace" / "sub"
        ws.mkdir(parents=True)
        link = ws / "alt.txt"
        import os as _os

        link.symlink_to(_os.path.relpath(str(cred), start=str(ws)))
        assert is_sensitive_path(str(link)) is True

    def test_base_dir_anchors_relative_path(self, tmp_path, monkeypatch) -> None:
        """A relative input is anchored against base_dir, not the process CWD."""
        home = tmp_path / "home"
        (home / ".aws").mkdir(parents=True)
        cred = home / ".aws" / "credentials"
        cred.write_text("[default]\n")
        # Both spellings: Path.home() reads USERPROFILE on Windows (see above).
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "cfg.ini").symlink_to(cred)
        # Relative path only resolves to the symlink when anchored at ws.
        assert is_sensitive_path("cfg.ini", base_dir=str(ws)) is True
        assert is_sensitive_path("Documents/notes.md", base_dir=str(ws)) is False

    def test_lexical_fallback_when_unresolvable(self, monkeypatch, tmp_path) -> None:
        """A path that textually names ~/.aws is caught even if it does not exist."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        assert is_sensitive_path("~/.aws/does-not-exist-yet") is True

    def test_empty_path(self) -> None:
        assert is_sensitive_path("") is False


class TestHomeDirTargetsCache:
    """Tests for the TTL cache in front of ``_home_dir_targets_uncached``.

    The cache exists because rebuilding the target set was 91% of every
    ``is_sensitive_path`` call (it realpath()s ``$HOME`` and each crew-home
    leaf), and callers hit it per FILE. These tests pin the two properties that
    make caching a security gate's inputs acceptable: the cached set is
    equivalent to an uncached build, and an env change is reflected AT ONCE
    rather than after the TTL.
    """

    @staticmethod
    def _clear() -> None:
        from kiro_crew import security

        security._home_targets_cache.clear()
        # Clear any recorded stall so a PathResolutionStalled from one test
        # does not poison the next test in this class.
        security._path_resolve_degraded.clear()

    def test_cached_result_matches_uncached(self, monkeypatch, tmp_path) -> None:
        """Caching must not change WHAT is considered sensitive."""
        from kiro_crew.security import (
            _SENSITIVE_HOME_DIRS,
            _home_dir_targets,
            _home_dir_targets_uncached,
        )

        monkeypatch.setenv("HOME", str(tmp_path))
        self._clear()
        assert _home_dir_targets(_SENSITIVE_HOME_DIRS) == _home_dir_targets_uncached(
            _SENSITIVE_HOME_DIRS
        )

    def test_second_call_does_not_rebuild(self, monkeypatch, tmp_path) -> None:
        """Within the TTL the expensive builder runs once, not per call.

        The cache compares ``time.monotonic()`` against a stored deadline
        (``_home_dir_targets`` reads the clock once to test the deadline, and
        once more after a build to measure what the build cost), so the
        clock is FROZEN here rather than raced: with a constant monotonic
        source, "every call is inside the TTL" is a fact of the test instead
        of a bet that the loop outruns the expiry floor
        (``_home_targets_ttl(0.0)``, 0.1s) on
        the slowest runner in the matrix. That removes the only
        platform-dependent input — before this, the assertion held only while
        50 iterations plus one ~1.4ms rebuild finished inside 100ms, which the
        Windows shards do not guarantee.

        The second half advances the fake clock past the TTL and requires a
        rebuild. That direction pins the TTL behavior itself AND proves the
        freeze took effect: were the patch silently a no-op, the +0.11s jump
        would not have happened in real time and the rebuild would not occur,
        failing the final assertion instead of degrading back into a timing
        race.
        """
        from kiro_crew import security

        monkeypatch.setenv("HOME", str(tmp_path))
        self._clear()
        calls: list[int] = []
        real = security._home_dir_targets_uncached

        def counting(home_dirs, roots=None):
            calls.append(1)
            return real(home_dirs, roots)

        monkeypatch.setattr(security, "_home_dir_targets_uncached", counting)
        clock = {"now": 1000.0}
        # flake-ok: pre-existing frozen stdlib monotonic scoped to this test by monkeypatch; moved verbatim and tracked for a de-flake pass
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing frozen monotonic scoped to this test by monkeypatch and moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(security.time, "monotonic", lambda: clock["now"])
        for _ in range(50):
            security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(calls) == 1

        # Guard: advancing the frozen clock past the TTL MUST rebuild.
        # Advance past the EFFECTIVE expiry, not past the floor constant. Under a
        # frozen clock a build measures as taking zero time, so the adaptive law
        # returns its floor -- but reading it through the law is what keeps this
        # jump correct if the floor is ever reached differently.
        clock["now"] += security._home_targets_ttl(0.0) + 0.01
        security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(calls) == 2

    def test_kirocrew_home_change_is_not_deferred_by_ttl(self, monkeypatch, tmp_path) -> None:
        """A changed KIROCREW_HOME must re-key immediately, not after the TTL.

        This is the security-relevant property: the keystone secrets live under
        KIROCREW_HOME, so a stale target set built for the OLD home would stop
        gating them. The resolved roots are part of the cache key precisely so
        this cannot wait out ``_HOME_TARGETS_TTL_SECS``.
        """
        from kiro_crew import security

        monkeypatch.setenv("HOME", str(tmp_path))
        home_a = tmp_path / "crew-a"
        home_b = tmp_path / "crew-b"
        home_a.mkdir()
        home_b.mkdir()
        self._clear()

        monkeypatch.setenv("KIROCREW_HOME", str(home_a))
        targets_a = set(security._home_dir_targets(security._SENSITIVE_HOME_DIRS))
        monkeypatch.setenv("KIROCREW_HOME", str(home_b))
        targets_b = set(security._home_dir_targets(security._SENSITIVE_HOME_DIRS))

        # No sleep: the switch is visible on the very next call.
        assert targets_a != targets_b
        assert any(str(home_b).casefold() in t for t in targets_b)
        # And the new home's secrets are actually gated through the public API.
        assert is_sensitive_path(str(home_b / "token_signing.key")) is True

    def test_repointed_home_symlink_is_not_served_from_cache(self, monkeypatch, tmp_path) -> None:
        """Repointing a symlink AT $HOME must invalidate the cached target set.

        Regression test for a real, reproduced fail-open: the builder anchors on
        ``Path.home().resolve()``, so when ``$HOME`` is itself a symlink every
        target moves while the ``$HOME`` string stays identical. Keying the cache
        on the raw env var therefore served a stale set and is_sensitive_path()
        returned False for a credential path the uncached code blocked. The key
        uses the RESOLVED root so the repoint re-keys.
        """
        real_a = tmp_path / "vol1" / "u"
        real_b = tmp_path / "vol2" / "u"
        real_a.mkdir(parents=True)
        real_b.mkdir(parents=True)
        link = tmp_path / "home"
        try:
            link.symlink_to(real_a)
        except (OSError, NotImplementedError):  # pragma: no cover — Windows w/o privilege
            pytest.skip("symlink creation not permitted on this platform")
        # Path.home() reads HOME on POSIX and USERPROFILE on Windows; set both so
        # the test pins the behavior on every supported platform.
        monkeypatch.setenv("HOME", str(link))
        monkeypatch.setenv("USERPROFILE", str(link))
        self._clear()

        probe = str(link / ".aws" / "credentials")
        assert is_sensitive_path(probe) is True  # warms the cache

        link.unlink()
        link.symlink_to(real_b)  # repoint INSIDE the TTL window
        assert is_sensitive_path(probe) is True, "cached target set served a fail-open verdict"

    def test_roots_are_resolved_once_for_key_and_build(self, monkeypatch, tmp_path) -> None:
        """The key and the target set must come from ONE root resolution.

        Regression test for a fail-open TOCTOU: when the key resolved the roots
        and the builder resolved them again, a root symlink repointed between
        the two reads filed root B's targets under root A's key, so later
        requests under A got a false-negative verdict for up to the TTL.

        Rather than racing a real symlink, this asserts the structural property
        that makes the race impossible: exactly one resolution per cache fill,
        and the builder receives those captured roots.
        """
        from kiro_crew import security

        monkeypatch.setenv("HOME", str(tmp_path))
        self._clear()
        calls: list[tuple[str, str | None]] = []
        real_key = security._resolved_root_key

        def counting_key():
            r = real_key()
            calls.append(r)
            return r

        seen_roots: list[object] = []
        real_build = security._home_dir_targets_uncached

        def spy_build(home_dirs, roots=None):
            seen_roots.append(roots)
            return real_build(home_dirs, roots)

        monkeypatch.setattr(security, "_resolved_root_key", counting_key)
        monkeypatch.setattr(security, "_home_dir_targets_uncached", spy_build)
        security._home_dir_targets(security._SENSITIVE_HOME_DIRS)

        assert len(calls) == 1, f"roots resolved {len(calls)}x for one fill; must be 1"
        assert seen_roots == [calls[0]], "builder did not receive the captured roots"

    def test_expired_entry_is_rebuilt(self, monkeypatch, tmp_path) -> None:
        """Past the TTL the set is rebuilt, so filesystem changes are picked up."""
        from kiro_crew import security

        monkeypatch.setenv("HOME", str(tmp_path))
        self._clear()
        calls: list[int] = []
        real = security._home_dir_targets_uncached

        def counting(home_dirs, roots=None):
            calls.append(1)
            return real(home_dirs, roots)

        monkeypatch.setattr(security, "_home_dir_targets_uncached", counting)
        security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        # Expire the entry rather than sleeping the real TTL.
        for key, (_expiry, targets) in list(security._home_targets_cache.items()):
            security._home_targets_cache[key] = (0.0, targets)
        security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(calls) == 2

    def test_cache_dict_is_bounded(self, monkeypatch, tmp_path) -> None:
        """Churning the env key must not grow the cache without limit."""
        from kiro_crew import security

        monkeypatch.setenv("HOME", str(tmp_path))
        self._clear()

        # Stub the expensive uncached rebuild: 200 real rebuilds (~130 realpath
        # calls each) race the mc-pathres budget on a loaded Windows runner
        # (8 xdist workers + Defender), causing PathResolutionStalled to be
        # raised and -- because the stall is recorded with a cooldown against
        # roots.home -- every subsequent iteration in the loop refuses too.
        # The test's intent is only "the cache dict stays bounded"; it does not
        # need real filesystem rebuilds to verify that.
        def _cheap_uncached(home_dirs, roots=None):
            home = roots.home if roots is not None else ""
            return {home}

        monkeypatch.setattr(security, "_home_dir_targets_uncached", _cheap_uncached)
        for i in range(200):
            monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / f"h{i}"))
            security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(security._home_targets_cache) <= 33


class TestAdaptiveHomeTargetsExpiry:
    """The target cache's expiry tracks what the rebuild it expires COST.

    The rebuild is ~130 ``realpath`` calls and each syscall hands
    the GIL over, so its cost is set by CPU contention rather than by the disk:
    measured on a 64-core Linux host with one local xfs mount, 2ms on an idle
    interpreter and 581ms, 399ms, 3859ms, 7456ms with 1, 2, 4 and 8 sibling
    threads running pure Python. A FIXED 0.1s expiry does not move with that, so
    the share of the wall clock spent rebuilding climbs with load until the
    rebuild misses its budget and the gate refuses ordinary project files.

    ``_home_targets_ttl`` therefore returns
    ``clamp(cost * _HOME_TARGETS_TTL_COST_RATIO, _HOME_TARGETS_TTL_SECS,
    _HOME_TARGETS_TTL_MAX_SECS)``. Each of those three constants gets its own
    test here, plus the switch that turns the law off, plus the two end-to-end
    properties the law is FOR, plus the reproduced bypass at the longest expiry
    the law can select.

    The clock is frozen and advanced by the stub builder, so "the rebuild cost
    0.4s" is a fact of the test rather than a race against a real build.
    """

    @staticmethod
    def _clear() -> None:
        from kiro_crew import security

        security._home_targets_cache.clear()

    @staticmethod
    def _timed_rebuild(monkeypatch, clock: dict[str, float], cost: float) -> list[int]:
        """Replace the builder with one that "costs" *cost* frozen seconds."""
        from kiro_crew import security

        calls: list[int] = []
        real = security._home_dir_targets_uncached

        def slow(home_dirs, roots=None):
            calls.append(1)
            clock["now"] += cost
            return real(home_dirs, roots)

        monkeypatch.setattr(security, "_home_dir_targets_uncached", slow)
        return calls

    def test_an_idle_rebuild_keeps_the_shipped_expiry(self) -> None:
        """2ms * the ratio lands ON the floor, so an idle host is unchanged.

        This is what makes the ratio reviewable rather than arbitrary: it is the
        value at which the measured idle rebuild reproduces the expiry that
        shipped before the law existed. A ratio raised without moving the floor
        would change idle behaviour, and this catches that.
        """
        from kiro_crew import security

        idle_rebuild_secs = 0.002  # measured, see the class docstring
        assert security._home_targets_ttl(idle_rebuild_secs, resolution_differed=False) == (
            security._HOME_TARGETS_TTL_SECS
        )
        assert (
            idle_rebuild_secs * security._HOME_TARGETS_TTL_COST_RATIO
            <= security._HOME_TARGETS_TTL_SECS
        )

    def test_the_expiry_scales_with_the_measured_cost(self) -> None:
        """Between the floor and the cap the expiry is cost times the ratio."""
        from kiro_crew import security

        cost = 0.4  # measured under two contending threads
        assert security._home_targets_ttl(cost, resolution_differed=False) == pytest.approx(
            cost * security._HOME_TARGETS_TTL_COST_RATIO
        )
        # And the share of the wall clock spent rebuilding is the ratio's
        # reciprocal, which is the whole point of expressing it as a ratio.
        assert cost / security._home_targets_ttl(cost, resolution_differed=False) == pytest.approx(
            1.0 / security._HOME_TARGETS_TTL_COST_RATIO
        )

    def test_the_expiry_is_capped(self) -> None:
        """The cap binds, so the stale window has a stated ceiling.

        Without it a rebuild that succeeded just under its 8s budget would earn
        minutes of staleness, which is the trade this cap exists to bound.
        """
        from kiro_crew import security

        expensive = 7.5  # measured under eight contending threads
        assert expensive * security._HOME_TARGETS_TTL_COST_RATIO > (
            security._HOME_TARGETS_TTL_MAX_SECS
        ), "the cap must actually bind at the measured worst case, or it is decoration"
        assert security._home_targets_ttl(expensive, resolution_differed=False) == (
            security._HOME_TARGETS_TTL_MAX_SECS
        )

    def test_a_negative_or_zero_cost_yields_the_floor(self) -> None:
        """A coarse or frozen clock must not produce a zero-length expiry."""
        from kiro_crew import security

        assert security._home_targets_ttl(0.0, resolution_differed=False) == (
            security._HOME_TARGETS_TTL_SECS
        )
        assert security._home_targets_ttl(-1.0, resolution_differed=False) == (
            security._HOME_TARGETS_TTL_SECS
        )

    def test_a_zero_ratio_pins_the_expiry_at_the_floor(self, monkeypatch) -> None:
        """A ratio of 0 is the revert, and it is reachable from outside the package.

        The law trades freshness for availability, so the revert has to be
        operator-reachable rather than a source edit. It is the SAME knob as the
        ratio itself (``KIROCREW_PATH_GATE_TTL_COST_RATIO=0``), because zero times
        any cost clamps to the floor for every input -- so there is no second
        mechanism to keep in step with this one.
        """
        from kiro_crew import security

        assert security.paths._TTL_COST_RATIO_MIN == 0.0, "0 must be an accepted value"
        monkeypatch.setattr(security, "_HOME_TARGETS_TTL_COST_RATIO", 0.0)
        for cost in (0.0, 0.4, 7.5, 1000.0):
            assert security._home_targets_ttl(cost, resolution_differed=False) == (
                security._HOME_TARGETS_TTL_SECS
            )

    def test_a_contended_rebuild_outlives_the_fixed_expiry(self, monkeypatch, tmp_path) -> None:
        """END TO END: a costly build is not re-paid 0.1s later.

        This is the defect, expressed as a test. On the fixed expiry a build
        costing 0.4s was rebuilt again 0.1s later, so a contended gateway spent
        most of its wall clock rebuilding; here the same build is still served.
        """
        from kiro_crew import security

        monkeypatch.setenv("HOME", str(tmp_path))
        self._clear()
        clock = {"now": 1000.0}
        # flake-ok: pre-existing frozen stdlib monotonic scoped to this test by monkeypatch; moved verbatim and tracked for a de-flake pass
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing frozen monotonic scoped to this test by monkeypatch and moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(security.time, "monotonic", lambda: clock["now"])
        calls = self._timed_rebuild(monkeypatch, clock, cost=0.4)

        security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(calls) == 1

        # The jump that DID rebuild before this law. It must not now.
        clock["now"] += security._HOME_TARGETS_TTL_SECS + 0.01
        security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(calls) == 1, "a contended build was re-paid at the fixed expiry"

        # Past the expiry the cost actually earned, it rebuilds -- the cache is
        # still TTL-bounded, only the bound moved.
        clock["now"] += security._home_targets_ttl(0.4, resolution_differed=False)
        security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(calls) == 2

    def test_the_expiry_starts_after_the_rebuild(self, monkeypatch, tmp_path) -> None:
        """A build costing more than its own expiry is not expired on arrival.

        The expiry starts at the clock read taken AFTER the build, not at the one
        that missed the cache. The distinction is immaterial for a 2ms build and
        decisive for a 0.4s one: an expiry started before the build has already
        elapsed when the build returns, so
        the very next call rebuilds and the cache stops being a cache exactly
        under the load it exists for. Forced here with a ratio that makes the
        expiry shorter than the build, so the ordering is the only thing the
        assertion can be reading.
        """
        from kiro_crew import security

        monkeypatch.setenv("HOME", str(tmp_path))
        self._clear()
        monkeypatch.setattr(security, "_HOME_TARGETS_TTL_COST_RATIO", 0.5)
        clock = {"now": 1000.0}
        # flake-ok: pre-existing frozen stdlib monotonic scoped to this test by monkeypatch; moved verbatim and tracked for a de-flake pass
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing frozen monotonic scoped to this test by monkeypatch and moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(security.time, "monotonic", lambda: clock["now"])
        calls = self._timed_rebuild(monkeypatch, clock, cost=0.4)

        security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(calls) == 1
        assert security._home_targets_ttl(0.4, resolution_differed=False) == pytest.approx(
            0.2
        )  # shorter than the build

        # No clock advance at all: the entry must still be live on return.
        security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(calls) == 1, "the entry was expired the moment the build returned"

    def test_the_cap_cannot_be_raised_without_bound(self, monkeypatch) -> None:
        """The operator knob has its own ceiling, so the window stays bounded.

        The ceiling is what keeps ``_HOME_TARGETS_TTL_MAX_SECS`` a bound rather
        than a suggestion: without it an env var could widen the stale window
        above that test's guarantee, and nothing in the module would notice.
        """
        from kiro_crew import security

        monkeypatch.setenv(security._TTL_MAX_SECS_ENV, "99999")
        refused = security._env_float(
            security._TTL_MAX_SECS_ENV,
            security.paths._TTL_MAX_SECS_DEFAULT,
            security.paths._TTL_MAX_SECS_MIN,
            security.paths._TTL_MAX_SECS_MAX,
        )
        assert refused == security.paths._TTL_MAX_SECS_DEFAULT

        # And a value inside the range is honoured, or the knob is decoration.
        monkeypatch.setenv(security._TTL_MAX_SECS_ENV, "5")
        assert (
            security._env_float(
                security._TTL_MAX_SECS_ENV,
                security.paths._TTL_MAX_SECS_DEFAULT,
                security.paths._TTL_MAX_SECS_MIN,
                security.paths._TTL_MAX_SECS_MAX,
            )
            == 5.0
        )

        # The ceiling is the measurement, not generous headroom: the committed
        # sweep (``measure_path_gate_ttl.py ksweep``, hogs 0,1,2,4,8) found 54.4s
        # as the largest expiry a serveable load demanded, and the operator ceiling
        # stays at or above it so a serveable load can always be admitted. A value
        # at or below the measured max is honoured while one above it is refused
        # back to the reviewed default; a value in (measured max, old headroom] --
        # e.g. 120s, which the prior 300s ceiling admitted -- must now be refused.
        assert security.paths._TTL_MAX_SECS_MAX == pytest.approx(60.0)
        monkeypatch.setenv(security._TTL_MAX_SECS_ENV, "54.4")
        assert security._env_float(
            security._TTL_MAX_SECS_ENV,
            security.paths._TTL_MAX_SECS_DEFAULT,
            security.paths._TTL_MAX_SECS_MIN,
            security.paths._TTL_MAX_SECS_MAX,
        ) == pytest.approx(54.4)
        monkeypatch.setenv(security._TTL_MAX_SECS_ENV, "120")
        assert (
            security._env_float(
                security._TTL_MAX_SECS_ENV,
                security.paths._TTL_MAX_SECS_DEFAULT,
                security.paths._TTL_MAX_SECS_MIN,
                security.paths._TTL_MAX_SECS_MAX,
            )
            == security.paths._TTL_MAX_SECS_DEFAULT
        ), "a value above the measured maximum must fall back to the default"

    def test_documented_worst_case_age_is_derived_from_the_constants(self) -> None:
        """The ~42s worst case the comment/spec cite is computed, not asserted blind.

        Worst-case data age for a BOUNDED rebuild = the default expiry ceiling PLUS
        the rebuild's full deadline (its budget plus its capped grace), because the
        expiry clock starts only after the build returns. If any of those constants
        moves, the documented number must move with it -- this derives it so the two
        cannot drift apart.
        """
        from kiro_crew import security

        budget = security.paths._PATH_RESOLVE_REBUILD_TIMEOUT_SECS
        grace = min(
            budget * security.paths._PATH_RESOLVE_GRACE_FACTOR,
            security.paths._PATH_RESOLVE_GRACE_MAX_SECS,
        )
        assert budget == pytest.approx(8.0)
        assert grace == pytest.approx(4.0), "8s budget's grace is capped at 4s"
        bounded_deadline = budget + grace
        assert bounded_deadline == pytest.approx(12.0), "one bounded rebuild runs at most 12s"
        worst_case_age = security.paths._TTL_MAX_SECS_DEFAULT + bounded_deadline
        assert worst_case_age == pytest.approx(42.0), (
            "the documented worst-case age is 30s default expiry + 12s bounded "
            "rebuild deadline = ~42s; update the comment and spec if this changes"
        )

    def test_a_bad_knob_value_keeps_the_reviewed_default(self, monkeypatch) -> None:
        """Absent, unparseable and out-of-range all fall back to the default.

        Fail-soft to the DEFAULT is the conservative direction for both knobs:
        the shipped ratio and ceiling are the reviewed values, so a typo can only
        leave reviewed behaviour in place and never widen the stale window.
        """
        from kiro_crew import security

        for raw in ("", "   ", "abc", "-5", "1e9"):
            monkeypatch.setenv(security._TTL_COST_RATIO_ENV, raw)
            assert (
                security._env_float(
                    security._TTL_COST_RATIO_ENV,
                    security.paths._TTL_COST_RATIO_DEFAULT,
                    security.paths._TTL_COST_RATIO_MIN,
                    security.paths._TTL_COST_RATIO_MAX,
                )
                == security.paths._TTL_COST_RATIO_DEFAULT
            ), raw

    def test_the_shipped_defaults_leave_an_idle_host_identical(self) -> None:
        """The shipped defaults make an idle rebuild select exactly today's 0.1s.

        This is the requirement the defaults exist to meet: an unloaded host must
        show no observable change, and it holds by arithmetic (2ms * 50 = 0.1s)
        rather than by being close enough.

        Asserted on the DEFAULT constants, not on the live values, so the property
        is pinned for every host rather than only for one whose environment
        happens to set no override. The live values are checked against the
        defaults separately, and only when no override is present.
        """
        from kiro_crew import security

        idle_rebuild_secs = 0.002  # measured, see the class docstring
        assert (
            idle_rebuild_secs * security.paths._TTL_COST_RATIO_DEFAULT
            == security._HOME_TARGETS_TTL_SECS
        ), "the default ratio must land an idle rebuild exactly on the floor"
        assert security.paths._TTL_MAX_SECS_DEFAULT > security._HOME_TARGETS_TTL_SECS

        if not os.environ.get(security._TTL_COST_RATIO_ENV):
            assert security._HOME_TARGETS_TTL_COST_RATIO == (security.paths._TTL_COST_RATIO_DEFAULT)
        if not os.environ.get(security._TTL_MAX_SECS_ENV):
            assert security._HOME_TARGETS_TTL_MAX_SECS == security.paths._TTL_MAX_SECS_DEFAULT

    def test_the_measurement_script_only_names_internals_that_exist(self) -> None:
        """The committed measurement script must not rot silently.

        ``scripts/measure_path_gate_ttl.py`` is the reproducible evidence for the
        constants above, so it reaches into private names in this module -- and
        nothing in CI runs it, because a contention sweep takes minutes. Without
        this test a rename here leaves a script that still looks authoritative and
        dies on an AttributeError the first time anyone re-derives the numbers.

        The list is READ OUT OF THE SCRIPT, not restated here. A hand-maintained
        copy would make every rename cost two edits and would drift from the
        script the moment one of them was forgotten -- so the script stays the
        single place its own dependencies are written down, and this test just
        asks whether each one still resolves.

        Asserts presence, not behaviour: it is a spelling contract between the
        script and the module, which is exactly the part a rename breaks.
        """
        import pathlib
        import re

        from kiro_crew.security import paths

        script = pathlib.Path(paths.__file__).parents[3] / "scripts" / "measure_path_gate_ttl.py"
        assert script.is_file(), f"the measurement script is missing at {script}"
        names = sorted(set(re.findall(r"\bgate\.(_[A-Za-z0-9_]+)", script.read_text("utf-8"))))
        assert names, "found no gate.<private> references; the regex or the script moved"
        missing = [name for name in names if not hasattr(paths, name)]
        assert not missing, (
            f"{script.name} reads {missing} from kiro_crew.security.paths, which no "
            "longer exist; update the script in the same change as the rename"
        )

    def test_a_symlinked_keystone_leaf_pins_the_expiry_at_the_floor(
        self, monkeypatch, tmp_path
    ) -> None:
        """The reachable stale-credential path is closed, not merely bounded.

        A keystone leaf such as ``security_policy.json`` is not an anchor, so it
        is not in the cache key; repoint it and the stale set answers with the
        old target until the entry expires. Under the adaptive expiry alone that
        window grows with load, which makes an ordinary contended host the
        reachable case.

        The build reports whether it traversed a symlink, and that pins the
        expiry to the floor. So the very install where a repoint can strand a
        resolution-derived target is the install that keeps the 0.1s window it
        has today, while a host with no symlinked leaf keeps the long expiry. The
        cost of knowing is one pass over a dict the build already filled.

        Asserted end to end rather than on the flag alone: the ratio is forced
        large enough that a long expiry would be selected if the symlink were
        ignored, so this fails loudly if the fact stops reaching the law.
        """
        from kiro_crew import security

        crew_home = tmp_path / "crew"
        crew_home.mkdir()
        real_a = tmp_path / "vol-a" / "signing.key"
        real_b = tmp_path / "vol-b" / "signing.key"
        real_a.parent.mkdir(parents=True)
        real_b.parent.mkdir(parents=True)
        real_a.write_text("a", encoding="utf-8")
        real_b.write_text("b", encoding="utf-8")
        leaf = crew_home / "token_signing.key"
        # No local skip guard: this test's exact node id is listed in
        # test/requires-real-symlinks.txt, so the root conftest skips it only when a
        # runtime probe says the platform cannot create one. An elevated or
        # Developer-Mode Windows runner therefore keeps the coverage.
        leaf.symlink_to(real_a)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        self._clear()
        monkeypatch.setattr(security, "_HOME_TARGETS_TTL_COST_RATIO", 1_000_000.0)
        clock = {"now": 1000.0}
        # flake-ok: pre-existing frozen stdlib monotonic scoped to this test by monkeypatch; moved verbatim and tracked for a de-flake pass
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing frozen monotonic scoped to this test by monkeypatch and moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(security.time, "monotonic", lambda: clock["now"])
        calls = self._timed_rebuild(monkeypatch, clock, cost=0.4)

        target_a = str(real_a.resolve()).casefold()
        target_b = str(real_b.resolve()).casefold()
        warm = security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert target_a in warm
        assert len(calls) == 1
        assert warm.resolution_differed is True, "a symlinked leaf must be reported"

        # The floor, not the cap, despite a ratio that would ask for 400000s.
        assert security._home_targets_ttl(0.4, resolution_differed=True) == (
            security._HOME_TARGETS_TTL_SECS
        )

        leaf.unlink()
        leaf.symlink_to(real_b)

        # One floor's worth of clock, not one cap's worth, and the repoint is live.
        clock["now"] += security._HOME_TARGETS_TTL_SECS + 0.01
        fresh = security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(calls) == 2, "the floor did not expire the entry"
        assert target_b in fresh, "the rebuilt set must carry the new target"

    def test_an_install_with_no_symlinked_leaf_keeps_the_long_expiry(
        self, monkeypatch, tmp_path
    ) -> None:
        """The other half: without a symlink there is nothing a repoint can stale.

        This is what keeps the fix above from being a blanket revert. When none of
        the paths the build RESOLVED came back spelled differently, the set holds
        no resolution-derived entry, so reaching the stale-credential case requires
        first CREATING a symlink inside the crew home -- a write
        ``is_sensitive_write_path`` refuses. The adaptive expiry therefore applies
        in full, which is the availability this PR is for.

        The population that can report a difference is the narrow one
        ``_BuiltTargets`` enumerates, not the built target set. This test's
        environment sets ``KIROCREW_HOME`` here and inherits ``XDG_CONFIG_HOME``
        from the root ``conftest``, but no declared credential leaf under either
        root is a symlink, so nothing reports a difference. Note what that does NOT
        say: a
        symlinked ``goose`` directory UNDER the exported ``XDG_CONFIG_HOME`` would
        report one, because that leaf is declared. Under this suite's root
        ``conftest`` that variable points at a tmp dir outside ``HOME``, so
        ``~/.config/goose`` is not the path any build resolves here.
        """
        from kiro_crew import security

        crew_home = tmp_path / "crew"
        crew_home.mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        self._clear()
        clock = {"now": 1000.0}
        # flake-ok: pre-existing frozen stdlib monotonic scoped to this test by monkeypatch; moved verbatim and tracked for a de-flake pass
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing frozen monotonic scoped to this test by monkeypatch and moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(security.time, "monotonic", lambda: clock["now"])
        calls = self._timed_rebuild(monkeypatch, clock, cost=0.4)

        warm = security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(calls) == 1
        assert warm.resolution_differed is False, "no symlink, so nothing to report"

        # The jump that expires a floor-pinned entry must NOT expire this one.
        clock["now"] += security._HOME_TARGETS_TTL_SECS + 0.01
        security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(calls) == 1, "a symlink-free build must earn the long expiry"

        # And it is still TTL-bounded: past what the cost earned, it rebuilds.
        clock["now"] += security._home_targets_ttl(0.4, resolution_differed=False)
        security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(calls) == 2

    def test_only_the_enumerated_classes_of_path_are_resolved_by_a_build(
        self, monkeypatch, tmp_path
    ) -> None:
        """Pin the resolve memo's population, which five prose sites enumerate.

        ``resolution_differed`` is read off the memo the build fills, so WHICH
        paths reach that memo is the flag's whole scope -- and the ``differed``
        comment, the :class:`_BuiltTargets`, :func:`_home_targets_ttl` and
        :func:`_report_expiry_pin` docstrings and the security spec all state that
        scope as a closed list. That list of five is the one the failure message
        below names, so the two stay in step.
        Prose cannot hold a closed list shut. A future ``resolve_target`` call on a
        seventh kind of path would widen what the flag reports while all five sites
        went on describing the old set, which is exactly the documentation defect
        this change exists to remove, reintroduced one call site later.

        The expectation is DERIVED from the same constants the build reads rather
        than spelled out here, so adding a sensitive LEAF keeps this passing and
        adding a resolve CALL SITE fails it.
        """
        from kiro_crew.security import paths as gate

        crew_home = tmp_path / "crew"
        crew_home.mkdir()
        kiro_home = tmp_path / "kiro"
        kiro_home.mkdir()
        os_home = tmp_path / "oshome"
        os_home.mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        monkeypatch.setenv("KIRO_HOME", str(kiro_home))
        monkeypatch.setenv("KIROCREW_OS_HOME", str(os_home))
        # Set EVERY harness override root this test asserts on, rather than
        # inheriting one from an unrelated autouse fixture. The override-anchored
        # arm only contributes where its root is set, so leaving that to a fixture
        # written for another purpose makes the last class's coverage depend on a
        # variable no reader of this test can see -- and the assertion at the end
        # would then pass or go vacuous for reasons outside this file.
        for _override_env in gate._ADAPTER_ROOT_ENVS:
            _override_root = tmp_path / f"override-{_override_env.lower()}"
            _override_root.mkdir(exist_ok=True)
            monkeypatch.setenv(_override_env, str(_override_root))
        self._clear()

        # Resolve the roots BEFORE recording and pass them in, so the recorder
        # sees the BUILD's own resolutions and not the root anchoring ahead of it.
        roots = gate._resolved_root_key()
        # The three lists the three callers actually hand the builder: the read
        # gate passes ``_SENSITIVE_HOME_DIRS``, ``is_sensitive_write_path`` passes
        # that plus ``_WRITE_PROTECTED_HOME_PATHS``, and
        # ``_is_keystone_publish_artifact`` passes ``_KEYSTONE_ARTIFACT_PARENTS``.
        # Both lists carrying ``_SENSITIVE_HOME_DIRS`` admit every
        # ``_OVERRIDE_ANCHORED_LEAVES`` member, because those leaves are spliced
        # into it; ``_WRITE_PROTECTED_HOME_PATHS`` on its own and
        # ``_KEYSTONE_ARTIFACT_PARENTS`` each filter all of them out of BOTH the
        # recorder and ``expected``, which is how the last resolved class can come
        # to be asserted vacuously.
        tiers = (
            gate._SENSITIVE_HOME_DIRS,
            gate._SENSITIVE_HOME_DIRS + gate._WRITE_PROTECTED_HOME_PATHS,
            gate._KEYSTONE_ARTIFACT_PARENTS,
        )
        adapter_roots = dict(roots.adapter_roots)
        real = gate._realpaths_or_none
        override_anchored_seen = 0

        for tier in tiers:
            self._clear()
            asked: list[str] = []

            # The build resolves its anchors as ONE batched child request, so the
            # recorder sits on the batch seam; the population it asks for is the same.
            def recording(paths: list[str], _sink=asked) -> list[str | None]:
                _sink.extend(paths)
                return real(paths)

            monkeypatch.setattr(gate, "_realpaths_or_none", recording)
            gate._home_dir_targets_uncached(tier, roots)

            expected = {roots.home}
            if roots.os_home:
                expected.add(roots.os_home)
            if roots.crew_home:
                for entry in tier:
                    for prefix in gate._CREW_HOME_PREFIXES:
                        if entry == prefix or entry.startswith(prefix + "/"):
                            leaf = entry[len(prefix) :].lstrip("/")
                            expected.add(
                                os.path.join(roots.crew_home, *gate._leaf_segments(leaf))
                                if leaf
                                else roots.crew_home
                            )
                            break
            # Every entry has its ``$HOME``-rooted form resolved, so a symlink at or
            # below a protected entry cannot move the real store out of the fence.
            for entry in tier:
                expected.add(os.path.join(roots.home, *gate._leaf_segments(entry)))
            if roots.kiro_home and gate._KIRO_AGENTS_DIR in tier:
                expected.add(os.path.join(roots.kiro_home, "agents"))
            if roots.kiro_home and gate._KIRO_MATE_AGENTS_DIR in tier:
                expected.add(os.path.join(roots.kiro_home, "mates", "agents"))
            for mate_leaf in gate._KIRO_MATE_STATE_LEAVES:
                if roots.kiro_home and mate_leaf in tier:
                    expected.add(os.path.join(roots.kiro_home, *gate._leaf_segments(mate_leaf)[1:]))
            # The registry leaf shares that class: one root, one guard shape. Derived
            # from the same constant the build reads, so it tracks a moved leaf.
            if roots.kiro_home and gate._KIRO_SETTINGS_MCP_JSON in tier:
                expected.add(
                    os.path.join(roots.kiro_home, *gate._leaf_segments("settings/mcp.json"))
                )
            for leaf, root_envs, under_root in gate._OVERRIDE_ANCHORED_LEAVES:
                if leaf not in tier:
                    continue
                for env_name in root_envs:
                    root = adapter_roots.get(env_name)
                    if root:
                        expected.add(os.path.join(root, *gate._leaf_segments(under_root)))
                        override_anchored_seen += 1

            assert set(asked) == expected, (
                "this build resolved a path outside the classes the prose enumerates, "
                "so the flag's scope changed: update the differed comment, "
                "_BuiltTargets, _home_targets_ttl, _report_expiry_pin and the "
                "security spec together"
            )

        # The last class has to be REACHED, not merely described. Without this the
        # override-anchored arm can contribute nothing to either side -- which is
        # what ``_WRITE_PROTECTED_HOME_PATHS`` alone does -- and a widened flag
        # would still pass while the prose about declared credential leaves went
        # unasserted.
        assert override_anchored_seen, (
            "no declared credential leaf was re-anchored under a harness home "
            "override, so the resolved class the prose rests on is unasserted: "
            "check that a harness override variable is set in this environment"
        )

    def test_a_builder_that_reports_nothing_gets_the_floor(self, monkeypatch, tmp_path) -> None:
        """An unknown build is treated as having traversed a symlink.

        The fact travels as an attribute on the returned set, so anything that
        returns a plain ``set`` -- a test double, an embedder's override, a future
        builder that forgets -- carries no answer at all. Both defaults that cover
        that case are asserted here, because each is a separate line: the class
        attribute for a set built without it, and the cache's own read for a set
        that is not a :class:`_BuiltTargets` at all. Either defaulting the other
        way would hand the long expiry to a build nobody vouched for.
        """
        from kiro_crew import security

        assert security._BuiltTargets({"x"}).resolution_differed is True

        crew_home = tmp_path / "crew"
        crew_home.mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        self._clear()
        monkeypatch.setattr(security, "_HOME_TARGETS_TTL_COST_RATIO", 1_000_000.0)
        clock = {"now": 1000.0}
        # flake-ok: pre-existing frozen stdlib monotonic scoped to this test by monkeypatch; moved verbatim and tracked for a de-flake pass
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing frozen monotonic scoped to this test by monkeypatch and moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(security.time, "monotonic", lambda: clock["now"])

        calls: list[int] = []

        def plain(home_dirs, roots=None):  # returns a bare set, not _BuiltTargets
            calls.append(1)
            clock["now"] += 0.4
            return {str(crew_home / "token_signing.key").casefold()}

        monkeypatch.setattr(security, "_home_dir_targets_uncached", plain)

        security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(calls) == 1
        clock["now"] += security._HOME_TARGETS_TTL_SECS + 0.01
        security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(calls) == 2, "an unvouched build must expire at the floor"

    def test_a_reported_symlink_pins_the_floor_on_every_platform(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        """The law itself, asserted without creating a symlink.

        Its symlinked-leaf sibling has to build a real symlink, so it SKIPS on a
        platform that will not make one, and that would leave the security half of
        this change unasserted exactly there. This one reports the flag from a
        double instead, so the pin runs everywhere: a build that says it traversed
        a symlink gets the floor even under a ratio that would ask for days.
        """
        from kiro_crew import security

        crew_home = tmp_path / "crew"
        crew_home.mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        self._clear()
        monkeypatch.setattr(security, "_HOME_TARGETS_TTL_COST_RATIO", 1_000_000.0)
        clock = {"now": 1000.0}
        # flake-ok: pre-existing frozen stdlib monotonic scoped to this test by monkeypatch; moved verbatim and tracked for a de-flake pass
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing frozen monotonic scoped to this test by monkeypatch and moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(security.time, "monotonic", lambda: clock["now"])

        calls: list[int] = []
        leaf = str(crew_home / "token_signing.key").casefold()

        def reporting(home_dirs, roots=None):
            calls.append(1)
            clock["now"] += 0.4
            built = security._BuiltTargets({leaf})
            built.resolution_differed = True
            return built

        monkeypatch.setattr(security, "_home_dir_targets_uncached", reporting)

        from kiro_crew.security import paths as gate

        gate._home_targets_pin_state.clear()
        with caplog.at_level(logging.INFO, logger=gate.__name__):
            security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        gate._home_targets_pin_state.clear()
        assert len(calls) == 1
        assert any(
            "pinned to the" in record.getMessage() for record in caplog.records
        ), "the cache fill must report the pin, not only the helper"
        clock["now"] += security._HOME_TARGETS_TTL_SECS + 0.01
        security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(calls) == 2, "a reported symlink must expire at the floor"

    def test_the_pin_is_reported_once_per_transition(self, caplog) -> None:
        """The diagnostic exists so the fix cannot self-disable in silence.

        One line per TRANSITION of the shared state, not per rebuild: an install
        whose resolved leaf under a home-override root is a symlink pins the floor
        on every build that was handed that leaf, and a per-build line would be
        noise that gets filtered, which is the same as having none.
        """
        from kiro_crew import security
        from kiro_crew.security import paths as gate

        read_dirs = security._SENSITIVE_HOME_DIRS
        gate._home_targets_pin_state.clear()
        with caplog.at_level(logging.INFO, logger=gate.__name__):
            gate._report_expiry_pin(read_dirs, True)
            gate._report_expiry_pin(read_dirs, True)
            gate._report_expiry_pin(read_dirs, True)
            gate._report_expiry_pin(read_dirs, False)
        lines = [record.getMessage() for record in caplog.records]
        gate._home_targets_pin_state.clear()

        assert len(lines) == 2, "a repeated state must not be re-reported"
        assert "pinned to the" in lines[0]
        assert "symlink" in lines[0]
        assert "cost-tracking expiry in force" in lines[1]
        # Both halves name the tier, so a reader diagnosing a refusal knows WHICH
        # gate the line is about rather than reading it as the whole gate's state.
        assert all("(read tier)" in line for line in lines), lines

    def test_each_tier_dedups_on_its_own_state_not_one_shared_slot(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        """A host whose builds disagree gets one line per tier, not an alternation.

        The gates hand the builder different lists, so the flag can come back
        differently for each: a symlinked write-protected crew leaf that holds no
        secret is in the write gate's list and in neither of the others, which pins
        the write tier while the keystone-artifact build reports no traversal and
        selects the cost-tracking expiry. Keyed on one shared slot, each build flips
        the key the other just set, so both messages repeat for as long as the
        disagreement lasts and the "in force" half asserts the adaptive expiry
        applies on a host where a gate IS pinned to the floor -- the exact wrong
        conclusion for whoever is reading the log to find out why. Keyed per list,
        each build transitions its own state and says it once.

        Driven through :func:`_home_dir_targets` rather than the reporter, so the
        wiring that passes the list is pinned and not only the helper.
        """
        from kiro_crew import security
        from kiro_crew.security import paths as gate

        crew_home = tmp_path / "crew"
        crew_home.mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        self._clear()
        gate._home_targets_pin_state.clear()
        clock = {"now": 1000.0}
        # flake-ok: pre-existing frozen stdlib monotonic scoped to this test by monkeypatch; moved verbatim and tracked for a de-flake pass
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing frozen monotonic scoped to this test by monkeypatch and moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(security.time, "monotonic", lambda: clock["now"])

        write_dirs = security._SENSITIVE_HOME_DIRS + security._WRITE_PROTECTED_HOME_PATHS
        keystone_dirs = security._KEYSTONE_ARTIFACT_PARENTS

        def disagreeing(home_dirs, roots=None):
            """Report a traversal for the write list alone, the shape of the defect."""
            clock["now"] += 0.4
            built = security._BuiltTargets({str(crew_home / "sessions").casefold()})
            built.resolution_differed = home_dirs == write_dirs
            return built

        monkeypatch.setattr(security, "_home_dir_targets_uncached", disagreeing)

        with caplog.at_level(logging.INFO, logger=gate.__name__):
            for _ in range(3):
                security._home_dir_targets(write_dirs)
                security._home_dir_targets(keystone_dirs)
                # Expire both entries, so the next pass is a fresh build for each
                # tier and reports again rather than being served from the cache.
                clock["now"] += security._HOME_TARGETS_TTL_MAX_SECS + 1.0
        lines = [r.getMessage() for r in caplog.records if "anchor cache" in r.getMessage()]
        gate._home_targets_pin_state.clear()

        assert len(lines) == 2, f"each tier reports once across the three passes: {lines}"
        pinned = [line for line in lines if "pinned to the" in line]
        adaptive = [line for line in lines if "cost-tracking expiry in force" in line]
        assert len(pinned) == 1 and "(write tier)" in pinned[0], lines
        assert len(adaptive) == 1 and "(keystone-artifact tier)" in adaptive[0], lines

    def test_every_gate_list_handed_to_the_builder_has_a_tier_name(self) -> None:
        """A new gate must not reach an operator's log as an ``unnamed`` tier.

        :func:`_home_targets_tier` names the lists it knows and answers generically
        for one it does not, which keeps the dedup correct either way -- so nothing
        at runtime notices a gate that arrives with no name, and the generic answer
        would sit in the log unchallenged. This is what notices: every list a call
        site in the module hands the builder is resolved against the module and must
        come back named. The expectation is derived from the call sites rather than
        restated, so it cannot agree with a stale copy of them.
        """
        import ast

        from kiro_crew.security import paths as gate

        tree = ast.parse(Path(gate.__file__).read_text(encoding="utf-8"))

        def resolve(expr: ast.expr) -> list[str] | None:
            """The module list this expression denotes, or None if it is not one."""
            if isinstance(expr, ast.Name):
                value = getattr(gate, expr.id, None)
                return value if isinstance(value, list) else None
            if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
                left, right = resolve(expr.left), resolve(expr.right)
                return None if left is None or right is None else left + right
            return None

        # Where the list sits in each signature: first for the builder, second for
        # the matcher that forwards to it.
        positions = {"_home_dir_targets": 0, "_path_in_home_dirs": 1}
        named: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            callee = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            index = positions.get(callee)
            if index is None or len(node.args) <= index:
                continue
            # A call that forwards its own parameter resolves to nothing here; the
            # list it received is pinned at whichever call site supplied it.
            listed = resolve(node.args[index])
            if listed is None:
                continue
            tier = gate._home_targets_tier(listed)
            assert not tier.startswith(
                "unnamed"
            ), f"a gate hands the builder an unnamed list: {ast.unparse(node.args[index])}"
            named.append(tier)

        assert set(named) == {
            "read",
            "write",
            "keystone-artifact",
        }, f"the scan must reach every gate, found {sorted(set(named))}"

    def test_repointed_home_symlink_is_not_served_from_cache_at_the_longest_expiry(
        self, monkeypatch, tmp_path
    ) -> None:
        """The reproduced bypass stays shut at the CAP, not only at the floor.

        Companion to ``TestHomeDirTargetsCache
        .test_repointed_home_symlink_is_not_served_from_cache``. That one runs at
        the floor, which is not the expiry a contended host selects. The
        reason it holds at any expiry is structural rather than temporal: every
        anchor is part of the cache KEY, so repointing ``$HOME`` re-keys and
        misses. Forced here by a ratio large enough that every build clamps to
        ``_HOME_TARGETS_TTL_MAX_SECS``, with the clock frozen so nothing can
        expire during the test.
        """
        from kiro_crew import security

        real_a = tmp_path / "vol1" / "u"
        real_b = tmp_path / "vol2" / "u"
        real_a.mkdir(parents=True)
        real_b.mkdir(parents=True)
        link = tmp_path / "home"
        # Listed in test/requires-real-symlinks.txt rather than skipped here, so the
        # capability probe owns the decision and a Windows runner that CAN make a
        # symlink still asserts the bypass stays shut at the cap.
        link.symlink_to(real_a)
        monkeypatch.setenv("HOME", str(link))
        monkeypatch.setenv("USERPROFILE", str(link))
        self._clear()
        monkeypatch.setattr(security, "_HOME_TARGETS_TTL_COST_RATIO", 1_000_000.0)
        clock = {"now": 1000.0}
        # flake-ok: pre-existing frozen stdlib monotonic scoped to this test by monkeypatch; moved verbatim and tracked for a de-flake pass
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing frozen monotonic scoped to this test by monkeypatch and moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(security.time, "monotonic", lambda: clock["now"])
        calls = self._timed_rebuild(monkeypatch, clock, cost=0.4)

        probe = str(link / ".aws" / "credentials")
        assert is_sensitive_path(probe) is True  # warms the cache
        assert len(calls) == 1

        # The expiry in force here really is the cap, and that is not an
        # assumption: a symlinked ``$HOME`` is a ROOT, which
        # ``_resolve_root_anchors`` already canonicalised, so this build resolved
        # nothing differently and earns the long expiry. Read the flag off the
        # built set rather than passing a literal, so the day a root DOES start
        # tripping it this assertion fails instead of quietly testing the floor.
        built = security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        assert len(calls) == 1, "re-reading the warmed cache must not rebuild"
        differed = getattr(built, "resolution_differed", True)
        assert differed is False, "a symlinked $HOME alone must not pin the floor"
        assert (
            security._home_targets_ttl(0.4, resolution_differed=differed)
            == security._HOME_TARGETS_TTL_MAX_SECS
        )

        link.unlink()
        link.symlink_to(real_b)  # repointed well INSIDE the longest expiry
        assert is_sensitive_path(probe) is True, "cached target set served a fail-open verdict"
        assert len(calls) == 2, "the repoint must MISS the cache, not be served stale"
