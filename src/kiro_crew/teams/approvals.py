"""Tool-approval decisions for Teams, resolved by an Adaptive Card submit.

``TeamsApprovalDecider`` is the ``TurnDriver`` decider: it awaits a human click
for one tool request and DENIES by default when nobody answers in time. The
transport resolves clicks through the process-global registry, because the inbound
adapter has no reference to whichever per-turn decider is waiting.

**No grant store lives here.** The card's third button and ``/yolo`` both arm the
ONE process-wide ``safety_override`` grant that the dashboard toggle drives, so a
grant taken anywhere shows up -- and expires -- everywhere. A channel-local trusted
set would be a second grant with its own lifetime, its own audit trail and its own
way to disagree with the dashboard about whether auto-approve is on; ``resolve``
therefore only RECORDS that Trust was pressed and the dispatcher arms the shared
grant through ``messaging.commands.run_yolo_command``.

Two properties are load-bearing and both are the reason this is not just a dict of
futures:

* **Deny-by-default on every non-answer.** A timeout, an unknown request id, a
  mismatched nonce, and a prompt that was already answered all resolve to "not
  approved". The only path to True is a live pending prompt whose nonce matches.
* **A stale card cannot answer a live prompt.** ACP request ids restart at 1 in
  every provider process, so a card still sitting in a Teams chat from a previous
  run can name a request id that is live again for a different tool. The nonce is
  minted per prompt and compared on resolve, so the old card's click is refused.
  The registry key is namespaced by session for the same reason Slack's is:
  two sessions can be waiting on request id ``1`` simultaneously.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Awaitable, Callable

from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: How long a prompt stays clickable. Past this the turn stops waiting and the
#: tool is denied -- an agent blocked forever on a card nobody will click is
#: worse than a refusal the user can retry.
APPROVAL_TIMEOUT_SECS = 300.0


def registry_key(session_key: str, request_id: str) -> str:
    """Session-namespaced registry key. Bare id only when there is no session."""
    return f"{session_key}:{request_id}" if session_key else str(request_id)


class TeamsApprovalDecider:
    """Awaits an approve / trust / deny click for one interactive tool prompt."""

    #: request registry key -> the decider currently awaiting it.
    _REGISTRY: dict[str, "TeamsApprovalDecider"] = {}

    def __init__(self, session_key: str = "") -> None:
        self.session_key = session_key
        self._futures: dict[str, asyncio.Future[bool]] = {}
        #: request id -> the nonce minted for the card now showing.
        self._nonces: dict[str, str] = {}
        #: Prompts whose card never reached the user (see :meth:`abandon`).
        self._abandoned: set[str] = set()
        #: Set when a Trust click lands. The dispatcher reads it and arms the SHARED
        #: process-wide grant -- Teams keeps no grant store of its own, so there is
        #: one grant, one lifetime and one audit trail across every surface. Recorded
        #: rather than acted on here because activating is async and audited, and
        #: ``resolve`` is called from a sync click path.
        self.trusted = False
        #: Set by the renderer so an EXPIRED prompt's card can be replaced with its
        #: outcome. Without it the buttons keep looking live forever, and a chat that
        #: accumulates controls resolving to nothing is what the nonce exists to
        #: make safe rather than to make acceptable. A plain attribute because the
        #: renderer is built after the decider and holds the only reference.
        self.on_expired: Callable[[str], Awaitable[None]] | None = None

    def arm(self, request_id: str, nonce: str) -> None:
        """Record the nonce for the card the renderer is about to post."""
        self._nonces[str(request_id)] = nonce

    def abandon(self, request_id: str) -> None:
        """Refuse a prompt whose card never reached the user.

        A card post can fail transiently, and the nonce is armed BEFORE the post so
        a fast click is not refused as stale. Without this the turn would then park
        for the full :data:`APPROVAL_TIMEOUT_SECS` behind a card nobody received and
        deny with no explanation. Retires the nonce too, so a card that did somehow
        land cannot answer a prompt already written off.
        """
        rid = str(request_id)
        self._nonces.pop(rid, None)
        self._abandoned.add(rid)
        future = self._futures.get(rid)
        if future is not None and not future.done():
            future.set_result(False)

    async def __call__(self, event: Any) -> bool:
        request_id = str(getattr(event, "request_id", ""))
        key = registry_key(self.session_key, request_id)
        if request_id in self._abandoned:
            # The renderer already knows the user never saw this prompt.
            self._abandoned.discard(request_id)
            return False
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._futures[request_id] = future
        TeamsApprovalDecider._REGISTRY[key] = self
        try:
            return await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            logger.info("Teams: tool approval timed out for %s; denying", key)
            if self.on_expired is not None:
                try:
                    await self.on_expired(request_id)
                except Exception:
                    # The decision is already made; a failed edit must not become
                    # the reason the turn does not continue.
                    logger.debug("Teams: settling an expired prompt failed", exc_info=True)
            return False
        finally:
            self._futures.pop(request_id, None)
            self._abandoned.discard(request_id)
            # Retire the nonce with the prompt: a card whose window closed must
            # not become clickable again if the id is later reused.
            self._nonces.pop(request_id, None)
            if TeamsApprovalDecider._REGISTRY.get(key) is self:
                TeamsApprovalDecider._REGISTRY.pop(key, None)

    def resolve(self, request_id: str, nonce: str, *, approved: bool, trust: bool = False) -> bool:
        """Resolve one pending prompt. Returns True iff a prompt was waiting.

        False means the click did nothing — expired, already answered, or from a
        stale card — and the caller should tell the user rather than silently
        appearing to have worked.
        """
        rid = str(request_id)
        expected = self._nonces.get(rid)
        # compare_digest, not ``!=``: a nonce is a secret the client echoes back, so
        # the comparison is constant-time like every other secret comparison here.
        if not expected or not nonce or not secrets.compare_digest(nonce, expected):
            sel().log_api_access(
                caller=self.session_key or "unknown",
                operation="teams_approval.resolve",
                outcome="denied_stale_nonce",
                source="teams",
            )
            return False
        future = self._futures.get(rid)
        if future is None or future.done():
            return False
        if trust:
            # Recorded before the future resolves, so the dispatcher's click handler
            # sees it and can arm the shared grant BEFORE the turn's next permission
            # request is evaluated. Promoting it only once the turn ended would keep
            # prompting for the rest of it, which reads as the button not working.
            self.trusted = True
        future.set_result(approved)
        return True

    @classmethod
    def resolve_global(
        cls, session_key: str, request_id: str, nonce: str, *, approved: bool, trust: bool = False
    ) -> bool:
        """Resolve through the process-global registry (the inbound adapter's entry)."""
        decider = cls._REGISTRY.get(registry_key(session_key, request_id))
        if decider is None:
            return False
        return decider.resolve(request_id, nonce, approved=approved, trust=trust)

    @classmethod
    def reset_for_tests(cls) -> None:
        """Drop the process-global registry between tests."""
        cls._REGISTRY.clear()
