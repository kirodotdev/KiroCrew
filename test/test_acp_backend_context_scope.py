"""Context injection: dialect concerns vs claude-specific branding.

A public-ACP-spec adapter reads no Kiro Crew agent config, so anything kiro-cli
would have loaded from the agent spec — steering resources, mapped skill globs —
must be injected into the prompt instead. That is a DIALECT question and applies
to every spec adapter.

The persona-prompt rewrite is not: it substitutes Claude's own product wording,
so it stays claude-only. These were one flag; conflating them would either
withhold skills from Codex or apply Claude branding to it.
"""

from __future__ import annotations

from kiro_crew.acp.types import (
    PROVIDER_LABEL_CLAUDE,
    PROVIDER_LABEL_CODEX,
    PROVIDER_LABEL_DEFAULT,
    PROVIDER_LABEL_KAS,
    SPEC_ADAPTER_PROVIDER_LABELS,
)
from kiro_crew.context import _skills_injection_plan


class TestSpecAdapterLabelSet:
    def test_both_adapters_are_members(self) -> None:
        assert PROVIDER_LABEL_CLAUDE in SPEC_ADAPTER_PROVIDER_LABELS
        assert PROVIDER_LABEL_CODEX in SPEC_ADAPTER_PROVIDER_LABELS

    def test_kiro_dialect_labels_are_not_members(self) -> None:
        """KAS speaks kiro's dialect and DOES load the agent config.

        Including it would double-inject skills it already has.
        """
        assert PROVIDER_LABEL_DEFAULT not in SPEC_ADAPTER_PROVIDER_LABELS
        assert PROVIDER_LABEL_KAS not in SPEC_ADAPTER_PROVIDER_LABELS


class TestSkillsInjectionPlan:
    def test_a_custom_agent_with_globs_injects_only_on_a_spec_adapter(self) -> None:
        """kiro-cli loads the agent's own skill globs; a spec adapter cannot.

        So the injection is what keeps a custom agent's skills reachable there.
        """
        inject_spec, globs_spec = _skills_injection_plan("some-agent", is_spec_adapter=True)
        inject_kiro, globs_kiro = _skills_injection_plan("some-agent", is_spec_adapter=False)
        # Same globs resolved either way; only the injection decision differs.
        assert globs_spec == globs_kiro
        if globs_spec:
            assert inject_spec is True
            assert inject_kiro is False

    def test_the_default_agent_injects_regardless_of_backend(self) -> None:
        """Kiro Crew's own agent has no spec-provided globs to defer to."""
        assert _skills_injection_plan(None, is_spec_adapter=False)[0] is True
        assert _skills_injection_plan(None, is_spec_adapter=True)[0] is True

    def test_the_parameter_is_named_for_the_dialect_not_the_vendor(self) -> None:
        """Guards the rename from coming undone.

        The old name (is_cc) invited the next reader to treat a dialect question
        as claude-specific, which is how Codex would silently lose its skills.
        """
        import inspect

        signature = inspect.signature(_skills_injection_plan)
        assert "is_spec_adapter" in signature.parameters
        assert "is_cc" not in signature.parameters


class TestBrandingStaysClaudeOnly:
    def test_build_message_keeps_a_separate_branding_flag(self) -> None:
        """The persona rewrite must not widen to every spec adapter.

        Rewriting kiro-cli branding to Claude's wording on a Codex session would
        tell the user they are talking to the wrong product.
        """
        import inspect

        from kiro_crew.context import ContextBuilder

        source = inspect.getsource(ContextBuilder.build_message)
        assert 'provider_type == "claude_code"' in source, "branding must stay claude-only"
        assert "is_spec_adapter_provider_label" in source, "skills must widen"
