"""Tests for kiro_crew.security: ``is_denied`` and the built-in deny patterns.

The ada / AWS command patterns and the ``self-protection-kill`` rule through the real gate, then
``BUILTIN_DENY_PATTERNS``, with the own-host seed's datagram socket stubbed so no test opens a
real socket or asks the routing table.
"""

from __future__ import annotations

import socket

import pytest


class TestSandboxDeniedCommands:
    """Verify command denial allows/blocks the right ada and AWS patterns.

    Command denial is not injected into the kiro-cli agent spec
    (``config/defaults.json`` does not carry ``deniedCommands``); it is
    enforced solely at Kiro Crew's own ``hooks.py`` PreToolUse gate, whose
    decision function is ``security.is_denied`` (built-in regex tier + the
    always-on keystone controls for exfiltration / sensitive-path reads).  These
    tests therefore exercise the real gate directly.
    """

    @staticmethod
    def _is_denied(cmd: str) -> bool:
        from kiro_crew.security import is_denied

        return is_denied(cmd) is not None

    # --- ada: allowed (blocked by kiro-cli at runtime) ---

    def test_ada_update_once_allowed(self) -> None:
        cmd = "ada credentials update --once --account 123 --provider sso --role Admin"
        assert not self._is_denied(cmd)

    def test_ada_update_daemon_allowed(self) -> None:
        cmd = "ada credentials update --account 123 --provider iam --role Admin"
        assert not self._is_denied(cmd)

    def test_ada_profile_add_allowed(self) -> None:
        cmd = "ada profile add --profile staging --account 123 --provider sso --role Y"
        assert not self._is_denied(cmd)

    def test_ada_profile_list_allowed(self) -> None:
        assert not self._is_denied("ada profile list")

    # --- ada: blocked by kiro-cli ---

    # --- AWS CLI: allowed ---

    def test_aws_describe_allowed(self) -> None:
        assert not self._is_denied("aws ec2 describe-instances")

    def test_aws_logs_filter_allowed(self) -> None:
        cmd = "aws logs filter-log-events --log-group-name /aws/lambda/fn"
        assert not self._is_denied(cmd)

    def test_aws_s3_ls_allowed(self) -> None:
        assert not self._is_denied("aws s3 ls s3://my-bucket")

    def test_aws_s3_download_allowed(self) -> None:
        assert not self._is_denied("aws s3 cp s3://bucket/file ./local")

    def test_aws_sts_assume_role_allowed(self) -> None:
        cmd = "aws sts assume-role --role-arn arn:aws:iam::123:role/X"
        assert not self._is_denied(cmd)

    def test_aws_sts_get_caller_identity_allowed(self) -> None:
        assert not self._is_denied("aws sts get-caller-identity")

    # --- AWS CLI: blocked ---

    def test_aws_s3_upload_blocked(self) -> None:
        assert self._is_denied("aws s3 cp ./file s3://bucket/")

    def test_aws_s3_sync_upload_blocked(self) -> None:
        assert self._is_denied("aws s3 sync ./dir s3://bucket/")

    def test_aws_delete_blocked(self) -> None:
        assert self._is_denied("aws ec2 delete-vpc --vpc-id vpc-123")

    def test_aws_terminate_blocked(self) -> None:
        assert self._is_denied("aws ec2 terminate-instances --instance-ids i-1")

    # --- Credential exfiltration: blocked ---

    def test_echo_aws_secret_blocked(self) -> None:
        assert self._is_denied("echo $AWS_SECRET_ACCESS_KEY")

    def test_printenv_aws_blocked(self) -> None:
        assert self._is_denied("printenv AWS_SECRET_ACCESS_KEY")

    def test_env_grep_aws_blocked(self) -> None:
        assert self._is_denied("env | grep AWS_SECRET")

    def test_curl_imds_blocked(self) -> None:
        assert self._is_denied("curl http://169.254.169.254/latest/meta-data/")

    def test_python_boto_creds_blocked(self) -> None:
        cmd = "python3 -c 'import boto3; print(boto3.Session().get_credentials())'"
        assert self._is_denied(cmd)


class TestKiroCliBundledDeniedCommands:
    """Verify the ``self-protection-kill`` built-in rule via the real gate.

    Command denial is not injected into the kiro-cli agent spec — the
    bundled ``config/defaults.json`` does not carry ``deniedCommands``.  The
    self-protection kill guard is now a ``BUILTIN_DENIED_RULES`` entry
    (``self-protection-kill``) enforced at Kiro Crew's own ``hooks.py`` PreToolUse
    gate, whose decision function is ``security.is_denied``.  These tests
    therefore exercise ``is_denied`` directly (tool-shape agnostic — the same
    gate runs regardless of whether the tool is ``execute_bash`` or ``shell``).

    Regression tests for the ``kill``/``kirocrew`` pattern false positive,
    narrowed in two steps.

    Step 1 (word boundaries): the original pattern ``.*kill.*kiro.?crew.*``
    matched any command whose argv contained ``~/.kirocrew/skills/...``
    (because ``skills`` contains the substring ``kill``) followed by
    ``kirocrew`` anywhere.  Anchoring the kill word on word boundaries
    stopped skill-dir paths from reading as ``kill``.

    Step 2 (command structure): boundaries still left the rule matching mere
    CO-OCCURRENCE — any command that both called ``kill`` and happened to
    *mention* the product anywhere, in any role (a file being restored, a log
    path, a comment).  The rule is now scoped to the kill TARGET:
    ``pkill``/``killall`` select processes by name, so the product name as an
    argument in the same command segment is the target; bare ``kill`` takes
    PIDs, so it only matches when the name is resolved to one inside a
    command substitution.  ``[^|;&]*`` confines each arm to a single command
    segment, so an unrelated later command in a ``;``/``&&``/pipe chain is not
    captured.  Every by-name kill form is still blocked; ``kiro-crew`` is
    still covered by the ``[-.]?`` separator.
    """

    @staticmethod
    def _is_denied(cmd: str) -> bool:
        from kiro_crew.security import is_denied

        return is_denied(cmd) is not None

    # --- real kill attempts: blocked ---

    def test_pkill_kirocrew_blocked(self) -> None:
        assert self._is_denied("pkill kirocrew")

    def test_kill_kirocrew_pid_blocked(self) -> None:
        assert self._is_denied("kill -9 $(pgrep kirocrew)")

    def test_killall_kirocrew_blocked(self) -> None:
        assert self._is_denied("sudo killall kirocrew")

    def test_kill_kiro_crew_hyphenated_blocked(self) -> None:
        # The `.?` in the pattern covers an optional separator so agents can't
        # bypass with "kiro-crew".
        assert self._is_denied("pkill kiro-crew")

    def test_kill_pidof_substitution_blocked(self) -> None:
        # `pidof` resolves the name to a PID exactly as `pgrep` does, so a
        # resolver-name allowlist would have been a bypass.
        assert self._is_denied("kill $(pidof kirocrew)")

    def test_kill_pidfile_substitution_blocked(self) -> None:
        assert self._is_denied("kill $(cat /var/run/kirocrew.pid)")

    def test_kill_backtick_substitution_blocked(self) -> None:
        assert self._is_denied("kill `pgrep kirocrew`")

    # --- skill-dir false positives: must be allowed ---

    def test_skill_create_sh_kirocrew_domain_allowed(self) -> None:
        """The brazil-workspace skill scaffold must not be blocked."""
        cmd = "/Users/user/.kirocrew/skills/brazil-workspace/create.sh --domain kirocrew"
        assert not self._is_denied(cmd)

    def test_skills_dir_listing_allowed(self) -> None:
        assert not self._is_denied("ls ~/.kirocrew/skills/")

    def test_skill_run_with_kirocrew_arg_allowed(self) -> None:
        cmd = "/Users/user/.kirocrew/skills/coder/run.sh kirocrew --dry-run"
        assert not self._is_denied(cmd)

    def test_bash_skill_script_allowed(self) -> None:
        assert not self._is_denied("bash ~/.kirocrew/skills/something.sh")

    def test_cat_kirocrew_config_allowed(self) -> None:
        # "cat" has no "kill" word anywhere — must not match.
        assert not self._is_denied("cat ~/.kirocrew/config.json")

    # --- incidental-mention false positives: must be allowed ---
    # A bare `kill` takes PIDs, so none of these can aim at a kirocrew process
    # by name; the product name is a FILE, a LOG PATH, or a COMMENT.

    def test_kill_bare_pid_allowed(self) -> None:
        assert not self._is_denied("kill 12345")

    def test_kill_pid_then_restore_config_file_allowed(self) -> None:
        cmd = "kill 12345 && cp /tmp/bk/kirocrew.json ~/.kiro/agents/"
        assert not self._is_denied(cmd)

    def test_kill_pid_then_diff_config_file_allowed(self) -> None:
        cmd = "kill $PID; diff /tmp/bk/kirocrew.json ~/.kiro/agents/kirocrew.json"
        assert not self._is_denied(cmd)

    def test_kill_pid_with_trailing_comment_allowed(self) -> None:
        assert not self._is_denied("kill $PID  # stop the stray kirocrew instance")

    def test_kill_pid_piped_to_kirocrew_log_allowed(self) -> None:
        assert not self._is_denied("kill 12345 | tee /tmp/kirocrew.log")


#: The peers the ssh-to-self floor's own-host seed UDP-``connect``s to learn this
#: machine's primary outbound address per family: RFC 5737 TEST-NET-2 and the
#: RFC 3849 documentation prefix, which no router forwards, so a datagram
#: ``connect`` to them sends no packet. This is what the seed MUST keep pointing
#: at; the stub below records what it pointed at instead of asking the routing
#: table.
_OWN_HOST_PROBE_PEERS = frozenset({("198.51.100.1", 53), ("2001:db8::1", 53)})
#: What the stubbed probe answers as this machine's outbound address, per family:
#: documentation addresses too, distinct from the peers, so a test can tell the
#: seed read the STUB (these turn up in the own-name set) from a real interface.
_STUB_OWN_ADDRESS: dict[int, str] = {
    socket.AF_INET: "203.0.113.7",
    socket.AF_INET6: "2001:db8::7",
}


class _InertDatagramSocket:
    """A datagram socket that connects nothing.

    ``connect`` records the peer instead of asking the routing table for a
    source address; ``getsockname`` answers the documentation address for the
    family; ``fileno`` refuses so the per-interface ioctl sweep, which needs a
    real descriptor, contributes nothing (its own ``except`` swallows this).
    """

    def __init__(self, family: int, recorded: list[tuple[int, object]]) -> None:
        self._family = family
        self._recorded = recorded

    def __enter__(self) -> _InertDatagramSocket:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def connect(self, peer: object) -> None:
        self._recorded.append((self._family, peer))

    def getsockname(self) -> tuple[str, int]:
        return (_STUB_OWN_ADDRESS.get(self._family, ""), 0)

    def fileno(self) -> int:
        raise OSError("inert datagram socket has no descriptor")

    def close(self) -> None:
        return None


class TestBuiltinDenyPatterns:
    """Tests for is_denied() from security.py BUILTIN_DENY_PATTERNS.

    Credential-related patterns were removed — the OS-level sandbox
    (sandbox.py) hides credential files and deniedCommands in the
    kiro-cli agent config blocks bash-level exfiltration.  Only
    explicit secret-fetching tool names and destructive ops remain.
    """

    @pytest.fixture(autouse=True)
    # flake-ok: pre-existing class autouse stub on the shared monkeypatch moved verbatim; tracked for a de-flake pass
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse stub moved verbatim; no test in this class calls monkeypatch.undo()
    def _own_host_seed_connects_nothing(self, monkeypatch) -> list[tuple[int, object]]:
        """The ``ssh`` cases here are the first own-host lookup in the process.

        ``is_denied("ssh ...")`` seeds the ssh-to-self floor's own-host set on
        first use and, once the backoff allows, starts a DNS enrichment thread.
        The seed learns this machine's outbound address with a UDP ``connect``
        to a documentation peer -- packet-less, but a real socket the routing
        table has to answer -- and the worker resolves real names. These tests
        are about the deny patterns, not about this host's identity, so the
        datagram socket is stubbed at the seam production reads -- the module's
        ``socket`` binding, datagram construction only; every other socket kind
        passes through -- and the enrichment backoff is pushed past the test.
        The seed still RUNS, through the stub, so the peers it names are
        observable (``_OWN_HOST_PROBE_PEERS``) and the address it reads back is
        the stub's. The enumeration's other layers -- the per-interface ioctl
        sweep (its ``fileno`` is refused), ``/proc/net/if_inet6`` and the
        Windows / macOS adapter tables -- are local reads that may still yield
        this host's real addresses, so the enumeration's result is filtered to
        the stub's addresses before it enters the own-name set: what the deny
        patterns see is host-independent, and the filter admitting the stub's
        addresses is what proves the seed read the stub.

        The own-host cache is reset for the test and restored after it, so the
        stub's addresses never become another test's idea of this machine.
        """
        from kiro_crew.security import argv_floor

        recorded: list[tuple[int, object]] = []
        real_socket = argv_floor.socket
        real_interface_addresses = argv_floor._own_interface_addresses
        stub_addresses = set(_STUB_OWN_ADDRESS.values())

        def _stub_addresses_only() -> set[str]:
            return real_interface_addresses() & stub_addresses

        class _SocketModule:
            """``socket`` with datagram construction routed to the inert stub."""

            def __getattr__(self, name: str):
                return getattr(real_socket, name)

            def socket(self, family: int = -1, type: int = -1, proto: int = -1, fileno=None):
                if type == real_socket.SOCK_DGRAM:
                    return _InertDatagramSocket(family, recorded)
                return real_socket.socket(family, type, proto, fileno)

        monkeypatch.setattr(argv_floor, "socket", _SocketModule())
        monkeypatch.setattr(argv_floor, "_own_interface_addresses", _stub_addresses_only)
        monkeypatch.setattr(argv_floor, "_OWN_HOST_NAMES_CACHE", None)
        monkeypatch.setattr(argv_floor, "_OWN_HOST_RESOLVE_DONE", False)
        monkeypatch.setattr(argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
        return recorded

    def test_allows_command_with_credential_in_path(self) -> None:
        """Commands in dirs like CredentialValidatorServiceCDK must not be blocked."""
        from kiro_crew.security import is_denied

        cmd = "cd /home/user/src/CredentialValidatorServiceCDK && git status"
        assert is_denied(cmd) is None

    def test_allows_credential_in_package_name(self) -> None:
        """Package names containing 'credential' must not be blocked."""
        from kiro_crew.security import is_denied

        assert is_denied("ada credentials update --account 123") is None
        assert is_denied("credential-rotation-service build") is None
        assert is_denied("get-credentials --profile default") is None

    def test_blocks_secretsmanager_destructive(self) -> None:
        """The new catalog blocks the REAL destructive Secrets Manager CLI verb.

        The old glob catalog blocked bare tool-name tokens like
        ``get_secret_value`` / ``read_secret_store`` — underscore/no-prefix
        method names the AWS CLI never emits.  The new ``credential-exfil`` /
        ``aws-destructive`` rules match the real hyphenated CLI instead; a plain
        secret READ is intentionally allowed (reading is not exfiltration — the
        always-on keystone catches actual exfil), while a destructive
        ``delete-secret`` stays blocked.
        """
        from kiro_crew.security import is_denied

        assert is_denied("aws secretsmanager delete-secret --secret-id x") is not None

    def test_secret_exfil_still_blocked_by_keystone(self) -> None:
        """Dumping an AWS secret env var stays blocked (credential-exfil rule)."""
        from kiro_crew.security import is_denied

        assert is_denied("echo $AWS_SECRET_ACCESS_KEY") is not None

    def test_blocks_git_push(self) -> None:
        from kiro_crew.security import is_denied

        # ── Real publish invocations: must remain BLOCKED ──
        assert is_denied("git push origin main") is not None
        assert is_denied("git push origin main --force") is not None
        assert is_denied("git -C /Volumes/Foo/Bar push") is not None
        assert is_denied("git -C /Volumes/Foo/Bar push --force") is not None
        assert is_denied("git_push") is not None
        assert is_denied("git_push origin main") is not None
        # ── Legitimate stash invocations: must be ALLOWED ──
        assert is_denied("git stash push") is None
        assert is_denied("git stash push -m 'wip'") is None
        assert is_denied("git -C /path stash push") is None
        assert is_denied("git -c core.autocrlf=true stash push -m 'wip'") is None
        # ── Path containing "stash" must NOT bypass deny ──
        # The exception pattern requires a literal " stash push" with a leading
        # space, so "/tmp/stash push" (slash, not space) doesn't match.
        assert is_denied("git -C /tmp/stash push origin main --force") is not None
        # ── Chaining-bypass attempts must remain BLOCKED ──
        # Per-segment evaluation: the embedded real publish lives in its own
        # segment after splitting on `;` / `&&` / `$(` / backtick, so it
        # matches the deny pattern even though an outer stash segment exists.
        assert is_denied("git stash push; git push origin main --force") is not None
        assert is_denied("git stash push && git push origin main") is not None
        assert is_denied('git stash push -m "$(git push origin main --force)"') is not None
        assert is_denied("git stash push -m `git push origin main`") is not None
        # Newline-chained publish (heredoc / multi-statement script body).
        assert is_denied("echo starting\ngit push origin main") is not None
        # Leading whitespace before the publish must not evade.
        assert is_denied("   git push origin main") is not None
        # Bare ``git push`` (no remote/branch — pushes current branch to the
        # default remote) inside a subshell / backtick, where ``push`` is
        # followed by a closing metacharacter rather than whitespace/EOL.
        # A naive ``push(?:\s|$)`` terminator missed these.
        assert is_denied("echo $(git push)") is not None
        assert is_denied("result=`git push`") is not None
        assert is_denied("x=$(git push); echo done") is not None
        assert is_denied("git push|cat") is not None
        assert is_denied("git push&") is not None

    def test_allows_legitimate_stash_in_pipeline(self) -> None:
        """Per-segment evaluation: legitimate ``git stash push`` followed by
        unrelated commands via shell separators is now allowed.

        Under the prior whole-string design these were
        over-blocked because any separator suppressed the stash exception.
        Per-segment evaluation classifies each segment independently — the
        stash segment matches its exception, the trailing segments don't
        match any deny pattern, so the whole input is allowed.

        The chaining-bypass protection is preserved: see
        ``test_blocks_git_push`` for the bypass-attempt cases that remain
        blocked because the embedded segment IS a real publish.
        """
        from kiro_crew.security import is_denied

        # The original pain point: stash output piped into a filter.
        assert is_denied('git stash push -m "wip" 2>&1 | tail -3') is None
        # Stash followed by status / log via &&.
        assert is_denied("git stash push && git status") is None
        assert is_denied("git stash push && git log --oneline -5") is None
        # Stash piped through grep / head.
        assert is_denied("git stash push -u | head") is None
        assert is_denied('git stash push -m "wip" | grep saved') is None
        # Stash followed by an unrelated git operation.
        assert is_denied("git stash push && git checkout main") is None
        assert is_denied("git stash push; git rebase origin/main") is None

    def test_blocks_command_substitution_boundary_evasion(self) -> None:
        """Pass-1 whole-string deny closes the segment-boundary evasion vector.

        ``git$(echo ' ')push origin main`` evaluates to ``git push origin
        main`` in bash. A naive pass-2-only implementation would split on
        ``$(`` and ``)`` producing ``["git", "echo ' '", "push origin main"]``
        — no segment contains both substrings, so the deny pattern would
        not match and the publish would slip through.

        With pass-1 whole-string deny, the input is checked against the
        glob first. ``*git*push*`` matches the full string (it contains
        both substrings), and the ``* stash push*`` exception requires a
        literal ` stash push` substring (with leading space) which this
        input lacks → outright deny on pass 1, no fall-through to pass 2.
        """
        from kiro_crew.security import is_denied

        # Concrete bypass attempt.
        assert is_denied("git$(echo ' ')push origin main") is not None
        # Other variants that exploit the same boundary trick.
        assert is_denied("git$(echo)push origin") is not None
        assert is_denied("git`echo`push origin main") is not None
        assert is_denied("git$()push origin") is not None

    def test_blocks_background_operator_bypass(self) -> None:
        """``&`` (single ampersand, the bash background operator) must split
        segments like ``;`` and ``&&``.

        A ``_CMD_SPLIT_RE`` that covers ``&&`` but not a lone ``&`` keeps
        ``git stash push & git push origin main`` (which bash backgrounds
        the left command and immediately runs the right) as a single
        segment that matches both the deny pattern and the stash exception
        → falsely allowed.

        ``&(?!&)`` follows ``&&`` in the alternation so ``&&`` is consumed
        as a single token and a lone ``&`` is split on.
        """
        from kiro_crew.security import is_denied

        # Core bypass.
        assert is_denied("git stash push & git push origin main") is not None
        assert is_denied("git stash push -m 'wip' & git push --force") is not None
        # Trailing ``&`` to background a real publish.
        assert is_denied("git push origin main &") is not None
        # ``&&`` must continue to work — it's a different operator entirely
        # and was already covered.
        assert is_denied("git stash push && git push origin main") is not None
        # Legitimate stash backgrounded with no embedded publish should
        # still be ALLOWED — the second segment must be deny-free.
        assert is_denied("git stash push -m 'wip' & echo done") is None

    def test_two_pass_evaluates_all_deny_patterns(self, monkeypatch) -> None:
        """Pass 1 must continue iterating deny patterns after granting an
        exception, so a *different* pattern with no exception still triggers
        an outright deny.

        A ``break`` after granting an exception would skip the remaining
        patterns.  Pass 1 records the exception-matched pattern as a candidate
        and keeps iterating (this test exercises that path); pass 2 uses
        ``continue`` for the same reason (covered by other tests).

        ``_DENY_EXCEPTIONS`` ships only the search-verb carve-out on the two
        ``local-destructive`` rm rules, so the multi-pattern interaction still cannot
        be expressed with live catalog data (one input would have to trip two rm rules
        at once).  We install a synthetic two-glob scenario to
        keep exercising the loop-control invariant directly: the input matches
        an exception-carrying glob AND a second glob with no exception, so pass 1
        must fall through to the second glob and deny outright.  A ``break``
        regression would skip the second glob and falsely allow.
        """
        import kiro_crew.security as security_module

        monkeypatch.setattr(security_module, "_DENY_EXCEPTIONS", {"*alpha*": ["* stash *"]})
        # Pass 1 sees:
        #   *alpha* — matches, " stash " whole-string exception matches → candidate
        #   *bravo* — matches, no exception → outright deny
        assert (
            security_module.is_denied("alpha stash bravo", extra_patterns=["*alpha*", "*bravo*"])
            is not None
        )
        # Confidence check: with only the exception-carrying glob and no second
        # deny, the command is allowed (the candidate path itself does not deny).
        assert security_module.is_denied("alpha stash here", extra_patterns=["*alpha*"]) is None

    def test_allows_commit_message_mentioning_push(self) -> None:
        """A ``git commit`` whose message merely mentions ``push`` must be
        ALLOWED — ``push`` is not the git verb here.

        Guards the silent ``Tool use aborted`` on the Claude Code
        provider: the broad
        ``*git*push*`` substring glob matched any commit whose ``-m`` body
        contained the word ``push``, so the host gate denied it and
        the claude-agent-acp adapter surfaced the cryptic abort with no
        approval prompt.  Anchoring ``push`` as the git subcommand fixes it
        while keeping real ``git push`` blocked.
        """
        from kiro_crew.security import is_denied

        assert is_denied("git commit -m 'fix: do not push secrets to remote'") is None
        assert (
            is_denied("git commit -m 'refactor: push results downstream and reset cache'") is None
        )
        # Multi-line / heredoc-style body mentioning push.
        assert is_denied("git commit -m 'docs: explain when to push and when to rebase'") is None

    # ── a destructive literal handed to a read-only search verb ──
    # Assembled at runtime so this test module is not itself an un-greppable
    # needle: a plain literal here would make the file impossible to search for
    # by the very rule it exercises, which is the bug being fixed.
    ROOT_WIPE = "rm -" + "rf /"
    HOME_WIPE = "rm -" + "rf ~"

    def test_allows_a_destructive_literal_as_a_search_operand(self) -> None:
        """A read-only search verb cannot execute its operands, so a destructive
        string handed to it as a PATTERN is text and must be ALLOWED.

        ``local-destructive-rm-rf-root`` / ``-home`` are
        plain literal patterns matched over the segment text, so grepping FOR
        the rule's own subject matter was refused exactly as if the deletion had
        been typed — which prevented nothing (the same work completes by moving
        the payload into a file, which is not scanned) while blocking anyone
        working ON these rules.
        """
        from kiro_crew.security import is_denied

        for verb in ("grep -rn", "egrep -r", "fgrep"):
            assert is_denied(f'{verb} "{self.ROOT_WIPE}" test/') is None, verb
            assert is_denied(f'{verb} "{self.HOME_WIPE}" test/') is None, verb

    def test_a_search_pattern_containing_an_alternation_is_still_denied(self) -> None:
        """KNOWN LIMITATION, pinned deliberately.

        The command that motivated the report puts a regex ALTERNATION in the
        search pattern::

            grep -rln "rm-rf-root\\|rm -rf /" test/

        ``_CMD_SPLIT_RE`` is quote-unaware, so the ``|`` inside the quoted
        pattern is read as a pipe and the command splits into
        ``grep -rln "rm-rf-root\\`` and ``rm -rf /" test/``.  The second segment
        genuinely looks like a bare deletion in command position, so the
        verb-anchored carve-out cannot reach it — the exception is keyed to
        segments that START with a search verb, and by design it must stay that
        way or ``grep x | xargs <destructive>`` would be exonerated too.

        Closing this case needs quote-aware SEGMENTATION, which is a separate and
        much larger change (it also has to stay compatible with the
        quote-NORMALIZED matching added for evasion resistance).  Asserting the
        current behaviour rather than xfailing it, so the boundary is explicit and
        a future segmentation fix has to update this test consciously.
        """
        from kiro_crew.security import is_denied

        assert is_denied(f'grep -rln "rm-rf-root\\|{self.ROOT_WIPE}" test/') is not None
        # The same search without the alternation IS exonerated — isolating the
        # cause to segmentation rather than to the carve-out.
        assert is_denied(f'grep -rln "{self.ROOT_WIPE}" test/') is None

    def test_still_denies_the_real_destruction_and_any_chaining(self) -> None:
        """The carve-out is anchored at the search verb, so it must not exonerate
        a destructive command — including one chained after a real search.

        This is the half that makes the carve-out safe: ``_CMD_SPLIT_RE`` splits on
        every execution boundary, so the destructive SEGMENT is still evaluated in
        its own right and the Pass 1 whole-string exception only defers to Pass 2.
        """
        from kiro_crew.security import is_denied

        # Bare, and behind a wrapper that DOES execute its operands.
        assert is_denied(self.ROOT_WIPE) is not None
        assert is_denied(self.HOME_WIPE) is not None
        assert is_denied(f"sudo {self.ROOT_WIPE}") is not None
        assert is_denied(f"grep -rl x test/ | xargs {self.ROOT_WIPE}") is not None
        # Chained after a genuine search, across every separator the splitter knows.
        for joiner in ("&&", ";", "||", "|", "&", "\n"):
            cmd = f'grep -rn "needle" test/ {joiner} {self.ROOT_WIPE}'
            assert is_denied(cmd) is not None, joiner
        # Command substitution, both spellings.
        assert is_denied(f'grep -rn "$({self.ROOT_WIPE})" test/') is not None
        assert is_denied(f'grep -rn "`{self.ROOT_WIPE}`" test/') is not None

    def test_the_carve_out_does_not_exonerate_other_rules(self) -> None:
        """The exception is keyed to the two rm patterns only, so a search verb
        must not become a blanket allowlist for unrelated deny rules."""
        from kiro_crew.security import is_denied

        # A different local-destructive rule in the same segment as a search verb.
        assert is_denied("grep -rn x test/ && mkfs.ext4 /dev/sda1") is not None

    def test_a_search_verb_fragment_inside_an_operand_does_not_exonerate(self) -> None:
        """A ``/grep `` fragment ANYWHERE in a destructive command must not exonerate it.

        Guards the first bypass found reviewing the carve-out fix. An
        earlier revision also carried path-qualified globs (``*/grep *``) so that
        ``/usr/bin/grep ...`` would be exonerated too. ``fnmatch`` is a full
        match, but ``*`` crosses spaces, so a LEADING ``*`` is really an
        unanchored substring test: ``*/grep *`` matches
        ``rm -rf / /bin/grep x`` -- a genuine root wipe that merely lists a path
        containing ``/grep `` among its operands -- and the deletion was ALLOWED.

        Only the bare ``<verb> *`` form is safe, because it forces the segment to
        BEGIN with the verb. The cost is that a path-qualified search is no
        longer exonerated, asserted below so the trade-off is explicit rather
        than looking like an oversight.
        """
        from kiro_crew.security import is_denied

        assert is_denied(f"{self.ROOT_WIPE} /bin/grep x") is not None
        assert is_denied(f"{self.ROOT_WIPE} x/grep y") is not None
        assert is_denied(f"{self.HOME_WIPE} /usr/bin/grep z") is not None
        # The accepted trade-off: a path-qualified search verb is NOT exonerated,
        # because no glob can express "the first token's basename is the verb".
        assert is_denied(f'/usr/bin/grep -rn "{self.ROOT_WIPE}" test/') is not None

    def test_no_shell_active_construct_is_ever_exonerated(self) -> None:
        """A command hidden in any expansion behind a search verb must stay denied.

        Guards the second and fourth bypasses found reviewing the carve-out's own
        fix. ``_CMD_SPLIT_RE`` isolates ``;`` ``|`` ``&&`` ``&`` ``$(`` ``)``
        backtick and newline, but NOT ``<(`` / ``>(`` / ``${`` / a bare ``(``. So
        ``_split_segments`` cuts ``grep x <(<destructive>)`` only at the trailing
        ``)``, and ``grep x ${ <destructive>;}`` only at the ``;`` -- in both
        cases leaving the destructive command glued to the search verb instead of
        isolated in its own command position, while bash still executes it.

        The first attempt blocklisted just ``(`` and was defeated by the bash 5.3
        funsub. ``_exception_eligible`` now refuses any view containing a
        shell-active character, closing the class instead of chasing spellings.
        """
        from kiro_crew.security import is_denied

        # Process substitution, input and output forms, and a bare subshell.
        assert is_denied(f"grep x <({self.ROOT_WIPE}tmp/victim)") is not None
        assert is_denied(f'grep -rn "x" <({self.ROOT_WIPE})') is not None
        assert is_denied(f"grep x >({self.ROOT_WIPE}tmp/victim)") is not None
        assert is_denied(f"grep x ({self.ROOT_WIPE})") is not None
        # bash >= 5.3 funsub -- the opener that defeated the `(`-only guard.
        assert is_denied(f"grep x ${{ {self.ROOT_WIPE};}}") is not None
        assert is_denied(f"grep x ${{ {self.HOME_WIPE};}}") is not None
        # Command substitution and backticks (already split, asserted anyway).
        assert is_denied(f'grep -rn "$({self.ROOT_WIPE})" test/') is not None
        assert is_denied(f'grep -rn "`{self.ROOT_WIPE}`" test/') is not None
        # Confidence check: the plain search is still exonerated, so the guard
        # narrowed exactly the shell-active forms and nothing else.
        assert is_denied(f'grep -rn "{self.ROOT_WIPE}" test/') is None

    def test_a_search_verb_that_can_execute_a_helper_is_not_exonerated(self) -> None:
        """Only verbs with no exec flag are exonerated.

        Guards the third bypass found reviewing the carve-out fix. The
        carve-out's premise is that the verb cannot execute its operands, and
        that is a property of the specific tool, not of "being a search tool":
        ``rg --pre <cmd>`` runs a preprocessor and ``ack --pager <cmd>`` runs a
        pager, so ``rg --pre sh "<destructive>" payload.sh`` really does execute.

        ``rg`` and ``ack`` were therefore dropped from the allowlist, which makes
        the premise true rather than merely asserted. ``grep``/``egrep``/``fgrep``
        have no flag that spawns a helper.
        """
        from kiro_crew.security import is_denied

        assert is_denied(f'rg --pre sh "{self.ROOT_WIPE}tmp/victim" payload.sh') is not None
        # Dropped wholesale, not just for the executing flag: a glob cannot tell
        # `rg PATTERN` from `rg --pre sh PATTERN`, so the verb cannot be trusted.
        assert is_denied(f'rg "{self.ROOT_WIPE}" src/') is not None
        assert is_denied(f'ack "{self.ROOT_WIPE}" src/') is not None

    def test_a_pipeline_into_an_interpreter_is_never_exonerated(self) -> None:
        """A search piped into something that executes what it emitted must deny.

        Guards the fifth bypass found reviewing the carve-out fix.
        ``grep '<destructive>' payload.py | python`` splits at the pipe, so the
        Pass 2 grep segment looks innocent on its own and the bare ``python``
        segment matches no rule -- the pipeline as a whole was allowed, and the
        interpreter runs the line the search emitted.

        Note the pipe cannot be caught in the Pass 2 segment (the splitter has
        already consumed it). What closes this is refusing the separators in the
        PASS 1 whole-string view, so the whole-string deny match stands instead
        of deferring to the innocent-looking segment. Hence
        ``_exception_eligible`` requires a single plain command, not merely one
        free of expansion openers.
        """
        from kiro_crew.security import is_denied

        assert is_denied(f"grep '{self.ROOT_WIPE}tmp/victim' payload.py | python") is not None
        assert is_denied(f"grep '{self.ROOT_WIPE}' payload.sh | sh") is not None
        assert is_denied(f"grep '{self.ROOT_WIPE}' f | bash -s") is not None
        # A compound whose later stage is inert is denied for the same reason:
        # an exception must not speak for more than one command.
        assert is_denied(f"grep '{self.ROOT_WIPE}' f && echo done") is not None
        # Confidence check: the single plain search remains exonerated.
        assert is_denied(f'grep -rn "{self.ROOT_WIPE}" test/') is None

    def test_feature_push_not_blocked_by_prose_push_word_in_earlier_segment(self) -> None:
        """A legit feature-branch push must be ALLOWED even when an EARLIER
        chained segment merely contains the word ``push``.

        Ported upstream regression guard (from the upstream project):
        upstream's two-pass gate matched a bare ``\\bpush\\b`` in any segment,
        so prose like ``git commit -m 'ready to push'`` was denied before the
        refspec normalizer could allow the real feature-branch push. This
        fork's ``_is_push_to_protected_branch`` never had that pass — it gates
        each segment on ``_is_git_publish`` and parses via the verb-anchored
        ``_git_push_args`` — but this test locks in the contract: a prose
        "push" in an earlier chained segment never blocks a real
        feature-branch push, while chained protected pushes stay denied.
        """
        from kiro_crew.security import is_denied

        assert is_denied("git commit -m 'ready to push' && git push origin feature-x") is None
        assert is_denied("echo 'time to push' && git push origin my-feature") is None
        # The protective behavior must remain: a real protected push chained
        # AFTER a benign feature push is still blocked.
        assert is_denied("git push origin feat && git push origin main") is not None
        assert is_denied("git commit -m 'ready to push' && git push origin main") is not None

    def test_allows_git_verbs_with_push_substring_args(self) -> None:
        """Other git subcommands whose arguments contain ``push`` (branch
        names, grep patterns, config keys) must be ALLOWED — only an actual
        ``git push`` invocation is a publish.
        """
        from kiro_crew.security import is_denied

        assert is_denied("git log --grep push") is None
        assert is_denied("git config push.default current") is None
        assert is_denied("git branch --contains pushed-feature") is None
        assert (
            is_denied("git switch -c fix/security-tighten-git-push origin/beta-braveheart") is None
        )
        # ``git remote`` referencing a remote literally named "push".
        assert is_denied("git remote show push") is None

    def test_allows_ssh_remote_command_without_publish(
        self, _own_host_seed_connects_nothing: list[tuple[int, object]]
    ) -> None:
        """A plain ``ssh host '<cmd>'`` whose remote command contains the word
        ``push`` (but is not a real ``git push``) must be ALLOWED.

        Covers the ssh symptom from the same thread: remote
        interactions starting with ``ssh xxxx`` were aborting.

        The first ``ssh`` verdict in a process also seeds the own-host set. That
        seed runs here through the inert datagram stub: the peers it names are
        the documentation addresses (the seam kept pointing where it must), and
        the own addresses that reached the own-name set are the STUB's: they
        got there only because the real enumeration read them back from the
        stubbed socket, and the fixture's filter admits nothing else.
        """
        from kiro_crew.security import argv_floor, is_denied

        assert is_denied("ssh dev-dsk 'cd /workplace && git status'") is None
        assert is_denied("ssh dev-dsk 'git commit -m \"address push-back from review\"'") is None
        recorded = _own_host_seed_connects_nothing
        assert {peer for _family, peer in recorded} == _OWN_HOST_PROBE_PEERS, recorded
        assert {family for family, _peer in recorded} == {socket.AF_INET, socket.AF_INET6}
        assert argv_floor._OWN_HOST_NAMES_CACHE is not None
        assert set(_STUB_OWN_ADDRESS.values()) <= argv_floor._OWN_HOST_NAMES_CACHE

    def test_blocks_ssh_remote_real_git_push(self) -> None:
        """A real ``git push`` inside an ``ssh`` remote command stays BLOCKED."""
        from kiro_crew.security import is_denied

        assert is_denied("ssh host 'cd /repo && git push origin main'") is not None

    def test_deny_event_audit_emitted_on_block(self, monkeypatch) -> None:
        """Every denial path emits a ``deny_event`` SEL event.

        Auditing only the exception-granted path would leave denials
        un-audited.
        """
        import kiro_crew.security as security_module

        captured: list[tuple[str, str, str]] = []

        def fake_emit(tool_name: str, deny_pattern: str, segment: str) -> None:
            captured.append((tool_name, deny_pattern, segment))

        monkeypatch.setattr(security_module, "_emit_deny_event", fake_emit)
        # Git-publish deny. The audited pattern is now the RULE's own pattern, not
        # the human "git push" label — a floor denial has to map back to a rule id
        # in the SEL trail, the way every other deny does.
        result = security_module.is_denied("git push origin main --force")
        assert result is not None
        assert len(captured) == 1
        assert captured[0][0] == "git push origin main --force"
        assert (
            captured[0][1]
            == security_module._GIT_PUBLISH_FLOOR_BY_ID["git-publish-push-protected-branch-name"]
        )
        # Chained bypass attempt is caught on the whole string (the separator
        # is part of the git-publish anchor), and still audited.
        captured.clear()
        result = security_module.is_denied("git stash push && git push origin main")
        assert result is not None
        assert any("git push origin main" in c[2] for c in captured)
        # A regex-tier built-in deny (real hyphenated AWS CLI) records the
        # matched rule pattern verbatim.
        captured.clear()
        result = security_module.is_denied("aws ec2 terminate-instances --instance-ids i-1")
        assert result is not None
        assert captured[0][1] == (
            r"aws(?:\s+--?[a-z-]+(?:[= ]\S+)?)*\s+ec2"
            r"(?:\s+--?[a-z-]+(?:[= ]\S+)?)*\s+terminate-instances.*"
        )

    def test_blocks_delete_stack(self) -> None:
        """The real hyphenated CloudFormation teardown is blocked.

        The old glob catalog matched the underscore token ``delete_stack`` the
        AWS CLI never emits; the new catalog matches the real
        ``aws cloudformation delete-stack`` invocation instead (see
        ``test_blocks_real_hyphenated_destructive_aws_cli``).
        """
        from kiro_crew.security import is_denied

        assert is_denied("aws cloudformation delete-stack --stack-name foo") is not None

    def test_blocks_terminate_instance(self) -> None:
        """The real hyphenated EC2 terminate is blocked (underscore form retired)."""
        from kiro_crew.security import is_denied

        assert is_denied("aws ec2 terminate-instances --instance-ids i-123") is not None

    def test_blocks_real_hyphenated_destructive_aws_cli(self) -> None:
        """Real AWS CLI destructive subcommands use HYPHENS, not underscores.

        The built-in deny globs only match the underscore
        forms (``*delete_stack*`` …), which the AWS CLI never emits — so the
        actual destructive invocations (``aws cloudformation delete-stack``
        …) slipped through ``is_denied`` entirely. ``mcp_cron._vet_shell_command``
        relies on ``is_denied`` to stop a prompt-injected ``cron_add`` from
        scheduling destructive shell, so this was an exploitable gap on the
        cron command path.
        """
        from kiro_crew.security import is_denied

        assert is_denied("aws cloudformation delete-stack --stack-name prod") is not None
        assert is_denied("aws ec2 terminate-instances --instance-ids i-123") is not None
        assert is_denied("aws s3api delete-bucket --bucket prod-data") is not None
        assert is_denied("aws dynamodb delete-table --table-name prod") is not None
        # NB: the underscore/boto3 method-name forms (``terminate_instances``,
        # ``delete_table``) are intentionally NOT blocked by the new catalog —
        # it ports only the real hyphenated AWS CLI regexes (the CLI never emits
        # the underscore forms).  See ``test_blocks_terminate_instance``.

    def test_allows_benign_aws_reads_after_deny_fix(self) -> None:
        """The hyphenated destructive patterns must not over-block benign
        AWS reads or package/command names that merely contain 'delete'/'credential'."""
        from kiro_crew.security import is_denied

        # Read-only AWS operations stay allowed.
        assert is_denied("aws ec2 describe-instances") is None
        assert is_denied("aws s3 ls s3://my-bucket") is None
        assert is_denied("aws sts get-caller-identity") is None
        assert is_denied("aws logs filter-log-events --log-group-name /x") is None
        # Non-destructive verbs that merely contain a destructive word as a
        # substring of a DIFFERENT token must not trip the specific globs.
        assert is_denied("credential-rotation-service build") is None
        assert is_denied("get-credentials --profile default") is None

    def test_allows_git_status(self) -> None:
        from kiro_crew.security import is_denied

        assert is_denied("git status") is None

    def test_allows_git_log(self) -> None:
        from kiro_crew.security import is_denied

        assert is_denied("git -P log --oneline -5") is None

    def test_allows_cr_command(self) -> None:
        from kiro_crew.security import is_denied

        assert is_denied("cr --summary 'Fix test discovery'") is None
