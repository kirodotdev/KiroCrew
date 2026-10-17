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
import os
import re
import stat
import threading
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.validation import is_registered_agent_name

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

#: Serializes a publication against the reaper's delete of the same registry.
#: Publication holds it from reading the base spec to writing the derived file;
#: the reaper holds it while it re-checks the base and unlinks. So a base that
#: comes back while a reap is deciding is either seen by that re-check, or the
#: publication runs after the unlink and writes the file again.
_SPEC_FILE_LOCK = threading.Lock()


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
    * ``base_spec_malformed`` — the base spec carries ``mcpServers`` or
      ``toolsSettings`` (or an entry under either) that is neither an object
      nor ``null``, so its grants cannot be found to empty.
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


#: The two shapes :func:`readonly_agent_name` produces: ``<base>--readonly`` and
#: ``<base>--readonly-<8 hex>``. Always applied with ``fullmatch``.
_DERIVED_NAME_RE = re.compile(r"(?P<base>.+)--readonly(?:-[0-9a-f]{8})?")


def readonly_base_name(name: str) -> str | None:
    """The base agent *name* was derived from, or ``None`` when it has neither derived shape.

    The inverse of :func:`readonly_agent_name` on the NAME alone: it reads no
    file, so it cannot tell a generated spec from a user's own agent that
    happens to be called ``<x>--readonly``. Use it only to WORD a message; a
    decision about what a spec is goes through :func:`is_owned_readonly_spec`,
    which reads the owner marker.
    """
    match = _DERIVED_NAME_RE.fullmatch(name) if isinstance(name, str) else None
    return match.group("base") if match else None


def unavailable_mode_explanation(agent: str) -> tuple[str, str]:
    """``(cause, remedy)`` for kiro-cli not advertising *agent* as a mode.

    Completes the "Agent mode ... is not available" refusal the ACP layer raises,
    which puts the cause before its "Refusing to run the backend default mode"
    sentence and the remedy after it. For an ordinary agent the file is likely
    missing and ``kirocrew setup --agent-only`` writes the managed ones. For a
    spec THIS module published both halves of that are wrong: setup never writes
    it (a side turn does, see :func:`publish_readonly_spec`), and it is usually
    PRESENT -- kiro-cli lists its agents once, at process start, so a process
    started before the side turn published it cannot select it, and the remedy
    says a new session fixes that. The spec is not a sub-agent either, which is
    the other way a caller reaches this message, so the remedy also names the
    base agent -- or says to omit ``agent``, since the host default is reached
    that way rather than by name.

    Which answer applies is decided by the owner marker on the file at the
    derived path, not by the name: a user's own agent that is merely called
    ``<x>--readonly`` keeps the ordinary answer. Blocking I/O (one bounded read
    through the hardened spec reader): the ACP callers run it off the loop.
    """
    base = readonly_base_name(agent)
    if base is None or not _is_published_readonly_spec(agent):
        return (
            f"its ~/.kiro/agents/{agent}.json is likely missing.",
            "Run `kirocrew setup --agent-only` to materialize the agent config.",
        )
    return (
        f"{agent!r} is the read-only spec Kiro Crew derives from {base!r} for side "
        "replies: a side turn writes it, `kirocrew setup` does not, and kiro-cli lists "
        "only the agents present when its process started.",
        "A new session starts a kiro-cli that lists it. It is not a sub-agent: to spawn "
        f"its source agent, name {base!r}, or omit 'agent' when that is the default agent.",
    )


def _is_published_readonly_spec(name: str) -> bool:
    """Whether ``~/.kiro/agents/<name>.json`` is a regular file carrying the owner marker.

    ``False`` on any read failure: the caller then gives the ordinary answer,
    which is what it gave before this module existed.
    """
    # Function-local for two reasons. ``agent_discovery`` imports this module at
    # module scope (``is_internal_agent_spec`` reads the owner marker), so a
    # module-scope import back would be circular. ``kiro_crew.agent`` is heavy,
    # and this runs only on an already-failing mode refusal.
    from kiro_crew.agent import kiro_agents_dir_path
    from kiro_crew.agent_discovery import _read_agent_spec

    target = kiro_agents_dir_path() / f"{name}.json"
    try:
        if os.path.islink(target) or not os.path.isfile(target):
            return False
        data = _read_agent_spec(target, operation="agent_mode_refusal", source="unknown")
    except Exception:  # noqa: BLE001 -- wording only; the refusal is raised regardless
        logger.debug("could not read %s to word a mode refusal", target, exc_info=True)
        return False
    return is_owned_readonly_spec(data)


def spec_digest(spec: dict[str, Any]) -> str:
    """sha256 of the canonical JSON of *spec* (sorted keys, no whitespace)."""
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _grant_container(spec: dict[str, Any], key: str) -> dict[str, Any]:
    """``spec[key]`` as a mapping to scrub; absent or ``null`` carries no grant."""
    value = spec.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ReadOnlySpecError(
            "base_spec_malformed", f"{key} is a {type(value).__name__}, not an object"
        )
    return value


def _grant_entry(value: Any, key: str) -> dict[str, Any]:
    """One entry under *key*; ``null`` carries no grant, any other non-object is refused."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ReadOnlySpecError(
            "base_spec_malformed",
            f"an entry under {key} is a {type(value).__name__}, not an object",
        )
    return value


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
      nothing the side chat can use is lost. ``useLegacyMcpJson``, kiro-cli's
      alias for the same field, is removed so the file does not declare it
      twice.
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

    A ``mcpServers`` or ``toolsSettings`` value, or an entry under either, that
    is neither an object nor ``null`` raises ``base_spec_malformed``: its grants
    cannot be located, so the turn is refused rather than run with them.
    """
    if not is_registered_agent_name(base_name):
        raise ReadOnlySpecError(
            "unsafe_name", f"agent name {base_name!r} is not a valid agent name"
        )
    derived_name = readonly_agent_name(base_name, source_id)
    if not is_registered_agent_name(derived_name):
        raise ReadOnlySpecError(
            "unsafe_name", f"derived name {derived_name!r} exceeds the agent-name grammar"
        )
    spec = copy.deepcopy(base_spec)
    spec["name"] = derived_name
    base_description = spec.get("description")
    # A project-scope variant records the checkout it was derived from: its name
    # carries only a digest of that path, and the reaper needs the path itself to
    # tell a removed checkout from a live one (see reap_orphaned_readonly_specs).
    # JSON-quoted so any character a path can hold round-trips.
    source = f" in project {json.dumps(source_id)}" if source_id is not None else ""
    spec["description"] = (
        f"{OWNER_MARKER} of {base_name!r}{source}; generated on every side turn, do not edit."
        + (
            f" Base: {base_description}"
            if isinstance(base_description, str) and base_description
            else ""
        )
    )
    spec["allowedTools"] = []
    for server in _grant_container(spec, "mcpServers").values():
        _grant_entry(server, "mcpServers").pop("autoApprove", None)
    for tool_settings in _grant_container(spec, "toolsSettings").values():
        entry = _grant_entry(tool_settings, "toolsSettings")
        for key in [k for k in entry if str(k).startswith(_TOOL_SETTING_GRANT_PREFIXES)]:
            entry.pop(key, None)
    spec.pop("useLegacyMcpJson", None)
    spec["includeMcpJson"] = False
    if "autoAllowReadonly" in spec:
        spec["autoAllowReadonly"] = False
    if "permissions" in spec:
        # An empty policy is still a policy, and the KEY's presence is what a
        # kiro-cli below ``SPEC_PERMISSIONS_MIN_VERSION`` -- or one whose
        # version cannot be established -- refuses the whole file for
        # (``deny_unknown_fields``). This file is published into the registry
        # kiro-cli loads agents from, so a refusal here costs the side turn the
        # very isolation this module exists to give it. Same gate, and same
        # removal of an inherited value, as the generated writers in
        # ``agent.py``; on an accepting release it derives ``{"rules": []}``
        # from the emptied grants above, which is what this line wrote. The
        # guard stays: a base carrying no block never gains one.
        from kiro_crew.agent import _write_derived_permissions

        _write_derived_permissions(spec, spec["allowedTools"], derived_name)
    spec.pop("hooks", None)
    return spec


def is_owned_readonly_spec(data: Any) -> bool:
    """True when *data* is a spec this module wrote (owner marker present)."""
    if not isinstance(data, dict):
        return False
    return is_readonly_spec_description(data.get("description"))


def is_readonly_spec_description(description: object) -> bool:
    """True when *description* carries this module's owner marker.

    The test :func:`is_owned_readonly_spec` applies to a raw spec, exposed on the
    field alone so a caller holding a discovery row (``AgentInfo.description``)
    rather than the parsed file asks the same question.
    """
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
        for spec_file in project_agent_files(
            project_dir, operation="side_readonly_spec", source="dashboard"
        ):
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
        for spec_file in project_agent_files(
            project_dir, operation="side_readonly_spec", source="dashboard"
        ):
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
    never returns a name whose file is not on disk. Holds
    :data:`_SPEC_FILE_LOCK` from the base read to the write, so the reaper
    cannot delete the file between this resolving its base and returning.
    """
    from kiro_crew.agent import _atomic_json_write, kiro_agents_dir_path

    with _SPEC_FILE_LOCK:
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
            raise ReadOnlySpecError(
                "spec_write_failed", f"could not write {target}: {exc}"
            ) from exc
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


#: The head :func:`derive_readonly_spec` writes into ``description``: the owner
#: marker, the base name and, for a project-scope variant, the checkout as a JSON
#: string. Matched at the start only; the base's own description follows the
#: ``; generated`` clause and is never parsed.
_DESCRIPTION_HEAD_RE = re.compile(
    re.escape(OWNER_MARKER)
    + r" of '(?P<base>[^']+)'(?: in project (?P<source>\"(?:[^\"\\]|\\.)*\"))?; generated "
)


@dataclass(frozen=True)
class _ReapCandidate:
    """One owned derived file, what it was derived from, and its ``lstat`` when read."""

    path: Path
    name: str
    base: str
    source: str | None
    seen: os.stat_result


def reap_orphaned_readonly_specs(in_use: Collection[str] = ()) -> list[str]:
    """Remove the derived read-only specs whose base agent does not resolve.

    Publication writes ``<agent>--readonly.json`` (or a project variant) on
    every side turn and nothing else removes one, while kiro-cli lists every
    file in the registry as a selectable agent: a deleted base agent or a
    removed checkout would otherwise leave a stale read-only agent behind for
    good. A file is removed only when ALL of these hold:

    * it is a regular ``.json`` file directly in the registry. ``lstat``
      decides, so a symlink, a directory or any other inode is never a
      candidate and is never followed; the hardened spec reader then refuses a
      hardlinked file.
    * its declared ``name`` equals its filename stem and is the derived name of
      the base its ``description`` names, and that ``description`` starts with
      the owner marker. A user's own agent called ``<x>--readonly`` carries no
      marker and is never touched.
    * no live session runs under it: *in_use* holds the agent names the session
      registry's providers were spawned with.
    * its base does not resolve where the publisher looks for it. For a
      user-scope ``<agent>--readonly``: no ``<agent>.json`` / ``<agent>.md``
      entry of any kind (a symlink included) exists, and
      :func:`kiro_crew.agent.agent_spec_path` finds no spec declaring the name
      (a name two specs declare still resolves). For a project variant: the
      checkout its ``description`` records is gone, or that checkout's
      ``.kiro/agents`` holds no entry named or declaring the agent. A variant
      whose checkout cannot be read, is a protected tree, is not recorded at
      all, or is recorded as a relative path is kept, as is one whose checkout
      or agents directory ``hooks.validate_file_path`` refuses (an untrusted
      UNC share, or a link into one, is never followed), and one whose agents
      directory holds a spec-named link (never followed either).

    The base is judged again under :data:`_SPEC_FILE_LOCK`, with an ``lstat``
    identity check, immediately before the unlink: a publication racing the
    reap either lands first and keeps its file, or lands after the unlink and
    writes it again. Nothing removed could have served a turn, because a side
    turn whose base does not resolve is refused (``base_spec_missing``) before a
    session exists, and a base that comes back regenerates its file on the next
    side turn.

    Blocking: one listing of the registry, one bounded read per name of a
    derived shape, and a declared-name scan only for a base with no same-named
    file. Run it off the event loop. Never raises; returns the names removed. A
    removal clears the agent-listing caches, which moves the spec generation
    every agents-dir memo revision carries, and refreshes the materialized-agent
    snapshot.
    """
    from kiro_crew.agent import kiro_agents_dir_path

    agents_dir = kiro_agents_dir_path()
    try:
        with os.scandir(agents_dir) as it:
            file_names = [entry.name for entry in it]
    except FileNotFoundError:
        return []
    except OSError:
        logger.debug("read-only spec reap: could not list %s", agents_dir, exc_info=True)
        return []
    live = {name for name in in_use if isinstance(name, str)}
    removed: list[str] = []
    for file_name in file_names:
        try:
            candidate = _owned_candidate(agents_dir, file_name)
            if candidate is None or candidate.name in live:
                continue
            if _orphan_reason(candidate, agents_dir) is None:
                continue
            with _SPEC_FILE_LOCK:
                reason = _orphan_reason(candidate, agents_dir)
                if reason is None or not _unlink_if_unchanged(candidate):
                    continue
        except Exception:  # noqa: BLE001 -- one file's failure keeps that file, not the sweep
            logger.debug("read-only spec reap: skipped %r", file_name, exc_info=True)
            continue
        removed.append(candidate.name)
        # ``%r``: the registry is shared with other tools, so a file name is untrusted.
        logger.info("Side panel: removed read-only agent spec %r (%s)", file_name, reason)
    if removed:
        from kiro_crew.agent_discovery import clear_list_agents_cache

        clear_list_agents_cache()
        _refresh_materialized_snapshot()
    return removed


def _owned_candidate(agents_dir: Path, file_name: str) -> _ReapCandidate | None:
    """The derived spec at *file_name*, or ``None`` unless it is provably this module's."""
    if not file_name.endswith(".json"):
        return None
    name = file_name[: -len(".json")]
    if readonly_base_name(name) is None:
        return None
    path = agents_dir / file_name
    try:
        seen = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(seen.st_mode):
        return None
    from kiro_crew.agent_discovery import _read_agent_spec

    # ``unknown``: the reap is the gateway's own maintenance, not any channel's request.
    data = _read_agent_spec(path, operation="side_readonly_reap", source="unknown")
    if not isinstance(data, dict) or not is_owned_readonly_spec(data) or data.get("name") != name:
        return None
    head = _DESCRIPTION_HEAD_RE.match(str(data["description"]))
    if head is None:
        return None
    base = head.group("base")
    source: str | None = None
    if head.group("source") is not None:
        try:
            source = json.loads(head.group("source"))
        except ValueError:
            return None
        if not isinstance(source, str) or not source:
            return None
    if not is_registered_agent_name(base) or readonly_agent_name(base, source) != name:
        return None
    return _ReapCandidate(path=path, name=name, base=base, source=source, seen=seen)


def _orphan_reason(candidate: _ReapCandidate, agents_dir: Path) -> str | None:
    """Why *candidate*'s base does not resolve, or ``None`` while it may."""
    if candidate.source is None:
        if _user_base_resolves(candidate.base, agents_dir):
            return None
        return "base agent removed"
    return _project_orphan_reason(candidate.base, candidate.source)


def _user_base_resolves(base: str, agents_dir: Path) -> bool:
    """Whether a user-scope spec could still answer to *base*.

    A same-named entry of any kind counts before anything is parsed: the
    publisher's resolver skips a symlinked or unreadable file, but kiro-cli
    may not, and a kept file costs nothing.
    """
    from kiro_crew.agent import agent_spec_path
    from kiro_crew.agent_spec_format import agent_spec_candidates

    if any(os.path.lexists(path) for path in agent_spec_candidates(agents_dir, base)):
        return True
    try:
        return agent_spec_path(base, agents_dir=agents_dir) is not None
    except ValueError:
        return True


def _project_orphan_reason(base: str, source: str) -> str | None:
    """Why checkout *source* does not declare *base*, or ``None`` while it may.

    Mirrors where :func:`_read_base_spec` looks (the checkout's
    ``.kiro/agents``) and widens it: an entry whose stem is the agent counts
    without being parsed, case-folded for a case-insensitive filesystem, and a
    spec-named entry that is not a regular file (a link, a junction) counts
    without being followed. Every failure other than "not there" keeps the file.
    """
    from kiro_crew import hooks
    from kiro_crew.agent_discovery import _project_scope_denied, project_agent_name
    from kiro_crew.agent_spec_format import is_agent_spec_name, spec_stem
    from kiro_crew.config.paths import project_agents_dir

    # The recorded checkout is text read from a file in a shared directory.
    # Publication records only an absolute resolved path, so a relative one is
    # not ours. The checkout and then its agents directory each pass
    # ``hooks.validate_file_path`` before anything follows them: it refuses an
    # untrusted UNC spelling and reads a Windows link's own target before the
    # link is traversed, because on Windows following either is an SMB
    # connection to the host it names. Only the canonical paths it returns are
    # probed; a refusal keeps the file.
    if not os.path.isabs(source):
        return None
    checkout = hooks.validate_file_path(source)
    if checkout is None:
        return None
    if _project_scope_denied(checkout, operation="side_readonly_reap", source="unknown"):
        return None
    try:
        info = os.stat(checkout)
    except (FileNotFoundError, NotADirectoryError):
        return "project removed"
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return "project removed"
    screened = hooks.validate_file_path(str(project_agents_dir(checkout)))
    if screened is None:
        return None
    agents = Path(screened)
    try:
        with os.scandir(agents) as it:
            entries = [(entry.name, entry.is_file(follow_symlinks=False)) for entry in it]
    except (FileNotFoundError, NotADirectoryError):
        return "project does not declare the agent"
    except OSError:
        return None
    folded = base.casefold()
    for entry_name, regular in entries:
        if not is_agent_spec_name(entry_name):
            continue
        if spec_stem(entry_name).casefold() == folded or not regular:
            return None
        if project_agent_name(agents / entry_name) == base:
            return None
    return "project does not declare the agent"


def _unlink_if_unchanged(candidate: _ReapCandidate) -> bool:
    """Unlink *candidate* only if its path still holds the regular file that was judged."""
    try:
        now = os.lstat(candidate.path)
    except OSError:
        return False
    seen = candidate.seen
    if not stat.S_ISREG(now.st_mode) or (
        now.st_dev,
        now.st_ino,
        now.st_size,
        now.st_mtime_ns,
    ) != (seen.st_dev, seen.st_ino, seen.st_size, seen.st_mtime_ns):
        return False
    try:
        os.unlink(candidate.path)
    except OSError:
        logger.debug("read-only spec reap: could not remove %r", candidate.path.name, exc_info=True)
        return False
    return True
