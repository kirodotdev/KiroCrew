"""A server's ``autoApprove`` reaches KAS as ``permissions``, under the kiro-cli checks.

kiro-cli honours ``mcpServers.<name>.autoApprove`` once
``governance.strip_ungoverned_auto_approve`` keeps it. KAS has no slot for the key,
so the projection turns each kept verb into an ``allowedTools`` entry and derives
the ``permissions`` rule from it, where it meets the per-entry ceiling and the
PreToolUse-hook withholding as well. These tests pin that a kept verb is granted
and that every check that withholds it on either route still withholds it.
"""

from __future__ import annotations

import types

import pytest
from kas_agents_helpers import _spec

from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_agents import to_client_custom_agent
from kiro_crew.acp.kas_auto_approve import auto_approve_refs
from kiro_crew.platform import governance

_DOCS = {"command": "docs-mcp", "autoApprove": ["read_page"]}


def _mcp_allows(out: dict) -> set[str]:
    rules = (out.get("permissions") or {}).get("rules") or []
    return {
        m
        for r in rules
        if r.get("capability") == "mcp" and r.get("effect") == "allow"
        for m in r.get("match") or []
    }


@pytest.fixture
def ungoverned(monkeypatch):
    """No ceiling, the owner opt-in on: the host the report came from."""
    monkeypatch.setattr(governance, "may_skip_gate_now", lambda ref: True)
    monkeypatch.setattr(governance, "_auto_approve_is_honoured", lambda: True)
    monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: True)


def _project(**servers) -> dict:
    spec = _spec(mcpServers={"kirocrew-core": {"command": "x"}, **servers})
    return to_client_custom_agent("a", spec, "p")


def test_an_owner_written_auto_approve_becomes_a_kas_permission(ungoverned):
    out = _project(**{"docs-mcp": dict(_DOCS)})
    assert "docs-mcp/read_page" in _mcp_allows(out)
    # The key itself still never reaches the wire.
    assert "autoApprove" not in out["mcpServers"]["docs-mcp"]


def test_a_ceiling_that_withholds_the_tool_withholds_the_permission(ungoverned, monkeypatch):
    """The per-entry re-ask every ``allowedTools`` grant meets at projection."""
    monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: ref != "@docs-mcp/read_page")
    assert _mcp_allows(_project(**{"docs-mcp": dict(_DOCS)})) == set()


def test_a_ceiling_that_governs_the_server_drops_its_auto_approve(ungoverned, monkeypatch):
    """The server-level check kiro-cli's route applies before a list survives."""
    monkeypatch.setattr(governance, "may_skip_gate_now", lambda ref: ref != "@docs-mcp")
    assert _mcp_allows(_project(**{"docs-mcp": dict(_DOCS)})) == set()


def test_the_owner_opt_in_off_keeps_the_tool_prompting(ungoverned, monkeypatch):
    monkeypatch.setattr(governance, "_auto_approve_is_honoured", lambda: False)
    assert _mcp_allows(_project(**{"docs-mcp": dict(_DOCS)})) == set()


def test_a_pre_tool_hook_on_the_server_outranks_the_permission(ungoverned):
    """KAS resolves the most restrictive rule, so the unscoped ``ask`` the hook
    withholding adds keeps the call on Crew's permission path, where the hook runs."""
    spec = _spec(mcpServers={"docs-mcp": dict(_DOCS)})
    out = to_client_custom_agent("a", spec, "p", pre_tool_hook_matchers=("@docs-mcp",))
    asks = [
        r
        for r in out["permissions"]["rules"]
        if r.get("capability") == "mcp" and r.get("effect") == "ask" and not r.get("match")
    ]
    assert asks


@pytest.mark.parametrize(
    "name, verbs",
    [
        pytest.param("my-app:srv", ["t"], id="app-namespaced-server"),
        pytest.param("docs-mcp", ["*"], id="glob-verb"),
        pytest.param("docs-mcp", ["a/b"], id="slashed-verb"),
        pytest.param("docs-mcp", "read_page", id="not-a-list"),
    ],
)
def test_entries_the_kiro_cli_route_would_not_keep_grant_nothing(ungoverned, name, verbs):
    out = _project(**{name: {"command": "x", "autoApprove": verbs}})
    assert _mcp_allows(out) == set()


def test_it_does_not_count_as_an_allowlist_for_an_authored_block(ungoverned):
    """``allowlist_present`` describes ``allowedTools``. A spec with no list and an
    authored allow keeps the outcome it had before any ``autoApprove`` was read."""
    authored = {"rules": [{"capability": "mcp", "match": ["other/*"], "effect": "allow"}]}
    without = to_client_custom_agent("a", _spec(permissions=authored), "p")
    with_aa = to_client_custom_agent(
        "a",
        _spec(
            permissions=authored,
            mcpServers={"kirocrew-core": {"command": "x"}, "docs-mcp": dict(_DOCS)},
        ),
        "p",
    )
    assert _mcp_allows(with_aa) - {"docs-mcp/read_page"} == _mcp_allows(without)


def test_a_kept_list_leaves_a_permission_decision_in_the_security_log(ungoverned, monkeypatch):
    """A hand-written spec reaches the projection without passing a spec writer, so
    the grant is recorded here as well."""
    events: list[dict] = []
    monkeypatch.setattr(
        governance,
        "sel",
        lambda: types.SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
    )
    _project(**{"docs-mcp": dict(_DOCS)})
    honoured = [e for e in events if e["operation"] == "mcp_auto_approve_honoured"]
    assert honoured and "@docs-mcp" in honoured[0]["resources"]


def test_refs_follow_the_spec_and_skip_malformed_entries(ungoverned):
    spec = {
        "mcpServers": {
            "s1": {"command": "x", "autoApprove": ["b", "a", "b", " "]},
            "s2": "nope",
            "s3": {"command": "x"},
            # Not a list[str]: kiro-cli's route keeps none of it, so neither does this.
            "s4": {"command": "x", "autoApprove": ["c", 3]},
        }
    }
    assert auto_approve_refs(spec) == ["@s1/b", "@s1/a"]
    assert auto_approve_refs({"mcpServers": []}) == []
