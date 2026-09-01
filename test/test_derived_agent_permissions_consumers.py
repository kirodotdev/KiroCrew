"""Every KAS-policy derive goes through ``derived_agent_permissions`` (#7513).

PR #7238 introduced the wrapper as the shared spelling of derive-plus-fallback
(``allowed_tools_to_permissions`` then ``{"rules": []}`` when nothing
qualifies), but migrated only one of the three call sites. The other two kept
the inline spelling, so the fallback existed in three places and a future
divergence between them would have been invisible: all three behaved
identically, so no behavioural test could tell them apart.

The guard here is structural, not behavioural, for exactly that reason: it
pins WHERE the derive is spelled. ``allowed_tools_to_permissions`` may be
imported only by the boundary itself -- the ``agent_sdk`` driver that wraps it
and the ``acp`` package that defines it. Application code that wants a KAS
policy goes through the wrapper, which is also what keeps ``agent.py`` out of
``.github/agent-sdk-boundary-baseline.txt`` (a shrink-only ratchet this
migration shrank to zero for that file).

Behavioural coverage of the fallback itself lives with each call site
(``test_kas_permissions.TestTheDiskWriter``, ``test_conductor_agent``,
``test_pipeline_conductor_agent``) -- those are the tests that red when the
wrapper's fallback changes, now for every site at once.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "kiro_crew"

#: The one symbol whose import location this guard pins.
DERIVE_SYMBOL = "allowed_tools_to_permissions"
DERIVE_MODULE = "kiro_crew.acp.kas_permissions"

#: Trees allowed to reach the raw derive, as ``as_posix()`` prefixes relative
#: to the repo root (Windows ``relative_to`` yields backslashes; the table
#: must not depend on the host separator):
#: - the ``acp`` package that defines it (package-internal use, e.g.
#:   ``kas_agents.py``, is not a boundary crossing -- the same exemption
#:   ``scripts/check_agent_sdk_boundary.py`` applies);
#: - the ``agent_sdk`` driver, the boundary layer whose
#:   ``derived_agent_permissions`` wrapper is the spelling everyone else uses.
ALLOWED_PREFIXES = (
    "src/kiro_crew/acp/",
    "src/kiro_crew/agent_sdk/drivers/acp.py",
)

#: Vendored third-party code: not ours, never imports the derive, and parsing
#: it would only slow the scan.
EXCLUDED_PARTS = ("_vendor",)


def _imports_raw_derive(tree: ast.AST) -> bool:
    """True when *tree* imports the derive symbol or its defining module.

    AST-based so a multi-line or function-local import is judged the same as a
    top-level one -- both inline sites this guard was written against were
    function-local imports.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == DERIVE_MODULE and any(
                alias.name == DERIVE_SYMBOL for alias in node.names
            ):
                return True
            if node.module == "kiro_crew.acp" and any(
                alias.name == "kas_permissions" for alias in node.names
            ):
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name == DERIVE_MODULE for alias in node.names):
                return True
    return False


def test_no_application_code_imports_the_raw_derive():
    """The derive-plus-fallback is spelled once, in the wrapper.

    Red on any ``src/`` module outside the boundary importing
    ``allowed_tools_to_permissions`` (or its module) directly -- which is the
    state this test was born red against: ``agent.py`` held two such imports,
    each followed by its own copy of the ``{"rules": []}`` fallback.
    """
    offenders: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if any(part in path.parts for part in EXCLUDED_PARTS):
            continue
        if rel.startswith(ALLOWED_PREFIXES):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        if _imports_raw_derive(tree):
            offenders.append(rel)
    assert offenders == [], (
        "these modules import the raw KAS derive instead of "
        f"kiro_crew.agent_sdk.drivers.acp.derived_agent_permissions: {offenders}"
    )


def test_the_inline_fallback_spelling_is_gone_from_application_code():
    """No copy of the wrapper's fallback survives outside the wrapper.

    The import guard above catches the derive; this one catches the fallback
    half re-growing around some other derive path. Textual on purpose: the
    duplication being removed was a byte-identical one-line spelling, so the
    exact spelling is the thing to pin.
    """
    needle = 'if derived is not None else {"rules": []}'
    offenders: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if any(part in path.parts for part in EXCLUDED_PARTS):
            continue
        if rel.startswith(ALLOWED_PREFIXES):
            continue
        if needle in path.read_text(encoding="utf-8"):
            offenders.append(rel)
    assert offenders == [], f"inline {needle!r} fallback re-grown in: {offenders}"
