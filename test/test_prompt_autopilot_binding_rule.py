"""The orchestrator system prompt must bind the user-facing name "Autopilot".

The mode is presented to users as **Autopilot** (badge, WelcomeView, nav), but
the system prompt otherwise speaks in terms of "orchestrator"/"plan"/"stages"
and the internal slot-mode value stays ``"orchestrator"`` for back-compat. If
the prompt never states that "Autopilot" *is* this mode, the model has no
binding for user references like "autopilot plan" / "autopilot this" and can
fail to recognize them even while genuinely running in orchestrator mode.

That name-binding line reads like ordinary prose and is exactly the kind of line
a prompt rewrite drops -- silently reintroducing the misunderstanding -- so it is
pinned by a test.
"""

from __future__ import annotations

from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "config"


def test_orchestrator_prompt_binds_autopilot_name() -> None:
    text = (CONFIG_DIR / "prompt-orchestrator.md").read_text(encoding="utf-8")
    assert "This is Autopilot" in text
    assert "autopilot plan" in text.lower()
