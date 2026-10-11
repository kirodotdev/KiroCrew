"""The credential scan stays linear on a long ``eyJ`` run and finds every match ``search`` finds.

On a file of ``eyJ`` repeats, ``_CREDENTIAL_PATTERNS.search`` re-walks the run from every
``eyJ`` (the JWT branch's greedy segment backtracks to the run end each time), so one
batch ``redact()`` of a capped dashboard file read would spend minutes holding the GIL.
``_credential_search`` finds the JWT-branch start separately and jumps a failed run
whole. These tests pin both halves: the match sequence is identical to the plain
``search`` loop, and a long run costs one branch attempt.
"""

from __future__ import annotations

import base64
import json
import random
import re
from typing import Any

import pytest

from kiro_crew.credential_patterns import JWT_MULTI_SEGMENT
from kiro_crew.security import redact, redaction


def _b64(obj: Any) -> str:
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


_JWS = f"{_b64({'alg': 'HS256', 'typ': 'JWT'})}.{_b64({'sub': 'u1'})}.{_b64(b'sig' * 11)}"
_JWE_DIR = f"{_b64({'alg': 'dir', 'enc': 'A256GCM'})}..{_b64(b'iv' * 6)}.{_b64(b'ct' * 9)}.tag"
_LINK = f"eyJ{'A' * 120}.{'B' * 43}"
_AWS = "AKIAIOSFODNN7EXAMPLE"


def _search_loop(text: str) -> list[tuple[int, int]]:
    """The reference scan: one ``search`` per step from the last match."""
    spans, pos = [], 0
    while (m := redaction._CREDENTIAL_PATTERNS.search(text, pos)) is not None:
        spans.append(m.span())
        pos = max(m.end(), m.start() + 1)
    return spans


def _linear_loop(text: str) -> list[tuple[int, int]]:
    spans, pos, starts = [], 0, [-1, -1, -1]
    while (m := redaction._credential_search(text, pos, starts)) is not None:
        spans.append(m.span())
        pos = max(m.end(), m.start() + 1)
    return spans


_PIECES = (
    "eyJ", ".", "..", "a", "Z9", "-", "_", " ", "=", '"', "\n", "e", "yJ", "k",
    _AWS, "ghp_" + "x" * 36, "Authorization: Bearer ", "://u:p@", "sk-ant-",
    _b64({"alg": "none"}), "1234567", "9", ":", "0" * 6 + ":" + "t" * 30, "xoxb-",
)  # fmt: skip


def test_match_sequence_equals_the_plain_search_loop_on_random_text() -> None:
    rng = random.Random(17553)
    for _ in range(20_000):
        text = "".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 40)))
        assert _linear_loop(text) == _search_loop(text), text


@pytest.mark.parametrize(
    "text",
    [
        _JWS,
        f"token {_JWS} end.",
        _JWE_DIR,
        _LINK,
        f"x{_JWS}",
        "eyJ" * 400 + ".a.b",
        "eyJ" * 400 + " " + _JWS,
        "eyJ" * 400 + f" {_AWS}",
        f"{_AWS} " + "eyJ" * 400,
        ("eyJ" * 50 + ".") * 30 + _JWS,
    ],
    ids=lambda text: f"len{len(text)}",
)
def test_every_credential_shape_is_still_redacted(text: str) -> None:
    assert _linear_loop(text) == _search_loop(text)
    out = redact(text)
    for secret in (_JWS, _JWE_DIR, _LINK, _AWS):
        assert secret not in out
    assert "[REDACTED" in out


def test_the_branch_check_agrees_with_the_branch_pattern_at_every_eyj() -> None:
    pattern = re.compile(JWT_MULTI_SEGMENT)
    rng = random.Random(1755)
    for _ in range(5_000):
        text = "".join(rng.choice(("eyJ", ".", "a", "-", " ", "e")) for _ in range(24))
        i = text.find("eyJ")
        while i != -1:
            expected = pattern.match(text, i) is not None
            got = redaction._jwt_branch_matches_at(text, i, redaction._segment_run_end(text, i))
            assert got is expected, (text, i)
            i = text.find("eyJ", i + 1)


def test_a_long_eyj_run_costs_one_branch_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    real = redaction._jwt_branch_matches_at

    def counting(text: str, i: int, run_end: int) -> bool:
        nonlocal calls
        calls += 1
        return real(text, i, run_end)

    monkeypatch.setattr(redaction, "_jwt_branch_matches_at", counting)
    assert list(redaction._credential_matches("eyJ" * 20_000)) == []
    assert calls == 1


def test_the_telegram_guard_rewrote_its_branch() -> None:
    starts = redaction._CREDENTIAL_STARTS_SANS_JWT.pattern
    assert starts != redaction._CREDENTIAL_PATTERNS_SANS_JWT.pattern
    assert f"(?<![0-9]){redaction._TELEGRAM_BRANCH}" in starts


def test_a_scan_resuming_inside_a_digit_run_finds_the_telegram_token() -> None:
    text = "123456789:" + "t" * 30
    for pos in range(len(text) + 1):
        expected = redaction._CREDENTIAL_PATTERNS_SANS_JWT.search(text, pos)
        got = redaction._sans_jwt_start(text, pos)
        assert got == (len(text) + 1 if expected is None else expected.start()), pos


def test_overlapping_matches_do_not_rewalk_a_matched_run(monkeypatch: pytest.MonkeyPatch) -> None:
    text = ("xoxb-eyJ" + "a" * 10 + "_") * 2_000 + ".a.b"
    walked = 0
    real = redaction._segment_run_end

    def counting(text: str, pos: int) -> int:
        nonlocal walked
        end = real(text, pos)
        walked += end - pos
        return end

    monkeypatch.setattr(redaction, "_segment_run_end", counting)
    assert _linear_loop(text) == _search_loop(text)
    assert walked <= 2 * len(text), walked
