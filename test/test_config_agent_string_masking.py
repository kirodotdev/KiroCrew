"""Masking of agent-writable free-text values in the config API response.

Agent ``description`` and ``triggers`` are writable by the agent itself and by an
installed package, and neither is schema-sensitive, so the schema-driven walk in
``_masked_config_dict`` does not reach them. GET /api/config/kirocrew masks them
instead. Two properties matter and both are pinned below: the masking applies
ONLY to the browser-facing view and NEVER to ``to_dict()``/``save()``, and it
covers a value that is not a ``str`` — a config.json is hand- and agent-writable,
so an object or a list in one of these fields is the untrusted case, not an
impossible one, and skipping it would let nested credential-shaped bytes through.
"""

from __future__ import annotations

import json
import unittest.mock

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK, _masked_config_dict


def _cfg_with_agent(**agent_kwargs) -> tuple[KiroCrewConfig, str]:
    cfg = KiroCrewConfig()
    name = "researcher"
    cfg.agents[name] = KiroCrewAgentConfig(**agent_kwargs)
    return cfg, name


def test_description_and_triggers_masked_in_view():
    """Both non-empty free-text fields are masked and their raw values vanish."""
    cfg, name = _cfg_with_agent(
        description="SECRET-DESCRIPTION-should-not-leak",
        triggers="SECRET-TRIGGERS-should-not-leak",
    )

    masked = _masked_config_dict(cfg)
    agent = masked["agents"][name]
    assert agent["description"] == _SENSITIVE_MASK
    assert agent["triggers"] == _SENSITIVE_MASK

    dumped = json.dumps(masked)
    assert "SECRET-DESCRIPTION-should-not-leak" not in dumped
    assert "SECRET-TRIGGERS-should-not-leak" not in dumped


def test_mask_does_not_leak_into_write_path():
    """to_dict()/save() must still carry the real values after masking."""
    cfg, name = _cfg_with_agent(
        description="real description",
        triggers="real triggers",
    )

    # Build the masked view (which must not mutate the underlying cfg).
    _masked_config_dict(cfg)

    persisted = cfg.to_dict()["agents"][name]
    assert persisted["description"] == "real description"
    assert persisted["triggers"] == "real triggers"


def test_empty_strings_left_as_empty():
    """Empty description/triggers stay empty, not replaced by the sentinel."""
    cfg, name = _cfg_with_agent(description="", triggers="")

    masked = _masked_config_dict(cfg)
    agent = masked["agents"][name]
    assert agent["description"] == ""
    assert agent["triggers"] == ""


def test_other_agent_fields_unchanged():
    """Non-sensitive structural fields survive the masked view verbatim."""
    cfg, name = _cfg_with_agent(
        description="hide me",
        triggers="hide me too",
        model="claude-sonnet",
        workspace="research-ws",
        source="some-package",
    )

    agent = _masked_config_dict(cfg)["agents"][name]
    assert agent["model"] == "claude-sonnet"
    assert agent["workspace"] == "research-ws"
    assert agent["source"] == "some-package"


def test_non_string_values_are_masked_not_skipped():
    """A value that is not a ``str`` is masked, so nested bytes cannot escape.

    Built directly on the dataclass rather than through the loader on purpose:
    the view must hold on its own, without depending on an upstream coercion
    having run. A guard that only recognizes ``str`` would skip every shape here
    and pass the nested credential straight to the browser.
    """
    for _value in (
        {"k": "AKIAIOSFODNN7EXAMPLE"},
        ["AKIAIOSFODNN7EXAMPLE"],
        [{"nested": "AKIAIOSFODNN7EXAMPLE"}],
    ):
        cfg, name = _cfg_with_agent(description=_value, triggers=_value)

        masked = _masked_config_dict(cfg)
        agent = masked["agents"][name]
        assert agent["description"] == _SENSITIVE_MASK, _value
        assert agent["triggers"] == _SENSITIVE_MASK, _value
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(masked), _value


def test_non_string_scalars_are_masked():
    """A number/bool in a free-text field is masked too — only "" renders as-is."""
    cfg, name = _cfg_with_agent(description=7, triggers=False)

    agent = _masked_config_dict(cfg)["agents"][name]
    assert agent["description"] == _SENSITIVE_MASK
    assert agent["triggers"] == _SENSITIVE_MASK


def test_absent_field_is_not_conjured_into_a_placeholder():
    """A record missing the key entirely does not gain a "set (hidden)" value.

    ``to_dict()`` emits every dataclass field, so this guards the rule rather
    than a live shape: masking must key off a key that is present, never write
    a sentinel where the operator set nothing.
    """
    cfg, name = _cfg_with_agent(description="hide me")
    real_to_dict = cfg.to_dict

    def _to_dict_without_free_text() -> dict:
        data = real_to_dict()
        data["agents"][name].pop("description", None)
        data["agents"][name].pop("triggers", None)
        return data

    with unittest.mock.patch.object(cfg, "to_dict", _to_dict_without_free_text):
        agent = _masked_config_dict(cfg)["agents"][name]

    assert "description" not in agent
    assert "triggers" not in agent
