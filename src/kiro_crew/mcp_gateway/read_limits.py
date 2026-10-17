"""Per-stream read ceiling for the gateway's asyncio readers — a stdlib-only leaf.

Every reader the gateway opens (subprocess pipes, unix sockets) is given this
value as asyncio's ``limit=``, so both ends of a stub<->gatewayd socket have to
agree on it or one side refuses a frame the other was willing to send. That
makes it a shared constant rather than a per-process tuning knob, which is why
it lives in a module of its own.

The module is a LEAF on purpose: importing it costs stdlib only. Resolution
consults ``kiro_crew.config.loader``, which pulls in the config package and
about 200 modules with it, and that read happens inside
:func:`resolve_read_buffer_limit` rather than in this module's body. A caller
that never needs the ceiling never pays for it.

``kiro_crew.mcp_gateway.stub`` is the process that makes the difference worth a
module: it runs once per session per MCP server, so whatever it loads at import
is multiplied by every session on the host. It calls
:func:`read_buffer_limit_bytes` at the point it opens its socket.
``kiro_crew.mcp_gateway.pool`` resolves eagerly instead and re-exports
:data:`~kiro_crew.mcp_gateway.pool.READ_BUFFER_LIMIT_BYTES`, because the daemon
that imports it has the config package loaded anyway.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Per-stream byte ceiling for ``readuntil(b"\n")`` across the gateway.
# Default 64 MiB — generous enough for any real MCP tool response
# (ReadInternalWebsites can return 1-5 MiB pages). Asyncio's stdlib default is
# 64 KiB which is too small, and even a 1 MiB limit silently drops legitimate
# large responses.
_DEFAULT_READ_BUFFER_LIMIT = 64 * 1024 * 1024  # 64 MiB


def _env_read_buffer_limit() -> Optional[int]:
    """``KIROCREW_MCP_READ_LIMIT`` if it names a usable ceiling, else ``None``.

    The per-process escape hatch, and the first step of every resolution below,
    so it has one spelling. Values under the 1024 floor and values that are not
    integers are ignored rather than raising: this is a tuning knob read at
    startup, and a typo in it must not stop a stub connecting.
    """
    raw = os.environ.get("KIROCREW_MCP_READ_LIMIT")
    if not raw:
        return None
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return None
    return value if value >= 1024 else None


def config_read_buffer_limit() -> int:
    """The ceiling from the CONFIG key alone, ignoring the environment.

    What ``mcp_gateway.rewriter`` stamps into each stub's ``--read-limit`` flag.
    It skips ``KIROCREW_MCP_READ_LIMIT`` on purpose: the env var is a
    per-process escape hatch, and baking the rewriting process's own value into
    every overlay would make it permanent and global. The stub applies the env
    var itself, ahead of the flag, so the precedence an operator sees is
    unchanged.

    The config read is function-level to avoid an import cycle, and best-effort:
    config unavailable means the default, because a ceiling is not worth failing
    a launch over.
    """
    try:
        from kiro_crew.config.loader import _raw_config

        cfg_val = (_raw_config().get("mcp_gateway") or {}).get("read_buffer_limit_bytes")
        if isinstance(cfg_val, int) and not isinstance(cfg_val, bool) and cfg_val >= 1024:
            return cfg_val
    except Exception:
        logger.debug("mcp read limit: config unavailable, using default", exc_info=True)
    return _DEFAULT_READ_BUFFER_LIMIT


def resolve_read_buffer_limit() -> int:
    """The ceiling for a process that has to work it out for itself.

    Precedence: ``KIROCREW_MCP_READ_LIMIT`` → config key
    ``mcp_gateway.read_buffer_limit_bytes`` → the default. Each step is one call
    to the function that owns it, so there is one spelling of each.
    """
    return _env_read_buffer_limit() or config_read_buffer_limit()


def read_buffer_limit_from_flag(flag_value: Optional[int]) -> int:
    """The ceiling for a process HANDED one, without reading config.

    Precedence: ``KIROCREW_MCP_READ_LIMIT`` → ``flag_value`` → the full
    resolver. The env var stays first so it still overrides per process. The
    fall-through is about correctness rather than tidiness: an overlay written
    before the flag existed supplies ``None``, and answering that with the bare
    default would silently ignore a config key the operator had set, so it
    resolves the long way instead. One rewrite replaces that path with the flag,
    and a stub carrying the flag never reads config at all.

    ``mcp_gateway.stub`` is the caller that makes this worth having: it runs once
    per session per MCP server, and the config read pulls roughly 140 modules
    into every one of them.
    """
    from_env = _env_read_buffer_limit()
    if from_env is not None:
        return from_env
    if flag_value is not None and flag_value >= 1024:
        return flag_value
    return resolve_read_buffer_limit()
