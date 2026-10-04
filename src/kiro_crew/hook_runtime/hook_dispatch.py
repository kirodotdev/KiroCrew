"""Firing hooks from outside the store: the global store accessors, the strict
on-disk read a store-less process uses, the informational tool-call fire and the
PreToolUse block gate with the names a matcher meets.

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        HOOK_EVENT_PRE_TOOL_USE,
        ScriptHook,
        ScriptHookStore,
        _global_script_hook_store,
        logger,
        webhooks,
    )


def set_global_hook_store(store: ScriptHookStore) -> None:
    """Register the global script hook store."""
    global _global_script_hook_store
    _global_script_hook_store = store


def get_global_hook_store() -> ScriptHookStore | None:
    """Get the global script hook store, or None if not initialized."""
    return _global_script_hook_store


def persisted_hook_store() -> ScriptHookStore:
    """The registered hook store, or the Hooks page's saved hooks read from disk.

    A process that registers no store (the standalone ``kirocrew run`` task runner)
    still has the user's saved hooks in ``hooks.json``. A gate that read them as
    absent would let a covered call past a deny hook, so gates that must enforce
    them read this instead of :func:`get_global_hook_store`.

    Strict, unlike the store's own fail-soft load: a ``hooks.json`` that cannot be
    read or parsed, or that holds an entry the store could not load, raises, so a
    gate fails closed instead of reading a saved deny hook as absent.

    The file is read ONCE, under the same ``hooks.json.lock`` its writers hold, and
    that one snapshot is both validated and loaded, so an edit landing mid-read
    cannot pair one version's shape check with another version's hooks. Blocking
    I/O: an event-loop caller runs it in a worker thread.
    """
    store = get_global_hook_store()
    if store is not None:
        return store
    store = ScriptHookStore(load=False)
    if not store._path.exists():
        return store
    with webhooks.locked(store._path):
        data = json.loads(store._path.read_text(encoding="utf-8"))
    hooks_data = data.get("hooks", []) if isinstance(data, dict) else None
    if not isinstance(hooks_data, list):
        raise ValueError(f"{store._path} does not hold a hooks list")
    store._load_data(data)
    if store._unparsed_hook_entries or len(store._hooks) != len(hooks_data):
        raise ValueError(f"{store._path} holds hooks that could not be loaded")
    return store


async def fire_tool_hooks(
    hook_store: ScriptHookStore | None,
    event_title: str,
    event_tool_input: str | None = None,
    subagent_id: str | None = None,
    parent_session_key: str | None = None,
    agent_role: str | None = None,
) -> None:
    """Fire PreToolUse hooks for an EVENT_TOOL_CALL event.

    PostToolUse is NOT fired here because EVENT_TOOL_CALL is a notification
    that the tool is starting - the tool hasn't completed yet. PostToolUse
    should be fired on EVENT_TOOL_RESULT when available.

    Note: For EVENT_TOOL_CALL, hooks are informational only. The tool is
    already running (auto-approved by kiro-cli), so hook results cannot
    block execution. Hook scripts can log, audit, or trigger side effects.

    Optional ``subagent_id``, ``parent_session_key``, and ``agent_role`` are
    forwarded to the underlying hook_store so hook scripts can attribute
    tool calls to the specific agent/session that fired them. Callers in
    parent contexts (dashboard chat, generic LLM helpers) leave them as
    ``None``; subagent and taskrunner callers pass real values.
    """
    if hook_store is None:
        return
    tool_name = event_title or ""
    if tool_name.startswith("Running: "):
        tool_name = tool_name[9:]
    tool_input = None
    if event_tool_input:
        try:
            tool_input = json.loads(event_tool_input)
        except Exception:
            pass
    try:
        await hook_store.fire(
            HOOK_EVENT_PRE_TOOL_USE,
            tool_name=tool_name,
            tool_input=tool_input,
            subagent_id=subagent_id,
            parent_session_key=parent_session_key,
            agent_role=agent_role,
        )
    except Exception:
        logger.debug("PreToolUse hook error", exc_info=True)


def pre_tool_match_names(
    title: str,
    *,
    tool_identity: str = "",
    mcp_server: str = "",
    harness_tool_id: str = "",
) -> tuple[tuple[str, ...], tuple[str, ...] | None]:
    """The names a PreToolUse matcher meets for one call: ``(all, spec)``.

    *all* is every name the call is known by, for a Hooks-page hook: its *title*,
    its canonical *tool_identity* (written by the harness, never the model), the
    ``@server/tool`` and ``mcp__server__tool`` forms built from the trusted
    *mcp_server*, and the names the harness's own *harness_tool_id* stands for
    (:func:`kiro_crew.agent_sdk.spec_hooks.spec_hook_tool_names`). *spec* is the
    same without the title, for a spec hook, whose matcher names tools; ``None``
    when the harness stated no id, so a spec hook keeps matching the title.
    """
    # circular import: spec_hooks imports kiro_crew.hooks at load time.
    from kiro_crew.agent_sdk.spec_hooks import spec_hook_tool_names

    harness_names = spec_hook_tool_names(harness_tool_id) or ()
    trusted = [*harness_names, tool_identity]
    if mcp_server and tool_identity:
        trusted += [f"@{mcp_server}/{tool_identity}", f"mcp__{mcp_server}__{tool_identity}"]
    every = tuple(dict.fromkeys(n for n in [title, *trusted] if n))
    spec = tuple(dict.fromkeys(n for n in trusted if n)) if harness_names else None
    return every, spec


async def permission_pre_tool_block(
    hook_store: ScriptHookStore | None,
    spec_hooks: Sequence[ScriptHook],
    spec_hooks_cwd: str | None,
    event_title: str,
    event_tool_input: str | None = None,
    *,
    tool_identity: str = "",
    mcp_server: str = "",
    harness_tool_id: str = "",
    subagent_id: str | None = None,
    parent_session_key: str | None = None,
    agent_role: str | None = None,
) -> str | None:
    """Run the PreToolUse hooks on a subagent or task-runner permission request.

    For a turn whose backend never receives the agent spec's ``hooks`` (see
    :func:`kiro_crew.agent_sdk.spec_hooks.turn_spec_hooks`): on that backend the
    projection turns every call a PreToolUse hook covers into a permission
    request, so this is where the hooks gate. The Hooks page's hooks and the
    spec's run together, as on the chat turn loop. Such a turn skips the
    informational tool-call fire: KAS sends a call's tool-call frame BEFORE its
    permission request, and every call a PreToolUse hook covers reaches this gate,
    so firing there too would run each hook twice. Returns why the call is
    blocked, or ``None``.

    A hook matcher is compared with every name the call is known by: its title
    (what the chat turn loop matches), its canonical *tool_identity*
    (``LLMEvent.tool_name``, written by the harness, never the model) and, for an
    MCP call, the ``@server/tool`` and ``mcp__server__tool`` forms built from the
    trusted *mcp_server* (``LLMEvent.mcp_server_name``). When the harness stated
    its own id for the call (*harness_tool_id*, KAS's ``_meta.kiro.toolId``), the
    names that id stands for join them
    (:func:`kiro_crew.agent_sdk.spec_hooks.spec_hook_tool_names`), so a ``web_fetch``
    hook meets KAS's "Fetch URL". A spec hook then matches those names and the
    trusted identity only, never the title, as on the chat turn loop.

    Blocks by the same rule the chat turn loop applies: exit 2 is a delivered
    deny, and any other nonzero exit or a fire that raises is a gate with no
    verdict, which blocks. With no store registered in this process the saved
    Hooks-page hooks are read from disk (:func:`persisted_hook_store`), and saved
    hooks that cannot be read block.
    """
    if hook_store is None:
        # No store registered in this process: the saved hooks still apply.
        try:
            # Off the loop: it reads and parses the whole saved file.
            hook_store = await asyncio.to_thread(persisted_hook_store)
        except Exception as exc:  # noqa: BLE001 - a gate with no verdict blocks
            logger.warning("saved PreToolUse hooks could not be read; blocking tool", exc_info=True)
            return f"saved PreToolUse hooks could not be read: {exc}"[:500]
    tool_name = event_title or ""
    if tool_name.startswith("Running: "):
        tool_name = tool_name[9:]
    match_names, spec_names = pre_tool_match_names(
        tool_name,
        tool_identity=tool_identity,
        mcp_server=mcp_server,
        harness_tool_id=harness_tool_id,
    )
    tool_input = None
    if event_tool_input:
        try:
            tool_input = json.loads(event_tool_input)
        except Exception:
            pass
    try:
        results = await hook_store.fire(
            HOOK_EVENT_PRE_TOOL_USE,
            tool_name=tool_name,
            tool_input=tool_input,
            subagent_id=subagent_id,
            parent_session_key=parent_session_key,
            agent_role=agent_role,
            extra_hooks=spec_hooks,
            extra_hooks_cwd=spec_hooks_cwd,
            extra_hooks_tool_names=spec_names,
            tool_match_names=match_names,
        )
    except Exception as exc:  # noqa: BLE001 - a gate with no verdict blocks
        logger.warning("PreToolUse hook fire failed; blocking tool", exc_info=True)
        return f"PreToolUse hook could not run: {exc}"[:500]
    for r in results:
        if r.exit_code == 2:
            return f"{r.hook_name}: {r.stderr[:200] if r.stderr else 'hook denied'}"
        if r.exit_code != 0:
            detail = (
                r.error[:200] if r.error else (r.stderr[-200:] or f"exited with code {r.exit_code}")
            )
            return f"{r.hook_name}: {detail}"
    return None
