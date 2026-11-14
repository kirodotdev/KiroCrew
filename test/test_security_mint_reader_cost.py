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
from denied_commands_security_helpers import _interpreter_work, _linear_work

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


def test_the_subshell_assignment_reader_is_linear_in_its_assignments():
    """Each ``$(`` close walk of an assignment value starts at its own opener.

    A walk restarted from offset 0 for every ``( b=$(:) )`` assignment made a
    20,000-character line take seconds in ``is_denied``. Asserted on interpreter work,
    never on a clock, as the rest of this file does.
    """
    from kiro_crew.security import shell_normalizer_subshell as _subshell

    def work_of(n: int) -> int:
        command = "(b=$(:));" * n
        return _interpreter_work(lambda: _subshell._subshell_bodies(command))

    sizes = (150, 300, 450)
    work = [work_of(n) for n in sizes]
    assert _linear_work(work), f"{work} instructions at sizes {sizes}"


def _shared_carry_line(words: int, subshells: int) -> str:
    """One large outer assignment, then *subshells* subshells that each read it."""
    value = " ".join(f"w{i}" for i in range(words))
    return f"A=$(echo {value}); " + "".join(f"(T=$(:{i}); $A); " for i in range(subshells))


def test_a_shared_outer_carry_is_resolved_once_per_walk(monkeypatch):
    """Every subshell reading the same outer assignment shares one resolution of it.

    Resolving the carry once per body made the gate cost subshells x carried size: 600
    subshells over a 1,500-word value took seconds against main's fraction of a second.
    Asserted on resolution count and interpreter work, never on a clock.
    """
    real = sn._subshell_carry
    calls: "list[int]" = []

    def spy(carried):
        calls.append(1)
        return real(carried)

    monkeypatch.setattr(sn, "_subshell_carry", spy)
    is_denied(_shared_carry_line(200, 40))
    assert len(calls) == 1, f"{len(calls)} carry resolutions for 40 subshells"
    monkeypatch.setattr(sn, "_subshell_carry", real)
    is_denied(_shared_carry_line(10, 2))  # warm the import-time work out of the measure
    # Quadrupling the carried value grows the work by about 2.1x on main and 2.3x here (one
    # resolution plus the line's own reading) and by about 2.9x when every body resolves it.
    work = [_interpreter_work(lambda w=w: is_denied(_shared_carry_line(w, 20))) for w in (100, 400)]
    assert work[1] < 2.6 * work[0], f"{work} instructions at 100 and 400 carried words"


@pytest.mark.parametrize(
    ("command", "refused"),
    [
        ("V=1; ( X=$(true); echo $V )", False),
        ("DIR=/tmp/x; ( RES=$(ls $DIR); echo $RES )", False),
        ('( T=$(: "\'"; echo token); kirocrew $T )', True),
        ("V=kirocrew; ( $V token )", True),
        ("V=kirocrew; ( $V status ); ( $V token )", True),
        ("V=kirocrew; W=token; ( $V $W ); ( $V status )", True),
        ("V=kirocrew; ( X=$(true); $V status ); ( Y=$(true); $V status )", False),
        (
            'P=$(: "\'"; echo token; : kirocrew); ( Q=$(true); kirocrew $P ); '
            "( R=$(true); kirocrew status )",
            True,
        ),
    ],
)
def test_a_shared_carry_keeps_each_subshell_verdict(command, refused):
    # Verdicts measured on the head before the carry was shared, so sharing changes none.
    assert bool(is_denied(command)) is refused, command


def _split_chain(n: int, referenced: int) -> str:
    """*n* split substitution assignments, the first *referenced* of them read by ``echo``."""
    head = "".join(f"T{i}=$(echo x); " for i in range(n))
    return head + (
        "echo " + " ".join(f"$T{i}" for i in range(referenced)) if referenced else "true"
    )


def test_unreferenced_split_assignments_build_no_rejoin_frame(monkeypatch):
    # A standalone assignment no later word reads cannot reach the guard, so the rejoined
    # frame (a second resolution of the whole line) is never built for it.
    from kiro_crew.security import shell_normalizer as _sn

    real = _sn._self_tokens
    builds = [0]

    def counting(text: str, **kwargs: object) -> "list[str]":
        builds[0] += bool(kwargs.get("rejoin"))
        return real(text, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(_sn, "_self_tokens", counting)
    assert is_denied(_split_chain(40, 0)) is None
    assert builds[0] == 0


#: A small budget for the pins below, so each exercises the rule on a few assignments.
#: The rule is the same at any size; the shipped value is pinned on its own.
_SMALL_BUDGET = 8


def test_the_shipped_rejoin_budget_is_512_assignments():
    from kiro_crew.security import shell_normalizer as _sn

    assert _sn._REJOIN_WORK_BUDGET == 512


def test_the_rejoin_work_budget_fails_closed(monkeypatch):
    # Referenced rejoined words resolve while the line's assignments stay within the
    # budget; one more and the rejoined resolution is refused unresolved.
    from kiro_crew.security import shell_normalizer as _sn

    monkeypatch.setattr(_sn, "_REJOIN_WORK_BUDGET", _SMALL_BUDGET)
    assert is_denied(_split_chain(_SMALL_BUDGET, _SMALL_BUDGET)) is None
    assert is_denied(_split_chain(_SMALL_BUDGET + 1, 1)) is not None


@pytest.mark.parametrize(
    "cmd",
    [
        'T=$(echo "ki" "rocrew"); $T token',
        'T=$(echo "to" "ken"); kirocrew $T',
        'T=$(printf "%s" tok en); kirocrew $T',
    ],
)
def test_a_computed_verb_keeps_its_verdict(cmd):
    # The body computes the name or verb without naming it: the documented computed-verb
    # residual, allowed on main and before the reach gate alike. The gate must not move it.
    assert is_denied(cmd) is None, cmd


def _split_payload_line(top: int, payload: int) -> str:
    """*top* referenced split assignments, then *payload* more inside a ``bash -c`` script."""

    def chain(n: int, prefix: str) -> str:
        head = "".join(f"{prefix}{i}=$(echo x); " for i in range(n))
        return head + "echo " + " ".join(f"${prefix}{i}" for i in range(n))

    return chain(top, "T") + "; bash -c '" + chain(payload, "U") + "'"


def _budget_reads(monkeypatch, command: str) -> "tuple[bool, list[int]]":
    """``(refused by the rejoin budget, assignments charged per reaching frame)`` for one
    payload walk of *command*: counts, not a clock."""
    from kiro_crew.security import shell_normalizer as _sn

    real = _sn._assignment_count
    charged: "list[int]" = []

    def counting(tokens: "list[str]") -> int:
        charged.append(real(tokens))
        return charged[-1]

    monkeypatch.setattr(_sn, "_assignment_count", counting)
    frames = _sn._shell_payload_walk_uncached(command.lower())
    return any(tokens == ["kirocrew", "token"] for _, tokens in frames), charged  # brand-ok


def test_the_rejoin_budget_is_spent_across_every_frame_of_the_line(monkeypatch):
    # Each frame here holds under the budget, so a per-frame budget allowed it; the
    # budget is the line's total, and the two frames together pass it.
    from kiro_crew.security import shell_normalizer as _sn

    monkeypatch.setattr(_sn, "_REJOIN_WORK_BUDGET", _SMALL_BUDGET)
    refused, charged = _budget_reads(monkeypatch, _split_payload_line(5, 5))
    assert refused, charged
    assert len(charged) == 2 and all(n <= _SMALL_BUDGET for n in charged), charged
    assert sum(charged) > _SMALL_BUDGET, charged
    assert is_denied(_split_payload_line(5, 5)) is not None


@pytest.mark.parametrize(("top", "payload"), [(3, 3), (4, 4)])
def test_a_split_line_within_the_rejoin_budget_resolves(monkeypatch, top, payload):
    # (4, 4) charges exactly the budget: the comparison is strictly greater-than.
    from kiro_crew.security import shell_normalizer as _sn

    monkeypatch.setattr(_sn, "_REJOIN_WORK_BUDGET", _SMALL_BUDGET)
    refused, charged = _budget_reads(monkeypatch, _split_payload_line(top, payload))
    assert not refused, charged
    assert len(charged) == 2 and sum(charged) <= _SMALL_BUDGET, charged
    assert is_denied(_split_payload_line(top, payload)) is None
