"""Whether a shell ``allowedTools`` grant lifts ``denyByDefault`` on KAS.

A spec that sets ``toolsSettings.shell.denyByDefault`` and also lists the shell in
``allowedTools`` is read one of two ways, and the ``kas_subagents`` feature preview
chooses which, the preview the engine's sub-agents follow, through its one reader
(``kas_engine_subagents.kas_subagents_preview_on``):

- on: the allowlist holds beside the grant, whatever shape the
  ``deniedCommands`` entries have, as kiro-cli's agent engine keeps it
  (``deny shell ["*"] exclude [<allowedCommands>]`` beside its ``allow shell``).
- off or never set: the grant lifts the allowlist while every
  ``deniedCommands`` entry rides the policy as one glob
  (``kas_permissions.spec_shell_allowlist``).

:func:`kas_shell_allowlist` is the one reading, taken where a KAS agent is
projected (``kas_agents.to_client_custom_agent``) and where its session-start
notice is built (``kas_agents.spec_keys_without_carrier``). Everything else reads
the record that projection wrote, so it follows the value the projection read:
the session handle's stamp and a mode switch, the engine sub-agent target check
(``kas_subagent_targets._gate_only_limits``) and the remote-workspace check
(``runtime_remote_workspace._carries_spec_allowlist``). The preview is read only for
a spec the grant would lift, so every other spec is projected without a config read.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.acp.kas_permissions import shell_denies_by_default, spec_shell_allowlist

logger = logging.getLogger(__name__)


def shell_grant_lifts_deny_by_default() -> bool:
    """Whether a shell grant lifts ``denyByDefault`` now: the reader says the
    preview is not on. A config that cannot be read is handed to the reader as
    ``None``, so the reader alone says what that means.

    A sub-agent run's session start asks the reader once more, before this, to
    decide whose grants the run takes (``kas_parent_grants.preview_on``). That
    call hands it the same config, the applied one and else the file, and reads a
    config that cannot be read as off too, so the two cannot differ.
    """
    # Call-time: kas_engine_subagents imports the projection this module serves,
    # and the config loader pulls in the config plane.
    from kiro_crew.acp import kas_engine_subagents as engine

    try:
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = live.snapshot() or KiroCrewConfig.load()
    except Exception:  # noqa: BLE001 - the reader decides what no config means
        logger.warning(
            "config unreadable; the kas_subagents preview is read without it", exc_info=True
        )
        cfg = None
    return not engine.kas_subagents_preview_on(cfg)


def kas_shell_allowlist(spec: dict[str, Any], tools_settings: Any, policy: Any) -> list[str] | None:
    """*spec*'s ``denyByDefault`` shell allowlist as a KAS session applies it, or ``None``.

    ``kas_permissions.spec_shell_allowlist`` with the spec's ``allowedTools``,
    except that the lift a shell grant gives there is kept only while
    :func:`shell_grant_lifts_deny_by_default` says so.
    """
    patterns = spec_shell_allowlist(tools_settings, spec.get("allowedTools"), policy)
    if patterns is not None or not shell_denies_by_default(tools_settings):
        return patterns
    if shell_grant_lifts_deny_by_default():
        return None
    return spec_shell_allowlist(tools_settings, None, policy)
