"""Ratchet: no machine-readable surface expands crew variables.

A variable is prose meant for an agent to read. The moment one renders into an
``mcpServers`` command/env, an agent spec, or an app manifest, it becomes a
credential-shaped path with none of a credential's protections -- and `bridges.py`'s
own placeholder renderer states the rule this guards: its values must never come
from a writable location, and `config.json` is writable.

Enforced as a source ratchet rather than a behavioural test because the property is
an ABSENCE. There is no call to observe; the only way to check it is to assert the
expander never reaches these modules.
"""

from __future__ import annotations

from pathlib import Path

import kiro_crew

MACHINE_SURFACES = [
    # mcpServers authoring and materialization.
    "dashboard/handlers/mcp_custom.py",
    "mcp_gateway/rewriter.py",
    "mcp_gateway/stub.py",
    # Agent-spec and app-manifest rendering.
    "apps/bridges.py",
    "agent.py",
]

# Any of these appearing in a machine-surface module means a variable value can
# reach it.
FORBIDDEN = (
    "from kiro_crew.variables import",
    "import kiro_crew.variables",
    "variable_values_for",
    "resolve_variables",
)


def _root() -> Path:
    return Path(kiro_crew.__file__).resolve().parent


def test_machine_surfaces_do_not_import_the_expander():
    root = _root()
    offenders: list[str] = []
    checked = 0
    for rel in MACHINE_SURFACES:
        path = root / rel
        if not path.exists():
            # A moved module must not silently drop out of the ratchet.
            offenders.append(f"{rel}: MISSING — update the surface list")
            continue
        checked += 1
        body = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            if needle in body:
                offenders.append(f"{rel}: references {needle!r}")
    assert checked, "ratchet checked nothing — the surface list is wrong"
    assert not offenders, "a machine-readable surface can now reach crew variables: " + "; ".join(
        offenders
    )


def test_the_ratchet_would_notice_an_expander_import():
    """Positive control: the needles are the real import forms, so a surface that
    gained one would be caught. Verified against a module that legitimately has it."""
    body = (_root() / "config" / "loader.py").read_text(encoding="utf-8")
    assert any(needle in body for needle in FORBIDDEN), (
        "no FORBIDDEN needle matches a module known to use the expander — "
        "the needles have drifted and the ratchet is vacuous"
    )
