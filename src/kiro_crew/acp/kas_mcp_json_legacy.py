"""The agent files that keep KAS's earlier ``mcp.json`` behaviour.

Left to itself, KAS starts every global and workspace ``mcp.json`` server for an
agent whatever its file says; Kiro Crew starts them only for an agent that opts
in, as kiro-cli does (:func:`kiro_crew.acp.kas_settings_mcp.excluded_whole`). The
user agent files that exist when that rule first runs, and that state neither
``includeMcpJson`` nor ``useLegacyMcpJson``, keep KAS's own behaviour: the
first KAS session that finds no list records them once, under the data home,
and every later session reads them as opted in. A file written after that
follows kiro-cli's rule.

The agent files themselves are never edited: kiro-cli reads the same directory,
and adding the key there would change what kiro-cli loads too.

shortcut: an entry is a file stem or a declared ``name``, not an inode or a
creation time. An editor that saves by writing a new file and renaming it over
the old one gives the file a new inode and creation time on every save, so such
a key would drop a file its user only edited; a file deleted and recreated under
the same name stays on the list. Upgrade if recreated files ever need telling
apart.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.acp.mcp_session_report import ERROR_CAP, NAME_CAP, sanitize_sink_text

logger = logging.getLogger(__name__)

LIST_NAME = "kas_mcp_json_legacy_agents.json"
_OPT_IN_KEYS = ("includeMcpJson", "useLegacyMcpJson")


def list_path() -> Path:
    """Where the list lives: beside the data home's other one-time migration marks."""
    from kiro_crew.config.paths import data_home

    return data_home() / ".migrations" / LIST_NAME


def legacy_mcp_json(agent_id: str, agents_dir: Path, spec: dict[str, Any]) -> bool:
    """Whether *agent_id* keeps KAS's own behaviour, recording the list on first use.

    *spec* is the spec the session runs: one that states either key now follows
    it, whatever the list says, so a later explicit ``false`` is honoured. The
    list is recorded first whichever agent the session runs, so a file written
    after the first KAS session is never on it. Blocking file I/O; the KAS
    projection calls it in its worker thread.
    """
    names = _read(list_path())
    if names is None:
        names = _record(agents_dir, list_path())
    if any(key in spec for key in _OPT_IN_KEYS):
        return False
    return agent_id in names


def _read(path: Path) -> frozenset[str] | None:
    """The recorded names, ``None`` when nothing was recorded yet.

    An unreadable or malformed list keeps nobody, and is never rebuilt: a rebuild
    would also record the files written since the first run.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("%s cannot be read; no agent file keeps mcp.json servers", path)
        return frozenset()
    try:
        names = json.loads(raw).get("agents")
    except (ValueError, AttributeError):
        names = None
    if not isinstance(names, list):
        logger.warning("%s is malformed; no agent file keeps mcp.json servers", path)
        return frozenset()
    return frozenset(n for n in names if isinstance(n, str))


def _record(agents_dir: Path, path: Path) -> frozenset[str]:
    """Record the agent files in *agents_dir* that state no opt-in, once.

    A file that cannot be read or parsed is skipped. A directory that cannot be
    listed records nothing, so the next session tries again.
    """
    from kiro_crew import agent_discovery
    from kiro_crew.agent_spec_format import iter_agent_spec_files_strict
    from kiro_crew.atomic_write import atomic_write

    try:
        files = iter_agent_spec_files_strict(agents_dir) if agents_dir.is_dir() else []
    except OSError:
        logger.warning("%s cannot be listed; mcp.json opt-ins not recorded yet", agents_dir)
        return frozenset()
    names: set[str] = set()
    for spec_path in files:
        try:
            spec = agent_discovery.read_agent_spec_strict(
                spec_path, operation="kas_mcp_json_legacy", source="unknown"
            )
        except (OSError, ValueError) as exc:
            shown = sanitize_sink_text(spec_path.name, NAME_CAP)
            logger.info("agent file %s skipped: %s", shown, sanitize_sink_text(str(exc), ERROR_CAP))
            continue
        if not isinstance(spec, dict) or any(key in spec for key in _OPT_IN_KEYS):
            continue
        kept = {spec_path.stem}
        if isinstance(spec.get("name"), str):
            kept.add(spec["name"])
        names |= kept
        shown = sanitize_sink_text(spec_path.name, NAME_CAP)
        logger.info("agent file %s keeps its mcp.json servers on KAS", shown)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps({"agents": sorted(names)}, indent=2) + "\n")
    except OSError:
        logger.warning("%s cannot be written; mcp.json opt-ins not recorded yet", path)
    return frozenset(names)
