"""Per-agent model resolution on the acp (kiro-cli) provider factory."""

from __future__ import annotations

import json
from unittest.mock import patch

from kiro_crew.config.loader import DEFAULT_MODEL, KiroCrewConfig


class TestAcpPerAgentModel:
    """A custom agent on the acp (kiro-cli) backend must run its own model.

    Regression: the acp factory previously passed model=None for custom agents,
    relying on kiro's session/set_mode to resolve the agent's model — but
    set_mode switches the prompt/tools, not the model, so the handshake skipped
    session/set_model and kiro fell back to its cli.json chat.defaultModel.
    These tests exercise the REAL factory (create_provider_factory) so the model
    actually threaded into AcpProvider is asserted end to end.
    """

    @staticmethod
    def _acp_cfg():
        # KiroCrew is KiroACP-only, so the factory is always acp; a plain
        # instance keeps construction side-effect-free (AcpProvider.__init__
        # builds an AcpClient without spawning kiro-cli).
        return KiroCrewConfig()

    def test_custom_agent_threads_its_declared_model(self):
        cfg = self._acp_cfg()
        with patch.object(
            KiroCrewConfig, "_resolve_named_agent_model", return_value="gpt-5.6-sol"
        ):
            provider = cfg.create_provider_factory()(
                session_key="cron:job:run", agent="smle-triage-canary"
            )
        # The declared model must reach the client verbatim (not the global
        # default, and not the DEFAULT_MODEL "auto" sentinel).
        assert provider.client._model == "gpt-5.6-sol"
        assert provider.client._is_claude is False

    def test_model_override_wins_over_agent_model(self):
        cfg = self._acp_cfg()
        with patch.object(
            KiroCrewConfig, "_resolve_named_agent_model", return_value="gpt-5.6-sol"
        ):
            provider = cfg.create_provider_factory()(
                session_key="cron:job:run",
                agent="smle-triage-canary",
                model_override="claude-sonnet-4.6",
            )
        assert provider.client._model == "claude-sonnet-4.6"

    def test_unresolved_agent_model_falls_back_to_default(self):
        # When the agent declares no model, _resolve_named_agent_model returns "";
        # AcpClient normalizes "" to DEFAULT_MODEL (same as the old None path).
        cfg = self._acp_cfg()
        with patch.object(KiroCrewConfig, "_resolve_named_agent_model", return_value=""):
            provider = cfg.create_provider_factory()(
                session_key="cron:job:run", agent="no-model-agent"
            )
        assert provider.client._model == DEFAULT_MODEL

    def test_kiro_agent_ignores_cc_model_sidecar(self, tmp_path):
        # The acp factory calls _resolve_named_agent_model, which returns the
        # kiro `model` slot and ignores a cc_model sidecar — so a kiro agent that
        # also carries a cc_model still runs its kiro model. Asserted on the real
        # resolver via its agents_dir seam, locking the resolver's contract.
        (tmp_path / "kiro-cc.json").write_text(
            json.dumps({"name": "kiro-cc", "model": "gpt-5.6-sol",
                        "cc_model": "claude-opus-4.6"})
        )
        assert KiroCrewConfig._resolve_named_agent_model(
            "kiro-cc", agents_dir=tmp_path
        ) == "gpt-5.6-sol"
