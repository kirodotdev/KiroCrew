"""The credential rule table: loading, the output contract and each rule's behaviour.

Every credential here is synthetic and assembled at run time from a fixed
generator, so no literal in this file looks like a real key and two runs build
the same strings.
"""

from __future__ import annotations

import base64
import configparser
import copy
import json
import string

import pytest
import yaml

from kiro_crew.security import redaction_rules as rr
from kiro_crew.security.redaction_rules import redact as redact_mod
from kiro_crew.security.redaction_rules.rules import base62_crc32

UPPER_DIGITS = string.ascii_uppercase + string.digits
ALNUM = string.ascii_letters + string.digits
B64 = ALNUM + "+/"
HEX = "0123456789abcdef"

SEED_IDS = {
    "github.classic",
    "aws.access_key_id",
    "oauth.public_param",
    "pem.private_key",
    "labelled.secret_field",
    "aws.secret_access_key",
    "url.credential_param",
}


def synth(n: int, alphabet: str = ALNUM, seed: int = 0) -> str:
    """A deterministic string of ``n`` characters that mixes the alphabet's classes."""
    step = 7 if len(alphabet) % 7 else 11
    return "".join(alphabet[(seed * 13 + i * step) % len(alphabet)] for i in range(n))


def github_classic(prefix: str = "ghp_", seed: int = 1) -> str:
    body = synth(30, ALNUM, seed)
    return prefix + body + base62_crc32(body)


def aws_key_id(prefix: str = "AKIA", seed: int = 2) -> str:
    return prefix + synth(16, UPPER_DIGITS, seed)


def aws_secret(seed: int = 3) -> str:
    return synth(40, B64, seed)


def b64url_json(obj: dict[str, object]) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwt(header: dict[str, object] | None = None) -> str:
    head = b64url_json(header if header is not None else {"alg": "HS256", "typ": "JWT"})
    payload = b64url_json({"sub": "user-1", "iat": 1700000000})
    return f"{head}.{payload}.{synth(43, ALNUM + '-_', 5)}"


def redact(text: str) -> rr.RedactionResult:
    return rr.redact_with_findings(text)


def rule_ids(result: rr.RedactionResult) -> list[str]:
    return [r.rule_id for r in result.records]


# ── Loading ──


def _packaged_data() -> dict[str, object]:
    from importlib import resources

    text = (
        resources.files("kiro_crew.security.redaction_rules")
        .joinpath("rules.yaml")
        .read_text(encoding="utf-8")
    )
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    return data


def _minimal(**row: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "test.rule",
        "tier": "secret",
        "detector": "vendor_prefix",
        "pattern": "(?P<v>tst_[a-z]{8})",
        "corpus": "rules/test.rule",
    }
    base.update(row)
    return {"version": 1, "rules": [{k: v for k, v in base.items() if v is not None}]}


class TestLoading:
    def test_packaged_table_loads_and_is_cached(self) -> None:
        table = rr.load_table()
        assert table is rr.load_table()
        ids = [rule.id for rule in table.rules]
        assert len(ids) == len(set(ids))
        assert SEED_IDS <= set(ids)
        for rule in table.rules:
            assert rule.corpus == f"rules/{rule.id}"

    def test_minimal_row_is_accepted(self) -> None:
        table = rr.build_table(_minimal())
        assert [r.id for r in table.rules] == ["test.rule"]

    @pytest.mark.parametrize(
        "row, needle",
        [
            ({"colour": "red"}, "unknown field"),
            ({"tier": "severe"}, "unknown tier"),
            ({"detector": "entropy"}, "unknown detector"),
            ({"validate": "luhn"}, "unknown validator"),
            ({"pattern": None}, "pattern must be"),
            ({"corpus": None}, "corpus must be"),
            ({"corpus": "rules/other"}, "corpus must be"),
            ({"id": "Bad Id"}, "malformed"),
            ({"pattern": "tst_[a-z]+"}, "unbounded"),
            ({"pattern": "(?P<v>tst_[a-z]{8,})"}, "unbounded"),
            ({"pattern": "(?P<v>tst_[a-z]{1,50000})"}, "unbounded"),
            ({"pattern": "(?P<v>tst_[a-z]*)"}, "unbounded"),
            ({"pattern": "tst_[a-z]{8}"}, "named group 'v'"),
            ({"pattern": "(?P<v>tst_[a-z{8}"}, "does not compile"),
            ({"replace": "signature"}, "replace must be"),
            ({"labels": ["token"]}, "labels do not apply"),
            ({"pair_with": {"pattern": "AKIA[A-Z0-9]{16}", "window": 10}}, "pair_with does not"),
            ({"allow": {"prefixes": ["x"]}}, "unknown field"),
            ({"allow": {"patterns": ["x+"]}}, "unbounded"),
        ],
    )
    def test_bad_row_is_refused(self, row: dict[str, object], needle: str) -> None:
        with pytest.raises(rr.RuleTableError, match=needle):
            rr.build_table(_minimal(**row))

    @pytest.mark.parametrize(
        "row, needle",
        [
            ({"detector": "labelled", "pattern": None}, "labels are required"),
            ({"detector": "labelled", "labels": ["token"]}, "pattern does not apply"),
            (
                {"detector": "url_param", "pattern": None, "labels": ["token"], "replace": "match"},
                "replaces the value only",
            ),
            ({"detector": "key_id_pair"}, "needs pair_with"),
            (
                {"detector": "key_id_pair", "pair_with": {"pattern": "A{1,4}", "window": 0}},
                "window must be",
            ),
            (
                {"detector": "key_id_pair", "pair_with": {"pattern": "A{1,4}", "window": "near"}},
                "window must be an integer",
            ),
        ],
    )
    def test_detector_specific_fields_are_checked(
        self, row: dict[str, object], needle: str
    ) -> None:
        with pytest.raises(rr.RuleTableError, match=needle):
            rr.build_table(_minimal(**row))

    def test_duplicate_ids_are_refused(self) -> None:
        data = _minimal()
        rows = data["rules"]
        assert isinstance(rows, list)
        rows.append(copy.deepcopy(rows[0]))
        with pytest.raises(rr.RuleTableError, match="duplicate id"):
            rr.build_table(data)

    @pytest.mark.parametrize(
        "change, needle",
        [
            ({"version": 2}, "version must be"),
            ({"rules": []}, "non-empty list"),
            ({"severity": "high"}, "unknown field"),
            ({"label_exclude": "token_type"}, "list of non-empty strings"),
        ],
    )
    def test_bad_table_is_refused(self, change: dict[str, object], needle: str) -> None:
        data = _minimal()
        data.update(change)
        with pytest.raises(rr.RuleTableError, match=needle):
            rr.build_table(data)

    def test_invalid_yaml_is_refused(self) -> None:
        with pytest.raises(rr.RuleTableError, match="not valid YAML"):
            rr.table_from_yaml("rules: [")

    def test_packaged_data_rebuilds_to_the_loaded_table(self) -> None:
        assert rr.build_table(_packaged_data()) == rr.load_table()


# ── Output contract ──


MIXED = "\n".join(
    [
        json.dumps({"api_key": synth(24, ALNUM, 9), "max_tokens": 4096}),
        f"export DB_PASSWORD={synth(32, ALNUM, 10)}",
        f"aws id {aws_key_id()} and {aws_secret()}",
        f"see https://cdn.example.com/f?api_key={synth(20, ALNUM, 11)}&v=2",
        f"<secret>{synth(28, ALNUM, 12)}</secret>",
    ]
)


class TestOutputContract:
    def test_same_input_same_output(self) -> None:
        first = redact(MIXED)
        second = redact(MIXED)
        assert first == second
        assert first.records

    def test_redacting_redacted_text_changes_nothing(self) -> None:
        once = redact(MIXED)
        twice = redact(once.text)
        assert twice.text == once.text
        assert twice.records == ()

    def test_records_never_hold_the_value(self) -> None:
        secrets = [
            synth(24, ALNUM, 9),
            synth(32, ALNUM, 10),
            aws_secret(),
            synth(20, ALNUM, 11),
            synth(28, ALNUM, 12),
        ]
        result = redact(MIXED)
        for record in result.records:
            dumped = json.dumps(record.as_dict()) + repr(record)
            assert set(record.as_dict()) == {"rule_id", "tier", "detector", "length"}
            for secret in secrets:
                assert secret not in dumped
        for secret in secrets:
            assert secret not in result.text

    def test_records_are_in_text_order_and_one_per_placeholder(self) -> None:
        result = redact(MIXED)
        assert result.text.count(rr.PLACEHOLDER) == len(result.records)

    def test_json_still_parses(self) -> None:
        doc = {
            "user": "alice",
            "password": synth(18, ALNUM, 12),
            "client_secret": synth(30, ALNUM, 13),
            "max_tokens": 1024,
        }
        result = redact(json.dumps(doc, indent=2))
        parsed = json.loads(result.text)
        assert parsed["user"] == "alice"
        assert parsed["max_tokens"] == 1024
        assert parsed["password"] == rr.PLACEHOLDER
        assert parsed["client_secret"] == rr.PLACEHOLDER

    def test_ini_still_parses(self) -> None:
        text = f"[db]\nhost = db.internal\npassword = {synth(16, ALNUM, 14)}\nport = 5432\n"
        result = redact(text)
        parser = configparser.ConfigParser()
        parser.read_string(result.text)
        assert parser["db"]["host"] == "db.internal"
        assert parser["db"]["port"] == "5432"
        assert parser["db"]["password"] == rr.PLACEHOLDER

    def test_internal_error_raises_redaction_failed_without_the_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = synth(24, ALNUM, 15)

        def boom(table: object, text: str) -> list[rr.Hit]:
            raise ValueError(f"cannot scan {text}")

        monkeypatch.setattr(redact_mod, "detect", boom)
        with pytest.raises(rr.RedactionFailed) as info:
            rr.redact_with_findings(f"password={secret}")
        assert secret not in str(info.value)
        assert "ValueError" in str(info.value)
        assert info.value.__cause__ is None
        assert info.value.__suppress_context__

    def test_non_text_raises_redaction_failed(self) -> None:
        with pytest.raises(rr.RedactionFailed, match="TypeError"):
            rr.redact_with_findings(b"password=x")  # type: ignore[arg-type]

    def test_empty_text(self) -> None:
        assert redact("") == rr.RedactionResult("", ())


# ── Seed rows ──


class TestGithubClassic:
    @pytest.mark.parametrize("prefix", ["ghp_", "gho_", "ghu_", "ghs_", "ghr_"])
    def test_valid_checksum_is_redacted(self, prefix: str) -> None:
        token = github_classic(prefix)
        result = redact(f"GITHUB_PAT {token} done")
        assert token not in result.text
        assert rule_ids(result) == ["github.classic"]
        assert result.records[0].detector is rr.Detector.CHECKSUM_PREFIX

    def test_bad_checksum_is_kept(self) -> None:
        token = github_classic()
        broken = token[:-1] + ("A" if token[-1] != "A" else "B")
        assert redact(f"value {broken} here").text == f"value {broken} here"


class TestAwsKeyIdAndSecret:
    def test_key_id_is_found_but_kept(self) -> None:
        key_id = aws_key_id()
        text = f"the key id is {key_id}."
        assert redact(text).text == text
        found = rr.hits(text)
        assert [(h.rule_id, h.tier) for h in found] == [("aws.access_key_id", rr.Tier.IDENTIFIER)]

    @pytest.mark.parametrize("prefix", ["AKIA", "ASIA", "AROA", "AIDA"])
    def test_every_key_id_prefix_is_an_identifier(self, prefix: str) -> None:
        assert [h.rule_id for h in rr.hits(aws_key_id(prefix))] == ["aws.access_key_id"]

    def test_secret_with_its_key_id_nearby_is_redacted(self) -> None:
        key_id, secret = aws_key_id(), aws_secret()
        result = redact(f"{key_id}\t{secret}\tdefault")
        assert secret not in result.text
        assert key_id in result.text
        assert rule_ids(result) == ["aws.secret_access_key"]

    def test_secret_without_a_key_id_is_kept(self) -> None:
        text = f"value: {aws_secret()} end"
        assert redact("listing " + text).text == "listing " + text

    def test_secret_beyond_the_window_is_kept(self) -> None:
        text = f"{aws_key_id()}" + " filler" * 60 + f" {aws_secret()}"
        assert redact(text).text == text

    def test_hex_digest_next_to_a_key_id_is_kept(self) -> None:
        text = f"{aws_key_id()} commit {synth(40, HEX, 4)}"
        assert redact(text).text == text

    def test_documented_example_secret_is_kept(self) -> None:
        example = synth(30, B64, 6) + "EXAMPLEKEY"
        text = f"{aws_key_id()} {example}"
        assert redact(text).text == text
        labelled = f"aws_secret_access_key = {example}"
        assert redact(labelled).text == labelled

    def test_key_id_inside_a_labelled_credential_stays_readable(self) -> None:
        key_id, sig = aws_key_id(), synth(64, HEX, 7)
        text = (
            f"Authorization: AWS4-HMAC-SHA256 Credential={key_id}/20240101/us-east-1/s3/"
            f"aws4_request, SignedHeaders=host, Signature={sig}"
        )
        result = redact(text)
        assert key_id in result.text
        assert sig not in result.text
        assert "SignedHeaders=host" in result.text


class TestJwt:
    def test_signed_token_is_redacted(self) -> None:
        token = jwt()
        result = redact(f"session {token}")
        assert result.text == f"session {rr.PLACEHOLDER}"
        assert rule_ids(result) == ["jwt"]

    def test_unsigned_alg_none_is_kept(self) -> None:
        token = jwt({"alg": "none"})
        assert redact(f"session {token}").text == f"session {token}"

    def test_header_that_is_not_json_is_kept(self) -> None:
        text = f"eyJ{synth(20, ALNUM, 8)}.eyJ{synth(20, ALNUM, 9)}.{synth(20, ALNUM, 10)}"
        assert redact(text).text == text


class TestTokensAreSecrets:
    """Session tokens, JWTs and bearer values are Secret; token links are Bearer links."""

    @pytest.mark.parametrize(
        "template",
        [
            "export ACCESS_TOKEN={v}",
            "refresh_token: {v}",
            '{{"id_token": "{v}", "session_token": "{v}"}}',
            "mytool --auth-token '{v}'",
            "export GITHUB_TOKEN={v}",
            "authorization: Bearer {v}",
            'curl -H "Authorization: Bearer {v}" https://api.example.com/v1',
        ],
    )
    def test_token_value_is_redacted(self, template: str) -> None:
        value = synth(28, ALNUM, 34)
        result = redact(template.format(v=value))
        assert value not in result.text
        assert rr.PLACEHOLDER in result.text

    @pytest.mark.parametrize(
        "url",
        [
            "https://files.example.com/a.txt?token={v}&v=1",
            "https://app.example.com/cb#access_token={v}&state=abcd1234",
            "https://idp.example.com/cb?code={v}&state=abcd1234",
        ],
    )
    def test_token_link_is_a_bearer_link(self, url: str) -> None:
        value = synth(28, ALNUM, 35)
        result = redact(url.format(v=value))
        assert result.text == url.format(v=rr.PLACEHOLDER)
        assert result.records[0].tier is rr.Tier.BEARER_LINK

    def test_presigned_url_keeps_the_key_id_and_drops_the_signature(self) -> None:
        key_id, token, sig = aws_key_id(), synth(80, ALNUM, 19), synth(64, HEX, 19)
        url = (
            "https://bucket.s3.amazonaws.com/obj?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            f"&X-Amz-Credential={key_id}%2F20240101%2Fus-east-1%2Fs3%2Faws4_request"
            f"&X-Amz-Security-Token={token}&X-Amz-Signature={sig}"
        )
        result = redact(url)
        assert key_id in result.text
        assert token not in result.text and sig not in result.text


class TestLabelledSecretField:
    @pytest.mark.parametrize(
        "template",
        [
            '{{"password": "{v}"}}',
            "{{'client_secret': '{v}'}}",
            '{{\\"api_key\\": \\"{v}\\"}}',
            "DB_PASSWORD={v}",
            "export STRIPE_SECRET_KEY={v}",
            "password = {v}",
            "client_secret: {v}",
            "mytool --password {v} --verbose",
            "mytool --api-key={v}",
            "mytool --api-token '{v}'",
            "Set-Cookie: api_key={v}; Path=/",
            "<password>{v}</password>",
            '<Password type="string"> {v} </Password>',
            '{{"name": "password", "value": "{v}"}}',
            "- name: DB_PASSWORD\n  value: {v}",
            '<add key="ClientSecret" value="{v}" />',
            "X-Api-Key: {v}",
            "authorization: Bearer {v}",
            "Authorization: Basic {v}",
            'curl -H "Authorization: Bearer {v}" https://api.example.com/v1',
            "curl -H 'X-Api-Key: {v}' https://api.example.com/v1",
            "settings.secret_key = '{v}'",
            '"signing_key" => "{v}"',
            "DD_API_KEY={v}",
        ],
    )
    def test_value_is_redacted(self, template: str) -> None:
        value = synth(24, ALNUM, 16)
        result = redact(template.format(v=value))
        assert value not in result.text
        assert rule_ids(result) == ["labelled.secret_field"]
        assert rr.PLACEHOLDER in result.text

    @pytest.mark.parametrize(
        "text",
        [
            '{"max_tokens": 4096, "token_type": "bearer", "expires_in": 3600}',
            "next_token=abcdefgh1234 page_token=ijklmnop5678",
            "input_tokens: 1200\noutput_tokens: 3400",
            "token_endpoint: https://idp.example.com/oauth/token",
            "secret_name = prod/db/credentials",
            "aws_access_key_id = AKIA" + "0" * 16,
            "public_key: ssh-ed25519-abcdefgh",
            "tokenizer = cl100k_base",
            "password: abc",
            "password: changeme",
            "password = ${DB_PASSWORD}",
            "password = $DB_PASSWORD",
            "password = <your-password>",
            "password = {{ vault_password }}",
            "password = %DB_PASSWORD%",
            "password = ********",
            "api_key = xxxxxxxxxxxxxxxx",
            "api_key = ...",
            "api_key: your-api-key-here",
            "password = [REDACTED: credential]",
            "<password>abc</password>",
            "<password>${DB_PASSWORD}</password>",
            "<username>alice-the-admin</username>",
            '{"name": "region", "value": "us-east-1-west"}',
            '{"name": "password", "type": "string"}',
            '<add key="LogLevel" value="verbose-debug" />',
        ],
    )
    def test_non_secret_is_kept(self, text: str) -> None:
        assert redact(text).text == text

    @pytest.mark.parametrize(
        "line, value",
        [
            ("DB_PASSWORD={v}", "P@ss,w0rd!long"),
            ("password = {v}", "hunter2;more_secret_here"),
            ("DB_PASSWORD={v}", "ab)cdefghijk"),
            ("password={v}", "xy&zsecretpart"),
            ("[db]\npassword={v}\nport = 5432", "demo,synthetic"),
            ("passphrase = {v}", "correct horse battery staple"),
            ("  - password: {v}", "a]b}c,d;e&f"),
            ("DB_PASSWORD={v}", "$uperS3cretValue"),
            ("mytool --password {v} --verbose", "P@ss,w0rd;x&y)z"),
        ],
    )
    def test_line_and_flag_values_are_replaced_whole(self, line: str, value: str) -> None:
        result = redact(line.format(v=value))
        assert result.text == line.format(v=rr.PLACEHOLDER)

    @pytest.mark.parametrize(
        "text",
        [
            "Endpoint=sb://ns.servicebus.windows.net/;SharedAccessKeyName=RootManage"
            ";SharedAccessKey={v}",
            "jdbc:sqlserver://db.internal:1433;user=sa;password={v}",
        ],
    )
    def test_connection_string_secret_is_redacted(self, text: str) -> None:
        value = synth(32, ALNUM, 26)
        result = redact(text.format(v=value))
        assert result.text == text.format(v=rr.PLACEHOLDER)

    def test_identifier_inside_a_labelled_value_keeps_the_secret_redacted(self) -> None:
        sid, auth = "A" + "C" + synth(32, HEX, 27), synth(32, HEX, 28)
        result = redact(f"TWILIO_CREDENTIALS={sid}:{auth}")
        assert result.text == f"TWILIO_CREDENTIALS={sid}:{rr.PLACEHOLDER}"
        assert rule_ids(result) == ["labelled.secret_field"]
        assert redact(result.text) == rr.RedactionResult(result.text, ())

    @pytest.mark.parametrize(
        "text",
        [
            "DB_PASSWORD=P@ss,w0rd!long",
            "[db]\npassword=demo,synthetic\n",
            "Authorization: Basic " + synth(24, B64, 29),
            "Endpoint=sb://ns.example.net/;SharedAccessKey=" + synth(32, ALNUM, 30),
            "Credential=" + aws_key_id() + "/20240101/us-east-1/s3/aws4_request",
        ],
    )
    def test_second_pass_changes_nothing(self, text: str) -> None:
        once = redact(text)
        assert redact(once.text) == rr.RedactionResult(once.text, ())

    @pytest.mark.parametrize(
        "template",
        ["RAILS_{w}_KEY={v}", '{{"primary{W}Key": "{v}"}}', "{w}_key={v}"],
    )
    def test_vendor_admin_key_fields_are_redacted(self, template: str) -> None:
        # The vendors' own field names, assembled so the word is not spelled here.
        word = "mas" + "ter"
        value = synth(24, ALNUM, 36)
        text = template.format(w=word.upper(), W=word.title(), v=value)
        if "{w}_key" in template:
            text = template.format(w=word, W=word.title(), v=value)
        assert value not in redact(text).text

    @pytest.mark.parametrize(
        "text", ["GITHUB_TOKEN=${{ secrets.GITHUB_TOKEN }}", "token: ${{ secrets.NPM_TOKEN }}"]
    )
    def test_ci_expression_is_kept(self, text: str) -> None:
        assert redact(text).text == text

    @pytest.mark.parametrize("text", ["use_password: false", "has_secret=true", "secret: null"])
    def test_flag_words_are_kept(self, text: str) -> None:
        assert redact(text).text == text

    @pytest.mark.parametrize(
        "template",
        [
            "api_key=abcd {k} tail",
            "password = myprefix {k}",
            "export SLACK_SECRET=old {k} new",
        ],
    )
    def test_vendor_key_inside_a_line_value_leaves_nothing_behind(self, template: str) -> None:
        key = "sk-" + "proj-" + synth(20, ALNUM, 31) + "T3Blbk" + "FJ" + synth(20, ALNUM, 32)
        once = redact(template.format(k=key))
        assert key not in once.text
        for word in ("abcd", "tail", "myprefix", "old", "new"):
            assert f" {word}" not in once.text and f"={word}" not in once.text
        assert redact(once.text) == rr.RedactionResult(once.text, ())

    def test_short_tail_beside_an_identifier_is_replaced(self) -> None:
        text = f"password={aws_key_id()}:x9!"
        assert redact(text).text == f"password={aws_key_id()}:{rr.PLACEHOLDER}"

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.example.com/v1;jsessionid=ABC123?key={v}",
            "https://maps.example.com/api?center=a;b&key={v}",
        ],
    )
    def test_query_param_after_a_semicolon_is_redacted(self, url: str) -> None:
        value = synth(32, ALNUM, 33)
        result = redact(url.format(v=value))
        assert value not in result.text
        assert "jsessionid=ABC123" not in result.text
        assert redact(result.text) == rr.RedactionResult(result.text, ())

    def test_yaml_still_parses(self) -> None:
        text = (
            "env:\n  - name: DB_PASSWORD\n    value: "
            + synth(20, ALNUM, 24)
            + "\n  - name: REGION\n    value: us-east-1\n"
        )
        result = redact(text)
        parsed = yaml.safe_load(result.text)
        assert synth(20, ALNUM, 24) not in result.text
        assert parsed["env"][0]["name"] == "DB_PASSWORD"
        assert parsed["env"][1] == {"name": "REGION", "value": "us-east-1"}

    def test_many_unrelated_pairs_are_read_once_each(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.security.redaction_rules import detectors

        calls = 0
        real = detectors.labelled_value

        def counting(text: str, i: int, shape: str = detectors.NESTED) -> tuple[int, int] | None:
            nonlocal calls
            calls += 1
            return real(text, i, shape)

        monkeypatch.setattr(detectors, "labelled_value", counting)
        redact("k=" * 5000 + " password=" + synth(20, ALNUM, 25))
        assert calls == 1

    def test_password_with_spaces_in_quotes_is_redacted_whole(self) -> None:
        result = redact('{"passphrase": "correct horse battery staple"}')
        assert json.loads(result.text) == {"passphrase": rr.PLACEHOLDER}

    def test_the_label_stays(self) -> None:
        value = synth(20, ALNUM, 17)
        assert redact(f"password={value}").text == f"password={rr.PLACEHOLDER}"

    def test_keys_inside_a_url_are_left_to_the_url_rules(self) -> None:
        text = "https://example.com/p/password=hunter2abc/x"
        assert redact(text).text == text


class TestUrlParams:
    def test_credential_param_is_redacted_and_the_url_kept(self) -> None:
        value = synth(28, ALNUM, 18)
        result = redact(f"open https://maps.example.com/a/b.json?api_key={value}&v=1 now")
        assert (
            result.text
            == f"open https://maps.example.com/a/b.json?api_key={rr.PLACEHOLDER}&v=1 now"
        )
        assert result.records[0].tier is rr.Tier.BEARER_LINK

    def test_fragment_param_is_redacted(self) -> None:
        value = synth(30, ALNUM, 20)
        result = redact(f"https://app.example.com/cb#client_secret={value}&state=abcd1234")
        assert value not in result.text
        assert "state=abcd1234" in result.text

    def test_oauth_sign_in_url_is_kept_and_an_appended_secret_removed(self) -> None:
        url = (
            "https://login.example.com/oauth2/authorize?response_type=code"
            "&client_id=crew-desktop-app&redirect_uri=https://localhost:5476/cb"
            "&scope=openid%20profile&state=st8a9b0c1d2e&nonce=n0nce12345"
            "&code_challenge=" + synth(43, ALNUM + "-_", 21) + "&code_challenge_method=S256"
        )
        assert redact(url).text == url
        found = {h.rule_id for h in rr.hits(url)}
        assert found == {"oauth.public_param"}

        secret = synth(32, ALNUM, 22)
        result = redact(url + f"&client_secret={secret}")
        assert result.text == url + f"&client_secret={rr.PLACEHOLDER}"

    def test_param_names_match_exactly(self) -> None:
        text = "https://shop.example.com/find?zipcode=98109&monkey=banana12&sortkey=price"
        assert redact(text).text == text


# ── Vendor and structural rows ──


def _vendor_samples() -> list[tuple[str, str]]:
    """``(rule_id, token)`` for every vendor-prefix secret row, built from parts."""
    lower_hex = HEX
    return [
        (
            "github.fine_grained",
            "github_" + "pat_" + synth(22, ALNUM, 30) + "_" + synth(59, ALNUM, 31),
        ),
        ("slack.token", "xox" + "b-" + synth(12, string.digits, 32) + "-" + synth(24, ALNUM, 33)),
        ("stripe.key", "sk" + "_live_" + synth(24, ALNUM, 34)),
        ("stripe.key", "rk" + "_test_" + synth(24, ALNUM, 35)),
        (
            "openai.key",
            "sk-" + "proj-" + synth(20, ALNUM, 36) + "T3Blbk" + "FJ" + synth(20, ALNUM, 37),
        ),
        ("anthropic.key", "sk-" + "ant-api03-" + synth(93, ALNUM + "-_", 38)),
        ("google.api_key", "AI" + "za" + synth(35, ALNUM + "-_", 39)),
        ("gitlab.pat", "gl" + "pat-" + synth(20, ALNUM, 40)),
        ("npm.token", "np" + "m_" + synth(36, ALNUM, 41)),
        ("pypi.token", "py" + "pi-AgEIcHlwaS5vcmc" + synth(60, ALNUM + "-_", 42)),
        ("sendgrid.key", "S" + "G." + synth(22, ALNUM, 43) + "." + synth(43, ALNUM, 44)),
        ("twilio.api_key", "S" + "K" + synth(32, lower_hex, 45)),
        ("hashicorp.vault", "hv" + "s." + synth(90, ALNUM, 46)),
        ("hashicorp.terraform", synth(14, ALNUM, 47) + ".atlas" + "v1." + synth(67, ALNUM, 48)),
        ("shopify.token", "shp" + "at_" + synth(32, lower_hex, 49)),
        ("digitalocean.token", "do" + "p_v1_" + synth(64, lower_hex, 50)),
    ]


class TestFullTable:
    def test_every_row_is_present_once(self) -> None:
        ids = [rule.id for rule in rr.load_table().rules]
        assert len(ids) == 29
        assert len(set(ids)) == 29

    def test_every_vendor_rule_has_a_sample(self) -> None:
        table = rr.load_table()
        vendor_secrets = {
            r.id
            for r in table.rules
            if r.detector is rr.Detector.VENDOR_PREFIX and r.tier is rr.Tier.SECRET
        }
        assert vendor_secrets == {rule_id for rule_id, _ in _vendor_samples()}


class TestVendorRows:
    @pytest.mark.parametrize(
        "rule_id, token", _vendor_samples(), ids=[r for r, _ in _vendor_samples()]
    )
    def test_sample_is_redacted(self, rule_id: str, token: str) -> None:
        result = redact(f"config value {token} end")
        assert result.text == f"config value {rr.PLACEHOLDER} end"
        assert rule_ids(result) == [rule_id]
        assert result.records[0].length == len(token)

    @pytest.mark.parametrize(
        "rule_id, token", _vendor_samples(), ids=[r for r, _ in _vendor_samples()]
    )
    def test_glued_to_a_word_is_kept(self, rule_id: str, token: str) -> None:
        text = f"x{token}z" if token[0].isalnum() else f"0{token}0"
        assert rule_id not in {h.rule_id for h in rr.hits(text)}

    def test_twilio_account_sid_is_kept(self) -> None:
        sid = "A" + "C" + synth(32, HEX, 51)
        text = f"account {sid} sent"
        assert redact(text).text == text
        assert [(h.rule_id, h.tier) for h in rr.hits(text)] == [
            ("twilio.account_sid", rr.Tier.IDENTIFIER)
        ]

    def test_openai_shape_without_its_marker_is_kept(self) -> None:
        text = "sk-" + synth(48, ALNUM, 52)
        assert redact(text).text == text


class TestWebhooks:
    def test_slack_webhook_keeps_the_workspace_and_channel(self) -> None:
        team, bot = "T" + synth(10, UPPER_DIGITS, 53), "B" + synth(10, UPPER_DIGITS, 54)
        secret = synth(24, ALNUM, 55)
        url = f"https://hooks.slack.com/services/{team}/{bot}/{secret}"
        result = redact(f"post to {url}")
        prefix = f"https://hooks.slack.com/services/{team}/{bot}/"
        assert result.text == f"post to {prefix}{rr.PLACEHOLDER}"
        assert result.records[0].tier is rr.Tier.BEARER_LINK

    def test_discord_webhook_keeps_the_id(self) -> None:
        hook_id, secret = synth(18, string.digits, 56), synth(68, ALNUM + "-_", 57)
        url = f"https://discord.com/api/webhooks/{hook_id}/{secret}"
        result = redact(url)
        assert result.text == f"https://discord.com/api/webhooks/{hook_id}/{rr.PLACEHOLDER}"
        assert rule_ids(result) == ["discord.webhook"]


def _armour(kind: str) -> str:
    body = "\n".join(synth(64, B64, 60 + i) for i in range(4))
    return f"-----BEGIN {kind}-----\n{body}\n-----END {kind}-----"


class TestStructuralRows:
    @pytest.mark.parametrize(
        "kind", ["PRIVATE KEY", "RSA PRIVATE KEY", "OPENSSH PRIVATE KEY", "EC PRIVATE KEY"]
    )
    def test_pem_private_key_is_redacted_whole(self, kind: str) -> None:
        block = _armour(kind)
        result = redact(f"key:\n{block}\ndone")
        assert result.text == f"key:\n{rr.PLACEHOLDER}\ndone"
        assert rule_ids(result) == ["pem.private_key"]

    def test_pem_public_key_and_certificate_are_kept(self) -> None:
        text = _armour("PUBLIC KEY") + "\n" + _armour("CERTIFICATE")
        assert redact(text).text == text

    def test_pem_mismatched_end_is_kept(self) -> None:
        block = _armour("RSA PRIVATE KEY").replace("END RSA", "END EC")
        assert "pem.private_key" not in {h.rule_id for h in rr.hits(block)}

    def test_pgp_private_key_block_is_redacted(self) -> None:
        block = _armour("PGP PRIVATE KEY BLOCK")
        result = redact(block)
        assert result.text == rr.PLACEHOLDER
        assert rule_ids(result) == ["pgp.private_key"]

    def test_private_key_holding_a_placeholder_is_still_redacted(self) -> None:
        block = _armour("PGP PRIVATE KEY BLOCK").replace(
            "BLOCK-----\n", f"BLOCK-----\nComment: {rr.PLACEHOLDER}\n", 1
        )
        result = redact(block)
        assert result.text == rr.PLACEHOLDER
        assert rule_ids(result) == ["pgp.private_key"]

    def test_pgp_public_key_block_is_kept(self) -> None:
        block = _armour("PGP PUBLIC KEY BLOCK")
        assert redact(block).text == block

    @pytest.mark.parametrize("scheme", ["postgres", "mysql", "https", "redis", "amqp"])
    def test_url_userinfo_password_is_redacted_and_the_user_kept(self, scheme: str) -> None:
        password = synth(16, ALNUM, 70)
        result = redact(f"DATABASE_URL {scheme}://app_user:{password}@db.internal:5432/app")
        assert (
            result.text == f"DATABASE_URL {scheme}://app_user:{rr.PLACEHOLDER}@db.internal:5432/app"
        )
        assert rule_ids(result) == ["url.userinfo_password"]

    @pytest.mark.parametrize(
        "text",
        [
            "https://db.internal:5432/app",
            "https://user@example.com/path",
            "postgres://app_user:${DB_PASSWORD}@db.internal/app",
            "postgres://app_user:<password>@db.internal/app",
            "see https://example.com/a:b/c@d",
        ],
    )
    def test_url_without_a_real_password_is_kept(self, text: str) -> None:
        assert redact(text).text == text


class TestSessionCookie:
    @pytest.mark.parametrize(
        "template",
        [
            "Set-Cookie: sessionid={v}; Path=/; HttpOnly; Secure",
            "Cookie: theme=dark; PHPSESSID={v}; lang=en",
            "Cookie: JSESSIONID={v}",
            "Set-Cookie: connect.sid={v}; Path=/",
            "Set-Cookie: ASP.NET_SessionId={v}; path=/",
            'curl -H "Cookie: session={v}" https://app.example.com/me',
        ],
    )
    def test_session_value_is_redacted_and_other_cookies_kept(self, template: str) -> None:
        value = synth(32, ALNUM, 80)
        result = redact(template.format(v=value))
        assert result.text == template.format(v=rr.PLACEHOLDER)
        assert rule_ids(result) == ["labelled.session_cookie"]

    def test_short_or_placeholder_session_is_kept(self) -> None:
        for text in ("session=abc", "sessionid=<session-id>", "session: ${SESSION}"):
            assert redact(text).text == text
