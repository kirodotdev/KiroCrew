"""The subagent spawning, steering, and host headroom tools: what they advertise and what they do.

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
import logging
import os
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlencode

from kiro_crew import mcp_core
from kiro_crew import resource_status as host_status
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.constants import DEFAULT_SUBAGENT_MAX_TURNS
from kiro_crew.context_management import COMPLETION_KEEP_DEFAULT_CHARS
from kiro_crew.execution_context import read_session_execution
from kiro_crew.mcp_shared import DeferredTool
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.subagent import (
    AGENT_INTERNAL_CODE,
    AGENT_NOT_AVAILABLE_CODE,
    AGENT_NOT_FOUND_CODE,
    agent_matches_allowlist,
    format_subagent_usage,
    is_internal_agent_spec,
    parent_spawn_allowlists,
    resolve_max_subagents,
    visible_agent_names,
)
from kiro_crew.subagent_persistence import agent_dir_for_display
from kiro_crew.subagent_wait_reasons import MEMORY_PRESSURE_PHRASE, queued_wait_text
from kiro_crew.validation import (
    MAX_MEDIUM_STRING,
    MAX_SHORT_STRING,
    SPAWN_CONTINUE_SCHEMA,
    SPAWN_RELEASE_SCHEMA,
    SPAWN_RUN_SCHEMA,
    SPAWN_STEER_SCHEMA,
    SPAWN_SUB_AGENTS_SCHEMA,
    validate_tool_args,
)

logger = logging.getLogger(__name__)

# A parked batch retains one refusal per entry, each bounded to this many characters.
_SA_ERROR_MAX_CHARS = 240

#: Bound on one ``/api/spawn/lost`` report posted from a deferred's ``cancel()``
#: or ``abandon()``, which the dispatch loop runs on its own thread: a gateway
#: that is not answering must cost that thread one short wait, not one
#: ``_post`` default timeout per unsubmitted task.
_LOST_REPORT_TIMEOUT_SECS = 5.0

# Roster carried in the spawn_run parameter descriptions. Kept small on purpose:
# a tool description is always-on context in every session, so this buys
# self-correction for a few dozen characters, not a full agent listing.
_MAX_ROSTER_NAMES = 8

# Owner recorded on an audit record when the resolver named no session. An empty
# owner is ambiguous by construction: a resolver whose every identity source
# failed and a spawn with genuinely no owning session both produce ``""``, and
# nothing downstream can recover which one it was. This marker names the failed
# resolution where it happens, so the audit trail says "the owner was lost"
# rather than naming no session at all.
#
# Same ``unresolved:<pid>`` wire format the computer-use shim already writes
# (``mcp_computer.UNRESOLVED_SESSION_PREFIX``), so one audit-reader vocabulary
# covers both producers. Deliberately NOT trustworthy attribution -- the prefix
# says it is not, so a reader cannot mistake a pid for a session identity.
_OWNER_UNRESOLVED_PREFIX = "unresolved:"


def _audit_owner(parent_session: str) -> str:
    """The owner to record on an audit record for *parent_session*.

    A resolved key is recorded verbatim. An empty one becomes the unresolved
    marker for THIS process, read at call time so a forked child cannot report
    its parent's pid.

    Audit-only. The spawn REQUEST keeps the empty owner, because
    ``parent_session_key`` addresses per-slot frame delivery and a synthetic key
    there would route frames at a slot that does not exist.
    """
    if parent_session:
        return parent_session
    return f"{_OWNER_UNRESOLVED_PREFIX}{os.getpid()}"


def _parent_template_for_roster() -> str:
    """The kiro agent template THIS tool server's session runs as, or ``""``.

    Advisory input to the roster only: the session key comes from the ordinary
    resolver (token, env, PID map), and its execution record names the template.
    A pool process that has not been rekeyed yet, a caller with no session, or a
    record this process cannot read all answer ``""`` -- and an empty answer means
    "filter nothing", the roster's pre-existing shape. The gateway's gate does its
    own resolution and is the decision; this only stops the description from
    advertising names that gate would refuse.
    """
    try:
        session_key = mcp_core._resolve_session_key()
        if not session_key:
            return ""
        execution = read_session_execution(session_key)
    except Exception:
        return ""
    return execution.template_id if execution is not None else ""


def _parent_allowlist_filter(names: Iterable[str]) -> tuple[list[str], bool]:
    """Keep the *names* the parent agent's spec allows spawning.

    Returns ``(kept, restricted)``: ``restricted`` is True only when the parent's
    spec DECLARES ``toolsSettings.subagent.availableAgents``; an omitted key, an
    unresolvable parent, or a spec the gate will refuse as unreadable (``None``
    from the resolver -- the gate's own denial names that, and this roster is
    advisory) keeps every name and reports False, so the roster a session without
    a declaration sees is the one it always saw. Bare-name aliasing is not
    applied here: this process has no verified app identity, and the roster
    lists installed names as the gate will see them.
    """
    allowlists = parent_spawn_allowlists(_parent_template_for_roster())
    if not allowlists:
        return list(names), False
    return [n for n in names if all(agent_matches_allowlist(n, al) for al in allowlists)], True


def _agent_roster_hint() -> str:
    """Valid agent names, for the ``agent``/``agents`` parameter descriptions.

    The roster is otherwise reachable only through ``spawn_list``'s OUTPUT, so a
    caller that goes straight to ``spawn_run`` never sees it and invents
    plausible-sounding names instead. Putting it in the parameter
    description puts it in front of exactly the caller that needs it.

    ADVISORY only, and deliberately never a gate: this process scans the
    user-level agents directory, while the gateway ALSO accepts a project-scope
    agent it cannot see from here. An incomplete roster is harmless as a hint --
    the gateway still owns the accept/refuse decision -- but refusing a name on
    this reading would reject an agent kiro-cli can load.

    Every name is matched against ``_AGENT_NAME_RE`` before it is rendered, then
    redacted, then the list is bounded -- all of it in
    ``subagent.visible_agent_names``, shared with the refusal roster and
    ``spawn_list`` so the filter cannot drift between them. The grammar is what
    makes this safe to put in front of a model: an agent spec's ``name`` field is
    taken verbatim by discovery with no validation, so a spec can declare a
    newline plus instruction-shaped text -- pure ASCII, and an isascii check would
    pass it straight into every session's tool list. The same grammar already
    gates the ``agent`` parameter in ``SPAWN_RUN_SCHEMA``, so a name that fails it
    is one no caller could pass here anyway.
    """
    try:
        # Sorted by DECLARED name, before redaction, so the order matches the
        # refusal roster's and a credential-shaped name is rewritten in place
        # rather than re-sorted into a different slot. Names the parent agent's
        # spec forbids spawning are dropped FIRST: advertising them would send
        # the model straight into the gate's refusal. So are Kiro Crew's own
        # generated specs (``is_internal_agent_spec``), which the gate refuses.
        names, restricted = _parent_allowlist_filter(
            sorted(
                a.name for a in mcp_core.list_agents() if a.name and not is_internal_agent_spec(a)
            )
        )
        shown, withheld = visible_agent_names(names, limit=_MAX_ROSTER_NAMES)
    except Exception:
        return ""  # never let a directory read break the tool advertisement
    if not shown:
        # A declared allowlist that admits no installed agent is still a fact the
        # caller needs: naming any agent will be refused, so say so rather than
        # advertising nothing and letting it guess.
        return (
            " This agent's toolsSettings.subagent.availableAgents allows none of the "
            "installed agents, so every spawn is refused; ask the operator to widen the list."
            if restricted
            else ""
        )
    hint = f" Valid names right now: {', '.join(shown)}"
    if withheld:
        hint += f" (+{withheld} more)"
    if restricted:
        hint += " (restricted by this agent's toolsSettings.subagent.availableAgents)"
    return hint + "."


def schemas(*, names_only: bool = False) -> list[dict[str, Any]]:
    """Descriptors for the spawn tools.

    ``names_only`` is set by the in-process discovery read (via
    ``mcp_tools.build_tool_names``), which keeps only tool NAMES and discards
    every description. Under it the two reads that exist solely to fill a
    description -- the live sub-agent cap and the agents-directory roster scan --
    are skipped, so a names-only caller never performs on-loop work for text it
    throws away. The returned names and their order are unchanged; only the two
    descriptions that would have carried those values are left without them.
    This is why neither read needs a ``get_running_loop`` guard of its own.
    """
    # Advertise the concurrent sub-agent cap so the model fans out with
    # confidence instead of self-limiting. The cap IN FORCE is preferred:
    # ``agent.max_subagents`` is a ceiling the adaptive controller may have cut
    # after admitted work kept failing, and a model sized to the ceiling queues
    # work it believes is running. The live figure comes from the in-process
    # registry only (``adaptive_exec_cap``, a dict read) -- this function runs
    # on the gateway's discovery cycle as well as in a tool server, and a
    # loopback request here would dial the gateway from inside the gateway. In
    # a tool server that registry is empty, so the configured ceiling is what
    # gets printed, LABELLED as a ceiling; ``resource_status`` is the tool that
    # pays for the API read and reports the live cap from any process.
    # resolve_max_subagents is the single source of truth for the ceiling
    # (agent.subagent_auto_max when agent.max_subagents is 0, else the explicit
    # pin; never 0) and the gateway's SubagentManager re-derives its ENFORCED
    # ceiling through the same function on every config reload. The count is a
    # high ceiling: free host memory bounds each start beneath it, which is what
    # the memory note says. A snapshot at tool-list time is fine: this is
    # advisory guidance, not an enforced limit, and SubagentManager
    # auto-queues any overflow regardless.
    _queue_note = (
        "; each start also waits until host memory can hold it, so a wide batch "
        "may start in waves. Submit only useful, ready independent tasks. Overflow "
        "queues automatically; capacity is a ceiling, not a target. Keep dependent "
        "tasks for a later batch."
    )
    # A names-only caller discards every description, so neither the live cap
    # nor the roster scan below runs for it: the two values exist only to fill
    # text it throws away. This is the whole reason those reads need no
    # running-loop skip -- the names path never reaches them.
    _cap_hint = ""
    _agent_hint = ""
    if not names_only:
        _live_cap = host_status.adaptive_exec_cap()
        if _live_cap > 0:
            _cap_hint = (
                f" You can run up to {_live_cap} sub-agents concurrently right now (the "
                "cap in force, at most your configured max)" + _queue_note
            )
        else:
            try:
                _max_sub = resolve_max_subagents(KiroCrewConfig.load())
            except Exception:
                _max_sub = 0
            _cap_hint = (
                f" Your configured sub-agent ceiling is {_max_sub}; the cap actually in "
                "force may be lower (the adaptive controller is not readable from this "
                "process -- resource_status reports it)" + _queue_note
                if _max_sub > 0
                else ""
            )
        # The valid agent names, read once and shared by every agent-taking field
        # below, so a caller that never called spawn_list still sees them.
        _agent_hint = _agent_roster_hint()
    # Context-scope switches, shared by spawn_run and spawn_sub_agents so the
    # rule cannot drift between them. The model reads these descriptions at
    # call time, which is why the rule lives here and not only in the prompt.
    _context_group_props = {
        "include_memory": {
            "type": "boolean",
            "description": (
                "Default true. Set false when the task is FULLY specified by the text "
                "you wrote — read these files, run this command, validate this finding, "
                "summarize this log. This is the normal case for parallel fan-out. If "
                "the sub-agent needs one fact from your memory, put that fact in the "
                "task text instead of turning this back on. Keep true when the task is "
                "open-ended about the user's own work or history."
            ),
        },
        "include_lessons": {
            "type": "boolean",
            "description": (
                "Default true. Set false ONLY when the sub-agent purely reads and "
                "reports (search, summarize, analyze, review). Keep true whenever it "
                "writes code, edits files, runs git, or pushes — the user's learned "
                "corrections live here and a sub-agent without them repeats mistakes "
                "the user already corrected."
            ),
        },
        "include_project": {
            "type": "boolean",
            "description": (
                "Default true. Set false when the work is outside the active project "
                "tree: web research, a different repo, pure reasoning."
            ),
        },
    }
    return [
        {
            "name": "spawn_run",
            "description": (
                "Spawn subagent(s) to run tasks in the background. "
                "One task is almost always faster done yourself: spawn for two or more "
                "independent tasks, or when a step would flood your context with bulk output. "
                "Returns immediately — results arrive as [Subagent completion event] "
                "messages in your conversation. For parallel work, use 'tasks' array. "
                "Tasks are automatically batched if they exceed the concurrency limit."
                + _cap_hint
                + " Validate all results before declaring the user task complete."
                " Follow the returned parent-work boundary; do not poll or duplicate child work."
                " If result batches from a previous spawn are still arriving,"
                " do not start a new spawn until all of them have been"
                " delivered and processed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "A bounded assignment with goal, ready inputs, ownership, "
                            "verifiable outputs and stop conditions. Never forward the whole "
                            "request to an equivalent worker merely to wait and relay."
                        ),
                    },
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": ["string", "object"],
                            "properties": {
                                "task": {"type": "string"},
                                "model": {"type": "string"},
                                "reasoning_effort": {"type": "string"},
                            },
                            "required": ["task"],
                            "additionalProperties": False,
                        },
                        "description": (
                            "Multiple tasks to run in parallel as one wave. Each entry is a "
                            "prompt string, or an object {task, model?, "
                            "reasoning_effort?} whose fields override the call's batch-wide "
                            "value for that task only, e.g. the same review prompt on two "
                            "models, delivered together."
                        ),
                    },
                    "agent": {
                        "type": "string",
                        "description": (
                            "Agent name for the subagent. An unknown name is REFUSED, "
                            "never silently replaced by the default, so use a name from "
                            "this list (or spawn_list) instead of guessing."
                        )
                        + _agent_hint,
                    },
                    "crew": {
                        "type": "string",
                        "description": (
                            "Crew Member name from select_crew or route_crew. "
                            "Selects that member's memory and provider template; agent "
                            "alone selects a template. Naming a member is enough; its "
                            "Triggers only steer automatic selection. Omit to inherit the "
                            "current member. Applies to every task."
                        ),
                    },
                    "target_member": {
                        "type": "string",
                        "description": (
                            "Explicit target Crew Member. Uses its existing memory without "
                            "copying the parent's learning. Omit to inherit the current member."
                        ),
                    },
                    "agents": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Agent names corresponding to each task in 'tasks' array. "
                            "Same rule as 'agent', which lists the valid names: every "
                            "name here must already exist."
                        ),
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": (
                            "Override tool-call budget for this spawn "
                            f"(default: config or {DEFAULT_SUBAGENT_MAX_TURNS})"
                        ),
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Optional absolute path to launch the subagent subprocess in, "
                            "instead of the default sandbox. Enables cwd-relative resource globs "
                            "(.kiro/steering, AGENTS.md, CLAUDE.md) to resolve against this directory. "
                            "Must be under a configured subagent_cwd_allowed_roots entry "
                            "(default: [~/workspace, ~/workspaces, ~/workplace, "
                            "~/workplaces]). Applies to all tasks in a batch spawn."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Optional model override for the subagent (e.g. 'deepseek-3.2', "
                            "'claude-haiku-4.5'). When set, the subagent runs on this model "
                            "instead of the gateway default. Model ids are per backend: on "
                            "the kiro backend list them with "
                            "`kiro-cli chat --list-models --format json`; another backend "
                            "(e.g. codex) accepts only ids from its own model list and "
                            "refuses ids from kiro's catalog."
                        ),
                    },
                    "reasoning_effort": {
                        "type": "string",
                        "description": (
                            "Optional reasoning-effort override for the subagent(s): "
                            "'low', 'medium', 'high', 'xhigh', or 'max' (empty/absent "
                            "= unset). Batch-wide — applies to every task in this "
                            "call and wins over the configured subagent role pin. "
                            "Setting it forces the dedicated-process path: each "
                            "subagent runs its own process (~3-5s start, ~400MB) "
                            "instead of session sharing (~200ms, near-zero memory), "
                            "so weigh it on a wide fan-out. Models that do not "
                            "support effort ignore the level, but the process cost "
                            "is still paid."
                        ),
                    },
                    "keep": {
                        "type": "boolean",
                        "description": (
                            "Optional. ALL runs are already continuable "
                            "best-effort (~1h retention) via spawn_continue — "
                            "keep=true additionally guarantees resumability "
                            "(dedicated process) and extends retention to "
                            "several hours upfront. Use for a run you know is "
                            "a long-lived delegation workstream."
                        ),
                    },
                    **_context_group_props,
                },
            },
        },
        {
            "name": "spawn_continue",
            "description": (
                "Dispatch a follow-up task into ANY completed subagent run's "
                "conversation — no flag needed at spawn time. The subagent "
                "resumes with its full accumulated context (no re-explaining). "
                "Continuing promotes the conversation: retention extends from "
                "~1h (default) to several hours; release with spawn_release "
                "when the workstream is done. Returns immediately; the result "
                "arrives as a normal [Subagent completion event]. Typed "
                "failures: conversation_busy (run in flight — use spawn_steer), "
                "conversation_gone (files expired — re-spawn with a summary), "
                "resume_failed (session could not be restored; never executes "
                "context-free). Context scope is inherited from the run being "
                "continued, so the include_* flags are not accepted here."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversation": {
                        "type": "string",
                        "description": (
                            "Conversation id — the run id of any completed subagent "
                            "run (keep=true only extends retention)"
                        ),
                    },
                    "task": {
                        "type": "string",
                        "description": "Follow-up task/instruction for the subagent",
                    },
                    "agent": {"type": "string", "description": "Agent name override"},
                    "max_turns": {
                        "type": "integer",
                        "description": "Override tool-call budget for this turn",
                    },
                    "model": {"type": "string", "description": "Model override"},
                },
                "required": ["conversation", "task"],
            },
        },
        {
            "name": "spawn_steer",
            "description": (
                "Inject a message into a RUNNING subagent's in-flight turn "
                "(course-correct without restarting it) — like steering a chat "
                "session. A steer arriving while a just-started run's session "
                "is still registering waits briefly for it (typed "
                "session_starting error if it still isn't up — retry then); "
                "runs still WAITING in the spawn queue return queued_not_started "
                "(409, 'queued — not started') until they start. Only works while the run is executing; for a "
                "finished continuable run use spawn_continue instead. "
                "mode='follow_up' queues the message instead of interrupting: "
                "it is delivered as a continuation on the run's conversation "
                "AFTER its current turn completes — use it when the correction "
                "can wait and interrupting critical work mid-execution would "
                "do more harm than good."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The running subagent's id (from spawn_run/spawn_list)",
                    },
                    "message": {
                        "type": "string",
                        "description": "Instruction to inject into the running turn",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["interrupt", "follow_up"],
                        "description": (
                            "interrupt (default): inject into the running turn "
                            "now. follow_up: wait for the current turn to "
                            "complete, then deliver as a continuation on the "
                            "run's conversation (its result arrives as a "
                            "separate completion event; multiple queued "
                            "follow-ups drain as one continuation)"
                        ),
                    },
                },
                "required": ["agent_id", "message"],
            },
        },
        {
            "name": "spawn_release",
            "description": (
                "End a continuable subagent conversation (spawn_run keep=true): "
                "deletes its persisted session so it can no longer be continued. "
                "Call when the delegated workstream is finished. Idle "
                "conversations also expire automatically after several hours."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversation": {
                        "type": "string",
                        "description": (
                            "Conversation id — the run id of any completed subagent "
                            "run (keep=true only extends retention)"
                        ),
                    },
                },
                "required": ["conversation"],
            },
        },
        {
            "name": "spawn_list",
            "description": (
                "List all running, queued (accepted, not yet started) and completed "
                "subagents (read-only, no commands executed)"
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "spawn_status",
            "description": (
                "Retrieve a subagent's live status and partial transcript while it runs, "
                "or its full retained transcript after completion. The completion event "
                "gives a summary plus the transcript path — use this tool (or the read/grep "
                "tools on the path) to read the rest instead of re-running the subagent. "
                "For large transcripts, page with offset/limit (line-based, like reading "
                "code) or filter with grep (regex) rather than pulling the whole thing into "
                "context. While a run is still going the partial transcript is a live view "
                "that grows (and past the manager's bound is truncated from the front), so "
                "line offsets can shift between polls and offset/limit paging is best-effort "
                "until completion. Terminal responses include elapsed time and credit "
                "usage when recorded."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Subagent ID from completion event",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "0-based start line for a paged read (default 0)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Max lines to return (1-2000). Omit for the full transcript; "
                            "use with offset to page through a large result."
                        ),
                    },
                    "grep": {
                        "type": "string",
                        "description": (
                            "Case-insensitive regex; return only transcript lines that "
                            "match (offset/limit then apply to the matches)."
                        ),
                    },
                },
                "required": ["agent_id"],
            },
        },
        {
            "name": "spawn_sub_agents",
            "description": (
                "Spawn one or more sub-agents to run tasks in parallel. Each sub-agent "
                "gets its own session with full tool access. BLOCKS until all sub-agents "
                "complete, then returns their collected results; a sub-agent the spawn "
                "gate deferred is reported with why it waits, and its result arrives "
                "later as a completion event. Use for delegating "
                "independent subtasks to specialist agents. Preferred over spawn_run when "
                "you need results before continuing." + _cap_hint
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agents": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent_or_mode": {
                                    "type": "string",
                                    "description": "Agent name for the sub-agent. An "
                                    "unknown name is refused, not defaulted." + _agent_hint,
                                },
                                "prompt": {
                                    "type": "string",
                                    "description": "Task/prompt for the sub-agent",
                                },
                            },
                            "required": ["prompt"],
                        },
                        "description": "Array of sub-agents to spawn in parallel",
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Optional absolute path to launch sub-agents in. "
                            "Must be under a configured subagent_cwd_allowed_roots entry."
                        ),
                    },
                    **_context_group_props,
                },
                "required": ["agents"],
            },
        },
        {
            "name": "resource_status",
            "description": (
                "Check current host resource headroom BEFORE starting a heavy "
                "step — a full test suite, a large build, or a big parallel "
                "sub-agent wave. Returns available memory, CPU load, and an "
                "advisory posture (ample / tight / critical) plus the sub-agent "
                "cap ACTUALLY in force right now against your configured max "
                "(and why it is lower, when it is), so you can decide whether to "
                "run the "
                "heavy path now, switch to a lighter path (targeted tests, fewer "
                "sub-agents, deferred build), or wait for memory to free. "
                "Read-only and advisory — it does NOT reserve or enforce "
                "anything, and headroom can change between the check and your "
                "action, so treat it as guidance, not a guarantee."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _is_unknown_agent_refusal(resp: Mapping[str, Any], agent: str) -> bool:
    """True when *resp* is the gateway refusing *agent* as a name this wave cannot use.

    Reads the response's machine-readable ``code`` (``AGENT_NOT_FOUND_CODE`` for a
    name it cannot load, ``AGENT_NOT_AVAILABLE_CODE`` for one the parent agent's
    spec forbids, ``AGENT_INTERNAL_CODE`` for one of Kiro Crew's own generated
    specs -- each spelled once in ``subagent`` and imported here), not its prose. The
    refusal text is advisory and free to be reworded; before this it WAS the
    contract, so any rewording silently disabled the wave short-circuit until a
    test caught it.

    The response answers the POST that named *agent*, so no name re-check is
    needed -- pairing is what the old text match had to reconstruct from the
    message. ``agent`` is still required, because an unnamed request means "use
    the default" and can never produce this refusal.

    Fail-soft by construction: a miss reproduces today's behavior (every member is
    dispatched and refused individually), never a refusal of a name the gateway
    would have accepted. That asymmetry is what makes a missing code safe -- a
    client newer than the gateway simply loses the short-circuit -- while using it
    to REJECT a spawn would not be.
    """
    return bool(agent) and resp.get("code") in (
        AGENT_NOT_FOUND_CODE,
        AGENT_NOT_AVAILABLE_CODE,
        AGENT_INTERNAL_CODE,
    )


def _collapse_effort_verdicts(
    pairs: list[tuple[str, tuple[str, str]]],
) -> list[tuple[str, tuple[str, str]]]:
    """Group (subagent id, verdict text) pairs into (id list, verdict text) rows.

    ``reasoning_effort`` and ``model`` are batch-wide, so a wide fan-out
    usually yields the IDENTICAL verdict for every member — rendering it once
    per subagent injects N copies of the same line into the calling agent's
    context. Collapse each group of 2+ ids sharing a verdict into one
    row naming all of them ("a1, a2, a3"); a verdict unique to one subagent
    keeps its own row, so mixed batches keep full per-id attribution. Groups
    preserve first-seen dispatch order, and ids keep their dispatch order
    within a group, so the collapsed output remains deterministic.
    """
    grouped: dict[tuple[str, str], list[str]] = {}
    for sid, text in pairs:
        grouped.setdefault(text, []).append(sid)
    return [(", ".join(ids), text) for text, ids in grouped.items()]


class _SpawnRunStep(DeferredTool):
    """Submit one task per pool step, then return the background-run receipt."""

    def __init__(
        self,
        *,
        task_count: int,
        agent_ids: list[str],
        transport_errors: list[str],
        submit_one: Callable[[int], None],
        receipt: Callable[[], str],
        reconcile_lost: Callable[[str], bool],
    ) -> None:
        self.task_count = task_count
        self.agent_ids = agent_ids
        self.transport_errors = transport_errors
        self._submit_one = submit_one
        self._receipt = receipt
        self._reconcile_lost = reconcile_lost
        self._submit_index = 0
        self._unsubmitted_closed = False
        self.phase = "submit"
        self.next_due = mcp_core.time.monotonic()

    def _close_unsubmitted(self) -> None:
        """Reconcile every task this call never posted, exactly once.

        Each member index ends in one bucket: accepted, refused (reconciled by
        the submit itself), transport-unknown (left to the stuck-wave sweep), or
        unsubmitted. Every sibling's ``batch_total`` counts the unsubmitted
        ones, so the wave digest holds the accepted children's results until
        each of them is reported lost here. Before the first POST the gateway
        has no wave for this batch, so there is nothing to reconcile and a
        report would only announce failures for a call that spawned nothing.
        """
        if self._unsubmitted_closed or self._submit_index == 0:
            return
        self._unsubmitted_closed = True
        reason = "not submitted: the spawn_run call ended before this task was posted"
        for _ in range(self._submit_index, self.task_count):
            if not self._reconcile_lost(reason):
                # The gateway is not answering: every later report would wait
                # out the same timeout on the loop's thread, and the stuck-wave
                # sweep closes the wave anyway. One bounded attempt is the cost.
                break

    def due_at(self) -> float:
        return self.next_due

    def step(self) -> str | None:
        if self._submit_index < self.task_count:
            self._submit_one(self._submit_index)
            self._submit_index += 1
        self.next_due = mcp_core.time.monotonic()
        if self._submit_index < self.task_count:
            return None
        self.phase = "done"
        return self._receipt()

    def abandon(self) -> str | None:
        remaining = self.task_count - self._submit_index
        self._close_unsubmitted()
        if not self.agent_ids and not self.transport_errors:
            return None
        self.phase = "abandoned"
        return self._receipt() + (
            "\n\nAny accepted subagents were NOT cancelled and keep running "
            "(queued members start once their condition clears). Do not spawn them again. "
            f"{remaining} task(s) were NOT submitted; only those unsubmitted tasks may be "
            "submitted again as a new call. They are reported lost to the wave, so its "
            "digest does not wait on them."
        )

    def cancel(self) -> None:
        # Accepted children keep running and deliver their completion events;
        # the tasks never posted are reported lost so their wave can close.
        self._close_unsubmitted()


def spawn_run(name: str, args: dict[str, Any]) -> str | DeferredTool:
    """Prepare a background batch; its deferred steps submit one task at a time."""
    args = validate_tool_args(args, SPAWN_RUN_SCHEMA)

    tasks = args.get("tasks")
    task = args.get("task")

    # Support both single task and batch tasks. A ``tasks`` entry is a prompt
    # string or an object carrying per-task overrides; both
    # normalise to (prompt, overrides) here so the dispatch loop has one shape.
    task_overrides: list[dict[str, str]] = []
    if tasks and isinstance(tasks, list):
        task_list = []
        for t in tasks:
            if isinstance(t, dict):
                prompt = t.get("task") or ""
                if not prompt.strip():
                    continue
                task_list.append(prompt)
                task_overrides.append({k: v for k, v in t.items() if k != "task" and v})
            elif isinstance(t, str) and t.strip():
                task_list.append(t)
                task_overrides.append({})
    elif task:
        task_list = [task]
        task_overrides = [{}]
    else:
        return "Error: task or tasks is required"

    # Read parent session key so completions inject back into this session.
    parent_session = mcp_core._resolve_session_key()
    if len(task_list) == 1:
        # One-line allowed-only audit: the task count is already known here,
        # and this is the only place a one-task spawn_run call is visible at
        # all now that the solo gate is gone. Kept so a one-task rate can
        # still be measured/re-audited without reviving the removed gate.
        mcp_core.sel().log_api_access(
            caller="internal",
            operation="spawn.solo",
            outcome="allowed",
            source="spawn_run",
            resources=parent_session or "",
        )

    # Fire-and-forget — gateway's SubagentManager queues excess tasks
    # and auto-spawns them as slots free up.
    agent = args.get("agent") or ""
    # The crew this run is DELEGATED to, distinct from `agent`: `agent` names a
    # kiro-cli template, `crew` names a crew member and is what gives the child
    # that crew's memory silo. Read here and forwarded below; the schema accepting
    # the field is not enough, and a field the handler drops makes every documented
    # `spawn_run(crew=...)` a silent no-op that runs on the operator's own memory.
    crew = args.get("crew") or ""
    target_member = args.get("target_member") or ""
    agents_list = args.get("agents") or []
    max_turns = args.get("max_turns") or 0
    cwd = args.get("cwd") or ""
    model = args.get("model") or ""
    reasoning_effort = args.get("reasoning_effort") or ""
    keep = bool(args.get("keep"))
    # Context scope: absent ⇒ true, so a parent that passes nothing gets the
    # same context a normal session would.
    inc_memory = args.get("include_memory", True) is not False
    inc_lessons = args.get("include_lessons", True) is not False
    inc_project = args.get("include_project", True) is not False
    if agents_list and len(agents_list) != len(task_list):
        return (
            f"Error: agents length ({len(agents_list)}) must match tasks length ({len(task_list)})"
        )
    agent_ids: list[str] = []
    can_work = True
    agent_names: list[str] = []
    # (subagent id, reason) pairs from the server's effort verdict — the
    # gateway resolves the effective model (per-call value, else role pin,
    # else unpinned) and reports when the requested effort cannot apply.
    effort_drops: list[tuple[str, tuple[str, str]]] = []
    # (subagent id, note) pairs for the delivery mirror: the resolved model and
    # the family settings key a requested effort is delivered under.
    effort_applies: list[tuple[str, tuple[str, str]]] = []
    agent_tasks: list[str] = []
    # subagent id -> the gate's reason, for members the gateway accepted but
    # answered ``status: "queued"`` (deferred, not started).
    queued_reasons: dict[str, str] = {}
    errors: list[str] = []
    transport_errors: list[str] = []
    # Forward this session's own approval_mode (set as an env var at
    # process spawn -- see gateway.py cron dispatch, mirroring
    # KIROCREW_SESSION_KEY/KIROCREW_CHANNEL_ID) so a cron running with
    # approval_mode="auto" deterministically auto-approves its own
    # spawn_run subagent launches. Without this, SubagentManager.spawn's
    # only route to auto-approve is its own parent_trusted lookup, which
    # requires parent_session to resolve back to the cron's session key
    # -- an identity-plumbing path that can fail silently and leave the
    # spawn stuck on the interactive approval path a cron has no
    # responder for.
    approval_mode = os.environ.get("KIROCREW_APPROVAL_MODE", "")
    # Batch/wave identity: one id per multi-task spawn_run call so the
    # gateway can digest completions (one injection turn per wave instead
    # of N) and emit batch lifecycle events at 60-100-agent scale.
    batch_id = uuid.uuid4().hex[:12] if len(task_list) > 1 else ""

    def _reconcile_lost(reason: str) -> bool:
        """Tell the gateway this member never reached ``mgr.spawn``.

        Every sibling's ``batch_total`` counts it, so an un-reconciled member
        leaves the wave at submitted < expected forever: the digest never closes
        and held sibling results strand until restart. Returns whether the
        report was delivered; the POST is bounded by ``_LOST_REPORT_TIMEOUT_SECS``
        because ``_close_unsubmitted`` runs it from the dispatch loop's thread.
        """
        if not batch_id:
            return True
        try:
            reply = mcp_core._post(
                "/api/spawn/lost",
                {
                    "batch_id": batch_id,
                    "batch_total": len(task_list),
                    "reason": reason[:300],
                    "parent_session": parent_session,
                },
                timeout=_LOST_REPORT_TIMEOUT_SECS,
            )
        except Exception:
            return False  # reaper backstop covers delivery failure
        # ``_post`` answers a transport or HTTP failure as an error dict rather
        # than raising, so the reply decides whether the report landed.
        return not (isinstance(reply, dict) and reply.get("error"))

    # Agent names this wave already learned the gateway refuses as unknown.
    # Re-posting one cannot succeed: the refusal is a property of the NAME, not
    # of the task, so the rest of a wave that shares it is dead on arrival. The
    # observed cost of not knowing that was a whole wave of doomed dispatches on
    # one invented name.
    refused_agents: dict[str, str] = {}

    def _submit_one(i: int) -> None:
        nonlocal can_work
        t = task_list[i]
        over = task_overrides[i]
        a = agents_list[i] if agents_list else agent
        t_model = over.get("model") or model
        t_effort = over.get("reasoning_effort") or reasoning_effort
        if a in refused_agents:
            # Short line on purpose: the full roster is already on the first
            # refusal above, and repeating it once per remaining member would
            # bury it.
            errors.append(
                f"{t[:60]}: not dispatched - agent {a!r} refused above"[:_SA_ERROR_MAX_CHARS]
            )
            _reconcile_lost(refused_agents[a])
            return
        body: dict[str, Any] = {"task": t, "agent": a, "parent_session": parent_session}
        if crew:
            body["crew"] = crew
        if target_member:
            body["target_member"] = target_member
        if batch_id:
            body["batch_id"] = batch_id
            body["batch_total"] = len(task_list)
        if max_turns:
            body["max_turns"] = max_turns
        if cwd:
            body["cwd"] = cwd
        if t_model:
            body["model"] = t_model
        if t_effort:
            body["reasoning_effort"] = t_effort
        if keep:
            body["keep"] = True
        if not inc_memory:
            body["include_memory"] = False
        if not inc_lessons:
            body["include_lessons"] = False
        if not inc_project:
            body["include_project"] = False
        if approval_mode:
            body["approval_mode"] = approval_mode
        d = mcp_core._post("/api/spawn", body)
        can_work = can_work and d.get("parent_work_supported") is True
        if d.get("error"):
            error_line = f"{t[:60]}: {d['error']}"[:_SA_ERROR_MAX_CHARS]
            if d.get("transport_error"):
                # The gateway may have accepted the spawn before the
                # response failed. Treat it as unknown, not rejected, and
                # do not reconcile it as lost (which could close a batch
                # early while the accepted member is still running).
                transport_errors.append(error_line)
                return
            errors.append(error_line)
            if a and _is_unknown_agent_refusal(d, a):
                refused_agents[a] = str(d["error"])
            # Wave-liveness reconcile: every sibling's batch_total counts
            # THIS member,
            # but an explicit pre-spawn rejection never reached mgr.spawn
            # unless the response says "counted". Un-reconciled, the
            # wave's submitted < expected forever — the digest never
            # closes and held sibling results strand until restart.
            # Transport failures are deliberately excluded because their
            # acceptance status is unknown; the stuck-wave reaper is the
            # safe backstop when such a submission was truly lost.
            if not d.get("counted"):
                _reconcile_lost(str(d.get("error", "")))
            return
        agent_ids.append(d.get("id", "?"))
        agent_names.append(a)
        agent_tasks.append(t)
        if d.get("status") == "queued":
            # Accepted but DEFERRED by the gate (memory floor, critical posture,
            # adaptive cap at 0): keyed and counted like a started member, but
            # not running, and re-checked only every admit wait. Reported apart
            # below so the caller does not wait for a completion event as if
            # it had started. An older gateway sends no ``status``; a row
            # waiting merely for a slot or the stagger tick sends ``spawned``.
            queued_reasons[str(d.get("id", "?"))] = str(
                d.get("reason_detail") or d.get("reason") or "deferred by the spawn gate"
            )[:_SA_ERROR_MAX_CHARS]
        if d.get("effort_dropped"):
            effort_drops.append((str(d.get("id", "?")), (t_effort, str(d["effort_dropped"]))))
        if d.get("effort_applied"):
            effort_applies.append((str(d.get("id", "?")), (t_effort, str(d["effort_applied"]))))

    def _receipt() -> str:
        return _spawn_run_receipt(
            agent_ids=agent_ids,
            agent_names=agent_names,
            agent_tasks=agent_tasks,
            effort_drops=effort_drops,
            effort_applies=effort_applies,
            queued_reasons=queued_reasons,
            errors=errors,
            transport_errors=transport_errors,
            parent_session=parent_session,
            can_work=can_work,
            keep=keep,
        )

    return _SpawnRunStep(
        task_count=len(task_list),
        agent_ids=agent_ids,
        transport_errors=transport_errors,
        submit_one=_submit_one,
        receipt=_receipt,
        reconcile_lost=_reconcile_lost,
    )


def _spawn_run_receipt(
    *,
    agent_ids: list[str],
    agent_names: list[str],
    agent_tasks: list[str],
    effort_drops: list[tuple[str, tuple[str, str]]],
    effort_applies: list[tuple[str, tuple[str, str]]],
    queued_reasons: dict[str, str],
    errors: list[str],
    transport_errors: list[str],
    parent_session: str,
    can_work: bool,
    keep: bool,
) -> str:
    """Render the accumulated background-run receipt without gateway requests."""
    spawn_lines: list[str] = []
    # Server-computed effort verdicts (never a rejection — gated on agent_ids
    # so a total-failure result keeps its "Error:" first line, which SEL and
    # callers test as a prefix). The gateway resolves the effective model
    # (per-call value, else the subagent role pin, else unpinned/"auto") at
    # accept time, so — unlike the old client-side check — this also reports
    # the default case where no per-call model was passed and the effort
    # would otherwise be dropped silently.
    if agent_ids:
        # A verdict is keyed by (effort, text): a tasks[] object may override
        # the batch-wide level, so each line names the level its tasks asked for.
        for drop_ids, (lvl, drop_reason) in _collapse_effort_verdicts(effort_drops):
            spawn_lines.append(f"ℹ reasoning_effort='{lvl}' dropped for {drop_ids}: {drop_reason}")
        for applied_ids, (lvl, applied_note) in _collapse_effort_verdicts(effort_applies):
            spawn_lines.append(
                f"✓ reasoning_effort='{lvl}' applied for {applied_ids} ({applied_note})"
            )
    if not parent_session and agent_ids:
        # Orphan alert: without a parent session key the subagents cannot
        # deliver completion events back to this conversation and will
        # not appear in the Subagents panel for this session. This
        # fails silently — say it
        # loudly so the agent/user can fall back to spawn_list +
        # result.txt polling instead of waiting forever.
        spawn_lines.append(
            "⚠ parent_session UNRESOLVED — these subagents are orphaned: "
            "completion events will NOT arrive in this conversation. "
            "Poll spawn_list and read ~/.kiro/crew/subagents/<id>/result.txt "
            "instead. (Identity plumbing issue — check KIROCREW_HOST_PID / "
            "session_pid / claim-push.)"
        )
    if agent_ids:
        members = list(zip(agent_ids, agent_names, agent_tasks))
        started = [m for m in members if m[0] not in queued_reasons]
        deferred = [m for m in members if m[0] in queued_reasons]
        if started:
            if parent_session:
                spawn_lines.append(
                    f"Spawned {len(started)} subagent(s). Results will arrive as completion events:"
                )
            else:
                # Orphaned (warning above): completion events cannot be
                # delivered — do not promise them in the same breath.
                spawn_lines.append(
                    f"Spawned {len(started)} subagent(s). Monitor results via polling:"
                )
            for aid, a, t in started:
                label = f"{aid} ({a})" if a else aid
                spawn_lines.append(f"  {label}: {t[:80]}")
        if deferred:
            # One gate verdict covers the wave (memory is host-wide), so the
            # first member's reason heads the block; a member whose reason
            # differs is annotated on its own line. The header keeps the
            # ``N subagent(s).`` shape of the Spawned line on purpose: the
            # dashboard's inline run card recognises a launch by that marker
            # and reads the ``  <id> (<agent>): <task>`` lines that follow, so
            # a queued-only wave still gets its card (which is what shows the
            # queued count and, with the event's reason, why it waits).
            head_reason = queued_reasons[deferred[0][0]]
            spawn_lines.append(
                f"Queued {len(deferred)} subagent(s). Not started yet: {head_reason}. "
                "The gateway re-checks them and starts each one once the condition "
                "clears; only then does its result arrive:"
            )
            for aid, a, t in deferred:
                label = f"{aid} ({a})" if a else aid
                note = "" if queued_reasons[aid] == head_reason else f" [{queued_reasons[aid]}]"
                spawn_lines.append(f"  {label}: {t[:80]}{note}")
        if keep:
            spawn_lines.append(
                "These conversations have GUARANTEED continuability: after "
                "completion, use spawn_continue(conversation=<id>, task=...) "
                "for follow-up work with full context, and "
                "spawn_release(conversation=<id>) when the workstream is done."
            )
    if errors:
        if agent_ids:
            spawn_lines.append(f"\n❌ {len(errors)} task(s) failed to start:")
        elif transport_errors:
            # No confirmed starts: retain the Error prefix used by SEL and
            # callers even though other submissions remain uncertain.
            spawn_lines.append(f"Error: {len(errors)} task(s) failed to start:")
        else:
            spawn_lines.append(
                f"Error: {len(errors)} task(s) failed to start; "
                "none of the requested subagents were started:"
            )
        for e in errors:
            spawn_lines.append(f"  - {e}")
    if transport_errors:
        if agent_ids or errors:
            spawn_lines.append(
                f"\n⚠ {len(transport_errors)} task(s) have unknown acceptance status:"
            )
        else:
            spawn_lines.append(
                f"Error: acceptance status is unknown for " f"{len(transport_errors)} task(s):"
            )
        for e in transport_errors:
            spawn_lines.append(f"  - {e}")
        guidance = (
            "The gateway may have accepted these tasks before the response failed. "
            "Do not retry automatically. Check spawn_list"
        )
        if parent_session:
            guidance += " and wait for completion events"
        guidance += (
            ". spawn_list lists queued spawns too, but a submission still in flight "
            "may not be listed yet; wait and recheck before retrying to avoid "
            "duplicate work."
        )
        spawn_lines.append(guidance)
    if agent_ids:
        if parent_session:
            if can_work:
                spawn_lines.append(
                    "\nAdvance only your ready, non-overlapping parent work for a short bounded "
                    "step (at most one minute), then END YOUR TURN so queued completion events "
                    "can be delivered. If no such work remains, END YOUR TURN now. "
                    "Do not poll, duplicate child work, or claim the task is complete."
                )
            else:
                spawn_lines.append(
                    "\nEND YOUR TURN now: this caller has no confirmed parent-work delivery "
                    "boundary. Wait for the [Subagent completion event] messages. "
                    "Dispatch is not completion."
                )
        else:
            spawn_lines.append(
                "\nDo NOT wait for completion events — poll spawn_list and read "
                "result.txt files instead."
            )
    elif not errors and not transport_errors:
        # Defensive fallback: every non-empty task list should produce an
        # id or an error, but never imply work was accepted if neither did.
        spawn_lines.append("Error: no subagents were started.")
    return "\n".join(spawn_lines)


def spawn_continue(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SPAWN_CONTINUE_SCHEMA)
    conv = (args.get("conversation") or "").strip()
    task = (args.get("task") or "").strip()
    if not conv or not task:
        return "Error: conversation and task are required"
    parent_session = mcp_core._resolve_session_key()
    body = {"task": task, "parent_session": parent_session}
    if args.get("agent"):
        body["agent"] = args["agent"]
    if args.get("model"):
        body["model"] = args["model"]
    if args.get("max_turns"):
        body["max_turns"] = args["max_turns"]
    d = mcp_core._post(f"/api/spawn/{conv}/continue", body)
    if d.get("error"):
        return f"Error: {d['error']}"
    return (
        f"Continued conversation {conv} as run {d.get('id', '?')}. "
        "The result will arrive as a [Subagent completion event] — "
        "END YOUR TURN and wait for it."
    )


def spawn_steer(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SPAWN_STEER_SCHEMA)
    agent_id = (args.get("agent_id") or "").strip()
    message = (args.get("message") or "").strip()
    mode = (args.get("mode") or "interrupt").strip()
    if not agent_id or not message:
        return "Error: agent_id and message are required"
    d = mcp_core._post(f"/api/spawn/{agent_id}/steer", {"message": message, "mode": mode})
    if d.get("error"):
        return f"Error: {d['error']}"
    if mode == "follow_up":
        return (
            f"Queued follow-up for run {agent_id}: it will be delivered as "
            "a continuation on the run's conversation after its current "
            "turn completes. The continuation's result arrives as a "
            "separate [Subagent completion event] — after this run's own."
        )
    return (
        f"Steered run {agent_id}: the message was injected into its "
        "running turn. Its completion event will reflect the correction."
    )


def spawn_release(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SPAWN_RELEASE_SCHEMA)
    conv = (args.get("conversation") or "").strip()
    if not conv:
        return "Error: conversation is required"
    d = mcp_core._post(f"/api/spawn/{conv}/release", {})
    if d.get("error"):
        return f"Error: {d['error']}"
    return f"Released conversation {conv} — it can no longer be continued."


def _held_by_a_deferral(record: Mapping[str, Any]) -> bool:
    """A queued, not-started member the gate DEFERRED (not one waiting for a slot).

    Read off the row's OWN deferral sentence (``reason_detail``), which the
    gateway sends only while that deferral is in force. ``reason`` is the
    parent's wait label, last writer wins, so a sibling's deferral would
    relabel a member that only waits behind the cap.
    """
    return (
        record.get("queued") is True
        and record.get("resuming") is not True
        and bool(str(record.get("reason_detail") or "").strip())
    )


def _queued_why(record: Mapping[str, Any]) -> str:
    """Why a queued spawn waits, redacted (:func:`queued_wait_text`)."""
    return redact(queued_wait_text(record))


def spawn_list(name: str, args: dict[str, Any]) -> str:
    d = mcp_core._get("/api/spawn?queued=1")
    agents = d.get("agents", [])
    # Accepted spawns with no run yet (gate-deferred, or waiting for a slot);
    # the gateway lists them apart from the runs.
    queued = d.get("queued") or []
    if not isinstance(queued, list):
        queued = []

    def _redact(text: str) -> str:
        return redact(text)

    lines: list[str] = []
    for q in queued:
        if isinstance(q, Mapping) and q.get("id"):
            tag = "resuming" if q.get("resuming") is True else "not started"
            lines.append(
                f"{q['id']}  [queued] ({tag}: {_queued_why(q)})  "
                f"{_redact(str(q.get('task') or ''))[:60]}"
            )
    if d.get("queued_truncated") is True:
        # The gateway listed only the oldest queued spawns, or could not read
        # them all; the rest exist too.
        lines.append(
            "(the queued list is partial: more spawns may be queued than listed here; "
            "they are accepted and start on their own -- do not spawn them again)"
        )
    if not agents and not lines:
        lines.append("No subagents running.")
    else:
        for a in agents:
            # A run parked on an unanswered spawn-approval prompt has launched
            # no process and produced no turn, so reporting it as "running" is
            # actively misleading to a caller this module itself points here
            # ("Check spawn_list", above) -- it reads as work in progress when
            # the truth is that a human has not approved it yet.
            if a.get("done"):
                status = "done"
            elif a.get("awaiting_approval"):
                status = "awaiting-approval"
            else:
                status = "running"
            err = f" error: {_redact(a['error'])}" if a.get("error") else ""
            progress = ""
            if not a.get("done"):
                turns = a.get("turns", 0)
                tool = _redact(a.get("last_tool", ""))
                elapsed = a.get("elapsed", 0)
                parts = [f"{elapsed}s"]
                if turns:
                    parts.append(f"{turns} turns")
                if tool:
                    parts.append(tool)
                progress = f" ({', '.join(parts)})"
            _withheld = a.get("context_withheld") or []
            scope = f"  ctx-withheld: {','.join(_withheld)}" if _withheld else ""
            lines.append(f"{a['id']}  [{status}]{err}{progress}{scope}  {_redact(a['task'])[:60]}")
    # Always append available agents (fresh read from disk). Same grammar filter and
    # redaction as the two rosters above, via the shared helper: this output is a
    # tool RESULT, so it lands in the same model context, and a spec's ``name``
    # field arrives unvalidated.
    #
    # Unbounded and ``exclude=()`` on purpose, and this is the one surface where
    # that is right: both bounded rosters cap themselves and point the caller HERE
    # ("call spawn_list", "which lists them all"), so withholding names from this
    # listing would falsify what they promise. The reserved pair is not suggested
    # elsewhere because it is reached by omitting ``agent`` -- but it is still a
    # name the gateway accepts, so a full listing shows it. Kiro Crew's own
    # generated specs are NOT: the gateway refuses them, so even the full listing
    # leaves them out (``is_internal_agent_spec``).
    try:
        names, restricted = _parent_allowlist_filter(
            a.name or "" for a in mcp_core.list_agents() if not is_internal_agent_spec(a)
        )
        names, _ = visible_agent_names(names, exclude=())
        if names:
            lines.append(f"\nAvailable agents: {', '.join(names)}")
        if restricted:
            lines.append(
                "(restricted to this agent's toolsSettings.subagent.availableAgents; "
                + ("other installed agents are" if names else "every installed agent is")
                + " refused at spawn)"
            )
    except Exception:
        pass  # list_agents failure is non-critical
    return "\n".join(lines)


def spawn_status(name: str, args: dict[str, Any]) -> str:
    agent_id = args.get("agent_id", "")
    if not agent_id or not agent_id.isalnum():
        return "Error: invalid agent_id"
    # Optional paged / filtered read of the retained transcript.
    spawn_params: dict[str, str] = {}
    offset = args.get("offset")
    limit = args.get("limit")
    grep = args.get("grep")
    if isinstance(offset, int) and offset > 0:
        spawn_params["offset"] = str(offset)
    if isinstance(limit, int) and limit > 0:
        spawn_params["limit"] = str(limit)
    if isinstance(grep, str) and grep.strip():
        spawn_params["grep"] = grep
    path = f"/api/spawn/{agent_id}"
    if spawn_params:
        path += "?" + urlencode(spawn_params)
    d = mcp_core._get(path)
    usage = format_subagent_usage(d.get("credits"), d.get("elapsed"))
    # ``error`` means two different things here. On a payload that is not a
    # finished run (a failed lookup or a transport error, such as
    # ``{"error": "HTTP 404"}``) it is the whole answer. On a finished run
    # (``done`` is True) it is the reason the run ended badly, sent next to the
    # transcript the run retained; a timed-out or reaped run keeps its partial
    # output on purpose. That transcript must stay readable, so the reason
    # becomes a header instead of replacing the output.
    ended_with_error = d.get("done") is True and bool(d.get("error"))
    if d.get("error") and not ended_with_error:
        error = f"Error: {d['error']}"
        return f"[usage: {usage}]\n{error}" if usage else error

    meta = d.get("result_meta")
    if isinstance(meta, dict) and meta.get("grep_error"):
        return f"Error: {meta['grep_error']}"

    running = d.get("done") is False
    # Present-only, and api_spawn_status sets it ONLY while the run is parked on
    # the SPAWN-approval gate (never entered execution): no process, no turn.
    # spawn_list renders that "awaiting-approval" and the CLI waiter says
    # "approve it ... to start this run", so this tool must not report work
    # under way for it either.
    awaiting = running and d.get("awaiting_approval") is True
    # Accepted but not started (the gate deferred it, or it waits for a slot).
    # It is real accepted work: re-spawning it would run it twice.
    queued = running and d.get("queued") is True
    result = d.get("result") or ""
    if queued and d.get("resuming") is True:
        # It ran before and waits to go on: never "not started".
        result = (
            f"(queued — {_queued_why(d)}. It continues on its own and its completion "
            "event arrives as usual; do not spawn it again)"
        )
    elif queued:
        result = (
            f"(not started — queued: {_queued_why(d)}. It starts on its own once that "
            "clears and its completion event arrives as usual; do not spawn it again)"
        )
    elif running and not result:
        turns = d.get("turns", 0)
        if awaiting:
            result = (
                "(not started — waiting for spawn approval; approve it in the "
                "dashboard (Approvals) to start this run)"
            )
        elif isinstance(meta, dict) and meta.get("total_lines", 0) > 0:
            result = (
                f"(no partial transcript lines in this view — {turns} turns so far; "
                "adjust offset/grep to inspect the running transcript)"
            )
        else:
            result = (
                f"(no streamed text yet — {turns} turns so far; "
                "transcript arrives with the completion event)"
            )
    elif not result:
        result = "_No result._"
    result, _ = redact_exfiltration_urls(result)
    result, _ = redact_credentials(result)

    if isinstance(meta, dict) and meta:
        # Paged/grepped read — prepend a compact header so the LLM knows how
        # much it saw and how to continue, without re-reading the whole file.
        hdr: list[str] = []
        if usage:
            hdr.append(f"usage: {usage}")
        total = meta.get("total_lines", "?")
        if "matched_lines" in meta:
            hdr.append(f"{meta['matched_lines']} line(s) matched grep of {total} total")
        start = meta.get("offset", 0)
        returned = meta.get("returned_lines", 0)
        hdr.append(f"showing lines {start}-{start + returned} of {total}")
        if meta.get("has_more"):
            hdr.append(f"more available — call again with offset={start + returned}")
        result = f"[{' | '.join(hdr)}]\n{result}"

    if running:
        if queued:
            state = "QUEUED"
        elif awaiting:
            state = "AWAITING-APPROVAL"
        else:
            state = "RUNNING"
        status = [state]
        if "elapsed" in d:
            status.append(f"{d['elapsed']}s")
        if "turns" in d:
            status.append(f"{d['turns']} turns")
        if d.get("last_tool"):
            status.append(f"last tool: {d['last_tool']}")
        header = f"[{' · '.join(status)}]"
        header, _ = redact_exfiltration_urls(header)
        header, _ = redact_credentials(header)
        return f"{header}\n{result}"
    if isinstance(meta, dict) and meta:
        out = result
    else:
        out = f"[usage: {usage}]\n{result}" if usage else result
    if ended_with_error:
        reason, _ = redact_exfiltration_urls(str(d["error"]))
        reason, _ = redact_credentials(reason)
        out = f"[ENDED WITH ERROR · {reason}]\n{out}"
    return out


#: Server-side hold per resume-poll request (seconds); under the client GET
#: timeout so a held request never reads as a hang.
RESUME_HOLD_SECS = 8.0


class _ResumeHold:
    """Wait, one probe at a time, for the subagent parent's slot to be granted back.

    Only a ``subagent:<id>`` parent has a lane slot to wait for; a chat-turn
    parent is done at once. The gateway answers ``known=False`` for a run it
    does not hold (finished, other incarnation), which also releases the hold.
    Split into probes so the deferred ``spawn_sub_agents`` can take one per
    step: each probe is one server-held request of at most ``RESUME_HOLD_SECS``,
    never the whole wait. The deadline passing with the slot still not granted
    yields a note for the tool result.
    """

    def __init__(self, parent_session: str, deadline: float) -> None:
        self.parent_id = ""
        if parent_session.startswith("subagent:"):
            self.parent_id = parent_session[len("subagent:") :]
        self.deadline = deadline
        self.held = False

    def probe(self) -> tuple[bool, dict[str, Any] | None]:
        """``(done, note)``: ``done`` False means ask again; ``note`` is the
        result note when the deadline passed with the slot still not granted."""
        if not self.parent_id:
            return True, None
        remaining = self.deadline - mcp_core.time.monotonic()
        if remaining <= 0:
            if not self.held:
                # The deadline was already spent on the children (still_running
                # is reported for them); nothing about the slot was observed.
                return True, None
            return True, {
                "status": "resume_pending",
                "parent": self.parent_id,
                "note": (
                    "The children finished but this run's execution slot was not granted "
                    "back before the wait deadline; it re-enters through admission by "
                    "capacity."
                ),
            }
        hold = max(0.0, min(RESUME_HOLD_SECS, remaining))
        try:
            st = mcp_core._get(f"/api/spawn/{self.parent_id}/resume?wait_secs={hold:.1f}")
        except Exception:
            return True, None  # a gateway that cannot answer is not a reason to hold
        if not isinstance(st, dict) or st.get("error"):
            return True, None
        if st.get("known") is not True or st.get("granted") is not False:
            return True, None
        # ``granted`` False with the hold consumed: the pump has not reached
        # this parent yet; ask again (the request itself was the wait).
        self.held = True
        return False, None


def spawn_sub_agents(name: str, args: dict[str, Any]) -> "str | DeferredTool":
    """Spawn the agents, then wait for them without holding the MCP worker.

    Submission, polling the children, the parent's slot grant and collection
    are :class:`_SubAgentsStep` phases the dispatch loop times. Each submission
    posts one member, so a large batch never holds the shared worker for its
    cumulative submission time (see ``mcp_shared.DeferredTool``).
    """
    args = validate_tool_args(args, SPAWN_SUB_AGENTS_SCHEMA)
    agents_input = args.get("agents")
    if not agents_input or not isinstance(agents_input, list):
        return "Error: 'agents' array is required"
    cwd = args.get("cwd") or ""
    # Context scope: batch-wide, absent ⇒ true (same rule as spawn_run).
    sa_groups = {
        k: False
        for k in ("include_memory", "include_lessons", "include_project")
        if args.get(k, True) is False
    }
    parent_session = mcp_core._resolve_session_key()

    def _redact_sa(text: str) -> str:
        return redact(text)

    def _inline_result(aid: str, raw: str) -> str:
        """A finished child's retained output as this reply carries it.

        Redacted, and past ``COMPLETION_KEEP_DEFAULT_CHARS`` replaced by the same
        summary plus disk path that ``spawn_run`` gives, so a large transcript
        does not fill the caller's context window. One rule for a child that
        completed and one that ended in error.
        """
        result_text = _redact_sa(raw)
        if len(result_text) > COMPLETION_KEEP_DEFAULT_CHARS:
            try:
                result_path = str(agent_dir_for_display(aid) / "result.txt")
            except (ValueError, OSError):
                result_path = ""
            if result_path:
                result_text = mcp_core.summarize_result(result_text, result_path)
        return result_text

    # Validate individual agent entries (schema guarantees dict entries)
    for entry in agents_input:
        p = entry.get("prompt", "")
        if len(p) > MAX_MEDIUM_STRING:
            entry["prompt"] = p[:MAX_MEDIUM_STRING]
        a = entry.get("agent_or_mode", "")
        if len(a) > MAX_SHORT_STRING:
            entry["agent_or_mode"] = a[:MAX_SHORT_STRING]

    # One-line allowed-only audit, mirroring spawn_run: a one-task call is only
    # visible here now that the solo gate is gone, so log it to keep the
    # one-task rate measurable without reviving the removed gate.
    if sum(1 for e in agents_input if e.get("prompt", "").strip()) == 1:
        mcp_core.sel().log_api_access(
            caller="internal",
            operation="spawn.solo",
            outcome="allowed",
            source="spawn_sub_agents",
            resources=parent_session or "",
        )

    mcp_core.sel().log_tool_invocation(
        session_key=_audit_owner(parent_session),
        source="mcp_core",
        tool_name="spawn_sub_agents",
        outcome="attempt",
        metadata={"agent_count": len(agents_input)},
    )

    # The parked step retains its members until each is submitted, so it keeps
    # only the two clamped fields it reads; any other key a caller sent is
    # dropped here, as the blocking tool ignored it.
    pending = [
        {"prompt": entry["prompt"], "agent_or_mode": entry.get("agent_or_mode") or ""}
        for entry in agents_input
        if entry.get("prompt", "").strip()
    ]

    try:
        max_wait = float(os.environ.get("KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT", "7200"))
    except (TypeError, ValueError):
        max_wait = 7200.0
    max_wait = max(60.0, min(7200.0, max_wait))  # clamp: 1 min .. 2 hours
    return _SubAgentsStep(
        sa_ids=[],
        sa_deferred=set(),
        sa_errors=[],
        parent_session=parent_session,
        max_wait=max_wait,
        inline_result=_inline_result,
        agents_input=pending,
        sa_groups=sa_groups,
        cwd=cwd,
    )


class _SubAgentsStep(DeferredTool):
    """Submit the batch, then poll, hold and collect without owning the worker.

    One submission step posts one member. Later steps poll the children, probe
    the parent's slot (``hold``), or build the final reply (``collect``).
    """

    #: Seconds between polls of the children.
    POLL_INTERVAL = 2.0
    #: Seconds between session keepalives while polling. Pinging
    #: ``/api/session-keepalive`` keeps the gateway's ``is_responsive()`` from
    #: flagging this session as stale and SIGTERMing the ACP subprocess
    #: mid-wait, which would abort the very sub-agents being waited on.
    KEEPALIVE_SECS = 60.0

    def __init__(
        self,
        *,
        sa_ids: list[str],
        sa_deferred: set[str],
        sa_errors: list[str],
        parent_session: str,
        max_wait: float,
        inline_result: Callable[[str, str], str],
        agents_input: list[dict[str, Any]] | None = None,
        sa_groups: dict[str, bool] | None = None,
        cwd: str = "",
    ) -> None:
        self.agents_input = agents_input or []
        self.sa_groups = sa_groups or {}
        self.cwd = cwd
        self._submit_index = 0
        self.sa_ids = sa_ids
        self.sa_deferred = sa_deferred
        self.sa_errors = sa_errors
        # Members whose POST failed in transport: the gateway may have accepted
        # them, so they are neither ids nor plain refusals, and a retry could
        # run them twice. Bounded like ``sa_errors``.
        self.sa_unknown: list[str] = []
        self.parent_session = parent_session
        self.max_wait = max_wait
        self._inline_result = inline_result
        now = mcp_core.time.monotonic()
        self.deadline = now + max_wait
        self.next_ping = now + self.KEEPALIVE_SECS  # first keepalive after 60s, not immediately
        self.next_due = now
        self.wait_settled: set[str] = set()
        self.phase = "submit" if agents_input is not None else "poll"
        self.hold = _ResumeHold(parent_session, self.deadline)
        self.resume_note: dict[str, Any] | None = None
        self._settled_ids: set[str] = set()
        # Per-child tallies the collect step computes for this call's own SEL
        # row (``outcome`` and ``metadata``), written at settle. ``None`` until a
        # collect ran: a batch the gateway refused outright settles from the
        # submit phase with an error text and gets no outcome row, as the
        # blocking tool returned before writing one.
        self._counts: dict[str, int] | None = None

    def due_at(self) -> float:
        return self.next_due

    def abandon(self) -> str | None:
        """This wait cannot be continued -- the serving process is exiting,
        the parked table is full, or the step pool could not start -- and the
        loop will never step it again. The children were already spawned and
        keep running, so "retry the call" would run them twice: answer with
        their ids instead, worded without naming a cause this step cannot
        see. Nothing is marked collected, so each child's completion event
        still arrives. With no child accepted yet AND no member whose POST
        failed in transport (acceptance unknown) -- turned away before its
        first member was submitted, or every member plainly refused -- nothing
        runs, so the whole call may safely be retried: ``None`` keeps the
        loop's retryable refusal. A member of unknown acceptance is reported
        as such, since resubmitting it could run it twice."""
        if not self.sa_ids and not self.sa_unknown:
            return None
        self.phase = "abandoned"
        sa_results = [
            json.dumps(
                {
                    "status": "still_running",
                    "task_ids": list(self.sa_ids),
                    "query": "spawn_status/spawn_list",
                    "note": (
                        "This wait could not be continued; these sub-agents were already "
                        "spawned, were NOT cancelled and keep running. Do not "
                        "spawn them again. Their [Subagent completion event] messages still "
                        "arrive; poll spawn_list or spawn_status for progress."
                    ),
                }
            )
        ]
        remaining = len(self.agents_input) - self._submit_index
        if remaining:
            sa_results.append(
                json.dumps(
                    {
                        "status": "not_submitted",
                        "count": remaining,
                        "note": (
                            f"{remaining} batch member(s) were NOT submitted or spawned. "
                            "Only these unsubmitted members may be submitted again."
                        ),
                    }
                )
            )
        if self.sa_unknown:
            sa_results.append(
                json.dumps(
                    {
                        "status": "unknown_acceptance",
                        "count": len(self.sa_unknown),
                        "members": self.sa_unknown,
                        "note": (
                            "These members' spawn requests failed in transport, so the "
                            "gateway may or may not have accepted them. Do not resubmit "
                            "them blindly; check spawn_list first."
                        ),
                    }
                )
            )
        if self.sa_errors:
            sa_results.append(json.dumps({"status": "spawn_errors", "errors": self.sa_errors}))
        return "\n\n".join(sa_results)

    def _submit_one(self) -> str | None:
        if self._submit_index < len(self.agents_input):
            entry = self.agents_input[self._submit_index]
            prompt = entry["prompt"].strip()
            body = {
                "task": prompt,
                "agent": entry.get("agent_or_mode") or "",
                "parent_session": self.parent_session,
                **self.sa_groups,
            }
            if self.cwd:
                body["cwd"] = self.cwd
            data = mcp_core._post("/api/spawn", body)
            self._submit_index += 1
            if data.get("transport_error"):
                line = f"{redact(prompt)[:60]}: {redact(str(data.get('error', '')))}"
                self.sa_unknown.append(line[:_SA_ERROR_MAX_CHARS])
                self.sa_errors.append(line[:_SA_ERROR_MAX_CHARS])
            elif data.get("error"):
                self.sa_errors.append(
                    f"{redact(prompt)[:60]}: {redact(data['error'])}"[:_SA_ERROR_MAX_CHARS]
                )
            elif data.get("id"):
                aid = data["id"]
                self.sa_ids.append(aid)
                if data.get("status") == "queued":
                    self.sa_deferred.add(aid)
            else:
                self.sa_errors.append(
                    f"{redact(prompt)[:60]}: spawn returned no agent id"[:_SA_ERROR_MAX_CHARS]
                )
        now = mcp_core.time.monotonic()
        self.next_due = now
        if self._submit_index < len(self.agents_input):
            return None
        if not self.sa_ids and self.sa_errors:
            return "Error spawning sub-agents:\n" + "\n".join(f"  - {e}" for e in self.sa_errors)
        if not self.sa_ids:
            return "Error: no valid agent entries found in 'agents' array"
        # Submission time is not part of the children's blocking wait budget.
        self.deadline = now + self.max_wait
        self.next_ping = now + self.KEEPALIVE_SECS
        self.hold = _ResumeHold(self.parent_session, self.deadline)
        self.phase = "poll"
        # Every member is posted: a parked, polling step retains no prompts.
        self.agents_input = []
        self._submit_index = 0
        return None

    def step(self) -> str | None:
        if self.phase == "submit":
            return self._submit_one()
        if self.phase == "poll":
            self._poll_once()
            return None
        if self.phase == "hold":
            # Hold the result until the PARENT holds its execution slot again. A
            # subagent parent blocked here yielded its lane slot
            # (waiting_children); its last child ending wakes it in the store,
            # but the slot comes back through admission's pump by capacity.
            # Handing the result over before the grant would let the parent run
            # on without a slot. Event-driven: each request is held server-side
            # until the grant or its bound, so a grant is seen at once. Bounded
            # by the same deadline; on expiry the results are still returned,
            # with the pending resume named.
            done, self.resume_note = self.hold.probe()
            if done:
                self.phase = "collect"
            self.next_due = mcp_core.time.monotonic()
            return None
        return self._collect()

    def _poll_once(self) -> None:
        now = mcp_core.time.monotonic()
        if now >= self.deadline:
            self.phase = "hold"
            return
        if now >= self.next_ping:
            try:
                mcp_core._post("/api/session-keepalive", {})
            except Exception:
                pass  # keepalive is best-effort
            self.next_ping = now + self.KEEPALIVE_SECS
        # Settled for this wait, and not polled again: done, or held by a gate
        # DEFERRAL -- that can wait far longer than this call should hold the
        # parent's turn, and its completion event arrives on its own later. A
        # member queued only behind the concurrency cap is NOT settled: it
        # drains with the wave itself, and the tool's contract is to return its
        # result. An error settles only THIS pass (an agent that never sets
        # done=True would otherwise spin the loop until max_wait), since it can
        # be one failed poll of a member that is still running.
        all_settled = True
        for aid in [a for a in self.sa_ids if a not in self.wait_settled]:
            sa_st = mcp_core._get(f"/api/spawn/{aid}")
            if sa_st.get("done") or _held_by_a_deferral(sa_st):
                self.wait_settled.add(aid)
            elif not sa_st.get("error"):
                all_settled = False
                break
        if all_settled:
            self.phase = "hold"
            self.next_due = mcp_core.time.monotonic()
            return
        self.next_due = mcp_core.time.monotonic() + self.POLL_INTERVAL

    def _collect(self) -> str:
        # Collect results
        sa_results: list[str] = []
        _settled_ids: set[str] = set()  # agents confirmed settled (done or error)
        # Children the wait ended on, with the state each was last seen in. The
        # wait expiring is a fact about THIS call, not about them: they keep their
        # own execution budget, are never cancelled here, and their completion
        # events still arrive, so the caller is told how to keep following them
        # rather than told they failed.
        _unsettled: dict[str, str] = {}
        # Members accepted and still not started when the wait ended (gate-deferred,
        # or waiting for a slot), with the reason they wait. Each is reported ONCE,
        # in the ``queued`` record below, and never as an error: a caller that sees
        # an error for accepted work dispatches it again, and then it runs twice.
        never_started: dict[str, str] = {}
        completed = still_running = errored = 0
        for aid in self.sa_ids:
            sa_st = mcp_core._get(f"/api/spawn/{aid}")
            sa_name = redact(sa_st.get("agent", ""))
            label = sa_name if sa_name else aid
            if not sa_st.get("done") and sa_st.get("queued") is True:
                still_running += 1
                if sa_st.get("resuming") is True:
                    # It ran before and waits to go on: running work, not unstarted.
                    _unsettled[aid] = "waiting_to_resume"
                else:
                    never_started[aid] = _queued_why(sa_st)
                continue
            if sa_st.get("error"):
                errored += 1
                # Only mark as settled if done is also true (confirmed terminal
                # state). An "error" without "done" could be a transport failure
                # from _get() — the agent may still be running.
                if sa_st.get("done"):
                    _settled_ids.add(aid)
                failure: dict[str, Any] = {
                    "agent": label,
                    "status": "error",
                    "error": redact(sa_st["error"]),
                }
                if sa_st.get("done"):
                    # On a finished run ``error`` is why it ended badly, sent next to
                    # the output the run retained (a timed-out run keeps its partial
                    # work). This call marks the child collected, so its completion
                    # event is not injected either: the reply is where that output
                    # reaches the caller.
                    failure["text"] = self._inline_result(aid, sa_st.get("result", ""))
                if aid in self.sa_deferred and not sa_st.get("done"):
                    failure["hint"] = (
                        "accepted at spawn time; its state couldn't be read now; "
                        "check spawn_status before re-spawning"
                    )
                sa_results.append(json.dumps(failure))
            elif not sa_st.get("done"):
                still_running += 1
                if sa_st.get("awaiting_approval"):
                    _unsettled[aid] = "waiting_permission"
                else:
                    _unsettled[aid] = "running"
            else:
                completed += 1
                _settled_ids.add(aid)
                result_text = self._inline_result(aid, sa_st.get("result", ""))
                sa_results.append(
                    json.dumps(
                        {
                            "agent": label,
                            "status": "completed",
                            "text": result_text,
                        }
                    )
                )
        if _unsettled:
            sa_results.append(
                json.dumps(
                    {
                        "status": "still_running",
                        "task_ids": list(_unsettled),
                        "states": _unsettled,
                        "waited_secs": int(self.max_wait),
                        "query": "spawn_status/spawn_list",
                        "note": (
                            "The blocking wait ended; these sub-agents were NOT cancelled and "
                            "keep running on their own budget. Their [Subagent completion "
                            "event] messages still arrive; poll spawn_list or spawn_status "
                            "for progress."
                        ),
                    }
                )
            )
        # Members still queued and not started (collected above). They have no run
        # yet, so without this record the reason -- the one fact that says what to
        # change -- would stay in the gateway log.
        if never_started:
            sa_results.append(
                json.dumps(
                    {
                        "status": "queued",
                        "agents": never_started,
                        "note": (
                            "Queued, not started: accepted, and waiting for the reason given. "
                            "They start on their own once it clears and their [Subagent "
                            "completion event] messages arrive as usual; nothing was "
                            "cancelled. Do not spawn them again."
                        ),
                    }
                )
            )
        if self.resume_note:
            sa_results.append(json.dumps(self.resume_note))
        if self.sa_errors:
            sa_results.append(json.dumps({"status": "spawn_errors", "errors": self.sa_errors}))
        self._settled_ids = _settled_ids
        self._counts = {
            "completed": completed,
            "still_running": still_running,
            "errored": errored,
        }
        return "\n\n".join(sa_results)

    def on_settled(self, text: str) -> None:
        # Only the driver's accepted-result commit suppresses completion events
        # and writes this tool's own row: a cancelled collection or an abandon
        # reply has delivered no child output. The generic ``call_tool_with_logging``
        # row is _AuditedDeferred's; this one carries the per-child tallies and the
        # ``partial`` outcome, as the blocking tool's did.
        if self.phase == "abandoned":
            return
        counts, self._counts = self._counts, None
        if counts is not None:
            mcp_core.sel().log_tool_invocation(
                session_key=_audit_owner(self.parent_session),
                source="mcp_core",
                tool_name="spawn_sub_agents",
                outcome=(
                    "completed"
                    if not counts["still_running"] and not counts["errored"]
                    else "partial"
                ),
                metadata={"spawned": len(self.sa_ids), **counts},
            )
        settled_ids, self._settled_ids = self._settled_ids, set()
        if settled_ids and self.parent_session:
            try:
                mcp_core._post(
                    "/api/spawn/mark-collected",
                    {"ids": list(settled_ids), "parent_session": self.parent_session},
                    timeout=5,
                )
            except Exception:
                pass  # best-effort: a missed mark leaves the completion event enabled


def _live_adaptive_state() -> dict[str, Any] | None:
    """The adaptive controller's state, however this process can reach it.

    In the GATEWAY process the controller is a module-level registry read. A
    tool server is a DIFFERENT process, so that registry is empty there and the
    answer has to come over the loopback API: without it the only number this
    tool can print is the configured ceiling (``max_subagents``), while the cap
    actually in force may be 1. Returns None when neither path answers; the
    caller then labels what it prints as a ceiling.

    ``/api/spawn/adaptive`` and NOT ``/api/tasks/summary``, which carries the
    same object: the ``/api/spawn`` prefix is on ``_MIXED_INTERNAL_API_PATHS``,
    so a sibling route under it is reachable with the internal secret this
    process holds, while the summary route is in neither internal bucket (it
    also lists per-task session keys and lease owners). Reading it there would
    403 and leave the ceiling printed -- the defect this exists to fix.

    Every failure is logged at debug. A silent degrade to the ceiling is exactly
    what went unnoticed before, so the one line that explains it has to exist.
    """
    state = host_status.adaptive_state()
    if state:
        return state
    try:
        payload = mcp_core._get("/api/spawn/adaptive", timeout=5)
    except Exception:
        logger.debug("adaptive state read failed in transport", exc_info=True)
        return None
    if not isinstance(payload, dict) or payload.get("error"):
        logger.debug(
            "adaptive state read refused: %s",
            payload.get("error") if isinstance(payload, dict) else type(payload).__name__,
        )
        return None
    remote = payload.get("adaptive")
    if isinstance(remote, dict) and remote:
        return remote
    logger.debug("adaptive state read returned no controller state")
    return None


def resource_status(name: str, args: dict[str, Any]) -> str:
    rstatus = host_status.probe()
    out = rstatus.summary_lines()
    # ``summary_lines`` already appended the adaptive block when this process
    # OWNS the controller. It does not in a tool server, so fetch it and render
    # it with the same helper rather than a second formatting of the same facts.
    have_block = any(line.startswith("Adaptive concurrency") for line in out)
    state = None if have_block else _live_adaptive_state()
    if state:
        out.extend(host_status.adaptive_summary_lines(state))
    elif not have_block:
        try:
            cap = resolve_max_subagents(KiroCrewConfig.load())
        except Exception:
            cap = 0
        if cap > 0:
            out.append(
                f"  Sub-agent ceiling: {cap} (configured max; the cap actually in "
                "force could not be read from the gateway)"
            )
    if rstatus.posture == "critical":
        out.append(
            "\nGuidance: memory is critically low — do NOT start heavy work "
            "(full suites, large builds, big sub-agent waves) now; run only "
            "light steps or wait for memory to free."
        )
    elif rstatus.posture == "tight":
        out.append(
            "\nGuidance: memory is tight — prefer the lighter path (targeted "
            "tests, fewer sub-agents, deferred builds) for heavy work."
        )
    elif rstatus.memory_pressure_held:
        # The posture is figure-based and can read AMPLE while the macOS kernel
        # reports pressure; "heavy work is fine" would then contradict the gate.
        # The queue note itself is on the "Kernel memory pressure" line above.
        out.append(
            f"\nGuidance: {MEMORY_PRESSURE_PHRASE} — prefer the lighter path for heavy work."
        )
    elif rstatus.posture == "ample":
        out.append("\nGuidance: ample headroom — heavy work is fine.")
    else:
        out.append(
            "\nGuidance: headroom could not be measured on this host — "
            "proceed with normal caution."
        )
    return "\n".join(out)


HANDLERS: dict[str, Callable[[str, dict[str, Any]], "str | DeferredTool"]] = {
    "spawn_run": spawn_run,
    "spawn_continue": spawn_continue,
    "spawn_steer": spawn_steer,
    "spawn_release": spawn_release,
    "spawn_list": spawn_list,
    "spawn_status": spawn_status,
    "spawn_sub_agents": spawn_sub_agents,
    "resource_status": resource_status,
}
