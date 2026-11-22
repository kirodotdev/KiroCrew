"""Labelled values after a listed credential name, the command-line and ``.netrc``
readers, and private keys masked body only (synthetic values only)."""

from __future__ import annotations

import base64

import pytest
from redaction_structured_cases import (
    HEX,
    PEM_BEGIN,
    PEM_BODY,
    PEM_END,
    assert_linear_work,
)

from kiro_crew.security.redaction_structured import TAG, TableError, build_table, redact
from kiro_crew.security.redaction_structured.table import ROWS, TABLE
from kiro_crew.testing.synthetic_credentials import (
    ALNUM,
    aws_secret,
    github_classic,
    synth,
)

LABELLED = next(r for r in TABLE.known_keys if r.id == "labelled.secret")


def _secret_rules(text: str) -> list[str]:
    return [rule for _, _, rule, tier in redact(text)[1] if tier == "secret"]


# ── The listed names ──

#: Common credential names the ``labelled.secret`` row lists, each read in four spellings.
_LISTED = """PASSWORD DB_PASSWORD DATABASE_PASSWORD POSTGRES_PASSWORD MYSQL_PASSWORD REDIS_PASSWORD
ADMIN_PASSWORD SMTP_PASSWORD MYSQL_PWD PASSWD SECRET SECRET_KEY CLIENT_SECRET APP_SECRET
JWT_SECRET API_SECRET API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY ACCESS_TOKEN REFRESH_TOKEN
AUTH_TOKEN GITHUB_TOKEN GH_TOKEN NPM_TOKEN SLACK_BOT_TOKEN SENTRY_AUTH_TOKEN TWILIO_AUTH_TOKEN
PGPASSWORD AUTHORIZATION_TOKEN TOKEN""".split()
#: Known gaps: credential names the row does not list. An opaque value after one stays
#: visible; a value of a published format is still masked by its format row.
_UNLISTED = """EMAIL_PASSWORD LDAP_PASSWORD RABBITMQ_PASSWORD KEYSTORE_PASSWORD DB_PASS SMTP_PASS
SESSION_SECRET COOKIE_SECRET WEBHOOK_SECRET SIGNING_SECRET SENDGRID_API_KEY STRIPE_SECRET_KEY
ACCESS_KEY STORAGE_ACCESS_KEY PRIVATE_KEY API_TOKEN BEARER_TOKEN GITLAB_TOKEN DB_CREDENTIALS
BASIC_AUTH""".split()


def test_the_row_lists_the_names_these_tests_read() -> None:
    keys = {k.replace(".", "").replace("_", "").lower() for k in LABELLED.keys}
    assert {n.replace("_", "").lower() for n in _LISTED} <= keys
    assert not {n.replace("_", "").lower() for n in _UNLISTED} & keys


def _spellings(name: str) -> list[str]:
    words = name.lower().split("_")
    camel = words[0] + "".join(w.title() for w in words[1:])
    return [name, name.lower(), camel, "-".join(words)]


@pytest.mark.parametrize("name", _LISTED)
def test_an_opaque_value_after_a_listed_name_is_masked_in_every_spelling(name: str) -> None:
    value = synth(32, ALNUM, 70)
    for spelling in _spellings(name):
        for text in (
            f"{spelling}={value}\n",
            f"  {spelling}: {value}\n",
            f'{{"{spelling}": "{value}"}}',
        ):
            assert redact(text)[0] == text.replace(value, TAG), text
    flag = "--" + "-".join(name.lower().split("_"))
    assert redact(f"tool {flag} {value} x")[0] == f"tool {flag} {TAG} x"


@pytest.mark.parametrize("name", _UNLISTED)
def test_stays_visible_known_gap_an_opaque_value_after_an_unlisted_name(name: str) -> None:
    value = synth(32, ALNUM, 70)
    for spelling in _spellings(name):
        for text in (
            f"{spelling}={value}\n",
            f"  {spelling}: {value}\n",
            f'{{"{spelling}": "{value}"}}',
        ):
            assert redact(text)[0] == text, text
    flag = "--" + "-".join(name.lower().split("_"))
    assert redact(f"tool {flag} {value} x")[0] == f"tool {flag} {value} x"


@pytest.mark.parametrize(
    "key",
    ["author", "AUTHOR_NAME", "NextToken", "nextPageToken", "csrf_token", "PASSWORD_FILE"],
)
def test_a_name_that_is_no_credential_name_stays(key: str) -> None:
    text = f"{key}={synth(32, ALNUM, 69)}\n"
    assert redact(text)[0] == text


def test_stays_visible_known_gap_a_name_that_only_holds_the_aws_words() -> None:
    """``MyAppSecretAccessKey`` is neither the AWS label nor a listed name."""
    text = f"MyAppSecretAccessKey={aws_secret()}"
    assert redact(text)[0] == text


def test_the_aws_names_keep_their_own_value_gate() -> None:
    # Owner decision O1b: the AWS label rows read the AWS value shapes only, so another
    # value after an exact AWS name stays.
    value = synth(32, ALNUM, 71)
    for text in (f"AWS_SECRET_ACCESS_KEY={value}", f'{{"SessionToken": "{value}"}}'):
        assert redact(text)[0] == text


def test_a_listed_credential_name_beats_the_git_row() -> None:
    # Owner ruling R4: a label is the evidence; the object-name row is only an alphabet
    # and a length. A webhook or JWT secret is often 40 or 64 hex characters.
    sha = synth(40, HEX, 72)
    assert redact(f"JWT_SECRET={sha}\n")[0] == f"JWT_SECRET={TAG}\n"


def test_a_yaml_value_and_a_code_name() -> None:
    assert redact("  password: hunter2hunter2\n")[0] == f"  password: {TAG}\n"
    assert redact("db_password = abc123XYZ789\n")[0] == f"db_password = {TAG}\n"
    for text in (
        "    password: SecretStr\n",
        "    token: Optional[str] = None\n",
        "password = user_password\n",
    ):
        assert redact(text)[0] == text


@pytest.mark.parametrize(
    "value", ["my-secret-pw", "see the vault", "${DB_PASSWORD}", "$db_password"]
)
def test_words_and_references_after_a_label_stay(value: str) -> None:
    for text in (f"DB_PASSWORD={value}", f'{{"password": "{value}"}}'):
        assert redact(text)[0] == text


@pytest.mark.parametrize(
    "value", ["kerberos", "see-the-vault", "shortvalue", "correct-horse-battery"]
)
def test_plain_words_after_a_password_label_are_masked(value: str) -> None:
    """One word, or words naming nothing an example names, can be a real password."""
    assert redact(f"DB_PASSWORD={value}\n")[0] == f"DB_PASSWORD={TAG}\n"


def test_stays_visible_known_gap_a_listed_name_inside_a_longer_one() -> None:
    """``9DB_PASSWORD`` and ``MY_DB_PASSWORD`` are not ``DB_PASSWORD``: names are equal or
    not read."""
    for text in ("9DB_PASSWORD=ordinary", "MY_DB_PASSWORD=ordinary"):
        assert redact(text)[0] == text


def test_a_long_lower_case_value_is_generated_and_masked() -> None:
    assert redact("SMTP_PASSWORD=qwhzkxmvbnlptrsd")[0] == f"SMTP_PASSWORD={TAG}"


@pytest.mark.parametrize(
    "text",
    [
        "SLACK_BOT_TOKEN=xoxb-your-bot-token",
        "xoxb-slack-bot-token-goes-here",
        "GITHUB_TOKEN=ghp_" + "x" * 36,
        "sk_live_" + "x" * 24,
        "STRIPE_SECRET_KEY=sk_test_YOUR_KEY_HERE",
    ],
)
def test_a_documented_token_example_stays(text: str) -> None:
    assert redact(text)[0] == text


def test_an_unlisted_name_in_a_url_query_stays() -> None:
    url = f"https://h.example.test/x?a=1&X-Amz-Credential={synth(30, ALNUM, 73)}&b=2"
    assert redact(url)[0] == url


# ── Command lines and .netrc ──


@pytest.mark.parametrize(
    ("text", "expected", "rule"),
    [
        ("mysql -u root -pS3cretPass1 db", f"mysql -u root -p{TAG} db", "cli.mysql_password"),
        (
            "mysqldump --password=S3cretPass1 db",
            f"mysqldump --password={TAG} db",
            "cli.mysql_password",
        ),
        (
            "kubectl exec -it db -- mysql -uroot -p'S3cret Pass1'",
            f"kubectl exec -it db -- mysql -uroot -p'{TAG}'",
            "cli.mysql_password",
        ),
        (
            "docker login -u bot --password S3cretPass1 r.example.test",
            f"docker login -u bot --password {TAG} r.example.test",
            "cli.docker_login_password",
        ),
        (
            "sudo docker login -u bot -p=S3cretPass1 r.example.test",
            f"sudo docker login -u bot -p={TAG} r.example.test",
            "cli.docker_login_password",
        ),
    ],
)
def test_a_command_line_password(text: str, expected: str, rule: str) -> None:
    assert redact(text)[0] == expected
    assert _secret_rules(text) == [rule]


@pytest.mark.parametrize(
    "text",
    [
        "mysql -u root -p mydb",
        "mysql -u root -p",
        "mysql -P3306 -h db.example.test",
        "ssh -p2222 host.example.test",
        "echo -pS3cretPass1",
        "mkdir -pS3cretPass1 && mysql -u root -p db",
        "docker run -p 8080:80 nginx",
        "docker push -p S3cretPass1",
        "docker login --password-stdin r.example.test",
    ],
)
def test_a_flag_that_is_no_password_stays(text: str) -> None:
    assert redact(text)[0] == text


def test_a_netrc_default_entry() -> None:
    text = "default login anonymous password S3cretPass1"
    assert redact(text)[0] == f"default login anonymous password {TAG}"
    assert _secret_rules(text) == ["netrc.password"]


@pytest.mark.parametrize(
    "text",
    [
        "machine h.example.test login bob password",
        "password S3cretPass1 alone",
        "the host login is bob, password S3cretPass1",
        "machine h.example.test login bob password your-password",
    ],
)
def test_text_that_is_no_netrc_password_stays(text: str) -> None:
    assert redact(text)[0] == text


def test_a_basic_value_with_a_placeholder_password_stays() -> None:
    for pair in (b"username:password", b"user:password", b"admin:changeme"):
        text = "Authorization: Basic " + base64.b64encode(pair).decode()
        assert redact(text)[0] == text
    value = base64.b64encode(b"svc:Xy7abQ29zzLL").decode()
    assert redact(f"Authorization: Basic {value}")[0] == f"Authorization: Basic {TAG}"


# ── Private keys: the body only ──


def test_a_private_key_keeps_its_armour_lines_and_stays_a_fixed_point() -> None:
    once = redact(f"{PEM_BEGIN}\n{PEM_BODY}\n{PEM_END}")[0]
    assert once == f"{PEM_BEGIN}\n{TAG}\n{PEM_END}"
    assert redact(once)[0] == once
    assert _secret_rules(once) == []


def test_an_empty_block_has_no_body_to_mask() -> None:
    text = f"{PEM_BEGIN}\n{PEM_END}"
    assert redact(text)[0] == text


def test_a_text_cut_inside_the_end_line_masks_the_body_alone() -> None:
    text = f"{PEM_BEGIN}\n{PEM_BODY}\n{PEM_END[:12]}"
    assert redact(text)[0] == f"{PEM_BEGIN}\n{TAG}\n{PEM_END[:12]}"
    assert _secret_rules(text) == ["pem.private_key_unterminated"]


# ── The table ──


def test_the_loader_refuses_a_name_shape() -> None:
    """A known-key row lists its names; a field that reads a name's words is refused."""
    base = dict(next(r for r in ROWS if r["id"] == "labelled.secret"))
    for extra in (
        {"name_words": ["pass"]},
        {"name_pairs": [["api", "key"]]},
        {"not_secret_before": {"token": ["next"]}},
        {"path_words": ["pwd"]},
    ):
        with pytest.raises(TableError, match="unknown field"):
            build_table([{**base, **extra}])
    netrc = dict(next(r for r in ROWS if r["id"] == "netrc.password"))
    with pytest.raises(TableError, match="reads no names"):
        build_table([{**netrc, "names": ["password"]}])


# ── Short values, a short curl password ──


@pytest.mark.parametrize("name", ["password", "secret"])
def test_a_short_value_after_a_listed_name_is_masked(name: str) -> None:
    assert redact(f"{name}=FAKEpw1")[0] == f"{name}={TAG}"


def test_a_four_character_curl_user_password_is_masked() -> None:
    assert redact("curl -u u:Zx9a http://h/")[0] == f"curl -u u:{TAG} http://h/"


@pytest.mark.parametrize(
    "text",
    [
        "token count=5",
        "password=x1",
        "tokenizer=fast",
        "token=words like this",
    ],
)
def test_ordinary_text_is_not_masked(text: str) -> None:
    assert redact(text)[0] == text


def test_a_short_token_value_is_masked() -> None:
    assert redact("token=FAKEpw1")[0] == f"token={TAG}"


@pytest.mark.parametrize("name", ["password", "token"])
def test_a_short_elided_example_stays(name: str) -> None:
    assert redact(f"{name}=sk-...")[0] == f"{name}=sk-..."


@pytest.mark.parametrize("ellipsis", ["...", "\u2026"])
@pytest.mark.parametrize("name", ["password", "secret"])
@pytest.mark.parametrize("head", ["hunter2", "Ab3xY9q", "Zq81Lm"])
def test_a_short_real_looking_value_cut_by_an_ellipsis_is_masked(
    name: str, head: str, ellipsis: str
) -> None:
    assert redact(f"{name}={head}{ellipsis}")[0] == f"{name}={TAG}"


@pytest.mark.parametrize("ellipsis", ["...", "\u2026"])
@pytest.mark.parametrize("name", ["password", "secret"])
@pytest.mark.parametrize("head", ["lemon", "abc12", "a.b.c"])
def test_a_very_short_value_cut_by_an_ellipsis_is_masked(
    name: str, head: str, ellipsis: str
) -> None:
    assert redact(f"{name}={head}{ellipsis}")[0] == f"{name}={TAG}"


@pytest.mark.parametrize("ellipsis", ["...", "\u2026"])
@pytest.mark.parametrize("name", ["password", "secret"])
@pytest.mark.parametrize(
    "head",
    [
        "lemon-",
        "lemon_",
        "lemon=",
        "Ab3xY9-",
        "sk-ant-lemon",  # a vendor stub, then a word
        "sk-proj-lemon",
        "sk--lemon",  # a doubled separator after the stub
    ],
)
def test_a_short_word_and_separator_cut_by_an_ellipsis_is_masked(
    name: str, head: str, ellipsis: str
) -> None:
    assert redact(f"{name}={head}{ellipsis}")[0] == f"{name}={TAG}"


@pytest.mark.parametrize("ellipsis", ["...", "\u2026"])
@pytest.mark.parametrize("name", ["password", "secret"])
def test_an_empty_head_before_an_ellipsis_stays(name: str, ellipsis: str) -> None:
    text = f"{name}={ellipsis}"
    assert redact(text)[0] == text


@pytest.mark.parametrize("ellipsis", ["...", "\u2026"])
@pytest.mark.parametrize("stub", ["sk-", "sk-ant-", "ghp_", "AKIA", "github_pat_", "AROA"])
def test_a_vendor_prefix_stub_stays(stub: str, ellipsis: str) -> None:
    text = f"password={stub}{ellipsis}"
    assert redact(text)[0] == text


@pytest.mark.parametrize("ellipsis", ["...", "\u2026"])
def test_a_long_token_cut_by_an_ellipsis_is_masked(ellipsis: str) -> None:
    cut = github_classic()[:20] + ellipsis
    assert redact(f"token={cut}")[0] == f"token={TAG}"


def _repeat(unit: str) -> object:
    return lambda n: (unit * (n // len(unit) + 1))[:n]


@pytest.mark.parametrize(
    "unit",
    [
        "the author said token auth pass api access private key_id=x\n",
        "MY_TOKEN=abc123XYZ789qq DB_PASSWORD: Xk29aLmQ0pZr7Tb\n",
        "mysql -p -pS3cretPass1 docker login -p S3cretPass1 password S3cretPass1\n",
        "machine h login u password S3cretPass1 ",
    ],
)
def test_the_new_anchors_stay_linear(unit: str) -> None:
    assert_linear_work(_repeat(unit), redact)  # type: ignore[arg-type]
