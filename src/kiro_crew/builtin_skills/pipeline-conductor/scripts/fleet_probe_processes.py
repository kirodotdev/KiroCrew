"""The fleet probe's process side: which ``/proc`` entries a ``BANNED`` line names.

Loaded by ``fleet_probe.py``, which re-exports every name defined here; it is not run on
its own. It holds the built-in banned command shapes, the reading of a runner's argv
(which token is the program, its worker count, its scope), the kernel's view of the
binary a pid runs, the shell-wrapper exemption, and whether the fleet owns a process.
The ``/proc`` walk itself, the line it prints and the ``cmd=`` redactor stay in
``fleet_probe.py``. New work on whether a process is a banned run, or whose it is, goes
here.

Standard library only, importing nothing from the package or from a sibling script, so
the probe stays runnable by a bare interpreter.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

#: Banned-operation cmdline shapes: a pytest whose worker pool BYPASSES the
#: budget, and a bare full-suite vitest with no file arguments.
#:
#: The axis is budgeted versus unbudgeted, not "did the caller write a number".
#: On THIS repo the explicit number is the one spelling that can outgrow the
#: host: ``setup.cfg`` documents that ``auto`` is bounded by the rootdir
#: conftest's ``pytest_xdist_auto_num_workers`` hook, which sizes the pool by
#: available memory and by what concurrent runs on the host already hold, and
#: that "an explicit ``-n <N>`` bypasses the budget" -- the hook is
#: ``firstresult`` and only ever answers ``auto``. The gate runner
#: (``scripts/run_scoped_tests.py::pytest_parallel_args``) therefore emits
#: ``-n auto`` and cannot pass a number, and the default ``addopts`` supply
#: ``-n auto`` to a pytest carrying no ``-n`` at all.
#:
#: So the rule flags a pytest whose EFFECTIVE worker count is an explicit number
#: of two or more, in any spelling -- ``-n 4``, ``-n=4``, ``-n4``, ``-n 32``,
#: ``--numprocesses=4``, ``--numprocesses 2`` -- and stays quiet on every
#: budgeted or single-process form: ``-n auto``, ``-n logical``,
#: ``--numprocesses auto``, a pytest with no ``-n`` (``addopts`` decide, and they
#: say ``auto``), and ``-n0`` / ``-n 1`` / ``-n=0`` / ``--numprocesses 1``, which
#: run one process with xdist inactive. ``-n0`` is the repo's own documented
#: override, so the form a worker is REQUIRED to use is a passing one. Where the
#: same flag is given twice the LAST one wins, as argparse resolves it, so
#: ``-n 4 -n auto`` ends up budgeted and is quiet -- that half is read from the
#: argv tokens (``_argv_worker_pool_is_budgeted``), since a joined command line
#: cannot express "the last one". The tokens settle two more things the joined
#: line cannot: pytest's ``--`` ends its options, so a ``-n0`` behind it is a
#: path and never the run's count; and ``-o addopts=...`` / ``--override-ini
#: addopts=...`` replace the ``addopts`` the run starts from, so their ``-n`` is
#: folded in AHEAD of the run's own tokens, exactly where pytest puts it.
#:
#: What the rule cannot read is the checkout: ``auto`` is budgeted by THIS tree's
#: conftest, and the probe already keys the stop on ``cwd=fleet`` for exactly
#: that reason. The cap decision reads argv and nothing else.
#:
#: The pytest rule matches an INVOCATION rather than a mention, and two guards are
#: what make that distinction; each one answers a false ``BANNED`` against a run
#: that IS budgeted.
#:
#: * The token has to be the runner's own name, optionally path-qualified
#:   (``pytest``, ``/x/.venv/bin/pytest``, ``-m pytest``). A filename that merely
#:   CONTAINS the word names no command, and a bare ``\bpytest\b`` cannot tell the
#:   two apart: ``.`` and ``-`` are non-word characters, so ``pytest.log``,
#:   ``pytest.ini``, ``.pytest_cache`` and ``pytest-cov`` all satisfy it. A worker's
#:   test step runs the capped pytest and then greps the log it wrote, which is
#:   exactly that shape. So the name must be a WHOLE shell token, sitting between two
#:   token boundaries: that covers the filename spellings above, the directory in
#:   ``/tmp/pytest/results.log``, and equally the packaging forms a character-by-
#:   character exclusion list keeps missing -- ``pytest:latest``, ``pytest==7.4.0``,
#:   ``pytest@1.2.3``.
#: * The worker count is read from the SAME command. One cmdline can carry a whole
#:   shell script in a single argument, where the unbudgeted run and a budgeted one
#:   are different lines, so the lookahead stops at a command separator (``;``,
#:   ``&``, ``|``, a newline) instead of scanning the rest of the script. Letting it
#:   span the whole script text is the opposite trade and a worse one: one line's
#:   ``-n 8`` would then condemn every budgeted run beside it.
#:
#:   A separator inside a BRACKETED or QUOTED span is not a command end, though, and
#:   this is where the argv matters: ``/proc`` hands over NUL-separated arguments that
#:   are joined for matching, so a metacharacter sitting inside ONE argument arrives in
#:   the scanned text with no shell involved. A parametrized node id does exactly that
#:   -- ``pytest f.py::t[a|b] -n 8`` is an unbudgeted run whose ``|`` would hide its
#:   own ``-n 8`` behind a barrier -- and the answer is a missed row on the one shape
#:   the counter exists to catch. So the scan crosses a bracketed or quoted span whole,
#:   and only a separator outside both ends the command. An UNQUOTED ``|`` stays a
#:   separator, because there it really is a pipe and the number after it belongs to
#:   the next command.
#:
#: The flag must start a token, so a target like ``test-n1.py`` cannot be read as
#: ``-n 1``. Zero and one are the single-process spellings and are not selected; any
#: other digit run is, and the argv reader has the last word on it -- ``-n 01`` is one
#: process there, and a value pytest would reject (``-n 4abc``) answers fail-closed.
#: Whitespace is spelled ``[\x20\t]`` rather than with a literal space because
#: ``rule=`` prints the pattern verbatim onto a line read as whitespace-separated
#: fields, and a pattern holding a space would split that one field into three.
#:
#: What the pair does NOT separate: a runner name standing alone as some other
#: program's argument (``grep -rn pytest src``) still matches. Telling that from a
#: launcher that really does run the runner (``poetry run pytest``, ``uv run
#: pytest``, an unknown wrapper) needs a list of every launcher rather than a
#: shape, and a list is what silently loses the launcher nobody added. So the
#: residual error stays on the reporting side, like the rest of this scan, and the
#: ``cmd=`` field on the line shows which program the match sat in.
#: What ends a shell TOKEN: whitespace or a metacharacter. The runner name has to sit
#: between two of these (or a string edge) to be a command rather than a fragment of a
#: longer word, and stating that positively is what keeps the rule from growing one
#: excluded character at a time -- a list of characters that continue a token has to
#: name ``.`` for ``pytest.log``, ``-`` for ``pytest-cov``, ``:`` for ``pytest:latest``,
#: ``=`` for ``pytest==7.4.0`` and ``@`` for ``pytest@1.2.3``, and the next packaging
#: spelling is another silent false stop. Spelled with escapes throughout because
#: ``rule=`` prints the pattern verbatim onto a line read as whitespace-separated
#: fields, so a literal space, quote or backtick in it would split or reopen that field.
_SHELL_TOKEN_BOUNDARY = r"[\s;&|<>()\x22\x27\x60]"

#: What the count search may cross on its way from the runner to the run's own ``-n``:
#: any character that is neither a command separator nor the OPENER of a span, plus a
#: bracketed or quoted span taken WHOLE, because a separator inside one of those is
#: data the command carries rather than the end of it. The openers are excluded from
#: the first branch to keep the alternation DISJOINT: leaving ``[``, ``\x27`` and
#: ``\x22`` in it gives every span two parses -- whole, or character by character --
#: so k spans admit 2**k of them. This star sits inside a lookahead that FAILS on
#: every budgeted run -- the ordinary, healthy case -- so the case that must walk
#: every parse is every worker's own ``-n auto`` rerun naming a few dozen
#: parametrized node ids, and there is no timeout or length bound in this script to
#: end the stall. The quotes are spelled ``\x22``/``\x27`` for the same reason the
#: boundary class is -- ``rule=`` prints the pattern verbatim onto a line read as
#: whitespace-separated fields.
_CAP_SCAN = r"(?:[^;&|\n\[\x27\x22]|\[[^\]\n]*\]|\x27[^\x27\n]*\x27|\x22[^\x22\n]*\x22)*"

#: A worker COUNT that bypasses the budget: a run of digits that is not the whole token
#: ``0`` or ``1``, which run a single process with xdist inactive. ``auto`` and
#: ``logical`` are not digits and never reach this. Everything else about the value is
#: deliberately NOT judged here, because the joined line only has to SELECT a candidate
#: and the argv reader decides: ``01`` is selected and read as one process there, and
#: ``4abc`` -- a value pytest rejects at argument parsing -- is selected and answered
#: fail-closed there. An anchor here would let the two authorities disagree.
_UNBUDGETED_COUNT = r"(?![01](?:" + _SHELL_TOKEN_BOUNDARY + r"|$))\d+"

#: The vitest rule's pattern, bound to a name so the scan can recognise the rule
#: it belongs to without depending on where it sits in ``DEFAULT_BANNED_RES``.
#: The pattern SELECTS a candidate out of the joined cmdline; ``argv`` decides
#: whether that candidate is an invocation. See ``_invokes_bare_vitest_run``.
_VITEST_BANNED_RE = r"\bvitest\b\s+run\s*$"

DEFAULT_BANNED_RES = (
    r"(?:(?<=" + _SHELL_TOKEN_BOUNDARY + r")|^)"
    r"(?:[^\s;&|<>()]*/)?pytest"
    r"(?=" + _SHELL_TOKEN_BOUNDARY + r"|$)"
    r"(?="
    + _CAP_SCAN
    + r"(?<![\w./-])(?:-n|--numprocesses)[\x20\t]*=?[\x20\t]*"
    + _UNBUDGETED_COUNT
    + r")",
    _VITEST_BANNED_RE,
)

#: Token bases that identify the test runner inside a ``/proc`` argv.
_RUNNER_BASES = frozenset(
    {"pytest", "pytest.exe", "py.test", "py.test.exe", "vitest", "vitest.cmd"}
)

#: A versioned runner alias, as a whole argv TOKEN base: ``pytest-3``,
#: ``pytest-3.12``, ``py.test-3``. Distributions install the runner under this name
#: so several interpreter versions can each own one, and it is a real invocation.
#:
#: This is deliberately NOT added to the joined-line rule above, and the reason is the
#: whole design of this pair. The rule matches a joined command line, where ``pytest-3``
#: as the PROGRAM and ``pytest-3`` as a path component or a package name are the same
#: characters in the same position -- so making the alias matchable there also makes
#: ``ls /var/tmp/pytest-of-ci/pytest-3`` and ``pip install pytest-3`` match, and a
#: fleet-owned false ``BANNED`` costs a stopped worker and its discarded turn. What
#: separates the two is the token's POSITION in argv, which ``/proc`` supplies
#: NUL-separated and the joined string cannot recover. The alias is therefore detected
#: on the argv side only, by ``_argv_is_uncapped_argv_only_runner``.
_ALIAS_RUNNER_BASE_RE = re.compile(r"^(?:pytest|py\.test)-\d+(?:\.\d+)*(?:\.exe)?$")

#: The two labels an alias is PRINTED as. The pattern above is a shape, so the text it
#: admits is caller-chosen past the stem; a label names the stem and nothing else, which
#: is what keeps the printed program vocabulary a closed set.
_ALIAS_PROGRAM_LABELS = (("pytest-", "pytest-<version>"), ("py.test-", "py.test-<version>"))


def _alias_program_label(canonical: str) -> str | None:
    """The fixed label for a versioned alias, or None when *canonical* is not one.

    Read by the command redactor instead of the token. A shape test cannot bound what it
    admits past its stem -- ``pytest-123456`` satisfies the pattern exactly as
    ``pytest-3.12`` does -- so echoing the token would put an arbitrary caller-chosen
    string on a line that lands in the conductor's context. The stem is the part that
    identifies the invocation, and there are only two of them.
    """
    if not _ALIAS_RUNNER_BASE_RE.match(canonical):
        return None
    for stem, label in _ALIAS_PROGRAM_LABELS:
        if canonical.startswith(stem):
            return label
    return None


#: The other runner spellings the joined-line rule cannot express. ``py.test`` is the
#: legacy entry point and ``pytest.exe`` the Windows one; both are already in
#: ``_RUNNER_BASES`` -- this file treats them as runners everywhere the argv is read --
#: but no rule above spells either, because the dot defeats the token boundary the rule
#: anchors on. They are admitted on the argv side with the versioned alias because they
#: need the same two things and for the same reason: the dot makes each a plausible
#: FILENAME (``py.test.log``, ``pytest.exe.bak``), so only position separates an
#: invocation from data.
#:
#: ``vitest.cmd`` is in ``_RUNNER_BASES`` and is deliberately NOT admitted here. Its
#: rule above spells an uncapped run as ``vitest run`` with nothing following it, not as
#: a numeric ``-n``, so routing it through ``_argv_worker_pool_is_budgeted`` -- which
#: reads pytest's ``-n`` grammar -- would answer a question vitest's argv never poses.
_ARGV_ONLY_RUNNER_BASES = frozenset({"py.test", "py.test.exe", "pytest.exe"})


#: Shapes a launcher consumes as part of its OWN grammar, so that a token carrying one
#: is not the command's subject. Three qualify: an option, a bare number (an option's
#: value, as in ``timeout 900`` and ``nice -n 10``), and a ``KEY=value`` assignment,
#: which is ``env``'s whole operand grammar.
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


#: Options that consume the FOLLOWING token as their value, so that token must not
#: be read as a target. Without this, every one of these whole-suite forms printed
#: ``scope=paths`` -- the LOW-priority readout -- because the value happens to carry
#: a separator or a ``::``: ``--cov src/kiro_crew``, ``-W ignore::DeprecationWarning``,
#: ``-c setup.cfg``, ``-o addopts=…``, ``--ignore <path>``, ``--deselect <node id>``,
#: ``--rootdir <dir>``, ``--junitxml <file>``.
#:
#: ``-k`` / ``-m`` are value-taking too but are deliberately absent: a selector
#: narrows a run, so they are answered before this table is consulted.
#:
#: Several of these (``--cov``, ``--cov-report``, ``--durations``) also accept the
#: bare form, where the next token IS a target. Consuming it then reads a narrowed
#: run as ``suite``, which over-states severity -- the same fail-closed direction the
#: bare-token case below resolves to, so the residual error stays on the safe side.
_VALUE_TAKING_OPTS = frozenset(
    {
        "-c",
        "-n",
        "-o",
        "-p",
        "-r",
        "-W",
        "--basetemp",
        "--confcutdir",
        "--cov",
        "--cov-config",
        "--cov-report",
        "--deselect",
        "--dist",
        "--durations",
        "--ignore",
        "--ignore-glob",
        "--import-mode",
        "--junitxml",
        "--log-file",
        "--log-level",
        "--maxfail",
        "--override-ini",
        "--rootdir",
        "--tb",
        "--tx",
    }
)


def _token_base(token: str) -> str:
    """Lowercased final path component of an argv token.

    Split on both separators explicitly: the argv comes from a Linux ``/proc`` even
    when this script runs elsewhere, so the answer must not depend on the host's.
    """
    return token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()


def _is_runner_base(base: str) -> bool:
    """Is *base* the runner's own token -- under its plain name or a versioned alias?

    The alias is admitted HERE, on the argv side, and not in the joined-line rule.
    ``_ALIAS_RUNNER_BASE_RE`` documents why that asymmetry is the point rather than an
    oversight: this function is only ever asked about a token that already stands alone
    in argv, so the alias cannot arrive as a path component or a package name.
    """
    return base in _RUNNER_BASES or bool(_ALIAS_RUNNER_BASE_RE.match(base))


def _runner_token_index(argv: list[str]) -> int | None:
    """Where the runner's OWN token sits in *argv*, or None when none stands alone.

    Two decisions need this position rather than the start of the argv: what the run
    was scoped to, and whether it declared a worker cap. Both are questions about the
    RUNNER's options, and a launcher in front of it has options of its own that can
    wear the same spelling. None means no runner token stands alone -- a wrapper, an
    unknown name, or a module flag glued to the runner -- and both callers answer
    conservatively rather than guessing from a position they could not find.

    A token that cannot be a program NAME is skipped before the question is asked at
    all, and that is load bearing rather than tidy. ``_token_base`` reduces a token to
    its last path component, so an option's or an assignment's VALUE reduces to one too:
    ``TMPDIR=/tmp/pytest-of-ci/pytest-3`` becomes ``pytest-3``, which the runner
    vocabulary admits. Answering with that position hands both callers the wrong span --
    the count reader then sees a LAUNCHER's ``-n 10`` as the run's own worker count and
    a budgeted run is reported, and the scope reader sees the runner's path as a target
    and calls a whole-suite run ``paths``. Every pytest temp directory is named after the
    runner, so the shape is ordinary rather than contrived.

    This also makes ``_is_runner_base``'s own precondition true: it is asked only about a
    token that could stand alone in argv, which is why it may admit the versioned alias
    without a path component or a package name ever reaching it.
    """
    for index, token in enumerate(argv):
        if token.startswith("-") or _ENV_ASSIGNMENT_RE.match(token) is not None:
            continue
        if _is_runner_base(_token_base(token)):
            return index
    return None


def _run_scope(argv: list[str]) -> str:
    """Classify a flagged run as ``suite`` or ``paths`` WITHOUT echoing any argument.

    A rule match says a run's worker count bypasses the budget; it says nothing
    about how much that run is doing, and those are wildly different severities. A
    whole-suite run is what reached the several-hundred-process fan-out and is the
    line to reach for first; a single-file run matching the same rule fans out over
    a few files and can wait its turn. Ranking only -- whether to respond at
    all stays keyed to ``cwd=fleet``, which this word never gates. Reported as ONE
    derived word so the readout stays judgeable without anyone opening ``ps``.

    Deliberately derived, never quoted. The caller documents why the argv is not
    echoed -- a command line can carry a credential or a presigned URL, and this
    text lands in the conductor's model context -- and a path is exactly the part
    that leaks a checkout layout. ``suite`` / ``paths`` / ``unknown`` carries the
    severity while being three fixed strings that can hold no argument content.

    Scanning starts AFTER the runner token because everything before it is the
    interpreter's own path: ``/usr/bin/python3`` contains a separator, so reading
    argv from index 0 would classify every POSIX invocation as path-scoped.

    **Deliberately approximate, and biased toward over-stating severity.** A bare
    token cannot be told from an option's VALUE without knowing which options take
    one, and this function cannot know that: ``--token secret test`` offers no way
    to see that ``secret`` is a value and ``test`` a target. So only an
    unmistakable target counts -- one carrying a separator, a ``.py`` suffix or a
    ``::`` node id -- and the two resulting misreads are not symmetrical:

    * ``pytest test`` (a bare directory, no separator) reads as ``suite``. That
      over-states severity, which is the fail-closed direction for a monitoring
      control and the same direction the wrapper exemption above chose.
    * Nothing reads as ``paths`` unless a real target shape is present, so the
      quiet answer is never the one produced by guessing.
    * ``pytest --pyargs kiro_crew.mod`` names an importable package, not a path,
      so it carries none of the three shapes and reads as ``suite`` -- narrowed in
      fact, over-stated in the readout, the same safe direction.

    Resolving a token against the filesystem would settle it and is refused on
    purpose: this walks OTHER processes' argv, each with its own cwd, so a probe
    that stat()s their arguments both lies about relative paths and turns a
    read-only monitor into something that touches attacker-influenced paths.
    """
    start: int | None = None
    runner = _runner_token_index(argv)
    if runner is not None:
        start = runner + 1
    if start is None:
        # The rule matched the joined string, but no runner token stands alone --
        # a wrapper, a name this function does not know, or an interpreter that
        # glued the module flag onto the runner name with no space. Guessing a
        # severity here would be worse than admitting the gap.
        return "unknown"
    skip_next = False
    for tok in argv[start:]:
        if skip_next:
            # The value of a value-taking option, not a target. `--cov src/kiro_crew`
            # and `-W ignore::X` are whole-suite runs whose VALUE carries a separator.
            skip_next = False
            continue
        # A selector narrows a run as surely as a path does, and `-k`/`-m` take
        # their value either as the next token or glued on with `=`.
        if tok in {"-k", "-m"} or tok.startswith(("-k=", "-m=")):
            return "paths"
        if tok in _VALUE_TAKING_OPTS:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        if "::" in tok or tok.endswith(".py") or "/" in tok or "\\" in tok:
            return "paths"
    return "suite"


def _invokes_bare_vitest_run(argv: list[str]) -> bool:
    """Does *argv* invoke a whole-suite ``vitest run``, rather than merely name one?

    The rule's pattern is matched against the space-joined cmdline, and that text
    cannot answer this question -- not with a wider boundary class, not with a
    different anchor. ``/proc/<pid>/cmdline`` separates arguments with NUL bytes, so
    ``["grep", "-rn", "vitest run"]`` and ``["grep", "-rn", "vitest", "run"]`` join
    to the SAME string: one greps for a phrase, the other is the phrase. Any
    expression over the joined form necessarily treats them alike, so the decision
    belongs where the separators still exist.

    On the argv the statement is short. A whole-suite run is the program, then its
    ``run`` subcommand, then nothing:

    * the program has to be vitest's own token, so a phrase carried INSIDE one
      argument -- a grep pattern, a filename, an echoed string -- names no program.
      The name is compared whole, after ``_basename`` drops any directory it was
      qualified with, which is every spelling the pattern can select: the pattern
      wants whitespace directly after ``vitest``, so a suffixed entry point
      (``vitest.cmd``, ``vitest.mjs``) never reaches this function at all;
    * ``run`` has to be its own final token, so a mention that trails a command
      cannot supply it;
    * nothing may follow, which is what "invoked with no file argument" means and
      is the condition the ``\\s*$`` anchor reaches for. Keeping it as "no argument
      at all" rather than "no TARGET argument" makes this decision a strict SUBSET
      of the pattern's: every argv answered True here also matches the pattern, so
      the set of processes reported can only shrink. A run carrying an option and no
      file (``vitest run --reporter=dot``) is whole-suite in fact and stays quiet,
      the same as under the pattern alone -- widening that is a change to which
      shapes the conductor stops mid-turn, which is a decision of its own and not
      this one's to make.

    Of those three, the SEPARATION is what the joined text cannot supply and what
    the pattern therefore cannot check. The trailing shape it can: ``\\s*$`` already
    implies ``run`` ends the text for every cmdline the pattern selects. Stating the
    whole shape here regardless keeps the decision readable on its own and keeps it
    correct if the pattern is ever retuned, rather than leaving it right only
    because something upstream happened to filter its input.

    What this does NOT separate: a runner name standing alone as another program's
    argument, ``echo vitest run``. Telling that from a launcher that really does run
    vitest needs a list of every launcher rather than a shape, and a list is what
    silently loses the launcher nobody added -- so the residual error stays on the
    reporting side, where a line names a pid an operator can dismiss.
    """
    return len(argv) >= 2 and argv[-1] == "run" and _basename(argv[-2]) == "vitest"


#: ``python``, whose real basenames carry a version (``python3``, ``python3.12``,
#: ``python.exe``). Naming each one would be a list that goes stale on every release,
#: so the stem is fixed and only the version digits vary -- which is the property that
#: matters: at most two digits of a match come from the command being read.
_PYTHON_BASE_RE = re.compile(r"^python(?:3(?:\.\d{1,2})?)?(?:\.exe)?$")


def _norm_path(path: str) -> str:
    """A compare-ready spelling of *path*.

    Two spellings of one directory must not read as two directories, or a
    fleet-owned process is filed as somebody else's and its banned run goes
    unreported. Three normalisations, all of them load-bearing on Windows, where
    this ran green on Linux and misfiled every match:

    * the extended-length prefix. ``os.readlink`` can answer ``\\\\?\\D:\\...``,
      which no configured root will ever spell, so a literal prefix comparison
      fails on a path that does match.
    * case and separator. ``normcase`` folds both, since ``D:/a`` and ``d:\\a``
      are the same directory there and only one of them is what the config says.
    * short (8.3) names. Left to ``os.path.realpath`` in the caller's fallback,
      because expanding them requires touching the filesystem and this half must
      stay a pure string operation for the unreadable-cwd case.
    """
    if path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normcase(os.path.normpath(path))


def _under(child: str, root: str) -> bool:
    """Is *child* the directory *root* or inside it?

    The boundary test is a separator, not a bare prefix: without it a sibling
    worktree named ``wt-a-old`` is swallowed by ``wt-a`` and its runs are
    attributed to the wrong owner.
    """
    return child == root or child.startswith(root.rstrip(os.sep) + os.sep)


def _program_path(cmd: str) -> str:
    """The program path out of a joined cmdline, or ``""``.

    ``/proc/<pid>/cmdline`` is world-readable where the ``cwd`` and ``exe``
    symlinks are not -- both of those need the same access a debugger would, so
    they fail for another user's process while the cmdline still reads. That
    asymmetry is the only reason this fallback is worth having.

    Reading argv is not the same as PRINTING it. A secret can ride in an
    argument, so the command line is still never emitted -- but the program PATH
    is structural, so it can be compared for a decision and dropped. The next
    reader will otherwise assume argv was excluded from being read at all.
    """
    return cmd.split(" ", 1)[0] if cmd else ""


#: Shells that take the program to run as a command STRING in an argument. A
#: banned tool named inside that string is text the shell was handed, not the
#: program this pid is running.
SHELL_PROGRAMS = frozenset({"sh", "bash", "zsh", "dash", "ash", "busybox"})


def _basename(program: str) -> str:
    """Lowercased basename of a program path, minus a ``.exe`` suffix.

    Split on ``/`` explicitly rather than via os.path: the cmdline comes from a
    Linux /proc even when this script is running somewhere else, so the answer
    must not depend on the host's separator.
    """
    base = program.replace("\\", "/").rpartition("/")[2].lower()
    return base[:-4] if base.endswith(".exe") else base


#: Shell options that consume the NEXT argv entry as their operand. Without this,
#: the leading-option scan below stops at the operand -- `bash -o pipefail -c ...`
#: broke on `pipefail`, never reached the `-c`, and the wrapper stayed
#: misattributed to whatever its command string named. The long forms are here
#: for the same reason and not with the other `--` options: skipping a `--long`
#: entry alone leaves its operand behind, so `bash --rcfile /dev/null -c ...`
#: stopped on the path. The `--opt=value` spelling is one entry and needs no
#: operand rule.
_SHELL_OPTS_WITH_OPERAND = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})


#: Directories whose contents are the system's real shells. A basename is not a
#: program: an interpreter COPIED to ``/tmp/bash`` answers ``bash`` to every
#: basename test while running whatever it was handed, so a name-only check let it
#: claim the wrapper exemption and its banned run left no ``BANNED`` line at all.
#: Requiring the kernel's ``exe`` to resolve INTO one of these directories is what
#: makes the shell claim checkable rather than self-asserted -- writing there needs
#: root, which is already enough privilege to stop the probe outright, so it buys
#: an attacker nothing they did not already have.
#:
#: A shell installed anywhere else -- a Nix store path, a container's ``/busybox``,
#: a relocated toolchain -- therefore gets NO exemption and its wrapper is
#: REPORTED. That is the direction to fail in: a false ``BANNED`` line names a pid
#: an operator can look at and dismiss, while a missing one hides a real unbounded
#: run for the whole session.
_TRUSTED_PROGRAM_DIRS = frozenset(
    {"/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/local/bin", "/usr/local/sbin"}
)


#: Suffix procfs adds to ``/proc/<pid>/exe`` once the running binary is unlinked.
_PROCFS_DELETED_MARKER = " (deleted)"


def _trusted_program_base(proc_entry: Path) -> str | None:
    """Basename of the binary this pid is REALLY running, or None if untrustworthy.

    ``argv[0]`` is chosen by the process itself -- ``exec -a bash …`` lets a genuine
    unbounded pytest introduce itself as a shell, and the wrapper check would then
    drop it as somebody else's argument and never stop the offender.
    ``/proc/<pid>/exe`` is a kernel-maintained symlink to the actual binary and the
    process cannot rewrite it, so it is the only trustworthy answer to "what is this".

    ``None`` means the link did not produce a trustworthy program. Two causes, and
    both must be treated the same way. The link could not be followed: usually a
    process that exited mid-scan, and on a shared host every OTHER user's process,
    whose ``exe`` this uid cannot resolve while its ``cmdline`` stays world-readable
    -- the same asymmetry ``_owner_class`` already documents for ``cwd``. Or it
    resolved OUTSIDE ``_TRUSTED_PROGRAM_DIRS``, which makes a shell-shaped basename
    a claim about a file anybody could have put there rather than a fact about a
    system shell.

    ``None`` grants NO exemption: the caller reports such a pid rather than trusting
    ``argv[0]``, because a process can hide its own ``exe`` by going non-dumpable and
    "no evidence" must not be the cheapest way to buy a pass. The cost is bounded --
    a pid this uid cannot inspect is one the conductor cannot stop either, and
    ``_owner_class`` sorts it into `foreign`/`unknown` rather than raising a fleet
    violation.

    Read only for a pid whose cmdline ALREADY matched a banned rule, so this is one
    extra syscall per match -- a handful per scan -- not one per process.
    """
    normalized = _kernel_program_path(proc_entry)
    if normalized is None:
        return None
    if normalized.rpartition("/")[0] not in _TRUSTED_PROGRAM_DIRS:
        return None
    return _basename(normalized)


def _kernel_program_path(proc_entry: Path) -> str | None:
    """Normalised target of ``/proc/<pid>/exe``, or None when the link will not read.

    The kernel maintains the link and the process cannot rewrite it, so this is the
    only answer to "what binary is this" that ``argv[0]`` cannot contradict. It is
    kept separate from the directory gate above because the two callers ask different
    questions of the same link: a SHELL's basename outside a system directory is a
    claim about a file anybody could have placed there, while a RUNNER's basename is
    the same fact wherever the binary sits -- a venv's own ``bin`` is the ordinary
    home of one.
    """
    try:
        target = os.readlink(proc_entry / "exe")
    except OSError:
        return None
    # Separators are normalised the same way ``_basename`` does, and for the same
    # reason: the answer comes from a Linux /proc even when this script runs
    # somewhere else, so the decision must not depend on the host's separator.
    normalized = target.replace("\\", "/")
    # procfs appends this marker when the running binary has been unlinked, which a
    # package upgrade of bash or coreutils does mid-session. Without the strip the
    # link reads ``/usr/bin/bash (deleted)``: the directory gate still passes, but
    # the basename becomes ``bash (deleted)``, matches no entry in SHELL_PROGRAMS,
    # and the wrapper exemption is lost -- so a live ``bash -c '… pytest …'`` in a
    # worktree emits a false ``BANNED`` and the conductor stops a healthy owner,
    # discarding its in-flight work. Stripping it can only NARROW the false-BANNED
    # direction: the name behind the marker still has to be a shell AND still has to
    # sit in a trusted directory, and a deleted ``/usr/bin/pytest`` reduces to
    # ``pytest``, which is reported exactly as before.
    if normalized.endswith(_PROCFS_DELETED_MARKER):
        normalized = normalized[: -len(_PROCFS_DELETED_MARKER)]
    return normalized


def _is_shell_command_wrapper(argv: list[str], exe_base: str | None) -> bool:
    """Is *argv* a known shell running a command string rather than the tool it names?

    The banned-operation rules are matched against the whole joined cmdline, which
    is what makes an arg-shaped rule expressible at all -- ``\\bvitest\\b\\s+run\\s*$``
    is a statement about the ARGUMENTS, and the pytest rule reads the worker count
    out of them. The cost of matching that far is that a wrapper's argument is read
    as the wrapper's own program: ``bash -c 'cd x && pytest -n 8'`` is reported as
    an unbudgeted pytest while the process that exists is a shell.

    Skipping the wrapper removes a misattribution and loses no coverage, because
    the probe scans EVERY process: a genuinely running wrapped tool has its own
    pid and is matched there on its own merits. Attributing it to the wrapper as
    well only makes the conductor stop the wrong pid.

    That "loses no coverage" argument rests on the wrapped tool still being alive
    at the next ``/proc`` walk, which is true of the two built-in rules and NOT
    guaranteed of an operator's own. The caller therefore consults this function
    only for a match on a built-in rule; a custom ``banned_process_res`` reports
    the wrapper as it did before the fix.

    Takes real ARGV, not a joined string. Once NUL separators become spaces, an
    argument containing a space is indistinguishable from two arguments, so the
    option/operand structure this function has to read cannot be recovered.

    Three spellings of the same flag all have to be recognised, and each one was
    a live misattribution before it was:

    * alone -- ``bash -c 'pytest'``
    * grouped into a cluster -- ``bash -lc``, ``bash -ic``, ``sh -euxc``; a login
      shell is the commonest spelling of all
    * behind an option that takes an OPERAND -- ``bash -o pipefail -c``, and its
      long forms ``bash --rcfile /dev/null -c`` and ``bash --init-file f -c``

    BusyBox is a multi-call binary, so the applet -- not ``busybox`` -- is the
    effective program, and it is resolved before the option scan starts. Which
    argv entry names it depends on how the applet was invoked, and only ONE of the
    two spellings puts it in argv[1]:

    * through a SYMLINK, the standard form -- ``argv[0]`` is the applet
      (``/usr/bin/wget``) while ``exe`` resolves to busybox itself
    * explicit multi-call dispatch -- ``busybox sh -c '...'`` names the applet in
      argv[1], where every other shell would put an option

    Reading argv[1] for the symlink form left ``base`` as ``busybox``, which is a
    shell, so a banned applet carrying a ``-c``-shaped flag (``wget -c URL``) was
    read as a shell holding a command string and dropped from the scan.
    """
    if not argv:
        return False
    # The exemption requires the KERNEL to say this is a shell, and to say it about a
    # binary in a system directory. ``argv[0]`` is not accepted as a substitute even
    # when ``exe`` cannot be read, because that is the whole spoof: a process that
    # makes itself non-dumpable (``prctl(PR_SET_DUMPABLE, 0)``) hides its own ``exe``
    # link, and could then present a shell-shaped ``argv[0]`` with a ``-c`` and be
    # dropped from the scan. Nor is a shell-shaped FILENAME accepted on its own -- an
    # interpreter copied to ``/tmp/bash`` is not bash, so ``_trusted_program_base``
    # answers ``None`` for it as well. No exemption on no evidence, so both cases now
    # REPORT. The cost is bounded and lands on the right side: a process this uid
    # cannot inspect is usually another user's, and ``_owner_class`` already sorts
    # those into `foreign`/`unknown` rather than raising a fleet violation.
    if exe_base is None:
        return False
    base = exe_base
    rest = argv[1:]
    if base == "busybox":
        # A busybox applet is normally reached through a SYMLINK, so argv[0] is the
        # applet and `exe` is busybox: `wget -c URL` invoked that way is a wget, not
        # a shell carrying a command string, and leaving `base` as `busybox` handed
        # it the shell exemption. argv[1] holds the applet ONLY for the explicit
        # `busybox <applet>` dispatch form.
        #
        # Consulting argv[0] here cannot widen the exemption: `exe` has already
        # proven this pid is busybox, and a spoofed argv[0] can only replace one
        # SHELL_PROGRAMS member with another (still a shell) or with a non-shell,
        # which REPORTS. The spoof direction stays closed.
        argv0_base = _basename(argv[0])
        if argv0_base != "busybox":
            base = argv0_base
        elif rest and not rest[0].startswith(("-", "+")):
            base = _basename(rest[0])
            rest = rest[1:]
    if base not in SHELL_PROGRAMS:
        return False
    # Only the LEADING option run is examined, and the scan stops at the first
    # entry that is not an option, because that entry is the command string (or a
    # script path) and everything after it is the shell's payload rather than the
    # shell's own flags -- `bash -lc 'grep -c foo'` must be decided by the `-lc`,
    # never by the `-c` inside the string it carries. A ``--long`` option cannot
    # be a cluster, so it is skipped rather than ending the run.
    index = 0
    while index < len(rest):
        token = rest[index]
        if not token.startswith(("-", "+")):
            break
        if token in _SHELL_OPTS_WITH_OPERAND:
            index += 2
            continue
        if token.startswith("--"):
            index += 1
            continue
        if "c" in token[1:]:
            return True
        index += 1
    return False


#: The worker-count flags, as ARGV tokens rather than as text in a joined command line.
_CAP_FLAGS = ("-n", "--numprocesses")

#: The ``-n`` values that hand the worker count to the rootdir hook rather than fixing
#: it: ``auto`` is what ``addopts`` and the gate runner pass, and ``logical`` is xdist's
#: other hook-resolved spelling (``pytest_xdist_auto_num_workers`` answers both).
_BUDGETED_COUNT_WORDS = frozenset({"auto", "logical"})

#: The ini-override flags, as ARGV tokens. ``-o addopts=...`` / ``--override-ini
#: addopts=...`` replace the tree's ``addopts`` with the value given, and pytest puts
#: that value in front of the run's own arguments -- so an ``-n`` inside it is the
#: run's count unless a later token of the run's own overrides it.
_OVERRIDE_INI_FLAGS = ("-o", "--override-ini")

#: The ini key whose override carries a worker count. Every other key is somebody
#: else's setting and is not read.
_ADDOPTS_KEY = "addopts"

#: pytest's argparse terminator: every token after it is a file or a node id, however
#: it is spelled, so a ``-n0`` behind it is a path and never the run's count.
_END_OF_OPTIONS = "--"


def _count_is_budgeted(value: str) -> bool:
    """Does this ``-n`` VALUE leave the worker pool budgeted or single-process?

    ``auto`` / ``logical`` go through the hook. ``0`` and ``1`` (with any leading zeros)
    run one process with xdist inactive, which no budget needs to bound. Any other
    digit run is a fixed count the host's state is never consulted about. Anything
    else is a value pytest rejects at argument parsing, and the answer is the
    fail-closed one for a monitoring control: not budgeted.

    Judged LEXICALLY. The value is never converted to an integer: the interpreter
    refuses to convert a run of more than a few thousand digits and refuses the
    non-ASCII characters ``str.isdigit`` accepts (a superscript digit), and either refusal raised out
    of ``_host_lines`` ends the patrol cycle for as long as that pid lives. A string
    comparison has no such edge.
    """
    if value in _BUDGETED_COUNT_WORDS:
        return True
    if not (value.isascii() and value.isdigit()):
        return False
    return value.lstrip("0") in ("", "1")


def _read_flag(tokens: list[str], index: int, flags: tuple[str, ...]) -> tuple[str | None, int]:
    """Read ``tokens[index]`` as one of *flags*, the way argparse reads an option.

    Returns the flag's value and the index of the first token not consumed, or
    ``(None, index)`` when the token is none of the flags. A short flag carries its
    value glued (``-n0``), after ``=`` (``-n=0``) or as the next token (``-n 0``); a
    long flag after ``=`` or as the next token, and a longer token with no ``=``
    (``--numprocesses-foo``) is a different option. A flag standing last with nothing
    after it reads as the empty value, which nothing budgets.
    """
    token = tokens[index]
    for flag in flags:
        if not token.startswith(flag):
            continue
        rest = token[len(flag) :]
        if rest.startswith("="):
            return rest[1:], index + 1
        if rest == "":
            if index + 1 < len(tokens):
                return tokens[index + 1], index + 2
            return "", index + 1
        if flag.startswith("--"):
            continue
        return rest, index + 1
    return None, index


def _addopts_override(tokens: list[str]) -> list[str] | None:
    """The ``addopts`` an ``-o`` / ``--override-ini`` on the command line replaces the
    tree's with, split into tokens as pytest splits them, or ``None`` when the run
    starts from the tree's own ``addopts``.

    Where the same key is overridden twice the last one wins, as pytest resolves it.
    A value the shell-style splitter refuses (an unbalanced quote) is one pytest
    refuses too, and it raises ``ValueError`` here rather than answering -- the
    caller's fail-closed answer is the reporting one.
    """
    override: list[str] | None = None
    index = 0
    while index < len(tokens) and tokens[index] != _END_OF_OPTIONS:
        value, index = _read_flag(tokens, index, _OVERRIDE_INI_FLAGS)
        if value is None:
            index += 1
            continue
        key, separator, ini_value = value.partition("=")
        if separator and key.strip() == _ADDOPTS_KEY:
            override = shlex.split(ini_value)
    return override


def _argv_worker_pool_is_budgeted(argv: list[str]) -> bool:
    """Is the RUNNER's effective worker count budgeted or single-process, read as TOKENS?

    The joined-line rule SELECTS a candidate: a pytest whose command line carries a
    numeric ``-n`` of two or more somewhere after the runner. This decides. What the
    joined text cannot read is read here:

    * ``/proc`` hands arguments over NUL-separated, so an argument's own bytes can
      never be mistaken for syntax -- a ``|`` in a log format or a parametrized node id
      is data, and the ``-n`` behind it is still the run's own.
    * argparse resolves a repeated option LAST-wins, so ``-n 4 -n auto`` is a budgeted
      run and ``-n auto -n 4`` is not. The tokens are walked to the end and the final
      specification is the one judged; a lookahead that stops at the first number
      cannot say which came last.
    * pytest's ``--`` ends its options. Every token behind it is a file or a node id,
      so ``-n32 -- -n0`` is a 32-worker run pointed at a path called ``-n0``, and the
      walk stops there.
    * ``-o addopts=...`` / ``--override-ini addopts=...`` replace the ``addopts`` the
      run starts from, and pytest puts that value IN FRONT of the run's own arguments.
      The override's tokens are walked first, so ``-o addopts='-n 16'`` is a 16-worker
      run, and ``-o addopts='-n 16' -n auto`` is budgeted by the run's own last word.

    No ``-n`` at all is budgeted: the default ``addopts`` supply ``-n auto``, and
    reporting a bare pytest would flag every targeted single-file run a worker makes.
    An override that supplies none runs one process with xdist inactive, which is the
    same answer.

    What it must not do is read SOMEBODY ELSE's option as the runner's. ``nice -n 10
    pytest test/`` has no worker count of its own -- the ``10`` is a priority -- and
    ``xvfb-run -n`` is the same shape; both launchers this scan recognises. So the scan
    starts after the runner's own token, and when no runner token stands alone it
    declines to answer at all, which reports (fail-closed) rather than exonerates.

    It reads argv and nothing else. ``-c other.ini`` names a file whose ``addopts``
    this reader does not open, and ``--noconftest`` / ``--confcutdir`` decide which
    ``conftest.py`` loads; the checkout, not the command line, is where those are
    answered, and the probe already keys the stop on ``cwd=fleet`` for that reason.
    """
    runner = _runner_token_index(argv)
    if runner is None:
        return False
    own = argv[runner + 1 :]
    try:
        override = _addopts_override(own)
    except ValueError:
        return False
    tokens = [*override, *own] if override is not None else own
    effective: str | None = None
    index = 0
    while index < len(tokens) and tokens[index] != _END_OF_OPTIONS:
        value, index = _read_flag(tokens, index, _CAP_FLAGS)
        if value is None:
            index += 1
            continue
        effective = value
    if effective is None:
        return True
    return _count_is_budgeted(effective)


#: The label printed as ``rule=`` when the ARGV path fired rather than a joined-line
#: regex. It is not a regex and deliberately does not look like one: the field names
#: which shape fired, and claiming the pytest pattern matched when it did not would
#: send a reader to a lookahead that is working correctly.
ARGV_RUNNER_RULE_LABEL = "argv:pytest-runner-uncapped"

#: Every shape this file detects on its OWN authority, joined-line or argv. It is what
#: the wrapper exemption is gated on, and the gate is a statement about authorship
#: rather than about mechanism: an operator's ``banned_process_res`` can name a
#: short-lived command whose only visible sample IS the wrapper, which is why a custom
#: rule reports it -- see ``_is_shell_command_wrapper``. Both shapes here name a
#: long-running test runner, so both can afford to wait for the runner's own pid.
_BUILTIN_SHAPES = frozenset(DEFAULT_BANNED_RES) | {ARGV_RUNNER_RULE_LABEL}


def _argv_only_runner_indices(argv: list[str]) -> list[int]:
    """EVERY position holding a runner spelling the joined-line rule cannot express.

    Two categories qualify -- a versioned alias (``pytest-3``, ``pytest-3.12``,
    ``py.test-3``, ``pytest-3.exe``) and the dotted plain spellings ``py.test``,
    ``py.test.exe`` and ``pytest.exe`` -- and every one shares the property that makes
    this an argv question: each is also a well-formed path component or filename, so
    nothing but its position separates the invocation from the data.

    All of them, not the first, because that property cuts both ways: an ``env``
    assignment whose value ends in a pytest temp path reduces to an alias-shaped base and
    can stand in FRONT of a genuine run. Answering only the first candidate would let
    ``env TMPDIR=/var/tmp/pytest-of-ci/pytest-3 pytest-3 test/`` be decided by the
    assignment and hide the uncapped runner two tokens later.
    """
    found = []
    for index, token in enumerate(argv):
        base = _token_base(token)
        if base in _ARGV_ONLY_RUNNER_BASES or _ALIAS_RUNNER_BASE_RE.match(base):
            found.append(index)
    return found


#: Launchers whose whole job is to adjust the environment and then exec the command that
#: follows, so the command's own first word is the program. These are the only launchers
#: a runner spelling may stand IMMEDIATELY behind.
#:
#: Every other entry of ``_LAUNCHER_BASES`` interposes a subject of its own -- a target,
#: a subcommand, a script -- and ``make pytest-3.12`` is why that distinction has to be
#: made at the immediate position too, not only after an operand. A per-interpreter test
#: matrix spelling its targets ``pytest-3.11``, ``pytest-3.12`` is an ordinary Makefile,
#: each target wraps a CAPPED run, and admitting it emits a fleet-owned line against a
#: healthy worker with no automatic recovery.
#:
#: The split is small and stable because it is the same question the operand allow-list
#: asks, one position earlier: a launcher is transparent exactly when its own grammar is
#: options, numbers and assignments and everything after that grammar is the command.
_TRANSPARENT_LAUNCHER_BASES = frozenset({"env", "nice", "timeout", "xvfb-run"})


def _is_launcher_own_operand(token: str) -> bool:
    """Is *token* part of the LAUNCHER's own grammar rather than the command's subject?

    Three shapes are: an option, a bare number (an option's value, as in ``timeout 900``
    and ``nice -n 10``), and a ``KEY=value`` assignment, which is ``env``'s whole operand
    grammar. Anything else is a word the launcher will run or hand on -- a subcommand
    (``npm run``, ``poetry run``, ``make clean``) or a script (``coverage run worker.py``)
    -- so the command already has a subject and a runner spelling after it is that
    subject's argument.

    Naming what MAY be skipped is the point, not an accident of ordering. A deny-list of
    suspicious shapes has to enumerate every way a program can be spelled and silently
    accepts the ordinary word it missed; this declines an operand it does not recognise.
    The cost is a missed alias run behind a launcher whose grammar is not described here,
    which is the same direction this file already takes for a launcher nobody listed.
    """
    return token.startswith("-") or token.isdigit() or _ENV_ASSIGNMENT_RE.match(token) is not None


def _stands_in_program_position(
    argv: list[str], index: int, proc_entry: Path | None = None
) -> bool:
    """Is the token at *index* the PROGRAM being run, rather than an argument to one?

    This is the whole reason these spellings are detected on the argv side. ``pytest-3``
    is a real invocation, a directory ``pytest-of-ci/pytest-3``, and a package name;
    ``py.test`` is a real invocation and a plausible filename. In each case the strings
    are identical -- so what separates them is what stands in FRONT.

    The token qualifies at ``argv[0]`` unless the kernel's own binary for this pid
    contradicts it, or when the command opens with a launcher this script recognises: a
    launcher's job is to run something else, so a runner after one is still the program
    -- but only until the launcher's own subject appears.

    An INTERPRETER is the sharp case, because it puts the program in two places and
    neither is ``argv[0]``. Python runs exactly one thing -- the module named by ``-m``,
    or the SCRIPT standing as its first non-option operand -- and every token after that
    belongs to it. So the runner qualifies at exactly one index, located by
    ``_python_execution_target`` from python's own grammar. The script spelling is
    not an edge case: a packaged ``pytest-3`` has a python shebang, so the kernel's argv
    for it is ``python3 /usr/bin/pytest-3 …`` and this is the ONLY position the ordinary
    alias run ever occupies. ``python3 worker.py pytest-3`` declines because ``worker.py``
    is the program and the alias follows it; ``python3 worker.py -m pytest-3`` declines
    for the same reason even though an ``-m`` sits right in front of the alias, because a
    script may spell its own options however it likes and python's selector came earlier;
    and ``python3 cleanup.py /var/tmp/pytest-of-ci/pytest-3`` declines with an operand
    that is a pytest temp path whose last component is alias-shaped exactly as every
    pytest temp path's is.

    A SCRIPT position carries one further requirement, and the module position is declined
    outright. An import finder resolves a NAME against ``sys.path``, whose first entry is
    the working directory, so ``-m pytest-3`` reaches a checkout-local ``pytest-3.py`` as
    readily as anything installed -- and no pytest module carries a version suffix in the
    first place, the packaged ``pytest-3`` being a console script. A script is a path, and
    a path proves only that a file of that name sits there. ``python3 tools/pytest-3`` and
    ``python3 pytest-3`` run a file a checkout holds, named from the same namespace as the
    runner and indistinguishable from it by name, so the script has to be an installed
    entry point -- see ``_is_installed_entry_point``.

    ``argv[0]`` itself is the one position with no token in front of it to read, so
    there the process's own claim is checked against the kernel's binary; see
    ``_kernel_program_confirms`` for why that link has to CONFIRM, so an unreadable
    one declines rather than reporting on the process's own word.

    Behind a TRANSPARENT launcher -- one whose job is to adjust the environment and then
    exec what follows -- only that launcher's own grammar may stand in between; see
    ``_is_launcher_own_operand``. ``timeout 900 pytest-3`` and ``nice -n 10 pytest-3``
    qualify because a number is an option's value.

    Every other launcher interposes a subject of its own and therefore never puts the
    runner in the program position, at any distance. ``npm run build pytest-3`` and
    ``coverage run worker.py pytest-3`` pass an alias to a subcommand's program, and
    ``make pytest-3.12`` names a target: a per-interpreter test matrix spelling its
    targets that way is an ordinary Makefile, each target wraps a run that caps its own
    workers, and reporting it stops a healthy worker with no automatic recovery.

    An UNRECOGNISED first token disqualifies, and that is the direction to fail in
    here. ``ls /var/tmp/pytest-of-ci/pytest-3`` and ``pip install pytest-3`` are the
    two shapes the issue measured as the cost of matching the alias in the joined line,
    and both are disqualified by their own first token rather than by a pattern that has
    to guess. The cost of declining is a missed alias run behind a launcher this file
    does not describe, which is the direction it already takes for a launcher nobody
    listed at all -- and a missed line costs a signal, where a false one costs a turn.
    """
    if index == 0:
        return _kernel_program_confirms(proc_entry, argv[0])
    first = _token_base(argv[0])
    if _PYTHON_BASE_RE.match(first):
        target = _python_execution_target(argv)
        if target is None or target[0] != index:
            return False
        # ONLY the script position, and only an installed one. A MODULE name is what
        # python's import machinery resolves, and no pytest module carries a version
        # suffix -- the packaged ``pytest-3`` is a console SCRIPT, while the module has
        # always been plain ``pytest``, which the joined-line rule already matches. So
        # ``python -m pytest-3`` is not a pytest run at all: it runs a checkout-local
        # ``pytest-3.py``, and reporting one stops a healthy worker. The cost of
        # declining the module position is a missed ``python -m py.test``, a spelling
        # modern pytest does not provide.
        return target[1] == "script" and _is_installed_entry_point(argv[index], argv[0])
    if first not in _TRANSPARENT_LAUNCHER_BASES:
        return False
    # The CANDIDATE has to be checked too, not only what stands in front of it. With the
    # candidate at index 1 the slice ``argv[1:1]`` is empty and ``all([])`` is vacuously
    # true, so ``env TMPDIR=/var/tmp/pytest-of-ci/pytest-3 make test`` would qualify: the
    # assignment's last path component is alias-shaped for the same reason every pytest
    # temp path's is, and the process is a ``make`` run with no pytest in it at all.
    # An assignment is ``env``'s own grammar, which is precisely what
    # ``_is_launcher_own_operand`` already says is not the command's subject.
    return not _is_launcher_own_operand(argv[index]) and all(
        _is_launcher_own_operand(token) for token in argv[1:index]
    )


def _is_installed_entry_point(path: str, interpreter: str) -> bool:
    """Is *path* a console script installed beside the interpreter that is running it?

    Asked only of python's SCRIPT position, where the kernel's binary is the interpreter
    and no ``/proc`` fact can say what the script is. Being in a directory NAMED ``bin``
    is not evidence: any checkout can hold one, so a rule satisfied by the name reports
    ``python3 <worktree>/bin/pytest-3`` -- a file the repository happens to carry -- and
    stops a healthy worker over it.

    What installing a console script actually does is write it into the same directory as
    the interpreter it was installed for, because that is what an entry point IS: the
    script's shebang names its neighbour. So the question is whether the interpreter is
    there, and *interpreter* is the name ``argv[0]`` carries -- which for the shape this
    matters for is written by the KERNEL, not chosen by the process: running
    ``/usr/bin/pytest-3`` makes the kernel build ``argv`` from the script's shebang line.

    That asks the filesystem for ONE named file, never for a directory's contents, which
    is the same shape ``_venv_root`` already uses for ``pyvenv.cfg``. Deciding from the
    installation rather than from a list of locations is what keeps the packaged run
    reportable everywhere the interpreter really sits beside its scripts -- a system
    prefix, a virtualenv and a version-manager prefix each do, so all three answer yes
    without the rule naming any of them, and a host whose interpreter is a global shim is
    not a special case at all.

    A ``pip install --user`` is a KNOWN MISS, named here rather than claimed: it writes
    the console script into ``~/.local/bin`` and no interpreter with it, so the shebang
    names the base interpreter, this check looks for ``~/.local/bin/python3``, finds
    nothing and declines. An uncapped run spelled that way emits no line. Confirming it
    would mean reading the script's own shebang -- its CONTENTS, at a path an argument
    supplied -- which is a wider surface than the directory listing this check replaced,
    so the miss is accepted and stated instead of closed.

    Both failure directions cost a signal rather than a turn. A RELATIVE path cannot be
    resolved from here, because the probe's working directory is not the scanned
    process's, so it declines; and a name the filesystem does not confirm declines too.
    """
    normalized = path.replace("\\", "/")
    if not normalized.startswith("/"):
        return False
    neighbour = normalized.rpartition("/")[0]
    # The raw basename, not `_basename`: that one lowercases for comparison against a
    # vocabulary, while this is a filesystem lookup and a POSIX path is case sensitive.
    program = interpreter.replace("\\", "/").rpartition("/")[2]
    if not program:
        return False
    try:
        return os.path.isfile(f"{neighbour}/{program}")
    except OSError:
        return False


#: Single-letter ``python`` flags that carry no value, so a bundle of them stands
#: between the interpreter and its script without being the script.
#:
#: Transcribed from CPython 3.12's ``python --help`` option list, which is the version to
#: diff against: a flag a newer interpreter adds is unknown here, and an unknown option
#: makes the grammar walk decline rather than guess, so a real run would go unreported
#: until this set is widened. The direction is deliberate -- a missed line costs a signal
#: where a guessed one costs a turn -- but it is a maintenance obligation, not a property.
_PYTHON_FLAG_LETTERS = frozenset("bBdEhiIOPqRsStuvVx3")

#: Single-letter ``python`` options whose value may be attached (``-Wignore``) or
#: separate (``-W ignore``). Neither spelling is the script operand, and the separate
#: spelling's value is not one either.
_PYTHON_VALUE_LETTERS = frozenset("WXQJ")

#: Long ``python`` options that take their value as the NEXT token. Written as the set
#: that does, rather than as a rule about long options generally, because the value of
#: ``--check-hash-based-pycs always`` is dashless and would otherwise be read as the
#: script -- declining the real invocation one position later.
_PYTHON_LONG_VALUE_OPTS = frozenset({"--check-hash-based-pycs"})


def _python_execution_target(argv: list[str]) -> tuple[int, str] | None:
    """Index and KIND of the ONE thing the interpreter runs, or None when it is not a token.

    Python runs exactly one program and its grammar says which: ``python [flags]
    (-m module | -c command | - | script) [args…]``. The first of those selectors to
    appear wins, and every token after it belongs to the thing being run. So this walk
    returns a single index plus which selector produced it -- the module after ``-m``,
    or the script operand -- and a caller compares against it rather than testing tokens
    near the candidate. The kind matters because a module name is resolved by the import
    machinery while a script is a path, so only one of the two can be checked for being
    an installed entry point.

    That single-index shape is the point. Asking only whether the PREVIOUS token is
    ``-m`` accepts ``python3 worker.py -m pytest-3``, where ``worker.py`` is the program
    and ``-m pytest-3`` is a pair of arguments that script was handed -- a script may
    spell its own options however it likes. Reporting that stops a healthy worker and
    discards its turn, so the selector has to be python's own first one, not any ``-m``
    anywhere in the line.

    Both selector positions matter for this file. A packaged ``pytest-3`` is a python
    script with a shebang, so the kernel's argv for ``/usr/bin/pytest-3 test/x.py`` is
    ``python3 /usr/bin/pytest-3 test/x.py``: the alias stands as the SCRIPT and never
    appears at ``argv[0]``. ``python3 -m pytest-3`` names it as a MODULE. Both are the
    program; ``python3 worker.py pytest-3`` and ``python3 cleanup.py
    /var/tmp/pytest-of-ci/pytest-3`` are neither.

    A token this grammar cannot classify returns None, which declines. That keeps the
    failure in the direction this file fails in everywhere else: an unrecognised shape
    is not detected rather than reported, because a missed line costs a signal and a
    false one costs a turn. ``-mpytest`` and ``python -c`` are declined for that reason
    -- the first spells its module inside one token, the second runs no named program.
    """
    position = 1
    while position < len(argv):
        token = argv[position]
        if token == "-m":
            return (position + 1, "module") if position + 1 < len(argv) else None
        if token in {"-c", "-"}:
            return None
        if not token.startswith("-"):
            return (position, "script")
        if token.startswith("--"):
            position += 2 if token in _PYTHON_LONG_VALUE_OPTS else 1
            continue
        letters = token[1:]
        unknown = next((c for c in letters if c not in _PYTHON_FLAG_LETTERS), None)
        if unknown is None:
            position += 1
            continue
        if unknown == "m" and letters.endswith("m"):
            # ``-Om`` selects a module whose name is the NEXT token. ``-mpytest`` spells
            # the module inside this one token, so the grammar cannot point at it and it
            # falls through to the decline below.
            return (position + 1, "module") if position + 1 < len(argv) else None
        if unknown in _PYTHON_VALUE_LETTERS:
            # ``-Wignore`` carries its value; a bare ``-W`` takes the next token,
            # which is a value and so can never be the script.
            position += 1 if len(letters) > letters.index(unknown) + 1 else 2
            continue
        return None
    return None


def _kernel_program_confirms(proc_entry: Path | None, claimed: str) -> bool:
    """Does the kernel's own binary CONFIRM that ``argv[0]``'s claim is a runner?

    ``argv[0]`` is chosen by the process, so ``exec -a pytest-3 sleep 600`` presents a
    sleeping shell as a test runner, and this is the one position with no earlier token
    to read -- so without the kernel's answer this path would stop a healthy worker on
    the strength of a name the process picked for itself.

    The link must CONFIRM, and an unreadable one therefore declines. This is the
    opposite direction from the wrapper check one screen up, which needs positive
    evidence to EXEMPT a pid a rule already matched, so an unreadable link grants it
    nothing: here the check is what ACCUSES, and an accusation with no evidence behind
    it is exactly the shape that costs a turn. A process can make its own link
    unreadable by going non-dumpable, so treating that silence as permission to report
    would put the stop back under the process's own control.

    "Unreadable link means the pid is already sorted foreign or unknown" is NOT a
    mitigation, and the measurement matters more than the reasoning: ``_owner_class``
    falls back to ``_program_class``, which reads the SAME process-chosen argv, so an
    argv[0] naming a path under a fleet worktree is classified ``fleet`` with no link
    and no readable ``cwd`` at all -- one token, a fleet-owned ``BANNED`` row, a stopped
    worker, and no restore of its discarded turn anywhere in the emit path.

    What declining costs is an alias-named process whose link this uid cannot read,
    which is a missed signal. An interpreter is NOT a confirmation, and the reason is
    worth stating because the opposite looks plausible: a shebang script's kernel binary
    is indeed the interpreter, but the kernel also puts that interpreter at ``argv[0]``
    and the runner one position later, so a shebang run is answered by the script-operand
    rule and never reaches this check. The shape that does reach it with an interpreter
    behind it is ``exec -a pytest-3 python3 -c …``, which is the spoof.
    """
    if proc_entry is None:
        return False
    normalized = _kernel_program_path(proc_entry)
    if normalized is None:
        return False
    kernel_base = _token_base(_basename(normalized))
    if kernel_base == _token_base(claimed):
        return True
    return _is_runner_base(kernel_base) or kernel_base in _ARGV_ONLY_RUNNER_BASES


def _argv_is_uncapped_argv_only_runner(argv: list[str], proc_entry: Path | None = None) -> bool:
    """Is *argv* a run of an argv-only runner spelling whose worker pool bypasses the budget?

    Consulted ONLY for a pid no joined-line rule matched, so it can add a ``BANNED``
    line and can never change one. Both halves are read from the tokens ``/proc``
    separates with NUL: the runner has to stand in a program position, and the worker
    count is asked of the runner's own arguments -- so ``pytest-3 -n 4 test/x.py`` is
    reported, and ``pytest-3 -n auto test/x.py`` stays quiet, for the same reason and
    through the same function as the ``pytest`` spellings.

    *proc_entry* is the pid's own ``/proc`` directory, passed so the program position
    at ``argv[0]`` can be checked against the kernel's binary rather than the name the
    process chose for itself.
    """
    if not any(
        _stands_in_program_position(argv, index, proc_entry)
        for index in _argv_only_runner_indices(argv)
    ):
        return False
    return not _argv_worker_pool_is_budgeted(argv)


def _venv_root(program: str) -> str | None:
    """The virtualenv a program path belongs to, or None if it is not in one.

    A venv is created inside one checkout and belongs to it, so its interpreter
    path attributes the process. A system or shim interpreter attributes nothing:
    every checkout on the host shares it.
    """
    parent = os.path.dirname(os.path.dirname(program))
    if not parent:
        return None
    # bin/python on POSIX, Scripts/python.exe on Windows; pyvenv.cfg sits beside
    # both, so the marker is checked rather than either layout being assumed.
    return parent if os.path.isfile(os.path.join(parent, "pyvenv.cfg")) else None


def _program_class(cmd: str, fleet: list[str]) -> str:
    """The ownership class implied by the program path alone, for use when the
    cwd could not be read. See ``_owner_class`` for why the two directions of
    this comparison do not carry the same weight.

    Every uncertain answer is ``unknown``, never ``fleet``. In the conductor's
    action table ``fleet`` is the one class that STOPS a session while ``unknown``
    only re-injects the directive, so the safe direction for a wrong answer is
    toward not enforcing. A root that reaches here without being comparable
    therefore widens nothing.
    """
    if not fleet:
        return "unknown"
    program = _program_path(cmd)
    if not program:
        return "unknown"
    try:
        literal = _norm_path(program)
        real = _norm_path(os.path.realpath(program))
        for root in fleet:
            if _under(literal, _norm_path(root)) or _under(
                real, _norm_path(os.path.realpath(root))
            ):
                return "fleet"
        venv = _venv_root(program)
    except (OSError, ValueError):
        # ValueError is the embedded-NUL case that validation refuses at load
        # time; this is the belt to that braces, and it fails toward not stopping.
        return "unknown"
    if venv is None:
        # A system or shim interpreter. Every checkout on the host shares it, so
        # it says nothing about ownership and must not be read as a denial.
        return "unknown"
    return "foreign"


def _owner_class(proc_entry: Path, fleet: list[str], cmd: str = "") -> str:
    """``fleet``, ``foreign`` or ``unknown`` for the process at *proc_entry*.

    ``/proc/<pid>/cwd`` is a symlink to the working directory, so the link TARGET
    is the answer. When it cannot be read -- a process that exited between the
    scan and the read, or one owned by another user -- the program path is asked
    instead, because in both attributable BANNED lines observed in the field the
    cwd was the field that failed while the cmdline survived. A cwd-only
    classifier would have returned ``unknown`` for two processes that could be
    PROVEN not to be the fleet's, and an unknown match makes the conductor act.

    The two signals do NOT get the same authority, and the difference is load
    bearing. A program path UNDER a fleet worktree is conclusive: nothing outside
    that checkout runs its interpreter. A program path outside one is only
    conclusive when it is a venv interpreter, which belongs to whichever checkout
    created it. A system or shim interpreter attributes nothing -- and that is the
    fleet's own case, not a hypothetical: a fleet worktree here has no ``.venv``
    and its workers run a global ``python3`` shim, so treating "not under a fleet
    worktree" as ``foreign`` would classify a real banned run INSIDE the fleet as
    somebody else's and never print it. That is the exact harm 2d exists to
    prevent, so the non-match stays ``unknown``.

    An empty or unset ``fleet_worktrees`` declares no scope, and scoping against
    an empty set would classify every match as ``foreign`` and mute the banned
    signal entirely -- a failure the conductor cannot see. Unscoped therefore
    means ``unknown``: every match is still reported and still counted, which is
    exactly the pre-2d behaviour.

    The comparison gets a second chance through ``realpath`` because a match
    missed is a banned run inside the fleet reported as somebody else's: it
    absorbs a symlinked worktree root and a Windows short (8.3) name, either of
    which spells the same directory a way the literal form does not.
    """
    try:
        target = os.readlink(proc_entry / "cwd")
    except OSError:
        # Usually a process that exited mid-scan; on a shared host also every
        # OTHER user's process, whose /proc entry this uid cannot follow.
        #
        # Asking WHO owns it would let most of the second group be summarised as
        # `foreign` instead of reported as `unknown`, and that refinement was
        # tried and withdrawn. It needs the current uid, and this file is part of a
        # standalone script run by a bare interpreter -- it imports nothing from
        # the package, so it cannot route through ``platform_compat``, and
        # ``os.getuid`` is POSIX-only: absent on Windows, where its absence
        # raises AttributeError rather than OSError and takes the whole scan
        # down. The exchange is a fail-open reading for a crash on one platform,
        # to buy quieter output on a path that is already correct. The PROGRAM
        # path is a different trade: cmdline is world-readable, so it costs no
        # new primitive and no portability risk, and it is asked below.
        return _program_class(cmd, fleet)
    if not fleet:
        return "unknown"
    try:
        literal = _norm_path(target)
        if any(_under(literal, _norm_path(root)) for root in fleet):
            return "fleet"
        real = _norm_path(os.path.realpath(target))
        if any(_under(real, _norm_path(os.path.realpath(root))) for root in fleet):
            return "fleet"
    except (OSError, ValueError):
        # Uncertain ownership answers `unknown`, never `fleet`: `fleet` is the one
        # class that stops a session, so a comparison that cannot be completed
        # must not promote a process into it.
        return "unknown"
    return "foreign"
