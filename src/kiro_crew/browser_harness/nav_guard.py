"""Judge every document request of an agent navigation BEFORE the browser sends it.

``ops._refuse_committed_url`` judges where a navigation LANDED, which is after the
browser has already sent each request on the way, redirect hops included. A public
URL that redirects to a loopback, private or link-local address has then already
reached that address (an OAuth ``redirect_uri`` on ``localhost`` receives the code,
a side-effecting GET on an internal service runs), and the refusal only stops the
agent from reading the answer.

:class:`NavigationGuard` closes that gap for the span of one navigation: it attaches
a relay-owned session to the agent's tab, enables CDP ``Fetch`` at the Request stage
for ``Document`` requests (the main frame and same-process iframes, every redirect
hop), continues each request whose URL the navigate gate allows, and fails the rest
with ``BlockedByClient``. The post-commit check stays as the second layer.

Fail closed: when the guard cannot be armed the navigation is not started, and a
paused request the guard cannot answer is failed, never continued.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Schemes a document request may carry that never leave the browser.
_LOCAL_SCHEMES = ("about:", "data:", "blob:")

_PATTERNS = [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]


class GuardUnavailable(RuntimeError):
    """The guard could not be armed on the tab, so the navigation must not start."""


class NavigationGuard:
    """One navigation's request-stage gate on *target_id*, as an async context manager.

    *relay* is the tab's :class:`~kiro_crew.browser_harness.relay.CdpRelay` (anything
    with ``attach``, ``detach`` and ``send``); *url_allowed* is the navigate gate the
    op already applies after commit. ``blocked`` lists each URL refused, in order.
    """

    def __init__(self, relay: Any, target_id: str, url_allowed: Callable[[str], bool]) -> None:
        self._relay = relay
        self._target_id = target_id
        self._url_allowed = url_allowed
        self._session_id = ""
        self._tasks: set[asyncio.Task[None]] = set()
        self.blocked: list[str] = []

    async def __aenter__(self) -> NavigationGuard:
        if self._relay is None or not self._target_id:
            raise GuardUnavailable("no relay-owned session for the agent tab")
        try:
            self._session_id = await self._relay.attach(self._target_id, self._on_event)
            await self._relay.send("Fetch.enable", {"patterns": _PATTERNS}, self._session_id)
        except Exception as exc:  # noqa: BLE001 - any arming failure refuses the navigation
            await self._release()
            raise GuardUnavailable(f"request guard could not be armed: {exc}") from exc
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        # Answer whatever is still paused before the session goes, so no request is
        # left hanging and none is continued unjudged.
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._release()

    async def _release(self) -> None:
        sid, self._session_id = self._session_id, ""
        if not sid:
            return
        try:
            await self._relay.send("Fetch.disable", {}, sid)
        except Exception:  # noqa: BLE001 - detaching ends the interception anyway
            logger.debug("nav guard: Fetch.disable failed", exc_info=True)
        try:
            await self._relay.detach(sid)
        except Exception:  # noqa: BLE001 - a tab already gone has no session to keep
            logger.debug("nav guard: detach failed", exc_info=True)

    async def _on_event(self, msg: dict[str, Any]) -> None:
        if msg.get("method") != "Fetch.requestPaused":
            return
        task = asyncio.create_task(self._answer(msg))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def allows(self, url: str) -> bool:
        """Whether a document request to *url* may be sent. Never raises."""
        if url.lower().startswith(_LOCAL_SCHEMES):
            return True
        try:
            return bool(self._url_allowed(url))
        except Exception:  # noqa: BLE001 - an unjudgeable URL is not sent
            return False

    async def _answer(self, msg: dict[str, Any]) -> None:
        params = msg.get("params") or {}
        request_id = params.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            return
        sid = msg.get("sessionId") or self._session_id
        url = str((params.get("request") or {}).get("url") or "")
        if self.allows(url):
            method, body = "Fetch.continueRequest", {"requestId": request_id}
        else:
            self.blocked.append(url)
            method = "Fetch.failRequest"
            body = {"requestId": request_id, "errorReason": "BlockedByClient"}
        try:
            await self._relay.send(method, body, sid)
        except Exception:  # noqa: BLE001 - the request then fails with the session
            logger.debug("nav guard: %s failed", method, exc_info=True)
