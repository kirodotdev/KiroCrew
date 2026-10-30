"""Slack agent sessions: thread status and title, and the native Stop button.

Gated on ``slack.agent_sessions`` (``kiro_crew.slack.features``); with it off,
nothing here is attached and the Slack integration behaves exactly as before.

* :mod:`.status` -- ``agents.sessions.setStatus`` / ``rename``, attached to the
  Slack client at boot (``status.configure``).
* :mod:`.session_events` -- ``agent_session_stopped`` and
  ``agent_session_title_changed``.
* :mod:`.fence` -- keeps a natively stopped turn from posting anything more.

This package re-exports nothing: :mod:`kiro_crew.slack.client` imports
:mod:`.fence`, so importing the package must stay as light as that leaf.
"""
