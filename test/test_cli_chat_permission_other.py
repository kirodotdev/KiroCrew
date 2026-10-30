"""``kirocrew chat``'s ``o`` (other) permission answer: refuse the call and send the
person's typed reply to the agent instead of a bare approve or deny.

Runs on ``_TestChatPermissionRequestHarness`` from ``cli_helpers.py``: the real
``_send_and_print`` and ``_answer_permission``, a real ``HookManager``, and stdin
patched only at the blocking ``_read_line_blocking`` seam, so the key matching,
the second read, the audit ordering and the steer all run as production code.
"""

import asyncio

import pytest
from cli_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _doctor_reads_the_warm_probe_cache,
    _TestChatPermissionRequestHarness,
)

_REPLY = "Use the staging bucket instead, then show me the diff."


class TestChatPermissionOtherReply(_TestChatPermissionRequestHarness):
    class _SteeringProvider(_TestChatPermissionRequestHarness._GatedProvider):
        """A gated provider whose backend takes a mid-turn steer."""

        supports_refusal_steer = True

        def __init__(self, event, trace, *, steer_result=True):
            super().__init__(event, trace)
            self._steer_result = steer_result

        async def steer(self, message):
            self.trace.append(("steer", message))
            if isinstance(self._steer_result, BaseException):
                raise self._steer_result
            return self._steer_result

    class _NoSteerProvider(_TestChatPermissionRequestHarness._GatedProvider):
        """No steer channel. Records every message the CLI streams to it."""

        def __init__(self, event, trace):
            super().__init__(event, trace)
            self.messages = []

        async def stream(self, message):
            from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

            self.messages.append(message)
            if len(self.messages) > 1:
                # The follow-up turn carrying the reply asks nothing.
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok, staging")
                yield LLMEvent(kind=EVENT_COMPLETE)
                return
            async for ev in super().stream(message):
                yield ev

    async def _run(self, monkeypatch, provider_cls, answer, **provider_kw):
        import kiro_crew.cli_chat as cli_chat

        trace: list = []
        provider = provider_cls(self._event(), trace, **provider_kw)
        reads = self._patch_env(monkeypatch, trace=trace, answer=answer)
        await asyncio.wait_for(
            cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate()),
            timeout=self._TIMEOUT,
        )
        return provider, trace, reads

    @staticmethod
    def _kinds(trace):
        return [t[0] for t in trace if t[0] != "sel" or t[1].get("outcome") == "denied"]

    @pytest.mark.asyncio
    async def test_the_prompt_advertises_the_other_key(self, monkeypatch):
        _, _, reads = await self._run(monkeypatch, self._SteeringProvider, "d")
        assert "[o] other" in reads["prompts"][0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["o", "O", " o \n"])
    async def test_a_reply_is_steered_into_the_turn_then_the_call_is_refused(
        self, monkeypatch, key
    ):
        provider, trace, reads = await self._run(
            monkeypatch, self._SteeringProvider, [key, _REPLY + "\n"]
        )
        # Audit first, then the person's words, then the wire -- never an approve.
        assert self._kinds(trace) == ["sel", "steer", "reject"], trace
        sel_row = next(t[1] for t in trace if t[0] == "sel" and t[1].get("outcome") == "denied")
        assert sel_row["error"] == "user_replied"
        # The reply is the person's message, not a decision: it is never audited.
        assert _REPLY not in repr(sel_row)
        notice = next(t[1] for t in trace if t[0] == "steer")
        assert _REPLY in notice
        assert "did not run" in notice
        # This IS a user action: the host notice's "NOT a user action" would lie.
        assert "[Kiro Crew host notice]" not in notice
        assert "NOT a user action" not in notice
        assert reads["n"] == 2
        assert "reply" in reads["prompts"][1].lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reply", ["", "   \n", EOFError])
    async def test_a_blank_reply_is_a_plain_denial(self, monkeypatch, reply):
        provider, trace, _ = await self._run(monkeypatch, self._SteeringProvider, ["o", reply])
        assert self._kinds(trace) == ["sel", "reject"], trace
        assert [t[1]["error"] for t in trace if t[0] == "sel"] == ["user_denied"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["other", "oo", "od"])
    async def test_only_the_exact_other_token_asks_for_a_reply(self, monkeypatch, key):
        _, trace, reads = await self._run(monkeypatch, self._SteeringProvider, [key, _REPLY])
        assert reads["n"] == 1
        assert self._kinds(trace) == ["sel", "reject"], trace

    @pytest.mark.asyncio
    async def test_the_model_authored_title_is_redacted_in_the_reply_notice(self, monkeypatch):
        import kiro_crew.cli_chat as cli_chat

        secret = "AKIAIOSFODNN7EXAMPLE"
        notice = cli_chat._user_reply_notice(f"upload with {secret}", _REPLY)
        assert secret not in notice
        assert notice.startswith(cli_chat._USER_REPLY_TAG)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("steer_result", [False, RuntimeError("pipe closed")])
    async def test_an_undelivered_steer_sends_the_reply_as_the_next_message(
        self, monkeypatch, capsys, steer_result
    ):
        class _FailingSteer(self._NoSteerProvider):
            supports_refusal_steer = True

            async def steer(self, message):
                self.trace.append(("steer", message))
                if isinstance(steer_result, BaseException):
                    raise steer_result
                return steer_result

        provider, trace, _ = await self._run(monkeypatch, _FailingSteer, ["o", _REPLY])
        assert [c[0] for c in provider.calls] == ["reject"]
        assert provider.messages == ["run it", _REPLY]
        captured = capsys.readouterr()
        assert "next message" in captured.err
        assert "ok, staging" in captured.out

    @pytest.mark.asyncio
    async def test_a_backend_without_a_steer_channel_still_gets_the_reply(
        self, monkeypatch, capsys
    ):
        provider, trace, _ = await self._run(monkeypatch, self._NoSteerProvider, ["o", _REPLY])
        assert not [t for t in trace if t[0] == "steer"]
        assert [c[0] for c in provider.calls] == ["reject"]
        assert provider.messages == ["run it", _REPLY]
