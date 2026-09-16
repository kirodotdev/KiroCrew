"""Capability-signature -> operation_id resolution tests. Pure lookup."""

from __future__ import annotations

from kiro_crew.connections.vendors.github import descriptors as d
from kiro_crew.connections.vendors.github.signatures import (
    known_tool_names,
    resolve_operation_id,
)


def test_unambiguous_tool_name_resolves() -> None:
    assert resolve_operation_id("search_repositories") == "gh_search_repositories"
    assert resolve_operation_id("merge_pull_request") == "gh_merge_pull_request"


def test_ambiguous_bare_tool_name_resolves_to_none() -> None:
    # issue_read backs two operations; a bare signature must not guess which,
    # because a wrong guess sets matches_manifest on the wrong discovery row.
    assert resolve_operation_id("issue_read") is None


def test_method_qualified_signature_resolves_the_right_operation() -> None:
    assert resolve_operation_id("issue_read:get") == "gh_issue_read_get"
    assert resolve_operation_id("issue_read:get_comments") == "gh_issue_read_get_comments"
    # issue_write's two methods split across its two operations.
    assert resolve_operation_id("issue_write:create") == "gh_issue_write_create"
    assert resolve_operation_id("issue_write:update") == "gh_issue_write_update"


def test_method_qualified_on_single_op_tool_resolves_only_documented_methods() -> None:
    # pull_request_read backs exactly one operation; its documented sub-methods
    # resolve to that op via explicit entries.
    assert resolve_operation_id("pull_request_read:get_review_comments") == "gh_pull_request_read"
    assert resolve_operation_id("pull_request_read:get_diff") == "gh_pull_request_read"


def test_invalid_method_on_single_op_tool_is_none_not_a_false_match() -> None:
    # A single-operation tool with an UNRECOGNIZED method must resolve to None,
    # not to that operation -- there is no "one op, so any method is it"
    # fallback, which would report a false matches_manifest for a capability
    # the vendor does not expose.
    assert resolve_operation_id("search_repositories:no_such_method") is None
    assert resolve_operation_id("pull_request_read:not_a_real_method") is None


def test_method_qualified_on_ambiguous_tool_with_unmapped_method_is_none() -> None:
    # issue_read backs two operations; an unmapped method cannot be guessed.
    assert resolve_operation_id("issue_read:totally_unknown") is None


def test_unknown_signature_resolves_to_none_never_a_manifest_write() -> None:
    assert resolve_operation_id("totally_unknown_capability") is None
    assert resolve_operation_id("issue_read:no_such_method") is None


def test_every_multi_op_tool_is_covered_by_the_method_table() -> None:
    # Mirrors the import-time assertion: every tool backing >1 operation has at
    # least one method-qualified entry, so no multi-op tool mints false gaps.
    from collections import Counter

    from kiro_crew.connections.vendors.github.signatures import _METHOD_QUALIFIED

    tool_op_count = Counter(t for desc in d.DESCRIPTORS.values() for t in desc.tool_names)
    covered = {tool for (tool, _m) in _METHOD_QUALIFIED}
    for tool, n in tool_op_count.items():
        if n > 1:
            assert tool in covered, tool


def test_empty_or_blank_signature_resolves_to_none() -> None:
    assert resolve_operation_id("") is None
    assert resolve_operation_id("   ") is None


def test_signature_is_whitespace_trimmed() -> None:
    assert resolve_operation_id("  search_repositories  ") == "gh_search_repositories"


def test_known_tool_names_are_sorted_and_cover_every_operation() -> None:
    names = known_tool_names()
    assert list(names) == sorted(names)
    # Every tool named by any descriptor appears in the index.
    all_tools = {t for desc in d.DESCRIPTORS.values() for t in desc.tool_names}
    assert set(names) == all_tools


def test_every_unambiguous_operation_round_trips_through_its_tool() -> None:
    # For each descriptor whose (single) tool backs only that operation, the
    # bare tool name resolves back to it.
    from collections import Counter

    tool_usage = Counter(t for desc in d.DESCRIPTORS.values() for t in desc.tool_names)
    for op, desc in d.DESCRIPTORS.items():
        if len(desc.tool_names) == 1 and tool_usage[desc.tool_names[0]] == 1:
            assert resolve_operation_id(desc.tool_names[0]) == op
