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
* ``/context`` reads the context meter's own reading (``_context_snapshot_fields``):
  how full the window is, in percent and, when measured, in tokens.
* ``/usage`` reads the credit reading the dashboard's credit counter shows, under
  the counter's own identity rules (``cached_usage_for_current_signin``). It never
  fetches one.
* ``/help`` lists the commands this chat runs, as the ``/`` menu lists them here
  (:func:`local_harness_menu`), with their descriptions.
* ``/agent`` and ``/agent list`` show the chat's agent and the agents a switch can
  reach (``pickable_agent_names``, the list the channel ``/agent`` shows).
* ``/hooks`` lists the hooks a turn of this chat fires: the Hooks page's store
  (``state._hook_store``) and the agent spec's own (``turn_spec_hooks``), by name,
  event and matcher, never by command.
* ``/context add|remove|clear``, ``/knowledge`` and ``/code status|init`` are sent
  to KAS's own verbs (``_kiro/session/context``, ``_kiro/knowledge``,
  ``_kiro/codeIntelligence``) through the session (``kas_command_call``), and KAS's
  answer is shown. A path is made absolute against the session's folder and must
  pass Crew's sensitive-path check (``sensitive_path_refusal``) before it is sent;
  a wildcard is refused, since each match cannot be checked. ``/knowledge`` is not
  in the ``/`` menu: a user's own quick prompt may still carry that name.

The other ``/agent`` subcommands, ``/tools`` with an argument and the other
``/code`` subcommands change state kiro-cli keeps or need a verb KAS lacks;
:func:`answers_locally` leaves them to the notice.

Only names reach the transcript. A server's ``env``, ``headers``, ``command`` and
``args`` are never read, and the whole reply goes through :func:`redact` last.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shlex
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.agent_sdk.drivers.acp import advertised_model_ids
from kiro_crew.agent_sdk.kas_tool_view import clean_names, kas_tool_view
from kiro_crew.security import redact
from kiro_crew.security.paths import is_unverifiable_path_refusal, sensitive_path_refusal

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

#: The commands this module answers (see :func:`answers_locally` for their arguments).
KAS_LOCAL_INSPECT_COMMANDS = frozenset(
    {
        "/agent",
        "/code",
        "/context",
        "/help",
        "/hooks",
        "/knowledge",
        "/mcp",
        "/model",
        "/tools",
        "/usage",
    }
)

#: kiro-cli commands that are not Crew harness commands (``_SLASH_COMMANDS``), so on
#: KAS they would reach the model as plain text. The slash gate takes them on KAS
#: alone: ``/knowledge`` is answered here, the rest have no KAS verb and get the
#: "doesn't work with KAS" notice.
KAS_KIRO_CLI_ONLY_COMMANDS = frozenset({"/effort", "/knowledge", "/plan", "/rewind"})

# ``/context`` words sent to KAS's ``_kiro/session/context``; ``show`` is the bare form.
_CONTEXT_EDIT_WORDS = frozenset({"add", "remove", "rm", "clear"})
_CONTEXT_WORDS = _CONTEXT_EDIT_WORDS | {"", "show"}
# ``/knowledge`` words, as KAS's ``_kiro/knowledge`` spells them; bare is ``show``.
_KNOWLEDGE_WORDS = frozenset({"", "show", "add", "remove", "update", "clear", "cancel"})
# ``/code`` words sent to KAS's ``_kiro/codeIntelligence``; bare is ``status``.
_CODE_WORDS = frozenset({"", "status", "init"})

# ``/agent`` words answered here: the bare command and ``list``. The rest
# (``create``, ``set-default``, ...) edit agent files kiro-cli owns.
_AGENT_SHOW_WORDS = frozenset({"", "list"})


def served_slash_commands(backend: str, dashboard_served: frozenset[str]) -> frozenset[str]:
    """The commands the dashboard serves for *backend*, a harness with no command channel.

    This module reads KAS's own registration, so its commands are served on KAS
    alone; any other member of ``ACP_BACKENDS_LOCAL_HARNESS_COMMANDS`` gets the
    notice for them, and the ``/`` menu leaves them out.

    ``/agent`` stays: ``/agent <name>`` is Crew's own switch on every harness, so
    only its bare and ``list`` forms get the notice there.
    """
    if backend == ACP_BACKEND_KAS:
        return dashboard_served
    return dashboard_served - (KAS_LOCAL_INSPECT_COMMANDS - {"/agent"})


def local_harness_menu(backend: str) -> frozenset[str]:
    """What the ``/`` menu lists, and ``/help`` prints, on a harness with no command
    channel: the commands the dashboard serves for *backend*, plus ``/compact``,
    which has a gate of its own. ``GET /api/slash-commands`` and ``/help`` both read
    this one function. On a harness with an ``ACP_BACKEND_PROMPT_HANDLER_COMMANDS``
    row, ``GET /api/slash-commands`` lists this and that row."""
    from kiro_crew.dashboard.chat_utils import (
        _BLOCKED_SLASH_COMMANDS,
        _DASHBOARD_SERVED_SLASH_COMMANDS,
    )

    served = served_slash_commands(backend, _DASHBOARD_SERVED_SLASH_COMMANDS)
    return (served | {"/compact"}) - _BLOCKED_SLASH_COMMANDS


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


def _amount(number: float) -> str:
    """A credit or token figure: whole numbers bare, others to two decimals."""
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}"


def _number(value: Any) -> float | None:
    """*value* as a finite float, else ``None`` (a bool is not a number here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


async def _inspect_context_text(state: DashboardState, slot: _ChatSlot) -> tuple[str, str]:
    """How full the context window is, from the reading the context meter draws."""
    # Call-time import, through the facade that composes ``chat_api.slot_detail``
    # (chat_handlers imports this package).
    from kiro_crew.dashboard.chat_handlers import _context_snapshot_fields

    fields = await _context_snapshot_fields(state, slot)
    pct = _number(fields.get("context_pct"))
    if pct is None:
        return (
            "📊 No context reading yet. Send a message first, then run `/context` again.",
            "no_reading",
        )
    line = f"📊 Context: {round(pct, 1):g}% full"
    used = _number(fields.get("context_used_tokens"))
    window = _number(fields.get("context_window_tokens"))
    if used and window:
        line += f" ({_amount(used)} of {_amount(window)} tokens)"
    elif window:
        line += f" of a {_amount(window)}-token window"
    lines = [line + "."]
    if fields.get("context_stale"):
        lines.append(
            "This reading is from before the session stopped. Your next message updates it."
        )
    return "\n".join(lines), "shown"


async def _inspect_usage_text() -> tuple[str, str]:
    """The account's credits, from the reading the dashboard's credit counter shows.

    Read through the counter's own identity rules
    (``cached_usage_for_current_signin``), so a reading left from another sign-in
    is never shown as this one's.
    """
    # Call-time import: the session handlers import the chat package.
    from kiro_crew.dashboard.handlers.sessions import cached_usage_for_current_signin

    usage = await cached_usage_for_current_signin()
    used = _number(usage.get("credits_used"))
    plan = _number(usage.get("credits_plan"))
    if used is None or not plan:
        if usage.get("reason") == "signin_required":
            return "💳 No credit reading: sign in again to see your credits.", "signin_required"
        return "💳 No credit reading yet. Try `/usage` again in a few minutes.", "no_reading"
    line = f"💳 Credits: {_amount(used)} of {_amount(plan)} used"
    pct = _number(usage.get("percentage"))
    if pct is not None:
        line += f" ({round(pct, 1):g}%)"
    plan_name = usage.get("plan")
    if isinstance(plan_name, str) and plan_name:
        line += f" on {plan_name}"
    lines = [line + "."]
    overage = _number(usage.get("credits_overage"))
    if overage is not None and overage > 0:
        lines.append(f"- Overage: {_amount(overage)} credits")
    resets = usage.get("resets")
    if isinstance(resets, str) and resets:
        lines.append(f"- Resets: {resets}")
    if usage.get("stale"):
        lines.append("This reading is from an earlier refresh; the latest one did not finish.")
    return "\n".join(lines), "shown"


def _inspect_help_text() -> tuple[str, str]:
    """The commands this chat runs, as the ``/`` menu lists them here."""
    from kiro_crew.dashboard.chat_utils import SLASH_COMMAND_DESCRIPTIONS

    lines = ["❓ Commands in this chat:"]
    for command in sorted(local_harness_menu(ACP_BACKEND_KAS)):
        description = SLASH_COMMAND_DESCRIPTIONS.get(command, "")
        lines.append(f"- `{command}`: {description}" if description else f"- `{command}`")
    lines.append("Type `/` in the message box to pick one.")
    return "\n".join(lines), "shown"


async def _inspect_agent_text(slot: _ChatSlot) -> tuple[str, str]:
    """The chat's agent and the agents ``/agent <name>`` can switch to."""
    from kiro_crew.agent_switch_command import pickable_agent_names

    current = str(slot.agent or "")
    try:
        names = clean_names(await asyncio.to_thread(pickable_agent_names))
    except Exception:  # noqa: BLE001 - an unreadable agent directory lists none
        names = []
    lines = [f"🤖 Agent: `{current}`" if current else "🤖 Agent: the default agent"]
    for name in names[:_INSPECT_LIST_CAP]:
        lines.append(f"- `{name}` (current)" if name == current else f"- `{name}`")
    if len(names) > _INSPECT_LIST_CAP:
        lines.append(f"- and {len(names) - _INSPECT_LIST_CAP} more")
    if not names:
        lines.append("No other agents are installed on this machine.")
    lines.append("Switch with `/agent <name>`, or use the agent picker.")
    return "\n".join(lines), "shown"


async def _inspect_hooks_text(
    state: DashboardState, slot: _ChatSlot, session_key: str
) -> tuple[str, str]:
    """The hooks a turn of this chat fires, by name, event and matcher, never by command.

    The Hooks page's store first, as ``ScriptHookStore.fire`` runs it, then the
    agent spec's own hooks (``turn_spec_hooks``), which need a running session.
    """
    from kiro_crew.agent_sdk.spec_hooks import turn_spec_hooks

    store = getattr(state, "_hook_store", None)
    hooks = list(store.list_all()) if store is not None else []
    provider = await _inspect_live_provider(state, session_key)
    note = ""
    if provider is None:
        note = "The agent's own hooks show once this chat has a running session."
    else:
        found = await turn_spec_hooks(provider, str(slot.agent or ""))
        if found.unreadable:
            note = "⚠ The agent's own hooks could not be read."
        hooks += found.hooks
    lines = ["🪝 Hooks for this chat:"]
    for hook in hooks[:_INSPECT_LIST_CAP]:
        name = str(getattr(hook, "name", "") or getattr(hook, "id", "") or "hook")
        event = str(getattr(hook, "event", "") or "")
        matcher = str(getattr(hook, "matcher", "") or "")
        line = f"- `{name}`: {event}" if event else f"- `{name}`"
        if matcher:
            line += f", for `{matcher}`"
        if getattr(hook, "enabled", True) is not True:
            line += " (off)"
        lines.append(line)
    if len(hooks) > _INSPECT_LIST_CAP:
        lines.append(f"- and {len(hooks) - _INSPECT_LIST_CAP} more")
    if not hooks:
        lines = ["🪝 No hooks are set up for this chat."]
    if note:
        lines.append(note)
    return "\n".join(lines), "shown"


#: Whether a backslash is a path separator here, not an escape (``_split_words``).
_WINDOWS = os.name == "nt"


def _split_words(rest: str) -> list[str] | None:
    """*rest* split as a shell would split it (quotes keep spaces), or None when unbalanced.

    On Windows a backslash is kept as typed, so ``C:\\work\\a.md`` stays one path.
    """
    try:
        return shlex.split(rest.replace("\\", "\\\\") if _WINDOWS else rest)
    except ValueError:
        return None


_WILDCARD_CHARS = frozenset("*?[")


def _absolute(raw: str, cwd: str) -> str | None:
    """*raw* made absolute against the session's folder, or None when it cannot be."""
    path = os.path.expanduser(raw)
    if not os.path.isabs(path):
        if not cwd:
            return None
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


def _checked_path(raw: str, cwd: str) -> tuple[str | None, str]:
    """*raw* as an absolute path KAS may read, or None and the reason it may not.

    Made absolute against the session's folder, so KAS and Crew's check read the
    same file. A wildcard is refused: Crew cannot check each file it matches.
    """
    if any(ch in _WILDCARD_CHARS for ch in raw):
        # shortcut: wildcards are refused because each match cannot be checked
        # against the sensitive-path list; expand and check matches here to allow them.
        return None, f"`{raw}`: wildcards are not supported here. Name each file or folder."
    path = _absolute(raw, cwd)
    if path is None:
        return None, f"`{raw}`: use a full path, since this session has no folder."
    refusal = sensitive_path_refusal(path)
    if refusal is not None and is_unverifiable_path_refusal(refusal):
        return None, f"`{raw}`: Kiro Crew could not check this path in time. Try again."
    if refusal is not None:
        return None, f"`{raw}`: Kiro Crew does not let the agent read this path."
    return path, ""


async def _kas_call(provider: Any, command: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """KAS's answer to *command*'s verb on *provider*'s session, or None when it cannot be sent."""
    call = getattr(provider, "kas_command_call", None)
    if call is None:
        return None
    try:
        answer = await call(command, params)
    except Exception as exc:  # noqa: BLE001 - a dead or refusing engine is shown, not raised
        return {"success": False, "message": str(exc)}
    return answer if isinstance(answer, dict) else None


def _kas_message(answer: dict[str, Any], fallback: str) -> str:
    message = answer.get("message")
    return message.strip() if isinstance(message, str) and message.strip() else fallback


_NO_SESSION = "this chat has no running session. Send a message first, then run `{0}` again."
_NO_VERB = "`{0}` doesn't work with this KAS version. It was not sent to the agent."


async def _kas_context_text(state: DashboardState, session_key: str, rest: str) -> tuple[str, str]:
    """``/context add [--force] <path>...``, ``remove <path>...`` or ``clear``, sent to KAS."""
    words = _split_words(rest)
    if not words:
        return "⚠ Could not read that command. Check its quotes.", "usage"
    sub = "remove" if words[0].lower() == "rm" else words[0].lower()
    force = any(w in ("--force", "-f") for w in words[1:])
    paths = [w for w in words[1:] if w not in ("--force", "-f")]
    if sub != "clear" and not paths:
        return f"Usage: `/context {sub} <path>`", "usage"
    if len(paths) > _INSPECT_LIST_CAP:
        return f"⚠ Name at most {_INSPECT_LIST_CAP} paths at once. Nothing was changed.", "usage"
    provider = await _inspect_live_provider(state, session_key)
    if provider is None:
        return "📎 No context change: " + _NO_SESSION.format("/context"), "no_session"
    cwd = str(getattr(provider, "cwd", "") or "")
    if sub == "clear":
        answer = await _kas_call(provider, "context", {"subcommand": "clear"})
        if answer is None:
            return "⚠ " + _NO_VERB.format("/context clear"), "unsupported_backend"
        return "📎 " + _kas_message(answer, "Context cleared."), _ok(answer)
    lines: list[str] = []
    outcome = "shown"
    for raw in paths:
        if sub == "add":
            path, refusal = _checked_path(raw, cwd)
        else:
            path = _absolute(raw, cwd)
            refusal = f"`{raw}`: use a full path, since this session has no folder."
        if path is None:
            lines.append(f"⚠ {refusal}")
            outcome = "error"
            continue
        params: dict[str, Any] = {"subcommand": sub, "path": path}
        if sub == "add" and force:
            params["force"] = True
        answer = await _kas_call(provider, "context", params)
        if answer is None:
            return "⚠ " + _NO_VERB.format(f"/context {sub}"), "unsupported_backend"
        mark = "📎" if answer.get("success") is True else "⚠"
        if answer.get("success") is not True:
            outcome = "error"
        lines.append(f"{mark} {_kas_message(answer, 'Done.')}")
    return "\n".join(lines), outcome


def _ok(answer: dict[str, Any]) -> str:
    return "shown" if answer.get("success") is True else "error"


def _flags_and_words(words: list[str]) -> tuple[dict[str, str], list[str]]:
    """``--name x`` / ``--name=x`` pairs, and the plain words, of *words*."""
    flags: dict[str, str] = {}
    plain: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if word.startswith("--") and "=" in word:
            key, value = word[2:].split("=", 1)
            flags[key] = value
        elif word.startswith("--") and i + 1 < len(words):
            flags[word[2:]] = words[i + 1]
            i += 1
        else:
            plain.append(word)
        i += 1
    return flags, plain


async def _kas_knowledge_text(
    state: DashboardState, session_key: str, rest: str
) -> tuple[str, str]:
    """``/knowledge [show|add|remove|update|clear|cancel]``, sent to KAS."""
    words = _split_words(rest)
    if words is None:
        return "⚠ Could not read that command. Check its quotes.", "usage"
    sub = words[0].lower() if words else "show"
    args = words[1:]
    params: dict[str, Any] = {"subcommand": sub}
    provider = await _inspect_live_provider(state, session_key)
    if provider is None:
        return "📚 No knowledge reading: " + _NO_SESSION.format("/knowledge"), "no_session"
    cwd = str(getattr(provider, "cwd", "") or "")
    if sub in ("add", "update"):
        flags, plain = _flags_and_words(args)
        if sub == "add":
            name = flags.get("name") or (plain.pop(0) if plain else "")
        else:
            name = ""
        raw = flags.get("path") or (plain[0] if plain else "")
        if not raw or (sub == "add" and not name):
            usage = "/knowledge add <name> <path>" if sub == "add" else "/knowledge update <path>"
            return f"Usage: `{usage}`", "usage"
        path, refusal = _checked_path(raw, cwd)
        if path is None:
            return f"⚠ {refusal}", "error"
        params["path"] = path
        if name:
            params["name"] = name
    elif sub == "remove":
        if not args:
            return "Usage: `/knowledge remove <name|path>`", "usage"
        params["target"] = args[0]
    elif sub == "cancel" and args:
        params["operationId"] = args[0]
    answer = await _kas_call(provider, "knowledge", params)
    if answer is None:
        return "⚠ " + _NO_VERB.format("/knowledge"), "unsupported_backend"
    if sub != "show" or answer.get("success") is not True:
        mark = "📚" if answer.get("success") is True else "⚠"
        return f"{mark} {_kas_message(answer, 'Done.')}", _ok(answer)
    entries = [e for e in answer.get("entries") or () if isinstance(e, dict)]
    if not entries:
        return "📚 No knowledge bases yet. Add one with `/knowledge add <name> <path>`.", "shown"
    lines = ["📚 Knowledge bases:"]
    for entry in entries[:_INSPECT_LIST_CAP]:
        name = str(entry.get("name") or entry.get("id") or "")
        line = f"- `{name}`"
        path = entry.get("path")
        if isinstance(path, str) and path:
            line += f": `{path}`"
        progress = entry.get("items_display")
        count = _number(entry.get("item_count"))
        if entry.get("indexing") and isinstance(progress, str) and progress:
            line += f" (indexing: {progress})"
        elif count is not None:
            line += f" ({_amount(count)} items)"
        lines.append(line)
    if len(entries) > _INSPECT_LIST_CAP:
        lines.append(f"- and {len(entries) - _INSPECT_LIST_CAP} more")
    return "\n".join(lines), "shown"


async def _kas_code_text(state: DashboardState, session_key: str, rest: str) -> tuple[str, str]:
    """``/code [status]`` or ``/code init``, sent to KAS's code intelligence."""
    words = rest.split()
    sub = words[0].lower() if words else "status"
    provider = await _inspect_live_provider(state, session_key)
    if provider is None:
        return "🧩 No code reading: " + _NO_SESSION.format("/code"), "no_session"
    answer = await _kas_call(provider, "code", {"subcommand": sub})
    if answer is None:
        return "⚠ " + _NO_VERB.format("/code"), "unsupported_backend"
    status = answer.get("status")
    if sub != "status" or answer.get("success") is not True or not isinstance(status, dict):
        mark = "🧩" if answer.get("success") is True else "⚠"
        return f"{mark} {_kas_message(answer, 'Done.')}", _ok(answer)
    ready = "set up" if status.get("initialized") is True else "not set up (run `/code init`)"
    lines = [f"🧩 Code intelligence: {ready}"]
    languages = clean_names(status.get("languages"))
    if languages:
        lines.append(f"- Languages: {_inspect_code_list(languages)}")
    for server in [s for s in status.get("lspServers") or () if isinstance(s, dict)][
        :_INSPECT_LIST_CAP
    ]:
        name = str(server.get("name") or "")
        state_word = str(server.get("status") or "").replace("_", " ")
        if name:
            lines.append(f"- `{name}`: {state_word}" if state_word else f"- `{name}`")
    return "\n".join(lines), "shown"


def answers_locally(message: str) -> bool:
    """Whether :func:`answer_kas_inspect_command` answers *message*.

    ``/agent`` only bare or ``list`` (the rest edit agent files kiro-cli owns),
    ``/tools`` only bare (``trust`` and the like change kiro-cli's own grants),
    ``/context``, ``/knowledge`` and ``/code`` only with a word KAS's verb takes.
    Everything else keeps the "doesn't work with KAS" notice.
    """
    parts = message.strip().split(maxsplit=1)
    command = parts[0].lower() if parts else ""
    rest = parts[1].strip().lower() if len(parts) > 1 else ""
    word = rest.split(maxsplit=1)[0] if rest else ""
    if command not in KAS_LOCAL_INSPECT_COMMANDS:
        return False
    if command == "/context":
        return word in _CONTEXT_WORDS
    if command == "/knowledge":
        return word in _KNOWLEDGE_WORDS
    if command == "/code":
        return word in _CODE_WORDS
    if command == "/tools":
        return not rest
    if command == "/agent":
        return rest in _AGENT_SHOW_WORDS
    return True


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
    elif command == "/context":
        words = rest.split(maxsplit=1)
        if words and words[0].lower() in _CONTEXT_EDIT_WORDS:
            text, outcome = await _kas_context_text(state, session_key, rest)
        else:
            text, outcome = await _inspect_context_text(state, slot)
    elif command == "/knowledge":
        text, outcome = await _kas_knowledge_text(state, session_key, rest)
    elif command == "/code":
        text, outcome = await _kas_code_text(state, session_key, rest)
    elif command == "/hooks":
        text, outcome = await _inspect_hooks_text(state, slot, session_key)
    elif command == "/usage":
        text, outcome = await _inspect_usage_text()
    elif command == "/help":
        text, outcome = _inspect_help_text()
    elif command == "/agent":
        text, outcome = await _inspect_agent_text(slot)
    else:
        view = kas_tool_view(await _inspect_live_provider(state, session_key))
        entry = view.get("entry") if isinstance(view, dict) else None
        declared = entry.get("servers") if isinstance(entry, dict) else None
        text, outcome = _inspect_mcp_text(slot, declared if isinstance(declared, list) else None)
    return redact(text), outcome
