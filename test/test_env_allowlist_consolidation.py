"""The shared env-allowlist membership convention: ``platform_compat.env_key_allowed``.

Five subprocess env allowlists delegate their matching to this one predicate —
``apps.registry._is_safe_env_key``, ``kiro_prerequisite._allowlisted_env``,
dev_fleet's ``_is_safe_env_key``, the auto-improvement github_repo profile's
measurement passthrough, and the source-provider CLI env filter. Each keeps its
OWN allowlist (they are deliberately different trust boundaries); only the
matching convention is shared: exact on POSIX, case-folded on Windows, because
CPython's ``os.environ`` upper-cases every key there and a literal test against
Microsoft's documented mixed-case spelling (``SystemRoot``) silently drops the
variable the allowlist is meant to carry.

These tests pin two things:
1. the convention itself, under a simulated Windows AND a simulated POSIX; and
2. per-site parity — each call site's admitted key set equals what its own
   private matching logic admits, so the consolidation is behavior-preserving.
   The wrapper sites (registry, kiro_prerequisite, dev_fleet) carry the
   fold-on-Windows oracle; the two sites whose allowlists are written
   upper-case-exact (github_repo profile, source_providers) carry the exact
   oracle on BOTH platforms, evaluated against the upper-cased key spelling a
   real Windows ``os.environ`` yields — which is exactly why the shared fold
   admits the same set there.
"""

from __future__ import annotations

import pytest

from kiro_crew import kiro_prerequisite, platform_compat
from kiro_crew.apps import registry
from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import profile as gh_profile
from kiro_crew.apps.builtins.dev_fleet import server as dev_fleet_server
from kiro_crew.dashboard.handlers import source_providers


def _oracle_admitted(
    environ: dict[str, str], allowed, *, windows: bool, folds_on_windows: bool
) -> set[str]:
    """A call site's own matching logic, re-stated as the parity oracle.

    Sites that fold carry ``folds_on_windows=True`` (upper-case fold on the
    Windows arm); sites whose allowlists are written upper-case-exact carry
    ``False`` (exact membership on both arms). Kept independent of the shared
    helper so the tests compare it against each site's own convention, never
    against itself.
    """
    if windows and folds_on_windows:
        folded = {name.upper() for name in allowed}
        return {key for key in environ if key.upper() in folded}
    return {key for key in environ if key in allowed}


# POSIX-shaped synthetic environ: mixed-case lookalikes are possible there and
# must be told apart, and credential-bearing names must never be admitted.
_POSIX_ENVIRON = {
    "PATH": "/usr/bin",
    "Path": "/sneaky-posix-lookalike",
    "SystemRoot": r"C:\Windows",
    "ComSpec": r"C:\Windows\System32\cmd.exe",
    "HOME": "/home/u",
    "LANG": "C.UTF-8",
    "TEMP": "/tmp",
    "GITHUB_TOKEN": "ghp_secret",
    "AWS_SECRET_ACCESS_KEY": "secret",
    "SLACK_BOT_TOKEN": "xoxb-secret",
    "SSH_AUTH_SOCK": "/tmp/agent.sock",
}

# Windows-shaped synthetic environ: CPython's os.environ upper-cases every key
# there, so this is the only spelling a filter ever sees at runtime.
_WINDOWS_ENVIRON = {
    "PATH": r"C:\Windows\System32",
    "SYSTEMROOT": r"C:\Windows",
    "COMSPEC": r"C:\Windows\System32\cmd.exe",
    "USERPROFILE": r"C:\Users\me",
    "APPDATA": r"C:\Users\me\AppData\Roaming",
    "TEMP": r"C:\Temp",
    "HOME": r"C:\Users\me",
    "LANG": "C.UTF-8",
    "GITHUB_TOKEN": "ghp_secret",
    "AWS_SECRET_ACCESS_KEY": "secret",
    "SLACK_BOT_TOKEN": "xoxb-secret",
    "SSH_AUTH_SOCK": "/tmp/agent.sock",
}


def _environ_for(windows: bool) -> dict[str, str]:
    return _WINDOWS_ENVIRON if windows else _POSIX_ENVIRON


class TestEnvKeyAllowedConvention:
    """The predicate itself, on both simulated platforms."""

    def test_windows_matches_the_spelling_os_environ_yields(self, monkeypatch) -> None:
        """A mixed-case allowlist entry must match the upper-cased runtime key.

        ``os.environ`` upper-cases every key on Windows, so the filter sees
        ``SYSTEMROOT`` while the allowlist writes Microsoft's documented
        ``SystemRoot``. Folding is what keeps the two ends agreeing.
        """
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        allowed = frozenset({"SystemRoot", "PATH"})
        assert platform_compat.env_key_allowed("SYSTEMROOT", allowed)
        assert platform_compat.env_key_allowed("SystemRoot", allowed)
        assert platform_compat.env_key_allowed("systemroot", allowed)
        assert not platform_compat.env_key_allowed("SLACK_BOT_TOKEN", allowed)

    def test_windows_fold_widens_case_never_the_key_set(self, monkeypatch) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        allowed = frozenset({"SystemRoot", "PATH", "TEMP"})
        for secret in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK"):
            assert not platform_compat.env_key_allowed(secret, allowed)

    def test_posix_stays_exact(self, monkeypatch) -> None:
        """``PATH`` and ``Path`` are genuinely different variables on POSIX.

        A case-insensitive match there would let a lookalike through, so the
        fold is Windows-only.
        """
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        allowed = frozenset({"SystemRoot", "PATH"})
        assert platform_compat.env_key_allowed("PATH", allowed)
        assert not platform_compat.env_key_allowed("Path", allowed)
        assert platform_compat.env_key_allowed("SystemRoot", allowed)
        assert not platform_compat.env_key_allowed("SYSTEMROOT", allowed)

    def test_accepts_tuple_and_frozenset_allowlists(self, monkeypatch) -> None:
        """Call sites keep their existing constant shapes (dev_fleet uses a tuple)."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        assert platform_compat.env_key_allowed("SYSTEMROOT", ("SystemRoot",))
        assert platform_compat.env_key_allowed("SYSTEMROOT", frozenset({"SystemRoot"}))


@pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
class TestCallSiteParity:
    """Per-site parity: each site admits exactly what its own oracle admits.

    The environ is platform-shaped (upper-cased keys on the Windows arm, the
    spelling a real ``os.environ`` yields there), so the exact-oracle sites'
    equality is a real claim about runtime behavior, not folding compared to
    itself.
    """

    def test_apps_registry(self, monkeypatch, windows: bool) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        environ = _environ_for(windows)
        admitted = {k for k in environ if registry._is_safe_env_key(k)}
        assert admitted == _oracle_admitted(
            environ, registry._SAFE_ENV_KEYS, windows=windows, folds_on_windows=True
        )

    def test_kiro_prerequisite_probe_allowlist(self, monkeypatch, windows: bool) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        environ = _environ_for(windows)
        filtered = kiro_prerequisite._allowlisted_env(
            dict(environ), kiro_prerequisite._PROBE_ENV_KEYS
        )
        assert set(filtered) == _oracle_admitted(
            environ,
            kiro_prerequisite._PROBE_ENV_KEYS,
            windows=windows,
            folds_on_windows=True,
        )

    def test_dev_fleet(self, monkeypatch, windows: bool) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        environ = _environ_for(windows)
        admitted = {k for k in environ if dev_fleet_server._is_safe_env_key(k)}
        assert admitted == _oracle_admitted(
            environ,
            dev_fleet_server._SAFE_ENV_KEYS,
            windows=windows,
            folds_on_windows=True,
        )

    def test_github_repo_profile_measurement_passthrough(
        self, monkeypatch, tmp_path, windows: bool
    ) -> None:
        """The passthrough allowlist is upper-case-exact, so its oracle is exact
        membership on both platforms — the shared fold must admit the same set."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        environ = _environ_for(windows)
        monkeypatch.setattr(gh_profile.os, "environ", dict(environ))
        env = gh_profile._measure_env(tmp_path)
        # _measure_env layers pinned measurement variables and PYTHONPATH on top
        # of the passthrough; only the keys sourced from the environ are the
        # allowlist's admitted set.
        admitted = set(env) & set(environ)
        assert admitted == _oracle_admitted(
            environ,
            gh_profile._MEASURE_ENV_PASSTHROUGH,
            windows=windows,
            folds_on_windows=False,
        )

    def test_source_providers_base_allowlist(self, monkeypatch, windows: bool) -> None:
        """The provider allowlist is upper-case-exact (lower-case proxy twins are
        listed explicitly), so its oracle is exact membership on both platforms.
        The provider CLI path is POSIX-only at runtime (Windows raises before the
        env filter), so the Windows arm additionally pins that the shared fold
        does not widen the set even if that guard ever moves."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        environ = _environ_for(windows)
        admitted = {
            k
            for k in environ
            if platform_compat.env_key_allowed(k, source_providers._PROVIDER_BASE_ENV_KEYS)
        }
        assert admitted == _oracle_admitted(
            environ,
            source_providers._PROVIDER_BASE_ENV_KEYS,
            windows=windows,
            folds_on_windows=False,
        )

    def test_no_secret_admitted_by_any_site(self, monkeypatch, windows: bool) -> None:
        """The fold must never turn a credential-bearing name into a match."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        secrets = {"GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "SLACK_BOT_TOKEN"}
        for allowed in (
            registry._SAFE_ENV_KEYS,
            kiro_prerequisite._PROBE_ENV_KEYS,
            dev_fleet_server._SAFE_ENV_KEYS,
            gh_profile._MEASURE_ENV_PASSTHROUGH,
            source_providers._PROVIDER_BASE_ENV_KEYS,
        ):
            for secret in secrets:
                assert not platform_compat.env_key_allowed(secret, allowed)
