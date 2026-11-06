"""The credential plan's callers with the structured detectors' switch on.

* pass 1 does not report the branches the detectors cover, a bare AWS key ID, or a
  labelled key ID the detectors protect; every other pass-1 branch keeps running;
* an Identifier value the detectors protect stays visible to the later passes;
* a pass-1 warning gives the length of its span on the original text;
* the display caches and stored verdicts name the engine that computed them, so a
  switch change never serves an answer the other engine produced;
* a field-name key under an authorization key in a recorded frame stays legible.

Every credential here is synthetic and assembled at run time.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from redaction_structured_cases import B64, aws_key_id, aws_secret, github_classic, synth

from kiro_crew.acp import _frame_record
from kiro_crew.dashboard import chat_utils, display_battery
from kiro_crew.dashboard import display_verdicts as dv
from kiro_crew.dashboard.chat_utils import _prepare_messages
from kiro_crew.security import StreamRedactor, redact
from kiro_crew.security.redaction import redact_credentials
from kiro_crew.security.redaction_structured import compat

KEY = "dashboard_chat-1-1700000000"
#: A word the stand-in battery below redacts only while the detectors run.
WORD = "codeword-6612"
TAG = "[REDACTED: credential]"


@pytest.fixture
def table_on() -> Iterator[None]:
    before = compat.use_rule_table()
    compat.publish_use_rule_table(True)
    try:
        yield
    finally:
        compat.publish_use_rule_table(before)


@pytest.fixture
def switch() -> Iterator[None]:
    """Starts OFF; the test moves it. Restored afterwards."""
    before = compat.use_rule_table()
    compat.publish_use_rule_table(False)
    try:
        yield
    finally:
        compat.publish_use_rule_table(before)


def test_the_switch_ships_on() -> None:
    assert compat._USE_RULE_TABLE is True


# ── pass 1 without the covered branches ─────────────────────────────────────


def test_a_labelled_aws_secret_keeps_its_label(table_on):
    """``aws_secret_access_key = <secret> words`` -> label kept, value replaced."""
    out, _ = redact_credentials("aws_secret_access_key = " + aws_secret(seed=3) + " words")
    assert out == f"aws_secret_access_key = {TAG} words"


@pytest.mark.parametrize("size", [1, 2, 3, 5, 8, 13, 40])
def test_a_labelled_aws_secret_streams_as_the_batch_pass(table_on, size):
    text = "aws_secret_access_key = " + aws_secret(seed=3) + " words"
    stream = StreamRedactor()
    out = "".join(stream.feed(text[i : i + size]) for i in range(0, len(text), size))
    assert out + stream.flush() == redact(text)


def test_a_bare_aws_key_id_stays_visible(table_on):
    key_id = aws_key_id(seed=21)
    assert redact_credentials(f"id {key_id} end")[0] == f"id {key_id} end"


def test_a_labelled_aws_key_id_stays_visible(table_on):
    key_id = aws_key_id(seed=22)
    assert redact_credentials(f"AccessKeyId: {key_id}")[0] == f"AccessKeyId: {key_id}"


def test_a_labelled_key_id_field_holding_no_key_id_is_still_redacted(table_on):
    """The ``AccessKeyId`` branch keeps running for a value the detectors do not protect."""
    value = synth(24, seed=23)
    out, _ = redact_credentials(f"AccessKeyId: {value} next")
    assert value not in out


def test_the_url_userinfo_branch_keeps_running(table_on):
    password = synth(18, seed=24)
    out, _ = redact_credentials(f"see postgres://app:{password}@db.example.test/main")
    assert password not in out


def _long_jwt() -> str:
    from redaction_structured_cases import b64url_json

    claims = {"sub": "user-1", "groups": [f"group-{i:04d}" for i in range(300)]}
    return b64url_json({"alg": "RS256"}) + "." + b64url_json(claims) + "." + synth(342, seed=28)


def _long_iv_jwe() -> str:
    from redaction_structured_cases import b64url_json

    header = b64url_json({"alg": "dir", "enc": "A256GCM"})
    return header + ".." + "A" * 101 + ".YQ." + synth(22, seed=29)


@pytest.mark.parametrize("make", [_long_jwt, _long_iv_jwe], ids=["jwt", "jwe"])
def test_a_jose_token_longer_than_the_detectors_read_is_still_redacted(table_on, make):
    """A token past every detector row's bounds: pass 1's JWT branch removes it."""
    token = make()
    out = redact(f"id_token {token} end")
    assert out == "id_token [REDACTED: credential] end"


def _leaks(token: str, out: str, width: int = 24) -> bool:
    return any(token[i : i + width] in out for i in range(0, len(token) - width + 1))


def _signed_jwt() -> str:
    from redaction_structured_cases import b64url_json

    head = b64url_json({"alg": "HS256", "typ": "JWT"})
    return head + "." + b64url_json({"sub": "a", "x": "y"}) + "." + synth(43, seed=30)


def _non_object_payload_jws() -> str:
    from redaction_structured_cases import b64url_json

    return b64url_json({"alg": "HS256"}) + ".eyJ" + synth(40, seed=31) + "." + synth(43, seed=32)


@pytest.mark.parametrize(
    "build",
    [
        lambda t: f"url /x/{t}.json",
        lambda t: f"T {t}.Next",
        lambda t: f"T {t}...and",
        lambda t: f"T {t}..x",
        lambda t: f"{t}.{t}",
    ],
    ids=["dot-json", "dot-word", "three-dots", "two-dots-word", "two-tokens"],
)
def test_a_jwt_the_detectors_refuse_for_a_trailing_segment_is_redacted(table_on, build):
    """The detectors read the whole dot run and refuse it; pass 1 still removes the token."""
    token = _signed_jwt()
    assert not _leaks(token, redact(build(token)))


def test_a_jws_whose_payload_is_not_an_object_is_redacted(table_on):
    token = _non_object_payload_jws()
    assert not _leaks(token, redact(f"t {token} end"))


@pytest.mark.parametrize("size", [7, 64, 1000])
def test_a_jwt_longer_than_the_detectors_read_streams_redacted(table_on, size):
    token = _long_jwt()
    text = f"id_token {token} end"
    stream = StreamRedactor()
    out = "".join(stream.feed(text[i : i + size]) for i in range(0, len(text), size))
    out += stream.flush()
    assert not _leaks(token, out)


@pytest.mark.parametrize("glue", ["-", "."])
def test_a_token_value_a_detector_hit_starts_is_redacted_whole(table_on, glue):
    """The parts of a ``token=`` value a detector hit leaves are redacted too."""
    tail = glue + synth(21, seed=33)
    out, _ = redact_credentials(f"x?token={github_classic(seed=32)}{tail} next")
    assert tail[1:] not in out
    assert out.endswith(" next")


@pytest.mark.parametrize(
    "build",
    [
        lambda a, b: f"see https://h.example/p?token=abcDEF12.{a}.tailXYZ end",
        lambda a, b: f"x?token=aa11.{a}.mid22.{b}.zz end",
        lambda a, b: f"x?token={a}-{synth(21, seed=34)} next",
    ],
    ids=["hit-mid-value", "two-hits", "hit-at-start"],
)
def test_a_token_value_a_hit_touches_matches_the_switch_off_plan(switch, build):
    """Text, warnings and records equal the plan without the detectors."""
    from kiro_crew.security.redaction import redact_credentials_with_records

    text = build(github_classic(seed=35), github_classic(seed=36))
    off = redact_credentials_with_records(text)
    compat.publish_use_rule_table(True)
    on = redact_credentials_with_records(text)
    assert on[0] == off[0]
    assert on[1] == off[1]
    assert [(m.rule, m.value) for m in on[2]] == [(m.rule, m.value) for m in off[2]]


def test_a_client_secret_past_the_url_cap_is_still_removed(table_on):
    """A URL longer than the detectors' URL cap: main's own passes still remove the value."""
    secret = synth(40, seed=25)
    url = "https://auth.example.test/cb?pad=" + "a" * 5000 + "&client_" + "secret=" + secret
    assert secret not in redact(f"go {url} done")


def test_residual_matches_drop_only_the_covered_rules(table_on):
    from kiro_crew.security import redaction

    pem = (
        "-----BEGIN RSA "
        + "PRIVATE KEY-----\n"
        + synth(64, B64, 26)
        + "\n-----END RSA "
        + "PRIVATE KEY-----"
    )
    text = f"{pem}\n{github_classic(seed=27)}\nAccessKeyId: {compat.PLACEHOLDER * 20}"
    rules = {redaction._pass1_rule(m.group())[0] for m in compat.residual_matches(text)}
    assert rules == {"github_token"}


def test_switch_off_pass1_reads_every_branch(switch):
    from kiro_crew.security import redaction

    text = (
        "-----BEGIN RSA "
        + "PRIVATE KEY-----\n"
        + synth(64, B64, 28)
        + "\n-----END RSA "
        + "PRIVATE KEY-----"
    )
    assert [m.span() for m in redaction._pass1_matches(text)] == [
        m.span() for m in redaction._credential_matches(text)
    ]


# ── protected Identifier values ─────────────────────────────────────────────


def test_a_protected_value_is_kept_from_the_later_passes(table_on):
    """An AWS request id run is protected; a pass 3 bare-secret run next to it is not."""
    key_id = aws_key_id(seed=29)
    text = f"key {key_id} with secret {aws_secret(seed=30)} end"
    masks = compat.protected_values(text)
    assert [text[s:e] for s, e, _r in masks] == [key_id]
    out, _ = redact_credentials(text)
    assert key_id in out and aws_secret(seed=30) not in out


@pytest.mark.parametrize("tail", ["/{key}", "/{key}.txt", "/kirocrew-computer-shots/{key}"])
def test_a_secret_used_as_a_file_name_in_a_macos_temp_dir_is_redacted(table_on, tail):
    """Only ``/var/folders/<b>/<id>/T`` is protected; a key below it is still removed."""
    root = "/var/folders/6r/" + synth(28, "abcdefghijklmnopqrstuvwxyz0123456789_", 34) + "/T"
    key = aws_secret(seed=35)
    out = redact(root + tail.format(key=key))
    assert key not in out
    assert out.startswith(root)


def test_placeholders_never_reach_the_output(table_on):
    key_id = aws_key_id(seed=31)
    out = redact(f"AccessKeyId={key_id}&token={synth(30, seed=32)}")
    assert compat.PLACEHOLDER not in out and key_id in out


def test_a_pass1_warning_gives_the_original_length(table_on):
    """A pass-1 match that reaches into a Secret hit's tag reports the original span."""
    secret = github_classic(seed=33)
    text = f"https://user:{secret}@host.example.test/x"
    spans, warnings, _rules = compat.credential_plan_via_table(text)
    lengths = sorted(int(w.split("(")[1].split()[0]) for w in warnings if "pattern" in w)
    assert lengths[-1] == max(e - s for s, e, _t in spans)


# ── display caches ──────────────────────────────────────────────────────────


@pytest.fixture
def engine_battery(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, int]]:
    """A credential pass that redacts ``WORD`` only while the detectors run; counts calls."""
    calls = {"n": 0}
    real = display_battery.redact_credentials

    def by_engine(text: str):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if compat.use_rule_table():
            text = text.replace(WORD, "[REDACTED]")
        return real(text)

    monkeypatch.setattr(display_battery, "redact_credentials", by_engine)
    chat_utils._clear_display_redaction_cache()
    yield calls
    chat_utils._clear_display_redaction_cache()


def test_a_switch_change_is_not_served_from_the_process_cache(switch, engine_battery):
    """Input ``"see codeword-6612 now"``; off it is kept, on it is redacted, off kept again."""
    text = f"see {WORD} now"
    dv.disable_for_tests()
    assert chat_utils._redact_for_display(text) == text
    compat.publish_use_rule_table(True)
    assert chat_utils._redact_for_display(text) == "see [REDACTED] now"
    compat.publish_use_rule_table(False)
    assert chat_utils._redact_for_display(text) == text


def test_a_switch_change_is_not_served_from_the_preview_caches(switch, engine_battery):
    """The same string object previewed under each engine gets each engine's preview."""
    text = f"see {WORD} now"
    assert chat_utils._redacted_preview(text, 100) == text
    compat.publish_use_rule_table(True)
    assert chat_utils._redacted_preview(text, 100) == "see [REDACTED] now"
    compat.publish_use_rule_table(False)
    assert chat_utils._redacted_preview(text, 100) == text


def test_a_switch_moved_while_the_battery_runs_caches_nothing(switch, engine_battery, monkeypatch):
    """The answer computed while the switch moved is returned but neither cache keeps it."""
    text = f"see {WORD} now"
    real = display_battery.redact_credentials

    def flips(t: str):  # type: ignore[no-untyped-def]
        out = real(t)
        compat.publish_use_rule_table(True)
        return out

    monkeypatch.setattr(display_battery, "redact_credentials", flips)
    assert chat_utils._redact_for_display(text) == text
    monkeypatch.setattr(display_battery, "redact_credentials", real)
    assert chat_utils._redact_for_display(text) == "see [REDACTED] now"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "sessions").mkdir(parents=True)
    (h / "sessions" / f"{KEY}.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_HOME", str(h))
    monkeypatch.setattr(dv, "rules_version", lambda: b"v" * 16)
    monkeypatch.setattr(dv, "LOAD_AFTER_MISSES", 0)
    dv.disable_for_tests()
    yield h
    dv.disable_for_tests()
    with dv._written_guard:
        dv._written.clear()


def _render(content: str) -> str:
    rows = [{"role": "assistant", "content": content, "cls": "msg msg-a"}]
    with dv.session(KEY):
        out = _prepare_messages(rows, running=False, live_child="", workspace="ws")
    return json.dumps(out)


@pytest.mark.skipif(os.name == "nt", reason="stored verdicts are POSIX-only")
def test_a_stored_verdict_names_the_engine_that_judged_it(
    switch, engine_battery, home: Path, monkeypatch
):
    """Rendered once with the switch off (stored clean), then on in the same process and
    after a restart: ``codeword-6612`` is redacted both times; off again it is kept."""
    monkeypatch.setattr(chat_utils, "allowed_hosts_for", lambda _ws: frozenset())
    assert dv.enable(home)
    content = f"plain words with {WORD} inside"
    assert WORD in _render(content)
    compat.publish_use_rule_table(True)
    assert WORD not in _render(content)
    dv.disable_for_tests()
    chat_utils._clear_display_redaction_cache()
    assert dv.enable(home)
    assert WORD not in _render(content)
    compat.publish_use_rule_table(False)
    assert WORD in _render(content)


def test_the_engine_mark_is_empty_with_the_switch_off(switch):
    """Off, a cache key is the key it always was; on, it carries a mark."""
    assert dv.engine_mark() == b""
    compat.publish_use_rule_table(True)
    assert dv.engine_mark() != b""


# ── recorded frames ─────────────────────────────────────────────────────────


@pytest.fixture
def frames(monkeypatch):
    _frame_record._reset_for_tests()
    monkeypatch.delenv(_frame_record.ENV_RECORD_FRAMES, raising=False)


def test_a_field_name_key_under_a_credential_key_stays_legible(table_on, frames):
    """``{"Authorization": {"value": "Basic dXNlcjpwYXNz"}}`` keeps the ``value`` key."""
    out = json.loads(_frame_record.scrub_frame({"Authorization": {"value": "Basic dXNlcjpwYXNz"}}))
    assert out == {"Authorization": {"value": TAG}}


def test_a_non_field_key_under_a_credential_key_is_still_replaced(table_on, frames):
    """``{"Authorization": {"Basic dXNlcjpwYXNz": true}}``: the key is the secret."""
    out = json.loads(_frame_record.scrub_frame({"Authorization": {"Basic dXNlcjpwYXNz": True}}))
    assert out == {"Authorization": {TAG: TAG}}


@pytest.mark.parametrize("ancestor", ["Authorization", "Proxy-Authorization", "X-Authorization"])
def test_recorded_authorization_field_names_remain_legible(table_on, frames, ancestor):
    out = json.loads(_frame_record.scrub_frame({ancestor: {"value": "opaque-example"}}))
    assert out == {ancestor: {"value": TAG}}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Authorization", True),
        ("proxy-authorization", True),
        ("X_Authorization", True),
        ("authorizations", False),
        ("password", False),
    ],
)
def test_names_authorization(name, expected):
    assert _frame_record._names_authorization(name) is expected
