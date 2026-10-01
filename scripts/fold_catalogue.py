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

## How a field's type is determined, and where it honestly cannot be

Each fold is rendered from its own ``start()`` state -- the empty fold -- because that
is reachable with no fixtures at all. A fixture per fold would be a second source of
truth, which is the thing being removed.

A field holding a real value reports that value's own ``type(value).__name__``, which
is an observation, not a guess.

An empty fold leaves some fields ``None``: the projection kernel uses ``None`` as the
initial value for fields that later hold a real one. For those the observed type says
nothing, and the catalogue says ``unknown``. It does not infer one from an entry field
of the same name: a rendered name is not owned by any one entry type, so that lookup
answers confidently and sometimes wrongly, and a reader cannot tell which rows to
trust. One wrong row costs more than every missing one.

That is not a gap to apologise for. A field that is ``None`` on an empty fold is
exactly a field a provider must read as possibly absent, so its row carries
``optional: true`` and the template contract types it ``... | Unsaid``. The safe answer
and the honest one are the same answer.
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

#: What a field's row reports when the empty fold shows ``None`` and the generator was
#: therefore never told the type. NOT a placeholder to be filled in later by inference:
#: see the module docstring for why a same-name lookup is worse than this word.
UNKNOWN_TYPE = "unknown"

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
    "ledger": (
        "One workstream's goal, phase and next step, what was tried and rejected, and "
        "its artifacts"
    ),
    "radar": "An issue crew's items, counts, phase lines and recorded skips",
    "work": "A conductor's board: the header, every work item, and what the fold dropped",
    "panel": "The publisher's own record, in the shape the crew drawer consumes",
}


class CatalogueError(Exception):
    """The catalogue cannot be built, or the committed one has drifted."""


def _rendered(name: str) -> dict[str, Any]:
    """One fold's rendered value on its EMPTY state.

    A fold that DECLARES a slot binder is bound before it is rendered: it is told which
    slot it is folding before the first entry, and rendering it unbound would answer
    about no board at all. Today exactly one fold declares one, so most slot-keyed folds
    take no binder -- those, like the session-keyed ones, need no slot to render their
    empty state, and this function must not pretend otherwise.
    """
    fold = proj._FOLDS[name]
    state = fold.start()
    if fold.bind_slot is not None:
        fold.bind_slot(state, "catalogue-probe")
    value = fold.render(state)
    if not isinstance(value, dict):
        raise CatalogueError(f"{name}: render returned {type(value).__name__}, not an object")
    return value


def _field_rows(name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for field, value in sorted(_rendered(name).items()):
        if value is None:
            # No inference here, on purpose. The obvious one -- look the field NAME up
            # in the entry-type registry -- is wrong: a rendered name belongs to no one
            # entry type, so it answered ``status.previous`` with ``dict`` and
            # ``status.turn`` with ``int``. ``unknown`` is the true answer.
            rows.append({"name": field, "type": UNKNOWN_TYPE, "optional": True})
            continue
        rows.append({"name": field, "type": type(value).__name__, "optional": False})
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

    folds: list[dict[str, Any]] = []
    for name in proj._FOLDS:
        fold = proj._FOLDS[name]
        folds.append(
            {
                "name": name,
                # A session-keyed fold answers about ONE conversation. A slot-keyed one
                # answers about a workstream that outlived several, and is folded over
                # every log the slot ran under -- so serving it under one session's id
                # reports a part as the whole.
                "mode": "session" if name in proj.SESSION_FOLD_NAMES else "slot",
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
                "fields": _field_rows(name),
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
        "provider reads with `read_text` / `read_int`, never with a `0` default. Every",
        "optional field's type is `unknown`, because an empty fold shows `None` and says",
        "nothing about what fills it -- read the fold's own render function before you",
        "type it, and never let this file guess on your behalf.",
        "",
        "A `session` fold answers about ONE conversation. A `slot` fold answers about a",
        "workstream that outlived several conversations and is folded over every log the",
        "slot ran under.",
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

    for fold in doc["folds"]:
        assert fold["fields"], f"{fold['name']} rendered no fields"
        assert fold["mode"] in ("session", "slot"), fold
        for row in fold["fields"]:
            assert row["type"] != "NoneType", f"{fold['name']}.{row['name']} typed NoneType"

    # The work fold is slot-keyed and carries the conductor board; a mode regression
    # there would send a template author to read one session's part as the whole.
    work = next(fold for fold in doc["folds"] if fold["name"] == "work")
    assert work["mode"] == "slot", work
    assert work["affects"] == ["work/recorded"], work

    # ``status`` is moved by every entry, which must survive as null rather than as an
    # empty list -- an empty list would read as "no entry moves it".
    status = next(fold for fold in doc["folds"] if fold["name"] == "status")
    assert status["affects"] is None, status

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
