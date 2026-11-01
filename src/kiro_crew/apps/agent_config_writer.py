"""The two writers of the base ``config.json`` ``agent`` section that app code uses.

:func:`mutate_trust_settings` edits a trust key (``apps_trusted`` and its
siblings) through the one trust writer every grant goes through;
:func:`mutate_agent_marker` edits a gateway-owned marker key that is read from the
base file. Both run the caller's ``mutate(agent_raw)`` inside the config lock and
the file lock, off the event loop. Shared by default installs and built-in
adoption, so both record a grant the same way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def mutate_trust_settings(mutate: Callable[[dict], None]) -> None:
    """Edit the base ``config.json`` ``agent`` section through the trust writer.

    ``security_trusted_apps._mutate_agent_config`` is the one writer every trust
    grant goes through: the shared config lock, the file lock, the refusal of a
    corrupt file, the refusal when ``config.local.json`` owns a trust key (so a
    grant is never written where it cannot take effect), and the cache
    invalidation. Raises its ``ConfigCorruptError`` / ``TrustSettingOverlayOwned``.
    """
    from kiro_crew.dashboard.handlers.security_trusted_apps import _mutate_agent_config

    await _mutate_agent_config(mutate)


async def mutate_agent_marker(mutate: Callable[[dict], None]) -> None:
    """Edit the base ``agent`` section for a key that is not a trust setting.

    Same locking as :func:`mutate_trust_settings` (the shared config lock, the file lock
    and corrupt-file refusal of ``update_config_locked``, the cache invalidation,
    off the event loop), without its refusal when ``config.local.json`` owns a
    trust key: that refusal exists because a base GRANT would be inert, and the
    applied marker is read from the base file, so writing it there always takes
    effect. Raises ``ConfigCorruptError`` on an unreadable file.
    """
    from kiro_crew.config.loader import (
        ConfigReadError,
        _invalidate_config_cache,
        config_path,
        update_config_locked,
    )
    from kiro_crew.dashboard.handlers.agents import _get_config_lock
    from kiro_crew.dashboard.handlers.security_audit import ConfigCorruptError

    def _read_modify_write() -> None:
        path = config_path()

        def _apply(raw: dict) -> dict:
            agent_raw = raw.get("agent")
            if not isinstance(agent_raw, dict):
                agent_raw = {}
                raw["agent"] = agent_raw
            mutate(agent_raw)
            return raw

        try:
            update_config_locked(path, mutate=_apply, stamp_meta=False)
        except ConfigReadError as exc:
            raise ConfigCorruptError(f"{path} is unreadable: {exc}") from exc
        _invalidate_config_cache()

    async with _get_config_lock():
        await asyncio.get_running_loop().run_in_executor(None, _read_modify_write)
