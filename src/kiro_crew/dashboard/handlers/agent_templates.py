"""Agent template management: the roster the Agent templates tab reads and writes.

A template is the harness-level agent definition (``~/.kiro/agents/<name>.json``)
a chat or a crewmate runs. The crew editor's Template pane edits a crew's
PRIVATE copy of one (blueprint semantics, see ``api_agent_fork``); this module is
the other half -- managing the shared templates themselves:

* ``GET /api/agents/templates`` -- every installed global template with the two
  things a management page needs beside the spec's own fields: whether it can be
  edited here (``read_only`` names why not), and what still points at it
  (``used_by``: crews, the default agent, schedules, chat folders, webhooks,
  private copies).
* ``POST /api/agents/templates`` -- create a template, blank or as a copy of an
  installed one. Nothing is enrolled as a crewmate.
* ``DELETE /api/agents/detail/{name}`` -- delete a template the user owns, refused
  while anything still references it (``409 template_referenced`` lists what).

What is NOT editable here, and why each is a rule rather than a gap:

* a **package** template (``<Package>-<name>.json``): the package rewrites the
  file on its next install, so an edit would be silently reverted -- duplicate
  it to own a copy;
* a **runtime** template (``kirocrew*.json`` in ``OWNED_KIRO_AGENT_FILES``): the
  runtime refreshes these; the same reversal applies;
* a **markdown** spec: one hand-authored document that a JSON round-trip would
  lose fields from (the detail PATCH already refuses it);
* a **private copy** (``private_to`` set): it belongs to one crew and is edited
  from that crew's Template pane, where reset/publish keep its lineage straight.

What the reference guard counts, and the one holder it deliberately does not:

* it counts every CONFIGURATION that names a template -- a crew binding, the
  default agent, a schedule's ``agent_id``, a chat folder's ``default_agent``, a
  webhook token's ``agent``, a private copy's ``forked_from`` -- because each
  silently redirects FUTURE work (a new session, the next cron fire, the next
  webhook call) onto whatever the missing name falls back to;
* it does NOT count an open chat slot that picked the template (``agent_kind:
  "template"``). A slot is a conversation the user has on screen, not a
  configuration that outlives it: its header names the template, and a delete
  guard that passed or refused depending on which tabs happen to be open in
  which window would be one nobody could reason about from the roster. The
  slot's next session start degrades the way any slot naming an unknown agent
  does (kiro-cli falls back to the default spec), visibly, in that chat.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from kiro_crew import agent_state
from kiro_crew.agent import agents_spec_lock, kiro_agents_dir_path
from kiro_crew.agent_capabilities import CapabilityError, require_unmanaged_template
from kiro_crew.agent_discovery import (
    AgentInfo,
    _global_agent_info,
    _read_agent_spec,
    clear_list_agents_cache,
    iter_agent_spec_files,
    list_agents,
)
from kiro_crew.agent_files import KAS_RESERVED_AGENT_IDS, OWNED_KIRO_AGENT_FILES
from kiro_crew.agent_spec_format import is_markdown_spec
from kiro_crew.config.loader import (
    KiroCrewConfig,
    config_local_path,
    publish_materialized_agents,
    refresh_materialized_agents,
    schedule_materialized_agents_refresh,
    update_config_locked,
)
from kiro_crew.config.paths import config_dir
from kiro_crew.cron import (
    CronStoreBusy,
    CronStoreUnreadable,
    cron_store_lock,
    dispatched_agents_from_disk,
)
from kiro_crew.dashboard.chat_utils import drained_to_thread
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor
from kiro_crew.sel import sel
from kiro_crew.webhooks import token_store

logger = logging.getLogger(__name__)

#: Longest prompt / description the dashboard editor writes. kiro-cli imposes no
#: cap, but a spec is read on every session start, and a multi-megabyte prompt
#: from a paste accident is a defect, not a feature.
MAX_TEMPLATE_PROMPT_CHARS = 200_000
MAX_TEMPLATE_DESCRIPTION_CHARS = 2_000
#: Tool references per list. Generous -- a wildcard covers a whole server -- but
#: finite, like ``MAX_AGENT_SKILLS`` on the skills list.
MAX_TEMPLATE_TOOLS = 200
#: Longest single tool reference. A real one is ``@server/tool`` or a wildcard,
#: tens of characters; the cap keeps the list's total size bounded by count.
MAX_TEMPLATE_TOOL_CHARS = 256

#: ``(folder_id, folder_name, default_agent)`` for one chat folder, read from
#: the folder store on the loop (the roster snapshots it; the delete guard
#: holds the store lock while it checks, see ``api_agent_template_delete``).
FolderPin = tuple[str, str, str]

#: The spec keys the templates tab may write through the detail PATCH, beyond the
#: ``model`` and ``skills`` the crew pane already writes. ``resources`` and
#: ``mcpServers`` are deliberately absent: skills are a computed view OVER
#: ``resources`` (writing both would race), and an MCP server is a capability
#: grant that has its own admission path (the MCP page and its quarantine).
TEMPLATE_DEFINITION_KEYS = frozenset({"description", "prompt", "tools", "allowedTools"})

_READ_ONLY_PACKAGE = "package"
_READ_ONLY_RUNTIME = "runtime"
_READ_ONLY_MARKDOWN = "markdown"
_READ_ONLY_PRIVATE_COPY = "private_copy"


def template_read_only_reason(info: AgentInfo) -> str | None:
    """Why *info* cannot be edited or deleted from the templates tab, or ``None``.

    Order matters only for the label: a markdown package spec is reported as a
    package spec, because duplicating it is the remedy either way.
    """
    if info.source == "package":
        return _READ_ONLY_PACKAGE
    if info.kirocrew_owned or info.filename in OWNED_KIRO_AGENT_FILES:
        return _READ_ONLY_RUNTIME
    if is_markdown_spec(info.filename):
        return _READ_ONLY_MARKDOWN
    if info.private_to:
        return _READ_ONLY_PRIVATE_COPY
    return None


def _cron_agent_ids() -> list[tuple[str, str, str]]:
    """``(job_id, job_name, agent)`` for every agent a stored job DISPATCHES.

    The scheduler owns the rule (``cron.dispatched_agents_from_disk``); this
    guard only asks for the records the scheduler could build, since a record
    that never fires must not pin a template. A pure file read, like
    ``count_enabled_from_disk``: the scheduler's in-memory snapshot belongs to
    the event loop and is not touched from here. Errors propagate: the guard
    fails closed, where doctor's wrapper fails open to ``[]``.
    """
    return dispatched_agents_from_disk(loadable_only=True)


def _webhook_agent_ids() -> list[tuple[str, str, str]]:
    """``(token_id, label, agent)`` for every webhook token pinned to an agent.

    A pinned token's calls are refused (``409 destination_agent_unavailable``)
    once the agent is gone, so the hook stops working rather than falling back;
    still a holder the guard must count -- a delete that breaks an integration
    is the same silent breakage one shelf over.
    """
    out: list[tuple[str, str, str]] = []
    for entry in token_store().list_entries():
        agent = entry.get("agent")
        if isinstance(agent, str) and agent:
            out.append(
                (str(entry.get("id", "")), str(entry.get("label") or entry.get("id", "")), agent)
            )
    return out


def template_references(
    aliases: set[str],
    cfg: KiroCrewConfig,
    forks: dict[str, dict],
    crons: list[tuple[str, str, str]],
    folders: list[FolderPin],
    webhooks: list[tuple[str, str, str]],
) -> list[dict[str, str]]:
    """Everything that still resolves one of *aliases* (a template's name and stem).

    Each row is ``{"kind", "id", "label"}``, ``kind`` one of ``crew``, ``default``,
    ``schedule``, ``folder``, ``webhook``, ``private_copy``. The list doubles as
    the delete guard's evidence and the roster's ``used_by``. A chat folder
    counts because its ``default_agent`` is what every new session filed there
    starts on; a template deleted underneath it would fall through to the
    default agent silently, the exact failure the guard exists to refuse.
    """
    refs: list[dict[str, str]] = []
    for crew, crew_cfg in sorted(cfg.agents.items()):
        if crew_cfg.kiro_agent in aliases:
            refs.append({"kind": "crew", "id": crew, "label": crew})
    # ``cfg.agent.default_agent`` is a TEMPLATE name (what a new session runs
    # when nothing else picks one). ``cfg.default_agent`` is not: ``load()``
    # normalizes it to a key of ``cfg.agents`` -- a crew alias -- and the
    # template that crew runs is already counted by the loop above, so
    # matching the alias here would call a same-named template "the default".
    if cfg.agent.default_agent in aliases:
        refs.append({"kind": "default", "id": "default", "label": "default agent"})
    seen_jobs: set[str] = set()
    for job_id, job_name, agent_id in crons:
        # One row per job, however many of its sequence entries name the template.
        if agent_id in aliases and job_id not in seen_jobs:
            seen_jobs.add(job_id)
            refs.append({"kind": "schedule", "id": job_id, "label": job_name})
    for folder_id, folder_name, default_agent in folders:
        if default_agent in aliases:
            refs.append({"kind": "folder", "id": folder_id, "label": folder_name})
    for token_id, label, agent in webhooks:
        if agent in aliases:
            refs.append({"kind": "webhook", "id": token_id, "label": label})
    for copy_name, info in sorted(forks.items()):
        if info.get("forked_from") in aliases:
            refs.append(
                {"kind": "private_copy", "id": copy_name, "label": str(info.get("private_to", ""))}
            )
    return refs


def _folder_pins_of(folders: list[dict[str, Any]]) -> list[FolderPin]:
    """The agent pins among *folders* (the store's committed list)."""
    return [
        (str(f.get("id", "")), str(f.get("name", "")), str(f.get("default_agent") or ""))
        for f in folders
        if f.get("default_agent")
    ]


async def _folder_pins(state: DashboardState) -> list[FolderPin]:
    """Snapshot the folder store's agent pins on the loop, for the executor."""
    return await state.read_folders(_folder_pins_of)


#: The discovery-row strings a package or an agent can write, each rendered
#: through ``_roster_mask`` like the two sibling roster paths do. ``name`` and
#: ``filename`` are the row's identity and are not maskable in place: a row
#: whose identity would be masked is left out, as ``agent_catalog`` leaves it
#: out of the picker.
_MASKED_ROW_FIELDS = ("description", "model", "source", "package", "forked_from", "private_to")
_MASKED_ROW_LISTS = ("skills", "mcp_servers")


def _masked_row(info: AgentInfo, mask: Callable[[object], str]) -> dict[str, Any] | None:
    """``info`` as a roster row with every externally controlled string masked."""
    if mask(info.name) != info.name or mask(info.filename) != info.filename:
        return None
    row = info.to_dict()
    for key in _MASKED_ROW_FIELDS:
        row[key] = mask(row.get(key))
    for key in _MASKED_ROW_LISTS:
        value = row.get(key)
        row[key] = [mask(item) for item in value] if isinstance(value, list) else []
    return row


def _masked_refs(refs: list[dict[str, str]], mask: Callable[[object], str]) -> list[dict[str, str]]:
    """Reference rows with their agent-writable ``id`` / ``label`` masked; ``kind``
    is this module's own vocabulary and stays."""
    return [{**ref, "id": mask(ref["id"]), "label": mask(ref["label"])} for ref in refs]


def _spec_file_beneath(agents_dir: Path, filename: str) -> Path | None:
    """The spec file a discovery row names, or ``None`` unless it is a plain
    basename that resolves to an existing regular file directly under
    *agents_dir*. A row's ``filename`` is discovery-supplied -- an edition
    catalog row carries whatever its provider wrote -- so the delete path never
    trusts it as a path: an absolute or traversing value names no template.
    """
    if not filename or Path(filename).name != filename:
        return None
    candidate = agents_dir / filename
    try:
        resolved = candidate.resolve(strict=True)
        root = agents_dir.resolve(strict=True)
    except OSError:
        return None
    if resolved.parent != root or not resolved.is_file():
        return None
    return candidate


def _tombstone(spec_path: Path) -> str:
    """Retire *spec_path* by renaming it to ``<name>.json.bak.<epoch>`` beside
    itself and return that filename.

    The one irreversible action on this tab becomes a rename: discovery reads
    only ``.json`` / ``.md`` suffixes, so the tombstone is invisible to every
    roster, and its shape is one the agents-directory janitor already recognises
    as an aged backup (swept only when ``agent.sweep_agents_backups`` is on). One
    deep: repeated create/delete cycles leave one recoverable copy, not a pile.
    Order matters: the rename to a fresh name comes first, and the earlier
    tombstones go only once it has succeeded, so a refused rename (a sharing lock
    on Windows) fails the request with both the live file and the previous
    recovery copy still on disk. Caller holds the spec lock.
    """
    stamp = int(time.time())
    grave = spec_path.with_name(f"{spec_path.name}.bak.{stamp}")
    n = 1
    while grave.exists():  # a second delete of the same name within one second
        grave = spec_path.with_name(f"{spec_path.name}.bak.{stamp}.{n}")
        n += 1
    spec_path.rename(grave)
    # Only a grave of THIS file goes: ``<name>.bak.<epoch>`` or its
    # same-second twin. A glob's ``*`` crosses dots, so ``foo.json.bak.*`` would
    # also match a live template someone named ``foo.json.bak.5`` (a legal name,
    # written to ``foo.json.bak.5.json``); the sweep must never unlink a spec.
    is_grave = re.compile(rf"^{re.escape(spec_path.name)}\.bak\.\d+(\.\d+)?$")
    for stale in spec_path.parent.glob(f"{spec_path.name}.bak.*"):
        if stale != grave and is_grave.match(stale.name):
            with contextlib.suppress(OSError):
                stale.unlink()
    return grave.name


def _template_rows(folders: list[FolderPin]) -> list[dict[str, Any]]:
    """Thread-side: the roster with editability and references attached.

    Every string a spec, a package, a crew record, a schedule, a folder or a
    webhook token can write reaches the browser through ``_roster_mask`` --
    the control ``GET /api/agents`` and the chat catalog apply -- so a
    credential- or exfil-URL-shaped value arrives as the sentinel, never
    verbatim. ``read_only`` and reference kinds are this module's own words.
    """
    # circular import: see api_agent_template_create.
    from kiro_crew.dashboard.handlers.agents import _roster_mask

    agents_dir = kiro_agents_dir_path()
    infos = list(list_agents(agents_dir=agents_dir))
    cfg = KiroCrewConfig.load()
    forks = agent_state.all_fork_info()
    crons = _cron_agent_ids()
    webhooks = _webhook_agent_ids()
    rows: list[dict[str, Any]] = []
    for info in infos:
        row = _masked_row(info, _roster_mask)
        if row is None:
            continue
        aliases = {info.name, Path(info.filename).stem}
        reason = template_read_only_reason(info)
        if reason is None and _spec_file_beneath(agents_dir, info.filename) is None:
            # An edition catalog row with no spec file beneath the agents
            # directory (an empty or foreign ``filename``): the runtime supplies
            # it, and no action here has a file to write, so the tab must not
            # offer one that can only answer 404.
            reason = _READ_ONLY_RUNTIME
        row["read_only"] = reason
        row["used_by"] = _masked_refs(
            template_references(aliases, cfg, forks, crons, folders, webhooks), _roster_mask
        )
        rows.append(row)
    rows.sort(key=lambda r: (r["source"] != "builtin" or r["kirocrew_owned"], r["name"].lower()))
    return rows


async def api_agent_templates(request: web.Request) -> web.Response:
    """GET /api/agents/templates -- the management roster of installed templates.

    Global scope only, like ``/api/agents/installed``: a project template is a
    file in that checkout and is edited there, and a management action taken
    here persists into the global agents directory.
    """
    state: DashboardState = request.app["state"]
    try:
        folders = await _folder_pins(state)
        rows = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), _template_rows, folders
        )
    except Exception:
        logger.warning("Agent templates roster could not be loaded", exc_info=True)
        return web.json_response(
            {
                "error": "Agent templates could not be loaded. Retry.",
                "code": "templates_unavailable",
            },
            status=503,
        )
    return web.json_response({"templates": rows})


def _find_infos(name: str) -> list[AgentInfo]:
    """Every spec FILE under the agents directory that *name* reaches, by
    declared name or by stem -- classified the way discovery classifies a row.

    Thread-side. Read from the raw file scan, never from ``list_agents``: the
    roster deduplicates by declared name (``atlas.json`` beside
    ``SomePkg-atlas.json`` keeps one row), so a second file claiming the same
    name is invisible there and a delete would unlink the one survivor as if it
    were unambiguous. Returning all of them, not the first, is what lets the
    delete refuse a crossover -- ``bar.json`` declaring ``baz`` beside
    ``foo.json`` declaring ``bar`` -- and a twin -- two files both declaring
    ``bar`` -- instead of unlinking whichever file the scan met first. The
    same walk the definition PATCH resolves against (``_agent_detail_candidates``).
    """
    agents_dir = kiro_agents_dir_path()
    forks = agent_state.all_fork_info()
    found: list[AgentInfo] = []
    for f in iter_agent_spec_files(agents_dir, ordered=False):
        spec = _read_agent_spec(f, operation="api_agent_template_delete", source="dashboard")
        if spec is None:
            continue
        info = _global_agent_info(f, spec)
        if info.name != name and f.stem != name:
            continue
        fork = forks.get(info.name)
        if fork:
            info.forked_from = str(fork.get("forked_from", ""))
            info.private_to = str(fork.get("private_to", ""))
        found.append(info)
    return found


def _blank_spec(name: str, description: str) -> dict[str, Any]:
    """The smallest spec kiro-cli runs: read-only tools, no skills, model auto."""
    return {
        "name": name,
        "description": description,
        "prompt": "",
        "tools": ["fs_read", "grep", "glob"],
        "allowedTools": ["fs_read", "grep", "glob"],
    }


async def api_agent_template_create(request: web.Request) -> web.Response:
    """POST /api/agents/templates -- create a template, blank or copied.

    Body: ``{"name", "description"?, "from"?}``. ``from`` names an installed
    template to copy (any source -- copying a package template is exactly how a
    user comes to own an editable version of it). The copy carries no fork
    lineage: it is a real, shareable template, not a crew's private copy.
    """
    # circular import: handlers.agents top-imports this module for the PATCH's
    # definition-key helpers, so its name/lock/spec helpers are bound here at
    # call time rather than at import time.
    from kiro_crew.dashboard.handlers.agents import (
        _TEMPLATE_NAME_RE,
        _AmbiguousTemplateName,
        _get_config_lock,
        _is_reserved_basename,
        _load_template_specs,
        _require_owner,
        _reserved_binding_names,
        _spec_stem_on_disk,
        _write_spec_file,
    )

    denied = await _require_owner(request, "agent_templates.create")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )
    raw_name = body.get("name")
    if not isinstance(raw_name, str) or not _TEMPLATE_NAME_RE.match(raw_name.strip()):
        return web.json_response(
            {
                "error": "name must be 1-63 letters, digits, dots, dashes or underscores",
                "code": "invalid_template_name",
            },
            status=400,
        )
    name: str = raw_name.strip()
    if _is_reserved_basename(name) or f"{name.lower()}.json" in {
        f.lower() for f in OWNED_KIRO_AGENT_FILES
    }:
        return web.json_response(
            {"error": f"'{name}' is reserved", "code": "template_name_reserved"}, status=400
        )
    if name in KAS_RESERVED_AGENT_IDS:
        # The KAS engine keeps its own agent under this id (or drops the entry)
        # without an error, so a template created under it never runs there.
        # Exact match, as the engine matches: ``Default`` registers normally.
        # Its own code, distinct from the runtime-owned stems above, so the
        # dashboard can say WHOSE name it is rather than "reserved" alone.
        return web.json_response(
            {
                "error": f"'{name}' is reserved by the KAS agent engine",
                "code": "template_name_reserved_by_engine",
            },
            status=400,
        )
    description = body.get("description", "")
    if not isinstance(description, str) or len(description) > MAX_TEMPLATE_DESCRIPTION_CHARS:
        return web.json_response(
            {"error": "description must be a short string", "code": "invalid_description"},
            status=400,
        )
    source_name = body.get("from")
    if source_name is not None and (not isinstance(source_name, str) or not source_name.strip()):
        return web.json_response(
            {"error": "from must name an installed template", "code": "invalid_source"}, status=400
        )
    source_key: str = source_name.strip() if isinstance(source_name, str) else ""

    state: DashboardState = request.app["state"]
    async with _get_config_lock():
        agents_dir = kiro_agents_dir_path()
        source_path: Path | None = None
        # Resolving either name can find two files declaring it (``atlas.json``
        # beside ``SomePkg-atlas.json``); that is the user's to untangle, so it
        # is a 409 naming the problem, not a 500. Only the source's PATH is kept
        # from this pre-lock read: its body is re-read inside the spec lock, so
        # a concurrent edit of the source cannot leave the copy one save behind.
        try:
            if source_name:
                probe, _resolved, taken, source_path = await asyncio.to_thread(
                    _load_template_specs,
                    agents_dir,
                    source_name.strip(),
                    "api_agent_template_create",
                )
                if probe is None or source_path is None:
                    return web.json_response(
                        {
                            "error": f"Template '{source_name}' not found",
                            "code": "template_not_found",
                        },
                        status=404,
                    )
            else:
                _none, _resolved, taken, _path = await asyncio.to_thread(
                    _load_template_specs, agents_dir, name, "api_agent_template_create"
                )
        except _AmbiguousTemplateName as exc:
            return web.json_response(
                {
                    "error": f"'{exc}' matches more than one template file; rename one first.",
                    "code": "ambiguous_template_name",
                },
                status=409,
            )
        # A member of this name would make the new template unreachable by bare
        # name (name-first resolution answers the member) -- refused, like publish.
        if name.lower() in taken:
            return web.json_response(
                {"error": f"A template named '{name}' already exists", "code": "name_taken"},
                status=409,
            )

        def _create() -> Path:
            created: list[Path] = []

            def _bound(cfg_data: dict) -> bool:
                # A binding to this name -- as a crew's ``kiro_agent`` or the
                # legacy fallback -- would make the new file resolve for that
                # dangling reference the moment it lands; a crew NAMED this
                # would shadow the template by bare name.
                return name.lower() in _reserved_binding_names(cfg_data) or name in (
                    cfg_data.get("agents") or {}
                )

            def _check_base_then_overlay(cfg_data: dict) -> None:
                if _bound(cfg_data):
                    raise _NameBound()

                def _check_overlay_then_write(local_data: dict) -> None:
                    # ``config.local.json`` deep-merges over the base and can
                    # carry a crew's effective ``kiro_agent`` on its own; it
                    # has its own sidecar lock, held here nested so neither
                    # layer's binding can land between the check and the write.
                    if _bound(local_data):
                        raise _NameBound()
                    with agents_spec_lock(agents_dir):
                        # Re-scanned under the lock by stem AND declared name:
                        # a package install landing ``Pkg-foo.json`` declaring
                        # ``foo`` after the pre-lock probe would otherwise let
                        # ``foo.json`` be written into an ambiguous name.
                        if _spec_stem_on_disk(agents_dir, name) or _find_infos(name):
                            raise FileExistsError(name)
                        source: dict[str, Any] | None = None
                        if source_path is not None:
                            # The source is re-resolved HERE, where it decides:
                            # the pre-lock probe chose a path, but a package
                            # install or rename landing after it can leave that
                            # path stale or make the name ambiguous, and the
                            # copy would carry the wrong definition. Exactly one
                            # file may reach the source name, and it must be the
                            # one the probe chose.
                            claimants = {
                                (agents_dir / info.filename).resolve()
                                for info in _find_infos(source_key)
                            }
                            if not claimants:
                                raise FileNotFoundError(source_path)
                            if claimants != {source_path.resolve()}:
                                raise _AmbiguousOnDisk(source_name)
                            fresh = _read_agent_spec(
                                source_path,
                                operation="api_agent_template_create",
                                source="dashboard",
                            )
                            if not isinstance(fresh, dict):
                                raise FileNotFoundError(source_path)
                            source = fresh
                        data = dict(source) if source else _blank_spec(name, description)
                        data["name"] = name
                        if description or not source:
                            data["description"] = description
                        dest = agents_dir / f"{name}.json"
                        agent_state.prune(name)
                        agent_state.lift_and_strip_bookkeeping(data, name)
                        _write_spec_file(dest, data)
                        created.append(dest)

                update_config_locked(config_local_path(), mutate=_check_overlay_then_write)

            update_config_locked(mutate=_check_base_then_overlay)
            return created[0]

        try:
            # drained: a client that disconnects mid-write must not leave the
            # spec written while the cache refresh and audit below are skipped.
            dest = await drained_to_thread(_create)
        except _NameBound:
            return web.json_response(
                {"error": f"A crew is bound to the name '{name}'", "code": "name_bound"},
                status=409,
            )
        except FileExistsError:
            return web.json_response(
                {"error": f"A template named '{name}' already exists", "code": "name_taken"},
                status=409,
            )
        except FileNotFoundError:
            # The source vanished between the pre-lock probe and the locked read.
            return web.json_response(
                {"error": f"Template '{source_name}' not found", "code": "template_not_found"},
                status=404,
            )
        except _AmbiguousOnDisk:
            # A second file reached the source name after the probe.
            return web.json_response(
                {
                    "error": f"'{source_name}' matches more than one template file; rename one first.",
                    "code": "ambiguous_template_name",
                },
                status=409,
            )
        except Exception:
            logger.exception("template create failed for %r", name)
            return web.json_response(
                {"error": "Could not write the template", "code": "template_write_failed"},
                status=500,
            )
    clear_list_agents_cache()
    # The dispatch snapshot (``_materialized_kiro_agent``) is what lets "Chat
    # with this template" resolve the new name on the very next turn: publish it
    # now (a pure set union, loop-safe) so no slot created in the window before
    # the rescan lands is normalized -- and durably stored -- onto the default
    # agent, then let the off-loop rescan bring the authoritative view.
    publish_materialized_agents([name])
    schedule_materialized_agents_refresh()
    state.push_refresh("agents")
    # The owner gate logs only its denials; the successful write of a machine-
    # global spec gets its own operation-labelled line beside the middleware's
    # request-level one.
    sel().log_api_access(
        caller="dashboard",
        operation="agent_templates.create",
        outcome="ok",
        resources=f"template:{name}" + (f" from:{source_name.strip()}" if source_name else ""),
    )
    return web.json_response({"ok": True, "name": name, "filename": dest.name}, status=201)


class _AmbiguousOnDisk(Exception):
    """Under the spec lock, the name resolves to something other than exactly the
    file the pre-lock probe chose -- a second claimant is present, or the file
    is not the one the probe saw."""


class _ReadOnlyOnDisk(Exception):
    """The file a delete resolved to is, by its OWN contents, one the tab may not
    unlink -- carries the read-only reason the roster would show for it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _NameBound(Exception):
    """A crew binding already resolves the requested template name."""


async def api_agent_template_delete(request: web.Request) -> web.Response:
    """DELETE /api/agents/detail/{name} -- delete a template the user owns.

    Refused with ``409 template_read_only`` for a package, runtime, markdown or
    private-copy spec, and with ``409 template_referenced`` (carrying the list)
    while a crew, the default agent, a schedule, a chat folder, a webhook or a
    private copy still names it: deleting underneath them would leave sessions
    that cannot start, or that silently start on the default agent.

    The reference check and the unlink are ONE critical section, off the loop,
    under every lock the reference stores' writers take -- the shape
    ``_unlink_copy_unless_referenced`` in ``handlers.agents`` established:

    * the folder store lock (``state.hold_folders``), held across the whole
      section, so no folder pin can commit between the check and the unlink;
    * the ``config.json`` advisory lock and, nested, the ``config.local.json``
      overlay's own lock (``update_config_locked`` on each, writing nothing) --
      cross-process, so a CLI ``config set`` or another gateway is excluded,
      not just this loop's handlers; crew bindings, the default agent and the
      fork sidecar (whose writers run inside the same config hold) are read
      under them;
    * the spec lock around the unlink itself;
    * innermost, the schedule store's own cross-process lock
      (``cron.cron_store_lock``, the one the scheduler's mutators take), held
      from the dispatch-reference walk through the rename.

    Webhook tokens are read inside that section AND their writers commit under
    the same spec lock (``hooks._commit_pinned_token`` re-verifies the agent
    there), so a pin cannot land against a file this delete is removing.
    Schedules are pinned by the store lock, so one cannot be saved against the
    template between the walk and the rename; the post-rename re-read below is
    for a writer that bypasses that lock (a hand edit). A present-but-unreadable
    ``crons.json`` fails the delete CLOSED (``503 schedule_store_unreadable``);
    a store lock another holder keeps past the bounded wait is
    ``503 schedule_store_busy``, nothing unlinked.
    """
    # circular import: see api_agent_template_create.
    from kiro_crew.dashboard.handlers.agents import (
        _get_config_lock,
        _require_owner,
        _roster_mask,
    )

    name = request.match_info["name"]
    denied = await _require_owner(request, "agent_templates.delete")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    async with _get_config_lock():
        matches = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), _find_infos, name
        )
        if not matches:
            return web.json_response(
                {"error": f"Template '{name}' not found", "code": "template_not_found"}, status=404
            )
        if len(matches) > 1:
            # A name that reaches two files (one by declared name, one by stem)
            # names no single template to unlink; the user renames one first,
            # as create and the definition PATCH already insist.
            return web.json_response(
                {
                    "error": f"'{name}' matches more than one template file; rename one first.",
                    "code": "ambiguous_template_name",
                },
                status=409,
            )
        found: AgentInfo = matches[0]
        # The row's filename is a discovery string, not a path: only a plain
        # basename resolving to a regular file directly under the agents
        # directory is a template this handler may unlink.
        spec_path = _spec_file_beneath(kiro_agents_dir_path(), found.filename)
        if spec_path is None:
            return web.json_response(
                {"error": f"Template '{name}' not found", "code": "template_not_found"}, status=404
            )
        reason = template_read_only_reason(found)
        if reason is not None:
            return web.json_response(
                {
                    "error": f"Template '{name}' is read-only ({reason})",
                    "code": "template_read_only",
                    "reason": reason,
                },
                status=409,
            )
        try:
            for identity in dict.fromkeys((Path(found.filename).stem, found.name)):
                await asyncio.to_thread(require_unmanaged_template, identity)
        except CapabilityError as exc:
            return web.json_response({"error": exc.code, "code": exc.code}, status=exc.status)

        aliases = {name, found.name, Path(found.filename).stem}

        # Schedules and webhook tokens whose writers landed a pin while the
        # guard ran (their stores are not under these locks -- see the module
        # docstring). Filled after the unlink, reported by the caller.
        late: list[dict[str, str]] = []
        # Where the spec went: the tombstone's filename, for the audit row.
        tombstone: list[str] = [""]

        def _guard_then_unlink(pins: list[FolderPin]) -> list[dict[str, str]]:
            """Thread-side, with the folder lock held by the caller."""
            refs: list[dict[str, str]] = []

            def _under_base(_cfg_data: dict) -> None:
                def _under_overlay(_local_data: dict) -> None:
                    agents_dir = kiro_agents_dir_path()
                    with agents_spec_lock(agents_dir):
                        # Re-validated under the lock: the file must still be the
                        # one beneath agents_dir the pre-lock check accepted.
                        if _spec_file_beneath(agents_dir, found.filename) is None:
                            raise FileNotFoundError(found.filename)
                        # The row paired a name with a filename, and both are
                        # discovery strings -- an edition catalog row may pair
                        # them freely. The FILE decides: it is re-read here and
                        # classified from its own contents, the way discovery
                        # and the definition PATCH classify a file, so the
                        # requested name must be one this file answers to (its
                        # declared name or its stem) and the file itself must be
                        # deletable from this tab. A row saying ``rogue`` over
                        # ``victim.json`` unlinks nothing.
                        fresh = _read_agent_spec(
                            spec_path,
                            operation="api_agent_template_delete",
                            source="dashboard",
                        )
                        if not isinstance(fresh, dict):
                            raise FileNotFoundError(found.filename)
                        on_disk = _global_agent_info(spec_path, fresh)
                        if name not in (on_disk.name, spec_path.stem):
                            raise FileNotFoundError(found.filename)
                        # The pre-lock ambiguity check is re-run HERE, where it
                        # decides: a package install landing a second file that
                        # reaches this name after the probe would otherwise let
                        # the stale single match unlink the user's own file.
                        # Exactly one file may reach the name, and it must be
                        # the one about to go.
                        claimants = {
                            (agents_dir / info.filename).resolve() for info in _find_infos(name)
                        }
                        if claimants != {spec_path.resolve()}:
                            raise _AmbiguousOnDisk(name)
                        forks = agent_state.all_fork_info()
                        fork = forks.get(on_disk.name)
                        if fork:
                            on_disk.private_to = str(fork.get("private_to", ""))
                        reason = template_read_only_reason(on_disk)
                        if reason is not None:
                            raise _ReadOnlyOnDisk(reason)
                        # Both config layers pinned: read the EFFECTIVE config
                        # (base with the overlay merged) and the sidecar that
                        # its writers update inside this same hold. The file's
                        # own declared name joins the aliases the row supplied.
                        # The schedule store is pinned too: its own cross-process
                        # lock is held from the walk that finds no dispatching
                        # job through the rename, so a schedule cannot be saved
                        # against this template between the two (the scheduler's
                        # writers take the same lock; contention is bounded and
                        # surfaces as CronStoreBusy -> 503, file intact).
                        with cron_store_lock(config_dir()):
                            refs.extend(
                                template_references(
                                    aliases | {on_disk.name},
                                    KiroCrewConfig.load(),
                                    forks,
                                    _cron_agent_ids(),
                                    pins,
                                    _webhook_agent_ids(),
                                )
                            )
                            if refs:
                                return None
                            tombstone[0] = _tombstone(spec_path)
                        with contextlib.suppress(Exception):
                            agent_state.prune(on_disk.name)
                        # Everything past this line OBSERVES a delete that has
                        # happened; nothing here may turn into a refusal, or
                        # the client hears "failed" about a file that is gone.
                        # The schedule store was pinned by its own lock across
                        # the walk and the rename, so only a writer that bypasses
                        # that lock (a hand edit) can have landed a pin; it is
                        # re-read once the file is gone so such a pin is named
                        # rather than first failing at its next fire. Webhook
                        # pins commit under this same spec lock, so none can land
                        # here; the re-read keeps them in the same report.
                        try:
                            late.extend(
                                ref
                                for ref in template_references(
                                    aliases | {on_disk.name},
                                    KiroCrewConfig.load(),
                                    forks,
                                    _cron_agent_ids(),
                                    [],
                                    _webhook_agent_ids(),
                                )
                                if ref["kind"] in ("schedule", "webhook")
                            )
                        except Exception:
                            logger.warning(
                                "template %r deleted; the post-delete reference check "
                                "could not read a store, so a schedule or webhook pinned "
                                "in the window is not reported",
                                name,
                                exc_info=True,
                            )
                    return None

                update_config_locked(config_local_path(), mutate=_under_overlay)
                return None

            update_config_locked(mutate=_under_base)
            return refs

        async def _with_folders_held(folders: list[dict[str, Any]]) -> list[dict[str, str]]:
            # drained: the unlink must never commit while the folder hold, the
            # cache refresh, the dangling-reference report and the audit row
            # are abandoned by a cancelled request.
            return await drained_to_thread(_guard_then_unlink, _folder_pins_of(folders))

        try:
            refs = await state.hold_folders(_with_folders_held)
        except FileNotFoundError:
            return web.json_response(
                {"error": f"Template '{name}' not found", "code": "template_not_found"}, status=404
            )
        except _ReadOnlyOnDisk as exc:
            return web.json_response(
                {
                    "error": f"Template '{name}' is read-only ({exc.reason})",
                    "code": "template_read_only",
                    "reason": exc.reason,
                },
                status=409,
            )
        except _AmbiguousOnDisk:
            return web.json_response(
                {
                    "error": f"'{name}' matches more than one template file; rename one first.",
                    "code": "ambiguous_template_name",
                },
                status=409,
            )
        except CronStoreUnreadable:
            # Fail CLOSED: a present-but-unreadable crons.json is not "no
            # schedules", and a repaired store brings its jobs back naming
            # whatever they named. Nothing was unlinked.
            return web.json_response(
                {
                    "error": "The schedule store could not be read; repair crons.json "
                    "before deleting a template.",
                    "code": "schedule_store_unreadable",
                },
                status=503,
            )
        except CronStoreBusy:
            # The scheduler held its store lock past the bounded wait: nothing
            # was unlinked, and the guard cannot claim "no schedule dispatches
            # this" without the store pinned. Retryable.
            return web.json_response(
                {
                    "error": "The schedule store is busy; try deleting the template again.",
                    "code": "schedule_store_busy",
                },
                status=503,
            )
        except Exception:
            logger.exception("template delete failed for %r", name)
            return web.json_response(
                {"error": "Could not delete the template", "code": "template_delete_failed"},
                status=500,
            )
        if refs:
            return web.json_response(
                {
                    "error": f"Cannot delete '{name}': still referenced",
                    "code": "template_referenced",
                    # Masked like the roster's ``used_by``: the labels are crew,
                    # folder, schedule and token records the agent can write.
                    "references": _masked_refs(refs, _roster_mask),
                },
                status=409,
            )
    clear_list_agents_cache()
    # Removal has no publish shortcut: the snapshot is replaced by a rescan, run
    # off the loop and AWAITED here, so the deleted name cannot be bound to a
    # new slot -- and stored there -- in the window a deferred refresh would
    # leave open. Never raises (the scan swallows its own errors).
    await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), refresh_materialized_agents
    )
    state.push_refresh("agents")
    sel().log_api_access(
        caller="dashboard",
        operation="agent_templates.delete",
        outcome="ok",
        resources=f"template:{found.name} file:{found.filename} tombstone:{tombstone[0]}",
    )
    if late:
        # Loud where the operator can see it, and in the audit log: the holder
        # is named, so the schedule or token can be repointed before it fires.
        holders = ", ".join(f"{ref['kind']}:{ref['id']}" for ref in late)
        logger.warning(
            "template %r was deleted while a schedule or webhook still names it: %s",
            name,
            holders,
        )
        sel().log_api_access(
            caller="dashboard",
            operation="agent_templates.delete",
            outcome="dangling_reference",
            resources=f"template:{found.name} holders:{holders}",
        )
    return web.json_response({"ok": True})


def validate_definition_patch(patch_body: dict[str, Any]) -> str | None:
    """Shape-check the definition keys of a detail PATCH; the error text or None."""
    for key in ("description", "prompt"):
        if key in patch_body:
            value = patch_body[key]
            cap = MAX_TEMPLATE_PROMPT_CHARS if key == "prompt" else MAX_TEMPLATE_DESCRIPTION_CHARS
            if not isinstance(value, str):
                return f"{key} must be a string"
            if len(value) > cap:
                return f"{key} is longer than {cap} characters"
    for key in ("tools", "allowedTools"):
        if key in patch_body:
            value = patch_body[key]
            if not isinstance(value, list) or not all(isinstance(t, str) and t for t in value):
                return f"{key} must be a list of non-empty strings"
            if len(value) > MAX_TEMPLATE_TOOLS:
                return f"at most {MAX_TEMPLATE_TOOLS} entries in {key}"
            if any(len(t) > MAX_TEMPLATE_TOOL_CHARS for t in value):
                return f"each {key} entry must be at most {MAX_TEMPLATE_TOOL_CHARS} characters"
    return None


def apply_definition_patch(data: dict[str, Any], patch_body: dict[str, Any]) -> None:
    """Write the validated definition keys into *data* (the spec about to be saved)."""
    for key in TEMPLATE_DEFINITION_KEYS:
        if key in patch_body:
            data[key] = patch_body[key]


def read_only_reason_for_path(path: Path) -> str | None:
    """The templates-tab read-only rule for THE spec file the PATCH is about to write.

    Classified from the file itself -- its name and its declared ``name`` -- the
    way discovery classifies every row, never by looking the declared name up in
    the deduplicated roster: two files can declare one name (``atlas.json``
    beside ``SomePkg-atlas.json``), and the roster keeps only the package twin,
    so a lookup would answer for the wrong file and let the package copy through
    as if it were the plain one.
    """
    if path.name in OWNED_KIRO_AGENT_FILES:
        return _READ_ONLY_RUNTIME
    if is_markdown_spec(path):
        return _READ_ONLY_MARKDOWN
    spec = _read_agent_spec(path, operation="api_agent_detail", source="dashboard")
    info = _global_agent_info(path, spec if isinstance(spec, dict) else {})
    fork = agent_state.all_fork_info().get(info.name)
    if fork:
        info.private_to = str(fork.get("private_to", ""))
    return template_read_only_reason(info)
