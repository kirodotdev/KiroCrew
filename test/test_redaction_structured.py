"""Per-rule tests for the structured credential detector (no regex)."""

from __future__ import annotations

import ast
import pathlib

import pytest
from redaction_structured_cases import (
    PEM_BEGIN,
    PEM_BODY,
    aws_key_id,
    aws_secret,
    benign_cases,
    secret_cases,
)

import kiro_crew.security.redaction_structured as rs
from kiro_crew.security.redaction_structured import TAG, TableError, build_table, redact
from kiro_crew.security.redaction_structured.table import ROWS

SECRET_CASES = secret_cases()
BENIGN_CASES = benign_cases()


def secret_rules(spans: list[tuple[int, int, str, str]]) -> list[str]:
    return [rule for _, _, rule, tier in spans if tier == "secret"]


@pytest.mark.parametrize(
    ("text", "expected", "rules"),
    [pytest.param(t, e, r, id=n) for n, t, e, r in SECRET_CASES],
)
def test_secret_is_replaced(text: str, expected: str, rules: list[str]) -> None:
    out, spans = redact(text)
    assert out == expected
    assert secret_rules(spans) == rules


@pytest.mark.parametrize("text", [pytest.param(t, id=n) for n, t in BENIGN_CASES])
def test_benign_text_is_kept(text: str) -> None:
    out, spans = redact(text)
    assert out == text
    assert secret_rules(spans) == []


@pytest.mark.parametrize(
    "text", [pytest.param(t, id=n) for n, t, _, _ in SECRET_CASES] + [t for _, t in BENIGN_CASES]
)
def test_redaction_is_idempotent(text: str) -> None:
    once, _ = redact(text)
    twice, spans = redact(once)
    assert twice == once
    assert secret_rules(spans) == []


def test_spans_are_input_offsets_and_hold_no_value() -> None:
    secret = aws_secret()
    text = f"id {aws_key_id()} s {secret}"
    _, spans = redact(text)
    pair = [s for s in spans if s[2] == "aws.secret_access_key"]
    assert len(pair) == 1
    start, end, _, tier = pair[0]
    assert text[start:end] == secret
    assert tier == "secret"
    assert all(secret not in repr(field) for span in spans for field in span[2:])


def test_key_id_stays_visible_and_is_reported_as_identifier() -> None:
    key_id = aws_key_id()
    out, spans = redact(f"{key_id} {aws_secret()}")
    assert out.startswith(key_id + " ")
    assert (0, 20, "aws.access_key_id", "identifier") in spans


def test_pairing_window_is_300_characters() -> None:
    key_id, secret = aws_key_id(), aws_secret()
    near = key_id + " " * 300 + secret
    far = key_id + " " * 301 + secret
    assert redact(near)[0].endswith(TAG)
    assert redact(far)[0] == far


def test_structural_identifier_beats_contained_label_hit() -> None:
    secret = aws_secret()
    text = f"see https://docs.example.test/q/aws_secret_access_key={secret}/page"
    assert redact(text)[0] == text


def test_unterminated_block_runs_to_end_of_text() -> None:
    text = f"intro {PEM_BEGIN}\n{PEM_BODY}\n" + "prose after the header " * 50
    out, spans = redact(text)
    assert out == "intro " + TAG
    assert spans[-1][:3] == (6, len(text), "pem.private_key_unterminated")


def test_block_past_max_block_is_unterminated() -> None:
    body = "A" * 16001
    text = f"{PEM_BEGIN}\n{body}\n" + PEM_BEGIN.replace("BEGIN", "END") + "\nafter"
    out, spans = redact(text)
    assert out == TAG
    assert secret_rules(spans) == ["pem.private_key_unterminated"]


def test_quoted_assignment_on_a_longer_line_is_a_known_false_negative() -> None:
    # Owner ruling: the assignment parser is not widened. An AWS label assignment that
    # sits inside a quoted string, glued to the quote, is not one of the read forms
    # (line start, or after a blank or ``;``), so the value stays visible.
    secret = aws_secret()
    missed = f'run("AWS_SECRET_ACCESS_KEY={secret}") now'
    assert redact(missed)[0] == missed
    # The same assignment led by a blank inside the quotes is a read form.
    caught = f'run("x AWS_SECRET_ACCESS_KEY={secret}") now'
    assert redact(caught)[0] == f'run("x AWS_SECRET_ACCESS_KEY={TAG}") now'


def test_known_key_lookups_are_built_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.security.redaction_structured import known_key

    redact("warm AWS_SECRET_ACCESS_KEY=x")
    calls = 0
    real = known_key.spellings

    def counting(rows):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return real(rows)

    monkeypatch.setattr(known_key, "spellings", counting)
    for _ in range(5):
        redact(f"AWS_SECRET_ACCESS_KEY={aws_secret()}")
    assert calls == 0


def test_tag_wording() -> None:
    assert TAG == "[REDACTED: credential]"


# ── The table is data ──


def test_rule_inventory() -> None:
    ids = {(r, d, t) for r, d, t in rs.TABLE.rule_ids()}
    secret = {r for r, _, t in ids if t == "secret"}
    identifier = {r for r, _, t in ids if t == "identifier"}
    assert secret == {
        "github.classic",
        "github.fine_grained",
        "slack.token",
        "stripe.key",
        "stripe.webhook_secret",
        "anthropic.key",
        "google.api_key",
        "google.oauth_client_secret",
        "gitlab.pat",
        "sendgrid.key",
        "hashicorp.vault",
        "shopify.token",
        "digitalocean.token",
        "jwt",
        "jwe",
        "link_token",
        "aws.secret_access_key",
        "pem.private_key",
        "pem.private_key_unterminated",
        "pgp.private_key",
        "pgp.private_key_unterminated",
        "aws.secret_access_key.label",
        "aws.session_token.label",
        "url.oauth_param.client_secret",
        "url.oauth_param.access_token",
        "url.oauth_param.refresh_token",
        "url.oauth_param.id_token",
        "url.oauth_param.code_verifier",
        "url.oauth_param.password",
    }
    assert identifier == {
        "git.object_name",
        "aws.access_key_id",
        "twilio.account_sid",
        "arn",
        "url.structure",
        "commit.permalink",
        "oauth.public_param",
        "aws.x_amz_id_2",
        "aws.request_id",
        "ssh.fingerprint",
        "data_uri",
        "image.base64",
        "aws.doc_example",
        "macos.temp_path",
        "placeholder",
        "oauth.form_param",
    }


@pytest.mark.parametrize(
    "extra",
    [{"pattern": "gh[pousr]_"}, {"regex": "x"}, {"validate": "github_crc32"}],
)
def test_loader_refuses_unknown_fields(extra: dict[str, str]) -> None:
    row = dict(ROWS[0])
    row.update(extra)
    with pytest.raises(TableError, match="unknown field"):
        build_table([row])


def test_loader_refuses_unknown_alphabet_and_checksum() -> None:
    row = dict(ROWS[0])
    row["body"] = [{"alphabet": "anything", "length": 36}]
    with pytest.raises(TableError, match="unknown alphabet"):
        build_table([row])
    row = dict(ROWS[0])
    row["checksum"] = "md5"
    with pytest.raises(TableError, match="unknown checksum"):
        build_table([row])


def test_loader_refuses_a_run_that_swallows_its_literal() -> None:
    row = {
        "id": "x.bad",
        "tier": "secret",
        "detector": "token_format",
        "prefixes": ["x_"],
        "body": [
            {"alphabet": "b64url", "min": 2, "max": 9},
            {"literal": ["-"]},
            {"alphabet": "base62", "length": 4},
        ],
        "boundary": "word_dash",
    }
    with pytest.raises(TableError, match="variable run"):
        build_table([row])


# ── REQ-N1: no regular expressions anywhere in the package ──

_PACKAGE = pathlib.Path(rs.__file__).parent
_FORBIDDEN = {"re", "regex", "sre_compile", "sre_parse", "_sre"}


def _package_sources() -> list[pathlib.Path]:
    files = sorted(_PACKAGE.glob("*.py"))
    assert len(files) >= 9
    return files


@pytest.mark.parametrize("path", _package_sources(), ids=lambda p: p.name)
def test_module_does_not_import_regex(path: pathlib.Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {alias.name.split(".")[0] for alias in node.names}
            assert not names & _FORBIDDEN, f"{path.name}:{node.lineno} imports {names}"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in _FORBIDDEN, f"{path.name}:{node.lineno} imports from {root}"
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in {"__import__", "import_module"} and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    assert arg.value.split(".")[0] not in _FORBIDDEN, f"{path.name}:{node.lineno}"
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert (
                node.value.id not in _FORBIDDEN
            ), f"{path.name}:{node.lineno} uses {node.value.id}"


def test_source_guard_catches_a_regex_import(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import re as _r\n", encoding="utf-8")
    with pytest.raises(AssertionError):
        test_module_does_not_import_regex(bad)


# ── No randomness scores or character-mix checks ──

#: Modules a score over a value's characters would need.
_SCORE_MODULES = {"math", "statistics", "kiro_crew.security.redaction_entropy"}
#: Name fragments of a randomness score or a character-mix check.
_SCORE_TERMS = ("entropy", "shannon", "randomness", "char_mix", "charmix", "diversity")


@pytest.mark.parametrize("path", _package_sources(), ids=lambda p: p.name)
def test_module_has_no_randomness_or_character_mix_check(path: pathlib.Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in _SCORE_MODULES, f"{path.name}:{node.lineno}"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module not in _SCORE_MODULES, f"{path.name}:{node.lineno}"
            assert not any(
                f"{module}.{a.name}" in _SCORE_MODULES for a in node.names
            ), f"{path.name}:{node.lineno}"
        name = ""
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.arg):
            name = node.arg
        lowered = name.lower()
        assert not any(t in lowered for t in _SCORE_TERMS), f"{path.name}:{node.lineno} {name}"


def test_score_guard_catches_an_entropy_check(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("def shannon_entropy(s):\n    return 0\n", encoding="utf-8")
    with pytest.raises(AssertionError):
        test_module_has_no_randomness_or_character_mix_check(bad)
