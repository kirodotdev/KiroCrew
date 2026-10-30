"""The git history-write classifier, the setting, the per-session allow and the steering line.

``hooks.confirm_git_writes`` (on by default) puts every git history write
(``security.git_write.git_history_write``) to the interactive card in dashboard
sessions until the session holds the per-session allow
(``slot._git_writes_allowed``); the ladder itself is pinned in
``test_git_write_ladder.py``. These tests pin the classifier's readings, the
setting's default and parse, the allow's record and the steering line.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew.context_assembly.output_contract import _CRITICAL_RULES, _CRITICAL_RULES_CHANNEL
from kiro_crew.dashboard.git_write_grant import (
    BY_PERSON,
    allow_git_writes,
    end_git_writes,
    git_writes_allowed,
)
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.hook_runtime.hook_types import HooksConfig
from kiro_crew.security import git_write
from kiro_crew.security.git_write import NOT_SINGLE_READ, UNREADABLE, git_history_write

# ── The classifier ──


@pytest.mark.parametrize(
    "command, expected",
    [
        ("git commit -m x", "git commit"),
        ("git --no-pager commit -am x", "git commit"),
        ("cd repo && git commit -m y", "git commit"),
        ("git push origin feature", "git push"),
        ("git merge origin/main", "git merge"),
        ("git rebase main", "git rebase"),
        ("git reset --hard HEAD", "git reset"),
        ("git revert HEAD", "git revert"),
        ("git cherry-pick abc123", "git cherry-pick"),
        ("git am < patch", "git am"),
        ("git pull", "git pull"),
        ("git tag v1", "git tag"),
        ("git tag -d v1", "git tag"),
        ("git branch -D old", "git branch"),
        ("git branch -m old new", "git branch"),
        ("git stash drop", "git stash"),
        ("git update-ref refs/heads/x HEAD", "git update-ref"),
        ("git checkout -B main origin/main", "git checkout"),
        ("git fetch origin main:main", "git fetch"),
        ("git notes add -m note", "git notes"),
        ("git config alias.co checkout", "git config"),
        ("sudo git push", "git push"),
        ("env git commit", "git commit"),
        ("/usr/bin/git commit", "git commit"),
        ('git "commit" -m x', "git commit"),
        ("bash -c 'git commit -m x'", "git passed to another program"),
        ("echo $(git commit -m x)", git_write.EXPANSION),
        ("echo `git commit -m x`", git_write.EXPANSION),
        ("GIT_DIR=.git git commit -m x", "git with a configuration override"),
    ],
)
def test_history_writes_are_recognised(command, expected):
    assert git_history_write(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --oneline -5",
        "git diff HEAD~1",
        "git show HEAD",
        "git rev-parse HEAD",
        "git config --get user.email",
        "git --no-pager log",
        "git --version",
        "ls -la",
        "gh pr view 1",
        "curl -s https://github.com/kirodotdev/KiroCrew",
        "make FOO=bar",
        "grep -r '#' .",
        "",
    ],
)
def test_known_git_reads_and_commands_without_git_do_not_ask(command):
    assert git_history_write(command) == ""


@pytest.mark.parametrize(
    "command",
    [
        # Not on the read list, even though they write no history.
        "git add -A",
        "git fetch origin",
        "git checkout main",
        "git switch main",
        "git restore f",
        "git clean -n",
        "git gc",
        "git worktree add --detach ../x",
        "git diff --output=/tmp/x.diff",
        "git grep -O foo",
        "git stash",
        "git config user.name someone",
        "git remote add origin x",
        # An argv that is not known until the shell runs.
        'git -C "$WT" status',
        "git log $(cat refs)",
        "git diff *.py",
        # git passed to another program.
        "echo git commit",
        "watch git status",
        "cat git-commit.md",
        # A .git path, or a lookup of the git program, is not a known read.
        "ls .git",
        "echo abc > .git/refs/heads/main",
        "which git",
        "HOME=/tmp/h git status",
        "GIT_DIR=/tmp/x git status",
    ],
)
def test_anything_that_is_not_a_known_read_asks(command):
    assert git_history_write(command)


@pytest.mark.parametrize("sub", ["frobnicate", "replay", "history", "Status", "co"])
def test_an_unknown_or_future_subcommand_is_a_write(sub):
    # The read list is positive: a subcommand git adds later, an alias, or a
    # different spelling of a read is not on it, so it asks.
    assert git_history_write(f"git {sub} --all") == f"git {sub}"


@pytest.mark.parametrize(
    "command",
    [
        # Ref creation and changes.
        "git branch newbranch",
        "git branch --del old",
        "git branch -u origin/main",
        "git checkout -b topic",
        "git switch -c topic",
        "git worktree add ../x",
        "git worktree add ../x -b y",
        "git gc --prune=now",
        "git log -1 --format=%H x --output=.git/refs/heads/main",
        # Shell reading.
        "gi\\\nt commit -m x",
        "g\\\nit push",
        "git tag newtag # --list",
        "$'\\x67it' commit -m x",
        "g$'\\x69't commit",
        "/usr/bin/g?t commit",
        "/usr/bin/[g]it commit",
        "{git,commit} -m x",
        "/usr/libexec/git-core/git-commit -m x",
        "git-push origin topic",
        "shopt -s expand_aliases\nalias g=git\ng commit",
        # Programs git runs, named in its environment.
        "GIT_EXTERNAL_DIFF='git commit' git diff",
        "GIT_PAGER=less git log",
        "GIT_SSH_COMMAND=ssh git fetch",
        # Runners and spellings.
        "timeout 5 git push",
        "nice -n 5 git commit",
        "env -u X git commit",
        "command -p git commit",
        "exec -a x git commit",
        "\\git commit",
        "Git commit",
        'sh -c "git commit"',
        "eval 'git commit'",
        "{ git commit; }",
        "if true; then git commit; fi",
        "f(){ git commit; }; f",
    ],
)
def test_review_round_spellings_read_as_writes(command):
    assert git_history_write(command)


@pytest.mark.parametrize(
    "command",
    [
        "$G commit -m x",  # the program is an expansion
        "git 'commit",  # quoting does not close
        "git $SUB",  # the subcommand is an expansion
        "git co",  # an alias, not a subcommand git ships
        "git -c alias.x=!sh x",  # configuration override
        "GIT_CONFIG_COUNT=1 git status",  # configuration override in the environment
        "ls | xargs git commit",  # argv read from input
        "echo $(git commit",  # substitution does not close
    ],
)
def test_unreadable_git_text_reads_as_a_write(command):
    assert git_history_write(command)


def test_unreadable_text_without_git_asks_when_a_quote_is_left_open():
    # A quote left open means the text was not read the way bash reads it.
    assert git_history_write("echo 'unterminated") == git_write.EXPANSION


# ── The setting ──


def test_confirm_git_writes_is_on_by_default():
    assert HooksConfig().confirm_git_writes is True
    assert HooksConfig.from_dict({}).confirm_git_writes is True


@pytest.mark.parametrize("raw", ["nope", None, [1], {}])
def test_a_malformed_setting_keeps_the_confirm_on(raw):
    # Only an explicit off turns it off; junk keeps it on.
    assert HooksConfig.from_dict({"confirm_git_writes": raw}).confirm_git_writes is True


def test_the_setting_can_be_turned_off_and_round_trips():
    cfg = HooksConfig.from_dict({"confirm_git_writes": False})
    assert cfg.confirm_git_writes is False
    assert HooksConfig.from_dict(cfg.to_dict()).confirm_git_writes is False


# ── The per-session allow ──


class TestThePerSessionAllow:
    def test_allow_records_who_set_it_and_audits(self, monkeypatch):
        from kiro_crew.dashboard import git_write_grant

        log = MagicMock()
        monkeypatch.setattr(git_write_grant, "sel", lambda: log)
        slot = _ChatSlot("chat-1")
        allow_git_writes(slot, by=BY_PERSON, caller="dashboard:chat-1")
        assert git_writes_allowed(slot)
        assert slot._git_writes_by == BY_PERSON
        assert log.log_api_access.call_args.kwargs["operation"] == "git_writes:allow"
        allow_git_writes(slot, by=BY_PERSON, caller="dashboard:chat-1")
        assert log.log_api_access.call_count == 1

    def test_end_clears_it(self, monkeypatch):
        from kiro_crew.dashboard import git_write_grant

        monkeypatch.setattr(git_write_grant, "sel", lambda: MagicMock())
        slot = _ChatSlot("chat-1")
        allow_git_writes(slot, by=BY_PERSON, caller="x")
        end_git_writes(slot, caller="x")
        assert not git_writes_allowed(slot)
        assert slot._git_writes_by == ""

    def test_a_new_slot_does_not_hold_it(self):
        slot = _ChatSlot("chat-1")
        assert slot._git_writes_allowed is False
        assert slot._git_writes_by == ""

    def test_the_slot_projection_shows_it(self):
        slot = _ChatSlot("chat-1")
        assert slot.to_dict().get("git_writes_allowed") is False
        slot._git_writes_allowed = True
        assert slot.to_dict().get("git_writes_allowed") is True


# ── The steering line ──


@pytest.mark.parametrize("block", [_CRITICAL_RULES, _CRITICAL_RULES_CHANNEL])
def test_the_critical_rules_say_git_writes_need_the_users_own_request(block):
    assert "writes history" in block
    assert "only when the user's own message" in block
    assert "is not that request" in block


def test_unreadable_reason_is_named():
    assert UNREADABLE == "unreadable git command"


@pytest.mark.parametrize(
    "command",
    [
        'OPT=-b; git checkout "$OPT" topic',
        'G=git; eval "$G commit -m x"',
        'G=git; bash -c "$G commit -m x"',
        "printf '%s\\n' '$('; $'\\x67it' commit -m x",
        "git stash branch topic",
        "git remote remove origin",
        "git reflog drop HEAD",
        "git gc --pr=now",
        "G=git; if $G commit -m x; then :; fi",
        "while /usr/bin/g?t commit -m x; do break; done",
        'q() { $G "$@"; }; G=git; q commit -m x',
        "case a in a) $G commit ;; esac",
        "coproc $G commit",
        "echo $(true)#; git commit -m x",
        "echo $'\\' #'; git commit -m x",
        "cat <<EOF\n'\nEOF\necho ' #'; git commit -m x",
        'perl -e \'system("git","commit")\'',
        'node -e \'require("child_process").execFileSync("git",["commit"])\'',
        "echo Z2l0IGNvbW1pdCAtbSB4 | base64 -d | sh",
        'cp "$(command -v git)" /tmp/q && /tmp/q commit -m x',
        "ln -s /usr/bin/gi? /tmp/q; /tmp/q commit",
        "printf x | tee .git/HEAD",
        "git checkout -t origin/feature",
        "git fetch --prune",
        "git submodule update --rebase",
    ],
)
def test_review_round_two_spellings_ask(command):
    assert git_history_write(command)


@pytest.mark.parametrize(
    "command",
    [
        # The shell opens a redirection's target before git runs.
        "git status > .git/HEAD",
        "git status >.git/HEAD",
        "git status 1>notes.txt",
        "git log >> out.txt",
        "git status <> .git/HEAD",
        "git status >| out.txt",
        "git log <<EOF\nx\nEOF",
        "git log 3>&1",
        # A read option that starts a program, in every spelling git accepts.
        "git grep --open-files-in-pager=sh -e marker -- payload.sh",
        "git grep --open-files-in-pager sh -e marker",
        "git grep --open=sh -e marker",
        "git grep -Osh -e marker",
        "git grep -iO -e marker",
        "git grep --open-files-in-pager=rm -l .",
        "git grep -Orm -l .",
        "git log -1 > .git/HEAD",
        "git show HEAD~5:src/a.py > src/a.py",
        # ls-remote options that name the program git starts.
        "git ls-remote --upload-pack='git push origin HEAD:main #' .",
        "git ls-remote -u 'touch marker' .",
        "git ls-remote -hu x .",
        "git ls-remote --exec='touch marker' .",
        "git ls-remote --upl=x .",
        # Configuration a later git read takes its programs from.
        "printf x >> ~/.gitconfig",
        'echo "$(printf x >> ~/.gitconfig)"',
        "git --git-dir=/tmp/e --work-tree=. status",
        # After a runner, the first plain word is the program.
        "nice python3 -c 'pass' git log",
        "env -u X git status",
        # A redirection after a group or loop applies to the git call inside.
        "{ git show HEAD~5:src/a.py; } > src/a.py",
        "{ git status;}>x",
        "for x in 1; do git log; done > out",
        "( git log; ) > out",
        "if true; then git log; fi > out",
        # Git inside a substitution, a redirection set by exec, a pipe into a writer.
        '{ printf "%s\\n" "$(git show HEAD~1:a.py)"; } > a.py',
        'echo "$(git show HEAD:a)" > a',
        "exec > src/a.py; git show HEAD~5:src/a.py",
        "git show HEAD:a | tee a",
        "git show HEAD:a |& /usr/bin/tee a",
        "git log | sponge f",
        # Any step beside a git read must itself be a known read.
        "git show HEAD~1:f | cp /dev/stdin f",
        "git show HEAD:a | command tee a",
        'git show HEAD:a | "tee" a',
        "git show HEAD:a | sudo tee a",
        "git show HEAD:a | sh -c 'cat > a'",
        "git show HEAD:a | sed -n 'w a'",
        "git show HEAD:a | sort -o a",
        "git show HEAD:a | install /dev/stdin a",
        "for x in 1; do git log; done 2>/dev/null",
    ],
)
def test_review_round_three_spellings_ask(command):
    assert git_history_write(command)


@pytest.mark.parametrize(
    "command",
    [
        'git log --grep="a>b"',
        'git log -- "x>y"',
        "git show HEAD~1:src/a.py",
        "git\tstatus",
    ],
)
def test_single_plain_git_reads_stay_reads(command):
    assert git_history_write(command) == ""


@pytest.mark.parametrize(
    "command",
    [
        # Redirections, even ones that only discard output.
        "git status 2>/dev/null",
        "git log 2>&1",
        "git diff >/dev/null 2>&1",
        "git status &>/dev/null",
        # Pipes, chains, groups, comments.
        "git log | head",
        "git log --oneline | grep fix",
        "git log --oneline -5 2>&1 | head",
        "git log 2>/dev/null | wc -l",
        "git diff --stat | cat",
        "git status && echo done",
        "cd /tmp/x && git status",
        "cd repo && git status && git diff --stat",
        "git status; git log -1",
        "git status\ngit log -1",
        "{ git status; git log -1; } 2>&1",
        "( git status )",
        "git log --oneline # then git commit",
        # Substitutions, expansions, globs, braces, escapes, tilde expansion.
        "echo $(git rev-parse HEAD)",
        "echo `git rev-parse HEAD`",
        'git log --grep="$X"',
        "git diff -- *.py",
        "git diff --{out,out}put=f",
        "git -C ~/repo status",
        "git log --author=~me",
        "git log \\--oneline",
        # Runners and environment prefixes.
        "timeout 5 git status",
        "nice -n 5 git log",
        "env LANG=C git status",
        "LANG=C git status",
        # Whitespace bash does not split on, and other non-ASCII text.
        "git show HEAD:f >&1\u00a0marker",
        "git status\u00a0--short",
        "git log --grep=caf\u00e9",
        "git status\x0b",
    ],
)
def test_anything_beyond_one_plain_git_call_asks(command):
    assert git_history_write(command)


def test_a_compound_form_of_a_known_read_names_the_shape():
    assert git_history_write("git log | head") == NOT_SINGLE_READ
    assert git_history_write("git status 2>/dev/null") == NOT_SINGLE_READ


# ── Per-subcommand option allow-lists ──


@pytest.mark.parametrize(
    "command",
    [
        # An option not listed for its subcommand asks, in any spelling.
        "git log --frobnicate",
        "git status --frobnicate=1",
        "git diff -Y",
        "git log --onel",
        "git log --one",
        # Options that start a program a configuration names, or a viewer.
        "git log --ext-diff -p",
        "git diff --ext-diff",
        "git diff --textconv",
        "git show --textconv HEAD",
        "git cat-file --filters HEAD:README.md",
        "git cat-file --textconv HEAD:README.md",
        "git log --show-signature",
        "git diff --submodule=log",
        "git stash show --ext-diff",
        "git stash list --textconv",
        "git reflog show --ext-diff",
        # Viewers and pagers.
        "git help status",
        "git help -w status",
        "git help --web status",
        "git --help status",
        "git -h",
        "git -p status",
        "git --paginate log",
        "git --bare log",
        # Signature checks start the signing program.
        "git verify-commit HEAD",
        "git verify-tag v1",
        # A listing subcommand followed by a word it does not list.
        "git reflog expire --all",
        "git reflog delete HEAD@{1}",
        "git submodule update",
        "git submodule foreach ls",
        "git notes add -m x",
        "git sparse-checkout set a",
        "git worktree list --frobnicate",
    ],
)
def test_an_unlisted_option_or_subcommand_asks(command):
    assert git_history_write(command)


@pytest.mark.parametrize(
    "command",
    [
        "git log --oneline -n 5",
        "git log --oneline -20",
        "git log -p --stat --since=2.weeks",
        "git log --graph --decorate --all",
        "git show --stat HEAD",
        "git show HEAD~1:src/a.py",
        "git diff --cached --name-only",
        "git diff -U5 --no-ext-diff --no-textconv",
        "git status --short --branch",
        "git status -sb",
        "git status -uno",
        "git status --porcelain=v2",
        "git rev-parse --show-toplevel",
        "git rev-parse --abbrev-ref HEAD",
        "git --no-pager log -1",
        "git --version",
    ],
)
def test_listed_options_stay_reads(command):
    assert git_history_write(command) == ""


# Removed from the read list: each of these now asks.
@pytest.mark.parametrize(
    "command",
    [
        "git blame -L 10,20 src/a.py",
        "git cat-file -p HEAD",
        "git config user.name",
        "git for-each-ref --format='%(refname)' refs/heads",
        "git grep -n -i -e pattern",
        "git grep -nC3 pattern",
        "git grep -o foo",
        "git ls-files --others --exclude-standard",
        "git ls-remote --heads origin",
        "git ls-remote -h origin",
        "git ls-remote origin",
        "git merge-base --is-ancestor HEAD main",
        "git reflog show -n 5",
        "git reflog",
        "git remote -v",
        "git stash list",
        "git stash show -p",
        "git submodule status",
        "git tag -l",
        "git tag",
        "git worktree list --porcelain",
        "git worktree list",
        "git tag -v v1",
        "git tag --verify v1",
        "git log -1 --format=%GG",
        "git log --format='%G?'",
        "git show --pretty=%GS HEAD",
        "git log --pretty=format:%GK",
        "git log --format %GG",
        "git branch --format='%(signature)'",
        "git config edit",
        "git config --global edit",
        "git config core.editor",
        "git config --get-all core.editor",
        "git config --list --show-origin",
        "git log --alternate-refs --oneline",
        "git remote show origin",
        "git remote show -n origin",
        "git ls-remote --get-url origin",
        "git --namespace=x log",
        "git --literal-pathspecs status",
        "git -v",
        "git branch new-branch",
        "git branch -v new-branch",
        "git branch --format=%(refname)",
        "git log --format='#%H'",
        "git -C /tmp/repo status --short",
        "git log --format='%H %s'",
        "git log --format='%h $not_expanded'",
        "git -C /tmp/x status",
        "/usr/bin/git status",
        "git --no-pager show --no-patch --format='%+GG' HEAD",
        "git log -n 1 --format='%+G?'",
        "git log --format='%-G?'",
        "git log --format='% G?'",
        "git log --pretty=myalias",
        "git log --format='%(describe)'",
        "./git status",
        "GIT status",
        "Git.EXE status",
        # ``branch`` is off the read list in every form.
        "git branch",
        "git branch --list",
        "git branch -a",
        "git branch -r",
        "git branch --show-current",
        "git branch --list 'feat*'",
        "git branch -- name --list",
        "git branch -- name -l",
        # ``--version`` is a read only as the whole argv.
        "git --version --help",
        "git --version x",
        "git --version -h",
        "git --no-pager --version",
        "git --version status",
    ],
)
def test_reads_outside_the_short_list_ask(command):
    assert git_history_write(command)


def test_git_version_alone_is_a_read():
    assert git_history_write("git --version") == ""


def test_a_long_config_key_is_read_in_linear_time():
    from kiro_crew.security.git_write import _CONFIG_KEY_RE

    # A middle part that cannot hold a dot splits a key one way only, so a
    # 10,002-character key is decided in one pass instead of 2**5000 tries.
    assert r"(?:\.[^\s=.]+)*" in _CONFIG_KEY_RE.pattern
    hostile = "A." + "!." * 5000 + " "
    assert _CONFIG_KEY_RE.match(hostile) is None
    long_key = "url." + ".".join(["part"] * 2000) + ".insteadOf"
    assert _CONFIG_KEY_RE.match(long_key)
    assert git_history_write("git config --get url.https://example.com/x.insteadOf") == ""


@pytest.mark.parametrize(
    "command",
    [
        # A quoted or escaped closing brace inside a list.
        "{g,'}'}it commit -m x",
        "{g,\\}}it commit -m x",
        # Sequence expressions and nested lists.
        "{g..g}it commit -m x",
        "{f..h}it commit -m x",
        "{1..3}git commit -m x",
        "{1..9..2}git commit -m x",
        "{{f..g},h}it commit -m x",
        "{,}{,}{,}{,}{,}{g,}it commit -m x",
        "{,}{,}{,}{,}{,}{g,}it-push --force origin x",
        # Braces that do not close or hold one part.
        "{g it commit -m x",
        "{g}it commit -m x",
        "/usr/{bin/g,x}it commit -m x",
        # An escaped opening brace counts as unquoted.
        "\\{g,}it commit -m x",
        # Behind a runner, another program or a shell body.
        "env {g,}it commit -m x",
        "xargs {g,}it commit -m x",
        "find . -exec {g..g}it commit -m x \\;",
        "sh -c '{g,}it commit -m x'",
        "eval '{g,}it commit -m x'",
        "if true; then {g,}it commit -m x; fi",
        "echo $({g,}it commit -m x)",
        '"$({g,}it commit -m x)"',
        # A brace after ``$$`` or after a ``${name}`` is a brace.
        "$${g,}it commit -m x",
        "${x}{g,}it commit -m x",
        "${x:-{g,}it} commit -m x",
        # Redirect targets and file arguments.
        "echo x > ~/.g{i..i}tconfig",
        "echo x >> .git/{config,}",
        "echo x > ~/.g{i,}tconfig",
        "cp evil .{g,}it/config",
        # A brace in a git argument word.
        "git diff --{out,out}put=f",
        "git log {a,b}",
        "git show HEAD@{1}",
    ],
)
def test_an_unquoted_brace_asks(command):
    assert git_history_write(command)


@pytest.mark.parametrize(
    "command",
    ["{g,'}'}it commit -m x", "{g,\\}}it commit -m x", "echo x > ~/.g{i..i}tconfig"],
)
def test_an_unquoted_brace_is_named_as_the_reason(command):
    assert git_history_write(command) == git_write.EXPANSION


@pytest.mark.parametrize(
    "command",
    [
        "git log --grep '{x}'",
        'git log --grep "{x}"',
        "git show 'HEAD@{1}'",
        "{ ls; }",
        "{ ls; } 2>&1",
        "ls -la",
    ],
)
def test_a_quoted_brace_parameter_form_or_group_keyword_does_not_ask(command):
    assert git_history_write(command) == ""


@pytest.mark.parametrize(
    "command",
    [
        "echo {a,b}",
        "echo {1..3}",
        "mkdir -p src/{a,b}",
        'curl -d \'{"a":{"b":1}}\' https://example.com',
        "awk '{print $1}' f",
        "find . -name x -exec grep y {} \\;",
    ],
)
def test_a_brace_in_a_command_without_git_asks(command):
    # Braces are not expanded, so a command that names no git still asks when an
    # unquoted brace could produce a git word or path. A quoted program body
    # (awk, curl JSON) is re-read as a command, so its braces count there.
    assert git_history_write(command)


def test_brace_scan_is_linear():
    from kiro_crew.security import git_write

    # One pass over the text, no expansion: the scan stops at the first brace.
    assert git_write._has_unquoted_expansion("'" + "{" * 100000 + "'") is False
    assert git_write._has_unquoted_expansion("a" * 100000 + "{b,c}") is True
    assert not hasattr(git_write, "_expand_braces")


@pytest.mark.parametrize(
    "command",
    [
        # A parameter expansion spelling a git configuration path.
        "echo x > ~/.g${x:-i}tconfig",
        "echo x > .g${x:-i}t/config",
        "cp evil .g${x:-i}t/config",
        "echo x > ~/.g${x:-i}tconfig.work",
        # One per expansion class.
        "cp evil .g?t/config",
        "cp evil .gi[t]/config",
        "cp evil .g*/config",
        "$(printf git) commit -m x",
        "`printf git` commit -m x",
        '"$X" commit -m x',
        "$X commit -m x",
        "$'\\x67it' commit -m x",
        '$"git" commit -m x',
        "echo x > >(cat)",
        "cat <(echo x) > .g${x}t/config",
        "cp evil .@(git)/config",
        "cp evil .!(x)/config",
        "cp evil .+(git)/config",
        "cp evil ~user/.gitconfig",
        "cp evil ~+/.gitconfig",
        "\\$X commit -m x",
        "cp evil .g\\*/config",
        # Inside double quotes, $ and backtick still expand.
        'echo x > "$HOME/.gitconfig"',
        'echo x > "`printf .git`/config"',
        # Inside a shell body handed as a quoted string.
        "sh -c 'cp evil .g?t/config'",
        "eval 'echo x > $H/.gitconfig'",
    ],
)
def test_any_shell_expansion_asks(command):
    assert git_history_write(command)


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --oneline",
        "git show HEAD~1:f",
        "git log --grep '{x}'",
        "git log --grep '$x'",
        "cp a ~/b",
        "echo ~",
        "ls -la",
        "[ -f x ]",
        "[[ -f x ]]",
        "{ ls; }",
    ],
)
def test_reads_plain_tilde_and_keywords_do_not_ask(command):
    assert git_history_write(command) == ""


@pytest.mark.parametrize(
    "command",
    [
        "echo $HOME > notes.txt",
        "ls *.py",
        "echo ${HOME}",
        "grep 'foo$' notes.txt",
        "cat ~/.gitconfig",
    ],
)
def test_harmless_commands_that_ask(command):
    # Nothing is expanded, so a harmless command holding an expansion asks too.
    # A quoted ``$`` in a command that names no git counts as well, because a
    # program can run its argument as shell code. ``cat ~/.gitconfig`` names
    # git and is not a git call, so it asks as before.
    assert git_history_write(command)


@pytest.mark.parametrize(
    "command",
    [
        # A quote character in a heredoc body, then a glob after the body.
        "cat <<'E'\nit's\nE\ncp evil .g?t/config",
        "cat <<E\nit's\nE\ncp evil .g?t/config",
        'cat <<E\nsay "hi\nE\ncp evil .g?t/config',
        "cat <<'E'\nit's\nE\ncp evil .gi[t]/config",
        "cat <<'E'\nit's\nE\ncp evil .gi*/config",
        "cat <<'E'\nit's\nE\ncp evil ~/.gitconf?g",
        "cat <<'E'\nit's\nE\nmv evil .g?t/hooks/pre-commit",
        "cat <<-E\n\tit's\n\tE\ncp evil .g?t/config",
        # A heredoc or herestring with nothing else in it.
        "cat <<EOF\nhello\nEOF",
        "cat <<< hello",
        "wc -l <<<'a b'",
        "git status <<< x",
        "git log --oneline <<E\nx\nE",
    ],
)
def test_a_heredoc_or_herestring_asks(command):
    assert git_history_write(command) == git_write.EXPANSION


@pytest.mark.parametrize(
    "command",
    [
        "echo 'it",
        'echo "it',
        "git status '",
        'git log --oneline "',
        "echo x \\",
        "git status \\",
    ],
)
def test_an_unclosed_quote_or_trailing_backslash_asks(command):
    assert git_history_write(command) == git_write.EXPANSION


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --oneline",
        "git diff --stat",
        "git show HEAD",
        "git rev-parse HEAD",
        "git config --get user.name",
        "git log --grep '<<'",
        'echo "a << b"',
        "echo 'it'\"'\"'s'",
        'echo "it\'s"',
        "sort < notes.txt",
        "[[ a < b ]]",
    ],
)
def test_quoted_heredoc_text_and_closed_quotes_do_not_ask(command):
    assert git_history_write(command) == ""


@pytest.mark.parametrize(
    "command",
    [
        "cat <<'EOF' > notes.txt\nhello\nEOF",
        "python3 - <<'EOF'\nprint(1)\nEOF",
        "ls  # it's here",
    ],
)
def test_harmless_heredoc_and_open_quote_commands_that_ask(command):
    # A heredoc is never read for where its body ends, so an agent piping text
    # through one asks. A comment holding a lone quote ends the raw reading
    # inside a quote, so it asks too.
    assert git_history_write(command)


@pytest.mark.parametrize(
    "command",
    [
        # An escaped backslash before a line break inside double quotes.
        'echo "\\\\\n"; cp evil .g?t/config; echo "\\\\\n"',
        'echo "\\\\\r\n"; cp evil .g?t/config; echo "\\\\\r\n"',
        'echo "\\\\\n"; cp evil .git/config; echo "\\\\\n"',
        # A plain continuation, outside and inside quotes.
        "gi\\\nt commit -m x",
        "gi\\\r\nt commit -m x",
        "git status \\\n  --short",
        "echo x \\\n  > notes.txt",
        "echo 'a\\\nb'",
        'echo "a\\\nb"',
    ],
)
def test_a_backslash_line_break_asks(command):
    assert git_history_write(command) == git_write.EXPANSION


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git status --short",
        "git log --oneline",
        "git diff --stat",
        "git show HEAD",
        "git rev-parse HEAD",
        "git config --get user.name",
        "git --version",
        'echo "a\\\\b"',
        "echo 'a\\b'",
        "ls\nls -la",
    ],
)
def test_single_line_reads_and_plain_line_breaks_do_not_ask(command):
    assert git_history_write(command) == ""


@pytest.mark.parametrize(
    "command",
    [
        "echo hit commit -m x | tr h g | sh /dev/stdin",
        "echo hit commit -m x | tr h g | bash /dev/stdin",
        "echo hit commit -m x | tr h g | sh /dev/fd/0",
        "echo hit commit -m x | tr h g | sh /proc/self/fd/0",
        "echo hit commit -m x | tr h g | sh /proc/1234/fd/0",
        "echo hit commit -m x | tr h g | . /dev/stdin",
        "echo hit commit -m x | tr h g | source /dev/stdin",
        "echo hit commit -m x | tr h g | dash script.sh",
        "echo hit commit -m x | tr h g | zsh -s",
        "echo hit commit -m x | tr h g | busybox sh",
        "echo hit commit -m x | tr h g | /bin/bash x.sh",
        "echo hit commit -m x | tr h g | env bash x.sh",
        "sh /dev/stdin < notes.txt",
        "bash /dev/fd/3 3< notes.txt",
        "ksh /proc/self/fd/0",
        "fish /dev/stdin",
        ". /dev/fd/0",
    ],
)
def test_a_shell_that_may_read_a_pipe_asks(command):
    assert git_history_write(command) == git_write.UNREADABLE


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --oneline",
        "git diff --stat",
        "bash build.sh",
        "sh -c 'echo hi'",
        "ls | wc -l",
        "cat notes.txt | grep x",
    ],
)
def test_reads_and_shells_without_a_pipe_do_not_ask(command):
    assert git_history_write(command) == ""
