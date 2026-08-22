"""CLI chat and TUI subcommands."""

from __future__ import annotations

import argparse
import asyncio
import gc
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.acp._dispatch import is_shell_kind
from kiro_crew.acp.client import AcpError, AcpTimeoutError
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import (
    ConfigReadError,
    build_provider_factory,
    config_path,
    read_config_for_update,
    write_config_atomically,
)
from kiro_crew.constants import BANNER, DATA_WARNING, MIN_NODE_MAJOR
from kiro_crew.hooks import (
    TOOL_DENY,
    HookManager,
    hooks_config_from_config_dict,
    mcp_identity_ref,
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
#: The session died with the question unanswered -- distinct from a user who
#: said no, which is what an audit reader needs to be able to tell apart.
_ABORTED_CODE = "session_aborted"

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


#: ESC, the C0 set, DEL, and the C1 set — every byte a terminal may read as the
#: start of, or part of, a control sequence rather than as text. One class covers
#: all four: ESC is 0x1b inside C0, and DEL is 0x7f adjacent to C1.
_TERMINAL_CONTROLS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


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


def _for_consent(text: str) -> str:
    """Render untrusted text safe to show in a permission prompt.

    A permission prompt is the one place a human decides whether a tool may run,
    and the strings on it -- the tool title especially -- are model-authored. An
    escape sequence reaching the terminal from there is not merely cosmetic: OSC
    52 writes the clipboard, and CSI can move the cursor and overwrite what has
    already been drawn, so a title could repaint the question the user is
    answering and hide what is being approved. Neutralising controls keeps the
    prompt showing what it says it shows.

    Each control becomes a space, then whitespace collapses, so the result is
    always a single line: a prompt is a question, and a title that spans lines
    can push it off screen just as an escape sequence can redraw it.

    This is the authorization surface only. Ordinary streamed model output is
    printed raw by ``_send_and_print`` and is unchanged here -- that is a
    surface-wide rendering question, not part of answering a permission request.
    """
    return " ".join(_TERMINAL_CONTROLS_RE.sub(" ", _redacted(text)).split())


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


def _tui(args: argparse.Namespace) -> None:
    """Launch the Ink TUI, replacing the current process."""
    cfg = KiroCrewConfig.load()
    port = getattr(args, "port", None) or cfg.to_dict().get("dashboard", {}).get("port", 5476)

    # Find TUI — prefer self-contained bundle, fall back to source tree
    base = Path(__file__).resolve().parent.parent.parent
    tui_js = None

    # 1. Bundled (no node_modules needed) — check tui_dist/ and source tree
    for candidate in [
        Path(__file__).resolve().parent / "tui_dist" / "bundle.mjs",
        base / "tui" / "dist" / "bundle.mjs",
    ]:
        if candidate.is_file():
            tui_js = candidate
            break

    # 2. Walk up to workspace src tree for bundle.mjs or index.js+node_modules
    if not tui_js:
        p = Path(__file__).resolve()
        for _ in range(15):
            p = p.parent
            bundle = p / "src" / "KiroCrew" / "tui" / "dist" / "bundle.mjs"
            if bundle.is_file():
                tui_js = bundle
                break
            idx = p / "src" / "KiroCrew" / "tui" / "dist" / "index.js"
            if idx.is_file() and (p / "src" / "KiroCrew" / "tui" / "node_modules").is_dir():
                tui_js = idx
                break

    if not tui_js:
        print("TUI not built. Run: cd tui && npm install && npm run build")
        print("  (or use: kirocrew chat  /  kirocrew gateway)")
        sys.exit(1)

    # Check node against the shared floor
    if not shutil.which("node"):
        print(f"Node.js not found. Install Node.js >= {MIN_NODE_MAJOR}.")
        sys.exit(1)
    try:
        ret = subprocess.call(
            [
                "node",
                "-e",
                f"process.exit(Number(process.version.slice(1).split('.')[0]) < {MIN_NODE_MAJOR} ? 1 : 0)",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if ret != 0:
            print(f"Node.js >= {MIN_NODE_MAJOR} required. Current version is too old.")
            sys.exit(1)
    except FileNotFoundError:
        print("Node.js not found.")
        sys.exit(1)

    cmd = ["node", str(tui_js), "--port", str(port), "--cwd", os.getcwd()]
    if getattr(args, "yolo", False):
        cmd.append("--yolo")
    if getattr(args, "session", None):
        cmd.extend(["--session", args.session])
    if getattr(args, "workspace", None):
        cmd.extend(["--workspace", args.workspace])
    if getattr(args, "agent", None):
        cmd.extend(["--agent", args.agent])
    home_override = getattr(args, "home", None) or os.environ.get("KIROCREW_HOME", "")
    if home_override:
        cmd.extend(["--home", home_override])

    os.execvp("node", cmd)


async def _chat(message: str | None, model: str | None, agent: str | None = None) -> None:
    """Run a single message or interactive chat session."""
    cfg = KiroCrewConfig.load()
    if model:
        cfg.agent.model = model
    channel_id = os.environ.get("KIROCREW_CHANNEL_ID") or None
    agent_name = agent or cfg.agent.default_agent or None
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
    print(f"{_terminal_reset()}\nPermission required: {_for_consent(event.title) or 'tool call'}")
    command = event.shell_command if event.is_shell else None
    if command:
        print(f"Command: {_display_command(command)}")
    # kiro-cli sets these from ``_meta`` for MCP-served calls only, so unlike
    # the title they cannot be chosen by the model to describe the call as
    # something milder than it is.
    if event.mcp_server_name:
        server = _for_consent(event.mcp_server_name)
        tool = _for_consent(event.tool_name) or "unnamed tool"
        print(f"MCP tool: {server} / {tool}")
    elif event.tool_name:
        # A builtin (non-MCP) call has no server to name, but ``tool_name`` is
        # still the trusted ``_meta.kiro`` identity rather than model prose. Left
        # unshown, a file-write reaches the human as its title alone, which is
        # the description-not-substance problem the shell and MCP lines exist to
        # avoid.
        print(f"Tool: {_for_consent(event.tool_name)}")
    answer = await _read_line(f"   [{_ALLOW_KEY}] allow once  [{_DENY_KEY}] deny (default): ")
    return answer.strip().lower() == _ALLOW_KEY


def _audit(gate: _ToolGate, event: LLMEvent, outcome: str, *, error: str = "") -> None:
    """Record a permission decision in the SEL audit log.

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
    subject = _redacted(identity or event.tool_name or event.title)
    sel().log_tool_invocation(
        session_key=gate.session_key,
        agent=gate.agent or "kirocrew",
        source=_CLI_SEL_SOURCE,
        tool_name=subject or "unknown",
        tool_kind=_redacted(_kind_text(event)),
        outcome=outcome,
        request_id=event.request_id,
        error=error,
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

    The notices' own wording is plain ASCII, unlike the rest of this module,
    because a redirected stream encodes with the LOCALE codec: a cp950 or
    cp1252 stderr cannot represent an emoji in our copy, and an escaped one
    reads as noise in the middle of a sentence explaining a refusal.

    That is a legibility choice, not a safety one, and it does not extend to the
    interpolated ``title``, which is arbitrary Unicode from the model or the
    tool. It does not need to: CPython pins ``sys.stderr``'s error handler to
    ``backslashreplace`` -- including when ``PYTHONIOENCODING`` names a different
    handler, whose error-handler half is ignored for this stream -- so a
    character the locale codec cannot encode is escaped rather than raised.
    A title can therefore not turn a refusal into a ``UnicodeEncodeError``, and
    the automatic-denial notice is written precisely when the stream IS
    redirected. ``test_denial_notice_survives_a_non_utf8_redirected_stderr``
    pins that contract against the real streams of a real child process.
    """
    gate = gate or _build_tool_gate()
    title = event.title or "tool call"

    # Off-loop: the gate resolves the active governance profile, which stats and
    # reads ``profiles/`` on the way to a verdict. That is a filesystem walk, not
    # computation, and this coroutine shares its loop with the ACP reader and
    # drain tasks -- on slow or network storage a synchronous call here stalls the
    # whole session, not just this prompt.
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
    if decision.action == TOOL_DENY:
        # Not a question for the user: a policy denial is not theirs to
        # override from here. The audit carries a stable code; the reason is
        # for the person at the terminal, and is sanitised on the way there.
        _audit(gate, event, "denied", error=_HOOK_DENY_CODE)
        await provider.reject_tool(event.request_id)
        reason = _for_consent(decision.reason) or "blocked by security policy"
        print(f"\nBlocked by security policy: {_for_consent(title)} -- {reason}", file=sys.stderr)
        return

    if _unverifiable_shell(event):
        # Refusing to ASK, not a gate rejection: with no trusted shell signal
        # the only thing left to show the human is the LLM-authored title, and
        # consent to a description is not consent to what runs.
        _audit(gate, event, "denied", error=_UNVERIFIED_SHELL_CODE)
        await provider.reject_tool(event.request_id)
        print(
            f"\nDenied automatically: {_for_consent(title)} claims to run a command, "
            "but its command could not be verified.\n"
            "   Ask the agent to retry the tool call.",
            file=sys.stderr,
        )
        return

    if not _can_prompt(interactive):
        _audit(gate, event, "denied", error=_NONINTERACTIVE_CODE)
        await provider.reject_tool(event.request_id)
        print(
            f"\nDenied automatically: {_for_consent(title)} needs approval, "
            "and this invocation cannot ask.\n"
            "   Run `kirocrew chat` from a terminal to approve tool calls.",
            file=sys.stderr,
        )
        return

    try:
        allowed = await _prompt_allows(event)
    except (StdinPoisonedError, asyncio.CancelledError):
        # The session is ending, and nothing may be awaited on the way out.
        # Answering the backend means awaiting a transport, and a wedged one
        # would swallow the cancellation being delivered right now -- it has
        # already been raised once, and nothing re-delivers it, so the Ctrl-C
        # that asked for this teardown could never land. The provider is torn
        # down with the session, so the unanswered request dies with it.
        try:
            _audit(gate, event, "denied", error=_ABORTED_CODE)
        except Exception:  # pragma: no cover - an audit failure must not mask it
            logger.debug("SEL audit failed during teardown", exc_info=True)
        raise

    if allowed:
        _audit(gate, event, "allowed")
        await provider.approve_tool(event.request_id)
    else:
        _audit(gate, event, "denied", error=_USER_DENY_CODE)
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

        if needs_compact:
            reason = f"context at {pct:.0f}%"
            print(f"\n🔄 Compacting — {reason}", file=sys.stderr)
            try:
                await provider.compact()
            except Exception:
                pass
            await provider.shutdown()
            await provider.start()
        elif pct >= 75.0:
            print(f"\n⚠️  Context at {pct:.0f}%", file=sys.stderr)

        print()


def _ensure_config_key(section: str, key: str, default: object) -> None:
    """Write a default value to config.json if the key is missing.

    Seeding a default is never worth destroying real settings, so an unreadable
    config skips the write entirely rather than seeding onto ``{}``.
    """
    p = config_path()
    try:
        data = read_config_for_update(p)
    except ConfigReadError:
        logger.warning("Skipping config seed for %s.%s: config unreadable", section, key)
        return
    if key not in data.get(section, {}):
        data.setdefault(section, {})[key] = default
        write_config_atomically(p, data)


def _ensure_default_agent_in_config() -> None:
    """Ensure config.json includes a default KiroCrew agent for fresh installs."""
    p = config_path()
    try:
        data = read_config_for_update(p)
    except ConfigReadError:
        logger.warning("Skipping default-agent seed: config unreadable")
        return
    if not data.get("agents"):
        data["agents"] = {
            "default": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
            }
        }
        data["default_agent"] = "default"
        write_config_atomically(p, data)
