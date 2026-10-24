"""The spawn tools' advertisement half, and the agent roster their descriptions name.

:func:`schemas` builds the descriptors ``tools/list`` answers from for the spawn domain,
reading the live sub-agent cap for a description. :func:`_agent_roster_hint` is the short
roster the ``spawn_run`` parameters carry, and :func:`_parent_allowlist_filter` narrows a
roster to the agents the calling parent may spawn, for the hint and for ``spawn_list``.
The handlers that run these tools are the other half:
``kiro_crew.mcp_tools.spawn.HANDLERS`` maps each name advertised here to one of them.

This module is an owner of :mod:`kiro_crew.mcp_tools.spawn`, which serves every name here
and stays the import path and the patch seam: a patch through it reaches this module too
(:mod:`kiro_crew._facade_mirror`). It never imports the facade.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from kiro_crew import mcp_core
from kiro_crew import resource_status as host_status
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.constants import DEFAULT_SUBAGENT_MAX_TURNS
from kiro_crew.execution_context import read_session_execution
from kiro_crew.spawn_label import SPAWN_LABEL_MAX_CHARS
from kiro_crew.subagent import (
    agent_matches_allowlist,
    is_internal_agent_spec,
    parent_spawn_allowlists,
    resolve_max_subagents,
    visible_agent_names,
)

# Roster carried in the spawn_run parameter descriptions. Kept small on purpose:
# a tool description is always-on context in every session, so this buys
# self-correction for a few dozen characters, not a full agent listing.
_MAX_ROSTER_NAMES = 8


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
                                "backend": {"type": "string"},
                                "label": {"type": "string"},
                            },
                            "required": ["task"],
                            "additionalProperties": False,
                        },
                        "description": (
                            "Multiple tasks to run in parallel as one wave. Each entry is a "
                            "prompt string, or an object {task, model?, "
                            "reasoning_effort?, backend?, label?} whose fields override the "
                            "call's batch-wide value for that task only, e.g. the same review "
                            "prompt on two models or two backends, delivered together."
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
                    "backend": {
                        "type": "string",
                        "description": (
                            "Optional backend (agent harness) for the subagent(s), e.g. "
                            "'kiro', 'claude', 'codex': the run starts on that harness "
                            "instead of the gateway default, so one orchestrator can hand "
                            "each task to the harness that suits it. A backend that is "
                            "unavailable AT SPAWN TIME is REFUSED with the list of "
                            "available ones (and its install command when it is only "
                            "missing), never replaced by the default. Pass 'model' as an "
                            "id from THAT backend's list. Batch-wide; a tasks[] object can "
                            "override it per task. A spawn_continue follow-up asks for the "
                            "run's backend."
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
                    "label": {
                        "type": "string",
                        "description": (
                            "Optional short name for the task, shown in the dashboard's "
                            "task box and in this tool's result instead of the task text "
                            f"(one line, at most {SPAWN_LABEL_MAX_CHARS} characters; longer "
                            "is cut). Display only: the subagent still receives the task "
                            "text. Batch-wide; a tasks[] object can set its own."
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
                "A backend that keeps its own session store answers 'Not "
                "disposed': that session may still resume. "
                "Call when the delegated workstream is finished. Idle "
                "conversations also expire automatically after several hours. "
                "On a run of yours that is still queued (accepted, not started) "
                "it CANCELS the run instead: it never starts."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversation": {
                        "type": "string",
                        "description": (
                            "Conversation id — the run id of any completed subagent "
                            "run (keep=true only extends retention), or of a queued "
                            "run to cancel"
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
