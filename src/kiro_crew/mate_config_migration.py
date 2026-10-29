"""Startup migration: move every EXISTING crewmate onto its worker and its own file.

RFC *mate-owned-agent-config* §6; the steps are in ``crew-mode.md``, "Existing
crewmates move at startup". Per crewmate whose ``kiro_agent`` does not yet name
its own file: read and govern the agent it runs on, build the file from what that
agent adds to the worker base (:func:`kiro_crew.mate_worker.worker_additions`),
dry-run the merge (:func:`plan_move`: nothing gained and no limit loosened; a
grant that would be gained is withheld for the capability card, a loss moves
anyway and is named in the crewmate's one notice; an unreadable agent moves onto
the default template), write and re-check the file and its kit record, then,
once every candidate is proven,
repoint each binding under the create path's optimistic lock and move its
pre-upgrade threads (:func:`repoint_moved_threads`).

Everything carried over is the crewmate's file content, so the worker's catalog
reports it as already there and no capability card is ever raised for it
(``mate_capability_catalog.worker_has``).

Nothing is deleted, so pointing ``kiro_agent`` back rolls a crewmate's new chats
back; threads already moved stay on the conductor (``repointed`` is a record, not
an undo). ``~/.kiro/mates/migrated.json`` keeps it from moving again. Idempotent, best
effort per crewmate, never raises, and never gates readiness. The user-level
agents dir and the mate dir come from their own helpers
(:func:`kiro_crew.config.paths.kiro_agents_dir`, ``mate_agents_dir``), which an
isolated instance can resolve to different roots.
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
    #: Kept for callers that read it; an unreadable agent now moves onto the
    #: default template (``moved_from_default``), so nothing lands here.
    skipped_no_source: list[str] = dataclasses.field(default_factory=list)
    #: Moved although its worker does not grant exactly what its old agent did:
    #: per crewmate, the sentence its one notice names (what it lost, what was withheld).
    moved_with_changes: dict[str, str] = dataclasses.field(default_factory=dict)
    #: Moved onto the default template because the agent it ran on was unreadable.
    moved_from_default: list[str] = dataclasses.field(default_factory=list)
    #: Per crewmate, the pre-upgrade DM threads moved onto its conductor.
    threads_repointed: dict[str, list[str]] = dataclasses.field(default_factory=dict)


def _ledger_path():
    return paths_mod.mate_agents_dir().parent / "migrated.json"


class LedgerUnreadable(Exception):
    """``migrated.json`` exists but cannot be read as the ledger."""


def _read_ledger() -> dict[str, Any]:
    """``migrated.json`` as written, ``{}`` when it is absent.

    Raises :class:`LedgerUnreadable` when it exists but cannot be read or is not
    an object: read as empty, it would move every rolled-back crewmate again and
    the next write would drop the thread passes it still owes.
    """
    try:
        data = json.loads(_ledger_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise LedgerUnreadable(f"{_ledger_path()} is unreadable") from exc
    if not isinstance(data, dict):
        raise LedgerUnreadable(f"{_ledger_path()} is not an object")
    return data


def _write_ledger(data: dict[str, Any]) -> None:
    from kiro_crew.atomic_write import atomic_write

    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(data, indent=2) + "\n", restrict_to_owner=True)


def _names(data: dict[str, Any]) -> set[str]:
    names = data.get("migrated")
    return {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()


def _member_id(name: str) -> str:
    """The immutable ``member_id`` of crewmate *name*, ``""`` when unreadable."""
    try:
        record = KiroCrewConfig.load().agents.get(name)
    except Exception:  # noqa: BLE001 - an unreadable config names no identity
        return ""
    return str(getattr(record, "member_id", "") or "") if record is not None else ""


def _moved_ids(data: dict[str, Any]) -> dict[str, str]:
    ids = data.get("migrated_ids")
    if not isinstance(ids, dict):
        return {}
    return {k: v for k, v in ids.items() if isinstance(k, str) and isinstance(v, str)}


def _already_moved(data: dict[str, Any], name: str, member_id: str) -> bool:
    """Whether the crewmate *name* with *member_id* was moved before.

    A name moved under another ``member_id`` was deleted and made again, so the
    new crewmate is not moved yet. A name recorded with no id (a ledger from
    before the ids) still counts as moved.
    """
    if name not in _names(data):
        return False
    recorded = _moved_ids(data).get(name)
    return recorded is None or recorded == member_id


def _record_migrated(name: str, source: str = "", *, moved: bool = True, reason: str = "") -> None:
    """Record *name* as moved (*moved*); with *source*, owe a repoint of its old threads.

    *source* is the agent the crewmate ran on before the move. Its entry under
    ``threads`` names it, so :func:`repoint_moved_threads` knows which records are
    the crewmate's pre-upgrade chats. Written with ``moved=False`` before the
    repoint (the intent), so a crash after the repoint still owes the threads.
    *reason* is how it moved, kept under ``reasons``.
    """
    data = _read_ledger()
    member_id = _member_id(name)
    if moved:
        data["migrated"] = sorted(_names(data) | {name})
        ids = _moved_ids(data)
        if member_id:
            ids[name] = member_id
        else:
            ids.pop(name, None)
        data["migrated_ids"] = dict(sorted(ids.items()))
        if reason:
            reasons = data.get("reasons")
            reasons = reasons if isinstance(reasons, dict) else {}
            reasons[name] = reason
            data["reasons"] = dict(sorted(reasons.items()))
    if source:
        threads = data.get("threads")
        threads = threads if isinstance(threads, dict) else {}
        entry = {"from": source, "repointed": [], "done": False}
        if member_id:
            entry["member_id"] = member_id
        old = threads.get(name)
        if not (isinstance(old, dict) and old.get("member_id") == member_id and member_id):
            # A new crewmate (or one made again under the same key) starts its own
            # entry; only the same crewmate's own entry, already in progress, stays.
            threads[name] = entry
        data["threads"] = threads
    _write_ledger(data)


def _dm_thread_keys(slugs: set[str]) -> list[str]:
    """The session keys of every DM thread transcript whose slot names one of *slugs*.

    Read from file names only; returned as ``dashboard:<slot key>``.
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

    Only a persistent ``member`` record carrying *member_id* and running *source*
    moves, and only its ``template_id`` changes, by compare-and-set
    (``bind_session_execution``), so memory routing stays. Returns the keys moved
    and whether every thread was read and written (False leaves the pass owed).
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

    An old thread's own execution record names its old agent, so without this
    it keeps running there. Owed per ``migrated.json`` entry with a ``from`` and
    no ``done``; a rolled-back or removed crewmate is left alone. Moved keys go
    under ``repointed`` and ``done`` is set once every thread was read.
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

    Unreadable answers True, so the crewmate stays where it is.
    """
    from kiro_crew.agent import agent_spec_path

    try:
        return agent_spec_path(name, agents_dir=paths_mod.kiro_agents_dir()) is not None
    except (OSError, ValueError):
        return True


def _candidates(cfg: KiroCrewConfig) -> dict[str, str]:
    """Crewmates whose ``kiro_agent`` does not resolve to their own file yet.

    ``{member_name: source_agent}``, leaving out ``default``, bindings with no
    ``kiro_agent`` or ``member_id``, ones already on their own file, and moved ones.
    """
    out: dict[str, str] = {}
    ledger = _read_ledger()
    for name, binding in cfg.agents.items():
        if name == "default":
            continue
        member_id = str(getattr(binding, "member_id", "") or "").strip()
        if not member_id:
            logger.debug("crewmate migration: %r has no member id; not a crewmate", name)
            continue
        if _already_moved(ledger, name, member_id):
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

    A base-declared server counts by name; others with their transport. An
    approval for a tool the spec excludes grants nothing.
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

    The base's own ``excludedTools`` apply to every worker by design, so losing an
    approval for one of them is not a loss.
    """
    return set(_strings(base.get("excludedTools")))


def _ref_server(ref: str) -> str:
    """The server an ``@server`` / ``@server/tool`` reference names, ``""`` for a built-in."""
    return ref[1:].split("/", 1)[0] if ref.startswith("@") else ""


def _base_owned_servers(base: dict[str, Any]) -> set[str]:
    """The servers whose grants the worker base decides: its own and the platform's."""
    from kiro_crew.mate_worker import conductor_mcp_servers

    servers = set(_servers(base.get("mcpServers")))
    try:
        servers |= set(conductor_mcp_servers())
    except Exception:  # noqa: BLE001 - the base's own servers still answer
        logger.debug("crewmate migration: platform server list unreadable", exc_info=True)
    return servers


def _by_design(grant: tuple[str, ...], owned: set[str], withheld: set[str]) -> bool:
    """Whether *grant* is one the worker leaves out by design, not a loss.

    A tool the base excludes, or an approval or mount on a built-in or a base-owned
    server. Blocking.
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


@dataclasses.dataclass
class Differences:
    """How the worker merged from a crewmate file differs from its old agent.

    Two kinds. A *loss* (a grant the old agent had that the worker does not, or
    its prompt) leaves the worker able to do less: the crewmate still moves and
    its user is told once (owner decision, ``docs/decisions``). A *loosening* (a grant
    gained, a switched-off server started, a hook or a ``permissions`` /
    ``toolsSettings`` restriction not kept) would let the worker do more than the
    old agent was allowed to: the crewmate never moves with one.
    """

    lost: set[tuple[str, ...]] = dataclasses.field(default_factory=set)
    gained: set[tuple[str, ...]] = dataclasses.field(default_factory=set)
    relaunched: set[str] = dataclasses.field(default_factory=set)
    prompt_lost: bool = False
    hooks_lost: bool = False
    limits_lost: set[str] = dataclasses.field(default_factory=set)

    def loosening(self) -> str:
        """Why the worker would be allowed more than the old agent, else ``""``."""
        problems = []
        if self.gained:
            problems.append(_describe("its worker would gain", self.gained))
        if self.relaunched:
            problems.append(
                _describe("its worker would start", {("server", n) for n in self.relaunched})
                + " it had switched off"
            )
        if self.hooks_lost:
            problems.append("its hooks would not run on the worker")
        if self.limits_lost:
            problems.append(
                "its worker would not keep its " + " and ".join(sorted(self.limits_lost))
            )
        return "; ".join(problems)

    def losses(self) -> str:
        """What the old agent had that the worker lacks, for the user's notice, else ``""``."""
        parts = []
        if self.lost:
            parts.append(_describe("it no longer has", self.lost))
        if self.prompt_lost:
            parts.append("its old instructions were not carried over")
        return "; ".join(parts)

    def text(self) -> str:
        """Every difference, in the order :func:`dry_run` has always named them."""
        problems = []
        if self.lost:
            problems.append(_describe("its worker would lose", self.lost))
        if self.gained:
            problems.append(_describe("its worker would gain", self.gained))
        if self.relaunched:
            problems.append(
                _describe("its worker would start", {("server", n) for n in self.relaunched})
                + " it had switched off"
            )
        if self.prompt_lost:
            problems.append("its worker would not carry its prompt")
        if self.hooks_lost:
            problems.append("its hooks would not run on the worker")
        if self.limits_lost:
            problems.append(
                "its worker would not keep its " + " and ".join(sorted(self.limits_lost))
            )
        return "; ".join(problems)


def differences(
    base: dict[str, Any], source: dict[str, Any], mate_file: dict[str, Any]
) -> Differences:
    """How the worker merged from *mate_file* over *base* differs from *source*.

    It should keep every grant *source* has beyond the base and add none (what
    the base withholds by design, :func:`_by_design`, is no loss), carry the old
    prompt, run every hook and keep every restriction.
    """
    additions = worker_additions(base, mate_file)
    merged = merge_worker_spec(base, additions)
    base_servers = set(_servers(base.get("mcpServers")))
    have_base = _grants(base, base_servers)
    have_source = _grants(source, base_servers)
    have_merged = _grants(merged, base_servers)
    withheld = _worker_withheld(base)
    owned = _base_owned_servers(base)
    out = Differences()
    out.lost = {
        t for t in (have_source - have_base) - have_merged if not _by_design(t, owned, withheld)
    }
    out.gained = have_merged - (have_base | have_source)
    # A server the old agent switched off stays off: the worker base declaring it
    # is no licence to launch it. Base-owned or not, this is a restriction to keep.
    merged_servers = _servers(merged.get("mcpServers"))
    out.relaunched = {
        name
        for name, entry in _servers(source.get("mcpServers")).items()
        if mcp_entry_is_muted(entry)
        and name in merged_servers
        and not mcp_entry_is_muted(merged_servers[name])
    }
    old_prompt, base_prompt, new_prompt = (
        p if isinstance(p, str) else ""
        for p in (source.get("prompt"), base.get("prompt"), merged.get("prompt"))
    )
    if old_prompt.strip() and old_prompt.strip() != base_prompt.strip():
        out.prompt_lost = old_prompt.strip() not in new_prompt
    out.hooks_lost = bool(_hook_entries(source) - _hook_entries(base))
    # The worker merge carries no ``permissions`` or ``toolsSettings`` from the
    # file, so an agent with a restriction there (a deny or ask rule, an
    # ``exclude``, a ``toolsSettings`` entry such as ``fs_write.deniedPaths``) the
    # worker would not have is not moved: the restriction would be lost.
    out.limits_lost = {k for k, _ in _restrictions(source) - _restrictions(merged)}
    return out


def dry_run(base: dict[str, Any], source: dict[str, Any], mate_file: dict[str, Any]) -> str:
    """Why the merged worker would not grant exactly its old capabilities, else ``""``.

    Every difference :func:`differences` finds, as one sentence for a reader.
    """
    return differences(base, source, mate_file).text()


def _without(spec: dict[str, Any], key: str, drop: Any) -> None:
    """Drop from list *key* of *spec* every item *drop* answers True for, if *key* is there."""
    if key in spec:
        spec[key] = [item for item in _strings(spec.get(key)) if not drop(item)]


def _strip_grants(spec: dict[str, Any], tokens: set[tuple[str, ...]]) -> dict[str, Any]:
    """*spec* with every grant in *tokens* taken out of it; *spec* is not changed.

    The planned file of a crewmate whose worker would gain something its old
    agent never had: the gain is not written, so nothing is granted silently.
    The user asks for it, if they want it, on the capability card.
    """
    out = copy_json_document(spec)
    servers = out.get("mcpServers")
    servers = servers if isinstance(servers, dict) else {}
    for token in tokens:
        kind, ref = token[0], token[1]
        if kind in ("server", "transport"):
            if isinstance(servers.get(ref), dict) and not mcp_entry_is_muted(servers[ref]):
                servers.pop(ref)
            for key in ("tools", "allowedTools"):
                _without(out, key, lambda r, s=ref: _ref_server(r) == s)
        elif kind == "approve":
            server, _, tool = ref[1:].partition("/")
            entry = servers.get(server)
            if isinstance(entry, dict) and "autoApprove" in entry:
                _without(entry, "autoApprove", lambda t, x=tool: t == x)
            _without(out, "allowedTools", lambda r, x=ref: r == x)
        elif kind == "allow":
            _without(out, "allowedTools", lambda r, x=ref: r == x)
        elif kind == "tool":
            _without(out, "tools", lambda r, x=ref: r == x)
        elif kind in ("resources", "skills"):
            _without(out, kind, lambda r, x=ref: r == x)
    return out


def plan_move(
    name: str, base: dict[str, Any], source: dict[str, Any]
) -> tuple[dict[str, Any], Differences, set[tuple[str, ...]]]:
    """The file crewmate *name* gets, how its worker then differs, and what was withheld.

    :func:`migration_file`, then made never to loosen: a server the old agent
    switched off is written switched off, and a grant the worker would gain is
    taken out (returned as *withheld*, for the user to ask for on a card). What
    is left in :meth:`Differences.loosening` keeps the crewmate where it is.
    """
    planned = migration_file(name, base, source)
    diff = differences(base, source, planned)
    if diff.relaunched:
        servers = planned.setdefault("mcpServers", {})
        for server in diff.relaunched:
            servers[server] = {"disabled": True}
        diff = differences(base, source, planned)
    withheld = set(diff.gained)
    if withheld:
        planned = _strip_grants(planned, withheld)
        diff = differences(base, source, planned)
        withheld -= diff.gained
    return planned, diff, withheld


def change_notice(diff: Differences, withheld: set[tuple[str, ...]]) -> str:
    """The sentence a moved crewmate's notice carries on what changed, ``""`` when nothing."""
    parts = [diff.losses()] if diff.losses() else []
    if withheld:
        parts.append(_describe("it was not given", withheld))
    return "; ".join(parts)


def uncarried_restrictions(source: dict[str, Any]) -> list[str]:
    """The sections of *source*'s restrictions its crewmate's worker would not keep.

    ``permissions``, ``toolsSettings`` and ``hooks`` are not carried, so the create
    path and the kit copy refuse them, as :func:`dry_run` does. Blocking.
    """
    try:
        base = _worker_base()
    except Exception:  # noqa: BLE001 - fail closed: nothing is carried
        logger.warning("worker base unreadable; refusing any template restriction")
        merged: dict[str, Any] = {}
    else:
        merged = merge_worker_spec(base, worker_additions(base, source))
    lost = {key for key, _ in _restrictions(source) - _restrictions(merged)}
    if _hook_entries(source) - _hook_entries(merged):
        lost.add("hooks")
    return sorted(lost)


def _restrictions(spec: dict[str, Any]) -> set[tuple[str, str]]:
    """Every restriction *spec* states in ``permissions`` or ``toolsSettings``, as keys.

    A permission rule that only allows (``effect: allow`` and no ``exclude``) is a
    grant, compared by the grant checks above, not a restriction.
    """
    out: set[tuple[str, str]] = set()
    perms = spec.get("permissions")
    rules = perms.get("rules") if isinstance(perms, dict) else None
    for rule in rules if isinstance(rules, list) else []:
        if isinstance(rule, dict) and (rule.get("effect") != "allow" or rule.get("exclude")):
            out.add(("permissions", json.dumps(rule, sort_keys=True)))
    settings = spec.get("toolsSettings")
    if isinstance(settings, dict) and settings:
        out.add(("toolsSettings", json.dumps(settings, sort_keys=True)))
    return out


def migration_file(name: str, base: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """The file crewmate *name* gets: what *source* adds to the worker *base*.

    Only additions and restrictions; a limit on a base server is kept as an
    ``excludedTools`` entry, a base server switched off as ``{"disabled": true}``.
    """
    adds = worker_additions(base, source)
    spec: dict[str, Any] = {"name": name}
    if adds["prompt"]:
        spec["prompt"] = adds["prompt"]
    servers = copy_json_document(adds["mcpServers"])
    spec["mcpServers"] = servers
    spec["tools"] = list(adds["tools"])
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

    The same additions a default-template crewmate gets, through the same
    dry-run; an unreadable template or a failed dry-run gives the empty file.
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


def _tool_offs(spec: dict[str, Any]) -> set[str]:
    """Every tool *spec* takes out: its ``excludedTools`` and its servers' ``disabledTools``."""
    return set(_strings(spec.get("excludedTools"))) | set(
        disabled_tool_refs(spec.get("mcpServers"))
    )


def _fallback_keeping_limits(
    source_agent: str, base: dict[str, Any], fallback: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """The default-template *fallback* made to keep *source_agent*'s readable limits.

    For an agent :func:`_governed_source` refused. One whose spec does not parse
    states no limit, so *fallback* is returned as it is. One that parses (a
    ``file://`` prompt that does not resolve, say) is read raw, and the fallback
    never loosens it: a server it switched off is written switched off, a tool it
    takes out is written into ``excludedTools``, and a grant it never had is
    taken out. Returns the file and why it would still loosen (a hook or a
    ``permissions`` / ``toolsSettings`` restriction it cannot carry), else ``""``.
    """
    try:
        raw = agent_specs.load_agent_spec(source_agents_dir(source_agent), source_agent)
    except (agent_specs.AgentSpecUnreadable, OSError):
        return fallback, ""
    raw = {**copy_json_document(raw), "prompt": ""}
    planned = copy_json_document(fallback)
    diff = differences(base, raw, planned)
    if diff.relaunched:
        servers = planned.setdefault("mcpServers", {})
        for server in diff.relaunched:
            servers[server] = {"disabled": True}
    offs = _tool_offs(raw)
    if offs:
        planned["excludedTools"] = list(
            dict.fromkeys([*_strings(planned.get("excludedTools")), *sorted(offs)])
        )
    diff = differences(base, raw, planned)
    if diff.gained:
        planned = _strip_grants(planned, diff.gained)
        diff = differences(base, raw, planned)
    merged = merge_worker_spec(base, worker_additions(base, planned))
    reason = diff.loosening()
    kept_off = offs - set(_strings(merged.get("excludedTools")))
    if kept_off:
        reason = "; ".join(
            filter(
                None, [reason, _describe("its worker would run", {("tool", t) for t in kept_off})]
            )
        )
    return planned, reason


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

    Returns the crewmates repaired. Never raises; a bad file is logged and left.
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
    except LedgerUnreadable as exc:
        # Kept as it is, so the owner can fix it; nothing moves until then.
        logger.warning(
            "crewmate migration: %s; no crewmate moves until it is fixed or removed", exc
        )
        return report
    except Exception:
        logger.warning(
            "crewmate migration: could not read the crewmates or the worker base", exc_info=True
        )
        return report

    verified: list[str] = []
    unchanged: set[str] = set()
    #: Per crewmate moved with a difference, the sentence its notice names.
    changes: dict[str, str] = {}
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
            # Moved anyway (owner decision, docs/decisions): its worker starts from
            # the default template, as a fresh create's does, and its user is told.
            logger.info("crewmate migration: %s's agent %r unreadable: %s", label, source, exc)
            fallback, kept_limit = _fallback_keeping_limits(
                source, base, _default_template_file(name, base)
            )
            if kept_limit:
                # Its spec parses (only its prompt did not resolve) and states a
                # limit the fallback would not keep: moving would loosen it.
                report.dry_run_failed[name] = kept_limit
                logger.warning(
                    "crewmate migration: %s stays on %r because %s", label, source, kept_limit
                )
                continue
            try:
                _write_file(name, fallback)
                _read_back(name)
            except (MateConfigError, agent_specs.AgentSpecUnreadable) as write_exc:
                report.copy_failed[name] = str(write_exc)
                logger.warning(
                    "crewmate migration: %s's file was not written: %s", label, write_exc
                )
                continue
            report.moved_from_default.append(name)
            changes[name] = (
                f"the agent it ran on ({source}) could not be read, so its worker starts "
                "from the default template and nothing that agent added was carried over"
            )
            verified.append(name)
            continue
        planned, diff, withheld = plan_move(name, base, old)
        reason = diff.loosening()
        if reason:
            report.dry_run_failed[name] = reason
            logger.warning("crewmate migration: %s stays on %r because %s", label, source, reason)
            continue
        try:
            _write_file(name, planned)
            diff = differences(base, old, _read_back(name))
            reason = diff.loosening()
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
        change = change_notice(diff, withheld)
        if change:
            # Moved anyway (owner decision, docs/decisions): a loss is told once, a gain
            # was not written and waits for the user's card.
            changes[name] = change
            logger.warning("crewmate migration: %s moves from %r; %s", label, source, change)
        _record_kit(name, source, planned)
        verified.append(name)

    # Pointers move only once every candidate above has been written and proven.
    for name in verified:
        binding = cfg.agents.get(name)
        source = "" if name in unchanged else candidates[name]
        try:
            # Intent first: the thread pass is owed before the binding moves.
            _record_migrated(name, source, moved=False)
            _repoint(
                name,
                source=candidates[name],
                store=binding.memory_store if binding is not None else None,
            )
        except Exception as exc:
            report.repoint_failed[name] = str(exc)
            logger.warning("crewmate migration: repoint failed for %s: %s", _label(cfg, name), exc)
            continue
        report.migrated.append(name)
        if name in changes and name not in report.moved_from_default:
            report.moved_with_changes[name] = changes[name]
        from kiro_crew.mate_upgrade_notice import clear_kept_owed, mark_notice_owed

        # A moved crewmate owes no "left as it was" notice from any earlier start.
        clear_kept_owed(name)
        if name not in unchanged:
            mark_notice_owed(name, changes.get(name, ""))
        if name in unchanged:
            how = "already on the conductor"
        elif name in changes:
            how = "moved; " + changes[name]
        else:
            how = "moved; its worker grants what its old agent did"
        try:
            _record_migrated(name, source, reason=how)
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
    _tell_kept(report)
    return report


def _tell_kept(report: MigrationReport) -> None:
    """Owe each crewmate the pass left where it was one notice naming why.

    The dry-run reasons are written for a reader; the other failures are not, so
    they get a plain cause. The notice is claimed once, so a crewmate still left
    behind at a later start is not told again.
    """
    from kiro_crew.mate_upgrade_notice import mark_kept_owed

    kept = dict(report.dry_run_failed)
    for name in report.skipped_no_source:
        kept.setdefault(name, "the agent it runs on could not be read")
    for name in report.copy_failed:
        kept.setdefault(name, "its own settings file could not be written")
    for name in report.repoint_failed:
        kept.setdefault(name, "its settings could not be switched over")
    for name, reason in kept.items():
        mark_kept_owed(name, reason)


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


def _repoint(name: str, *, source: str, store: str | None) -> None:
    """Flip *name*'s ``kiro_agent`` from *source* to its own id, under the config lock.

    The create path's write, compare-and-set on ``kiro_agent`` == *source* and the
    store *store*, so a change made meanwhile wins and the crewmate waits a pass.
    """
    from kiro_crew.memory_stores import persist_member_config

    cfg = KiroCrewConfig.load()
    binding = cfg.agents.get(name)
    if binding is None:
        raise MateConfigError(f"{name!r} is no longer a configured agent")
    binding.kiro_agent = name
    persist_member_config(
        cfg,
        name,
        expected_store=store,
        expected_values={"kiro_agent": source},
        changed_fields={"kiro_agent"},
    )
