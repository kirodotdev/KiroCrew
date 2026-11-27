"""The session-start refusal for a contested agent name (R-SPEC-13).

Imported by :mod:`kiro_crew.acp.session_mcp` under its own name, so callers reach it
as ``session_mcp.refuse_contested_agent_spec``.
"""

from __future__ import annotations

from pathlib import Path


def refuse_contested_agent_spec(agent: str | None) -> None:
    """Refuse a start when differing user-level specs declare *agent*. Blocking.

    :func:`~kiro_crew.acp.session_mcp._user_agent_spec` answers such a contest with
    ``None``, so a host that reads its servers from there would start on Crew's
    control plane alone. This is
    the one start check for every host that does not resolve the spec itself
    (kiro-cli's native projection and KAS already refuse): it asks the resolver KAS
    starts from, :func:`~kiro_crew.agent_discovery.declared_spec_claim`, and lets
    its :class:`~kiro_crew.agent_discovery.AmbiguousAgentSpecError` propagate,
    a pin that names none of the claimants included. That message names every
    file and the pin command, and the start's caller wraps it so
    ``ambiguity_meta`` finds it on the cause chain.

    With no pin, a claimant found and a spec file held open past the read retry
    (Windows), the start is refused too: that file may be a second, differing
    claimant that a later read sees. Its error names each held file and the pin
    commands but carries no ``paths``, so no chooser card replaces its text: which
    agent a held file declares is unknown. Any pin for *agent* waives this check.

    No refusal, the start goes on as before, for: one file, identical copies, a
    pin that picks a claimant, no file at all, or a directory that cannot be read.
    A file every reader is refused (an ACL, a broken document) is not held.
    A checkout spec for *agent* is no exemption: a mirrored session never lets an
    untrusted checkout pick its servers
    (:func:`~kiro_crew.acp.session_mcp._project_mcp_trusted`), so it still reads the
    user level, and the contest would still drop its servers.
    """
    from kiro_crew import agent_spec_pins
    from kiro_crew.agent import is_registered_agent_name, kiro_agents_dir_path
    from kiro_crew.agent_discovery import (
        AmbiguousAgentSpecError,
        declared_spec_claim,
        held_spec_refusal,
    )
    from kiro_crew.config.paths import spec_dir_for

    if not agent or not is_registered_agent_name(agent):
        return
    agents_dir = spec_dir_for(agent, kiro_agents_dir_path())
    pins = agent_spec_pins.spec_pins()
    held: list[Path] = []
    try:
        claim = declared_spec_claim(
            agents_dir,
            agent,
            operation="session_start_spec_check",
            source="acp",
            pins=pins,
            held=held,
        )
    except AmbiguousAgentSpecError as exc:
        # No claimant on disk is the "no agent file" case, which is not refused here.
        if exc.paths:
            raise
        return
    except OSError:
        return
    refusal = held_spec_refusal(agent, claim, held, pins)
    if refusal is not None:
        raise refusal
