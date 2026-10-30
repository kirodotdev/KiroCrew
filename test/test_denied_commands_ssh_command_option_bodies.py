"""An ssh-family option value that names a LOCAL command is judged as a command.

``ssh``/``scp``/``sftp`` run the value of a ``*Command`` option through ``$SHELL -c``
on this host, ``rsync`` runs its ``-e``/``--rsh`` value, and a leading
``GIT_SSH_COMMAND=``/``RSYNC_RSH=`` assignment is a command line run from here. The
self-protection floors (credential mint, restart, kill) must see each of those bodies
exactly as they see a ``bash -c`` body. ``shell_normalizer_ssh_options`` feeds them to
the nested-command walk; this module pins that every allowed row from the filed report
is now refused, and that an ordinary ssh ProxyCommand and other legitimate option
values stay allowed.
"""

from __future__ import annotations

import pytest

from kiro_crew import security
from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import is_denied
from kiro_crew.security.shell_normalizer import _argv_programs
from kiro_crew.security.shell_normalizer_ssh_options import ssh_command_option_payloads


def _payloads(tokens):
    return ssh_command_option_payloads(tokens, _argv_programs(tokens))


# Match the ssh-self family: start with the macOS libc/libproc handle caches unbuilt.
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


# Bodies that must be REFUSED when carried as an ssh-family option value. Each is a
# self-protection command the gate already refuses when wrapped in ``bash -c``.
_REFUSED = [
    "ssh -oProxyCommand='kirocrew token' x",
    "ssh -o ProxyCommand='kirocrew token' x",
    "ssh -o 'ProxyCommand kirocrew token' x",
    "ssh -oProxyCommand='bash -c \"kirocrew token\"' x",
    "ssh -oPermitLocalCommand=yes -oLocalCommand='kirocrew token' x",
    "ssh -o PermitLocalCommand=yes -o 'LocalCommand kirocrew token' x",
    "ssh -oKnownHostsCommand='kirocrew token' x",
    "ssh -voProxyCommand='kirocrew token' x",
    "SSH -OPROXYCOMMAND='kirocrew token' x",
    "scp -oProxyCommand='kirocrew token' f x:/tmp/",
    "sftp -oProxyCommand='kirocrew token' x",
    "rsync -e \"ssh -oProxyCommand='kirocrew token'\" f x:/tmp/",
    "rsync -e 'kirocrew token' f x:/tmp/",
    "rsync --rsh='kirocrew token' f x:/tmp/",
    "rsync -ave 'kirocrew token' f x:/tmp/",
    "GIT_SSH_COMMAND='kirocrew token' git fetch x",
    "RSYNC_RSH='kirocrew token' rsync f x:/tmp/",
    "ssh -oProxyCommand='kirocrew restart' x",
    "ssh -oProxyCommand='pkill -f kirocrew' x",
    "scp -oProxyCommand='pkill -f kirocrew' f x:/tmp/",
    "sftp -oKnownHostsCommand='kirocrew restart' x",
    # A '=' inside the value must not break the whitespace-form keyword split:
    # OpenSSH ends the keyword at the first whitespace or '=', whichever is first.
    "ssh -o 'ProxyCommand env A=1 kirocrew token' x",
    "ssh '-oProxyCommand env A=1 kirocrew restart' x",
    # A whitespace-then-'=' delimiter must be consumed, not kept on the value.
    "ssh -o 'ProxyCommand =kirocrew token' x",
    "ssh -o 'ProxyCommand = kirocrew token' x",
    # The case-folded deny view turns scp's valueless -O into a second -o and
    # rsync's valueless -E into -e; the spurious one must not eat the real option.
    "scp -O -o ProxyCommand='kirocrew token' f x:/tmp/",
    "scp -O -o 'ProxyCommand kirocrew restart' f x:/tmp/",
    "scp -roProxyCommand='kirocrew token' f x:/tmp/",
    "rsync -aE -e 'kirocrew restart' f x:/tmp/",
    "rsync -avE --rsh='kirocrew token' f x:/tmp/",
    # A GIT_SSH_COMMAND= that leads a LATER statement after '&&' chaining.
    "cd . && GIT_SSH_COMMAND='kirocrew token' git ls-remote x",
    # The verb is read from attribution, which peels a subshell paren and ``.exe``.
    "(ssh -oProxyCommand='kirocrew token' x)",
    "ssh.exe -oProxyCommand='kirocrew restart' remotebox",
    "(GIT_SSH_COMMAND='kirocrew token' git fetch x)",
    # ssh keeps reading options after the destination and after a valued flag.
    "ssh host -oProxyCommand='kirocrew token'",
    "ssh -p 22 host -oProxyCommand='kirocrew token'",
    # A wrapper between the assignment and its consumer still hands it over.
    "RSYNC_RSH='kirocrew token' command rsync f x:/tmp/",
    # A single-token cluster that bundles a valueless flag before the value-taking
    # one: scp's ``-O`` folds onto ``-o`` and rsync's ``-E`` onto ``-e`` in the deny
    # view, so the walk must try every folded split (``-Oo`` / ``-aEe`` / ``-Ee``),
    # not stop at the first same-letter.
    "scp -Oo ProxyCommand='kirocrew token' f x:/tmp/",
    "scp -Oo 'ProxyCommand kirocrew restart' f x:/tmp/",
    "scp -OoProxyCommand='kirocrew token' f x:/tmp/",
    "sftp -Oo ProxyCommand='kirocrew token' x",
    "ssh -vo ProxyCommand='kirocrew restart' x",
    "rsync -Ee 'kirocrew token' f x:/tmp/",
    "rsync -aEe 'kirocrew restart' f x:/tmp/",
    "rsync -Ee'kirocrew token' f x:/tmp/",
    # A glued ``-e`` value keeps a leading ``=`` the whole token carries; popt
    # strips one, so the gate must too.
    "rsync -e='kirocrew token' f x:/tmp/",
    "rsync -ae='kirocrew restart' f x:/tmp/",
    # OpenSSH strdelim drops double quotes anywhere in the keyword, so a quoted key
    # must still match ``*Command``.
    "ssh -o '\"ProxyCommand\" kirocrew token' h",
    "ssh '-o\"ProxyCommand\"=kirocrew restart' h",
    "ssh -o 'Proxy\"Command\" kirocrew token' h",
    # readconf skips the WHOLE run of whitespace and ``=`` after the keyword, not one.
    "ssh -o 'ProxyCommand==kirocrew token' h",
    "ssh -o 'ProxyCommand=\t=kirocrew restart' h",
    # A consumer hidden behind a glued shell separator (``r&&git``, ``r;rsync``) in an
    # ``sh -c`` body still runs the assignment, so the word scan splits on separators.
    "GIT_SSH_COMMAND='kirocrew token' sh -c 'cd r&&git fetch'",
    "GIT_SSH_COMMAND='kirocrew restart' sh -c 'cd r;git fetch'",
    "RSYNC_RSH='kirocrew token' sh -c 'cd r;rsync f x:/'",
    # ``sftp -D`` connects to a LOCAL sftp-server command, run on this host.
    "sftp -D 'kirocrew token' h",
    "sftp -D'kirocrew restart' h",
]


# Legitimate commands that merely NAME an ssh-family program as an argument must
# keep their own option parsing -- a decoy word must not trigger ssh/rsync rules.
_ALLOWED_DECOY = [
    "grep -e rsync -e 'kirocrew token' notes.txt",
    "git log -S rsync -e 'kirocrew token'",
    "grep -c ssh -o 'LocalCommand kirocrew restart' notes.txt",
    # RemoteCommand runs on the far side, not here.
    "ssh -o RemoteCommand='pkill -f kirocrew' devbox",
]


# Legitimate option values that must STAY allowed: an ordinary ProxyCommand, a token
# substitution, an rsync remote-shell with a port, an ``-i`` identity, and a ``-e``
# that only belongs to a non-ssh program (grep).
_ALLOWED = [
    "ssh -oProxyCommand='ssh -W %h:%p bastion' host",
    "ssh -o ProxyCommand='nc %h %p' remotebox",
    "rsync -e 'ssh -p 2222' f x:/tmp/",
    "GIT_SSH_COMMAND='ssh -i ~/.ssh/k' git fetch origin",
    "grep -e 'kirocrew token' notes.txt",
    # Common invocations whose valueless flags share a letter with -o/-e: the
    # scan must not read their file operands as an option value.
    "rsync -aE f x:/tmp/",
    "scp -O f x:/tmp/",
    "cd . && ls",
    # The case-folded ``-E`` reads the source operand as a one-word body.
    "rsync -avE src/ x:/dst/",
    # Operands after ``--`` and ssh's remote-command words are not local options.
    "scp -- '-oProxyCommand=kirocrew token' remotebox:/tmp/",
    "ssh host ls '-oLocalCommand=kirocrew token'",
    # An assignment leading a statement that never runs its consumer is inert.
    "RSYNC_RSH='ssh localhost' true ; rsync remote-host:/x .",
    "GIT_SSH_COMMAND='kirocrew token' true ; git fetch x",
    # Benign twins of the folded-cluster rows: the same ``-Oo`` / ``-aEe`` / ``-e=``
    # shapes carrying a harmless value, or whose value-taking letter only reads a
    # file operand, must stay allowed.
    "scp -Oo BatchMode=yes f x:/tmp/",
    "rsync -aEe ssh src/ x:/dst/",
    "rsync -e=ssh f x:/tmp/",
    "sftp -Oo ConnectTimeout=5 x",
    # Benign twins of the strdelim / separator-run / consumer / sftp-D rows.
    "ssh -o '\"BatchMode\" yes' h",
    "ssh -o 'RemoteCommand==kirocrew token' h",
    "GIT_SSH_COMMAND='ssh -i k' sh -c 'cd r;true'",
    "sftp -D /usr/libexec/sftp-server h",
    "sftp user@h:/path",
]


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- same own-host pin the ssh-self suite uses, to keep the DNS daemon thread and network out of the test
def _pin_own_host_cache(monkeypatch):
    # Pin the own-host cache with the resolved-once latch set so ``_own_host_names``
    # never seeds the module globals or spawns the real resolver thread (a
    # no-test-side-effects violation), the kernel address table reads as published,
    # and unresolved names answer "not self" so the remote-host allow cases stay
    # allowed. Mirrors ``TestSandboxEscapeSshSelf._pin_own_host_cache``.
    own = security.socket.gethostname().strip().lower()
    pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
    monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
    monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
    monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
    monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
    monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)


@pytest.mark.parametrize("command", _REFUSED)
def test_ssh_command_option_body_is_refused(command):
    assert is_denied(command) is not None, command


@pytest.mark.parametrize("command", _ALLOWED + _ALLOWED_DECOY)
def test_legitimate_option_value_stays_allowed(command):
    assert is_denied(command) is None, command


class TestExtractor:
    """``ssh_command_option_payloads`` yields the local-command bodies verbatim."""

    def test_glued_and_separated_and_whitespace_spellings(self):
        assert "kirocrew token" in _payloads(["ssh", "-oproxycommand=kirocrew token", "x"])
        assert "kirocrew token" in _payloads(["ssh", "-o", "proxycommand=kirocrew token", "x"])
        assert "kirocrew token" in _payloads(["ssh", "-o", "proxycommand kirocrew token", "x"])

    def test_value_may_contain_equals_in_the_whitespace_form(self):
        # The keyword ends at the first whitespace; the '=' belongs to the value.
        assert "env a=1 kirocrew token" in _payloads(
            ["ssh", "-o", "proxycommand env a=1 kirocrew token", "x"]
        )

    def test_remotecommand_runs_on_the_far_side_so_is_not_a_local_body(self):
        assert _payloads(["ssh", "-oremotecommand=kirocrew token", "x"]) == []

    def test_rsync_remote_shell_and_leading_env_assignment(self):
        assert "kirocrew token" in _payloads(["rsync", "-e", "kirocrew token", "f", "x:/tmp/"])
        assert "kirocrew token" in _payloads(
            ["git_ssh_command=kirocrew token", "git", "fetch", "x"]
        )

    def test_verb_only_parsed_in_command_position(self):
        # ``grep -e`` is not rsync's remote-shell flag, and a ``rsync`` word that is
        # merely grep's argument must not be read as the program.
        assert _payloads(["grep", "-e", "kirocrew token", "notes.txt"]) == []
        assert _payloads(["grep", "-e", "rsync", "-e", "kirocrew token", "notes.txt"]) == []

    def test_delimiter_whitespace_and_equals_are_consumed(self):
        # OpenSSH drops the keyword delimiter; the value must not keep a leading '='.
        assert "kirocrew token" in _payloads(["ssh", "-o", "proxycommand =kirocrew token", "x"])
        assert "kirocrew token" in _payloads(["ssh", "-o", "proxycommand = kirocrew token", "x"])

    def test_spurious_o_from_case_fold_does_not_eat_the_real_option(self):
        # scp's valueless -O folds to -o in the deny view; the spurious -o must not
        # consume the following real -o spec (which starts with '-').
        assert "kirocrew token" in _payloads(
            ["scp", "-o", "-o", "proxycommand=kirocrew token", "f", "x:/tmp/"]
        )
        # rsync's valueless -E folds to -e; a trailing bare -e must not swallow the
        # following real -e option.
        assert "kirocrew token" in _payloads(
            ["rsync", "-ae", "-e", "kirocrew token", "f", "x:/tmp/"]
        )

    def test_folded_valueless_flag_before_the_value_letter_is_split_every_way(self):
        # ``scp -Oo Key=v`` folds to ``-oo``: the first ``o`` is the valueless ``-O``
        # and the second is ``-o``, so the next word is the keyword. The glued
        # ``-OoKey=v`` (``-ooKey=v``) and ``ssh -vo`` (verbose before ``-o``) resolve too.
        assert "kirocrew token" in _payloads(
            ["scp", "-oo", "proxycommand=kirocrew token", "f", "x:/tmp/"]
        )
        assert "kirocrew token" in _payloads(
            ["scp", "-ooproxycommand=kirocrew token", "f", "x:/tmp/"]
        )
        assert "kirocrew token" in _payloads(["ssh", "-vo", "proxycommand=kirocrew token", "x"])
        # A valueless flag sharing the letter only reads a file operand, so a benign
        # keyword and a plain operand stay inert.
        assert _payloads(["scp", "-oo", "batchmode=yes", "f", "x:/tmp/"]) == []

    def test_rsync_folded_E_and_glued_equals_value(self):
        # ``-Ee``/``-aEe`` fold to ``-ee``/``-aee``: the trailing ``-e`` takes the
        # next word. A glued ``-e=cmd`` keeps a leading ``=`` popt would strip.
        assert "kirocrew token" in _payloads(["rsync", "-ee", "kirocrew token", "f", "x:/tmp/"])
        assert "kirocrew token" in _payloads(["rsync", "-aee", "kirocrew token", "f", "x:/tmp/"])
        assert "kirocrew token" in _payloads(["rsync", "-e=kirocrew token", "f", "x:/tmp/"])
        assert "kirocrew token" in _payloads(["rsync", "-ae=kirocrew token", "f", "x:/tmp/"])
        # The same shapes with a benign remote shell carry only harmless words
        # (``ssh``, and the spurious one-letter split of the folded ``-E``); none
        # names a refused command, so the row stays allowed.
        assert "ssh" in _payloads(["rsync", "-aee", "ssh", "src/", "x:/dst/"])
        assert _payloads(["rsync", "-e=ssh", "f", "x:/tmp/"]) == ["ssh"]

    def test_double_quotes_in_the_keyword_are_dropped(self):
        # OpenSSH strdelim drops double quotes anywhere in the keyword token.
        assert "kirocrew token" in _payloads(["ssh", "-o", '"proxycommand" kirocrew token', "h"])
        assert "kirocrew token" in _payloads(["ssh", "-o", 'proxy"command" kirocrew token', "h"])
        assert "kirocrew token" in _payloads(["ssh", '-o"proxycommand"=kirocrew token', "h"])

    def test_whole_separator_run_after_the_keyword_is_skipped(self):
        # readconf skips the full run of whitespace and ``=``, not a single ``=``.
        assert "kirocrew token" in _payloads(["ssh", "-o", "proxycommand==kirocrew token", "h"])
        assert "kirocrew token" in _payloads(["ssh", "-o", "proxycommand=\t=kirocrew token", "h"])

    def test_consumer_hidden_behind_a_glued_separator_is_found(self):
        # ``sh -c 'cd r&&git fetch'`` reaches here as one token ``cd r&&git fetch``; the
        # consumer scan splits on shell separators so the ``git`` is not hidden in ``r&&git``.
        assert "kirocrew token" in _payloads(
            ["git_ssh_command=kirocrew token", "sh", "-c", "cd r&&git fetch"]
        )
        assert "kirocrew token" in _payloads(
            ["rsync_rsh=kirocrew token", "sh", "-c", "cd r;rsync f x:/"]
        )

    def test_sftp_dash_D_runs_a_local_server_command(self):
        # ``sftp -D <prog>`` runs the program locally; glued and next-word both read.
        assert "kirocrew token" in _payloads(["sftp", "-d", "kirocrew token", "h"])
        assert "kirocrew token" in _payloads(["sftp", "-dkirocrew token", "h"])
        # A real server path and a non-sftp verb carrying ``-d`` stay inert.
        assert _payloads(["ssh", "-d", "kirocrew token", "h"]) == []

    def test_leading_assignment_of_a_later_statement_is_inspected(self):
        toks = ["cd", ".", "&&", "git_ssh_command=kirocrew token", "git", "ls-remote", "x"]
        assert "kirocrew token" in _payloads(toks)

    def test_repeated_verb_scan_is_linear_and_not_duplicated(self):
        # A line of repeated ssh words must be scanned once (not restarted at each
        # verb token), so neither the payload list nor the time is quadratic.
        assert _payloads(["ssh"] * 4000) == []
        out = _payloads(["ssh", "-oproxycommand=a b", ";"] * 500)
        assert len(out) == 500
        assert len(_payloads(["rsync_rsh=a b"] * 3000 + ["rsync"])) == 3000

    def test_options_end_at_double_dash_and_at_the_ssh_remote_command(self):
        # One argv: the second ``ssh`` word is the remote command, run far away.
        assert _payloads(["ssh", "-oproxycommand=a b"] * 3) == ["a b", "a b"]
        assert _payloads(["scp", "--", "-oproxycommand=a b", "x:/tmp/"]) == []
        assert _payloads(["ssh", "-p", "22", "h", "-oproxycommand=a b"]) == ["a b"]
        assert _payloads(["ssh", "--", "h", "-oproxycommand=a b"]) == []

    def test_attributed_verb_and_subshell_assignment(self):
        assert _payloads(["(ssh", "-oproxycommand=a b", "x)"]) == ["a b"]
        assert _payloads(["ssh.exe", "-oproxycommand=a b", "x"]) == ["a b"]
        assert _payloads(["(git_ssh_command=a b", "git", "fetch)"]) == ["a b"]

    def test_assignment_counts_only_for_a_statement_running_its_consumer(self):
        assert _payloads(["rsync_rsh=a b", "true", ";", "rsync", "x:/", "."]) == []
        assert _payloads(["git_ssh_command=a b", "sh", "-c", "git fetch"]) == ["a b"]
