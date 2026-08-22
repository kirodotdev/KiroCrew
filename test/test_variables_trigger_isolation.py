"""A variable's value must not select skills.

The explicit `$skill` resolver was already guarded. This covers the OTHER selection
mechanism: `build_message` runs trigger-WORD matching, and expansion happens upstream
of it, so without the pre-expansion hand-off a value holding an ordinary word (a URL
path, a queue name) pulls in an unrelated skill BODY the user never referenced.
"""

from __future__ import annotations

import inspect

# Reuse the expansion suite's fixtures rather than rebuilding a ContextBuilder and a
# config here: the point of this module is the trigger-isolation property, and a
# second, subtly different harness for the same objects is how the two drift apart.
from test_variables_expansion import (  # noqa: E402
    _builder,
    _config,
    _patch_config,
    _write_builtin_prompt,
)

from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard import chat_runner as runner_mod
from kiro_crew.messaging import dispatch as dispatch_mod


class TestTriggerMatchingUsesPreExpansionText:
    def test_build_message_accepts_the_pre_expansion_text(self):
        params = inspect.signature(ContextBuilder.build_message).parameters
        assert "trigger_text" in params
        assert params["trigger_text"].default is None, "must default to today's behaviour"

    def test_the_trigger_call_reads_the_caller_supplied_text(self):
        source = inspect.getsource(ContextBuilder.build_message)
        assert "trigger_source = text if trigger_text is None else trigger_text" in source
        # Anchored on the ARGUMENT, not the whole call: main added a `project_dir=`
        # keyword to get_triggered_skills during a rebase, and a guard pinned to the
        # full call form fails on an unrelated addition while telling us nothing
        # about the property it is supposed to protect.
        assert "get_triggered_skills(trigger_source" in source
        assert "get_triggered_skills(text" not in source

    def test_an_absent_trigger_text_falls_back_to_the_message(self, tmp_path, monkeypatch):
        """Nine of eleven callers pass nothing; their behaviour must not change.

        Drives the real build_message and records what the skills loader was asked
        to match. The earlier version of this test evaluated a Python ternary
        written inline in its own body, so it asserted a property of the test file
        rather than of the product and passed with the feature fully reverted.
        """
        seen = self._capture_trigger(tmp_path, monkeypatch, text="do the thing")
        assert seen == ["do the thing"]

    def test_an_explicit_trigger_text_is_what_gets_matched(self, tmp_path, monkeypatch):
        """The property the PR claims: a variable VALUE cannot select a skill."""
        seen = self._capture_trigger(
            tmp_path,
            monkeypatch,
            text="please run expanded-secret-value now",
            trigger_text="please run {{token}} now",
        )
        assert seen == ["please run {{token}} now"]
        assert "expanded-secret-value" not in seen[0]

    def test_an_empty_trigger_text_is_honoured_not_treated_as_absent(self, tmp_path, monkeypatch):
        """`is None` rather than a truthiness check: a caller whose user text is
        empty (an @prompt with no trailing words) means "match nothing", and
        falling back to the expanded text there would reintroduce the defect."""
        seen = self._capture_trigger(tmp_path, monkeypatch, text="expanded value", trigger_text="")
        assert seen == [""], "an empty trigger text was treated as absent"

    @staticmethod
    def _capture_trigger(tmp_path, monkeypatch, *, text: str, trigger_text=None) -> list[str]:
        """Run build_message and return every string handed to trigger matching."""
        cfg = _config(crew="crew1", global_vars={"token": "expanded-secret-value"})
        _patch_config(monkeypatch, cfg)
        _write_builtin_prompt(monkeypatch, tmp_path, "prompt body")
        builder = _builder(tmp_path)

        seen: list[str] = []
        original = builder.skills.get_triggered_skills

        def _record(arg, *a, **kw):
            seen.append(arg)
            return original(arg, *a, **kw)

        monkeypatch.setattr(builder.skills, "get_triggered_skills", _record)
        kwargs = {} if trigger_text is None else {"trigger_text": trigger_text}
        builder.build_message(text, False, "sess-1", **kwargs)
        return seen


class TestBothExpandingCallersHandOverTheRawText:
    def test_the_dashboard_captures_the_text_as_typed(self):
        source = inspect.getsource(runner_mod)
        # Prefix anchor: the assignment now takes an override for the auto-nudge
        # case, so pinning the full right-hand side breaks on that addition without
        # testing the ordering this guard is for.
        assert "pre_expansion_message = " in source
        capture_at = source.index("pre_expansion_message = ")
        # Anchored on the IDENTIFIER, not the call syntax. This site has been
        # rewritten twice — it gained conjuncts, then moved behind
        # asyncio.to_thread, which passes the helper by name with no paren.
        expand_at = source.index("_expand_message_variables, message")
        assert capture_at < expand_at, "the capture must precede every rewriting stage"

    def test_an_already_expanded_body_can_override_the_capture(self):
        """An auto-nudge body arrives ALREADY expanded, rendered upstream with the
        loop's armed crew, so `message` is not the pre-expansion text on that path.
        The caller supplies the loop's own instruction instead; without the
        override, trigger matching would read resolved values."""
        source = inspect.getsource(runner_mod)
        assert "trigger_text if trigger_text is not None else message" in source
        params = inspect.signature(runner_mod._run_chat).parameters
        assert params["trigger_text"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["trigger_text"].default is None

    def test_the_dashboard_passes_it_to_build_message(self):
        source = inspect.getsource(runner_mod)
        assert "trigger_text=pre_expansion_message" in source

    def test_the_channel_path_needs_no_handover_because_it_does_not_expand(self):
        """The channel path used to expand and therefore had to hand over the raw
        text. Inbound expansion was removed (a participant could otherwise read
        operator config), so there is nothing to hand over — and no `trigger_text`
        is the CORRECT state here, since the text reaching build_message is already
        exactly what the participant sent."""
        source = inspect.getsource(dispatch_mod)
        assert "expand_variables(" not in source, "the channel path expands again"
        build_at = source.index("ctx_builder.build_message,")
        call = source[build_at : build_at + 200]
        assert "turn.user_text," in call
        assert (
            "trigger_text=" not in call
        ), "a trigger_text here would imply the text was rewritten, which it is not"

    def test_the_capture_precedes_the_prompt_and_skill_stages(self):
        """@prompt replaces the message and $skill appends to it, so a capture
        taken after either would already be polluted. Anchored on the CALL forms:
        both helpers are defined earlier in the module than the pipeline uses them,
        so searching for the bare name finds the definition instead.

        The capture anchor is the assignment's PREFIX, not the whole expression: it
        now carries an override for the auto-nudge case (whose body arrives already
        expanded), and pinning the full right-hand side made this guard fail on that
        addition while saying nothing about the ordering it exists to protect.
        """
        source = inspect.getsource(runner_mod)
        capture_at = source.index("pre_expansion_message = ")
        prompt_at = source.index("message, prompt_blocks, _status = _resolve_prompt_mention(")
        skills_at = source.index("message, skill_blocks, _n_skills = _resolve_dollar_skills(")
        assert capture_at < prompt_at
        assert capture_at < skills_at


class TestTriggerSelectionIsReachableAtAll:
    def test_the_skill_loader_exposes_the_trigger_entry_point(self):
        """Positive control: if this method were renamed, the tests above would
        pass against a mechanism that no longer exists."""
        from kiro_crew.skills import SkillsLoader

        assert callable(getattr(SkillsLoader, "get_triggered_skills", None))

    def test_a_value_holding_a_common_word_would_have_matched(self):
        """Shows the defect this ordering prevents, without asserting on the real
        skills tree: the expanded text contains a word the raw text does not."""
        from kiro_crew.variables import expand

        raw = "hit {{baseUrl}} and report"
        expanded, _ = expand(raw, {"baseUrl": "https://api.example.com/deploy"})
        assert "deploy" in expanded
        assert "deploy" not in raw


def test_build_message_callers_that_do_not_expand_are_unchanged():
    """A caller that never expands must not be forced to pass anything."""
    sig = inspect.signature(ContextBuilder.build_message)
    required = [
        name
        for name, p in sig.parameters.items()
        if p.default is inspect.Parameter.empty and name != "self"
    ]
    assert "trigger_text" not in required
