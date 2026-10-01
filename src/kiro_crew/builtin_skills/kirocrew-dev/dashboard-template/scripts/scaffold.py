#!/usr/bin/env python3
"""scaffold.py -- emit one dashboard template's four files from a field list.

    scaffold.py <slug> --fold status --fields turns_refused:fraction:turns_completed \
        lifecycle:'str|unsaid'

Writes, under the repository root it is run from:

    src/kiro_crew/dashboard_templates/<slug>.html
    src/kiro_crew/dashboard_templates/<slug>_contract.py
    src/kiro_crew/dashboard_templates/<slug>_provider.py
    test/test_dashboard_template_<slug>.py

and prints the one registration line to paste into ``registry.py``.

The point of the script is that the mechanical part is not judgment. A template's
html, contract, provider and parity test must agree on one field list, and a human
transcribing that list four times will get it wrong in the direction that is silent:
a field in the page and not the contract renders an empty cell. So the list is stated
once, here, and the four files are derived from it.

What it does NOT decide: which fold answers the question, what the page should say,
or whether a number belongs on it at all. Those are the author's, and the skill body
is where they are argued.

Field kinds
-----------

``str|unsaid``     text read out of the fold, which may not carry it
``fraction``       a count, rendered ``N/M``; the ONLY way a number reaches a page

There is no bare ``str``. Every field an author declares is read out of the fold, and a
fold value can always be absent -- that is the premise of ``read_text`` -- so a declared
text field is ``str | Unsaid`` or it is a contract the provider cannot satisfy. Plain
``str`` exists only for the two fields this script supplies itself (``contract_version``,
``captured_at``), which the provider writes rather than reads.

There is no bare ``int``. A count as a plain number has to answer two questions with
no safe default -- what its total is, and what to show when the fold does not carry it
-- and both wrong answers are silent: a bare "7" reads as complete information, and a
zero standing in for an unknown reads as good news. A ``fraction`` field reads
``<name>`` out of the fold together with the total key its third token names --
``open:fraction:calls`` -- and renders ``N/M``, or the words "not said" when either side
is missing.

Every template also gets ``contract_version``, ``captured_at`` and the three judgment
fields (``lede``, ``you``, ``notes``) without asking, because every card carries them:
the version so a reader can branch on the shape it was handed, the stamp so a reader
knows how old the page is, and the judgments because they are the only values a
publisher may write at run time.
"""

from __future__ import annotations

import argparse
import json
import keyword
import os
import re
import stat
import sys
from pathlib import Path
from typing import NamedTuple

#: Kinds the ``--fields`` grammar accepts, mapped to the annotation they emit.
#:
#: There is deliberately no bare ``str``. Every author-declared field is read out of the
#: fold by ``read_text``, which returns ``str | Unsaid`` because a fold value can always
#: be absent; a ``str`` kind emitted ``<name>: str`` over that line and the generated
#: contract failed mypy. Plain ``str`` is reserved for the fields the script supplies
#: itself in ``_ALWAYS`` -- the provider writes those, it does not read them.
#:
#: There is deliberately no bare ``int``. A count reaching the page as a number has to
#: answer two questions this script cannot answer for it -- what its total is, and what
#: to render when the fold does not carry it -- and both wrong answers are silent: a
#: bare "7" reads as complete information, and a zero standing in for an unknown reads
#: as good news. ``fraction`` answers both at once: it is ``N/M`` when both sides are
#: known and :data:`UNSAID` otherwise, which reaches the page as words.
#:
#: A hand-written contract MAY still declare an ``int`` where a template genuinely
#: wants the pair rendered apart; the denominator gate then requires its ``<name>_of``
#: sibling. That escape hatch is checked, not emitted.
KINDS: dict[str, str] = {
    "str|unsaid": "str | Unsaid",
    "fraction": "str | Unsaid",
}

#: The one kind an author cannot declare: always-present text the PROVIDER writes rather
#: than reads. Only ``_ALWAYS`` uses it, so it lives outside the author-facing grammar
#: and ``parse_fields`` refuses it in ``--fields``.
_SUPPLIED: str = "str"

#: Every kind a :class:`Field` may carry, and the annotation each emits.
_ANNOTATIONS: dict[str, str] = {**KINDS, _SUPPLIED: "str"}

#: The three fields a publisher may write at run time, and nothing else. Numbers are
#: absent on purpose: a publisher that can type a count can make the page disagree
#: with the log it claims to summarise.
JUDGMENT_FIELDS: tuple[str, ...] = ("lede", "you", "notes")

#: Fields every template carries whether or not the author asks for them.
_ALWAYS: tuple[tuple[str, str], ...] = (
    ("contract_version", _SUPPLIED),
    ("captured_at", _SUPPLIED),
    ("lede", "str|unsaid"),
    ("you", "str|unsaid"),
    ("notes", "str|unsaid"),
)

#: Fields one card may carry; the host refuses an over-cap card WHOLE. Restates
#: ``kiro_crew.dashboard_templates.MAX_CARD_FIELDS`` rather than importing it: this
#: script ships with the skill to every install and runs against a checkout whose
#: package may not be importable. ``test_dashboard_templates`` asserts the two agree.
MAX_CARD_FIELDS: int = 24

_SLUG = re.compile(r"[a-z][a-z0-9_]{1,40}\Z")
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,40}\Z")

#: The generated catalogue beside this script's own skill. READ, never restated: a
#: fold list written out here is right only on the day it is written, and a fold the
#: product adds or renames leaves it silently wrong -- which is worse than absent,
#: because it then refuses a fold that exists and accepts one that does not.
CATALOGUE_PATH = Path(__file__).resolve().parent.parent / "folds.json"


def folds() -> tuple[str, ...]:
    """Every fold a template may draw from, from the generated catalogue.

    Raises :class:`ScaffoldError` rather than falling back to a built-in list. A
    fallback would be the restated list this reads the catalogue to avoid, and it would
    be reached exactly when the catalogue is missing -- the one case where a guess is
    least likely to be right.
    """
    try:
        document = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
        names = [str(fold["name"]) for fold in document["folds"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ScaffoldError(
            f"cannot read the fold catalogue at {CATALOGUE_PATH} ({exc}); regenerate it "
            "with: python3 scripts/fold_catalogue.py --write"
        ) from None
    if not names:
        raise ScaffoldError(f"{CATALOGUE_PATH} lists no folds; regenerate it")
    return tuple(names)


def counting_fields(fold: str) -> tuple[str, ...]:
    """The ``int`` fields of *fold*, which are the only ones a fraction can read.

    Read from the same generated catalogue as :func:`folds`, for the same reason: a
    denominator named here and absent there renders the words "not said" at every
    render, and nothing in the emitted gates notices because they build the card from an
    EMPTY view, where "not said" is the expected answer.
    """
    return _fold_fields(fold, "int")


def text_fields(fold: str) -> tuple[str, ...]:
    """The fields of *fold* a text field may read.

    The same reader and the same reason as :func:`counting_fields`. A text source named
    here and absent there renders the words "not said" at every render, and the emitted
    gates do not notice: they build the card from an EMPTY view, where "not said" IS the
    expected answer.
    """
    return _fold_fields(fold, "str")


#: What the catalogue writes for an OPTIONAL field's type. Its real type is not knowable
#: from an empty fold and the catalogue refuses to guess one, so this is not "no type" --
#: it is "a type this reader must not assume is wrong".
_UNKNOWN_TYPE = "unknown"


def _fold_fields(fold: str, wanted: str) -> tuple[str, ...]:
    """The names of *fold*'s fields a *wanted*-kind field may read.

    Two types answer yes, and the second is the one that matters. A row typed exactly
    *wanted* is plainly readable. A row typed ``"unknown"`` is ALSO accepted, because that
    is what the catalogue writes for every OPTIONAL field -- its type is not knowable from
    an empty fold, and the catalogue refuses to guess one. Filtering on *wanted* alone
    therefore sees only the REQUIRED fields and refuses the optional ones, which are
    exactly the fields a dashboard wants: an optional field is the one that can be absent,
    which is what ``Unsaid`` is for.

    So this menu is not "fields of this type". It is "names this fold has, minus the ones
    whose type is known and wrong". Read what that does NOT buy, because it is the whole
    limit of this check: an ``"unknown"`` row whose real type IS wrong is accepted here.
    ``status.turn`` is optional and the projection writes it as a dict, so
    ``turn:str|unsaid`` is admitted and the card renders the words "not said" for every
    active turn.

    Nothing in this script or in the emitted gates can catch that. The type is not
    recoverable: the catalogue derives it from a rendered fold, where an absent optional
    field is ``None`` and nothing else, and looking the name up in the entry-type registry
    was tried and answered ``status.previous`` with ``dict`` and ``status.turn`` with
    ``int`` -- which is why the catalogue writes ``unknown`` rather than a guess. The
    emitted gates build the card from an EMPTY view, where "not said" is the expected
    answer, so they agree with the wrong render.

    What is left is to say so, at the moment the choice is made, to the person making it:
    :func:`parse_fields` prints a line naming every ``unknown``-typed source it admits.
    A reader deciding whether this check is strong enough should assume it is not, for
    optional rows, and read the note.
    """
    rows = _catalogue_rows(fold)
    return tuple(
        sorted(name for name, declared in rows if declared == wanted or declared == _UNKNOWN_TYPE)
    )


def unchecked_sources(fold: str) -> frozenset[str]:
    """The *fold* rows admitted to a menu whose type the catalogue could not determine.

    Separate from :func:`_fold_fields` so the caller can tell "this name is allowed" from
    "this name is allowed and nobody checked it", which are different facts and only the
    second one is worth interrupting an author about.
    """
    return frozenset(name for name, declared in _catalogue_rows(fold) if declared == _UNKNOWN_TYPE)


def _catalogue_rows(fold: str) -> tuple[tuple[str, str], ...]:
    """Every ``(name, declared type)`` pair the catalogue holds for *fold*."""
    try:
        document = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
        for entry in document["folds"]:
            if str(entry["name"]) != fold:
                continue
            return tuple((str(field["name"]), str(field.get("type"))) for field in entry["fields"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ScaffoldError(
            f"cannot read the fold catalogue at {CATALOGUE_PATH} ({exc}); regenerate it "
            "with: python3 scripts/fold_catalogue.py --write"
        ) from None
    raise ScaffoldError(f"{fold!r} is not in {CATALOGUE_PATH}")


def _totals_hint(totals: tuple[str, ...] | None) -> str:
    """The fold's counting fields, named in the error rather than left to a guess."""
    if not totals:
        return ""
    return "; this fold counts: " + ", ".join(totals)


class ScaffoldError(Exception):
    """A field list, slug or fold this script will not emit files for."""


class Field(NamedTuple):
    """One field of the list, as one row of all four files.

    A :class:`~typing.NamedTuple` rather than a dataclass, deliberately.
    ``@dataclass`` resolves each annotation against ``sys.modules[cls.__module__]``
    while the class is being built, and a test that loads this script WITHOUT
    registering it there -- which the repository's own loader does on purpose, so two
    tests can load one script under different names -- then gets ``None`` back and the
    import dies before any assertion runs. The failure names ``dataclasses`` and looks
    nothing like its cause.
    """

    name: str
    kind: str
    #: For a ``fraction``, the fold key holding its total. Named by the author and
    #: checked against the fold, never derived from the field name: a derived
    #: ``<name>_of`` matches no key in any of the ten folds, so every fraction resolved
    #: its total to "not said" at every render while the emitted gates stayed green --
    #: they build the card from an EMPTY view, where "not said" IS the expected answer.
    denominator: str | None = None
    #: For a ``fraction``, the fold key its NUMERATOR reads. Defaults to the card
    #: field's own name and is checked against the fold the same way the total is,
    #: so a typo cannot ship a card that reads "not said" at every render.
    numerator: str | None = None

    @property
    def annotation(self) -> str:
        return _ANNOTATIONS[self.kind]

    @property
    def reads_a_denominator(self) -> bool:
        """Whether the provider reads a second value out of the FOLD for it.

        A fraction's total comes from the fold beside its numerator. It is not a card
        field of its own, because the page renders one string.
        """
        return self.denominator is not None


def camel(slug: str) -> str:
    """``work_board`` -> ``WorkBoard``. The contract and judgment type names."""
    return "".join(part.capitalize() for part in slug.split("_"))


def parse_fields(
    raw: list[str],
    *,
    totals: tuple[str, ...] | None = None,
    texts: tuple[str, ...] | None = None,
    unchecked: frozenset[str] = frozenset(),
) -> list[Field]:
    """*raw* tokens as fields, duplicates and unknown kinds refused.

    A ``fraction`` carries a third token naming the fold key that holds its total --
    ``open:fraction:calls`` -- and that name is checked against *totals*, the ``int``
    fields of the fold being drawn from. Unchecked it was derived as ``<name>_of``, which
    matches nothing in any of the ten folds, so the total read as "not said" forever.

    *totals* is optional so a caller can parse syntax alone; the command always passes
    the fold's own list, because a name nobody checked is the defect this argument exists
    to prevent.
    """
    fields: list[Field] = []
    seen: dict[str, str] = {}

    def add(
        name: str,
        kind: str,
        *,
        origin: str,
        denominator: str | None = None,
        numerator: str | None = None,
    ) -> None:
        if not _FIELD.fullmatch(name):
            raise ScaffoldError(
                f"{name!r} is not a field name: lower-case, digits and underscore, "
                "starting with a letter"
            )
        # A field becomes an annotation in the contract's class body, so a Python
        # keyword there is a SyntaxError at import -- and 'class' is a name this
        # product would plausibly want, since one of the ten folds is called that.
        # Refused here, where the message can name the cause, rather than as a
        # traceback from a generated file nobody wrote by hand.
        # HARD keywords only. A soft keyword (``match``, ``case``, ``type``, ``_``) is
        # contextual and parses fine as an annotation target, and ``type`` is a field
        # name a template would plausibly want -- refusing it would be this script
        # inventing a restriction the language does not have.
        if keyword.iskeyword(name):
            raise ScaffoldError(
                f"{name!r} is a Python keyword, so the generated contract would not "
                f"import; name the field something else (e.g. {name}_name)"
            )
        if name in seen:
            raise ScaffoldError(f"{name!r} is declared twice ({seen[name]} and {origin})")
        seen[name] = origin
        fields.append(Field(name, kind, denominator, numerator))

    for token in raw:
        name, sep, rest = token.partition(":")
        if not sep:
            raise ScaffoldError(f"{token!r} is not 'name:kind'; kinds: {sorted(KINDS)}")
        kind, _, tail = rest.partition(":")
        total, _, source = tail.partition(":")
        if kind not in KINDS:
            raise ScaffoldError(f"{token!r} has unknown kind {kind!r}; kinds: {sorted(KINDS)}")
        if kind == "fraction":
            if not total:
                raise ScaffoldError(
                    f"{token!r} needs the fold key holding its total: "
                    f"'{name}:fraction:<total>'. A fraction renders N/M, so the total is "
                    f"a second value read from the fold" + _totals_hint(totals)
                )
            if not _FIELD.fullmatch(total):
                raise ScaffoldError(
                    f"{total!r} is not a field name: lower-case, digits and underscore, "
                    "starting with a letter"
                )
            if totals is not None and total not in totals:
                raise ScaffoldError(
                    f"{total!r} is not a counting field of this fold, so its total would "
                    f"read as not said at every render" + _totals_hint(totals)
                )
            # The NUMERATOR is read from the fold too, and by default under the card
            # field's own name. Unchecked, one typo emits a read of a key no fold has and
            # the card shows "not said" at every render -- the same defect the total had,
            # on the other half of the same ratio.
            numerator = source or name
            if source and not _FIELD.fullmatch(source):
                raise ScaffoldError(
                    f"{source!r} is not a field name: lower-case, digits and underscore, "
                    "starting with a letter"
                )
            if totals is not None and numerator not in totals:
                hint = _totals_hint(totals)
                if not source:
                    hint += (
                        f". If the card field is deliberately named something else, name the "
                        f"fold key it reads: '{name}:fraction:{total}:<source>'"
                    )
                raise ScaffoldError(
                    f"{numerator!r} is not a counting field of this fold, so the numerator "
                    f"would read as not said at every render" + hint
                )
        else:
            if source:
                raise ScaffoldError(
                    f"{token!r} has extra tokens; text takes at most one, the fold key it "
                    f"reads: '{name}:{kind}:<source>'"
                )
            # ``total`` holds the single optional token for a non-fraction kind: the fold
            # key this field reads, for a card field whose name differs from it.
            numerator = total or name
            if total and not _FIELD.fullmatch(total):
                raise ScaffoldError(
                    f"{total!r} is not a field name: lower-case, digits and underscore, "
                    f"starting with a letter"
                )
            if any(name == always for always, _ in _ALWAYS):
                # An always-present field reads NOTHING out of the fold: the provider
                # writes it, from the judgment or from the clock. Checking it against the
                # fold's text menu refuses a declaration the loop below already handles by
                # dropping the author's kind and saying so -- and it refuses it on grounds
                # that are not true of it, since no fold is ever asked for this name.
                pass
            elif numerator in unchecked:
                # Admitted, and SAID. The catalogue could not type this row, so nothing
                # here knows whether the fold writes text at that name -- and if it does
                # not, the card renders the words "not said" at every render with every
                # gate green. Printed rather than refused: refusing would make every
                # OPTIONAL field undeclarable, and an optional field is precisely the one
                # a dashboard wants, because absence is what `Unsaid` renders.
                print(
                    f"note: {numerator!r} is optional in this fold, so the catalogue could "
                    f"not type it; confirm the fold writes TEXT there, or the card renders "
                    f"'not said' at every render and no gate will say so",
                    file=sys.stderr,
                )
            elif texts is not None and numerator not in texts:
                # The SAME check the fraction halves already get, on the half that did not
                # have it. `goal` mistyped as `goall` emits `read_text(view, "goall")`, the
                # card renders "not said" at every render, and the emitted gates agree with
                # it because they build from an empty view where that is the right answer.
                hint = f"; this fold's text fields: {', '.join(texts) or '(none)'}"
                if not total:
                    hint += (
                        f". If the card field is named differently from the fold key, name "
                        f"the key: '{name}:{kind}:<source>'"
                    )
                raise ScaffoldError(
                    f"{numerator!r} is not a text field of this fold, so it would read as "
                    f"not said at every render" + hint
                )
        if kind == "fraction":
            # `name:fraction:<total>[:<source>]` -- two fold keys, the total and the
            # numerator, and the numerator defaults to the card field's own name.
            add(name, kind, origin="--fields", denominator=total, numerator=source or name)
        else:
            # `name:<kind>[:<source>]` -- ONE fold key, and it arrives in the same position
            # a fraction's total does. It belongs in `numerator`, the slot that means "the
            # key this field reads"; putting it in `denominator` would emit a fraction line
            # for a text field.
            add(name, kind, origin="--fields", numerator=total or name)

    for name, kind in _ALWAYS:
        if name in seen:
            # Re-declaring one of these is not an error to correct: the author asked
            # for a field the template already has, and the kind this script uses is
            # the one the gates expect, so theirs is dropped and said so. Dropped for
            # real: the provider assigns ``gate_text(...)`` (``str | Unsaid``) to a
            # judgment field, so an author's ``lede:str`` left standing is a mypy
            # failure in the contract on a line the author did not write.
            print(f"note: {name} is always present; ignoring the declared kind", file=sys.stderr)
            index = next(i for i, field in enumerate(fields) if field.name == name)
            fields[index] = Field(name, kind)  # an always-present field reads no total
            continue
        add(name, kind, origin="always present")

    if len(fields) > MAX_CARD_FIELDS:
        raise ScaffoldError(
            f"{len(fields)} fields exceeds the host's cap of {MAX_CARD_FIELDS} for one card; "
            "the host refuses an over-cap card whole, so the page would not appear"
        )
    return fields


# --------------------------------------------------------------------------
# the four files
# --------------------------------------------------------------------------


def _row(field: Field) -> str:
    """One row of the page: the field's name as a label, its value bound."""
    return (
        f'    <div class="row"><span class="k">{field.name.replace("_", " ")}</span>'
        f'<span class="v" data-dashboard-field="{field.name}"></span></div>'
    )


def render_html(slug: str, fields: list[Field]) -> str:
    """The page. Inert layout, one binding per field, no controls.

    No script, no form, no image, no outbound link: the host strips every one of them,
    so authoring them ships a layout with holes and an author who believes a button
    exists. The page states facts; a decision goes through the product's own question
    and approval surfaces, which live outside this frame and carry the identity of the
    session that owns them.
    """
    rows = "\n".join(_row(field) for field in fields)
    return f"""<!doctype html>
<html lang="en">
<head><title>{slug.replace("_", " ")}</title>
<style>
  .wrap {{ display: flex; flex-direction: column; gap: 6px; }}
  .row {{ display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }}
  .k {{ color: var(--muted, #888); font-size: 12px; }}
  .v {{ font-variant-numeric: tabular-nums; }}
</style>
</head>
<body>
  <div class="wrap">
{rows}
  </div>
</body>
</html>
"""


def render_contract(slug: str, fields: list[Field]) -> str:
    name = camel(slug)
    lines = "\n".join(f"    {f.name}: {f.annotation}" for f in fields)
    judgments = "\n".join(f"    {f}: str | Unsaid" for f in JUDGMENT_FIELDS)
    empty = "\n".join(f'    "{f}": UNSAID,' for f in JUDGMENT_FIELDS)
    return f'''"""The {slug} template's contract: what the page reads, and who may write it.

Every key here is REQUIRED. A key that may be unknown is typed ``str | Unsaid`` and
the provider writes the sentinel out, so mypy refuses both the missing key and a
``None`` from a lookup that found nothing. The host renders a key it was not given as
the empty string, which on a page of numbers is indistinguishable from a zero -- that
is the reading this shape exists to prevent.

:class:`{name}Judgment` is the publisher's whole surface. Every number is absent from
it on purpose: a publisher that can type a count can make the page disagree with the
log it claims to summarise.
"""

from __future__ import annotations

from typing import Final, TypedDict

from kiro_crew.dashboard_templates import UNSAID, Unsaid

__all__ = [
    "CONTRACT_VERSION",
    "EMPTY_JUDGMENT",
    "{name}Card",
    "{name}Judgment",
]


class {name}Card(TypedDict):
    """THE contract: every field ``{slug}.html`` binds, and nothing else.

    Asserted equal to the page's own bindings by
    ``test_dashboard_template_{slug}``, because mypy cannot see inside html.
    """

{lines}


class {name}Judgment(TypedDict):
    """What a publisher may write at run time. Sentences, never numbers."""

{judgments}


CONTRACT_VERSION: Final[int] = 1
"""Bumped when :class:`{name}Card` changes shape. One per contract type."""

EMPTY_JUDGMENT: Final[{name}Judgment] = {{
{empty}
}}
"""A publisher that said nothing. The page then reads as facts with no judgments."""
'''


def _derivation(field: Field) -> str:
    """The provider line for one field: how it is read out of the fold."""
    if field.name == "contract_version":
        return '        "contract_version": str(CONTRACT_VERSION),'
    if field.name == "captured_at":
        return '        "captured_at": captured_at,'
    if field.name in JUDGMENT_FIELDS:
        gate = "gate_action" if field.name == "you" else "gate_text"
        return f'        "{field.name}": {gate}(judgment["{field.name}"]),'
    if field.kind == "fraction":
        return (
            f'        "{field.name}": fraction('
            f'read_int(view, "{field.numerator or field.name}"), '
            f'read_int(view, "{field.denominator}")),'
        )
    return f'        "{field.name}": read_text(view, "{field.numerator or field.name}"),'


#: Every name a generated provider may import from the shared package. The emitted file
#: is scanned for each one; a name the file never mentions is not imported.
_SHARED_HELPERS: tuple[str, ...] = (
    "UNSAID",
    "CardData",
    "Unsaid",
    "card_data",
    "fraction",
    "gate_action",
    "gate_text",
    "read_int",
    "read_text",
)

#: Stands in for the import block while the provider is rendered, so the scan reads the
#: WHOLE file -- signatures included -- rather than the body fragment. A helper used only
#: in a return annotation is invisible to a body-only scan, which is the shape that
#: produced an undefined name in an emitted file.
_IMPORT_MARKER = "@@PACKAGE_IMPORTS@@"


def _package_imports(body: str) -> str:
    """The shared-package names *body* actually uses, as one import block.

    DERIVED from the rendered file rather than listed. A fixed import list is right for
    whichever field list it was written against and wrong for the others: a template of
    only ``fraction`` fields never calls ``read_text``, and the unused import fails
    ``flake8`` on a line its author did not write and cannot see a reason for. Reading
    the file is the only way the two cannot disagree.

    Ordered the way the repository's import sorter orders a from-import -- constants,
    then classes, then functions -- so the emitted file needs no tidying afterwards.
    """
    used = [name for name in _SHARED_HELPERS if re.search(rf"\b{name}\b", body)]
    constants = sorted(name for name in used if name.isupper())
    classes = sorted(name for name in used if name[:1].isupper() and not name.isupper())
    functions = sorted(name for name in used if name[:1].islower())
    ordered = constants + classes + functions
    if not ordered:  # pragma: no cover - a provider always calls at least one gate
        raise ScaffoldError("the emitted provider uses no shared helper; refusing to emit")
    return "\n".join(f"    {name}," for name in ordered)


def render_provider(slug: str, fold: str, fields: list[Field]) -> str:
    name = camel(slug)
    body = "\n".join(_derivation(f) for f in fields)
    package_imports = _IMPORT_MARKER
    rendered = f'''"""``build_{slug}``: the one place the {fold} fold and the contract meet.

The signature IS the agreement. A fold's rendered value in, a
:class:`~kiro_crew.dashboard_templates.{slug}_contract.{name}Card` out, and mypy checks
both ends -- a missing key, a misspelled key or a wrong type is a build failure rather
than an empty cell on somebody's page.

The host values arrive as keyword arguments because they belong to neither input: the
fold does not know when it was read, and the publisher must not decide what stamp its
own page carries.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kiro_crew.dashboard_templates import (
{package_imports}
)
from kiro_crew.dashboard_templates.{slug}_contract import (
    CONTRACT_VERSION,
    {name}Card,
    {name}Judgment,
)

#: The projection the derived values come from.
SOURCE_FOLD = "{fold}"

__all__ = ["SOURCE_FOLD", "build_{slug}", "{slug}_card_data"]


def build_{slug}(
    view: Mapping[str, Any],
    judgment: {name}Judgment,
    *,
    captured_at: str,
) -> {name}Card:
    """One folded ``{fold}`` value plus one publisher judgment, as the page's contract."""
    return {{
{body}
    }}


def {slug}_card_data(card: {name}Card) -> CardData:
    """*card* as the card's ``data`` half: text only, every gap named in words."""
    return card_data(card)
'''
    # The marker line is removed before the scan so the import block cannot count as a
    # use of the names it declares -- that would make every candidate look used and the
    # derivation a no-op.
    scanned = rendered.replace(_IMPORT_MARKER, "")
    return rendered.replace(_IMPORT_MARKER, _package_imports(scanned))


def render_test(slug: str, fold: str) -> str:
    name = camel(slug)
    return f'''"""The {slug} template's page and contract read the same field set.

``mypy`` checks ``build_{slug}``: a fold value in, a :class:`{name}Card` out. It cannot
see inside ``{slug}.html``, so the html end needs this gate.

Scope, stated honestly. This asserts a field EXISTS on both sides, that the page
offers no controls, and that the provider's output survives the host's own caps. It
does not assert a value is RIGHT, or that the page puts it somewhere a person will
look. That is the limit of a text gate: it makes an omission visible, and a reviewer
makes it answered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kiro_crew.dashboard_templates as templates
from kiro_crew.dashboard.dynamic_cards import normalize_card
from kiro_crew.dashboard_templates import NOT_SAID, UNSAID, card_data
from kiro_crew.dashboard_templates.parity import (
    contract_keys,
    control_tags_used,
    denominator_gaps,
    dropped_text_attributes,
    html_fields,
    outbound_references,
)
from kiro_crew.dashboard_templates.{slug}_contract import (
    CONTRACT_VERSION,
    EMPTY_JUDGMENT,
    {name}Card,
)
from kiro_crew.dashboard_templates.{slug}_provider import SOURCE_FOLD, build_{slug}

PAGE = Path(templates.__file__).resolve().parent / "{slug}.html"


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_the_extractor_reads_this_page() -> None:
    """Precondition. An extractor that found nothing would read as an over-specified
    contract, which sends the reader to delete the contract instead of fixing the page."""
    assert html_fields(_page()), "extracted no bindings from {slug}.html"


def test_the_page_reads_exactly_what_the_contract_declares() -> None:
    """THE assertion: the html end of the agreement, which mypy cannot see.

    Strict equality, no exemption list. An exemption list is a hiding place -- a field
    inconvenient to render gets declared and listed, and every check still passes."""
    read = html_fields(_page())
    declared = contract_keys({name}Card)
    assert read - declared == set(), f"page binds undeclared fields: {{sorted(read - declared)}}"
    assert declared - read == set(), f"contract declares unread fields: {{sorted(declared - read)}}"


def test_an_undeclared_binding_is_caught() -> None:
    """Direction one, planted: the gate must observe a real mismatch."""
    seeded = _page().replace(
        "</div>\\n  </div>", '<span data-dashboard-field="bogus"></span></div>\\n  </div>', 1
    )
    assert seeded != _page(), "failed to plant the binding -- the anchor moved"
    assert html_fields(seeded) - contract_keys({name}Card) == {{"bogus"}}


def test_the_page_offers_no_controls() -> None:
    """A dashboard states facts. The host strips a control outright, so authoring one
    ships a layout with a hole in it and an author who believes the button exists."""
    assert control_tags_used(_page()) == set()


def test_the_page_reaches_nothing_outside_itself() -> None:
    """No ``src``, no ``srcset``, and no ``href`` that is not a same-document fragment. The
    host strips them like a control, so the author would believe in an image or a link that
    is a hole."""
    assert outbound_references(_page()) == set()


def test_the_page_states_nothing_where_the_host_deletes_it() -> None:
    """An ``alt``, ``title``, ``start`` or ``value`` is removed from a card, so a fact put
    there is a fact deleted before any reader sees it."""
    assert dropped_text_attributes(_page()) == set()


def test_every_count_carries_its_denominator() -> None:
    assert denominator_gaps({name}Card) == []


def test_the_host_accepts_this_page_and_its_data_together() -> None:
    """The one gate that covers what no rule here restates.

    Every other check reasons about the page or the contract alone. This hands the real
    pair to the real normalizer, which knows the whole answer at once: html bytes, data
    bytes, field count and every field name. A page that grows past the html cap passes
    every other gate in this file and is dropped WHOLE by the host, so the card stops
    appearing with nothing red anywhere.
    """
    card = build_{slug}({{}}, EMPTY_JUDGMENT, captured_at="1970-01-01T00:00:00Z")
    accepted = normalize_card({{"html": _page(), "data": card_data(card)}})
    assert accepted is not None, "the host refuses this card, so it would render nothing"


def test_an_unsupplied_value_says_so_rather_than_reading_as_zero() -> None:
    """Invariant 3. The host renders a key it was not given as the empty string, so a
    gap must arrive as words instead."""
    card = build_{slug}({{}}, EMPTY_JUDGMENT, captured_at="1970-01-01T00:00:00Z")
    data = card_data(card)
    assert data.keys() == contract_keys({name}Card)
    assert data["lede"] == NOT_SAID
    assert "" not in set(data.values()), f"a blank value reads as a zero: {{data}}"


def test_the_card_fits_the_hosts_caps() -> None:
    """An over-cap card is refused WHOLE by the host, so the page would simply not
    appear -- with nothing red anywhere. Fail here instead."""
    card = build_{slug}({{}}, EMPTY_JUDGMENT, captured_at="1970-01-01T00:00:00Z")
    assert card_data(card)


def test_a_name_in_the_action_field_is_refused() -> None:
    """The field says what a person must DO. A live board once rendered an owner's
    name and a session id there."""
    judgment = dict(EMPTY_JUDGMENT)
    judgment["you"] = "Raymond"
    card = build_{slug}({{}}, judgment, captured_at="1970-01-01T00:00:00Z")  # type: ignore[arg-type]
    assert card["you"] is UNSAID
    judgment["you"] = "fix it"
    card = build_{slug}({{}}, judgment, captured_at="1970-01-01T00:00:00Z")  # type: ignore[arg-type]
    assert card["you"] == "fix it", "a short real action must survive the gate"


def test_the_contract_version_is_a_positive_integer() -> None:
    assert isinstance(CONTRACT_VERSION, int) and CONTRACT_VERSION >= 1


def test_the_source_fold_is_one_the_product_keeps() -> None:
    from kiro_crew.crew_log.projection import FOLD_NAMES

    assert SOURCE_FOLD in FOLD_NAMES, (
        f"{{SOURCE_FOLD!r}} is not a fold this product keeps: {{sorted(FOLD_NAMES)}}. "
        "A template whose numbers have no fold behind them has numbers somebody typed."
    )


@pytest.mark.parametrize("field", sorted(contract_keys({name}Card)))
def test_every_field_name_is_one_the_host_will_bind(field: str) -> None:
    assert templates.field_name_ok(field), f"{{field!r}} is not a binding the host accepts"
'''


# --------------------------------------------------------------------------
# writing them out
# --------------------------------------------------------------------------


def _checkout_above(start: Path) -> Path | None:
    """The nearest ancestor of ``start`` (inclusive) holding ``src/kiro_crew``."""
    for candidate in (start, *start.parents):
        if (candidate / "src" / "kiro_crew").is_dir():
            return candidate
    return None


def repo_root() -> Path:
    """The checkout to write into: the working directory's, then this script's.

    The working directory comes FIRST because this script ships as a built-in skill and
    is normally run from its installed copy, whose ancestors are the skills directory and
    hold no checkout at all. Keying only on ``__file__`` made the command the skill
    documents fail for its own intended reader.

    Falling back to the script's own ancestors keeps a checkout-local copy working from
    any directory. Neither path guesses: without ``src/kiro_crew`` above one of them this
    raises, rather than writing four files into a tree with nowhere to put them.
    """
    found = _checkout_above(Path.cwd().resolve())
    if found is not None:
        return found
    here = Path(__file__).resolve()
    found = _checkout_above(here.parent)
    if found is not None:
        return found
    raise ScaffoldError(
        f"no checkout with src/kiro_crew at or above the working directory "
        f"({Path.cwd()}) or this script ({here}); run it from inside a checkout, "
        f"or pass --out-root"
    )


def emit(slug: str, fold: str, fields: list[Field], out_root: Path) -> dict[Path, str]:
    """The four files' paths and contents. Pure, so a test can read them unwritten."""
    pkg = out_root / "src" / "kiro_crew" / "dashboard_templates"
    return {
        pkg / f"{slug}.html": render_html(slug, fields),
        pkg / f"{slug}_contract.py": render_contract(slug, fields),
        pkg / f"{slug}_provider.py": render_provider(slug, fold, fields),
        out_root / "test" / f"test_dashboard_template_{slug}.py": render_test(slug, fold),
    }


def is_symlinked(root: Path, path: Path) -> bool:
    """Is any component of ``path`` below ``root`` a symlink?

    ``Path.write_text`` follows the final component and ``mkdir(parents=True)`` accepts a
    symlinked directory, so a link planted at a derived path in the checkout being written
    into sends the write outside it -- and ``--force`` then truncates whatever it found,
    while the line this script prints still names the path inside the tree.

    Every component is asked, not just the leaf, because a link one directory up moves the
    write just as effectively. ``lstat`` is used rather than a directory descriptor walk:
    ``os.open`` on a directory is refused on Windows, which this repo supports, so a
    descriptor chain here would mean the scaffold could not write at all there.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    walked = root
    for part in relative.parts:
        walked = walked / part
        try:
            if _is_link_or_junction(walked):
                return True
        except OSError:
            return False
    return False


def _is_link_or_junction(path: Path) -> bool:
    """Whether *path* is a symlink OR a Windows directory junction.

    ``S_ISLNK`` alone is not the question. CPython sets ``S_IFLNK`` only for
    ``IO_REPARSE_TAG_SYMLINK``, so a junction -- which an unprivileged Windows user can
    plant with ``mklink /J``, unlike a symlink -- keeps ``S_IFDIR`` and walks straight
    past a mode test. The write then lands wherever the junction points while this script
    prints the in-repo path, which is the one outcome no ``git checkout`` undoes.

    The reparse tag is read through ``getattr`` on both sides: neither the ``st_`` field
    nor the ``stat`` constant exists on POSIX, where the mode test is the whole answer.
    The repository's own ``platform_compat.is_link_or_junction`` asks the same question,
    and is deliberately not imported -- this script ships with the skill and runs against
    a checkout whose package may not be importable.
    """
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        return True
    mount_point = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    if mount_point is None:
        return False
    return bool(getattr(info, "st_reparse_tag", 0) == mount_point)


def _write_new(path: Path, text: str) -> None:
    """Create ``path`` and write ``text``, refusing to follow a link at the final name.

    ``O_EXCL`` is what makes this safe rather than the flag beside it: the file must not
    exist, so there is nothing to follow. ``O_NOFOLLOW`` is added where the platform has
    it as a second answer for the case where something appears between the check and here,
    and is read through ``getattr`` because Windows has no such flag.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o644)
    except OSError as exc:
        raise ScaffoldError(f"cannot write {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except BaseException as exc:
        # Removed HERE, by the call that created it. Once ``os.open`` returns, the file
        # exists at full size zero, and a quota or full disk reported at flush-on-close
        # leaves it there with part of the text in it. A caller that records the path only
        # after this function returns never learns the file was created, so its own sweep
        # walks past the fragment -- and the next run then reports "exists; pass --force"
        # about a file nothing wrote on purpose.
        #
        # ``BaseException``, because an interrupt during the write creates the same
        # fragment as an ``ENOSPC`` does, and the unlink is the same answer to both.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise ScaffoldError(f"cannot write {path}: {exc}") from exc
        raise


def _publish_new(files: dict[Path, str]) -> None:
    """Create every output where it belongs, refusing any that already exists.

    The path that overwrites NOTHING needs no staging: it has nothing to preserve, so
    ``O_EXCL`` at the final name is both the existence check and the write. Checking first
    and renaming after leaves a window between them -- two runs for one slug both see the
    target absent, both stage, and the second replace silently overwrites what the first
    just created. ``O_EXCL`` closes that by construction, on every platform, with the one
    primitive this file already uses.

    A failure part-way removes only what THIS call created, which is every output it has
    written so far: nothing else can be at stake, because an output that already existed is
    what stopped the run.
    """
    created: list[Path] = []
    try:
        for path, text in files.items():
            try:
                _write_new(path, text)
            except ScaffoldError as exc:
                if path.exists():
                    raise ScaffoldError(f"{path} exists; pass --force to overwrite") from None
                raise exc
            created.append(path)
    except BaseException:
        for done in created:
            try:
                done.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _write_over(path: Path, text: str) -> None:
    """Replace ``path``'s contents with *text*, refusing to follow a link at the final name.

    ``O_NOFOLLOW`` where the platform has it, read through ``getattr`` because Windows has
    none: :func:`is_symlinked` has already asked about every component, and this closes the
    window between that answer and this call.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError as exc:
        raise ScaffoldError(f"cannot write {path}: {exc}") from exc


def publish(root: Path, files: dict[Path, str], *, force: bool) -> None:
    """Write the four outputs, refusing to write outside *root*.

    This deliberately does NOT make the four writes atomic, and an earlier version of it
    did: outputs were staged in a directory this run owned, copies of the targets were kept
    beside them, and a failed move was undone from those copies. That machinery was removed
    rather than repaired.

    What it defended against was a failure part-way leaving an html and a contract from one
    run beside a provider and a test from another. The reason it is not worth defending here
    is where these files live: a checkout, under version control, on their way to review. A
    mixed set is named by ``git status`` and undone by ``git checkout --`` or by deleting the
    new files, and nothing reaches anyone until a human reads the diff.

    ``--force`` is the case that settles it. Overwriting is what the flag is for, so a run
    that SUCCEEDS already destroys every uncommitted edit to all four outputs. Machinery
    that preserves some of them when the third write fails is protecting a fraction of what
    the flag destroys by design -- while being, itself, the most intricate code in this file.

    Two properties are kept, because git supplies neither.

    :func:`is_symlinked` stays: a link along the path sends the write OUTSIDE the tree this
    root names, which is not a mixed set but a write somewhere else entirely -- no
    ``git checkout`` in this repository undoes it, and the line this script prints still
    names the path inside the tree.

    ``O_EXCL`` on the path that overwrites nothing stays, in :func:`_publish_new`: two runs
    for one slug can otherwise both find the target absent and the second silently replace
    what the first created. That is a race between two runs, not a torn write, and git does
    not arbitrate it.
    """
    for path in files:
        if is_symlinked(root, path):
            raise ScaffoldError(
                f"refusing to write {path}: a component of it is a symlink, so the write "
                f"would land somewhere this path does not name"
            )
    for path in files:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ScaffoldError(f"cannot create {path.parent}: {exc}") from exc
    if not force:
        _publish_new(files)
        return
    for path, text in files.items():
        _write_over(path, text)


def registration_line(slug: str, fold: str) -> str:
    """The registry row to paste into ``registry.py``."""
    name = camel(slug)
    return (
        f'    "{slug}": TemplateSpec(\n'
        f'        "{slug}", {name}Card, build_{slug}, "{fold}", {slug.upper()}_VERSION\n'
        f"    ),"
    )


def registration_imports(slug: str) -> str:
    """The imports that row needs, aliased so two templates cannot collide.

    Printed WITH the row rather than left to the reader. Every template's contract
    module exports a constant called ``CONTRACT_VERSION``, so an unaliased import makes
    the second template silently rebind the first one's version, and the row on its own
    names three symbols the registry has never imported -- pasting it alone is a
    ``NameError`` at import, which takes the whole package down rather than one
    template.
    """
    name = camel(slug)
    return (
        f"from kiro_crew.dashboard_templates.{slug}_contract import (\n"
        f"    CONTRACT_VERSION as {slug.upper()}_VERSION,\n"
        f"    {name}Card,\n"
        f")\n"
        f"from kiro_crew.dashboard_templates.{slug}_provider import build_{slug}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("slug", help="lower_snake identity of the template")
    # Resolved BEFORE argparse sees it, so an unreadable catalogue is its own named
    # error rather than an empty ``choices`` list that refuses every fold with argparse's
    # own wording -- which reads as "your fold is wrong" when the catalogue is.
    try:
        choices = folds()
    except ScaffoldError as exc:
        print(f"scaffold: {exc}", file=sys.stderr)
        return 2
    parser.add_argument("--fold", required=True, choices=choices, help="the projection behind it")
    parser.add_argument(
        "--fields",
        nargs="*",
        default=[],
        metavar="NAME:KIND",
        help=(
            "one token per fold-read value; kinds: "
            f"{', '.join(sorted(KINDS))}. A fraction takes the fold key holding its "
            "total: open:fraction:calls"
        ),
    )
    parser.add_argument("--out-root", type=Path, default=None, help="checkout to write into")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument("--print-only", action="store_true", help="write nothing; print paths")
    args = parser.parse_args(argv)

    try:
        if not _SLUG.fullmatch(args.slug):
            raise ScaffoldError(
                f"{args.slug!r} is not a slug: lower-case, digits and underscore, "
                "starting with a letter"
            )
        # Same reason as a field name, one level out: the slug becomes part of a module
        # name AND of ``build_<slug>``, so a keyword there is a SyntaxError in the
        # provider and an unimportable module beside it.
        if keyword.iskeyword(args.slug):
            raise ScaffoldError(
                f"{args.slug!r} is a Python keyword, so the generated module would not "
                "import; pick another slug"
            )
        fields = parse_fields(
            list(args.fields),
            totals=counting_fields(args.fold),
            texts=text_fields(args.fold),
            unchecked=unchecked_sources(args.fold),
        )
        root = args.out_root if args.out_root is not None else repo_root()
        files = emit(args.slug, args.fold, fields, root)
    except ScaffoldError as exc:
        print(f"scaffold: {exc}", file=sys.stderr)
        return 2

    if args.print_only:
        for path in files:
            print(path)
        return 0

    existing = [p for p in files if p.exists()]
    if existing and not args.force:
        for path in existing:
            print(f"scaffold: {path} exists; pass --force to overwrite", file=sys.stderr)
        return 2
    try:
        publish(root, files, force=args.force)
    except ScaffoldError as exc:
        print(f"scaffold: {exc}", file=sys.stderr)
        return 2
    # Printed only after every output is in place, so a printed path is one that landed.
    for path in files:
        print(path)
    print()
    print("Add these imports to src/kiro_crew/dashboard_templates/registry.py:")
    print(registration_imports(args.slug))
    print()
    print("and this row to its REGISTRY dict:")
    print(registration_line(args.slug, args.fold))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
