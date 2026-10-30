"""Dashboard chat: what a prompt goes through before dispatch — blocked slash commands, pack consent, $skills."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.state import _ChatSlot

# ── Slash command tests ──


class TestBlockedSlashCommands:
    """Tests for _BLOCKED_SLASH_COMMANDS blocking dangerous commands."""

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("/quit", id="quit_is_blocked"),
            pytest.param("/exit", id="exit_is_blocked"),
            pytest.param("/q", id="q_is_blocked"),
            pytest.param("/editor", id="editor_is_blocked"),
            pytest.param("/chat", id="chat_is_blocked"),
            pytest.param("/paste", id="paste_is_blocked"),
            pytest.param("/reply", id="reply_is_blocked"),
            # kiro-cli's terminal checkpoint toggle (Ctrl+T); the dashboard's
            # session model and /side cover its purpose, so it is inert here.
            pytest.param("/tangent", id="tangent_is_blocked"),
        ],
    )
    def test_is_blocked(self, command: str):
        from kiro_crew.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert command in _BLOCKED_SLASH_COMMANDS

    def test_compact_is_not_blocked(self):
        from kiro_crew.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/compact" not in _BLOCKED_SLASH_COMMANDS

    def test_blocked_is_subset_of_slash(self):
        from kiro_crew.dashboard.chat import _BLOCKED_SLASH_COMMANDS, _SLASH_COMMANDS

        assert _BLOCKED_SLASH_COMMANDS.issubset(_SLASH_COMMANDS)

    @pytest.mark.asyncio
    async def test_blocked_command_returns_warning_no_session(self, tmp_path, monkeypatch):
        """Posting /quit should add warning to slot and never acquire a session."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/quit")

        # Should have the warning message
        texts = [m["content"] for m in slot.messages if m.get("role") == "assistant"]
        assert any("not available in the dashboard" in t for t in texts)
        # Should never have called get_or_create (no session acquired)
        state.sessions.get_or_create.assert_not_called()


# ── Persona injection: installed pack consent ──


class TestInstalledPackConsentInjection:
    """Content-bound (sha256) consent gate for INSTALLED pack personas
    (``custom-<slug>``). Injection requires the caller's ``theme_consent_sha``
    to equal sha256 of the persona text actually read from disk; anything else
    fails closed. Guards the Codex HIGH reinstall-swap fix."""

    PERSONA = "Speak like a friendly installed-pack host."

    @staticmethod
    def _sha(text: str) -> str:
        import hashlib

        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def test_matching_sha_injects(self):
        from kiro_crew.dashboard.chat import _maybe_inject_persona

        with patch(
            "kiro_crew.dashboard.chat_utils._installed_theme_persona",
            return_value=self.PERSONA,
        ):
            result = _maybe_inject_persona(
                "hello",
                "custom-mypack",
                True,
                theme_consent_sha=self._sha(self.PERSONA),
            )
        assert "[THEME PERSONA]" in result
        assert self.PERSONA in result

    def test_stale_sha_not_injected(self):
        # Reinstall rewrote persona.md; the stored hash does not match the
        # on-disk text -> the new, never-consented persona must NOT be injected.
        from kiro_crew.dashboard.chat import _maybe_inject_persona

        with patch(
            "kiro_crew.dashboard.chat_utils._installed_theme_persona",
            return_value=self.PERSONA,
        ):
            result = _maybe_inject_persona(
                "hello",
                "custom-mypack",
                True,
                theme_consent_sha=self._sha("OLD PERSONA THE USER CONSENTED TO"),
            )
        assert result == "hello"

    def test_absent_sha_not_injected(self):
        from kiro_crew.dashboard.chat import _maybe_inject_persona

        with patch(
            "kiro_crew.dashboard.chat_utils._installed_theme_persona",
            return_value=self.PERSONA,
        ):
            result = _maybe_inject_persona("hello", "custom-mypack", True)
        assert result == "hello"

    def test_legacy_boolean_alone_not_injected(self):
        # The legacy boolean consent field grants nothing on its own: with no
        # content-bound sha, an installed pack persona is never injected even
        # though the pack ships one.
        from kiro_crew.dashboard.chat import _maybe_inject_persona

        with patch(
            "kiro_crew.dashboard.chat_utils._installed_theme_persona",
            return_value=self.PERSONA,
        ):
            result = _maybe_inject_persona(
                "hello",
                "custom-mypack",
                True,
                theme_consent_sha=None,
            )
        assert result == "hello"

    def test_not_injected_on_followup(self):
        from kiro_crew.dashboard.chat import _maybe_inject_persona

        with patch(
            "kiro_crew.dashboard.chat_utils._installed_theme_persona",
            return_value=self.PERSONA,
        ):
            result = _maybe_inject_persona(
                "hello",
                "custom-mypack",
                False,
                theme_consent_sha=self._sha(self.PERSONA),
            )
        assert result == "hello"

    def test_malformed_sha_no_crash_no_injection(self):
        # GPT HIGH: a non-ASCII (or otherwise malformed) theme_consent_sha
        # must never reach hmac.compare_digest (which raises TypeError on
        # non-ASCII str, aborting the whole chat turn). The compare-site guard
        # treats anything that is not exactly 64 lowercase-hex as ABSENT:
        # no exception AND no injection.
        from kiro_crew.dashboard.chat import _maybe_inject_persona

        valid = self._sha(self.PERSONA)
        malformed = [
            "é",  # non-ASCII -> would TypeError in compare_digest
            valid.upper(),  # uppercase hex (raw, un-normalized) -> not 64-lower
            valid[:-1],  # 63 chars
            valid + "a",  # 65 chars
            "",  # empty
            "  ",  # whitespace only
            12345,  # non-str
            None,  # absent
            valid[:-2] + "gg",  # non-hex chars
        ]
        with patch(
            "kiro_crew.dashboard.chat_utils._installed_theme_persona",
            return_value=self.PERSONA,
        ):
            for bad in malformed:
                # The call must not raise for any malformed input...
                result = _maybe_inject_persona(
                    "hello",
                    "custom-mypack",
                    True,
                    theme_consent_sha=bad,
                )
                # ...and must not inject the persona.
                assert result == "hello", f"unexpected injection for {bad!r}"

    def test_normalizer_fail_closed_and_salvage(self):
        # The parse-site normalizer (validation.normalize_theme_consent_sha)
        # rejects malformed values (-> None, fail closed) and salvages a valid
        # sha wrapped in surrounding whitespace / uppercase (strip + lower).
        from kiro_crew.validation import normalize_theme_consent_sha

        valid = self._sha(self.PERSONA)
        assert normalize_theme_consent_sha("é") is None
        assert normalize_theme_consent_sha(valid[:-1]) is None
        assert normalize_theme_consent_sha("") is None
        assert normalize_theme_consent_sha(12345) is None
        assert normalize_theme_consent_sha(None) is None
        assert normalize_theme_consent_sha(valid) == valid
        # salvage: leading/trailing whitespace + uppercase normalize to canonical
        assert normalize_theme_consent_sha("  " + valid.upper() + "\n") == valid
        # a normalized value then injects through the real gate
        from kiro_crew.dashboard.chat import _maybe_inject_persona

        with patch(
            "kiro_crew.dashboard.chat_utils._installed_theme_persona",
            return_value=self.PERSONA,
        ):
            norm = normalize_theme_consent_sha("  " + valid.upper() + "\n")
            result = _maybe_inject_persona(
                "hello",
                "custom-mypack",
                True,
                theme_consent_sha=norm,
            )
        assert "[THEME PERSONA]" in result


class TestExpandDollarSkills:
    """Runner-side ``_expand_dollar_skills``: redaction, chip,
    SEL audit, and empty/exception branches. The pure resolution logic is
    covered separately by test_skills.TestResolveDollarSkills; here we
    exercise the chat_runner wrapper that adds runner concerns."""

    def _make_skill(self, skills_dir: Path, name: str, body: str) -> None:
        d = skills_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")

    def _state_with_skills(self, tmp_path, monkeypatch, *skills):
        """A DashboardState whose _get_skills() returns a hermetic loader
        pointed at *skills* (each a ``(name, body)`` pair)."""
        # Keep edition-contributed skill roots out of the hermetic loader.
        from kiro_crew.platform.defaults import DefaultMcpToolingProvider
        from kiro_crew.skills import SkillsLoader

        monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: [])
        skills_dir = tmp_path / "skills"
        for name, body in skills:
            self._make_skill(skills_dir, name, body)
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)

        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state._standalone_skills = loader  # _get_skills returns this verbatim
        return state

    def test_no_dollar_short_circuits(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _expand_dollar_skills

        state = self._state_with_skills(tmp_path, monkeypatch)
        slot = _ChatSlot("s1")
        out, n = _expand_dollar_skills("plain message", state, slot, "sess", slot.project or "")
        assert out == "plain message"
        assert n == 0
        state.push_slots_update.assert_not_called()

    def test_resolves_appends_block_and_chip(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _expand_dollar_skills

        state = self._state_with_skills(
            tmp_path,
            monkeypatch,
            ("oncall-handover", "---\nname: oncall-handover\n---\n# Handover\nBODY-A"),
        )
        slot = _ChatSlot("s1")
        out, n = _expand_dollar_skills(
            "please $oncall-handover now", state, slot, "sess", slot.project or ""
        )
        assert n == 1
        # original message preserved, skill body appended as a [Skill: name] block
        assert out.startswith("please $oncall-handover now")
        assert "[Skill: oncall-handover]" in out
        assert "BODY-A" in out
        # a system chip is appended and the UI is poked
        notice = next(
            m
            for m in slot.messages
            if "Loaded skill(s)" in m.get("content", "") and m.get("role") == "system"
        )
        assert notice["meta"]["kind"] == "skill_load"
        assert notice["meta"]["skills"] == [
            {"name": "oncall-handover", "body": "# Handover\nBODY-A"}
        ]
        state.push_slots_update.assert_called_once()

    def test_skill_notice_preserves_multiple_resolved_bodies_in_order(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _expand_dollar_skills

        state = self._state_with_skills(
            tmp_path,
            monkeypatch,
            ("first", "---\nname: first\n---\n# First\nONE"),
            ("second", "---\nname: second\n---\n# Second\nTWO"),
        )
        slot = _ChatSlot("s1")

        out, n = _expand_dollar_skills(
            "use $first then $second", state, slot, "sess", slot.project or ""
        )

        assert n == 2
        assert out.index("[Skill: first]") < out.index("[Skill: second]")
        notice = next(m for m in slot.messages if m.get("role") == "system")
        assert notice["meta"] == {
            "kind": "skill_load",
            "skills": [
                {"name": "first", "body": "# First\nONE"},
                {"name": "second", "body": "# Second\nTWO"},
            ],
            "mid": notice["meta"]["mid"],
        }

    def test_unresolved_token_no_chip(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import chat_runner

        state = self._state_with_skills(
            tmp_path,
            monkeypatch,
            ("oncall-handover", "---\nname: oncall-handover\n---\nBODY"),
        )
        slot = _ChatSlot("s1")
        sel_mock = MagicMock()
        monkeypatch.setattr(chat_runner, "sel", lambda: sel_mock)

        out, n = chat_runner._expand_dollar_skills(
            "$does-not-exist hi", state, slot, "sess", slot.project or ""
        )
        assert (out, n) == ("$does-not-exist hi", 0)
        state.push_slots_update.assert_not_called()
        # a real $skill-shaped token that didn't resolve IS audited as not_found
        assert sel_mock.log_tool_invocation.called
        _, kwargs = sel_mock.log_tool_invocation.call_args
        assert kwargs.get("outcome") == "not_found"
        assert kwargs.get("tool_name") == "skill_dollar_expansion"

    def test_incidental_dollar_not_audited(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import chat_runner

        state = self._state_with_skills(
            tmp_path,
            monkeypatch,
            ("oncall-handover", "---\nname: oncall-handover\n---\nBODY"),
        )
        slot = _ChatSlot("s1")
        sel_mock = MagicMock()
        monkeypatch.setattr(chat_runner, "sel", lambda: sel_mock)

        # $5 / $PATH / bare $ are NOT skill-shaped → no not_found noise.
        out, n = chat_runner._expand_dollar_skills(
            "it costs $5 not $PATH", state, slot, "sess", slot.project or ""
        )
        assert (out, n) == ("it costs $5 not $PATH", 0)
        sel_mock.log_tool_invocation.assert_not_called()

    def test_credentials_redacted_in_loaded_body(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _expand_dollar_skills

        secret = "AKIAIOSFODNN7EXAMPLE"
        state = self._state_with_skills(
            tmp_path,
            monkeypatch,
            ("leaky", f"---\nname: leaky\n---\n# Leaky\naws_key={secret}"),
        )
        slot = _ChatSlot("s1")
        out, n = _expand_dollar_skills("use $leaky", state, slot, "sess", slot.project or "")
        assert n == 1
        # the raw credential must not survive into the expanded message
        assert secret not in out

    def test_resolution_exception_audited_and_swallowed(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import chat_runner

        state = self._state_with_skills(tmp_path, monkeypatch)
        slot = _ChatSlot("s1")

        boom = MagicMock()
        boom.resolve_dollar_skills.side_effect = RuntimeError("kaboom")
        # Force _get_skills(state) to return the exploding loader.
        monkeypatch.setattr(chat_runner, "_get_skills", lambda _s: boom)

        sel_mock = MagicMock()
        monkeypatch.setattr(chat_runner, "sel", lambda: sel_mock)

        out, n = chat_runner._expand_dollar_skills(
            "try $whatever", state, slot, "sess", slot.project or ""
        )
        # message returned unchanged, no crash
        assert (out, n) == ("try $whatever", 0)
        # the failure was audited via SEL with outcome="error"
        assert sel_mock.log_tool_invocation.called
        _, kwargs = sel_mock.log_tool_invocation.call_args
        assert kwargs.get("outcome") == "error"
        assert kwargs.get("tool_name") == "skill_dollar_expansion"
