"""A hand-edited message rule in config.json's ``hooks`` section must not break chat.

``HookManager.on_message`` runs on every chat and channel turn, so an entry it
cannot read is dropped or repaired at load time instead of raising each turn.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.hooks import (
    HOOK_INJECT_CONTEXT,
    HOOK_PASSTHROUGH,
    HOOK_REPLY,
    HookManager,
    HooksConfig,
)


def _on(section: dict, text: str):
    return HookManager(HooksConfig.from_dict(section)).on_message(text)


@pytest.mark.parametrize(
    "section",
    [
        {"auto_replies": [{"pattern": 1, "reply": "x"}]},
        {"auto_replies": [{"pattern": None, "reply": "x", "exact": True}]},
        {"transforms": [{"pattern": ["hi"], "prefix": "p"}]},
        {"context_rules": [{"triggers": None, "context": "C"}]},
        {"context_rules": [{"triggers": [3], "context": "C"}]},
        {"context_rules": [{"triggers": ["hi"], "context": None}]},
        {"context_rules": [{"triggers": "", "context": "C"}]},
    ],
)
def test_an_unreadable_rule_passes_the_message_through(section):
    assert _on(section, "hi there").action == HOOK_PASSTHROUGH


def test_a_readable_rule_beside_an_unreadable_one_still_fires():
    section = {"auto_replies": [{"pattern": 7, "reply": "x"}, {"pattern": "hi", "reply": "R"}]}

    result = _on(section, "hi there")

    assert (result.action, result.text) == (HOOK_REPLY, "R")


def test_a_lone_string_trigger_is_one_trigger_not_its_letters():
    section = {"context_rules": [{"triggers": "deploy", "context": "C"}]}

    assert _on(section, "hi there").action == HOOK_PASSTHROUGH
    assert _on(section, "please deploy now").action == HOOK_INJECT_CONTEXT


def test_a_skipped_rule_is_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="kiro_crew.hooks"):
        HooksConfig.from_dict({"auto_replies": [{"pattern": 1, "reply": "x"}]})

    assert "skipped a rule whose pattern is not text" in caplog.text


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "False"])
def test_exact_written_as_a_false_word_matches_as_a_substring(value):
    section = {"auto_replies": [{"pattern": "hi", "reply": "R", "exact": value}]}

    assert _on(section, "hi there").action == HOOK_REPLY


@pytest.mark.parametrize("value", [True, "true", "yes", 1])
def test_exact_written_as_a_true_value_still_needs_the_whole_message(value):
    section = {"auto_replies": [{"pattern": "hi", "reply": "R", "exact": value}]}

    assert _on(section, "hi there").action == HOOK_PASSTHROUGH
    assert _on(section, "HI").action == HOOK_REPLY


def test_a_valid_config_behaves_as_before():
    section = {
        "auto_replies": [{"pattern": "ping", "reply": "pong"}],
        "transforms": [{"pattern": "draft", "prefix": "P", "suffix": "S"}],
        "context_rules": [{"triggers": ["deploy", "ship"], "context": "C"}],
    }

    assert (_on(section, "ping me").action, _on(section, "ping me").text) == (HOOK_REPLY, "pong")
    assert _on(section, "a draft").text == "P\na draft\nS"
    assert _on(section, "we ship today").text == "C"
    assert _on(section, "nothing here").action == HOOK_PASSTHROUGH
