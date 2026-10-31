"""Lifecycle hooks for the code-intel app.

Language servers are started on demand from the Language servers tab, so there
is nothing to do at startup. On shutdown, every server this gateway started is
stopped and the app's worker pools are shut. The hook runs both when the app is
disabled and when the gateway stops; nothing latches, so enabling the app again
gets a fresh server pool and fresh workers, and at gateway stop no request can
arrive after the server socket closed.
"""

from __future__ import annotations

import asyncio
from typing import Any


async def on_shutdown(ctx: Any) -> None:
    from kiro_crew.apps.builtins.code_intel import pools
    from kiro_crew.apps.builtins.code_intel.manager import stop_all_servers

    await asyncio.get_running_loop().run_in_executor(pools.request_executor(), stop_all_servers)
    pools.shutdown_pools()
