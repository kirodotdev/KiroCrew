"""``kirocrew update`` and ``kirocrew stop`` on a headless Windows desktop install.

The gateway itself hands the installer off at the start a restart causes
(:mod:`kiro_crew.update_boot_apply`). These are the two terminal paths
(decision entry "a headless Windows desktop install updates from its gateway"):

* ``kirocrew update`` verifies the release now. With a gateway running it arms
  the update for the next restart; with none it hands the installer to the
  helper at once, which installs once this command has exited. Any refusal
  exits non-zero and installs nothing.
* ``kirocrew stop`` installs an armed update once the gateway has stopped, and
  leaves the gateway stopped.

Neither runs while the desktop app runs from this install
(:func:`~kiro_crew.platform.desktop_installer_update.desktop_app_running`): the
app hosts the gateway then, and its own updater owns the update. That is asked of
the host, because this terminal's environment says nothing about who hosts the gateway.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from kiro_crew import update_next_start
from kiro_crew.platform import desktop_installer_update as desktop


def _gateway_running() -> bool:
    """Whether this home's gateway holds its lock. Unknown counts as running."""
    from kiro_crew import gateway_lock
    from kiro_crew.config.loader import config_dir

    try:
        holder = gateway_lock.lock_holder(config_dir())
    except gateway_lock.LockProbeError:
        return True
    return holder.pid is not None and holder.alive


def _snapshot_memory_or_refuse() -> str:
    """Copy every memory store before the installer rewrites this install.

    ``""`` when the copy landed; otherwise the refusal to show, and the caller
    installs nothing (the rule every other applier follows).
    """
    from kiro_crew.platform.wheel_apply import snapshot_memory_before_update, snapshot_refusal

    copied, failure = snapshot_memory_before_update()
    if failure:
        return snapshot_refusal(failure)
    print(f"  💾 Memory snapshot: {copied} store(s) copied")
    return ""


def _log_path() -> str:
    from kiro_crew.config.paths import data_home

    return str(data_home() / desktop.HELPER_LOG)


def _defer_to_app() -> None:
    from kiro_crew.platform.update_capability import (
        EXTERNALLY_MANAGED_MESSAGES,
        UNAVAILABLE_MANAGED_BY_APP,
    )

    print("  ℹ️  The desktop app is running from this install, and it updates it.")
    print(f"  {EXTERNALLY_MANAGED_MESSAGES[UNAVAILABLE_MANAGED_BY_APP]}")


def update_desktop_install() -> None:
    """``kirocrew update``: verify now, then arm (gateway running) or install (stopped)."""
    from kiro_crew import __version__ as running

    if desktop.desktop_app_running():
        _defer_to_app()
        return
    print("  ℹ  Desktop install without the desktop app: updating from the release feed.")
    failed = desktop.read_result()
    if failed and failed.get("ok") is False and isinstance(failed.get("error"), str):
        print(f"  ⚠  The last update attempt failed: {failed['error']}")
    try:
        prepared = asyncio.run(desktop.prepare(running))
    except desktop.DesktopUpdateError as exc:
        if exc.code == desktop.ERR_DOWNGRADE:
            print(f"\n✅ Already on the latest version! ({running})")
            return
        print(f"\n❌ {exc}")
        print("   Nothing was installed; this install is unchanged.")
        sys.exit(1)
    version = prepared.release.version
    print(f"  ✅ {version} verified: the release feed's sha512 and a valid signature by")
    print(f"     {', '.join(prepared.publishers)}.")
    if _gateway_running():
        update_next_start.arm(version)
        print(f"\n⏳ {version} is armed. It installs when the gateway next restarts:")
        print("     kirocrew restart")
        return
    failure = _snapshot_memory_or_refuse()
    if failure:
        print(f"\n❌ {failure}")
        sys.exit(1)
    # Armed as well, so the next start reports a failure the helper records, and
    # clears the record once this version runs.
    update_next_start.arm(version)
    helper = desktop.hand_off(prepared, wait_pids=(os.getpid(),), restart=False)
    print(f"\n⬇  Installing {version} now (helper pid {helper}), once this command exits.")
    print(f"   Its log: {_log_path()}")
    print("   Check the result with: kirocrew --version")


#: How long ``kirocrew stop`` waits for the stopped gateway to release its lock.
_STOP_RELEASE_SECS = 30.0


def _wait_gateway_gone(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _gateway_running():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)
    return True


def apply_after_stop() -> None:
    """``kirocrew stop``: install an armed update now that the gateway is down.

    Only the person's own stop calls this; a restart stops the gateway through
    the same code and the replacement's start hands the update off instead.
    """
    from kiro_crew import __version__ as running

    if not desktop.headless_desktop_install() or desktop.provider_owns_updates():
        return
    state = update_next_start.read()
    if not state.armed or not state.armed_version:
        return
    if not _wait_gateway_gone(_STOP_RELEASE_SECS):
        print(f"⚠  The armed update {state.armed_version} was not installed: the gateway")
        print("   is still running. It installs at the next restart.")
        return
    if desktop.desktop_app_running():
        # The app hosted the gateway just stopped, or runs beside it: the arm is
        # its updater's to apply, so it stays as it is.
        print(f"ℹ️  The armed update {state.armed_version} was not installed here: the")
        print("   desktop app is running from this install, and it updates it.")
        return
    try:
        prepared = asyncio.run(desktop.prepare(running, expected_version=state.armed_version))
    except desktop.DesktopUpdateError as exc:
        print(f"⚠  The armed update {state.armed_version} was not installed: {exc}")
        update_next_start.finish(error=str(exc))
        return
    failure = _snapshot_memory_or_refuse()
    if failure:
        print(f"⚠  The armed update {state.armed_version} was not installed: {failure}")
        update_next_start.finish(error=failure)
        return
    # The arm stays: the next start clears it when this version runs, or reports
    # the helper's failure as the pending apply error.
    helper = desktop.hand_off(prepared, wait_pids=(os.getpid(),), restart=False)
    print(
        f"⬇  Installing the armed update {state.armed_version} now (helper pid {helper}); "
        "the gateway stays stopped."
    )
    print(f"   Its log: {_log_path()}")
