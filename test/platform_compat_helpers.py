"""The helper the ``test_platform_compat_*.py`` siblings share.

``_fake_windows_bins`` resolves Windows system binaries while a test fakes
``IS_WINDOWS`` on POSIX. ``test_platform_compat_kill_reap.py`` and
``test_platform_compat_listening_ports.py`` both use it.
"""

from __future__ import annotations

from kiro_crew import platform_compat as pc


def _fake_windows_bins(monkeypatch):
    """Resolve Windows system binaries while ``IS_WINDOWS`` is faked on POSIX.

    The Windows branches are deliberately exercised on the Linux CI fleet by
    flipping ``IS_WINDOWS``. Those branches resolve their binary from the
    trusted system directories before spawning, which a Linux host cannot
    satisfy, so the lookup is faked alongside the platform flag — otherwise the
    spawn reports the tool missing before the branch under test is reached.
    """

    monkeypatch.setattr(pc, "trusted_system_bin", lambda name: rf"C:\Windows\System32\{name}.exe")
