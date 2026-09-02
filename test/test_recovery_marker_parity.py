"""Guard: the backend's recovery markers must match the frontend's wire list.

Every ``*_RECOVERY_PREFIX`` in ``kiro_crew.dashboard.state`` is a literal string
the backend puts at the head of an injected continuation. ``PREFIXES`` in
``website/src/pages/chat/RecoveryCard.tsx`` is the frontend's copy of that same
list, matched with ``startsWith`` to pick which card a transcript row renders as.
There is no shared schema between them -- the marker travels as message text --
so the two lists are hand-synced, and ``state.py`` says so itself: "all eight
recovery markers share one home and the frontend has one list to mirror".

Drift is silent in the direction that matters. A backend marker the frontend
does not know falls through ``parseRecoveryMessage`` and renders as the generic
warning-styled inject row, which is the exact defect the conn-recovery change
fixed: machine orchestration shown as if it were the user's own text. Nothing
errors, no test fails, and the only symptom is a row that looks wrong to a
person reading the transcript.

The family has drifted before: ``main`` gained a ``manual`` marker without
bumping the count comment in ``state.py``, so that comment read "five" while
eight markers existed. This guard exists so the next addition cannot land on one
side only. It DISCOVERS the backend constants rather than listing them, so a
ninth marker is picked up here automatically instead of making this file a third
hand-maintained copy.

This is a regression guard for behaviour that is already correct, not a test of
new behaviour: it is verified non-vacuous by mutation (drop a marker from either
side and it fails), not by having been observed red first.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_BACKEND = _REPO / "src" / "kiro_crew" / "dashboard" / "state.py"
_FRONTEND = _REPO / "website" / "src" / "pages" / "chat" / "RecoveryCard.tsx"

# `SOMETHING_RECOVERY_PREFIX = "[...]"` at column 0.
_BACKEND_RE = re.compile(
    r'^(?P<name>[A-Z][A-Z0-9_]*_RECOVERY_PREFIX)\s*=\s*"(?P<prefix>\[[^"]+\])"',
    re.MULTILINE,
)
# `const PREFIXES: ReadonlyArray<[RecoveryKind, string]> = [ ... ]`
_FRONTEND_BLOCK_RE = re.compile(
    r"const PREFIXES:[^=]*=\s*\[(?P<body>.*?)^\]", re.DOTALL | re.MULTILINE
)
_FRONTEND_ENTRY_RE = re.compile(r"\[\s*'(?P<kind>[a-z_]+)'\s*,\s*'(?P<prefix>\[[^']+\])'\s*\]")


def _backend_markers() -> dict[str, str]:
    found = _BACKEND_RE.findall(_BACKEND.read_text(encoding="utf-8"))
    assert found, "no *_RECOVERY_PREFIX constants found in state.py"
    return {name: prefix for name, prefix in found}


def _frontend_markers() -> dict[str, str]:
    match = _FRONTEND_BLOCK_RE.search(_FRONTEND.read_text(encoding="utf-8"))
    assert match, "could not find the PREFIXES list in RecoveryCard.tsx"
    entries = _FRONTEND_ENTRY_RE.findall(match.group("body"))
    assert entries, "PREFIXES parsed as empty"
    return {kind: prefix for kind, prefix in entries}


def test_backend_recovery_markers_match_the_frontend_prefix_list() -> None:
    backend = _backend_markers()
    frontend = _frontend_markers()

    missing_in_frontend = set(backend.values()) - set(frontend.values())
    missing_in_backend = set(frontend.values()) - set(backend.values())

    assert not missing_in_frontend, (
        "backend marker(s) the frontend cannot parse, so they render as the "
        f"generic inject row instead of a recovery card: {sorted(missing_in_frontend)}. "
        "Add them to PREFIXES in RecoveryCard.tsx."
    )
    assert not missing_in_backend, (
        "frontend PREFIXES entr(ies) with no backend constant, so nothing emits "
        f"them: {sorted(missing_in_backend)}. Remove them, or add the constant to state.py."
    )
    # Redundant given the two set checks, but it names the invariant the
    # `state.py` comment states in prose and pins the count against a duplicate
    # entry on either side.
    assert len(backend) == len(frontend), (
        f"{len(backend)} backend marker(s) vs {len(frontend)} frontend entr(ies) — "
        "one side has a duplicate prefix under two names"
    )
