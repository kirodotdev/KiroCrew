"""Instance-data integrity for the GitHub connector descriptor table.

Pure data assertions -- no I/O, no side effects. These pin the facts the
campaign evidence recorded, including the documented CONTRADICTIONS, against
what the descriptor table claims, so the table cannot silently drift from the
evidence or claim a guarantee an operation does not have.
"""

from __future__ import annotations

from kiro_crew.connections.vendors.github import descriptors as d
from kiro_crew.connections.vendors.github.descriptors import (
    ADMIN,
    DELETE,
    POLICY_LAYERS,
    READ,
    WRITE,
    IdempotencyClass,
    Pagination,
)
from kiro_crew.platform.governance import SCOPE_CATALOG


def test_all_45_operations_present() -> None:
    assert len(d.DESCRIPTORS) == 45


def test_effect_tally_matches_evidence() -> None:
    from collections import Counter

    tally = Counter(desc.effect for desc in d.DESCRIPTORS.values())
    assert tally[READ] == 24
    assert tally[WRITE] == 16
    assert tally[ADMIN] == 3
    assert tally[DELETE] == 2


def test_required_count_matches_evidence() -> None:
    assert sum(1 for desc in d.DESCRIPTORS.values() if desc.required) == 26


def test_every_policy_value_is_a_live_scope_catalog_member_or_none() -> None:
    catalog = set(SCOPE_CATALOG.keys())
    for desc in d.DESCRIPTORS.values():
        for layer, value in zip(POLICY_LAYERS, desc.policy.values()):
            if value is None:
                continue
            assert value in catalog, f"{desc.operation_id}.{layer}={value!r} not in catalog"


def test_policy_registers_no_new_scope() -> None:
    # Every non-null value the table uses must already exist; the union of used
    # scopes is a subset of the catalog, never a superset.
    used = {
        value
        for desc in d.DESCRIPTORS.values()
        for value in desc.policy.values()
        if value is not None
    }
    assert used <= set(SCOPE_CATALOG.keys())
    # The specific existing members this stream maps onto.
    assert used == {"network.egress", "approval_mode", "mcp"}


def test_read_effects_carry_none_read_idempotency() -> None:
    for desc in d.DESCRIPTORS.values():
        if desc.effect == READ:
            assert desc.idempotency_class is IdempotencyClass.NONE_READ
        else:
            assert desc.idempotency_class is not IdempotencyClass.NONE_READ


def test_mutating_effects_gate_session_on_approval_mode() -> None:
    for desc in d.DESCRIPTORS.values():
        if desc.effect in (WRITE, DELETE, ADMIN):
            assert desc.policy.session_scope == "approval_mode"
        else:
            assert desc.policy.session_scope is None


def test_platform_egress_and_mcp_provider_gating_except_non_mcp_ops() -> None:
    non_mcp_ops = {
        "gh_get_branch_protection_legacy",
        "gh_update_branch_protection_legacy",
        "gh_update_status_check_protection_legacy",
        "gh_rate_limit_status",
    }
    for op, desc in d.DESCRIPTORS.items():
        assert desc.policy.platform_scope == "network.egress"
        # The raw legacy REST endpoints and the meta rate-limit endpoint do not
        # dispatch through a github-mcp-server tool, so their provider layer is
        # None, not a false mcp claim; every MCP-tool operation is mcp.
        if op in non_mcp_ops:
            assert desc.policy.provider_scope is None, op
        else:
            assert desc.policy.provider_scope == "mcp", op
        # No connection scope exists in the catalog yet; null is the honest
        # encoding, and workspace imposes no local-filesystem scope.
        assert desc.policy.connection_scope is None
        assert desc.policy.workspace_scope is None


# --- documented CONTRADICTIONS, pinned as facts, not papered over ----------


def test_create_or_update_file_has_base_sha_guard() -> None:
    assert (
        d.DESCRIPTORS["gh_create_or_update_file"].idempotency_class
        is IdempotencyClass.BASE_SHA_GUARD
    )


def test_push_files_and_delete_file_lack_a_base_sha_guard() -> None:
    # The recorded contradiction: unlike create_or_update_file, these two carry
    # no expected-head-sha param. The table must NOT pretend they have a
    # base-SHA guard -- they are verify-by-readback.
    for op in ("gh_push_files", "gh_delete_file"):
        assert d.DESCRIPTORS[op].idempotency_class is IdempotencyClass.NONE_VERIFY_BY_READBACK


def test_merge_pull_request_uses_expected_head_sha_guard() -> None:
    assert (
        d.DESCRIPTORS["gh_merge_pull_request"].idempotency_class is IdempotencyClass.BASE_SHA_GUARD
    )


def test_cursor_paginated_ops_are_the_graphql_backed_ones() -> None:
    cursor_ops = {
        op for op, desc in d.DESCRIPTORS.items() if desc.pagination is Pagination.CURSOR_AFTER
    }
    # The recorded pagination inconsistency: these advance by 'after', not page.
    assert cursor_ops == {"gh_list_issues", "gh_list_dependabot_alerts"}


def test_rest_page_ops_do_not_overlap_cursor_ops() -> None:
    rest = {op for op, x in d.DESCRIPTORS.items() if x.pagination is Pagination.REST_PAGE}
    cursor = {op for op, x in d.DESCRIPTORS.items() if x.pagination is Pagination.CURSOR_AFTER}
    assert rest.isdisjoint(cursor)


def test_delete_repository_needs_delete_repo_scope() -> None:
    # A classic PAT needs delete_repo explicitly, not repo alone.
    assert "delete_repo" in d.DESCRIPTORS["gh_delete_repository"].scopes


def test_legacy_branch_protection_and_rate_limit_accept_service_to_service_auth() -> None:
    # The legacy protection endpoints and rate_limit additionally accept a
    # GitHub App installation token, which is the control plane's
    # service_to_service credential class -- named with the shared vocabulary,
    # not a vendor-specific string.
    for op in (
        "gh_get_branch_protection_legacy",
        "gh_update_branch_protection_legacy",
        "gh_update_status_check_protection_legacy",
        "gh_rate_limit_status",
    ):
        assert "service_to_service" in d.DESCRIPTORS[op].auth_modes


def test_every_auth_mode_is_a_control_plane_credential_mode() -> None:
    from kiro_crew.connections.control_plane import CREDENTIAL_MODES

    for desc in d.DESCRIPTORS.values():
        for auth_mode in desc.auth_modes:
            assert auth_mode in CREDENTIAL_MODES, (desc.operation_id, auth_mode)


def test_rate_limit_status_needs_no_scope() -> None:
    assert d.DESCRIPTORS["gh_rate_limit_status"].scopes == ()


def test_get_descriptor_roundtrip_and_unknown() -> None:
    assert d.get_descriptor("gh_merge_pull_request") is d.DESCRIPTORS["gh_merge_pull_request"]
    assert d.get_descriptor("gh_not_a_real_op") is None


def test_every_descriptor_names_at_least_one_tool() -> None:
    # A manifest entry naming no tool is not implementable.
    for desc in d.DESCRIPTORS.values():
        assert desc.tool_names, desc.operation_id


def test_http_methods_are_uppercase_verbs() -> None:
    valid = {"GET", "POST", "PUT", "PATCH", "DELETE"}
    for desc in d.DESCRIPTORS.values():
        assert desc.http_method in valid, (desc.operation_id, desc.http_method)


def test_every_effect_is_a_control_plane_effect() -> None:
    # The effect vocabulary is consumed from the control plane, not restated;
    # every descriptor's effect must be a member of its EFFECTS closed set.
    from kiro_crew.connections.control_plane import EFFECTS

    for desc in d.DESCRIPTORS.values():
        assert desc.effect in EFFECTS, (desc.operation_id, desc.effect)
