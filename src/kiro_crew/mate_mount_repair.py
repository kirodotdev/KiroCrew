"""Startup repair: give a moved Bot back the mount of a server its old agent had.

A Bot the startup migration moved (``mate_config_migration``) can hold a file
that declares one of its own servers with no ``tools`` selector for it: the
move compared the file's selectors with the old agent's ``tools`` entries
literally, so the ``@server`` it spells for a server the old agent mounted
through ``*`` read as a gain and was withheld. Declaring a server is not
mounting it (``mate_worker.server_selectors``), so that worker cannot call the
server.

Per moved Bot still bound to its own file, for each server its file declares
that is not the worker base's or Crew's own, is not switched off and has no
selector: when the old agent the ledger names declared that server, did not
switch it off and mounted it through ``*``, the ``@server`` the move withheld
is written back. A selector that is literally one of the old agent's own
``tools`` entries (``@server``, ``@server/tool``) was never withheld, since the
literal comparison matched it; its absence from the file is the user's choice
and is left as it is. The old agent is read at the user level, as the move read
it; when no readable agent of that name is left there, it is read from where the
move retired it as a Bot's private copy (``mates/retired/``). The result goes
through :func:`mate_config_migration.differences` first, so it is a restoration
and never a gain. A server the old agent never selected, or whose entry in the
file is not the one the old agent defined, is left as it is.

Each Bot is checked once: ``migrated.json`` lists it under ``mounts_checked``,
so a selector its user takes out later is not put back. Never raises.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kiro_crew.agent_files import is_mate_service_agent
from kiro_crew.agent_sdk import agent_specs
from kiro_crew.config import paths as paths_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mate_config import MateConfigError, source_agents_dir, write_mate_spec
from kiro_crew.mate_worker import server_selectors
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS, mcp_entry_is_muted

logger = logging.getLogger(__name__)

#: The ``migrated.json`` key listing every Bot this repair has checked.
CHECKED_KEY = "mounts_checked"


def _servers(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, dict)}


def _old_agent(source: str) -> dict[str, Any] | None:
    """The agent *source* as the migration read it, ``None`` when it cannot be read.

    Governed when it resolves; else its raw spec with no prompt, as the
    default-template move reads an agent whose prompt does not resolve. With no
    readable spec for *source* in its folder, *source* may be a Bot's private
    copy the move retired, which every Bot that ran on it names as its old agent:
    that copy is read (:func:`_retired_copy`).
    """
    from kiro_crew.mate_config_migration import _governed_source
    from kiro_crew.user_json import copy_json_document

    try:
        return _governed_source(source)
    except MateConfigError:
        pass
    try:
        raw = agent_specs.load_agent_spec(source_agents_dir(source), source)
    except (agent_specs.AgentSpecUnreadable, OSError):
        return _retired_copy(source)
    return {**copy_json_document(raw), "prompt": ""}


def _retired_copy(name: str) -> dict[str, Any] | None:
    """The private copy *name* as the move retired it, governed; ``None`` when there is none.

    ``_retire_shadow`` writes Bot *name*'s copy to ``mates/retired/<name>.json``,
    or to the first ``<name>.<n>.json`` (n = 1, 2, ...) not taken yet, so the last
    of that run is the newest move's copy. It is looked up by its file's stem, so
    a numbered copy is read from the file named for it: every copy declares
    *name*, and a lookup by that name would answer from another copy or refuse two
    that differ. Its prompt is left out, since a relative ``file://`` one names a
    file beside where the copy was, and :func:`restored_mounts` compares grants
    only. An unreadable copy is ``None`` too.
    """
    from kiro_crew.platform.governance import sanitize_agent_config_governance

    retired = paths_mod.mate_agents_dir().parent / "retired"
    newest, stem, n = "", name, 1
    try:
        while (retired / f"{stem}.json").exists():
            newest, stem = stem, f"{name}.{n}"
            n += 1
        if not newest:
            return None
        raw = agent_specs.load_agent_spec(retired, newest)
    except (agent_specs.AgentSpecUnreadable, OSError):
        return None
    spec = {**raw, "prompt": ""}
    sanitize_agent_config_governance(spec, audit=False)
    return spec


def restored_mounts(
    base: dict[str, Any], old: dict[str, Any], mate_file: dict[str, Any]
) -> dict[str, Any] | None:
    """*mate_file* with the selectors the move withheld back, for each server it does not mount.

    A withheld selector is one :func:`~kiro_crew.mate_worker.server_selectors`
    gives for *old* that is not literally among *old*'s own ``tools`` entries:
    the ``@server`` its ``*`` spells.

    ``None`` when there is nothing to restore, or when restoring would grant the
    worker anything :func:`~kiro_crew.mate_config_migration.differences` counts
    as a gain over *old* that *mate_file* does not already hold. *mate_file* is
    not changed.
    """
    from kiro_crew.mate_config_migration import _base_owned_servers, differences
    from kiro_crew.user_json import copy_json_document

    owned = _base_owned_servers(base) | set(KIROCREW_BIN_MCP_SERVERS)
    old_servers = _servers(old.get("mcpServers"))
    gained = differences(base, old, mate_file).gained
    # A server whose entry is itself a gain (not as the old agent defined it) is
    # not the old agent's, so its selectors are not the old agent's to restore.
    not_old = {t[1] for t in gained if t[0] in ("server", "transport")}
    # The move kept every selector spelled exactly as one of the old agent's own
    # entries, so only a selector spelled otherwise can have been withheld.
    old_tools = set(_strings(old.get("tools")))
    restored: list[str] = []
    for name, entry in _servers(mate_file.get("mcpServers")).items():
        if name in owned or mcp_entry_is_muted(entry) or server_selectors(mate_file, [name]):
            continue
        if name not in old_servers or mcp_entry_is_muted(old_servers[name]) or name in not_old:
            continue
        restored.extend(
            ref
            for ref in server_selectors(old, [name])
            if ref not in old_tools and ref not in restored
        )
    if not restored:
        return None
    tools = mate_file.get("tools")
    out = copy_json_document(mate_file)
    out["tools"] = [*(tools if isinstance(tools, list) else []), *restored]
    if differences(base, old, out).gained - gained:
        return None
    return out


def restore_withheld_mounts(cfg: KiroCrewConfig | None = None) -> list[str]:
    """Run the repair over every moved Bot not checked yet; the Bots repaired. Never raises."""
    from kiro_crew.mate_config_migration import (
        _already_moved,
        _read_ledger,
        _worker_base,
        _write_ledger,
    )

    repaired: list[str] = []
    try:
        data = _read_ledger()
        threads = data.get("threads")
        done = set(_strings(data.get(CHECKED_KEY)))
        owed = {
            name: entry["from"]
            for name, entry in (threads.items() if isinstance(threads, dict) else ())
            if name not in done
            and isinstance(entry, dict)
            and isinstance(entry.get("from"), str)
            and entry["from"]
            and not is_mate_service_agent(name)
        }
        if not owed:
            return repaired
        cfg = cfg or KiroCrewConfig.load()
        base = _worker_base()
    except Exception:  # noqa: BLE001 - best effort; the next start tries again
        logger.warning("crewmate mount repair: ledger or worker base unreadable", exc_info=True)
        return repaired
    for name, source in sorted(owed.items()):
        binding = cfg.agents.get(name)
        member_id = str(getattr(binding, "member_id", "") or "")
        if binding is None or binding.kiro_agent != name:
            continue
        if not _already_moved(data, name, member_id):
            continue
        try:
            outcome = _repair_one(base, name, source)
        except Exception:  # noqa: BLE001 - one Bot's file never stops the others
            logger.warning("crewmate mount repair: %s not checked", name, exc_info=True)
            continue
        if outcome is None:
            continue
        if outcome:
            repaired.append(name)
            logger.info("crewmate mount repair: %s's own servers are mounted again", name)
        done.add(name)
    if not done - set(_strings(data.get(CHECKED_KEY))):
        return repaired
    try:
        data = _read_ledger()
        data[CHECKED_KEY] = sorted(done | set(_strings(data.get(CHECKED_KEY))))
        _write_ledger(data)
    except Exception:  # noqa: BLE001 - a repaired file now mounts; a re-check is a no-op
        logger.warning("crewmate mount repair: not recorded in the ledger", exc_info=True)
    return repaired


def _repair_one(base: dict[str, Any], name: str, source: str) -> bool | None:
    """Repair Bot *name*'s file from its old agent *source*: whether it was rewritten.

    ``None`` when its file or its old agent cannot be read now, so a later start
    checks it again.
    """
    path = paths_mod.mate_agents_dir() / f"{name}.json"
    try:
        mate_file = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    old = _old_agent(source)
    if not isinstance(mate_file, dict) or old is None:
        return None
    fixed = restored_mounts(base, old, mate_file)
    if fixed is None:
        return False
    try:
        write_mate_spec(name, fixed)
    except MateConfigError:
        logger.warning("crewmate mount repair: %s's file was not written", name)
        return None
    return True


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []
