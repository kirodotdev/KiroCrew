"""Stream/whole parity and linear-time tests for the structured credential detector."""

from __future__ import annotations

import random
from collections.abc import Callable

import pytest
from redaction_structured_cases import (
    B64,
    PEM_BEGIN,
    PEM_BODY,
    PGP_BEGIN,
    assert_linear_work,
    aws_key_id,
    aws_secret,
    benign_cases,
    github_classic,
    jwt,
    secret_cases,
    session_token,
    synth,
)

from kiro_crew.security.redaction_structured import TAG, StreamRedactor, redact
from kiro_crew.security.redaction_structured.stream import CONTEXT

ALL_TEXTS = [(n, t) for n, t, _, _ in secret_cases()] + benign_cases()
ALL_TEXTS += [
    ("mixed", "\n".join(t for _, t in ALL_TEXTS[:12])),
    ("pair_across", f"{aws_secret()} " + "y" * 250 + f" {aws_key_id()} tail text"),
]


def run_stream(chunks: list[str]) -> tuple[str, list[tuple[int, int, str, str]]]:
    s = StreamRedactor()
    out = "".join(s.feed(c) for c in chunks) + s.flush()
    return out, s.spans


def split_at(text: str, cuts: list[int]) -> list[str]:
    bounds = [0, *sorted(cuts), len(text)]
    return [text[a:b] for a, b in zip(bounds, bounds[1:], strict=False)]


@pytest.mark.parametrize("text", [pytest.param(t, id=n) for n, t in ALL_TEXTS])
def test_chunked_equals_whole_at_every_split_point(text: str) -> None:
    whole = redact(text)
    for cut in range(len(text) + 1):
        assert run_stream([text[:cut], text[cut:]]) == whole, f"split at {cut}"


@pytest.mark.parametrize("text", [pytest.param(t, id=n) for n, t in ALL_TEXTS])
def test_chunked_equals_whole_at_random_multi_splits(text: str) -> None:
    whole = redact(text)
    rng = random.Random(len(text))
    for _ in range(25):
        cuts = [rng.randrange(len(text) + 1) for _ in range(rng.randrange(2, 12))]
        assert run_stream(split_at(text, cuts)) == whole, cuts


@pytest.mark.parametrize("text", [pytest.param(t, id=n) for n, t in ALL_TEXTS[:20]])
def test_one_character_feeds(text: str) -> None:
    assert run_stream(list(text)) == redact(text)


def _long_cases() -> list[tuple[str, str]]:
    sess = session_token(6000, seed=7)
    return [
        ("pem_past_max_block", f"a {PEM_BEGIN}\n" + "B" * 17000 + "\nafter " * 20),
        ("pgp_unterminated_long", f"{PGP_BEGIN}\n" + PEM_BODY * 300),
        ("session_run_fail_closed", f"AWS_SESSION_TOKEN={sess}=\nnext {github_classic()}"),
        ("session_run_padded", f'{{"SessionToken": "{sess}=="}} ok'),
        ("long_jose_run", "eyJ" + "A" * 20000 + " " + jwt()),
        ("long_benign", ("plain words, a://b and arn:aws:s3:::bkt " * 600)),
        ("context_reach", "x" * (CONTEXT * 3) + f" {aws_key_id()} {aws_secret()}"),
    ]


@pytest.mark.parametrize("text", [pytest.param(t, id=n) for n, t in _long_cases()])
def test_long_units_match_whole_text(text: str) -> None:
    whole = redact(text)
    step = max(1, len(text) // 300)
    for cut in range(0, len(text) + 1, step):
        assert run_stream([text[:cut], text[cut:]]) == whole, f"split at {cut}"
    rng = random.Random(7)
    for _ in range(10):
        cuts = [rng.randrange(len(text) + 1) for _ in range(rng.randrange(5, 40))]
        assert run_stream(split_at(text, cuts)) == whole
    assert run_stream([text[i : i + 997] for i in range(0, len(text), 997)]) == whole


def test_session_run_tail_is_discarded_not_resumed() -> None:
    sess = session_token(9000, seed=3)
    text = f"AWS_SESSION_TOKEN={sess}\nafter"
    s = StreamRedactor()
    pieces = [s.feed(text[i : i + 512]) for i in range(0, len(text), 512)]
    out = "".join(pieces) + s.flush()
    assert out == f"AWS_SESSION_TOKEN={TAG}\nafter"
    assert sess[-50:] not in out


def test_pending_text_stays_bounded() -> None:
    s = StreamRedactor()
    longest = 0
    rng = random.Random(1)
    corpus = [t for _, t in ALL_TEXTS] + ["word " * 50, synth(400, B64, 3), "eyJ" + "Q" * 3000]
    for _ in range(400):
        s.feed(rng.choice(corpus) + "\n")
        longest = max(longest, len(s._pending))  # noqa: SLF001
    s.flush()
    # The largest hold is a private-key block (16,000 plus its END line) or a JOSE run.
    assert longest < 20_000


@pytest.mark.parametrize("unit", ["x", "X", "*"])
def test_a_mask_run_fed_in_pieces_is_not_held_past_its_bound(unit: str) -> None:
    s = StreamRedactor()
    longest = 0
    for _ in range(64):
        s.feed(unit * 4096)
        longest = max(longest, len(s._pending))  # noqa: SLF001
    s.flush()
    assert longest < 20_000


# ── Linear work on adversarial input ──


def _adversarial() -> list[tuple[str, Callable[[int], str]]]:
    def fill(unit: str, lead: str = "") -> Callable[[int], str]:
        return lambda n: lead + (unit * (n // len(unit) + 1))[:n]

    return [
        ("ghp_near_miss", fill("ghp_" + "a" * 35 + " ")),
        ("ghp_glued", fill("ghp_")),
        ("eyj_dots", fill("eyJ.")),
        ("eyj_link_shape", fill("eyJ" + "A" * 96 + ".")),
        ("begin_headers", fill("-----" + "BEGIN ")),
        ("begin_typed", fill(PEM_BEGIN + " ")),
        ("key_ids_beside_secrets", fill(f"{aws_key_id()} {aws_secret()} ")),
        ("akia_eq", fill("AKIA=")),
        ("known_key_repeat", fill("aws_secret_access_key=")),
        ("known_key_json", fill('"SecretAccessKey": "')),
        ("arn_repeat", fill("arn:")),
        ("url_repeat", fill("https://")),
        ("url_nested_query", fill("a://h/?state=")),
        ("anthropic_near", fill("sk-ant-api0")),
        ("b64_run", fill("A")),
        ("session_run", fill("A", lead="SessionToken=")),
        ("mask_run_lower", fill("x")),
        ("mask_run_upper", fill("X")),
        ("star_run", fill("*")),
        ("plain", fill("lorem ipsum dolor ")),
        ("deep_quotes", fill('"\\"')),
    ]


def _streamed(text: str) -> None:
    s = StreamRedactor()
    for i in range(0, len(text), 4096):
        s.feed(text[i : i + 4096])
    s.flush()


@pytest.mark.parametrize("build", [pytest.param(b, id=n) for n, b in _adversarial()])
def test_adversarial_input_costs_linear_work_whole(build: Callable[[int], str]) -> None:
    assert_linear_work(build, redact)


@pytest.mark.parametrize("build", [pytest.param(b, id=n) for n, b in _adversarial()])
def test_adversarial_input_costs_linear_work_streamed(build: Callable[[int], str]) -> None:
    assert_linear_work(build, _streamed)
