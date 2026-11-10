"""A glob that can name several ssh-family programs, read where bash may run it.

``s*`` can name ssh, scp and sftp; ``*s*`` and ``[rs]s*`` can name rsync too.  Bash
may expand such a glob to any of them, so where it may run as a program the floor
refuses it outright without parsing options or requiring a self-host operand.
A glob proven to be a listed data program's operand (``git add s* README.md``)
keeps the reading of one program. The rest of the floor's tests live
in ``test_denied_commands_security_ssh_self.py``.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _denied_by

from kiro_crew import security
from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import argv_floor_exec as _argv_floor_exec

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_RULE = "sandbox-escape-ssh-self"
#: Names the fail-closed resolver stub knows are other machines.
_KNOWN_REMOTE = frozenset(
    {"build.example.org", "backup.example.net", "mirror.example.com", "example.com"}
)


@pytest.fixture(autouse=True)
def _pin_own_host_cache(request, _floor_monkeypatch):
    # The same pins as ``TestSandboxEscapeSshSelf._pin_own_host_cache``: a fixed
    # own-name cache with the resolved-once latch set, so no resolver thread starts.
    own_mp = pytest.MonkeyPatch()
    request.addfinalizer(own_mp.undo)
    own = security.socket.gethostname().strip().lower()
    pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
    _floor_monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
    own_mp.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
    _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)


@pytest.fixture
def _lvh_is_this_host(monkeypatch):
    monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: host == "lvh.me")


@pytest.fixture
def _unknown_dotted_names_fail_closed(monkeypatch):
    # A cache miss on a dotted name fails closed; a dotless name is asked with
    # fail_closed=False and answers open on a miss, as the real verdict does.
    def _verdict(host, *, fail_closed=True, **_kw):
        return fail_closed and host not in _KNOWN_REMOTE

    monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", _verdict)


@pytest.mark.usefixtures("_lvh_is_this_host")
@pytest.mark.parametrize(
    "cmd",
    [
        # Program position, bare and behind wrappers the resolver steps over.
        "s* lvh.me id",
        "env s* lvh.me id",
        "sudo s* lvh.me id",
        "xargs s* lvh.me id",
        "/usr/bin/s* lvh.me id",
        # A leading redirect: the resolver takes the redirect word for the program.
        "2>/dev/null s* lvh.me id",
        ">/dev/null s* lvh.me id",
        "2>/dev/null /usr/bin/s* lvh.me id",
        # find's -exec family runs the glob.
        "find . -exec s* lvh.me id \\;",
        "find . -execdir s* lvh.me id \\;",
        "sudo find . -exec s* lvh.me id +",
        # Programs that run a word of their operands.
        "strace s* lvh.me id",
        "strace -f s* lvh.me id",
        "watch s* lvh.me id",
        # An option whose value only one of the programs reads, before a redirect.
        "s* -X lvh.me>/dev/null id",
        "s* -x lvh.me>/dev/null id",
        "s* -X lvh.me</dev/null id",
        "s* -X lvh.me>>/dev/null id",
        "s* -X lvh.me > /dev/null id",
        "s* -x lvh.me > /dev/null id",
    ],
)
def test_a_glob_naming_several_programs_where_bash_may_run_it_is_refused(cmd):
    assert _denied_by(cmd) == _RULE, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # rsync's remote shell, behind a glob that can also name scp, ssh or sftp.
        "*s* -e 'ssh localhost id #' a b:c",
        "*s* --rsh 'ssh localhost id' a b:c",
        "*s* --rsh='ssh localhost id' a b:c",
        "*s* -ave 'ssh localhost id' a b:c",
        "*s* -e'ssh localhost id' a b:c",
        # The same selector inherited from the environment.
        "rsync_rsh='ssh localhost' *s* a b:c",
        "export rsync_rsh='ssh localhost'; *s* a b:c",
        # An archive sync to this host behind a glob whose first program is ssh.
        "[rs]s* -a x localhost:/y",
        "*s* -a x localhost:/y",
        # The same forms behind a redirect, find -exec, strace and watch.
        "2>/dev/null *s* -e 'ssh localhost id' a b:c",
        "find . -exec [rs]s* -a x localhost:/y \\;",
        "strace [rs]s* -a x localhost:/y",
        "watch *s* -e 'ssh localhost id' a b:c",
    ],
)
def test_rsync_readings_of_a_glob_naming_several_programs_are_refused(cmd):
    assert _denied_by(cmd) == _RULE, cmd


@pytest.mark.usefixtures("_unknown_dotted_names_fail_closed")
@pytest.mark.parametrize("host", ["localhost", "example.com"])
@pytest.mark.parametrize(
    "cmd",
    [
        "*s* -o {host}:/tmp/source ./dest",
        "*s* -vo {host}:/x ./d",
        "*s* -avo {host}:/x ./d",
        "[rs]s* -o {host}:/x ./d",
        "env *s* -o {host}:/x ./d",
        "sudo *s* -o {host}:/x ./d",
        "2>/dev/null *s* -o {host}:/x ./d",
        "find . -exec *s* -o {host}:/x ./d \\;",
        "find . -execdir *s* -o {host}:/x ./d \\;",
        "strace *s* -o {host}:/x ./d",
        "*s* -e {host}:/x ./d",
        "*s* --rsh {host}:/x ./d",
        "*s* -R {host}:9000:example.com:22 ./d",
        "*s* -J {host}:/x ./d",
        "*s* -o 'ssh {host} id' a b:c",
    ],
)
def test_options_cannot_make_an_executed_multi_program_glob_safe(cmd, host):
    command = cmd.format(host=host)
    assert _denied_by(command) == _RULE, command


@pytest.mark.parametrize(
    "cmd",
    [
        "s*",
        "*s*",
        "[rs]s*",
        "/usr/bin/s*",
        "s* example.com id",
        'case "$f" in x) s* example.com ;; esac',
        'case "$f" in *.md) ;; s*) s* example.com ;; esac',
        "bash -c 'cd /tmp; s* example.com'",
        "bash -c 'cd /tmp; strace s* example.com'",
        "s* build.example.org uptime",
        "s* -v example.com id",
        "*s* -a x example.com:/y",
        "env s* example.com id",
        "sudo [rs]s* foo bar",
        "2>/dev/null s* x",
        "find . -exec s* x \\;",
    ],
)
def test_an_executed_multi_program_glob_is_refused_without_a_self_host(cmd):
    assert _denied_by(cmd) == _RULE, cmd


@pytest.mark.parametrize(
    "prefix",
    [
        "env",
        "sudo",
        "time",
        "nice",
        "timeout 10",
        "nohup",
        "command",
        "xargs",
        "strace",
        "watch",
        "ltrace",
        "valgrind",
        "2>/dev/null",
        "find . -exec",
        "find . -execdir",
        "find . -ok",
        "find . -okdir",
    ],
)
@pytest.mark.parametrize("glob", ["s*", "*s*", "[rs]s*", "/usr/bin/s*", "*[cp]"])
def test_wrappers_cannot_make_an_executed_multi_program_glob_safe(prefix, glob):
    command = f"{prefix} {glob} example.com"
    if prefix.startswith("find "):
        command += " \\;"
    assert len(_argv_floor._ssh_glob_names(glob)) >= 2
    assert _denied_by(command) == _RULE, command


@pytest.mark.parametrize(
    "command",
    [
        "case x in x) s?h -F /dev/null local* id;; esac",
        "2>/dev/null s?h -F /dev/null local* id",
        "> /dev/null s?h -F /dev/null local* id",
        "find . -maxdepth 0 -exec s?h -F /dev/null local* id \\;",
        "find . -execdir s?h -F /dev/null local* id \\;",
        "find . -ok s?h -F /dev/null local* id \\;",
        "find . -okdir s?h -F /dev/null local* id \\;",
        "strace s?h -F /dev/null local* id",
        "watch s?h -F /dev/null local* id",
        "env s?h -F /dev/null local* id",
        "sudo s?h -F /dev/null local* id",
        "ltrace s?h -F /dev/null local* id",
        "foo s?h local*",
    ],
)
def test_unproven_single_program_globs_keep_host_matching(command):
    assert _argv_floor._ssh_glob_names("s?h") == ("ssh",)
    assert _denied_by(command) == _RULE, command


@pytest.mark.usefixtures("_lvh_is_this_host")
def test_a_literal_verb_redirect_keeps_the_host_checks_of_an_ambiguous_option():
    # ssh ``-c`` takes a value and ``-C`` does not; the command line is lowercased,
    # so the word after ``-c`` may be the destination.  A redirect glued to it does
    # not change that.
    assert _denied_by("ssh -c lvh.me id") == _RULE
    assert _denied_by("ssh -c lvh.me>/dev/null id") == _RULE


@pytest.mark.usefixtures("_lvh_is_this_host")
@pytest.mark.parametrize(
    "cmd",
    [
        # GPT span 4fda723f6f7e: a quoted remote-shell or routing value that holds
        # ``>`` or ``<`` must not be read as an outer redirect.
        "*s* -e 'ssh localhost id # > ignored' a b:c",
        "*s* -e 'ssh localhost id # < ignored' a b:c",
        "*s* --rsh 'ssh localhost >x' a b:c",
        "*s* --rsh='ssh localhost >x' a b:c",
        "*s* -ave 'ssh localhost >x' a b:c",
        "*s* -e'ssh localhost >x' a b:c",
        "s* -o proxycommand='ssh localhost id >x' far.example.test:",
        "s* -oproxycommand='ssh localhost id >x' far.example.test:",
        # Behind a leading redirect and a find -exec, same values.
        "2>/dev/null *s* -e 'ssh localhost id > ignored' a b:c",
        "find . -exec *s* -e 'ssh localhost id > ignored' a b:c \\;",
    ],
)
def test_a_redirect_char_inside_a_quoted_value_is_not_an_outer_redirect(cmd):
    assert _denied_by(cmd) == _RULE, cmd


@pytest.mark.usefixtures("_lvh_is_this_host")
@pytest.mark.parametrize(
    "cmd",
    [
        # Opus span a06a491b21e3: a program outside the data list may run a word
        # of its operands, so a glob that can name ssh or sftp keeps its host
        # checks after it.  An unknown program is treated the same way.
        "ltrace s* lvh.me id",
        "valgrind s* lvh.me id",
        "gdb --args s* lvh.me id",
        "parallel s* lvh.me id",
        "strace s* lvh.me id",
        "watch s* lvh.me id",
        "foo s* lvh.me id",
        "sudo ltrace s* lvh.me id",
        # find is a data program, but its -exec action runs the glob.
        "find . -exec s* lvh.me id \\;",
        "find . -okdir s* lvh.me id \\;",
    ],
)
def test_a_runner_of_a_glob_naming_ssh_keeps_the_host_checks(cmd):
    assert _denied_by(cmd) == _RULE, cmd


@pytest.mark.usefixtures("_unknown_dotted_names_fail_closed")
@pytest.mark.parametrize(
    "cmd",
    [
        # A glob after a known data program, directly or behind a wrapper, is a
        # file name: no dotted word after it reaches the DNS layer.
        "git add s* README.md",
        "git add src/* README.md",
        "tar -czf handoff.tgz src/* pyproject.toml",
        "git diff --stat -- src/* CHANGELOG.md",
        "ruff check s* setup.py",
        "sudo git add s* README.md",
        "env tar -czf handoff.tgz s* pyproject.toml",
        "time ruff check s* setup.py",
        "find s* -name notes.md",
        "ls * notes.md",
        "cp *.py dist/notes.v2",
        "0 * * * * backup.sh",
        "*/5 * * * * run.py --out daily.csv",
        "ssh deploy@build.example.org uptime",
        "scp ./notes.md backup.example.net:/tmp/",
        "rsync -a ./site/ mirror.example.com:/srv/site/",
        "ss? example.com",
        "ssh example.com",
    ],
)
def test_ordinary_lines_stay_allowed_when_unknown_names_fail_closed(cmd):
    assert _denied_by(cmd) is None, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # Opus span on acf7687d4828: the ``&`` or ``|`` of a redirect glued to the
        # next word is not a command separator.  Bash runs one simple command
        # here, the redirect and then ``s* {host} id``.
        ">&cat s* {host} id",
        ">|cat s* {host} id",
        "2>&1cat s* {host} id",
        "<&0cat s* {host} id",
        "&>cat s* {host} id",
        "&>>cat s* {host} id",
        "1>|cat s* {host} id",
        "2>&1 >&cat s* {host} id",
    ],
)
@pytest.mark.parametrize("host", ["localhost", "example.com"])
def test_a_redirect_glued_to_a_data_program_does_not_end_the_command(cmd, host):
    command = cmd.format(host=host)
    assert _denied_by(command) == _RULE, command


@pytest.mark.parametrize(
    "cmd",
    [
        "git add s* README.md 2>&1",
        "ls s* >&2",
        "cat s* >| out.txt",
        "echo a & cat s*",
        # A real ``&`` glued between words still ends the first command.
        "echo a&cat s* localhost id",
        "echo a&cat s* example.com id",
    ],
)
def test_redirects_and_real_separators_keep_data_globs_allowed(cmd):
    assert _denied_by(cmd) is None, cmd


@pytest.mark.parametrize(
    "token, words",
    [
        (">&cat", [">&cat"]),
        (">|cat", [">|cat"]),
        ("2>&1", ["2>&1"]),
        ("&>cat", ["&>cat"]),
        ("a&b", ["a", "&", "b"]),
        ("a|b", ["a", "|", "b"]),
        ("a&&b", ["a", "&&", "b"]),
        ("a>&b&c", ["a>&b", "&", "c"]),
    ],
)
def test_cut_words_cuts_only_at_control_operators(token, words):
    assert _argv_floor_exec._cut_words([token]) == (words, [0] * len(words))


@pytest.mark.parametrize(
    "tokens, proven",
    [
        (["git", "add", "s*", "readme.md"], {1, 2, 3}),
        (["sudo", "tar", "-czf", "x", "s*"], {2, 3, 4}),
        # The program position itself is never an operand.
        (["s*", "lvh.me"], {1}),
        (["env", "s*", "lvh.me"], {2}),
        # A leading redirect is taken for the program, so nothing after it is proven.
        (["2>/dev/null", "s*", "lvh.me"], set()),
        # find's -exec family runs the words after it.
        (["find", ".", "-name", "x", "-exec", "s*", "lvh.me", ";"], {1, 2, 3}),
        # A runner the wrapper resolver does not step over proves its operands;
        # the ssh floor keeps the host checks on them when the glob names ssh/sftp.
        (["strace", "-f", "s*", "lvh.me"], {1, 2, 3}),
        (["watch", "s*", "lvh.me"], {1, 2}),
        # Array elements and a substitution's first command are read in their own
        # frames.  (The resolver reads ``s*`` as the array's program, so the
        # closing ``)`` counts as its operand; it names nothing.)
        (["files=(", "s*", "*.*", ")"], {1, 2, 3}),
        (["n=$(ls", "s*", "*.*)"], {1, 2}),
        # A comment's words and a case arm pattern are never run.
        (["tar", "x", "#", "s*", "*.*"], {1, 2, 3, 4}),
        (["s*)", "s*", "lvh.me"], {0, 2}),
        # A word that is also run in a later command is not proven; the words
        # after it are that program's operands.
        (["git", "add", "x;s*", "lvh.me"], {1, 3}),
    ],
)
def test_proven_operand_indices_mark_only_words_handed_to_another_program(tokens, proven):
    assert _argv_floor_exec._proven_operand_indices(tokens) == proven


@pytest.mark.parametrize(
    "tokens, runner",
    [
        # A listed data program hands its operands over as names.
        (["git", "add", "s*", "readme.md"], set()),
        (["tar.exe", "-czf", "x", "s*"], set()),
        # Wrappers resolve to the real program.
        (["sudo", "git", "add", "s*"], set()),
        (["env", "tar", "s*"], set()),
        (["time", "ruff", "check", "s*"], set()),
        # Any other program, known runner or not, keeps its operands checkable.
        (["ltrace", "s*", "lvh.me"], {1, 2}),
        (["sudo", "valgrind", "s*"], {2}),
        (["foo", "s*", "lvh.me"], {1, 2}),
        # Words that are never run stay out.  (The resolver reads ``s*`` as the
        # array's program, so the closing ``)`` counts; it names nothing.)
        (["files=(", "s*", ")"], {2}),
        (["n=$(foo", "s*", "x)"], set()),
    ],
)
def test_runner_operand_indices_leave_out_known_data_programs(tokens, runner):
    assert _argv_floor_exec._runner_operand_indices(tokens) == runner


@pytest.mark.parametrize(
    "command",
    [
        "*",
        "**",
        "* *",
        "** *",
        "* lvh.me",
        "** lvh.me",
        "* lvh.me id",
        "** lvh.me id",
        "env * lvh.me id",
        "sudo ** example.com",
        "2>/dev/null * x",
        "find . -exec * x \\;",
        "find . -execdir * x \\;",
        "find . -ok * x \\;",
        "find . -okdir * x \\;",
        "* * * * * curl -s https://status.example.com/ping",
    ],
)
def test_all_star_command_words_are_refused(command):
    assert _denied_by(command) == _RULE, command


def test_literal_filenames_follow_the_refusal_notes_advice():
    from kiro_crew.security.denied_rules_tables import _SELF_PROTECTION_FLOOR_NOTES

    note = _SELF_PROTECTION_FLOOR_NOTES[_RULE]
    assert "name the files literally" in note
    assert "in front of each file glob" not in note
    assert _denied_by("npx prettier --check s* *.*") == _RULE
    assert _denied_by("npx prettier --check ./s* ./*.*") == _RULE
    assert _denied_by("npx prettier --check src/app.py README.md") is None
