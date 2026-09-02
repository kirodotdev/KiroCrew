"""Tests for ``agent_state.lift_and_strip_bookkeeping``.

kiro-cli's ``deny_unknown_fields`` rejects an entire agent spec on any
unrecognized key, so ``model_managed`` / ``cc_model`` must never reach a kiro
JSON file. This is the single helper shared by the PUT handler,
``migrate_agent_specs``, and ``_refresh_dynamic_fields`` (see #2570) — pin its
lift/strip/no-clobber/type-guard contract directly, independent of any caller.
"""

from __future__ import annotations

import logging

from kiro_crew import agent_state


def test_lifts_when_unset():
    config = {"name": "kirocrew", "model_managed": True, "cc_model": "claude-sonnet-4.6"}

    changed = agent_state.lift_and_strip_bookkeeping(config, "kirocrew")

    assert changed is True
    assert "model_managed" not in config
    assert "cc_model" not in config
    assert agent_state.get_model_managed("kirocrew") is True
    assert agent_state.get_cc_model("kirocrew") == "claude-sonnet-4.6"


def test_does_not_clobber_existing_sidecar_value():
    agent_state.set_model_managed("kirocrew", False)
    agent_state.set_cc_model("kirocrew", "test-model-stub")
    config = {"model_managed": True, "cc_model": "claude-sonnet-4.6"}

    changed = agent_state.lift_and_strip_bookkeeping(config, "kirocrew")

    assert changed is True
    assert "model_managed" not in config
    assert "cc_model" not in config
    assert agent_state.get_model_managed("kirocrew") is False
    assert agent_state.get_cc_model("kirocrew") == "test-model-stub"


def test_non_bool_model_managed_discarded_not_lifted(caplog):
    config = {"model_managed": "false"}

    with caplog.at_level(logging.WARNING):
        changed = agent_state.lift_and_strip_bookkeeping(config, "kirocrew")

    assert changed is True
    assert "model_managed" not in config
    # Not lifted: bool("false") is True, which would have silently flipped
    # the flag's meaning had the raw value been coerced instead of guarded.
    assert agent_state.get_model_managed("kirocrew") is None
    assert "non-bool model_managed" in caplog.text


def test_non_string_cc_model_discarded_not_lifted(caplog):
    config = {"cc_model": 123}

    with caplog.at_level(logging.WARNING):
        changed = agent_state.lift_and_strip_bookkeeping(config, "kirocrew")

    assert changed is True
    assert "cc_model" not in config
    assert agent_state.get_cc_model("kirocrew") is None
    assert "non-string cc_model" in caplog.text


def test_returns_false_when_neither_key_present():
    config = {"name": "kirocrew", "model": "auto"}

    changed = agent_state.lift_and_strip_bookkeeping(config, "kirocrew")

    assert changed is False
    assert config == {"name": "kirocrew", "model": "auto"}


def test_falsy_cc_model_strips_without_lifting(caplog):
    config = {"cc_model": ""}

    with caplog.at_level(logging.WARNING):
        changed = agent_state.lift_and_strip_bookkeeping(config, "kirocrew")

    assert changed is True
    assert "cc_model" not in config
    assert agent_state.get_cc_model("kirocrew") is None
    assert caplog.text == ""
