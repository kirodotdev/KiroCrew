"""Whether the running kirocrew service is THIS gateway, asked of the service manager.

The generated service definition sets ``KIROCREW_SERVICE_MANAGED=1``
(:func:`kiro_crew.config.loader.launched_as_managed_service`). A unit installed
before that marker existed runs without it, and nothing rewrites it on update,
so a missing marker says nothing about who launched this process. The restart
idle check asks the manager directly: the main pid of each loaded kirocrew unit
against this process (or the launcher that ``exec``-ed or spawned it, the pair
:mod:`kiro_crew.gateway_restart` matches too).

The answer is ``"other"`` when any running unit a service restart reaches runs
a different process (that restart would stop work nobody checked),
``"service"`` when every one read runs this process, and ``"unknown"`` when
none could be read. Only ``"other"`` refuses.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Literal

from kiro_crew import platform_compat
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)

Identity = Literal["service", "other", "unknown"]

#: Bound on each ``launchctl print``; the handler runs this off the event loop.
_LAUNCHCTL_TIMEOUT_SECS = 3.0


def service_identity() -> Identity:
    """Whether a loaded kirocrew unit runs this process, from the manager's own answer."""
    from kiro_crew.service import common

    plat = common.current_platform()
    try:
        if plat == common.Platform.SYSTEMD:
            pids = _systemd_main_pids()
        elif plat == common.Platform.LAUNCHD:
            pids = _launchd_pids()
        else:
            return "unknown"
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("Service identity unknown: the service manager query failed (%s)", exc)
        return "unknown"
    return judge(pids, {os.getpid(), os.getppid()})


def judge(pids: list[int], mine: set[int]) -> Identity:
    """The verdict for the main *pids* of every running unit a service restart reaches.

    Any of them that is not this process means the restart would stop a gateway
    whose work was never checked: ``"other"``. Otherwise one that is this
    process is ``"service"``, and nothing read is ``"unknown"``.
    """
    if any(pid not in mine for pid in pids):
        return "other"
    if pids:
        return "service"
    return "unknown"


def _systemd_main_pids() -> list[int]:
    """Main pid of OUR running unit in each scope this process can ask.

    Through ``gateway_restart._systemd_show``, the one ``systemctl show`` reader
    for this unit. A scope counts only when its canonical ``Id`` is
    ``kirocrew.service``: an alias answers for another unit, and the service
    restart never selects it (``linux._UnitState.acts_on``). A scope that does
    not answer adds nothing.
    """
    from kiro_crew import gateway_restart
    from kiro_crew.service import common, linux

    pids: list[int] = []
    for user in (False, True):
        if user and linux._user_scope_unreachable_reason() is not None:
            continue
        try:
            props = gateway_restart._systemd_show(user)
        except gateway_restart._Inconclusive:
            return pids
        except (OSError, subprocess.SubprocessError) as exc:
            # This scope adds nothing, and what another scope already showed stands.
            logger.info("Service identity: a systemd scope did not answer (%s)", exc)
            continue
        if props.get("Id") != [f"{common.SERVICE_NAME}.service"]:
            continue
        if props.get("LoadState") != ["loaded"]:
            continue
        pid = _as_pid(props.get("MainPID", [""])[0])
        if pid:
            pids.append(pid)
    return pids


def _launchd_pids() -> list[int]:
    """``pid`` of the agent in the one domain ``macos.restart()`` would kickstart.

    The same walk as that restart: the domains in its order, skipping one that
    holds no job or does not exist, and stopping at the first other answer.
    A job in a later domain is never restarted, so it does not count. A domain
    whose ``print`` fails ends the walk with nothing read, as ``restart()``
    would refuse there.
    """
    from kiro_crew.pod import launchd as pod_launchd
    from kiro_crew.service import macos

    if not macos.PLIST_PATH.exists():
        return []
    # From the fixed system directories only, as ``_systemd_show`` resolves
    # ``systemctl``: this runs inside the gateway, whose ``PATH`` can lead with a
    # directory an agent can write to.
    launchctl = platform_compat.trusted_system_bin("launchctl")
    if launchctl is None:
        return []
    for domain in macos._legacy_domains():
        try:
            res = subprocess.run(
                [launchctl, "print", f"{domain}/{macos.LAUNCHD_LABEL}"],
                capture_output=True,
                check=False,
                timeout=_LAUNCHCTL_TIMEOUT_SECS,
                **UTF8_TEXT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.info("Service identity: launchd domain %s did not answer (%s)", domain, exc)
            return []
        if pod_launchd._service_absent(res) or macos._domain_unavailable(res):
            continue
        if res.returncode != 0:
            return []
        pid = _as_pid(macos._launchctl_print_value(res.stdout or "", "pid") or "")
        return [pid] if pid else []
    return []


def _as_pid(raw: str) -> int:
    """A positive pid from the manager's text, else 0 (not running, or unreadable)."""
    text = raw.strip()
    return int(text) if text.isdigit() and int(text) > 0 else 0
