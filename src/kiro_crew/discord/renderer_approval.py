"""Discord's approval ladder: the wait an Approve or Deny press ends.

:class:`DiscordApprovalDecider` is the process-wide awaiter, with its own class-level
state (``_REGISTRY``, ``_NONCES``, ``_DETACHED``) and the per-prompt nonce that keeps a
stale button from resolving a later request; it denies by default when
``_APPROVAL_TIMEOUT_S`` elapses. The prompt itself is posted by
:meth:`~kiro_crew.discord.renderer.DiscordRenderer.on_prompt_choice`, which stays with
the renderer's other sends; :func:`approval_prompt_text` writes its words. New
approval-wait work goes here. :mod:`kiro_crew.discord.renderer` imports these and
stays their import path and patch surface.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from kiro_crew.constants import DENY_CAUSE_APPROVAL_TIMEOUT, DENY_CAUSE_APPROVAL_UNDELIVERABLE
from kiro_crew.discord.client import DISCORD_MAX_TEXT
from kiro_crew.messaging.approval import APPROVAL_TIMEOUT_S, adoptable_reservation
from kiro_crew.messaging.approval_scan import _BACKTICK_PAIR_RE, bounded_scan
from kiro_crew.messaging.renderer import new_approval_nonce, nonce_eq
from kiro_crew.preview_text import drop_format_chars

logger = logging.getLogger("kiro_crew.discord.renderer")


# Interactive approval wait; deny-by-default when it elapses with no press.
# Owned by messaging.approval so every channel's window is the same one.
_APPROVAL_TIMEOUT_S = APPROVAL_TIMEOUT_S

#: A redaction pass the renderer hands in: its display-form or its source-form scan.
_Scan = Callable[[str], str]

#: Length caps on the one-line fields, checked on the whole original: a longer
#: field is hidden, never clipped, so the input always keeps most of the message.
_LINE_MAX = 200
_NAME_MAX = 100

#: Ends an input cut to fit the message, inside the fence so the fence still closes.
_CUT_MARKER = "\n… [truncated]"

#: Shown in place of a field that cannot be shown exactly as the tool gets it.
_HIDDEN = "hidden: it cannot be shown exactly as the tool gets it"

#: Printable ASCII. A field holding any other character is hidden whole rather than
#: shown altered: an accent, a non-Latin script or an invisible character cannot
#: then split a key from the scan, look like a different character, or go missing.
_ASCII = frozenset(map(chr, range(0x20, 0x7F)))
#: A one-line field (title, MCP names, purpose) is a plain single-backtick span,
#: so it may hold no backtick, which would end the span.
_LINE_CHARS = _ASCII - {"`"}
#: The input may also hold tabs, newlines and backticks; it is shown in a fence.
_INPUT_CHARS = _ASCII | {"\t", "\n"}


def _shown(text: str, allowed: frozenset[str], scan: _Scan, plain: _Scan) -> str | None:
    """*text* redacted for display, or ``None`` when the field must be hidden.

    The whole original is checked against *allowed* and then redacted
    (:func:`_literal_scan`), before anything cuts it.
    """
    if not set(text) <= allowed:
        return None
    return _literal_scan(text, scan, plain)


def _tag_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every ``[REDACTED…]`` tag in *text*."""
    spans: list[tuple[int, int]] = []
    start = text.find("[REDACTED")
    while start != -1:
        end = text.find("]", start)
        if end == -1:
            break
        spans.append((start, end + 1))
        start = text.find("[REDACTED", end)
    return spans


def _cut(text: str, limit: int) -> str | None:
    """*text* cut to *limit* characters ending in :data:`_CUT_MARKER`.

    The cut lands outside every tag of the whole text, or at a tag's start, never
    inside one. ``None`` when nothing visible would be left.
    """
    if len(text) <= limit:
        return text
    at = limit - len(_CUT_MARKER)
    for start, end in _tag_spans(text):
        if start < at < end:
            at = start
    head = text[:at]
    return head + _CUT_MARKER if head.strip() else None


def _inline(text: str, scan: _Scan, plain: _Scan, limit: int) -> str:
    """A one-line field as a single-backtick span, or the notice.

    Never clipped: shown only when the whole original is non-empty, has no edge
    space (a code span would not show it), fits *limit* and passes the allowlist.
    """
    if not text or text != text.strip(" ") or len(text) > limit:
        return f"({_HIDDEN})"
    shown = _shown(text, _LINE_CHARS, scan, plain)
    return f"({_HIDDEN})" if not shown else f"`{shown}`"


def _literal_scan(text: str, scan: _Scan, plain: _Scan) -> str | None:
    """*text* scanned for a LITERAL display (code), or ``None`` when it cannot be shown.

    Discord shows code as written, but *scan* reads text as rendered markdown. When
    markup hides a credential, the scan emits the rendered form, which drops a
    link's url and every emphasis marker: shown in a fence, ``[ok](https://evil)``
    would read ``ok`` while the tool gets the evil url. So the scanned text is kept
    only when it is exactly *plain* (the renderer's source-form redaction, the
    scan's own first pass) of the same text, format characters aside; both run
    through :func:`~kiro_crew.messaging.approval_scan.bounded_scan`. Then every
    character shown outside a tag is the tool's own, in place. Otherwise ``None``:
    the caller says the text is hidden rather than show either the credential or a
    text the tool will not receive.

    Callers check the character allowlist first (:func:`_shown`). The check here is
    still needed: the allowlist admits ``*``, ``_``, ``[`` and ``(``, so markup can
    still hide a key from the plain scan.
    """
    # Both through the one shared bound. Should the two choose different spans of a
    # long text (a scan that shrinks the head past half the bound reads it whole),
    # the forms differ and the text is hidden: the mismatch fails closed.
    literal = bounded_scan(text, plain)
    return literal if drop_format_chars(bounded_scan(text, scan)) == literal else None


def _break_backtick_pairs(text: str) -> str:
    return _BACKTICK_PAIR_RE.sub("`\u200b", text)


def approval_prompt_text(
    title: str,
    purpose: str,
    tool_input: str,
    scan: _Scan,
    plain: _Scan,
    mcp_identity: tuple[str, str] = ("", ""),
) -> str:
    """The approval prompt's message: the tool, its MCP identity, purpose and input.

    Every string is display-scanned with *scan* (the renderer's
    ``_redact_transformed``) before it is bounded, so a redaction tag that lengthens
    the text cannot push the closing fence past Discord's cap. *plain* is the
    renderer's source-form redaction (``_redact_all``), which code is checked
    against. Both are the renderer's, passed in because an owner never imports its
    facade. Blocking work: call it off the event loop.

    Every field is shown as code, so no Discord markup in it renders: the title,
    the MCP names and the purpose as single-backtick spans, the input in a fence.
    Each is shown only when the whole original passes the character allowlist
    (:data:`_LINE_CHARS`, :data:`_INPUT_CHARS`) and its display redaction matches
    its plain one (:func:`_literal_scan`); otherwise it reads as the hidden notice.
    A one-line field is never clipped (:func:`_inline`). The input alone is cut,
    after redaction and only outside a tag (:func:`_cut`). No empty span or fence
    is drawn.

    * *mcp_identity* is the trusted ``(server, tool)`` pair; its line is drawn only
      when it names a server, so a model-authored title cannot pose as another
      server's tool. The ``Purpose:`` label keeps the purpose from posing as it.
    * An empty *tool_input* draws no block: ``""`` means the provider sent no
      detail, not that the tool takes no arguments.
    """
    heading = title if title.strip(" ") else "this tool"
    lines = [f"🔐 Approve {_inline(heading, scan, plain, _LINE_MAX)}?"]
    server, tool = mcp_identity
    if server.strip(" "):
        tool = tool if tool.strip(" ") else "unnamed tool"
        names = (_inline(name, scan, plain, _NAME_MAX) for name in (server, tool))
        lines.append("MCP tool: {} / {}".format(*names))
    if purpose.strip(" "):
        lines.append(f"Purpose: {_inline(purpose, scan, plain, _LINE_MAX)}")
    head = "\n".join(lines)
    if not tool_input.strip(" \t\n"):
        return head
    shown = _shown(tool_input, _INPUT_CHARS, scan, plain)
    if shown is None:
        return f"{head}\nInput {_HIDDEN}."
    # Cut after the backtick break, which lengthens the text, so the cap holds.
    open_fence, close_fence = "\n```\n", "\n```"
    room = DISCORD_MAX_TEXT - len(head) - len(open_fence) - len(close_fence)
    detail = _cut(_break_backtick_pairs(shown), room)
    if detail is None:
        return f"{head}\nInput {_HIDDEN}."
    return f"{head}{open_fence}{detail}{close_fence}"


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

        A reservation another event loop left behind is no reservation at all
        (:func:`~kiro_crew.messaging.approval.adoptable_reservation`): it is
        replaced, so the wait blocks on a future this loop can resolve.
        """
        nonce = new_approval_nonce()
        cls._NONCES[key] = nonce
        if detached:
            cls._DETACHED.add(key)
        loop = asyncio.get_running_loop()
        reserved = adoptable_reservation(cls._REGISTRY.get(key), loop)
        if reserved is None or reserved.done():
            cls._REGISTRY[key] = loop.create_future()
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
        :meth:`refuse_undelivered` instead, which leaves a press that already
        landed standing.
        """
        cls._NONCES.pop(key, None)
        cls._REGISTRY.pop(key, None)
        cls._DETACHED.discard(key)
        cls._UNDELIVERED.discard(key)

    @classmethod
    def refuse_undelivered(cls, key: str) -> None:
        """Record a denial for a prompt that never reached the channel.

        The prompt is unanswerable, so the only safe verdict is a refusal, recorded
        on the reservation the driver is about to adopt so that it denies at once.

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
        # An entry from another event loop is not adopted: awaiting it raises
        # instead of deciding, and a verdict on it belongs to an ended turn.
        loop = asyncio.get_running_loop()
        reserved = adoptable_reservation(DiscordApprovalDecider._REGISTRY.get(k), loop)
        if reserved is None:
            # Nothing reserved means no prompt went out: a muted conversation's
            # SilentRenderer drops it. Deny now rather than wait the window for
            # a question nobody was asked, and hold a loop bound here as an
            # expiry would.
            DiscordApprovalDecider.retire(k)
            self.last_deny_cause = DENY_CAUSE_APPROVAL_UNDELIVERABLE
            self._notify_stalled()
            return False
        if reserved.done():
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
        fut: "asyncio.Future[bool]" = reserved if reserved is not None else loop.create_future()
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
            self._notify_stalled()
            return False  # deny-by-default on timeout
        finally:
            DiscordApprovalDecider._UNDELIVERED.discard(k)
            DiscordApprovalDecider._REGISTRY.pop(k, None)
            # Retire the prompt's nonce with the decision window: a press on
            # the (now stale) buttons can never resolve a future request.
            DiscordApprovalDecider._NONCES.pop(k, None)
            DiscordApprovalDecider._DETACHED.discard(k)

    def _notify_stalled(self) -> None:
        """Nobody can press for this session, so a monitoring loop bound to it
        cannot act either -- record it so the loop stops on its next wake instead
        of spending the rest of its cycle cap being denied. Inert for a session
        with no loop (``notify_approval_stalled`` resolves by binding key), and
        best-effort: a monitoring convenience must never change how this turn's
        denial is reported.
        """
        try:
            from kiro_crew.autonudge import get_instance as _autonudge_get

            _autonudge = _autonudge_get()
            if _autonudge is not None:
                _autonudge.notify_approval_stalled(self._session_key)
        except Exception:
            logger.debug("autonudge.notify_approval_stalled failed", exc_info=True)

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
