"""Under ``--port auto`` the readers set up before the runner starts name the served port.

The requested port is ``0`` there, which names no listener. ``app["served_port"]``
(read by the tailnet publish route and the webhook self-test) and the CSRF origin set
must name the port the reserved socket holds. The origin set keeps the requested
port's entries too, so nothing it admitted before is dropped.

The auth-cookie name fallback stays on the requested port, as on main: ``app["port"]``
and the token-auth middleware's ``port`` still name ``0``, so an ``mc_token_0`` cookie
set by an earlier auto-port gateway is still read the same way.

No host listener is bound (``no-test-side-effects``): the startup harness reserves an
inert socket double whose name is the served port.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard import server as srv


@pytest.mark.asyncio
async def test_an_auto_port_dashboard_gives_pre_bind_readers_the_served_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_dashboard_server_startup_coverage as harness

    chain = MagicMock(wraps=srv._install_dashboard_middlewares)
    monkeypatch.setattr(srv, "_install_dashboard_middlewares", chain)
    # The harness leaves the IPv6 secondary loopback listener real; stub it so no
    # host socket is bound.
    monkeypatch.setattr(srv, "_start_secondary_loopback_site", AsyncMock(return_value=None))
    async with harness._dashboard(tmp_path, monkeypatch) as (runner, _state, _spies):
        served = srv._reserve_dashboard_port.return_value.getsockname()[1]
        assert served != 0
        assert runner.app["served_port"] == served
        origins = runner.app["allowed_origins"]
        assert f"http://127.0.0.1:{served}" in origins
        # Added beside the requested port's entries, never in place of them.
        assert "http://127.0.0.1:0" in origins
        # The cookie-name fallback is unchanged from main.
        assert runner.app["port"] == 0
        assert chain.call_args.kwargs["port"] == 0
