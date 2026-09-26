"""Operator-supplied ACP launch configuration; validation never runs the command."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_KEY = "agent.custom_acp"
LABEL = "Custom ACP"
MAX_COMMAND_LENGTH = 4096
MAX_ARGUMENTS = 128
MAX_ARGUMENT_LENGTH = 8192


def validate_custom_acp(raw: object) -> dict[str, Any]:
    """Validate the whole launch pair without shell parsing or partial recovery."""
    if not isinstance(raw, dict) or set(raw) != {"command", "args"}:
        raise ValueError("Custom ACP configuration must contain only command and args")
    command = raw["command"]
    args = raw["args"]
    if (
        not isinstance(command, str)
        or len(command) > MAX_COMMAND_LENGTH
        or any(ord(char) < 32 for char in command)
    ):
        raise ValueError("Custom ACP executable must be a single path or executable name")
    command = command.strip()
    if (
        command
        and ("/" in command or "\\" in command)
        and not Path(command).expanduser().is_absolute()
    ):
        raise ValueError("Custom ACP executable must use an absolute path or a name on PATH")
    if not isinstance(args, list) or len(args) > MAX_ARGUMENTS:
        raise ValueError(f"Custom ACP args must be an array of at most {MAX_ARGUMENTS} strings")
    if any(
        not isinstance(arg, str) or "\0" in arg or len(arg) > MAX_ARGUMENT_LENGTH for arg in args
    ):
        raise ValueError("Custom ACP arguments must be strings without null characters")
    if not command and args:
        raise ValueError("Configure a Custom ACP executable before adding arguments")
    return {"command": command, "args": list(args)}


def coerce_custom_acp(raw: object) -> dict[str, Any]:
    """An invalid saved pair disables custom launch, not the gateway."""
    if raw is not None:
        try:
            return validate_custom_acp(raw)
        except ValueError:
            # Arguments can contain private values; never log the submitted pair.
            logger.warning("Ignoring invalid agent.custom_acp; configure it in Agent Backend")
    return {"command": "", "args": []}


def resolve_custom_acp() -> list[str]:
    """Read one config snapshot and resolve argv without invoking an executable.

    Blocking config and filesystem reads belong in the caller's executor. Nothing
    is cached: an edited command must not reuse the previous executable's result.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.env import augmented_path

    config = validate_custom_acp(KiroCrewConfig.load().agent.custom_acp)
    command = config["command"]
    if not command:
        raise ValueError("Configure Custom ACP in Developer > Agent Backend before using it")
    executable = shutil.which(
        str(Path(command).expanduser()), path=augmented_path(os.environ.get("PATH", ""))
    )
    if not executable:
        raise ValueError("Custom ACP executable was not found; check its path in Agent Backend")
    # PATH may contain relative entries; the child starts in the session's cwd.
    return [os.path.abspath(executable), *config["args"]]
