"""ext / executed-config / linearity / alias tests for the git/tar self floor.

Split out of ``test_denied_commands_security_git_tar_self.py`` to keep each file
under the 2000-line cap. Same rule, own-host pin and helpers as that sibling;
this file holds the ``ext::`` decode, executed-value config keys, GIT_SSH env,
url-rewrite base, the bounded-work (linearity) tests and the alias/residual
cases.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import (  # noqa: F401
    _denied_by,
    _interpreter_work,
    _linear_work,
    _rule_pattern,
)

from kiro_crew import security
from kiro_crew.security import BUILTIN_DENIED_RULES  # noqa: F401
from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import compute_effective_denied, is_denied, is_safe_user_regex  # noqa: F401

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

#: Ordinary prose, commit messages and PR-body text with an apostrophe or a
#: quote that never closes.  ``is_denied`` reads every tool-input string, so
#: each one must stay allowed, including text that holds ``git``/``tar`` inside
#: a longer word (``digit``, ``start``, ``guitar``).
_OPEN_QUOTE_PROSE = {
    "i-dont-know": "I don't know",
    "dont-restart": "Please don't restart the server",
    "echo-digit": "echo it's a digit",
    "cant-reproduce": "can't reproduce on my machine",
    "its-fine": "it's fine, ship it",
    "wont-fix": "won't fix: works as intended",
    "users-file": "Open the user's file and read it",
    "open-double-quote": '"quoted phrase without an end',
    "separator-in-prose": "don't do that; it's fine",
    "rm-in-a-note": "rm -rf not run, it's a note",
    "commit-body": (
        "Fix the loader so it doesn't crash\n\n"
        "The user's config wasn't read when the path had a space."
    ),
    "pr-body": "Summary\n\nThis change doesn't alter the CLI. Reviewers' notes are welcome.",
    "start-target": "We'll start the target after lunch",
    "avatar": "The avatar's border is off by one",
    "digital": "Digital signatures aren't checked yet",
    "lets-restart": "Let's restart: it'll take a minute",
    "guitar": "O'Brien's guitar wasn't tuned",
    "said-dont": "She said \"don't, and left",
    "markdown-list": "Here's a list:\n- don't panic\n- it's ok",
    "parenthesis": "Note: the tests weren't run (CI's down)",
    "legit": "Use the 'legit' path, isn't it?",
    "dollar-amount": "Costs are $5 per run, isn't that odd",
    "tilde-path": "Don't touch ~/notes, it's mine",
    "backtick": "Wait `it's done",
    "echo-pipe": "echo don't | tee notes.txt",
    "commit-command": "git commit -m \"don't break the user's flow\"",
    # A ``git``/``tar`` word that is NOT in command position names no command:
    # it sits mid-sentence or is an argument, so an open quote beside it leaves
    # the string to the other floors.  A bare ``*``/``?`` program word matches
    # every file in the directory, not a fixed program, so it names none either.
    "cant-run-git": "I can't run git here",
    "bare-star-prose": "* don't do that",
    "dash-bullet": "- don't retry the push",
    "bold-cant": "**bold** text can't nest here",
    "git-mid-sentence": "we should git-blame it, can't tell",
    "tar-later": "I'd use tar later, not now",
    "pr-body-tar": "The release tarball wasn't attached; I'll add tar output",
    "digit-target": "the digit isn't right and target's off",
    # A backslash-escaped ``\$`` is a literal dollar, not an expansion, so a
    # quoted regex argument that holds one names no program even beside an
    # open quote.
    "escaped-dollar-regex": 'grep -n "a|\\$x" it\'s.txt',
    "markdown-star-list": "* item one\n* don't forget two",
    "git-as-argument": "please don't run git status yet",
    # A ``git``/``tar`` word that IS in command position (a line start) but whose
    # whole string names no self host, carries no override shape and holds no
    # ``$`` expansion.  An unclosed apostrophe cannot make a self host appear, so
    # the open-quote backstop finds no self-dial signal and leaves these to the
    # other floors -- exactly the file-write and computer-use text this PR broke.
    "its-fine-git": "It's fine\ngit is great",
    "dont-worry-tar": "Don't worry; tar xzf is easy",
    "markdown-git-clone": (
        "# Setup\n\nDon't skip this step.\n\n```\ngit clone https://github.com/x/y\n```\n"
    ),
    "cant-test-git-status": "I can't test now\ngit status shows clean",
    "readme-tar-note": "The build's output isn't small\ntar czf dist.tgz dist\n",
    "changelog-git-log": "What's new\n\ngit log --oneline shows it; don't rebase\n",
    # A non-fenced inline backtick beside an open quote, with a git/tar word as
    # the backtick's first token, is ordinary prose: it names no self host and
    # opens no transport, so the open-quote backstop (kinds 1-2) leaves it to
    # the other floors.  ``is_denied`` reads editor / computer-use text, so a
    # ``git status`` or HTTPS-clone example and a lone backtick must stay allowed.
    "inline-backtick-git-status": "it's a `git status` example here",
    "inline-backtick-https-clone": "run `git clone https://github.com/x/y` to start",
    "lone-backtick-git": "a `one git status",
    # A ``$`` that builds no self host (``$HOME``) is prose, not a self-dial.
    "dollar-home-git": "It's $HOME\ngit status",
    # A base64 / arbitrary substitution the floor cannot read is left to the
    # other floors behind an unclosed quote, the same as behind a closed one:
    # the backstop reads only a self host or override it can recognise, not an
    # opaque command substitution.
    "open-backtick-opaque-substitution": "x'; git clone `printf bG9jYWxob3N0|base64 -d`:x",
    "open-dollar-opaque-substitution": "x'; git clone $(printf bG9jYWxob3N0|base64 -d):x",
}

#: Commands that dial this host, or carry a self-dial SIGNAL beside a quote no
#: reader can close, and must stay refused.  The rows after the plain dials end
#: inside an unclosable quote, so only the open-quote refusal
#: (``_open_frame_could_run_git_tar``) can refuse them, and only when the whole
#: string also carries a self-dial signal the backstop reads: a literal self
#: host (kind 1), including a ``$(hostname)`` / `` `hostname `` self-hostname
#: form, or an ``ext::`` / ``ssh://`` / ``core.sshCommand`` / ``GIT_SSH``
#: override shape (kind 2).  A bare ``$`` or a lone backtick that builds no
#: recognisable self host is prose, not a signal, so it sits in the prose set.
_OPEN_QUOTE_SELF_DIALS = {
    "unclosed-quote": "echo 'abc ; git clone ssh://localhost/x",
    "ansi-c-escape": "echo $'a\\'b' ; git clone ssh://localhost/x",
    "comment-apostrophe": "git status # it's\ngit clone ssh://localhost/x",
    "heredoc-apostrophe": "cat > notes.md <<'EOF'\nDon't\nEOF\ngit clone ssh://localhost/x",
    "sq-escape-outside-quote": "echo x'\\''abc' ; git clone ssh://localhost/x",
    "tar-glued-i": "tar -I'ssh localhost id' -cf - --files-from=/dev/null",
    "tar-cluster-i": "tar -cI'ssh localhost id' -f - --files-from=/dev/null",
    "tar-glued-f": "tar -F'ssh localhost id' -cf x.tar dir",
    "git-ssh-url": "git clone ssh://localhost/x",
    "git-scp-like": "git clone localhost:x",
    "open-spliced-git": "echo 'a ; gi''t clone ssh://localhost/x",
    "open-path-git": "echo 'a ; /usr/bin/git clone ssh://localhost/x",
    "open-git-exe": "echo 'a ; git.exe clone ssh://localhost/x",
    "open-tar": "echo 'a ; tar -cf localhost:a.tar dir",
    "open-variable-program": "echo 'a ; $G clone ssh://localhost/x",
    # An escaped backslash leaves the ``$`` after it live.
    "open-escaped-backslash-variable": "echo 'a ; \\\\$G clone ssh://localhost/x",
    "open-glob-git": "echo 'a ; g?t clone ssh://localhost/x",
    "open-glob-path-tar": "echo 'a ; /usr/bin/t*r -cf localhost:a.tar dir",
    "open-carried-git": "echo 'a ; sh -c \"git clone ssh://localhost/x",
    # git in command position at the FRAME START (behind an open quote that an
    # assignment prefix or a command wrapper opened), and right after a
    # separator behind a wrapper or an assignment prefix: command position
    # steps over ``VAR=value`` and ``env``/``sudo``-style wrappers.
    "open-bare-start-git": "x'; git clone ssh://localhost/x",
    "open-assign-prefix-git": "FOO=bar'; git clone ssh://localhost/x",
    "open-wrapper-git": "echo 'a ; sudo git clone ssh://localhost/x",
    "open-assign-then-git": "echo 'a ; FOO=1 git clone ssh://localhost/x",
    # One row per self-dial SIGNAL KIND the open-quote backstop reads, each
    # ending inside an unclosable quote with a git/tar word in command position.
    # ``unclosed-quote`` above is a kind-1 row (literal ``localhost``).  The
    # backstop reads kinds 1 and 2 only: a self host the floor can name, or an
    # override / transport shape that reaches self.  A ``$(hostname)`` /
    # `` `hostname `` substitution is a kind-1 self host too
    # (``_value_names_literal_self`` reads the self-hostname forms), so it stays
    # refused.  A bare ``$`` or a lone backtick that builds no recognisable self
    # host is prose, not a signal, so it is allowed (see ``inline-backtick-git-
    # status`` / ``dollar-home-git`` / the opaque-substitution rows in the prose
    # set).
    # kind 2: a ``GIT_SSH_COMMAND=`` override shape (no literal self host).
    "open-ssh-command-override": "echo 'a ; GIT_SSH_COMMAND=ssh git fetch origin",
    # kind 1 via a self-hostname substitution: ``$(hostname)`` / `` `hostname ``
    # name THIS host, so git would scp-dial them.
    "open-hostname-expansion": "echo 'a ; git clone $(hostname):x",
    "open-hostname-backtick": "x'; git clone `hostname`:x",
}

#: Every corpus row with the verdict ``origin/main`` gives it and the verdict
#: this floor must give (True = refused).  A row this floor must refuse that is
#: allowed is a LOOSEN; a row refused where ``origin/main`` allows it is a
#: TIGHTEN.  Every TIGHTEN now names a real self-dial shape: the prose rows a
#: literal ``git`` word or a bare ``*`` program word once put in TIGHTEN are
#: command-position prose this floor now allows, so they sit in
#: ``_OPEN_QUOTE_PROSE`` (want = allow) and drop out of the TIGHTEN list.
_OPEN_QUOTE_CORPUS = (
    *((cmd, False, False) for cmd in _OPEN_QUOTE_PROSE.values()),
    ("Don't run git push here.", True, False),
    *((cmd, False, True) for cmd in _OPEN_QUOTE_SELF_DIALS.values()),
)


class TestGitTarSelfExec:
    """ext / executed-config / linearity / alias cases for the git/tar floor."""

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

    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            (
                "git -c protocol.ext.allow=always ls-remote " "'ext::git ls-remote localhost:x'",
                _RULE,
            ),
            (
                "git -c protocol.ext.allow=always ls-remote " "'ext::ssh localhost id'",
                _SSH_RULE,
            ),
            (
                "git -c protocol.ext.allow=always ls-remote "
                "'ext::git ls-remote far.example.com:x'",
                None,
            ),
            (
                "git -c protocol.ext.allow=always ls-remote 'ext::sh -c id'",
                None,
            ),
        ],
        ids=["git-self", "ssh-self-control", "git-remote-control", "shell-control"],
    )
    def test_git_ext_commands_run_through_both_command_floors(self, cmd, rule):
        assert _denied_by(cmd) == rule, cmd

    def test_git_ext_escaped_spaces_to_self_are_denied(self):
        cmd = (
            "git -c protocol.ext.allow=always ls-remote "
            "'ext::ssh -oProxyCommand=ssh% localhost% id far.example.com repo'"
        )
        assert _denied_by(cmd) == self._SSH_RULE

    def test_git_ext_escaped_spaces_to_remote_are_allowed(self):
        cmd = (
            "git -c protocol.ext.allow=always ls-remote "
            "'ext::ssh -oProxyCommand=ssh% bastion.example.com far.example.com repo'"
        )
        assert _denied_by(cmd) not in self._DIAL_RULES

    def test_git_ext_literal_percent_is_decoded(self):
        command = _argv_floor._git_ext_command(
            "ext::ssh -oIdentityFile=key%%name far.example.com", []
        )
        assert command == "ssh -oIdentityFile=key%name far.example.com"

    # An unquoted ``ext::`` payload runs on over the operands after it, options
    # included, up to the next ``ext::`` operand.
    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            ("git clone ext::x ext::ssh localhost id", _SSH_RULE),
            ("git clone ext::ssh localhost ext::x", _SSH_RULE),
            ("git clone ext::ssh -p 22 localhost id", _SSH_RULE),
            ("git clone ext::ssh --upload-pack=ext::x localhost", _SSH_RULE),
            ("git clone ext::ssh far.example.com --upload-pack=ext::ssh% localhost", _SSH_RULE),
            ("git clone ext::ssh far.example.com ext::x localhost", None),
        ],
        ids=[
            "second-operand",
            "before-next-operand",
            "over-options",
            "over-option-value",
            "option-value-payload",
            "ends-at-next-operand",
        ],
    )
    def test_an_unquoted_ext_payload_runs_to_the_next_ext_operand(self, cmd, rule):
        assert _denied_by(cmd) == rule, cmd

    # ── executable git config values: the ``!`` and shell forms ──
    # git EXECUTES the value of each key below.  For a ``credential.helper`` /
    # ``credential.<url>.helper`` and an ``alias`` body git strips a leading
    # ``!`` and runs the remainder through the shell; for every other key it
    # runs the value as a shell command verbatim, where a leading ``! word``
    # still runs ``word``.  So a value that dials THIS host is refused in BOTH
    # the ``!ssh localhost id`` form and the bare ``ssh localhost id`` form,
    # reached either as ``-c key=value`` or through the numbered
    # ``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_n`` / ``GIT_CONFIG_VALUE_n``
    # environment.  Source: ``git help config`` for the executing keys and
    # ``git help cli`` / ``git help environment`` for the numbered arrival form.
    _EXECUTED_CONFIG_KEYS = (
        "credential.helper",
        "credential.https://example.com.helper",
        "core.sshCommand",
        "core.gitProxy",
        "core.pager",
        "core.editor",
        "core.fsmonitor",
        "diff.d.command",
        "diff.d.textconv",
        "merge.m.driver",
        "filter.f.clean",
        "filter.f.smudge",
        "filter.f.process",
        "gpg.program",
        "gpg.x509.program",
        "sequence.editor",
        "pager.log",
        "alias.co",
    )

    @staticmethod
    def _dash_c_value(key: str, value: str) -> str:
        return f"git -c '{key}={value}' fetch https://example.com/x"

    @staticmethod
    def _numbered_env_value(key: str, value: str) -> str:
        return (
            f"GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0='{key}'"
            f" GIT_CONFIG_VALUE_0='{value}' git fetch https://example.com/x"
        )

    @pytest.mark.parametrize(
        "value", ["!ssh localhost id", "ssh localhost id"], ids=["bang", "shell"]
    )
    @pytest.mark.parametrize("arrival", ["dash_c", "numbered_env"])
    @pytest.mark.parametrize("key", _EXECUTED_CONFIG_KEYS)
    def test_executed_config_value_dialing_self_is_refused(self, key, arrival, value):
        build = self._dash_c_value if arrival == "dash_c" else self._numbered_env_value
        cmd = build(key, value)
        # The carried ``ssh localhost id`` is reported under the ssh floor's row.
        assert _denied_by(cmd) == self._SSH_RULE, cmd

    # The ``GIT_SSH`` / ``GIT_SSH_COMMAND`` / ``GIT_PROXY_COMMAND`` transport
    # commands are the same ``!``/shell pair, delivered only through the
    # environment.
    @pytest.mark.parametrize(
        "value", ["!ssh localhost id", "ssh localhost id"], ids=["bang", "shell"]
    )
    @pytest.mark.parametrize("name", ["GIT_SSH_COMMAND", "GIT_PROXY_COMMAND", "GIT_SSH"])
    def test_executed_ssh_env_command_dialing_self_is_refused(self, name, value):
        cmd = f"{name}='{value}' git fetch https://example.com/x"
        assert _denied_by(cmd) == self._SSH_RULE, cmd

    # A ``url.<base>.insteadOf`` / ``pushInsteadOf`` rewrite whose base is an
    # ``ext::`` command dials this host when that command does (the ssh/scp
    # bases are covered by ``test_url_rewrite_to_self_is_denied``); a base to a
    # remote host stays allowed.
    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            ("git -c 'url.ext::ssh localhost id.insteadOf=ex:' fetch ex:repo", _SSH_RULE),
            (
                "git -c 'url.ext::ssh localhost id.pushInsteadOf=ex:' push ex:repo feat",
                _SSH_RULE,
            ),
            ("git -c 'url.ext::ssh far.example.com id.insteadOf=ex:' fetch ex:repo", None),
        ],
        ids=[
            "ext-insteadof-self",
            "ext-pushinsteadof-self",
            "ext-insteadof-remote",
        ],
    )
    def test_url_rewrite_ext_base_dialing_self_is_refused(self, cmd, rule):
        assert _denied_by(cmd) == rule, cmd

    # Controls: an executed value git runs that does NOT dial this host stays
    # allowed, including a bare ``!`` helper and a ``!``-prefixed command naming
    # a remote host (the ``!`` strip must not read a far host as self).
    @pytest.mark.parametrize(
        "cmd",
        [
            "git -c 'credential.helper=!echo hi' fetch https://example.com/x",
            "git -c credential.helper=store fetch https://example.com/x",
            "git -c core.pager=less fetch https://example.com/x",
            "git -c 'credential.helper=!ssh far.example.com id' fetch https://example.com/x",
            "git -c 'core.sshCommand=!ssh far.example.com id' fetch far.example.com:x",
            (
                "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=credential.helper"
                " GIT_CONFIG_VALUE_0='!echo hi' git fetch https://example.com/x"
            ),
        ],
        ids=[
            "bang-echo",
            "builtin-store",
            "pager-less",
            "bang-ssh-remote",
            "sshcommand-bang-remote",
            "numbered-bang-echo",
        ],
    )
    def test_executed_config_controls_stay_allowed(self, cmd):
        assert _denied_by(cmd) is None, cmd

    @pytest.fixture
    def work_meter(self, monkeypatch):
        """Count the two work units the quadratic paths inflate.

        ``following`` sums ``len(following)`` over every ``_git_ext_command``
        payload read -- the ext-payload operands one ``ext::`` read consumes,
        which the ext bound keeps at the next ext operand.  ``reentries`` counts
        dispatcher re-entries (``deny_gate._reentered_floor_hit``), which the
        re-entry memo collapses for a command many invocations carry.
        ``reentered_chars`` sums the length of the command text each of those
        re-entries receives, which grows with every copy of an environment a
        rebuilt command carries.  All are exact, host-independent counts: no
        wall clock, no coverage sensitivity.
        """
        from kiro_crew.security import deny_gate as _deny_gate

        counts = {"following": 0, "reentries": 0, "reentered_chars": 0, "tar_letters": 0}
        real_ext = _argv_floor._git_ext_command
        real_hit = _deny_gate._reentered_floor_hit

        class _CountedLetters(frozenset):
            def __contains__(self, letter):
                counts["tar_letters"] += 1
                return frozenset.__contains__(self, letter)

        def ext(operand, following):
            counts["following"] += len(following)
            return real_ext(operand, following)

        def hit(command):
            counts["reentries"] += 1
            counts["reentered_chars"] += len(command)
            return real_hit(command)

        monkeypatch.setattr(_argv_floor, "_git_ext_command", ext)
        monkeypatch.setattr(_deny_gate, "_reentered_floor_hit", hit)
        monkeypatch.setattr(
            _argv_floor,
            "_TAR_UPPER_VALUE_SHORTS",
            _CountedLetters(_argv_floor._TAR_UPPER_VALUE_SHORTS),
        )

        class _Meter:
            def reset(self):
                counts["following"] = 0
                counts["reentries"] = 0
                counts["reentered_chars"] = 0
                counts["tar_letters"] = 0

            @property
            def following(self):
                return counts["following"]

            @property
            def reentries(self):
                return counts["reentries"]

            @property
            def reentered_chars(self):
                return counts["reentered_chars"]

            @property
            def tar_letters(self):
                return counts["tar_letters"]

        return _Meter()

    # An old-style tar bundle is read once per possible archive ``f``: each
    # reading carries on from the letters before it, so the bundle letters
    # read stay a small multiple of the bundle's length.  Re-reading every
    # prefix for each ``f`` makes the letters read grow with the square of
    # the length.  ``$(true)`` keeps the both-case reading on.  The count is
    # exact, with no wall clock.
    def test_long_tar_bundle_is_read_in_linear_work(self, work_meter):
        for n in (500, 2000):
            work_meter.reset()
            is_denied("tar " + "f" * n + " $(true)")
            assert work_meter.tar_letters <= 8 * n, (n, work_meter.tar_letters)

    # A frame's glob words are read once, however many git or tar commands the
    # frame holds.  Reading them again for each command re-reads the whole frame
    # per command, so the work grows with the square of the frame.  The floor
    # reads the whole command twice (main's unsplit boundaries, then the
    # glued-split boundaries), each an independent pass, so the healthy count is
    # one per pass: two, a constant that does not grow with the 200 commands.
    # A quadratic re-read drives it far past two.  The count is exact, no wall clock.
    def test_frame_glob_words_are_read_once_per_frame(self, monkeypatch):
        calls = {"n": 0}
        real = _argv_floor._glob_word_spellings

        def counted(source):
            calls["n"] += 1
            return real(source)

        monkeypatch.setattr(_argv_floor, "_glob_word_spellings", counted)
        assert is_denied("tar -cf o d; " * 200 + "git fetch o") is None
        assert calls["n"] == 2, calls["n"]

    # Each of n pure ``ext::`` operands bounds its payload to the NEXT ext
    # operand, so no operand re-reads the tail: the ext-payload words read stay
    # at zero however many operands there are.  Restoring the per-operand
    # re-read of every later operand (the quadratic ``ext::`` path) makes each
    # operand decode the whole tail, so the payload words read go O(n^2) and the
    # command is refused once the carried-read budget trips.  Reading the work
    # count and the ``None`` verdict catches both, with no wall clock.
    def test_many_ext_operands_are_read_in_linear_work(self, work_meter):
        for n in (1000, 2000):
            cmd = "git clone " + " ".join(["ext::x"] * n)
            work_meter.reset()
            verdict = is_denied(cmd)
            assert verdict is None, (n, verdict)
            # The ext-payload words read never exceed the operand count; the
            # quadratic restore drives this past it (then to the budget cap).
            assert work_meter.following <= len(cmd.split()), (n, work_meter.following)

    #: Each worst case is read with its re-entry work bounded.  A repeated or
    #: carried command re-enters the dispatcher a bounded number of times (the
    #: re-entry memo collapses a command many invocations carry); the largest
    #: healthy case re-enters 35 times, well under this ceiling.  Restoring a
    #: quadratic path drives a case that must be ALLOW to the carried-read
    #: budget and so to a DENY verdict -- caught by the verdict check below --
    #: while this ceiling catches an unbounded re-entry runaway.  No wall clock.
    _REENTRY_WORK_CEILING = 128
    _GLUED = " -o" + "a" * 250

    # Each command stays under ``MAX_SCANNABLE_COMMAND_CHARS`` and every git or
    # tar argv in it under ``_GIT_TAR_WORD_CAP``.  The ``denied`` cases are
    # refused because the read passes ``_CARRIED_READ_CHAR_BUDGET`` (fail closed).
    @pytest.mark.parametrize(
        ("cmd", "denied"),
        [
            ("git" + " -c alias.x=status" * 1000 + " x", False),
            ("git" + "".join(f" -c alias.x=s{i}" for i in range(1000)) + " x", False),
            ("git" + " -c 'alias.x=!echo'" * 900 + " x", False),
            ("X=status git" + " --config-env=alias.x=X" * 800 + " x", False),
            ('GIT_CONFIG_PARAMETERS="' + " 'alias.x=status'" * 1000 + '" git x', False),
            (
                "".join(
                    f"GIT_CONFIG_KEY_{i}=alias.x GIT_CONFIG_VALUE_{i}=s{i} " for i in range(380)
                )
                + "git x",
                True,
            ),
            ("git x" + "".join(f" -c alias.x=s{i}" for i in range(1000)), False),
            ("git clone" + " ext::x" * 2800, False),
            ("git clone ext::x" + " --u=ext::x" * 1800, False),
            ("git" + " -c core.sshCommand=ssh" * 800 + " fetch origin", False),
            ("export GIT_SSH_COMMAND='ssh" + _GLUED * 30 + "'; " + "git fetch o; " * 800, False),
            ("tar -cf out.tar" + " --rsh-command=ssh" * 1000 + " dir", False),
            ("git clone" + " --branch=a" * 1800 + " https://github.com/x/y", False),
            ("git clone" + " --upload-pack=x" * 1200 + " /local/repo", False),
            (
                "".join(f"export TAR_OPTIONS=-v{i}; " for i in range(350))
                + "tar -cf out.tar"
                + " a" * 2400,
                False,
            ),
            ("export TAR_OPTIONS='" + " -v" * 1500 + "'; " + "tar -cf o d; " * 600, False),
            ("export TAR_OPTIONS='--label=" + "a" * 9000 + "'; " + "tar; " * 2200, False),
            (
                'export GIT_CONFIG_PARAMETERS="'
                + " 'a.b=c'" * 1100
                + '"; '
                + "git fetch o; " * 750,
                False,
            ),
            (
                'export GIT_CONFIG_PARAMETERS="' + " 'a.b=c'" * 1000 + '"; ' + "git x; " * 1500,
                False,
            ),
            (
                "".join(f"export X=s{i}; " for i in range(500))
                + "git"
                + "".join(f" --config-env=k{i}=X" for i in range(500))
                + " fetch origin",
                False,
            ),
            (
                "".join(
                    f"export GIT_CONFIG_KEY_0=k{i}; export GIT_CONFIG_VALUE_0=v{i}; "
                    for i in range(150)
                )
                + "git fetch o; " * 700,
                False,
            ),
            ("export A='" + _GLUED * 30 + "'; " + "git -c alias.x=status x; " * 450, False),
            (
                "export A='"
                + _GLUED * 30
                + "'; "
                + "".join(f"git -c alias.x={i} x; " for i in range(450)),
                True,
            ),
            (
                "export A='"
                + _GLUED * 30
                + "'; "
                + "".join(f"git -c 'alias.x=!echo {i}' x; " for i in range(400)),
                True,
            ),
        ],
        ids=[
            "repeated-alias",
            "redefined-alias",
            "repeated-shell-alias",
            "repeated-config-env-alias",
            "repeated-parameters-alias",
            "numbered-alias-definitions",
            "post-verb-alias-definitions",
            "ext-operands",
            "ext-option-values",
            "repeated-ssh-command-config",
            "exported-ssh-command-many-gits",
            "repeated-tar-rsh-command",
            "attached-branch",
            "attached-upload-pack",
            "tar-options-values",
            "exported-tar-options-many-tars",
            "exported-long-tar-options-many-tars",
            "exported-parameters-many-gits",
            "exported-parameters-many-alias-lookups",
            "config-env-name-values",
            "exported-numbered-product-many-gits",
            "exported-env-same-alias-many-gits",
            "exported-env-distinct-aliases",
            "exported-env-distinct-shell-aliases",
        ],
    )
    def test_worst_case_reads_finish_in_bounded_work(self, cmd, denied, work_meter):
        from kiro_crew.security.paths import MAX_SCANNABLE_COMMAND_CHARS

        assert len(cmd) <= MAX_SCANNABLE_COMMAND_CHARS, len(cmd)
        assert len(cmd.split()) <= _argv_floor._GIT_TAR_WORD_CAP, len(cmd.split())
        work_meter.reset()
        verdict = is_denied(cmd)
        assert (verdict is not None) is denied
        assert work_meter.reentries <= self._REENTRY_WORK_CEILING, work_meter.reentries

    # Every later git/tar word of one command is another place a git or tar
    # argv starts, and each is read to the command's end, so n repeated ``git``
    # words cost about n^2/2 word reads.  Past ``_GIT_TAR_WORD_CAP`` words read
    # in total, a command that carries a self-dial signal is refused unread
    # (fail closed); one that carries none (``git git ... status`` names no
    # host) reads each later start only up to the next.  Counting the per-word
    # git/tar check keeps it exact, with no wall clock.
    @pytest.mark.parametrize(
        ("cmd", "denied"),
        [
            ("git " * 2000 + "status", False),
            ("git " * 2000 + "clone localhost:x", True),
            ("git status && git log && git diff", False),
            ("parallel git fetch ::: a b c", False),
            ("git add " + " ".join(f"f{i}" for i in range(1, 201)), False),
        ],
        ids=[
            "repeated-git-words",
            "repeated-git-words-self",
            "chained-gits",
            "parallel-fetch",
            "add-200-files",
        ],
    )
    def test_git_tar_words_read_in_one_command_are_bounded(self, cmd, denied, monkeypatch):
        from kiro_crew.security import argv_floor_git_tar as _git_tar

        calls = {"n": 0}
        real = _git_tar._value_runs_git_or_tar

        def counted(value):
            calls["n"] += 1
            return real(value)

        monkeypatch.setattr(_git_tar, "_value_runs_git_or_tar", counted)
        verdict = _denied_by(cmd)
        assert (verdict == self._RULE) if denied else verdict is None, (cmd[:40], verdict)
        assert calls["n"] <= _argv_floor._GIT_TAR_WORD_CAP, calls["n"]

    # git reads the numbered environment config, then ``GIT_CONFIG_PARAMETERS``
    # (the inherited value, then this invocation's own ``-c`` / ``--config-env``
    # options), and runs the LAST ``alias.<verb>`` it read.  Each pair is one
    # shape twice: a later harmless definition is allowed, and the same two
    # definitions with the self-dialing one last are refused.
    _LAST_ALIAS_DEFINITION_PAIRS = [
        (
            "c-then-c",
            "git -c alias.x='ls-remote localhost:x' -c alias.x=status x",
            "git -c alias.x=status -c alias.x='ls-remote localhost:x' x",
        ),
        (
            "config-env-then-c",
            "X='ls-remote localhost:x' git --config-env=alias.x=X -c alias.x=status x",
            "X='ls-remote localhost:x' git -c alias.x=status --config-env=alias.x=X x",
        ),
        (
            "parameters-then-c",
            "GIT_CONFIG_PARAMETERS=\"'alias.x=ls-remote localhost:x'\" git -c alias.x=status x",
            "GIT_CONFIG_PARAMETERS=\"'alias.x=status'\" git -c alias.x='ls-remote localhost:x' x",
        ),
        (
            "parameter-then-parameter",
            "GIT_CONFIG_PARAMETERS=\"'alias.x=ls-remote localhost:x' 'alias.x=status'\" git x",
            "GIT_CONFIG_PARAMETERS=\"'alias.x=status' 'alias.x=ls-remote localhost:x'\" git x",
        ),
        (
            "numbered-then-c",
            "GIT_CONFIG_KEY_0=alias.x GIT_CONFIG_VALUE_0='ls-remote localhost:x' "
            "git -c alias.x=status x",
            "GIT_CONFIG_KEY_0=alias.x GIT_CONFIG_VALUE_0=status "
            "git -c alias.x='ls-remote localhost:x' x",
        ),
        (
            "loop-then-c",
            "git -c alias.a=a -c alias.a=status a",
            "git -c alias.a=status -c alias.a=a a",
        ),
    ]

    @pytest.mark.parametrize(
        ("cmd", "denied"),
        [
            *(
                pytest.param(cmd, denied, id=f"{name}-{'reversed' if denied else 'allowed'}")
                for name, allowed, reversed_ in _LAST_ALIAS_DEFINITION_PAIRS
                for cmd, denied in ((allowed, False), (reversed_, True))
            ),
            pytest.param(
                "GIT_CONFIG_KEY_0=alias.x GIT_CONFIG_VALUE_0='ls-remote localhost:x' "
                "GIT_CONFIG_KEY_1=alias.x GIT_CONFIG_VALUE_1=status git x",
                True,
                id="every-numbered-definition-is-read",
            ),
            pytest.param(
                "git -c alias.x=status x -c alias.x='ls-remote localhost:x'",
                True,
                id="post-verb-definition-is-read",
            ),
            # ``x`` is also read as typed (a ``git-x`` runs first), so a ``-c``
            # value that dials this host refuses whichever definition is last.
            pytest.param(
                "git -c 'alias.x=!ssh localhost' -c alias.x=status x",
                True,
                id="shell-then-c-read-as-typed",
            ),
        ],
    )
    def test_the_last_alias_definition_git_reads_decides(self, cmd, denied):
        assert security._is_git_tar_to_self(cmd) is denied, cmd
        if denied:
            assert _denied_by(cmd) in self._DIAL_RULES, cmd
        else:
            assert _denied_by(cmd) is None, cmd

    def test_a_read_past_the_carried_character_budget_is_refused(self, monkeypatch):
        cmd = "git -c alias.x=status x; git -c alias.y=status y"
        assert not security._is_git_tar_to_self(cmd)
        # The first rebuilt alias command fits, the second one does not.
        first = len("git -c alias.x=status status")
        monkeypatch.setattr(_argv_floor, "_CARRIED_READ_CHAR_BUDGET", first)
        assert security._is_git_tar_to_self(cmd)

    def test_a_git_argv_past_the_word_cap_is_refused_unread(self):
        cap = _argv_floor._GIT_TAR_WORD_CAP
        # ``origin`` and the ``a`` refspecs: exactly the cap, then one word past it.
        assert _denied_by("git fetch origin" + " a" * (cap - 1)) is None
        assert _denied_by("git fetch origin" + " a" * cap) == self._RULE

    def test_a_command_value_past_the_word_cap_is_refused_unread(self):
        cap = _argv_floor._GIT_TAR_WORD_CAP
        value = "ssh" + " a" * (cap - 1)
        assert _argv_floor._value_names_literal_self(value) is False
        assert _argv_floor._value_names_literal_self(value + " a") is None
        remote = " git fetch far.example.com:x"
        assert _denied_by(f"GIT_SSH_COMMAND='{value}'" + remote) is None
        assert _denied_by(f"GIT_SSH_COMMAND='{value} a'" + remote) == self._RULE

    @pytest.mark.parametrize(
        "cmd",
        [
            "GIT_PROXY_COMMAND='ssh localhost' git ls-remote git://far.example.com/repo",
            "git -c core.gitProxy='ssh localhost' ls-remote git://far.example.com/repo",
        ],
        ids=["environment", "config"],
    )
    def test_git_proxy_commands_to_self_are_denied(self, cmd):
        assert _denied_by(cmd) == self._SSH_RULE, cmd

    def test_invoked_shell_alias_to_self_is_denied(self):
        assert _denied_by("git -c alias.x='!ssh localhost' x") == self._SSH_RULE

    def test_uninvoked_shell_alias_to_self_is_allowed(self):
        cmd = "git -c alias.x='!ssh localhost' status"
        assert _denied_by(cmd) not in self._DIAL_RULES

    # git runs a ``!`` alias as ``sh -c '<body> "$@"' '<body>' <operands>``, so
    # the body's positionals name the operands.
    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            ("git -c alias.f='!ssh $1' f localhost", _SSH_RULE),
            ("git -c alias.f='!ssh \"$@\"' f localhost", _SSH_RULE),
            ("git -c alias.f='!ssh ${1}' f localhost", _SSH_RULE),
            ("git -c alias.f='!ssh $*' f localhost", _SSH_RULE),
            ("git -c alias.f='!ssh $1' f far.example.com", None),
            ("git -c alias.f='!ssh \"$@\"' f far.example.com", None),
            ("git -c alias.f='!echo $1' f localhost", None),
        ],
        ids=["dollar-1", "at", "braced", "star", "remote", "remote-at", "echo-control"],
    )
    def test_shell_alias_positionals_bind_the_operands(self, cmd, rule):
        assert _denied_by(cmd) == rule, cmd

    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            ("git -c alias.x='ls-remote localhost:x' x", _RULE),
            ("X='ls-remote localhost:x' git --config-env=alias.x=X x", _RULE),
            (
                "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.x "
                "GIT_CONFIG_VALUE_0='ls-remote localhost:x' git x",
                _RULE,
            ),
            (
                "GIT_CONFIG_PARAMETERS=\"'alias.x=ls-remote localhost:x'\" git x",
                _RULE,
            ),
            ("git -c alias.x='!ssh localhost' x", _SSH_RULE),
            ("git -c alias.x='status' x", None),
            ("git -c alias.x='ls-remote far.example.com:x' x", None),
            ("git -c alias.x='ls-remote localhost:x' status", None),
            (
                "git -c alias.x='ssh localhost' ls-remote far.example.com:x",
                _SSH_RULE,
            ),
        ],
        ids=[
            "c-ordinary-self",
            "config-env-ordinary-self",
            "numbered-env-ordinary-self",
            "parameters-ordinary-self",
            "shell-self-control",
            "local-control",
            "remote-control",
            "ordinary-unused-control",
            "unused-alias-over-refusal-residual",
        ],
    )
    def test_invoked_ordinary_aliases_are_inspected(self, cmd, rule):
        # The final case pins the packaged key-agnostic command-value residual:
        # a transport invocation still inspects an unused alias value as a command.
        assert _denied_by(cmd) == rule, cmd

    # git runs a ``git-<verb>`` program (``git-subtree``) before an alias of its
    # name, so a verb outside both verb tables is read as typed too: an alias
    # reading that dials nothing does not hide the command's own operands.
    @pytest.mark.parametrize(
        ("cmd", "denied"),
        [
            ("git -c alias.subtree=status subtree pull --prefix=lib localhost:x main", True),
            (
                "S=status git --config-env=alias.subtree=S subtree pull --prefix=lib "
                "localhost:x main",
                True,
            ),
            ("git -c alias.foo=status foo localhost:x", True),
            (
                "GIT_SSH_COMMAND='ssh localhost' git -c alias.subtree=status subtree pull "
                "--prefix=lib far:x main",
                True,
            ),
            ("git -c alias.st=status st", False),
            ("git -c alias.lg='log --oneline' lg", False),
            (
                "git -c alias.subtree=status subtree pull --prefix=lib "
                "https://github.com/a/b main",
                False,
            ),
        ],
    )
    def test_alias_of_an_unlisted_verb_is_also_read_as_typed(self, cmd, denied):
        assert security._is_git_tar_to_self(cmd) is denied, cmd
        assert (_denied_by(cmd) in self._DIAL_RULES) is denied, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "GIT_CONFIG=/work/config git ls-remote far:x",
            "GIT_CONFIG_GLOBAL=/work/config git ls-remote far:x",
            "GIT_CONFIG_SYSTEM=/work/config git ls-remote far:x",
            "git -c include.path=/work/config ls-remote far:x",
            "GIT_DIR=/work/repo/.git git ls-remote far:x",
            "GIT_COMMON_DIR=/work/repo/.git git ls-remote far:x",
            "HOME=/work/home XDG_CONFIG_HOME=/work/config git ls-remote far:x",
        ],
        ids=[
            "config",
            "global",
            "system",
            "include-path",
            "git-dir",
            "common-dir",
            "home-and-xdg",
        ],
    )
    def test_file_backed_config_channels_remain_residual(self, cmd):
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "git -c protocol.foo.allow=always ls-remote foo::localhost",
            "git -c remote.x.vcs=foo ls-remote x",
            "GIT_EXEC_PATH=/work/helpers git ls-remote foo::localhost",
        ],
        ids=["custom-scheme", "remote-vcs", "exec-path"],
    )
    def test_remote_helper_indirection_remains_residual(self, cmd):
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    def test_transport_wrapper_indirection_remains_residual(self):
        cmd = "GIT_SSH=/work/self-routing-wrapper git ls-remote far:x"
        assert _denied_by(cmd) not in self._DIAL_RULES

    @pytest.mark.parametrize(
        "cmd",
        [
            "GIT_SSH_VARIANT=ssh git ls-remote far:x",
            "git -c ssh.variant=ssh ls-remote far:x",
        ],
        ids=["environment", "config"],
    )
    def test_ssh_variant_does_not_choose_a_target(self, cmd):
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "GIT_ALLOW_PROTOCOL=ext git ls-remote ext::id",
            "GIT_PROTOCOL_FROM_USER=1 git ls-remote ext::id",
        ],
        ids=["allow-protocol", "from-user"],
    )
    def test_protocol_permission_does_not_hide_the_target(self, cmd):
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    def test_opt_out_disables_both_tiers(self):
        effective = compute_effective_denied(BUILTIN_DENIED_RULES, (self._RULE,), False, (), ())
        assert is_denied("git clone localhost:x", denied_regexes=list(effective)) is None
        assert is_denied("tar -cf localhost:a.tar dir", denied_regexes=list(effective)) is None

    # Every command a git/tar invocation runs, and the git invocation an invoked
    # alias becomes, is re-entered through the whole argv-floor dispatcher.
    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            # An ordinary alias keeps the invocation's own operands and config.
            ("git -c alias.c=clone c localhost:x", _RULE),
            ("git -c alias.f=fetch f localhost:x", _RULE),
            (
                "git -c alias.probe='ls-remote selftarget' "
                "-c remote.selftarget.url=localhost:/srv/repo probe",
                _RULE,
            ),
            ("GIT_SSH_COMMAND='ssh localhost' git -c alias.f=fetch f origin", _SSH_RULE),
            ("git -c alias.c='clone ssh://local%68ost/x' c", _RULE),
            ("git -c alias.x='ls-remote localhost:x' x", _RULE),
            ("git -c alias.x='status' x", None),
            # A ``git-s`` program would run before the alias, with this environment.
            ("GIT_SSH_COMMAND='ssh localhost' git -c alias.s=status s", _SSH_RULE),
            ("git -c alias.c=clone c far.example.com:x", None),
            ("git -c alias.x='ls-remote localhost:x' status", None),
            # A ``!`` alias runs its shell command with the operands appended and
            # git's ``-c`` config in the child's environment.
            ("git -c 'alias.c=!git clone localhost:x' c", _RULE),
            ("git -c 'alias.c=!git clone' c localhost:x", _RULE),
            ("git -c core.sshCommand='ssh localhost' -c 'alias.x=!git fetch origin' x", _SSH_RULE),
            ("git -c core.sshCommand='ssh localhost' -c 'alias.x=!git status' x", _SSH_RULE),
            ("git -c 'alias.c=!git clone far.example.com:x' c", None),
            # Nested aliases resolve, through either kind.
            ("git -c alias.a=b -c alias.b=c -c alias.c='ls-remote localhost:x' a", _RULE),
            ("git -c 'alias.a=!git b' -c alias.b='ls-remote localhost:x' a", _RULE),
            ("git -c alias.a=b -c alias.b=c -c alias.c=status a", None),
        ],
        ids=[
            "verb-plus-operand-clone",
            "verb-plus-operand-fetch",
            "alias-plus-remote-url-config",
            "alias-plus-ssh-command-env",
            "alias-encoded-host",
            "operand-in-body",
            "local-control",
            "env-unlisted-verb-read-as-typed",
            "remote-operand-control",
            "unused-alias-residual-control",
            "shell-alias-git-clone",
            "shell-alias-operand",
            "shell-alias-inherits-config",
            "shell-alias-unlisted-verb-read-as-typed",
            "shell-alias-remote-control",
            "nested-ordinary",
            "nested-shell-to-ordinary",
            "nested-local-control",
        ],
    )
    def test_invoked_alias_is_rebuilt_and_reentered(self, cmd, rule):
        assert security._is_git_tar_to_self(cmd) is (rule is not None), cmd
        assert _denied_by(cmd) == rule, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "git -c alias.a=a a",
            "git -c alias.a=b -c alias.b=a a",
            "git -c alias.a='a x' a",
        ],
        ids=["self", "two-cycle", "growing"],
    )
    def test_alias_cycle_is_refused(self, cmd):
        # git rejects an alias loop; the floor refuses it rather than unroll it.
        assert _denied_by(cmd) == self._RULE, cmd

    def test_alias_chain_past_the_reentry_limit_is_refused(self):
        def chain(length: int) -> str:
            links = [f"-c alias.a{n}=a{n + 1}" for n in range(length - 1)]
            return "git " + " ".join([*links, f"-c alias.a{length - 1}=status", "a0"])

        within = chain(_argv_floor._FUNCTION_BIND_CAP)
        assert not security._is_git_tar_to_self(within), within
        beyond = chain(_argv_floor._FUNCTION_BIND_CAP + 1)
        assert security._is_git_tar_to_self(beyond), beyond

    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            ("git ls-remote --upload-pack='git ls-remote localhost:/srv/repo' .", _RULE),
            ("git clone --upload-pack='ssh localhost id' /local/repo", _SSH_RULE),
            ("git ls-remote --receive-pack='git ls-remote localhost:x' .", _RULE),
            ("git fetch -u 'git ls-remote localhost:x' origin", _RULE),
            ("git archive --remote=far:x --exec='git ls-remote localhost:x' HEAD", _RULE),
            ("git ls-remote --upload-pack='git clone git+ssh://localhost/x' .", _RULE),
            ("git ls-remote --upload-pack='git clone [localhost:22]:x' .", _RULE),
            (
                "git ls-remote --upload-pack='git archive --remote=ssh://local%68ost/x HEAD' .",
                _RULE,
            ),
            ("git ls-remote --upload-pack=\"git clone 'ext::ssh localhost id'\" .", _SSH_RULE),
            (
                'git ls-remote --upload-pack="git ls-remote '
                "'ext::ssh -oProxyCommand=ssh% localhost% id far.example.com repo'\" .",
                _SSH_RULE,
            ),
            ("GIT_SSH_COMMAND='git ls-remote localhost:x' git fetch origin", _RULE),
            ("GIT_PROXY_COMMAND='git ls-remote localhost:x' git fetch git://far/x", _RULE),
            ("git -c core.gitProxy='git ls-remote localhost:x' fetch git://far/x", _RULE),
            ("git clone 'ext::git ls-remote localhost:x'", _RULE),
            ("tar --rsh-command='git ls-remote localhost:x' -cf far:a.tar dir", _RULE),
            ("git ls-remote --upload-pack='git ls-remote far.example.com:x' .", None),
            ("git ls-remote --upload-pack='git-upload-pack' .", None),
            (
                'git ls-remote --upload-pack="git ls-remote '
                "'ext::ssh -oProxyCommand=ssh% bastion.example.com far.example.com repo'\" .",
                None,
            ),
            ("GIT_SSH_COMMAND='git ls-remote far.example.com:x' git fetch origin", None),
        ],
        ids=[
            "upload-pack-git",
            "upload-pack-ssh",
            "receive-pack-git",
            "short-u-git",
            "archive-exec-git",
            "upload-pack-git-ssh-scheme",
            "upload-pack-bracketed",
            "upload-pack-encoded-option-value",
            "upload-pack-wraps-ext",
            "upload-pack-wraps-ext-escaped-spaces",
            "ssh-command-env-git",
            "proxy-command-env-git",
            "git-proxy-config-git",
            "ext-git",
            "tar-rsh-command-git",
            "upload-pack-remote-control",
            "upload-pack-program-control",
            "upload-pack-wraps-ext-remote-control",
            "ssh-command-env-remote-control",
        ],
    )
    def test_command_values_reenter_every_floor(self, cmd, rule):
        assert security._is_git_tar_to_self(cmd) is (rule is not None), cmd
        assert _denied_by(cmd) == rule, cmd

    def test_command_values_reach_floors_beyond_ssh_and_git(self, monkeypatch):
        # The re-entry is the whole dispatcher, not a hand-picked pair: a floor
        # that refuses the carried command refuses its carrier too.
        cmd = "git ls-remote --upload-pack='probe-marker localhost' ."
        assert not security._is_git_tar_to_self(cmd), cmd
        monkeypatch.setattr(security, "_is_self_restart", lambda text: "probe-marker" in text)
        assert security._is_git_tar_to_self(cmd), cmd

    @pytest.mark.parametrize(
        ("cmd", "denied"),
        [
            ("git ls-remote --upload-pack='tar cLf 1024 localhost:a.tar d' .", True),
            ("git ls-remote --upload-pack='tar cLf 1024 out.tar localhost:file' .", False),
        ],
        ids=["self-archive", "self-shaped-member"],
    )
    def test_a_carried_tar_bundle_is_read_in_the_submitted_case(self, cmd, denied):
        # A re-entered carried command is cut from the lowered text; its tar
        # bundle is still read in the case the outermost command was submitted in.
        assert (_denied_by(cmd) == self._RULE) is denied, cmd
        assert security._is_git_tar_to_self(cmd.lower(), raw_text=cmd) is denied, cmd

    def test_a_carried_command_no_floor_can_read_is_refused(self, monkeypatch):
        def unreadable(_text, **_kwargs):
            raise RuntimeError("floor exploded")

        cmd = "git ls-remote --upload-pack='localhost run' ."
        assert not security._is_git_tar_to_self(cmd), cmd
        monkeypatch.setattr(security, "_is_self_kill", unreadable)
        assert security._is_git_tar_to_self(cmd), cmd

    @pytest.mark.parametrize(
        ("chain", "alias", "command"),
        [
            ((0, frozenset({"a"}), frozenset()), "a", "git status"),
            ((0, frozenset(), frozenset({"git status"})), None, "git status"),
            ((_argv_floor._FUNCTION_BIND_CAP, frozenset(), frozenset()), None, "git status"),
        ],
        ids=["alias-on-chain", "command-on-chain", "depth-at-limit"],
    )
    def test_reentry_refuses_a_loop_or_overdeep_chain(self, chain, alias, command):
        assert not _argv_floor._reenter_floors(command, alias=alias)
        previous = _argv_floor._REENTRY_CHAIN.get()
        _argv_floor._REENTRY_CHAIN.set(chain)
        try:
            assert _argv_floor._reenter_floors(command, alias=alias)
        finally:
            _argv_floor._REENTRY_CHAIN.set(previous)

    # A carried command is read with the operator's row set, the way a whole
    # command is: a row turned off stays off for the commands git or tar runs,
    # and the rows still on keep refusing.
    @pytest.mark.parametrize(
        ("off", "cmd", "rule"),
        [
            (_SSH_RULE, "GIT_SSH_COMMAND='ssh localhost' git fetch origin", None),
            (_SSH_RULE, "git -c alias.x='!ssh localhost' x", None),
            (_SSH_RULE, "GIT_SSH_COMMAND='ssh localhost' git -c alias.f=fetch f origin", None),
            (
                "self-protection-kill",
                "git ls-remote --upload-pack='pkill -f kirocrew localhost' .",
                None,
            ),
            (_SSH_RULE, "git ls-remote --upload-pack='git ls-remote localhost:x' .", _RULE),
        ],
        ids=["ssh-env", "ssh-shell-alias", "ssh-ordinary-alias", "kill-program", "git-tar-control"],
    )
    def test_a_disabled_row_stays_off_through_reentry(self, off, cmd, rule):
        assert _denied_by(cmd) == (rule or off), cmd
        effective = list(compute_effective_denied(BUILTIN_DENIED_RULES, (off,), False, (), ()))
        verdict = is_denied(cmd, denied_regexes=effective)
        if rule is None:
            assert verdict is None, cmd
        else:
            assert verdict is not None and _rule_pattern(rule) in verdict.splitlines()[0], cmd

    # A hit found by re-entering a carried command is reported under the row that
    # matched it -- its pattern, its structural note, its diagnostic and its audit
    # event -- not under the git/tar row that carried it.  The top-level floors of
    # those rows do not refuse these commands, so the id comes from the re-entry.
    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            ("GIT_SSH_COMMAND='ssh localhost' git fetch origin", _SSH_RULE),
            ("git -c alias.x='!ssh localhost' x", _SSH_RULE),
            (
                "git ls-remote --upload-pack='pkill -f kirocrew localhost' .",
                "self-protection-kill",
            ),
            ("git ls-remote --upload-pack='git ls-remote localhost:x' .", _RULE),
        ],
        ids=["ssh-env", "ssh-shell-alias", "kill-program", "git-tar-itself"],
    )
    def test_a_reentered_hit_reports_the_inner_row(self, cmd, rule, monkeypatch):
        lower = cmd.lower()
        assert not security._is_ssh_to_self(lower) and not security._is_self_kill(lower), cmd
        events: "list[str]" = []
        monkeypatch.setattr(
            security, "_emit_deny_event", lambda _tool, pattern, *_a, **_k: events.append(pattern)
        )
        verdict = is_denied(cmd)
        assert verdict is not None, cmd
        assert verdict.splitlines()[0].endswith(_rule_pattern(rule)), cmd
        assert security._SELF_PROTECTION_FLOOR_NOTES[rule] in verdict, cmd
        assert f"rule={rule} component=argv-floor" in verdict, cmd
        assert events == [_rule_pattern(rule)], cmd

    # An attached ``--opt=value`` is read exactly like the detached ``--opt value``.
    @pytest.mark.parametrize(
        ("template", "value", "denied"),
        [
            ("git push {opt} feat", "localhost:x", True),
            ("git push {opt} feat", "far.example.com:x", False),
            ("git ls-remote {opt} .", "git ls-remote localhost:x", True),
            ("git ls-remote {opt} .", "git ls-remote far.example.com:x", False),
            ("git send-pack {opt} far:x", "git ls-remote localhost:x", True),
            ("git archive {opt} HEAD", "localhost:x", True),
            ("git archive {opt} HEAD", "github.com:x/y", False),
            ("git clone {opt} https://github.com/x/y", "localhost:x", True),
            ("git clone {opt} https://github.com/x/y", "/srv/ref", False),
        ],
        ids=[
            "repo-self",
            "repo-remote",
            "upload-pack-self",
            "upload-pack-remote",
            "receive-pack-self",
            "remote-self",
            "remote-remote",
            "reference-self",
            "reference-local",
        ],
    )
    @pytest.mark.parametrize("form", ["attached", "detached"])
    def test_attached_option_value_reads_like_detached(self, template, value, denied, form):
        name = {
            "git push": "--repo",
            "git ls-remote": "--upload-pack",
            "git send-pack": "--receive-pack",
            "git archive": "--remote",
            "git clone": "--reference",
        }[" ".join(template.split()[:2])]
        quoted = "'" + value + "'"
        opt = f"{name}={quoted}" if form == "attached" else f"{name} {quoted}"
        cmd = template.format(opt=opt)
        assert security._is_git_tar_to_self(cmd) is denied, cmd

    # Every tar option whose value is a program the shell runs, in every
    # spelling it accepts: separated (``-I 'ssh ...'``), glued to the short
    # letter (``-I'ssh ...'``), inside a short cluster (``-cI'ssh ...'``),
    # ``--opt=value`` and ``--opt value``.  A value that dials this host is
    # refused in every spelling (the carried ``ssh`` is caught by the ssh row);
    # the same spelling naming a non-self host stays allowed.  The short exec
    # letters are only ``I`` (``--use-compress-program``) and ``F``
    # (``--info-script``/``--new-volume-script``); the glued spellings for them
    # are the fix's target.
    @pytest.mark.parametrize(
        ("spelling", "template"),
        [
            ("I-separated", "tar -I 'ssh {h} id' -cf - /dev/null"),
            ("I-attached", "tar -I'ssh {h} id' -cf - /dev/null"),
            ("I-cluster", "tar -cI'ssh {h} id' -f - /dev/null"),
            ("I-long-eq", "tar --use-compress-program='ssh {h} id' -cf - /dev/null"),
            ("I-long-sp", "tar --use-compress-program 'ssh {h} id' -cf - /dev/null"),
            ("F-separated", "tar -F 'ssh {h} id' -cM -L 1 -f - /dev/null"),
            ("F-attached", "tar -F'ssh {h} id' -cM -L 1 -f - /dev/null"),
            ("F-cluster", "tar -cMF'ssh {h} id' -L 1 -f - /dev/null"),
            ("info-script-eq", "tar --info-script='ssh {h} id' -cM -L 1 -f - /dev/null"),
            ("info-script-sp", "tar --info-script 'ssh {h} id' -cM -L 1 -f - /dev/null"),
            ("new-volume-eq", "tar --new-volume-script='ssh {h} id' -cM -L 1 -f - /dev/null"),
            ("new-volume-sp", "tar --new-volume-script 'ssh {h} id' -cM -L 1 -f - /dev/null"),
            ("to-command-eq", "tar --to-command='ssh {h} id' -xf a.tar"),
            ("to-command-sp", "tar --to-command 'ssh {h} id' -xf a.tar"),
            ("rsh-command-eq", "tar --rsh-command='ssh {h} id' -cf - /dev/null"),
            ("rsh-command-sp", "tar --rsh-command 'ssh {h} id' -cf - /dev/null"),
            (
                "checkpoint-eq",
                "tar -cf x.tar --checkpoint=1 --checkpoint-action=exec='ssh {h} id' dir",
            ),
            (
                "checkpoint-sp",
                "tar -cf x.tar --checkpoint=1 --checkpoint-action exec='ssh {h} id' dir",
            ),
        ],
    )
    @pytest.mark.parametrize(
        ("host", "denied"),
        [("localhost", True), ("far.example.com", False)],
        ids=["self", "remote"],
    )
    def test_tar_exec_option_value_dialing_self_is_denied_in_every_spelling(
        self, spelling, template, host, denied
    ):
        # ``_denied_by`` is the real gate path: it lowers the command and keeps
        # the submitted case as ``raw_text``, the way ``is_denied`` does.  An
        # uppercase short letter (``-I``/``-F``) reaches the floor folded, so the
        # floor must deny it without seeing the original case.
        cmd = template.format(h=host)
        if denied:
            assert _denied_by(cmd) == self._SSH_RULE, cmd
        else:
            assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    @pytest.mark.parametrize(
        ("cmd", "denied"),
        [
            # A glued ``f`` before an ``I`` takes the rest of the cluster as its
            # own archive value, so ``-cfI'ssh localhost id'`` names a local
            # archive ``Issh ...`` and runs no program.
            ("tar -cfI'ssh localhost id' dir", False),
            # A local compressor is not a self dial, even glued to the letter.
            ("tar -Izstd -cf out.tar dir", False),
            ("tar -cIzstd -f out.tar dir", False),
        ],
        ids=["glued-f-before-i-is-archive", "glued-local-compressor", "cluster-local-compressor"],
    )
    def test_tar_glued_short_option_non_exec_values_stay_allowed(self, cmd, denied):
        assert (_denied_by(cmd) in self._DIAL_RULES) is denied, cmd

    # A frame that ends inside a quote no reader can close is refused only when
    # it could run git or tar.  Prose with an apostrophe names neither, and
    # ``is_denied`` reads every tool-input string, so it stays allowed.
    @pytest.mark.parametrize("cmd", list(_OPEN_QUOTE_PROSE.values()), ids=list(_OPEN_QUOTE_PROSE))
    def test_prose_with_an_open_quote_is_allowed(self, cmd):
        assert _denied_by(cmd) is None, cmd

    @pytest.mark.parametrize(
        "cmd", list(_OPEN_QUOTE_SELF_DIALS.values()), ids=list(_OPEN_QUOTE_SELF_DIALS)
    )
    def test_a_self_dial_beside_an_open_quote_is_denied(self, cmd):
        assert _denied_by(cmd) in self._DIAL_RULES, cmd

    def test_the_open_quote_corpus_loosens_nothing_and_names_every_tighten(self):
        refused = {cmd: _denied_by(cmd) is not None for cmd, _main, _want in _OPEN_QUOTE_CORPUS}
        loosen = [cmd for cmd, _main, want in _OPEN_QUOTE_CORPUS if want and not refused[cmd]]
        tighten = [cmd for cmd, main, _want in _OPEN_QUOTE_CORPUS if refused[cmd] and not main]
        assert loosen == []
        assert tighten == [cmd for cmd, main, want in _OPEN_QUOTE_CORPUS if want and not main]

    # The open-quote backstop fires only when the whole string carries a
    # self-dial signal it reads (kind 1: a self host, including a self-hostname
    # substitution; kind 2: an override / transport shape), so each must refuse
    # its row on its own while the prose rows above stay allowed.
    @pytest.mark.parametrize(
        ("kind", "cmd"),
        [
            ("literal-self-host", _OPEN_QUOTE_SELF_DIALS["unclosed-quote"]),
            ("override-shape", _OPEN_QUOTE_SELF_DIALS["open-ssh-command-override"]),
            ("hostname-substitution", _OPEN_QUOTE_SELF_DIALS["open-hostname-expansion"]),
            ("hostname-backtick", _OPEN_QUOTE_SELF_DIALS["open-hostname-backtick"]),
        ],
    )
    def test_each_open_quote_signal_kind_refuses_on_its_own(self, kind, cmd):
        assert _denied_by(cmd) in self._DIAL_RULES, (kind, cmd)

    def test_an_exported_environment_with_aliases_finishes_in_bounded_work(self, work_meter):
        # A rebuilt alias re-enters the dispatcher as text; before the fix every
        # rebuild carried the WHOLE exported environment, so an ~18 KB export
        # with nine aliases re-parsed that env nine times and one harmless
        # command took ~27 s in the gate.  The fix drops env entries the rebuilt
        # command cannot read, so the text all re-entries receive together stays
        # below ONE copy of the export.  Restoring the whole-environment copy
        # makes the re-entries carry it again (about seven copies, ~140 K
        # characters, measured).  The bound is an exact character count, not a
        # wall clock.  It is checked before the verdict because that mutation
        # also turns the verdict into a refusal, which would hide the count.
        export = "export " + " ".join(f"V{i}=" + "a" * 200 for i in range(100)) + "; "
        command = export + "; ".join(f"git -c alias.b{i}=status b{i}" for i in range(9))
        assert len(export) >= 18 * 1024, len(export)
        work_meter.reset()
        verdict = is_denied(command)
        assert work_meter.reentries >= 1, work_meter.reentries
        assert work_meter.reentered_chars < len(export), work_meter.reentered_chars
        assert verdict is None, verdict  # nine local ``status`` aliases, no self dial

    def test_force_local_cannot_exempt_a_self_shaped_archive(self):
        # Failing closed here is the maintainer-accepted price of removing the
        # option-grammar exemption: a self-shaped archive is always refused.
        assert security._is_git_tar_to_self("tar --force-local -cf localhost:a.tar dir")
        assert security._is_git_tar_to_self("tar -cf localhost:a.tar --force-local")

    # A wrapper's option words are read in the submitted case: ``env -C`` and
    # ``sudo -u`` take a value, ``sudo -H`` takes none (env(1), sudo(8)), so the
    # program is the word after them, however a lowered reading would see them.
    _WRAPPER_CASE_DENIED = [
        "env -C /tmp git clone localhost:x",
        "sudo -H git clone localhost:x",
        "sudo -u root -H git ls-remote localhost:x",
        "env -C /tmp tar -cf localhost:/x .",
        "nice env -C / git fetch localhost:x",
        "bash -c 'sudo -H git clone localhost:x'",
    ]

    @pytest.mark.parametrize("cmd", _WRAPPER_CASE_DENIED)
    def test_wrapper_options_are_read_in_the_submitted_case(self, cmd):
        assert _denied_by(cmd) == self._RULE, cmd

    @pytest.mark.parametrize("cmd", ["env -C /tmp git clone github.com:x", "sudo -H git status"])
    def test_wrapper_options_in_the_submitted_case_keep_local_work_allowed(self, cmd):
        assert is_denied(cmd, denied_regexes=self._effective()) is None, cmd

    # The ssh-self floor's case-folded env names (``rsync_rsh`` / ``RSYNC_RSH``,
    # ``git_ssh_command`` / ``GIT_SSH_COMMAND``), read by the frame environment
    # both floors share.
    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            # The floors read lower-cased text, so a benign lower-case variable
            # folds onto the upper-case one the program honors.  Every value
            # kept under the folded name is checked, in either order and through
            # ``export``, ``env`` and a nested payload.
            (f"{cmd} far:/x .", "sandbox-escape-ssh-self")
            for cmd in (
                "RSYNC_RSH='ssh localhost' rsync_rsh=ssh rsync",
                "rsync_rsh=ssh RSYNC_RSH='ssh localhost' rsync",
                "export RSYNC_RSH='ssh localhost' rsync_rsh=ssh; rsync",
                "export rsync_rsh=ssh RSYNC_RSH='ssh localhost'; rsync",
                "RSYNC_RSH='ssh localhost'; rsync_rsh=ssh; export RSYNC_RSH; rsync",
                "env RSYNC_RSH='ssh localhost' rsync_rsh=ssh rsync",
                "env rsync_rsh=ssh RSYNC_RSH='ssh localhost' rsync",
            )
        ]
        + [
            (
                "RSYNC_RSH='ssh localhost' rsync_rsh=ssh sh -c 'rsync far:/x .'",
                "sandbox-escape-ssh-self",
            ),
            (
                "rsync_rsh=ssh RSYNC_RSH='ssh localhost' sh -c 'rsync far:/x .'",
                "sandbox-escape-ssh-self",
            ),
        ]
        + [
            (f"{cmd} far:x", "sandbox-escape-ssh-self")
            for cmd in (
                "GIT_SSH_COMMAND='ssh localhost id #' git_ssh_command=ssh git ls-remote",
                "git_ssh_command=ssh GIT_SSH_COMMAND='ssh localhost id #' git ls-remote",
                "export GIT_SSH_COMMAND='ssh localhost id #' git_ssh_command=ssh; git ls-remote",
                "export git_ssh_command=ssh GIT_SSH_COMMAND='ssh localhost id #'; git ls-remote",
                "env GIT_SSH_COMMAND='ssh localhost id #' git_ssh_command=ssh git ls-remote",
                "env git_ssh_command=ssh GIT_SSH_COMMAND='ssh localhost id #' git ls-remote",
            )
        ]
        + [
            (
                "GIT_SSH_COMMAND='ssh localhost id #' git_ssh_command=ssh sh -c 'git ls-remote far:x'",
                "sandbox-escape-ssh-self",
            ),
            (
                "git_ssh_command=ssh GIT_SSH_COMMAND='ssh localhost id #' sh -c 'git ls-remote far:x'",
                "sandbox-escape-ssh-self",
            ),
        ],
    )
    def test_case_folded_env_names_keep_every_value(self, cmd, rule):
        assert _denied_by(cmd) == rule, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "RSYNC_RSH='ssh far-host' rsync_rsh=ssh rsync far:/x .",
            "rsync_rsh=ssh RSYNC_RSH='ssh far-host' sh -c 'rsync far:/x .'",
            "GIT_SSH_COMMAND='ssh -i key' git_ssh_command=ssh git ls-remote far:x",
        ],
    )
    def test_case_folded_env_names_with_no_self_value_stay_allowed(self, cmd):
        assert _denied_by(cmd) is None, cmd

    # A launcher outside the known wrapper set runs a later ``git``/``tar``/``gtar``
    # word as its program, and ``script -c`` runs its value as a command line.
    # GNU tar installed as ``gtar`` reads ``host:file`` the way ``tar`` does.  A
    # ``submodule.<name>.url`` override is the remote ``submodule update`` clones.
    _LAUNCHED_SELF_DIALS = [
        "unbuffer git clone --upload-pack='touch /tmp/pwn' localhost:x",
        "strace -f git clone --upload-pack='touch /tmp/pwn' localhost:x",
        "watch -n 999 git clone --upload-pack='touch /tmp/pwn' localhost:x",
        "chronic git clone localhost:x",
        "caffeinate git clone localhost:x",
        "torsocks git clone localhost:x",
        "unbuffer tar -cf localhost:a.tar d",
        "script -qc 'git clone localhost:x' /dev/null",
        "script --command='git clone localhost:x' /dev/null",
        "gtar -cf localhost:a.tar d",
        "gtar -cf far:a d --rsh-command='ssh localhost'",
        "unbuffer gtar -cf localhost:a.tar d",
        "git -c submodule.x.url=localhost:x submodule update --init",
        "git -c submodule.x.url=localhost:x submodule add far:y",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=submodule.x.url GIT_CONFIG_VALUE_0=localhost:x "
        "git submodule update --init",
        # ``script -c`` runs its value with the environment ``script`` itself got.
        "GIT_SSH_COMMAND='ssh localhost' script -qc 'git fetch origin' /dev/null",
        "export GIT_SSH_COMMAND='ssh localhost'; script -qc 'git fetch o' /dev/null",
        # git rewrites a ``submodule.<name>.url`` by ``insteadOf`` like a remote URL.
        "git -c url.ssh://local.insteadOf=foo -c submodule.x.url=foohost/repo submodule update --init",
        "GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=url.ssh://local.insteadOf GIT_CONFIG_VALUE_0=foo "
        "GIT_CONFIG_KEY_1=submodule.x.url GIT_CONFIG_VALUE_1=foohost/repo git submodule update",
        "GIT_CONFIG_PARAMETERS=\"'url.ssh://local.insteadof'='foo' 'submodule.x.url'='foohost/r'\" "
        "git submodule update --init",
        # GNU ``parallel`` appends each ``:::`` argument to the command it runs.
        "parallel git clone ::: localhost:x",
        "parallel git clone {} ::: localhost:x",
        "parallel -j2 git ls-remote ::: far:x localhost:y",
        "parallel tar -cf ::: localhost:a.tar",
        # ... or puts it in place of each replacement string (``{}``, ``{.}``).
        "parallel tar -cf {} ::: localhost:a.tar",
        "parallel git clone {} dir ::: localhost:x",
        "parallel tar -cf {.}.tar d ::: localhost:a",
        # ... or the custom replacement string a replace option (``-I``, ``--replace``,
        # ``--extensionreplace``...) sets.
        "parallel -I @@ tar -cf @@ ::: localhost:a.tar",
        "parallel -I@@ git clone @@ dir ::: localhost:x",
        "parallel --replace=XX tar -cf XX ::: localhost:a",
        "parallel --replace XX tar -cf XX ::: localhost:a",
        "parallel --er E tar -cf E.tar d ::: localhost:a",
        # A replacement string whose value holds a ``$`` is refused unread (fail closed).
        "parallel -I '$X' tar -cf x ::: a",
        # ``--rpl``, ``--plus`` and perl strings (``{=...=}``) are refused unread.
        "parallel --rpl '{X} s/a/b/' tar -cf {X} ::: localhost:a",
        "parallel --plus tar -cf {..} ::: localhost:a.tar",
        "parallel tar -cf '{=s/x/y/=}' ::: localhost:a",
        "parallel git clone '{=s/x/y/=}' ::: a",
        "parallel --plus git fetch {..} ::: a",
        "/usr/bin/parallel git {=x=} ::: clone",
        # A ``--rpl`` tag can be any word, the git verb included, and ``--plus``
        # adds strings: behind ``parallel`` both are refused, whatever the verb.
        "parallel --rpl 'log s/.*/clone/' git log localhost:x ::: a",
        "parallel --rpl 'log s/.*/clone/' git log --upload-pack='touch /tmp/pwn' localhost:x ::: a",
        "parallel --rpl 'log s/.*/fetch/' git -c core.sshcommand='ssh localhost id' log origin ::: a",
        "parallel --rpl 'status s/.*/fetch/' git status localhost:x ::: a",
        "parallel --rpl='status s/.*/fetch/' git status localhost:x ::: a",
        "parallel --rp 'status s/.*/fetch/' git status localhost:x ::: a",
        "parallel --plus git status ::: a",
        # A replacement string in or before the verb rewrites it from input the
        # floor cannot read (a ``::::`` file, stdin), so it is refused.
        "parallel -I XX git XX localhost:x :::: verbs.txt",
        "echo clone | parallel git {} localhost:x",
        "parallel -I XX git -c XX log origin :::: opts.txt",
        "parallel -I '$X' git status ::: a",
        "parallel git -c {} fetch origin ::: core.sshcommand=ssh\\ localhost",
        "ls | parallel git {} localhost:x",
        "ls | parallel git -C {} -c {} fetch origin",
        "ls | parallel git -C {} fetch localhost:x",
        "parallel git {1} {2} ::: clone ::: localhost:x",
        # A launcher other than ``parallel`` still runs a later git word.
        "unbuffer git clone localhost:x",
        "strace -f git clone localhost:x",
        # Printed text piped to a shell runs: the data-reader exemption is lost.
        "echo git clone localhost:x | sh",
        "echo git clone --upload-pack=touch\\ /tmp/pwn localhost:x | bash",
        "printf '%s ' git clone localhost:x | sh",
        "echo tar -cf localhost:a.tar d | zsh",
        "echo git clone localhost:x | $SHELL",
    ]

    @pytest.mark.parametrize("cmd", _LAUNCHED_SELF_DIALS)
    def test_launched_gtar_and_submodule_self_dials_are_denied(self, cmd):
        assert security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) in self._DIAL_RULES, cmd

    _LAUNCHED_LOCAL_WORK = [
        "grep git README",
        "echo git clone localhost:x",
        "echo tar -cf localhost:a d",
        "man git",
        "make git",
        "watch -n 1 git status",
        "strace git status",
        "unbuffer git fetch origin",
        "unbuffer git clone far:x",
        "script -qc 'git clone far:x' /dev/null",
        "gtar -cf far.example.com:a.tar d",
        "parallel git clone ::: github.com:x",
        "parallel -j4 git fetch ::: origin upstream",
        "parallel tar -cf {}.tar {} ::: a b",
        "parallel git clone {} ::: github.com:x",
        "parallel -I @@ tar -cf @@.tar @@ ::: a b",
        "parallel -I @@ git clone @@ ::: github.com:x",
        "parallel --bnr B git clone B ::: github.com:x",
        # Only ``parallel`` reads replacement strings; a local verb opens no transport.
        "unbuffer git commit -m 'use {..} ranges'",
        "unbuffer git log --oneline -- 'src/{..}'",
        "strace -f git log --format='{+x}'",
        "uv run git commit -m 'replace {...} with Object.assign'",
        "strace -f tar -cf a.tar 'dir{..}'",
        "parallel git commit -m '{=x=}' ::: a",
        "parallel -I XX git log XX :::: revs.txt",
        "echo status | parallel -I XX git diff XX",
        "parallel -j4 git -C {} pull ::: repo1 repo2",
        # parallel shell-quotes each input, so one in a path option's value names
        # a directory, never a verb or config, even from stdin or a ``::::`` file.
        "ls | parallel git -C {} pull",
        "ls -d */ | parallel -j4 git -C {} fetch --prune origin",
        "ls -d */ | parallel git -C {} pull",
        "parallel git --git-dir={}/.git fetch origin :::: repos.txt",
        "parallel git --work-tree {} --git-dir {}/.git pull :::: repos.txt",
        "echo git status | sh",
        "git log --oneline | grep localhost:",
        "grep git README | sort",
        "git commit -m 'git clone localhost:x'",
        "git -c submodule.x.url=far:x submodule update --init",
        "script -qc 'git fetch origin' /dev/null",
        "GIT_SSH_COMMAND='ssh -i key' script -qc 'git fetch o' /dev/null",
        "git -c url.ssh://github.com/.insteadOf=gh: -c submodule.x.url=gh:org/repo submodule update --init",
    ]

    @pytest.mark.parametrize("cmd", _LAUNCHED_LOCAL_WORK)
    def test_launched_local_work_and_other_hosts_stay_allowed(self, cmd):
        assert not security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    def test_parallel_runs_past_the_word_cap_are_refused_unread(self):
        inputs = " ".join(f"a{i}" for i in range(2100))
        assert not security._is_git_tar_to_self(f"parallel git status {{}} ::: {inputs[:400]}")
        assert security._is_git_tar_to_self(f"parallel git status {{}} ::: {inputs}")

    # A carried value that builds its program from a same-line assignment
    # (``G=git; $G ...``) runs that program, so it is re-entered like a literal
    # ``git``/``tar`` word: the value is read through the same source pre-walk.
    _VARIABLE_BUILT_SELF_DIALS = [
        "git -c alias.x='!G=git; $G -c url.ssh://local.insteadOf=foo "
        "ls-remote --upload-pack=id foohost/x' x",
        "GIT_SSH_COMMAND='G=git; $G ls-remote localhost:x' git fetch o",
        "git -c core.sshCommand='G=git; $G ls-remote localhost:x' fetch o",
        "git clone --upload-pack='G=git; $G ls-remote localhost:x' far:x",
        "tar -cf far:a d --rsh-command='T=tar; $T -cf localhost:b d'",
        # The same carriers with no literal self host: only the re-entry sees
        # the ``insteadOf`` rewrite to self.
        "GIT_SSH_COMMAND='G=git; $G -c url.ssh://local.insteadOf=foo ls-remote foohost/x' "
        "git fetch o",
        "git -c core.sshCommand='G=git; $G -c url.ssh://local.insteadOf=foo ls-remote foohost/x' "
        "fetch o",
        "git clone --upload-pack='G=git; $G -c url.ssh://local.insteadOf=foo ls-remote foohost/x' "
        "far:x",
        "tar -cf far:a d --rsh-command='T=git; $T -c url.ssh://local.insteadOf=foo "
        "ls-remote foohost/x'",
        "GIT_SSH_COMMAND='${G:-git} -c url.ssh://local.insteadOf=foo ls-remote foohost/x' "
        "git fetch o",
    ]

    @pytest.mark.parametrize("cmd", _VARIABLE_BUILT_SELF_DIALS)
    def test_variable_built_carried_git_or_tar_is_denied(self, cmd):
        assert security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) in self._DIAL_RULES, cmd

    # A variable-built program that is not git/tar, or that resolves to git
    # doing local work, stays allowed; an unresolved ``$SSH`` value naming only
    # a remote hop is re-entered literal-only, so it is never DNS-pending refused.
    _VARIABLE_BUILT_LOCAL_WORK = [
        "GIT_SSH_COMMAND='G=ssh; $G -i key' git fetch o",
        "git -c alias.x='!G=git; $G status' x",
        "GIT_SSH_COMMAND='$SSH -J bastion.example.com' git fetch o",
    ]

    def test_a_resolved_program_word_is_read_as_what_it_runs(self):
        # ``G=ssh; $G`` resolves to ssh, so the value keeps the literal prefilter;
        # a ``$``-built word the pre-walk cannot resolve may be git or tar.
        assert not _argv_floor._value_runs_git_or_tar("G=ssh; $G -i key")
        assert _argv_floor._value_runs_git_or_tar("G=git; $G status")
        assert _argv_floor._value_runs_git_or_tar("${G:-git} status")

    @pytest.mark.parametrize("cmd", _VARIABLE_BUILT_LOCAL_WORK)
    def test_variable_built_local_work_is_allowed(self, cmd, _floor_monkeypatch):
        _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: True)
        assert not security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    # A carried program word that is a glob ``_git_tar_verbs`` reads as git or tar
    # (``/usr/bin/g?t``, ``[g]it``, ``t?r``) is re-entered like the literal word:
    # the carried-value prefilter shares that verb test instead of its own list.
    _GLOB_BUILT_SELF_DIALS = [
        "GIT_SSH_COMMAND='/usr/bin/g?t -c url.ssh://local.insteadOf=foo "
        "ls-remote --upload-pack=id foohost/x' git fetch o",
        "git -c alias.x='!/usr/bin/g?t ls-remote --upload-pack=id foohost/x "
        "-c url.ssh://local.insteadOf=foo' x",
        "tar -cf far:a d --rsh-command='t?r -cf localhost:b d'",
        "tar -cf far:a d --rsh-command='g?t -c url.ssh://local.insteadOf=foo "
        "ls-remote --upload-pack=id foohost/x'",
        "GIT_SSH_COMMAND='[g]it ls-remote localhost:x' git fetch o",
    ]

    @pytest.mark.parametrize("cmd", _GLOB_BUILT_SELF_DIALS)
    def test_glob_built_carried_git_or_tar_is_denied(self, cmd):
        assert security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) in self._DIAL_RULES, cmd

    # A glob that cannot name git or tar keeps the literal prefilter, and a glob
    # git doing local work is re-entered literal-only, so both stay allowed.
    @pytest.mark.parametrize(
        "cmd",
        [
            "GIT_SSH_COMMAND='/usr/bin/s?h -i key' git fetch o",
            "GIT_SSH_COMMAND='g?t status' git fetch o",
        ],
    )
    def test_glob_built_local_work_is_allowed(self, cmd):
        assert not security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    def test_the_value_prefilter_reads_globs_like_the_verb_test(self):
        assert _argv_floor._value_runs_git_or_tar("/usr/bin/g?t status")
        assert _argv_floor._value_runs_git_or_tar("gt?r -cf a d")
        assert not _argv_floor._value_runs_git_or_tar("/usr/bin/s?h -i key")

    # A glob host counts as bash expands it: a glob character outside quotes in
    # the word as typed, even when quotes split the word (``l"ocal"*``) or the
    # glob sits in an attached option value (``--remote=``, ``-cf<word>``),
    # which the floor reads as active whatever its quotes.
    _SPLICED_GLOB_SELF_HOSTS = [
        'git ls-remote l"ocal"*host:x',
        "git ls-remote lo\\cal*host:x",
        "git archive --remote=local*:x HEAD",
        "tar -cflocal*:a.tar d",
        'tar -cf l"ocal"*:a.tar d',
    ]

    @pytest.mark.parametrize("cmd", _SPLICED_GLOB_SELF_HOSTS)
    def test_a_quote_spliced_or_attached_glob_self_host_is_denied(self, cmd):
        assert security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) in self._DIAL_RULES, cmd

    # A glob quoted whole reaches git as typed, and a glob that cannot match a
    # self name is never a self host; neither is sent to DNS.
    _QUOTED_OR_FOREIGN_GLOBS = [
        "git fetch origin '+refs/heads/*:refs/remotes/origin/*'",
        "git push origin '*:*'",
        "git ls-remote 'local*:x'",
        "git clone github*:x",
    ]

    @pytest.mark.parametrize("cmd", _QUOTED_OR_FOREIGN_GLOBS)
    def test_a_quoted_or_foreign_glob_host_is_allowed(self, cmd, _floor_monkeypatch):
        _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: True)
        assert not security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    # Many short git or tar commands in one line, or one git command followed by
    # many ordinary lines, cost the same work for each command added.  Reading
    # every later command again for each git/tar word made the work grow with
    # the square of the line count.  Counted in interpreter instructions over
    # two runs of three equally spaced sizes, never a clock.
    @pytest.mark.parametrize(
        "build",
        [
            lambda n: "git status; " * n,
            lambda n: "tar -tf a.tar; " * n,
            lambda n: "git status\n" + "".join(f"echo line{i} >> out.txt\n" for i in range(n)),
        ],
        ids=["repeated-git-status", "repeated-tar-list", "git-then-many-lines"],
    )
    @pytest.mark.parametrize("sizes", [(50, 100, 150), (100, 200, 300)], ids=["50", "100"])
    def test_many_commands_in_one_line_are_read_in_linear_work(self, build, sizes):
        assert is_denied(build(sizes[0] // 2)) is None  # first-call work is spent here
        work = []
        for n in sizes:
            command = build(n)
            verdicts: "list[str | None]" = []
            work.append(_interpreter_work(lambda c=command: verdicts.append(is_denied(c))))
            assert verdicts == [None], (n, verdicts)
        assert _linear_work(work), f"{work} instructions at sizes {sizes}"

    # A PR body or commit message with inline code written as \`x\` gives the
    # payload walk one frame per escaped backtick, each the rest of the body.
    # Reading every such frame made the floor's work grow with the square of the
    # body (a 5.5 KB ``gh pr create --body`` took 15 s in this floor alone).  The
    # walk is shared with the ssh floor on main, so it is computed first and
    # handed back from a memo: what is counted is this floor's own work.
    _TICK_SENTENCE = "Run \\`kirocrew status\\` to check the \\`x\\` value first. "

    @staticmethod
    def _floor_work(patch, command):
        walks = {}
        real = _argv_floor._shell_payload_walk

        def walk(text, **kwargs):
            key = (text, tuple(sorted(kwargs.items())))
            if key not in walks:
                walks[key] = real(text, **kwargs)
            return [(source, list(tokens)) for source, tokens in walks[key]]

        patch.setattr(_argv_floor, "_shell_payload_walk", walk)

        def read():
            return security._is_git_tar_to_self(command.lower(), raw_text=command)

        assert read() is False  # fills the walk memo
        verdicts = []
        work = _interpreter_work(lambda: verdicts.append(read()))
        assert verdicts == [False], command[:80]
        patch.setattr(_argv_floor, "_shell_payload_walk", real)
        return work

    def test_escaped_backticks_in_a_quoted_body_are_read_in_linear_work(self, _floor_monkeypatch):
        work = []
        for n in (20, 40, 60):
            body = 'gh pr create --title t --body "' + self._TICK_SENTENCE * n + '"'
            work.append(self._floor_work(_floor_monkeypatch, body))
        assert _linear_work(work), f"{work} instructions at 20, 40, 60 sentences"

    def test_a_5kb_body_costs_the_same_with_escaped_backticks(self, _floor_monkeypatch):
        # The reviewer's 5.5 KB body against the same body with \$ in place of
        # each \`: an escaped ``$`` keeps the floor reading (a body with no
        # machinery at all stops at the first gate) but adds no frame.  Measured
        # 1.04x after the fix; reading every frame cost 328x.
        def body(sentence):
            return 'gh pr create --title t --body "' + (sentence * 100)[:5500] + '"'

        ticks = self._floor_work(_floor_monkeypatch, body(self._TICK_SENTENCE))
        plain = self._floor_work(
            _floor_monkeypatch, body(self._TICK_SENTENCE.replace("\\`", "\\$"))
        )
        assert ticks * 2 <= plain * 3, f"{ticks} instructions with \\`, {plain} with \\$"

    # Prose that names git many times passes the word cap (each ``git`` word is a
    # later start), but names no host: it is read, not refused.  Past the cap a
    # later start is read only up to the next one, so the work stays linear, and
    # a command that carries a self host is still refused unread.
    _PROSE_SENTENCE = (
        "Run \\`git status\\` then \\`git fetch origin\\` and \\`git log\\` to check. "
    )
    _PLAIN_SENTENCE = "Run git status then git fetch origin and git log to check. "

    @staticmethod
    def _prose_body(n):
        sentence = TestGitTarSelfExec._PROSE_SENTENCE
        return 'gh pr create --title "docs: explain the flow" --body "' + sentence * n + '"'

    @pytest.mark.parametrize(
        "cmd",
        [
            'gh pr create --title "docs: explain the flow" --body "' + _PROSE_SENTENCE * 22 + '"',
            'gh pr create --title "docs: explain the flow" --body "' + _PROSE_SENTENCE * 70 + '"',
            'gh pr create --title t --body "' + _PLAIN_SENTENCE * 52 + '"',
            "cat > notes.md <<'EOF'\n" + _PROSE_SENTENCE.replace("\\", "") * 25 + "\nEOF",
        ],
        ids=["tick-body-1.5k", "tick-body-5k", "plain-body-3k", "heredoc-one-long-line"],
    )
    def test_long_prose_naming_git_is_allowed(self, cmd):
        assert len(cmd) > 1400, len(cmd)
        assert _denied_by(cmd) is None, cmd[:80]

    @pytest.mark.parametrize(
        "cmd",
        [
            "unbuffer git clone localhost:x",
            "unbuffer " + "git " * 100 + "clone --origin git localhost:x",
            "git " * 2000 + "clone localhost:x",
            'gh pr create --body "'
            + "Run git status then git fetch origin and git log to check. " * 50
            + '"; git clone localhost:x',
            'sh -c "echo \\`git clone localhost:x\\`"',
            "eval \\`git clone localhost:x\\`",
            'sh -c "echo ' + _PROSE_SENTENCE * 22 + ' \\`git clone localhost:x\\`"',
        ],
        ids=[
            "unbuffer-clone",
            "past-cap-origin-git",
            "past-cap-git-words",
            "prose-then-live",
            "sh-c-unescapes-tick",
            "eval-unescapes-tick",
            "sh-c-long-prose-then-tick",
        ],
    )
    def test_a_self_dial_beside_many_git_words_is_denied(self, cmd):
        assert _denied_by(cmd) == self._RULE, cmd[:80]

    def test_long_prose_naming_git_is_read_in_linear_work(self, _floor_monkeypatch):
        work = []
        for n in (20, 40, 60):  # about 1.5, 3 and 4.5 KB
            command = self._prose_body(n)
            assert _denied_by(command) is None, n
            work.append(self._floor_work(_floor_monkeypatch, command))
        assert _linear_work(work), f"{work} instructions at 20, 40, 60 sentences"

    # ``export``, ``declare -x`` and ``typeset -x`` export only as the program
    # word of their command.  Printed by ``echo``/``printf`` they are data, so
    # the value they name never reaches the next command.
    @pytest.mark.parametrize(
        "cmd",
        [
            "echo export GIT_SSH_COMMAND='ssh localhost'; git ls-remote .",
            "printf '%s\\n' export RSYNC_RSH='ssh localhost'; rsync far:/x .",
            "echo declare -x GIT_SSH_COMMAND='ssh localhost'; git fetch o",
        ],
        ids=["echo-export", "printf-export-rsync", "echo-declare-x"],
    )
    def test_a_printed_export_word_exports_nothing(self, cmd):
        assert is_denied(cmd) is None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "export GIT_SSH_COMMAND='ssh localhost'; git ls-remote .",
            "declare -x GIT_SSH_COMMAND='ssh localhost'; git fetch o",
            "GIT_SSH_COMMAND='ssh localhost' git fetch o",
            "export A=1 RSYNC_RSH='ssh localhost'; rsync far:/x .",
            "command export GIT_SSH_COMMAND='ssh localhost'; git fetch o",
            "typeset -x RSYNC_RSH='ssh localhost'; rsync far:/x .",
            "if true; then export GIT_SSH_COMMAND='ssh localhost'; fi; git fetch o",
        ],
        ids=[
            "export",
            "declare-x",
            "leading-assignment",
            "export-second-operand-rsync",
            "command-export",
            "typeset-x-rsync",
            "export-after-then",
        ],
    )
    def test_a_real_export_word_still_exports(self, cmd):
        assert _denied_by(cmd) == "sandbox-escape-ssh-self", cmd

    # Bash takes a redirection out of the command before it finds the program,
    # so ``>/dev/null export A=x`` still runs ``export``.  The redirection, and
    # a target written as its own word, are not the program word.
    @pytest.mark.parametrize(
        "cmd",
        [
            ">/dev/null export RSYNC_RSH='ssh localhost id #'; rsync far:/x .",
            "2>/dev/null export RSYNC_RSH='ssh localhost id #'; rsync far:/x .",
            "> /dev/null export RSYNC_RSH='ssh localhost id #'; rsync far:/x .",
            ">/dev/null export RSYNC_CONNECT_PROG='ssh localhost id'; rsync rsync://far/x .",
            "<in.txt export RSYNC_RSH='ssh localhost id #'; rsync far:/x .",
            "2>&1 export RSYNC_RSH='ssh localhost id #'; rsync far:/x .",
            "2> /dev/null >>log export RSYNC_RSH='ssh localhost id #'; rsync far:/x .",
            "export >/dev/null RSYNC_RSH='ssh localhost id #'; rsync far:/x .",
            ">/dev/null declare -x RSYNC_RSH='ssh localhost'; rsync far:/x .",
            "case x in x) >/dev/null export RSYNC_RSH='ssh localhost';; esac; rsync far:/x .",
        ],
        ids=[
            "stdout-null",
            "stderr-null",
            "detached-target",
            "connect-prog",
            "stdin-file",
            "dup-fd",
            "two-redirections",
            "redirection-after-export",
            "declare-x",
            "case-arm-body",
        ],
    )
    def test_a_redirection_before_export_still_exports(self, cmd):
        assert is_denied(cmd) is not None, cmd

    # The git/tar floor also reads each command with its redirections taken out,
    # so a redirection between an option and its value does not hide the host.
    @pytest.mark.parametrize(
        ("cmd", "dials"),
        [
            ("tar -cf >/dev/null localhost:a.tar d", True),
            (">/dev/null export GIT_SSH_COMMAND='ssh localhost'; git fetch o", True),
            ("tar -cf >/dev/null a.tar d", False),
        ],
        ids=["tar-archive-after-redirect", "export-after-redirect", "local-archive"],
    )
    def test_the_redirection_free_view_is_read(self, cmd, dials):
        assert security._is_git_tar_to_self(cmd) is dials, cmd

    # tar short options are case-sensitive: ``-C``/``-T``/``-X`` take a value and
    # name no operation, so the bundle's own ``c``/``t``/``x`` stays the operation
    # and its ``f`` keeps the archive slot.  Words past ``--`` are not options, and
    # a ``TAR_OPTIONS`` value names an operation only when it holds one.
    @pytest.mark.parametrize(
        "cmd",
        [
            "tar xzf localhost:a.tgz -C /srv",
            "tar cf localhost:a.tar -C /tmp .",
            "tar cf localhost:a.tar -T list.txt",
            "tar cf localhost:a.tar -X list",
            "tar cf localhost:.bashrc --rsh-command=/usr/bin/ssh -C /tmp payload",
            "tar cf localhost:a.tar -- --create",
            "TAR_OPTIONS='--rsh-command=ssh' tar tf localhost:a.tar",
            "tar tf localhost:a.tar -T /dev/null",
            "tar tf localhost:a.tar -X /dev/null",
            "tar cf localhost:a.tar -Cx /tmp",
            "tar cf localhost:a.tar -C -x",
            "tar cf localhost:a.tar --exclude -- --create",
            "tar cf localhost:a.tar -C $D .",
            "sh -c 'tar cf localhost:a.tar -C /tmp .'",
            "tar cf localhost:a.tar dir",
            "tar cf localhost:a.tar --directory=/tmp .",
            "tar cf localhost:a.tar --directory /tmp .",
            "tar cf localhost:a.tar -v .",
            "tar -cf localhost:a.tar -C /tmp .",
            "TAR_OPTIONS=--create tar Cf /tmp localhost:a.tar",
            # GNU tar takes a repeated operation: the bundle's ``c`` stays ``c``.
            "tar cf localhost:a.tar --create dir",
            "tar cf localhost:a.tar -c dir",
            "tar xf localhost:a.tar --extract",
            "TAR_OPTIONS=--create tar cf localhost:a.tar dir",
            "TAR_OPTIONS=-c tar cf localhost:a.tar dir",
        ],
    )
    def test_a_bundle_archive_beside_a_cased_value_option_is_read(self, cmd):
        assert _denied_by(cmd) == self._RULE, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "tar -xzf x.tgz -C /tmp",
            "tar czf out.tgz -C src .",
            "tar xzf a.tgz -C /srv",
            "tar cf a.tar -T list.txt",
            "tar tf far.example.com:a.tar -C /tmp",
            "tar cf far.example.com:a.tar -C /tmp .",
            "TAR_OPTIONS='--rsh-command=ssh' tar tf a.tar -T list",
        ],
    )
    def test_a_local_bundle_archive_beside_a_cased_value_option_is_allowed(self, cmd):
        assert is_denied(cmd) is None, cmd
