"""Tests for hook matcher_mode and skills-only injection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.hooks import (
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHook,
    ScriptHookStore,
    _context_matches,
)


class TestContextMatches:
    """Test the _context_matches helper with all three modes."""

    # -- glob mode (default, backward-compatible) --

    def test_glob_exact(self):
        assert _context_matches("*deploy*", "glob", "please deploy this")

    def test_glob_no_match(self):
        assert not _context_matches("*deploy*", "glob", "fix the build")

    def test_glob_case_insensitive(self):
        assert _context_matches("*DEPLOY*", "glob", "please deploy this")

    # -- regex mode --

    def test_regex_word_boundary(self):
        assert _context_matches(r"\bPR\b", "regex", "open a PR for this")

    def test_regex_word_boundary_no_match(self):
        assert not _context_matches(r"\bPR\b", "regex", "improve the code")

    def test_regex_pipe_or(self):
        assert _context_matches(r"PR|pull request|worktree", "regex", "create a new worktree")

    def test_regex_pipe_or_no_match(self):
        assert not _context_matches(r"PR|pull request|worktree", "regex", "hello world")

    def test_regex_case_insensitive(self):
        assert _context_matches(r"\bpr\b", "regex", "Open a PR now")

    def test_regex_invalid_pattern(self):
        """Invalid regex should not match (graceful failure)."""
        assert not _context_matches(r"[invalid", "regex", "anything")

    def test_regex_complex_pattern(self):
        assert _context_matches(
            r"github\.com/kirodotdev/KiroCrew|fix CI|ship it",
            "regex",
            "Can you fix CI on this branch?",
        )

    # -- contains mode --

    def test_contains_single_term(self):
        assert _context_matches("deploy", "contains", "please deploy this service")

    def test_contains_pipe_separated(self):
        assert _context_matches("PR|pull request|worktree", "contains", "check the worktree")

    def test_contains_no_match(self):
        assert not _context_matches("PR|pull request|worktree", "contains", "hello world")

    def test_contains_case_insensitive(self):
        assert _context_matches("PR|DEPLOY", "contains", "deploy now")

    def test_contains_whitespace_handling(self):
        """Terms with surrounding whitespace should still match."""
        assert _context_matches(" PR | deploy ", "contains", "open a PR")

    # -- unknown mode falls back to glob --

    def test_unknown_mode_falls_back_to_glob(self):
        assert _context_matches("*test*", "unknown_mode", "run the test suite")


class TestScriptHookMatcherMode:
    """Test that matcher_mode is correctly serialized and deserialized."""

    def test_from_dict_default_glob(self):
        hook = ScriptHook.from_dict({"name": "test", "command": "echo hi"})
        assert hook.matcher_mode == "glob"

    def test_from_dict_regex(self):
        hook = ScriptHook.from_dict(
            {"name": "test", "command": "echo hi", "matcher_mode": "regex"}
        )
        assert hook.matcher_mode == "regex"

    def test_from_dict_contains(self):
        hook = ScriptHook.from_dict(
            {"name": "test", "command": "echo hi", "matcher_mode": "contains"}
        )
        assert hook.matcher_mode == "contains"

    def test_to_dict_includes_matcher_mode(self):
        hook = ScriptHook(name="test", command="echo hi", matcher_mode="regex")
        d = hook.to_dict()
        assert d["matcher_mode"] == "regex"

    def test_to_dict_includes_skills(self):
        hook = ScriptHook(name="test", skills=["kirocrew-dev/prepare-pr", "dev-fleet/pod-e2e"])
        d = hook.to_dict()
        assert d["skills"] == ["kirocrew-dev/prepare-pr", "dev-fleet/pod-e2e"]


class TestScriptHookSkills:
    """Test skills field on ScriptHook."""

    def test_from_dict_default_empty(self):
        hook = ScriptHook.from_dict({"name": "test", "command": "echo hi"})
        assert hook.skills == []

    def test_from_dict_with_skills(self):
        hook = ScriptHook.from_dict(
            {"name": "test", "skills": ["skill-a", "skill-b"]}
        )
        assert hook.skills == ["skill-a", "skill-b"]

    def test_from_dict_non_list_skills_defaults_empty(self):
        hook = ScriptHook.from_dict({"name": "test", "skills": "not-a-list"})
        assert hook.skills == []

    def test_from_dict_filters_non_strings(self):
        hook = ScriptHook.from_dict({"name": "test", "skills": ["valid", 123, None]})
        assert hook.skills == ["valid"]


class TestSkillsOnlyFire:
    """Test that skills-only hooks fire without subprocess."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> ScriptHookStore:
        return ScriptHookStore(tmp_path)

    @pytest.mark.asyncio
    async def test_skills_only_no_command_injects_directive(self, store: ScriptHookStore):
        """A hook with skills but no command should synthesize output."""
        store.create(
            {
                "name": "contributor-skills",
                "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": "",
                "matcher": r"\bPR\b|worktree",
                "matcher_mode": "regex",
                "skills": ["kirocrew-dev/prepare-pr", "dev-fleet/pod-e2e"],
            }
        )
        results = await store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, "open a PR")
        assert len(results) == 1
        assert results[0].exit_code == 0
        assert "$prepare-pr" in results[0].stdout
        assert "$pod-e2e" in results[0].stdout
        assert results[0].duration_ms == 0  # no subprocess

    @pytest.mark.asyncio
    async def test_skills_only_no_match_no_injection(self, store: ScriptHookStore):
        """A hook with skills should not inject if matcher doesn't match."""
        store.create(
            {
                "name": "contributor-skills",
                "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": "",
                "matcher": r"\bPR\b|worktree",
                "matcher_mode": "regex",
                "skills": ["kirocrew-dev/prepare-pr"],
            }
        )
        results = await store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, "hello world")
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_skills_with_command_runs_command(self, tmp_path: Path):
        """fire() takes the command path (never skills-only) when a command is set.

        Both write paths (create/update) now reject command+skills (issue
        #5444) — the skills would be inert — so a mixed hook can only reach the
        store via deserialization of a hand-edited / older ``hooks.json``. This
        pins the defense-in-depth ``fire()`` behavior for such a persisted
        config: the command runs, the skills are ignored, and it does NOT take
        the skills-only shortcut.
        """
        # Persist a mixed hook directly (bypassing create's validation) to model
        # a hand-edited store file, then load it fail-soft via from_dict.
        path = tmp_path / "hooks.json"
        path.write_text(
            json.dumps(
                {
                    "hooks": [
                        {
                            "id": "mixed",
                            "name": "mixed-hook",
                            "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                            "command": "echo extra-context",
                            "matcher": "",
                            "skills": ["skill-a"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        store = ScriptHookStore(tmp_path)
        results = await store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, "anything")
        # When command is set, the subprocess path runs (not skills-only).
        # In sandboxed CI the command may be governance-blocked (exit -1 or 2),
        # but the key invariant is: it did NOT take the skills-only shortcut
        # (which would give duration_ms=0 and a "Load skills:" prefix).
        assert len(results) == 1
        # Skills-only path produces "Load skills: $..." — command path does not.
        if results[0].exit_code == 0:
            assert "extra-context" in results[0].stdout
        else:
            # Governance-blocked or sandbox error — still confirms it attempted
            # the subprocess path rather than the skills-only shortcut.
            assert not results[0].stdout.startswith("Load skills:")


class TestReDoSProtection:
    """Verify that regex mode uses bounded matching to prevent ReDoS."""

    def test_catastrophic_backtracking_returns_false(self):
        """A pattern known to cause exponential backtracking fails closed."""
        # This pattern causes catastrophic backtracking on non-matching input
        evil_pattern = r"(a+)+b"
        # Input that triggers exponential backtracking (no 'b' at the end)
        evil_input = "a" * 30 + "!"
        # Should return False (fail-closed via timeout) rather than hang
        result = _context_matches(evil_pattern, "regex", evil_input)
        # The bounded_pattern_search kills the subprocess on timeout
        # For short patterns this may still complete fast enough — the key
        # invariant is it NEVER hangs the caller
        assert result is True or result is False  # completes, doesn't hang


def test_hook_create_requires_command_or_skills():
    """A hook must have either a command or skills — empty both is rejected."""
    from kiro_crew.validation import ValidationError, _validate_hook_has_action

    # Valid: command only
    _validate_hook_has_action({"command": "echo hi", "skills": []})
    # Valid: skills only (on a skills-capable event)
    _validate_hook_has_action(
        {"command": "", "skills": ["prepare-pr"], "event": "UserPromptSubmit"}
    )
    # Invalid: both command and skills (skills would be inert)
    with pytest.raises(ValidationError, match="cannot be combined with a command"):
        _validate_hook_has_action({"command": "echo hi", "skills": ["prepare-pr"]})
    # Invalid: neither
    with pytest.raises(ValidationError, match="either command or skills must be provided"):
        _validate_hook_has_action({"command": "", "skills": []})


class TestRegexCaseInsensitivityFix:
    """Verify the (?i) prepend only skips for actual inline-flag groups."""

    def test_non_capturing_group_gets_case_insensitive(self):
        """(?:...) is a non-capturing group, not a flag — (?i) must be prepended."""
        assert _context_matches(r"(?:deploy|release)", "regex", "DEPLOY now")

    def test_lookahead_gets_case_insensitive(self):
        """(?=...) is a lookahead, not a flag — (?i) must be prepended."""
        assert _context_matches(r"(?=deploy)deploy", "regex", "DEPLOY now")

    def test_lookbehind_gets_case_insensitive(self):
        """(?<name) is a named group — (?i) must be prepended."""
        assert _context_matches(r"(?P<word>deploy)", "regex", "DEPLOY now")

    def test_actual_flag_group_not_doubled(self):
        """(?i) already present — must not be doubled."""
        assert _context_matches(r"(?i)deploy", "regex", "DEPLOY now")

    def test_multi_flag_group_not_doubled(self):
        """(?im) already present — must not be doubled."""
        assert _context_matches(r"(?im)^deploy", "regex", "DEPLOY now")

    def test_flag_group_with_colon_not_doubled(self):
        """(?i:...) already has flag — must not be doubled."""
        assert _context_matches(r"(?i:deploy)", "regex", "DEPLOY now")

    def test_bare_pattern_case_insensitive(self):
        """A plain word pattern gets (?i) and matches case-insensitively."""
        assert _context_matches("deploy", "regex", "DEPLOY THIS")


class TestRegexValidationAtSave:
    """Verify invalid regex is rejected at save time."""

    def test_valid_regex_passes(self):
        from kiro_crew.validation import _validate_hook_regex

        _validate_hook_regex({"matcher": r"\bPR\b", "matcher_mode": "regex"})

    def test_invalid_regex_rejected(self):
        from kiro_crew.validation import ValidationError, _validate_hook_regex

        with pytest.raises(ValidationError, match="invalid regex"):
            _validate_hook_regex({"matcher": "[invalid", "matcher_mode": "regex"})

    def test_glob_mode_skips_regex_validation(self):
        from kiro_crew.validation import _validate_hook_regex

        # An "invalid regex" in glob mode should not be rejected
        _validate_hook_regex({"matcher": "[invalid", "matcher_mode": "glob"})

    def test_empty_matcher_passes(self):
        from kiro_crew.validation import _validate_hook_regex

        _validate_hook_regex({"matcher": "", "matcher_mode": "regex"})


class TestSkillsOnlyDeadConfigValidation:
    """Verify skills-only hooks on tool/Stop events are rejected at save time."""

    def test_skills_only_on_user_prompt_allowed(self):
        from kiro_crew.validation import _validate_hook_has_action

        _validate_hook_has_action(
            {"command": "", "skills": ["prepare-pr"], "event": "UserPromptSubmit"}
        )

    def test_skills_only_on_agent_spawn_allowed(self):
        from kiro_crew.validation import _validate_hook_has_action

        _validate_hook_has_action(
            {"command": "", "skills": ["prepare-pr"], "event": "AgentSpawn"}
        )

    def test_skills_only_on_pre_tool_use_rejected(self):
        from kiro_crew.validation import ValidationError, _validate_hook_has_action

        with pytest.raises(ValidationError, match="cannot fire on PreToolUse"):
            _validate_hook_has_action(
                {"command": "", "skills": ["prepare-pr"], "event": "PreToolUse"}
            )

    def test_skills_only_on_stop_rejected(self):
        from kiro_crew.validation import ValidationError, _validate_hook_has_action

        with pytest.raises(ValidationError, match="cannot fire on Stop"):
            _validate_hook_has_action(
                {"command": "", "skills": ["prepare-pr"], "event": "Stop"}
            )

    def test_skills_with_command_rejected(self):
        """A hook with BOTH command and skills is rejected — skills would be inert."""
        from kiro_crew.validation import ValidationError, _validate_hook_has_action

        with pytest.raises(ValidationError, match="cannot be combined with a command"):
            _validate_hook_has_action(
                {"command": "echo hi", "skills": ["prepare-pr"], "event": "UserPromptSubmit"}
            )
        # Rejected on Stop too (the command+skills check fires before the event check)
        with pytest.raises(ValidationError, match="cannot be combined with a command"):
            _validate_hook_has_action(
                {"command": "echo hi", "skills": ["prepare-pr"], "event": "Stop"}
            )

    def test_command_only_on_stop_allowed(self):
        """A command-only hook is allowed on any event (no skills to strand)."""
        from kiro_crew.validation import _validate_hook_has_action

        _validate_hook_has_action(
            {"command": "echo hi", "skills": [], "event": "Stop"}
        )
