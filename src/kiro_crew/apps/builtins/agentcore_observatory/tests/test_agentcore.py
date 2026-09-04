"""Query layer: argv shape, error-vs-empty, denial, truncation, child wiring."""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew.apps.builtins.agentcore_observatory.backend import agentcore, catalog
from kiro_crew.apps.builtins.agentcore_observatory.backend.config import ObservatoryConfig
from kiro_crew.cloud.aws import CloudActionDenied

CFG = ObservatoryConfig(profile="prof", region="us-east-2")
SVC = "bedrock-agentcore-control"


def _stub(
    monkeypatch: pytest.MonkeyPatch, responses: list[tuple[int, str, str]]
) -> list[list[str]]:
    """Replace the CLI chokepoint, recording each argv and replaying responses."""
    seen: list[list[str]] = []

    def fake_run_aws(
        args: list[str], profile: str = "", region: str = "", **kwargs: Any
    ) -> tuple[int, str, str]:
        seen.append(list(args))
        assert profile == CFG.profile
        assert region == CFG.region
        return responses[len(seen) - 1]

    monkeypatch.setattr(agentcore, "run_aws", fake_run_aws)
    return seen


def _ok(key: str, rows: list[dict[str, Any]], token: str = "") -> tuple[int, str, str]:
    body: dict[str, Any] = {key: rows}
    if token:
        body["nextToken"] = token
    return (0, json.dumps(body), "")


# --------------------------------------------------------------------------
# Every catalog row is reachable through the one code path.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rt", [t for t in catalog.RESOURCE_TYPES if t.listable], ids=lambda t: t.id
)
def test_every_listable_type_builds_its_declared_argv(
    monkeypatch: pytest.MonkeyPatch, rt: catalog.ResourceType
) -> None:
    """The generic path must serve all 27 types, not just the hand-checked ones."""
    seen = _stub(monkeypatch, [_ok(rt.list_key, [{"probe": rt.id}])])
    parents = {param: "parent-id" for param in rt.parent_params}
    result = agentcore.list_resource(CFG, rt.id, parents)
    assert result.ok is True, result.error
    assert result.items == [{"probe": rt.id}]
    expected = [SVC, rt.list_verb]
    for param in rt.parent_params:
        expected += [param, "parent-id"]
    assert seen == [expected]


def test_child_without_parent_id_is_a_caller_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing parent must be named, never sent as a call with a hole in it."""
    seen = _stub(monkeypatch, [])
    result = agentcore.list_resource(CFG, "gateway-targets", {})
    assert result.ok is False
    assert "--gateway-identifier" in result.error
    assert seen == []


def test_two_parent_type_needs_both(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub(monkeypatch, [])
    result = agentcore.list_resource(
        CFG, "policy-generation-assets", {"--policy-generation-id": "g1"}
    )
    assert result.ok is False
    assert "--policy-engine-id" in result.error
    assert seen == []


@pytest.mark.parametrize("bad", ["-rm", "a b", "x;y", "$(id)", "a" * 300, ""])
def test_hostile_parent_id_never_reaches_argv(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    seen = _stub(monkeypatch, [])
    result = agentcore.list_resource(CFG, "gateway-targets", {"--gateway-identifier": bad})
    assert result.ok is False
    assert seen == []


def test_unknown_type_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub(monkeypatch, [])
    result = agentcore.list_resource(CFG, "../etc/passwd")
    assert result.ok is False
    assert "unknown resource type" in result.error
    assert seen == []


def test_singleton_has_no_list_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub(monkeypatch, [])
    result = agentcore.list_resource(CFG, "token-vault")
    assert result.ok is False
    assert "no list operation" in result.error
    assert seen == []


def test_unconfigured_region_never_spawns_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub(monkeypatch, [])
    result = agentcore.list_resource(ObservatoryConfig(profile="p", region=""), "agent-runtimes")
    assert result.ok is False
    assert "region" in result.error
    assert seen == []


# --------------------------------------------------------------------------
# Error-vs-empty: the distinction the whole surface is built around.
# --------------------------------------------------------------------------


def test_empty_result_is_success_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, [_ok("agentRuntimes", [])])
    result = agentcore.list_resource(CFG, "agent-runtimes")
    assert (result.ok, result.items, result.error) == (True, [], "")


def test_nonzero_exit_is_error_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, [(255, "", "Error loading SSO Token: expired")])
    result = agentcore.list_resource(CFG, "agent-runtimes")
    assert result.ok is False
    assert "expired" in result.error
    assert result.items == []


def test_non_json_stdout_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, [(0, "<html>proxy error</html>", "")])
    assert "not JSON" in agentcore.list_resource(CFG, "agent-runtimes").error


def test_json_non_object_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, [(0, "[1, 2]", "")])
    assert "not an object" in agentcore.list_resource(CFG, "agent-runtimes").error


def test_empty_stdout_is_an_empty_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, [(0, "   ", "")])
    result = agentcore.list_resource(CFG, "agent-runtimes")
    assert (result.ok, result.items, result.error) == (True, [], "")


def test_wrong_response_key_yields_no_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """The silent failure the catalog tests exist to prevent, shown here."""
    _stub(monkeypatch, [(0, json.dumps({"somethingElse": [{"id": "x"}]}), "")])
    result = agentcore.list_resource(CFG, "agent-runtimes")
    assert (result.ok, result.items) == (True, [])


def test_agent_session_denial_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*_args: Any, **_kwargs: Any) -> tuple[int, str, str]:
        raise CloudActionDenied("refused from an agent session")

    monkeypatch.setattr(agentcore, "run_aws", deny)
    result = agentcore.list_resource(CFG, "agent-runtimes")
    assert (result.ok, result.denied) == (False, True)
    assert "agent session" in result.error


def test_broken_cli_does_not_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> tuple[int, str, str]:
        raise OSError("aws not found")

    monkeypatch.setattr(agentcore, "run_aws", boom)
    result = agentcore.list_resource(CFG, "agent-runtimes")
    assert (result.ok, result.denied) == (False, False)
    assert "aws CLI" in result.error


def test_non_dict_rows_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, [(0, json.dumps({"memories": [{"id": "a"}, "junk", 7, None]}), "")])
    assert agentcore.list_resource(CFG, "memories").items == [{"id": "a"}]


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


def test_pagination_follows_next_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub(
        monkeypatch,
        [_ok("evaluators", [{"id": "a"}], token="t1"), _ok("evaluators", [{"id": "b"}])],
    )
    result = agentcore.list_resource(CFG, "evaluators")
    assert [row["id"] for row in result.items] == ["a", "b"]
    assert result.truncated is False
    assert seen[1][-2:] == ["--next-token", "t1"]


def test_runaway_pagination_reports_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial set must never be presentable as a total."""
    page = _ok("evaluators", [{"id": "x"}], token="forever")
    _stub(monkeypatch, [page] * (agentcore._MAX_PAGES + 1))
    result = agentcore.list_resource(CFG, "evaluators")
    assert (result.ok, result.truncated) == (True, True)
    assert len(result.items) == agentcore._MAX_PAGES


# --------------------------------------------------------------------------
# get_resource
# --------------------------------------------------------------------------


def test_get_builds_sorted_id_args(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub(monkeypatch, [(0, json.dumps({"gatewayId": "g1"}), "")])
    result = agentcore.get_resource(
        CFG, "gateway-target", {"--gateway-identifier": "g1", "--target-id": "t1"}
    )
    # Unknown type id guards first — `gateway-target` is not a catalog row.
    assert result.ok is False
    assert seen == []


def test_get_singleton_takes_no_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub(monkeypatch, [(0, json.dumps({"tokenVaultId": "default"}), "")])
    result = agentcore.get_resource(CFG, "token-vault")
    assert result.ok is True
    assert result.item == {"tokenVaultId": "default"}
    assert seen == [[SVC, "get-token-vault"]]


def test_get_passes_identifier_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub(monkeypatch, [(0, json.dumps({"memoryId": "m1"}), "")])
    result = agentcore.get_resource(CFG, "memories", {"--memory-id": "m1"})
    assert result.ok is True
    assert seen == [[SVC, "get-memory", "--memory-id", "m1"]]


def test_get_rejects_a_hostile_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub(monkeypatch, [])
    result = agentcore.get_resource(CFG, "memories", {"--memory-id": "-rf /"})
    assert result.ok is False
    assert seen == []


def test_get_on_a_type_without_a_get_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub(monkeypatch, [])
    result = agentcore.get_resource(CFG, "agent-runtime-versions")
    assert result.ok is False
    assert "no get operation" in result.error
    assert seen == []


def test_get_needs_a_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same rule as the list path: no region means no call, not a guessed one."""
    seen = _stub(monkeypatch, [])
    result = agentcore.get_resource(ObservatoryConfig(profile="p", region=""), "token-vault")
    assert result.ok is False
    assert "region" in result.error
    assert seen == []


def test_get_rejects_a_key_that_is_not_a_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """An identifier mapping must be keyed by a CLI flag, not a bare word."""
    seen = _stub(monkeypatch, [])
    result = agentcore.get_resource(CFG, "memories", {"memory-id": "m1"})
    assert result.ok is False
    assert seen == []


def test_get_denial_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*_args: Any, **_kwargs: Any) -> tuple[int, str, str]:
        raise CloudActionDenied("refused from an agent session")

    monkeypatch.setattr(agentcore, "run_aws", deny)
    result = agentcore.get_resource(CFG, "token-vault")
    assert (result.ok, result.denied) == (False, True)


def test_result_dicts_carry_every_flag() -> None:
    assert agentcore.ListResult(ok=True, truncated=True).to_dict() == {
        "ok": True,
        "items": [],
        "error": "",
        "denied": False,
        "truncated": True,
    }
    assert agentcore.ObjectResult(ok=False, error="x", denied=True).to_dict() == {
        "ok": False,
        "item": {},
        "error": "x",
        "denied": True,
    }
