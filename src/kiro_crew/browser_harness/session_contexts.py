"""Per-chat browser contexts: ``browser.isolation: per_session``.

By default every chat's tabs live in the browser's default context, so they share
one cookie jar: a sign-in in one chat is the sign-in of all of them. Under
``per_session`` each chat (one live-hub key) gets its own browser context from
``Target.createBrowserContext``: an in-memory profile with its own cookies,
storage and cache, so two chats can hold two accounts of one site at once.

The map lives on the relay (:meth:`CdpRelay.session_context`) because a context
lives on the connection that created it: Chrome disposes it when the connection
closes, and a reconnected relay starts with none. A chat's context is disposed when
the chat ends (``browser_ops._close_tabs_of``), which closes its tabs with it.

A chat's tabs are the ledgered ones in the context its op's bind put it in
(``LiveHub.context_of``, :func:`in_context`): under ``per_session`` its own context,
under ``shared`` the default one; the setting is read once per op, at the bind. A tab from the other side of a flip of the
setting is neither listed nor kept, so a chat that chose isolation never slides
back into the shared cookie jar through a tab it held before.

The person's own tabs stay in the default context and are never adopted, and the
default context's cookies never reach a chat's context: on the person's own
Chrome each chat signs in once, through takeover; on Kiro's Chromium the edition's
credentials are written into each new context (``HarnessSupervisor.seed_context``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from kiro_crew.config.sections_browser import (
    BROWSER_ISOLATION_PER_SESSION,
    browser_isolation_resolution,
)

if TYPE_CHECKING:
    from kiro_crew.browser_harness.relay import CdpRelay

logger = logging.getLogger(__name__)

_UNKNOWN = "browser.isolation could not be read; refusing to browse until it can"

#: The last value of ``browser.isolation`` a clean read gave, or ``None`` before one.
#: What an unreadable read falls back on: ``shared`` users keep browsing on a broken
#: file (the old code path), a chat that was ``per_session`` is refused instead.
_last_clean: str | None = None


class IsolationUnknown(RuntimeError):
    """``browser.isolation`` was ``per_session`` and cannot be read now: no op may run."""


def per_session() -> bool:
    """``browser.isolation`` is ``per_session``, read fresh.

    ``KiroCrewConfig.load`` does not raise for a config file that is present but
    unreadable or will not parse; it answers with defaults and marks the load
    (``_base_unreadable`` / ``_overlay_unreadable``, as ``auth.whoami`` and the git
    gateway read them). Such a read answers with the last clean value: ``shared``
    when that was ``shared`` or nothing was ever read (the default path is not
    changed by a broken file), and :class:`IsolationUnknown` when it was
    ``per_session``, since a chat whose person chose isolation must not silently
    browse in the shared jar.
    """
    global _last_clean
    try:
        from kiro_crew.config import KiroCrewConfig

        cfg = KiroCrewConfig.load()
    except Exception as exc:  # noqa: BLE001 - reported; the last clean value answers
        logger.warning("browser: browser.isolation could not be read: %s", exc)
        return _fallback()
    if getattr(cfg, "_base_unreadable", False) or getattr(cfg, "_overlay_unreadable", False):
        logger.warning("browser: a config file did not read whole; browser.isolation not read")
        return _fallback()
    value = browser_isolation_resolution(getattr(cfg.browser, "isolation", None))
    _last_clean = value
    return value == BROWSER_ISOLATION_PER_SESSION


def _fallback() -> bool:
    if _last_clean == BROWSER_ISOLATION_PER_SESSION:
        raise IsolationUnknown(_UNKNOWN)
    return False


def in_context(relay: CdpRelay, targets: Iterable[str], context: str) -> list[str]:
    """Those of *targets* that opened in *context*, in the given order."""
    return [t for t in targets if relay.context_of(t) == context]
