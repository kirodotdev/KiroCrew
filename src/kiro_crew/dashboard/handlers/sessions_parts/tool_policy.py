"""Which managed tools an agent may not reach: its spec's ``managedToolPolicy``.

``GET /api/session-tool-policy``, which managed MCP servers call to filter their tool lists
per agent, and the memoised off-loop read behind it: the spec resolved by its declared name,
an unreadable or ambiguous spec refused rather than answered as "no policy", and the include
list a policy derives from the spec's tool refs. New managed-tool policy rules go here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import hooks
from kiro_crew.agent_discovery import (
    AgentsDirMemo,
    AmbiguousAgentSpecError,
    SensitiveAgentSpecPathError,
    _SpecReadRefused,
    plain_markdown_document,
    read_agent_spec_strict,
    spec_by_declared_name,
)
from kiro_crew.agent_sdk.mcp_refs import per_tool_grants
from kiro_crew.agent_spec_format import (
    agent_spec_candidates,
    is_markdown_spec,
    iter_agent_spec_files,
)
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.dashboard.handlers._shared import guard_owner_surface_routes, internal_memory_scope
from kiro_crew.dashboard.handlers.sessions_parts.app_scope import _sel
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.platform import redact_log_via_context

# The family logs under the facade's name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.handlers.sessions")


class ManagedToolPolicyUnreadable(Exception):
    """An agent's spec exists but its ``managedToolPolicy`` cannot be read.

    Distinct from "this agent has no policy": an operator may have written an
    exclusion that is unreadable from here, so the two must not share one
    answer on the wire. A caller that cannot tell them apart has to treat an
    unreadable deny as no deny, which silently widens it.
    """


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
ManagedToolPolicyUnreadable.__module__ = "kiro_crew.dashboard.handlers.sessions"


def _spec_failure_kind(path: Path, exc: BaseException) -> str:
    """WHY *path* failed the strict read, in plain words and with NO path in it.

    Every answer is a PREDICATE phrase -- it reads after "``<file>`` is" and
    after "could not be read (" alike -- because two templates splice it in.

    The reason this feeds crosses the wire to the MCP client and lands in the
    model-visible refusal text, so it names the failure class rather than
    quoting ``str(exc)``: the strict reader's own messages carry the full
    path (``f"{path}: {exc}"``, the AppleDouble and size-cap arms) and an
    ``OSError`` carries ``filename``. The classes are the ones
    :func:`kiro_crew.agent_discovery.read_agent_spec_strict` documents; an
    unfamiliar one still gets a usable name from its class.
    """
    if path.name.startswith("._"):
        return "an AppleDouble sidecar, not a spec"
    if isinstance(exc, json.JSONDecodeError):
        return "not valid JSON"
    if isinstance(exc, UnicodeDecodeError):
        return "not UTF-8 text"
    if isinstance(exc, SensitiveAgentSpecPathError):
        return "a path the spec reader refuses"
    if isinstance(exc, OSError) and isinstance(exc.__cause__, _SpecReadRefused):
        # The strict reader maps the pinned open's refusal to a generic
        # ``EACCES``; its ``strerror`` is the reader's own placeholder, which
        # would only restate "could not be read". The cause says why.
        return (
            "not a plain readable file: a link at its name, a hardlinked or "
            "non-regular inode, or a target the spec reader fences"
        )
    if isinstance(exc, OSError):
        # ``strerror`` is the C library's text ("Permission denied"); the path
        # lives in ``filename`` and is deliberately left out.
        return f"unreadable ({exc.strerror or exc.__class__.__name__})"
    if isinstance(exc.__cause__, hooks.FileTooLargeError):
        return "larger than the spec size cap"
    if is_markdown_spec(path):
        # No causal clause: the parser refuses a closed fence too (frontmatter
        # that is a list, nested too deeply, a bare ``on:`` key YAML reads as a
        # bool), so naming one cause would send the operator to check a fence
        # that is closed. The strict reader's own messages for this family are
        # path-free, but not every ValueError reaching here is, so the class is
        # named rather than the text quoted.
        return "markdown frontmatter the spec parser refuses"
    return f"not a spec ({exc.__class__.__name__})"


def _ambiguous_spec_reason(agent_name: str, exc: AmbiguousAgentSpecError) -> str:
    """The wire ``reason`` for two differing specs declaring *agent_name*: names,
    not paths.

    The exception's own message quotes each file's full path for the terminal
    it was written for; this text reaches the MCP client's model-visible
    refusal, so it carries the same files by name only (``repr``'d, as
    untrusted input from a user-writable directory) and the remedy. Falls back
    to the message when the raiser supplied no paths -- the one other raiser,
    ``agent.agent_spec_path``, is never reached from here.
    """
    if not exc.paths:
        return str(exc)
    names = ", ".join(repr(path.name) for path in exc.paths)
    return (
        f"{len(exc.paths)} specs in the agents directory declare the name {agent_name!r} "
        f"with differing content: {names}. Which one is live is undefined, so the policy "
        f"for {agent_name!r} is unknown. Remove or rename one of them in the agents "
        f"directory (~/.kiro/agents unless relocated); no restart needed."
    )


def _unreadable_spec_remedy(path: Path) -> str:
    """The one sentence an operator can act on, appended to every refusal.

    The filename is ``repr``'d: it is untrusted input from a user-writable,
    tool-shared directory and this text reaches a terminal and the model. The
    directory is named by role, not by path -- the reason is client-visible.
    """
    return (
        f"Move or fix {path.name!r} in the agents directory (~/.kiro/agents unless "
        f"relocated); no restart needed."
    )


# ``(path digest, mtime_ns)`` pairs already warned about. The client re-asks
# for its policy on every ``tools/call`` and a refusal is never memoized, so
# without this the gateway log would carry one WARNING per refused tool call
# for as long as the file stays broken. A fix or a re-break changes the mtime
# and is logged again. Bounded in BOTH dimensions: the entry count is capped
# (clearing costs one repeated line, nothing else), and each entry retains a
# fixed-size SHA-256 digest of the path rather than the path itself, so the
# cap bounds the bytes held and not only the number of items -- the path is
# needed once, for the log line, and never read back out of here.
_UNREADABLE_SPEC_WARNED: set[tuple[bytes, int]] = set()
_UNREADABLE_SPEC_WARNED_MAX = 1024


def _warn_unreadable_spec_once(path: Path, kind: str) -> None:
    """Name *path* in the gateway log, once per on-disk revision of it.

    The FULL path goes here, ``%r``'d: the gateway log is local to the
    operator, and it is the one place the reason on the wire (name only) can be
    joined back to a location. The SEL row the caller writes per request
    carries the wire reason, so the audit trail is complete without this line;
    this is for the operator tailing the log.
    """
    try:
        revision = path.stat().st_mtime_ns
    except OSError:
        revision = -1
    key = (hashlib.sha256(str(path).encode("utf-8", "surrogateescape")).digest(), revision)
    if key in _UNREADABLE_SPEC_WARNED:
        return
    if len(_UNREADABLE_SPEC_WARNED) >= _UNREADABLE_SPEC_WARNED_MAX:
        _UNREADABLE_SPEC_WARNED.clear()
    _UNREADABLE_SPEC_WARNED.add(key)
    # The filename is untrusted input from a user-writable directory and this
    # line persists in gateway.log: the same log-egress redaction every other
    # operational line carrying foreign text applies (``redact_log_via_context``,
    # the non-raising spelling for a log site), so a credential-shaped name is
    # scrubbed before it is written. ``%r`` still escapes control bytes.
    logger.warning(
        "agent spec %r could not be read (%s); every session whose agent has no "
        "spec of its own is refused its managed tools until it is moved or fixed",
        redact_log_via_context(str(path)),
        kind,
    )


def _refuse_if_any_spec_is_unreadable(agents_dir: Path, agent_name: str) -> None:
    """Raise when a spec in *agents_dir* cannot be read, so "no match" is honest.

    :func:`spec_by_declared_name` resolves an agent by parsing every spec and
    comparing its declared ``name``. Its reader folds each refusal into "not a
    usable spec", which a scan for a name cannot tell apart from "a spec for
    someone else" -- so an unparseable file leaves the scan reporting no match
    when the policy it was looking for may be inside that very file.

    A file's declared name cannot be recovered without reading it, and its
    filename does not have to carry it, so there is no sound way to narrow this
    to "specs that could be *agent_name*". The honest answer while any spec is
    unreadable is that this agent's policy is unknown. Fixing or removing the
    file clears it, and the refusal is audited by the caller.

    One exception, taken only after the strict read already refused: a markdown
    file with no opening frontmatter fence is not a spec at all (see
    :func:`kiro_crew.agent_discovery.plain_markdown_document`), so it is
    skipped rather than allowed to deny every agent that has no spec of its
    own. A FENCED document that fails
    to parse still raises: its declared name is unrecoverable, so the policy
    stays unknown.

    Uses :func:`read_agent_spec_strict`, the reader that keeps the failure class,
    for exactly the reason its docstring gives: this caller needs to know WHY --
    and the refusal says WHICH: the message names the file (name only, the
    directory is the caller's) and the failure kind in words, then what to do.
    The verdict is unchanged by that; only its text is. Without the name, the
    operator told to "fix or remove the unreadable spec" had to validate every
    file in the directory by hand to find it.
    """
    for path in iter_agent_spec_files(agents_dir):
        try:
            read_agent_spec_strict(path, operation="session_tool_policy", source="dashboard")
        except (OSError, ValueError) as exc:
            if is_markdown_spec(path) and plain_markdown_document(path):
                # Not a spec (no opening fence): it cannot declare a policy,
                # so it must not turn into a denial of every other agent.
                continue
            kind = _spec_failure_kind(path, exc)
            _warn_unreadable_spec_once(path, kind)
            raise ManagedToolPolicyUnreadable(
                f"agent spec {path.name!r} in the agents directory could not be read "
                f"({kind}), so the policy for {agent_name!r} is unknown: it may be "
                f"the file that declares it. {_unreadable_spec_remedy(path)}"
            ) from exc


# Resolved policies keyed by agents directory and agent name, each pinned to
# the stat-only revision of the directory they were read under. The read below
# parses EVERY spec in the directory to find one declared name, and refuses
# only after a second strict pass over all of them; a managed MCP server asks
# for its policy on ordinary request traffic, so with a couple of thousand
# installed specs each request costs the worker thread most of a second of
# GIL-holding path validation and JSON parsing. An unchanged directory answers
# from here for the price of one ``scandir``. Its own instance, not shared with
# the KAS projection's: the two reads carry different SEL ``operation`` labels.
_TOOL_POLICY_MEMO: AgentsDirMemo[dict[str, Any] | None] = AgentsDirMemo()

#: The spec key that opts an agent into reading its ``@<server>/<tool>`` refs as
#: an include list, inside ``managedToolPolicy``. Absent or ``false`` keeps the
#: answer the policy alone gives.
HONOUR_TOOL_REFS_KEY = "honourToolRefs"

#: The answer key carrying the per-server include lists. Computed here from the
#: spec's ``tools`` and never copied from the spec itself.
POLICY_INCLUDE_KEY = "include"

#: The managed server an opted-in spec's per-tool refs narrow.
INCLUDE_LIST_SERVER = "kirocrew-core"

#: The spec key, inside ``managedToolPolicy``, naming managed servers the agent is
#: denied WHOLE. Each one is answered as an empty ``include`` list, so that server
#: lists nothing and refuses every call. The app framework writes it for a managed
#: server whose tool names it could not read (``apps.permissions``), where a
#: name-based ``exclude`` cannot cover the server.
EXCLUDE_SERVERS_KEY = "excludeServers"


def _policy_answer(
    policy: dict[str, Any], config: dict[str, Any], agent_name: str, spec_path: Path | None
) -> dict[str, Any]:
    """The policy body the endpoint answers, given the spec's own ``managedToolPolicy``.

    For a spec without ``honourToolRefs: true`` or ``excludeServers`` that is the
    policy as written, less any ``include`` key: an include list is derived here,
    and an ``include`` written by hand is not a field the managed servers read
    from a spec.

    With the opt-in set and the spec granting :data:`INCLUDE_LIST_SERVER` ONLY
    through ``@<server>/<tool>`` refs, its set of tool names goes under
    ``include``; the server lists those and refuses any other call.
    A whole-server grant -- ``*``, a bare ``@<server>``, a glob tool part --
    yields no entry, so the server answers exactly as it does without the opt-in.

    Every server named in :data:`EXCLUDE_SERVERS_KEY` is answered as an EMPTY
    ``include`` entry, overriding any per-tool entry for it: that server lists
    nothing and refuses every call. The key itself is not answered.

    Raises :class:`ManagedToolPolicyUnreadable` for an opt-in that is present
    but not a bool, or an ``excludeServers`` that is present but not a list of
    server names: the operator wrote something there and whether it narrows
    the agent is unknown.
    """
    answer = {k: v for k, v in policy.items() if k not in (POLICY_INCLUDE_KEY, EXCLUDE_SERVERS_KEY)}
    opt_in = policy.get(HONOUR_TOOL_REFS_KEY, False)
    withheld = policy.get(EXCLUDE_SERVERS_KEY, [])
    for key, bad in (
        (HONOUR_TOOL_REFS_KEY, not isinstance(opt_in, bool)),
        (
            EXCLUDE_SERVERS_KEY,
            not isinstance(withheld, list) or not all(isinstance(s, str) and s for s in withheld),
        ),
    ):
        if not bad:
            continue
        value = policy.get(key)
        where = f"in {spec_path.name!r}" if spec_path is not None else "in its spec"
        remedy = (
            _unreadable_spec_remedy(spec_path)
            if spec_path is not None
            else "Fix the spec in the agents directory; no restart needed."
        )
        expected = "a bool" if key == HONOUR_TOOL_REFS_KEY else "a list of server names"
        raise ManagedToolPolicyUnreadable(
            f"managedToolPolicy.{key} for {agent_name!r} {where} is "
            f"{type(value).__name__}, not {expected}. {remedy}"
        )
    include: dict[str, list[str]] = {}
    if opt_in:
        granted = per_tool_grants(config.get("tools"), INCLUDE_LIST_SERVER)
        if granted is not None:
            include[INCLUDE_LIST_SERVER] = sorted(granted)
    for server in withheld:
        include[server] = []
    if include:
        answer[POLICY_INCLUDE_KEY] = include
    return answer


def _read_managed_tool_policy_sync(agents_dir: Path, agent_name: str) -> dict[str, Any] | None:
    """:func:`_read_managed_tool_policy_uncached`, answered from the memo while
    *agents_dir* is unchanged. Blocking; runs on a worker like the read it wraps.

    Only a resolved answer -- a policy, or ``None`` for an agent that has none
    -- is memoized. A refusal (:class:`ManagedToolPolicyUnreadable`,
    :class:`~kiro_crew.agent_discovery.AmbiguousAgentSpecError`) propagates out
    of :class:`~kiro_crew.agent_discovery.AgentsDirMemo` unstored and so is
    re-derived on every call: it names an operator error the caller audits per
    request, and the state it reports is the one a fix to the directory clears.
    The revision and store rules are the memo's; see its docstring.
    """
    return _TOOL_POLICY_MEMO.get(
        agents_dir, agent_name, lambda: _read_managed_tool_policy_uncached(agents_dir, agent_name)
    )


def _read_managed_tool_policy_uncached(agents_dir: Path, agent_name: str) -> dict[str, Any] | None:
    """Read one agent's ``managedToolPolicy`` from disk. Blocking.

    Split out so the whole filesystem transaction -- the existence probe, the
    read and the JSON parse -- crosses to a worker as ONE unit. Offloading only
    the read would leave the ``stat`` and the parse on the gateway's single event
    loop, which is the same defect in a smaller form.

    The spec is resolved by its declared ``name`` first, through
    :func:`kiro_crew.agent_discovery.spec_by_declared_name`, and
    ``<agent_name>.json`` is read only when no spec declares the name -- the
    same order the KAS projection uses to start the session, so the policy
    handed to a session's MCP servers is the policy of the spec that session
    runs. A package-installed agent is namespaced on disk as
    ``<package>-<name>.json`` and dispatchable under its bare name, so a
    filename-only read here would hand exactly that agent an empty policy;
    and a misnamed ``<agent_name>.json`` declaring some other agent must not
    hand this one that agent's policy. The scan reads specs it did not name in
    a user-writable directory, so it goes through the hardened reader,
    labelled ``session_tool_policy`` / ``dashboard``.

    ``None`` means "this agent has no policy to report" -- no spec file, a spec
    that declares none, or a fence-less ``<agent_name>.md`` in the filename slot,
    which is a prose document and not a spec
    (:func:`kiro_crew.agent_discovery.plain_markdown_document`).
    The caller answers ``{}`` for it, without a SEL ``ok`` record when nothing
    was parsed.

    Raises :class:`ManagedToolPolicyUnreadable` when a spec EXISTS but its
    policy cannot be determined (unparseable, valid JSON that is not an object,
    or a ``managedToolPolicy`` of the wrong shape). That is not "no policy": the
    operator may have written an exclusion this cannot see, so the caller must
    not answer it with the same empty body a policy-free agent gets.

    Propagates :class:`kiro_crew.agent_discovery.AmbiguousAgentSpecError` when
    two specs declare *agent_name* with differing content: that is not "no
    policy" either, and the caller records it as a denial rather than answering
    it silently. Two IDENTICAL specs declaring the name are one policy, and the
    scan answers it (see ``spec_by_declared_name``): a package installer that
    flattens dependencies writes such twins, and whichever of them the session
    runs, its policy is this one.
    """
    # The file the policy was read from, when it was a direct-filename read.
    # The declared-name scan returns a parse and not a path (see
    # ``spec_by_declared_name``: a path to reopen would put a second read
    # outside the guards), so a shape refusal on ITS result names the agent
    # only -- which identifies the spec, since exactly one declares that name.
    spec_path: Path | None = None
    try:
        config: Any = spec_by_declared_name(
            agents_dir, agent_name, operation="session_tool_policy", source="dashboard"
        )
        if config is None:
            # ``<name>.json`` then ``<name>.md``: beside a twin the JSON wins,
            # the order every direct-filename fallback uses (see
            # ``kas_agents.load_agent_spec``).
            present = [p for p in agent_spec_candidates(agents_dir, agent_name) if p.is_file()]
            if not present:
                # No spec matched by declared name and none by filename. That is
                # "no policy" only if every spec in the directory was READABLE:
                # the scan above resolves a name by parsing each file, and its
                # reader folds a refusal into "no match", so an unparseable spec
                # is indistinguishable from one declaring a different name. A
                # package-installed agent is namespaced on disk
                # (``<package>-<name>.json``) and has no bare filename
                # candidate, so the scan is the ONLY thing that could have found
                # its policy -- and a file this cannot read may be exactly it.
                _refuse_if_any_spec_is_unreadable(agents_dir, agent_name)
                return None
            spec_path = present[0]
            # The hardened reader: the agents directory is user-writable, so
            # a symlink here is not followed to a sensitive target.
            try:
                config = read_agent_spec_strict(
                    spec_path, operation="session_tool_policy", source="dashboard"
                )
            except (OSError, ValueError):
                if not (is_markdown_spec(spec_path) and plain_markdown_document(spec_path)):
                    raise
                # ``<agent_name>.md`` with no opening fence and no JSON twin is
                # not this agent's spec: it is a prose document sharing the
                # name (see ``agent_discovery.plain_markdown_document``), so it
                # cannot hold a policy any more than a stray ``README.md`` can.
                # The same not-a-spec rule the enumeration guard and the KAS
                # projection's direct-filename read apply -- and the same
                # disposition as no candidate at all. A fenced document
                # that fails to parse re-raised above: it announced itself as
                # a spec, so its policy stays unknown.
                _refuse_if_any_spec_is_unreadable(agents_dir, agent_name)
                return None
    except AmbiguousAgentSpecError:
        # A ``ValueError`` subclass, so it is named BEFORE the parse-failure arm
        # below or it would be swallowed as "no policy" instead of propagating.
        raise
    except (OSError, ValueError) as exc:
        # The file is there and could not be read or parsed. Whatever exclusions
        # it declares are unknown, so this is reported as unknown. The prefix is
        # the one an earlier reader of this arm matches on; the name and kind
        # follow it. ``spec_path`` is unset only when the directory WALK itself
        # raised (``spec_by_declared_name`` and ``iter_agent_spec_files`` both
        # propagate the glob's ``OSError``): there is no file to name, so that
        # arm keeps its class-name-only text.
        if spec_path is None:
            raise ManagedToolPolicyUnreadable(
                f"agent spec for {agent_name!r} could not be read: {exc.__class__.__name__}"
            ) from exc
        kind = _spec_failure_kind(spec_path, exc)
        _warn_unreadable_spec_once(spec_path, kind)
        raise ManagedToolPolicyUnreadable(
            f"agent spec for {agent_name!r} could not be read: {spec_path.name!r} is "
            f"{kind}. {_unreadable_spec_remedy(spec_path)}"
        ) from exc
    if not isinstance(config, dict):
        # Valid JSON that is not an object (a list, a scalar, null) parses
        # fine, but `.get` on it would raise. It is a malformed spec, so it
        # takes the same disposition as the unparseable case above. Only the
        # direct read lands here -- the scan matches ``dict`` specs only -- so
        # ``spec_path`` is set; the bare form is kept for the type checker.
        if spec_path is None:
            raise ManagedToolPolicyUnreadable(
                f"agent spec for {agent_name!r} is valid JSON but not an object"
            )
        raise ManagedToolPolicyUnreadable(
            f"agent spec for {agent_name!r} could not be read: {spec_path.name!r} is "
            f"valid JSON but not an object. {_unreadable_spec_remedy(spec_path)}"
        )
    policy = config.get("managedToolPolicy", {})
    if isinstance(policy, dict):
        return _policy_answer(policy, config, agent_name, spec_path)
    # A policy of the wrong shape is a policy this cannot read, not an absent
    # one: the operator wrote something here and its meaning is unknown.
    if spec_path is None:
        raise ManagedToolPolicyUnreadable(
            f"managedToolPolicy for {agent_name!r} is {type(policy).__name__}, not an "
            f"object. Fix the spec declaring name {agent_name!r} in the agents "
            f"directory (~/.kiro/agents unless relocated); no restart needed."
        )
    raise ManagedToolPolicyUnreadable(
        f"managedToolPolicy for {agent_name!r} in {spec_path.name!r} is "
        f"{type(policy).__name__}, not an object. {_unreadable_spec_remedy(spec_path)}"
    )


async def api_session_tool_policy(request: web.Request) -> web.Response:
    """GET /api/session-tool-policy — return managedToolPolicy for the
    calling session's agent.

    Used by managed MCP servers (kirocrew-core, kirocrew-cron) to filter
    their tool lists per-agent.  Returns {"exclude": [...]} on success,
    or 400/404 when the session cannot be identified (deny-by-default:
    callers that cannot prove identity get an error, not an empty policy).
    Authenticated via X-Internal-Secret + X-Session-Key.
    """
    _, refusal = await internal_memory_scope(
        request, "api_session_tool_policy", claimed_session=request.headers.get("X-Session-Key", "")
    )
    if refusal is not None:
        return refusal
    state: DashboardState = request.app["state"]
    session_key = request.headers.get("X-Session-Key", "").strip()
    if not session_key:
        _sel().log_api_access(
            caller="unknown",
            operation="session_tool_policy",
            outcome="denied",
            source="dashboard",
            resources="missing X-Session-Key",
        )
        return web.json_response({"error": "X-Session-Key required"}, status=400)

    # Resolve agent name from session
    agent_name = ""

    # Dashboard slot
    if session_key.startswith("dashboard:"):
        slot_key = session_key[len("dashboard:") :]
        slot = state.get_slot(slot_key)
        if slot:
            agent_name = slot.agent
    # Subagent — look up in SubagentManager
    elif session_key.startswith("subagent:"):
        if state.subagents:
            subagent_id = session_key[len("subagent:") :]
            info = state.subagents.get(subagent_id)
            if info:
                agent_name = info.agent
    # Cron — fall through to session manager lookup below
    elif session_key.startswith("cron:"):
        pass

    # Also check session manager for agent name
    if not agent_name and state.sessions:
        agent_name = state.sessions.get_agent(session_key)

    if not agent_name:
        _sel().log_api_access(
            caller=session_key,
            operation="session_tool_policy",
            outcome="denied",
            source="dashboard",
            resources="agent not resolved",
        )
        return web.json_response({"error": "agent not resolved"}, status=404)

    # Sanitize agent_name to prevent path traversal
    if "/" in agent_name or "\\" in agent_name or ".." in agent_name:
        _sel().log_api_access(
            caller=session_key,
            operation="session_tool_policy",
            outcome="denied",
            source="dashboard",
            resources=f"invalid agent_name={agent_name!r}",
        )
        return web.json_response({"error": "invalid agent name"}, status=400)

    # Read agent config from disk, OFF the event loop. A managed MCP server
    # calls this to filter its tool list, so it runs on ordinary request traffic
    # rather than at startup: the stat, the read and the parse would otherwise
    # execute on the single loop every gateway request shares.
    try:
        policy = await asyncio.to_thread(
            _read_managed_tool_policy_sync, kiro_agents_dir(), agent_name
        )
    except AmbiguousAgentSpecError as exc:
        # Two specs declare this agent's name. The policy is undefined, not
        # empty, so it is answered with a status the caller cannot mistake for
        # a policy-free agent, and recorded as a denial naming both files. The
        # SEL row keeps the exception's own message, full paths included --
        # the audit trail is local. The wire ``reason`` names the files
        # WITHOUT their directory, in the shape every other refusal here takes:
        # it reaches the MCP client's model-visible error text, and a full
        # path there discloses the account name and on-disk layout.
        _sel().log_api_access(
            caller=session_key,
            operation="session_tool_policy",
            outcome="denied",
            source="dashboard",
            resources=f"agent={agent_name}",
            error=str(exc),
        )
        return web.json_response(
            {
                "error": f"The policy for agent {agent_name!r} could not be determined.",
                "code": "policy_unreadable",
                "reason": _ambiguous_spec_reason(agent_name, exc),
            },
            status=409,
        )
    except ManagedToolPolicyUnreadable as exc:
        # The spec is present and its policy could not be determined. Answering
        # {} here would be byte-identical to an agent that legitimately has no
        # policy, and the caller would run a tool this cannot prove is allowed.
        _sel().log_api_access(
            caller=session_key,
            operation="session_tool_policy",
            outcome="denied",
            source="dashboard",
            resources=f"agent={agent_name}",
            error=str(exc),
        )
        return web.json_response(
            {
                "error": f"The policy for agent {agent_name!r} could not be determined.",
                "code": "policy_unreadable",
                "reason": str(exc),
            },
            status=409,
        )
    if policy is None:
        # This agent has no policy: no spec file, or a spec declaring none.
        # A genuinely absent policy is an empty one, so the answer is unchanged
        # -- and still not logged as a success, since nothing was parsed.
        return web.json_response({})

    _sel().log_api_access(
        caller=session_key,
        operation="session_tool_policy",
        outcome="ok",
        source="dashboard",
        resources=f"agent={agent_name}",
    )
    return web.json_response(policy)


# Every ``api_session*`` handler is an owner surface, so a private member's
# internal call is refused before it runs (audit label = handler name).
# The one named below verifies and scopes its own caller instead.
guard_owner_surface_routes(
    globals(),
    prefix="api_session",
    member_scoped=frozenset({"api_session_tool_policy"}),
)
