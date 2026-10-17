"""Widths of the two cold-start queues: ``agent.cold_start_concurrency`` and
``agent.runtime_spawn_concurrency``.

A cold start passes three bounded queues (rule: ``kiro_crew.start_priority``).
``SessionManager._start_sem`` (``cold_start_concurrency``) is held from runtime
spawn until the session's MCP servers have reported, so it spans the other two:
the gateway-wide runtime spawn + ``initialize`` admission
(``runtime_spawn_concurrency``) and the ``session/new`` gate
(``session_start_concurrency``, sized by ``session_start_sizing``).

``"auto"`` (the default for both) derives them from the effective
``session_start_concurrency`` width, so the three bounds move together. A
cold-start bound narrower than the ``session/new`` gate leaves gate permits
idle; spawn + ``initialize`` is the short first part of a start and needs about
half as many permits. The floors are the constants these bounds replaced, so a
small host never gets narrower admission than before; the auto ceilings are the
widest values measured (16 / 8 on a 128-vCPU host).

Two ways to resolve, as for the ``session/new`` gate: the ``resolve_*`` functions
probe the host on first use (disk I/O, so off the event loop), and the
``cached_*`` ones never probe, sizing ``"auto"`` from the process's cached host
reading and from the floors until that reading exists. ``SessionManager`` is
built on the gateway boot path, so it sizes its queues with the ``cached_*``
widths; the gateway's post-bind sizing task takes the reading in a worker thread
and then widens them (``SessionManager.widen_cold_start_queues``).

A neutral leaf module: the session layer and the ACP runtime both read it, and
application code may not import ``kiro_crew.acp``
(``scripts/check_agent_sdk_boundary.py``).
"""

from __future__ import annotations

import logging
from typing import Callable

from kiro_crew.session_start_sizing import (
    cached_session_start_concurrency,
    effective_session_start_concurrency,
    is_auto,
)

logger = logging.getLogger(__name__)

#: The fixed widths these bounds had before they were configurable; also the
#: ``"auto"`` floors and the fallback for an unreadable value.
COLD_START_DEFAULT = 4
RUNTIME_SPAWN_DEFAULT = 2
#: Explicit integers are clamped to ``1..CEILING`` (the loader does this too).
CEILING = 32
#: ``"auto"`` never sizes past what was measured.
AUTO_COLD_START_CEILING = 16
AUTO_RUNTIME_SPAWN_CEILING = 8


def auto_cold_start_limits(start_width: int) -> tuple[int, int]:
    """``(cold_start, runtime_spawn)`` for a ``session/new`` gate *start_width*.

    Width 4 or less: 4 / 2; width 8: 8 / 4; width 16: 16 / 8. The width already
    folds in the host's cores and free memory (``session_start_sizing``).
    """
    cold = min(AUTO_COLD_START_CEILING, max(COLD_START_DEFAULT, start_width))
    spawn = min(AUTO_RUNTIME_SPAWN_CEILING, max(RUNTIME_SPAWN_DEFAULT, start_width // 2))
    return cold, spawn


def _start_width(agent: object, width_of: Callable[[object], int]) -> int | None:
    """The effective ``session_start_concurrency``, or ``None`` if unreadable."""
    configured = getattr(agent, "session_start_concurrency", "auto")
    if not is_auto(configured) and (
        isinstance(configured, bool) or not isinstance(configured, int)
    ):
        return None
    try:
        return width_of(configured)
    except Exception:
        logger.debug("session start width unreadable; cold starts use defaults", exc_info=True)
        return None


def _resolve(
    agent: object, name: str, default: int, pick: int, *, probe: bool = True
) -> tuple[int, bool]:
    """``(width, is_auto)`` for one bound; *pick* indexes :func:`auto_cold_start_limits`.

    *probe* ``False`` sizes ``"auto"`` from the cached host reading only (event
    loop safe). A non-int that is not ``"auto"`` -- a hand-built or mocked
    config's stray attribute -- falls back to *default*: ``int(MagicMock())`` is
    1, which would silently serialise every start.
    """
    configured = getattr(agent, name, "auto")
    if is_auto(configured):
        width_of = (
            effective_session_start_concurrency if probe else cached_session_start_concurrency
        )
        width = _start_width(agent, width_of)
        if width is None:
            return default, True
        return auto_cold_start_limits(width)[pick], True
    if isinstance(configured, bool) or not isinstance(configured, int):
        return default, False
    return max(1, min(CEILING, configured)), False


def cached_cold_start_concurrency(agent: object) -> int:
    """``SessionManager._start_sem``'s background width, from the cache; loop safe."""
    return _resolve(agent, "cold_start_concurrency", COLD_START_DEFAULT, 0, probe=False)[0]


def cached_runtime_spawn_concurrency(agent: object) -> int:
    """The runtime spawn + ``initialize`` admission width, from the cache; loop safe."""
    return _resolve(agent, "runtime_spawn_concurrency", RUNTIME_SPAWN_DEFAULT, 1, probe=False)[0]


# The runtime spawn admission width the process's session manager configured, or
# ``None`` before one has. The admission is gateway-wide per event loop, but the
# config lives on the manager, so the manager publishes it here and the ACP layer
# reads it, without I/O, each time it looks a loop's admission up.
_configured_runtime_spawn_width: int | None = None


def configure_runtime_spawn_width(agent: object) -> int:
    """Publish ``agent.runtime_spawn_concurrency`` for this process, from the cache.

    Called from ``SessionManager.__init__`` (the floor for ``"auto"`` until the
    host reading exists) and again from ``widen_cold_start_queues`` once it does.
    The admission grows to the published width on its next lookup and never
    shrinks; the last manager built wins, and a process builds one.
    """
    global _configured_runtime_spawn_width
    width = cached_runtime_spawn_concurrency(agent)
    _configured_runtime_spawn_width = width
    return width


def configured_runtime_spawn_width() -> int | None:
    """The published runtime spawn width, or ``None`` if no manager published one."""
    return _configured_runtime_spawn_width


def describe_cold_start_limits(agent: object) -> str:
    """``cold_start_concurrency=N[ (auto)] runtime_spawn_concurrency=N[ (auto)]``.

    Probes the host for ``"auto"`` (and fills the cache): call off the loop.
    """
    parts = []
    for name, default, pick in (
        ("cold_start_concurrency", COLD_START_DEFAULT, 0),
        ("runtime_spawn_concurrency", RUNTIME_SPAWN_DEFAULT, 1),
    ):
        width, auto = _resolve(agent, name, default, pick)
        parts.append(f"{name}={width}{' (auto)' if auto else ''}")
    return " ".join(parts)
