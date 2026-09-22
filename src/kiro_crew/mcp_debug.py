"""The debug MCP server — five questions about a running gateway, asked of the
process that owns the answer.

Debugging Kiro Crew without this means an agent writes ad-hoc shell: grep the
gateway log, cat the autonudge registry, stat ``.env``, run ``ps``, read
``/proc``. That fails in four ways this server is shaped around.

1. **Slow and blocked.** Every shell command passes the path gate, and under load
   its symlink resolver runs out of budget and refuses fail-closed. The refusal is
   not a match — the path was never judged — but the call is gone either way, and
   a pathless MCP tool can meet the same budget. An answer assembled from twenty
   shell calls has twenty chances to be refused; one tool call has one.
2. **The sandbox shows fakes.** Empty placeholders are mounted over fenced paths,
   so ``ls`` reports 0 entries whatever the host holds; ``/proc/<pid>/environ`` is
   unreadable from the sandbox; and kiro-cli labels a policy refusal "User denied
   tool execution", which reads as a human cancelling. An agent reasoning from
   those readings reaches confident wrong conclusions.
3. **No history.** The adaptive controller samples the host every cycle but only
   in memory, and stall enrichment snapshots only on a stall. "What was the host
   doing at 21:50:44" has no answer at all.
4. **Knowledge lives in memories and lessons** and gets re-derived, sometimes
   wrongly.

So: one tool = one debug question, answered by the gateway, with time as the axis
of every question. File layout is internal and may change; a tool signature is a
question plus a window.

**Read-only by construction, and structurally so.** There is no write tool and
none may be added: ``test/test_mcp_debug.py`` ratchets the tool set to exactly the
five names in :data:`TOOLS`, so a write tool cannot be slipped in later without a
test changing. Nothing here kills a process, rotates a log, or flips a flag —
``debug_processes`` reports the reaper's own orphan verdict and cannot act on it.

**No ``autoApprove`` key, and none may be added.** An autoApproved MCP tool is
approved inside kiro-cli and emits no permission request, so
``hooks.on_tool_call`` — the PreToolUse gate carrying the deny floor, the
sensitive-path check and the governance ceiling — is never reached for it. A
server whose whole purpose is reading host and cross-session state is not the
place to break that. Every call also goes through
:func:`~kiro_crew.mcp_shared.call_tool_with_logging`, so every read is validated
against its schema and lands in SEL.

**Separate opt-in server, not part of core.** ``kirocrew-core`` is in every
agent's spec, and kiro-cli reads ``tools/list`` once per session, so a tool listed
there spends context in every request of every session forever. Debugging a
gateway is something a person asks for on purpose. An agent gets these tools only
when its own kiro spec carries both the ``mcpServers`` entry and a matching
``@kirocrew-debug`` reference in ``tools``.

**Authorization lives in the ROUTES, not here.** Every tool is a thin proxy over a
dashboard route on loopback carrying ``X-Internal-Secret`` and
``X-Internal-Caller: kirocrew-debug`` (attached centrally by the ``mcp_core``
request helpers), and the route decides what the calling session may see. This
module shapes output and nothing else, which is why a reviewer looking for the
security argument should read ``dashboard/handlers/debug.py``. The rule it applies,
stated here so the two can be compared: the host-wide views (``gateway``,
``threads``, ``processes``, ``snapshots``) are for the OWNER's dashboard tab alone,
because they carry cross-session metadata — other sessions' titles, their
processes, Python stacks from a shared interpreter. The per-session view
(``refusals`` with ``session=self``) serves the caller and its spawn tree, the same
scope ``kirocrew-crew-log`` uses. Channel-linked, incognito, temporary and
unattended callers get no host-wide view at all: a channel-linked session's
conversation is a Slack or Telegram thread that several allow-listed people read,
so "one operator" does not reach it.

Identity posture: the strict resolver, never the lenient ``/proc`` walk. A
subagent lives under its spawner's process tree and the lenient walk would answer
the parent slot, filing a subagent's read under a session that read nothing — and
for a server this wide, the operator's record of WHICH session read host state is
the point of auditing it at all.

Three of the five tools read ``kiro_crew.diag`` through their routes. Where a
build does not carry that module the routes answer HTTP 501 and these tools relay
that refusal VERBATIM rather than dressing it up: an agent must be able to tell
"this build cannot answer" from "the answer is nothing", and a synthesized empty
result would read as the second.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode

from kiro_crew.mcp_core import _get, _resolve_session_key, require_strict_session_key
from kiro_crew.mcp_shared import call_tool_with_logging, run_mcp_stdio_loop
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import MCP_DEBUG_SCHEMAS, validate_tool_args

logger = logging.getLogger(__name__)

SERVER_NAME = "kirocrew-debug"
SERVER_VERSION = "1.0.0"

#: The whole tool set, in one place because the ratchet test, the ``tools/list``
#: builder and the dispatcher must agree on it. Every name here is a READ, and
#: ``test/test_mcp_debug.py`` pins this tuple exactly — adding a sixth name is a
#: test change, which is the point.
TOOLS: tuple[str, ...] = (
    "debug_gateway",
    "debug_refusals",
    "debug_threads",
    "debug_processes",
    "debug_snapshots",
)

#: The route prefix every tool proxies. One prefix, so the strict-transport entry
#: in ``dashboard/server._STRICT_INTERNAL_API_PATHS`` is a single line and a route
#: added later cannot land outside it by omission.
ROUTE_PREFIX = "/api/debug"

#: Bytes one tool result may occupy. A process roster or a thread ledger on a busy
#: host is large, and a tool result is context: past this the payload is cut and a
#: cursor carries the rest, because half a JSON document is not a shorter answer,
#: it is an unparseable one.
MAX_OUTPUT_BYTES = 64 * 1024

#: The refusal the three diag-dependent routes answer with until the recorder and
#: the process-tree modules land. Spelled here as the exact contract so the ratchet
#: test can pin the server's relay and the route's body to the same string.
DIAG_UNAVAILABLE = "diag not available in this build"

#: The code the relay attaches to that refusal. The route's body carries only
#: ``error`` (the module contract fixes its shape), so the code is added here where
#: "this is the diag gap, not a transport fault" is known. An agent branches on the
#: code: ``diag_unavailable`` means wait for a build, ``unavailable`` means nothing
#: answered, ``forbidden`` means stop asking.
DIAG_UNAVAILABLE_CODE = "diag_unavailable"

#: The literal a caller passes to ``debug_refusals`` to mean "my own session".
SELF = "self"

#: Seconds of on-demand stack sampling one call may ask for. Restates the sampler's
#: own ceiling so a caller learns the bound from the schema rather than from a
#: refusal; the route clamps independently, which is what actually enforces it.
MAX_SAMPLE_SECONDS = 60


def _tool_definitions() -> list[dict[str, Any]]:
    """The tool surface this server advertises.

    Advertised UNCONDITIONALLY, including while the recorder is off and while the
    diag modules are absent. An agent must learn a subsystem's state from a
    refusal that says what to do about it, not from a tool that is not there: a
    missing tool reads as "Kiro Crew cannot do this", which is a different and
    wrong conclusion.
    """
    return [
        {
            "name": "debug_gateway",
            "description": (
                "WHICH CODE IS RUNNING — the first question to ask when a fix seems "
                "not to have landed. Returns the gateway's pid and start time, the "
                "code fingerprint of the kiro_crew package it actually imported, "
                "HEAD's commit time beside that start time (a gateway older than "
                "your commit is running the previous revision), whether the MCP "
                "gateway daemon runs the same fingerprint (a daemon that outlived a "
                "code change keeps handing out backends built from the old "
                "checkout, and the symptom is remote from the cause), the systemd "
                "drop-in names in effect, and the diagnostic recorder's health. Ask "
                "this BEFORE concluding a change did not work. READ-ONLY."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "debug_refusals",
            "description": (
                "WHY WAS I REFUSED — the tool to reach for when a call came back "
                "'User denied tool execution', which on kiro-cli is the wording for "
                "a POLICY refusal and not a human cancelling. Each refusal is "
                "classed: 'sensitive_path_match' (a real match against the "
                "protected list), 'unverifiable_path' (the symlink resolver ran out "
                "of budget, so the path was refused WITHOUT being judged — "
                "transient, retry the identical call), 'denied_rule' (a deny rule, "
                "carrying its refusal-diagnostic id), 'governance' (a profile "
                "ceiling) and 'tool_policy_timeout'. Filters: 'session' ('self' for "
                "your own session and the sessions you spawned), 'since' and "
                "'last'. Read from the security event log, which is the gateway's "
                "own record — so this answers for calls your session never saw. "
                "READ-ONLY."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "description": (
                            "'self' for your own session and its spawn tree, or a "
                            "session key. Omit for your own. A host-wide view is "
                            "the owner's dashboard tab only."
                        ),
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Keep refusals at or after this time. An ISO 8601 "
                            "timestamp, or a relative window like '30m' or '2h'."
                        ),
                    },
                    "last": {
                        "type": "integer",
                        "description": "Return at most this many refusals, newest first.",
                    },
                },
            },
        },
        {
            "name": "debug_threads",
            "description": (
                "GIL AND THREAD STATE — whether the interpreter is contended, and by "
                "what. mode='now' returns the per-thread ledger (state, cpu delta, "
                "run-queue wait, context switches, top Python frame) plus the "
                "probe's GIL-wait distribution; mode='sample' profiles for "
                f"'seconds' at 'hz' (max {MAX_SAMPLE_SECONDS}s, one at a time) and "
                "returns folded stacks; mode='dumps' lists the loop-watchdog's "
                "faulthandler dumps and reads one with 'read=<name>', scrubbed. "
                "Read the interpretation the output ships with: high run-queue wait "
                "means CPU contention (the host is busy), while low run-queue wait "
                "beside high GIL wait means GIL contention. CPython exposes no "
                "'who holds the GIL' API, so these are corroborating signals and "
                "only deep sampling is exact — the output says which it gave you. "
                "READ-ONLY."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["now", "sample", "dumps"],
                        "description": "Which reading to take. Default 'now'.",
                    },
                    "seconds": {
                        "type": "number",
                        "description": f"mode='sample': how long to sample, up to {MAX_SAMPLE_SECONDS}.",
                    },
                    "hz": {
                        "type": "integer",
                        "description": "mode='sample': samples per second.",
                    },
                    "deep": {
                        "type": "boolean",
                        "description": (
                            "mode='sample': use py-spy with --gil for an exact "
                            "reading. Only when py-spy is on PATH; a refusal "
                            "explains itself and no privilege is widened to get it."
                        ),
                    },
                    "read": {
                        "type": "string",
                        "description": "mode='dumps': read this dump by name instead of listing.",
                    },
                },
            },
        },
        {
            "name": "debug_processes",
            "description": (
                "THE PROCESS TREE — every process in the gateway's family, including "
                "the ones reparented to pid 1, which are the orphans. Per node: pid, "
                "ppid, kind (gateway / chat / subagent / cron / mcp-server / pod / "
                "browser / test), owning session, age, cpu %, run-queue wait, rss, "
                "thread states, fds, cwd. The orphan verdict is computed by the SAME "
                "function the reaper uses, so this view and the reaper never "
                "disagree. A process pinned near one core with several threads in "
                "futex wait is flagged 'gil_saturated_hint' — a kernel-level hint, "
                "labelled as one. Use it to find what is eating the host when "
                "everything is slow. READ-ONLY: there is no kill here, and killing "
                "stays with the reaper."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "format": {
                        "type": "string",
                        "enum": ["tree", "flat"],
                        "description": "Shape of the result. Default 'tree'.",
                    },
                    "kind": {"type": "string", "description": "Keep only this kind of process."},
                    "owner": {
                        "type": "string",
                        "description": "Keep only processes owned by this session.",
                    },
                    "orphan_only": {
                        "type": "boolean",
                        "description": "Keep only processes the reaper would call orphaned.",
                    },
                    "include_env": {
                        "type": "boolean",
                        "description": (
                            "Include the four allow-listed environment keys "
                            "(KIROCREW_HOME, KIROCREW_POD_ROOT, TMPDIR, "
                            "KIROCREW_SCRATCH) and nothing else. The gateway can "
                            "read same-uid /proc/<pid>/environ; your sandbox cannot."
                        ),
                    },
                },
            },
        },
        {
            "name": "debug_snapshots",
            "description": (
                "WHAT HAPPENED AROUND T — the recorded host and gateway series, which "
                "is the only way to answer a question about a moment that has "
                "passed. Ask with 'around' plus 'radius', or 'since' plus 'until'. "
                "Returns the sampled series, the thicker EVENT rows (gateway "
                "start/stop, loop stall, adaptive cap lowered, memory posture "
                "change, a config file rewritten, threshold crossings, process "
                "bursts, an orphan appearing) and min/max/avg per field. "
                "'fields' narrows the series; 'events_only' drops it. This is what "
                "makes '.env went to 0 bytes at 21:50:44 — what was the host doing' "
                "answerable at all. READ-ONLY."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "around": {
                        "type": "string",
                        "description": "Centre the window on this time (ISO 8601).",
                    },
                    "radius": {
                        "type": "string",
                        "description": "Half-width around 'around', e.g. '5m'.",
                    },
                    "since": {"type": "string", "description": "Window start (ISO 8601)."},
                    "until": {"type": "string", "description": "Window end (ISO 8601)."},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Keep only these series fields.",
                    },
                    "events_only": {
                        "type": "boolean",
                        "description": "Return only event rows, no series.",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Continue a result that was cut to fit.",
                    },
                },
            },
        },
    ]


def _list_tools() -> list[dict[str, Any]]:
    """The tool surface, unconditionally.

    Reaching this process at all means an agent spec referenced this server, so
    the assignment already happened; there is nothing left to gate here.
    """
    return _tool_definitions()


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate tool arguments against schema. Returns cleaned args."""
    schema = MCP_DEBUG_SCHEMAS.get(name)
    if schema:
        return validate_tool_args(args, schema)
    return args


def _error(code: str, message: str) -> str:
    """A typed refusal, never a traceback.

    One shape for every failure this server reports, because an agent acting on
    the answer needs the CODE to branch on: ``forbidden`` means stop asking,
    ``diag_unavailable`` means this build cannot answer yet, ``unavailable`` means
    nothing answered. Prose alone makes every failure look like the same dead end.
    """
    return redact(json.dumps({"error": {"code": code, "message": message}}, indent=2))


def _proxy_error(payload: dict[str, Any], *, fallback: str) -> str:
    """The route's own refusal, carried through with its code intact.

    The route owns the invariants and the authorization, so its code is the
    authoritative one and re-deriving a code here would let the two disagree.

    Two cases get special handling for the same reason — an agent must be able to
    tell them apart. The diag gap is relayed VERBATIM under
    :data:`DIAG_UNAVAILABLE_CODE`, because "wait for a build" is a different action
    from anything else on this list. A transport failure has no code of its own and
    becomes ``unavailable``: the distinction that matters is "Kiro Crew said no"
    versus "nothing answered".
    """
    message = str(payload.get("error") or fallback)
    if message == DIAG_UNAVAILABLE:
        return _error(DIAG_UNAVAILABLE_CODE, DIAG_UNAVAILABLE)
    code = str(payload.get("code") or "")
    return _error(code or "unavailable", message)


def _caller_key() -> tuple[str, str]:
    """``(key, "")`` for the strictly-resolved calling session, else ``("", refusal)``.

    EVERY request this server makes carries this key. The route requires a strict
    session identity to admit a read at all and records that identity against the
    read, so a leniently-resolved key is not a cosmetic difference: the lenient
    resolver walks ``/proc`` ancestors, a subagent lives under its spawner's
    process tree, and the spawner is commonly a dashboard tab. A request that let
    the helper resolve its own key would file a subagent's read of host state
    under its parent slot, leaving the operator's record naming a session that read
    nothing. Resolving once here, strictly, and sending that key on every leg is
    what makes the identity that was checked the identity that is used and the
    identity that is audited.

    Fails closed: a caller the gateway cannot name reads nothing at all, not even
    its own refusals, because "its own refusals" is derived from this same key.
    """
    key, strict_err = require_strict_session_key(
        "Error: this session cannot be identified well enough to read debug state. "
        "Every read is scoped to the calling session, and only a gateway-issued "
        "key counts.",
        server=SERVER_NAME,
    )
    if not key:
        return "", _error("forbidden", strict_err)
    return key, ""


def _fit(payload: dict[str, Any]) -> str:
    """*payload* rendered within :data:`MAX_OUTPUT_BYTES`, cut at a line boundary.

    The route caps its own body and hands back a cursor, so this is the second
    belt rather than the only one: a payload that still does not fit — a roster
    from a host under real load — is cut HERE and says so, because a tool result
    that silently exceeds the budget costs the caller its context and a tool
    result cut mid-string costs it the parse.

    Cut by LINES of the rendered JSON, never by bytes of it: the marker replaces
    the tail with a valid closing shape, so what comes back always parses. The
    route's own ``cursor`` is preserved when it set one; when the cut happens here
    the caller is told to narrow the question instead, because this server cannot
    mint a cursor into a series it did not page.
    """
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    if len(rendered.encode("utf-8")) <= MAX_OUTPUT_BYTES:
        return redact(rendered)
    keep = dict(payload)
    keep["truncated_to_fit"] = True
    keep["truncated_hint"] = (
        "this result exceeded the 64 KB budget and was cut; narrow it with a "
        "shorter window, a 'fields' list, or a filter"
    )
    for large in ("series", "events", "nodes", "threads", "refusals", "stacks"):
        rows = keep.get(large)
        if not isinstance(rows, list) or not rows:
            continue
        while rows and len(json.dumps(keep, indent=2, ensure_ascii=False).encode("utf-8")) > (
            MAX_OUTPUT_BYTES
        ):
            # Halve rather than step: one serialization per row would make the
            # loop's cost proportional to the very size it is bounding.
            rows = rows[: max(0, len(rows) // 2)]
            keep[large] = rows
            keep[f"{large}_returned"] = len(rows)
    rendered = json.dumps(keep, indent=2, ensure_ascii=False)
    if len(rendered.encode("utf-8")) <= MAX_OUTPUT_BYTES:
        logger.debug("debug result cut to fit the %d byte budget", MAX_OUTPUT_BYTES)
        return redact(rendered)
    # Nothing structural left to drop and still over budget: answer with the
    # envelope alone rather than an unbounded body. A caller gets a parseable
    # result that says it was cut, which is the one thing it can act on.
    return redact(
        json.dumps(
            {
                "truncated_to_fit": True,
                "truncated_hint": keep["truncated_hint"],
                "error": {
                    "code": "too_large",
                    "message": "the answer does not fit in one result; narrow the question",
                },
            },
            indent=2,
        )
    )


def _read(route: str, query: dict[str, Any], caller_key: str, *, fallback: str) -> str:
    """GET one debug route and shape its answer. The single proxy leg.

    One function for all five tools, because the transport, the refusal handling
    and the size cap are identical for every question and a second copy is how two
    tools come to disagree about what a refusal means.
    """
    path = f"{ROUTE_PREFIX}/{route}"
    if query:
        path = f"{path}?{urlencode(query, doseq=True)}"
    payload = _get(path, session_key=caller_key)
    # An empty mapping is a TRANSPORT failure, not an empty answer. Every one of
    # these five routes answers with at least one field on success -- there is no
    # such thing as a gateway that describes itself with zero keys -- so a payload
    # with nothing in it means the request never reached a handler. Reporting it as
    # a successful empty result would tell the caller "the host has nothing to
    # report", which is the opposite of "nothing answered".
    if not isinstance(payload, dict) or not payload or payload.get("error"):
        return _proxy_error(payload if isinstance(payload, dict) else {}, fallback=fallback)
    return _fit(payload)


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    """Dispatch one validated tool call.

    Identity is resolved ONCE, here, for every tool rather than per request leg:
    the route scopes each read on the key it is handed, so one call must not be
    able to present two.
    """
    if name not in TOOLS:
        return _error("unknown_tool", f"unknown tool: {name}")
    caller_key, refusal = _caller_key()
    if refusal:
        return refusal
    if name == "debug_gateway":
        return _read("gateway", {}, caller_key, fallback="the gateway could not describe itself")
    if name == "debug_refusals":
        return _read(
            "refusals", _refusal_query(args), caller_key, fallback="refusals could not be read"
        )
    if name == "debug_threads":
        return _read(
            "threads", _thread_query(args), caller_key, fallback="thread state could not be read"
        )
    if name == "debug_processes":
        return _read(
            "processes",
            _process_query(args),
            caller_key,
            fallback="the process tree could not be read",
        )
    return _read(
        "snapshots", _snapshot_query(args), caller_key, fallback="snapshots could not be read"
    )


def _refusal_query(args: dict[str, Any]) -> dict[str, Any]:
    """``debug_refusals`` arguments as query parameters.

    ``session`` defaults to :data:`SELF` rather than to "everything". The wide
    view is the owner's alone and the route enforces that, but a tool whose
    default asks for it would make every ordinary caller's first call a refusal.
    """
    query: dict[str, Any] = {"session": str(args.get("session") or SELF)}
    if args.get("since"):
        query["since"] = str(args["since"])
    if args.get("last"):
        query["last"] = int(args["last"])
    return query


def _thread_query(args: dict[str, Any]) -> dict[str, Any]:
    """``debug_threads`` arguments as query parameters."""
    query: dict[str, Any] = {"mode": str(args.get("mode") or "now")}
    if args.get("seconds") is not None:
        query["seconds"] = float(args["seconds"])
    if args.get("hz") is not None:
        query["hz"] = int(args["hz"])
    if args.get("deep"):
        query["deep"] = "1"
    if args.get("read"):
        query["read"] = str(args["read"])
    return query


def _process_query(args: dict[str, Any]) -> dict[str, Any]:
    """``debug_processes`` arguments as query parameters."""
    query: dict[str, Any] = {"format": str(args.get("format") or "tree")}
    if args.get("kind"):
        query["kind"] = str(args["kind"])
    if args.get("owner"):
        query["owner"] = str(args["owner"])
    if args.get("orphan_only"):
        query["orphan_only"] = "1"
    if args.get("include_env"):
        query["include_env"] = "1"
    return query


def _snapshot_query(args: dict[str, Any]) -> dict[str, Any]:
    """``debug_snapshots`` arguments as query parameters."""
    query: dict[str, Any] = {}
    for key in ("around", "radius", "since", "until", "cursor"):
        if args.get(key):
            query[key] = str(args[key])
    fields = args.get("fields")
    if isinstance(fields, list) and fields:
        query["fields"] = [str(f) for f in fields]
    if args.get("events_only"):
        query["events_only"] = "1"
    return query


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


#: Whether this server advertises ``kirocrew.caller-identity`` — i.e. whether it
#: consumes the per-call caller block gatewayd injects instead of reading identity
#: from its own process. True here because it does: every tool resolves through
#: :func:`~kiro_crew.mcp_core.require_strict_session_key`, whose first source is
#: that block, and the route scopes every read on the key this server forwards.
#:
#: Advertising is not cosmetic. ``mcp_gateway/backend.py`` strips any client-forged
#: caller block from EVERY forwarded request and re-injects its own only when the
#: backend advertised this capability — so without the advertisement the block
#: never arrives and a pooled backend would resolve an empty identity for every
#: caller. For this server that fails CLOSED (an unnamed session is refused at the
#: route), so the failure mode is tools that stop working rather than an
#: unattributed read of host state; the advertisement is what makes them work on
#: the topology pooling was built to serve.
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
