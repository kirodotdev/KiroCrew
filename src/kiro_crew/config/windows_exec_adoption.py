"""The two load-path rules that bound the Windows ``agent.sandbox_allow_unsandboxed_exec`` row.

The ``superseded_defaults`` row for this key drops a stored ``false`` once on
Windows, which lets agent subprocesses run without OS isolation. Two rules keep
that drop to a value that was already stored when the host first loaded this build:

* :func:`close_when_nothing_stale` ends the one-shot on an install whose base
  document holds no ``false`` to drop (the key absent, or ``true``). It records
  ``false`` as acknowledged, the same record ``kirocrew setup``, ``config set`` and
  Settings saves write for a ``false`` they store, so any ``false`` written later,
  a hand edit included, is the operator's and is never offered for adoption.
* :func:`audit_or_keep` writes the same critical SEL event ``kirocrew setup`` writes
  when it grants unsandboxed execution, before the drop. When that write fails,
  the drop is withheld and the stored ``false`` stays.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("kiro_crew.config.loader")

KEY = "agent.sandbox_allow_unsandboxed_exec"
_FIELD = "sandbox_allow_unsandboxed_exec"


def _row():
    # Imported at call time: the loader imports this module.
    from kiro_crew.config.superseded_defaults import SUPERSEDED_DEFAULTS

    return next(e for e in SUPERSEDED_DEFAULTS if e.dotted_key == KEY)


def close_when_nothing_stale(base_data: dict) -> bool:
    """Acknowledge ``false`` once where the row applies and nothing stale is stored.

    Returns False only when the close was due and the single-shot lock was BUSY:
    another writer is mid-write, so the next load can succeed. The loader does not
    cache a document whose close is deferred, so that next load retries it rather
    than serving the cached copy past it.

    Every other failure returns True. An unreadable ledger, a refused path or a
    failed write will fail the same way on the next load, so retrying would only
    turn the config cache off for good. Nothing is dropped meanwhile: an unreadable
    ledger also stops :func:`~kiro_crew.config.superseded_defaults.auto_adoptable`.
    """
    from kiro_crew.config import superseded_defaults as sd

    row = _row()
    if row.applies is not None and not row.applies(base_data):
        return True
    section = base_data.get("agent")
    stored = section.get(_FIELD, None) if isinstance(section, dict) else None
    if stored is not None and stored is not True:
        return True
    adopted = sd.adopted_superseded_if_readable()
    if adopted is None:
        return True
    if KEY in adopted or KEY in sd.acked_superseded():
        return True
    try:
        sd._update_map(
            sd.ACK_SECTION, lambda existing: {**existing, KEY: False}, wait_for_lock=False
        )
    except BlockingIOError as exc:
        logger.debug("config: deferring the %s one-shot close: %s", KEY, exc)
        return False
    except OSError as exc:
        logger.debug("config: could not record the %s one-shot close: %s", KEY, exc)
    return True


def audit_or_keep(to_record: dict[str, object]) -> None:
    """Audit the drop of *KEY* before it happens, or take it out of *to_record*.

    *to_record* is the set of keys the migration is about to record and remove.
    """
    if KEY not in to_record:
        return
    from kiro_crew.config.loader import config_path  # circular import
    from kiro_crew.sel import sel

    resources = str(config_path())
    try:
        sel().log_tool_invocation(
            session_key="config-migration",
            agent="system",
            source="config.windows_exec_adoption.audit_or_keep",
            tool_name=_FIELD,
            tool_kind="config",
            outcome="allowed",
            resources=resources,
            metadata={"reason": "stale_windows_false_adopted"},
            critical=True,
        )
    except Exception as exc:
        del to_record[KEY]
        logger.warning(
            "config: kept %s=false: the security audit event for removing it could "
            "not be written (%s); the removal is retried on a later load",
            KEY,
            exc,
        )
