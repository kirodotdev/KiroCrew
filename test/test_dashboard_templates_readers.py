"""The dashboard-template readers: what a page binds and what a contract names.

The parity check in ``test_dashboard_templates.py`` (invariant 1) compares ``html_fields``
with ``contract_keys``, so a reader that counts the wrong set lets a blank cell pass. These
tests pin which bindings and placements the page reader reads or refuses, and which
contract shapes the contract reader accepts or refuses.
"""

from __future__ import annotations

import re
import sys
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Required, TypedDict
from unittest import mock

import pytest
from dashboard_templates_helpers import (
    PAGE,
    ROOT,
    _Card,
    _MaybeCountCard,
)
from skill_script_helpers import load_skill_script

import kiro_crew.dashboard_templates.parity as parity
from kiro_crew.dashboard_templates import Unsaid
from kiro_crew.dashboard_templates.parity import ExtractionRefused, contract_keys, html_fields


# Declared at MODULE level: ``typing.get_type_hints`` resolves against module globals,
# so a TypedDict nested in a function would raise ``NameError``.
class _Inner(TypedDict):
    n: int


class _NestedCard(TypedDict):
    lede: str
    inner: _Inner


class _ListCard(TypedDict):
    rows: list[str]


class _EmptyCard(TypedDict):
    pass


class _OptionalNestedCard(TypedDict):
    """The usual way a nested value slips in: someone makes it optional."""

    lede: str
    inner: _Inner | Unsaid


class _OptionalListCard(TypedDict):
    rows: list[str] | Unsaid


# reading the page


class TestThePageReader:
    def test_it_finds_every_binding(self) -> None:
        assert html_fields(PAGE) == {"lede", "open"}

    def test_a_page_binding_nothing_is_refused(self) -> None:
        """A reader returning an empty set makes the contract read as entirely
        over-specified, which sends the next person to delete the contract."""
        with pytest.raises(ExtractionRefused, match="no data-dashboard-field"):
            html_fields("<html><body><p>hello</p></body></html>")

    def test_a_binding_with_no_name_is_refused(self) -> None:
        """The host looks the empty string up and writes it back, so the element
        renders blank forever and names nothing to trace it to."""
        with pytest.raises(ExtractionRefused, match="carry no field name"):
            html_fields(PAGE.replace('data-dashboard-field="open"', 'data-dashboard-field=" "'))

    def test_a_binding_on_style_is_refused(self) -> None:
        """The binder steps over ``style`` before it reads the field name.

        Counted, the equality gate passes while that cell is blank at every render --
        this reader handing the page a clean bill for the exact defect it exists to
        catch.
        """
        with pytest.raises(ExtractionRefused, match="steps over"):
            html_fields(
                "<html><body><p data-dashboard-field='lede'></p>"
                "<style data-dashboard-field='open'>.v{}</style></body></html>"
            )

    @pytest.mark.parametrize("root", ["body", "html"], ids=["body", "html"])
    def test_a_binding_on_the_root_or_the_body_itself_is_refused(self, root: str) -> None:
        """The binder asks the body for its DESCENDANTS, which excludes both of them.

        `body.querySelectorAll` never matches the body or the root above it, and a list
        of skipped tags cannot catch this.
        """
        page = (
            f"<html><{root} data-dashboard-field='open'>"
            f"<p data-dashboard-field='lede'></p></{root}></html>"
        )
        with pytest.raises(ExtractionRefused, match="never matched"):
            html_fields(page)

    def test_markup_after_the_body_closes_is_still_read(self) -> None:
        """The parser reparents it into the body, so the binder does reach it.

        Refusing on stack position would reject a page the browser binds. Only the head is
        outside the binder's reach.
        """
        assert html_fields(
            "<html><body><p data-dashboard-field='lede'></p></body>"
            "<span data-dashboard-field='open'></span></html>"
        ) == {"lede", "open"}

    def test_a_fragment_with_no_body_tag_is_still_read(self) -> None:
        """Control: a fragment has no body element and the browser wraps it, so refusing
        it would reject the shape the scaffold's own page could take."""
        assert html_fields(
            "<p data-dashboard-field='lede'></p><span data-dashboard-field='open'></span>"
        ) == {"lede", "open"}

    def test_content_before_a_named_body_is_still_read(self) -> None:
        """Markup ahead of the tag opens the body implicitly, so the browser does have it
        inside; refusing it here would be stricter than the page it describes."""
        assert html_fields(
            "<html><span data-dashboard-field='open'></span>"
            "<body><p data-dashboard-field='lede'></p></body></html>"
        ) == {"lede", "open"}

    def test_a_binding_in_the_head_is_refused(self) -> None:
        """The binder queries the body, so a binding parked in the head is never visited.

        The element must be one html really keeps in the head: a ``<span>`` there is
        reparented into the body, where the binder finds it.
        """
        with pytest.raises(ExtractionRefused, match="queries the body"):
            html_fields(
                "<html><head><title data-dashboard-field='open'></title></head>"
                "<body><p data-dashboard-field='lede'></p></body></html>"
            )

    def test_a_span_written_inside_the_head_is_bindable(self) -> None:
        """The pair to the refusal above: html moves this one into the body itself."""
        assert html_fields(
            "<html><head><span data-dashboard-field='open'></span></head>"
            "<body><p data-dashboard-field='lede'></p></body></html>"
        ) == {"lede", "open"}

    def test_where_the_head_ends_is_a_content_question_not_a_nesting_one(self) -> None:
        """Why the head is NOT read off the open-element stack.

        html reparents content the head may not hold into the body, so the head ends at
        the first element html does not permit there, as a browser's tree builder does.
        """
        assert html_fields(
            "<html><head><title>x</title>"
            "<span data-dashboard-field='open'></span></head>"
            "<body><p data-dashboard-field='lede'></p></body></html>"
        ) == {"lede", "open"}

    def test_head_content_written_after_a_body_element_does_not_reopen_the_head(
        self,
    ) -> None:
        """Control on the rule above: the head ended and a later `<title>` does not put the
        reader back inside it. The `<title>` is still refused a binding -- it renders none of
        its text wherever it sits -- but the `<span>` after it is in the body and binds."""
        assert html_fields(
            "<html><head><meta charset='utf-8'></head>"
            "<body><p data-dashboard-field='lede'></p><title>late</title>"
            "<span data-dashboard-field='open'></span></body></html>"
        ) == {"lede", "open"}

    def test_the_same_page_passes_once_the_binding_moves_into_the_body(self) -> None:
        """Control: the refusals above are about WHERE the binding sits, not the field."""
        assert html_fields(
            "<html><head><style>.v{}</style></head>"
            "<body><p data-dashboard-field='lede'></p>"
            "<span data-dashboard-field='open'></span></body></html>"
        ) == {"lede", "open"}

    def test_a_binding_on_a_tag_the_sanitizer_removes_is_refused(self) -> None:
        """The element leaves and the binding goes with it, so the field is unfillable
        for a reason no author can see in their own markup."""
        with pytest.raises(ExtractionRefused, match="sanitizer removes"):
            html_fields(
                "<html><body><p data-dashboard-field='lede'></p>"
                "<set data-dashboard-field='open'></set></body></html>"
            )

    def test_a_binding_nested_inside_another_binding_is_refused(self) -> None:
        """Filling the outer element replaces its children, detaching the inner one.

        The binder assigns ``textContent`` in document order, so the inner value is written
        to a node nothing renders.
        """
        with pytest.raises(ExtractionRefused, match="detached node"):
            html_fields(
                "<html><body><p data-dashboard-field='lede'>"
                "<span data-dashboard-field='open'></span></p></body></html>"
            )

    @pytest.mark.parametrize(
        "page",
        [
            "<p data-dashboard-field='lede'>x<span data-dashboard-field='open'>y</span></p>",
            "<div data-dashboard-field='lede'><div data-dashboard-field='open'>y</div></div>",
            "<table><tr><td data-dashboard-field='lede'><table><tr>"
            "<td data-dashboard-field='open'>y</td></tr></table></td></tr></table>",
        ],
    )
    def test_real_nesting_is_refused_on_explicitly_closed_markup(self, page: str) -> None:
        """The property the open-element stack exists for: which element a binding sits in.

        With explicit end tags the reader still tracks nesting, because the outer
        `textContent` assignment detaches the inner bound element.
        """
        with pytest.raises(ExtractionRefused, match="detached node"):
            html_fields(f"<html><body>{page}</body></html>")

    def test_a_binding_nested_several_levels_down_is_still_refused(self) -> None:
        """The inner one need not be a direct child; any descendant is detached too."""
        with pytest.raises(ExtractionRefused, match="detached node"):
            html_fields(
                "<html><body><div data-dashboard-field='lede'><em><b>"
                "<span data-dashboard-field='open'></span>"
                "</b></em></div></body></html>"
            )

    def test_siblings_and_unbound_wrappers_are_still_read(self) -> None:
        """Control, and the case the refusal must not catch: nesting is only a problem
        when the OUTER element is itself bound."""
        assert html_fields(
            "<html><body><div><p data-dashboard-field='lede'><em>x</em></p>"
            "<span data-dashboard-field='open'></span></div></body></html>"
        ) == {"lede", "open"}

    def test_self_closing_syntax_on_a_normal_element_is_refused(self) -> None:
        """Html does NOT honour ``<span/>``: the element stays open, so the next binding
        is nested inside it.

        The slash reaches a different parser callback, hence its own case; it is refused
        like any element left open.
        """
        with pytest.raises(ExtractionRefused, match="innermost open element|still open"):
            html_fields(
                "<html><body><span data-dashboard-field='lede'/>"
                "<span data-dashboard-field='open'></span></body></html>"
            )

    def test_an_end_tag_matching_no_open_element_is_refused(self) -> None:
        """A stray `</div>` inside a bound `<p>`.

        A browser discards it and keeps the `p` open, so the next binding is nested.
        Refusing the markup keeps the nesting answer from depending on a repair model.
        """
        with pytest.raises(ExtractionRefused, match="innermost open element"):
            html_fields(
                "<html><body><p data-dashboard-field='lede'></div>"
                "<span data-dashboard-field='open'></span></p></body></html>"
            )

    def test_self_closing_syntax_inside_svg_really_does_close(self) -> None:
        """Foreign content honours the slash, so an svg child must not swallow what
        follows it. Refusing here would reject a legitimate page."""
        assert html_fields(
            "<html><body><p data-dashboard-field='lede'></p>"
            "<svg><use xlink:href='#icon'/></svg>"
            "<span data-dashboard-field='open'></span></body></html>"
        ) == {"lede", "open"}

    @pytest.mark.parametrize("tag", ["path", "circle", "g", "rect", "svg", "text"])
    def test_a_binding_anywhere_inside_svg_is_refused(self, tag: str) -> None:
        """Whether svg paints a string is not a property of the element holding it.

        ``<text>`` paints, but not under a definitions container, so the reader refuses
        the whole foreign subtree instead of deciding per element.
        """
        with pytest.raises(ExtractionRefused, match="inside svg"):
            html_fields(
                f"<html><body><p data-dashboard-field='lede'></p>"
                f"<svg><{tag} data-dashboard-field='open'></{tag}></svg></body></html>"
            )

    @pytest.mark.parametrize("wrapper", ["defs", "clipPath", "mask", "pattern", "symbol"])
    def test_a_binding_on_svg_text_under_a_non_rendering_container_is_refused(
        self, wrapper: str
    ) -> None:
        """The case a per-element rule gets wrong: the tag paints text, the ancestor
        means this instance of it paints none."""
        with pytest.raises(ExtractionRefused, match="inside svg"):
            html_fields(
                f"<html><body><p data-dashboard-field='lede'></p><svg><{wrapper}>"
                f"<text data-dashboard-field='open'></text></{wrapper}></svg>"
                f"</body></html>"
            )

    @pytest.mark.parametrize("tag", ["x-dashboard-value", "my-card", "spann", "dvi"])
    def test_a_binding_on_a_tag_the_sanitizer_cannot_keep_is_refused(self, tag: str) -> None:
        """A custom element or a misspelling is REMOVED, not just left unpainted.

        The host calls DOMPurify with only `ADD_TAGS: ['style']`, so a name outside its
        default allow-list is removed with its binding. Only an allow-list can detect a
        name nobody listed.
        """
        with pytest.raises(ExtractionRefused, match="not an html element"):
            html_fields(
                f"<html><body><p data-dashboard-field='lede'></p>"
                f"<{tag} data-dashboard-field='open'></{tag}></body></html>"
            )

    @pytest.mark.parametrize("tag", sorted(parity._OBSOLETE_TAGS - {"plaintext"}))
    def test_a_binding_on_an_obsolete_element_is_refused(self, tag: str) -> None:
        """Refused on this package's own ground, not on a guess about the sanitizer.

        An obsolete element is not how this product renders a fact. "DOMPurify drops it"
        would need the allow-list, which is unreadable from here.
        """
        with pytest.raises(ExtractionRefused, match="marks obsolete"):
            html_fields(
                f"<html><body><p data-dashboard-field='lede'></p>"
                f"<{tag} data-dashboard-field='open'></{tag}></body></html>"
            )

    def test_plaintext_is_refused_however_the_parser_tokenizes_it(self) -> None:
        """`plaintext` is the one member of the obsolete set whose refusal is not fixed here.

        Whether `html.parser` reports its end tag differs by platform, so either the
        structural or the obsolete rule refuses it; the assertion accepts both.
        """
        with pytest.raises(ExtractionRefused, match="still open|marks obsolete"):
            html_fields(
                "<html><body><p data-dashboard-field='lede'></p>"
                "<plaintext data-dashboard-field='open'></plaintext></body></html>"
            )

    @pytest.mark.parametrize("tag", ["big", "font", "nobr", "strike", "tt"])
    def test_an_obsolete_formatting_element_is_still_read(self, tag: str) -> None:
        """The boundary, and the reason the obsolete set stops where it does: a browser paints
        these, a sanitizer that keeps them is plausible, and refusing them would reject a page
        that renders. Being obsolete is not on its own enough to refuse a binding."""
        assert html_fields(
            f"<html><body><p data-dashboard-field='lede'></p>"
            f"<{tag} data-dashboard-field='open'>x</{tag}></body></html>"
        ) == {"lede", "open"}

    def test_an_element_dropped_from_html_is_not_a_known_element(self) -> None:
        """Obsolete-but-parsed elements belong in the known set; a DROPPED one does not.

        `keygen` is gone from html and from every browser, so a binding on one is a binding
        on nothing.
        """
        assert "keygen" not in parity._KNOWN_HTML
        with pytest.raises(ExtractionRefused, match="not an html element"):
            html_fields(
                "<html><body><p data-dashboard-field='lede'></p>"
                "<keygen data-dashboard-field='open'></keygen></body></html>"
            )

    @pytest.mark.parametrize("tag", ["span", "div", "p", "td", "output", "time", "data"])
    def test_an_ordinary_element_is_still_read(self, tag: str) -> None:
        """The allow-list is html's element index, which is WIDER than DOMPurify's, so this
        rule must never be what refuses a page the host would have rendered."""
        assert html_fields(
            f"<html><body><table><tr><{tag} data-dashboard-field='open'></{tag}></tr></table>"
            f"<p data-dashboard-field='lede'>x</p></body></html>"
        ) == {"lede", "open"}

    def test_the_known_element_list_agrees_with_every_other_set_here(self) -> None:
        """The one rule in this reader not read from the authority that governs it, so it is
        held to the module's own neighbours instead: every html tag any other set names must
        be a known element, or two rules in one file disagree about what html contains."""
        named: set[str] = set()
        for group in (
            parity._VOID_TAGS,
            parity.CONTROL_TAGS,
            parity._FALLBACK_TEXT_TAGS,
            parity._HEAD_CONTENT,
            parity._OBSOLETE_TAGS,
            parity._BINDING_SKIPPED_TAGS,
            parity._ROOTS,
        ):
            named |= set(group)
        assert not named - parity._KNOWN_HTML

    @pytest.mark.parametrize(
        "opened,inner",
        [("svg", "circle"), ("math", "mi")],
    )
    def test_a_binding_in_well_formed_foreign_content_is_refused(
        self, opened: str, inner: str
    ) -> None:
        """Whether foreign content paints a string is not a property of the element holding
        it, so the refusal covers foreign content whole rather than listing svg tags."""
        with pytest.raises(ExtractionRefused, match="depends on the ancestor chain"):
            html_fields(
                f"<html><body><p data-dashboard-field='lede'></p><{opened}>"
                f"<{inner} data-dashboard-field='open'></{inner}></{opened}>"
                f"</body></html>"
            )

    @pytest.mark.parametrize(
        "page,expected",
        [
            # misnested: the <i> is left open across the </div>
            ("<div><i data-dashboard-field='open'>x</div></i>", "innermost open element"),
            # omitted end tag: html permits it, this reader does not
            ("<ul><li data-dashboard-field='open'>x<li>y</li></ul>", "innermost open element"),
            (
                "<table><tr><td data-dashboard-field='open'>x<td>y</td></tr></table>",
                "innermost open element",
            ),
            ("<p data-dashboard-field='open'>x<div>y</div>", "innermost open element"),
            # stray: closes nothing at all
            ("<span data-dashboard-field='open'>x</span></b>", "innermost open element"),
            # an end tag for a void element, which html has none of
            ("<span data-dashboard-field='open'>x</span></br>", "void element"),
        ],
    )
    def test_an_end_tag_that_does_not_close_the_innermost_element_is_refused(
        self, page: str, expected: str
    ) -> None:
        """One rule covering every way the source and the browser's tree come apart.

        Modelling html's omitted end tags and scope rules would make the nesting answer
        depend on that model being right. Requiring explicit closes cannot be wrong about
        scope, and the author's fix is one end tag on the line named.
        """
        with pytest.raises(ExtractionRefused, match=expected):
            html_fields(f"<html><body>{page}</body></html>")

    def test_an_end_tag_before_anything_opens_is_refused(self) -> None:
        """The empty-stack arm of the same rule: nothing is open, so there is nothing this
        end tag could be closing."""
        with pytest.raises(ExtractionRefused, match="closes nothing"):
            html_fields("</b><html><body><span data-dashboard-field='open'>x</span></body></html>")

    def test_a_page_ending_with_an_element_still_open_is_refused(self) -> None:
        """The other end of the same rule. Nothing mismatched, the page just stopped."""
        with pytest.raises(ExtractionRefused, match="still open"):
            html_fields("<html><body><div><span data-dashboard-field='open'>x</span>")

    def test_explicitly_closed_markup_is_read(self) -> None:
        """The control, and the shape the scaffold emits: closed everywhere, including the
        list and table cells whose end tags html would let an author leave out."""
        assert html_fields(
            "<html><head><title>t</title></head><body>"
            "<p data-dashboard-field='lede'>z</p>"
            "<div class='r'><b>Open</b>"
            "<span class='v' data-dashboard-field='open'></span></div>"
            "<ul><li data-dashboard-field='first'>a</li><li>b</li></ul>"
            "<table><tr><td data-dashboard-field='cell'>c</td><td>d</td></tr></table>"
            "</body></html>"
        ) == {"lede", "open", "first", "cell"}

    def test_a_binding_inside_the_head_is_refused(self) -> None:
        """The binder queries the body's descendants, so nothing in the head is ever filled.

        Paired with the case above it: that one fixes where the head ENDS, this one fixes
        that a binding before that point is refused.
        """
        with pytest.raises(ExtractionRefused, match="inside <head>"):
            html_fields(
                "<html><head><title data-dashboard-field='open'>x</title></head>"
                "<body><p data-dashboard-field='lede'>z</p></body></html>"
            )

    @pytest.mark.parametrize("tag", sorted(parity._FALLBACK_TEXT_TAGS))
    def test_a_binding_on_an_element_whose_text_is_fallback_is_refused(self, tag: str) -> None:
        """The spec calls this content FALLBACK: it is painted only where the element
        itself cannot be used, so a browser that supports the element shows none of it.

        The binder fills it anyway, so the binding is reachable and invisible. The set is
        read from the module so a new element cannot join it uncovered.
        """
        with pytest.raises(ExtractionRefused, match="FALLBACK content"):
            html_fields(
                f"<html><body><p data-dashboard-field='lede'></p>"
                f"<{tag} data-dashboard-field='open'></{tag}></body></html>"
            )

    def test_an_element_that_paints_its_own_text_is_still_read(self) -> None:
        """Control on the set above: `picture` wraps an image but its stray text IS painted,
        so widening the rule to everything media-shaped would refuse a page that works."""
        assert html_fields(
            "<html><body><p data-dashboard-field='lede'></p>"
            "<picture data-dashboard-field='open'></picture></body></html>"
        ) == {"lede", "open"}

    @pytest.mark.parametrize("tag", sorted(parity._HEAD_CONTENT))
    def test_a_binding_on_an_element_that_renders_no_text_is_refused(self, tag: str) -> None:
        """Asked wherever the element sits, not only inside the head.

        The binder matches `<body><title data-dashboard-field="open">`, but the default
        sheet hides `title`. No element html permits in the head renders its text anywhere.

        Some (`base`, `link`, `meta`, `style`) hit an earlier rule, so the match accepts any
        refusal. The set is read from the module so a new element cannot go uncovered.
        """
        # Void elements (`base`, `link`, `meta`) are written WITHOUT an end tag: html has
        # none, and this reader refuses one before it judges the binding.
        closing = "" if tag in parity._VOID_TAGS else f"</{tag}>"
        with pytest.raises(ExtractionRefused, match="the host will never fill"):
            html_fields(
                f"<html><body><p data-dashboard-field='lede'></p>"
                f"<{tag} data-dashboard-field='open'>{closing}</body></html>"
            )

    def test_html_after_the_svg_closes_is_unaffected(self) -> None:
        """Control: the svg rule must end with the svg, not leak onto the rest of the
        page."""
        assert html_fields(
            "<html><body><p data-dashboard-field='lede'></p><svg><path/></svg>"
            "<span data-dashboard-field='open'></span></body></html>"
        ) == {"lede", "open"}

    def test_a_binding_inside_svg_foreign_object_is_refused(self) -> None:
        """The sanitizer's default profile drops `foreignObject`, so a binding inside it
        reads as ordinary layout and renders nowhere."""
        with pytest.raises(ExtractionRefused, match="sanitizer removes"):
            html_fields(
                "<html><body><p data-dashboard-field='lede'></p>"
                "<svg><foreignObject data-dashboard-field='open'></foreignObject></svg>"
                "</body></html>"
            )

    def test_a_repeated_binding_on_one_element_is_refused(self) -> None:
        """Only the first is read, so the second is a field the contract must carry and
        the page will never show."""
        with pytest.raises(ExtractionRefused, match="repeated"):
            html_fields(
                "<html><body>"
                "<p data-dashboard-field='lede' data-dashboard-field='open'></p>"
                "</body></html>"
            )

    def test_every_attribute_the_host_strips_is_covered_by_one_of_the_two_readers(
        self,
    ) -> None:
        """Why this file reads the host rather than listing what it believes.

        A hand-kept mirror goes silently stale when the host's set grows. Every attribute
        the binder removes must be claimed by a reader here (outbound for ones that fetch,
        dropped for text the host cannot audit), so a new one fails by name.
        """
        source = self._BINDER.read_text(encoding="utf-8")
        removed = set(re.findall(r"attr\.localName === '([a-zA-Z-]+)'", source))
        listed = {
            match.group(1)
            for match in re.finditer(r"DISPLAYED_ATTRIBUTES = new Set\(\[([^\]]*)\]\)", source)
            for match in re.finditer(r"'([a-zA-Z-]+)'", match.group(1))
        }
        assert removed, "read no stripped attribute names out of the binder; the probe broke"
        assert listed, "read no DISPLAYED_ATTRIBUTES out of the binder; the probe broke"
        covered = parity._OUTBOUND | parity._DROPPED_TEXT_ATTRS
        missing = sorted((removed | listed) - covered)
        assert missing == [], (
            f"the host strips {missing} and neither reader in this package looks for "
            f"them, so a page carrying one passes every gate and renders a hole"
        )

    def test_the_control_tags_split_into_host_stripped_and_locally_refused(self) -> None:
        """Which control tags the HOST removes, and which this package refuses alone.

        `label`, `fieldset` and `option` survive sanitizing. Both halves are read out of
        the binder's own forbid list, so a tag moving between them fails here.
        """
        source = self._BINDER.read_text(encoding="utf-8")
        listed = re.search(r"FORBID_TAGS: \[([^\]]*)\]", source)
        assert listed is not None, "cannot find FORBID_TAGS in the binder; the probe broke"
        forbidden = set(re.findall(r"'([a-z]+)'", listed.group(1)))
        assert forbidden, "read no forbidden tags out of the binder; the probe broke"
        kept = parity.CONTROL_TAGS - forbidden
        assert kept == {"label", "fieldset", "option"}, (
            f"the host's forbid list moved: this package refuses {sorted(kept)} on its own "
            f"account, and the comment on CONTROL_TAGS names exactly those three"
        )

    def test_the_refused_placements_are_the_ones_the_browser_really_skips(self) -> None:
        """The list cannot be maintained by memory, so it is checked against the binder.

        The binder must still query the body, not the document, and still skip every tag
        named here before reading a field.
        """
        source = self._BINDER.read_text(encoding="utf-8")
        assert f"body.querySelectorAll('[{parity._BINDING}]')" in source, (
            "the binder no longer queries doc.body for bindings, so refusing a binding "
            "in the head may now be wrong"
        )
        for tag in parity._BINDING_SKIPPED_TAGS:
            assert f"=== '{tag}') continue" in source, (
                f"the binder no longer skips <{tag}>; this reader still refuses a " f"binding there"
            )

    #: The browser half, read rather than described. Same file the limits class reads.
    _BINDER = (
        ROOT / "website" / "src" / "pages" / "chat" / "command-center" / "dashboardDocument.ts"
    )

    def test_an_upper_case_tag_and_attribute_are_still_read(self) -> None:
        """Tag and attribute names are case-insensitive in html, so the reader must be.

        Read case-sensitively, an upper-case binding is invisible: the field reads as
        one the page does not use, and the equality gate passes over a real mismatch.
        """
        shouty = '<HTML><BODY><SPAN DATA-DASHBOARD-FIELD="lede"></SPAN></BODY></HTML>'
        assert html_fields(shouty) == {"lede"}

    def test_a_self_closing_void_element_leaves_the_page_readable(self) -> None:
        """The void half of the self-closing callback, which must NOT be routed through the
        end-tag rule: a void element is never pushed, so treating the slash as an end tag
        would complain about an end tag nobody wrote."""
        assert html_fields(
            '<html><body><img src="#" alt="a"/>'
            '<span data-dashboard-field="lede">x</span></body></html>'
        ) == {"lede"}

    def test_a_binding_padded_with_whitespace_is_refused(self) -> None:
        """The binder looks the attribute up EXACTLY, with no trimming.

        Trimming here would report the field as bound and let the equality gate agree with
        a contract the page cannot reach: the browser asks the data for `" lede "`, finds
        nothing, and writes the empty string.
        """
        with pytest.raises(ExtractionRefused, match="surrounding whitespace"):
            html_fields('<html><body><p data-dashboard-field=" lede "></p></body></html>')

    def test_a_binding_with_no_padding_is_still_read(self) -> None:
        """Control: the refusal is about the padding, not about the name."""
        assert html_fields('<html><body><p data-dashboard-field="lede"></p></body></html>') == {
            "lede"
        }

    def test_a_binding_on_a_void_element_is_refused(self) -> None:
        """A void element cannot hold text, so a binding on one is a field that reads as
        present and shows nothing.

        The host binds by assigning ``textContent``. On ``<br>`` that assignment is
        accepted and renders nothing, which is the same silence as a missing binding and
        harder to find, since the page and the contract agree.
        """
        with pytest.raises(ExtractionRefused, match="holds no text"):
            html_fields('<html><body><br data-dashboard-field="lede"/></body></html>')

    def test_a_binding_inside_a_comment_is_not_read(self) -> None:
        """A page documents its own fields beside the markup that binds them, so a
        comment must not count as a use."""
        seeded = PAGE.replace(
            '<span data-dashboard-field="open">',
            '<!-- data-dashboard-field="phantom" --><span data-dashboard-field="open">',
        )
        assert html_fields(seeded) == {"lede", "open"}


# reading the contract


class TestTheContractReader:
    def test_it_lists_every_key(self) -> None:
        assert contract_keys(_Card) == {"lede", "open"}

    @pytest.mark.parametrize("spelling", ["not_required", "total_false"])
    def test_a_contract_that_lets_a_field_be_omitted_is_refused(
        self, tmp_path: Path, spelling: str
    ) -> None:
        """An optional key is a field the html binds that the provider may leave out.

        `Unsaid` is what a missing value is for, and it keeps the key required.

        Written to a real module with `from __future__ import annotations`, as every
        contract here is: then `NotRequired` is a string and does NOT reach
        `__optional_keys__`, which an inline contract would hide.
        """
        module = tmp_path / "leaky_contract.py"
        declaration = (
            "class Leaky(TypedDict):\n"
            "    lede: str | Unsaid\n"
            "    open: NotRequired[str | Unsaid]\n"
            if spelling == "not_required"
            else "class Leaky(TypedDict, total=False):\n"
            "    lede: str | Unsaid\n"
            "    open: str | Unsaid\n"
        )
        module.write_text(
            "from __future__ import annotations\n"
            "from typing import NotRequired, TypedDict\n"
            "from kiro_crew.dashboard_templates import Unsaid\n\n" + declaration,
            encoding="utf-8",
        )
        name = f"leaky_contract_{spelling}"
        loaded = load_skill_script(name, module)
        # Registered for the duration: resolving a string annotation looks the defining
        # module up by name, and the helper deliberately leaves it out of `sys.modules`.
        with mock.patch.dict(sys.modules, {name: loaded}):
            with pytest.raises(ExtractionRefused, match="cannot make one optional"):
                contract_keys(loaded.Leaky)

    def test_a_contract_that_requires_every_field_is_read(self) -> None:
        """Control: an explicit `Required` marker is not an omission and must still pass,
        or the refusal above would reject a contract that is stricter than it needs to be.
        """

        class Strict(TypedDict):
            lede: Required[str | Unsaid]
            open: str | Unsaid

        assert contract_keys(Strict) == {"lede", "open"}

    def test_a_nested_contract_is_refused(self) -> None:
        """A card's data is FLAT text. A nested shape cannot reach the page at all, and
        accepting it here would let a contract declare structure the page can never
        read while the equality gate stayed green over the leaves."""
        with pytest.raises(ExtractionRefused, match="flat text"):
            contract_keys(_NestedCard)

    def test_a_list_valued_contract_is_refused(self) -> None:
        with pytest.raises(ExtractionRefused, match="flat text"):
            contract_keys(_ListCard)

    @pytest.mark.parametrize(
        "annotation",
        [
            Mapping[str, str],
            Sequence[str],
            frozenset[str],
            tuple[str, ...],
            deque,
            dict[str, str],
            list[str],
        ],
        ids=["Mapping", "Sequence", "frozenset", "tuple", "deque", "dict", "list"],
    )
    def test_every_container_spelling_is_refused_not_just_the_four(self, annotation: Any) -> None:
        """The reason this gate names what IS flat rather than what is not.

        A denylist of shapes misses `Mapping`, `Sequence`, `frozenset` or `deque`, whose
        origin is none of `dict`, `list`, `set` or `tuple`.
        """
        card = TypedDict("card", {"contract_version": str, "captured_at": str, "x": annotation})
        with pytest.raises(ExtractionRefused, match="flat text"):
            contract_keys(card)

    @pytest.mark.parametrize(
        "annotation",
        [str, int, str | Unsaid, int | Unsaid],
        ids=["str", "int", "str|Unsaid", "int|Unsaid"],
    )
    def test_the_flat_leaves_and_their_unsaid_unions_are_accepted(self, annotation: Any) -> None:
        """Control, and the half an allow-list can get wrong: every shape a template
        legitimately carries must still pass, including the optional spelling."""
        card = TypedDict("card", {"contract_version": str, "captured_at": str, "x": annotation})
        assert contract_keys(card) == {"contract_version", "captured_at", "x"}

    @pytest.mark.parametrize(
        "annotation",
        [float, bool, type(None), str | None, float | Unsaid],
        ids=["float", "bool", "None", "str|None", "float|Unsaid"],
    )
    def test_a_leaf_the_single_exit_cannot_render_is_refused(self, annotation: Any) -> None:
        """``card_data`` stringifies, and says what that means: an ``int`` or a ``str``.

        ``None`` would render as the word "None" (a gap that reads like data, breaking
        invariant 3), ``bool`` as a Python literal, and ``float`` is a ratio where a count
        should state its denominator.
        """
        card = TypedDict("card", {"contract_version": str, "captured_at": str, "x": annotation})
        with pytest.raises(ExtractionRefused, match="flat text"):
            contract_keys(card)

    def test_a_nested_value_hidden_inside_a_union_is_refused(self) -> None:
        """Nesting usually arrives by being made optional, so the check looks inside unions.

        Only the outer annotation would read as a union, neither a TypedDict nor a list.
        """
        with pytest.raises(ExtractionRefused, match="flat text"):
            contract_keys(_OptionalNestedCard)

    def test_a_list_hidden_inside_a_union_is_refused(self) -> None:
        with pytest.raises(ExtractionRefused, match="flat text"):
            contract_keys(_OptionalListCard)

    def test_the_sentinel_itself_is_not_mistaken_for_structure(self) -> None:
        """Control. Every optional field is a union carrying the sentinel, so a
        recursion that flagged the sentinel would refuse every real contract."""
        assert contract_keys(_Card) == {"lede", "open"}
        assert contract_keys(_MaybeCountCard) == {"failed"}

    def test_a_contract_with_no_keys_is_refused(self) -> None:
        with pytest.raises(ExtractionRefused, match="no keys"):
            contract_keys(_EmptyCard)
