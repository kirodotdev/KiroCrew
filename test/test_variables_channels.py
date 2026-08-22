"""Tests for crew-variable expansion on inbound channel text.

A channel message (Slack, Discord, Telegram, Webex, WeCom, Teams, Weixin) reaches
the agent through one shared dispatch, so the expansion lives there once. Driving
the whole dispatch would require a session store, a renderer and a turn driver, so
these cover the two things the change actually introduces: the shared resolver, and
the structural guarantee that the dispatch expands the user's text before handing it
to the context builder.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

from kiro_crew.config import loader as loader_mod
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    WorkspaceConfig,
    variable_values_for,
)
from kiro_crew.messaging import dispatch as dispatch_mod


def _config() -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.variables = {"baseUrl": "https://global.test"}
    cfg.workspaces = {"ops": WorkspaceConfig(dir="w-ops", variables={"queue": "oncall"})}
    cfg.default_workspace = "ops"
    cfg.agents = {
        "crew1": KiroCrewAgentConfig(workspace="ops", variables={"baseUrl": "https://crew.test"})
    }
    cfg.default_agent = "crew1"
    return cfg


class TestVariableValuesFor:
    def test_returns_the_effective_map(self):
        with patch.object(loader_mod.KiroCrewConfig, "load", classmethod(lambda cls: _config())):
            values = variable_values_for("crew1")
        assert values == {"baseUrl": "https://crew.test", "queue": "oncall"}

    def test_unknown_agent_falls_back_to_the_default_crew(self):
        with patch.object(loader_mod.KiroCrewConfig, "load", classmethod(lambda cls: _config())):
            assert variable_values_for("no-such-crew")["baseUrl"] == "https://crew.test"

    def test_a_broken_config_yields_an_empty_map_rather_than_raising(self):
        """Text is left unexpanded rather than failing the turn: a variable is a
        convenience, and the message is still what its author meant to send."""
        with patch.object(
            loader_mod.KiroCrewConfig,
            "load",
            classmethod(lambda cls: (_ for _ in ()).throw(OSError("boom"))),
        ):
            assert variable_values_for("crew1") == {}

    def test_returns_a_copy_so_a_caller_cannot_mutate_config_state(self):
        cfg = _config()
        with patch.object(loader_mod.KiroCrewConfig, "load", classmethod(lambda cls: cfg)):
            values = variable_values_for("crew1")
        values["baseUrl"] = "mutated"
        assert cfg.agents["crew1"].variables["baseUrl"] == "https://crew.test"


class TestDispatchLeavesInboundTextAlone:
    """The inbound channel path hands ``build_message`` the text VERBATIM.

    This class previously asserted the opposite. Inbound expansion was removed
    because a variable's value is operator configuration while inbound text is
    authored by a channel participant, so expanding it published operator config to
    anyone allowed to message the bot.
    """

    def test_the_raw_turn_text_is_what_reaches_build_message(self):
        source = inspect.getsource(dispatch_mod)
        build_at = source.index("ctx_builder.build_message,")
        call = source[build_at : build_at + 200]
        assert "turn.user_text," in call, "the inbound text must be passed through unmodified"

    def test_the_module_no_longer_carries_an_expander(self):
        """Not just unused — absent. An import left in place is the first half of a
        re-introduction, and this path must not have the capability at hand."""
        assert not hasattr(dispatch_mod, "expand_variables")
        assert not hasattr(dispatch_mod, "variable_values_for")

    def test_a_channel_message_leaves_a_token_literal(self):
        """The security property, scoped to the function that actually drives a turn.

        An earlier version split the module source on ``def dispatch_channel_turn``
        — a function that does not exist here (it is ``drive_turn``) — so ``split``
        returned the whole module and the ``[:4000]`` window inspected only the
        import header. Adding an expander inside the turn body would not have failed
        it. Bound to the real function's own source now.
        """
        body = inspect.getsource(dispatch_mod.drive_turn)
        assert "expand_variables(" not in body, "the turn body expands participant text"
        assert (
            "variable_values_for(" not in body
        ), "the turn body resolves a variable map for participant text"

        # And the value that WOULD be disclosed is real, so the guard is not
        # protecting an empty set.
        cfg = _config()
        cfg.variables = {"SECRET": "operator-only-value"}
        with patch.object(loader_mod.KiroCrewConfig, "load", classmethod(lambda cls: cfg)):
            assert variable_values_for("crew1")["SECRET"] == "operator-only-value"


class TestSharedHelperIsWiredWhereItMatters:
    def test_a_value_containing_a_token_is_not_rescanned(self):
        from kiro_crew.variables import expand

        out, unresolved = expand("{{a}}", {"a": "{{b}}", "b": "boom"})
        assert out == "{{b}}"
        assert unresolved == frozenset()

    def test_resolution_is_scoped_to_the_requested_agent(self):
        cfg = _config()
        cfg.agents["crew2"] = KiroCrewAgentConfig(
            workspace="ops", variables={"baseUrl": "https://two.test"}
        )
        with patch.object(loader_mod.KiroCrewConfig, "load", classmethod(lambda cls: cfg)):
            assert variable_values_for("crew2")["baseUrl"] == "https://two.test"
            assert variable_values_for("crew1")["baseUrl"] == "https://crew.test"


def test_turn_agent_still_selects_the_crew_for_the_system_prompt():
    """``turn.agent`` no longer drives inbound expansion, but it still reaches
    build_message as the crew identity — the agent SYSTEM PROMPT is
    operator-authored and does expand, so the wrong crew there is still a defect.

    Checked against the REAL ChannelTurn: an earlier version asserted
    ``hasattr(MagicMock(), "agent")``, which a MagicMock satisfies for every
    conceivable name, so it could not fail and said nothing about the type.
    """
    from kiro_crew.messaging.dispatch import ChannelTurn

    assert "agent" in getattr(ChannelTurn, "__dataclass_fields__", {}) or hasattr(
        ChannelTurn, "agent"
    ), "ChannelTurn no longer carries an `agent` field"
    source = inspect.getsource(dispatch_mod)
    assert "agent=turn.agent" in source, (
        "turn.agent must still reach build_message: the agent system prompt expands "
        "and needs the right crew even though the inbound text does not"
    )


class TestNoInboundTransportExpands:
    """Inbound channel text is NEVER expanded, and this ratchet is the guard.

    A variable's value is OPERATOR configuration. Inbound channel text is authored
    by a channel participant — ``allowed_users`` admits several people, and the
    dispatch layer carries no operator-vs-participant distinction — so expanding it
    would let anyone permitted to message the bot read operator config by sending
    ``{{NAME}}`` and reading the reply. That holds whether or not the values are
    secrets, because the operator never opted into publishing them.

    This deliberately REVERSES an earlier round that added expansion to these five
    modules. Widening the coverage widened the disclosure; the correct scope is
    operator-authored text only (the dashboard composer, the agent system prompt,
    a cron message, a monitor instruction).

    WHAT THIS RATCHET DOES NOT PROVE. It is an INTEGRITY boundary — participant text
    never reaches the expander — not a confidentiality one. The agent system prompt
    is operator-authored and still expands on a channel turn, so a participant can
    ask the agent to repeat its own prompt and read the values that way. This guard
    removes one direct read path (sending ``{{NAME}}``); it does not make a value
    unreadable to someone in ``allowed_users``. Do not cite it as though it did, and
    do not let it justify putting a secret in a variable — v1 has no secret store.
    """

    TRANSPORTS = (
        "kiro_crew/messaging/dispatch.py",
        "kiro_crew/slack/handler.py",
        "kiro_crew/slack/transport_dispatch.py",
        "kiro_crew/discord/transport_dispatch.py",
        "kiro_crew/telegram/transport_dispatch.py",
    )

    def _source(self, rel: str) -> str:
        root = Path(__file__).resolve().parent.parent / "src"
        return (root / rel).read_text(encoding="utf-8")

    def test_no_transport_resolves_or_expands_a_variable_map(self):
        for rel in self.TRANSPORTS:
            src = self._source(rel)
            assert "variable_values_for(" not in src, (
                f"{rel} resolves a variable map for inbound channel text; a channel "
                "participant could then read operator config by sending {{NAME}}"
            )
            assert "expand_variables(" not in src, f"{rel} expands inbound channel text"

    def test_the_transports_still_pass_an_explicit_crew(self):
        """The agent SYSTEM PROMPT still expands — it is operator-authored — so the
        crew identity still has to be right on a channel turn."""
        for rel, expected in (
            ("kiro_crew/slack/handler.py", "crew=_agent"),
            ("kiro_crew/slack/transport_dispatch.py", "crew=_agent"),
            ("kiro_crew/discord/transport_dispatch.py", "crew=agent"),
            ("kiro_crew/telegram/transport_dispatch.py", "crew=agent"),
        ):
            assert expected in self._source(rel), f"{rel} does not pass {expected}"
