"""CLI chat subcommands."""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass

from kiro_crew.acp._dispatch import is_shell_kind
from kiro_crew.acp.client import AcpError, AcpTimeoutError
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import (
    CONTEXT_WARN_MARGIN_PCT,
    ConfigReadError,
    build_provider_factory,
    config_path,
    update_config_locked,
)
from kiro_crew.constants import BANNER, DATA_WARNING
from kiro_crew.hooks import (
    TOOL_DENY,
    HookManager,
    hooks_config_from_config_dict,
    mcp_identity_ref,
    target_paths,
)
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    LLMEvent,
    LLMProvider,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Session key this surface presents everywhere it is identified: to the
#: provider factory, to the PreToolUse gate, and to the SEL audit log. It is the
#: value ``sel._infer_source`` and ``validation`` already recognise as the CLI,
#: so the three must not drift apart.
_CLI_SESSION_KEY = "cli_chat"

#: The ACP runtime's canonical identity when no agent is configured.  The
#: provider normalizes an empty agent to this name; the governance gate must see
#: the same value so a task-bound ``kirocrew`` profile cannot be skipped merely
#: because the CLI relied on the provider's default.
_DEFAULT_KIRO_AGENT = "kirocrew"

#: The audit ``source`` ``_CLI_SESSION_KEY`` maps to. Passed explicitly so a
#: record is attributed to the CLI even if the key ever gains a suffix.
_CLI_SEL_SOURCE = "cli"

#: The only answer that approves a tool call, and the key advertised for
#: refusing one. Matched exactly -- see :func:`_prompt_allows`.
_ALLOW_KEY = "a"
_DENY_KEY = "d"

#: Audit codes. Stable and machine-readable, mirroring the dashboard's own
#: ``error="hook_deny"``: an audit record must not restate the path or command a
#: gate reason names.
_HOOK_DENY_CODE = "hook_deny"
_NONINTERACTIVE_CODE = "noninteractive"
_USER_DENY_CODE = "user_denied"
#: An ``execute``-kind request the trusted shell cache never confirmed, so no
#: command could be recovered to gate on. Distinct from ``hook_deny``: the gate
#: did not reject it, we refused to ASK about it.
_UNVERIFIED_SHELL_CODE = "unverified_shell"
#: The authorization gate itself could not produce a verdict. A broken gate is
#: not permission to run, so the request is refused and answered under its own
#: code rather than being left pending.
_GATE_FAILURE_CODE = "gate_failed"
#: The attended consent surface could not be rendered or read. A question the
#: operator could not reliably see and answer is a denial, never implicit consent.
_PROMPT_FAILURE_CODE = "prompt_failed"
#: The session died with the question unanswered -- distinct from a user who
#: said no, which is what an audit reader needs to be able to tell apart.
_ABORTED_CODE = "session_aborted"
#: The human allowed the call but the decision could not be persisted, so the
#: allow was downgraded to a refusal. Distinct from ``user_denied``: the operator
#: said yes, and an audit reader must not read this as a human refusal.
_UNAUDITABLE_CODE = "audit_unwritable"

#: Cap for the command line echoed above a permission prompt. Wide enough to
#: read a real command, narrow enough that a generated one-liner cannot flood
#: the terminal the question is being asked on.
_MAX_COMMAND_DISPLAY = 240


#: True once a prompt's await was cancelled. See :func:`_require_usable_stdin`.
_stdin_poisoned = False


class StdinPoisonedError(RuntimeError):
    """Raised when stdin is read after an abandoned prompt was left on it.

    A blocking terminal read cannot be retracted: cancelling the coroutine frees
    the coroutine, not the thread, and that reader still takes the next line the
    user types. So a cancelled prompt leaves no recoverable session -- every
    later entry point refuses rather than racing it for keystrokes.
    """


def _require_usable_stdin() -> None:
    """Guard every read of the terminal. Raises once a prompt was abandoned."""
    if _stdin_poisoned:
        raise StdinPoisonedError(
            "stdin was abandoned by a cancelled permission prompt; "
            "this session cannot read the terminal again"
        )


def _redacted(text: str) -> str:
    """Strip credentials and exfiltration URLs from text that leaves this turn.

    ``log_tool_invocation`` does not redact for its callers, so anything derived
    from model-authored prose or from a real command is scrubbed here before it
    reaches the audit log or the terminal.
    """
    text, _ = redact_exfiltration_urls(text or "")
    text, _ = redact_credentials(text)
    return text


def _for_audit(text: str) -> str:
    """Redact text and replace non-scalar Unicode before SEL persistence."""
    return _redacted(text).encode("utf-8", errors="backslashreplace").decode("utf-8")


#: ESC, the C0 set, DEL, the C1 set, and lone UTF-16 surrogate code points. The
#: controls can be interpreted as terminal protocol. A lone surrogate is not a
#: Unicode scalar value and cannot be encoded by UTF-8 or legacy Windows console
#: codecs, so letting one reach ``print`` can abandon the pending request with a
#: ``UnicodeEncodeError``.
_TERMINAL_CONTROLS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")


#: Emitted immediately before the permission prompt. Ordered, and every part is
#: load-bearing:
#:
#: 1. ``CAN`` (``\x18``) ABORTS any OSC/DCS/APC/PM string the previous output
#:    left OPEN. Ordinary streamed model output is printed raw, so a turn can end
#:    mid-sequence -- and while a string is open the terminal consumes everything
#:    that follows as its payload, which would swallow the rest of this reset AND
#:    the prompt itself. Resetting modes first would therefore reset nothing.
#:    Aborting is required rather than merely closing: a String Terminator
#:    (``ESC \``) would COMPLETE the pending string, so an unterminated ``OSC 52``
#:    in model output would be handed to the terminal as a finished command and
#:    set the user's clipboard. CAN discards the partial sequence instead, so the
#:    payload never executes. It is a no-op with no string open, so it is safe to
#:    send unconditionally, and unlike BEL it does not beep when nothing is open.
#: 2. SGR reset undoes concealed, inverted or colour-matched text.
#: 3. Show-cursor undoes a hidden cursor.
#:
#: This stays scoped to the authorization boundary rather than becoming a
#: rendering policy for all streamed output: it makes the question visible
#: without changing how anything else is displayed.
_PROMPT_TERMINAL_RESET = "\x18\x1b[0m\x1b[?25h"


def _terminal_reset() -> str:
    """The reset to emit before the permission prompt, or ``""`` when not a TTY.

    Escape sequences written to a pipe or a redirected file are literal bytes in
    someone's log, so the reset is emitted only when stdout is a terminal that
    can act on it.
    """
    try:
        return _PROMPT_TERMINAL_RESET if sys.stdout.isatty() else ""
    except (ValueError, OSError):
        # A detached or closed stream cannot be interrogated; treat it as not a
        # terminal rather than letting the check itself break the prompt.
        return ""


def _for_consent(text: str, *, stream: object | None = None) -> str:
    """Render untrusted text safe to show in a permission prompt.

    A permission prompt is the one place a human decides whether a tool may run,
    and the strings on it -- the tool title especially -- are model-authored. An
    escape sequence reaching the terminal from there is not merely cosmetic: OSC
    52 writes the clipboard, and CSI can move the cursor and overwrite what has
    already been drawn, so a title could repaint the question the user is
    answering and hide what is being approved. Neutralising controls keeps the
    prompt showing what it says it shows.

    Each control or lone surrogate becomes a space, then whitespace collapses,
    so the result is always a single line: a prompt is a question, and a title
    that spans lines can push it off screen just as an escape sequence can redraw
    it. The final text is round-tripped through the destination stream's codec
    with ``backslashreplace``. That preserves ordinary Unicode on UTF-8 terminals
    and renders unrepresentable characters as inert ASCII escapes on cp1252 and
    other legacy Windows codepages, even when the stream itself is strict.

    This is the authorization surface only. Ordinary streamed model output is
    printed raw by ``_send_and_print`` and is unchanged here -- that is a
    surface-wide rendering question, not part of answering a permission request.
    """
    rendered = " ".join(_TERMINAL_CONTROLS_RE.sub(" ", _redacted(text)).split())
    destination = stream if stream is not None else sys.stdout
    try:
        encoding_value = getattr(destination, "encoding", None)
    except Exception:
        encoding_value = None
    encoding = encoding_value if isinstance(encoding_value, str) and encoding_value else "utf-8"
    try:
        return rendered.encode(encoding, errors="backslashreplace").decode(encoding)
    except (LookupError, UnicodeError):
        # A detached/custom stream can advertise a broken codec. ASCII escapes
        # are the narrowest portable fallback; if the stream itself is unusable,
        # the caller converts that render failure into an explicit rejection.
        return rendered.encode("ascii", errors="backslashreplace").decode("ascii")


def _print_permission_notice(message: str) -> None:
    """Best-effort stderr notice after a permission request is already answered.

    A closed or broken terminal must not turn a completed rejection back into a
    failed turn. Callers sanitise every interpolated field with ``_for_consent``
    before constructing ``message``; this helper owns only the output failure.
    """
    try:
        print(message, file=sys.stderr)
    except Exception:
        logger.warning("Could not render the CLI permission notice", exc_info=True)


@dataclass(frozen=True)
class _ToolGate:
    """Kiro Crew's own PreToolUse gate, plus the identity it is asked under.

    ``session_key`` and ``agent`` are what let the gate resolve the governance
    ceiling ∩ profile for this surface; without them it can only apply the
    ceiling, and a profile narrowing (say) ``filesystem.write`` would silently
    not be enforced for tools the CLI approves.
    """

    hooks: HookManager
    session_key: str = _CLI_SESSION_KEY
    agent: str = ""


def _build_tool_gate(agent: str = "") -> _ToolGate:
    """Load the security gate for this CLI process.

    Opt-out state comes from the keystone ``denied_commands.json`` rather than
    the config's hooks section, so this mirrors the gateway's own construction
    instead of assembling a weaker manager.
    """
    return _ToolGate(
        hooks=HookManager(hooks_config_from_config_dict(KiroCrewConfig.load().hooks)),
        agent=agent,
    )


async def _chat(message: str | None, model: str | None, agent: str | None = None) -> None:
    """Run a single message or interactive chat session."""
    cfg = KiroCrewConfig.load()
    if model:
        cfg.agent.model = model
    channel_id = os.environ.get("KIROCREW_CHANNEL_ID") or None
    # Keep the provider and governance gate on one canonical identity.  ACP
    # resolves an omitted agent to ``kirocrew``; passing an empty string only to
    # the gate would therefore run that agent while bypassing its task profile.
    agent_name = agent or cfg.agent.default_agent or _DEFAULT_KIRO_AGENT
    provider: LLMProvider = build_provider_factory(cfg)(
        _CLI_SESSION_KEY, agent=agent_name, channel_id=channel_id
    )
    # Built once per process, not per request: a permission request must not
    # depend on a config read succeeding while the turn is parked.
    gate = _build_tool_gate(agent_name or "")
    # A permission prompt cancelled at the terminal raises through the turn by
    # design (the request is deliberately left unanswered, see
    # `_answer_permission`), so the teardown belongs in `finally` rather than on
    # the success path -- a Ctrl-C there would leave the backend running with
    # nothing owning it. `start()` is inside the try because it spawns the
    # backend before the handshake completes: a failure partway through leaves a
    # process that only `shutdown()` ends.
    try:
        await provider.start()

        if message:
            # `-m` is documented as a non-interactive single message, so this
            # path never stops to ask -- a permission request is denied rather
            # than blocking a caller that may be a script.
            await _send_and_print(provider, message, interactive=False, gate=gate)
        else:
            await _interactive(provider, cfg, gate=gate)
    finally:
        try:
            await provider.shutdown()
        except Exception:  # pragma: no cover - cleanup must not mask the outcome
            # A failed teardown is not worth replacing the exception that is
            # already propagating: raising here would discard the very
            # CancelledError the shutdown exists to clean up after.
            logger.debug("Provider shutdown failed during teardown", exc_info=True)
        finally:
            # Force GC so subprocess transports are collected while the loop is
            # still open, avoiding "Event loop is closed" noise on exit. Nested
            # so a raising shutdown cannot skip it.
            gc.collect()


def _run_chat(message: str | None, model: str | None, agent: str | None = None) -> None:
    """Run chat at the sync CLI boundary and render SIGINT as a clean exit."""
    try:
        asyncio.run(_chat(message, model, agent=agent))
    except KeyboardInterrupt:
        print("\nBye! 👻")


def _can_prompt(interactive: bool) -> bool:
    """True when this invocation may stop and ask a human.

    Two conditions, and both are load-bearing. ``-m`` is documented as
    ``Single message (non-interactive)``, so it must never block on stdin even
    from a terminal -- a script wrapped in a pty would otherwise hang on a
    question nobody is watching for. And a prompt nobody can see is a hang, not
    consent, so the interactive REPL still needs a real terminal on both ends.
    """
    return interactive and sys.stdin.isatty() and sys.stdout.isatty()


def _read_line_blocking(prompt: str) -> str:
    """Read one line from the controlling terminal. Blocks the calling thread.

    The single blocking seam of the permission prompt, kept off the event loop
    by :func:`_read_line`. It reads through a PRIVATE file object over a dup of
    the stdin descriptor rather than ``sys.stdin``: an abandoned read parks here
    forever, and a thread parked inside ``sys.stdin`` holds a buffer lock the
    interpreter must acquire to finalize it, aborting the process when it
    cannot. A private buffer is nobody else's to finalize.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    with open(
        os.dup(sys.stdin.fileno()),
        "r",
        encoding=sys.stdin.encoding or "utf-8",
        errors="replace",
        closefd=True,
    ) as stream:
        return stream.readline()


async def _read_line(prompt: str) -> str:
    """Await one line of terminal input without stalling the event loop.

    The request is answered from INSIDE an active provider stream, not at an
    idle REPL: the ACP runtime is holding a reader task on the backend's stdout
    and a drain task on its stderr, and a read on the loop thread stops draining
    both for as long as the human takes to answer. The read runs on an owned
    daemon thread rather than the default executor, which ``asyncio.run`` joins
    at shutdown -- a worker still parked in ``read`` would wedge the interpreter.

    Cancelling this await does not retract the thread, so it poisons stdin (see
    :class:`StdinPoisonedError`) and re-raises. Returns ``""`` on EOF or any read
    failure, which every caller must treat as a deny.
    """
    _require_usable_stdin()
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def _worker() -> None:
        try:
            line = _read_line_blocking(prompt)
        except BaseException:  # EOF, a stdin with no descriptor to dup, a decode failure
            line = ""

        def _deliver() -> None:
            if not future.done():
                future.set_result(line)

        try:
            loop.call_soon_threadsafe(_deliver)
        except RuntimeError:
            # The loop is already closed: this prompt was abandoned and the
            # answer has nowhere to go. Dropping it is correct -- nothing is
            # waiting, and no tool may be approved on it.
            pass

    threading.Thread(target=_worker, daemon=True, name="kirocrew-permission-prompt").start()
    try:
        return await future
    except asyncio.CancelledError:
        global _stdin_poisoned
        _stdin_poisoned = True
        raise


def _display_command(command: str) -> str:
    """Render what will actually run, for the human deciding whether it may.

    Collapsed to one line and capped: the value is a real shell command, which
    may be a multi-line heredoc or a generated one-liner long enough to push the
    question itself off the screen.
    """
    text = _for_consent(command)
    if len(text) > _MAX_COMMAND_DISPLAY:
        text = f"{text[:_MAX_COMMAND_DISPLAY]}... [truncated]"
    return text


def _target_path(event: LLMEvent) -> str:
    """The file path this call acts on, from the request's own arguments.

    ``raw_tool_params`` is the argument dict the backend sent for the call, not
    model prose about it, so it is the same class of trusted signal as
    ``shell_command`` -- and it is where the gate reads a path from to deny
    ``~/.ssh`` or a write-protected config.

    Resolved through ``hooks.target_paths``, the SAME helper the gate uses, so the
    two cannot drift about which argument names carry a target. An earlier revision
    duplicated the spellings here and claimed parity in a comment; the gate's
    sensitive-path keystone was in fact reading two of the three, so a
    ``filePath`` target was displayed as vetted while never having been gated.

    The FIRST path is shown when a call carries several, with a ``(+N more)``
    count appended so the human is never told one file while consenting to a
    batch — before the extraction became nesting-aware a multi-target batch
    yielded nothing here, so the single-path display was honest by
    construction; now that every nested target is collected, a bare first path
    would under-disclose. The gate denies on ANY of them, so anything forbidden
    has already been refused before this runs; what is left is a disclosure
    choice, and a prompt is a question rather than a manifest.

    Returns ``""`` when the call carries no path, which is the ordinary case for
    a tool that acts on no file. That is deliberately NOT a refusal: most builtin
    calls (a memory write, a tag creation) legitimately have no path, so denying
    on absence would refuse them all to close a gap that only exists for tools
    which DO name a file.
    """
    found = target_paths(event.raw_tool_params)
    if not found:
        return ""
    if len(found) == 1:
        return found[0]
    return f"{found[0]} (+{len(found) - 1} more)"


def _display_path(path: str) -> str:
    """Render a target path for the human deciding whether it may be written.

    Capped and collapsed like a command, and for the same reason: the value is
    attacker-influenceable text on an authorization prompt, so it must not be
    able to push the question off the screen.

    Local paths are NOT redacted, matching the command line above it -- this is
    the operator's own terminal and seeing the real path IS the consent. Only
    credential and exfiltration-URL shapes are scrubbed, by ``_for_consent``.
    """
    text = _for_consent(path)
    if len(text) > _MAX_COMMAND_DISPLAY:
        text = f"{text[:_MAX_COMMAND_DISPLAY]}... [truncated]"
    return text


def _unverifiable_shell(event: LLMEvent) -> bool:
    """True when the request claims to execute a command we cannot verify.

    ``is_shell`` is set ONLY from the trusted cache the preceding ``tool_call``
    populated, so a cache miss leaves it False even for a real shell call --
    and ``shell_command`` returns None whenever ``is_shell`` is False. Gating
    that on the title alone is exactly the bypass the gate exists to prevent:
    the title may be LLM-authored prose over a sensitive command.

    The payload's own ``tool_kind`` is agent-influenced, so it must never WAIVE
    a check -- but reading it to DENY is sound, because an agent that forges
    ``execute`` only earns a refusal. That asymmetry is why the trusted cache
    stays the only thing that can set ``is_shell`` True.

    The cost is a refused call when the cache genuinely missed. For an
    authorization gate that is the correct direction: a refusal the user can
    retry, rather than a command approved on a description of itself.
    """
    if event.is_shell:
        # Trusted signal: the gate's own deny-by-default backstop covers an
        # unrecoverable command from here.
        return False
    # No classification happened AT ALL: the preceding ``tool_call`` carried no
    # resolvable ``kind``, so nothing was cached and ``is_shell`` is the miss
    # default rather than a resolved "not a shell tool". Reading the payload's own
    # ``kind`` here would let an uncached shell request labelled ``read`` past the
    # check, and the human would then be asked to approve a title with no command
    # behind it. An absent classification is not a negative one -- the same
    # distinction ``child_low_fidelity`` already draws on this flag.
    if not event.shell_classified:
        return True
    # Normalised before the shared check so a cosmetic variant still denies --
    # widening a fail-closed test is safe in a way widening an allow is not.
    # ``tool_kind`` is relayed verbatim from ACP, so a backend may supply a
    # non-string (``kind: 1``); coerced here rather than trusted, because an
    # AttributeError on this path would abort the turn without answering the
    # permission request.
    return is_shell_kind(_kind_text(event))


def _kind_text(event: LLMEvent) -> str:
    """The ACP tool kind as comparable text, or ``""`` when it is not usable.

    ACP relays ``toolCall.kind`` verbatim, so the value is agent-influenced and
    need not be a string. Every consumer here compares it as text, and an
    unusable kind must read as absent rather than raise: a raise would leave the
    permission request unanswered, which is the hang this path exists to end.
    An absent kind cannot match the read-only allow-list, so the human is asked.
    """
    kind = event.tool_kind
    return kind.strip().lower() if isinstance(kind, str) else ""


async def _prompt_allows(event: LLMEvent) -> bool:
    """Ask the human, and return True ONLY for the exact allow token.
    Matched exactly rather than by first letter: a prefix match reads ``abort``
    as an allow, which is the opposite of what the person typing it means.
    Anything else -- an unrecognised word, a blank line, EOF -- denies, so the
    safe answer is the one a user gets by doing nothing.

    A shell call also shows its command, and any call with a trusted identity
    shows that identity. ``title`` is LLM-authored prose, so approving on it
    alone asks the user to consent to a description rather than to what runs --
    the same reason the security gate keys on ``shell_command`` and
    ``mcp_server_name`` rather than the title. Only those non-model-authored
    fields are shown, never the whole tool input: this is the question, not a
    detail panel.
    """
    print(
        f"{_terminal_reset()}\nPermission required: "
        f"{_for_consent(event.title, stream=sys.stdout) or 'tool call'}"
    )
    command = event.shell_command if event.is_shell else None
    if command:
        print(f"Command: {_display_command(command)}")
    # kiro-cli sets these from ``_meta`` for MCP-served calls only, so unlike
    # the title they cannot be chosen by the model to describe the call as
    # something milder than it is.
    if event.mcp_server_name:
        server = _for_consent(event.mcp_server_name, stream=sys.stdout)
        tool = _for_consent(event.tool_name, stream=sys.stdout) or "unnamed tool"
        print(f"MCP tool: {server} / {tool}")
    elif event.tool_name:
        # A builtin (non-MCP) call has no server to name, but ``tool_name`` is
        # still the trusted ``_meta.kiro`` identity rather than model prose. Left
        # unshown, a file-write reaches the human as its title alone, which is
        # the description-not-substance problem the shell and MCP lines exist to
        # avoid.
        print(f"Tool: {_for_consent(event.tool_name, stream=sys.stdout)}")
    # The trusted identity says WHICH tool runs; it does not say what it runs
    # against. ``fs_write`` under a benign title is consent to a verb, and a
    # write to an ordinary valuable file is not disclosed by the verb alone --
    # the gate's sensitive-path and write-protected-config denials cover the
    # named-dangerous paths, so what is left for the human is exactly the file
    # no rule speaks for. Printed for any call that carries a path, not just an
    # edit-kind one: the kind is agent-influenced, and disclosure can only ever
    # inform the decision.
    target = _target_path(event)
    if target:
        print(f"Path: {_display_path(target)}")
    answer = await _read_line(f"   [{_ALLOW_KEY}] allow once  [{_DENY_KEY}] deny (default): ")
    return answer.strip().lower() == _ALLOW_KEY


async def _audit_off_loop(
    gate: _ToolGate, event: LLMEvent, outcome: str, *, error: str = "", critical: bool = False
) -> None:
    """Await :func:`_audit` on a worker thread.

    ``critical`` selects SEL's audit-or-deny contract: the event is written
    synchronously and a persistence failure is RE-RAISED so the caller can refuse
    the action rather than let it run unaudited. Only the approval path passes it
    -- see :func:`_answer_permission`.

    ``sel()`` initializes the audit log on first use -- opening it, and reading
    it to recover the running HMAC chain -- so the first permission of a fresh
    chat pays a filesystem cost inside this call, and slow or corrupt storage
    makes it an unbounded one. This coroutine shares its loop with the ACP
    reader and stderr-drain tasks, exactly as ``on_tool_call`` does, so the same
    off-loop treatment applies for the same reason: a blocking write here stops
    draining the backend and freezes the turn the audit is about.

    Used on EVERY decision path, including the cancellation teardown, which wraps
    it in :func:`asyncio.shield` so the record still lands if another
    cancellation arrives mid-write. No caller audits synchronously on the loop.
    """
    await asyncio.to_thread(_audit, gate, event, outcome, error=error, critical=critical)


async def _audit_refusal(gate: _ToolGate, event: LLMEvent, *, error: str) -> None:
    """Audit a REFUSAL without letting the audit become the outcome.

    Every caller goes on to ``reject_tool``, and that call is what ends the turn:
    the backend holds it open until the request is answered. So an exception from
    the audit must not escape, or it skips the rejection and abandons the request
    unanswered -- the hang this whole path exists to end, reached by way of the
    bookkeeping rather than the decision.

    ``critical`` is deliberately NOT used here, and the asymmetry with the approval
    path is the point. Audit-or-deny only has meaning where the alternative is
    EXECUTION; a refusal is already refusing, so a lost record cannot authorize
    anything. Refusal paths therefore fail safe when bookkeeping is unavailable.
    """
    try:
        await _audit_off_loop(gate, event, "denied", error=error)
    except Exception:
        # Warning, not debug: an audit sink that cannot record refusals is an
        # operational fault worth seeing, even though it must not stop the refusal.
        logger.warning("SEL audit failed for a refusal; refusing anyway", exc_info=True)


async def _reject_internal_failure(
    provider: LLMProvider,
    gate: _ToolGate,
    event: LLMEvent,
    *,
    error: str,
    notice: str,
) -> None:
    """Answer a request whose gate or consent UI failed before a decision.

    The audit is best-effort because the safe outcome is already a refusal. The
    transport response comes before the notice so a broken output stream cannot
    leave the backend waiting on a question nobody can answer.
    """
    await _audit_refusal(gate, event, error=error)
    await provider.reject_tool(event.request_id)
    _print_permission_notice(notice)


def _audit(
    gate: _ToolGate, event: LLMEvent, outcome: str, *, error: str = "", critical: bool = False
) -> None:
    """Record a permission decision in the SEL audit log.

    ``critical`` is SEL's audit-or-deny flag: it writes synchronously and raises
    on a persistence failure instead of swallowing it in the background writer.

    Callers on the event loop must go through :func:`_audit_off_loop`; this
    function performs blocking I/O.

    Called BEFORE the matching ``approve_tool``/``reject_tool``: a transport
    failure must not be able to erase the record of a security decision that was
    already made.

    ``error`` is a stable machine code, never a gate reason: a reason carries
    the path or command that triggered it, and an audit record is not a place to
    copy the thing being protected.

    The tool identity prefers ``event.tool_name`` -- the canonical,
    non-model-authored name from ``_meta.kiro`` -- and falls back to the
    LLM-authored title only when the backend supplies none.

    For an MCP call that name is unique only WITHIN its server, so two servers
    exposing the same tool name would otherwise produce indistinguishable
    records. The canonical ``@server/tool`` reference carries both halves. It is
    used in preference to an ``mcp__server__tool`` composition because that form
    re-splits on the last ``__``, so a server or tool name containing ``__``
    collapses two distinct identities onto one string.

    Whichever identity wins is scrubbed before it is logged, as is the kind:
    these fields are served over ``/api/sel/events`` and the SEL writer does not
    redact for its callers, so a credential embedded in a tool name would
    otherwise be persisted and exposed.
    """
    identity = mcp_identity_ref(event.mcp_server_name, event.tool_name)
    # Redact whatever identity wins, not just the title branch. These fields reach
    # ``/api/sel/events``, and ``log_tool_invocation`` does not scrub for its
    # callers, so a credential-bearing tool name or kind would be persisted
    # verbatim. Scrubbing is lossless for an ordinary identity -- it only rewrites
    # credential and exfiltration-URL shapes -- so the canonical ``@server/tool``
    # precision is preserved. The kind is normalised first: it is relayed verbatim
    # from ACP and may not even be a string.
    subject = _for_audit(identity or event.tool_name or event.title)
    sel().log_tool_invocation(
        session_key=gate.session_key,
        agent=gate.agent or _DEFAULT_KIRO_AGENT,
        source=_CLI_SEL_SOURCE,
        tool_name=subject or "unknown",
        tool_kind=_for_audit(_kind_text(event)),
        outcome=outcome,
        request_id=event.request_id,
        error=error,
        critical=critical,
    )


async def _answer_permission(
    provider: LLMProvider,
    event: LLMEvent,
    *,
    interactive: bool,
    gate: _ToolGate | None = None,
) -> None:
    """Answer a pending permission request so the backend can resume the turn.

    Answering one is an authorization decision, so Kiro Crew's own PreToolUse
    gate runs first and a human is asked only about what survives it. That gate
    is the shared :class:`~kiro_crew.hooks.HookManager` -- sensitive paths, the
    built-in denied commands, the governance ceiling -- never a second copy of
    those rules living here. It is fed the event's NON-model-authored fields,
    not just ``title``: for a shell tool the title may be an LLM-authored
    description, so a dangerous command behind a benign label is exactly what
    keying on the title alone would let through (see ``AcpEvent.shell_command``).

    Its verdict is a deny CEILING: ``TOOL_AUTO_APPROVE`` still asks, because
    honouring it here would add a second path that runs a tool with no human
    confirmation. There is no persistent-approval option either -- ``always``
    asks the backend to stop SENDING these requests, and a request never sent is
    a call this gate never sees and never audits.

    The answer always goes through the provider's ``approve_tool`` /
    ``reject_tool``: the ACP layer records the option ids the agent advertised
    for this request, and those differ per backend.

    Every interpolated field is made representable in its destination stream by
    :func:`_for_consent`, independently of the stream's configured error handler.
    This covers strict UTF-8 and legacy Windows codepages as well as CPython's
    usual ``backslashreplace`` stderr. A gate, prompt, render, or audit failure
    always becomes an explicit rejection; only cancellation keeps the existing
    session-abort contract, where provider teardown owns the pending request.
    """
    gate = gate or _build_tool_gate()
    title = event.title or "tool call"

    # Off-loop: the gate resolves the active governance profile, which stats and
    # reads ``profiles/`` on the way to a verdict. That is a filesystem walk, not
    # computation, and this coroutine shares its loop with the ACP reader and
    # drain tasks -- on slow or network storage a synchronous call here stalls the
    # whole session, not just this prompt.
    try:
        decision = await asyncio.to_thread(
            gate.hooks.on_tool_call,
            event.title,
            session_key=gate.session_key,
            agent=gate.agent,
            tool_kind=_kind_text(event),
            raw_params=event.raw_tool_params,
            command=event.shell_command,
            is_shell=event.is_shell,
            mcp_server_name=event.mcp_server_name,
            mcp_tool_name=event.tool_name,
        )
    except Exception:
        logger.warning("CLI permission gate failed; refusing the request", exc_info=True)
        await _reject_internal_failure(
            provider,
            gate,
            event,
            error=_GATE_FAILURE_CODE,
            notice=(
                "\nDenied automatically: the security gate could not verify this tool call.\n"
                "   Fix the gate error, then retry the tool call."
            ),
        )
        return
    if decision.action == TOOL_DENY:
        # Not a question for the user: a policy denial is not theirs to
        # override from here. The audit carries a stable code; the reason is
        # for the person at the terminal, and is sanitised on the way there.
        await _audit_refusal(gate, event, error=_HOOK_DENY_CODE)
        await provider.reject_tool(event.request_id)
        try:
            reason = (
                _for_consent(decision.reason, stream=sys.stderr) or "blocked by security policy"
            )
            safe_title = _for_consent(title, stream=sys.stderr)
            _print_permission_notice(f"\nBlocked by security policy: {safe_title} -- {reason}")
        except Exception:
            logger.warning("Could not prepare the CLI policy-denial notice", exc_info=True)
        return

    if _unverifiable_shell(event):
        # Refusing to ASK, not a gate rejection: with no trusted shell signal
        # the only thing left to show the human is the LLM-authored title, and
        # consent to a description is not consent to what runs.
        await _audit_refusal(gate, event, error=_UNVERIFIED_SHELL_CODE)
        await provider.reject_tool(event.request_id)
        try:
            safe_title = _for_consent(title, stream=sys.stderr)
            _print_permission_notice(
                f"\nDenied automatically: {safe_title} claims to run a command, "
                "but its command could not be verified.\n"
                "   Ask the agent to retry the tool call."
            )
        except Exception:
            logger.warning("Could not prepare the CLI shell-denial notice", exc_info=True)
        return

    try:
        can_prompt = _can_prompt(interactive)
    except Exception:
        logger.warning("CLI permission prompt availability check failed", exc_info=True)
        await _reject_internal_failure(
            provider,
            gate,
            event,
            error=_PROMPT_FAILURE_CODE,
            notice=(
                "\nDenied automatically: the permission prompt could not be opened.\n"
                "   Retry from a usable terminal."
            ),
        )
        return

    if not can_prompt:
        await _audit_refusal(gate, event, error=_NONINTERACTIVE_CODE)
        await provider.reject_tool(event.request_id)
        try:
            safe_title = _for_consent(title, stream=sys.stderr)
            _print_permission_notice(
                f"\nDenied automatically: {safe_title} needs approval, "
                "and this invocation cannot ask.\n"
                "   Run `kirocrew chat` from a terminal to approve tool calls."
            )
        except Exception:
            logger.warning("Could not prepare the CLI noninteractive-denial notice", exc_info=True)
        return

    try:
        allowed = await _prompt_allows(event)
    except (StdinPoisonedError, asyncio.CancelledError):
        # The BACKEND is deliberately not answered here. Answering means awaiting
        # a transport, and a wedged one would park this teardown on a call that
        # may never return, so the Ctrl-C that asked for it could never land. The
        # provider is torn down with the session, so the unanswered request dies
        # with it.
        #
        # The AUDIT is a different matter, and an earlier version of this comment
        # wrongly generalised the transport rule to cover it. It is not a
        # transport: it is local SEL I/O with a bounded caller, and running it
        # synchronously here blocks the loop -- which still owns the ACP reader
        # and stderr-drain tasks -- for as long as the audit store takes. Cold
        # ``sel()`` initialization replays the log to recover the HMAC chain, so
        # on slow storage that is exactly the freeze this whole path exists to
        # end, just relocated to the exit.
        #
        # Shielded because this record is the one that distinguishes "the session
        # died with the question unanswered" from "the user said no", and that is
        # the harder of the two for an audit reader to reconstruct. A plain await
        # would be re-cancelled the moment another cancellation arrives and the
        # record would be dropped; the shield lets the write run to completion on
        # its worker thread regardless. ``CancelledError`` from the shielded wait
        # is therefore expected rather than exceptional -- it is caught so the
        # bare ``raise`` below still re-delivers the ORIGINAL cancellation.
        try:
            await asyncio.shield(_audit_off_loop(gate, event, "denied", error=_ABORTED_CODE))
        except asyncio.CancelledError:
            logger.debug("SEL audit outlived the cancelled turn; it completes off-loop")
        except Exception:  # pragma: no cover - an audit failure must not mask it
            logger.debug("SEL audit failed during teardown", exc_info=True)
        raise
    except Exception:
        logger.warning("CLI permission prompt failed; refusing the request", exc_info=True)
        await _reject_internal_failure(
            provider,
            gate,
            event,
            error=_PROMPT_FAILURE_CODE,
            notice=(
                "\nDenied automatically: the permission prompt could not be rendered or read.\n"
                "   Retry the tool call from a usable terminal."
            ),
        )
        return

    if allowed:
        # AUDIT-OR-DENY, and only here. ``critical=True`` writes the record
        # synchronously and re-raises a persistence failure (read-only SEL file,
        # full disk) instead of letting the background writer swallow it. Without
        # it an unwritable log means the tool RUNS while the only record that a
        # human authorized it is silently dropped -- the one outcome on this
        # surface that cannot be reconstructed afterwards, because the consent was
        # a keystroke that left no other trace.
        #
        # The failure is converted to a REFUSAL here rather than allowed to
        # propagate. Letting it escape ``_answer_permission`` would abandon the
        # request unanswered, and the backend holds the turn open until it is
        # answered -- the exact hang this whole path exists to end. So an audit
        # failure denies the call, which is the same direction the gate fails in.
        #
        # ``error`` is a distinct code, not ``user_denied``: the operator said
        # yes, and an audit reader must not be told they refused. That record is
        # best-effort by necessity -- the writer that just failed is the only one
        # available -- so it is attempted and its own failure only logged.
        try:
            await _audit_off_loop(gate, event, "allowed", critical=True)
        except Exception:
            logger.warning("SEL audit unwritable; refusing the approved call", exc_info=True)
            await _audit_refusal(gate, event, error=_UNAUDITABLE_CODE)
            await provider.reject_tool(event.request_id)
            try:
                safe_title = _for_consent(title, stream=sys.stderr)
                _print_permission_notice(
                    f"\nDenied automatically: {safe_title} was approved, but the "
                    "decision could not be recorded.\n"
                    "   Fix the security event log, then retry the tool call."
                )
            except Exception:
                logger.warning("Could not prepare the CLI audit-denial notice", exc_info=True)
            return
        await provider.approve_tool(event.request_id)
    else:
        # Deliberately NOT critical, and the asymmetry is the point: this call is
        # already being refused, so a lost record cannot authorize anything. Making
        # the deny paths audit-or-deny would trade availability for no security
        # gain -- it would turn a refusal into an error. Fail-closed only has
        # meaning where the alternative is execution.
        await _audit_refusal(gate, event, error=_USER_DENY_CODE)
        await provider.reject_tool(event.request_id)


async def _send_and_print(
    provider: LLMProvider,
    message: str,
    *,
    interactive: bool = False,
    gate: _ToolGate | None = None,
) -> None:
    """Stream a single message to stdout, handling errors and timeouts.

    ``interactive`` says whether the caller is the REPL, which may stop and ask
    a human, or single-message mode, which may not. It is passed explicitly
    rather than inferred from a TTY check: only the caller knows which command
    mode is running, and ``-m`` is non-interactive even from a terminal.

    ``gate`` is the security gate permission requests are decided against. None
    builds the process default on first use, so a caller that never sees a
    request pays nothing for it.
    """
    try:
        async for event in provider.stream(message):
            if event.kind == EVENT_TEXT_CHUNK:
                print(event.text, end="", flush=True)
            elif event.kind == EVENT_PERMISSION_REQUEST:
                # The backend holds the turn open until this is answered, so an
                # unhandled request is not a missed prompt -- it is a turn that
                # never ends.
                await _answer_permission(provider, event, interactive=interactive, gate=gate)
            elif event.kind == EVENT_COMPLETE:
                break
        print()  # final newline
    except AcpTimeoutError as e:
        if e.partial_output:
            print(e.partial_output)
        print("\n⏱️  Response timed out.", file=sys.stderr)
        sys.exit(1)
    except AcpError as e:
        print(f"\n❌ {e}", file=sys.stderr)
        sys.exit(1)


async def _interactive(
    provider: LLMProvider, cfg: KiroCrewConfig, *, gate: _ToolGate | None = None
) -> None:
    """REPL loop — read user input, stream responses, auto-compact at configured threshold."""
    print(BANNER)
    print(DATA_WARNING)
    print()

    print("Type your message (Ctrl+D or 'exit' to quit)\n")

    while True:
        # A cancelled permission prompt left a reader on stdin that this call
        # would race for the user's keystrokes, so the session ends here instead
        # of silently losing lines to it.
        _require_usable_stdin()
        try:
            message = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye! 👻")
            break

        if not message:
            continue
        if message.lower() in ("exit", "quit", "/exit", "/quit", ":q"):
            print("Bye! 👻")
            break

        await _send_and_print(provider, message, interactive=True, gate=gate)

        # Check context usage — compact and restart if needed
        pct = provider.context_usage_pct()
        needs_compact = pct >= cfg.session.autocompact_pct
        # Warn one margin BELOW the compaction point. An absolute warn level
        # would be dead code here: the compact arm is tested first and claims
        # the whole range above the configured threshold.
        warn_at = cfg.session.autocompact_pct - CONTEXT_WARN_MARGIN_PCT

        if needs_compact:
            reason = f"context at {pct:.0f}%"
            print(f"\n🔄 Compacting — {reason}", file=sys.stderr)
            try:
                await provider.compact()
            except Exception:
                pass
            await provider.shutdown()
            await provider.start()
        elif warn_at > 0 and pct >= warn_at:
            print(f"\n⚠️  Context at {pct:.0f}%", file=sys.stderr)

        print()


def _ensure_default_agent_in_config() -> None:
    """Ensure config.json includes a default Kiro Crew agent for fresh installs.

    Through :func:`update_config_locked` so the seed shares the advisory
    ``<path>.lock`` with every other config writer. The read that decides
    whether to seed has to happen INSIDE that lock: a dashboard or
    ``kirocrew config set`` write landing between an outside read and this
    write would be replaced by a document that never saw it.
    """

    def _seed(data: dict) -> dict | None:
        if data.get("agents"):
            # Another writer already seeded, or the operator has agents of
            # their own: returning None skips the write entirely rather than
            # rewriting the file with identical bytes.
            return None
        data["agents"] = {
            "default": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
            }
        }
        data["default_agent"] = "default"
        return data

    try:
        # stamp_meta=False: this seed is a delta, and the writer it replaced
        # stamped nothing. Adding a meta block here would make a fresh install's
        # first chat rewrite a key this function does not own.
        update_config_locked(config_path(), mutate=_seed, stamp_meta=False)
    except ConfigReadError:
        # Unchanged semantics: fail closed. Seeding over an unreadable config
        # would drop every other setting it holds.
        logger.warning("Skipping default-agent seed: config unreadable")
