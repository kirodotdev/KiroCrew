"""The crew log reference documents types the code actually owns, with valid examples.

An API reference drifts silently: a type is renamed in ``schema.TYPE_OWNERSHIP`` and
the page that documents it keeps its old spelling, or an example is hand-edited into
a shape the envelope would refuse. Both failures look fine to a reader, which is
exactly why they need a gate rather than a review.

Two properties are asserted here.

Every type the page documents is a type this build's session kind owns. Ownership is
per DOMAIN (the part before the slash), matched by prefix so a new action needs no
registry change, so the domain is what can be checked -- and it is the half that
actually moves when the schema is reorganised.

Every JSON example parses, survives the envelope reader, and would be accepted as a
session entry: owned type, acceptable ``src``, serializable ``data``, and a
serialized line inside the size ceiling. The examples are the part of a reference a
reader copies, so an example the writer would refuse is worse than no example.

The page's own summary table is checked against its subsections in the same pass.
The table is the index a reader scans; a type in one and not the other means the page
disagrees with itself.

Scope is the LIVE types only -- the subsection headings. The "Removed types"
appendix names types with no emitter, deliberately kept as a plain table with no
subsection and no example, so trimming them from the schema does not red this test.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

import pytest

from kiro_crew.crew_log.schema import (
    KIND_SESSION,
    MAX_ENTRY_BYTES,
    MAX_REF_SPAN,
    TYPE_OWNERSHIP,
    Entry,
    check_ownership,
    require_data,
    require_entry_line,
    serialize,
    split_type,
)
from kiro_crew.crew_log.store import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT

_DOCS = Path(__file__).parent.parent / "docs" / "reference" / "crew-log"
SESSION_TYPES_DOC = _DOCS / "session-types.md"

#: A live type's subsection heading: ``### `domain/action` ``.
_SUBSECTION_RE = re.compile(r"^### `([a-z]+/[a-z_]+)`\s*$", re.MULTILINE)

#: A summary-table row's first cell, which links to the subsection anchor.
_SUMMARY_ROW_RE = re.compile(r"^\| \[`([a-z]+/[a-z_]+)`\]\(#[a-z]+\) \|", re.MULTILINE)

#: A fenced ``json`` block. Every one on this page is a single entry line.
_JSON_BLOCK_RE = re.compile(r"^```json\n(.*?)\n```$", re.MULTILINE | re.DOTALL)

#: The count the page's own prose claims. Pinned so adding a subsection without
#: updating the page's opening line fails here rather than misleading a reader.
EXPECTED_LIVE_TYPES = 31

#: The only two emitters that write a session entry. Kept as a literal rather than
#: read from ``FIXED_SOURCES``, which holds the crew-side values too.
SESSION_SOURCES = frozenset({"gateway", "acp"})


def _doc_text() -> str:
    return SESSION_TYPES_DOC.read_text(encoding="utf-8")


def _documented_types() -> list[str]:
    return _SUBSECTION_RE.findall(_doc_text())


def _json_examples() -> list[str]:
    return _JSON_BLOCK_RE.findall(_doc_text())


def test_reference_page_exists() -> None:
    """A missing page would make every parametrized case below vacuous."""
    assert SESSION_TYPES_DOC.is_file(), f"{SESSION_TYPES_DOC} is missing"


def test_documented_type_count() -> None:
    types = _documented_types()
    assert len(types) == EXPECTED_LIVE_TYPES, f"documented types: {sorted(types)}"
    assert len(set(types)) == len(types), "a type has two subsections"


@pytest.mark.parametrize("entry_type", _documented_types())
def test_documented_type_domain_is_owned_by_the_session_kind(entry_type: str) -> None:
    domain, _action = split_type(entry_type)
    assert domain in TYPE_OWNERSHIP[KIND_SESSION], (
        f"{entry_type} is documented as a session type, but the session kind does "
        f"not own the {domain!r} domain"
    )


def test_summary_table_matches_the_subsections() -> None:
    assert set(_SUMMARY_ROW_RE.findall(_doc_text())) == set(_documented_types())


def test_every_documented_type_has_an_example() -> None:
    """A type documented without an example is a contract nobody can copy."""
    example_types = {json.loads(block)["type"] for block in _json_examples()}
    assert set(_documented_types()) <= example_types


@pytest.mark.parametrize("block", _json_examples())
def test_example_is_one_line_of_valid_json(block: str) -> None:
    assert "\n" not in block, "an entry example must be one line"
    assert isinstance(json.loads(block), dict), "an entry example must be an object"


@pytest.mark.parametrize("block", _json_examples())
def test_example_passes_the_envelope_reader(block: str) -> None:
    entry = Entry.from_dict(json.loads(block))
    assert entry is not None, "the envelope reader would discard this example"


@pytest.mark.parametrize("block", _json_examples())
def test_example_would_be_accepted_as_a_session_entry(block: str) -> None:
    """The writer's own gates, in the order ``append`` applies them."""
    entry = Entry.from_dict(json.loads(block))
    assert entry is not None
    require_data(entry.data)
    check_ownership(KIND_SESSION, entry.type, entry.src)
    require_entry_line(serialize(entry.to_dict()))


@pytest.mark.parametrize("block", _json_examples())
def test_example_type_is_documented_on_this_page(block: str) -> None:
    """An example may not illustrate a type the page does not document."""
    assert json.loads(block)["type"] in set(_documented_types())


@pytest.mark.parametrize("block", _json_examples())
def test_session_examples_carry_no_crew_only_envelope_fields(block: str) -> None:
    """Session entries never thread, and no session emitter cites a ref."""
    raw = json.loads(block)
    assert "thread" not in raw, "thread is the crew's log shape"
    assert "ref" not in raw, "no session emitter sets ref"


@pytest.mark.parametrize("block", _json_examples())
def test_session_examples_name_only_a_session_emitter(block: str) -> None:
    """``src`` on a session example is one of the two emitters that write one.

    ``check_ownership`` accepts any fixed source for any kind, so ``dashboard`` or
    ``patrol`` on a session entry passes the writer's gates while naming an emitter
    that writes to crew logs. That makes it precisely the mistake a reference
    page can carry without anything catching it, so the page is pinned here.
    """
    assert json.loads(block)["src"] in SESSION_SOURCES


#: A summary row's type together with its ``Emitter`` cell (the third column).
_EMITTER_CELL_RE = re.compile(
    r"^\| \[`([a-z]+/[a-z_]+)`\]\(#[a-z]+\) \| [^|]*\| ([^|]*)\|", re.MULTILINE
)


def _subsection(entry_type: str) -> str:
    """The body of one type's subsection, up to the next one."""
    text = _doc_text()
    start = text.index(f"### `{entry_type}`")
    nxt = text.find("\n### ", start + len(entry_type))
    return text[start : len(text) if nxt < 0 else nxt]


def test_summary_emitter_cell_agrees_with_the_subsection_since_line() -> None:
    """A row claiming an unlanded emitter and its ``Since`` line say the same thing.

    The two facts sit ~850 lines apart -- a table cell at the top, a ``Since`` line in
    the subsection -- so they drift without anything noticing, and the drift has a
    direction that misleads: a row reading ``live`` for a type whose emitter has not
    landed tells a consumer to handle entries nothing writes. Pinned in BOTH
    directions, because either half alone can be the stale one.
    """
    rows = _EMITTER_CELL_RE.findall(_doc_text())
    assert len(rows) == EXPECTED_LIVE_TYPES, f"summary rows parsed: {len(rows)}"
    for entry_type, cell in rows:
        pending = cell.strip().startswith("#")
        since_is_pending = "emitter #" in _subsection(entry_type)
        assert pending == since_is_pending, (
            f"{entry_type}: summary Emitter cell {cell.strip()!r} and its Since line "
            f"disagree about whether the emitter has landed"
        )


#: Every page in the reference, for the checks that span more than session-types.
_PAGES = sorted(_DOCS.glob("*.md"))

#: How each constant is written in prose. A change to the constant changes the
#: string the pages must carry, which is the whole point of asserting it here.
_CONSTANT_RENDERINGS = {
    "MAX_ENTRY_BYTES": f"{MAX_ENTRY_BYTES // 1024} KiB",
    "MAX_REF_SPAN": str(MAX_REF_SPAN),
    "MAX_PAGE_LIMIT": str(MAX_PAGE_LIMIT),
    "DEFAULT_PAGE_LIMIT": str(DEFAULT_PAGE_LIMIT),
}


def _pending_types() -> list[str]:
    """Types whose summary row says their emitter has not landed yet."""
    return [t for t, cell in _EMITTER_CELL_RE.findall(_doc_text()) if cell.strip().startswith("#")]


@functools.lru_cache(maxsize=1)
def _emit_functions_called_in_the_tree() -> frozenset[str]:
    """Every ``on_*`` emitter function called under ``src/kiro_crew``.

    A call site is what makes a type actually written. The emitter module is skipped
    because it DEFINES these functions -- an emitter API with no caller writes nothing,
    which is exactly the state a pending mark claims.

    Read ONCE for the whole module rather than once per pending mark. The tree is
    ~1570 files and ~50 MiB, so scanning it per mark multiplies that by the number of
    marks, which is invisible on a warm Linux filesystem and is shard time on a cold
    Windows one.
    """
    root = Path(__file__).parent.parent / "src" / "kiro_crew"
    called: set[str] = set()
    for path in root.rglob("*.py"):
        if path.parent.name == "crew_log" and path.name == "emit.py":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - unreadable file is not this test's subject
            continue
        # Cheap guard first: almost no file mentions the emitter at all.
        if "crew_log_emit" not in text:
            continue
        called.update(re.findall(r"\b(on_[a-z_]+)\s*\(", text))
    return frozenset(called)


def test_a_pending_emitter_mark_is_not_contradicted_by_the_code() -> None:
    """A type marked as awaiting its emitter must have no caller in THIS build.

    The agreement test above compares the page against itself, so both halves can be
    stale together -- which is what happens the moment the pending change merges and
    nobody flips the marks. This is the half that reads the code, and it is
    deliberately self-clearing: when the emitter gains a caller, this fails and names
    the type, so the page cannot keep claiming the entry is never written.
    """
    called = _emit_functions_called_in_the_tree()
    stale = sorted(t for t in _pending_types() if "on_" + t.replace("/", "_") in called)
    assert not stale, (
        "these types are marked as awaiting their emitter but the code already calls "
        f"it, so each one's summary Emitter cell and Since line must be flipped: {stale}"
    )


@pytest.mark.parametrize("page", _PAGES, ids=lambda p: p.name)
def test_a_documented_constant_carries_its_current_value(page: Path) -> None:
    """A paragraph naming a constant states that constant's value.

    The pages spell these numbers out, because "64 KiB" is what a reader needs and
    ``MAX_ENTRY_BYTES`` is not. Spelling them out is also how they rot, so each claim
    is checked against the constant it names. A mention in a signature default
    (``limit=DEFAULT_PAGE_LIMIT``) asserts no value and is not a claim.
    """
    for block in page.read_text(encoding="utf-8").split("\n\n"):
        for name, rendering in _CONSTANT_RENDERINGS.items():
            if name not in block.replace(f"={name}", ""):
                continue
            assert re.search(rf"\b{re.escape(rendering)}", block), (
                f"{page.name}: a paragraph names {name} without stating its value " f"{rendering!r}"
            )
