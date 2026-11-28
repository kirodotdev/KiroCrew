"""A labelled credential written in Chinese or Japanese text, with full-width forms.

A key glued after CJK text or punctuation reads as a key after a blank, a full-width
separator or label letter reads as its ASCII form, and an unquoted value ends at CJK
punctuation. CJK prose that only names a key stays as written. Synthetic values only.
"""

from __future__ import annotations

import random
import unicodedata

import pytest
from redaction_structured_cases import assert_linear_work

from kiro_crew.default_ignorable import DEFAULT_IGNORABLE
from kiro_crew.security import StreamRedactor, redact
from kiro_crew.security.redaction_structured import charsets, known_key
from kiro_crew.security.redaction_structured.compat import ends_word
from kiro_crew.security.redaction_structured.engine import redact_hits
from kiro_crew.security.redaction_structured.table import TABLE
from kiro_crew.testing.synthetic_credentials import SAMPLE_AWS_SECRET

TAG = "[REDACTED: credential]"
PW = "Zq8vLm2Kd9pRt4"


@pytest.mark.parametrize("before", ["密钥", "密钥：", "密钥，", "（", "パスワード"])
def test_a_known_key_glued_after_cjk_is_read(before: str) -> None:
    text = f"{before}aws_secret_access_key={SAMPLE_AWS_SECRET}好"
    assert redact(text) == f"{before}aws_secret_access_key={TAG}好"


def test_an_upper_case_env_key_glued_after_cjk_is_read() -> None:
    assert redact(f"密码DB_PASSWORD={PW}") == f"密码DB_PASSWORD={TAG}"


@pytest.mark.parametrize(
    ("text", "want"),
    [
        (f"password：{PW}，下一个", f"password：{TAG}，下一个"),
        (f"password＝{PW}。", f"password＝{TAG}。"),
        (f"api_key：{PW}{PW}，下一个", f"api_key：{TAG}，下一个"),
        (f"aws_secret_access_key：{SAMPLE_AWS_SECRET}", f"aws_secret_access_key：{TAG}"),
        (f"DB_PASSWORD＝{PW}", f"DB_PASSWORD＝{TAG}"),
        (f"--password＝{PW}", f"--password＝{TAG}"),
        ('{"password"："' + PW + '"}', '{"password"："' + TAG + '"}'),
        (f"password：\u3000{PW}", f"password：\u3000{TAG}"),
        # A quoted value closes only on its opening quote as written.
        ('password："abc\uff02defghi"', f'password："{TAG}"'),
        ('password："ab\uff02cdefghij"', f'password："{TAG}"'),
        (f"password：\uff02{PW}\uff02", f"password：\uff02{TAG}\uff02"),
        # A value that starts with a non-ASCII character outside CJK text is read.
        ("password：\u00e9Ab3dEf9h", f"password：{TAG}"),
        ("password：\U0001f511Ab3dEf9h", f"password：{TAG}"),
        ("password：\uff04Ab3dEf9h", f"password：{TAG}"),
        ("password：\u2605Ab3dEf9h，好", f"password：{TAG}，好"),
    ],
)
def test_a_full_width_separator_is_read(text: str, want: str) -> None:
    assert redact(text) == want


@pytest.mark.parametrize(
    ("text", "want"),
    [
        (f"ｐａｓｓｗｏｒｄ={PW}，好", f"ｐａｓｓｗｏｒｄ={TAG}，好"),
        (f"ｐａｓｓｗｏｒｄ: {PW}", f"ｐａｓｓｗｏｒｄ: {TAG}"),
        (f"ＡＷＳ_SECRET_ACCESS_KEY={SAMPLE_AWS_SECRET}", f"ＡＷＳ_SECRET_ACCESS_KEY={TAG}"),
    ],
)
def test_a_full_width_label_is_read(text: str, want: str) -> None:
    assert redact(text) == want


@pytest.mark.parametrize(
    "text",
    [
        "密码说明password是什么",
        "这个token的意思",
        "密码password=user_password",
        "这里的api_key，是用来登录的",
        "（password）是必填项",
        "说明：password：在设置里",
        "token：请参考下面的文档说明",
        "password：请在设置页面里修改",
        "填入api_key＝你的密钥即可",
        "用ｔｏｋｅｎ登录",
        "password：xxxxxx",
        "api_key：YOUR_API_KEY，然后保存",
        "第一步，password=$PASSWORD，第二步",
        "password：パスワードを入力してください",
        "password：비밀번호를입력하세요",
    ],
)
def test_cjk_prose_naming_a_key_stays(text: str) -> None:
    assert redact(text) == text


@pytest.mark.parametrize(
    ("text", "want"),
    [
        # Other punctuation inside a value on an ASCII label stays in the value.
        ("password=Zq8vLm2\u2014Kd9pRt4", f"password={TAG}"),
        ("password=Pa\u00a7ss1234Word", f"password={TAG}"),
        ("password=Pa\u00bfss1234Word", f"password={TAG}"),
        ("password=abc\u2019defghi done", f"password={TAG} done"),
        ("--password=Zq8vLm2\u2019Kd9pRt4", f"--password={TAG}"),
        # CJK punctuation does too when the label and separator are ASCII.
        ("password=abc\uff0cdef123", f"password={TAG}"),
        ("password=abcdefgh\u3000ijk", f"password={TAG}"),
        # A value is read as written: a full-width ``$`` or parenthesis is no code.
        ("password=\uff04Ab3dEf9hIjK2mN done", f"password={TAG} done"),
        ("password=pass\uff08word\uff09123", f"password={TAG}"),
        # A full-width quote, blank or ``=`` after an ASCII separator starts the value.
        ("password=\uff02Ab3dEf9hIjK2mN done", f"password={TAG} done"),
        ("password: \u3000Ab3dEf9hIjK2mN", f"password: {TAG}"),
        ("password=\uff1dAb3dEf9hIjK2mN", f"password={TAG}"),
        # A full-width quote does not close an ASCII-quoted value.
        ('password="abcdefgh\uff02ijk"', f'password="{TAG}"'),
        # A full-width letter glued before a CLI flag leaves the flag a key.
        ("\uff58--password=Ab3dEf9hIjK2mN", f"\uff58--password={TAG}"),
        # The character after an AWS secret is read as written.
        (
            f"aws_secret_access_key={SAMPLE_AWS_SECRET}\uff1dx",
            f"aws_secret_access_key={TAG}\uff1dx",
        ),
        # A full-width character glued before a CLI flag leaves the flag read as written.
        (f"\uff3f--password={PW}", f"\uff3f--password={TAG}"),
        (f"Options: \uff3f--password={PW}", f"Options: \uff3f--password={TAG}"),
        (f"\uff0d--password={PW}\uff0cabc", f"\uff0d--password={TAG}"),
    ],
)
def test_an_ascii_label_reads_its_value_as_without_the_fold(text: str, want: str) -> None:
    assert redact(text) == want


@pytest.mark.parametrize(
    "text",
    [
        "password=\uff02Zq8vLm2,Kd9pRt4\uff02",
        "api_key=\uff02Zq8vLm2;Kd9pRt4\uff02 done",
        "--password=\uff02Zq8vLm2&Kd9pRt4\uff02",
    ],
)
def test_a_full_width_quoted_value_is_masked_past_a_separator(text: str) -> None:
    out = redact(text)
    assert "Zq8vLm2" not in out and "Kd9pRt4" not in out
    assert out.count(TAG) == 1
    r = StreamRedactor()
    assert "".join(r.feed(ch) for ch in text) + r.flush() == out


_LABEL_SHAPES = (
    "{k}={v}",
    "{k}: {v}",
    "{k} = {v}",
    "x; {k}={v}",
    "f({k}={v})",
    "--{f}={v}",
    "--{f} {v}",
    '{{"{k}": "{v}"}}',
    "<{k}>{v}</{k}>",
    "export {K}={v}",
    "aws configure set {k} {v}",
)
_LABELS = ("password", "api_key", "token", "secret", "aws_secret_access_key", "client_secret")
_INSERTS = (
    "\uff21",
    "\uff10",
    "\uff3f",
    "\u2019",
    "\u2014",
    "\u00a7",
    "\uff0c",
    "\u3000",
    "\uff02",
    "\uff04",
    "\u00e9",
    "\U0001f511",
    ",",
)
_BEFORE = (
    "",
    "",
    "\uff3f",
    "\uff0d",
    "\uff58",
    "\u5bc6\u7801",
    "\uff0c",
    "\u3000",
    "Options: \uff3f",
)
_VALUE_CHARS = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _labelled_inputs(seed: int, count: int) -> list[str]:
    rnd = random.Random(seed)
    out = []
    for _ in range(count):
        label = rnd.choice(_LABELS)
        size = 40 if label == "aws_secret_access_key" else rnd.randint(8, 18)
        value = list(rnd.choice(_VALUE_CHARS) for _ in range(size))
        for _ in range(rnd.randint(0, 2)):
            value.insert(rnd.randint(0, len(value)), rnd.choice(_INSERTS))
        shape = rnd.choice(_LABEL_SHAPES)
        body = shape.format(k=label, K=label.upper(), f=label.replace("_", "-"), v="".join(value))
        out.append(rnd.choice(_BEFORE) + body + rnd.choice(("", " done", "\uff0c\u597d")))
    return out


def _as_written(text: str) -> list[tuple[int, int]]:
    """The value spans the known-key pass as written masks on its own."""
    rows = TABLE.known_keys
    found = known_key._scan(known_key._Reader(text, True), charsets.ascii_lower(text), rows)[0]
    return [(hit.start, hit.end) for _, hit in found]


def test_the_cjk_pass_only_adds_to_what_the_text_as_written_masks() -> None:
    masked = 0
    for text in _labelled_inputs(seed=42162, count=400):
        spans = _as_written(text)
        got = {
            (h.start, h.end) for h in known_key.detect_known_keys(text, TABLE.known_keys, True)[0]
        }
        assert set(spans) <= got, text
        merged = [(h.start, h.end) for h in redact_hits(text)[1] if h.tier == "secret"]
        for start, end in spans:
            assert any(s <= start and end <= e for s, e in merged), text
        out = redact(text)
        for start, end in spans:
            assert text[start:end] not in out, text
        masked += bool(spans)
    # The generator reaches the shapes the pass as written reads.
    assert masked >= 150


def test_the_full_width_fold_is_nfkc_one_character_for_one() -> None:
    wide = "".join(chr(c) for c in range(0xFF01, 0xFF5F)) + "\u3000"
    folded = charsets.fold_wide(wide)
    assert len(folded) == len(wide)
    assert all(unicodedata.normalize("NFKC", w) == a for w, a in zip(wide, folded, strict=True))
    assert charsets.fold_wide("密码：好") == "密码:好"


@pytest.mark.parametrize(
    ("ch", "breaks", "ends"),
    [
        ("好", True, True),
        ("，", True, True),
        ("：", True, True),
        ("ｐ", False, True),
        ("０", False, True),
        ("\u017f", False, False),
    ],
)
def test_a_full_width_letter_stays_inside_a_key_word_but_ends_a_stream_word(
    ch: str, breaks: bool, ends: bool
) -> None:
    assert charsets.breaks_word(ch) is breaks
    assert ends_word(ch) is ends


def _ends_word_on_main(c: str) -> bool:
    """The stream's word-end rule as main has it: a blank, or a non-ASCII character
    that is not default-ignorable and that no ASCII-folding reader maps onto ASCII."""
    if c.isspace():
        return True
    if c.isascii() or ord(c) in DEFAULT_IGNORABLE:
        return False
    folds = (charsets.ascii_lower(c), c.casefold(), c.lower(), c.upper())
    return not any(f.isascii() for f in folds)


def test_the_stream_word_end_rule_is_main_s_for_every_sampled_character() -> None:
    chars = [chr(c) for c in range(0xFF01, 0xFF5F)]
    chars += [chr(c) for c in range(0x3000, 0x3040)]
    chars += [chr(c) for c in range(0x4E00, 0x4E00 + 512)]
    chars += [chr(c) for c in range(0x3040, 0x3100)]
    chars += [chr(c) for c in range(0x00A0, 0x0100)]
    chars += ["\u017f", "\u212a", "\u200b", "\ufeff", "\u00ad", "\ufb01"]
    assert [c for c in chars if ends_word(c) is not _ends_word_on_main(c)] == []


_LONG_RUN = 1024


@pytest.mark.parametrize("quote", ['"', "'"])
def test_the_stream_matches_batch_after_a_quoted_value_and_full_width_letters(
    quote: str,
) -> None:
    text = f"password={quote}{PW}{quote}" + "ｘ" * _LONG_RUN + " 完"
    r = StreamRedactor()
    out = "".join(r.feed(c) + r.release_at_boundary() for c in text) + r.flush()
    assert out == redact(text)


@pytest.mark.parametrize("size", [1, 2, 3, 5, 64])
def test_the_stream_matches_batch_for_cjk_labels(size: int) -> None:
    text = (
        f"密钥aws_secret_access_key={SAMPLE_AWS_SECRET}好\npassword：{PW}，"
        f"然后\nｐａｓｓｗｏｒｄ＝{PW}。完"
    )
    r = StreamRedactor()
    out = "".join(r.feed(text[i : i + size]) for i in range(0, len(text), size)) + r.flush()
    assert out == redact(text)
    assert SAMPLE_AWS_SECRET not in out and PW not in out


_WIDE_SLASH_CLOSER = f"<password>{PW}<\uff0fpassword>"


@pytest.mark.parametrize("size", [1, 2, 3])
def test_the_stream_matches_batch_for_a_full_width_slash_closing_tag(size: int) -> None:
    text = _WIDE_SLASH_CLOSER
    r = StreamRedactor()
    out = "".join(r.feed(text[i : i + size]) for i in range(0, len(text), size)) + r.flush()
    assert out == redact(text)


def test_the_stream_matches_batch_for_a_full_width_slash_closing_tag_cut_after_the_value() -> None:
    text = _WIDE_SLASH_CLOSER
    cut = text.index(PW) + len(PW)
    r = StreamRedactor()
    assert r.feed(text[:cut]) + r.feed(text[cut:]) + r.flush() == redact(text)


def test_merging_the_cjk_pass_costs_linear_work() -> None:
    # Many labelled lines plus one full-width comma: both passes find a hit per line,
    # and the merge once compared every CJK hit with every hit as written.
    line = f"password={PW}\n"

    def build(n: int) -> str:
        return line * (n // len(line)) + "，"

    assert_linear_work(
        build, lambda text: known_key.detect_known_keys(text, TABLE.known_keys, True)
    )
