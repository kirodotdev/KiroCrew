"""The five debug reads the ``kirocrew-debug`` MCP server proxies.

``mcp_debug.py`` shapes output. THIS module decides who may see it, which is why a
reviewer looking for the security argument should read here.

``gateway`` and ``refusals`` need nothing outside this module, so they always
answer. ``threads``, ``processes`` and ``snapshots`` read :mod:`kiro_crew.diag`:
they answer whenever that module resolves, and when a build does not carry it they
answer HTTP 501 with exactly ``{"error": "diag not available in this build"}``,
which the tools relay verbatim. The import is LAZY for a reason a test
pins: this module is on the gateway's boot path, and a gateway must not pay to load
a diagnostics package on a build where it does not exist at all.

**Why the authorization lives here and not in the server.** The server is an MCP
stdio process the agent's own session starts; anything it decided about
entitlement would be a decision made inside the thing being entitled. The gateway
holds the session registry, so only the gateway can say which session is asking.

**The rule, and what makes these views different from a crew log.** Four of the
five are HOST-WIDE: the gateway's own identity, the interpreter's threads, every
process in the family, the recorded host series. They carry cross-session metadata
by construction — another session's title as a process owner, Python frames from
an interpreter several sessions share, the shape of what else is running. So they
are for the OWNER at a dashboard tab and nobody else. That is stricter than the
crew-log door, which lets a conductor read the logs of sessions it dispatched,
and deliberately so: a dispatch tree bounds whose CONVERSATION you may read, but
it does not bound a host-wide view — the host is not inside anyone's dispatch
tree.

``refusals`` is the one per-session view, and it gets the crew-log scope: the
caller's own rows, plus the rows of any session it spawned at any depth, plus
anything at all for the owner's tab. A session asking why its own calls were
refused learns nothing it did not already experience, which is what makes that
arm safe without an owner check.

**The caller classes that get no host-wide view at all**, mirroring
``session_control.authorize_target`` and importing its constants rather than
restating them: unattended callers (a scheduled run has no operator watching what
it did with the answer), app-scoped callers (the session belongs to its app),
incognito and temporary callers (created to leave and learn nothing), and
channel-linked or channel-mirrored callers — including an owner's own tab that is
mirrored, because a mirrored tab republishes every turn, so the one session
entitled to the wide view would become its publisher. The exclusions are about
where a read LANDS, not about how much a session is trusted.

**Redaction.** Every string leaves through ``redact_via_context`` and
``sel._redact_text``. Command lines are redacted; environment exposure is four
allow-listed keys and nothing else; ``.env``, the vault and the trust directories
are reported as metadata — size, mtime, mode — and never as bytes. faulthandler
dumps cannot be redacted at write time because C code writes them, so they stay in
the fenced directory and are scrubbed on read-back. Output is capped at 64 KB and
cut at a row boundary with a cursor.

**The risk this module accepts, stated plainly** because the PR body states it
too: ``processes`` and ``threads`` expose cross-session metadata — owner titles,
stack frames — that no other agent-reachable surface exposes. The mitigation is
the owner-tab gate above plus the absence of any message body: conversation
content stays behind the crew-log tools, and nothing here reads a transcript.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Final

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import guard_owner_surface_routes

logger = logging.getLogger(__name__)

#: The component name this module's door recognizes on ``X-Internal-Caller``.
#: Spelled here rather than imported from :mod:`kiro_crew.mcp_debug`, because that
#: module is an MCP stdio server and this one is on the gateway's boot path; a test
#: pins the two together so they cannot drift.
DEBUG_MCP_CALLER: Final[str] = "kirocrew-debug"

#: The refusal the three diag-dependent routes answer with until the recorder and
#: process-tree modules land. The EXACT body the module contract fixes, so the
#: server's relay and this string are pinned to each other by a test.
DIAG_UNAVAILABLE: Final[str] = "diag not available in this build"

#: The code that rides with that message. Spelled here and in
#: ``mcp_debug.DIAG_UNAVAILABLE_CODE``; a test pins the two together. An agent
#: branches on it: this one means "wait for a build", where ``unavailable`` means
#: nothing answered and ``forbidden`` means stop asking.
DIAG_UNAVAILABLE_CODE: Final[str] = "diag_unavailable"

#: The views that expose host-wide or cross-session state, and are therefore the
#: owner's alone. Named as a set so a route added later must choose a side
#: explicitly rather than inherit the weaker check by omission.
HOST_WIDE_VIEWS: Final[frozenset[str]] = frozenset({"gateway", "threads", "processes", "snapshots"})

#: The literal a caller passes to mean "my own session".
SELF: Final[str] = "self"

#: Refusal rows one ``refusals`` read returns, newest first.
MAX_REFUSALS: Final[int] = 200

#: Bytes of the security event log this read will walk backwards over. A SEL file
#: rotates at a bounded size but the segment directory holds several, and a debug
#: read must not turn into an unbounded scan on a busy host: past this the answer
#: says it was cut rather than reading further.
MAX_SEL_SCAN_BYTES: Final[int] = 8 * 1024 * 1024

#: ``outcome`` values that make a SEL row a REFUSAL.
#:
#: The spec asked for "SEL ``tool_denied`` rows". No such event type exists: SEL's
#: vocabulary is ``tool_invocation`` / ``tool_approval`` / ``tool_denial`` /
#: ``mcp_call`` / ``api_access`` (:class:`kiro_crew.sel.SecurityEvent`), and a
#: refusal appears either as a ``tool_denial`` row or as a ``tool_invocation`` /
#: ``api_access`` row whose outcome is denied or rejected. Reading the real
#: vocabulary rather than the spec's wording is the approved reading; the spec is
#: being corrected to match.
REFUSAL_OUTCOMES: Final[frozenset[str]] = frozenset({"denied", "rejected"})

#: Event types whose rows can carry a refusal at all. ``tool_approval`` is excluded
#: on purpose: an approval row with a refusal outcome would be a contradiction, and
#: reading one as a refusal would report an approval as its opposite.
REFUSAL_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {"tool_denial", "tool_invocation", "api_access", "mcp_call"}
)

#: The system-scope systemd unit directory, assembled from parts rather than
#: written as one literal. Two reasons, and the second is the load-bearing one:
#: systemd's system scope exists only on Linux, and a quoted absolute POSIX path
#: is what the Cross-Platform Portability gate refuses, because Windows has no
#: such root. Nothing here requires it to exist -- ``_systemd_drop_ins`` guards
#: every candidate with ``is_dir()``, so on a host without it the list is simply
#: shorter.
_SYSTEM_UNIT_ROOT: Final[Path] = Path(os.sep) / "etc" / "systemd" / "system"


# --------------------------------------------------------------------------- #
# Refusal classification
# --------------------------------------------------------------------------- #
#
# The classes exist because the four refusals read identically to an agent today
# and call for completely different actions. The most costly confusion is the
# second one: a path the resolver could not finish judging is reported with the
# same "denied" wording as a real match, so an agent concludes the path is
# protected, stops, and reports a wall -- when the correct action was to retry the
# identical call. Classifying is the whole value of this tool.


def _classify_refusal(row: dict[str, Any]) -> str:
    """Which KIND of refusal a SEL row records.

    The budget case is asked FIRST, and asked structurally:
    :func:`~kiro_crew.security.paths.is_unverifiable_path_refusal` matches the fixed
    prefix that precedes any caller-influenced text, so no path spelling can move a
    refusal into another class. It has to come first because a budget refusal's text
    also names the sensitive-path list -- it says the path could not be checked
    against it -- so a substring test for the match would label every timeout a
    match and invert the distinction this function exists to draw. The prefix test
    cannot make that mistake, and the order is kept so the reason a reader would ask
    about stays visible.

    The remaining classes are read from the reason text, because that is where the
    gate states what it decided and the outcome field says only that something was
    refused. They are coarse on purpose: each one answers "what should the caller do
    now", not "which line refused".

    A row that TALKS about the resolver budget but fails the structural test is left
    ``unclassified`` rather than falling through to the sensitive-path arm. Both
    refusals name the sensitive-path list, so the fall-through would tell a caller
    "this path is protected, stop asking" about a transient timeout it should simply
    retry -- the precise inversion this function exists to prevent, and worse than
    admitting the row is unrecognised.
    """
    from kiro_crew.security.paths import is_unverifiable_path_refusal

    reason = str(row.get("error") or "")
    if is_unverifiable_path_refusal(reason.lstrip()):
        return "unverifiable_path"
    text = f"{reason} {row.get('resources') or ''}".lower()
    if not text.strip():
        return "unclassified"
    if "resolver budget" in text or "symlink resolution did not complete" in text:
        return "unclassified"
    if "sensitive" in text and "path" in text:
        return "sensitive_path_match"
    # A write-protected config path is the SAME class of answer as a sensitive-path
    # match, and it is grouped with it deliberately rather than given a sixth name:
    # both mean "this path is protected and the refusal is correct", so the agent's
    # action is identical -- stop asking, do not retry. Found by reading real rows,
    # where this wording classified as ``unclassified`` and so told a caller nothing.
    if "write-protected" in text or "protected path" in text:
        return "sensitive_path_match"
    if "governance" in text or "profile" in text or "ceiling" in text:
        return "governance"
    if "timeout" in text or "timed out" in text:
        return "tool_policy_timeout"
    if "deny" in text or "denied rule" in text or "refusal diagnostic" in text:
        return "denied_rule"
    # The marker above is the gate's own; this is the fallback for a gate that states
    # its rule in prose. It needs BOTH a denial word and "rule", because every more
    # specific class has already been offered the row by now. It spells "denied" and
    # not only "deny": "deny" is not a substring of "denied", so the commonest
    # phrasing of all -- "denied by rule X" -- fell through to unclassified and told
    # the caller nothing, which is the same defect already fixed above for
    # write-protected paths.
    if ("denied" in text or "denial" in text) and "rule" in text:
        return "denied_rule"
    return "unclassified"


def _refusal_diagnostic_id(row: dict[str, Any]) -> str:
    """The refusal-diagnostic id a ``denied_rule`` row carries, or ``""``.

    The id is what names the specific rule, so an operator can find it without
    reading prose. Parsed rather than recomputed: the gate already rendered it into
    the reason, and re-deriving it here would let the two disagree about which rule
    fired.

    Read from the LAST line carrying the prefix, and only from a line that STARTS
    with it. :func:`~kiro_crew.security.diagnostics.annotate_refusal` appends the
    diagnostic on its own final line, so the last such line is the gateway's own;
    an earlier one is inside the refusal text, and a refusal reason can quote the
    subject that caused it. Taking the first match would let quoted text name the
    rule. The token is then passed through the writer's own validator, so a value
    that is not a diagnostic id is dropped rather than echoed to the operator as
    one.
    """
    from kiro_crew.security.diagnostics import (
        _UNNAMED,
        REFUSAL_DIAGNOSTIC_PREFIX,
        _diagnostic_id,
    )

    for line in reversed(str(row.get("error") or "").splitlines()):
        if not line.startswith(REFUSAL_DIAGNOSTIC_PREFIX):
            continue
        tail = line[len(REFUSAL_DIAGNOSTIC_PREFIX) :]
        for token in tail.split():
            if not token.startswith("rule="):
                continue
            value = _diagnostic_id(token[len("rule=") :])
            return "" if value == _UNNAMED else value
        return ""
    return ""


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


def _forbidden(reason: str) -> web.Response:
    """A 403 in this module's own vocabulary, with the reason the agent can act on."""
    return web.json_response({"error": reason, "code": "forbidden"}, status=403)


def _diag_unavailable() -> web.Response:
    """The refusal a diag-backed read earns until the sibling modules land.

    501, and the message is EXACTLY the contract string: an agent must be able to
    tell "this build cannot answer yet" from "the answer is nothing", and a
    synthesized empty result would read as the second. 501 rather than 503 because
    the capability is genuinely not implemented in this build, not temporarily down.

    It carries a ``code`` beside that message because every error response in this
    repo does -- ``test_error_code_contract.py`` enforces it, and an agent branching
    on a code is the whole reason. The module contract fixes the MESSAGE, which is
    what the MCP server matches on to relay verbatim, so naming the code alongside
    it satisfies both without changing what the relay sees.
    """
    return web.json_response({"error": DIAG_UNAVAILABLE, "code": DIAG_UNAVAILABLE_CODE}, status=501)


def _caller_session_key(request: web.Request) -> str:
    """The session key the internal proxy forwarded, or ``""``.

    The agent cannot forge it: it does not build the request, and the MCP request
    helpers set it from the calling session's own strictly-resolved context rather
    than from tool arguments.
    """
    return (request.headers.get("X-Session-Key") or "").strip()


def _live_slot(state: Any, session_key: str) -> Any:
    """The live slot behind *session_key*, or ``None``.

    ``None`` means the key names no slot this gateway currently holds, and every
    caller here treats that as a refusal rather than a pass: a caller that cannot
    be placed cannot be granted a host-wide view of the machine it is running on.
    """
    if not session_key:
        return None
    slots = getattr(state, "_slots", None)
    lookup = getattr(slots, "get", None) if slots is not None else None
    if lookup is None:
        return None
    slot = session_key.partition(":")[2] if ":" in session_key else session_key
    if not slot:
        return None
    return lookup(slot)


def _caller_class_refusal(request: web.Request, session_key: str) -> str:
    """``""`` when this caller CLASS may hold a host-wide view, else why not.

    Mirrors the caller side of
    :func:`~kiro_crew.dashboard.session_control.authorize_target`, importing its
    constants so a change to what that gate excludes reaches this one instead of
    leaving two spellings to drift.

    Each class is refused for the reason that gate refuses it, and the reasons are
    about where the answer LANDS rather than about how much a session is trusted: a
    scheduled run has nobody watching what it did with a process roster; an
    app-scoped session belongs to its app; an incognito or temporary session was
    created to leave and learn nothing; and a channel-linked or mirrored session's
    conversation is a channel thread, so a host-wide view read into it is published
    to whoever is in that channel. A cron tab's link is exempt because it names the
    job's own run transcript and republishes to nobody.
    """
    from kiro_crew.dashboard.session_control import (
        CRON_LINK_PREFIX,
        UNATTENDED_SLOT_PREFIXES,
        _has_channel_mirror,
    )

    if session_key.startswith(("cron:", "taskrunner:")) or session_key.split(":", 1)[-1].startswith(
        UNATTENDED_SLOT_PREFIXES
    ):
        return "an unattended session (a scheduled run) gets no host-wide debug view"
    state = request.app.get("state")
    slot = _live_slot(state, session_key)
    if slot is None:
        return "the calling session names no live slot, so it cannot be granted a host-wide view"
    if getattr(slot, "_app", ""):
        return "an app-owned session gets no host-wide debug view"
    if getattr(slot, "memory_mode", "persistent") != "persistent":
        return (
            "an incognito or temporary session gets no host-wide debug view; that "
            "session is meant to leave and learn nothing"
        )
    link = getattr(slot, "linked_session_key", "")
    if link and not link.startswith(CRON_LINK_PREFIX):
        return (
            "a channel-linked session gets no host-wide debug view; what it reads "
            "lands in that channel's thread"
        )
    if _has_channel_mirror(state, slot):
        return (
            "a session mirrored to a channel gets no host-wide debug view; what it "
            "reads lands in front of that channel's audience"
        )
    return ""


def _owner_session_refusal(request: web.Request, session_key: str) -> str:
    """``""`` when *session_key* is the owner at a dashboard tab, else the reason.

    Four conditions, each ruling out a caller that is not the person at their own
    tab: a ``dashboard:`` key, so a headless or channel-bound caller is refused by
    the namespace it carries rather than by a list of what it is not; no app owns
    it, derived by :func:`~kiro_crew.dashboard.token_auth.derive_caller_app` against
    the server-side registries, because an app agent granted this server arrives on
    the same transport as the person; nobody CREATED it, because an agent can mint a
    full dashboard session through the session-control create verb and that child
    carries a ``dashboard:`` key, a live slot, and no app of its own, so it satisfies
    every other condition here while being an agent's session rather than the
    owner's; and the session keeps persistent memory.

    ``_created_by`` is the gateway's own record of that mint, empty for a slot nobody
    asked for -- a person's own tab, a fork, a restore -- and restored for exactly
    this purpose after a restart, so a session an agent opened before the gateway
    bounced is still refused.
    """
    from kiro_crew.dashboard.token_auth import derive_caller_app

    if not session_key:
        return "the request carried no session identity"
    if not session_key.startswith("dashboard:"):
        return f"{session_key.split(':', 1)[0]}: is not the owner's own dashboard session"
    state = request.app.get("state")
    slot = _live_slot(state, session_key)
    if slot is None:
        return "the calling session names no live dashboard slot"
    slots = getattr(state, "_slots", None)
    jobs = getattr(getattr(state, "crons", None), "_jobs", None)
    subagents = getattr(getattr(state, "subagents", None), "_agents", None)
    if derive_caller_app(slots, session_key, jobs, subagents):
        return "an app-owned session is not the owner's own dashboard session"
    if str(getattr(slot, "_created_by", "") or ""):
        return "a session created by an agent is not the owner's own dashboard session"
    if getattr(slot, "is_restricted", False):
        return "an incognito or temporary session is not the owner's own dashboard session"
    return ""


def _spawn_tree_keys(state: Any, root_key: str) -> set[str]:
    """*root_key* plus the keys of every session it spawned, at any depth.

    Walked from ``parent_session_key`` on the subagent registry's own records, so
    the tree this scope trusts is the tree the gateway actually dispatched. Bounded
    by the registry's size and cycle-guarded by the visited set, because a record
    whose parent chain loops would otherwise spin here.

    A record's session key is ``conversation_key`` when it has one and
    ``subagent:<id>`` otherwise, which is the precedence the runtime itself applies
    (``subagent.py``, where a live handle is matched by
    ``info.conversation_key or f"subagent:{info.id}"``). Deriving the key from ``id``
    ALONE dropped every continued subagent out of this set, and the failure was
    silent in the worst direction: the parent's own ``debug_refusals`` read came back
    short rather than refused, so a caller would conclude a child had no refusals
    when the scope simply never covered it. All spellings are added rather than one,
    because the parent chain is matched against whichever the registry recorded.
    """
    keys = {root_key}
    agents = getattr(getattr(state, "subagents", None), "_agents", None)
    values = getattr(agents, "values", None) if agents is not None else None
    if values is None:
        return keys
    records = list(values())
    # Repeat until the frontier stops growing: a grandchild's parent may be read
    # before its parent joins the set, so one pass is not enough and the number of
    # passes cannot exceed the tree's depth.
    for _ in range(len(records) + 1):
        grew = False
        for record in records:
            parent = str(getattr(record, "parent_session_key", "") or "")
            child_id = str(getattr(record, "id", "") or "")
            conversation = str(getattr(record, "conversation_key", "") or "")
            if not parent or parent not in keys:
                continue
            if not child_id and not conversation:
                continue
            spellings = [conversation] if conversation else []
            if child_id:
                spellings += [f"subagent:{child_id}", child_id]
            for spelling in spellings:
                if spelling and spelling not in keys:
                    keys.add(spelling)
                    grew = True
        if not grew:
            break
    return keys


async def _authorize_debug_read(
    request: web.Request, view: str, operation: str, *, target_session: str = ""
) -> web.Response | None:
    """``None`` when the caller may take this reading, else the refusal.

    The transport check is LOAD-BEARING, not a re-assert of the routing table. Being
    on ``_STRICT_INTERNAL_API_PATHS`` does not mean the secret was checked: for a
    LOOPBACK request carrying no ``X-Internal-Secret``, ``token_auth_middleware``
    falls through to ordinary cookie auth and calls this handler on success. So a
    same-machine tab, and a forwarded one once remote access is on, both arrive here
    with a valid cookie and no secret; this branch is the only thing that refuses
    them. Unlike the crew log there is no cookie-only door to send them to -- the
    dashboard has no debug panel -- so a browser is simply refused.

    It tests ``request["internal_auth"]`` and NOT the presence of the header.
    ``token_auth_middleware`` sets that key only after a constant-time match on the
    secret, so it is the one signal that separates "the internal loopback caller
    authenticated" from "some auth ran and the request happens to carry a header".
    Reading the header instead would accept a cookie-authenticated caller that
    merely attaches one, which is the same reasoning ``handlers/agent_panel``
    records for its own session-key claim: the header is an identity CLAIM, so the
    grant has to come from the transport.

    Every read is audited, granted and denied alike, including the denials that
    refuse a caller before its identity is settled. Those are the ones an operator
    most wants: a secret-less request at an MCP-only route, and a request naming
    another component, are both the shape of an attempted boundary crossing.
    ``request_origin`` is resolved FIRST so they can be recorded, and it clamps an
    unrecognized component to ``unknown-internal``, so reading it early cannot let
    an unauthenticated caller name itself into the audit log.
    """
    from kiro_crew.dashboard.token_auth import request_origin

    source, caller = request_origin(request, what="debug read", log=logger)
    if not request.get("internal_auth"):
        refusal = (
            "these reads are internal-transport only; the dashboard has no debug "
            "panel and a browser has no door here"
        )
        _audit_debug_read(caller, source, operation, "denied", refusal)
        return _forbidden(refusal)
    if caller != DEBUG_MCP_CALLER:
        refusal = f"this route serves {DEBUG_MCP_CALLER}; the request named {caller!r}"
        _audit_debug_read(caller, source, operation, "denied", refusal)
        return _forbidden(refusal)
    session_key = _caller_session_key(request)
    if not session_key:
        refusal = (
            "this read needs a session identity and the request carried none; only a "
            "session the gateway can name may read debug state"
        )
        _audit_debug_read(caller, source, operation, "denied", refusal)
        return _forbidden(refusal)

    class_refusal = _caller_class_refusal(request, session_key)
    if view in HOST_WIDE_VIEWS:
        # Host-wide: the owner's own tab, and its class must allow it too. The class
        # test gates the owner arm as well, which is deliberate rather than symmetry
        # for its own sake -- a mirrored owner tab republishes every turn, so the one
        # session entitled to this view would become its publisher.
        refusal = class_refusal or _owner_session_refusal(request, session_key)
        if refusal:
            _audit_debug_read(caller, source, operation, "denied", refusal)
            return _forbidden(refusal)
        _audit_debug_read(caller, source, operation, "granted", "")
        return None

    wants = (target_session or SELF).strip()
    if wants != SELF and wants != session_key:
        # Naming ANOTHER session explicitly. The class test runs FIRST and is never
        # skipped by lineage; only then may the dispatch tree or the owner arm admit
        # it.
        if class_refusal:
            _audit_debug_read(caller, source, operation, "denied", class_refusal)
            return _forbidden(class_refusal)
        state = request.app.get("state")
        if wants not in _spawn_tree_keys(state, session_key):
            if _owner_session_refusal(request, session_key):
                refusal = (
                    "this read is scoped to your own refusals and those of the "
                    "sessions you spawned, and this request does not fall inside "
                    "that scope; the owner's own dashboard session reads any session"
                )
                _audit_debug_read(caller, source, operation, "denied", refusal)
                return _forbidden(refusal)
    # ``self`` is always admitted, because a caller's OWN rows are always its own to
    # read -- it learns nothing it did not already experience. What ``self`` must NOT
    # do is silently widen to the spawn tree for a caller whose class forbids reading
    # past itself; the route NARROWS it instead of refusing it, so a channel-linked
    # session still gets its own answer. ``_refusal_scope_keys`` is the one place
    # that decision lives.
    _audit_debug_read(caller, source, operation, "granted", "")
    return None


def _refusal_scope_keys(request: web.Request, session_key: str, wants: str) -> set[str] | None:
    """The session keys a ``refusals`` read may cover, or ``None`` for every key.

    Split out of the route so the widening decision and the class test cannot drift
    apart. ``self`` widens to the caller's spawn tree ONLY when the caller's class
    permits reading past its own rows: a channel-linked or mirrored session asking
    for ``self`` gets exactly its own rows, because its children's refusals would
    land in that channel's thread. Narrowing rather than refusing is what keeps the
    tool useful to the caller class that needs it most.

    ``None`` -- no narrowing at all -- is reachable only for the owner at a dashboard
    tab whose own class allows it, which is the same pair of tests the gate ran.
    """
    state = request.app.get("state")
    if wants == SELF:
        if _caller_class_refusal(request, session_key):
            return {session_key}
        return _spawn_tree_keys(state, session_key)
    if wants == "*":
        if _caller_class_refusal(request, session_key) or _owner_session_refusal(
            request, session_key
        ):
            return {session_key}
        return None
    return {wants}


def _stale_grant_refusal(
    request: web.Request, view: str, operation: str, *, reads_past_own: bool = True
) -> web.Response | None:
    """``None`` when the grant still holds, else the refusal that overtook it.

    Called after the offload that BUILDS a payload and before that payload is
    returned. :func:`_authorize_debug_read` decides on state read BEFORE the read,
    and every one of these routes then suspends: ``gateway`` spawns git and probes a
    socket, the others fold a store or sample an interpreter. A caller can acquire a
    channel mirror or lose its persistent memory inside that window, so a verdict
    taken before it would deliver host-wide state INTO a session that is publishing
    by the time it lands.

    What this buys, stated exactly, because a wider claim would be false: the route
    never hands host-wide state to a caller that is publishing AT THE MOMENT OF THE
    HANDOFF. That is an act this route performs and therefore controls. What it does
    NOT buy is a caller that was entitled at the handoff and acquires a mirror
    afterwards -- it holds the payload in context and nothing at turn emission
    inspects a turn for host state. Those are different in kind, not merely in
    timing, and closing the second needs provenance on delivered content, which does
    not exist. ``crew_log`` records the same residual for the same reason.

    Re-reading can only ADD a refusal: every class test here moves away from
    permissive, never toward it.

    ``reads_past_own`` is what keeps this honest rather than merely strict. It must
    apply exactly the tests the GATE applied: a ``refusals`` read of the caller's own
    rows was admitted with NO class test, because a session reading its own refusals
    learns nothing it did not already experience. Re-testing the class here would
    refuse a read the gate deliberately allowed, and a channel-linked session would
    lose the one view built for it. The route passes False for that case, and the
    scope was already narrowed to the caller's own key by
    :func:`_refusal_scope_keys`.
    """
    if not reads_past_own:
        return None
    session_key = _caller_session_key(request)
    refusal = _caller_class_refusal(request, session_key)
    if view in HOST_WIDE_VIEWS and not refusal:
        refusal = _owner_session_refusal(request, session_key)
    if not refusal:
        return None
    from kiro_crew.dashboard.token_auth import request_origin

    source, caller = request_origin(request, what="debug read", log=logger)
    _audit_debug_read(caller, source, operation, "denied", refusal)
    return _forbidden(refusal)


def _audit_debug_read(caller: str, source: str, operation: str, outcome: str, error: str) -> None:
    """SEL for one debug read.

    Records GRANTS as well as denials, unlike the browser owner gate: a granted read
    of host or cross-session state is the event an operator asked about — "what did
    the agent look at" has no other answer.
    """
    try:
        from kiro_crew.sel import sel as _sel

        _sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome=outcome,
            source=source,
            error=error,
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


# --------------------------------------------------------------------------- #
# GET /api/debug/gateway
# --------------------------------------------------------------------------- #


def _proc_start_time() -> float | None:
    """This process's start time as a unix timestamp, or ``None`` off Linux.

    Field 22 of ``/proc/self/stat`` is the start time in clock ticks since boot, so
    it is added to the boot time from ``/proc/stat``. Read rather than guessed from
    a module-level constant recorded at import: a gateway that was re-executed in
    place would report the new import's time and hide exactly the staleness this
    route exists to expose. ``None`` where it cannot be read, never a guess.
    """
    try:
        with open("/proc/self/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
        # The comm field can contain spaces and parentheses, so fields are counted
        # from AFTER the last ')' rather than by splitting the whole line.
        tail = raw[raw.rfind(")") + 1 :].split()
        ticks = float(tail[19])
        clock = os.sysconf("SC_CLK_TCK")
        with open("/proc/stat", "rb") as handle:
            for line in handle.read().decode("utf-8", "replace").splitlines():
                if line.startswith("btime "):
                    return float(line.split()[1]) + (ticks / clock)
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    return None


def _head_commit_time() -> float | None:
    """HEAD's commit time in the package's repo, or ``None``.

    Compared against the gateway's start time, this is what answers "is the running
    gateway older than my fix". A subprocess, which the periodic recorder may not
    spawn — but this is an on-demand route answering one question, not the sampling
    path, and the fingerprint helper beside it already runs git. Bounded by a short
    timeout and answers ``None`` on any failure, including a checkout that is not a
    repo at all.
    """
    import subprocess

    try:
        from kiro_crew.code_fingerprint import _PACKAGE_ROOT

        done = subprocess.run(
            ["git", "log", "-1", "--format=%ct"],
            cwd=str(_PACKAGE_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ImportError):
        return None
    if done.returncode != 0:
        return None
    try:
        return float(done.stdout.strip())
    except ValueError:
        return None


def _systemd_drop_ins() -> list[str]:
    """The names of the systemd drop-in files in effect, user scope and system.

    NAMES only, never contents: a drop-in can carry an ``Environment=`` line, and
    the whole point of the four-key environment allowlist elsewhere in this module
    would be undone by dumping one here. A name is enough to answer "is there an
    override I forgot about", which is the question.
    """
    names: list[str] = []
    try:
        from kiro_crew.service.linux import SERVICE_NAME, USER_UNIT_SUBDIR
    except Exception:
        return names
    candidates = [
        Path.home() / USER_UNIT_SUBDIR / f"{SERVICE_NAME}.service.d",
        _SYSTEM_UNIT_ROOT / f"{SERVICE_NAME}.service.d",
    ]
    for directory in candidates:
        try:
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                if entry.suffix == ".conf":
                    names.append(f"{directory.name}/{entry.name}")
        except OSError:
            continue
    return names


def _recorder_health() -> dict[str, Any]:
    """The diagnostic recorder's health, or a stated absence.

    Lazy import, and an absent package is reported as a FACT rather than as an
    error: on this build the recorder does not exist yet, and a route that raised
    here would make ``debug_gateway`` — the one tool that works when nothing else
    does — the first casualty of the missing module.
    """
    try:
        from kiro_crew.diag.recorder import get_recorder
    except Exception:
        return {"available": False, "reason": DIAG_UNAVAILABLE}
    try:
        recorder = get_recorder()
        if recorder is None:
            return {"available": True, "running": False}
        health = recorder.health()
        return {"available": True, **(health if isinstance(health, dict) else {})}
    except Exception as exc:  # pragma: no cover - a health probe is not a verdict
        logger.debug("recorder health probe failed", exc_info=True)
        return {"available": True, "error": type(exc).__name__}


def _daemon_block() -> dict[str, Any]:
    """The MCP gateway daemon's revision beside this one.

    The failure this answers is remote from its cause: a daemon that outlived a code
    change keeps handing out pooled backends built from the old checkout, and the
    symptom is a directive tool reporting success while the gateway logs
    ``not_derivable``. Putting the two fingerprints side by side is what makes that
    one glance instead of an investigation.
    """
    from kiro_crew.code_fingerprint import code_fingerprint

    mine = code_fingerprint()
    try:
        from kiro_crew.mcp_gateway.daemon_control import describe_daemon

        info = describe_daemon()
    except Exception:
        return {"running": None, "matches_this_install": None}
    if info is None:
        return {"running": False, "matches_this_install": None}
    theirs = getattr(info, "fingerprint", "") or ""
    return {
        "running": True,
        "pid": getattr(info, "pid", None),
        "fingerprint": theirs or "unknown (pre-fingerprint build)",
        "matches_this_install": bool(theirs) and theirs == mine,
        "owner_pid": getattr(info, "owner_pid", None),
        "owner_alive": getattr(info, "owner_alive", None),
    }


async def api_debug_gateway(request: web.Request) -> web.Response:
    """GET /api/debug/gateway — which code is actually running.

    The whole payload is assembled OFF the event loop. Every source in it blocks:
    ``_head_commit_time`` spawns git, ``_systemd_drop_ins`` lists directories,
    ``_daemon_block`` probes a unix socket, and ``code_fingerprint`` walks the
    package tree on its first call. Assembling that on the loop would stall the
    gateway for the length of a git invocation on every call — a debug tool that
    causes the symptom it exists to measure. ``describe_daemon`` additionally
    REFUSES to run inside a running loop (it would need the async probe), so on the
    loop it returns nothing and the answer would silently claim no daemon.
    """
    denied = await _authorize_debug_read(request, "gateway", "debug.gateway")
    if denied is not None:
        return denied
    import asyncio

    from kiro_crew.platform import redact_via_context as redact

    payload = await asyncio.to_thread(_gateway_payload)
    stale = _stale_grant_refusal(request, "gateway", "debug.gateway")
    if stale is not None:
        return stale
    return web.json_response(json.loads(redact(json.dumps(payload))))


def _gateway_payload() -> dict[str, Any]:
    """The gateway description. Blocking; runs in a thread."""
    from kiro_crew.code_fingerprint import code_fingerprint

    started = _proc_start_time()
    head = _head_commit_time()
    return {
        "pid": os.getpid(),
        "started_at": started,
        "uptime_secs": (time.time() - started) if started else None,
        "code_fingerprint": code_fingerprint(),
        "head_commit_time": head,
        # The comparison, computed here rather than left to the caller: "is the
        # gateway older than HEAD" is the actual question, and an agent doing the
        # subtraction itself is an agent that can get the sign wrong.
        "gateway_predates_head": (
            bool(started and head and started < head) if (started and head) else None
        ),
        "mcp_gateway_daemon": _daemon_block(),
        "systemd_drop_ins": _systemd_drop_ins(),
        "recorder": _recorder_health(),
    }


# --------------------------------------------------------------------------- #
# GET /api/debug/refusals
# --------------------------------------------------------------------------- #


def _sel_files() -> list[Path]:
    """The security event log and its closed segments, newest first.

    Read through the SEL singleton's own directory because the gateway IS the log's
    writer: this is the one process entitled to read it, which is exactly why the
    route holds this read and the agent's file tools cannot. An agent shell sees
    empty placeholders mounted over the fenced path whatever the host holds, so a
    reading taken there proves nothing — another reason the question belongs to the
    gateway.
    """
    from kiro_crew.sel import sel as _sel

    try:
        instance = _sel()
        base = getattr(instance, "_dir", None)
        segments = getattr(instance, "_segment_dir", None)
    except Exception:
        return []
    files: list[Path] = []
    if base is not None:
        live = Path(base) / "security_events.jsonl"
        if live.is_file():
            files.append(live)
    if segments is not None:
        try:
            closed = sorted(
                (p for p in Path(segments).iterdir() if p.suffix == ".jsonl"),
                reverse=True,
            )
            files.extend(closed)
        except OSError:
            pass
    return files


def _parse_window(since: str) -> float | None:
    """*since* as a unix timestamp, accepting ISO 8601 or a relative window.

    Relative windows are accepted because that is how the question is actually
    asked — "what was refused in the last half hour" — and making a caller compute
    a timestamp for that is how a debug tool acquires its own arithmetic bugs.
    """
    from datetime import datetime

    value = (since or "").strip()
    if not value:
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if value[-1] in units and value[:-1].replace(".", "", 1).isdigit():
        return time.time() - float(value[:-1]) * units[value[-1]]
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _read_refusals(*, keys: set[str] | None, since_ts: float | None, limit: int) -> dict[str, Any]:
    """Refusal rows from the security event log, newest first. Blocking.

    Walks files newest first and stops at *limit* or at
    :data:`MAX_SEL_SCAN_BYTES`, whichever comes first, and SAYS which — a debug
    answer that was cut short and does not admit it is worse than a short one,
    because the absence of a refusal is what a caller would conclude.

    ``keys`` of ``None`` means no session narrowing, which only the owner arm
    reaches; otherwise a row is kept when its ``caller_identity`` is in the set.
    """
    from datetime import datetime

    from kiro_crew.sel import _redact_text

    rows: list[dict[str, Any]] = []
    scanned = 0
    truncated = False
    for path in _sel_files():
        if len(rows) >= limit or scanned >= MAX_SEL_SCAN_BYTES:
            truncated = True
            break
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        scanned += len(raw)
        for line in reversed(raw.decode("utf-8", "replace").splitlines()):
            if len(rows) >= limit:
                truncated = True
                break
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if str(row.get("event_type") or "") not in REFUSAL_EVENT_TYPES:
                continue
            if str(row.get("outcome") or "") not in REFUSAL_OUTCOMES:
                continue
            identity = str(row.get("caller_identity") or "")
            if keys is not None and identity not in keys:
                continue
            stamp = str(row.get("timestamp") or "")
            if since_ts is not None:
                try:
                    if datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() < since_ts:
                        continue
                except ValueError:
                    continue
            kind = _classify_refusal(row)
            entry: dict[str, Any] = {
                "ts": stamp,
                "session": identity,
                "tool": str(row.get("operation") or ""),
                "class": kind,
                "outcome": str(row.get("outcome") or ""),
                # Already redacted on write by SEL; redacted AGAIN on read because
                # the two passes protect against different things -- the write pass
                # against what was stored, this one against a pattern the current
                # build recognizes and the build that wrote the row did not.
                "reason": _redact_text(str(row.get("error") or ""))[:800],
            }
            if kind == "denied_rule":
                diagnostic = _refusal_diagnostic_id(row)
                if diagnostic:
                    entry["refusal_diagnostic"] = diagnostic
            if kind == "unverifiable_path":
                entry["retryable"] = True
                entry["action"] = (
                    "NOT a match: the path was never judged. Retry the identical "
                    "call after ~30s; do not re-spell it."
                )
            rows.append(entry)
    classes: dict[str, int] = {}
    for row in rows:
        classes[row["class"]] = classes.get(row["class"], 0) + 1
    return {
        "refusals": rows,
        "returned": len(rows),
        "by_class": classes,
        "truncated": truncated,
        "scanned_bytes": scanned,
        # The live path-gate counters (probes, cache hits, budget timeouts) are
        # DEFERRED: security/paths.py is being changed concurrently by the TTL and
        # positive-cache work, so an accessor added now would count the wrong thing.
        # A follow-up PR adds path_gate_stats() once that lands, the recorder
        # registers it as a source, and this block becomes real. Stated in the
        # answer rather than omitted, so a caller is not left wondering whether the
        # gateway simply had nothing to report.
        "live": {"available": False, "reason": "path-gate counters land in a follow-up change"},
    }


async def api_debug_refusals(request: web.Request) -> web.Response:
    """GET /api/debug/refusals — why calls were refused, each one classed."""
    import asyncio

    wants = (request.query.get("session") or SELF).strip()
    denied = await _authorize_debug_read(
        request, "refusals", "debug.refusals", target_session=wants
    )
    if denied is not None:
        return denied
    session_key = _caller_session_key(request)
    # One place decides how wide ``self`` and ``*`` go, and it re-consults the class
    # test rather than trusting the gate's verdict: a caller whose class forbids
    # reading past its own rows is NARROWED to them, not refused.
    keys = _refusal_scope_keys(request, session_key, wants)
    try:
        limit = int(request.query.get("last") or 50)
    except ValueError:
        return web.json_response(
            {"error": "last must be an integer", "code": "bad_range"}, status=400
        )
    limit = max(1, min(limit, MAX_REFUSALS))
    since_ts = _parse_window(request.query.get("since") or "")
    payload = await asyncio.to_thread(_read_refusals, keys=keys, since_ts=since_ts, limit=limit)
    # The re-check applies only when this read reached PAST the caller's own rows.
    # A self-narrowed read did not, so re-testing the class would refuse exactly the
    # view a channel-linked session is entitled to.
    stale = _stale_grant_refusal(
        request,
        "refusals",
        "debug.refusals",
        reads_past_own=keys is None or keys != {session_key},
    )
    if stale is not None:
        return stale
    payload["scope"] = wants
    return web.json_response(payload)


# --------------------------------------------------------------------------- #
# The three diag-backed reads
# --------------------------------------------------------------------------- #
#
# Authorized FIRST and refused second, deliberately. A caller with no entitlement
# must learn that from the 403 rather than from a 501: answering "not implemented"
# to an unauthorized caller would tell it which capabilities this build has, and
# the order is what keeps the two answers from leaking into each other.


def _diag_module(name: str) -> Any:
    """The named ``kiro_crew.diag`` submodule, or ``None`` when absent.

    LAZY, and a failure is ``None`` rather than an exception: this module is on the
    gateway's boot path and a build without the diag package must not fail to import
    the dashboard. A test pins that a clean interpreter importing this module loads
    no ``kiro_crew.diag`` submodule at all.
    """
    try:
        import importlib

        return importlib.import_module(f"kiro_crew.diag.{name}")
    except Exception:
        return None


async def api_debug_threads(request: web.Request) -> web.Response:
    """GET /api/debug/threads — GIL and thread state, or 501 without diag."""
    denied = await _authorize_debug_read(request, "threads", "debug.threads")
    if denied is not None:
        return denied
    threads = _diag_module("threads")
    if threads is None:
        return _diag_unavailable()
    import asyncio

    from kiro_crew.platform import redact_via_context as redact

    mode = (request.query.get("mode") or "now").strip()
    try:
        if mode == "dumps":
            name = (request.query.get("read") or "").strip()
            result = (
                await asyncio.to_thread(threads.read_dump, name)
                if name
                else await asyncio.to_thread(threads.list_dumps)
            )
            payload: dict[str, Any] = {"mode": mode, "dumps": result}
        elif mode == "sample":
            seconds = min(float(request.query.get("seconds") or 5), 60.0)
            hz = max(1, min(int(request.query.get("hz") or 100), 1000))
            deep = request.query.get("deep") in ("1", "true", "True")
            payload = {
                "mode": mode,
                **(await asyncio.to_thread(threads.sample, seconds, hz, deep)),
            }
        else:
            payload = {"mode": "now", **(await asyncio.to_thread(threads.ledger_now))}
    except ValueError as exc:
        return web.json_response({"error": str(exc), "code": "bad_range"}, status=400)
    except FileNotFoundError as exc:
        # A dump named by an earlier listing can be pruned, or rotated away, before
        # this read reaches it. That is an ordinary race between two reads, not a
        # fault in the gateway, so it answers 404 rather than 500.
        return web.json_response({"error": str(exc), "code": "dump_missing"}, status=404)
    except OSError as exc:
        return web.json_response({"error": str(exc), "code": "dump_unreadable"}, status=503)
    stale = _stale_grant_refusal(request, "threads", "debug.threads")
    if stale is not None:
        return stale
    return web.json_response(json.loads(redact(json.dumps(payload, default=str))))


#: Guards the one-time construction of the process roster's rate baseline.
_RATE_BASELINE_LOCK = threading.Lock()
_RATE_BASELINE: Any = None


def _process_rate_baseline(procs: Any) -> Any:
    """The one rate baseline this route keeps, built on first use.

    ``cpu_pct`` and ``runq_wait_pct`` are deltas, so whoever reads them has to
    hold the previous roster; :mod:`kiro_crew.diag.procs` deliberately holds no
    background state, which makes this route the owner. The route is also the
    honest owner of the cadence: the gap the delta measures is the gap between
    two reads of it.

    Built lazily rather than at import, because importing this module must not
    pull in the diag package.
    """
    global _RATE_BASELINE
    with _RATE_BASELINE_LOCK:
        if _RATE_BASELINE is None:
            _RATE_BASELINE = procs.RateBaseline()
        return _RATE_BASELINE


async def api_debug_processes(request: web.Request) -> web.Response:
    """GET /api/debug/processes — the process family, or 501 without diag."""
    denied = await _authorize_debug_read(request, "processes", "debug.processes")
    if denied is not None:
        return denied
    procs = _diag_module("procs")
    if procs is None:
        return _diag_unavailable()
    import asyncio

    from kiro_crew.platform import redact_via_context as redact

    filters: dict[str, Any] = {}
    for key in ("kind", "owner"):
        if request.query.get(key):
            filters[key] = request.query[key]
    for flag in ("orphan_only", "include_env"):
        if request.query.get(flag) in ("1", "true", "True"):
            filters[flag] = True
    fmt = (request.query.get("format") or "tree").strip()

    def _scan() -> dict[str, Any]:
        # scan_with_rates, not scan: cpu_pct and runq_wait_pct are deltas, and
        # the baseline they need is this route's to hold.
        return procs.tree(procs.scan_with_rates(_process_rate_baseline(procs)), fmt, **filters)

    payload = await asyncio.to_thread(_scan)
    stale = _stale_grant_refusal(request, "processes", "debug.processes")
    if stale is not None:
        return stale
    return web.json_response(json.loads(redact(json.dumps(payload, default=str))))


async def api_debug_snapshots(request: web.Request) -> web.Response:
    """GET /api/debug/snapshots — the recorded series, or 501 without diag."""
    denied = await _authorize_debug_read(request, "snapshots", "debug.snapshots")
    if denied is not None:
        return denied
    recorder_mod = _diag_module("recorder")
    if recorder_mod is None:
        return _diag_unavailable()
    import asyncio

    from kiro_crew.platform import redact_via_context as redact

    recorder = recorder_mod.get_recorder()
    if recorder is None:
        return web.json_response(
            {"error": "the diagnostic recorder is not running", "code": "recorder_off"}, status=422
        )
    fields = request.query.getall("fields", []) or None
    try:
        payload = await asyncio.to_thread(
            recorder.query,
            request.query.get("since") or None,
            request.query.get("until") or None,
            request.query.get("around") or None,
            request.query.get("radius") or None,
            fields,
            request.query.get("events_only") in ("1", "true", "True"),
            request.query.get("cursor") or None,
        )
    except ValueError as exc:
        # A malformed window value (``radius=5x``, an unparsable ``around``) is
        # the caller's error and names its field; it must not read as a crash.
        return web.json_response({"error": str(exc), "code": "bad_range"}, status=400)
    stale = _stale_grant_refusal(request, "snapshots", "debug.snapshots")
    if stale is not None:
        return stale
    return web.json_response(json.loads(redact(json.dumps(payload, default=str))))


# ``api_debug_refusals`` is named as member-scoped; the other four are not, and the
# split is the point. That wrapper makes every ``api_*`` an OWNER surface, which
# refuses a private member's scoped caller before the handler runs -- correct for the
# four host-wide views, which are owner-only anyway, and wrong for ``refusals``, whose
# whole purpose is to serve a NON-owner caller its own rows. Left wrapped, a V2
# member asking why its own call was refused would get a 403 before its per-session
# authorization ever ran. The helper's contract is exactly this: a route that
# "verifies and scopes its own caller" belongs in the set, and
# ``_authorize_debug_read`` plus ``_refusal_scope_keys`` are that verification.
guard_owner_surface_routes(globals(), member_scoped=frozenset({"api_debug_refusals"}))
