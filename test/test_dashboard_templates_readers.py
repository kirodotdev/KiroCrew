"""The dashboard-template readers: what a page binds and what a contract names.

The parity check in ``test_dashboard_templates.py`` (invariant 1, and the registry and
scaffold gates that reuse it) compares the output of two readers, ``html_fields`` for the
page and ``contract_keys`` for the contract, so a reader that counts the wrong set lets
that check pass over a cell that renders blank. These tests pin both: the bindings the
page reader must find, the placements it refuses rather than misreads, and which contract
shapes the contract reader accepts (flat leaves and their ``Unsaid`` unions) or refuses
(optional keys, nesting, lists, no keys at all).
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


# Declared at MODULE level, not inside the cases that use them.
# ``typing.get_type_hints`` resolves an annotation against the defining module's
# globals, and a TypedDict defined inside a function has its own names nowhere in
# them -- so a nested probe class raises ``NameError`` and the case fails for a reason
# that has nothing to do with the rule it is testing.
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


# ---------------------------------------------------------------------------
# reading the page
# ---------------------------------------------------------------------------


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

        `body.querySelectorAll` never matches the body, and never matches the root above
        it, so a binding there is parsed, kept, and never visited. This is the placement a
        list of skipped kinds cannot catch: neither element is a kind of element, it is
        the thing being queried.
        """
        page = (
            f"<html><{root} data-dashboard-field='open'>"
            f"<p data-dashboard-field='lede'></p></{root}></html>"
        )
        with pytest.raises(ExtractionRefused, match="never matched"):
            html_fields(page)

    def test_markup_after_the_body_closes_is_still_read(self) -> None:
        """The parser reparents it into the body, so the binder does reach it.

        Refusing on stack position -- "the body is not currently open" -- would look like
        a tighter rule and would reject a page the browser binds perfectly well. Only the
        head is genuinely outside the binder's reach, and that has its own case.
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
        """The binder queries the body, so a binding parked in the head is never visited
        and no visible element was ever placed for that value.

        The element has to be one html really keeps in the head. A ``<span>`` written
        between ``<head>`` and ``</head>`` does not stay there -- the tree builder closes
        the head at it and reparents it into the body, where the binder finds it -- so
        that page is bindable and refusing it would be wrong.
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
        """Why the head is NOT read off the open-element stack, even though every end tag is
        now required.

        html reparents content the head may not hold into the body, so this page is well
        formed by every rule in this reader and the browser still puts the `<span>` in the
        body and binds it. A stack read would find `head` open and refuse it. So the head
        ends at the first element html does not permit there, which is where a browser's
        tree builder ends it.
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

        The binder walks matches in document order and assigns ``textContent``. The outer
        element is reached first, and that assignment removes the inner bound element from
        the document -- so the inner value is still written, to a node nothing renders.
        Counted, the equality gate passes on a contract with a field that never appears.
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
        """The property the open-element stack exists for, and the one the subtraction keeps.

        Requiring explicit end tags removed the need to model where html closes an element
        for the author. It did not remove the need to know which element a binding sits
        inside: a `span` inside a bound `p`, a bound `div` inside a bound `div`, a cell of a
        nested table inside the outer cell. The binder assigns `textContent` in document
        order, so the outer assignment detaches the inner element before the loop reaches it.
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
        """Html does NOT honour ``<span/>``: the element stays open, and a reader that
        closed it there would read the next binding as a sibling of one the browser sees it
        nested inside.

        The slash reaches a different parser callback, which is why it needs its own case.
        It is refused rather than interpreted, by the same rule as any other element left
        open -- the author writes the end tag.
        """
        with pytest.raises(ExtractionRefused, match="innermost open element|still open"):
            html_fields(
                "<html><body><span data-dashboard-field='lede'/>"
                "<span data-dashboard-field='open'></span></body></html>"
            )

    def test_an_end_tag_matching_no_open_element_is_refused(self) -> None:
        """A stray `</div>` inside a bound `<p>`.

        A browser discards it and keeps the `p` open, so the binding after it really is
        nested. An earlier reader reproduced that, which meant its nesting answer depended
        on its own repair being right. Refusing the markup makes the answer depend on
        nothing: the author deletes one end tag or writes the matching one.
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

        ``<text>`` paints; the same ``<text>`` under a definitions container paints
        nothing. So no set of tag names answers the question, and the reader refuses the
        whole foreign subtree instead of deciding per element. Reached and fillable are
        different properties, and this is the gap a reachability check alone still leaves.
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

        The host calls DOMPurify with only `ADD_TAGS: ['style']`, so its default allow-list
        applies and a name outside it goes, taking the binding with it -- the cell does not
        render blank, it disappears, and the equality gate stays green over a field that is
        absent from the page entirely.

        An allow-list is the only shape that answers this, because what is being detected is
        exactly a name nobody listed.
        """
        with pytest.raises(ExtractionRefused, match="not an html element"):
            html_fields(
                f"<html><body><p data-dashboard-field='lede'></p>"
                f"<{tag} data-dashboard-field='open'></{tag}></body></html>"
            )

    @pytest.mark.parametrize("tag", sorted(parity._OBSOLETE_TAGS - {"plaintext"}))
    def test_a_binding_on_an_obsolete_element_is_refused(self, tag: str) -> None:
        """Refused on this package's own ground, not on a guess about the sanitizer.

        A dashboard renders a fact and an obsolete element is not how this product renders
        one. Saying it that way is what makes the set answerable: the other ground --
        "DOMPurify drops it" -- needs the allow-list this reader already records as unreadable
        from here, and guessing at it refuses pages that render.
        """
        with pytest.raises(ExtractionRefused, match="marks obsolete"):
            html_fields(
                f"<html><body><p data-dashboard-field='lede'></p>"
                f"<{tag} data-dashboard-field='open'></{tag}></body></html>"
            )

    def test_plaintext_is_refused_however_the_parser_tokenizes_it(self) -> None:
        """`plaintext` is the one member of the obsolete set whose refusal is not fixed here.

        Whether `html.parser` treats what follows it as text -- its own end tag included, so
        the page ends with it open -- or reports that end tag, differs by platform: this case
        is answered by the structural rule on one and by the obsolete rule on another. Both
        are refusals and this package has an opinion about neither tokenization, so the
        assertion is that the binding is refused, not which sentence says so.
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
        on nothing -- and listing it made this reader call that page bindable.
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

        The alternative is to model html: which end tags an author may omit, the scope
        variant each end tag is resolved in, which formatting elements a misnested end tag
        crosses. That accepts more pages, and it makes the reader's nesting answer depend on
        its own repair being right -- a scope set slightly wrong reads a child as a sibling
        and says so in a precise sentence.

        This reader runs while someone here is writing the page, and the only thing it has to
        answer is where a binding lands. So it asks for markup it can follow: the rule cannot
        be wrong about scope, and the author's remedy is one end tag on the line named.
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

        The binder matches on the attribute alone and fills it regardless, which makes the
        binding reachable and invisible at once -- the same pair `title` and svg already
        cost this reader. The set is read from the module so a sixth element cannot join it
        uncovered.
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

        `<body><title data-dashboard-field="open">` is matched by the binder -- it selects
        on the attribute alone -- and the browser's default sheet hides `title`, so the
        value is written and nothing appears. Every element html permits in the head
        renders none of its text content anywhere, which is why one set answers both
        questions.

        Several of these are already refused by an earlier rule -- `base`, `link` and
        `meta` hold no text at all, `style` is stepped over by the binder -- so the
        assertion is that the binding is refused, not which sentence says so. The set is
        read from the module rather than retyped, so a tenth element cannot be added to it
        without this case covering it.
        """
        # A void element among these (`base`, `link`, `meta`) is written WITHOUT an end tag:
        # html has none for it, and this reader now says so before it judges the binding.
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

        A hand-kept mirror of another file's set has one failure mode and it is silent:
        the set grows on that side and every gate here still passes. Nothing in this
        package can see that, so each omission has to be found by a person reading both
        files, and finding one leaves the next equally invisible.

        So the comparison itself is the gate. Every attribute the binder's own sweep
        removes must be claimed by exactly one reader here -- outbound for the ones that
        fetch, dropped for the ones whose text the host's scan cannot audit. An entry added
        on that side fails here, by name, instead of reaching a reviewer.
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

        Saying the host strips all of them reads as one rule and is two. Three of them --
        `label`, `fieldset`, `option` -- survive sanitizing, so a reader who believed the
        single claim would expect the host to protect a page this package rejects, and a
        reader checking the claim would find it false and trust the rest less.

        Both halves are read out of the binder's own forbid list, so a tag moving between
        them fails here instead of leaving a comment quietly wrong.
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

        The cost of a rule like this is that it silently stops matching the host. So the
        host's own source is read: the binder must still query the body rather than the
        document, and must still skip every tag named here before reading a field. A
        change on their side fails here instead of rendering a blank cell.
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


# ---------------------------------------------------------------------------
# reading the contract
# ---------------------------------------------------------------------------


class TestTheContractReader:
    def test_it_lists_every_key(self) -> None:
        assert contract_keys(_Card) == {"lede", "open"}

    @pytest.mark.parametrize("spelling", ["not_required", "total_false"])
    def test_a_contract_that_lets_a_field_be_omitted_is_refused(
        self, tmp_path: Path, spelling: str
    ) -> None:
        """An optional key is a field the html binds that the provider may leave out.

        The cell then renders blank while the equality gate stays green over the names --
        the exact failure this package exists to prevent, reached through the gate itself.
        `Unsaid` is what a missing value is for, and it keeps the key required.

        Written to a real module compiled with `from __future__ import annotations`, the
        way every contract in this tree is, because that is what decides the answer: under
        it a `TypedDict` sees its annotations as strings, so `total=False` still reaches
        `__optional_keys__` while `NotRequired` does NOT and the key reads as required. A
        contract defined inline in this test file resolves differently and would have let
        the `NotRequired` half pass while the shipped spelling leaked.
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

        Refusing a list of shapes accepts every shape nobody listed, and a container can
        be spelled many ways whose origin is none of `dict`, `list`, `set` or `tuple` --
        `Mapping`, `Sequence`, `frozenset`, `deque`. Each would have reached the page as
        one text field it cannot render.
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

        Each of these reaches the page as something no template should state. ``None``
        arrives as the word "None", which is invariant 3 broken in its worst form -- a gap
        rendering as text that reads like data. ``bool`` arrives as a Python literal.
        ``float`` is a ratio, and a count states its denominator instead.
        """
        card = TypedDict("card", {"contract_version": str, "captured_at": str, "x": annotation})
        with pytest.raises(ExtractionRefused, match="flat text"):
            contract_keys(card)

    def test_a_nested_value_hidden_inside_a_union_is_refused(self) -> None:
        """The hole the outer-annotation-only check left, and the common way in.

        Nesting usually arrives by being made optional. A check that looks only at the
        outermost annotation sees a union -- neither a TypedDict nor a list -- and lets
        the nested shape through, so the gate reports one flat key for a value the page
        can never render.
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
