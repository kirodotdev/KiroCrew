"""The work-ledger MCP server — the narrow write path from a worker back to the
conductor that dispatched it, and the conductor's structured read of its own fleet.

A conductor session dispatches work to child sessions and otherwise learns what
happened by reading their transcripts. These four tools replace that inference
with a record: a worker writes a schema-bounded status against the ONE work item
it was bound to, and the conductor reads that record as data.

Why this is a store and not a message. ``session_send`` would let a worker push a
line into the conductor's session, and it is withheld from workers for a reason
that does not weaken with care: the text becomes the conductor's next prompt,
executed under the conductor's grants. So the requirement here is not "a channel
from worker to conductor" but a channel that CANNOT carry an instruction. Nothing
on this server reaches ``enqueue_or_run_prompt``; ``work_report`` writes JSON
fields into a file the conductor reads.

Its own server, and ``opt_in`` rather than always-on. ``kirocrew-core`` is in
every agent's spec, and almost no session is a conductor or a worker — for those
the only reachable answer is ``not_bound`` or ``no_ledger``, so mounting these
schemas everywhere would charge every session context for a refusal. kiro-cli
loads a server only when something references it, so a spec that wants the set
hand-builds the ``mcpServers`` entry and adds ``@kirocrew-work`` to ``tools``.
The refusal itself is kept: it is still what an unbound caller here gets.

**No ``autoApprove`` key, and none may be added.** An autoApproved MCP tool is
approved inside kiro-cli and emits no permission request, so ``hooks.on_tool_call``
— the PreToolUse gate carrying the deny floor, the sensitive-path check and the
governance ceiling — is never reached for it. A store that writes agent-authored
text into a record the user reads is not the place to break that.

All four tools are advertised to every caller and DISPATCH BY RESOLVED IDENTITY at
call time, because a session can be a worker to its parent and a conductor to its
own children:

* a binding file, no ledger directory → the worker pair answers, the conductor
  pair returns ``no_ledger``
* a ledger directory, no binding file → the conductor pair answers, the worker
  pair returns ``not_bound``
* both — a second-level conductor → all four answer
* neither → ``not_bound`` / ``no_ledger``

Splitting the worker half onto a server of its own would express the same rule in
the specs instead and buy nothing: a second-level conductor mounts both halves
anyway, so the split would have to be rejoined for exactly the case the depth cap
exists to permit.

Identity is resolved, never asserted. Every tool routes through
``mcp_core.require_strict_session_key`` — the gateway-injected caller block,
``KIROCREW_SESSION_KEY``, or the HMAC host-pid sidecar, and explicitly NOT the
lenient resolver's ``/proc`` ancestor walk, under which a subagent resolves to its
PARENT's identity and could report against its parent's item. The key that passed
the gate is the key sent on the wire; re-resolving at the request would check one
identity and act as another. No tool takes a session key, a conductor id, or an
item id from the worker side — the server derives all three from the caller's own
binding.
"""

from __future__ import annotations

import json
import logging
from typing import Any

# Same cross-module reuse as ``mcp_dashboard``: the authenticated loopback client
# to the gateway lives in ``mcp_core``, and its heavy dependencies are
# function-local so importing it here is cheap.
from kiro_crew.mcp_core import _get, _post, _resolve_session_key, require_strict_session_key
from kiro_crew.mcp_shared import call_tool_with_logging, run_mcp_stdio_loop
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import MCP_WORK_SCHEMAS, validate_tool_args

logger = logging.getLogger(__name__)

SERVER_NAME = "kirocrew-work"
SERVER_VERSION = "1.0.0"

#: The worker half. Named as a tuple so the channel-agent block list and the
#: registration tests can enumerate the surface without parsing the definitions.
WORKER_TOOLS: tuple[str, ...] = ("work_brief", "work_report")

#: The conductor half.
CONDUCTOR_TOOLS: tuple[str, ...] = ("work_ledger_read", "work_ledger_record")

WORK_TOOLS: tuple[str, ...] = WORKER_TOOLS + CONDUCTOR_TOOLS

_BRIEF_PATH = "/api/work-ledger/brief"
_REPORT_PATH = "/api/work-ledger/report"
_READ_PATH = "/api/work-ledger"
_RECORD_PATH = "/api/work-ledger/record"

#: Fields ``work_ledger_record`` forwards. The action selects which of them it
#: REQUIRES; a field an action has no use for is generally IGNORED rather than
#: refused, so a caller cannot infer from a 200 that every field it sent was read.
#: The store refuses only where a wrong field would change meaning — a ``round`` on
#: an action that does not carry one is ``invalid_value``, and a ``verdict`` or
#: ``state`` outside its vocabulary likewise — while a stray ``title`` on a
#: ``bind`` is simply dropped.
_RECORD_FIELDS: tuple[str, ...] = (
    "action",
    "item_id",
    "title",
    "acceptance",
    "worker_session_key",
    "decision",
    "verdict",
    "state",
    "goal",
    "round",
    "fails",
)


def _tool_definitions() -> list[dict[str, Any]]:
    """The tool surface this server advertises."""
    return [
        {
            "name": "work_brief",
            "description": (
                "Read the ONE work item this session was dispatched for: its title, its "
                "acceptance condition, the round it belongs to, the conductor's latest "
                "decision, and your own last reported status. Call it before starting "
                "work and treat title + acceptance as the definition of done. Takes no "
                "arguments — which item you are bound to is resolved from your own "
                "session, not supplied. It does not return the conductor's goal or any "
                "sibling item: you have neither. Answers 'not_bound' when this session "
                "is not a dispatched worker."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "work_report",
            "description": (
                "Report your status against your own work item, as data the conductor "
                "reads without interpreting it. Call it at each real milestone rather "
                "than on a timer. 'progress' is informational; 'blocked' means an "
                "external dependency stopped the work; 'question' means the conductor's "
                "own decision is needed (the two differ by who must act); 'done' claims "
                "the acceptance condition is met — put the evidence in artifacts and any "
                "pull-request number in pr. A 'done' is a claim the conductor verifies, "
                "never an acceptance: nothing here can write a verdict. Write facts and "
                "pointers to what you produced, not requests. Which item this lands on "
                "is resolved from your own session — there is no item parameter."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["progress", "done", "blocked", "question"],
                        "description": "Where the work stands, in one of four values.",
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "Your own account of where the work is, <= 500 chars. Facts "
                            "and artifact pointers; refused, not truncated, when longer."
                        ),
                    },
                    "artifacts": {
                        "type": "object",
                        "description": (
                            "String->string pointers to what you produced (pr, commit, "
                            "branch, paths). <= 16 keys, key <= 64, value <= 512."
                        ),
                    },
                    "pr": {
                        "type": "integer",
                        "description": (
                            "A pull-request number you produced. A CLAIM only: the "
                            "conductor promotes it into the acceptance bar explicitly, "
                            "so naming a number does not move your own bar."
                        ),
                    },
                },
                "required": ["status", "summary"],
            },
        },
        {
            "name": "work_ledger_read",
            "description": (
                "Read the whole work ledger this conductor session owns: the conductor "
                "record, every item with all its fields, each item's derived 'orphaned', "
                "'stale' and 'acceptance_concrete' flags, the newest events per item, and "
                "a ready-to-pipe 'accept_batch' document for the goal-conductor skill's "
                "accept_eval.py. Takes no arguments — the ledger is your own. accept_batch "
                "is built from each item's acceptance ALONE and deliberately ignores a "
                "worker's claimed pr, so a worker cannot point your bar at someone else's "
                "green pull request. It also leaves out any item whose bar is not concrete "
                "yet — an unknown kind, or a placeholder ('TBD', blank) or wrong type in a "
                "field the evaluator reads for that kind, such as a pr_checks pr that is "
                "not a positive integer — because accept_eval.py can only answer 'error' "
                "to those; a placeholder in a field it never reads costs an item nothing. "
                "The item's 'acceptance_concrete' flag is why it is missing, and an "
                "'accept' write puts it back. Each entry carries that item's status so you can "
                "apply your own 'done only' filter without a second lookup; the batch is "
                "not filtered for you. An item is stale only when it has gone quiet AND "
                "its session is not running AND its last report still left the move with "
                "the worker, so a worker in a long build is never flagged and neither is "
                "a 'done' item waiting on you — though a 'done' item you ruled "
                "verdict=fail on and left open counts again, since that hands the retry "
                "back to the worker. Answers 'no_ledger' when this session owns "
                "none yet."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "work_ledger_record",
            "description": (
                "Write one conductor-owned field set on your own ledger. One action per "
                "call, because the field sets are disjoint: 'goal' sets the goal and "
                "round (and opens the ledger); 'create' mints an item from title + "
                "acceptance; 'bind' attaches a worker session key to an item — do this "
                "BEFORE seeding that session, so the worker never starts unbound; "
                "'decide' records what you decided and why (the one field a worker reads "
                "as an instruction); 'verdict' records accept_eval.py's verdict and the "
                "fail count; 'accept' promotes a worker's claimed pr into the item's "
                "acceptance once you have checked it; 'close' stamps a terminal state. "
                "Caps refuse rather than truncate, naming the field."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "create",
                            "bind",
                            "decide",
                            "verdict",
                            "close",
                            "goal",
                            "accept",
                        ],
                        "description": "Which write to perform.",
                    },
                    "item_id": {
                        "type": "string",
                        "description": (
                            "The server-minted it_<8 hex> id from a create, for every "
                            "action but create and goal."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": "create: what the item is, <= 200 chars.",
                    },
                    "acceptance": {
                        "type": "object",
                        "description": (
                            "create / accept: the accept_eval.py condition object, stored "
                            'verbatim — e.g. {"kind": "pr_checks", "pr": 123, '
                            '"repo": "owner/name"}.'
                        ),
                    },
                    "worker_session_key": {
                        "type": "string",
                        "description": "bind: the worker session's key from session_create.",
                    },
                    "decision": {
                        "type": "string",
                        "description": (
                            "decide / close: what you decided and why, <= 2000 chars. The "
                            "worker reads this as an instruction."
                        ),
                    },
                    "verdict": {
                        "type": "string",
                        "enum": ["pass", "fail", "pending", "refused", "error"],
                        "description": "verdict: accept_eval.py's own five-value answer.",
                    },
                    "state": {
                        "type": "string",
                        "enum": ["accepted", "rejected", "abandoned"],
                        "description": "close: the terminal disposition.",
                    },
                    "goal": {
                        "type": "string",
                        "description": "goal: the goal this ledger serves, <= 2000 chars.",
                    },
                    "round": {
                        "type": "integer",
                        "description": "goal / decide / create: the patrol round counter.",
                    },
                    "fails": {
                        "type": "integer",
                        "description": "verdict: acceptance attempts that came back fail.",
                    },
                },
                "required": ["action"],
            },
        },
    ]


def _list_tools() -> list[dict[str, Any]]:
    """The tool surface, unconditionally.

    Reaching this process at all means an agent spec referenced this server, so
    the assignment already happened. What a caller may DO with a tool is decided
    by what it resolves to at call time, not by hiding half the list: a
    second-level conductor legitimately reaches all four, and a list that varied
    by identity would make a worker's missing conductor tools look like a broken
    install rather than a refusal it can read.
    """
    return _tool_definitions()


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate tool arguments against schema. Returns cleaned args."""
    schema = MCP_WORK_SCHEMAS.get(name)
    if schema:
        return validate_tool_args(args, schema)
    return args


def _strict_caller() -> tuple[str, str]:
    """Resolve the calling session strictly, refusing PID-walked identities.

    Returns ``(key, "")`` or ``("", error)``. The lenient resolver walks ``/proc``
    ancestors and a spawned subagent lives under its parent slot's process tree,
    so the walk would hand a subagent its parent's identity — letting it read the
    parent's brief or report against the parent's item. Both halves of this server
    send THIS key rather than resolving their own, so the identity that passed the
    gate is the identity on the wire.
    """
    return require_strict_session_key(
        "Error: this session's identity could not be verified strictly, so the work "
        "ledger is not reachable from here. A subagent inherits no session identity "
        "of its own — call the work tools from the dispatched session itself.",
        server=SERVER_NAME,
    )


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    """Dispatch one validated tool call."""
    if name not in WORK_TOOLS:
        return f"Error: unknown tool '{name}'"

    caller_key, strict_err = _strict_caller()
    if not caller_key:
        return strict_err

    if name == "work_brief":
        resp = _get(_BRIEF_PATH, session_key=caller_key)
        if resp.get("error"):
            return _refusal("could not read your work brief", resp)
        brief = resp.get("brief") or {}
        # Redacted on the way OUT as well as in: the brief carries the conductor's
        # own `decision` prose and this worker's last summary, and a resume reads
        # both back into context.
        return redact(json.dumps(brief, indent=2, ensure_ascii=False))

    if name == "work_report":
        payload = {k: v for k, v in args.items() if k in ("status", "summary", "artifacts", "pr")}
        resp = _post(_REPORT_PATH, payload, session_key=caller_key)
        if resp.get("error"):
            return _refusal("could not record your report", resp)
        return (
            f"Recorded. status={resp.get('status') or '(unset)'} "
            f"item={resp.get('item_id') or '(unknown)'}"
        )

    if name == "work_ledger_read":
        resp = _get(_READ_PATH, session_key=caller_key)
        if resp.get("error"):
            return _refusal("could not read your work ledger", resp)
        # The ledger holds worker-authored prose written from untrusted work, and a
        # patrol cycle re-reads it into context every round.
        return redact(json.dumps(resp, indent=2, ensure_ascii=False))

    if name == "work_ledger_record":
        payload = {k: v for k, v in args.items() if k in _RECORD_FIELDS}
        resp = _post(_RECORD_PATH, payload, session_key=caller_key)
        if resp.get("error"):
            return _refusal("could not write the work ledger", resp)
        item = resp.get("item") or {}
        action = resp.get("action") or payload.get("action") or ""
        if item:
            return (
                f"Recorded {action}. item={item.get('item_id') or '(unknown)'} "
                f"state={item.get('state') or '(unset)'} "
                f"verdict={item.get('verdict') or '(none)'}"
            )
        conductor = resp.get("conductor") or {}
        return f"Recorded {action}. round={conductor.get('round', '(unset)')}"

    return f"Error: unknown tool '{name}'"  # pragma: no cover - guarded above


def _refusal(what: str, resp: dict[str, Any]) -> str:
    """One error string, carrying the store's machine-readable code when there is one.

    The code is what a conductor or worker dispatches on — ``not_bound`` means
    "you are not a worker", ``no_ledger`` means "open one first", ``item_closed``
    means "stop reporting" — so it is quoted rather than folded into prose.
    """
    code = resp.get("code")
    field = resp.get("field")
    detail = f"Error: {what}: {resp['error']}"
    if code:
        detail += f" [{code}"
        detail += f", field={field}]" if field else "]"
    return redact(detail)


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    """Guarded entry point — schema validation and SEL audit live in the wrapper."""
    return call_tool_with_logging(
        name,
        raw_args,
        _validate_args,
        _call_tool_inner,
        session_key=_resolve_session_key() or SERVER_NAME,
        downstream_service=SERVER_NAME,
    )


ADVERTISE_CALLER_IDENTITY = True


def run_mcp_server() -> None:
    """Run the MCP stdio server — reads JSON-RPC from stdin, writes to stdout."""
    run_mcp_stdio_loop(
        SERVER_NAME,
        SERVER_VERSION,
        _list_tools,
        _call_tool,
        advertise_caller_identity=ADVERTISE_CALLER_IDENTITY,
    )
