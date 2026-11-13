"""Write an invited login into ``dashboard.tailscale.allowed_logins``.

The allowlist in ``config.json`` is what the next gateway start reads; the live
overlay (:mod:`kiro_crew.dashboard.tailnet_live_allowlist`) covers the time
until then. Adding a login also turns on ``enabled`` and ``trust_identity`` and
keeps this machine's own login on the list, the same durable shape one-click
mobile setup writes, so the first accepted invite cannot lock the owner's own
devices out. Removing a login never removes the machine owner's own login.

Returns whether a restart is needed: identity trust that was off in the file is
off in the running gateway too, which then checks no forwarded login at all.
"""

from __future__ import annotations

import asyncio
from typing import Any


def _mutate(
    data: dict[str, Any], login: str, add: bool, owner_login: str, out: dict[str, bool]
) -> dict[str, Any] | None:
    dashboard = data.setdefault("dashboard", {})
    if not isinstance(dashboard, dict):
        raise ValueError("config section 'dashboard' is not an object")
    tailscale = dashboard.setdefault("tailscale", {})
    if not isinstance(tailscale, dict):
        raise ValueError("config section 'dashboard.tailscale' is not an object")
    raw = tailscale.get("allowed_logins", [])
    if not isinstance(raw, list):
        raise ValueError("config field 'dashboard.tailscale.allowed_logins' is not a list")
    logins = [e for e in raw if isinstance(e, str)]
    lower = {e.strip().lower() for e in logins}
    before = list(logins)
    if add:
        out["restart"] = (
            tailscale.get("enabled") is not True or tailscale.get("trust_identity") is not True
        )
        for wanted in (owner_login, login):
            if wanted and wanted.strip().lower() not in lower:
                logins.append(wanted.strip())
                lower.add(wanted.strip().lower())
        tailscale["enabled"] = True
        tailscale["trust_identity"] = True
    else:
        key = login.strip().lower()
        if key and key != owner_login.strip().lower():
            logins = [e for e in logins if e.strip().lower() != key]
    if logins == before and not out.get("restart"):
        return None
    tailscale["allowed_logins"] = logins
    return data


async def write(login: str, *, add: bool) -> bool:
    from kiro_crew.config.loader import config_path, update_config_locked
    from kiro_crew.dashboard.handlers.agents import _get_config_lock
    from kiro_crew.shared_crew import tailscale_invites

    me = await asyncio.to_thread(tailscale_invites.daemon_self_reader)
    owner_login = me.login if me is not None else ""
    out: dict[str, bool] = {"restart": False}
    async with _get_config_lock():
        await asyncio.to_thread(
            update_config_locked,
            config_path(),
            mutate=lambda data: _mutate(data, login, add, owner_login, out),
        )
    return out["restart"]
