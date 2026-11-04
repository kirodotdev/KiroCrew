"""The opt-in V2 engine for a Kiro CLI harness choice (``agent.kiro_cli_engine``).

``"auto"`` (the default) leaves a stored Kiro CLI choice to the
a-kiro-cli-choice-runs-on-kas entry in ``docs/decisions``: it runs on Kiro
Crew's KAS path once this host's kiro-cli is confirmed to offer its v3 engine, and
on the bare v2 launch otherwise. ``"v2"`` keeps that choice on the Kiro CLI harness
and pins kiro-cli's v2 engine with an explicit ``--agent-engine v2``, because a
bare ``kiro-cli acp`` on kiro-cli 3.0 starts the v3 engine whatever its help says.

The pin is offered only while kiro-cli still lists v2. A kiro-cli that lists engines
without v2 (v2 retired) gives the ``"auto"`` answer on both halves: the choice
moves onto KAS on the usual evidence, and a spawn gets no engine flag. Both
fallbacks are logged once per binary, naming why.

Import-light: the config dataclasses import the constants at module load, so the
probe and the config reader are imported where they are used.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Leave the engine to the a-kiro-cli-choice-runs-on-kas decision.
KIRO_CLI_ENGINE_AUTO = "auto"
#: Keep a Kiro CLI choice on the Kiro CLI harness and pin kiro-cli's v2 engine.
KIRO_CLI_ENGINE_V2 = "v2"
#: Every value ``agent.kiro_cli_engine`` accepts, default first.
KIRO_CLI_ENGINE_CHOICES: tuple[str, ...] = (KIRO_CLI_ENGINE_AUTO, KIRO_CLI_ENGINE_V2)

#: The kiro-cli flag that selects the engine.
AGENT_ENGINE_FLAG = "--agent-engine"

#: Binaries a fallback was already logged for, so a long-lived gateway says it once.
_warned: set[tuple[str, str]] = set()


def coerce_kiro_cli_engine(value: object) -> str:
    """A stored value as the engine choice: ``"v2"`` only for that exact string."""
    return KIRO_CLI_ENGINE_V2 if value == KIRO_CLI_ENGINE_V2 else KIRO_CLI_ENGINE_AUTO


def stored_kiro_cli_engine() -> str:
    """The stored choice, read from the config documents. ``"auto"`` on any failure."""
    try:
        from kiro_crew.config.loader_document import kiro_cli_engine_stored

        return kiro_cli_engine_stored()
    except Exception:  # noqa: BLE001 - an unreadable document leaves the default
        return KIRO_CLI_ENGINE_AUTO


#: The engine this process runs, read once. ``agent.kiro_cli_engine`` is a
#: restart field, so a value saved while the process runs reaches neither the pick
#: nor the spawn argv until the process is replaced; reading it again per spawn
#: would pin one engine while the pick still answered for the other.
_process_engine: str | None = None


def process_kiro_cli_engine() -> str:
    """The engine this process runs: the stored choice as it read at first use."""
    global _process_engine
    if _process_engine is None:
        _process_engine = stored_kiro_cli_engine()
    return _process_engine


def _warn_once(binary: str, reason: str, message: str) -> None:
    if (binary, reason) in _warned:
        return
    _warned.add((binary, reason))
    logger.warning(message)


def pinned_kiro_cli_engines(kiro_bin: str | None = None) -> tuple[str, ...] | None:
    """The engines the pinned kiro-cli's ``acp --help`` lists, or ``None`` when unknown.

    Only the :func:`kiro_cli.pin_kiro_cli` binary is probed, because the probe runs
    outside the sandbox. With *kiro_bin* given, a binary that is not the pinned one
    (one found on an inherited ``PATH``) is never run and reads as unknown.
    Blocking: one bounded spawn per binary identity, cached. Never raises.
    """
    from kiro_crew import kiro_cli

    try:
        pinned, _unpinned = kiro_cli.pin_kiro_cli()
    except Exception:  # noqa: BLE001 - an unanswerable pin is unknown
        return None
    if pinned is None:
        return None
    if kiro_bin is not None and os.path.realpath(pinned) != os.path.realpath(kiro_bin):
        return None
    return kiro_cli.acp_agent_engines_at(pinned)


def kiro_cli_retired_v2(engines: tuple[str, ...] | None) -> bool:
    """Positive evidence that this kiro-cli dropped v2: it lists engines, none is v2."""
    return bool(engines) and KIRO_CLI_ENGINE_V2 not in (engines or ())


def _record_retired_v2() -> None:
    from kiro_crew.agent_sdk import backends

    backends.confirm_kiro_cli_retired_v2()


def kiro_cli_engine_argv(kiro_bin: str, engine: str | None = None) -> list[str]:
    """The engine flag a Kiro CLI harness spawn appends: ``[]`` or ``--agent-engine v2``.

    *engine* defaults to this process's choice (:func:`process_kiro_cli_engine`). ``"auto"`` adds nothing, which is the
    argv every release before this setting built. ``"v2"`` adds the pin only when
    the pinned kiro-cli lists v2 for ``acp --agent-engine``; otherwise the argv is
    unchanged and the reason is logged once. A kiro-cli that lists engines without
    v2 is also recorded as having retired it, so later loads follow ``"auto"``. Blocking (see
    :func:`pinned_kiro_cli_engines`); never raises.
    """
    if engine is None:
        engine = process_kiro_cli_engine()
    if engine != KIRO_CLI_ENGINE_V2:
        return []
    engines = pinned_kiro_cli_engines(kiro_bin)
    if engines and KIRO_CLI_ENGINE_V2 in engines:
        return [AGENT_ENGINE_FLAG, KIRO_CLI_ENGINE_V2]
    if engines is None:
        _warn_once(
            kiro_bin,
            "unknown",
            "agent.kiro_cli_engine is v2, but which engines this kiro-cli accepts could "
            f"not be read ({kiro_bin} is not the pinned kiro-cli, or its 'acp --help' "
            "did not run); starting it without --agent-engine",
        )
    elif not engines:
        _warn_once(
            kiro_bin,
            "no-selector",
            "agent.kiro_cli_engine is v2, but this kiro-cli has no --agent-engine "
            "selector; starting it without one (it runs v2 already)",
        )
    else:
        # Positive evidence that v2 is gone: record it, so the next config load
        # moves the pick onto the automatic choice instead of this bare launch.
        _record_retired_v2()
        _warn_once(
            kiro_bin,
            "retired",
            "agent.kiro_cli_engine is v2, but this kiro-cli no longer offers v2 "
            f"(it lists {', '.join(engines)}); following the automatic engine choice",
        )
    return []
