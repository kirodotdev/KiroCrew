"""Every known agent backend must have a column in the host contract.

`docs/system-specs/modules/agent-host-contract.md` says what a backend must
supply to Crew *besides* speaking ACP, in nine buckets, one column per backend.
Its own New-provider checklist says "Silence is not an answer" -- and until this
gate existed, nothing enforced that.

Codex proved it.  It landed in ``ACP_BACKENDS_KNOWN`` and in
``BASELINE_SELECTABLE_BACKENDS``, so a plain public build served sessions on it,
while appearing in none of the nine buckets.  The §5 failure the checklist
already described then recurred unchanged on it: no MCP projection, so none of
Crew's own control plane reaches a codex session.  A stronger sentence had
already been tried; this is the gate.

Scope, stated honestly: this asserts a column EXISTS, not that its cells are
true or even informative.  A column of "unknown" passes.  That is the limit of a
text gate -- it makes the omission visible, and a reviewer makes it answered.

What it does and does not check structurally, since a reader will assume more
than is here.  It DOES require each bucket's contract table to be the first
table under its heading, reject a second backend-shaped table in the same
bucket, verify the markdown rule row's width, resolve every Column-meaning row's
CONSTANT NAME against :mod:`kiro_crew.acp_backends` and confirm the documented
value matches the real one, and require bucket numbering to be consecutive from
1 and to reach at least 9.  It does NOT check heading titles, cell padding, or
prose -- a bucket renamed but still numbered passes.

Three attacks drove that list, all of which an earlier draft of this file
allowed: a decoy backend-shaped table placed before the real one (the parser
took the first and stopped looking), a continuation table further down the
bucket omitting a backend, and a fabricated ``ACP_BACKEND_FAKE = ""`` row
impersonating kiro-cli -- which worked because only the quoted value was read
and a set erases the alias.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew import acp_backends

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOC = _REPO_ROOT / "docs" / "system-specs" / "modules" / "agent-host-contract.md"

_MEANING_HEADING = "## Column meaning"
_BUCKET_HEADING = re.compile(r"^## (?P<n>\d+)\.\s+(?P<title>.+?)\s*$")

# ``| **CC** | `ACP_BACKEND_CLAUDE = "claude"` -- ... |`` -- BOTH halves are
# captured: the value alone is forgeable, the constant name is not.
_MEANING_ROW = re.compile(
    r"^\|\s*\*\*(?P<label>[^*]+)\*\*\s*\|.*?(?P<const>ACP_BACKEND_[A-Z0-9_]+)\s*=\s*\"(?P<id>[^\"]*)\""
)

_RULE_CELL = re.compile(r"^:?-{3,}:?$")


def _lines() -> list[str]:
    return _DOC.read_text(encoding="utf-8").split("\n")


def _cells(row: str) -> list[str]:
    """Cells of a markdown table row, outer pipes dropped, each stripped."""
    inner = row.strip()
    if not (inner.startswith("|") and inner.endswith("|")):
        return []
    return [c.strip() for c in inner[1:-1].split("|")]


def _is_backend_header(row: str) -> bool:
    """A bucket table's header: leading empty cell, then one cell per backend."""
    cells = _cells(row)
    return len(cells) >= 3 and cells[0] == "" and all(cells[1:])


def _is_rule_row(row: str) -> bool:
    cells = _cells(row)
    return bool(cells) and all(_RULE_CELL.match(c) for c in cells)


def _constant_names() -> dict[str, str]:
    """Backend id -> the ``ACP_BACKEND_*`` name that holds it, for messages."""
    names: dict[str, str] = {}
    for attr in dir(acp_backends):
        if attr.startswith("ACP_BACKEND_") and not attr.startswith("ACP_BACKENDS_"):
            value = getattr(acp_backends, attr)
            if isinstance(value, str):
                names[value] = attr
    return names


def _spell(ids: set[str]) -> list[str]:
    """Name ids by their constant, so the empty kiro id is not printed as ``''``."""
    names = _constant_names()
    return sorted(f"{names.get(i, '?')}={i!r}" for i in ids)


def _column_meaning() -> tuple[dict[str, str], list[str]]:
    """``(label -> backend id, problems)`` parsed from the Column-meaning table.

    Scoped to that section: a row anywhere else in the document must not be able
    to define a backend, and the constant name is resolved against the module so
    a fabricated name cannot stand in for a real one.
    """
    meaning: dict[str, str] = {}
    problems: list[str] = []
    inside = False
    for i, line in enumerate(_lines(), start=1):
        if line.startswith("## "):
            inside = line.strip() == _MEANING_HEADING
            continue
        if not inside:
            continue
        m = _MEANING_ROW.match(line)
        if not m:
            continue
        label, const, documented = m.group("label").strip(), m.group("const"), m.group("id")
        actual = getattr(acp_backends, const, None)
        if actual is None:
            problems.append(
                f"line {i}: Column-meaning row names {const}, which does not exist in "
                "kiro_crew.acp_backends"
            )
            continue
        if actual != documented:
            problems.append(
                f"line {i}: Column-meaning row documents {const} = {documented!r}, but the "
                f"constant holds {actual!r}"
            )
            continue
        if label in meaning:
            problems.append(f"line {i}: duplicate column label {label!r}")
            continue
        meaning[label] = actual
    return meaning, problems


def _bucket_tables() -> tuple[list[tuple[int, str, int, list[str]]], list[str]]:
    """``[(number, title, header line, column labels)]`` plus structural problems.

    The contract table must be the FIRST table under its heading and carry a
    matching rule row; a second backend-shaped table in the same bucket is
    rejected rather than ignored, because ignoring it is how a continuation table
    omits a backend unnoticed.
    """
    lines = _lines()
    tables: list[tuple[int, str, int, list[str]]] = []
    problems: list[str] = []
    open_bucket: tuple[int, str] | None = None
    seen_table = False

    for idx, line in enumerate(lines):
        heading = _BUCKET_HEADING.match(line)
        if line.startswith("## "):
            open_bucket = (int(heading.group("n")), heading.group("title")) if heading else None
            seen_table = False
            continue
        if open_bucket is None or not _is_backend_header(line):
            continue
        number, title = open_bucket
        if seen_table:
            problems.append(
                f"bucket {number} ({title!r}) has a second backend-shaped table at line "
                f"{idx + 1}. The gate reads only the first, so a later table can omit a "
                "backend unnoticed -- fold it into the contract table or reshape it."
            )
            continue
        seen_table = True
        nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
        if not _is_rule_row(nxt):
            problems.append(
                f"bucket {number} ({title!r}): line {idx + 2} is not a markdown rule row, so "
                f"the table at line {idx + 1} does not render as a table"
            )
        elif len(_cells(nxt)) != len(_cells(line)):
            problems.append(
                f"bucket {number} ({title!r}): the rule row at line {idx + 2} has "
                f"{len(_cells(nxt))} cells but the header at line {idx + 1} has "
                f"{len(_cells(line))}"
            )
        tables.append((number, title, idx + 1, _cells(line)[1:]))
    return tables, problems


def test_every_known_backend_has_a_column_in_every_bucket() -> None:
    meaning, meaning_problems = _column_meaning()
    tables, structure_problems = _bucket_tables()
    known = set(acp_backends.ACP_BACKENDS_KNOWN)

    offenders = list(meaning_problems) + list(structure_problems)
    for number, title, line, labels in tables:
        unresolved = [label for label in labels if label not in meaning]
        if unresolved:
            offenders.append(
                f"bucket {number} ({title!r}) line {line}: column label(s) {unresolved} have "
                f"no row in the '{_MEANING_HEADING}' table, so they name no backend"
            )
            continue
        resolved = {meaning[label] for label in labels}
        if resolved != known:
            missing, extra = known - resolved, resolved - known
            detail = []
            if missing:
                detail.append(f"missing {_spell(missing)}")
            if extra:
                detail.append(f"unexpected {_spell(extra)}")
            offenders.append(f"bucket {number} ({title!r}) line {line}: {'; '.join(detail)}")

    assert not offenders, (
        "every id in ACP_BACKENDS_KNOWN needs a column in every bucket of "
        "docs/system-specs/modules/agent-host-contract.md, and each bucket's contract "
        f"table must be the first table under its heading. Problems: {offenders}. A "
        "backend reachable on a public build with no column here is the failure this "
        "gate exists for -- Codex shipped that way and repeated a documented section 5 "
        'defect. Add the column plus a \'| **<Label>** | `ACP_BACKEND_<NAME> = "<id>"` '
        "-- ... |' row to the Column-meaning table; where an answer is not known yet, "
        "write that rather than a guess."
    )


def test_the_column_meaning_table_covers_every_known_backend() -> None:
    """The resolver itself must know every id, or the gate above cannot see one."""
    meaning, problems = _column_meaning()
    assert not problems, problems
    ids = set(meaning.values())
    known = set(acp_backends.ACP_BACKENDS_KNOWN)
    assert ids == known, (
        f"the '{_MEANING_HEADING}' table must name exactly the ids in ACP_BACKENDS_KNOWN; "
        f"missing {_spell(known - ids)}, unexpected {_spell(ids - known)}"
    )


def test_the_gate_sees_the_buckets_it_is_meant_to_cover() -> None:
    """A renumbered or dropped bucket would otherwise make this suite vacuous.

    Numbering must be consecutive from 1 and reach at least 9, so deleting a
    bucket fails while legitimately adding a tenth does not.
    """
    tables, problems = _bucket_tables()
    assert not problems, problems
    numbers = [n for n, _, _, _ in tables]
    assert numbers == list(range(1, len(numbers) + 1)), (
        "bucket headings must be numbered consecutively from 1, one contract table each; "
        f"found {numbers}"
    )
    assert len(numbers) >= 9, (
        f"expected at least the nine documented buckets, found {len(numbers)}: "
        f"{[(n, t) for n, t, _, _ in tables]}"
    )
    for number, title, line, labels in tables:
        assert len(labels) >= 2, f"bucket {number} ({title!r}) line {line} parsed as {labels}"
