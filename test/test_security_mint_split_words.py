"""A mint behind a shell word the tokenizer splits inside its own substitution.

Two spellings hide a mint whose verb arrives through a substitution:

* a leading assignment word whose substitution holds a space
  (``X=$(echo hi) <product> "$(printf token)"``): the tokens split it into
  ``x=$(echo`` and ``hi)``, so ``hi)`` reads as the program;
* a double-quoted assignment value that mixes a parameter expansion holding a quote
  with a command substitution (``T="${U:+"'"}$(printf token)"; <product> $T``): the
  substitution's span, read from the start of the word, lets the quote in the
  ``${...}`` prefix pair across it.

The floor also reads each frame with those words rejoined, reads a substitution's
span from its own opener, and places each assignment by its ``NAME=`` when the raw word
count does not pair. Every row in ``_MINTS`` is allowed with that removed and refused
with it; the controls stay allowed.
"""

from __future__ import annotations

import pytest

from kiro_crew import security

_N = "kiro" + "crew"
_Q = "'"

_MINTS = [
    f'X=$(echo hi) {_N} "$(printf token)"',
    f'X=$(echo hi) Y=1 {_N} "$(printf token)"',
    f'env X=$(echo a b) {_N} "$(printf token)"',
    f'T="${{U:+"{_Q}"}}$(printf token)"; {_N} $T',
    f'T="${{U:-"{_Q}"}}$(printf token)"; {_N} $T',
    f'export T="${{U:+"{_Q}"}}$(printf token)"; {_N} $T',
    f'T="${{U:+"{_Q}"}}`printf token`"; {_N} $T',
]

# Refused by the as-tokenized reading too; pinned so they stay refused.
_STILL_REFUSED = [
    f'X=`echo hi` {_N} "$(printf token)"',
    f"X=$(echo hi) {_N} $(printf token)",
    f'T="${{U:+x}}$(printf token)"; {_N} $T',
]

_CONTROLS = [
    f"X=$(echo hi) {_N} doctor",
    f'T="${{U:+"{_Q}"}}$(printf doctor)"; {_N} $T',
    'X=$(echo hi) echo "$(printf token)"',
    f'T="${{U:+"{_Q}"}}$(printf token)"; echo $T',
    "X=$(echo hi) ls -la",
]


@pytest.mark.parametrize("cmd", _MINTS + _STILL_REFUSED)
def test_a_mint_behind_a_split_word_is_refused(cmd: str) -> None:
    assert security.is_denied(cmd), f"bash runs the mint: {cmd!r}"


@pytest.mark.parametrize("cmd", _CONTROLS)
def test_the_same_shapes_without_a_mint_stay_allowed(cmd: str) -> None:
    assert not security.is_denied(cmd), cmd


def test_an_opener_that_never_closes_is_read_in_linear_work() -> None:
    # A literal ``$(`` before many words (``grep -F '$(' <paths>``) never closes. Each
    # token is scanned once, so doubling the words about doubles the work.
    from denied_commands_security_helpers import _interpreter_work

    from kiro_crew.security.shell_normalizer_redirects import _rejoin_split_substitutions

    def work(n: int) -> int:
        tokens = ["grep", "-f", "$("] + [f"src/p{i}.py" for i in range(n)]
        return _interpreter_work(lambda: _rejoin_split_substitutions(tokens))

    assert work(800) < 3 * work(400)


_BASE = f'X=$(echo hi) {_N} "$(printf token)"'

# A later opener that never closes (one token) keeps every run rejoined before it.
_TAIL_MINTS = [
    f"{_BASE}; echo '$('",
    f"{_BASE} '$('",
    f"{_BASE} '`'",
    f"{_BASE} # $(",
    f"{_BASE} && grep -F '$(' f",
]

_TAIL_CONTROLS = [
    "grep -F '$(' src/a.py src/b.py",
    f"X=$(echo hi) {_N} status",
    "echo '`' done",
    "ls # $( not closed",
]


@pytest.mark.parametrize("cmd", _TAIL_MINTS)
def test_a_never_closing_tail_keeps_the_rejoined_mint_refused(cmd: str) -> None:
    assert security.is_denied(cmd), f"bash runs the mint: {cmd!r}"


@pytest.mark.parametrize("cmd", _TAIL_CONTROLS)
def test_a_never_closing_tail_alone_stays_allowed(cmd: str) -> None:
    assert not security.is_denied(cmd), cmd


# The quoted-substitution mint behind one more decoration: a prefix assignment before a
# ``bash -c`` use, a ``{ }`` or ``( )`` wrapper, or a no-op naming the product in the body.
_DECORATED_MINTS = [
    f'X=$(echo hi) T=$(: "{_Q}"; echo token); bash -c "{_N} $T"',
    f'{{ T=$(: "{_Q}"; echo token); {_N} $T; }}',
    f'{{ T=$(: "{_Q}"; echo token); }}; {_N} $T',
    f'( T=$(: "{_Q}"; echo token); {_N} $T )',
    f'T=$(: "{_Q}"; echo token; : {_N}); {_N} $T',
    f'if true; then ( T=$(: "{_Q}"; echo token); {_N} $T ); fi',
    f'if true; then {{ T=$(: "{_Q}"; echo token); {_N} $T; }}; fi',
    f'while :; do ( T=$(: "{_Q}"; echo token); {_N} $T ); break; done',
    f'! ( T=$(: "{_Q}"; echo token); {_N} $T )',
    f'! {{ T=$(: "{_Q}"; echo token); {_N} $T; }}',
    f'time ( T=$(: "{_Q}"; echo token); {_N} $T )',
    f'{{ ( T=$(: "{_Q}"; echo token); {_N} $T ); }}',
    f'if ( T=$(: "{_Q}"; echo token); {_N} $T ); then :; fi',
    f'while ( T=$(: "{_Q}"; echo token); {_N} $T ); do break; done',
    f'until ( T=$(: "{_Q}"; echo token); {_N} $T ); do :; done',
    f'if {{ T=$(: "{_Q}"; echo token); {_N} $T; }}; then :; fi',
    f'if {{ T=$(: "{_Q}"; echo token); }}; then {_N} $T; fi',
    f'while {{ T=$(: "{_Q}"; echo token); }}; do {_N} $T; break; done',
    f'case x in (x) ( T=$(: "{_Q}"; echo token); {_N} $T );; esac',
    f'case x in (x) {{ T=$(: "{_Q}"; echo token); {_N} $T; }};; esac',
    f'time -p ( T=$(: "{_Q}"; echo token); {_N} $T )',
    # A function body is read as run, as main reads one it can parse
    # (``f(){ T=$(echo token); }; kirocrew $T`` is refused on main): bash sets the
    # global ``T`` when ``f`` runs.
    f'f(){{ T=$(: "{_Q}"; echo token); }}; f; {_N} $T',
    f'function f {{ T=$(: "{_Q}"; echo token); }}; f; {_N} $T',
    f'{{ f(){{ T=$(: "{_Q}"; echo token); }}; f; }}; {_N} $T',
    f'f(){{ T=$(: "{_Q}"; echo token); }}; {_N} $T',
    f'function f {{ T=$(: "{_Q}"; echo token); }}; {_N} $T',
]

# What bash does not run as a mint: a subshell's binding ends at its ``)``, and a body
# naming only the program binds the program.
_DECORATED_CONTROLS = [
    f'( T=$(: "{_Q}"; echo token) ); {_N} $T',
    f"X=$(echo hi) {_N} status",
    f'T=$(: "{_Q}"; : {_N}); echo $T',
    f"(cd /tmp && {_N} status)",
    f"if true; then ( {_N} status ); fi",
    f"if ( {_N} status ); then :; fi",
    "while ( false ); do :; done",
]


@pytest.mark.parametrize("cmd", _DECORATED_MINTS)
def test_a_decorated_quoted_substitution_mint_is_refused(cmd: str) -> None:
    assert security.is_denied(cmd), f"bash runs the mint: {cmd!r}"


@pytest.mark.parametrize("cmd", _DECORATED_CONTROLS)
def test_the_decorations_without_a_mint_stay_allowed(cmd: str) -> None:
    assert not security.is_denied(cmd), cmd


# The verb-slot seam: a use glued to its ``;`` in a ``{ }``/``if``/``for``/``while`` body, a
# verb behind an operand bound earlier to empty or a flag (``Q=-v; kirocrew $Q $T``), and a
# verb forwarded by a function wrapper or ``xargs`` are all refused, because the operand-side
# binding is carried at every non-program position.
_SLOT = f'T=$(: "{_Q}"; echo token; : {_N}); '
_VERB_SLOT_MINTS = [
    f"{_SLOT}{{ {_N} $T; }}",
    f"{_SLOT}{_N} $T;",
    f"{_SLOT}if true; then {_N} $T; fi",
    f"{_SLOT}for x in a; do {_N} $T; done",
    f"{_SLOT}while :; do {_N} $T; break; done",
    f"{_SLOT}Q=-v; {_N} $Q $T",
    f"{_SLOT}Q=; {_N} $Q $T",
    f"{_SLOT}k(){{ {_N} $1; }}; k $T",
    f'{_SLOT}k(){{ {_N} "$@"; }}; k $T',
    f"{_SLOT}xargs {_N} <<< $T",
]

# A substitution that resolves to a known subcommand word is not quoted-token laundering.
_VERB_SLOT_CONTROLS = [
    f"T=$(echo doctor); {_N} $T",
]


@pytest.mark.parametrize("cmd", _VERB_SLOT_MINTS)
def test_a_mint_at_the_resolved_verb_slot_is_refused(cmd: str) -> None:
    assert security.is_denied(cmd), f"bash runs the mint: {cmd!r}"


@pytest.mark.parametrize("cmd", _VERB_SLOT_CONTROLS)
def test_a_substitution_naming_a_benign_verb_stays_allowed(cmd: str) -> None:
    assert not security.is_denied(cmd), cmd


# The verb-slot seam (a word before the verb): the operand-side binding is carried at EVERY
# non-program position, so a verb behind any word bash may empty, flag or rebind is refused --
# an unquoted ``$(...)``/backtick, a positional or special parameter (``$1``/``$@``), a computed
# binding (``X=$(true); ... $X``), a whitespace-only literal, or a variable whose value at the use
# differs from its last assignment (``... $X $T; X=chat``). Each shape is pinned flat, in a
# ``{ }`` group, and in a ``( )`` subshell.
def _seam_nest3(body_use: str) -> "list[str]":
    return [body_use, f"{{ {body_use}; }}", f"( {body_use} )"]


_SEAM_BODY = f"T=$(echo token; : {_N})"
_SEAM_CORES = [
    f"{_SEAM_BODY}; X=$(true); {_N} $X $T",  # computed binding before the verb
    f"{_SEAM_BODY}; {_N} $(true) $T",  # unquoted command substitution
    f"{_SEAM_BODY}; {_N} `true` $T",  # backtick substitution
    f"{_SEAM_BODY}; {_N} $1 $T",  # positional parameter
    f'{_SEAM_BODY}; {_N} "$@" $T',  # special parameter
    f"{_SEAM_BODY}; E=' '; {_N} $E $T",  # whitespace-only binding
    f"{_SEAM_BODY}; uvx {_N} $(true) $T",  # launcher form, uncertain first operand
    f"{_SEAM_BODY}; python -m kiro_crew $(true) $T",  # module form, uncertain first operand
    f"{_SEAM_BODY}; {_N} $X $T; X=chat",  # unset at use, bound to a verb AFTER
    f"{_SEAM_BODY}; X=-v; {_N} $X $T; X=chat",  # flag at use, bound to a verb AFTER
    f"{_SEAM_BODY}; {_N} $X $T; X=",  # unset at use, bound empty AFTER
    f'T=$(: "{_Q}"; echo token); {_N} $V $T; V=x',  # GPT r55 false-positive row (lone quote)
]
_SEAM_MINTS = [c for core in _SEAM_CORES for c in _seam_nest3(core)]

_SEAM_CONTROLS = [
    c
    for core in [
        'f() { echo "$1"; }; f $T',  # benign wrapper: body names no self
        f"T=$(echo doctor); {_N} $T",  # substitution resolving to a benign verb
    ]
    for c in _seam_nest3(core)
]


@pytest.mark.parametrize("cmd", _SEAM_MINTS)
def test_a_verb_behind_an_uncertain_slot_word_is_refused(cmd: str) -> None:
    assert security.is_denied(cmd), f"bash runs the mint: {cmd!r}"


# A word whose reading bash expansion can empty, turn into a flag or rebind -- a brace or glob
# word, a parameter default, or a variable rebound by ``unset``/``for``/``read``/``printf -v`` or
# read before its own prefix assignment applies -- never hides the verb after it. The last core
# is fail-closed: a real variable subcommand does not keep its later operand either.
_EXPANSION_CORES = [
    f"{_SEAM_BODY}; {_N} {{,}} $T",
    f"{_SEAM_BODY}; {_N} {{-v,}} $T",
    f"{_SEAM_BODY}; X=; {_N} ${{X-chat}} $T",
    f"{_SEAM_BODY}; {_N} ${{X:+chat}} $T",
    f"{_SEAM_BODY}; shopt -s nullglob; {_N} /nonexist* $T",
    f"{_SEAM_BODY}; X=chat; unset X; {_N} $X $T",
    f'{_SEAM_BODY}; X=chat; for X in ""; do {_N} $X $T; done',
    f"{_SEAM_BODY}; X=chat; read -r X </dev/null; {_N} $X $T",
    f'{_SEAM_BODY}; X=chat; printf -v X ""; {_N} $X $T',
    f"{_SEAM_BODY}; X=chat {_N} $X $T",
    f'{_SEAM_BODY}; X=chat; {_N} $X -m "$T"',
]


@pytest.mark.parametrize("cmd", [c for core in _EXPANSION_CORES for c in _seam_nest3(core)])
def test_a_verb_behind_an_expandable_slot_word_is_refused(cmd: str) -> None:
    assert security.is_denied(cmd), f"bash runs the mint: {cmd!r}"


@pytest.mark.parametrize("cmd", _SEAM_CONTROLS)
def test_a_command_that_runs_no_mint_stays_allowed(cmd: str) -> None:
    assert not security.is_denied(cmd), cmd


# A verb word inside a NESTED command (``$( )``, a backtick, ``<( )``/``>( )``) is that inner
# command's verb slot, however the outer command reads its operands; and a subcommand GROUP or
# forwarder (``pod``, ``pod exec ... --``) runs the word after it as its own verb.
_NESTED_AND_GROUP_CORES = [
    f"{_SEAM_BODY}; {_N} doctor $({_N} $T)",
    f"{_SEAM_BODY}; {_N} doctor `{_N} $T`",
    f"{_SEAM_BODY}; {_N} doctor <({_N} $T)",
    f"{_SEAM_BODY}; {_N} doctor >({_N} $T)",
    f"{_SEAM_BODY}; {_N} chat -m $({_N} $T)",
    f"{_SEAM_BODY}; {_N} pod $T wt1",
    f'{_SEAM_BODY}; {_N} pod "$T" wt1',
    f"{_SEAM_BODY}; {_N} pod -v $T wt1",
    f"{_SEAM_BODY}; python -m kiro_crew pod $T wt1",
    f"{_SEAM_BODY}; {_N} pod exec wt1 -- $T",
    f"{_SEAM_BODY}; echo $({_N} $T)",
]


@pytest.mark.parametrize("cmd", [c for core in _NESTED_AND_GROUP_CORES for c in _seam_nest3(core)])
def test_a_verb_in_a_nested_command_or_behind_a_group_is_refused(cmd: str) -> None:
    assert security.is_denied(cmd), f"bash runs the mint: {cmd!r}"


# A value after a literal subcommand (``artifact save --content-file "$T"``) is that
# subcommand's data even when its substitution body names the verb and the product: the
# literal subcommand fills the verb slot. Allowed, as on main.
_DATA_AFTER_A_SUBCOMMAND = [
    f'{_SEAM_BODY}; {_N} artifact save --content-file "$T"',
    f'T=$(: "{_Q}"; echo token; : {_N}); {_N} artifact save --content-file "$T"',
    f"LOG=$(grep -l token ~/.{_N}/logs/*.log | tail -1); "
    f'{_N} artifact save --name auth-failure --content-file "$LOG"',
]

# After a literal subcommand (``chat``, ``logs``) the verb slot is filled, so a later
# variable is that subcommand's data: allowed, as on main.
_LEAF_SUBCOMMAND_DATA = [
    f'{_SEAM_BODY}; {_N} chat -m "$T"',
    f'T=$(: "{_Q}"; echo token; : {_N}); {_N} chat -m "$T"',
    f'NOTE=$({_N} learn list | grep -m1 token); {_N} chat -m "$NOTE"',
    f'LOGS=$({_N} logs -n 50 | grep -i token); {_N} chat -m "$LOGS"',
    f'T=$(: "{_Q}"; echo token); {_N} logs -n 5 $T',
]

# The same variable where it can be the subcommand still mints, and is refused.
_LEAF_SLOT_STILL_MINTS = [
    f"LOGS=$({_N} logs -n 50 | grep -i token); {_N} $LOGS",
    f'LOGS=$({_N} logs -n 50 | grep -i token); {_N} "$LOGS"',
    f"LOGS=$({_N} logs -n 50 | grep -i token); {_N} $LOGS chat",
    f'T=$(: "{_Q}"; echo token); {_N} $T chat',
    f'T=$(: "{_Q}"; echo token); {_N} pod $T',
    f'T=$(: "{_Q}"; echo token); {_N} --verbose chat $T',
    f"T=$(echo token; : {_N}); {_N} chat -m $({_N} $T)",
    # The substituted word itself is in the verb slot; a literal verb after it is no
    # subcommand data, and an option word before it does not fill the slot.
    f"ID=$({_N} cron list | grep token | awk '{{print $1}}'); {_N} \"$ID\" token",
    f"LOGS=$({_N} logs -n 50 | grep -i token); {_N} --verbose $LOGS",
]


@pytest.mark.parametrize("cmd", _LEAF_SUBCOMMAND_DATA)
def test_a_variable_after_a_leaf_subcommand_is_data(cmd: str) -> None:
    assert not security.is_denied(cmd), cmd


@pytest.mark.parametrize("cmd", _LEAF_SLOT_STILL_MINTS)
def test_a_variable_that_can_be_the_subcommand_is_still_refused(cmd: str) -> None:
    assert security.is_denied(cmd), f"bash can run the mint: {cmd!r}"


@pytest.mark.parametrize("cmd", _DATA_AFTER_A_SUBCOMMAND)
def test_a_file_operand_after_a_literal_subcommand_is_data(cmd: str) -> None:
    assert not security.is_denied(cmd), cmd


def _bare_nest(n: int) -> str:
    return f'T=$(: "{_Q}"; echo token); ' + "( " * n + f"{_N} $T" + " )" * n


def _own_assign_nest(n: int) -> str:
    # An own-level ``X=$(...)`` at EVERY level: each scope is emitted, but a scope nested in
    # another is elided from the enclosing body, so the total body text stays linear.
    return "( X=$(echo a); " * n + "echo hi" + " )" * n


def _mixed_nest(n: int) -> str:
    # A substitution assignment at every OTHER level.
    opens = "".join("( X=$(echo a); " if i % 2 == 0 else "( " for i in range(n))
    return opens + "echo hi" + " )" * n


def _carried_nest(n: int) -> str:
    # n own-level assignments in an outer scope, then one nested own-assignment scope: the
    # nested frame carries all n outer assignments, so the carry prefix must not re-cost per
    # level nor let the body's inflated length trip the walk's re-descent bound.
    return "( " + "".join(f"a{i}=$(:);" for i in range(n)) + "( x=$(:); echo hi ) )"


@pytest.mark.parametrize("build", [_bare_nest, _own_assign_nest, _mixed_nest, _carried_nest])
def test_a_deep_subshell_nesting_is_read_in_linear_work(build) -> None:
    # Each subshell body is found in one pass; a scope nested in another is elided from the
    # enclosing body and read as its own frame, so doubling the nesting under-quadruples the
    # work -- for bare nesting, own-assignment-at-every-level, and every-other-level alike.
    from denied_commands_security_helpers import _interpreter_work

    def work(n: int) -> int:
        return _interpreter_work(lambda: security.is_denied(build(n)))

    assert work(160) < 4 * work(80)


def test_a_deep_subshell_nesting_does_not_rescan_its_text(monkeypatch) -> None:
    # A regex rescan runs in C, which ``_interpreter_work`` cannot see: count the
    # characters the assignment pattern is asked to read instead.
    from kiro_crew.security import shell_normalizer_redirects as redir
    from kiro_crew.security import shell_normalizer_walk as walk

    def scanned(n: int) -> int:
        cmd = f'T=$(: "{_Q}"; echo token); ' + "( " * n + f"{_N} $T" + " )" * n
        real = redir._SUBST_ASSIGN_RE
        read = [0]

        class Counting:
            def finditer(self, text, *a):
                read[0] += len(text)
                return real.finditer(text, *a)

            def search(self, text, *a):
                read[0] += len(text)
                return real.search(text, *a)

        with monkeypatch.context() as scoped:
            scoped.setattr(redir, "_SUBST_ASSIGN_RE", Counting())
            walk._subshell_bodies(cmd)
        return read[0]

    assert scanned(800) < 3 * scanned(400)


def _carried_mint(k: int) -> str:
    # The GPT r52 shape: k own-level assignments in the outer scope, then a nested scope that
    # mints with a carried variable. The carried assignments prepended to the inner body must
    # not push its length past the walk's re-descent bound (which is on the RAW source span).
    outer = "".join(f"a{i}=$(:);" for i in range(k))
    return f'({outer}t=$(: "{_Q}"; echo token; : {_N});(x=$(:);{_N} $t))'


@pytest.mark.parametrize("k", [0, 2, 4, 8, 16, 64])
def test_a_carried_assignment_subshell_mint_is_refused(k: int) -> None:
    assert security.is_denied(_carried_mint(k)), k


@pytest.mark.parametrize("k", [0, 4, 16, 64])
def test_a_carried_assignment_subshell_without_a_mint_stays_allowed(k: int) -> None:
    # Same shape, inner mint removed: no mint, so it stays allowed (the carry does not
    # invent a refusal, and the length-bound fix did not over-refuse).
    outer = "".join(f"a{i}=$(:);" for i in range(k))
    assert not security.is_denied(f"({outer}t=$(echo hi);(x=$(:);echo $t))"), k


_VERB_SLOT = f'T=$(: "{_Q}"; echo token; : {_N}); '
_VERB_SLOT_MINTS = [
    # The subcommand slot is located on the RESOLVED argv by the floor's own self-invocation
    # recognizer: a program from a literal/alias binding, a launcher, the module form, an
    # unset word skipped, a ``>|`` redirect stepped over -- each runs ``kirocrew token``.
    f"{_VERB_SLOT}P={_N}; $P $T",
    f"{_VERB_SLOT}F=$(ls kiro*); $F $T",
    f"{_VERB_SLOT}python -m kiro_crew $T",
    f"{_VERB_SLOT}uvx {_N} $T",
    f"{_VERB_SLOT}{_N} $NOPE $T",
    f"{_VERB_SLOT}{_N} >| /tmp/o $T",
    f'( P={_N}; T=$(: "{_Q}"; echo token; : {_N}); ( X=$(echo a); $P $T ) )',
]


@pytest.mark.parametrize("cmd", _VERB_SLOT_MINTS)
def test_a_mint_at_the_resolved_subcommand_slot_is_refused(cmd: str) -> None:
    assert security.is_denied(cmd), cmd


def test_the_substitution_reader_is_memoised_across_the_walk() -> None:
    # The payload walk reads the same command through many frames (suffixes), so without
    # a per-gate-call memo _raw_substitution_mint_name would re-read each assignment's
    # body once per frame. @once_per_call collapses that: every distinct value string is
    # computed at most once per is_denied call, so the call count equals the distinct
    # count. (An unproven close still reads the whole remainder -- the memo removes the
    # per-frame factor, not the per-assignment one; the residual cost is the baseline
    # resolver's own whole-remainder read, tracked separately.)
    from kiro_crew.security import shell_normalizer as sn

    inner = sn._raw_substitution_mint_name.__wrapped__
    calls: list[str] = []

    def spy(value: str) -> object:
        calls.append(value)
        return inner(value)

    cmd = "a=$(: #); " * 40 + f"{_N} doctor"
    with pytest.MonkeyPatch.context() as scoped:
        from kiro_crew.security import call_views

        scoped.setattr(sn, "_raw_substitution_mint_name", call_views.once_per_call()(spy))
        security.is_denied(cmd)

    assert calls, "the reader was not exercised"
    assert len(calls) == len(set(calls)), (len(calls), len(set(calls)))


# GPT 6.1 on 899c268a: two bypasses of this change's own coverage, each a mint bash runs,
# allowed on origin/main and on that head, refused here.
_NESTED_SCOPE_MINTS = [
    # F1: a child subshell's assignment must not suppress the enclosing subshell's own.
    f'( ( X=$(echo hi) ); T=$(: "{_Q}"; echo token); {_N} $T )',
    f'( ( ( X=$(echo hi) ) ); T=$(: "{_Q}"; echo token); {_N} $T )',
    # F2: the operand-side (verb+program) binding follows an alias, a transform and an append.
    f'T=$(: "{_Q}"; echo token; : {_N}); U=$T; {_N} $U',
    f'T=$(: "{_Q}"; echo token; : {_N}); U=${{T:0}}; {_N} $U',
    f'T=$(: "{_Q}"; echo token; : {_N}); U=; U+=$T; {_N} $U',
    # F1: a subshell mint inside an executable payload (bash -c / eval), after a pipe, or
    # after a leading assignment must still refuse -- the per-frame subshell scan runs on a
    # payload frame, not only the top level (r49 Opus).
    f"""bash -c '( T=$(: "{_Q}"; echo token); {_N} $T )'""",
    f"""eval '( T=$(: "{_Q}"; echo token); {_N} $T )'""",
    f"""echo hi | bash -c '( T=$(: "{_Q}"; echo token); {_N} $T )'""",
    f"""X=$(echo a); bash -c '( T=$(: "{_Q}"; echo token); {_N} $T )'""",
    # A substitution whose body ALSO names the program, used at the verb slot of a
    # double-quoted ``-c`` / ``eval`` payload: the operand-side binding carries into the
    # payload frame so the walk sees ``kirocrew token``, same as it does at the top level.
    f'T=$(: "{_Q}"; echo token; : {_N}); bash -c "{_N} $T"',
    f'T=$(: "{_Q}"; echo token; : {_N}); eval {_N} $T',
    # A redirection is stripped from argv, so it does not hold the subcommand slot: the verb
    # is the first NON-redirect word, and these all run ``kirocrew token``.
    f'T=$(: "{_Q}"; echo token; : {_N}); {_N} 2>/dev/null $T',
    f'T=$(: "{_Q}"; echo token; : {_N}); {_N} > /tmp/o $T',
    f'T=$(: "{_Q}"; echo token; : {_N}); {_N} &>/dev/null $T',
    f'T=$(: "{_Q}"; echo token; : {_N}); {_N} 2>&1 $T',
    f'T=$(: "{_Q}"; echo token; : {_N}); {_N} $T 2>/dev/null',
    f'T=$(: "{_Q}"; echo token; : {_N}); U=$T; {_N} 2>/dev/null $U',
    # The verb-bound reading carries through a concatenated RHS into the payload
    # that runs it; bash runs ``kirocrew token`` from each (main allows them).
    f'T=$(: "{_Q}"; echo token; : {_N}); C="{_N} $T"; eval "$C"',
    f'T=$(: "{_Q}"; echo token; : {_N}); C="{_N} ${{T}}"; eval "$C"',
    f'T=$(: "{_Q}"; echo token; : {_N}); C="{_N} ${{T:0}}"; eval "$C"',
    f'T=$(: "{_Q}"; echo token; : {_N}); C="{_N} $T"; eval $C',
    f'T=$(: "{_Q}"; echo token; : {_N}); C="{_N} $T"; bash -c "$C"',
    f'T=$(: "{_Q}"; echo token; : {_N}); C="{_N} $T"; sh -c "$C"',
    f'T=$(: "{_Q}"; echo token; : {_N}); C="{_N}"; C+=" $T"; eval "$C"',
    f'T=$(: "{_Q}"; echo token; : {_N}); C="{_N}"; C+=" ${{T}}"; bash -c "$C"',
]

_NESTED_SCOPE_CONTROLS = [
    # A child subshell's assignment alone is no mint; the enclosing one runs nothing minty.
    f"( ( X=$(echo hi) ); {_N} status )",
    # A later rebinding that aliases nothing drops the operand binding.
    f'T=$(: "{_Q}"; echo token; : {_N}); U=$T; U=other; {_N} $U',
    f"( ( cd /tmp ) ); {_N} status",
    # Concatenation is not a whole-value alias: $U expands to more than the verb, so the
    # operand binding is not carried and these stay allowed (gpt r46).
    f'T=$(: "{_Q}"; echo token; : {_N}); U=prefix$T; {_N} $U',
    f'T=$(: "{_Q}"; echo token; : {_N}); U=${{T}}suffix; {_N} $U',
    f'T=$(: "{_Q}"; echo token; : {_N}); U=$T; U+=$T; {_N} $U',
    f'T=$(: "{_Q}"; echo token; : {_N}); U=$T; U+=tail; {_N} $U',
    # A payload built from a substitution that prints no verb runs no mint.
    f'T=$(echo doctor); C="{_N} $T"; eval "$C"',
]


@pytest.mark.parametrize("cmd", _NESTED_SCOPE_MINTS)
def test_a_nested_or_aliased_substitution_mint_is_refused(cmd: str) -> None:
    assert security.is_denied(cmd), f"bash runs the mint: {cmd!r}"


@pytest.mark.parametrize("cmd", _NESTED_SCOPE_CONTROLS)
def test_the_nested_or_aliased_shapes_without_a_mint_stay_allowed(cmd: str) -> None:
    assert not security.is_denied(cmd), cmd


# A job looked up by name and acted on by its id: the id follows a literal subcommand
# (``cron trigger``, ``learn add``, ``artifact show``), which fills the verb slot.
_FILTERED_LOOKUPS = [
    f"ID=$({_N} cron list | grep token | awk '{{print $1}}'); {_N} cron trigger \"$ID\"",
    f"ID=$({_N} cron list | grep -w token | cut -d' ' -f1); {_N} cron history \"$ID\"",
    f"ID=`{_N} cron list | grep token | cut -d' ' -f1`; {_N} cron pause \"$ID\"",
    f'NOTE=$({_N} logs | grep token | tail -1); {_N} learn add "$NOTE"',
    f'F=$({_N} artifact list | grep token | head -1); {_N} artifact show "$F"',
    f"bash -c 'ID=$({_N} cron list | grep token | cut -f1); {_N} cron pause \"$ID\"'",
    f"ID=$({_N} cron list | grep token | awk '{{print $1}}'); "
    f'/opt/{_N}/.venv/bin/{_N} cron trigger "$ID"',
    f"ID=$({_N}.exe cron list | grep token | cut -d' ' -f1); {_N}.exe cron history \"$ID\"",
]
# The binding itself is unchanged: in the verb slot, a filtered word still mints.
_FILTERED_LITERALS = [
    f'T=$(: "{_Q}"; echo token | grep token); {_N} $T',
    f'T=$(: "{_Q}"; printf token | head -1); {_N} $T',
    f'T=$(: "{_Q}"; ls | grep x && echo token); {_N} $T',
]


@pytest.mark.parametrize("cmd", _FILTERED_LOOKUPS)
def test_an_id_after_a_literal_subcommand_is_data(cmd: str) -> None:
    assert not security.is_denied(cmd), cmd


@pytest.mark.parametrize("cmd", _FILTERED_LITERALS)
def test_a_filtered_word_in_the_verb_slot_still_mints(cmd: str) -> None:
    assert security.is_denied(cmd), cmd


# KNOWN over-refusal (late binding): a nested payload reads the command's FINAL bindings, so a
# variable rebound to the product AFTER the payload is refused, although bash runs ``echo token``
# here. Text position is not execution order, so no position-based reading is kept; main allows
# these, dc0ac92c refused them.
_LATE_BINDING_OVER_REFUSALS = [
    f"export F=$(printf echo); bash -c '$F token'; F=$(printf {_N})",
    f"export F=$(printf echo); sh -c '$F token'; F=$(printf {_N})",
    f"export F=$(printf echo); env bash -c '$F token'; F=$(printf {_N})",
]


@pytest.mark.parametrize("cmd", _LATE_BINDING_OVER_REFUSALS)
def test_a_late_binding_is_refused_as_a_known_over_refusal(cmd: str) -> None:
    assert security.is_denied(cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # The binding BEFORE the payload is a real mint.
        f"F=$(printf {_N}); bash -c '$F token'",
        # The variable as the subcommand is a real mint.
        f'T=$(: "{_Q}"; echo token); {_N} $T',
    ],
)
def test_the_verb_slot_and_prior_binding_mints_stay_refused(cmd: str) -> None:
    assert security.is_denied(cmd), cmd


# Two carriers can hold the SAME payload text (``bash -c '$F token'`` twice). Every payload
# reads the command's final bindings, so a mint bound anywhere before the end reaches it.
_DUP_P = "bash -c '$F token'"
_DUP_BENIGN = "F=$(printf echo)"
_DUP_MINT = f'F=$(: "{_Q}"; printf %s kirocre?)'
_DUP_PAYLOAD_MINTS = [
    f"{_DUP_BENIGN}; {_DUP_P}; {_DUP_MINT}; {_DUP_P}",
    f"{_DUP_BENIGN}; {_DUP_P}; F=$(printf {_N}); {_DUP_P}",
    # KNOWN over-refusal (late binding): the mint is bound after the last payload.
    f"{_DUP_BENIGN}; {_DUP_P}; {_DUP_P}; {_DUP_P}; {_DUP_MINT}",
    # Bash runs the mint at the MIDDLE payload; final bindings alone read ``echo``, but
    # main's git/tar self-dial floor refuses this line, so this matches main.
    f"{_DUP_BENIGN}; {_DUP_P}; {_DUP_MINT}; {_DUP_P}; {_DUP_BENIGN}; {_DUP_P}",
]
_DUP_PAYLOAD_ALLOWED = [
    f"{_DUP_BENIGN}; {_DUP_P}; F=$(printf true); {_DUP_P}",
]


@pytest.mark.parametrize("cmd", _DUP_PAYLOAD_MINTS)
def test_a_repeated_payload_reads_the_final_bindings(cmd: str) -> None:
    assert security.is_denied(cmd), f"bash runs the mint: {cmd!r}"


@pytest.mark.parametrize("cmd", _DUP_PAYLOAD_ALLOWED)
def test_a_repeated_payload_whose_final_binding_is_benign_stays_allowed(cmd: str) -> None:
    assert not security.is_denied(cmd), cmd


# A binding that bash applies AFTER the payload's text position but BEFORE it runs: a prefix
# assignment on the pipe's evaluator, a function body called after the export, a loop body
# run again after it. The final bindings hold the mint, so each is refused.
_EXECUTION_ORDER_MINTS = [
    f"printf '$F\\x20token' | F=$(: \"{_Q}\"; printf %s {_N}) bash",
    f"echo '$F token' | F=$(: \"{_Q}\"; printf %s {_N}) bash",
    f"f() {{ bash -c '$F token'; }}; export F=$(: \"{_Q}\"; printf %s {_N}); f",
    f"for i in 1 2; do bash -c '$F token'; export F=$(: \"{_Q}\"; printf %s {_N}); done",
    f"export F=$(printf echo); printf '$F\\x20token' | bash; export F=$(: \"{_Q}\"; printf %s {_N})",
]


@pytest.mark.parametrize("cmd", _EXECUTION_ORDER_MINTS)
def test_a_binding_applied_before_the_payload_runs_is_read(cmd: str) -> None:
    assert security.is_denied(cmd), cmd


# A rebinding to a computed program or verb clears the name's earlier program-only and
# operand flags: inside the subshell F is the product's own name, so bash runs ``kirocrew
# kirocrew`` (no mint), and the outer binding's flags must not leak into the rebound name.
_REBIND_CLEARS_FLAGS_ALLOWED = [
    f'F=$(: "{_Q}"; echo token; : {_N}); ( F=$(printf {_N}); {_N} $F )',
    f'F=$(: "{_Q}"; echo token; : {_N}); ( F=$(printf {_N}); bash -c "{_N} $F" )',
    f'F=$(: "{_Q}"; echo token; : {_N}); ( F=$(: "{_Q}"; printf %s kirocre?); {_N} $F )',
]


@pytest.mark.parametrize("cmd", _REBIND_CLEARS_FLAGS_ALLOWED)
def test_a_rebinding_to_a_computed_program_clears_the_old_flags(cmd: str) -> None:
    assert not security.is_denied(cmd), cmd


# ``eval`` joins its words once per walk, at the FIRST eval, and the joined text spans every
# later position, so it reads the command's final bindings: a later ``eval "$F" token`` whose
# F is bound after an earlier eval is still a mint.
_EVAL_JOIN_MINTS = [
    f'cd /repo && eval "$(direnv export bash)" && {_DUP_MINT} && eval "$F" token',
    f'{_DUP_BENIGN}; eval "$F" token; {_DUP_MINT}; eval "$F" token',
    f'eval echo hi; {_DUP_MINT}; eval "$F" token',
    f'{_DUP_MINT}; eval "$F" token',
]

# Known over-refusal, pinned to its current verdict: bash runs ``echo token`` here, but the
# eval join reads the final bindings (F bound to the product after the eval), so it refuses.
_EVAL_JOIN_KNOWN_OVER_REFUSAL = f"{_DUP_BENIGN}; eval '$F' 'token'; {_DUP_MINT}"


@pytest.mark.parametrize("cmd", _EVAL_JOIN_MINTS + [_EVAL_JOIN_KNOWN_OVER_REFUSAL])
def test_an_eval_join_reads_the_commands_final_bindings(cmd: str) -> None:
    assert security.is_denied(cmd), cmd
