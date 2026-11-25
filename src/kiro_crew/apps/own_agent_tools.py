"""The tool gate's grant for a first-party app's own agent calling that app's own tools.

An app's manifest ``agentTools`` block declares MCP tools the app serves from its own
process; ``apps/app_agent_tools.py`` reads the blocks of first-party apps only, and the
``kirocrew-core`` server lists them. The default agent approves that whole server, so a
person's chat already calls these tools unasked. An app's own agent carries no such
approval, so without this grant each of its calls to the app's own tool asks, and an
unattended run (the app's cron) is refused when the card expires with nobody there.

:func:`gate_grants` is the ``app-own-tools`` row of the gate (``hooks._tier_app_own_tools``).
It runs after every deny row and governance, so a ceiling, a profile or a deny rule on
the tool still refuses the call. It approves only when every fact agrees, and each
fact comes from the gateway or from a shipped manifest, never from a name the app, the
agent or the model chose:

* the run is the app's: ``facts.app`` (a slot's ``_app``, set only from an
  authenticated app scope) or, for a run with no slot, the app half of
  ``ToolCall.app_run`` (:func:`app_run_of`: a cron job's host-written ``created_by``
  stamp);
* the agent that ACTUALLY ran is one that same app's shipped manifest declares
  (``hooks._builtin_app_for_agent`` on the resolved agent). The slot's or job's agent
  NAME is an alias that may resolve to the owner's own agent, so it is never read;
* the call is ``@kirocrew-core/<tool>`` by its VERIFIED identity, never by title;
* that same app declares the tool (``app_agent_tools.declared_tools``, minus any name a
  core tool has), read fresh, so a tool another app declares, a core tool, a built-in
  and a shell call keep their card;
* the app is readably enabled, and no operator ``confirm_tools`` pattern names the call.

Which declarations count is ``app_agent_tools``' rule, unchanged: a built-in's block is
read from the wheel, and an adopted former built-in's block is kept only up to its wheel
copy, so a tool added to an installed tree is not declared and asks. A third-party
app's block is not read at all, so its agents keep their card.
"""

from __future__ import annotations

from typing import Any

#: The MCP server every declared app tool is listed under.
_CORE_SERVER = "kirocrew-core"


def app_run_of(job: object, client: object) -> tuple[str, str]:
    """``ToolCall.app_run`` for a cron run: the job's app stamp and the agent that ran.

    The app is read from ``created_by``, which the host writes from a verified app
    claim (``CronSDK``, the REST create) and no request body sets; a person's job, and
    a job an agent made with ``cron_add``, carries none and yields ``""``. The agent is
    what the client was started on, never the job's ``agent`` field.
    """
    from kiro_crew.apps.cron_sdk import app_owner_name
    from kiro_crew.dashboard.handlers.usage import read_effective_agent

    return app_owner_name(getattr(job, "created_by", "")), read_effective_agent(client)


def gate_grants(facts: Any) -> bool:
    """Whether the gate's ``app-own-tools`` row approves this call. Fails toward the card."""
    call = facts.call
    app, agent = (facts.app, call.resolved_agent) if facts.app else call.app_run
    if not (app and agent):
        return False
    if not (
        call.identity_trusted
        and call.mcp_server == _CORE_SERVER
        and call.mcp_tool
        and facts.canonical_mcp_name
    ):
        return False
    # Call time: the facade imports nothing from here at module level.
    from kiro_crew import hooks  # noqa: PLC0415

    if hooks._builtin_app_for_agent(agent) != app:
        return False
    ref = hooks.mcp_identity_ref(call.mcp_server, call.mcp_tool)
    if hooks._operator_confirms(facts, (f"Running: {ref}", ref)):
        return False
    from kiro_crew.mcp_tools.catalog import app_tools  # noqa: PLC0415

    if not any(t.name == call.mcp_tool and t.app == app for t in app_tools._tools()):
        return False
    return app_tools.app_enabled_state(app) is True
