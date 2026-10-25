"""Tests that naming a program or a permission verb is not running it.

The product name or a kill verb in a data command's argv is a mention, and a permission verb
handed to a search tool is text; an executor wrapper or an exec-capable consumer still gets
the refusal.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _KA, _NAME, _PK, _TOK, _denied_by

from kiro_crew import security
from kiro_crew.security import BUILTIN_DENIED_RULES, is_denied
from kiro_crew.security import perm_verb_mention as _perm_verb_mention

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


def _named(origin, *cases):
    """Lead each case with the test name it came from, so the old name stays in its id."""
    return [(origin, *c) if isinstance(c, tuple) else (origin, c) for c in cases]


class TestNameAsDataIsNotAnInvocation:
    """The product name in a DATA command's argv is a mention, not an invocation.

    ``echo <name> <verb>`` prints two words. Both halves of the union were
    position-blind about this: the regex matched the two words co-occurring, and the
    argv predicate treated the name as a program wherever it appeared in an argv.

    The classification is a DENYLIST of data consumers rather than an ALLOWLIST of
    executors, on purpose. Many commands hand their remaining argv to an executor
    (``ssh``, ``docker exec``, ``sudo``, ``env``, ``nohup``, ``timeout``,
    ``runuser``, ``chroot``, ``pkexec``, ``xargs``), so enumerating THOSE would make
    a forgotten entry a silent bypass; enumerating data consumers makes a forgotten
    entry a false positive instead — visible and safe.
    """

    @pytest.mark.parametrize(
        ("origin", "cmd"),
        [
            *_named(
                "test_name_and_verb_as_data_allowed",
                f"echo {_NAME} {_TOK}",
                f"echo 'the {_NAME} {_TOK} command mints a credential'",
                f"printf '%s' {_NAME} {_TOK}",
                f"cat notes.md | grep {_NAME} {_TOK}",
                f"git commit -m 'note: {_NAME} {_TOK} rule'",
            ),
            *_named(
                "test_kill_verb_as_data_allowed",
                f"echo {_PK} {_NAME}",
                f"echo 'run {_PK} {_NAME} to stop it'",
            ),
        ],
    )
    def test_names_as_data_allowed(self, origin, cmd):
        assert _denied_by(cmd) is None

    @pytest.mark.parametrize(
        "cmd",
        [
            f"ssh remote-host {_NAME} {_TOK}",
            f"docker exec c {_NAME} {_TOK}",
            f"sudo {_KA} {_NAME}",
            f"KIROCREW_HOME=/tmp/h {_NAME} {_TOK}",
            f"env FOO=1 {_NAME} {_TOK}",
            f"nohup {_NAME} {_TOK}",
            f"timeout 5 {_NAME} {_TOK}",
        ],
    )
    def test_executor_wrappers_still_blocked(self, cmd):
        # These pass their remaining argv to something that runs it, so the name IS
        # reachable as a program.  An executor allowlist would have to name every
        # one of them; the denylist shape means an unrecognised program defaults to
        # "this could execute the name".
        assert _denied_by(cmd) is not None


# Spelled in halves so this file can be grepped and edited without every read of
# it tripping the very rules under test.
_CM = "ch" + "mod"
_CO = "ch" + "own"


class TestPermissionVerbMentionNarrowing:
    """A permission verb handed to a SEARCH tool is text, not an action.

    The ``chmod``/``chown`` rows are ``re.search`` patterns over the
    whole command, so they cannot tell a verb in PROGRAM position from the same word
    handed to ``grep`` as a pattern.  An ordinary audit OF those rules was refused
    as ``User denied tool execution`` — indistinguishable from a human cancelling —
    while preventing nothing, since the same search completes by spelling the verb
    another way.

    ``_DENY_EXCEPTIONS`` could not reach this class: it is a text glob gated on the
    view holding NO shell-active character, and a real search carries ``|`` or
    ``>``.  The narrowing is therefore argv-structural
    (``argv_floor._perm_verb_mention_only``), reusing the frame walk and
    data-consumer primitives the self-protection floor already relies on.

    Every gate in it REFUSES, so it fails closed: an unreadable construct keeps the
    deny.  The must-deny cases below are the bypasses that were reachable while it
    was written, plus the wrapper family the repo's data-consumer denylist exists to
    cover.
    """

    @pytest.mark.parametrize(
        "cmd",
        [
            # The two reported false positives.
            f"git show origin/main:src/x.py | grep -nE '{_CM}|{_CO}|/etc/' | head -50",
            f"grep -rnE 'os\\.{_CO}|/etc/cron' src/ 2>/dev/null | head -60",
            # A search whose verb and path land in the SAME segment, so Pass 2
            # matches too and the Pass 2 wiring is what clears it.
            f"grep -rn '{_CM} /etc/' src/",
            f"grep '{_CO} root /etc/passwd' docs/",
            # A quoted alternation severed mid-literal by the quote-unaware split:
            # the fragment ``chown' /etc/profile.d`` leads with the verb, which is
            # why Pass 2 must judge the WHOLE command rather than the fragment.
            f"grep -nE '{_CM}|{_CO}' /etc/profile.d",
            # A read of a system file alongside the search is still only a read.
            f"grep '{_CM}' docs/ && cat /etc/os-release",
            f"fgrep -n '{_CM}' /etc/passwd",
            f"egrep -n '{_CM}' /etc/passwd",
            f"grep -rn {_CM} /etc/cron.d",
            f"grep -rn '{_CM}|/etc/' src/ | uniq | head -20",
            # ``&`` as a token of its OWN with nothing after it is a real argv
            # boundary, so the uncut-operator refusal must not reach it.
            f"grep -rn '{_CM}|/etc/' src/ &",
            # A real fd duplication ends the token, so the anchored sink
            # allow-list still strips it and these audits stay readable.
            f"grep -rn '{_CM}|/etc/' src/ 2>&1 | head -20",
            f"grep -rn '{_CM}|/etc/' src/ >/dev/null 2>&1",
            f"jq -rn '\"{_CM} 777\"' 2>&1",
            # DOUBLE quotes.  The shell reads ``;&|`` inside them as ordinary
            # text exactly as it does inside single quotes, and this is the more
            # common spelling of the audit the narrowing exists to allow.  It was
            # measured DENIED on the single-quote-only mask.
            f'grep -rnE "{_CM}|/etc/" src/',
            f'grep -nE "{_CM}|{_CO}" src/ | sort | uniq | head -20',
            f'git show origin/main:src/x.py | grep -nE "{_CM}|/etc/" | head -50',
            # File-descriptor DUPLICATION as the frame's redirect.  It names no
            # new destination, so it cannot persist the mention the way a real
            # sink (``> /tmp/s.sh``) can.  Both the uncut-operator refusal and
            # the sink allow-list had to admit it.
            f"grep -rn '{_CM}|/etc/' src/ 2>&1",
            f'grep -rn "{_CM}|{_CO}" src/ &>/dev/null',
            # A second-operand writer with NO operand writes nothing, which
            # is why ``uniq``/``xxd`` are judged on operand count rather than
            # excluded outright: excluding them would refuse these pipelines.
            f"grep -rnE '{_CM}|/etc/' src/ | uniq -c | head",
            f"grep -rnE '{_CM}|/etc/' src/ | xxd",
            f"grep -rnE '{_CM}|/etc/' src/ | uniq f",
            # The mode row carries no path, so it is reached by a search that
            # names the mode instead.  Auditing THIS very file was refused by it.
            f"grep -n '{_CM} 777' src/kiro_crew/security/denied_rules.py",
        ],
    )
    def test_inert_mention_allowed(self, cmd):
        assert _denied_by(cmd) is None

    @pytest.mark.parametrize(
        "cmd",
        [
            f"ack --pager=sh -c '{_CM} 777 /etc/shadow' needle src/",
            f"ag --pager=sh -c '{_CM} 777 /etc/shadow' needle src/",
            f"awk 'BEGIN{{\"{_CM} 777 /etc/shadow\" | getline}}'",
            # ``+cmd`` is the real initial-command spelling for both pagers;
            # ``--cmd=`` is not an option either of them has.
            f"less +'!{_CM} 777 /etc/shadow' payload.txt",
            f"more +'!{_CM} 777 /etc/shadow' payload.txt",
            f"rg --pre sh '{_CM} 777' payload.sh",
            f"sed '1e {_CM} 777 /etc/shadow' /dev/null",
            # Option-named sinks.  macOS ``base64 -o`` writes an arbitrary path
            # and ``yq -i`` rewrites its operand, so the protected path the
            # matched row named is written while no verb sits in program
            # position.  Operand counting cannot see either: the target is a
            # FLAG's argument.  Measured ALLOWED before these two were excluded.
            f"base64 {_CM} -o /etc/shadow",
            f"base64 -i {_CM} -o /usr/local/bin/git",
            f"base64 {_CM} --output=/etc/shadow",
            f"yq -i '.x = \"{_CM} 777\"' /etc/passwd",
            f"yq --inplace '.x = \"{_CM} 777\"' /etc/passwd",
            # ``file -C -m NAME`` COMPILES the magic file and truncates
            # ``NAME.mgc``.  The destination is the ``-m`` flag's argument plus a
            # suffix the operand never spells, so neither operand counting nor a
            # sink-token check reaches it.  Measured ALLOWED before ``file`` was
            # excluded, and DENIED on ``origin/main``.
            f"file -C -F {_CM} -m ./usr/custom.magic",
            f"file -C -F {_CM} -m /usr/share/misc/magic",
            f"file -C -F {_CO} -m /etc/magic",
            f"file --compile --separator {_CM} --magic-file /usr/local/share/magic",
            # The sink allow-list is substituted out of the WHOLE frame text, so
            # an UNANCHORED alternative consumed a prefix of a longer token and
            # carried that token's own ``>`` away with it.  ``>&1x`` is not a
            # redirection -- the shell writes to a file named ``&1x`` -- and
            # ``>/dev/nullx`` is the same shape one alternative over.  All
            # measured ALLOWED before the token-boundary lookahead, DENIED on
            # ``origin/main``.
            f"jq -rn '\"{_CM} 777 /etc/x\"' >&1x",
            f"jq -rn '\"{_CM} 777 /etc/x\"' >&2y",
            f"jq -rn '\"{_CM} 777 /etc/x\"' 1>&2z",
            f"grep -h '{_CM} 777 /etc/x' f >&1evil",
            f"grep -h '{_CO} root /etc/x' f 2>&1payload",
            f"jq -rn '\"{_CM} 600 /etc/shadow\"' >/dev/nullx",
            f"jq -rn '\"{_CM} 600 /etc/shadow\"' >>/dev/nullish",
            # The verb must be the PROGRAM ``--compress-program`` names.  With
            # ``=sh`` instead, sh is run over sort's own temporaries and the
            # operand is read as an input filename, so nothing executes the verb.
            f"sort --compress-program='{_CM} 777 /etc/shadow' big.txt",
        ],
    )
    def test_exec_capable_consumer_mentions_stay_denied(self, cmd):
        """A consumer that can spawn a helper never exonerates a mention.

        Each spelling above was measured ALLOWED with its program removed from
        ``_PERM_VERB_MENTION_EXCLUDED_PROGRAMS``, so every entry is load-bearing
        rather than defensive: no existing gate withdraws the exemption for it.
        """
        assert _denied_by(cmd) is not None

    @pytest.mark.parametrize(
        ("origin", "cmd"),
        [
            *_named(
                "test_exec_capable_exclusion_costs_these_audit_spellings",
                # The audit spellings this exclusion COSTS.  All four were allowed
                # before the exec-capable programs were excluded and are refused now.
                # Kept as assertions so the cost is recorded in code, not only in the
                # PR description: an option allow-list would recover them but fails
                # OPEN on an option nobody enumerated, which is the wrong direction
                # for a deny rule.  ``grep``/``egrep``/``fgrep`` remain exempt and do
                # the same job.
                f"rg -n '{_CM}|{_CO}' /etc/profile.d",
                f"sed -n '/{_CM}/p' /etc/passwd",
                f"awk '/{_CM}/ {{print $1}}' /etc/passwd",
                # The two option-named sinks cost their piped spellings as well.
                # ``jq`` (no in-place flag), ``xxd`` and ``strings`` do the same job.
                f"grep -rn '{_CM}|/etc/' src/ | base64",
                f"grep -rn '{_CM}|/etc/' src/ | yq",
                # ``sort`` here is a later PIPELINE STAGE with no verb in its own
                # argv.  The walk asks its question of the whole command, so one
                # non-exempt frame refuses all of it -- the same pre-existing
                # behaviour ``| tee`` already had.
                f"grep -rn '{_CM}|/etc/' src/ | sort | uniq | head -20",
            ),
            *_named(
                "test_real_invocation_still_denied",
                # Passthrough wrappers: ``_argv_programs`` names the WRAPPER as the
                # program, and no wrapper is a data consumer, so the verb is reachable.
                f"sudo {_CM} 600 /etc/shadow",
                f"env X=1 {_CM} 600 /etc/shadow",
                f"timeout 5 {_CO} root /etc/passwd",
                f"nice -n 5 {_CO} root /etc/passwd",
                f"nohup {_CO} root /etc/passwd",
                f"runuser -u root -- {_CM} 600 /etc/shadow",
                f"chroot / {_CM} 600 /etc/shadow",
                f"pkexec {_CM} 600 /etc/shadow",
                f"systemd-run {_CM} 600 /etc/shadow",
                f"doas {_CM} 600 /etc/shadow",
                f"su -c '{_CM} 600 /etc/shadow'",
                f"echo x | xargs {_CM} 600 /etc/shadow",
                f"find . -name x -exec {_CM} 600 /etc/shadow ;",
                # A LEADING assignment is skipped by ``_argv_programs``, so the verb
                # becomes its own command's program — and ``chmod`` IS listed in
                # ``_DATA_CONSUMER_PROGRAMS`` (as a mover whose arguments are paths),
                # so without ``_PERM_VERB_MENTION_EXCLUDED_PROGRAMS`` the verb would
                # exonerate itself.  This was a real bypass during development.
                f"X=1 {_CM} 600 /etc/shadow",
                f"A=1 B=2 {_CM} 600 /etc/shadow",
                # Nested shell payloads, reached by the frame walk.
                f"bash -c '{_CM} 000 /etc/shadow'",
                f'sh -c "{_CM} 000 /etc/shadow"',
                f"eval '{_CM} 600 /etc/shadow'",
                f"echo $({_CM} 600 /etc/shadow)",
                f"cat <({_CM} 600 /etc/shadow)",
                f"echo `{_CM} 600 /etc/shadow`",
                # Frame 0 reads as pure data here (the argument opens with a quote, not
                # with the substitution), so only the NESTED frame catches it.
                f"echo \"$(bash -c '{_CM} 600 /etc/shadow')\"",
                f"sh <<EOF\n{_CM} 600 /etc/shadow\nEOF",
                # A GLUED control operator.  ``_ends_argv`` cuts an argv on a glued
                # ``|`` or ``;`` but on ``&`` only as a token of its own (so ``2>&1``
                # stays a redirection), and bash really does start a new command at
                # ``d&``.  Without the uncut-operator refusal every token after it is
                # attributed to ``ls`` and the real invocation reads as inert data --
                # measured ALLOWED on the pre-fix commit for all four spellings.
                f"ls /etc/profile.d& {_CM} -R g+w /etc/profile.d",
                f"ls /etc/profile.d&& {_CM} -R g+w /etc/profile.d",
                f"cat /tmp/f& {_CO} root:root /etc/profile.d",
                f"ls /tmp& {_CM} 777 ~",
                f"grep -rn {_CM} src/& sudo {_CM} 600 /etc/shadow",
                # ... and the same glue inside a nested payload.
                f"bash -c 'ls /etc/profile.d& {_CM} -R g+w /etc/profile.d'",
                # A glued ``|`` or ``;`` hides the program that FOLLOWS it, and the
                # first fix asked the wrong function about it.  ``_ends_argv``
                # answers True for ANY token carrying ``|``, so
                # ``not _ends_argv(token)`` never refused ``f|bash`` -- while
                # ``_argv_programs`` still never records ``bash`` as a program,
                # because it opens a new frame only BETWEEN whole tokens.  The
                # spaced spelling ``f | bash`` was denied throughout, so deleting
                # two spaces was the entire bypass.  Measured ALLOWED on the
                # pre-fix commit for every spelling here.
                f"grep -h '{_CM} 600 /etc/shadow' f|bash|wc",
                f"grep -h '{_CM} 600 /etc/shadow' f|sh",
                f"grep -h '{_CM} 777 /etc/x' f|python3",
                f"grep -h '{_CM} 600 /etc/shadow' f;bash",
                f"bash -c \"grep -h '{_CM} 600 /etc/shadow' f|bash\"",
                # A data consumer handed BOTH an input and a sink is mutating its
                # second operand, whatever its first one is named.  ``xxd in out``
                # and ``uniq in out`` truncate ``out``, so the protected path in
                # each of these is written even though no verb sits in program
                # position -- the verb is the INPUT file's name, which is exactly
                # why every position gate reads the command as inert.  Measured
                # ALLOWED on the pre-fix commit.
                f"xxd {_CM} /usr/local/bin/git",
                f"xxd {_CO} /usr/local/bin/git",
                f"uniq {_CM} /usr/local/bin/git",
                f"uniq -c {_CM} /usr/local/bin/git",
                # A bare ``-`` names stdin as the INPUT, so the next word is still
                # the sink and must count as an operand.
                f"xxd - {_CM}/etc/shadow",
                # ... and the writer is equally reachable as a later pipeline stage
                # or from inside a nested payload.
                f"grep -h '{_CM} 600 /etc/shadow' f | xxd a b",
                f"bash -c 'xxd {_CM} /usr/local/bin/git'",
                # A verb RE-SPELLED so no ``{_CM}`` WORD appears runs all the same,
                # and Pass 2 matches the deny on the quote-normalized view.  With an
                # inert mention placed AFTER it, every per-token gate looked at the
                # mention and the downstream sweep started past it, so the real
                # invocation upstream was never examined -- measured ALLOWED, and the
                # key really went to mode 777.  The mirrored order was already denied,
                # which is what made the asymmetry the tell.
                f"ch\"\"mod 777 ~/.ssh/id_rsa ; grep -h '{_CM} 777 /etc/x' f",
                f"ch''mod 777 ~/.ssh/id_rsa ; grep -h '{_CM} 777 /etc/x' f",
                f"ch'mod' 777 ~/.ssh/id_rsa ; grep -h '{_CM} 777 /etc/x' f",
                f"\"ch\"mod 777 ~/.ssh/id_rsa ; grep -h '{_CM} 777 /etc/x' f",
                f"ch\\mod 777 ~/.ssh/id_rsa ; grep -h '{_CM} 777 /etc/x' f",
                f"ch$()mod 777 ~/.ssh/id_rsa ; grep -h '{_CM} 777 /etc/x' f",
                f"/bin/ch\"\"mod 777 ~/.ssh/id_rsa ; grep -h '{_CM} 777 /etc/x' f",
                f"ch\"\"own root /etc/passwd ; grep -h '{_CM} 777 /etc/x' f",
                f"ch\"\"mod 777 ~/.ssh/id_rsa && grep -h '{_CM} 777 /etc/x' f",
                # Widening the mask to double quotes must not reach a token whose
                # quotes do NOT suppress expansion: a substitution inside ``"..."``
                # runs, so its operators are live.
                f'grep "$(ls /etc/x& {_CM} -R g+w /etc/x)" f',
                f'grep "`{_CM} 600 /etc/shadow`" f',
                # Two double-quoted literals glued around a REAL pipe must not read
                # as one literal -- the trap the no-inner-quote condition guards.
                f'grep "a"|"b" {_CM} 600 /etc/shadow',
                # A bare ``&`` survives the redirect strip, so a duplication sitting
                # beside it does not buy the frame an exemption.
                f"ls /etc/x&2>&1 {_CM} -R g+w /etc/x",
                f"ls /etc/x& {_CM} -R g+w /etc/x 2>&1",
                # A real sink is judged on its own token, so admitting ``2>&1`` does
                # not admit the file beside it.
                f"grep -rn x src/ > /tmp/s.sh 2>&1; {_CM} 600 /etc/shadow",
                # Chaining: the embedded invocation leads its own argv.
                f"grep -rn {_CM} src/ ; {_CM} 600 /etc/shadow",
                f"grep -rn {_CM} src/ && {_CM} 600 /etc/shadow",
                f"grep -rn {_CM} src/ || sudo {_CO} root /etc/passwd",
                f"grep -rn x src/;{_CM} 600 /etc/shadow",
                f"grep x f && ({_CM} 600 /etc/shadow)",
                f"grep x f && {{ {_CM} 600 /etc/shadow; }}",
                # A newline is a separator ``shlex`` consumes as whitespace, so the
                # joined text is refused outright and Pass 2 judges it line by line.
                f"grep -rn {_CM} src/\n{_CM} 600 /etc/shadow",
                f"{_CM} 600 \\\n/etc/shadow",
                # Unbalanced quotes: the argv would be a guess.
                f"grep -nE '{_CM} /etc/shadow",
                # UNQUOTED, so the ``|`` really is a pipe and ``chown /etc/passwd``
                # really runs.  Indistinguishable from the quoted form after POSIX
                # quote removal, which is why the walk tokenizes quotes-retained and
                # masks operators only inside a proven single-quoted literal.
                f"grep -nE {_CM}|{_CO} /etc/passwd",
                f"grep 'x'|'{_CM}' /etc/shadow",
                # A double-quoted token still expands, so it gets no mask.
                f'grep "x|$({_CM} 600 /etc/shadow)" f',
                # Emitters would turn the mention into a script on disk, and ``>`` is
                # not a segment separator — the case ``_INERT_SEARCH_VERBS`` declined
                # to open.  Any redirect but ``/dev/null`` refuses.
                f"echo '{_CM} 600 /etc/shadow' > /tmp/s.sh",
                f"printf '{_CM} 600 /etc/shadow' >> /tmp/s.sh",
                f"grep -h '{_CM} 600 /etc/shadow' f > /tmp/s.sh",
                f"tee /etc/x <<< '{_CM} 600 /etc/shadow'",
                # A downstream stage can EXECUTE what the search emitted.
                f"grep -h '{_CM} 600 /etc/shadow' f | sh",
                f"grep -h '{_CM} 600 /etc/shadow' f | python",
                f"echo '{_CM} 600 /etc/shadow' | bash",
                # A "data consumer" that can execute is not one for this command.
                f"awk 'begin{{system(\"{_CM} 600 /etc/shadow\")}}'",
                f"awk '{{print | \"{_CM} 600 /etc/shadow\"}}' f",
                # Filesystem mutators are excluded: their arguments are destinations.
                f"cp {_CM} /etc/shadow",
                f"mv {_CM} /etc/shadow",
                # Plain and re-spelled invocations.
                f"{_CM} 600 /etc/shadow",
                f"/bin/{_CM} 600 /etc/shadow",
                f"'{_CM}' 600 /etc/shadow",
                f"$'{_CM}' 600 /etc/shadow",
                f'ch""{_CM[2:]} 600 /etc/shadow',
                f"{_CM} --reference=/etc/passwd f",
                f"{_CM} -R g-w /etc/ssl",
                f"{_CO} 0:0 /etc/sudoers",
                # Every one of the six protected roots, on both verbs.
                f"sudo {_CM} 755 /usr/bin/ls",
                f"sudo {_CM} 600 /boot/vmlinuz",
                f"sudo {_CM} 640 /sbin/init",
                f"sudo {_CM} 644 /lib/libc.so",
                f"sudo {_CM} 644 /lib64/ld.so",
                f"sudo {_CO} root /usr/bin/ls",
                f"sudo {_CO} root /boot/vmlinuz",
                f"sudo {_CO} root /sbin/init",
                f"sudo {_CO} root /lib/libc.so",
                f"sudo {_CO} root /lib64/ld.so",
                # The mode row, whose exposure needs no system path at all.  It is in
                # the carve-out too, so its real spellings need their own cover.
                f"{_CM} 777 ~",
                f"sudo {_CM} 777 ~",
                f"bash -c '{_CM} 777 ~'",
                f"echo '{_CM} 777 ~' > /tmp/s.sh",
                f"grep -rn {_CM} src/ && {_CM} 777 ~",
            ),
        ],
    )
    def test_exec_capable_spellings_still_denied(self, origin, cmd):
        assert _denied_by(cmd) is not None

    def test_permission_verb_mention_program_membership_is_pinned(self):
        expected = [
            "basename",
            "cat",
            "column",
            "comm",
            "cut",
            "diff",
            "dirname",
            "du",
            "egrep",
            "fgrep",
            "fold",
            "grep",
            "head",
            "jq",
            "ls",
            "md5sum",
            "nl",
            "od",
            "readlink",
            "realpath",
            "sha256sum",
            "stat",
            "strings",
            "tac",
            "tail",
            "tr",
            "uniq",
            "wc",
            "xxd",
        ]

        assert sorted(_perm_verb_mention._PERM_VERB_MENTION_PROGRAMS) == expected, (
            "Decide whether each new _DATA_CONSUMER_PROGRAMS member can execute "
            "a helper, mutate the filesystem, or name a SINK with an option; if "
            "so, add it to _PERM_VERB_MENTION_EXCLUDED_PROGRAMS."
        )

    def test_narrowing_is_scoped_to_the_permission_verb_rules(self):
        """The opt-in set is DERIVED from the catalog, never hand-listed.

        Hand-listing those regex literals would silently stop covering a row that is
        renamed or added, which is why the selector reads the catalog.  The
        selector is anchored on the VERB and blind to the pattern's tail, so a row
        whose target spelling is revised stays covered -- the mode row's pattern is
        under revision to admit flag spellings, and a tail-keyed selector would
        have dropped it on that rebase with no test noticing.
        """
        from kiro_crew.security.denied_rules import (
            _PERM_VERB_MENTION_PATTERNS,
            _PERM_VERB_MENTION_RULES,
            _PERM_VERB_MENTION_VERBS,
        )

        assert {rule.id for rule in _PERM_VERB_MENTION_RULES} == {
            f"local-destructive-{verb}-{root}"
            for verb in (_CM, _CO)
            for root in ("usr", "etc", "sbin", "boot", "lib", "lib64")
        } | {f"local-destructive-{_CM}-777"}
        assert len(_PERM_VERB_MENTION_PATTERNS) == 13
        assert _PERM_VERB_MENTION_VERBS == {_CM, _CO}
        # Every opted-in row is a permission-verb row, and no row of that
        # shape is left out -- the property the derivation exists to hold.
        assert _PERM_VERB_MENTION_PATTERNS == {
            rule.pattern
            for rule in BUILTIN_DENIED_RULES
            if rule.category == "local-destructive" and rule.pattern.startswith((_CM, _CO))
        }

    def test_no_other_rule_is_narrowed(self):
        """The adapter answers False for every pattern outside the opt-in set.

        This is the guarantee that a change to the permission rules cannot leak
        into another category: ``is_denied`` consults the argv predicate only after
        this membership test.
        """
        from kiro_crew.security import _perm_verb_mention_narrows
        from kiro_crew.security.denied_rules import _PERM_VERB_MENTION_PATTERNS

        text = f"grep -rn '{_CM}|/etc/' src/"
        # The predicate itself says "inert mention" for this text …
        assert _perm_verb_mention._perm_verb_mention_only(text) is True
        # … yet every non-opted-in pattern is unaffected by that answer.
        for rule in BUILTIN_DENIED_RULES:
            if rule.pattern in _PERM_VERB_MENTION_PATTERNS:
                continue
            assert _perm_verb_mention_narrows(rule.pattern, text, {}) is False

    def test_exemption_requires_a_successful_audit(self, monkeypatch):
        """A failed SEL write must keep the deny (fail-closed), as for the globs.

        ``_emit_deny_exception_event`` returns False when the audit cannot be
        written, and the carve-out is gated on it in BOTH passes.  Without that,
        an exemption could be granted with no record of it.
        """
        monkeypatch.setattr(
            security,
            "_emit_deny_exception_event",
            lambda _tool, _pattern, _mechanism=None: False,
        )
        assert _denied_by(f"grep -rn '{_CM} /etc/' src/") is not None

    def test_audit_names_this_narrowing_not_the_glob_map(self, monkeypatch):
        """The SEL record must say WHICH narrowing allowed the command.

        Two unrelated mechanisms reach one emitter -- the glob carve-out map and
        this argv-structural reading.  Recorded under a single name, the audit
        trail cannot answer the only question it exists for.
        """
        seen: list[str] = []

        def _record(_tool, _pattern, mechanism="_DENY_EXCEPTIONS"):
            seen.append(mechanism)
            return True

        monkeypatch.setattr(security, "_emit_deny_exception_event", _record)
        # Pass 1 clears this one (verb and path in one segment reaches Pass 2 too).
        assert _denied_by(f"grep -rn '{_CM} /etc/' src/") is None
        assert seen, "the exemption was granted without reaching the emitter"
        assert set(seen) == {security._PERM_VERB_MENTION_MECHANISM}
        assert security._PERM_VERB_MENTION_MECHANISM != "_DENY_EXCEPTIONS"

    def test_mention_walk_is_bounded_and_the_bound_only_refuses(self):
        """Past the length bound the deny stands, so padding buys nothing.

        The walk descends every nested payload, and the self-protection floor it
        shares that descent with SKIPS it for text carrying no expansion machinery
        (``_self_floor_can_fire``).  Without a bound, a 20k command of plain words
        would buy a descent today's gate never performs.  The bound can only
        withhold the exemption, which is why it is safe to have at all.
        """
        short = f"grep -rn '{_CM} /etc/' src/"
        padded = short + " " + "a" * _perm_verb_mention._PERM_VERB_MENTION_MAX_CHARS

        assert _perm_verb_mention._perm_verb_mention_only(short) is True
        assert _perm_verb_mention._perm_verb_mention_only(padded) is False
        # …and the bound's effect at the gate is a DENY, never an allow.
        assert _denied_by(short) is None
        assert _denied_by(padded) is not None

    def test_uncut_control_operator_asks_the_question_itself(self):
        """The refusal reads the token directly; it does not delegate to ``_ends_argv``.

        ``_ends_argv`` answers "where does this argv END", and for ``f|bash`` the
        answer is correctly yes.  This walk needs the opposite fact -- "is every
        program in this frame one ``_argv_programs`` can SEE" -- and ``f|bash``
        fails it, because ``_argv_programs`` opens a new frame only between whole
        tokens.  Delegating produced a live bypass, so the predicate now compares
        the token against the operator tokens the tokenizer hands over alone.
        """
        from kiro_crew.security.perm_verb_mention import _uncut_control_operator
        from kiro_crew.security.shell_normalizer import _ends_argv

        # The gap: an operator glued inside a word, whichever operator it is.
        assert _uncut_control_operator("/etc/profile.d&") is True
        assert _uncut_control_operator("/etc/profile.d&&") is True
        assert _uncut_control_operator("f|bash") is True
        assert _uncut_control_operator("f;bash") is True
        assert _uncut_control_operator("f||bash") is True
        # ...and the delegation that missed two of them: ``_ends_argv`` says the
        # argv ends at ``f|bash``, which was read as "nothing is hidden here".
        assert _ends_argv("/etc/profile.d&") is False
        assert _ends_argv("f|bash") is True
        # Not the gap: an operator token standing ALONE is a boundary the
        # tokenizer already hands over, or there is no operator at all.
        assert _uncut_control_operator("&") is False
        assert _uncut_control_operator("&&") is False
        assert _uncut_control_operator("|") is False
        assert _uncut_control_operator("||") is False
        assert _uncut_control_operator(";") is False
        assert _uncut_control_operator("/etc/profile.d") is False
        # A quoted alternation is masked BEFORE the question is asked, which is
        # what keeps the exemption this PR exists to grant.
        from kiro_crew.security.perm_verb_mention import _mask_quoted_operators

        assert _uncut_control_operator(_mask_quoted_operators(f"'{_CM}|{_CO}'")) is False
        # A duplication carries an ``&`` that starts no command, so the refusal
        # steps over it -- but only the fixed shapes, never a bare ``&``.
        assert _uncut_control_operator("2>&1") is False
        assert _uncut_control_operator(">&2") is False
        assert _uncut_control_operator("/etc/x&2>&1") is True

    def test_a_frame_that_runs_the_verb_voids_the_exemption(self):
        """Program position is asked through the tokenizer the deny VIEWS use.

        The per-token gates key on ``_PERM_VERB_WORD_RE`` over RAW text, which a
        re-spelling defeats without changing what runs.  This predicate asks
        ``_shell_tokens`` + ``_argv_programs`` instead -- the same tokenizer the
        normalized deny view is built on -- so coverage tracks that view rather
        than a hand-listed set of glue spellings.
        """
        from kiro_crew.security.perm_verb_mention import _frame_voids_perm_verb_mention

        for spelling in (
            'ch""mod 777 ~/.ssh/id_rsa',
            "ch''mod 777 ~/.ssh/id_rsa",
            "ch'mod' 777 ~/.ssh/id_rsa",
            '"ch"mod 777 ~/.ssh/id_rsa',
            "ch\\mod 777 ~/.ssh/id_rsa",
            "ch$()mod 777 ~/.ssh/id_rsa",
            '/bin/ch""mod 777 ~/.ssh/id_rsa',
            'ch""own root /etc/passwd',
            f"{_CM} 777 ~/.ssh/id_rsa",
        ):
            assert _frame_voids_perm_verb_mention(spelling) is True, spelling

        # An audit puts the verb in an ARGUMENT, never in program position, so
        # the refusal must not reach any of these.
        for spelling in (
            f"grep -rnE '{_CM}|{_CO}|/etc/' src/ | head -20",
            f'grep -rnE "{_CM}|{_CO}|/etc/" src/',
            f"git show main:src/x.py | grep -nE '{_CM}|/etc/' | head -50",
            f"grep -rnE '{_CM}|/etc/' src/ | uniq -c | head",
            f"grep -rn {_CM} /etc/cron.d",
            f"grep -nE '{_CM}|{_CO}' /etc/profile.d",
            f"grep -n '{_CM} 777' src/kiro_crew/security/denied_rules.py",
        ):
            assert _frame_voids_perm_verb_mention(spelling) is False, spelling

    def test_an_unresolved_program_voids_the_exemption(self):
        """A program the scan cannot resolve is an unknown command, so deny stands.

        ``ch?od`` runs the verb whenever a matching name exists in the working
        directory, and ``${x}chmod`` whenever the environment supplies the prefix.
        Neither is visible to any de-glue: the name is decided outside the text.
        An audit never puts a glob or an expansion in program position, so the
        refusal does not reach one.
        """
        from kiro_crew.security.perm_verb_mention import _frame_voids_perm_verb_mention

        for spelling in (
            "ch?od 777 ~/.ssh/id_rsa",
            "chm[o]d 777 ~/.ssh/id_rsa",
            "ch*od 777 ~/.ssh/id_rsa",
            "${x}ch''mod 777 ~/.ssh/id_rsa",
        ):
            assert _frame_voids_perm_verb_mention(spelling) is True, spelling

        for spelling in (
            f"grep -rnE '{_CM}|{_CO}|/etc/' src/ | head -20",
            f"grep -rn {_CM} /etc/cron.d",
            f"grep -rnE '{_CM}|/etc/' src/ | uniq -c | head",
        ):
            assert _frame_voids_perm_verb_mention(spelling) is False, spelling

    def test_a_deglued_verb_is_judged_by_the_same_position_gates(self):
        """A token that only DE-QUOTES to the verb enters the per-token loop.

        This is what closes the wrapper spellings.  ``command``, ``env``, ``exec``,
        ``nohup``, ``time``, ``nice``, ``sudo``, ``xargs`` and ``find -exec`` each
        leave the verb at an ARGUMENT position, so the frame's program is the
        wrapper and the program-position gate never fired on the verb at all.
        Widening the loop's TRIGGER hands those spellings to the gates that already
        exist -- no wrapper is an accepted data consumer -- instead of asking a
        hand-kept wrapper list to stay complete.
        """
        from kiro_crew.security.perm_verb_mention import _deglues_to_perm_verb

        assert _deglues_to_perm_verb(f'ch""{_CM[2:]}') is True
        assert _deglues_to_perm_verb("ch''mod") is True
        # Spells the verb outright, so the raw-text trigger already has it.
        assert _deglues_to_perm_verb(_CM) is False
        # No glue character at all: the pre-filter answers without tokenizing.
        assert _deglues_to_perm_verb("--include=*.py") is False

    def test_wrapper_prefixed_glue_spellings_stay_denied(self):
        """Each wrapper spelling was measured EXECUTING before this gate existed.

        A read-only verifier took a scratch file from 0o600 to 0o777 with rc=0 for
        every prefix below, while the command as a whole was ALLOW -- the mention
        appended after the invocation exonerated the frame.  The mention used here
        is drawn from this class's own must-allow list, which is what made the
        bypass realistic: the bait is an audit the narrowing is meant to permit.
        """
        mention = f"grep -rn {_CM} /etc/cron.d"
        glue = f'ch""{_CM[2:]}'
        glue_sq = f"ch''{_CM[2:]}"
        for invocation in (
            f"command {glue} 777 ~/.ssh/id_rsa",
            f"env {glue_sq} 777 ~/.ssh/id_rsa",
            f"env A=1 {glue} 777 ~/.ssh/id_rsa",
            f"exec {glue} 777 ~/.ssh/authorized_keys",
            f"nohup {glue_sq} 777 ~/.ssh/id_rsa",
            f"time {glue_sq} 777 /etc/shadow",
            f"nice {glue_sq} 777 ~/.ssh/id_rsa",
            f"sudo {glue_sq} 777 /etc/shadow",
            f"timeout 5 {glue_sq} 777 ~/.ssh/id_rsa",
            f"builtin {glue_sq} 777 ~/.ssh/id_rsa",
            f"xargs {glue_sq} 777 < f",
            f"find ~/.ssh/id_rsa -exec {glue_sq} 777 {{}} +",
            f'command ch""{_CO[2:]} root /etc/shadow',
        ):
            assert is_denied(f"{invocation} ; {mention}"), invocation
            # ... and with the mention piped rather than sequenced.
            assert is_denied(f"{invocation} ; {mention} | head -20"), invocation

    def test_second_operand_writers_are_judged_on_operand_count(self):
        """``uniq``/``xxd`` keep the exemption only while they write nothing.

        Both are in the accepted set on purpose.  Excluding them outright would
        refuse the exemption's commonest shape (``... | uniq | head``), and
        keeping them unconditionally would allow ``xxd <verb> <protected path>``
        to truncate that path.  Operand count separates the two, per command, so
        a later pipeline stage with no operand is unaffected.
        """
        import shlex

        from kiro_crew.security.perm_verb_mention import (
            _PERM_VERB_MENTION_PROGRAMS,
            _SECOND_OPERAND_WRITER_PROGRAMS,
            _writes_a_second_operand,
        )

        # The set is pinned: a member added here must be a program whose SECOND
        # operand is a write destination, not merely one that looks risky.
        assert sorted(_SECOND_OPERAND_WRITER_PROGRAMS) == ["uniq", "xxd"]
        # Every member stays ACCEPTED -- that is what the operand count buys.
        assert _SECOND_OPERAND_WRITER_PROGRAMS <= _PERM_VERB_MENTION_PROGRAMS

        def walk(command):
            return _writes_a_second_operand(shlex.split(command, posix=False))

        assert walk(f"xxd {_CM} /usr/local/bin/git") is True
        assert walk(f"uniq {_CM} /usr/local/bin/git") is True
        assert walk(f"uniq -c {_CM} /usr/local/bin/git") is True
        assert walk(f"xxd - {_CM}/etc/shadow") is True
        assert walk(f"grep -h '{_CM}' f | xxd a b") is True
        # No operand, one operand, or the writer absent: nothing is written.
        assert walk(f"grep -rn '{_CM}' src/ | uniq | head") is False
        assert walk(f"grep -rn '{_CM}' src/ | xxd") is False
        assert walk(f"uniq {_CM}") is False
        assert walk(f"cat {_CM} /usr/local/bin/git") is False

    def test_double_quoted_literal_refuses_expansion_machinery(self):
        """The mask covers ``"..."`` only when nothing inside it can run."""
        from kiro_crew.security.perm_verb_mention import _double_quoted_literal

        assert _double_quoted_literal('"a|b"') is True
        assert _double_quoted_literal('"a;b&c"') is True
        # A substitution inside double quotes really runs.
        assert _double_quoted_literal('"$(ls)"') is False
        assert _double_quoted_literal('"`ls`"') is False
        assert _double_quoted_literal('"${x}"') is False
        # Two literals glued around a real operator are not one literal.
        assert _double_quoted_literal('"a"|"b"') is False
        # Single quotes stay the other function's business.
        assert _double_quoted_literal("'a|b'") is False

    def test_mention_walk_refuses_when_the_verb_is_not_a_word(self):
        """A pattern can match a SUBSTRING, and that must not be read as "inert".

        ``foo{verb}bar`` trips the regex with no verb word anywhere, so answering
        "no occurrence, therefore all occurrences are inert" would widen those
        inputs silently.  The predicate refuses instead, leaving them exactly as
        they are today.
        """
        assert _perm_verb_mention._perm_verb_mention_only(f"grep foo{_CM}bar /etc/x") is False
