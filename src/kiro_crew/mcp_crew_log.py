"""The crew-log MCP server — the agent's sanctioned, read-only door to the crew log.

Deliberately NOT part of ``kirocrew-core``, for the reason
:mod:`kiro_crew.mcp_dashboard` states at length: core is the surface EVERY session
carries, kiro-cli reads ``tools/list`` once per session, so a tool listed there
spends context in every request of every session forever. Reading a crew log is
something a person asks for on purpose — verifying the log while developing it,
auditing what a session did — and it does not belong in the always-on surface.

So this is its own server, and an ASSIGNABLE SET: an agent gets these tools only
when its own kiro spec carries both the ``mcpServers`` entry and a matching
``@kirocrew-crew-log`` reference in ``tools``. The default agent's spec carries
neither, so a default session pays nothing for a capability it never uses.

**Why the door exists at all.** The crew log is fenced from the agent by design —
the sandbox hides the leaf and ``security._CREW_SECRET_LEAVES`` fences the agent's
file tools — so that a session cannot alter its own audit trail. That stays, and
nothing here changes it. What the fence also did was make the log unverifiable by
anyone except the operator running ``jq`` in their own shell: every check of a
feature written against the crew log was a round trip through a human. A read-only
door removes that without touching integrity, because reading was never the thing
the fence protected.

**Read-only, and structurally so.** There is no write tool, and none may be added:
``test/test_mcp_crew_log.py`` ratchets the tool set to exactly the three names
below, so a write tool cannot be slipped in later without a test changing. A
capability that writes to the log belongs to the gateway, which is the log's only
writer.

**Authorization lives in the ENDPOINTS, not here.** Every tool is a thin proxy over
a dashboard route on loopback carrying ``X-Internal-Secret`` and
``X-Internal-Caller: kirocrew-crew-log`` (attached centrally by the ``mcp_core``
request helpers), and the route decides what the calling session may see:
the caller's OWN unit needs only a strict session identity, and any wider read
needs the owner at a dashboard tab — not an app-owned session, not a cron or a
subagent, not a channel conversation, and not an incognito or temporary session.
This module shapes output and nothing else, which is why a reviewer looking for
the security argument should read ``dashboard/handlers/crew_log.py``.

**What it grants, in comparison.** ``session_read_message`` in
``kirocrew-dashboard`` already returns a peer session's full transcript to an agent
the owner granted it, and a crew log carries full message bodies too: ``message/*``
entries record the text itself. So this is NOT a thinner read, and the whole of the
justification is the gate above, which is stricter. ``docs/reference/crew-log/
reading-from-an-agent.md`` carries that argument in full.

Identity posture: the strict resolver, never the lenient ``/proc`` walk. ``self``
is resolved through :func:`~kiro_crew.mcp_core.require_strict_session_key` and the
key it returns is the key sent on the wire, so the identity that was checked is the
identity that is used — under a pooled backend the lenient walk can answer a parent
slot, and a subagent must not read its parent's log as though it were its own.
"""

from __future__ import annotations

import json
import logging
import re as _re
from typing import Any
from urllib.parse import quote, urlencode

from kiro_crew.mcp_core import _get, _resolve_session_key, require_strict_session_key
from kiro_crew.mcp_shared import call_tool_with_logging, run_mcp_stdio_loop
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import MCP_CREW_LOG_SCHEMAS, validate_tool_args

logger = logging.getLogger(__name__)

SERVER_NAME = "kirocrew-crew-log"
SERVER_VERSION = "1.0.0"

#: The whole tool set, in one place because the ratchet test, the ``tools/list``
#: builder and the dispatcher must agree on it. Every name here is a READ.
TOOLS: tuple[str, ...] = ("crew_log_list", "crew_log_read", "crew_log_projection")

#: The folds :mod:`kiro_crew.crew_log.projection` serves, named here so the tool
#: description lists them without importing the storage package into this process.
#: A name this list is missing is refused by the ENDPOINT, which reads the real
#: ``PROJECTION_NAMES``; a test pins the two together.
PROJECTION_NAMES: tuple[str, ...] = ("status", "usage", "timeline", "tools", "approvals")

#: Entries one ``crew_log_read`` returns, whatever a caller asks for. The endpoint
#: clamps its own span too; this is the tool's promise to its caller.
MAX_READ_LIMIT = 200

#: Bytes one ``crew_log_read`` result may occupy. A crew log holds message bodies,
#: so a whole file is megabytes and a tool result is context: past this the rows
#: are cut at a ROW boundary and ``next_from`` carries the rest, because half a
#: JSON row is not a shorter answer, it is an unparseable one.
MAX_READ_BYTES = 64 * 1024

#: Characters of one entry's ``data`` a page keeps. A caller wanting one row whole
#: asks for it by seq with ``full=True``, which is cheap and explicit; trimming by
#: default is what keeps a 100-row page from being the whole conversation.
MAX_DATA_CHARS = 400

#: The literal a caller passes to mean "the unit my own work is landing in".
SELF = "self"

#: What makes a ``unit`` argument a session KEY rather than a raw unit id: a
#: namespaced key always carries a colon (``dashboard:``, ``slack:``, ``subagent:``
#: all do), and a bare dashboard slot name is ``chat-<n>-<ts>``. Anything else is
#: handed to the endpoint as a raw unit id. Stated as a rule rather than probed by
#: trying both, so one argument cannot silently mean two different units.
_SLOT_KEY_RE = _re.compile(r"^(?:[a-z][a-z0-9_]*:|chat-\d)")


def _tool_definitions() -> list[dict[str, Any]]:
    """The tool surface this server advertises.

    Advertised UNCONDITIONALLY, including while ``KIROCREW_CREW_LOG`` is off. An
    agent must learn the flag state from a refusal that says how to switch it on,
    not from a tool that is not there: a missing tool reads as "Kiro Crew cannot do
    this", which is a different and wrong conclusion.
    """
    return [
        {
            "name": "crew_log_list",
            "description": (
                "List the CREW LOGS on this host, newest first — one row per "
                "session, carrying its unit id, slot, agent, model, first and last "
                "entry time, last seq, and whether the session is still open. Use "
                "it to find the unit you want to read when you do not already hold "
                "an id, and to see at a glance which sessions wrote anything. "
                "``with_type_counts=True`` adds the per-entry-type histogram, "
                "globally and per session — the counts you would otherwise get by "
                "running jq over the files by hand. ``slot_contains`` filters on "
                "the slot the session is bound to, and ``active_within_secs`` keeps "
                "only sessions written to within that window. The result reports "
                "``scanned`` and ``truncated``, so a cut list cannot be mistaken "
                "for a short one. READ-ONLY, like every tool on this server."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slot_contains": {
                        "type": "string",
                        "description": "Keep only sessions whose slot contains this substring.",
                    },
                    "active_within_secs": {
                        "type": "integer",
                        "description": (
                            "Keep only sessions written to within this many seconds. "
                            "Omit for no window."
                        ),
                    },
                    "with_type_counts": {
                        "type": "boolean",
                        "description": (
                            "Add the per-entry-type histogram per session and across "
                            "the listing. Costs a full read of each listed log."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Rows to return, 1-{MAX_READ_LIMIT}. Default 50.",
                    },
                },
            },
        },
        {
            "name": "crew_log_read",
            "description": (
                "Read a range of entries from one crew log, oldest first, as compact "
                "rows {seq, ts, type, data} with each entry's citation resolved to "
                "its verdict and span. ``unit`` is a raw session id, OR a session "
                "key the gateway resolves for you (a dashboard slot like "
                "'chat-1533-1789617503', or a namespaced key like "
                "'dashboard:chat-1533-1789617503'), OR the literal 'self' for the "
                "unit your OWN session's work is landing in — which is the form to "
                "use when verifying that something you just did was recorded. "
                "``types`` filters to exact entry types and ``since_ts`` drops older "
                "rows, both applied after the range is read. Each row's ``data`` is "
                "trimmed to a few hundred characters; to see one row whole, ask for "
                "it alone with from_seq=<seq>, limit=1, full=True. The result "
                "carries ``next_from`` whenever more follows, including when the "
                "page was cut to fit — so page with it rather than raising "
                "``limit``. READ-ONLY: this server has no way to write to a crew "
                "log, and the gateway remains its only writer."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "unit": {
                        "type": "string",
                        "description": ("A raw unit id, a session/slot key to resolve, or 'self'."),
                    },
                    "from_seq": {
                        "type": "integer",
                        "description": "First seq to return, 1-based. Default 1.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Entries to return, 1-{MAX_READ_LIMIT}. Default 100.",
                    },
                    "types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Keep only these exact entry types.",
                    },
                    "since_ts": {
                        "type": "integer",
                        "description": "Drop entries older than this epoch-millisecond time.",
                    },
                    "full": {
                        "type": "boolean",
                        "description": (
                            "Return each row's data untrimmed. Intended for a single "
                            "row (limit=1); a full page can exceed the size cap and "
                            "be cut."
                        ),
                    },
                },
                "required": ["unit"],
            },
        },
        {
            "name": "crew_log_projection",
            "description": (
                "Read one FOLD over a crew log and the seq it was folded through — "
                "the same values the session side panel shows, computed by the "
                "gateway rather than by you. 'status' is lifecycle, agent, model and "
                "the open turn; 'usage' is tokens, credits, injected context and "
                "compactions; 'timeline' is the recent moments; 'tools' is per-tool "
                "counts and outcomes; 'approvals' is what was asked and what was "
                "answered. Prefer this over reading entries when the question is "
                "'what did this session spend' or 'is it still running' — one fold "
                "answers it without paging the file. ``unit`` takes the same forms "
                "as crew_log_read, including 'self'. READ-ONLY."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "unit": {
                        "type": "string",
                        "description": ("A raw unit id, a session/slot key to resolve, or 'self'."),
                    },
                    "name": {
                        "type": "string",
                        "enum": list(PROJECTION_NAMES),
                        "description": "Which fold to read.",
                    },
                },
                "required": ["unit", "name"],
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
    schema = MCP_CREW_LOG_SCHEMAS.get(name)
    if schema:
        return validate_tool_args(args, schema)
    return args


def _error(code: str, message: str) -> str:
    """A typed refusal, never a traceback.

    One shape for every failure this server reports, because an agent acting on
    the answer needs the CODE to branch on: ``crew_log_disabled`` means switch a
    flag, ``forbidden`` means stop asking, ``unknown_unit`` means look the unit up
    again. Prose alone makes every failure look like the same dead end.
    """
    return redact(json.dumps({"error": {"code": code, "message": message}}, indent=2))


def _proxy_error(payload: dict[str, Any], *, fallback: str) -> str:
    """The endpoint's own refusal, carried through with its code intact.

    The route owns the invariants and the authorization, so its code is the
    authoritative one and re-deriving a code here would let the two disagree. A
    transport failure — the gateway not running, a socket refused — has no code of
    its own and becomes ``unavailable``: the distinction the agent needs is
    "Kiro Crew said no" versus "nothing answered".
    """
    code = str(payload.get("code") or "")
    message = str(payload.get("error") or fallback)
    # No code means the failure never reached a handler: the request helper reports
    # a transport fault as a bare ``{"error": ...}``. ``unavailable`` is the honest
    # label for that, and inventing a domain code here would tell the agent Kiro
    # Crew refused when in fact nothing answered.
    return _error(code or "unavailable", message)


def _caller_key() -> tuple[str, str]:
    """``(key, "")`` for the strictly-resolved calling session, else ``("", refusal)``.

    EVERY request this server makes carries this key, not just the ``self`` one.
    The endpoint decides what a caller may read FROM the key it is handed, so a
    leniently-resolved key is not a cosmetic difference there: the lenient
    resolver walks ``/proc`` ancestors, a subagent lives under its spawner's
    process tree, and the spawner is commonly the owner's own dashboard tab. A
    request that let the helper resolve its own key would therefore hand the
    endpoint an OWNER identity for a subagent's call, and the wider-read rule
    would admit it. Resolving once here, strictly, and sending that key on every
    leg is what makes the identity that was checked the identity that is used.

    Fails closed: a caller the gateway cannot name reads nothing at all, not even
    its own unit, because "its own unit" is derived from this same key.
    """
    key, strict_err = require_strict_session_key(
        "Error: this session cannot be identified well enough to read a crew log. "
        "Every read is scoped to the calling session, and only a gateway-issued "
        "key counts.",
        server=SERVER_NAME,
    )
    if not key:
        return "", _error("forbidden", strict_err)
    return key, ""


def _resolve_unit(raw: str, caller_key: str) -> tuple[str, str]:
    """``(unit, "")`` for *raw*, or ``("", error)``. The one place ``unit`` is read.

    Three forms, and the rule that separates them is stated rather than probed:

    * ``self`` — the unit the CALLING session's work is landing in, resolved from
      *caller_key* (which :func:`_caller_key` produced strictly).
    * a session KEY — anything carrying a namespace (a colon) or shaped like a
      bare dashboard slot (``chat-<n>-…``). Handed to the gateway's resolver,
      which answers the unit that key is landing in RIGHT NOW; a slot's unit
      changes on a reset, a compaction or an agent switch, so the answer is only
      valid at the moment it is asked.
    * anything else — a raw unit id, passed through untouched.

    Resolving somebody else's key is itself scoped by *caller_key*: the route
    decides on the CALLER, never on the key being looked up, so naming another
    session here cannot widen what this call may then read.
    """
    value = (raw or "").strip()
    if not value:
        return "", _error("unknown_unit", "unit is required")
    if value == SELF:
        return _resolve_key(caller_key, caller_key)
    if _SLOT_KEY_RE.match(value):
        return _resolve_key(value, caller_key)
    return value, ""


def _resolve_key(key: str, caller_key: str) -> tuple[str, str]:
    """``(unit, "")`` for a session key, or ``("", error)``."""
    path = f"/api/crew-log/resolve?{urlencode({'key': key})}"
    payload = _get(path, session_key=caller_key)
    unit = str(payload.get("unit") or "") if isinstance(payload, dict) else ""
    if unit:
        return unit, ""
    if isinstance(payload, dict) and (payload.get("error") or payload.get("code")):
        return "", _proxy_error(payload, fallback=f"{key!r} could not be resolved to a unit")
    return "", _error("unresolvable_key", f"{key!r} could not be resolved to a unit")


def _trim(value: Any, *, full: bool) -> Any:
    """One entry's ``data``, trimmed to :data:`MAX_DATA_CHARS` unless *full*.

    Serialized and cut as TEXT rather than pruned key by key, because what makes a
    row large is unpredictable — one long string, or many short ones — and a caller
    reading a trimmed row needs to see that it was trimmed, which the marker says
    and a silently dropped key does not.
    """
    if full:
        return value
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= MAX_DATA_CHARS:
        return value
    return {"_trimmed": f"{rendered[:MAX_DATA_CHARS]}… ({len(rendered)} chars)"}


def _compact_rows(entries: list[dict[str, Any]], *, full: bool) -> list[dict[str, Any]]:
    """The wire rows, in the shape this server promises: ``{seq, ts, type, data}``."""
    rows: list[dict[str, Any]] = []
    for entry in entries:
        row: dict[str, Any] = {
            "seq": entry.get("seq"),
            "ts": entry.get("time"),
            "type": entry.get("type"),
            "data": _trim(entry.get("data"), full=full),
        }
        if entry.get("src"):
            row["src"] = entry["src"]
        if entry.get("ref_resolution") is not None:
            row["ref"] = entry["ref_resolution"]
        elif entry.get("ref") is not None:
            # A citation the page did not resolve, reported rather than dropped:
            # the page's own ``refs_unresolved`` says how many, and a row that
            # silently lost its citation would read as an entry that never had one.
            row["ref"] = {"status": "unresolved", "cited": entry["ref"]}
        rows.append(row)
    return rows


def _fit(envelope: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    """*envelope* with as many *rows* as fit :data:`MAX_READ_BYTES`, cut at a row.

    ``next_from`` is rewritten when rows are dropped, so a caller that pages on it
    reads the whole log rather than stopping at the cut, and ``returned`` is
    rewritten with it: a body claiming 100 rows beside 40 of them is worse than a
    short page, because the count is what a caller checks against its request.
    Dropping from the END keeps the page contiguous from ``from_seq``, which is what
    makes ``next_from`` a single number rather than a set of holes.

    The cap is ABSOLUTE, including for one ``full=True`` row. Halving stops at a
    single row, and a single row can be larger than the whole budget on its own --
    a crew log carries message bodies -- so the last step trims that row's data to
    what is left and says so. Otherwise the one shape a caller reaches for to see a
    row whole would be the one shape that ignores the cap.
    """
    kept = list(rows)
    while True:
        body = dict(envelope)
        body["entries"] = kept
        if kept and len(kept) < len(rows):
            body["truncated_to_fit"] = True
            body["returned"] = len(kept)
            body["next_from"] = int(kept[-1]["seq"] or 0) + 1
        rendered = json.dumps(body, indent=2, ensure_ascii=False)
        if len(rendered.encode("utf-8")) <= MAX_READ_BYTES:
            if body.get("truncated_to_fit"):
                logger.debug("crew log page cut to fit: %d of %d rows", len(kept), len(rows))
            return redact(rendered)
        if len(kept) <= 1:
            return redact(_one_row_within_budget(body, kept))
        # Halve rather than step: a page of long rows would otherwise need one
        # serialization per row to converge, and the loop's whole job is to be
        # bounded.
        kept = kept[: max(1, len(kept) // 2)]


def _one_row_within_budget(body: dict[str, Any], kept: list[dict[str, Any]]) -> str:
    """*body* holding at most one row, trimmed until it fits :data:`MAX_READ_BYTES`.

    The row's ``data`` is what can be arbitrarily large, so that is what gives way;
    the envelope and the row's own identity (``seq``, ``ts``, ``type``) are what the
    caller needs to ask again more narrowly, and are never dropped. The trim states
    the original size, so an answer cut here is visibly cut.

    Converges by SHRINKING the character budget and re-rendering, never by slicing
    the rendered bytes: a byte slice through a JSON string produces a body no
    caller can parse, which is the failure this whole cap exists to avoid. Halving
    terminates in at most ~17 passes from a 64 KiB budget, and a budget that
    reaches zero drops the data to the marker alone, which is bounded by
    construction.
    """
    body = dict(body)
    body["truncated_to_fit"] = True
    if not kept:
        # No row at all and still over budget: cannot happen with the fields this
        # server sets, but answering with a valid empty page beats answering with
        # an unbounded one on a shape nobody predicted.
        body["entries"] = []
        body["returned"] = 0
        return json.dumps(body, indent=2, ensure_ascii=False)
    body["returned"] = 1
    original = json.dumps(kept[0].get("data"), ensure_ascii=False, separators=(",", ":"))
    budget = MAX_READ_BYTES
    while True:
        row = dict(kept[0])
        row["data"] = {"_trimmed": f"{original[:budget]}… ({len(original)} chars)"}
        body["entries"] = [row]
        rendered = json.dumps(body, indent=2, ensure_ascii=False)
        if len(rendered.encode("utf-8")) <= MAX_READ_BYTES or budget == 0:
            return rendered
        budget = budget // 2


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    """Dispatch one validated tool call.

    Identity is resolved ONCE, here, for every tool rather than per request leg:
    the endpoint scopes each read on the key it is handed, so one call must not be
    able to present two.
    """
    if name not in TOOLS:
        return _error("unknown_tool", f"unknown tool: {name}")
    caller_key, refusal = _caller_key()
    if refusal:
        return refusal
    if name == "crew_log_list":
        return _list(args, caller_key)
    if name == "crew_log_read":
        return _read(args, caller_key)
    return _projection(args, caller_key)


def _list(args: dict[str, Any], caller_key: str) -> str:
    query: dict[str, Any] = {}
    if args.get("slot_contains"):
        query["slot_contains"] = str(args["slot_contains"])
    if args.get("active_within_secs"):
        query["active_within_secs"] = int(args["active_within_secs"])
    if args.get("with_type_counts"):
        query["with_type_counts"] = "1"
    query["limit"] = min(int(args.get("limit") or 50), MAX_READ_LIMIT)
    payload = _get(f"/api/crew-log/sessions?{urlencode(query)}", session_key=caller_key)
    if not isinstance(payload, dict) or payload.get("error"):
        return _proxy_error(
            payload if isinstance(payload, dict) else {}, fallback="the listing could not be read"
        )
    return redact(json.dumps(payload, indent=2, ensure_ascii=False))


def _read(args: dict[str, Any], caller_key: str) -> str:
    unit, err = _resolve_unit(str(args.get("unit") or ""), caller_key)
    if err:
        return err
    from_seq = max(1, int(args.get("from_seq") or 1))
    limit = min(max(1, int(args.get("limit") or 100)), MAX_READ_LIMIT)
    full = bool(args.get("full"))
    query = {"from": from_seq, "to": from_seq + limit - 1}
    payload = _get(
        f"/api/crew-log/units/{quote(unit, safe='')}/page?{urlencode(query)}",
        session_key=caller_key,
    )
    if not isinstance(payload, dict) or payload.get("error"):
        return _proxy_error(
            payload if isinstance(payload, dict) else {},
            fallback=f"no crew log page for {unit!r}",
        )
    entries = [row for row in payload.get("entries", []) if isinstance(row, dict)]
    wanted = args.get("types")
    if isinstance(wanted, list) and wanted:
        keep = {str(t) for t in wanted}
        entries = [row for row in entries if str(row.get("type")) in keep]
    since_ts = args.get("since_ts")
    if isinstance(since_ts, int) and not isinstance(since_ts, bool):
        entries = [row for row in entries if int(row.get("time") or 0) >= since_ts]
    envelope: dict[str, Any] = {
        "unit": unit,
        "from_seq": payload.get("from"),
        "last_seq": payload.get("last_seq"),
        "next_from": payload.get("next_from"),
        "refs_unresolved": payload.get("refs_unresolved", 0),
        "returned": len(entries),
    }
    if isinstance(wanted, list) and wanted:
        # Named because a filtered page that returns nothing is NOT an empty log,
        # and ``next_from`` is the answer to both -- the caller has to be able to
        # tell "no rows of this type in this range" from "the log ends here".
        envelope["filtered_by_types"] = sorted({str(t) for t in wanted})
    return _fit(envelope, _compact_rows(entries, full=full))


def _projection(args: dict[str, Any], caller_key: str) -> str:
    unit, err = _resolve_unit(str(args.get("unit") or ""), caller_key)
    if err:
        return err
    name = str(args.get("name") or "")
    path = f"/api/crew-log/units/{quote(unit, safe='')}/projection/{quote(name, safe='')}"
    payload = _get(path, session_key=caller_key)
    if not isinstance(payload, dict) or payload.get("error"):
        return _proxy_error(
            payload if isinstance(payload, dict) else {},
            fallback=f"no {name!r} projection for {unit!r}",
        )
    return redact(json.dumps({"unit": unit, **payload}, indent=2, ensure_ascii=False))


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
#: from its own process. True here because it does: ``self`` resolves through
#: :func:`~kiro_crew.mcp_core.require_strict_session_key`, whose first source is
#: that block, and the endpoint scopes every read on the key this server forwards.
#:
#: Advertising is not cosmetic. ``mcp_gateway/backend.py`` strips any client-forged
#: caller block from EVERY forwarded request and re-injects its own only when the
#: backend advertised this capability — so without the advertisement the block
#: never arrives and a pooled backend would resolve an empty identity for every
#: caller. For this server that fails CLOSED (``self`` refuses, a wider read is
#: refused for want of an owner session), so the failure mode is tools that stop
#: working rather than a read that widens; the advertisement is what makes them
#: work on the topology pooling was built to serve.
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
