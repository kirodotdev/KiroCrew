"""The output seams the owner's one redaction setting governs.

``credential_redaction.json`` carries ONE owner setting, ``enabled``
(``security.redaction_switch``): credential redaction on or off. Its default is
:data:`~kiro_crew.security.redaction_switch.DEFAULT_REDACTION_ENABLED`.

This module is the ONE list of what ``enabled: false`` turns off besides the owner's
Files view (``redaction_switch.owner_view``). A seam is a call site, or a whole entry
point, that wraps its credential passes in :func:`output_seam`; inside that scope, with
the setting off, ``security.redaction``'s credential plan returns no spans, so the rule
table and the token-parameter pass are skipped and every value, a credential inside a
URL included, stays as written. Every redaction call
that does not open the scope keeps redacting whatever the owner chose. That includes
every refusal gate that decides by comparing ``redact(x)`` with ``x``, so a seam left
off this list fails closed.

What stays on with the setting off is :data:`ALWAYS_REDACTED`. The two site tables
name the modules: :data:`SEAM_SITES` every module that opens a seam, and
:data:`ALWAYS_REDACTED_SITES` every module that runs a credential pass in full inside
one, each with its reason. ``test_redaction_one_setting`` holds both to the source.

Where the verdict comes from
----------------------------
A process-wide value, :func:`publish`-ed by the gateway when it boots, on every
governance refresh and on every owner PUT
(``dashboard.handlers.credential_redaction``). Nothing on a seam reads a file: a
log record or a prompt build never waits on disk. A process that never publishes
(an MCP server, a cron script, the CLI) keeps the default, ``False``, and so
keeps redacting. That is the fail direction: a stale or missing verdict redacts.

An import leaf (stdlib only): ``log_redaction`` imports it, and that module
installs at CLI bootstrap, before the ``security`` package can load.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any

#: The earlier conversation replayed into the agent's prompt.
ACP_PROMPT = "acp_prompt"
#: The credential pass over operational log lines.
LOGS = "logs"
#: The dashboard chat turn: the live stream, the saved reply, tool and approval cards.
CHAT = "chat"
#: The saved transcript, written and read back.
TRANSCRIPT = "transcript"
#: What reaches a messaging channel: Slack, Discord, Telegram and the others.
CHANNELS = "channels"

#: Every output seam the setting covers, with the call sites that open it.
OUTPUT_SEAMS: Mapping[str, str] = MappingProxyType(
    {
        ACP_PROMPT: (
            "the conversation history replayed into the agent's prompt: "
            "context.build_session_replay and context_assembly.session_start"
        ),
        LOGS: (
            "the credential pass over log records: log_redaction.SecretRedactionFilter. "
            "Not platform.context.redact_log_via_context: its callers clip the result "
            "and send it to sinks that stay redacted"
        ),
        CHAT: (
            "the chat turn: dashboard.chat_runner._run_chat, the side-question and thread "
            "turns and the history reads that restore them (side._wire_side_row, "
            "chat_threads._redacted_reply and _parent_payload), the display battery, and "
            "the chat-history tool's output (mcp_core_parts.history) in a process that has "
            "the setting published"
        ),
        TRANSCRIPT: "the transcript save and its load pass: slot_persistence.message_entries",
        CHANNELS: (
            "channel egress: messaging.egress, the turn driver, the Slack turn, the "
            "dashboard's send and update handlers, a Slack code channel's display sink "
            "(slack.code_channels.surface.display_text: its wrap-up, pickers and cards) and "
            "a channel file upload's comment or caption (dashboard.file_api.channel_upload), "
            "and the channel sinks themselves, so a post made inside another seam (the "
            "owner's chat turn) is still a channel post: every MessagingTransport's sends "
            "(messaging.transport), the Slack renderers (slack.format.render_for_slack, "
            "render_one_for_slack and the OPTIONS choices), the shared channel display sink "
            "and chunker (messaging.renderer.display_safe, chunk_for_transport), the "
            "dashboard's mirror echo (chat_turn.recipient._prepare_mirror_msg) and its Slack "
            "task stream titles (chat_turn.tool_rows), and every bridged notification "
            "(notifications.bridge). Its posts reach channel members, so it never stands "
            "down unless the sender names the owner's 1:1 DM (SEAM_AUDIENCE, owner_alone)"
        ),
    }
)

#: Who reads a text. The owner's setting governs only what the owner ALONE reads; anything
#: a non-owner human or an app can read stays redacted whatever the setting
#: (the credential-redaction decision in ``docs/decisions/``).
#: The owner's own authenticated dashboard session (an owner socket, an owner request).
OWNER_DASHBOARD = "owner_dashboard"
#: The owner's Files view (``redaction_switch.owner_view``).
OWNER_FILES = "owner_files"
#: The model: the ACP prompt replay and the chat-history tool's output.
MODEL = "model"
#: The owner's 1:1 direct message with the crew bot, where the sender knows it is one.
OWNER_DM = "owner_dm"
#: Any channel or group with other members, which is what a channel post reaches unless
#: its sender names :data:`OWNER_DM`.
CHANNEL = "channel"
#: Everyone else: a Shared Crew teammate, an allowlist link, a ``!dashboard`` user, an app.
NON_OWNER = "non_owner"
#: The audiences the owner alone reads: the only ones the setting can leave unredacted.
OWNER_ALONE: frozenset[str] = frozenset({OWNER_DASHBOARD, OWNER_FILES, MODEL, OWNER_DM})


def owner_alone(audience: str) -> bool:
    """Whether *audience* is one the owner alone reads (:data:`OWNER_ALONE`).

    The one predicate every edge asks: a text the setting left unredacted reaches any
    other audience redacted in full, whatever the setting.
    """
    return audience in OWNER_ALONE


#: The audience each seam's text reaches. A seam whose audience is not the owner's
#: alone never stands down: :data:`CHANNELS` posts reach channel members.
SEAM_AUDIENCE: Mapping[str, str] = MappingProxyType(
    {
        ACP_PROMPT: MODEL,
        LOGS: OWNER_DASHBOARD,
        CHAT: OWNER_DASHBOARD,
        TRANSCRIPT: OWNER_DASHBOARD,
        CHANNELS: CHANNEL,
    }
)

#: Every module that opens each seam, as a path under ``kiro_crew/``: an
#: ``output_seam``/``in_seam`` call naming it. ``LOGS`` is the one seam no scope opens:
#: ``log_redaction.SecretRedactionFilter`` reads :func:`published_off` itself.
SEAM_SITES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        ACP_PROMPT: ("context.py", "context_assembly/session_start.py"),
        LOGS: ("log_redaction.py",),
        CHAT: (
            "dashboard/chat_runner.py",
            "dashboard/chat_threads.py",
            "dashboard/chat_utils_display.py",
            "dashboard/handlers/side.py",
            "mcp_core_parts/history.py",
        ),
        TRANSCRIPT: ("dashboard/slot_persistence/message_entries.py",),
        CHANNELS: (
            "dashboard/chat_turn/recipient.py",
            "dashboard/chat_turn/tool_rows.py",
            "dashboard/file_api/channel_upload.py",
            "dashboard/handlers/messaging.py",
            "messaging/driver.py",
            "messaging/egress.py",
            "messaging/renderer.py",
            "messaging/transport.py",
            "notifications/bridge.py",
            "slack/code_channels/surface.py",
            "slack/files.py",
            "slack/format.py",
            "slack/handler.py",
            "slack/transport_dispatch.py",
        ),
    }
)

#: What stays redacted with the setting off.
ALWAYS_REDACTED: tuple[str, ...] = (
    "vault secret values in logs (log_redaction.SecretRedactionFilter's literal pass)",
    "request-blocking decisions about a URL or command (security.exfil scan_exfiltration_urls, "
    "_contains_fixed_credential, the OAuth consent-URL gate): decisions, not text rewriting",
    "request-blocking gates, and every refusal gate that compares redact(x) with x",
    "every safety decision and audit row inside a seam, run in full under "
    "redaction_switch.always_scan: ALWAYS_REDACTED_SITES names each",
    "the dashboard's live log stream and its ring replay, which reach non-owner sockets "
    "(dashboard.handlers.updates_streams._stream_safe)",
    "a loaded companion's own extra patterns on log lines",
    "every process that never publishes the setting (an MCP server, a cron script, the "
    "CLI): it never reads the setting and keeps redacting, the chat-history tool included",
    "every channel or group post: its members are not the owner (owner_alone)",
    "every non-owner dashboard audience (a socket, a chat SSE stream or an HTTP read that "
    "is not the owner's: a teammate, an allowlist link, an app): dashboard.redaction_audience",
    "every Stop hook payload, the dashboard turn's and the Slack turn's",
    "text stored while the setting was on: a transcript row saved then keeps its masks, and "
    "an open session keeps the rows it showed then until it is reloaded",
)

#: Every module that runs a credential pass in full inside a seam, as a path under
#: ``kiro_crew/``, with why: a ``redaction_switch.always_scan`` scope, or a fresh
#: context that no seam reaches.
ALWAYS_REDACTED_SITES: Mapping[str, str] = MappingProxyType(
    {
        "dashboard/chat_turn/segment_flush.py": (
            "_log_glued_footer_text: the SEL preview of text glued after an options footer; "
            "the security event log stays redacted"
        ),
        "dashboard/chat_turn/turn_spec_hooks.py": (
            "_turn_stop_hook builds the Stop hook payload: a hook is a sink no seam lists"
        ),
        "dashboard/file_api/channel_upload.py": (
            "_gate_upload_file decides whether a file, by its name or its text, may leave "
            "for a channel: a safety decision"
        ),
        "dashboard/handlers/cron_board.py": (
            "_jobs_response: a job's last result and error read the display redaction cache "
            "for speed, whose pass opens the chat seam; they stay redacted like every other "
            "cron field, which no seam lists"
        ),
        "dashboard/handlers/updates_streams.py": (
            "_stream_safe: the live log stream and its ring replay reach every dashboard-user "
            "socket, a relayed teammate's and a log-scoped app's among them"
        ),
        "dashboard/redaction_audience.py": (
            "for_socket, SseEvents and redaction_audience_middleware: what a socket, a chat "
            "SSE stream or an HTTP request the owner does not alone read receives is redacted "
            "in full"
        ),
        "dashboard/slot_list_pass.py": (
            "_serialize: a shared slot list pass for any audience whose redaction does not "
            "stand down runs in full, since a trailing pass runs in its first caller's "
            "context and a teammate who joined it reads the same rows"
        ),
        "hook_runtime/hook_dispatch.py": (
            "hook_tool_input builds the PreToolUse payload's tool_input: a hook is a sink "
            "no seam lists"
        ),
        "platform/context.py": (
            "_scan_verdict: the upload gates' content scans decide whether a file may leave; "
            "redact_log_via_context: its callers clip the result for sinks that stay redacted"
        ),
        "secrets_mediation/dispatch.py": (
            "_refuse_if_outbound_redacted refuses a mediated request that carries a "
            "credential: a refusal gate"
        ),
        "security/blocked_links.py": (
            "_credential_clean decides whether a blocked link can open without sending a "
            "credential: a safety decision"
        ),
        "security/deny_gate.py": (
            "_audit_clip: the SEL rows of the deny and injection gates, scanned before "
            "their clip: the security event log stays redacted"
        ),
        "security/exfil.py": (
            "refuses: every refusal gate that compares redact(x) with x (an upload, an "
            "outbox name, a file name in a header, an elicitation value) runs its credential "
            "pass in full, beside the exfiltration URL scan: safety decisions"
        ),
        "sel.py": (
            "_redact_text: every security event log row is redacted in full whatever the "
            "setting, inside a seam too"
        ),
        "slack/code_channels/agent_picker.py": (
            "the SEL row of a refused agent switch: the security event log stays redacted"
        ),
        "slack/handler_runtime/turn.py": (
            "fire_stop_hooks builds the Slack turn's Stop hook payload: a hook is a sink no seam "
            "lists"
        ),
        "update_boot_apply.py": (
            "_run_apply_child: the boot-time update output is cut to its tail after the pass "
            "and stored for the dashboard's update notice, so a skipped pass would keep a "
            "fragment"
        ),
    }
)

#: Whether the owner has turned redaction off, as last published in this process.
_redaction_off = False
_generation = 0

#: The seam the calling context is inside with the setting off, or "".
_IN_SEAM: ContextVar[str] = ContextVar("kirocrew_redaction_output_seam", default="")


def publish(off: bool) -> None:
    """Record the setting's position for this process (the gateway's writers only)."""
    global _redaction_off, _generation
    if bool(off) != _redaction_off:
        _generation += 1
    _redaction_off = bool(off)


def generation() -> int:
    """How many times the published position changed: a cache keyed on it never outlives a flip."""
    return _generation


def published_off() -> bool:
    """Whether the setting was last published as off in this process."""
    return _redaction_off


@contextmanager
def output_seam(name: str, audience: str | None = None) -> Iterator[None]:
    """Mark the enclosed credential passes as the output seam *name*.

    With the setting off and the seam's audience one the owner alone reads (*audience*,
    else :data:`SEAM_AUDIENCE`, judged by :func:`owner_alone`),
    ``security.redaction_switch.credential_pass_bypassed`` is True inside the scope;
    otherwise the scope closes any seam around it, so a channel post made inside the
    owner's chat turn stays redacted unless its sender names :data:`OWNER_DM`. An
    unknown name is a programming error and raises, so a seam cannot open under a
    misspelling that this table does not list. Re-entrant and task-local.
    """
    if name not in OUTPUT_SEAMS:
        raise ValueError(f"not an output seam: {name!r}")
    if not _redaction_off or (audience is None and _IN_SEAM.get() == name):
        # Off, or inside the same seam already standing down: a channel sink that
        # opens CHANNELS itself keeps the owner's DM its sender named.
        yield
        return
    token = _IN_SEAM.set(name if owner_alone(audience or SEAM_AUDIENCE[name]) else "")
    try:
        yield
    finally:
        _IN_SEAM.reset(token)


def in_seam(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: run the coroutine function (or function) inside :func:`output_seam`.

    For an entry point whose body is too long to indent under a ``with``; generators
    are not supported.
    """
    if name not in OUTPUT_SEAMS:
        raise ValueError(f"not an output seam: {name!r}")

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        if fn.__code__.co_flags & _CO_COROUTINE:

            @functools.wraps(fn)
            async def run_async(*args: Any, **kwargs: Any) -> Any:
                if not _redaction_off:
                    return await fn(*args, **kwargs)
                with output_seam(name):
                    return await fn(*args, **kwargs)

            return run_async

        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            if not _redaction_off:
                return fn(*args, **kwargs)
            with output_seam(name):
                return fn(*args, **kwargs)

        return run

    return decorate


#: ``code.co_flags`` bit of a coroutine function, so no ``inspect`` import on this leaf.
_CO_COROUTINE = 0x80


def in_bypassed_seam() -> bool:
    """Whether the calling context is inside an output seam while the setting is off NOW.

    A turn opened while redaction was off stops bypassing the moment it is published on.
    """
    return _redaction_off and bool(_IN_SEAM.get())
