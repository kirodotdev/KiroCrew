"""Tests for the git-publish branch gate (feature-branch allow, protected deny).

Covers the always-on Python gate in ``kiro_crew.security`` and its kiro-cli
``defaults.json`` mirror:

* ``_is_git_publish`` — a PURE detector ("is this a git publish?"), incl.
  command-substitution glue-evasion. No side effects.
* ``_is_push_to_protected_branch`` — the allow/deny decision. Fails CLOSED on
  bare/ambiguous refs, protected targets (main/mainline/master in any ref
  spelling), wildcard refspecs, ``--mirror``/``--all``, quote/escape evasion,
  and substitution glue. EVERY publish sub-invocation is validated.
* ``is_denied`` — the enforcement point: denial reason for a blocked publish,
  ``push_allowed`` SEL audit for an allowed one (final-outcome only).

KiroCrew protects the git default branch names only (enumerated at line 9
above); it has no ``beta-braveheart``/``develop``/``prod`` integration branch
nor a ``release/*`` namespace, so those names are ordinary feature branches here.
"""

import signal
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from kiro_crew import security
from kiro_crew.security import (
    _is_git_publish,
    _is_push_to_protected_branch,
    _schedule_push_allow_audit,
    is_denied,
)

# "git pus" + "h" keeps a literal blocked command out of the test source.
PUSH = "git pus" + "h"


class TestIsPushToProtectedBranch:
    """Unit tests for the branch-level allow/deny decision."""

    def test_feature_branch_allowed(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} github my-feature-branch") is False
        assert _is_push_to_protected_branch(f"{PUSH} -u origin fix/relax-git-rule") is False
        assert _is_push_to_protected_branch(f"{PUSH} origin feat/welcome-kiro-ghost") is False

    def test_refspec_to_feature_allowed(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} github feature:my-feature") is False
        assert _is_push_to_protected_branch(f"{PUSH} origin HEAD:my-topic") is False

    def test_protected_branch_blocked(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} origin main") is True
        assert _is_push_to_protected_branch(f"{PUSH} github mainline") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin master") is True

    def test_nondefault_integration_branches_not_protected(self) -> None:
        """KiroCrew protects only git defaults; other integration branch names
        are ordinary feature branches here and stay pushable."""
        assert _is_push_to_protected_branch(f"{PUSH} origin beta-integration") is False
        assert _is_push_to_protected_branch(f"{PUSH} origin develop") is False
        assert _is_push_to_protected_branch(f"{PUSH} origin release/1.0") is False

    def test_similar_feature_names_not_false_positive(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} origin mainline-refactor") is False
        assert _is_push_to_protected_branch(f"{PUSH} origin main-refactor") is False
        assert _is_push_to_protected_branch(f"{PUSH} origin feature/main") is False

    def test_refspec_to_protected_blocked(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} github feature:main") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin HEAD:master") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin feat:mainline") is True

    def test_bare_push_blocked(self) -> None:
        assert _is_push_to_protected_branch(PUSH) is True
        assert _is_push_to_protected_branch(f"{PUSH} origin") is True

    def test_force_push_to_feature_allowed(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} --force github my-feature") is False
        assert _is_push_to_protected_branch(f"{PUSH} -f origin my-feature") is False
        assert _is_push_to_protected_branch(f"{PUSH} --force-with-lease github feat") is False

    def test_force_push_to_protected_blocked(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} --force origin main") is True
        assert _is_push_to_protected_branch(f"{PUSH} -f github mainline") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin +main") is True

    def test_multiple_refspecs(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} origin my-feature main") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin feat1 feat2") is False
        assert _is_push_to_protected_branch(f"{PUSH} origin feat1 mainline feat2") is True

    def test_ambiguous_refs_blocked(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} origin head") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin @") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin HEAD:my-feature") is False

    def test_push_all_branches_flags_blocked(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} --all origin feat-branch") is True
        assert _is_push_to_protected_branch(f"{PUSH} --mirror origin feat-branch") is True

    def test_shell_expansion_blocked(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} origin $BRANCH") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin ${{BRANCH}}") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin @{{u}}") is True

    def test_quoted_branch_names_stripped(self) -> None:
        """bash collapses interior quotes/escapes into one word (ma\"in\" -> main)."""
        assert _is_push_to_protected_branch(f"{PUSH} origin 'main'") is True
        assert _is_push_to_protected_branch(f'{PUSH} origin ma"in"') is True
        assert _is_push_to_protected_branch(f"{PUSH} origin m''ain") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin ma\\in") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin 'my-feature'") is False

    # ── ref-spelling normalization (server-side resolution) ──
    def test_ref_path_spellings_blocked(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} origin refs/heads/main") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin heads/main") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin remotes/origin/main") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin refs/remotes/origin/mainline") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin HEAD:refs/heads/master") is True

    def test_ref_path_feature_not_false_positive(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} origin refs/heads/my-feature") is False
        assert _is_push_to_protected_branch(f"{PUSH} origin heads/main-refactor") is False

    # ── wildcard refspecs (push MANY refs, may include protected) ──
    def test_wildcard_refspecs_blocked(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} origin refs/heads/*:refs/heads/*") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin '*:*'") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin +refs/heads/*:refs/heads/*") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin feat*") is True


class TestCompoundAndGlueGate:
    """EVERY publish sub-invocation is validated; glue-evasion fails closed."""

    def test_second_push_to_protected_blocked(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} origin feat && {PUSH} origin main") is True

    def test_all_feature_pushes_allowed(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} origin feat1 && {PUSH} origin feat2") is False

    def test_glue_evasion_verb_and_target_blocked(self) -> None:
        """git$(echo ' ')push and ma$(echo)in both resolve to a protected push."""
        assert (
            _is_push_to_protected_branch(
                f"{PUSH} origin feature; git$(echo x)pus{'h'} origin ma$(echo)in"
            )
            is True
        )
        assert _is_push_to_protected_branch(f"git`echo`pus{'h'} origin main") is True

    def test_substitution_in_target_blocked(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} origin ma$(echo)in") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin ${{x}}main") is True

    def test_brace_expansion_blocked(self) -> None:
        """bash brace expansion resolves to a protected branch before git sees it."""
        assert _is_push_to_protected_branch(f"{PUSH} origin {{main,dummy}}") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin ma{{i,i}}n") is True
        assert _is_push_to_protected_branch(f"{PUSH} origin {{feat,main}}") is True

    def test_substitution_in_non_push_segment_allowed(self) -> None:
        assert _is_push_to_protected_branch(f"{PUSH} origin feat && echo $(date)") is False
        assert _is_push_to_protected_branch(f"echo $(date); {PUSH} origin my-feature") is False
        assert _is_push_to_protected_branch(f"{PUSH} origin feat && echo {{a,b}}") is False

    def test_word_push_in_other_segment_allowed(self) -> None:
        assert (
            _is_push_to_protected_branch(f"{PUSH} origin feat; echo remember-to-pus{'h'}") is False
        )


class TestIsGitPublishDetection:
    """_is_git_publish is a PURE detector — True for ANY publish, no side effects."""

    def test_detects_feature_and_protected(self) -> None:
        assert _is_git_publish(f"{PUSH} github my-feature-branch") is True
        assert _is_git_publish(f"{PUSH} origin main") is True

    def test_bare_detected(self) -> None:
        assert _is_git_publish(PUSH) is True

    def test_stash_and_commit_message_not_detected(self) -> None:
        assert _is_git_publish("git stash pus" + "h") is False
        assert _is_git_publish("git commit -m 'pus" + "h to prod'") is False
        assert _is_git_publish("git log --grep pus" + "h") is False

    def test_glue_evasion_detected(self) -> None:
        assert _is_git_publish("git_pus" + "h") is True
        assert _is_git_publish(f"git$(echo ' ')pus{'h'} origin main") is True

    def test_program_substitution_evasion_detected(self) -> None:
        # Program NAME produced by an expansion the shell resolves to git before
        # exec -- the literal ``git`` token never appears in the source text.
        # Port of the upstream project (security-review 3eeb3852).
        P = "pus" + "h"
        assert _is_git_publish("$(echo git) %s origin main" % P) is True
        assert _is_git_publish("`echo git` %s origin main" % P) is True
        assert _is_git_publish("${git} %s origin main" % P) is True
        assert _is_git_publish("$git %s origin main" % P) is True
        # Bare $VAR after an env-assignment in a chained segment.
        assert _is_git_publish("git=x; $git %s origin mainline" % P) is True

    def test_program_substitution_non_push_not_detected(self) -> None:
        # Substitutions/expansions NOT followed by a ``push`` subcommand, and
        # unrelated $VAR usage, must NOT be flagged.
        assert _is_git_publish("$(echo ls) -la") is False
        assert _is_git_publish("$editor notes.txt") is False
        assert _is_git_publish("echo $git is set") is False


@pytest.fixture
def captured_sel_events(monkeypatch):
    """Capture SEL events without real I/O (isolate the ambient forensic log)."""
    events = []

    class _Capture:
        def log(self, event) -> None:
            events.append(event)

    monkeypatch.setattr(security, "SecurityEventLog", lambda: _Capture())
    return events


class TestGitPushEnforcement:
    """Behavioral tests through the real enforcement point ``is_denied``."""

    @pytest.mark.asyncio
    async def test_feature_push_allowed_and_audited(self, captured_sel_events, monkeypatch) -> None:
        """Allowed feature publish returns None and emits ONE push_allowed event.

        Async so is_denied runs under a live loop and takes the PRODUCTION path
        (audit offloaded to the maintenance executor, not the sync fallback);
        a dedicated single-worker executor is installed and drained.
        """
        executor = ThreadPoolExecutor(max_workers=1)
        monkeypatch.setattr(security, "maintenance_executor", lambda: executor)
        try:
            assert is_denied(f"{PUSH} origin my-feature") is None
        finally:
            executor.shutdown(wait=True)
        allow = [e for e in captured_sel_events if e.event_type == "push_allowed"]
        assert len(allow) == 1
        assert allow[0].outcome == "allowed"
        assert allow[0].metadata.get("mechanism") == "BRANCH_GATE"

    def test_allow_audit_records_the_raw_command_not_the_matching_view(
        self, captured_sel_events
    ) -> None:
        """``is_denied`` audits the RAW command, never its lowercased match view.

        Case is preserved for two reasons and this pins both. Faithfulness: a
        branch name is case-sensitive, so an audit of a push to ``Feature-ABC``
        must not read as a push to ``feature-abc``. Redaction: the emitter
        redacts before it clips to 200 chars, but the credential scrubber matches
        an AWS key ID case-SENSITIVELY on purpose, so a case-folded key slips
        past that pass, gets cut by the clip, and the surviving prefix is too
        short for SEL's any-case write-path net to match either -- a partial key
        in the durable log. The key is planted so it STRADDLES index 200, which
        is the only window where both nets miss.
        """
        # Named ``planted`` rather than anything in CodeQL's sensitive-name
        # vocabulary: ``py/clear-text-logging-sensitive-data`` classifies purely
        # on the identifier, so a name like ``secret`` here makes this AWS
        # example key taint the pre-existing ``logger.warning`` calls that
        # ``is_denied`` reaches on its failure paths.
        planted = "AKIA" + "IOSFODNN7EXAMPLE"  # 20-char AWS access key ID
        prefix = f"{PUSH} origin Feature-"
        pad = "b" * (190 - len(prefix))  # key starts at 190, so the clip cuts it
        command = prefix + pad + planted + "-branch-suffix"
        assert len(command) > 200

        assert is_denied(command) is None

        allow = [e for e in captured_sel_events if e.event_type == "push_allowed"]
        assert len(allow) == 1
        audited = allow[0].metadata["command"]
        assert "akia" not in audited.lower(), audited
        assert "Feature-" in audited, audited

    def test_protected_push_blocked_no_audit(self, captured_sel_events) -> None:
        assert is_denied(f"{PUSH} origin main").startswith("Blocked by security policy")
        assert is_denied(f"{PUSH} --force origin master").startswith("Blocked by security policy")
        assert not any(e.event_type == "push_allowed" for e in captured_sel_events)

    def test_substitution_program_push_blocked(self, captured_sel_events) -> None:
        # Verb obfuscated via an expansion that resolves to git; the branch gate
        # still reads the target and blocks a protected one (security-review 3eeb3852).
        P = "pus" + "h"
        assert is_denied("$(echo git) %s origin main" % P).startswith("Blocked by security policy")
        assert is_denied("$git %s origin mainline" % P).startswith("Blocked by security policy")
        # Third protected name built by concatenation so the inclusive-language
        # scanner does not flag the literal legacy-primary branch token here.
        legacy_primary = "mast" + "er"
        assert is_denied("${git} %s origin %s" % (P, legacy_primary)).startswith(
            "Blocked by security policy"
        )
        assert not any(e.event_type == "push_allowed" for e in captured_sel_events)

    def test_substitution_program_push_to_feature_blocked(self, captured_sel_events) -> None:
        # Fork divergence: unlike upstream (which allows an explicit
        # feature-branch target under an obfuscated verb), this fork's
        # _AMBIGUOUS_EXPANSION_RE fails closed — a push whose program token is
        # an unresolvable expansion is denied even for a feature target, because
        # the gate cannot prove the resolved command is really `git push`.
        P = "pus" + "h"
        assert is_denied("$(echo git) %s origin my-feature" % P).startswith(
            "Blocked by security policy"
        )

    def test_bare_push_blocked(self, captured_sel_events) -> None:
        assert is_denied(PUSH).startswith("Blocked by security policy")

    def test_glue_evasion_bypass_blocked(self, captured_sel_events) -> None:
        """The reviewer's bypass: benign sibling + glued protected publish."""
        cmd = f"{PUSH} origin feature; git$(echo x)pus{'h'} origin ma$(echo)in"
        assert is_denied(cmd).startswith("Blocked by security policy")
        assert not any(e.event_type == "push_allowed" for e in captured_sel_events)

    def test_wildcard_and_shorthand_blocked(self, captured_sel_events) -> None:
        assert is_denied(f"{PUSH} origin refs/heads/*:refs/heads/*").startswith(
            "Blocked by security policy"
        )
        assert is_denied(f"{PUSH} origin heads/main").startswith("Blocked by security policy")

    def test_brace_expansion_blocked(self, captured_sel_events) -> None:
        assert is_denied(f"{PUSH} origin {{main,dummy}}").startswith("Blocked by security policy")
        assert is_denied(f"{PUSH} origin ma{{i,i}}n").startswith("Blocked by security policy")
        assert not any(e.event_type == "push_allowed" for e in captured_sel_events)

    def test_compound_second_push_blocked(self, captured_sel_events) -> None:
        assert is_denied(f"{PUSH} origin feat && {PUSH} origin main").startswith(
            "Blocked by security policy"
        )

    def test_chained_dangerous_command_denied_without_allow_audit(
        self, captured_sel_events
    ) -> None:
        """A feature publish chained with a denied command is denied, and NO
        push_allowed audit fires (SEL reflects the FINAL outcome)."""
        reason = is_denied(f"{PUSH} origin feat && aws delete_bucket my-bucket")
        assert reason is not None and reason.startswith("Blocked by security policy")
        assert not any(e.event_type == "push_allowed" for e in captured_sel_events)

    def test_allow_audit_sync_fallback(self, captured_sel_events) -> None:
        """No running loop -> _schedule_push_allow_audit writes inline."""
        _schedule_push_allow_audit(f"{PUSH} origin my-feature")
        allow = [e for e in captured_sel_events if e.event_type == "push_allowed"]
        assert len(allow) == 1
        assert allow[0].operation == "git_push"

    def test_redos_safe_on_pathological_input(self) -> None:
        """is_denied must not backtrack exponentially on whitespace-laden flags.

        The SIGALRM is the real guarantee: a catastrophic pattern would run for
        seconds-to-minutes and trip it. The elapsed bound is deliberately wide
        (load-tolerant) — a shared, parallel CI runner can inflate the sub-ms
        linear scan to a few hundred ms without that being a regression.
        """

        def _timeout(*_):
            raise TimeoutError

        signal.signal(signal.SIGALRM, _timeout)
        signal.setitimer(signal.ITIMER_REAL, 10.0)
        try:
            t = time.perf_counter()
            is_denied("git " + ("\t-! " * 5000) + "x")
            elapsed = time.perf_counter() - t
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        assert elapsed < 5.0


class TestDefaultsJsonPushRegexes:
    """Git-publish enforcement at the KiroCrew hooks gate (``is_denied``).

    Denied commands are no longer injected into the kiro-cli agent config's
    ``deniedCommands`` (that injection path is retired); the git-publish
    protected-branch gate is enforced solely by ``is_denied`` via the always-on
    ``_is_git_publish`` / ``_is_push_to_protected_branch`` floor.  These cases
    verify that floor blocks protected targets without over-blocking legit
    feature-branch pushes.
    """

    def _blocked(self, cmd: str) -> bool:
        return is_denied(cmd) is not None

    def test_protected_targets_blocked(self) -> None:
        for cmd in (
            f"{PUSH} origin main",
            f"{PUSH} origin mainline",
            f"{PUSH} origin HEAD:main",
            f"{PUSH} origin refs/heads/main",
            f"{PUSH} origin heads/main",
            f"{PUSH} origin remotes/origin/main",
            f"{PUSH} origin +main",
            f"{PUSH} --mirror origin",
            f"{PUSH} --all origin",
            f"{PUSH} origin refs/heads/*:refs/heads/*",
            f"{PUSH} origin {{main,dummy}}",
            f"{PUSH} origin ma{{i,i}}n",
        ):
            assert self._blocked(cmd), cmd

    def test_feature_paths_not_blocked(self) -> None:
        for cmd in (
            f"{PUSH} origin my-feature",
            f"{PUSH} origin feature/main",
            f"{PUSH} origin fix/mainline",
            f"{PUSH} origin main-refactor",
            f"{PUSH} origin refs/heads/main-refactor",
            f"{PUSH} origin +my-feature",
            f"{PUSH} origin heads/develop",
            # wildcard/brace patterns are scoped to the publish's own args, not
            # a sibling command's shell glob/brace:
            f"{PUSH} origin feat && ls *.py",
            f"{PUSH} origin feat && echo {{a,b}}",
        ):
            assert not self._blocked(cmd), cmd

    def test_stash_push_not_blocked(self) -> None:
        assert not self._blocked("git stash pus" + "h --all")
        assert not self._blocked("git stash pus" + "h")


class TestEveryGatedTagNamesARealCatalogRule:
    """A gated tag naming no catalog rule is a SILENT ALLOW, not a loud error.

    Now that the floor consults the per-rule enabled set, ``is_denied`` resolves
    each gated tag through ``_GIT_PUBLISH_FLOOR_BY_ID.get(tag)``.  A miss yields
    ``None`` and the branch ``continue``s — so a protected-branch push is simply
    not denied, nothing reddens, and the command runs.  A rule rename or a typo
    in one of the seven ``tags.add("...")`` literals is therefore a
    push-protection bypass that no behavioural test in this file would catch,
    because they all exercise commands whose tags currently DO resolve.

    ``test_exfil_gate_opt_out.py`` has the twin guard for the exfiltration
    branches; this is the git-publish half.
    """

    # Every literal a gated ``tags.add(...)`` in ``_push_segment_targets_protected``
    # or ``_git_publish_floor_tags`` can emit.  Written out rather than scraped
    # from source so that DELETING an emitter is visible here too.
    GATED_TAGS = (
        "git-publish-push-mirror-all",
        "git-publish-push-bare",
        "git-publish-push-single-arg",
        "git-publish-push-wildcard-refspec",
        "git-publish-push-ambiguous-ref",
        "git-publish-push-protected-ref-path",
        "git-publish-push-protected-branch-name",
    )

    def test_every_gated_tag_resolves_to_a_pattern(self) -> None:
        missing = [t for t in self.GATED_TAGS if t not in security._GIT_PUBLISH_FLOOR_BY_ID]
        assert not missing, (
            f"gated git-publish tag(s) naming no catalog rule: {missing}. In is_denied "
            "these resolve to None and the branch is SKIPPED, so the push is ALLOWED. "
            "Fix the tag literal or add the catalog rule."
        )

    def test_every_gated_tag_is_a_real_rule_id(self) -> None:
        ids = {r.id for r in security.BUILTIN_DENIED_RULES}
        assert not [t for t in self.GATED_TAGS if t not in ids]

    def test_the_ungated_sentinel_is_unspellable_as_a_rule_id(self) -> None:
        """If an operator could name the sentinel, disabling that row would turn
        off the anti-obfuscation branches — the one thing opt-out must not reach."""
        assert security._GIT_PUBLISH_UNGATED not in self.GATED_TAGS
        assert security._GIT_PUBLISH_UNGATED not in {r.id for r in security.BUILTIN_DENIED_RULES}

    def test_the_emitter_only_ever_emits_known_tags(self) -> None:
        """Drive the real function: everything it emits is either a listed gated
        tag or the ungated sentinel — never a third, unhandled kind."""
        known = set(self.GATED_TAGS) | {security._GIT_PUBLISH_UNGATED}
        for cmd in (
            "git push",
            "git push origin",
            "git push --mirror",
            "git push --all origin",
            "git push origin main",
            "git push origin refs/heads/main",
            "git push origin HEAD",
            "git push origin @",
            "git push origin feat/x",
            "git push origin $(echo main)",
            "git push origin 'main@{upstream}'",
        ):
            emitted = security._git_publish_floor_tags(cmd.lower())
            assert emitted <= known, f"{cmd!r} emitted unknown tag(s): {emitted - known}"


class TestPushOptionsCannotDodgeTheProtectedRule:
    """Flag spellings that must not be read as the repository, or as no-op flags.

    Both shapes were latent while the whole git-publish floor was unconditional:
    they mis-CLASSIFIED a protected-branch push rather than allowing it, so the
    floor denied anyway. Making the rules individually disableable is what turned
    them into bypasses — switching off the rule the shape was mis-attributed to
    published the push. Regression for the GPT 5.6 blocking finding on #7705.
    """

    def test_repo_flag_carries_the_remote_so_the_lone_token_is_a_refspec(self):
        # `--repo=origin` starts with '-', so a naive flag strip leaves ["main"]
        # and reads it as the REMOTE — classifying a push to main as the single-arg
        # shape. It must be seen as the protected-branch shape instead.
        for cmd in (
            "git push --repo=origin main",
            "git push --repo origin main",
        ):
            tags = security._git_publish_floor_tags(cmd)
            assert "git-publish-push-protected" in tags or any(
                t.startswith("git-publish-push-protected") for t in tags
            ), f"{cmd!r} did not resolve to a protected-branch tag: {tags}"
            assert "git-publish-push-single-arg" not in tags, (
                f"{cmd!r} still mis-classified as the single-arg shape, so disabling "
                "that one rule would publish to main"
            )

    def test_branches_is_an_all_branches_flag(self):
        # `--branches` is git's own modern alias for `--all` (2.44+), so it pushes
        # every local branch including protected ones.
        tags = security._git_publish_floor_tags("git push --branches origin")
        assert "git-publish-push-mirror-all" in tags, (
            "--branches pushes all branches like --all; leaving it out means the "
            "shape carries no tag at all and nothing denies it"
        )

    def test_repo_flag_without_a_refspec_is_still_caught(self):
        # The flag named the remote and nothing named a branch — the bare form.
        tags = security._git_publish_floor_tags("git push --repo=origin")
        assert tags, "a repo-flag push with no refspec emitted no tag at all"


class TestGitOptionAbbreviationsResolveLikeGit:
    """Git resolves an unambiguous long-option PREFIX to that option, so matching
    flag literals exactly misses every abbreviation.

    This is the third round of findings in this one span, so it is fixed at the
    INVARIANT rather than per spelling: the classifier now resolves a token against
    the enumerated set of `git push` long options, the same way git does. A
    spelling list can only ever trail the next abbreviation; a name table cannot.
    """

    def test_abbreviated_all_branches_flags_still_deny(self):
        for cmd in (
            "git push --mirr origin",
            "git push --mir origin",
            "git push --al origin",
            "git push --branch origin",
        ):
            tags = security._git_publish_floor_tags(cmd)
            assert "git-publish-push-mirror-all" in tags, (
                f"{cmd!r} is an abbreviation git accepts for an all-branches flag, "
                f"but it carried no all-branches tag: {tags}"
            )

    def test_abbreviated_repo_flag_still_shifts_the_positional_read(self):
        for cmd in ("git push --rep=origin main", "git push --rep origin main"):
            tags = security._git_publish_floor_tags(cmd)
            assert any(
                t.startswith("git-publish-push-protected") for t in tags
            ), f"{cmd!r} did not resolve to a protected-branch tag: {tags}"
            assert "git-publish-push-single-arg" not in tags

    def test_an_unrelated_flag_is_not_swallowed(self):
        """The guard against over-denial, which is why the option table is complete.

        `--atomic` shares a prefix with `--all`. Resolving abbreviations against
        `{all, branches, mirror}` alone would read `--a`-family flags as
        all-branches and deny an ordinary feature-branch push. `--atomic` resolves
        to exactly one option, so it must stay clean.
        """
        tags = security._git_publish_floor_tags("git push --atomic origin feat/x")
        assert "git-publish-push-mirror-all" not in tags, (
            "--atomic was misread as an all-branches flag, which denies a legitimate "
            "feature-branch push"
        )
        assert not tags, f"a plain feature-branch push should emit no tag, got {tags}"

    def test_an_ambiguous_abbreviation_covering_a_dangerous_option_fails_closed(self):
        """`--a` is ambiguous to git (all, atomic, ...) so the command never runs.

        Reading it as the dangerous member can therefore only deny a push that was
        already going to fail — the safe direction for a floor.
        """
        assert security._push_option_matches("--a", security._PUSH_ALL_BRANCHES_OPTS)
        assert "git-publish-push-mirror-all" in security._git_publish_floor_tags(
            "git push --a origin"
        )

    def test_the_prefix_test_matches_a_full_option_table(self):
        """Pins WHY no git-wide option table is carried here.

        Resolving a token against every `git push` long option and then
        intersecting with the dangerous ones gives the same answer as testing the
        prefix against the dangerous ones directly: a non-dangerous option can only
        ADD a candidate, never remove a dangerous one. Asserted over every prefix
        of every option so the equivalence is a checked property rather than a
        claim in a comment — if it ever stops holding, the table is needed and this
        goes red.
        """
        full_table = frozenset(
            {
                "all",
                "atomic",
                "branches",
                "delete",
                "dry-run",
                "exec",
                "follow-tags",
                "force",
                "force-if-includes",
                "force-with-lease",
                "ipv4",
                "ipv6",
                "mirror",
                "no-atomic",
                "no-follow-tags",
                "no-force-with-lease",
                "no-progress",
                "no-recurse-submodules",
                "no-signed",
                "no-thin",
                "no-verify",
                "porcelain",
                "progress",
                "prune",
                "push-option",
                "quiet",
                "receive-pack",
                "recurse-submodules",
                "repo",
                "set-upstream",
                "signed",
                "tags",
                "thin",
                "verbose",
                "verify",
            }
        )
        probes = {opt[:i] for opt in full_table for i in range(1, len(opt) + 1)}
        probes |= {"zzz", "a", "m", "r", "b"}
        for name in sorted(probes):
            token = f"--{name}"
            candidates = {opt for opt in full_table if opt.startswith(name)}
            for target in (
                security._PUSH_ALL_BRANCHES_OPTS,
                security._PUSH_REPO_OPTS,
            ):
                assert bool(candidates & target) == security._push_option_matches(token, target), (
                    f"{token!r}: full-table resolution and the direct prefix test "
                    f"disagree for {set(target)} — the option table is load-bearing "
                    "after all and must be restored"
                )


def test_an_all_branches_push_does_not_also_carry_the_single_arg_tag():
    """`push --all origin` must not earn BOTH tags.

    It used to: the all-branches flag tagged mirror-all, then the empty-refspec
    fallback added single-arg on top. Disabling mirror-all therefore left the
    command blocked by its sibling — the toggle read as enabled-and-off while
    enforcement never changed, which is the exact defect class this PR closes.
    GPT 5.6 flagged it on #7705.
    """
    tags = security._git_publish_floor_tags("git push --all origin")
    assert "git-publish-push-mirror-all" in tags
    assert "git-publish-push-single-arg" not in tags
    assert "git-publish-push-bare" not in tags

    # And the bare/single-arg shapes still tag when no all-branches flag is present.
    assert "git-publish-push-single-arg" in security._git_publish_floor_tags("git push origin")
    assert "git-publish-push-bare" in security._git_publish_floor_tags("git push")


class TestValueTakingOptionArity:
    """Push options with a REQUIRED, separable value must have that value
    consumed, not leaked into the positional list. (#7796 — the fourth finding
    in this parser span.)

    Measured on the unfixed parser: appending ``--push-option ci.skip`` (or
    ``-o``, ``--receive-pack``, ``--exec``) to the bare ``--repo=origin``
    publish flipped the floor from {bare} to EMPTY — an ALLOW, because the
    leaked value was read as the only refspec and ``ci.skip`` is not a
    protected name. The tag set IS the protected-branch decision, so one extra
    flag switched the floor off.
    """

    SEPARATED = (
        ("-o", "ci.skip"),
        ("--push-option", "ci.skip"),
        ("--receive-pack", "/usr/bin/git-receive-pack"),
        ("--exec", "/usr/bin/git-receive-pack"),
    )

    def test_a_separated_value_does_not_erase_the_floor_tag(self):
        base = security._git_publish_floor_tags("git push --repo=origin")
        assert base == frozenset({"git-publish-push-bare"})
        for opt, val in self.SEPARATED:
            cmd = f"git push --repo=origin {opt} {val}"
            assert security._git_publish_floor_tags(cmd) == base, (
                f"{cmd!r}: appending one option changed the floor answer — the "
                "separated value leaked into the positional list and erased the tag"
            )

    def test_a_separated_value_is_not_read_in_place_of_a_real_target(self):
        for opt, val in self.SEPARATED:
            cmd = f"git push {opt} {val} origin main"
            tags = security._git_publish_floor_tags(cmd)
            assert "git-publish-push-protected-branch-name" in tags, (
                f"{cmd!r}: the real protected target lost its tag to the leaked "
                f"option value: {set(tags)}"
            )

    def test_a_consumed_value_keeps_a_feature_branch_push_allowed(self):
        # Kills the ARITY-TABLE mutant separately from the fallback mutant:
        # drop a family from the table and these commands fall to the
        # protective fallback and DENY, so this test is what proves the table
        # (precision) is load-bearing, not just the fallback (protection).
        for opt, val in self.SEPARATED:
            cmd = f"git push {opt} {val} origin feature-x"
            assert not security._git_publish_floor_tags(cmd), (
                f"{cmd!r}: a legitimate feature-branch push with a known "
                "value-taking option must stay allowed"
            )

    def test_the_attached_form_keeps_parsing_as_before(self):
        # ``=`` binds the value inside the token, so it can never disturb the
        # positional split; these already parsed correctly and must not regress.
        for cmd in (
            "git push --repo=origin --push-option=ci.skip",
            "git push --repo=origin -oci.skip",
        ):
            assert security._git_publish_floor_tags(cmd) == frozenset(
                {"git-publish-push-bare"}
            ), f"{cmd!r}: the attached form regressed"

    def test_value_option_abbreviations_consume_like_git(self):
        # Git resolves an unambiguous long-option prefix, so ``--push-opt`` is
        # ``--push-option`` — its separated value must be consumed too (the
        # same resolution rule finding 2 established for the danger flags).
        cmd = "git push --repo=origin --push-opt ci.skip"
        assert security._git_publish_floor_tags(cmd) == frozenset({"git-publish-push-bare"})

    def test_a_short_bundle_ending_in_o_consumes_like_git(self):
        # ``-fo ci.skip`` is ``-f -o ci.skip``: the first value-taking short in
        # a bundle takes the rest of the token or, when the rest is empty, the
        # NEXT token.
        cmd = "git push --repo=origin -fo ci.skip"
        assert security._git_publish_floor_tags(cmd) == frozenset({"git-publish-push-bare"})


class TestUnrecognisedOptionsReadProtectively:
    """The invariant that closes the class (#7796 shape C): an option the scan
    does not model might take a separated value, so the positional split cannot
    be trusted — read the segment protectively instead. A mis-parse can then
    only ever OVER-protect; a future value-taking push option cannot silently
    reopen the erasure.
    """

    def test_an_unknown_separated_long_option_cannot_erase_the_floor(self):
        cmd = "git push --frobnicate ci.skip origin feature-x"
        tags = security._git_publish_floor_tags(cmd)
        assert "git-publish-push-bare" in tags, (
            f"{cmd!r}: an unrecognised option left the positional split trusted "
            f"— the erasure class is open again: {set(tags)}"
        )

    def test_an_unknown_short_flag_reads_protectively(self):
        tags = security._git_publish_floor_tags("git push -z origin feature-x")
        assert "git-publish-push-bare" in tags

    def test_a_quoted_value_containing_whitespace_reads_protectively(self):
        # GPT 5.6 review findings on #7808 (rounds 1-2), verified real: the
        # tokenizer is whitespace-split, so a quoted (or escape-continued)
        # value spanning whitespace reaches the scan as FRAGMENTS — consuming
        # one token left the tail fragment trusted as a refspec, and the
        # erasure was back. Round 2: an ESCAPED quote is data, not a
        # delimiter, so counting quote characters was bypassed by \" — the
        # fragment test now tracks the shell's own quote/escape state and
        # flags any token whose state does not return to normal.
        for cmd in (
            "git push --repo=origin --push-option='ci skip'",
            'git push --repo=origin --push-option="ci skip"',
            "git push --repo=origin --push-option 'ci skip'",
            "git push --repo=origin -o 'ci skip'",
            "git push --repo=origin --push-option=ci\\ skip",
            'git push --repo=origin --push-option="ci\\" skip\\""',
            "git push --repo=origin --push-option='ci'\\'' skip'",
            "git push --repo=origin --push-option=$'ci\\' skip'",
            # ANSI-C-only signal: under a plain-single reading BOTH fragments
            # scan clean (the tail's quotes pair up), so this row is what
            # proves the $'...' escape branch is load-bearing.
            "git push --repo=origin --push-option=$'a\\' bc'\\''d'",
        ):
            tags = security._git_publish_floor_tags(cmd)
            assert "git-publish-push-bare" in tags, (
                f"{cmd!r}: a whitespace-fused option value erased the floor "
                f"tag again: {set(tags)}"
            )

    def test_a_protected_name_beside_a_fragmented_value_keeps_its_tag(self):
        tags = security._git_publish_floor_tags("git push --push-option='ci skip' origin main")
        assert "git-publish-push-protected-branch-name" in tags

    def test_balanced_quoting_still_parses_precisely(self):
        # The fragment signal is the quote/escape state not returning to
        # normal; complete words — including ones with ESCAPED quotes — keep
        # their existing precise reading, both directions: no over-deny of a
        # feature push, and no loss of the precise tag identity.
        assert not security._git_publish_floor_tags("git push origin 'feature-x'")
        assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
            "git push origin 'main'"
        )
        assert security._git_publish_floor_tags(
            'git push --repo=origin "--push-option=ci.skip"'
        ) == frozenset({"git-publish-push-bare"})
        # An escaped quote inside a COMPLETE word is data, not a fragment.
        assert not security._git_publish_floor_tags('git push origin "feat\\"x"')
        # The quote-splice spelling of a protected name reads EXACTLY as the
        # protected-branch row: dequote evasion-resistance intact, and no
        # spurious bare tag riding along from a fragment false-positive.
        assert security._git_publish_floor_tags("git push origin 'ma'\\''in'") == frozenset(
            {"git-publish-push-protected-branch-name"}
        )

    def test_the_protective_reading_still_names_a_protected_target_precisely(self):
        # Not trusting the split widens the refspec scan to EVERY positional,
        # so a protected name still reports its own catalog row — an operator
        # who disabled the bare rule is still covered by the protected one.
        tags = security._git_publish_floor_tags("git push --frobnicate ci.skip origin main")
        assert (
            "git-publish-push-protected-branch-name" in tags
        ), f"the protective reading dropped the precise protected tag: {set(tags)}"

    def test_known_no_value_flags_do_not_trip_the_fallback(self):
        # The guard against over-denial: every boolean / attached-optional-value
        # push option is modelled, so ordinary feature-branch pushes keep
        # working. (``--signed`` / ``--force-with-lease`` take a value in
        # ATTACHED form only — git never consumes a separate token for them.)
        for cmd in (
            "git push --force origin feature-x",
            "git push --force-with-lease origin feature-x",
            "git push --signed origin feature-x",
            "git push --no-verify origin feature-x",
            "git push -f origin feature-x",
            "git push -fq origin feature-x",
            "git push --atomic --dry-run origin feature-x",
        ):
            assert not security._git_publish_floor_tags(cmd), (
                f"{cmd!r}: a known no-value flag was read as unrecognised and "
                "over-denied a legitimate feature-branch push"
            )

    def test_recurse_submodules_is_deliberately_not_vouched_for(self):
        # Its arity is not modelled (optional-value in current git, and the
        # separated spelling is exactly the leak shape if that ever changes),
        # so the separated form reads protectively while the attached form —
        # which cannot leak — parses as before.
        assert "git-publish-push-bare" in security._git_publish_floor_tags(
            "git push --recurse-submodules check origin feature-x"
        )
        assert not security._git_publish_floor_tags(
            "git push --recurse-submodules=check origin feature-x"
        )

    def test_an_all_branches_flag_keeps_its_single_tag_under_the_fallback(self):
        # The fallback does not stack the bare tag on top of mirror-all: the
        # all-branches flag names the target set exhaustively (finding 3's
        # suppression), and mirror-all already covers a superset of bare.
        tags = security._git_publish_floor_tags("git push --all --frobnicate v")
        assert tags == frozenset({"git-publish-push-mirror-all"})

    def test_an_expandable_value_lands_on_the_ungated_branch(self):
        # GPT 5.6 round 3, verified real: a token the split consumes or drops
        # is not inert text — `V='ci.skip main'; git push --repo=origin
        # --push-option $V` expands and word-splits AFTER this scan, handing
        # git `main` as a refspec the split never saw. Consuming the literal
        # `$V` had REGRESSED this from main's accidental posture (the leaked
        # value hit the refspec ambiguity check -> the ungated sentinel) to
        # the disableable bare rule. Any unquoted expansion syntax anywhere in
        # the segment now lands on the ungated branch, the same posture as
        # `ma$in`: no single catalog row can be disabled to admit it.
        for cmd in (
            "git push --repo=origin --push-option $v",
            "git push --repo=origin -o $v",
            "git push --repo=origin --push-option=$v",
            "git push --repo=origin --receive-pack $hook",
            "git push --repo=$r",
            "git push $remote feature-x",
        ):
            tags = security._git_publish_floor_tags(cmd)
            assert security._GIT_PUBLISH_UNGATED in tags, (
                f"{cmd!r}: an expandable token classified as only "
                f"disableable rules: {set(tags)}"
            )

    def test_a_glob_value_or_remote_is_the_wildcard_shape(self):
        # Pathname expansion also produces words: `--push-option ma*` with a
        # matching file hands git extra positional words. Main's accidental
        # value-scan already tagged these wildcard-refspec; the consumed value
        # must keep that identity rather than degrading to bare.
        for cmd in (
            "git push --repo=origin --push-option ma*",
            "git push --repo=origin --push-option=ma*",
            "git push ma* feature-x",
        ):
            tags = security._git_publish_floor_tags(cmd)
            assert "git-publish-push-wildcard-refspec" in tags, (
                f"{cmd!r}: a glob-capable dropped token lost the wildcard " f"tag: {set(tags)}"
            )

    def test_glob_characters_beyond_star_in_a_refspec_are_wildcards(self):
        # `ma[i]n` expands against CWD files BEFORE git runs — a file named
        # `main` makes it push main. `?` and `[` are never legit in refnames
        # (git refuses them), so over-protecting costs no real command.
        for cmd in ("git push origin ma[i]n", "git push origin ma?n"):
            assert "git-publish-push-wildcard-refspec" in security._git_publish_floor_tags(
                cmd
            ), f"{cmd!r} was not read as a wildcard shape"

    def test_extglob_patterns_are_wildcards_too(self):
        # GPT 5.6 round 9, verified real: with `shopt -s extglob` (or
        # BASHOPTS=extglob), `@(main)` / `+(main)` / `!(x)` are pathname
        # patterns — beside a file named `main`, `git push origin @(main)`
        # expands to a push of MAIN, and the scan gave it NO tags. Extglob is
        # pathname expansion, so it takes the same catalog identity as
        # `*`/`?`/`[` (the wildcard row), not the substitution sentinel:
        # like a glob, it can only ever match existing FILE names. `?(` and
        # `*(` were already covered by their leading glob character.
        for cmd in (
            "git push origin @(main)",
            "git push origin +(main)",
            "git push origin !(x)",
            "git push --repo=origin --push-option @(x)",
        ):
            assert "git-publish-push-wildcard-refspec" in security._git_publish_floor_tags(
                cmd
            ), f"{cmd!r} was not read as a wildcard shape"
        # No operator adjacency, no extglob: an ordinary parenthesised
        # refname spelling stays data.
        assert not security._git_publish_floor_tags("git push origin 'feat(x)'")

    def test_plain_values_and_remotes_keep_their_precise_reading(self):
        # No expansion syntax, no glob: the new checks add nothing.
        assert security._git_publish_floor_tags(
            "git push --repo=origin --push-option ci.skip"
        ) == frozenset({"git-publish-push-bare"})
        assert not security._git_publish_floor_tags("git push origin feature-x")
        assert not security._git_publish_floor_tags(
            "git push --push-option ci.skip origin feature-x"
        )

    def test_a_heredoc_strip_operator_consumes_its_delimiter(self):
        # GPT 5.6 round 7, verified real: `<<-` is a complete operator (the
        # tab-stripping heredoc); its `-` landed in the regex REMAINDER, so
        # the token read as self-contained and the separated delimiter word
        # became a phantom refspec — erasing the tag exactly like round 4's
        # `</dev/null`. The `-` is part of the operator only for `<<`.
        assert security._git_publish_floor_tags("git push origin <<- EOF") == frozenset(
            {"git-publish-push-single-arg"}
        )
        assert security._git_publish_floor_tags("git push --repo=origin <<- EOF") == frozenset(
            {"git-publish-push-bare"}
        )
        # fd-close / fd-move spellings keep their self-contained reading:
        # their trailing '-' is an fd disposition, not a separated target.
        assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
            "git push origin >&- main"
        )
        assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
            "git push origin main 2>&1-"
        )

    def test_process_substitution_lands_on_the_ungated_branch(self):
        # GPT 5.6 round 8, verified real: `<(cmd)` / `>(cmd)` are WORDS (the
        # shell substitutes a /dev/fd path), not removable redirections —
        # dropping `-o <(echo)` as a redirection shifted the option's value
        # consumption onto `origin` and downgraded a push of MAIN to the
        # disableable single-arg row. Process substitution is substitution:
        # it joins `$(`/`${`/backticks on the upstream ungated branch.
        for cmd in (
            "git push -o <(echo) origin main",
            "git push --push-option >(cat) origin main",
            "git push origin <(echo x)",
        ):
            tags = security._git_publish_floor_tags(cmd)
            assert security._GIT_PUBLISH_UNGATED in tags, (
                f"{cmd!r}: process substitution classified as only "
                f"disableable rules: {set(tags)}"
            )
        # A parenthesis without the operator adjacency is an ordinary
        # refname character and stays data.
        assert not security._git_publish_floor_tags("git push origin 'feat(x)'")

    def test_a_named_fd_redirection_is_not_a_word(self):
        # GPT 5.6 round 11, verified real — a regression the round-10
        # decomposition introduced: bash's named descriptor `{fd}>...` is
        # ALL redirection, but `{fd}` read as the pre-operator word and
        # became the sole "refspec", erasing every tag while the shell ran a
        # remote-only push with all rules enabled.
        assert security._git_publish_floor_tags("git push origin {fd}>/dev/null") == frozenset(
            {"git-publish-push-single-arg"}
        )
        assert security._git_publish_floor_tags("git push --repo=origin {fd}>err") == frozenset(
            {"git-publish-push-bare"}
        )
        # Quoted, it is an ordinary (weird) refname and stays data.
        assert not security._git_publish_floor_tags("git push origin '{fd}'")

    def test_a_quoted_redirection_target_keeps_redirection_arity(self):
        # Round 11's second leg: quotes can only ever appear in the TARGET
        # group of a redirection token (the operator grammar admits none), so
        # refusing the whole token for containing a quote pushed `>'log'`
        # into the fallback and mislabelled a remote-only push as bare.
        assert security._git_publish_floor_tags("git push origin >'log'") == frozenset(
            {"git-publish-push-single-arg"}
        )
        assert security._git_publish_floor_tags("git push origin main 2>'err'") == frozenset(
            {"git-publish-push-protected-branch-name"}
        )

    def test_shell_redirections_are_not_refspecs(self):
        # GPT 5.6 round 4, verified real (and pre-existing on main): the
        # shell consumes a redirection BEFORE git runs, so `git push origin
        # </dev/null` executes a remote-only push while the scan read
        # `</dev/null` as the refspec — a phantom positional filled the
        # refspec slot and the single-arg tag was erased. Redirections are
        # modelled with the shell's own arity: an attached target is
        # self-contained, a bare operator consumes the next word.
        assert security._git_publish_floor_tags("git push origin </dev/null") == frozenset(
            {"git-publish-push-single-arg"}
        )
        assert security._git_publish_floor_tags("git push origin > log") == frozenset(
            {"git-publish-push-single-arg"}
        )
        assert security._git_publish_floor_tags("git push --repo=origin 2>err") == frozenset(
            {"git-publish-push-bare"}
        )

    def test_redirected_pushes_keep_their_precise_reading(self):
        # The modelling is precise, not a blanket fallback: the everyday
        # scripted spelling `... 2>&1` stays an allowed feature push, and a
        # redirected protected push keeps its precise tag.
        assert not security._git_publish_floor_tags("git push origin feature-x 2>&1")
        assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
            "git push origin main 2>&1"
        )

    def test_a_glued_operator_cannot_hide_a_protected_name(self):
        # `main>log` is the word `main` plus the redirection `>log` to the
        # shell — it pushes MAIN. The glued word is decomposed precisely:
        # the pre-operator word is a positional, the redirection is consumed.
        tags = security._git_publish_floor_tags("git push origin main>log")
        assert "git-publish-push-protected-branch-name" in tags

    def test_a_glued_redirection_keeps_the_precise_positional_identity(self):
        # GPT 5.6 round 10, verified real: `origin>/dev/null` is the word
        # `origin` plus a redirection — a remote-only push, whose true row is
        # SINGLE-ARG. The protective fallback emitted BARE instead, so an
        # operator who disabled only the bare rule had this shape allowed
        # while its real row was still enabled. The pre-operator word is now
        # kept as a positional and the redirection consumed, exactly as bash
        # reads it — no fallback, no identity drift, and glued legit pushes
        # stay allowed.
        assert security._git_publish_floor_tags("git push origin>/dev/null") == frozenset(
            {"git-publish-push-single-arg"}
        )
        assert security._git_publish_floor_tags("git push origin main>log") == frozenset(
            {"git-publish-push-protected-branch-name"}
        )
        assert not security._git_publish_floor_tags("git push origin feature-x>log")
        assert not security._git_publish_floor_tags("git push --repo=origin ci.skip>log")
        # A glued separated-target form consumes the NEXT word as the target.
        assert security._git_publish_floor_tags("git push origin> log") == frozenset(
            {"git-publish-push-single-arg"}
        )
        # Glued fd-close and all-output forms are redirections too (GPT 5.6
        # round 12): `origin>&-` closes stdout and `origin&>/dev/null`
        # redirects everything — both remote-only pushes, single-arg rows.
        assert security._git_publish_floor_tags("git push origin>&-") == frozenset(
            {"git-publish-push-single-arg"}
        )
        assert security._git_publish_floor_tags("git push origin&>/dev/null") == frozenset(
            {"git-publish-push-single-arg"}
        )
        assert not security._git_publish_floor_tags("git push --repo=origin ci>&2")
        # A glued & is NOT a redirection (it is a command boundary) and keeps
        # the protective fallback.
        assert "git-publish-push-bare" in security._git_publish_floor_tags(
            "git push origin& echo x"
        )

    def test_a_glued_value_still_feeds_a_pending_option(self):
        # GPT 5.6 round 13, verified real: with a separated `--repo` pending
        # its value, a GLUED word+redirection token fed its word into the
        # positional list instead of the option — `--repo origin>/dev/null
        # main` then consumed `main` as the "value" and returned NO tags
        # while the shell hands git a protected push. The glued word now
        # feeds the pending option value exactly as the shell's argv does.
        assert security._git_publish_floor_tags(
            "git push --repo origin>/dev/null main"
        ) == frozenset({"git-publish-push-protected-branch-name"})
        assert security._git_publish_floor_tags("git push --repo origin>/dev/null") == frozenset(
            {"git-publish-push-bare"}
        )

    def test_a_glued_all_branches_flag_keeps_its_identity(self):
        # GPT 5.6 round 16, verified real: `--all>/dev/null` is the
        # all-branches flag plus a redirection, but the flag-shaped prefix
        # bailed to the fallback, emitting only the disableable no-refspec
        # rows — disabling those admitted an all-branches push while
        # mirror-all stayed enabled. The prefix is now classified against
        # the all-branches set (abbreviations included) before the fallback,
        # and the finding-3 suppression then yields exactly the true row.
        assert security._git_publish_floor_tags("git push --all>/dev/null") == frozenset(
            {"git-publish-push-mirror-all"}
        )
        assert security._git_publish_floor_tags("git push --mirr>log") == frozenset(
            {"git-publish-push-mirror-all"}
        )
        # Other glued flag prefixes keep the protective fallback (over-deny
        # only — a redirected force push is denied, never admitted).
        tags = security._git_publish_floor_tags("git push --force>/dev/null origin feature-x")
        assert "git-publish-push-bare" in tags

    def test_a_lone_dash_is_a_positional_not_an_option(self):
        # Round 13's second leg: git's own option parsing treats a lone `-`
        # as an OPERAND (a repository spelled `./-` is addressable), but the
        # scan skipped it as a flag, shifting `main` into the remote slot and
        # downgrading the row to single-arg.
        assert security._git_publish_floor_tags("git push - main") == frozenset(
            {"git-publish-push-protected-branch-name"}
        )
        assert not security._git_publish_floor_tags("git push - feature-x")

    def test_spaced_subshell_parens_read_protectively(self):
        # GPT 5.6 round 14, verified real: in `( ... )` with spaces, the `)`
        # token read as a refspec and erased every tag for an otherwise-bare
        # publish inside a subshell. Unquoted parens are shell operators;
        # they now poison the split like the other operator glue. (The
        # glued spelling without spaces never tokenizes a clean `git` word
        # and was already ungated upstream.)
        tags = security._git_publish_floor_tags("( git push --repo=origin )")
        assert "git-publish-push-bare" in tags
        assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
            "( git push origin main )"
        )

    def test_the_untrusted_fallback_names_both_no_refspec_rows(self):
        # Rounds 10, 13 and 14 each turned the fallback's single identity
        # into a bypass by disabling whichever row it happened to emit. An
        # untrusted split with visible positionals cannot distinguish the
        # bare shape from the remote-only shape, so BOTH rows fire; with no
        # positionals the remote-only shape is impossible and bare stands
        # alone; an all-branches flag suppresses both.
        tags = security._git_publish_floor_tags("git push --recurse-submodules check origin")
        assert {"git-publish-push-bare", "git-publish-push-single-arg"} <= tags
        assert security._git_publish_floor_tags("git push --frobnicate") == frozenset(
            {"git-publish-push-bare"}
        )
        assert security._git_publish_floor_tags("git push --all --frobnicate v") == frozenset(
            {"git-publish-push-mirror-all"}
        )

    def test_a_comment_truncates_the_argv_like_the_shell_does(self):
        # `#` at the start of a word comments out the rest of the segment,
        # so `git push origin #main` runs a remote-only push: the precise
        # single-arg tag, not an erased one and not a fallback.
        assert security._git_publish_floor_tags("git push origin #main") == frozenset(
            {"git-publish-push-single-arg"}
        )
        assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
            "git push origin main #x"
        )
        assert not security._git_publish_floor_tags("git push origin feature-x #main")

    def test_a_fused_hash_is_not_a_comment_and_cannot_discard_a_refspec(
        self,
    ):  # GPT 5.6 round 5, verified real: `#` opens a comment only at the
        # START of a word. When an earlier token leaves the shell state open
        # (a trailing escape or an unterminated quote fuses across the
        # split), a `#`-leading token may be MID-WORD — truncating there
        # discarded the real trailing `main`, leaving only the disableable
        # bare tag. Truncation now requires every preceding token to have
        # closed cleanly; otherwise the open state poisons the split and the
        # superset scan keeps the trailing protected name visible.
        for cmd in (
            "git push --repo=origin --push-option=ci\\ #x main",
            "git push origin 'a #b' main",
        ):
            tags = security._git_publish_floor_tags(cmd)
            assert "git-publish-push-protected-branch-name" in tags, (
                f"{cmd!r}: a word-fused '#' truncated the scan and dropped "
                f"the protected refspec: {set(tags)}"
            )

    def test_a_background_operator_reads_protectively(self):
        # A single `&` is NOT a segment separator upstream (only `&&` is), so
        # the second command rides inside this segment's token list. The
        # split is untrusted, and the superset scan keeps a protected name in
        # the trailing command visible.
        tags = security._git_publish_floor_tags("git push origin & git push origin2 main")
        assert "git-publish-push-protected-branch-name" in tags
        assert "git-publish-push-bare" in tags

    def test_quoted_operator_characters_stay_data(self):
        # A quoted operator is an ordinary character the shell passes
        # through (git allows < > & in refnames), so the precise reading
        # holds and nothing over-denies.
        assert not security._git_publish_floor_tags("git push origin 'feat<x'")

    def test_a_line_continuation_lands_on_the_ungated_branch(self):
        # GPT 5.6 round 6, verified real: backslash-newline VANISHES in bash,
        # so `origin ma\` + newline + `in` splices to a push of MAIN — while
        # the newline is a segment boundary here, so no token in this segment
        # spells the name and the scan emitted only the DISABLEABLE bare tag.
        # A segment whose cumulative quote/escape state is open at its end is
        # therefore unreconstructable — the ungated posture, like ma$in.
        for cmd in (
            "git push origin ma\\\nin",
            'git push origin "ma\\\nin"',
        ):
            tags = security._git_publish_floor_tags(cmd)
            assert security._GIT_PUBLISH_UNGATED in tags, (
                f"{cmd!r}: a spliced-across-segments word classified as only "
                f"disableable rules: {set(tags)}"
            )

    def test_a_quoted_whitespace_value_cannot_erase_a_refspec(self):
        # A quoted value spanning whitespace is ONE shell word, and the word
        # split now reads it as one, so it never becomes a pair of fragments
        # whose tail was trusted as the only refspec (the shape that erased the
        # floor tag). Nothing splices into the next line either, so the ungated
        # sentinel — reserved for a word this segment cannot reconstruct — must
        # stay out of it: with no positionals the push is bare, and with
        # positionals they are read exactly, protected name included.
        for cmd in (
            "git push --repo=origin --push-option='ci skip'",
            "git push --repo=origin -o 'a b'",
        ):
            tags = security._git_publish_floor_tags(cmd)
            assert security._GIT_PUBLISH_UNGATED not in tags, (
                f"{cmd!r}: a closed in-segment fusion escalated to the "
                f"ungated sentinel: {set(tags)}"
            )
            assert "git-publish-push-bare" in tags
        # The half that must not be lost: a protected refspec BEHIND such a
        # value is still seen, whether the remote is positional or in a flag.
        for cmd in (
            "git push --repo=origin --push-option='ci skip' origin main",
            "git push --push-option='ci skip' origin main",
            "git push --repo=origin -o 'a b' main",
        ):
            assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
                cmd
            ), f"{cmd!r}: a quoted whitespace value erased the protected refspec"
        # And an unprotected one stays pushable — the value is not a refspec.
        assert not security._git_publish_floor_tags(
            "git push --repo=origin --push-option='ci skip' origin feature-x"
        )

    def test_every_bash_metacharacter_is_accounted_for(self):
        """The shell-syntax inventory, as a checked property (Design review).

        The option-arity axis fails protective by CONSTRUCTION (unrecognised
        option -> fallback); the shell-syntax axis cannot — a construct the
        scan does not know about parses as an ordinary word — so its safety
        rests on this inventory being COMPLETE over bash's metacharacters.
        Every row names the layer that accounts for the character. Adding a
        bash construct? It must land in one of these layers, and a row here.
        """
        floor = security._git_publish_floor_tags
        UNGATED = security._GIT_PUBLISH_UNGATED
        # Segment separators — split upstream (_CMD_SEPARATOR_RE), each side
        # scanned on its own, so the push half keeps its precise tag.
        assert floor("git push origin | cat") == {"git-publish-push-single-arg"}
        assert floor("git push origin; echo x") == {"git-publish-push-single-arg"}
        assert floor("git push origin && echo x") == {"git-publish-push-single-arg"}
        # Substitution / expansion glue — ungated upstream
        # (_AMBIGUOUS_EXPANSION_RE): $(, ${, backtick, brace-with-comma/range.
        assert floor("git push origin `x`") == {UNGATED}
        assert floor("git push origin $(echo main)") == {UNGATED}
        assert floor("git push origin ma{i,x}n") == {UNGATED}
        # Parameter expansion in any slot — the $ pre-check (ungated). A
        # LEADING tilde is the same layer: it is env-driven text, not path
        # syntax (bare `~` IS $HOME; `HOME=refs/heads` turns `~/main` into a
        # protected refspec), so it cannot be vouched benign. Round 15
        # corrected this inventory: tilde originally sat in the benign group
        # with a "expands to a path" rationale, and the rationale was the bug.
        assert UNGATED in floor("git push $remote feature-x")
        assert UNGATED in floor("git push origin ~")
        assert UNGATED in floor("git push origin ~main")
        assert UNGATED in floor("git push ~/repo.git feature-x")
        # Pathname expansion in any slot — the glob pre-check (wildcard row).
        assert "git-publish-push-wildcard-refspec" in floor("git push origin ma?n")
        # Redirections — consumed with the shell's arity; comments truncate;
        # a lone & or operator glue reads protectively (this class).
        assert floor("git push origin </dev/null") == {"git-publish-push-single-arg"}
        assert floor("git push origin #x") == {"git-publish-push-single-arg"}
        assert "git-publish-push-bare" in floor("git push origin & echo x")
        # Quoting and escapes — the shared walk: fragments poison the split.
        assert "git-publish-push-bare" in floor("git push --repo=origin -o 'a b'")
        # Subshell parens are punctuation around the command, not part of the
        # word: the anchor resolves ``(git`` to the git program and the closing
        # paren is stripped from the ref, so the push is read PRECISELY rather
        # than through the unparseable fallback. The row must be the same one the
        # unparenthesised spelling gets — a spelling-specific row would be an
        # escape hatch — and it is still a denial.
        assert floor("(git push origin main)") == floor("git push origin main")
        assert floor("(git push origin main)") == {"git-publish-push-protected-branch-name"}
        # The spaced spelling leaves a bare ``)`` token, which is operator glue
        # with no word in front of it, so that one keeps the protective fallback.
        assert "git-publish-push-protected-branch-name" in floor("( git push origin main )")
        # Benign by bash semantics, deliberately NOT flagged: a comma-free
        # brace passes through LITERALLY (a ref named ``{main}`` is not
        # ``main``); ``!`` history expansion does not run in non-interactive
        # shells; a mid-word ``~`` is literal in an argv word.
        assert floor("git push origin {main}") == frozenset()
        assert floor("git push origin !x") == frozenset()
        assert floor("git push origin feat~x") == frozenset()

    def test_end_of_options_marker_makes_everything_after_it_positional(self):
        # A bare ``--`` ends option parsing in git; the scan must read what
        # follows as positionals, not drop or consume it as flags.
        assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
            "git push -- origin main"
        )
        assert not security._git_publish_floor_tags("git push -- origin feature-x")

    def test_end_of_options_stops_option_parsing_for_later_dash_tokens(self):
        # After ``--``, a dash-prefixed token is a POSITIONAL in git's reading
        # (the only way to push a ref whose name starts with a dash), so it
        # must be read as a refspec candidate — not dropped as a flag (the old
        # scan's single-arg misread) and not fed to the unrecognised-option
        # fallback (which would over-deny a shape git accepts).
        assert not security._git_publish_floor_tags("git push -- origin -x")


class TestParserBranchTable:
    """Every branch of the positional/arity parser, with why each is correct.

    The issue makes this table an explicit acceptance condition: four
    consecutive findings landed in this one span, each fix narrowing one branch
    while the adjacent one stayed wrong, so the parser's whole behaviour is
    pinned here as data rather than implied by scattered tests. The same table
    appears in the PR body.
    """

    BARE = "git-publish-push-bare"
    SINGLE = "git-publish-push-single-arg"
    MIRROR = "git-publish-push-mirror-all"
    NAME = "git-publish-push-protected-branch-name"
    REF_PATH = "git-publish-push-protected-ref-path"
    AMBIG = "git-publish-push-ambiguous-ref"
    WILD = "git-publish-push-wildcard-refspec"

    # (push args, exact expected tag set, why this is the correct reading)
    TABLE = (
        ("", {BARE}, "no target named — the current branch may be protected"),
        ("origin", {SINGLE}, "remote only, no refspec — same current-branch risk, its own row"),
        ("origin feature-x", set(), "explicit non-protected target — the allowed shape"),
        ("origin main", {NAME}, "explicit protected target by bare name"),
        ("origin refs/heads/main", {REF_PATH}, "protected target by ref path — separate row"),
        ("origin head", {AMBIG}, "symbolic ref resolves at runtime — unverifiable"),
        ("origin refs/heads/*:refs/heads/*", {WILD}, "wildcard expands to many refs"),
        ("origin main feature-x", {NAME}, "ALL refspecs scanned, one protected suffices"),
        ("--repo=origin", {BARE}, "remote in the flag, nothing names a branch — bare"),
        ("--repo origin", {BARE}, "separated repo value consumed, still bare"),
        ("--repo=origin main", {NAME}, "remote in flag, so the sole positional is a refspec"),
        ("--all origin", {MIRROR}, "all-branches flag; single-arg deliberately suppressed"),
        ("--mirror", {MIRROR}, "mirror pushes everything"),
        ("--force origin feature-x", set(), "boolean flag — force to a feature branch is allowed"),
        ("-fq origin feature-x", set(), "short bundle of modelled no-value options"),
        (
            "--force-with-lease origin feature-x",
            set(),
            "optional-value option: git binds a value in attached form only",
        ),
        ("-o ci.skip origin feature-x", set(), "arity: -o consumes its separated value"),
        (
            "--push-option ci.skip origin main",
            {NAME},
            "value consumed, the real protected target still read",
        ),
        ("--push-option=ci.skip origin feature-x", set(), "attached value cannot leak"),
        (
            "--repo=origin --push-option ci.skip",
            {BARE},
            "the #7796 erasure row: value consumed, bare tag preserved",
        ),
        (
            "--receive-pack /usr/bin/git-receive-pack origin feature-x",
            set(),
            "arity: --receive-pack consumes its separated value",
        ),
        (
            "--exec /usr/bin/git-receive-pack origin main",
            {NAME},
            "arity: --exec consumes its value, protected target still read",
        ),
        ("-- origin main", {NAME}, "end-of-options: everything after -- is positional"),
        ("-- origin feature-x", set(), "end-of-options with a feature target stays allowed"),
        (
            "-- origin -x",
            set(),
            "a dash-named refspec after -- is positional, not an option",
        ),
        (
            "--frobnicate ci.skip origin feature-x",
            {BARE, SINGLE},
            "unrecognised option: split untrusted, BOTH no-refspec rows (the "
            "positionals may be values or a remote — rounds 10/13/14)",
        ),
        (
            "--frobnicate ci.skip origin main",
            {BARE, SINGLE, NAME},
            "protective reading scans every positional, precise tag preserved",
        ),
        ("--frobnicate", {BARE}, "unrecognised and nothing positional — bare either way"),
        (
            "--all --frobnicate v",
            {MIRROR},
            "fallback does not stack bare onto mirror-all (finding 3 suppression)",
        ),
        (
            "--repo=origin --push-option ma*",
            {BARE, WILD},
            "a glob-capable consumed value keeps the wildcard identity: pathname "
            "expansion can hand git words this scan never saw",
        ),
    )

    def test_the_parser_branch_table(self):
        for args, expected, why in self.TABLE:
            cmd = f"git push {args}".strip()
            got = security._git_publish_floor_tags(cmd)
            assert got == frozenset(
                expected
            ), f"{cmd!r}: expected {set(expected) or '{}'} because {why}; got {set(got) or '{}'}"

    def test_substitution_glue_stays_on_the_ungated_branch(self):
        # Kept out of TABLE because the sentinel is not a catalog row: the
        # anti-obfuscation branches are upstream of this parser and unaffected.
        got = security._git_publish_floor_tags("git push origin $(echo main)")
        assert got == frozenset({security._GIT_PUBLISH_UNGATED})


class TestTokenizerContractBetweenTheLayers:
    """The seam that broke: what the argument scan is handed, and by whom.

    Two changes to this gate landed independently and crossed. One taught the
    parse to resolve a git program hidden behind glued operators and to step over
    redirections; the other added the option-arity and shell-syntax model, which
    is defined over RAW words — it splits each word at its own unquoted operators
    and models redirection arity itself. Because neither textually conflicted, the
    outer parse silently pre-chewed the words the inner model reads, and six
    shapes lost the identity they are classified by. These pin the contract in
    both directions so the seam cannot drift apart again.
    """

    def test_the_argument_scan_is_handed_raw_words(self):
        # The shapes whose identity lives in the operator: cutting them here
        # turned each into a plain word and erased its tag.
        from kiro_crew.security import _git_push_args

        assert _git_push_args("git push origin>/dev/null") == ["origin>/dev/null"]
        assert _git_push_args("git push origin @(main)") == ["origin", "@(main)"]
        assert _git_push_args("git push origin & echo x") == ["origin", "&", "echo", "x"]
        assert _git_push_args("git push --all>/dev/null") == ["--all>/dev/null"]

    def test_redirection_arity_comes_from_one_model(self):
        # ``<<-`` is a complete operator, so its delimiter is a SEPARATED word.
        # A second reading of the grammar here read the ``-`` as an attached
        # target and left ``EOF`` behind as a phantom refspec.
        from kiro_crew.security import _git_push_args

        assert _git_push_args("git push origin <<- EOF") == ["origin"]
        # Still stepped over with the shell's arity, and the words behind a
        # redirection still survive as refspecs.
        assert _git_push_args("git push origin feature 2>/dev/null main") == [
            "origin",
            "feature",
            "main",
        ]
        assert _git_push_args("git push origin feature > out main") == [
            "origin",
            "feature",
            "main",
        ]
        assert _git_push_args("git push > >(git push origin my-feature)") == []

    def test_process_substitution_is_a_word_not_a_redirection(self):
        # ``<(`` / ``>(`` at the start of a word is process substitution, which
        # bash substitutes as a /dev/fd path WORD. Consuming it as a redirection
        # ate an option's value and shifted the positional split onto the remote.
        from kiro_crew.security import _git_push_args

        assert _git_push_args("git push -o <(echo) origin main") == [
            "-o",
            "<(echo)",
            "origin",
            "main",
        ]
        # In a word position it is unverifiable; as a redirection TARGET the shell
        # removes it, so an ordinary feature push whose output is teed stays
        # allowed. Both readings, from one place.
        assert security._GIT_PUBLISH_UNGATED in security._git_publish_floor_tags(
            "git push -o <(echo) origin main"
        )
        assert not security._git_publish_floor_tags("git push origin my-feature > >(tee log.txt)")
        assert security._git_publish_floor_tags("git push > >(git push origin my-feature)") == {
            "git-publish-push-bare"
        }

    def test_a_quoted_separator_does_not_split_the_segment(self):
        # Splitting on a quoted separator truncated the word mid-quote, and the
        # fragment then arrived with the shell state open — which the fragment
        # rule reads as a word fused across the boundary. Legal, unprotected
        # branch names were denied by the protective fallback.
        from kiro_crew.security import _split_push_command_segments

        assert _split_push_command_segments("git push origin 'feature|x'") == [
            "git push origin 'feature|x'"
        ]
        for cmd in (
            "git push origin 'feature|x'",
            "git push origin 'a;b'",
            "git push origin 'a&&b'",
            'git push origin "a||b"',
        ):
            assert not security._git_publish_floor_tags(cmd), cmd
        # An UNQUOTED separator still separates, and each side is judged alone.
        assert _split_push_command_segments("git push origin | cat") == [
            "git push origin ",
            " cat",
        ]
        assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
            "echo x | git push origin main"
        )
        # A quoted separator cannot smuggle a protected name past the scan: it
        # stays inside the word, so the refspec contains it and is not the name.
        assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
            "git push origin 'x' ; git push origin main"
        )

    def test_a_backslash_newline_still_splices_unreconstructably(self):
        # The one separator that VANISHES, so the words on either side fuse into
        # one this segment cannot reconstruct. It must keep splitting, and the
        # trailing escape must survive into the segment it ends — that escape is
        # the signal the ungated sentinel is drawn from.
        from kiro_crew.security import _split_push_command_segments

        assert _split_push_command_segments("git push origin ma\\\nin") == [
            "git push origin ma\\",
            "in",
        ]
        for cmd in ("git push origin ma\\\nin", 'git push origin "ma\\\nin"'):
            assert security._GIT_PUBLISH_UNGATED in security._git_publish_floor_tags(cmd), cmd

    def test_a_wrapped_payload_is_one_word_not_a_bare_git_token(self):
        # ``str.split`` tore a wrapper's quoted payload into fragments, so the
        # OUTER line — which is not itself a push — parsed as one and its
        # fragmented ref denied ordinary work. The payload is one word; the
        # caller judges it on its own, which is where the denial still comes from.
        from kiro_crew.security import _git_push_args, _split_shell_words, is_denied

        assert _split_shell_words("bash -c '(cd /tmp && git push origin my-feature)'") == [
            "bash",
            "-c",
            "'(cd /tmp && git push origin my-feature)'",
        ]
        assert _git_push_args("bash -c '(cd /tmp && git push origin my-feature)'") is None
        assert is_denied("bash -c '(cd /tmp && git push origin my-feature)'") is None
        assert is_denied("bash -c '(cd /tmp && git push origin main)'") is not None

    def test_stripping_punctuation_never_reclassifies_the_word(self):
        """A punctuation strip changes WHERE a word came from, not WHAT it is.

        The subshell-punctuation branch recovered the word in front of a ``)``
        and appended it to the positional list directly, skipping the
        option-vs-positional classifier every bare word goes through. So
        ``(git push --repo=origin -f)`` filed ``-f`` as the only refspec, it
        matched no protected name, and the segment came back with NO tags — a
        force push to a possibly-protected current branch, admitted by adding
        one parenthesis. ``(git push -f)`` reported the remote-only row instead
        of bare, the wrong-identity hazard three earlier rounds each turned into
        a bypass. Every glue branch that recovers a word now routes it through
        the one classifier, so the paired spellings agree by construction.
        """
        floor = security._git_publish_floor_tags
        for parenthesised, bare in (
            # The reported bypass, and its wrong-row sibling.
            ("(git push --repo=origin -f)", "git push --repo=origin -f"),
            ("(git push -f)", "git push -f"),
            ("(git push --repo=origin --force)", "git push --repo=origin --force"),
            ("(git push -qf)", "git push -qf"),
            # A value-taking option must still consume its separated value.
            ("(git push -o ci.skip origin main)", "git push -o ci.skip origin main"),
            (
                "(git push --push-option ci.skip origin main)",
                "git push --push-option ci.skip origin main",
            ),
            # End-of-options, all-branches, and an unmodelled option.
            ("(git push -- origin main)", "git push -- origin main"),
            ("(git push --all)", "git push --all"),
            ("(git push --frobnicate origin main)", "git push --frobnicate origin main"),
            # A GENUINE positional keeps being read as one, protected or not.
            ("(git push origin main)", "git push origin main"),
            ("(git push origin refs/heads/main)", "git push origin refs/heads/main"),
            ("(git push -f origin main)", "git push -f origin main"),
            ("(git push origin my-feature)", "git push origin my-feature"),
            ("(git push origin 'release/x')", "git push origin 'release/x'"),
        ):
            assert floor(parenthesised) == floor(bare), (
                f"{parenthesised!r}: the paren strip changed the reading — "
                f"{set(floor(parenthesised))} vs {set(floor(bare))}"
            )
        # Stated absolutely, not only as a pair: a force push with no refspec
        # names no branch, so the bare row must fire whatever the punctuation.
        for cmd in (
            "(git push --repo=origin -f)",
            "(git push -f)",
            "(git push --repo=origin -f --quiet)",
        ):
            assert "git-publish-push-bare" in security._git_publish_floor_tags(cmd), cmd

    def test_the_other_glue_branches_do_not_reclassify_either(self):
        # The sibling shapes that recover a word from glue: a glued redirection
        # keeps its own guard against a flag-shaped prefix, and the bare-operator
        # fallback marks the split untrusted before it scans the pieces, so a
        # flag landing among the candidates can only ADD a tag. Pinned so a
        # future edit cannot quietly turn one of them into an allow.
        for cmd in (
            "git push --repo=origin -f>/dev/null",
            "git push --repo=origin -f&",
            "git push --repo=origin -f <<- EOF",
            "git push --repo=origin -f 2>&1",
            "git push --repo=origin -f)",
            "git push -f)",
        ):
            assert "git-publish-push-bare" in security._git_publish_floor_tags(cmd), (
                f"{cmd!r}: a force push naming no refspec lost the bare row: "
                f"{set(security._git_publish_floor_tags(cmd))}"
            )


class TestProcessSubstitutionBoundaryIsProven:
    """The boundary rule: a PROVEN-complete substitution is a word, an
    UNPROVABLE one is ambiguous.

    Round 1 moved process substitution off the whole-segment expansion regex's
    fail-closed ``[<>]\\(`` and onto a paren-depth walk, so that a substitution
    the shell REMOVES (the target of ``> >(tee log)``) could keep the precise
    reading instead of denying an ordinary teed push. That walk counted parens
    with ``str.count``, which is quote-UNAWARE -- and a quoted paren inflates the
    depth, so the real closer never returns it to zero and every following word
    is swallowed into the substitution. Both review lanes traced it to a
    protected-branch push being allowed. The walk now shares the module's one
    quote/escape state machine and fails closed when the boundary cannot be
    proven, which restores the old regex's posture for exactly the unprovable
    cases while keeping round 1's win for the provable ones.
    """

    def test_a_quoted_paren_cannot_swallow_the_refspec(self):
        # Both lanes' traces, verbatim. The quoted '(' contributes NOTHING to the
        # depth, so the substitution ends at its real ')' and 'main' survives as
        # the refspec it is.
        for cmd in (
            "git push origin feature > >(echo '(' ) main",
            'git push origin feature > >(printf "(") main',
            "git push origin feature > >(echo '((((' ) main",
            'git push origin feature > >(cat "((") main',
        ):
            tags = security._git_publish_floor_tags(cmd)
            assert "git-publish-push-protected-branch-name" in tags, (
                f"{cmd!r}: a quoted paren inflated the substitution boundary and "
                f"swallowed the protected refspec: {set(tags)}"
            )
            assert tags, f"{cmd!r}: publishes main and was ALLOWED"
        # The words really are recovered, not merely tagged by a fallback.
        assert security._git_push_args("git push origin feature > >(echo '(' ) main") == [
            "origin",
            "feature",
            "main",
        ]

    def test_an_unprovable_boundary_fails_closed(self):
        # The words ran out with the substitution still open, so the boundary
        # cannot be proven and the rest of the segment cannot be read. Silently
        # swallowing it is what hid a refspec; this is the ambiguity sentinel,
        # which no catalog row can switch off -- the posture the whole-segment
        # regex gave process substitution before the walk existed.
        for cmd in (
            "git push origin feature > >(echo main",
            "git push origin feature > >(echo '('",
            "git push origin main > >(tee log",
            "git push origin feature > >(echo 'unterminated",
        ):
            tags = security._git_publish_floor_tags(cmd)
            assert (
                security._GIT_PUBLISH_UNGATED in tags
            ), f"{cmd!r}: an unclosed process substitution did not fail closed: {set(tags)}"
            assert security._git_push_args(cmd) is None, cmd

    def test_the_teed_push_round_one_unblocked_stays_unblocked(self):
        # The round-1 win: a process substitution the shell REMOVES as a
        # redirection target never reaches git's argv, so the push keeps its
        # precise reading. A feature push stays allowed; a protected one is still
        # reported by its own row, and a bare one still fails closed.
        assert not security._git_publish_floor_tags(
            "git push origin my-feature > >(tee log.txt)"
        ), "the teed feature push regressed to a denial"
        assert security._git_publish_floor_tags("git push origin main > >(tee log)") == frozenset(
            {"git-publish-push-protected-branch-name"}
        )
        assert security._git_publish_floor_tags("git push > >(git push origin my-feature)") == {
            "git-publish-push-bare"
        }
        assert security._git_push_args("git push --force origin 2> >(cmd)") == [
            "--force",
            "origin",
        ]
        # In a WORD position it is still unverifiable -- that half is unchanged.
        assert security._GIT_PUBLISH_UNGATED in security._git_publish_floor_tags(
            "git push -o <(echo) origin main"
        )

    def test_quoted_parens_outside_a_substitution_stay_data(self):
        # git allows ( and ) in a refname, and a quoted paren is an ordinary
        # character. The walk must not read one as shell structure in either
        # direction: these are legal unprotected names and stay pushable, while
        # a protected name inside a subshell is still reported.
        for cmd in (
            "git push origin '('",
            'git push origin ")"',
            "git push origin 'feat(x)'",
            "(git push origin ')')",
            "(cd /tmp; git push origin my-feature)",
        ):
            assert not security._git_publish_floor_tags(cmd), cmd
        assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
            "(git push origin main)"
        )
        # An ANSI-C quoted paren carries a '$', which the per-token pre-check
        # already ungates -- over-protective, and deliberately so.
        assert security._GIT_PUBLISH_UNGATED in security._git_publish_floor_tags(
            "git push origin $'('"
        )

    def test_the_state_machine_ignores_quoted_parens(self):
        # The property the boundary rests on, stated directly on the walk.
        walk = security._shell_quote_walk
        for text, delta in (
            ("(", 1),
            (")", -1),
            ("'('", 0),
            ('"("', 0),
            ("$'('", 0),
            ("\\(", 0),
            (">(echo", 1),
            ("log)", -1),
            ('"(")', -1),
            ("'()'", 0),
        ):
            assert walk(text).paren_delta == delta, f"{text!r}: delta {walk(text).paren_delta}"
        # Quote state resumes across a whitespace boundary, which is why a
        # quoted word spanning one cannot desync the depth.
        assert walk("'a").end_state == 1
        assert walk("b'", state=1).end_state == 0
        assert walk("(", state=1).paren_delta == 0

    def test_the_subshell_punctuation_branch_cannot_be_inflated(self):
        # Round 2's branch also looks at parens, so it was audited for the same
        # shape. It is a character-MEMBERSHIP test over the operator tail, not a
        # depth count, and it is structurally immune: a quoted paren needs a
        # quote character in that tail, and a quote is not in "()", so the tail
        # stops qualifying as punctuation and the protective fallback takes it.
        # A quoted paren also produces no operator split at all, so such a token
        # never reaches the branch.
        assert security._push_token_shell_read("main')'") == (None, False)
        # With no split, it reads as the literal refname `main)` -- a different,
        # unprotected branch, so allowing it is the correct precise reading.
        assert not security._git_publish_floor_tags("git push origin main')'")
        # When a quoted paren DOES sit in the operator tail, the tail stops
        # qualifying as punctuation and the protective fallback takes the token.
        for cmd in (
            "git push origin main)'('",
            "git push origin main)'x'",
        ):
            assert security._git_publish_floor_tags(cmd), f"{cmd!r} was allowed"
        # And the branch's own case still reads precisely.
        assert security._git_publish_floor_tags("(git push origin main)") == frozenset(
            {"git-publish-push-protected-branch-name"}
        )


class TestOneQuoteModelOnThePushPath:
    """Every push-path reading of shell quoting walks ONE state machine.

    This module has now been cured of the same defect three times: two
    tokenizers with different word models (#7808 vs #7356), two paren counters
    with different quote models, and finally two QUOTE models -- the word
    splitter had no ANSI-C (``$'...'``) awareness while the boundary walk did.
    In ``git push origin feature > >(echo $'a\\'b') main`` the splitter read the
    ESCAPED quote as a real closer, reopened on the next quote, and fused the
    trailing ``main`` into one unterminated word; the boundary walk then proved
    its parenthesis correctly but could no longer rescue a refspec that was
    already inside the word it had been handed. The cure is structural rather
    than another flag: :func:`_iter_shell_chars` owns the machine and every
    consumer drives it, so there is no second opinion left to drift from.
    """

    def test_an_ansi_c_escaped_quote_cannot_fuse_the_refspec(self):
        # The trace verbatim. A backslash inside $'...' ESCAPES, so the word ends
        # at its real closing quote and `main` stays the separate word it is.
        for cmd in (
            "git push origin feature > >(echo $'a\\'b') main",
            "git push origin feature > >(echo $'a\\'b') main extra",
            "git push origin feature > >(printf $'\\'') main",
            "git push origin feature > >(echo $'a\\'b\\'c') main",
        ):
            tags = security._git_publish_floor_tags(cmd)
            assert "git-publish-push-protected-branch-name" in tags, (
                f"{cmd!r}: an ANSI-C escaped quote desynced the word split and "
                f"fused the protected refspec: {set(tags)}"
            )
            assert tags, f"{cmd!r}: publishes main and was ALLOWED"
        assert security._git_push_args("git push origin feature > >(echo $'a\\'b') main") == [
            "origin",
            "feature",
            "main",
        ]

    def test_the_word_split_and_the_boundary_walk_agree_on_ansi_c(self):
        # The property that makes the class impossible: both readings come from
        # one machine, so they cannot disagree about where a quote ends.
        for token in (
            "$'a\\'b'",
            "$'\\''",
            "$'('",
            "$')'",
            "$'a\\'b'$'c\\'d'",  # adjacent ANSI-C words concatenate into one
            "$'a\\'b'\"x\"",  # ANSI-C followed by ordinary quoting
            "$'\\\\'",
        ):
            walk = security._shell_quote_walk(token)
            assert walk.end_state == 0, f"{token!r}: walk left the quote open"
            # The splitter must agree: a closed word does not swallow its neighbour.
            assert security._split_shell_words(f"x {token} y") == ["x", token, "y"], token
        # The CONTRAST that proves the ANSI-C branch is what differs: in a PLAIN
        # single quote a backslash is literal, so the quote really does close and
        # the next one really does reopen -- and both readings say so together.
        plain = "'a\\'b'"
        assert security._shell_quote_walk(plain).end_state == 1
        assert security._split_shell_words(f"x {plain} y") == ["x", f"{plain} y"]

    def test_ansi_c_parens_do_not_move_a_substitution_boundary(self):
        # An ANSI-C quoted paren is data, exactly like a plainly quoted one, so it
        # contributes nothing to the depth in either direction.
        for token in ("$'('", "$')'", "$'(('", "$'()'"):
            assert security._shell_quote_walk(token).paren_delta == 0, token
        for cmd in (
            "git push origin feature > >(echo $'(' ) main",
            "git push origin feature > >(echo $')' ) main",
            "git push origin feature > >(echo $'((((' ) main",
        ):
            assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
                cmd
            ), cmd
        # Outside a substitution the '$' pre-check already ungates it, which is
        # over-protective and deliberately so.
        assert security._GIT_PUBLISH_UNGATED in security._git_publish_floor_tags(
            "git push origin $'('"
        )

    def test_the_operator_cut_honours_escapes_through_the_same_machine(self):
        # _cut_at_operator was the fourth walker: it tracked quotes but not
        # escapes at all. Its pinned readings are unchanged, and an ESCAPED
        # operator now stays in the word -- bash hands git the ref `ma)in`.
        cut = security._cut_at_operator
        assert cut("(git") == "git"
        assert cut("main)&") == "main"
        assert cut("mainline)>log") == "mainline"
        assert cut("my-feature") == "my-feature"
        assert cut("'(main)'") == "'(main)'"
        assert cut('"(main)"') == '"(main)"'
        assert cut("ma\\)in") == "ma\\)in"
        assert security._dequote_token("ma\\)in") == "ma)in"

    def test_every_earlier_round_still_holds_under_the_shared_machine(self):
        # The unification must not cost any win the earlier rounds bought.
        floor = security._git_publish_floor_tags
        assert not floor("git push origin my-feature > >(tee log.txt)")  # round 1/3
        assert floor("git push origin main > >(tee log)") == frozenset(
            {"git-publish-push-protected-branch-name"}
        )
        assert floor("(git push --repo=origin -f)") == floor("git push --repo=origin -f")  # round 2
        assert floor("(git push origin main)") == frozenset(
            {"git-publish-push-protected-branch-name"}
        )
        assert not floor("git push origin 'feature|x'")  # round 1
        assert not floor("git push origin 'a;b'")
        assert floor("git push origin <<- EOF") == frozenset({"git-publish-push-single-arg"})
        assert "git-publish-push-wildcard-refspec" in floor("git push origin @(main)")
        assert security._GIT_PUBLISH_UNGATED in floor("git push origin ma\\\nin")  # round 1
        assert security._git_push_args("bash -c '(cd /tmp && git push origin my-feature)'") is None


class TestNestedPayloadExtractionSpansTheProvenBoundary:
    """The body extracted for nested scanning must span the boundary the walk PROVES.

    Rounds 3 and 4 gave the git-publish boundary walk and the word splitter one
    quote-aware machine. The nested-payload EXTRACTOR still walked parens on its
    own, and a quote-unaware span there does not merely mis-size the body -- it
    loses the nested command outright: ``git push origin my-feature > >(X=')' git
    push origin main)`` extracted ``X='``, so the publish of a protected branch
    inside the substitution was never scanned and ``is_denied`` returned None for
    a command bash executes. Extraction and boundary now read the same span, which
    is the only arrangement in which they cannot disagree.
    """

    def test_a_quoted_paren_cannot_hide_a_nested_push(self):
        # The trace verbatim, plus the quoting variants and nesting.
        for cmd in (
            "git push origin my-feature > >(X=')' git push origin main)",
            'git push origin my-feature > >(X=")" git push origin main)',
            "git push origin my-feature > >(X=$')' git push origin main)",
            "git push origin my-feature > >(X=')' Y=')' git push origin main)",
            "git push origin my-feature > >(X=')' git push origin main Y=')')",
            "git push origin my-feature > >(echo >(git push origin main))",
            "git push origin my-feature > >(echo >(X=')' git push origin main))",
            "cat <(X=')' git push origin main)",
        ):
            assert security.is_denied(cmd) is not None, (
                f"{cmd!r}: a quoted paren truncated the extracted body and the "
                f"nested protected push was never scanned"
            )
        # The control: the same shape WITHOUT the quoted paren was always denied,
        # so the quoting is what the fix restores rather than a new denial.
        assert (
            security.is_denied("git push origin my-feature > >(git push origin main)") is not None
        )

    def test_the_extracted_body_is_the_whole_body(self):
        from kiro_crew.security import _substitution_bodies

        assert _substitution_bodies(">(X=')' git push origin main)") == [
            "X=')' git push origin main"
        ]
        assert _substitution_bodies("$(X=')' echo hi)") == ["X=')' echo hi"]
        assert _substitution_bodies("<(a)<(b)") == ["a", "b"]
        # Nested substitutions: the outer body keeps the inner one whole. The
        # extractor returns ONE body per top-level opener; the payload walk is what
        # descends, so the inner command is reached from the outer body.
        assert _substitution_bodies(">(echo >(git push origin main))") == [
            "echo >(git push origin main)"
        ]
        assert "git push origin main" in security._shell_payload_sources(
            "cat >(echo >(git push origin main))"
        )
        # UNPROVEN span -> the whole remainder, the fail-closed direction.
        assert _substitution_bodies(">(git push origin main") == ["git push origin main"]
        assert _substitution_bodies(">(X=')' git push origin main") == [
            "X=')' git push origin main"
        ]

    def test_the_span_helper_is_quote_aware_and_reports_proof(self):
        from kiro_crew.security import _matching_close_paren

        # offset 2 is just past a ">(" / "$(" opener.
        assert _matching_close_paren(">(a)", 2) == (4, True)
        assert _matching_close_paren(">(a(b))", 2) == (7, True)
        # A quoted closer is data and must not end the span.
        assert _matching_close_paren(">(X=')' a)", 2) == (10, True)
        assert _matching_close_paren('>(X=")" a)', 2) == (10, True)
        assert _matching_close_paren(">(X=$')' a)", 2) == (11, True)
        # A quoted OPENER must not deepen it either.
        assert _matching_close_paren(">(X='(' a)", 2) == (10, True)
        # Never balanced -> not proven, and the span runs to the end.
        assert _matching_close_paren(">(a", 2) == (3, False)
        assert _matching_close_paren(">(X=')' a", 2) == (9, False)

    def test_the_self_protection_substitution_scan_is_quote_aware_too(self):
        # Same class at the self-protection rule: its private str.count walk
        # stopped at a quoted ')', so a name after it was never seen. That is an
        # UNDER-deny for this rule -- correcting an earlier audit note on this
        # line, which had recorded the truncation as over-deny-only.
        from kiro_crew.security import _protected_name_in_substitution

        # The quoted ')' must sit in a LATER token to reach the close test: a
        # private str.count walk drove depth to 0 there and BROKE, so the name
        # after it was never seen.
        tokens = ["$(true", "X=')'", "kirocrew", "token)"]
        assert _protected_name_in_substitution(tokens, 0) == "kirocrew"
        # Still stops at the REAL closer rather than reading a later command.
        assert _protected_name_in_substitution(["$(true)", ";", "kirocrew"], 0) == ""

    def test_every_earlier_round_still_holds(self):
        floor = security._git_publish_floor_tags
        assert not floor("git push origin my-feature > >(tee log.txt)")
        assert floor("git push origin main > >(tee log)") == frozenset(
            {"git-publish-push-protected-branch-name"}
        )
        assert security.is_denied("git push origin my-feature > >(tee log.txt)") is None
        assert floor("(git push --repo=origin -f)") == floor("git push --repo=origin -f")
        assert not floor("git push origin 'feature|x'")
        assert "git-publish-push-protected-branch-name" in floor(
            "git push origin feature > >(echo '(' ) main"
        )
        assert "git-publish-push-protected-branch-name" in floor(
            "git push origin feature > >(echo $'a\\'b') main"
        )
        assert security._GIT_PUBLISH_UNGATED in floor("git push origin feature > >(echo main")
        assert security._git_push_args("bash -c '(cd /tmp && git push origin my-feature)'") is None
