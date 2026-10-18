"""Fakes and the message timestamp shared by the ``test_slack_transport_dispatch*.py`` modules.

Binds the golden-transcript harness from ``test_slack_golden_transcript``
(``FakeSessions``, ``RecordingSlackClient``, ``ScriptedProvider`` and
``make_event``) and holds the message timestamp ``_MSG_TS``, the
``_CapturingSessions`` stand-in that records the agent of each turn it
acquires, and the ``_CapturingCtxBuilder`` that records the keyword arguments
``build_message`` receives. ``test_slack_transport_dispatch.py``,
``test_slack_transport_dispatch_turn_outcomes.py`` and
``test_slack_transport_dispatch_flat_dm.py`` import from it.
"""

from __future__ import annotations

import importlib

_golden = importlib.import_module("test_slack_golden_transcript")

FakeSessions = _golden.FakeSessions
RecordingSlackClient = _golden.RecordingSlackClient
ScriptedProvider = _golden.ScriptedProvider
make_event = _golden.make_event

_MSG_TS = "1700000000.000100"


class _CapturingSessions(FakeSessions):
    """FakeSessions that records the ``agent`` passed to get_or_create.

    ``agents`` records TURN acquisitions only. The shared background session is
    tracked separately in ``background_keys``: a successful turn now also fires
    the fire-and-forget auto-title, which takes that session, and folding its
    acquire into ``agents`` would turn every "exactly one acquire, under this
    agent" assertion into a count of unrelated background work.
    """

    def __init__(self, provider):
        super().__init__(provider)
        self.agents: list = []
        self.background_keys: list = []

    async def get_or_create(self, session_key, agent=None, channel_id=None, start_priority=None):
        from kiro_crew.session import BACKGROUND_KEY

        if session_key == BACKGROUND_KEY:
            self.background_keys.append(session_key)
        else:
            self.agents.append(agent)
        return await super().get_or_create(session_key, agent=agent, channel_id=channel_id)


class _CapturingCtxBuilder:
    """Records build_message kwargs; hooks.on_message is passthrough by default."""

    def __init__(self, hook_result=None):
        self.captured: dict = {}

        class _Hooks:
            def on_message(self, text):
                from kiro_crew.hooks import HookResult

                return hook_result or HookResult.passthrough()

        self.hooks = _Hooks()

    def build_message(self, text, is_new, session_key, **kw):
        self.captured = kw
        return text, {}
