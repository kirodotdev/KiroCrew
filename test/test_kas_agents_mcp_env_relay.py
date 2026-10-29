"""KAS projection: a third-party MCP server's boolean ``env`` flags reach it, nothing else does.

kiro-cli reads an agent spec off disk, so a server declared there starts with its
whole ``env``. KAS takes the agent over the ACP wire, so every ``env`` value the
projection keeps is in the ``session/new`` frame -- and in any frame recording
and log of it. These tests pin the rule in
:func:`kiro_crew.acp.kas_agents._relayable_literal`: a value crosses only when it
is exactly ``true`` or ``false`` (case-insensitive, trimmed), its key is not
credential-shaped, and the credential redactor leaves it alone. No secret is a
boolean, so the rule needs no guess about which names or shapes carry one; every
other value is withheld and named in the log. They are judged on the serialized
``session/new`` params rather than on the projected dict, so a later carrier of
the entry (the session-level hoist) is covered too.
"""

from __future__ import annotations

import json
import logging

import pytest
from kas_agents_helpers import _spec

from kiro_crew.acp._dispatch import build_session_new_params
from kiro_crew.acp.kas_agents import hoist_servers_for_session, to_client_custom_agent

#: Boolean flags, the one value shape that crosses.
_FLAGS = {
    "FEATURE_X_ENABLED": "true",
    "ENABLE_EXPERIMENTAL_TOOLS": "false",
    "DEBUG": "TRUE",
    "TELEMETRY": "False",
    "VERBOSE": " true ",
}


def _wire(env: dict, *, load: bool = False) -> str:
    """The ``session/new`` params for one agent declaring ``vendor`` with *env*, as sent."""
    agent = to_client_custom_agent(
        "a",
        _spec(mcpServers={"vendor": {"command": "vendor-mcp", "env": env}}),
        "p",
    )
    agents, servers = hoist_servers_for_session([agent], "a", [], include_user_servers=load)
    return json.dumps(build_session_new_params("/w", mcp_servers=servers, kas_custom_agents=agents))


def _relayed_env(env: dict) -> dict:
    out = to_client_custom_agent(
        "a", _spec(mcpServers={"vendor": {"command": "vendor-mcp", "env": env}}), "p"
    )
    return out["mcpServers"]["vendor"].get("env", {})


class TestBooleansReachTheServer:
    def test_boolean_flags_reach_a_third_party_server(self):
        """The reported case: a server reading feature flags from ``env`` starts
        with them on KAS, as it does on kiro-cli. Relayed as written."""
        assert _relayed_env(_FLAGS) == _FLAGS

    @pytest.mark.parametrize("key", sorted(_FLAGS))
    def test_each_boolean_relays_on_its_own(self, key):
        assert _relayed_env({key: _FLAGS[key]}) == {key: _FLAGS[key]}

    @pytest.mark.parametrize("load", [False, True])
    def test_the_flags_are_in_the_frame_on_new_and_on_load(self, load):
        wire = _wire(_FLAGS, load=load)
        for value in _FLAGS.values():
            assert json.dumps(value) in wire

    def test_a_secret_beside_the_flags_costs_only_itself(self):
        assert _relayed_env({**_FLAGS, "API_KEY": "abc123"}) == _FLAGS

    def test_a_managed_server_still_relays_no_literal(self):
        """Crew's own servers are unchanged: their entry is Crew-authored, so any
        extra literal is a hand edit and stays withheld."""
        out = to_client_custom_agent(
            "a",
            _spec(mcpServers={"kirocrew-core": {"command": "x", "env": {"DEBUG": "true"}}}),
            "p",
        )
        assert "env" not in out["mcpServers"]["kirocrew-core"]


#: (key, value) pairs that are not booleans. Every VALUE must stay off the wire,
#: whatever its key: settings and secrets alike, numbers included (a PIN is digits).
_NOT_BOOLEAN = [
    # Settings that are not booleans: withheld too, and named in the log.
    ("LOG_LEVEL", "debug"),
    ("MAX_RESULTS", "50"),
    ("TEMPERATURE", "0.7"),
    ("API_HOST", "api.example.com"),
    ("YES_FLAG", "yes"),
    ("ON_FLAG", "on"),
    ("ONE_FLAG", "1"),
    ("BASE_URL", "https://api.example.com/v1"),
    ("WORKSPACE_ROOT", "/home/user/projects/demo"),
    # Short secrets under names that earlier rounds of review found no key list
    # could cover: run-together, acronym-prefixed, or simply innocent.
    ("DBPASS", "hunter3"),
    ("SMTPPASS", "hunter3"),
    ("MYSQLPWD", "hunter3"),
    ("ADMINPW", "hunter3"),
    ("DB_PASSPHRASE", "hunter3"),
    ("AWS_CREDS", "hunter3"),
    ("SID_COOKIE", "hunter3"),
    ("BASIC_AUTH", "hunter3"),
    ("MYSQL_PWD", "hunter3"),
    ("dbPass", "hunter3"),
    ("SERVICE_PW", "hunter3"),
    ("MODE", "hunter2"),
    ("DOOR_CODE", "4321"),
    # Credential shapes.
    ("GH", "ghp_" + "a1" * 18),
    ("OPENAI", "sk-proj-" + "a" * 40),
    ("ACCOUNT", "AKIA" + "ABCDEFGHIJKLMNOP"),
    ("VENDOR", "q8Z3kLm0Vx7Rt2Yp9Wn4Bc6Hd1Fg5Js"),
    ("CONFIG", '{"password": "hunter2"}'),
    ("CONNECTION", "Server=db;Database=app;Pwd=hunter6"),
    ("TARGET", "postgresql+asyncpg://app:hunter4@db.example.com/x"),
    ("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123/aB-cD_eF-gH_iJ-kL"),
    ("VENDOR_HOME", "${KIRO_API_KEY}"),
    ("VENDOR_CONF", "secret://vendor-api"),
    ("PHRASE", "correct horse battery staple"),
    # A boolean inside other text is not a boolean.
    ("FLAG_WITH_TAIL", "true;rm"),
    ("QUOTED_FLAG", '"true"'),
]


class TestEverythingElseIsWithheld:
    @pytest.mark.parametrize("load", [False, True])
    @pytest.mark.parametrize(
        ("key", "value"),
        _NOT_BOOLEAN,
        ids=[key for key, _ in _NOT_BOOLEAN],
    )
    def test_a_non_boolean_value_is_not_in_the_frame(self, key, value, load):
        wire = _wire({**_FLAGS, key: value}, load=load)
        # The entry is gone whole, and a value long enough to be unique is
        # nowhere in the frame (a short one such as "on" occurs in other words).
        assert json.dumps(key) not in wire
        if len(value) >= 6:
            assert json.dumps(value)[1:-1] not in wire
        # The flags beside it still went: the withhold is per key.
        assert json.dumps(_FLAGS["FEATURE_X_ENABLED"]) in wire

    def test_every_listed_value_is_withheld_together(self):
        env = {f"{key}_{i}": value for i, (key, value) in enumerate(_NOT_BOOLEAN)}
        assert _relayed_env(env) == {}


class TestABooleanCanStillBeWithheld:
    @pytest.mark.parametrize(
        "key", ["API_KEY", "GITHUB_TOKEN", "CLIENT_SECRET", "DB_PASSWORD", "encryptionKey"]
    )
    def test_a_boolean_under_a_credential_key_is_withheld(self, key):
        """The shared key check stays in front: a flag-shaped value under a
        credential name is not sent."""
        assert _relayed_env({key: "true"}) == {}

    def test_a_key_paired_credential_is_withheld(self):
        """The redactor sees the key: ``ghp_...=true`` is credential-shaped only as
        a pair, so the value under that key is withheld."""
        assert _relayed_env({"ghp_" + "b2" * 18: "true"}) == {}


class TestTheRuleIsLocalToTheKasProjection:
    def test_agent_env_keeps_accepting_settings_kas_withholds(self):
        """``PW_TEST_HTML_REPORT_OPEN`` is withheld on KAS (not a boolean), while
        ``agent.env``, which keeps the shared class unchanged, still saves and
        loads it."""
        from kiro_crew.config.sections import agent_env_refusal, coerce_agent_env
        from kiro_crew.config.sections_agent_env import agent_env_write_refusals

        name = "PW_TEST_HTML_REPORT_OPEN"
        assert _relayed_env({name: "never"}) == {}
        assert agent_env_refusal(name) is None
        assert coerce_agent_env({name: "never"}) == {name: "never"}
        assert agent_env_write_refusals({name: "never"}) == ()


class TestTheWithholdIsLogged:
    def test_withheld_keys_are_named_and_values_are_not(self, caplog):
        with caplog.at_level(logging.INFO, logger="kiro_crew.acp.kas_agents"):
            _relayed_env({**_FLAGS, "DB_PASSWORD": "hunter2", "LOG_LEVEL": "debug"})
        assert "'vendor'" in caplog.text
        assert "DB_PASSWORD" in caplog.text and "LOG_LEVEL" in caplog.text
        assert "hunter2" not in caplog.text
        assert "FEATURE_X_ENABLED" not in caplog.text

    def test_a_credential_shaped_key_name_is_counted_not_printed(self, caplog):
        key = "ghp_" + "b2" * 18
        with caplog.at_level(logging.INFO, logger="kiro_crew.acp.kas_agents"):
            assert _relayed_env({key: "x"}) == {}
        assert "1 unnamed" in caplog.text
        assert key not in caplog.text

    def test_nothing_withheld_logs_nothing(self, caplog):
        with caplog.at_level(logging.INFO, logger="kiro_crew.acp.kas_agents"):
            _relayed_env(_FLAGS)
        assert "not relaying" not in caplog.text
