"""The notice naming a named agent's ``model`` pin a claude session does not apply.

On ``acp_backend: claude`` a spec's ``model`` pin is applied when it is a Claude
Code id; one spelled for another harness is scoped out (``model_scope``), and is
named here so it is not dropped silently (R-SPEC-1 in
``docs/architecture/harness-requirements.md``). The check reads the shared
advertised-model cache, not the live session's list, so the two can disagree for a
session whose list moved since. The line echoes no spec text.

The spec's ``tools`` list needs no line: the session's ``tools`` option carries it
(``claude_worker_tools.claude_agent_builtin_tools``).

Notice only: nothing here refuses a spawn.
"""

from __future__ import annotations

from typing import Any

__all__ = ["spec_builtin_tools_notice", "spec_model_pin_unapplied"]


def _crew_written_agents() -> frozenset[str]:
    """The agents whose spec Crew writes: ``AGENT_FILENAME`` and every ``*_AGENT_FILENAME``
    in ``agent_files``."""
    from kiro_crew import agent_files

    return frozenset(
        value[: -len(".json")]
        for name, value in vars(agent_files).items()
        if (name == "AGENT_FILENAME" or name.endswith("_AGENT_FILENAME"))
        and isinstance(value, str)
        and value.endswith(".json")
    )


def spec_model_pin_unapplied(
    spec: dict[str, Any], *, namespace: str, worker_session: bool = False
) -> bool:
    """Whether a claude session running *spec* leaves its ``model`` pin unapplied.

    *namespace* is the session's model-id namespace
    (``SessionCapabilities.model_id_namespace``). *worker_session* is a Bot's
    worker, whose model its Bot decides, so it is never named.
    """
    from kiro_crew.agent_discovery import spec_model
    from kiro_crew.model_scope import DEFAULT_MODEL, pin_applies

    if worker_session:
        return False
    pin = spec_model(spec)
    return bool(pin) and pin != DEFAULT_MODEL and not pin_applies(pin, namespace)


def spec_builtin_tools_notice(
    agent_id: str,
    project_dir: str | None,
    *,
    backend: str,
    namespace: str,
    session_key: str = "",
) -> str:
    """One line naming *agent_id*'s ``model`` pin this session does not apply, or ``""``.

    Only for ``acp_backend: claude``, and never for an agent whose spec Crew
    writes (:func:`_crew_written_agents`). Read through the
    spec reader the hooks use (``spec_hooks._agent_spec``), so it is the spec the
    session runs. Blocking; raises when the spec cannot be read.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE

    on_claude = backend == ACP_BACKEND_CLAUDE
    if not on_claude or not agent_id or agent_id in _crew_written_agents():
        return ""
    from kiro_crew.agent_sdk.spec_hooks import _agent_spec
    from kiro_crew.members import is_crewmate_worker_session

    spec = _agent_spec(agent_id, project_dir, session_key)
    if not spec_model_pin_unapplied(
        spec, namespace=namespace, worker_session=is_crewmate_worker_session(session_key)
    ):
        return ""
    return (
        "On the claude backend this agent's spec is not applied in full: "
        "its `model` pin is not a Claude Code model id, so it is not applied."
    )
