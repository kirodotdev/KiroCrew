"""The dashboard-template machinery: the three invariants, and the scaffold that holds them.

A template is an html page plus a ``TypedDict`` plus a provider. ``mypy`` checks the
provider's two ends; nothing checks the page, so a field the page reads and the contract
omits renders an empty cell and nobody is accountable for it. These tests are that
missing check, plus the two rules about what a number and a gap may look like.

Every gate here also carries a PLANTED failure. The registry ships empty -- the first
templates arrive with the work that needs them -- and a gate whose only evidence is
"nothing is wrong" would read the same way whether it worked or not. So each rule is
asserted against a deliberately broken input in the same run.

The tests of the two readers the parity check compares, ``html_fields`` for the page and
``contract_keys`` for the contract, are in ``test_dashboard_templates_readers.py``; the
pieces both files use live in ``dashboard_templates_helpers.py``.
"""

from __future__ import annotations

import ast
import configparser
import importlib
import inspect
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import types
import typing
from collections.abc import Iterator
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable, TypedDict
from unittest import mock

import pytest
from dashboard_templates_helpers import (
    PAGE,
    ROOT,
    _Card,
    _MaybeCountCard,
)
from skill_script_helpers import load_skill_script, no_bytecode

import kiro_crew.dashboard_templates.parity as parity
from kiro_crew.crew_log.projection import FOLD_NAMES
from kiro_crew.dashboard_templates import (
    MAX_CARD_DATA_BYTES,
    MAX_CARD_FIELDS,
    NOT_SAID,
    UNSAID,
    TemplateSpec,
    Unsaid,
    card_data,
    field_name_ok,
    fraction,
    read_int,
    read_text,
)
from kiro_crew.dashboard_templates.parity import (
    CONTROL_TAGS,
    contract_keys,
    control_tags_used,
    denominator_gaps,
    dropped_text_attributes,
    html_fields,
    outbound_references,
)
from kiro_crew.dashboard_templates.registry import REGISTRY, html_path, spec_for
from kiro_crew.subprocess_utf8 import UTF8_TEXT

PACKAGE = ROOT / "src" / "kiro_crew" / "dashboard_templates"
SCAFFOLD = (
    ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "dashboard-template"
    / "scripts"
    / "scaffold.py"
)


class _BareCountCard(TypedDict):
    failed: int


class _PairedCountCard(TypedDict):
    failed: int
    failed_of: int


@dataclass(frozen=True)
class _Emitted:
    """What one scaffolded template hands a case: its files, page, types and provider."""

    files: dict[Path, str]
    page: str
    card: type
    empty_judgment: dict[str, Any]
    build: Callable[..., dict[str, Any]]


# ---------------------------------------------------------------------------
# invariant 1 -- parity
# ---------------------------------------------------------------------------


class TestParity:
    def test_a_matching_pair_is_clean(self) -> None:
        assert html_fields(PAGE) == contract_keys(_Card)

    def test_a_field_the_page_reads_and_the_contract_omits_is_caught(self) -> None:
        seeded = PAGE.replace("</body>", '<span data-dashboard-field="bogus"></span></body>')
        assert html_fields(seeded) - contract_keys(_Card) == {"bogus"}

    def test_a_field_the_contract_declares_and_the_page_ignores_is_caught(self) -> None:
        stripped = PAGE.replace('<span data-dashboard-field="open"></span>', "")
        assert contract_keys(_Card) - html_fields(stripped) == {"open"}


# ---------------------------------------------------------------------------
# invariant 2 -- every number carries its denominator
# ---------------------------------------------------------------------------


class TestDenominators:
    def test_a_fraction_shaped_field_needs_nothing(self) -> None:
        assert denominator_gaps(_Card) == []

    def test_a_bare_count_is_caught(self) -> None:
        assert denominator_gaps(_BareCountCard) == ["failed"]

    def test_a_count_with_its_total_is_accepted(self) -> None:
        assert denominator_gaps(_PairedCountCard) == []

    def test_a_count_that_may_be_unknown_still_needs_its_total(self) -> None:
        """``int | Unsaid`` is still a number on the page when it is known."""
        assert denominator_gaps(_MaybeCountCard) == ["failed"]

    def test_a_denominator_that_is_not_a_number_does_not_count(self) -> None:
        """The sibling must be divisible by, not merely named correctly.

        A membership check on the NAME passes here while the page still has no total, so
        the count renders exactly as unsized as a bare one -- the rule's own failure
        mode, reached by satisfying its letter.
        """

        class _Card(TypedDict):
            contract_version: str
            captured_at: str
            failed: int
            failed_of: str

        assert denominator_gaps(_Card) == ["failed"]

    def test_a_denominator_that_may_be_unknown_still_counts(self) -> None:
        """``int | Unsaid`` on the total is honest, not missing: the provider says so when
        it knows, and the page reads "not said" when it does not."""

        class _Card(TypedDict):
            contract_version: str
            captured_at: str
            failed: int
            failed_of: int | Unsaid

        assert denominator_gaps(_Card) == []

    def test_the_fraction_helper_refuses_to_render_half_a_ratio(self) -> None:
        assert fraction(7, 12) == "7/12"
        assert fraction(7, UNSAID) is UNSAID
        assert fraction(UNSAID, 12) is UNSAID


# ---------------------------------------------------------------------------
# invariant 3 -- a value nobody supplied says so
# ---------------------------------------------------------------------------


class TestUnsaidReachesThePageAsWords:
    def test_the_sentinel_becomes_words(self) -> None:
        assert card_data({"lede": UNSAID})["lede"] == NOT_SAID

    def test_no_writer_can_spell_the_sentinel(self) -> None:
        """The sentinel is an enum member, so no text a fold or publisher supplies is
        mistaken for it. A string sentinel is forgeable in both directions: real content
        equal to the token would be rewritten as "not said", and a publisher could print
        what looks like a system marker onto a status page."""
        assert not isinstance(UNSAID, str)
        for impostor in ("__unsaid__", "unsaid", "UNSAID", "not said", str(UNSAID)):
            assert card_data({"lede": impostor})["lede"] == impostor
            assert read_text({"s": impostor}, "s") == impostor

    def test_no_value_reaches_the_page_blank(self) -> None:
        """The host renders a key it was NOT given as the empty string, so a blank is
        indistinguishable from an absent key -- and on a page of counts, from a zero."""
        data = card_data({"lede": UNSAID, "open": UNSAID})
        assert "" not in set(data.values())

    def test_a_reader_of_a_fold_never_invents_a_zero(self) -> None:
        assert read_int({}, "n") is UNSAID
        assert read_int({"n": "7"}, "n") is UNSAID
        assert read_int({"n": 0}, "n") == 0

    def test_a_flag_is_not_a_count(self) -> None:
        """``bool`` passes ``isinstance(x, int)``, so ``True`` would render as ``1``."""
        assert read_int({"n": True}, "n") is UNSAID

    def test_blank_text_is_a_gap_not_an_empty_sentence(self) -> None:
        assert read_text({"s": "   "}, "s") is UNSAID
        assert read_text({"s": " hi "}, "s") == "hi"
        assert read_text({}, "s") is UNSAID
        assert read_text({"s": 7}, "s") is UNSAID


# ---------------------------------------------------------------------------
# the host's caps, which a template cannot be allowed to exceed quietly
# ---------------------------------------------------------------------------


class TestTheHostsCaps:
    def test_too_many_fields_is_refused_here(self) -> None:
        """The host refuses an over-cap card WHOLE, so the page would stop appearing
        with nothing red anywhere. Fail at build time instead."""
        payload = {f"f{i}": "x" for i in range(MAX_CARD_FIELDS + 1)}
        with pytest.raises(ValueError, match="exceeds the host's cap"):
            card_data(payload)

    def test_too_many_bytes_is_refused_here(self) -> None:
        payload = {"a": "x" * (MAX_CARD_DATA_BYTES + 1)}
        with pytest.raises(ValueError, match="bytes of card data"):
            card_data(payload)

    def test_a_name_the_host_would_not_bind_is_refused(self) -> None:
        for bad in ("9lives", "has space", "", "x" * 49, "dotted.name"):
            assert not field_name_ok(bad), bad
            with pytest.raises(ValueError, match="not a field name"):
                card_data({bad: "x"})

    def test_a_name_the_host_binds_is_accepted(self) -> None:
        for good in ("lede", "open_of", "a-b", "A1"):
            assert field_name_ok(good), good


# ---------------------------------------------------------------------------
# no controls
# ---------------------------------------------------------------------------


class TestNoControls:
    def test_a_plain_page_has_none(self) -> None:
        assert control_tags_used(PAGE) == set()

    @pytest.mark.parametrize("tag", sorted(CONTROL_TAGS))
    def test_each_control_tag_is_observed(self, tag: str) -> None:
        """Every name in the set must actually be detectable. A name in the set that the
        reader cannot see makes the gate look wider than it is."""
        assert control_tags_used(f"<html><body><{tag}></{tag}></body></html>") == {tag}

    def test_a_style_block_is_not_a_control(self) -> None:
        """The host keeps ``style``; layout is the half a template is allowed to own."""
        assert control_tags_used("<html><head><style>p{color:red}</style></head></html>") == set()


#: The prose copies of the control-tag rule, and how to find the paragraph in each.
#: ``CONTROL_TAGS`` in ``parity.py`` is the authority; each of these restates it for a
#: reader, so each must name every tag or the next addition drifts silently.
_CONTROL_TAG_PROSE: dict[str, tuple[Path, str]] = {
    "skill": (SCAFFOLD.parents[1] / "SKILL.md", "## What the page may not contain"),
    "agent-spec": (SCAFFOLD.parents[1] / "agent-spec.md", "**Zero controls.**"),
    "system-spec": (
        ROOT / "docs" / "system-specs" / "modules" / "dashboard-templates.md",
        "## What a page may not contain",
    ),
}


def _control_tag_paragraph(path: Path, marker: str) -> str:
    """The prose from *marker* up to the next ``##`` heading."""
    text = path.read_text(encoding="utf-8")
    start = text.index(marker)
    end = text.find("\n## ", start + len(marker))
    return text[start : end if end != -1 else len(text)]


class TestTheProseNamesEveryControlTag:
    @pytest.mark.parametrize("copy", sorted(_CONTROL_TAG_PROSE))
    def test_each_copy_names_every_tag_in_the_code(self, copy: str) -> None:
        path, marker = _CONTROL_TAG_PROSE[copy]
        paragraph = _control_tag_paragraph(path, marker)
        assert "`form`" in paragraph, f"{copy}: found no tag list after {marker!r}"
        missing = sorted(tag for tag in CONTROL_TAGS if f"`{tag}`" not in paragraph)
        assert missing == [], f"{copy} ({path.name}) does not name {missing}"


# ---------------------------------------------------------------------------
# nothing outside the page
# ---------------------------------------------------------------------------


class TestNoOutboundReferences:
    """A page may not reach outside itself: no ``src`` and no ``href`` that is not a
    same-document fragment. Attributes, not tags, so a separate reader from the control
    gate -- "control tags" should not silently also mean attributes."""

    def test_a_plain_page_has_none(self) -> None:
        assert outbound_references(PAGE) == set()

    def test_a_src_is_refused(self) -> None:
        page = PAGE.replace("</body>", '<img src="chart.png" alt=""></body>')
        assert outbound_references(page) == {("src", "chart.png")}

    def test_an_external_href_is_refused(self) -> None:
        page = PAGE.replace("</body>", '<a href="https://example.com/x">x</a></body>')
        assert outbound_references(page) == {("href", "https://example.com/x")}

    def test_a_same_document_href_is_allowed(self) -> None:
        page = PAGE.replace("</body>", '<a href="#top">up</a></body>')
        assert outbound_references(page) == set()

    def test_an_svg_xlink_href_is_read_by_its_real_attribute_name(self) -> None:
        """``xlink:href`` reaches the parser as that literal attribute name, so a check
        on ``href`` alone never sees it. A same-document ``#icon`` stays allowed."""
        external = PAGE.replace(
            "</body>", '<svg><use xlink:href="https://cdn.example/i.svg#i"/></svg></body>'
        )
        assert outbound_references(external) == {("xlink:href", "https://cdn.example/i.svg#i")}
        local = PAGE.replace("</body>", '<svg><use xlink:href="#icon"/></svg></body>')
        assert outbound_references(local) == set()

    def test_an_upper_case_attribute_is_still_read(self) -> None:
        page = PAGE.replace("</body>", '<IMG SRC="chart.png"></body>')
        assert outbound_references(page) == {("src", "chart.png")}

    def test_a_fragment_href_padded_with_whitespace_is_refused(self) -> None:
        """The host tests the RAW value with `startsWith('#')`, without trimming.

        A leading space makes `` #top`` fail that test, so the host removes the attribute
        and the link is a hole. Trimming here would call it a same-document fragment and
        report nothing to fix.
        """
        page = PAGE.replace("</body>", '<a href=" #top">up</a></body>')
        assert outbound_references(page) == {("href", "#top")}

    def test_a_srcset_is_refused(self) -> None:
        """The host removes it beside ``src``, in the same branch. Missed here, a
        responsive image passes every gate and renders as nothing."""
        page = PAGE.replace("</body>", '<img srcset="chart-2x.png 2x"></body>')
        assert outbound_references(page) == {("srcset", "chart-2x.png 2x")}


class TestAttributesTheHostDeletes:
    """``alt``, ``title``, ``start`` and ``value`` reach nothing outside the page, and the
    host still removes them from a card so no fact arrives through a channel its own text
    scan cannot audit. Held apart from the outbound reader because the reason differs."""

    def test_a_plain_page_has_none(self) -> None:
        assert dropped_text_attributes(PAGE) == set()

    def test_an_alt_carrying_part_of_the_answer_is_refused(self) -> None:
        page = PAGE.replace("</body>", '<span alt="7 of 12 settled"></span></body>')
        assert dropped_text_attributes(page) == {("alt", "7 of 12 settled")}

    def test_a_hover_title_is_refused(self) -> None:
        """The most tempting one: a tooltip reads like extra context and is deleted."""
        page = PAGE.replace("</body>", '<span title="since 09:00">x</span></body>')
        assert dropped_text_attributes(page) == {("title", "since 09:00")}

    def test_a_list_marker_start_is_refused(self) -> None:
        page = PAGE.replace("</body>", '<ol start="7"><li>x</li></ol></body>')
        assert dropped_text_attributes(page) == {("start", "7")}

    def test_it_does_not_also_report_the_outbound_ones(self) -> None:
        """Each attribute belongs to exactly one reader, so neither gate's caller is
        surprised by what the other silently covers."""
        page = PAGE.replace("</body>", '<img src="c.png" alt="chart"></body>')
        assert dropped_text_attributes(page) == {("alt", "chart")}
        assert outbound_references(page) == {("src", "c.png")}


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


class TestTheRegistry:
    def test_every_registered_template_draws_from_a_real_fold(self) -> None:
        for slug, spec in REGISTRY.items():
            assert spec.fold in FOLD_NAMES, (
                f"{slug} draws from {spec.fold!r}, which is not a fold this product "
                "keeps; a template with no fold behind it has numbers somebody typed"
            )

    def test_every_registered_template_holds_its_own_invariants(self) -> None:
        for slug, spec in REGISTRY.items():
            page = html_path(slug).read_text(encoding="utf-8")
            assert html_fields(page) == contract_keys(spec.contract), slug
            assert control_tags_used(page) == set(), slug
            assert outbound_references(page) == set(), slug
            assert dropped_text_attributes(page) == set(), slug
            assert denominator_gaps(spec.contract) == [], slug

    def test_every_registered_template_is_a_card_the_host_accepts(self) -> None:
        """The one gate that covers the caps no rule here restates.

        Every other check reasons about the page or the contract. This one hands the real
        pair to the real normalizer, which is the only thing that knows the whole answer:
        html bytes, data bytes, field count and every field name at once. A page that grew
        past the html cap passes every other gate in this file and is dropped WHOLE by the
        host, which is the failure this package exists to prevent.
        """
        host = importlib.import_module("kiro_crew.dashboard.dynamic_cards")
        for slug, spec in REGISTRY.items():
            page = html_path(slug).read_text(encoding="utf-8")
            contract_module = importlib.import_module(spec.contract.__module__)
            empty = spec.provider(
                {}, contract_module.EMPTY_JUDGMENT, captured_at="1970-01-01T00:00:00Z"
            )
            assert (
                host.normalize_card({"html": page, "data": card_data(empty)}) is not None
            ), f"{slug}: the host refuses this card, so it would render nothing at all"

    def test_every_page_and_contract_in_the_package_has_a_registry_row(self) -> None:
        """The gates iterate the REGISTRY, so an unregistered template is ungated.

        Forgetting the row is the one mistake that costs nothing at the time: the four
        files exist, mypy checks the provider, and every gate here walks straight past
        them because the registry is what it walks. So the directory is compared to the
        registry rather than trusted to agree with it.
        """
        pages = {path.stem for path in PACKAGE.glob("*.html")}
        contracts = {
            path.stem[: -len("_contract")]
            for path in PACKAGE.glob("*_contract.py")
            if path.stem.endswith("_contract")
        }
        registered = set(REGISTRY)
        assert pages - registered == set(), (
            f"page(s) with no REGISTRY row: {sorted(pages - registered)}; every gate here "
            "walks the registry, so these ship unchecked"
        )
        assert (
            contracts - registered == set()
        ), f"contract(s) with no REGISTRY row: {sorted(contracts - registered)}"

    def test_a_slug_resolves_to_a_page_inside_this_package(self) -> None:
        """The path helper is what a reader of a template uses, so it has to answer the
        same way from a checkout and from an install: package-relative, never repo-relative."""
        assert html_path("demo") == PACKAGE / "demo.html"

    def test_every_provider_returns_its_own_contract(self) -> None:
        """The provider's return type IS the contract. A provider annotated with
        something else is a second shape nobody gated."""
        for slug, spec in REGISTRY.items():
            hints = typing.get_type_hints(spec.provider)
            assert (
                hints.get("return") is spec.contract
            ), f"{slug}: provider returns {hints.get('return')!r}, not {spec.contract!r}"

    def test_an_unregistered_slug_names_what_is_registered(self) -> None:
        with pytest.raises(KeyError, match="no dashboard template"):
            spec_for("nope")

    def test_a_spec_records_the_four_things_a_reader_needs(self) -> None:
        spec = TemplateSpec("demo", _Card, lambda: None, "work", 1)
        assert (spec.slug, spec.fold, spec.version) == ("demo", "work", 1)


# ---------------------------------------------------------------------------
# the scaffold
# ---------------------------------------------------------------------------


def _scaffold() -> Any:
    return load_skill_script("dashboard_template_scaffold", SCAFFOLD)


def _can_symlink() -> bool:
    """Can this platform make a symlink at all? Windows needs a privilege for it.

    Asked by trying, not by naming a platform: the refusal the guarded case pins is
    checked with ``lstat`` and holds everywhere, so only the test's own setup is in
    question here.
    """
    with tempfile.TemporaryDirectory() as raw:
        probe = Path(raw)
        try:
            (probe / "link").symlink_to(probe / "target")
        except (OSError, NotImplementedError):
            return False
    return True


_CAN_SYMLINK = _can_symlink()


class TestTheScaffoldsFieldList:
    def test_a_fraction_without_a_named_total_is_refused(self) -> None:
        """A derived `<name>_of` names no key any fold declares, so its total reads as
        "not said" at every render. Naming the key is what lets the scaffold check it, so
        a fraction that names nothing is refused rather than guessed for."""
        mod = _scaffold()
        with pytest.raises(mod.ScaffoldError, match="needs the fold key holding its total"):
            mod.parse_fields(["open:fraction"], totals=("calls",))

    def test_a_numerator_the_fold_does_not_count_is_refused(self) -> None:
        """Both halves of the ratio are read from the fold, so both have to be checked.

        The numerator defaults to the card field's own name, and one typo emits a read of a
        key no fold has -- the card then shows "not said" at every render, which is the same
        defect the total had, on the other side of the same fraction.
        """
        mod = _scaffold()
        with pytest.raises(mod.ScaffoldError, match="numerator would read as not said"):
            mod.parse_fields(["opne:fraction:calls"], totals=("calls", "open"))

    def test_a_renamed_card_field_may_name_the_key_it_reads(self) -> None:
        """The card field and the fold key are allowed to differ, so the refusal above must
        not force them to match. A fourth token names the source explicitly, and it is checked
        the same way."""
        mod = _scaffold()
        fields = mod.parse_fields(["shown:fraction:calls:open"], totals=("calls", "open"))
        fraction = next(f for f in fields if f.kind == "fraction")
        assert (fraction.name, fraction.numerator, fraction.denominator) == (
            "shown",
            "open",
            "calls",
        )
        with pytest.raises(mod.ScaffoldError, match="numerator would read as not said"):
            mod.parse_fields(["shown:fraction:calls:opne"], totals=("calls", "open"))

    def test_the_refusal_offers_the_explicit_source_form(self) -> None:
        """A refusal that does not mention the escape hatch sends an author with a
        deliberately renamed field looking for a bug that is not there."""
        mod = _scaffold()
        with pytest.raises(mod.ScaffoldError, match="name the fold key it reads"):
            mod.parse_fields(["shown:fraction:calls"], totals=("calls", "open"))

    def test_a_total_the_fold_does_not_count_is_refused(self) -> None:
        """Checked against the fold's own `int` fields, read from the same generated
        catalogue `--fold` is checked against, so a typo is caught where it is made rather
        than rendering as "not said" forever."""
        mod = _scaffold()
        with pytest.raises(mod.ScaffoldError, match="not a counting field of this fold"):
            mod.parse_fields(["open:fraction:invented"], totals=("calls", "completed"))

    def test_the_refusal_names_what_the_fold_does_count(self) -> None:
        """A refusal that does not say what IS available makes the author guess again."""
        mod = _scaffold()
        with pytest.raises(mod.ScaffoldError, match="this fold counts: calls, completed"):
            mod.parse_fields(["open:fraction:invented"], totals=("calls", "completed"))

    def test_a_fourth_token_on_a_non_fraction_is_refused(self) -> None:
        """Text takes ONE extra token, the fold key it reads. A fraction takes two, a total
        and a numerator, so a second extra token on a text field is a misunderstanding worth
        naming rather than ignoring."""
        mod = _scaffold()
        with pytest.raises(mod.ScaffoldError, match="text takes at most one"):
            mod.parse_fields(["headline:str|unsaid:goal:extra"], texts=("goal",))

    def test_the_counting_fields_come_from_the_catalogue(self) -> None:
        """Read, never restated: the pair the documented example uses must really be two
        `int` fields of that fold, or the example ships broken again."""
        mod = _scaffold()
        counted = mod.counting_fields("tools")
        assert "calls" in counted and "open" in counted
        assert mod.counting_fields("class") == ()

    def test_a_fraction_reaches_the_contract_as_text(self) -> None:
        mod = _scaffold()
        kinds = {f.name: f.annotation for f in mod.parse_fields(["open:fraction:calls"])}
        assert kinds["open"] == "str | Unsaid"

    def test_there_is_no_bare_count_kind(self) -> None:
        """A bare number on a page answers neither 'out of what' nor 'and if unknown'."""
        assert "int" not in _scaffold().KINDS

    def test_there_is_no_always_present_text_kind(self) -> None:
        """Every author-declared field is read out of the fold, and a fold value can
        always be absent -- that is the premise of ``read_text``. A ``str`` kind put
        ``<name>: str`` in the contract over a provider line returning ``str | Unsaid``,
        so the first documented form of the grammar failed mypy."""
        mod = _scaffold()
        assert "str" not in mod.KINDS
        with pytest.raises(mod.ScaffoldError, match="unknown kind"):
            mod.parse_fields(["plain:str"])

    def test_every_author_declared_kind_admits_the_sentinel(self) -> None:
        """The property behind the case above, stated for whatever kinds come next."""
        for kind, annotation in _scaffold().KINDS.items():
            assert (
                "Unsaid" in annotation
            ), f"{kind!r} emits {annotation!r}, which a fold read cannot fill"

    def test_the_script_supplied_fields_stay_plain_text(self) -> None:
        """``contract_version`` and ``captured_at`` are not fold-read: the provider
        writes them itself, so they are the one place ``str`` is right -- and they must
        keep working after the kind left the author-facing grammar."""
        mod = _scaffold()
        annotations = {f.name: f.annotation for f in mod.parse_fields([])}
        assert annotations["contract_version"] == "str"
        assert annotations["captured_at"] == "str"
        assert annotations["lede"] == "str | Unsaid"

    def test_an_unknown_kind_is_refused_and_names_the_kinds(self) -> None:
        mod = _scaffold()
        with pytest.raises(mod.ScaffoldError, match="unknown kind"):
            mod.parse_fields(["open:integer"])

    def test_a_token_with_no_kind_is_refused(self) -> None:
        mod = _scaffold()
        with pytest.raises(mod.ScaffoldError, match="not 'name:kind'"):
            mod.parse_fields(["open"])

    def test_a_duplicate_field_is_refused(self) -> None:
        mod = _scaffold()
        with pytest.raises(mod.ScaffoldError, match="declared twice"):
            mod.parse_fields(["open:str|unsaid", "open:fraction:calls"])

    def test_the_always_present_fields_arrive_without_asking(self) -> None:
        names = {f.name for f in _scaffold().parse_fields([])}
        assert {"contract_version", "captured_at", "lede", "you", "notes"} <= names

    def test_a_publisher_may_write_only_judgments(self) -> None:
        """Numbers are absent from the publisher's surface on purpose: a publisher that
        can type a count can make the page disagree with the log it summarises."""
        assert _scaffold().JUDGMENT_FIELDS == ("lede", "you", "notes")

    def test_the_always_present_kind_wins_over_a_redeclared_one(self) -> None:
        """The note says the declared kind is ignored; the code must do that.

        A ``lede:fraction`` that survived would put a fraction derivation under a
        judgment field the provider fills with ``gate_text(...)``, and a
        ``captured_at:str|unsaid`` would widen a field the provider always writes.
        """
        mod = _scaffold()
        fields = mod.parse_fields(["lede:fraction:calls", "captured_at:str|unsaid"])
        kinds = {f.name: f.kind for f in fields}
        assert kinds["lede"] == "str|unsaid"
        assert kinds["captured_at"] == "str"
        assert [f.name for f in fields].count("lede") == 1

    def test_the_scaffolds_cap_is_the_hosts_cap(self) -> None:
        """The script cannot import the package -- it ships to every install and runs
        against a checkout whose package may not be importable -- so its cap is a
        literal, and this is the one thing tying the literal to the host's number."""
        assert _scaffold().MAX_CARD_FIELDS == MAX_CARD_FIELDS

    def test_a_field_list_over_the_hosts_cap_is_refused(self) -> None:
        """Exactly one over: MAX_CARD_FIELDS fields in total is accepted, one more is
        refused. A looser list (MAX plus the always-present five) stays green over any
        drift between the script's literal and the host's constant."""
        mod = _scaffold()
        always = len(mod._ALWAYS)
        at_cap = [f"f{i}:str|unsaid" for i in range(MAX_CARD_FIELDS - always)]
        assert len(mod.parse_fields(at_cap)) == MAX_CARD_FIELDS
        with pytest.raises(mod.ScaffoldError, match="exceeds the host's cap"):
            mod.parse_fields(at_cap + ["one_more:str|unsaid"])

    @pytest.mark.parametrize("name", ["class", "for", "import", "lambda", "return", "not"])
    def test_a_python_keyword_field_is_refused(self, name: str) -> None:
        """A field becomes an annotation in the contract's class body, so a keyword
        there is a SyntaxError at import -- and ``class`` is a name this product would
        plausibly want, since one of the ten folds is called that."""
        mod = _scaffold()
        with pytest.raises(mod.ScaffoldError, match="Python keyword"):
            mod.parse_fields([f"{name}:str|unsaid"])

    @pytest.mark.parametrize("name", ["match", "case", "type"])
    def test_a_soft_keyword_field_is_allowed(self, name: str) -> None:
        """Control, and a real one: a soft keyword is contextual and parses fine as an
        annotation target. ``type`` is a field name a template would plausibly want, so
        refusing it would be this script inventing a restriction Python does not have."""
        mod = _scaffold()
        fields = mod.parse_fields([f"{name}:str|unsaid"])
        assert name in {f.name for f in fields}
        compile(mod.render_contract("probe", fields), "<contract>", "exec", dont_inherit=True)

    def test_the_emitted_contract_and_provider_are_valid_python(self) -> None:
        """Compiled, not eyeballed: a generated file that does not parse is a defect the
        template's own test can never report, because the test cannot import it either."""
        mod = _scaffold()
        fields = mod.parse_fields(["settled:fraction:requested", "goal:str|unsaid"])
        emitted = mod.emit("probe_board", "work", fields, Path("/nonexistent"))
        compiled = []
        for path, text in emitted.items():
            if path.suffix == ".py":
                compile(text, str(path), "exec", dont_inherit=True)
                compiled.append(path.name)
        assert sorted(compiled) == [
            "probe_board_contract.py",
            "probe_board_provider.py",
            "test_dashboard_template_probe_board.py",
        ]


class TestTheScaffoldedTemplateHoldsTheInvariants:
    """The end-to-end check: what the script emits satisfies the gates above.

    This is what makes the script MECHANICAL rather than a starting point. The four
    files agree on one field list because they are derived from one field list, and the
    proof is that the generated page and the generated contract pass the same equality
    gate a hand-written template does.
    """

    SLUG = "probe_board"
    FIELDS = ["settled:fraction:requested", "goal:str|unsaid"]
    CARD = "ProbeBoardCard"
    STAMP = "1970-01-01T00:00:00Z"

    @pytest.fixture
    def emitted(self, tmp_path: Path) -> Iterator[_Emitted]:
        """The four files written, and the two generated modules imported.

        The modules are registered in ``sys.modules`` under the dotted names the
        generated provider imports, for two reasons. That import is part of what is
        being checked -- a provider naming its contract wrongly must fail here, not in
        someone's pull request -- and ``typing.get_type_hints`` resolves a TypedDict's
        annotations against ``sys.modules[cls.__module__]``, so an unregistered contract
        raises ``NameError`` on ``Unsaid`` and the case fails for the wrong reason.

        Unregistered again afterwards, so these names cannot answer another module's
        import for the rest of the session.
        """
        mod = _scaffold()
        fields = mod.parse_fields(list(self.FIELDS))
        files = mod.emit(self.SLUG, "work", fields, tmp_path)
        for path, text in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

        pkg = tmp_path / "src" / "kiro_crew" / "dashboard_templates"
        contract_name = f"kiro_crew.dashboard_templates.{self.SLUG}_contract"
        provider_name = f"kiro_crew.dashboard_templates.{self.SLUG}_provider"
        with no_bytecode():
            contract = load_skill_script(contract_name, pkg / f"{self.SLUG}_contract.py")
            sys.modules[contract_name] = contract
            try:
                provider = load_skill_script(provider_name, pkg / f"{self.SLUG}_provider.py")
                sys.modules[provider_name] = provider
                yield _Emitted(
                    files=files,
                    page=(pkg / f"{self.SLUG}.html").read_text(encoding="utf-8"),
                    card=getattr(contract, self.CARD),
                    empty_judgment=getattr(contract, "EMPTY_JUDGMENT"),
                    build=getattr(provider, f"build_{self.SLUG}"),
                )
            finally:
                sys.modules.pop(provider_name, None)
                sys.modules.pop(contract_name, None)

    def test_it_writes_exactly_four_files(self, emitted: _Emitted) -> None:
        assert sorted(p.name for p in emitted.files) == [
            f"{self.SLUG}.html",
            f"{self.SLUG}_contract.py",
            f"{self.SLUG}_provider.py",
            f"test_dashboard_template_{self.SLUG}.py",
        ]

    def test_the_generated_page_and_contract_agree(self, emitted: _Emitted) -> None:
        assert html_fields(emitted.page) == contract_keys(emitted.card)

    def test_the_generated_page_offers_no_controls(self, emitted: _Emitted) -> None:
        assert control_tags_used(emitted.page) == set()

    def test_the_generated_page_reaches_nothing_outside_itself(self, emitted: _Emitted) -> None:
        assert outbound_references(emitted.page) == set()

    def test_the_generated_page_states_nothing_where_the_host_deletes_it(
        self, emitted: _Emitted
    ) -> None:
        assert dropped_text_attributes(emitted.page) == set()

    def test_the_host_accepts_what_the_scaffold_emits(self, emitted: _Emitted) -> None:
        """Run against the real normalizer, not against a restated cap.

        The emitted test file is read as text elsewhere in this class, so this is where the
        scaffold's own output actually faces the host: the page and its empty-fold data
        together, judged on html bytes, data bytes, field count and every field name at
        once. A template born over a cap would be dropped whole on first render.
        """
        host = importlib.import_module("kiro_crew.dashboard.dynamic_cards")
        card = emitted.build({}, emitted.empty_judgment, captured_at=self.STAMP)
        accepted = host.normalize_card({"html": emitted.page, "data": card_data(card)})
        assert accepted is not None, "the host refuses the scaffold's own template"
        assert accepted["data"].keys() == contract_keys(emitted.card)

    def test_the_generated_test_carries_the_outbound_gate(self, emitted: _Emitted) -> None:
        """The scaffold's test is what gates a template an author edits AFTER emitting,
        so the rule has to be in the emitted file, not only in this one."""
        test_text = next(t for p, t in emitted.files.items() if p.name.startswith("test_"))
        assert "outbound_references(_page()) == set()" in test_text

    def test_the_generated_test_runs_every_reader_the_registry_gate_runs(
        self, emitted: _Emitted
    ) -> None:
        """Two gates, one rule, and nothing else compares them.

        A reader added to this package reaches a template through exactly two doors: the
        registry gate here, which covers pages already registered, and the emitted test,
        which covers the one an author is about to write. A reader wired into neither is
        dead code that the description still claims as a gate; wired into only the first,
        every future template ships past it.

        So the emitted test's reader set must cover the registry gate's. Derived from that
        gate's own source rather than listed, because a list here is the same drift again.
        """
        gate = inspect.getsource(
            TestTheRegistry.test_every_registered_template_holds_its_own_invariants
        )
        exported = set(parity.__all__)
        required = {name for name in re.findall(r"\b([a-z_]+)\(", gate) if name in exported}
        assert required, "read no parity readers out of the registry gate; the probe broke"
        test_text = next(t for p, t in emitted.files.items() if p.name.startswith("test_"))
        missing = sorted(name for name in required if f"{name}(" not in test_text)
        assert missing == [], (
            f"the registry gate runs {missing} and the emitted test does not, so every "
            f"template written from the scaffold ships without that check"
        )
        assert "normalize_card(" in test_text, (
            "the emitted test does not hand its page and data to the host normalizer, so "
            "a template over the html byte cap would pass every gate it carries"
        )

    def test_the_generated_contract_has_no_bare_count(self, emitted: _Emitted) -> None:
        assert denominator_gaps(emitted.card) == []

    def test_the_generated_provider_returns_the_generated_contract(self, emitted: _Emitted) -> None:
        assert typing.get_type_hints(emitted.build)["return"] is emitted.card

    def test_the_generated_provider_names_a_gap_instead_of_a_zero(self, emitted: _Emitted) -> None:
        """An empty fold and a silent publisher: every field must read as words."""
        data = card_data(emitted.build({}, emitted.empty_judgment, captured_at=self.STAMP))
        assert data.keys() == contract_keys(emitted.card)
        assert "" not in set(data.values()), data
        assert data["settled"] == NOT_SAID
        assert data["lede"] == NOT_SAID

    def test_the_generated_provider_renders_a_fraction_with_both_sides(
        self, emitted: _Emitted
    ) -> None:
        card = emitted.build(
            {"settled": 2, "requested": 6, "goal": "clear the backlog"},
            emitted.empty_judgment,
            captured_at=self.STAMP,
        )
        assert card["settled"] == "2/6"
        assert card["goal"] == "clear the backlog"

    def test_the_generated_provider_reads_the_total_key_the_author_named(
        self, emitted: _Emitted
    ) -> None:
        """The key comes from the field list, not from the field's own name.

        A derived `<name>_of` matches nothing in any of the ten folds, so a fraction
        resting on it reads "not said" at every render -- and no emitted gate can see
        that, because they all build the card from an EMPTY view where that IS the
        answer. So a view carrying the derived name must not satisfy the fraction.
        """
        derived = emitted.build(
            {"settled": 2, "settled_of": 6},
            emitted.empty_judgment,
            captured_at=self.STAMP,
        )
        assert derived["settled"] is UNSAID
        named = emitted.build(
            {"settled": 2, "requested": 6},
            emitted.empty_judgment,
            captured_at=self.STAMP,
        )
        assert named["settled"] == "2/6"

    def test_the_generated_provider_refuses_a_name_in_the_action_field(
        self, emitted: _Emitted
    ) -> None:
        """The field says what a person must DO. A live board once rendered an owner's
        name and a session id there, and nothing refused it."""
        judgment = dict(emitted.empty_judgment)
        judgment["you"] = "Raymond"
        assert emitted.build({}, judgment, captured_at=self.STAMP)["you"] is UNSAID
        judgment["you"] = "rebase onto main and push"
        assert emitted.build({}, judgment, captured_at=self.STAMP)["you"] == judgment["you"]

    def test_emitting_twice_produces_the_same_bytes(self, tmp_path: Path) -> None:
        """Deterministic, so re-running it on an existing template is a no-op diff and a
        reviewer sees only the field-list change."""
        mod = _scaffold()
        fields = mod.parse_fields(list(self.FIELDS))
        first = mod.emit(self.SLUG, "work", fields, tmp_path)
        second = mod.emit(self.SLUG, "work", fields, tmp_path)
        assert {p.name: t for p, t in first.items()} == {p.name: t for p, t in second.items()}

    def test_a_short_real_action_survives_the_gate(self, emitted: _Emitted) -> None:
        """The action gate judges phrase SHAPE, never length.

        A length floor rejects nothing a one-token check does not already reject -- a
        two-word name clears any floor worth setting -- and it does suppress genuine
        short actions, so the cell reads "not said" when somebody did say what to do.
        """
        judgment = dict(emitted.empty_judgment)
        judgment["you"] = "fix it"
        assert emitted.build({}, judgment, captured_at=self.STAMP)["you"] == "fix it"

    def test_the_registration_output_is_pasteable_on_its_own(self) -> None:
        """The row names three symbols the registry has never imported, so the imports
        are printed with it. Pasting the row alone is a ``NameError`` at import, which
        takes the whole package down rather than one template."""
        mod = _scaffold()
        row = mod.registration_line(self.SLUG, "work")
        imports = mod.registration_imports(self.SLUG)
        for token in ("ProbeBoardCard", f"build_{self.SLUG}", '"work"'):
            assert token in row, row
        assert f"{self.SLUG}_contract import" in imports
        assert f"{self.SLUG}_provider import build_{self.SLUG}" in imports
        # Every contract module exports a constant of the SAME name, so an unaliased
        # import makes the second template silently rebind the first one's version.
        alias = f"{self.SLUG.upper()}_VERSION"
        assert f"CONTRACT_VERSION as {alias}" in imports
        assert alias in row, row

    def test_every_name_the_row_uses_is_bound_by_the_imports(self) -> None:
        """The property that matters: the pair RESOLVES.

        Read structurally rather than by string comparison, and against the names the
        imports BIND (an ``as`` alias binds the alias, not the original), so a row
        naming a symbol nothing imports fails here instead of as a ``NameError`` that
        takes the whole package down at import.
        """
        mod = _scaffold()
        bound = {"TemplateSpec"}  # the registry already imports this one
        for node in ast.walk(ast.parse(mod.registration_imports(self.SLUG))):
            if isinstance(node, ast.ImportFrom):
                bound |= {alias.asname or alias.name for alias in node.names}
        row = ast.parse("REGISTRY = {\n" + mod.registration_line(self.SLUG, "work") + "\n}\n")
        used = {n.id for n in ast.walk(row) if isinstance(n, ast.Name) and n.id != "REGISTRY"}
        assert used, "parsed no names out of the row -- the probe is broken, not the row"
        assert used <= bound, f"row uses unimported names: {sorted(used - bound)}"


class TestTheRestatedHostLimits:
    """This package restates the host's limits; these cases check every one against it.

    The restatement is the package's one fact no type checker can hold, and it is the one
    whose silent failure is worst: a template over a cap is dropped WHOLE by the host, so
    the page stops appearing with nothing red anywhere.

    Two of the four facts are not host CONSTANTS and cannot be compared by name:

    - the field cap is the literal ``24`` inside ``normalize_card``, named nowhere, so it
      is pinned by exercising the normalizer at the boundary instead;
    - the binding attribute lives in the browser (``dashboardDocument.ts`` queries
      ``[data-dashboard-field]``), not in Python, so it is read out of that file.

    Both checks are behavioural rather than nominal, which is the stronger of the two:
    they keep holding if the host renames a constant or inlines one.
    """

    #: Where the browser half of the contract lives. Read, not restated: the attribute
    #: this package binds is only useful if that file is the thing looking for it.
    _BROWSER_BINDER = (
        ROOT / "website" / "src" / "pages" / "chat" / "command-center" / "dashboardDocument.ts"
    )

    @staticmethod
    def _host() -> Any:
        """The host card normalizer. Imported per call so a stub can be injected."""
        return importlib.import_module("kiro_crew.dashboard.dynamic_cards")

    def test_the_field_cap_is_the_hosts_real_boundary(self) -> None:
        """Pinned against behaviour because the host names no constant for it.

        ``normalize_card`` refuses a card with more than 24 data fields by comparing
        against an inline literal. Comparing to an attribute would raise ``AttributeError``
        the day it is renamed; driving the normalizer keeps working, and it pins the
        boundary itself rather than a number that is supposed to describe it.
        """
        host = self._host()
        page = "<p data-dashboard-field='x'></p>"
        at_cap = {f"f{i}": "v" for i in range(MAX_CARD_FIELDS)}
        over_cap = {f"f{i}": "v" for i in range(MAX_CARD_FIELDS + 1)}
        assert host.normalize_card({"html": page, "data": at_cap}) is not None, (
            f"the host refuses {MAX_CARD_FIELDS} fields, so this package's cap is too high "
            "and a template at its own cap is dropped whole"
        )
        assert (
            host.normalize_card({"html": page, "data": over_cap}) is None
        ), f"the host accepts {MAX_CARD_FIELDS + 1} fields, so this package's cap is stricter than it needs to be"

    def test_the_byte_cap_matches_the_hosts(self) -> None:
        assert MAX_CARD_DATA_BYTES == self._host().MAX_DATA_BYTES

    def test_the_binding_name_pattern_matches_the_hosts(self) -> None:
        """A name this package accepts and the host rejects is a field that silently
        never binds; the reverse is a field this package refuses for no reason."""
        host_pattern = self._host()._FIELD_NAME.pattern
        for probe in ("lede", "open_of", "a-b", "A1", "9lives", "has space", "x" * 49):
            assert field_name_ok(probe) == bool(
                re.fullmatch(host_pattern, probe)
            ), f"{probe!r} is judged differently by this package and the host"

    def test_the_binding_attribute_is_the_one_the_browser_queries(self) -> None:
        """The attribute the whole parity mechanism keys on, checked where it is USED.

        It is not a Python constant: the page is bound in the browser, so the only real
        authority is the selector ``dashboardDocument.ts`` runs. A page binding an
        attribute nothing queries renders every field blank, and no Python test that
        compares two Python strings can see that.
        """
        source = self._BROWSER_BINDER.read_text(encoding="utf-8")
        assert f"[{parity._BINDING}]" in source, (
            f"{self._BROWSER_BINDER.name} does not query [{parity._BINDING}]; the pages this "
            "package emits bind an attribute the browser never reads"
        )
        assert f"getAttribute('{parity._BINDING}')" in source, (
            f"{self._BROWSER_BINDER.name} queries [{parity._BINDING}] but reads the field name "
            "from some other attribute"
        )

    def test_each_comparison_above_really_asserts(self, monkeypatch: Any) -> None:
        """PROOF the comparisons are not tautologies, now that they run against the host.

        A gate that cannot be observed failing is indistinguishable from one that never
        fires, which is the defect this whole package exists to remove -- so it must not be
        the shape of the package's own checks. A stub carrying DELIBERATELY wrong values is
        installed under the host's name, and each case reading the host must then fail.

        The browser-side case is excluded on purpose: it reads a file, not this module, and
        no Python stub can make it lie. Its own failure is proven by the file's content
        rather than by injection.
        """
        stub = types.ModuleType("kiro_crew.dashboard.dynamic_cards")
        stub.MAX_DATA_BYTES = MAX_CARD_DATA_BYTES + 1  # type: ignore[attr-defined]
        stub._FIELD_NAME = re.compile(r"zzz\Z")  # type: ignore[attr-defined]
        stub.normalize_card = lambda raw, previous=None: None  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "kiro_crew.dashboard.dynamic_cards", stub)

        for case in (
            self.test_the_field_cap_is_the_hosts_real_boundary,
            self.test_the_byte_cap_matches_the_hosts,
            self.test_the_binding_name_pattern_matches_the_hosts,
        ):
            with pytest.raises(AssertionError):
                case()

    def test_the_host_is_the_one_this_package_names(self) -> None:
        """The module must be the real normalizer, not any importable namesake.

        ``_host()`` keys on one dotted path. Asserting the path imports proves only that
        SOMETHING answers to it, so this pins the function every case above depends on.
        """
        host = self._host()
        assert callable(getattr(host, "normalize_card", None)), (
            "kiro_crew.dashboard.dynamic_cards imports but has no normalize_card; "
            "_host() is pointed at the wrong module"
        )
        assert Path(host.__file__).name == "dynamic_cards.py"


class TestWhatShips:
    """A page absent from the wheel is a green build and an empty card.

    A page is read package-relative, and a source checkout has it -- so the whole
    feature can be green here while every card renders nothing on any pip, index or
    desktop install, because the pages were never copied into site-packages. Nothing
    else in the suite can see that: every other test reads the same tree that hides it.
    """

    @staticmethod
    def _declared_globs() -> list[str]:
        cfg = configparser.ConfigParser()
        cfg.read(ROOT / "setup.cfg", encoding="utf-8")
        raw = cfg.get("options.package_data", "kiro_crew", fallback="")
        return [
            line.strip()
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def test_a_glob_covers_this_packages_pages(self) -> None:
        """Asserted against the GLOB, not against the files present.

        The registry ships empty, so a per-file loop would be vacuous today and would
        start failing only once the first template arrived -- charged to whoever added
        it. The declaration is what has to exist now.
        """
        pkg = ROOT / "src" / "kiro_crew"
        probe = (PACKAGE / "example.html").relative_to(pkg).as_posix()
        assert any(fnmatch(probe, glob) for glob in self._declared_globs()), (
            "no [options.package_data] glob covers this package's .html pages, so pip "
            "would copy a template's provider and contract but not its page"
        )

    def test_a_page_outside_the_package_is_not_covered(self) -> None:
        """Control. Without this, a glob of ``*`` would satisfy the case above and the
        assertion would say nothing about where pages live."""
        assert not any(fnmatch("elsewhere/example.html", glob) for glob in self._declared_globs())

    def test_the_sdist_manifest_ships_them_too(self) -> None:
        """``python -m build`` builds the wheel FROM the sdist, and the sdist takes its
        contents from the manifest -- so a setup.cfg-only entry ships a wheel-only fix."""
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        assert "dashboard_templates" in manifest


class TestWhichCheckoutItWritesInto:
    """``repo_root`` must find the checkout the AUTHOR is in, not the script's own.

    This script ships as a built-in skill, so the copy an agent actually runs lives in
    the skills directory, whose ancestors hold no checkout. Keying only on ``__file__``
    made the command the skill documents raise for its own intended reader -- while
    passing every test here, because the test tree's copy sits inside a checkout.
    """

    @staticmethod
    def _fake_checkout(root: Path) -> Path:
        (root / "src" / "kiro_crew").mkdir(parents=True)
        return root

    def test_the_working_directorys_checkout_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wanted = self._fake_checkout(tmp_path / "authors-checkout")
        monkeypatch.chdir(wanted / "src")
        assert _scaffold().repo_root() == wanted.resolve()

    def test_an_installed_copy_still_works_from_a_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression itself: the script's own ancestors hold nothing.

        Loaded from a path with no checkout above it -- which is what the installed skill
        looks like -- it must still resolve the author's tree from the directory they ran
        it in, instead of raising.
        """
        installed = tmp_path / "skills" / "dashboard-template" / "scripts"
        installed.mkdir(parents=True)
        copy = installed / "scaffold.py"
        copy.write_text(SCAFFOLD.read_text(encoding="utf-8"), encoding="utf-8")
        mod = load_skill_script("dashboard_template_scaffold_installed", copy)

        wanted = self._fake_checkout(tmp_path / "elsewhere")
        monkeypatch.chdir(wanted)
        assert mod.repo_root() == wanted.resolve()

    def test_with_no_checkout_either_way_it_refuses_rather_than_guessing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Writing four files into a tree with no package to hold them is worse than
        stopping: the error must name both places it looked."""
        installed = tmp_path / "skills" / "scripts"
        installed.mkdir(parents=True)
        copy = installed / "scaffold.py"
        copy.write_text(SCAFFOLD.read_text(encoding="utf-8"), encoding="utf-8")
        mod = load_skill_script("dashboard_template_scaffold_rootless", copy)

        bare = tmp_path / "not-a-checkout"
        bare.mkdir()
        monkeypatch.chdir(bare)
        with pytest.raises(mod.ScaffoldError) as caught:
            mod.repo_root()
        assert "working directory" in str(caught.value)
        assert "--out-root" in str(caught.value)


class TestTheScaffoldCli:
    def test_it_refuses_a_fold_the_product_does_not_keep(self, tmp_path: Path) -> None:
        done = subprocess.run(
            [sys.executable, str(SCAFFOLD), "x_board", "--fold", "invented"],
            capture_output=True,
            **UTF8_TEXT,
            cwd=str(tmp_path),
        )
        assert done.returncode != 0
        assert "invented" in done.stderr

    def test_it_writes_under_windows_file_semantics(self, tmp_path: Path) -> None:
        """The scaffold must write on a platform with no `O_NOFOLLOW` and no directory open.

        Pinned by simulation because this case has already broken once in a way no Linux
        run could see: a pinned-directory walk opened each component with `os.open`, which
        Windows refuses on a directory, so every write raised before producing a file.

        The three differences that mattered are applied together, since any one alone lets
        the broken version pass: the two flag constants do not exist, `os.open` on a
        directory is `EACCES`, and `dir_fd` is unavailable.
        """
        mod = _scaffold()
        root = tmp_path
        (root / "src" / "kiro_crew").mkdir(parents=True)
        files = mod.emit("x_board", "work", mod.parse_fields(["open:fraction:calls"]), root)
        real = os.open

        def windows_open(path: Any, flags: int, *rest: Any, **kw: Any) -> int:
            if kw.get("dir_fd") is not None:
                raise NotImplementedError("dir_fd unavailable on this platform")
            try:
                if stat.S_ISDIR(os.lstat(path).st_mode):
                    raise PermissionError(13, "Permission denied", str(path))
            except FileNotFoundError:
                pass
            return real(path, flags, *rest, **kw)

        absent = {
            name: getattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY") if hasattr(os, name)
        }
        for name in absent:
            delattr(os, name)
        try:
            with mock.patch.object(mod.os, "open", windows_open):
                mod.publish(root, files, force=False)
                mod.publish(root, files, force=True)
        finally:
            for name, value in absent.items():
                setattr(os, name, value)
        assert sorted(p.name for p in files) == sorted(p.name for p in files if p.is_file())
        # Cleanup is NOT asserted here. Patching `os.open` globally also reaches
        # `shutil.rmtree`, whose descriptor-based path this simulation refuses while real
        # Windows never takes it -- so a leftover here would be the harness, not the
        # product. The two cases running under real semantics assert it instead.

    def test_a_run_that_overwrites_nothing_cannot_clobber_a_concurrent_one(
        self, tmp_path: Path
    ) -> None:
        """Checking a target is absent and creating it later leaves a window between the two.

        Two runs for one slug both see it absent and the second overwrites what the first
        just created. `O_EXCL` at the final name is both the check and the write, which
        closes the window by construction. This is a race between two runs, not a torn
        write, so it is the one write property version control cannot arbitrate.
        """
        mod = _scaffold()
        root = tmp_path
        (root / "src" / "kiro_crew").mkdir(parents=True)
        first = mod.emit("x_board", "tools", mod.parse_fields(["open:fraction:calls"]), root)
        second = mod.emit(
            "x_board", "approvals", mod.parse_fields(["pending:fraction:requested"]), root
        )
        mod.publish(root, first, force=False)
        landed = {path: path.read_bytes() for path in first}
        with pytest.raises(mod.ScaffoldError, match="pass --force to overwrite"):
            mod.publish(root, second, force=False)
        assert {path: path.read_bytes() for path in first} == landed

    def test_a_write_that_fails_after_the_file_exists_leaves_no_fragment(
        self, tmp_path: Path
    ) -> None:
        """`os.open` returning is already a file on disk, at size zero.

        A quota or full disk reported at flush-on-close then leaves it there holding part of
        the text, and a caller that records the path only once the write RETURNS never learns
        it was created -- so its own sweep walks past the fragment, and the next run reports
        "exists; pass --force" about a file nothing wrote on purpose. The write removes it
        itself, which is the only place that knows it exists.
        """
        mod = _scaffold()
        root = tmp_path
        (root / "src" / "kiro_crew").mkdir(parents=True)
        files = mod.emit("x_board", "tools", mod.parse_fields(["open:fraction:calls"]), root)
        target = list(files)[-1]
        target.parent.mkdir(parents=True, exist_ok=True)

        def fail_once_the_descriptor_is_open(fd: int, *args: Any, **kwargs: Any) -> Any:
            os.close(fd)
            raise OSError(28, "No space left on device")

        with mock.patch.object(mod.os, "fdopen", fail_once_the_descriptor_is_open):
            with pytest.raises(mod.ScaffoldError, match="cannot write"):
                mod._write_new(target, "the text that never landed\n")
        assert not target.exists()
        # And the next run is not misled by a fragment into demanding --force.
        mod.publish(root, files, force=False)
        assert target.read_text(encoding="utf-8") == files[target]

    def test_force_plainly_overwrites_all_four(self, tmp_path: Path) -> None:
        """What `--force` is for, and why nothing here stages the writes.

        A run that SUCCEEDS already destroys every uncommitted edit to all four outputs, so
        machinery preserving some of them when the third write fails protects a fraction of
        what the flag destroys by design. These files live in a checkout on their way to
        review: `git status` names a mixed set and `git checkout --` undoes it.
        """
        mod = _scaffold()
        root = tmp_path
        (root / "src" / "kiro_crew").mkdir(parents=True)
        files = mod.emit("x_board", "tools", mod.parse_fields(["open:fraction:calls"]), root)
        mod.publish(root, files, force=False)
        changed = {path: text + "\n# changed\n" for path, text in files.items()}
        mod.publish(root, changed, force=True)
        assert {path: path.read_text(encoding="utf-8") for path in files} == changed

    def test_a_refused_new_run_removes_only_what_it_created(self, tmp_path: Path) -> None:
        """The other half: a run stopped part-way must take back its own outputs and leave
        the file that stopped it alone -- that file belongs to whoever wrote it first."""
        mod = _scaffold()
        root = tmp_path
        (root / "src" / "kiro_crew").mkdir(parents=True)
        files = mod.emit("x_board", "tools", mod.parse_fields(["open:fraction:calls"]), root)
        targets = list(files)
        targets[-1].parent.mkdir(parents=True, exist_ok=True)
        targets[-1].write_text("a racing run got here first\n", encoding="utf-8")
        with pytest.raises(mod.ScaffoldError, match="exists"):
            mod.publish(root, files, force=False)
        assert targets[-1].read_text(encoding="utf-8") == "a racing run got here first\n"
        assert not any(path.exists() for path in targets[:-1])

    @pytest.mark.skipif(not _CAN_SYMLINK, reason="this platform will not create a symlink here")
    @pytest.mark.parametrize("planted", ["file", "directory"])
    def test_it_refuses_to_write_through_a_planted_symlink(
        self, tmp_path: Path, planted: str
    ) -> None:
        """A link at one of the derived output paths must not send the write outside.

        The paths are derived and the slug cannot traverse, so the way out is a symlink
        planted in the checkout being written into -- which on the invocation this skill
        documents is a checkout a fork controls. `Path.write_text` follows the final
        component and `mkdir(parents=True)` accepts a symlinked directory, so BOTH shapes
        are pinned; with `--force` the old write truncated whatever the link resolved to
        while still printing the in-repo path.
        """
        root = tmp_path / "checkout"
        (root / "src" / "kiro_crew").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "victim.py"
        victim.write_text("do not touch\n", encoding="utf-8")
        pkg = root / "src" / "kiro_crew" / "dashboard_templates"
        if planted == "file":
            pkg.mkdir()
            (pkg / "x_board_contract.py").symlink_to(victim)
        else:
            pkg.symlink_to(outside, target_is_directory=True)
        done = subprocess.run(
            [sys.executable, str(SCAFFOLD), "x_board", "--fold", "work", "--force"],
            capture_output=True,
            cwd=str(root),
            **UTF8_TEXT,
        )
        assert done.returncode == 2, done.stdout
        assert "symlink" in done.stderr
        assert victim.read_text(encoding="utf-8") == "do not touch\n"

    def test_the_documented_example_command_runs(self, tmp_path: Path) -> None:
        """A command the skill tells an author to type has to work when typed.

        Read out of `SKILL.md`'s own fenced block rather than restated here, so the
        document and this case cannot drift: an example naming a fold that has no such
        counting field, or a fraction with no total, exits 2 and fails here instead of
        reaching an author who then has to work out which half is wrong.

        Fenced blocks only. A prose mention of the script is not a command, and treating
        one as a command means parsing sentences, which answers a question nobody asked.
        """
        skill = SCAFFOLD.parents[1] / "SKILL.md"
        blocks = re.findall(r"```bash\n(.*?)```", skill.read_text(encoding="utf-8"), re.DOTALL)
        commands = [
            block.replace("\\\n", " ")
            for block in blocks
            if "scaffold.py" in block and "--fold" in block
        ]
        assert commands, "SKILL.md documents no runnable scaffold command in a bash block"
        root = tmp_path / "checkout"
        (root / "src" / "kiro_crew").mkdir(parents=True)
        for command in commands:
            argv = shlex.split(command)
            index = next(i for i, token in enumerate(argv) if token.endswith("scaffold.py"))
            argv = argv[index + 1 :]
            argv[0] = "x_board" if argv[0] == "<slug>" else argv[0]
            done = subprocess.run(
                [sys.executable, str(SCAFFOLD)] + argv + ["--print-only"],
                capture_output=True,
                cwd=str(root),
                **UTF8_TEXT,
            )
            assert done.returncode == 0, f"SKILL.md documents a failing command: {done.stderr}"

    def test_the_scripts_own_example_matches_the_skills(self) -> None:
        """The script's usage line and the skill's block are two copies of one command, so
        a correction to either that misses the other is caught here rather than by whoever
        types the stale one."""
        skill = (SCAFFOLD.parents[1] / "SKILL.md").read_text(encoding="utf-8")
        source = SCAFFOLD.read_text(encoding="utf-8").splitlines()
        start = next(i for i, line in enumerate(source) if "scaffold.py <slug>" in line)
        usage = source[start]
        # The usage line may wrap with a trailing backslash, the same way the skill's block
        # does, so the whole command is read rather than its first line.
        while usage.rstrip().endswith("\\"):
            start += 1
            usage = usage.rstrip().removesuffix("\\") + " " + source[start].strip()
        # Derived from the skill's own block rather than retyped here: a token spelled in
        # this test is a third copy of the example, and the copy nobody updates.
        block = next(b for b in re.findall(r"```bash\n(.*?)```", skill, re.S) if "scaffold.py" in b)
        plain = usage.replace("'", "").replace('"', "")
        documented = [
            word.replace("'", "").replace('"', "")
            for word in block.replace("\\\n", " ").split()
            if ":" in word
        ]
        fold = block.split("--fold", 1)[1].split()[0]
        assert f"--fold {fold}" in usage, "the script's usage line names a different fold"
        for token in documented:
            assert token in plain, f"the script's usage line does not name {token}"

    def test_it_refuses_to_overwrite_without_force(self, tmp_path: Path) -> None:
        args = [
            sys.executable,
            str(SCAFFOLD),
            "x_board",
            "--fold",
            "work",
            "--out-root",
            str(tmp_path),
        ]
        first = subprocess.run(args, capture_output=True, cwd=str(tmp_path), **UTF8_TEXT)
        assert first.returncode == 0, first.stderr
        again = subprocess.run(args, capture_output=True, cwd=str(tmp_path), **UTF8_TEXT)
        assert again.returncode == 2
        assert "pass --force" in again.stderr
        forced = subprocess.run(
            args + ["--force"], capture_output=True, cwd=str(tmp_path), **UTF8_TEXT
        )
        assert forced.returncode == 0, forced.stderr

    def test_it_writes_nothing_under_print_only(self, tmp_path: Path) -> None:
        done = subprocess.run(
            [
                sys.executable,
                str(SCAFFOLD),
                "x_board",
                "--fold",
                "work",
                "--out-root",
                str(tmp_path),
                "--print-only",
            ],
            capture_output=True,
            **UTF8_TEXT,
            cwd=str(tmp_path),
        )
        assert done.returncode == 0, done.stderr
        assert not (tmp_path / "src").exists()
        assert "x_board.html" in done.stdout
