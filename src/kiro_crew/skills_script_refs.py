"""Reconcile the scripts a generated procedure cites with the scripts it ships.

A generated skill body can name a script that the pipeline never wrote: the
generator cited it without emitting it, or the static validator rejected it.
Staged unchanged, that body hands a reviewer a step with nothing to run.
``reconcile_script_references`` finds the cited names that did not ship and
marks each citing line, so the gap is visible in the body itself.

Only reference shapes that mean "run this script" count, because a body may
legitimately mention a project file by name (``history.py``) without asking
anyone to run it:

- ``scripts/<name>.py`` / ``scripts/<name>.sh`` not inside a longer path, the
  layout every auto-skill ships its scripts under;
- an interpreter invocation of a bare name: ``python3 <name>.py``,
  ``bash <name>.sh`` (flags between the two allowed);
- ``./<name>.py`` / ``./<name>.sh``;
- ``run`` / ``execute`` / ``invoke`` followed by a bare name, backticked or not.

The marker says the script is not shipped with this skill rather than that it
was never generated: an invocation of a project file the skill does not own is
matched too, and "not shipped" stays true for it.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from kiro_crew.skills_auto_model import _PENDING_DOCUMENT_MAX_BYTES

_NAME = r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:py|sh)(?![\w-]|\.\w)"

_REFERENCE_PATTERNS = (
    re.compile(rf"(?<![\w./-])scripts/({_NAME})"),
    re.compile(rf"(?<![\w./-])(?:python3?|bash|sh|zsh)\s+(?:-{{1,2}}[\w-]+\s+)*({_NAME})"),
    re.compile(rf"(?<![\w./-])\./({_NAME})"),
    re.compile(rf"(?i)\b(?:run|execute|invoke)\s+`?({_NAME})"),
)

_FENCE = re.compile(r"^\s*(```|~~~)")

#: Bounds on the refused-script report a staged candidate's ``.meta.json``
#: carries. The detail read refuses a ``.meta.json`` over its document cap
#: (``_PENDING_DOCUMENT_MAX_BYTES``), which would make the whole candidate
#: unreviewable, so the serialized report is also held to half of that cap.
MAX_REJECTED_ENTRIES = 16
MAX_REJECTED_FINDINGS = 8
MAX_REJECTED_CHARS = 200
#: The key a cut report carries; angle brackets keep it off any real file name.
TRUNCATED_KEY = "<truncated>"
_TRUNCATED_NOTE = (
    "report truncated: entries, findings or strings over the retention bounds were omitted"
)


def shippable_script_name(name: str) -> bool:
    """Whether staging writes a script under *name*: a plain file name, no nesting."""
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name


def bound_rejected_scripts(report: dict[str, list[str]]) -> dict[str, list[str]]:
    """Return *report* cut to the entry, finding and string bounds above.

    Entries are dropped from the end until the JSON form fits half the pending
    document cap, the bound that matters to the reader. Any cut is recorded
    under ``TRUNCATED_KEY``, so a shortened report never reads as complete. The
    caller passes already-redacted text; cutting after redaction cannot leave a
    credential fragment the scrubber missed.
    """
    bounded: dict[str, list[str]] = {}
    cut = len(report) > MAX_REJECTED_ENTRIES
    for name, findings in list(report.items())[:MAX_REJECTED_ENTRIES]:
        key = str(name)[:MAX_REJECTED_CHARS]
        findings = list(findings)
        values = [str(f)[:MAX_REJECTED_CHARS] for f in findings[:MAX_REJECTED_FINDINGS]]
        cut = cut or key != str(name) or len(findings) > MAX_REJECTED_FINDINGS
        cut = cut or any(len(str(f)) > MAX_REJECTED_CHARS for f in findings[:MAX_REJECTED_FINDINGS])
        if key in bounded or key == TRUNCATED_KEY:
            cut = True
            continue
        bounded[key] = values
    if cut:
        bounded[TRUNCATED_KEY] = [_TRUNCATED_NOTE]
    while (
        len(bounded) > 1 and len(json.dumps(bounded, indent=2)) > _PENDING_DOCUMENT_MAX_BYTES // 2
    ):
        bounded.pop(next(k for k in reversed(bounded) if k != TRUNCATED_KEY))
        bounded[TRUNCATED_KEY] = [_TRUNCATED_NOTE]
    return bounded


def marker_for(name: str) -> str:
    """The visible marker a citing line carries for a script that did not ship."""
    return f"[script {name} not shipped with this skill]"


def _cited_names(line: str) -> list[str]:
    names: list[str] = []
    for pattern in _REFERENCE_PATTERNS:
        for match in pattern.finditer(line):
            name = match.group(1)
            if name not in names:
                names.append(name)
    return names


def reconcile_script_references(procedure_md: str, shipped: Iterable[str]) -> tuple[str, list[str]]:
    """Return ``(body, missing)`` for *procedure_md* against the *shipped* script names.

    ``missing`` lists, in first-cited order, every cited script name absent from
    *shipped*. ``body`` is *procedure_md* with each citing line followed by one
    marker per missing name; a line inside a fenced block gets its markers on a
    line after the fence closes, so the fenced text itself is unchanged. A
    marker already present is not added again. With nothing missing the body is
    returned as given.
    """
    shipped_names = {str(n).strip() for n in shipped if str(n).strip()}
    missing: list[str] = []
    out: list[str] = []
    in_fence = False
    fence_markers: list[str] = []
    for line in procedure_md.split("\n"):
        if _FENCE.match(line):
            out.append(line)
            if in_fence and fence_markers:
                out.append(" ".join(fence_markers))
                fence_markers = []
            in_fence = not in_fence
            continue
        absent = [n for n in _cited_names(line) if n not in shipped_names]
        for name in absent:
            if name not in missing:
                missing.append(name)
        markers = [marker_for(n) for n in absent if marker_for(n) not in line]
        if in_fence:
            fence_markers.extend(m for m in markers if m not in fence_markers)
            out.append(line)
        elif markers:
            out.append(f"{line.rstrip()} {' '.join(markers)}")
        else:
            out.append(line)
    if fence_markers:
        out.append(" ".join(fence_markers))
    if not missing:
        return procedure_md, []
    return "\n".join(out), missing
