"""What handler modules tell the user after a settings write: the spec rebuild and extras.

``rebuild_agent_spec`` rewrites the agent spec a config write affects and reports
whether it confirmed the rewrite, and ``pip_extra_install_command`` names the command
that installs the optional extra a setting needs into this gateway's interpreter.

Served through :mod:`kiro_crew.dashboard.handlers._shared`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import logging

from kiro_crew import extras

logger = logging.getLogger("kiro_crew.dashboard.handlers._shared")


# The response text for a saved change whose agent-spec rebuild did not confirm a write.
AGENT_SPEC_NOT_REWRITTEN = (
    "saved, but the agent spec may not have been rewritten, so new sessions may keep "
    "the previous servers; see the gateway log"
)


def rebuild_agent_spec(context: str) -> bool:
    """Rebuild the agent spec after a dashboard config write; True only on a confirmed rewrite.

    Blocking: call it through ``asyncio.to_thread``. A handler puts the answer in
    its response as ``agent_spec_rewritten`` so the dashboard can say the change
    was saved but may not have reached the agent spec. ``False`` covers both
    unconfirmed outcomes: the rebuild declined to write (this instance may not own
    the shared agent home), or it raised, possibly after part of the spec set was
    already written. The config write itself already landed, so neither is an
    error response.

    The import stays function-local: ``kiro_crew.agent`` imports the dashboard
    handlers, and resolving the name at call time is what lets a test that
    substitutes ``kiro_crew.agent.rebuild_agent_config`` reach this call.
    """
    try:
        from kiro_crew.agent import rebuild_agent_config_reporting

        _spec_path, wrote = rebuild_agent_config_reporting()
    except Exception:
        logger.warning("agent spec rebuild failed after %s", context, exc_info=True)
        return False
    if not wrote:
        logger.warning(
            "agent spec was not rewritten after %s: this instance may not write the shared "
            "agent home, so new sessions may keep the previous spec",
            context,
        )
    return wrote


# ── Optional-extra install advice ──
# Two handler modules need these: `core` for the [voice] extra behind
# Speech-to-Text, and `messaging` for the per-channel SDK extras ([feishu] ->
# lark-oapi, [teams] -> PyJWT, [whatsapp] -> neonize). They live here rather than
# in either one so neither handler module has to import the other.


def _pip_install_channel_available() -> bool:
    """True when ``<gateway python> -m pip install`` can plausibly succeed.

    Thin wrapper over :func:`kiro_crew.extras.pip_install_channel_available`,
    which owns the predicate because it also renders the command the predicate
    governs -- `doctor` asks the same question about the same command, and two
    copies of "can pip install here" would drift apart.

    Touches the filesystem (``find_spec``, then the marker file), so call it
    from a worker thread on an async path.
    """
    return extras.pip_install_channel_available()


def pip_extra_install_command(extra: str) -> str:
    """The command that installs *extra*'s dependencies into THIS gateway's python.

    Thin wrapper over :func:`kiro_crew.extras.pip_install_command`, which owns
    the two things that make this string correct: it names the extra's real
    distributions rather than ``kirocrew[extra]`` (this project is not on any
    index, so that form cannot resolve for anyone), and it spells out the
    interpreter so the install cannot land in a different environment than the
    one that has to import it.

    Empty for an extra this build does not declare -- callers already treat an
    empty command as "no install channel" and show the unsupported notice.
    """
    return extras.pip_install_command(extra)
