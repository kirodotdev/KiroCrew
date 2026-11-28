"""Governance judges an MCP tool whose name contains ``__`` under the right server.

The ``mcp__<server>__<tool>`` title is read back by splitting at its LAST ``__``,
so a tool named ``github__create_issue`` on server ``srv`` reads as tool
``create_issue`` on server ``srv__github``. A caller that also holds the trusted
identity passes it as ``mcp_ref``; when the title is only that identity spelled
the ``mcp__`` way, the title adds nothing and its mis-split reading must not
refuse the call.
"""

from __future__ import annotations

import pytest

from kiro_crew.platform.governance import gate_decision, parse_policy

TOOL = "github__create_issue"
TITLE = f"mcp__srv__{TOOL}"
REF = f"@srv/{TOOL}"


def _ceiling(**mcp):
    return parse_policy({"version": 1, "boot": {}, "mcp": mcp})


def test_an_allow_list_naming_the_server_admits_its_dunder_tool() -> None:
    ceiling = _ceiling(mode="allow", allow=["@srv"])

    assert gate_decision(ceiling, None, TITLE, mcp_ref=REF).permitted is True


@pytest.mark.parametrize("rule", ["@srv", REF], ids=["server", "tool"])
def test_a_deny_on_the_server_or_the_tool_still_binds(rule) -> None:
    ceiling = _ceiling(mode="deny", deny=[rule])

    assert gate_decision(ceiling, None, TITLE, mcp_ref=REF).permitted is False


def test_an_allow_list_naming_another_server_still_refuses() -> None:
    ceiling = _ceiling(mode="allow", allow=["@other"])

    assert gate_decision(ceiling, None, TITLE, mcp_ref=REF).permitted is False


def test_a_title_naming_a_different_call_is_still_asked() -> None:
    """Only a title that restates ``mcp_ref`` is left out; any other title is a
    second name for the call and must be permitted too."""
    ceiling = _ceiling(mode="allow", allow=["@srv"])

    assert gate_decision(ceiling, None, "mcp__other__x", mcp_ref="@srv/x").permitted is False


def test_without_a_trusted_identity_the_title_is_still_judged() -> None:
    """A harness that sends no identity has only the title, so it is read as before."""
    ceiling = _ceiling(mode="allow", allow=["@srv"])

    assert gate_decision(ceiling, None, TITLE).permitted is False
    assert gate_decision(ceiling, None, "mcp__srv__list_repos").permitted is True


@pytest.fixture
def allow_srv_ceiling(tmp_path, monkeypatch):
    """The live platform context carries an ``mcp`` allow list naming only ``@srv``."""
    import dataclasses

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform import governance_profiles as gp
    from kiro_crew.platform.bootstrap import build_default_context

    monkeypatch.setattr(gp, "_PROFILES_DIR", tmp_path / "profiles")
    gp.reset_store()
    base = build_default_context(KiroCrewConfig.load())
    ctx_mod.set_context(
        dataclasses.replace(base, governance=_ceiling(mode="allow", allow=["@srv"]))
    )
    yield
    ctx_mod.reset_context()
    gp.reset_store()


def test_the_tool_gate_admits_a_dunder_tool_named_by_its_identity(allow_srv_ceiling) -> None:
    """The gate a chat call meets: an ``mcp__`` title beside the identity the
    harness stamped. Before the fix the title's split refused the call."""
    from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig

    result = HookManager(HooksConfig()).on_tool_call(
        TITLE,
        session_key="dashboard:chat-1",
        mcp_server_name="srv",
        mcp_tool_name=TOOL,
        mcp_identity_trusted=True,
    )

    assert result.action != TOOL_DENY, result.reason
