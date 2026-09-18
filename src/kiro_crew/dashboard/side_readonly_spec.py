"""The derived read-only agent spec a side turn runs under.

A side turn streams under ``ToolApprovalPolicy.READ_ONLY``: every permission
request that reaches the host gate is either proven read-only and auto-approved,
or rejected. That gate only sees the requests kiro-cli raises. A tool on the
agent's ``allowedTools`` (and an MCP server's ``autoApprove`` list, a
``toolsSettings`` ``allowed*`` / ``trusted*`` grant, a KAS ``permissions`` rule,
the servers pulled in by ``includeMcpJson``) is approved by the backend itself
and never raises one — so with the parent agent's own spec the side surface
would run the user's main-chat grants unattended, cron removals included
(``config/defaults.json`` pre-authorizes ``@kirocrew-cron/cron_remove_all`` and
``@kirocrew-core`` wholesale).

This module closes that gap at the spec: the side session is bound to
``<agent>--readonly``, a copy of the active agent's spec with every backend-side
grant emptied and the lifecycle ``hooks`` removed (shell commands the backend
runs with no permission request). Everything else — ``tools``, ``mcpServers``,
``resources``, the prompt, the model — is kept, so reads still work: they simply
raise a permission request now, which the READ_ONLY gate judges.

Where the file lives, and why: kiro-cli discovers its selectable agents ONLY from
``~/.kiro/agents/*.json`` (user scope) and ``$PWD/.kiro/agents/*.json`` (the
session's project), at process start (``acp/runtime.py``, ``agent.py``
``ensure_agent_materialized``). There is no private directory it can be pointed
at, and the project scope is the user's checkout — a tracked directory Kiro Crew
must not write into. So the derived spec is published into the user-level kiro
agent registry, ``~/.kiro/agents/<agent>--readonly.json``, the same directory
Kiro Crew already owns its managed specs in (``agent_files.py``) and apps
publish ``<app>--<agent>.json`` into (``apps/bridges._register_agents``). It is a
runtime resource: deterministic name, regenerated from the base spec on every
side turn, written atomically and only when its content changed, never
hand-edited (an edit is overwritten on the next turn).

The derived name must resolve to OUR file and nothing else, because kiro-cli
loads whichever spec declares the name — the project scope first. So publication
refuses (rather than overwrites or defers to) any other spec that claims the
name: a project-scope file declaring it, a second user-scope file declaring it,
or a file already at the derived path that does not carry this module's owner
marker in its ``description``.

Fail closed: a side turn that cannot derive or publish its spec is refused
(:class:`ReadOnlySpecError`, with a stable ``code``) — it never runs under the
base agent.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.validation import _AGENT_NAME_RE

logger = logging.getLogger(__name__)

#: Suffix of the derived spec's name and filename stem. Two dashes, the same
#: separator apps use for ``<app>--<agent>`` (``apps/bridges._namespace``), so a
#: base agent whose name ends in ``-readonly`` is still distinguishable.
READONLY_SUFFIX = "--readonly"

#: Prefix of the derived spec's ``description``: the OWNER MARKER. A file at the
#: derived path whose description does not start with this is somebody else's
#: agent and is never overwritten. ``description`` is a first-class kiro-cli spec
#: field, so the marker survives ``deny_unknown_fields`` where a custom key would
#: not.
OWNER_MARKER = "Kiro Crew derived read-only spec"

#: Keys under ``toolsSettings.<tool>`` that pre-authorize without a prompt:
#: ``allowedCommands`` / ``allowedPaths`` / ``allowedServices`` …, the
#: ``subagent`` tool's ``trustedAgents`` (a trusted sub-agent's tool calls run
#: without a card), and ``shell.autoAllowReadonly`` (kiro-cli's own read-only
#: auto-approve, whose notion of read-only is not the host's). Matched by prefix
#: on purpose: a new ``allowed*`` / ``trusted*`` / ``auto*`` grant kiro-cli adds
#: must fail closed here, while ``denied*`` and ``available*`` keys — which only
#: narrow or list — are kept.
_TOOL_SETTING_GRANT_PREFIXES = ("allowed", "trusted", "auto")


class ReadOnlySpecError(RuntimeError):
    """The derived read-only spec could not be produced; the side turn must not run.

    ``code`` is stable and logged, so a refusal can be told apart from an agent
    that failed to load or a signed-out CLI:

    * ``unsafe_name`` — the base or derived name fails the agent-name grammar.
    * ``base_spec_missing`` — no spec declares the base agent in either scope.
    * ``base_spec_unreadable`` — the base spec exists but did not parse.
    * ``derived_name_shadowed`` — another spec (project scope, or a second
      user-scope file) declares the derived name, so kiro-cli would load it
      instead of the generated one.
    * ``derived_path_foreign`` — a file at the derived path is not this
      module's (no owner marker); it is left alone and the turn is refused.
    * ``spec_write_failed`` — the derived file could not be written.
    """

    def __init__(self, code: str, detail: str):
        super().__init__(f"{detail} [{code}]")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class PublishedSpec:
    """What :func:`publish_readonly_spec` produced.

    ``digest`` identifies the derived CONTENT (sha256 of its canonical JSON), so
    a caller holding a live session can tell whether the spec that session was
    spawned under is still the one on disk — kiro-cli reads the file at spawn,
    so a changed base needs a cold start to take effect.
    """

    name: str
    digest: str


def readonly_agent_name(base_name: str, source_id: str | None = None) -> str:
    """The derived agent name for *base_name* (no validation; see :func:`derive_readonly_spec`).

    ``<agent>--readonly`` for a user-scope base. For a PROJECT-scope base,
    ``<agent>--readonly-<8 hex>`` where the suffix digests *source_id* (the
    project's real path): two checkouts may each declare a project agent called
    ``helper``, and both publish into the one user-level registry, so the name
    itself has to say which source it was derived from — otherwise the two
    would contend for one file and a session could spawn on the other's spec.
    """
    if source_id is None:
        return f"{base_name}{READONLY_SUFFIX}"
    tag = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:8]
    return f"{base_name}{READONLY_SUFFIX}-{tag}"


def spec_digest(spec: dict[str, Any]) -> str:
    """sha256 of the canonical JSON of *spec* (sorted keys, no whitespace)."""
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def derive_readonly_spec(
    base_spec: dict[str, Any], *, base_name: str, source_id: str | None = None
) -> dict[str, Any]:
    """A deep copy of *base_spec* with every backend-side grant emptied.

    Pure: no I/O, no logging. The derived spec declares its own ``name``
    (``<base>--readonly``) so no two files in the registry declare the base
    agent's name — ``agent.agent_spec_path`` refuses that ambiguity, and it is
    what ``reset_agent_model`` resolves the base agent by — and its
    ``description`` starts with :data:`OWNER_MARKER`, which is how publication
    tells its own file from a foreign one.

    What is emptied, and why each is a grant:

    * ``allowedTools`` → ``[]`` — kiro-cli approves these without a permission
      request; the exemption path the host gate never sees.
    * ``mcpServers.<name>.autoApprove`` → removed — the second route to the same
      exemption, per server (``platform.governance.strip_ungoverned_auto_approve``).
    * ``toolsSettings.<tool>.allowed*`` / ``trusted*`` / ``auto*`` → removed —
      per-tool pre-authorization (``allowedCommands`` runs a matching shell
      command unprompted; ``subagent.trustedAgents`` lets a sub-agent's tool
      calls run without a card; ``shell.autoAllowReadonly`` self-approves
      reads); ``denied*`` and ``available*`` keys stay.
    * ``includeMcpJson`` → ``False`` — the global ``mcp.json`` carries its own
      ``autoApprove`` lists, which this derivation cannot see or empty. Under
      READ_ONLY an MCP-served tool is never provably read-only anyway, so
      nothing the side chat can use is lost.
    * ``autoAllowReadonly`` → ``False`` when present — kiro-cli's own (retired)
      read-only auto-approve, whose notion of read-only is not the host's.
    * ``permissions`` → ``{"rules": []}`` when the base carries one — the KAS
      spelling of ``allowedTools``; the key's presence is what makes KAS load the
      file, so it is emptied, not dropped.
    * ``hooks`` → removed — kiro-cli lifecycle hooks (``agentSpawn``,
      ``userPromptSubmit``, ``preToolUse``, ``postToolUse``, ``stop``) are shell
      commands the backend runs with no permission request, several of them fed
      model-controlled input, and the read-only surface executes nothing but
      proven reads. The host's SEL audit already records every side-turn tool
      decision, so the shipped bash audit hook is not needed here. Kiro Crew's
      own ``hooks.auto_approve_tools`` grant list goes with it.

    ``tools``, ``mcpServers`` (minus ``autoApprove``), ``resources``, ``prompt``,
    ``model`` and every other key are untouched: mounting a tool is not
    approving it, and the reads the side chat exists for go through the gate.
    """
    if not _AGENT_NAME_RE.match(base_name or ""):
        raise ReadOnlySpecError(
            "unsafe_name", f"agent name {base_name!r} is not a valid agent name"
        )
    derived_name = readonly_agent_name(base_name, source_id)
    if not _AGENT_NAME_RE.match(derived_name):
        raise ReadOnlySpecError(
            "unsafe_name", f"derived name {derived_name!r} exceeds the agent-name grammar"
        )
    spec = copy.deepcopy(base_spec)
    spec["name"] = derived_name
    base_description = spec.get("description")
    spec["description"] = (
        f"{OWNER_MARKER} of {base_name!r}; generated on every side turn, do not edit."
        + (
            f" Base: {base_description}"
            if isinstance(base_description, str) and base_description
            else ""
        )
    )
    spec["allowedTools"] = []
    servers = spec.get("mcpServers")
    if isinstance(servers, dict):
        for server in servers.values():
            if isinstance(server, dict):
                server.pop("autoApprove", None)
    settings = spec.get("toolsSettings")
    if isinstance(settings, dict):
        for tool_settings in settings.values():
            if isinstance(tool_settings, dict):
                for key in [
                    k for k in tool_settings if str(k).startswith(_TOOL_SETTING_GRANT_PREFIXES)
                ]:
                    tool_settings.pop(key, None)
    spec["includeMcpJson"] = False
    if "autoAllowReadonly" in spec:
        spec["autoAllowReadonly"] = False
    if "permissions" in spec:
        spec["permissions"] = {"rules": []}
    spec.pop("hooks", None)
    return spec


def is_owned_readonly_spec(data: Any) -> bool:
    """True when *data* is a spec this module wrote (owner marker present)."""
    if not isinstance(data, dict):
        return False
    description = data.get("description")
    return isinstance(description, str) and description.startswith(OWNER_MARKER)


def _read_base_spec(base_name: str, project_dir: str | None) -> tuple[dict[str, Any], str | None]:
    """The spec kiro-cli would load for *base_name*, project scope first, and its source.

    The second element is ``None`` for a user-scope spec and the project's real
    path for a project-scope one — the source identity the derived NAME carries
    (see :func:`readonly_agent_name`).

    Mirrors kiro-cli's own order: ``$PWD/.kiro/agents`` (the session's project,
    which is also the cwd the side session is spawned in) before the user-level
    registry. Both reads go through the hardened, size-capped reader every other
    agent-spec consumer uses. Function-local imports keep this module off the
    gateway boot path (``kiro_crew.agent`` is heavy and imports the ACP stack).
    """
    from kiro_crew.agent import agent_spec_path
    from kiro_crew.agent_discovery import _read_agent_spec, project_agent_files, project_agent_name

    if project_dir:
        for spec_file in project_agent_files(project_dir):
            if project_agent_name(spec_file) == base_name:
                data = _read_agent_spec(
                    spec_file, operation="side_readonly_spec", source="dashboard"
                )
                if data is None:
                    raise ReadOnlySpecError(
                        "base_spec_unreadable", f"project agent spec {spec_file} did not parse"
                    )
                return data, str(Path(project_dir).resolve())
    try:
        user_path = agent_spec_path(base_name)
    except ValueError as exc:  # two safe specs declare the same name
        raise ReadOnlySpecError("base_spec_unreadable", str(exc)) from exc
    if user_path is None:
        raise ReadOnlySpecError(
            "base_spec_missing", f"no agent spec declares {base_name!r} in either scope"
        )
    data = _read_agent_spec(user_path, operation="side_readonly_spec", source="dashboard")
    if data is None:
        raise ReadOnlySpecError("base_spec_unreadable", f"agent spec {user_path} did not parse")
    return data, None


def _refuse_if_shadowed(derived_name: str, target: Path, project_dir: str | None) -> None:
    """Refuse when any spec other than *target* would answer to *derived_name*.

    kiro-cli resolves ``--agent`` by declared ``name`` (or filename stem) across
    the project scope FIRST and then the user scope, so a project checkout that
    carries ``.kiro/agents/<agent>--readonly.json`` — or a second user-scope file
    declaring the name — would be loaded in place of the generated spec, with
    whatever grants it carries. Neither is ours to rewrite; the turn is refused.
    """
    from kiro_crew.agent import agent_spec_path
    from kiro_crew.agent_discovery import project_agent_files, project_agent_name

    if project_dir:
        for spec_file in project_agent_files(project_dir):
            if spec_file.stem == derived_name or project_agent_name(spec_file) == derived_name:
                raise ReadOnlySpecError(
                    "derived_name_shadowed",
                    f"project spec {spec_file} declares {derived_name!r}; kiro-cli would load it "
                    "in place of the generated read-only spec",
                )
    try:
        claimant = agent_spec_path(derived_name)
    except ValueError as exc:
        raise ReadOnlySpecError("derived_name_shadowed", str(exc)) from exc
    if claimant is not None and claimant.resolve() != target.resolve():
        raise ReadOnlySpecError(
            "derived_name_shadowed",
            f"{claimant} declares {derived_name!r}; kiro-cli would load it in place of {target}",
        )


def publish_readonly_spec(base_name: str, project_dir: str | None = None) -> PublishedSpec:
    """Derive ``<base_name>--readonly`` from the live base spec and publish it.

    Returns the derived agent NAME the side session must be created with and the
    content digest. Reads the base spec (project
    scope first), derives, refuses a shadowed name or a foreign file at the
    target, and writes ``~/.kiro/agents/<derived>.json`` atomically — only when
    the file is absent or its content differs, so an unchanged base costs reads
    and no write. Refreshes the materialized-agent snapshot after a write so the
    name is known to ``resolve_agent_bindings`` on the same process.

    Synchronous and I/O-bound: call it off the event loop
    (``asyncio.to_thread``). Raises :class:`ReadOnlySpecError` on every failure;
    never returns a name whose file is not on disk.
    """
    from kiro_crew.agent import _atomic_json_write, kiro_agents_dir_path

    base_spec, source_id = _read_base_spec(base_name, project_dir)
    derived = derive_readonly_spec(base_spec, base_name=base_name, source_id=source_id)
    derived_name = derived["name"]
    agents_dir = kiro_agents_dir_path()
    target = agents_dir / f"{derived_name}.json"
    digest = spec_digest(derived)
    try:
        agents_dir.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            # We only ever write regular files, so a link here is not ours.
            raise ReadOnlySpecError(
                "derived_path_foreign", f"{target} is a symlink; refusing to write through it"
            )
        current = _read_current(target)
        if target.exists() and not is_owned_readonly_spec(current):
            raise ReadOnlySpecError(
                "derived_path_foreign",
                f"{target} exists and does not carry the {OWNER_MARKER!r} owner marker; "
                "it is not overwritten",
            )
        _refuse_if_shadowed(derived_name, target, project_dir)
        if current != derived:
            _atomic_json_write(target, derived)
            logger.info("Side turn: published read-only agent spec %s", target)
            _refresh_materialized_snapshot()
    except OSError as exc:
        raise ReadOnlySpecError("spec_write_failed", f"could not write {target}: {exc}") from exc
    return PublishedSpec(name=derived_name, digest=digest)


def _read_current(target: Path) -> dict[str, Any] | None:
    """The derived file's current content, or ``None`` when absent/unparseable."""
    try:
        with target.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _refresh_materialized_snapshot() -> None:
    """Best-effort: let the in-process agent snapshot see the new name."""
    try:
        from kiro_crew.config.loader import refresh_materialized_agents

        refresh_materialized_agents()
    except Exception:  # noqa: BLE001 — the spec is on disk; the snapshot is a cache
        logger.debug("materialized-agent snapshot refresh failed", exc_info=True)
