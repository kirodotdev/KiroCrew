"""What one ACP session's MCP servers actually reported.

Kiro Crew holds two different views of MCP, and reading one as the other is the
confusion this module exists to end:

* the **configured** view — what an agent spec on disk declares, and what the
  gateway's own probe can start on this host. Both answer a question about the
  HOST. ``/api/mcp/active`` and ``/api/mcp/probe`` are this view.
* the **reported** view — the ``server_initialized`` / ``server_init_failure`` /
  ``oauth_request`` frames a PARTICULAR session received from its backend.

Only the second speaks for a session. Both ACP transports already receive those
frames at session init and both already reduce them — but only into a timeout
error string, after which the frames are dropped. A session that started with a
server missing therefore had no way to say so, and a dashboard showing the
configured view looked like it was answering the session question.

This holds the reported view for the life of a session so the dashboard can
show it beside the configured one. Three properties are load-bearing:

**A missing report is not proof of absence.** Both drains are time-bounded, so a
slow server can report after the drain gives up, and a frame can arrive mid-turn
(after an OAuth callback completes). Callers must render an unreported server as
*not reported yet*, never as *not mounted* — replacing one false authority with
another is the failure this whole view is meant to remove.

**Reports are a superset of the roster.** The backend initializes the agent
spec's own servers as well as the ones Kiro Crew injects on the wire, so a name
can be reported without appearing in :attr:`configured`. That is information (it
is how a spec-declared server proves it started), not an inconsistency.

**A server's state moves.** ``failure`` then ``initialized`` is the normal shape
of a server that needed authorization, so a name is recorded in exactly one
bucket, last frame winning, rather than accumulating in several at once.

The sanitizers here are deliberately not shared with the equivalents in
:mod:`kiro_crew.acp.runtime`: those bound an exception string built from
runtime-wide state, these bound a per-session payload that reaches a browser, and
the two have different caps for that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kiro_crew.acp._dispatch import classify_notification
from kiro_crew.acp.types import (
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    METHOD_KAS_MCP_STATUS,
    METHOD_KAS_TOOLS_CHANGED,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.mcp_refs import parse_tools_refs
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# A server name is config-derived, so an installed app chooses it: it reaches a
# log line, a JSON payload and a DOM node. Bound its length here so one hostile
# name cannot dominate any of those sinks. Generous, because the frontend matches
# these names EXACTLY against the configured list: a truncated name matches
# nothing and its row reads "no report" for a server that did report. The bound
# only has to stop a pathological name, so it sits well above any real one.
_NAME_CAP = 128
# Shared with the unresolved-ref guard, whose refs are config-derived in exactly
# the same way and reach the same three sinks.
NAME_CAP = _NAME_CAP
# A failure text comes from the failing server's own startup and can carry a
# connection string. It is redacted first; this bounds what survives.
_ERROR_CAP = 240
# Servers per bucket. A stock install runs well under ten; the cap exists so a
# misconfigured host cannot push an unbounded list into every slots snapshot.
_BUCKET_CAP = 64

_ACTION_INITIALIZED = "mcp_server_initialized"
_ACTION_INIT_FAILURE = "mcp_server_init_failure"
_ACTION_OAUTH = "mcp_oauth_request"

#: Frame actions this report records. Any other notification is ignored.
REPORT_ACTIONS = frozenset({_ACTION_INITIALIZED, _ACTION_INIT_FAILURE, _ACTION_OAUTH})

# ``AcpEvent.kind`` -> bucket action, for frames that arrive mid-turn rather than
# during the init drain. The two vocabularies happen to spell these the same
# today; mapping them explicitly makes a rename of either a visible break here
# instead of a live path that silently stops recording.
_EVENT_ACTIONS = {
    EVENT_MCP_SERVER_INITIALIZED: _ACTION_INITIALIZED,
    EVENT_MCP_SERVER_INIT_FAILURE: _ACTION_INIT_FAILURE,
    EVENT_MCP_OAUTH_REQUEST: _ACTION_OAUTH,
}


def sanitize_sink_text(text: str, cap: int) -> str:
    """Redact, collapse whitespace, drop control characters, then truncate.

    Order matters: redaction runs first so a credential cannot be split across
    the truncation boundary and survive, and the control strip runs after the
    whitespace collapse so a redaction marker cannot reintroduce a line break
    into a log line or a payload.

    Public because it is the ONE cleaner for every config-derived string this
    package pushes at a sink -- a server name, a startup failure, an unresolved
    tool ref. :mod:`kiro_crew.acp.mcp_ref_guard` shares it rather than repeating
    the order above: two sanitizers with the same job are two that can drift, and
    the one that drifts is the one that stops redacting.
    """
    scrubbed, _ = redact_exfiltration_urls(text)
    scrubbed, _ = redact_credentials(scrubbed)
    return "".join(ch for ch in " ".join(scrubbed.split()) if ch.isprintable())[:cap]


def server_name_of(msg: JsonRpcMessage) -> str:
    """The sanitized MCP server name a notification names, or ``""``.

    Both spellings are accepted because the two frame families differ:
    registration frames carry ``serverName``, some builds send ``name``.
    """
    params = msg.params if isinstance(msg.params, dict) else {}
    raw = params.get("serverName") or params.get("name") or ""
    return sanitize_sink_text(str(raw), _NAME_CAP)


def roster_names(servers: Any) -> tuple[str, ...]:
    """Sanitized server names from a ``session/new`` ``mcpServers`` array.

    Accepts the wire shape directly (a list of dicts with a ``name``) and
    tolerates anything else by returning empty, so a caller can hand over
    whatever it sent without pre-validating it.
    """
    out: list[str] = []
    for entry in servers if isinstance(servers, list) else []:
        if not isinstance(entry, dict):
            continue
        name = sanitize_sink_text(str(entry.get("name") or ""), _NAME_CAP)
        if name and name not in out:
            out.append(name)
    return tuple(out[:_BUCKET_CAP])


def active_custom_agent(params: dict[str, Any], agent: str) -> dict[str, Any] | None:
    """The active descriptor as projected onto this session's outgoing wire."""
    agents = params.get("_meta", {}).get("kiro", {}).get("customAgents", [])
    for entry in agents:
        if entry.get("id") == agent:
            return entry
    return None


def required_managed_servers(params: dict[str, Any], agent: str) -> tuple[str, ...]:
    """Managed declarations actually sent for the ACTIVE agent and session."""
    names = set(roster_names(params.get("mcpServers")))
    active = active_custom_agent(params, agent)
    if active is not None:
        names.update(active.get("mcpServers", {}))
    return tuple(name for name in KIROCREW_BIN_MCP_SERVERS if name in names)


#: Terminal readiness state for a required server that only the ACTIVE AGENT's
#: ``mcpServers`` block declares, reported ``connected`` by a backend that stamps
#: no ``_meta.kiro.resource.source`` on any status entry. Released kiro-cli
#: 2.18.0 is such a backend (captured wire), and on it the two declaration sites
#: differ: an explicit session-level ``mcpServers`` injection wins over a
#: same-named global ``~/.kiro/settings/mcp.json`` server on ``session/new`` and
#: ``session/load`` alike, whereas an agent-block declaration is shadowed by the
#: global one on ``session/new`` and coexists with it under one name on
#: ``session/load`` -- all reporting under the session's own ``sessionId``. So an
#: injected name is positively the session's own; an agent-only name cannot be
#: told from the global server, and the barrier refuses rather than guess.
STATE_CONNECTED_WITHOUT_PROVENANCE = "connected without provenance"

_PROVENANCE_LIMIT = (
    "this kiro-cli reports no MCP server origin, so a server declared only by the "
    "active agent cannot be told from a same-named global one (a session-level "
    "injection would be trusted); use a kiro-cli release whose _kiro/mcp/status "
    "entries carry _meta.kiro.resource.source.origin"
)


def _server_source(server: dict[str, Any]) -> dict[str, Any] | None:
    """The ``_meta.kiro.resource.source`` block of one status entry, if any."""
    meta = server.get("_meta")
    kiro = meta.get("kiro") if isinstance(meta, dict) else None
    resource = kiro.get("resource") if isinstance(kiro, dict) else None
    source = resource.get("source") if isinstance(resource, dict) else None
    return source if isinstance(source, dict) else None


@dataclass
class KasMcpReadiness:
    """One activation's required servers; global and other-session state cannot satisfy it.

    Status and tool tags are full snapshots. Exposure -- the model can actually
    reach the connected server's tools -- is established by EITHER a non-empty
    ``tools`` catalog on the server's own ``connected`` status entry OR an
    ``@server/tool`` tag in the ``_kiro/tools/didChange`` snapshot. Neither is
    the callable spelling: ``@server/tool`` need not be the native function ID.

    Both are accepted because released kiro-cli versions differ on which one
    they send. Captured 2.18.0 / 2.20.0 emit both the catalog and the tags.
    Captured 2.22.0 (KAS 0.66.0) still lists the full catalog on the connected
    entry but its ``didChange`` snapshot carries only ``builtin`` tags -- no MCP
    tag ever arrives, with or without ``tool_search`` in the agent's tools. A
    barrier that took tags as the only exposure evidence timed out every
    session start on that release.

    ``injected`` names the required servers Crew put in the session-level
    ``mcpServers`` array itself (as opposed to the active agent's block). It is
    consulted only when the backend stamps no provenance at all -- see
    :data:`STATE_CONNECTED_WITHOUT_PROVENANCE` for the captured behaviour that
    makes an injected name trustworthy there and an agent-only name not.
    """

    session_id: str
    required: tuple[str, ...]
    tool_policy: dict[str, Any] | None = None
    injected: frozenset[str] = frozenset()
    states: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    #: Exposure evidence, kept per source because each snapshot is authoritative
    #: only for its own kind: a status snapshot replaces the catalog evidence,
    #: a tag snapshot replaces the tag evidence, and neither may retract the
    #: other's. Read the union through :attr:`advertised`.
    advertised_by_catalog: set[str] = field(default_factory=set)
    advertised_by_tag: set[str] = field(default_factory=set)
    intentionally_hidden: set[str] = field(default_factory=set)

    @property
    def advertised(self) -> set[str]:
        """Required servers with exposure evidence from either snapshot kind."""
        return self.advertised_by_catalog | self.advertised_by_tag

    def _needs_exposure(self, name: str, server: dict[str, Any]) -> bool:
        """Do not demand tags for tools the active agent deliberately hides.

        ``permissions`` controls approval, not exposure. Read only the projected
        ``tools`` / ``excludedTools`` and the backend's per-tool disabled flags.
        Missing or empty catalogs alone never establish an intentional restriction.
        """
        if self.tool_policy is None or "tools" not in self.tool_policy:
            return True
        raw = self.tool_policy["tools"]
        tools = ["*"] if raw == "*" else raw if isinstance(raw, list) else []
        excluded = self.tool_policy.get("excludedTools", [])
        if not isinstance(excluded, list):
            excluded = []
        tools = [tool for tool in tools if tool not in excluded]
        grant_all, refs = parse_tools_refs(tools)
        if not (grant_all or name in refs) or "*" in excluded or f"@{name}" in excluded:
            return False
        catalog = server.get("tools")
        if not isinstance(catalog, list) or not catalog:
            return True
        selected = []
        for tool in catalog:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                return True
            tag = f"@{name}/{tool['name']}"
            if grant_all or f"@{name}" in tools or tag in tools:
                selected.append((tag, tool))
        if not selected:
            return True
        return any(
            tool.get("disabled") is not True and tag not in excluded for tag, tool in selected
        )

    def record(self, msg: JsonRpcMessage) -> None:
        params = msg.params if isinstance(msg.params, dict) else {}
        if msg.id is not None or params.get("sessionId") != self.session_id:
            return
        if msg.is_method(METHOD_KAS_MCP_STATUS):
            servers = params.get("servers")
            if not isinstance(servers, list):
                return
            self.states.clear()
            self.errors.clear()
            self.intentionally_hidden.clear()
            entries = [server for server in servers if isinstance(server, dict)]
            # Servers whose connected entry carries its catalog: fresh exposure
            # evidence from this very snapshot (see the class docstring).
            catalogued: set[str] = set()
            # Whether this BACKEND speaks provenance at all is read off the whole
            # snapshot, not one entry: a backend that does (2.20.0 stamps every
            # entry, connecting included) and leaves one entry unstamped is
            # reporting a server that is not the client's, while one that stamps
            # nothing is a release from before the field existed.
            provenance = any(_server_source(server) is not None for server in entries)
            for server in entries:
                name = server.get("name")
                if not isinstance(name, str) or name not in self.required:
                    continue
                source = _server_source(server)
                if provenance and (source is None or source.get("origin") != "client"):
                    # A same-named inherited global server is not the private
                    # declaration Crew sent for this session.
                    continue
                state = server.get("status")
                if state not in ("connecting", "connected", "failed", "disabled"):
                    state = "unreported"
                if server.get("failedAuthorization") is True:
                    state = "authorization failed"
                elif state == "connected" and not provenance and name not in self.injected:
                    # Captured 2.18.0 wire: connected, catalog and tag present, no
                    # origin anywhere, and this name reached the backend only
                    # through the agent block -- the declaration site the global
                    # server shadows there. Reading it as ready could hand the
                    # session a server carrying another identity's session key
                    # and memory; refuse, naming the limit. A session-level
                    # injection is exempt because the same capture shows it
                    # winning over the global server on new and load.
                    state = STATE_CONNECTED_WITHOUT_PROVENANCE
                self.states[name] = state
                if not self._needs_exposure(name, server):
                    self.intentionally_hidden.add(name)
                catalog = server.get("tools")
                if state == "connected" and isinstance(catalog, list) and catalog:
                    catalogued.add(name)
                error = server.get("errorMessage")
                if isinstance(error, str):
                    self.errors[name] = sanitize_sink_text(error, _ERROR_CAP)
                if state == STATE_CONNECTED_WITHOUT_PROVENANCE:
                    self.errors[name] = _PROVENANCE_LIMIT
            # A reconnect must obtain fresh exposure evidence of either kind; a
            # connected entry that carries its catalog in this snapshot IS that
            # evidence.
            connected = {name for name in self.required if self.states.get(name) == "connected"}
            self.advertised_by_catalog.intersection_update(connected)
            self.advertised_by_catalog.update(catalogued)
            self.advertised_by_tag.intersection_update(connected)
        elif msg.is_method(METHOD_KAS_TOOLS_CHANGED):
            tags = params.get("tags")
            if not isinstance(tags, list):
                return
            # A full snapshot: it replaces the TAG evidence, so a tag that
            # disappears while the server stays connected is retracted as
            # before. It never touches the catalog evidence -- on a release whose
            # snapshot lists only builtin tags (2.22.0) that would clear it on the
            # very next frame and reopen the timeout this path was built to end.
            self.advertised_by_tag = {
                name
                for name in self.required
                if any(
                    isinstance(tag, dict)
                    and tag.get("source") == "mcp"
                    and isinstance(tag.get("tag"), str)
                    and tag["tag"].startswith(f"@{name}/")
                    for tag in tags
                )
            }

    @property
    def failure(self) -> str:
        for name in self.required:
            state = self.states.get(name)
            if state in (
                "failed",
                "disabled",
                "authorization failed",
                STATE_CONNECTED_WITHOUT_PROVENANCE,
            ):
                detail = self.errors.get(name)
                return f"{name}: {state}" + (f" ({detail})" if detail else "")
        return ""

    @property
    def pending(self) -> str:
        return ", ".join(
            f"{name}: "
            + (
                "tools not advertised"
                if self.states.get(name) == "connected"
                else self.states.get(name, "unreported")
            )
            for name in self.required
            if self.states.get(name) != "connected"
            or (name not in self.advertised and name not in self.intentionally_hidden)
        )


@dataclass
class McpSessionReport:
    """Mutable accumulator for one session's MCP registration frames.

    Buckets preserve first-seen order so a rendered list is stable across
    snapshots rather than reordering under the user.
    """

    #: Server names Kiro Crew put on the wire in this session's ``session/new``.
    #: Empty means Kiro Crew injected none — NOT that the session has none, since
    #: the backend also starts the agent spec's own servers.
    configured: tuple[str, ...] = ()
    #: Agent-spec ``@server`` refs that named no server this session receives, as
    #: :mod:`kiro_crew.acp.mcp_ref_guard` found them. A DIFFERENT claim from every
    #: bucket below, and the difference is what makes it worth a slot: those say
    #: what a configured server reported, this says the spec asked for a server
    #: nothing configured -- so there is no row for it to be missing FROM, which
    #: is exactly why the defect was invisible three times.
    unresolved_refs: tuple[str, ...] = ()
    _ready: list[str] = field(default_factory=list)
    _failed: list[str] = field(default_factory=list)
    _awaiting_auth: list[str] = field(default_factory=list)
    _failures: dict[str, str] = field(default_factory=dict)
    #: True once ``begin_session`` has run. Distinguishes "no session" from "a
    #: session that has reported nothing yet" — see ``payload``. Without it, the
    #: kiro-cli path (whose wire roster is empty by design) reads as no session.
    _started: bool = False

    def begin_session(self, servers: Any) -> None:
        """Start a report for a new session attempt, discarding any prior one.

        Named for what it does rather than for the roster it takes, because the
        discard is the load-bearing half. A ``session/load`` that fails still
        drains notifications first, so its frames land here; the client then
        falls back to ``session/new``, and without this the failed attempt's
        servers — INCLUDING ready ones — would be published as the replacement
        session's own. That is a false all-clear, the exact reading this view
        exists to remove, so a report must never span two attempts. Every caller
        is a session-establishment point, which is why the clear belongs here and
        not at any one of them.
        """
        self.configured = roster_names(servers)
        self.unresolved_refs = ()
        self._started = True
        self._ready.clear()
        self._failed.clear()
        self._awaiting_auth.clear()
        self._failures.clear()

    def include_configured(self, names: tuple[str, ...]) -> None:
        """Add active-agent declarations without restarting this session's report."""
        self.configured = roster_names([{"name": name} for name in (*self.configured, *names)])
        self._started = True

    def record_unresolved_refs(self, refs: Any) -> None:
        """Record the spec refs that named no server this session receives.

        Sanitized and capped on the same terms as a server name: a ref is
        config-derived, so an installed app chooses the text, and it reaches a log
        line, a JSON payload and a DOM node. Set rather than accumulated -- the
        guard evaluates the whole spec against the whole wire array in one pass,
        so a second call is a re-evaluation of the same question and replaces the
        answer instead of appending to it.

        Only strings are taken. The guard emits nothing else, and stringifying a
        non-string would put a row reading ``None`` or ``7`` in front of a user as
        though the spec had asked for a server by that name.
        """
        seen: list[str] = []
        for raw in refs if isinstance(refs, (list, tuple)) else ():
            if not isinstance(raw, str):
                continue
            ref = sanitize_sink_text(raw, _NAME_CAP)
            if ref and ref not in seen:
                seen.append(ref)
        self.unresolved_refs = tuple(seen[:_BUCKET_CAP])

    def record_frame(self, msg: JsonRpcMessage, *, owned: bool) -> bool:
        """Fold one notification in. Returns True when the report changed.

        Non-registration frames and frames with no server name are ignored, so
        a caller can hand over every notification it drains without filtering.

        ``owned`` is the caller's answer to "is this frame provably THIS
        session's?", and it is required rather than defaulted because only the
        caller knows its own transport. On an exclusive transport every frame
        belongs to the one session by construction. On the shared multiplexed
        runtime it does not: a frame naming no session is broadcast to every
        registered queue, and the runtime marks it (``fanout_no_owner``) only
        once MORE THAN ONE queue is registered — correct for a consumer that
        merely measures its own activity, but not for this one. A lone
        registered session would otherwise be handed a co-tenant's sessionless
        frame unmarked and publish that server as its own, which is the exact
        wrong answer in the one view whose whole purpose is to be the session's
        own. So the multiplexed caller must require the frame to NAME it, not
        merely to be unmarked.
        """
        if not owned:
            return False
        if msg.is_method(METHOD_KAS_MCP_STATUS):
            params = msg.params if isinstance(msg.params, dict) else {}
            servers = params.get("servers")
            if not isinstance(servers, list):
                return False
            changed = False
            for server in servers[:_BUCKET_CAP]:
                if not isinstance(server, dict):
                    continue
                name = sanitize_sink_text(str(server.get("name") or ""), _NAME_CAP)
                if not name:
                    continue
                status = server.get("status")
                if server.get("failedAuthorization") is True:
                    action = _ACTION_OAUTH
                elif status in ("failed", "disabled"):
                    action = _ACTION_INIT_FAILURE
                elif status == "connected":
                    action = _ACTION_INITIALIZED
                else:
                    # A reconnect is pending, not the old successful connection.
                    for bucket in (self._ready, self._failed, self._awaiting_auth):
                        if name in bucket:
                            bucket.remove(name)
                            changed = True
                    changed = self._failures.pop(name, None) is not None or changed
                    continue
                error = sanitize_sink_text(str(server.get("errorMessage") or ""), _ERROR_CAP)
                changed = self._record(action, name, error) or changed
            return changed
        action = classify_notification(msg)
        if action not in REPORT_ACTIONS:
            return False
        name = server_name_of(msg)
        if not name:
            # Without a name the frame cannot be correlated with a server, and a
            # nameless row would read as a real server that failed.
            return False
        error = ""
        if action == _ACTION_INIT_FAILURE:
            params = msg.params if isinstance(msg.params, dict) else {}
            error = sanitize_sink_text(str(params.get("error") or ""), _ERROR_CAP)
        return self._record(action, name, error)

    def record_event(
        self,
        kind: str,
        server_name: str,
        error: str = "",
        *,
        fanout_no_owner: bool = False,
    ) -> bool:
        """Fold in a registration signal that arrived as an ``AcpEvent``.

        The init drain consumes the raw frames, so the ones that come later —
        a server finishing init after its OAuth callback, or failing mid-session
        — only ever reach the dashboard as events. Both transports emit the same
        events, so recording them here keeps the live path single even though
        init capture has to sit in each transport's own drain.

        ``fanout_no_owner`` carries the same provenance ``record_frame`` reads off
        the frame: set it from ``AcpEvent.runtime_global``.
        """
        action = _EVENT_ACTIONS.get(kind)
        if action is None:
            return False
        if fanout_no_owner:
            # Same refusal as ``record_frame``, for the same reason: an MCP
            # notification carries no sessionId, so on a shared runtime it is
            # fanned out to every co-tenant. Recording it would credit this
            # session with a server it may not have. Kept here rather than at the
            # call site so both ownership rules live together — the event path
            # missing the rule the frame path had is exactly the bug this closes.
            return False
        name = sanitize_sink_text(str(server_name or ""), _NAME_CAP)
        if not name:
            return False
        return self._record(action, name, sanitize_sink_text(str(error or ""), _ERROR_CAP))

    def _record(self, action: str, name: str, error: str = "") -> bool:
        """Move ``name`` into the bucket ``action`` implies, evicting the others."""
        target = {
            _ACTION_INITIALIZED: self._ready,
            _ACTION_INIT_FAILURE: self._failed,
            _ACTION_OAUTH: self._awaiting_auth,
        }[action]
        before = (tuple(self._ready), tuple(self._failed), tuple(self._awaiting_auth))
        prior_error = self._failures.get(name, "")
        # Whether ``name`` is already tracked decides what a full target bucket
        # means. A NEW name past the cap is dropped whole — the cap exists to
        # bound spam, and letting an unbounded stream of fresh names displace
        # tracked servers would defeat it. But a TRACKED server transitioning
        # into a full bucket must still move: removing it from its old bucket
        # and then refusing it at the full one would erase a server the report
        # was already describing (the worst direction — a real server silently
        # vanishing). For that case the oldest entry in the target is evicted
        # instead; the evicted server degrades to "no report", which is an
        # absent claim rather than the wrong one.
        tracked = any(name in bucket for bucket in (self._ready, self._failed, self._awaiting_auth))
        if not tracked and name not in target and len(target) >= _BUCKET_CAP:
            return False
        for bucket in (self._ready, self._failed, self._awaiting_auth):
            if bucket is not target and name in bucket:
                bucket.remove(name)
        if name not in target:
            if len(target) >= _BUCKET_CAP:
                evicted = target.pop(0)
                self._failures.pop(evicted, None)
            target.append(name)
        if action == _ACTION_INIT_FAILURE:
            # A name that reaches this point always landed in the bucket: the
            # over-cap drop for new names happens at the early return above, and
            # a tracked transition evicts to make room. So the reason dict stays
            # bounded by the buckets it describes.
            #
            # An empty error CLEARS the stored reason rather than leaving the
            # previous one standing. A retry that fails again without saying why
            # is still the current state of that server, so keeping the earlier
            # text would present a reason this failure never gave — the same
            # stale-evidence defect, one layer in, that this view exists to
            # remove.
            if name in target:
                if error:
                    self._failures[name] = error
                else:
                    self._failures.pop(name, None)
        else:
            # A server that has since initialized (or gone back to asking for
            # authorization) must not keep showing the stale reason it failed
            # with, which is exactly the misleading evidence this view removes.
            self._failures.pop(name, None)
        after = (tuple(self._ready), tuple(self._failed), tuple(self._awaiting_auth))
        return after != before or self._failures.get(name, "") != prior_error

    @property
    def empty(self) -> bool:
        """True when nothing has been recorded and no roster was sent."""
        return not (
            self.configured
            or self.unresolved_refs
            or self._ready
            or self._failed
            or self._awaiting_auth
        )

    def payload(self) -> dict[str, Any] | None:
        """The serialized report, or ``None`` when no session has begun.

        Three states, and collapsing any two of them is how the false all-clear
        gets back in:

        * No session yet → ``None``. Absence of knowledge; the consumer keeps
          showing configuration.
        * A session began and nothing has been reported → a dict of EMPTY
          buckets. This is the honest "no report from this session yet", and it
          is NOT the same as the first case: on the kiro-cli path the wire roster
          is empty by design (servers come via ``--agent``), so a session with no
          frames yet would otherwise be indistinguishable from no session at all
          and fall back to host-configured green dots.
        * Something was reported → populated buckets.
        """
        if self.empty and not self._started:
            return None
        return {
            "configured": list(self.configured),
            "unresolved_refs": list(self.unresolved_refs),
            "ready": list(self._ready),
            "failed": list(self._failed),
            "awaiting_auth": list(self._awaiting_auth),
            "failures": dict(self._failures),
        }
