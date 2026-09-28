"""Resolve a GitHub capability signature to a campaign ``operation_id``.

A provider-capabilities discovery response carries, per row, the vendor's own
capability identifier -- a tool name, sometimes a tool name plus a method
selector (several github-mcp-server tools expose one name over many methods).
This module OWNS the GitHub-specific reading of such a signature back to the
campaign's stable ``operation_id``, so a discovery row can set
``matches_manifest`` correctly.

It is a pure lookup over the instance data in :mod:`.descriptors`: no I/O, and
it NEVER invents a manifest entry. A signature that maps to nothing returns
``None`` -- the discovery contract records that as a gap for a human to
register, and must not auto-write it into the manifest.

A method-qualified signature (``tool:method``) resolves ONLY through the
explicit :data:`_METHOD_QUALIFIED` table below; there is no "single operation,
so any method must be it" fallback. That fallback looked convenient but is
unsound: an invalid method against a one-operation tool
(``search_repositories:no_such_method``) would resolve to that operation and
report a false ``matches_manifest`` for a capability the vendor does not
expose. Requiring an explicit entry means an unrecognized method is a gap, not
a false match. The table's completeness for every multi-method tool is pinned
at import by :func:`_assert_method_table_complete`, so a newly-added
multi-method tool cannot silently mint false gaps (the design lane's request to
pin the two tables together, the way ``descriptors._validate`` pins policy to
``SCOPE_CATALOG``).
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from .descriptors import DESCRIPTORS

# Every ``(tool, method)`` the github-mcp-server multi-method tools document,
# mapped to the operation_id it belongs to. Methods are verbatim from the
# vendor's own input schemas (captured in the campaign evidence). A tool that
# splits its methods across two campaign operations (issue_read, issue_write)
# maps each method to the correct one; a tool that is one operation over many
# methods (pull_request_read, the actions_* tools, pull_request_review_write)
# maps every method to that single operation, so a valid sub-method resolves
# and an invalid one does not.
_METHOD_QUALIFIED: Dict[Tuple[str, str], str] = {
    # issue_read -> two operations
    ("issue_read", "get"): "gh_issue_read_get",
    ("issue_read", "get_sub_issues"): "gh_issue_read_get",
    ("issue_read", "get_comments"): "gh_issue_read_get_comments",
    # issue_write -> two operations
    ("issue_write", "create"): "gh_issue_write_create",
    ("issue_write", "update"): "gh_issue_write_update",
    # pull_request_read -> one operation, nine read methods
    ("pull_request_read", "get"): "gh_pull_request_read",
    ("pull_request_read", "get_diff"): "gh_pull_request_read",
    ("pull_request_read", "get_status"): "gh_pull_request_read",
    ("pull_request_read", "get_files"): "gh_pull_request_read",
    ("pull_request_read", "get_commits"): "gh_pull_request_read",
    ("pull_request_read", "get_review_comments"): "gh_pull_request_read",
    ("pull_request_read", "get_reviews"): "gh_pull_request_read",
    ("pull_request_read", "get_comments"): "gh_pull_request_read",
    ("pull_request_read", "get_check_runs"): "gh_pull_request_read",
    # pull_request_review_write -> one operation, five write methods
    ("pull_request_review_write", "create"): "gh_pull_request_review_write",
    ("pull_request_review_write", "submit"): "gh_pull_request_review_write",
    ("pull_request_review_write", "delete"): "gh_pull_request_review_write",
    ("pull_request_review_write", "resolve_thread"): "gh_pull_request_review_write",
    ("pull_request_review_write", "unresolve_thread"): "gh_pull_request_review_write",
    # actions_list -> one operation, four list methods
    ("actions_list", "list_workflows"): "gh_actions_list",
    ("actions_list", "list_workflow_runs"): "gh_actions_list",
    ("actions_list", "list_workflow_jobs"): "gh_actions_list",
    ("actions_list", "list_workflow_run_artifacts"): "gh_actions_list",
    # actions_get -> one operation, its detail methods
    ("actions_get", "get_workflow"): "gh_actions_get",
    ("actions_get", "get_workflow_run"): "gh_actions_get",
    ("actions_get", "get_workflow_run_usage"): "gh_actions_get",
    ("actions_get", "get_workflow_run_logs"): "gh_actions_get",
    ("actions_get", "get_workflow_job"): "gh_actions_get",
    ("actions_get", "download_workflow_run_artifact"): "gh_actions_get",
    # actions_run_trigger -> one operation, its trigger methods
    ("actions_run_trigger", "run_workflow"): "gh_actions_run_trigger",
    ("actions_run_trigger", "rerun_workflow_run"): "gh_actions_run_trigger",
    ("actions_run_trigger", "rerun_failed_jobs"): "gh_actions_run_trigger",
    ("actions_run_trigger", "cancel_workflow_run"): "gh_actions_run_trigger",
}


def _build_tool_index() -> Dict[str, Tuple[str, ...]]:
    """Map each tool name to the operation_ids it can back.

    Most tool names map to exactly one operation; a few (issue_read,
    issue_write) back several, which is why the value is a tuple and a
    ``tool:method`` signature is required to disambiguate those.
    """
    index: Dict[str, list] = {}
    for operation_id, descriptor in DESCRIPTORS.items():
        for tool_name in descriptor.tool_names:
            index.setdefault(tool_name, []).append(operation_id)
    return {tool: tuple(ops) for tool, ops in index.items()}


_TOOL_INDEX: Dict[str, Tuple[str, ...]] = _build_tool_index()


def _assert_method_table_complete() -> None:
    """Pin the method table to the tool index at import, fail-closed.

    Every tool that backs MORE THAN ONE operation MUST have at least one
    ``(tool, method)`` entry in :data:`_METHOD_QUALIFIED`, because a bare
    signature for such a tool is ambiguous and only a method can resolve it --
    a multi-operation tool with no method entries would make every one of its
    signatures a false gap. Also verify every mapped operation_id actually
    exists, so a typo in the table cannot point at nothing.
    """
    multi_op_tools = {tool for tool, ops in _TOOL_INDEX.items() if len(ops) > 1}
    covered_tools = {tool for (tool, _method) in _METHOD_QUALIFIED}
    missing = multi_op_tools - covered_tools
    if missing:
        raise ValueError(
            f"multi-operation tool(s) with no _METHOD_QUALIFIED entry: {sorted(missing)}; "
            f"a bare signature for these is ambiguous and would mint false discovery gaps"
        )
    known_ops = set(DESCRIPTORS)
    for (tool, method), operation_id in _METHOD_QUALIFIED.items():
        if operation_id not in known_ops:
            raise ValueError(
                f"_METHOD_QUALIFIED[{(tool, method)!r}] -> {operation_id!r}, not a known operation_id"
            )


_assert_method_table_complete()


def resolve_operation_id(signature: str) -> Optional[str]:
    """Resolve a capability signature to an ``operation_id``, or ``None``.

    Accepted signature shapes:

    * ``"<tool_name>"`` -- resolves when exactly one operation is backed by that
      tool. An ambiguous bare tool name (one backing several operations, e.g.
      ``issue_read``) resolves to ``None``: the reader will not guess which
      method was meant.
    * ``"<tool_name>:<method>"`` -- resolves ONLY through an explicit
      :data:`_METHOD_QUALIFIED` entry. An unrecognized method resolves to
      ``None`` even when the tool backs a single operation, so an invalid
      method never produces a false match.

    Returns ``None`` for an unknown, ambiguous, or unrecognized-method
    signature; the caller records that as a discovery gap, never a manifest
    write.
    """
    if not signature:
        return None
    signature = signature.strip()

    if ":" in signature:
        tool, _, method = signature.partition(":")
        return _METHOD_QUALIFIED.get((tool.strip(), method.strip()))

    ops = _TOOL_INDEX.get(signature)
    if ops is None:
        return None
    if len(ops) == 1:
        return ops[0]
    # Ambiguous: the tool backs several operations and no method was given.
    return None


def known_tool_names() -> Tuple[str, ...]:
    """Return every tool name that backs at least one operation, sorted."""
    return tuple(sorted(_TOOL_INDEX))
