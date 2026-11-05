"""The github_repo profile's measurement passthrough on core's env-allowlist convention.

``_measure_env`` filters the gateway environment through
``platform_compat.env_key_allowed``: exact on POSIX, case-folded on Windows, where
CPython's ``os.environ`` upper-cases every key. The passthrough allowlist is written
upper-case-exact, so its oracle is exact membership on both platforms and the shared
fold must admit the same set. No credential-bearing name is ever admitted.
"""

from __future__ import annotations

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import profile as gh_profile

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
    "KIRO_API_KEY": "kiro-secret",
}

# CPython's os.environ upper-cases every key on Windows, so this is the only
# spelling a filter ever sees there at runtime.
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
    "KIRO_API_KEY": "kiro-secret",
}


@pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
class TestMeasurementPassthrough:
    def test_admits_exactly_its_own_upper_case_allowlist(
        self, monkeypatch, tmp_path, windows: bool
    ) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        environ = _WINDOWS_ENVIRON if windows else _POSIX_ENVIRON
        monkeypatch.setattr(gh_profile.os, "environ", dict(environ))
        env = gh_profile._measure_env(tmp_path)
        # _measure_env layers pinned measurement variables and PYTHONPATH on top
        # of the passthrough; only the keys sourced from the environ are the
        # allowlist's admitted set.
        admitted = set(env) & set(environ)
        assert admitted == {k for k in environ if k in gh_profile._MEASURE_ENV_PASSTHROUGH}

    def test_admits_no_credential(self, monkeypatch, windows: bool) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        for secret in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "SLACK_BOT_TOKEN", "KIRO_API_KEY"):
            assert not platform_compat.env_key_allowed(secret, gh_profile._MEASURE_ENV_PASSTHROUGH)
