"""Whether a forum message addresses this bot, and which message a turn quotes.

``telegram.forum_activation`` decides whether a message in a forum Topic is served
at all (``always`` / ``mention`` / ``off``, read live per message); a 1:1 DM and a
press on the bot's own keyboard are always served. A turn in a Topic, or a drained
turn, visibly answers the message that triggered it; a live DM turn quotes nothing.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from kiro_crew.config.loader import ACTIVATION_MENTION, ACTIVATION_OFF

if TYPE_CHECKING:
    from kiro_crew.messaging.transport import InboundMessage
    from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

#: Cache of compiled @handle matchers. One handle per process in practice (the
#: bot's own), so this is a single entry; keyed anyway because tests set several.
_MENTION_RES: dict[str, "re.Pattern[str]"] = {}


def _mention_re(handle: str) -> "re.Pattern[str]":
    """A case-insensitive matcher for ``@handle`` as a whole token.

    ``(?![A-Za-z0-9_])`` is the whole point: without it `@kirocrewbot` matches
    inside `@kirocrewbot2`, so a message aimed at another bot in the same Topic
    activates this one. The leading `(?<![A-Za-z0-9_@])` stops a match inside an
    email-like or doubled-@ token for the same reason.
    """
    cached = _MENTION_RES.get(handle)
    if cached is None:
        cached = re.compile(
            r"(?<![A-Za-z0-9_@])@" + re.escape(handle) + r"(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        _MENTION_RES[handle] = cached
    return cached


def _addresses_this_bot(self: TelegramDispatcher, msg: InboundMessage) -> bool:
    """Whether *msg* addresses this bot, by @handle or by replying to it.

    Two routes, because Telegram users use both and a gate that recognised only
    the first would look broken to anyone who answers a bot by long-pressing its
    message:

    * the bot's own ``@handle`` appears in the text, case-insensitively. Only
      THIS bot's handle counts — a command aimed at another bot in the same
      Topic is not ours, the same reasoning ``_strip_bot_mention`` already
      applies to the ``/cmd@Other`` suffix.
    * the message replies to one sent by this bot, matched on ``bot_id``.
      ``is_bot`` on the replied-to sender is not enough: several bots can share
      a Topic.

    Both inputs are unresolved until ``getMe`` lands at startup
    (``bot_username`` empty, ``bot_id`` zero), which makes this answer False
    rather than True — the gate then holds until the identity is known instead of
    opening on a value it does not have yet.
    """
    if self.bot_id and getattr(msg, "reply_to_user_id", 0) == self.bot_id:
        return True
    handle = self.bot_username.strip().lstrip("@")
    if not handle:
        return False
    # Telegram's OWN classification, not a text scan. It marks a handle inside a
    # URL as a `url`/`text_link` entity rather than a `mention`, and a scan
    # cannot tell the two apart: `https://host/@thebot/x` satisfies any
    # `@handle` pattern, and `_flatten_text_links` appends a formatted link's
    # TARGET into the text, so anyone who can post a link could hand the scan a
    # handle to find. Comparison is on the lowercased handle, which is also what
    # makes it exact — Telegram usernames extend one another (`@kirocrewbot`,
    # `@kirocrewbot2`, `@kirocrewbot_dev` may all sit in one Topic), and an
    # entity names one username rather than a span to be matched.
    if getattr(msg, "has_entities", False):
        return handle.lower() in getattr(msg, "mentions", ())
    # No entity list: a synthesized message (an album with no captions, a legacy
    # or hand-built envelope). Fall back to the token matcher rather than
    # refusing, since "never parsed" is not "nobody was mentioned" — and such a
    # message has no entities precisely because it also has no auto-detected
    # URL for the matcher to trip over. `(?![A-Za-z0-9_])` is the same grammar
    # `_strip_bot_mention` uses, so `@kirocrewbot` does not match inside
    # `@kirocrewbot2`.
    return _mention_re(handle).search(msg.text or "") is not None


def _activation_outcome(self: TelegramDispatcher, msg: InboundMessage) -> str | None:
    """``None`` to serve this message, else the SEL outcome to audit and drop.

    Scoped to non-private chats: a 1:1 DM is unconditionally served, matching
    Slack, whose separate ``slack_dm_activation`` also defaults to ``always``.
    Mixing the two would mean an operator narrowing a noisy Topic silently
    muted their own DM.

    Mirrors ``forum_gate_outcome``'s shape — ``str | None`` — so both gates
    audit through one code path and a reader can see they are the same kind of
    decision at two different altitudes: may it, then should it.

    Deliberately WITHOUT Slack's ``thread_follow`` escape hatch, which lets an
    already-active thread continue unaddressed. Slack needs it because a Slack
    thread offers no way to aim a message at the bot specifically; Telegram
    does — replying to one of its messages, which ``_addresses_this_bot``
    already treats as addressing it. Adding a second, implicit route would make
    ``mention`` mean "mention, or some window after the last answer", which is
    the sort of rule an operator cannot predict from its name.
    """
    if getattr(msg, "chat_type", "private") == "private":
        return None
    # A press on the bot's own inline keyboard is addressing the bot by
    # construction — there is no @handle to type and no message to reply to —
    # so it is served in every mode, `off` included: the operator who set `off`
    # still expects their own tap to do something, and the keyboard only exists
    # because this bot posted it.
    if getattr(msg, "from_widget", False):
        return None
    activation = str(self._live_cfg().telegram.forum_activation)
    if activation == ACTIVATION_OFF:
        return "denied_activation_off"
    if activation == ACTIVATION_MENTION and not self._addresses_this_bot(msg):
        return "denied_activation_mention_only"
    return None


def _reply_target(msg: InboundMessage, *, interpret_commands: bool) -> int | None:
    """The message this turn should visibly answer, or ``None`` for no quote.

    Slack attaches every answer to what triggered it (``thread_ts or msg_ts``),
    which costs nothing there because a thread is the unit of conversation.
    Telegram has no such unit below the Topic, so attaching unconditionally
    would put a quote block above every reply in a 1:1 DM — where the answer
    already follows the question with nothing in between, so the quote adds a
    line of chrome and no information.

    It IS attached in the two cases where the link is genuinely ambiguous:

    * a **non-private chat** (a forum Topic), where several allow-listed
      participants can be talking at once and a flat answer belongs to nobody
      in particular;
    * a **drained queue turn** (``interpret_commands=False``, the marker the
      drain path passes), which is answered after the turn that was already
      running and therefore lands well below the message it answers.

    Returns ``None`` when the id is absent rather than guessing — the client's
    ``allow_sending_without_reply`` covers a target deleted after this point,
    but a zero id is not a target at all.
    """
    if getattr(msg, "chat_type", "private") == "private" and interpret_commands:
        return None
    return getattr(msg, "message_id", 0) or None
