"""Differential + complexity guards for the two ``security.py`` linearity fixes.

Both fixes are performance-only and MUST be behaviour-preserving, so the tests
here are written as *differentials*: the expected values were captured from the
implementation as it stood immediately BEFORE each change (origin/main
``760d8f570``) and are pinned as literals. A verdict or byte that moves in either
direction fails.

Covered:

* Mesh-3654 -- ``redact_credentials`` pass 1 was ``for m in
  _CREDENTIAL_PATTERNS.finditer(result): result = result.replace(...)``, which
  rebuilt the whole string per match (O(n^2) on credential-dense text). It is now
  a single ``_CREDENTIAL_PATTERNS.sub(...)``. The redacted text AND the
  ``warnings`` list (content *and* order) must be unchanged.
* Mesh-3693 -- eleven branches of the sensitive-path regex were anchored
  ``(?:^|.*[\\s'\\"=:,;])``. The leading ``.*`` is redundant under ``re.search``
  (which retries at every offset) and made matching quadratic in the longest
  line. The anchor is now ``(?:^|[\\s'\\"=:,;])``. This is a DENY surface, so the
  verdict tests below replay positives and negatives to make it obvious that
  nothing became more permissive.
* #8338 -- the SAME defect one branch over. Mesh-3693 fixed the eleven
  verb-independent branches but left branch (1), whose four alternatives still
  carry a leading ``.*`` (``_READ_CMDS.*``, ``_WRITE_CMDS.*``, ``_SCRIPT_OPEN.*``,
  ``.*[<>|]\\s*``), so the scan stayed quadratic: 1.09s at 9KB, 16.03s at 37KB,
  65.11s at 75KB on a clean command. The gate runs synchronously on the event loop
  from ``_resolve_permission``, so a long cron-built command wedged the loop past
  the stall watchdog's budget and the gateway dump-then-exited every couple of
  hours. Branch (1) is now GATED by a cheap superset pre-filter rather than
  rewritten. The cost tests below count expensive SCANS rather than seconds --
  #3080, #4108, #3938 and #2811 are all flakes from wall-clock ratio assertions on
  this very module, so wall clock is deliberately not asserted.
"""

from __future__ import annotations

import pathlib
import re
import time
from unittest import mock

import pytest

from kiro_crew.security import (
    is_sensitive_bash_command,
    is_sensitive_path,
    redact_credentials,
)

# ─────────────────────────────────────────────────────────────────────────────
# Mesh-3654: redact_credentials pass 1 -- single sub() must be byte-identical
# ─────────────────────────────────────────────────────────────────────────────

# (input, expected_redacted_text, expected_warnings) captured from the
# pre-change loop implementation. Secret-shaped fixtures are written as adjacent
# literals so no single source line is a complete provider token (matches the
# convention in test_security.py, which keeps secret scanners quiet).
_AKIA = "AKIAIOSFODNN7EXAMPLE"
_GHP = "ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12"
_ANT = "sk-ant-api03-" "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP"
_GLPAT = "glpat-" "xxxx1234xxxx5678xxxx"
_XOXB = "xoxb-" "1234567890-abcdefghij"
_TAG = "[REDACTED: credential]"

REDACTION_GOLDEN: list[tuple[str, str, list[str]]] = [
    (
        f"Found key {_AKIA} in output",
        f"Found key {_TAG} in output",
        ["Redacted credential pattern (20 chars)"],
    ),
    # Two occurrences of the SAME credential: both spans replaced, two warnings.
    # This is the case the old `str.replace(matched, tag, 1)` shape depended on
    # positional luck for -- sub() splices each matched span in place.
    (
        f"a {_AKIA} b {_AKIA} c",
        f"a {_TAG} b {_TAG} c",
        [
            "Redacted credential pattern (20 chars)",
            "Redacted credential pattern (20 chars)",
        ],
    ),
    # Three DIFFERENT credentials -- pins warning ORDER (20, 38, 26 chars),
    # which is the ordering guarantee sub() has to preserve.
    (
        f"first {_AKIA} then {_GHP} and {_XOXB} tail",
        f"first {_TAG} then {_TAG} and {_TAG} tail",
        [
            "Redacted credential pattern (20 chars)",
            "Redacted credential pattern (38 chars)",
            "Redacted credential pattern (26 chars)",
        ],
    ),
    (
        "SecretAccessKey=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        _TAG,
        ["Redacted credential pattern (56 chars)"],
    ),
    (
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG",
        _TAG,
        ["Redacted credential pattern (45 chars)"],
    ),
    (
        f"Token is {_XOXB}",
        f"Token is {_TAG}",
        ["Redacted credential pattern (26 chars)"],
    ),
    (f"KEY={_GHP}", f"KEY={_TAG}", ["Redacted credential pattern (38 chars)"]),
    (f"KEY={_ANT}", f"KEY={_TAG}", ["Redacted credential pattern (55 chars)"]),
    (f"KEY={_GLPAT}", f"KEY={_TAG}", ["Redacted credential pattern (26 chars)"]),
    (
        "mongodb://user:supersecretpassword@cluster0.example.net/db",
        f"{_TAG}cluster0.example.net/db",
        ["Redacted credential pattern (35 chars)"],
    ),
    (
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1r",
        _TAG,
        ["Redacted credential pattern (48 chars)"],
    ),
    # Negatives: the cheap superset gate must still short-circuit to identity.
    (
        "See the PRIVATE KEY handling section of the runbook.",
        "See the PRIVATE KEY handling section of the runbook.",
        [],
    ),
    (
        "just some ordinary log line with no secrets at all",
        "just some ordinary log line with no secrets at all",
        [],
    ),
    ("", "", []),
]


@pytest.mark.parametrize(
    ("text", "expected_text", "expected_warnings"),
    REDACTION_GOLDEN,
    ids=[f"case-{i}" for i in range(len(REDACTION_GOLDEN))],
)
def test_pass1_single_sub_is_byte_identical_to_pre_change_loop(
    text: str, expected_text: str, expected_warnings: list[str]
) -> None:
    """Pass 1 as one ``sub()`` reproduces the old loop's bytes and warnings.

    Differential for Mesh-3654. ``expected_warnings`` is compared with ``==`` on
    the list, so both the CONTENT and the ORDER are pinned -- appending in the
    replacement callback has to keep the left-to-right match order the old
    ``finditer`` loop had.
    """
    result, warnings = redact_credentials(text)
    assert result == expected_text
    assert warnings == expected_warnings


def test_pass1_warning_order_tracks_match_order_not_length() -> None:
    """Warnings come out in match order, not sorted or grouped.

    A replacement callback that batched or reordered its appends would still
    produce identical TEXT, so this asserts the ordering separately.
    """
    text = f"{_ANT} {_AKIA} {_GHP}"
    _, warnings = redact_credentials(text)
    assert warnings == [
        f"Redacted credential pattern ({len(_ANT)} chars)",
        f"Redacted credential pattern ({len(_AKIA)} chars)",
        f"Redacted credential pattern ({len(_GHP)} chars)",
    ]


def test_pass1_warnings_still_carry_no_secret_bytes() -> None:
    """The replacement callback must not slice the match into the warning."""
    text = f"KEY={_ANT}"
    _, warnings = redact_credentials(text)
    joined = " ".join(warnings)
    assert _ANT not in joined
    assert _ANT[:20] not in joined
    assert "Redacted credential pattern" in joined


def test_pass1_is_linear_on_credential_dense_text() -> None:
    """Complexity guard for Mesh-3654.

    The old shape rebuilt the whole string per match, so redacting N credentials
    in an N-credential string was O(N^2). 4000 credentials (~84 KB) is
    sub-second as one ``sub()`` pass; the generous ceiling keeps this off slow
    CI's flake list while still failing hard if the per-match rebuild returns.
    """
    dense = f"{_AKIA} " * 4000
    started = time.perf_counter()
    result, warnings = redact_credentials(dense)
    elapsed = time.perf_counter() - started
    assert len(warnings) == 4000
    assert _AKIA not in result
    assert elapsed < 5.0, f"pass 1 took {elapsed:.2f}s -- per-match string rebuild is back"


# ─────────────────────────────────────────────────────────────────────────────
# Mesh-3693: sensitive-path anchor -- zero verdict change (DENY surface)
# ─────────────────────────────────────────────────────────────────────────────

# (command, expected_verdict) captured from the pre-change regex. Ordered so the
# separator-boundary cases the character class exists for are explicit: the path
# preceded by a space, a single quote, a double quote, `=`, `:`, `,`, `;`, and at
# string start -- plus mid-token cases that must stay NEGATIVE.
SENSITIVE_COMMAND_GOLDEN: list[tuple[str, bool]] = [
    # ── separator boundaries: each must stay a HIT ──
    ("cat ~/.aws/credentials", True),  # space
    ("cat '~/.aws/credentials'", True),  # single quote
    ('cat "~/.ssh/id_rsa"', True),  # double quote
    ("FOO=~/.aws/credentials", True),  # `=` (VAR=path)
    ("PATH=/x:~/.ssh/id_rsa", True),  # `:` (PATH-style list)
    ("cmd --a=1,~/.aws/credentials", True),  # `,`
    ("run;~/.aws/credentials", True),  # `;`
    ("~/.aws/credentials", True),  # start-of-string (`^`)
    ("echo x\n~/.aws/credentials", True),  # newline is in the class
    # ── the same hits further into the line: `.*` was never what found these,
    #    `re.search` retrying at every offset was ──
    ("a b c d e f g h ~/.aws/credentials", True),
    ("prefix text then FOO=bar:~/.aws/credentials suffix", True),
    ("deploy --flag ~/.ssh/id_rsa", True),
    ("a,~/.gnupg/secring.gpg", True),
    # ── other spellings that route through the rewritten branches ──
    ("cat $HOME/.aws/credentials", True),
    ("type %USERPROFILE%\\.aws\\credentials", True),
    ("type $env:USERPROFILE\\.ssh\\id_rsa", True),
    ("cp ~/.kiro/agents/x.json /tmp/y", True),
    # ── embedded MID-TOKEN: no separator immediately before the path, so the
    #    anchor must NOT fire. These are the cases that would flip to True if
    #    the character class were dropped along with the `.*`. ──
    ("xyz~/.aws/credentials", False),
    ("FOO=bar~/.aws/credentials", False),
    ("VAR=x~/.gnupg/secring.gpg", False),
    ("printf q~/.aws/credentials", False),
    # ── ordinary commands: must stay allowed ──
    ("ls -la", False),
    ("echo hello world", False),
    ("cat myfile.txt", False),
    ("notaws/credentials", False),
    ("python -c 'print(1)'", False),
    ("grep -r pattern src/", False),
    ("cat ./relative/notsensitive.json", False),
    ("git status", False),
    ("make build", False),
]


@pytest.mark.parametrize(("command", "expected"), SENSITIVE_COMMAND_GOLDEN)
def test_sensitive_bash_verdicts_unchanged_by_anchor_rewrite(command: str, expected: bool) -> None:
    """Differential for Mesh-3693 on ``is_sensitive_bash_command``.

    Every verdict is pinned to what the pre-change regex returned. Dropping the
    redundant ``.*`` cannot change any of them: the alternative is still ``^`` or
    a single separator character, and ``re.search`` already retried at every
    offset. A regression in EITHER direction fails here -- the negatives are what
    make it obvious the gate did not become more permissive.
    """
    assert bool(is_sensitive_bash_command(command)) is expected


SENSITIVE_PATH_GOLDEN: list[tuple[str, bool]] = [
    ("~/.aws/credentials", True),
    ("~/.ssh/id_rsa", True),
    ("~/.gnupg/secring.gpg", True),
    ("/tmp/harmless.txt", False),
    ("./README.md", False),
    ("src/kiro_crew/security.py", False),
    ("notes.md", False),
]


@pytest.mark.parametrize(("path", "expected"), SENSITIVE_PATH_GOLDEN)
def test_sensitive_path_verdicts_unchanged_by_anchor_rewrite(path: str, expected: bool) -> None:
    """Differential for Mesh-3693 on ``is_sensitive_path``."""
    assert bool(is_sensitive_path(path)) is expected


def test_sensitive_anchor_has_no_leading_wildcard() -> None:
    """Guard: the redundant ``.*`` must not come back, on either pattern.

    Asserted against the COMPILED forms now rather than the source text. The
    anchor used to be spelled once per branch, so counting source occurrences was
    the only way to check every branch carried it; it is now written once and
    applied to a tuple of tails (#8338), which makes the source count meaningless
    and the compiled count exact. Both patterns are checked, because the
    pre-filter being free of ``.*`` is what makes it linear -- and therefore what
    makes it safe to run in front of the pattern on every command.
    """
    from kiro_crew import security as security_mod

    pattern, prefilter_alternatives = security_mod._build_sensitive_patterns()
    prefilter = security_mod._build_sensitive_prefilter(prefilter_alternatives)

    wildcard_anchor = r"""(?:^|.*[\s'\"=:,;])"""
    assert wildcard_anchor not in pattern.pattern, (
        "a leading `.*` is back in the sensitive-path anchor -- it is redundant "
        "under re.search and makes matching quadratic in the longest line"
    )
    # And the fixed form is still there, on every branch it was applied to: the
    # eleven verb-independent branches, and nothing else.
    branch_anchor = r"""(?:^|[\s'\"=:,;])"""
    assert pattern.pattern.count(branch_anchor) == 11
    # The pre-filter is the pattern's own branch list with exactly ONE
    # substitution: branch (1) replaced by its bare tail. So it carries the same
    # eleven anchored tails plus that bare tail plus the two anchor-free ones.
    assert len(prefilter_alternatives) == 14

    # The pre-filter must stay free of `.*`. Branch (1) is the only alternative
    # that has one, and the pre-filter takes its TAIL instead. A `.*` here would
    # make the pre-filter as quadratic as the scan it exists to avoid, and it
    # would be charged to every command.
    assert ".*" not in prefilter.pattern, (
        "the sensitive-path pre-filter has acquired a `.*` -- it is evaluated on "
        "every command, so it must stay linear"
    )
    # The token anchor must be KEPT on those eleven tails, not dropped. Dropping it
    # would still be a sound superset, but it lets the engine enter the Windows
    # tails at every offset, and those contain `win_gsep`, a starred group: a 10KB
    # UNC-style backslash run then backtracks for 25.4s against 0.008s anchored --
    # past the watchdog, i.e. the pre-filter would introduce the very crash it
    # exists to prevent. Pinned by
    # `test_a_backslash_run_does_not_blow_up_the_prefilter`.
    assert prefilter.pattern.count(branch_anchor) == 11, (
        "the pre-filter dropped its token anchors -- unanchored Windows tails make "
        "the pre-filter itself quadratic on a backslash run"
    )

    # Branch (1)'s redirect alternative must stay free of a leading `.*` too. It
    # was `.*[<>|]\s*`, and that `.*` was the DOMINANT quadratic term: unlike the
    # verb alternatives beside it, which only enter where a verb matches, it
    # entered at every offset unconditionally. It is redundant for the same reason
    # the token anchor's was -- `.search` already retries at every offset -- so
    # removing it is match-set identical, which
    # `test_removing_the_redirect_wildcard_is_match_set_identical` pins.
    assert r"|.*[<>|]" not in pattern.pattern, (
        "a leading `.*` is back on branch (1)'s redirect alternative -- it is "
        "redundant under re.search and was the dominant quadratic term"
    )
    assert r"|[<>|]\s*)" in pattern.pattern, (
        "branch (1)'s redirect alternative is gone or respelled -- the shapes it "
        "covers (`>~/path`, `|~/path`) are matched by no other branch"
    )


#: Every shape branch (1)'s redirect alternative exists for: the fenced path is
#: glued to a redirect operator or a pipe, so the character before it is not in
#: the token-anchor class and branches (2+) cannot match. These are exactly what a
#: wrong simplification of that alternative would silently stop blocking.
REDIRECT_GLUED_POSITIVES = [
    "cat foo >~/.aws/credentials",
    "cat foo >>~/.aws/credentials",
    "cat foo 2>~/.aws/credentials",
    "cat foo <~/.aws/credentials",
    "cat x|~/.ssh/id_rsa",
    "echo hi | tee ~/.ssh/authorized_keys",
]


@pytest.mark.parametrize("command", REDIRECT_GLUED_POSITIVES)
def test_redirect_glued_paths_are_still_blocked(command: str) -> None:
    """The alternative whose `.*` was removed still covers every shape it owns."""
    assert is_sensitive_bash_command(command) is not None, command


def test_a_backslash_run_does_not_blow_up_the_prefilter() -> None:
    """The pre-filter runs on EVERY command, so it must be safe on hostile input.

    The Windows tails contain ``win_gsep``, a starred group. With the token anchor
    dropped from them the engine entered those tails at every offset and a UNC-style
    backslash run backtracked quadratically -- measured 0.066s / 0.258s / 1.019s /
    4.055s at 0.5KB / 1KB / 2KB / 4KB and **25.4s at 10KB**, past the 25s watchdog.
    Keeping the anchor takes the same 10KB subject to 0.008s.

    An absolute ceiling rather than a doubling ratio, following
    ``test_long_nonshell_line_does_not_blow_up`` above: a ratio assertion on this
    module is the direct cause of four separate CI flakes (#3080, #4108, #3938,
    #2811). 2s clears the fixed path by ~250x while the quadratic form overshoots
    by ~12x, so the bound distinguishes them without being tight enough to flake.
    The structural half of this guard -- that the anchors are still there at all --
    is asserted in ``test_sensitive_anchor_has_no_leading_wildcard``.
    """
    command = "copy " + ("\\" * 10_000) + "server\\share\\file.txt c:\\out"
    assert len(command) > 10_000

    started = time.perf_counter()
    verdict = is_sensitive_bash_command(command)
    elapsed = time.perf_counter() - started

    assert verdict is None, "a UNC run naming no fenced path must stay clean"
    assert elapsed < 2.0, (
        f"is_sensitive_bash_command took {elapsed:.2f}s on a 10KB backslash run -- "
        "the pre-filter's Windows tails have lost their token anchor"
    )


def test_removing_the_redirect_wildcard_is_match_set_identical() -> None:
    """Differential for the `.*` removal, against the pattern that still has it.

    Rebuilds the pre-change form by putting the `.*` back into the compiled
    pattern and asserts the two agree on every case in both corpora -- positives,
    the redirect-glued shapes the alternative exists for, and negatives. This is
    the argument stated directly rather than trusted: under ``.search``, which
    retries at every offset, ``.*[<>|]\\s*P`` matches a subject exactly when
    ``[<>|]\\s*P`` does, because a match of the former from any offset implies the
    metacharacter sits at some offset the search also visits.
    """
    from kiro_crew import security as security_mod

    fixed, _ = security_mod._build_sensitive_patterns()
    assert r"|[<>|]\s*)" in fixed.pattern
    with_wildcard = re.compile(
        fixed.pattern.replace(r"|[<>|]\s*)", r"|.*[<>|]\s*)", 1), re.IGNORECASE
    )

    negatives = [
        "echo hello world",
        "npm ci && npm run build",
        'curl -s https://example.com/api --data \'{"k":"v"}\'',
        # Glued to a NAME character, not a redirect: no branch matches this, and
        # it is the subject shape the pre-filter admits but the pattern rejects.
        "abc~/.aws/credentials",
        "ls -la /var/log",
    ]
    for command in PREFILTER_MUST_NOT_MISS + REDIRECT_GLUED_POSITIVES + negatives:
        assert (fixed.search(command) is not None) == (
            with_wildcard.search(command) is not None
        ), f"the `.*` removal changed the verdict for: {command}"


def inspect_source(func: object) -> str:
    """``inspect.getsource`` indirection kept local so the test module has one import."""
    import inspect

    return inspect.getsource(func)  # type: ignore[arg-type]


def test_long_nonshell_line_does_not_blow_up() -> None:
    """Complexity guard for Mesh-3693.

    A ~20 KB newline-free non-shell string is the worst case for the old anchor:
    eleven branches each retried a greedy ``.*`` from every offset. Measured on
    the dev box this took ~27 s before the rewrite and ~1.5 s after, so a 6 s
    ceiling clears the fixed path by ~4x while the quadratic form overshoots by
    ~4.5x. Deliberately generous -- this test exists to catch a complexity
    regression, not to benchmark CI.
    """
    blob = "abcdefgh " * 2500
    assert len(blob) > 20_000
    started = time.perf_counter()
    verdict = is_sensitive_bash_command(blob)
    elapsed = time.perf_counter() - started
    assert bool(verdict) is False
    assert elapsed < 6.0, (
        f"is_sensitive_bash_command took {elapsed:.2f}s on a 20 KB line -- "
        "a leading `.*` in the sensitive-path anchor is quadratic"
    )


def test_credential_pattern_module_still_compiles_one_alternation() -> None:
    """Invariant: the rewritten pass 1 still uses the shared compiled pattern.

    Guards against a future refactor swapping in a locally compiled regex, which
    would silently drop the ``_might_contain_credential`` pre-filter pairing.
    """
    from kiro_crew import security as security_mod

    assert isinstance(security_mod._CREDENTIAL_PATTERNS, re.Pattern)
    body = inspect_source(security_mod.redact_credentials)
    assert "_CREDENTIAL_PATTERNS.sub(" in body
    assert "_might_contain_credential(result)" in body


# ── #8338: branch (1)'s `.*` is gated by a cheap superset pre-filter ──
#
# Two properties matter and are tested separately, because only one of them is
# about speed:
#
#   1. SAFETY -- the pre-filter is a superset, so no command that the pattern
#      would have blocked is now allowed. This is a deny surface; a regression
#      here is a credential bypass, not a slowdown.
#   2. COST -- a clean command must not reach the quadratic pattern at all.
#      Asserted by COUNTING scans, never by timing: #3080, #4108, #3938 and #2811
#      are four separate flakes caused by wall-clock ratio assertions on this
#      module, and a count is exact on every runner.


class _CountingPattern:
    """Wraps the compiled pattern to count how often it is actually scanned."""

    def __init__(self, inner: re.Pattern[str]) -> None:
        self._inner = inner
        self.searches = 0

    def search(self, subject: str):  # noqa: ANN202 - mirrors re.Pattern.search
        self.searches += 1
        return self._inner.search(subject)


def _count_expensive_scans(monkeypatch: pytest.MonkeyPatch) -> _CountingPattern:
    """Install a counting proxy over the module's cached sensitive-path pattern."""
    from kiro_crew import security as security_mod

    counter = _CountingPattern(security_mod._get_sensitive_re())
    monkeypatch.setattr(security_mod, "_SENSITIVE_RE", counter)
    return counter


def _clean_pipeline(repeats: int) -> str:
    """A long, CLEAN command shaped like the cron turns that wedged the loop.

    Dense in the quote/colon/comma characters the branch anchors retry from, and
    carrying the ``//`` of a URL -- which is itself a cost multiplier, because
    ``_SEPARATOR_RUN_RE`` matches any doubled separator, so pass 1b re-scanned the
    whole command once per collapsed variant (three of them) on top of the
    original.
    """
    return " && ".join(
        f"curl --silent --header 'X-K:{i}' https://example.com/api/v1/pages/{i} "
        f'--data \'{{"k{i}":"v{i}","path":"a/b/c/d{i}"}}\''
        for i in range(repeats)
    )


#: Commands the pattern matches, spanning every branch family: POSIX and
#: Windows-native spellings, the ``%APPDATA%``/``%LOCALAPPDATA%`` aliases, a UNC
#: anchor, the ``$KIRO_HOME`` override, the two anchor-free bare leaves, a
#: separator run that only a collapsed variant matches, and the two shapes that
#: ONLY branch (1) matches (a path glued to a redirect or a pipe, where the
#: preceding character is not in the token-anchor class).
PREFILTER_MUST_NOT_MISS = [
    "cat ~/.aws/credentials",
    "cat $HOME/.ssh/id_rsa",
    "cat /home/someone/.netrc",
    "cat /Users/someone/.npmrc",
    "cat ~/.kube/config",
    "cat ~/.config/gcloud/credentials.db",
    "cat ~/.git-credentials",
    "echo x > ~/.kiro/crew/security_policy.json",
    "CAT ~/.AWS/CREDENTIALS",
    "cat ~/.aws/credentials;whoami",
    "type %USERPROFILE%\\.aws\\credentials",
    "Get-Content $env:USERPROFILE\\.ssh\\id_rsa",
    "type %APPDATA%\\kiro-cli\\data.sqlite3",
    "type %LOCALAPPDATA%\\kiro-cli\\data.sqlite3",
    "type !APPDATA!\\kiro-cli\\data.sqlite3",
    "copy \\\\server\\share\\.kiro\\crew\\security_policy.json c:\\x",
    # The home-anchored native spelling of the same Roaming store, which the
    # %APPDATA% alias branch does NOT cover, and the variable-LEAF shape whose
    # branch anchors on the keystone's parent because there is no literal leaf.
    "type %USERPROFILE%\\AppData\\Roaming\\kiro-cli\\data.sqlite3",
    "type %USERPROFILE%\\.kiro\\crew\\%F%",
    'echo forged > "C:\\Users\\u\\.kiro\\crew\\connections-tool-aliases.json"',
    "tee $KIRO_HOME/agents/evil.json",
    "tee %KIRO_HOME%\\agents\\evil.json",
    "cat connections-tool-aliases.json",
    "rm ./ggml-base.bin",
    # Branch (1) only: the character before the path is `>` / `|`, neither of
    # which the token anchor admits, so branches (2+) cannot match these.
    "cat foo >~/.aws/credentials",
    "cat x|~/.ssh/id_rsa",
]


@pytest.mark.parametrize("command", PREFILTER_MUST_NOT_MISS)
def test_prefilter_never_misses_what_the_pattern_matches(command: str) -> None:
    """The pre-filter's one hard contract, over every branch family.

    It may match where the pattern would not; it must NEVER fail to match where
    the pattern would, or the gate silently stops blocking. Both shapes that only
    branch (1) matches are included, because branch (1) is DROPPED from the
    pre-filter rather than stripped -- it is covered by branch (2)'s identical
    tail, and these two cases are what proves that.
    """
    from kiro_crew import security as security_mod

    pattern, prefilter_alternatives = security_mod._build_sensitive_patterns()
    prefilter = security_mod._build_sensitive_prefilter(prefilter_alternatives)

    assert pattern.search(command) is not None, "corpus case no longer matches"
    assert prefilter.search(command) is not None, (
        "the pre-filter missed a command the pattern matches -- this is a "
        "credential-fence bypass, not a performance regression"
    )


@pytest.mark.parametrize("command", PREFILTER_MUST_NOT_MISS)
def test_gate_verdicts_unchanged_by_the_prefilter(command: str) -> None:
    """End to end: every corpus case is still denied through the real gate."""
    assert is_sensitive_bash_command(command) is not None, command


def test_clean_command_does_not_reach_the_quadratic_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix, stated as a count: zero expensive scans on a clean command.

    Before the pre-filter this command cost FOUR full scans of the quadratic
    pattern -- one on the original plus one per separator-collapsed variant -- and
    the count is what the loop-stall crash was made of. A count is asserted rather
    than a duration so the test cannot flake on a loaded runner.
    """
    counter = _count_expensive_scans(monkeypatch)
    command = _clean_pipeline(40)
    assert len(command) > 4_000

    assert is_sensitive_bash_command(command) is None
    assert counter.searches == 0, (
        f"a clean command reached the quadratic pattern {counter.searches} time(s) "
        "-- the pre-filter is no longer gating it"
    )


def test_a_url_no_longer_multiplies_the_expensive_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass 1b's variants must not each pay for a scan.

    ``_SEPARATOR_RUN_RE`` matches any doubled separator, so the ``//`` in an
    ordinary URL produced three collapsed variants and tripled the cost of an
    already-quadratic scan -- measured 16.06s against 4.58s at 37KB for the same
    command with a single slash. The variants are still produced and still
    checked; they just no longer reach the pattern.
    """
    from kiro_crew import security as security_mod

    command = _clean_pipeline(4)
    assert len(security_mod._separator_collapsed_variants(command)) == 3, (
        "the URL no longer yields collapsed variants -- this test is no longer "
        "exercising the multiplier it was written for"
    )

    counter = _count_expensive_scans(monkeypatch)
    assert is_sensitive_bash_command(command) is None
    assert counter.searches == 0


def test_a_fenced_command_still_reaches_the_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-filter admits a real candidate rather than deciding for itself.

    It is a cost gate, not a second matcher: on a command that names a fenced
    path the pattern must still be the thing that returns the verdict.
    """
    counter = _count_expensive_scans(monkeypatch)
    assert is_sensitive_bash_command("cat ~/.aws/credentials") is not None
    assert counter.searches >= 1


def test_prefilter_is_built_from_the_same_home_as_the_pattern() -> None:
    """Both patterns come from ONE build, so neither can describe a stale home.

    ``_build_sensitive_cache`` populates both caches together. A home that is not
    under ``Users``/``home`` has only the resolved literal to match on, which is
    exactly the case where a pre-filter built from a second, later reading of
    ``Path.home()`` would disagree with the pattern.
    """
    from kiro_crew import security as security_mod

    with mock.patch.object(security_mod.Path, "home", return_value=pathlib.Path("D:\\profiles\\u")):
        pattern, prefilter_alternatives = security_mod._build_sensitive_patterns()
        prefilter = security_mod._build_sensitive_prefilter(prefilter_alternatives)

    command = r'type "D:\profiles\u\.aws\credentials"'
    assert pattern.search(command) is not None
    assert prefilter.search(command) is not None
