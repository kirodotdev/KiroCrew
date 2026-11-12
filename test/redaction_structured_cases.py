"""Synthetic, obviously fake test values for the structured credential detector.

Every private-key armour line is written as two adjacent string literals so repo
content scanners do not read this file as holding a key.
"""

from __future__ import annotations

import base64
import json
import pathlib
import string
import sys
from collections.abc import Callable
from types import FrameType
from typing import Any

import kiro_crew.security.redaction_structured as _package
from kiro_crew.security.redaction_structured.checks import base62_crc32

_PACKAGE_DIR = str(pathlib.Path(_package.__file__).parent)

UPPER_DIGITS = string.ascii_uppercase + string.digits
ALNUM = string.ascii_letters + string.digits
B64 = ALNUM + "+/"
B64URL = ALNUM + "-_"
HEX = "0123456789abcdef"


def package_lines(run: Callable[[], object]) -> int:
    """The source lines the detectors' package executes for ``run()``: a work count.

    Counted with a line tracer limited to the package's own files, so the count
    is the same on every runner, however loaded.
    """
    count = 0

    def local(frame: FrameType, event: str, _arg: Any) -> Any:
        nonlocal count
        if event == "line":
            count += 1
        return local

    def tracer(frame: FrameType, _event: str, _arg: Any) -> Any:
        return local if frame.f_code.co_filename.startswith(_PACKAGE_DIR) else None

    before = sys.gettrace()
    sys.settrace(tracer)
    try:
        run()
    finally:
        sys.settrace(before)
    return count


def assert_linear_work(build: Callable[[int], str], run: Callable[[str], object]) -> None:
    """Doubling the input at most about doubles the package lines ``run`` executes.

    Both sizes are several times every unit's own length bound (the largest is
    64 KiB), so a per-anchor read up to that bound is linear here, and a read
    that grows with the text is not.
    """
    small = package_lines(lambda: run(build(1 << 18)))
    large = package_lines(lambda: run(build(1 << 19)))
    assert large <= small * 5 // 2 + 1000, (small, large)


def synth(n: int, alphabet: str = ALNUM, seed: int = 0) -> str:
    """A deterministic string of ``n`` characters that mixes the alphabet's classes."""
    step = 7 if len(alphabet) % 7 else 11
    return "".join(alphabet[(seed * 13 + i * step) % len(alphabet)] for i in range(n))


def b64url_json(obj: dict[str, object]) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def github_classic(prefix: str = "ghp_", seed: int = 1) -> str:
    body = synth(30, ALNUM, seed)
    return prefix + body + base62_crc32(body)


def github_fine_grained(seed: int = 2) -> str:
    return "github_pat_" + synth(22, ALNUM, seed) + "_" + synth(59, ALNUM, seed + 1)


def aws_key_id(prefix: str = "AKIA", seed: int = 2) -> str:
    return prefix + synth(16, UPPER_DIGITS, seed)


def aws_secret(seed: int = 3) -> str:
    return synth(40, B64, seed)


def session_token(length: int = 360, seed: int = 4) -> str:
    return "IQoJb3JpZ2luX2VjE" + synth(length - 17, B64, seed)


def jwt(header: dict[str, object] | None = None, seed: int = 5) -> str:
    head = b64url_json(header if header is not None else {"alg": "HS256", "typ": "JWT"})
    payload = b64url_json({"sub": "user-1", "iat": 1700000000})
    return f"{head}.{payload}.{synth(43, B64URL, seed)}"


def jwe(direct: bool = False, seed: int = 130, tag_len: int = 22) -> str:
    header = b64url_json({"alg": "dir" if direct else "A256KW", "enc": "A256GCM"})
    key = "" if direct else synth(43, B64URL, seed)
    return ".".join(
        [
            header,
            key,
            synth(16, B64URL, seed + 1),
            synth(64, B64URL, seed + 2),
            synth(tag_len, B64URL, seed + 3),
        ]
    )


def jwe_short_tag() -> str:
    """The direct-key JWE shape of test_redaction_jwt_scan_linear.py: a 3-character tag."""

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "dir", "enc": "A256GCM"}).encode())
    return f"{header}..{b64(b'iv' * 6)}.{b64(b'ct' * 9)}.tag"


def link_token(seed: int = 150, claims: bool = True) -> str:
    body: dict[str, object] = {
        "sub": synth(8, ALNUM, seed),
        "exp": 1800000000,
        "iat": 1700000000,
        "nonce": synth(16, HEX, seed + 1),
        "gen": 1,
        "session_exp": 1750000000,
    }
    if not claims:
        body = {"subject": synth(40, ALNUM, seed), "expires": 1800000000, "x": synth(30, HEX, 1)}
    return f"{b64url_json(body)}.{synth(43, B64URL, seed + 2)}"


PEM_BEGIN = "-----BEGIN RSA " "PRIVATE KEY-----"
PEM_END = "-----END RSA " "PRIVATE KEY-----"
PEM_BEGIN_PLAIN = "-----BEGIN " "PRIVATE KEY-----"
PEM_END_PLAIN = "-----END " "PRIVATE KEY-----"
PGP_BEGIN = "-----BEGIN PGP " "PRIVATE KEY BLOCK-----"
PGP_END = "-----END PGP " "PRIVATE KEY BLOCK-----"
CERT_BEGIN = "-----BEGIN CERTIFICATE-----"
CERT_END = "-----END CERTIFICATE-----"
PEM_BODY = "\n".join(synth(64, B64, s) for s in range(4))
PEM_BLOCK = f"{PEM_BEGIN}\n{PEM_BODY}\n{PEM_END}"
PGP_BLOCK = f"{PGP_BEGIN}\n\n{PEM_BODY}\n{PGP_END}"


# (name, text, expected redacted text, expected secret rule ids)
def secret_cases() -> list[tuple[str, str, str, list[str]]]:
    T = "[REDACTED: credential]"
    gh = github_classic()
    secret = aws_secret()
    key_id = aws_key_id()
    sess = session_token()
    cases: list[tuple[str, str, str, list[str]]] = [
        ("github.classic", f"token {gh} end", f"token {T} end", ["github.classic"]),
        ("github.classic.gho", f"x={github_classic('gho_', 9)}", f"x={T}", ["github.classic"]),
        (
            "github.fine_grained",
            f"pat: {github_fine_grained()}",
            f"pat: {T}",
            ["github.fine_grained"],
        ),
        (
            "slack.token",
            "slack xoxb-1234567890-0987654321-AbCdEfGh ok",
            f"slack {T} ok",
            ["slack.token"],
        ),
        ("stripe.key", f"STRIPE=sk_live_{synth(24, ALNUM, 7)}", f"STRIPE={T}", ["stripe.key"]),
        ("stripe.key.rk", f"rk_test_{synth(30, ALNUM, 8)}.", f"{T}.", ["stripe.key"]),
        ("stripe.webhook_secret", f"whsec_{synth(32, ALNUM, 9)}", T, ["stripe.webhook_secret"]),
        (
            "stripe.webhook_secret.64",
            f"'whsec_{synth(64, ALNUM, 9)}'",
            f"'{T}'",
            ["stripe.webhook_secret"],
        ),
        (
            "anthropic.key",
            f"key sk-ant-api03-{synth(95, B64URL, 10)} end",
            f"key {T} end",
            ["anthropic.key"],
        ),
        ("anthropic.admin", f"sk-ant-admin01-{synth(90, B64URL, 11)}", T, ["anthropic.key"]),
        ("google.api_key", f'"AIza{synth(35, B64URL, 12)}"', f'"{T}"', ["google.api_key"]),
        (
            "google.oauth_client_secret",
            f"secret: GOCSPX-{synth(28, B64URL, 13)}",
            f"secret: {T}",
            ["google.oauth_client_secret"],
        ),
        ("gitlab.pat", f"glpat-{synth(20, B64URL, 14)}", T, ["gitlab.pat"]),
        (
            "sendgrid.key",
            f"SG.{synth(22, B64URL, 15)}.{synth(43, B64URL, 16)}.",
            f"{T}.",
            ["sendgrid.key"],
        ),
        (
            "hashicorp.vault",
            f"VAULT_TOKEN=hvs.{synth(90, B64URL, 17)}",
            f"VAULT_TOKEN={T}",
            ["hashicorp.vault"],
        ),
        ("shopify.token", f"shpat_{synth(32, HEX, 18)}", T, ["shopify.token"]),
        (
            "digitalocean.token",
            f"do: dop_v1_{synth(64, HEX, 19)}",
            f"do: {T}",
            ["digitalocean.token"],
        ),
        ("jwt", f"Authorization: Bearer {jwt()}", f"Authorization: Bearer {T}", ["jwt"]),
        ("jwt.sentence_end", f"the token is {jwt()}.", f"the token is {T}.", ["jwt"]),
        ("jwe", f"enc {jwe()} done", f"enc {T} done", ["jwe"]),
        ("jwe.direct", f"{jwe(direct=True)}", T, ["jwe"]),
        ("link_token", f"https://crew.example.test/#t={link_token()}", None, ["link_token"]),  # type: ignore[list-item]
        (
            "aws.pair",
            f"key {key_id} secret {secret}",
            f"key {key_id} secret {T}",
            ["aws.secret_access_key"],
        ),
        (
            "aws.pair.before",
            f"{secret}\n...\n{aws_key_id('ASIA', 4)}",
            f"{T}\n...\n{aws_key_id('ASIA', 4)}",
            ["aws.secret_access_key"],
        ),
        (
            # Upper-case hex is not git's object-name format, so the pairing takes it.
            "aws.pair.upper_hex_40",
            f"{key_id} {synth(40, HEX, 5).upper()}",
            f"{key_id} {T}",
            ["aws.secret_access_key"],
        ),
        (
            "aws.pair.mixed_case_hex_40",
            f"{key_id} {synth(39, HEX, 5)}A",
            f"{key_id} {T}",
            ["aws.secret_access_key"],
        ),
        (
            "url.oauth.client_secret_query",
            f"https://auth.example.test/token?client_id=app1&client_secret={synth(40, B64URL, 31)}&x=1",
            f"https://auth.example.test/token?client_id=app1&client_secret={T}&x=1",
            ["url.oauth_param.client_secret"],
        ),
        (
            "url.oauth.access_token_fragment",
            f"go https://app.example.test/cb#access_token={synth(36, B64URL, 32)}&token_type=bearer",
            f"go https://app.example.test/cb#access_token={T}&token_type=bearer",
            ["url.oauth_param.access_token"],
        ),
        (
            "url.oauth.refresh_token_at_end",
            f"(see https://auth.example.test/r?grant=x&refresh_token={synth(30, B64URL, 33)}).",
            f"(see https://auth.example.test/r?grant=x&refresh_token={T}).",
            ["url.oauth_param.refresh_token"],
        ),
        (
            "url.oauth.encoded_key",
            f"https://auth.example.test/cb?access%5Ftoken={synth(30, B64URL, 34)}&a=1",
            f"https://auth.example.test/cb?access%5Ftoken={T}&a=1",
            ["url.oauth_param.access_token"],
        ),
        (
            "url.oauth.encoded_letter_upper_key",
            f"https://auth.example.test/cb?%49D_TOKEN={synth(30, B64URL, 35)}",
            f"https://auth.example.test/cb?%49D_TOKEN={T}",
            ["url.oauth_param.id_token"],
        ),
        (
            "url.oauth.code_verifier_and_password",
            f"https://h.example.test/x?code_verifier={synth(43, B64URL, 36)}&password=hunter2x&q=1",
            f"https://h.example.test/x?code_verifier={T}&password={T}&q=1",
            ["url.oauth_param.code_verifier", "url.oauth_param.password"],
        ),
        (
            "url.oauth.spa_fragment_route",
            f"https://app.example.test/#/cb?access_token={synth(30, B64URL, 37)}",
            f"https://app.example.test/#/cb?access_token={T}",
            ["url.oauth_param.access_token"],
        ),
        ("pem", f"key:\n{PEM_BLOCK}\nafter", f"key:\n{T}\nafter", ["pem.private_key"]),
        (
            "pem.plain_type",
            f"{PEM_BEGIN_PLAIN}\n{PEM_BODY}\n{PEM_END_PLAIN} tail",
            f"{T} tail",
            ["pem.private_key"],
        ),
        (
            "pem.json_escaped",
            '{"private_key": "' + PEM_BLOCK.replace("\n", "\\n") + '\\n", "x": 1}',
            '{"private_key": "' + T + '\\n", "x": 1}',
            ["pem.private_key"],
        ),
        (
            "pem.unterminated",
            f"before\n{PEM_BEGIN}\n{PEM_BODY}\nmore text",
            f"before\n{T}",
            ["pem.private_key_unterminated"],
        ),
        (
            "pem.end_type_mismatch",
            f"{PEM_BEGIN}\n{PEM_BODY}\n{PEM_END_PLAIN}\nafter",
            T,
            ["pem.private_key_unterminated"],
        ),
        ("pgp", f"{PGP_BLOCK}\nok", f"{T}\nok", ["pgp.private_key"]),
        (
            "pgp.unterminated",
            f"x {PGP_BEGIN}\n{PEM_BODY}",
            f"x {T}",
            ["pgp.private_key_unterminated"],
        ),
        (
            "aws.label.json",
            f'{{"AccessKeyId": "{key_id}", "SecretAccessKey": "{secret}"}}',
            f'{{"AccessKeyId": "{key_id}", "SecretAccessKey": "{T}"}}',
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.label.json_escaped",
            f'"{{\\"SecretAccessKey\\": \\"{secret}\\"}}"',
            f'"{{\\"SecretAccessKey\\": \\"{T}\\"}}"',
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.label.python_dict",
            f"{{'aws_secret_access_key': '{secret}'}}",
            f"{{'aws_secret_access_key': '{T}'}}",
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.label.ini",
            f"[default]\naws_secret_access_key = {secret}\n",
            f"[default]\naws_secret_access_key = {T}\n",
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.label.export",
            f"export AWS_SECRET_ACCESS_KEY={secret}\n",
            f"export AWS_SECRET_ACCESS_KEY={T}\n",
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.label.yaml_list",
            f"env:\n  - SecretAccessKey: '{secret}'\n",
            f"env:\n  - SecretAccessKey: '{T}'\n",
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.label.properties",
            f"aws.secret.access.key={secret}",
            f"aws.secret.access.key={T}",
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.label.cli_space",
            f"tool --aws-secret-access-key {secret} --region us-east-1",
            f"tool --aws-secret-access-key {T} --region us-east-1",
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.label.cli_eq_quoted",
            f'tool --aws-secret-access-key="{secret}"',
            f'tool --aws-secret-access-key="{T}"',
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.label.inline_env",
            f"docker run -e AWS_SECRET_ACCESS_KEY={secret} img",
            f"docker run -e AWS_SECRET_ACCESS_KEY={T} img",
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.label.xml",
            f"<Credentials><SecretAccessKey>{secret}</SecretAccessKey></Credentials>",
            f"<Credentials><SecretAccessKey>{T}</SecretAccessKey></Credentials>",
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.label.slack_markup",
            f"*SecretAccessKey:* {secret}",
            f"*SecretAccessKey:* {T}",
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.session.json",
            f'{{"SessionToken": "{sess}"}}',
            f'{{"SessionToken": "{T}"}}',
            ["aws.session_token.label"],
        ),
        (
            "aws.session.env",
            f"AWS_SESSION_TOKEN={sess}==\nnext",
            f"AWS_SESSION_TOKEN={T}\nnext",
            ["aws.session_token.label"],
        ),
        (
            "aws.session.xml",
            f"<SessionToken>{sess}</SessionToken>",
            f"<SessionToken>{T}</SessionToken>",
            ["aws.session_token.label"],
        ),
        (
            "aws.label.diff_plus",
            f"diff\n+AWS_SECRET_ACCESS_KEY={secret}\n",
            f"diff\n+AWS_SECRET_ACCESS_KEY={T}\n",
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.label.diff_plus_export",
            f"@@ -1 +1 @@\n+export AWS_SECRET_ACCESS_KEY={secret}\n",
            f"@@ -1 +1 @@\n+export AWS_SECRET_ACCESS_KEY={T}\n",
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.label.diff_minus_export",
            f"-export AWS_SECRET_ACCESS_KEY={secret}\n",
            f"-export AWS_SECRET_ACCESS_KEY={T}\n",
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.label.diff_plus_json",
            f'+    "SecretAccessKey": "{secret}",\n',
            f'+    "SecretAccessKey": "{T}",\n',
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.label.diff_plus_yaml",
            f"+  aws_secret_access_key: {secret}\n",
            f"+  aws_secret_access_key: {T}\n",
            ["aws.secret_access_key.label"],
        ),
        (
            "aws.session.diff_plus",
            f"+AWS_SESSION_TOKEN={sess}\n",
            f"+AWS_SESSION_TOKEN={T}\n",
            ["aws.session_token.label"],
        ),
        ("jwe.short_tag", f"enc {jwe_short_tag()} end", f"enc {T} end", ["jwe"]),
        ("jwe.empty_tag", f"{jwe(tag_len=0)} end", f"{T} end", ["jwe"]),
        (
            "url.token_in_path_still_redacted",
            f"https://example.test/hook/{github_classic(seed=21)}/x",
            f"https://example.test/hook/{T}/x",
            ["github.classic"],
        ),
        (
            "pem.contains_token",
            f"{PEM_BEGIN}\n{github_classic(seed=22)}\n{PEM_END}",
            T,
            ["pem.private_key"],
        ),
    ]
    out = []
    for name, text, expected, rules in cases:
        if expected is None:
            expected = text.replace(link_token(), T)
        out.append((name, text, expected, rules))
    return out


# (name, text): must come back unchanged, with no Secret span.
def benign_cases() -> list[tuple[str, str]]:
    key_id = aws_key_id()
    secret = aws_secret()
    return [
        ("bolin.password_prose", "Enter your password: see the onboarding docs."),
        ("bolin.api_token_placeholder", "export API_TOKEN=<your-token-here>"),
        ("bolin.password_changeme", "password=changeme"),
        ("github.bad_checksum", "ghp_" + synth(30, ALNUM, 1) + "AAAAAA"),
        ("github.glued", "xghp_" + github_classic()[4:]),
        ("github.too_long", github_classic() + "Z"),
        (
            "github.fine_grained.short",
            "github_pat_" + synth(22, ALNUM, 2) + "_" + synth(50, ALNUM, 3),
        ),
        ("slack.short", "xoxb-12345"),
        ("stripe.short", "sk_live_abc123"),
        ("stripe.webhook.wrong_length", "whsec_" + synth(40, ALNUM, 9)),
        ("anthropic.bad_kind", "sk-ant-foo03-" + synth(90, B64URL, 10)),
        ("google.short", "AIza" + synth(34, B64URL, 12)),
        ("sendgrid.bad_shape", "SG." + synth(20, B64URL, 15) + "." + synth(43, B64URL, 16)),
        ("vault.short", "hvs.shortvalue"),
        ("shopify.short", "shpat_" + synth(31, HEX, 18)),
        ("digitalocean.upper", "dop_v1_" + synth(64, HEX, 19).upper()),
        ("jwt.alg_none", jwt({"alg": "none"})),
        (
            "jwe.header_without_alg",
            b64url_json({"enc": "A256GCM"})
            + ".."
            + synth(16, B64URL, 1)
            + "."
            + synth(24, B64URL, 2)
            + ".tag",
        ),
        (
            "jwt.header_not_json",
            "eyJ" + synth(30, ALNUM, 3) + "." + "eyJ" + synth(30, ALNUM, 4) + ".sig",
        ),
        ("link.no_claims", link_token(claims=False)),
        ("aws.key_id_alone", f"AccessKeyId: {key_id}"),
        ("aws.secret_no_key_id", f"value {secret} here"),
        ("aws.secret_far", f"{key_id}" + " pad" * 100 + f" {secret}"),
        ("aws.example_key", "AKIAIOSFODNN7EXAMPLE wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
        ("aws.label.prose", "rotate the SecretAccessKey before Friday"),
        ("aws.label.prose_colon", "SecretAccessKey: see the docs"),
        ("aws.label.short", "aws_secret_access_key=abc"),
        ("aws.label.example", "aws_secret_access_key=example"),
        ("aws.label.key_id_value", f"aws_secret_access_key={key_id}"),
        ("aws.label.39", f"aws_secret_access_key={secret[:39]}"),
        ("aws.label.glued_name", f"MyAppSecretAccessKey={secret}"),
        ("aws.label.template", "aws_secret_access_key=${AWS_SECRET}"),
        ("aws.label.env_ref", "SecretAccessKey: $AWS_SECRET_ACCESS_KEY"),
        ("aws.session.short", "aws_session_token=" + synth(60, B64, 4)),
        ("cert", f"{CERT_BEGIN}\n{PEM_BODY}\n{CERT_END}"),
        ("pem.prose", "the header line starts with -----BEGIN and names a type"),
        ("arn", "arn:aws:iam::123456789012:role/Deploy-Role"),
        (
            "oauth.public_params",
            "https://auth.example.test/authorize?client_id=abc123def456&state="
            + synth(43, B64URL, 3)
            + "&code_challenge="
            + synth(43, B64URL, 4)
            + "&scope=openid",
        ),
        ("commit.permalink", "https://github.com/org/repo/commit/" + synth(40, HEX, 6)),
        ("sha.prose", "fixed in " + synth(40, HEX, 7) + " yesterday"),
        ("twilio.sid", "AC" + synth(32, HEX, 8)),
        ("plain", "nothing to see here, just words and numbers 12345."),
        (
            "aws.pair_inside_url_path",
            f"{key_id} see https://docs.example.test/a-{secret}-b/page",
        ),
        (
            "aws.pair_inside_oauth_param",
            f"{key_id} https://auth.example.test/cb?state={secret}&x=1",
        ),
        # Git object names (40 or 64 lower-case hex) beside a key ID stay visible.
        ("git.sha1_beside_key_id", f"{key_id} at commit {synth(40, HEX, 5)} today"),
        ("git.sha1_right_after_key_id", f"{key_id} {synth(40, HEX, 5)}"),
        ("git.sha256_beside_key_id", f"{key_id}\nobject {synth(64, HEX, 9)}\n"),
        # Exact OAuth keys only: other parameters and the key outside a URL stay.
        (
            "url.oauth.other_params_stay",
            "https://auth.example.test/cb?code="
            + synth(30, B64URL, 40)
            + "&state="
            + synth(30, B64URL, 41)
            + "&token="
            + synth(30, B64URL, 42)
            + "&client_id=app1&secret_hint=x",
        ),
        ("url.oauth.key_in_prose", "set access_token=" + synth(30, B64URL, 43) + " in the env"),
        (
            "url.oauth.key_name_glued",
            "https://h.example.test/x?my_access_token=abc&access_tokens=d",
        ),
        ("url.oauth.empty_value", "https://h.example.test/x?client_secret=&password="),
    ]
