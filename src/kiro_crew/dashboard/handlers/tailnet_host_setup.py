"""POST /api/tailnet/mobile/host-setup: set Tailscale up on this headless host.

The phone-access card offers this on its ``install``, ``start_daemon`` and
``sign_in`` steps when :func:`kiro_crew.dashboard.tailnet_host_setup.host_plan`
says the host can do it itself (Linux, no desktop, a supported distro,
passwordless ``sudo``). The work is in that module; this handler adds the same
gates the card's other mutations have: owner-only, dashboard users only, no
restricted session, the live step re-derived here, and an audit record.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.dashboard import tailnet_host_setup
from kiro_crew.dashboard.handlers import tailnet_mobile

#: The steps whose errand this endpoint can run. Every later step, and the
#: policy and pin refusals ahead of these, answer ``not_needed``.
HOST_SETUP_STEPS = frozenset({"install", "start_daemon", "sign_in"})


async def api_tailnet_mobile_host_setup(request: web.Request) -> web.Response:
    """Install, start and sign this machine in to Tailscale.

    A sign-in link the daemon now holds reaches the card through the status
    read (``auth_url``), not this body. It is not written to the audit record
    or any log.
    """
    refusal = tailnet_mobile._guard(request)
    if refusal is not None:
        await tailnet_mobile._audit_async(
            request, "tailnet.mobile.host_setup", "denied", "restricted-session"
        )
        return refusal
    live = await tailnet_mobile._live_state(request, tailnet_mobile._dashboard_port(request))
    if live.step not in HOST_SETUP_STEPS:
        await tailnet_mobile._audit_async(
            request, "tailnet.mobile.host_setup", "denied", f"step={live.step}"
        )
        detail = "Tailscale is already set up on this machine, or setup is not allowed here."
        return web.json_response(
            {"ok": False, "code": "not_needed", "error": detail, "detail": detail},
            status=409,
        )
    plan = await asyncio.to_thread(tailnet_host_setup.host_plan)
    if plan is None:
        await tailnet_mobile._audit_async(
            request, "tailnet.mobile.host_setup", "denied", "unsupported-host"
        )
        detail = "This machine cannot install Tailscale by itself. Use the download link."
        return web.json_response(
            {"ok": False, "code": "unsupported_host", "error": detail, "detail": detail},
            status=409,
        )
    result = await asyncio.to_thread(tailnet_host_setup.run_host_setup, plan)
    await tailnet_mobile._audit_async(
        request,
        "tailnet.mobile.host_setup",
        "success" if result.ok else "denied",
        f"step={live.step} manager={plan.manager}",
    )
    # Two literal returns, not a computed status: see api_tailnet_mobile_publish.
    if result.ok:
        return web.json_response(
            {"ok": True, "code": "ok", "detail": result.detail},
            status=200,
        )
    return web.json_response(
        {"ok": False, "code": "failed", "error": result.detail, "detail": result.detail},
        status=409,
    )
