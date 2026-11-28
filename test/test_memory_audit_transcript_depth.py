"""A single unreadable row must not hide the rest of a security audit."""

import json

import pytest
from stray_line_helpers import cyclic_gc_quiesced, too_deep_line, too_deep_to_decode  # noqa: F401

from kiro_crew.security.memory_audit import scan_history


@pytest.mark.usefixtures("cyclic_gc_quiesced")
def test_a_deep_row_does_not_hide_valid_transcript_rows(tmp_path):
    path = tmp_path / "session.jsonl"
    before = json.dumps({"role": "assistant", "content": "hello"}).encode()
    after = json.dumps(
        {
            "role": "tool",
            "meta": {
                "kind": "execute",
                "input": json.dumps({"command": "chmod 777 /srv/app"}),
            },
        }
    ).encode()
    nested_input = json.dumps(
        {"role": "tool", "meta": {"kind": "execute", "input": too_deep_to_decode()}}
    ).encode()
    path.write_bytes(before + b"\n" + too_deep_line() + nested_input + b"\n" + after + b"\n")

    findings, scanned = scan_history(tmp_path)

    assert scanned == 2
    assert len(findings) == 2
    assert "nesting limit" in findings[0]["warning"]
    assert findings[0]["snippet"] == ""
    assert findings[1]["snippet"] == "chmod 777 /srv/app"
