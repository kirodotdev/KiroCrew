"""Provenance + independence tests for the vendored ACP v1 conformance oracle.

Two jobs:

1. **Provenance** — the files under ``test/conformance/vendor/acp-v1/`` are the
   verbatim upstream ACP v1 schema at the pinned tag, intact (checksum), the
   right dialect (Draft 2020-12), and the right wire version (1).

2. **Independence / drift-catch** — Kiro Crew's own :mod:`kiro_crew.acp.types`
   constants agree with the *vendored upstream* vocabulary. This is what makes
   the black-box gate independent: the oracle is built from the pinned schema
   (:mod:`acp_v1_vendor`), and here we assert the implementation's constants are
   a faithful subset of it. If Kiro Crew renames a method, invents a stop reason,
   or bumps the protocol version away from the pinned wire version, THIS test
   fails — the drift is caught against an external source of truth, not against
   the very constants the server emits with.
"""

from __future__ import annotations

import acp_v1_vendor as acp
import pytest
from acp_bb_schema import AcpSchemaError, _validate_config_option

from kiro_crew.acp import types as mc
from kiro_crew.acp_server.server import SUPPORTED_PROTOCOL_VERSION


class TestVendorProvenance:
    def test_checksums_dialect_and_version(self) -> None:
        summary = acp.verify_provenance()
        assert summary["dialect"].endswith("2020-12/schema")
        assert summary["version"] == 1
        assert summary["checked_files"] >= 5
        assert summary["tag"] == "schema-v1.21.0"
        assert summary["commit"] == "272bf799f35a258c6a4107a0410ed361e83683d3"

    def test_meta_method_tables_present(self) -> None:
        # Sanity: the authoritative method table loaded and contains the baseline.
        assert "initialize" in acp.agent_methods()
        assert "session/prompt" in acp.agent_methods()
        assert "session/update" in acp.client_methods()
        assert "session/request_permission" in acp.client_methods()

    def test_closed_vocabularies_load(self) -> None:
        assert acp.stop_reasons() == {
            "end_turn",
            "max_tokens",
            "max_turn_requests",
            "refusal",
            "cancelled",
        }
        assert {"text", "image", "audio", "resource_link", "resource"} <= acp.content_block_types()
        assert {"allow_once", "allow_always", "reject_once", "reject_always"} <= (
            acp.permission_option_kinds()
        )
        assert {"agent_message_chunk", "tool_call"} <= acp.session_update_kinds()


class TestKiroCrewConformsToVendoredSchema:
    """The implementation's ACP constants must be a faithful subset of upstream."""

    def test_protocol_version_matches_pinned_wire_version(self) -> None:
        assert SUPPORTED_PROTOCOL_VERSION == acp.protocol_version() == 1

    def test_all_implemented_methods_are_real_acp_methods(self) -> None:
        implemented = {
            mc.METHOD_INITIALIZE,
            mc.METHOD_SESSION_NEW,
            mc.METHOD_SESSION_LOAD,
            mc.METHOD_SESSION_LIST,
            mc.METHOD_SESSION_RESUME,
            mc.METHOD_PROMPT,
            mc.METHOD_CANCEL,
            mc.METHOD_SESSION_UPDATE,
            mc.METHOD_REQUEST_PERMISSION,
        }
        unknown = implemented - acp.all_methods()
        assert not unknown, f"Kiro Crew implements non-ACP methods: {sorted(unknown)}"

    def test_valid_stop_reasons_are_a_subset_of_schema(self) -> None:
        extra = set(mc.ACP_VALID_STOP_REASONS) - acp.stop_reasons()
        assert not extra, f"Kiro Crew ACP_VALID_STOP_REASONS not in schema StopReason: {extra}"
        # And every schema stop reason is representable (no silent omission).
        assert set(mc.ACP_VALID_STOP_REASONS) == acp.stop_reasons()

    def test_emitted_session_update_kinds_are_in_schema(self) -> None:
        emitted = {
            mc.UPDATE_AGENT_MESSAGE_CHUNK,
            mc.UPDATE_AGENT_THOUGHT_CHUNK,
            mc.UPDATE_USER_MESSAGE_CHUNK,
            mc.UPDATE_TOOL_CALL,
            mc.UPDATE_TOOL_CALL_UPDATE,
        }
        unknown = emitted - acp.session_update_kinds()
        assert not unknown, f"Kiro Crew emits non-schema session/update kinds: {sorted(unknown)}"

    def test_permission_option_kinds_are_in_schema(self) -> None:
        kinds = {
            mc.OPTION_ALLOW_ONCE,
            mc.OPTION_ALLOW_ALWAYS,
            mc.OPTION_REJECT_ONCE,
            mc.OPTION_REJECT_ALWAYS,
        }
        unknown = kinds - acp.permission_option_kinds()
        assert not unknown, f"Kiro Crew permission kinds not in schema: {sorted(unknown)}"

    def test_jsonrpc_error_codes_match_standard(self) -> None:
        assert mc.JSONRPC_PARSE_ERROR == acp.JSONRPC_PARSE_ERROR
        assert mc.JSONRPC_INVALID_REQUEST == acp.JSONRPC_INVALID_REQUEST
        assert mc.JSONRPC_METHOD_NOT_FOUND == acp.JSONRPC_METHOD_NOT_FOUND
        assert mc.JSONRPC_INVALID_PARAMS == acp.JSONRPC_INVALID_PARAMS
        assert mc.JSONRPC_INTERNAL_ERROR == acp.JSONRPC_INTERNAL_ERROR
        # The five standard codes Kiro Crew may emit are exactly the standard set.
        emitted = {
            mc.JSONRPC_PARSE_ERROR,
            mc.JSONRPC_INVALID_REQUEST,
            mc.JSONRPC_METHOD_NOT_FOUND,
            mc.JSONRPC_INVALID_PARAMS,
            mc.JSONRPC_INTERNAL_ERROR,
        }
        assert emitted <= acp.ALLOWED_JSONRPC_ERROR_CODES
        assert emitted == acp.STANDARD_JSONRPC_ERROR_CODES


class TestConfigOptionShapeValidation:
    def test_boolean_option_uses_current_value(self) -> None:
        option = {
            "id": "flag",
            "name": "Flag",
            "type": "boolean",
            "currentValue": True,
        }
        _validate_config_option(option, option)

    def test_boolean_option_rejects_request_value_field(self) -> None:
        option = {"id": "flag", "name": "Flag", "type": "boolean", "value": True}
        with pytest.raises(AcpSchemaError, match="currentValue"):
            _validate_config_option(option, option)
