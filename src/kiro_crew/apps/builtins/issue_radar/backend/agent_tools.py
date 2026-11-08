"""The app's agent tools: ``issue_radar_record_investigation``, ``issue_radar_crew_read``
and ``issue_radar_crew_record``, served from the app's own ``agentTools`` route.

``app.json`` declares the three tools (names, descriptions, argument schemas) and the
route ``agent-tools``. The ``kirocrew-core`` MCP server lists them, validates a call
against the declaration and forwards it to the gateway's ``POST /api/app-tools/call``,
which hands it here through the route registry (``app-kit-platform.md`` section 30.1).
No app code runs in that MCP server. What a call does is decided here, and the answer
is ``{"text": <the result>}``, which the core returns to the agent as the tool result.

Who may call: only that forwarded call. A request reaches this handler with
``internal_auth`` only through ``/api/app-tools/call``, because the gateway admits no
internal-secret caller under ``/api/apps/issue-radar``; a dashboard request is refused.

Who the crew is: the two crew tools are declared ``strictSession``, so the core forwards
a call only from a session it identifies directly (never a subagent resolved to its
parent's slot) and sends that session's key as ``X-Session-Key``. The crew is resolved
from that key on the app's own crew routes (``crew_routes``), never from an argument.
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend.http_routes import investigation
from kiro_crew.config.loader import config_dir
from kiro_crew.platform import redact_via_context as redact

#: Progress lines returned by a read. The log is repo-wide and append-only, so
#: an unbounded slice would grow to dominate a crew's context.
_CREW_MAX_EVENTS = 20

#: ``maxResultChars`` of ``issue_radar_crew_read`` in ``app.json``: where the core cuts
#: the answer. A cut there splits the JSON, so the read is fitted under it here.
_CREW_READ_CAP = 60_000
#: What the read may fill: the cap less room for the core's second redaction pass.
_CREW_READ_BUDGET = _CREW_READ_CAP - 2_000

# Abbreviated-or-full git object name. Bounds ``base_sha`` to something that can
# actually be handed to git on a resume; a resumed turn checks out from this
# value, so an arbitrary string here is a resume that fails much later. The
# declaration bounds its length; the shape is checked here.
_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _payload(resp: web.Response) -> dict[str, Any]:
    """A route's answer as the tool reads it: its JSON object, or ``{"error": ...}``."""
    raw = resp.text or ""
    try:
        parsed: Any = json.loads(raw) if raw else {}
    except ValueError:
        parsed = None
    if resp.status >= 400:
        if isinstance(parsed, dict) and "error" in parsed:
            return {"error": str(parsed["error"])}
        return {"error": raw or f"HTTP {resp.status}"}
    return parsed if isinstance(parsed, dict) else {}


def _crew_machine_markers() -> list[tuple[str, str]]:
    """Strings that identify THIS machine, longest first.

    Longest-first matters: the Kiro Crew home normally sits inside the user's
    home, so scrubbing the home first would leave ``<home>/.kiro/crew/...`` —
    still a directory layout — instead of collapsing the whole prefix.
    """
    markers: list[tuple[str, str]] = []
    for value, placeholder in (
        (str(config_dir()), "<kirocrew-home>"),
        (str(Path.home()), "<home>"),
        (tempfile.gettempdir(), "<tmp>"),
    ):
        if value and value not in ("/", "\\"):
            markers.append((value, placeholder))
    host = ""
    with contextlib.suppress(Exception):
        host = socket.gethostname()
    # Only a distinctive hostname is scrubbed. A short one ("dev", "mac") is a
    # real English word often enough that substring-replacing it would corrupt
    # ordinary prose, and a corrupted progress line is a worse outcome than a
    # short hostname the brief already forbids writing.
    if len(host) >= 8:
        markers.append((host, "<host>"))
    markers.sort(key=lambda pair: len(pair[0]), reverse=True)
    return markers


def _crew_public_text(text: str) -> str:
    """Sanitize a crew string that becomes PUBLIC, on the way IN.

    Two passes, for two different reasons:

    1. ``redact`` — the canonical egress helper ``issue_radar_record_investigation``
       uses. That tool redacts because LLM prose about an untrusted issue body is
       re-rendered on a card; here the same prose is ALSO rendered into a comment on
       the forge, so a credential or exfil URL quoted out of an issue would be
       published, not merely stored.
    2. ``_crew_machine_markers`` — redaction covers credentials and exfil URLs,
       NOT an absolute path or a host name, and those are exactly what must not
       leave this machine in a public comment. This pass is a backstop, not the
       control: it can only remove identifiers this process can name, so the
       crew brief's prohibition remains the primary rule. It is deliberately NOT
       applied to ``worktree`` / ``branch`` / ``base_sha`` — those are the one
       place an absolute path legitimately belongs, they stay local, and
       scrubbing them would break the resume they exist for.
    """
    out = redact(text)
    for value, placeholder in _crew_machine_markers():
        out = out.replace(value, placeholder)
        if "\\" in value:
            # Windows: the same path is written both ways in practice.
            out = out.replace(value.replace("\\", "/"), placeholder)
    return out


def _crew_identity(payload: dict[str, Any]) -> tuple[str, str, str] | None:
    """Pull ``(owner, repo, crew_id)`` out of a crew-read response.

    The identity is echoed back by the READ route, which resolves it from the
    calling session's ``X-Session-Key`` — it is never taken from tool arguments.
    That is what makes a cross-crew write impossible: a crew cannot name a repo, so
    it cannot overwrite a same-numbered issue in another repo, and it cannot reach
    another crew's item at all (which would also defeat the store's per-crew
    "one editing item" invariant).

    Tolerant of where the route puts it — top level, on the crew record, or on a
    work item — because all three carry it and a single hard-coded location would
    turn a harmless shape difference into a dead write path. The top level is the
    one that is always present: a crew with no work items yet has no other source.
    """
    _raw_crew = payload.get("crew")
    crew: dict[str, Any] = _raw_crew if isinstance(_raw_crew, dict) else {}
    _raw_items = payload.get("items")
    items: list[Any] = _raw_items if isinstance(_raw_items, list) else []
    first = next((it for it in items if isinstance(it, dict)), {})
    owner = repo = ""
    for source in (payload, crew, first):
        owner = str(source.get("owner") or "").strip()
        repo = str(source.get("repo") or "").strip()
        if owner and repo:
            break
    if not (owner and repo):
        return None
    crew_id = str(crew.get("id") or crew.get("crew_id") or first.get("crew_id") or "").strip()
    if not crew_id:
        return None
    return owner, repo, crew_id


def _crew_ledger_view(payload: dict[str, Any]) -> dict[str, Any]:
    """Project a crew-read response into what a resuming turn actually needs.

    Crew and unfinished-item fields pass through (they are the resume state); the
    repo-wide event log is bounded to the newest ``_CREW_MAX_EVENTS`` lines.

    The skip fields are NOT re-trimmed here: a crew checks ``skipped_numbers``
    before investigating, so a trimmed list would answer "not skipped" for an
    issue that is.
    """
    _raw_events = payload.get("events")
    events: list[Any] = _raw_events if isinstance(_raw_events, list) else []
    view: dict[str, Any] = {
        "crew": payload.get("crew") or {},
        "settings": payload.get("settings") or {},
        "counts": payload.get("counts") or {},
        "skipped_numbers": payload.get("skipped_numbers") or [],
        "recent_skips": payload.get("recent_skips") or [],
        # Who handed each recent issue back: the human-answered wake carries it, and a
        # wake that lands mid-turn is dropped, so the read carries it too.
        "recent_answers": payload.get("recent_answers") or [],
        # ``read_events`` returns NEWEST FIRST, so the newest N is ``events[:N]``
        # (``events[-N:]`` would be the oldest). Reversed into chronological order
        # because the crew reads it as a transcript.
        "recent_events": list(reversed(events[:_CREW_MAX_EVENTS])),
    }
    if len(events) > _CREW_MAX_EVENTS:
        view["recent_events_note"] = (
            f"newest {_CREW_MAX_EVENTS} of {len(events)} — the full log is on the crew page"
        )
    # Last, so even a cut that reached the JSON would take items before the skip index.
    view["open_items"] = payload.get("items") or []
    return view


def _render_view(view: dict[str, Any], indent: int | None) -> str:
    # Redact the OUTPUT too: the ledger holds LLM prose written from untrusted issue
    # text, and a resume re-reads it into context. Paths in `worktree` survive this
    # pass (it removes credentials and exfil URLs, not paths), which the resume needs.
    return redact(json.dumps(view, indent=indent, ensure_ascii=False))


def _without_oldest(
    entries: list[Any], count: int, droppable: Callable[[Any], bool], oldest_last: bool
) -> list[Any]:
    """*entries* less the *count* oldest that *droppable* accepts."""
    order = [i for i, entry in enumerate(entries) if droppable(entry)]
    if oldest_last:
        order.reverse()
    gone = set(order[:count])
    return [entry for i, entry in enumerate(entries) if i not in gone]


def _any_entry(_entry: Any) -> bool:
    return True


def _fit_ledger_view(view: dict[str, Any], finished: Callable[[Any], bool]) -> str:
    """The read's answer, whole JSON within ``_CREW_READ_BUDGET``, or a refusal.

    The view as built when it fits. Otherwise it is rendered without indentation and
    sheds the oldest entries, least needed first: finished items, progress lines,
    skip reasons, answers, then unfinished items. ``omitted`` counts what went. The
    crew record, settings, counts and ``skipped_numbers`` are never shed: a trimmed
    skip list answers "not skipped" for an issue that is.
    """
    text = _render_view(view, indent=2)
    if len(text) <= _CREW_READ_BUDGET:
        return text
    # (field, label in ``omitted``, which entries may go, oldest at the end?)
    stages: tuple[tuple[str, str, Callable[[Any], bool], bool], ...] = (
        ("open_items", "finished_items", finished, True),
        ("recent_events", "recent_events", _any_entry, False),
        ("recent_skips", "recent_skips", _any_entry, True),
        ("recent_answers", "recent_answers", _any_entry, True),
        ("open_items", "open_items", _any_entry, True),
    )
    omitted: dict[str, int] = {}

    def trimmed(field: str, label: str, keep: list[Any]) -> dict[str, Any]:
        gone = {**omitted, label: len(view[field]) - len(keep)}
        return {
            **view,
            field: keep,
            "omitted": gone,
            "omitted_note": (
                "left out to fit this answer, oldest first; the crew page has all of it"
            ),
        }

    for field, label, droppable, oldest_last in stages:
        entries: list[Any] = view[field]
        candidates = sum(1 for entry in entries if droppable(entry))
        # Fewest entries to drop for a fit: dropping more never grows the text.
        low, high = 0, candidates
        while low < high:
            mid = (low + high) // 2
            keep = _without_oldest(entries, mid, droppable, oldest_last)
            if len(_render_view(trimmed(field, label, keep), None)) <= _CREW_READ_BUDGET:
                high = mid
            else:
                low = mid + 1
        if low:
            view = trimmed(field, label, _without_oldest(entries, low, droppable, oldest_last))
            omitted = view["omitted"]
        text = _render_view(view, None)
        if len(text) <= _CREW_READ_BUDGET:
            return text
    return (
        "Error: this crew's ledger does not fit in one answer even without its work "
        "items and recent history; open the crew page to read it."
    )


def _crew_record_argument_error(args: dict[str, Any]) -> str:
    """The refusal for a record call the declared schema cannot express, or ``""``.

    The declaration bounds every field; two checks need more than its subset:

    * ``base_sha`` is a git object name, so a resume can hand it to git;
    * ``event`` and ``event_kind`` travel together, and a ``phase`` write must carry
      its reason. That is what justifies ONE write tool instead of two: splitting
      upsert and append is what allows a phase to move with nothing logged, and
      merging them only closes that if the event is mandatory on a phase change.
    """
    base_sha = args.get("base_sha")
    if base_sha and not _GIT_SHA_RE.match(base_sha):
        return "Error: base_sha: invalid format"
    if args.get("event") and not args.get("event_kind"):
        return "Error: event_kind: required when 'event' is given"
    if args.get("event_kind") and not args.get("event"):
        return "Error: event: required when 'event_kind' is given"
    if args.get("phase") and not args.get("event"):
        return (
            "Error: event: required when 'phase' changes — record the reason with the "
            "phase (also pass 'event_kind')"
        )
    return ""


async def record_investigation(request: web.Request, args: dict[str, Any]) -> str:
    """``issue_radar_record_investigation``: write the item's local investigation record."""
    _ir_findings: dict[str, Any] = {
        key: redact(args[key])
        for key in ("verdict", "root_cause", "next_action", "summary")
        if args.get(key)
    }
    _ir_labels = [redact(s) for s in (args.get("suggested_labels") or []) if s]
    if _ir_labels:
        _ir_findings["suggested_labels"] = _ir_labels
    # provider/host/kind are sent EXPLICITLY, never left to the server
    # default: the record is keyed on them, so a GitLab item recorded as
    # public GitHub would write into — and could overwrite — a same-slug
    # GitHub repo's record. Same reason the seed prompt echoes them.
    _ir_body: dict[str, Any] = {
        "owner": args["owner"],
        "repo": args["repo"],
        "number": args["number"],
        "provider": args.get("provider") or "github",
        "host": args.get("host") or "github.com",
        "kind": args.get("kind") or "issue",
        "status": args.get("status") or "resolved",
    }
    if _ir_findings:
        _ir_body["findings"] = _ir_findings
    _ir_resp = _payload(await investigation.put_investigation(_ir_body))
    if _ir_resp.get("error"):
        return f"Error: {_ir_resp['error']}"
    _ir_saved = (_ir_resp.get("investigation") or {}).get("findings") or {}
    _ir_ref = (
        f"{_ir_body['owner']}/{_ir_body['repo']}"
        f"{'!' if _ir_body['kind'] == 'pull' and _ir_body['provider'] == 'gitlab' else '#'}"
        f"{_ir_body['number']}"
    )
    if not _ir_saved:
        return (
            f"Recorded status `{_ir_body['status']}` for {_ir_ref} "
            "(no findings supplied — the card will show the status badge only)."
        )
    _ir_verdict = _ir_saved.get("verdict") or "(no verdict)"
    return (
        f"Recorded investigation for {_ir_ref}: status `{_ir_body['status']}`, "
        f"verdict `{_ir_verdict}`. It now shows on the item's Issue Radar card."
    )


async def crew_read(request: web.Request, args: dict[str, Any]) -> str:
    """``issue_radar_crew_read``: the calling crew's own ledger, bounded for a resume."""
    # Imported here: ``crew_routes`` imports the routes facade back, so the hook
    # module that loads this one must not pull it in at import.
    from kiro_crew.apps.builtins.issue_radar.backend import crew_routes

    _cr_payload = _payload(await crew_routes.crew_read_for_session(request))
    if _cr_payload.get("error"):
        return f"Error: {_cr_payload['error']}"
    if _crew_identity(_cr_payload) is None:
        return (
            "Error: this session is not bound to an Issue Radar crew, so there "
            "is no ledger to read. Only a crew's own session can use this tool."
        )
    finished = crew_routes.crew_store.TERMINAL_PHASES
    return _fit_ledger_view(
        _crew_ledger_view(_cr_payload),
        lambda item: isinstance(item, dict) and item.get("phase") in finished,
    )


async def crew_record(request: web.Request, args: dict[str, Any]) -> str:
    """``issue_radar_crew_record``: one step of crew work, item and progress line together."""
    # Imported here: ``crew_routes`` imports the routes facade back, so the hook
    # module that loads this one must not pull it in at import.
    from kiro_crew.apps.builtins.issue_radar.backend import crew_routes

    _cw_refusal = _crew_record_argument_error(args)
    if _cw_refusal:
        return _cw_refusal
    _cw_payload = _payload(await crew_routes.crew_read_for_session(request))
    if _cw_payload.get("error"):
        return f"Error: {_cw_payload['error']}"
    _cw_identity = _crew_identity(_cw_payload)
    if _cw_identity is None:
        return (
            "Error: this session is not bound to an Issue Radar crew, so there "
            "is no ledger to write. Only a crew's own session can use this tool."
        )
    _cw_owner, _cw_repo, _cw_crew_id = _cw_identity

    _cw_body: dict[str, Any] = {
        "owner": _cw_owner,
        "repo": _cw_repo,
        "crew_id": _cw_crew_id,
    }
    # Presence, not truthiness. Omitting `number` is a MEANING — "this step belongs
    # to no issue" — so it is forwarded as an absence rather than as a zero, which
    # the route would read as a malformed issue number. The route pairs the absence
    # with the crew-level `sweep` kind and refuses any other combination.
    if "number" in args:
        _cw_body["number"] = args["number"]
    # Local-only resume fields, passed through verbatim. NOT scrubbed: an
    # absolute worktree path is the point of the field, and it is never
    # rendered into a comment (crew_store keeps these local).
    for _cw_key in ("worktree", "branch", "base_sha"):
        if args.get(_cw_key):
            _cw_body[_cw_key] = args[_cw_key]
    # `phase` is an allowlisted enum value (validation rejects anything
    # else), so it is passed verbatim — redacting a closed vocabulary would
    # only obscure where the real sanitizing happens.
    if args.get("phase"):
        _cw_body["phase"] = args["phase"]
    # Classification for the repo-wide shared skip index. Forwarded raw: the
    # store coerces an unrecognised value to `other` rather than refusing, so
    # a mislabelled pass is still an indexed pass (see crew_store.SKIP_SCOPES).
    if args.get("skip_scope"):
        _cw_body["skip_scope"] = args["skip_scope"]
    # Prose that is rendered on the crew page. Redacted for the same reason
    # the investigation tool redacts its findings — it is LLM prose about an
    # untrusted issue body, stored verbatim and re-displayed on every visit.
    for _cw_key in ("outcome", "next", "decision", "why"):
        if args.get(_cw_key):
            _cw_body[_cw_key] = redact(args[_cw_key])
    if args.get("tried_approach"):
        _cw_body["tried_approach"] = redact(args["tried_approach"])
        if args.get("tried_rejected_because"):
            _cw_body["tried_rejected_because"] = redact(args["tried_rejected_because"])
    if args.get("pr_number"):
        _cw_body["pr_number"] = args["pr_number"]
    if args.get("claim_comment_id"):
        _cw_body["claim_comment_id"] = args["claim_comment_id"]
    # Presence, not truthiness: `labels_applied: []` is the crew SAYING it now
    # holds no labels, which is what it reports after removing its last one.
    # Gating on the list being non-empty made that indistinguishable from not
    # mentioning labels at all, so the store kept the previous set and the
    # crew's record claimed labels it had just taken off the issue.
    if "labels_applied" in args:
        _cw_body["labels_applied"] = [redact(s) for s in (args.get("labels_applied") or []) if s]
    # Names, forwarded as names: the route turns each into an explicit null in the
    # work-item patch, which is how a field is emptied. Every field above is gated
    # on truthiness, so this list is the ONLY way a clear reaches the ledger.
    if "clear" in args:
        _cw_body["clear"] = [s for s in (args.get("clear") or []) if isinstance(s, str) and s]
    # The flat ci_* args are re-assembled into the store's `ci_state` dict
    # (crew_store merges it key-by-key). `ci_state` the ARG is the forge's
    # verdict word and becomes the dict's `state`; an int reading of 0 is
    # meaningful (0/47 green, 0 inherited reds) so these are dropped on
    # "not supplied", not on falsiness.
    _cw_ci: dict[str, Any] = {}
    if args.get("ci_state"):
        _cw_ci["state"] = args["ci_state"]
    for _cw_arg, _cw_field in (
        ("ci_passed", "passed"),
        ("ci_total", "total"),
        ("ci_round", "round"),
        ("ci_inherited_reds", "inherited_reds"),
    ):
        if args.get(_cw_arg) is not None:
            _cw_ci[_cw_field] = args[_cw_arg]
    if _cw_ci:
        _cw_body["ci_state"] = _cw_ci
    # The progress line: rendered inside the <details> block of the claim
    # comment on the forge, so it is the strictest string in this payload.
    if args.get("event"):
        _cw_body["event"] = _crew_public_text(args["event"])
        _cw_body["event_kind"] = args["event_kind"]

    _cw_resp = _payload(await crew_routes.crew_work_for_session(request, _cw_body))
    if _cw_resp.get("error"):
        return f"Error: {_cw_resp['error']}"
    _cw_raw_item = _cw_resp.get("item")
    _cw_item: dict[str, Any] = _cw_raw_item if isinstance(_cw_raw_item, dict) else {}
    # A crew-level line has no issue to name and no item to report a phase for, so
    # it is summarised by the crew it belongs to. Reusing the numbered wording here
    # would print a bare `#` and claim a phase the write never touched.
    if "number" in args:
        _cw_ref = f"{_cw_owner}/{_cw_repo}#{args['number']}"
        _cw_phase = _cw_item.get("phase") or _cw_body.get("phase") or "(phase unchanged)"
        _cw_lines = [f"Recorded {_cw_ref}: phase `{_cw_phase}`."]
    else:
        # Whether a line was actually written matters to the caller: a coalesced
        # checkpoint is still acknowledged, but a crew that believed each idle
        # cycle added a line would misread its own log's length as its cycle count.
        if _cw_resp.get("coalesced"):
            _cw_lines = [
                f"Crew-level step for {_cw_owner}/{_cw_repo} folded into the open "
                "idle stretch (its ledger line already says the queue was empty, "
                "so no duplicate was added -- the existing line's timestamp marks "
                "when the stretch began)."
            ]
        else:
            _cw_lines = [
                f"Recorded a crew-level step for {_cw_owner}/{_cw_repo} "
                "(no issue -- no work item was written)."
            ]
    if _cw_body.get("event"):
        # Echo the stored line, not the argument — if a sanitizer pass
        # changed it, the crew must see what actually became public.
        _cw_raw_event = _cw_resp.get("event")
        _cw_ev: dict[str, Any] = _cw_raw_event if isinstance(_cw_raw_event, dict) else {}
        _cw_stored_event = _cw_ev.get("text") or _cw_body["event"]
        _cw_lines.append(f"Logged ({_cw_body['event_kind']}): {_cw_stored_event}")
    if _cw_item.get("next"):
        _cw_lines.append(f"Next: {_cw_item['next']}")
    # Confirm the SHARED index write. The brief promises that recording
    # `phase: skipped` is itself what tells every other crew in the repo not to
    # re-investigate this issue, so the crew has to see that it landed —
    # otherwise the one guarantee that stops the fleet looping is invisible to
    # the only party that can act on it. Echoing the STORED scope also shows a
    # coercion: an unrecognised scope is filed as `other`, and a crew that
    # believed it recorded `architecture` should see what was really kept.
    _cw_raw_skip = _cw_resp.get("skip")
    if isinstance(_cw_raw_skip, dict) and _cw_raw_skip.get("number") is not None:
        _cw_lines.append(
            f"Shared skip index: #{_cw_raw_skip['number']} recorded as "
            f"`{_cw_raw_skip.get('scope') or 'other'}` — no other crew in this "
            "repository will investigate it now."
        )
    return redact("\n".join(_cw_lines))


#: The tools ``app.json`` declares, by name.
TOOLS: dict[str, Callable[[web.Request, dict[str, Any]], Awaitable[str]]] = {
    "issue_radar_record_investigation": record_investigation,
    "issue_radar_crew_read": crew_read,
    "issue_radar_crew_record": crew_record,
}


async def handle_agent_tool_call(request: web.Request, ctx: Any = None) -> web.Response:
    """``POST /api/apps/issue-radar/agent-tools`` -- run one forwarded agent tool call.

    Refuses anything but the core's forwarded call (``internal_auth``); the body is the
    ``{"tool", "arguments"}`` the core already checked against the declaration.
    """
    if request.get("internal_auth") is not True:
        return web.json_response(
            {"error": "agent tool calls only", "code": "agent_tool_only"}, status=403
        )
    try:
        body: Any = await request.json()
    except (LookupError, RecursionError, ValueError):
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("arguments"), dict):
        return web.json_response(
            {"error": "body must be {tool, arguments}", "code": "invalid_json"}, status=400
        )
    run = TOOLS.get(str(body.get("tool") or ""))
    if run is None:
        return web.json_response({"error": "unknown tool", "code": "unknown_tool"}, status=404)
    return web.json_response({"text": await run(request, body["arguments"])})


class _Route:
    """One route record for the registry (read structurally: method, path, handler)."""

    def __init__(self, method: str, path: str, handler: Callable[..., Any]) -> None:
        self.method = method
        self.path = path
        self.handler = handler


def register_routes(ctx: Any) -> list[_Route]:
    """The ``backend.hooks.routes`` hook: the ``agentTools`` route, through the registry.

    The app's other routes are mounted on the gateway router at boot
    (``backend.routes:register_routes``); the core's tool forwarder reaches an
    in-gateway app only through a route the app registered here.
    """
    return [_Route("POST", "/agent-tools", handle_agent_tool_call)]
