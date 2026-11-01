"""``/model``, ``/tools`` and ``/mcp`` answered by the dashboard on a harness with no
command channel (``ACP_BACKENDS_LOCAL_HARNESS_COMMANDS``, which is KAS).

kiro-cli runs these three itself. KAS has no command verb, so the slash gate would
otherwise only say the command does not work. Each answer here is read from state
the dashboard already holds, never from the model, and costs no turn:

* ``/model`` reads the slot's pin and the session's advertised list; ``/model <id>``
  runs the model picker's own transaction (:func:`switch_slot_model`), so a busy
  session defers the pick exactly as the picker does.
* ``/tools`` reads the agent entry the session runs now, out of the batch it
  registered with KAS, as names (``agent_sdk.kas_tool_view``): its ``tools``, ``excludedTools``, the ``allow`` rules
  of its ``permissions`` and the built-ins Crew answers itself.
* ``/mcp`` reads the slot's MCP session report (``mcp_report_payload``), the same
  sanitized payload the dashboard's MCP panel draws.

Only names reach the transcript. A server's ``env``, ``headers``, ``command`` and
``args`` are never read, and the whole reply goes through :func:`redact` last.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.agent_sdk.drivers.acp import advertised_model_ids
from kiro_crew.agent_sdk.kas_tool_view import clean_names, kas_tool_view
from kiro_crew.security import redact

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

#: The commands this module answers.
KAS_LOCAL_INSPECT_COMMANDS = frozenset({"/mcp", "/model", "/tools"})


def served_slash_commands(backend: str, dashboard_served: frozenset[str]) -> frozenset[str]:
    """The commands the dashboard serves for *backend*, a harness with no command channel.

    This module reads KAS's own registration, so its commands are served on KAS
    alone; any other member of ``ACP_BACKENDS_LOCAL_HARNESS_COMMANDS`` gets the
    notice for them, and the ``/`` menu leaves them out.
    """
    if backend == ACP_BACKEND_KAS:
        return dashboard_served
    return dashboard_served - KAS_LOCAL_INSPECT_COMMANDS


# Names listed per line, so one huge catalog or roster cannot flood the transcript.
_INSPECT_LIST_CAP = 60


def _inspect_code_list(values: list[str]) -> str:
    shown = ", ".join(f"`{v}`" for v in values[:_INSPECT_LIST_CAP])
    more = len(values) - _INSPECT_LIST_CAP
    return f"{shown}, and {more} more" if more > 0 else shown


async def _inspect_live_provider(state: DashboardState, session_key: str) -> Any:
    """The session's provider when it is positively alive, else ``None`` (peeked, never created)."""
    provider = state.sessions.get_provider(session_key)
    if provider is None or await state.sessions.is_provider_alive(session_key) is not True:
        return None
    return provider


def _inspect_catalog_model_ids() -> list[str]:
    """The signed-in account's recorded KAS catalog, as the model picker lists it.

    Blocking (reads the vault); call off the loop.
    """
    from kiro_crew.agent_sdk import kas_model_catalog

    rows = kas_model_catalog.recorded_catalog_snapshot(kas_model_catalog.vault_identity())
    return clean_names([row.get("model_name") for row in rows or () if isinstance(row, dict)])


async def _inspect_picker_catalog_ids(state: DashboardState) -> list[str]:
    """The ``--list-models`` catalog the picker serves on the kiro-cli relay route.

    Read from the picker's own in-memory cache (``handlers.agents._catalog_cache``),
    never fetched here: a fetch spawns kiro-cli, and this command must not. Empty
    until the picker has loaded once. Narrowed by the picker's own
    ``_entitled_kiro_models`` (which reads only the request's app state), so a model
    another live session's entitlement withholds is left out here too. Where the
    picker would answer its degraded 503 (a revalidation in flight), this lists none.
    """
    from kiro_crew.dashboard.handlers import agents as picker

    rows = [row for row in picker._catalog_cache.models or () if isinstance(row, dict)]
    if not rows:
        return []
    try:
        rows = await picker._entitled_kiro_models(
            cast(Any, SimpleNamespace(app={"state": state})), rows
        )
    except Exception:  # noqa: BLE001 - incl. EntitlementRevalidating: the picker's 503
        return []
    return clean_names(
        [row.get("model_id") or row.get("model_name") for row in rows if isinstance(row, dict)]
    )


async def _inspect_available_model_ids(state: DashboardState, provider: Any) -> list[str]:
    """What the picker offers: the live session's own list, else the catalog the picker
    serves on this route (the relay route's ``--list-models`` cache, or the bundle
    route's recorded KAS select)."""
    if provider is not None:
        try:
            ids = advertised_model_ids(provider.available_models())
        except Exception:  # noqa: BLE001 - a provider without the list offers none
            ids = []
        if ids:
            return clean_names(ids)
    try:
        ids = await _inspect_picker_catalog_ids(state)
    except Exception:  # noqa: BLE001 - an unreadable cache offers none
        ids = []
    if ids:
        return ids
    try:
        return await asyncio.to_thread(_inspect_catalog_model_ids)
    except Exception:  # noqa: BLE001 - an unreadable catalog offers none
        return []


async def _inspect_model_text(
    state: DashboardState, slot: _ChatSlot, session_key: str, argument: str
) -> tuple[str, str]:
    if argument:
        return await _inspect_switch_model_text(state, slot, argument)
    current = slot.model or "auto"
    served = str(getattr(slot, "served_model", "") or "")
    line = f"🧠 Model: `{current}`"
    if served and served != current:
        line += f" (running `{served}`)"
    lines = [line]
    ids = await _inspect_available_model_ids(
        state, await _inspect_live_provider(state, session_key)
    )
    if ids:
        lines.append(f"Available: {_inspect_code_list(ids)}")
    else:
        lines.append("The model list is not loaded yet. It loads when the session starts.")
    lines.append("Switch with `/model <id>`, or use the model button under the message box.")
    return "\n".join(lines), "shown"


async def _inspect_switch_model_text(
    state: DashboardState, slot: _ChatSlot, argument: str
) -> tuple[str, str]:
    # Call-time import: chat_handlers imports this package.
    from kiro_crew.dashboard.chat_handlers import SlotAgentSwitchCaller, switch_slot_model

    resp = await switch_slot_model(
        state, slot, slot.key, argument, SlotAgentSwitchCaller.for_turn(slot)
    )
    try:
        body = json.loads(resp.text or "{}")
    except (TypeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    if resp.status == 200 and body.get("ok"):
        model = str(body.get("model") or "auto")
        if body.get("deferred"):
            text = f"🧠 Model set to `{model}`. It applies when the current turn ends."
        else:
            text = f"🧠 Model set to `{model}`."
        warning = str(body.get("warning") or "")
        if warning:
            text += f"\n⚠ {warning}"
        return text, "switched"
    reason = str(body.get("error") or "the switch was refused")
    return f"⚠ Could not switch the model to `{argument}`: {reason}", "error"


async def _inspect_tools_text(state: DashboardState, session_key: str) -> tuple[str, str]:
    provider = await _inspect_live_provider(state, session_key)
    if provider is None:
        return (
            "🔧 No tool list yet: this chat has no running session. "
            "Send a message first, then run `/tools` again.",
            "no_session",
        )
    view = kas_tool_view(provider)
    if view is None:
        return (
            "🔧 This session holds no agent batch registered with KAS, "
            "so there is no tool list for it here.",
            "no_batch",
        )
    entry = view.get("entry")
    if not isinstance(entry, dict):
        mode_id = view.get("mode_id")
        mode = f" (`{mode_id}`)" if isinstance(mode_id, str) and mode_id else ""
        return (
            f"🔧 This session runs an agent{mode} that Kiro Crew did not register, "
            "so there is no tool list for it here.",
            "unregistered_agent",
        )
    agent = str(entry.get("id") or "")
    lines = [f"🔧 Tools for agent `{agent}`:" if agent else "🔧 Tools:"]
    tools = entry.get("tools")
    if tools == "*":
        lines.append("- Tools: all tools (`*`)")
    else:
        names = tools if isinstance(tools, list) else []
        lines.append(f"- Tools: {_inspect_code_list(names)}" if names else "- Tools: none")
    excluded = entry.get("excluded") or []
    if excluded:
        lines.append(f"- Not offered: {_inspect_code_list(excluded)}")
    server_names = entry.get("servers") or []
    if server_names:
        lines.append(f"- MCP servers in this agent: {_inspect_code_list(server_names)}")
    approved = entry.get("approved") or []
    lines.append(
        f"- Runs without asking: {_inspect_code_list(approved)}"
        if approved
        else "- Runs without asking: none"
    )
    answered = entry.get("crew_answered") or []
    if answered:
        lines.append(f"- Runs without asking after Crew's checks: {_inspect_code_list(answered)}")
    lines.append("Everything else asks first. `/mcp` shows each MCP server's state.")
    return "\n".join(lines), "shown"


def _inspect_mcp_text(slot: _ChatSlot, declared: list[str] | None = None) -> tuple[str, str]:
    """Each MCP server with its state from the slot's session report.

    *declared* are the servers the session's active agent entry declares
    (``kas_tool_view``). A declared server the report has not named reads "not
    reported yet", never "not mounted": a report missing a name is not proof the
    server is absent (``mcp_session_report``).
    """
    report = slot.mcp_report_payload()
    if not isinstance(report, dict):
        return (
            "🔌 No MCP report yet: this chat has no running session. "
            "Send a message first, then run `/mcp` again.",
            "no_session",
        )
    ready = set(clean_names(report.get("ready")))
    failed = set(clean_names(report.get("failed")))
    awaiting = set(clean_names(report.get("awaiting_auth")))
    no_tools = set(clean_names(report.get("no_tools")))
    restart = set(clean_names(report.get("restart_to_load")))
    raw_failures = report.get("failures")
    failures: dict[str, Any] = raw_failures if isinstance(raw_failures, dict) else {}
    names = clean_names(
        [
            *clean_names(report.get("configured")),
            *(declared or []),
            *sorted(ready),
            *sorted(failed),
            *sorted(awaiting),
            *sorted(no_tools),
        ]
    )
    if not names:
        return "🔌 This session has no MCP servers.", "shown"
    lines = ["🔌 MCP servers:"]
    for name in names[:_INSPECT_LIST_CAP]:
        if name in failed:
            reason = failures.get(name) if isinstance(failures.get(name), str) else ""
            status = f"failed: {reason}" if reason else "failed"
        elif name in awaiting:
            status = "waiting for sign-in"
        elif name in ready and name in no_tools:
            status = "ready, but it offers no tools"
        elif name in ready:
            status = "ready"
        else:
            status = "not reported yet"
        if name in restart:
            status += " (start a new session to load it)"
        lines.append(f"- `{name}`: {status}")
    if len(names) > _INSPECT_LIST_CAP:
        lines.append(f"- and {len(names) - _INSPECT_LIST_CAP} more")
    return "\n".join(lines), "shown"


async def answer_kas_inspect_command(
    state: DashboardState, slot: _ChatSlot, session_key: str, message: str
) -> tuple[str, str]:
    """The reply text and the recorded outcome for one of :data:`KAS_LOCAL_INSPECT_COMMANDS`."""
    parts = message.strip().split(maxsplit=1)
    command = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    if command == "/model":
        text, outcome = await _inspect_model_text(state, slot, session_key, rest.strip())
    elif command == "/tools":
        text, outcome = await _inspect_tools_text(state, session_key)
    else:
        view = kas_tool_view(await _inspect_live_provider(state, session_key))
        entry = view.get("entry") if isinstance(view, dict) else None
        declared = entry.get("servers") if isinstance(entry, dict) else None
        text, outcome = _inspect_mcp_text(slot, declared if isinstance(declared, list) else None)
    return redact(text), outcome
