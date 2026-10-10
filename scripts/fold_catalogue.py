#!/usr/bin/env python3
"""fold_catalogue.py -- what every projection fold answers, read out of the code.

    python3 scripts/fold_catalogue.py --check    # gate: committed == generated
    python3 scripts/fold_catalogue.py --write    # regenerate both artifacts
    python3 scripts/fold_catalogue.py --test     # self-test the generator

A dashboard template's numbers come from a fold. An agent writing one needs to know
which folds exist, what each answers, and what the rendered value's fields are called
and typed -- and it has no way to find that out except by reading the projection kernel,
which is four thousand lines and mostly about resumable checkpoints.

So the catalogue is GENERATED. Two artifacts, both committed:

* ``folds.json`` beside the dashboard-template skill -- what its ``scaffold.py`` reads
  to validate ``--fold``, so the script does not restate the fold list.
* ``FOLDS.md`` beside it -- the section the skill body points a reader at.

Hand-writing either one is the failure this replaces. A hand-written list is right on
the day it is written: a fold added, renamed, or given a new rendered field leaves it
silently stale, and stale is worse than absent here, because an agent that trusts it
writes a provider reading a field no fold produces. ``--check`` is the gate that makes
that impossible, and ``test_fold_catalogue.py`` runs it.

## Where the types come from: ONE derivation

This script does not probe the registry. It RENDERS
:func:`kiro_crew.dashboard_types.catalog`, which is the runtime verb an agent calls for
the same question -- so these two artifacts and that tool cannot answer differently.

A second probe here would be a second source of truth wearing one coat: both walk
``start()``->``render()``, both have to be edited together, and they drift. The catalog
consults its own declarations for the leaves whose type IS knowable, so a bare probe
types a stamp ``unknown`` where the catalog types it ``number`` -- and no gate sees the
split, because each half compares against itself.

So the fold's shape, each field's type and whether it is optional are all read off the
catalog. ``--check`` compares the committed bytes with that same rendering, which is
what makes a disagreement impossible rather than merely unlikely.

The types are the MANIFEST's vocabulary -- ``string``, ``number``, ``boolean``,
``object``, ``array`` -- and not Python's, because a template author declares a Model
field in those words. A row that said ``str`` was a row the author had to translate.

``unknown`` survives for one case and is not a gap to apologise for: the empty fold
renders ``None`` and nothing declares what fills it. Those are exactly the fields a
provider must read as possibly absent, so the row carries ``optional: true`` and the
template contract types it ``... | Unsaid``. The safe answer and the honest one are the
same answer. What is gone is the case where a type WAS known and this file still said
``unknown``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from kiro_crew import dashboard_types as dt  # noqa: E402
from kiro_crew.crew_log import projection as proj  # noqa: E402
from kiro_crew.platform_compat import is_link_or_junction  # noqa: E402

#: Where the generated artifacts live: beside the skill that consumes them, so they
#: ship in the same package-data glob the skill already ships under.
SKILL_DIR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "dashboard-template"
JSON_PATH = SKILL_DIR / "folds.json"
MARKDOWN_PATH = SKILL_DIR / "FOLDS.md"

#: Bumped when the DOCUMENT's shape changes, so a consumer can refuse a shape it does
#: not understand rather than reading a missing key as an empty one.
CATALOGUE_VERSION = 1

#: What a field's row reports when the empty fold shows ``None`` and nothing declares
#: what fills it. Taken from the catalog rather than spelled again here: the word has to
#: be the same one the runtime tool answers with, or a reader comparing the two artifacts
#: against a live gateway sees two vocabularies.
UNKNOWN_TYPE = dt.UNKNOWN_TYPE

#: One line per fold: the question it answers, in a reader's words. The ONLY hand-held
#: text in the document, and it is here rather than in the markdown because the gate
#: below asserts every registered fold has one -- a fold added to the kernel with no
#: sentence fails the gate instead of shipping a nameless row.
_ANSWERS: dict[str, str] = {
    "status": (
        "Is this session open, what agent and model is it on, how many turns has it "
        "taken, and what stopped it last"
    ),
    "usage": "Tokens, credits, context, compactions and duration, broken down per model",
    "timeline": "The ordered moments of the session, and how many were dropped",
    "tools": ("Which tools ran, how often, for how long, how many errored, and what is still open"),
    "approvals": "What was requested, what was decided, and what is still pending",
    "subagents": (
        "Which children this session dispatched, and for each one what happened, how "
        "long it ran and what it cost"
    ),
    "class": "What KIND of session this log belongs to, over the log's whole life",
    "outline": (
        "The conversation one row per turn: the first line the human typed and the "
        "reply the turn settled on, both clipped, with no injected context and no "
        "tool output"
    ),
    "ledger": (
        "One workstream's goal, phase and next step, what was tried and rejected, and "
        "its artifacts"
    ),
    "radar": "An issue crew's items, counts, phase lines and recorded skips",
    "work": "A conductor's board: the header, every work item, and what the fold dropped",
    "panel": "The publisher's own record, in the shape the crew drawer consumes",
    "agentic": (
        "Which dashboard fields this crewmate filled itself, with what value, of what "
        "declared type and when"
    ),
    "mistakes": (
        "Which dashboard writes of this crewmate's were refused, grouped by reason and "
        "field, how often, and what worked instead"
    ),
    "workstreams": (
        "Every workstream this crewmate is running: each board's goal and counts, each "
        "task's result, and what each task cost its own worker session"
    ),
    "worktree": (
        "The shape of a fleet: every work board the tree reaches, which board each one "
        "hangs under, and which boards are its roots"
    ),
}


class CatalogueError(Exception):
    """The catalogue cannot be built, or the committed one has drifted."""


def _catalog() -> dict[str, dt.FoldType]:
    """:func:`kiro_crew.dashboard_types.catalog`, keyed by fold name.

    Called once per document build, because the catalog probes every fold and this
    script asks about each one twice -- for its key kind and for its fields.
    """
    return {entry.name: entry for entry in dt.catalog()}


def _field_rows(entry: dt.FoldType) -> list[dict[str, Any]]:
    """One row per top-level field of *entry*'s shape.

    FLAT on purpose, where the catalog's shape nests: this document's reader is the
    skill's ``scaffold.py``, which validates a ``--fold`` choice and a field name
    against it, and a nested row list would make it walk a tree to answer a question
    about one name. A composing agent that needs the nested shape calls the
    ``dashboard_types`` tool, which is the same data unflattened.

    ``optional`` is the shape's own ``nullable``, so it keeps meaning "the empty fold
    renders ``None`` here" -- and the TYPE beside it is now whatever the catalog could
    establish for that leaf, which for a declared stamp is ``number`` and not
    ``unknown``.
    """
    shape = dict(entry.shape)
    if shape.get("type") != "object":
        raise CatalogueError(
            f"{entry.name}: the catalog shape is {shape.get('type')!r}, not an object"
        )
    properties = shape.get("properties")
    if not isinstance(properties, dict):
        raise CatalogueError(f"{entry.name}: the catalog shape carries no properties")
    rows: list[dict[str, Any]] = []
    for field, node in sorted(properties.items()):
        rows.append(
            {
                "name": field,
                "type": node.get("type", UNKNOWN_TYPE),
                "optional": bool(node.get("nullable")),
            }
        )
    return rows


def catalogue() -> dict[str, Any]:
    """The whole document, deterministic, in the kernel's own registry order."""
    missing = sorted(set(proj._FOLDS) - set(_ANSWERS))
    if missing:
        raise CatalogueError(
            f"folds with no sentence saying what they answer: {missing}. Add one to "
            "_ANSWERS in this script; a row with no question is a row nobody can use."
        )
    stale = sorted(set(_ANSWERS) - set(proj._FOLDS))
    if stale:
        raise CatalogueError(f"_ANSWERS names folds the kernel does not have: {stale}")

    catalog = _catalog()
    absent = sorted(set(proj._FOLDS) - set(catalog))
    if absent:
        raise CatalogueError(
            f"the dashboard_types catalog is missing registered folds: {absent}. This "
            "document is rendered from that catalog, so a fold it does not list cannot "
            "be described here."
        )

    folds: list[dict[str, Any]] = []
    for name in proj._FOLDS:
        fold = proj._FOLDS[name]
        entry = catalog[name]
        folds.append(
            {
                "name": name,
                # The catalog's own answer, not a second membership test. A session-keyed
                # fold answers about ONE conversation; a slot-keyed one answers about a
                # workstream that outlived several, and is folded over every log the slot
                # ran under -- so serving it under one session's id reports a part as the
                # whole. A TREE-keyed one answers about a whole fleet and is folded over
                # the logs of every slot the tree reaches, so serving it under one slot
                # reports one board as the tree.
                #
                # One lookup, in ``dashboard_types._keyed_by``. A second one here would
                # have to be kept in the same order -- tree before slot before session,
                # because a two-branch test reports a tree fold as session-keyed -- and
                # the import-time key-kind guard makes a wrong answer unreachable only on
                # the side that asks the guard's own sets.
                "mode": entry.keyed_by,
                "advertised": name not in proj.INTERNAL_PROJECTION_NAMES,
                # ``None`` means every entry moves this fold -- whether the registry
                # leaves ``affects`` unset or spells out the whole vocabulary, which is
                # how ``status`` and ``class`` declare it.
                "affects": (
                    None
                    if fold.affects is None or fold.affects >= proj.KNOWN_TYPES
                    else sorted(fold.affects)
                ),
                "answers": _ANSWERS[name],
                "fields": _field_rows(entry),
            }
        )
    return {"catalogue_version": CATALOGUE_VERSION, "folds": folds}


# --------------------------------------------------------------------------
# the markdown the skill points at
# --------------------------------------------------------------------------


def render_markdown(doc: dict[str, Any]) -> str:
    lines: list[str] = [
        "# The fold catalogue",
        "",
        "<!-- GENERATED by scripts/fold_catalogue.py -- do not edit by hand.",
        "     Regenerate with: python3 scripts/fold_catalogue.py --write",
        "     A test asserts this file equals the generated one, so an edit here is",
        "     reverted by the next run and fails the gate in between. -->",
        "",
        "A dashboard template's numbers come from a fold: a durable projection of the",
        "append-only crew log. Pick one of these before writing anything. A new fold is",
        "the exception and argues for itself in the pull request that adds it.",
        "",
        "`optional` means the field is `None` on an empty fold, so a writer could leave",
        "it unset. Those are exactly the fields a contract types `... | Unsaid` and a",
        "provider reads with `read_text` / `read_int`, never with a `0` default.",
        "",
        "A type is given in the Model field's own vocabulary -- `string`, `number`,",
        "`boolean`, `object`, `array` -- so you can declare the field without",
        "translating. An optional field still carries a real type wherever one is",
        "declared; `unknown` is left only where the empty fold shows `None` and nothing",
        "says what fills it, and there you read the fold's own render function.",
        "",
        "A `session` fold answers about ONE conversation. A `slot` fold answers about a",
        "workstream that outlived several conversations and is folded over every log the",
        "slot ran under. A `tree` fold answers about a whole FLEET: it is keyed by a tree",
        "root and folded over the logs of every slot the tree reaches, so it is the kind a",
        "block binding a tree names. It is read on a page load and on a refetch rather",
        "than pushed, because its value is stale when any member's log grows and no bus",
        "scope covers that.",
        "",
        "## Which fold answers what",
        "",
        "| Fold | Keyed by | Answers |",
        "|---|---|---|",
    ]
    for fold in doc["folds"]:
        internal = "" if fold["advertised"] else " *(internal)*"
        lines.append(f"| `{fold['name']}`{internal} | {fold['mode']} | {fold['answers']} |")
    lines.append("")

    for fold in doc["folds"]:
        lines.append(f"## `{fold['name']}`")
        lines.append("")
        lines.append(fold["answers"] + ".")
        lines.append("")
        if fold["affects"] is None:
            lines.append("Moved by **every** entry in the log.")
        else:
            moved = ", ".join(f"`{item}`" for item in fold["affects"])
            lines.append(f"Moved by: {moved}.")
        lines.append("")
        lines.append("| Field | Type | Optional |")
        lines.append("|---|---|---|")
        for row in fold["fields"]:
            mark = "yes" if row["optional"] else "no"
            lines.append(f"| `{row['name']}` | `{row['type']}` | {mark} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def as_json(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2, sort_keys=False, ensure_ascii=True) + "\n"


# --------------------------------------------------------------------------
# the two modes
# --------------------------------------------------------------------------


def _committed(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogueError(f"{path} is missing or unreadable ({exc}); run --write") from None


def check() -> list[str]:
    """Paths whose committed bytes differ from the generated ones."""
    doc = catalogue()
    drifted: list[str] = []
    for path, generated in ((JSON_PATH, as_json(doc)), (MARKDOWN_PATH, render_markdown(doc))):
        if _committed(path) != generated:
            drifted.append(str(path.relative_to(ROOT)))
    return drifted


def _write_without_following(path: Path, text: str) -> None:
    """Replace *path*'s contents, refusing to write through a link at that name.

    ``Path.write_text`` follows a symlink, so a link committed at one of the two artifact
    names sends this write to whatever it resolves to, while the line this script prints
    still names the path inside the repository.

    The same rule the skill's own scaffold applies to the files it generates, and
    deliberately NOT a sibling temporary file swapped in with :func:`os.replace`: that is
    the staged-write machinery this change removed from the scaffold, on the ground that
    these files live in a checkout on their way to review. What is refused here is a write
    landing somewhere the path does not name, which version control cannot undo -- a
    different property from a torn write, and the only one worth code here.

    ``lstat`` first, because Windows has no ``O_NOFOLLOW``; the flag is added where the
    platform has it as a second answer for a link appearing between the two.
    """
    if is_link_or_junction(path):
        raise SystemExit(
            f"refusing to write {path}: it is a symlink or a directory junction, so the "
            f"write would land somewhere this path does not name"
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write() -> list[str]:
    doc = catalogue()
    written: list[str] = []
    SKILL_DIR.mkdir(parents=True, exist_ok=True)
    for path, generated in ((JSON_PATH, as_json(doc)), (MARKDOWN_PATH, render_markdown(doc))):
        if not path.exists() or path.read_text(encoding="utf-8") != generated:
            _write_without_following(path, generated)
            written.append(str(path.relative_to(ROOT)))
    return written


def selftest() -> int:
    """Prove the generator can SEE a drift, and that its own rules hold.

    A gate whose only evidence is "nothing differs" reads the same way whether it works
    or not, so the comparison is exercised against a planted change here.
    """
    doc = catalogue()
    names = [fold["name"] for fold in doc["folds"]]
    assert names == list(proj._FOLDS), f"catalogue order {names} against {list(proj._FOLDS)}"
    assert names, "catalogue is empty -- the probe is broken, not the kernel"

    vocabulary = set(dt.describe()["field_types"]) | {UNKNOWN_TYPE}
    for fold in doc["folds"]:
        assert fold["fields"], f"{fold['name']} rendered no fields"
        assert fold["mode"] in (dt.KEYED_BY_SESSION, dt.KEYED_BY_SLOT, dt.KEYED_BY_TREE), fold
        for row in fold["fields"]:
            # The MANIFEST's words, which is the whole point of rendering the catalog
            # rather than probing: a Python type name here would be one an author
            # cannot declare.
            assert row["type"] in vocabulary, f"{fold['name']}.{row['name']}: {row['type']!r}"

    # The work fold is slot-keyed and carries the conductor board; a mode regression
    # there would send a template author to read one session's part as the whole.
    work = next(fold for fold in doc["folds"] if fold["name"] == "work")
    assert work["mode"] == "slot", work
    assert work["affects"] == ["work/recorded"], work

    # ``status`` is moved by every entry, which must survive as null rather than as an
    # empty list -- an empty list would read as "no entry moves it".
    status = next(fold for fold in doc["folds"] if fold["name"] == "status")
    assert status["affects"] is None, status

    # The tree-keyed fold is the only one whose mode is neither of the original two, and
    # a regression to ``session`` or ``slot`` is the silent one: it reads as a complete
    # answer and sends a template author to key a whole fleet's tree by one conversation.
    tree_names = [fold["name"] for fold in doc["folds"] if fold["mode"] == "tree"]
    assert tree_names == list(proj.TREE_PROJECTION_NAMES), tree_names

    # Planted drift: one renamed field must change both renderings.
    mutated = json.loads(json.dumps(doc))
    mutated["folds"][0]["fields"][0]["name"] = "__planted__"
    assert as_json(mutated) != as_json(doc), "the JSON rendering ignored a renamed field"
    assert render_markdown(mutated) != render_markdown(
        doc
    ), "the markdown rendering ignored a renamed field"

    print(f"fold-catalogue selftest passed: {len(names)} fold(s), drift is observable")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the projection fold catalogue.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="gate: committed == generated")
    mode.add_argument("--write", action="store_true", help="regenerate both artifacts")
    mode.add_argument("--test", action="store_true", help="self-test the generator")
    args = parser.parse_args(argv)

    try:
        if args.test:
            return selftest()
        if args.write:
            written = write()
            for path in written:
                print(f"wrote {path}")
            if not written:
                print("fold catalogue already current")
            return 0
        drifted = check()
        if drifted:
            print(
                "fold-catalogue gate FAILED: the committed catalogue does not match the "
                f"folds in the code: {drifted}",
                file=sys.stderr,
            )
            print(
                "  Regenerate it: python3 scripts/fold_catalogue.py --write\n"
                "  A stale catalogue is worse than none: an agent that trusts it writes "
                "a provider reading a field no fold produces.",
                file=sys.stderr,
            )
            return 1
        print("fold-catalogue gate passed: the committed catalogue matches the code.")
        return 0
    except CatalogueError as exc:
        print(f"fold-catalogue: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
