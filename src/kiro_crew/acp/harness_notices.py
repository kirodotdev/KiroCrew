"""What a harness tells the person outside its reply, read on ``AcpClient``.

Two standard ACP session updates, read for a backend in
``ACP_BACKENDS_HARNESS_NOTICES`` and only on this client's own session:

* ``notice`` -- a live advisory. claude-agent-acp sends it only to a client that
  declares ``clientCapabilities.session.notices``, which ``CLAUDE_CLIENT_CAPABILITIES``
  does, in this shape (claude-agent-acp 0.107.0 ``session-notices.js`` ``noticeUpdate``)::

      {"sessionUpdate": "notice", "severity": "info" | "warning",
       "title": ..., "description"?: ...}

  Its sources are a hook's informational message, a model fallback, a client MCP
  server that failed to start or needs sign-in, Auto mode falling back, a background
  task stopped by the user and a Fast mode change (``v1/acp-agent.js``
  ``reportMcpServerStartupFailures``, the ``informational`` and
  ``model_refusal_fallback`` arms, ``syncFastModeState``; ``session-mode.js``
  ``publishFallbackWarning``; ``async-tasks.js``). A client without the capability is
  sent the advisory as a blockquote ``agent_message_chunk`` instead
  (``noticeOrTranscriptUpdate``, ``transcriptBlock``). :func:`adapter_notice` reads the
  frame into a :class:`HarnessNotice` whose ``transcript`` is the notice rebuilt from
  its fields in the adapter's notice format (:func:`transcript_block`), and every
  ``AcpClient`` text reader reads it as that line (:func:`text_chunk`): a consumer of
  the turn's text (a channel reply, a subagent's result, a cron run) reads the advisory
  in the reply, and the streaming reader attaches the notice to the chunk, so the
  dashboard, which can draw a notice, draws a notice row instead. A ``notice`` the
  adapter addresses to a native child is that child's text on its card
  (``client_native_children._child_text``), and one read between prompts starts an
  agent-started turn (``client_transport._starts_agent_turn``).

* ``current_mode_update`` -- the session's permission mode moved: EnterPlanMode's
  ``plan``, the mode an approved plan sets, an Auto fallback's ``acceptEdits``, or the
  mode the clear-context restart after an approved plan starts in (``session-mode.js``
  ``publishCurrent``, ``publishFallbackState``; ``exit-plan.js``
  ``publishSessionState``). :func:`mode_notice_event` turns a change into an
  ``EVENT_HARNESS_NOTICE`` naming the mode by the name the session advertised for it
  (``modes.availableModes`` on ``session/new``, else the ``mode`` config option's
  choices), or by its id. A report naming the mode the session is already in is no
  change (:func:`_mode_before`), and neither is the mode it started in: the pin a
  claude session may get before its first prompt (``_pin_claude_starting_mode``) is
  answered and its frames drained before any turn reads frames, and the ``mode``
  config option the drain leaves names the pinned mode, which is what a first report
  is compared with.

Each function here whose first parameter is ``client`` reads and writes that
``AcpClient``'s own state: ``_harness_mode_names`` and ``_harness_mode`` (written:
the last mode reported, with the ``mode`` option's value at that moment),
``backend``, ``_session_id``, ``_acp_config_options`` and ``_extract_text_chunk``
(read).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kiro_crew.acp._dispatch import redact_text
from kiro_crew.acp.types import (
    EVENT_HARNESS_NOTICE,
    METHOD_SESSION_UPDATE,
    UPDATE_CURRENT_MODE,
    UPDATE_NOTICE,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_HARNESS_NOTICES

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: :attr:`HarnessNotice.kind` of an advisory the adapter composed (a ``notice``).
NOTICE_KIND_ADAPTER = "adapter"
#: :attr:`HarnessNotice.kind` of a permission mode the session moved to.
NOTICE_KIND_MODE = "mode"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
#: The ``severity`` values shown as a warning. ACP names ``info``, ``warning`` and
#: ``error`` and allows any other string; ``error`` is a warning here, because the
#: notice row's third tone is a policy block, and anything unknown reads as info.
_WARNING_SEVERITIES = frozenset({"warning", "error"})

logger = logging.getLogger(__name__)

#: Bounds on what a notice carries. The adapter keeps a title to 256 characters
#: (``MAX_NOTICE_TITLE_LENGTH``) and moves longer text into the description.
NOTICE_TITLE_MAX = 300
NOTICE_DESCRIPTION_MAX = 2000
#: A mode name or id is a short label ("Plan", "Accept edits").
MODE_NAME_MAX = 80
#: Modes kept from one ``session/new`` or ``session/load``, and the longest mode id
#: kept. A mode past either is not kept (a name falls back to the id).
MAX_SESSION_MODES = 32
MODE_ID_MAX = 64

#: A title ending in one of these is a sentence, so its description follows it as
#: the next one rather than after a colon (:func:`title_ends_sentence`).
SENTENCE_ENDS = (".", "!", "?", ":", "…")

#: The id of the ``configOptions`` select that holds the permission mode
#: (claude-agent-acp ``session-mode.js`` ``configOption``).
MODE_CONFIG_ID = "mode"


@dataclass(frozen=True)
class HarnessNotice:
    """One thing a harness reported for the person, ready for a notice row.

    ``title`` and ``description`` are redacted and bounded; ``transcript`` is the
    advisory as text (:func:`transcript_block`), which rides the text stream unredacted
    like any other chunk (its consumers redact text). A mode notice has no transcript:
    no text reader reads a mode change.
    """

    kind: str
    severity: str = SEVERITY_INFO
    title: str = ""
    description: str = ""
    transcript: str = ""
    mode_id: str = ""
    mode_name: str = ""


def _own_update(client: AcpClient, msg: JsonRpcMessage) -> dict[str, Any] | None:
    """The update of a ``session/update`` frame of *client*'s own session, or None."""
    if client.backend not in ACP_BACKENDS_HARNESS_NOTICES:
        return None
    params = getattr(msg, "params", None)
    if getattr(msg, "method", None) != METHOD_SESSION_UPDATE or not isinstance(params, dict):
        return None
    session_id = params.get("sessionId")
    own = client._session_id
    if isinstance(session_id, str) and session_id and own and session_id != own:
        return None
    update = params.get("update")
    return update if isinstance(update, dict) else None


def _plain(value: Any, limit: int) -> str:
    """*value* as redacted, stripped text cut to *limit*, or "" for a non-string."""
    if not isinstance(value, str):
        return ""
    return redact_text(value.strip())[:limit]


def title_ends_sentence(title: str) -> bool:
    """Whether *title* ends a sentence, so its description follows it with no colon."""
    return title.endswith(SENTENCE_ENDS)


def transcript_block(title: str, description: str) -> str:
    """A notice rebuilt from its fields in the adapter's notice format, for a text reader.

    ``**title:** description``, ``**title**`` with no description, each line quoted, in a
    paragraph of its own (``session-notices.js`` ``noticeTranscriptText``,
    ``transcriptBlock``). A title that ends a sentence (:func:`title_ends_sentence`) is
    followed by its description with no colon, as the notice row writes it.

    It is the text a client without the capability is sent for a client MCP server's
    startup and a stopped background task. For the others the adapter composes that
    text apart from the notice, and it differs:

    * an informational message is written ``**Warning:** <message>`` (``Notice``,
      ``Suggestion`` for those levels), a hook prefix every line repeats kept on the
      first line only, where the notice is the message's first line as the title and
      the rest as the description, inline markdown removed (``v1/acp-agent.js``
      3489-3495, ``session-notices.js`` ``informationalNotice``);
    * a model fallback is written ``**Model fallback:** <summary>`` with the explanation
      after it, where the notice's title is the summary itself unless it is too long
      (``modelRefusalFallback``);
    * Auto mode unavailable and Fast mode turned off write their reason in lower case,
      where the notice sentence-cases it (``session-mode.js`` ``publishFallbackWarning``,
      ``session-fast-mode.js``);
    * the adapter would write a sentence-ending title with a colon after it.

    The ``_meta.claudeCode`` the adapter puts on that text chunk is not rebuilt.
    """
    if not description:
        text = f"**{title}**"
    elif title_ends_sentence(title):
        text = f"**{title}** {description}"
    else:
        text = f"**{title}:** {description}"
    lines = text.strip().splitlines()
    return "\n\n" + "\n".join(f"> {line}" if line else ">" for line in lines) + "\n\n"


def parse_notice(update: Any) -> HarnessNotice | None:
    """The :class:`HarnessNotice` a ``notice`` update carries, or None.

    The title is required and stands alone; a frame with none falls back to its
    description as the title, and one with neither is not a notice.
    """
    if not isinstance(update, dict) or update.get("sessionUpdate") != UPDATE_NOTICE:
        return None
    title, description = update.get("title"), update.get("description")
    raw_title = title if isinstance(title, str) else ""
    raw_description = description if isinstance(description, str) else ""
    if not raw_title.strip():
        raw_title, raw_description = raw_description, ""
    if not raw_title.strip():
        return None
    severity = update.get("severity")
    return HarnessNotice(
        kind=NOTICE_KIND_ADAPTER,
        severity=SEVERITY_WARNING if severity in _WARNING_SEVERITIES else SEVERITY_INFO,
        title=_plain(raw_title, NOTICE_TITLE_MAX),
        description=_plain(raw_description, NOTICE_DESCRIPTION_MAX),
        transcript=transcript_block(raw_title.strip(), raw_description.strip()),
    )


def adapter_notice(client: AcpClient, msg: JsonRpcMessage) -> HarnessNotice | None:
    """The advisory a ``notice`` frame of *client*'s session carries, or None.

    Read through :func:`text_chunk`, which hands the reader its ``transcript`` as the
    frame's text.
    """
    update = _own_update(client, msg)
    return parse_notice(update) if update is not None else None


def text_chunk(
    client: AcpClient, msg: JsonRpcMessage
) -> tuple[str | None, bool, HarnessNotice | None]:
    """``client._extract_text_chunk(msg)``, with an adapter notice read as its line.

    Returns ``(text, is_thinking, notice)``: for a ``notice`` frame of *client*'s
    session, the notice's ``transcript`` as reply text and the notice itself, which
    the streaming reader attaches to the chunk; for any other frame, the extractor's
    answer and None.
    """
    notice = adapter_notice(client, msg)
    if notice is not None:
        return notice.transcript, False, notice
    text, is_thinking = client._extract_text_chunk(msg)
    return text, is_thinking, None


def _mode_option(client: AcpClient) -> dict[str, Any] | None:
    options = getattr(client, "_acp_config_options", None)
    if not isinstance(options, list):
        return None
    for option in options:
        if isinstance(option, dict) and option.get("id") == MODE_CONFIG_ID:
            return option
    return None


def _configured_mode(client: AcpClient) -> str | None:
    """The ``mode`` config option's current value, or None."""
    option = _mode_option(client)
    value = option.get("currentValue") if option else None
    return value if isinstance(value, str) and value else None


def mode_name(client: AcpClient, mode_id: str) -> str:
    """The name the session advertised for *mode_id*, redacted and bounded, else the id."""
    names = getattr(client, "_harness_mode_names", None) or {}
    name = names.get(mode_id)
    if not name:
        option = _mode_option(client) or {}
        for choice in option.get("options") or ():
            if isinstance(choice, dict) and choice.get("value") == mode_id:
                name = choice.get("name")
                break
    return _plain(name, MODE_NAME_MAX) or _plain(mode_id, MODE_NAME_MAX)


def note_session_modes(client: AcpClient, resp: Any) -> None:
    """Record the modes a ``session/new`` or ``session/load`` response advertises.

    The names answer :func:`mode_name`. The last report is forgotten: a new session is
    in the mode its ``mode`` config option names, which is read when a report comes.
    """
    if client.backend not in ACP_BACKENDS_HARNESS_NOTICES:
        return
    names: dict[str, str] = {}
    modes = resp.get("modes") if isinstance(resp, dict) else None
    available = modes.get("availableModes") if isinstance(modes, dict) else None
    refused = 0
    for mode in available if isinstance(available, list) else ():
        mode_id = mode.get("id") if isinstance(mode, dict) else None
        name = mode.get("name") if isinstance(mode, dict) else None
        if not isinstance(mode_id, str) or not isinstance(name, str) or not name.strip():
            continue
        if len(mode_id) > MODE_ID_MAX or len(names) >= MAX_SESSION_MODES:
            refused += 1
            continue
        names[mode_id] = _plain(name, MODE_NAME_MAX)
    if refused:
        logger.warning(
            "%d advertised mode(s) past %d modes or a %d-character id were not kept",
            refused,
            MAX_SESSION_MODES,
            MODE_ID_MAX,
        )
    client._harness_mode_names = names
    client._harness_mode = None


def _mode_before(client: AcpClient) -> str | None:
    """The mode the session was in before the report now being read.

    The adapter sends a mode change as a ``current_mode_update`` and then a
    ``config_option_update`` carrying the new ``mode`` value, so the config option
    lags the report it follows. The last report wins while the option still reads what
    it read when that report came; once the option moved (its update arrived, on any
    reader that tracks it), the option is the newer word.
    """
    reported, option_then = getattr(client, "_harness_mode", None) or (None, None)
    option_now = _configured_mode(client)
    if reported is not None and option_now == option_then:
        return reported
    return option_now


def mode_notice_event(client: AcpClient, msg: JsonRpcMessage) -> AcpEvent | None:
    """The ``EVENT_HARNESS_NOTICE`` a mode change of *client*'s session makes, or None.

    A ``current_mode_update`` naming the mode the session is already in
    (:func:`_mode_before`) is no change. A side effect of the frame, never a
    substitute.
    """
    update = _own_update(client, msg)
    if update is None or update.get("sessionUpdate") != UPDATE_CURRENT_MODE:
        return None
    mode_id = update.get("currentModeId")
    if not isinstance(mode_id, str) or not mode_id.strip() or len(mode_id) > MODE_ID_MAX:
        return None
    previous = _mode_before(client)
    client._harness_mode = (mode_id, _configured_mode(client))
    if mode_id == previous:
        return None
    notice = HarnessNotice(
        kind=NOTICE_KIND_MODE,
        mode_id=_plain(mode_id, MODE_NAME_MAX),
        mode_name=mode_name(client, mode_id),
    )
    return AcpEvent(kind=EVENT_HARNESS_NOTICE, harness_notice=notice)
