"""Tests that the deny walk's cost stays bounded on hostile input.

A polynomial-backtracking regex keeps the bounded engine, and the data-consumer guard is
charged once per command and once per frame rather than once per nested payload or per
trigger token.
"""

from __future__ import annotations

import time

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _NAME, _PK, _TOK, _denied_by

from kiro_crew import security
from kiro_crew.security import argv_floor as _argv_floor

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestPolynomialBacktrackingStaysBounded:
    """The unbounded full-input path must not accept polynomial-backtracking regexes.

    Single-fragment patterns get full-input matching so
    an edition rule could not be silently capped at 2000 chars (padding bypass).
    That reasoning was about correctness and missed cost: the length cap was also
    what made POLYNOMIAL backtracking harmless. `a+a+$` is not the exponential
    shape `_redos_prone` screens, so it publishes fine, and unbounded it measures
    ~3.5s against 2,000 characters — a stall of the synchronous PreToolUse gate.
    GPT 5.6 flagged it, then flagged the grouped spelling `(a+)(a+)$` that the
    first screen still let through. Both were right.
    """

    def test_the_exponential_screen_does_not_catch_the_polynomial_family(self):
        # Why a second predicate is needed at all: the publication screen passes
        # this pattern, so nothing else stands between it and the gate.
        assert security.is_safe_user_regex("a+a+$") is True
        assert security.is_safe_user_regex("(a+)+$") is False

    @pytest.mark.parametrize(
        "pattern",
        [
            "a+a+$",
            r"\w+\d+$",
            ".*.*!",
            "[a-z]*[a-z]+;",
            "a{2,}b{2,}",
            # Grouped spellings: parentheses do not change the backtracking, so
            # treating a group as opaque let these through.
            "(a+)(a+)$",
            "(a+)a+$",
            "a+(a+)$",
            "(?:a+)(?:a+)$",
            "(ab)+(cd)+$",
            "((a+))(a+)$",
        ],
    )
    def test_polynomial_shapes_are_flagged(self, pattern):
        assert security._polynomial_backtracking_prone(pattern) is True

    @pytest.mark.parametrize("pattern", ["(a+)(a+)$", "(a+)a+$", "(?:a+)(?:a+)$"])
    def test_grouped_spellings_keep_the_bounded_engine(self, pattern):
        # The whole point: each of these measured multiple SECONDS unbounded.
        assert security._DenyMatcher(pattern)._bounded is True

    @pytest.mark.parametrize(
        "pattern",
        [
            r"rm\s+-rf\s+/",
            r"ada[^;&#>|\n]*credentials",
            r"curl.*169\.254\.169\.254",
            "a+b",
            r"(?:sudo\s+)?shutdown",
        ],
    )
    def test_ordinary_rules_are_not_flagged(self, pattern):
        # A literal between the quantified units is the common shape; flagging it
        # would cost the fast path for nearly every real rule.
        assert security._polynomial_backtracking_prone(pattern) is False

    def test_a_flagged_pattern_keeps_the_bounded_engine(self):
        matcher = security._DenyMatcher("a+a+$")
        assert matcher._bounded is True, "must not take the unbounded full-input path"

    def test_the_flagged_pattern_evaluates_fast_on_a_long_input(self):
        # The actual property under test is wall-clock: on the bounded engine a
        # 20k-character command must not stall the gate. Unbounded, this input
        # would take minutes.
        matcher = security._DenyMatcher("a+a+$")
        subject = "a" * 20000 + "!"
        start = time.perf_counter()
        matcher.match(subject)
        elapsed = time.perf_counter() - start
        # flake-ok: pre-existing 1 s wall-clock ceiling on a 20k-character input that takes minutes unbounded, moved verbatim; tracked for a de-flake pass
        assert elapsed < 1.0, f"deny evaluation took {elapsed:.1f}s — gate would stall"


class TestDataConsumerGuardIsChargedPerCommandNotPerPayload:
    """The deny walk must not be quadratic in nested payload COUNT.

    ``_data_consumer_exempt`` is called once per extracted payload, and three of
    its guards read only ``tokens`` -- a value the caller binds once, outside the
    payload loop.  One of those guards sweeps the whole argv with
    ``_SCRIPT_EXECUTES_RE``, so re-asking per payload cost N x len(tokens):
    18,000 payloads took ~209s measured, against ~2.5s once the answer is
    charged once.

    These assertions are STRUCTURAL on purpose.  A wall-clock bound would claim
    a performance budget for every other pass in the gate and would flake on a
    slower runner, so what is pinned is the bounded QUANTITY -- how many times
    the command-level answer is computed -- which is the property the fix
    actually establishes.
    """

    @staticmethod
    def _spaced(n: int) -> str:
        """The issue's repro shape: ``bash -c a0pay -c a1pay ...``."""
        return "bash" + "".join(f" -c a{i}pay" for i in range(n))

    @staticmethod
    def _count_guard_calls(monkeypatch, cmd: str) -> int:
        calls = {"n": 0}
        real = security._data_consumer_command_disqualified

        def counting(tokens):
            calls["n"] += 1
            return real(tokens)

        monkeypatch.setattr(security, "_data_consumer_command_disqualified", counting)
        security.is_denied(cmd)
        return calls["n"]

    @staticmethod
    def _count_argv_sweeps(monkeypatch, cmd: str) -> int:
        calls = {"n": 0}
        real = security._SCRIPT_EXECUTES_RE

        class Counting:
            def search(self, s):
                calls["n"] += 1
                return real.search(s)

            def __getattr__(self, name):
                return getattr(real, name)

        monkeypatch.setattr(security, "_SCRIPT_EXECUTES_RE", Counting())
        security.is_denied(cmd)
        return calls["n"]

    def test_guard_is_charged_once_however_many_payloads(self, monkeypatch):
        # The count must not scale with the payload count.  Measured: 1 at every
        # size here; before the fix the guard's work was re-done per payload.
        counts = {n: self._count_guard_calls(monkeypatch, self._spaced(n)) for n in (30, 60, 120)}
        assert (
            counts[30] == counts[60] == counts[120]
        ), f"command-level guard is charged per payload, not per command: {counts}"
        # Belt as well as braces: a future change making it 2*N would still keep
        # the three counts EQUAL to each other only by accident, so bound it
        # against the payload count directly.
        assert counts[120] < 30, f"guard charged {counts[120]} times for 120 payloads"

    @pytest.mark.parametrize("n", [30, 60, 120])
    def test_argv_sweep_is_linear_in_the_argv_not_quadratic_in_payloads(self, monkeypatch, n):
        # ``_SCRIPT_EXECUTES_RE`` sweeps every token.  Charged once per command
        # that is O(len(tokens)); charged once per payload it was
        # O(len(tokens) x payloads).  Measured before the fix: 1830 / 7260 /
        # 28920 sweeps at n = 30 / 60 / 120 (ratio ~3.98, quadratic).  After:
        # 61 / 121 / 241 (ratio ~1.99, linear).  The bound below is ~3x the argv
        # length, which the linear form clears with room and the quadratic form
        # misses by a factor of forty at n=120.
        cmd = self._spaced(n)
        argv_len = len(security.normalize_shell_command(cmd))
        sweeps = self._count_argv_sweeps(monkeypatch, cmd)
        assert sweeps <= 3 * argv_len, (
            f"argv sweep is quadratic in payload count: {sweeps} sweeps for an "
            f"argv of {argv_len} tokens ({n} payloads)"
        )

    @staticmethod
    def _count_argv_elements(monkeypatch, cmd: str) -> "tuple[int, int]":
        """(argv elements consumed, argv length) for one ``is_denied`` call.

        The argv is handed out as a list subclass whose iterator counts the
        elements taken from it, which is what separates ONE hoisted pass over
        the argv from one pass PER PAYLOAD.
        """
        counted = {"n": 0}

        class CountingList(list):
            def __iter__(self):
                for item in list.__iter__(self):
                    counted["n"] += 1
                    yield item

        real = security._shell_tokens

        def wrapped(*args, **kwargs):
            return CountingList(real(*args, **kwargs))

        monkeypatch.setattr(security, "_shell_tokens", wrapped)
        security.is_denied(cmd)
        return counted["n"], len(real(cmd))

    @pytest.mark.parametrize("n", [30, 60, 120, 240])
    def test_argv_is_walked_per_command_not_per_payload(self, monkeypatch, n):
        # The second half of the guard: recovering a payload's token positions with
        # ``[i for i, tok in enumerate(tokens) if tok == payload]`` walks the
        # whole argv once per payload.  Hoisting the guard alone leaves the walk
        # quadratic -- measured 1.37s / 4.30s / 15.56s at 4k / 8k / 16k payloads,
        # ratios 3.15 and 3.62 -- so this half is load-bearing, not tidying.
        #
        # Measured argv elements consumed, 30 / 60 / 120 / 240 payloads:
        #   both hoists      1154 /  2294 /  4574 /   9134   (ratio ~2.00, linear)
        #   position reverted 2984 /  9554 / 33494 / 124574   (ratio ~3.72, quadratic)
        # The bound below sits at 40x the argv length: the linear form uses ~19x
        # and the quadratic form ~259x at n=240.
        cmd = self._spaced(n)
        elements, argv_len = self._count_argv_elements(monkeypatch, cmd)
        assert elements <= 40 * argv_len, (
            f"argv walked per payload: {elements} elements consumed for an argv of "
            f"{argv_len} tokens ({n} payloads) -- expected O(argv), not O(argv x payloads)"
        )

    @pytest.mark.parametrize(
        "cmd",
        [
            # pipes into an evaluator -- the printed text IS the command
            f"echo {_NAME} {_TOK} | sh",
            f"echo '{_PK} -f {_NAME}' | bash",
            # substitution occupies program position -- its OUTPUT runs
            f"$(printf echo) {_NAME} {_TOK}",
            f"`printf echo` {_PK} -f {_NAME}",
            # the script text can EXECUTE rather than print
            f"awk 'system(\"{_PK} -f {_NAME}\")'",
            f"awk 'BEGIN{{print | \"{_PK} -f {_NAME}\"}}'",
            # a control operator inside the token starts a command that runs
            f"echo foo;{_PK} -f {_NAME}",
        ],
    )
    def test_every_way_the_exemption_is_refused_still_refuses(self, cmd):
        # Hoisting must not widen the exemption.  A faster deny gate that misses
        # one case is strictly worse than a slow one, so each documented refusal
        # route is pinned here alongside the cost assertions above.
        assert _denied_by(cmd) is not None

    @pytest.mark.parametrize(
        "cmd",
        [
            f"echo {_NAME} {_TOK}",
            f"printf '{_NAME} {_TOK}'",
            "awk '{print $1}' file",
            "sed 's/a/b/' file",
        ],
    )
    def test_the_ordinary_data_consumer_is_still_exempt(self, cmd):
        # The other direction: hoisting must not NARROW the exemption either, or
        # the change trades a latency fix for a false positive.
        assert _denied_by(cmd) is None

    @pytest.mark.parametrize(
        "cmd",
        [
            f"echo {_NAME} {_TOK}",
            f"echo {_NAME} {_TOK} | sh",
            f"awk 'system(\"{_PK} -f {_NAME}\")'",
            "awk '{print $1}' file",
            f"$(printf echo) {_NAME} {_TOK}",
        ],
    )
    def test_the_command_level_verdict_is_the_callers_to_supply(self, cmd):
        # ``command_disqualified`` is required, so a caller cannot reach the guards
        # without having charged them once for its own argv. Omitting it is a
        # TypeError rather than a silent per-token recomputation, which is the
        # shape that costs one whole-argv sweep per candidate token.
        tokens = security.normalize_shell_command(cmd)
        programs = security._argv_programs(tokens)
        hoisted = security._data_consumer_command_disqualified(tokens)
        with pytest.raises(TypeError):
            security._data_consumer_exempt(0, tokens[0], programs, tokens)
        for i, token in enumerate(tokens):
            # The supplied verdict governs: a disqualified command earns no
            # exemption for any token, whatever that token looks like.
            assert (
                security._data_consumer_exempt(
                    i, token, programs, tokens, command_disqualified=True
                )
                is False
            ), f"token {i} ({token!r}) was exempted by a disqualified command"
            supplied = security._data_consumer_exempt(
                i, token, programs, tokens, command_disqualified=hoisted
            )
            assert isinstance(supplied, bool)


class TestDataConsumerGuardIsChargedPerFrameNotPerTriggerToken:
    """The self-protection floors must not be quadratic in TRIGGER-token count.

    Four floors walk one fixed argv per frame and ask ``_data_consumer_exempt``
    about each token that passes a narrow trigger predicate: the self-program and
    self-module names (credential mint), a kill-family program name (self kill), a
    resolved self-program index (self subcommand), and an ssh-family verb (ssh to
    self). The command-level half of that guard reads only ``tokens``, and one of
    its members sweeps the whole argv with ``_SCRIPT_EXECUTES_RE``, so charging it
    per trigger token costs N x len(tokens): a 24KB command carrying 1,600
    kill-family words as arguments of a data consumer took ~14s, which crosses a
    25s-class watchdog around 2,200 such words.

    A per-frame memo makes the charge one per frame. It is computed LAZILY, at the
    first trigger token, so the far more common command that reaches these floors
    and trips no trigger predicate pays nothing at all -- the property
    ``test_a_command_with_no_trigger_token_pays_nothing`` pins, and the reason a
    memo is preferable to an unconditional per-frame hoist.

    The assertions are STRUCTURAL, matching the payload-axis class above: a
    wall-clock ratio cannot separate this property from the runner, and a wall-clock
    bound tight enough to catch the quadratic on a slow host passes it on a fast one
    -- the 24KB repro above lands under 4s on some hosts and near 14s on others. So
    what is pinned is the bounded QUANTITY: how often the command-level answer is
    computed, and how many times the argv is swept for it.
    """

    @staticmethod
    def _triggers(n: int) -> str:
        """The issue's repro shape: kill-family words as arguments of ``echo``."""
        return "echo " + " ".join([_PK, _NAME] * n)

    @staticmethod
    def _count_guard_calls(monkeypatch, cmd: str) -> "tuple[int, str | None]":
        calls = {"n": 0}
        real = security._data_consumer_command_disqualified

        def counting(tokens):
            calls["n"] += 1
            return real(tokens)

        monkeypatch.setattr(security, "_data_consumer_command_disqualified", counting)
        verdict = security.is_denied(cmd)
        return calls["n"], verdict

    def test_guard_is_charged_the_same_however_many_trigger_tokens(self, monkeypatch):
        # Measured before the memo: 700 / 1400 / 2100 calls at n = 100 / 200 / 300
        # -- exactly 7n, one per trigger token per reaching floor. After: 7 at
        # every size, one per frame per reaching floor.
        counts = {}
        for n in (100, 200, 300):
            counts[n], verdict = self._count_guard_calls(monkeypatch, self._triggers(n))
            # The verdict has to be reached THROUGH the instrumented path, or the
            # counts are counting nothing. ``echo`` makes these words data.
            assert verdict is None, f"n={n} changed the exemption verdict: {verdict!r}"
        assert counts[100] == counts[200] == counts[300], (
            "the command-level guard is charged per trigger token, not per frame: " f"{counts}"
        )
        # Equal counts alone could hold by accident for a shape that is still a
        # multiple of the trigger count, so bound the count directly too.
        assert counts[300] < 30, f"guard charged {counts[300]} times for {300 * 2} trigger tokens"

    def test_a_command_with_no_trigger_token_pays_nothing(self, monkeypatch):
        # The memo is lazy, so the common command reaching these floors without
        # tripping a trigger predicate must not pay the sweep an unconditional
        # per-frame hoist would charge it.
        calls, verdict = self._count_guard_calls(monkeypatch, f"ls -la /var/log {_NAME}.log")
        assert verdict is None
        assert calls == 0, f"a command with no trigger token paid {calls} argv sweeps"

    @pytest.mark.parametrize(
        "cmd",
        [
            # One per trigger predicate, each reaching its floor and each exempt.
            f"echo {_PK} {_NAME}",
            f"echo {_NAME} {_TOK}",
            f"echo {_NAME} restart",
            "echo ssh localhost",
            # And cases where the exemption is REFUSED, so the memo is consulted on
            # the deny side as well.
            f"echo {_PK} {_NAME} | sh",
            f"echo {_NAME} {_TOK} | sh",
            "echo ssh localhost | sh",
            f"$(printf echo) {_NAME} {_TOK}",
            # Two frames, so the memo is built more than once in one call.
            f"echo {_PK} {_NAME}; sed 's/a/{_PK} -f {_NAME}/e' f",
            f"echo {_NAME} {_TOK}; $(printf echo) {_NAME} {_TOK}",
        ],
    )
    def test_the_memo_reaches_the_same_verdict_as_recomputing_every_call(self, monkeypatch, cmd):
        # Charging the guard once per frame may not move any verdict: it is a pure
        # function of ``tokens``, which a frame binds once. Compare the real verdict
        # against one where the memo is discarded and the answer recomputed from the
        # frame's tokens at every single call.
        #
        # BOTH namespaces are patched, and neither is redundant. Each caller binds
        # ``_data_consumer_exempt`` as its own module global via ``from
        # .shell_normalizer import ...``: the four frame loops in ``argv_floor``, and
        # the payload walk in the ``security`` package body. The facade mirrors an
        # attribute write onto ONE owning submodule -- the normalizer, for this name --
        # so a facade write alone leaves ``argv_floor`` resolving the real function and
        # instruments nothing here. Which caller a given command reaches also varies:
        # ``awk 'system(...)'`` carries its kill inside one quoted token, so no frame
        # loop sees a trigger word and only the payload walk judges it.
        #
        # ``calls`` is asserted non-zero for that reason. A wrong or incomplete patch
        # target then reads as a RED test rather than a comparison of the real
        # function against itself, which would pass whatever the memo did.
        #
        # That assertion is also why ``awk 'system(...)'`` is absent from the cases
        # above: its kill is denied by a different tier and the guard is never asked,
        # so it would trip the non-zero check while proving nothing about the memo.
        # The sibling class covers that shape under refused exemptions.
        real = _argv_floor._data_consumer_exempt
        assert security._data_consumer_exempt is real, "the two callers hold one object"
        calls = {"n": 0}

        def recomputing_every_call(index, token, programs, tokens, *, command_disqualified):
            calls["n"] += 1
            return real(
                index,
                token,
                programs,
                tokens,
                command_disqualified=security._data_consumer_command_disqualified(tokens),
            )

        with_memo = security.is_denied(cmd)
        monkeypatch.setattr(_argv_floor, "_data_consumer_exempt", recomputing_every_call)
        monkeypatch.setattr(security, "_data_consumer_exempt", recomputing_every_call)
        without_memo = security.is_denied(cmd)
        assert calls["n"] > 0, (
            "the instrument observed nothing -- the patch target is not the namespace "
            f"the frame loops resolve through, so this comparison is vacuous ({cmd!r})"
        )
        assert with_memo == without_memo, (
            f"the per-frame memo changed the verdict for {cmd!r}: "
            f"memo={with_memo!r} recomputed={without_memo!r}"
        )

    @pytest.mark.parametrize(
        "floor",
        [
            "_is_credential_mint",
            "_is_self_kill",
            "_matches_self_subcommand",
            "_is_ssh_to_self",
        ],
    )
    def test_the_memo_is_declared_inside_the_frame_loop(self, floor):
        """The memo's SCOPE is the frame, and that is asserted on the source.

        Hoisting the declaration one level further out would compute the answer
        from the first frame's tokens and reuse it for every later frame -- a
        different command-level verdict silently applied to a different argv.

        This is asserted structurally rather than behaviourally because the
        behaviour is not reachable: each floor returns as soon as a frame denies,
        so a frame whose guard answer differs from an earlier frame's is only ever
        visited when the earlier frame did not deny, and no command was found that
        both survives its first frame and disagrees with it. The scope is still the
        correct shape, so it is pinned where it is visible.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(security, floor))))

        def memo_targets(node) -> "list[int]":
            found = []
            for sub in ast.walk(node):
                targets = []
                if isinstance(sub, ast.Assign):
                    targets = sub.targets
                elif isinstance(sub, ast.AnnAssign):
                    targets = [sub.target]
                for t in targets:
                    if isinstance(t, ast.Name) and t.id == "disqualified":
                        value = sub.value
                        if isinstance(value, ast.Constant) and value.value is None:
                            found.append(sub.lineno)
            return found

        frame_loops = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == "tokens"
        ]
        assert len(frame_loops) == 1, f"{floor} no longer has exactly one frame loop"
        loop = frame_loops[0]
        inside = [ln for stmt in loop.body for ln in memo_targets(stmt)]
        assert inside, f"{floor} declares no per-frame memo inside its frame loop"
        all_declarations = memo_targets(tree)
        assert sorted(all_declarations) == sorted(inside), (
            f"{floor} declares the memo outside its frame loop as well "
            f"(inside={sorted(inside)} all={sorted(all_declarations)}) -- an outer "
            "declaration carries one frame's command-level answer into the next"
        )

    @pytest.mark.parametrize(
        "cmd",
        [
            # A data-consumer mention beside a real invocation: the real one must
            # still be judged, whichever frame it lands in.
            f"echo {_PK} {_NAME}; sed 's/a/{_PK} -f {_NAME}/e' f",
            f"echo {_NAME} {_TOK}; $(printf echo) {_NAME} {_TOK}",
            f"echo {_PK} {_NAME}; echo {_PK} {_NAME} | sh",
            "echo ssh localhost; echo ssh localhost | sh",
        ],
    )
    def test_a_mention_beside_a_real_invocation_is_still_denied(self, cmd):
        assert (
            security.is_denied(cmd) is not None
        ), f"a real invocation beside a mention went unjudged: {cmd!r}"

    @pytest.mark.parametrize("n", [50, 100, 150])
    def test_the_argv_sweep_is_linear_in_the_argv_not_quadratic_in_triggers(self, monkeypatch, n):
        """Backstop against the cost the guard-call counts cannot see.

        Those counts pin how often the command-level guard is ASKED. This one pins
        the expensive thing inside it -- ``_SCRIPT_EXECUTES_RE`` sweeping every
        token -- so a regression that re-pays the sweep somewhere else would still
        be caught. Measured per trigger token the sweeps are 35,350 / 140,700 /
        316,050 at n = 50 / 100 / 150, which is 350x / 700x / 1050x the argv length:
        the multiplier itself grows, which is what quadratic means here. Charged per
        frame it is exactly 7x the argv length at every size. The bound below leaves
        the linear form room and the quadratic form misses it by 35x at n=50.
        """
        cmd = self._triggers(n)
        argv_len = len(security.normalize_shell_command(cmd))
        calls = {"n": 0}
        real = security._SCRIPT_EXECUTES_RE

        class Counting:
            def search(self, text):
                calls["n"] += 1
                return real.search(text)

            def __getattr__(self, name):
                return getattr(real, name)

        monkeypatch.setattr(security, "_SCRIPT_EXECUTES_RE", Counting())
        assert security.is_denied(cmd) is None
        assert calls["n"] <= 10 * argv_len, (
            f"the argv sweep is quadratic in trigger count: {calls['n']} sweeps for "
            f"an argv of {argv_len} tokens ({n * 2} trigger tokens)"
        )
