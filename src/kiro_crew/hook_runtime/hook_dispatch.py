"""Firing hooks from outside the store: the global store accessors, the strict
on-disk read a store-less process uses, the informational tool-call fire and the
PreToolUse block gate with the names a matcher meets.

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        HOOK_EVENT_PRE_TOOL_USE,
        HOOK_EVENT_SESSION_LANE_CHANGED,
        ScriptHook,
        ScriptHookStore,
        _context_matches,
        _global_script_hook_store,
        _lane_dispatch_tasks,
        _session_lane_matcher_context,
        logger,
        run_script_hook,
        sel,
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

    Blocks by the same rule the chat turn loop applies
    (:attr:`ScriptHookResult.pre_tool_blocks`): for a Hooks-page hook exit 2 is a
    delivered deny and any other nonzero exit is a gate with no verdict, which
    blocks; a spec hook blocks only on a delivered exit 2, as on kiro-cli. A fire
    that raises blocks. With no store registered in this process the saved
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
        if r.exit_code == 0:
            continue
        if not r.pre_tool_blocks:
            # A spec hook that was refused or failed: kiro-cli warns and runs the tool.
            logger.warning(
                "Spec hook %s did not run cleanly (%s); tool allowed",
                r.hook_name,
                r.failure_detail,
            )
            continue
        if r.exit_code == 2 and not r.refused:
            return f"{r.hook_name}: {r.stderr[:200] if r.stderr else 'hook denied'}"
        return f"{r.hook_name}: {r.failure_detail}"
    return None


def _status_tag_ids(tag_ids: Sequence[object], tags_by_id: Mapping[str, object]) -> set[str]:
    """The subset of ``tag_ids`` that name a status tag, which is what a lane is.

    ``slot.tags`` holds tag IDS; the objects carrying the ``status`` field live in
    the live vocabulary, so an id has to be resolved before it can be classified.
    Plain truthiness of the stored ``status``, which is how the board itself reads it
    (the tag manager's toggle is ``!!t.status``): a stricter test would reclassify a
    hand-edited ``"status": "false"`` from a lane to an ordinary tag while the board
    still draws it as a column.

    An id absent from the vocabulary resolves to ``None`` and is therefore NOT a
    lane. That is right for a stale id on a slot, and it is the reason the tag-DELETE
    path cannot emit through here: by the time a deletion has stripped the id from
    every holder, the object it would have been classified by is already gone. That
    path is outside this change (see the RFC's write-surface table) and stays silent
    rather than firing a half-classified delta.
    """
    ids: set[str] = set()
    for tag_id in tag_ids:
        if not isinstance(tag_id, str) or not tag_id:
            continue
        tag = tags_by_id.get(tag_id)
        if isinstance(tag, dict) and tag.get("status"):
            ids.add(tag_id)
    return ids


def _lane_dispatch_is_permitted(request: object) -> tuple[bool, str, str]:
    """Whether this request may cause a lane dispatch, and who it came from.

    Returns ``(permitted, source, caller)``: the two identity strings travel with the
    verdict so the SEL row names the caller the decision was made about, rather than
    asserting a dashboard user for every outcome.

    Confines firing to the dashboard USER. An app-scoped caller reaches the tag routes
    through the ``chat_tag_assign`` MCP tool on behalf of an agent, and a hook firing
    under that identity would run the operator's command for a caller the operator
    never saw move the session.

    It is also why the RFC's remaining emit sites -- channel first-filing and the
    choke point -- are a deferral rather than a hole: they have no dashboard caller to
    check here, and a path that cannot be authorized cannot dispatch.
    """
    if request is None:
        return (False, "unattributed", "unattributed")
    app = getattr(request, "app", None)
    if app is None or not hasattr(app, "get"):
        return (False, "unattributed", "unattributed")
    state = app.get("state")
    if state is None:
        return (False, "unattributed", "unattributed")
    # Imported here rather than at module scope to keep the dashboard modules off
    # this module's import graph, and deliberately NOT inside a try: a wrong path
    # must raise rather than read as "not permitted", which no test asserting a
    # non-fire can tell apart from a correct denial.
    from kiro_crew.dashboard import token_auth as _auth
    from kiro_crew.dashboard.chat_tags_policy import slot_tag_write_principal

    # The SHARED enumeration the two tag routes already decide "is this the
    # person?" with, rather than an app-claim read of our own. An absent app claim
    # is NOT the person: a plain agent session reaches these routes over the
    # internal-secret transport with no app claim at all, and so does an admitted
    # crew-member principal. Keying on the app claim alone let an agent moving a
    # session run the operator's shell hooks -- the case this gate exists to
    # refuse -- and stamped the SEL row as a dashboard action. Deny-by-default on
    # identity: permitted only when the caller is POSITIVELY the person.
    request_app = _auth.effective_request_app(state, request)  # type: ignore[arg-type]
    non_person, source, caller = slot_tag_write_principal(
        request,  # type: ignore[arg-type]
        request_app,
        what="hooks.session_lane_changed",
    )[:3]
    return (not non_person, source or "unattributed", caller or "unattributed")


async def dispatch_session_lane_changed_bulk(
    moves: Sequence[tuple[str, Sequence[object], Sequence[object]]],
    *,
    tags_by_id: Mapping[str, object],
    session_keys: Mapping[str, str] | None = None,
    request: object = None,
) -> None:
    """Fire ``SessionLaneChanged`` for each slot whose STATUS tags actually changed.

    ``moves`` is ``(slot_key, tag_ids_before, tag_ids_after)`` per session -- IDS, the
    shape ``slot.tags`` actually holds -- and ``tags_by_id`` is a view of the live tag
    vocabulary the ids resolve through. Both callers pass one move today; the SEL row
    is per DECISION, so one ``log_api_access`` is written on either outcome and a host
    with no enabled subscriber writes none.

    Informational: a hook cannot veto. By the time this runs the write is applied and
    the drag has happened, so a veto would make the board unusable when a hook breaks
    rather than preventing anything. Callers must therefore invoke it only AFTER a
    successful persist -- a refused or rolled-back write never fires it.
    """
    store = get_global_hook_store()
    if store is None:
        return
    # ``list_all()``, not ``_hooks``: that attribute is a dict keyed by id, so
    # iterating it yields ID STRINGS and every ``h.enabled`` here would raise.
    hooks = [
        h for h in store.list_all() if h.enabled and h.event == HOOK_EVENT_SESSION_LANE_CHANGED
    ]
    # Zero is the default shipping state, and it costs nothing: with no enabled
    # subscriber there is no decision to record, so no SEL row is written either.
    if not hooks:
        return

    permitted, source, caller = _lane_dispatch_is_permitted(request)
    sel().log_api_access(
        caller=caller,
        operation="hooks.session_lane_changed",
        outcome="allowed" if permitted else "denied",
        source=source,
        resources=",".join(slot for slot, _, _ in moves),
    )
    if not permitted:
        return

    for slot_key, before, after in moves:
        prior = _status_tag_ids(before, tags_by_id)
        now = _status_tag_ids(after, tags_by_id)
        if prior == now:
            continue
        added = sorted(now - prior)
        removed = sorted(prior - now)
        # The fire gate compares the status-only sets, so the symmetric difference
        # here always holds at least one id. That is load-bearing rather than
        # incidental: a matcher is consulted only when the context is non-empty, so
        # an empty delta would skip filtering and run EVERY registered hook.
        context = _session_lane_matcher_context(added, removed)
        payload = {
            "hook_event_name": HOOK_EVENT_SESSION_LANE_CHANGED,
            "slot": slot_key,
            # All three keys unconditionally, so a hook that always reads one never
            # KeyErrors on an addition-only or removal-only change.
            "added": added,
            "removed": removed,
        }
        # The firing session's own key, which ``run_script_hook`` resolves the
        # ``capabilities.script_hooks`` governance scope from. Omitting it resolved
        # the scope from an EMPTY key -- classified ``unknown`` -- so a profile that
        # disables script hooks for this session was bypassed rather than enforced.
        if session_keys and (session_key := session_keys.get(slot_key)):
            payload["session_key"] = session_key
        for hook in hooks:
            # Awaited inside the task, not here. ``_context_matches`` reaches
            # ``_bounded_pattern_search``, which runs a subprocess with a 2s
            # timeout for a regex matcher, so evaluating it on this path blocked
            # the gateway's event loop once per hook per lane change.
            # ``ScriptHookStore.fire`` moves the same call off the loop for the
            # same reason.
            task = asyncio.create_task(_run_one_lane_hook(hook, context, payload))
            _lane_dispatch_tasks.add(task)
            task.add_done_callback(_lane_dispatch_tasks.discard)


async def _run_one_lane_hook(hook: ScriptHook, context: str, payload: dict) -> None:
    """Match, run and PERSIST one lane hook, entirely off the request path.

    The matcher is evaluated here rather than at the dispatch site because a regex
    matcher spawns a subprocess with a 2s timeout, which on the request path blocks
    the gateway's single event loop.

    The persist is what makes a run survive a restart. ``run_script_hook`` mutates
    ``last_run``, ``last_status``, ``last_error`` and ``run_count`` on the hook
    object in memory; ``ScriptHookStore.fire`` follows its own runs with
    ``_persist_current``, and a dispatcher that calls ``run_script_hook`` directly
    has to do the same or the completed run is lost and the Hooks page restores the
    previous persisted values.
    """
    if hook.matcher:
        matched = await asyncio.to_thread(
            _context_matches, hook.matcher, hook.matcher_mode, context
        )
        if not matched:
            return
    try:
        await run_script_hook(hook, context=context, hook_event=dict(payload))
    finally:
        store = get_global_hook_store()
        if store is not None:
            # Off-loop, through the store's own seam, and failure-tolerant for the
            # reason that seam documents: losing this write loses bookkeeping, not
            # the hook set, so it must not turn a fired hook into an exception on a
            # task nobody awaits.
            try:
                await asyncio.to_thread(store._persist_current)
            except Exception:
                logger.debug("SessionLaneChanged: run bookkeeping not persisted", exc_info=True)
