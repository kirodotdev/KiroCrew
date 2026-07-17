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

import json
import re
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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

    def test_meshclaw_only_branches_not_protected(self) -> None:
        """KiroCrew protects only git defaults; MeshClaw integration branch names
        are ordinary feature branches here and stay pushable."""
        assert _is_push_to_protected_branch(f"{PUSH} origin beta-braveheart") is False
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

    def test_protected_push_blocked_no_audit(self, captured_sel_events) -> None:
        assert is_denied(f"{PUSH} origin main").startswith("Blocked by security policy")
        assert is_denied(f"{PUSH} --force origin master").startswith("Blocked by security policy")
        assert not any(e.event_type == "push_allowed" for e in captured_sel_events)

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
        """is_denied must not backtrack exponentially on whitespace-laden flags."""

        def _timeout(*_):
            raise TimeoutError

        signal.signal(signal.SIGALRM, _timeout)
        signal.setitimer(signal.ITIMER_REAL, 3.0)
        try:
            t = time.perf_counter()
            is_denied("git " + ("\t-! " * 5000) + "x")
            elapsed = time.perf_counter() - t
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        assert elapsed < 1.0


class TestDefaultsJsonPushRegexes:
    """The kiro-cli regex layer (defaults.json) must mirror the Python gate
    WITHOUT over-blocking legit commands, and stay in sync with it."""

    @staticmethod
    def _push_regexes() -> "list[re.Pattern[str]]":
        cfg = json.loads((Path(security.__file__).parent / "config" / "defaults.json").read_text())
        pats = cfg["toolsSettings"]["execute_bash"]["deniedCommands"]
        return [re.compile(p) for p in pats if "push" in p and p.startswith(".*git")]

    def _blocked(self, cmd: str) -> bool:
        return any(rx.search(cmd) for rx in self._push_regexes())

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
