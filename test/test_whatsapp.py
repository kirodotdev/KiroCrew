"""WhatsApp channel core-logic tests: echo discipline, group gate, commands.

The echo tracker and group gate are the two safety-critical pure modules of
the QR-linked personal-account channel: the first keeps the agent from
answering its own sends (which arrive ``from_me=True`` exactly like operator
input), the second keeps it silent in groups unless configuration says
otherwise. Both take an injectable clock so expiry paths are deterministic.
"""

from __future__ import annotations

from kiro_crew.whatsapp.commands import parse_command
from kiro_crew.whatsapp.echo import EchoTracker
from kiro_crew.whatsapp.group_gate import (
    SILENCE_SENTINEL,
    GroupGate,
    build_silence_contract,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestEchoTracker:
    def test_untracked_from_me_is_not_an_echo(self):
        t = EchoTracker()
        assert not t.is_own_echo("447700900000@s.whatsapp.net", "3EB0AAAA")

    def test_remembered_send_is_an_echo(self):
        t = EchoTracker()
        t.remember("447700900000@s.whatsapp.net", "3EB0AAAA")
        assert t.is_own_echo("447700900000@s.whatsapp.net", "3EB0AAAA")

    def test_redelivery_still_matches_a_consumed_entry(self):
        """WhatsApp redelivers after reconnect — the first read must not pop
        the entry, or the redelivery becomes a phantom operator command."""
        t = EchoTracker()
        t.remember("jid@s.whatsapp.net", "ID1")
        assert t.is_own_echo("jid@s.whatsapp.net", "ID1")
        assert t.is_own_echo("jid@s.whatsapp.net", "ID1")

    def test_same_id_in_another_chat_is_not_an_echo(self):
        t = EchoTracker()
        t.remember("a@s.whatsapp.net", "ID1")
        assert not t.is_own_echo("b@s.whatsapp.net", "ID1")

    def test_entries_expire_after_ttl(self):
        clock = FakeClock()
        t = EchoTracker(ttl_s=60, clock=clock)
        t.remember("jid@s.whatsapp.net", "ID1")
        clock.now += 61
        assert not t.is_own_echo("jid@s.whatsapp.net", "ID1")
        assert len(t) == 0

    def test_lru_cap_evicts_oldest(self):
        clock = FakeClock()
        t = EchoTracker(ttl_s=3600, max_entries=2, clock=clock)
        for i, msg in enumerate(("A", "B", "C")):
            clock.now += 1
            t.remember("jid@s.whatsapp.net", msg)
        assert not t.is_own_echo("jid@s.whatsapp.net", "A")
        assert t.is_own_echo("jid@s.whatsapp.net", "B")
        assert t.is_own_echo("jid@s.whatsapp.net", "C")

    def test_empty_message_id_is_ignored(self):
        t = EchoTracker()
        t.remember("jid@s.whatsapp.net", "")
        assert len(t) == 0


class TestGroupGate:
    JID = "1203630000000@g.us"

    def gate(self, mode="mention", rules="", cooldown_s=120, clock=None):
        entry = {
            "jid": self.JID,
            "name": "Test Group",
            "mode": mode,
            "rules": rules,
            "cooldown_s": cooldown_s,
        }
        return GroupGate([entry], clock=clock)

    def test_unconfigured_group_is_dropped(self):
        gate = GroupGate([])
        verdict = gate.evaluate(self.JID, sender_is_operator=False, addressed=True)
        assert not verdict.respond
        assert verdict.reason == "group_not_configured"
        assert not gate.configured(self.JID)

    def test_mode_off_is_dropped_even_when_addressed(self):
        verdict = self.gate(mode="off").evaluate(self.JID, sender_is_operator=True, addressed=True)
        assert not verdict.respond
        assert verdict.reason == "group_mode_off"

    def test_mention_mode_replies_only_when_addressed(self):
        gate = self.gate(mode="mention")
        hit = gate.evaluate(self.JID, sender_is_operator=False, addressed=True)
        miss = gate.evaluate(self.JID, sender_is_operator=False, addressed=False)
        assert hit.respond and not hit.unprompted
        assert not miss.respond and miss.reason == "not_addressed"

    def test_only_the_operator_may_steer(self):
        gate = self.gate(mode="mention")
        member = gate.evaluate(self.JID, sender_is_operator=False, addressed=True)
        operator = gate.evaluate(self.JID, sender_is_operator=True, addressed=True)
        assert not member.may_steer
        assert operator.may_steer

    def test_rules_mode_unprompted_carries_rules_and_flag(self):
        gate = self.gate(mode="rules", rules="Answer 3D-printer questions.")
        verdict = gate.evaluate(self.JID, sender_is_operator=False, addressed=False)
        assert verdict.respond and verdict.unprompted
        assert "3D-printer" in verdict.rules

    def test_rules_mode_without_rules_text_stays_silent(self):
        verdict = self.gate(mode="rules", rules="  ").evaluate(
            self.JID, sender_is_operator=False, addressed=False
        )
        assert not verdict.respond
        assert verdict.reason == "rules_mode_without_rules"

    def test_cooldown_blocks_unprompted_until_elapsed(self):
        clock = FakeClock()
        gate = self.gate(mode="rules", rules="Help.", cooldown_s=100, clock=clock)
        first = gate.evaluate(self.JID, sender_is_operator=False, addressed=False)
        assert first.respond
        gate.record_unprompted_reply(self.JID)
        clock.now += 50
        blocked = gate.evaluate(self.JID, sender_is_operator=False, addressed=False)
        assert not blocked.respond and blocked.reason == "cooldown_active"
        clock.now += 51
        again = gate.evaluate(self.JID, sender_is_operator=False, addressed=False)
        assert again.respond

    def test_cooldown_does_not_gate_addressed_messages(self):
        clock = FakeClock()
        gate = self.gate(mode="rules", rules="Help.", cooldown_s=100, clock=clock)
        gate.record_unprompted_reply(self.JID)
        addressed = gate.evaluate(self.JID, sender_is_operator=False, addressed=True)
        assert addressed.respond

    def test_silence_contract_names_the_sentinel_and_rules(self):
        contract = build_silence_contract("Only weekend plans.")
        assert SILENCE_SENTINEL in contract
        assert "Only weekend plans." in contract


class TestCommands:
    def test_new_and_compact_and_passthrough(self):
        assert parse_command("/new") == "new"
        assert parse_command("  /COMPACT ") == "compact"
        assert parse_command("hello") is None
        assert parse_command("") is None
