"""The ACP-layer pieces native KAS workflows need, behind the agent SDK boundary.

``kiro_crew.kas_workflows`` is application code: it reaches the backend through
``kiro_crew.agent_sdk`` and never imports ``kiro_crew.acp`` itself
(``scripts/check_agent_sdk_boundary.py``). The things it needs from the
driver live here, each imported lazily so loading this module starts nothing:

- :func:`user_input_method` -- the JSON-RPC method of KAS's structured question
  (``_kiro/userInput``), which picks the "no answer" result a question gets;
- :func:`user_input_no_answer` -- that "no answer" result, from the driver's own
  ``_kiro/userInput`` reply builder;
- :func:`reject_option_id` -- the least-destructive reject option a permission
  request advertises, for refusing a step ask;
- :func:`spawn_query_runtime` -- a session-less KAS process for catalog and
  history reads.
"""

from __future__ import annotations

from typing import Any


def user_input_method() -> str:
    """The JSON-RPC method KAS sends a structured ``user_input`` question on."""
    from kiro_crew.acp.kas_wire import METHOD_USER_INPUT

    return METHOD_USER_INPUT


def user_input_no_answer(*, person: bool) -> dict[str, Any]:
    """The ``_kiro/userInput`` reply for a question nobody answered.

    The driver's own builder (``acp.elicitation.user_input_response``): always
    ``answered``, with text saying a person declined (*person*) or that no person
    saw it, so KAS never hands the model a reply it reads as yes.
    """
    from kiro_crew.acp.elicitation import (
        ELICITATION_CANCEL,
        ELICITATION_DECLINE,
        user_input_response,
    )

    return user_input_response(ELICITATION_DECLINE if person else ELICITATION_CANCEL, person=person)


def reject_option_id(params: dict) -> str | None:
    """The least-destructive reject optionId *params* advertises, or None."""
    from kiro_crew.acp._dispatch import reject_option_id as _reject_option_id

    return _reject_option_id(params)


async def spawn_query_runtime(sandbox_mode: str) -> Any:
    """Spawn and return a session-less KAS runtime (no MCP reports expected)."""
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    runtime = AcpRuntime(
        sandbox_mode=sandbox_mode,
        acp_backend=ACP_BACKEND_KAS,
        expect_mcp_reports=False,
    )
    await runtime.spawn()
    return runtime
