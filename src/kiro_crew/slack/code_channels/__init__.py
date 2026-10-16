"""Slack Code: code channels for Kiro Crew.

A code channel is a Slack channel that IS one agent session. Mention Kiro
Crew in a thread with work that takes several steps and it opens a code
channel with the thread as context, runs the work in a dashboard session
filed in the configured default folder (with the configured agent, model and
reasoning effort), shows "Working..." in the channel while a turn runs, and
answers there top-level -- no @mention needed. A one-message question is still
answered in place. It runs when the ``slack.code_channels`` setting is on AND the
install can run code channels (``slack.features.code_channels_enabled``);
otherwise the gateway behaves exactly as it does without this package.

Modules
-------

========================= ================================================
``models`` / ``client``   typed ``agents.conversations.*`` client
``classifier``            stay-or-open decision (tool-less model call)
``creation``              eligibility, session ids, Slack's origin echo
``recognition``           is a channel a code channel, and ours?
``store``                 durable routes + bindings
``binding``               the gate; the bound dashboard session; mirror bridges
``hooks`` / ``status``    turn hooks; the native status subscriber
``routing``               the seams ``slack/events.py`` calls
``workspace`` (package)   diff view / context bar (separate feature)
========================= ================================================

Interface for other features
----------------------------

**Binding record** -- :class:`~.store.Binding`, read with
``store.store().get_binding(channel_id)``:

``channel_id``, ``team_id``
    the code channel.
``session_key``
    the bound dashboard slot key (``chat-...``).
``anchor_ts``
    the kickoff message ts the slot's Slack link is recorded against.
``project_dir``
    the project the session was opened on ("" = dashboard default).
``origin_channel_id``, ``origin_ts`` (``origin_link``)
    the message the channel was opened from (empty for an adopted channel
    with no origin).
``created_by_us``
    False for a channel opened from Slack's UI that this gateway adopted.
``channel_name``
    the Slack channel name at bind time ("" when unknown).
``archived_at``, ``wrapped_up_at``
    wall-clock stamps the wrap-up feature sets (``0.0`` = not yet).

Change fields with ``store.store().update_binding(channel_id, **fields)``: one
atomic read-change-write under the store lock, ``channel_id`` fixed, an unknown
field refused, ``None`` for an unbound channel. A rebind (the dashboard session
was closed and reopened) keeps ``channel_name``, ``archived_at`` and
``wrapped_up_at``.

**Turn hooks** -- :mod:`.hooks`::

    hooks.on_turn_start(async (binding, TurnEvent) -> None)
    hooks.on_turn_end(async (binding, TurnEvent) -> None)

``TurnEvent`` carries ``channel_id``, ``session_key``, ``source``
(``"slack"`` / ``"dashboard"``) and, on end, ``outcome`` (``ok`` /
``error`` / ``stopped``). Hooks fire for every turn of a bound session while the
toggle is on, are awaited with a timeout, and can never fail the turn.
"""

from __future__ import annotations

from typing import Any

_INSTALLED = False


def install(slack_provider: Any) -> None:
    """Wire the built-in subscribers (idempotent); called at gateway start.

    *slack_provider* returns the live Slack client (``lambda: orch.slack``).
    Installing does nothing observable while the toggle is off: the hooks it
    registers only fire for a bound channel with the feature on.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    from kiro_crew.slack.agent_sessions.session_events import register_channel_stop_handler
    from kiro_crew.slack.code_channels import status
    from kiro_crew.slack.code_channels.routing import handle_session_stopped, install_temp_cleanup

    status.install(slack_provider)
    install_temp_cleanup()
    register_channel_stop_handler(handle_session_stopped)
    from kiro_crew.slack.code_channels.workspace import core as workspace

    workspace.install(slack_provider)
    _INSTALLED = True
