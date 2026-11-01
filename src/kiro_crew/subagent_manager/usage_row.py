"""The usage row a sub-agent's run persists for each prompt its child answers.

``run_turn._run_inner_impl`` owns the row's clock (``_row_t0``) and calls
:func:`persist_usage_row` from its nested ``_persist_usage_row``, so this body runs
exactly where it would inline. ``_run_inner_impl`` runs on ``kiro_crew.subagent``'s
globals (``bind_component_globals``), so ``time``, ``asyncio``, ``logger`` and
``provider_fallback_active`` are read from that module here too, at call time.
"""

from __future__ import annotations

from typing import Any


def persist_usage_row(
    coordinator: Any,
    completion: Any,
    session_key: str,
    client: Any,
    info: Any,
    agent: str,
    is_cc: bool,
    row_t0: list[float],
) -> None:
    """Persist one per-prompt usage row and restart *row_t0* (``_run_inner_impl``'s helper)."""
    # circular import: kiro_crew.subagent imports subagent_manager while it initialises;
    # by the time a run's prompt ends it is loaded.
    from kiro_crew import subagent as ns

    # Its inputs are read here, synchronously, and the append itself is
    # best-effort analytics on a task the manager holds (``_report_tasks``,
    # which ``cancel_all()`` drains within its bound): no ending waits on
    # it, so a cancel, the shutdown or a wedged FS can never hold one.
    try:
        # circular import: reached while kiro_crew.slack.handler is still
        # initialising (dashboard/file_api/channel_upload.py imports is_tracked_channel
        # from it), so a module-scope import raises ImportError under the
        # suite's import order.
        from kiro_crew.dashboard.handlers.usage import (
            persist_token_record_async,
            read_context_tokens,
            read_effective_agent,
        )

        _used, _window = read_context_tokens(client)
        _usage_row = ns.asyncio.ensure_future(
            persist_token_record_async(
                session_key,
                # Blank while a fallback serves this run: the explicit pin
                # would bill the fallback's spend to a model that never
                # executed; model_source reports what actually ran.
                ("" if ns.provider_fallback_active(client) else (info.model or "")),
                completion,
                provider="claude_code" if is_cc else "acp",
                surface="subagent",
                # Ownership stamp (see _build_token_record): an app-dispatched
                # subagent's spend must be readable by that app's audit — the
                # illustrator lane of an app is exactly this path.
                app=info.app or "",
                # Explicit/inherited `agent` FIRST here — unlike every other
                # surface. Under session sharing this subagent reuses the
                # PARENT's runtime, so read_effective_agent() would report the
                # parent's agent and misattribute a `spawn_run(agent="…")` turn.
                # `agent` is already the resolved value (it inherits the parent
                # session's agent when the spawn did not name one), and the
                # helper stays as the fallback for when it is empty.
                agent=agent or read_effective_agent(client) or "",
                context_used=_used,
                context_window=_window,
                elapsed_ms=int((ns.time.monotonic() - row_t0[0]) * 1000),
                model_source=client,
                trigger="subagent",
            )
        )
        coordinator._manager._report_tasks.add(_usage_row)
        _usage_row.add_done_callback(coordinator._manager._report_tasks.discard)
    except Exception:
        ns.logger.debug("usage row (subagent) persist failed", exc_info=True)
    row_t0[0] = ns.time.monotonic()
