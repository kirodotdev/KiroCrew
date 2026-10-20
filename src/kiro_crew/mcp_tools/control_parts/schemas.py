"""The control tools' advertisement half: each tool's name, description and input schema.

:func:`schemas` builds the descriptors ``tools/list`` answers from for the control domain.
The two config reads that shape a description -- the structured-arming preference and the
monitor runtime ceiling -- live beside it, and the steering sentences they choose between
are the constants below. The handlers that run these tools are the other half:
``kiro_crew.mcp_tools.control.HANDLERS`` maps each name advertised here to one of them.

This module is an owner of :mod:`kiro_crew.mcp_tools.control`, which serves every name
here and stays the import path and the patch seam: a patch through it reaches this module
too (:mod:`kiro_crew._facade_mirror`). It never imports the facade.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.autonudge_judge import ending_phrase, screen_phrase
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mcp_tools._limits import (
    _MONITOR_DEFAULT_MAX_CYCLES,
    _MONITOR_DEFAULT_MAX_RUNTIME_SECS,
)
from kiro_crew.monitoring.limits import DEFAULT_RUNTIME_CEILING_SECS, runtime_ceiling_secs
from kiro_crew.monitoring.models import (
    MAX_MONITOR_AGENT_TURNS,
    MAX_MONITOR_CADENCE_SECS,
    MAX_MONITOR_PROVIDER_ERRORS,
    MAX_MONITOR_TOKENS,
    MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS,
    MIN_MONITOR_CADENCE_SECS,
)
from kiro_crew.monitoring.registry import (
    publicly_armable_kinds,
    publicly_armable_objectives,
)

#: The facade's logger: records from this owner keep the module name they always carried,
#: so log filters and captures keyed on it still match.
logger = logging.getLogger("kiro_crew.mcp_tools.control")

#: The one value ``watch`` accepts on ``monitor_start`` / ``monitor_update``, spelled as a
#: LITERAL for the same reason ``kiro_crew.probes`` and ``kiro_crew.validation`` each spell
#: it as one: this module builds the model-facing descriptors and must not import the
#: observation layer to advertise a string. It has to stay equal to
#: ``validation._WATCH_WORK_LEDGER`` (the schema's own allowed set) and to
#: ``probes.WORK_LEDGER`` (the kind the service resolves), which
#: ``test_the_watch_field_is_spelled_the_same_at_every_layer`` pins.
_WATCH_WORK_LEDGER = "work-ledger"

#: The sentences in the two monitoring descriptors that decide WHICH SIDE has to
#: justify itself before a supported pull request is armed.
#: ``monitoring.prefer_structured_arming`` picks one; nothing else in either
#: description moves, and neither tool is refused.
#:
#: The two positions are NOT two different routes. Both send evidence the typed
#: provider cannot observe -- comments, advisory review findings -- to the prompt
#: loop. What moves is the burden: off, the structured path is admissible only
#: once the caller has satisfied itself the objective is fully typed-decidable,
#: which is a judgement that leans to the loop whenever the caller is unsure; on,
#: a supported pull request is enough and the loop is the exception that needs its
#: own reason. Describing this as a swap of two defaults would be false, and the
#: help text does not.
#:
#: Both positions are spelled out in full rather than built from a shared stem.
#: The off text has to make a positive claim of its own, because a test can tell
#: "the flag was read and resolved off" from "the flag was never read" only when
#: the two positions say different things -- an off position that merely OMITS
#: the structured wording is indistinguishable from a read that never happened.
_ARMING_STEER_STRUCTURED_ON_CONDITION = (
    "Use monitor_watch for supported pull-request review readiness only when "
    "the objective is fully determined by typed provider facts. Use the prompt "
    "loop when comments or advisory review evidence must be interpreted. "
)
_ARMING_STEER_STRUCTURED_BY_DEFAULT = (
    "On a supported pull request this installation arms monitor_watch by "
    "default, and this prompt loop is the exception: take it when comments or "
    "advisory review evidence must be interpreted, which the typed provider "
    "cannot observe. "
)
#: Appended to ``monitor_watch``'s own description in the on position, so the
#: preference is stated on the tool it points AT and not only on the one it
#: points away from.
_WATCH_STEER_STRUCTURED_DEFAULT = (
    " This installation arms this path by default for a supported pull request."
)


def _prefers_structured_arming() -> bool:
    """Whether this installation arms the structured monitor by default.

    Read fresh on every descriptor build. That is what keeps a Settings change
    from needing a gateway restart: ``mcp_tools.build_tool_list`` rebuilds the
    descriptors per call and deliberately does not cache them. It does NOT
    reach a session that is already open, because kiro-cli caches a session's
    tool list for that session's life -- the same limitation
    ``mcp_tools/browser.py`` records for ``dashboard.use_builtin_browser``.

    Fails to the OFF position on any error: off is the shipped behaviour, and a
    config a gateway cannot parse must not silently re-point every arming
    decision it is about to advise on. The catch stays broad because this runs
    inside the tool-list build, where an escaping exception would withdraw EVERY
    tool rather than one sentence -- so the failure is logged instead of
    narrowed, which is what keeps a defect here discoverable rather than
    concealed.
    """
    try:
        return bool(KiroCrewConfig.load().monitoring.prefer_structured_arming)
    except Exception:
        logger.debug("monitoring.prefer_structured_arming unreadable; using off", exc_info=True)
        return False


def _ending_clause() -> str:
    """The one thing that ENDS a watch, capitalised to open a sentence."""
    return ending_phrase()


def schemas(*, names_only: bool = False) -> list[dict[str, Any]]:
    """Descriptors for the control tools.

    ``names_only`` is set by the in-process discovery read (via
    ``mcp_tools.build_tool_names``), which keeps only tool NAMES and discards
    every description. Under it the two reads that exist solely to shape a
    description -- the structured-arming preference and the monitor runtime
    ceiling, both config reads -- are skipped, so a names-only caller performs no
    on-loop disk work for text it throws away. The returned names and their order
    are unchanged. This is why neither read needs a ``get_running_loop`` guard of
    its own: the names path never reaches them, and the full-descriptor path (the
    stdio server serving a model) always wants the live values.
    """
    prefer_structured = False if names_only else _prefers_structured_arming()
    # A failed descriptive read must not withdraw every control tool, and the
    # actual invocation still validates the current policy at the mutation
    # boundary -- so a default here is safe. The names-only path skips the read
    # outright (its result would be discarded).
    runtime_ceiling = DEFAULT_RUNTIME_CEILING_SECS
    if not names_only:
        try:
            runtime_ceiling = runtime_ceiling_secs()
        except Exception:
            logger.debug("monitor runtime descriptor unavailable; using default", exc_info=True)
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
            "name": "route_crew",
            "description": (
                "Rank the crews whose triggers match a task, best first, and return each "
                "one's score, description and memory store. Use this when you want the "
                "same task to reach the same crew every time; use select_crew when you "
                "want the roster and intend to judge the fit yourself. Only when both "
                "`matches` and `unavailable` are empty does no crew claim the task; "
                "handle that case on the default crew. Report unavailable members and "
                "their reasons without substituting Global memory. Acting on a match means "
                "spawn_run(crew=<name>), which is what gives that run the crew's memory "
                "and template and keeps another crew's memory out of it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The task to route. Usually the user's own words.",
                    },
                },
                "required": ["task"],
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
                "spawn_run(crew=<name>) -- `crew=`, NOT `agent=`: `agent` names a "
                "kiro-cli template, and passing a crew name there gives the run the "
                "DEFAULT memory store, silently, which is how one crew's work ends up "
                "in another's memory. Selection rules: (1) pick a crew ONLY when its "
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
                "Legacy loops are removed. For a structured monitor, this compatibility "
                "alias records a durable user-stop outcome and retains the record for "
                "inspection. Safe to call even if no loop is active."
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
                "custom answer in the card's free-text field). In a dashboard "
                "session the call BLOCKS until the user responds and the answers "
                'are this tool\'s result, one \'"<question>" -> "<answer>"\' line per '
                "question — do NOT end your turn; continue with the answers. The "
                "result instead says when the user dismissed the card, did not "
                "answer in time, or replied in chat (then read their next message), "
                "or why no card could be shown (then ask in plain text). "
                "Use it when a decision is genuinely "
                "needed before the work can continue (which of these approaches, "
                "which account, confirm before I refactor). When you are ending "
                "your turn anyway a final [OPTIONS: a | b | c] tag is cheaper and "
                "renders on every channel — the card's advantage is several "
                "questions at once, multi-select and the free-text field. "
                "Dashboard sessions only: from another surface the call returns an "
                "[OPTIONS:] steer instead of a card."
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
                    # No timeout_secs: the gateway sets the wait window.
                },
                "required": ["questions"],
            },
        },
        {
            "name": "nothing_to_do",
            "description": (
                "End this turn with NO reply. The turn-end contract: after your tool "
                "calls, a turn ends either with a closing text or with this call — "
                "never by simply stopping after a tool. Call it when the turn ran tools "
                "and found nothing the user needs to read: a quiet patrol cycle, a check "
                "that found no change, a cron wake with nothing to report. It is "
                "TERMINAL: write nothing and call nothing after it. Never use it to skip "
                "answering a direct question, to end a turn the user is waiting on, or "
                "when work is still unfinished — the host REFUSES it on a turn a person "
                "opened (and on every messaging-channel turn), and the normal empty-reply "
                "handling then runs. The optional note is recorded as a quiet transcript "
                "step the user can inspect; it is not a message to them."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": (
                            "Optional one-line reason there is nothing to report, "
                            "e.g. 'patrol: no new activity on the watched PRs'. Recorded "
                            "on the transcript step only."
                        ),
                    },
                },
            },
        },
        {
            "name": "monitor_watch",
            "description": (
                "Watch a supported pull request with cheap provider probes. The owning session "
                "is woken only when a new revision needs action; unchanged, pending, retry, "
                "and terminal probes use no agent turn. Available from dashboard, Slack, and "
                "Discord sessions. One structured monitor per session."
                + (_WATCH_STEER_STRUCTURED_DEFAULT if prefer_structured else "")
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": sorted(publicly_armable_kinds())},
                    "target": {"type": "string", "description": "Canonical provider PR URL"},
                    "objective": {"type": "string", "enum": sorted(publicly_armable_objectives())},
                    "interval_secs": {
                        "type": "integer",
                        "minimum": MIN_MONITOR_CADENCE_SECS,
                        "maximum": MAX_MONITOR_CADENCE_SECS,
                    },
                    "max_runtime_secs": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": runtime_ceiling,
                    },
                    "max_agent_turns": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MAX_MONITOR_AGENT_TURNS,
                        "description": (
                            "How many times this watch may wake its session. Omit it, or "
                            "pass 0, for no wake ceiling: the watch is then retired by its "
                            "runtime, token and provider-error budgets instead. Pass a "
                            "positive number only when a count of wakes is itself the "
                            "thing you want bounded."
                        ),
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
                "Inspect the monitor bound to your authenticated current session. "
                "Reports a structured monitor's full record, or a legacy timer "
                "loop's presence and cadence reading, whichever the session "
                "holds. Takes no session key or monitor id."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "monitor_stop",
            "description": (
                "Durably stop the monitor on your current session. A structured "
                "monitor is retained with its terminal outcome for inspection; a "
                "legacy timer loop is stopped and leaves no record behind, so a "
                "later monitor_inspect reports it as not armed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
            },
        },
        {
            "name": "monitor_start",
            "description": (
                "Start a finite prompt loop for repeated work on YOUR CURRENT session, "
                "including first-class self-session patrol by conductor agents. For "
                "monitoring targets, this is also the legacy fallback for targets, "
                "objectives, or required evidence unsupported by monitor_watch. "
                + (
                    _ARMING_STEER_STRUCTURED_BY_DEFAULT
                    if prefer_structured
                    else _ARMING_STEER_STRUCTURED_ON_CONDITION
                )
                + "Start a prompt loop on YOUR CURRENT session: every "
                "interval_secs the given message is re-injected into this same "
                "session as your next turn — same context, same tools, same "
                "conversation. The countdown is deadline-preserving: user "
                "messages defer a due fire until their turn ends but do NOT "
                "restart the interval, so checks stay on schedule even in an "
                "actively-used session. Works from dashboard chat, Slack "
                "threads, Discord DMs, and Webex conversations. Put the check instructions and "
                "the exit "
                "condition in the message, then END YOUR TURN — the loop wakes you on the "
                "interval. When the exit condition is met (or the user says "
                "stop), call autonudge_stop — reaching max_cycles is a runaway "
                "backstop, NOT a successful finish. From dashboard, Slack, or "
                "Discord, use monitor_update to revise or re-arm the instruction "
                "if what you are watching changes. On Webex, stop the loop and "
                "create a new finite one instead. One automation may occupy a "
                "session; monitor_start "
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
                f"the tick needs you: {screen_phrase()}. Where the screen is "
                "available, progress that asks nothing of you raises no wake and "
                "costs no model turn -- one "
                "lane of many finishing, a pending count shrinking, a bot "
                "posting its own status -- and a raised wake is held briefly, "
                "so it lands up to about one interval after the tick that "
                f"observed it. {_ending_clause()} ends the watch rather than "
                "waking you. "
                "max_cycles then counts the turns actually "
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
                            "interval even when the tick needs nothing from you. "
                            "Use it for a loop whose duty is to act "
                            "WHILE the subject is quiet -- refresh a heartbeat "
                            "file, chase a reviewer who still has not replied, "
                            "keep a branch rebased on a moving base -- since the "
                            "screen reads the pull request and continued "
                            "silence is invisible to it. Pass it too for a loop "
                            "that must see lanes land one at a time, since "
                            "per-lane progress raises no wake unless your own "
                            "wake criteria ask for it. A gated loop is "
                            "never starved (it is delivered anyway after enough "
                            "quiet intervals) so reach for this only when every "
                            "interval genuinely has work"
                        ),
                    },
                    "max_cycles": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1000,
                        "description": (
                            "Safety cap on delivered cycles (default "
                            f"{_MONITOR_DEFAULT_MAX_CYCLES}). Use a larger finite "
                            "value for a longer watch"
                        ),
                    },
                    "max_runtime_secs": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": runtime_ceiling,
                        "description": (
                            "Runtime budget in seconds (awake time while the "
                            "gateway runs; a restart counts the time since "
                            "created_ts on the wall clock), measured from when "
                            "the loop is armed (default "
                            f"{min(_MONITOR_DEFAULT_MAX_RUNTIME_SECS, runtime_ceiling)}; "
                            f"configured max {runtime_ceiling}). "
                            "Unlike max_cycles this "
                            "bounds running TIME, so a loop with slow turns or "
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
                            "as a transcript row on every single cycle. "
                            'Something like "watching PR #123 for CI" is '
                            "enough. Omit it for a short message, and omit it on "
                            "a channel-bound loop (`slack:`/`discord:`/`webex:`) "
                            "— a banner there is refused with a 400, since only "
                            "the dashboard transcript renders it"
                        ),
                    },
                    "judge": {
                        "type": ["object", "boolean"],
                        "description": (
                            "Optional WAKE JUDGE: say in plain words what is worth "
                            "waking this session for, and a cycle with nothing new "
                            "costs no turn at all. Before each cycle the evidence "
                            "your watched targets produced since the last one — a "
                            "watched session's new assistant lines, a watched pull "
                            "request's typed state and check tallies — is read and "
                            "answered against these two sentences. Reach for it "
                            "whenever what decides the answer is PROSE no typed "
                            "check can evaluate: worker transcripts you are "
                            "patrolling, or your own criterion applied to a pull "
                            "request's state. It never ends "
                            "the loop and it never silences one indefinitely — after "
                            "a run of quiet cycles one fires anyway — so a wrong "
                            "answer costs a late turn, not a missed one. You do not "
                            "have to pass it: once the evidence scope is granted, a "
                            "gated loop is screened on every cycle under a default "
                            "brief that asks whether the subject needs its owner, and "
                            "these two sentences REPLACE that default with your own. "
                            "Pass `false` to bypass the judge entirely; a `gate=false` "
                            "loop is never screened, since its duty is to act while "
                            "its subject is quiet"
                        ),
                        "properties": {
                            "wake_when": {
                                "type": "string",
                                "description": (
                                    "What genuinely needs this session's attention, "
                                    'in one sentence, e.g. "a worker line starts '
                                    'with RULING or BLOCKED" or "a reviewer asked '
                                    'for a change"'
                                ),
                            },
                            "quiet_when": {
                                "type": "string",
                                "description": (
                                    "What is progress not worth a turn, in one "
                                    'sentence, e.g. "workers report WORKING with '
                                    'no new status" or "checks are still running"'
                                ),
                            },
                            "targets": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Optional. What to read evidence from: dashboard "
                                    "chat keys (`chat-...`) and pull-request URLs. "
                                    "Omit it and the ones named in `message` are "
                                    "used, which is usually what you want"
                                ),
                            },
                        },
                    },
                    "watch": {
                        "type": "string",
                        "enum": [_WATCH_WORK_LEDGER],
                        "description": (
                            "Optional. Name the SUBJECT this loop observes, for the one "
                            "subject your instruction cannot name. Pass "
                            f'"{_WATCH_WORK_LEDGER}" to gate this loop on YOUR OWN work '
                            "ledger: a cycle where no worker you dispatched said "
                            "anything you must act on costs no turn, and a worker's "
                            "report of 'blocked', 'question' or 'done' pulls the next "
                            "cycle forward to within seconds instead of "
                            "waiting out interval_secs. Nothing else does: a report of "
                            "'progress', a worker ending its turn and a worker's session "
                            "closing all leave the interval as it is, so a worker that "
                            "crashed or went silent is found by the item's stale flag on "
                            "an ordinary cycle. For a conductor patrolling "
                            "dispatched workers this is the field to use, and it lets "
                            "you set interval_secs in hours -- the timer becomes the "
                            "liveness fallback, not the delivery path. Omit it and the "
                            "subject is inferred from `message`, which is how a pull "
                            "request is named. Gating does not need `gate` as well: "
                            "naming a watch gates the loop on its own"
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
                        "minimum": 1,
                        "maximum": 1000,
                        "description": (
                            "New cap on delivered cycles; raise it when a loop "
                            "is close to its cap but the work is still live. "
                            "Omit to leave unchanged"
                        ),
                    },
                    "max_runtime_secs": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": runtime_ceiling,
                        "description": (
                            "New runtime budget in seconds (awake time while the "
                            "gateway runs; a restart counts the time since "
                            "created_ts on the wall clock), measured from "
                            f"when the loop was first armed (configured max {runtime_ceiling}). "
                            "Omit to leave unchanged"
                        ),
                    },
                    "target": {
                        "type": "string",
                        "description": (
                            "New canonical PR/MR URL of the same provider kind "
                            "as the structured monitor"
                        ),
                    },
                    "objective": {"type": "string", "enum": sorted(publicly_armable_objectives())},
                    "max_agent_turns": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MAX_MONITOR_AGENT_TURNS,
                        "description": (
                            "New wake ceiling for a structured monitor. 0 removes the "
                            "ceiling, leaving the runtime, token and provider-error "
                            "budgets as the watch's only bounds."
                        ),
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
                    "judge": {
                        "type": ["object", "boolean"],
                        "description": (
                            "Revise the WAKE JUDGE on this loop, or arm one on a loop "
                            "that has none. Pass the two sentences again to replace "
                            "them; pass "
                            '`{"wake_when": "", "quiet_when": ""}` to drop your own '
                            "criteria, after which a gated loop runs under the "
                            "default brief (an empty object means the same, but "
                            "some clients drop it before it reaches the server); "
                            "pass `false` to bypass the judge entirely: a gated "
                            "pull-request loop then costs no turn while its reading "
                            "is unchanged and fires once on a change, and a loop "
                            "with nothing to read fires every cycle. Omit it "
                            "to leave the current brief untouched. Revising resets "
                            "the quiet-cycle count and the read positions, because "
                            "both describe the brief you are replacing"
                        ),
                        "properties": {
                            "wake_when": {
                                "type": "string",
                                "description": (
                                    "What genuinely needs this session's attention, "
                                    "in one sentence"
                                ),
                            },
                            "quiet_when": {
                                "type": "string",
                                "description": (
                                    "What is progress not worth a turn, in one " "sentence"
                                ),
                            },
                            "targets": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Optional. What to read evidence from: dashboard "
                                    "chat keys (`chat-...`) and pull-request URLs. "
                                    "Omit it and the ones named in `message` are used"
                                ),
                            },
                        },
                    },
                    "watch": {
                        "type": "string",
                        "enum": [_WATCH_WORK_LEDGER],
                        "description": (
                            "Optional. Point this loop's observation at YOUR OWN work "
                            f'ledger by passing "{_WATCH_WORK_LEDGER}", on a loop that '
                            "was armed as a plain timer -- without tearing it down and "
                            "losing its cycle count. After it, a cycle where no worker "
                            "said anything actionable costs no turn, and a worker's "
                            "report of 'blocked', 'question' or 'done' pulls the next "
                            "cycle forward. Omit it to leave the "
                            "loop's current subject alone"
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
                "Use after a skill scaffolds a new working tree (e.g. a new workspace), "
                "or after you run `git worktree add` or clone a checkout you will "
                "edit in, so the session retargets to the new source instead of the "
                "old one. "
                "Also use when the user asks you to work on a specific repository or "
                "project folder — calling set_project ensures the session's CWD is "
                "updated and future tool calls (file reads, bash commands) default to "
                "the correct location. "
                'To clear the project, pass path="" with clear=true. '
                "\n\n"
                "Restrictions: headless callers (cron jobs, subagents, task "
                "runners) are rejected — a cron turn can run on a user's "
                "dashboard slot and a subagent shares its parent's slot, so "
                "they must not retarget it. A turn another session sent (for "
                "example via session_send) is refused too: only a turn a person "
                "started may move the project. Sensitive paths (~/.aws, ~/.ssh, "
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
                "subagent shares its parent's slot, so neither may wipe it. A "
                "monitor wake is rejected too, unless this session runs a goal or "
                "ledger conductor and the wake comes from its own live loop, which it "
                "armed itself, with no question card or tool approval pending."
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
            "name": "chat_tag",
            "description": (
                "Tag THIS chat session on the dashboard board so a human scanning "
                "many sessions can see what each one needs. Use to move your own "
                "conversation between workflow states (e.g. flip it to Review when "
                "there is nothing left for you to do and it awaits the user), and to "
                "add or remove non-state labels."
                "\n\n"
                "Arguments (at least one required): set_state=<state tag id> sets the "
                "single mutually-exclusive workflow state (planned / todo / "
                "implementation / review / done), replacing whichever state tag the "
                "session currently carries; add=[ids] adds non-state labels; "
                "remove=[ids] removes labels. Each entry may be a tag id or a "
                "tag's display name; both resolve case-insensitively against "
                "the board's vocabulary (an id wins when a name collides with "
                "a different tag's id)."
                "\n\n"
                "PERMISSION: each tag carries an agent policy — a tag the human "
                "reserved for themselves is refused (tag_policy_denied), an "
                "add-only tag can be added but not removed, and workflow states "
                "are agent-writable by default on a fresh install or when newly "
                "created as status tags (an upgraded install starts with every "
                "tag human-only until granted: a set_state that meets a custom "
                "status tag with no protected record is refused "
                "status_identity_unprotected, and the dashboard owner restores it "
                "by choosing Set up agent permissions on that tag in the tag "
                "manager, then choosing Agent: add & remove). "
                "tag_grants_unavailable means the grants store itself is "
                "unreadable or was quarantined at boot (for example after a "
                "token-key rotation), not that a human reserved the tag: tell the "
                "user, who re-grants it from the tag manager. The result reports the "
                "session's RESULTING tag list, so this is also how you READ your own "
                "current tags — call it with just the change you want (or a no-op "
                "add of a tag already present) to see them."
                "\n\n"
                "Restrictions: slot-backed sessions only (a dashboard chat, or a "
                "messaging thread bound to a dashboard slot) — a standalone channel "
                "session with no slot is refused. Headless callers "
                "(cron jobs, subagents) are refused — a cron turn can run on a user's "
                "slot and a subagent shares its parent's, so neither may retag it. "
                "The change applies when this turn's result is processed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "set_state": {
                        "type": "string",
                        "description": (
                            "A workflow-state tag id (planned / todo / implementation "
                            "/ review / done). Replaces any state tag the session "
                            "currently carries — the states are mutually exclusive."
                        ),
                    },
                    "add": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tag ids to add (non-state labels only; a workflow-state id is refused — use set_state).",
                    },
                    "remove": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tag ids to remove.",
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
                "a git worktree off origin/HEAD, or the project's current HEAD when "
                "that does not resolve, opens a new chat "
                "session scoped to it, and pre-fills the composer with your prompt), "
                "'Add to this session' (pre-fills this session's composer with your "
                "prompt), and 'Skip'. Both non-skip buttons PRE-FILL the composer — "
                "the user still presses send — so nothing runs without their consent. "
                "When the session has no project directory, or its project directory "
                "is not a git repository (or is only a folder inside a repository "
                "rooted higher up), the first button is 'Start in new chat' "
                "instead: it opens a new chat session in the same project directory "
                "and sidebar folder, with no worktree and no git call, and pre-fills "
                "its composer (the tool result tells you when the session has no "
                "project directory); 'Add to this session' always works."
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
                "Restrictions: dashboard sessions only, AND only a turn a person "
                "sent. Slack has no card surface; cron, sub-agent, and task-runner "
                "turns are rejected even when they ride a user's slot with an open "
                "dashboard tab, because only a human-sent turn may post a card. One "
                "card at a time per slot: a new call replaces any card the user has "
                "not yet acted on."
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
