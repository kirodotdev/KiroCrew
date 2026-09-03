"""The KAS derive-plus-fallback is spelled once, in ``derived_agent_permissions`` (#7513).

PR #7238 introduced the wrapper as the shared spelling of derive-plus-fallback
(``allowed_tools_to_permissions`` then ``{"rules": []}`` when nothing
qualifies), but migrated only one of the three call sites. The other two kept
the inline spelling, so the fallback existed in three places and a future
divergence between them would have been invisible: all three behaved
identically, so no behavioural test could tell them apart.

The guard here is structural, not behavioural, for exactly that reason: it
pins WHERE the fallback is spelled. The IMPORT half of the invariant is
already held by the live CI boundary gate -- after this migration pruned
``agent.py`` from ``.github/agent-sdk-boundary-baseline.txt`` (shrink-only),
``scripts/check_agent_sdk_boundary.py`` reds any re-grown
``kiro_crew.acp.kas_permissions`` import in application code, including the
dynamic-import spellings a second scanner would miss. This module guards only
the half nothing else covers: the fallback expression itself re-growing
around some other derive path.

Behavioural coverage of the fallback lives with each call site
(``test_kas_permissions.TestTheDiskWriter``, ``test_conductor_agent``,
``test_pipeline_conductor_agent``) -- those are the tests that red when the
wrapper's fallback changes, now for every site at once.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "kiro_crew"

#: Trees allowed to spell the fallback, as ``as_posix()`` prefixes relative
#: to the repo root (Windows ``relative_to`` yields backslashes; the table
#: must not depend on the host separator):
#: - the ``acp`` package that defines the derive (package-internal use is not
#:   a boundary crossing -- the same exemption
#:   ``scripts/check_agent_sdk_boundary.py`` applies);
#: - the ``agent_sdk`` driver, whose ``derived_agent_permissions`` wrapper is
#:   the one legitimate spelling.
ALLOWED_PREFIXES = (
    "src/kiro_crew/acp/",
    "src/kiro_crew/agent_sdk/drivers/acp.py",
)

#: Vendored third-party code: not ours, never spells the fallback, and
#: reading it would only slow the scan.
EXCLUDED_PARTS = ("_vendor",)


def test_the_inline_fallback_spelling_is_gone_from_application_code():
    """No copy of the wrapper's fallback survives outside the wrapper.

    Born red against unmodified main, where ``agent.py`` held two copies of
    the spelling (one per inline derive site). Textual on purpose: the
    duplication being removed was a byte-identical one-line spelling, so the
    exact spelling is the thing to pin. The import half of the invariant is
    the CI boundary gate's (see the module docstring).
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
