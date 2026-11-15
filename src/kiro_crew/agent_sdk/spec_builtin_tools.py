"""The notice naming what of a named agent's ``tools`` and ``model`` a claude session
does not apply.

On ``acp_backend: claude`` a spec's ``tools`` list narrows the MCP array only
(``agent-spec-fields.md``, note 3): Claude Code's own built-ins are outside it, so a
spec that lists only ``read`` still runs with Bash, Edit, Write and the rest.
``tools: []``, a Bot's worker and a Side Chat session are the cases the options
writer does narrow (``acp.client_claude_session``), so they get no notice. A
``model`` pin is applied when it is a Claude Code id; one spelled for another
harness is scoped out (``model_scope``). Both are named here, so neither is dropped
silently (R-SPEC-1, R-SPEC-5 in ``docs/architecture/harness-requirements.md``).

The line fires when the list leaves out a kiro-cli tool of
:data:`claude_worker_tools.CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL`, so a list that
grants every row gets none. It names no tool, neither the spec's entries nor a
harness's built-ins, which change between releases. The model check reads the shared advertised-model cache, not the live
session's list, so the two can disagree for a session whose list moved since.

Notice only: nothing here refuses a spawn or narrows a tool. Applying the list to
the built-ins would refuse calls a session makes today, which is a security
owner's call.
"""

from __future__ import annotations

from typing import Any

__all__ = ["spec_builtin_tools_notice", "spec_unapplied_builtin_limits"]

#: ``tools`` entries that grant every built-in, so the list limits none of them.
_ALL_BUILTINS = frozenset({"*", "@builtin", "@builtin/*"})


def _ungranted_builtins(tools: Any) -> tuple[str, ...]:
    """The kiro-cli tools with a claude built-in that a spec's ``tools`` value does not grant.

    Empty for ``[]`` (the zero-tool case the options writer applies), a missing or
    non-list value, a list with no string entry, or a list with an entry that
    grants every built-in.
    """
    from kiro_crew.agent_sdk.claude_worker_tools import (
        CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL,
        excluded_builtin_rows,
    )

    entries = [t for t in tools if isinstance(t, str)] if isinstance(tools, list) else []
    if not entries or any(t.strip() in _ALL_BUILTINS for t in entries):
        return ()
    granted = {row for entry in entries for row in excluded_builtin_rows(entry)}
    return tuple(row for row in CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL if row not in granted)


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


def spec_unapplied_builtin_limits(
    spec: dict[str, Any], *, namespace: str, worker_session: bool = False
) -> tuple[tuple[str, ...], bool]:
    """``(ungranted_tools, model_unapplied)`` for a claude session running *spec*.

    *namespace* is the session's model-id namespace
    (``SessionCapabilities.model_id_namespace``). *worker_session* is a Bot's
    worker, whose native entries the options writer maps onto the built-ins and
    whose model its Bot decides, so it gets neither.
    """
    from kiro_crew.agent_discovery import spec_model
    from kiro_crew.model_scope import DEFAULT_MODEL, pin_applies

    if worker_session:
        return (), False
    ungranted = _ungranted_builtins(spec.get("tools"))
    pin = spec_model(spec)
    model_unapplied = bool(pin) and pin != DEFAULT_MODEL and not pin_applies(pin, namespace)
    return ungranted, model_unapplied


def spec_builtin_tools_notice(
    agent_id: str,
    project_dir: str | None,
    *,
    backend: str,
    namespace: str,
    session_key: str = "",
) -> str:
    """One line naming what of *agent_id*'s ``tools`` and ``model`` this session
    does not apply, or ``""``.

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
    ungranted, model_unapplied = spec_unapplied_builtin_limits(
        spec, namespace=namespace, worker_session=is_crewmate_worker_session(session_key)
    )
    parts: list[str] = []
    if ungranted:
        parts.append(
            "this backend does not apply the spec's `tools` list to its built-in "
            "tools, so the harness's full built-in tool set is in effect; the list "
            "still limits its MCP servers"
        )
    if model_unapplied:
        parts.append("its `model` pin is not a Claude Code model id, so it is not applied")
    if not parts:
        return ""
    return (
        "On the claude backend this agent's spec is not applied in full: " + "; ".join(parts) + "."
    )
