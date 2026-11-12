"""Checks the conductor agent test files share, written once.

Each check lives here as a mixin. A test class in ``test_*conductor_agent.py`` takes the
mixin as a base, so the node id stays ``<file>::<Class>::<test>``, the class keeps its
own setup, and pytest resolves the test's fixtures from that file.
"""

from __future__ import annotations


class PromptDoesNotCarryTheRetiredVerbosityToken:
    def test_prompt_does_not_carry_the_retired_verbosity_token(self, tmp_path, monkeypatch):
        data = self._install(tmp_path, monkeypatch)
        # Reply style now arrives as session-context chrome for every
        # agent; a token left here would reach the model as a literal.
        assert "{{VERBOSITY_BLOCK}}" not in data["prompt"]


class PromptDrivesPatrolWithMonitorStartNotWait:
    def test_prompt_drives_patrol_with_monitor_start_not_wait(self, tmp_path, monkeypatch):
        data = self._install(tmp_path, monkeypatch)
        prompt = " ".join(data["prompt"].split())
        assert "Patrol with `monitor_start`, never with `wait`" in prompt
        assert "autonudge_stop" in prompt
