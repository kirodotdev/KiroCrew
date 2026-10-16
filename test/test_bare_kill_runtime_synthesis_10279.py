"""A bare kill's substitution body must not assemble the protected name at runtime.

The bare-kill window searched each substitution body raw, with parameter
defaults resolved, and per word of the tokenized view -- all static.  A body
whose ``pgrep``/``pidof`` target is built at runtime from constant-output
substitutions (``"$(printf kiro)$(printf crew)"``) or brace expansion
(``kiro{,}crew``, ``kiro{c..c}rew``) slipped every member.  The window now
folds constant ``printf``/``echo`` output and expands braces per word before
searching.

Posture: these forms are DENIED because the floor can compute them exactly.
An unresolvable operand does NOT fail closed: a pattern variable bound outside
the line, a lookup naming a different product, and a brace product past the
fan-out cap stay allowed and are pinned here as deliberate residuals (the
reported forms all fit within the cap, and a wider product names no one live
process).

The protected verb and name are assembled at runtime so an agent shell can
read and grep this file without tripping the very rule under test.
"""

from __future__ import annotations

import re
from typing import Callable

import pytest

from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import is_denied

_K = "k" + "ill"
_PG = "pg" + "rep"
_NAME = "kiro" + "crew"
_HEAD = _NAME[:4]
_TAIL = _NAME[4:]
_ESAC_PREFIX = "case x in x) :;; esac; "
# Nine single-literal printf substitutions nested innermost-first around the head.
_NEST9 = "$(printf " * 9 + _HEAD + ")" * 9


def _floor(cmd: str) -> bool:
    """The argv floor's own verdict, independent of the regex tier."""
    return _argv_floor._is_self_kill(cmd.lower())


_SYNTHESIZED_BODIES = [
    # nested constant-output substitutions
    f'{_PG} -f "$(printf {_HEAD})$(printf {_TAIL})"',
    f'{_PG} -f "$(echo -n {_HEAD})$(echo -n {_TAIL})"',
    f"{_PG} -f `printf {_HEAD}`{_TAIL}",
    f'{_PG} -f "$(printf "$(printf {_HEAD})"){_TAIL}"',
    # nine nested single-literal substitutions fold to a fixpoint, not one short
    f'{_PG} -f "{_NEST9}{_TAIL}"',
    # a computed value carried through a same-line assignment
    f'x=$(printf {_HEAD}); {_PG} -f "${{x}}{_TAIL}"',
    # brace expansion: alternation and a one-character sequence
    f"{_PG} -f {_HEAD}{{,}}{_TAIL}",
    f"{_PG} -f {_HEAD}{{{_TAIL[0]}..{_TAIL[0]}}}{_TAIL[1:]}",
    f"pidof {_HEAD}{{,}}{_TAIL}",
    # an argument shaped like name=value is NOT an assignment, so its braces
    # expand (bash runs `pgrep -f x=.*|kirocrew`, whose regex names the gateway)
    f"{_PG} -f x='.*|'{_HEAD}{{{_TAIL[0]}..{_TAIL[0]}}}{_TAIL[1:]}",
    # a leading assignment does not stop a later command argument from expanding
    f"TMPDIR=/t {_PG} -f {_HEAD}{{,}}{_TAIL}",
    # a parameter word nested in a brace group does not stop the group
    # expanding (bash brace-expands before parameter expansion)
    f"pidof -x {_HEAD}{{${{x:-}},}}{_TAIL}",
    # identical brace alternatives collapse rather than overflow the fan-out cap
    f"pidof -x {_HEAD}{{{_TAIL[0]}..{_TAIL[0]}}}{_TAIL[1:]}" + "{,}" * 9,
    # an UNPROVEN backtick in a parameter word must not swallow the group's close
    f"pidof -x {_HEAD}{{${{x:-'`'}},{_TAIL[0]}}}{_TAIL[1:]}",
    # same-line variable concatenation (denied through the tokenized view)
    f'a={_HEAD}; b={_TAIL}; {_PG} -f "$a$b"',
    # a quote inside a comment is not syntax and must not mask the next line
    f'# it\'s\n{_PG} -f "$(printf {_HEAD})$(printf {_TAIL})"',
    # a quoted, escaped or unterminated ``${`` does not open a parameter expansion
    f"pidof '${{' {_HEAD}{{,}}{_TAIL}",
    f"pidof \\${{ {_HEAD}{{,}}{_TAIL}",
    f": '${{'; {_PG} -f {_HEAD}{{{_TAIL[0]}..{_TAIL[0]}}}{_TAIL[1:]}",
    # an ANSI-C escaped quote does not close its string
    f": $'\\''; {_PG} -f {_HEAD}{{{_TAIL[0]}..{_TAIL[0]}}}{_TAIL[1:]}",
    # a command substitution inside a parameter default runs, braces and all
    f'echo "${{x:-$({_PG} -f {_HEAD}{{{_TAIL[0]}..{_TAIL[0]}}}{_TAIL[1:]})}}"',
    # a double-quoted close-paren and an escaped brace are data, not delimiters
    f': "$( : ")")"; {_PG} -f {_HEAD}{{{_TAIL[0]}..{_TAIL[0]}}}{_TAIL[1:]}',
    f": ${{x:-\\{{}}; {_PG} -f {_HEAD}{{{_TAIL[0]}..{_TAIL[0]}}}{_TAIL[1:]}; : \\}}",
    # only a substitution's real closer ends its scope: not a subshell's, not a
    # case pattern's, and a ``$$`` (the PID parameter) opens nothing
    f'echo "$( ( : ); {_PG} -f {_HEAD}{{{_TAIL[0]}..{_TAIL[0]}}}{_TAIL[1:]})"',
    f'echo "$(case a in a) printf \'"\';; esac)"; {_PG} -f {_HEAD}{{,}}{_TAIL}',
    f'echo "$$( \'"\' )\'")" ; {_PG} -f {_NAME}',
]


@pytest.mark.parametrize("prefix", ["", _ESAC_PREFIX], ids=["plain", "esac-tail"])
@pytest.mark.parametrize("body", _SYNTHESIZED_BODIES)
def test_runtime_synthesized_target_is_denied(prefix: str, body: str) -> None:
    cmd = f"{_K} $({prefix}{body})"
    assert _floor(cmd), cmd
    assert is_denied(cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # an unresolvable pattern variable is NOT failed closed
        f'{_K} $({_PG} -f "$pattern")',
        f'{_K} $({_PG} -f "${{pattern}}")',
        # a lookup naming a different product
        f"{_K} $({_PG} -f otherproduct)",
        f"{_K} $({_PG} -f other{{,}}product)",
        f'{_K} $({_PG} -f "$(printf other)$(printf product)")',
        # a constant substitution unrelated to the target
        f'{_K} $({_PG} -f "$(printf worker)")',
        # output words bash keeps apart are not joined
        f'{_K} $({_PG} -f "$(echo {_HEAD} {_TAIL}-worker)")',
        # a format conversion is not modelled -- residual, left unfolded
        f'{_K} $({_PG} -f "$(printf %s%s {_HEAD} {_TAIL})")',
        # a printf escape in the operand is a residual (not decoded, no crash)
        f"{_K} $({_PG} -f \"$(printf '{_HEAD}\\n{_TAIL}')\")",
        f"{_K} $({_PG} -f \"$(printf '\\8')\")",
        # an unknown printf escape is kept, not decoded or crashed on
        f"{_K} $({_PG} -f \"$(printf '\\8')\")",
        # output carrying syntax is left unfolded rather than altered
        f"{_K} $({_PG} -f \"$(printf '{_HEAD};{_TAIL}')\")",
        # a numeric range past the cap cannot add letters
        f"{_K} $(lsof -ti tcp:{{3000..3999}})",
        f"{_K} $({_PG} -f {_HEAD}-cli-server-worker-{{1..300}})",
        # a 300-way alternation of long replica names, no name in any of them
        f"{_K} $({_PG} -f svc-{{" + ",".join(f"worker-alpha-{i:03d}x" for i in range(300)) + "})",
        f"{_K} $({_PG} -f shard-{{a..z}}{{0..9}})",
        # a nested group whose own product overflows the cap, no name in it
        f"{_K} $({_PG} -f 'shard-{{{{a..j}}{{a..z}},main}}')",
        f"{_K} $({_PG} -f shard-{{{{a..z}}{{a..z}},main,canary}})",
        # an over-cap brace product is a residual even when a member could spell
        # the name: the reported forms all fit within the cap, and a
        # product this wide names no one live process
        f"{_K} $({_PG} -f " + "".join("{" + ch + ",x}" for ch in _NAME) + "{1,2})",
        f"{_K} $({_PG} -f {_HEAD}" + "{a..z}" * 4 + ")",
        # a trailing (non-leading) echo operand is not an option -> residual
        f'{_K} $({_PG} -f "$(echo {_HEAD} -n)crew")',
        # quoting makes braces and substitutions LITERAL -- bash does not expand
        # them, so neither does the floor (these name no live process)
        f"{_K} $({_PG} -f '{_HEAD}{{c..c}}{_TAIL}')",
        f"{_K} $({_PG} -f '$(printf {_HEAD}){_TAIL}')",
        f"{_K} $({_PG} -f \"$(printf '{_HEAD};')crew\")",
        # a substitution nested inside another's single-quoted arg is inert
        f"{_K} $({_PG} -f \"worker|$(printf '$(printf {_HEAD})'){_TAIL}\")",
        f"{_K} $({_PG} -f \"worker|`printf '$(printf {_HEAD})'`{_TAIL}\")",
        # an escaped substitution and a parameter default's braces are literal
        f'{_K} $({_PG} -f "worker|\\$(printf {_HEAD}){_TAIL}")',
        # quotes that are DATA inside the operand are kept, so no name is invented
        f'{_K} $({_PG} -f "$(printf "\'{_HEAD}\'"){_TAIL}")',
        # an assignment stores its braces; a later reference is never brace-expanded
        f'{_K} $(pattern={_HEAD}{{3,}}{_TAIL}; {_PG} -f "$pattern")',
        f'{_K} $({_PG} -f "worker|{_HEAD}${{x:-{{{_TAIL[0]}..{_TAIL[0]}}}}}{_TAIL[1:]}")',
        f"{_K} $({_PG} -f {_HEAD}-cli-server-worker-{{a..z}}{{a..z}})",
        # the name built in a DIFFERENT command than the kill
        f"{_K} 4242; echo {_HEAD}{{,}}{_TAIL}",
    ],
)
def test_false_positive_controls_stay_allowed(cmd: str) -> None:
    assert not _floor(cmd), cmd


def test_quoted_substitution_close_paren_does_not_end_the_kill_window() -> None:
    # ``"$( : ")")"`` is one word: its inner substitution has its own quoting,
    # so the quoted ``)`` closes nothing and the lookup after it is in the window.
    cmd = f'{_K} $(: "$( : ")")"; {_PG} -f {_NAME})'
    assert _floor(cmd), cmd
    assert is_denied(cmd), cmd


@pytest.mark.parametrize("slashes", [2, 4], ids=["two", "four"])
def test_even_backslash_run_before_a_quoted_substitution_keeps_it_open(
    slashes: int,
) -> None:
    # An even run of backslashes escapes only itself, so the ``$(`` after it
    # still opens a substitution and the lookup after it stays in the window.
    lead = "\\" * slashes
    for target in (_NAME, f'"$(printf {_HEAD})$(printf {_TAIL})"'):
        cmd = f'{_K} $( : "{lead}$( : ")")"; {_PG} -f {target})'
        assert _floor(cmd), cmd
        assert is_denied(cmd), cmd


def _record_resolver_calls(monkeypatch: pytest.MonkeyPatch) -> "list[int]":
    """Replace the eager assignment resolver with a recorder; return its call log."""
    from kiro_crew.security import shell_normalizer as sn

    calls: list[int] = []

    def record(tokens: "list[str]") -> "list[str]":
        calls.append(len(tokens))
        return []

    monkeypatch.setattr(sn, "_resolve_local_assignments", record)
    return calls


def test_doubling_assignment_chain_fails_closed_before_building_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each step doubles the value; the bound is checked on lengths, so the
    # resolver that would build the strings is never reached.
    calls = _record_resolver_calls(monkeypatch)
    chain = "; ".join(f"x{i}=${{x{i - 1}}}${{x{i - 1}}}" for i in range(1, 41))
    body = f'x0=$(printf a); {chain}; {_PG} -f "$x40"'
    assert _argv_floor._kill_body_synthesizes_self(body)
    assert not calls


def test_references_that_add_up_past_the_cap_fail_closed_before_building_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every word fits under the body's character cap on its own, but the words
    # together resolve past it, so the body is refused on lengths before the
    # resolver builds any of them.
    calls = _record_resolver_calls(monkeypatch)
    chain = "; ".join(["x=$x$x"] * 10)
    refs = " ".join(["$x."] * 3)
    body = f"x=$(printf {'a' * 64}); {chain}; {_PG} -f {refs}"
    assert _argv_floor._kill_body_synthesizes_self(body)
    assert not calls


@pytest.mark.parametrize(("operands", "refused"), [(1, False), (6, True)])
def test_the_resolved_size_limit_is_on_the_total_of_the_words(operands: int, refused: bool) -> None:
    # A 64-character value doubled five times resolves to about 2,300 characters
    # per reference: one reference keeps the body under a 16,384 limit, six
    # together pass it, though every word alone stays far below it.
    from kiro_crew.security import shell_normalizer as sn

    chain = "; ".join(["x=$x$x"] * 5)
    refs = " ".join(["$x."] * operands)
    body = f"x=$(printf {'a' * 64}); {chain}; {_PG} -f {refs}"
    words = sn._synthesis_words(body, _argv_floor._static_substitution_output, 16_384)
    assert (words is None) is refused


def test_continuation_split_chain_fails_closed_before_building_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The doubling refs are split by a line continuation; the resolver folds it
    # and builds one doubled value, so the bound sizes the folded token.
    calls = _record_resolver_calls(monkeypatch)
    chain = "; ".join(f"x{i}=${{x{i - 1}}}\\\n${{x{i - 1}}}" for i in range(1, 41))
    body = f'x0=$(printf a); {chain}; {_PG} -f "$x40"'
    assert _argv_floor._kill_body_synthesizes_self(body)
    assert not calls


def test_quoted_space_chain_fails_closed_before_building_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The doubling refs are concatenated across a QUOTED space, so the quoted
    # value is one assignment and is sized whole.
    calls = _record_resolver_calls(monkeypatch)
    chain = "; ".join(f'x{i}="${{x{i - 1}}} ${{x{i - 1}}}"' for i in range(1, 41))
    body = f'x0=$(printf a); {chain}; {_PG} -f "$x40"'
    assert _argv_floor._kill_body_synthesizes_self(body)
    assert not calls


def test_quoted_word_holding_an_assignment_chain_fails_closed_before_building_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One double-quoted word holds the whole chain with its separators. The
    # resolver de-quotes it and splits it into assignments, so the bound sizes
    # those same tokens rather than one opaque word.
    calls = _record_resolver_calls(monkeypatch)
    chain = ";".join(f"x{i}=${{x{i - 1}}}${{x{i - 1}}}" for i in range(1, 41))
    target = f'"$(printf {_NAME[:4]})$(printf {_NAME[4:]})"'
    body = f'x0=$(printf a); "{chain}"; {_PG} -f {target}'
    assert _argv_floor._kill_body_synthesizes_self(body)
    assert not calls


def test_a_resolver_failure_is_a_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    # A failed expansion must not read as "no words": that would search nothing.
    from kiro_crew.security import shell_normalizer as sn

    def fail(_tokens: "list[str]") -> "list[str]":
        raise MemoryError

    monkeypatch.setattr(sn, "_resolve_local_assignments", fail)
    assert _argv_floor._kill_body_synthesizes_self(f"{_PG} -f worker")


def test_deep_substitution_nesting_fails_closed_before_any_fold_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Work is counted, not timed: a body nested past the fold's depth cap is
    # refused from the one quote-mask read, without a single fold pass.
    from kiro_crew.security import shell_normalizer as sn

    real = sn._CONST_OUTPUT_SUBST_RE
    passes = [0]

    class Counting:
        def sub(self, repl: "Callable[[re.Match[str]], str]", text: str) -> str:
            passes[0] += 1
            return real.sub(repl, text)

    monkeypatch.setattr(sn, "_CONST_OUTPUT_SUBST_RE", Counting())
    depth = sn._FOLD_DEPTH_CAP + 50
    body = f'{_PG} -f "' + "$(printf x" * depth + "a" * 15_000 + ")" * depth + '"'
    assert _argv_floor._kill_body_synthesizes_self(body)
    assert passes[0] == 0


def test_nesting_at_the_depth_cap_still_folds() -> None:
    from kiro_crew.security import shell_normalizer as sn

    depth = sn._FOLD_DEPTH_CAP
    body = "$(printf x" * depth + "a" + ")" * depth
    assert sn._fold_const_output_substitutions(body, lambda sub: sub.split()[-1]) == (
        "x" * depth + "a"
    )


def test_wide_comma_group_is_capped_before_building() -> None:
    # A comma alternation past the cap is signalled as overflow and never
    # materialised, so a group of thousands of commas carrying a long seed is
    # an over-cap residual decided without allocating its product.
    big = ",".join([""] * 20_000)
    alts = _argv_floor._brace_alternatives(big)
    assert alts is not None and len(alts) > _argv_floor._BRACE_EXPANSION_CAP
    body = "z{" + big + "}" + "y" * 100_000
    assert _argv_floor._brace_expansions(body, budget=[_argv_floor._BRACE_CHAR_CAP]) is None


def test_many_unterminated_parameter_openers_are_scanned_once() -> None:
    # Work is counted, not timed: each character is read a bounded number of times.
    from kiro_crew.security import shell_normalizer as sn

    reads = [0]

    class Counting(str):
        def __getitem__(self, key: "int | slice") -> str:  # type: ignore[override]
            reads[0] += 1
            return str.__getitem__(self, key)

    text = Counting("${ " * 2000)
    sn._mask_quoted_braces(text)
    assert reads[0] <= 20 * len(text), reads[0]


def test_connection_operand_brace_check_has_no_character_budget() -> None:
    # The body's work budget is the bare-kill caller's; the connection-target
    # check keeps its word cap alone, so a long path with a modest fan-out is
    # judged on its alternatives rather than refused for its length.
    source = "".join(f"/segment{i}" for i in range(80)) + "/archive{1..220}"
    assert len(_argv_floor._brace_expansions(source) or ()) == 220
    assert not _argv_floor._operand_targets_self(source)


def test_unrelated_expansion_overflow_stays_allowed() -> None:
    cmd = f"{_K} $({_PG} -f x{{1..9999}})"
    assert not _floor(cmd), cmd


@pytest.mark.parametrize(
    "cmd,denied",
    [
        # a word whose fan-out overflows the cap: an over-cap residual (its
        # expansions name no live process), decided without an enumeration
        (f"{_K} $({_PG} -f worker{{{{a..z}}{{a..z}}{{a..f}}" + "{a..a}" * 1200 + ",main}})", False),
        # many single-alternative groups multiplied by local-variable references:
        # the resolved value would pass the character budget, so it fails closed
        (
            f"{_K} $(x=worker" + "{a..a}" * 1000 + f"; {_PG} -f " + " ".join(["$x" * 40] * 3) + ")",
            True,
        ),
    ],
    ids=["wide-word", "variable-multiplied"],
)
def test_pathological_brace_work_is_bounded(
    cmd: str, denied: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The expansion work is bounded by one cumulative character budget per body,
    # so the number of group rewrites is a small constant here, not one per group
    # per word (over a hundred thousand for the second command without it).
    rewrites = [0]
    real = _argv_floor._brace_alternatives

    def counting(body: str, cap: int = _argv_floor._BRACE_EXPANSION_CAP) -> "list[str] | None":
        rewrites[0] += 1
        return real(body, cap)

    monkeypatch.setattr(_argv_floor, "_brace_alternatives", counting)
    assert _floor(cmd) is denied, cmd
    assert rewrites[0] <= 2000, rewrites[0]


def test_single_literal_resolver_declines_formats_and_multi_operand() -> None:
    from kiro_crew.security import shell_normalizer as sn

    def r(body: str) -> str:
        return sn._single_literal_output(body, _argv_floor._static_substitution_output)

    assert r(f"printf {_NAME}") == _NAME
    assert r(f"echo -n {_NAME}") == _NAME
    assert r("printf '%s@%s' a b") == "\x00"
    assert r(f"echo {_HEAD} {_TAIL}") == "\x00"
    assert r(f"printf '{_HEAD}\\n{_TAIL}'") == "\x00"
