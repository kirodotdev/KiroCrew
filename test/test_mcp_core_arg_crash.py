"""Regression tests: every internally-validated kirocrew-core MCP tool must be
registered in MCP_CORE_SCHEMAS so a malformed/missing arg returns a clean
"Error:" string instead of raising a ValidationError out of the stdio loop.

Background: the delete_message fix schema-gated one tool (a missing arg crashed
the whole kirocrew-core server), but the workflow_* tools that call
``validate_tool_args(args, <SCHEMA>)`` inside their handler were never added to
MCP_CORE_SCHEMAS. The outer guard in ``call_tool_with_logging`` only catches
ValidationError from the *registered* schema lookup (``_validate_args``); an
unregistered tool passed args through raw and its internal validate raised,
propagating out of ``run_mcp_core_server``'s ``while True`` stdio loop and
terminating every kirocrew-core tool for the session (and, when the backend is
pooled, for every attached session) until respawn.
"""

from __future__ import annotations

from kiro_crew.mcp_core import _call_tool
from kiro_crew.validation import MCP_CORE_SCHEMAS

# Tools that validate their args internally and were previously absent from
# MCP_CORE_SCHEMAS. Each entry: (tool_name, args_that_should_fail_validation).
# A required arg is omitted, which raised the crash pre-fix.
_PREVIOUSLY_UNGATED = [
    ("workflow_author", {}),
    ("workflow_run", {}),
    ("workflow_status", {}),
    ("workflow_result", {}),
    ("workflow_cancel", {}),
    ("workflow_rerun_subtree", {}),
]


class TestMcpCoreToolArgCrash:
    def test_bad_args_return_error_not_raise(self):
        """The core assertion: a malformed call returns a clean "Error:" string.

        Pre-fix each of these raised ValidationError out of the stdio loop,
        killing the server. Post-fix the outer guard converts it to an error.
        """
        for tool, bad_args in _PREVIOUSLY_UNGATED:
            result = _call_tool(tool, bad_args)
            assert isinstance(result, str), f"{tool} returned non-str {result!r}"
            assert result.lower().startswith("error"), (
                f"{tool} did not return a clean error for {bad_args!r}: {result!r}"
            )

    def test_all_internally_validated_tools_are_registered(self):
        """Guards against re-introducing the crash for any future tool: every
        tool listed here must be schema-gated at the registry boundary."""
        for tool, _ in _PREVIOUSLY_UNGATED:
            assert tool in MCP_CORE_SCHEMAS, (
                f"{tool} validates args internally but is not in MCP_CORE_SCHEMAS "
                f"— a bad arg would crash the kirocrew-core stdio loop"
            )
