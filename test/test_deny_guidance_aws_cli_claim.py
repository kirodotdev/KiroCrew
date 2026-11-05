"""The "AWS CLI calls are not blocked" promise is scoped to the rule that refused.

The refusal's remediation, the blocked-by-policy skill and the user doc all tell
the agent that the credential-file block leaves `aws` alone. That holds for the
rule that fired and nothing wider: the builtin catalog itself refuses some `aws`
calls (an upload, a `delete-*`), and an enterprise overlay can deny `aws`
outright, so an unscoped promise is false on a real host and walks the agent into
the second wall :mod:`kiro_crew.deny_guidance` calls worse than silence.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew import deny_guidance as dg

_ROOT = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

#: "AWS CLI calls/commands ... not blocked" with no rule named after it.
_UNSCOPED = re.compile(r"aws cli (?:calls|commands)[^.]*not blocked(?! by th)", re.IGNORECASE)


def test_every_surface_scopes_the_aws_cli_promise_to_the_rule():
    skill = _ROOT / "builtin_skills" / "blocked-by-policy" / "SKILL.md"
    surfaces = {
        "REMEDIATION": (
            dg.REMEDIATION[dg.DENY_CLASS_AWS_CREDENTIAL],
            "AWS CLI calls themselves are NOT blocked by this rule",
        ),
        "SKILL.md": (
            skill.read_text(encoding="utf-8"),
            "running AWS CLI commands is not blocked by the rule that refused the read",
        ),
        "blocked-commands.md": (
            (_ROOT / "docs" / "blocked-commands.md").read_text(encoding="utf-8"),
            "Running AWS CLI commands is not blocked by that rule",
        ),
    }
    for label, (text, scoped) in surfaces.items():
        assert scoped in text, f"{label} lost the scoped promise"
        assert not _UNSCOPED.search(text), f"{label} promises more than the rule that fired"
