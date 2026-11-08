"""``computer_launch_app``: real-catalog invariant on Windows.

:class:`TestWindowsHostCatalog` asserts the resolution rules against the host's
REAL installed-application catalog on Windows. The fabricated-catalog tests in
``test_computer_use_launch.py`` prove the rules are implemented; this proves they
are SATISFIABLE — that a real installed application actually resolves, and that
every resolvable entry lives under a protected root.

Skipped on non-Windows platforms and, for the first test, on elevated Windows
runners (elevated process correctly refuses everything).
"""

from __future__ import annotations

import os

import pytest

from kiro_crew.computer_use.types import ComputerUseError
from kiro_crew.platform_compat import IS_WINDOWS

pytestmark = pytest.mark.xdist_group("computer_use_launch")


def _running_elevated() -> bool:
    """Whether this process has an administrator token. ``False`` off Windows.

    Two host-catalog assertions are only meaningful for an ORDINARY user: an elevated process
    can write every install root and owns content inside them, so the resolver correctly
    refuses everything and the assertion would be measuring privilege rather than the rule.
    CI's ``windows-latest`` runner is elevated.
    """
    if not IS_WINDOWS:
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - an unknown privilege level is not a test failure
        return False


@pytest.mark.skipif(not IS_WINDOWS, reason="asserts the real Windows host catalog")
class TestWindowsHostCatalog:
    """The invariant against the host's REAL catalog.

    The fabricated-catalog tests above prove the rules are implemented; this proves
    they are SATISFIABLE here — that a real installed application actually resolves.
    A resolver that refused everything would pass every test above.
    """

    def test_at_least_one_real_installed_app_resolves(self):
        """Satisfiable on an ORDINARY user's host, which is who this verb is for.

        Skipped when the suite runs elevated, and that is the honest reading rather than a
        convenience: an administrator genuinely can rewrite ``System32`` and genuinely does
        own content under ``Program Files``, so refusing every target is the CORRECT answer
        for that process — the trust question is "could this user have planted it", and for
        an admin the answer is yes everywhere. CI's ``windows-latest`` runner is elevated, so
        without this guard the assertion measures the runner's privilege rather than the
        resolver. The rules themselves are covered on every platform by
        :class:`TestLaunchResolutionTrust`; what this adds is that they are satisfiable on a
        real unelevated desktop.
        """
        from kiro_crew.computer_use import launch_windows

        catalog = launch_windows.installed_apps()
        assert catalog, "the host reported no installed applications at all"
        resolved = []
        for app in catalog:
            try:
                resolved.append(launch_windows.resolve_target(app.name))
            except ComputerUseError:
                continue
        if not resolved and _running_elevated():
            pytest.skip("elevated process: every install root is writable, so refusing is correct")
        assert resolved, "no entry in the host's own catalog survived resolution"

    def test_every_resolvable_entry_lives_under_a_protected_root(self):
        # The rule restated over real data: whatever resolves must be somewhere this
        # user cannot write. A failure here means the protected-root list is missing
        # a root that real applications use, which would be a genuine finding rather
        # than a test to relax.
        from kiro_crew.computer_use import launch_windows

        for app in launch_windows.installed_apps():
            try:
                executable, _name = launch_windows.resolve_target(app.name)
            except ComputerUseError:
                continue
            assert launch_windows._under_protected(executable), executable

    def test_the_local_windowsapps_alias_dir_is_never_a_launch_source(self):
        # Measured: %LOCALAPPDATA%\Microsoft\WindowsApps is ON PATH and writable, and
        # it is what shutil.which("mspaint") returns. Nothing that resolves may come
        # from there — that is the specific hole the protected-root rule closes.
        from kiro_crew.computer_use import launch_windows

        alias_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps")
        if not alias_dir or not os.path.isdir(alias_dir):
            pytest.skip("no execution-alias directory on this host")
        folded = os.path.realpath(alias_dir).casefold()
        for app in launch_windows.installed_apps():
            try:
                executable, _name = launch_windows.resolve_target(app.name)
            except ComputerUseError:
                continue
            assert not os.path.realpath(executable).casefold().startswith(folded)
