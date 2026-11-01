"""Glitter relay collaboration: lead model + fast teammate inside one turn.

The lead plans, makes every change and writes the final answer. A fast
same-provider teammate only continues rounds whose previous tool calls were
pure information gathering, and it never sees a tool that changes anything.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from io import StringIO
from types import SimpleNamespace

import kiro_byo.turn_runner as turn_mod
from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import ModelSpec, ProviderSpec
from kiro_byo.providers.base import ChatTurn, ToolCall

LEAD = ModelSpec("x", "X", "x-pro", "X Pro", tool_use=True)
FAST = ModelSpec("x", "X", "x-flash", "X Flash", tool_use=True)
PROVIDER = ProviderSpec("x", "X", "openai-compatible", "https://example.invalid/v1", "", (LEAD, FAST))
CAPS = {"read": "fs_read", "grep": "fs_read", "write": "fs_write", "shell": "shell"}


class FakeNative:
    def specs(self):
        return tuple(SimpleNamespace(name=name) for name in CAPS)

    def tool_specs(self, names=None):
        return [{"type": "function", "function": {"name": n}} for n in CAPS if names is None or n in names]

    def has_tool(self, name):
        return name in CAPS

    def capability_for(self, name):
        return CAPS.get(name, "unknown")


class RelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = self.tmp.name
        self.orig_turn = turn_mod.chat_completion_turn
        self.orig_worker_router = turn_mod.DefaultModelRouter.collaboration
        self.server = AcpServer(stdin=StringIO(), stdout=StringIO())
        self.calls: list[tuple[str, list[str]]] = []
        self.executed: list[str] = []
        self.server._execute_native_tool = lambda session, call, args: (
            self.executed.append(call.name) or (f"{call.name} ok", 1.0, "ok")
        )

    def tearDown(self) -> None:
        turn_mod.chat_completion_turn = self.orig_turn
        turn_mod.DefaultModelRouter.collaboration = self.orig_worker_router
        self.server._executor.shutdown(wait=True)
        if self.old_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = self.old_home
        self.tmp.cleanup()

    def _run(self, scripted: list[ChatTurn], relay=True, *, routing_config=None, routing_decision=None) -> Session:
        turns = iter(scripted)

        def fake_turn(provider, model_id, messages, emit, **kw):
            names = [((t or {}).get("function") or {}).get("name") for t in kw.get("tools") or []]
            self.calls.append((model_id, names))
            return next(turns)

        turn_mod.chat_completion_turn = fake_turn
        session = Session("s1", self.tmp.name, "kiro-byo-type:tiga:glitter", native=FakeNative())
        self.server._put_session(session)
        session.messages.append({"role": "user", "content": "investigate and fix"})
        self.server._run_provider_tool_loop(
            1, "s1", session, PROVIDER, LEAD, 0, 0.0, 0.0, [], None,
            relay=(PROVIDER, FAST, "low") if relay else None,
            routing_config=routing_config, routing_decision=routing_decision,
        )
        return session

    def test_fast_teammate_continues_gathering_and_lead_writes_and_answers(self) -> None:
        session = self._run([
            ChatTurn(text="", tool_calls=(ToolCall("c1", "read", '{"path":"a"}'),)),   # lead
            ChatTurn(text="", tool_calls=(ToolCall("c2", "grep", '{"q":"b"}'),)),      # teammate
            ChatTurn(text="a and b found in module m"),                                 # teammate hands back
            ChatTurn(text="", tool_calls=(ToolCall("c3", "write", '{"path":"m"}'),)),  # lead changes
            ChatTurn(text="fixed m"),                                                   # lead answers
        ])
        models = [model for model, _ in self.calls]
        self.assertEqual(models, ["x-pro", "x-flash", "x-flash", "x-pro", "x-pro"])
        # The teammate never sees a tool that changes anything.
        for model, names in self.calls:
            if model == "x-flash":
                self.assertEqual(sorted(names), ["grep", "read"])
        self.assertEqual(self.executed, ["read", "grep", "write"])
        self.assertEqual(session.messages[-1]["role"], "assistant")
        self.assertEqual(session.messages[-1]["content"], "fixed m")
        self.assertTrue(any("[Relay worker findings]" in str(m.get("content")) for m in session.messages))

    def test_any_change_keeps_the_lead_in_charge(self) -> None:
        self._run([
            ChatTurn(text="", tool_calls=(ToolCall("c1", "read", "{}"), ToolCall("c2", "shell", "{}"))),
            ChatTurn(text="done"),
        ])
        self.assertEqual([m for m, _ in self.calls], ["x-pro", "x-pro"])


    def test_productive_teammate_is_not_forced_back_after_six_rounds(self) -> None:
        scripted = [
            ChatTurn(text="", tool_calls=(ToolCall("lead-read", "read", '{"path":"seed"}'),)),
        ]
        for index in range(8):
            scripted.append(
                ChatTurn(text="", tool_calls=(ToolCall(f"w{index}", "grep", f'{{"q":"q{index}"}}'),))
            )
        scripted.extend([
            ChatTurn(text="worker gathered enough"),
            ChatTurn(text="lead final"),
        ])
        session = self._run(scripted)
        models = [model for model, _ in self.calls]
        self.assertEqual(models[0], "x-pro")
        self.assertEqual(models[1:10], ["x-flash"] * 9)
        self.assertEqual(models[-1], "x-pro")
        self.assertEqual(session.messages[-1]["content"], "lead final")

    def test_worker_is_rerouted_again_for_each_gathering_invocation(self) -> None:
        alt_fast = ModelSpec("y", "Y", "y-fast", "Y Fast", tier="fast", tool_use=True)
        alt_provider = ProviderSpec(
            "y", "Y", "openai-compatible", "https://other.invalid/v1", "", (alt_fast,)
        )
        picks = iter([
            SimpleNamespace(provider=PROVIDER, model=FAST),
            SimpleNamespace(provider=alt_provider, model=alt_fast),
        ])

        def reroute(_router, *args, **kwargs):
            return next(picks)

        turn_mod.DefaultModelRouter.collaboration = reroute
        routing_decision = SimpleNamespace(
            execution_intent=SimpleNamespace(collaboration_tier_pool="fast"),
            signals=SimpleNamespace(),
        )
        session = self._run([
            ChatTurn(text="", tool_calls=(ToolCall("c1", "read", '{"path":"a"}'),)),
            ChatTurn(text="", tool_calls=(ToolCall("c2", "grep", '{"q":"b"}'),)),
            ChatTurn(text="worker gathered enough"),
            ChatTurn(text="lead final"),
        ], routing_config=SimpleNamespace(), routing_decision=routing_decision)
        self.assertEqual([model for model, _ in self.calls], ["x-pro", "x-flash", "y-fast", "x-pro"])
        self.assertEqual(session.messages[-1]["content"], "lead final")

    def test_teammate_failure_never_fails_the_turn(self) -> None:
        from kiro_byo.providers import ProviderError

        scripted = iter([
            ChatTurn(text="", tool_calls=(ToolCall("c1", "read", "{}"),)),
            ProviderError("teammate down"),
            ChatTurn(text="lead finished alone"),
        ])

        def fake_turn(provider, model_id, messages, emit, **kw):
            self.calls.append((model_id, []))
            item = next(scripted)
            if isinstance(item, Exception):
                raise item
            return item

        turn_mod.chat_completion_turn = fake_turn
        session = Session("s2", self.tmp.name, "kiro-byo-type:tiga:glitter", native=FakeNative())
        self.server._put_session(session)
        session.messages.append({"role": "user", "content": "go"})
        self.server._run_provider_tool_loop(
            2, "s2", session, PROVIDER, LEAD, 0, 0.0, 0.0, [], None, relay=(PROVIDER, FAST, "low"),
        )
        self.assertEqual([m for m, _ in self.calls], ["x-pro", "x-flash", "x-pro"])
        self.assertEqual(session.messages[-1]["content"], "lead finished alone")

    def test_without_relay_the_lead_runs_every_round(self) -> None:
        self._run([
            ChatTurn(text="", tool_calls=(ToolCall("c1", "read", "{}"),)),
            ChatTurn(text="done"),
        ], relay=False)
        self.assertEqual([m for m, _ in self.calls], ["x-pro", "x-pro"])

    def test_teammate_selection(self) -> None:
        from kiro_byo.mode_router import route_model
        from kiro_byo.config import KiroByoConfig

        decision = route_model(
            KiroByoConfig((PROVIDER,), mode_type="glitter"), pack="tiga", policy="glitter",
            prompt="long task", history=[], has_image=False, tools_expected=True,
        )
        self.assertEqual(decision.model.model_id, "x-pro")
        teammate = self.server._relay_teammate(decision, decision.provider, decision.model)
        self.assertIsNotNone(teammate)
        self.assertEqual(teammate[1].model_id, "x-flash")

        solo = ProviderSpec("s", "S", "openai-compatible", "https://example.invalid/v1", "", (LEAD,))
        decision = route_model(
            KiroByoConfig((solo,), mode_type="glitter"), pack="tiga", policy="glitter",
            prompt="long task", history=[], has_image=False, tools_expected=True,
        )
        self.assertIsNone(self.server._relay_teammate(decision, decision.provider, decision.model))


if __name__ == "__main__":
    unittest.main()
