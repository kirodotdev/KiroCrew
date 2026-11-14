"""The credential-mint reader's cost on never-closing command substitutions.

``is_denied`` is the synchronous PreToolUse gate. A command of N assignments whose
substitutions never close (the ``#`` comment swallows each ``)``) becomes N nested
frames, and each frame re-read every suffix of its own tokens for a mint verb, so the
mint reader's work grew with the cube of the command length.

The work is counted as the characters handed to ``_substitution_bodies`` instead of
being timed: the count is a pure function of the input, so the bound holds the same on
a loaded host. Measured on the reader before the memo: 61870 characters at 30 repeats
and 425225 at 60 (x6.9 for 2x the length); with it, 10395 and 38775 (x3.7).
"""

from __future__ import annotations

import pytest

from kiro_crew.security import call_views, is_denied
from kiro_crew.security import shell_normalizer as sn


def _never_closing(reps: int) -> str:
    return "a=$(: #); " * reps + "kirocrew doctor"


@pytest.mark.parametrize("reps", [30, 60])
def test_never_closing_substitutions_are_read_in_quadratic_work(
    monkeypatch: pytest.MonkeyPatch, reps: int
) -> None:
    command = _never_closing(reps)
    seen = [0]
    real = sn._substitution_bodies

    def counting(text: str, **kwargs: bool) -> "list[str]":
        seen[0] += len(text)
        return real(text, **kwargs)

    monkeypatch.setattr(sn, "_substitution_bodies", counting)
    is_denied(command)
    # A quarter of length squared sits between the memoized reader (about a tenth) and
    # the cubic one (0.6 at 30 repeats, 1.1 at 60, and growing with the length).
    assert seen[0] <= len(command) ** 2 // 4, (reps, len(command), seen[0])


# Each command with the verdict the memoized reader must give, the same one the reader
# gives without the memo, refusals included: True is a refusal.
_VERDICTS = (
    ("a=$(: #); a=$(: #); kirocrew doctor", False),
    ("F=$(: # )\nprintf kirocrew); $F token", True),
    ("T=$(: ${x:-(}; printf kirocre[w]); $T token", True),
    ("T=$(cat <<E\n(\nE\nprintf kirocre[w]); $T token", True),
    ("T=$(printf token); kirocrew $T", True),
    ("T=$(echo hi); kirocrew $T", False),
    ("a=$(: #); T=$(printf token); kirocrew $T", True),
    ("a=$(: #); kirocrew doctor", False),
    ("kirocrew doctor", False),
)


@pytest.mark.parametrize(("command", "refused"), _VERDICTS)
def test_the_memo_leaves_the_verdict_where_it_was(command: str, refused: bool) -> None:
    assert (is_denied(command) is not None) is refused


def _reference(tokens: "list[str]", idx: int) -> bool:
    """The reader as it stood before the memo: every body of the suffix, read afresh."""
    for body in sn._substitution_bodies(" ".join(tokens[idx:])):
        if any(sn._is_mint_verb(word) for word in body.split()):
            return True
    return False


def _every_start(command: str) -> "list[bool]":
    tokens = sn._self_tokens(command, resolve=False)
    return [sn._mint_verb_in_substitution(tokens, idx) for idx in range(len(tokens))]


@pytest.mark.parametrize("command", [command for command, _ in _VERDICTS])
def test_the_memo_answers_every_suffix_as_the_unmemoized_reader_does(command: str) -> None:
    tokens = sn._self_tokens(command, resolve=False)
    expected = [_reference(tokens, idx) for idx in range(len(tokens))]
    assert _every_start(command) == expected
    assert call_views.sharing_views(_every_start)(command) == expected
