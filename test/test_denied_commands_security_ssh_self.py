"""Tests for the ssh-to-self floor: which ssh-family command lines reach a host.

``TestSandboxEscapeSshSelf`` pins the ``sandbox-escape-ssh-self`` rule on both tiers, its
refusal note, and how the argv floor reads a destination through quoting, options,
``user@`` prefixes, globs and shell expansions. The own-host set a destination is
compared against is pinned by ``TestSandboxEscapeSshSelfOwnHost`` in
``test_denied_commands_security_own_host.py``.
"""

from __future__ import annotations

import re

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _denied_by, _rule_pattern

from kiro_crew import security
from kiro_crew.security import BUILTIN_DENIED_RULES
from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import compute_effective_denied, is_denied, is_safe_user_regex

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestSandboxEscapeSshSelf:
    """``ssh localhost`` re-enters this machine OUTSIDE the sandbox.

    The far side of a loopback/own-host connection is a fresh unsandboxed
    login shell (and passwordless sudo there completes a full escape), so the
    ssh family refuses a target that resolves to THIS machine.  Same two-tier
    build as the other self-protection floors: a lint-safe positional regex in
    the catalog (the human-auditable subset) plus the ``_is_ssh_to_self`` argv
    floor that resolves options, quoting, ``user@`` prefixes, and this host's
    own names.  Connections to OTHER hosts must stay allowed — including a
    remote command that merely mentions "localhost" as data.
    """

    _RULE = "sandbox-escape-ssh-self"

    @staticmethod
    def _effective():
        return list(compute_effective_denied(BUILTIN_DENIED_RULES, (), False, (), ()))

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-local autouse stub, moved verbatim; de-flake pass tracked
    def _pin_own_host_cache(self, monkeypatch):
        # None of this class's parametrized deny cases target the machine's own
        # hostname dynamically -- they use localhost / 127.0.0.1 / ::1 / the
        # literal ``$(hostname)`` text hint -- so a fixed cache with the
        # resolved-once latch set covers them, and it keeps ``_own_host_names``
        # from seeding the module globals and spawning the real
        # ``kirocrew-own-host-resolve`` getfqdn/getaddrinfo daemon thread (a
        # no-test-side-effects violation, plus leaked global state for the
        # worker). ``_OWN_HOST_RESOLVE_DONE`` short-circuits ``_own_host_names``
        # before the lock, so there is no seed and no thread; monkeypatch
        # restores the globals afterward. The resolver unit tests in
        # ``TestSandboxEscapeSshSelfOwnHost`` re-pin these same globals and call the
        # cache fn directly, so this autouse pin does not interfere with them.
        own = security.socket.gethostname().strip().lower()
        pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
        # The cache slots live on the module that OWNS them: they are
        # deliberately not re-exported by the facade (a slot rebound through
        # ``global`` would leave the facade holding a stale value), so the
        # facade's patch mirroring does not cover them and the owner is
        # patched directly.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
        # tests run at steady state — the kernel address table has
        # published, so IP-literal allow rows resolve on their own merits.
        # The pending-window tests in ``TestSandboxEscapeSshSelfOwnHost`` set this back
        # to False themselves.
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
        # The DNS-alias verdict layer fails closed on hostnames it has not
        # resolved, and tests must not resolve real names -- stub it to
        # "not self" so the parametrized remote-host allow cases stay
        # allowed.  The alias-layer tests in ``TestSandboxEscapeSshSelfOwnHost`` re-bind the
        # real function (captured at import as _REAL_RESOLVED_HOST_VERDICT) and
        # stub the resolver socket instead.
        monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)

    def test_rule_is_registered_on_both_tiers(self):
        assert self._RULE in {r.id for r in BUILTIN_DENIED_RULES}
        assert self._RULE in security._SELF_PROTECTION_FLOOR_RULE_IDS
        assert self._RULE in security._SELF_PROTECTION_FLOOR_NOTES
        assert _rule_pattern(self._RULE) in self._effective()
        # A pattern that fails the safety lint is silently DISABLED by
        # ``_DenyMatcher`` — the rule would report as present while enforcing
        # nothing, which is exactly how the first draft of this rule failed.
        assert is_safe_user_regex(_rule_pattern(self._RULE))

    def test_refusal_note_is_actionable(self):
        """The note carries the three facts a refused caller needs.

        A first-contact refusal is a PENDING classification, so the note must
        say so and tell the caller the action that resolves it (retry the same
        command).  A forwarded-port target (container/VM at ``localhost:2222``)
        is denied by design, so the note must name that class and the operator
        recourse — otherwise the only discoverable option is abandoning the
        command.  A retry can itself land inside the background check, so a
        refusal is only settled once it repeats after the check has had time to
        finish; the note must say how long to keep retrying and what to use
        instead after that (an unresolvable name, an IP address while the
        address list cannot be read, or this machine).  The cases are labelled
        so an agent can find its own without parsing a conditional sentence.
        Content-pinned here because the note is the whole UX of this floor: a
        reword that drops the retry hint leaves the caller with no way forward.
        """
        note = security._SELF_PROTECTION_FLOOR_NOTES[self._RULE]
        assert note.startswith("Matched structurally on the command's argv")
        assert "(1) PENDING, retry" in note
        assert "retry this exact command" in note
        # A dotless name refused while a large hosts file is still being parsed
        # off the event loop is transient, so it belongs under (1), not (2).
        assert (
            "outside Windows a dotless name is refused while a hosts file over 64 KiB is "
            "still being read in the background" in note
        )
        assert "wait a minute, retry, and retry again a few seconds later" in note
        assert "(2) Still refused after those retries" in note
        # The minute in the note is the own-address worker's retry backoff: a
        # retry sooner than that cannot start a new check after a failed one.
        assert _argv_floor._OWN_HOST_RESOLVE_BACKOFF_SECS == 60.0
        assert "ssh_config-only alias" in note
        assert "address list cannot be read, so use a resolvable name" in note
        assert (
            "a dotless name on Windows when the hosts file is over 64 KiB, since its content "
            "cannot be verified, so use the full hostname or an IP address" in note
        )
        assert "(3) FORWARDED port" in note
        assert "per-rule toggle in Settings" in note
        # One line: the note is the refusal's second line, which the recovery
        # card and the suite's ``_denied_by`` split on.
        assert "\n" not in note

    @pytest.mark.parametrize(
        "cmd",
        [
            # Regex-tier spellings (target directly after the verb).
            "ssh localhost 'sudo -n true'",
            "ssh 127.0.0.1 whoami",
            "ssh ::1 uptime",
            "ssh user@localhost id",
            "scp localhost:/var/tmp/f .",
            "true; ssh localhost id",
            "ssh $(hostname) id",
            "ssh.exe localhost id",
            # Floor-only spellings (options/quoting the regex cannot resolve).
            "ssh -o StrictHostKeyChecking=no localhost 'sudo -n true'",
            "ssh -p 22 localhost id",
            'ssh "localhost" id',
            "ssh 127.1 whoami",
            "bash -c 'ssh localhost id'",
            "rsync -av /var/tmp/d/ localhost:/var/tmp/b/",
            "sftp user@localhost",
            "ssh ssh://localhost:22 id",
            # Option-shadow fail-closed: a valueless-in-scp/rsync flag must
            # not swallow the target (the shape that defeated round one).
            "scp -r localhost:/dir .",
            "rsync -r localhost:/src /dst",
            "ssh -q localhost id",
            "ssh -vp 22 localhost id",
            # Redirections are removed from argv the way bash removes them.
            "ssh >/dev/null localhost id",
            "ssh > /dev/null localhost id",
            "ssh 2>&1 localhost id",
            "ssh -p 22 localhost>/dev/null",
            # Routing options set the destination regardless of the operand —
            # in both the ``key=value`` and OpenSSH's config-style whitespace
            # spellings, and the attached jump-host form.
            "ssh -ohostname=localhost far-alias id",
            "ssh -o hostname=localhost far-alias id",
            "ssh -o proxyjump=localhost far-host id",
            'ssh -o "Hostname localhost" far-alias id',
            'ssh -o "ProxyJump localhost" far-host id',
            "ssh -Jlocalhost far-host id",
            # getopt BUNDLES valueless short flags in front of the
            # option letter, so ``-voHostname=…`` is ``-v`` + ``-o Hostname=…``
            # and ``-vJhost`` is ``-v`` + ``-J host``.
            "ssh -voHostname=localhost 192.0.2.1",
            "ssh -4voHostname=localhost far-host id",
            "ssh -Aohostname=localhost far-alias id",
            "ssh -vJlocalhost far-host id",
            # a v-led bundle still carries the glued remote-forward
            # spec, and folded ``-C`` must not swallow its neighbor unchecked.
            "ssh -vR2222:localhost:22 far.example.com",
            "ssh -C localhost id",
            # POSIX export-first idiom — ``export NAME`` before the
            # assignment still exports the later value in real bash.
            "export RSYNC_RSH; RSYNC_RSH='ssh localhost'; rsync remote-host:/x .",
            "export RSYNC_CONNECT_PROG; RSYNC_CONNECT_PROG='ssh localhost'; rsync rsync://far.example.com/module /tmp/x",
            # URI authority and @-in-path parsing.
            "rsync rsync://localhost/module/x .",
            "scp -v localhost:/tmp/a@b .",
            # userinfo may CONTAIN a colon (``user:pass@host``) — the
            # destination is after the LAST ``@`` in both the URI authority and
            # OpenSSH's plain ``[user@]host`` form.
            "ssh ssh://user:pass@localhost id",
            "ssh ssh://user:pass@127.0.0.1:2222",
            "ssh user:pass@localhost id",
            "sftp user:pass@::1",
            # IP-literal forms: IPv6, IPv4-mapped, decimal, hex.
            "ssh ::ffff:127.0.0.1 id",
            "ssh 2130706433 id",
            "ssh 0x7f000001 id",
            # An expansion's embedded default is the destination when the
            # variable is unset — bash substitutes it before exec.
            "ssh ${TARGET:-localhost} id",
            'ssh "${TARGET:-localhost}" id',
            "ssh ${H:=127.0.0.1} id",
            # Empty command substitutions expand to nothing, and quote/backslash
            # splices rejoin, so ``s$()sh``/``ss""h``/``s\sh`` all run ``ssh`` and
            # ``local$()host`` resolves to ``localhost``.
            "s$()sh localhost 'id'",
            "ssh local$()host id",
            'ss""h localhost id',
            "s\\sh localhost id",
            # ProxyJump is a comma-separated hop chain dialed from HERE; a self
            # host anywhere in it is a self dial (``-o`` and attached ``-J``).
            "ssh -o proxyjump=localhost,far.example.com far.example.com",
            "ssh -Jlocalhost,far.example.com far.example.com",
            # the DETACHED ``-J value`` spelling (the standard form)
            # must comma-split the hop chain exactly like the attached form --
            # the first hop is dialed from HERE.  A flag bundle ending in the
            # jump letter takes the next token as its value too.
            "ssh -J localhost,far.example.com far.example.com",
            "ssh -4J localhost,far.example.com far.example.com",
            # a same-line literal assignment splices the verb or the
            # operand back together before exec -- resolve what is statically
            # known (``a=s; ${a}sh`` -> ``ssh``; ``h=localhost; ssh $h``).
            "a=s; ${a}sh localhost id",
            "h=localhost; ssh $h id",
            # a one-level function definition whose body dials
            # ``$1`` binds the call's literal argument (``f localhost``).
            'f(){ ssh "$1" id; }; f localhost',
            # bash equally accepts the parenthesis-free keyword
            # form -- same binding.
            'function f { ssh "$1" id; }; f localhost',
            # the ``${N}`` spelling nests a brace pair inside the
            # body, so the def regex must capture BALANCED bodies (one level)
            # instead of stopping at the first ``}``.
            "f(){ ssh ${1} id; }; f localhost",
            # a call still invokes the function behind leading
            # assignment words or invocation keywords -- the binder must skip
            # them instead of requiring the name in word 0.
            'f(){ ssh "$1" id; }; x=1 f localhost',
            'f(){ ssh "$1" id; }; time f localhost',
            'f(){ ssh "$1" id; }; if f localhost; then :; fi',
            # arithmetic EXPRESSIONS stay statically unresolvable, so
            # in a connection-target position they fail closed (the shell
            # would glue ``$((0+1))`` into ``127.0.0.1``).
            "ssh 127.0.0.$((0+1)) id",
            "scp 127.0.0.$((0+1)):/etc/passwd /tmp/x",
            # sftp's UPPERCASE ``-R num_requests`` takes a value; the
            # case-folded classifier must treat a collided letter as
            # VALUE-TAKING, or the value consumes the positional slot and the
            # real host is never checked.  ``ssh -c cipher`` is the same class.
            "sftp -R 64 localhost",
            # uppercase ``-X sftp_option`` (OpenSSH >=9.0)
            # takes a value too; without the table entry the value consumes
            # the positional slot and the self host after it is never checked.
            "sftp -X num_requests=64 localhost",
            "ssh -c aes128-ctr localhost id",
            # ...so any unresolved arithmetic expression in a TARGET position
            # fails closed, remote-looking spellings included.
            "ssh host$((i)) id",
            # ProxyCommand/LocalCommand values are command lines run LOCALLY, so
            # a self ssh inside one reaches the local sshd; scp forwards ``-o``.
            'ssh -o proxycommand="ssh localhost sh" far.example.com',
            'scp -o proxycommand="ssh localhost x" far.example.com:/a /tmp/b',
            # the value's TRANSPORT ENDPOINT decides where the outer
            # session lands even when no ssh-family verb appears in it -- a
            # raw-TCP relay to loopback hands the whole session to the local
            # sshd, so a literal self endpoint anywhere in the value is a self
            # connection regardless of the named (far) target.
            "ssh -o proxycommand='nc 127.0.0.1 22' ignored.example.com",
            "ssh -o proxycommand='socat - TCP:localhost:22' far.example.com id",
            "ssh -o proxycommand='openssl s_client -connect [::1]:22 -quiet' far.example.com",
            # a REMOTE forward's destination is dialed FROM HERE --
            # the far sshd hands each accepted connection back for this client
            # to connect locally, so a self host in the ``-R`` spec (or the
            # spec-less reverse-SOCKS form, where the REMOTE picks every local
            # destination) hands remote users the local unsandboxed sshd.
            "ssh -R 2222:localhost:22 far.example.com id",
            "ssh -R localhost:2222:localhost:22 far-host",
            "ssh -R 2222:[::1]:22 far.example.com",
            "ssh -R 2222 far.example.com",
            "ssh -R2222:127.0.0.1:22 far.example.com",
            "ssh -o remoteforward='2222 localhost:22' far.example.com",
            # rsync execs its ``-e``/``--rsh`` value from HERE (detached,
            # ``--rsh=`` attached, and bundle-final ``-e`` like ``-ave``).
            "rsync -e 'ssh localhost' /tmp/f far.example.com:/p",
            "rsync --rsh='ssh localhost' /tmp/f far.example.com:/p",
            "rsync -ave 'ssh localhost' /tmp/f far.example.com:/p",
            # rsync also reads its remote shell from a leading ``RSYNC_RSH=``
            # environment assignment, which rides BEFORE the verb where the
            # operand walk never sees it -- including the ``env VAR=x prog``
            # spelling that puts the assignment after the word ``env``.
            "RSYNC_RSH='ssh localhost' rsync /tmp/f far:/f",
            "env RSYNC_RSH='ssh localhost' rsync /tmp/f far:/f",
            "RSYNC_RSH='ssh localhost' OTHER=1 rsync /tmp/f far:/f",
            # ``RSYNC_CONNECT_PROG`` is the DAEMON-mode twin — rsync
            # execs its value FROM HERE to reach an rsync:// / host::module
            # destination, exactly as it execs ``RSYNC_RSH``.
            "RSYNC_CONNECT_PROG='ssh localhost nc %H 873' rsync rsync://far.example.com/module /tmp/x",
            "RSYNC_CONNECT_PROG='ssh localhost nc %H 873' rsync far.example.com::module /tmp/x",
            "export RSYNC_CONNECT_PROG='ssh localhost'; rsync rsync://far.example.com/module /tmp/x",
            # the two rsync env vars are INDEPENDENT — a later far
            # assignment must not overwrite an earlier self value, and a bare
            # ``export NAME`` promotes THAT name's value.
            "RSYNC_RSH='ssh localhost' RSYNC_CONNECT_PROG='ssh proxy.example.com nc %H 873' rsync rsync://far.example.com/module /tmp/x",
            "RSYNC_RSH='ssh localhost'; RSYNC_CONNECT_PROG='ssh proxy.example.com'; export RSYNC_RSH; rsync remote-host:/x .",
            # the function binder fails CLOSED at its caps — the 9th
            # definition or the 9th same-name call is not resolvable, so it is
            # denied rather than skipped.
            "a1(){ :;}; a2(){ :;}; a3(){ :;}; a4(){ :;}; a5(){ :;}; a6(){ :;}; a7(){ :;}; a8(){ :;}; go(){ ssh $1; }; go localhost",
            "go(){ ssh $1; }; go h1; go h2; go h3; go h4; go h5; go h6; go h7; go h8; go localhost",
            # braced positionals have no single-digit ceiling in
            # bash -- ${10} and up must bind like $1..$9 do.
            'f(){ ssh "${10}"; }; f a b c d e f g h i localhost',
            'f(){ ssh "${12}" uptime; }; f a b c d e f g h i j k localhost',
            # A glued shell operator after the target is a word BOUNDARY, not
            # part of the hostname, so ``ssh localhost;true`` connects HERE --
            # the own-name compare reads the operator-cut spelling too.
            "ssh localhost;true",
            "printf 'id\\n' | ssh localhost;",
            # OpenSSH runs KnownHostsCommand LOCALLY, exactly like
            # Proxy/LocalCommand, so a self ssh in its value reaches the local
            # sshd -- the ``-o key=value`` and glued ``-okey=value`` spellings
            # alike.
            "ssh -o KnownHostsCommand='ssh localhost id' far-host uptime",
            "ssh -oKnownHostsCommand='ssh localhost id' far-host uptime",
            # A wrapper (``command``/``exec``) between the leading ``RSYNC_RSH``
            # and rsync does not end the simple command, so the assignment still
            # applies when rsync execs -- the self remote-shell runs from here.
            "RSYNC_RSH='ssh localhost' command rsync -a /src/ far:/dst/",
            "RSYNC_RSH='ssh localhost' exec rsync -a /src/ far:/dst/",
            # A non-wrapper word (``timeout 5``) between the assignment and rsync
            # does not clear it either: bash applies a leading assignment to the
            # WHOLE simple command that follows, wrappers of any shape included.
            "RSYNC_RSH='ssh localhost' timeout 5 rsync remote-host:/x .",
            # ``export`` makes the value persist for the rest of the line, so it
            # crosses the ``;`` into the later rsync.
            "export RSYNC_RSH='ssh localhost' ; rsync remote-host:/x .",
            # Regression: the base leading-assignment form stays denied.
            "RSYNC_RSH='ssh localhost' rsync remote-host:/x .",
            # a leading ``RSYNC_RSH`` is inherited into a NESTED
            # ``sh -c``/``bash -c`` payload frame, where rsync execs it FROM HERE.
            # The per-frame pending/export walk re-initialises inside each frame,
            # so a function-scope latch carries the self-targeting value forward.
            "RSYNC_RSH='ssh localhost' sh -c 'rsync host:/x .'",
            "export RSYNC_RSH='ssh localhost'; bash -c 'rsync h:/x .'",
            # ``VAR=value; export VAR`` is the standard POSIX two-step: the
            # bare ``export`` promotes the earlier plain assignment into the
            # environment, so the later rsync execs the self remote shell.
            "RSYNC_RSH='ssh localhost'; export RSYNC_RSH; rsync remote-host:/x .",
            'RSYNC_RSH="ssh localhost"; export RSYNC_RSH ; rsync remote-host:/x .',
            # Userinfo in front of a bare IPv6 loopback: the host is the WHOLE
            # remainder after the ``@`` (``::1``), which a plain colon split
            # would misread as an empty host.
            "ssh user@::1",
            "ssh -p 22 user@::1 id",
            # A separator that was QUOTED (or backslash-escaped) in the source
            # is DATA, not a command boundary -- shlex dequotes it, so the walk
            # must not stop at the token carrying it and miss the real self
            # target that follows.  (Masked before the walk, restored only at
            # the faithful operand/routing checks.)
            "scp 'a;b' localhost:/tmp/x",
            "ssh -o 'remotecommand=id;' localhost",
            # A backslash-escaped double quote inside a double-quoted operand is
            # a LITERAL quote, not a close: the masker must not exit
            # the quote at ``\"`` and then read the following ``;`` as a real
            # separator, which would end the operand walk before the self-host
            # target.
            'scp "a\\";b" localhost:/tmp/x',
            # A quoted separator INSIDE the rsync remote-shell value still
            # denies: the value recurses the floor and finds ssh-to-self.
            "rsync -e 'ssh localhost; true' remote-host:/x .",
            # a parameter-default GLUED to literal text reconstructs
            # the self host -- bash substitutes the default INTO the surrounding
            # word (``local${KC_UNSET:-host}`` -> ``localhost``), so the WHOLE
            # operand is resolved via ``_resolve_param_defaults`` and re-checked,
            # not only each isolated default word.  Every operator spelling and
            # nesting resolves the same way (fixpoint), colon-less included.
            "ssh local${KC_UNSET:-host} id",
            "scp /tmp/f local${U:-host}:/tmp/x",
            "ssh local${U:=host} id",
            "ssh local${U:+host} id",
            "ssh local${U-host} id",
            "ssh ${A:-local${B:-host}} id",
            # brace expansion splices each alternative into the
            # surrounding word before any other expansion, so an operand
            # carrying ``{a,b}``/``{n..m}`` is checked against EACH choice.
            "ssh local{h,}ost id",
            "ssh local{host,box} id",
            "ssh ::{1,2} id",
            "ssh 127.0.0.{1..3} id",
            "ssh 0x7f00000{1..2} id",
            "ssh -o proxyjump=local{h,}ost far.example.com",
            # a single integer literal inside arithmetic expansion is
            # printed decimally by bash (hex/octal spellings normalize), gluing
            # into the surrounding word (``$((0x7f)).0.0.1`` -> ``127.0.0.1``).
            "ssh $((0x7f)).0.0.1 id",
            "scp /tmp/f $((0x7f)).0.0.1:/tmp/x",
        ],
    )
    def test_self_targets_are_denied(self, cmd):
        assert _denied_by(cmd) == self._RULE, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            # The remote command is DATA: "localhost" in a later operand is not
            # a destination, quoted or not.
            "ssh far-host.example.com 'curl localhost:8080/health'",
            "ssh far-host.example.com curl localhost:8080/health",
            "ssh far-host 'grep localhost /var/tmp/hostsfile'",
            # Ordinary remote work stays allowed.
            "ssh clu1767-ops.example.com uptime",
            "scp file.txt far-host:/var/tmp/",
            "rsync -av src/ far-host:/dst/",
            "sftp far-host",
            "ssh -o proxycommand='nc %h %p' far-host id",
            # a transport whose endpoint is FAR stays allowed
            # -- only a literal self endpoint in the value is a self dial.
            "ssh -o proxycommand='nc proxy.example.com 22' far-host id",
            "ssh -o proxycommand='ssh -W %h:%p jump.example.com' far-host id",
            # only ``-R`` dials its destination from here.
            # A far -R destination, an ``-L`` destination (dialed from the FAR
            # side, so ``localhost`` is the far machine), and dynamic ``-D``
            # all stay allowed.
            "ssh -R 2222:far-db.example.com:5432 far.example.com",
            "ssh -L 2222:localhost:22 far.example.com id",
            "ssh -D 1080 far.example.com",
            # a CONNECT_PROG whose endpoint is FAR is not a
            # self dial.
            "RSYNC_CONNECT_PROG='ssh proxy.example.com nc %H 873' rsync rsync://far.example.com/module /tmp/x",
            # WITHIN the binder cap (8 definitions) a far call
            # still resolves and stays allowed.
            "a1(){ :;}; a2(){ :;}; a3(){ :;}; a4(){ :;}; a5(){ :;}; a6(){ :;}; a7(){ :;}; go(){ ssh $1; }; go far-host.example.com",
            # a FAR target in braced position ten binds and
            # resolves as allowed.
            'f(){ ssh "${10}"; }; f a b c d e f g h i far-host.example.com',
            "ssh -l ubuntu far-host id",
            # ``-l`` CONSUMES the rest of its token (a login,
            # not a glued option), an ``@`` inside a far path is data, and a
            # far URI with a colon in its userinfo still routes FAR.
            "ssh -loHostname far-host id",
            "ssh -l ohostname far-host id",
            # folded ``-C`` with a FAR neighbor stays allowed,
            # and a real ``-c`` cipher value neither denies nor eats the far
            # destination.
            "ssh -C far-host.example.com id",
            "ssh -c aes128-ctr far-host.example.com id",
            "scp notes.txt far-host:/backup/a@localhost",
            "ssh ssh://user:pass@far-host.example.com id",
            # Non-connection mentions.
            "grep -r localhost src/",
            "echo ssh localhost",
            "curl http://localhost:8080/api",
            "man ssh",
            # Subset/false-positive guards: a REMOTE host that
            # merely starts with "localhost", an IPv6 that merely starts with
            # "::1", an unrelated program name, an unrelated variable, and a
            # data-naming opt=value.
            "ssh localhost.example.com id",
            "ssh ::10 id",
            "notssh localhost",
            "ssh $HOSTNAME_BACKUP id",
            "rsync --exclude=localhost src/ far-host:/d/",
            # The whitespace routing form is read only in a VALUE SLOT: in
            # operand position a two-word token is remote command data.
            "ssh far-host 'hostname localhost'",
            # Forward/bind specs name listen addresses and far-side hops, not
            # a destination this process connects to from here -- EXCEPT the
            # remote-forward destination, which is on the deny side:
            # ``-R``'s host field is dialed FROM here.
            "ssh -L 127.0.0.1:8080:db:5432 far-host",
            "ssh -D localhost:1080 far-host",
            "ssh -W localhost:22 far-host",
            "ssh -b localhost far-host id",
            "ssh -l ubuntu far-host uptime",
            # A remote-host default in an expansion stays allowed; only the
            # embedded word is checked, and a bare $VAR is the documented
            # run-time residual.
            "ssh ${TARGET:-far-host} id",
            # the whole-operand resolutions only WIDEN the self
            # check -- defaults, braces, and arithmetic naming remote hosts or
            # plain files stay allowed.
            "ssh web${N:-01}.example.com id",
            "scp {a,b}.txt far-host:/x",
            "ssh far{1..3}.example.com uptime",
            # A remote command that merely contains an empty expansion is data,
            # not a self dial.
            "ssh far.example.com 'echo $()'",
            # A ProxyJump chain of only remote hops stays allowed (``-o`` and
            # detached ``-J`` alike).
            "ssh -o proxyjump=far1.example.com,far2.example.com target.example.com",
            "ssh -J far1.example.com,far2.example.com target.example.com",
            # the value after ``-R`` is consumed as a value; the
            # remote destination after it stays allowed.
            "sftp -R 64 far.example.com",
            "sftp -X num_requests=64 far.example.com",
            # assignment/function binding that resolves to a REMOTE
            # host stays allowed -- the resolution only widens the deny.
            "a=far.example.com; ssh $a uptime",
            'f(){ ssh "$1" uptime; }; f far.example.com',
            # the prefix-skipping call scan binds INVOCATIONS only --
            # the function name as another command's argument is data, and a
            # prefixed call bound to a REMOTE host stays allowed.
            'f(){ ssh "$1" uptime; }; echo f localhost',
            'f(){ ssh "$1" uptime; }; x=1 f far.example.com',
            # an arithmetic expression in a NON-target position (a
            # local scp source file) is not a connection target.
            "scp release$((2*3)).tar far.example.com:/dst",
            # arithmetic in the remote PATH (after the colon) glues
            # into a filename, never into the host -- only the pre-colon host
            # part can name this machine.
            "scp backup.tar far.example.com:/backups/part$((i)).tar",
            "rsync -a src/ far.example.com:/data/run$((n))/",
            # rsync ``--rsh`` naming plain ``ssh`` (no self host) is the
            # normal remote-shell selector; ``--exclude`` names data.  (The
            # detached ``-e ssh`` spelling is floor-allowed too -- asserted in
            # test_rsync_detached_rsh_floor_allows_plain_ssh -- and, because the
            # ``reverse-shell-nc`` row is anchored to the ``nc`` command token,
            # allowed end-to-end as well; pinned by
            # TestReverseShellNcIsCommandTokenAnchored.)
            "rsync --rsh=ssh /tmp/f far.example.com:/p",
            "rsync --exclude=localhost /tmp/f far.example.com:/p",
            # A leading ``RSYNC_RSH`` naming a REMOTE shell target is the
            # ordinary selector; and a mention with no live rsync verb (``echo
            # RSYNC_RSH=…``) connects to nothing.
            "RSYNC_RSH='ssh far-host' rsync /tmp/f other:/f",
            "echo RSYNC_RSH='ssh localhost'",
            # A quoted mention that is never executed connects to nothing.
            'echo "ssh localhost"',
            # A glued operator after a REMOTE target is still just a boundary:
            # ``ssh far-host;true`` connects to far-host, not here.
            "ssh far-host;true",
            # KnownHostsCommand naming a non-ssh local helper is not a self dial
            # -- the value recurses the floor and finds no ssh-to-self.
            "ssh -o KnownHostsCommand='/usr/bin/true' far-host uptime",
            # An env-preserving wrapper with a REMOTE ``RSYNC_RSH`` is the
            # ordinary remote-shell selector, not a self dial.
            "RSYNC_RSH='ssh far-host' command rsync -a /s/ d:/d/",
            # A non-exported leading ``RSYNC_RSH`` applies only to its own simple
            # command, so it does NOT cross a ``;`` into a later rsync.
            "RSYNC_RSH='ssh localhost' true ; rsync remote-host:/x .",
            # A leading ``RSYNC_RSH`` naming a REMOTE shell through a wrapper is
            # the ordinary selector, not a self dial.
            "RSYNC_RSH='ssh far-host' timeout 5 rsync remote-host:/x .",
            # a NON-self ``RSYNC_RSH`` inherited into a nested
            # payload stays allowed, and a self value inherited into a payload
            # that runs NO rsync verb connects to nothing.
            "RSYNC_RSH='ssh buildhost22' sh -c 'rsync h:/x .'",
            "RSYNC_RSH='ssh localhost' sh -c 'echo hi'",
            # The POSIX two-step with a REMOTE value is the ordinary selector.
            "RSYNC_RSH='ssh far-host'; export RSYNC_RSH; rsync remote-host:/x .",
            # A bare ``export RSYNC_RSH`` with no assignment anywhere exports
            # an unset variable: rsync falls back to plain ssh of its operand.
            "export RSYNC_RSH; rsync remote-host:/x .",
            # An ``@`` in the PATH of a remote operand is data, not userinfo --
            # even when an IPv6 loopback spelling follows it.
            "scp notes.txt far-host:/backup/a@::1",
            # A QUOTED separator is data, so the walk keeps going past it -- but
            # the target after it is REMOTE, so the command still stays allowed.
            "scp 'a;b' far-host:/tmp/x",
            # A REAL (unquoted) separator still ends the walk, so a self host in
            # the NEXT simple command is not this command's ssh target.
            "scp f.txt far-host:/x; ping localhost",
            "ssh far-host; echo localhost",
        ],
    )
    def test_other_hosts_and_mentions_stay_allowed(self, cmd):
        assert _denied_by(cmd) is None, cmd

    def test_rsync_detached_rsh_floor_allows_plain_ssh(self):
        # THIS floor must not deny the normal detached remote-shell selector, and
        # neither does the ``reverse-shell-nc`` catalog row: it is anchored to the
        # ``nc`` command token, so the letters ``nc -e`` inside ``rsync -e`` are not
        # a match.  Pinned end-to-end here as well as at the floor.
        cmd = "rsync " + "-e ssh /tmp/f far.example.com:/p"
        assert not security._is_ssh_to_self(cmd)
        assert _denied_by(cmd) is None

    def test_mask_quoted_separators_round_trip(self):
        # The mask rewrites only QUOTED / backslash-escaped ``;``/``|`` to
        # sentinels and leaves a real operator alone; unmask is its exact
        # inverse.  This is the mechanism that keeps a shlex-dequoted separator
        # from ending the operand walk early, which would skip the self target.
        mask = security._mask_quoted_separators
        unmask = security._unmask_separators
        semi = security._QUOTED_SEP_SENTINELS[";"]
        pipe = security._QUOTED_SEP_SENTINELS["|"]
        # Single-quoted separators are data -> sentinels.
        assert mask("scp 'a;b' localhost:/x") == "scp 'a" + semi + "b' localhost:/x"
        # Double-quoted separators (both forms) are data -> sentinels.
        assert mask('echo "a;b|c"') == 'echo "a' + semi + "b" + pipe + 'c"'
        # A backslash-escaped separator OUTSIDE quotes is data -> sentinel; the
        # backslash is kept so shlex still de-escapes downstream.
        assert mask("a\\;b") == "a\\" + semi + "b"
        # An UNQUOTED, unescaped separator is a real operator -> untouched.
        masked_real = mask("ssh far; echo x")
        assert masked_real == "ssh far; echo x"
        assert ";" in masked_real and semi not in masked_real
        # An unterminated quote leaves the rest of the string quoted.
        assert mask("ssh 'a;b") == "ssh 'a" + semi + "b"
        # unmask reverses the mask exactly (round-trip identity).
        for s in (
            "scp 'a;b' localhost:/x",
            'echo "a;b|c"',
            "a\\;b",
            "ssh far; echo x",
            "ssh 'a;b",
        ):
            assert unmask(mask(s)) == s

    def test_ipv6_zone_id_is_stripped_before_match(self, monkeypatch):
        # a link-local address carries a ``%zone`` suffix that
        # never equals the bare cached address, so the operand chokepoint
        # (``_host_is_self``) strips it before the own-address compare -- bare
        # and bracketed spellings alike.  An address NOT in the cache stays
        # allowed after stripping, so the strip does not over-block.
        own = security.socket.gethostname().strip().lower()
        pinned = frozenset({"fe80::1"} | {n for n in {own, own.split(".", 1)[0]} if n})
        monkeypatch.setattr(security.argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
        monkeypatch.setattr(security.argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
        assert _denied_by("ssh fe80::1%eth0 whoami") == self._RULE
        assert _denied_by("scp file [fe80::1%eth0]:/tmp/") == self._RULE
        assert _denied_by("ssh fe80::99%eth0 true") is None

    @pytest.mark.parametrize(
        "cmd",
        [
            # A QUOTED token whose text is an ``_ends_argv`` boundary shape is
            # DATA, not a command separator: it must not end the operand walk
            # before the self-host target later in the argv is examined.  Same
            # class as the quoted ``;`` / ``|`` masking above -- scp/rsync take
            # multiple operands, so a junk first operand does not stop the
            # client from connecting to the destination.
            "scp '&' localhost:/tmp/x",
            "scp '#' localhost:/tmp/x",
            "scp '(' localhost:/tmp/x",
            "scp '{' localhost:/tmp/x",
            "scp '\n' localhost:/tmp/x",
            "rsync -av '&' localhost:/tmp/b/",
            'scp "&&" localhost:/tmp/x',
        ],
    )
    def test_quoted_boundary_token_does_not_hide_self_target(self, cmd):
        assert _denied_by(cmd) == self._RULE

    def test_quoted_boundary_token_keeps_remote_allowed(self):
        # Masking a quoted boundary char must not create false denies for the
        # same shape aimed at a REMOTE destination.
        assert _denied_by("scp '&' remote.example.com:/tmp/x") is None

    def test_backslash_newline_continuation_still_resolves_self(self):
        # Backslash-newline OUTSIDE quotes is a line continuation: bash glues
        # ``local\<newline>host`` into ``localhost``.  The mask must leave it
        # alone so the glued token still matches the self-host set.
        assert _denied_by("ssh local\\\nhost id") == self._RULE

    def test_static_substitution_output_cannot_hide_self_host(self):
        # bash splices a substitution's output into the command line before
        # exec, so an ``echo``/``printf`` literal producing a self-host IS
        # the destination.  Flat, statically-decidable substitutions resolve
        # in the source text; dynamic generators stay the documented
        # run-time residual.
        assert _denied_by("ssh $(printf localhost) id") == self._RULE
        assert _denied_by("ssh `echo localhost` id") == self._RULE
        assert _denied_by("ssh local$(printf host) id") == self._RULE
        assert _denied_by("ssh $(printf remote.example.com) id") is None

    def test_here_string_fed_xargs_is_judged_as_the_rebuilt_command(self):
        # bash strips ``<<< word`` from argv and delivers the
        # word on stdin, and xargs turns stdin into ARGUMENTS for the verb it
        # launches -- so ``xargs ssh <<< localhost`` reaches ``ssh localhost``.
        # The floor rebuilds that command from the statically-known pieces and
        # judges it; a far host stays allowed, and stdin that only exists at
        # run time (a pipe) stays the documented run-time residual.
        assert _denied_by("xargs ssh <<< localhost") == self._RULE
        assert _denied_by("xargs ssh <<<localhost") == self._RULE
        assert _denied_by("xargs -I{} ssh {} <<< localhost") == self._RULE
        assert _denied_by("xargs <<< localhost ssh") == self._RULE
        assert _denied_by("sudo xargs ssh <<< localhost") == self._RULE
        assert _denied_by("<<< localhost xargs ssh") == self._RULE
        assert _denied_by("<<< localhost sudo xargs ssh") == self._RULE
        assert _denied_by("xargs -I{} ssh user@{} <<< localhost") == self._RULE
        assert _denied_by("<<< far.example.com xargs ssh") is None
        assert _denied_by("xargs -I{} ssh user@{} <<< far.example.com") is None
        assert _denied_by("xargs ssh <<< remote.example.com") is None

    def test_plain_redirects_still_consume_their_filename(self):
        # The here-string handling must not widen the redirect branch: a
        # detached ``<``/``>`` filename is removed from argv by bash and is
        # not a destination.
        assert _denied_by("ssh remote.example.com < /etc/hosts") is None
        assert _denied_by("scp remote.example.com:/tmp/f . > /tmp/log") is None

    def test_glob_expandable_verb_is_denied(self):
        # bash pathname-expands an unquoted glob against the filesystem
        # before exec, so ``/usr/bin/s?h`` IS ``/usr/bin/ssh`` wherever the
        # client is installed.  A command word whose glob CAN name an
        # ssh-family program must not slip the verb gate.
        assert _denied_by("/usr/bin/s?h localhost id") == self._RULE
        assert _denied_by("/usr/bin/s?h remote.example.com id") is None

    def test_glob_expandable_self_operand_is_denied(self):
        # The same expansion applies to operands: ``localho?t`` matches a
        # file named ``localhost`` in the working directory the agent can
        # create itself.  A pattern that CAN match a self name is a self
        # target; one that cannot stays allowed.
        assert _denied_by("ssh localho?t id") == self._RULE
        assert _denied_by("ssh 127.0.0.? id") == self._RULE
        assert _denied_by("ssh remo?e.example.com id") is None

    def test_fold_ambiguous_flag_does_not_hide_the_destination(self, monkeypatch):
        # ``-C`` (compression, valueless) folds onto value-taking
        # ``-c`` (cipher).  After folding the letter is AMBIGUOUS, so the
        # swallowed token keeps FULL host-position checks (DNS included) and
        # the positional slot stays pending — both candidate destinations are
        # over-checked, the floor's safe direction.
        def _self_only(host, **_kw):
            return host == "self.attacker.example"

        monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", _self_only)
        assert _denied_by("ssh -C self.attacker.example uptime") == self._RULE
        # Both ways: a real cipher value with a self DESTINATION after it.
        assert _denied_by("ssh -c aes128-ctr self.attacker.example id") == self._RULE
        # A far neighbor stays allowed.
        assert _denied_by("ssh -C far.example.com id") is None

    def test_dns_verdict_is_consulted_only_in_host_position(self, monkeypatch):
        # the fail-closed DNS verdict applies to HOSTS -- the
        # ssh/sftp positional, a ``host:path`` prefix, or a routing option
        # value -- never to a dotted LOCAL FILE operand of scp/rsync.  A
        # verdict stub that denies everything proves the layer is not even
        # consulted for file operands: an everyday ``backup.tar.gz`` must
        # not be refused as a first-contact hostname.
        consulted: "list[str]" = []

        def _deny_all(host, **_kw):
            consulted.append(host)
            return True

        monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", _deny_all)
        assert _denied_by("scp backup.tar.gz far.example.com:/dst") == self._RULE
        assert consulted and all(h == "far.example.com" for h in consulted), (
            "only the host:path prefix may reach the DNS layer, got %r" % consulted
        )
        consulted.clear()
        assert _denied_by("rsync -av notes.2026.txt far.example.com:/dst") == self._RULE
        assert all(h == "far.example.com" for h in consulted)
        # An ssh OPTION VALUE is not a destination either (``-i`` takes a
        # value, so the filename fills the value slot, not the host slot).
        consulted.clear()
        assert _denied_by("ssh -i id_rsa.pub far.example.com uptime") == self._RULE
        assert all(h == "far.example.com" for h in consulted)
        # The ssh positional IS a host: the layer must still be consulted
        # there (fail-closed deny with this stub).
        consulted.clear()
        assert _denied_by("ssh unseen.example.com uptime") == self._RULE
        assert any(h == "unseen.example.com" for h in consulted)
        # a VALUELESS flag (``-v``) does not swallow the host slot
        # -- the next token is the destination and must be DNS-checked.
        consulted.clear()
        assert _denied_by("ssh -v self.example id") == self._RULE
        assert any(h == "self.example" for h in consulted)
        # the consumed positional ends host position -- a dotted
        # remote-command argument after the host is data, not a destination.
        consulted.clear()
        assert _denied_by("ssh -v far.example.com hostname.txt") == self._RULE
        assert consulted and all(h == "far.example.com" for h in consulted)
        # a DOTLESS name in host position may still be a loopback
        # alias (/etc/hosts) -- it is resolved like any other hostname.
        consulted.clear()
        assert _denied_by("ssh localalias uptime") == self._RULE
        assert any(h == "localalias" for h in consulted)

    def test_arith_overflow_denies_instead_of_crashing(self):
        # ``int(lit, 16)`` uses a power-of-two base and is exempt
        # from the interpreter's int<->str digit cap, so a huge hex literal
        # converts -- but the decimal ``str()`` of it is capped and would raise
        # an uncaught ValueError THROUGH ``is_denied``, aborting the tool-call
        # evaluation instead of answering.  The conversion fails closed: the
        # unresolved spelling stays, and the target-position rule denies it.
        huge = "ssh 127.0.0.$((0x" + "f" * 3700 + ")) id"
        assert _denied_by(huge) == self._RULE
        # 5000 octal digits ~= 4515 decimal digits -- past the str() cap
        # (4400 would still convert: ~3973 decimal digits).
        huge_octal = "ssh 127.0.0.$((0" + "7" * 5000 + ")) id"
        assert _denied_by(huge_octal) == self._RULE

    def test_numeric_loopback_needs_a_valid_address(self):
        # ``127.example.com`` is an ordinary remote domain, not a loopback
        # spelling: only forms ``inet_aton``/``ip_address`` accept count as
        # numeric loopback.  The abbreviated numeric form stays denied.
        assert _denied_by("ssh 127.example.com id") is None
        assert _denied_by("ssh 127.1 id") == self._RULE

    def test_parameter_default_expansion_cannot_hide_the_verb(self):
        # bash substitutes ``${VAR:-word}`` defaults before exec, so
        # ``s${KC_UNSET:-s}h`` IS ``ssh`` by the time the kernel sees it.
        # The raw-substring verb gate probes the source text and must
        # resolve static defaults the way the operand walk does -- an
        # unresolved probe early-returns and the walk never runs.
        assert _denied_by("s${KC_UNSET:-s}h localhost id") == self._RULE
        assert _denied_by("s${KC_UNSET:-s}h remote.example.com id") is None

    def test_oversized_brace_range_integers_fail_closed(self):
        # ``int()`` refuses digit strings past the interpreter's conversion
        # cap (~4300 digits); uncaught, that ValueError crashes the gate.
        # A range endpoint or step needing thousands of digits cannot name
        # one legitimate target: it must land on the overflow deny.
        big = "9" * 4301
        assert _denied_by(f"ssh h{{1..{big}}} id") == self._RULE
        assert _denied_by(f"ssh h{{1..2..{big}}} id") == self._RULE

    def test_userinfo_colon_does_not_hide_own_host(self, monkeypatch):
        # userinfo may CONTAIN a colon.  OpenSSH resolves the
        # destination AFTER the LAST ``@`` (URI authority and the plain
        # ``[user@]host`` form alike), so ``user:pass@myhost`` routes to
        # myhost while a colon-first split reads the host as ``user``.
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
        monkeypatch.setattr(
            _argv_floor, "_OWN_HOST_NAMES_CACHE", frozenset({"myhost.example.com", "myhost"})
        )
        assert _denied_by("ssh ssh://user:pass@myhost.example.com id") == self._RULE
        assert _denied_by("ssh ssh://user:pass@myhost:2222 id") == self._RULE
        assert _denied_by("ssh user:pass@myhost id") == self._RULE
        assert _denied_by("sftp user:pass@myhost.example.com") == self._RULE
        # Far destinations with the same shape stay allowed, and an ``@``
        # inside a far PATH is data, not userinfo.
        assert _denied_by("ssh ssh://user:pass@otherhost.example.com id") is None
        assert _denied_by("scp f otherhost.example.com:/backup/a@myhost") is None

    def test_mapped_loopback_denial_does_not_rely_on_is_loopback_delegation(self, monkeypatch):
        # Before Python 3.12.4, IPv6Address("::ffff:127.0.0.1").is_loopback is
        # False (no ipv4_mapped delegation).  _host_is_self must unwrap the
        # mapped address itself, so the deny holds on every supported micro.
        # Simulate the old semantics by pinning the IPv6 properties to False.
        import ipaddress as _ipaddress

        monkeypatch.setattr(_ipaddress.IPv6Address, "is_loopback", property(lambda self: False))
        monkeypatch.setattr(_ipaddress.IPv6Address, "is_unspecified", property(lambda self: False))
        assert _denied_by("ssh ::ffff:127.0.0.1 id") == self._RULE

    def test_pattern_is_a_subset_of_the_floor_predicate(self):
        """The catalog-visible pattern must never claim more than the floor.

        Mirror of ``test_retained_pattern_is_a_subset_of_its_predicate``: every
        command the pattern denies must also be denied by ``_is_ssh_to_self``,
        or the displayed text and the enforcement drift apart.
        """
        rx = re.compile(_rule_pattern(self._RULE), re.IGNORECASE)
        corpus = [
            "ssh localhost id",
            "scp localhost x",
            "rsync localhost x",
            "sftp localhost",
            "ssh user@127.0.0.1",
            "ssh ::1",
            "scp localhost:/var/tmp/f .",
            "ssh $(hostname) id",
            "ssh ${HOSTNAME} id",
            "true; ssh localhost",
            "ssh.exe localhost id",
            "dir/ssh localhost",
            "/usr/bin/ssh localhost id",
            "ssh localhost:",
        ]
        for cmd in corpus:
            if rx.search(cmd.lower()):
                assert security._is_ssh_to_self(
                    cmd.lower()
                ), f"pattern matched but predicate did not: {cmd}"

    def test_opt_out_disables_both_tiers(self):
        effective = compute_effective_denied(BUILTIN_DENIED_RULES, (self._RULE,), False, (), ())
        assert is_denied("ssh -p 22 localhost id", denied_regexes=list(effective)) is None
        assert is_denied("ssh localhost id", denied_regexes=list(effective)) is None

    def test_tokenizer_failure_does_not_allow_the_regex_form(self, monkeypatch):
        # Union, not replacement: with the floor's tokenizer down, the raw-text
        # pattern must still catch the adjacent spelling.
        def _boom(_cmd):
            raise ValueError("simulated tokenizer failure")

        monkeypatch.setattr(security, "normalize_shell_command", _boom)
        assert _denied_by("ssh localhost id") == self._RULE

    def test_ansi_c_quoted_verb_is_decoded_before_the_gate(self):
        # bash decodes ANSI-C quoting before exec, and the
        # operand walk's tokenizer resolves it too -- so the raw-substring
        # verb gate must probe the DECODED text, or $'\x73\x73\x68' (ssh) never
        # reaches the walk that would deny it.
        assert _denied_by("$'\\x73\\x73\\x68' localhost id") == self._RULE
        assert _denied_by("$'\\x73\\x63\\x70' /etc/hostname localhost:/tmp/") == self._RULE
        # A remote target through the decoded verb stays allowed, and a
        # non-ssh ANSI-C verb gains nothing from the decode.
        assert _denied_by("$'\\x73\\x73\\x68' far.example.com id") is None
        assert _denied_by("$'\\x6c\\x73' /tmp") is None

    def test_same_line_copied_ssh_binary_is_bound(self):
        # a same-line copy/rename of an ssh-family binary
        # (cp/mv/ln/install) binds the DESTINATION as that verb for the rest
        # of the line -- shedding the basename must not shed the floor.  A
        # copy staged in an EARLIER command line is the documented residual
        # (nothing textual survives across invocations).
        assert _denied_by("cp /usr/bin/ssh /tmp/x && /tmp/x localhost id") == self._RULE
        assert _denied_by("ln -s /usr/bin/scp ./s && ./s notes.txt localhost:/tmp/") == self._RULE
        assert _denied_by("install /usr/bin/ssh /tmp/y; /tmp/y 127.0.0.1") == self._RULE
        # The binding carries the VERB, not a verdict: a remote target
        # through the copied binary stays allowed, and a non-ssh copy binds
        # nothing.
        assert _denied_by("cp /usr/bin/ssh /tmp/x && /tmp/x far.example.com id") is None
        assert _denied_by("cp notes.txt /tmp/x && /tmp/x localhost") is None

    def test_double_dash_is_an_option_terminator(self, monkeypatch):
        # exact ``--`` is the POSIX option terminator; the
        # token after it is the OPERAND.  Classifying it as a long option would
        # let ``value_shadow`` swallow the next token out of host position, so a
        # DNS-classified self alias after ``--`` would never be checked.
        consulted: "list[str]" = []

        def _deny_all(host, **_kw):
            consulted.append(host)
            return True

        monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", _deny_all)
        assert _denied_by("ssh -- alias.example uptime") == self._RULE
        assert any(h == "alias.example" for h in consulted), (
            "the operand after -- must keep host position, consulted=%r" % consulted
        )
        # Literal self targets after ``--`` are denied, remote ones allowed
        # (the deny-all stub above never sees a literal loopback, and the
        # textual layers decide these without it).
        assert _denied_by("ssh -- localhost") == self._RULE
        assert _denied_by("ssh -v -- 127.0.0.1 id") == self._RULE
