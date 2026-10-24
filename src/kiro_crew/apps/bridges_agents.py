"""Materializing an app's agents into the kiro-cli agents directory.

The app's MCP policy merged into each agent config, host-managed MCP references,
user edits carried across a refresh, shipped agent templates, ``_register_agents`` /
``_deregister_agents``, and the residue an uninstalled app's agents leave behind
(``purge_uninstalled_agent_residue``).

Part of the ``kiro_crew.apps.bridges`` family: callers and tests reach every name here
as ``kiro_crew.apps.bridges.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any, Container

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.apps.bridges_mcp_config import (
    _health_reconcile_guard,
    _mcp_lock,
    _read_mcp_json_unlocked,
)
from kiro_crew.apps.bridges_sources import _kiro_agents_dir, _namespace, _safe_link_name
from kiro_crew.apps.manager import app_data_dir, app_dir
from kiro_crew.apps.manifest import (
    AppManifest,
    Containment,
    contained_in_screened_root,
    screen_app_path,
)
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import (
    publish_materialized_agents,
    schedule_materialized_agents_refresh,
)
from kiro_crew.mcp_utils import without_mcp_refs
from kiro_crew.platform.governance import may_skip_gate_now, strip_ungoverned_auto_approve
from kiro_crew.sel import sel
from kiro_crew.user_json import loads_user_json

logger = logging.getLogger("kiro_crew.apps.bridges")


# ---------------------------------------------------------------------------
# Agent registration
# ---------------------------------------------------------------------------

#: Filename an app's backend writes into its data dir to declare per-agent overrides
#: the framework applies when it materializes those agents. Two kinds today: which
#: MCP servers each agent may reach (:func:`_apply_agent_mcp_policy`) and a pinned
#: system prompt (:func:`_apply_agent_prompt`). The filename keeps its original
#: MCP-era name so existing installs keep being read.
AGENT_MCP_POLICY_FILE = "agent_mcp_policy.json"


def _agent_mcp_policy(app_name: str) -> dict[str, Any]:
    """Read the app's MCP policy, or ``{}`` when it declares none.

    Lives in the app's DATA dir (user state), never in the app's code, so a
    packaged/read-only app can still have per-user policy.
    """
    path = app_dir(app_name) / "data" / AGENT_MCP_POLICY_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _global_mcp_specs() -> dict[str, Any]:
    """Server name -> full spec from the user's global ``~/.kiro/settings/mcp.json``.

    Read-only and best-effort: the file belongs to kiro-cli, and a policy merge
    that cannot read it neutralizes nothing rather than failing app enable.
    """
    try:
        raw = loads_user_json(
            (Path.home() / ".kiro" / "settings" / "mcp.json").read_text(encoding="utf-8")
        )
        specs = raw.get("mcpServers")
        return dict(specs) if isinstance(specs, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 — malformed global file is not our error
        logger.warning("MCP policy merge: cannot read global mcp.json: %s", exc)
        return {}


def _is_within(candidate: Path, root: Path) -> bool:
    """True if the already-resolved ``candidate`` is ``root`` or sits under it.

    Both arguments must already be resolved by the caller (``_apply_agent_prompt``
    resolves the prompt path so a symlink cannot point out of the app's dirs).
    """
    try:
        return candidate == root or candidate.is_relative_to(root)
    except (OSError, ValueError):
        return False


def _apply_agent_prompt(
    agent_data: dict[str, Any],
    agent_name: str,
    policy: dict[str, Any],
    app_name: str,
    app_root: Path,
) -> dict[str, Any]:
    """Pin one agent's system prompt from the app's policy file.

    WHY THIS IS RUNTIME AND NOT THE TEMPLATE: an app's agent JSON ships INSIDE the
    app, so it can only name paths that exist at packaging time. A GENERATED prompt
    cannot be expressed there — an app whose persona depends on user-chosen settings
    (a display name, a chosen appearance) has to render the prompt into its data dir
    and then tell the framework where it landed. Without a seam for that, the app's
    prompt is simply never attached and its agent runs on its one-line
    ``description`` alone, which looks like the app's own prompt being ignored.

    Policy shape (optional, alongside the MCP keys)::

        {"agents": {"<agent-name>": {"prompt": "file:///abs/path/to/prompt.md"}}}

    ABSOLUTE and EXISTING only. A prompt that silently fails to resolve is exactly
    the failure this function exists to prevent, so a relative or missing path is
    dropped with a warning rather than written through to the agent file.
    """
    per_agent = (policy.get("agents") or {}).get(agent_name) or {}
    raw = per_agent.get("prompt")
    if not isinstance(raw, str) or not raw:
        return agent_data
    path = Path(raw[len("file://") :] if raw.startswith("file://") else raw)
    if not path.is_absolute():
        logger.warning("Agent %s: prompt path is not absolute, ignoring: %s", agent_name, raw)
        return agent_data
    if not path.is_file():
        logger.warning("Agent %s: prompt file does not exist, ignoring: %s", agent_name, path)
        return agent_data

    # CONTAINMENT: the path is app-controlled (it comes out of the app's own
    # agent_mcp_policy.json), and kiro-cli reads whatever it names as the agent's
    # SYSTEM PROMPT — so an app that writes "file:///Users/me/.ssh/id_rsa" here
    # would feed a credential file straight into the model. The prompt is only
    # ever legitimately either shipped inside the app (app_root) or rendered into
    # the app's own data dir (app_data_dir, which for a builtin lives in a
    # different tree than the packaged root), so the resolved path must sit under
    # one of those two roots. Resolve BEFORE the check so a symlink planted inside
    # the data dir cannot point back out.
    allowed_roots = [app_root.resolve(), app_data_dir(app_name).resolve()]
    resolved = path.resolve()
    if not any(_is_within(resolved, root) for root in allowed_roots):
        logger.warning(
            "Agent %s: prompt path escapes the app's own directories, ignoring: %s",
            agent_name,
            path,
        )
        return agent_data

    merged = dict(agent_data)
    merged["prompt"] = f"file://{resolved}"
    return merged


def _strip_ungoverned_auto_approve(
    servers: dict[str, Any],
    *,
    third_party: Container[str] = (),
    honour_owner_written: bool = True,
) -> dict[str, Any]:
    """Drop a ceiling-governed ``autoApprove`` from a server map (see governance).

    ``third_party`` names the keys THIS app chose in a map that also holds the
    owner's; ``honour_owner_written=False`` is for a map that is entirely an app's.
    """
    return dict(
        strip_ungoverned_auto_approve(
            servers,
            third_party=third_party,
            honour_owner_written=honour_owner_written,
        )
    )


def _may_auto_approve(ref: str) -> bool:
    """Whether ``ref`` may keep its auto-approve entry (see governance)."""
    return may_skip_gate_now(ref)


def _ceiling_filtered_allowed(refs: object, agent_name: str = "") -> list[Any]:
    """Drop auto-approve entries the ceiling has an opinion about.

    Applies to entries an app's PACKAGED agent JSON already carries (e.g.
    ``@<app>:<server>``), not only to ones registration appends: copying the
    template's list verbatim left a ceiling-denied server auto-approved, which
    is the one state the gate never sees.

    The decision itself is NOT made here. It is
    :func:`~kiro_crew.platform.governance.may_skip_gate`, because this is one of
    two places that write an ``allowedTools`` list — the host agent's shared-MCP
    sync (``sync_shared_server_refs`` in ``agent_materialization/mcp_sources.py``)
    is the other — so the rule is not reimplemented here. A local copy, including
    a private builtin-tool→scope map, would protect one write point and not the
    other, and a newly governed scope would silently re-open the shortcut for the
    copy that had not heard of it.
    """
    out: list[Any] = []
    withheld: list[str] = []
    for ref in refs if isinstance(refs, list) else []:
        if isinstance(ref, str) and not _may_auto_approve(ref):
            withheld.append(ref)
            continue
        out.append(ref)
    if withheld:
        # Withholding a template's own auto-approve is a permission DECISION, and
        # this app-agent writer is one of the three that produce that state — the
        # host shared-MCP sync and doctor emit this same SEL event, so a silent
        # drop here would be the one path with no audit trail. Never fail the
        # rebuild on an audit error.
        try:
            sel().log_api_access(
                caller="system",
                operation="mcp_auto_approve_withheld",
                outcome="ok",
                source="app_agent_materialization",
                resources=(
                    f"{', '.join(withheld)} mounted without auto-approve "
                    f"(governance ceiling) for agent {agent_name or '?'}; "
                    "calls go through the approval gate"
                ),
            )
        except Exception:  # noqa: BLE001 — audit must not break materialization
            logger.debug("SEL audit unavailable for app-agent withhold", exc_info=True)
    return out


def _apply_agent_mcp_policy(
    agent_data: dict[str, Any], agent_name: str, policy: dict[str, Any]
) -> dict[str, Any]:
    """Merge an app's MCP policy into one materialized agent config.

    kiro-cli loads the servers in the GLOBAL ``~/.kiro/settings/mcp.json`` into
    every agent regardless of that agent's own config — there is no "off"
    switch.  The only way to keep a server out of an agent's reach is to
    re-declare it in the agent's own ``mcpServers`` with its tools disabled,
    which is what a ``neutralize`` entry does.

    Policy shape (all keys optional)::

        {"agents": {"<agent-name>": {
            "servers":   {"<server>": {"autoApprove": [...], "disabledTools": [...]}},
            "neutralize": {"<server>": [<every tool name>]}}}}

    ``servers`` entries are granted (spec merged + ``@server`` added to
    ``tools``); ``neutralize`` entries are declared with every tool disabled and
    are NOT added to ``tools``.  Explicit tool lists rather than a wildcard: the
    app discovers the real tool names, so a server that grows a tool cannot
    quietly slip through a stale pattern.
    """
    per_agent = (policy.get("agents") or {}).get(agent_name) or {}
    granted = per_agent.get("servers") or {}
    neutralized = per_agent.get("neutralize") or {}

    # The ceiling filter below runs even with NO per-app policy: a template's own
    # `allowedTools` needs checking regardless of whether the user has granted or
    # neutralized anything, and returning early here left exactly those entries
    # unchecked.
    filtered_allowed = _ceiling_filtered_allowed(agent_data.get("allowedTools") or [], agent_name)
    if not granted and not neutralized:
        if filtered_allowed == list(agent_data.get("allowedTools") or []):
            return agent_data
        return {**agent_data, "allowedTools": filtered_allowed}

    servers = dict(agent_data.get("mcpServers") or {})
    tools = list(agent_data.get("tools") or [])
    allowed = filtered_allowed
    # Needed by BOTH branches: a policy entry names a server, it does not carry
    # the launch spec, so the real command/args/env come from the ambient config.
    ambient = _global_mcp_specs() if (granted or neutralized) else {}

    for name, spec in granted.items():
        if not isinstance(spec, dict):
            continue
        # A granted entry must be a COMPLETE server spec. The policy only carries
        # POLICY (autoApprove/disabledTools); without the ambient command/args on
        # top of it the entry is command-less, so the server never launches and
        # its tools are simply absent -- the agent reports "not available" with
        # nothing in any log. (Same defect the neutralize branch below had.)
        base = servers.get(name) or ambient.get(name) or _managed_mcp_spec(name)
        if not isinstance(base, dict) or not base.get("command"):
            logger.warning(
                "Skipping MCP grant for %r on agent %r: no launch spec found in "
                "the agent config, the global MCP config, or the host's managed "
                "servers",
                name,
                agent_name,
            )
            continue
        merged = {**base, **spec}
        merged.pop("neutralized", None)
        merged.pop("disabled", None)  # a grant un-disables a denied server
        # `mountOnly` (set by a policy for a built-in grant) means: mount the
        # server so the tool is visible, but keep it OFF allowedTools no matter
        # what the ceiling says, so every call routes through the approval gate
        # and the user's tool-trust settings decide. It is a policy directive,
        # not part of the kiro-cli server spec, so pop it before writing.
        mount_only = bool(merged.pop("mountOnly", False))
        # The POLICY is a third source of `autoApprove` (after the app manifest and
        # the managed specs), so the whole map is filtered once below rather than
        # here — see the pass at the end of this function.
        servers[name] = merged
        ref = f"@{name}"
        if ref not in tools:
            tools.append(ref)
        # allowedTools is the auto-approve list. A granted server that is only in
        # `tools` still prompts for every call, which for an unattended app agent
        # resolves to "rejected" -- the user asked for this server explicitly, so
        # granting it means granting its use.
        #
        # ...EXCEPT where the enterprise ceiling forbids it, OR the grant is
        # mountOnly. Auto-approve is the one path that never reaches
        # `hooks.on_tool_call`: kiro-cli only sends `session/request_permission`
        # for tools it must ask about, and the gate (where the governance deny
        # runs) hangs off that request. Writing a ceiling-denied — or mountOnly —
        # server here would route around the ONE control the docs promise cannot
        # be routed around. So the grant is intersected with the ceiling at write
        # time AND skipped entirely when mountOnly: permitted & not mountOnly ->
        # auto-approve as before; otherwise the grant stays in `tools` but NOT
        # here, which forces every call through request_permission / the approval
        # gate. A user may grant anything; whether it auto-runs remains the
        # policy's (and the user's trust settings') call.
        if ref not in allowed and not mount_only and _may_auto_approve(f"@{name}"):
            allowed.append(ref)
        elif ref not in allowed and mount_only:
            # A mountOnly grant deliberately withholds auto-approve — the same
            # permission DECISION the ceiling filter makes, so it emits the same
            # SEL audit event rather than being the one withhold path with no
            # trail. (The ceiling case is covered by _ceiling_filtered_allowed;
            # this is the mountOnly case, which never reaches that filter because
            # the ref was never added to `allowed`.) Never fail the rebuild on an
            # audit error.
            try:
                sel().log_api_access(
                    caller="system",
                    operation="mcp_auto_approve_withheld",
                    outcome="ok",
                    source="app_agent_materialization",
                    resources=(
                        f"@{name} mounted without auto-approve (mount-only grant) "
                        f"for agent {agent_name or '?'}; calls go through the "
                        "approval gate"
                    ),
                )
            except Exception:  # noqa: BLE001 — audit must not break materialization
                logger.debug("SEL audit unavailable for mount-only withhold", exc_info=True)

    for name, disabled in neutralized.items():
        if name in granted:
            continue  # granted wins — never neutralize a server we just allowed
        # A neutralize entry must be a COMPLETE server spec (command/args/env
        # copied from the global mcp.json) with the tools disabled on top.
        # kiro-cli's agent loader parses strictly: an mcpServers entry without a
        # command makes it reject the whole agent file, so a bare
        # {"disabledTools": [...]} would not "deny the server" — it would
        # silently unregister the agent itself ("Mode not found" at session
        # time, while `agent list`/`validate` still show it, because those use
        # a lenient parser). A server we cannot find a spec for is skipped:
        # kiro-cli treats an agent-file entry as an OVERRIDE of the same-named
        # global entry, so no spec to override means nothing to neutralize.
        base = servers.get(name) or ambient.get(name)
        if not isinstance(base, dict) or not base.get("command"):
            continue
        # ``disabled: true`` is honored by kiro-cli natively (verified with a
        # marker-command probe: the process is never launched) and does not trip
        # the strict parser. It is the primary deny — the server does not start
        # at all, so a session does not pay 14 idle server processes for a
        # policy that only wanted to hide them. ``disabledTools`` stays as
        # defense in depth for a kiro-cli that predates the flag: if the server
        # does start there, every tool is still dead.
        servers[name] = {
            **base,
            "disabled": True,
            "disabledTools": list(disabled) if isinstance(disabled, list) else [],
        }
        tools = without_mcp_refs(tools, (name,), per_tool=False)
        allowed = without_mcp_refs(allowed, (name,))

    agent_data["mcpServers"] = servers
    agent_data["tools"] = tools
    agent_data["allowedTools"] = allowed
    return agent_data


def _enforce_declared_mcp_tools(
    agent_data: dict[str, Any], manifest: AppManifest
) -> dict[str, Any]:
    """Make ``permissions.mcpTools`` actually scope an app agent's managed tools.

    The manifest field was validated at install and shown on the details page, but
    nothing narrowed what the app's own agent (and its cron sessions, which run on
    this same materialized spec) could call: the gateway mounts its managed servers
    into every agent regardless, so an app declaring a short ``mcpTools`` still had
    the full ``kirocrew-core`` / ``kirocrew-cron`` / ``kirocrew-dashboard`` surface.

    This turns the declaration into enforcement through the mechanism that already
    gates managed tools per agent: every managed tool the app did NOT declare is
    added to this agent's ``managedToolPolicy.exclude``, which the managed servers
    read per session (``GET /api/session-tool-policy``) and use to hide and refuse
    those tools. The exclusion is UNIONED with any exclude the app's own agent spec
    authored, so an app that already hand-excludes a managed tool keeps it.

    A no-op when the app declares no ``mcpTools`` (empty = unrestricted, matching
    :func:`~kiro_crew.apps.permissions.check_tool_permission`), and it never touches
    the app's own server tools or the agent's base built-ins -- only host-managed
    tool names are candidates. ``exclude`` and ``excludeServers`` are the only keys
    written; a present ``include`` or ``honourToolRefs`` in the agent's policy is
    preserved. A malformed authored ``managedToolPolicy``, ``exclude`` or
    ``excludeServers`` is returned untouched so the session gate keeps refusing it
    as ``policy_unreadable``.

    A managed server whose tool declaration could not be read is written to
    ``excludeServers``, which withholds that server's tools WHOLE: its names are
    unknown, so the name-based ``exclude`` cannot cover it, and leaving it out
    would leave every tool on it reachable.
    """
    from kiro_crew.apps.permissions import managed_tool_exclusion

    excluded, withheld_servers = managed_tool_exclusion(manifest)
    if not excluded and not withheld_servers:
        return agent_data

    if "managedToolPolicy" in agent_data and not isinstance(agent_data["managedToolPolicy"], dict):
        # A malformed authored policy (an explicit null included) is left exactly
        # as written: the per-session gate refuses every managed tool with
        # ``policy_unreadable`` for it, which is stricter than any exclusion derived
        # here. Rewriting it into a valid policy would turn that fail-closed
        # refusal into a narrower allow.
        return agent_data
    policy = dict(agent_data.get("managedToolPolicy", {}))
    for key in ("exclude", "excludeServers"):
        if key in policy and (
            not isinstance(policy[key], list) or not all(isinstance(t, str) for t in policy[key])
        ):
            return agent_data
    if excluded:
        policy["exclude"] = sorted(set(policy.get("exclude", [])) | set(excluded))
    if withheld_servers:
        policy["excludeServers"] = sorted(
            set(policy.get("excludeServers", [])) | set(withheld_servers)
        )

    merged = dict(agent_data)
    merged["managedToolPolicy"] = policy
    return merged


#: Most tool names one ``app_mcp_tools_restricted`` event lists by name; the rest
#: are counted, so the event stays bounded however large the managed surface grows.
_AUDIT_TOOL_NAMES_MAX = 20


def _audit_declared_mcp_tools(
    app_name: str, agent_name: str, manifest: AppManifest, written: dict[str, Any]
) -> None:
    """Record, in SEL, the managed-tool restriction an app agent was materialized with.

    Turning ``permissions.mcpTools`` into ``managedToolPolicy`` is a permission
    decision, so it gets one bounded audit event per written agent: the app, the
    agent, how many managed tools and which whole servers its spec withholds, and
    the first :data:`_AUDIT_TOOL_NAMES_MAX` tool names. Silent for an app that
    declares no ``mcpTools`` (nothing is derived) and for a policy the derivation
    left untouched because it was malformed. Never fails the write it follows.
    """
    if not manifest.permissions.mcpTools:
        return
    policy = written.get("managedToolPolicy")
    if not isinstance(policy, dict):
        return
    tools = policy.get("exclude", [])
    servers = policy.get("excludeServers", [])
    if not isinstance(tools, list) or not isinstance(servers, list):
        return
    if not tools and not servers:
        return
    shown = ", ".join(str(t) for t in tools[:_AUDIT_TOOL_NAMES_MAX])
    more = len(tools) - _AUDIT_TOOL_NAMES_MAX
    try:
        sel().log_api_access(
            caller="system",
            operation="app_mcp_tools_restricted",
            outcome="ok",
            source="app_agent_materialization",
            resources=(
                f"app {app_name} agent {agent_name}: {len(tools)} managed tool(s) "
                f"excluded [{shown}{f', +{more} more' if more > 0 else ''}]; "
                f"server(s) withheld whole: [{', '.join(str(s) for s in servers)}]"
            ),
        )
    except Exception:  # noqa: BLE001 — audit must not break materialization
        logger.debug("SEL audit unavailable for mcpTools restriction", exc_info=True)


def _own_mcp_servers(app_name: str) -> dict[str, Any]:
    """The app's OWN registered MCP servers, keyed as ``<app>:<server>``.

    Read back from the registered config rather than re-derived from the
    manifest so this inherits the health-gated live-port rewrite (an app with
    ``backend.port: "auto"`` carries an illustrative port in its manifest).

    Without this, an app that ships its own agent gets a DANGLING tool
    reference: ``_register_mcp_servers`` writes the server into Kiro Crew's own
    agent config, so ``@<app>:<server>`` in the app agent's ``tools`` pointed at
    a server that agent had never been told about — the app's own MCP simply did
    not load for it, silently.
    """
    prefix = f"{app_name}:"
    try:
        with _mcp_lock():
            data = _read_mcp_json_unlocked()
    except Exception as exc:  # noqa: BLE001 — never block agent materialization
        logger.warning("App %s: cannot read registered MCP servers: %s", app_name, exc)
        return {}
    servers = data.get("mcpServers") or {}
    return {k: v for k, v in servers.items() if k.startswith(prefix)}


def _managed_mcp_spec(name: str) -> dict[str, Any] | None:
    """Launch spec for a HOST-MANAGED MCP server, or None.

    Managed servers live in the host agent's config rather than the global MCP
    config, so a grant naming one finds nothing in the ambient map.
    """
    from kiro_crew.agent import _MANAGED_MCP_SERVERS

    managed = _MANAGED_MCP_SERVERS.get(name)
    if managed is None:
        return None
    invocation_fn = managed.get("invocation_fn")
    if invocation_fn is None:
        return None
    try:
        command, args = invocation_fn()
    except Exception:  # noqa: BLE001 -- a broken invocation must not block the agent
        return None
    return {"command": command, "args": list(args)}


def _materialize_managed_refs(agent_data: dict[str, Any]) -> None:
    """Materialize specs for host-managed MCP servers referenced in ``tools``.

    kiro-cli resolves a ``@server`` tool reference against the agent's own
    ``mcpServers`` plus the global ``mcp.json``. The host's managed servers
    (cron/core) are written into the HOST agent config only, so an app agent
    that lists them in ``tools`` holds a dangling reference unless the spec is
    copied in here.
    """
    from kiro_crew.agent import _MANAGED_MCP_SERVERS

    refs = {
        t[1:].split("/", 1)[0]
        for t in agent_data.get("tools") or []
        if isinstance(t, str) and t.startswith("@")
    }
    servers = agent_data.get("mcpServers") or {}
    for name, managed in _MANAGED_MCP_SERVERS.items():
        if name not in refs or name in servers:
            continue
        invocation_fn = managed.get("invocation_fn")
        if invocation_fn is None:
            continue
        try:
            command, args = invocation_fn()
        except Exception:  # noqa: BLE001 -- a broken invocation must not block the agent
            logger.warning("Could not resolve managed MCP server %r for an app agent", name)
            continue
        servers[name] = {"command": command, "args": list(args)}
    agent_data["mcpServers"] = servers


def _unresolvable_tool_refs(agent_data: dict[str, Any]) -> list[str]:
    """``@server``/``@server/tool`` grants whose server no config declares.

    kiro-cli resolves a ``@`` tool reference against the agent's own
    ``mcpServers`` plus the global ``mcp.json``; a name found in neither is
    dropped SILENTLY at mount time — the agent simply loses the tool with no
    exception and no log line anywhere. CI gates the shipped specs (see
    ``test_pptx_maker_provision.py``), but a user-installed app whose server
    failed to register is a runtime event CI cannot see, so the registration
    path — the last point that holds both the grants and the merged server
    map — reports it here.

    Diagnostic only: the caller logs a warning and still registers the agent.
    A dangling ref never mounts; it does not break the agent, so it must not
    become fatal. The global config is read only when a candidate survives the
    merged map (the common all-resolved case costs no I/O).
    """
    servers = agent_data.get("mcpServers")
    known = set(servers) if isinstance(servers, dict) else set()
    candidates: list[tuple[str, str]] = []
    for entry in agent_data.get("tools") or []:
        if not isinstance(entry, str) or not entry.startswith("@"):
            continue
        server = entry[1:].split("/", 1)[0]
        if server not in known:
            candidates.append((entry, server))
    if not candidates:
        return []
    if agent_data.get("includeMcpJson") is False:
        # The spec opts out of the global mcp.json, so kiro-cli will not
        # consult it at mount time — an ambient entry cannot rescue these refs
        # and treating it as resolvable would suppress the one signal that
        # exists for exactly the specs (mochi's) that set this flag.
        return [f"{entry} (server {server!r})" for entry, server in candidates]
    ambient = set(_global_mcp_specs())
    return [f"{entry} (server {server!r})" for entry, server in candidates if server not in ambient]


#: Keys the framework OWNS in a materialized app agent config: each is derived
#: from the manifest, the per-app MCP policy, or the running install, so a stale
#: value is a bug rather than a preference. Everything else a user hand-edits in
#: ``~/.kiro/agents/<app>--<agent>.json`` is theirs and survives a refresh (see
#: :func:`_preserve_user_agent_edits`).
_FRAMEWORK_OWNED_AGENT_KEYS = frozenset(
    {
        "name",  # the namespaced identity — renaming it orphans the agent
        "mcpServers",  # merged from the app's own servers + per-app policy
        "tools",  # policy grants/neutralizes decide what may mount
        "allowedTools",  # auto-approve list, intersected with the gov ceiling
        "prompt",  # generated (pet name / persona) — a path, not a preference
        # The two below are CONTAINMENT, not preference — see the note in
        # _preserve_user_agent_edits for why that distinction decides ownership.
        "managedToolPolicy",  # which managed tools an app agent may NOT reach
        "includeMcpJson",  # whether the global mcp.json bleeds into this agent
        # `resources` holds `file://` URIs into the app's own provisioned tree, written
        # with `{ENGINE_ROOT}`-style placeholders the GATEWAY renders (see
        # `_render_shipped_agent`). It is a generated path list, not a preference: a
        # user-pinned copy would keep pointing at a previous engine root and silently
        # stop resolving after a re-provision, exactly like `prompt` above.
        "resources",
    }
)


def _read_agent_config(path: Path) -> dict[str, Any] | None:
    """The agent JSON currently on disk, or None when there is nothing usable."""
    try:
        data = loads_user_json(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _preserve_user_agent_edits(
    name: str, prior: dict[str, Any] | None, fresh: dict[str, Any]
) -> dict[str, Any]:
    """Carry a user's own edits in an existing agent config across a refresh.

    Registration re-materializes app agent JSONs from the packaged template on
    every boot (see :func:`reconcile_enabled_app_resources`), which is what lets
    a template change take effect without a reinstall. But it wrote the file
    WHOLESALE, so anything the user had tuned by hand — ``model``, extra
    ``toolsSettings``, a ``description`` — was silently reverted on the next
    gateway start, with no warning and nothing to point at.

    Same split the managed-MCP refresh uses (``agent._refresh_dynamic_fields``):
    keys the framework derives are refreshed because a stale one is a BUG, and
    every other key present in the on-disk file wins over the template.

    "Can only have gotten there by the user" would be the natural reading of that
    second half, and it is WRONG — the previous boot's materialization wrote the
    template's own values there too. Nothing here records what the last template
    wrote, so there is no provenance to tell a hand edit from a stale copy of the
    template. Rather than introduce that provenance (a sidecar file to keep in
    sync, for the sake of two keys), the ownership line is drawn by what a key
    MEANS:

    * A **preference** (``model``, ``description``, extra ``toolsSettings``) is
      preserved. A template change that never reaches an existing install is the
      intended outcome — the user's choice outranks the template's default.
    * **Containment** (``managedToolPolicy``, ``includeMcpJson``) is framework-owned
      and always refreshed, because preserving it is wrong in BOTH directions: a
      template that later tightens ``managedToolPolicy.exclude`` would never reach
      an already-enabled install, AND anything that edits that file could drop the
      exclude list — with the old rule the framework then faithfully preserved the
      deletion forever. These are not user preferences to honour; they are the
      reason the app agent cannot reach ``spawn_run``/``cron_*``/``task_run`` or
      inherit the global mcp.json.

    ``prior`` is the snapshot taken BEFORE the file was replaced; None (missing,
    unreadable, or not an object) means "nothing to preserve" and the fresh
    config is returned unchanged rather than the refresh failing.
    """
    if not prior:
        return fresh

    merged = dict(fresh)
    kept: list[str] = []
    for key, value in prior.items():
        if key in _FRAMEWORK_OWNED_AGENT_KEYS:
            continue
        if merged.get(key) != value:
            merged[key] = value
            kept.append(key)
    if kept:
        logger.info("Preserved user edits in %s: %s", name, ", ".join(sorted(kept)))
    return merged


#: Placeholder syntax an agent template uses for a path only known at runtime.
#:
#: A shipped builtin's agent config is read from the immutable package root
#: (:func:`_registration_source`) so mutable installed metadata cannot borrow a
#: builtin's name. That is correct and stays. But it means a builtin whose agent
#: config must name a RUNTIME path — one under the data home, which ``KIROCREW_HOME``
#: can move, or a dependency resolved from the installed Python package — cannot
#: express it in the packaged file at all.
#:
#: `pptx-maker` is that case: its MCP server lives under the provisioned engine root
#: and is launched with `uv`, neither of which has a fixed location.


_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{[A-Z][A-Z0-9_]*\}")


#: Where the gateway writes the configs it renders itself. Under the data home,
#: alongside the agent symlinks, NOT in the app's install dir — see
#: :func:`_render_shipped_agent`.
_RENDERED_AGENTS_DIRNAME = "rendered-agents"


def _placeholder_values(app_name: str) -> dict[str, str]:
    """Trusted substitutions for *app_name*'s agent templates, or ``{}``.

    Every value is COMPUTED HERE, in the gateway, from the data home and the installed
    Python package — the same way the app's own provisioner computes them. Nothing is
    read back from disk, so there is nothing for an attacker with write access to the
    engine directory to influence.

    An unknown app gets ``{}``, which leaves its template unrendered and therefore
    unregistered (see :func:`_render_shipped_agent`) — fail-closed, so adding a
    placeholder to a new app's config is inert until its values are named here.
    """
    if app_name != "pptx-maker":
        return {}
    # Imported lazily and defensively: this is a builtin's own module, and a
    # registration path must not fail because one app's package is unimportable.
    try:
        from kiro_crew.apps.builtins.pptx_maker.backend import paths as pptx_paths
        from kiro_crew.apps.builtins.pptx_maker.backend import provision as pptx_provision
    except Exception:  # pragma: no cover - defensive
        logger.warning("App %s: cannot resolve placeholder values", app_name)
        return {}
    uv_bin = pptx_provision.resolve_uv()
    if not uv_bin:
        return {}
    return {
        "{UV_BIN}": uv_bin,
        "{ENGINE_ROOT}": str(pptx_paths.engine_root()),
        "{ENGINE_MCP_DIR}": str(pptx_paths.engine_mcp_dir()),
        "{APP_PROMPTS}": str(app_dir(app_name) / "prompts"),
        # The engine invokes `pdftoppm`/`soffice` BY NAME from inside its MCP
        # server, which kiro-cli spawns from this config — not from any gateway
        # subprocess. So the app's managed tool dir has to be on the PATH declared
        # here or the managed install is invisible at the one moment it matters.
        # Appended, so a real system poppler still wins the engine's own lookup.
        "{TOOLS_PATH}": pptx_provision.mcp_tools_path(),
    }


def _render_shipped_agent(
    app_name: str, agent_path: Path, io_failures: list[str] | None = None
) -> Path | None:
    """Render *agent_path*'s placeholders and return the gateway-written copy.

    Returns *agent_path* unchanged when the shipped file holds no placeholder, and
    ``None`` when it holds one that cannot be resolved (nothing is registered rather
    than registering a config with a literal ``{ENGINE_ROOT}`` in it).

    ``io_failures`` collects ONLY the write failure. This returns ``None`` for three
    different reasons and just one of them can succeed on a retry: an unresolved
    placeholder and invalid rendered JSON are properties of the template and will fail
    identically forever, so reporting them to a caller that retries would spin without
    ever converging. The ``OSError`` arm is the transient one.

    **The gateway renders the template itself.** An earlier version of this took the
    app's own provisioned copy from its install dir and verified it was "the template
    with only placeholders substituted". A reviewer pointed out why that is unsound and
    they were right: the check constrained WHERE a substitution could appear but not
    WHAT it could contain, and `{UV_BIN}` is an executable path — so an agent with
    write access to the engine dir could substitute its own binary and kiro-cli would
    run it. Constraining the shape of an injection point is not the same as trusting
    the value that lands in it.
    Since every value is computable here (:func:`_placeholder_values`), there is no
    reason to read any of them back from a directory the agent can write. Nothing on
    the mutable side is trusted now — the bytes come from the immutable package, the
    values from the gateway.

    The output goes under the DATA HOME next to the agent symlinks, not into the app's
    install dir, so the file kiro-cli reads is not one the app can rewrite afterwards.
    """
    try:
        template = agent_path.read_text(encoding="utf-8")
    except OSError:
        return agent_path
    if not _TEMPLATE_PLACEHOLDER_RE.search(template):
        return agent_path

    values = _placeholder_values(app_name)
    rendered = template
    for placeholder, value in values.items():
        # `json.dumps` minus the surrounding quotes: the placeholders sit INSIDE JSON
        # string literals, so a Windows path's backslashes have to be escaped or the
        # result is invalid JSON (or, worse, a path with a mangled separator).
        rendered = rendered.replace(placeholder, json.dumps(value)[1:-1])
    leftover = _TEMPLATE_PLACEHOLDER_RE.search(rendered)
    if leftover:
        logger.warning(
            "App %s: agent %s has an unresolved placeholder %s — not registered",
            app_name,
            agent_path.name,
            leftover.group(0),
        )
        return None
    try:
        json.loads(rendered)
    except ValueError:
        logger.warning("App %s: rendered agent %s is not valid JSON", app_name, agent_path.name)
        return None

    target_dir = _kiro_agents_dir().parent / _RENDERED_AGENTS_DIRNAME / app_name
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / agent_path.name
        atomic_write(target, rendered)
    except OSError as exc:
        logger.warning("App %s: could not write rendered agent: %s", app_name, exc)
        if io_failures is not None:
            io_failures.append(str(agent_path))
        return None
    return target


def _register_agents(
    app_name: str,
    manifest: AppManifest,
    app_root: Path,
    io_failures: list[str] | None = None,
) -> list[str]:
    """Materialize app agent JSONs into ~/.kiro/agents/ with namespaced names.

    ``io_failures``, when supplied, collects the agents skipped because of an OS-level
    read or write error. That is deliberately NARROWER than "declared minus registered":
    most skips here are PERMANENT refusals — a path escaping the app root, an unsafe
    agent name, malformed JSON, an unresolved template placeholder — and retrying those
    never converges. Only the I/O class can succeed on a later attempt, so only it is
    worth reporting to a caller that retries.

    Written as a COPY, not a symlink, for two reasons:

    * the source may be inside the installed Python package (a builtin), which
      must stay read-only, yet the agent config needs per-user MCP policy merged
      in (see :func:`_apply_agent_mcp_policy`);
    * a copy is regenerated from the template on every registration, and the
      gateway reconciles registration at startup, so an edit to the packaged
      template still takes effect on the next boot.

    Returns list of registered agent names (namespaced).
    """
    registered: list[str] = []
    dispatchable: set[str] = set()
    # Held across the whole materialization, not just the writes: each agent COPIES the
    # ambient server spec, so a read taken before a health scrub and a write landing
    # after it would leave the agent naming a server that does not exist. The read and
    # the write have to be inside the same critical section as the transition they race.
    #
    # Kept INLINE rather than delegated to a helper: the governed-auto-approve strip
    # below is a write chokepoint that `test_both_config_writers_run_the_pass` asserts by
    # inspecting THIS function's source. Moving the body elsewhere would keep the
    # behaviour and silently retire the guarantee.
    with _health_reconcile_guard():
        agents_dir = _kiro_agents_dir()
        agents_dir.mkdir(parents=True, exist_ok=True)
        policy = _agent_mcp_policy(app_name)
        own_servers = _own_mcp_servers(app_name)

        screened_root = screen_app_path(app_root) if manifest.agents else Containment.UNCHECKED
        for agent_path_str in manifest.agents:
            agent_path = app_root / agent_path_str
            # Path containment check — reject paths that escape the app root
            contained = contained_in_screened_root(agent_path, screened_root)
            if contained is Containment.UNCHECKED:
                # Unverified is not refused: report it as retryable.
                logger.warning("App %s: agent path could not be checked: %s", app_name, agent_path)
                if io_failures is not None:
                    io_failures.append(str(agent_path))
                continue
            if contained is Containment.REFUSED:
                logger.warning(
                    "App %s: agent path is refused by the path screen: %s", app_name, agent_path
                )
                continue
            if contained is Containment.OUTSIDE:
                logger.warning("App %s: agent path escapes app root: %s", app_name, agent_path)
                continue
            if not agent_path.is_file():
                logger.warning("App %s: agent file not found: %s", app_name, agent_path)
                continue
            # A shipped TEMPLATE is rendered BY THE GATEWAY, from values it computes
            # itself, into a file under the data home. `None` means a placeholder could
            # not be resolved, so nothing is registered for this agent rather than
            # registering a config that names a literal `{ENGINE_ROOT}`.
            resolved = _render_shipped_agent(app_name, agent_path, io_failures=io_failures)
            if resolved is None:
                continue
            agent_path = resolved

            # Read agent JSON to get the agent name
            try:
                agent_data = json.loads(agent_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("App %s: unreadable agent %s: %s", app_name, agent_path, exc)
                if isinstance(exc, OSError) and io_failures is not None:
                    io_failures.append(str(agent_path))  # transient; a malformed spec is not
                continue
            if not isinstance(agent_data, dict):
                # Valid JSON that is not an object (a list, a scalar, null) parses
                # fine, but every `.get` below would raise. Same disposition as the
                # unreadable case: skip this agent rather than register a config
                # the spec never described.
                logger.warning(
                    "App %s: agent spec %s is not a JSON object; skipping", app_name, agent_path
                )
                continue
            agent_name = agent_data.get("name", agent_path.stem)

            # The agent name is app-controlled (read from the agent JSON) and is
            # about to become a filesystem path component. Reject any path separator
            # or parent-dir token BEFORE constructing link_path: on Windows a name
            # like "..\\..\\crew\\config" would otherwise traverse out of the agents
            # dir (backslash is a separator there) and atomic_write would overwrite
            # an arbitrary JSON file such as ~/.kiro/crew/config.json.
            if (
                not isinstance(agent_name, str)
                or "/" in agent_name
                or "\\" in agent_name
                or "\x00" in agent_name
                or agent_name in ("", ".", "..")
            ):
                logger.warning("App %s: refusing agent with unsafe name %r", app_name, agent_name)
                continue

            # Namespaced link name: app-name--agent-name.json
            link_name = _safe_link_name(_namespace(app_name, agent_name)) + ".json"
            link_path = agents_dir / link_name

            # Snapshot the user's own edits BEFORE the unlink below — after it there
            # is nothing left to read (see _preserve_user_agent_edits).
            prior_on_disk = _read_agent_config(link_path)

            # Drop a legacy SYMLINK from an older Kiro Crew (which pointed at a file
            # inside the app) so the write below lands a real file at this path.
            #
            # NOTHING is unlinked first — not even a legacy symlink. atomic_write does
            # tmp+os.replace, which atomically swaps the destination NAME whether it is
            # a regular file OR a symlink (rename operates on the path, not the
            # symlink target), so the new real file replaces the old link in one step.
            # Unlinking first (for either kind) opened a window where the working
            # config was already gone and the replacement had not landed — a write
            # that then failed (disk full, at startup reconciliation) made the agent
            # DISAPPEAR. Leaving the old entry in place means a failed write leaves the
            # last-good config untouched.
            try:
                # The app's own servers are always granted -- they are declared by
                # the manifest, not chosen by the user, and the agent's `tools`
                # already references them.
                if own_servers:
                    agent_data["mcpServers"] = {
                        **own_servers,
                        **(agent_data.get("mcpServers") or {}),
                    }
                # An app agent may also reference the host's managed servers
                # (@kirocrew-cron / @kirocrew-core) in `tools`. Those specs live in
                # the HOST agent's config, not the global mcp.json, so without this
                # merge the reference dangles and the tool silently never mounts.
                _materialize_managed_refs(agent_data)
                merged = _apply_agent_mcp_policy(agent_data, agent_name, policy)
                # Enforce permissions.mcpTools: withhold every host-managed tool the
                # app did not declare via managedToolPolicy.exclude. BEFORE
                # _preserve_user_agent_edits, which treats managedToolPolicy as
                # framework-owned and refreshes it from `merged` — so the enforced
                # value is the one carried across the refresh.
                merged = _enforce_declared_mcp_tools(merged, manifest)
                merged = _apply_agent_prompt(merged, agent_name, policy, app_name, app_root)
                merged = _preserve_user_agent_edits(link_name, prior_on_disk, merged)
                # LAST governance pass, on the map that is about to be written. By this
                # point `autoApprove` could have come from the app's own manifest, the
                # per-agent MCP policy, a materialized managed ref, or a preserved
                # on-disk entry — filtering each of those separately is how earlier
                # rounds kept leaving one open. The host agent's writer does the same
                # thing at the same position (see agent.install_agent).
                #
                # This map is an APP's, so it declines the owner-written opt-in
                # outright instead of naming the app's keys. Naming them is an
                # enumeration that goes stale: the manifest's servers, the shipped
                # agent spec's own un-namespaced keys and the per-agent policy's
                # `servers` keys were each found only after the previous one was
                # covered, and a policy IS app-controlled state that may carry
                # `autoApprove` (a shipped builtin already writes one). Nothing here
                # is the owner stating a preference about their own tools, so the
                # verbs a spec DECLARES still survive and everything else routes
                # through the approval gate.
                _servers = merged.get("mcpServers")
                if isinstance(_servers, dict):
                    merged["mcpServers"] = _strip_ungoverned_auto_approve(
                        _servers, honour_owner_written=False
                    )
                # The map above is FINAL — every source of servers has been merged —
                # so this is the one point a dangling `@` grant is decidable. Warn,
                # never reject: kiro-cli just skips the ref, so the agent works
                # minus the tool, and the warning is the only signal that exists.
                dangling = _unresolvable_tool_refs(merged)
                if dangling:
                    logger.warning(
                        "App %s: agent %r grants MCP tool ref(s) not found in this "
                        "agent's merged mcpServers or the global mcp.json; kiro-cli "
                        "will silently never mount them: %s",
                        app_name,
                        agent_name,
                        ", ".join(dangling),
                    )
                # The ownership decision and the write share one critical section,
                # the agent-spec lock every spec writer of this directory holds: an
                # instance on a data home that does not own the shared agents
                # directory leaves the owner's copy in place rather than stamping its
                # own launcher and data home into a spec the owner's sessions load. A
                # refusal is permanent until ownership changes, so it is not an I/O
                # failure a retry could clear. Imported here, like every other read of
                # the agent facade in this module.
                from kiro_crew import agent as _agent

                with _agent.agents_spec_lock(agents_dir):
                    if _agent._declined_foreign_spec_write(link_path):
                        continue
                    atomic_write(link_path, json.dumps(merged, indent=2) + "\n")
                _audit_declared_mcp_tools(app_name, agent_name, manifest, merged)
                registered.append(_namespace(app_name, agent_name))
                # The DECLARED name only — kiro-cli enumerates agents by their
                # `name` field, so the namespaced filename stem is not a name it
                # can resolve (see _scan_materialized_agents).
                dispatchable.add(agent_name)
                logger.info("Registered agent: %s (from %s)", link_name, agent_path)
            except OSError as exc:
                logger.warning("Failed to write agent %s: %s", link_name, exc)
                if io_failures is not None:
                    io_failures.append(link_name)

        if dispatchable:
            # Publish the names just written BEFORE scheduling the rescan, and do it
            # synchronously: publishing is a pure set union with no filesystem access,
            # while the rescan can be delayed arbitrarily if the default executor is
            # saturated. That window is not cosmetic — a slot created inside it would
            # resolve to the default agent, so the app's first turn after being enabled
            # would be answered by the wrong agent.
            publish_materialized_agents(dispatchable)
        # Reconcile the whole directory UNCONDITIONALLY, even when this call wrote
        # nothing: a re-registration whose manifest does not declare an agent (or
        # that follows a prune) leaves the removed name in the snapshot, and only a
        # rescan drops it. A name that is dispatchable in memory but gone from disk is
        # an invisible mismatch — kiro-cli cannot load it and falls back to its own
        # default. `_register_agents` runs ON the loop for the dashboard enable/update
        # handlers (see the prune note in `register_app`), so the scan goes to an
        # executor rather than walking every agent file inline.
        schedule_materialized_agents_refresh()

        return registered


def _deregister_agents(app_name: str) -> int:
    """Remove all agent symlinks for an app from ~/.kiro/agents/."""
    prefix = _safe_link_name(app_name + "/")
    removed = 0
    agents_dir = _kiro_agents_dir()
    if not agents_dir.is_dir():
        return 0
    for entry in agents_dir.iterdir():
        if entry.name.startswith(prefix) and entry.name.endswith(".json"):
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    # The launch copies the app's palette sessions ran under (apps.agent_launch)
    # describe the specs just removed, so they go with them.
    from kiro_crew.apps.agent_launch import remove_app_agent_launches

    removed += remove_app_agent_launches(app_name)
    if removed:
        logger.info("Deregistered %d agent(s) for app %s", removed, app_name)
        # Drop the removed names from the resolver's snapshot. Without this a
        # disabled app's agent stays dispatchable in memory: a slot still bound to
        # it would hand kiro-cli a name whose config is gone, and the turn fails
        # instead of falling back. Mirrors the refresh in `_register_agents`, and
        # goes through the scheduler for the same reason — deregistration runs ON
        # the loop for the dashboard disable/update handlers, so the directory scan
        # belongs on an executor.
        schedule_materialized_agents_refresh()
    return removed


def _unlink_unheld_app_lock(path: Path, identity: tuple[int, int]) -> bool:
    """Remove the lock file at *path* only while nobody holds it, and only that inode.

    A launcher may still hold an old ``<stem>.lock``; unlinking it then would let
    the next launcher lock a fresh inode while the first still holds the old one.
    So the file is opened without following links (with delete-sharing on
    Windows, so our own handle does not block the unlink), its identity and
    emptiness checked, and an exclusive lock taken without waiting: a held lock is
    left alone, and the identity-verified unlink happens while ours is held.

    Local to the apps package rather than reused from ``kiro_crew.acp``: the
    agent-SDK import boundary (``scripts/check_agent_sdk_boundary.py``) forbids
    application code from importing the ACP backend directly.
    """
    try:
        fd = platform_compat.open_lock_file_for_sweep(path)
    except OSError:
        return False
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != identity or opened.st_size != 0:
            return False
        if not platform_compat.try_acquire_lock(fd, exclusive=True):
            return False
        return pinned_fs.unlink_verified_by_name(path.parent, path.name, identity)
    except OSError:
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def purge_uninstalled_agent_residue(app_name: str) -> None:
    """Remove the per-agent files an uninstalled app's agents left behind.

    :func:`_deregister_agents` unlinks ``<app>--<agent>.json`` from the agents
    directory, but two files derived from that same name outlive it:

    * ``~/.kiro/agents/<app>--<agent>.lock`` -- the empty per-spec lock a
      launcher takes beside each spec it rewrites.
    * ``<mcp_gateway.overlay_dir>/<app>--<agent>.json`` -- the MCP gateway's
      rewritten copy of the spec. The rewriter prunes it only on its next pass
      (broker start), so until then a session asking for the agent by its bare
      name -- including one of a DIFFERENT app reinstalled under this name --
      would be handed stubs for servers the removed app declared.

    Called at uninstall only, never on disable: a disabled app's overlay is still
    the one its re-enable would want. Both names are derived exactly as
    registration derives them (the ``<app>--`` namespace; kebab app names cannot
    contain ``--``, so the prefix never matches another app), and each is left
    alone while its source spec still exists. A lock goes through
    :func:`_unlink_unheld_app_lock`, which removes only a regular, empty, unheld
    file and unlinks it while holding its lock, so a launcher that still holds it
    is never raced. Best-effort: a missing file or an unreadable
    directory is not an error.
    """
    prefix = _safe_link_name(app_name + "/")
    agents_dir = _kiro_agents_dir()
    removed: list[str] = []

    def _spec_present(stem: str) -> bool:
        return (agents_dir / f"{stem}.json").exists()

    try:
        locks = list(agents_dir.glob(f"{prefix}*.lock"))
    except OSError:
        locks = []
    for lock in locks:
        if _spec_present(lock.stem):
            continue
        try:
            info = os.lstat(lock)
        except OSError:
            continue
        # lstat: a link or junction at the name is not S_ISREG, so it is refused
        # outright and never followed.
        if not stat.S_ISREG(info.st_mode):
            continue
        if _unlink_unheld_app_lock(lock, (info.st_dev, info.st_ino)):
            removed.append(str(lock))

    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.mcp_gateway.rewriter import resolve_overlay_dir

        overlay_dir = resolve_overlay_dir(KiroCrewConfig.load().mcp_gateway.overlay_dir)
        overlays = list(overlay_dir.glob(f"{prefix}*.json"))
    except Exception:  # noqa: BLE001 - best-effort cleanup must not fail an uninstall
        logger.debug("could not list MCP gateway overlays for %s", app_name, exc_info=True)
        overlays = []
    for overlay in overlays:
        if _spec_present(overlay.stem):
            continue
        try:
            # lstat, so a link at the name is refused rather than followed.
            if not stat.S_ISREG(os.lstat(overlay).st_mode):
                continue
            overlay.unlink()
            removed.append(str(overlay))
        except OSError:
            pass

    if removed:
        logger.info("Removed %d leftover agent file(s) for app %s", len(removed), app_name)
