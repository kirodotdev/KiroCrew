"""What a Discord turn leaves in the dashboard: the live window rows and the transcript
(``_record_turn``, ``_persist_turn``), and whether a session may leave either
(``_session_restricted``) or ship local files (``_uploads_restricted``).

``_record_turn`` and ``_session_restricted`` are the ``record`` and ``restricted`` adapters
``DiscordDispatcher._channel_turns`` hands the shared turn pipeline, and
``_uploads_restricted`` is the upload ceiling ``handle_message``'s renderer reads. The
``surface`` adapter (``_surface_own_session``) stays in the class body: its own
``self.dashboard_state = ...`` is the only declaration of that attribute the type checker
sees. The dashboard imports stay inside the functions, because the dashboard imports the
channel transports on its boot path.

Each function here whose first parameter is ``self`` is a ``DiscordDispatcher``
method: the class binds it by name, so it reads and writes the dispatcher's own
state on the instance, and a patch of the method on the class or on an instance
reaches every caller.

Instance fields read (a call on one included): ``_session_resume``, ``conv_log``.

Instance fields assigned, or whose mapping is changed: none.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from kiro_crew.history import mint_row_mid
from kiro_crew.messaging.dispatch import TurnRecord
from kiro_crew.messaging.upload_gate import session_is_restricted, uploads_restricted
from kiro_crew.security import redact, redact_local_paths

if TYPE_CHECKING:
    from kiro_crew.discord.transport_dispatch import DiscordDispatcher


async def _record_turn(self: DiscordDispatcher, record: TurnRecord) -> None:
    """Write one exchange to the live dashboard window and the transcript.

    The pipeline's ``record`` adapter: it decides WHEN this runs, and this
    decides WHAT it writes. A resumed ``dashboard:`` key carries the dashboard
    slot's privacy mode, not a Discord-local one, so a restricted session
    writes nothing at all -- decided on the loop before either writer, because
    ``project_channel_turn_live`` marks the slot dirty and a later slot flush
    would otherwise persist the restricted rows.

    The reply as the transcript will carry it is decided ONCE for both writers:
    whitespace alone (the steer-boundary separator) is no reply, and the live
    projection and the durable write must agree on that or a phantom assistant
    row lands in one and not the other. The live window goes FIRST, so the
    dashboard's own save serializes the turn in chronological position instead
    of appending it to the foreign tail.

    The turn's OUTCOME row follows the user's row when it produced no assistant
    text: the driver's empty-turn verdict on a completed turn -- recorded the way
    the dashboard runner records its own empty-turn card, so the transcript never
    ends on a question the model silently declined to answer -- or, for a turn
    that raised, the error it died with, after the partial reply the user
    already saw. That error takes the same redaction and cap as the member-memory
    refusal and the dashboard runner's own terminal-error row's role and class.
    """
    if await self._session_restricted(record.session_key):
        return
    # Circular import: the dashboard package imports the channel transports on
    # its boot path, so this edge only exists at call time.
    from kiro_crew.dashboard.channel_slots import (
        project_channel_row_live,
        project_channel_turn_live,
    )

    key = record.session_key
    reply = record.reply_text if record.reply_text.strip() else ""
    dashboard_state = getattr(self._session_resume, "dashboard_state", None)
    mirror_mids = project_channel_turn_live(dashboard_state, key, record.user_text, reply)
    extra_row: tuple[str, str, str, str | None] | None = None
    if record.kind == "failed":
        exc = record.error
        failure = "❌ " + (redact_local_paths(redact(str(exc) or exc.__class__.__name__))[0][:1000])
        error_mid = (
            project_channel_row_live(dashboard_state, key, "error", failure, "msg msg-err")
            if mirror_mids is not None
            else None
        )
        extra_row = ("error", failure, "msg msg-err", error_mid)
    elif record.notice:
        notice_mid = (
            project_channel_row_live(dashboard_state, key, "notice", record.notice, "msg msg-info")
            if mirror_mids is not None
            else None
        )
        extra_row = ("notice", record.notice, "msg msg-info", notice_mid)
    await asyncio.to_thread(
        self._persist_turn,
        key,
        record.user_text,
        reply,
        record.is_new,
        agent=record.agent,
        mirror_mids=mirror_mids,
        extra_row=extra_row,
    )


async def _session_restricted(self: DiscordDispatcher, session_key: str) -> bool:
    """True when this resumed session must leave no durable transcript.

    Discord can carry a ``dashboard:`` key, whose restriction lives on the
    dashboard slot rather than in a channel-local tracker. The shared
    predicate is also the upload ceiling's answer, so a conversation cannot
    refuse the file and then persist the text.

    With the tab closed, history restricts on an incognito/temporary marker and
    on an unreadable mode whose transcript EXISTS (an ambiguous stem, or a
    header no normal session wrote) — that is where an incognito session hides.
    ``unknown_denies`` is deliberately false here (and true for uploads) only
    so a truly ABSENT record still records: nothing on disk claims that session
    is restricted, and denying there would stop recording every conversation
    whose transcript is not yet written. A legacy header missing the field
    reads ``persistent``, so it never reaches the unknown case.

    The persisted probe is injected to keep ``messaging`` from importing
    ``dashboard``. This import stays local because dashboard boot imports the
    channel transports.
    """
    from kiro_crew.dashboard.handlers._shared import _probe_persisted_session

    return await session_is_restricted(
        getattr(self._session_resume, "dashboard_state", None),
        session_key,
        persisted_probe=_probe_persisted_session,
        unknown_denies=False,
    )


async def _uploads_restricted(self: DiscordDispatcher, session_key: str) -> bool:
    """True when this session must not ship local file bytes to Discord.

    The ladder and its fail-closed reasoning live in
    :func:`kiro_crew.messaging.upload_gate.uploads_restricted`, shared with the
    Telegram dispatcher; this supplies Discord's dashboard state and audit label.
    An approved guild thread is readable by every member who can view it, which
    is why the restricted ceiling matters at least as much here as elsewhere.

    The persisted-transcript probe is passed IN because ``messaging`` may not
    import ``dashboard``; this package may, so the import lives here. Kept
    function-local for the same reason it always was: the dashboard gateway
    imports the channel transports, so a module-level import would cycle.
    """
    from kiro_crew.dashboard.handlers._shared import _probe_persisted_session

    return await uploads_restricted(
        getattr(self._session_resume, "dashboard_state", None),
        session_key,
        channel_type="discord",
        persisted_probe=_probe_persisted_session,
    )


def _persist_turn(
    self: DiscordDispatcher,
    session_key: str,
    user_text: str,
    reply_text: str,
    is_new: bool,
    agent: str | None = None,
    mirror_mids: tuple[str, str] | None = None,
    extra_row: tuple[str, str, str, str | None] | None = None,
) -> None:
    """Record the turn to conversation_log (dashboard visibility + restart).

    *mirror_mids* is what ``project_channel_turn_live`` returned: the ids the
    live dashboard window minted for this turn's rows, or ``None`` when there
    was no live slot to mirror into. It carries BOTH facts, so there is no
    separate ``mirrored`` flag -- the presence of the tuple IS the flag, and a
    second parameter encoding the same thing could only ever disagree with it.

    With a live slot the disk write must be idempotent: that slot's own save
    re-serializes its window, so a plain append would persist the same message
    twice. The write goes under the SAME ids the window rows carry -- the
    dual-writer shape ``cron_inject`` uses -- because ``append_if_absent``
    skips only a body-equal row carrying the same mid. A fresh id there would
    never match the slot save's copy and the turn would land on disk twice.

    With no live slot nothing has the row yet, so it is a plain append under a
    newly minted id.

    *extra_row* is the turn's OUTCOME row when it produced no assistant text:
    ``(role, text, cls, mid)`` -- the driver's empty-turn ``notice`` on a
    completed turn, or the ``error`` a raised turn died with -- written after
    the user's row so the transcript never ends on an unanswered message. Its
    ``mid`` is the id ``project_channel_row_live`` minted for the live window
    (the same idempotency rule as *mirror_mids*), or ``None`` for a plain
    append.

    *reply_text* arrives already normalized by the caller (whitespace alone
    is ``""``), and is tested for truth here exactly as
    ``project_channel_turn_live`` tests it, so the live window and the disk
    can never disagree about whether an assistant row exists.
    """
    if self.conv_log is None:
        return
    if mirror_mids is not None:
        user_mid, assistant_mid = mirror_mids
        self.conv_log.append_if_absent(session_key, "user", user_text, agent=agent, mid=user_mid)
        if reply_text:
            self.conv_log.append_if_absent(
                session_key, "assistant", reply_text, agent=agent, mid=assistant_mid
            )
    else:
        self.conv_log.append(session_key, "user", user_text, agent=agent, mid=mint_row_mid())
        if reply_text:
            self.conv_log.append(
                session_key, "assistant", reply_text, agent=agent, mid=mint_row_mid()
            )
    if extra_row is not None:
        role, row_text, cls, mid = extra_row
        if mid:
            self.conv_log.append_if_absent(
                session_key, role, row_text, agent=agent, cls=cls, mid=mid
            )
        else:
            self.conv_log.append(
                session_key, role, row_text, agent=agent, cls=cls, mid=mint_row_mid()
            )
    if is_new:
        title = (user_text or "").strip().replace("\n", " ")[:40] or "Discord"
        self.conv_log.set_title(session_key, title)
