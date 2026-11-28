"""The dashboard keys the lane's form and row actions by the ids the backend stamps."""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.managed_env import PROVISIONER_ID, PROVISIONER_KIND

_DIR = Path(__file__).resolve().parents[1] / "website/src/components/managedEnv"


def _const(file: str, name: str) -> list[str]:
    text = (_DIR / file).read_text(encoding="utf-8")
    return re.findall(rf"export const {name} = '([^']*)'", text)


def test_the_dashboard_uses_the_backends_provisioner_id_and_kind():
    """A drift draws nothing, silently: the seams match these verbatim."""
    assert _const("SandboxCrewRowActions.tsx", "SANDBOX_PROVISIONER_ID") == [PROVISIONER_ID]
    assert _const("SandboxLaunchForm.tsx", "SANDBOX_PROVISIONER_KIND") == [PROVISIONER_KIND]


def test_the_forms_crew_name_rule_is_the_engines():
    """The form holds Launch on this rule and the engine refuses on its own; one rule."""
    from kiro_crew.managed_env.engine import _CREW_RE

    text = (_DIR / "SandboxLaunchForm.tsx").read_text(encoding="utf-8")
    assert re.findall(r"const CREW_NAME = /\^(.*)\$/", text) == [_CREW_RE.pattern]
    assert "!name.includes('--')" in text
