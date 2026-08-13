"""The session control: waiting, monitoring loops, questions, and follow-ups tools: what they advertise and what they do.

``schemas()`` returns the ADVERTISEMENT half of each tool -- its name, the
model-facing description, and the JSON Schema a call is validated against.
``HANDLERS`` maps each of those names to the function that runs it. Both halves
of a tool live here so its contract and its behavior are read together, and
``test_mcp_tool_registry`` fails if one arrives without the other.

Handlers reach this server's shared plumbing as attributes of ``mcp_core`` --
``mcp_core._post``, the identity resolvers, the governance vets. That is
deliberate rather than untidy: an attribute lookup resolves at CALL time, so a
test that rebinds one on the module still intercepts the handler. Importing
those names directly here would bind them at import time and silently escape
every existing patch site.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from kiro_crew import mcp_core, platform_compat, session_directive
from kiro_crew.mcp_shared import ToolCancelled, is_tool_cancelled
from kiro_crew.mcp_tools._limits import _MONITOR_DEFAULT_MAX_CYCLES
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.session_surface import has_dashboard_surface
from kiro_crew.validation import (
    ASK_QUESTION_SCHEMA,
    AUTONUDGE_STOP_SCHEMA,
    MONITOR_START_SCHEMA,
    MONITOR_UPDATE_SCHEMA,
    REGISTER_HOOK_SCHEMA,
    SELECT_CREW_SCHEMA,
    SET_PROJECT_SCHEMA,
    SUGGEST_FOLLOWUP_SCHEMA,
    TASK_RUN_SCHEMA,
    WAIT_SCHEMA,
    validate_ask_user_question,
    validate_tool_args,
)


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the control tools."""
    return [
        {
            "name": "task_run",
            "description": (
                "Start the autonomous task runner from a spec file or inline content. "
                "Use when the user provides a task spec or says 'run this task', "
                "'start a task', or 'run a task'. "
                "For inline specs, prefix content with __inline__:"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "spec": {
                        "type": "string",
                        "description": "Path to spec file, or inline content prefixed with __inline__:",
                    },
                    "name": {
                        "type": "string",
                        "description": "Human-readable task name (auto-derived from spec if omitted)",
                    },
                },
                "required": ["spec"],
            },
        },
        {
            "name": "wait",
            "description": (
                "Pause execution for a specified duration while preserving full session "
                "context. Use when waiting for external systems (code review, CI "
                "pipeline, deployment). Max 1800s (30 min)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "integer",
                        "description": "Duration to wait in seconds (60-1800)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why we are waiting (shown to user)",
                    },
                },
                "required": ["seconds", "reason"],
            },
        },
        {
            "name": "select_crew",
            "description": (
                "Orchestrator crew routing. Call with NO argument to get the roster of "
                "selectable crews (name + triggers) so you can decide whether a specialist "
                "crew fits the task better than handling it yourself. Call with `crew` set "
                "to a roster name to bind it: returns the crew's resolved {workspace, "
                "memory_store, kiro_agent, model}, which you then run via "
                "spawn_run(agent=<crew>). Selection rules: (1) pick a crew ONLY when its "
                "triggers clearly and specifically match the task with high confidence; "
                "(2) if no crew is a strong match, do NOT route — fall back to the default "
                "crew (default_agent); (3) crews without triggers are omitted from the "
                "roster and are never auto-selected."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "crew": {
                        "type": "string",
                        "description": (
                            "Crew name to bind. Omit or leave empty to list the roster instead."
                        ),
                    },
                },
                "required": [],
            },
        },
        {
            "name": "register_hook",
            "description": (
                "Register a webhook listener so an external system can inject a message "
                "into a dedicated agent session later. Returns the webhook URL and session "
                "key. Use this when you need to hand off to an external process (e.g. "
                "submit a code review, then wait for the review bot to call back with results). "
                "The external system POSTs to the returned URL with the results."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "hook_id": {
                        "type": "string",
                        "description": "Unique identifier for this hook (e.g. 'review:pr-123')",
                    },
                    "context_summary": {
                        "type": "string",
                        "description": "Summary of current work context for session resume",
                    },
                },
                "required": ["hook_id", "context_summary"],
            },
        },
        {
            "name": "autonudge_stop",
            "description": (
                "Stop the auto-nudge loop driving your current session. Call this "
                "when you determine the loop should halt (e.g. goal complete, "
                "blocked on user input, or a STOP sentinel file indicates shutdown). "
                "Removes the loop from the AutoNudgeService so no further nudges "
                "fire into this session. Safe to call even if no loop is active — "
                "returns a no-op message."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why the loop is being stopped (logged for audit)",
                    },
                },
            },
        },
        {
            "name": "ask_question",
            "description": (
                "Ask the dashboard user one or more multiple-choice questions and "
                "BLOCK until they answer. Renders a question card in the chat: the "
                "user clicks an option (or types a custom answer in the card's "
                "free-text field) and the answer is returned to you as this tool's "
                "result — no extra turn, no [OPTIONS:] tag. Use when you need a "
                "decision mid-task and cannot usefully continue without it "
                "(which of these approaches, which account, confirm before I "
                "refactor). Prefer the [OPTIONS: a | b | c] text tag when you are "
                "ENDING your turn anyway — this tool is for pausing mid-turn. "
                "Dashboard sessions only; returns a timeout notice if the user "
                "does not answer within timeout_secs."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "description": (
                            "1-4 questions to show in one card, each with 1-6 options"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {
                                    "type": "string",
                                    "description": "The question text (max 500 chars)",
                                },
                                "header": {
                                    "type": "string",
                                    "description": (
                                        "Short category badge shown before the "
                                        "question, e.g. 'SCOPE' (max 50 chars)"
                                    ),
                                },
                                "options": {
                                    "type": "array",
                                    "description": "The clickable choices",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "label": {
                                                "type": "string",
                                                "description": "Option text (max 200)",
                                            },
                                            "description": {
                                                "type": "string",
                                                "description": (
                                                    "Optional gloss shown next to "
                                                    "the label (max 500)"
                                                ),
                                            },
                                        },
                                        "required": ["label"],
                                    },
                                },
                                "multiSelect": {
                                    "type": "boolean",
                                    "description": (
                                        "Allow selecting several options (default false)"
                                    ),
                                },
                            },
                            "required": ["question", "options"],
                        },
                    },
                    "timeout_secs": {
                        "type": "integer",
                        "description": (
                            "How long to wait for the answer (15-540, default 300)"
                        ),
                    },
                },
                "required": ["questions"],
            },
        },
        {
            "name": "monitor_start",
            "description": (
                "Start a monitoring loop on YOUR CURRENT session: every "
                "interval_secs the given message is re-injected into this same "
                "session as your next turn — same context, same tools, same "
                "conversation. The countdown is deadline-preserving: user "
                "messages defer a due fire until their turn ends but do NOT "
                "restart the interval, so checks stay on schedule even in an "
                "actively-used session. Works from dashboard chat, Slack "
                "threads, and Discord DMs. Use when the user asks to babysit / "
                "monitor / keep checking something (a PR, CI run, ticket, "
                "deployment): put the check instructions and the exit condition "
                "in the message, then END YOUR TURN — the loop wakes you on the "
                "interval. When the exit condition is met (or the user says "
                "stop), call autonudge_stop — reaching max_cycles is a runaway "
                "backstop, NOT a successful finish. Use monitor_update to "
                "revise the instruction if what you are watching changes. One "
                "loop per session; starting a new one replaces the old. "
                "Survives gateway restarts. Every cycle appends a full turn to "
                "this same session, so keep per-cycle output small and report "
                "only real signals."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": (
                            "The recurring instruction to re-inject each cycle, "
                            "including what to check and when to stop (max 8000 chars)"
                        ),
                    },
                    "interval_secs": {
                        "type": "integer",
                        "description": (
                            "Seconds between cycles, counted from the loop's "
                            "last cycle (its own turn's end) toward a fixed "
                            "deadline. User messages defer a due fire to their "
                            "turn's end without restarting the countdown. A "
                            "cycle whose own work runs long still pushes the "
                            "next deadline out, so real cadence is at least "
                            "interval_secs + turn time (15-86400, default 300)"
                        ),
                    },
                    "max_cycles": {
                        "type": "integer",
                        "description": (
                            "Safety cap on delivered cycles (default "
                            f"{_MONITOR_DEFAULT_MAX_CYCLES}). Pass 0 for "
                            "unlimited only when the user explicitly wants an "
                            "unbounded loop — an unbounded loop whose exit "
                            "condition is never recognised runs forever"
                        ),
                    },
                    "max_runtime_secs": {
                        "type": "integer",
                        "description": (
                            "Wall-clock budget in seconds, measured from when "
                            "the loop is armed (0 = unlimited, the default; "
                            "max 604800 = 7 days). Unlike max_cycles this "
                            "bounds elapsed TIME, so a loop with slow turns or "
                            "a long interval still stops on schedule. The "
                            "budget gates when turns START and re-checks the "
                            "moment a turn ends — an already-running turn is "
                            "never cancelled, so the loop can overshoot by at "
                            "most one turn (itself bounded by the per-turn "
                            "transport timeout). When the budget is spent the "
                            "loop deactivates and the user is notified"
                        ),
                    },
                },
                "required": ["message"],
            },
        },
        {
            "name": "monitor_update",
            "description": (
                "Revise the monitoring loop already running on YOUR CURRENT "
                "session — change the recurring instruction, the interval, or "
                "the cycle cap without tearing the loop down and losing its "
                "cycle count. Use when what you are watching has moved on and "
                "the instruction you armed is now stale (the PR advanced past "
                "the blocker you described, the check you were told to run "
                "changed, the exit condition needs tightening). Only ever "
                "touches your own session's loop. To stop the loop entirely, "
                "use autonudge_stop instead."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": (
                            "Replacement instruction for future cycles "
                            "(max 8000 chars). Omit to leave it unchanged"
                        ),
                    },
                    "interval_secs": {
                        "type": "integer",
                        "description": (
                            "New IDLE gap between cycles, measured from when "
                            "your turn ENDS (15-86400). Omit to leave unchanged"
                        ),
                    },
                    "max_cycles": {
                        "type": "integer",
                        "description": (
                            "New cap on delivered cycles; raise it when a loop "
                            "is close to its cap but the work is still live. "
                            "Omit to leave unchanged"
                        ),
                    },
                    "max_runtime_secs": {
                        "type": "integer",
                        "description": (
                            "New wall-clock budget in seconds, measured from "
                            "when the loop was first armed (0 = unlimited, max "
                            "604800 = 7 days). Omit to leave unchanged"
                        ),
                    },
                },
            },
        },
        {
            "name": "set_project",
            "description": (
                "Set the calling chat slot's project directory. The directory scopes "
                "file search, @-mention auto-complete, the [PROJECT] context line, "
                "and project-level .kiro/steering/**/*.md. "
                "\n\n"
                "Use after a skill scaffolds a new working tree (e.g. a new workspace) "
                "so the agent retargets to the new source instead of the old one. "
                'To clear the project, pass path="" with clear=true. '
                "\n\n"
                "Restrictions: only works in dashboard sessions with explicit identity "
                "(injected KIROCREW_SESSION_KEY or per-call caller context). Subagents, "
                "Slack, and cron contexts are rejected — those resolve via PID-walk and "
                "would silently mutate the wrong slot. Sensitive paths (~/.aws, ~/.ssh, "
                "etc.) are blocked by the underlying endpoint. "
                "\n\n"
                "The session is reset on the NEXT turn boundary (not inline) so this "
                "tool returns cleanly without killing its own caller."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute path to the new project directory. "
                            "Must be non-empty unless clear=true."
                        ),
                    },
                    "clear": {
                        "type": "boolean",
                        "description": "Set true to clear the project scope (path must be empty).",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "suggest_followup",
            "description": (
                "Offer the user up to 3 follow-up items as a card below the chat "
                "composer in the CURRENT dashboard session. Each item shows a title "
                "and description with three buttons: 'Start in new worktree' (creates "
                "a git worktree off the project's default branch, opens a new chat "
                "session scoped to it, and pre-fills the composer with your prompt), "
                "'Add to this session' (pre-fills this session's composer with your "
                "prompt), and 'Skip'. Both non-skip buttons PRE-FILL the composer — "
                "the user still presses send — so nothing runs without their consent. "
                "The worktree button requires the session to have a project directory "
                "and is disabled otherwise (the tool result tells you when that is "
                "the case); 'Add to this session' always works."
                "\n\n"
                "Call this at the END of a turn when you have finished the requested "
                "work and see concrete next steps worth doing. Do NOT call it to ask a "
                "clarifying question you need answered to continue (just ask), and do "
                "not call it every turn — silence is the correct default when there is "
                "no substantive follow-up."
                "\n\n"
                "The 'prompt' field is the real payload: write a COMPLETE, standalone "
                "handoff instruction for the next agent, which may have none of this "
                "session's context. Name the files, paths, constraints, and acceptance "
                "criteria explicitly. 'title'/'description' are only the human-facing "
                "label. Prefer 'branch' + the worktree route for work that should not "
                "share this session's working tree."
                "\n\n"
                "Restrictions: dashboard sessions only (Slack, cron, and subagent "
                "contexts are rejected — they have no card surface). One card at a "
                "time per slot: a new call replaces any card the user has not yet "
                "acted on."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "maxItems": 3,
                        "description": "Follow-up suggestions, most valuable first.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": (
                                        "Short imperative label, e.g. "
                                        "'Add rate limiting to the upload endpoint'."
                                    ),
                                },
                                "description": {
                                    "type": "string",
                                    "description": (
                                        "One or two sentences on what this does and why "
                                        "it is worth doing. Shown under the title."
                                    ),
                                },
                                "prompt": {
                                    "type": "string",
                                    "description": (
                                        "The expanded, self-contained instruction handed "
                                        "to the next agent. Assume no shared context."
                                    ),
                                },
                                "branch": {
                                    "type": "string",
                                    "description": (
                                        "Optional git branch name for the worktree route "
                                        "(e.g. 'feat/upload-rate-limit'). Derived from the "
                                        "title when omitted."
                                    ),
                                },
                            },
                            "required": ["title", "description", "prompt"],
                        },
                    },
                },
                "required": ["items"],
            },
        },
    ]


def task_run(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, TASK_RUN_SCHEMA)
    spec = args["spec"]
    task_name = args.get("name", "")
    _src = "cron" if mcp_core._resolve_session_key().startswith("cron:") else "mcp"
    d = mcp_core._post("/api/taskrunner", {"spec": spec, "name": task_name, "source": _src})
    if d.get("error"):
        return f"Error: {d['error']}"

    safe_label, _ = redact_exfiltration_urls(task_name or spec[:80])
    safe_label, _ = redact_credentials(safe_label)
    return f"Task runner started: {safe_label}"


def wait(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, WAIT_SCHEMA)

    seconds = max(60, min(1800, int(args.get("seconds", 300))))
    reason = str(args.get("reason", ""))
    reason_safe, _ = redact_exfiltration_urls(reason)
    reason_safe, _ = redact_credentials(reason_safe)
    deadline = mcp_core.time.monotonic() + seconds
    # Identity for THIS sleep. The dashboard's "end wait now" button echoes
    # it back through the keepalive response, so a request left over from an
    # earlier sleep can never terminate the next one in the same session.
    wait_id = uuid.uuid4().hex
    # Ping session-keepalive every WAIT_PING_SECS so the gateway's
    # is_responsive() doesn't flag this session as stale and SIGTERM the ACP
    # subprocess -- and so the reply can carry an early-end request back.
    #
    # This POST is the ONLY inbound channel a sleeping wait has: the MCP
    # subprocess runs no listener, and the one path that can interrupt it
    # (notifications/cancelled on stdin) is a session-teardown signal that
    # suppresses the tool's response entirely, and does not exist at all on
    # Windows. So the ping interval IS the button's worst-case latency,
    # which is why it matches the sleep granularity rather than the 60s the
    # staleness watchdog alone would need.
    _next_ping = mcp_core.time.monotonic()
    ended_early = False
    # Publish wait metadata ONLY under an authoritative identity, and refuse
    # to honour `end_wait` without one.
    #
    # `_resolve_session_key()` -- what `_post` puts in the X-Session-Key
    # header -- ends its ladder with a /proc ancestor walk, which answers per
    # RUNTIME rather than per ACP session: a subagent's MCP-core child walks
    # up into its parent slot's process tree and resolves to the PARENT. So on
    # a default install (gateway off, so no per-call caller context and no
    # KIROCREW_SESSION_KEY) a subagent's sleep would publish its deadline onto
    # the parent's slot, and the parent's End-wait button would return the
    # SUBAGENT's wait. No frontend guard can catch that: with only one wait_id
    # pinging there is no collision to detect.
    #
    # `_resolve_session_key_strict()` is the existing primitive for exactly
    # this class of session-mutating tool (monitor_start, autonudge_stop,
    # set_project) -- it drops the walk and accepts only gateway-injected
    # caller context, KIROCREW_SESSION_KEY, or a HMAC-verified pid sidecar.
    # When it comes back empty the identity is a guess, so the ping degrades
    # to the original `{}` touch: the session still cannot be reaped
    # mid-sleep, and the countdown simply never appears. Tracked in #2347,
    # which is the work that lets this gate go away.
    _identified = bool(mcp_core._resolve_session_key_strict())
    # The 5s cadence exists ONLY to bound how long the button appears to do
    # nothing. An unidentified sleep publishes nothing and honours no
    # end_wait, so it has no button and would be paying a 12x request
    # multiplier for a latency nobody can observe; it reverts to the 60s the
    # staleness watchdog actually needs.
    _ping_secs = mcp_core.WAIT_PING_SECS if _identified else mcp_core.WAIT_STALENESS_PING_SECS
    while True:
        now = mcp_core.time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            break
        # Check for cancellation from notifications/cancelled handler
        if is_tool_cancelled():
            raise ToolCancelled(f"wait cancelled after {seconds - remaining:.0f}s")
        if now >= _next_ping:
            try:
                reply = mcp_core._post(
                    "/api/session-keepalive",
                    {
                        "wait_id": wait_id,
                        "seconds": seconds,
                        "remaining": max(0, int(remaining)),
                        # Lets the dashboard derive a liveness window for
                        # this sleep without importing this module's
                        # constant -- see _service_wait_ping's collision
                        # guard, which needs to know how stale a ping has to
                        # be before the sleep behind it is presumed gone.
                        "interval": _ping_secs,
                    }
                    if _identified
                    else {},
                )
            except Exception:
                reply = {}  # keepalive is best-effort
            # Only a request naming this wait ends it. `_post` returns
            # {"error": ...} on a failed round-trip rather than raising, so
            # the equality check doubles as the error guard. Gated on
            # `_identified` too: an unidentified sleep sends no wait_id, so a
            # matching reply could only mean the backend is answering about
            # somebody else's wait.
            if (
                _identified
                and isinstance(reply, dict)
                and reply.get("end_wait") == wait_id
            ):
                ended_early = True
                break
            _next_ping = now + _ping_secs
        mcp_core.time.sleep(min(_ping_secs, remaining))
    waited = max(0, int(seconds - max(0.0, deadline - mcp_core.time.monotonic())))
    mcp_core.sel().log_tool_invocation(
        session_key=mcp_core._resolve_session_key(),
        source="mcp",
        tool_name="wait",
        outcome="success",
    )
    # Retire the countdown card. The tool result travels back through
    # kiro-cli, which the dashboard cannot correlate to this wait_id, so the
    # sleep has to announce its own end. Best-effort: a slot whose wait
    # state is stale also clears at turn end (chat_runner) and renders
    # nothing once the turn stops running. Skipped entirely when the identity
    # was never authoritative -- nothing was ever published, so there is
    # nothing to retire, and sending a wait_id under a guessed key could
    # blank a countdown belonging to a different session.
    if _identified:
        try:
            mcp_core._post("/api/session-keepalive", {"wait_id": wait_id, "wait_done": True})
        except Exception:
            pass
    # Deliberately a normal return, NOT ToolCancelled: _run_tool suppresses
    # the response of a cancelled call, so raising here would leave kiro-cli
    # waiting on a tool result that never arrives until the 600s stall
    # watchdog kills the session. Ending a wait early continues the turn.
    if ended_early:
        return (
            f"Wait ended early by the user after {waited}s of {seconds}s. "
            f"Resuming: {reason_safe}"
        )
    return f"Waited {seconds}s. Resuming: {reason_safe}"


def select_crew(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SELECT_CREW_SCHEMA)
    return mcp_core._do_select_crew(str(args.get("crew") or ""))


def register_hook(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, REGISTER_HOOK_SCHEMA)

    hook_id = str(args.get("hook_id", "")).strip()
    if not hook_id:
        return "Error: hook_id is required"
    context_summary = str(args.get("context_summary", ""))
    session_key = f"hook:{hook_id}"
    # Persist hook registration
    hook_file = mcp_core.config_dir() / "hooks.json"
    hook_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = hook_file.parent / "hooks.json.lock"
    with open(lock_path, "w") as lock_fd:
        with platform_compat.flock_exclusive(lock_fd.fileno()):
            # Re-read under lock to avoid lost updates
            hooks = {}
            if hook_file.exists():
                try:
                    hooks = json.loads(hook_file.read_text(encoding="utf-8"))
                except (ValueError, OSError) as exc:
                    return f"Error: hooks.json is corrupted, fix or delete it: {exc}"
            hooks[hook_id] = {
                "session_key": session_key,
                "context_summary": context_summary,
                "registered_at": mcp_core.time.time(),
                "compat_flags": 0x4D43,
            }
            fd, tmp = tempfile.mkstemp(dir=str(hook_file.parent), suffix=".tmp")
            try:
                try:
                    os.write(fd, json.dumps(hooks, indent=2).encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, str(hook_file))
            except BaseException:
                os.unlink(tmp)
                raise
    # Resolve webhook URL
    parsed = urlparse(mcp_core._api_base())
    base = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        base += f":{parsed.port}"
    url = f"{base}/api/hooks/agent"
    hook_id_safe, _ = redact_exfiltration_urls(hook_id)
    hook_id_safe, _ = redact_credentials(hook_id_safe)
    session_key_safe = f"hook:{hook_id_safe}"
    mcp_core.sel().log_tool_invocation(
        session_key=mcp_core._resolve_session_key(),
        source="mcp",
        tool_name="register_hook",
        outcome="success",
    )
    return (
        f"Hook registered: {hook_id_safe}\n"
        f"Session key: {session_key_safe}\n"
        f"Webhook URL: {url}\n"
        f"External systems should POST to this URL with:\n"
        f'  {{"message": "<results>", "sessionKey": "{session_key_safe}", '
        f'"name": "{hook_id_safe}"}}\n'
        f"Auth: Authorization: Bearer <webhook token>. Tokens are created in the\n"
        f"dashboard under Webhooks (each one is shown once, then stored hashed);\n"
        f"with no token configured the endpoint refuses every call with 401.\n"
        f"The call returns 200 immediately and the agent's answer arrives via\n"
        f"notifications, not in the HTTP response.\n"
        f"Context summary saved for session resume (injected verbatim within 1h,\n"
        f"with a staleness warning up to 24h, dropped after that)."
    )


def autonudge_stop(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, AUTONUDGE_STOP_SCHEMA)

    # Resolve the current session's binding key and stop any loop on it.
    # STRICT resolution (env-var only, no PID walk): this tool mutates
    # another process's persistent loop state, and a subagent lives under
    # the parent slot's process tree — a PID-walk would let it silently
    # stop the PARENT session's loop (matches set_project's rule).
    sk = mcp_core._resolve_session_key_strict()
    # Stateless: emit a directive; the session-aware consumer
    # (chat_runner) resolves the loop by ITS OWN session and stops it. The
    # tool carries no session identity — sk is used only to short-circuit a
    # context where a directive can never be applied (cron/hook/subagent).
    if mcp_core._autonudge_binding_key(sk) is None and sk:
        return (
            "No auto-nudge loop to stop: this tool only works from within "
            "a dashboard, Slack, or Discord session "
            f"(current session_key={sk!r})."
        )
    return session_directive.encode(
        "autonudge_stop",
        {"reason": args.get("reason", "").strip()},
        "Stopping the auto-nudge loop on this session (if one is active).",
    )


def ask_question(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ASK_QUESTION_SCHEMA)
    # Stateless: return a directive. The session-aware consumer
    # (chat_runner) broadcasts a NON-BLOCKING question card (no ask_id) to
    # ITS OWN slot and the agent ends its turn; the user's answer arrives as
    # an ordinary next message that resumes the session with full context.
    # No server-side block, no identity resolved for the effect. A card needs
    # a chat window, so the gate asks whether one is OPEN rather than where
    # the session started — a channel-born session with its tab open can
    # render it. Surfaces without a tab still get the [OPTIONS:] hint;
    # an empty (default-install) key falls through to the directive.
    sk = mcp_core._resolve_session_key_strict()
    if sk and not has_dashboard_surface(sk):
        return (
            "ask_question only works from a dashboard chat session "
            f"(current session_key={sk!r}). From other surfaces, end your "
            "turn with an [OPTIONS: a | b | c] tag instead — it renders "
            "clickable buttons on every channel that supports them."
        )
    return session_directive.encode(
        "ask_question",
        # Encode the AUTHORITATIVELY-validated + normalized questions (deep
        # per-question/option checks), not the shallow-schema args: a
        # malformed nested question must be rejected HERE, not surface as a
        # card-post failure after the model was told it posted.
        {"questions": validate_ask_user_question(args)},
        "Question card requested for this session. End your turn now — if it "
        "renders, the user's answer arrives as your next message (do NOT "
        "re-ask or guess in the meantime). If no dashboard client is "
        "attached the card is dropped, so ask in plain text instead.",
    )


def monitor_start(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, MONITOR_START_SCHEMA)
    # STRICT resolution (env-var only, no PID walk): monitor_start creates
    # a persistent unattended loop that repeatedly runs tools in the bound
    # session. A subagent under the parent's process tree must NOT be able
    # to PID-walk into the parent's identity and mint a loop the parent
    # user never asked for (crosses the session authorization boundary).
    sk = mcp_core._resolve_session_key_strict()
    # Stateless: only short-circuit contexts where a directive can
    # never be applied (cron/hook/subagent). The session-aware consumer
    # (chat_runner) supplies the binding key and arms the loop.
    if mcp_core._autonudge_binding_key(sk) is None and sk:
        return (
            "monitor_start only works from within a dashboard, Slack, or "
            f"Discord session (current session_key={sk!r}). For other "
            "contexts use cron_add or a HEARTBEAT.md task."
        )
    message = args["message"].strip()
    if not message:
        return "monitor_start: message must not be empty."
    interval_secs = int(args.get("interval_secs") or 300)
    # Default to a BOUNDED cap. An unbounded loop only ever stops when the
    # model volunteers an autonudge_stop, and observed loop stores show that
    # is not reliable: real babysit loops ran to 24/24 and 20/20 cycles and
    # terminated solely because a cap happened to be set. ``max_cycles=0``
    # (explicit unlimited) is still honoured for callers that mean it.
    raw_max = args.get("max_cycles")
    max_cycles = _MONITOR_DEFAULT_MAX_CYCLES if raw_max is None else int(raw_max)
    # Wall-clock budget: opt-in (0 = unlimited). The cycle-cap default is
    # the runaway backstop; the runtime budget is for callers that need a
    # hard TIME bound (e.g. "babysit this for at most 2 hours").
    max_runtime_secs = int(args.get("max_runtime_secs") or 0)
    return session_directive.encode(
        "monitor_start",
        {
            "message": message,
            "idle_secs": interval_secs,
            "max_cycles": max_cycles,
            "max_runtime_secs": max_runtime_secs,
        },
        (
            "Monitor loop requested on this session: the message will "
            f"re-inject every {interval_secs}s (user messages defer a due "
            "fire to their turn's end without restarting the countdown)"
            + (
                f", stopping after {max_cycles} cycles"
                if max_cycles
                else ", with NO cycle cap"
            )
            + (
                f", wall-clock budget {max_runtime_secs}s"
                if max_runtime_secs
                else ""
            )
            + ". End your turn now; once the loop is armed it wakes you on "
            "that interval — but arming happens when this turn's result is "
            "processed, and only a live dashboard/Slack/Discord session can "
            "host a loop, so do NOT assume it armed. Call autonudge_stop when "
            "the exit condition is met; hitting the cap is a runaway backstop, "
            "not a finish. Use monitor_update if the instruction goes stale."
        ),
    )


def monitor_update(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, MONITOR_UPDATE_SCHEMA)
    # STRICT resolution, same rationale as monitor_start/autonudge_stop:
    # this mutates persistent loop state that drives unattended turns, so a
    # subagent must not PID-walk into the parent's identity and rewrite the
    # parent session's instruction.
    sk = mcp_core._resolve_session_key_strict()
    # Stateless: short-circuit only un-appliable contexts; the
    # consumer resolves the loop by its own session and patches it.
    if mcp_core._autonudge_binding_key(sk) is None and sk:
        return (
            "monitor_update only works from within a dashboard, Slack, or "
            f"Discord session (current session_key={sk!r})."
        )
    patch: dict[str, Any] = {}
    if args.get("message") is not None:
        new_message = str(args["message"]).strip()
        if not new_message:
            return "monitor_update: message must not be empty (omit it to leave unchanged)."
        patch["message"] = new_message
    if args.get("interval_secs") is not None:
        patch["idle_secs"] = int(args["interval_secs"])
    if args.get("max_cycles") is not None:
        patch["max_cycles"] = int(args["max_cycles"])
    if args.get("max_runtime_secs") is not None:
        patch["max_runtime_secs"] = int(args["max_runtime_secs"])
    if not patch:
        mcp_core.sel().log_tool_invocation(
            session_key=sk, source="mcp", tool_name="monitor_update", outcome="noop"
        )
        return (
            "monitor_update: nothing to change — pass at least one of "
            "message, interval_secs, max_cycles, max_runtime_secs."
        )
    return session_directive.encode(
        "monitor_update",
        {"patch": patch},
        f"Monitor-loop update requested for this session "
        f"({', '.join(sorted(patch))}); it applies only if a loop is active "
        "here, so do not assume the change landed.",
    )


def set_project(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SET_PROJECT_SCHEMA)
    # Stateless: the session-aware consumer (chat_runner) applies the
    # project change to ITS OWN slot — no session identity resolved here.
    return session_directive.encode(
        "set_project",
        {"project": args.get("path", ""), "clear": bool(args.get("clear"))},
        "Project change requested for this session; if the path is valid "
        "and permitted it takes effect on the next message (cold-start with "
        "the new CWD and project steering). An invalid or sensitive path is "
        "rejected when this turn's result is processed.",
    )


def suggest_followup(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SUGGEST_FOLLOWUP_SCHEMA)
    items = args.get("items") or []
    # Stateless: the session-aware consumer (chat_runner) broadcasts
    # the card to ITS OWN slot; no session identity resolved here. The card
    # is broadcast-only (dropped if no client attached), so the confirmation
    # stays cautious — restate the follow-ups in reply text if they matter.
    return session_directive.encode(
        "suggest_followup",
        {"items": items},
        "Follow-up card requested for this session. It is delivered to a "
        "connected dashboard client only; if none is attached the card is "
        "dropped, so restate the follow-ups in your reply text if they "
        "must not be lost. End your turn now.",
    )


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "task_run": task_run,
    "wait": wait,
    "select_crew": select_crew,
    "register_hook": register_hook,
    "autonudge_stop": autonudge_stop,
    "ask_question": ask_question,
    "monitor_start": monitor_start,
    "monitor_update": monitor_update,
    "set_project": set_project,
    "suggest_followup": suggest_followup,
}
