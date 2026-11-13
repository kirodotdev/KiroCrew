"""Name the kiro-cli setting that stops Kiro Crew's MCP servers from starting.

On macOS, ``"sandbox": true`` in kiro-cli's settings file makes kiro-cli confine
itself, and every MCP server it starts runs inside that confinement. Kiro Crew's
own servers (``kirocrew-core`` and its siblings) fail to start under kiro-cli's
profile, so every kiro-cli session fails at ``session/new`` with kiro-cli's own
words, ``mcp-client(kirocrew-core): initial connection or tool synchronization
failed``. ``kirocrew doctor``'s MCP probe starts the same server OUTSIDE
that sandbox, so it passes on exactly the host where sessions fail.

This module answers one question for two surfaces, the doctor row and the
session's start error: is that setting on, and what should the operator change.
It never edits kiro-cli's settings: turning a sandbox layer off is the operator's
call, and whether it is safe depends on Kiro Crew's own tier (see
:func:`self_sandbox_remedy`).

Every read goes through the ``kiro_crew.sandbox`` module at call time, so a test
that patches the switch, the settings path or the tier there reaches this module too.
"""

from __future__ import annotations

import re
import sys

from kiro_crew import sandbox

#: kiro-cli's message when an MCP server it started never finished its handshake,
#: scoped to Kiro Crew's own servers. A third-party server failing the same way says
#: nothing about this setting, so it gets no hint.
_CREW_MCP_START_FAILED_RE = re.compile(
    r"mcp-client\((?P<server>kirocrew[\w.-]*)\)\s*:\s*"
    r"initial connection or tool synchronization failed",
    re.IGNORECASE,
)


def kiro_cli_self_sandboxes() -> bool:
    """True when kiro-cli's own sandbox confines this host's kiro-cli spawns.

    macOS only: that is where the setting moves confinement to kiro-cli and Kiro
    Crew skips its own wrap. Linux namespace isolation is unaffected by the file.
    Never raises: an unreadable setting answers False, so no surface claims a cause
    it could not read.
    """
    if sys.platform != "darwin":
        return False
    try:
        return bool(sandbox.kiro_internal_sandbox_enabled())
    except Exception:  # noqa: BLE001 - an unreadable setting is not evidence
        return False


def self_sandbox_remedy() -> str:
    """What to change, worded for the tier Kiro Crew would ACTUALLY apply.

    The one copy of this decision: the Sandbox section's ``kiro-cli:`` note, the
    MCP Tools row and the session-start hint all print it. The tier includes the
    governance clamp (:func:`sandbox.effective_sandbox_mode`). Telling an operator
    to disable kiro-cli's sandbox while ``agent.sandbox`` is ``"off"`` would remove
    the only layer confining the spawn and make ``~/.aws`` and ``~/.ssh`` readable,
    and the two settings correlate: ``"off"`` exists to defer isolation to kiro-cli.
    A tier that cannot be read gets the cautious wording, never the bare advice.
    """
    _settings_path, key = sandbox.kiro_internal_sandbox_switch()
    try:
        own_tier = sandbox.effective_sandbox_mode(sandbox.configured_sandbox_mode())
    except Exception:  # noqa: BLE001 - an unreadable tier must not shape the advice
        own_tier = None
    if own_tier is not None and own_tier != "off":
        return (
            f'Set "{key}" to false to hand isolation back to Kiro Crew\'s own profile '
            f'(agent.sandbox="{own_tier}"), which masks credential paths and leaves the '
            "rest of your home readable; the value is re-read per spawn, so no restart "
            "is needed."
        )
    if own_tier == "off":
        return (
            f'Do NOT just set "{key}" to false here: agent.sandbox is "off" too, so Kiro '
            "Crew builds no profile and turning this key off would leave the spawn with "
            "no OS confinement at all, making credential paths such as ~/.aws and ~/.ssh "
            'readable. Set agent.sandbox to "auto" first, then either layer owns isolation.'
        )
    return (
        f'Before setting "{key}" to false, check that agent.sandbox is not "off": with '
        "both off the spawn runs with no OS confinement at all and credential paths "
        "such as ~/.aws and ~/.ssh become readable. Kiro Crew's own tier could not be "
        "read from here."
    )


def conflict_explanation(server: str = "kirocrew-core") -> str:
    """The cause in plain words, followed by :func:`self_sandbox_remedy`."""
    settings_path, key = sandbox.kiro_internal_sandbox_switch()
    return (
        f'{settings_path} sets "{key}": true, so kiro-cli runs inside its own sandbox '
        "and starts its MCP servers inside it too. Kiro Crew's MCP server "
        f"({server}) fails to start under that sandbox, so kiro-cli sessions fail with "
        f'"mcp-client({server}): initial connection or tool synchronization failed". '
        f"{self_sandbox_remedy()} Then start a new session."
    )


def backend_spawns_kiro_cli(backend: str) -> bool:
    """True when a session on *backend* runs inside a kiro-cli process.

    The Kiro CLI backend, or KAS served through kiro-cli's relay. KAS on Kiro Crew's
    own bundle runs Node with no kiro-cli, and every other harness launches on its
    own, so kiro-cli's sandbox setting says nothing about why they failed. Reads
    config for the KAS route; a read that fails answers False, so no hint claims a
    cause it could not confirm.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

    if backend == ACP_BACKEND_KIRO:
        return True
    if backend == ACP_BACKEND_KAS:
        return _kas_runs_on_the_relay()
    return False


def _kas_runs_on_the_relay() -> bool:
    """True when ``agent.kas_bundle`` is off, so KAS starts through kiro-cli's relay."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return not KiroCrewConfig.load().agent.kas_bundle
    except Exception:  # noqa: BLE001 - an unreadable route is not evidence
        return False


def session_start_hint(error_text: str, backend: str) -> str | None:
    """The cause and fix to append to a session-start error, or None.

    Only when the failing session runs inside kiro-cli (*backend*, see
    :func:`backend_spawns_kiro_cli`), kiro-cli's error names one of Kiro Crew's own
    servers, AND the setting is on: the error alone has other causes (a broken
    install, a crashed server), and the setting alone is harmless to a session that
    did not fail or that kiro-cli does not host. Reads the settings file and config,
    so an async caller runs it off the event loop.
    """
    match = _CREW_MCP_START_FAILED_RE.search(error_text or "")
    if match is None or not kiro_cli_self_sandboxes() or not backend_spawns_kiro_cli(backend):
        return None
    try:
        return f"Likely cause: {conflict_explanation(match.group('server'))}"
    except Exception:  # noqa: BLE001 - a hint must never replace the error it explains
        return None
