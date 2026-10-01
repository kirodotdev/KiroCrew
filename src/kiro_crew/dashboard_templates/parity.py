"""The gates on a template, in one implementation both the tests import.

``mypy`` checks the provider: a :class:`~typing.TypedDict` in, the contract out,
required keys and all. It cannot see inside an html file, so the html end of the
agreement needs a reader of its own. This is that reader, plus the two structural
rules a contract must satisfy.

Written with the stdlib html parser rather than a regular expression, deliberately.
An attribute scan by regex mis-reads the cases that matter -- an attribute inside a
comment, a value quoted with the other quote character, a tag name in upper case --
and each of those makes the gate QUIETLY incomplete, which is worse than a gate that
fails: a field the extractor missed reads as a field the html does not use, so the
equality assertion passes over a real mismatch.

Every function here refuses rather than returning a partial answer. A partial answer
is indistinguishable from a template that genuinely uses fewer fields, and that is
the one direction an equality assertion cannot catch on its own.
"""

from __future__ import annotations

import types
import typing
from html.parser import HTMLParser
from typing import Any, Final

from kiro_crew.dashboard_templates import Unsaid

__all__ = [
    "CONTROL_TAGS",
    "ExtractionRefused",
    "contract_keys",
    "control_tags_used",
    "denominator_gaps",
    "html_fields",
    "dropped_text_attributes",
    "outbound_references",
]

#: The binding attribute the host reads. One attribute, one field, one value.
_BINDING: Final[str] = "data-dashboard-field"

#: Attributes that reach OUTSIDE the page. ``src`` and ``srcset`` always do; ``href``
#: does unless it is a same-document fragment. Matched on the attribute's local name, so
#: an SVG ``xlink:href`` is read as an ``href`` rather than slipping past a check on the
#: plain spelling.
_OUTBOUND: Final[frozenset[str]] = frozenset({"src", "srcset", "href"})

#: Attributes the host removes from a card because they render as text its own scan
#: cannot read -- a broken image's ``alt``, a hover ``title``, a list marker's ``start``.
#: They reach nothing outside the page, so they are not outbound; they are simply
#: deleted, which makes authoring one a way to put a fact where the reader never sees it.
_DROPPED_TEXT_ATTRS: Final[frozenset[str]] = frozenset({"alt", "title", "start", "value"})

#: Elements that cannot hold text. The host binds by assigning ``textContent``, which on
#: one of these is accepted and renders nothing, so a binding here is a field that reads
#: as present and shows nothing -- the same silence as a missing binding, harder to see.
_VOID_TAGS: Final[frozenset[str]] = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

#: Tags that make a page a control surface, refused for two different reasons.
#:
#: Most of them the host strips, so a template authoring one ships a layout with a hole
#: in it and an author who believes the button exists. Three -- ``label``, ``fieldset``
#: and ``option`` -- the host KEEPS, and this package refuses them on its own account:
#: they render as the furniture of a control, so a page carrying them tells a reader an
#: action is available where none is. :func:`control_tags_used` does not distinguish the
#: two, because the answer to both is the same, but the claim that the host removes every
#: one of these would be false and ``TestTheControlTagsSplit`` holds the real division.
#:
#: A dashboard template states facts and offers no actions: a decision goes through the
#: product's own question and approval surfaces, which are outside the rendered frame and
#: carry the identity of the session that owns them.
CONTROL_TAGS: Final[frozenset[str]] = frozenset(
    {
        "form",
        "input",
        "button",
        "textarea",
        "select",
        "option",
        "label",
        "fieldset",
        "script",
        "iframe",
        "object",
        "embed",
        "link",
        "meta",
        "base",
        "template",
        "noscript",
    }
)

#: Tags the host RENDERS but its binder steps over, so a binding on one never receives
#: text. Read out of the binder itself, which skips ``style`` before it reads the field
#: name: writing provider text into live CSS is its own deliberate refusal.
_BINDING_SKIPPED_TAGS: Final[frozenset[str]] = frozenset({"style"})

#: Elements html marks NON-CONFORMING -- its own obsolete list -- that this reader also
#: refuses a binding on. The ground is this package's, not the sanitizer's: a dashboard
#: renders a fact, and an obsolete element is not how this product renders one. Stating it
#: that way is what makes the set answerable, because the alternative ground -- "DOMPurify
#: drops it" -- needs the allow-list :data:`_KNOWN_HTML` already records as unreadable here.
#:
#: They stay in :data:`_KNOWN_HTML`, which is html's index and says nothing about whether a
#: tag should be used; this set is the separate question of whether a card may bind one.
#:
#: The formatting elements among the obsolete ones (``big``, ``font``, ``nobr``, ``strike``,
#: ``tt``) are deliberately NOT here: a browser paints their text, a sanitizer that keeps
#: them is plausible, and refusing them would reject a page that renders.
_OBSOLETE_TAGS: Final[frozenset[str]] = frozenset(
    {
        "basefont",
        "bgsound",
        "dir",
        "frameset",
        "listing",
        "noframes",
        "plaintext",
        "xmp",
    }
)

#: Tags the host's sanitizer removes outright and that :data:`CONTROL_TAGS` does not
#: already name. A binding on one leaves with the element that carried it.
#:
#: ``foreignobject`` is here rather than with the controls because it is not one: it is an
#: SVG container for html, and the sanitizer's default profile drops it, so a binding
#: parked inside looks like ordinary layout and never renders.
_BINDING_STRIPPED_TAGS: Final[frozenset[str]] = frozenset(
    {"frame", "applet", "animate", "set", "foreignobject"}
)

#: Where the binder looks. It queries the BODY, so a binding the source puts in the head
#: is parsed, kept, and never visited.
_HEAD: Final[str] = "head"

#: What html permits INSIDE the head, which is what decides where the head ends. An
#: author who omits ``</head>`` is writing valid html and the tree builder closes the
#: head for them at the first element that cannot live there; ``html.parser`` reports
#: tags as written and synthesizes no end tag, so the same closed set has to be applied
#: here. Taken from the spec's "in head" insertion mode, which names exactly these.
_HEAD_CONTENT: Final[frozenset[str]] = frozenset(
    {
        "base",
        "basefont",
        "bgsound",
        "link",
        "meta",
        "noscript",
        "script",
        "style",
        "template",
        "title",
    }
)

#: The subtree the binder walks.
_BODY: Final[str] = "body"

#: Elements the binder can never match, because it asks the body for its DESCENDANTS and
#: neither the body nor the root is one of its own descendants.
_ROOTS: Final[frozenset[str]] = frozenset({"html", _BODY})

#: Elements whose content is parsed as FOREIGN, where ``<x/>`` really does close. Outside
#: these, html ignores the slash on a non-void tag and the element stays open.
_FOREIGN_ROOTS: Final[frozenset[str]] = frozenset({"svg", "math"})

#: Every element name html defines, including the obsolete ones a parser still recognises.
#: Used for ONE question: is this tag something the host's sanitizer could keep at all?
#:
#: The host calls DOMPurify with only ``ADD_TAGS: ['style']``, so its DEFAULT allow-list
#: applies and anything outside it -- a custom element, a misspelled tag -- is removed
#: along with the binding it carried, leaving a green gate over a cell that never appears.
#: An allow-list is the only shape that answers that, because the thing being detected is
#: precisely a name nobody listed.
#:
#: This is the one rule here NOT read from the authority that governs it: DOMPurify's own
#: default list lives in ``node_modules``, which this package cannot import and a Python
#: reader cannot parse. So it is written as html's element index, which is WIDER than
#: DOMPurify's list -- deliberately, because being wider means this rule never refuses a
#: page the host would have rendered. The narrower cases are already covered element by
#: element above (controls, sanitizer-stripped tags, fallback content).
#:
#: Obsolete elements a parser still recognises are IN, because the reader's other sets name
#: several of them and html still defines how to parse them. An element the spec has DROPPED
#: is not: ``keygen`` is gone from html and from every browser, so a binding on one is a
#: binding on nothing.
#:
#: A test holds it to every tag the rest of this module names, so it cannot drift out of
#: agreement with its own neighbours.
_KNOWN_HTML: Final[frozenset[str]] = frozenset("""
    a abbr address applet area article aside audio b base basefont bdi bdo bgsound big
    blockquote body br button canvas caption center cite code col colgroup data datalist
    dd del details dfn dialog dir div dl dt em embed fieldset figcaption figure font
    footer form frame frameset h1 h2 h3 h4 h5 h6 head header hgroup hr html i iframe img
    input ins kbd label legend li link listing main map mark marquee menu meta
    meter nav nobr noframes noscript object ol optgroup option output p param picture
    plaintext pre progress q rp rt ruby s samp script search section select slot small
    source span strike strong style sub summary sup table tbody td template textarea
    tfoot th thead time title tr track tt u ul var video wbr xmp
    """.split())

#: Elements whose text content the spec defines as FALLBACK: it is painted only by a user
#: agent that cannot use the element itself, so a browser that supports the element shows
#: none of it. The binder matches on the attribute alone and fills it regardless, which
#: makes a binding here reachable and invisible at the same time.
#:
#: Named as the spec's own category rather than one tag at a time. ``object`` and
#: ``iframe`` are already in :data:`CONTROL_TAGS`, so a page carrying one is refused by the
#: control reader too -- they are here so this reader agrees with itself rather than
#: relying on its neighbour. ``embed`` needs no entry: it is void and already refused for
#: holding no text at all.
#: An ENUMERATION, not a set derived from one rule -- worth saying plainly, because the
#: rest of this reader avoids enumerations for exactly the reason this one keeps growing.
#: The spec marks this content fallback element by element, in each element's own content
#: model, so there is no single list to read and no membership test a reader can compute.
#:
#: Left as an enumeration rather than inverted into an allow-list of elements that DO paint
#: text, because that list is most of html: getting it wrong refuses a page the browser
#: renders, which is worse than the miss here. A tag absent from this set costs one blank
#: cell on a page under review; a tag wrongly absent from an allow-list costs every page
#: that uses it.
_FALLBACK_TEXT_TAGS: Final[frozenset[str]] = frozenset(
    {"canvas", "audio", "video", "object", "iframe", "meter", "progress"}
)

#: A binding inside foreign content is refused whatever it sits on, and no list of svg
#: tags decides it. Whether svg paints a string is not a property of the element holding
#: it: ``<text>`` paints, the same ``<text>`` under ``<defs>``, ``<clipPath>``, ``<mask>``,
#: ``<pattern>``, ``<marker>`` or ``<symbol>`` paints nothing, ``<tspan>`` and
#: ``<textPath>`` paint only under a ``<text>``, and ``<title>``/``<desc>`` are the
#: accessible name and never glyphs. So the answer needs the whole ancestor chain and its
#: rendering context, which a tag-name scanner does not have.
#:
#: Refusing the subtree costs an author nothing: the only writer this package ships emits
#: bindings on ``<span>`` in ordinary flow content, and a value that must sit over a
#: drawing goes in a sibling ``<span>`` positioned above it.
_FOREIGN_REFUSAL: Final[str] = (
    "inside svg, where whether a string is painted depends on the ancestor chain, not "
    "on the element holding the binding; bind a <span> in the html flow instead"
)

#: The suffix a count's denominator field carries. ``failed`` needs ``failed_of``.
_DENOMINATOR_SUFFIX: Final[str] = "_of"


class ExtractionRefused(Exception):
    """The template is not something this reader can answer about.

    Raised instead of returning what it managed to find. A short set reads as a
    template using fewer fields, which is exactly the mismatch the gate exists to
    catch, so an unreadable page has to be loud.
    """


class _FieldScanner(HTMLParser):
    """Collects every ``data-dashboard-field`` value, and the tags used.

    ``convert_charrefs`` stays on (the default) because the value is an attribute and
    the host reads the DECODED attribute, so decoding here matches what it binds.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fields: list[str] = []
        self.tags: set[str] = set()
        self.outbound: set[tuple[str, str]] = set()
        self.dropped_text: set[tuple[str, str]] = set()
        self.empty_bindings = 0
        self.unbindable: list[str] = []
        #: End tags that did not close the innermost open element, each named with what
        #: was open instead. One complaint covers every way the source and the browser's
        #: tree can come apart: an omitted end tag, a misnested one, a stray one.
        self.unclosed: list[str] = []
        #: Elements still open when the page ended, filled in by :func:`_scan` once the
        #: feed is closed. Held separately from the stack so a reader of the refusals
        #: below never reaches into the parser's own bookkeeping.
        self.open_at_end: list[str] = []
        #: Whether the cursor is still inside the head. NOT read off the open-element
        #: stack, and the strict end-tag rule below does not make it readable from there:
        #: html reparents content the head may not hold into the BODY, so
        #: ``<head><span data-dashboard-field=...></span></head>`` is well formed by every
        #: rule here and the browser still binds that span. A stack read would refuse it.
        #: This is a content-model question, not a nesting one, and it is answered with a
        #: set this reader keeps for its own sake.
        self._head_depth = 0
        #: Every open element around the cursor as ``(tag, field or None)``, innermost
        #: last. A pair of counters cannot survive an unmatched ``</div>``: it pops a
        #: bound element that is still open, and the next binding then reads as a
        #: sibling. Holding the tag names lets an end tag that closes nothing be ignored,
        #: which is what a browser does with it.
        #:
        #: Why any of this is tracked: the binder walks matches in document order and
        #: assigns ``textContent``, which replaces the outer element's children, so an
        #: inner bound element is detached before the loop reaches it and its value is
        #: written to a node outside the document.
        self._stack: list[tuple[str, str | None]] = []

    def _in_foreign(self, tag: str) -> bool:
        """Is ``tag`` being read as foreign content -- svg or mathml?

        Answered from the open-element stack rather than a depth counter, for the reason
        the counter was replaced everywhere else in this scanner: an end tag matching
        nothing open closes nothing, and a counter cannot tell. ``<svg></math><circle
        data-dashboard-field=...>`` decremented a count that ``</math>`` never raised,
        and the binding then read as ordinary html and was accepted. Unwinding the stack
        cannot make that mistake -- the stray end tag finds no ``math`` open and leaves
        the ``svg`` where it is.
        """
        if tag in _FOREIGN_ROOTS:
            return True
        return any(open_tag in _FOREIGN_ROOTS for open_tag, _ in self._stack)

    def _unreachable(self, tag: str) -> str | None:
        """Why the binder will never write to this element, or ``None`` if it will.

        Recording the binding anyway would make the equality gate PASS on a page whose
        cell stays blank at every render -- the precise failure this whole package is
        built to make impossible, reached through its own reader.

        The first question asked is whether the binder VISITS this element at all, rather
        than whether the element is one of the kinds it is known to step over. Listing
        what is skipped means every placement nobody listed is accepted, and the binder's
        reach has a short definition: ``body.querySelectorAll`` walks the descendants of
        the body, which excludes the body and the root themselves.
        """
        if tag in _ROOTS:
            return (
                f"on <{tag}> itself; the binder queries the body's DESCENDANTS, so the "
                f"root and the body are never matched"
            )
        # Deliberately NOT "is the body currently open": the parser reparents markup
        # before and after the tag into the body, so the head check below is the only
        # outside-the-body case that is real. Refusing on stack position would reject
        # pages the browser binds perfectly well.
        if self._head_depth:
            return f"inside <{_HEAD}>, and the binder queries the body"
        if tag in _BINDING_SKIPPED_TAGS:
            return f"on <{tag}>, which the binder steps over rather than filling"
        if tag in _OBSOLETE_TAGS:
            return (
                f"on <{tag}>, which html marks obsolete; a dashboard renders a fact and an "
                f"obsolete element is not how this product renders one, and the host's "
                f"sanitizer is under no obligation to keep it"
            )
        if tag in _BINDING_STRIPPED_TAGS:
            return f"on <{tag}>, which the sanitizer removes along with the binding"
        if tag in _VOID_TAGS:
            return f"on <{tag}>, which holds no text, so the bound value renders nowhere"
        if self._in_foreign(tag):
            return f"on <{tag}> {_FOREIGN_REFUSAL}"
        if tag in _FALLBACK_TEXT_TAGS:
            return (
                f"on <{tag}>, whose text is FALLBACK content, painted only where the "
                f"element itself cannot be used; the binder fills it and a browser that "
                f"supports <{tag}> shows nothing"
            )
        if tag not in _KNOWN_HTML:
            # Asked LAST of the element-name questions, so every tag with a more specific
            # answer above gets that answer instead of this one.
            return (
                f"on <{tag}>, which is not an html element the host's sanitizer keeps -- a "
                f"custom element or a misspelling is removed with the binding on it, so the "
                f"cell does not render blank, it disappears"
            )
        if tag in _HEAD_CONTENT:
            # Asked WHEREVER the element sits, not only in the head. Every element html
            # permits in the head renders none of its text content anywhere -- the
            # browser's default sheet hides `title` and `style`, `script` and `template`
            # content is never painted -- and the binder matches on the attribute alone,
            # so `<body><title data-dashboard-field=...>` is reached and still invisible.
            return (
                f"on <{tag}>, which renders none of its text wherever it sits, so the "
                f"binder fills it and nothing appears"
            )
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self.tags.add(tag)
        if tag == _HEAD:
            self._head_depth += 1
        elif self._head_depth and tag not in _HEAD_CONTENT:
            # The head ends HERE, at the first element html does not permit in it, the same
            # place a browser's tree builder ends it -- which then reparents this element
            # into the body and binds it. Waiting for ``</head>`` would refuse a binding the
            # host fills, on markup no end-tag rule can object to.
            self._head_depth = 0
        opened: str | None = None
        bindings_here = 0
        for name, value in attrs:
            lowered = name.lower()
            if lowered == _BINDING:
                raw = value or ""
                text = raw.strip()
                bindings_here += 1
                if bindings_here > 1:
                    # The parser keeps the FIRST of repeated attributes and the binder
                    # reads that one, so a second name here is a field the contract is
                    # then required to carry and the page will never show.
                    self.unbindable.append(
                        f"{text or '<empty>'!r} is a repeated {_BINDING} on one "
                        f"<{tag}>; only the first is read"
                    )
                    continue
                if not text:
                    # A binding with no name binds nothing: the host looks the empty
                    # string up in the data and writes the empty string back, so the
                    # element renders blank forever with nothing to trace it to.
                    self.empty_bindings += 1
                    continue
                if raw != text:
                    # The binder looks the attribute up EXACTLY, without trimming, so
                    # " lede " is a key the data never has and the element renders
                    # blank. Trimming here would report the field as bound and let the
                    # equality gate agree with a contract the page cannot reach.
                    self.unbindable.append(
                        f"{raw!r} carries surrounding whitespace; the binder looks the "
                        f"name up exactly, so it never matches {text!r}"
                    )
                    continue
                unreachable = self._unreachable(tag)
                if unreachable is not None:
                    self.unbindable.append(f"{text!r} is bound {unreachable}")
                    continue
                outer = next(
                    (field for _, field in reversed(self._stack) if field is not None), None
                )
                if outer is not None:
                    self.unbindable.append(
                        f"{text!r} is bound inside the element bound to {outer!r}; "
                        f"filling the outer one replaces its children, so this value is "
                        f"written to a detached node"
                    )
                    continue
                self.fields.append(text)
                opened = text
                continue
            # The local name, so ``xlink:href`` is judged as an ``href``.
            local = lowered.rsplit(":", 1)[-1]
            if local in _DROPPED_TEXT_ATTRS:
                self.dropped_text.add((lowered, (value or "").strip()))
                continue
            if local not in _OUTBOUND:
                continue
            target = value or ""
            # Judged on the RAW value, because the host's own test is
            # ``!value.startsWith('#')`` with no trimming: a leading space makes
            # `` #top`` fail that test and the attribute is removed. Stripping first
            # would call it a same-document fragment and report nothing.
            if local == "href" and target.startswith("#"):
                continue
            self.outbound.add((lowered, target.strip()))
        if tag not in _VOID_TAGS:
            # Pushed after the attribute loop so the element does not count as nested
            # inside itself. A void element opens no scope, so nothing can sit in it.
            self._stack.append((tag, opened))

    def handle_endtag(self, tag: str) -> None:
        """Every end tag must close the innermost open element, and nothing else.

        This is STRICTER than html, deliberately. html lets an author omit many end tags and
        repairs a misnested one, so accepting what a browser accepts means modelling both: a
        table of omission rules, the scope variant each end tag is resolved in, and an unwind
        that notices which formatting elements a stray end tag crossed.

        That machinery is declined because of what this reader is FOR. It runs at authoring
        time on a page someone in this repository is writing, and its whole job is to say
        where a binding lands. A tree builder is a lot of machinery to answer that, and its
        failure mode is silent: a scope set slightly wrong reads a child as a sibling and
        reports it in a precise sentence. This rule cannot be wrong about scope at all, and
        the author's remedy is one end tag, named, on the line that needs it.

        So a page that renders and is refused here is not a bug in the page. It is this gate
        asking for markup a reader can follow without guessing, which a dashboard page can
        always supply.
        """
        tag = tag.lower()
        if tag == _HEAD and self._head_depth:
            self._head_depth -= 1
        if tag in _VOID_TAGS:
            self.unclosed.append(
                f"</{tag}> is an end tag for a void element, which html has none of"
            )
            return
        if not self._stack:
            self.unclosed.append(f"</{tag}> closes nothing; no element is open here")
            return
        innermost = self._stack[-1][0]
        if innermost != tag:
            self.unclosed.append(
                f"</{tag}> does not close the innermost open element, which is " f"<{innermost}>"
            )
            return
        self._stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """``<span/>`` written self-closing, which html does NOT honour.

        The parser reports one event here, but a browser closes the element only for a
        void tag or inside foreign content (svg, mathml). Everywhere else the slash is
        ignored and the element stays OPEN, so the next binding is nested inside this
        one. Closing it here would hide exactly that nesting from the reader above.
        """
        lowered = tag.lower()
        was_foreign = self._in_foreign(lowered)
        self.handle_starttag(tag, attrs)
        if lowered not in _VOID_TAGS and was_foreign:
            # Only the case where the start tag actually PUSHED. A void tag is never
            # pushed, so routing it through the end-tag rule would report it as an end
            # tag for a void element -- a complaint about markup nobody wrote.
            self._stack.pop()


def _scan(html: str) -> _FieldScanner:
    scanner = _FieldScanner()
    scanner.feed(html)
    scanner.close()
    scanner.open_at_end = [tag for tag, _ in scanner._stack]
    return scanner


def html_fields(html: str) -> set[str]:
    """Every field *html* binds, as a set.

    Refuses a page that binds nothing -- an extractor returning an empty set would
    make the equality gate report the contract as entirely over-specified, which sends
    the reader to delete the contract instead of fixing the page.
    """
    scanner = _scan(html)
    # Structure FIRST. Every other judgment below is computed against the open-element
    # stack, and an end tag that did not match what was open means the stack stops
    # describing the document from that point on. A verdict drawn from it after that --
    # which binding is nested in which -- is a guess wearing a precise message.
    if scanner.unclosed:
        listed = "; ".join(scanner.unclosed)
        raise ExtractionRefused(
            f"{len(scanner.unclosed)} end tag(s) do not match the element they close: "
            f"{listed}. This reader requires every element to be closed where it opens, "
            f"which is stricter than html on purpose: what it has to answer is where a "
            f"binding lands, and it declines to reproduce a tree builder to do it. Close "
            f"each element explicitly."
        )
    if scanner.open_at_end:
        left = ", ".join(f"<{tag}>" for tag in scanner.open_at_end)
        raise ExtractionRefused(
            f"the page ends with {len(scanner.open_at_end)} element(s) still open: {left}. "
            f"An unclosed element makes every following binding read as its child, so "
            f"where the value lands is not readable from the source. Close each one."
        )
    if scanner.empty_bindings:
        raise ExtractionRefused(
            f"{scanner.empty_bindings} {_BINDING} attribute(s) carry no field name; "
            "such an element renders blank forever and names nothing to fix"
        )
    if scanner.unbindable:
        listed = "; ".join(scanner.unbindable)
        raise ExtractionRefused(
            f"{len(scanner.unbindable)} binding(s) the host will never fill: {listed}. "
            "Counting them would let the equality gate pass on a page whose cell is "
            "blank at every render. Move each onto a visible element inside the body."
        )
    if not scanner.fields:
        raise ExtractionRefused(
            f"no {_BINDING} bindings found; either the page renders no data or this "
            "reader cannot see it, and those must not look alike"
        )
    return set(scanner.fields)


def control_tags_used(html: str) -> set[str]:
    """The control tags *html* contains. Empty is the only acceptable answer.

    Tags only. Attributes that reach outside the page are a separate rule with a
    separate reader, :func:`outbound_references`, so a reader of either gate is never
    surprised by what it silently also covers.
    """
    return _scan(html).tags & CONTROL_TAGS


def outbound_references(html: str) -> set[tuple[str, str]]:
    """Every ``(attribute, value)`` in *html* that reaches outside the page.

    A ``src`` or ``srcset`` of any value and an ``href`` that is not a same-document
    ``#fragment``, with the attribute matched on its local name so ``xlink:href`` counts.
    The host strips these like it strips a control, so a page carrying one ships a hole
    where the author believes an image or a link is. Empty is the only acceptable answer.
    """
    return _scan(html).outbound


def dropped_text_attributes(html: str) -> set[tuple[str, str]]:
    """Every ``(attribute, value)`` the host DELETES because it carries unreadable text.

    A separate reader from :func:`outbound_references` because the reason differs and a
    reader of either should not be surprised by what the other silently also covers:
    these reach nothing outside the page. The host removes them from a card so that no
    fact reaches the reader through a channel its own text scan cannot audit -- which
    means an ``alt`` or a ``title`` carrying part of the answer is a fact deleted before
    anyone reads it. Empty is the only acceptable answer.
    """
    return _scan(html).dropped_text


# --------------------------------------------------------------------------
# the contract end
# --------------------------------------------------------------------------


def _is_typed_dict(annotation: Any) -> bool:
    return (
        isinstance(annotation, type)
        and issubclass(annotation, dict)
        and hasattr(annotation, "__annotations__")
    )


#: The only leaves a flat card can carry. An ALLOW-list on purpose: naming the shapes
#: that cannot reach the page means every shape nobody thought of is accepted, and
#: ``Mapping``, ``Sequence``, ``frozenset`` and ``deque`` are all containers whose origin
#: is not ``dict``, ``list``, ``set`` or ``tuple``. Anything not named here is refused
#: until someone decides how it becomes one string.
#:
#: The set is what :func:`~kiro_crew.dashboard_templates.card_data` already says it
#: handles: a sentinel, or something stringified, "which for this package's contracts
#: means an ``int`` or a ``str``". Nothing else belongs here. ``None`` would reach the
#: page as the word ``None``, which is invariant 3 broken in its worst form -- a gap
#: rendering as something that reads like data. ``bool`` would render a Python literal.
#: ``float`` is a ratio, and a template states a count with its denominator instead.
_FLAT_LEAVES: Final[tuple[type, ...]] = (str, int, Unsaid)


def _is_structured(annotation: Any) -> bool:
    """Whether *annotation* names a shape a flat card cannot carry, at ANY depth.

    Recursive through unions, which is not a refinement but the whole rule: the common
    way a contract acquires a nested value is by making it optional
    (``inner: Inner | Unsaid``). Checking only the outermost annotation sees a union
    -- neither a TypedDict nor a list -- and lets the nested shape straight through,
    so the gate reports one flat key for a value the page can never render.

    The leaf test is an allow-list, so a container spelled in a way this file has never
    heard of is refused rather than waved through.
    """
    if _is_typed_dict(annotation):
        return True
    args = typing.get_args(annotation)
    if args:
        # A parameterised generic. ``x | y`` is one too, so the members are checked
        # rather than the wrapper: a union of flat leaves is flat.
        if typing.get_origin(annotation) not in (typing.Union, types.UnionType):
            return True
        return any(_is_structured(arg) for arg in args)
    return annotation not in _FLAT_LEAVES


def contract_keys(contract: Any) -> set[str]:
    """Every key *contract* declares.

    Refuses a nested :class:`~typing.TypedDict` or a list of one, at any depth. A
    card's ``data`` is FLAT text -- the host binds one field name to one element's text
    -- so a nested shape has no way to reach the page, and accepting it here would let
    a contract declare structure the html could never read while the equality gate
    stayed green over the leaves.
    """
    hints = typing.get_type_hints(contract)
    if not hints:
        raise ExtractionRefused(f"{contract!r} declares no keys")
    nested = sorted(name for name, annotation in hints.items() if _is_structured(annotation))
    if nested:
        raise ExtractionRefused(
            f"card data is flat text, so a contract cannot nest: {nested}. "
            "Flatten the value into its own field, or render it as one text field."
        )
    optional = sorted(_optional_keys(contract))
    if optional:
        raise ExtractionRefused(
            f"every field the page binds must be supplied, so a contract cannot make one "
            f"optional: {optional}. Declare it required and publish NOT_SAID when there "
            f"is no value; that is what Unsaid is for."
        )
    return set(hints)


def _optional_keys(contract: Any) -> set[str]:
    """The keys a provider is ALLOWED to omit, by either spelling.

    An optional key is a field the html binds and the provider need not supply, so the
    cell renders blank while the equality gate stays green over the NAMES -- the failure
    this package exists to prevent, reached through the gate itself.

    Both spellings have to be asked separately, because neither answer covers the other in
    this codebase's own style. Every module here uses ``from __future__ import
    annotations``, and under it a ``TypedDict`` sees its annotations as strings:
    ``total=False`` still reaches ``__optional_keys__``, while ``NotRequired[...]`` does
    NOT -- the key is reported as required. Resolving the hints with ``include_extras``
    keeps the ``NotRequired`` wrapper visible, which is what makes that half answerable.
    """
    optional = set(getattr(contract, "__optional_keys__", ()))
    annotated = typing.get_type_hints(contract, include_extras=True)
    for name, annotation in annotated.items():
        if typing.get_origin(annotation) is typing.NotRequired:
            optional.add(name)
    return optional


def _mentions_int(annotation: Any) -> bool:
    """Whether *annotation* is ``int`` or a union containing it."""
    if annotation is int:
        return True
    return int in typing.get_args(annotation)


def denominator_gaps(contract: Any) -> list[str]:
    """Int-typed keys with no denominator, i.e. invariant 2's violations.

    A count reaches the page either already shaped ``N/M`` -- in which case its type
    is ``str`` and this says nothing about it -- or as an ``int`` whose total is a
    sibling field named ``<key>_of``. A bare ``int`` with neither is a number a reader
    cannot size, and "7" on a status card reads as complete information.

    A denominator field is itself exempt, otherwise the rule would demand
    ``failed_of_of``.

    The sibling must itself mention ``int``. A present-but-unusable denominator is the
    same defect wearing the right name: ``failed: int`` beside ``failed_of: str`` passes
    a mere membership check while the page still has no number to divide by, so "7"
    renders exactly as sized as it did before.
    """
    hints = typing.get_type_hints(contract)
    gaps: list[str] = []
    for name, annotation in hints.items():
        if name.endswith(_DENOMINATOR_SUFFIX) or not _mentions_int(annotation):
            continue
        sibling = hints.get(f"{name}{_DENOMINATOR_SUFFIX}")
        if sibling is None or not _mentions_int(sibling):
            gaps.append(name)
    return gaps
