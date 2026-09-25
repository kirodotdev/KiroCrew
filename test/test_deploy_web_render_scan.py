"""Tests for deploy_web render_standalone + pre-publish scan."""
from __future__ import annotations

from kiro_crew.deploy.render import render_standalone
from kiro_crew.deploy.scan import scan_content, summarize

# --- render ---------------------------------------------------------------


def test_widget_wrapped_in_standalone_shell():
    out = render_standalone("widget", "<div class='card'>Hi</div>", title="My Widget")
    assert out.startswith("<!DOCTYPE html>")
    assert "<meta name=\"viewport\"" in out
    assert "--bg:#ffffff" in out  # fixed light theme inlined
    assert "<div class='card'>Hi</div>" in out
    assert "<title>My Widget</title>" in out


def test_markdown_rendered_to_html():
    md = "# Title\n\nSome **bold** and `code`.\n\n- one\n- two\n"
    out = render_standalone("markdown", md)
    assert "<h1>Title</h1>" in out
    assert "<strong>bold</strong>" in out
    assert "<code>code</code>" in out
    assert "<ul>" in out and "<li>one</li>" in out
    assert out.startswith("<!DOCTYPE html>")


def test_markdown_escapes_html():
    out = render_standalone("markdown", "a <script>alert(1)</script> b")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_markdown_link_href_keeps_a_balanced_pair():
    url = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    out = render_standalone("markdown", f"see [Python]({url}) now")
    assert f'<a href="{url}">Python</a> now' in out


def test_markdown_link_with_an_unbalanced_url_is_left_as_text():
    out = render_standalone("markdown", "see [a](https://example.com/(b) now")
    assert "<a href" not in out
    assert "see [a](https://example.com/(b) now" in out


def test_a_quote_in_a_parenthesised_link_url_stays_escaped_in_the_href():
    out = render_standalone("markdown", 'see [a](https://example.com/(b"onmouseover="x)) now')
    assert '<a href="https://example.com/(b&quot;onmouseover=&quot;x)">a</a> now' in out
    assert 'onmouseover="' not in out


def test_full_html_passthrough():
    doc = "<html><head></head><body>hi</body></html>"
    assert render_standalone("html", doc) == doc


def test_html_fragment_gets_wrapped():
    out = render_standalone("html", "<p>fragment</p>")
    assert out.startswith("<!DOCTYPE html>")
    assert "<p>fragment</p>" in out


# --- scan -----------------------------------------------------------------

def test_scan_clean_content():
    findings = scan_content("<html><body><h1>Hello world</h1></body></html>")
    assert findings == []
    assert "No secrets" in summarize(findings)


def test_scan_flags_aws_access_key():
    findings = scan_content("key AKIAABCDEFGHIJKLMNOP here")
    assert any(f.kind == "credential" for f in findings)


def test_scan_does_not_flag_the_redactors_own_output():
    """The redactor keeps the key that names a value and replaces the value alone,
    so rendered output it already cleaned reads `aws_secret_access_key=[REDACTED:
    credential]` -- a line the raw patterns match again. The scan reads the
    redactor's live matches instead: cleaned text is not a finding, the secret it
    replaced is, and a tag with bytes glued to it is a value and is."""
    from kiro_crew.security import REDACTED_CREDENTIAL_TAG, redact_credentials

    secret = "aws_secret_access_key=test-secret-not-a-credential-0123\n"
    assert any(f.kind == "credential" for f in scan_content(secret))

    cleaned, warnings = redact_credentials(secret)
    assert warnings and cleaned == f"aws_secret_access_key={REDACTED_CREDENTIAL_TAG}\n"
    assert [f for f in scan_content(cleaned) if f.kind == "credential"] == []
    assert [
        f
        for f in scan_content(f'"SecretAccessKey": "{REDACTED_CREDENTIAL_TAG}"\n')
        if f.kind == "credential"
    ] == []

    glued = f"aws_secret_access_key={REDACTED_CREDENTIAL_TAG}test-secret-not-a-credential-0123\n"
    assert any(f.kind == "credential" for f in scan_content(glued))


def test_scan_flags_internal_host_and_arn():
    text = "see https://w.amazon.com/bin/foo and arn:aws:s3:::secret-bucket/x"
    kinds = {f.kind for f in scan_content(text)}
    assert "internal-host" in kinds
    assert "aws-arn" in kinds


def test_scan_flags_account_id():
    findings = scan_content("account 123456789012 is ours")
    assert any(f.kind == "aws-account-id" for f in findings)


def test_scan_account_id_not_double_counted_with_arn():
    # An ARN line carrying a 12-digit id should not also yield a bare account-id finding.
    text = "arn:aws:iam::123456789012:role/foo"
    kinds = [f.kind for f in scan_content(text)]
    assert "aws-arn" in kinds
    assert "aws-account-id" not in kinds


def test_summarize_reports_line_numbers():
    text = "clean line\nAKIAABCDEFGHIJKLMNOP\n"
    s = summarize(scan_content(text))
    assert "line 2" in s
    assert "credential" in s


def test_scan_credential_snippet_is_masked():
    """Credential snippets MUST NOT contain the raw matched text (security)."""
    findings = scan_content("key AKIAABCDEFGHIJKLMNOP here")
    cred = [f for f in findings if f.kind == "credential"][0]
    # Must be a masked fingerprint: first 4 chars + length
    assert cred.snippet == "AKIA…(20 chars)"
    # Must NOT contain the full key
    assert "ABCDEFGHIJKLMNOP" not in cred.snippet


def test_scan_internal_host_snippet_is_unmasked():
    """internal-host and aws-arn are NOT secrets — their snippets are raw."""
    findings = scan_content("see https://w.amazon.com/bin/foo")
    host = [f for f in findings if f.kind == "internal-host"][0]
    assert host.snippet == "w.amazon.com"
