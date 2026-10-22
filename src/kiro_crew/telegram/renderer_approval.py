"""Telegram's approval ladder: the prompt the renderer posts and the wait a press ends.

:class:`TelegramApprovalDecider` is the process-wide awaiter, with its own class-level
state (``_REGISTRY``, ``_NONCES``, ``_AWAITED``); ``on_prompt_choice`` posts the inline
Approve/Deny/Trust prompt and arms the decider for it. Each function here whose first
parameter is ``self`` is bound as the ``TelegramRenderer`` attribute of the same name in
:mod:`kiro_crew.telegram.renderer`, which keeps the renderer's state, so
``self.<name>(...)`` calls and instance and class patches reach it as they reach a
method defined in the class body. ``on_prompt_choice`` reads ``_chat_id``, ``_client``,
``_last_tool``, ``_session_key`` and ``_thread_id`` and writes ``_awaiting_approval``.
New approval-prompt work goes here.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.constants import DENY_CAUSE_APPROVAL_TIMEOUT, DENY_CAUSE_APPROVAL_UNDELIVERABLE
from kiro_crew.messaging.approval import APPROVAL_TIMEOUT_S, adoptable_reservation
from kiro_crew.messaging.renderer import new_approval_nonce, nonce_eq
from kiro_crew.telegram.renderer_display import _display_safe

if TYPE_CHECKING:
    from kiro_crew.telegram.renderer import TelegramRenderer

logger = logging.getLogger("kiro_crew.telegram.renderer")


# Interactive approval wait; deny-by-default when it elapses with no press.
# Owned by messaging.approval so every channel's window is the same one.
_APPROVAL_TIMEOUT_S = APPROVAL_TIMEOUT_S


#: How much of a tool's arguments the approval prompt shows. Enough to judge a
#: shell command or a file path; past this the operator is reading a payload, not
#: making a decision, and the prompt has to stay inside one message.
_APPROVAL_INPUT_CHARS = 900


class TelegramApprovalDecider:
    """Awaits an inline-button approval for a tool-permission request.

    Process-global Future registry keyed by ``session_key:request_id`` so
    concurrent turns (and users) never resolve each other's prompts. Denies by
    default when the wait elapses.

    The decision window opens when the nonce is armed, not when the wait starts:
    :meth:`arm` reserves the future and ``__call__`` adopts it. So a press lands
    inside the window from the moment the prompt is built, including across the
    suspension points between posting it and awaiting the decision.

    It closes at the decision, at the wait's timeout, or at a :meth:`retire` /
    :meth:`refuse_undelivered` / :meth:`discard_session` for a prompt that never
    went out or was never awaited -- NOT when the prompt stops being visible.
    Nothing here strips a timed-out prompt's buttons, so they stay clickable in
    the chat indefinitely; a press on them finds no nonce and is told the approval
    expired, which by then it has.
    """

    _REGISTRY: dict[str, "asyncio.Future[bool]"] = {}
    #: registry key -> the nonce minted for the buttons now showing. A press whose
    #: nonce does not match is refused, which is what stops a button from a previous
    #: run answering a live prompt that reuses its request id.
    _NONCES: dict[str, str] = {}
    #: Keys a wait currently OWNS -- added when ``__call__`` takes the future and
    #: discarded in the same ``finally`` that unregisters it. The registry holds
    #: one future per key, so the wait that added a key is the one that clears it.
    #: :meth:`discard_session` reads this to leave an owned window alone, since a
    #: wait under this session key need not belong to the turn running that sweep.
    _AWAITED: set[str] = set()

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
    def arm(cls, key: str, nonce: str, *, detached: bool = False) -> None:
        """Record the nonce for the buttons about to be posted, and OPEN the window.

        Called by whatever is about to post the prompt, so it runs on the event
        loop the wait will run on.

        Reserving the future here, rather than in ``__call__``, is what keeps a
        press inside the window while the prompt is being posted. ``TurnDriver``
        dispatches ``PROMPT_CHOICE`` to the renderer and only then awaits the
        decider, and the renderer suspends in between -- two thread hops for the
        display-safety scan of the tool name and its arguments, then the send. A
        press landing in that gap found the nonce armed but no future, so
        ``resolve_global`` judged it a stale button and failed closed: the user was
        told the approval had expired, and the request denied itself when the
        window elapsed. A Trust press in that gap also failed ``is_pending`` and so
        granted nothing, leaving the operator with neither the grant nor the tool.

        Never replaces a LIVE future. A second arm for one key, or an arm that
        follows the wait, keeps the object the waiter is blocked on; replacing it
        would leave that waiter on a future nobody resolves. A DONE future IS
        replaced, and that is the isolation bound: a decision left unawaited must
        not be adoptable by the next request to reuse this key.

        Off the event loop only the nonce is armed. A reservation is a promise to a
        wait that runs on THIS loop, so without one there is no waiter to hold a
        window open for -- and a caller that cannot await the decider cannot be
        raced by a press. That keeps this callable as a pure nonce operation.

        Pass *detached* when the wait this arms for runs OUTSIDE the turn whose
        end-of-turn sweep would otherwise reach the key. ``__call__`` claims a key
        as owned when it starts awaiting, which leaves the span from here to there
        unowned -- and for a caller whose wait is in a task of its own that span
        contains a network send, long enough for the originating turn to finish and
        sweep the window away under the buttons. The press then resolves nothing
        and the request denies at its own timeout on a refusal nobody made. The
        claim is released by ``__call__``'s ``finally`` and by :meth:`retire`, so
        every exit that ends the window also ends the claim. Ownership stays the
        sweep's one predicate; this only lets the caller that knows its wait is
        detached say so, rather than the sweep guessing from a request id's shape.
        """
        cls._NONCES[key] = nonce
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        reserved = adoptable_reservation(cls._REGISTRY.get(key), loop)
        if reserved is None or reserved.done():
            cls._REGISTRY[key] = loop.create_future()
            cls._UNDELIVERED.discard(key)
        if detached:
            cls._AWAITED.add(key)

    @classmethod
    def retire(cls, key: str) -> None:
        """Close a decision window whose prompt never went out (idempotent).

        ``__call__`` retires the nonce and the reservation together with the wait
        it ran, but a caller that ARMS then fails to post (a spawn-approval prompt
        Telegram rejected) has no wait to run that ``finally`` -- without this both
        would outlive the prompt that never existed, and the nonce is what
        authorizes a press.

        For a caller with somewhere else to fall through to, so no wait of its own
        runs on this key: the spawn-approval gate falls through to Slack and the
        dashboard. A caller whose driver WILL await the decider wants
        :meth:`refuse_undelivered` instead, because dropping the reservation there
        only means the wait opens a fresh window and spends the whole timeout on a
        prompt nobody can see.

        Releases a detached caller's ownership claim as well, so a window that ends
        here cannot leave the key permanently exempt from the sweep.
        """
        cls._NONCES.pop(key, None)
        cls._REGISTRY.pop(key, None)
        cls._AWAITED.discard(key)
        cls._UNDELIVERED.discard(key)

    @classmethod
    def refuse_undelivered(cls, key: str) -> None:
        """Record a denial for a prompt that never reached the chat.

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

        Skips a key a wait OWNS, which is what keeps this a sweep of unawaited
        windows rather than of every window a session holds. Not every wait under
        a session key belongs to the turn that runs this sweep: a spawn-approval
        prompt is armed under the parent session key and awaited by a detached
        task with its own window, so sweeping it would pop the future and nonce
        while an operator still had the buttons in front of them -- their press
        would then resolve nothing and the spawn would deny at its timeout on a
        refusal nobody made. Ownership is the predicate rather than the shape of
        the request id, so a wait added later is covered without being enumerated
        here.

        Drops every reservation no wait owns, whatever state its future is in. A
        completed one no wait adopted has no reader -- ``__call__`` for that turn
        never ran -- so keeping it retains the future and its nonce for the life of
        the process, once per key. The nonce is the worse half: the buttons stay in
        the chat, so a later press still matches a prompt nothing can answer. The
        prefix carries its own ``:`` so one session key cannot match another that
        merely starts the same way.
        """
        prefix = f"{session_key}:"
        for k in [k for k in cls._REGISTRY if k.startswith(prefix) and k not in cls._AWAITED]:
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
        reserved = adoptable_reservation(
            TelegramApprovalDecider._REGISTRY.get(k), asyncio.get_running_loop()
        )
        if reserved is not None and reserved.done():
            if reserved.cancelled() or reserved.exception() is not None:
                # A torn-down reservation, not a decision. Open a fresh window
                # rather than read it as consent or as a refusal.
                reserved = None
            else:
                try:
                    answer = bool(reserved.result())
                    if not answer and k in TelegramApprovalDecider._UNDELIVERED:
                        # The prompt never reached the chat; nobody refused.
                        self.last_deny_cause = DENY_CAUSE_APPROVAL_UNDELIVERABLE
                    return answer
                finally:
                    TelegramApprovalDecider._UNDELIVERED.discard(k)
                    TelegramApprovalDecider._REGISTRY.pop(k, None)
                    TelegramApprovalDecider._NONCES.pop(k, None)
        fut: "asyncio.Future[bool]" = (
            reserved if reserved is not None else asyncio.get_running_loop().create_future()
        )
        TelegramApprovalDecider._REGISTRY[k] = fut
        # This wait now owns the key, so the end-of-turn sweep must leave it be.
        TelegramApprovalDecider._AWAITED.add(k)
        try:
            answer = bool(await asyncio.wait_for(fut, _APPROVAL_TIMEOUT_S))
            if not answer and k in TelegramApprovalDecider._UNDELIVERED:
                self.last_deny_cause = DENY_CAUSE_APPROVAL_UNDELIVERABLE
            return answer
        except asyncio.TimeoutError:
            # Recorded for the driver, which steers the cause into the turn
            # before it rejects, so the model hears "expired" not "denied".
            self.last_deny_cause = DENY_CAUSE_APPROVAL_TIMEOUT
            return False  # deny-by-default on timeout
        finally:
            TelegramApprovalDecider._UNDELIVERED.discard(k)
            TelegramApprovalDecider._AWAITED.discard(k)
            TelegramApprovalDecider._REGISTRY.pop(k, None)
            # Retire the nonce with the prompt, so a button for a request id the
            # provider later reuses cannot match a nonce that is not live.
            TelegramApprovalDecider._NONCES.pop(k, None)

    @classmethod
    def nonce_matches(cls, key: str, nonce: str) -> bool:
        """Whether *nonce* is the one minted for the prompt currently at *key*.

        The binding a key alone cannot provide. ACP request ids are REUSABLE — a
        provider or gateway restart resets the sequence — and the conversation
        generation only changes on ``/new`` or an idle/daily rotation. So a
        provider that restarts mid-conversation issues request id 1 again, and a
        stale button from before the restart carries that same id: pressing it
        resolves a prompt for an UNRELATED tool the user never read, and on the
        Trust button also hands out standing auto-approve for the conversation.
        Only an unpredictable per-prompt value closes that, so the nonce is the
        thing actually checked and the key is just where it is filed.

        Constant-time compare, and fails closed on a missing or empty value.
        """
        expected = cls._NONCES.get(key)
        if not expected or not nonce:
            return False
        return nonce_eq(expected, nonce)

    @classmethod
    def is_pending(cls, key: str, nonce: str = "") -> bool:
        """Whether a live, unresolved prompt is registered for *key*.

        Asked BEFORE a side effect that a press should only be able to cause
        while its prompt is still live. The registry is empty after a gateway
        restart, so every approval button still sitting in a chat's scrollback
        would otherwise take effect against a session that does not exist.

        *nonce* is checked when supplied, so a caller asking "may this PRESS act"
        gets the prompt-identity answer rather than the weaker key-identity one.
        """
        if nonce and not cls.nonce_matches(key, nonce):
            return False
        fut = cls._REGISTRY.get(key)
        return fut is not None and not fut.done()

    @classmethod
    def resolve_global(cls, key: str, approved: bool, *, nonce: str = "") -> bool:
        """Resolve a pending approval by key. Returns True iff one was waiting
        AND the pressed button's nonce matches the one minted for that prompt."""
        if not cls.nonce_matches(key, nonce):
            return False  # stale or foreign button — fail closed
        fut = cls._REGISTRY.get(key)
        if fut is not None and not fut.done():
            fut.set_result(bool(approved))
            return True
        return False


async def on_prompt_choice(  # noqa: D401 - imperative reads wrong for a handler
    self: TelegramRenderer,
    options: list[dict[str, Any]],
    request_id: str | int,
    tool_title: str = "",
    tool_purpose: str = "",
    tool_input: str = "",
) -> None:
    # Approve/Deny as a SEPARATE message so ongoing streaming edits to the
    # answer bubble don't clobber the buttons.
    #
    # callback_data is ``a:<request_id>:<nonce>:<1|0|t>`` and stays well under
    # Telegram's 64-byte cap. The NONCE is load-bearing: ACP request ids restart
    # at 1 in every provider process, so a button still sitting in a Telegram
    # chat from a previous run names an id that is live again for a DIFFERENT
    # tool, and pressing it would approve that one. Minted per prompt and
    # compared on resolve, exactly as Discord and Teams do.
    self._note_progress()
    self._awaiting_approval = True
    rid = str(request_id)
    nonce = new_approval_nonce()
    key = TelegramApprovalDecider.key(self._session_key, rid)
    TelegramApprovalDecider.arm(key, nonce)
    # Three choices, matching Slack's ladder: approve this one, trust the rest
    # of this session, or refuse. Without Trust every tool of an agentic turn
    # costs its own round-trip, which is what pushes an operator to global YOLO,
    # a far wider grant than the one they actually wanted.
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"a:{rid}:{nonce}:1"},
                {"text": "🚫 Deny", "callback_data": f"a:{rid}:{nonce}:0"},
            ],
            [
                {
                    "text": "🤝 Trust this conversation",
                    "callback_data": f"a:{rid}:{nonce}:t",
                }
            ],
        ]
    }
    # The tool name and its arguments are LLM-authored and land in a body
    # Telegram RENDERS, so both go through the same display-form scan as the
    # answer rather than relying on the driver's byte-level pass alone: that
    # pass sees `AKIA**...**` as broken while the rendered message shows it
    # whole. Off-loop, because the scan is a full credential/exfil pass.
    #
    # The request's OWN title first: `_last_tool` is the last tool_call seen and
    # is never cleared, so it names the PREVIOUS tool for any permission that
    # arrives without one of its own, and the operator would be consenting to
    # something other than what they read.
    #
    # The name is monospaced, so this goes out as HTML: send_message defaults
    # to plaintext and markdown backticks would arrive literally. Escaped after
    # the scan, and retry_plain re-sends without a parse_mode if the markup is
    # ever rejected.
    tool = await asyncio.to_thread(_display_safe, tool_title or self._last_tool or "this tool")
    body = f"🔐 Approve <code>{html.escape(tool)}</code>?"
    # Show the arguments. "Approve bash?" is not a decision a user can make;
    # which command it wants to run is. Bounded so the prompt stays one message.
    detail = " ".join((tool_input or "").split())
    if detail:
        detail = await asyncio.to_thread(_display_safe, detail)
        if len(detail) > _APPROVAL_INPUT_CHARS:
            detail = detail[: _APPROVAL_INPUT_CHARS - 1].rstrip() + "…"
        body = f"{body}\n<pre>{html.escape(detail)}</pre>"
    try:
        posted = await self._client.send_message(
            self._chat_id,
            body,
            parse_mode="HTML",
            reply_markup=keyboard,
            message_thread_id=self._thread_id,
        )
    except BaseException:
        # The prompt never reached the chat, so nothing can be pressed and no
        # wait will run the ``finally`` that normally closes this window.
        # Retire it here instead of leaving a live nonce and reservation for a
        # prompt nobody saw. Raised on, because a caller that swallowed this
        # would leave the driver waiting out the whole window on an invisible
        # prompt and then call that elapsed wait a decision.
        TelegramApprovalDecider.retire(key)
        raise
    if not posted:
        # This client reports a failed send by RETURNING no message id rather
        # than by raising -- a revoked token, a chat it cannot write to, a
        # deleted topic, a rate limit or 5xx past its retries -- so the
        # ``except`` above does not cover it. Nothing is on screen to press,
        # and the driver awaits the decision next, so record the refusal on the
        # reservation it is about to adopt: it denies at once instead of
        # spending the whole window on a prompt nobody can see and reporting
        # that as an expiry.
        TelegramApprovalDecider.refuse_undelivered(key)
        logger.warning(
            "Telegram: the approval prompt for %s was not accepted by the chat; "
            "refusing the request rather than waiting it out",
            rid,
        )
