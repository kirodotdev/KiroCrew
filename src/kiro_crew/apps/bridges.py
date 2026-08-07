"""Registration bridges — wire app resources into KiroCrew's runtime.

When an app is installed or enabled, its agents, skills, and cron jobs need
to be registered with KiroCrew's existing systems.  This module provides
``register_app`` and ``deregister_app`` which handle the namespacing and
symlink/copy operations.

Namespace convention: ``{app_name}/{resource_name}`` to avoid collisions
between apps.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlparse, urlunparse

from kiro_crew import platform_compat
from kiro_crew.apps.cron_sdk import CronSDK
from kiro_crew.apps.execution import (
    app_execution_denied,
    shipped_builtin_app_root,
)
from kiro_crew.apps.manager import (
    app_data_dir,
    app_dir,
    get_app,
    get_app_manifest,
    list_apps,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import (
    config_dir,
    publish_materialized_agents,
    schedule_materialized_agents_refresh,
)
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.cron import CronStoreBusy
from kiro_crew.cron_script import resolve_script_path
from kiro_crew.platform.governance import may_skip_gate_now, strip_ungoverned_auto_approve
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Where kiro-cli looks for agent definitions.
#
# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home" and
# issue #874; dashboard/handlers/usage.py is the reference implementation.
KIRO_AGENTS_DIR: Path | None = None


def _kiro_agents_dir() -> Path:
    """The kiro-cli agents directory, resolved against the live data home."""
    return KIRO_AGENTS_DIR if KIRO_AGENTS_DIR is not None else kiro_agents_dir()


# Where KiroCrew loads skills from
SKILLS_DIR_NAME = "skills"


def _app_resource_root(app_name: str) -> Path:
    """Where an app's declarative resources (agents, skills) actually live.

    A builtin's resources live in the PACKAGE, not the data home. Resolving them
    against the data home always misses — silently, because registration only
    logs a warning — which is how the first builtin to declare agents/skills got
    zero of them registered while its mcpServers (needing no path) registered
    fine.

    Delegates to :func:`shipped_builtin_app_root` rather than deriving the
    directory from the name. That primitive matches on the shipped manifest's own
    ``name`` field after ``resolve(strict=True)`` + containment, so it also
    handles the hyphen/underscore convention (`auto-research` ships as
    `builtins/auto_research`) without a normalising step here, and the path comes
    from a directory listing rather than from the input. Same resolution the
    lifecycle hook loader uses, so the two cannot disagree about where an app is.
    """
    return shipped_builtin_app_root(app_name) or app_dir(app_name)


def _skills_dir() -> Path:
    return config_dir() / SKILLS_DIR_NAME


def app_skills_dir(app_name: str) -> Path:
    """Where this app's skills are registered, as an absolute path.

    Public because an app may need to TELL ITS OWN AGENT where its skills
    landed. Session context injects the skill catalogue only for the built-in
    agent (``ContextBuilder`` skips it for custom agents, which are expected to
    resolve their own), and an agent JSON ships inside the app, so it can only
    name paths that exist at packaging time — neither route can express a
    data-home path. An app that renders its agent prompt at runtime can, so
    hand it the directory rather than making it re-derive the layout.

    The namespaced directory is returned, not the flat one: the flat link is
    skipped for reserved names and can be shadowed by another app, while the
    namespaced path is always this app's own.
    """
    return _skills_dir() / app_name


def _namespace(app_name: str, resource_name: str) -> str:
    """Build a namespaced resource name: ``app_name/resource_name``."""
    return f"{app_name}/{resource_name}"


def _safe_link_name(namespaced: str) -> str:
    """Convert ``app/resource`` to a safe filename for symlinks: ``app--resource``.

    Neutralizes BOTH path separators, not just ``/``: on Windows a backslash is
    a separator too, so a resource name carrying ``\\`` would otherwise let the
    constructed link path traverse out of the agents directory.
    """
    return namespaced.replace("/", "--").replace("\\", "--")


def _registration_source(app_name: str) -> tuple[AppManifest | None, Path]:
    """Return the authoritative manifest and root for executable resources.

    A shipped builtin is always read from its immutable package root. This
    prevents mutable installed metadata from borrowing a builtin name and then
    registering attacker-controlled agents, skills, crons, or MCP servers.
    Third-party apps continue to use their installed snapshot.
    """
    shipped_root = shipped_builtin_app_root(app_name)
    if shipped_root is not None:
        try:
            return (
                AppManifest.from_json_file(shipped_root / "app.json"),
                shipped_root,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "App %s: shipped resource manifest is unreadable: %s",
                app_name,
                exc,
            )
            return None, shipped_root
    return get_app_manifest(app_name), app_dir(app_name)


def _registration_denied(
    app_name: str,
    *,
    action: str,
    app_root: Path,
) -> str | None:
    """Apply the shared execution decision to an executable-resource bridge."""
    denied = app_execution_denied(
        app_name,
        action=action,
        app_root=app_root,
        caller="app_bridge",
    )
    if denied:
        logger.warning(
            "App %s: skipping executable resource registration (%s): %s",
            app_name,
            action,
            denied,
        )
    return denied


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
        raw = json.loads(
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


def _strip_ungoverned_auto_approve(servers: dict[str, Any]) -> dict[str, Any]:
    """Drop a ceiling-governed ``autoApprove`` from a server map (see governance)."""
    return dict(strip_ungoverned_auto_approve(servers))


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
    sync in ``agent.py`` is the other — and the earlier revision reimplemented
    the rule locally, including a private copy of the builtin-tool→scope map. One
    copy meant one write point was protected and the other was not, and a newly
    governed scope silently re-opened the shortcut for the copy that had not heard
    of it.
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
        merged.pop("disabled", None)  # a grant un-disables a previously denied server
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
        # ...EXCEPT where the enterprise ceiling forbids it. Auto-approve is the
        # one path that never reaches `hooks.on_tool_call`: kiro-cli only sends
        # `session/request_permission` for tools it must ask about, and the gate
        # (where the governance deny runs) hangs off that request. Writing a
        # ceiling-denied server here would therefore route around the ONE control
        # the docs promise cannot be routed around. So the grant is intersected
        # with the ceiling at write time: permitted -> auto-approve as before;
        # denied -> the grant stays in `tools` but NOT here, which forces every
        # call through request_permission, where the gate denies it. A user may
        # grant anything; whether it RUNS remains the policy's call.
        if ref not in allowed and _may_auto_approve(f"@{name}"):
            allowed.append(ref)

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
        tools = [t for t in tools if t != f"@{name}"]
        allowed = [t for t in allowed if t != f"@{name}"]

    agent_data["mcpServers"] = servers
    agent_data["tools"] = tools
    agent_data["allowedTools"] = allowed
    return agent_data


#: The host CLI name a builtin app may declare as its MCP ``command``.
_HOST_CLI_COMMAND = "kirocrew"


def _pin_host_cli_command(app_name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Rewrite ``command: "kirocrew"`` to THIS gateway's interpreter.

    A builtin app's MCP server is the gateway's own code — but declaring the host
    CLI by name makes kiro-cli spawn it via a PATH lookup, which is wrong in two
    ways that both fail silently:

    * the launcher on PATH may belong to a DIFFERENT KiroCrew install, so the
      server reads a different data home than the gateway that registered it and
      reports the app as not installed;
    * that launcher may simply be broken (a stale wrapper whose interpreter is
      gone), in which case the server never starts and its tools never register —
      the agent just has no such tools, with no error surfaced anywhere.

    Pinning ``sys.executable -m kiro_crew`` plus PYTHONPATH and KIROCREW_HOME makes
    the spawned server provably the same code and the same data home as the
    gateway. Only applied to the host CLI name; an app shipping its own binary is
    left alone.
    """
    if cfg.get("command") != _HOST_CLI_COMMAND:
        return cfg
    import sys

    pkg_parent = str(Path(kiro_crew_file()).parent.parent)
    env = dict(cfg.get("env") or {})
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{pkg_parent}{os.pathsep}{existing}" if existing else pkg_parent
    env.setdefault("KIROCREW_HOME", str(app_dir(app_name).parent.parent))
    cfg["command"] = sys.executable
    cfg["args"] = ["-s", "-m", "kiro_crew", *list(cfg.get("args") or [])]
    cfg["env"] = env
    return cfg


def kiro_crew_file() -> str:
    """Path to the running ``kiro_crew`` package's ``__init__``."""
    import kiro_crew

    return str(kiro_crew.__file__)


def _own_mcp_servers(app_name: str) -> dict[str, Any]:
    """The app's OWN registered MCP servers, keyed as ``<app>:<server>``.

    Read back from the registered config rather than re-derived from the
    manifest so this inherits the health-gated live-port rewrite (an app with
    ``backend.port: "auto"`` carries an illustrative port in its manifest).

    Without this, an app that ships its own agent gets a DANGLING tool
    reference: ``_register_mcp_servers`` writes the server into KiroCrew's own
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
        t[1:] for t in agent_data.get("tools") or [] if isinstance(t, str) and t.startswith("@")
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
        data = json.loads(path.read_text(encoding="utf-8"))
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


def _render_shipped_agent(app_name: str, agent_path: Path) -> Path | None:
    """Render *agent_path*'s placeholders and return the gateway-written copy.

    Returns *agent_path* unchanged when the shipped file holds no placeholder, and
    ``None`` when it holds one that cannot be resolved (nothing is registered rather
    than registering a config with a literal ``{ENGINE_ROOT}`` in it).

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
        return None
    return target


def _register_agents(app_name: str, manifest: AppManifest, app_root: Path) -> list[str]:
    """Materialize app agent JSONs into ~/.kiro/agents/ with namespaced names.

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
    agents_dir = _kiro_agents_dir()
    agents_dir.mkdir(parents=True, exist_ok=True)
    policy = _agent_mcp_policy(app_name)
    own_servers = _own_mcp_servers(app_name)

    for agent_path_str in manifest.agents:
        agent_path = app_root / agent_path_str
        # Path containment check — reject paths that escape the app root
        if not agent_path.resolve().is_relative_to(app_root.resolve()):
            logger.warning("App %s: agent path escapes app root: %s", app_name, agent_path)
            continue
        if not agent_path.is_file():
            logger.warning("App %s: agent file not found: %s", app_name, agent_path)
            continue
        # A shipped TEMPLATE is rendered BY THE GATEWAY, from values it computes
        # itself, into a file under the data home. `None` means a placeholder could
        # not be resolved, so nothing is registered for this agent rather than
        # registering a config that names a literal `{ENGINE_ROOT}`.
        resolved = _render_shipped_agent(app_name, agent_path)
        if resolved is None:
            continue
        agent_path = resolved

        # Read agent JSON to get the agent name
        try:
            agent_data = json.loads(agent_path.read_text(encoding="utf-8"))
            agent_name = agent_data.get("name", agent_path.stem)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("App %s: unreadable agent %s: %s", app_name, agent_path, exc)
            continue

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

        # Drop a legacy SYMLINK from an older KiroCrew (which pointed at a file
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
            merged = _apply_agent_prompt(merged, agent_name, policy, app_name, app_root)
            merged = _preserve_user_agent_edits(link_name, prior_on_disk, merged)
            # LAST governance pass, on the map that is about to be written. By this
            # point `autoApprove` could have come from the app's own manifest, the
            # per-agent MCP policy, a materialized managed ref, or a preserved
            # on-disk entry — filtering each of those separately is how earlier
            # rounds kept leaving one open. The host agent's writer does the same
            # thing at the same position (see agent.install_agent).
            _servers = merged.get("mcpServers")
            if isinstance(_servers, dict):
                merged["mcpServers"] = _strip_ungoverned_auto_approve(_servers)
            atomic_write(link_path, json.dumps(merged, indent=2) + "\n")
            registered.append(_namespace(app_name, agent_name))
            # The DECLARED name only — kiro-cli enumerates agents by their
            # `name` field, so the namespaced filename stem is not a name it
            # can resolve (see _scan_materialized_agents).
            dispatchable.add(agent_name)
            logger.info("Registered agent: %s (from %s)", link_name, agent_path)
        except OSError as exc:
            logger.warning("Failed to write agent %s: %s", link_name, exc)

    if dispatchable:
        # Publish the names just written BEFORE scheduling the rescan, and do it
        # synchronously: publishing is a pure set union with no filesystem access,
        # while the rescan can be delayed arbitrarily if the default executor is
        # saturated. That window is not cosmetic — a slot created inside it would
        # resolve to the default agent, so the app's first turn after being enabled
        # would be answered by the wrong agent.
        publish_materialized_agents(dispatchable)
    # Reconcile the whole directory UNCONDITIONALLY, even when this call wrote
    # nothing: a re-registration whose manifest no longer declares an agent (or
    # that follows a prune) leaves the removed name in the snapshot, and only a
    # rescan drops it. A name that is dispatchable in memory but gone from disk is
    # the same invisible mismatch as the bug this change fixes — kiro-cli cannot
    # load it and falls back to its own default. `_register_agents` runs ON the
    # loop for the dashboard enable/update handlers (see the prune note in
    # `register_app`), so the scan goes to an executor rather than walking every
    # agent file inline.
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


# ---------------------------------------------------------------------------
# Skill registration
# ---------------------------------------------------------------------------

_RESERVED_SKILL_DIRS = {"auto"}


def _register_skills(app_name: str, manifest: AppManifest, app_root: Path) -> list[str]:
    """Symlink app skill directories into ~/.kiro/crew/skills/.

    Creates both a namespaced link (``skills/{app_name}/{skill_name}``) and a
    flat link (``skills/{skill_name}``) so the skill scanner finds the skill
    regardless of whether it walks subdirectories or only checks the top level.

    Returns list of registered skill names (namespaced).
    """
    registered: list[str] = []
    if not manifest.skills:
        # Nothing to link. Creating the namespaced dir anyway leaves an EMPTY
        # ``skills/<app_name>/`` behind, and when a PACKAGED builtin skill shares
        # the app's name that empty dir masks it: ``_ensure_builtin_skills`` copies
        # at gateway start, app registration runs after, and the mkdir then leaves a
        # directory whose ``SKILL.md`` is gone — so every SOP the app's cron prompts
        # point at silently does not exist on disk. Observed with ops-mission-control,
        # whose skill ships under ``builtin_skills/`` precisely because a builtin's
        # app dir is never copied into the data home.
        return registered

    skills_root = _skills_dir()
    app_skills_dir = skills_root / app_name
    app_skills_dir.mkdir(parents=True, exist_ok=True)

    for skill_path_str in manifest.skills:
        skill_path = app_root / skill_path_str
        if not skill_path.resolve().is_relative_to(app_root.resolve()):
            logger.warning("App %s: skill path escapes app root: %s", app_name, skill_path)
            continue
        if not skill_path.is_dir():
            logger.warning("App %s: skill directory not found: %s", app_name, skill_path)
            continue

        skill_name = skill_path.name

        # Namespaced link: ~/.kiro/crew/skills/{app_name}/{skill_name}
        link_path = app_skills_dir / skill_name
        if link_path.exists() or platform_compat.is_link_or_junction(link_path):
            if platform_compat.is_link_or_junction(link_path):
                # Symlink OR Windows junction — remove the link, not its target.
                platform_compat.unlink_link_or_junction(link_path)
            else:
                shutil.rmtree(link_path)

        # Flat link: ~/.kiro/crew/skills/{skill_name} (for skill scanner)
        if skill_name in _RESERVED_SKILL_DIRS:
            logger.info("App %s: skipping flat link for reserved name %s", app_name, skill_name)
            flat_link = None
        else:
            flat_link = skills_root / skill_name
            if flat_link.exists() or platform_compat.is_link_or_junction(flat_link):
                if platform_compat.is_link_or_junction(flat_link):
                    platform_compat.unlink_link_or_junction(flat_link)
                else:
                    logger.info(
                        "App %s: skipping flat link for %s — non-symlink dir exists",
                        app_name,
                        skill_name,
                    )
                    flat_link = None  # type: ignore[assignment]

        try:
            # symlink on POSIX; directory junction on non-admin Windows (a plain
            # os.symlink there raises WinError 1314 and would silently drop every
            # app skill for the ordinary user).
            platform_compat.symlink_or_junction(str(skill_path), str(link_path))
            if flat_link is not None:
                platform_compat.symlink_or_junction(str(skill_path), str(flat_link))
            namespaced = _namespace(app_name, skill_name)
            registered.append(namespaced)
            logger.info("Registered skill: %s -> %s", namespaced, skill_path)
        except OSError as exc:
            logger.warning("Failed to link skill %s: %s", skill_name, exc)

    if registered:
        sel().log_tool_invocation(
            session_key="",
            agent="kirocrew",
            source="app_bridge",
            tool_name="register_skills",
            tool_kind="permission_change",
            outcome="completed",
            resources=f"app={app_name} skills={registered}",
        )
    else:
        sel().log_tool_invocation(
            session_key="",
            agent="kirocrew",
            source="app_bridge",
            tool_name="register_skills",
            tool_kind="permission_change",
            outcome="no_op",
            resources=f"app={app_name} skills=[]",
        )
    return registered


def _deregister_skills(app_name: str) -> int:
    """Remove the app's skill symlinks from ~/.kiro/crew/skills/.

    Removes **only what registration created** — the symlinks, and the directory
    itself once it holds nothing else. It must NOT ``rmtree`` unconditionally: when a
    PACKAGED builtin skill shares the app's name, ``skills/<app_name>/`` is that
    skill's real directory, not an app-owned link farm. Blowing it away deleted a
    shipped skill and every SOP under it, leaving the app's cron prompts pointing at
    files that no longer existed — silently, since a missing skill file is not an
    error anywhere. Hit for real by ops-mission-control, whose skill ships under
    ``builtin_skills/`` because a builtin app's own directory is never copied into
    the data home.
    """
    skills_root = _skills_dir()
    app_skills_dir = skills_root / app_name
    if not app_skills_dir.exists():
        return 0
    try:
        removed_skills = [
            item.name
            for item in app_skills_dir.iterdir()
            if platform_compat.is_link_or_junction(item)
        ]
        for item in app_skills_dir.iterdir():
            # Symlink on POSIX, directory junction on non-admin Windows.
            if platform_compat.is_link_or_junction(item):
                if item.name in _RESERVED_SKILL_DIRS:
                    continue
                target = item.resolve()
                flat_link = skills_root / item.name
                # is_link_or_junction: a junction (non-admin Windows) is not a
                # symlink, and unlink_link_or_junction removes the link, never
                # the target it points at.
                if (
                    platform_compat.is_link_or_junction(flat_link)
                    and flat_link.resolve() == target
                ):
                    platform_compat.unlink_link_or_junction(flat_link)
                platform_compat.unlink_link_or_junction(item)
        # Only prune the directory if registration is all that was ever in it.
        # Any surviving real file means this path belongs to something else.
        if any(app_skills_dir.iterdir()):
            logger.info(
                "Deregistered %d app skill link(s) for %s; keeping %s "
                "(contains files this app does not own)",
                len(removed_skills),
                app_name,
                app_skills_dir,
            )
            return 1 if removed_skills else 0
        app_skills_dir.rmdir()
        logger.info("Deregistered skills for app %s", app_name)
        sel().log_tool_invocation(
            session_key="",
            agent="kirocrew",
            source="app_bridge",
            tool_name="deregister_skills",
            tool_kind="permission_change",
            outcome="completed",
            resources=f"app={app_name} skills={removed_skills}",
        )
        return 1
    except OSError:
        sel().log_tool_invocation(
            session_key="",
            agent="kirocrew",
            source="app_bridge",
            tool_name="deregister_skills",
            tool_kind="permission_change",
            outcome="failed",
            resources=f"app={app_name}",
        )
        return 0


# ---------------------------------------------------------------------------
# Skill reconcile (startup — ensures manifest-declared skills are linked)
# ---------------------------------------------------------------------------


def reconcile_app_skills(app_name: str) -> list[str]:
    """Reconcile skill symlinks for an enabled app at gateway startup.

    Ensures manifest-declared skills are registered (idempotent: existing
    correct symlinks are overwritten by _register_skills, missing ones are
    created).  Also removes stale symlinks for skills that were removed from
    the manifest since the last registration.

    Called from start_enabled_app_backends() so that an app upgraded in-place
    (new manifest declaring new skills) gets its symlinks without needing a
    disable/enable cycle.

    Returns list of currently-registered namespaced skill names.
    """
    info = get_app(app_name)
    if info and info.get("resources") == "app":
        # Self-managed apps own their registration lifecycle -- never touch
        # their symlinks here, even when the manifest declares no skills
        # (dynamically managed skills are not manifest-declared).
        return []

    manifest, app_root = _registration_source(app_name)
    if _registration_denied(
        app_name,
        action="skill_reconcile",
        app_root=app_root,
    ):
        # A policy tightened after a prior registration must revoke stale links,
        # not merely decline to create new ones.
        _deregister_skills(app_name)
        return []

    if not manifest or not manifest.skills:
        # No skills declared — remove any stale symlinks left from a prior version
        _deregister_skills(app_name)
        return []

    # _register_skills is already idempotent (overwrites existing symlinks)
    registered = _register_skills(app_name, manifest, app_root)

    # Clean stale links: skills present as symlinks but no longer in manifest
    skills_root = _skills_dir()
    app_skills_dir = skills_root / app_name
    if app_skills_dir.is_dir():
        manifest_skill_names = {Path(s).name for s in manifest.skills}
        for entry in list(app_skills_dir.iterdir()):
            # is_link_or_junction: a junction (non-admin Windows) is not a symlink.
            if platform_compat.is_link_or_junction(entry) and (
                entry.name not in manifest_skill_names
            ):
                # Stale link — skill was removed from manifest
                target = entry.resolve()
                platform_compat.unlink_link_or_junction(entry)
                # Also remove the flat link if it points to the same target
                flat_link = skills_root / entry.name
                if platform_compat.is_link_or_junction(flat_link):
                    try:
                        if flat_link.resolve() == target:
                            platform_compat.unlink_link_or_junction(flat_link)
                    except OSError:
                        pass
                logger.info(
                    "Reconcile: removed stale skill link %s/%s for app %s",
                    app_name,
                    entry.name,
                    app_name,
                )

    return registered


# ---------------------------------------------------------------------------
# Cron registration (deferred — writes a manifest for the CronService)
# ---------------------------------------------------------------------------

_CRON_MANIFEST_NAME = "app-crons.json"


def _app_crons_path(app_name: str) -> Path:
    """Path to the app's cron manifest within its install directory."""
    return app_dir(app_name) / _CRON_MANIFEST_NAME


def _cron_defs_from_manifest(
    app_name: str,
    manifest: AppManifest,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build scheduler definitions from an authoritative app manifest."""
    cron_defs: list[dict[str, Any]] = []
    registered: list[str] = []
    for cron in manifest.crons:
        namespaced = _namespace(app_name, cron.name)
        cron_defs.append(
            {
                "name": namespaced,
                "every": cron.every,
                "cron_expr": cron.cron_expr,
                "agent": cron.agent,
                "message": cron.message,
                "command": cron.command,
                "script": cron.script,
                "app": app_name,
                "agent_sequence": cron.agent_sequence,
                "env": cron.env,
                "persistent_session": cron.persistent_session,
                "silent": cron.silent,
                "enabled": cron.enabled,
            }
        )
        registered.append(namespaced)
    return cron_defs, registered


def _register_crons(app_name: str, manifest: AppManifest) -> list[str]:
    """Write app cron definitions to a manifest file for later CronService pickup.

    The actual CronService registration happens at enable time via
    ``register_app_crons_with_service()``. This just persists the definitions
    so they survive restarts.

    Returns list of namespaced cron names.
    """
    cron_defs, registered = _cron_defs_from_manifest(app_name, manifest)
    if not cron_defs:
        return []

    path = _app_crons_path(app_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cron_defs, indent=2), encoding="utf-8")
    logger.info("Wrote %d cron definition(s) for app %s", len(cron_defs), app_name)
    return registered


def _deregister_crons(app_name: str) -> int:
    """Remove the app's cron manifest."""
    path = _app_crons_path(app_name)
    if path.is_file():
        path.unlink()
        logger.info("Removed cron manifest for app %s", app_name)
        return 1
    return 0


def load_app_cron_defs(app_name: str) -> list[dict[str, Any]]:
    """Load persisted cron definitions for an app (used by CronService bridge)."""
    path = _app_crons_path(app_name)
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


async def register_app_crons_with_service(app_name: str, cron_service: Any) -> list[str]:
    """Promote admitted app cron definitions into the running CronService.

    Third-party definitions come from the installed ``app-crons.json`` only
    after the central execution decision admits them. Shipped builtin jobs are
    rebuilt from their immutable package manifest so a mutable derivative cannot
    forge a trusted command.

    ASYNC: awaits ``CronSDK.add_job_async``, which offloads each store-lock spin
    to a worker thread — so this can be awaited directly on the gateway event
    loop (no ``asyncio.to_thread`` wrapper, no caller-side timer re-arm) without
    parking the loop. Timer arming is owned by CronService.

    Idempotent — jobs already present (by name) are skipped.
    """
    if cron_service is None:
        return []

    manifest, app_root = _registration_source(app_name)
    if _registration_denied(
        app_name,
        action="cron_register",
        app_root=app_root,
    ):
        return []

    if shipped_builtin_app_root(app_name) is not None:
        if manifest is None:
            return []
        defs, _ = _cron_defs_from_manifest(app_name, manifest)
    else:
        defs = load_app_cron_defs(app_name)
    if not defs:
        return []

    sdk = CronSDK(app_name, cron_service)
    existing_names = {j.name for j in sdk.list_jobs()}

    # circular import: mcp_cron → security → ... → hooks_integration → bridges
    from kiro_crew.mcp_cron import _vet_script_file, _vet_shell_command

    newly_registered: list[str] = []
    for d in defs:
        name = d.get("name", "")
        if not name or name in existing_names:
            continue
        command = d.get("command") or ""
        script = d.get("script") or ""
        # Security vetting: same checks as the MCP cron_add path
        if command:
            err = _vet_shell_command(command)
            if err:
                logger.warning("App %s: cron %r command rejected: %s", app_name, name, err)
                sel().log_api_access(
                    caller="app_bridge",
                    operation="app_cron_command_vetted",
                    outcome="denied",
                    resources=f"app={app_name} cron={name}",
                    error=err,
                )
                continue
            sel().log_api_access(
                caller="app_bridge",
                operation="app_cron_command_vetted",
                outcome="allowed",
                resources=f"app={app_name} cron={name}",
            )
        if script:
            try:
                file_path, _ = resolve_script_path(script)
                err = _vet_script_file(file_path)
                if err:
                    logger.warning("App %s: cron %r script rejected: %s", app_name, name, err)
                    sel().log_api_access(
                        caller="app_bridge",
                        operation="app_cron_script_vetted",
                        outcome="denied",
                        resources=f"app={app_name} cron={name}",
                        error=err,
                    )
                    continue
                sel().log_api_access(
                    caller="app_bridge",
                    operation="app_cron_script_vetted",
                    outcome="allowed",
                    resources=f"app={app_name} cron={name}",
                )
            except (PermissionError, FileNotFoundError, ValueError) as exc:
                logger.warning("App %s: cron %r script path rejected: %s", app_name, name, exc)
                sel().log_api_access(
                    caller="app_bridge",
                    operation="app_cron_script_vetted",
                    outcome="denied",
                    resources=f"app={app_name} cron={name}",
                    error=str(exc),
                )
                continue
        try:
            await sdk.add_job_async(
                name=name,
                message=d.get("message", ""),
                every_secs=d.get("every"),  # JSON "every" → Python "every_secs"
                cron_expr=d.get("cron_expr"),
                agent=d.get("agent") or "",
                command=command,
                script=script,
                agent_sequence=d.get("agent_sequence") or None,
                env=d.get("env") or None,
                persistent_session=d.get("persistent_session", False),
                silent=bool(d.get("silent", False)),
                enabled=bool(d.get("enabled", True)),
            )
            newly_registered.append(name)
            sel().log_api_access(
                caller="app_bridge",
                operation="app_cron_add_job",
                outcome="allowed",
                resources=f"app={app_name} cron={name}",
            )
        except Exception as exc:
            logger.warning(
                "App %s: failed to register cron %r (%s): %s",
                app_name,
                name,
                type(exc).__name__,
                exc,
            )
            sel().log_api_access(
                caller="app_bridge",
                operation="app_cron_add_job",
                outcome="failed",
                resources=f"app={app_name} cron={name}",
                error=str(exc),
            )

    if newly_registered:
        logger.info(
            "App %s: registered %d cron job(s) with scheduler: %s",
            app_name,
            len(newly_registered),
            ", ".join(newly_registered),
        )
    return newly_registered


async def deregister_app_crons_from_service(app_name: str, cron_service: Any) -> int:
    """Remove app-owned cron jobs from the running CronService.

    Mirrors :func:`register_app_crons_with_service`. Uses :class:`CronSDK`,
    which only removes jobs tagged ``created_by="app:{app_name}"`` — other
    apps' jobs are unaffected.

    ASYNC: awaits ``CronSDK.remove_all_async``, which removes all owned jobs in
    ONE atomic ``CronService.remove_jobs_by_owner`` transaction (owned set
    selected against the in-lock reloaded on-disk state, store-lock spin
    offloaded to a worker thread) — all-or-nothing, never a partial removal that
    orphans still-ENABLED app jobs, and never a cache-only snapshot that could
    miss a cross-process creation. Awaitable directly on the gateway loop.

    Idempotent — safe to call when no jobs are registered (returns ``0``).
    Returns the number of jobs removed.

    Propagates :class:`CronStoreBusy` (re-raised) so a contended cleanup is
    REPORTED to the disable/uninstall caller as a failure rather than masked as
    a successful ``0`` while owned jobs stay enabled and keep executing.
    """
    if cron_service is None:
        return 0
    sdk = CronSDK(app_name, cron_service)
    try:
        return await sdk.remove_all_async()
    except CronStoreBusy as exc:
        logger.warning(
            "App %s: cron cleanup could not complete — store busy: %s",
            app_name,
            exc,
        )
        sel().log_api_access(
            caller="app_bridge",
            operation="app_crons_deregister",
            outcome="failed",
            resources=app_name,
            error=str(exc),
        )
        raise
    except Exception as exc:
        logger.warning(
            "App %s: failed to remove crons from scheduler (%s): %s",
            app_name,
            type(exc).__name__,
            exc,
        )
        sel().log_api_access(
            caller="app_bridge",
            operation="app_crons_deregister",
            outcome="failed",
            resources=app_name,
            error=str(exc),
        )
        return 0


async def disarm_app_crons_for_execution(
    app_name: str,
    cron_service: Any,
) -> int:
    """Atomically remove app-owned jobs with best-effort audit logging."""
    if cron_service is None:
        return 0
    removed = await cron_service.remove_jobs_by_owner(f"app:{app_name}")
    try:
        sel().log_api_access(
            caller="app_bridge",
            operation="app_crons_execution_disarm",
            outcome="completed",
            resources=f"app={app_name} removed={len(removed)}",
        )
    except Exception:  # noqa: BLE001 - cleanup verdict must survive audit failure
        logger.debug("app cron execution-disarm audit failed", exc_info=True)
    return len(removed)


async def reconcile_app_crons_for_execution(cron_service: Any) -> list[str]:
    """Disarm persisted app jobs before the gateway starts cron timers.

    ``CronService.create`` has loaded the durable store but has not armed its
    timer yet when this runs. Any cleanup failure propagates so the caller can
    leave the scheduler stopped rather than execute a denied job.
    """
    if cron_service is None:
        return []

    app_infos = list_apps()
    installed_names = {app_name for app_info in app_infos if (app_name := app_info.get("name", ""))}
    cron_owner_names: set[str] = set()
    for job in cron_service.list_jobs(include_disabled=True):
        owner = str(getattr(job, "created_by", ""))
        if owner.startswith("app:") and owner != "app:":
            cron_owner_names.add(owner.removeprefix("app:"))

    disarmed: list[str] = []
    for app_name in sorted(cron_owner_names - installed_names):
        reason = "orphaned app cron owner has no installed app"
        logger.warning(
            "App %s: denying persisted cron restore (cron_boot_restore): %s",
            app_name,
            reason,
        )
        try:
            sel().log_api_access(
                caller="app_bridge",
                operation="app_execution_admission",
                outcome="denied",
                resources=(f"app={app_name} action=cron_boot_restore " "provenance=unverified"),
                error=reason,
            )
        except Exception:  # noqa: BLE001 - denial must survive audit unavailability
            logger.debug("app execution denial audit failed", exc_info=True)

        removed = await disarm_app_crons_for_execution(app_name, cron_service)
        if removed:
            disarmed.append(app_name)
            logger.warning(
                "Boot: disarmed %d persisted cron(s) for orphaned app %s",
                removed,
                app_name,
            )

    for app_info in app_infos:
        app_name = app_info.get("name", "")
        if not app_name:
            continue

        should_disarm = not bool(app_info.get("enabled"))
        if not should_disarm:
            _, app_root = _registration_source(app_name)
            should_disarm = bool(
                _registration_denied(
                    app_name,
                    action="cron_boot_restore",
                    app_root=app_root,
                )
            )
        if not should_disarm:
            continue

        removed = await disarm_app_crons_for_execution(app_name, cron_service)
        if removed:
            disarmed.append(app_name)
            logger.warning(
                "Boot: disarmed %d persisted cron(s) for inactive app %s",
                removed,
                app_name,
            )
    return disarmed


# ---------------------------------------------------------------------------
# MCP server registration
# ---------------------------------------------------------------------------


# App MCP servers are written to KiroCrew's OWN agent config, not to the shared
# ~/.kiro/settings/mcp.json. That shared file is read by everything else living
# under ~/.kiro — Kiro IDE and any other kiro-cli agent — so registering an app's
# tools there leaks them into surfaces that never installed the app (and a dead
# HTTP entry there breaks EVERY kiro session, see backend.py's warning). KiroCrew
# sessions read only the agent config (``includeMcpJson`` pinned False in
# agent.py), so this is both sufficient and correctly scoped.
def _mcp_json_path() -> Path:
    """KiroCrew's own agent config. A function, not an import-time constant:
    the path must track the live data home, and freezing it at import would
    write to the real ``~/.kiro`` from an isolated run. Goes through
    :func:`_kiro_agents_dir` so the module-level override honoured by every
    other agent-dir caller applies here too."""
    return _kiro_agents_dir() / "kirocrew.json"


# The pre-fix location. Still scrubbed on deregister so an upgrade removes
# entries an older build leaked into the shared file.
_LEGACY_SHARED_MCP_PATH = Path.home() / ".kiro" / "settings" / "mcp.json"


@contextmanager
def _mcp_lock(*, exclusive: bool = True, target: Optional[Path] = None) -> Iterator[None]:
    """Acquire a lock on an mcp/config file for the duration of the block.

    Uses a single ``.lock`` sidecar file for both shared and exclusive
    locks so that readers and writers coordinate properly. ``target`` selects
    WHICH file's sidecar to lock (default: KiroCrew's own agent config); pass the
    legacy shared ``mcp.json`` so its read-modify-write serializes against any
    other writer of THAT file, which sits under a different sidecar.
    """
    base = target if target is not None else _mcp_json_path()
    lock_path = base.with_suffix(".lock")
    base.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    # "r+" (not "r"): Windows msvcrt.locking requires write access on the fd —
    # a read-only handle fails with EACCES and platform_compat.file_lock
    # swallows it (best-effort), silently degrading this to a no-op and letting
    # concurrent writers race the atomic mcp.json rename.
    with open(lock_path, "r+") as lf:
        with platform_compat.file_lock(lf.fileno(), exclusive=exclusive):
            yield


def _read_mcp_json_unlocked(*, strict: bool = False) -> dict[str, Any]:
    """Read mcp.json without acquiring a lock (caller must hold lock).

    ``strict`` is for read-MODIFY-write callers: an existing-but-unreadable
    config must NOT be treated as empty and overwritten — that drops the agent's
    entire configuration (tools, allowedTools, prompt, every other server). With
    ``strict=True`` a parse/OS error on a PRESENT file propagates so the writer
    aborts without persisting; a genuinely MISSING file is still an empty map.
    Read-only callers keep the lenient default (degrade to ``{}``).
    """
    if not _mcp_json_path().is_file():
        return {}
    try:
        return json.loads(_mcp_json_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        if strict:
            raise
        logger.warning("Failed to read mcp.json: %s", exc)
        return {}


def _write_mcp_json_unlocked(data: dict[str, Any]) -> None:
    """Write mcp.json without acquiring a lock (caller must hold lock)."""
    _mcp_json_path().parent.mkdir(parents=True, exist_ok=True)
    atomic_write(_mcp_json_path(), json.dumps(data, indent=2) + "\n")


def _read_mcp_json() -> dict[str, Any]:
    """Read mcp.json with a shared lock."""
    with _mcp_lock(exclusive=False):
        return _read_mcp_json_unlocked()


def _resolve_live_mcp_url(app_name: str, url: str, live_port: int | None = None) -> str:
    """Rewrite a manifest HTTP MCP url's port to the backend's ACTUALLY-allocated port.

    Gateway-managed backends declare ``backend.port:"auto"`` and get a free port at
    spawn time (``backend.py:_find_free_port`` — 9100 if free, else 9101, …). The
    manifest's ``mcpServers.<name>.url`` carries an illustrative fixed port (e.g.
    ``http://localhost:9100/mcp``). Registering that verbatim is a latent bug: whenever
    the backend lands on a different port, the registered MCP server points at the wrong
    one and every agent tool call to the app silently fails. Here we substitute the live
    port (preserving scheme/host/path) so the registration always matches the running
    backend. Non-HTTP transports and apps with no resolvable port are passed through
    unchanged.

    ``live_port`` may be passed explicitly by a caller that knows the just-allocated
    port (the boot/enable path, where the backend isn't marked *healthy* yet so the
    tracked-port lookup would still return None). When omitted we fall back to the
    health-gated ``get_app_backend_port``.
    """
    if not url or not url.startswith("http"):
        return url
    try:
        if live_port is None:
            # circular import: backend.py imports from bridges (reregister_app_mcp_servers
            # in its boot path), so bridges can't import backend at module load — defer it.
            from kiro_crew.apps.backend import get_app_backend_port

            live_port = get_app_backend_port(app_name)
        if not live_port:
            return url  # backend not running yet — keep the manifest default
        p = urlparse(url)
        if p.port == live_port:
            return url  # already correct
        host = p.hostname or "127.0.0.1"
        netloc = f"{host}:{live_port}"
        return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))
    except Exception:  # noqa: BLE001 — registration must never crash on URL rewrite
        return url


def _live_port_for(app_name: str, live_port: int | None) -> int | None:
    """The backend's actually-allocated port, or None if it isn't running yet.

    ``live_port`` (passed by the boot/enable path that just spawned the backend) wins;
    otherwise fall back to the health-gated tracked-port lookup. Never raises — a failure
    to resolve is treated as "not live" so registration can fail safe."""
    if live_port:
        return live_port
    try:
        # circular import: backend.py imports from bridges in its boot path — defer.
        from kiro_crew.apps.backend import get_app_backend_port

        return get_app_backend_port(app_name)
    except Exception:  # noqa: BLE001 — registration must never crash on a port lookup
        return None


#: Bare python launchers an app manifest may name. Each is substituted with the RUNNING
#: interpreter, which is the only one guaranteed to import ``kiro_crew``.
_BARE_PYTHON = frozenset({"python", "python3", "py"})


def resolve_stdio_command(cfg: dict) -> dict:
    """Resolve a bare ``python``/``python3`` MCP command to the running interpreter.

    A manifest that hard-codes ``python3`` does not launch on a native Windows install (a venv
    there ships ``python.exe`` and no ``python3.exe``), so the spawn fails with ENOENT and the
    app's MCP tools go silently missing. Plain ``python`` is not a fix either: on Windows
    ``shutil.which("python")`` can resolve a 0-byte Microsoft-Store reparse point — the case
    ``platform_compat._is_windows_store_python_stub`` exists for — and kiro-cli strips env when
    spawning MCP subprocesses, so a PATH lookup can land on an interpreter that cannot import
    ``kiro_crew`` at all. ``mcp_gateway/rewriter.py`` bakes ``sys.executable`` into its stub
    entry for exactly that reason; this is the same decision for app manifests.

    Only a BARE launcher is substituted. An absolute path, ``node``, ``docker`` or anything
    else was chosen deliberately and is left untouched, as is an HTTP entry (no ``command``).
    """
    command = cfg.get("command")
    if isinstance(command, str) and command.strip().lower() in _BARE_PYTHON:
        cfg["command"] = sys.executable
    return cfg


def _register_mcp_servers(
    app_name: str, manifest: AppManifest, live_port: int | None = None
) -> list[str]:
    """Register app-provided MCP servers into KiroCrew's agent config.

    Uses ``{app_name}:{server_name}`` namespace to avoid collisions. HTTP MCP urls have
    their port rewritten to the backend's live allocated port (see
    :func:`_resolve_live_mcp_url`) so a ``backend.port:"auto"`` app whose backend landed
    on a non-default port is still reachable by agents. ``live_port`` lets the boot/enable
    path pass the just-allocated port directly (health not yet confirmed).

    FAIL-SAFE for ``backend.port:"auto"`` HTTP servers (regression fix):
    a manifest's ``mcpServers.<name>.url`` carries an ILLUSTRATIVE
    fixed port (e.g. ``:9100``). If we wrote that verbatim while the backend is NOT
    running (app disabled / down / registered before the port is known), the entry is a
    reachable-LOOKING but dead URL. kiro-cli connects to every server in the agent config on each request; a connect failure surfaces as a "transient
    HTTP 5xx / backend hiccup", gets retried 3× by the transient-retry path, then shown as
    a hard error — breaking ALL kiro requests, not just this app's. (An alternate ACP
    backend reads a different config file, so it was unaffected — the asymmetry in the
    report.)
    So: an HTTP server with NO resolvable LIVE port is NOT written at all (and any stale
    entry for it is scrubbed) — never a dead URL the kiro binary might still dial whether
    or not it honours a ``disabled`` flag. The boot/enable path calls
    :func:`reregister_app_mcp_servers` with the real ``live_port`` once the backend is up,
    which writes the entry with the correct, reachable port. stdio/command servers (no
    ``url``) are always registered — they have no port to be dead.
    """
    if not manifest.mcpServers:
        return []
    resolved_port = _live_port_for(app_name, live_port)
    registered: list[str] = []
    skipped: list[str] = []
    with _mcp_lock():
        mcp_data = _read_mcp_json_unlocked(strict=True)
        servers = mcp_data.setdefault("mcpServers", {})
        for server_name, server_config in manifest.mcpServers.items():
            namespaced = f"{app_name}:{server_name}"
            cfg = dict(server_config) if isinstance(server_config, dict) else server_config
            if isinstance(cfg, dict):
                cfg = _pin_host_cli_command(app_name, cfg)
            is_http = isinstance(cfg, dict) and bool(cfg.get("url"))
            if is_http and not resolved_port:
                # No live backend → registering the manifest's dead default-port URL would
                # break every kiro session. Skip it AND scrub any stale entry so a prior
                # (now-dead) registration can't keep poisoning the provider path.
                servers.pop(namespaced, None)
                skipped.append(namespaced)
                continue
            if is_http:
                cfg["url"] = _resolve_live_mcp_url(app_name, cfg["url"], live_port=resolved_port)
                cfg.pop("disabled", None)  # backend is live — ensure enabled
            else:
                # A stdio entry: resolve a bare `python`/`python3` to the running
                # interpreter (see `resolve_stdio_command`).
                cfg = resolve_stdio_command(cfg) if isinstance(cfg, dict) else cfg
            servers[namespaced] = cfg
            registered.append(namespaced)
        # LAST governance pass before this map hits disk. This file IS read by
        # kiro-cli, and an `autoApprove` on an entry here auto-approves that
        # server locally with NO permission request — so a manifest that ships
        # `autoApprove` on a governed server would bypass the PreToolUse gate and
        # the ceiling's denial, the same second route the agent-config writers
        # already close. Strip a governed grant here too; the tools stay, they
        # just go through the gate. Idempotent and a no-op on an ungoverned host.
        mcp_data["mcpServers"] = dict(strip_ungoverned_auto_approve(servers))
        _write_mcp_json_unlocked(mcp_data)
    logger.info(
        "Registered %d MCP server(s) for app %s (live_port=%s); skipped %d HTTP server(s) "
        "with no live backend: %s",
        len(registered),
        app_name,
        resolved_port,
        len(skipped),
        skipped or "none",
    )
    return registered


def registered_app_mcp_servers() -> dict[str, Any]:
    """Return the app MCP servers as CURRENTLY registered on disk.

    This is the live map :func:`_register_mcp_servers` writes: an auto-port app's
    HTTP ``url`` already rewritten to the backend's live allocated port, and any
    HTTP server with no live backend already skipped (never a dead-port URL). The
    agent rebuild reads THIS in preference to the manifest so that a resolved
    auto-port survives the rebuild — copying the manifest's illustrative port back
    over a live one is exactly the regression this exists to prevent. Best-effort:
    a missing or unreadable file yields an empty map, and the caller falls back to
    the manifest for stdio servers (and skips live-portless HTTP ones).
    """
    try:
        with _mcp_lock():
            data = _read_mcp_json_unlocked()
    except Exception:  # noqa: BLE001 — never let a rebuild fail on this read
        return {}
    servers = data.get("mcpServers", {})
    return dict(servers) if isinstance(servers, dict) else {}


def reregister_app_mcp_servers(app_name: str, live_port: int | None = None) -> list[str]:
    """Re-register admitted MCP servers after an app backend has started.

    HTTP URLs are rewritten to the live allocated port. Shipped definitions are
    sourced from their immutable package manifest; denied apps have any stale
    global entries scrubbed instead of being made reachable.
    """
    manifest, app_root = _registration_source(app_name)
    if _registration_denied(
        app_name,
        action="mcp_register",
        app_root=app_root,
    ):
        _deregister_mcp_servers(app_name)
        return []
    if not manifest or not manifest.mcpServers:
        return []
    registered = _register_mcp_servers(app_name, manifest, live_port=live_port)
    # Refresh the app's AGENTS after the live server lands. register_app runs
    # _register_agents AFTER _register_mcp_servers precisely because agents COPY
    # the registered spec into their own config; this live-port path (health
    # confirmed, backend just came up) wrote only the global map, so without this
    # the app's agent kept the pre-live spec (or none) and could not reach its own
    # declared MCP tools until some unrelated rebuild happened to run.
    if registered:
        try:
            _register_agents(app_name, manifest, app_root)
        except Exception:  # noqa: BLE001 — a refresh failure must not fail health re-registration
            logger.warning(
                "Could not refresh agents for app %s after live MCP registration", app_name
            )
    return registered


def _scrub_legacy_shared_mcp(app_name: str) -> int:
    """Remove an app's entries from the pre-fix shared ~/.kiro/settings/mcp.json.

    Older builds registered app MCP servers into the shared file, where they were
    visible to Kiro IDE and every other kiro-cli agent. Upgrading doesn't retroact
    on what is already on disk, so deregister scrubs both locations — otherwise a
    stale entry keeps leaking (and, for an HTTP server whose backend is gone,
    keeps pointing at a dead port). Best-effort: never raises.
    """
    prefix = f"{app_name}:"
    try:
        if not _LEGACY_SHARED_MCP_PATH.is_file():
            return 0
        # Hold the shared file's own lock across read+remove+write: this file can
        # be written concurrently (Kiro IDE, another kiro-cli agent, another
        # KiroCrew process), and an unlocked stale read-modify-write here would
        # clobber a server another writer added between our read and our atomic
        # rename. It is a DIFFERENT sidecar than _mcp_lock's default (that guards
        # kirocrew.json), so pass the legacy path explicitly.
        with _mcp_lock(target=_LEGACY_SHARED_MCP_PATH):
            data = json.loads(_LEGACY_SHARED_MCP_PATH.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", {})
            stale = [k for k in servers if k.startswith(prefix)]
            if not stale:
                return 0
            for k in stale:
                del servers[k]
            atomic_write(_LEGACY_SHARED_MCP_PATH, json.dumps(data, indent=2) + "\n")
        logger.info("Scrubbed %d legacy shared MCP entry(ies) for app %s", len(stale), app_name)
        return len(stale)
    except Exception as exc:  # noqa: BLE001 — cleanup must never block deregistration
        logger.warning("Failed to scrub legacy shared MCP entries for %s: %s", app_name, exc)
        return 0


def _deregister_mcp_servers(app_name: str) -> int:
    """Remove an app's MCP servers from the agent config (and the legacy shared file)."""
    prefix = f"{app_name}:"
    with _mcp_lock():
        mcp_data = _read_mcp_json_unlocked(strict=True)
        servers = mcp_data.get("mcpServers", {})
        to_remove = [k for k in servers if k.startswith(prefix)]
        for k in to_remove:
            del servers[k]
        if to_remove:
            _write_mcp_json_unlocked(mcp_data)
    # NOT scrubbed here: the legacy shared ~/.kiro/settings/mcp.json is held by
    # OTHER processes (Kiro IDE, other kiro-cli agents), so its cross-process
    # flock can block indefinitely. deregister_app() runs synchronously on the
    # gateway event loop (dashboard disable/update/uninstall), and a stall here
    # would freeze all chat and heartbeat tasks. The scrub is idempotent and is
    # performed at boot by reconcile_enabled_app_resources(), which the gateway
    # already runs off-loop via run_in_executor — so the migration still lands,
    # just not on this hot path.
    if to_remove:
        logger.info("Deregistered %d MCP server(s) for app %s", len(to_remove), app_name)
    return len(to_remove)


# ---------------------------------------------------------------------------
# Top-level register / deregister
# ---------------------------------------------------------------------------


@dataclass
class RegistrationResult:
    """Summary of what was registered/deregistered for an app."""

    agents: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    crons: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agents": self.agents,
            "skills": self.skills,
            "crons": self.crons,
            "mcp_servers": self.mcp_servers,
            "errors": self.errors,
        }


def _prune_stale_app_resources(app_name: str, manifest: AppManifest, app_root: Path) -> None:
    """Remove app-owned agents/MCP servers a manifest UPGRADE dropped.

    ``register_app`` OVERWRITES the resources the CURRENT manifest declares, but
    it never deletes ones a new app version removed — so a dropped agent or MCP
    server would stay registered on disk and callable after an upgrade. Prune
    exactly the app-namespaced resources whose names are absent from the current
    manifest, leaving still-declared ones in place (a SELECTIVE prune, not a
    deregister-then-readd that would transiently break a live app). Idempotent: a
    fresh install has nothing to prune.
    """
    # Agents: the current link names the manifest still declares (same derivation
    # as _register_agents: the JSON's `name`, falling back to the file stem).
    current_links: set[str] = set()
    for agent_path_str in manifest.agents or []:
        agent_path = app_root / agent_path_str
        try:
            if not agent_path.resolve().is_relative_to(app_root.resolve()):
                continue
            data = json.loads(agent_path.read_text(encoding="utf-8"))
            agent_name = data.get("name", agent_path.stem)
        except (json.JSONDecodeError, OSError) as exc:
            # A declared agent we CANNOT read is not the same as a removed one. If
            # we skipped it, its name would be absent from `current_links` and the
            # prune below would delete its last-good materialized config — over a
            # transient IO error. Abort the AGENT prune entirely (the caller then
            # re-registers current agents idempotently); the MCP prune below still
            # runs off the manifest, which we CAN read.
            logger.warning(
                "Skipping agent prune for %s: declared agent %s is unreadable (%s)",
                app_name,
                agent_path_str,
                exc,
            )
            current_links = None  # type: ignore[assignment]  # sentinel: do not prune agents
            break
        current_links.add(_safe_link_name(_namespace(app_name, agent_name)) + ".json")
    agents_dir = _kiro_agents_dir()
    if current_links is not None and agents_dir.is_dir():
        prefix = _safe_link_name(app_name + "/")
        for entry in agents_dir.iterdir():
            if (
                entry.name.startswith(prefix)
                and entry.name.endswith(".json")
                and entry.name not in current_links
            ):
                try:
                    entry.unlink()
                    logger.info("Pruned stale app agent %s (absent from manifest)", entry.name)
                except OSError:
                    pass

    # MCP servers: keep only servers the current manifest still declares.
    current_servers = {f"{app_name}:{srv}" for srv in (manifest.mcpServers or {})}
    with _mcp_lock():
        data = _read_mcp_json_unlocked(strict=True)
        servers = data.get("mcpServers", {})
        if isinstance(servers, dict):
            stale = [
                k for k in servers if k.startswith(f"{app_name}:") and k not in current_servers
            ]
            for k in stale:
                del servers[k]
            if stale:
                _write_mcp_json_unlocked(data)
                logger.info("Pruned %d stale app MCP server(s) for %s", len(stale), app_name)


def register_app(app_name: str) -> RegistrationResult:
    """Register all executable resources for an admitted installed app.

    Third-party resources come from the installed app snapshot. Shipped builtin
    resources come from the immutable manifest root that proves their provenance.

    Apps with ``resources="app"`` manage their own resource registration
    (agents, skills, MCP servers via SDK). Bridge registration is skipped
    entirely to avoid creating duplicates that confuse kiro-cli.
    """
    result = RegistrationResult()
    if not get_app_manifest(app_name):
        result.errors.append(f"app {app_name!r} not found or has invalid manifest")
        return result

    # Self-managed apps handle their own registration — skip all bridge work.
    info = get_app(app_name)
    if info and info.get("resources") == "app":
        logger.debug(
            "Skipping bridge registration for %s (resources=app)",
            app_name,
        )
        return result

    manifest, app_root = _registration_source(app_name)
    if manifest is None:
        result.errors.append(f"app {app_name!r} has no authoritative resource manifest")
        return result

    denied = _registration_denied(
        app_name,
        action="resource_register",
        app_root=app_root,
    )
    if denied:
        # Re-registration can happen after an operator tightens policy. Scrub
        # derivative links/config from a prior admitted run as well as skipping
        # every new side effect. Running cron jobs are removed by the scheduler
        # reconciliation boundary, which owns the CronService instance.
        cleanup = deregister_app(app_name)
        result.errors.extend(cleanup.errors)
        result.errors.append(f"registration blocked by execution policy: {denied}")
        return result

    # NOTE: pruning of resources a manifest UPGRADE removed is NOT done here.
    # register_app is called on the event loop by the enable/update handlers,
    # and a prune (directory walk over every agent file + lock acquisition)
    # scales with agent count and would stall chat/heartbeat. Those on-loop
    # callers all deregister_app() first, so nothing stale survives for them to
    # prune. The one path that re-registers WITHOUT a preceding deregister — the
    # boot reconcile — does the prune itself, off the loop, in
    # reconcile_enabled_app_resources(). See that function.

    # MCP servers BEFORE agents: _register_agents copies the app's own registered
    # server specs into each agent config, so registering them afterwards leaves
    # every agent holding the previous spec (or none on a first install) until
    # something else happens to rewrite it. The ordering is the whole fix — both
    # steps individually looked correct.
    try:
        result.mcp_servers = _register_mcp_servers(app_name, manifest)
    except Exception as exc:
        result.errors.append(f"MCP server registration failed: {exc}")

    try:
        result.agents = _register_agents(app_name, manifest, app_root)
    except Exception as exc:
        result.errors.append(f"agent registration failed: {exc}")

    try:
        result.skills = _register_skills(app_name, manifest, app_root)
    except Exception as exc:
        result.errors.append(f"skill registration failed: {exc}")

    try:
        result.crons = _register_crons(app_name, manifest)
    except Exception as exc:
        result.errors.append(f"cron registration failed: {exc}")

    logger.info(
        "Registered app %s: %d agents, %d skills, %d crons, %d mcp, %d errors",
        app_name,
        len(result.agents),
        len(result.skills),
        len(result.crons),
        len(result.mcp_servers),
        len(result.errors),
    )
    return result


def refresh_app_agents(app_name: str) -> list[str]:
    """Re-materialize just this app's agent configs.

    Called when something the agent config is derived from changes (today: the
    app's MCP reach policy) so the change takes effect without a gateway
    restart. Cheap and idempotent — it rewrites the same files registration
    would.
    """
    manifest = get_app_manifest(app_name)
    if not manifest or not manifest.agents:
        return []
    info = get_app(app_name)
    if info and info.get("resources") == "app":
        return []
    return _register_agents(app_name, manifest, _app_resource_root(app_name))


def reconcile_enabled_app_resources() -> dict[str, int]:
    """Re-register resources for every ENABLED gateway-managed app.

    Called once at gateway startup.  Registration used to happen ONLY in the
    enable path, so an app that gained agents/skills in a later version never
    registered them for a user who had already enabled it — silently, because a
    missing resource only logs a warning.  Reconciling at boot makes the on-disk
    state a function of the current manifests instead of of install history.

    Idempotent: agent configs are rewritten from their template, skills/crons/MCP
    registration already overwrite in place.  Apps with ``resources="app"`` are
    skipped by :func:`register_app` itself.
    """
    counts = {"apps": 0, "agents": 0, "skills": 0, "errors": 0}
    try:
        apps = list_apps()
    except Exception as exc:  # noqa: BLE001 — never block startup
        logger.warning("Resource reconcile skipped: cannot list apps: %s", exc)
        return counts

    for info in apps:
        if not info.get("enabled"):
            continue
        name = info.get("name") or ""
        if not name:
            continue
        # Prune stale resources a manifest UPGRADE removed, BEFORE re-registering.
        # This runs ONLY here, on the off-loop boot reconcile: register_app no
        # longer prunes (it is also called on the event loop, where the directory
        # walk + lock would stall chat/heartbeat), and this is the one path that
        # re-registers without a preceding deregister_app(), so a dropped agent or
        # MCP server would otherwise stay registered and callable.
        try:
            _man, _root = _registration_source(name)
            if _man is not None:
                _prune_stale_app_resources(name, _man, _root)
        except Exception as exc:  # noqa: BLE001 — a prune failure must not block reconcile
            logger.warning("Could not prune stale resources for %s: %s", name, exc)
        try:
            result = register_app(name)
        except Exception as exc:  # noqa: BLE001 — one bad app must not stop the rest
            logger.warning("Resource reconcile failed for %s: %s", name, exc)
            counts["errors"] += 1
            continue
        counts["apps"] += 1
        counts["agents"] += len(result.agents)
        counts["skills"] += len(result.skills)
        counts["errors"] += len(result.errors)
        # Finish the shared-file migration for apps that are ALREADY enabled.
        # Registration now targets the agent config, but an older build wrote
        # this app's servers into the shared ``~/.kiro/settings/mcp.json``, and
        # re-registering does not remove what is already there. Scrubbing only
        # on deregister meant the leak this change exists to fix (one app's
        # private tools visible to Kiro IDE and every other kiro-cli agent, plus
        # dead-port entries that break unrelated sessions) survived until the
        # user happened to disable the app. Best-effort and idempotent.
        scrubbed = _scrub_legacy_shared_mcp(name)
        if scrubbed:
            logger.info(
                "Removed %d stale %s entr%s from the shared kiro MCP config",
                scrubbed,
                name,
                "y" if scrubbed == 1 else "ies",
            )
        for err in result.errors:
            logger.warning("Resource reconcile issue for %s: %s", name, err)

    if counts["apps"]:
        logger.info(
            "Reconciled resources for %d enabled app(s): %d agent(s), %d skill(s), %d error(s)",
            counts["apps"],
            counts["agents"],
            counts["skills"],
            counts["errors"],
        )
    return counts


def deregister_app(app_name: str) -> RegistrationResult:
    """Deregister all resources for an app.

    Removes symlinks and cron manifests.  Does not remove the app directory.
    """
    result = RegistrationResult()

    try:
        n = _deregister_agents(app_name)
        result.agents = [f"removed {n} agent(s)"]
    except Exception as exc:
        result.errors.append(f"agent deregistration failed: {exc}")

    try:
        _deregister_skills(app_name)
        result.skills = ["removed"]
    except Exception as exc:
        result.errors.append(f"skill deregistration failed: {exc}")

    try:
        _deregister_crons(app_name)
        result.crons = ["removed"]
    except Exception as exc:
        result.errors.append(f"cron deregistration failed: {exc}")

    try:
        n = _deregister_mcp_servers(app_name)
        result.mcp_servers = [f"removed {n} MCP server(s)"]
    except Exception as exc:
        result.errors.append(f"MCP server deregistration failed: {exc}")

    logger.info("Deregistered app %s", app_name)
    return result
