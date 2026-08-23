"""Tool approval by TYPED REPLY, for channels that have no interactive widget.

This is the fallback ``transport.py`` and ``renderer.py`` both already promise:
a channel declaring ``max_buttons=0`` renders no widget and strips the
``[OPTIONS:]`` trailer, and both docstrings say the numbered-text form "lands
with the approval-ladder work". This module is that work, and it is
channel-neutral for the same reason the button cap is: a channel cannot forget
what it does not implement itself.

Without it, a zero-widget channel is not merely less pretty -- it cannot run a
tool at all. ``TurnDriver`` in ``interactive`` mode is deny-by-default with no
decider (``driver.py``), so every permission request is refused, and nothing in
the conversation says so. The operator sees an agent that answers questions and
silently fails to act.

**The security shape, which is the whole point of the module:**

* **Deny on silence.** The wait resolves to DENY on timeout. There is no path
  where not answering means yes. :data:`TEXT_APPROVAL_TIMEOUT_S` is held well
  under ``acp/client.py``'s ``_TOOL_STALL_TIMEOUT`` because no provider frame
  arrives while a permission is pending, so a longer window here would let the
  harness's own stall watchdog fire first and tear down a turn the operator was
  still deciding on.
* **An answer is bound to ONE request.** The registry is keyed by
  ``session_key`` AND ``request_id``, and a delivered verdict removes its
  entry. A "yes" typed after a request timed out cannot approve the NEXT tool
  the agent asks for -- the reply finds no open entry and is reported as
  expired instead of silently becoming consent.
* **Authorization is NOT here.** Whether a given sender may answer at all is a
  channel question (the transport knows who its operator is), so the channel
  gates the decider's construction. This module never sees a user id, which
  keeps it from being the place someone accidentally makes approval
  world-readable.
* **Trust reuses the session's own approval policy** rather than adding a
  second trust store. ``slack/handler.py`` keeps an in-memory
  ``_trusted_sessions`` set that ``dashboard/server.py`` already reaches across
  a circular-import boundary to clear; a third copy of "is this session
  trusted" would be one more thing to keep in sync. ``SessionManager``'s
  approval policy is durable, already SEL-audited on write, and already what
  spawned subagents inherit.

Dependency direction: stdlib only. No channel import, and nothing from
``slack``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Callable, Optional

from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

#: How long a pending approval waits for a typed answer before DENYING.
#:
#: Must stay comfortably below ``acp/client.py``'s ``_TOOL_STALL_TIMEOUT``
#: (600s): while a permission request is outstanding the provider emits no
#: frames, so that watchdog is armed against this wait. Five minutes is the
#: window for someone answering from a phone; the cost of it being too short is
#: a denied tool the operator can retry, and the cost of it being too long is a
#: torn-down turn.
TEXT_APPROVAL_TIMEOUT_S = 300.0

#: Verdicts. Ordinals are what the operator types; the strings are internal.
APPROVE = "approve"
DENY = "deny"
TRUST = "trust"

#: Ordinal -> verdict, matching the order the prompt lists them in.
APPROVAL_ORDINALS = {"1": APPROVE, "2": DENY, "3": TRUST}

#: Word forms accepted in addition to the ordinals. Kept deliberately small:
#: every entry here is a word that stops reaching the model while a prompt is
#: open, so a chatty synonym list would start eating real messages.
APPROVAL_WORDS = {
    "approve": APPROVE,
    "allow": APPROVE,
    "yes": APPROVE,
    "y": APPROVE,
    "ok": APPROVE,
    "deny": DENY,
    "no": DENY,
    "n": DENY,
    "reject": DENY,
    "cancel": DENY,
    "trust": TRUST,
    "always": TRUST,
}

#: Prompt/receipt prose. Backend-owned chat strings have no translation catalog
#: (the i18n catalog covers the dashboard), so they live here as the owning
#: module's constants rather than inline at each call site.
APPROVAL_CHOICE_LABELS = ("Approve once", "Deny", "Trust this session")
PROMPT_HEADER = "Tool approval needed"
PROMPT_FOOTER = "Reply 1, 2 or 3. No reply within 5 minutes denies."
RECEIPT_APPROVED = "Approved."
RECEIPT_DENIED = "Denied."
RECEIPT_TRUSTED = "Trusted for this session; later tools run without asking."
RECEIPT_EXPIRED = "That approval request already timed out. Ask again if you still want it."
TIMEOUT_NOTICE = "No answer in time, so I denied the tool."


@dataclass
class PendingApproval:
    """One outstanding permission request awaiting a typed answer."""

    session_key: str
    request_id: str
    #: Bound to the RUNNING loop by :func:`open_approval`. Not a
    #: ``default_factory``: ``asyncio.Future()`` resolves its loop through the
    #: deprecated ``get_event_loop()``, which outside a running loop invents one
    #: the waiter will never run on -- so the future would never resolve and the
    #: approval would hang to its timeout instead of taking the answer.
    future: asyncio.Future
    #: The conversation the prompt was posted in. An answer must arrive HERE.
    #: Session is too coarse: with a unified DM scope several conversations share
    #: one session key, so a bare "1" typed in an unrelated chat would otherwise
    #: resolve a tool the operator was asked about somewhere else.
    conversation_id: str = ""
    tool_title: str = ""
    #: Told by the renderer that the window closed, so the prompt still on the
    #: operator's screen can be resolved where they are looking. Deny-on-silence
    #: is otherwise INVISIBLE: the tool is refused, the turn moves on, and a live
    #: -looking prompt sits in the chat that a later "1" can no longer answer.
    #: A callback rather than a transport because this module never learns what a
    #: channel is; the renderer that posted the prompt is the only thing that
    #: knows which bubble to edit. Awaited from :meth:`wait`, so it must not raise
    #: -- it cannot change the verdict, and a failure to speak is not consent.
    on_timeout: Optional[Callable[[], Awaitable[None]]] = None

    async def wait(self, timeout_s: float) -> str:
        """The typed verdict, or :data:`DENY` when the window closes.

        Deny-on-timeout and deny-on-cancellation are the same answer on
        purpose: a turn torn down mid-decision must not leave a tool approved.
        """
        try:
            return await asyncio.wait_for(asyncio.shield(self.future), timeout_s)
        except asyncio.TimeoutError:
            await self._announce_timeout()
            return DENY
        except asyncio.CancelledError:
            # Two different events raise this, and conflating them is wrong in
            # opposite directions. The REQUEST being abandoned is a denial. The
            # TURN being torn down is cancellation, and swallowing it would
            # report a decision to a caller that is no longer listening and
            # break the cancellation it was told to honour -- so re-raise, which
            # is also the safe outcome: the tool is never approved.
            if self.future.cancelled():
                return DENY
            raise
        finally:
            _forget(self.session_key, self.request_id)

    async def _announce_timeout(self) -> None:
        """Let the channel resolve its own prompt. Never raises, never blocks long.

        Cancellation is re-raised because the caller is unwinding; every other
        failure is logged and swallowed, since the verdict is already DENY and a
        notice that could not be posted must not turn into an approval or an
        exception the decider reports as one.
        """
        if self.on_timeout is None:
            return
        try:
            await self.on_timeout()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001: the tool is already denied
            logger.warning("approval: could not announce the timeout", exc_info=True)


#: Open requests, keyed ``session_key:request_id``. Process-local and confined
#: to the gateway event loop, like the other per-conversation state in this
#: package, so no lock is taken.
_PENDING: dict[str, PendingApproval] = {}


def _registry_key(session_key: str, request_id: str) -> str:
    return f"{session_key}\n{request_id}"


def _forget(session_key: str, request_id: str) -> None:
    _PENDING.pop(_registry_key(session_key, request_id), None)


def open_approval(
    session_key: str,
    request_id: Any,
    tool_title: str = "",
    conversation_id: str = "",
    on_timeout: Optional[Callable[[], Awaitable[None]]] = None,
) -> PendingApproval:
    """Register the pending request. **Only a RENDERER may call this.**

    Who creates the entry is the load-bearing part. ``TurnDriver`` dispatches
    ``PROMPT_CHOICE`` to the renderer and only then awaits the decider -- two
    sequential ``await``s in one loop body, so the renderer always runs first.
    That ordering lets the decider treat "no entry" as "no prompt was
    delivered", which is the only safe reading: a request nobody was asked
    about must be denied at once, not waited on.

    It matters because the renderer is sometimes NOT the channel's. A muted
    conversation gets ``SilentRenderer``, whose ``on_prompt_choice`` drops the
    prompt on purpose. If the decider minted its own entry it would then wait
    the full timeout for an answer to a question that was never asked, turning
    a mute into a multi-minute stall on every tool call.

    Idempotent for the same ``(session, request)`` so a renderer that dispatches
    twice cannot orphan the first future.
    """
    key = _registry_key(session_key, str(request_id))
    entry = _PENDING.get(key)
    if entry is None:
        entry = PendingApproval(
            session_key=session_key,
            request_id=str(request_id),
            future=asyncio.get_running_loop().create_future(),
            conversation_id=conversation_id,
            tool_title=tool_title,
            on_timeout=on_timeout,
        )
        _PENDING[key] = entry
    elif tool_title and not entry.tool_title:
        entry.tool_title = tool_title
    return entry


def claim_approval(session_key: str, request_id: Any) -> Optional[PendingApproval]:
    """The entry a renderer already opened, or ``None`` if it never did.

    The decider's half of :func:`open_approval`. ``None`` means no prompt
    reached the user, so the caller must deny immediately rather than wait.
    """
    return _PENDING.get(_registry_key(session_key, str(request_id)))


def pending_for(session_key: str, conversation_id: str = "") -> Optional[PendingApproval]:
    """The oldest unresolved request for *session_key*, if any.

    Oldest-first because a reply answers the question the operator was just
    asked. Dict order is insertion order, which is what "oldest" means here.

    ``conversation_id`` narrows it to the chat the prompt was posted in. Pass it
    whenever the caller knows where the reply arrived: several conversations can
    share one session key, and an answer typed in a chat that was never asked
    must not resolve another chat's tool.
    """
    for entry in _PENDING.values():
        if entry.session_key != session_key or entry.future.done():
            continue
        if conversation_id and entry.conversation_id and entry.conversation_id != conversation_id:
            continue
        return entry
    return None


def parse_approval_reply(text: str) -> str:
    """The verdict a typed reply carries, or ``""`` when it carries none.

    Only a reply that is ENTIRELY an answer counts. "no" is a verdict; "no, use
    the other file instead" is a message for the model, and consuming it as a
    denial would eat instructions.
    """
    token = (text or "").strip().lower().rstrip(".!")
    if not token:
        return ""
    if token in APPROVAL_ORDINALS:
        return APPROVAL_ORDINALS[token]
    return APPROVAL_WORDS.get(token, "")


def deliver_verdict(session_key: str, verdict: str, conversation_id: str = "") -> str:
    """Hand *verdict* to the oldest open request; return the receipt to send.

    Returns :data:`RECEIPT_EXPIRED` when nothing is open, which is the case
    that must never be silent: the operator typed "yes" believing they were
    approving something, and they need to know they were not.
    """
    entry = pending_for(session_key, conversation_id)
    if entry is None:
        return RECEIPT_EXPIRED
    if not entry.future.done():
        entry.future.set_result(verdict)
    _forget(entry.session_key, entry.request_id)
    if verdict == APPROVE:
        return RECEIPT_APPROVED
    if verdict == TRUST:
        return RECEIPT_TRUSTED
    return RECEIPT_DENIED


def abandon_approval(session_key: str, request_id: Any) -> None:
    """Drop an entry whose prompt never reached the user.

    The renderer opens the entry before it sends, because the send is what can
    fail. If it does, the driver fails the turn but the entry would otherwise
    stay in the registry unresolved, and ``pending_for`` hands the NEXT typed
    message to it: the operator answers a question they never saw, for a tool
    from a turn that already ended.
    """
    _forget(session_key, str(request_id))


def _screened(text: str) -> str:
    """A tool title or purpose, screened for what the CHANNEL will show.

    Both are agent-authored: the model chooses the tool name and writes the
    purpose, so either can carry a credential or an exfiltration URL, and this
    prompt puts them straight into a chat message. Screening happens HERE rather
    than being left to each caller because that is the whole reason this helper is
    in the shared layer: a channel adopting the typed-reply ladder inherits the
    guarantee instead of re-deriving it, and a caller that forgets leaks on a
    security prompt of all places. A channel that screens again at its own sink is
    merely redundant, which costs a regex pass on two short strings.
    """
    safe, _ = redact_for_display(text or "", _redact_all)
    return safe


def _redact_all(text: str) -> str:
    """The redactor pair ``TurnDriver`` streams provider text through."""
    out, _ = redact_exfiltration_urls(text or "")
    out, _ = redact_credentials(out)
    return out


def build_approval_prompt(tool_title: str, tool_purpose: str = "") -> str:
    """The numbered prompt text for one permission request.

    Names the tool and its stated purpose only. ``OutputEvent`` carries no tool
    INPUT (see ``renderer.py``), so the prompt cannot show the command line --
    a real limit on how informed this consent is, and the reason
    :data:`APPROVAL_CHOICE_LABELS` offers "Trust this session" rather than
    anything broader.
    """
    lines = [PROMPT_HEADER, ""]
    lines.append(f"Tool: {_screened(tool_title) or 'unknown'}")
    if tool_purpose:
        lines.append(f"Purpose: {_screened(tool_purpose)}")
    lines.append("")
    for ordinal, label in enumerate(APPROVAL_CHOICE_LABELS, start=1):
        lines.append(f"{ordinal}. {label}")
    lines.append("")
    lines.append(PROMPT_FOOTER)
    return "\n".join(lines)


class TextReplyApprovalDecider:
    """An ``ApprovalDecider`` that waits for a typed answer.

    Constructed by the CHANNEL, and only once the channel has established that
    the sender may approve -- passing ``decider=None`` instead keeps
    ``TurnDriver`` at deny-by-default, which is the right answer for a sender
    who may chat but not authorize.
    """

    def __init__(
        self,
        session_key: str,
        sessions: Any = None,
        timeout_s: float = TEXT_APPROVAL_TIMEOUT_S,
    ) -> None:
        self.session_key = session_key
        self._sessions = sessions
        self._timeout_s = timeout_s

    async def __call__(self, event: Any) -> bool:
        request_id = str(getattr(event, "request_id", "") or "")
        entry = claim_approval(self.session_key, request_id)
        if entry is None:
            # The renderer did not open one, so no prompt reached anybody --
            # a muted conversation substitutes SilentRenderer, which drops the
            # prompt by design. Deny NOW: waiting the full window for an answer
            # to an unasked question would turn a mute into a stall on every
            # tool call.
            logger.debug(
                "approval: no prompt was delivered for %s; denying immediately",
                request_id,
            )
            return False
        verdict = await entry.wait(self._timeout_s)
        if verdict == TRUST:
            self._grant_session_trust()
            return True
        return verdict == APPROVE

    def trusted(self) -> bool:
        """Whether this session is already trusted.

        Wired to ``TurnDriver.auto_approve_session`` so a trusted session skips
        the prompt entirely instead of asking again for every tool.
        """
        if self._sessions is None:
            return False
        try:
            return bool(self._sessions.get_approval_policy(self.session_key) == "auto")
        except Exception:  # noqa: BLE001: a read failure must not grant trust
            logger.warning("approval: trust lookup failed", exc_info=True)
            return False

    def _grant_session_trust(self) -> None:
        """Record Trust as the session's approval policy.

        Reusing the session's own policy rather than a module-level set is what
        makes a spawned subagent inherit the grant: it reads the parent's
        approval policy, never an in-memory trust set.
        """
        if self._sessions is None:
            return
        try:
            self._sessions.set_approval_policy(self.session_key, "auto")
        except Exception:  # noqa: BLE001: the tool is already approved
            logger.warning("approval: could not persist session trust", exc_info=True)


def reset_for_tests() -> None:
    """Drop every pending request. Test-only; the registry is process-local."""
    _PENDING.clear()
