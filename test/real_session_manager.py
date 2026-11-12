"""A real ``SessionManager`` for tests, faked only at the ACP child.

``real_sessions()`` builds the production ``kiro_crew.session.SessionManager`` on a
default ``KiroCrewConfig``. Its provider factory hands back an in-process stand-in
for the ACP child (``fake_provider``), so nothing is spawned and nothing reaches the
network, while every registry, link, policy and teardown path is the real one.

A test can then read what the code left behind through the manager's own API
(``has_session``, ``get_slack_link``, ``get_approval_policy``,
``consume_needs_reinjection`` ...) instead of reading back what a mock was sent.

Writes such as the session map land under ``KIROCREW_HOME``, which ``conftest.py``
points at a per-test directory.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager


def fake_provider(provider: Any = None) -> Any:
    """Give *provider* (or a new ``AsyncMock``) the attributes a live ACP child has.

    The lifecycle calls the manager awaits and the attributes it reads
    synchronously are pinned, so a plain ``MagicMock`` still starts a session; an
    attribute the caller already set is kept, so a test can script ``stream`` or
    ``cancel``.
    """
    p = AsyncMock() if provider is None else provider
    defaults: dict[str, Any] = {
        "start": AsyncMock(),
        "shutdown": AsyncMock(),
        "cwd": "",
        "memory_mode": "persistent",
        "exit_code": None,
        "disown_work_dir": MagicMock(),
        "is_process_alive": MagicMock(return_value=True),
        "context_usage_pct": MagicMock(return_value=0.0),
        "context_usage_unknown": MagicMock(return_value=False),
        "context_window_tokens": MagicMock(return_value=0),
        "context_used_tokens": MagicMock(return_value=0),
        "reclaim": MagicMock(),
        "has_active_turn": MagicMock(return_value=False),
        "runtime_info": MagicMock(return_value=(None, None)),
    }
    for name, value in defaults.items():
        if not _has_own(p, name):
            setattr(p, name, value)
    return p


def _has_own(obj: Any, name: str) -> bool:
    """Whether *obj* carries *name* itself; a mock answers every name, so ask it directly."""
    if name in vars(obj):
        return True
    children = getattr(obj, "_mock_children", None)
    if isinstance(children, dict):
        return name in children
    return hasattr(obj, name)


def real_sessions(
    provider: Any = None,
    *,
    factory: Callable[..., Any] | None = None,
    cfg: KiroCrewConfig | None = None,
) -> SessionManager:
    """A real ``SessionManager`` whose every new session runs on a fake ACP child.

    With *provider*, every session shares that one stand-in (the shape a test that
    scripts one turn wants). With *factory*, it is called like the production
    provider factory and its result is completed by ``fake_provider``. With
    neither, each session gets a fresh stand-in.
    """
    if provider is not None and factory is not None:
        raise ValueError("pass provider or factory, not both")

    def _factory(*args: Any, **kwargs: Any) -> Any:
        if provider is not None:
            return fake_provider(provider)
        if factory is not None:
            return fake_provider(factory(*args, **kwargs))
        return fake_provider()

    return SessionManager(cfg or KiroCrewConfig(), provider_factory=_factory)


async def open_session(mgr: SessionManager, key: str, **kwargs: Any) -> str:
    """Create a live session under *key*, release its turn lease, return the folded key."""
    await mgr.get_or_create(key, **kwargs)
    folded = mgr._fold_key(key)
    mgr.release(folded)
    # A later "the session is gone" check means something only if it was here.
    assert mgr.has_session(folded), f"no live session was created under {key!r}"
    return folded
