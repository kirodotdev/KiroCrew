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

import asyncio
import json
import os
import uuid
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode

from kiro_crew import mcp_core
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.context_management import COMPLETION_KEEP_DEFAULT_CHARS
from kiro_crew.mcp_shared import ToolCancelled, is_tool_cancelled
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.subagent import (
    AGENT_NOT_FOUND_CODE,
    resolve_max_subagents,
    visible_agent_names,
)
from kiro_crew.subagent_persistence import agent_dir_for_display
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


def _agent_roster_hint() -> str:
    """Valid agent names, for the ``agent``/``agents`` parameter descriptions.

    The roster used to be reachable only through ``spawn_list``'s OUTPUT, so a
    caller that went straight to ``spawn_run`` had never seen it and invented
    plausible-sounding names instead (#4842). Putting it in the parameter
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

    Skipped entirely when an event loop is running, because then this is NOT the
    stdio server: ``mcp_discovery._managed_tools_in_process`` imports this package
    and calls ``_list_tools()`` from ``async def probe_server`` on the gateway's
    loop, on hosts where the probe spawn is refused. A directory scan there would
    stall the loop -- and that caller keeps only tool NAMES, discarding every
    description, so it loses nothing. ``mcp_shared.run_mcp_stdio_loop`` is a plain
    select/readline loop that never imports asyncio, so the process that actually
    serves ``tools/list`` to a model still gets the roster.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # no loop: the stdio server, where a bounded cached scan is fine
    else:
        return ""
    try:
        # Sorted by DECLARED name, before redaction, so the order matches the
        # refusal roster's and a credential-shaped name is rewritten in place
        # rather than re-sorted into a different slot.
        shown, withheld = visible_agent_names(
            sorted(a.name for a in mcp_core.list_agents() if a.name),
            limit=_MAX_ROSTER_NAMES,
        )
    except Exception:
        return ""  # never let a directory read break the tool advertisement
    if not shown:
        return ""
    hint = f" Valid names right now: {', '.join(shown)}"
    if withheld:
        hint += f" (+{withheld} more)"
    return hint + "."


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the spawn tools."""
    # Advertise the live concurrent sub-agent cap so the model fans out with
    # confidence instead of self-limiting. resolve_max_subagents is the single
    # source of truth (auto-sizes from host mem/CPU + learned cost, or the
    # explicit agent.max_subagents). A snapshot at tool-list time is fine: this
    # is advisory guidance, not an enforced limit, and SubagentManager
    # auto-queues any overflow regardless.
    try:
        _max_sub = resolve_max_subagents(KiroCrewConfig.load())
    except Exception:
        _max_sub = 0
    _cap_hint = (
        f" You can run up to {_max_sub} sub-agents concurrently; if a task has "
        "more independent parts than that, still pass ALL of them in one call — "
        "any beyond the cap are queued and drained automatically as slots free, "
        "so you never need to split the work into multiple manual rounds."
        if _max_sub > 0
        else ""
    )
    # The valid agent names, read once and shared by every agent-taking field
    # below, so a caller that never called spawn_list still sees them (#4842).
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
                "Returns immediately — results arrive as [Subagent completion event] "
                "messages in your conversation. For parallel work, use 'tasks' array. "
                "Tasks are automatically batched if they exceed the concurrency limit."
                + _cap_hint
                + " WAIT for all completion events before responding to the user."
                " If result batches from a previous spawn are still arriving,"
                " do not start a new spawn until all of them have been"
                " delivered and processed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Single task description",
                    },
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Multiple tasks to run in parallel",
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
                        "description": "Override tool-call budget for this spawn (default: config or 100)",
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
                            "instead of the gateway default. To discover available models, "
                            "run: kiro-cli chat --list-models --format json"
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
                        "description": "Conversation id — the id of the original keep=true spawn_run",
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
                "runs still WAITING in the spawn queue return not_found until "
                "they start. Only works while the run is executing; for a "
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
                        "description": "Conversation id — the id of the original keep=true spawn_run",
                    },
                },
                "required": ["conversation"],
            },
        },
        {
            "name": "spawn_list",
            "description": "List all running and completed subagents (read-only, no commands executed)",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "spawn_status",
            "description": (
                "Retrieve a completed subagent's full transcript by agent ID (from a "
                "completion event). The completion event gives a summary plus this "
                "transcript on disk — use this tool (or the read/grep tools on the path) "
                "to read the rest instead of re-running the subagent. For large "
                "transcripts, page with offset/limit (line-based, like reading code) or "
                "filter with grep (regex) rather than pulling the whole thing into context."
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
                "complete, then returns their collected results. Use for delegating "
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
                "advisory posture (ample / tight / critical) plus the current "
                "concurrent sub-agent cap, so you can decide whether to run the "
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
    """True when *resp* is the gateway refusing *agent* as a name it cannot load.

    Reads the response's machine-readable ``code`` (``AGENT_NOT_FOUND_CODE``,
    spelled once in ``subagent`` and imported by both sides), not its prose. The
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
    return bool(agent) and resp.get("code") == AGENT_NOT_FOUND_CODE


def _collapse_effort_verdicts(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Group (subagent id, verdict text) pairs into (id list, verdict text) rows.

    ``reasoning_effort`` and ``model`` are batch-wide, so a wide fan-out
    usually yields the IDENTICAL verdict for every member — rendering it once
    per subagent injects N copies of the same line into the calling agent's
    context (#6185). Collapse each group of 2+ ids sharing a verdict into one
    row naming all of them ("a1, a2, a3"); a verdict unique to one subagent
    keeps its own row, so mixed batches keep full per-id attribution. Groups
    preserve first-seen dispatch order, and ids keep their dispatch order
    within a group, so the collapsed output remains deterministic.
    """
    grouped: dict[str, list[str]] = {}
    for sid, text in pairs:
        grouped.setdefault(text, []).append(sid)
    return [(", ".join(ids), text) for text, ids in grouped.items()]


def spawn_run(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SPAWN_RUN_SCHEMA)

    tasks = args.get("tasks")
    task = args.get("task")

    # Support both single task and batch tasks
    if tasks and isinstance(tasks, list):
        task_list = [t for t in tasks if isinstance(t, str) and t.strip()]
    elif task:
        task_list = [task]
    else:
        return "Error: task or tasks is required"

    # Read parent session key so completions inject back into this session.
    parent_session = mcp_core._resolve_session_key()

    # Fire-and-forget — gateway's SubagentManager queues excess tasks
    # and auto-spawns them as slots free up.
    agent = args.get("agent") or ""
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
    agent_names: list[str] = []
    # (subagent id, reason) pairs from the server's effort verdict — the
    # gateway resolves the effective model (per-call value, else role pin,
    # else unpinned) and reports when the requested effort cannot apply.
    effort_drops: list[tuple[str, str]] = []
    # (subagent id, note) pairs for the delivery mirror: the resolved model and
    # the family settings key a requested effort is delivered under.
    effort_applies: list[tuple[str, str]] = []
    agent_tasks: list[str] = []
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

    def _reconcile_lost(reason: str) -> None:
        """Tell the gateway this member never reached ``mgr.spawn``.

        Every sibling's ``batch_total`` counts it, so an un-reconciled member
        leaves the wave at submitted < expected forever: the digest never closes
        and held sibling results strand until restart.
        """
        if not batch_id:
            return
        try:
            mcp_core._post(
                "/api/spawn/lost",
                {
                    "batch_id": batch_id,
                    "batch_total": len(task_list),
                    "reason": reason[:300],
                    "parent_session": parent_session,
                },
            )
        except Exception:
            pass  # reaper backstop covers delivery failure

    # Agent names this wave already learned the gateway refuses as unknown.
    # Re-posting one cannot succeed: the refusal is a property of the NAME, not
    # of the task, so the rest of a wave that shares it is dead on arrival. The
    # observed cost of not knowing that was a whole wave of doomed dispatches on
    # one invented name (#4842).
    refused_agents: dict[str, str] = {}
    for i, t in enumerate(task_list):
        a = agents_list[i] if agents_list else agent
        if a in refused_agents:
            # Short line on purpose: the full roster is already on the first
            # refusal above, and repeating it once per remaining member would
            # bury it.
            errors.append(f"{t[:60]}: not dispatched - agent {a!r} refused above")
            _reconcile_lost(refused_agents[a])
            continue
        body: dict[str, Any] = {"task": t, "agent": a, "parent_session": parent_session}
        if batch_id:
            body["batch_id"] = batch_id
            body["batch_total"] = len(task_list)
        if max_turns:
            body["max_turns"] = max_turns
        if cwd:
            body["cwd"] = cwd
        if model:
            body["model"] = model
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
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
        if d.get("error"):
            error_line = f"{t[:60]}: {d['error']}"
            if d.get("transport_error"):
                # The gateway may have accepted the spawn before the
                # response failed. Treat it as unknown, not rejected, and
                # do not reconcile it as lost (which could close a batch
                # early while the accepted member is still running).
                transport_errors.append(error_line)
                continue
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
            continue
        agent_ids.append(d.get("id", "?"))
        agent_names.append(a)
        agent_tasks.append(t)
        if d.get("effort_dropped"):
            effort_drops.append((str(d.get("id", "?")), str(d["effort_dropped"])))
        if d.get("effort_applied"):
            effort_applies.append((str(d.get("id", "?")), str(d["effort_applied"])))

    spawn_lines: list[str] = []
    # Server-computed effort verdicts (never a rejection — gated on agent_ids
    # so a total-failure result keeps its "Error:" first line, which SEL and
    # callers test as a prefix). The gateway resolves the effective model
    # (per-call value, else the subagent role pin, else unpinned/"auto") at
    # accept time, so — unlike the old client-side check — this also reports
    # the default case where no per-call model was passed and the effort
    # would otherwise be dropped silently.
    if agent_ids:
        for drop_ids, drop_reason in _collapse_effort_verdicts(effort_drops):
            spawn_lines.append(
                f"ℹ reasoning_effort='{reasoning_effort}' dropped for {drop_ids}: {drop_reason}"
            )
        for applied_ids, applied_note in _collapse_effort_verdicts(effort_applies):
            spawn_lines.append(
                f"✓ reasoning_effort='{reasoning_effort}' applied for {applied_ids} ({applied_note})"
            )
    if not parent_session and agent_ids:
        # Orphan alert: without a parent session key the subagents cannot
        # deliver completion events back to this conversation and will
        # not appear in the Subagents panel for this session. This has
        # historically failed silently — say it
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
        if parent_session:
            spawn_lines.append(
                f"Spawned {len(agent_ids)} subagent(s). Results will arrive as completion events:"
            )
        else:
            # Orphaned (warning above): completion events cannot be
            # delivered — do not promise them in the same breath.
            spawn_lines.append(
                f"Spawned {len(agent_ids)} subagent(s). Monitor results via polling:"
            )
        for aid, a, t in zip(agent_ids, agent_names, agent_tasks):
            label = f"{aid} ({a})" if a else aid
            spawn_lines.append(f"  {label}: {t[:80]}")
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
            ". An empty spawn_list result is inconclusive for queued work; "
            "wait and recheck before retrying to avoid duplicate work."
        )
        spawn_lines.append(guidance)
    if agent_ids:
        if parent_session:
            spawn_lines.append(
                "\n⚠️ END YOUR TURN NOW — do no further work this turn."
                " Wait for the [Subagent completion event] messages, which will resume you."
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


def spawn_list(name: str, args: dict[str, Any]) -> str:
    d = mcp_core._get("/api/spawn")
    agents = d.get("agents", [])

    def _redact(text: str) -> str:
        return redact(text)

    lines: list[str] = []
    if not agents:
        lines.append("No subagents running.")
    else:
        for a in agents:
            # A run parked on an unanswered spawn-approval prompt has launched
            # no process and produced no turn, so reporting it as "running" is
            # actively misleading to a caller this module itself points here
            # ("Check spawn_list", above) -- it reads as work in progress when
            # the truth is that a human has not approved it yet (#6484).
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
    # name the gateway accepts, so a full listing shows it.
    try:
        names, _ = visible_agent_names((a.name or "" for a in mcp_core.list_agents()), exclude=())
        if names:
            lines.append(f"\nAvailable agents: {', '.join(names)}")
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
    if d.get("error"):
        return f"Error: {d['error']}"

    meta = d.get("result_meta")
    if isinstance(meta, dict) and meta.get("grep_error"):
        return f"Error: {meta['grep_error']}"

    result = d.get("result") or "_No result._"
    result, _ = redact_exfiltration_urls(result)
    result, _ = redact_credentials(result)

    if isinstance(meta, dict) and meta:
        # Paged/grepped read — prepend a compact header so the LLM knows how
        # much it saw and how to continue, without re-reading the whole file.
        hdr: list[str] = []
        total = meta.get("total_lines", "?")
        if "matched_lines" in meta:
            hdr.append(f"{meta['matched_lines']} line(s) matched grep of {total} total")
        start = meta.get("offset", 0)
        returned = meta.get("returned_lines", 0)
        hdr.append(f"showing lines {start}-{start + returned} of {total}")
        if meta.get("has_more"):
            hdr.append(f"more available — call again with offset={start + returned}")
        return f"[{' | '.join(hdr)}]\n{result}"
    return result


def spawn_sub_agents(name: str, args: dict[str, Any]) -> str:
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

    # Validate individual agent entries (schema guarantees dict entries)
    for entry in agents_input:
        p = entry.get("prompt", "")
        if len(p) > MAX_MEDIUM_STRING:
            entry["prompt"] = p[:MAX_MEDIUM_STRING]
        a = entry.get("agent_or_mode", "")
        if len(a) > MAX_SHORT_STRING:
            entry["agent_or_mode"] = a[:MAX_SHORT_STRING]

    mcp_core.sel().log_tool_invocation(
        session_key=_audit_owner(parent_session),
        source="mcp_core",
        tool_name="spawn_sub_agents",
        outcome="attempt",
        metadata={"agent_count": len(agents_input)},
    )

    sa_ids: list[str] = []
    sa_errors: list[str] = []
    for entry in agents_input:
        prompt = entry.get("prompt", "").strip()
        if not prompt:
            continue
        sa_agent = entry.get("agent_or_mode") or ""
        sa_body = {
            "task": prompt,
            "agent": sa_agent,
            "parent_session": parent_session,
            **sa_groups,
        }
        if cwd:
            sa_body["cwd"] = cwd
        d = mcp_core._post("/api/spawn", sa_body)
        if d.get("error"):
            sa_errors.append(f"{_redact_sa(prompt)[:60]}: {_redact_sa(d['error'])}")
        else:
            aid = d.get("id", "")
            if aid:
                sa_ids.append(aid)
            else:
                sa_errors.append(f"{_redact_sa(prompt)[:60]}: spawn returned no agent id")

    if not sa_ids and sa_errors:
        return "Error spawning sub-agents:\n" + "\n".join(f"  - {e}" for e in sa_errors)
    if not sa_ids:
        return "Error: no valid agent entries found in 'agents' array"

    # Poll until all sub-agents complete. Ping /api/session-keepalive every
    # 60s so the gateway's is_responsive() does not flag this session as
    # stale and SIGTERM the ACP subprocess mid-poll, which would abort the
    # very sub-agents we are waiting on.
    poll_interval = 2.0
    try:
        max_wait = float(os.environ.get("KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT", "7200"))
    except (TypeError, ValueError):
        max_wait = 7200.0
    max_wait = max(60.0, min(7200.0, max_wait))  # clamp: 1 min .. 2 hours
    deadline = mcp_core.time.monotonic() + max_wait
    _next_ping = mcp_core.time.monotonic() + 60.0  # first keepalive after 60s, not immediately
    while mcp_core.time.monotonic() < deadline:
        # Cooperative cancellation: honor notifications/cancelled the same
        # way wait does, so a cancelled spawn_sub_agents call exits promptly
        # instead of blocking the tool worker until every sub-agent settles
        # or max_wait elapses.
        if is_tool_cancelled():
            raise ToolCancelled(
                f"spawn_sub_agents cancelled while awaiting {len(sa_ids)} sub-agent(s)"
            )
        if mcp_core.time.monotonic() >= _next_ping:
            try:
                mcp_core._post("/api/session-keepalive", {})
            except Exception:
                pass  # keepalive is best-effort
            _next_ping = mcp_core.time.monotonic() + 60.0
        all_done = True
        for aid in sa_ids:
            sa_st = mcp_core._get(f"/api/spawn/{aid}")
            # An errored/crashed agent is "settled" — without this, an agent
            # that never sets done=True would spin the loop until max_wait.
            if not (sa_st.get("done") or sa_st.get("error")):
                all_done = False
                break
        if all_done:
            break
        mcp_core.time.sleep(poll_interval)

    # Collect results
    sa_results: list[str] = []
    completed = 0
    timed_out = 0
    errored = 0
    _settled_ids: set[str] = set()  # agents confirmed settled (done or error)
    for aid in sa_ids:
        sa_st = mcp_core._get(f"/api/spawn/{aid}")
        sa_name = _redact_sa(sa_st.get("agent", ""))
        label = sa_name if sa_name else aid
        if sa_st.get("error"):
            errored += 1
            # Only mark as settled if done is also true (confirmed terminal
            # state). An "error" without "done" could be a transport failure
            # from _get() — the agent may still be running.
            if sa_st.get("done"):
                _settled_ids.add(aid)
            sa_results.append(
                json.dumps(
                    {
                        "agent": label,
                        "status": "error",
                        "error": _redact_sa(sa_st["error"]),
                    }
                )
            )
        elif not sa_st.get("done"):
            timed_out += 1
            sa_results.append(json.dumps({"agent": label, "status": "timed_out"}))
        else:
            completed += 1
            _settled_ids.add(aid)
            result_text = _redact_sa(sa_st.get("result", ""))
            # Apply the same summarize_result treatment as spawn_run:
            # when results exceed completion_keep threshold, return a
            # summary + disk path instead of the full transcript. This
            # prevents massive tool_results from filling the model's
            # context window and causing attention degradation.
            if len(result_text) > COMPLETION_KEEP_DEFAULT_CHARS:
                try:
                    result_path = str(agent_dir_for_display(aid) / "result.txt")
                except (ValueError, OSError):
                    result_path = ""
                if result_path:
                    result_text = mcp_core.summarize_result(result_text, result_path)
            sa_results.append(
                json.dumps(
                    {
                        "agent": label,
                        "status": "completed",
                        "text": result_text,
                    }
                )
            )
    if sa_errors:
        sa_results.append(json.dumps({"status": "spawn_errors", "errors": sa_errors}))
    mcp_core.sel().log_tool_invocation(
        session_key=_audit_owner(parent_session),
        source="mcp_core",
        tool_name="spawn_sub_agents",
        outcome="completed" if not timed_out and not errored else "partial",
        metadata={
            "spawned": len(sa_ids),
            "completed": completed,
            "timed_out": timed_out,
            "errored": errored,
        },
    )
    # Mark collected IDs so _subagent_done skips redundant injection.
    # The blocking tool already delivered results inline; without this the
    # on_done callback triggers a new _run_chat turn that clobbers any
    # [OPTIONS:] buttons rendered in the synthesis.
    # Only mark agents whose results were actually delivered inline
    # (completed or errored) — timed-out agents may still complete later
    # and their real result must not be suppressed.
    if _settled_ids and parent_session:
        try:
            mcp_core._post(
                "/api/spawn/mark-collected",
                {"ids": list(_settled_ids), "parent_session": parent_session},
                timeout=5,
            )
        except Exception:
            pass  # best-effort; worst case = duplicate turn (pre-existing behavior)
    return "\n\n".join(sa_results)


def resource_status(name: str, args: dict[str, Any]) -> str:
    from kiro_crew.resource_status import probe as _probe_resources

    rstatus = _probe_resources()
    out = rstatus.summary_lines()
    try:
        cap = resolve_max_subagents(KiroCrewConfig.load())
    except Exception:
        cap = 0
    if cap > 0:
        out.append(f"  Concurrent sub-agent cap: {cap}")
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
    elif rstatus.posture == "ample":
        out.append("\nGuidance: ample headroom — heavy work is fine.")
    else:
        out.append(
            "\nGuidance: headroom could not be measured on this host — "
            "proceed with normal caution."
        )
    return "\n".join(out)


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "spawn_run": spawn_run,
    "spawn_continue": spawn_continue,
    "spawn_steer": spawn_steer,
    "spawn_release": spawn_release,
    "spawn_list": spawn_list,
    "spawn_status": spawn_status,
    "spawn_sub_agents": spawn_sub_agents,
    "resource_status": resource_status,
}
