"""Startup migration: move every EXISTING crewmate onto its worker and its own file.

RFC *mate-owned-agent-config* §6 "Migrating existing crewmates". A crewmate
created before this release has ``kiro_agent`` pointing at the agent it runs
on: a shared ``~/.kiro/agents/`` template, or its private ``crew-<hex>`` copy
when the Capabilities page forked one for it. Once the crewmate owns a file its
chat runs on the source-defined conductor and its work on the source-defined
worker (:mod:`kiro_crew.mate_worker`), so whatever that agent carried beyond the
worker's source base has to move into the file, or the crewmate would silently
lose it.

For every crewmate whose ``kiro_agent`` does not yet resolve to its own file,
this module:

1. reads the agent ``kiro_agent`` resolves to today (prompt inlined) and runs it
   through the governance sanitizer, exactly as its sessions saw it;
2. builds the file from what that agent adds to the worker base
   (:func:`kiro_crew.mate_worker.worker_additions`): its prompt with the SOPs it
   holds, the MCP servers the base lacks with their auto-approvals and their
   ``allowedTools`` grants, its skills, and its restrictions (``excludedTools``
   plus every ``disabledTools`` entry, and a base server it switched off whole).
   Anything equal to the worker base is not copied, so a crewmate on the default
   template gets a file that adds nothing;
3. dry-runs the merge (:func:`dry_run`): the merged worker must grant exactly
   the worker base plus the carried additions, governance applied -- nothing
   the old agent had is lost, nothing it lacked is gained, and no server it
   switched off starts. The tools the base withholds from every worker (its
   ``excludedTools``) and the hooks the base runs itself are the worker's by
   design, not a loss. A crewmate that fails is skipped, its binding left as it
   was, and the reason logged at WARNING under its display name;
4. writes the file and the worker-kit record naming the agent it was copied
   from (``mate_worker_kit``), so Advanced > Re-copy from template works, reads
   the file back through the same merge and checks it again;
5. only once every candidate has been written and proven, repoints each
   ``kiro_agent`` to the crewmate's own id, one member at a time, under the same
   optimistic-lock write the create path uses, and records the one upgrade
   notice the crewmate owes its user (:mod:`kiro_crew.mate_upgrade_notice`);
6. moves each moved crewmate's pre-upgrade DM threads onto its conductor
   (:func:`repoint_moved_threads`): their execution records name the agent it
   ran on, and only ``template_id`` changes, so their memory stays where it is.

Everything carried over is the crewmate's file content, so the worker's catalog
reports it as already there and no capability card is ever raised for it
(``mate_capability_catalog.worker_has``).

Nothing is deleted: the shared template and any ``crew-<hex>`` copy stay on
disk, so pointing ``agents.<name>.kiro_agent`` back at the old name rolls a
crewmate back (its own file is then ignored). Every moved crewmate is recorded
in ``~/.kiro/mates/migrated.json``, so a rolled-back one is not moved again.
Idempotent: a crewmate already on its own id with a file, or already recorded,
is not a candidate, and a skipped one is evaluated again, unchanged, on the
next boot. Best effort per crewmate and never raises.
Running sub-agents need nothing here: the upgrade's gateway restart ends them
through the restart reconciliation.

Two directories, two helpers, never one shared root: the user-level agents dir
and the mate dir come from :func:`kiro_crew.config.paths.kiro_agents_dir` and
:func:`kiro_crew.config.paths.mate_agents_dir`, which an isolated instance can
resolve to different roots. Runs off the event loop at startup and never gates
readiness.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

from kiro_crew.agent_files import is_mate_service_agent
from kiro_crew.agent_sdk import agent_specs
from kiro_crew.config import paths as paths_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mate_config import (
    MateConfigError,
    _resolved_source_spec,
    has_mate_config,
    source_agents_dir,
    write_mate_spec,
)
from kiro_crew.mate_worker import (
    MATE_CONDUCTOR_AGENT,
    MATE_WORKER_AGENT,
    disabled_tool_refs,
    merge_worker_spec,
    worker_additions,
)
from kiro_crew.mcp_cleanup import mcp_entry_is_muted
from kiro_crew.user_json import copy_json_document

logger = logging.getLogger(__name__)

#: How many differences a skip reason names before it says "and N more".
_REASON_ITEMS = 5


@dataclasses.dataclass
class MigrationReport:
    migrated: list[str] = dataclasses.field(default_factory=list)
    copy_failed: dict[str, str] = dataclasses.field(default_factory=dict)
    dry_run_failed: dict[str, str] = dataclasses.field(default_factory=dict)
    repoint_failed: dict[str, str] = dataclasses.field(default_factory=dict)
    skipped_no_source: list[str] = dataclasses.field(default_factory=list)
    #: Per crewmate, the pre-upgrade DM threads moved onto its conductor.
    threads_repointed: dict[str, list[str]] = dataclasses.field(default_factory=dict)


def _ledger_path():
    return paths_mod.mate_agents_dir().parent / "migrated.json"


def _read_ledger() -> dict[str, Any]:
    """``migrated.json`` as written, ``{}`` when it is absent or unreadable."""
    try:
        data = json.loads(_ledger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_ledger(data: dict[str, Any]) -> None:
    from kiro_crew.atomic_write import atomic_write

    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(data, indent=2) + "\n", restrict_to_owner=True)


def _names(data: dict[str, Any]) -> set[str]:
    names = data.get("migrated")
    return {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()


def migrated_names() -> set[str]:
    """The crewmates this migration has already moved. Unreadable reads empty."""
    return _names(_read_ledger())


def _record_migrated(name: str, source: str = "") -> None:
    """Record *name* as moved; with *source*, owe a repoint of its old threads.

    *source* is the agent the crewmate ran on before the move. Its entry under
    ``threads`` names it, so :func:`repoint_moved_threads` knows which records are
    the crewmate's pre-upgrade chats and a rollback knows what to put back.
    """
    data = _read_ledger()
    data["migrated"] = sorted(_names(data) | {name})
    if source:
        threads = data.get("threads")
        threads = threads if isinstance(threads, dict) else {}
        threads.setdefault(name, {"from": source, "repointed": [], "done": False})
        data["threads"] = threads
    _write_ledger(data)


def _dm_thread_keys(slugs: set[str]) -> list[str]:
    """The session keys of every DM thread transcript whose slot names one of *slugs*.

    A member's pinned DM thread is logged under ``dashboard_<slot key>``, the slot
    key being ``member-<slug>`` with an optional ``.memory-<store>`` generation
    (:func:`kiro_crew.members.member_slot_key`). Read from the file names alone,
    so nothing is opened here. Returned in the ``dashboard:<slot key>`` spelling
    every out-of-turn touch of a member session uses.
    """
    import os

    from kiro_crew.history_files import _sessions_dir
    from kiro_crew.members import slug_from_dm_slot_key

    prefix = "dashboard_"
    keys: list[str] = []
    try:
        with os.scandir(_sessions_dir()) as entries:
            names = sorted(e.name for e in entries if e.name.endswith(".jsonl"))
    except OSError:
        return keys
    for filename in names:
        stem = filename[: -len(".jsonl")]
        if not stem.startswith(prefix):
            continue
        slot = stem[len(prefix) :]
        if slug_from_dm_slot_key(slot) in slugs:
            keys.append(f"dashboard:{slot}")
    return keys


def _repoint_member_threads(member_id: str, slugs: set[str], source: str) -> tuple[list[str], bool]:
    """Move *member_id*'s pre-upgrade DM threads from *source* onto the conductor.

    A record is the crewmate's own pre-upgrade chat only when it is a persistent
    ``member`` selection carrying *member_id* and running *source*, the agent the
    crewmate was moved from. Only its ``template_id`` changes; member, store,
    selection, mode and app stay, so memory routing does not move. The write is
    :func:`kiro_crew.execution_context.bind_session_execution`'s compare-and-set
    against the value just read, not vouched: a template switch republishes the
    store the record already names. Returns the keys moved and whether every
    thread could be read and written (False leaves the pass owed).
    """
    from kiro_crew.execution_context import bind_session_execution, read_session_execution

    moved: list[str] = []
    complete = True
    for key in _dm_thread_keys(slugs):
        try:
            current = read_session_execution(key)
        except Exception:  # noqa: BLE001 - an unreadable record stays as it is
            logger.warning("crewmate migration: thread %s could not be read", key, exc_info=True)
            complete = False
            continue
        if (
            current is None
            or current.member_id != member_id
            or current.selection_kind != "member"
            or current.template_id != source
            or current.memory_mode != "persistent"
        ):
            continue
        conductor = dataclasses.replace(current, template_id=MATE_CONDUCTOR_AGENT)
        try:
            bind_session_execution(
                key, conductor, replace_existing=True, expected=current, vouch=False
            )
        except Exception:  # noqa: BLE001 - a record that moved meanwhile is left
            logger.warning("crewmate migration: thread %s was not repointed", key, exc_info=True)
            complete = False
            continue
        moved.append(key)
    return moved, complete


def repoint_moved_threads() -> dict[str, list[str]]:
    """Put every moved crewmate's pre-upgrade DM threads on its conductor. Never raises.

    The migration repoints the binding, which decides the agent of a NEW thread.
    A thread opened before the upgrade carries its own execution record naming
    the agent the crewmate ran on, and every turn reuses that record, so without
    this pass an old chat keeps running its old template after the move.

    Owed for each crewmate ``migrated.json`` records with a ``from`` agent and not
    yet ``done``. A crewmate that was rolled back (its ``kiro_agent`` no longer its
    own id) or removed is left alone, as is every thread of a skipped crewmate,
    which the ledger does not name. The keys moved are recorded under
    ``repointed`` so a rollback can put them back on ``from``; ``done`` is set once
    every thread was read, so a second boot reads nothing. A thread whose turn is
    running when this runs takes the conductor on its next turn.
    """
    out: dict[str, list[str]] = {}
    try:
        data = _read_ledger()
        threads = data.get("threads")
        if not isinstance(threads, dict):
            return out
        pending = {
            name: entry
            for name, entry in threads.items()
            if isinstance(entry, dict)
            and not entry.get("done")
            and isinstance(entry.get("from"), str)
            and entry["from"]
        }
        if not pending:
            return out
        from kiro_crew.members import member_slug

        cfg = KiroCrewConfig.load()
        for name, entry in pending.items():
            binding = cfg.agents.get(name)
            member_id = str(getattr(binding, "member_id", "") or "").strip()
            if binding is None or not member_id or binding.kiro_agent != name:
                continue
            if _shadowed_by_user_agent(name):
                # Its binding names a user-level agent, not its own file, so its
                # chat does not run on the conductor; its threads stay with it.
                continue
            slugs = {member_id}
            try:
                slugs.add(member_slug(name, cfg))
            except Exception:  # noqa: BLE001 - the id alone still finds its threads
                pass
            moved, complete = _repoint_member_threads(member_id, slugs, entry["from"])
            entry["repointed"] = sorted(set(_strings(entry.get("repointed"))) | set(moved))
            entry["done"] = complete
            out[name] = moved
            if moved:
                logger.info(
                    "crewmate migration: %s's %d earlier thread(s) now run on its conductor",
                    _label(cfg, name),
                    len(moved),
                )
        _write_ledger(data)
    except Exception:
        logger.warning("crewmate migration: earlier threads were not repointed", exc_info=True)
    return out


def _shadowed_by_user_agent(name: str) -> bool:
    """Whether a user-level agent declares *name*, so a crewmate file *name* is never read.

    Resolution reads ``~/.kiro/agents/`` before the crewmate folder, and the
    conductor route needs a file found ONLY in the crewmate folder
    (``mate_worker.is_mate_owned_agent``). An unreadable directory answers True:
    the crewmate then stays where it is, which is the safe side.
    """
    from kiro_crew.agent import agent_spec_path

    try:
        return agent_spec_path(name, agents_dir=paths_mod.kiro_agents_dir()) is not None
    except (OSError, ValueError):
        return True


def _candidates(cfg: KiroCrewConfig) -> dict[str, str]:
    """Crewmates whose ``kiro_agent`` does not resolve to their own file yet.

    Returns ``{member_name: source_agent}``. A record with no ``kiro_agent``,
    one already on its own id with a file, one this migration already moved
    (pointed back by hand, it stays where its owner put it), and the reserved
    ``default`` binding are excluded. So is a binding with no ``member_id``: it
    is a legacy agent binding (an older install's ``assistant``), not a
    crewmate, and is left as it is without a word.
    """
    out: dict[str, str] = {}
    done = migrated_names()
    for name, binding in cfg.agents.items():
        if name == "default" or name in done:
            continue
        if not str(getattr(binding, "member_id", "") or "").strip():
            logger.debug("crewmate migration: %r has no member id; not a crewmate", name)
            continue
        if is_mate_service_agent(name):
            # Its file would overwrite a spec Kiro Crew generates in the same folder.
            logger.warning(
                "crewmate migration: %r keeps its agent because a crewmate cannot be "
                "named after a spec Kiro Crew generates; rename it to move it",
                name,
            )
            continue
        source = getattr(binding, "kiro_agent", "")
        if not source:
            continue
        if source == name and has_mate_config(name):
            continue
        out[name] = source
    return out


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _servers(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, dict)}


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _grants(spec: dict[str, Any], base_servers: set[str]) -> set[tuple[str, ...]]:
    """What *spec* lets a session do, one token per grant.

    A server the worker base also declares counts by name alone: the base's
    entry always wins the merge, so its transport is the base's by design. Any
    other server counts with its transport. An approval (``allowedTools`` or a
    server's ``autoApprove``) for a tool the spec itself excludes grants nothing.
    """
    servers = _servers(spec.get("mcpServers"))
    excluded = set(_strings(spec.get("excludedTools"))) | set(disabled_tool_refs(servers))
    out: set[tuple[str, ...]] = set()
    for name, entry in servers.items():
        out.add(("server", name))
        if name not in base_servers:
            transport = {
                k: v for k, v in entry.items() if k not in ("autoApprove", "disabledTools")
            }
            out.add(("transport", name, _canon(transport)))
        for tool in _strings(entry.get("autoApprove")):
            if f"@{name}/{tool}" not in excluded:
                out.add(("approve", f"@{name}/{tool}"))
    for ref in _strings(spec.get("allowedTools")):
        if ref not in excluded:
            out.add(("allow", ref))
    for ref in _strings(spec.get("tools")):
        out.add(("tool", ref))
    for key in ("resources", "skills"):
        for ref in _strings(spec.get(key)):
            out.add((key, ref))
    return out


def _token_text(token: tuple[str, ...]) -> str:
    kind = token[0]
    if kind == "server":
        return f"MCP server {token[1]!r}"
    if kind == "transport":
        return f"MCP server {token[1]!r} as defined"
    if kind in ("approve", "allow"):
        return f"running {token[1]} without asking"
    if kind == "tool":
        return f"tool {token[1]!r}"
    return f"{kind[:-1]} {token[1]!r}"


def _describe(label: str, tokens: set[tuple[str, ...]]) -> str:
    items = sorted(_token_text(t) for t in tokens)
    shown = ", ".join(items[:_REASON_ITEMS])
    more = len(items) - _REASON_ITEMS
    return f"{label} {shown}" + (f" and {more} more" if more > 0 else "")


def _hook_entries(spec: dict[str, Any]) -> set[tuple[str, str]]:
    """``(event, entry)`` for every hook *spec* declares, each entry canonical."""
    hooks = spec.get("hooks")
    if not isinstance(hooks, dict):
        return set()
    out: set[tuple[str, str]] = set()
    for event, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else [entries]:
            out.add((str(event), _canon(entry)))
    return out


def _worker_withheld(base: dict[str, Any]) -> set[str]:
    """The tool references the worker base takes out of every crewmate's worker.

    The base's own ``excludedTools`` (the conductor-only ``kirocrew-mate`` tools
    among them) apply to every worker by design, so an old agent's approval for
    one of them is the conductor's to hold now, not a capability this crewmate
    loses.
    """
    return set(_strings(base.get("excludedTools")))


def _ref_server(ref: str) -> str:
    """The server an ``@server`` / ``@server/tool`` reference names, ``""`` for a built-in."""
    return ref[1:].split("/", 1)[0] if ref.startswith("@") else ""


def _base_owned_servers(base: dict[str, Any]) -> set[str]:
    """The servers whose grants the worker base decides: its own and the platform's.

    The base declares its servers with the approvals it chose, and a platform
    server it leaves out (``mate_worker.conductor_mcp_servers``) is the
    conductor's, never a worker file's. What the base gives on these is what
    every worker gets, by design.
    """
    from kiro_crew.mate_worker import conductor_mcp_servers

    servers = set(_servers(base.get("mcpServers")))
    try:
        servers |= set(conductor_mcp_servers())
    except Exception:  # noqa: BLE001 - the base's own servers still answer
        logger.debug("crewmate migration: platform server list unreadable", exc_info=True)
    return servers


def _by_design(grant: tuple[str, ...], owned: set[str], withheld: set[str]) -> bool:
    """Whether the worker base, not the merge, is why *grant* is missing.

    The base's ``excludedTools``; every approval it does not give for a built-in
    tool or for a server it owns (an old ``@kirocrew-cron`` or
    ``@kirocrew-guide/rename_self`` auto-approval); and a mount of a server it
    owns. A grant the old agent had on a server the base does NOT own is the
    crewmate's own, and losing it is a real loss.
    """
    kind = grant[0]
    if kind in ("approve", "allow"):
        ref = grant[1]
        return ref in withheld or not _ref_server(ref) or _ref_server(ref) in owned
    if kind == "tool":
        return grant[1] in withheld or _ref_server(grant[1]) in owned
    if kind in ("server", "transport"):
        return grant[1] in owned
    return False


def dry_run(base: dict[str, Any], source: dict[str, Any], mate_file: dict[str, Any]) -> str:
    """Why the merged worker would not grant exactly its old capabilities, else ``""``.

    *base* is the worker's source base (prompt literal), *source* the governed
    agent the crewmate runs on today (prompt literal), *mate_file* the file the
    migration would write. The merged worker is what
    :func:`kiro_crew.mate_worker.resolve_worker_spec` projects from that file.
    It must keep every grant *source* has that the base lacks and add none that
    neither has. What the base deliberately does not give is not a loss
    (:func:`_by_design`: its excluded tools, and the approvals and mounts on
    built-ins and on the servers it owns); its
    prompt must carry the old prompt; and an agent with a hook the worker base
    does not run is not moved.
    """
    additions = worker_additions(base, mate_file)
    merged = merge_worker_spec(base, additions)
    base_servers = set(_servers(base.get("mcpServers")))
    have_base = _grants(base, base_servers)
    have_source = _grants(source, base_servers)
    have_merged = _grants(merged, base_servers)
    problems = []
    withheld = _worker_withheld(base)
    owned = _base_owned_servers(base)
    lost = {
        t for t in (have_source - have_base) - have_merged if not _by_design(t, owned, withheld)
    }
    gained = have_merged - (have_base | have_source)
    if lost:
        problems.append(_describe("its worker would lose", lost))
    if gained:
        problems.append(_describe("its worker would gain", gained))
    # A server the old agent switched off stays off: the worker base declaring it
    # is no licence to launch it. Base-owned or not, this is a restriction to keep.
    relaunched = {
        name
        for name, entry in _servers(source.get("mcpServers")).items()
        if mcp_entry_is_muted(entry)
        and name in _servers(merged.get("mcpServers"))
        and not mcp_entry_is_muted(_servers(merged.get("mcpServers"))[name])
    }
    if relaunched:
        problems.append(
            _describe("its worker would start", {("server", n) for n in relaunched})
            + " it had switched off"
        )
    old_prompt, base_prompt, new_prompt = (
        p if isinstance(p, str) else ""
        for p in (source.get("prompt"), base.get("prompt"), merged.get("prompt"))
    )
    if old_prompt.strip() and old_prompt.strip() != base_prompt.strip():
        if old_prompt.strip() not in new_prompt:
            problems.append("its worker would not carry its prompt")
    if _hook_entries(source) - _hook_entries(base):
        problems.append("its hooks would not run on the worker")
    return "; ".join(problems)


def migration_file(name: str, base: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """The file crewmate *name* gets: what *source* adds to the worker *base*.

    Only additions and restrictions are written; everything the base already
    carries is left out. A per-tool limit on a server the base declares is kept
    as an ``excludedTools`` entry, since the base's entry wins the merge, and a
    base server the agent switched off whole is kept as ``{"disabled": true}``.
    """
    adds = worker_additions(base, source)
    spec: dict[str, Any] = {"name": name}
    if adds["prompt"]:
        spec["prompt"] = adds["prompt"]
    servers = copy_json_document(adds["mcpServers"])
    spec["mcpServers"] = servers
    spec["tools"] = [f"@{server}" for server in servers]
    spec["allowedTools"] = list(adds["allowedTools"])
    for key in ("resources", "skills"):
        if adds[key]:
            spec[key] = list(adds[key])
    excluded = list(
        dict.fromkeys(
            [
                *adds["excludedTools"],
                *(r for r in adds["disabledTools"] if r[1:].split("/", 1)[0] not in servers),
            ]
        )
    )
    if excluded:
        spec["excludedTools"] = excluded
    # A whole-server switch-off on a base server is kept as the name and the
    # switch alone; the base's entry keeps its transport and the merge mutes it.
    for server in adds["disabledServers"]:
        servers[server] = {"disabled": True}
    return spec


def _default_template_file(name: str, base: dict[str, Any]) -> dict[str, Any]:
    """The file a crewmate on the conductor gets: the default template's additions.

    A crewmate created on the default template gets that template's additions
    to the worker base (the user-level servers ``kirocrew.json`` carries and the
    worker base lacks). The first crewmate is created on the conductor, so it is
    given the same additions here, through the same dry-run every migrated
    crewmate passes. An unreadable template, or one whose additions would not
    pass, leaves it the file that adds nothing.
    """
    from kiro_crew.agent_files import AGENT_FILENAME

    template = AGENT_FILENAME.removesuffix(".json")
    try:
        source = _governed_source(template)
    except MateConfigError as exc:
        logger.info("crewmate migration: %r unreadable for %s: %s", template, name, exc)
        return migration_file(name, base, {})
    planned = migration_file(name, base, source)
    reason = dry_run(base, source, planned)
    if reason:
        logger.info("crewmate migration: %s gets no additions because %s", name, reason)
        return migration_file(name, base, {})
    return planned


def _governed_source(source_agent: str) -> dict[str, Any]:
    from kiro_crew.platform.governance import sanitize_agent_config_governance

    spec = _resolved_source_spec(source_agent)
    try:
        raw = agent_specs.load_agent_spec(source_agents_dir(source_agent), source_agent)
    except agent_specs.AgentSpecUnreadable as exc:
        raise MateConfigError(str(exc)) from exc
    raw_prompt = raw.get("prompt")
    if not (isinstance(raw_prompt, str) and raw_prompt.strip()):
        # A prompt-less agent resolves to the engine's fallback text, which is
        # not something the agent carried, so it is no addition to the worker.
        spec["prompt"] = ""
    sanitize_agent_config_governance(spec, audit=False)
    return spec


def _worker_base() -> dict[str, Any]:
    """The worker's source base with its prompt as literal text.

    Read from the crewmate folder, where Crew generates it
    (``agent_files.MATE_SERVICE_AGENT_FILENAMES``).
    """
    agents_dir = paths_mod.mate_agents_dir()
    base = agent_specs.load_agent_spec(agents_dir, MATE_WORKER_AGENT)
    base = copy_json_document(base)
    base["prompt"] = agent_specs.resolve_prompt(
        base, agent_id=MATE_WORKER_AGENT, agents_dir=agents_dir
    )
    return base


def _write_file(name: str, spec: dict[str, Any]) -> None:
    write_mate_spec(name, spec)


def _read_back(name: str) -> dict[str, Any]:
    """The written file as the worker merge reads it (``resolve_worker_spec``)."""
    mates_dir = paths_mod.mate_agents_dir()
    spec = agent_specs.load_agent_spec(mates_dir, name)
    prompt = spec.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        spec["prompt"] = agent_specs.resolve_prompt(spec, agent_id=name, agents_dir=mates_dir)
    else:
        spec["prompt"] = ""
    return spec


def _label(cfg: KiroCrewConfig, name: str) -> str:
    display = getattr(cfg.agents.get(name), "display_name", "") or ""
    return display.strip() if isinstance(display, str) and display.strip() else name


def repair_partial_platform_servers() -> list[str]:
    """Rewrite each crewmate file holding a platform server with no transport.

    Such an entry (``"kirocrew-mate": {"disabled": false}``) was written by an
    older create from a template that lacked the server. It is dropped
    (:func:`kiro_crew.mate_worker.drop_partial_platform_servers`) and the file is
    written back through the crewmate writer. Returns the crewmates repaired.
    Never raises; a file that cannot be read or written is logged and left.
    """
    from kiro_crew.mate_worker import drop_partial_platform_servers

    repaired: list[str] = []
    try:
        files = sorted(paths_mod.mate_agents_dir().glob("*.json"))
    except OSError:
        return repaired
    for path in files:
        name = path.stem
        if is_mate_service_agent(name):
            # The generated conductor and worker specs share the folder.
            continue
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(spec, dict) or not drop_partial_platform_servers(spec):
                continue
            write_mate_spec(name, spec)
        except (OSError, ValueError, MateConfigError):
            logger.warning("crewmate file %s: platform server entry not repaired", name)
            continue
        repaired.append(name)
        logger.info("crewmate file %s: dropped a platform server entry with no transport", name)
    return repaired


def migrate_existing_crewmates(
    cfg: KiroCrewConfig | None = None, *, work_dir: str = ""
) -> MigrationReport:
    """Run the migration; safe to call on every boot (idempotent). Never raises.

    *work_dir* is accepted for the startup caller's signature and unused: the
    source agent is read from the user-level agents folder, as the create path
    reads it.
    """
    del work_dir
    report = MigrationReport()
    repair_partial_platform_servers()
    try:
        cfg = cfg or KiroCrewConfig.load()
        paths_mod.mate_agents_dir().mkdir(parents=True, exist_ok=True)
        candidates = _candidates(cfg)
        if not candidates:
            report.threads_repointed = repoint_moved_threads()
            return report
        base = _worker_base()
    except Exception:
        logger.warning(
            "crewmate migration: could not read the crewmates or the worker base", exc_info=True
        )
        return report

    verified: list[str] = []
    unchanged: set[str] = set()
    for name, source in candidates.items():
        label = _label(cfg, name)
        if source == MATE_CONDUCTOR_AGENT:
            # Bound to the conductor already (the first crewmate a fresh install
            # creates): its chat runs where it will keep running, so it owes no
            # upgrade notice and has no kit to record. Its file is seeded from
            # the default template, exactly as a crewmate created on that
            # template is, so its worker carries the same user-level servers.
            unchanged.add(name)
            try:
                _write_file(name, _default_template_file(name, base))
            except MateConfigError as exc:
                report.copy_failed[name] = str(exc)
                logger.warning("crewmate migration: %s's file was not written: %s", label, exc)
                continue
            verified.append(name)
            continue
        if _shadowed_by_user_agent(name):
            # A user-level agent carries the crewmate's own id, so a file under
            # that id would never be read: resolution prefers the user level, and
            # the binding already names it. Moving would change nothing but the
            # thread records. Kept where it is, said once.
            report.dry_run_failed[name] = (
                "its id is also the name of a user-level agent, so its own file would "
                "never be read; rename that agent or the crewmate to move it"
            )
            logger.warning(
                "crewmate migration: %s stays on %r because %s",
                label,
                source,
                report.dry_run_failed[name],
            )
            continue
        try:
            old = _governed_source(source)
        except MateConfigError as exc:
            report.skipped_no_source.append(name)
            logger.info("crewmate migration: %s's agent %r unreadable: %s", label, source, exc)
            continue
        planned = migration_file(name, base, old)
        reason = dry_run(base, old, planned)
        if reason:
            report.dry_run_failed[name] = reason
            logger.warning("crewmate migration: %s stays on %r because %s", label, source, reason)
            continue
        try:
            _write_file(name, planned)
            reason = dry_run(base, old, _read_back(name))
        except (MateConfigError, agent_specs.AgentSpecUnreadable) as exc:
            report.copy_failed[name] = str(exc)
            logger.warning("crewmate migration: %s's file was not written: %s", label, exc)
            continue
        if reason:
            report.dry_run_failed[name] = reason
            logger.warning(
                "crewmate migration: %s stays on %r because its written file %s",
                label,
                source,
                reason,
            )
            continue
        _record_kit(name, source, planned)
        verified.append(name)

    # Pointers move only once every candidate above has been written and proven.
    for name in verified:
        try:
            _repoint(name)
        except Exception as exc:
            report.repoint_failed[name] = str(exc)
            logger.warning("crewmate migration: repoint failed for %s: %s", _label(cfg, name), exc)
            continue
        report.migrated.append(name)
        from kiro_crew.mate_upgrade_notice import mark_notice_owed

        if name not in unchanged:
            mark_notice_owed(name)
        try:
            _record_migrated(name, "" if name in unchanged else candidates[name])
        except OSError:
            # The binding already points at the file, so it is not a candidate
            # again either way; only a later hand-made rollback is affected.
            logger.warning("crewmate migration: %s not recorded as moved", name, exc_info=True)

    if report.migrated:
        logger.info(
            "crewmate migration: moved %d crewmate(s) onto their worker: %s",
            len(report.migrated),
            ", ".join(sorted(_label(cfg, n) for n in report.migrated)),
        )
    # After the repoints, so a thread moves only with a crewmate that moved.
    report.threads_repointed = repoint_moved_threads()
    return report


def _record_kit(name: str, source: str, spec: dict[str, Any]) -> None:
    """Record *source* as the agent the worker kit was copied from. Never raises.

    The default template is not recorded, as on the create path: it adds
    nothing to the worker, so there is nothing to re-copy from it.
    """
    from kiro_crew.agent_files import is_kirocrew_default_agent
    from kiro_crew.mate_worker_kit import _kit_of, write_record

    if is_kirocrew_default_agent(source):
        return
    try:
        write_record(name, source, _kit_of(spec))
    except Exception:  # noqa: BLE001 - the file stands; a re-copy then keeps all
        logger.warning(
            "crewmate migration: worker kit record not written for %s", name, exc_info=True
        )


def _repoint(name: str) -> None:
    """Flip *name*'s ``kiro_agent`` to its own id, under the config lock.

    The create path's write (same optimistic lock via ``expected_store``,
    ``changed_fields={"kiro_agent"}``), re-reading the config fresh under the
    lock rather than reusing a snapshot a concurrent writer may have moved on.
    """
    from kiro_crew.memory_stores import persist_member_config

    cfg = KiroCrewConfig.load()
    binding = cfg.agents.get(name)
    if binding is None:
        raise MateConfigError(f"{name!r} is no longer a configured agent")
    prior_store = binding.memory_store
    binding.kiro_agent = name
    persist_member_config(cfg, name, expected_store=prior_store, changed_fields={"kiro_agent"})
