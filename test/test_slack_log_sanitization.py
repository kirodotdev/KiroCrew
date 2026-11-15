"""Regression tests for Slack log sanitization (_safe_log).

Covers CWE-117 (log injection via CR/LF) and CWE-532 (customer data /
credentials written verbatim to logs) for the Slack slash-command text
(``slack/events_slash.py``) and inbound message body (``slack/events.py``) log sites.
"""

from redaction_structured_cases import github_classic

from kiro_crew.slack.events import _safe_log

# A GitHub token with a valid checksum, which the plan redacts. An AWS key ID
# stays visible as an identifier, so it cannot show that a value is redacted.
_SECRET_TOKEN = github_classic(seed=35)


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
    # A GitHub token must not survive verbatim into the log string.
    secret = f"my key is {_SECRET_TOKEN} ok"
    out = _safe_log(secret)
    assert _SECRET_TOKEN not in out


def test_safe_log_empty_and_plain():
    assert _safe_log("") == ""
    assert _safe_log("plain text") == "plain text"
