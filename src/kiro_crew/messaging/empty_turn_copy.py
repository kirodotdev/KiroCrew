"""The sentences a turn that ended with no assistant text owes the user.

Two surfaces state that verdict: the dashboard runner (``dashboard.chat_runner``)
for a dashboard turn, and the channel driver (``messaging.driver``) for a channel
turn, which is mirrored into the dashboard transcript as the same ``notice``
row. Both import their sentences from here, so the two cannot drift -- a user
who reads a channel thread in the dashboard reads ONE story only while the
words are identical, not merely similar.

Every sentence pairs what happened with what to DO, and the remedy is decided
by whether the turn did work first. A turn that ran a tool or reasoned has side
effects that already landed, so it is NEVER told to send its message again: a
resend re-runs them (a second ``send_message``, a second write, a second PR).
It is told to continue from where it stopped instead. The two remedies are
spelled once, below, and every sentence is composed from one of them.
"""

from __future__ import annotations

#: The remedy after a turn that did no work: the prompt itself can be resent.
EMPTY_TURN_RESEND = "Just send your message again to continue."
#: The remedy after a turn whose work already landed: continue, never resend.
EMPTY_TURN_CONTINUE = (
    "Send a message to continue from where it stopped — completed steps will not re-run."
)

#: A closed turn that produced nothing at all -- no text, no tool, no reasoning.
EMPTY_TURN_NOTICE = f"ℹ️ The model returned nothing this turn. {EMPTY_TURN_RESEND}"
#: The same, once the dashboard's automatic recovery also produced nothing.
EMPTY_TURN_NOTICE_AFTER_RECOVERY = (
    "ℹ️ The model returned nothing this turn (automatic recovery was attempted). "
    f"{EMPTY_TURN_RESEND}"
)
#: A closed turn that did work (ran a tool, reasoned) but ended without a
#: closing reply. "Returned nothing" would be false here.
EMPTY_TURN_NOTICE_AFTER_WORK = f"ℹ️ The turn ended without a closing reply. {EMPTY_TURN_CONTINUE}"
#: A textless turn the model DECLINED. Deterministic: the same prompt refuses
#: again, so the remedy is to rephrase, not to resend.
EMPTY_TURN_NOTICE_REFUSAL = (
    "⚠️ The model declined this request and returned no reply. Rephrase it to continue."
)
#: A textless turn closed by an ``error:``-family terminal. ``{reason}`` is a
#: fixed label for a closed protocol value, never the wire string.
EMPTY_TURN_NOTICE_ERROR = (
    f"⚠️ The turn ended with an error before it produced a reply ({{reason}}). {EMPTY_TURN_RESEND}"
)
#: The same terminal after the turn did work: names the failure, never a resend.
EMPTY_TURN_NOTICE_ERROR_AFTER_WORK = (
    "⚠️ The turn ended with an error before it produced a closing reply "
    f"({{reason}}). {EMPTY_TURN_CONTINUE}"
)
#: The provider stream ended without a terminal completion event.
EMPTY_TURN_NOTICE_UNCLOSED = f"⚠️ The turn ended without a reply. {EMPTY_TURN_RESEND}"
#: The same, after the turn did work.
EMPTY_TURN_NOTICE_UNCLOSED_AFTER_WORK = (
    f"⚠️ The turn ended without a closing reply. {EMPTY_TURN_CONTINUE}"
)
