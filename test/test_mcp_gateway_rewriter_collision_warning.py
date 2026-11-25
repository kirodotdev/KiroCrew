"""Regression tests: the env-key collision warning fires once per key, and only
for two different server names.

``_collect_target_env`` runs on every rewrite pass (one call per agent spec,
plus one per kept overlay), so a single ambiguous config — two distinct server
names that normalize to one ``KIROCREW_MCP_TARGET_<SERVER>`` env key — would
print the same WARNING line on every pass. The emitter latches each distinct
normalized key at module level so the notice fires once per key per process.

One server name that two agents declare with different args is no collision:
each variant gets its own args-hashed key, so the pass stays quiet about it.
"""

from __future__ import annotations

import json
import logging
import sys

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


def test_one_name_with_different_args_across_agents_is_not_a_collision(caplog):
    """Two agents declare ``demo`` with different args: each gets its own hashed
    key, so the shared pass map keeps the bare-key fallback quiet."""
    target_env: dict[str, str] = {}
    owners: dict[str, str] = {}

    with caplog.at_level(logging.DEBUG, logger=rewriter.logger.name):
        _collect_target_env({"demo": _wrapped_entry("/opt/demo", "--mode=a")}, target_env, owners)
        _collect_target_env({"demo": _wrapped_entry("/opt/demo", "--mode=b")}, target_env, owners)

    assert not [r for r in caplog.records if _COLLISION_TEXT in r.getMessage()]
    hashed = sorted(v for k, v in target_env.items() if k.startswith("KIROCREW_MCP_TARGET_DEMO__"))
    assert hashed == ["/opt/demo --mode=a", "/opt/demo --mode=b"]
    # The bare fallback stays first-wins.
    assert target_env["KIROCREW_MCP_TARGET_DEMO"] == "/opt/demo --mode=a"


def test_distinct_names_across_agents_still_warn_once_naming_both(caplog):
    """``builder-mcp`` in one agent and ``builder_mcp`` in another share a key:
    the shared pass map still sees two names, and the WARNING names both."""
    target_env: dict[str, str] = {}
    owners: dict[str, str] = {}

    with caplog.at_level(logging.DEBUG, logger=rewriter.logger.name):
        for _pass in range(2):
            _collect_target_env(
                {"builder-mcp": _wrapped_entry("/opt/a/builder", "--mode=a")}, target_env, owners
            )
            _collect_target_env(
                {"builder_mcp": _wrapped_entry("/opt/b/builder", "--mode=b")}, target_env, owners
            )

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and _COLLISION_TEXT in r.getMessage()
    ]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    message = warnings[0].getMessage()
    assert "KIROCREW_MCP_TARGET_BUILDER_MCP" in message
    assert "'builder-mcp' and 'builder_mcp'" in message


def test_full_and_cached_pass_stay_quiet_for_one_name_with_different_args(tmp_path, caplog):
    """The rewrite pass itself, fresh and then from its cache, shares one map
    across the agents, so ``demo`` declared by two agents with different args
    logs no collision on either path."""
    agents = tmp_path / "agents"
    agents.mkdir()
    for name, mode in (("alpha", "--mode=a"), ("beta", "--mode=b")):
        (agents / f"{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "tools": ["@demo"],
                    "mcpServers": {"demo": {"command": sys.executable, "args": ["demo.py", mode]}},
                }
            ),
            encoding="utf-8",
        )
    kwargs = dict(
        source_dir=agents,
        overlay_dir=tmp_path / "overlay" / "agents",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "work",
        stub_servers=frozenset({"demo"}),
    )

    for cached in (False, True):
        _reset_collision_warnings()
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger=rewriter.logger.name):
            results, target_env = rewriter.rewrite_agents(**kwargs)
        assert cached == any("serving cached overlays" in r.getMessage() for r in caplog.records)
        assert results == {"alpha.json": 1, "beta.json": 1}
        assert not [r for r in caplog.records if _COLLISION_TEXT in r.getMessage()]
        hashed = [k for k in target_env if k.startswith("KIROCREW_MCP_TARGET_DEMO__")]
        assert len(hashed) == 2
