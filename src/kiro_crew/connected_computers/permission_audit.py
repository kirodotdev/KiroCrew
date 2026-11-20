"""One credential-free SEL event per committed local permission operation."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

logger = logging.getLogger(__name__)
_caller: ContextVar[str] = ContextVar(
    "connected_computer_permission_caller", default="connected_computers"
)


@contextmanager
def as_caller(caller: str) -> Iterator[None]:
    """Carry trusted entry-point identity through awaited work and to_thread."""
    token = _caller.set(caller)
    try:
        yield
    finally:
        _caller.reset(token)


def permission_changed(
    operation: str, resource: str = "", *, outcome: str = "ok", caller: str | None = None
) -> None:
    """Called after a successful store transaction, never inside its retry callback."""
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=caller if caller is not None else _caller.get(),
            operation="connected_computer_" + operation,
            outcome=outcome,
            source="connected_computers",
            resources=resource,
        )
    except Exception:  # noqa: BLE001 -- never undo a committed revoke when audit fails
        logger.warning("connected computers: permission audit unavailable", exc_info=True)


def permission_denied(
    operation: str, resource: str = "", *, reason: str = "", caller: str | None = None
) -> None:
    """One credential-free SEL event for a refused permission decision.

    ``reason`` is a short refusal code, never a credential or a path.
    """
    permission_changed(
        operation, resource, outcome="denied:" + reason if reason else "denied", caller=caller
    )
