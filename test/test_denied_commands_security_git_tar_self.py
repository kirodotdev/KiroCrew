"""Tests for the git/tar self-target floor (``sandbox-escape-git-tar-self``).

``git`` and ``tar`` start an ssh transport (or run an arbitrary command) on their own,
without naming any ssh-family verb the ssh-self floor reads, so they are a second route
back into this host outside the sandbox.  ``TestSandboxEscapeGitTarSelf`` pins the rule on
both tiers and how ``_is_git_tar_to_self`` reads a transport through env/wrapper
unwrapping, git global options, quoting, ``user@`` prefixes, URI schemes, globs and
nested shell payloads, and that it leaves normal local git/tar use alone.  The own-host
set a destination is compared against is pinned here exactly as in
``test_denied_commands_security_ssh_self.py``.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _denied_by, _rule_pattern

from kiro_crew import security
from kiro_crew.security import BUILTIN_DENIED_RULES
from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import compute_effective_denied, is_denied, is_safe_user_regex

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestSandboxEscapeGitTarSelf:
    """A git/tar command dialing THIS host is refused; local use is not."""

    _RULE = "sandbox-escape-git-tar-self"
    #: A command a git or tar transport runs is re-entered through every argv
    #: floor, and the refusal names the row that refused it: a carried ``ssh``
    #: that dials this host is reported under the ssh floor's own row.
    _SSH_RULE = "sandbox-escape-ssh-self"
    _DIAL_RULES = frozenset({_RULE, _SSH_RULE})

    @staticmethod
    def _effective():
        return list(compute_effective_denied(BUILTIN_DENIED_RULES, (), False, (), ()))

    @pytest.fixture(autouse=True)
    def _pin_own_host_cache(self, request, _floor_monkeypatch):
        # Same pin as the ssh-self suite: a fixed own-host cache with the
        # resolved-once latch set, so no command here spawns the real own-host
        # resolver thread or blocks on DNS, and the DNS-alias verdict layer is
        # stubbed to "not self" so remote-host allow cases stay allowed.
        own_mp = pytest.MonkeyPatch()
        request.addfinalizer(own_mp.undo)
        own = security.socket.gethostname().strip().lower()
        pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
        _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
        _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
        _floor_monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
        own_mp.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
        _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)

    def test_rule_is_registered_on_both_tiers(self):
        assert self._RULE in {r.id for r in BUILTIN_DENIED_RULES}
        assert self._RULE in security._SELF_PROTECTION_FLOOR_RULE_IDS
        assert self._RULE in security._SELF_PROTECTION_FLOOR_NOTES
        assert _rule_pattern(self._RULE) in self._effective()
        # A pattern that fails the safety lint is silently disabled, which is
        # how the first draft of the ssh-self rule failed.
        assert is_safe_user_regex(_rule_pattern(self._RULE))

    # Each form from the mapping in the PR: a git or tar transport that reaches
    # this host, or that runs a command dialing this host in place of the transport.
    _SELF_DENIED = [
        # git scp-like host:path, literal self host (``0`` is 0.0.0.0, this host)
        "git clone localhost:x",
        "git clone 0:x",
        "git clone localhost:/srv/repo.git",
        "git clone 127.0.0.1:x",
        "git ls-remote [::1]:x",
        # user@ prefix on an scp-like self host
        "git clone user@localhost:x",
        "git fetch me@127.0.0.1:x",
        "git clone user@[::1]:x",
        # git ssh-family URL schemes
        "git fetch ssh://127.0.0.1/x",
        "git fetch ssh://me@localhost:22/x",
        "git clone git+ssh://localhost/x",
        "git clone ssh+git://localhost/x",
        "git clone rsync://localhost/x",
        # git -C prefix does not shed the floor
        "git -C /tmp clone localhost:x",
        # git submodule / remote carry the url after their sub-subcommand
        "git submodule add localhost:x",
        "git remote add o localhost:x",
        "git remote set-url origin localhost:x",
        # every post-verb token is checked, even where option grammar would
        # otherwise consume or attach the remote
        "git fetch -u localhost:x",
        "git fetch -u=localhost:x",
        "git fetch -ulocalhost:x",
        # a remote after a detached option value is still read
        "git clone -b main localhost:x",
        "git fetch --depth 1 localhost:x",
        # wrappers keep the real verb behind them
        "sudo git clone localhost:x",
        "timeout 5 git clone localhost:x",
        # nested shell payload
        "sh -c 'git clone localhost:x'",
        # tar host:file, literal/old-style, and --rsh-command
        "tar -cf localhost:out.tar dir",
        "tar -xf localhost:/tmp/a.tar",
        "tar --file=localhost:/tmp/a.tar -x",
        "tar -f localhost:a.tar -c dir",
        "tar cf localhost:a.tar dir",
        "tar xf localhost:a.tar",
        # an old-style bundle's value letters take the next words in order, and
        # a case-folded ``c`` may be ``C``; ``--`` ends option parsing, so a
        # later member cannot enable force-local
        "tar cbf 20 localhost:a.tar dir --rsh-command=ssh",
        "tar cCf /tmp localhost:a.tar dir --rsh-command=ssh",
        "tar Crf /tmp localhost:a.tar dir",
        "tar Cf /tmp localhost:a.tar --create",
        "TAR_OPTIONS=--create tar Cf /tmp localhost:a.tar",
        "tar -cf -- -f localhost:a.tar",
        "tar --rsh-command=ssh -cf localhost:a.tar -- --force-local",
        "tar -cf me@localhost:a.tar dir",
        "nohup tar -cf localhost:a.tar dir",
        # a wrapper's value option is stepped over (argv_rules' wrapper reader)
        "sudo -u root git clone localhost:x",
        "env -u PATH git clone localhost:x",
        # git archive --remote is the remote it dials
        "git archive --remote=localhost:x HEAD",
        "git archive --remote localhost:x HEAD",
    ]

    @pytest.mark.parametrize("cmd", _SELF_DENIED)
    def test_self_targets_are_denied(self, cmd):
        assert _denied_by(cmd) == self._RULE, cmd

    # A command git or tar runs in place of the transport, when it is an ssh
    # that dials this host: refused by the ssh floor it is re-entered through.
    _SELF_DENIED_THROUGH_SSH = [
        # git ext:: whose command dials this host
        "git clone ext::ssh localhost id",
        # core.sshCommand / GIT_SSH* overrides whose command dials this host
        "git -c core.sshCommand='ssh localhost' clone far:x",
        "git clone -c core.sshCommand='ssh localhost' far:x",
        "GIT_SSH_COMMAND='ssh localhost' git fetch origin",
        "env GIT_SSH_COMMAND='ssh 127.0.0.1' git fetch origin",
        # the override VALUE dials self via ::1 or a glob host, while the
        # remote itself is not self -- so the deny comes from the recursed value
        "git -c core.sshCommand='ssh ::1' clone far:x",
        # a shell runs the override value, so its glob host still expands
        "GIT_SSH_COMMAND='ssh local*' git fetch origin",
        # a ProxyJump / -J hop that is a literal self host dials this host
        "GIT_SSH_COMMAND='ssh -o ProxyJump=localhost' git fetch origin",
        "GIT_SSH_COMMAND='ssh -J127.0.0.1' git fetch origin",
        "git -c core.sshCommand='ssh -4vJlocalhost' fetch origin",
        "git clone ext::ssh 127.0.0.1 id",
        # --upload-pack / --receive-pack program that dials this host
        'git clone --upload-pack="ssh localhost id" /local/repo',
        "git clone --upload-pack='ssh localhost id' /local/repo",
        "git fetch -u 'ssh localhost id' origin",
        # tar --rsh-command
        "tar --rsh-command='ssh localhost' -cf far:a.tar dir",
        "tar --rsh-command 'ssh localhost' -cf far:a.tar dir",
        # glued --config-env reads the named variable, globally or below clone
        "X='ssh localhost' git --config-env=core.sshCommand=X clone far:x",
        "V='ssh localhost' git clone --config-env=core.sshCommand=V far:x",
        # git archive --exec runs the program that dials
        "git archive --remote=far:x --exec='ssh localhost id' HEAD",
    ]

    @pytest.mark.parametrize("cmd", _SELF_DENIED_THROUGH_SSH)
    def test_carried_ssh_self_dials_are_denied_by_the_ssh_floor(self, cmd):
        assert _denied_by(cmd) == self._SSH_RULE, cmd

    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            ("git clone -u'ssh localhost echo x=y #' . out", _SSH_RULE),
            ("git clone -ccore.sshCommand='ssh localhost id #' far:x out", _SSH_RULE),
            ("tar --rsh-command=ssh -cf localhost:a.tar --exclude --force-local dir", _RULE),
            ("tar -cf localhost:a.tar --force-local", _RULE),
        ],
        ids=[
            "short-upload-pack-command",
            "short-config-command",
            "tar-detached-option-collision",
            "tar-force-local-after-archive",
        ],
    )
    def test_token_agnostic_candidates_are_denied(self, cmd, rule):
        assert _denied_by(cmd) == rule, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "tar -cf x.tar --checkpoint=1 --checkpoint-action=exec='ssh localhost id' dir",
            "tar -cf x.tar --checkpoint=1 --checkpoint-action=exec='ssh 127.0.0.1 id' dir",
            "tar -cf x.tar --checkpoint=1 --checkpoint-action exec='ssh localhost id' dir",
        ],
        ids=["attached-localhost", "attached-loopback-ip", "detached-localhost"],
    )
    def test_tar_checkpoint_exec_self_dial_is_denied(self, cmd):
        # tar runs the checkpoint ``exec=`` command; both spellings read alike.
        assert _denied_by(cmd) == self._SSH_RULE, cmd

    def test_tar_checkpoint_exec_remote_dial_is_allowed(self):
        cmd = "tar -cf x.tar --checkpoint=1 --checkpoint-action=exec='ssh far.example.com id' dir"
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "git fetch origin localhost:localhost",
            "git clone https://github.com/kirodotdev/KiroCrew.git localhost:dir",
            "git fetch origin 0:0",
        ],
        ids=[
            "git-self-shaped-refspec",
            "git-self-shaped-clone-destination",
            "git-zero-host-refspec",
        ],
    )
    def test_maintainer_accepted_operand_overrefusals_are_denied(self, cmd):
        # Maintainer-accepted F3 price of checking every transport operand.
        assert _denied_by(cmd) == self._RULE, cmd

    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            ("git request-pull HEAD localhost:x", _RULE),
            ("GIT_SSH_COMMAND='ssh localhost' git maintenance run --task=prefetch", _SSH_RULE),
            ("GIT_SSH_COMMAND='ssh localhost id' git some-unknown-verb origin", _SSH_RULE),
            # git ignores an alias that would hide a command it ships
            ("git -c alias.request-pull=status request-pull HEAD localhost:x", _RULE),
        ],
        ids=["request-pull", "maintenance-prefetch", "unknown-verb", "shipped-verb-alias"],
    )
    def test_verbs_not_known_local_are_read_in_full(self, cmd, rule):
        # Only a known local-only verb skips the read, so a missing verb fails closed.
        assert _denied_by(cmd) == rule, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "git status",
            "git log",
            "git commit -m x",
            "git diff",
            "git add .",
            "git request-pull HEAD git@github.com:o/r",
            "git fetch github.com:o/r",
            "GIT_SSH_COMMAND='ssh far.example.com' git maintenance run --task=prefetch",
            "GIT_SSH_COMMAND='ssh localhost' git status",
        ],
    )
    def test_full_read_still_allows_local_work_and_other_hosts(self, cmd):
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "tar -cf out.tar localhost:file",
            "tar -cf out.tar 127.0.0.1:backup",
            'tar -czf logs.tgz "logs/app 2026-11-11 10:00:00.log" 0:00-rotation.log',
            "tar cf out.tar localhost:file dir",
            "tar -cfC localhost:x 0:0 d",
            "tar --exclude -- localhost:x -c d",
            # old-style ``v`` (verbose) claims no word, so the member is not the archive
            "tar cvf out.tar localhost:file",
            "tar czvf out.tar localhost:file",
            "tar cvf out.tar 127.0.0.1:x dir",
            # ``b`` (blocking factor) claims its value word; the member after the
            # archive is still a member
            "tar cbf 20 out.tar localhost:file",
        ],
        ids=[
            "self-shaped-member",
            "loopback-member",
            "clock-time-members",
            "old-style-member",
            "glued-archive-then-member",
            "member-after-option-value",
            "verbose-member",
            "verbose-gzip-member",
            "verbose-loopback-member",
            "blocking-factor-member",
        ],
    )
    def test_tar_member_operands_are_not_archives(self, cmd):
        # Only the -f/--file value or an old-style ``f`` slot is the archive;
        # every other operand is a file to add, which tar never dials.
        assert not security._is_git_tar_to_self(cmd.lower()), cmd
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "tar -cf localhost:out.tar dir",
            "tar -c -f localhost:out.tar dir",
            "tar --file=localhost:out.tar dir",
            "tar --file localhost:out.tar dir",
            "tar -flocalhost:out.tar dir",
            "tar cf localhost:out.tar dir",
            "tar cfz localhost:out.tar dir",
            "tar cvf localhost:out.tar dir",
            "tar czf localhost:out.tar dir",
            "tar cbf 20 localhost:out.tar dir",
            "tar -cf me@localhost:a.tar dir",
        ],
        ids=[
            "bundled",
            "detached",
            "long-attached",
            "long-detached",
            "glued",
            "old-style",
            "old-style-letter-after-f",
            "old-style-verbose",
            "old-style-gzip",
            "old-style-blocking-factor-value",
            "user-at-self",
        ],
    )
    def test_tar_self_archive_operand_is_denied(self, cmd):
        assert _denied_by(cmd) == self._RULE, cmd

    # tar's short options are case-sensitive: uppercase L/F/H/I/N/V/K take a
    # value and their lowercase forms do not (B/G: the other way round), so an
    # old-style bundle's ``f`` slot is read from the command as submitted.
    @pytest.mark.parametrize(
        "cmd",
        [
            "tar cLf 1024 localhost:a.tar /dev/null",
            "tar cLf 1024 localhost:a.tar /dev/null --rsh-command=ssh",
            "tar cFf script.sh localhost:a.tar dir",
            "tar cHf posix localhost:a.tar dir",
            "tar cIf zstd localhost:a.tar dir",
            "tar cNf 2026-01-01 localhost:a.tar dir",
            "tar cVf mylabel localhost:a.tar dir",
            "tar xKf member localhost:a.tar",
            "tar cBf localhost:a.tar dir",
            "sh -c 'tar cLf 1024 localhost:a.tar d'",
            'tar c""Lf 1024 localhost:a.tar d',
            'echo clf; tar c""Lf 1024 localhost:a.tar d',
            # A letter the shell builds is read both ways (fail closed).
            "u=L; tar c${u}f 1024 localhost:a.tar d",
            "tar c$(printf L)f 1024 localhost:a.tar d",
        ],
        ids=[
            "tape-length-L",
            "tape-length-L-rsh-command",
            "info-script-F",
            "format-H",
            "compress-program-I",
            "newer-than-N",
            "label-V",
            "starting-file-K",
            "read-full-records-B-takes-no-value",
            "nested-sh-c",
            "quote-spliced-letter",
            "quote-spliced-letter-beside-lowercase-twin",
            "assigned-letter",
            "substituted-letter",
        ],
    )
    def test_tar_uppercase_bundle_letter_hiding_self_archive_is_denied(self, cmd):
        assert _denied_by(cmd) == self._RULE, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "tar cvf out.tar dir",
            "tar clf out.tar dir",
            "tar chf out.tar dir",
            "tar cvf out.tar localhost:file",
            "tar czvf out.tar localhost:file",
            "tar clf out.tar localhost:file",
            "tar cLf 1024 out.tar dir",
            "tar cLf 1024 out.tar localhost:file",
            "tar cVf mylabel out.tar localhost:file",
            "tar cLf 1024 far.example.com:a.tar dir",
        ],
        ids=[
            "verbose",
            "check-links",
            "dereference",
            "verbose-self-shaped-member",
            "verbose-gzip-self-shaped-member",
            "check-links-self-shaped-member",
            "tape-length-local-archive",
            "tape-length-self-shaped-member",
            "label-self-shaped-member",
            "tape-length-remote-archive",
        ],
    )
    def test_tar_bundle_letter_case_keeps_non_self_archives_allowed(self, cmd):
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    def test_tar_bundle_is_read_in_the_submitted_case(self):
        # The gate hands the floor the lowered command and the command as
        # submitted; only the latter tells ``L`` from ``l``.
        lowered = "tar clf 1024 localhost:a.tar d"
        assert not security._is_git_tar_to_self(lowered)
        assert security._is_git_tar_to_self(lowered, raw_text="tar cLf 1024 localhost:a.tar d")

    def test_push_zero_host_refspec_is_refused(self):
        # The same F3 price on a push: this floor reads the ``0`` host as self,
        # and the git-publish floor (which claims a push first) refuses it too.
        cmd = "git push origin 0:main"
        assert security._is_git_tar_to_self(cmd), cmd
        assert is_denied(cmd, denied_regexes=self._effective()) is not None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "git 2>/dev/null clone localhost:x",
            "git >/tmp/o fetch localhost:x",
            "git -C . 2>/dev/null clone localhost:x",
            "git > /tmp/o fetch localhost:x",
        ],
        ids=["fd-redirect", "stdout-redirect", "redirect-after-global-option", "detached-redirect"],
    )
    def test_redirect_before_verb_is_denied(self, cmd):
        # A redirection is not git's verb: bash removes it before exec.
        assert _denied_by(cmd) == self._RULE, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "2>/dev/null git clone localhost:x",
            "env 2>/dev/null git clone localhost:x",
            "sh -c '2>/dev/null git clone ssh://127.0.0.1/r'",
            "2>/dev/null tar -cf localhost:o d",
            ">out git clone localhost:x",
            ">>out git clone localhost:x",
            "2> /dev/null git clone localhost:x",
        ],
        ids=[
            "fd-attached",
            "wrapper-fd-attached",
            "nested-fd-attached",
            "tar-fd-attached",
            "stdout-attached",
            "stdout-append-attached",
            "fd-detached",
        ],
    )
    def test_redirect_before_program_is_denied(self, cmd):
        assert _denied_by(cmd) == self._RULE, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "2>/dev/null git clone https://github.com/x/y",
            "'>out' git clone localhost:x",
        ],
        ids=["remote-target", "quoted-redirect-program"],
    )
    def test_redirect_before_non_self_program_is_allowed(self, cmd):
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            ("git clone -b ';' localhost:x", _RULE),
            ("git clone -b '&&' localhost:x", _RULE),
            ("git clone -b '|' localhost:x", _RULE),
            ("git archive --prefix ';' --remote=localhost:x HEAD", _RULE),
            # tar reads its --rsh-command before the archive: the ssh floor refuses it
            (
                "tar --exclude ';' -cf localhost:out.tar dir --rsh-command 'ssh localhost'",
                _SSH_RULE,
            ),
        ],
        ids=["semicolon", "and-and", "pipe", "archive-prefix", "tar-exclude"],
    )
    def test_quoted_separator_does_not_end_the_scan(self, cmd, rule):
        # A quoted separator is data, not a command boundary.
        assert _denied_by(cmd) == rule, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "git -c url.localhost:.insteadOf=x: clone x:repo",
            "git -c url.ssh://localhost/.insteadOf=https://example.com/ ls-remote"
            " https://example.com/repo",
            "git -c url.https://example.com/.insteadOf=localhost: fetch https://example.com/repo",
            "X=x: git --config-env=url.localhost:.insteadOf=X clone x:repo",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=url.localhost:.insteadOf GIT_CONFIG_VALUE_0=x:"
            " git clone x:repo",
        ],
        ids=[
            "c-base-self",
            "c-base-self-url",
            "c-value-self",
            "config-env-base-self",
            "env-numbered",
        ],
    )
    def test_url_rewrite_to_self_is_denied(self, cmd):
        # git rewrites the remote through url.<base>.insteadOf before dialing.
        assert _denied_by(cmd) == self._RULE, cmd

    def test_url_rewrite_not_to_self_is_allowed(self):
        cmd = "git -c url.https://github.com/.insteadOf=gh: clone gh:o/r"
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    # The text is read through the ssh floor's own source pre-walk, so a static
    # ``$(printf …)`` substitution is its output before git/tar is read.
    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            ('git ls-remote "$(printf localhost):repo"', _RULE),
            ("git ls-remote local$(printf host):repo", _RULE),
            ("$(printf git) ls-remote localhost:repo", _RULE),
            ('tar --rsh-command=ssh -cf "$(printf localhost):a.tar" dir', _RULE),
            ('git -c core.sshCommand="ssh $(printf localhost)" ls-remote far:x', _SSH_RULE),
        ],
        ids=["quoted-host", "glued-host", "program-word", "tar-archive", "config-value"],
    )
    def test_static_substitution_to_self_is_denied(self, cmd, rule):
        assert _denied_by(cmd) == rule, cmd

    @pytest.mark.parametrize(
        ("substituted", "literal"),
        [
            ('git ls-remote "$(printf localhost):repo"', "git ls-remote localhost:repo"),
            ("$(printf git) ls-remote localhost:repo", "git ls-remote localhost:repo"),
            # A bare word is a local path to git with or without the substitution.
            ('git ls-remote "$(printf localhost)repo"', "git ls-remote localhostrepo"),
            ("$(printf git) ls-remote localhostrepo", "git ls-remote localhostrepo"),
            ("git ls-remote $(printf github.com):x/y", "git ls-remote github.com:x/y"),
        ],
    )
    def test_static_substitution_reads_like_its_output(self, substituted, literal):
        assert security._is_git_tar_to_self(substituted) == security._is_git_tar_to_self(literal)

    # The environment a git command runs with is the ssh floor's
    # ``_FrameEnvironment``: an exported value persists past ``;`` and a
    # frame's leading assignments reach its nested ``sh -c`` payload.
    @pytest.mark.parametrize(
        "cmd",
        [
            "export GIT_SSH_COMMAND='ssh localhost id #'; git ls-remote far:x",
            "GIT_SSH_COMMAND='ssh localhost id #' sh -c 'git ls-remote far:x'",
            "GIT_SSH_COMMAND='ssh localhost id #'; export GIT_SSH_COMMAND; git ls-remote far:x",
            "export GIT_SSH='ssh localhost id #' && git fetch origin",
            "export GIT_CONFIG_KEY_0=core.sshCommand; export GIT_CONFIG_VALUE_0='ssh localhost id #';"
            " git ls-remote far:x",
            "export GIT_CONFIG_KEY_0=core.sshCommand; export GIT_CONFIG_VALUE_0='ssh localhost id #';"
            " sh -c 'git ls-remote far:x'",
            "export X='ssh localhost'; git --config-env=core.sshCommand=X ls-remote far:x",
        ],
        ids=[
            "export-ssh-command",
            "prefix-into-nested-sh-c",
            "assign-then-export",
            "export-git-ssh",
            "export-numbered-config",
            "export-numbered-config-nested",
            "export-config-env",
        ],
    )
    def test_carried_environment_to_self_is_denied(self, cmd):
        assert _denied_by(cmd) == self._SSH_RULE, cmd

    def test_nested_payload_inherits_parent_launch_environment(self):
        # Each payload keeps the environment of the command that launches it;
        # sibling commands cannot replace that snapshot in either direction.
        self_target = "GIT_SSH_COMMAND='ssh localhost id #' sh -c 'git ls-remote far:x'"
        assert _denied_by(self_target) == self._SSH_RULE
        assert (
            _denied_by(
                self_target + "; GIT_SSH_COMMAND='ssh far.example.com' sh -c 'git ls-remote far:x'"
            )
            == self._SSH_RULE
        )
        assert (
            _denied_by(
                "sh -c 'git ls-remote far:x'; " "export GIT_SSH_COMMAND='ssh localhost id #'"
            )
            not in self._DIAL_RULES
        )

    def test_payload_walk_failure_degrades_to_top_level(self, monkeypatch):
        def raise_on_payloads(*_args, **_kwargs):
            raise RuntimeError("payload walk exploded")

        monkeypatch.setattr(security, "_nested_shell_payloads", raise_on_payloads)
        assert security._is_git_tar_to_self("git clone localhost:x")
        assert not security._is_git_tar_to_self("git clone https://github.com/x/y")

    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            ("git -c remote.selftarget.url=localhost:x ls-remote selftarget", _RULE),
            (
                "TARGET=localhost:x git --config-env=remote.selftarget.url=TARGET "
                "ls-remote selftarget",
                _RULE,
            ),
            (
                "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.selftarget.url "
                "GIT_CONFIG_VALUE_0=localhost:x git ls-remote selftarget",
                _RULE,
            ),
            ("git -c arbitrary.key='ssh localhost' ls-remote far:x", _SSH_RULE),
        ],
        ids=["config", "config-env", "numbered-env", "command-value"],
    )
    def test_every_invocation_local_config_value_to_self_is_denied(self, cmd, rule):
        assert _denied_by(cmd) == rule, cmd

    def test_invocation_local_config_values_to_remote_hosts_are_allowed(self):
        for cmd in (
            "git -c remote.selftarget.url=github.com:x ls-remote selftarget",
            "git -c arbitrary.key='ssh far.example.com' ls-remote far:x",
        ):
            assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    def test_multi_name_export_applies_git_ssh_command(self):
        cmd = "export A=1 GIT_SSH_COMMAND='ssh localhost id #'; git ls-remote far:x"
        assert _denied_by(cmd) == self._SSH_RULE

    @pytest.mark.parametrize(
        "cmd",
        [
            # a leading assignment ends with its simple command
            "GIT_SSH_COMMAND='ssh localhost id #' true; git ls-remote far:x",
            # an export after the git command does not reach it
            "git ls-remote far:x; export GIT_SSH_COMMAND='ssh localhost id #'",
            # a local verb runs no transport
            "export GIT_SSH_COMMAND='ssh localhost id #'; git status",
            # an exported command that does not dial this host
            "export GIT_SSH_COMMAND='ssh -i key'; git ls-remote far:x",
            "GIT_SSH_COMMAND='ssh -i key' sh -c 'git ls-remote far:x'",
        ],
        ids=["cleared-by-separator", "export-after", "local-verb", "export-far", "nested-far"],
    )
    def test_carried_environment_not_to_self_is_allowed(self, cmd):
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            # The floor reads lower-cased text, so a benign lower-case variable
            # folds onto the upper-case one git or tar honors: every value kept
            # under the folded name is read, in either order.
            (
                "GIT_SSH_COMMAND='ssh localhost id #' git_ssh_command=ssh git ls-remote far:x",
                _SSH_RULE,
            ),
            (
                "git_ssh_command=ssh GIT_SSH_COMMAND='ssh localhost id #' git ls-remote far:x",
                _SSH_RULE,
            ),
            (
                "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.selftarget.url git_config_key_0=x "
                "git_config_value_0=y GIT_CONFIG_VALUE_0=localhost:x git ls-remote selftarget",
                _RULE,
            ),
            (
                "X='ssh localhost' x=ssh git --config-env=core.sshCommand=X ls-remote far:x",
                _SSH_RULE,
            ),
            (
                "x=ssh X='ssh localhost' git --config-env=core.sshCommand=X ls-remote far:x",
                _SSH_RULE,
            ),
            ("TAR_OPTIONS='-f localhost:o' tar_options=-v tar -c dir", _RULE),
            ("tar_options=-v TAR_OPTIONS='-f localhost:o' tar -c dir", _RULE),
            ("TAPE=localhost:x tape=/dev/st0 tar -c dir", _RULE),
        ],
        ids=[
            "git-ssh-command-upper-first",
            "git-ssh-command-lower-first",
            "numbered-config-pair",
            "config-env-upper-first",
            "config-env-lower-first",
            "tar-options-upper-first",
            "tar-options-lower-first",
            "tape",
        ],
    )
    def test_case_folded_env_names_keep_every_value(self, cmd, rule):
        assert _denied_by(cmd) == rule, cmd

    # git push to this host is a self dial too, but the git-publish floor claims
    # a push first; assert only that it is refused, not by which rule.
    @pytest.mark.parametrize("cmd", ["git push me@localhost:x", "git push localhost:x main"])
    def test_push_to_self_is_refused(self, cmd):
        assert is_denied(cmd, denied_regexes=self._effective()) is not None, cmd

    # The benign set the fix must not touch, including the 14 file-operand
    # commands the scope review pinned (``git add s* *.*`` and friends).
    _ALLOWED = [
        "git add s* *.*",
        "git add .",
        'git commit -m "fix localhost:8080 bug"',
        'git commit -m "see ssh://localhost/x please"',
        "git status",
        "git -c core.sshCommand='ssh localhost' status",
        "GIT_SSH_COMMAND='ssh localhost' git status",
        "git -c core.sshCommand='ssh localhost' archive HEAD -- --remote",
        "git log",
        "git log --grep=localhost:x",
        "git diff",
        "git clone /local/path",
        "git clone /tmp/mirror.git dest",
        "git clone file:///local/repo",
        "git clone https://github.com/kirodotdev/KiroCrew",
        "git clone https://github.com/kirodotdev/KiroCrew localhost-dir",
        "git clone https://localhost/x",
        "git clone https://localhost:8080/x",
        # http(s) with userinfo to this host is not an ssh transport
        "git clone http://dev@localhost:3000/org/app.git",
        "git fetch https://ci@127.0.0.1:8443/org/app.git main",
        "git.exe clone http://dev@localhost:3000/org/app.git",
        "git clone ssh://github.com/x/y.git",
        "git clone git@github.com:kirodotdev/KiroCrew.git",
        # dotted remote hosts: read as literal-self only, never DNS-pending.
        # clone, fetch AND push to github.com / gitlab.com all stay allowed.
        "git clone ssh://git@github.com/x/y.git",
        "git fetch github.com:x/y main",
        "git push git@github.com:kirodotdev/KiroCrew.git main",
        "GIT_SSH_COMMAND='ssh -i key' git push git@github.com:x",
        "git clone git@gitlab.com:group/proj.git",
        "git fetch git@gitlab.com:g/p.git main",
        "git push gitlab.com:g/p main",
        "tar -cf backup.example.com:/archive.tar dir",
        "git fetch origin",
        "git fetch upstream main",
        "git fetch https://github.com/kirodotdev/KiroCrew main",
        "git push origin my-feature-branch",
        "git fetch --upload-pack=git-upload-pack origin",
        # an option value is not a remote (bare words never dial)
        "git clone -b release-1.0 https://github.com/x/y",
        "git clone --branch v1.2.3 https://github.com/x/y",
        "git fetch -j 0 origin",
        "git clone -o myremote https://x/y",
        "git fetch origin main:main",
        "git push origin HEAD:main",
        "git clone localhost",
        # a command run in place of the transport that does not dial this host
        # runs in the agent's own sandbox, so it is not an escape
        'GIT_SSH_COMMAND="ssh" git clone far:x',
        'git -c core.sshCommand="ssh -o x" clone far:x',
        "GIT_SSH=/tmp/x git clone far:x",
        "env GIT_SSH_COMMAND=x git fetch origin",
        "tar --rsh-command=id -cf a.tar dir",
        "tar --rsh-command=/usr/bin/ssh -cf far:a.tar dir",
        "git clone --upload-pack=x file:///local/repo",
        'git clone -u "sh -c id" /local/repo',
        'git clone "ext::sh -c id"',
        "git -c protocol.ext.allow=always clone ext::id",
        # rsync-over-ssh to a remote host is the ssh floor's domain, not this
        # one; pin that the git/tar floor does not interfere with it.
        "rsync -e ssh file user@otherhost:/tmp/",
        "git -C /tmp status",
        "git config remote.origin.url localhost:x",
        "git remote -v",
        "git remote set-url --delete origin localhost:x",
        "GIT_SSH_COMMAND='ssh localhost' git remote show -n origin",
        "git remote get-url localhost:x",
        "git remote remove localhost:x",
        "git remote rename localhost:x elsewhere",
        "git remote add o https://localhost/x",
        "git submodule update --init",
        "git stash",
        "tar -czf x.tgz dir",
        "tar -czf out.tar dir",
        "tar -xf x.tar",
        "tar -czf handoff.tgz s* *.*",
        "tar -xzf x.tgz -C /tmp",
        "tar -cf /tmp/out.tar localhost",
        "tar -czf out.tgz ./localhost",
        "tar --exclude=localhost -czf out.tgz dir",
        "dir s* *.*",
        "git archive --remote=github.com:x/y HEAD",
        "git archive -o out.tar HEAD",
        "sudo -u root git status",
    ]

    @pytest.mark.parametrize("cmd", _ALLOWED)
    def test_benign_commands_stay_allowed(self, cmd):
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    # The verb spelled through quoting or a glob is the program the shell runs,
    # exactly as the ssh floor reads ``ss''h``/``s*h``: the raw text carries no
    # literal ``git``/``tar`` (or not as the program word), so the quick gate
    # and the verb match must both see through the spelling.
    _SPELLED_VERB_SELF = [
        "gi''t fetch -q localhost:x",
        '"git" clone localhost:x',
        "g*t clone localhost:x",
        "/usr/bin/g?t clone localhost:x",
        "t''ar -cf localhost:x d",
    ]

    @pytest.mark.parametrize("cmd", _SPELLED_VERB_SELF)
    def test_quoted_or_globbed_verb_to_self_is_denied(self, cmd):
        assert security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) == self._RULE, cmd

    # The same spellings against a remote that is not this host stay allowed.
    _SPELLED_VERB_REMOTE = [
        "gi''t fetch -q git@github.com:x/y",
        '"git" clone https://github.com/x/y',
        "g*t clone git@github.com:x/y",
        "/usr/bin/g?t clone git@github.com:x/y",
        "t''ar -cf github.com:x d",
    ]

    @pytest.mark.parametrize("cmd", _SPELLED_VERB_REMOTE)
    def test_quoted_or_globbed_verb_to_remote_is_allowed(self, cmd):
        assert not security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) is None, cmd

    # The DNS-alias layer answering "self" (its fail-closed PENDING answer on a
    # first-seen dotted name) must not refuse a dotted git/tar remote: those
    # remotes are checked against literal self addresses only.
    _DOTTED_REMOTES = [
        "git clone git@github.com:kirodotdev/KiroCrew.git",
        "git clone ssh://git@github.com/x/y.git",
        "git fetch github.com:x/y main",
        "tar -cf backup.example.com:/archive.tar dir",
    ]

    @pytest.mark.parametrize("cmd", _DOTTED_REMOTES)
    def test_dotted_remote_is_not_dns_pending_refused(self, cmd, _floor_monkeypatch):
        _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: True)
        assert not security._is_git_tar_to_self(cmd), cmd
        assert security._is_git_tar_to_self("git clone localhost:x")

    # A git/tar remote host is read as typed: git and tar never glob-expand a
    # host, and a word with a ``/`` before its colon is a refspec, not a
    # remote.  Maintainer decision: these are not hosts.
    _LITERAL_HOST_ALLOWED = [
        "git fetch origin '+refs/heads/*:refs/remotes/origin/*'",
        "git fetch origin +refs/heads/*:refs/remotes/origin/*",
        "git push origin '*:*'",
        "git ls-remote local*:repo",
        "git ls-remote l?calhost:repo",
        "git -c core.sshcommand=s?h ls-remote local*:x",
        "tar -cf local*:out.tar s*",
    ]

    @pytest.mark.parametrize("cmd", _LITERAL_HOST_ALLOWED)
    def test_glob_or_refspec_host_is_allowed(self, cmd):
        assert not security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    # A command value that names only REMOTE hosts (a ProxyJump bastion) is read
    # literally, with no DNS: the DNS layer answering "pending" for the
    # first-seen dotted bastion must not refuse it.
    _REMOTE_HOP_VALUES = [
        'GIT_SSH_COMMAND="ssh -o ProxyJump=bastion.example.com" git fetch origin',
        "GIT_SSH_COMMAND='ssh -J bastion.example.com' git fetch origin",
        "git -c core.sshCommand='ssh -o ProxyJump=bastion.example.com' fetch origin",
        "git clone --upload-pack='ssh bastion.example.com git-upload-pack' far:x",
        "tar --rsh-command='ssh -J bastion.example.com' -cf far.example.com:a.tar dir",
    ]

    @pytest.mark.parametrize("cmd", _REMOTE_HOP_VALUES)
    def test_remote_hop_value_is_not_dns_pending_refused(self, cmd, _floor_monkeypatch):
        _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: True)
        assert not security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd
        # The literal self-dialing value is still refused under the same stub.
        assert security._is_git_tar_to_self("GIT_SSH_COMMAND='ssh localhost' git fetch origin")

    def test_force_local_cannot_exempt_a_self_shaped_archive(self):
        # Failing closed here is the maintainer-accepted price of removing the
        # option-grammar exemption: a self-shaped archive is always refused.
        assert security._is_git_tar_to_self("tar --force-local -cf localhost:a.tar dir")
        assert security._is_git_tar_to_self("tar -cf localhost:a.tar --force-local")

    def test_own_hostname_and_fqdn_still_refused(self, monkeypatch):
        # dns=False keeps the literal own-name cache authoritative: this
        # machine's own short name AND its FQDN are still refused as self-dials
        # for git and tar, while an unrelated dotted remote stays allowed.
        monkeypatch.setattr(
            _argv_floor,
            "_OWN_HOST_NAMES_CACHE",
            frozenset({"selfhost", "selfhost.example.com"}),
        )
        for cmd in (
            "git clone selfhost:x",
            "git clone git@selfhost.example.com:x",
            "git fetch selfhost.example.com:x main",
            "tar -cf selfhost.example.com:/a.tar dir",
        ):
            assert security._is_git_tar_to_self(cmd), cmd
        assert not security._is_git_tar_to_self("git clone notus.example.com:x")

    def test_pattern_is_a_subset_of_the_floor_predicate(self):
        """Every command the catalog pattern denies, the predicate denies too."""
        import re

        rx = re.compile(_rule_pattern(self._RULE), re.IGNORECASE)
        corpus = [
            "git clone localhost:x",
            "git fetch 127.0.0.1:x",
            "git pull localhost:/srv/x",
            "git ls-remote localhost:x",
            "git push me@localhost:x",
            "/usr/bin/git clone localhost:x",
            "git.exe clone localhost:x",
            "true; git clone localhost:x",
            "git clone [::1]:x",
            "git clone 127.0.0.1:",
            # non-escape forms the pattern must NOT match
            "git clone https://localhost/x",
            "git log --grep localhost:x",
            "git clone /local/path",
        ]
        for cmd in corpus:
            if rx.search(cmd.lower()):
                assert security._is_git_tar_to_self(
                    cmd.lower()
                ), f"pattern matched but predicate did not: {cmd}"

    # The raw-text pattern's userinfo group must not swallow a ``scheme://``
    # prefix: an http(s) URL with ``user@`` to this host is not an ssh dial.
    @pytest.mark.parametrize(
        "cmd",
        [
            "git clone http://dev@localhost:3000/org/app.git",
            "git fetch https://ci@127.0.0.1:8443/org/app.git main",
            "git.exe clone http://dev@localhost:3000/org/app.git",
        ],
    )
    def test_pattern_skips_http_userinfo_to_self(self, cmd):
        import re

        rx = re.compile(_rule_pattern(self._RULE), re.IGNORECASE)
        assert rx.search(cmd) is None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "git clone user@localhost:x",
            "git clone localhost:x",
            "git fetch me@127.0.0.1:x",
            "git clone user@[::1]:x",
        ],
    )
    def test_pattern_still_matches_scp_self_dials(self, cmd):
        import re

        rx = re.compile(_rule_pattern(self._RULE), re.IGNORECASE)
        assert rx.search(cmd) is not None, cmd

    _CONFIG_TRANSPORT_COMMANDS = [
        (
            "clone",
            "git {config} clone https://github.com/kirodotdev/KiroCrew.git",
            "git clone {config} https://github.com/kirodotdev/KiroCrew.git",
        ),
        ("fetch", "git {config} fetch origin", "git fetch {config} origin"),
        (
            "ls-remote",
            "git {config} ls-remote https://github.com/kirodotdev/KiroCrew.git",
            "git ls-remote {config} https://github.com/kirodotdev/KiroCrew.git",
        ),
        (
            "submodule-update",
            "git {config} submodule update",
            "git submodule update {config}",
        ),
        (
            "remote-add",
            "git {config} remote add origin https://github.com/kirodotdev/KiroCrew.git",
            "git remote add {config} origin https://github.com/kirodotdev/KiroCrew.git",
        ),
    ]
    _CONFIG_CASES = [
        ("http-proxy", "http.proxy", "localhost:3128", None),
        ("https-proxy", "https.proxy", "localhost:3128", None),
        ("remote-url", "remote.x.url", "localhost:x", _RULE),
        (
            "url-rewrite",
            "url.ssh://localhost/.insteadOf",
            "https://github.com/",
            _RULE,
        ),
        ("ssh-command", "core.sshCommand", "ssh localhost", _SSH_RULE),
    ]

    @pytest.mark.parametrize(("verb", "pre", "post"), _CONFIG_TRANSPORT_COMMANDS)
    @pytest.mark.parametrize(("case", "key", "value", "rule"), _CONFIG_CASES)
    @pytest.mark.parametrize("position", ["pre", "post"])
    @pytest.mark.parametrize("source", ["config", "config-env"])
    def test_git_config_values_are_classified_by_key(
        self, verb, pre, post, case, key, value, rule, position, source
    ):
        del verb, case
        if source == "config-env":
            prefix = f"CONFIG_VALUE='{value}' "
            config = f"--config-env {key}=CONFIG_VALUE"
        else:
            prefix = ""
            config = f"-c {key}='{value}'"
        cmd = prefix + (pre if position == "pre" else post).format(config=config)
        assert security._is_git_tar_to_self(cmd) is (rule is not None), cmd
        assert _denied_by(cmd) == rule, cmd

    def test_post_verb_proxy_config_is_not_a_remote_candidate(self):
        cmd = "git clone -c http.proxy=localhost:3128 https://github.com/org/repo"
        assert not security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    @pytest.mark.parametrize(
        "template",
        ["-c {}", "-c{}", "--config {}", "--config={}"],
        ids=["short-detached", "short-attached", "long-detached", "long-attached"],
    )
    @pytest.mark.parametrize(
        ("config", "denied"),
        [
            ("http.proxy=localhost:3128", False),
            ("remote.x.url=localhost:x", True),
        ],
        ids=["http-proxy", "remote-url"],
    )
    def test_post_verb_config_spellings_use_the_key_aware_classifier(
        self, template, config, denied
    ):
        option = template.format(config)
        cmd = f"git clone {option} https://github.com/kirodotdev/KiroCrew.git"
        assert security._is_git_tar_to_self(cmd) is denied, cmd
        if denied:
            assert _denied_by(cmd) == self._RULE, cmd
        else:
            assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    @pytest.mark.parametrize(
        ("key", "value", "denied"),
        [
            ("remote.x.url", "localhost:x", True),
            ("http.proxy", "localhost:3128", False),
        ],
        ids=["transport-url", "http-proxy"],
    )
    @pytest.mark.parametrize(
        "env_form",
        [
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0={key} GIT_CONFIG_VALUE_0={value} ",
            "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0={key} " "GIT_CONFIG_VALUE_0={value}; ",
            "declare -x GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0={key} " "GIT_CONFIG_VALUE_0={value}; ",
            "typeset -x GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0={key} " "GIT_CONFIG_VALUE_0={value}; ",
        ],
        ids=["prefix", "export", "declare-export", "typeset-export"],
    )
    def test_numbered_environment_config_uses_the_key_aware_classifier(
        self, key, value, denied, env_form
    ):
        cmd = env_form.format(key=key, value=value) + "git fetch origin"
        assert security._is_git_tar_to_self(cmd) is denied, cmd
        if denied:
            assert _denied_by(cmd) == self._RULE, cmd
        else:
            assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            ("git -c remote.selftarget.url=localhost:x ls-remote selftarget", _RULE),
            ("git -c remote.selftarget.pushurl=localhost:x ls-remote selftarget", _RULE),
            ("git -c alias.foo='ssh localhost' ls-remote far:x", _SSH_RULE),
        ],
        ids=["remote-url", "remote-pushurl", "command-value-key-agnostic"],
    )
    def test_transport_url_key_or_command_value_to_self_is_denied(self, cmd, rule):
        assert _denied_by(cmd) == rule, cmd

    # git percent-decodes a URL authority before dialing, so the self check
    # reads the DECODED host.
    @pytest.mark.parametrize(
        "cmd",
        [
            "git ls-remote --upload-pack='sh -c id' ssh://local%68ost/x",
            "git fetch ssh://127.0.0.%31/x",
            "git clone git+ssh://local%68ost/x",
        ],
        ids=["encoded-host", "encoded-ip", "alias-encoded-host"],
    )
    def test_percent_encoded_self_host_is_denied(self, cmd):
        assert security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) == self._RULE, cmd

    def test_percent_encoded_remote_host_stays_allowed(self):
        # Decoding only ever reveals a literal self host; a decoded remote host
        # is still just a remote.
        cmd = "git clone ssh://git%68ub.example.com/x"
        assert not security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    # A separator REAL at the nested payload's own level splits it into its
    # sub-commands, so the git/tar verb after ``cd``/``true`` is read; a
    # single-command payload and a quoted separator at the TOP level are
    # unchanged.
    @pytest.mark.parametrize(
        "cmd",
        [
            "sh -c 'cd /; git clone ssh://127.0.0.1/r'",
            "bash -c 'true && tar -cf localhost:o d'",
            "sh -c 'cd /; git clone localhost:x'",
            "sh -c 'git clone localhost:x'",
        ],
        ids=["nested-semicolon-git", "nested-andand-tar", "nested-semicolon-scp", "single-command"],
    )
    def test_nested_multi_command_payload_to_self_is_denied(self, cmd):
        assert security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) == self._RULE, cmd

    def test_nested_payload_comment_is_data_at_its_own_level(self):
        # The ``#`` is a comment at the payload's OWN level, so ``localhost:y``
        # after it is prose, not a second remote; the clone target is far:x.
        # (A frame that left the separator masked would read the comment tail as
        # a self dial.)
        cmd = "sh -c 'git clone far:x # localhost:y'"
        assert not security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    def test_attr_source_global_option_value_is_stepped_over(self):
        # ``--attr-source`` takes a SEPARATE value, so the verb after its value
        # is the transport verb.
        cmd = "git --attr-source HEAD fetch ssh://localhost/x"
        assert security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) == self._RULE, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "TAR_OPTIONS='--rsh-command=ssh --file=localhost:out.tar' "
            "tar -c --files-from=/dev/null",
            "TAR_OPTIONS=\"--rsh-command='ssh' --file=localhost:x\" tar -c dir",
            'TAR_OPTIONS="--file=127.0.0.1:x \'" tar -c dir',
            "TAR_OPTIONS='-f localhost:out.tar' tar -c dir",
            "TAPE=localhost:x tar -c dir",
            "export TAR_OPTIONS='--rsh-command=ssh -f localhost:o'; tar -c dir",
            "declare -x TAR_OPTIONS='-f localhost:o'; tar -c dir",
            "typeset -x TAR_OPTIONS='-f localhost:o'; tar -c dir",
            "env TAR_OPTIONS='-f localhost:o' tar -c dir",
            "sh -c 'TAR_OPTIONS=\"-f localhost:o\" tar -c dir'",
            # The TAPE default archive is refused whenever it is self-shaped,
            # even with an explicit ``-f`` argv archive or any ``f`` glued into
            # a value or case-folded option letter: the old ``f``-in-cluster
            # shortcut that skipped the TAPE check is gone.
            "TAPE=localhost:x tar -cf out.tar dir",
            "TAPE=localhost:x tar -c -C/tmp/foo d",
            "TAPE=localhost:x tar -c -F true d",
            "TAPE=localhost:x tar -c -CF/tmp/foo d",
            "TAR_OPTIONS='-C/tmp/foo' TAPE=localhost:x tar -c d",
        ],
        ids=[
            "options-rsh-and-file",
            "options-nested-rsh-and-file",
            "options-unbalanced-self-file",
            "options-short-file",
            "tape-default",
            "export",
            "declare-export",
            "typeset-export",
            "env-wrapper",
            "nested-shell",
            "tape-with-explicit-file",
            "tape-f-in-glued-value",
            "tape-upper-case-F-option",
            "tape-f-in-mixed-case-cluster",
            "tape-f-in-tar-options-value",
        ],
    )
    def test_tar_environment_to_self_is_denied(self, cmd):
        assert _denied_by(cmd) == self._RULE, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "TAR_OPTIONS='-f out.tar' tar -c dir",
            "TAR_OPTIONS='--exclude=*.pyc' tar -czf src.tgz src",
            "TAR_OPTIONS=\"--exclude='*.pyc'\" tar -czf src.tgz src",
            "TAR_OPTIONS='--verbose' tar -c dir",
            "TAPE=/dev/st0 tar -c dir",
            "TAPE=/dev/st0 tar -cf out.tar dir",
            "tar -c -f far.example.com:o dir",
        ],
        ids=[
            "options-local-file",
            "options-exclude",
            "options-nested-quote-exclude",
            "options-verbose",
            "tape-local-device",
            "tape-local-device-with-explicit-file",
            "argv-far-host",
        ],
    )
    def test_tar_environment_not_to_self_is_allowed(self, cmd):
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    # tar reads every word of a ``TAR_OPTIONS`` value as an option word: a
    # ``;``, ``&&`` or ``|`` there is data, so it never ends the read of the
    # argv that follows it.  A newline inside the value is plain whitespace to
    # tar's splitter, in a literal single-quoted value and in ``$'...'`` alike.
    @pytest.mark.parametrize(
        "cmd",
        [
            "TAR_OPTIONS='; x' tar -cf localhost:x d",
            "TAR_OPTIONS='&& x' tar -cf localhost:x d",
            "TAR_OPTIONS='| x' tar -cf localhost:x d",
            "TAR_OPTIONS='|| x' tar -cf localhost:x d",
            "TAR_OPTIONS='& x' tar -cf localhost:x d",
            "TAR_OPTIONS='x ;' tar -cf localhost:x d",
            "TAR_OPTIONS='-- ; x' tar -cf localhost:x d",
            'TAR_OPTIONS="; x" tar -cf localhost:x d',
            "TAR_OPTIONS=$'; x' tar -cf localhost:x d",
            "TAR_OPTIONS='; -f localhost:x' tar -c d",
            "TAR_OPTIONS='; x' tar -c --file=localhost:x d",
            "TAR_OPTIONS='x\ny' tar -cf localhost:x d",
            "TAR_OPTIONS=$'x\\ny' tar -cf localhost:x d",
            "export TAR_OPTIONS='; x'; tar -cf localhost:x d",
            "export TAR_OPTIONS='&& x'; tar -cf localhost:x d",
        ],
        ids=[
            "semicolon",
            "and-list",
            "pipe",
            "or-list",
            "background",
            "trailing-semicolon",
            "after-end-of-options",
            "double-quoted",
            "ansi-c-quoted",
            "self-archive-in-value",
            "attached-file-option",
            "literal-newline",
            "ansi-c-newline",
            "exported",
            "exported-and-list",
        ],
    )
    def test_tar_options_separator_words_are_data(self, cmd):
        assert security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) == self._RULE, cmd

    @pytest.mark.parametrize(
        ("cmd", "denied"),
        [
            ("TAR_OPTIONS='x' tar -cf localhost:x d", True),
            ("TAR_OPTIONS='; x' tar -cf out.tar d", False),
            ("tar -cf out.tar d; echo ok", False),
            ("tar -cf out.tar d; echo localhost:x", False),
            ("TAR_OPTIONS='-v' tar -cf out.tar d; tar -tf out.tar", False),
        ],
        ids=[
            "plain-value-self-archive",
            "separator-value-local-archive",
            "real-separator",
            "real-separator-ends-the-tar-argv",
            "real-separator-between-tars",
        ],
    )
    def test_tar_options_controls(self, cmd, denied):
        assert security._is_git_tar_to_self(cmd) is denied, cmd
        assert (_denied_by(cmd) == self._RULE) is denied, cmd
        if not denied:
            assert _denied_by(cmd) is None, cmd

    # The detached value of a value-taking tar option (``_TAR_VALUE_OPTS`` /
    # ``_TAR_VALUE_SHORTS``) is that option's value, never an archive: ``0:0``
    # after ``--owner`` is not host ``0`` plus a path.  Separate, ``=``, glued
    # and bundled forms, abbreviations and ``TAR_OPTIONS`` words alike.
    _TAR_OPTION_VALUE_ALLOWED = [
        "tar -czf release.tgz --owner 0:0 dist/",
        "tar -czf release.tgz --group 0:0 dist/",
        "tar -czf release.tgz --mode 0:0 dist/",
        "tar -czf release.tgz --owner=0:0 dist/",
        "tar -czf release.tgz --own 0:0 dist/",
        "tar --owner 0:0 -czf release.tgz dist/",
        "tar -czf release.tgz -C 0:0 dist/",
        "tar -czf release.tgz --directory 0:0 dist/",
        "tar -czf release.tgz --directory=0:0 dist/",
        "tar -czf release.tgz -C0:0 dist/",
        "tar -czC 0:0 -f release.tgz dist/",
        "tar -xzC 0:0 -f release.tgz",
        "tar -czf release.tgz --transform s/0:0// dist/",
        "tar -czf release.tgz --label 0:0 dist/",
        "tar -czf release.tgz --exclude 0:0 dist/",
        "tar -cf release.tgz --use-compress-program 0:0 dist/",
        "tar -czf release.tgz --mtime 0:0 dist/",
        "tar -czf release.tgz -T 0:0",
        "tar -czf release.tgz -X 0:0 dist/",
        "tar -cf release.tar -b 0:0 dist/",
        "tar -czf release.tgz --newer 0:0 dist/",
        "tar -czf release.tgz -N 0:0 dist/",
        "tar -xzf release.tgz --strip-components 0:0",
        "tar -czf release.tgz --sort 0:0 dist/",
        "tar -czf release.tgz --format 0:0 dist/",
        "tar -czf release.tgz -H 0:0 dist/",
        "TAR_OPTIONS='--owner 0:0' tar -czf release.tgz dist/",
        "TAR_OPTIONS='-C 0:0' tar -czf release.tgz dist/",
    ]

    @pytest.mark.parametrize("cmd", _TAR_OPTION_VALUE_ALLOWED)
    def test_tar_option_values_are_not_archives(self, cmd):
        assert not security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    @pytest.mark.parametrize("option", sorted(_argv_floor._TAR_VALUE_OPTS))
    def test_every_long_value_option_takes_its_value(self, option):
        for cmd in (f"tar -cf out.tar --{option} 0:0 d", f"tar -cf out.tar --{option}=0:0 d"):
            assert not security._is_git_tar_to_self(cmd), cmd
        assert security._is_git_tar_to_self(f"tar --{option} 0:0 -cf localhost:x d")

    @pytest.mark.parametrize("letter", sorted(_argv_floor._TAR_VALUE_SHORTS))
    def test_every_short_value_option_takes_its_value(self, letter):
        for cmd in (f"tar -cf out.tar -{letter} 0:0 d", f"tar -cz{letter} 0:0 -f out.tar d"):
            assert not security._is_git_tar_to_self(cmd), cmd
        assert security._is_git_tar_to_self(f"tar -{letter} 0:0 -cf localhost:x d")

    # The archive and the remote-shell command stay read wherever they sit
    # among value options.  A word that looks like an option is read as one,
    # since the case-folded ``-c`` may be the valueless create flag.
    # A carried ``ssh localhost`` command may be refused by the ssh floor first.
    @pytest.mark.parametrize(
        "cmd",
        [
            "tar -cf 0:0 dir",
            "tar -czf localhost:x dir",
            "tar --file=localhost:x -c d",
            "tar --file localhost:x -c d",
            "tar -c -f localhost:x d",
            "tar -czf localhost:x --owner 0:0 d",
            "tar --owner 0:0 -cf localhost:x d",
            "tar -cC 0:0 -f localhost:x d",
            "TAR_OPTIONS='--owner 0:0' tar -cf localhost:x d",
            "TAR_OPTIONS='; -f localhost:x' tar -c d",
            "TAR_OPTIONS='--owner 0:0 -f localhost:x' tar -c d",
            "tar -c --rsh-command='ssh localhost' -f h:x d",
            "tar -c --rsh-command 'ssh localhost' -f h:x d",
            "tar -c -I 'ssh localhost' -f out.tar d",
            "tar -c --owner 'ssh localhost' -f out.tar d",
            "tar -c --fil localhost:x d",
            "tar -c --rsh localhost:x -f h:x d",
            "tar -c -H --file=localhost:x d",
            "tar -c --owner -flocalhost:x d",
            "tar -c --file=localhost:x d",
            "tar -cv --file=localhost:x d",
            "tar -x -flocalhost:x",
            "tar --exclude -- -f localhost:x d",
        ],
    )
    def test_tar_archive_among_option_values_is_denied(self, cmd):
        assert security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) in self._DIAL_RULES, cmd

    # The gate reads the command lower-cased, so ``-c`` (create) and ``-C``
    # (directory), ``-t`` and ``-T``, ``-v`` and ``-V`` fold together and the
    # folded letter counts as value-taking.  The word after it is then skipped
    # as an archive candidate.  GNU tar reads that word as a member name, never
    # as the archive, so tar never dials it.  Each allowed shape is paired with
    # the same word named as the archive by ``-f``/``--file`` or ``TAPE``.
    @pytest.mark.parametrize(
        "allowed, denied",
        [
            ("tar -c localhost:x", "tar -c -f localhost:x"),
            ("tar -c localhost:x", "TAPE=localhost:x tar -c localhost:x"),
            ("tar -cv localhost:x -f out.tar", "tar -cv -f localhost:x out.tar"),
            ("tar -t localhost:x", "tar -t --file localhost:x"),
            ("tar -czv 0:0 -f out.tgz", "tar -czv -f 0:0 out.tgz"),
            ("tar -c -f out.tar -v localhost:x", "TAPE=localhost:x tar -c -v d"),
        ],
    )
    def test_case_folded_member_names_are_not_archives(self, allowed, denied):
        assert not security._is_git_tar_to_self(allowed), allowed
        assert _denied_by(allowed) not in self._DIAL_RULES, allowed
        assert security._is_git_tar_to_self(denied), denied
        assert _denied_by(denied) in self._DIAL_RULES, denied

    @pytest.mark.parametrize(
        "cmd",
        [
            "GIT_CONFIG_PARAMETERS=\"'core.sshCommand=ssh localhost id #'\" " "git ls-remote far:x",
            "GIT_CONFIG_PARAMETERS=\"'core.sshCommand'='ssh localhost id #'\" "
            "git ls-remote far:x",
            "GIT_CONFIG_PARAMETERS=\"'alias.note=it'\\''s' "
            "'core.sshCommand=ssh localhost'\" git ls-remote far:x",
            "env GIT_CONFIG_PARAMETERS=\"'core.sshCommand=ssh localhost id #'\" "
            "git ls-remote far:x",
            "export GIT_CONFIG_PARAMETERS=\"'core.sshCommand=ssh localhost id #'\"; "
            "git ls-remote far:x",
            "declare -x GIT_CONFIG_PARAMETERS=\"'core.sshCommand=ssh localhost id #'\"; "
            "git ls-remote far:x",
            "typeset -x GIT_CONFIG_PARAMETERS=\"'core.sshCommand=ssh localhost id #'\"; "
            "git ls-remote far:x",
            "GIT_CONFIG_PARAMETERS=\"'core.sshCommand=ssh localhost id #'\" "
            "sh -c 'git ls-remote far:x'",
        ],
        ids=[
            "inline",
            "split-key-value",
            "escaped-quote",
            "env",
            "export",
            "declare-export",
            "typeset-export",
            "nested-shell",
        ],
    )
    def test_git_config_parameters_sq_quote_to_self_is_denied(self, cmd):
        assert _denied_by(cmd) == self._SSH_RULE, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "GIT_CONFIG_PARAMETERS=\"'http.proxy=localhost:3128'\" git ls-remote far:x",
            "GIT_CONFIG_PARAMETERS=\"'core.sshCommand=ssh far.example.com'\" "
            "git ls-remote far:x",
        ],
        ids=["http-proxy", "remote-ssh-command"],
    )
    def test_git_config_parameters_non_self_values_are_allowed(self, cmd):
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    def test_malformed_git_config_parameters_falls_back_to_the_config_classifier(self):
        cmd = 'GIT_CONFIG_PARAMETERS="remote.x.url=localhost:x \'" git ls-remote far:x'
        assert _denied_by(cmd) == self._RULE

    @pytest.mark.parametrize(
        ("cmd", "denied"),
        [
            ("git clone '[localhost:22]:x'", True),
            ("git clone '[127.0.0.1:22]:x'", True),
            ("git clone '[localhost]:x'", True),
            ("git clone '[::1]:x'", True),
            ("git clone '[user@localhost:22]:x'", True),
            ("git clone '[far.example.com:22]:x'", False),
        ],
        ids=[
            "hostname-and-port",
            "ipv4-and-port",
            "hostname-no-port",
            "ipv6-control",
            "userinfo-control",
            "remote-control",
        ],
    )
    def test_bracketed_scp_hosts_are_classified(self, cmd, denied):
        assert (_denied_by(cmd) == self._RULE) is denied, cmd

    # One level of function-call arguments is bound the way the ssh floor binds
    # them: the call argument is the remote the body's ``$1`` names.
    @pytest.mark.parametrize(
        ("cmd", "denied"),
        [
            ('f(){ git ls-remote --upload-pack="id #" "$1"; }; f localhost:x', True),
            ('f(){ git ls-remote "${1}"; }; f localhost:x', True),
            ("f(){ git ls-remote $@; }; f localhost:x", True),
            ('f(){ git ls-remote "$*"; }; f localhost:x', True),
            ('f(){ tar -cf "$1" dir; }; f localhost:a.tar', True),
            ('f(){ git ls-remote "$1"; }; f far.example.com:x', False),
            ('f(){ tar -cf "$1" dir; }; f far.example.com:a.tar', False),
            ('f(){ tar -cf out.tar "$1"; }; f localhost:file', False),
            ("f(){ git status; }; f", False),
        ],
        ids=[
            "upload-pack-positional",
            "braced-positional",
            "at-positional",
            "star-positional",
            "tar-archive-positional",
            "remote-control",
            "tar-remote-control",
            "tar-member-control",
            "local-control",
        ],
    )
    def test_function_call_arguments_are_bound(self, cmd, denied):
        assert (_denied_by(cmd) == self._RULE) is denied, cmd
        if not denied:
            assert _denied_by(cmd) is None, cmd

    # Git's sq-quote ``'\''`` is masked only inside an open single quote.  Outside
    # one, ``x'\''abc'`` is ``x``, a quoted ``\`` and ``'abc'``, so the ``;`` after it
    # is a real separator and the git command after it is read.
    @pytest.mark.parametrize(
        ("cmd", "denied"),
        [
            ("echo x'\\''abc' ; git clone ssh://localhost/x", True),
            ("echo x'\\''abc' ; git clone ssh://far.example.com/x", False),
            ("echo 'it'\\''s'; git status", False),
            ("git clone ssh://localhost/x", True),
            ("echo hi ; git clone ssh://localhost/x", True),
        ],
        ids=[
            "escape-outside-quote-to-self",
            "escape-outside-quote-remote-control",
            "escape-inside-quote-control",
            "ssh-url-alone",
            "ssh-url-after-separator",
        ],
    )
    def test_sq_escape_outside_a_quote_keeps_the_separator(self, cmd, denied):
        assert (_denied_by(cmd) == self._RULE) is denied, cmd
        if not denied:
            assert _denied_by(cmd) is None, cmd

    @pytest.mark.parametrize(
        ("cmd", "commands"),
        [
            (
                "echo x'\\''abc' ; git clone ssh://localhost/x",
                [["echo", "x\\abc"], ["git", "clone", "ssh://localhost/x"]],
            ),
            (
                "git -c core.sshcommand='ssh '\\''localhost'\\''' clone far:x",
                [["git", "-c", "core.sshcommand=ssh '\\''localhost'\\''", "clone", "far:x"]],
            ),
        ],
        ids=["escape-outside-quote", "escape-inside-quote"],
    )
    def test_sq_escape_framing_follows_the_quote_state(self, cmd, commands):
        from kiro_crew.security.argv_rules import _split_commands

        tokens, left_open = _argv_floor._frame_commands(cmd)
        assert _split_commands(tokens) == commands
        assert left_open is False

    # A frame whose text ends inside a quote has every later separator masked as
    # data.  The commands the shell reader finds are read too, and a quote the
    # shell reader cannot close either is refused (fail closed).  A quote in a
    # comment or a here-document body, or after an ANSI-C ``\'``, opens nothing.
    @pytest.mark.parametrize(
        ("cmd", "denied"),
        [
            ("echo 'abc ; git clone ssh://localhost/x", True),
            ("echo $'a\\'b' ; git clone ssh://localhost/x", True),
            ("git status # it's\ngit clone ssh://localhost/x", True),
            ("cat > notes.md <<'EOF'\nDon't\nEOF\ngit clone ssh://localhost/x", True),
            ("echo $'a\\'b' ; git clone ssh://far.example.com/x", False),
            ("git commit -m 'fix' # don't amend", False),
            ("cat > notes.md <<'EOF'\nDon't push yet\nEOF\ngit status", False),
        ],
        ids=[
            "unclosed-quote",
            "ansi-c-escape-to-self",
            "comment-apostrophe-to-self",
            "heredoc-apostrophe-to-self",
            "ansi-c-escape-remote-control",
            "comment-apostrophe-control",
            "heredoc-apostrophe-control",
        ],
    )
    def test_frame_left_inside_a_quote_fails_closed(self, cmd, denied):
        assert (_denied_by(cmd) == self._RULE) is denied, cmd
        if not denied:
            assert _denied_by(cmd) is None, cmd
