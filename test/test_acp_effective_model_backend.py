"""The cold-start model id is translated for the backend that will run it.

``acp_effective_model`` is the factory's own selection. It used to hardcode
``to_acp_id``, so a concrete ``agent.model`` reached the claude adapter in
kiro's namespace — rejected by ``set_config_option``, and withheld by nothing,
because the pre-wire availability guard is deliberately kiro-only (the two
backends advertise in different namespaces). The warm-pool switch path
(``session_allocation``) already keyed the translation on the backend, so the
same pinned model behaved differently depending on whether a pooled process
happened to exist. These pin that the two paths now agree.
"""

from __future__ import annotations

from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.config.loader import AgentConfig, KiroCrewConfig

# A canonical registry key whose kiro id and claude provider id differ — the
# whole point of the fix. Kept as data so the assertions below read as "which
# namespace did it land in", not as a second copy of the registry.
PINNED = "opus-4.8-1m"
KIRO_ID = "claude-opus-4.8"


def _cfg(backend: str, model: str) -> KiroCrewConfig:
    """A config resolved without reading the installed agents dir.

    ``model`` is written straight onto ``AgentConfig`` so the global is already
    collapsed — ``_resolve_agent_model`` (which reads
    ``~/.kiro/agents/kirocrew.json``) is only consulted for the ``auto``
    sentinel, and the auto case below wants exactly that skipped.
    """
    return KiroCrewConfig(agent=AgentConfig(acp_backend=backend, model=model))


def test_claude_backend_gets_a_claude_provider_id() -> None:
    """A canonical key resolves into the claude namespace, not kiro's."""
    resolved = _cfg(ACP_BACKEND_CLAUDE, PINNED).acp_effective_model(None, None)
    assert resolved != KIRO_ID, "kiro-namespaced id leaked to the claude backend"
    assert "anthropic" in resolved, f"expected a claude provider id, got {resolved!r}"


def test_kiro_backend_keeps_the_kiro_id() -> None:
    """The kiro path is unchanged — the fix must not retranslate it."""
    assert _cfg(ACP_BACKEND_KIRO, PINNED).acp_effective_model(None, None) == KIRO_ID


def test_kas_backend_keeps_the_kiro_id() -> None:
    """kas is served through the same ids as kiro, so it takes the same branch."""
    assert _cfg(ACP_BACKEND_KAS, PINNED).acp_effective_model(None, None) == KIRO_ID


def test_auto_pins_nothing_on_the_claude_backend() -> None:
    """The default config is the path that already worked — keep it sending nothing.

    ``auto`` collapses to ``""`` in both namespaces and the client skips the
    model send for ``""``. This is why enabling claude via config.json works
    today, and why the defect above was never the mainline path.
    """
    assert _cfg(ACP_BACKEND_CLAUDE, "").acp_effective_model(None, None) == ""


def test_an_explicit_override_is_also_translated_for_claude() -> None:
    """``model_override`` wins the precedence chain and must not skip translation."""
    resolved = _cfg(ACP_BACKEND_CLAUDE, "").acp_effective_model(None, PINNED)
    assert resolved != KIRO_ID
    assert "anthropic" in resolved
