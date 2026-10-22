"""Discord's approval ladder: the wait an Approve or Deny press ends.

:class:`DiscordApprovalDecider` is the process-wide awaiter, with its own class-level
state (``_REGISTRY``, ``_NONCES``, ``_DETACHED``) and the per-prompt nonce that keeps a
stale button from resolving a later request; it denies by default when
``_APPROVAL_TIMEOUT_S`` elapses. The prompt itself is posted by
:meth:`~kiro_crew.discord.renderer.DiscordRenderer.on_prompt_choice`, which stays with
the renderer's other sends. New approval-wait work goes here.
:mod:`kiro_crew.discord.renderer` imports these and stays their import path and patch
surface.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.constants import DENY_CAUSE_APPROVAL_TIMEOUT, DENY_CAUSE_APPROVAL_UNDELIVERABLE
from kiro_crew.messaging.approval import APPROVAL_TIMEOUT_S
from kiro_crew.messaging.renderer import new_approval_nonce, nonce_eq

logger = logging.getLogger("kiro_crew.discord.renderer")


# Interactive approval wait; deny-by-default when it elapses with no press.
# Owned by messaging.approval so every channel's window is the same one.
_APPROVAL_TIMEOUT_S = APPROVAL_TIMEOUT_S


class DiscordApprovalDecider:
    """Awaits a button approval for a tool-permission request.

    Process-global Future registry keyed by ``session_key:request_id`` so
    concurrent turns (and users) never resolve each other's prompts. Denies by
    default when the wait elapses.

    Nonce guard: ACP request IDs are reusable (a provider or gateway restart
    resets the sequence), so a stale Approve button whose ``custom_id`` carries
    an old request ID could otherwise resolve a NEW pending request for an
    unrelated tool. Each rendered prompt therefore embeds an unpredictable
    per-prompt nonce (``register_nonce``), and ``resolve_global`` only resolves
    when the pressed button's nonce matches the one registered for that key —
    a press from any earlier prompt (or earlier process) fails closed.

    The decision window opens when the nonce is armed, not when the wait starts:
    ``register_nonce`` reserves the future and ``__call__`` adopts it. So a press
    lands inside the window from the moment the prompt is built, including across
    the suspension points between posting it and awaiting the decision.

    It closes at the decision, at the wait's timeout, or at a ``retire`` /
    ``refuse_undelivered`` for a prompt that never went out -- NOT when the prompt
    stops being visible. Nothing here strips a timed-out prompt's buttons, so they
    stay clickable in the channel indefinitely; a press on them finds no nonce and
    is told the approval expired, which by then it has.
    """

    _REGISTRY: dict[str, "asyncio.Future[bool]"] = {}
    #: key -> the per-prompt nonce embedded in that prompt's buttons.
    _NONCES: dict[str, str] = {}
    #: Keys whose wait runs OUTSIDE the turn that armed them, so the arming turn's
    #: end must not close their window. A spawn approval is the case: the gate
    #: awaits it in its own task and the agent is told to end its turn, so the
    #: per-turn sweep would otherwise drop a prompt the user is still looking at.
    _DETACHED: set[str] = set()

    #: Keys whose recorded refusal came from :meth:`refuse_undelivered`, so the
    #: adopting ``__call__`` can name the cause instead of reading it as a
    #: human's Deny. Cleared on every exit that drops the key.
    _UNDELIVERED: set[str] = set()

    def __init__(self, *, session_key: str) -> None:
        self._session_key = session_key
        #: Why the LAST call denied -- see ``messaging.driver.ApprovalDecider``.
        self.last_deny_cause = ""

    @staticmethod
    def key(session_key: str, request_id: str | int) -> str:
        return f"{session_key}:{request_id}"

    @classmethod
    def register_nonce(cls, key: str, *, detached: bool = False) -> str:
        """Mint the per-prompt nonce for *key* and OPEN its decision window.

        Called by whatever is about to post the prompt, so it runs on the event
        loop the wait will run on.

        Reserving the future here, rather than in ``__call__``, is what keeps a
        press inside the window while the prompt is being posted. ``TurnDriver``
        dispatches ``PROMPT_CHOICE`` to the renderer and only then awaits the
        decider, and the renderer suspends in between -- a thread hop for the
        display-safety scan, then the send. A press landing in that gap found the
        nonce armed but no future, so ``resolve_global`` judged it a stale button
        and failed closed: the user was told the approval had expired, and the
        request denied itself when the window elapsed.

        Never replaces a LIVE future. A second arm for one key, or an arm that
        follows the wait, keeps the object the waiter is blocked on; replacing it
        would leave that waiter on a future nobody resolves. A DONE future IS
        replaced, and that is the isolation bound: a decision left unawaited must
        not be adoptable by the next request to reuse this key.

        ``detached`` says the wait will run outside the turn arming this, so
        :meth:`discard_session` must leave it alone. Pass it whenever the prompt
        outlives its own turn -- the window then closes at the decision, at the
        wait's timeout, or at a ``retire``, and nowhere else.
        """
        nonce = new_approval_nonce()
        cls._NONCES[key] = nonce
        if detached:
            cls._DETACHED.add(key)
        reserved = cls._REGISTRY.get(key)
        if reserved is None or reserved.done():
            cls._REGISTRY[key] = asyncio.get_running_loop().create_future()
            cls._UNDELIVERED.discard(key)
        return nonce

    @classmethod
    def retire(cls, key: str) -> None:
        """Close a decision window whose prompt never went out (idempotent).

        ``__call__`` retires the nonce and the reservation together with the wait
        it ran, but a caller that arms and then fails to post has no wait to run
        that ``finally``. Without this, both would outlive a prompt nobody ever
        saw, and the nonce is what authorizes a press.

        For a caller with somewhere else to fall through to, so no wait of its own
        runs on this key. A caller whose driver WILL await the decider wants
        :meth:`refuse_undelivered` instead: dropping the reservation there only
        means the wait opens a fresh window and spends the whole timeout on a
        prompt nobody can see.
        """
        cls._NONCES.pop(key, None)
        cls._REGISTRY.pop(key, None)
        cls._DETACHED.discard(key)
        cls._UNDELIVERED.discard(key)

    @classmethod
    def refuse_undelivered(cls, key: str) -> None:
        """Record a denial for a prompt that never reached the channel.

        The prompt is unanswerable, so the only safe verdict is a refusal -- and
        recording it on the reservation the driver is about to adopt is what makes
        that refusal immediate. The alternative, dropping the reservation, reaches
        the same verdict only after the wait has spent the full decision window on
        a prompt nobody can see, and reports that elapsed wait as an expiry.

        Leaves the maps alone: the adopting ``__call__`` clears both when it
        consumes the decision, which keeps one owner for that cleanup. A key with
        no live reservation is left untouched, so this cannot overwrite a decision
        the user actually made.
        """
        reserved = cls._REGISTRY.get(key)
        if reserved is not None and not reserved.done():
            reserved.set_result(False)
            cls._UNDELIVERED.add(key)

    @classmethod
    def discard_session(cls, session_key: str) -> None:
        """Drop *session_key*'s unawaited reservations at the end of its turn.

        ``__call__`` clears its own entry in a ``finally``, and the failed-post
        paths above clear theirs, so this covers the one case neither can: the
        prompt went out and the turn then ended before the driver reached the
        decider -- a cancellation, or a failure between the two. No wait ever ran,
        so nothing else closes that window, and the nonce left behind is what
        authorizes a press.

        Drops only PENDING reservations. A resolved one holds a decision that was
        already delivered, and the prefix carries its own ``:`` so one session key
        cannot match another that merely starts the same way.

        A reservation marked detached by :meth:`register_nonce` is left alone: its
        wait runs in another task and survives this turn, so closing it here would
        strand a prompt the user can still see, and answer their press with an
        expiry the window had not actually reached.
        """
        prefix = f"{session_key}:"
        for k in [
            k
            for k, fut in cls._REGISTRY.items()
            if k.startswith(prefix) and not fut.done() and k not in cls._DETACHED
        ]:
            cls._REGISTRY.pop(k, None)
            cls._NONCES.pop(k, None)
            cls._UNDELIVERED.discard(k)

    async def __call__(self, event: Any) -> bool:
        self.last_deny_cause = ""
        k = self.key(self._session_key, getattr(event, "request_id", ""))
        # Adopt the reservation opened when this prompt's nonce was armed. The
        # press may ALREADY have landed, in the gap between the prompt going out
        # and this wait starting, in which case the reservation holds the user's
        # decision and there is nothing left to await. Minting a fresh future
        # here would discard that decision and deny when the window elapsed.
        reserved = DiscordApprovalDecider._REGISTRY.get(k)
        if reserved is not None and reserved.done():
            if reserved.cancelled() or reserved.exception() is not None:
                # A torn-down reservation, not a decision. Open a fresh window
                # rather than read it as consent or as a refusal.
                reserved = None
            else:
                try:
                    answer = bool(reserved.result())
                    if not answer and k in DiscordApprovalDecider._UNDELIVERED:
                        # The prompt never reached the channel; nobody refused.
                        self.last_deny_cause = DENY_CAUSE_APPROVAL_UNDELIVERABLE
                    return answer
                finally:
                    DiscordApprovalDecider._UNDELIVERED.discard(k)
                    DiscordApprovalDecider._REGISTRY.pop(k, None)
                    DiscordApprovalDecider._NONCES.pop(k, None)
                    # Every exit that drops this key drops its detached mark with
                    # it. A mark outliving its key exempts that key from the
                    # turn-end sweep for the life of the process, so the next
                    # request reusing it is swept by nothing.
                    DiscordApprovalDecider._DETACHED.discard(k)
        fut: "asyncio.Future[bool]" = (
            reserved if reserved is not None else asyncio.get_running_loop().create_future()
        )
        DiscordApprovalDecider._REGISTRY[k] = fut
        try:
            answer = bool(await asyncio.wait_for(fut, _APPROVAL_TIMEOUT_S))
            if not answer and k in DiscordApprovalDecider._UNDELIVERED:
                self.last_deny_cause = DENY_CAUSE_APPROVAL_UNDELIVERABLE
                return False
            # A button was pressed: a loop paused for approval in this session resumes.
            from kiro_crew.autonudge import release_approval_hold_for

            release_approval_hold_for(self._session_key, why="an approval was answered")
            return answer
        except asyncio.TimeoutError:
            # Recorded for the driver, which steers the cause into the turn
            # before it rejects, so the model hears "expired" not "denied".
            self.last_deny_cause = DENY_CAUSE_APPROVAL_TIMEOUT
            # Nobody pressed a button for the whole window, so a monitoring loop
            # bound to this session cannot act either -- record it so the loop
            # stops on its next wake instead of spending the rest of its cycle
            # cap being denied. Inert for a session with no loop
            # (``notify_approval_stalled`` resolves by binding key), and
            # best-effort: a monitoring convenience must never change how this
            # turn's denial is reported.
            try:
                from kiro_crew.autonudge import get_instance as _autonudge_get

                _autonudge = _autonudge_get()
                if _autonudge is not None:
                    _autonudge.notify_approval_stalled(self._session_key)
            except Exception:
                logger.debug("autonudge.notify_approval_stalled failed", exc_info=True)
            return False  # deny-by-default on timeout
        finally:
            DiscordApprovalDecider._UNDELIVERED.discard(k)
            DiscordApprovalDecider._REGISTRY.pop(k, None)
            # Retire the prompt's nonce with the decision window: a press on
            # the (now stale) buttons can never resolve a future request.
            DiscordApprovalDecider._NONCES.pop(k, None)
            DiscordApprovalDecider._DETACHED.discard(k)

    @classmethod
    def resolve_global(cls, key: str, approved: bool, *, nonce: str = "") -> bool:
        """Resolve a pending approval by key. Returns True iff one was waiting
        AND the button's nonce matches the registered per-prompt nonce."""
        expected = cls._NONCES.get(key)
        if not expected or not nonce or not nonce_eq(expected, nonce):
            return False  # stale/foreign button — fail closed
        fut = cls._REGISTRY.get(key)
        if fut is not None and not fut.done():
            fut.set_result(bool(approved))
            return True
        return False
