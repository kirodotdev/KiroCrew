"""The owner's on/off record for Agent Plugins.

``agent-plugins.json`` in the data home names every plugin the owner turned on.
Consent is to a plugin by name, not to a version of its files: an update to an
enabled plugin, by Kiro IDE, kiro-cli, Crew or a hand edit, keeps it on.

Where the record lives, and who writes it
-----------------------------------------
The leaf is sealed read-only by the OS sandbox (``sandbox._CREW_READONLY_LEAVES``)
and fenced from the agent's file tools (``security._CREW_SECRET_LEAVES``), the
placement ``aws_service_consent.json`` has, for the same reason: it is an
authorization, not a preference, and an agent that could write it could turn on
a plugin whose stdio servers run outside the PreToolUse gate. The owner-only
dashboard route ``POST /api/agent-plugins`` is the only caller of
:func:`set_enabled`; there is no MCP tool and no CLI verb.

Reads fail closed: a missing, empty, unreadable or malformed record means no
plugin is on. The launcher reads it from inside a harness child, which is why
the sandbox lists it as readable by one (``_CREW_CHILD_READABLE_LEAVES``); it
holds plugin names only.

Writers serialise on an in-process lock, for the reason ``aws_consent`` gives:
every writer runs in the gateway process, and a lock file beside the record
would be one a sandboxed agent could hold to block the owner's change.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: The data-home leaf the record lives in.
CONSENT_LEAF = "agent-plugins.json"
RECORD_VERSION = 1
#: A record larger than this is not read; it lists names only.
MAX_RECORD_BYTES = 1024 * 1024

_STORE_LOCK = threading.Lock()


def consent_path() -> Path:
    """``<data home>/agent-plugins.json``. Respects ``KIROCREW_HOME``."""
    return config_dir() / CONSENT_LEAF


def _read_record() -> dict[str, Any]:
    """The record as stored, or ``{}`` when missing, unreadable or malformed."""
    path = consent_path()
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            logger.warning("agent plugins: %s is too large; no plugin is on", path.name)
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        logger.warning("agent plugins: %s is unreadable; no plugin is on", path.name)
        return {}
    return raw if isinstance(raw, dict) else {}


def enabled() -> frozenset[str]:
    """The names of every plugin the owner turned on.

    Fails closed to an empty set. Each row is an object keyed by the plugin
    name; its contents are ignored, so any object row reads as on. A row that is
    not an object is dropped.
    """
    data = _read_record()
    if data.get("version") != RECORD_VERSION:
        return frozenset()
    rows = data.get("enabled")
    if not isinstance(rows, dict):
        return frozenset()
    return frozenset(name for name, row in rows.items() if name and isinstance(row, dict))


def _preserve_if_unreadable() -> None:
    """Copy an unparseable record aside before a write replaces it."""
    path = consent_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    try:
        if isinstance(json.loads(raw), dict):
            return
    except (ValueError, RecursionError):
        pass
    sidecar = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}")
    try:
        atomic_write(sidecar, raw, restrict_to_owner=True)
        logger.warning("agent plugins: kept the unreadable record as %s", sidecar.name)
    except OSError:
        logger.warning("agent plugins: could not keep the unreadable record", exc_info=True)


def set_enabled(name: str, on: bool) -> None:
    """Turn *name* on or off. Called only by the dashboard handler."""
    if not isinstance(name, str) or not name:
        raise ValueError("a plugin name is required")
    with _STORE_LOCK:
        _preserve_if_unreadable()
        current = set(enabled())
        if on:
            current.add(name)
        else:
            current.discard(name)
        doc = {"version": RECORD_VERSION, "enabled": {n: {} for n in sorted(current)}}
        atomic_write(
            consent_path(), json.dumps(doc, indent=2, sort_keys=True), restrict_to_owner=True
        )
