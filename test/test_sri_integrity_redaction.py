"""SRI integrity digests survive the bare-secret pass; a key beside or posing as one does not."""

from __future__ import annotations

import base64
import hashlib
import random

import pytest

from kiro_crew.security import redact, redact_credentials
from kiro_crew.security import redaction as _redaction

# The canonical AWS documentation example secret key, never a live credential.
KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
TAG = "[REDACTED: credential]"

# Public integrity values from hermeticbuild/hermetic-llvm's MODULE.bazel; six of
# the seven fire the bare-secret heuristic when the digest exemption is absent.
BAZEL_VALUES = [
    "sha256-qOhU+p8/uurCfXME4pfxZg1xN0ATid61fPeYPzPBb+8=",
    "sha256-/0VWYL7gcadEcVqWZWZvopwHaBV509gVlZqe7FpVZCQ=",
    "sha256-A7wMA9Q5zvhQdLvNQl+dXTptO0Z5Z6zSHaO6bf7q+mc=",
    "sha256-rWzpD/lAEGmdKVSLPCseUsieuBFD4hmRVdmlw4ABtSs=",
    "sha256-q7bwGW3nRigiGuXj/2kIZMi/LFCHZpVbMD/kRdllLCg=",
    "sha256-OAZfeifniTQmKit60qHpZanOLLIJVyYEiMlpN5Q7Fjw=",
    "sha256-XHk51jB2RHDmeUI6JiYBAd5hqfI4kN+wpjTqE17WTqA=",
]
FIRING_VALUES = [v for v in BAZEL_VALUES if _redaction._text_contains_bare_secret(v)]


def _sri(algorithm: str, data: bytes) -> str:
    digest = getattr(hashlib, algorithm)(data).digest()
    return f"{algorithm}-{base64.b64encode(digest).decode()}"


def _key_shaped_run(rng: random.Random, length: int) -> str:
    """A random run of *length* base64 characters the heuristic judges a key on its own."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    while True:
        run = "".join(rng.choice(alphabet) for _ in range(length))
        if _redaction._text_contains_bare_secret(run):
            return run


def test_the_fixtures_are_what_they_claim() -> None:
    assert len(KEY) == 40
    assert _redaction._looks_like_secret_key(KEY)
    assert len(FIRING_VALUES) == 6
    for value in BAZEL_VALUES:
        digest = value.removeprefix("sha256-")
        assert len(base64.b64decode(digest, validate=True)) == 32


@pytest.mark.parametrize("value", BAZEL_VALUES)
@pytest.mark.parametrize(
    "template",
    [
        "{}",
        '    integrity = "{}",',
        '<script src="x.js" integrity="{}" crossorigin="anonymous"></script>',
        '"integrity": "{}",',
        "integrity={} ok",
        "`{}`",
    ],
)
def test_an_sri_value_survives(value: str, template: str) -> None:
    text = template.format(value)
    assert redact_credentials(text) == (text, [])
    assert redact(text) == text


@pytest.mark.parametrize("algorithm", ["sha256", "sha384", "sha512"])
def test_every_digest_of_each_algorithm_survives(algorithm: str) -> None:
    rng = random.Random(17818)
    for _ in range(200):
        text = f"integrity={_sri(algorithm, rng.randbytes(32))} ok"
        assert redact_credentials(text) == (text, [])


def test_without_the_exemption_most_digests_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exemption is what keeps them: the pass fires on the same values without it."""
    monkeypatch.setattr(_redaction, "_sri_digest_spans", lambda text: frozenset())
    redacted = sum(redact_credentials(v)[0] != v for v in BAZEL_VALUES)
    assert redacted == 6


def _probes() -> list[tuple[str, str]]:
    rng = random.Random(17818)
    run43 = _key_shaped_run(rng, 43)
    run64 = _key_shaped_run(rng, 64)
    run86 = _key_shaped_run(rng, 86)
    value = BAZEL_VALUES[0]
    return [
        # A 40-char key labelled as a digest: wrong length for every algorithm.
        (f"sha256-{KEY}", KEY),
        (f"sha256-{KEY}=", KEY),
        (f"sha256-{KEY}==", KEY),
        (f"sha384-{KEY}", KEY),
        # The right length with the wrong padding, or a character after it.
        (f"sha256-{run43}", run43),
        (f"sha256-{run43}==", run43),
        (f"sha256-{run43}=X", run43),
        (f"sha256-{run43}=/", run43),
        (f"sha384-{run64}=", run64),
        (f"sha512-{run86}=", run86),
        (f"sha512-{run86}", run86),
        # The right length under another algorithm's name.
        (f"sha256-{run64}", run64),
        (f"sha384-{run43}=", run43),
        # A name glued to a run before it, written in upper case, or not an SRI algorithm.
        (f"X{value}", value.removeprefix("sha256-")),
        (f"SHA256-{value.removeprefix('sha256-')}", value.removeprefix("sha256-")),
        (f"sha1-{run43}=", run43),
        (f"md5-{run43}=", run43),
        # A key beside a digest, in the same line or a later field.
        (f"{value} {KEY}", KEY),
        (f'{{"integrity":"{value}","secret":"{KEY}"}}', KEY),
        (f"{KEY} {value}", KEY),
    ]


@pytest.mark.parametrize("text,secret", _probes())
def test_a_key_beside_or_posing_as_a_digest_is_still_redacted(text: str, secret: str) -> None:
    out, warnings = redact_credentials(text)
    assert secret not in out
    assert TAG in out
    assert warnings


def test_the_digest_survives_beside_a_redacted_key() -> None:
    value = BAZEL_VALUES[0]
    assert redact_credentials(f"{value} {KEY}") == (
        f"{value} {TAG}",
        ["Redacted bare secret key (40 chars)"],
    )


def test_the_accepted_residual_is_a_digest_length_secret_labelled_as_one() -> None:
    """A 43-char secret a writer labels `sha256-<secret>=` is byte-identical to a digest."""
    run43 = _key_shaped_run(random.Random(17818), 43)
    text = f"sha256-{run43}="
    assert redact_credentials(text) == (text, [])


def test_a_lockfile_of_many_digests_costs_one_probe_per_run() -> None:
    """Every digest in a lockfile fires the heuristic; the lookup must not scan them all per run."""
    calls = 0
    real = _redaction._sri_digest_spans

    class Counting(frozenset[tuple[int, int]]):
        def __contains__(self, item: object) -> bool:
            nonlocal calls
            calls += 1
            return super().__contains__(item)

    def counting(text: str) -> frozenset[tuple[int, int]]:
        return Counting(real(text))

    text = "\n".join(f'"integrity": "{BAZEL_VALUES[i % 7]}",' for i in range(2_000))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_redaction, "_sri_digest_spans", counting)
        assert redact_credentials(text) == (text, [])
    assert calls <= 2_000


def test_the_exemption_leaves_the_exfil_scan_unchanged() -> None:
    """Only pass 3 of the redactor consults the shape; the URL scan judges runs as before."""
    assert _redaction._text_contains_bare_secret(FIRING_VALUES[0])


def _flush_streamed(deltas: list[str]) -> str:
    """Stream *deltas* through the per-delta removers as the run loop does, then flush."""
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("sri-flush")
    text = ""
    for delta in deltas:
        shown, removed = chat_runner._redact_delta(delta)
        chat_runner._accumulate_segment_raw(slot, delta, shown, removed)
        text += shown
    return chat_runner._redact_segment(slot, text)[0]


def test_a_streamed_sri_value_in_one_delta_is_saved_whole() -> None:
    line = f'Run this:\n    integrity = "{FIRING_VALUES[0]}",\nthen build.'
    assert _flush_streamed([line[:5], line[5:]]) == line


@pytest.mark.parametrize("value", FIRING_VALUES)
def test_a_delta_split_sri_value_saves_the_reply_not_the_withheld_notice(value: str) -> None:
    from kiro_crew.dashboard.chat_turn.segment_flush import _SEGMENT_UNCHECKED_TEXT

    head, digest = 'Run this:\n    integrity = "sha256-', value.removeprefix("sha256-")
    tail = '",\nthen build.'
    saved = _flush_streamed([head, digest, tail])
    # The digest's own delta could not see the SRI shape and redacted it, so the
    # save falls back to main's verdict on the digest instead of withholding all.
    assert saved != _SEGMENT_UNCHECKED_TEXT
    assert saved.startswith("Run this:") and saved.endswith("then build.")
    assert digest not in saved and TAG in saved


def test_a_delta_split_sri_value_beside_a_key_keeps_the_key_redacted() -> None:
    value = FIRING_VALUES[1]
    digest = value.removeprefix("sha256-")
    saved = _flush_streamed([f"key {KEY} and integrity sha256-", digest, "= ok"])
    assert KEY not in saved and digest not in saved
    assert saved.startswith("key ") and saved.endswith(" ok")
