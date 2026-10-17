"""Regression tests: the env-key collision warning fires once per key.

``_collect_target_env`` runs on every rewrite pass (one call per agent spec,
plus one per kept overlay), so a single ambiguous config — two distinct server
names that normalize to one ``KIROCREW_MCP_TARGET_<SERVER>`` env key — would
print the same WARNING line on every pass. The emitter latches each distinct
normalized key at module level so the notice fires once per key per process.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.mcp_gateway import rewriter
from kiro_crew.mcp_gateway.rewriter import (
    _WRAPPER_MARKER,
    _collect_target_env,
    _reset_collision_warnings,
)

_COLLISION_TEXT = "KIROCREW_MCP_TARGET env-key collision on"


@pytest.fixture(autouse=True)
def reset_collision_latch():
    """Clear the per-process warning latch around each case.

    The latch is module-level and persists for the life of the process, so a
    test asserting on the first-occurrence WARNING must reset it or a prior
    test that already tripped the same key would suppress the record.
    """
    _reset_collision_warnings()
    yield
    _reset_collision_warnings()


def _wrapped_entry(target_command: str, target_arg: str) -> dict:
    """A rewritten (wrapped) MCP entry shaped as ``_build_stub_entry`` emits."""
    return {
        _WRAPPER_MARKER: True,
        "command": "/bin/stub",
        "args": [
            "--target-command",
            target_command,
            f"--target-args={target_arg}",
        ],
    }


def test_colliding_pair_warns_once_across_two_passes(caplog):
    """Two server names normalizing to one env key, rewritten twice, produce
    exactly one WARNING record for that key."""
    # "builder-mcp" and "builder_mcp" both normalize to
    # KIROCREW_MCP_TARGET_BUILDER_MCP; distinct target specs make it a real
    # collision rather than a first-wins no-op.
    servers = {
        "builder-mcp": _wrapped_entry("/opt/a/builder", "--mode=a"),
        "builder_mcp": _wrapped_entry("/opt/b/builder", "--mode=b"),
    }

    with caplog.at_level(logging.DEBUG, logger=rewriter.logger.name):
        _collect_target_env(dict(servers), {})  # pass 1 (one spec)
        _collect_target_env(dict(servers), {})  # pass 2 (a later rewrite)

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and _COLLISION_TEXT in r.getMessage()
    ]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "KIROCREW_MCP_TARGET_BUILDER_MCP" in warnings[0].getMessage()
    # The full first-occurrence text is preserved.
    assert "the args-hashed key is used at resolve time" in warnings[0].getMessage()


def test_two_distinct_colliding_keys_each_warn_once(caplog):
    """Two different colliding key groups each get their own single WARNING."""
    servers = {
        "builder-mcp": _wrapped_entry("/opt/a/builder", "--mode=a"),
        "builder_mcp": _wrapped_entry("/opt/b/builder", "--mode=b"),
        "search-mcp": _wrapped_entry("/opt/a/search", "--mode=a"),
        "search_mcp": _wrapped_entry("/opt/b/search", "--mode=b"),
    }

    with caplog.at_level(logging.DEBUG, logger=rewriter.logger.name):
        _collect_target_env(dict(servers), {})
        _collect_target_env(dict(servers), {})

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and _COLLISION_TEXT in r.getMessage()
    ]
    keyed = {
        "BUILDER_MCP": sum("KIROCREW_MCP_TARGET_BUILDER_MCP" in r.getMessage() for r in warnings),
        "SEARCH_MCP": sum("KIROCREW_MCP_TARGET_SEARCH_MCP" in r.getMessage() for r in warnings),
    }
    assert keyed == {"BUILDER_MCP": 1, "SEARCH_MCP": 1}, [r.getMessage() for r in warnings]


def test_reset_hook_lets_the_warning_fire_again(caplog):
    """The reset hook clears the latch so a fresh case sees the notice again."""
    servers = {
        "builder-mcp": _wrapped_entry("/opt/a/builder", "--mode=a"),
        "builder_mcp": _wrapped_entry("/opt/b/builder", "--mode=b"),
    }

    with caplog.at_level(logging.WARNING, logger=rewriter.logger.name):
        _collect_target_env(dict(servers), {})
        first = [r for r in caplog.records if _COLLISION_TEXT in r.getMessage()]
        assert len(first) == 1

        caplog.clear()
        _collect_target_env(dict(servers), {})
        assert not [r for r in caplog.records if _COLLISION_TEXT in r.getMessage()]

        caplog.clear()
        _reset_collision_warnings()
        _collect_target_env(dict(servers), {})
        after = [r for r in caplog.records if _COLLISION_TEXT in r.getMessage()]
        assert len(after) == 1
