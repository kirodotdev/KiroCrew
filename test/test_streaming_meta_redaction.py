"""Test that permission meta is redacted before streaming to dashboard."""

import json

from kiro_crew.dashboard.chat import _build_stream_chunk, _redact_deep
from kiro_crew.testing.synthetic_credentials import SAMPLE_AWS_SECRET, github_classic

# A synthetic 40-character AWS secret access key (not the documentation example).
SECRET = SAMPLE_AWS_SECRET


def _build_streaming_chunk(msg: dict) -> dict:
    """Call the actual production helper and parse the JSON result."""
    return json.loads(_build_stream_chunk(msg))


class TestStreamingMetaRedaction:
    def test_credential_in_tool_input_is_redacted(self):
        """tool_input containing AWS credentials should be redacted."""
        meta_json = json.dumps({
            "request_id": "req-123",
            "tool_input": f"aws configure set aws_secret_access_key {SECRET}",
            "is_read_only": "1",
        })
        msg = {"role": "permission", "content": "shell", "cls": meta_json, "ts": "t1"}
        chunk = _build_streaming_chunk(msg)

        assert "meta" in chunk
        assert SECRET not in chunk["meta"]["tool_input"]

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
        token = github_classic(seed=46)
        nested = {"params": {"secret": token}, "tags": ["safe", token]}
        result = _redact_deep(nested)

        assert token not in result["params"]["secret"]
        assert token not in result["tags"][1]
        assert result["tags"][0] == "safe"
