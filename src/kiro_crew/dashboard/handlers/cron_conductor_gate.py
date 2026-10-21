"""The cron tools' gate for a crewmate's conductor chat.

A crewmate's conductor answers, reads and plans; scheduled code is work, so a
``cron_add`` that carries a ``script`` or ``command`` from a conductor caller is
refused here, gateway-side, by the caller's attested execution record. Message
reminders pass, and so does every other caller. Called by
``dashboard.handlers.cron.api_cron_tools`` before dispatch.
"""

from __future__ import annotations

from aiohttp import web

#: The cron tool arguments that make a job run code rather than send a message.
_SCRIPTED_JOB_FIELDS = ("script", "command")

#: What a crewmate's conductor is told when it asks for a scripted job.
CONDUCTOR_SCRIPTED_JOB_REFUSAL = (
    "A crewmate's own chat schedules message reminders only. Hand scripted or "
    "command work to your worker (session_create, then session_send); it can "
    "schedule the job itself."
)


async def refuse_conductor_scripted_job(
    request: web.Request, name: str, arguments: dict
) -> web.Response | None:
    """Refuse a ``script`` or ``command`` job from a crewmate's conductor chat.

    Decided on the caller's attested execution record (``member_request_scope``,
    already resolved for this request), never on what the model passes: a
    conductor answers, reads and plans, and scheduled code is work. A
    message-only reminder passes, and so does every other caller.
    """
    if name != "cron_add" or not any(arguments.get(k) for k in _SCRIPTED_JOB_FIELDS):
        return None
    from kiro_crew.dashboard.handlers._shared import member_request_scope
    from kiro_crew.mate_worker import is_conductor_execution

    scope = await member_request_scope(request)
    if not is_conductor_execution(getattr(scope, "execution", None)):
        return None
    return web.json_response(
        {"error": CONDUCTOR_SCRIPTED_JOB_REFUSAL, "code": "conductor_scripted_job"},
        status=403,
    )
