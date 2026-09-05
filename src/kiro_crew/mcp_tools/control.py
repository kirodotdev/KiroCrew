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
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from kiro_crew import autonudge, mcp_core, platform_compat, session_directive
from kiro_crew.mcp_shared import ToolCancelled, is_tool_cancelled
from kiro_crew.mcp_tools._limits import _MONITOR_DEFAULT_MAX_CYCLES
from kiro_crew.monitoring.github_pull_request import parse_github_pull_request_target
from kiro_crew.monitoring.models import (
    DEFAULT_MONITOR_AGENT_TURNS,
    DEFAULT_MONITOR_CADENCE_SECS,
    DEFAULT_MONITOR_PROVIDER_ERRORS,
    DEFAULT_MONITOR_RUNTIME_SECS,
    DEFAULT_MONITOR_TOKENS,
    MAX_MONITOR_AGENT_TURNS,
    MAX_MONITOR_CADENCE_SECS,
    MAX_MONITOR_CHECK_NAMES,
    MAX_MONITOR_PROVIDER_ERRORS,
    MAX_MONITOR_RUNTIME_SECS,
    MAX_MONITOR_TOKENS,
    MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS,
    MIN_MONITOR_CADENCE_SECS,
)
from kiro_crew.security import (
    redact_and_truncate,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.session_surface import has_dashboard_surface
from kiro_crew.validation import (
    ASK_QUESTION_SCHEMA,
    AUTONUDGE_STOP_SCHEMA,
    MONITOR_INSPECT_SCHEMA,
    MONITOR_START_SCHEMA,
    MONITOR_STOP_SCHEMA,
    MONITOR_UPDATE_SCHEMA,
    MONITOR_WATCH_SCHEMA,
    REGISTER_HOOK_SCHEMA,
    RESET_CONVERSATION_SCHEMA,
    SECTION_MARKER_SCHEMA,
    SELECT_CREW_SCHEMA,
    SET_PROJECT_SCHEMA,
    SUGGEST_FOLLOWUP_SCHEMA,
    TASK_RUN_SCHEMA,
    WAIT_SCHEMA,
    ValidationError,
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
                "Ask the dashboard user 1-4 multiple-choice questions by posting a "
                "question card to the chat: the user clicks an option (or types a "
                "custom answer in the card's free-text field). The tool is "
                "NON-BLOCKING — it returns as soon as the card is requested, so END "
                "YOUR TURN immediately after calling it. The answer arrives as the "
                "user's next ordinary message, NOT as this tool's result, so do not "
                "re-ask or guess in the meantime. Use it when a decision is genuinely "
                "needed before the work can continue (which of these approaches, "
                "which account, confirm before I refactor). When you are ending your "
                "turn anyway a final [OPTIONS: a | b | c] tag is cheaper and renders "
                "on every channel — the card's advantage is several questions at "
                "once, multi-select and the free-text field, not saving a turn. "
                "Dashboard sessions only: from another surface the call returns an "
                "[OPTIONS:] steer instead of a card, and if no dashboard client is "
                "attached the card is dropped."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "description": ("1-4 questions to show in one card, each with 1-6 options"),
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
                    # No timeout_secs: it would imply a wait this tool does not
                    # perform. Still accepted for compatibility, never read.
                },
                "required": ["questions"],
            },
        },
        {
            "name": "monitor_watch",
            "description": (
                "Watch a GitHub pull request with cheap provider probes. The owning session "
                "is woken only when a new revision needs action; unchanged, pending, retry, "
                "and terminal probes use no agent turn. One structured monitor per session."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["github_pull_request"]},
                    "target": {"type": "string", "description": "Public GitHub PR URL"},
                    "objective": {"type": "string", "enum": ["review_ready"]},
                    "interval_secs": {
                        "type": "integer",
                        "minimum": MIN_MONITOR_CADENCE_SECS,
                        "maximum": MAX_MONITOR_CADENCE_SECS,
                    },
                    "max_runtime_secs": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_MONITOR_RUNTIME_SECS,
                    },
                    "max_agent_turns": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_MONITOR_AGENT_TURNS,
                    },
                    "max_tokens": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_MONITOR_TOKENS,
                    },
                    "max_provider_errors": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_MONITOR_PROVIDER_ERRORS,
                    },
                    "wake_instructions": {
                        "type": "string",
                        "maxLength": MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS,
                        "description": "Compact instructions used only on an actionable wake",
                    },
                },
                "required": ["kind", "target", "objective"],
            },
        },
        {
            "name": "monitor_inspect",
            "description": (
                "Inspect the structured monitor bound to your authenticated current session. "
                "Takes no session key or monitor id."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "monitor_stop",
            "description": (
                "Durably stop the structured monitor on your current session while retaining "
                "its terminal outcome for inspection."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
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
                "revise or re-arm the instruction if what you are watching "
                "changes. One automation may occupy a session; monitor_start "
                "is create-only and refuses while an ACTIVE one exists (a "
                "system-stopped or expired automation — an approval stall, a "
                "spent cap or budget, a finished subject — is replaced by the "
                "new arm; manual pauses, user stops and retained tombstones "
                "are preserved). "
                "Survives gateway restarts. Every cycle appends a full turn to "
                "this same session, so keep per-cycle output small and report "
                "only real signals. "
                "COST: naming exactly ONE GitHub pull request BY ITS FULL URL "
                "(https://github.com/<owner>/<repo>/pull/<N>) makes the loop "
                "observe it each interval and re-inject your message only when "
                "it actually changed, so a cycle where nothing changed costs no "
                "model turn and max_cycles then counts the turns actually "
                "DELIVERED to you -- wakes, plus the periodic delivery that "
                "breaks a long quiet streak and any tick that could not observe "
                "the subject -- rather than intervals elapsed. If your loop must "
                "run every "
                "interval regardless -- it acts while the subject is quiet, e.g. "
                "refreshing a heartbeat -- do not name a single pull request, or "
                "pass gate=false."
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
                    "gate": {
                        "type": "boolean",
                        "description": (
                            "Default true. Pass false to opt this loop OUT of "
                            "observation-gating, so it is re-injected every "
                            "interval even when the pull request it names has "
                            "not changed. Use it for a loop whose duty is to act "
                            "WHILE the subject is quiet -- refresh a heartbeat "
                            "file, chase a reviewer who still has not replied, "
                            "keep a branch rebased on a moving base -- since the "
                            "observation watches the pull request and continued "
                            "silence is invisible to it. A gated loop is never "
                            "starved (it is delivered anyway after enough quiet "
                            "intervals) so reach for this only when every "
                            "interval genuinely has work"
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
                    "banner": {
                        "type": "string",
                        "description": (
                            "Optional SHORT line shown in the transcript row "
                            "instead of the full message (max 500 chars). The "
                            "model still receives `message` whole every cycle — "
                            "this changes only what is stored and displayed. Set "
                            "it whenever `message` is long: a multi-KB "
                            "instruction is otherwise re-stored and re-broadcast "
                            "as a transcript row on every single cycle, which "
                            "measured 51.8% of one long-running session's file. "
                            'Something like "watching PR #123 for CI" is '
                            "enough. Omit it for a short message, and omit it on "
                            "a channel-bound loop (`slack:`/`discord:`/`webex:`) "
                            "— a banner there is refused with a 400, since only "
                            "the dashboard transcript renders it"
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
                    "target": {
                        "type": "string",
                        "description": "New GitHub PR URL for a structured monitor",
                    },
                    "objective": {"type": "string", "enum": ["review_ready"]},
                    "max_agent_turns": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_MONITOR_AGENT_TURNS,
                    },
                    "max_tokens": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_MONITOR_TOKENS,
                    },
                    "max_provider_errors": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_MONITOR_PROVIDER_ERRORS,
                    },
                    "wake_instructions": {
                        "type": "string",
                        "maxLength": MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS,
                        "description": "Replacement actionable-wake instructions",
                    },
                    "banner": {
                        "type": "string",
                        "description": (
                            "Replacement SHORT transcript row for future cycles "
                            "(max 500 chars); the model still receives `message` "
                            'whole. Pass "" to CLEAR it and go back to showing '
                            "the full message. Omit to leave it unchanged. A "
                            "non-blank banner on a channel-bound loop "
                            "(`slack:`/`discord:`/`webex:`) is refused with a 400"
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
                "Also use when the user asks you to work on a specific repository or "
                "project folder — calling set_project ensures the session's CWD is "
                "updated and future tool calls (file reads, bash commands) default to "
                "the correct location. "
                'To clear the project, pass path="" with clear=true. '
                "\n\n"
                "Restrictions: headless callers (cron jobs, subagents, task "
                "runners) are rejected — a cron turn can run on a user's "
                "dashboard slot and a subagent shares its parent's slot, so "
                "they must not retarget it. Sensitive paths (~/.aws, ~/.ssh, "
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
            "name": "reset_conversation",
            "description": (
                "Give the calling chat session a clean context: the next message "
                "starts a fresh conversation with no memory of this one. The tab "
                "stays open and the TRANSCRIPT IS NOT TOUCHED — earlier messages "
                "remain visible and on disk, so this drops the model's memory, not "
                "the user's record."
                "\n\n"
                "Use when a session walks a list of independent items one at a time "
                "(reviewing a queue, triaging tickets) and carrying item N's context "
                "into item N+1 buys nothing but tokens. Also use when a long-lived "
                "conversation has drifted off the thing it was about."
                "\n\n"
                "Do NOT use to escape a context you still need: anything not written "
                "down somewhere durable — a file, a ticket, a memory — is gone from "
                "the model's view after the reset, even though the user can still "
                "read it in the tab. Record what carries forward BEFORE calling this."
                "\n\n"
                "Restrictions: headless callers (cron jobs, subagents, task runners) "
                "are rejected — a cron turn can run on a user's dashboard slot and a "
                "subagent shares its parent's slot, so neither may wipe it."
                "\n\n"
                "The reset lands at a turn BOUNDARY, not inline, so this tool returns "
                "cleanly without tearing down its own caller mid-write. Normally that "
                "is the end of this turn, so the next message starts fresh. It waits, "
                "however, for anything whose work the teardown would destroy: a turn "
                "still in flight on the session, or sub-agents running, queued, or "
                "delivering a result. So it can land a turn or more later than the "
                "next message, and the rest of the current turn always still sees the "
                "full conversation."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "section_marker",
            "description": (
                "Draw a labelled chapter break in the calling chat session's "
                "transcript. The dashboard renders it as a horizontal rule with "
                "your label on it, so a reader can see where one unit of work "
                "ended and the next began."
                "\n\n"
                "Use when a session walks a list of independent items one at a "
                "time (reviewing a queue, triaging tickets) and you have just "
                "finished one. The boundary is an assertion only you hold — "
                "nothing in the transcript records where item N ended, and no "
                "reader can infer it afterwards."
                "\n\n"
                "A marker SEPARATES TURNS, not regions within a turn. It is "
                "appended at a turn boundary, normally the end of this turn, so "
                "it lands after your closing message for the item just finished "
                "and before anything for the next one. If one turn walks several "
                "items, every marker it emits clumps at that turn's end — so "
                "call this once per turn, at the seam."
                "\n\n"
                "This changes only what is RENDERED. It does not touch the "
                "model's memory: use reset_conversation for that, and the two "
                "compose — mark the section for the view, reset the conversation "
                "for the context."
                "\n\n"
                "Restrictions: dashboard chat sessions only, because the effect "
                "is a rendering — a messaging-channel turn has no transcript "
                "surface to draw a labelled rule on. Headless producers (cron "
                "jobs, sub-agents, task runners) are refused too, and refused "
                "EVEN when the session has an open dashboard tab: such a turn can "
                "run on a user's slot and inherit a surface it did not open, and "
                "writing a structural row into a human's transcript requires that "
                "a human asked for it. Also refused during multi-stage plan "
                "execution: a held row would not land at that stage's boundary "
                "but at the end of the whole plan, so draw the marker before the "
                "plan starts or after it finishes."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": (
                            "Short heading for the section that is ENDING, e.g. "
                            "the item id or name you just finished. The row "
                            'renders it as "End of: <label>", so write the bare '
                            "id and let the row supply the direction — do not "
                            'write "End of" yourself. Max 120 chars. Omit to '
                            'draw a generic "End of section" rule instead.'
                        ),
                    },
                },
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
                "Restrictions: dashboard sessions only, and only when a human is "
                "driving the turn (Slack, cron, and subagent contexts are rejected "
                "— a headless turn is refused even when it inherited an open tab). "
                "One card at a "
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

    # ``task_name`` is used whole; only the ``spec`` fallback is bounded, so
    # only that branch needs the redact-then-bound composition — bounding first
    # can cut a credential into fragments no redaction regex matches.
    if task_name:
        safe_label, _ = redact_exfiltration_urls(task_name)
        safe_label, _ = redact_credentials(safe_label)
    else:
        safe_label = redact_and_truncate(spec, 80)
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
    # earlier sleep can never terminate the next one in the same session. The
    # same reply also ends the sleep when a mid-turn steer lands after it
    # began: the backend can only inject a steer at a model-inference
    # boundary, and this sleep is the absence of one (see _wait_end_reason).
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
    # `require_strict_session_key` is the shared gate for exactly this class
    # of session-mutating tool (monitor_start, autonudge_stop, set_project)
    # -- it drops the walk and accepts only gateway-injected caller context,
    # KIROCREW_SESSION_KEY, or a HMAC-verified pid sidecar.
    # When it comes back empty the identity is a guess, so the ping degrades
    # to the original `{}` touch: the session still cannot be reaped
    # mid-sleep, and the countdown simply never appears. Tracked in #2347,
    # which is the work that lets this gate go away.
    _identified = bool(mcp_core.require_strict_session_key("the wait keepalive ping")[0])
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
                    (
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
                        else {}
                    ),
                )
            except Exception:
                reply = {}  # keepalive is best-effort
            # Only a request naming this wait ends it. `_post` returns
            # {"error": ...} on a failed round-trip rather than raising, so
            # the equality check doubles as the error guard. Gated on
            # `_identified` too: an unidentified sleep sends no wait_id, so a
            # matching reply could only mean the backend is answering about
            # somebody else's wait.
            if _identified and isinstance(reply, dict) and reply.get("end_wait") == wait_id:
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


def _emit_directive(kind: str, args: dict[str, Any], human: str) -> str:
    """Encode a validated directive AND publish it out of band; return the text.

    Two delivery paths, one each way round:

    * The MARKER in the returned text is the original path. A consumer that can
      verify the call's ``_meta.kiro`` identity (kiro-cli) decodes and applies it
      from there, exactly as before — this function changes nothing for that
      backend.
    * The out-of-band POST is the provider-neutral path. ``_post`` already carries
      ``X-Session-Key`` (and the gateway kernel-verifies that claim on the unix
      socket), so the gateway parks the payload for the RIGHT session without the
      model's tool result being trusted for anything. A backend that emits no
      ``_meta.kiro`` identity has no other way to reach its own control plane.

    Order matters: encode FIRST. ``encode`` refuses an oversized payload by
    returning a marker-less error string, and a refused directive must NOT be
    published — otherwise the model is told "nothing was applied" while a record
    sits waiting to apply it.

    Fail-soft on the POST, and SILENT by design. An older gateway with no such
    route, or one that is simply down, must not turn a working tool call into an
    error: the marker is already in hand and the kiro-cli path still works. There
    is no log line because this module runs as a stdio MCP server, where the
    process's own streams are the protocol channel — and because the failure that
    matters is reported at the CONSUMER, which is the side that knows whether a
    directive actually landed.
    """
    out = session_directive.encode(kind, args, human)
    if session_directive.is_refusal(out):
        return out
    try:
        mcp_core._post("/api/session-directive", {"kind": kind, "args": args})
    except Exception:
        pass
    return out


def autonudge_stop(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, AUTONUDGE_STOP_SCHEMA)

    # Resolve the current session's binding key and stop any loop on it.
    # STRICT resolution via the shared gate (env-var only, no PID walk): this
    # tool mutates another process's persistent loop state, and a subagent
    # lives under the parent slot's process tree — a PID-walk would let it
    # silently stop the PARENT session's loop (matches set_project's rule).
    # Resolve-half only: an empty key deliberately does NOT refuse here — it
    # falls through to the directive, whose consumer resolves its own session.
    sk, _ = mcp_core.require_strict_session_key("autonudge_stop")
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
    return _emit_directive(
        "autonudge_stop",
        {"reason": args.get("reason", "").strip()},
        # NOT a confirmation, and worded so a model cannot read it as one: this
        # tool resolves no session, so it cannot know whether a loop is bound
        # here. The consumer applies the stop and records the real outcome —
        # including "nothing was stopped" when the binding resolves no loop —
        # onto the transcript, so a caller that reads this as success would be
        # acting on an unverified claim.
        "Stop REQUESTED for this session's auto-nudge loop. This is not "
        "confirmation that a loop was found or stopped; the applied outcome is "
        "recorded separately and may report that nothing was stopped.",
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
    # Resolve-half of the shared strict gate only: ask_question gates on the
    # dashboard surface, not on identity, so an empty key is not a refusal.
    sk, _ = mcp_core.require_strict_session_key("ask_question")
    if sk and not has_dashboard_surface(sk):
        return (
            "ask_question only works from a dashboard chat session "
            f"(current session_key={sk!r}). From other surfaces, end your "
            "turn with an [OPTIONS: a | b | c] tag instead — it renders "
            "clickable buttons on every channel that supports them."
        )
    # Deep per-question/option validation, AUTHORITATIVELY here rather than in
    # the shallow schema: a malformed nested question must be rejected before the
    # model is told a card was posted, not surface later as a card-post failure.
    # RETURNED, not raised: an escaped exception is turned into the same
    # ``"Error: …"`` text by the JSON-RPC layer, but it escapes this server's own
    # return path — so it is neither audited with the call's args nor tagged as a
    # refusal, and the consumer reads a decline as a LOST DIRECTIVE MARKER
    # (#8635). Returning keeps the model-facing text identical and keeps the
    # "marker or refusal, nothing in between" invariant total.
    try:
        questions = validate_ask_user_question(args)
    except ValidationError as exc:
        return f"Error: {exc}"
    return _emit_directive(
        "ask_question",
        {"questions": questions},
        "Question card requested for this session. End your turn now — if it "
        "renders, the user's answer arrives as your next message (do NOT "
        "re-ask or guess in the meantime). If no dashboard client is "
        "attached the card is dropped, so ask in plain text instead.",
    )


def monitor_start(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, MONITOR_START_SCHEMA)
    # STRICT resolution via the shared gate (env-var only, no PID walk):
    # monitor_start creates a persistent unattended loop that repeatedly runs
    # tools in the bound session. A subagent under the parent's process tree
    # must NOT be able to PID-walk into the parent's identity and mint a loop
    # the parent user never asked for (crosses the session authorization
    # boundary). Resolve-half only: the short-circuit below is on context,
    # not identity, so an empty key falls through to the directive.
    sk, _ = mcp_core.require_strict_session_key("monitor_start")
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
    # The one escape from gating, and deliberately an opt-OUT. An opt-IN is what
    # this change exists to stop shipping: five consecutive opt-in mechanisms
    # measured zero adoption, because the default never moved. An opt-out does
    # not share that failure -- the default gates everything, and this only
    # releases the minority of loops whose duty is to act WHILE the subject is
    # quiet (refresh a heartbeat, chase a silent reviewer, rebase onto a moving
    # base). Those loops previously had no control but the wording of their own
    # instruction, which is a fragile thing to key a cadence on.
    gate = args.get("gate")
    gate = True if gate is None else bool(gate)
    # Infer from the message AS IT WILL BE STORED. The authorizer redacts
    # exfiltration URLs and credentials at its own chokepoint before persisting,
    # so a subject named by a URL the redactor rewrites survives here but not
    # there -- and the ack would then promise gating for a loop that is armed
    # ungated. Applying the same transform first makes the disclosure describe
    # the loop that will actually exist.
    stored_message, _ = redact_exfiltration_urls(message)
    stored_message, _ = redact_credentials(stored_message)
    gated = autonudge.infer_monitor(stored_message, time.time()) if gate else None
    # ``banner`` is CONDITIONAL, unlike the fields above: a caller that sets no
    # banner must see the payload shape it saw before, because the tool's
    # contract test asserts this dict by EXACT equality. The applier reads it
    # with ``.get``, so absent and empty mean the same thing there.
    banner = str(args.get("banner") or "").strip()
    payload: dict[str, Any] = {
        "message": message,
        "idle_secs": interval_secs,
        "max_cycles": max_cycles,
        "max_runtime_secs": max_runtime_secs,
        "gate": gate,
    }
    if banner:
        payload["banner"] = banner
    # Say whether this loop will be GATED, in the ack, at the surface that armed
    # it. This calls the SCHEDULER'S OWN decision function rather than
    # re-deriving the answer from the target: a subject can infer cleanly and
    # still fail to form a valid monitor, so a second evaluation could claim a
    # gate the loop never got -- and a disclosure that can be wrong is worse than
    # none. Without any disclosure the ack promises a plain re-injection "every
    # {interval}s", which for a gated loop is untrue, and this whole change
    # exists because a cadence change nobody could see had no effect.
    return _emit_directive(
        "monitor_start",
        payload,
        (
            "Monitor loop requested on this session: "
            + (
                f"observing {gated.target} every {interval_secs}s and "
                "re-injecting the message only when it changes, so quiet cycles "
                "cost no turn"
                + (f" and the {max_cycles} cap counts delivered turns" if max_cycles else "")
                if gated is not None
                else f"the message will re-inject every {interval_secs}s"
            )
            + " (user messages defer a due "
            "fire to their turn's end without restarting the countdown)"
            + (f", stopping after {max_cycles} cycles" if max_cycles else ", with NO cycle cap")
            + (f", wall-clock budget {max_runtime_secs}s" if max_runtime_secs else "")
            + ". End your turn now; once the loop is armed it wakes you on "
            "that interval — but arming happens when this turn's result is "
            "processed, and only a live dashboard/Slack/Discord session can "
            "host a loop, so do NOT assume it armed. Call autonudge_stop when "
            "the exit condition is met; hitting the cap is a runaway backstop, "
            "not a finish. Use monitor_update if the instruction goes stale."
        ),
    )


def _monitor_context_refusal(tool_name: str, session_key: str, message: str) -> str:
    """Return a failed tool result and retain the security-relevant refusal."""
    mcp_core.sel().log_tool_invocation(
        session_key=session_key or "mcp_core",
        source="mcp",
        tool_name=tool_name,
        outcome="denied",
        error="unsupported_session_binding",
    )
    return f"Error: {message}"


def _parsed_pull_request_target(raw: Any) -> tuple[str, str]:
    """Return ``(url, "")`` for a valid PR target, or ``("", "Error: …")``.

    ONE guarded parse for BOTH callers (``monitor_watch`` and ``monitor_update``)
    rather than a ``try`` at each site. `parse_github_pull_request_target` raises,
    and a raise from a directive tool escapes this server's own return path: the
    JSON-RPC layer turns it into the same ``"Error: …"`` text, but past the point
    that tags a decline as a refusal, so the consumer reads it as a LOST directive
    marker and fires the WARNING reserved for a transport regression. Guarding the
    two sites separately is what let the second one ship unguarded (#8635); a
    single seam is what makes the next caller correct by construction.
    """
    try:
        return parse_github_pull_request_target(str(raw)).url, ""
    except ValueError as exc:
        return "", f"Error: {exc}"


def monitor_watch(name: str, args: dict[str, Any]) -> str:
    """Validate and emit a session-bound structured monitor directive."""
    args = validate_tool_args(args, MONITOR_WATCH_SCHEMA)
    sk, strict_err = mcp_core.require_strict_session_key(
        "monitor_watch requires an authenticated strict session binding. "
        "No process-ancestor fallback is used."
    )
    if not sk:
        return _monitor_context_refusal("monitor_watch", sk, strict_err)
    if mcp_core._structured_monitor_binding_key(sk) is None:
        return _monitor_context_refusal(
            "monitor_watch",
            sk,
            "monitor_watch only works from within a dashboard, Slack, or "
            f"Discord session (current session_key={sk!r}).",
        )
    target, target_error = _parsed_pull_request_target(args["target"])
    if target_error:
        return target_error
    payload = {
        "kind": args["kind"],
        "target": target,
        "objective": args["objective"],
        "cadence_secs": int(args.get("interval_secs") or DEFAULT_MONITOR_CADENCE_SECS),
        "max_runtime_secs": int(args.get("max_runtime_secs") or DEFAULT_MONITOR_RUNTIME_SECS),
        "max_agent_turns": int(args.get("max_agent_turns") or DEFAULT_MONITOR_AGENT_TURNS),
        "max_tokens": int(args.get("max_tokens") or DEFAULT_MONITOR_TOKENS),
        "max_provider_errors": int(
            args.get("max_provider_errors") or DEFAULT_MONITOR_PROVIDER_ERRORS
        ),
        "wake_instructions": str(args.get("wake_instructions") or "").strip(),
    }
    return _emit_directive(
        "monitor_watch",
        payload,
        "Structured monitor requested for this session. End your turn; inspect the monitor "
        "to confirm the authoritative consumer armed it.",
    )


def monitor_inspect(name: str, args: dict[str, Any]) -> str:
    """Read only the monitor bound to a verified strict session identity."""
    validate_tool_args(args, MONITOR_INSPECT_SCHEMA)
    sk, strict_err = mcp_core.require_strict_session_key(
        "Monitor inspection unavailable without an authenticated strict session binding. "
        "No process-ancestor fallback is used."
    )
    if not sk:
        return _monitor_context_refusal("monitor_inspect", sk, strict_err)
    if mcp_core._structured_monitor_binding_key(sk) is None:
        return _monitor_context_refusal(
            "monitor_inspect",
            sk,
            f"monitor_inspect is unavailable for this session type ({sk!r}).",
        )
    result = mcp_core._get("/api/autonudge/session-monitor", session_key=sk)
    if result.get("error"):
        return f"Error: Monitor inspection failed: {result['error']}"
    return json.dumps(
        _compact_monitor_inspection(result),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _compact_monitor_inspection(result: dict[str, Any]) -> dict[str, Any]:
    """Project the browser record into a bounded, agent-oriented status."""
    compact = {key: result.get(key) for key in ("enabled", "active", "monitor_id") if key in result}
    raw = result.get("monitor")
    if not isinstance(raw, dict):
        compact["monitor"] = None
        return compact
    fields = (
        "kind",
        "target",
        "objective",
        "budgets",
        "cadence_secs",
        "last_fingerprint",
        "last_wake_fingerprint",
        "wake_in_flight",
        "wake_count",
        "agent_turns",
        "input_tokens",
        "output_tokens",
        "probe_count",
        "provider_error_count",
        "consecutive_provider_errors",
        "last_probe_at",
        "created_ts",
        "last_decision",
        "last_wake_reason_code",
        "last_provider_error",
        "next_probe_at",
        "outcome",
        "stopped_reason",
        "user_stop_reason",
        "stopped_at",
    )
    monitor = {key: raw.get(key) for key in fields}
    observation = raw.get("last_observation")
    if isinstance(observation, dict):
        observation_fields = (
            "state",
            "draft",
            "head_revision",
            "mergeability",
            "review_decision",
            "blocking_review",
            "unresolved_review_threads",
            "review_threads_complete",
        )
        summary = {key: observation.get(key) for key in observation_fields}
        checks = observation.get("checks")
        if isinstance(checks, dict):
            check_summary: dict[str, Any] = {}
            for status in ("failed", "pending", "unknown"):
                values = checks.get(status)
                if isinstance(values, list):
                    check_summary[status] = values[:MAX_MONITOR_CHECK_NAMES]
                    check_summary[f"{status}_count"] = len(values)
            passed = checks.get("passed")
            if isinstance(passed, list):
                check_summary["passed_count"] = len(passed)
            summary["checks"] = check_summary
        monitor["observation"] = summary
    compact["monitor"] = monitor
    return compact


def monitor_stop(name: str, args: dict[str, Any]) -> str:
    """Emit a durable structured-stop directive without caller identity."""
    args = validate_tool_args(args, MONITOR_STOP_SCHEMA)
    sk, strict_err = mcp_core.require_strict_session_key(
        "monitor_stop requires an authenticated strict session binding. "
        "No process-ancestor fallback is used."
    )
    if not sk:
        return _monitor_context_refusal("monitor_stop", sk, strict_err)
    if mcp_core._structured_monitor_binding_key(sk) is None:
        return _monitor_context_refusal(
            "monitor_stop",
            sk,
            "monitor_stop only works from within a dashboard, Slack, or "
            f"Discord session (current session_key={sk!r}).",
        )
    return _emit_directive(
        "monitor_stop",
        {"reason": str(args.get("reason") or "").strip()},
        "Structured monitor stop requested for this session.",
    )


def monitor_update(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, MONITOR_UPDATE_SCHEMA)
    # STRICT resolution via the shared gate, same rationale as
    # monitor_start/autonudge_stop: this mutates persistent loop state that
    # drives unattended turns, so a subagent must not PID-walk into the
    # parent's identity and rewrite the parent session's instruction.
    sk, strict_err = mcp_core.require_strict_session_key(
        "monitor_update requires an authenticated strict session binding. "
        "No process-ancestor fallback is used."
    )
    # Stateless: short-circuit only un-appliable contexts; the
    # consumer resolves the loop by its own session and patches it.
    if not sk:
        return _monitor_context_refusal("monitor_update", sk, strict_err)
    if mcp_core._structured_monitor_binding_key(sk) is None:
        return _monitor_context_refusal(
            "monitor_update",
            sk,
            "monitor_update only works from within a dashboard, Slack, or "
            f"Discord session (current session_key={sk!r}).",
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
    if args.get("target") is not None:
        patch["target"], target_error = _parsed_pull_request_target(args["target"])
        if target_error:
            return target_error
    if args.get("objective") is not None:
        patch["objective"] = str(args["objective"])
    for field in ("max_agent_turns", "max_tokens", "max_provider_errors"):
        if args.get(field) is not None:
            patch[field] = int(args[field])
    if args.get("wake_instructions") is not None:
        patch["wake_instructions"] = str(args["wake_instructions"]).strip()
    # Blank is KEPT here, unlike ``message`` above which rejects it: a loop with
    # no instruction cannot fire, but a loop with no banner is the default state,
    # so "" has to round-trip as a request to CLEAR. Dropping it as "unchanged"
    # would make a banner set once impossible to remove without tearing the loop
    # down and losing its cycle count.
    if args.get("banner") is not None:
        patch["banner"] = str(args["banner"]).strip()
    if not patch:
        mcp_core.sel().log_tool_invocation(
            session_key=sk, source="mcp", tool_name="monitor_update", outcome="noop"
        )
        return (
            "monitor_update: nothing to change — pass at least one of "
            "message, interval_secs, max_cycles, max_runtime_secs."
        )
    return _emit_directive(
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
    return _emit_directive(
        "set_project",
        {"project": args.get("path", ""), "clear": bool(args.get("clear"))},
        "Project change requested for this session; if the path is valid "
        "and permitted it takes effect on the next message (cold-start with "
        "the new CWD and project steering). An invalid or sensitive path is "
        "rejected when this turn's result is processed.",
    )


def reset_conversation(name: str, args: dict[str, Any]) -> str:
    validate_tool_args(args, RESET_CONVERSATION_SCHEMA)
    # Stateless: the session-aware consumer (chat_runner) queues the discard
    # against ITS OWN slot — no session identity resolved here. The payload is
    # empty because there is nothing to choose: a caller asking for a clean
    # context always wants a clean one, and the HTTP route carries a replay flag
    # for the rare caller that does not.
    return _emit_directive(
        "reset_conversation",
        {},
        "Conversation reset requested for this session; if this turn is "
        "user-facing it takes effect at a turn boundary, and the next message "
        "starts with no memory of this conversation. The transcript is not "
        "deleted — write down anything that must carry forward.",
    )


def section_marker(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SECTION_MARKER_SCHEMA)
    # Stateless: the session-aware consumer (chat_runner) appends the row to ITS
    # OWN slot's transcript — no session identity resolved here.
    #
    # The confirmation HEDGES because the applier refuses on three paths this tool
    # cannot see; both consumer paths then replace this text with the real outcome.
    return _emit_directive(
        "section_marker",
        {
            "label": args.get("label", "") or "",
        },
        "Section break requested. If this turn is user-facing and not inside a "
        "multi-stage plan, it appears at the end of this turn, after this turn's "
        "closing message — but this tool does not apply it, so do not assume it "
        "rendered. It changes only what is rendered — no model context is dropped.",
    )


def suggest_followup(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SUGGEST_FOLLOWUP_SCHEMA)
    items = args.get("items") or []
    # Stateless: the session-aware consumer (chat_runner) broadcasts
    # the card to ITS OWN slot; no session identity resolved here. The card
    # is broadcast-only (dropped if no client attached), so the confirmation
    # stays cautious — restate the follow-ups in reply text if they matter.
    return _emit_directive(
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
    "monitor_watch": monitor_watch,
    "monitor_inspect": monitor_inspect,
    "monitor_stop": monitor_stop,
    "monitor_update": monitor_update,
    "set_project": set_project,
    "reset_conversation": reset_conversation,
    "section_marker": section_marker,
    "suggest_followup": suggest_followup,
}
