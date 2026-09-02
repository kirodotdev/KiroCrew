"""Regression tests for Slack log sanitization (_safe_log).

Covers CWE-117 (log injection via CR/LF) and CWE-532 (customer data /
credentials written verbatim to logs) for the Slack slash-command text and
inbound message body log sites in ``slack/events.py``.
"""

from kiro_crew.slack.events import _safe_log


def test_safe_log_strips_crlf_and_tab():
    # A forged log line embedded via newlines must be flattened to one line.
    injected = "hello\n2024-01-01 INFO kiro_crew: FORGED ENTRY\r\tmore"
    out = _safe_log(injected)
    assert "\n" not in out
    assert "\r" not in out
    assert "\t" not in out
    # Content is preserved (flattened), just made single-line.
    assert "FORGED ENTRY" in out


def test_safe_log_redacts_credentials():
    # An AWS access key id must not survive verbatim into the log string.
    secret = "my key is AKIAIOSFODNN7EXAMPLE ok"
    out = _safe_log(secret)
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_safe_log_empty_and_plain():
    assert _safe_log("") == ""
    assert _safe_log("plain text") == "plain text"
