"""Sidecar store for KiroCrew's per-agent bookkeeping.

kiro-cli validates ``~/.kiro/agents/*.json`` with serde ``deny_unknown_fields``
and rejects the *entire* spec on any unknown key, then silently falls back to
the default agent (``--agent <name>`` resolves to default with only a stderr
"no agent with name X found" line). KiroCrew therefore keeps its private
per-agent bookkeeping OUT of the kiro spec and in this sidecar, so every spec
stays schema-valid for kiro-cli.

Two values are tracked, both kept in this sidecar rather than the kiro spec:

- ``model_managed`` (bool): whether an agent's ``model`` should track the
  shipped ``defaults.json`` (so a default bump propagates) or is an explicit
  user pick frozen against future bumps.
- ``cc_model`` (str): a per-agent model for the ``claude_code`` provider (that
  backend can't pick a per-agent model from ``--agent`` the way kiro-cli does).

State file (``~/.kiro/crew/agent_model_state.json``, honoring ``KIROCREW_HOME``)::

    {
      "kirocrew":           {"model_managed": true},
      "kirocrew-heartbeat": {"cc_model": "auto"}
    }

This is a near-leaf module: it imports only the stdlib plus the leaf
``config.paths`` and ``atomic_write`` helpers, so it never participates in the
``agent`` <-> ``config.loader`` import cycle.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import MutableMapping

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

_STATE_FILENAME = "agent_model_state.json"
_MODEL_MANAGED = "model_managed"
_CC_MODEL = "cc_model"

# Guards in-process read-modify-write races (e.g. dashboard PATCH vs gateway
# refresh). Cross-process atomicity is provided by ``atomic_write``.
_lock = threading.RLock()


def _state_path() -> Path:
    """Return the sidecar path (resolved fresh so KIROCREW_HOME is honored)."""
    return config_dir() / _STATE_FILENAME


def _read() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    atomic_write(_state_path(), json.dumps(data, indent=2, sort_keys=True) + "\n")


def _entry(data: dict, name: str) -> dict:
    entry = data.get(name)
    return entry if isinstance(entry, dict) else {}


def get_model_managed(name: str) -> bool | None:
    """Return the agent's managed flag, or ``None`` when unset (grandfathered)."""
    with _lock:
        value = _entry(_read(), name).get(_MODEL_MANAGED)
    return bool(value) if isinstance(value, bool) else None


def set_model_managed(name: str, value: bool) -> None:
    with _lock:
        data = _read()
        entry = data.get(name)
        if not isinstance(entry, dict):
            entry = {}
        entry[_MODEL_MANAGED] = bool(value)
        data[name] = entry
        _write(data)


def get_cc_model(name: str) -> str | None:
    """Return the agent's claude_code-provider model, or ``None`` when unset."""
    with _lock:
        value = _entry(_read(), name).get(_CC_MODEL)
    return value if isinstance(value, str) and value else None


def set_cc_model(name: str, value: str | None) -> None:
    """Set (or clear, when ``value`` is falsy) the agent's claude_code model."""
    with _lock:
        data = _read()
        entry = data.get(name)
        if not isinstance(entry, dict):
            entry = {}
        if value:
            entry[_CC_MODEL] = str(value)
        else:
            entry.pop(_CC_MODEL, None)
        if entry:
            data[name] = entry
        else:
            data.pop(name, None)
        _write(data)


def prune(name: str) -> None:
    """Drop an agent's entry entirely (call when the agent is deleted)."""
    with _lock:
        data = _read()
        if name in data:
            data.pop(name, None)
            _write(data)


def lift_and_strip_bookkeeping(config: MutableMapping[str, object], name: str) -> bool:
    """Lift ``model_managed`` / ``cc_model`` into the sidecar when unset; strip both.

    kiro-cli rejects unknown fields on the whole agent spec. Every writer that
    persists a kiro agent JSON must run this so those keys, owned only by
    Kiro Crew, never land on disk. When the sidecar already holds a value, a
    stale key in *config* is discarded rather than clobbering the
    authoritative sidecar (same rule as ``migrate_agent_specs`` /
    ``_refresh_dynamic_fields`` / the per-agent PATCH handler).

    A key is only LIFTED when it has the right type — ``bool`` for
    ``model_managed``, non-empty ``str`` for ``cc_model`` — mirroring the read
    guards in :func:`get_model_managed` / :func:`get_cc_model`. A hand-edited
    PUT body (the dashboard's free-text Agent Config textarea) can carry e.g.
    ``"model_managed": "false"``, and ``bool("false")`` is ``True``: silently
    coercing that would flip the flag's meaning rather than just ignore the
    bad value. The key is ALWAYS stripped from *config* regardless of type,
    since it must never reach the kiro spec either way.

    Returns True if either key was present on *config* (and removed).
    """
    changed = False
    if _MODEL_MANAGED in config:
        value = config[_MODEL_MANAGED]
        if isinstance(value, bool):
            # Hold the lock across the get-and-set so a concurrent writer
            # (e.g. an explicit-model PATCH racing a stale config PUT)
            # can't sneak a value in between the unset check and the lift —
            # the lifted stale value would clobber the fresher one.
            # ``_lock`` is an RLock, so the nested get/set acquisitions are
            # re-entrant and safe.
            with _lock:
                if get_model_managed(name) is None:
                    set_model_managed(name, value)
        else:
            logger.warning("Discarding non-bool model_managed=%r for agent %r", value, name)
        config.pop(_MODEL_MANAGED, None)
        changed = True
    if _CC_MODEL in config:
        value = config[_CC_MODEL]
        if isinstance(value, str):
            with _lock:
                if value and get_cc_model(name) is None:
                    set_cc_model(name, value)
        elif value:
            logger.warning("Discarding non-string cc_model=%r for agent %r", value, name)
        config.pop(_CC_MODEL, None)
        changed = True
    return changed
