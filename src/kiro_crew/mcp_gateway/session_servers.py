"""Session-scoped MCP server injection for the shared gateway.

kiro-cli's ACP ``session/new`` accepts an ``mcpServers`` array, and a
session-injected server takes precedence over the same-named entry in the
resolved agent spec: the spec's own copy is never launched. That makes pooling
a protocol-level operation. The broker stubs replace an agent's poolable
servers for the lifetime of one session, and nothing is written to the user's
project, to their ``~/.kiro/agents/``, or through a bind mount — so pooling
works with ``agent.sandbox`` set to ``off`` (the default) and on macOS and
Windows, neither of which can bind-mount.

Only stub entries are injected. A non-poolable server is left entirely to the
agent spec, so its ``env`` — which routinely holds tokens and API keys — never
leaves the file it was declared in. Stub entries carry ``env: {}`` by
construction (``rewriter._build_stub_entry``): the pooled backend is spawned by
gatewayd, not by kiro-cli, so no credential is transmitted here either. The one
value a stub entry's ``env`` does carry is this session's stub token
(:func:`attach_stub_session_token`), which names the SESSION the entry was
injected for and is what stops a subagent sharing its parent's process from
inheriting the parent's identity.

Project scope: the overlay is keyed by agent NAME, and kiro-cli resolves
``--agent`` against ``<project>/.kiro/agents/`` as well as the user-level
directory. A session running a PROJECT agent must therefore not be handed the
user-level overlay's stubs for that name. They were rewritten from a different
file, so the session would reach servers the project never declared -- and,
worse, a same-named server would point at the user-level command while the
project's own declaration of it stayed unlaunched, because a stub outranks the
spec entry it shadows. Such a session resolves its servers from the project spec
instead, which leaves them unpooled: that is this module's standing direction for
a stub it cannot vouch for (``_load_overlay_for_agent`` fail-softs to ``None``,
``_acp_server_entry`` returns ``None`` and leaves the spec's own server in
place). Brokering them instead is not reachable from here: ``gatewayd`` resolves
a backend command from ``KIROCREW_MCP_TARGET_<SERVER>`` in its OWN process env,
written at daemon launch from the rewriter's ``target_env``, and a stub carries
its target only in its argv and its fallback log -- so an overlay written for a
project agent after launch has no target the daemon can resolve.

Precedence caveat: same-name override is verified against the shipped binary
(``test_mcp_gateway_session_inject.py`` pins it, including a live check when
kiro-cli is on PATH) but is NOT documented by kiro-cli. The documented
hierarchy covers only the three *file* tiers (agent config > workspace
``mcp.json`` > global ``mcp.json``). If a future release made injection purely
additive, an agent's own copy would launch alongside the stub, which is worse
than not pooling — hence the pinning test.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.mcp_gateway.hashing import STUB_FLAGS_FLAG, encode_target_args, expand_stub_flags
from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER, _WRAPPER_MARKER_LEGACY

logger = logging.getLogger(__name__)


def attach_stub_session_token(entries: list[dict[str, Any]], token: str) -> list[dict[str, Any]]:
    """Return *entries* with *token* added to each element's ACP ``env`` array.

    Applied to the stub entries of ONE ACP session, after
    :func:`pooled_session_servers` has shaped them. Kept separate from that
    function rather than folded into it because the token is per SESSION while
    the overlay lookup is per agent — and because the shaping call is
    monkeypatched by callers that know nothing about tokens.

    An empty *token* returns the input unchanged, so a caller that cannot mint
    one (or a build with the gateway off, where ``entries`` is empty anyway)
    stays byte-identical to the pre-token wire shape. Copies each element: the
    caller's list may be a cached array shared with another session.
    """
    if not token:
        return entries
    out: list[dict[str, Any]] = []
    for entry in entries:
        shaped = dict(entry)
        raw_env = shaped.get("env")
        env = [e for e in raw_env if isinstance(e, dict)] if isinstance(raw_env, list) else []
        env = [e for e in env if e.get("name") != STUB_SESSION_TOKEN_ENV]
        env.append({"name": STUB_SESSION_TOKEN_ENV, "value": token})
        shaped["env"] = env
        out.append(shaped)
    return out


# Keys that are positional in the ACP element shape (``name``) or that we
# always re-derive (``env``), so they must not be copied verbatim.
_ACP_RESERVED = frozenset({"name", "env", _WRAPPER_MARKER, _WRAPPER_MARKER_LEGACY})


def _acp_env(raw: Any) -> list[dict[str, str]]:
    """Convert a kiro-agent-JSON ``env`` mapping to ACP's array-of-pairs form.

    Stub entries always carry an empty mapping, so this normally returns ``[]``.
    It is still a faithful conversion rather than a hardcoded empty list so a
    future caller that injects a non-stub entry cannot silently drop its env.
    """
    if not isinstance(raw, dict):
        return []
    return [{"name": str(k), "value": str(v)} for k, v in raw.items()]


def _acp_server_entry(
    name: str, entry: dict[str, Any], channel_id: str | None = None
) -> dict[str, Any] | None:
    """Shape one rewritten ``mcpServers`` entry into an ACP array element.

    Operator-set passthrough keys (``timeout``, ``type``, ``disabledTools``,
    ``autoApprove``, vendor keys) are preserved: kiro-cli tolerates them on the
    session-injected element, and dropping ``autoApprove`` in particular would
    re-prompt for tools the agent spec had already auto-approved.

    ``channel_id`` is APPENDED as an encoded ``--channel-id <value>`` pair
    rather than prepended: the overlay entry runs the interpreter, so ``args``
    opens with ``-m kiro_crew.mcp_gateway.stub`` and anything inserted ahead of
    that would be eaten by the interpreter instead of the stub. argparse does
    not care about order. It rides its own ``--stub-flags-b64`` envelope for
    the same reason the rewriter's flags do: a channel identifier is external
    text, and a plain token crossing cmd.exe has its ``%NAME%`` spans expanded. The channel value is known here, at the one place that runs
    per session, so the stub does not need to recover it by walking its
    ancestors' ``/proc/<pid>/environ`` from a bash launcher.
    """
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        # A stub without a command cannot be launched; injecting it would
        # shadow the agent's working entry with a broken one. Skip instead,
        # leaving the spec's own server in place.
        return None
    args = [
        a if isinstance(a, str) else json.dumps(a, sort_keys=True, default=str)
        for a in (entry.get("args") or [])
    ]
    try:
        flags = expand_stub_flags(args)
    except ValueError:
        # A stub whose envelope cannot be read cannot be launched against the
        # metadata the rewriter hashed; injecting it would shadow the agent's
        # working entry with one that dies at parse time. Skip it, like the
        # command-less case above, so one unreadable overlay entry degrades
        # this server to unpooled operation instead of failing the session.
        logger.warning("mcp-gateway: skipping stub %r with an unreadable flag envelope", name)
        return None
    if channel_id and "--channel-id" not in flags:
        args.append(f"{STUB_FLAGS_FLAG}={encode_target_args(['--channel-id', channel_id])}")
    shaped: dict[str, Any] = {
        k: v for k, v in entry.items() if k not in _ACP_RESERVED and k != "command"
    }
    shaped.update(
        {
            "name": name,
            "command": command,
            "args": args,
            "env": _acp_env(entry.get("env")),
        }
    )
    return shaped


def _project_spec_for_agent(
    agent: str,
    work_dir: str | Path | None,
    markdown_specs: bool = False,
    dispatchable_only: bool = True,
) -> Path | None:
    """The project checkout's own spec for *agent*, or ``None``.

    A thin adapter over :func:`kiro_crew.agent._project_shadow_of`, which is this
    repository's named answer to "does the checkout declare this agent" and already
    carries the rule: ``<work_dir>/.kiro/agents/`` is the only project location the
    backends resolve ``--agent`` against, the declared ``name`` beats the filename,
    and an unreadable checkout answers "no shadow" rather than raising. Calling it
    rather than restating its body is what keeps this module's answer and
    ``acp/session_mcp.py``'s spec resolution from drifting into two different
    verdicts about which file a session is running.

    The import is deferred because ``agent`` reaches ``config.loader``, which imports
    this package's path helpers at its own module top level; the same reason
    ``rewriter`` defers its import of ``agent_discovery``.

    Only a project spec in a form the host DISPATCHES counts as a shadow.
    ``_project_shadow_of`` goes through ``agent_discovery.project_agent_files``,
    which scans ``*.json`` and ``*.md`` alike because whether a checkout's spec
    may be projected is a governance question for its consumers rather than a
    question of form. This caller is narrower: it is deciding whether the agent
    the session RUNS came from the checkout, and a form the host cannot activate
    did not. kiro-cli discovers ``*.json`` in a checkout, so a project ``foo.md``
    with no JSON twin must not suppress a user-level ``foo.json``'s stubs -- that
    would leave the servers kiro-cli does activate running outside the broker,
    with no pool, no caller-identity attribution and no governance. Answering "no
    shadow" is the safe direction: the overlay stays in effect and the session's
    servers stay brokered.

    The rule is a per-host opt-in rather than unconditional because the hosts
    disagree, and both answers are correct for the host holding them. A MIRRORED
    host's array is composed by Crew from a spec ``acp.session_mcp`` resolves
    through the same scanner, so it honours a project ``foo.md`` and that file IS
    the agent running: its shadow must suppress the user-level overlay, or the
    stub's command and credentials mount under the checkout's agent. kiro-cli
    resolves ``--agent`` from the checkout itself and discovers ``*.json`` only,
    so for it the markdown form is not dispatchable and suppressing on it would
    un-broker servers the session really has.
    ``agent_sdk.backends.overlay_project_scope`` is the one place that answer is
    computed, from the same ``has_mirror`` read both call paths already use to
    decide whether a projection happens at all.
    """
    if not agent or not work_dir:
        return None
    # Deferred, but NOT inside the fail-soft ``try``: the swallow below is for a
    # checkout that cannot be scanned, and an import that does not resolve is a
    # packaging fault. Conflating them turns a wrong module path into "no shadow"
    # -- the overlay silently back in effect for every project agent, which is the
    # defect this function exists to prevent, reported as success.
    from kiro_crew.agent import _project_shadow_of

    try:
        # The form set goes INTO the scan rather than filtering its result. The
        # scan sorts by stem and returns the first declared-name match, so a
        # differing-stem pair (``a.md`` and ``z.json`` both declaring ``foo``)
        # hands back the markdown file; rejecting it here would answer "no shadow"
        # with a dispatchable ``z.json`` sitting unexamined, and the user-level
        # stub's command and credentials would mount under the checkout's agent.
        spec = _project_shadow_of(
            agent,
            work_dir,
            markdown_specs=markdown_specs,
            dispatchable_only=dispatchable_only,
        )
        if spec is None:
            logger.debug(
                "MCP-gateway: %s declares no %s spec for %r that this session's agent "
                "resolution honours, so the user-level overlay stays in effect",
                work_dir,
                "json-or-markdown" if markdown_specs else "json",
                agent,
            )
            return None
        return spec
    except Exception:
        logger.debug(
            "MCP-gateway: could not resolve a project shadow for %r under %s; the "
            "user-level overlay stays in effect",
            agent,
            work_dir,
            exc_info=True,
        )
        return None


def _load_overlay_for_agent(
    overlay_dir: Path,
    agent: str,
    work_dir: str | Path | None = None,
    markdown_specs: bool = False,
    dispatchable_only: bool = True,
) -> dict[str, Any] | None:
    """Locate the rewritten overlay spec for *agent*, or ``None``.

    Package-installed agents are written to the overlay directory under a
    package-qualified filename (e.g. ``Pkg-gpu-dev.json``) while the session
    requests them by bare name (``gpu-dev``). A filename-only lookup therefore
    silently misses and disables pooling for every packaged agent. Match the
    bare filename first (fast path for unprefixed agents), then fall back to a
    filename-qualified overlay (``*<agent>.json``) whose parsed ``name`` equals
    *agent*.

    ``work_dir`` is the session's project checkout, and a spec for *agent* there
    means this lookup has no answer: the overlay directory holds user-level
    agents only, so every candidate under that name was rewritten from a file
    this session is not running (see the module docstring for what the session
    would otherwise reach). The guard sits HERE rather than in the two public
    entry points so the set of stubs injected and the set of spec entries
    withheld for them are decided once and cannot disagree. ``None`` -- the
    default, and every caller that has no checkout to name -- resolves by name
    alone, exactly as before.

    Fail-soft: an unreadable/malformed overlay yields ``None`` (unpooled), never
    an exception.
    """
    project = _project_spec_for_agent(agent, work_dir, markdown_specs, dispatchable_only)
    if project is not None:
        # WARNING, not debug. This is not an error path: it is the posture this
        # change deliberately ships -- the project's own servers launch, outside
        # the pool, outside caller-identity attribution and outside broker
        # governance -- and it fires for an ORDINARY configuration, including the
        # documented user-level-duplicate workaround. Logged at debug it would be
        # the same silent governance downgrade the original defect was faulted
        # for, with the operator's gateway switched on and nothing saying so.
        # Fires once per overlay lookup, so a session start that asks for both the
        # withheld and the injected set says it twice; deduplicating would need
        # per-session state here, which is worse than a repeated true statement.
        logger.warning(
            "MCP-gateway: %r is declared by %s, so the user-level overlay in %s is not "
            "consulted for it: this session's servers come from the project spec and run "
            "UNBROKERED -- no pool, no caller-identity attribution, no broker governance",
            agent,
            project,
            overlay_dir,
        )
        return None
    direct = overlay_dir / f"{agent}.json"
    try:
        return json.loads(direct.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass  # not emitted under the bare name — try a name-field match below
    except (OSError, ValueError):
        logger.warning("MCP-gateway: cannot read overlay spec %s", direct, exc_info=True)
        return None
    # Fallback: a package-installed agent's overlay keeps its package-qualified
    # source filename (e.g. ``Pkg-gpu-dev.json``) while the session requests it
    # by bare name. Restrict the scan to filenames that END with the agent name
    # so at most a handful of plausible candidates are read on the async
    # session-creation path — never the whole directory — then confirm each
    # against the authoritative ``name`` field so a coincidental filename suffix
    # can't mismatch.
    try:
        candidates = sorted(overlay_dir.glob(f"*{agent}.json"))
    except (OSError, ValueError):
        # ValueError: an agent name carrying glob metacharacters (e.g. ``*`` ->
        # ``**.json``) is an invalid pattern; fail soft to unpooled rather than
        # aborting session creation.
        return None
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("name") == agent:
            return data
    if candidates:
        # Distinguish "packaged agent not found" from "gateway disabled" / "agent
        # declared nothing poolable" for an operator debugging a low backend count.
        logger.debug(
            "MCP-gateway: no overlay with name %r among %d filename-qualified "
            "candidate(s) in %s; session runs unpooled",
            agent,
            len(candidates),
            overlay_dir,
        )
    return None


def pooled_session_servers(
    overlay_dir: str | Path | None,
    agent: str | None,
    channel_id: str | None = None,
    *,
    work_dir: str | Path | None = None,
    markdown_specs: bool = False,
    dispatchable_only: bool = True,
) -> list[dict[str, Any]]:
    """Return ACP ``session/new`` entries for *agent*'s broker stubs.

    ``overlay_dir`` is the rewriter's output directory (usually
    ``<config_dir>/mcp-gateway/agents/``); it is ``None`` when the shared
    gateway is disabled, which is the natural off switch — this returns ``[]``
    and the session runs entirely on the agent's own servers.

    ``channel_id`` reaches the stub so it can report the channel in its caller
    identity, which ``gatewayd`` stamps onto every forwarded ``tools/call`` as
    ``_meta.kirocrew.caller``. It is deliberately NOT a pool dimension (see
    :mod:`kiro_crew.mcp_gateway.pool`), so passing it does not split backends —
    two channels reaching the same agent still share one. It is passed here
    rather than baked into the overlay because the overlay is written once at
    gateway startup and is session-agnostic, while this function runs per
    session. ``None`` simply leaves the flag off and the channel unreported.

    ``work_dir`` is this session's project checkout, and it is a SCOPE rather
    than a pool dimension: an agent the checkout declares is not the user-level
    agent of that name, so the user-level overlay holds no stubs for it and this
    returns ``[]`` (see :func:`_load_overlay_for_agent` and the module
    docstring). Keyword-only, matching ``acp.session_mcp``'s
    ``session_mcp_servers`` / ``session_mcp_deny_rules``, and each call site
    passes the same value its sibling resolver there already gets — the two must
    agree on which file the session's agent is, or the stub set and the spec it
    shadows are read from different agents.

    Fail-soft by design: any unreadable or malformed overlay yields ``[]``, so a
    bad rewrite degrades to unpooled operation rather than breaking the spawn.
    """
    if not overlay_dir or not agent:
        return []
    spec = _load_overlay_for_agent(
        Path(overlay_dir), agent, work_dir, markdown_specs, dispatchable_only
    )
    if not isinstance(spec, dict):
        return []
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    out: list[dict[str, Any]] = []
    for name, entry in sorted(servers.items()):
        if not isinstance(entry, dict) or not (
            entry.get(_WRAPPER_MARKER) or entry.get(_WRAPPER_MARKER_LEGACY)
        ):
            # Not a broker stub: leave it to the agent spec entirely.
            continue
        shaped = _acp_server_entry(str(name), entry, channel_id)
        if shaped is not None:
            out.append(shaped)
    return out


def injection_server_names(
    overlay_dir: str | Path | None,
    agent: str | None,
    *,
    work_dir: str | Path | None = None,
    markdown_specs: bool = False,
    dispatchable_only: bool = True,
) -> frozenset[str]:
    """Return the set of server names that WILL be injected for *agent*.

    Callers use this to detect an additive-injection regression: if a launched
    session reports MCP servers whose names overlap with this set, injection has
    become additive rather than overriding and every pooled server is running
    twice.

    It is also what a mirror withholds from its own projection, so it has to
    answer for the SAME agent file :func:`pooled_session_servers` injects from:
    a set naming a server that is not injected withholds the spec's only copy of
    it and the session gets nothing. ``work_dir`` is therefore not optional in
    practice for a caller that has one — both functions resolve it through the
    one guard in :func:`_load_overlay_for_agent`, so passing it to one and not
    the other is the way to make them disagree.

    This is deliberately cheap (one file read, no shaping) so it can be called
    as a post-launch health check without adding latency to the session path.
    """
    if not overlay_dir or not agent:
        return frozenset()
    spec = _load_overlay_for_agent(
        Path(overlay_dir), agent, work_dir, markdown_specs, dispatchable_only
    )
    if not isinstance(spec, dict):
        return frozenset()
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return frozenset()
    return frozenset(
        name
        for name, entry in servers.items()
        if isinstance(entry, dict)
        and (entry.get(_WRAPPER_MARKER) or entry.get(_WRAPPER_MARKER_LEGACY))
    )
