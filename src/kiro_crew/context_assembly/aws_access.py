"""The ``[AWS ACCESS]`` note: what works for AWS in a chat, said before the first command.

The common AWS failure is not a refusal. The agent runs ``aws``, is told there are
no credentials or that a profile could not be found, and stops or reaches for a
profile flag. Advice that rides only a refusal never shows then. This note says
what is true on the turn's own context rail, in the sentences the refusal advice
uses (:mod:`kiro_crew.deny_guidance`).

Rules the note is held to:

* **Once per context window.** A fresh conversation and the first turn after a
  compaction get it; later turns do not.
* **Told only when sent.** A session counts as told once a turn's settle reports
  that the turn landed (:func:`settle`, called from ``delivery_records``). Until
  then every build carries the note, so a caller that never settles its turns
  sends it on each of them.
* **Not for the guest agent, a minimal-context turn or a sub-agent's session.** A
  sub-agent gets :func:`subagent_line` on its first message instead.
* **A Bot's own chat gets its own line**: its worker does AWS work.
* **Nothing from the session.** No code that builds a prompt is handed the tier a
  session was spawned under, so the text names every tier, and no value from a
  session or from configuration is put into it.

Builder state read and written: ``_aws_access_told`` and ``_aws_access_lock``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.context import ContextBuilder

logger = logging.getLogger("kiro_crew.context")

#: The opener every note starts with (``context_blocks`` books it as ``aws_access``).
AWS_ACCESS_HEADER = "[AWS ACCESS]"

#: Sessions whose record is kept. In memory, oldest evicted: a session past the
#: bound is told once more.
_TOLD_SESSIONS = 512

#: Added when the owner bound at least one folder (``aws.project_profiles``). It
#: does not say that this chat's folder is bound: the binding is resolved where
#: the session is allocated, not here.
_BOUND_LINE = (
    " A folder the owner bound with `aws.project_profiles` starts its chat with "
    "`AWS_PROFILE` set to a profile name. That is a name, not a credential: where "
    "the owner's AWS files are hidden, `aws` says the profile could not be found."
)

_BOT_NOTE = (
    f"{AWS_ACCESS_HEADER} Your worker does AWS work. This chat mounts only Kiro "
    "Crew's own servers, so hand an AWS task to your worker."
)


def note_text(*, bound_folders: bool = False) -> str:
    """The note an ordinary chat gets, with the bound-folder sentence when asked."""
    from kiro_crew import deny_guidance

    return (
        f"{AWS_ACCESS_HEADER} What works for AWS in this chat. Run `aws` plainly first: "
        f"it uses the owner's default setup. {deny_guidance.AWS_PROFILE_LINE} "
        f"{deny_guidance.AWS_TIER_LINE} {deny_guidance.AWS_HIDDEN_LINE}"
        f"{_BOUND_LINE if bound_folders else ''}\n\n"
    )


def bot_note() -> str:
    """The line a Bot's own chat gets in place of the note."""
    return f"{_BOT_NOTE}\n\n"


def subagent_line() -> str:
    """The one line a sub-agent's first message carries in place of the note."""
    from kiro_crew import deny_guidance

    return (
        f"{AWS_ACCESS_HEADER} Run `aws` plainly. {deny_guidance.AWS_PROFILE_LINE} If "
        "`aws` finds no credentials, or says a profile could not be found, say so once "
        "in your result and do not retry it: this session may not be able to see the "
        "owner's AWS files.\n\n"
    )


def _folders_bound() -> bool:
    """Whether ``aws.project_profiles`` binds any folder. A failed read is no."""
    try:
        from kiro_crew.aws_project_profiles import configured_project_profiles
        from kiro_crew.config.loader import KiroCrewConfig

        return bool(configured_project_profiles(KiroCrewConfig.load()))
    except Exception:  # noqa: BLE001 - a note is never worth a failed turn
        logger.debug("aws.project_profiles unreadable for the AWS note", exc_info=True)
        return False


def turn_block(
    self: ContextBuilder, session_key: str | None, agent: str | None, *, rebuilt: bool = False
) -> str:
    """The ``[AWS ACCESS]`` note this turn's prompt carries, or ``""``.

    *rebuilt*: this turn's context window is a new one (a fresh conversation, or
    the first turn after a compaction), so what an earlier window was told is
    not in it and the note is sent again.

    ``""`` for the guest agent, for a sub-agent's session and for a turn with no
    session key, which has nothing to remember the note under.
    """
    from kiro_crew.agent_files import GUEST_AGENT_FILENAME
    from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT

    guest = GUEST_AGENT_FILENAME.removesuffix(".json")
    if not session_key or session_key.startswith("subagent:") or agent == guest:
        return ""
    key = self._cap_memo_key(session_key)
    with self._aws_access_lock:
        if rebuilt:
            self._aws_access_told.pop(key, None)
        if self._aws_access_told.get(key):
            self._aws_access_told.move_to_end(key)
            return ""
        # False until a settle says the prompt landed. Until then every build
        # carries the note, so a prompt that was built and dropped costs nothing
        # and two builds of one turn are the same.
        self._aws_access_told[key] = False
        self._aws_access_told.move_to_end(key)
        while len(self._aws_access_told) > _TOLD_SESSIONS:
            self._aws_access_told.popitem(last=False)
    if agent == MATE_CONDUCTOR_AGENT:
        return bot_note()
    return note_text(bound_folders=_folders_bound())


def settle(self: ContextBuilder, session_key: str | None, *, landed: bool) -> None:
    """Keep or take back the record this turn's build wrote for *session_key*.

    *landed*: the prompt reached the provider, so the session was told. Otherwise
    the record is dropped and the next turn sends the note again. A record an
    earlier turn settled is left alone, and a session with none is a no-op.
    """
    if not session_key:
        return
    key = self._cap_memo_key(session_key)
    with self._aws_access_lock:
        if self._aws_access_told.get(key) is False:
            if landed:
                self._aws_access_told[key] = True
            else:
                del self._aws_access_told[key]
