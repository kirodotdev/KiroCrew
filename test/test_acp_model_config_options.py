"""Harvest the configOptions model select when session/new omits `models`."""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO

SESSION_NEW_RESPONSE = json.loads(r"""{
 "sessionId": "92889b94-e33b-482e-b465-28a89a2b379f",
 "modes": {
  "currentModeId": "default",
  "availableModes": [
   {
    "id": "default",
    "name": "Manual",
    "description": "Always ask before making changes",
    "_meta": {
     "kind": "standard"
    }
   },
   {
    "id": "acceptEdits",
    "name": "Accept edits",
    "description": "Automatically accept all file edits",
    "_meta": {
     "kind": "standard"
    }
   },
   {
    "id": "plan",
    "name": "Plan",
    "description": "Create a plan before making changes",
    "_meta": {
     "kind": "plan"
    }
   },
   {
    "id": "auto",
    "name": "Auto",
    "description": "Claude handles permission decisions",
    "_meta": {
     "kind": "auto_review"
    }
   },
   {
    "id": "bypassPermissions",
    "name": "Bypass permissions",
    "description": "Accepts all permissions",
    "_meta": {
     "kind": "full_access"
    }
   }
  ]
 },
 "configOptions": [
  {
   "id": "mode",
   "name": "Mode",
   "description": "Session permission mode",
   "category": "mode",
   "type": "select",
   "currentValue": "default",
   "options": [
    {
     "value": "default",
     "name": "Manual",
     "description": "Always ask before making changes",
     "_meta": {
      "kind": "standard"
     }
    },
    {
     "value": "acceptEdits",
     "name": "Accept edits",
     "description": "Automatically accept all file edits",
     "_meta": {
      "kind": "standard"
     }
    },
    {
     "value": "plan",
     "name": "Plan",
     "description": "Create a plan before making changes",
     "_meta": {
      "kind": "plan"
     }
    },
    {
     "value": "auto",
     "name": "Auto",
     "description": "Claude handles permission decisions",
     "_meta": {
      "kind": "auto_review"
     }
    },
    {
     "value": "bypassPermissions",
     "name": "Bypass permissions",
     "description": "Accepts all permissions",
     "_meta": {
      "kind": "full_access"
     }
    }
   ]
  },
  {
   "id": "model",
   "name": "Model",
   "description": "AI model to use",
   "category": "model",
   "type": "select",
   "currentValue": "sonnet",
   "options": [
    {
     "value": "default",
     "name": "Default",
     "description": "Opus"
    },
    {
     "value": "sonnet",
     "name": "us.anthropic.claude-sonnet-5",
     "description": "Custom Sonnet model"
    },
    {
     "value": "us.anthropic.claude-fable-5-1",
     "name": "Fable",
     "description": "Fable 5.1 \u00b7 Most capable for your hardest and longest-running tasks"
    },
    {
     "value": "us.anthropic.claude-opus-4-1-20250805-v1:0",
     "name": "Opus 4.1",
     "description": "Opus 4.1 \u00b7 Legacy"
    },
    {
     "value": "us.anthropic.claude-opus-5",
     "name": "Opus",
     "description": "Opus 5 \u00b7 Best for everyday, complex tasks"
    },
    {
     "value": "us.anthropic.claude-opus-5[1m]",
     "name": "Opus (1M context)",
     "description": "Opus 5 for long sessions"
    },
    {
     "value": "us.anthropic.claude-opus-4-8",
     "name": "Opus 4.8",
     "description": "Opus 4.8 \u00b7 Previous Opus version"
    },
    {
     "value": "us.anthropic.claude-opus-4-8[1m]",
     "name": "Opus 4.8 (1M context)",
     "description": "Opus 4.8 for long sessions"
    },
    {
     "value": "us.anthropic.claude-opus-4-7",
     "name": "Opus 4.7",
     "description": "Opus 4.7 \u00b7 Legacy"
    },
    {
     "value": "us.anthropic.claude-opus-4-7[1m]",
     "name": "Opus 4.7 (1M context)",
     "description": "Opus 4.7 for long sessions"
    },
    {
     "value": "us.anthropic.claude-opus-4-6-v1",
     "name": "Opus 4.6",
     "description": "Opus 4.6 \u00b7 Legacy"
    },
    {
     "value": "us.anthropic.claude-opus-4-6-v1[1m]",
     "name": "Opus 4.6 (1M context)",
     "description": "Opus 4.6 for long sessions"
    },
    {
     "value": "haiku",
     "name": "Haiku",
     "description": "Haiku 4.5 \u00b7 Fastest for quick answers"
    }
   ]
  },
  {
   "id": "effort",
   "name": "Effort",
   "description": "Available effort levels for this model",
   "category": "thought_level",
   "type": "select",
   "currentValue": "xhigh",
   "options": [
    {
     "value": "default",
     "name": "Default"
    },
    {
     "value": "low",
     "name": "Low"
    },
    {
     "value": "medium",
     "name": "Medium"
    },
    {
     "value": "high",
     "name": "High"
    },
    {
     "value": "xhigh",
     "name": "Xhigh"
    },
    {
     "value": "max",
     "name": "Max"
    }
   ]
  }
 ]
}""")


def test_capture_harvests_config_options_when_models_field_is_absent(tmp_path: Path):
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)

    client._capture_available_models(SESSION_NEW_RESPONSE)

    available = client.available_models()
    assert len(available) == 13
    by_id = {m["modelId"]: m for m in available}
    assert by_id["us.anthropic.claude-opus-5[1m]"]["name"] == "Opus (1M context)"
    assert by_id["sonnet"]["name"] == "us.anthropic.claude-sonnet-5"
    assert client._resolved_model_id == "sonnet"


def test_capture_ignores_config_options_on_backends_without_the_capability(tmp_path: Path):
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_KIRO)

    client._capture_available_models(SESSION_NEW_RESPONSE)

    assert client.available_models() == []
    assert client._resolved_model_id is None


def test_cc_models_serves_harvested_catalog_with_verbatim_wire_ids(tmp_path: Path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers.agents import _cc_models
    from kiro_crew.providers.acp import AcpProvider

    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    client._capture_available_models(SESSION_NEW_RESPONSE)
    provider = MagicMock(spec=AcpProvider)
    provider.is_claude_backend = True
    provider.available_models.return_value = client.available_models()
    state = SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: [provider]))
    request = MagicMock()
    request.app.__getitem__.return_value = state

    names = [m["model_name"] for m in _cc_models(request)]

    assert names[0] == "auto"
    assert "us.anthropic.claude-opus-5[1m]" in names
    assert "us.anthropic.claude-fable-5-1" in names
    assert "haiku" in names
    assert "sonnet" in names


def test_cc_models_falls_back_to_registry_without_a_session(tmp_path: Path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers.agents import _cc_models

    state = SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: []))
    request = MagicMock()
    request.app.__getitem__.return_value = state

    names = [m["model_name"] for m in _cc_models(request)]

    assert names[0] == "auto"
    assert "us.anthropic.claude-opus-5" not in names
    assert "sonnet" not in names
    assert len(names) > 1
